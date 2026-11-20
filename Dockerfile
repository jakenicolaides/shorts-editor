# The editor on Linux: what edits an ambassador's take on the server.
# Build:  docker build -t shorts-editor .
# Run:    docker run --rm -v "$PWD/take:/take" shorts-editor /take/game.mp4 /take/cam.mp4 --game twixtle
FROM python:3.13-slim-trixie

RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements-linux.txt .
RUN pip install --no-cache-dir -r requirements-linux.txt

# The speech model goes INTO the image (1.6 GB): a server that fetched it per take
# would spend longer downloading than editing, and would depend on another site being up.
ENV HF_HOME=/opt/models HF_HUB_DISABLE_TELEMETRY=1
RUN python -c "from faster_whisper import WhisperModel; WhisperModel('large-v3-turbo', device='cpu', compute_type='float32')"
ENV HF_HUB_OFFLINE=1

COPY shorts_editor ./shorts_editor
COPY templates ./templates

ENV SHORTS_WORK=/work PYTHONUNBUFFERED=1
RUN mkdir -p /work
ENTRYPOINT ["python", "-m", "shorts_editor.worker"]
