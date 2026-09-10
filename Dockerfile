# Built for linux/arm64 (Oracle Ampere A1) but architecture-neutral: the base
# image and ffmpeg both come from Debian's multi-arch repositories.
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    DEBIAN_FRONTEND=noninteractive

# ffmpeg is the only system dependency. No build-essential: psycopg ships
# binary wheels, and leaving a compiler out of the runtime image keeps it
# smaller and reduces what an attacker could use if they got in.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /srv

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app/ ./app/

# The worker writes clips here; compose mounts a volume over it so the files
# survive a redeploy.
RUN mkdir -p /data && useradd --system --uid 1000 clipstory \
 && chown -R clipstory:clipstory /srv /data
USER clipstory

ENV DATA_DIR=/data
EXPOSE 8000

# Overridden to `python -m app.worker` for the worker service.
CMD ["sh", "-c", "uvicorn app.web:app --host 0.0.0.0 --port ${PORT:-8000} --workers ${WEB_WORKERS:-2} --proxy-headers --forwarded-allow-ips='*'"]
