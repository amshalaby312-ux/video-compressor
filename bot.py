import os
import asyncio
import logging
import tempfile
from pathlib import Path

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.constants import ChatAction
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

# URL of the Local Telegram Bot API server.
TELEGRAM_API_URL = os.getenv(
    "TELEGRAM_API_URL",
    "http://localhost:8081",
)

# Temporary working directory.
TEMP_DIR = os.getenv("TEMP_DIR", "/tmp/video-compressor")

# ============================================================
# Compression levels
# ============================================================
#
# Each level bundles together the FFmpeg settings that control
# output quality/size. "medium" mirrors the bot's old single
# fixed behavior and can still be tuned via env vars so existing
# Railway variables keep working.

LEVELS: dict[str, dict] = {
    "low": {
        "label": "🟢 Low compression",
        "detail": "Best quality, biggest file",
        "crf": "23",
        "preset": "medium",
        "max_width": 1920,
        "audio_bitrate": "128k",
    },
    "medium": {
        "label": "🟡 Medium compression",
        "detail": "Balanced (default)",
        "crf": os.getenv("CRF", "28"),
        "preset": os.getenv("PRESET", "medium"),
        "max_width": int(os.getenv("MAX_WIDTH", "1280")),
        "audio_bitrate": os.getenv("AUDIO_BITRATE", "96k"),
    },
    "high": {
        "label": "🔴 High compression",
        "detail": "Smallest file, lower quality",
        "crf": "32",
        "preset": "fast",
        "max_width": 854,
        "audio_bitrate": "64k",
    },
}

LEVEL_ORDER = ["low", "medium", "high"]

DEFAULT_LEVEL = "medium"

# Per-chat default compression level.
# NOTE: this lives in memory only, so it resets whenever the
# service redeploys or restarts. Fine for a personal bot; if you
# want it to survive restarts, it can be written to a small JSON
# file on disk instead.
chat_default_level: dict[int, str] = {}

# Short-lived cache so the "redo at a different level" buttons
# can re-fetch a video without you re-sending it. Keyed by the
# original message_id.
recent_files: dict[int, dict] = {}
RECENT_FILES_MAX = 200


def remember_file(message_id: int, file_id: str, original_size: int | None) -> None:
    recent_files[message_id] = {
        "file_id": file_id,
        "original_size": original_size,
    }

    # Trim the cache so it doesn't grow forever.
    if len(recent_files) > RECENT_FILES_MAX:
        oldest_key = next(iter(recent_files))
        recent_files.pop(oldest_key, None)


def get_default_level(chat_id: int) -> str:
    return chat_default_level.get(chat_id, DEFAULT_LEVEL)


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

def format_size(size: int) -> str:
    """Convert bytes to a human-readable size."""

    units = ["B", "KB", "MB", "GB", "TB"]

    value = float(size)

    for unit in units:
        if value < 1024:
            return f"{value:.1f} {unit}"

        value /= 1024

    return f"{value:.1f} PB"


def get_video_filter(max_width: int) -> str:
    """
    Scale the video down so its width does not exceed max_width.

    - Keeps aspect ratio.
    - -2 ensures dimensions remain compatible with H.264.
    """

    return f"scale='min({max_width},iw)':-2"


def level_keyboard(prefix: str, message_id: int, highlight: str | None = None) -> InlineKeyboardMarkup:
    """
    Build the Low/Medium/High inline keyboard.

    prefix is "compress" (process a freshly received video) or
    "redo" (recompress an already-processed one) or "setlevel"
    (just change the stored default, no video attached).
    """

    buttons = []

    for level in LEVEL_ORDER:
        info = LEVELS[level]
        text = info["label"]

        if level == highlight:
            text = f"✅ {text}"

        buttons.append(
            InlineKeyboardButton(
                text,
                callback_data=f"{prefix}:{level}:{message_id}",
            )
        )

    return InlineKeyboardMarkup([buttons])


# ============================================================
# FFmpeg
# ============================================================

async def compress_video(
    input_path: str,
    output_path: str,
    level: str,
) -> None:
    """
    Compress a video using FFmpeg at the given level.
    """

    settings = LEVELS[level]

    video_filter = get_video_filter(settings["max_width"])

    command = [
        "ffmpeg",

        "-y",

        "-i",
        input_path,

        # Video
        "-c:v",
        "libx264",

        "-preset",
        settings["preset"],

        "-crf",
        settings["crf"],

        "-vf",
        video_filter,

        # Audio
        "-c:a",
        "aac",

        "-b:a",
        settings["audio_bitrate"],

        # Makes MP4 start playing sooner when downloaded/streamed.
        "-movflags",
        "+faststart",

        output_path,
    ]

    logger.info("Running FFmpeg (%s): %s", level, " ".join(command))

    process = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    stdout, stderr = await process.communicate()

    if process.returncode != 0:
        error = stderr.decode(errors="replace")

        logger.error("FFmpeg failed:\n%s", error)

        raise RuntimeError(
            f"FFmpeg failed with exit code {process.returncode}"
        )

    logger.info("FFmpeg compression completed.")


# ============================================================
# Telegram
# ============================================================

async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:

    await update.message.reply_text(
        "🎥 Send me a video and I'll compress it for you.\n\n"
        "You can also forward a video to me.\n\n"
        "Use /setlevel to change the default compression strength."
    )


async def setlevel(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:

    message = update.effective_message

    if message is None:
        return

    chat_id = message.chat_id
    current = get_default_level(chat_id)

    await message.reply_text(
        "Pick the default compression level to use for new videos:",
        reply_markup=level_keyboard("setlevel", message.message_id, highlight=current),
    )


async def process_video(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    file_id: str,
    original_size: int | None,
    filename: str,
    level: str,
    reply_to_message_id: int,
) -> None:

    message = update.effective_message

    if message is None:
        return

    chat_id = message.chat_id

    # --------------------------------------------------------
    # Create temporary directory
    # --------------------------------------------------------

    Path(TEMP_DIR).mkdir(
        parents=True,
        exist_ok=True,
    )

    work_dir = Path(
        tempfile.mkdtemp(
            prefix=f"video_{message.message_id}_",
            dir=TEMP_DIR,
        )
    )

    input_path = work_dir / "input"

    output_path = work_dir / "compressed.mp4"

    level_info = LEVELS[level]

    try:

        # ----------------------------------------------------
        # Initial message
        # ----------------------------------------------------

        if original_size:
            size_text = format_size(original_size)
        else:
            size_text = "unknown size"

        status_message = await message.reply_text(
            f"📥 Receiving video...\n"
            f"Original size: {size_text}\n"
            f"Level: {level_info['label']}"
        )

        # ----------------------------------------------------
        # Download from Telegram
        # ----------------------------------------------------

        await context.bot.send_chat_action(
            chat_id=chat_id,
            action=ChatAction.UPLOAD_VIDEO,
        )

        logger.info(
            "Downloading Telegram file %s",
            file_id,
        )

        telegram_file = await context.bot.get_file(file_id)

        await telegram_file.download_to_drive(
            custom_path=str(input_path)
        )

        downloaded_size = input_path.stat().st_size

        logger.info(
            "Downloaded %s",
            format_size(downloaded_size),
        )

        # ----------------------------------------------------
        # Compress
        # ----------------------------------------------------

        await status_message.edit_text(
            f"⚙️ Compressing video ({level_info['label']})...\n"
            "This can take a while for large videos."
        )

        await compress_video(
            str(input_path),
            str(output_path),
            level,
        )

        compressed_size = output_path.stat().st_size

        # ----------------------------------------------------
        # Calculate savings
        # ----------------------------------------------------

        if downloaded_size > 0:
            saved = downloaded_size - compressed_size
            percentage = (saved / downloaded_size) * 100
        else:
            saved = 0
            percentage = 0

        logger.info(
            "Compression: %s -> %s (%.1f%% saved)",
            format_size(downloaded_size),
            format_size(compressed_size),
            percentage,
        )

        # ----------------------------------------------------
        # Send result
        # ----------------------------------------------------

        await status_message.edit_text("📤 Sending compressed video...")

        await context.bot.send_chat_action(
            chat_id=chat_id,
            action=ChatAction.UPLOAD_VIDEO,
        )

        # Remember this file so the "redo" buttons can re-fetch it
        # without asking you to resend the video.
        remember_file(reply_to_message_id, file_id, original_size)

        await message.reply_video(
            video=str(output_path),
            supports_streaming=True,
            caption=(
                f"✅ Compression complete! ({level_info['label']})\n\n"
                f"Original: {format_size(downloaded_size)}\n"
                f"Compressed: {format_size(compressed_size)}\n"
                f"Saved: {percentage:.1f}%"
            ),
            reply_markup=level_keyboard("redo", reply_to_message_id),
        )

        logger.info(
            "Finished processing message %s",
            message.message_id,
        )

    except Exception as error:

        logger.exception("Error processing video:")

        await message.reply_text(
            "❌ Something went wrong while processing "
            "the video.\n\n"
            f"Error: {error}"
        )

    finally:

        # ----------------------------------------------------
        # Cleanup
        # ----------------------------------------------------

        try:
            for file in work_dir.iterdir():
                if file.is_file():
                    file.unlink()

            work_dir.rmdir()

        except Exception:
            logger.exception("Could not clean temporary files.")


# ============================================================
# Video handler
# ============================================================

async def handle_video(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:

    message = update.effective_message

    if message is None or message.video is None:
        return

    video = message.video

    filename = f"video_{message.message_id}.mp4"

    level = get_default_level(message.chat_id)

    remember_file(message.message_id, video.file_id, video.file_size)

    await process_video(
        update=update,
        context=context,
        file_id=video.file_id,
        original_size=video.file_size,
        filename=filename,
        level=level,
        reply_to_message_id=message.message_id,
    )


# ============================================================
# Document handler
# ============================================================

async def handle_document(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:

    message = update.effective_message

    if message is None or message.document is None:
        return

    document = message.document

    mime_type = document.mime_type or ""

    filename = document.file_name or "video"

    is_video = (
        mime_type.startswith("video/")
        or filename.lower().endswith(
            (
                ".mp4",
                ".mkv",
                ".mov",
                ".avi",
                ".webm",
                ".m4v",
                ".mpeg",
                ".mpg",
                ".3gp",
            )
        )
    )

    if not is_video:
        await message.reply_text("❌ That doesn't look like a video file.")
        return

    level = get_default_level(message.chat_id)

    remember_file(message.message_id, document.file_id, document.file_size)

    await process_video(
        update=update,
        context=context,
        file_id=document.file_id,
        original_size=document.file_size,
        filename=filename,
        level=level,
        reply_to_message_id=message.message_id,
    )


# ============================================================
# Button handler (setlevel + redo)
# ============================================================

async def handle_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:

    query = update.callback_query

    if query is None or query.data is None:
        return

    await query.answer()

    try:
        action, level, message_id_str = query.data.split(":", 2)
        message_id = int(message_id_str)
    except ValueError:
        return

    if level not in LEVELS:
        return

    chat_id = query.message.chat_id if query.message else None

    if action == "setlevel":
        if chat_id is not None:
            chat_default_level[chat_id] = level

        await query.edit_message_text(
            f"Default compression level set to {LEVELS[level]['label']}.\n"
            "This will be used for new videos you send."
        )
        return

    if action == "redo":
        cached = recent_files.get(message_id)

        if cached is None:
            await query.message.reply_text(
                "⚠️ I don't have that video cached anymore — please resend it."
            )
            return

        await query.message.reply_text(
            f"🔁 Recompressing at {LEVELS[level]['label']}..."
        )

        await process_video(
            update=update,
            context=context,
            file_id=cached["file_id"],
            original_size=cached["original_size"],
            filename="video",
            level=level,
            reply_to_message_id=message_id,
        )


# ============================================================
# Error handler
# ============================================================

async def error_handler(
    update: object,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:

    logger.exception(
        "Unhandled Telegram error:",
        exc_info=context.error,
    )


# ============================================================
# Main
# ============================================================

def main() -> None:

    logger.info("Starting Telegram Video Compressor...")
    logger.info("Local Bot API URL: %s", TELEGRAM_API_URL)

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .base_url(f"{TELEGRAM_API_URL}/bot{{token}}")
        .base_file_url(f"{TELEGRAM_API_URL}/file/bot{{token}}")
        .local_mode(True)
        .build()
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("setlevel", setlevel))

    application.add_handler(MessageHandler(filters.VIDEO, handle_video))
    application.add_handler(MessageHandler(filters.Document.ALL, handle_document))

    application.add_handler(CallbackQueryHandler(handle_callback))

    application.add_error_handler(error_handler)

    logger.info("Bot is running.")

    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
