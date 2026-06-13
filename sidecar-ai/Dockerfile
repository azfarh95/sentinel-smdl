# media-ai sidecar — Sentinel Media's transcription/summary/search service.
#
# Deliberately a SEPARATE image from `smdl`: the heavy ML stack (CTranslate2,
# onnxruntime, PyAV) stays out of the SMDL web image so the OSS/community SMDL
# build keeps its "no AI dependency" promise. SMDL talks to this over HTTP and
# degrades dark when SMDL_MEDIA_AI_URL is unset.
FROM python:3.11-slim

# libgomp1 = OpenMP runtime that CTranslate2 / onnxruntime link against.
# PyAV bundles its own ffmpeg libraries, so no system ffmpeg is required to
# decode mp4/mkv/webm audio.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgomp1 curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# Whisper model weights are downloaded on first use into MEDIA_AI_MODELS_DIR
# (mounted as a named volume so they survive rebuilds).
ENV MEDIA_AI_MODELS_DIR=/models \
    MEDIA_AI_DOWNLOADS_DIR=/downloads \
    MEDIA_AI_PORT=8097 \
    HF_HUB_DISABLE_TELEMETRY=1

EXPOSE 8097

HEALTHCHECK --interval=30s --timeout=10s --start-period=20s --retries=3 \
    CMD curl -sf http://localhost:8097/healthz || exit 1

CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8097"]
