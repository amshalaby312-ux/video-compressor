import os
import asyncio
import logging
import shutil
import tempfile
import time
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
# Access control — hard-coded on purpose (not an env var) so the
# allowlist lives in the repo, not in Railway config. Add your
# numeric Telegram user ID(s) here. You can get your own ID by
# messaging @userinfobot on Telegram, or just try using this bot
# once — it will reply with your ID so you can copy it in here.
# ------------------------------------------------------------
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
    # <- replace with your Telegram user ID
    # 222222222,  # <- add more IDs here if needed
}


def is_allowed(user_id: int | None) -> bool:
    return user_id is not None and user_id in ALLOWED_USER_IDS


async def reject_unauthorized(message, user_id: int | None) -> None:
    await message.reply_text(
        "⛔ You're not authorized to use this bot.\n\n"
        f"Your Telegram user ID is: {user_id}\n"
        "If this is your account, add that number to ALLOWED_USER_IDS in bot.py."
    )

# How many times to retry a flaky network call (download/upload)
# before giving up, and the base delay between attempts.
RETRY_ATTEMPTS = int(os.getenv("RETRY_ATTEMPTS", "5"))
RETRY_BASE_DELAY = float(os.getenv("RETRY_BASE_DELAY", "3"))

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
        InlineKeyboardButton("📄 Extract to PDF", callback_data=f"pdfmenu:_:{message_id}")
    ])

    rows.append([
        InlineKeyboardButton("❌ Cancel", callback_data=f"cancel:_:{message_id}")
    ])

    return InlineKeyboardMarkup(rows)


def pdf_interval_keyboard(message_id: int) -> InlineKeyboardMarkup:
    row = [
        InlineKeyboardButton(f"{seconds}s", callback_data=f"pdf:{seconds}:{message_id}")
        for seconds in PDF_INTERVALS
    ]
    return InlineKeyboardMarkup([
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
        "-b:a", settings["audio_bitrate"],
        "-movflags", "+faststart",
        # Prevents an abrupt "Conversion failed!" at finalization if
        # the muxer's internal packet queue backs up on a tight
        # memory budget.
        "-max_muxing_queue_size", "1024",
        output_path,
    ]

    return command


async def compress_video(
    input_path: str,
    output_path: str,
    level: str,
    total_duration: float | None = None,
    progress_callback=None,
) -> None:
    """
    Compress a video with FFmpeg. If total_duration and a
    progress_callback are supplied, streams real encode progress
    (based on how much of the video's timeline has been processed)
    to the callback as it happens.
    """

    settings = LEVELS[level]
    command = build_ffmpeg_command(input_path, output_path, settings, enable_progress=True)

    logger.info("Running FFmpeg (%s): %s", level, " ".join(command))

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

    logger.info("FFmpeg compression completed.")


async def extract_pdf_frames(
    input_path: str,
    work_dir: Path,
    interval: int,
) -> tuple[Path | None, int]:
    """
    Grab one frame every `interval` seconds and combine them into a
    single PDF (one frame per page). Returns (pdf_path, frame_count) —
    pdf_path is None if extraction failed outright. No cap on frame
    count: a long video at a short interval will produce a large PDF.
    """

    frames_dir = work_dir / "frames"
    frames_dir.mkdir(exist_ok=True)

    pattern = str(frames_dir / "frame_%04d.jpg")

    command = [
        "ffmpeg", "-y",
        "-i", input_path,
        "-vf", f"fps=1/{interval}",
        "-q:v", "3",
        pattern,
    ]

    returncode, stdout, stderr = await run_command(command)

    if returncode != 0:
        logger.error("PDF frame extraction failed:\n%s", stderr.decode(errors="replace"))
        return None, 0

    frame_paths = sorted(frames_dir.glob("frame_*.jpg"))

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
) -> tuple[list[Path], list[Path], float]:
    """
    Walk frames in order, dropping any frame that's extremely similar
    to the last frame that was kept. If threshold is None (the normal
    case), one is computed automatically for this specific video via
    compute_auto_threshold. Returns (kept_paths, removed_paths,
    threshold_used) — the threshold is returned so it can be shown to
    the user for transparency/sanity-checking.
    """

    if len(frame_paths) < 2:
        return list(frame_paths), [], threshold if threshold is not None else DEDUP_SIMILARITY_THRESHOLD

    signature_semaphore = asyncio.Semaphore(ESTIMATE_CONCURRENCY)

    async def bounded_signature(path: Path) -> bytes | None:
        async with signature_semaphore:
            return await compute_frame_signature(str(path))

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
    user_id = update.effective_user.id if update.effective_user else None

    if not is_allowed(user_id):
        await reject_unauthorized(update.message, user_id)
        return

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
) -> None:
    message = update.effective_message

    if message is None:
        return

    chat_id = message.chat_id

    # A new video replaces whatever we were holding open for a
    # previous PDF session in this chat — that's the only other
    # trigger (besides "Finish session") that deletes a stored video.
    open_session_message_id = open_pdf_sessions.get(chat_id)
    if open_session_message_id is not None:
        clear_pending(open_session_message_id)

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

        telegram_file = await with_retries(context.bot.get_file, file_id)
        await with_retries(telegram_file.download_to_drive, custom_path=str(input_path))

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
                "duration": duration,
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
    duration: float | None = None,
) -> None:
    global active_compressions

    level_info = LEVELS[level]
    output_path = work_dir / "compressed.mp4"

    try:
        # Queue: only MAX_CONCURRENT_COMPRESSIONS compressions actually
        # encode at once. If others are already running, say so up
        # front instead of leaving the user staring at 0%.
        if active_compressions >= MAX_CONCURRENT_COMPRESSIONS:
            queue_text = (
                f"🕓 {active_compressions} compression(s) already in progress — "
                "you're queued and will start automatically..."
            )
            if status_message is not None:
                await with_retries(status_message.edit_text, queue_text)
            else:
                status_message = await with_retries(
                    context.bot.send_message, chat_id=chat_id, text=queue_text
                )

        async with compression_semaphore:
            active_compressions += 1
            try:
                text = f"⚙️ Compressing at {level_info['label']}...\n{render_progress_bar(0)}"

                if status_message is not None:
                    await with_retries(status_message.edit_text, text)
                else:
                    status_message = await with_retries(context.bot.send_message, chat_id=chat_id, text=text)

                await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.UPLOAD_VIDEO)

                progress_state = {
                    "last_percent": -10.0,
                    "last_edit": 0.0,
                    "start_time": time.monotonic(),
                }

                async def on_progress(percent: float) -> None:
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
                        f"⚙️ Compressing at {level_info['label']}...\n"
                        f"{render_progress_bar(percent)}{eta_text}"
                    )

                    try:
                        await status_message.edit_text(bar_text)
                    except Exception:
                        # A rate-limit hiccup or "message not modified" here
                        # shouldn't abort the actual compression.
                        pass

                await compress_video(
                    str(input_path),
                    str(output_path),
                    level,
                    total_duration=duration,
                    progress_callback=on_progress if duration else None,
                )
            finally:
                active_compressions -= 1

        compressed_size = output_path.stat().st_size

        if original_size:
            saved = original_size - compressed_size
            percentage = (saved / original_size) * 100
        else:
            percentage = 0

        # Probe the actual compressed file for duration/dimensions
        # (rather than reusing the source video's numbers) and grab
        # a thumbnail frame — without these, Telegram clients show
        # a blank 00:00 preview even though the video plays fine.
        output_duration = await get_duration(str(output_path))
        output_width, output_height = await get_video_dimensions(str(output_path))

        thumbnail_path = work_dir / "thumb.jpg"
        has_thumbnail = await generate_thumbnail(str(output_path), str(thumbnail_path), output_duration)

        await status_message.edit_text("📤 Sending compressed video...")

        await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.UPLOAD_VIDEO)

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
        if source_message_id in pending_compressions:
            clear_pending(source_message_id)
        else:
            # "redo" builds a fresh work_dir that was never registered
            # in pending_compressions, so just clean it up directly.
            cleanup_work_dir(work_dir)


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

        await query.edit_message_text("🔎 Estimating size at each compression level...")

        estimates = await estimate_all_levels(str(input_path), duration, work_dir)
        default_level = get_default_level(chat_id)

        lines = [f"Original size: {format_size(pending['original_size'])}", ""]
        for level_key in LEVEL_ORDER:
            info = LEVELS[level_key]
            est = estimates.get(level_key)
            lines.append(f"{info['label']} — ~{format_size(est)} · {info['detail']}")

        lines.append("")
        lines.append("Estimates are based on a short sample and may vary ±15%.")
        lines.append("Pick a level to compress:")

        await query.edit_message_text(
            "\n".join(lines),
            reply_markup=estimate_keyboard(message_id, estimates, default_level),
        )
        return

    if action == "pdffinish":
        clear_pending(message_id)
        await query.edit_message_text(
            "✅ Session finished — the stored video and temporary files were deleted."
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

        await run_compression(
            update, context,
            input_path=pending["input_path"],
            work_dir=pending["work_dir"],
            original_size=pending["original_size"],
            level=level,
            chat_id=pending["chat_id"],
            source_message_id=message_id,
            status_message=query.message,
            duration=pending.get("duration"),
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
