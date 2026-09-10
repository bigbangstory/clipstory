# Built for linux/arm64 (Oracle Ampere A1) but architecture-neutral: the base
# image, ffmpeg and the Python wheels all come from multi-arch sources.
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    DEBIAN_FRONTEND=noninteractive

# ffmpeg is the only system dependency. No build-essential: every Python
# dependency ships a binary wheel, and leaving a compiler out of the runtime
# image keeps it smaller and reduces what an attacker could use if they got in.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /srv

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app/ ./app/

# A real home directory: faster-whisper and Hugging Face write caches under
# $HOME, and a system user without one fails on the first transcription.
RUN useradd --system --create-home --uid 1000 clipstory \
 && mkdir -p /data && chown -R clipstory:clipstory /srv /data
USER clipstory

# Whisper weights live on the data volume (DATA_DIR/models), so a rebuild or
# redeploy does not download them again. Pre-fetching here means the first
# real job does not wait on a 150 MB download either.
ENV DATA_DIR=/data \
    HF_HOME=/data/models/hf
ARG WHISPER_MODEL_SIZE=base
RUN python -c "from faster_whisper import WhisperModel; WhisperModel('${WHISPER_MODEL_SIZE}', device='cpu', compute_type='int8', download_root='/data/models')"

EXPOSE 8000

# Overridden to `python -m app.worker` for the worker service.
CMD ["sh", "-c", "uvicorn app.web:app --host 0.0.0.0 --port ${PORT:-8000} --workers ${WEB_WORKERS:-2} --proxy-headers --forwarded-allow-ips='*'"]
