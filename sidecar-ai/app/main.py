"""media-ai sidecar HTTP API.

Phase 1 endpoints:
  GET  /healthz      liveness + active config (no model load)
  POST /transcribe   transcribe a media file under the downloads root

SMDL calls /transcribe with a path relative to its /downloads mount; this
service mounts the same media volume at MEDIA_AI_DOWNLOADS_DIR (read-only) so the
path resolves to the same bytes. The result is the transcript + segment
timestamps + speed metrics. Summary/chapters (Phase 2) and search (Phase 3) add
their own endpoints alongside this one.
"""
from __future__ import annotations

import logging
import os
from concurrent.futures import ThreadPoolExecutor

from fastapi import FastAPI, HTTPException
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field

from . import config, engine

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("media-ai")

app = FastAPI(title="Sentinel Media AI", version="0.1.0")

# Transcription is CPU-bound and blocking; run it on a small dedicated pool so a
# long job doesn't starve the event loop or pile up unbounded.
_pool = ThreadPoolExecutor(max_workers=int(os.environ.get("MEDIA_AI_WORKERS", "1")),
                           thread_name_prefix="transcribe")


def _resolve_media_path(rel_or_abs: str) -> str:
    """Resolve a request path to a real file UNDER the downloads root, rejecting
    traversal. Accepts either a path relative to the root ('Instagram/x.mp4') or
    an absolute path already inside it ('/downloads/Instagram/x.mp4')."""
    raw = (rel_or_abs or "").strip()
    if not raw:
        raise HTTPException(400, "path is required")
    root = os.path.realpath(config.DOWNLOADS_DIR)
    candidate = raw if os.path.isabs(raw) else os.path.join(root, raw)
    real = os.path.realpath(candidate)
    if real != root and not real.startswith(root + os.sep):
        raise HTTPException(400, "path escapes the downloads root")
    if not os.path.isfile(real):
        raise HTTPException(404, f"file not found: {raw}")
    return real


class TranscribeRequest(BaseModel):
    path: str = Field(..., description="file path relative to (or under) the downloads root")
    model: str | None = Field(None, description="whisper model size; default from env")
    language: str | None = Field(None, description="force language (e.g. 'en'); default = auto-detect")
    engine: str = Field("auto", description="auto|cpu|gpu (gpu reserved; falls back to cpu)")


@app.get("/healthz")
async def healthz():
    return {
        "ok": True,
        "service": "media-ai",
        "version": app.version,
        "engine_default": engine._cpu.name,
        "model_default": config.MODEL,
        "compute_type": config.COMPUTE_TYPE,
        "models_dir": config.MODELS_DIR,
        "downloads_dir": config.DOWNLOADS_DIR,
        "gpu_broker_enabled": config.GPU_BROKER_ENABLED,
        "qwen_wired": bool(config.QWEN_URL),
    }


@app.post("/transcribe")
async def transcribe(req: TranscribeRequest):
    real = _resolve_media_path(req.path)
    model_size = (req.model or config.MODEL).strip()
    eng = engine.resolve(req.engine)
    logger.info("transcribe path=%s model=%s engine=%s", req.path, model_size, eng.name)
    try:
        result = await run_in_threadpool(
            eng.transcribe, real, model_size=model_size, language=req.language
        )
    except Exception as e:  # noqa: BLE001
        logger.exception("transcribe failed")
        raise HTTPException(500, f"transcription failed: {e}")
    return {
        "ok": True,
        "path": req.path,
        "engine": result.engine,
        "model": result.model,
        "language": result.language,
        "language_probability": result.language_probability,
        "duration": result.duration,
        "transcribe_seconds": result.transcribe_seconds,
        "realtime_factor": result.realtime_factor,
        "segment_count": len(result.segments),
        "text": result.text,
        "segments": [s.__dict__ for s in result.segments],
    }


@app.on_event("startup")
async def _startup():
    # Preload the default model so the first /transcribe isn't penalised by the
    # ~1-2 s model load. Best-effort: a download hiccup must not crash the boot.
    try:
        await run_in_threadpool(engine.warmup)
        logger.info("warmup complete (model=%s)", config.MODEL)
    except Exception as e:  # noqa: BLE001
        logger.warning("warmup skipped: %s", e)
