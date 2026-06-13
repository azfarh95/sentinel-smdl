"""Owner-only per-platform cookie management API (Mini App + bot share this).

Lets the owner refresh yt-dlp auth cookies from a phone instead of editing
`/cookies/<site>.txt` on the host. See cookies_admin.py for the file mechanics.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, File, HTTPException, Request, UploadFile
from pydantic import BaseModel

from . import cookies_admin as _ck
from . import miniapp as _mini   # reuse _verify + _require_owner

logger = logging.getLogger(__name__)

router = APIRouter()


class CookiePasteBody(BaseModel):
    text: str


@router.get("/api/cookies/status")
async def cookies_status(request: Request) -> dict:
    payload = await _mini._verify(request)
    _mini._require_owner(payload)
    return {"ok": True, "platforms": _ck.status_all()}


@router.post("/api/cookies/{platform}")
async def cookies_save(platform: str, body: CookiePasteBody, request: Request) -> dict:
    payload = await _mini._verify(request)
    _mini._require_owner(payload)
    try:
        st = _ck.save(platform, body.text or "")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    logger.info("cookies updated (paste) platform=%s count=%s", platform, st.get("count"))
    return {"ok": True, "status": st}


@router.post("/api/cookies/{platform}/upload")
async def cookies_upload(platform: str, request: Request,
                         file: UploadFile = File(...)) -> dict:
    payload = await _mini._verify(request)
    _mini._require_owner(payload)
    raw = await file.read()
    if len(raw) > 2 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="file too large")
    try:
        st = _ck.save(platform, raw.decode("utf-8", errors="replace"))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    logger.info("cookies updated (upload) platform=%s count=%s", platform, st.get("count"))
    return {"ok": True, "status": st}


@router.post("/api/cookies/{platform}/delete")
async def cookies_delete(platform: str, request: Request) -> dict:
    payload = await _mini._verify(request)
    _mini._require_owner(payload)
    try:
        _ck.delete(platform)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True}
