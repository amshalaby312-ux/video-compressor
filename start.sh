#!/bin/sh
set -e

mkdir -p /data

# Start the Local Bot API server in the background
telegram-bot-api \
  --api-id="${TELEGRAM_API_ID}" \
  --api-hash="${TELEGRAM_API_HASH}" \
  --local \
  --http-port=8081 \
   --dir=/data &

# Give it a couple seconds to come up before the bot tries to talk to it
sleep 3

# Start the Python bot in the foreground (keeps the container alive)
exec python bot.py
