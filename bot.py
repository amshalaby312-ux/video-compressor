import os
import sys
import signal
import json
import asyncio
import logging
import shutil
import tempfile
import time
import contextlib
import contextvars
import inspect
import re
import itertools
import hashlib
import io
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

import img2pdf
from telegram import (
    Bot,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.constants import ChatAction
from telegram.error import BadRequest, NetworkError, TimedOut
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    TypeHandler,
    ApplicationHandlerStop,
    filters,
)

# ============================================================
# TEXTS — every message and button the bot shows, in one place.
# Edit the wording / emojis here. Keep the {placeholders} exactly as they
# are (they're filled in by the bot): you may move them around or delete
# the words around them, but don't rename them.
#   MSG_...  = a message          BTN_...  = a button label
# Sections are named after the part of the bot that uses them.
# ============================================================
# ---- cancel button (while compressing) ----
MSG_COMPRESSION_CANCELLED = "🛑 Cancelled — nothing was sent."
MSG_CANCELLING_JOB = "Cancelling…"
MSG_TOO_LATE_TO_CANCEL = "Too late to cancel — it has already finished encoding."
MSG_NOT_YOUR_JOB = "This isn't your video."
MSG_STATS_CANCELLED = "🛑 Cancelled by users: {cancelled}"
MSG_COMPRESSION_CANCELLED_REFUNDED = "🛑 Cancelled — nothing was sent, and {size} was given back to your daily limit."
MSG_CANCELLED_FILES_REMOVED_REFUNDED = "❌ Cancelled — temporary files removed, and {size} was given back to your daily limit."

# ---- live job updates sent to the admin (one message per job, edited as it runs) ----
MSG_ADMIN_JOB_HEADER = "👤 User: {name} ({username}) · id {user_id}"
MSG_ADMIN_JOB_PROGRESS = "⚙️ Compressing {kind} — {percent:.0f}% complete"
MSG_ADMIN_JOB_DONE = "✅ Finished compressing {kind}"
MSG_ADMIN_JOB_CANCELLED = "🛑 Cancelled compressing {kind}{refund_note}"
MSG_ADMIN_JOB_FAILED = "❌ Failed compressing {kind}{refund_note}"
MSG_ADMIN_JOB_STOPPED = "⚠️ Stopped before finishing ({kind})"
MSG_ADMIN_JOB_REFUND_NOTE = " — {size} refunded"

# ---- quota status text ----
MSG_TODAY_YOU_VE_SENT_OF = (
    "{plan}\n"
    "📊 Today you've sent {size_used} of your {size_limit} daily limit — {size_left} left.\n"
    "It resets at 12:00 AM, in {reset_in}."
)
MSG_YOU_HAVE_NO_DAILY_LIMIT = "♾️ You have no daily limit."
MSG_NO_DAILY_LIMIT_RIGHT_NOW = (
    "{plan}\n"
    "♾️ No daily limit right now."
)

# ---- file too large text ----
MSG_THIS_FILE_IS_BUT_YOUR = "⛔ This file is {size_bytes}, but your {plan_get_tier} plan takes files up to {size_max_file_bytes}.{upgrade_hint}"

# ---- quota refusal text ----
MSG_THE_LIMIT_RESETS_EVERY_DAY = (
    "{first_line}\n"
    "\n"
    "The limit resets every day at 12:00 AM — in {reset_in}. See you then! 👋{upgrade_hint}"
)
MSG_YOU_VE_USED_ALL_OF = "⛔ You've used all of your {size_limit} daily limit."
MSG_THIS_FILE_IS_BUT_YOU = "⛔ This file is {size_bytes}, but you only have {size_left} left of your {size_limit} daily limit."

# ---- upgrade hint ----
MSG_WANT_MORE_SEE_PLANS_THEN = (
    "\n"
    "\n"
    "💎 Want more? See /plans, then use /subscribe.\n"
    "{dm_text}"
)

# ---- home text ----
MSG_HI_SEND_ME_A_VIDEO = (
    "👋 Hi {name}!\n"
    "\n"
    "🎬 I compress videos and audio — and show you a preview before anything is compressed.\n"
    "\n"
    "📖 How to use\n"
    "1️⃣ Send me a video or an audio file.\n"
    "2️⃣ I'll send a {SAMPLE_SECONDS:.0f}s sample and the estimated size at each level.\n"
    "3️⃣ Pick a level and an audio quality.\n"
    "4️⃣ Get your compressed file ✅\n"
    "\n"
    "💎 Subscriptions\n"
    "{plans}\n"
    "⚡ Each plan is served before the smaller ones: Pro → Plus → Free.\n"
    "\n"
    "{your_plan}\n"
    "\n"
    "👇 Pick an option:"
)
MSG_HOME_PLAN_LINE = "{emoji} {label} — {size_daily_mb}/day · up to {size_max_file_mb} per file"
MSG_RAM_CLEAN_CONFIRM = (
    "🧹 Clean everything and restart the bot?\n"
    "\n"
    "• {running} running job(s) will be cancelled (their daily limit is given back)\n"
    "• {menus} open menu(s) will be removed (their daily limit is given back)\n"
    "• All temporary files ({temp_size}) will be deleted\n"
    "• The bot restarts and is back in a few seconds\n"
    "\n"
    "Plans, daily limits, approved users and statistics are kept (a backup is saved first)."
)
BTN_YES_CLEAN_RESTART = "✅ Yes, clean & restart"
MSG_RAM_CLEAN_CANCELLED = "👍 Cancelled — nothing was changed."
MSG_RAM_CLEAN_WORKING = "🧹 Stopping jobs and deleting temporary files..."
MSG_RAM_CLEAN_DONE = (
    "✅ Cleaned: {jobs} job(s) cancelled, {menus} menu(s) removed, {files} item(s) deleted ({freed} freed).\n"
    "♻️ Restarting now — back in a few seconds."
)
MSG_RAM_CLEAN_TOO_SOON = "⏳ The bot has only just restarted. Try again in a minute."
MSG_RAM_CLEAN_BACK = "✅ The bot restarted and is running again."
MSG_RAM_TITLE = "🧠 RAM usage"
MSG_RAM_CONTAINER = "📦 Container: {used} of {limit}\n{bar}"
MSG_RAM_CONTAINER_NO_LIMIT = "📦 Container: {used} (no limit set)"
MSG_RAM_BOT = "🤖 Bot process: {size}"
MSG_RAM_FFMPEG = "🎞 FFmpeg ({count} running): {size}"
MSG_RAM_FFMPEG_IDLE = "🎞 FFmpeg: not running"
MSG_RAM_HOST = "🖥 Host machine: {available} free of {total}"
MSG_RAM_UNAVAILABLE = "⚠️ Couldn't read the memory usage on this system."
MSG_SUBSCRIPTIONS_COMING_SOON = "💎 Subscriptions aren't available yet — coming soon!"
MSG_HOME_LEFT_TODAY = "📊 Left today: {size_left} of {size_limit}"

# ---- usage page text ----
MSG_YOUR_USAGE = (
    "📊 Your usage\n"
    "\n"
    "{usage}"
)
MSG_BIGGEST_FILE_YOU_CAN_SEND = (
    "\n"
    "\n"
    "📁 Biggest file you can send: {size_max_file_bytes}"
)

# ---- about text ----
MSG_ABOUT_THIS_BOT_COMPRESSES_VIDEOS = (
    "ℹ️ About this bot\n"
    "\n"
    "🎬 Compresses videos at 5 levels — you get a short sample and the estimated size of each level before anything is compressed.\n"
    "🧠 Looks at your video's resolution, frame rate, codec and bitrate and recommends the best settings.\n"
    "🔊 Edits audio: volume boost, noise reduction, voice enhancement, or replace a video's audio.\n"
    "🎧 Compresses audio files too.\n"
    "🏷 Renames audio and sets artist / album / cover — with or without compression.\n"
    "📄 Turns a video into a PDF of its frames.\n"
    "\n"
    "🔒 Your files are deleted as soon as they're processed{idle}.\n"
    "💎 Free to use with daily limits — see Subscribe for more."
)

# ---- payment steps text ----
MSG_30_DAYS_PER_DAY_FILES = (
    "{plan_tier} — 30 days\n"
    "• {size_daily_mb} per day · files up to {size_max_file_mb}\n"
    "\n"
    "💳 How to pay:\n"
    "{how_to_pay}\n"
    "\n"
    "📸 Then send me a screenshot of the payment here, as a photo, and I'll pass it to the owner. You'll get a message here as soon as your plan is active.\n"
    "\n"
    "{dm_text}"
)

# ---- maybe handle payment proof ----
MSG_PAYMENT_PROOF_FROM_ID_WANTS = (
    "💳 Payment proof\n"
    "From: {name} ({username}) · id {id}\n"
    "Wants: {plan_tier} for 30 days\n"
    "Current plan: {plan_id}"
)
MSG_GOT_IT_I_VE_SENT = "✅ Got it! I've sent your screenshot to the owner. You'll get a message here as soon as your plan is active."
BTN_APPROVE = "✅ Approve {plan_plus}{mark_plus}"
BTN_APPROVE_2 = "✅ Approve {plan_pro}{mark_pro}"
BTN_REJECT = "❌ Reject"
MSG_I_COULDN_T_PASS_THAT = "❌ I couldn't pass that on. Please send it through the channel's Direct Messages: {REQUIRED_CHANNEL_URL}"

# ---- settings & menus ----
MSG_ORIGINAL = "🎵 Original"
MSG_KEEP_THE_AUDIO_UNTOUCHED_NO = "Keep the audio untouched (no compression)"
MSG_LOTS_OF_ROOM_TO_SHRINK = "🟢 lots of room to shrink"
MSG_SOME_ROOM_TO_SHRINK = "🟡 some room to shrink"
MSG_ALREADY_FAIRLY_COMPACT_MODEST_SAVING = "🟠 already fairly compact — modest savings only"
MSG_ALREADY_VERY_COMPACT_RECOMPRESSING_M = "🔴 already very compact — recompressing may barely help"
MSG_FREE = "Free"
MSG_PLUS = "Plus"
MSG_PRO = "Pro"
MSG_LOW = "🟢 Low"
MSG_BEST_QUALITY_BIGGEST_FILE = "Best quality, biggest file"
MSG_MEDIUM = "🟡 Medium"
MSG_BALANCED_DEFAULT = "Balanced (default)"
MSG_HIGH = "🟠 High"
MSG_SMALLER_FILE_VISIBLE_QUALITY_LOSS = "Smaller file, visible quality loss"
MSG_VERY_HIGH = "🔴 Very High"
MSG_MUCH_SMALLER_NOTICEABLY_SOFTER = "Much smaller, noticeably softer"
MSG_EXTREME = "⚫ Extreme"
MSG_SMALLEST_POSSIBLE_ROUGH_QUALITY = "Smallest possible, rough quality"
MSG_LOW_2 = "🔈 Low"
MSG_SMALLEST_AUDIO_NOTICEABLE_QUALITY_LO = "Smallest audio, noticeable quality loss"
MSG_MEDIUM_2 = "🔉 Medium"
MSG_BALANCED = "Balanced"
MSG_HIGH_2 = "🔊 High"
MSG_BEST_QUALITY_BIGGEST_AUDIO = "Best quality, biggest audio"
JOIN_TEXT = (
    "📢 To use this bot, please join our channel first.\n\n"
    "1️⃣ Tap the green button and join\n"
    "2️⃣ Come back here and tap “✅ I've joined”"
)
UPDATE_BUSY_TEXT = (
    "🔧 The bot is being updated right now, so I can't take new files at the moment.\n"
    "Please send yours again in a few minutes — sorry!"
)

# ---- flow header ----
MSG_AUDIO = "🎚️ {level_label} · audio: {audio_label}"
MSG_AUDIO_QUALITY = "🎧 Audio quality: {audio_label}"
MSG_JUST_EDITING_AUDIO_VIDEO_UNTOUCHED = "🎧 Just editing audio (video untouched) · quality: {audio_label}"

# ---- enhancement keyboard ----
MSG_NOISE_REDUCTION = "🔇 Noise reduction: {noise_reduction_choice}"
MSG_VOICE_ENHANCEMENT = "🎙 Voice enhancement: {voice_enhancement_choice}"
BTN_PREVIEW_NOISE_VOICE_BOTH = "🎧 Preview: noise / voice / both"
BTN_CONTINUE = "▶️ Continue"

# ---- audio meta text ----
MSG_AUDIO_DETAILS_ALL_OPTIONAL_NAME = (
    "✏️ Audio details (all optional)\n"
    "\n"
    "📝 Name: {out_name}\n"
    "🎤 Artist: {artist}\n"
    "💿 Album: {album}\n"
    "🖼 Cover: {cover_path_choice}\n"
    "\n"
    "Tap a button, then send the new value. When you're done, pick the quality in the menu above — or tap \"Send as is\" to keep the sound untouched."
)

# ---- access request text ----
MSG_NAME = "👤 Name: {name}"
MSG_USERNAME = "🔗 Username: {username}"
MSG_ID = "🆔 ID: {user_id}"
MSG_ACCESS_REQUEST = "🔔 Access request"
MSG_ACCESS_REQUEST_DECIDED = "🔔 Access request — decided"

# ---- plans text ----
MSG_PLANS = "📦 Plans"
MSG_YOUR_PLAN = "Your plan: {plan}"
MSG_TO_SUBSCRIBE_SEND_SUBSCRIBE_PAY = "To subscribe: send /subscribe, pay, and send me a screenshot of the payment."
MSG_DAY_FILES_UP_TO = "{emoji} {label} — {size_daily_mb}/day · files up to {size_max_file_mb} · {perk}"

# ---- subscribe page text ----
MSG_WHICH_PLAN_DO_YOU_WANT = (
    "\n"
    "\n"
    "👇 Which plan do you want? (30 days)"
)

# ---- subs command ----
MSG_ACTIVE_SUBSCRIBERS = "💎 Active subscribers: {subscribers}"
MSG_UNTIL_D_LEFT = "{plan_tier} · {name} ({user_id}) · until {date_until} · {days_left}d left"
MSG_NO_ACTIVE_SUBSCRIBERS = "No active subscribers."

# ---- join keyboard ----
BTN_JOIN_THE_CHANNEL = "📢 Join the channel"
BTN_I_VE_JOINED = "✅ I've joined"

# ---- stats overview text ----
MSG_BOT_STATISTICS_TIMES_IN_UTC = "📊 Bot statistics (times in UTC)"
MSG_SINCE_USER_S = "Since {since} · 👥 {count_users} user(s)"
MSG_FINISHED_JOBS = "✅ Finished jobs: {jobs}"
MSG_VIDEO_COMPRESSIONS = "   🎬 Video compressions: {video}"
MSG_AUDIO_COMPRESSIONS = "   🎧 Audio compressions: {audio}"
MSG_AUDIO_EDITS_AUDIO_REPLACED = "   🔊 Audio edits: {audio_edit} · 🔁 Audio replaced: {audio_replace}"
MSG_FAILED = "❌ Failed: {failed}"
MSG_FILES_RECEIVED_VIDEO_AUDIO = "📥 Files received: {received_video} video · {received_audio} audio"
MSG_JOBS_TODAY_LAST_7_DAYS = "📅 Jobs today: {jobs} · last 7 days: {jobs_2}"
MSG_TODAY_PERSON_S_SENT_FILES = "🎟 Today: {quota_today_summary} person(s) sent files · {size} in total"
MSG_MORE_STATS_USERS_STATS_CSV = "More: /stats users · /stats csv"
MSG_COMPRESSION = "💾 Compression"
MSG_ORIGINAL_TOTAL = "   Original total: {size_orig}"
MSG_COMPRESSED_TOTAL = "   Compressed total: {size_comp}"
MSG_SPACE_SAVED_OVERALL = "   Space saved: {size_orig_comp} ({pct_text_overall_pct} overall)"
MSG_AVERAGE_SAVING_PER_FILE = "   Average saving per file: {pct_text_avg_pct}"
MSG_BEST_WORST = "   Best: {pct_text_best_saved_pct} · Worst: {pct_text_worst_saved_pct}"
MSG_BIGGEST_FILE_RECEIVED = "   Biggest file received: {size_max_original_bytes}"
MSG_TIME = "⏱ Time"
MSG_MEDIA_PROCESSED_LONG = "   Media processed: {time_media_seconds} long"
MSG_TOTAL_ENCODING_TIME = "   Total encoding time: {time_encode_seconds}"
MSG_AVERAGE_PER_JOB_REAL_TIME = "   Average per job: {time_encode_seconds_jobs} (~{speed:.1f}× real time)"
MSG_AVERAGE_QUEUE_WAIT = "   Average queue wait: {time_wait_seconds_jobs}"
MSG_BY_LEVEL = "🎚 By level"
MSG_BUSIEST_HOUR_00_59_UTC = "🕒 Busiest hour: {peak:02d}:00–{peak:02d}:59 UTC ({hours_peak} job(s))"
MSG_LAST_JOBS_NEWEST_FIRST = "🧾 Last jobs (newest first)"
MSG_JOB_S_SAVED_AVG_ENCODE = "   {label}: {jobs} job(s) · saved {pct_text_level_pct} · avg encode {time_encode_seconds_jobs}"
MSG_STATS_OVERVIEW_TEXT = "{size_original_bytes} → {size_compressed_bytes} (−{saved_pct:.1f}%)"
MSG_STATS_OVERVIEW_TEXT_2 = "• {when} · {value} · {level} · {size_part}{enc_part}"

# ---- stats users text ----
MSG_USER_S_HAVE_FINISHED_AT = "👥 {count_users} user(s) · {active} have finished at least one job"
MSG_AND_MORE = "…and {len_users} more"

# ---- smart analysis text ----
MSG_SMART_ANALYSIS = "🧠 Smart analysis"
MSG_RECOMMENDED_AUDIO = "✅ Recommended: {level_info_label} + audio {audio_info_label}"
MSG_SMART_ANALYSIS_TEXT = "📹 {width}×{height} · {fps_text} · {codec} · {bitrate_text_video_bps}"
MSG_COULDN_T_READ_THIS_VIDEO = "📹 Couldn't read this video's properties — using balanced defaults"
MSG_BITS_PER_PIXEL_AS_H = "📊 {eq_bpp:.3f} bits per pixel (as H.264) → {smart_verdict_text_verdict}"
MSG_SMART_ANALYSIS_TEXT_2 = "   ≈ {size_est_size}{saved}"
MSG_SMART_ANALYSIS_TEXT_3 = "• {note}"
MSG_SMART_ANALYSIS_TEXT_4 = "⚠️ {warning}"
MSG_SMART_ANALYSIS_TEXT_5 = "🔊 {audio_codec} · {audio_bps_choice} · {channels}"
MSG_NO_AUDIO_TRACK = "🔇 No audio track"

# ---- analyze and show level menu ----
MSG_ORIGINAL_SIZE = "Original size: {size_original_size}"
MSG_ESTIMATES_ARE_BASED_ON_THE = "Estimates are based on the {sample_label} samples and may vary ±15%."
MSG_PICK_A_LEVEL_TO_COMPRESS = "Pick a level to compress:"
MSG_ENCODING_A_SAMPLE_AT_EACH = (
    "🔎 Encoding a {sample_label} sample at each compression level (0/{count_level_order} done)...\n"
    "This can take a minute or two 🦦"
)
MSG_ANALYZE_AND_SHOW_LEVEL_MENU = "{label} — ~{size_estimates_level} · {detail}"

# ---- update status text ----
MSG_UPDATE_MODE_IS_ON_NEW = "🔧 Update mode is ON — new files are turned away."
MSG_UPDATE_OFF_CANCEL_AND_ACCEPT = "/update off — cancel and accept files again"
MSG_NOBODY_IS_IN_THE_MIDDLE = "✅ Nobody is in the middle of anything."
MSG_STILL_TO_FINISH = "👥 {people_left} still to finish:"
MSG_BEING_DOWNLOADED_ANALYZED_COMPRESSED = "   ⚙️ {work_active} being downloaded / analyzed / compressed / sent"
MSG_WAITING_IN_LINE = "   🕓 {waiting} waiting in line"
MSG_I_LL_MESSAGE_YOU_THE = "I'll message you the moment they're all done."
MSG_PERSON_PEOPLE_HAVE_AN_ANALYZED = "📋 {menus} person/people have an analyzed video open but haven't picked yet. They can still finish, but anything unfinished is lost when the bot restarts."
MSG_PDF_JOB_S_RUNNING = "   🧾 {other_jobs_running} PDF job(s) running"

# ---- maybe notify update ready ----
MSG_EVERYONE_HAS_FINISHED_IT_S = "✅ Everyone has finished — it's safe to deploy now."
MSG_UPDATE_OFF_CANCELS_UPDATE_MODE = "/update off cancels update mode."
MSG_THE_LATEST_DATABASE_ZIP_IS = "💾 The latest Database.zip is saved in the backup channel."
MSG_I_COULDN_T_CONFIRM_THE = "⚠️ I couldn't confirm the Database.zip backup — check the backup channel before deploying."
MSG_OPEN_MENU_S_WILL_BE = "⚠️ {menus} open menu(s) will be lost on restart — those people will need to resend."

# ---- handle callback inner ----
MSG_SCANNING_THE_VIDEO_FOR_CHANGES = "📸 Scanning the video for changes (sampling every {interval:.1f}s)..."
MSG_SESSION_FINISHED_THE_STORED_VIDEO = "✅ Session finished — the stored video and temporary files were deleted."
MSG_REJECTED_THE_PAYMENT_FROM = "❌ Rejected the payment from {name} ({target_id})."
MSG_FRAME_S_ONE_EVERY_S = "📄 {frame_count} frame(s), one every {interval}s."
MSG_SAMPLED_FRAME_S_COMPARING_FOR = "🔍 Sampled {count_candidate_paths} frame(s) — comparing for real changes..."
MSG_FRAME_S_KEPT_OUT_OF = (
    "📄 {count_kept_paths} frame(s) kept out of {count_candidate_paths} sampled (every {interval:.1f}s).\n"
    "(auto threshold: {threshold_used:.0f})"
)
MSG_NOT_AUTHORIZED_YOUR_TELEGRAM_USER = "⛔ Not authorized. Your Telegram user ID is {user_id}."
MSG_APPROVED_FOR_UNTIL = "✅ Approved: {plan_level} {verb} for {name} ({target_id}) until {date_until}."
MSG_STATISTICS_RESET_COUNTING_FROM_ZERO = "✅ Statistics reset — counting from zero now."
MSG_DEFAULT_STAR_SET_TO_NEW = (
    "Default star set to {level_label}.\n"
    "New estimate menus will highlight this level."
)
MSG_CANCELLED_TEMPORARY_FILES_REMOVED = "❌ Cancelled — temporary files removed."
MSG_PICK_A_SCREENSHOT_INTERVAL_ONE = "Pick a screenshot interval — one frame will be captured every N seconds and combined into a PDF:"
MSG_EXTRACTING_A_FRAME_EVERY_S = "📄 Extracting a frame every {interval}s and building a PDF..."
MSG_COMPARING_FRAMES_FOR_NEAR_DUPLICATES = "🔍 Comparing frames for near-duplicates..."
MSG_JUST_EDITING_THE_AUDIO_THE = (
    "🎧 Just editing the audio — the video stays exactly as it is (no re-encode).\n"
    "Pick audio quality:"
)
MSG_SEND_THE_NEW_FILE_NAME = "📝 Send the new file name now (just the name, no extension)."
MSG_SEND_THE_ALBUM_NAME_NOW = "💿 Send the album name now. (Send - to remove it.)"
MSG_SEND_THE_ARTIST_NAME_NOW = "🎤 Send the artist name now. (Send - to remove it.)"
MSG_UPLOADING_AUDIO_AS_IS = "📤 Uploading your audio..."
MSG_SENDING_AUDIO_AS_IS = "📤 Applying your changes — the sound itself is not re-encoded..."
MSG_AUDIO_SENT_AS_IS = "✅ Sent without compression — the sound is exactly as it was."
MSG_ARTIST = (
    "🎤 Artist: {custom_artist}\n"
)
MSG_COVER_NOT_EMBEDDED = "ℹ️ This format can't hold an embedded cover, so it's only shown as the preview image."
MSG_SENT_AS_FILE_NOT_MP3_M4A = "ℹ️ Telegram only plays MP3 / M4A as music, so this one was sent as a file (still untouched)."
MSG_SEND_THE_ALBUM_COVER_NOW = "🖼 Send the album cover now — a photo or an image file."
MSG_OPTIONAL_AUDIO_ENHANCEMENTS_TOGGLE_T = (
    "{header}\n"
    "🎛 Optional audio enhancements — toggle, then continue:"
)
MSG_GENERATING_S_ENHANCEMENT_PREVIEWS_ON = "🎧 Generating {ENHANCEMENT_PREVIEW_SECONDS}s enhancement previews — one moment..."
MSG_NOISE_REDUCTION_2 = "🔇 Noise reduction"
MSG_VOICE_ENHANCEMENT_2 = "🎙 Voice enhancement"
MSG_BOTH_TOGETHER = "🔇 + 🎙 Both together"
MSG_NOISE_REDUCTION_3 = "🔇 noise reduction"
MSG_VOICE_ENHANCEMENT_3 = "🎙 voice enhancement"
MSG_GENERATING_S_PREVIEWS_AT_EACH = "🎧 Generating {VOLUME_PREVIEW_SECONDS}s previews at each volume level{enhancement_note} — one moment..."
MSG_SELECTED_NOW_PICK_AUDIO_QUALITY = "🎚️ {level_settings_label} selected. Now pick audio quality:"
MSG_CANCELLED_THE_STATISTICS_WERE_NOT = "Cancelled — the statistics were not changed."
MSG_THAT_VIDEO_ISN_T_IN_2 = "⚠️ That video isn't in my cache anymore — please resend it."
MSG_NO_EXTRACTED_FRAMES_TO_COMPARE = "⚠️ No extracted frames to compare yet — extract a PDF first."
MSG_NOTHING_TO_COMPARE_NOT_ENOUGH = "Nothing to compare — not enough frames."
MSG_GETTING_SAMPLES_READY = "🔎 Getting samples ready..."
MSG_THIS_VIDEO_HAS_NO_AUDIO = "⚠️ This video has no audio track, so there's nothing to edit. Pick a compression level above instead."
MSG_SEND_ME_THE_NEW_AUDIO = (
    "🔁 Send me the new audio now — a voice message, an audio file, or an audio document.\n"
    "\n"
    "It replaces the video's current audio; the picture stays exactly as it is. The video decides the final length: a longer recording is cut at the end of the video, a shorter one leaves the rest silent."
)
MSG_AUDIO_REPLACEMENT_CANCELLED_PICK_ANO = "❌ Audio replacement cancelled — pick another option from the menu above."
MSG_S_PREVIEW_OF_THE_SAME = "🎧 {sample_duration:.0f}s preview of the same moment, from the middle:"
MSG_S_PREVIEW_AT_EACH_VOLUME = "🎧 {sample_duration:.0f}s preview at each volume level{enhancement_note}, from the middle:"
MSG_PICK_A_VOLUME_LEVEL = "Pick a volume level:"
MSG_A_COPY_OF_THE_STATISTICS = "🗂 A copy of the statistics from before the reset."
MSG_WHAT_WOULD_YOU_LIKE_TO = (
    "What would you like to do next?\n\n"
    "✂️ To delete slides, just send their page numbers, e.g. 2 5 11 16 (ranges like 3-6 work too)."
)
MSG_DELETE_SLIDES_OUT_OF_RANGE = "⚠️ The PDF has {total} page(s) — these numbers don't exist: {bad}. Nothing was deleted."
MSG_DELETE_SLIDES_WOULD_EMPTY = "⚠️ That would delete every page. Send fewer numbers."
MSG_DELETE_SLIDES_WORKING = "✂️ Deleting {count} slide(s) and rebuilding the PDF..."
MSG_DELETE_SLIDES_DONE = (
    "✂️ Deleted slide(s) {deleted} — {remaining} page(s) left.\n"
    "Page numbers in this new PDF start again from 1."
)
MSG_DELETE_SLIDES_FAILED = "❌ Something went wrong deleting those slides.\n\nError: {error}"
MSG_REMOVED_NEAR_DUPLICATE_FRAME_S = (
    "🧹 Removed {count_removed_paths} near-duplicate frame(s) — {count_kept_paths} remain.\n"
    "(auto threshold: {threshold_used:.0f})"
)
MSG_AUDIO_REPLACEMENT_CANCELLED = "❌ Audio replacement cancelled."
MSG_SEND_ME_THE_IMAGE_TO = (
    "🖼 Send me the image to use as this video's cover — a photo, or an image file if you want to keep full quality.\n"
    "\n"
    "Then pick what to do with the video from the menu above."
)
MSG_CANCELLED = "❌ Cancelled."
MSG_I_COULDN_T_CONFIRM_YOUR = (
    "❌ I couldn't confirm your payment, so your plan wasn't activated.\n"
    "Please contact the owner. {dm_text}"
)
MSG_COULDN_T_EXTRACT_ANY_FRAMES = "❌ Couldn't extract any frames from that video."
MSG_SOMETHING_WENT_WRONG_EXTRACTING_FRAM = (
    "❌ Something went wrong extracting frames.\n"
    "\n"
    "Error: {error}"
)
MSG_SOMETHING_WENT_WRONG_SCANNING_THE = (
    "❌ Something went wrong scanning the video.\n"
    "\n"
    "Error: {error}"
)
MSG_NO_EXTREMELY_SIMILAR_FRAMES_FOUND = (
    "✅ No extremely-similar frames found — nothing removed.\n"
    "(auto threshold: {threshold_used:.0f})"
)
MSG_SOMETHING_WENT_WRONG_REMOVING_SIMILA = (
    "❌ Something went wrong removing similar frames.\n"
    "\n"
    "Error: {error}"
)
MSG_SAVED_NOW_PICK_THE_QUALITY = (
    "\n"
    "\n"
    "✅ Saved — now pick the quality in the menu above, or tap Send as is."
)
MSG_COULDN_T_GENERATE_THE_PREVIEW = "⚠️ Couldn't generate the preview for {label}."
MSG_COULDN_T_GENERATE_A_PREVIEW = "⚠️ Couldn't generate a preview for {label}."
MSG_HANDLE_CALLBACK_INNER = "🔊 {label}"
MSG_VOLUME_SAMPLE_TITLE = "Audio sample {number}"
MSG_LEVEL_WOULD_GROW_FILE = "⛔ This level would make the file bigger than the original, so it's disabled. Pick a different level."
VOLUME_SAMPLE_PERFORMER = "صلي علي النبي"

# ---- reject unauthorized ----
MSG_YOU_RE_NOT_APPROVED_TO = (
    "⛔ You're not approved to use this bot yet.\n"
    "\n"
    "I've sent your request to the admin — you'll get a message here when they decide."
)
MSG_YOUR_ACCESS_REQUEST_WAS_DECLINED = "🔴 Your access request was declined."
MSG_YOUR_REQUEST_IS_STILL_WAITING = "⏳ Your request is still waiting for the admin's approval. You'll get a message here as soon as they decide."
MSG_I_COULDN_T_REACH_THE = "⚠️ I couldn't reach the admin to ask for you right now — please try again later."

# ---- report forwarded channel id ----
MSG_THAT_CHANNEL_S_ID_IS = (
    "📢 That channel's ID is:\n"
    "{origin_chat_id}\n"
    "\n"
    "Set it as BACKUP_CHANNEL_ID (Railway variable, or in bot.py) and make sure the bot is an admin of the channel with the \"Post messages\" and \"Pin messages\" rights."
)

# ---- handle access decision ----
MSG_ACCESS_REVOKED = "Access revoked 🚫"
MSG_ACCESS_REVOKED_2 = "🚫 Access revoked"
MSG_YOUR_ACCESS_TO_THIS_BOT = "🔴 Your access to this bot was removed by the admin."
MSG_ONLY_THE_ADMIN_CAN_DO = "Only the admin can do this."
MSG_ACCEPTED = "Accepted ✅"
MSG_ACCEPTED_2 = "✅ Accepted"
MSG_GREEN_CARD_GRANTED_THE_ADMIN = (
    "🟢 Green card granted! The admin approved you — you can use the bot now.\n"
    "\n"
    "Send me a video or an audio recording to get started."
)
MSG_DENIED = "Denied ❌"
MSG_DENIED_2 = "❌ Denied"
MSG_YOUR_REQUEST_TO_USE_THIS = "🔴 Your request to use this bot was declined."
MSG_THAT_USER_IS_HARD_CODED = "That user is hard-coded in bot.py, so it can't be revoked here."

# ---- grant command ----
MSG_FOR_ACTIVE_UNTIL = (
    "✅ {plan_tier} {verb} for {name} ({user_id}).\n"
    "Active until {date_until} ({QUOTA_TZ})."
)
MSG_I_COULDN_T_MESSAGE_THEM = "(I couldn't message them — they may need to send me /start first.)"

# ---- revoke command ----
MSG_PLAN_ENDED_FOR_BACK_TO = "✅ Plan ended for {name} ({user_id}) — back to Free."
MSG_USAGE_REVOKE_USER_NUMERIC_ID = "Usage: /revoke <user>  (numeric ID or @username)"
MSG_HAS_NO_ACTIVE_PLAN = "{name} ({user_id}) has no active plan."

# ---- reset stats command ----
MSG_RESET_ALL_STATISTICS_THIS_ERASES = (
    "⚠️ Reset ALL statistics?\n"
    "\n"
    "This erases {totals_jobs} finished job(s), each user's job counts, the per-level numbers and the job history. I'll send you a copy of the data first.\n"
    "\n"
    "Not touched: the list of {count_users} user(s) (names, usernames, first/last seen), approved users, plans and today's daily data limits."
)
BTN_YES_RESET = "✅ Yes, reset"

# ---- estimate keyboard ----
MSG_ESTIMATE_KEYBOARD = "✅ {text}"
BTN_JUST_EDIT_AUDIO_KEEP_VIDEO = "🎧 Just edit audio (keep video as is)"
BTN_REPLACE_AUDIO_SEND_A_RECORDING = "🔁 Replace audio (send a recording)"
BTN_SET_VIDEO_COVER_SEND_AN = "🖼 Set video cover (send an image)"
BTN_EXTRACT_TO_PDF = "📄 Extract to PDF"
BTN_EXTRACT_AUDIO = "🎵 Extract audio"
MSG_EXTRACT_AUDIO_PICK_QUALITY = (
    "🎵 Extracting the audio from your video into an audio file.\n\n"
    "Pick the audio quality (estimated size of the audio file):"
)
MSG_EXTRACT_AUDIO_CANT_COPY = (
    "⚠️ This video's audio is in a format that can't be saved as a playable audio file without "
    "converting it. Pick an audio quality above instead."
)
# Audio formats that can be copied out of a video untouched, and the file they become.
EXTRACT_COPY_EXTENSIONS = {"aac": ".m4a", "mp3": ".mp3"}
MSG_EXTRACT_AUDIO_NO_AUDIO = "⚠️ This video has no audio track, so there's nothing to extract."
BTN_CANCEL = "❌ Cancel"

# ---- pdf interval keyboard ----
BTN_S = "{seconds}s"
BTN_AUTO_DETECT_CHANGES = "🧠 Auto-detect changes"

# ---- smart analyze ----
MSG_RESOLUTION = "Resolution {width}×{height} → {out_w}×{out_h}"
MSG_FPS_CAPPED_AT_FPS_SMALLER = "{fps:.0f} fps → capped at {SMART_FPS_CAP} fps (smaller file, slightly less smooth motion)"

# ---- setlevel ----
MSG_PICK_THE_LEVEL_THAT_SHOULD = "Pick the level that should be starred by default in the menu:"

# ---- run compression ----
MSG_AUDIO_EDITED_VIDEO_UNTOUCHED_AUDIO = "✅ Audio edited — video untouched! (audio: {audio_info_label}{boost_suffix}{enhancement_suffix})"
MSG_AUDIO_REPLACED_VIDEO_UNTOUCHED_AUDIO = "✅ Audio replaced — video untouched! (audio: {audio_info_label})"
MSG_COMPLETE_AUDIO = "✅ {smart_choice} complete! ({level_info_label}{fps_note}, audio: {audio_info_label}{boost_suffix}{enhancement_suffix})"
MSG_THAT_AUDIO_FORMAT_CAN_T = "ℹ️ That audio format can't be kept inside an MP4, so it was re-encoded at High quality."
MSG_RUN_COMPRESSION = " · 🔊+{volume_boost_percent}%"
MSG_COMPRESSING_AUDIO = "⚙️ Compressing audio ({audio_info_label}{boost_suffix}{enhancement_suffix})..."
MSG_SENDING_COMPRESSED_AUDIO = "📤 Sending compressed audio..."
MSG_SENDING_EDITED_VIDEO = "📤 Sending edited video..."
MSG_SENDING_COMPRESSED_VIDEO = "📤 Sending compressed video..."
MSG_EDITING_AUDIO_VIDEO_UNTOUCHED = "⚙️ Editing audio, video untouched ({audio_info_label}{boost_suffix}{enhancement_suffix})..."
MSG_SOMETHING_WENT_WRONG_WHILE_COMPRESSI = (
    "❌ Something went wrong while compressing.\n"
    "\n"
    "Error: {error}"
)
MSG_REPLACING_THE_AUDIO_VIDEO_UNTOUCHED = "⚙️ Replacing the audio, video untouched ({audio_info_label})..."
MSG_AT_AUDIO = "⚙️ {smart_choice} at {level_info_label}{fps_note} (audio: {audio_info_label}{boost_suffix}{enhancement_suffix})..."
MSG_COMPRESSION_COMPLETE_AUDIO = (
    "✅ Compression complete! (audio: {audio_info_label}{boost_suffix}{enhancement_suffix})\n"
    "\n"
)
MSG_ALBUM = (
    "💿 Album: {custom_album}\n"
    "\n"
)

# ---- process audio replacement ----
MSG_DOWNLOADING_THE_NEW_AUDIO = "📥 Downloading the new audio..."
MSG_YOUR_RECORDING_IS_LONGER_THAN = "ℹ️ Your recording ({time_replacement_duration}) is longer than the video ({time_video_duration}), so its last {time_difference} was cut."
MSG_THAT_VIDEO_ISN_T_IN = "⚠️ That video isn't in my cache anymore — please resend it, then tap Replace audio again."
MSG_YOUR_RECORDING_IS_SHORTER_THAN = "ℹ️ Your recording ({time_replacement_duration}) is shorter than the video ({time_video_duration}), so the last {time_difference} is silent."
MSG_I_COULDN_T_FIND_ANY = "❌ I couldn't find any audio in that file. Send a different recording (or tap Cancel above)."
MSG_COULDN_T_DOWNLOAD_THAT_AUDIO = (
    "❌ Couldn't download that audio — send it again.\n"
    "\n"
    "Error: {error}"
)

# ---- apply cover image ----
MSG_PROCESSING_THE_IMAGE = "🖼 Processing the image..."
MSG_THAT_FILE_ISN_T_IN = "⚠️ That file isn't in my cache anymore — please resend it."
MSG_I_COULDN_T_READ_THAT = "❌ I couldn't read that image. Send a different one (JPG / PNG / WebP)."
MSG_ALBUM_COVER_SET = "✅ Album cover set."
MSG_VIDEO_COVER_SET_NOW_PICK = (
    "✅ Video cover set.\n"
    "\n"
    "Pick what to do with the video from the menu above — the cover is used on the result — "
    "or upload the video exactly as it is with just the new cover:"
)
BTN_UPLOAD_VIDEO_UNCHANGED = "📤 Upload video unchanged (cover only)"
MSG_COVER_ONLY_UPLOADING = "📤 Uploading your video with the new cover (video unchanged)..."
MSG_COVER_CHANGED_VIDEO_UNCHANGED = "✅ Cover changed — video unchanged!"
MSG_COULDN_T_DOWNLOAD_THAT_IMAGE = (
    "❌ Couldn't download that image — send it again.\n"
    "\n"
    "Error: {error}"
)

# ---- users command ----
MSG_APPROVED_USER_S_RIGHT_NOW = "📋 {count_approved_users} approved user(s) right now. (To load a save manually, just send a Database.zip back to me.)"

# ---- usage keyboard ----
BTN_BACK = "⬅️ Back"
BTN_GO_BACK = "⬅️ Go back"
BTN_GET_MORE_SUBSCRIBE = "💎 Get more — Subscribe"

# ---- require channel membership ----
MSG_YOU_HAVEN_T_JOINED_YET = "You haven't joined yet — tap the green button first."
MSG_PLEASE_JOIN_OUR_CHANNEL_FIRST = "Please join our channel first."
MSG_THANKS_FOR_JOINING = "✅ Thanks for joining!"
MSG_YOU_RE_IN_SEND_ME = "✅ You're in! Send me a video (or an audio file) to get started."
MSG_I_CAN_T_CHECK_WHO = (
    "⚠️ I can't check who joined the required channel, so everyone is being let in.\n"
    "Add me as an ADMINISTRATOR of that channel (no special rights needed) and it will start working."
)

# ---- wait text ----
MSG_JOB_S_AHEAD_OF_YOU = "🕓 {ahead} job(s) ahead of you — I'll start yours automatically as soon as the server is free."
MSG_YOU_RE_SUBSCRIBED_SO_YOU = "{plan_label} — you're subscribed, so you're served before free users."
MSG_THE_COMPRESSION_RUNNING_NOW_HAS = "⏱ The compression running now has ~{time_remaining} left."
MSG_PLUS_AND_PRO_MEMBERS_ARE = "💡 Plus and Pro members are served first (see /plans)."

# ---- volume keyboard ----
MSG_NO_BOOST = "🔈 No boost"
MSG_VOLUME_KEYBOARD = "🔊 +{boost}%"

# ---- post pdf keyboard ----
BTN_COMPRESS_VIDEO = "🗜 Compress video"
BTN_FINISH_SESSION_DELETES_VIDEO = "✅ Finish session (deletes video)"
BTN_AUTO_DELETE_SIMILAR_FRAMES = "🧹 Auto-delete similar frames"

# ---- send level samples ----
MSG_SAMPLE_AT_EACH_COMPRESSION_LEVEL = "🎬 {sample_length_duration} sample at each compression level (taken from the middle of the video, with that level's default audio):"
MSG_SAMPLE_FULL_VIDEO = (
    "{label} — {detail}\n"
    "Sample: {size_sample_size} · full video ≈ {size_estimates_level}"
)

# ---- on progress ----
MSG_ENCODING_A_SAMPLE_AT_EACH_2 = (
    "🔎 Encoding a {sample_label} sample at each compression level ({done}/{total} done)...\n"
    "This can take a minute or two on a small CPU."
)

# ---- handle incoming ----
MSG_DOWNLOADING = "📥 Downloading {kind_label} ({size_text})..."
MSG_ORIGINAL_SIZE_PICK_AUDIO_QUALITY = (
    "🎧 Original size: {size_downloaded_size}\n"
    "Pick audio quality:"
)
MSG_SOMETHING_WENT_WRONG_WHILE_ANALYZING = (
    "❌ Something went wrong while analyzing the {kind_label}.\n"
    "\n"
    "Error: {error}"
)

# ---- handle photo ----
MSG_I_M_WAITING_FOR_TEXT = "✏️ I'm waiting for text here, not an image."

# ---- handle document ----
MSG_THAT_DOESN_T_LOOK_LIKE = "❌ That doesn't look like a video or audio file."

# ---- update command ----
MSG_UPDATE_MODE_IS_OFF_NEW = "✅ Update mode is OFF — new files are accepted again."
MSG_UPDATE_MODE_WASN_T_ON = "Update mode wasn't on."

# ---- access request keyboard ----
BTN_REVOKE_ACCESS = "🚫 Revoke access"
BTN_ACCEPT_INSTEAD = "✅ Accept instead"
BTN_ACCEPT_AGAIN = "✅ Accept again"
BTN_ACCEPT = "✅ Accept"
BTN_DENY = "❌ Deny"

# ---- pin backup file ----
MSG_I_POSTED_DATABASE_ZIP_IN = "⚠️ I posted Database.zip in the channel but couldn't pin it. Give the bot the \"Pin messages\" admin right there — the pinned file is what I restore from after a restart."

# ---- notify user granted ----
MSG_YOU_RE_NOW_ON_THE = (
    "🎉 You're now on the {plan_tier} plan until {date_until}!\n"
    "• {size_daily_mb} per day\n"
    "• files up to {size_max_file_mb}\n"
    "• you're served before free users in the queue"
)

# ---- home keyboard ----
BTN_USAGE = "📊 Usage"
BTN_SUBSCRIBE = "💎 Subscribe"
BTN_ABOUT_THE_BOT = "ℹ️ About the bot"

# ---- about keyboard ----
BTN_OUR_CHANNEL = "📢 Our channel"

# ---- stats command ----
MSG_NO_FINISHED_JOBS_YET = "No finished jobs yet."
MSG_THE_LAST_JOB_S = "🧾 The last {count_recent} job(s)."

# ---- audio keyboard ----
BTN_AUDIO_KEYBOARD = "{original_info_label} — ~{size_estimates_original_audio} · {original_info_detail}"
BTN_RENAME_ALBUM_NAME_COVER = "✏️ Rename / album name / cover"

# ---- audio meta keyboard ----
BTN_RENAME_FILE = "📝 Rename file"
BTN_ALBUM_NAME = "💿 Album name"
BTN_ARTIST_NAME = "🎤 Artist name"
BTN_SEND_AS_IS = "📤 Send as is (no compression)"
BTN_ALBUM_COVER = "🖼 Album cover"
BTN_CLEAR_ALL = "🗑 Clear all"
BTN_DONE = "✅ Done"

# ---- handle text input ----
MSG_THAT_NAME_HAS_NO_USABLE = "❌ That name has no usable characters — try another one."
MSG_I_M_WAITING_FOR_AN = "🖼 I'm waiting for an image here — send a photo or an image file."

# ---- restore approved users from document ----
MSG_LOADED_THAT_FILE_NEW_USER = "✅ Loaded that file — {added} new user(s) added, {count_approved_users} approved in total."
MSG_I_COULDN_T_USE_THAT = "❌ I couldn't use that file: {error}"
MSG_SOMETHING_WENT_WRONG_READING_THAT = (
    "❌ Something went wrong reading that file.\n"
    "\n"
    "Error: {error}"
)
MSG_COULDN_T_SAVE_TO_DISK = (
    "\n"
    "⚠️ Couldn't save to disk on the server."
)

# ---- backup database if changed ----
MSG_DATABASE_BACKUP_APPROVED_USER_S = "💾 Database backup — {count} approved user(s), {totals_jobs} job(s) logged — {stamp}"
MSG_I_COULDN_T_POST_DATABASE = (
    "⚠️ I couldn't post Database.zip to the backup channel ({BACKUP_CHANNEL_ID}): {error}\n"
    "Make sure the bot is an admin there with the \"Post messages\" and \"Pin messages\" rights. I'll keep retrying every few minutes."
)

# ---- restore from pinned backup ----
MSG_BOT_RESTARTED_RESTORED_APPROVED_USER = "♻️ Bot restarted — restored {added} approved user(s) from the pinned Database.zip ({count_approved_users} approved in total)."

# ---- expire idle files ----
MSG_I_DELETED_YOUR_VIDEO_BECAUSE = "⌛ I deleted your video because you didn't choose anything for {IDLE_FILE_TTL_MINUTES} minutes. Send it again whenever you're ready."
MSG_I_DELETED_YOUR_VIDEO_REFUNDED = "⌛ I deleted your video because you didn't choose anything for {IDLE_FILE_TTL_MINUTES} minutes, and {size} was given back to your daily limit. Send it again whenever you're ready."
MSG_EVICTED_MAKE_ROOM = "⌛ I had to delete your video to make room for other people's files. Send it again whenever you're ready."
MSG_EVICTED_MAKE_ROOM_REFUNDED = "⌛ I had to delete your video to make room for other people's files, and {size} was given back to your daily limit. Send it again whenever you're ready."


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
#      IDs are saved in Database.zip, and every few minutes (if anything
#      changed) the bot posts a fresh Database.zip in the private BACKUP
#      CHANNEL and PINS it.
#
# Railway wipes local files on every redeploy (no Volume needed here):
# when the bot starts it downloads the Database.zip pinned in the backup
# channel and restores from it, so the pinned file IS the backup.
# ------------------------------------------------------------
# The one and only admin. Deliberately NOT read from an environment variable.
ADMIN_USER_ID = 940770584

# True = everyone can use the bot (no approval needed). Set the OPEN_ACCESS
# variable to 0 in Railway to bring the approval system back.
OPEN_ACCESS = os.getenv("OPEN_ACCESS", "1").strip().lower() not in ("0", "false", "no")

# The private channel where Database.zip is posted + pinned. The bot must
# be an ADMIN there with the "Post messages" and "Pin messages" rights.
# Override with the BACKUP_CHANNEL_ID variable in Railway if needed.
# While it's 0 nothing is backed up (the local file is still written).
BACKUP_CHANNEL_ID = int(os.getenv("BACKUP_CHANNEL_ID", "-1002891277206") or 0)

# How often (seconds) the bot checks for changes and re-uploads Database.zip.
BACKUP_INTERVAL_SECONDS = int(os.getenv("BACKUP_INTERVAL_SECONDS", "300"))

# Local copy of the save file (wiped by Railway on redeploy — the pinned
# copy in the channel is the real backup).
DATABASE_FILE = Path(
    os.getenv("DATABASE_FILE", str(Path(__file__).resolve().parent / "Database.zip"))
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

    if OPEN_ACCESS:
        return True

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
    """Write Database.zip to disk (atomically). Returns False if it couldn't."""
    try:
        DATABASE_FILE.parent.mkdir(parents=True, exist_ok=True)
        temp_path = DATABASE_FILE.with_suffix(".zip.tmp")
        temp_path.write_bytes(build_database_zip())
        os.replace(temp_path, DATABASE_FILE)
        return True
    except OSError:
        logger.exception("Could not save %s", DATABASE_FILE)
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
    """Load the saved Database.zip at startup (missing file = empty list)."""
    if not DATABASE_FILE.exists():
        logger.info("No %s yet — starting with no approved users.", DATABASE_FILE)
        return

    try:
        read_database_bytes(DATABASE_FILE.read_bytes())
        logger.info("Loaded %d approved user(s) from %s", len(approved_users), DATABASE_FILE)
    except (OSError, ValueError):
        logger.exception("Couldn't read %s — starting with no approved users.", DATABASE_FILE)


def access_request_text(user_id: int, info: dict, status: str | None = None) -> str:
    lines = [
        MSG_ACCESS_REQUEST if status is None else MSG_ACCESS_REQUEST_DECIDED,
        "",
        MSG_NAME.format(name=info.get('name') or 'Unknown'),
        MSG_USERNAME.format(username=format_username(info.get('username'))),
        MSG_ID.format(user_id=user_id),
    ]

    if status:
        lines += ["", status]

    return "\n".join(lines)


def access_request_keyboard(user_id: int, state: str = "pending") -> InlineKeyboardMarkup:
    """state: 'pending' (Accept/Deny), 'accepted' (Revoke), 'denied' (Accept instead)."""
    if state == "accepted":
        rows = [[InlineKeyboardButton(BTN_REVOKE_ACCESS, callback_data=f"access:revoke:{user_id}")]]
    elif state == "denied":
        rows = [[InlineKeyboardButton(BTN_ACCEPT_INSTEAD, callback_data=f"access:accept:{user_id}")]]
    elif state == "revoked":
        rows = [[InlineKeyboardButton(BTN_ACCEPT_AGAIN, callback_data=f"access:accept:{user_id}")]]
    else:
        rows = [[
            InlineKeyboardButton(BTN_ACCEPT, callback_data=f"access:accept:{user_id}"),
            InlineKeyboardButton(BTN_DENY, callback_data=f"access:deny:{user_id}"),
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
            await message.reply_text(MSG_YOUR_ACCESS_REQUEST_WAS_DECLINED)
        return

    if user_id in pending_access_requests:
        if should_notify_user():
            await message.reply_text(
                MSG_YOUR_REQUEST_IS_STILL_WAITING
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
                MSG_I_COULDN_T_REACH_THE
            )
        return

    pending_access_requests[user_id] = {**info, "requested_at": now}
    _last_access_notice[user_id] = now

    await message.reply_text(
        MSG_YOU_RE_NOT_APPROVED_TO
    )


# ------------------------------------------------------------
# Database.zip — the save file pinned in the backup channel
#
# Every BACKUP_INTERVAL_SECONDS the bot checks whether the data changed
# since the last upload. If it did, it posts a fresh Database.zip in the
# backup channel, pins it and unpins the old one. On startup the pinned
# Database.zip is downloaded and restored. To save more data in future,
# add another file inside build_database_zip() and read it back in
# read_database_bytes().
# ------------------------------------------------------------
DATABASE_ZIP_FILENAME = "Database.zip"
USERS_ENTRY_NAME = "approved_users.json"
STATS_ENTRY_NAME = "stats.json"
MAX_DATABASE_ENTRY_BYTES = 10 * 1024 * 1024

_last_backed_up_fingerprint: str | None = None
_backup_lock = asyncio.Lock()
_backup_warning_sent = False


def build_database_zip() -> bytes:
    """The whole save as the bytes of a Database.zip."""
    buffer = io.BytesIO()

    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(USERS_ENTRY_NAME, approved_users_json_bytes())
        archive.writestr(STATS_ENTRY_NAME, stats_json_bytes())
        archive.writestr(QUOTA_ENTRY_NAME, quota_json_bytes())
        archive.writestr(SUBS_ENTRY_NAME, subscriptions_json_bytes())
        archive.writestr(REFERRALS_ENTRY_NAME, referrals_json_bytes())

    return buffer.getvalue()


def database_fingerprint() -> str:
    """Changes only when the saved data changes (zip bytes carry timestamps, so hash the data)."""
    return hashlib.sha256(
        approved_users_json_bytes() + b"|" + stats_json_bytes() + b"|" + quota_json_bytes()
        + b"|" + subscriptions_json_bytes() + b"|" + referrals_json_bytes()
    ).hexdigest()


def read_database_bytes(raw: bytes) -> int:
    """
    Merge a save into memory. Accepts a Database.zip, or an old
    approved_users .json. Returns how many NEW users were added.
    Raises ValueError if the content isn't usable.
    """
    if not zipfile.is_zipfile(io.BytesIO(raw)):
        return merge_approved_users(raw)

    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            try:
                entry = archive.getinfo(USERS_ENTRY_NAME)
            except KeyError:
                raise ValueError(f"the zip has no {USERS_ENTRY_NAME} inside") from None

            if entry.file_size > MAX_DATABASE_ENTRY_BYTES:
                raise ValueError("the zip's contents are too large")

            added = merge_approved_users(archive.read(entry))

            if STATS_ENTRY_NAME in archive.namelist():
                try:
                    merge_stats_bytes(archive.read(STATS_ENTRY_NAME))
                except ValueError:
                    logger.warning("Database.zip has an unreadable %s — skipping the stats.", STATS_ENTRY_NAME)

            if QUOTA_ENTRY_NAME in archive.namelist():
                try:
                    merge_quota_bytes(archive.read(QUOTA_ENTRY_NAME))
                except ValueError:
                    logger.warning("Database.zip has an unreadable %s — skipping the daily limits.", QUOTA_ENTRY_NAME)

            if SUBS_ENTRY_NAME in archive.namelist():
                try:
                    merge_subscriptions_bytes(archive.read(SUBS_ENTRY_NAME))
                except ValueError:
                    logger.warning("Database.zip has an unreadable %s — skipping the subscriptions.", SUBS_ENTRY_NAME)

            if REFERRALS_ENTRY_NAME in archive.namelist():
                try:
                    merge_referrals_bytes(archive.read(REFERRALS_ENTRY_NAME))
                except ValueError:
                    logger.warning("Database.zip has an unreadable %s — skipping the invites.", REFERRALS_ENTRY_NAME)

            return added
    except zipfile.BadZipFile as error:
        raise ValueError(f"not a valid zip ({error})") from error


async def warn_admin_once(bot, text: str) -> None:
    """Tell the admin about a backup problem once (not every 5 minutes)."""
    global _backup_warning_sent

    if _backup_warning_sent:
        return

    _backup_warning_sent = True

    try:
        await bot.send_message(chat_id=ADMIN_USER_ID, text=text)
    except Exception:
        logger.warning("Couldn't send the backup warning to the admin")


async def backup_database_if_changed(bot, force: bool = False) -> bool:
    """Upload + pin a fresh Database.zip if the data changed. Returns True if it uploaded."""
    global _last_backed_up_fingerprint, _backup_warning_sent

    if not BACKUP_CHANNEL_ID:
        return False

    async with _backup_lock:
        fingerprint = database_fingerprint()

        if not force and fingerprint == _last_backed_up_fingerprint:
            return False

        save_approved_users()
        count = len(approved_users)
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

        try:
            sent = await with_retries(
                bot.send_document,
                chat_id=BACKUP_CHANNEL_ID,
                document=build_database_zip(),
                filename=DATABASE_ZIP_FILENAME,
                caption=(
                    MSG_DATABASE_BACKUP_APPROVED_USER_S.format(count=count, totals_jobs=stats['totals']['jobs'], stamp=stamp)
                ),
                disable_notification=True,
            )
        except Exception as error:
            logger.exception("Couldn't post Database.zip to channel %s", BACKUP_CHANNEL_ID)
            await warn_admin_once(
                bot,
                MSG_I_COULDN_T_POST_DATABASE.format(BACKUP_CHANNEL_ID=BACKUP_CHANNEL_ID, error=error),
            )
            return False

        _last_backed_up_fingerprint = fingerprint
        pinned = await pin_backup_file(bot, sent)

        if pinned:
            _backup_warning_sent = False

        logger.info("Database.zip backed up to channel %s (%d user(s)).", BACKUP_CHANNEL_ID, count)
        return True


async def pin_backup_file(bot, sent_message) -> bool:
    """
    Pin the freshly posted Database.zip, then unpin the previous one so
    the pinned message is always the newest save. The new one is pinned
    FIRST, so a failure can never leave the channel with no pinned backup.
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
            and (old_document.file_name or "").lower().endswith((".zip", ".json"))
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
        logger.exception("Couldn't pin Database.zip in %s", BACKUP_CHANNEL_ID)
        await warn_admin_once(
            bot,
            MSG_I_POSTED_DATABASE_ZIP_IN,
        )
        return False

    if old_message_id is not None:
        try:
            await bot.unpin_chat_message(chat_id=BACKUP_CHANNEL_ID, message_id=old_message_id)
        except Exception:
            logger.warning("Couldn't unpin the previous backup (message %s)", old_message_id)

    return True


async def restore_from_pinned_backup(bot) -> None:
    """
    At startup: download the Database.zip pinned in the backup channel and
    restore it. Railway wipes local files on every redeploy, so this is
    what brings the data back.
    """
    global _last_backed_up_fingerprint

    if not BACKUP_CHANNEL_ID:
        return

    Path(TEMP_DIR).mkdir(parents=True, exist_ok=True)
    temp_path = Path(TEMP_DIR) / "pinned_backup.bin"
    had_data_before = bool(approved_users) or _stats_activity(stats) > 0

    try:
        chat = await bot.get_chat(BACKUP_CHANNEL_ID)
        pinned = getattr(chat, "pinned_message", None)
        document = getattr(pinned, "document", None) if pinned is not None else None

        if document is None:
            logger.info("No pinned Database.zip in channel %s — nothing to restore.", BACKUP_CHANNEL_ID)
            return

        telegram_file = await with_retries(bot.get_file, document.file_id)
        await with_retries(telegram_file.download_to_drive, custom_path=str(temp_path))

        added = read_database_bytes(temp_path.read_bytes())
        save_approved_users()

        # Fresh server (no local data before) => memory now equals the pinned
        # file, so there's no need to upload an identical copy.
        if not had_data_before:
            _last_backed_up_fingerprint = database_fingerprint()

        logger.info(
            "Restored from the pinned Database.zip: %d new user(s), %d approved in total.",
            added, len(approved_users),
        )

        if added:
            await bot.send_message(
                chat_id=ADMIN_USER_ID,
                text=(
                    MSG_BOT_RESTARTED_RESTORED_APPROVED_USER.format(added=added, count_approved_users=len(approved_users))
                ),
            )
    except Exception:
        logger.exception("Couldn't restore from the pinned Database.zip.")
    finally:
        temp_path.unlink(missing_ok=True)


async def backup_loop(bot) -> None:
    """Every BACKUP_INTERVAL_SECONDS: upload a new Database.zip if anything changed."""
    while True:
        await asyncio.sleep(BACKUP_INTERVAL_SECONDS)

        try:
            await backup_database_if_changed(bot)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Backup check failed")


async def post_init(application: Application) -> None:
    if os.environ.get(RESTART_ENV_FLAG):
        with contextlib.suppress(Exception):
            await application.bot.send_message(chat_id=ADMIN_USER_ID, text=MSG_RAM_CLEAN_BACK)

    swept = sweep_temp_dir()
    if swept:
        logger.info("Removed %d leftover item(s) from %s", swept, TEMP_DIR)

    await restore_from_pinned_backup(application.bot)
    application.bot_data["backup_task"] = asyncio.create_task(backup_loop(application.bot))
    application.bot_data["idle_task"] = asyncio.create_task(idle_cleanup_loop(application.bot))


async def post_stop(application: Application) -> None:
    """On shutdown/redeploy: stop the loop and make one last upload if something changed."""
    for key in ("backup_task", "idle_task"):
        task = application.bot_data.pop(key, None)

        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    try:
        await asyncio.wait_for(backup_database_if_changed(application.bot), timeout=8)
    except Exception:
        logger.warning("Final backup before shutdown didn't finish.")


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
        MSG_THAT_CHANNEL_S_ID_IS.format(origin_chat_id=origin_chat.id)
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
        await query.answer(MSG_ONLY_THE_ADMIN_CAN_DO, show_alert=True)
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

        await query.answer(MSG_ACCEPTED)
        await update_request_message(MSG_ACCEPTED_2, "accepted")
        await tell_user(
            MSG_GREEN_CARD_GRANTED_THE_ADMIN
        )
        # Database.zip is re-uploaded to the backup channel by the 5-minute loop.
        return

    if decision == "deny":
        pending_access_requests.pop(user_id, None)
        denied_users.add(user_id)

        await query.answer(MSG_DENIED)
        await update_request_message(MSG_DENIED_2, "denied")
        await tell_user(MSG_YOUR_REQUEST_TO_USE_THIS)
        return

    # revoke
    if user_id in ALLOWED_USER_IDS or user_id == ADMIN_USER_ID:
        await query.answer(
            MSG_THAT_USER_IS_HARD_CODED,
            show_alert=True,
        )
        return

    approved_users.pop(user_id, None)
    denied_users.add(user_id)

    save_approved_users()

    await query.answer(MSG_ACCESS_REVOKED)
    await update_request_message(MSG_ACCESS_REVOKED_2, "revoked")
    await tell_user(MSG_YOUR_ACCESS_TO_THIS_BOT)
    # Database.zip is re-uploaded to the backup channel by the 5-minute loop.


def _read_text(path: str) -> str | None:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return handle.read()
    except OSError:
        return None


def _read_number(path: str) -> int | None:
    text = _read_text(path)

    if text is None:
        return None

    text = text.strip()

    if not text.isdigit():
        return None   # e.g. "max" = no limit

    value = int(text)
    return None if value >= (1 << 60) else value   # cgroup v1 shows "unlimited" as a huge number


def _proc_rss_bytes(status_text: str | None) -> int:
    """VmRSS (in bytes) out of a /proc/<pid>/status file."""
    for line in (status_text or "").splitlines():
        if line.startswith("VmRSS:"):
            parts = line.split()
            if len(parts) >= 2 and parts[1].isdigit():
                return int(parts[1]) * 1024

    return 0


def memory_snapshot() -> dict | None:
    """Current RAM numbers, read straight from Linux (no extra packages). None if unreadable."""
    result: dict = {}

    # What the container is actually using, and its limit (cgroup v2, then v1).
    used = _read_number("/sys/fs/cgroup/memory.current")
    limit = _read_number("/sys/fs/cgroup/memory.max")
    inactive_file_key = "inactive_file"
    stat_text = _read_text("/sys/fs/cgroup/memory.stat")

    if used is None:
        used = _read_number("/sys/fs/cgroup/memory/memory.usage_in_bytes")
        limit = _read_number("/sys/fs/cgroup/memory/memory.limit_in_bytes")
        stat_text = _read_text("/sys/fs/cgroup/memory/memory.stat")
        inactive_file_key = "total_inactive_file"

    if used is not None:
        # Cached file data is handed back when needed, so it isn't counted (same as `docker stats`).
        for line in (stat_text or "").splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[0] == inactive_file_key and parts[1].isdigit():
                used = max(0, used - int(parts[1]))
                break

        result["container_used"] = used
        result["container_limit"] = limit

    # The whole machine (on shared hosting this is bigger than the container's share).
    meminfo = _read_text("/proc/meminfo")
    if meminfo:
        values = {}
        for line in meminfo.splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[1].isdigit():
                values[parts[0].rstrip(":")] = int(parts[1]) * 1024

        if "MemTotal" in values:
            result["host_total"] = values["MemTotal"]
            result["host_available"] = values.get("MemAvailable", values.get("MemFree", 0))

    result["bot_rss"] = _proc_rss_bytes(_read_text("/proc/self/status"))

    # FFmpeg processes started by the bot.
    ffmpeg_count = 0
    ffmpeg_rss = 0
    try:
        for entry in os.listdir("/proc"):
            if not entry.isdigit():
                continue

            comm = _read_text(f"/proc/{entry}/comm")
            if comm and comm.strip() == "ffmpeg":
                ffmpeg_count += 1
                ffmpeg_rss += _proc_rss_bytes(_read_text(f"/proc/{entry}/status"))
    except OSError:
        pass

    result["ffmpeg_count"] = ffmpeg_count
    result["ffmpeg_rss"] = ffmpeg_rss

    if "container_used" not in result and "host_total" not in result:
        return None

    return result


# Set by /ram_clean: once the bot has shut down cleanly, main() starts it again.
RESTART_REQUESTED = False
RESTART_ENV_FLAG = "BOT_RESTARTED_AT"


def dir_size_bytes(path: Path) -> int:
    total = 0

    try:
        for item in path.rglob("*"):
            with contextlib.suppress(OSError):
                if item.is_file():
                    total += item.stat().st_size
    except OSError:
        pass

    return total


def kill_stray_ffmpeg() -> None:
    """Stop any FFmpeg/FFprobe still running (so nothing keeps writing to deleted files)."""
    try:
        for entry in os.listdir("/proc"):
            if not entry.isdigit() or int(entry) == os.getpid():
                continue

            comm = _read_text(f"/proc/{entry}/comm")
            if comm and comm.strip() in ("ffmpeg", "ffprobe"):
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.kill(int(entry), signal.SIGKILL)
    except OSError:
        pass


async def ram_clean_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/ram_clean (admin only): asks for confirmation, then clears everything and restarts."""
    message = update.effective_message
    user_id = update.effective_user.id if update.effective_user else None

    if message is None or not is_admin(user_id):
        return

    running = sum(1 for job in running_jobs.values() if not job.get("done"))
    menus = max(0, len(pending_compressions) - running)

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton(BTN_YES_CLEAN_RESTART, callback_data="ramclean:yes:0"),
        InlineKeyboardButton(BTN_CANCEL, callback_data="ramclean:no:0"),
    ]])

    await message.reply_text(
        MSG_RAM_CLEAN_CONFIRM.format(
            running=running,
            menus=menus,
            temp_size=format_size(dir_size_bytes(Path(TEMP_DIR))),
        ),
        reply_markup=keyboard,
    )


async def perform_ram_clean(query) -> None:
    """The confirmed part of /ram_clean. Never leaves the bot half-cleaned: it always ends in a restart."""
    global RESTART_REQUESTED

    # Guard against a restart loop (e.g. the confirmation tap being delivered again).
    restarted_at = os.environ.get(RESTART_ENV_FLAG)
    if restarted_at:
        try:
            if time.time() - float(restarted_at) < 45:
                await query.edit_message_text(MSG_RAM_CLEAN_TOO_SOON)
                return
        except ValueError:
            pass

    await query.edit_message_text(MSG_RAM_CLEAN_WORKING)

    temp_before = dir_size_bytes(Path(TEMP_DIR))

    # 1. Cancel running jobs the normal way (their quota is refunded and the people are told).
    jobs_cancelled = 0
    cancelled_ids: list[int] = []
    for source_id, job in list(running_jobs.items()):
        if job.get("done"):
            continue

        job["cancelled"] = True
        jobs_cancelled += 1
        cancelled_ids.append(source_id)
        process = job.get("process")

        if process is not None and process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                process.kill()

    # Give them a moment to wind down: refund, "cancelled" message and file cleanup.
    # (A job leaves active_job_ids only at the very end of all that.)
    for _ in range(30):
        if not any(source_id in active_job_ids for source_id in cancelled_ids):
            break
        await asyncio.sleep(0.5)

    # 2. Open menus: give the daily limit back, then remove their files.
    menus_removed = 0
    for message_id in list(pending_compressions):
        if message_id in active_job_ids:
            continue   # still being handled by a job (it refunds / cleans up itself)

        entry = pending_compressions.get(message_id) or {}
        owner_id = entry.get("owner_id")
        charged = int(entry.get("charged_bytes") or 0)

        if owner_id is not None and charged > 0:
            quota_refund(owner_id, charged)

        clear_pending(message_id)
        menus_removed += 1

    # 3. Anything else on disk.
    kill_stray_ffmpeg()
    files_deleted = menus_removed + sweep_temp_dir()

    await with_retries(
        query.edit_message_text,
        MSG_RAM_CLEAN_DONE.format(
            jobs=jobs_cancelled,
            menus=menus_removed,
            files=files_deleted,
            freed=format_size(temp_before),
        ),
    )

    # 4. Stop cleanly (post_stop saves a last backup), then main() starts the bot again.
    RESTART_REQUESTED = True
    asyncio.get_running_loop().call_later(1.0, os.kill, os.getpid(), signal.SIGTERM)


async def ram_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/ram (admin only): how much memory the bot is using right now."""
    message = update.effective_message
    user_id = update.effective_user.id if update.effective_user else None

    if message is None or not is_admin(user_id):
        return

    snapshot = memory_snapshot()

    if snapshot is None:
        await message.reply_text(MSG_RAM_UNAVAILABLE)
        return

    lines = [MSG_RAM_TITLE, ""]

    used = snapshot.get("container_used")
    limit = snapshot.get("container_limit")

    if used is not None and limit:
        lines.append(
            MSG_RAM_CONTAINER.format(
                used=format_size(used),
                limit=format_size(limit),
                bar=render_progress_bar(used / limit * 100),
            )
        )
    elif used is not None:
        lines.append(MSG_RAM_CONTAINER_NO_LIMIT.format(used=format_size(used)))

    lines.append(MSG_RAM_BOT.format(size=format_size(snapshot["bot_rss"])))

    if snapshot["ffmpeg_count"]:
        lines.append(
            MSG_RAM_FFMPEG.format(count=snapshot["ffmpeg_count"], size=format_size(snapshot["ffmpeg_rss"]))
        )
    else:
        lines.append(MSG_RAM_FFMPEG_IDLE)

    if "host_total" in snapshot:
        lines.append(
            MSG_RAM_HOST.format(
                available=format_size(snapshot["host_available"]),
                total=format_size(snapshot["host_total"]),
            )
        )

    await message.reply_text("\n".join(lines))


async def users_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/users (admin only): send the current Database.zip on demand."""
    message = update.effective_message
    user_id = update.effective_user.id if update.effective_user else None

    if message is None or not is_admin(user_id):
        return

    await context.bot.send_document(
        chat_id=ADMIN_USER_ID,
        document=build_database_zip(),
        filename=DATABASE_ZIP_FILENAME,
        caption=MSG_APPROVED_USER_S_RIGHT_NOW.format(count_approved_users=len(approved_users)),
    )


# ------------------------------------------------------------
# Daily data limit: each person can send DAILY_LIMIT_MB of files per day
# (the total size of everything they send — a single 500 MB video uses it all).
# It resets at 12:00 AM in QUOTA_TIMEZONE (Egypt time by default). Usage is
# saved inside Database.zip (quota.json), so restarting the bot can't be used
# to get a fresh allowance.
# ------------------------------------------------------------
DAILY_LIMIT_MB = float(os.getenv("DAILY_LIMIT_MB", "300"))      # 0 = no limit for anyone
DAILY_LIMIT_BYTES = int(DAILY_LIMIT_MB * 1024 * 1024)

# Telegram user IDs with NO daily limit (and no need to join the channel). Add more IDs here.
UNLIMITED_USER_IDS: set[int] = {
    ADMIN_USER_ID,
    # 123456789,
}

QUOTA_ENTRY_NAME = "quota.json"

try:
    from zoneinfo import ZoneInfo

    QUOTA_TZ = ZoneInfo(os.getenv("QUOTA_TIMEZONE", "Africa/Cairo"))
except Exception:
    # No timezone database in this image: use fixed Egypt winter time (UTC+2).
    # (Add `tzdata` to requirements.txt to get the real, daylight-saving-aware one.)
    QUOTA_TZ = timezone(timedelta(hours=2))
    logger.warning("Timezone database not found — daily limit resets at midnight UTC+2.")

daily_usage: dict = {"date": "", "used": {}}   # used: user id (text) -> bytes sent today


def quota_now() -> datetime:
    return datetime.now(QUOTA_TZ)


def quota_today() -> str:
    return quota_now().strftime("%Y-%m-%d")


def _quota_rollover() -> None:
    """New day (after 12 AM): everyone starts fresh."""
    today = quota_today()

    if daily_usage["date"] != today:
        daily_usage["date"] = today
        daily_usage["used"] = {}


def is_unlimited(user_id: int | None) -> bool:
    return user_id in UNLIMITED_USER_IDS


def quota_applies(user_id: int | None) -> bool:
    return user_id is not None and not is_unlimited(user_id) and daily_limit_bytes(user_id) > 0


def quota_used_bytes(user_id: int) -> int:
    _quota_rollover()
    return int(daily_usage["used"].get(str(user_id), 0))


def quota_allows(user_id: int | None, size_bytes: int) -> bool:
    """Would a file of this size fit in what's left of today's allowance?"""
    if not quota_applies(user_id):
        return True

    return quota_used_bytes(user_id) + max(0, size_bytes) <= daily_limit_bytes(user_id)


def quota_consume(user_id: int | None, size_bytes: int) -> None:
    if not quota_applies(user_id) or size_bytes <= 0:
        return

    _quota_rollover()
    daily_usage["used"][str(user_id)] = quota_used_bytes(user_id) + size_bytes


def quota_refund(user_id: int | None, size_bytes: int) -> None:
    """Give the bytes back (the file couldn't be processed)."""
    if not quota_applies(user_id) or size_bytes <= 0:
        return

    _quota_rollover()
    daily_usage["used"][str(user_id)] = max(0, quota_used_bytes(user_id) - size_bytes)


def quota_today_summary() -> tuple[int, int]:
    """(people who sent something today, total bytes they sent)."""
    if daily_usage["date"] != quota_today():
        return 0, 0

    amounts = [int(value) for value in daily_usage["used"].values()]
    return sum(1 for amount in amounts if amount > 0), sum(amounts)


def time_until_reset_text() -> str:
    now = quota_now()
    next_midnight = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    hours, remainder = divmod(int((next_midnight - now).total_seconds()), 3600)
    minutes = remainder // 60
    return f"{hours}h {minutes}m" if hours else f"{minutes}m"


def quota_status_text(user_id: int | None) -> str:
    if user_id is not None and is_unlimited(user_id):
        return MSG_YOU_HAVE_NO_DAILY_LIMIT

    limit = daily_limit_bytes(user_id)
    plan = plan_status_text(user_id)

    if limit <= 0:
        return MSG_NO_DAILY_LIMIT_RIGHT_NOW.format(plan=plan)

    used = quota_used_bytes(user_id)
    left = max(0, limit - used)
    return (
        MSG_TODAY_YOU_VE_SENT_OF.format(plan=plan, size_used=format_size(used), size_limit=format_size(limit), size_left=format_size(left), reset_in=time_until_reset_text())
    )


def file_too_large_text(user_id: int | None, size_bytes: int) -> str:
    return (
        MSG_THIS_FILE_IS_BUT_YOUR.format(size_bytes=format_size(size_bytes), plan_get_tier=tier_label(get_tier(user_id)), size_max_file_bytes=format_size(max_file_bytes(user_id)), upgrade_hint=upgrade_hint(user_id))
    )


def quota_refusal_text(user_id: int | None, size_bytes: int) -> str:
    limit = daily_limit_bytes(user_id)
    left = max(0, limit - (quota_used_bytes(user_id) if user_id is not None else 0))

    if left <= 0:
        first_line = MSG_YOU_VE_USED_ALL_OF.format(size_limit=format_size(limit))
    else:
        first_line = (
            MSG_THIS_FILE_IS_BUT_YOU.format(size_bytes=format_size(size_bytes), size_left=format_size(left), size_limit=format_size(limit))
        )

    return (
        MSG_THE_LIMIT_RESETS_EVERY_DAY.format(first_line=first_line, reset_in=time_until_reset_text(), upgrade_hint=upgrade_hint(user_id))
    )


def quota_json_bytes() -> bytes:
    return json.dumps(daily_usage, sort_keys=True, separators=(",", ":")).encode("utf-8")


def merge_quota_bytes(raw: bytes) -> bool:
    """Load today's usage from a Database.zip (older days are ignored). Raises ValueError if unusable."""
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"quota is not valid JSON ({error})") from error

    if not isinstance(data, dict) or not isinstance(data.get("used"), dict):
        raise ValueError("not a quota file")

    if data.get("date") != quota_today():
        return False

    _quota_rollover()
    changed = False

    for key, value in data["used"].items():
        try:
            amount = int(value)
        except (TypeError, ValueError):
            continue

        if amount > int(daily_usage["used"].get(str(key), 0)):
            daily_usage["used"][str(key)] = amount
            changed = True

    return changed


async def limit_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/limit: how much of today's data allowance is left."""
    message = update.effective_message
    user = update.effective_user

    if message is None or user is None:
        return

    await message.reply_text(quota_status_text(user.id))


# ------------------------------------------------------------
# Subscriptions (manual): the admin gives a plan with /grant. Plans change the
# daily data limit, the biggest file allowed, and the place in the queue.
# Saved inside Database.zip (subscriptions.json).
# ------------------------------------------------------------
SUBS_ENTRY_NAME = "subscriptions.json"
REFERRALS_ENTRY_NAME = "referrals.json"
SUBSCRIBE_CONTACT = os.getenv("SUBSCRIBE_CONTACT", "")   # e.g. "@yourusername" — shown to people who hit a limit

# Subscribe buttons stay switched off until this is set to "1" (env SUBSCRIPTIONS_ENABLED).
SUBSCRIPTIONS_ENABLED = os.getenv("SUBSCRIPTIONS_ENABLED", "0") == "1"

FREE_MAX_FILE_MB = float(os.getenv("FREE_MAX_FILE_MB", "200"))

# How people pay: the text you want shown to them (account numbers, prices...).
# Set PAYMENT_INSTRUCTIONS in Railway; use \n for a new line.
PAYMENT_INSTRUCTIONS = os.getenv("PAYMENT_INSTRUCTIONS", "").replace("\\n", "\n")

TIERS: dict[str, dict] = {
    "free": {"label": MSG_FREE, "emoji": "🆓", "daily_mb": DAILY_LIMIT_MB, "max_file_mb": FREE_MAX_FILE_MB, "priority": 0},
    "plus": {"label": MSG_PLUS, "emoji": "⭐", "daily_mb": 1536, "max_file_mb": 500, "priority": 1},
    "pro":  {"label": MSG_PRO,  "emoji": "💎", "daily_mb": 3072, "max_file_mb": 800, "priority": 2},
}

subscriptions: dict[str, dict] = {}   # user id (text) -> {"tier": "plus", "until": unix time}

# Invite rewards (/invite). Saved inside Database.zip (referrals.json).
referrals: dict = {
    "pending": {},    # new person (text id) -> who invited them; opened the bot via the link, not counted yet
    "progress": {},   # inviter (text id) -> [people counted since their last reward]
    "counted": {},    # everyone ever counted (text id) -> inviter, so nobody counts twice
    "rewards": {},    # inviter (text id) -> how many times they earned the reward
    "bonus": {},      # user (text id) -> {"tier": ..., "until": ...} free days; runs next to a paid plan
}


def tier_label(tier: str) -> str:
    return f"{TIERS[tier]['emoji']} {TIERS[tier]['label']}"


def _paid_subscription(user_id: int | None) -> dict | None:
    if user_id is None:
        return None

    entry = subscriptions.get(str(user_id))

    if entry is None:
        return None

    if float(entry.get("until", 0)) <= time.time() or entry.get("tier") not in TIERS:
        subscriptions.pop(str(user_id), None)   # expired: back to Free
        return None

    return entry


def _active_bonus(user_id: int | None) -> dict | None:
    """Free days earned with /invite."""
    if user_id is None:
        return None

    entry = referrals["bonus"].get(str(user_id))

    if entry is None:
        return None

    if float(entry.get("until", 0)) <= time.time() or entry.get("tier") not in TIERS:
        referrals["bonus"].pop(str(user_id), None)
        return None

    return entry


def active_subscription(user_id: int | None) -> dict | None:
    paid = _paid_subscription(user_id)
    bonus = _active_bonus(user_id)

    if paid is None:
        return bonus

    if bonus is None:
        return paid

    def rank(entry: dict) -> tuple:
        return (TIERS[entry["tier"]]["priority"], float(entry["until"]))

    return bonus if rank(bonus) > rank(paid) else paid


def get_tier(user_id: int | None) -> str:
    entry = active_subscription(user_id)
    return entry["tier"] if entry else "free"


def daily_limit_bytes(user_id: int | None) -> int:
    return int(TIERS[get_tier(user_id)]["daily_mb"] * 1024 * 1024)


def max_file_bytes(user_id: int | None) -> int:
    return int(TIERS[get_tier(user_id)]["max_file_mb"] * 1024 * 1024)


def file_too_large(user_id: int | None, size_bytes: int) -> bool:
    if user_id is None or is_unlimited(user_id) or TIERS[get_tier(user_id)]["max_file_mb"] <= 0:
        return False

    return size_bytes > max_file_bytes(user_id)


def user_priority(user_id: int | None) -> int:
    if user_id is not None and is_unlimited(user_id):
        return 99

    return TIERS[get_tier(user_id)]["priority"]


def user_plan_label(user_id: int | None) -> str | None:
    """Shown in the queue message — only for paying users."""
    if user_id is None or is_unlimited(user_id):
        return None

    tier = get_tier(user_id)
    return None if tier == "free" else tier_label(tier)


def format_until(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, QUOTA_TZ).strftime("%Y-%m-%d %H:%M")


def plan_status_text(user_id: int | None) -> str:
    entry = active_subscription(user_id)

    if entry is None:
        return f"{tier_label('free')} plan"

    return f"{tier_label(entry['tier'])} plan — active until {format_until(entry['until'])}"


def dm_text() -> str:
    extra = f" or message {SUBSCRIBE_CONTACT}" if SUBSCRIBE_CONTACT else ""
    return f"You can also message us directly through the Direct Messages of our channel{extra}: {REQUIRED_CHANNEL_URL}"


def upgrade_hint(user_id: int | None) -> str:
    tier = get_tier(user_id)

    if tier == "pro":
        return ""

    return MSG_WANT_MORE_SEE_PLANS_THEN.format(dm_text=dm_text())


def plans_text(user_id: int | None) -> str:
    lines = [MSG_PLANS, ""]

    for tier in ("free", "plus", "pro"):
        info = TIERS[tier]
        perk = {
            "free": "normal queue",
            "plus": "served before Free users",
            "pro": "served first",
        }[tier]
        lines.append(
            MSG_DAY_FILES_UP_TO.format(emoji=info['emoji'], label=info['label'], size_daily_mb=format_size(int(info['daily_mb'] * 1024 * 1024)), size_max_file_mb=format_size(int(info['max_file_mb'] * 1024 * 1024)), perk=perk)
        )

    lines += ["", MSG_YOUR_PLAN.format(plan=plan_status_text(user_id))]

    lines.append("")
    lines.append(MSG_TO_SUBSCRIBE_SEND_SUBSCRIBE_PAY)
    lines.append(dm_text())

    return "\n".join(lines)


def subscribers_summary() -> str:
    counts = {"plus": 0, "pro": 0}

    for user_id in list(subscriptions):
        entry = active_subscription(int(user_id))
        if entry:
            counts[entry["tier"]] += 1

    return f"{TIERS['plus']['label']} {counts['plus']} · {TIERS['pro']['label']} {counts['pro']}"


def subscriptions_json_bytes() -> bytes:
    return json.dumps(subscriptions, sort_keys=True, separators=(",", ":")).encode("utf-8")


def merge_subscriptions_bytes(raw: bytes) -> int:
    """Load subscriptions from a Database.zip; for each person the later expiry wins. Returns how many changed."""
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"subscriptions are not valid JSON ({error})") from error

    if not isinstance(data, dict):
        raise ValueError("not a subscriptions file")

    changed = 0

    for key, entry in data.items():
        if not isinstance(entry, dict) or entry.get("tier") not in TIERS or "until" not in entry:
            continue

        try:
            until = float(entry["until"])
        except (TypeError, ValueError):
            continue

        if until <= time.time():
            continue

        current = subscriptions.get(str(key))
        if current is None or until > float(current.get("until", 0)):
            subscriptions[str(key)] = {"tier": entry["tier"], "until": until}
            changed += 1

    return changed


def find_user_id(target: str) -> int | None:
    """A numeric ID, or an @username of someone the bot has seen."""
    target = target.strip()

    if target.lstrip("-").isdigit():
        return int(target)

    wanted = target.lstrip("@").lower()
    for user_id, entry in stats["users"].items():
        if (entry.get("username") or "").lower() == wanted:
            return int(user_id)

    return None


def display_name_for(user_id: int) -> str:
    entry = stats["users"].get(str(user_id))
    return entry["name"] if entry else str(user_id)


def grant_plan(user_id: int, tier: str, days: int) -> tuple[float, str]:
    """Give (or extend) a plan. Returns (expiry time, wording)."""
    current = _paid_subscription(user_id)

    if current is not None and current["tier"] == tier:
        until = float(current["until"]) + days * 86400      # same plan: add the days on top
        verb = f"extended by {days} days"
    else:
        until = time.time() + days * 86400                   # new or changed plan: starts now
        verb = f"granted for {days} days"

    subscriptions[str(user_id)] = {"tier": tier, "until": until}
    return until, verb


async def notify_user_granted(bot, user_id: int, tier: str, until: float) -> bool:
    info = TIERS[tier]

    try:
        await bot.send_message(
            chat_id=user_id,
            text=(
                MSG_YOU_RE_NOW_ON_THE.format(plan_tier=tier_label(tier), date_until=format_until(until), size_daily_mb=format_size(int(info['daily_mb'] * 1024 * 1024)), size_max_file_mb=format_size(int(info['max_file_mb'] * 1024 * 1024)))
            ),
        )
        return True
    except Exception:
        return False


# ------------------------------------------------------------
# /invite — every user gets a personal link. When INVITE_TARGET brand-new
# people open the bot through it AND are in the channel, the inviter gets
# INVITER_DAYS days of Pro and those people get INVITEE_DAYS day of Plus.
# It repeats: every group of 3 earns the reward again.
# ------------------------------------------------------------
INVITE_TARGET = 3
INVITER_TIER, INVITER_DAYS = "pro", 3
INVITEE_TIER, INVITEE_DAYS = "plus", 1
MAX_PENDING_INVITES = 5000
INVITE_MAX_REWARDS = 2   # each person can earn the reward at most this many times

MSG_INVITE_PAGE = (
    "🎁 Invite friends, get free Pro!\n\n"
    "Your personal link:\n{link}\n\n"
    "When {target} new people open the bot through it and join our channel:\n"
    "• you get {inviter_days} of {inviter_tier}\n"
    "• each of them gets {invitee_days} of {invitee_tier}\n\n"
    "📊 Progress: {progress}/{target}\n"
    "🏆 Rewards earned: {rewards}/{max_rewards}"
)
MSG_INVITE_LIMIT_REACHED = (
    "🏆 You've earned the invite reward {max_rewards}/{max_rewards} times — that's the maximum.\n"
    "Thanks for spreading the word! 💙"
)
MSG_INVITE_JOINED = "🎉 {name} joined through your link! Progress: {progress}/{target}"
MSG_INVITE_DONE_INVITER = (
    "🎁 {target}/{target} reached! You got {inviter_days} of {inviter_tier}.\n"
    "Your {target} friends got {invitee_days} of {invitee_tier}.\n\n"
    "You can earn this reward up to {max_rewards} times in total — rewards used: {rewards}/{max_rewards}."
)
MSG_INVITE_DONE_INVITEE = "🎁 {name}'s invite just reached {target}/{target} — you got {invitee_days} of {invitee_tier}!"
BTN_SHARE_INVITE = "📤 Share my link"
MSG_SHARE_INVITE_TEXT = "Free video & audio compressor on Telegram (مش هكر والله 😁❤️)"


def _days_text(days: int) -> str:
    return f"{days} day" + ("" if days == 1 else "s")


def referrals_json_bytes() -> bytes:
    return json.dumps(referrals, sort_keys=True, separators=(",", ":")).encode("utf-8")


def merge_referrals_bytes(raw: bytes) -> None:
    """Load invites from a Database.zip (adds what's missing; never removes anything)."""
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"referrals are not valid JSON ({error})") from error

    if not isinstance(data, dict):
        raise ValueError("not a referrals file")

    for key, inviter in (data.get("counted") or {}).items():
        referrals["counted"].setdefault(str(key), inviter)

    for key, inviter in (data.get("pending") or {}).items():
        if str(key) not in referrals["counted"]:
            referrals["pending"].setdefault(str(key), inviter)

    for inviter, people in (data.get("progress") or {}).items():
        mine = referrals["progress"].setdefault(str(inviter), [])
        for person in people if isinstance(people, list) else []:
            if person not in mine:
                mine.append(person)

    for inviter, amount in (data.get("rewards") or {}).items():
        try:
            referrals["rewards"][str(inviter)] = max(int(amount), int(referrals["rewards"].get(str(inviter), 0)))
        except (TypeError, ValueError):
            continue

    for key, entry in (data.get("bonus") or {}).items():
        try:
            until = float(entry["until"])
            tier = entry["tier"]
        except (TypeError, KeyError, ValueError):
            continue

        if tier not in TIERS or until <= time.time():
            continue

        current = referrals["bonus"].get(str(key))
        if current is None or until > float(current.get("until", 0)):
            referrals["bonus"][str(key)] = {"tier": tier, "until": until}

    while len(referrals["pending"]) > MAX_PENDING_INVITES:
        referrals["pending"].pop(next(iter(referrals["pending"])))


def grant_bonus(user_id: int, tier: str, days: int) -> float:
    """Free days that never downgrade or shorten anything. Returns the new expiry."""
    paid = _paid_subscription(user_id)

    # They already pay for this tier or a better one: just add the days to their paid plan.
    if paid is not None and TIERS[paid["tier"]]["priority"] >= TIERS[tier]["priority"]:
        until, _ = grant_plan(user_id, paid["tier"], days)
        return until

    current = _active_bonus(user_id)

    if current is not None and TIERS[current["tier"]]["priority"] >= TIERS[tier]["priority"]:
        current["until"] = float(current["until"]) + days * 86400
        return float(current["until"])

    until = time.time() + days * 86400
    referrals["bonus"][str(user_id)] = {"tier": tier, "until": until}
    return until


def invite_limit_reached(user_id: int) -> bool:
    return int(referrals["rewards"].get(str(user_id), 0)) >= INVITE_MAX_REWARDS


def invite_link(bot, user_id: int) -> str:
    return f"https://t.me/{bot.username}?start=ref_{user_id}"


def _parse_ref_start(text: str | None) -> int | None:
    parts = (text or "").split()

    if len(parts) < 2 or not parts[0].lower().startswith("/start"):
        return None

    payload = parts[1]
    return int(payload[4:]) if payload.startswith("ref_") and payload[4:].isdigit() else None


async def capture_referral(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Runs before the channel gate: remembers who invited a brand-new person."""
    try:
        message = update.message
        user = update.effective_user

        if message is None or user is None or user.is_bot:
            return

        inviter_id = _parse_ref_start(message.text)

        if inviter_id is None or inviter_id == user.id:
            return

        uid = str(user.id)

        # Only people who have never used the bot count as "new".
        if uid in stats["users"] or uid in referrals["counted"] or uid in referrals["pending"] or is_unlimited(user.id):
            return

        if str(inviter_id) not in stats["users"] or invite_limit_reached(inviter_id):
            return

        referrals["pending"][uid] = inviter_id

        while len(referrals["pending"]) > MAX_PENDING_INVITES:
            referrals["pending"].pop(next(iter(referrals["pending"])))

        if REQUIRED_CHANNEL_ID == 0:   # no channel to join: count straight away
            await credit_referral(context.bot, user)
    except Exception:
        logger.exception("Couldn't record an invite")


async def credit_referral(bot, user) -> None:
    """The invited person is now in the channel: count them, and reward when the group is complete."""
    uid = str(user.id)
    inviter_id = referrals["pending"].pop(uid, None)

    if inviter_id is None or uid in referrals["counted"] or invite_limit_reached(inviter_id):
        return

    referrals["counted"][uid] = inviter_id
    group = referrals["progress"].setdefault(str(inviter_id), [])
    group.append(user.id)

    if len(group) < INVITE_TARGET:
        with contextlib.suppress(Exception):
            await bot.send_message(
                chat_id=inviter_id,
                text=MSG_INVITE_JOINED.format(name=user_display_name(user), progress=len(group), target=INVITE_TARGET),
            )
        return

    batch = group[:INVITE_TARGET]
    referrals["progress"][str(inviter_id)] = group[INVITE_TARGET:]
    referrals["rewards"][str(inviter_id)] = int(referrals["rewards"].get(str(inviter_id), 0)) + 1

    grant_bonus(inviter_id, INVITER_TIER, INVITER_DAYS)
    fmt = dict(
        target=INVITE_TARGET,
        inviter_days=_days_text(INVITER_DAYS), inviter_tier=tier_label(INVITER_TIER),
        invitee_days=_days_text(INVITEE_DAYS), invitee_tier=tier_label(INVITEE_TIER),
        max_rewards=INVITE_MAX_REWARDS, rewards=referrals["rewards"][str(inviter_id)],
    )

    with contextlib.suppress(Exception):
        await bot.send_message(chat_id=inviter_id, text=MSG_INVITE_DONE_INVITER.format(**fmt))

    for person_id in batch:
        grant_bonus(person_id, INVITEE_TIER, INVITEE_DAYS)
        with contextlib.suppress(Exception):
            await bot.send_message(
                chat_id=person_id,
                text=MSG_INVITE_DONE_INVITEE.format(name=display_name_for(inviter_id), **fmt),
            )


async def invite_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/invite — your personal link and how far along you are."""
    message = update.effective_message
    user = update.effective_user

    if message is None or user is None:
        return

    if invite_limit_reached(user.id):
        await message.reply_text(MSG_INVITE_LIMIT_REACHED.format(max_rewards=INVITE_MAX_REWARDS))
        return

    link = invite_link(context.bot, user.id)
    text = MSG_INVITE_PAGE.format(
        link=link,
        target=INVITE_TARGET,
        inviter_days=_days_text(INVITER_DAYS), inviter_tier=tier_label(INVITER_TIER),
        invitee_days=_days_text(INVITEE_DAYS), invitee_tier=tier_label(INVITEE_TIER),
        progress=len(referrals["progress"].get(str(user.id), [])),
        rewards=int(referrals["rewards"].get(str(user.id), 0)),
        max_rewards=INVITE_MAX_REWARDS,
    )
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton(
            BTN_SHARE_INVITE,
            url=f"https://t.me/share/url?url={quote(link, safe='')}&text={quote(MSG_SHARE_INVITE_TEXT)}",
        )
    ]])
    await message.reply_text(text, reply_markup=keyboard)


# ---- /subscribe: pick a plan, pay, send a screenshot -> the admin approves with one tap
awaiting_payment_proof: dict[int, dict] = {}   # user id -> {"tier": ..., "since": time}
_last_proof_at: dict[int, float] = {}
PAYMENT_PROOF_WAIT_SECONDS = 1800


# ------------------------------------------------------------
# Homepage (/start): Usage / Subscribe / About
# ------------------------------------------------------------
def subscribe_callback(real_callback: str) -> str:
    """The real callback when subscriptions are on; an inert "coming soon" one while they're off."""
    return real_callback if SUBSCRIPTIONS_ENABLED else "subsoon:_:0"


def home_plans_text() -> str:
    """One short line per plan (Free / Plus / Pro) for the homepage."""
    lines = []

    for tier in ("free", "plus", "pro"):
        info = TIERS[tier]
        lines.append(
            MSG_HOME_PLAN_LINE.format(
                emoji=info["emoji"],
                label=info["label"],
                size_daily_mb=format_size(int(info["daily_mb"] * 1024 * 1024)),
                size_max_file_mb=format_size(int(info["max_file_mb"] * 1024 * 1024)),
            )
        )

    return "\n".join(lines)


def home_your_plan_text(user_id: int | None) -> str:
    """The user's own plan, plus how much of today's limit is left."""
    if user_id is not None and is_unlimited(user_id):
        return MSG_YOU_HAVE_NO_DAILY_LIMIT

    text = MSG_YOUR_PLAN.format(plan=plan_status_text(user_id))
    limit = daily_limit_bytes(user_id)

    if limit > 0 and user_id is not None:
        left = max(0, limit - quota_used_bytes(user_id))
        text += "\n" + MSG_HOME_LEFT_TODAY.format(
            size_left=format_size(left), size_limit=format_size(limit)
        )

    return text


def home_text(user) -> str:
    name = user_display_name(user) if user is not None else "there"
    user_id = user.id if user is not None else None

    return MSG_HI_SEND_ME_A_VIDEO.format(
        name=name,
        SAMPLE_SECONDS=SAMPLE_SECONDS,
        plans=home_plans_text(),
        your_plan=home_your_plan_text(user_id),
    )


def home_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(BTN_MERGE_VIDEOS, callback_data="merge:start:0")],
        [InlineKeyboardButton(BTN_SUBSCRIBE, callback_data=subscribe_callback("home:subscribe:0"))],
        [
            InlineKeyboardButton(BTN_USAGE, callback_data="home:usage:0"),
            InlineKeyboardButton(BTN_ABOUT_THE_BOT, callback_data="home:about:0"),
        ],
    ])


def usage_page_text(user_id: int | None) -> str:
    text = MSG_YOUR_USAGE.format(usage=quota_status_text(user_id))

    if user_id is not None and not is_unlimited(user_id):
        text += MSG_BIGGEST_FILE_YOU_CAN_SEND.format(size_max_file_bytes=format_size(max_file_bytes(user_id)))

    return text


def usage_keyboard(user_id: int | None) -> InlineKeyboardMarkup:
    rows = []

    if user_id is not None and not is_unlimited(user_id) and get_tier(user_id) != "pro":
        rows.append([InlineKeyboardButton(BTN_GET_MORE_SUBSCRIBE, callback_data=subscribe_callback("home:subscribe:0"))])

    rows.append([InlineKeyboardButton(BTN_BACK, callback_data="home:back:0")])
    return InlineKeyboardMarkup(rows)


def subscribe_page_text(user_id: int | None) -> str:
    return plans_text(user_id).split("\n\nTo subscribe")[0] + MSG_WHICH_PLAN_DO_YOU_WANT


def about_text() -> str:
    idle = f" or after {IDLE_FILE_TTL_MINUTES} minutes without any action" if IDLE_FILE_TTL_MINUTES > 0 else ""

    return (
        MSG_ABOUT_THIS_BOT_COMPRESSES_VIDEOS.format(idle=idle)
    )


def about_keyboard() -> InlineKeyboardMarkup:
    rows = []

    if REQUIRED_CHANNEL_URL:
        rows.append([InlineKeyboardButton(BTN_OUR_CHANNEL, url=REQUIRED_CHANNEL_URL)])

    rows.append([InlineKeyboardButton(BTN_BACK, callback_data="home:back:0")])
    return InlineKeyboardMarkup(rows)


async def show_page(query, text: str, keyboard: InlineKeyboardMarkup) -> None:
    """Swap the homepage message for another page (ignoring 'message is not modified')."""
    try:
        await query.edit_message_text(text, reply_markup=keyboard)
    except Exception as error:
        if "not modified" not in str(error).lower():
            logger.warning("Couldn't show the page: %s", error)


def subscribe_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(tier_label("plus"), callback_data=subscribe_callback("subpick:plus:0")),
        InlineKeyboardButton(tier_label("pro"), callback_data=subscribe_callback("subpick:pro:0")),
    ], [InlineKeyboardButton(BTN_BACK, callback_data="home:back:0")]])


async def subscribe_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/subscribe: choose a plan, then send a screenshot of the payment."""
    message = update.effective_message
    user = update.effective_user

    if message is None or user is None:
        return

    await message.reply_text(subscribe_page_text(user.id), reply_markup=subscribe_keyboard())


def payment_steps_text(tier: str) -> str:
    info = TIERS[tier]
    how_to_pay = PAYMENT_INSTRUCTIONS or "Ask the owner for the payment details (see the last line)."

    return (
        MSG_30_DAYS_PER_DAY_FILES.format(plan_tier=tier_label(tier), size_daily_mb=format_size(int(info['daily_mb'] * 1024 * 1024)), size_max_file_mb=format_size(int(info['max_file_mb'] * 1024 * 1024)), how_to_pay=how_to_pay, dm_text=dm_text())
    )


async def maybe_handle_payment_proof(update: Update, context: ContextTypes.DEFAULT_TYPE, file_id: str, kind: str) -> bool:
    """If this person is in the middle of /subscribe, an image is their payment proof. Returns True if used."""
    user = update.effective_user
    message = update.effective_message

    if user is None or message is None:
        return False

    state = awaiting_payment_proof.get(user.id)

    if state is None:
        return False

    if time.time() - state["since"] > PAYMENT_PROOF_WAIT_SECONDS:
        awaiting_payment_proof.pop(user.id, None)
        return False

    if time.time() - _last_proof_at.get(user.id, 0) < 20:
        return True   # ignore a flood of screenshots

    tier = state["tier"]
    username = f"@{user.username}" if user.username else "no username"
    caption = (
        MSG_PAYMENT_PROOF_FROM_ID_WANTS.format(name=user_display_name(user), username=username, id=user.id, plan_tier=tier_label(tier), plan_id=plan_status_text(user.id))
    )

    def mark(candidate: str) -> str:
        return " (requested)" if candidate == tier else ""

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton(BTN_APPROVE.format(plan_plus=tier_label('plus'), mark_plus=mark('plus')), callback_data=f"payok:plus:{user.id}")],
        [InlineKeyboardButton(BTN_APPROVE_2.format(plan_pro=tier_label('pro'), mark_pro=mark('pro')), callback_data=f"payok:pro:{user.id}")],
        [InlineKeyboardButton(BTN_REJECT, callback_data=f"payno:_:{user.id}")],
    ])

    try:
        if kind == "photo":
            await context.bot.send_photo(chat_id=ADMIN_USER_ID, photo=file_id, caption=caption, reply_markup=keyboard)
        else:
            await context.bot.send_document(chat_id=ADMIN_USER_ID, document=file_id, caption=caption, reply_markup=keyboard)
    except Exception:
        logger.exception("Couldn't forward the payment proof to the admin")
        await message.reply_text(MSG_I_COULDN_T_PASS_THAT.format(REQUIRED_CHANNEL_URL=REQUIRED_CHANNEL_URL))
        return True

    _last_proof_at[user.id] = time.time()
    awaiting_payment_proof.pop(user.id, None)
    await message.reply_text(MSG_GOT_IT_I_VE_SENT)
    return True


async def grant_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/grant <user> <plus|pro> [days=30] — admin only. <user> is a numeric ID or @username."""
    message = update.effective_message
    admin_id = update.effective_user.id if update.effective_user else None

    if message is None or not is_admin(admin_id):
        return

    usage = "Usage: /grant <user> <plus|pro> [days]\nExample: /grant 123456789 plus 30\n(<user> = numeric ID, or @username if they've used the bot)"

    if len(context.args) < 2:
        await message.reply_text(usage)
        return

    user_id = find_user_id(context.args[0])
    tier = context.args[1].lower()

    try:
        days = int(context.args[2]) if len(context.args) > 2 else 30
    except ValueError:
        days = 0

    if user_id is None:
        await message.reply_text(
            "I don't know that username. Use their numeric ID (they can get it from @userinfobot) "
            "or ask them to send me /start first.\n\n" + usage
        )
        return

    if tier not in ("plus", "pro") or not 1 <= days <= 3650:
        await message.reply_text(usage)
        return

    until, verb = grant_plan(user_id, tier, days)

    await message.reply_text(
        MSG_FOR_ACTIVE_UNTIL.format(plan_tier=tier_label(tier), verb=verb, name=display_name_for(user_id), user_id=user_id, date_until=format_until(until), QUOTA_TZ=QUOTA_TZ)
    )

    if not await notify_user_granted(context.bot, user_id, tier, until):
        await message.reply_text(MSG_I_COULDN_T_MESSAGE_THEM)


async def revoke_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/revoke <user> — admin only: end someone's plan now."""
    message = update.effective_message
    admin_id = update.effective_user.id if update.effective_user else None

    if message is None or not is_admin(admin_id):
        return

    user_id = find_user_id(context.args[0]) if context.args else None

    if user_id is None:
        await message.reply_text(MSG_USAGE_REVOKE_USER_NUMERIC_ID)
        return

    if active_subscription(user_id) is None:
        await message.reply_text(MSG_HAS_NO_ACTIVE_PLAN.format(name=display_name_for(user_id), user_id=user_id))
        return

    subscriptions.pop(str(user_id), None)
    referrals["bonus"].pop(str(user_id), None)
    await message.reply_text(MSG_PLAN_ENDED_FOR_BACK_TO.format(name=display_name_for(user_id), user_id=user_id))


async def subs_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/subs — admin only: who is subscribed."""
    message = update.effective_message
    admin_id = update.effective_user.id if update.effective_user else None

    if message is None or not is_admin(admin_id):
        return

    rows = []
    for key in list(subscriptions):
        entry = active_subscription(int(key))
        if entry:
            rows.append((entry["until"], int(key), entry["tier"]))

    if not rows:
        await message.reply_text(MSG_NO_ACTIVE_SUBSCRIBERS)
        return

    rows.sort()
    lines = [MSG_ACTIVE_SUBSCRIBERS.format(subscribers=subscribers_summary()), ""]

    for until, user_id, tier in rows[:40]:
        days_left = max(0, int((until - time.time()) // 86400))
        lines.append(MSG_UNTIL_D_LEFT.format(plan_tier=tier_label(tier), name=display_name_for(user_id), user_id=user_id, date_until=format_until(until), days_left=days_left))

    await message.reply_text("\n".join(lines)[:4000])


async def plans_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/plans — what each plan gives, and the person's own plan."""
    message = update.effective_message
    user = update.effective_user

    if message is None or user is None:
        return

    await message.reply_text(plans_text(user.id))


# ------------------------------------------------------------
# Join-the-channel gate: nobody can use the bot until they have joined
# REQUIRED_CHANNEL (the bot must be an ADMIN of that channel to see its
# members). Runs before every other handler; the admin and UNLIMITED_USER_IDS
# are exempt. Set REQUIRED_CHANNEL_ID=0 to switch it off.
# ------------------------------------------------------------
REQUIRED_CHANNEL_ID = int(os.getenv("REQUIRED_CHANNEL_ID", "-1004286055380"))
REQUIRED_CHANNEL_URL = os.getenv("REQUIRED_CHANNEL_URL", "https://t.me/Kemopression")
JOIN_CACHE_SECONDS = 600   # a member isn't re-checked for 10 minutes


_joined_until: dict[int, float] = {}
_join_check_warned = False


def join_keyboard(styled: bool = True) -> InlineKeyboardMarkup:
    # Telegram can colour a button ("style": "success" = green). It goes in
    # api_kwargs, so it works on any python-telegram-bot version.
    join_button = InlineKeyboardButton(
        BTN_JOIN_THE_CHANNEL,
        url=REQUIRED_CHANNEL_URL,
        api_kwargs={"style": "success"} if styled else None,
    )
    return InlineKeyboardMarkup([
        [join_button],
        [InlineKeyboardButton(BTN_I_VE_JOINED, callback_data="joincheck")],
    ])


async def is_channel_member(bot, user_id: int) -> bool | None:
    """True / False, or None when it can't be checked (bot isn't an admin of the channel)."""
    now = time.monotonic()

    if _joined_until.get(user_id, 0) > now:
        return True

    try:
        member = await bot.get_chat_member(chat_id=REQUIRED_CHANNEL_ID, user_id=user_id)
    except Exception:
        logger.exception("Couldn't check channel membership for %s", user_id)
        return None

    joined = member.status in ("member", "administrator", "creator") or (
        member.status == "restricted" and bool(getattr(member, "is_member", False))
    )

    if joined:
        _joined_until[user_id] = now + JOIN_CACHE_SECONDS

    return joined


async def send_join_prompt(bot, chat_id: int) -> None:
    try:
        await bot.send_message(chat_id=chat_id, text=JOIN_TEXT, reply_markup=join_keyboard(styled=True))
    except Exception:
        # Maybe this Bot API server doesn't know button colours: send it plain.
        logger.warning("Styled join button failed — sending a plain one", exc_info=True)
        await bot.send_message(chat_id=chat_id, text=JOIN_TEXT, reply_markup=join_keyboard(styled=False))


async def require_channel_membership(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    global _join_check_warned

    user = update.effective_user

    if REQUIRED_CHANNEL_ID == 0 or user is None or user.is_bot or is_unlimited(user.id):
        return

    joined = await is_channel_member(context.bot, user.id)

    if joined is None:
        # Can't check (bot not an admin there?): let people through instead of locking everyone out.
        if not _join_check_warned:
            _join_check_warned = True
            try:
                await context.bot.send_message(
                    chat_id=ADMIN_USER_ID,
                    text=(
                        MSG_I_CAN_T_CHECK_WHO
                    ),
                )
            except Exception:
                pass
        return

    query = update.callback_query
    is_recheck = query is not None and query.data == "joincheck"

    if joined:
        if str(user.id) in referrals["pending"]:
            await credit_referral(context.bot, user)

        if is_recheck:
            await query.answer(MSG_THANKS_FOR_JOINING)
            with contextlib.suppress(Exception):
                await query.edit_message_text(MSG_YOU_RE_IN_SEND_ME)
            raise ApplicationHandlerStop
        return

    if is_recheck:
        await query.answer(MSG_YOU_HAVEN_T_JOINED_YET, show_alert=True)
        raise ApplicationHandlerStop

    if query is not None:
        await query.answer(MSG_PLEASE_JOIN_OUR_CHANNEL_FIRST, show_alert=True)

    chat_id = update.effective_chat.id if update.effective_chat else user.id
    await send_join_prompt(context.bot, chat_id)
    raise ApplicationHandlerStop


# ------------------------------------------------------------
# Statistics (/stats, admin only). Saved inside Database.zip (stats.json)
# so they survive restarts. All dates/times are UTC.
# ------------------------------------------------------------
STATS_RECENT_LIMIT = 500
STATS_DAILY_DAYS = 60


def _empty_stats() -> dict:
    return {
        "since": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "totals": {
            "jobs": 0, "video": 0, "audio": 0, "audio_edit": 0, "audio_replace": 0, "cover_only": 0, "smart_jobs": 0,
            "failed": 0, "cancelled": 0, "received_video": 0, "received_audio": 0,
            "original_bytes": 0, "compressed_bytes": 0,
            "media_seconds": 0.0, "encode_seconds": 0.0, "wait_seconds": 0.0,
            "pct_sum": 0.0, "pct_n": 0,
            "max_original_bytes": 0, "best_saved_pct": None, "worst_saved_pct": None,
        },
        "levels": {},   # level label -> jobs / bytes / seconds
        "users": {},    # user id (as text) -> name, username, first/last seen, jobs, bytes
        "hours": [0] * 24,
        "daily": {},    # "YYYY-MM-DD" -> jobs
        "recent": [],   # the last STATS_RECENT_LIMIT jobs, one dict each
    }


stats: dict = _empty_stats()


def _stats_activity(data: dict) -> int:
    totals = data.get("totals", {})
    return (
        int(totals.get("jobs", 0)) + int(totals.get("failed", 0))
        + int(totals.get("received_video", 0)) + int(totals.get("received_audio", 0))
        + len(data.get("users", {}))
    )


def stats_json_bytes() -> bytes:
    return json.dumps(stats, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def merge_stats_bytes(raw: bytes) -> bool:
    """
    Load stats from a Database.zip. If this server already has stats, the
    bigger set wins (so an old backup can't overwrite newer numbers).
    Returns True if the loaded stats were adopted. Raises ValueError if unusable.
    """
    global stats

    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"stats are not valid JSON ({error})") from error

    if not isinstance(data, dict) or not isinstance(data.get("totals"), dict):
        raise ValueError("not a stats file")

    loaded = _empty_stats()
    loaded["since"] = str(data.get("since") or loaded["since"])
    loaded["totals"].update({k: v for k, v in data["totals"].items() if k in loaded["totals"]})

    for key in ("levels", "users", "daily"):
        if isinstance(data.get(key), dict):
            loaded[key] = data[key]

    hours = data.get("hours")
    if isinstance(hours, list) and len(hours) == 24 and all(isinstance(h, int) for h in hours):
        loaded["hours"] = hours

    if isinstance(data.get("recent"), list):
        loaded["recent"] = data["recent"][-STATS_RECENT_LIMIT:]

    if _stats_activity(loaded) >= _stats_activity(stats):
        stats = loaded
        return True

    return False


def _touch_user(user) -> dict | None:
    """Create/refresh this user's entry (dates only, so it doesn't change on every message)."""
    if user is None or getattr(user, "is_bot", False):
        return None

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    name = user_display_name(user)
    username = getattr(user, "username", None)
    entry = stats["users"].get(str(user.id))

    if entry is None:
        entry = {
            "name": name, "username": username,
            "first_seen": today, "last_seen": today,
            "jobs": 0, "original_bytes": 0, "compressed_bytes": 0,
        }
        stats["users"][str(user.id)] = entry
    else:
        entry["name"] = name
        entry["username"] = username
        entry["last_seen"] = today

    return entry


async def track_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Runs before every other handler: keeps the list of users up to date."""
    _touch_user(update.effective_user)


def record_received(media_type: str) -> None:
    key = "received_audio" if media_type == "audio" else "received_video"
    stats["totals"][key] += 1


def record_cancel() -> None:
    stats["totals"]["cancelled"] = stats["totals"].get("cancelled", 0) + 1


def record_failure(user) -> None:
    stats["totals"]["failed"] += 1
    _touch_user(user)


def record_job(
    *, user, kind: str, level_label: str | None, original_size, compressed_size,
    media_seconds, encode_seconds, wait_seconds, total_seconds,
    audio_label: str, boost: int, noise_reduction: bool, voice_enhancement: bool,
    smart: bool = False,
) -> None:
    """Log one finished job. kind: video | audio | audio_edit | audio_replace."""
    now = datetime.now(timezone.utc)
    totals = stats["totals"]

    # Only real compressions count towards size/percentage numbers —
    # audio edits/replacements keep the video, so "saved %" means nothing there.
    is_compression = kind in ("video", "audio")
    saved_pct = None
    if is_compression and original_size and compressed_size is not None:
        saved_pct = (original_size - compressed_size) / original_size * 100

    totals["jobs"] += 1
    totals[kind] = totals.get(kind, 0) + 1
    if smart:
        totals["smart_jobs"] = totals.get("smart_jobs", 0) + 1
    totals["media_seconds"] += float(media_seconds or 0)
    totals["encode_seconds"] += float(encode_seconds or 0)
    totals["wait_seconds"] += float(wait_seconds or 0)
    totals["max_original_bytes"] = max(totals["max_original_bytes"], int(original_size or 0))

    if is_compression:
        totals["original_bytes"] += int(original_size or 0)
        totals["compressed_bytes"] += int(compressed_size or 0)

    if saved_pct is not None:
        totals["pct_sum"] += saved_pct
        totals["pct_n"] += 1
        best, worst = totals["best_saved_pct"], totals["worst_saved_pct"]
        totals["best_saved_pct"] = saved_pct if best is None else max(best, saved_pct)
        totals["worst_saved_pct"] = saved_pct if worst is None else min(worst, saved_pct)

    label = level_label or {
        "audio": "Audio only", "audio_edit": "Audio edit", "audio_replace": "Audio replace",
        "cover_only": "Cover only", "audio_copy": "Audio tags (no compression)",
    }.get(kind, "Video")
    level_entry = stats["levels"].setdefault(
        label, {"jobs": 0, "original_bytes": 0, "compressed_bytes": 0, "encode_seconds": 0.0, "media_seconds": 0.0}
    )
    level_entry["jobs"] += 1
    level_entry["encode_seconds"] += float(encode_seconds or 0)
    level_entry["media_seconds"] += float(media_seconds or 0)
    if is_compression:
        level_entry["original_bytes"] += int(original_size or 0)
        level_entry["compressed_bytes"] += int(compressed_size or 0)

    user_entry = _touch_user(user)
    if user_entry is not None:
        user_entry["jobs"] += 1
        if is_compression:
            user_entry["original_bytes"] += int(original_size or 0)
            user_entry["compressed_bytes"] += int(compressed_size or 0)

    stats["hours"][now.hour] += 1
    day = now.strftime("%Y-%m-%d")
    stats["daily"][day] = stats["daily"].get(day, 0) + 1
    for old_day in sorted(stats["daily"])[:-STATS_DAILY_DAYS]:
        stats["daily"].pop(old_day, None)

    stats["recent"].append({
        "t": now.isoformat(timespec="seconds"),
        "user_id": getattr(user, "id", None),
        "user": user_display_name(user) if user is not None else "Unknown",
        "kind": kind,
        "level": label,
        "original_bytes": original_size,
        "compressed_bytes": compressed_size,
        "saved_pct": None if saved_pct is None else round(saved_pct, 1),
        "media_seconds": None if media_seconds is None else round(media_seconds, 1),
        "encode_seconds": None if encode_seconds is None else round(encode_seconds, 1),
        "wait_seconds": None if wait_seconds is None else round(wait_seconds, 1),
        "total_seconds": None if total_seconds is None else round(total_seconds, 1),
        "audio": audio_label,
        "boost_percent": boost,
        "noise_reduction": noise_reduction,
        "voice_enhancement": voice_enhancement,
        "smart": bool(smart),
    })
    del stats["recent"][:-STATS_RECENT_LIMIT]


def _pct_text(value) -> str:
    return "n/a" if value is None else f"{value:.1f}%"


def stats_overview_text() -> str:
    t = stats["totals"]
    jobs = t["jobs"]
    orig, comp = t["original_bytes"], t["compressed_bytes"]
    overall_pct = (orig - comp) / orig * 100 if orig else None
    avg_pct = t["pct_sum"] / t["pct_n"] if t["pct_n"] else None
    now = datetime.now(timezone.utc)

    def jobs_in_last(days: int) -> int:
        days_list = [(now - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(days)]
        return sum(stats["daily"].get(d, 0) for d in days_list)

    lines = [
        MSG_BOT_STATISTICS_TIMES_IN_UTC,
        MSG_SINCE_USER_S.format(since=stats['since'][:10], count_users=len(stats['users'])),
        "",
        MSG_FINISHED_JOBS.format(jobs=jobs),
        MSG_VIDEO_COMPRESSIONS.format(video=t['video']),
        MSG_AUDIO_COMPRESSIONS.format(audio=t['audio']),
        MSG_AUDIO_EDITS_AUDIO_REPLACED.format(audio_edit=t['audio_edit'], audio_replace=t['audio_replace']),
        MSG_FAILED.format(failed=t['failed']),
        MSG_STATS_CANCELLED.format(cancelled=t.get('cancelled', 0)),
        MSG_FILES_RECEIVED_VIDEO_AUDIO.format(received_video=t['received_video'], received_audio=t['received_audio']),
        MSG_JOBS_TODAY_LAST_7_DAYS.format(jobs=jobs_in_last(1), jobs_2=jobs_in_last(7)),
        MSG_TODAY_PERSON_S_SENT_FILES.format(quota_today_summary=quota_today_summary()[0], size=format_size(quota_today_summary()[1])),
        MSG_ACTIVE_SUBSCRIBERS.format(subscribers=subscribers_summary()),
    ]

    if orig:
        lines += [
            "",
            MSG_COMPRESSION,
            MSG_ORIGINAL_TOTAL.format(size_orig=format_size(orig)),
            MSG_COMPRESSED_TOTAL.format(size_comp=format_size(comp)),
            MSG_SPACE_SAVED_OVERALL.format(size_orig_comp=format_size(orig - comp), pct_text_overall_pct=_pct_text(overall_pct)),
            MSG_AVERAGE_SAVING_PER_FILE.format(pct_text_avg_pct=_pct_text(avg_pct)),
            MSG_BEST_WORST.format(pct_text_best_saved_pct=_pct_text(t['best_saved_pct']), pct_text_worst_saved_pct=_pct_text(t['worst_saved_pct'])),
            MSG_BIGGEST_FILE_RECEIVED.format(size_max_original_bytes=format_size(t['max_original_bytes'])),
        ]

    if jobs and t["encode_seconds"]:
        speed = t["media_seconds"] / t["encode_seconds"]
        lines += [
            "",
            MSG_TIME,
            MSG_MEDIA_PROCESSED_LONG.format(time_media_seconds=format_duration(t['media_seconds'])),
            MSG_TOTAL_ENCODING_TIME.format(time_encode_seconds=format_duration(t['encode_seconds'])),
            MSG_AVERAGE_PER_JOB_REAL_TIME.format(time_encode_seconds_jobs=format_duration(t['encode_seconds'] / jobs), speed=speed),
            MSG_AVERAGE_QUEUE_WAIT.format(time_wait_seconds_jobs=format_duration(t['wait_seconds'] / jobs)),
        ]

    if stats["levels"]:
        lines += ["", MSG_BY_LEVEL]
        for label, entry in sorted(stats["levels"].items(), key=lambda item: -item[1]["jobs"]):
            level_pct = (
                (entry["original_bytes"] - entry["compressed_bytes"]) / entry["original_bytes"] * 100
                if entry["original_bytes"] else None
            )
            lines.append(
                MSG_JOB_S_SAVED_AVG_ENCODE.format(label=label, jobs=entry['jobs'], pct_text_level_pct=_pct_text(level_pct), time_encode_seconds_jobs=format_duration(entry['encode_seconds'] / entry['jobs']))
            )

    if any(stats["hours"]):
        peak = max(range(24), key=lambda h: stats["hours"][h])
        lines += ["", MSG_BUSIEST_HOUR_00_59_UTC.format(peak=peak, hours_peak=stats['hours'][peak])]

    recent = stats["recent"][-10:]
    if recent:
        lines += ["", MSG_LAST_JOBS_NEWEST_FIRST]
        for job in reversed(recent):
            when = job["t"][5:16].replace("T", " ")
            if job.get("saved_pct") is not None:
                size_part = (
                    MSG_STATS_OVERVIEW_TEXT.format(size_original_bytes=format_size(job['original_bytes']), size_compressed_bytes=format_size(job['compressed_bytes']), saved_pct=job['saved_pct'])
                )
            else:
                size_part = format_size(job["original_bytes"])
            enc = job.get("encode_seconds")
            enc_part = f" · encode {format_duration(enc)}" if enc is not None else ""
            lines.append(MSG_STATS_OVERVIEW_TEXT_2.format(when=when, value=job['user'], level=job['level'], size_part=size_part, enc_part=enc_part))

    lines += ["", MSG_MORE_STATS_USERS_STATS_CSV]
    return "\n".join(lines)


def stats_users_text() -> str:
    users = sorted(
        stats["users"].items(),
        key=lambda item: (-item[1]["jobs"], item[1].get("last_seen", "")),
    )
    active = sum(1 for _, entry in users if entry["jobs"])

    lines = [MSG_USER_S_HAVE_FINISHED_AT.format(count_users=len(users), active=active), ""]

    for rank, (user_id, entry) in enumerate(users[:25], start=1):
        saved = entry["original_bytes"] - entry["compressed_bytes"]
        username = f" ({format_username(entry.get('username'))})" if entry.get("username") else ""
        lines.append(
            f"{rank}. {entry['name']}{username} — {entry['jobs']} job(s)"
            + (f" · saved {format_size(saved)}" if entry["original_bytes"] else "")
            + f" · first {entry.get('first_seen', '?')} · last {entry.get('last_seen', '?')}"
        )

    if len(users) > 25:
        lines.append(MSG_AND_MORE.format(len_users=len(users) - 25))

    return "\n".join(lines)


def stats_csv_bytes() -> bytes:
    import csv

    fields = [
        "t", "user_id", "user", "kind", "level", "original_bytes", "compressed_bytes",
        "saved_pct", "media_seconds", "encode_seconds", "wait_seconds", "total_seconds",
        "audio", "boost_percent", "noise_reduction", "voice_enhancement", "smart",
    ]
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(stats["recent"])
    return buffer.getvalue().encode("utf-8-sig")


def reset_stats_data() -> None:
    """Wipe the statistics but KEEP the known users (name, @username, first/last seen).

    Only each user's counters (jobs / bytes) go back to zero, so the totals and the
    per-user numbers still add up, and /grant @username keeps working after a reset.
    """
    global stats

    kept_users: dict = {}
    for user_id, entry in stats.get("users", {}).items():
        kept_users[user_id] = {
            "name": entry.get("name") or "Unknown",
            "username": entry.get("username"),
            "first_seen": entry.get("first_seen") or datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            "last_seen": entry.get("last_seen") or datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            "jobs": 0, "original_bytes": 0, "compressed_bytes": 0,
        }

    stats = _empty_stats()
    stats["users"] = kept_users


async def reset_stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/reset_stats (admin only): asks for confirmation, then wipes the statistics."""
    message = update.effective_message
    user_id = update.effective_user.id if update.effective_user else None

    if message is None or not is_admin(user_id):
        return

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton(BTN_YES_RESET, callback_data="resetstats:yes:0"),
        InlineKeyboardButton(BTN_CANCEL, callback_data="resetstats:no:0"),
    ]])

    await message.reply_text(
        MSG_RESET_ALL_STATISTICS_THIS_ERASES.format(totals_jobs=stats['totals']['jobs'], count_users=len(stats['users'])),
        reply_markup=keyboard,
    )


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/stats (admin only). /stats users = per-user list, /stats csv = last jobs as a spreadsheet."""
    message = update.effective_message
    user_id = update.effective_user.id if update.effective_user else None

    if message is None or not is_admin(user_id):
        return

    option = (context.args[0].lower() if context.args else "")

    if option == "csv":
        if not stats["recent"]:
            await message.reply_text(MSG_NO_FINISHED_JOBS_YET)
            return
        await message.reply_document(
            document=stats_csv_bytes(),
            filename="stats_jobs.csv",
            caption=MSG_THE_LAST_JOB_S.format(count_recent=len(stats['recent'])),
        )
        return

    text = stats_users_text() if option == "users" else stats_overview_text()
    await message.reply_text(text[:4000])


# How many times to retry a flaky network call (download/upload)
# before giving up, and the base delay between attempts.
RETRY_ATTEMPTS = int(os.getenv("RETRY_ATTEMPTS", "5"))
RETRY_BASE_DELAY = float(os.getenv("RETRY_BASE_DELAY", "3"))

# Length (seconds) of the sample clip encoded at EACH compression level.
# The same clip is used two ways: to extrapolate the full-video size
# estimate, and to send you a real preview of how that level looks
# before you pick one. Longer = more accurate estimate and a better
# preview, but the analyze step takes longer on a small CPU.
SAMPLE_SECONDS = float(os.getenv("SAMPLE_SECONDS", "5"))

# Caps FFmpeg's own thread usage so it doesn't try to use more
# CPU than the service is actually allotted (helps avoid getting
# throttled/killed on tight Railway resource limits).
FFMPEG_THREADS = os.getenv("FFMPEG_THREADS", "2")

# Threads FFmpeg may use to DECODE the source video. Left alone, the decoder
# picks its own thread count (often the host's core count), and every thread
# keeps decoded frames in flight — a big RAM cost on 1080p/4K sources.
DECODER_THREADS = os.getenv("DECODER_THREADS", "0")  # 0 = let FFmpeg decide

# Priority of the heavy FFmpeg jobs (0 = normal, 19 = lowest). Raising it makes
# FFmpeg step aside whenever the bot itself, the Telegram API server or anything
# else needs CPU, so a big encode can no longer starve them. It costs nothing
# when the server is otherwise idle. Set FFMPEG_NICE=0 to switch it off.
FFMPEG_NICE = int(os.getenv("FFMPEG_NICE", "10"))

# How many level-estimates run at once during the "analyze" step.
# Running all 5 fully in parallel spikes CPU/RAM briefly; this
# caps it so the estimate phase stays within tight resource limits.
ESTIMATE_CONCURRENCY = int(os.getenv("ESTIMATE_CONCURRENCY", "1"))

# (How many heavy jobs run at once is MAX_CONCURRENT_JOBS, further down:
# downloads/analysis and compressions share that one slot.)

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

# The automatic threshold is only trusted inside this range. Real duplicates
# differ by compression noise only (a few levels out of 255); anything above
# the cap is a real change (a new slide, new text), so it must never be treated
# as a duplicate -- no matter how the diffs of this particular video cluster.
DEDUP_MIN_THRESHOLD = 6
DEDUP_MAX_THRESHOLD = 14
# For the automatic split to count, the "duplicate" group must really be noise-
# level and sit clearly apart from the "real change" group.
DEDUP_MAX_LOW_GROUP_MEAN = 8
DEDUP_MIN_GROUP_GAP = 20

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
        "label": MSG_LOW,
        "detail": MSG_BEST_QUALITY_BIGGEST_FILE,
        "crf": "23",
        "preset": "fast",
        "max_width": 1920,
        "audio_bitrate": "128k",
        "lookahead": "20",
        "refs": "3",
        "bframes": "3",
    },
    "medium": {
        "label": MSG_MEDIUM,
        "detail": MSG_BALANCED_DEFAULT,
        "crf": os.getenv("CRF", "28"),
        "preset": os.getenv("PRESET", "veryfast"),
        "max_width": int(os.getenv("MAX_WIDTH", "1280")),
        "audio_bitrate": os.getenv("AUDIO_BITRATE", "96k"),
        "lookahead": "15",
        "refs": "2",
        "bframes": "2",
    },
    "high": {
        "label": MSG_HIGH,
        "detail": MSG_SMALLER_FILE_VISIBLE_QUALITY_LOSS,
        "crf": "32",
        "preset": "veryfast",
        "max_width": 854,
        "audio_bitrate": "64k",
        "lookahead": "10",
        "refs": "1",
        "bframes": "2",
    },
    "very_high": {
        "label": MSG_VERY_HIGH,
        "detail": MSG_MUCH_SMALLER_NOTICEABLY_SOFTER,
        "crf": "36",
        "preset": "superfast",
        "max_width": 640,
        "audio_bitrate": "48k",
        "lookahead": "8",
        "refs": "1",
        "bframes": "1",
    },
    "extreme": {
        "label": MSG_EXTREME,
        "detail": MSG_SMALLEST_POSSIBLE_ROUGH_QUALITY,
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
    "low": {"label": MSG_LOW_2, "detail": MSG_SMALLEST_AUDIO_NOTICEABLE_QUALITY_LO, "bitrate": "48k"},
    "medium": {"label": MSG_MEDIUM_2, "detail": MSG_BALANCED, "bitrate": "96k"},
    "high": {"label": MSG_HIGH_2, "detail": MSG_BEST_QUALITY_BIGGEST_AUDIO, "bitrate": "160k"},
}

AUDIO_LEVEL_ORDER = ["low", "medium", "high"]

DEFAULT_AUDIO_LEVEL = "medium"

# Extra choice in the video flow only: keep the original audio exactly as it is
# (no re-encoding, so no quality loss; enhancements/volume boost don't apply).
ORIGINAL_AUDIO = "original"
AUDIO_LEVELS[ORIGINAL_AUDIO] = {
    "label": MSG_ORIGINAL,
    "detail": MSG_KEEP_THE_AUDIO_UNTOUCHED_NO,
    "bitrate": None,
}
# Audio formats that can be copied straight into the output MP4.
MP4_COPY_SAFE_AUDIO = {"aac", "mp3", "ac3", "eac3", "alac"}

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

# Sentinel for "only the cover changed": the original video file is
# uploaded as it is (no re-encode, nothing else touched) with the new
# cover. Only ever passed to run_compression from the "upload unchanged"
# button shown after a video cover is set.
COVER_ONLY_LEVEL = "cover_only"

# Sentinel for an AUDIO file the user wants back with new tags (name /
# artist / album / cover) but WITHOUT any re-encoding: the sound is
# stream-copied, so quality and size stay exactly as they were. Only ever
# passed to run_compression from the "Send as is" button (callback
# "audiosend") — it never appears in audioset/volumeset callback data.
AUDIO_COPY_LEVEL = "audio_copy"

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
        return MSG_AUDIO_QUALITY.format(audio_label=audio_label)
    if level == AUDIO_EDIT_LEVEL:
        return MSG_JUST_EDITING_AUDIO_VIDEO_UNTOUCHED.format(audio_label=audio_label)
    return MSG_AUDIO.format(level_label=LEVELS[level]['label'], audio_label=audio_label)

# Audio file types accepted directly (message.audio/.voice) or as a
# document upload — routes into the audio-only flow instead of the
# video flow.
AUDIO_FILE_EXTENSIONS = (".mp3", ".m4a", ".aac", ".wav", ".ogg", ".oga", ".flac", ".opus", ".wma")

# Volume boost is intentionally a fixed set of steps (plus "no
# boost") rather than a free-form percentage — keeps it simple and
# gives each step its own preview clip below.
VOLUME_BOOST_OPTIONS = [0, 50, 100, 150, 200]  # percent: no boost, +50%, +100%, +150%, +200%

# Length of the audio-only preview clip sent for each volume level
# before the user picks one.
VOLUME_PREVIEW_SECONDS = int(os.getenv("VOLUME_PREVIEW_SECONDS", "20"))
ENHANCEMENT_PREVIEW_SECONDS = int(os.getenv("ENHANCEMENT_PREVIEW_SECONDS", "10"))

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

# chat_id -> {"mid": pending message_id, "field": "name" | "artist" | "album" | "cover" | "videocover"}
# While an entry exists, the next text message (name / album) or image
# (cover / videocover) that chat sends is treated as that value.
awaiting_input: dict[int, dict] = {}

# active_compressions is a plain counter — safe as a bare int since everything
# here runs on the single asyncio event loop, no separate threads involved.

# ONE heavy job at a time. A download + analysis (probe + sample encodes) and
# a compression (encode + upload + temp-file cleanup) share this single slot,
# so they never run together: the next job only starts once the previous one
# has finished AND cleaned up its temp files (RAM is tight on this server).
# Waiting jobs are served by plan first (Pro, then Plus, then Free) and, within
# the same plan, first-come-first-served. A running job is never interrupted.
MAX_CONCURRENT_JOBS = int(os.getenv("MAX_CONCURRENT_JOBS", "1"))


class PriorityGate:
    """A slot (or a few) that waiting jobs get in order of priority, then arrival."""

    def __init__(self, slots: int) -> None:
        self.free = slots
        self.waiters: list[dict] = []

    def locked(self) -> bool:
        return self.free <= 0 or bool(self.waiters)

    def try_take(self) -> bool:
        if self.free > 0 and not self.waiters:
            self.free -= 1
            return True
        return False

    def enqueue(self, entry: dict) -> None:
        entry["future"] = asyncio.get_running_loop().create_future()
        self.waiters.append(entry)

    async def wait(self, entry: dict) -> None:
        try:
            await entry["future"]
        except asyncio.CancelledError:
            if entry["future"].done() and not entry["future"].cancelled():
                self.release()                    # the slot had just been handed to us: give it back
            elif entry in self.waiters:
                self.waiters.remove(entry)
            raise

    def release(self) -> None:
        self.free += 1
        self._wake()

    def _wake(self) -> None:
        while self.free > 0 and self.waiters:
            entry = min(self.waiters, key=lambda e: (-e["priority"], e["seq"]))
            self.waiters.remove(entry)
            if not entry["future"].done():
                self.free -= 1
                entry["future"].set_result(None)


work_gate = PriorityGate(MAX_CONCURRENT_JOBS)
work_waiting = work_gate.waiters     # same list: other code only counts it
work_active = 0
_gate_sequence = itertools.count()

# /update mode (admin): turn away NEW files so a deploy can happen once the
# people already in the system have finished. See update_command() below.
update_mode = {"on": False, "notified": False}
active_job_ids: set[int] = set()   # message ids with a download/analysis/compression in the system
other_jobs_running = 0             # PDF / frame-extraction jobs (they don't use the work slot)

# How many Telegram updates the bot may handle at the same time. Without
# this the bot handles ONE update at a time, so while a video is compressing
# nobody else's message or button would even be looked at (no queue message,
# no /stats, no Cancel). 0 = old one-at-a-time behavior.
CONCURRENT_UPDATES = int(os.getenv("CONCURRENT_UPDATES", "16"))


def remaining_encode_seconds() -> float | None:
    """Rough time left on the compression running right now (None if unknown)."""
    now = time.monotonic()

    for info in active_job_progress.values():
        if info["percent"] >= 1:
            elapsed = now - info["start_time"]
            return elapsed * (100 - info["percent"]) / info["percent"]

    return None


async def acquire_work_slot(post_status, priority: int = 0, plan_label: str | None = None) -> None:
    """
    Wait for the single heavy-work slot. If there's a line, post_status(text)
    (awaited; it should post the first message and edit it afterwards) shows the
    person their place and refreshes it every few seconds. Subscribers (higher
    priority) are served before free users. Returns once the slot is theirs — the
    caller MUST call release_work_slot() when completely done (for a compression:
    after the result is sent and the temp files are gone).
    """
    global work_active

    entry = {"priority": priority, "seq": next(_gate_sequence), "future": None}
    refresher = None

    if not work_gate.try_take():
        work_gate.enqueue(entry)

        def wait_text() -> str:
            my_key = (-entry["priority"], entry["seq"])
            ahead = work_active + sum(
                1 for other in work_gate.waiters if (-other["priority"], other["seq"]) < my_key
            )

            lines = []
            if plan_label:
                lines.append(MSG_YOU_RE_SUBSCRIBED_SO_YOU.format(plan_label=plan_label))
            lines.append(
                MSG_JOB_S_AHEAD_OF_YOU.format(ahead=ahead)
            )

            remaining = remaining_encode_seconds()
            if remaining is not None:
                lines.append(MSG_THE_COMPRESSION_RUNNING_NOW_HAS.format(time_remaining=format_duration(remaining)))

            if not plan_label and priority == 0:
                lines.append(MSG_PLUS_AND_PRO_MEMBERS_ARE)

            return "\n".join(lines)

        try:
            await post_status(wait_text())
        except Exception:
            logger.warning("Couldn't post the queue message")

        async def refresh_wait_message() -> None:
            while True:
                await asyncio.sleep(8)
                try:
                    await post_status(wait_text())
                except Exception:
                    pass

        refresher = asyncio.create_task(refresh_wait_message())

        try:
            await work_gate.wait(entry)
        finally:
            refresher.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await refresher

    work_active += 1
    update_mode["notified"] = False  # new work started: the "all done" message must fire again


def release_work_slot() -> None:
    global work_active

    work_active = max(0, work_active - 1)
    work_gate.release()
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

    input_chats = [
        chat_id for chat_id, info in awaiting_input.items() if info.get("mid") == message_id
    ]
    for chat_id in input_chats:
        awaiting_input.pop(chat_id, None)


# ------------------------------------------------------------
# Disk clean-up: nothing a person uploaded is kept forever.
#  - IDLE_FILE_TTL_MINUTES without any button tap -> the stored video is deleted
#    (they're told, and just send it again). 0 switches this off.
#  - the copy the local Bot API server keeps of every download is deleted
#    right after we make our own copy.
#  - at startup, leftovers from before a restart are swept away.
# ------------------------------------------------------------
IDLE_FILE_TTL_MINUTES = int(os.getenv("IDLE_FILE_TTL_MINUTES", "5"))


def delete_server_copy(telegram_file) -> None:
    """The local Bot API server never deletes what it downloaded; we have our own copy now."""
    path = getattr(telegram_file, "file_path", None)

    if path and os.path.isabs(str(path)):
        try:
            os.remove(path)
        except OSError:
            pass


def sweep_temp_dir() -> int:
    """At startup nothing is in flight, so anything in TEMP_DIR is a leftover."""
    root = Path(TEMP_DIR)
    removed = 0

    if not root.is_dir():
        return 0

    for item in root.iterdir():
        try:
            if item.is_dir():
                shutil.rmtree(item)
            else:
                item.unlink()
            removed += 1
        except OSError:
            logger.warning("Couldn't remove leftover %s", item)

    return removed


async def expire_idle_files(bot) -> int:
    """Delete stored videos whose owner hasn't done anything for IDLE_FILE_TTL_MINUTES."""
    if IDLE_FILE_TTL_MINUTES <= 0:
        return 0

    now = time.time()
    limit = IDLE_FILE_TTL_MINUTES * 60
    removed = 0

    for message_id, pending in list(pending_compressions.items()):
        if message_id in active_job_ids:
            continue   # being analyzed / queued / compressed right now

        if now - pending.get("last_active", now) < limit:
            continue

        chat_id = pending.get("chat_id")

        # Nothing was sent back for this file, so give the daily-limit bytes back.
        refunded_text = ""
        owner_id = pending.get("owner_id")
        charged = int(pending.get("charged_bytes") or pending.get("original_size") or 0)

        if quota_applies(owner_id) and charged > 0:
            quota_refund(owner_id, charged)
            refunded_text = format_size(charged)

        clear_pending(message_id)
        removed += 1

        if chat_id is not None:
            with contextlib.suppress(Exception):
                await bot.send_message(
                    chat_id=chat_id,
                    text=(
                        MSG_I_DELETED_YOUR_VIDEO_REFUNDED.format(
                            IDLE_FILE_TTL_MINUTES=IDLE_FILE_TTL_MINUTES, size=refunded_text
                        )
                        if refunded_text
                        else MSG_I_DELETED_YOUR_VIDEO_BECAUSE.format(IDLE_FILE_TTL_MINUTES=IDLE_FILE_TTL_MINUTES)
                    ),
                )

    return removed


async def idle_cleanup_loop(bot) -> None:
    while True:
        await asyncio.sleep(60)

        try:
            removed = await expire_idle_files(bot)
            removed += await expire_idle_merge_sessions(bot)
            if removed:
                logger.info("Deleted %d idle stored video(s).", removed)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Idle clean-up failed")


def remember_pending(message_id: int, entry: dict) -> list[dict]:
    """
    Store an analyzed file waiting for its owner's choice. If that goes over
    PENDING_MAX, the oldest stored files are deleted to make room — but never
    one that is being analyzed / queued / compressed right now, and never the
    new one. When every other entry is busy the cap is simply exceeded for a
    while (it shrinks again as jobs finish).

    Returns who was evicted (chat_id + refunded size text) so the caller can tell
    them — see notify_evicted(). The owner's daily-limit bytes are refunded, the
    same as when an idle file expires (except for a file whose PDF was already
    delivered: that one got its result, so nothing is given back).
    """
    entry["last_active"] = time.time()   # refreshed on every button tap (see handle_callback_inner)
    pending_compressions[message_id] = entry

    evicted: list[dict] = []

    while len(pending_compressions) > PENDING_MAX:
        victim_id = next(
            (
                key for key in pending_compressions
                if key != message_id and key not in active_job_ids and key not in running_jobs
            ),
            None,
        )

        if victim_id is None:
            break   # everyone else is busy: don't break a running job

        victim = pending_compressions.get(victim_id) or {}
        owner_id = victim.get("owner_id")
        charged = int(victim.get("charged_bytes") or victim.get("original_size") or 0)
        pdf_delivered = victim_id in open_pdf_sessions.values()
        refunded_text = ""

        if not pdf_delivered and quota_applies(owner_id) and charged > 0:
            quota_refund(owner_id, charged)
            refunded_text = format_size(charged)

        evicted.append({"chat_id": victim.get("chat_id"), "refunded": refunded_text})
        clear_pending(victim_id)
        logger.info("Evicted stored file %s to make room (PENDING_MAX=%d).", victim_id, PENDING_MAX)

    return evicted


async def notify_evicted(bot, evicted: list[dict]) -> None:
    """Tell the people whose stored file was removed by remember_pending(). Never raises."""
    for item in evicted:
        chat_id = item.get("chat_id")

        if chat_id is None:
            continue

        text = (
            MSG_EVICTED_MAKE_ROOM_REFUNDED.format(size=item["refunded"])
            if item.get("refunded")
            else MSG_EVICTED_MAKE_ROOM
        )

        with contextlib.suppress(Exception):
            await bot.send_message(chat_id=chat_id, text=text)


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


def level_grows_file(estimate: int | None, original_size: int | None) -> bool:
    """True when compressing at this level is estimated to make the file BIGGER."""
    return bool(estimate and original_size and estimate > original_size)


def estimate_keyboard(
    message_id: int,
    estimates: dict[str, int | None],
    highlight: str | None,
    smart: dict | None = None,
    original_size: int | None = None,
    styled: bool = True,
) -> InlineKeyboardMarkup:
    rows = []


    for level in LEVEL_ORDER:
        info = LEVELS[level]
        size_text = format_size(estimates.get(level))
        text = f"{info['label']} — ~{size_text}"

        if level_grows_file(estimates.get(level), original_size):
            # Would make the file bigger: red button that does nothing but explain why.
            rows.append([
                InlineKeyboardButton(
                    text,
                    callback_data=f"toobig:{level}:{message_id}",
                    api_kwargs={"style": "danger"} if styled else None,
                )
            ])
            continue

        if level == highlight:
            text = MSG_ESTIMATE_KEYBOARD.format(text=text)

        rows.append([
            InlineKeyboardButton(
                text,
                callback_data=f"compress:{level}:{message_id}",
            )
        ])

    rows.append([
        InlineKeyboardButton(
            BTN_JUST_EDIT_AUDIO_KEEP_VIDEO,
            callback_data=f"audioedit:_:{message_id}",
        )
    ])

    rows.append([
        InlineKeyboardButton(
            BTN_REPLACE_AUDIO_SEND_A_RECORDING,
            callback_data=f"audioreplace:_:{message_id}",
        )
    ])

    rows.append([
        InlineKeyboardButton(
            BTN_SET_VIDEO_COVER_SEND_AN,
            callback_data=f"videocover:_:{message_id}",
        )
    ])

    rows.append([
        InlineKeyboardButton(BTN_EXTRACT_TO_PDF, callback_data=f"pdfmenu:_:{message_id}")
    ])

    rows.append([
        InlineKeyboardButton(BTN_EXTRACT_AUDIO, callback_data=f"audioextract:_:{message_id}")
    ])

    rows.append([
        InlineKeyboardButton(BTN_CANCEL, callback_data=f"cancel:_:{message_id}")
    ])

    return InlineKeyboardMarkup(rows)


def audio_keyboard(
    message_id: int,
    level: str,
    estimates: dict[str, int | None],
    highlight: str,
    from_video: bool = False,
    copy_ok: bool = True,
) -> InlineKeyboardMarkup:
    rows = []

    for audio_level in AUDIO_LEVEL_ORDER:
        info = AUDIO_LEVELS[audio_level]
        size_text = format_size(estimates.get(audio_level))
        text = f"{info['label']} — ~{size_text} · {info['detail']}"

        if audio_level == highlight:
            text = MSG_ESTIMATE_KEYBOARD.format(text=text)

        rows.append([
            InlineKeyboardButton(
                text,
                callback_data=f"audioset:{level}|{audio_level}:{message_id}",
            )
        ])

    if level in LEVELS:
        original_info = AUDIO_LEVELS[ORIGINAL_AUDIO]
        rows.append([
            InlineKeyboardButton(
                BTN_AUDIO_KEYBOARD.format(original_info_label=original_info['label'], size_estimates_original_audio=format_size(estimates.get(ORIGINAL_AUDIO)), original_info_detail=original_info['detail']),
                callback_data=f"audioset:{level}|{ORIGINAL_AUDIO}:{message_id}",
            )
        ])

    if level == AUDIO_ONLY_LEVEL:
        # Extracting from a video: "send as is" only when the audio can be copied out untouched.
        if copy_ok or not from_video:
            rows.append([
                InlineKeyboardButton(
                    BTN_SEND_AS_IS,
                    callback_data=f"audiosend:_:{message_id}",
                )
            ])
        rows.append([
            InlineKeyboardButton(
                BTN_RENAME_ALBUM_NAME_COVER,
                callback_data=f"ameta:open:{message_id}",
            )
        ])

    # After picking a compression level (or extracting audio from a video),
    # let the user go back to the video menu.
    if level in LEVELS or from_video:
        rows.append([
            InlineKeyboardButton(BTN_GO_BACK, callback_data=f"levelback:_:{message_id}")
        ])

    rows.append([
        InlineKeyboardButton(BTN_CANCEL, callback_data=f"cancel:_:{message_id}")
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
            MSG_NO_BOOST if boost == 0 else MSG_VOLUME_KEYBOARD.format(boost=boost),
            callback_data=f"volumeset:{level}|{audio_level}|{boost}|{flags}:{message_id}",
        )
        for boost in VOLUME_BOOST_OPTIONS
    ]

    rows = [buttons[i:i + 3] for i in range(0, len(buttons), 3)]
    rows.append([InlineKeyboardButton(BTN_CANCEL, callback_data=f"cancel:_:{message_id}")])

    return InlineKeyboardMarkup(rows)


def enhancement_keyboard(
    message_id: int,
    level: str,
    audio_level: str,
    noise_reduction: bool,
    voice_enhancement: bool,
) -> InlineKeyboardMarkup:
    noise_label = MSG_NOISE_REDUCTION.format(noise_reduction_choice='ON ✅' if noise_reduction else 'OFF')
    voice_label = MSG_VOICE_ENHANCEMENT.format(voice_enhancement_choice='ON ✅' if voice_enhancement else 'OFF')

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
            BTN_PREVIEW_NOISE_VOICE_BOTH,
            callback_data=f"enhpreview:{level}|{audio_level}|{current_flags}:{message_id}",
        )],
        [InlineKeyboardButton(
            BTN_CONTINUE,
            callback_data=f"enhcontinue:{level}|{audio_level}|{current_flags}:{message_id}",
        )],
        [InlineKeyboardButton(BTN_CANCEL, callback_data=f"cancel:_:{message_id}")],
    ])


def pdf_interval_keyboard(message_id: int) -> InlineKeyboardMarkup:
    row = [
        InlineKeyboardButton(BTN_S.format(seconds=seconds), callback_data=f"pdf:{seconds}:{message_id}")
        for seconds in PDF_INTERVALS
    ]
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(BTN_AUTO_DETECT_CHANGES, callback_data=f"pdfauto:_:{message_id}")],
        row,
        [InlineKeyboardButton(BTN_CANCEL, callback_data=f"cancel:_:{message_id}")],
    ])


def post_pdf_keyboard(message_id: int, dedup_available: bool = True) -> InlineKeyboardMarkup:
    rows = []

    if dedup_available:
        rows.append([
            InlineKeyboardButton(
                BTN_AUTO_DELETE_SIMILAR_FRAMES,
                callback_data=f"pdfdedup:_:{message_id}",
            )
        ])

    rows.append([
        InlineKeyboardButton(BTN_COMPRESS_VIDEO, callback_data=f"pdfcompress:_:{message_id}")
    ])

    rows.append([
        InlineKeyboardButton(
            BTN_FINISH_SESSION_DELETES_VIDEO,
            callback_data=f"pdffinish:_:{message_id}",
        )
    ])

    return InlineKeyboardMarkup(rows)


def sanitize_filename(name: str | None, max_length: int = 100) -> str | None:
    """
    Turn user-typed text into a safe file name (no extension). Strips
    path separators / reserved characters / control characters, drops
    a trailing audio extension if they typed one, and returns None if
    nothing usable is left.
    """

    if not name:
        return None

    cleaned = re.sub(r'[\\/:*?"<>|\x00-\x1f]', " ", name)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")

    lowered = cleaned.lower()
    for extension in AUDIO_FILE_EXTENSIONS:
        if lowered.endswith(extension):
            cleaned = cleaned[: -len(extension)].strip(" .")
            break

    cleaned = cleaned[:max_length].strip(" .")
    return cleaned or None


def audio_meta_text(pending: dict) -> str:
    original = Path(pending.get("filename") or "audio").stem
    return (
        MSG_AUDIO_DETAILS_ALL_OPTIONAL_NAME.format(out_name=pending.get('out_name') or original + ' (original)', artist=pending.get('artist') or '—', album=pending.get('album') or '—', cover_path_choice='✅ set' if pending.get('cover_path') else '—')
    )


def audio_meta_keyboard(message_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(BTN_RENAME_FILE, callback_data=f"ameta:name:{message_id}")],
        [InlineKeyboardButton(BTN_ARTIST_NAME, callback_data=f"ameta:artist:{message_id}")],
        [InlineKeyboardButton(BTN_ALBUM_NAME, callback_data=f"ameta:album:{message_id}")],
        [InlineKeyboardButton(BTN_ALBUM_COVER, callback_data=f"ameta:cover:{message_id}")],
        [InlineKeyboardButton(BTN_CLEAR_ALL, callback_data=f"ameta:clear:{message_id}")],
        [InlineKeyboardButton(BTN_SEND_AS_IS, callback_data=f"audiosend:_:{message_id}")],
        [InlineKeyboardButton(BTN_DONE, callback_data=f"ameta:done:{message_id}")],
    ])


def setlevel_keyboard(highlight: str | None) -> InlineKeyboardMarkup:
    buttons = []
    for level in LEVEL_ORDER:
        text = LEVELS[level]["label"]
        if level == highlight:
            text = MSG_ESTIMATE_KEYBOARD.format(text=text)
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


async def prepare_cover_image(input_path: str, output_path: str, max_side: int, quality: str = "3") -> bool:
    """
    Convert any image (jpg/png/webp...) into a JPEG that fits inside
    max_side x max_side, keeping its aspect ratio and never upscaling.
    Used for the full-size cover (embedded in the audio file / sent as
    the video cover) and a 320px thumbnail (Telegram's limit).
    """

    command = [
        "ffmpeg", "-y",
        "-i", input_path,
        "-frames:v", "1",
        "-vf",
        (
            f"scale='min({max_side},iw)':'min({max_side},ih)':"
            "force_original_aspect_ratio=decrease,format=yuvj420p"
        ),
        "-q:v", quality,
        output_path,
    ]

    returncode, _stdout, stderr = await run_command(command)

    if returncode != 0 or not Path(output_path).exists():
        logger.warning("Cover preparation failed: %s", stderr.decode(errors="replace"))
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
    copy_audio: bool = False,
) -> list[str]:
    command = ["ffmpeg", "-y"]

    if enable_progress:
        # Machine-readable progress (key=value lines) on stdout,
        # separate from the human-readable logs on stderr.
        command += ["-progress", "pipe:1", "-nostats", "-loglevel", "error"]

    if seek is not None:
        command += ["-ss", f"{seek:.2f}"]

    # -threads BEFORE -i limits the decoder (the one after the filters limits the encoder).
    command += ["-threads", DECODER_THREADS, "-i", input_path]

    if duration is not None:
        command += ["-t", f"{duration:.2f}"]

    x264_params = (
        f"rc-lookahead={settings['lookahead']}:"
        f"ref={settings['refs']}:"
        f"bframes={settings['bframes']}"
    )

    video_filter = get_video_filter(settings["max_width"])
    if settings.get("max_fps"):
        # Smart compression: drop to a lower frame rate BEFORE scaling (cheaper).
        video_filter = f"fps={settings['max_fps']}," + video_filter

    command += [
        "-c:v", "libx264",
        "-preset", settings["preset"],
        "-crf", settings["crf"],
        "-x264-params", x264_params,
        "-threads", FFMPEG_THREADS,
        "-vf", video_filter,
    ]

    if copy_audio:
        # Keep the original audio exactly as is (filters can't apply to a copied stream).
        command += ["-c:a", "copy"]
    else:
        command += ["-c:a", "aac", "-b:a", audio_bitrate or settings["audio_bitrate"]]

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


class JobCancelled(Exception):
    """The person pressed Cancel while their video was being processed."""


# The job whose FFmpeg is running (set per task, so concurrent people don't mix).
CURRENT_JOB: contextvars.ContextVar = contextvars.ContextVar("current_job", default=None)
running_jobs: dict[int, dict] = {}   # message id -> {"user_id", "cancelled", "process", "done"}


def cancel_job_keyboard(message_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton(BTN_CANCEL, callback_data=f"canceljob:_:{message_id}")]])


async def handle_cancel_job(query, user_id: int | None) -> None:
    """The Cancel button under a progress bar: stop FFmpeg for that job."""
    try:
        source_id = int(query.data.split(":", 2)[2])
    except (IndexError, ValueError):
        await query.answer()
        return

    job = running_jobs.get(source_id)

    if job is None or job.get("done"):
        await query.answer(MSG_TOO_LATE_TO_CANCEL, show_alert=True)
        return

    if job.get("user_id") != user_id and not is_admin(user_id):
        await query.answer(MSG_NOT_YOUR_JOB, show_alert=True)
        return

    job["cancelled"] = True
    process = job.get("process")

    if process is not None and process.returncode is None:
        with contextlib.suppress(ProcessLookupError):
            process.kill()

    await query.answer(MSG_CANCELLING_JOB)


class AdminJobWatcher:
    """
    One message to the admin per job: who is compressing, and how far along it is.
    The same message is edited as the job runs and ends as done / cancelled / failed.
    Never raises — a problem here must not affect the person's job. Nothing is
    sent when the person IS the admin (they already see their own progress bar).
    """

    def __init__(self, bot, user, kind: str) -> None:
        self.bot = bot
        self.kind = kind
        self.enabled = bool(ADMIN_USER_ID) and user is not None and not is_admin(getattr(user, "id", None))
        self.header = ""
        self.message_id: int | None = None
        self.last_text: str | None = None
        self.last_percent = -100.0
        self.last_edit = 0.0

        if self.enabled:
            self.header = MSG_ADMIN_JOB_HEADER.format(
                name=user_display_name(user),
                username=format_username(getattr(user, "username", None)),
                user_id=user.id,
            )

    async def _show(self, line: str) -> None:
        if not self.enabled:
            return

        text = f"{self.header}\n{line}"

        if text == self.last_text:
            return

        self.last_edit = time.monotonic()   # also throttles retries when Telegram is failing

        try:
            if self.message_id is None:
                sent = await asyncio.wait_for(
                    self.bot.send_message(chat_id=ADMIN_USER_ID, text=text), timeout=10
                )
                self.message_id = sent.message_id
            else:
                await asyncio.wait_for(
                    self.bot.edit_message_text(
                        chat_id=ADMIN_USER_ID, message_id=self.message_id, text=text
                    ),
                    timeout=10,
                )
            self.last_text = text
        except Exception:
            logger.debug("Couldn't update the admin's job message", exc_info=True)

    async def start(self) -> None:
        self.last_percent = 0.0
        await self._show(MSG_ADMIN_JOB_PROGRESS.format(kind=self.kind, percent=0))

    async def progress(self, percent: float) -> None:
        if not self.enabled:
            return

        now = time.monotonic()

        if percent < 100:
            if now - self.last_edit < 3:
                return
            if percent - self.last_percent < 5 and now - self.last_edit < 10:
                return

        self.last_percent = percent
        await self._show(MSG_ADMIN_JOB_PROGRESS.format(kind=self.kind, percent=percent))

    async def finish(self, outcome: tuple[str, str] | None) -> None:
        """outcome = (\"done\" | \"cancelled\" | \"failed\", refunded size text or \"\")."""
        if not self.enabled or self.message_id is None:
            return

        state, refunded = outcome or ("stopped", "")
        refund_note = MSG_ADMIN_JOB_REFUND_NOTE.format(size=refunded) if refunded else ""

        if state == "done":
            line = MSG_ADMIN_JOB_DONE.format(kind=self.kind)
        elif state == "cancelled":
            line = MSG_ADMIN_JOB_CANCELLED.format(kind=self.kind, refund_note=refund_note)
        elif state == "failed":
            line = MSG_ADMIN_JOB_FAILED.format(kind=self.kind, refund_note=refund_note)
        else:
            line = MSG_ADMIN_JOB_STOPPED.format(kind=self.kind)

        await self._show(line)


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

    if FFMPEG_NICE > 0 and shutil.which("nice"):
        command = ["nice", "-n", str(FFMPEG_NICE), *command]

    job = CURRENT_JOB.get()

    if job is not None and job.get("cancelled"):
        raise JobCancelled()

    process = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    if job is not None:
        job["process"] = process
        if job.get("cancelled"):   # they pressed Cancel while it was starting
            with contextlib.suppress(ProcessLookupError):
                process.kill()

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

    if job is not None:
        job["process"] = None
        if job.get("cancelled"):
            raise JobCancelled()

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
    title: str | None = None,
    album: str | None = None,
    cover_path: str | None = None,
    artist: str | None = None,
) -> None:
    """
    Re-encode an audio-only file at the given bitrate (and optional
    volume boost / noise reduction / voice enhancement) — no video
    stream involved. Same progress-streaming setup as compress_video,
    via the shared run_ffmpeg_with_progress.

    Optional tags: title / album are written into the file's metadata,
    and cover_path (a JPEG) is embedded as the album art. If embedding
    the cover somehow fails, it retries once without it rather than
    losing the whole job.
    """

    def build_command(with_cover: bool) -> list[str]:
        command = ["ffmpeg", "-y"]

        if progress_callback is not None and total_duration:
            command += ["-progress", "pipe:1", "-nostats", "-loglevel", "error"]

        command += ["-i", input_path]

        if with_cover:
            command += [
                "-i", str(cover_path),
                "-map", "0:a:0",
                "-map", "1:v:0",
                "-c:v", "copy",
                "-disposition:v:0", "attached_pic",
            ]
        else:
            command += ["-vn"]

        command += ["-c:a", "aac", "-b:a", audio_bitrate]

        audio_filter_chain = build_audio_filter_chain(noise_reduction, voice_enhancement, volume_boost_percent)
        if audio_filter_chain:
            command += ["-af", audio_filter_chain]

        if title:
            command += ["-metadata", f"title={title}"]
        if artist:
            command += ["-metadata", f"artist={artist}"]
        if album:
            command += ["-metadata", f"album={album}"]

        command.append(output_path)
        return command

    if cover_path and Path(cover_path).exists():
        command = build_command(True)
        logger.info("Running FFmpeg (audio-only, with cover): %s", " ".join(command))
        try:
            await run_ffmpeg_with_progress(command, total_duration, progress_callback)
            return
        except RuntimeError:
            logger.warning("Embedding the cover failed — retrying without it.")

    command = build_command(False)
    logger.info("Running FFmpeg (audio-only): %s", " ".join(command))

    await run_ffmpeg_with_progress(command, total_duration, progress_callback)



# Containers where a plain stream copy + tags works, mapped to the file
# extension they should be saved with. Telegram plays only MP3 / M4A as music.
AUDIO_COPY_EXTENSIONS = {
    "mp3": ".mp3", "flac": ".flac", "wav": ".wav", "asf": ".wma",
    "aac": ".m4a",   # raw AAC (ADTS) can't hold tags: it is re-wrapped into M4A, still no re-encode
}
AUDIO_PLAYABLE_EXTENSIONS = (".mp3", ".m4a")


async def detect_audio_extension(input_path: str, filename: str | None) -> str:
    """Pick the output extension from the REAL container (voice notes are named .m4a but are Ogg)."""
    command = [
        "ffprobe", "-v", "error",
        "-show_entries", "format=format_name:stream=codec_name",
        "-select_streams", "a:0",
        "-of", "json", input_path,
    ]
    returncode, stdout, _ = await run_command(command)

    if returncode == 0:
        try:
            data = json.loads(stdout.decode(errors="replace") or "{}")
            format_names = (data.get("format", {}).get("format_name") or "").lower().split(",")
            codec = ((data.get("streams") or [{}])[0].get("codec_name") or "").lower()

            for name, extension in AUDIO_COPY_EXTENSIONS.items():
                if name in format_names:
                    return extension
            if "mov" in format_names or "mp4" in format_names or "m4a" in format_names:
                return ".m4a"
            if "ogg" in format_names:
                return ".opus" if codec == "opus" else ".ogg"
            if "matroska" in format_names or "webm" in format_names:
                return ".mka"
        except Exception:
            logger.exception("Couldn't parse the ffprobe output for the audio format")

    suffix = Path(filename or "").suffix.lower()
    return suffix if suffix in AUDIO_FILE_EXTENSIONS else ".m4a"


async def tag_audio_without_compression(
    input_path: str,
    output_path: str,
    total_duration: float | None = None,
    progress_callback=None,
    title: str | None = None,
    artist: str | None = None,
    album: str | None = None,
    cover_path: str | None = None,
    source_is_video: bool = False,
) -> bool:
    """
    Copy the audio stream as it is (no re-encode, so no quality loss) and
    write the new tags into the file. Tags the user didn't change keep the
    values the file already had; an existing embedded cover is kept unless
    a new one was set. Returns True if the new cover was embedded in the
    file (False if there was none, or the format can't hold one — the job
    is retried without it rather than failing).
    """

    extension = Path(output_path).suffix.lower()

    def build_command(with_cover: bool) -> list[str]:
        command = ["ffmpeg", "-y"]

        if progress_callback is not None and total_duration:
            command += ["-progress", "pipe:1", "-nostats", "-loglevel", "error"]

        command += ["-i", input_path]

        if with_cover:
            command += [
                "-i", str(cover_path),
                "-map", "0:a:0", "-map", "1:v:0",
                "-c", "copy",
                "-disposition:v:0", "attached_pic",
            ]
            if extension == ".mp3":
                command += ["-id3v2_version", "3", "-metadata:s:v", "title=Album cover", "-metadata:s:v", "comment=Cover (front)"]
        else:
            # Keep the file's own embedded cover (if any) when no new one was set.
            # (A video's own picture must never be copied into the audio file.)
            command += ["-map", "0:a:0"] + ([] if source_is_video else ["-map", "0:v?"]) + ["-c", "copy"]
            if extension == ".mp3":
                command += ["-id3v2_version", "3"]

        command += ["-map_metadata", "0"]

        if title:
            command += ["-metadata", f"title={title}"]
        if artist:
            command += ["-metadata", f"artist={artist}"]
        if album:
            command += ["-metadata", f"album={album}"]

        command.append(output_path)
        return command

    if cover_path and Path(cover_path).exists():
        command = build_command(True)
        logger.info("Running FFmpeg (audio tags, no re-encode, with cover): %s", " ".join(command))
        try:
            await run_ffmpeg_with_progress(command, total_duration, progress_callback)
            return True
        except RuntimeError:
            logger.warning("Embedding the cover failed — retrying without it.")

    command = build_command(False)
    logger.info("Running FFmpeg (audio tags, no re-encode): %s", " ".join(command))
    try:
        await run_ffmpeg_with_progress(command, total_duration, progress_callback)
    except RuntimeError:
        # Last resort: audio stream only (some containers reject the extra stream).
        fallback = ["ffmpeg", "-y", "-i", input_path, "-map", "0:a:0", "-c", "copy", "-map_metadata", "0"]
        if title:
            fallback += ["-metadata", f"title={title}"]
        if artist:
            fallback += ["-metadata", f"artist={artist}"]
        if album:
            fallback += ["-metadata", f"album={album}"]
        fallback.append(output_path)
        logger.info("Running FFmpeg (audio tags, audio stream only): %s", " ".join(fallback))
        await run_ffmpeg_with_progress(fallback, total_duration, progress_callback)

    return False


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
    max_fps: int | None = None,
    copy_audio: bool = False,
) -> None:
    """
    Compress a video with FFmpeg. If total_duration and a
    progress_callback are supplied, streams real encode progress
    (based on how much of the video's timeline has been processed)
    to the callback as it happens. audio_bitrate overrides the
    level's default audio bitrate if given; volume_boost_percent
    applies a volume filter on top (0 = no change).
    """

    settings = dict(LEVELS[level])
    if max_fps:
        settings["max_fps"] = max_fps

    command = build_ffmpeg_command(
        input_path,
        output_path,
        settings,
        enable_progress=True,
        audio_bitrate=audio_bitrate,
        volume_boost_percent=volume_boost_percent,
        noise_reduction=noise_reduction,
        voice_enhancement=voice_enhancement,
        copy_audio=copy_audio,
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
    any extra image-processing dependency. Grid is 64x36 (was 24x24) —
    more sample points makes the comparison more sensitive to real
    differences between frames (a changed line of text on an otherwise
    identical slide now shows up), so visually-different frames are
    much less likely to get averaged down into looking "near-identical".
    """

    command = [
        "ffmpeg", "-y",
        "-i", frame_path,
        "-vf", "scale=64:36:flags=area,format=gray",
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
    best_means = (0.0, 0.0)

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
            best_means = (mean_low, mean_high)

    if best_split is None:
        return DEDUP_SIMILARITY_THRESHOLD

    # This search ALWAYS finds a split, even when the video has no duplicates
    # at all (every diff is a real change) -- it would then label the smaller
    # real changes as "duplicates" and delete real slides. So only trust the
    # split when the low group really is noise (near zero) and clearly apart
    # from the high group; otherwise use the safe fixed default.
    mean_low, mean_high = best_means

    if mean_low > DEDUP_MAX_LOW_GROUP_MEAN or (mean_high - mean_low) < DEDUP_MIN_GROUP_GAP:
        return DEDUP_SIMILARITY_THRESHOLD

    # Never go above the cap: anything bigger than noise is a real change.
    return min(max(best_split, DEDUP_MIN_THRESHOLD), DEDUP_MAX_THRESHOLD)


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
    """'10s' normally, or the whole video's length if it's shorter than that."""
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
            MSG_SAMPLE_AT_EACH_COMPRESSION_LEVEL.format(sample_length_duration=sample_length_text(duration))
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
                    MSG_SAMPLE_FULL_VIDEO.format(label=info['label'], detail=info['detail'], size_sample_size=format_size(sample_size), size_estimates_level=format_size(estimates.get(level)))
                ),
            )
        except Exception:
            logger.exception("Couldn't send %s sample clip", level)
        finally:
            sample_path.unlink(missing_ok=True)
            thumb_path.unlink(missing_ok=True)


# ============================================================
# Smart compression: probe the source, then recommend settings
# ============================================================

# How much bitrate a codec needs for the same visual quality, relative
# to H.264 (1.0). HEVC at 3 Mbps looks like H.264 at ~4.6 Mbps, etc.
CODEC_EFFICIENCY = {
    "h264": 1.0, "hevc": 0.65, "vp9": 0.7, "av1": 0.55,
    "vp8": 1.2, "mpeg4": 1.6, "msmpeg4v3": 1.8, "mpeg2video": 2.0,
    "wmv3": 1.6, "vc1": 1.5, "mjpeg": 3.0,
}
CODEC_NAMES = {
    "h264": "H.264", "hevc": "H.265/HEVC", "vp9": "VP9", "av1": "AV1",
    "vp8": "VP8", "mpeg4": "MPEG-4", "mpeg2video": "MPEG-2",
}

# Bits per pixel per frame (H.264-equivalent) that split the verdicts.
SMART_PLENTY_BPP = 0.12
SMART_MODERATE_BPP = 0.07
SMART_COMPACT_BPP = 0.04

# The gentlest level whose estimate is at most this share of the
# original wins — so a bloated video gets a real cut, while one that is
# already compact only gets a modest one.
SMART_TARGET_RATIO = {"plenty": 0.35, "moderate": 0.60, "compact": 0.85, "tiny": 0.95}

SMART_VERDICT_TEXT = {
    "plenty": MSG_LOTS_OF_ROOM_TO_SHRINK,
    "moderate": MSG_SOME_ROOM_TO_SHRINK,
    "compact": MSG_ALREADY_FAIRLY_COMPACT_MODEST_SAVING,
    "tiny": MSG_ALREADY_VERY_COMPACT_RECOMPRESSING_M,
}

# Above this frame rate (50/60 fps) the smart mode caps to SMART_FPS_CAP,
# except at Low, which is the "keep everything" level.
SMART_FPS_THRESHOLD = 45
SMART_FPS_CAP = 30


def parse_frame_rate(value) -> float | None:
    try:
        numerator, denominator = str(value).split("/")
        numerator, denominator = float(numerator), float(denominator)
        return numerator / denominator if denominator else None
    except (ValueError, AttributeError):
        return None


def _to_int(value) -> int | None:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _to_float(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


async def probe_video_info(input_path: str) -> dict | None:
    """One ffprobe call: resolution, fps, codec, bitrates, HDR, audio details."""
    command = [
        "ffprobe", "-v", "error", "-print_format", "json",
        "-show_streams", "-show_format", input_path,
    ]

    returncode, stdout, stderr = await run_command(command)

    if returncode != 0:
        logger.warning("ffprobe (full) failed: %s", stderr.decode(errors="replace"))
        return None

    try:
        data = json.loads(stdout.decode(errors="replace"))
    except ValueError:
        return None

    streams = data.get("streams") or []
    video = next(
        (s for s in streams
         if s.get("codec_type") == "video" and not (s.get("disposition") or {}).get("attached_pic")),
        None,
    )
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)

    if video is None:
        return None

    fmt = data.get("format") or {}
    width, height = _to_int(video.get("width")), _to_int(video.get("height"))

    # Phone videos are often stored sideways with a rotation flag.
    rotation = None
    for side in video.get("side_data_list") or []:
        if "rotation" in side:
            rotation = _to_int(side.get("rotation"))
    if rotation is None:
        rotation = _to_int((video.get("tags") or {}).get("rotate"))
    if rotation is not None and abs(rotation) % 180 == 90 and width and height:
        width, height = height, width

    total_bps = _to_int(fmt.get("bit_rate"))
    video_bps = _to_int(video.get("bit_rate"))
    audio_bps = _to_int(audio.get("bit_rate")) if audio else None

    if video_bps is None and total_bps:
        video_bps = max(total_bps - (audio_bps or (128_000 if audio else 0)), int(total_bps * 0.5))

    transfer = (video.get("color_transfer") or "").lower()

    return {
        "width": width,
        "height": height,
        "fps": parse_frame_rate(video.get("avg_frame_rate")) or parse_frame_rate(video.get("r_frame_rate")),
        "codec": (video.get("codec_name") or "").lower(),
        "pix_fmt": video.get("pix_fmt") or "",
        "hdr": transfer in ("smpte2084", "arib-std-b67"),
        "video_bps": video_bps,
        "total_bps": total_bps,
        "has_audio": audio is not None,
        "audio_codec": (audio.get("codec_name") or "").lower() if audio else "",
        "audio_bps": audio_bps,
        "channels": _to_int(audio.get("channels")) if audio else None,
    }


def smart_analyze(
    info: dict | None,
    original_size: int | None,
    duration: float | None,
    estimates: dict[str, int | None],
) -> dict:
    """Turn the probe + the per-level size estimates into one recommendation."""
    info = dict(info or {})
    width, height, fps = info.get("width"), info.get("height"), info.get("fps")
    codec = info.get("codec") or ""
    video_bps = info.get("video_bps")
    bitrate_estimated = False
    probed = bool(width and height)

    if probed and not video_bps and original_size and duration and duration > 0:
        total_bps = original_size * 8 / duration
        audio_guess = (info.get("audio_bps") or 128_000) if info.get("has_audio") else 0
        video_bps = max(total_bps - audio_guess, total_bps * 0.5)
        bitrate_estimated = True
        info["video_bps"] = int(video_bps)

    bpp = eq_bpp = None
    if video_bps and width and height and fps:
        bpp = video_bps / (width * height * fps)
        eq_bpp = bpp / CODEC_EFFICIENCY.get(codec, 1.0)

    if eq_bpp is None:
        verdict = "moderate"
    elif eq_bpp >= SMART_PLENTY_BPP:
        verdict = "plenty"
    elif eq_bpp >= SMART_MODERATE_BPP:
        verdict = "moderate"
    elif eq_bpp >= SMART_COMPACT_BPP:
        verdict = "compact"
    else:
        verdict = "tiny"

    # --- video level: the gentlest one that reaches the target saving ---
    worthwhile = True
    usable = {key: est for key, est in estimates.items() if est}

    if usable and original_size:
        level = next(
            (key for key in LEVEL_ORDER
             if usable.get(key) and usable[key] <= original_size * SMART_TARGET_RATIO[verdict]),
            None,
        )
        if level is None:
            # Nothing reaches the target: take the gentlest level that still
            # shrinks the file at all; if none does, re-encoding isn't worth it.
            level = next(
                (key for key in LEVEL_ORDER
                 if usable.get(key) and usable[key] < original_size * 0.97),
                None,
            )
            if level is None:
                level = "medium"
                worthwhile = False
    else:
        level = {"plenty": "medium", "moderate": "medium", "compact": "medium", "tiny": "high"}[verdict]

    # --- audio: never ask for more than the source has ---
    source_kbps = (info.get("audio_bps") or 0) / 1000
    if not info.get("has_audio") or not source_kbps:
        audio_level = DEFAULT_AUDIO_LEVEL
    elif source_kbps <= 56:
        audio_level = "low"
    else:
        audio_level = "medium"

    # --- frame rate ---
    max_fps = SMART_FPS_CAP if (fps and fps > SMART_FPS_THRESHOLD and level != "low") else None

    # --- expected size ---
    est_size = usable.get(level)
    if est_size is not None:
        default_audio = estimate_audio_bytes(LEVELS[level]["audio_bitrate"], duration) or 0
        chosen_audio = estimate_audio_bytes(AUDIO_LEVELS[audio_level]["bitrate"], duration) or 0
        est_size = max(0, est_size - default_audio + chosen_audio)
        if max_fps:
            est_size = int(est_size * 0.8)

    saved_pct = (
        (original_size - est_size) / original_size * 100
        if est_size is not None and original_size else None
    )

    notes: list[str] = []
    warnings: list[str] = []

    settings = LEVELS[level]
    if width and width > settings["max_width"] and height:
        out_w = settings["max_width"]
        out_h = int(round(height * out_w / width / 2) * 2)
        notes.append(MSG_RESOLUTION.format(width=width, height=height, out_w=out_w, out_h=out_h))
    elif width and height:
        notes.append(f"Keeps the {width}×{height} resolution")

    if max_fps:
        notes.append(MSG_FPS_CAPPED_AT_FPS_SMALLER.format(fps=fps, SMART_FPS_CAP=SMART_FPS_CAP))

    if codec in ("hevc", "av1", "vp9"):
        notes.append(
            f"The source already uses {CODEC_NAMES.get(codec, codec.upper())}, which is more efficient "
            "than H.264 — so savings are smaller than the file size suggests"
        )

    if info.get("hdr"):
        warnings.append("HDR video: the output will be standard (SDR), so colors can look flatter")

    if not worthwhile:
        warnings.append("Re-encoding likely won't make this file smaller — keep the original unless you need a different size or format")

    if bitrate_estimated:
        notes.append("Bitrate estimated from file size and length")

    return {
        "level": level, "audio_level": audio_level, "max_fps": max_fps,
        "verdict": verdict, "worthwhile": worthwhile,
        "bpp": bpp, "eq_bpp": eq_bpp,
        "est_size": est_size, "saved_pct": saved_pct,
        "info": info, "notes": notes, "warnings": warnings,
    }


def _bitrate_text(bps: float | None) -> str:
    if not bps:
        return "unknown bitrate"
    if bps >= 1_000_000:
        return f"{bps / 1_000_000:.1f} Mbps"
    return f"{bps / 1000:.0f} kbps"


def smart_analysis_text(rec: dict) -> str:
    info = rec["info"]
    lines = [MSG_SMART_ANALYSIS]

    if info.get("width") and info.get("height"):
        codec = info.get("codec") or "?"
        fps_text = f"{info['fps']:.0f} fps" if info.get("fps") else "? fps"
        lines.append(
            MSG_SMART_ANALYSIS_TEXT.format(width=info['width'], height=info['height'], fps_text=fps_text, codec=CODEC_NAMES.get(codec, codec.upper()), bitrate_text_video_bps=_bitrate_text(info.get('video_bps')))
        )
    else:
        lines.append(MSG_COULDN_T_READ_THIS_VIDEO)

    if not info.get("width"):
        pass
    elif info.get("has_audio"):
        channels = {1: "mono", 2: "stereo"}.get(info.get("channels"), f"{info.get('channels') or '?'} ch")
        lines.append(
            MSG_SMART_ANALYSIS_TEXT_5.format(audio_codec=(info.get('audio_codec') or '?').upper(), audio_bps_choice=_bitrate_text(info.get('audio_bps')) if info.get('audio_bps') else 'bitrate unknown', channels=channels)
        )
    else:
        lines.append(MSG_NO_AUDIO_TRACK)

    if rec.get("eq_bpp") is not None:
        lines.append(
            MSG_BITS_PER_PIXEL_AS_H.format(eq_bpp=rec['eq_bpp'], smart_verdict_text_verdict=SMART_VERDICT_TEXT[rec['verdict']])
        )

    level_info = LEVELS[rec["level"]]
    audio_info = AUDIO_LEVELS[rec["audio_level"]]
    lines.append(MSG_RECOMMENDED_AUDIO.format(level_info_label=level_info['label'], audio_info_label=audio_info['label']))

    if rec.get("est_size") is not None:
        saved = f" (−{rec['saved_pct']:.0f}%)" if rec.get("saved_pct") is not None and rec["saved_pct"] > 0 else ""
        lines.append(MSG_SMART_ANALYSIS_TEXT_2.format(size_est_size=format_size(rec['est_size']), saved=saved))

    for note in rec["notes"]:
        lines.append(MSG_SMART_ANALYSIS_TEXT_3.format(note=note))
    for warning in rec["warnings"]:
        lines.append(MSG_SMART_ANALYSIS_TEXT_4.format(warning=warning))

    return "\n".join(lines)


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
            MSG_ENCODING_A_SAMPLE_AT_EACH_2.format(sample_label=sample_label, done=done, total=total)
        )

    await status_message.edit_text(
        MSG_ENCODING_A_SAMPLE_AT_EACH.format(sample_label=sample_label, count_level_order=len(LEVEL_ORDER))
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

    # Smart compression: probe the source and recommend settings.
    smart_rec = None
    try:
        smart_rec = smart_analyze(
            await probe_video_info(str(input_path)), original_size, duration, estimates
        )
        if pending is not None:
            pending["smart"] = smart_rec
    except Exception:
        logger.exception("Smart analysis failed — showing the normal menu")

    lines = [MSG_ORIGINAL_SIZE.format(size_original_size=format_size(original_size)), ""]
    for level in LEVEL_ORDER:
        info = LEVELS[level]
        lines.append(MSG_ANALYZE_AND_SHOW_LEVEL_MENU.format(label=info['label'], size_estimates_level=format_size(estimates.get(level)), detail=info['detail']))

    if smart_rec is not None:
        lines.append("")
        lines.append(smart_analysis_text(smart_rec))

    lines.append("")
    lines.append(MSG_ESTIMATES_ARE_BASED_ON_THE.format(sample_label=sample_label))
    lines.append(MSG_PICK_A_LEVEL_TO_COMPRESS)

    menu_text = "\n".join(lines)

    if pending is not None:
        # Kept so the "Go back" button can bring this exact menu back.
        pending["menu_text"] = menu_text
        pending["default_level"] = default_level

    try:
        await context.bot.send_message(
            chat_id=chat_id,
            text=menu_text,
            reply_markup=estimate_keyboard(
                message_id, estimates, default_level, smart_rec, original_size
            ),
        )
    except BadRequest:
        # Maybe this Bot API server doesn't know button colours: send it plain.
        logger.warning("Styled level menu failed — sending a plain one", exc_info=True)
        await context.bot.send_message(
            chat_id=chat_id,
            text=menu_text,
            reply_markup=estimate_keyboard(
                message_id, estimates, default_level, smart_rec, original_size, styled=False
            ),
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

    await update.message.reply_text(home_text(update.effective_user), reply_markup=home_keyboard())


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
        MSG_PICK_THE_LEVEL_THAT_SHOULD,
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

    # Merge mode: every video this chat sends is added to the merge list
    # instead of being compressed.
    if chat_id in merge_sessions:
        if media_type == "video":
            await merge_add_video(update, context, file_id, original_size, filename)
        else:
            await message.reply_text(MSG_MERGE_VIDEOS_ONLY)
        return

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

    # /update mode: no NEW files (the admin is exempt).
    if update_mode["on"] and not is_admin(update.effective_user.id if update.effective_user else None):
        await message.reply_text(UPDATE_BUSY_TEXT)
        return

    # Daily data limit: the size of every file they send counts. Refuse up front
    # (before downloading anything) if it doesn't fit in what's left of today's allowance.
    sender_id = update.effective_user.id if update.effective_user else None
    charged_bytes = int(original_size or 0)

    if file_too_large(sender_id, charged_bytes):
        await message.reply_text(file_too_large_text(sender_id, charged_bytes))
        return

    if not quota_allows(sender_id, charged_bytes):
        await message.reply_text(quota_refusal_text(sender_id, charged_bytes))
        return

    quota_consume(sender_id, charged_bytes)

    # Sending a new file means they moved on from any half-finished
    # "send me the name / album / cover" prompt.
    awaiting_input.pop(chat_id, None)

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

    slot_held = False
    status_message = None

    async def post_status(text: str) -> None:
        nonlocal status_message
        if status_message is None:
            status_message = await message.reply_text(text)
        else:
            await status_message.edit_text(text)

    active_job_ids.add(message.message_id)

    try:
        size_text = format_size(original_size)

        # Wait in line if other files are being downloaded/analyzed right now.
        await acquire_work_slot(post_status, user_priority(sender_id), user_plan_label(sender_id))
        slot_held = True

        await post_status(MSG_DOWNLOADING.format(kind_label=kind_label, size_text=size_text))

        await context.bot.send_chat_action(
            chat_id=chat_id,
            action=ChatAction.UPLOAD_VOICE if is_audio else ChatAction.UPLOAD_VIDEO,
        )

        telegram_file = await with_retries(context.bot.get_file, file_id)
        await with_retries(telegram_file.download_to_drive, custom_path=str(input_path))
        delete_server_copy(telegram_file)

        downloaded_size = input_path.stat().st_size

        if charged_bytes == 0 and downloaded_size > 0:
            # Telegram didn't tell us the size up front: charge the real size now.
            if file_too_large(sender_id, downloaded_size):
                cleanup_work_dir(work_dir)
                await message.reply_text(file_too_large_text(sender_id, downloaded_size))
                return

            if not quota_allows(sender_id, downloaded_size):
                cleanup_work_dir(work_dir)
                await message.reply_text(quota_refusal_text(sender_id, downloaded_size))
                return

            quota_consume(sender_id, downloaded_size)
            charged_bytes = downloaded_size

        duration = await get_duration(str(input_path))
        record_received(media_type)

        if is_audio:
            # No video stream to compress — audio-only files skip
            # straight to the audio quality menu (same one the video
            # flow shows after a video level is picked).
            audio_estimates = {
                audio_key: estimate_audio_bytes(AUDIO_LEVELS[audio_key]["bitrate"], duration)
                for audio_key in AUDIO_LEVEL_ORDER
            }

            evicted = remember_pending(
                message.message_id,
                {
                    "input_path": input_path,
                    "work_dir": work_dir,
                    "original_size": downloaded_size,
                    "filename": filename,
                    "chat_id": chat_id,
                    "duration": duration,
                    "media_type": "audio",
                    "owner_id": sender_id,           # who was charged ...
                    "charged_bytes": charged_bytes,  # ... and how much (for refunds)
                },
            )
            await notify_evicted(context.bot, evicted)

            await status_message.edit_text(
                MSG_ORIGINAL_SIZE_PICK_AUDIO_QUALITY.format(size_downloaded_size=format_size(downloaded_size)),
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
            "owner_id": sender_id,           # who was charged ...
            "charged_bytes": charged_bytes,  # ... and how much (for refunds)
        }
        await notify_evicted(context.bot, remember_pending(message.message_id, pending_entry))

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
        quota_refund(sender_id, charged_bytes)   # nothing came of it: give the bytes back
        cleanup_work_dir(work_dir)
        pending_compressions.pop(message.message_id, None)
        await message.reply_text(
            MSG_SOMETHING_WENT_WRONG_WHILE_ANALYZING.format(kind_label=kind_label, error=error)
        )

    finally:
        if slot_held:
            release_work_slot()
        active_job_ids.discard(message.message_id)
        await maybe_notify_update_ready(context.bot)


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
    max_fps: int | None = None,
    smart: bool = False,
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

    # Optional extras the user attached through the menus (rename /
    # album / cover). Read now — clear_pending() wipes them at the end.
    extras = pending_compressions.get(source_message_id) or {}
    custom_name = sanitize_filename(extras.get("out_name"))
    custom_album = extras.get("album")
    custom_artist = extras.get("artist")
    cover_path = extras.get("cover_path")
    cover_thumb_path = extras.get("cover_thumb_path")
    cover_embedded = False

    is_audio_copy = level == AUDIO_COPY_LEVEL
    is_audio_only = level is None
    is_video_audio_edit = level == AUDIO_EDIT_LEVEL
    is_video_audio_replace = level == AUDIO_REPLACE_LEVEL
    is_cover_only = level == COVER_ONLY_LEVEL
    level_info = (
        None
        if (is_audio_only or is_audio_copy or is_video_audio_edit or is_video_audio_replace or is_cover_only)
        else LEVELS[level]
    )
    copy_audio = False
    if audio_level == ORIGINAL_AUDIO:
        if level_info is None:
            # Audio-only / audio edit / replace: there is nothing to "keep".
            audio_level = DEFAULT_AUDIO_LEVEL
        else:
            source_info = (extras.get("smart") or {}).get("info") or await probe_video_info(str(input_path))

            if source_info and (
                source_info.get("has_audio") is False
                or source_info.get("audio_codec") in MP4_COPY_SAFE_AUDIO
            ):
                copy_audio = True
            else:
                # The audio format can't go into an MP4 as is (or the probe failed):
                # re-encode at the best audio quality instead of failing.
                audio_level = "high"
                extra_caption = (
                    (extra_caption + "\n" if extra_caption else "")
                    + MSG_THAT_AUDIO_FORMAT_CAN_T
                )

    audio_info = AUDIO_LEVELS[audio_level]
    audio_bitrate = audio_info["bitrate"]
    if is_audio_copy:
        audio_info = AUDIO_LEVELS[ORIGINAL_AUDIO]   # the sound is copied, not re-encoded
        audio_bitrate = None
    # Local-mode uploads use the file's own name on disk, so a rename
    # has to be the real output file name.
    output_path = work_dir / (
        f"{custom_name or 'compressed'}.m4a" if is_audio_only else "compressed.mp4"
    )
    if is_cover_only:
        # Nothing is encoded: the original file itself is what gets uploaded.
        output_path = Path(input_path)
    if is_audio_copy:
        # Keep the real container (and so the real extension); the file keeps
        # its original name unless the user renamed it.
        copy_extension = extras.get("extract_copy_ext") if extras.get("extract_audio") else None
        if not copy_extension:
            copy_extension = await detect_audio_extension(str(input_path), extras.get("filename"))
        copy_base = custom_name or sanitize_filename(Path(extras.get("filename") or "").stem) or "audio"
        output_path = work_dir / f"{copy_base}{copy_extension}"

    job_token = object()
    queue_refresh_task = None

    stats_user = update.effective_user if update is not None else None

    # Who gets the bytes back if this job is cancelled / fails: the person who was
    # charged when the file arrived (not necessarily whoever pressed the button).
    refund_user_id = extras.get("owner_id") or (stats_user.id if stats_user is not None else None)
    refund_bytes = int(extras.get("charged_bytes") or original_size or 0)

    def give_bytes_back() -> str:
        """Refund the daily-limit bytes. Returns the size text, or "" if nothing was refunded."""
        if quota_applies(refund_user_id) and refund_bytes > 0:
            quota_refund(refund_user_id, refund_bytes)
            return format_size(refund_bytes)
        return ""

    # Live "who is compressing what" message for the admin.
    admin_watch = AdminJobWatcher(
        context.bot,
        stats_user,
        "audio" if is_audio_only
        else "audio (tags only, no compression)" if is_audio_copy
        else "audio (video edit)" if is_video_audio_edit
        else "audio (replacement)" if is_video_audio_replace
        else "cover (video unchanged)" if is_cover_only
        else "video",
    )
    admin_outcome: tuple[str, str] | None = None

    work_slot_held = False
    active_job_ids.add(source_message_id)
    job_started = time.monotonic()
    encode_seconds: float | None = None
    queue_wait_seconds: float | None = None

    def log_success() -> None:
        nonlocal admin_outcome
        admin_outcome = ("done", "")
        try:
            record_job(
                user=stats_user,
                kind=(
                    "audio" if is_audio_only
                    else "audio_copy" if is_audio_copy
                    else "audio_edit" if is_video_audio_edit
                    else "audio_replace" if is_video_audio_replace
                    else "cover_only" if is_cover_only
                    else "video"
                ),
                level_label=level_info["label"] if level_info else None,
                original_size=original_size,
                compressed_size=compressed_size,
                media_seconds=duration,
                encode_seconds=encode_seconds,
                wait_seconds=queue_wait_seconds,
                total_seconds=time.monotonic() - job_started,
                audio_label=audio_info["label"],
                boost=volume_boost_percent,
                noise_reduction=noise_reduction,
                voice_enhancement=voice_enhancement,
                smart=smart,
            )
        except Exception:
            logger.exception("Couldn't record the job in the statistics")

    try:
        # One heavy job at a time (shared with downloads/analysis): wait here,
        # with a status message, while another job holds the slot. The slot is
        # kept until this job has sent its result AND cleaned up (see `finally`).
        async def post_queue_status(text: str) -> None:
            nonlocal status_message
            if status_message is not None:
                await status_message.edit_text(text)
            else:
                status_message = await context.bot.send_message(chat_id=chat_id, text=text)

        queue_user_id = stats_user.id if stats_user is not None else None
        await acquire_work_slot(post_queue_status, user_priority(queue_user_id), user_plan_label(queue_user_id))
        work_slot_held = True

        async with contextlib.nullcontext():
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

            # Make this job cancellable (the Cancel button under the progress bar).
            job_handle = {
                "user_id": stats_user.id if stats_user is not None else None,
                "cancelled": False,
                "process": None,
                "done": False,
            }
            running_jobs[source_message_id] = job_handle
            job_context_token = CURRENT_JOB.set(job_handle)

            try:
                boost_suffix = MSG_RUN_COMPRESSION.format(volume_boost_percent=volume_boost_percent) if volume_boost_percent else ""
                enhancement_bits = []
                if noise_reduction:
                    enhancement_bits.append(MSG_NOISE_REDUCTION_3)
                if voice_enhancement:
                    enhancement_bits.append(MSG_VOICE_ENHANCEMENT_3)
                enhancement_suffix = f" · {' + '.join(enhancement_bits)}" if enhancement_bits else ""
                fps_note = f" · {max_fps} fps cap" if max_fps else ""

                if is_audio_only:
                    compressing_label = (
                        MSG_COMPRESSING_AUDIO.format(audio_info_label=audio_info['label'], boost_suffix=boost_suffix, enhancement_suffix=enhancement_suffix)
                    )
                elif is_audio_copy:
                    compressing_label = MSG_SENDING_AUDIO_AS_IS
                elif is_cover_only:
                    compressing_label = MSG_COVER_ONLY_UPLOADING
                elif is_video_audio_edit:
                    compressing_label = (
                        MSG_EDITING_AUDIO_VIDEO_UNTOUCHED.format(audio_info_label=audio_info['label'], boost_suffix=boost_suffix, enhancement_suffix=enhancement_suffix)
                    )
                elif is_video_audio_replace:
                    compressing_label = (
                        MSG_REPLACING_THE_AUDIO_VIDEO_UNTOUCHED.format(audio_info_label=audio_info['label'])
                    )
                else:
                    compressing_label = (
                        MSG_AT_AUDIO.format(smart_choice='🧠 Smart compression' if smart else 'Compressing', level_info_label=level_info['label'], fps_note=fps_note, audio_info_label=audio_info['label'], boost_suffix=boost_suffix, enhancement_suffix=enhancement_suffix)
                    )
                text = f"{compressing_label}\n{render_progress_bar(0)}"

                if status_message is not None:
                    await with_retries(status_message.edit_text, text, reply_markup=cancel_job_keyboard(source_message_id))
                else:
                    status_message = await with_retries(
                        context.bot.send_message, chat_id=chat_id, text=text,
                        reply_markup=cancel_job_keyboard(source_message_id),
                    )

                await context.bot.send_chat_action(
                    chat_id=chat_id,
                    action=ChatAction.UPLOAD_VOICE if (is_audio_only or is_audio_copy) else ChatAction.UPLOAD_VIDEO,
                )

                await admin_watch.start()

                progress_state = {
                    "last_percent": -10.0,
                    "last_edit": 0.0,
                    "start_time": time.monotonic(),
                }

                async def on_progress(percent: float) -> None:
                    active_job_progress[job_token]["percent"] = percent

                    await admin_watch.progress(percent)

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
                        await status_message.edit_text(bar_text, reply_markup=cancel_job_keyboard(source_message_id))
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
                        title=custom_name,
                        album=custom_album,
                        cover_path=cover_path,
                        artist=custom_artist,
                    )
                elif is_audio_copy:
                    cover_embedded = await tag_audio_without_compression(
                        str(input_path),
                        str(output_path),
                        total_duration=duration,
                        progress_callback=on_progress if duration else None,
                        title=custom_name,
                        artist=custom_artist,
                        album=custom_album,
                        cover_path=cover_path,
                        source_is_video=bool(extras.get("extract_audio")),
                    )
                elif is_cover_only:
                    pass   # nothing to encode: the original file is uploaded as it is
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
                        max_fps=max_fps,
                        copy_audio=copy_audio,
                        total_duration=duration,
                        progress_callback=on_progress if duration else None,
                        audio_bitrate=audio_bitrate,
                        volume_boost_percent=volume_boost_percent,
                        noise_reduction=noise_reduction,
                        voice_enhancement=voice_enhancement,
                    )
                encode_seconds = time.monotonic() - progress_state["start_time"]
                queue_wait_seconds = max(0.0, progress_state["start_time"] - job_started)
            finally:
                job_handle["done"] = True
                running_jobs.pop(source_message_id, None)
                CURRENT_JOB.reset(job_context_token)
                active_compressions -= 1
                active_job_progress.pop(job_token, None)

        compressed_size = output_path.stat().st_size

        if original_size:
            saved = original_size - compressed_size
            percentage = (saved / original_size) * 100
        else:
            percentage = 0

        if is_audio_copy:
            output_duration = await get_duration(str(output_path))

            await status_message.edit_text(MSG_UPLOADING_AUDIO_AS_IS)
            await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.UPLOAD_VOICE)

            audio_thumbnail = (
                str(cover_thumb_path)
                if cover_thumb_path and Path(cover_thumb_path).exists()
                else None
            )

            caption = MSG_AUDIO_SENT_AS_IS + "\n\n"
            if custom_artist:
                caption += MSG_ARTIST.format(custom_artist=custom_artist)
            if custom_album:
                caption += MSG_ALBUM.format(custom_album=custom_album).rstrip("\n") + "\n"
            caption += f"Size: {format_size(compressed_size)}"

            if cover_path and not cover_embedded:
                caption += "\n\n" + MSG_COVER_NOT_EMBEDDED

            if output_path.suffix.lower() in AUDIO_PLAYABLE_EXTENSIONS:
                await with_retries(
                    context.bot.send_audio,
                    chat_id=chat_id,
                    audio=str(output_path),
                    duration=round(output_duration) if output_duration else None,
                    title=custom_name,
                    performer=custom_artist,
                    thumbnail=audio_thumbnail,
                    caption=caption,
                )
            else:
                # Telegram only plays MP3 / M4A as music: other formats go out as a file, still untouched.
                await with_retries(
                    context.bot.send_document,
                    chat_id=chat_id,
                    document=str(output_path),
                    thumbnail=audio_thumbnail,
                    caption=caption + "\n\n" + MSG_SENT_AS_FILE_NOT_MP3_M4A,
                )
            log_success()
            return

        if is_audio_only:
            output_duration = await get_duration(str(output_path))

            await status_message.edit_text(MSG_SENDING_COMPRESSED_AUDIO)
            await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.UPLOAD_VOICE)

            audio_thumbnail = (
                str(cover_thumb_path)
                if cover_thumb_path and Path(cover_thumb_path).exists()
                else None
            )

            await with_retries(
                context.bot.send_audio,
                chat_id=chat_id,
                audio=str(output_path),
                duration=round(output_duration) if output_duration else None,
                title=custom_name,
                thumbnail=audio_thumbnail,
                caption=(
                    MSG_COMPRESSION_COMPLETE_AUDIO.format(audio_info_label=audio_info['label'], boost_suffix=boost_suffix, enhancement_suffix=enhancement_suffix)
                    + (MSG_ALBUM.format(custom_album=custom_album) if custom_album else "")
                    + f"Original: {format_size(original_size)}\n"
                    f"Compressed: {format_size(compressed_size)}\n"
                    f"Saved: {percentage:.1f}%"
                ),
            )
            log_success()
            return

        # Probe the actual compressed file for duration/dimensions
        # (rather than reusing the source video's numbers) and grab
        # a thumbnail frame — without these, Telegram clients show
        # a blank 00:00 preview even though the video plays fine.
        output_duration = await get_duration(str(output_path))
        output_width, output_height = await get_video_dimensions(str(output_path))

        if cover_thumb_path and Path(cover_thumb_path).exists():
            # The user sent their own cover image — use it instead of
            # a frame grabbed from the video.
            thumbnail_path = Path(cover_thumb_path)
            has_thumbnail = True
        else:
            thumbnail_path = work_dir / "thumb.jpg"
            has_thumbnail = await generate_thumbnail(str(output_path), str(thumbnail_path), output_duration)

        await status_message.edit_text(
            MSG_SENDING_EDITED_VIDEO
            if (is_video_audio_edit or is_video_audio_replace or is_cover_only)
            else MSG_SENDING_COMPRESSED_VIDEO
        )

        await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.UPLOAD_VIDEO)

        if is_cover_only:
            result_heading = MSG_COVER_CHANGED_VIDEO_UNCHANGED
        elif is_video_audio_edit:
            result_heading = MSG_AUDIO_EDITED_VIDEO_UNTOUCHED_AUDIO.format(audio_info_label=audio_info['label'], boost_suffix=boost_suffix, enhancement_suffix=enhancement_suffix)
        elif is_video_audio_replace:
            result_heading = MSG_AUDIO_REPLACED_VIDEO_UNTOUCHED_AUDIO.format(audio_info_label=audio_info['label'])
        else:
            result_heading = (
                MSG_COMPLETE_AUDIO.format(smart_choice='Smart compression' if smart else 'Compression', level_info_label=level_info['label'], fps_note=fps_note, audio_info_label=audio_info['label'], boost_suffix=boost_suffix, enhancement_suffix=enhancement_suffix)
            )

        await send_video_with_cover(
            context.bot,
            cover_path,
            chat_id=chat_id,
            video=str(output_path),
            supports_streaming=True,
            width=output_width,
            height=output_height,
            duration=round(output_duration) if output_duration else None,
            thumbnail=str(thumbnail_path) if has_thumbnail else None,
            caption=(
                (
                    f"{result_heading}\n\nSize: {format_size(compressed_size)}"
                    if is_cover_only
                    else (
                        f"{result_heading}\n\n"
                        f"Original: {format_size(original_size)}\n"
                        f"Compressed: {format_size(compressed_size)}\n"
                        f"Saved: {percentage:.1f}%"
                    )
                )
                + (f"\n\n{extra_caption}" if extra_caption else "")
            ),
        )
        log_success()

    except JobCancelled:
        logger.info("Cancelled by the user (message %s)", source_message_id)
        record_cancel()
        refunded_text = give_bytes_back()   # they cancelled: nothing was sent, so the bytes go back
        admin_outcome = ("cancelled", refunded_text)
        if status_message is not None:
            with contextlib.suppress(Exception):
                await status_message.edit_text(   # also removes the button
                    MSG_COMPRESSION_CANCELLED_REFUNDED.format(size=refunded_text)
                    if refunded_text
                    else MSG_COMPRESSION_CANCELLED
                )

    except Exception as error:
        logger.exception("Error compressing %s:", "audio" if (is_audio_only or is_audio_copy) else "video")
        record_failure(stats_user)
        admin_outcome = ("failed", give_bytes_back())   # no result: give the bytes back
        await context.bot.send_message(
            chat_id=chat_id,
            text=MSG_SOMETHING_WENT_WRONG_WHILE_COMPRESSI.format(error=error),
        )

    finally:
        await admin_watch.finish(admin_outcome)

        if queue_refresh_task is not None and not queue_refresh_task.done():
            queue_refresh_task.cancel()
        compression_queue[:] = [entry for entry in compression_queue if entry["job_id"] is not job_token]
        active_job_progress.pop(job_token, None)

        try:
            if source_message_id in pending_compressions:
                clear_pending(source_message_id)
            else:
                # No pending entry (e.g. it was already cleared): just clean the folder up.
                cleanup_work_dir(work_dir)
        finally:
            # Only now (result sent, temp files gone) may the next job start.
            if work_slot_held:
                release_work_slot()

        active_job_ids.discard(source_message_id)
        await maybe_notify_update_ready(context.bot)


# Newer python-telegram-bot versions (Bot API 8.3+) have a real `cover`
# argument on send_video; older ones don't, so fall back to passing it
# straight through api_kwargs as a file:// URI (the Local Bot API
# server reads it from disk, same as every other local file here).
SEND_VIDEO_HAS_COVER = "cover" in inspect.signature(Bot.send_video).parameters


class _CoverRejected(Exception):
    """Telegram refused the cover — the video itself is fine to send without it."""


async def send_video_with_cover(bot, cover_path, **kwargs):
    if cover_path and Path(cover_path).exists():
        if SEND_VIDEO_HAS_COVER:
            extra = {"cover": str(cover_path)}
        else:
            extra = {"api_kwargs": {"cover": Path(cover_path).resolve().as_uri()}}

        async def attempt():
            try:
                return await bot.send_video(**kwargs, **extra)
            except BadRequest as error:
                # Not a network problem, so don't let with_retries repeat it.
                raise _CoverRejected(str(error)) from error

        try:
            return await with_retries(attempt)
        except _CoverRejected as error:
            logger.warning("Telegram rejected the video cover (%s) — sending without it.", error)

    return await with_retries(bot.send_video, **kwargs)


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
            MSG_THAT_VIDEO_ISN_T_IN
        )
        return

    work_dir = pending["work_dir"]
    audio_path = work_dir / f"replacement_{message.message_id}"

    status_message = await message.reply_text(MSG_DOWNLOADING_THE_NEW_AUDIO)

    try:
        await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.UPLOAD_VOICE)

        telegram_file = await with_retries(context.bot.get_file, file_id)
        await with_retries(telegram_file.download_to_drive, custom_path=str(audio_path))
        delete_server_copy(telegram_file)

        has_audio, _ = await probe_audio_stream(str(audio_path))

        if not has_audio:
            audio_path.unlink(missing_ok=True)
            # Still waiting — they can just send a different file.
            await status_message.edit_text(
                MSG_I_COULDN_T_FIND_ANY
            )
            return

        replacement_duration = await get_duration(str(audio_path))
    except Exception as error:
        logger.exception("Error downloading replacement audio:")
        audio_path.unlink(missing_ok=True)
        await status_message.edit_text(
            MSG_COULDN_T_DOWNLOAD_THAT_AUDIO.format(error=error)
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
                MSG_YOUR_RECORDING_IS_LONGER_THAN.format(time_replacement_duration=format_duration(replacement_duration), time_video_duration=format_duration(video_duration), time_difference=format_duration(difference))
            )
        elif difference < -1:
            extra_caption = (
                MSG_YOUR_RECORDING_IS_SHORTER_THAN.format(time_replacement_duration=format_duration(replacement_duration), time_video_duration=format_duration(video_duration), time_difference=format_duration(-difference))
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
# Merge videos
# ============================================================
#
# Flow: the person taps "🎞 Merge videos" (or sends /merge). From then on every
# video that chat sends is ADDED to a list instead of being compressed. Nothing
# is downloaded yet. When they tap "Merge", the job waits for the shared work
# slot like every other heavy job, downloads the videos, and joins them in the
# order they were sent:
#   • if every video has identical codec/size/frame-rate/audio settings the
#     streams are simply joined (very fast, no quality loss);
#   • otherwise everything is re-encoded to the first video's size and frame
#     rate (smaller videos get black bars, videos without sound get silence).

MERGE_MAX_VIDEOS = int(os.getenv("MERGE_MAX_VIDEOS", "10"))
MERGE_CRF = os.getenv("MERGE_CRF", "23")
MERGE_PRESET = os.getenv("MERGE_PRESET", "veryfast")
MERGE_MAX_WIDTH = int(os.getenv("MERGE_MAX_WIDTH", "1920"))
MERGE_AUDIO_BITRATE = os.getenv("MERGE_AUDIO_BITRATE", "128k")
# A merge session left alone this long is dropped (and the daily-limit bytes refunded).
MERGE_IDLE_MINUTES = int(os.getenv("MERGE_IDLE_MINUTES", "15"))
MERGE_COPY_VIDEO_CODECS = {"h264", "hevc"}
TELEGRAM_UPLOAD_LIMIT_BYTES = int(1.95 * 1024 * 1024 * 1024)

BTN_MERGE_VIDEOS = "🎞 Merge videos"
BTN_MERGE_NOW = "✅ Merge {count} videos"
BTN_MERGE_UNDO = "↩️ Remove last"

MSG_MERGE_INTRO = (
    "🎞 Merge videos\n\n"
    "Send me the videos you want to join, in the order you want them "
    "(2 to {max} videos). You can send them one by one or forward several at once.\n\n"
    "While merge mode is on, videos you send are added here instead of being compressed. "
    "Tap Cancel to leave merge mode."
)
MSG_MERGE_LIST_HEADER = "🎞 Merge videos — {count} added (max {max})"
MSG_MERGE_LIST_FOOTER = "Send the next video, or tap Merge when you're done. They're joined in the order you sent them."
MSG_MERGE_NEED_TWO = "Add at least 2 videos first, then tap Merge."
MSG_MERGE_FULL = "That's the maximum of {max} videos for one merge. Tap Merge to join them."
MSG_MERGE_VIDEOS_ONLY = "🎞 Merge mode is on — please send videos only, or tap Cancel to leave merge mode."
MSG_MERGE_NO_SESSION = "There's no merge in progress. Tap 🎞 Merge videos (or send /merge) to start one."
MSG_MERGE_ALREADY_RUNNING = "⏳ Your merge is already running — please wait for it to finish."
MSG_MERGE_CANCELLED = "❌ Merge cancelled. Videos you send now will be compressed as usual."
MSG_MERGE_CANCELLED_REFUNDED = "❌ Merge cancelled — {size} was given back to your daily limit."
MSG_MERGE_CANCEL_WHILE_RUNNING = "Your merge is already running — use the Cancel button under its progress bar."
MSG_MERGE_DOWNLOADING = "⬇️ Downloading video {index} of {total} ({size})…"
MSG_MERGE_CHECKING = "🔍 Checking your videos…"
MSG_MERGE_JOINING = "🔗 Merging {count} videos — joining them without re-encoding (original quality)"
MSG_MERGE_REENCODING = "🔗 Merging {count} videos — re-encoding so they match"
MSG_MERGE_SENDING = "📤 Sending the merged video…"
MSG_MERGE_NOT_A_VIDEO = "Video {index} couldn't be read — it may be damaged or not a real video. Nothing was merged."
MSG_MERGE_TOO_BIG = "The merged video would be bigger than Telegram's 2 GB upload limit. Try merging fewer or shorter videos."
MSG_MERGE_NO_LENGTH = "I couldn't read the length of video {index}, so I can't merge it."
MSG_MERGE_FAILED = "😕 Something went wrong while merging: {error}"
MSG_MERGE_CANCELLED_JOB = "❌ Merge cancelled."
MSG_MERGE_REFUND_NOTE = "\n{size} was given back to your daily limit."
MSG_MERGE_DONE_CAPTION = (
    "✅ Merged {count} videos ({mode})\n\n"
    "Length: {length}\n"
    "Size: {size}"
)
MSG_MERGE_MODE_COPY = "joined without re-encoding"
MSG_MERGE_MODE_REENCODE = "re-encoded to match"
MSG_MERGE_IDLE_EXPIRED = "🎞 I closed your merge after {minutes} minutes without activity."
MSG_MERGE_IDLE_EXPIRED_REFUNDED = (
    "🎞 I closed your merge after {minutes} minutes without activity, "
    "and gave {size} back to your daily limit."
)

# chat_id -> the merge list that chat is building (or merging right now).
merge_sessions: dict[int, dict] = {}
_merge_job_keys = itertools.count(1)


class MergeRefused(Exception):
    """The merge can't go ahead; the message is shown to the person as is."""


def merge_panel_text(session: dict) -> str:
    items = session["items"]

    if not items:
        return MSG_MERGE_INTRO.format(max=MERGE_MAX_VIDEOS)

    lines = [MSG_MERGE_LIST_HEADER.format(count=len(items), max=MERGE_MAX_VIDEOS), ""]

    for number, item in enumerate(items, start=1):
        name = item["filename"]
        if len(name) > 32:
            name = name[:29] + "..."

        bits = [format_size(item["size"])] if item.get("size") else []
        if item.get("duration"):
            bits.append(format_duration(item["duration"]))

        lines.append(f"{number}. {name}" + (f" · {' · '.join(bits)}" if bits else ""))

    total = sum(int(item.get("size") or 0) for item in items)
    if total:
        lines += ["", f"Total: {format_size(total)}"]

    lines += ["", MSG_MERGE_LIST_FOOTER]
    return "\n".join(lines)


def merge_keyboard(count: int) -> InlineKeyboardMarkup:
    rows = []

    if count >= 2:
        rows.append([InlineKeyboardButton(BTN_MERGE_NOW.format(count=count), callback_data="merge:go:0")])

    last_row = []
    if count >= 1:
        last_row.append(InlineKeyboardButton(BTN_MERGE_UNDO, callback_data="merge:undo:0"))
    last_row.append(InlineKeyboardButton(BTN_CANCEL, callback_data="merge:cancel:0"))
    rows.append(last_row)

    return InlineKeyboardMarkup(rows)


def merge_refund_all(session: dict) -> str:
    """Give the daily-limit bytes of every added video back. Returns the size text, or ""."""
    owner_id = session.get("owner_id")
    total = sum(int(item.get("charged") or 0) for item in session["items"])

    if quota_applies(owner_id) and total > 0:
        quota_refund(owner_id, total)
        for item in session["items"]:
            item["charged"] = 0
        return format_size(total)

    return ""


async def send_merge_panel(bot, session: dict) -> None:
    """Show the current list as a fresh message (and take the buttons off the old one)."""
    async with session["lock"]:
        old_panel_id = session.get("panel_id")

        if old_panel_id is not None:
            with contextlib.suppress(Exception):
                await bot.edit_message_reply_markup(
                    chat_id=session["chat_id"], message_id=old_panel_id, reply_markup=None
                )

        sent = await bot.send_message(
            chat_id=session["chat_id"],
            text=merge_panel_text(session),
            reply_markup=merge_keyboard(len(session["items"])),
        )
        session["panel_id"] = sent.message_id
        session["last_active"] = time.time()


async def begin_merge(bot, chat_id: int, user) -> None:
    """Start merge mode for a chat (home button or /merge)."""
    user_id = user.id if user is not None else None

    if update_mode["on"] and not is_admin(user_id):
        await bot.send_message(chat_id=chat_id, text=UPDATE_BUSY_TEXT)
        return

    existing = merge_sessions.get(chat_id)

    if existing is not None:
        if existing.get("running"):
            await bot.send_message(chat_id=chat_id, text=MSG_MERGE_ALREADY_RUNNING)
        else:
            await send_merge_panel(bot, existing)
        return

    # Merge mode takes over: forget any half-finished "send me the audio/name/cover" prompt.
    awaiting_audio_replacement.pop(chat_id, None)
    awaiting_input.pop(chat_id, None)

    session = {
        "chat_id": chat_id,
        "owner_id": user_id,
        "items": [],
        "panel_id": None,
        "last_active": time.time(),
        "running": False,
        "lock": asyncio.Lock(),
    }
    merge_sessions[chat_id] = session
    await send_merge_panel(bot, session)


async def merge_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    user = update.effective_user

    if message is None or user is None:
        return

    if not is_allowed(user.id):
        await reject_unauthorized(message, user.id)
        return

    await begin_merge(context.bot, message.chat_id, user)


async def merge_add_video(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    file_id: str,
    size: int | None,
    filename: str,
) -> None:
    """A video arrived while the chat is in merge mode: add it to the list."""
    message = update.effective_message
    chat_id = message.chat_id
    session = merge_sessions.get(chat_id)

    if session is None:
        return

    sender_id = update.effective_user.id if update.effective_user else None

    if session.get("running"):
        await message.reply_text(MSG_MERGE_ALREADY_RUNNING)
        return

    if update_mode["on"] and not is_admin(sender_id):
        await message.reply_text(UPDATE_BUSY_TEXT)
        return

    if len(session["items"]) >= MERGE_MAX_VIDEOS:
        await message.reply_text(MSG_MERGE_FULL.format(max=MERGE_MAX_VIDEOS))
        return

    charged = int(size or 0)

    if file_too_large(sender_id, charged):
        await message.reply_text(file_too_large_text(sender_id, charged))
        return

    if not quota_allows(sender_id, charged):
        await message.reply_text(quota_refusal_text(sender_id, charged))
        return

    quota_consume(sender_id, charged)

    video = message.video
    session["items"].append({
        "file_id": file_id,
        "size": size,
        "filename": filename,
        "duration": getattr(video, "duration", None) if video is not None else None,
        "message_id": message.message_id,
        "charged": charged,
        "sender_id": sender_id,
    })
    # Several videos forwarded at once can be handled out of order: Telegram's
    # message ids always grow in the order they were sent, so sort by them.
    session["items"].sort(key=lambda item: item["message_id"])

    await send_merge_panel(context.bot, session)


async def handle_merge_callback(query, context: ContextTypes.DEFAULT_TYPE, command: str) -> None:
    chat_id = query.message.chat_id if query.message else None

    if chat_id is None:
        return

    if command == "start":
        await begin_merge(context.bot, chat_id, query.from_user)
        return

    session = merge_sessions.get(chat_id)

    if session is None:
        await query.message.reply_text(MSG_MERGE_NO_SESSION)
        return

    if command == "cancel":
        if session.get("running"):
            await query.message.reply_text(MSG_MERGE_CANCEL_WHILE_RUNNING)
            return

        refunded = merge_refund_all(session)
        merge_sessions.pop(chat_id, None)
        text = MSG_MERGE_CANCELLED_REFUNDED.format(size=refunded) if refunded else MSG_MERGE_CANCELLED

        try:
            await query.edit_message_text(text)
        except Exception:
            await query.message.reply_text(text)
        return

    if session.get("running"):
        await query.message.reply_text(MSG_MERGE_ALREADY_RUNNING)
        return

    session["last_active"] = time.time()

    if command == "undo":
        if session["items"]:
            removed = session["items"].pop()
            if quota_applies(removed.get("sender_id")) and removed.get("charged"):
                quota_refund(removed["sender_id"], int(removed["charged"]))

        try:
            await query.edit_message_text(
                merge_panel_text(session), reply_markup=merge_keyboard(len(session["items"]))
            )
        except Exception as error:
            if "not modified" not in str(error).lower():
                logger.warning("Couldn't refresh the merge list: %s", error)
        return

    if command == "go":
        if len(session["items"]) < 2:
            await query.message.reply_text(MSG_MERGE_NEED_TWO)
            return

        # Set BEFORE any await so a double tap can't start two merges.
        session["running"] = True

        with contextlib.suppress(Exception):
            await query.edit_message_reply_markup(reply_markup=None)

        await run_merge(context, session, query.from_user)


async def expire_idle_merge_sessions(bot) -> int:
    """Drop merge lists nobody has touched for MERGE_IDLE_MINUTES (nothing was downloaded yet)."""
    if IDLE_FILE_TTL_MINUTES <= 0 or MERGE_IDLE_MINUTES <= 0:
        return 0

    now = time.time()
    removed = 0

    for chat_id, session in list(merge_sessions.items()):
        if session.get("running"):
            continue

        if now - session.get("last_active", now) < MERGE_IDLE_MINUTES * 60:
            continue

        refunded = merge_refund_all(session)
        merge_sessions.pop(chat_id, None)
        removed += 1

        with contextlib.suppress(Exception):
            await bot.send_message(
                chat_id=chat_id,
                text=(
                    MSG_MERGE_IDLE_EXPIRED_REFUNDED.format(minutes=MERGE_IDLE_MINUTES, size=refunded)
                    if refunded
                    else MSG_MERGE_IDLE_EXPIRED.format(minutes=MERGE_IDLE_MINUTES)
                ),
            )

    return removed


async def probe_merge_signature(path: str) -> dict | None:
    """What has to match between videos for them to be joined without re-encoding."""
    command = [
        "ffprobe", "-v", "error", "-print_format", "json",
        "-show_streams", path,
    ]
    returncode, stdout, stderr = await run_command(command)

    if returncode != 0:
        logger.warning("ffprobe (merge) failed: %s", stderr.decode(errors="replace"))
        return None

    try:
        data = json.loads(stdout.decode(errors="replace"))
    except ValueError:
        return None

    streams = data.get("streams") or []
    video = next(
        (s for s in streams
         if s.get("codec_type") == "video" and not (s.get("disposition") or {}).get("attached_pic")),
        None,
    )
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)

    if video is None:
        return None

    width, height = _to_int(video.get("width")), _to_int(video.get("height"))

    rotation = None
    for side in video.get("side_data_list") or []:
        if "rotation" in side:
            rotation = _to_int(side.get("rotation"))
    if rotation is None:
        rotation = _to_int((video.get("tags") or {}).get("rotate"))
    rotation = rotation or 0

    display_width, display_height = width, height
    if abs(rotation) % 180 == 90 and width and height:
        display_width, display_height = height, width

    return {
        "display_width": display_width,
        "display_height": display_height,
        "fps": parse_frame_rate(video.get("avg_frame_rate")) or parse_frame_rate(video.get("r_frame_rate")),
        "has_audio": audio is not None,
        # Everything below must be identical for a lossless join:
        "match": (
            (video.get("codec_name") or "").lower(),
            video.get("profile"),
            video.get("pix_fmt"),
            width, height, rotation,
            video.get("r_frame_rate"),
            video.get("sample_aspect_ratio"),
            (audio.get("codec_name") or "").lower() if audio else None,
            audio.get("profile") if audio else None,
            audio.get("sample_rate") if audio else None,
            audio.get("channels") if audio else None,
        ),
        "video_codec": (video.get("codec_name") or "").lower(),
        "audio_codec": (audio.get("codec_name") or "").lower() if audio else "",
    }


def merge_can_stream_copy(signatures: list[dict]) -> bool:
    """True when every video is built the same way, so they can simply be joined."""
    first = signatures[0]

    if first["video_codec"] not in MERGE_COPY_VIDEO_CODECS:
        return False

    if first["has_audio"] and first["audio_codec"] not in MP4_COPY_SAFE_AUDIO:
        return False

    return all(sig["match"] == first["match"] for sig in signatures)


def build_merge_copy_command(paths: list[Path], list_path: Path, output_path: Path) -> list[str]:
    list_path.write_text(
        "".join(f"file '{path.resolve().as_posix()}'\n" for path in paths),
        encoding="utf-8",
    )

    return [
        "ffmpeg", "-y",
        "-progress", "pipe:1", "-nostats", "-loglevel", "error",
        "-f", "concat", "-safe", "0", "-i", str(list_path),
        "-c", "copy",
        "-movflags", "+faststart",
        "-max_muxing_queue_size", "1024",
        str(output_path),
    ]


def build_merge_reencode_command(
    paths: list[Path],
    signatures: list[dict],
    durations: list[float | None],
    output_path: Path,
) -> list[str]:
    first = signatures[0]
    width = first["display_width"] or 1280
    height = first["display_height"] or 720

    if width > MERGE_MAX_WIDTH:
        height = round(height * MERGE_MAX_WIDTH / width)
        width = MERGE_MAX_WIDTH

    width = max(2, width - width % 2)    # x264 needs even sizes
    height = max(2, height - height % 2)

    fps = min(max(first["fps"] or 30.0, 1.0), 60.0)
    any_audio = any(sig["has_audio"] for sig in signatures)

    command = ["ffmpeg", "-y", "-progress", "pipe:1", "-nostats", "-loglevel", "error"]

    for path in paths:
        command += ["-threads", DECODER_THREADS, "-i", str(path)]

    parts: list[str] = []
    labels: list[str] = []

    for index, sig in enumerate(signatures):
        parts.append(
            f"[{index}:v:0]scale={width}:{height}:force_original_aspect_ratio=decrease,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:black,setsar=1,"
            f"fps={fps:.3f},format=yuv420p,setpts=PTS-STARTPTS[v{index}]"
        )
        labels.append(f"[v{index}]")

        if any_audio:
            if sig["has_audio"]:
                parts.append(
                    f"[{index}:a:0]aformat=sample_rates=44100:channel_layouts=stereo,"
                    f"asetpts=PTS-STARTPTS[a{index}]"
                )
            else:
                # This video has no sound: fill its part with silence of the same length.
                if not durations[index]:
                    raise MergeRefused(MSG_MERGE_NO_LENGTH.format(index=index + 1))
                parts.append(
                    f"anullsrc=r=44100:cl=stereo,atrim=duration={durations[index]:.3f},"
                    f"asetpts=PTS-STARTPTS[a{index}]"
                )
            labels.append(f"[a{index}]")

    parts.append(
        "".join(labels)
        + f"concat=n={len(paths)}:v=1:a={1 if any_audio else 0}[v]"
        + ("[a]" if any_audio else "")
    )

    command += ["-filter_complex", ";".join(parts), "-map", "[v]"]

    if any_audio:
        command += ["-map", "[a]"]

    medium = LEVELS["medium"]   # reuse its low-RAM x264 buffering settings
    command += [
        "-c:v", "libx264",
        "-preset", MERGE_PRESET,
        "-crf", MERGE_CRF,
        "-x264-params",
        f"rc-lookahead={medium['lookahead']}:ref={medium['refs']}:bframes={medium['bframes']}",
        "-threads", FFMPEG_THREADS,
    ]

    if any_audio:
        command += ["-c:a", "aac", "-b:a", MERGE_AUDIO_BITRATE]

    command += [
        "-movflags", "+faststart",
        "-max_muxing_queue_size", "1024",
        str(output_path),
    ]

    return command


async def run_merge(context: ContextTypes.DEFAULT_TYPE, session: dict, user) -> None:
    """Download the added videos, join them, send the result. Holds the shared work slot."""
    global active_compressions

    chat_id = session["chat_id"]
    owner_id = session.get("owner_id")
    items = list(session["items"])
    count = len(items)
    key = -next(_merge_job_keys)   # negative: can never clash with a real message id

    Path(TEMP_DIR).mkdir(parents=True, exist_ok=True)
    work_dir = Path(tempfile.mkdtemp(prefix=f"merge_{abs(key)}_", dir=TEMP_DIR))
    output_path = work_dir / "merged.mp4"

    status_message = None
    work_slot_held = False
    admin_watch = AdminJobWatcher(context.bot, user, "merge")
    admin_outcome: tuple[str, str] | None = None
    job_token = object()
    job_started = time.monotonic()
    queue_wait_seconds: float | None = None
    encode_seconds: float | None = None
    total_duration: float | None = None
    total_input_bytes = 0
    merged_size: int | None = None

    def give_bytes_back() -> str:
        return merge_refund_all({"owner_id": owner_id, "items": items})

    async def post_status(text: str, markup=None) -> None:
        nonlocal status_message
        if status_message is None:
            status_message = await context.bot.send_message(chat_id=chat_id, text=text, reply_markup=markup)
        else:
            await status_message.edit_text(text, reply_markup=markup)

    active_job_ids.add(key)

    try:
        queued_at = time.monotonic()
        await acquire_work_slot(post_status, user_priority(owner_id), user_plan_label(owner_id))
        work_slot_held = True
        queue_wait_seconds = time.monotonic() - queued_at

        # ---- download ----
        paths: list[Path] = []

        for index, item in enumerate(items, start=1):
            await post_status(MSG_MERGE_DOWNLOADING.format(
                index=index, total=count, size=format_size(item.get("size"))
            ))
            await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.UPLOAD_VIDEO)

            path = work_dir / f"part_{index}"
            telegram_file = await with_retries(context.bot.get_file, item["file_id"])
            await with_retries(telegram_file.download_to_drive, custom_path=str(path))
            delete_server_copy(telegram_file)

            real_size = path.stat().st_size
            total_input_bytes += real_size

            if not item.get("charged") and real_size > 0:
                # Telegram didn't tell us the size up front: charge the real size now.
                sender_id = item.get("sender_id")

                if file_too_large(sender_id, real_size):
                    raise MergeRefused(file_too_large_text(sender_id, real_size))
                if not quota_allows(sender_id, real_size):
                    raise MergeRefused(quota_refusal_text(sender_id, real_size))

                quota_consume(sender_id, real_size)
                item["charged"] = real_size

            paths.append(path)

        # ---- look at every video ----
        await post_status(MSG_MERGE_CHECKING)

        signatures: list[dict] = []
        durations: list[float | None] = []

        for index, path in enumerate(paths, start=1):
            signature = await probe_merge_signature(str(path))

            if signature is None:
                raise MergeRefused(MSG_MERGE_NOT_A_VIDEO.format(index=index))

            signatures.append(signature)
            durations.append(await get_duration(str(path)))

        known = [d for d in durations if d]
        total_duration = sum(known) if known else None

        # ---- encode ----
        job_handle = {"user_id": owner_id, "cancelled": False, "process": None, "done": False}
        running_jobs[key] = job_handle
        job_context_token = CURRENT_JOB.set(job_handle)
        active_compressions += 1
        active_job_progress[job_token] = {
            "start_time": time.monotonic(),
            "duration": total_duration,
            "percent": 0.0,
        }

        try:
            progress_state = {"last_percent": -10.0, "last_edit": 0.0, "start_time": time.monotonic(), "label": ""}

            async def show_bar(label: str) -> None:
                progress_state.update(
                    last_percent=-10.0, last_edit=0.0, start_time=time.monotonic(), label=label
                )
                active_job_progress[job_token].update(start_time=time.monotonic(), percent=0.0)
                await post_status(f"{label}\n{render_progress_bar(0)}", cancel_job_keyboard(key))

            async def on_progress(percent: float) -> None:
                active_job_progress[job_token]["percent"] = percent
                await admin_watch.progress(percent)

                now = time.monotonic()

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
                if 1 <= percent < 100 and elapsed > 2:
                    eta_text = f" · ~{format_duration(elapsed * (100 - percent) / percent)} left"

                try:
                    await status_message.edit_text(
                        f"{progress_state['label']}\n{render_progress_bar(percent)}{eta_text}",
                        reply_markup=cancel_job_keyboard(key),
                    )
                except Exception:
                    pass   # rate-limit hiccup / "message not modified": never abort the merge for it

            progress_callback = on_progress if total_duration else None
            used_copy = False
            encode_started = time.monotonic()

            await admin_watch.start()

            if merge_can_stream_copy(signatures):
                await show_bar(MSG_MERGE_JOINING.format(count=count))

                try:
                    await run_ffmpeg_with_progress(
                        build_merge_copy_command(paths, work_dir / "list.txt", output_path),
                        total_duration,
                        progress_callback,
                    )
                    used_copy = output_path.exists() and output_path.stat().st_size > 0
                except JobCancelled:
                    raise
                except Exception:
                    logger.warning("Joining without re-encoding failed — re-encoding instead.", exc_info=True)

                if not used_copy:
                    output_path.unlink(missing_ok=True)

            if not used_copy:
                await show_bar(MSG_MERGE_REENCODING.format(count=count))
                await run_ffmpeg_with_progress(
                    build_merge_reencode_command(paths, signatures, durations, output_path),
                    total_duration,
                    progress_callback,
                )

            encode_seconds = time.monotonic() - encode_started
        finally:
            job_handle["done"] = True
            running_jobs.pop(key, None)
            CURRENT_JOB.reset(job_context_token)
            active_compressions -= 1
            active_job_progress.pop(job_token, None)

        # ---- send ----
        merged_size = output_path.stat().st_size

        if merged_size > TELEGRAM_UPLOAD_LIMIT_BYTES:
            raise MergeRefused(MSG_MERGE_TOO_BIG)

        output_duration = await get_duration(str(output_path))
        output_width, output_height = await get_video_dimensions(str(output_path))
        thumbnail_path = work_dir / "thumb.jpg"
        has_thumbnail = await generate_thumbnail(str(output_path), str(thumbnail_path), output_duration)

        await status_message.edit_text(MSG_MERGE_SENDING)
        await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.UPLOAD_VIDEO)

        await send_video_with_cover(
            context.bot,
            None,
            chat_id=chat_id,
            video=str(output_path),
            supports_streaming=True,
            width=output_width,
            height=output_height,
            duration=round(output_duration) if output_duration else None,
            thumbnail=str(thumbnail_path) if has_thumbnail else None,
            caption=MSG_MERGE_DONE_CAPTION.format(
                count=count,
                mode=MSG_MERGE_MODE_COPY if used_copy else MSG_MERGE_MODE_REENCODE,
                length=format_duration(output_duration) if output_duration else "unknown",
                size=format_size(merged_size),
            ),
        )

        admin_outcome = ("done", "")
        with contextlib.suppress(Exception):
            await status_message.delete()

        try:
            record_job(
                user=user,
                kind="merge",
                level_label="Merge",
                original_size=total_input_bytes,
                compressed_size=merged_size,
                media_seconds=total_duration,
                encode_seconds=encode_seconds,
                wait_seconds=queue_wait_seconds,
                total_seconds=time.monotonic() - job_started,
                audio_label="Merged audio",
                boost=0,
                noise_reduction=False,
                voice_enhancement=False,
            )
        except Exception:
            logger.exception("Couldn't record the merge in the statistics")

    except JobCancelled:
        logger.info("Merge cancelled by the user")
        record_cancel()
        refunded = give_bytes_back()
        admin_outcome = ("cancelled", refunded)

        if status_message is not None:
            with contextlib.suppress(Exception):
                await status_message.edit_text(
                    MSG_MERGE_CANCELLED_JOB + (MSG_MERGE_REFUND_NOTE.format(size=refunded) if refunded else "")
                )

    except MergeRefused as problem:
        record_failure(user)
        refunded = give_bytes_back()
        admin_outcome = ("failed", refunded)
        await context.bot.send_message(
            chat_id=chat_id,
            text=str(problem) + (MSG_MERGE_REFUND_NOTE.format(size=refunded) if refunded else ""),
        )

    except Exception as error:
        logger.exception("Error merging videos:")
        record_failure(user)
        refunded = give_bytes_back()
        admin_outcome = ("failed", refunded)
        await context.bot.send_message(
            chat_id=chat_id,
            text=MSG_MERGE_FAILED.format(error=error) + (MSG_MERGE_REFUND_NOTE.format(size=refunded) if refunded else ""),
        )

    finally:
        await admin_watch.finish(admin_outcome)
        active_job_progress.pop(job_token, None)

        if merge_sessions.get(chat_id) is session:
            merge_sessions.pop(chat_id, None)

        try:
            cleanup_work_dir(work_dir)
        finally:
            # Only now (result sent, temp files gone) may the next job start.
            if work_slot_held:
                release_work_slot()

        active_job_ids.discard(key)
        await maybe_notify_update_ready(context.bot)



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

    await handle_incoming(
        update, context,
        file_id=audio.file_id,
        original_size=audio.file_size,
        filename=filename,
        media_type="audio",
    )


async def show_audio_meta_panel(context, chat_id: int, message_id: int) -> None:
    """Show (or refresh) the rename / album / cover panel for an audio file."""
    pending = pending_compressions.get(message_id)

    if pending is None:
        return

    text = audio_meta_text(pending)
    markup = audio_meta_keyboard(message_id)
    panel_id = pending.get("meta_panel_id")

    if panel_id:
        try:
            await context.bot.edit_message_text(
                chat_id=chat_id, message_id=panel_id, text=text, reply_markup=markup
            )
            return
        except BadRequest as error:
            if "not modified" in str(error).lower():
                return
        except Exception:
            pass

    sent = await context.bot.send_message(chat_id=chat_id, text=text, reply_markup=markup)
    pending["meta_panel_id"] = sent.message_id


async def apply_cover_image(update: Update, context: ContextTypes.DEFAULT_TYPE, file_id: str) -> None:
    """An image arrived while we were waiting for a cover — process and attach it."""
    message = update.effective_message
    chat_id = message.chat_id
    waiting = awaiting_input.get(chat_id)

    if waiting is None or waiting["field"] not in ("cover", "videocover"):
        return

    target_message_id = waiting["mid"]
    pending = pending_compressions.get(target_message_id)

    if pending is None:
        awaiting_input.pop(chat_id, None)
        await message.reply_text(MSG_THAT_FILE_ISN_T_IN)
        return

    work_dir = pending["work_dir"]
    source_path = work_dir / f"cover_src_{message.message_id}"
    cover_path = work_dir / "cover.jpg"
    thumb_path = work_dir / "cover_thumb.jpg"

    status_message = await message.reply_text(MSG_PROCESSING_THE_IMAGE)

    try:
        telegram_file = await with_retries(context.bot.get_file, file_id)
        await with_retries(telegram_file.download_to_drive, custom_path=str(source_path))
        delete_server_copy(telegram_file)

        full_ok = await prepare_cover_image(str(source_path), str(cover_path), 1000, "3")
        thumb_ok = full_ok and await prepare_cover_image(str(source_path), str(thumb_path), 320, "5")
    except Exception as error:
        logger.exception("Error preparing cover image:")
        await status_message.edit_text(MSG_COULDN_T_DOWNLOAD_THAT_IMAGE.format(error=error))
        return
    finally:
        source_path.unlink(missing_ok=True)

    if not full_ok or not thumb_ok:
        await status_message.edit_text(
            MSG_I_COULDN_T_READ_THAT
        )
        return

    pending["cover_path"] = cover_path
    pending["cover_thumb_path"] = thumb_path
    awaiting_input.pop(chat_id, None)

    if waiting["field"] == "cover":
        await status_message.edit_text(MSG_ALBUM_COVER_SET)
        await show_audio_meta_panel(context, chat_id, target_message_id)
    else:
        await status_message.edit_text(
            MSG_VIDEO_COVER_SET_NOW_PICK,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton(
                    BTN_UPLOAD_VIDEO_UNCHANGED,
                    callback_data=f"coveronly:_:{target_message_id}",
                )
            ]]),
        )


SLIDE_NUMBERS_RE = re.compile(r"^[\d\s,;\-\u2013]+$")


def parse_slide_numbers(text: str) -> list[int] | None:
    """
    "2 5 11 16" / "2,5,11" / "3-6 9" -> sorted unique page numbers.
    Returns None if the text isn't purely numbers/ranges (so ordinary
    chat text is ignored).
    """
    text = text.strip()
    if not text or not SLIDE_NUMBERS_RE.match(text):
        return None

    numbers: set[int] = set()
    for token in re.split(r"[\s,;]+", text):
        if not token:
            continue
        match = re.fullmatch(r"(\d+)[\-\u2013](\d+)", token)
        if match:
            start, end = int(match.group(1)), int(match.group(2))
            if start > end or end - start > 5000:
                return None
            numbers.update(range(start, end + 1))
        elif token.isdigit():
            numbers.add(int(token))
        else:
            return None

    return sorted(numbers) if numbers else None


async def delete_pdf_slides(
    update: Update, context: ContextTypes.DEFAULT_TYPE, message_id: int, numbers: list[int]
) -> None:
    """Remove the given 1-based pages from the open PDF session and send the rebuilt PDF."""
    message = update.effective_message
    pending = pending_compressions.get(message_id)

    if pending is None or not pending.get("frames_dir"):
        await message.reply_text(MSG_NO_EXTRACTED_FRAMES_TO_COMPARE)
        return

    frame_paths = sorted(Path(pending["frames_dir"]).glob("frame_*.jpg"))
    total = len(frame_paths)

    bad = [n for n in numbers if n < 1 or n > total]
    if bad:
        await message.reply_text(
            MSG_DELETE_SLIDES_OUT_OF_RANGE.format(total=total, bad=" ".join(map(str, bad)))
        )
        return

    if len(numbers) >= total:
        await message.reply_text(MSG_DELETE_SLIDES_WOULD_EMPTY)
        return

    status = await message.reply_text(MSG_DELETE_SLIDES_WORKING.format(count=len(numbers)))

    try:
        doomed = {frame_paths[n - 1] for n in numbers}
        kept_paths = [path for path in frame_paths if path not in doomed]

        for path in doomed:
            path.unlink(missing_ok=True)

        pdf_path = Path(pending["work_dir"]) / "frames.pdf"
        pdf_bytes = await asyncio.to_thread(img2pdf.convert, [str(path) for path in kept_paths])
        pdf_path.write_bytes(pdf_bytes)

        await with_retries(
            context.bot.send_document,
            chat_id=pending["chat_id"],
            document=str(pdf_path),
            filename="frames_edited.pdf",
            caption=MSG_DELETE_SLIDES_DONE.format(
                deleted=" ".join(map(str, numbers)), remaining=len(kept_paths)
            ),
            reply_markup=post_pdf_keyboard(message_id, dedup_available=len(kept_paths) > 1),
        )
        with contextlib.suppress(Exception):
            await status.delete()

    except Exception as error:
        logger.exception("Error deleting slides:")
        await status.edit_text(MSG_DELETE_SLIDES_FAILED.format(error=error))


async def handle_text_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Plain text: only meaningful while we're waiting for a name / album."""
    message = update.effective_message

    if message is None or not message.text:
        return

    user_id = update.effective_user.id if update.effective_user else None

    if not is_allowed(user_id):
        return

    chat_id = message.chat_id
    waiting = awaiting_input.get(chat_id)

    if waiting is None:
        # No name/album prompt pending: if a PDF session is open, numbers
        # like "2 5 11 16" mean "delete these pages from the PDF".
        session_mid = open_pdf_sessions.get(chat_id)
        if session_mid is not None:
            numbers = parse_slide_numbers(message.text)
            if numbers:
                await delete_pdf_slides(update, context, session_mid, numbers)
        return

    pending = pending_compressions.get(waiting["mid"])

    if pending is None:
        awaiting_input.pop(chat_id, None)
        await message.reply_text(MSG_THAT_FILE_ISN_T_IN)
        return

    field = waiting["field"]
    text = message.text.strip()

    if field == "name":
        cleaned = sanitize_filename(text)

        if not cleaned:
            await message.reply_text(MSG_THAT_NAME_HAS_NO_USABLE)
            return

        pending["out_name"] = cleaned
    elif field == "album":
        if text == "-":
            pending.pop("album", None)
        else:
            pending["album"] = text[:200]
    elif field == "artist":
        if text == "-":
            pending.pop("artist", None)
        else:
            pending["artist"] = text[:200]
    else:
        await message.reply_text(MSG_I_M_WAITING_FOR_AN)
        return

    awaiting_input.pop(chat_id, None)
    await show_audio_meta_panel(context, chat_id, waiting["mid"])


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """A photo: used as a cover only while we're waiting for one."""
    message = update.effective_message

    if message is None or not message.photo:
        return

    user_id = update.effective_user.id if update.effective_user else None

    if not is_allowed(user_id):
        return

    if await maybe_handle_payment_proof(update, context, message.photo[-1].file_id, "photo"):
        return

    waiting = awaiting_input.get(message.chat_id)

    if waiting is None:
        return

    if waiting["field"] not in ("cover", "videocover"):
        await message.reply_text(MSG_I_M_WAITING_FOR_TEXT)
        return

    await apply_cover_image(update, context, message.photo[-1].file_id)


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
    if is_admin(user_id) and filename.lower().endswith((".json", ".zip")):
        await restore_approved_users_from_document(message, context, document)
        return

    # An image file (sent as a document so Telegram doesn't compress it)
    # while we're waiting for a cover.
    is_image = mime_type.startswith("image/") or filename.lower().endswith(
        (".jpg", ".jpeg", ".png", ".webp")
    )
    if is_image and await maybe_handle_payment_proof(update, context, document.file_id, "document"):
        return

    waiting_input = awaiting_input.get(message.chat_id)
    if is_image and waiting_input is not None:
        if waiting_input["field"] in ("cover", "videocover"):
            await apply_cover_image(update, context, document.file_id)
        else:
            await message.reply_text(MSG_I_M_WAITING_FOR_TEXT)
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
        await message.reply_text(MSG_THAT_DOESN_T_LOOK_LIKE)
        return


    await handle_incoming(
        update, context,
        file_id=document.file_id,
        original_size=document.file_size,
        filename=filename,
        media_type="audio" if is_audio else "video",
    )


async def restore_approved_users_from_document(message, context, document) -> None:
    """Admin sent a Database.zip (or an old .json): merge its users and save."""
    Path(TEMP_DIR).mkdir(parents=True, exist_ok=True)
    temp_path = Path(TEMP_DIR) / f"restore_{message.message_id}.bin"

    try:
        telegram_file = await with_retries(context.bot.get_file, document.file_id)
        await with_retries(telegram_file.download_to_drive, custom_path=str(temp_path))

        added = read_database_bytes(temp_path.read_bytes())
        saved = save_approved_users()

        await message.reply_text(
            MSG_LOADED_THAT_FILE_NEW_USER.format(added=added, count_approved_users=len(approved_users))
            + ("" if saved else MSG_COULDN_T_SAVE_TO_DISK)
        )
    except ValueError as error:
        await message.reply_text(MSG_I_COULDN_T_USE_THAT.format(error=error))
    except Exception as error:
        logger.exception("Error restoring approved users:")
        await message.reply_text(MSG_SOMETHING_WENT_WRONG_READING_THAT.format(error=error))
    finally:
        temp_path.unlink(missing_ok=True)


# ============================================================
# Button handler (setlevel / compress / cancel)
# ============================================================

# ------------------------------------------------------------
# /update — stop new files so you can deploy, and see who is left
# ------------------------------------------------------------
def nothing_in_progress() -> bool:
    return work_active == 0 and not work_waiting and other_jobs_running == 0


def open_menu_count() -> int:
    """Analyzed videos whose owner hasn't tapped anything yet (lost on restart)."""
    menus = sum(1 for message_id in pending_compressions if message_id not in active_job_ids)
    return menus + sum(1 for session in merge_sessions.values() if not session.get("running"))


def update_status_text() -> str:
    waiting = len(work_waiting)
    people_left = work_active + waiting + other_jobs_running

    lines = [MSG_UPDATE_MODE_IS_ON_NEW, ""]

    if people_left == 0:
        lines.append(MSG_NOBODY_IS_IN_THE_MIDDLE)
    else:
        lines += [
            MSG_STILL_TO_FINISH.format(people_left=people_left),
            MSG_BEING_DOWNLOADED_ANALYZED_COMPRESSED.format(work_active=work_active),
            MSG_WAITING_IN_LINE.format(waiting=waiting),
        ]
        if other_jobs_running:
            lines.append(MSG_PDF_JOB_S_RUNNING.format(other_jobs_running=other_jobs_running))
        lines += ["", MSG_I_LL_MESSAGE_YOU_THE]

    menus = open_menu_count()
    if menus:
        lines += [
            "",
            MSG_PERSON_PEOPLE_HAVE_AN_ANALYZED.format(menus=menus),
        ]

    lines += ["", MSG_UPDATE_OFF_CANCEL_AND_ACCEPT]
    return "\n".join(lines)


async def maybe_notify_update_ready(bot, force: bool = False) -> None:
    """Called whenever a job ends: once everyone is done, save the backup and tell the admin."""
    if not update_mode["on"] or (update_mode["notified"] and not force):
        return

    if not nothing_in_progress():
        return

    update_mode["notified"] = True

    try:
        await backup_database_if_changed(bot)  # latest users + stats into the pinned Database.zip
    except Exception:
        logger.exception("Backup before update failed")

    # Someone may have tapped an old menu while the backup was uploading.
    if not nothing_in_progress():
        update_mode["notified"] = False
        return

    backed_up = bool(BACKUP_CHANNEL_ID) and database_fingerprint() == _last_backed_up_fingerprint
    lines = [
        MSG_EVERYONE_HAS_FINISHED_IT_S,
        MSG_THE_LATEST_DATABASE_ZIP_IS
        if backed_up else MSG_I_COULDN_T_CONFIRM_THE,
    ]
    menus = open_menu_count()
    if menus:
        lines.append(MSG_OPEN_MENU_S_WILL_BE.format(menus=menus))
    lines.append(MSG_UPDATE_OFF_CANCELS_UPDATE_MODE)

    try:
        await bot.send_message(chat_id=ADMIN_USER_ID, text="\n".join(lines))
    except Exception:
        logger.warning("Couldn't send the update-ready message")


async def update_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/update (admin): stop new files + show who's left. /update off cancels."""
    message = update.effective_message
    user_id = update.effective_user.id if update.effective_user else None

    if message is None or not is_admin(user_id):
        return

    option = context.args[0].lower() if context.args else ""

    if option in ("off", "cancel", "stop"):
        if not update_mode["on"]:
            await message.reply_text(MSG_UPDATE_MODE_WASN_T_ON)
            return
        update_mode["on"] = False
        update_mode["notified"] = False
        await message.reply_text(MSG_UPDATE_MODE_IS_OFF_NEW)
        return

    if not update_mode["on"]:
        update_mode["on"] = True
        update_mode["notified"] = False

    if nothing_in_progress():
        await message.reply_text(MSG_UPDATE_MODE_IS_ON_NEW)
        await maybe_notify_update_ready(context.bot, force=True)
    else:
        await message.reply_text(update_status_text())


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Wraps the real button handler so PDF jobs count as "people still working"."""
    global other_jobs_running

    query = update.callback_query
    action = (query.data or "").split(":", 1)[0] if query else ""
    tracked = action in ("pdf", "pdfauto", "pdfdedup")

    if tracked:
        other_jobs_running += 1
        update_mode["notified"] = False

    try:
        await handle_callback_inner(update, context)
    finally:
        if tracked:
            other_jobs_running -= 1
            await maybe_notify_update_ready(context.bot)


async def handle_callback_inner(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query

    if query is None or query.data is None:
        return

    user_id = query.from_user.id if query.from_user else None

    if not is_allowed(user_id):
        await query.answer(
            MSG_NOT_AUTHORIZED_YOUR_TELEGRAM_USER.format(user_id=user_id),
            show_alert=True,
        )
        return

    if query.data.startswith("canceljob:"):
        await handle_cancel_job(query, user_id)
        return

    if query.data.startswith("subsoon:"):
        await query.answer(MSG_SUBSCRIPTIONS_COMING_SOON, show_alert=True)
        return

    if query.data.startswith("toobig:"):
        # A level that would make the file bigger: the button does nothing but explain.
        await query.answer(MSG_LEVEL_WOULD_GROW_FILE, show_alert=True)
        return

    await query.answer()

    try:
        action, level, message_id_str = query.data.split(":", 2)
        message_id = int(message_id_str)
    except ValueError:
        return

    chat_id = query.message.chat_id if query.message else None

    # Any tap on a menu counts as "still here": it restarts that video's idle timer.
    if action not in ("payok", "payno", "subpick", "subcancel", "resetstats", "home", "joincheck"):
        touched = pending_compressions.get(message_id)
        if touched is not None:
            touched["last_active"] = time.time()

    if action == "merge":
        await handle_merge_callback(query, context, level)
        return

    if action == "home":
        page = level

        if page == "usage":
            await show_page(query, usage_page_text(user_id), usage_keyboard(user_id))
        elif page == "subscribe":
            await show_page(query, subscribe_page_text(user_id), subscribe_keyboard())
        elif page == "about":
            await show_page(query, about_text(), about_keyboard())
        else:
            await show_page(query, home_text(query.from_user), home_keyboard())
        return

    if action == "subpick" and not SUBSCRIPTIONS_ENABLED:
        await query.message.reply_text(MSG_SUBSCRIPTIONS_COMING_SOON)
        return

    if action == "home" and level == "subscribe" and not SUBSCRIPTIONS_ENABLED:
        await query.message.reply_text(MSG_SUBSCRIPTIONS_COMING_SOON)
        return

    if action == "subpick":
        if level not in ("plus", "pro") or user_id is None:
            return

        awaiting_payment_proof[user_id] = {"tier": level, "since": time.time()}
        await query.edit_message_text(
            payment_steps_text(level),
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(BTN_CANCEL, callback_data="subcancel:_:0")]]),
        )
        return

    if action == "subcancel":
        if user_id is not None:
            awaiting_payment_proof.pop(user_id, None)
        await show_page(query, home_text(query.from_user), home_keyboard())
        return

    if action in ("payok", "payno"):
        if not is_admin(user_id):
            return

        target_id = message_id   # the third part of the button data is the person's ID here

        if action == "payok":
            if level not in ("plus", "pro"):
                return

            until, verb = grant_plan(target_id, level, 30)
            told = await notify_user_granted(context.bot, target_id, level, until)
            result = (
                MSG_APPROVED_FOR_UNTIL.format(plan_level=tier_label(level), verb=verb, name=display_name_for(target_id), target_id=target_id, date_until=format_until(until)) + ("" if told else "\n(I couldn't message them.)")
            )
        else:
            result = MSG_REJECTED_THE_PAYMENT_FROM.format(name=display_name_for(target_id), target_id=target_id)
            with contextlib.suppress(Exception):
                await context.bot.send_message(
                    chat_id=target_id,
                    text=(
                        MSG_I_COULDN_T_CONFIRM_YOUR.format(dm_text=dm_text())
                    ),
                )

        original_caption = query.message.caption or ""
        try:
            await query.edit_message_caption(caption=f"{original_caption}\n\n{result}")
        except Exception:
            await query.message.reply_text(result)
        return

    if action == "ramclean":
        if not is_admin(user_id):
            return

        if level != "yes":
            await query.edit_message_text(MSG_RAM_CLEAN_CANCELLED)
            return

        await perform_ram_clean(query)
        return

    if action == "resetstats":
        if not is_admin(user_id):
            return

        if level != "yes":
            await query.edit_message_text(MSG_CANCELLED_THE_STATISTICS_WERE_NOT)
            return

        try:
            await context.bot.send_document(
                chat_id=ADMIN_USER_ID,
                document=stats_json_bytes(),
                filename=f"stats_before_reset_{quota_today()}.json",
                caption=MSG_A_COPY_OF_THE_STATISTICS,
            )
        except Exception:
            logger.warning("Couldn't send the pre-reset statistics copy")

        reset_stats_data()
        await query.edit_message_text(MSG_STATISTICS_RESET_COUNTING_FROM_ZERO)
        return

    if action == "setlevel":
        if level not in LEVELS or chat_id is None:
            return
        chat_default_level[chat_id] = level
        await query.edit_message_text(
            MSG_DEFAULT_STAR_SET_TO_NEW.format(level_label=LEVELS[level]['label'])
        )
        return

    if action == "cancel":
        refunded_text = ""
        pending = pending_compressions.get(message_id)

        # (If a job is already running for it, that job refunds itself.)
        if pending is not None and message_id not in running_jobs:
            owner_id = pending.get("owner_id")
            charged = int(pending.get("charged_bytes") or pending.get("original_size") or 0)

            if quota_applies(owner_id) and charged > 0:
                quota_refund(owner_id, charged)
                refunded_text = format_size(charged)

        clear_pending(message_id)
        await query.edit_message_text(
            MSG_CANCELLED_FILES_REMOVED_REFUNDED.format(size=refunded_text)
            if refunded_text
            else MSG_CANCELLED_TEMPORARY_FILES_REMOVED
        )
        return

    if action == "pdfmenu":
        pending = pending_compressions.get(message_id)

        if pending is None:
            await query.message.reply_text(
                MSG_THAT_VIDEO_ISN_T_IN_2
            )
            return

        await query.edit_message_text(
            MSG_PICK_A_SCREENSHOT_INTERVAL_ONE,
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
                MSG_THAT_VIDEO_ISN_T_IN_2
            )
            return

        chat_id = pending["chat_id"]
        input_path = pending["input_path"]
        work_dir = pending["work_dir"]

        await query.edit_message_text(
            MSG_EXTRACTING_A_FRAME_EVERY_S.format(interval=interval)
        )

        try:
            pdf_path, frame_count = await extract_pdf_frames(
                str(input_path), work_dir, interval
            )

            if pdf_path is None:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=MSG_COULDN_T_EXTRACT_ANY_FRAMES,
                )
                clear_pending(message_id)
                return

            caption = MSG_FRAME_S_ONE_EVERY_S.format(frame_count=frame_count, interval=interval)

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
                text=MSG_WHAT_WOULD_YOU_LIKE_TO,
                reply_markup=post_pdf_keyboard(message_id, dedup_available=frame_count > 1),
            )

        except Exception as error:
            logger.exception("Error extracting PDF:")
            await context.bot.send_message(
                chat_id=chat_id,
                text=MSG_SOMETHING_WENT_WRONG_EXTRACTING_FRAM.format(error=error),
            )
            clear_pending(message_id)

        return

    if action == "pdfauto":
        pending = pending_compressions.get(message_id)

        if pending is None:
            await query.message.reply_text(
                MSG_THAT_VIDEO_ISN_T_IN_2
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

        scan_label = MSG_SCANNING_THE_VIDEO_FOR_CHANGES.format(interval=interval)
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
                    text=MSG_COULDN_T_EXTRACT_ANY_FRAMES,
                )
                clear_pending(message_id)
                return

            compare_label = MSG_SAMPLED_FRAME_S_COMPARING_FOR.format(count_candidate_paths=len(candidate_paths))
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
                MSG_FRAME_S_KEPT_OUT_OF.format(count_kept_paths=len(kept_paths), count_candidate_paths=len(candidate_paths), interval=interval, threshold_used=threshold_used)
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
            # These frames are already de-duplicated with this threshold: a later
            # "auto-delete similar frames" must reuse it instead of working out a
            # new one from frames that are all different (which deleted real slides).
            pending["dedup_threshold"] = threshold_used
            pending["interval"] = interval
            pending["frames_dir"] = str(work_dir / "frames")
            open_pdf_sessions[chat_id] = message_id

            await context.bot.send_message(
                chat_id=chat_id,
                text=MSG_WHAT_WOULD_YOU_LIKE_TO,
                reply_markup=post_pdf_keyboard(message_id, dedup_available=len(kept_paths) > 1),
            )

        except Exception as error:
            logger.exception("Error during smart PDF capture:")
            await context.bot.send_message(
                chat_id=chat_id,
                text=MSG_SOMETHING_WENT_WRONG_SCANNING_THE.format(error=error),
            )
            clear_pending(message_id)

        return

    if action == "pdfdedup":
        pending = pending_compressions.get(message_id)

        if pending is None:
            await query.message.reply_text(
                MSG_THAT_VIDEO_ISN_T_IN_2
            )
            return

        frames_dir = pending.get("frames_dir")

        if not frames_dir:
            await query.message.reply_text(
                MSG_NO_EXTRACTED_FRAMES_TO_COMPARE
            )
            return

        frame_paths = sorted(Path(frames_dir).glob("frame_*.jpg"))

        if len(frame_paths) < 2:
            await query.answer(MSG_NOTHING_TO_COMPARE_NOT_ENOUGH, show_alert=True)
            return

        chat_id = pending["chat_id"]

        await query.edit_message_text(MSG_COMPARING_FRAMES_FOR_NEAR_DUPLICATES)

        try:
            kept_paths, removed_paths, threshold_used = await dedup_frames(
                frame_paths, threshold=pending.get("dedup_threshold")
            )
            pending["dedup_threshold"] = threshold_used

            if not removed_paths:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=(
                        MSG_NO_EXTREMELY_SIMILAR_FRAMES_FOUND.format(threshold_used=threshold_used)
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
                    MSG_REMOVED_NEAR_DUPLICATE_FRAME_S.format(count_removed_paths=len(removed_paths), count_kept_paths=len(kept_paths), threshold_used=threshold_used)
                ),
                reply_markup=post_pdf_keyboard(message_id, dedup_available=False),
            )

        except Exception as error:
            logger.exception("Error deduplicating frames:")
            await context.bot.send_message(
                chat_id=chat_id,
                text=MSG_SOMETHING_WENT_WRONG_REMOVING_SIMILA.format(error=error),
            )

        return

    if action == "pdfcompress":
        pending = pending_compressions.get(message_id)

        if pending is None:
            await query.message.reply_text(
                MSG_THAT_VIDEO_ISN_T_IN_2
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
            text=MSG_GETTING_SAMPLES_READY,
        )

        async def post_wait_status(text: str) -> None:
            await status_message.edit_text(text)

        await acquire_work_slot(post_wait_status, user_priority(user_id), user_plan_label(user_id))
        try:
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
        finally:
            release_work_slot()
        return

    if action == "pdffinish":
        clear_pending(message_id)

        finished_text = MSG_SESSION_FINISHED_THE_STORED_VIDEO

        try:
            await query.edit_message_text(finished_text)
        except Exception:
            # Tapped from the PDF document message (no text to edit):
            # remove its buttons and post the confirmation instead.
            with contextlib.suppress(Exception):
                await query.edit_message_reply_markup(reply_markup=None)
            await query.message.reply_text(finished_text)
        return

    if action == "audioextract":
        pending = pending_compressions.get(message_id)

        if pending is None:
            await query.message.reply_text(MSG_THAT_VIDEO_ISN_T_IN_2)
            return

        has_audio, _ = await probe_audio_stream(str(pending["input_path"]))

        if not has_audio:
            await query.message.reply_text(MSG_EXTRACT_AUDIO_NO_AUDIO)
            return

        # Name the audio file after the video (unless it has a generic bot-made name
        # or the person already chose a name).
        video_stem = Path(pending.get("filename") or "").stem
        if video_stem and not pending.get("out_name") and not re.fullmatch(r"video_\d+", video_stem):
            pending["out_name"] = sanitize_filename(video_stem)

        # Can the audio be copied out untouched ("Send as is")? Only for AAC / MP3.
        source_info = await probe_video_info(str(pending["input_path"]))
        pending["extract_audio"] = True
        pending["extract_copy_ext"] = EXTRACT_COPY_EXTENSIONS.get(
            (source_info or {}).get("audio_codec") or ""
        )

        duration = pending.get("duration")
        audio_estimates = {
            audio_key: estimate_audio_bytes(AUDIO_LEVELS[audio_key]["bitrate"], duration)
            for audio_key in AUDIO_LEVEL_ORDER
        }

        await query.edit_message_text(
            MSG_EXTRACT_AUDIO_PICK_QUALITY,
            reply_markup=audio_keyboard(
                message_id, AUDIO_ONLY_LEVEL, audio_estimates, DEFAULT_AUDIO_LEVEL,
                from_video=True, copy_ok=pending["extract_copy_ext"] is not None,
            ),
        )
        return

    if action == "audioedit":
        pending = pending_compressions.get(message_id)

        if pending is None:
            await query.message.reply_text(
                MSG_THAT_VIDEO_ISN_T_IN_2
            )
            return

        has_audio, source_audio_bitrate = await probe_audio_stream(str(pending["input_path"]))

        if not has_audio:
            await query.message.reply_text(
                MSG_THIS_VIDEO_HAS_NO_AUDIO
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
            MSG_JUST_EDITING_THE_AUDIO_THE,
            reply_markup=audio_keyboard(message_id, AUDIO_EDIT_LEVEL, audio_estimates, DEFAULT_AUDIO_LEVEL),
        )
        return

    if action == "audioreplace":
        pending = pending_compressions.get(message_id)

        if pending is None:
            await query.message.reply_text(
                MSG_THAT_VIDEO_ISN_T_IN_2
            )
            return

        chat_id = pending["chat_id"]
        awaiting_audio_replacement[chat_id] = message_id

        await context.bot.send_message(
            chat_id=chat_id,
            text=(
                MSG_SEND_ME_THE_NEW_AUDIO
            ),
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton(BTN_CANCEL, callback_data=f"audioreplacecancel:_:{message_id}")
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
                MSG_AUDIO_REPLACEMENT_CANCELLED_PICK_ANO
            )
        except Exception:
            await query.message.reply_text(MSG_AUDIO_REPLACEMENT_CANCELLED)
        return

    if action == "videocover":
        pending = pending_compressions.get(message_id)

        if pending is None:
            await query.message.reply_text(
                MSG_THAT_VIDEO_ISN_T_IN_2
            )
            return

        chat_id = pending["chat_id"]
        awaiting_input[chat_id] = {"mid": message_id, "field": "videocover"}

        await context.bot.send_message(
            chat_id=chat_id,
            text=(
                MSG_SEND_ME_THE_IMAGE_TO
                + ("\n(A cover is already set — a new image replaces it.)" if pending.get("cover_path") else "")
            ),
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton(BTN_CANCEL, callback_data=f"ameta:stop:{message_id}")
            ]]),
        )
        return

    if action == "ameta":
        pending = pending_compressions.get(message_id)

        if pending is None:
            await query.message.reply_text(
                MSG_THAT_FILE_ISN_T_IN
            )
            return

        chat_id = pending["chat_id"]
        field = level

        if field == "open":
            pending["meta_panel_id"] = None
            await show_audio_meta_panel(context, chat_id, message_id)
            return

        if field in ("name", "artist", "album", "cover"):
            awaiting_input[chat_id] = {"mid": message_id, "field": field}
            prompts = {
                "name": MSG_SEND_THE_NEW_FILE_NAME,
                "artist": MSG_SEND_THE_ARTIST_NAME_NOW,
                "album": MSG_SEND_THE_ALBUM_NAME_NOW,
                "cover": MSG_SEND_THE_ALBUM_COVER_NOW,
            }
            await context.bot.send_message(
                chat_id=chat_id,
                text=prompts[field],
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton(BTN_CANCEL, callback_data=f"ameta:stop:{message_id}")
                ]]),
            )
            return

        if field == "stop":
            if awaiting_input.get(chat_id, {}).get("mid") == message_id:
                awaiting_input.pop(chat_id, None)
            try:
                await query.edit_message_text(MSG_CANCELLED)
            except Exception:
                pass
            return

        if field == "clear":
            for key in ("out_name", "artist", "album", "cover_path", "cover_thumb_path"):
                pending.pop(key, None)
            await show_audio_meta_panel(context, chat_id, message_id)
            return

        if field == "done":
            if awaiting_input.get(chat_id, {}).get("mid") == message_id:
                awaiting_input.pop(chat_id, None)
            pending["meta_panel_id"] = None
            try:
                await query.edit_message_text(
                    audio_meta_text(pending).split("\n\nTap a button")[0]
                    + MSG_SAVED_NOW_PICK_THE_QUALITY
                )
            except Exception:
                pass
            return

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
                MSG_THAT_FILE_ISN_T_IN
            )
            return

        if chosen_audio == ORIGINAL_AUDIO:
            # Nothing to enhance or boost when the audio is kept as is: go straight to compressing.
            if chosen_level not in LEVELS:
                return

            await run_compression(
                update, context,
                input_path=pending["input_path"],
                work_dir=pending["work_dir"],
                original_size=pending["original_size"],
                level=chosen_level,
                chat_id=pending["chat_id"],
                source_message_id=message_id,
                status_message=query.message,
                duration=pending.get("duration"),
                audio_level=ORIGINAL_AUDIO,
            )
            return

        header = flow_header(chosen_level, chosen_audio)

        await query.edit_message_text(
            MSG_OPTIONAL_AUDIO_ENHANCEMENTS_TOGGLE_T.format(header=header),
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
            MSG_OPTIONAL_AUDIO_ENHANCEMENTS_TOGGLE_T.format(header=header),
            reply_markup=enhancement_keyboard(
                message_id, chosen_level, chosen_audio, noise_reduction, voice_enhancement
            ),
        )
        return

    if action == "enhpreview":
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
                MSG_THAT_FILE_ISN_T_IN
            )
            return

        chat_id = pending["chat_id"]
        input_path = pending["input_path"]
        work_dir = pending["work_dir"]
        duration = pending.get("duration")
        audio_bitrate = AUDIO_LEVELS[chosen_audio]["bitrate"]
        header = flow_header(chosen_level, chosen_audio)

        await query.edit_message_text(
            MSG_GENERATING_S_ENHANCEMENT_PREVIEWS_ON.format(ENHANCEMENT_PREVIEW_SECONDS=ENHANCEMENT_PREVIEW_SECONDS)
        )

        sample_duration = (
            min(ENHANCEMENT_PREVIEW_SECONDS, duration) if duration else ENHANCEMENT_PREVIEW_SECONDS
        )
        seek = max(0.0, (duration - sample_duration) / 2) if duration else 0.0

        # The same clip four ways, so they can be compared directly.
        variants = [
            ("original", "Original (no enhancement)", False, False),
            ("noise", MSG_NOISE_REDUCTION_2, True, False),
            ("voice", MSG_VOICE_ENHANCEMENT_2, False, True),
            ("both", MSG_BOTH_TOGETHER, True, True),
        ]

        preview_semaphore = asyncio.Semaphore(ESTIMATE_CONCURRENCY)

        async def bounded_enhancement_preview(key, label, nr, ve):
            preview_path = work_dir / f"preview_enh_{key}.m4a"
            async with preview_semaphore:
                ok = await generate_volume_preview(
                    str(input_path), str(preview_path), seek, sample_duration, audio_bitrate, 0,
                    noise_reduction=nr, voice_enhancement=ve,
                )
            return label, (preview_path if ok else None)

        results = await asyncio.gather(
            *(bounded_enhancement_preview(*variant) for variant in variants)
        )

        await context.bot.send_message(
            chat_id=chat_id,
            text=MSG_S_PREVIEW_OF_THE_SAME.format(sample_duration=sample_duration),
        )

        for label, preview_path in results:
            if preview_path is None:
                await context.bot.send_message(
                    chat_id=chat_id, text=MSG_COULDN_T_GENERATE_THE_PREVIEW.format(label=label)
                )
                continue

            try:
                await with_retries(
                    context.bot.send_audio,
                    chat_id=chat_id,
                    audio=str(preview_path),
                    title=label,
                    caption=label,
                )
            finally:
                preview_path.unlink(missing_ok=True)

        await context.bot.send_message(
            chat_id=chat_id,
            text=MSG_OPTIONAL_AUDIO_ENHANCEMENTS_TOGGLE_T.format(header=header),
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
                MSG_THAT_FILE_ISN_T_IN
            )
            return

        chat_id = pending["chat_id"]
        input_path = pending["input_path"]
        work_dir = pending["work_dir"]
        duration = pending.get("duration")
        audio_bitrate = AUDIO_LEVELS[chosen_audio]["bitrate"]

        enhancement_bits = []
        if noise_reduction:
            enhancement_bits.append(MSG_NOISE_REDUCTION_3)
        if voice_enhancement:
            enhancement_bits.append(MSG_VOICE_ENHANCEMENT_3)
        enhancement_note = f" ({' + '.join(enhancement_bits)})" if enhancement_bits else ""

        await query.edit_message_text(
            MSG_GENERATING_S_PREVIEWS_AT_EACH.format(VOLUME_PREVIEW_SECONDS=VOLUME_PREVIEW_SECONDS, enhancement_note=enhancement_note)
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
            text=MSG_S_PREVIEW_AT_EACH_VOLUME.format(sample_duration=sample_duration, enhancement_note=enhancement_note),
        )

        for sample_number, (boost, preview_path) in enumerate(
            sorted(results, key=lambda item: item[0]), start=1
        ):
            label = "No boost" if boost == 0 else f"+{boost}%"

            if preview_path is None:
                await context.bot.send_message(
                    chat_id=chat_id, text=MSG_COULDN_T_GENERATE_A_PREVIEW.format(label=label)
                )
                continue

            try:
                await with_retries(
                    context.bot.send_audio,
                    chat_id=chat_id,
                    audio=str(preview_path),
                    title=MSG_VOLUME_SAMPLE_TITLE.format(number=sample_number),
                    performer=VOLUME_SAMPLE_PERFORMER,
                    caption=MSG_HANDLE_CALLBACK_INNER.format(label=label),
                )
            finally:
                preview_path.unlink(missing_ok=True)

        await context.bot.send_message(
            chat_id=chat_id,
            text=MSG_PICK_A_VOLUME_LEVEL,
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
                MSG_THAT_FILE_ISN_T_IN
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

    if action == "audiosend":
        pending = pending_compressions.get(message_id)

        is_extraction = bool(pending and pending.get("extract_audio"))

        if pending is None or (pending.get("media_type") != "audio" and not is_extraction):
            await query.message.reply_text(MSG_THAT_FILE_ISN_T_IN)
            return

        if is_extraction and not pending.get("extract_copy_ext"):
            await query.message.reply_text(MSG_EXTRACT_AUDIO_CANT_COPY)
            return

        if message_id in active_job_ids:
            return   # already being processed: ignore a double tap

        if awaiting_input.get(pending["chat_id"], {}).get("mid") == message_id:
            awaiting_input.pop(pending["chat_id"], None)   # they moved on from a half-finished prompt

        await run_compression(
            update, context,
            input_path=pending["input_path"],
            work_dir=pending["work_dir"],
            original_size=pending["original_size"],
            level=AUDIO_COPY_LEVEL,
            chat_id=pending["chat_id"],
            source_message_id=message_id,
            status_message=query.message,
            duration=pending.get("duration"),
            audio_level=ORIGINAL_AUDIO,
        )
        return

    if action == "coveronly":
        pending = pending_compressions.get(message_id)

        if pending is None:
            await query.message.reply_text(MSG_THAT_VIDEO_ISN_T_IN_2)
            return

        cover_file = pending.get("cover_path")

        if not cover_file or not Path(cover_file).exists():
            await query.message.reply_text(MSG_THAT_VIDEO_ISN_T_IN_2)
            return

        if message_id in active_job_ids:
            return   # already being processed: ignore a double tap

        await run_compression(
            update, context,
            input_path=pending["input_path"],
            work_dir=pending["work_dir"],
            original_size=pending["original_size"],
            level=COVER_ONLY_LEVEL,
            chat_id=pending["chat_id"],
            source_message_id=message_id,
            status_message=query.message,
            duration=pending.get("duration"),
        )
        return

    if action == "levelback":
        pending = pending_compressions.get(message_id)

        if pending is None:
            await query.message.reply_text(
                MSG_THAT_VIDEO_ISN_T_IN_2
            )
            return

        menu_text = pending.get("menu_text")
        if not menu_text:
            return

        level_menu_args = (
            message_id,
            pending.get("estimates", {}),
            pending.get("default_level"),
            pending.get("smart"),
            pending.get("original_size"),
        )

        try:
            await query.edit_message_text(
                menu_text, reply_markup=estimate_keyboard(*level_menu_args)
            )
        except BadRequest:
            await query.edit_message_text(
                menu_text, reply_markup=estimate_keyboard(*level_menu_args, styled=False)
            )
        return

    if level not in LEVELS:
        return

    if action == "smart":
        pending = pending_compressions.get(message_id)
        recommendation = pending.get("smart") if pending else None

        if pending is None or recommendation is None:
            await query.message.reply_text(
                MSG_THAT_VIDEO_ISN_T_IN_2
            )
            return

        await run_compression(
            update, context,
            input_path=pending["input_path"],
            work_dir=pending["work_dir"],
            original_size=pending["original_size"],
            level=recommendation["level"],
            chat_id=pending["chat_id"],
            source_message_id=message_id,
            status_message=query.message,
            duration=pending.get("duration"),
            audio_level=recommendation["audio_level"],
            max_fps=recommendation.get("max_fps"),
            smart=True,
        )
        return

    if action == "compress":
        pending = pending_compressions.get(message_id)

        if pending is None:
            await query.message.reply_text(
                MSG_THAT_VIDEO_ISN_T_IN_2
            )
            return

        duration = pending.get("duration")
        level_settings = LEVELS[level]
        total_estimate = pending.get("estimates", {}).get(level)

        if level_grows_file(total_estimate, pending.get("original_size")):
            await query.message.reply_text(MSG_LEVEL_WOULD_GROW_FILE)
            return

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

        # "Original" keeps the source audio as is: video estimate + the source's own audio size.
        source_info = ((pending or {}).get("smart") or {}).get("info") or {}
        if source_info.get("has_audio") is False:
            source_audio_bytes = 0
        elif source_info.get("audio_bps") and duration:
            source_audio_bytes = int(source_info["audio_bps"] * duration / 8)
        else:
            source_audio_bytes = None
        audio_estimates[ORIGINAL_AUDIO] = (
            video_only_estimate + source_audio_bytes
            if video_only_estimate is not None and source_audio_bytes is not None
            else None
        )

        await query.edit_message_text(
            MSG_SELECTED_NOW_PICK_AUDIO_QUALITY.format(level_settings_label=level_settings['label']),
            reply_markup=audio_keyboard(message_id, level, audio_estimates, DEFAULT_AUDIO_LEVEL),
        )
        return


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
        .post_stop(post_stop)
        .concurrent_updates(CONCURRENT_UPDATES if CONCURRENT_UPDATES > 0 else False)
        .build()
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("setlevel", setlevel))
    application.add_handler(CommandHandler("merge", merge_command))
    application.add_handler(CommandHandler("users", users_command))
    application.add_handler(CommandHandler("ram", ram_command))
    application.add_handler(CommandHandler("ram_clean", ram_clean_command))
    application.add_handler(CommandHandler("stats", stats_command))
    application.add_handler(CommandHandler("reset_stats", reset_stats_command))
    application.add_handler(CommandHandler("update", update_command))
    # Runs before everything else (group -2): people must join the channel first.
    application.add_handler(TypeHandler(Update, capture_referral), group=-3)
    application.add_handler(TypeHandler(Update, require_channel_membership), group=-2)
    # Then (group -1) keep the user list current.
    application.add_handler(TypeHandler(Update, track_user), group=-1)
    application.add_handler(CommandHandler("limit", limit_command))
    application.add_handler(CommandHandler("plans", plans_command))
    application.add_handler(CommandHandler("invite", invite_command))
    application.add_handler(CommandHandler("subscribe", subscribe_command))
    application.add_handler(CommandHandler("grant", grant_command))
    application.add_handler(CommandHandler("revoke", revoke_command))
    application.add_handler(CommandHandler("subs", subs_command))
    application.add_handler(MessageHandler(filters.VIDEO, handle_video))
    application.add_handler(MessageHandler(filters.AUDIO | filters.VOICE, handle_audio))
    application.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    application.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    # Admin forwards a text post from the backup channel -> bot replies with the channel ID.
    application.add_handler(
        MessageHandler(filters.FORWARDED & filters.TEXT & ~filters.COMMAND, report_forwarded_channel_id)
    )
    # Plain text = a new file name / album name, but only while a prompt is open.
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_input))
    # Must come BEFORE the general button handler: the first matching
    # handler wins, and access-request buttons have their own format.
    application.add_handler(CallbackQueryHandler(handle_access_decision, pattern=r"^access:"))
    application.add_handler(CallbackQueryHandler(handle_callback))
    application.add_error_handler(error_handler)

    logger.info("Bot is running.")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

    if RESTART_REQUESTED:
        # run_polling() has returned: the bot stopped cleanly and its last backup is saved.
        logger.info("Restarting the bot process (/ram_clean)...")
        os.environ[RESTART_ENV_FLAG] = str(time.time())

        try:
            os.execv(sys.executable, [sys.executable, *sys.argv])
        except OSError:
            logger.exception("Couldn't restart in place — exiting so the host restarts the bot.")
            raise SystemExit(1)


if __name__ == "__main__":
    main()
