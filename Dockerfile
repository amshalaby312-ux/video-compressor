FROM python:3.12-slim

# Install FFmpeg
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Create application directory
WORKDIR /app

# Install Python dependencies
COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

# Copy bot
COPY bot.py .

# Start bot
CMD ["python", "bot.py"]
