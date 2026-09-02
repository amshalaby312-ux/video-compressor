#!/usr/bin/env bash
set -euo pipefail

: "${TELEGRAM_API_ID:?Set TELEGRAM_API_ID (from https://my.telegram.org)}"
: "${TELEGRAM_API_HASH:?Set TELEGRAM_API_HASH (from https://my.telegram.org)}"

telegram-bot-api \
    --api-id="${TELEGRAM_API_ID}" \
    --api-hash="${TELEGRAM_API_HASH}" \
    --local \
    --http-port=8081 \
    --dir="${TGAPI_DIR}" \
    --log="${TGAPI_DIR}/tgapi.log" &
TGAPI_PID=$!

cleanup() {
    kill "${TGAPI_PID}" 2>/dev/null || true
}
trap cleanup EXIT

# Wait for the API server's port to accept connections (plain TCP check -
# the server doesn't return 200 on a bare GET, so we can't curl-probe it).
for _ in $(seq 1 30); do
    if (exec 3<>"/dev/tcp/127.0.0.1/8081") 2>/dev/null; then
        exec 3<&- 3>&-
        break
    fi
    sleep 0.5
done

exec python bot.py
