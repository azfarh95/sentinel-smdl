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

from . import broker, config, embed, engine, gpu, store, summarize
from .engine import Segment

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


def _transcribe_cached(real: str, key: str, model_size: str,
                       language: str | None, fresh: bool = False) -> dict:
    """Transcript for `real`, served from cache when the file is unchanged
    (mtime+size). `key` is the cache key = the caller's relative path (the same
    value used as media_path for index/search/status). BLOCKING (CPU)."""
    st = os.stat(real)
    mtime, size = st.st_mtime, st.st_size
    if not fresh:
        cached = store.get_transcript(key, mtime, size)
        if cached:
            return cached
    eng = engine.resolve("auto")
    result = eng.transcribe(real, model_size=model_size, language=language)
    out = {
        "language": result.language,
        "language_probability": result.language_probability,
        "duration": result.duration,
        "text": result.text,
        "segments": [s.__dict__ for s in result.segments],
        "transcribe_seconds": result.transcribe_seconds,
        "realtime_factor": result.realtime_factor,
        "engine": result.engine, "model": result.model,
        "cached": False,
    }
    store.put_transcript(key, mtime, size, out)
    return out


class TranscribeRequest(BaseModel):
    path: str = Field(..., description="file path relative to (or under) the downloads root")
    model: str | None = Field(None, description="whisper model size; default from env")
    language: str | None = Field(None, description="force language (e.g. 'en'); default = auto-detect")
    engine: str = Field("auto", description="auto|cpu|gpu (gpu reserved; falls back to cpu)")
    fresh: bool = Field(False, description="bypass the cache and re-transcribe")


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
        "summary_enabled": summarize.enabled(),
        "qwen_model": config.QWEN_MODEL if summarize.enabled() else None,
        "vram_free_gb": gpu.free_vram_gb(),
        "min_vram_gb": config.MIN_VRAM_GB,
        "embed_model": config.EMBED_MODEL,
        "index": _safe_stats(),
    }


def _safe_stats() -> dict:
    try:
        return store.stats()
    except Exception:  # noqa: BLE001
        return {"segments": None, "media": None}


class SummarizeRequest(BaseModel):
    path: str | None = Field(None, description="media path to transcribe first (under downloads root)")
    transcript: str | None = Field(None, description="plain transcript text (alternative to path)")
    segments: list[dict] | None = Field(None, description="pre-computed segments [{start,end,text}]")
    model: str | None = Field(None, description="whisper model size when transcribing from path")
    language: str | None = Field(None, description="force transcription language")


def _segments_from_request(req: "SummarizeRequest") -> tuple[list[Segment], str | None]:
    """Resolve the request into transcript segments + detected language. Order of
    precedence: explicit segments > plain transcript > transcribe a media path."""
    if req.segments:
        return ([Segment(float(s.get("start", 0)), float(s.get("end", 0)),
                          str(s.get("text", "")).strip()) for s in req.segments],
                req.language)
    if req.transcript and req.transcript.strip():
        return ([Segment(0.0, 0.0, req.transcript.strip())], req.language)
    if req.path:
        real = _resolve_media_path(req.path)
        tx = _transcribe_cached(real, req.path, (req.model or config.MODEL).strip(), req.language)
        segs = [Segment(s["start"], s["end"], s["text"]) for s in tx["segments"]]
        return segs, tx["language"]
    raise HTTPException(400, "provide one of: segments, transcript, or path")


@app.post("/summarize")
async def summarize_endpoint(req: SummarizeRequest):
    if not summarize.enabled():
        raise HTTPException(503, "summary not configured (MEDIA_AI_QWEN_URL unset)")

    # Summary cache (path-based only): a stored summary skips the GPU entirely.
    cache_key = mtime = size = None
    if req.path and not req.transcript and not req.segments:
        real = _resolve_media_path(req.path)
        st = os.stat(real)
        cache_key, mtime, size = req.path, st.st_mtime, st.st_size
        cached = await run_in_threadpool(store.get_summary, cache_key, mtime, size)
        if cached:
            return {"ok": True, "cached": True, **cached}

    # Resolve the transcript (cached CPU transcription — no GPU yet).
    segments, lang = await run_in_threadpool(_segments_from_request, req)

    # GPU GATE (no-collision contract). Two layers, BOTH must pass:
    #  1. Broker policy — defers on a leased FLUX render / gaming.
    #  2. Physical VRAM headroom — catches GPU users the broker can't see
    #     (a resident ComfyUI/FLUX model holding the card without a lease).
    allowed, reason = await run_in_threadpool(broker.gpu_gate)
    if allowed:
        vram_ok, vram_reason = await run_in_threadpool(gpu.vram_gate)
        if not vram_ok:
            allowed, reason = False, vram_reason
    if not allowed:
        logger.info("summary deferred — %s", reason)
        return {"ok": False, "deferred": True, "reason": reason,
                "segment_count": len(segments), "language": lang}

    logger.info("summarize segments=%d (%s)", len(segments), reason)
    try:
        out = await run_in_threadpool(summarize.summarize, segments, detected_language=lang)
    except Exception as e:  # noqa: BLE001
        msg = str(e)
        # A model-load failure (llama-swap can't fit Qwen — usually a GPU the
        # broker can't see) is a DEFERRAL, not a server error: nothing crashed,
        # the load just couldn't proceed. Surface it as deferred so callers retry.
        if "health check timed out" in msg or "loading model" in msg.lower():
            logger.info("summary deferred — Qwen load failed (gpu busy): %s", msg)
            return {"ok": False, "deferred": True,
                    "reason": "qwen could not load (gpu busy / insufficient VRAM)",
                    "detail": msg, "segment_count": len(segments), "language": lang}
        logger.exception("summarize failed")
        raise HTTPException(502, f"summary failed: {e}")
    if cache_key and not out.get("empty"):
        await run_in_threadpool(store.put_summary, cache_key, mtime, size, out)
    return {"ok": True, "gpu_gate": reason, "segment_count": len(segments), **out}


@app.post("/transcribe")
async def transcribe(req: TranscribeRequest):
    real = _resolve_media_path(req.path)
    model_size = (req.model or config.MODEL).strip()
    logger.info("transcribe path=%s model=%s fresh=%s", req.path, model_size, req.fresh)
    try:
        out = await run_in_threadpool(_transcribe_cached, real, req.path,
                                      model_size, req.language, req.fresh)
    except Exception as e:  # noqa: BLE001
        logger.exception("transcribe failed")
        raise HTTPException(500, f"transcription failed: {e}")
    return {"ok": True, "path": req.path, "segment_count": len(out.get("segments", [])), **out}


@app.get("/transcript")
async def transcript_endpoint(path: str, fresh: bool = False):
    """Full transcript for a media path (cached; transcribes on a miss). Powers the
    transcript viewer."""
    real = _resolve_media_path(path)
    try:
        out = await run_in_threadpool(_transcribe_cached, real, path, config.MODEL, None, fresh)
    except Exception as e:  # noqa: BLE001
        logger.exception("transcript failed")
        raise HTTPException(500, f"transcript failed: {e}")
    return {"ok": True, "path": path, "segment_count": len(out.get("segments", [])), **out}


class StatusRequest(BaseModel):
    paths: list[str] = Field(default_factory=list, description="media paths to report status for")


@app.post("/status")
async def status_endpoint(req: StatusRequest):
    """Per-path indexed/transcribed/summarized flags for UI badges."""
    out = await run_in_threadpool(store.media_status, req.paths)
    return {"ok": True, "status": out}


class IndexRequest(BaseModel):
    path: str = Field(..., description="media path to transcribe + index (under downloads root)")
    model: str | None = Field(None, description="whisper model size; default from env")
    language: str | None = Field(None, description="force transcription language")


class SearchRequest(BaseModel):
    query: str = Field(..., description="natural-language search query")
    k: int = Field(10, ge=1, le=100, description="number of hits to return")
    path: str | None = Field(None, description="restrict the search to one media path")


@app.post("/index")
async def index_endpoint(req: IndexRequest):
    """Transcribe (CPU) → embed (CPU) → store. No GPU touched, so this is safe to
    run across the library while FLUX/Qwen/a game hold the card."""
    real = _resolve_media_path(req.path)

    def _run() -> dict:
        tx = _transcribe_cached(real, req.path, (req.model or config.MODEL).strip(), req.language)
        segs = tx.get("segments", [])
        lang = tx.get("language")
        if not segs:
            store.index_segments(req.path, [], [], lang)
            return {"indexed": 0, "language": lang, "empty": True, "cached": tx.get("cached")}
        seg_objs = [Segment(s["start"], s["end"], s["text"]) for s in segs]
        vectors = embed.embed_passages([s["text"] for s in segs])
        n = store.index_segments(req.path, seg_objs, vectors, lang)
        return {"indexed": n, "language": lang, "duration": tx.get("duration"),
                "cached": tx.get("cached")}

    try:
        out = await run_in_threadpool(_run)
    except Exception as e:  # noqa: BLE001
        logger.exception("index failed")
        raise HTTPException(500, f"index failed: {e}")
    logger.info("indexed path=%s segments=%s", req.path, out.get("indexed"))
    return {"ok": True, "path": req.path, **out}


@app.post("/search")
async def search_endpoint(req: SearchRequest):
    """Semantic search across indexed transcripts → timestamped hits (CPU)."""
    def _run() -> list[dict]:
        qv = embed.embed_query(req.query)
        return store.search(qv, k=req.k, media_path=req.path)

    try:
        hits = await run_in_threadpool(_run)
    except Exception as e:  # noqa: BLE001
        logger.exception("search failed")
        raise HTTPException(500, f"search failed: {e}")
    return {"ok": True, "query": req.query, "count": len(hits), "hits": hits}


@app.on_event("startup")
async def _startup():
    # Initialise the search store (idempotent).
    try:
        store.init()
    except Exception as e:  # noqa: BLE001
        logger.warning("store init skipped: %s", e)
    # Preload the default model so the first /transcribe isn't penalised by the
    # ~1-2 s model load. Best-effort: a download hiccup must not crash the boot.
    try:
        await run_in_threadpool(engine.warmup)
        logger.info("warmup complete (model=%s)", config.MODEL)
    except Exception as e:  # noqa: BLE001
        logger.warning("warmup skipped: %s", e)
