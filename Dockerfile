# On-site image for the VIGI bridge: run it on a box on the same LAN as the NVR
# so it can pull snapshots and RTSP clips.
FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
ENV VIGI_DATA_DIR=/data PORT=8080
VOLUME /data
EXPOSE 8080
CMD ["sh", "-c", "gunicorn app:app --bind 0.0.0.0:${PORT} --workers 1 --threads 4"]
