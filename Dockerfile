# Runs the archiver/video-capture logic in a container. The Chromium-family
# browser itself is NOT containerized here — it stays on the host, already
# logged into LinkedIn, exposing its CDP port. This container only ever
# attaches to that existing session over the network; it never launches or
# manages the browser itself (see "Container limitations" in README).
FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ ./src/
COPY scripts/ ./scripts/

ENV PYTHONPATH=/app/src

# data/archive/logs are meant to be bind-mounted from the host so output
# survives after the container exits.
VOLUME ["/app/data", "/app/archive", "/app/logs"]

ENTRYPOINT ["python", "scripts/archive_linkedin_posts.py"]
