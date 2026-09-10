import os
import asyncio
import logging
import tempfile
import time
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

TELEGRAM_API_URL = os.getenv(
    "TELEGRAM_API_URL",
    "http://localhost:8081",
)

TEMP_DIR = os.getenv("TEMP_DIR", "/tmp/video-compressor")

# How many seconds of the video to sample when estimating size.
SAMPLE_SECONDS = float(os.getenv("SAMPLE_SECONDS", "8"))

# Caps FFmpeg's own thread usage so it doesn't try to use more
# CPU than the service is actually allotted (helps avoid getting
# throttled/killed on tight Railway resource limits).
FFMPEG_THREADS = os.getenv("FFMPEG_THREADS", "2")

# How many level-estimates run at once during the "analyze" step.
# Running all 5 fully in parallel spikes CPU/RAM briefly; this
# caps it so the estimate phase stays within tight resource limits.
ESTIMATE_CONCURRENCY = int(os.getenv("ESTIMATE_CONCURRENCY", "2"))

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

# Per-chat default level, used only to mark which button is
# starred in the estimate menu.
chat_default_level: dict[int, str] = {}

# Videos that have been downloaded and analyzed, waiting on the
# user to pick a level. Keyed by the original message_id.
# NOTE: in-memory only — resets on redeploy/restart, and capped
# below since each entry holds a real downloaded video file.
pending_compressions: dict[int, dict] = {}
PENDING_MAX = 5

# Small cache so "redo at a different level" on an already-sent
# result can re-fetch the source without asking you to resend.
recent_files: dict[int, dict] = {}
RECENT_FILES_MAX = 200


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
        for file in work_dir.iterdir():
            if file.is_file():
                file.unlink()
        work_dir.rmdir()
    except Exception:
        logger.exception("Could not clean temporary files in %s", work_dir)


def remember_pending(message_id: int, entry: dict) -> None:
    pending_compressions[message_id] = entry

    if len(pending_compressions) > PENDING_MAX:
        oldest_key = next(iter(pending_compressions))
        oldest = pending_compressions.pop(oldest_key, None)
        if oldest is not None:
            cleanup_work_dir(oldest["work_dir"])


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


def get_video_filter(max_width: int) -> str:
    return f"scale='min({max_width},iw)':-2"


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
        InlineKeyboardButton("❌ Cancel", callback_data=f"cancel:_:{message_id}")
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


def build_ffmpeg_command(
    input_path: str,
    output_path: str,
    settings: dict,
    seek: float | None = None,
    duration: float | None = None,
) -> list[str]:
    command = ["ffmpeg", "-y"]

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
        "-b:a", settings["audio_bitrate"],
        "-movflags", "+faststart",
        # Prevents an abrupt "Conversion failed!" at finalization if
        # the muxer's internal packet queue backs up on a tight
        # memory budget.
        "-max_muxing_queue_size", "1024",
        output_path,
    ]

    return command


async def compress_video(input_path: str, output_path: str, level: str) -> None:
    settings = LEVELS[level]
    command = build_ffmpeg_command(input_path, output_path, settings)

    logger.info("Running FFmpeg (%s): %s", level, " ".join(command))

    returncode, stdout, stderr = await run_command(command)

    if returncode != 0:
        error = stderr.decode(errors="replace")
        logger.error("FFmpeg failed:\n%s", error)
        raise RuntimeError(f"FFmpeg failed with exit code {returncode}")

    logger.info("FFmpeg compression completed.")


async def estimate_level_size(
    input_path: str,
    duration: float | None,
    level: str,
    work_dir: Path,
) -> int | None:
    """
    Encode a short sample clip at this level and extrapolate the
    full-length size from it. Returns None if estimation fails
    (the level can still be picked — it just shows "unknown").
    """

    settings = LEVELS[level]

    if not duration or duration <= 0:
        return None

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

    try:
        if returncode != 0 or not sample_path.exists():
            logger.warning(
                "Estimate sample failed for %s: %s",
                level,
                stderr.decode(errors="replace"),
            )
            return None

        sample_size = sample_path.stat().st_size

        if sample_duration <= 0:
            return None

        return int((sample_size / sample_duration) * duration)

    finally:
        sample_path.unlink(missing_ok=True)


async def estimate_all_levels(input_path: str, duration: float | None, work_dir: Path) -> dict[str, int | None]:
    # Limit how many sample encodes run at once — 5 fully parallel
    # ffmpeg processes can spike CPU/RAM past tight resource limits.
    semaphore = asyncio.Semaphore(ESTIMATE_CONCURRENCY)

    async def bounded_estimate(level: str) -> int | None:
        async with semaphore:
            return await estimate_level_size(input_path, duration, level, work_dir)

    results = await asyncio.gather(
        *(bounded_estimate(level) for level in LEVEL_ORDER),
        return_exceptions=True,
    )

    estimates: dict[str, int | None] = {}

    for level, result in zip(LEVEL_ORDER, results):
        if isinstance(result, Exception):
            logger.warning("Estimate for %s raised: %s", level, result)
            estimates[level] = None
        else:
            estimates[level] = result

    return estimates


# ============================================================
# Telegram
# ============================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "🎥 Send me a video and I'll show you estimated sizes at a "
        "few compression levels before compressing.\n\n"
        "You can also forward a video to me.\n\n"
        "Use /setlevel to change which level is starred by default."
    )


async def setlevel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message

    if message is None:
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
) -> None:
    message = update.effective_message

    if message is None:
        return

    chat_id = message.chat_id

    Path(TEMP_DIR).mkdir(parents=True, exist_ok=True)

    work_dir = Path(
        tempfile.mkdtemp(prefix=f"video_{message.message_id}_", dir=TEMP_DIR)
    )

    input_path = work_dir / "input"

    try:
        size_text = format_size(original_size)

        status_message = await message.reply_text(
            f"📥 Downloading video ({size_text})..."
        )

        await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.UPLOAD_VIDEO)

        telegram_file = await context.bot.get_file(file_id)
        await telegram_file.download_to_drive(custom_path=str(input_path))

        downloaded_size = input_path.stat().st_size

        await status_message.edit_text(
            f"🔎 Analyzing video ({format_size(downloaded_size)})...\n"
            "Estimating size at each compression level, one moment..."
        )

        duration = await get_duration(str(input_path))
        estimates = await estimate_all_levels(str(input_path), duration, work_dir)

        remember_pending(
            message.message_id,
            {
                "input_path": input_path,
                "work_dir": work_dir,
                "original_size": downloaded_size,
                "filename": filename,
                "chat_id": chat_id,
            },
        )

        default_level = get_default_level(chat_id)

        lines = [f"Original size: {format_size(downloaded_size)}", ""]
        for level in LEVEL_ORDER:
            info = LEVELS[level]
            est = estimates.get(level)
            lines.append(f"{info['label']} — ~{format_size(est)} · {info['detail']}")

        lines.append("")
        lines.append("Estimates are based on a short sample and may vary ±15%.")
        lines.append("Pick a level to compress:")

        await status_message.edit_text(
            "\n".join(lines),
            reply_markup=estimate_keyboard(message.message_id, estimates, default_level),
        )

    except Exception as error:
        logger.exception("Error analyzing video:")
        cleanup_work_dir(work_dir)
        pending_compressions.pop(message.message_id, None)
        await message.reply_text(
            f"❌ Something went wrong while analyzing the video.\n\nError: {error}"
        )


async def run_compression(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    input_path: Path,
    work_dir: Path,
    original_size: int | None,
    level: str,
    chat_id: int,
    source_message_id: int,
    status_message=None,
) -> None:
    level_info = LEVELS[level]
    output_path = work_dir / "compressed.mp4"

    try:
        text = f"⚙️ Compressing at {level_info['label']}...\nThis can take a while for large videos."

        if status_message is not None:
            await status_message.edit_text(text)
        else:
            status_message = await context.bot.send_message(chat_id=chat_id, text=text)

        await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.UPLOAD_VIDEO)

        await compress_video(str(input_path), str(output_path), level)

        compressed_size = output_path.stat().st_size

        if original_size:
            saved = original_size - compressed_size
            percentage = (saved / original_size) * 100
        else:
            percentage = 0

        await status_message.edit_text("📤 Sending compressed video...")

        await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.UPLOAD_VIDEO)

        await context.bot.send_video(
            chat_id=chat_id,
            video=str(output_path),
            supports_streaming=True,
            caption=(
                f"✅ Compression complete! ({level_info['label']})\n\n"
                f"Original: {format_size(original_size)}\n"
                f"Compressed: {format_size(compressed_size)}\n"
                f"Saved: {percentage:.1f}%"
            ),
            reply_markup=redo_keyboard(source_message_id),
        )

    except Exception as error:
        logger.exception("Error compressing video:")
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"❌ Something went wrong while compressing.\n\nError: {error}",
        )

    finally:
        cleanup_work_dir(work_dir)
        pending_compressions.pop(source_message_id, None)


# ============================================================
# Video / document handlers
# ============================================================

async def handle_video(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message

    if message is None or message.video is None:
        return

    video = message.video
    remember_file(message.message_id, video.file_id, video.file_size)

    await handle_incoming(
        update, context,
        file_id=video.file_id,
        original_size=video.file_size,
        filename=f"video_{message.message_id}.mp4",
    )


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message

    if message is None or message.document is None:
        return

    document = message.document
    mime_type = document.mime_type or ""
    filename = document.file_name or "video"

    is_video = (
        mime_type.startswith("video/")
        or filename.lower().endswith(
            (".mp4", ".mkv", ".mov", ".avi", ".webm", ".m4v", ".mpeg", ".mpg", ".3gp")
        )
    )

    if not is_video:
        await message.reply_text("❌ That doesn't look like a video file.")
        return

    remember_file(message.message_id, document.file_id, document.file_size)

    await handle_incoming(
        update, context,
        file_id=document.file_id,
        original_size=document.file_size,
        filename=filename,
    )


# ============================================================
# Button handler (setlevel / compress / redo / cancel)
# ============================================================

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query

    if query is None or query.data is None:
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
        pending = pending_compressions.pop(message_id, None)
        if pending is not None:
            cleanup_work_dir(pending["work_dir"])
        await query.edit_message_text("❌ Cancelled — temporary files removed.")
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

        await run_compression(
            update, context,
            input_path=pending["input_path"],
            work_dir=pending["work_dir"],
            original_size=pending["original_size"],
            level=level,
            chat_id=pending["chat_id"],
            source_message_id=message_id,
            status_message=query.message,
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
            telegram_file = await context.bot.get_file(cached["file_id"])
            await telegram_file.download_to_drive(custom_path=str(input_path))
        except Exception as error:
            logger.exception("Error re-downloading for redo:")
            cleanup_work_dir(work_dir)
            await query.message.reply_text(f"❌ Couldn't re-download the video.\n\nError: {error}")
            return

        await run_compression(
            update, context,
            input_path=input_path,
            work_dir=work_dir,
            original_size=cached["original_size"],
            level=level,
            chat_id=chat_id,
            source_message_id=message_id,
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
