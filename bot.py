import os
import asyncio
import logging
import tempfile
from pathlib import Path

from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
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
#
# In Railway, if your service is called "telegram-api", Railway
# will provide an internal hostname. Set TELEGRAM_API_URL to:
#
# http://telegram-api:8081
#
TELEGRAM_API_URL = os.getenv(
    "TELEGRAM_API_URL",
    "http://localhost:8081",
)

# Maximum output resolution.
# 1280 means 720p/1080p videos can be reduced to max 1280px wide.
MAX_WIDTH = int(os.getenv("MAX_WIDTH", "1280"))

# CRF:
# Lower = better quality + larger file
# Higher = lower quality + smaller file
#
# 28 is a good starting point for this bot.
CRF = os.getenv("CRF", "28")

# FFmpeg encoding preset.
#
# Slower presets generally compress better but use more CPU.
PRESET = os.getenv("PRESET", "medium")

# Audio bitrate.
AUDIO_BITRATE = os.getenv("AUDIO_BITRATE", "96k")

# Temporary working directory.
TEMP_DIR = os.getenv("TEMP_DIR", "/tmp/video-compressor")


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


def get_video_filter() -> str:
    """
    Scale the video down so its width does not exceed MAX_WIDTH.

    - Keeps aspect ratio.
    - -2 ensures dimensions remain compatible with H.264.
    """

    return (
        f"scale='min({MAX_WIDTH},iw)':-2"
    )


# ============================================================
# FFmpeg
# ============================================================

async def compress_video(
    input_path: str,
    output_path: str,
) -> None:
    """
    Compress a video using FFmpeg.
    """

    video_filter = get_video_filter()

    command = [
        "ffmpeg",

        "-y",

        "-i",
        input_path,

        # Video
        "-c:v",
        "libx264",

        "-preset",
        PRESET,

        "-crf",
        CRF,

        "-vf",
        video_filter,

        # Audio
        "-c:a",
        "aac",

        "-b:a",
        AUDIO_BITRATE,

        # Makes MP4 start playing sooner when downloaded/streamed.
        "-movflags",
        "+faststart",

        output_path,
    ]

    logger.info("Running FFmpeg: %s", " ".join(command))

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
        "You can also forward a video to me."
    )


async def process_video(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    file_id: str,
    original_size: int | None,
    filename: str,
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

    # Each Telegram message gets its own temporary directory.
    work_dir = Path(
        tempfile.mkdtemp(
            prefix=f"video_{message.message_id}_",
            dir=TEMP_DIR,
        )
    )

    input_path = work_dir / "input"

    output_path = work_dir / "compressed.mp4"

    try:

        # ----------------------------------------------------
        # Initial message
        # ----------------------------------------------------

        if original_size:
            size_text = format_size(original_size)
        else:
            size_text = "unknown size"

        await message.reply_text(
            f"📥 Receiving video...\n"
            f"Original size: {size_text}"
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

        # Local Bot API server allows this download without
        # the normal cloud Bot API file-size restriction.
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

        await message.reply_text(
            "⚙️ Compressing video...\n"
            "This can take a while for large videos."
        )

        await compress_video(
            str(input_path),
            str(output_path),
        )

        compressed_size = output_path.stat().st_size

        # ----------------------------------------------------
        # Calculate savings
        # ----------------------------------------------------

        if downloaded_size > 0:

            saved = downloaded_size - compressed_size

            percentage = (
                saved / downloaded_size
            ) * 100

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

        await message.reply_text(
            "📤 Sending compressed video..."
        )

        await context.bot.send_chat_action(
            chat_id=chat_id,
            action=ChatAction.UPLOAD_VIDEO,
        )

        # In local_mode, python-telegram-bot can pass the
        # local filesystem path to the Local Bot API server.
        await message.reply_video(
            video=str(output_path),
            supports_streaming=True,
            caption=(
                f"✅ Compression complete!\n\n"
                f"Original: {format_size(downloaded_size)}\n"
                f"Compressed: {format_size(compressed_size)}\n"
                f"Saved: {percentage:.1f}%"
            ),
        )

        logger.info(
            "Finished processing message %s",
            message.message_id,
        )

    except Exception as error:

        logger.exception(
            "Error processing video:"
        )

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

            logger.exception(
                "Could not clean temporary files."
            )


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

    filename = (
        f"video_{message.message_id}.mp4"
    )

    await process_video(
        update=update,
        context=context,
        file_id=video.file_id,
        original_size=video.file_size,
        filename=filename,
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

    # Telegram sometimes receives a video as a document.
    # Accept common video MIME types and filenames.

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

        await message.reply_text(
            "❌ That doesn't look like a video file."
        )

        return

    await process_video(
        update=update,
        context=context,
        file_id=document.file_id,
        original_size=document.file_size,
        filename=filename,
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

    logger.info(
        "Starting Telegram Video Compressor..."
    )

    logger.info(
        "Local Bot API URL: %s",
        TELEGRAM_API_URL,
    )

    application = (
        Application.builder()
        .token(BOT_TOKEN)

        # IMPORTANT:
        # Tell python-telegram-bot to use our Local Bot API.
        .base_url(
            f"{TELEGRAM_API_URL}/bot{{token}}"
        )
        .base_file_url(
            f"{TELEGRAM_API_URL}/file/bot{{token}}"
        )

        # Tell python-telegram-bot that this is a
        # Local Bot API server.
        .local_mode(True)

        .build()
    )

    # Commands
    application.add_handler(
        CommandHandler(
            "start",
            start,
        )
    )

    # Videos sent normally or forwarded
    application.add_handler(
        MessageHandler(
            filters.VIDEO,
            handle_video,
        )
    )

    # Videos sent as documents/files
    application.add_handler(
        MessageHandler(
            filters.Document.ALL,
            handle_document,
        )
    )

    # Errors
    application.add_error_handler(
        error_handler
    )

    logger.info(
        "Bot is running."
    )

    application.run_polling(
        allowed_updates=Update.ALL_TYPES
    )


if __name__ == "__main__":
    main()
