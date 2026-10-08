FROM python:3.11-slim

# ffmpeg for moviepy + fonts for English/Hindi/Marathi/Telugu subtitles
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg fonts-dejavu-core fonts-noto-core \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

ENV PYTHONUNBUFFERED=1 \
    VIDEO_PRESET=ultrafast \
    VIDEO_THREADS=1 \
    VIDEO_FPS=24 \
    VIDEO_BITRATE=3000k \
    SESSION_COOKIE_SECURE=true \
    LOW_MEMORY_RENDER=true

# 1 worker only: task progress is kept in memory, so all requests must hit the same process
CMD gunicorn app:app --bind 0.0.0.0:${PORT:-10000} --workers 1 --threads 4 --timeout 300
