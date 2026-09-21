import os
import json
import asyncio
import logging
import shutil
import tempfile
import time
import contextlib
from datetime import datetime, timezone
from pathlib import Path

import img2pdf
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.constants import ChatAction
from telegram.error import NetworkError, TimedOut
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ============================================================
# Configuration
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN")

TELEGRAM_API_URL = os.getenv(
    "TELEGRAM_API_URL",
    "http://localhost:8081",
)

TEMP_DIR = os.getenv("TEMP_DIR", "/tmp/video-compressor")

# ------------------------------------------------------------
# Access control
#
# Three ways a user gets in:
#   1. ADMIN_USER_ID below — always allowed, and the only person who
#      can approve/deny others.
#   2. ALLOWED_USER_IDS — hard-coded in the repo (the original list).
#   3. Approved users — anyone the admin taps "Accept" for. Someone
#      who isn't allowed just messages the bot; the admin gets a
#      request (name, username, ID) with Accept / Deny buttons. Accepted
#      IDs are saved in APPROVED_USERS_FILE (a .json file), and after
#      every change the bot posts the full list as a new .json file in
#      the private BACKUP CHANNEL and PINS it.
#
# Railway wipes local files on every redeploy (no Volume needed here):
# when the bot starts it reads the file pinned in the backup channel and
# loads the approved list from it, so the pinned file IS the backup.
# ------------------------------------------------------------
ADMIN_USER_ID = int(os.getenv("ADMIN_USER_ID", "940770584"))

# The private channel where the approved-users .json is posted + pinned.
# It must be a channel the bot is an ADMIN of, with the "Post messages"
# and "Pin messages" rights. Channel IDs look like -1001234567890.
# Paste it here or set the BACKUP_CHANNEL_ID variable in Railway.
# (To find it: forward any TEXT post from the channel to this bot, from
# the admin account — it replies with the ID.) While it's 0/unset the
# file is sent to the admin's private chat instead and nothing is pinned.
BACKUP_CHANNEL_ID = int(os.getenv("BACKUP_CHANNEL_ID", "0") or 0)

APPROVED_USERS_FILE = Path(
    os.getenv("APPROVED_USERS_FILE", str(Path(__file__).resolve().parent / "approved_users.json"))
)

ALLOWED_USER_IDS: set[int] = {
    940770584,
   5879238618,
   6608494574,
    6651204891,
    1194780815,
    5879238618,
    2112316128,
    7504848343,
    5131223597,
}

# user_id -> {"name": str, "username": str | None, "approved_at": iso str}
approved_users: dict[int, dict] = {}

# Requests waiting on the admin's decision (user_id -> info). Kept in
# memory only: after a restart the user just tries again.
pending_access_requests: dict[int, dict] = {}

# Users the admin denied this session — remembered so they can't spam
# the admin with new requests (the admin can still flip it to Accept
# from the original request message).
denied_users: set[int] = set()

# user_id -> last time we told them "still waiting"/"denied", so a
# burst of messages (e.g. an album of videos) gets one reply, not ten.
_last_access_notice: dict[int, float] = {}
ACCESS_NOTICE_COOLDOWN_SECONDS = 60


def is_allowed(user_id: int | None) -> bool:
    if user_id is None:
        return False

    return (
        user_id == ADMIN_USER_ID
        or user_id in ALLOWED_USER_IDS
        or user_id in approved_users
    )


def is_admin(user_id: int | None) -> bool:
    return user_id is not None and user_id == ADMIN_USER_ID


def user_display_name(user) -> str:
    """'First Last' from a Telegram user object (falls back to 'Unknown')."""
    parts = [getattr(user, "first_name", None), getattr(user, "last_name", None)]
    name = " ".join(part for part in parts if part)
    return name or "Unknown"


def format_username(username: str | None) -> str:
    return f"@{username}" if username else "none"


def approved_users_payload() -> dict:
    return {
        "approved_users": [
            {"id": user_id, **info}
            for user_id, info in sorted(approved_users.items())
        ]
    }


def approved_users_json_bytes() -> bytes:
    return json.dumps(approved_users_payload(), indent=2, ensure_ascii=False).encode("utf-8")


def save_approved_users() -> bool:
    """Write the approved list to disk (atomically). Returns False if it couldn't."""
    try:
        APPROVED_USERS_FILE.parent.mkdir(parents=True, exist_ok=True)
        temp_path = APPROVED_USERS_FILE.with_suffix(".json.tmp")
        temp_path.write_bytes(approved_users_json_bytes())
        os.replace(temp_path, APPROVED_USERS_FILE)
        return True
    except OSError:
        logger.exception("Could not save %s", APPROVED_USERS_FILE)
        return False


def merge_approved_users(raw: bytes) -> int:
    """
    Merge users from JSON bytes into approved_users. Accepts the file
    this bot sends ({"approved_users": [{"id": ..., ...}]}) or a bare
    list of IDs / user objects. Returns how many NEW users were added.
    Raises ValueError if the content isn't usable.
    """
    try:
        data = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"not valid JSON ({error})") from error

    entries = data.get("approved_users") if isinstance(data, dict) else data

    if not isinstance(entries, list):
        raise ValueError('expected {"approved_users": [...]} or a list of IDs')

    added = 0

    for entry in entries:
        if isinstance(entry, dict):
            raw_id, info = entry.get("id"), {k: v for k, v in entry.items() if k != "id"}
        else:
            raw_id, info = entry, {}

        try:
            user_id = int(raw_id)
        except (TypeError, ValueError):
            continue

        if user_id not in approved_users:
            added += 1

        approved_users[user_id] = {
            "name": info.get("name") or approved_users.get(user_id, {}).get("name") or "Unknown",
            "username": info.get("username") or approved_users.get(user_id, {}).get("username"),
            "approved_at": info.get("approved_at")
            or approved_users.get(user_id, {}).get("approved_at")
            or datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }

    return added


def load_approved_users() -> None:
    """Load the saved approved list at startup (missing file = empty list)."""
    if not APPROVED_USERS_FILE.exists():
        logger.info("No %s yet — starting with no approved users.", APPROVED_USERS_FILE)
        return

    try:
        merge_approved_users(APPROVED_USERS_FILE.read_bytes())
        logger.info("Loaded %d approved user(s) from %s", len(approved_users), APPROVED_USERS_FILE)
    except (OSError, ValueError):
        logger.exception("Couldn't read %s — starting with no approved users.", APPROVED_USERS_FILE)


def access_request_text(user_id: int, info: dict, status: str | None = None) -> str:
    lines = [
        "🔔 Access request" if status is None else "🔔 Access request — decided",
        "",
        f"👤 Name: {info.get('name') or 'Unknown'}",
        f"🔗 Username: {format_username(info.get('username'))}",
        f"🆔 ID: {user_id}",
    ]

    if status:
        lines += ["", status]

    return "\n".join(lines)


def access_request_keyboard(user_id: int, state: str = "pending") -> InlineKeyboardMarkup:
    """state: 'pending' (Accept/Deny), 'accepted' (Revoke), 'denied' (Accept instead)."""
    if state == "accepted":
        rows = [[InlineKeyboardButton("🚫 Revoke access", callback_data=f"access:revoke:{user_id}")]]
    elif state == "denied":
        rows = [[InlineKeyboardButton("✅ Accept instead", callback_data=f"access:accept:{user_id}")]]
    elif state == "revoked":
        rows = [[InlineKeyboardButton("✅ Accept again", callback_data=f"access:accept:{user_id}")]]
    else:
        rows = [[
            InlineKeyboardButton("✅ Accept", callback_data=f"access:accept:{user_id}"),
            InlineKeyboardButton("❌ Deny", callback_data=f"access:deny:{user_id}"),
        ]]

    return InlineKeyboardMarkup(rows)


async def reject_unauthorized(message, user_id: int | None) -> None:
    """
    Someone who isn't allowed tried to use the bot. Instead of a dead
    end, forward a request (name, username, ID + Accept/Deny buttons)
    to the admin — once; repeat attempts while it's pending just get a
    short "still waiting" reply.
    """
    if user_id is None:
        return

    now = time.monotonic()

    def should_notify_user() -> bool:
        last = _last_access_notice.get(user_id, 0.0)
        if now - last < ACCESS_NOTICE_COOLDOWN_SECONDS:
            return False
        _last_access_notice[user_id] = now
        return True

    if user_id in denied_users:
        if should_notify_user():
            await message.reply_text("🔴 Your access request was declined.")
        return

    if user_id in pending_access_requests:
        if should_notify_user():
            await message.reply_text(
                "⏳ Your request is still waiting for the admin's approval. "
                "You'll get a message here as soon as they decide."
            )
        return

    user = message.from_user
    info = {
        "name": user_display_name(user),
        "username": getattr(user, "username", None),
    }

    try:
        await message.get_bot().send_message(
            chat_id=ADMIN_USER_ID,
            text=access_request_text(user_id, info),
            reply_markup=access_request_keyboard(user_id),
        )
    except Exception:
        logger.exception("Couldn't send the access request for %s to the admin", user_id)
        if should_notify_user():
            await message.reply_text(
                "⚠️ I couldn't reach the admin to ask for you right now — please try again later."
            )
        return

    pending_access_requests[user_id] = {**info, "requested_at": now}
    _last_access_notice[user_id] = now

    await message.reply_text(
        "⛔ You're not approved to use this bot yet.\n\n"
        "I've sent your request to the admin — you'll get a message here "
        "when they decide."
    )


async def send_approved_users_file(bot, caption: str) -> None:
    """
    Post the current full approved-users list as a .json file: into the
    backup channel (and pin it there) if one is configured, otherwise —
    or if the channel post fails — into the admin's private chat so the
    file is never lost.
    """
    payload = approved_users_json_bytes()

    if not BACKUP_CHANNEL_ID:
        await bot.send_document(
            chat_id=ADMIN_USER_ID,
            document=payload,
            filename="approved_users.json",
            caption=caption,
        )
        return

    try:
        sent = await bot.send_document(
            chat_id=BACKUP_CHANNEL_ID,
            document=payload,
            filename="approved_users.json",
            caption=caption,
        )
    except Exception as error:
        logger.exception("Couldn't post the backup file to channel %s", BACKUP_CHANNEL_ID)
        await bot.send_document(
            chat_id=ADMIN_USER_ID,
            document=payload,
            filename="approved_users.json",
            caption=(
                f"{caption}\n\n⚠️ I couldn't post this to the backup channel "
                f"({BACKUP_CHANNEL_ID}): {error}\n"
                "Make sure the bot is an admin there and the ID is right."
            ),
        )
        return

    await pin_backup_file(bot, sent)


async def pin_backup_file(bot, sent_message) -> None:
    """
    Pin the freshly posted backup file, then unpin the previous backup
    file (if any) so the pinned message is always the newest list. The
    new one is pinned FIRST, so a failure can never leave the channel
    with no pinned backup.
    """
    old_message_id = None

    try:
        chat = await bot.get_chat(BACKUP_CHANNEL_ID)
        old = getattr(chat, "pinned_message", None)
        old_document = getattr(old, "document", None) if old is not None else None

        if (
            old is not None
            and old.message_id != sent_message.message_id
            and old_document is not None
            and (old_document.file_name or "").lower().endswith(".json")
        ):
            old_message_id = old.message_id
    except Exception:
        logger.warning("Couldn't look up the previously pinned backup in %s", BACKUP_CHANNEL_ID)

    try:
        await bot.pin_chat_message(
            chat_id=BACKUP_CHANNEL_ID,
            message_id=sent_message.message_id,
            disable_notification=True,
        )
    except Exception:
        logger.exception("Couldn't pin the backup file in %s", BACKUP_CHANNEL_ID)
        try:
            await bot.send_message(
                chat_id=ADMIN_USER_ID,
                text=(
                    "⚠️ I posted the backup file in the channel but couldn't pin it. "
                    "Give the bot the \"Pin messages\" admin right there — the pinned "
                    "file is what I reload the approved list from after a redeploy."
                ),
            )
        except Exception:
            pass
        return

    if old_message_id is not None:
        try:
            await bot.unpin_chat_message(chat_id=BACKUP_CHANNEL_ID, message_id=old_message_id)
        except Exception:
            logger.warning("Couldn't unpin the previous backup (message %s)", old_message_id)


async def restore_from_pinned_backup(bot) -> None:
    """
    At startup: read the .json pinned in the backup channel and merge its
    users into the approved list. This is what makes the pinned file a
    real backup — Railway wipes the local file on every redeploy.
    """
    if not BACKUP_CHANNEL_ID:
        return

    Path(TEMP_DIR).mkdir(parents=True, exist_ok=True)
    temp_path = Path(TEMP_DIR) / "pinned_backup.json"

    try:
        chat = await bot.get_chat(BACKUP_CHANNEL_ID)
        pinned = getattr(chat, "pinned_message", None)
        document = getattr(pinned, "document", None) if pinned is not None else None

        if document is None:
            logger.info("No pinned backup file in channel %s — nothing to restore.", BACKUP_CHANNEL_ID)
            return

        telegram_file = await with_retries(bot.get_file, document.file_id)
        await with_retries(telegram_file.download_to_drive, custom_path=str(temp_path))

        added = merge_approved_users(temp_path.read_bytes())
        save_approved_users()

        logger.info(
            "Restored from the pinned backup: %d new user(s), %d approved in total.",
            added, len(approved_users),
        )

        if added:
            await bot.send_message(
                chat_id=ADMIN_USER_ID,
                text=(
                    f"♻️ Bot restarted — loaded {added} approved user(s) from the pinned "
                    f"backup in your channel ({len(approved_users)} approved in total)."
                ),
            )
    except Exception:
        logger.exception("Couldn't restore the approved users from the pinned backup.")
    finally:
        temp_path.unlink(missing_ok=True)


async def post_init(application: Application) -> None:
    await restore_from_pinned_backup(application.bot)


async def report_forwarded_channel_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin forwards a text post from a channel -> reply with that channel's ID."""
    message = update.effective_message
    user_id = update.effective_user.id if update.effective_user else None

    if message is None or not is_admin(user_id):
        return

    origin_chat = getattr(getattr(message, "forward_origin", None), "chat", None)

    if origin_chat is None or getattr(origin_chat, "type", None) != "channel":
        return

    await message.reply_text(
        f"📢 That channel's ID is:\n{origin_chat.id}\n\n"
        "Set it as BACKUP_CHANNEL_ID (Railway variable, or in bot.py) and make "
        "sure the bot is an admin of the channel with the \"Post messages\" and "
        "\"Pin messages\" rights."
    )


async def resolve_user_info(bot, user_id: int) -> dict:
    """Best-effort name/username for a user we no longer have a pending entry for (e.g. after a restart)."""
    known = pending_access_requests.get(user_id) or approved_users.get(user_id)
    if known:
        return {"name": known.get("name"), "username": known.get("username")}

    try:
        chat = await bot.get_chat(user_id)
        return {"name": user_display_name(chat), "username": getattr(chat, "username", None)}
    except Exception:
        return {"name": f"User {user_id}", "username": None}


async def handle_access_decision(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin tapped Accept / Deny / Revoke on an access-request message."""
    query = update.callback_query

    if query is None or query.data is None:
        return

    if not is_admin(query.from_user.id if query.from_user else None):
        await query.answer("Only the admin can do this.", show_alert=True)
        return

    try:
        _, decision, user_id_text = query.data.split(":", 2)
        user_id = int(user_id_text)
    except ValueError:
        await query.answer()
        return

    if decision not in ("accept", "deny", "revoke"):
        await query.answer()
        return

    bot = context.bot
    info = await resolve_user_info(bot, user_id)

    async def update_request_message(status: str, state: str) -> None:
        try:
            await query.edit_message_text(
                access_request_text(user_id, info, status),
                reply_markup=access_request_keyboard(user_id, state),
            )
        except Exception:
            logger.exception("Couldn't edit the access request message for %s", user_id)

    async def tell_user(text: str) -> None:
        try:
            await bot.send_message(chat_id=user_id, text=text)
        except Exception:
            # They may have blocked the bot or never opened the chat.
            logger.warning("Couldn't notify user %s about their access decision", user_id)

    if decision == "accept":
        approved_users[user_id] = {
            "name": info.get("name") or "Unknown",
            "username": info.get("username"),
            "approved_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        pending_access_requests.pop(user_id, None)
        denied_users.discard(user_id)
        _last_access_notice.pop(user_id, None)

        save_approved_users()

        await query.answer("Accepted ✅")
        await update_request_message("✅ Accepted", "accepted")
        await tell_user(
            "🟢 Green card granted! The admin approved you — you can use the bot now.\n\n"
            "Send me a video or an audio recording to get started."
        )
        await send_approved_users_file(
            bot,
            f"✅ {info.get('name') or user_id} approved — {len(approved_users)} approved user(s) in total.",
        )
        return

    if decision == "deny":
        pending_access_requests.pop(user_id, None)
        denied_users.add(user_id)

        await query.answer("Denied ❌")
        await update_request_message("❌ Denied", "denied")
        await tell_user("🔴 Your request to use this bot was declined.")
        return

    # revoke
    if user_id in ALLOWED_USER_IDS or user_id == ADMIN_USER_ID:
        await query.answer(
            "That user is hard-coded in bot.py, so it can't be revoked here.",
            show_alert=True,
        )
        return

    approved_users.pop(user_id, None)
    denied_users.add(user_id)

    save_approved_users()

    await query.answer("Access revoked 🚫")
    await update_request_message("🚫 Access revoked", "revoked")
    await tell_user("🔴 Your access to this bot was removed by the admin.")
    await send_approved_users_file(
        bot,
        f"🚫 {info.get('name') or user_id} removed — {len(approved_users)} approved user(s) left.",
    )


async def users_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/users (admin only): send the current approved-users .json on demand."""
    message = update.effective_message
    user_id = update.effective_user.id if update.effective_user else None

    if message is None or not is_admin(user_id):
        return

    await context.bot.send_document(
        chat_id=ADMIN_USER_ID,
        document=approved_users_json_bytes(),
        filename="approved_users.json",
        caption=f"📋 {len(approved_users)} approved user(s) right now. "
        "(To load a list manually, just send a .json file back to me.)",
    )


# How many times to retry a flaky network call (download/upload)
# before giving up, and the base delay between attempts.
RETRY_ATTEMPTS = int(os.getenv("RETRY_ATTEMPTS", "5"))
RETRY_BASE_DELAY = float(os.getenv("RETRY_BASE_DELAY", "3"))

# Length (seconds) of the sample clip encoded at EACH compression level.
# The same clip is used two ways: to extrapolate the full-video size
# estimate, and to send you a real preview of how that level looks
# before you pick one. Longer = more accurate estimate and a better
# preview, but the analyze step takes longer on a small CPU.
SAMPLE_SECONDS = float(os.getenv("SAMPLE_SECONDS", "30"))

# Caps FFmpeg's own thread usage so it doesn't try to use more
# CPU than the service is actually allotted (helps avoid getting
# throttled/killed on tight Railway resource limits).
FFMPEG_THREADS = os.getenv("FFMPEG_THREADS", "2")

# How many level-estimates run at once during the "analyze" step.
# Running all 5 fully in parallel spikes CPU/RAM briefly; this
# caps it so the estimate phase stays within tight resource limits.
ESTIMATE_CONCURRENCY = int(os.getenv("ESTIMATE_CONCURRENCY", "2"))

# How many actual compressions (not estimates) are allowed to run at
# once. Railway's trial/free tiers give you one shared vCPU, so two
# full FFmpeg encodes at the same time don't run in parallel so much
# as fight each other and both go slower. Extra requests queue up
# and run one at a time instead. Raise this only if you've upgraded
# to more CPU.
MAX_CONCURRENT_COMPRESSIONS = int(os.getenv("MAX_CONCURRENT_COMPRESSIONS", "1"))

# ------------------------------------------------------------
# PDF frame-extraction mode
# ------------------------------------------------------------
PDF_INTERVALS = [15, 30, 45, 60]  # seconds
# No cap on frame count — a long video at a short interval can
# produce a very large PDF (and take a while for img2pdf to build),
# but nothing gets silently dropped anymore.

# "Auto-detect changes" mode: sample the video at a fine, fixed
# granularity, then run the same auto-threshold dedup used elsewhere
# to keep only frames that actually changed — no fixed interval to
# pick at all. SMART_CAPTURE_BASE_INTERVAL is the finest we'll ever
# sample at. SMART_CAPTURE_MAX_CANDIDATES caps how many raw candidate
# frames get extracted+compared regardless of video length (each one
# costs an FFmpeg call), by widening the sampling interval for long
# videos instead of scanning e.g. an hour-long video at 2s resolution.
SMART_CAPTURE_BASE_INTERVAL = float(os.getenv("SMART_CAPTURE_BASE_INTERVAL", "5"))
SMART_CAPTURE_MAX_CANDIDATES = int(os.getenv("SMART_CAPTURE_MAX_CANDIDATES", "900"))

# How close two frames' 24x24 grayscale signatures must be to count
# as "extremely similar" for the auto-delete-similar-frames step.
# This is the WORST single-cell difference (0-255 scale), not an
# average — see frames_are_near_identical. A low value means even one
# badly-differing patch of the frame (a subtitle, a small motion,
# anything localized) is enough to call the frames different.
DEDUP_SIMILARITY_THRESHOLD = 10

# ============================================================
# Compression levels
# ============================================================
#
# "medium" mirrors the bot's original fixed behavior and can
# still be tuned via env vars so existing Railway variables
# keep working.
#
# lookahead/refs/bframes control libx264's internal frame
# buffering, which is the main driver of encoder-side memory
# use. Lower numbers = less RAM, at some cost to compression
# efficiency (usually negligible at the CRF values used here).

LEVELS: dict[str, dict] = {
    "low": {
        "label": "🟢 Low",
        "detail": "Best quality, biggest file",
        "crf": "23",
        "preset": "medium",
        "max_width": 1920,
        "audio_bitrate": "128k",
        "lookahead": "20",
        "refs": "3",
        "bframes": "3",
    },
    "medium": {
        "label": "🟡 Medium",
        "detail": "Balanced (default)",
        "crf": os.getenv("CRF", "28"),
        "preset": os.getenv("PRESET", "medium"),
        "max_width": int(os.getenv("MAX_WIDTH", "1280")),
        "audio_bitrate": os.getenv("AUDIO_BITRATE", "96k"),
        "lookahead": "15",
        "refs": "2",
        "bframes": "2",
    },
    "high": {
        "label": "🟠 High",
        "detail": "Smaller file, visible quality loss",
        "crf": "32",
        "preset": "fast",
        "max_width": 854,
        "audio_bitrate": "64k",
        "lookahead": "10",
        "refs": "1",
        "bframes": "2",
    },
    "very_high": {
        "label": "🔴 Very High",
        "detail": "Much smaller, noticeably softer",
        "crf": "36",
        "preset": "veryfast",
        "max_width": 640,
        "audio_bitrate": "48k",
        "lookahead": "8",
        "refs": "1",
        "bframes": "1",
    },
    "extreme": {
        "label": "⚫ Extreme",
        "detail": "Smallest possible, rough quality",
        "crf": "40",
        "preset": "ultrafast",
        "max_width": 426,
        "audio_bitrate": "32k",
        "lookahead": "5",
        "refs": "1",
        "bframes": "0",
    },
}

LEVEL_ORDER = ["low", "medium", "high", "very_high", "extreme"]

DEFAULT_LEVEL = "medium"

# ============================================================
# Audio quality + volume boost (chosen after the video level)
# ============================================================

AUDIO_LEVELS: dict[str, dict] = {
    "low": {"label": "🔈 Low", "detail": "Smallest audio, noticeable quality loss", "bitrate": "48k"},
    "medium": {"label": "🔉 Medium", "detail": "Balanced", "bitrate": "96k"},
    "high": {"label": "🔊 High", "detail": "Best quality, biggest audio", "bitrate": "160k"},
}

AUDIO_LEVEL_ORDER = ["low", "medium", "high"]

DEFAULT_AUDIO_LEVEL = "medium"

# Sentinel used in place of a real LEVELS key when the input is an
# audio file with no video stream to compress — skips straight to
# audio quality + volume boost, same buttons/previews as the video
# flow, just without a video-level step in front of them.
AUDIO_ONLY_LEVEL = "audio_only"

# Sentinel for a VIDEO input where the user only wants the audio
# edited (quality / enhancements / volume boost). The video stream is
# copied as-is (no re-encode), so it's fast and the picture is
# untouched. Same audio buttons/previews as the other flows.
AUDIO_EDIT_LEVEL = "audio_edit"

# Sentinel for a VIDEO input where the user wants its audio track
# swapped for a different recording they send next. Video is copied
# untouched; the new recording becomes the audio. (Only ever passed
# to run_compression together with replacement_audio_path — it never
# appears in callback data, so it's not part of is_flow_level.)
AUDIO_REPLACE_LEVEL = "audio_replace"

# Audio quality used for a replacement recording. The video is copied
# (not re-encoded), so the extra bitrate over "medium" costs very
# little size but keeps the user's recording sounding as good as sent.
REPLACE_AUDIO_LEVEL = "high"


def is_flow_level(level: str) -> bool:
    """True for a real video level or either audio-flow sentinel."""
    return level in LEVELS or level in (AUDIO_ONLY_LEVEL, AUDIO_EDIT_LEVEL)


def flow_header(level: str, audio_level: str) -> str:
    """One-line summary of what's been picked so far, shown above the audio menus."""
    audio_label = AUDIO_LEVELS[audio_level]["label"]

    if level == AUDIO_ONLY_LEVEL:
        return f"🎧 Audio quality: {audio_label}"
    if level == AUDIO_EDIT_LEVEL:
        return f"🎧 Just editing audio (video untouched) · quality: {audio_label}"
    return f"🎚️ {LEVELS[level]['label']} · audio: {audio_label}"

# Audio file types accepted directly (message.audio/.voice) or as a
# document upload — routes into the audio-only flow instead of the
# video flow.
AUDIO_FILE_EXTENSIONS = (".mp3", ".m4a", ".aac", ".wav", ".ogg", ".oga", ".flac", ".opus", ".wma")

# Volume boost is intentionally a fixed set of steps (plus "no
# boost") rather than a free-form percentage — keeps it simple and
# gives each step its own preview clip below.
VOLUME_BOOST_OPTIONS = [0] + list(range(20, 201, 20))  # 0, 20, 40, ..., 200 percent

# Length of the audio-only preview clip sent for each volume level
# before the user picks one.
VOLUME_PREVIEW_SECONDS = int(os.getenv("VOLUME_PREVIEW_SECONDS", "20"))

# ------------------------------------------------------------
# Audio enhancements (optional, toggled before the volume step)
# ------------------------------------------------------------

# FFT-based denoiser — cuts steady background hiss/hum (fan noise,
# room tone, mic self-noise). nf is the assumed noise floor in dB;
# -25 is ffmpeg's own default and works reasonably broadly.
NOISE_REDUCTION_FILTER = os.getenv("NOISE_REDUCTION_FILTER", "afftdn=nf=-25")

# Tuned for spoken lecture/voice recordings: a highpass to cut
# low-end rumble/mic handling noise below where voice lives, a
# presence-band boost around 3kHz for intelligibility, then
# dynaudnorm to even out volume swings from mic distance changing
# (walking around, turning away from the mic, etc).
VOICE_ENHANCEMENT_FILTER = os.getenv(
    "VOICE_ENHANCEMENT_FILTER",
    "highpass=f=100,equalizer=f=3000:width_type=o:width=1.5:g=4,dynaudnorm=f=150:g=15",
)


def build_audio_filter_chain(
    noise_reduction: bool = False,
    voice_enhancement: bool = False,
    volume_boost_percent: int = 0,
) -> str | None:
    """
    Combines the optional audio filters into one -af filtergraph, in
    a sensible order: clean up noise first, then reshape for voice
    clarity, then apply the volume boost last (so gain staging happens
    after cleanup, not before it). Returns None if nothing's enabled.
    """

    parts = []

    if noise_reduction:
        parts.append(NOISE_REDUCTION_FILTER)

    if voice_enhancement:
        parts.append(VOICE_ENHANCEMENT_FILTER)

    if volume_boost_percent:
        parts.append(f"volume={1 + volume_boost_percent / 100:.2f}")

    return ",".join(parts) if parts else None

# Per-chat default level, used only to mark which button is
# starred in the estimate menu.
chat_default_level: dict[int, str] = {}

# Videos that have been downloaded and analyzed, waiting on the
# user to pick a level. Keyed by the original message_id.
# NOTE: in-memory only — resets on redeploy/restart, and capped
# below since each entry holds a real downloaded video file.
pending_compressions: dict[int, dict] = {}
PENDING_MAX = 5

# After a PDF is extracted, we deliberately keep the downloaded
# video around (instead of deleting it right away) so the user can
# still choose to compress it or auto-delete similar frames. Maps
# chat_id -> the pending_compressions message_id being held open.
# It's only torn down when the user sends another video or taps
# "Finish session" — see clear_pending().
open_pdf_sessions: dict[int, int] = {}

# chat_id -> message_id of the video whose audio is about to be replaced.
# While an entry exists, the next audio recording that chat sends is
# treated as the replacement track instead of a new audio file to compress.
awaiting_audio_replacement: dict[int, int] = {}

# Small cache so "redo at a different level" on an already-sent
# result can re-fetch the source without asking you to resend.
recent_files: dict[int, dict] = {}
RECENT_FILES_MAX = 200

# Serializes actual FFmpeg compression jobs (see MAX_CONCURRENT_COMPRESSIONS
# above). active_compressions is just a plain counter used to word the
# "you're queued" message — safe as a bare int since everything here runs
# on the single asyncio event loop, no separate threads involved.
compression_semaphore = asyncio.Semaphore(MAX_CONCURRENT_COMPRESSIONS)
active_compressions = 0

# job_token (a unique object per compression call) -> live progress, for
# jobs currently actually encoding. Used to estimate how long a queued
# job will have to wait.
active_job_progress: dict[object, dict] = {}

# FIFO of jobs waiting for a compression slot: [{"job_id": token, "duration": seconds|None}, ...]
compression_queue: list[dict] = []


def estimate_queue_wait_seconds(job_token: object) -> float:
    """
    Rough estimate of how long job_token will wait for a compression
    slot: the remaining time on whatever's actively encoding right
    now, plus the expected encode time of every job ahead of it in
    the queue. "Expected encode time" for a queued-but-not-yet-started
    job is guessed from the encode rate (seconds of encoding per
    second of source video) observed on whichever active job has made
    enough progress to measure it — falling back to a rough "about
    real-time" assumption if nothing's measurable yet.
    """

    now = time.monotonic()

    encode_rate = 1.0  # fallback: assume roughly 1 second of encoding per second of video

    for info in active_job_progress.values():
        if info["percent"] >= 1 and info["duration"]:
            elapsed = now - info["start_time"]
            processed_seconds = info["duration"] * (info["percent"] / 100)
            if processed_seconds > 0:
                encode_rate = elapsed / processed_seconds
                break

    total_wait = 0.0

    for info in active_job_progress.values():
        elapsed = now - info["start_time"]
        if info["percent"] >= 1:
            total_wait += elapsed * (100 - info["percent"]) / info["percent"]
        elif info["duration"]:
            total_wait += max(0.0, info["duration"] * encode_rate - elapsed)

    for entry in compression_queue:
        if entry["job_id"] is job_token:
            break
        if entry["duration"]:
            total_wait += entry["duration"] * encode_rate

    return total_wait


def get_default_level(chat_id: int) -> str:
    return chat_default_level.get(chat_id, DEFAULT_LEVEL)


def remember_file(message_id: int, file_id: str, original_size: int | None) -> None:
    recent_files[message_id] = {
        "file_id": file_id,
        "original_size": original_size,
    }

    if len(recent_files) > RECENT_FILES_MAX:
        oldest_key = next(iter(recent_files))
        recent_files.pop(oldest_key, None)


def cleanup_work_dir(work_dir: Path) -> None:
    try:
        shutil.rmtree(work_dir, ignore_errors=True)
    except Exception:
        logger.exception("Could not clean temporary files in %s", work_dir)


def clear_pending(message_id: int) -> None:
    """
    Tear down a pending compression: delete its temp/work dir, drop
    it from pending_compressions, and close out any open PDF session
    pointing at it. This is the single place that actually deletes a
    stored video — call it only when a replacement video has arrived
    or the user has explicitly finished/cancelled the session.
    """

    pending = pending_compressions.pop(message_id, None)
    if pending is not None:
        cleanup_work_dir(pending["work_dir"])

    stale_chats = [
        chat_id for chat_id, mid in open_pdf_sessions.items() if mid == message_id
    ]
    for chat_id in stale_chats:
        open_pdf_sessions.pop(chat_id, None)

    waiting_chats = [
        chat_id for chat_id, mid in awaiting_audio_replacement.items() if mid == message_id
    ]
    for chat_id in waiting_chats:
        awaiting_audio_replacement.pop(chat_id, None)


def remember_pending(message_id: int, entry: dict) -> None:
    pending_compressions[message_id] = entry

    if len(pending_compressions) > PENDING_MAX:
        oldest_key = next(iter(pending_compressions))
        if oldest_key != message_id:
            clear_pending(oldest_key)


# ============================================================
# Logging
# ============================================================

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

logger = logging.getLogger(__name__)


# ============================================================
# Validation
# ============================================================

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set.")


# ============================================================
# Helpers
# ============================================================

async def with_retries(func, *args, attempts: int = RETRY_ATTEMPTS, base_delay: float = RETRY_BASE_DELAY, **kwargs):
    """
    Call an async Telegram API function, retrying on flaky-network
    errors (TimedOut / NetworkError) with exponential backoff,
    instead of failing on the first hiccup.
    """

    last_error: Exception | None = None

    for attempt in range(1, attempts + 1):
        try:
            return await func(*args, **kwargs)
        except (NetworkError, TimedOut) as error:
            last_error = error

            if attempt == attempts:
                break

            delay = min(base_delay * (2 ** (attempt - 1)), 30)

            logger.warning(
                "Network error on attempt %s/%s (%s) — retrying in %.0fs",
                attempt, attempts, error, delay,
            )

            await asyncio.sleep(delay)

    assert last_error is not None
    raise last_error


def render_progress_bar(percent: float, width: int = 14) -> str:
    percent = max(0.0, min(100.0, percent))
    filled = round(width * percent / 100)
    bar = "▓" * filled + "░" * (width - filled)
    return f"[{bar}] {percent:.0f}%"


def format_size(size: float | None) -> str:
    if size is None:
        return "unknown"

    units = ["B", "KB", "MB", "GB", "TB"]
    value = float(size)

    for unit in units:
        if value < 1024:
            return f"{value:.1f} {unit}"
        value /= 1024

    return f"{value:.1f} PB"


def format_duration(seconds: float) -> str:
    seconds = max(0, int(round(seconds)))
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)

    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {seconds}s"
    return f"{seconds}s"


def get_video_filter(max_width: int) -> str:
    return f"scale='min({max_width},iw)':-2"


def estimate_audio_bytes(bitrate: str, duration: float | None) -> int | None:
    """
    Audio is encoded at a constant target bitrate (-b:a), so its size
    is just bitrate * duration — no sample encode needed, unlike video.
    """

    if not duration or duration <= 0:
        return None

    try:
        kbps = int(bitrate.rstrip("k"))
    except ValueError:
        return None

    return int(kbps * 1000 * duration / 8)


def estimate_keyboard(message_id: int, estimates: dict[str, int | None], highlight: str | None) -> InlineKeyboardMarkup:
    rows = []

    for level in LEVEL_ORDER:
        info = LEVELS[level]
        size_text = format_size(estimates.get(level))
        text = f"{info['label']} — ~{size_text}"

        if level == highlight:
            text = f"✅ {text}"

        rows.append([
            InlineKeyboardButton(
                text,
                callback_data=f"compress:{level}:{message_id}",
            )
        ])

    rows.append([
        InlineKeyboardButton(
            "🎧 Just edit audio (keep video as is)",
            callback_data=f"audioedit:_:{message_id}",
        )
    ])

    rows.append([
        InlineKeyboardButton(
            "🔁 Replace audio (send a recording)",
            callback_data=f"audioreplace:_:{message_id}",
        )
    ])

    rows.append([
        InlineKeyboardButton("📄 Extract to PDF", callback_data=f"pdfmenu:_:{message_id}")
    ])

    rows.append([
        InlineKeyboardButton("❌ Cancel", callback_data=f"cancel:_:{message_id}")
    ])

    return InlineKeyboardMarkup(rows)


def audio_keyboard(
    message_id: int,
    level: str,
    estimates: dict[str, int | None],
    highlight: str,
) -> InlineKeyboardMarkup:
    rows = []

    for audio_level in AUDIO_LEVEL_ORDER:
        info = AUDIO_LEVELS[audio_level]
        size_text = format_size(estimates.get(audio_level))
        text = f"{info['label']} — ~{size_text} · {info['detail']}"

        if audio_level == highlight:
            text = f"✅ {text}"

        rows.append([
            InlineKeyboardButton(
                text,
                callback_data=f"audioset:{level}|{audio_level}:{message_id}",
            )
        ])

    rows.append([
        InlineKeyboardButton("❌ Cancel", callback_data=f"cancel:_:{message_id}")
    ])

    return InlineKeyboardMarkup(rows)


def volume_keyboard(
    message_id: int,
    level: str,
    audio_level: str,
    noise_reduction: bool = False,
    voice_enhancement: bool = False,
) -> InlineKeyboardMarkup:
    flags = f"{int(noise_reduction)}{int(voice_enhancement)}"
    buttons = [
        InlineKeyboardButton(
            "🔈 No boost" if boost == 0 else f"🔊 +{boost}%",
            callback_data=f"volumeset:{level}|{audio_level}|{boost}|{flags}:{message_id}",
        )
        for boost in VOLUME_BOOST_OPTIONS
    ]

    rows = [buttons[i:i + 3] for i in range(0, len(buttons), 3)]
    rows.append([InlineKeyboardButton("❌ Cancel", callback_data=f"cancel:_:{message_id}")])

    return InlineKeyboardMarkup(rows)


def enhancement_keyboard(
    message_id: int,
    level: str,
    audio_level: str,
    noise_reduction: bool,
    voice_enhancement: bool,
) -> InlineKeyboardMarkup:
    noise_label = f"🔇 Noise reduction: {'ON ✅' if noise_reduction else 'OFF'}"
    voice_label = f"🎙 Voice enhancement: {'ON ✅' if voice_enhancement else 'OFF'}"

    # Each toggle button's callback carries the flag state that
    # results from tapping IT specifically — flip its own bit, leave
    # the other one as-is.
    after_noise_toggle = f"{int(not noise_reduction)}{int(voice_enhancement)}"
    after_voice_toggle = f"{int(noise_reduction)}{int(not voice_enhancement)}"
    current_flags = f"{int(noise_reduction)}{int(voice_enhancement)}"

    return InlineKeyboardMarkup([
        [InlineKeyboardButton(
            noise_label,
            callback_data=f"enhset:{level}|{audio_level}|{after_noise_toggle}:{message_id}",
        )],
        [InlineKeyboardButton(
            voice_label,
            callback_data=f"enhset:{level}|{audio_level}|{after_voice_toggle}:{message_id}",
        )],
        [InlineKeyboardButton(
            "▶️ Continue",
            callback_data=f"enhcontinue:{level}|{audio_level}|{current_flags}:{message_id}",
        )],
        [InlineKeyboardButton("❌ Cancel", callback_data=f"cancel:_:{message_id}")],
    ])


def pdf_interval_keyboard(message_id: int) -> InlineKeyboardMarkup:
    row = [
        InlineKeyboardButton(f"{seconds}s", callback_data=f"pdf:{seconds}:{message_id}")
        for seconds in PDF_INTERVALS
    ]
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🧠 Auto-detect changes", callback_data=f"pdfauto:_:{message_id}")],
        row,
        [InlineKeyboardButton("❌ Cancel", callback_data=f"cancel:_:{message_id}")],
    ])


def post_pdf_keyboard(message_id: int, dedup_available: bool = True) -> InlineKeyboardMarkup:
    rows = []

    if dedup_available:
        rows.append([
            InlineKeyboardButton(
                "🧹 Auto-delete similar frames",
                callback_data=f"pdfdedup:_:{message_id}",
            )
        ])

    rows.append([
        InlineKeyboardButton("🗜 Compress video", callback_data=f"pdfcompress:_:{message_id}")
    ])

    rows.append([
        InlineKeyboardButton(
            "✅ Finish session (deletes video)",
            callback_data=f"pdffinish:_:{message_id}",
        )
    ])

    return InlineKeyboardMarkup(rows)


def redo_keyboard(message_id: int) -> InlineKeyboardMarkup:
    buttons = [
        InlineKeyboardButton(LEVELS[level]["label"], callback_data=f"redo:{level}:{message_id}")
        for level in LEVEL_ORDER
    ]
    return InlineKeyboardMarkup([buttons])


def setlevel_keyboard(highlight: str | None) -> InlineKeyboardMarkup:
    buttons = []
    for level in LEVEL_ORDER:
        text = LEVELS[level]["label"]
        if level == highlight:
            text = f"✅ {text}"
        buttons.append(InlineKeyboardButton(text, callback_data=f"setlevel:{level}:0"))
    return InlineKeyboardMarkup([buttons])


# ============================================================
# FFmpeg / FFprobe
# ============================================================

async def run_command(command: list[str]) -> tuple[int, bytes, bytes]:
    process = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate()
    return process.returncode, stdout, stderr


async def get_duration(input_path: str) -> float | None:
    command = [
        "ffprobe",
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        input_path,
    ]

    returncode, stdout, stderr = await run_command(command)

    if returncode != 0:
        logger.warning("ffprobe failed: %s", stderr.decode(errors="replace"))
        return None

    try:
        return float(stdout.decode().strip())
    except ValueError:
        return None


async def get_video_dimensions(input_path: str) -> tuple[int | None, int | None]:
    command = [
        "ffprobe",
        "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=width,height",
        "-of", "csv=s=x:p=0",
        input_path,
    ]

    returncode, stdout, stderr = await run_command(command)

    if returncode != 0:
        logger.warning("ffprobe (dimensions) failed: %s", stderr.decode(errors="replace"))
        return None, None

    try:
        width_str, height_str = stdout.decode().strip().split("x")
        return int(width_str), int(height_str)
    except (ValueError, AttributeError):
        return None, None


async def probe_audio_stream(input_path: str) -> tuple[bool, int | None]:
    """
    Returns (has_audio, bitrate_in_bits_per_second). The bitrate is
    None when the file has audio but the container doesn't report a
    per-stream bitrate (common with mkv/webm) — callers should treat
    that as "unknown" rather than zero.
    """

    command = [
        "ffprobe",
        "-v", "error",
        "-select_streams", "a:0",
        "-show_entries", "stream=bit_rate",
        "-of", "csv=p=0",
        input_path,
    ]

    returncode, stdout, stderr = await run_command(command)

    if returncode != 0:
        logger.warning("ffprobe (audio) failed: %s", stderr.decode(errors="replace"))
        return False, None

    text = stdout.decode(errors="replace").strip()

    if not text:
        return False, None

    try:
        return True, int(text.splitlines()[0].strip().rstrip(","))
    except ValueError:
        return True, None


async def generate_thumbnail(input_path: str, output_path: str, duration: float | None) -> bool:
    """
    Grab a frame from roughly the middle of the video as a JPEG
    thumbnail. Telegram wants thumbnails no larger than 320px on
    a side and under 200 KB.
    """

    seek = (duration / 2) if duration else 1.0

    command = [
        "ffmpeg", "-y",
        "-ss", f"{seek:.2f}",
        "-i", input_path,
        "-frames:v", "1",
        "-vf", "scale='min(320,iw)':-2",
        "-q:v", "4",
        output_path,
    ]

    returncode, stdout, stderr = await run_command(command)

    if returncode != 0 or not Path(output_path).exists():
        logger.warning("Thumbnail generation failed: %s", stderr.decode(errors="replace"))
        return False

    return True


def build_ffmpeg_command(
    input_path: str,
    output_path: str,
    settings: dict,
    seek: float | None = None,
    duration: float | None = None,
    enable_progress: bool = False,
    audio_bitrate: str | None = None,
    volume_boost_percent: int = 0,
    noise_reduction: bool = False,
    voice_enhancement: bool = False,
) -> list[str]:
    command = ["ffmpeg", "-y"]

    if enable_progress:
        # Machine-readable progress (key=value lines) on stdout,
        # separate from the human-readable logs on stderr.
        command += ["-progress", "pipe:1", "-nostats", "-loglevel", "error"]

    if seek is not None:
        command += ["-ss", f"{seek:.2f}"]

    command += ["-i", input_path]

    if duration is not None:
        command += ["-t", f"{duration:.2f}"]

    x264_params = (
        f"rc-lookahead={settings['lookahead']}:"
        f"ref={settings['refs']}:"
        f"bframes={settings['bframes']}"
    )

    command += [
        "-c:v", "libx264",
        "-preset", settings["preset"],
        "-crf", settings["crf"],
        "-x264-params", x264_params,
        "-threads", FFMPEG_THREADS,
        "-vf", get_video_filter(settings["max_width"]),
        "-c:a", "aac",
        "-b:a", audio_bitrate or settings["audio_bitrate"],
    ]

    audio_filter_chain = build_audio_filter_chain(noise_reduction, voice_enhancement, volume_boost_percent)
    if audio_filter_chain:
        command += ["-af", audio_filter_chain]

    command += [
        "-movflags", "+faststart",
        # Prevents an abrupt "Conversion failed!" at finalization if
        # the muxer's internal packet queue backs up on a tight
        # memory budget.
        "-max_muxing_queue_size", "1024",
        output_path,
    ]

    return command


async def run_ffmpeg_with_progress(
    command: list[str],
    total_duration: float | None = None,
    progress_callback=None,
) -> None:
    """
    Runs an FFmpeg command that was built with `-progress pipe:1`
    enabled, streaming real progress (how much of the video's
    timeline has been processed) to progress_callback as it happens.
    Shared by any FFmpeg step that wants a progress bar — compression,
    frame extraction, etc. Raises RuntimeError if FFmpeg exits non-zero.
    """

    process = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    stderr_chunks: list[bytes] = []

    async def drain_stderr() -> None:
        assert process.stderr is not None
        async for line in process.stderr:
            stderr_chunks.append(line)

    async def drain_progress() -> None:
        # Always drain stdout — ffmpeg's progress pipe must be read
        # continuously or its write buffer can fill and stall ffmpeg,
        # regardless of whether we act on the values.
        assert process.stdout is not None

        async for raw_line in process.stdout:
            if progress_callback is None or not total_duration:
                continue

            line = raw_line.decode(errors="replace").strip()

            if line.startswith("out_time_ms="):
                try:
                    # ffmpeg's "out_time_ms" key is actually in
                    # microseconds despite the name.
                    processed_seconds = int(line.split("=", 1)[1]) / 1_000_000
                except ValueError:
                    continue

                percent = (processed_seconds / total_duration) * 100
                await progress_callback(percent)

            elif line == "progress=end":
                await progress_callback(100.0)

    await asyncio.gather(drain_stderr(), drain_progress())
    returncode = await process.wait()

    if returncode != 0:
        error = b"".join(stderr_chunks).decode(errors="replace")
        logger.error("FFmpeg failed:\n%s", error)
        raise RuntimeError(f"FFmpeg failed with exit code {returncode}")


def make_progress_editor(status_message, label: str):
    """
    Returns an async progress_callback(percent) that edits
    status_message to show `label` plus a progress bar, throttled the
    same way the compression progress bar is (so it doesn't hammer
    Telegram's rate limit).
    """

    state = {"last_percent": -10.0, "last_edit": 0.0}

    async def callback(percent: float) -> None:
        now = time.monotonic()

        if (
            percent - state["last_percent"] < 4
            and now - state["last_edit"] < 3
            and percent < 100
        ):
            return

        state["last_percent"] = percent
        state["last_edit"] = now

        try:
            await status_message.edit_text(f"{label}\n{render_progress_bar(percent)}")
        except Exception:
            # A rate-limit hiccup or "message not modified" here
            # shouldn't abort the underlying work.
            pass

    return callback


async def compress_audio_only(
    input_path: str,
    output_path: str,
    audio_bitrate: str,
    total_duration: float | None = None,
    progress_callback=None,
    volume_boost_percent: int = 0,
    noise_reduction: bool = False,
    voice_enhancement: bool = False,
) -> None:
    """
    Re-encode an audio-only file at the given bitrate (and optional
    volume boost / noise reduction / voice enhancement) — no video
    stream involved. Same progress-streaming setup as compress_video,
    via the shared run_ffmpeg_with_progress.
    """

    command = ["ffmpeg", "-y"]

    if progress_callback is not None and total_duration:
        command += ["-progress", "pipe:1", "-nostats", "-loglevel", "error"]

    command += ["-i", input_path, "-vn", "-c:a", "aac", "-b:a", audio_bitrate]

    audio_filter_chain = build_audio_filter_chain(noise_reduction, voice_enhancement, volume_boost_percent)
    if audio_filter_chain:
        command += ["-af", audio_filter_chain]

    command.append(output_path)

    logger.info("Running FFmpeg (audio-only): %s", " ".join(command))

    await run_ffmpeg_with_progress(command, total_duration, progress_callback)


async def edit_video_audio(
    input_path: str,
    output_path: str,
    audio_bitrate: str,
    total_duration: float | None = None,
    progress_callback=None,
    volume_boost_percent: int = 0,
    noise_reduction: bool = False,
    voice_enhancement: bool = False,
) -> None:
    """
    "Just edit audio" for a video: the video stream is copied
    untouched (-c:v copy, so no quality loss and it finishes in
    seconds), while the audio is re-encoded at the chosen bitrate with
    any enabled noise reduction / voice enhancement / volume boost.
    Only the first video and first audio stream are kept, so stray
    subtitle/data streams can't break the mp4 mux.
    """

    command = ["ffmpeg", "-y"]

    if progress_callback is not None and total_duration:
        command += ["-progress", "pipe:1", "-nostats", "-loglevel", "error"]

    command += [
        "-i", input_path,
        "-map", "0:v:0",
        "-map", "0:a:0",
        "-c:v", "copy",
        "-c:a", "aac",
        "-b:a", audio_bitrate,
    ]

    audio_filter_chain = build_audio_filter_chain(noise_reduction, voice_enhancement, volume_boost_percent)
    if audio_filter_chain:
        command += ["-af", audio_filter_chain]

    command += [
        "-movflags", "+faststart",
        "-max_muxing_queue_size", "1024",
        output_path,
    ]

    logger.info("Running FFmpeg (audio edit, video copied): %s", " ".join(command))

    await run_ffmpeg_with_progress(command, total_duration, progress_callback)


async def replace_video_audio(
    video_path: str,
    audio_path: str,
    output_path: str,
    audio_bitrate: str,
    total_duration: float | None = None,
    progress_callback=None,
) -> None:
    """
    Swap a video's audio track for a different recording. The video
    stream is copied untouched; the recording is encoded to AAC so it
    fits in the mp4. The VIDEO decides the final length:
      - recording longer than the video  -> cut off where the video ends
      - recording shorter than the video -> silence for the remainder
    (`apad` pads the audio with silence forever and `-shortest` then
    stops at the only finite stream, i.e. the video.)
    """

    command = ["ffmpeg", "-y"]

    if progress_callback is not None and total_duration:
        command += ["-progress", "pipe:1", "-nostats", "-loglevel", "error"]

    command += [
        "-i", video_path,
        "-i", audio_path,
        "-map", "0:v:0",
        "-map", "1:a:0",
        "-c:v", "copy",
        "-c:a", "aac",
        "-b:a", audio_bitrate,
        "-af", "apad",
        "-shortest",
        "-movflags", "+faststart",
        "-max_muxing_queue_size", "1024",
        output_path,
    ]

    logger.info("Running FFmpeg (audio replaced, video copied): %s", " ".join(command))

    await run_ffmpeg_with_progress(command, total_duration, progress_callback)


async def compress_video(
    input_path: str,
    output_path: str,
    level: str,
    total_duration: float | None = None,
    progress_callback=None,
    audio_bitrate: str | None = None,
    volume_boost_percent: int = 0,
    noise_reduction: bool = False,
    voice_enhancement: bool = False,
) -> None:
    """
    Compress a video with FFmpeg. If total_duration and a
    progress_callback are supplied, streams real encode progress
    (based on how much of the video's timeline has been processed)
    to the callback as it happens. audio_bitrate overrides the
    level's default audio bitrate if given; volume_boost_percent
    applies a volume filter on top (0 = no change).
    """

    settings = LEVELS[level]
    command = build_ffmpeg_command(
        input_path,
        output_path,
        settings,
        enable_progress=True,
        audio_bitrate=audio_bitrate,
        volume_boost_percent=volume_boost_percent,
        noise_reduction=noise_reduction,
        voice_enhancement=voice_enhancement,
    )

    logger.info("Running FFmpeg (%s): %s", level, " ".join(command))

    await run_ffmpeg_with_progress(command, total_duration, progress_callback)

    logger.info("FFmpeg compression completed.")


async def extract_frames_at_interval(
    input_path: str,
    work_dir: Path,
    interval: float,
    total_duration: float | None = None,
    progress_callback=None,
) -> list[Path]:
    """
    Grab one frame every `interval` seconds. Returns the sorted list
    of extracted frame paths (empty list on failure). No cap on frame
    count — a short interval on a long video will extract a lot of
    frames. If total_duration and progress_callback are given, streams
    extraction progress (how much of the video's timeline has been
    scanned) to the callback.
    """

    frames_dir = work_dir / "frames"
    frames_dir.mkdir(exist_ok=True)

    pattern = str(frames_dir / "frame_%04d.jpg")

    command = ["ffmpeg", "-y"]

    if progress_callback is not None and total_duration:
        command += ["-progress", "pipe:1", "-nostats", "-loglevel", "error"]

    command += [
        "-i", input_path,
        "-vf", f"fps=1/{interval}",
        "-q:v", "3",
        pattern,
    ]

    try:
        await run_ffmpeg_with_progress(command, total_duration, progress_callback)
    except RuntimeError:
        logger.error("PDF frame extraction failed.")
        return []

    return sorted(frames_dir.glob("frame_*.jpg"))


async def extract_pdf_frames(
    input_path: str,
    work_dir: Path,
    interval: float,
) -> tuple[Path | None, int]:
    """
    Fixed-interval capture: extract frames every `interval` seconds
    and combine them all into a single PDF (one frame per page).
    Returns (pdf_path, frame_count) — pdf_path is None if extraction
    failed outright.
    """

    frame_paths = await extract_frames_at_interval(input_path, work_dir, interval)

    if not frame_paths:
        return None, 0

    pdf_path = work_dir / "frames.pdf"

    pdf_bytes = img2pdf.convert([str(path) for path in frame_paths])
    pdf_path.write_bytes(pdf_bytes)

    return pdf_path, len(frame_paths)


async def compute_frame_signature(frame_path: str) -> bytes | None:
    """
    Render a tiny grayscale raw-pixel signature for a frame via
    FFmpeg. Cheap way to compare two frames for near-identity without
    any extra image-processing dependency. Grid is 24x24 (was 12x12) —
    more sample points makes the comparison more sensitive to real
    differences between frames, so visually-different frames are less
    likely to get averaged down into looking "near-identical".
    """

    command = [
        "ffmpeg", "-y",
        "-i", frame_path,
        "-vf", "scale=24:24:flags=area,format=gray",
        "-f", "rawvideo",
        "-frames:v", "1",
        "pipe:1",
    ]

    process = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    stdout, _ = await process.communicate()

    if process.returncode != 0 or not stdout:
        logger.warning("Frame signature failed for %s", frame_path)
        return None

    return stdout


def signature_diff(sig_a: bytes | None, sig_b: bytes | None) -> int | None:
    """Worst (max) single-cell difference between two frame signatures, or None if either is missing/mismatched."""

    if not sig_a or not sig_b or len(sig_a) != len(sig_b):
        return None

    return max(abs(a - b) for a, b in zip(sig_a, sig_b))


def frames_are_near_identical(sig_a: bytes, sig_b: bytes, threshold: float) -> bool:
    """
    Compares the two frames' grids cell-by-cell and looks at the
    WORST (max) cell difference, not the average. Averaging washes
    out localized change — a subtitle or small motion in one corner
    barely moves an average over 576 cells — so a single badly-
    differing region is enough to call the frames different, even if
    the rest of the frame is identical.
    """

    diff = signature_diff(sig_a, sig_b)
    return diff is not None and diff <= threshold


def compute_auto_threshold(diffs: list[int]) -> float:
    """
    Finds the natural split point between "small" (likely duplicate)
    and "large" (likely real content change) frame-to-frame diffs, so
    the dedup threshold adapts to how static or noisy THIS video is
    instead of using one fixed number for every video.

    Simple 1D clustering (an Otsu-style search): tries every possible
    split point between the observed diff values, and keeps the split
    that best separates them into a "low" group and a "high" group
    (maximizes the gap between the two groups' averages, weighted by
    how many diffs fall on each side). That split point becomes the
    threshold. Falls back to DEDUP_SIMILARITY_THRESHOLD if there isn't
    enough data to find a meaningful split.
    """

    if len(diffs) < 4:
        return DEDUP_SIMILARITY_THRESHOLD

    candidates = sorted(set(diffs))

    if len(candidates) < 2:
        return DEDUP_SIMILARITY_THRESHOLD

    best_split = None
    best_score = -1.0

    for i in range(len(candidates) - 1):
        split = (candidates[i] + candidates[i + 1]) / 2
        low = [d for d in diffs if d <= split]
        high = [d for d in diffs if d > split]

        if not low or not high:
            continue

        weight_low = len(low) / len(diffs)
        weight_high = len(high) / len(diffs)
        mean_low = sum(low) / len(low)
        mean_high = sum(high) / len(high)

        # Between-group variance — bigger when the two groups are both
        # sizeable AND far apart, which is what a clean "duplicates vs
        # real changes" split looks like.
        score = weight_low * weight_high * (mean_high - mean_low) ** 2

        if score > best_score:
            best_score = score
            best_split = split

    return best_split if best_split is not None else DEDUP_SIMILARITY_THRESHOLD


async def dedup_frames(
    frame_paths: list[Path],
    threshold: float | None = None,
    progress_callback=None,
) -> tuple[list[Path], list[Path], float]:
    """
    Walk frames in order, dropping any frame that's extremely similar
    to the last frame that was kept. If threshold is None (the normal
    case), one is computed automatically for this specific video via
    compute_auto_threshold. Returns (kept_paths, removed_paths,
    threshold_used) — the threshold is returned so it can be shown to
    the user for transparency/sanity-checking. If progress_callback is
    given, it's called with 0-100 as frame signatures are computed.
    """

    if len(frame_paths) < 2:
        return list(frame_paths), [], threshold if threshold is not None else DEDUP_SIMILARITY_THRESHOLD

    signature_semaphore = asyncio.Semaphore(ESTIMATE_CONCURRENCY)
    completed = 0

    async def bounded_signature(path: Path) -> bytes | None:
        nonlocal completed
        async with signature_semaphore:
            result = await compute_frame_signature(str(path))
        completed += 1
        if progress_callback is not None:
            await progress_callback(completed / len(frame_paths) * 100)
        return result

    signatures = await asyncio.gather(*(bounded_signature(path) for path in frame_paths))

    if threshold is None:
        adjacent_diffs = [
            diff
            for diff in (
                signature_diff(signatures[i], signatures[i + 1])
                for i in range(len(signatures) - 1)
            )
            if diff is not None
        ]
        threshold = compute_auto_threshold(adjacent_diffs)

    kept: list[Path] = []
    removed: list[Path] = []
    last_kept_signature: bytes | None = None

    for path, signature in zip(frame_paths, signatures):
        if (
            signature is not None
            and last_kept_signature is not None
            and frames_are_near_identical(signature, last_kept_signature, threshold)
        ):
            removed.append(path)
            continue

        kept.append(path)
        if signature is not None:
            last_kept_signature = signature

    return kept, removed, threshold


async def generate_volume_preview(
    input_path: str,
    output_path: str,
    seek: float,
    sample_duration: float,
    audio_bitrate: str,
    boost_percent: int,
    noise_reduction: bool = False,
    voice_enhancement: bool = False,
) -> bool:
    """
    Extracts a short audio-only clip with the given volume boost (and
    any enabled noise reduction / voice enhancement) applied, so the
    user can listen before picking a level. Returns True on success.
    """

    command = ["ffmpeg", "-y"]

    if seek:
        command += ["-ss", f"{seek:.2f}"]

    command += ["-i", input_path, "-t", f"{sample_duration:.2f}", "-vn", "-c:a", "aac", "-b:a", audio_bitrate]

    audio_filter_chain = build_audio_filter_chain(noise_reduction, voice_enhancement, boost_percent)
    if audio_filter_chain:
        command += ["-af", audio_filter_chain]

    command.append(output_path)

    returncode, stdout, stderr = await run_command(command)

    if returncode != 0:
        logger.warning("Volume preview failed (+%s%%): %s", boost_percent, stderr.decode(errors="replace"))
        return False

    return True


async def make_level_sample(
    input_path: str,
    duration: float | None,
    level: str,
    work_dir: Path,
) -> tuple[int | None, Path | None]:
    """
    Encode a SAMPLE_SECONDS-long clip (from the middle of the video) at
    this level. Returns (estimated_full_size, sample_path). The size is
    extrapolated from the clip; the clip itself is KEPT on disk so it can
    be sent as a preview — the caller is responsible for deleting it.
    Returns (None, None) if the encode fails (the level can still be
    picked — it just shows "unknown" and has no preview).
    """

    settings = LEVELS[level]

    if not duration or duration <= 0:
        return None, None

    sample_duration = min(SAMPLE_SECONDS, duration)
    seek = max(0.0, (duration - sample_duration) / 2)

    sample_path = work_dir / f"sample_{level}.mp4"

    command = build_ffmpeg_command(
        input_path,
        str(sample_path),
        settings,
        seek=seek,
        duration=sample_duration,
    )

    returncode, stdout, stderr = await run_command(command)

    if returncode != 0 or not sample_path.exists():
        logger.warning(
            "Sample encode failed for %s: %s",
            level,
            stderr.decode(errors="replace"),
        )
        sample_path.unlink(missing_ok=True)
        return None, None

    sample_size = sample_path.stat().st_size

    if sample_duration <= 0:
        sample_path.unlink(missing_ok=True)
        return None, None

    return int((sample_size / sample_duration) * duration), sample_path


async def sample_all_levels(
    input_path: str,
    duration: float | None,
    work_dir: Path,
    on_progress=None,
) -> tuple[dict[str, int | None], dict[str, Path]]:
    """
    Runs make_level_sample for every level. Returns
    (estimates, samples): estimates maps level -> estimated full size
    (or None), samples maps level -> the kept sample clip (only for
    levels that encoded successfully). on_progress(done, total), if
    given, is awaited each time a level finishes.
    """

    # Limit how many sample encodes run at once — 5 fully parallel
    # ffmpeg processes can spike CPU/RAM past tight resource limits.
    semaphore = asyncio.Semaphore(ESTIMATE_CONCURRENCY)
    total = len(LEVEL_ORDER)
    done_count = 0

    async def bounded_sample(level: str) -> tuple[int | None, Path | None]:
        nonlocal done_count

        async with semaphore:
            result = await make_level_sample(input_path, duration, level, work_dir)

        done_count += 1

        if on_progress is not None:
            try:
                await on_progress(done_count, total)
            except Exception:
                # A progress-message hiccup shouldn't abort the encodes.
                pass

        return result

    results = await asyncio.gather(
        *(bounded_sample(level) for level in LEVEL_ORDER),
        return_exceptions=True,
    )

    estimates: dict[str, int | None] = {}
    samples: dict[str, Path] = {}

    for level, result in zip(LEVEL_ORDER, results):
        if isinstance(result, Exception):
            logger.warning("Sample for %s raised: %s", level, result)
            estimates[level] = None
        else:
            estimate, sample_path = result
            estimates[level] = estimate
            if sample_path is not None:
                samples[level] = sample_path

    return estimates, samples


def sample_length_text(duration: float | None) -> str:
    """'30s' normally, or the whole video's length if it's shorter than that."""
    seconds = min(SAMPLE_SECONDS, duration) if duration else SAMPLE_SECONDS
    return f"{seconds:.0f}s"


async def send_level_samples(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    samples: dict[str, Path],
    estimates: dict[str, int | None],
    duration: float | None,
) -> None:
    """
    Sends the sample clip for each compression level (lowest
    compression first) so the user can actually watch the quality
    difference before choosing. Each clip is deleted right after it's
    sent. A failed send is logged and skipped — it never blocks the
    level menu that follows.
    """

    if not samples:
        return

    await context.bot.send_message(
        chat_id=chat_id,
        text=(
            f"🎬 {sample_length_text(duration)} sample at each compression level "
            "(taken from the middle of the video, with that level's default audio):"
        ),
    )

    for level in LEVEL_ORDER:
        sample_path = samples.get(level)

        if sample_path is None:
            continue

        info = LEVELS[level]
        thumb_path = sample_path.with_suffix(".jpg")

        try:
            sample_size = sample_path.stat().st_size
            sample_duration = await get_duration(str(sample_path))
            width, height = await get_video_dimensions(str(sample_path))
            has_thumb = await generate_thumbnail(str(sample_path), str(thumb_path), sample_duration)

            await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.UPLOAD_VIDEO)

            await with_retries(
                context.bot.send_video,
                chat_id=chat_id,
                video=str(sample_path),
                supports_streaming=True,
                width=width,
                height=height,
                duration=round(sample_duration) if sample_duration else None,
                thumbnail=str(thumb_path) if has_thumb else None,
                caption=(
                    f"{info['label']} — {info['detail']}\n"
                    f"Sample: {format_size(sample_size)} · "
                    f"full video ≈ {format_size(estimates.get(level))}"
                ),
            )
        except Exception:
            logger.exception("Couldn't send %s sample clip", level)
        finally:
            sample_path.unlink(missing_ok=True)
            thumb_path.unlink(missing_ok=True)


async def analyze_and_show_level_menu(
    context: ContextTypes.DEFAULT_TYPE,
    status_message,
    chat_id: int,
    message_id: int,
    input_path: Path,
    duration: float | None,
    work_dir: Path,
    original_size: int | None,
    pending: dict | None = None,
) -> dict[str, int | None]:
    """
    Shared by "new video arrived" and "compress after PDF": encodes a
    sample at every level, sends those samples as playable clips, then
    posts the level menu (with size estimates) as a fresh message at the
    bottom of the chat — so the buttons sit right under the clips
    instead of scrolled away above them. Returns the estimates; also
    stores them in `pending["estimates"]` if a pending dict is given.
    """

    sample_label = sample_length_text(duration)

    async def on_progress(done: int, total: int) -> None:
        await status_message.edit_text(
            f"🔎 Encoding a {sample_label} sample at each compression level "
            f"({done}/{total} done)...\n"
            "This can take a minute or two on a small CPU."
        )

    await status_message.edit_text(
        f"🔎 Encoding a {sample_label} sample at each compression level (0/{len(LEVEL_ORDER)} done)...\n"
        "This can take a minute or two on a small CPU."
    )

    estimates, samples = await sample_all_levels(
        str(input_path), duration, work_dir, on_progress=on_progress
    )

    if pending is not None:
        pending["estimates"] = estimates

    try:
        await send_level_samples(context, chat_id, samples, estimates, duration)
    finally:
        # Anything not sent (e.g. an exception mid-way) shouldn't linger.
        for leftover in samples.values():
            leftover.unlink(missing_ok=True)

    default_level = get_default_level(chat_id)

    lines = [f"Original size: {format_size(original_size)}", ""]
    for level in LEVEL_ORDER:
        info = LEVELS[level]
        lines.append(f"{info['label']} — ~{format_size(estimates.get(level))} · {info['detail']}")

    lines.append("")
    lines.append(f"Estimates are based on the {sample_label} samples and may vary ±15%.")
    lines.append("Pick a level to compress:")

    await context.bot.send_message(
        chat_id=chat_id,
        text="\n".join(lines),
        reply_markup=estimate_keyboard(message_id, estimates, default_level),
    )

    # The old "encoding..." status message has done its job.
    with contextlib.suppress(Exception):
        await status_message.delete()

    return estimates


# ============================================================
# Telegram
# ============================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id if update.effective_user else None

    if not is_allowed(user_id):
        await reject_unauthorized(update.message, user_id)
        return

    await update.message.reply_text(
        f"🎥 Send me a video and I'll send you a {SAMPLE_SECONDS:.0f}s sample plus an "
        "estimated size at each compression level before compressing.\n\n"
        "You can also just edit the audio of a video (boost volume, reduce "
        "noise, enhance voice) or replace its audio with a recording you send, "
        "all without touching the picture — or send me an audio recording "
        "directly.\n\n"
        "You can also forward a video to me.\n\n"
        "Use /setlevel to change which level is starred by default."
    )


async def setlevel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message

    if message is None:
        return

    user_id = update.effective_user.id if update.effective_user else None

    if not is_allowed(user_id):
        await reject_unauthorized(message, user_id)
        return

    current = get_default_level(message.chat_id)

    await message.reply_text(
        "Pick the level that should be starred by default in the menu:",
        reply_markup=setlevel_keyboard(current),
    )


async def handle_incoming(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    file_id: str,
    original_size: int | None,
    filename: str,
    media_type: str = "video",
) -> None:
    message = update.effective_message

    if message is None:
        return

    chat_id = message.chat_id

    # The user tapped "Replace audio" and this chat is now waiting for
    # the recording: an audio file / voice message IS that recording.
    waiting_for_message_id = awaiting_audio_replacement.get(chat_id)
    if waiting_for_message_id is not None:
        if media_type == "audio":
            await process_audio_replacement(
                update, context, file_id, waiting_for_message_id
            )
            return

        # A video arrived instead — they've moved on, so forget the
        # pending replacement and handle this as a normal new video.
        awaiting_audio_replacement.pop(chat_id, None)

    # A new video replaces whatever we were holding open for a
    # previous PDF session in this chat — that's the only other
    # trigger (besides "Finish session") that deletes a stored video.
    open_session_message_id = open_pdf_sessions.get(chat_id)
    if open_session_message_id is not None:
        clear_pending(open_session_message_id)

    Path(TEMP_DIR).mkdir(parents=True, exist_ok=True)

    work_dir = Path(
        tempfile.mkdtemp(prefix=f"{media_type}_{message.message_id}_", dir=TEMP_DIR)
    )

    input_path = work_dir / "input"
    is_audio = media_type == "audio"
    kind_label = "audio" if is_audio else "video"

    try:
        size_text = format_size(original_size)

        status_message = await message.reply_text(
            f"📥 Downloading {kind_label} ({size_text})..."
        )

        await context.bot.send_chat_action(
            chat_id=chat_id,
            action=ChatAction.UPLOAD_VOICE if is_audio else ChatAction.UPLOAD_VIDEO,
        )

        telegram_file = await with_retries(context.bot.get_file, file_id)
        await with_retries(telegram_file.download_to_drive, custom_path=str(input_path))

        downloaded_size = input_path.stat().st_size

        duration = await get_duration(str(input_path))

        if is_audio:
            # No video stream to compress — audio-only files skip
            # straight to the audio quality menu (same one the video
            # flow shows after a video level is picked).
            audio_estimates = {
                audio_key: estimate_audio_bytes(AUDIO_LEVELS[audio_key]["bitrate"], duration)
                for audio_key in AUDIO_LEVEL_ORDER
            }

            remember_pending(
                message.message_id,
                {
                    "input_path": input_path,
                    "work_dir": work_dir,
                    "original_size": downloaded_size,
                    "filename": filename,
                    "chat_id": chat_id,
                    "duration": duration,
                    "media_type": "audio",
                },
            )

            await status_message.edit_text(
                f"🎧 Original size: {format_size(downloaded_size)}\nPick audio quality:",
                reply_markup=audio_keyboard(
                    message.message_id, AUDIO_ONLY_LEVEL, audio_estimates, DEFAULT_AUDIO_LEVEL
                ),
            )
            return

        # Registered BEFORE the samples are encoded/sent so the menu
        # buttons that appear afterwards always find their pending entry.
        pending_entry = {
            "input_path": input_path,
            "work_dir": work_dir,
            "original_size": downloaded_size,
            "filename": filename,
            "chat_id": chat_id,
            "duration": duration,
            "estimates": {},
            "media_type": "video",
        }
        remember_pending(message.message_id, pending_entry)

        await analyze_and_show_level_menu(
            context,
            status_message=status_message,
            chat_id=chat_id,
            message_id=message.message_id,
            input_path=input_path,
            duration=duration,
            work_dir=work_dir,
            original_size=downloaded_size,
            pending=pending_entry,
        )

    except Exception as error:
        logger.exception("Error analyzing %s:", "audio" if is_audio else "video")
        cleanup_work_dir(work_dir)
        pending_compressions.pop(message.message_id, None)
        await message.reply_text(
            f"❌ Something went wrong while analyzing the {kind_label}.\n\nError: {error}"
        )


async def run_compression(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    input_path: Path,
    work_dir: Path,
    original_size: int | None,
    level: str | None,
    chat_id: int,
    source_message_id: int,
    status_message=None,
    duration: float | None = None,
    audio_level: str = DEFAULT_AUDIO_LEVEL,
    volume_boost_percent: int = 0,
    noise_reduction: bool = False,
    voice_enhancement: bool = False,
    replacement_audio_path: Path | None = None,
    extra_caption: str = "",
) -> None:
    """
    level=None means "audio-only" — no video stream to compress, just
    re-encode the audio at the chosen quality/boost and send it back
    as an audio file instead of a video.

    level=AUDIO_EDIT_LEVEL means "just edit the audio of a video" — the
    video stream is copied untouched, only the audio is re-encoded, and
    the result is sent back as a video.

    level=AUDIO_REPLACE_LEVEL means "swap the video's audio for the
    recording at replacement_audio_path" — video copied untouched.
    extra_caption (optional) is appended to the final video caption.
    """

    global active_compressions

    is_audio_only = level is None
    is_video_audio_edit = level == AUDIO_EDIT_LEVEL
    is_video_audio_replace = level == AUDIO_REPLACE_LEVEL
    level_info = (
        None if (is_audio_only or is_video_audio_edit or is_video_audio_replace) else LEVELS[level]
    )
    audio_info = AUDIO_LEVELS[audio_level]
    audio_bitrate = audio_info["bitrate"]
    output_path = work_dir / ("compressed.m4a" if is_audio_only else "compressed.mp4")

    job_token = object()
    queue_refresh_task = None

    try:
        # Queue: only MAX_CONCURRENT_COMPRESSIONS compressions actually
        # encode at once. If others are already running, say so up
        # front — with an estimated wait — instead of leaving the
        # user staring at 0%.
        if active_compressions >= MAX_CONCURRENT_COMPRESSIONS:
            compression_queue.append({"job_id": job_token, "duration": duration})

            def queue_text() -> str:
                position = next(
                    (i for i, entry in enumerate(compression_queue) if entry["job_id"] is job_token),
                    len(compression_queue) - 1,
                )
                wait_seconds = estimate_queue_wait_seconds(job_token)
                return (
                    f"🕓 {active_compressions} compression(s) in progress, "
                    f"{position} ahead of you in queue — "
                    f"~{format_duration(wait_seconds)} estimated wait..."
                )

            if status_message is not None:
                await with_retries(status_message.edit_text, queue_text())
            else:
                status_message = await with_retries(
                    context.bot.send_message, chat_id=chat_id, text=queue_text()
                )

            async def refresh_queue_message() -> None:
                # Re-estimate and re-post the wait periodically while
                # this job sits in the queue, so the estimate reflects
                # real progress on whatever's currently encoding.
                while True:
                    await asyncio.sleep(8)
                    try:
                        await status_message.edit_text(queue_text())
                    except Exception:
                        pass

            queue_refresh_task = asyncio.create_task(refresh_queue_message())

        async with compression_semaphore:
            if queue_refresh_task is not None:
                queue_refresh_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await queue_refresh_task

            compression_queue[:] = [entry for entry in compression_queue if entry["job_id"] is not job_token]

            active_compressions += 1
            active_job_progress[job_token] = {
                "start_time": time.monotonic(),
                "duration": duration,
                "percent": 0.0,
            }
            try:
                boost_suffix = f" · 🔊+{volume_boost_percent}%" if volume_boost_percent else ""
                enhancement_bits = []
                if noise_reduction:
                    enhancement_bits.append("🔇 noise reduction")
                if voice_enhancement:
                    enhancement_bits.append("🎙 voice enhancement")
                enhancement_suffix = f" · {' + '.join(enhancement_bits)}" if enhancement_bits else ""

                if is_audio_only:
                    compressing_label = (
                        f"⚙️ Compressing audio ({audio_info['label']}{boost_suffix}{enhancement_suffix})..."
                    )
                elif is_video_audio_edit:
                    compressing_label = (
                        f"⚙️ Editing audio, video untouched "
                        f"({audio_info['label']}{boost_suffix}{enhancement_suffix})..."
                    )
                elif is_video_audio_replace:
                    compressing_label = (
                        f"⚙️ Replacing the audio, video untouched ({audio_info['label']})..."
                    )
                else:
                    compressing_label = (
                        f"⚙️ Compressing at {level_info['label']} "
                        f"(audio: {audio_info['label']}{boost_suffix}{enhancement_suffix})..."
                    )
                text = f"{compressing_label}\n{render_progress_bar(0)}"

                if status_message is not None:
                    await with_retries(status_message.edit_text, text)
                else:
                    status_message = await with_retries(context.bot.send_message, chat_id=chat_id, text=text)

                await context.bot.send_chat_action(
                    chat_id=chat_id,
                    action=ChatAction.UPLOAD_VOICE if is_audio_only else ChatAction.UPLOAD_VIDEO,
                )

                progress_state = {
                    "last_percent": -10.0,
                    "last_edit": 0.0,
                    "start_time": time.monotonic(),
                }

                async def on_progress(percent: float) -> None:
                    active_job_progress[job_token]["percent"] = percent

                    now = time.monotonic()

                    # Throttle edits so we don't hammer Telegram's rate limit —
                    # only push an update on a meaningful jump or after a
                    # few seconds, whichever comes first.
                    if (
                        percent - progress_state["last_percent"] < 4
                        and now - progress_state["last_edit"] < 3
                        and percent < 100
                    ):
                        return

                    progress_state["last_percent"] = percent
                    progress_state["last_edit"] = now

                    elapsed = now - progress_state["start_time"]
                    eta_text = ""
                    # Need a little progress before the elapsed/percent
                    # ratio means anything — otherwise early jitter
                    # produces a wildly wrong estimate.
                    if 1 <= percent < 100 and elapsed > 2:
                        remaining_seconds = elapsed * (100 - percent) / percent
                        eta_text = f" · ~{format_duration(remaining_seconds)} left"

                    bar_text = (
                        f"{compressing_label}\n"
                        f"{render_progress_bar(percent)}{eta_text}"
                    )

                    try:
                        await status_message.edit_text(bar_text)
                    except Exception:
                        # A rate-limit hiccup or "message not modified" here
                        # shouldn't abort the actual compression.
                        pass

                if is_audio_only:
                    await compress_audio_only(
                        str(input_path),
                        str(output_path),
                        audio_bitrate,
                        total_duration=duration,
                        progress_callback=on_progress if duration else None,
                        volume_boost_percent=volume_boost_percent,
                        noise_reduction=noise_reduction,
                        voice_enhancement=voice_enhancement,
                    )
                elif is_video_audio_replace:
                    await replace_video_audio(
                        str(input_path),
                        str(replacement_audio_path),
                        str(output_path),
                        audio_bitrate,
                        total_duration=duration,
                        progress_callback=on_progress if duration else None,
                    )
                elif is_video_audio_edit:
                    await edit_video_audio(
                        str(input_path),
                        str(output_path),
                        audio_bitrate,
                        total_duration=duration,
                        progress_callback=on_progress if duration else None,
                        volume_boost_percent=volume_boost_percent,
                        noise_reduction=noise_reduction,
                        voice_enhancement=voice_enhancement,
                    )
                else:
                    await compress_video(
                        str(input_path),
                        str(output_path),
                        level,
                        total_duration=duration,
                        progress_callback=on_progress if duration else None,
                        audio_bitrate=audio_bitrate,
                        volume_boost_percent=volume_boost_percent,
                        noise_reduction=noise_reduction,
                        voice_enhancement=voice_enhancement,
                    )
            finally:
                active_compressions -= 1
                active_job_progress.pop(job_token, None)

        compressed_size = output_path.stat().st_size

        if original_size:
            saved = original_size - compressed_size
            percentage = (saved / original_size) * 100
        else:
            percentage = 0

        if is_audio_only:
            output_duration = await get_duration(str(output_path))

            await status_message.edit_text("📤 Sending compressed audio...")
            await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.UPLOAD_VOICE)

            await with_retries(
                context.bot.send_audio,
                chat_id=chat_id,
                audio=str(output_path),
                duration=round(output_duration) if output_duration else None,
                caption=(
                    f"✅ Compression complete! (audio: {audio_info['label']}{boost_suffix}{enhancement_suffix})\n\n"
                    f"Original: {format_size(original_size)}\n"
                    f"Compressed: {format_size(compressed_size)}\n"
                    f"Saved: {percentage:.1f}%"
                ),
            )
            return

        # Probe the actual compressed file for duration/dimensions
        # (rather than reusing the source video's numbers) and grab
        # a thumbnail frame — without these, Telegram clients show
        # a blank 00:00 preview even though the video plays fine.
        output_duration = await get_duration(str(output_path))
        output_width, output_height = await get_video_dimensions(str(output_path))

        thumbnail_path = work_dir / "thumb.jpg"
        has_thumbnail = await generate_thumbnail(str(output_path), str(thumbnail_path), output_duration)

        await status_message.edit_text(
            "📤 Sending edited video..."
            if (is_video_audio_edit or is_video_audio_replace)
            else "📤 Sending compressed video..."
        )

        await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.UPLOAD_VIDEO)

        if is_video_audio_edit:
            result_heading = f"✅ Audio edited — video untouched! (audio: {audio_info['label']}{boost_suffix}{enhancement_suffix})"
        elif is_video_audio_replace:
            result_heading = f"✅ Audio replaced — video untouched! (audio: {audio_info['label']})"
        else:
            result_heading = (
                f"✅ Compression complete! ({level_info['label']}, "
                f"audio: {audio_info['label']}{boost_suffix}{enhancement_suffix})"
            )

        await with_retries(
            context.bot.send_video,
            chat_id=chat_id,
            video=str(output_path),
            supports_streaming=True,
            width=output_width,
            height=output_height,
            duration=round(output_duration) if output_duration else None,
            thumbnail=str(thumbnail_path) if has_thumbnail else None,
            caption=(
                f"{result_heading}\n\n"
                f"Original: {format_size(original_size)}\n"
                f"Compressed: {format_size(compressed_size)}\n"
                f"Saved: {percentage:.1f}%"
                + (f"\n\n{extra_caption}" if extra_caption else "")
            ),
            reply_markup=redo_keyboard(source_message_id),
        )

    except Exception as error:
        logger.exception("Error compressing %s:", "audio" if is_audio_only else "video")
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"❌ Something went wrong while compressing.\n\nError: {error}",
        )

    finally:
        if queue_refresh_task is not None and not queue_refresh_task.done():
            queue_refresh_task.cancel()
        compression_queue[:] = [entry for entry in compression_queue if entry["job_id"] is not job_token]
        active_job_progress.pop(job_token, None)

        if source_message_id in pending_compressions:
            clear_pending(source_message_id)
        else:
            # "redo" builds a fresh work_dir that was never registered
            # in pending_compressions, so just clean it up directly.
            cleanup_work_dir(work_dir)


async def process_audio_replacement(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    file_id: str,
    target_message_id: int,
) -> None:
    """
    The recording for a "Replace audio" request has arrived: download
    it, sanity-check it has audio, and run the swap through the normal
    compression pipeline (queue, progress bar, sending, cleanup).
    """

    message = update.effective_message
    chat_id = message.chat_id

    pending = pending_compressions.get(target_message_id)

    if pending is None:
        awaiting_audio_replacement.pop(chat_id, None)
        await message.reply_text(
            "⚠️ That video isn't in my cache anymore — please resend it, "
            "then tap Replace audio again."
        )
        return

    work_dir = pending["work_dir"]
    audio_path = work_dir / f"replacement_{message.message_id}"

    status_message = await message.reply_text("📥 Downloading the new audio...")

    try:
        await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.UPLOAD_VOICE)

        telegram_file = await with_retries(context.bot.get_file, file_id)
        await with_retries(telegram_file.download_to_drive, custom_path=str(audio_path))

        has_audio, _ = await probe_audio_stream(str(audio_path))

        if not has_audio:
            audio_path.unlink(missing_ok=True)
            # Still waiting — they can just send a different file.
            await status_message.edit_text(
                "❌ I couldn't find any audio in that file. "
                "Send a different recording (or tap Cancel above)."
            )
            return

        replacement_duration = await get_duration(str(audio_path))
    except Exception as error:
        logger.exception("Error downloading replacement audio:")
        audio_path.unlink(missing_ok=True)
        await status_message.edit_text(
            f"❌ Couldn't download that audio — send it again.\n\nError: {error}"
        )
        return

    # Got a usable recording — no longer waiting.
    awaiting_audio_replacement.pop(chat_id, None)

    video_duration = pending.get("duration")
    extra_caption = ""

    if replacement_duration and video_duration:
        difference = replacement_duration - video_duration

        if difference > 1:
            extra_caption = (
                f"ℹ️ Your recording ({format_duration(replacement_duration)}) is longer than "
                f"the video ({format_duration(video_duration)}), so its last "
                f"{format_duration(difference)} was cut."
            )
        elif difference < -1:
            extra_caption = (
                f"ℹ️ Your recording ({format_duration(replacement_duration)}) is shorter than "
                f"the video ({format_duration(video_duration)}), so the last "
                f"{format_duration(-difference)} is silent."
            )

    await run_compression(
        update,
        context,
        pending["input_path"],
        work_dir,
        pending["original_size"],
        AUDIO_REPLACE_LEVEL,
        chat_id,
        target_message_id,
        status_message=status_message,
        duration=video_duration,
        audio_level=REPLACE_AUDIO_LEVEL,
        replacement_audio_path=audio_path,
        extra_caption=extra_caption,
    )


# ============================================================
# Video / document handlers
# ============================================================

async def handle_video(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message

    if message is None or message.video is None:
        return

    user_id = update.effective_user.id if update.effective_user else None

    if not is_allowed(user_id):
        await reject_unauthorized(message, user_id)
        return

    video = message.video
    remember_file(message.message_id, video.file_id, video.file_size)

    await handle_incoming(
        update, context,
        file_id=video.file_id,
        original_size=video.file_size,
        filename=f"video_{message.message_id}.mp4",
    )


async def handle_audio(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message

    if message is None or (message.audio is None and message.voice is None):
        return

    user_id = update.effective_user.id if update.effective_user else None

    if not is_allowed(user_id):
        await reject_unauthorized(message, user_id)
        return

    audio = message.audio or message.voice
    filename = getattr(audio, "file_name", None) or f"audio_{message.message_id}.m4a"
    remember_file(message.message_id, audio.file_id, audio.file_size)

    await handle_incoming(
        update, context,
        file_id=audio.file_id,
        original_size=audio.file_size,
        filename=filename,
        media_type="audio",
    )


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message

    if message is None or message.document is None:
        return

    user_id = update.effective_user.id if update.effective_user else None

    if not is_allowed(user_id):
        await reject_unauthorized(message, user_id)
        return

    document = message.document
    mime_type = document.mime_type or ""
    filename = document.file_name or "video"

    # The admin sending back an approved_users .json restores/merges
    # the approved list (e.g. after Railway wiped the local file).
    if is_admin(user_id) and filename.lower().endswith(".json"):
        await restore_approved_users_from_document(message, context, document)
        return

    is_video = (
        mime_type.startswith("video/")
        or filename.lower().endswith(
            (".mp4", ".mkv", ".mov", ".avi", ".webm", ".m4v", ".mpeg", ".mpg", ".3gp")
        )
    )
    is_audio = (
        mime_type.startswith("audio/")
        or filename.lower().endswith(AUDIO_FILE_EXTENSIONS)
    )

    if not is_video and not is_audio:
        await message.reply_text("❌ That doesn't look like a video or audio file.")
        return

    remember_file(message.message_id, document.file_id, document.file_size)

    await handle_incoming(
        update, context,
        file_id=document.file_id,
        original_size=document.file_size,
        filename=filename,
        media_type="audio" if is_audio else "video",
    )


async def restore_approved_users_from_document(message, context, document) -> None:
    """Admin sent a .json: merge its users into the approved list and save."""
    Path(TEMP_DIR).mkdir(parents=True, exist_ok=True)
    temp_path = Path(TEMP_DIR) / f"restore_{message.message_id}.json"

    try:
        telegram_file = await with_retries(context.bot.get_file, document.file_id)
        await with_retries(telegram_file.download_to_drive, custom_path=str(temp_path))

        added = merge_approved_users(temp_path.read_bytes())
        saved = save_approved_users()

        await message.reply_text(
            f"✅ Loaded that file — {added} new user(s) added, "
            f"{len(approved_users)} approved in total."
            + ("" if saved else "\n⚠️ Couldn't save to disk on the server.")
        )
    except ValueError as error:
        await message.reply_text(f"❌ I couldn't use that JSON file: {error}")
    except Exception as error:
        logger.exception("Error restoring approved users:")
        await message.reply_text(f"❌ Something went wrong reading that file.\n\nError: {error}")
    finally:
        temp_path.unlink(missing_ok=True)


# ============================================================
# Button handler (setlevel / compress / redo / cancel)
# ============================================================

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query

    if query is None or query.data is None:
        return

    user_id = query.from_user.id if query.from_user else None

    if not is_allowed(user_id):
        await query.answer(
            f"⛔ Not authorized. Your Telegram user ID is {user_id}.",
            show_alert=True,
        )
        return

    await query.answer()

    try:
        action, level, message_id_str = query.data.split(":", 2)
        message_id = int(message_id_str)
    except ValueError:
        return

    chat_id = query.message.chat_id if query.message else None

    if action == "setlevel":
        if level not in LEVELS or chat_id is None:
            return
        chat_default_level[chat_id] = level
        await query.edit_message_text(
            f"Default star set to {LEVELS[level]['label']}.\n"
            "New estimate menus will highlight this level."
        )
        return

    if action == "cancel":
        clear_pending(message_id)
        await query.edit_message_text("❌ Cancelled — temporary files removed.")
        return

    if action == "pdfmenu":
        pending = pending_compressions.get(message_id)

        if pending is None:
            await query.message.reply_text(
                "⚠️ That video isn't in my cache anymore — please resend it."
            )
            return

        await query.edit_message_text(
            "Pick a screenshot interval — one frame will be captured "
            "every N seconds and combined into a PDF:",
            reply_markup=pdf_interval_keyboard(message_id),
        )
        return

    if action == "pdf":
        try:
            interval = int(level)
        except ValueError:
            return

        pending = pending_compressions.get(message_id)

        if pending is None:
            await query.message.reply_text(
                "⚠️ That video isn't in my cache anymore — please resend it."
            )
            return

        chat_id = pending["chat_id"]
        input_path = pending["input_path"]
        work_dir = pending["work_dir"]

        await query.edit_message_text(
            f"📄 Extracting a frame every {interval}s and building a PDF..."
        )

        try:
            pdf_path, frame_count = await extract_pdf_frames(
                str(input_path), work_dir, interval
            )

            if pdf_path is None:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text="❌ Couldn't extract any frames from that video.",
                )
                clear_pending(message_id)
                return

            caption = f"📄 {frame_count} frame(s), one every {interval}s."

            await with_retries(
                context.bot.send_document,
                chat_id=chat_id,
                document=str(pdf_path),
                filename="frames.pdf",
                caption=caption,
            )

            # Keep the downloaded video (and its work_dir) around —
            # the user can still auto-delete similar frames or jump
            # into compression. It's only deleted if another video
            # arrives or they tap "Finish session".
            pending["interval"] = interval
            pending["frames_dir"] = str(work_dir / "frames")
            open_pdf_sessions[chat_id] = message_id

            await context.bot.send_message(
                chat_id=chat_id,
                text="What would you like to do next?",
                reply_markup=post_pdf_keyboard(message_id, dedup_available=frame_count > 1),
            )

        except Exception as error:
            logger.exception("Error extracting PDF:")
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"❌ Something went wrong extracting frames.\n\nError: {error}",
            )
            clear_pending(message_id)

        return

    if action == "pdfauto":
        pending = pending_compressions.get(message_id)

        if pending is None:
            await query.message.reply_text(
                "⚠️ That video isn't in my cache anymore — please resend it."
            )
            return

        chat_id = pending["chat_id"]
        input_path = pending["input_path"]
        work_dir = pending["work_dir"]
        duration = pending.get("duration")
        status_message = query.message

        # Widen the sampling interval for long videos so we never
        # extract+compare more than SMART_CAPTURE_MAX_CANDIDATES raw
        # frames, regardless of how long the video is.
        interval = SMART_CAPTURE_BASE_INTERVAL
        if duration:
            interval = max(SMART_CAPTURE_BASE_INTERVAL, duration / SMART_CAPTURE_MAX_CANDIDATES)

        scan_label = f"📸 Scanning the video for changes (sampling every {interval:.1f}s)..."
        await status_message.edit_text(f"{scan_label}\n{render_progress_bar(0)}")

        try:
            candidate_paths = await extract_frames_at_interval(
                str(input_path),
                work_dir,
                interval,
                total_duration=duration,
                progress_callback=make_progress_editor(status_message, scan_label),
            )

            if not candidate_paths:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text="❌ Couldn't extract any frames from that video.",
                )
                clear_pending(message_id)
                return

            compare_label = f"🔍 Sampled {len(candidate_paths)} frame(s) — comparing for real changes..."
            await status_message.edit_text(f"{compare_label}\n{render_progress_bar(0)}")

            kept_paths, removed_paths, threshold_used = await dedup_frames(
                candidate_paths,
                progress_callback=make_progress_editor(status_message, compare_label),
            )

            for path in removed_paths:
                path.unlink(missing_ok=True)

            pdf_path = work_dir / "frames.pdf"
            pdf_bytes = img2pdf.convert([str(path) for path in kept_paths])
            pdf_path.write_bytes(pdf_bytes)

            caption = (
                f"📄 {len(kept_paths)} frame(s) kept out of {len(candidate_paths)} sampled "
                f"(every {interval:.1f}s).\n"
                f"(auto threshold: {threshold_used:.0f})"
            )

            await with_retries(
                context.bot.send_document,
                chat_id=chat_id,
                document=str(pdf_path),
                filename="frames.pdf",
                caption=caption,
            )

            # Keep the downloaded video (and its work_dir) around —
            # the user can still auto-delete similar frames or jump
            # into compression. It's only deleted if another video
            # arrives or they tap "Finish session".
            pending["interval"] = interval
            pending["frames_dir"] = str(work_dir / "frames")
            open_pdf_sessions[chat_id] = message_id

            await context.bot.send_message(
                chat_id=chat_id,
                text="What would you like to do next?",
                reply_markup=post_pdf_keyboard(message_id, dedup_available=len(kept_paths) > 1),
            )

        except Exception as error:
            logger.exception("Error during smart PDF capture:")
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"❌ Something went wrong scanning the video.\n\nError: {error}",
            )
            clear_pending(message_id)

        return

    if action == "pdfdedup":
        pending = pending_compressions.get(message_id)

        if pending is None:
            await query.message.reply_text(
                "⚠️ That video isn't in my cache anymore — please resend it."
            )
            return

        frames_dir = pending.get("frames_dir")

        if not frames_dir:
            await query.message.reply_text(
                "⚠️ No extracted frames to compare yet — extract a PDF first."
            )
            return

        frame_paths = sorted(Path(frames_dir).glob("frame_*.jpg"))

        if len(frame_paths) < 2:
            await query.answer("Nothing to compare — not enough frames.", show_alert=True)
            return

        chat_id = pending["chat_id"]

        await query.edit_message_text("🔍 Comparing frames for near-duplicates...")

        try:
            kept_paths, removed_paths, threshold_used = await dedup_frames(frame_paths)

            if not removed_paths:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=(
                        "✅ No extremely-similar frames found — nothing removed.\n"
                        f"(auto threshold: {threshold_used:.0f})"
                    ),
                    reply_markup=post_pdf_keyboard(message_id, dedup_available=False),
                )
                return

            for path in removed_paths:
                path.unlink(missing_ok=True)

            work_dir = pending["work_dir"]
            pdf_path = Path(work_dir) / "frames.pdf"
            pdf_bytes = img2pdf.convert([str(path) for path in kept_paths])
            pdf_path.write_bytes(pdf_bytes)

            await with_retries(
                context.bot.send_document,
                chat_id=chat_id,
                document=str(pdf_path),
                filename="frames_deduped.pdf",
                caption=(
                    f"🧹 Removed {len(removed_paths)} near-duplicate frame(s) — "
                    f"{len(kept_paths)} remain.\n"
                    f"(auto threshold: {threshold_used:.0f})"
                ),
                reply_markup=post_pdf_keyboard(message_id, dedup_available=False),
            )

        except Exception as error:
            logger.exception("Error deduplicating frames:")
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"❌ Something went wrong removing similar frames.\n\nError: {error}",
            )

        return

    if action == "pdfcompress":
        pending = pending_compressions.get(message_id)

        if pending is None:
            await query.message.reply_text(
                "⚠️ That video isn't in my cache anymore — please resend it."
            )
            return

        input_path = pending["input_path"]
        work_dir = pending["work_dir"]
        duration = pending.get("duration")
        chat_id = pending["chat_id"]

        # The "Compress video" button can sit on the PDF *document*
        # message (after "auto-delete similar frames"), and a document
        # has no text to edit — editing it raises an error that used to
        # be swallowed silently, so nothing appeared to happen. So
        # instead of editing the tapped message, drop its buttons (stops
        # double-taps) and post a fresh status message to work with.
        with contextlib.suppress(Exception):
            await query.edit_message_reply_markup(reply_markup=None)

        status_message = await context.bot.send_message(
            chat_id=chat_id,
            text="🔎 Getting samples ready...",
        )

        await analyze_and_show_level_menu(
            context,
            status_message=status_message,
            chat_id=chat_id,
            message_id=message_id,
            input_path=input_path,
            duration=duration,
            work_dir=work_dir,
            original_size=pending["original_size"],
            pending=pending,
        )
        return

    if action == "pdffinish":
        clear_pending(message_id)

        finished_text = "✅ Session finished — the stored video and temporary files were deleted."

        try:
            await query.edit_message_text(finished_text)
        except Exception:
            # Tapped from the PDF document message (no text to edit):
            # remove its buttons and post the confirmation instead.
            with contextlib.suppress(Exception):
                await query.edit_message_reply_markup(reply_markup=None)
            await query.message.reply_text(finished_text)
        return

    if action == "audioedit":
        pending = pending_compressions.get(message_id)

        if pending is None:
            await query.message.reply_text(
                "⚠️ That video isn't in my cache anymore — please resend it."
            )
            return

        has_audio, source_audio_bitrate = await probe_audio_stream(str(pending["input_path"]))

        if not has_audio:
            await query.message.reply_text(
                "⚠️ This video has no audio track, so there's nothing to edit. "
                "Pick a compression level above instead."
            )
            return

        duration = pending.get("duration")
        original_size = pending.get("original_size")

        # The video is copied untouched, so the result is roughly:
        # original size - the original audio track + the new audio track.
        source_audio_bytes = (
            int(source_audio_bitrate * duration / 8)
            if source_audio_bitrate and duration else None
        )

        audio_estimates: dict[str, int | None] = {}
        for audio_key in AUDIO_LEVEL_ORDER:
            new_audio_bytes = estimate_audio_bytes(AUDIO_LEVELS[audio_key]["bitrate"], duration)
            if original_size and source_audio_bytes is not None and new_audio_bytes is not None:
                audio_estimates[audio_key] = max(0, original_size - source_audio_bytes) + new_audio_bytes
            else:
                audio_estimates[audio_key] = None

        await query.edit_message_text(
            "🎧 Just editing the audio — the video stays exactly as it is (no re-encode).\n"
            "Pick audio quality:",
            reply_markup=audio_keyboard(message_id, AUDIO_EDIT_LEVEL, audio_estimates, DEFAULT_AUDIO_LEVEL),
        )
        return

    if action == "audioreplace":
        pending = pending_compressions.get(message_id)

        if pending is None:
            await query.message.reply_text(
                "⚠️ That video isn't in my cache anymore — please resend it."
            )
            return

        chat_id = pending["chat_id"]
        awaiting_audio_replacement[chat_id] = message_id

        await context.bot.send_message(
            chat_id=chat_id,
            text=(
                "🔁 Send me the new audio now — a voice message, an audio file, "
                "or an audio document.\n\n"
                "It replaces the video's current audio; the picture stays exactly as it is. "
                "The video decides the final length: a longer recording is cut at the end "
                "of the video, a shorter one leaves the rest silent."
            ),
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("❌ Cancel", callback_data=f"audioreplacecancel:_:{message_id}")
            ]]),
        )
        return

    if action == "audioreplacecancel":
        pending = pending_compressions.get(message_id)
        chat_id = pending["chat_id"] if pending else query.message.chat_id

        if awaiting_audio_replacement.get(chat_id) == message_id:
            awaiting_audio_replacement.pop(chat_id, None)

        try:
            await query.edit_message_text(
                "❌ Audio replacement cancelled — pick another option from the menu above."
            )
        except Exception:
            await query.message.reply_text("❌ Audio replacement cancelled.")
        return

    if action == "audioset":
        try:
            chosen_level, chosen_audio = level.split("|", 1)
        except ValueError:
            return

        if not is_flow_level(chosen_level) or chosen_audio not in AUDIO_LEVELS:
            return

        pending = pending_compressions.get(message_id)

        if pending is None:
            await query.message.reply_text(
                "⚠️ That file isn't in my cache anymore — please resend it."
            )
            return

        header = flow_header(chosen_level, chosen_audio)

        await query.edit_message_text(
            f"{header}\n🎛 Optional audio enhancements — toggle, then continue:",
            reply_markup=enhancement_keyboard(message_id, chosen_level, chosen_audio, False, False),
        )
        return

    if action == "enhset":
        try:
            chosen_level, chosen_audio, flags_str = level.split("|", 2)
            noise_reduction = flags_str[0] == "1"
            voice_enhancement = flags_str[1] == "1"
        except (ValueError, IndexError):
            return

        if not is_flow_level(chosen_level) or chosen_audio not in AUDIO_LEVELS:
            return

        header = flow_header(chosen_level, chosen_audio)

        await query.edit_message_text(
            f"{header}\n🎛 Optional audio enhancements — toggle, then continue:",
            reply_markup=enhancement_keyboard(
                message_id, chosen_level, chosen_audio, noise_reduction, voice_enhancement
            ),
        )
        return

    if action == "enhcontinue":
        try:
            chosen_level, chosen_audio, flags_str = level.split("|", 2)
            noise_reduction = flags_str[0] == "1"
            voice_enhancement = flags_str[1] == "1"
        except (ValueError, IndexError):
            return

        if not is_flow_level(chosen_level) or chosen_audio not in AUDIO_LEVELS:
            return

        pending = pending_compressions.get(message_id)

        if pending is None:
            await query.message.reply_text(
                "⚠️ That file isn't in my cache anymore — please resend it."
            )
            return

        chat_id = pending["chat_id"]
        input_path = pending["input_path"]
        work_dir = pending["work_dir"]
        duration = pending.get("duration")
        audio_bitrate = AUDIO_LEVELS[chosen_audio]["bitrate"]

        enhancement_bits = []
        if noise_reduction:
            enhancement_bits.append("🔇 noise reduction")
        if voice_enhancement:
            enhancement_bits.append("🎙 voice enhancement")
        enhancement_note = f" ({' + '.join(enhancement_bits)})" if enhancement_bits else ""

        await query.edit_message_text(
            f"🎧 Generating {VOLUME_PREVIEW_SECONDS}s previews at each volume level{enhancement_note} — one moment..."
        )

        sample_duration = min(VOLUME_PREVIEW_SECONDS, duration) if duration else VOLUME_PREVIEW_SECONDS
        seek = max(0.0, (duration - sample_duration) / 2) if duration else 0.0

        preview_semaphore = asyncio.Semaphore(ESTIMATE_CONCURRENCY)

        async def bounded_preview(boost: int) -> tuple[int, Path | None]:
            preview_path = work_dir / f"preview_{boost}.m4a"
            async with preview_semaphore:
                ok = await generate_volume_preview(
                    str(input_path), str(preview_path), seek, sample_duration, audio_bitrate, boost,
                    noise_reduction=noise_reduction, voice_enhancement=voice_enhancement,
                )
            return boost, (preview_path if ok else None)

        results = await asyncio.gather(*(bounded_preview(boost) for boost in VOLUME_BOOST_OPTIONS))

        await context.bot.send_message(
            chat_id=chat_id,
            text=f"🎧 {sample_duration:.0f}s preview at each volume level{enhancement_note}, from the middle:",
        )

        for boost, preview_path in sorted(results, key=lambda item: item[0]):
            label = "No boost" if boost == 0 else f"+{boost}%"

            if preview_path is None:
                await context.bot.send_message(
                    chat_id=chat_id, text=f"⚠️ Couldn't generate a preview for {label}."
                )
                continue

            try:
                await with_retries(
                    context.bot.send_audio,
                    chat_id=chat_id,
                    audio=str(preview_path),
                    title=label,
                    caption=f"🔊 {label}",
                )
            finally:
                preview_path.unlink(missing_ok=True)

        await context.bot.send_message(
            chat_id=chat_id,
            text="Pick a volume level:",
            reply_markup=volume_keyboard(
                message_id, chosen_level, chosen_audio, noise_reduction, voice_enhancement
            ),
        )
        return

    if action == "volumeset":
        try:
            chosen_level, chosen_audio, boost_str, flags_str = level.split("|", 3)
            boost = int(boost_str)
            noise_reduction = flags_str[0] == "1"
            voice_enhancement = flags_str[1] == "1"
        except (ValueError, IndexError):
            return

        if (
            not is_flow_level(chosen_level)
            or chosen_audio not in AUDIO_LEVELS
            or boost not in VOLUME_BOOST_OPTIONS
        ):
            return

        pending = pending_compressions.get(message_id)

        if pending is None:
            await query.message.reply_text(
                "⚠️ That file isn't in my cache anymore — please resend it."
            )
            return

        await run_compression(
            update, context,
            input_path=pending["input_path"],
            work_dir=pending["work_dir"],
            original_size=pending["original_size"],
            level=None if chosen_level == AUDIO_ONLY_LEVEL else chosen_level,
            chat_id=pending["chat_id"],
            source_message_id=message_id,
            status_message=query.message,
            duration=pending.get("duration"),
            audio_level=chosen_audio,
            volume_boost_percent=boost,
            noise_reduction=noise_reduction,
            voice_enhancement=voice_enhancement,
        )
        return

    if level not in LEVELS:
        return

    if action == "compress":
        pending = pending_compressions.get(message_id)

        if pending is None:
            await query.message.reply_text(
                "⚠️ That video isn't in my cache anymore — please resend it."
            )
            return

        duration = pending.get("duration")
        level_settings = LEVELS[level]
        total_estimate = pending.get("estimates", {}).get(level)

        # Back out roughly how much of that level's estimate is audio
        # (using the level's own default audio bitrate) so each audio
        # quality option can be re-estimated without any extra encoding.
        default_audio_estimate = estimate_audio_bytes(level_settings["audio_bitrate"], duration)
        video_only_estimate = None
        if total_estimate is not None and default_audio_estimate is not None:
            video_only_estimate = max(0, total_estimate - default_audio_estimate)

        audio_estimates: dict[str, int | None] = {}
        for audio_key in AUDIO_LEVEL_ORDER:
            audio_bytes = estimate_audio_bytes(AUDIO_LEVELS[audio_key]["bitrate"], duration)
            if video_only_estimate is not None and audio_bytes is not None:
                audio_estimates[audio_key] = video_only_estimate + audio_bytes
            else:
                audio_estimates[audio_key] = None

        await query.edit_message_text(
            f"🎚️ {level_settings['label']} selected. Now pick audio quality:",
            reply_markup=audio_keyboard(message_id, level, audio_estimates, DEFAULT_AUDIO_LEVEL),
        )
        return

    if action == "redo":
        cached = recent_files.get(message_id)

        if cached is None:
            await query.message.reply_text(
                "⚠️ I don't have that video cached anymore — please resend it."
            )
            return

        if chat_id is None:
            return

        await query.message.reply_text(f"🔁 Recompressing at {LEVELS[level]['label']}...")

        Path(TEMP_DIR).mkdir(parents=True, exist_ok=True)
        work_dir = Path(tempfile.mkdtemp(prefix=f"redo_{message_id}_", dir=TEMP_DIR))
        input_path = work_dir / "input"

        try:
            telegram_file = await with_retries(context.bot.get_file, cached["file_id"])
            await with_retries(telegram_file.download_to_drive, custom_path=str(input_path))
        except Exception as error:
            logger.exception("Error re-downloading for redo:")
            cleanup_work_dir(work_dir)
            await query.message.reply_text(f"❌ Couldn't re-download the video.\n\nError: {error}")
            return

        duration = await get_duration(str(input_path))

        await run_compression(
            update, context,
            input_path=input_path,
            work_dir=work_dir,
            original_size=cached["original_size"],
            level=level,
            chat_id=chat_id,
            source_message_id=message_id,
            duration=duration,
        )


# ============================================================
# Error handler
# ============================================================

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.exception("Unhandled Telegram error:", exc_info=context.error)


# ============================================================
# Main
# ============================================================

def main() -> None:
    logger.info("Starting Telegram Video Compressor...")
    load_approved_users()
    logger.info("Local Bot API URL: %s", TELEGRAM_API_URL)

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .base_url(f"{TELEGRAM_API_URL}/bot{{token}}")
        .base_file_url(f"{TELEGRAM_API_URL}/file/bot{{token}}")
        .local_mode(True)
        # Defaults here are just a few seconds, which is nowhere near
        # enough for large video downloads/uploads through the Local
        # Bot API server — this is almost certainly why timeouts were
        # happening in the first place.
        .read_timeout(600)
        .write_timeout(600)
        .connect_timeout(60)
        .pool_timeout(60)
        .media_write_timeout(600)
        .post_init(post_init)
        .build()
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("setlevel", setlevel))
    application.add_handler(CommandHandler("users", users_command))
    application.add_handler(MessageHandler(filters.VIDEO, handle_video))
    application.add_handler(MessageHandler(filters.AUDIO | filters.VOICE, handle_audio))
    application.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    # Admin forwards a text post from the backup channel -> bot replies with the channel ID.
    application.add_handler(
        MessageHandler(filters.FORWARDED & filters.TEXT & ~filters.COMMAND, report_forwarded_channel_id)
    )
    # Must come BEFORE the general button handler: the first matching
    # handler wins, and access-request buttons have their own format.
    application.add_handler(CallbackQueryHandler(handle_access_decision, pattern=r"^access:"))
    application.add_handler(CallbackQueryHandler(handle_callback))
    application.add_error_handler(error_handler)

    logger.info("Bot is running.")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
