"""Owner-only proxy API to the media-ai sidecar (transcription / summary / search).

Thin, degrade-dark bridge: every route 503s when the sidecar isn't configured
(``SMDL_MEDIA_AI_URL`` unset), and is owner-gated via the Mini App identity
(same pattern as cookie_routes). The blocking sidecar calls run in a thread so
the event loop isn't held.

This is the Phase-4 BACKEND seam — no Mini App UI yet. Auto-transcribe on
download (via the async job queue, ADR MED-004) and a Library transcript/search
panel build on top of these.
"""
from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from . import media_ai
from . import miniapp as _mini   # reuse _verify + _require_owner

logger = logging.getLogger(__name__)

router = APIRouter()


def _require_enabled():
    if not media_ai.enabled():
        raise HTTPException(status_code=503, detail="media-ai sidecar not configured")


async def _owner(request: Request):
    payload = await _mini._verify(request)
    _mini._require_owner(payload)


class PathBody(BaseModel):
    path: str = Field(..., description="media path under the downloads root")
    model: str | None = None
    language: str | None = None


class SummarizeBody(BaseModel):
    path: str | None = None
    transcript: str | None = None


class SearchBody(BaseModel):
    query: str
    k: int = Field(10, ge=1, le=100)
    path: str | None = None


@router.get("/api/media-ai/status")
async def media_ai_status(request: Request) -> dict:
    await _owner(request)
    # status() is safe when disabled (returns {enabled:False}); no 503 here so the
    # UI can show a "not configured" state.
    return await asyncio.to_thread(media_ai.status)


@router.post("/api/media-ai/transcribe")
async def media_ai_transcribe(body: PathBody, request: Request) -> dict:
    await _owner(request)
    _require_enabled()
    try:
        return await asyncio.to_thread(media_ai.transcribe, body.path, body.model, body.language)
    except media_ai.MediaAIError as e:
        raise HTTPException(status_code=502, detail=str(e))


@router.post("/api/media-ai/index")
async def media_ai_index(body: PathBody, request: Request) -> dict:
    await _owner(request)
    _require_enabled()
    try:
        return await asyncio.to_thread(media_ai.index, body.path, body.model)
    except media_ai.MediaAIError as e:
        raise HTTPException(status_code=502, detail=str(e))


@router.post("/api/media-ai/summarize")
async def media_ai_summarize(body: SummarizeBody, request: Request) -> dict:
    await _owner(request)
    _require_enabled()
    if not (body.path or body.transcript):
        raise HTTPException(status_code=400, detail="provide path or transcript")
    try:
        return await asyncio.to_thread(media_ai.summarize, path=body.path, transcript=body.transcript)
    except media_ai.MediaAIError as e:
        raise HTTPException(status_code=502, detail=str(e))


@router.post("/api/media-ai/search")
async def media_ai_search(body: SearchBody, request: Request) -> dict:
    await _owner(request)
    _require_enabled()
    try:
        return await asyncio.to_thread(media_ai.search, body.query, body.k, body.path)
    except media_ai.MediaAIError as e:
        raise HTTPException(status_code=502, detail=str(e))
