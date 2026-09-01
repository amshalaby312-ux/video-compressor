# Local Telegram Bot API server — raises the 20MB/50MB Bot API limits to
# ~2000MB. Deploy this as its OWN Railway service (separate from bot.py) —
# create a second service in the same Railway project pointing at this
# subfolder as the root directory.
#
# Uses the official prebuilt image from the aiogram/telegram-bot-api project,
# which wraps the same server Telegram maintains at tdlib/telegram-bot-api.
FROM aiogram/telegram-bot-api:latest

# The image's entrypoint reads TELEGRAM_API_ID and TELEGRAM_API_HASH
# directly from the environment — set them in Railway's Variables tab for
# THIS service (not in this file, and not the same service as the bot).
#
# Listens on port 8081 by default.
EXPOSE 8081
