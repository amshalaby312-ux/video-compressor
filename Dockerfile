# Dockerfile for deploying the Telegram compressor bot on Railway
# (or any other Docker-based host).

FROM python:3.11-slim

# System deps: ffmpeg for video, ghostscript for PDF compression
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    ghostscript \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py .

# This bot uses long-polling (not webhooks), so no port needs to be exposed.
CMD ["python", "bot.py"]
