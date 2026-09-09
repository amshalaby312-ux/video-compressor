# Stage 1: grab the prebuilt Telegram Bot API server binary
FROM aiogram/telegram-bot-api:latest AS botapi

# Stage 2: your actual bot image
FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ffmpeg \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Bring in the telegram-bot-api server binary from stage 1
COPY --from=botapi /usr/local/bin/telegram-bot-api /usr/local/bin/telegram-bot-api

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py .
COPY start.sh .
RUN chmod +x start.sh

CMD ["./start.sh"]
