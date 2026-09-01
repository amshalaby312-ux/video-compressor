#!/usr/bin/env python3
"""
Telegram Video/PDF Compressor Bot
==================================

Send the bot a video or a PDF. It downloads the file, compresses it
(ffmpeg for video, Ghostscript for PDF), and sends the compressed file
back to you along with before/after size stats.

Setup:
    1. pip install -r requirements.txt
    2. Install system deps: ffmpeg, ghostscript
       - Debian/Ubuntu: sudo apt install ffmpeg ghostscript
       - macOS:         brew install ffmpeg ghostscript
    3. Get a bot token from @BotFather on Telegram
    4. export BOT_TOKEN="123456:ABC-your-token"
    5. python bot.py

Notes on Telegram limits:
    - Regular Bot API: bots can download files up to 20 MB and upload
      files up to 50 MB.
    - If you run your own local Bot API server (telegram-bot-api),
      those limits rise to ~2000 MB. This script auto-detects a
      LOCAL_BOT_API_URL env var and uses it if set; see README.md.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.constants import ChatAction, ParseMode
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)
from telegram.error import TelegramError

# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
LOCAL_BOT_API_URL = os.environ.get("LOCAL_BOT_API_URL", "").strip() or None

# Regular Bot API hard limits (bytes). If using a local Bot API server,
# these are effectively much higher (~2000 MB) - adjust if you run one.
MAX_DOWNLOAD_BYTES = 20 * 1024 * 1024 if not LOCAL_BOT_API_URL else 2000 * 1024 * 1024
MAX_UPLOAD_BYTES = 50 * 1024 * 1024 if not LOCAL_BOT_API_URL else 2000 * 1024 * 1024

WORKDIR = Path(tempfile.gettempdir()) / "tg_compressor_bot"
WORKDIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("compressor_bot")


# --------------------------------------------------------------------------
# Per-user settings (in-memory; swap for a DB if you need persistence)
# --------------------------------------------------------------------------

@dataclass
class UserSettings:
    video_preset: str = "medium"   # "high" | "medium" | "low" | "tiny"
    pdf_preset: str = "ebook"      # ghostscript -dPDFSETTINGS value


USER_SETTINGS: dict[int, UserSettings] = {}


def get_settings(user_id: int) -> UserSettings:
    return USER_SETTINGS.setdefault(user_id, UserSettings())


VIDEO_PRESETS = {
    # name: (crf, extra scale filter or None, x264 preset)
    "high":   dict(crf=20, scale=None, speed="slow", label="High quality (larger file)"),
    "medium": dict(crf=25, scale=None, speed="medium", label="Medium (recommended)"),
    "low":    dict(crf=30, scale="-2:720", speed="medium", label="Low (720p, smaller)"),
    "tiny":   dict(crf=34, scale="-2:480", speed="fast", label="Tiny (480p, max compression)"),
}

PDF_PRESETS = {
    "prepress": "High quality (300 dpi images, larger file)",
    "printer":  "Print quality (300 dpi, good compression)",
    "ebook":    "Medium quality (150 dpi, recommended)",
    "screen":   "Max compression (72 dpi, smallest file)",
}


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def human_size(num_bytes: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if abs(num_bytes) < 1024.0:
            return f"{num_bytes:.1f} {unit}"
        num_bytes /= 1024.0
    return f"{num_bytes:.1f} TB"


async def run_subprocess(cmd: list[str], timeout: int = 1800) -> tuple[int, str, str]:
    """Run a subprocess off the event loop thread and return (rc, stdout, stderr)."""
    def _run():
        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
        )
        return proc.returncode, proc.stdout.decode(errors="replace"), proc.stderr.decode(errors="replace")

    return await asyncio.to_thread(_run)


def check_binary(name: str) -> bool:
    return shutil.which(name) is not None


# --------------------------------------------------------------------------
# Video compression
# --------------------------------------------------------------------------

async def probe_duration(path: Path) -> Optional[float]:
    """Return duration in seconds via ffprobe, or None if unknown."""
    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(path),
    ]
    rc, out, _ = await run_subprocess(cmd, timeout=30)
    if rc == 0:
        try:
            return float(out.strip())
        except ValueError:
            return None
    return None


async def compress_video(
    src: Path,
    dst: Path,
    preset_name: str,
    progress_cb=None,
) -> tuple[bool, str]:
    """
    Compress a video with ffmpeg using H.264 + CRF (constant quality).
    Audio is transcoded to AAC at a modest bitrate to also shrink audio size.
    Returns (success, message).
    """
    preset = VIDEO_PRESETS.get(preset_name, VIDEO_PRESETS["medium"])
    crf = preset["crf"]
    speed = preset["speed"]
    scale = preset["scale"]

    duration = await probe_duration(src)

    vf_args = []
    if scale:
        vf_args = ["-vf", f"scale={scale}"]

    cmd = [
        "ffmpeg", "-y",
        "-i", str(src),
        *vf_args,
        "-c:v", "libx264",
        "-crf", str(crf),
        "-preset", speed,
        "-pix_fmt", "yuv420p",
        "-c:a", "aac",
        "-b:a", "128k",
        "-movflags", "+faststart",
        "-progress", "pipe:1",
        "-nostats",
        str(dst),
    ]

    def _run_with_progress():
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        last_pct = -1
        for line in proc.stdout:
            if progress_cb and duration and "out_time_ms=" in line:
                try:
                    out_time_ms = int(line.strip().split("=")[1])
                    pct = min(99, int((out_time_ms / 1_000_000) / duration * 100))
                    if pct != last_pct:
                        last_pct = pct
                        progress_cb(pct)
                except Exception:
                    pass
        proc.wait()
        return proc.returncode

    rc = await asyncio.to_thread(_run_with_progress)
    if rc != 0 or not dst.exists():
        return False, "ffmpeg failed to compress the video."
    return True, "ok"


# --------------------------------------------------------------------------
# PDF compression
# --------------------------------------------------------------------------

async def compress_pdf(src: Path, dst: Path, preset_name: str) -> tuple[bool, str]:
    """
    Compress a PDF with Ghostscript (downsamples images, this is where
    most of the size reduction in a scanned/image-heavy PDF comes from).
    Falls back to a no-op copy if Ghostscript isn't available or the
    result isn't actually smaller.
    """
    preset = preset_name if preset_name in PDF_PRESETS else "ebook"

    if check_binary("gs"):
        cmd = [
            "gs",
            "-sDEVICE=pdfwrite",
            "-dCompatibilityLevel=1.5",
            f"-dPDFSETTINGS=/{preset}",
            "-dNOPAUSE", "-dQUIET", "-dBATCH",
            "-dDetectDuplicateImages=true",
            "-dCompressFonts=true",
            "-dSubsetFonts=true",
            f"-sOutputFile={dst}",
            str(src),
        ]
        rc, out, err = await run_subprocess(cmd, timeout=1200)
        if rc == 0 and dst.exists():
            # Ghostscript sometimes produces a *larger* file for already-
            # optimized PDFs. If so, just keep the original.
            if dst.stat().st_size >= src.stat().st_size:
                shutil.copyfile(src, dst)
                return True, "Already optimized - Ghostscript couldn't shrink it further, sending as-is."
            return True, "ok"
        log.warning("Ghostscript failed (rc=%s): %s", rc, err[:500])

    # Fallback: try pikepdf (lossless stream recompression only)
    try:
        import pikepdf
        with pikepdf.open(src) as pdf:
            pdf.save(dst, compress_streams=True, object_stream_mode=pikepdf.ObjectStreamMode.generate)
        if dst.exists() and dst.stat().st_size < src.stat().st_size:
            return True, "ok (lossless mode - install ghostscript for much better compression)"
        else:
            shutil.copyfile(src, dst)
            return True, "Couldn't shrink further without Ghostscript installed; sending original."
    except Exception as e:
        return False, f"No compressor available (install ghostscript): {e}"


# --------------------------------------------------------------------------
# Telegram handlers
# --------------------------------------------------------------------------

WELCOME = (
    "👋 *Video/PDF Compressor Bot*\n\n"
    "Send me a video or a PDF and I'll compress it and send it back.\n\n"
    "Commands:\n"
    "/settings \\- choose video/PDF compression level\n"
    "/help \\- show this message\n\n"
    f"⚠️ Regular Telegram bots can only *download* files up to "
    f"{human_size(MAX_DOWNLOAD_BYTES)} and *upload* up to {human_size(MAX_UPLOAD_BYTES)}\\. "
    "See README for how to raise this with a local Bot API server\\."
)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
await update.message.reply_text(WELCOME)

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await cmd_start(update, context)


async def cmd_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    settings = get_settings(update.effective_user.id)
    buttons = [
        [InlineKeyboardButton(f"🎬 Video: {settings.video_preset}", callback_data="menu_video")],
        [InlineKeyboardButton(f"📄 PDF: {settings.pdf_preset}", callback_data="menu_pdf")],
    ]
    await update.message.reply_text(
        "Choose what to configure:",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    settings = get_settings(user_id)
    data = query.data

    if data == "menu_video":
        buttons = [
            [InlineKeyboardButton(v["label"], callback_data=f"set_video_{k}")]
            for k, v in VIDEO_PRESETS.items()
        ]
        buttons.append([InlineKeyboardButton("⬅️ Back", callback_data="menu_back")])
        await query.edit_message_text("Pick video compression level:", reply_markup=InlineKeyboardMarkup(buttons))

    elif data == "menu_pdf":
        buttons = [
            [InlineKeyboardButton(v, callback_data=f"set_pdf_{k}")]
            for k, v in PDF_PRESETS.items()
        ]
        buttons.append([InlineKeyboardButton("⬅️ Back", callback_data="menu_back")])
        await query.edit_message_text("Pick PDF compression level:", reply_markup=InlineKeyboardMarkup(buttons))

    elif data.startswith("set_video_"):
        settings.video_preset = data.removeprefix("set_video_")
        await query.edit_message_text(f"✅ Video preset set to *{settings.video_preset}*", parse_mode=ParseMode.MARKDOWN)

    elif data.startswith("set_pdf_"):
        settings.pdf_preset = data.removeprefix("set_pdf_")
        await query.edit_message_text(f"✅ PDF preset set to *{settings.pdf_preset}*", parse_mode=ParseMode.MARKDOWN)

    elif data == "menu_back":
        buttons = [
            [InlineKeyboardButton(f"🎬 Video: {settings.video_preset}", callback_data="menu_video")],
            [InlineKeyboardButton(f"📄 PDF: {settings.pdf_preset}", callback_data="menu_pdf")],
        ]
        await query.edit_message_text("Choose what to configure:", reply_markup=InlineKeyboardMarkup(buttons))


async def handle_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Unified handler for videos and PDFs sent as video/document/etc."""
    message = update.effective_message
    user_id = update.effective_user.id
    settings = get_settings(user_id)

    tg_file_obj = None
    file_name = None
    file_size = None
    kind = None  # "video" or "pdf"

    if message.video:
        tg_file_obj = message.video
        file_name = tg_file_obj.file_name or f"video_{tg_file_obj.file_unique_id}.mp4"
        file_size = tg_file_obj.file_size
        kind = "video"
    elif message.document:
        doc = message.document
        mime = (doc.mime_type or "").lower()
        name = (doc.file_name or "").lower()
        if mime.startswith("video/") or name.endswith((".mp4", ".mov", ".mkv", ".avi", ".webm")):
            tg_file_obj = doc
            file_name = doc.file_name or f"video_{doc.file_unique_id}.mp4"
            file_size = doc.file_size
            kind = "video"
        elif mime == "application/pdf" or name.endswith(".pdf"):
            tg_file_obj = doc
            file_name = doc.file_name or f"file_{doc.file_unique_id}.pdf"
            file_size = doc.file_size
            kind = "pdf"
        else:
            await message.reply_text("I only compress videos and PDFs. Send one of those 🙂")
            return
    else:
        return

    if file_size and file_size > MAX_DOWNLOAD_BYTES:
        await message.reply_text(
            f"That file is {human_size(file_size)}, which is over the "
            f"{human_size(MAX_DOWNLOAD_BYTES)} download limit for this bot. "
            "See README.md for running a local Bot API server to raise this limit."
        )
        return

    job_dir = WORKDIR / f"{user_id}_{int(time.time()*1000)}"
    job_dir.mkdir(parents=True, exist_ok=True)
    src_path = job_dir / file_name
    ext = ".mp4" if kind == "video" else ".pdf"
    dst_path = job_dir / (Path(file_name).stem + "_compressed" + ext)

    status_msg = await message.reply_text("⬇️ Downloading...")

    try:
        await context.bot.send_chat_action(chat_id=message.chat_id, action=ChatAction.TYPING)
        tg_file = await tg_file_obj.get_file()
        await tg_file.download_to_drive(custom_path=str(src_path))
    except TelegramError as e:
        await status_msg.edit_text(f"❌ Failed to download file: {e}")
        shutil.rmtree(job_dir, ignore_errors=True)
        return

    original_size = src_path.stat().st_size

    if kind == "video":
        await status_msg.edit_text("🎬 Compressing video (0%)...")
        await context.bot.send_chat_action(chat_id=message.chat_id, action=ChatAction.RECORD_VIDEO)

        loop = asyncio.get_running_loop()
        last_edit_time = [0.0]

        def progress_cb(pct: int):
            now = time.time()
            if now - last_edit_time[0] > 3:  # throttle edits to every 3s
                last_edit_time[0] = now
                asyncio.run_coroutine_threadsafe(
                    status_msg.edit_text(f"🎬 Compressing video ({pct}%)..."),
                    loop,
                )

        ok, msg = await compress_video(src_path, dst_path, settings.video_preset, progress_cb)
    else:
        await status_msg.edit_text("📄 Compressing PDF...")
        ok, msg = await compress_pdf(src_path, dst_path, settings.pdf_preset)

    if not ok:
        await status_msg.edit_text(f"❌ Compression failed: {msg}")
        shutil.rmtree(job_dir, ignore_errors=True)
        return

    compressed_size = dst_path.stat().st_size
    saved_pct = (1 - compressed_size / original_size) * 100 if original_size else 0

    if compressed_size > MAX_UPLOAD_BYTES:
        await status_msg.edit_text(
            f"⚠️ Compressed file is still {human_size(compressed_size)}, over the "
            f"{human_size(MAX_UPLOAD_BYTES)} upload limit. Try a smaller/lower preset "
            "with /settings, or run a local Bot API server (see README.md)."
        )
        shutil.rmtree(job_dir, ignore_errors=True)
        return

    caption = (
        f"✅ Done!\n"
        f"Original: {human_size(original_size)}\n"
        f"Compressed: {human_size(compressed_size)}\n"
        f"Saved: {saved_pct:.1f}%"
    )

    try:
        await status_msg.edit_text("⬆️ Uploading result...")
        with open(dst_path, "rb") as f:
            if kind == "video":
                await message.reply_video(video=f, caption=caption, filename=dst_path.name, supports_streaming=True)
            else:
                await message.reply_document(document=f, caption=caption, filename=dst_path.name)
        await status_msg.delete()
    except TelegramError as e:
        await status_msg.edit_text(f"❌ Failed to upload result: {e}")
    finally:
        shutil.rmtree(job_dir, ignore_errors=True)


async def on_startup_check(app: Application):
    missing = []
    if not check_binary("ffmpeg"):
        missing.append("ffmpeg")
    if not check_binary("ffprobe"):
        missing.append("ffprobe")
    if not check_binary("gs"):
        log.warning("Ghostscript ('gs') not found - PDF compression will fall back to a weaker lossless mode.")
    if missing:
        log.error("Missing required binaries: %s. Install them before running.", ", ".join(missing))


def main():
    if not BOT_TOKEN:
        raise SystemExit("Set the BOT_TOKEN environment variable (get one from @BotFather).")

    builder = ApplicationBuilder().token(BOT_TOKEN).post_init(on_startup_check)
    if LOCAL_BOT_API_URL:
        builder = builder.base_url(f"{LOCAL_BOT_API_URL}/bot").base_file_url(f"{LOCAL_BOT_API_URL}/file/bot")

    app = builder.build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("settings", cmd_settings))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.VIDEO | filters.Document.ALL, handle_file))

    log.info("Bot starting (download limit=%s, upload limit=%s)...",
              human_size(MAX_DOWNLOAD_BYTES), human_size(MAX_UPLOAD_BYTES))
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
