# ---- Stage 1: build the official Telegram Bot API server from source ----
# Built on the same glibc base as the runtime stage so the resulting
# binary is guaranteed compatible (no musl/glibc mismatch).
FROM debian:bookworm-slim AS tgapi-builder

RUN apt-get update && apt-get install -y --no-install-recommends \
    git ca-certificates make cmake g++ gperf libssl-dev zlib1g-dev \
    && rm -rf /var/lib/apt/lists/*

RUN git clone --recursive https://github.com/tdlib/telegram-bot-api.git /src/telegram-bot-api
WORKDIR /src/telegram-bot-api/build
RUN cmake -DCMAKE_BUILD_TYPE=Release .. \
    && cmake --build . --target install -j"$(nproc)"
# Installs the `telegram-bot-api` binary to /usr/local/bin by default.

# ---- Stage 2: runtime image (bot + API server together) ----
FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg ghostscript libssl3 zlib1g \
    && rm -rf /var/lib/apt/lists/*

COPY --from=tgapi-builder /usr/local/bin/telegram-bot-api /usr/local/bin/telegram-bot-api

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py entrypoint.sh ./
RUN chmod +x entrypoint.sh

# Working directory for the local Bot API server (downloaded/uploaded files
# live here on disk - same container, same filesystem as the Python process).
ENV TGAPI_DIR=/data/tgapi
RUN mkdir -p ${TGAPI_DIR}

# The bot talks to the API server over localhost now instead of a
# separate Railway service, and TELEGRAM_LOCAL turns on local_mode in bot.py.
ENV LOCAL_BOT_API_URL=http://127.0.0.1:8081
ENV TELEGRAM_LOCAL=1

ENTRYPOINT ["./entrypoint.sh"]
