"""Client for the media-ai sidecar (transcription / summary / search).

DEGRADE-DARK: every function is a no-op-ish guard unless ``SMDL_MEDIA_AI_URL`` is
set. The OSS/community SMDL image has NO AI dependency and never assumes a sidecar
— the feature simply stays hidden (``enabled()`` is False) when the env var is
absent. The Sentinel deployment points this at ``http://media-ai:8097`` (the
sidecar on the compose network).

These calls are BLOCKING (urllib) and some are slow — transcription scales with
clip length; a summary can ride a ~3 min Qwen cold load. Call them from a thread
(``asyncio.to_thread`` / ``run_in_threadpool``) so the event loop isn't blocked,
and prefer the background job queue (ADR MED-004) for auto-transcribe in Phase 4
so a user never waits on a cold load.
"""
from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request

logger = logging.getLogger(__name__)

BASE_URL = os.environ.get("SMDL_MEDIA_AI_URL", "").rstrip("/")
# Per-call timeouts (s). Transcription/index scale with length; summary can ride a
# cold 27B load (the sidecar's own QWEN_TIMEOUT is 300).
TIMEOUT_FAST = float(os.environ.get("SMDL_MEDIA_AI_TIMEOUT", "20"))        # status/search
TIMEOUT_TRANSCRIBE = float(os.environ.get("SMDL_MEDIA_AI_TRANSCRIBE_TIMEOUT", "1800"))
TIMEOUT_SUMMARIZE = float(os.environ.get("SMDL_MEDIA_AI_SUMMARIZE_TIMEOUT", "360"))


def enabled() -> bool:
    """True iff a media-ai sidecar URL is configured."""
    return bool(BASE_URL)


class MediaAIError(RuntimeError):
    pass


def _get(path: str, timeout: float):
    req = urllib.request.Request(f"{BASE_URL}{path}", method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def _post(path: str, body: dict, timeout: float):
    if not BASE_URL:
        raise MediaAIError("media-ai not configured (SMDL_MEDIA_AI_URL unset)")
    data = json.dumps(body).encode()
    req = urllib.request.Request(f"{BASE_URL}{path}", data=data,
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", "replace")[:300]
        except Exception:  # noqa: BLE001
            pass
        raise MediaAIError(f"media-ai HTTP {e.code}: {detail or e.reason}") from None


# ── thin proxies to the sidecar (all BLOCKING — call from a thread) ───────────
def status() -> dict:
    if not BASE_URL:
        return {"enabled": False}
    try:
        return {"enabled": True, **_get("/healthz", TIMEOUT_FAST)}
    except Exception as e:  # noqa: BLE001
        logger.warning("media-ai status failed: %s", e)
        return {"enabled": True, "ok": False, "error": str(e)}


def transcribe(path: str, model: str | None = None, language: str | None = None) -> dict:
    return _post("/transcribe", {"path": path, "model": model, "language": language},
                 TIMEOUT_TRANSCRIBE)


def summarize(*, path: str | None = None, transcript: str | None = None,
              segments: list | None = None) -> dict:
    return _post("/summarize", {"path": path, "transcript": transcript, "segments": segments},
                 TIMEOUT_SUMMARIZE)


def index(path: str, model: str | None = None) -> dict:
    return _post("/index", {"path": path, "model": model}, TIMEOUT_TRANSCRIBE)


def search(query: str, k: int = 10, path: str | None = None) -> dict:
    return _post("/search", {"query": query, "k": k, "path": path}, TIMEOUT_FAST)


def status_paths(paths: list) -> dict:
    return _post("/status", {"paths": paths}, TIMEOUT_FAST)


def transcript(path: str) -> dict:
    import urllib.parse
    return _get("/transcript?path=" + urllib.parse.quote(path), TIMEOUT_TRANSCRIBE)
