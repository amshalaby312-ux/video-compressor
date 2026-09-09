# Stage 1: grab the prebuilt Telegram Bot API server binary
FROM aiogram/telegram-bot-api:latest AS botapi

# Stage 2: your actual bot image — Alpine, to match the musl-linked binary above
FROM python:3.12-alpine

RUN apk add --no-cache ffmpeg ca-certificates

# Bring in the telegram-bot-api server binary from stage 1
COPY --from=botapi /usr/local/bin/telegram-bot-api /usr/local/bin/telegram-bot-api

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py .
COPY start.sh .
RUN chmod +x start.sh

CMD ["./start.sh"]
