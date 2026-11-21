"""KNIGHT X RAIGEN VAULT — Telegram file vault bot.

All file blobs and the JSON catalogue live in a private Telegram storage channel.
No SQL database and no local persistent filesystem are used.
"""
from __future__ import annotations

import hashlib
import html
import io
import json
import logging
import os
import re
import secrets
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

import telebot
from flask import Flask, abort, jsonify, request
from telebot import types

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
BOT_TOKEN = os.getenv("BOT_TOKEN", "9102224053:AAFdY3-fRXn8cQXNLrm-l448A9t4kBVnCwI").strip()
STORAGE_CHAT_ID_RAW = os.getenv("STORAGE_CHAT_ID", "-1004367924445").strip()
OWNER_ID = int(os.getenv("OWNER_ID", "5684263622"))
BOT_USERNAME_FALLBACK = os.getenv("BOT_USERNAME", "@QUANTUM_VAULTbot").lstrip("@").strip()
MAX_FILE_BYTES = 100 * 1024 * 1024  # 100 MiB
INDEX_CAPTION = "QUANTUM VAULT"
CREDIT_TEXT = "DEVELOPER CREDITS\n@QUANTUM_NULL || @RAIGENBRO"
PAGE_SIZE = 5

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is missing. Add it in Render Environment Variables.")
if not STORAGE_CHAT_ID_RAW:
    raise RuntimeError("STORAGE_CHAT_ID is missing. Add your private Telegram storage channel ID.")

try:
    STORAGE_CHAT_ID: Any = int(STORAGE_CHAT_ID_RAW)
except ValueError:
    # A public channel username is accepted for initial setup, but private storage
    # is strongly recommended. The startup validation below rejects public storage.
    STORAGE_CHAT_ID = STORAGE_CHAT_ID_RAW

WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "").strip()
if not WEBHOOK_SECRET:
    WEBHOOK_SECRET = hashlib.sha256((BOT_TOKEN + ":KNIGHT-X-RAIGEN-VAULT").encode()).hexdigest()
WEBHOOK_SECRET = re.sub(r"[^A-Za-z0-9_-]", "", WEBHOOK_SECRET)[:64] or secrets.token_urlsafe(24)

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("knight-vault")

bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML", threaded=True, num_threads=8)
app = Flask(__name__)

# ---------------------------------------------------------------------------
# Telegram-backed catalogue
# ---------------------------------------------------------------------------
STATE_LOCK = threading.RLock()
STATE: Dict[str, Any] = {
    "version": 1,
    "users": {},
    "files": {},
    "admins": [],
    "banned": [],
    "channels": [],
}
INDEX_MESSAGE_ID: Optional[int] = None
BOT_USERNAME = BOT_USERNAME_FALLBACK
BOT_DISPLAY_NAME = "QUANTUM VAULT"
BOOTSTRAP_ERROR: Optional[str] = None
BOOTSTRAPPED = False
BROADCAST_PENDING: Dict[int, str] = {}
BROADCAST_LOCK = threading.Lock()


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")


def normalise_state(value: Any) -> Dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("Storage index is not a JSON object")
    value.setdefault("version", 1)
    value.setdefault("users", {})
    value.setdefault("files", {})
    value.setdefault("admins", [])
    value.setdefault("banned", [])
    value.setdefault("channels", [])
    if not isinstance(value["users"], dict) or not isinstance(value["files"], dict):
        raise ValueError("Storage index has an invalid structure")
    for key in ("admins", "banned", "channels"):
        if not isinstance(value[key], list):
            value[key] = []
    value["admins"] = sorted({int(x) for x in value["admins"] if str(x).lstrip("-").isdigit()})
    value["banned"] = sorted({int(x) for x in value["banned"] if str(x).lstrip("-").isdigit()})
    return value


def _send_index_document(snapshot: Dict[str, Any]) -> int:
    data = json.dumps(snapshot, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    buf = io.BytesIO(data)
    buf.name = "knight_vault_index.json"
    sent = bot.send_document(
        STORAGE_CHAT_ID,
        buf,
        caption=INDEX_CAPTION,
        disable_notification=True,
    )
    return int(sent.message_id)


def save_state() -> None:
    """Persist catalogue by replacing the most recently pinned JSON document."""
    global INDEX_MESSAGE_ID
    with STATE_LOCK:
        snapshot = json.loads(json.dumps(STATE, ensure_ascii=False))
        old_id = INDEX_MESSAGE_ID
        new_id = _send_index_document(snapshot)
        bot.pin_chat_message(STORAGE_CHAT_ID, new_id, disable_notification=True)
        INDEX_MESSAGE_ID = new_id
        if old_id and old_id != new_id:
            try:
                bot.delete_message(STORAGE_CHAT_ID, old_id)
            except Exception as exc:  # an older index copy is harmless
                log.warning("Could not delete previous index message %s: %s", old_id, exc)


def load_state() -> None:
    """Load index from the storage channel's newest pinned message."""
    global INDEX_MESSAGE_ID
    chat = bot.get_chat(STORAGE_CHAT_ID)
    if getattr(chat, "type", None) != "channel":
        raise RuntimeError("STORAGE_CHAT_ID must point to a Telegram channel (not a group or user chat).")
    if getattr(chat, "username", None):
        raise RuntimeError("Storage channel must be PRIVATE. Remove its public username before using it.")

    # Validate that the bot can write, pin and later delete channel messages.
    me = bot.get_me()
    member = bot.get_chat_member(STORAGE_CHAT_ID, me.id)
    if getattr(member, "status", "") not in ("administrator", "creator"):
        raise RuntimeError("Add the bot as an administrator in the private storage channel.")
    if getattr(member, "status", "") == "administrator":
        required = ("can_post_messages", "can_edit_messages", "can_delete_messages")
        missing = [name for name in required if not getattr(member, name, False)]
        if missing:
            raise RuntimeError("Storage channel bot admin is missing permissions: " + ", ".join(missing))

    pinned = getattr(chat, "pinned_message", None)
    if pinned and getattr(pinned, "document", None) and (getattr(pinned, "caption", "") or "").strip() == INDEX_CAPTION:
        file_info = bot.get_file(pinned.document.file_id)
        raw = bot.download_file(file_info.file_path)
        loaded = normalise_state(json.loads(raw.decode("utf-8")))
        with STATE_LOCK:
            STATE.clear()
            STATE.update(loaded)
        INDEX_MESSAGE_ID = int(pinned.message_id)
        log.info("Loaded Telegram index: %d users, %d files", len(STATE["users"]), len(STATE["files"]))
    else:
        # First launch: create and pin the initial Telegram-hosted JSON index.
        with STATE_LOCK:
            STATE.clear()
            STATE.update({"version": 1, "users": {}, "files": {}, "admins": [], "banned": [], "channels": []})
        INDEX_MESSAGE_ID = None
        save_state()
        log.info("Created a new Telegram-hosted index")


def is_owner(user_id: int) -> bool:
    return int(user_id) == OWNER_ID


def is_admin(user_id: int) -> bool:
    return is_owner(user_id) or int(user_id) in STATE.get("admins", [])


def is_banned(user_id: int) -> bool:
    return int(user_id) in STATE.get("banned", [])


def all_admin_ids() -> List[int]:
    return list(dict.fromkeys([OWNER_ID] + [int(x) for x in STATE.get("admins", [])]))


def escape(value: Any) -> str:
    return html.escape(str(value or ""), quote=True)


def user_full_name(user: Any) -> str:
    name = " ".join(part for part in [getattr(user, "first_name", ""), getattr(user, "last_name", "")] if part)
    return name or "Unknown"


def register_user(user: Any) -> bool:
    """Register/update a user. Returns True only on their first /start."""
    uid = int(user.id)
    key = str(uid)
    first_join = False
    changed = False
    with STATE_LOCK:
        record = STATE["users"].get(key)
        if record is None:
            record = {
                "user_id": uid,
                "first_name": getattr(user, "first_name", "") or "",
                "last_name": getattr(user, "last_name", "") or "",
                "username": getattr(user, "username", None),
                "phone": None,
                "joined_at": now_iso(),
                "last_seen": now_iso(),
            }
            STATE["users"][key] = record
            first_join = True
            changed = True
        else:
            for field, value in (
                ("first_name", getattr(user, "first_name", "") or ""),
                ("last_name", getattr(user, "last_name", "") or ""),
                ("username", getattr(user, "username", None)),
            ):
                if record.get(field) != value:
                    record[field] = value
                    changed = True
            record["last_seen"] = now_iso()
    if changed:
        try:
            save_state()
        except Exception:
            log.exception("Could not persist user registration")
    if first_join:
        notify_admins_new_user(uid)
    return first_join


def notify_admins_new_user(user_id: int) -> None:
    record = STATE.get("users", {}).get(str(user_id), {})
    username = record.get("username")
    username_text = "@" + username if username else "Not set"
    full_name = " ".join(x for x in [record.get("first_name", ""), record.get("last_name", "")] if x) or "Unknown"
    phone = record.get("phone") or "Not shared"
    message = (
        "<b>🆕 NEW VAULT MEMBER</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"<b>Name:</b> {escape(full_name)}\n"
        f"<b>Username:</b> {escape(username_text)}\n"
        f"<b>User ID:</b> <code>{user_id}</code>\n"
        f"<b>Phone:</b> {escape(phone)}\n"
        f"<b>Joined:</b> {escape(record.get('joined_at', now_iso()))}\n"
        "━━━━━━━━━━━━━━━━━━"
    )
    for admin_id in all_admin_ids():
        try:
            bot.send_message(admin_id, message, disable_web_page_preview=True)
        except Exception as exc:
            log.warning("Join notification failed for admin %s: %s", admin_id, exc)

# ---------------------------------------------------------------------------
# UI helpers
# ---------------------------------------------------------------------------

def main_keyboard(user_id: int) -> types.ReplyKeyboardMarkup:
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2, input_field_placeholder="Send a file, image, video or code…")
    kb.add(types.KeyboardButton("📤 Upload File"), types.KeyboardButton("📁 My Vault"))
    kb.add(types.KeyboardButton("ℹ️ Help"), types.KeyboardButton("📱 Share Phone"))
    if is_admin(user_id):
        kb.add(types.KeyboardButton("👑 Admin Panel"))
    return kb


def home_text(user: Any) -> str:
    name = escape(getattr(user, "first_name", "there") or "there")
    return (
        "<b>⚔️ QUANTUM VAULT</b>\n"
        "<i>Your personal Telegram cloud vault.</i>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"Welcome, <b>{name}</b>!\n\n"
        "📤 Send any supported file, image, video, audio or code/text here.\n"
        "🔗 Get a private share link for every upload.\n"
        "📁 Only you can see your personal vault list.\n"
        "🛡️ Files are stored in Telegram cloud storage.\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "<b>Maximum upload:</b> 100 MB per item\n"
        "<b>Developer:</b> @QUANTUM_NULL || @RAIGENBRO"
    )


def admin_panel_markup() -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton("📊 Statistics", callback_data="a:stats"),
        types.InlineKeyboardButton("🗂 Recent uploads", callback_data="a:files:0"),
    )
    kb.add(
        types.InlineKeyboardButton("📣 Broadcast users", callback_data="a:bcall"),
        types.InlineKeyboardButton("🔐 Broadcast admins", callback_data="a:bcadmin"),
    )
    kb.add(
        types.InlineKeyboardButton("📡 Verification channels", callback_data="a:channels"),
        types.InlineKeyboardButton("🔄 Refresh", callback_data="a:home"),
    )
    return kb


def verification_keyboard(pending_token: Optional[str] = None) -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup(row_width=1)
    for channel in STATE.get("channels", []):
        kb.add(types.InlineKeyboardButton("➕ Join " + (channel.get("title") or "required channel")[:45], url=channel.get("link")))
    callback = "v:check:" + pending_token if pending_token else "v:check"
    kb.add(types.InlineKeyboardButton("✅ I Joined — Verify", callback_data=callback))
    return kb


def send_verification_prompt(chat_id: int, note: str = "", pending_token: Optional[str] = None) -> None:
    text = (
        "<b>🔐 CHANNEL VERIFICATION REQUIRED</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "To use QUANTUM VAULT, join every required channel below, then tap <b>I Joined — Verify</b>.\n\n"
        "Until verification is complete, uploads, downloads and shared links stay locked.\n"
        "━━━━━━━━━━━━━━━━━━"
    )
    if note:
        text += "\n\n<i>" + escape(note) + "</i>"
    bot.send_message(chat_id, text, reply_markup=verification_keyboard(pending_token), disable_web_page_preview=True)


def is_member_of_channel(user_id: int, channel_id: Any) -> bool:
    member = bot.get_chat_member(channel_id, user_id)
    status = getattr(member, "status", "")
    if status in ("creator", "administrator", "member"):
        return True
    if status == "restricted":
        return bool(getattr(member, "is_member", False))
    return False


def check_verification(user_id: int) -> Tuple[bool, Optional[str]]:
    if is_admin(user_id):
        return True, None
    for channel in STATE.get("channels", []):
        try:
            if not is_member_of_channel(user_id, channel["chat_id"]):
                return False, channel.get("title") or "a required channel"
        except Exception as exc:
            log.warning("Could not verify user %s in %s: %s", user_id, channel.get("chat_id"), exc)
            return False, channel.get("title") or "a required channel"
    return True, None


def require_access(message_or_call: Any, user_id: int, chat_id: int) -> bool:
    if is_banned(user_id):
        try:
            bot.send_message(chat_id, "⛔ <b>Your access to this vault has been restricted.</b>")
        except Exception:
            pass
        return False
    verified, missing = check_verification(user_id)
    if not verified:
        send_verification_prompt(chat_id, f"Please join: {missing}")
        return False
    return True


def start_link(token: str) -> str:
    return f"https://t.me/{BOT_USERNAME}?start=f_{token}"

# ---------------------------------------------------------------------------
# File recognition and storage
# ---------------------------------------------------------------------------
MEDIA_FIELDS = (
    "document", "photo", "video", "animation", "audio", "voice",
    "video_note", "sticker", "video_chat_started", "video_chat_ended",
    "paid_media", "live_photo",
)


def get_upload_details(message: Any) -> Tuple[str, str, int, bool, str]:
    """Return kind, filename, size, caption_supported, original caption."""
    content_type = getattr(message, "content_type", "unknown")
    caption = getattr(message, "caption", None) or ""
    kind = content_type
    filename = content_type.replace("_", " ").title() or "Telegram item"
    size = 0
    caption_supported = content_type in {"photo", "document", "video", "animation", "audio"}

    obj = getattr(message, content_type, None)
    if content_type == "photo":
        photos = getattr(message, "photo", []) or []
        if photos:
            obj = photos[-1]
            filename = "photo.jpg"
    elif content_type == "document" and obj:
        filename = getattr(obj, "file_name", None) or "document"
    elif content_type == "video" and obj:
        filename = getattr(obj, "file_name", None) or "video.mp4"
    elif content_type == "animation" and obj:
        filename = getattr(obj, "file_name", None) or "animation.mp4"
    elif content_type == "audio" and obj:
        filename = getattr(obj, "file_name", None) or "audio"
    elif content_type == "voice":
        filename = "voice_message.ogg"
    elif content_type == "video_note":
        filename = "video_note.mp4"
    elif content_type == "sticker" and obj:
        filename = "sticker.webp"
    elif content_type == "text":
        filename = "code_or_text.txt"
        kind = "text/code"
        # Telegram messages are limited to 4096 characters; this is a text item.
        size = len((getattr(message, "text", "") or "").encode("utf-8"))
        return kind, filename, size, False, getattr(message, "text", "") or ""

    if obj:
        size = int(getattr(obj, "file_size", 0) or 0)
    return kind, filename, size, caption_supported, caption


def safe_caption(original: str) -> str:
    suffix = "\n\n" + CREDIT_TEXT
    # Telegram media captions have a 1024-character limit after HTML parsing.
    room = max(0, 1024 - len(suffix))
    base = original or ""
    if len(base) > room:
        base = base[: max(0, room - 18)] + "… [caption cut]"
    return escape(base) + suffix


def safe_html_text(original: str, output_limit: int) -> str:
    """Escape literal user text and keep its serialized HTML within Telegram limits."""
    raw = original or ""
    low, high = 0, len(raw)
    while low < high:
        middle = (low + high + 1) // 2
        if len(escape(raw[:middle])) <= output_limit:
            low = middle
        else:
            high = middle - 1
    return escape(raw[:low])


def notify_admins_file(record: Dict[str, Any]) -> None:
    uid = int(record["owner_id"])
    username = record.get("owner_username")
    owner_name = record.get("owner_name") or "Unknown"
    caption = record.get("caption") or "—"
    text = (
        "<b>📥 NEW FILE UPLOADED</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"<b>File:</b> {escape(record.get('filename'))}\n"
        f"<b>Type:</b> {escape(record.get('kind'))}\n"
        f"<b>Size:</b> {escape(format_size(record.get('size', 0)))}\n"
        f"<b>User:</b> {escape(owner_name)}\n"
        f"<b>Username:</b> {escape('@' + username if username else 'Not set')}\n"
        f"<b>User ID:</b> <code>{uid}</code>\n"
        f"<b>Caption:</b> {escape(caption[:500])}\n"
        f"<b>Uploaded:</b> {escape(record.get('created_at'))}\n"
        f"<b>Share:</b> <a href=\"{escape(start_link(record['token']))}\">Open shared file</a>\n"
        "━━━━━━━━━━━━━━━━━━"
    )
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("📥 Download file", callback_data="ad:" + record["token"]))
    for admin_id in all_admin_ids():
        try:
            bot.send_message(admin_id, text, reply_markup=kb, disable_web_page_preview=True)
            bot.copy_message(admin_id, STORAGE_CHAT_ID, int(record["storage_message_id"]))
            if record.get("credit_message_id"):
                bot.copy_message(admin_id, STORAGE_CHAT_ID, int(record["credit_message_id"]))
        except Exception as exc:
            log.warning("Admin file notification failed for %s: %s", admin_id, exc)


def format_size(size: int) -> str:
    if not size:
        return "Unknown / text"
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{size} B"


def store_upload(message: Any) -> None:
    user = message.from_user
    uid = int(user.id)
    if not require_access(message, uid, message.chat.id):
        return

    kind, filename, size, caption_supported, caption = get_upload_details(message)
    if size > MAX_FILE_BYTES:
        bot.reply_to(message, "⛔ <b>File too large!</b>\nMaximum allowed size is <b>100 MB</b>.")
        return

    status_msg = bot.reply_to(message, "⏳ <b>Uploading to QUANTUM Vault…</b>")
    try:
        if kind == "text/code":
            # Text/code is stored as a Telegram message, not as a local file.
            text_suffix = "\n\n" + CREDIT_TEXT
            stored_text = safe_html_text(caption, 4096 - len(text_suffix)) + text_suffix
            stored = bot.send_message(
                STORAGE_CHAT_ID,
                stored_text,
                disable_web_page_preview=True,
            )
            storage_message_id = int(stored.message_id)
            credit_message_id = None
            original_caption = caption
        else:
            copy_kwargs: Dict[str, Any] = {}
            credit_message_id = None
            if caption_supported:
                copy_kwargs["caption"] = safe_caption(caption)
            copied = bot.copy_message(
                STORAGE_CHAT_ID,
                message.chat.id,
                message.message_id,
                parse_mode="HTML",
                **copy_kwargs,
            )
            storage_message_id = int(copied.message_id)
            if not caption_supported:
                credit_msg = bot.send_message(STORAGE_CHAT_ID, CREDIT_TEXT, disable_notification=True)
                credit_message_id = int(credit_msg.message_id)
            original_caption = caption

        token = secrets.token_urlsafe(9).replace("-", "a").replace("_", "b")[:12]
        record = {
            "token": token,
            "owner_id": uid,
            "owner_name": user_full_name(user),
            "owner_username": getattr(user, "username", None),
            "filename": filename,
            "kind": kind,
            "size": int(size or 0),
            "caption": original_caption,
            "storage_message_id": storage_message_id,
            "credit_message_id": credit_message_id,
            "created_at": now_iso(),
        }
        with STATE_LOCK:
            STATE["files"][token] = record
        save_state()

        # Replace the temporary status with a polished result card.
        try:
            bot.delete_message(message.chat.id, status_msg.message_id)
        except Exception:
            pass
        link = start_link(token)
        kb = types.InlineKeyboardMarkup(row_width=2)
        kb.row(
            types.InlineKeyboardButton("📋 Copy Link", copy_text=types.CopyTextButton(text=link)),
            types.InlineKeyboardButton("🔗 Open Link", url=link),
        )
        kb.add(types.InlineKeyboardButton("📁 My Vault", callback_data="my:0"))
        bot.send_message(
            message.chat.id,
            "<b>✅ FILE SAVED SUCCESSFULLY</b>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"<b>Name:</b> {escape(filename)}\n"
            f"<b>Type:</b> {escape(kind)}\n"
            f"<b>Size:</b> {escape(format_size(size))}\n\n"
            "<b>Your share link:</b>\n"
            f"<code>{escape(link)}</code>\n\n"
            "Anyone with this link can open the file (after channel verification, if enabled).\n\n"
            f"<i>{escape(CREDIT_TEXT)}</i>",
            reply_markup=kb,
            disable_web_page_preview=True,
        )
        notify_admins_file(record)
    except Exception as exc:
        log.exception("Upload failed for user %s", uid)
        try:
            bot.edit_message_text(
                "❌ <b>Upload failed.</b> Please try again.\n\n<i>" + escape(str(exc)[:300]) + "</i>",
                message.chat.id,
                status_msg.message_id,
            )
        except Exception:
            bot.reply_to(message, "❌ Upload failed. Check the bot's storage channel permissions and try again.")


def deliver_file(chat_id: int, token: str) -> bool:
    record = STATE.get("files", {}).get(token)
    if not record:
        bot.send_message(chat_id, "❌ <b>This share link is invalid or the file was removed.</b>")
        return False
    try:
        bot.copy_message(chat_id, STORAGE_CHAT_ID, int(record["storage_message_id"]))
        if record.get("credit_message_id"):
            bot.copy_message(chat_id, STORAGE_CHAT_ID, int(record["credit_message_id"]))
        return True
    except Exception as exc:
        log.exception("Could not deliver file token=%s", token)
        bot.send_message(chat_id, "❌ File delivery failed. The storage message may have been removed.")
        return False

# ---------------------------------------------------------------------------
# Personal vault
# ---------------------------------------------------------------------------

def user_files(user_id: int) -> List[Dict[str, Any]]:
    rows = [r for r in STATE.get("files", {}).values() if int(r.get("owner_id", 0)) == int(user_id)]
    return sorted(rows, key=lambda item: item.get("created_at", ""), reverse=True)


def show_my_files(chat_id: int, user_id: int, page: int = 0) -> None:
    rows = user_files(user_id)
    if not rows:
        bot.send_message(
            chat_id,
            "<b>📁 YOUR PERSONAL VAULT</b>\n\nYour vault is empty. Send a file, photo, video or code snippet to save your first item.",
            reply_markup=main_keyboard(user_id),
        )
        return
    page = max(0, min(page, (len(rows) - 1) // PAGE_SIZE))
    chunk = rows[page * PAGE_SIZE : (page + 1) * PAGE_SIZE]
    kb = types.InlineKeyboardMarkup(row_width=2)
    lines = [
        "<b>📁 YOUR PERSONAL VAULT</b>",
        f"<i>{len(rows)} saved item(s) · Page {page + 1}/{(len(rows) - 1) // PAGE_SIZE + 1}</i>",
        "━━━━━━━━━━━━━━━━━━",
    ]
    for number, record in enumerate(chunk, start=page * PAGE_SIZE + 1):
        lines.append(
            f"<b>{number}. {escape(record.get('filename'))}</b>\n"
            f"{escape(record.get('kind'))} · {escape(format_size(int(record.get('size', 0))))} · {escape(record.get('created_at', ''))}"
        )
        kb.row(
            types.InlineKeyboardButton("📥 Open", callback_data="op:" + record["token"]),
            types.InlineKeyboardButton("🔗 Share", callback_data="sh:" + record["token"]),
            types.InlineKeyboardButton("🗑 Delete", callback_data="del:" + record["token"]),
        )
    nav = []
    if page > 0:
        nav.append(types.InlineKeyboardButton("⬅️ Previous", callback_data=f"my:{page - 1}"))
    if (page + 1) * PAGE_SIZE < len(rows):
        nav.append(types.InlineKeyboardButton("Next ➡️", callback_data=f"my:{page + 1}"))
    if nav:
        kb.row(*nav)
    bot.send_message(chat_id, "\n\n".join(lines), reply_markup=kb, disable_web_page_preview=True)


def admin_files_page(chat_id: int, page: int = 0) -> None:
    rows = sorted(STATE.get("files", {}).values(), key=lambda item: item.get("created_at", ""), reverse=True)
    if not rows:
        bot.send_message(chat_id, "🗂 No uploads yet.")
        return
    page = max(0, min(page, (len(rows) - 1) // PAGE_SIZE))
    chunk = rows[page * PAGE_SIZE : (page + 1) * PAGE_SIZE]
    kb = types.InlineKeyboardMarkup(row_width=2)
    lines = [f"<b>🗂 RECENT UPLOADS · {len(rows)} total</b>", "━━━━━━━━━━━━━━━━━━"]
    for record in chunk:
        lines.append(
            f"<b>{escape(record.get('filename'))}</b>\n"
            f"{escape(record.get('owner_name'))} · <code>{record.get('owner_id')}</code>\n"
            f"{escape(format_size(int(record.get('size', 0))))} · {escape(record.get('created_at', ''))}"
        )
        kb.add(types.InlineKeyboardButton("📥 Download · " + record["token"][:6], callback_data="ad:" + record["token"]))
    nav = []
    if page > 0:
        nav.append(types.InlineKeyboardButton("⬅️ Previous", callback_data=f"a:files:{page - 1}"))
    if (page + 1) * PAGE_SIZE < len(rows):
        nav.append(types.InlineKeyboardButton("Next ➡️", callback_data=f"a:files:{page + 1}"))
    if nav:
        kb.row(*nav)
    bot.send_message(chat_id, "\n\n".join(lines), reply_markup=kb)

# ---------------------------------------------------------------------------
# Admin functions
# ---------------------------------------------------------------------------

def show_admin_panel(chat_id: int, user_id: int) -> None:
    if not is_admin(user_id):
        bot.send_message(chat_id, "⛔ Admin access only.")
        return
    bot.send_message(
        chat_id,
        "<b>👑 QUANTUM VAULT · CONTROL PANEL</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"<b>Owner UID:</b> <code>{OWNER_ID}</code>\n"
        f"<b>Admins:</b> {len(STATE.get('admins', [])) + 1}\n"
        f"<b>Users:</b> {len(STATE.get('users', {}))}\n"
        f"<b>Files:</b> {len(STATE.get('files', {}))}\n"
        f"<b>Banned:</b> {len(STATE.get('banned', []))}\n"
        f"<b>Verification channels:</b> {len(STATE.get('channels', []))}\n\n"
        "<i>Commands: /addadmin /removeadmin /ban /unban /broadcast /broadcast_admin /addchannel /removechannel /channels</i>",
        reply_markup=admin_panel_markup(),
    )


def admin_stats(chat_id: int) -> None:
    total_bytes = sum(int(r.get("size", 0)) for r in STATE.get("files", {}).values())
    bot.send_message(
        chat_id,
        "<b>📊 VAULT STATISTICS</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"👥 Users: <b>{len(STATE.get('users', {}))}</b>\n"
        f"📦 Files: <b>{len(STATE.get('files', {}))}</b>\n"
        f"💾 Indexed file size: <b>{escape(format_size(total_bytes))}</b>\n"
        f"🛡️ Banned users: <b>{len(STATE.get('banned', []))}</b>\n"
        f"👑 Admins: <b>{len(STATE.get('admins', [])) + 1}</b>\n"
        f"📡 Required channels: <b>{len(STATE.get('channels', []))}</b>\n"
        f"🕒 Server time: <code>{escape(now_iso())}</code>"
    )


def begin_broadcast(user_id: int, mode: str, chat_id: int) -> None:
    if not is_admin(user_id):
        return
    BROADCAST_PENDING[user_id] = mode
    label = "all registered users" if mode == "all" else "only owner and admins"
    bot.send_message(
        chat_id,
        f"<b>📣 BROADCAST MODE</b>\n\nSend the message/media you want to broadcast to <b>{label}</b>.\n\nSend /cancel to cancel.",
    )


def run_broadcast(source_message: Any, mode: str, sender_id: int) -> None:
    if mode == "admins":
        targets = all_admin_ids()
    else:
        targets = [int(uid) for uid in STATE.get("users", {}).keys() if not is_banned(int(uid))]
    delivered = 0
    failed = 0
    for target in targets:
        try:
            bot.copy_message(target, source_message.chat.id, source_message.message_id)
            delivered += 1
        except Exception:
            failed += 1
        time.sleep(0.045)  # stay under normal Telegram message-rate limits
    try:
        bot.send_message(
            sender_id,
            "<b>✅ BROADCAST FINISHED</b>\n"
            f"Delivered: <b>{delivered}</b>\n"
            f"Failed/blocked: <b>{failed}</b>\n"
            f"Target group: <b>{'Admins' if mode == 'admins' else 'All users'}</b>",
        )
    except Exception:
        pass


def add_verification_channel(chat_id: Any, invite: Optional[str], admin_user_id: int) -> str:
    chat = bot.get_chat(chat_id)
    if getattr(chat, "type", None) != "channel":
        raise ValueError("That ID/username is not a channel.")
    me = bot.get_me()
    member = bot.get_chat_member(chat.id, me.id)
    if getattr(member, "status", "") not in ("administrator", "creator"):
        raise ValueError("First add this bot as a channel administrator.")
    title = getattr(chat, "title", None) or str(chat.id)
    username = getattr(chat, "username", None)
    if invite:
        link = invite
    elif username:
        link = f"https://t.me/{username}"
    else:
        try:
            link = bot.export_chat_invite_link(chat.id)
        except Exception:
            raise ValueError("Private channel detected. Pass its invite link too: /addchannel -100... https://t.me/+invite")
    if not (link.startswith("https://t.me/") or link.startswith("http://t.me/")):
        raise ValueError("Invite link must start with https://t.me/")
    with STATE_LOCK:
        if any(str(ch.get("chat_id")) == str(chat.id) for ch in STATE.get("channels", [])):
            return f"<b>{escape(title)}</b> is already in the verification list."
        STATE["channels"].append({"chat_id": int(chat.id), "title": title, "link": link, "added_by": admin_user_id, "added_at": now_iso()})
    save_state()
    return f"✅ Added <b>{escape(title)}</b> to mandatory verification."


def channel_list_text() -> str:
    channels = STATE.get("channels", [])
    if not channels:
        return "<b>📡 No verification channels configured.</b>"
    lines = ["<b>📡 REQUIRED VERIFICATION CHANNELS</b>", "━━━━━━━━━━━━━━━━━━"]
    for i, channel in enumerate(channels, start=1):
        lines.append(f"{i}. <a href=\"{escape(channel.get('link'))}\">{escape(channel.get('title'))}</a> · <code>{channel.get('chat_id')}</code>")
    return "\n".join(lines)

# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------
@bot.message_handler(commands=["start"])
def cmd_start(message: Any) -> None:
    if message.chat.type != "private":
        return
    register_user(message.from_user)
    uid = int(message.from_user.id)
    if is_banned(uid):
        bot.send_message(message.chat.id, "⛔ Your access to this vault has been restricted.")
        return
    payload = " ".join((message.text or "").split()[1:]).strip()
    verified, missing = check_verification(uid)
    if not verified:
        pending_token = payload[2:] if payload.startswith("f_") else None
        send_verification_prompt(message.chat.id, f"Please join: {missing}", pending_token=pending_token)
        return
    if payload.startswith("f_"):
        token = payload[2:]
        if token in STATE.get("files", {}):
            deliver_file(message.chat.id, token)
            bot.send_message(message.chat.id, "<i>Shared by KNIGHT X RAIGEN VAULT</i>", reply_markup=main_keyboard(uid))
            return
        bot.send_message(message.chat.id, "❌ This share link is invalid or expired.", reply_markup=main_keyboard(uid))
        return
    bot.send_message(message.chat.id, home_text(message.from_user), reply_markup=main_keyboard(uid), disable_web_page_preview=True)


@bot.message_handler(commands=["help"])
def cmd_help(message: Any) -> None:
    uid = int(message.from_user.id)
    if message.chat.type != "private" or not require_access(message, uid, message.chat.id):
        return
    bot.send_message(
        message.chat.id,
        "<b>ℹ️ HOW TO USE YOUR VAULT</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "1. Send a photo, video, document, audio, sticker, voice note or code/text.\n"
        "2. Add a caption before sending, if needed.\n"
        "3. The bot saves it and creates a share link.\n"
        "4. Open <b>My Vault</b> to view, share or delete your own items.\n\n"
        "<b>Maximum:</b> 100 MB per item.\n"
        "<b>Phone number:</b> only visible after you voluntarily share your contact.\n\n"
        f"{escape(CREDIT_TEXT)}",
        reply_markup=main_keyboard(uid),
    )


@bot.message_handler(commands=["myfiles", "vault"])
def cmd_myfiles(message: Any) -> None:
    uid = int(message.from_user.id)
    if message.chat.type != "private" or not require_access(message, uid, message.chat.id):
        return
    show_my_files(message.chat.id, uid, 0)


@bot.message_handler(commands=["admin"])
def cmd_admin(message: Any) -> None:
    show_admin_panel(message.chat.id, int(message.from_user.id))


@bot.message_handler(commands=["stats"])
def cmd_stats(message: Any) -> None:
    if is_admin(message.from_user.id):
        admin_stats(message.chat.id)


@bot.message_handler(commands=["adminfiles"])
def cmd_adminfiles(message: Any) -> None:
    if is_admin(message.from_user.id):
        admin_files_page(message.chat.id, 0)


@bot.message_handler(commands=["addadmin"])
def cmd_addadmin(message: Any) -> None:
    uid = int(message.from_user.id)
    if not is_owner(uid):
        bot.reply_to(message, "⛔ Only the owner can add admins.")
        return
    parts = (message.text or "").split()
    if len(parts) < 2 or not parts[1].lstrip("-").isdigit():
        bot.reply_to(message, "Usage: <code>/addadmin USER_ID</code>")
        return
    target = int(parts[1])
    if target == OWNER_ID:
        bot.reply_to(message, "Owner is already the highest authority.")
        return
    with STATE_LOCK:
        if target not in STATE["admins"]:
            STATE["admins"].append(target)
    save_state()
    bot.reply_to(message, f"✅ Added <code>{target}</code> as an admin.")


@bot.message_handler(commands=["removeadmin"])
def cmd_removeadmin(message: Any) -> None:
    uid = int(message.from_user.id)
    if not is_owner(uid):
        bot.reply_to(message, "⛔ Only the owner can remove admins.")
        return
    parts = (message.text or "").split()
    if len(parts) < 2 or not parts[1].lstrip("-").isdigit():
        bot.reply_to(message, "Usage: <code>/removeadmin USER_ID</code>")
        return
    target = int(parts[1])
    with STATE_LOCK:
        STATE["admins"] = [x for x in STATE["admins"] if int(x) != target]
    save_state()
    bot.reply_to(message, f"✅ Removed <code>{target}</code> from admins.")


@bot.message_handler(commands=["ban"])
def cmd_ban(message: Any) -> None:
    if not is_admin(message.from_user.id):
        return
    parts = (message.text or "").split()
    if len(parts) < 2 or not parts[1].lstrip("-").isdigit():
        bot.reply_to(message, "Usage: <code>/ban USER_ID</code>")
        return
    target = int(parts[1])
    if is_owner(target) or target in all_admin_ids():
        bot.reply_to(message, "⛔ You cannot ban the owner or another admin.")
        return
    with STATE_LOCK:
        if target not in STATE["banned"]:
            STATE["banned"].append(target)
    save_state()
    bot.reply_to(message, f"⛔ User <code>{target}</code> has been banned.")
    try:
        bot.send_message(target, "⛔ Your access to KNIGHT X RAIGEN VAULT has been restricted by an administrator.")
    except Exception:
        pass


@bot.message_handler(commands=["unban"])
def cmd_unban(message: Any) -> None:
    if not is_admin(message.from_user.id):
        return
    parts = (message.text or "").split()
    if len(parts) < 2 or not parts[1].lstrip("-").isdigit():
        bot.reply_to(message, "Usage: <code>/unban USER_ID</code>")
        return
    target = int(parts[1])
    with STATE_LOCK:
        STATE["banned"] = [x for x in STATE["banned"] if int(x) != target]
    save_state()
    bot.reply_to(message, f"✅ User <code>{target}</code> has been unbanned.")


@bot.message_handler(commands=["broadcast"])
def cmd_broadcast(message: Any) -> None:
    begin_broadcast(int(message.from_user.id), "all", message.chat.id)


@bot.message_handler(commands=["broadcast_admin"])
def cmd_broadcast_admin(message: Any) -> None:
    begin_broadcast(int(message.from_user.id), "admins", message.chat.id)


@bot.message_handler(commands=["cancel"])
def cmd_cancel(message: Any) -> None:
    BROADCAST_PENDING.pop(int(message.from_user.id), None)
    bot.reply_to(message, "✅ Cancelled.")


@bot.message_handler(commands=["addchannel"])
def cmd_addchannel(message: Any) -> None:
    if not is_admin(message.from_user.id):
        return
    parts = (message.text or "").split(maxsplit=2)
    if len(parts) < 2:
        bot.reply_to(
            message,
            "<b>Add verification channel</b>\n"
            "Public: <code>/addchannel @channelusername</code>\n"
            "Private: <code>/addchannel -1001234567890 https://t.me/+invite</code>\n\n"
            "The bot must already be an administrator in that channel.",
        )
        return
    channel_ref = parts[1]
    invite = parts[2].strip() if len(parts) > 2 else None
    try:
        result = add_verification_channel(channel_ref, invite, int(message.from_user.id))
        bot.reply_to(message, result)
    except Exception as exc:
        bot.reply_to(message, "❌ " + escape(str(exc)))


@bot.message_handler(commands=["removechannel", "delchannel"])
def cmd_removechannel(message: Any) -> None:
    if not is_admin(message.from_user.id):
        return
    parts = (message.text or "").split()
    if len(parts) < 2:
        bot.reply_to(message, "Usage: <code>/removechannel CHANNEL_ID</code>")
        return
    target = parts[1]
    before = len(STATE.get("channels", []))
    with STATE_LOCK:
        STATE["channels"] = [c for c in STATE["channels"] if str(c.get("chat_id")) != target]
    if len(STATE["channels"]) != before:
        save_state()
        bot.reply_to(message, "✅ Verification channel removed.")
    else:
        bot.reply_to(message, "Channel ID not found. Use /channels to see the configured IDs.")


@bot.message_handler(commands=["channels"])
def cmd_channels(message: Any) -> None:
    if is_admin(message.from_user.id):
        bot.send_message(message.chat.id, channel_list_text(), disable_web_page_preview=True)


@bot.message_handler(commands=["id"])
def cmd_id(message: Any) -> None:
    user = message.from_user
    bot.reply_to(
        message,
        f"🆔 Your Telegram user ID: <code>{int(user.id)}</code>\n"
        f"Username: {escape('@' + user.username if getattr(user, 'username', None) else 'Not set')}",
    )


@bot.message_handler(commands=["sharephone"])
def cmd_sharephone(message: Any) -> None:
    if message.chat.type != "private":
        return
    uid = int(message.from_user.id)
    if is_banned(uid):
        return
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    kb.add(types.KeyboardButton("📱 Share my phone number", request_contact=True))
    kb.add(types.KeyboardButton("Cancel"))
    bot.send_message(
        message.chat.id,
        "Telegram hides your phone number from bots. Tap the button only if you choose to share it with this bot.",
        reply_markup=kb,
    )

# ---------------------------------------------------------------------------
# Contact, button menu and general uploads
# ---------------------------------------------------------------------------
@bot.message_handler(content_types=["contact"])
def handle_contact(message: Any) -> None:
    uid = int(message.from_user.id)
    contact = message.contact
    if contact and int(getattr(contact, "user_id", 0) or 0) == uid:
        with STATE_LOCK:
            record = STATE["users"].setdefault(str(uid), {
                "user_id": uid,
                "first_name": getattr(message.from_user, "first_name", "") or "",
                "last_name": getattr(message.from_user, "last_name", "") or "",
                "username": getattr(message.from_user, "username", None),
                "joined_at": now_iso(),
                "last_seen": now_iso(),
            })
            record["phone"] = contact.phone_number
        save_state()
        bot.send_message(message.chat.id, "✅ Phone number shared successfully.", reply_markup=main_keyboard(uid))
        for admin_id in all_admin_ids():
            try:
                bot.send_message(admin_id, f"📱 User <code>{uid}</code> voluntarily shared a phone number: <code>{escape(contact.phone_number)}</code>")
            except Exception:
                pass
    else:
        bot.send_message(message.chat.id, "Please share your own contact using Telegram's button.", reply_markup=main_keyboard(uid))


@bot.message_handler(
    content_types=[
        "text", "photo", "document", "video", "animation", "audio",
        "voice", "video_note", "sticker", "location", "venue",
        "dice", "game", "poll", "paid_media", "live_photo",
        "web_app_data", "user_shared", "chat_shared", "story",
    ]
)
def handle_any_message(message: Any) -> None:
    if message.chat.type != "private":
        return
    uid = int(message.from_user.id)
    register_user(message.from_user)

    # Broadcast reply has priority over normal uploads.
    pending_mode = BROADCAST_PENDING.get(uid)
    if pending_mode and is_admin(uid):
        if message.content_type == "text" and (message.text or "").strip() == "/cancel":
            BROADCAST_PENDING.pop(uid, None)
            bot.reply_to(message, "✅ Broadcast cancelled.")
            return
        BROADCAST_PENDING.pop(uid, None)
        bot.reply_to(message, "⏳ Broadcast queued. I will report the result when finished.")
        thread = threading.Thread(target=run_broadcast, args=(message, pending_mode, uid), daemon=True)
        thread.start()
        return

    if is_banned(uid):
        bot.send_message(message.chat.id, "⛔ Your access to this vault has been restricted.")
        return

    text = (getattr(message, "text", None) or "").strip()
    if text == "📁 My Vault":
        if require_access(message, uid, message.chat.id):
            show_my_files(message.chat.id, uid, 0)
        return
    if text == "📤 Upload File":
        if require_access(message, uid, message.chat.id):
            bot.send_message(message.chat.id, "📤 Send your file, image, video, audio or code/text now. You can add a Telegram caption before sending.")
        return
    if text == "ℹ️ Help":
        if require_access(message, uid, message.chat.id):
            cmd_help(message)
        return
    if text == "👑 Admin Panel":
        show_admin_panel(message.chat.id, uid)
        return
    if text == "📱 Share Phone":
        cmd_sharephone(message)
        return
    if text == "Cancel":
        bot.send_message(message.chat.id, "Cancelled.", reply_markup=main_keyboard(uid))
        return
    if text.startswith("/"):
        return

    if not require_access(message, uid, message.chat.id):
        return

    bot.send_chat_action(message.chat.id, "typing")
    store_upload(message)

# ---------------------------------------------------------------------------
# Inline callbacks
# ---------------------------------------------------------------------------
@bot.callback_query_handler(func=lambda call: True)
def handle_callback(call: Any) -> None:
    uid = int(call.from_user.id)
    chat_id = int(call.message.chat.id) if call.message else uid
    data = call.data or ""
    try:
        if is_banned(uid):
            bot.answer_callback_query(call.id, "Access restricted", show_alert=True)
            return

        # Verification button is the one callback allowed while unverified.
        if data == "v:check" or data.startswith("v:check:"):
            pending_token = data.split(":", 2)[2] if data.startswith("v:check:") else None
            verified, missing = check_verification(uid)
            if verified:
                bot.answer_callback_query(call.id, "Verified successfully!")
                bot.send_message(chat_id, "✅ <b>Verification complete.</b> Welcome to your vault!", reply_markup=main_keyboard(uid))
                if pending_token and pending_token in STATE.get("files", {}):
                    deliver_file(chat_id, pending_token)
            else:
                bot.answer_callback_query(call.id, "You have not joined every required channel yet.", show_alert=True)
                send_verification_prompt(chat_id, f"Still missing: {missing}", pending_token=pending_token)
            return

        verified, missing = check_verification(uid)
        if not verified:
            bot.answer_callback_query(call.id, "Join all required channels first.", show_alert=True)
            send_verification_prompt(chat_id, f"Please join: {missing}")
            return

        if data.startswith("my:"):
            bot.answer_callback_query(call.id)
            show_my_files(chat_id, uid, int(data.split(":", 1)[1]))
            return
        if data.startswith("op:"):
            token = data.split(":", 1)[1]
            record = STATE.get("files", {}).get(token)
            if not record or int(record.get("owner_id", 0)) != uid:
                bot.answer_callback_query(call.id, "This is not your file.", show_alert=True)
                return
            bot.answer_callback_query(call.id, "Sending file…")
            deliver_file(chat_id, token)
            return
        if data.startswith("sh:"):
            token = data.split(":", 1)[1]
            record = STATE.get("files", {}).get(token)
            if not record or int(record.get("owner_id", 0)) != uid:
                bot.answer_callback_query(call.id, "This is not your file.", show_alert=True)
                return
            link = start_link(token)
            bot.answer_callback_query(call.id, "Share link ready")
            kb = types.InlineKeyboardMarkup(row_width=2)
            kb.row(
                types.InlineKeyboardButton("📋 Copy Link", copy_text=types.CopyTextButton(text=link)),
                types.InlineKeyboardButton("🔗 Open Link", url=link),
            )
            bot.send_message(chat_id, f"<b>🔗 Your share link</b>\n<code>{escape(link)}</code>", reply_markup=kb, disable_web_page_preview=True)
            return
        if data.startswith("del:"):
            token = data.split(":", 1)[1]
            record = STATE.get("files", {}).get(token)
            if not record or int(record.get("owner_id", 0)) != uid:
                bot.answer_callback_query(call.id, "This is not your file.", show_alert=True)
                return
            try:
                bot.delete_message(STORAGE_CHAT_ID, int(record["storage_message_id"]))
                if record.get("credit_message_id"):
                    bot.delete_message(STORAGE_CHAT_ID, int(record["credit_message_id"]))
            except Exception as exc:
                log.warning("Storage delete warning for %s: %s", token, exc)
            with STATE_LOCK:
                STATE["files"].pop(token, None)
            save_state()
            bot.answer_callback_query(call.id, "File deleted")
            bot.send_message(chat_id, "🗑 <b>Your file has been removed from the vault.</b>")
            return

        # Admin-only panel actions.
        if data.startswith("a:") or data.startswith("ad:"):
            if not is_admin(uid):
                bot.answer_callback_query(call.id, "Admin only", show_alert=True)
                return
            if data == "a:home":
                bot.answer_callback_query(call.id)
                show_admin_panel(chat_id, uid)
            elif data == "a:stats":
                bot.answer_callback_query(call.id)
                admin_stats(chat_id)
            elif data.startswith("a:files:"):
                bot.answer_callback_query(call.id)
                admin_files_page(chat_id, int(data.split(":")[2]))
            elif data == "a:bcall":
                bot.answer_callback_query(call.id)
                begin_broadcast(uid, "all", chat_id)
            elif data == "a:bcadmin":
                bot.answer_callback_query(call.id)
                begin_broadcast(uid, "admins", chat_id)
            elif data == "a:channels":
                bot.answer_callback_query(call.id)
                bot.send_message(chat_id, channel_list_text(), disable_web_page_preview=True)
            elif data.startswith("ad:"):
                token = data.split(":", 1)[1]
                record = STATE.get("files", {}).get(token)
                if not record:
                    bot.answer_callback_query(call.id, "File not found", show_alert=True)
                    return
                bot.answer_callback_query(call.id, "Sending file…")
                deliver_file(chat_id, token)
            return

        bot.answer_callback_query(call.id, "Unknown action")
    except Exception as exc:
        log.exception("Callback processing failed")
        try:
            bot.answer_callback_query(call.id, "Something went wrong. Try again.", show_alert=True)
        except Exception:
            pass

# ---------------------------------------------------------------------------
# Render web service + webhook
# ---------------------------------------------------------------------------
@app.get("/")
def index_page():
    username = BOT_USERNAME or BOT_USERNAME_FALLBACK
    channels = STATE.get("channels", []) if BOOTSTRAPPED else []
    channel_html = "".join(
        f'<a class="channel" href="{html.escape(str(c.get("link", "#")), quote=True)}" target="_blank" rel="noopener">'
        f'<span class="dot"></span><span>{html.escape(str(c.get("title", "Required channel")))}</span><b>JOIN ↗</b></a>'
        for c in channels
    ) or '<div class="empty">No mandatory verification channels are configured.</div>'
    status = "ONLINE" if BOOTSTRAPPED and not BOOTSTRAP_ERROR else "SETUP REQUIRED"
    error_html = f'<p class="error">{html.escape(BOOTSTRAP_ERROR)}</p>' if BOOTSTRAP_ERROR else ""
    page = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>KNIGHT X RAIGEN VAULT</title><meta name="theme-color" content="#090d18">
<style>
:root{{--bg:#080c16;--panel:#101827;--line:rgba(148,163,184,.17);--muted:#9aa8be;--text:#edf4ff;--cyan:#65e8ff;--violet:#8b7cff;}}
*{{box-sizing:border-box}}body{{margin:0;min-height:100vh;background:radial-gradient(ellipse at 15% 0%,rgba(88,87,255,.19),transparent 42%),radial-gradient(ellipse at 90% 85%,rgba(34,211,238,.12),transparent 35%),var(--bg);color:var(--text);font-family:Inter,ui-sans-serif,system-ui,-apple-system,Segoe UI,sans-serif;display:grid;place-items:center;padding:28px;overflow-x:hidden}}
body:before{{content:"";position:fixed;inset:0;background-image:linear-gradient(rgba(255,255,255,.018) 1px,transparent 1px),linear-gradient(90deg,rgba(255,255,255,.018) 1px,transparent 1px);background-size:42px 42px;mask-image:linear-gradient(to bottom,black,transparent);pointer-events:none}}
.wrap{{width:min(760px,100%);position:relative;animation:rise .75s cubic-bezier(.2,.8,.2,1) both}}.card{{border:1px solid var(--line);background:linear-gradient(145deg,rgba(20,31,50,.94),rgba(11,17,30,.94));border-radius:28px;padding:clamp(24px,5vw,46px);box-shadow:0 30px 100px rgba(0,0,0,.42);backdrop-filter:blur(18px);position:relative;overflow:hidden}}
.card:after{{content:"";position:absolute;width:250px;height:250px;right:-100px;top:-110px;border-radius:50%;background:var(--violet);filter:blur(110px);opacity:.23;pointer-events:none}}
.brand{{display:flex;align-items:center;gap:14px;position:relative;z-index:1}}.logo{{width:54px;height:54px;border-radius:17px;display:grid;place-items:center;font-size:25px;background:linear-gradient(135deg,#7367ff,#21c9db);box-shadow:0 10px 35px rgba(85,120,255,.28)}}.eyebrow{{font-size:11px;letter-spacing:.22em;text-transform:uppercase;color:var(--cyan);font-weight:800}}h1{{font-size:clamp(25px,5vw,42px);line-height:1.08;margin:25px 0 12px;letter-spacing:-.04em;position:relative;z-index:1}}.sub{{color:var(--muted);line-height:1.7;max-width:570px;position:relative;z-index:1}}.status{{display:inline-flex;align-items:center;gap:9px;border:1px solid rgba(72,222,170,.2);background:rgba(34,197,94,.08);padding:9px 13px;border-radius:999px;font-size:11px;font-weight:900;letter-spacing:.16em;color:#72efb4;margin-top:25px}}.pulse{{width:7px;height:7px;border-radius:50%;background:#4adea0;box-shadow:0 0 0 0 rgba(74,222,160,.65);animation:pulse 1.7s infinite}}.section{{margin-top:35px;position:relative;z-index:1}}.section h2{{font-size:12px;letter-spacing:.18em;text-transform:uppercase;color:#d7e2f5;margin:0 0 13px}}.channel{{display:flex;align-items:center;gap:12px;text-decoration:none;color:var(--text);padding:14px 15px;border:1px solid var(--line);background:rgba(255,255,255,.025);border-radius:14px;margin-top:9px;transition:.22s ease}}.channel:hover{{transform:translateY(-2px);border-color:rgba(101,232,255,.48);background:rgba(101,232,255,.06)}}.channel .dot{{width:8px;height:8px;border-radius:50%;background:var(--cyan);box-shadow:0 0 16px var(--cyan)}}.channel span:nth-child(2){{flex:1;font-weight:650}}.channel b{{font-size:10px;letter-spacing:.12em;color:var(--cyan)}}.empty{{color:var(--muted);padding:17px;border:1px dashed var(--line);border-radius:14px;font-size:13px}}.footer{{margin-top:30px;padding-top:20px;border-top:1px solid var(--line);display:flex;flex-wrap:wrap;justify-content:space-between;gap:12px;color:var(--muted);font-size:12px;position:relative;z-index:1}}.footer a{{color:var(--cyan);text-decoration:none}}.error{{color:#ff9d9d;font-size:12px;overflow-wrap:anywhere}}@keyframes rise{{from{{opacity:0;transform:translateY(18px)}}to{{opacity:1;transform:translateY(0)}}}}@keyframes pulse{{70%{{box-shadow:0 0 0 8px rgba(74,222,160,0)}}100%{{box-shadow:0 0 0 0 rgba(74,222,160,0)}}}}@media(max-width:520px){{body{{padding:14px}}.card{{border-radius:22px}}.footer{{display:block;line-height:2}}}}
</style></head><body><main class="wrap"><section class="card"><div class="brand"><div class="logo">♞</div><div><div class="eyebrow">Telegram Cloud Storage</div><strong>KNIGHT X RAIGEN VAULT</strong></div></div>
<h1>Your files.<br>Safe in Telegram.</h1><p class="sub">A personal file vault for images, videos, documents, code and more. Save your uploads, manage your private library and share files using simple Telegram links.</p>
<div class="status"><i class="pulse"></i> {status} · @{html.escape(username)}</div>{error_html}
<div class="section"><h2>Required channel verification</h2>{channel_html}</div>
<div class="footer"><span>DEVELOPER CREDITS · <a href="https://t.me/QUANTUM_NULL">@QUANTUM_NULL</a> || <a href="https://t.me/RAIGENBRO">@RAIGENBRO</a></span><span>100 MB max · Telegram cloud storage</span></div>
</section></main></body></html>"""
    return page


@app.get("/health")
def health():
    return jsonify({
        "status": "ok" if BOOTSTRAPPED and not BOOTSTRAP_ERROR else "degraded",
        "bot": "@" + (BOT_USERNAME or BOT_USERNAME_FALLBACK),
        "users": len(STATE.get("users", {})),
        "files": len(STATE.get("files", {})),
        "verification_channels": len(STATE.get("channels", [])),
        "storage": "telegram-private-channel-index",
    }), (200 if BOOTSTRAPPED and not BOOTSTRAP_ERROR else 503)


@app.post("/telegram/<secret>")
def telegram_webhook(secret: str):
    if not secrets.compare_digest(secret, WEBHOOK_SECRET):
        abort(404)
    header_secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if not secrets.compare_digest(header_secret, WEBHOOK_SECRET):
        abort(403)
    if not BOOTSTRAPPED:
        return "not ready", 503
    payload = request.get_json(silent=True)
    if not payload:
        return "bad request", 400
    try:
        update = types.Update.de_json(json.dumps(payload))
        bot.process_new_updates([update])
    except Exception:
        log.exception("Webhook update processing failed")
        return "error", 500
    return "ok", 200


def bootstrap() -> None:
    global BOT_USERNAME, BOT_DISPLAY_NAME, BOOTSTRAPPED, BOOTSTRAP_ERROR
    try:
        me = bot.get_me()
        BOT_USERNAME = (me.username or BOT_USERNAME_FALLBACK).lstrip("@")
        BOT_DISPLAY_NAME = user_full_name(me)
        load_state()
        external_url = os.getenv("WEBHOOK_URL", "").strip() or os.getenv("RENDER_EXTERNAL_URL", "").strip()
        if external_url:
            webhook_url = external_url.rstrip("/") + "/telegram/" + WEBHOOK_SECRET
            bot.set_webhook(
                url=webhook_url,
                secret_token=WEBHOOK_SECRET,
                allowed_updates=["message", "callback_query"],
                drop_pending_updates=False,
            )
            log.info("Webhook configured: %s", external_url)
        else:
            # Local development fallback. Render supplies RENDER_EXTERNAL_URL and uses webhooks.
            bot.remove_webhook()
            thread = threading.Thread(
                target=lambda: bot.infinity_polling(skip_pending=True, timeout=30, long_polling_timeout=30, allowed_updates=["message", "callback_query"]),
                daemon=True,
            )
            thread.start()
            log.info("Local long-polling thread started")
        try:
            bot.set_my_commands([
                types.BotCommand("start", "Open the vault"),
                types.BotCommand("myfiles", "View your personal files"),
                types.BotCommand("help", "How to use the vault"),
                types.BotCommand("admin", "Admin control panel"),
            ])
        except Exception:
            log.warning("Could not register bot commands", exc_info=True)
        BOOTSTRAPPED = True
        BOOTSTRAP_ERROR = None
        log.info("KNIGHT X RAIGEN VAULT is online as @%s", BOT_USERNAME)
    except Exception as exc:
        BOOTSTRAPPED = False
        BOOTSTRAP_ERROR = str(exc)
        log.exception("Vault bootstrap failed")
        # Keep the health page available so the setup error is visible in Render logs/UI.


bootstrap()

if __name__ == "__main__":
    # Local testing only. On Render use gunicorn app:app.
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "10000")))
