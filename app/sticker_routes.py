"""Sticker maker — FastAPI routes + inline HTML editor.

Mounted alongside miniapp.py in main.py. All routes go through `_verify()`
from miniapp so initData + APK-cookie auth share the same gate.

Paths
-----
HTML
    GET  /stickers                 — drafts list + pack info
    GET  /stickers/{id}/edit       — editor page

JSON
    GET  /api/sticker_drafts       — drafts as JSON (drives the list page)
    POST /api/sticker_drafts/{id}/make
    POST /api/sticker_drafts/{id}/delete
    POST /api/sticker_drafts/delete_all

Static
    GET  /api/sticker_drafts/{id}/preview  — serve the source video so the
                                              MiniApp can show it
"""

from __future__ import annotations

import asyncio
import logging
import os
import uuid
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel
from telegram.error import TelegramError

from . import database as _db
from . import sticker_processor as _sp
from . import sticker_telegram as _st
from . import miniapp as _mini   # reuse _verify, _is_owner

logger = logging.getLogger(__name__)

router = APIRouter()

DOWNLOADS_DIR = os.environ.get("DOWNLOADS_DIR", "/downloads")
DRAFTS_DIR    = Path(os.environ.get("STICKER_DRAFTS_DIR",
                                     "/data/sticker_drafts"))
OUTPUT_DIR    = Path(os.environ.get("STICKER_OUTPUT_DIR",
                                     "/data/stickers"))


def draft_path(user_id: int, draft_id: int, ext: str) -> Path:
    """Canonical filesystem path for a draft file. Stored per-user to make
    /delete_data and TTL sweeps simple."""
    ext = (ext or ".bin").lstrip(".")
    return DRAFTS_DIR / str(user_id) / f"{draft_id}.{ext}"


def output_path(user_id: int, draft_id: int) -> Path:
    return OUTPUT_DIR / str(user_id) / f"{draft_id}.webm"


# ── Request models ──────────────────────────────────────────────────────────


class MakeStickerBody(BaseModel):
    emoji: str
    # Crop is in source-pixel coordinates; the frontend computes them from
    # the video's intrinsic width/height + the overlay position.
    crop_x: int | None = None
    crop_y: int | None = None
    crop_w: int | None = None
    crop_h: int | None = None
    # Trim in seconds. Frontend clamps to [0, duration] before posting.
    trim_start: float = 0.0
    trim_end:   float = 3.0


class RenamePackBody(BaseModel):
    title: str


class StickerByIdBody(BaseModel):
    file_id: str


class StickerPositionBody(BaseModel):
    file_id: str
    position: int


class StickerEmojisBody(BaseModel):
    file_id: str
    emojis: list[str]


class StickerKeywordsBody(BaseModel):
    file_id: str
    keywords: list[str]


class DeletePackBody(BaseModel):
    # Required-true confirm so an accidental empty POST can't nuke a pack.
    confirm: bool = False


# Web-upload limits. Mirrors what Telegram itself accepts at the bot path
# (50 MB inline upload ceiling) and a small mime allowlist so we don't end
# up storing arbitrary files in DRAFTS_DIR.
_UPLOAD_MAX_BYTES   = 50 * 1024 * 1024
_UPLOAD_MIME_PREFIX = ("video/",)
_UPLOAD_MIME_EXACT  = frozenset({"image/gif"})
# Sentinel for direct (browser) uploads — `sticker_drafts.telegram_file_id`
# is NOT NULL in the schema but isn't used downstream by `make_sticker`
# (the make-flow keys off `file_path`). Use a recognisable sentinel so
# anyone grepping the DB can tell where the row came from.
_WEB_UPLOAD_SENTINEL = "web_upload"
# File-extension hints for a few known mime types so the on-disk filename
# carries the right suffix (ffprobe + ffmpeg use the extension as a hint).
_MIME_EXT = {
    "video/mp4":         ".mp4",
    "video/quicktime":   ".mov",
    "video/webm":        ".webm",
    "video/x-matroska":  ".mkv",
    "video/mpeg":        ".mpeg",
    "image/gif":         ".gif",
}


# ── JSON routes ─────────────────────────────────────────────────────────────


@router.get("/api/sticker_drafts")
async def list_drafts(request: Request):
    payload = await _mini._verify(request)
    _mini.require_scope(payload, "smdl.stickers")
    user_id = int(payload["user"]["id"])
    drafts = await _db.sticker_draft_list(user_id)
    # Strip server-only fields
    out = [
        {
            "id":          d["id"],
            "mime_type":   d.get("mime_type"),
            "duration_s":  d.get("duration_s"),
            "width":       d.get("width"),
            "height":      d.get("height"),
            "uploaded_at": d.get("uploaded_at"),
            "expires_at":  d.get("expires_at"),
            "status":      d.get("status"),
            "error":       d.get("error"),
        }
        for d in drafts
    ]
    pack = await _db.sticker_pack_get(user_id)
    return {
        "drafts": out,
        "pack":   pack,
    }


@router.get("/api/sticker_drafts/{draft_id}/preview")
async def preview_draft(draft_id: int, request: Request):
    payload = await _mini._verify(request)
    _mini.require_scope(payload, "smdl.stickers")
    user_id = int(payload["user"]["id"])
    d = await _db.sticker_draft_get(draft_id, user_id)
    if not d:
        raise HTTPException(status_code=404, detail="draft not found")
    p = Path(d["file_path"])
    if not p.exists():
        raise HTTPException(status_code=410, detail="draft file gone")
    mime = d.get("mime_type") or "video/mp4"
    return FileResponse(str(p), media_type=mime, filename=p.name)


@router.post("/api/sticker_drafts/{draft_id}/make")
async def make_sticker(draft_id: int, body: MakeStickerBody, request: Request):
    payload = await _mini._verify(request)
    _mini.require_scope(payload, "smdl.stickers")
    user_id = int(payload["user"]["id"])
    first_name = (payload.get("user") or {}).get("first_name")

    d = await _db.sticker_draft_get(draft_id, user_id)
    if not d:
        raise HTTPException(status_code=404, detail="draft not found")
    src = Path(d["file_path"])
    if not src.exists():
        raise HTTPException(status_code=410, detail="source file missing on disk")

    # Validate the crop box if all four were sent. The frontend always sends
    # them together; missing → no crop, encode whole frame.
    crop = None
    if (body.crop_x is not None and body.crop_y is not None
            and body.crop_w is not None and body.crop_h is not None):
        crop = (int(body.crop_x), int(body.crop_y),
                int(body.crop_w), int(body.crop_h))

    await _db.sticker_draft_set_status(draft_id, "processing")
    dst = output_path(user_id, draft_id)

    ok, err = await _sp.make_video_sticker(
        src, dst,
        start=float(body.trim_start or 0.0),
        end=float(body.trim_end or 3.0),
        crop=crop,
    )
    if not ok:
        await _db.sticker_draft_set_status(draft_id, "failed", err)
        raise HTTPException(status_code=422,
                             detail=err or "ffmpeg failed")

    # Resolve pack
    from .bot import get_application
    tg_app = get_application()
    if tg_app is None:
        await _db.sticker_draft_set_status(draft_id, "failed", "bot not running")
        raise HTTPException(status_code=503, detail="bot not running")

    pack = await _st.resolve_pack(tg_app.bot, user_id, first_name)
    try:
        file_id, set_url = await _st.upload_and_add(
            tg_app.bot, user_id, dst,
            emoji=(body.emoji or "🎬"),
            pack_name=pack["pack_name"],
            pack_title=pack["pack_title"],
        )
    except TelegramError as e:
        msg = str(e)
        logger.warning("Telegram sticker API failed for u=%s d=%s: %s",
                       user_id, draft_id, msg)
        await _db.sticker_draft_set_status(draft_id, "failed", msg)
        # PEER_ID_INVALID = user never DM'd the bot — surface that clearly.
        if "PEER_ID_INVALID" in msg or "user not found" in msg.lower():
            raise HTTPException(
                status_code=400,
                detail="Send /start to the bot first, then try again.",
            )
        raise HTTPException(status_code=502, detail=f"Telegram: {msg}")

    # Persist pack + sticker rows
    if not pack.get("exists_in_db"):
        await _db.sticker_pack_create(
            user_id, pack["pack_name"], pack["pack_title"], set_url,
        )
    await _db.sticker_record(
        user_id=user_id,
        pack_name=pack["pack_name"],
        source_draft_id=draft_id,
        emoji=body.emoji or "🎬",
        telegram_file_id=file_id,
        webm_path=str(dst),
    )
    # Reset to 'awaiting_edit' so the user can keep making variants from the
    # same draft (different emoji, different trim, etc.). The draft will
    # auto-purge on its 6h TTL — no need to lock it as 'done'.
    await _db.sticker_draft_set_status(draft_id, "awaiting_edit")

    # Confirmation sticker back to the user's DM
    try:
        await tg_app.bot.send_sticker(chat_id=user_id, sticker=file_id)
        await tg_app.bot.send_message(
            chat_id=user_id,
            text=f"✅ Added to your pack:\n{set_url}",
            disable_web_page_preview=True,
        )
    except Exception as e:
        logger.warning("confirmation DM failed for u=%s: %s", user_id, e)

    return {
        "ok":               True,
        "sticker_file_id":  file_id,
        "set_url":          set_url,
        "pack_name":        pack["pack_name"],
    }


@router.post("/api/sticker_drafts/{draft_id}/delete")
async def delete_one(draft_id: int, request: Request):
    payload = await _mini._verify(request)
    _mini.require_scope(payload, "smdl.stickers")
    user_id = int(payload["user"]["id"])
    path_str = await _db.sticker_draft_delete(draft_id, user_id)
    if path_str is None:
        raise HTTPException(status_code=404, detail="draft not found")
    try:
        Path(path_str).unlink(missing_ok=True)
    except Exception:
        pass
    return {"ok": True}


@router.post("/api/sticker_drafts/delete_all")
async def delete_all(request: Request):
    payload = await _mini._verify(request)
    _mini.require_scope(payload, "smdl.stickers")
    user_id = int(payload["user"]["id"])
    paths = await _db.sticker_drafts_delete_all(user_id)
    for p in paths:
        try:
            Path(p).unlink(missing_ok=True)
        except Exception:
            pass
    # Sweep any encoded outputs sitting in OUTPUT_DIR/<uid> too — they
    # came from this user's drafts so the "delete my data" promise covers
    # them. Keep `stickers` rows (already added to a Telegram pack — they're
    # the audit log of what was made, not user-private data).
    udir = OUTPUT_DIR / str(user_id)
    if udir.exists():
        for f in udir.iterdir():
            try: f.unlink(missing_ok=True)
            except Exception: pass
    return {"ok": True, "deleted": len(paths)}


# ── Direct (browser) upload — alternative to the bot DM path ───────────────


@router.post("/api/sticker_drafts")
async def upload_draft(request: Request,
                       file: UploadFile = File(...)) -> dict:
    """Accept a video / GIF uploaded directly from the Mini App and produce
    a draft row equivalent to one the bot would have created from a DM.

    The bot path stores `telegram_file_id` from PTB's upload result; web
    uploads have no such id, so we tag those rows with `_WEB_UPLOAD_SENTINEL`
    in that NOT NULL column. The make-sticker flow only reads `file_path`,
    so the sentinel never reaches the encode/upload pipeline.
    """
    payload = await _mini._verify(request)
    _mini.require_scope(payload, "smdl.stickers")
    user_id = int(payload["user"]["id"])

    mime = (file.content_type or "").lower()
    if not (mime.startswith(_UPLOAD_MIME_PREFIX) or mime in _UPLOAD_MIME_EXACT):
        raise HTTPException(415, f"unsupported media type: {mime or 'unknown'}")

    # Stream to disk under a temporary basename so the file lands somewhere
    # before we have a draft id. Once the row is created we rename it into
    # the canonical `<draft_id>.<ext>` slot that `draft_path()` predicts.
    ext = _MIME_EXT.get(mime, ".bin")
    user_dir = DRAFTS_DIR / str(user_id)
    user_dir.mkdir(parents=True, exist_ok=True)
    tmp_path = user_dir / f"_upload_{uuid.uuid4().hex}{ext}"
    written = 0
    try:
        with open(tmp_path, "wb") as out:
            while True:
                chunk = await file.read(64 * 1024)
                if not chunk:
                    break
                written += len(chunk)
                if written > _UPLOAD_MAX_BYTES:
                    raise HTTPException(413, "file too large (max 50 MB)")
                out.write(chunk)
    except HTTPException:
        try: tmp_path.unlink(missing_ok=True)
        except Exception: pass
        raise
    except Exception as e:
        logger.warning("web-upload write failed for u=%s: %s", user_id, e)
        try: tmp_path.unlink(missing_ok=True)
        except Exception: pass
        raise HTTPException(500, "upload write failed")

    if written == 0:
        try: tmp_path.unlink(missing_ok=True)
        except Exception: pass
        raise HTTPException(400, "empty upload")

    # Probe metadata. Best-effort — the editor falls back to client-side
    # decoding for duration/dimensions, so we don't fail upload on a
    # ffprobe miss.
    meta = await _sp.probe(tmp_path)

    # Insert the draft row first so we know the canonical id, THEN rename
    # the file into the `<id>.<ext>` slot the rest of the code expects.
    draft_id = await _db.sticker_draft_insert(
        user_id=user_id,
        telegram_file_id=_WEB_UPLOAD_SENTINEL,
        file_path="",
        mime_type=mime,
        duration_s=meta.get("duration_s"),
        width=meta.get("width"),
        height=meta.get("height"),
    )
    final_path = draft_path(user_id, draft_id, ext)
    try:
        tmp_path.rename(final_path)
    except Exception as e:
        logger.warning("draft rename failed u=%s d=%s: %s", user_id, draft_id, e)
        # The row exists but its file_path is empty — the housekeeping
        # purge will sweep it on its 6h TTL. Surface the failure so the
        # caller can retry rather than appearing-to-succeed silently.
        raise HTTPException(500, "draft persist failed")
    # Patch the row's file_path now that we have it. There's no dedicated
    # setter, so go through aiosqlite directly — single column, single row.
    import aiosqlite
    async with aiosqlite.connect(_db.DB_PATH) as dbh:
        await dbh.execute(
            "UPDATE sticker_drafts SET file_path = ? WHERE id = ?",
            (str(final_path), draft_id),
        )
        await dbh.commit()

    logger.info("web-upload OK u=%s d=%s mime=%s bytes=%d",
                user_id, draft_id, mime, written)
    return {
        "id":          draft_id,
        "mime_type":   mime,
        "duration_s":  meta.get("duration_s"),
        "width":       meta.get("width"),
        "height":      meta.get("height"),
        "bytes":       written,
    }


# ── Pack rename (Telegram setStickerSetTitle) ──────────────────────────────


@router.post("/api/sticker_pack/rename")
async def rename_pack(body: RenamePackBody, request: Request) -> dict:
    """Rename the caller's sticker pack on Telegram AND in the local DB.

    Telegram caps `set_sticker_set_title` at 64 chars; reject longer input
    locally so we don't burn a Telegram API call to learn that."""
    payload = await _mini._verify(request)
    _mini.require_scope(payload, "smdl.stickers")
    user_id = int(payload["user"]["id"])

    new_title = (body.title or "").strip()
    if not new_title:
        raise HTTPException(400, "title is required")
    if len(new_title) > 64:
        raise HTTPException(400, "title is over 64 chars (Telegram limit)")

    pack = await _db.sticker_pack_get(user_id)
    if not pack:
        raise HTTPException(404, "no sticker pack to rename (make one sticker first)")

    from .bot import get_application
    tg_app = get_application()
    if tg_app is None:
        raise HTTPException(503, "bot not running")

    try:
        await tg_app.bot.set_sticker_set_title(
            name=pack["pack_name"], title=new_title,
        )
    except TelegramError as e:
        msg = str(e)
        logger.warning("set_sticker_set_title failed u=%s: %s", user_id, msg)
        raise HTTPException(502, f"Telegram: {msg}")

    # Mirror into the DB so subsequent /api/sticker_drafts responses
    # reflect the new title without depending on a Telegram round-trip.
    await _db.sticker_pack_create(
        user_id=user_id,
        pack_name=pack["pack_name"],
        pack_title=new_title,
        telegram_url=pack.get("telegram_url") or f"https://t.me/addstickers/{pack['pack_name']}",
    )
    return {"ok": True, "title": new_title}


# ── Pack contents (Telegram getStickerSet + per-sticker mutations) ─────────


# Disk-backed proxy cache for sticker files served via /api/sticker_pack/
# sticker_file/{file_id}. Telegram file_ids are stable per bot, so caching
# by file_id never goes stale; we only TTL to keep the cache from growing
# unboundedly. The cache lives under STICKER_OUTPUT_DIR/_proxy so it shares
# the same volume that's already mounted on the container.
_PROXY_DIR = OUTPUT_DIR / "_proxy"
_PROXY_TTL_SEC = 7 * 24 * 3600
_PROXY_MAX_FILES = 500   # rough cap; LRU-ish eviction on overflow


def _proxy_cache_path(file_id: str, mime_hint: str | None) -> Path:
    """Map a Telegram file_id to a deterministic cache path. File_ids are
    long opaque base64-ish strings; we hash to keep filesystem entries short
    and avoid edge cases on case-insensitive filesystems."""
    import hashlib
    h = hashlib.sha256(file_id.encode("ascii", errors="ignore")).hexdigest()[:24]
    ext = ".webm" if (mime_hint or "").endswith("webm") else ".bin"
    return _PROXY_DIR / f"{h}{ext}"


async def _proxy_evict_if_needed() -> None:
    """Simple cap-by-count eviction. Drops oldest mtime files until under cap."""
    try:
        if not _PROXY_DIR.exists():
            return
        files = sorted(_PROXY_DIR.iterdir(), key=lambda p: p.stat().st_mtime)
        excess = len(files) - _PROXY_MAX_FILES
        for f in files[:max(0, excess)]:
            try: f.unlink(missing_ok=True)
            except Exception: pass
    except Exception:
        pass


def _pack_format_for(pack) -> str:
    """Decide the format string for set_sticker_set_thumbnail.

    PTB exposes a StickerSet object with a `sticker_type` ('regular' /
    'custom_emoji') and per-sticker `is_video` / `is_animated` flags. The
    thumbnail API needs us to pick 'static' / 'animated' / 'video'."""
    if not pack or not getattr(pack, "stickers", None):
        return "video"
    first = pack.stickers[0]
    if getattr(first, "is_video", False):  return "video"
    if getattr(first, "is_animated", False): return "animated"
    return "static"


def _serialise_sticker(s) -> dict:
    """Shrink a PTB Sticker object to the minimal dict the frontend needs."""
    thumb = getattr(s, "thumbnail", None)
    return {
        "file_id":      s.file_id,
        "file_unique_id": getattr(s, "file_unique_id", None),
        "emoji":        getattr(s, "emoji", None) or "",
        "is_video":     bool(getattr(s, "is_video", False)),
        "is_animated":  bool(getattr(s, "is_animated", False)),
        "width":        getattr(s, "width", None),
        "height":       getattr(s, "height", None),
        "thumb_file_id": (thumb.file_id if thumb else None),
    }


async def _resolve_pack_or_404(user_id: int):
    """Look up the caller's pack in the local DB and refuse cleanly if
    they don't have one yet. Returns the local row."""
    pack = await _db.sticker_pack_get(user_id)
    if not pack:
        raise HTTPException(404, "no sticker pack yet (make one sticker first)")
    return pack


def _require_bot():
    """Return the running PTB Application or 503. All sticker-pack
    mutations bottom out in a Bot call."""
    from .bot import get_application
    tg_app = get_application()
    if tg_app is None:
        raise HTTPException(503, "bot not running")
    return tg_app


@router.get("/api/sticker_pack/contents")
async def pack_contents(request: Request) -> dict:
    """Return the live contents of the caller's sticker pack — Telegram is
    the SoT for which stickers actually exist in the set; the local
    `stickers` table is best-effort audit only.
    """
    payload = await _mini._verify(request)
    _mini.require_scope(payload, "smdl.stickers")
    user_id = int(payload["user"]["id"])
    pack = await _resolve_pack_or_404(user_id)
    tg_app = _require_bot()
    try:
        tg_set = await tg_app.bot.get_sticker_set(name=pack["pack_name"])
    except TelegramError as e:
        # Common case: user deleted the pack via BotFather / Telegram client;
        # surface clearly so the UI can offer to re-create.
        raise HTTPException(404, f"Telegram pack lookup failed: {e}")
    return {
        "name":         tg_set.name,
        "title":        tg_set.title,
        "sticker_type": getattr(tg_set, "sticker_type", "regular"),
        "stickers":     [_serialise_sticker(s) for s in (tg_set.stickers or [])],
    }


@router.get("/api/sticker_pack/sticker_file/{file_id}")
async def pack_sticker_file(file_id: str, request: Request):
    """Proxy a Telegram file_id → raw bytes. Required because Telegram's
    file_path links are bot-token-scoped (can't be embedded in the WebApp),
    AND the WebView needs same-origin URLs to play <video>/show <img>.

    Caches to disk for `_PROXY_TTL_SEC` so a busy pack-view doesn't
    hammer Telegram's file servers."""
    payload = await _mini._verify(request)
    _mini.require_scope(payload, "smdl.stickers")
    # We don't authorise per-file_id (any pack member is fetchable by anyone
    # with the file_id), but the auth gate restricts the surface to signed-in
    # community users. File_ids are not enumerable.
    tg_app = _require_bot()
    _PROXY_DIR.mkdir(parents=True, exist_ok=True)
    # Try cache first.
    for ext_hint in (".webm", ".bin"):
        cached = _proxy_cache_path(file_id, "video/webm" if ext_hint == ".webm" else None)
        if cached.exists():
            try:
                # Refresh mtime so frequently-used files survive eviction.
                cached.touch()
                ct = "video/webm" if cached.suffix == ".webm" else "application/octet-stream"
                return FileResponse(str(cached), media_type=ct)
            except Exception:
                pass
    try:
        f = await tg_app.bot.get_file(file_id)
        data = bytes(await f.download_as_bytearray())
    except TelegramError as e:
        raise HTTPException(502, f"Telegram file fetch failed: {e}")
    # Guess mime from the first bytes — webm/webp are the realistic
    # sticker formats; default to octet-stream as a fallback.
    mime = "application/octet-stream"
    if data[:4] == b"\x1aE\xdf\xa3":  # EBML / Matroska / WebM magic
        mime, ext = "video/webm", ".webm"
    elif data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        mime, ext = "image/webp", ".webp"
    elif data[:6] in (b"GIF87a", b"GIF89a"):
        mime, ext = "image/gif", ".gif"
    elif data[:8] == b"\x89PNG\r\n\x1a\n":
        mime, ext = "image/png", ".png"
    else:
        ext = ".bin"
    cached = _proxy_cache_path(file_id, mime)
    # Rewrite suffix so the cache extension matches the detected mime.
    cached = cached.with_suffix(ext)
    try:
        cached.write_bytes(data)
        await _proxy_evict_if_needed()
    except Exception as e:
        logger.debug("proxy cache write failed for %s: %s", file_id[:12], e)
    return FileResponse(str(cached), media_type=mime) if cached.exists() else \
        JSONResponse({"detail": "proxy cache miss"}, status_code=500)


@router.post("/api/sticker_pack/sticker/delete")
async def pack_sticker_delete(body: StickerByIdBody, request: Request) -> dict:
    payload = await _mini._verify(request)
    _mini.require_scope(payload, "smdl.stickers")
    user_id = int(payload["user"]["id"])
    await _resolve_pack_or_404(user_id)
    tg_app = _require_bot()
    try:
        await tg_app.bot.delete_sticker_from_set(sticker=body.file_id)
    except TelegramError as e:
        raise HTTPException(502, f"Telegram: {e}")
    return {"ok": True}


@router.post("/api/sticker_pack/sticker/position")
async def pack_sticker_position(body: StickerPositionBody, request: Request) -> dict:
    payload = await _mini._verify(request)
    _mini.require_scope(payload, "smdl.stickers")
    user_id = int(payload["user"]["id"])
    await _resolve_pack_or_404(user_id)
    tg_app = _require_bot()
    try:
        await tg_app.bot.set_sticker_position_in_set(
            sticker=body.file_id, position=int(body.position),
        )
    except TelegramError as e:
        raise HTTPException(502, f"Telegram: {e}")
    return {"ok": True}


@router.post("/api/sticker_pack/sticker/emojis")
async def pack_sticker_emojis(body: StickerEmojisBody, request: Request) -> dict:
    payload = await _mini._verify(request)
    _mini.require_scope(payload, "smdl.stickers")
    user_id = int(payload["user"]["id"])
    await _resolve_pack_or_404(user_id)
    emojis = [str(e).strip() for e in (body.emojis or []) if str(e).strip()]
    if not emojis:
        raise HTTPException(400, "at least one emoji required")
    if len(emojis) > 20:
        raise HTTPException(400, "max 20 emojis per sticker")
    tg_app = _require_bot()
    try:
        await tg_app.bot.set_sticker_emoji_list(
            sticker=body.file_id, emoji_list=emojis,
        )
    except TelegramError as e:
        raise HTTPException(502, f"Telegram: {e}")
    return {"ok": True, "emojis": emojis}


@router.post("/api/sticker_pack/sticker/keywords")
async def pack_sticker_keywords(body: StickerKeywordsBody, request: Request) -> dict:
    payload = await _mini._verify(request)
    _mini.require_scope(payload, "smdl.stickers")
    user_id = int(payload["user"]["id"])
    await _resolve_pack_or_404(user_id)
    # Keywords cap: Telegram allows up to 20 keywords, joined length <= 64 chars.
    kws = [str(k).strip() for k in (body.keywords or []) if str(k).strip()]
    if len(kws) > 20:
        raise HTTPException(400, "max 20 keywords per sticker")
    if sum(len(k) for k in kws) > 64:
        raise HTTPException(400, "total keyword length must be ≤ 64 chars")
    tg_app = _require_bot()
    try:
        await tg_app.bot.set_sticker_keywords(
            sticker=body.file_id, keywords=kws,
        )
    except TelegramError as e:
        raise HTTPException(502, f"Telegram: {e}")
    return {"ok": True, "keywords": kws}


@router.post("/api/sticker_pack/sticker/set_cover")
async def pack_sticker_set_cover(body: StickerByIdBody, request: Request) -> dict:
    """Use this sticker's file_id as the pack thumbnail. Telegram requires
    the thumbnail format to match the pack's sticker type."""
    payload = await _mini._verify(request)
    _mini.require_scope(payload, "smdl.stickers")
    user_id = int(payload["user"]["id"])
    pack = await _resolve_pack_or_404(user_id)
    tg_app = _require_bot()
    # Look up current pack so we can derive the right `format` arg.
    try:
        tg_set = await tg_app.bot.get_sticker_set(name=pack["pack_name"])
    except TelegramError as e:
        raise HTTPException(404, f"Telegram pack lookup failed: {e}")
    fmt = _pack_format_for(tg_set)
    sticker_type = getattr(tg_set, "sticker_type", "regular")
    try:
        if sticker_type == "custom_emoji":
            # Custom-emoji packs have a different thumbnail setter; takes the
            # custom_emoji_id of the sticker (which IS its file_unique_id for
            # PTB's purposes — but TG's API expects the custom_emoji_id, which
            # is the sticker's file_id for custom emoji).
            await tg_app.bot.set_custom_emoji_sticker_set_thumbnail(
                name=pack["pack_name"], custom_emoji_id=body.file_id,
            )
        else:
            await tg_app.bot.set_sticker_set_thumbnail(
                name=pack["pack_name"], user_id=user_id,
                thumbnail=body.file_id, format=fmt,
            )
    except TelegramError as e:
        raise HTTPException(502, f"Telegram: {e}")
    return {"ok": True, "format": fmt}


@router.post("/api/sticker_pack/delete")
async def pack_delete(body: DeletePackBody, request: Request) -> dict:
    """Nuke the entire pack on Telegram + drop the local rows. Irreversible —
    the caller MUST send `{confirm: true}`."""
    payload = await _mini._verify(request)
    _mini.require_scope(payload, "smdl.stickers")
    user_id = int(payload["user"]["id"])
    if not body.confirm:
        raise HTTPException(400, "destructive — pass {\"confirm\": true}")
    pack = await _resolve_pack_or_404(user_id)
    tg_app = _require_bot()
    try:
        await tg_app.bot.delete_sticker_set(name=pack["pack_name"])
    except TelegramError as e:
        # If the pack is already gone on TG, that's effectively success —
        # let the local cleanup proceed.
        msg = str(e).lower()
        if "not found" not in msg and "stickerset_invalid" not in msg:
            raise HTTPException(502, f"Telegram: {e}")
    # Clear local rows so the next /make starts a fresh pack with the same
    # canonical name.
    import aiosqlite
    async with aiosqlite.connect(_db.DB_PATH) as dbh:
        await dbh.execute("DELETE FROM stickers WHERE user_id = ?", (user_id,))
        await dbh.execute("DELETE FROM sticker_packs WHERE user_id = ?", (user_id,))
        await dbh.commit()
    return {"ok": True}


# ── HTML pages ──────────────────────────────────────────────────────────────


_LIST_HTML = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>SMDL Sticker Maker</title>
  <script src="https://telegram.org/js/telegram-web-app.js"></script>
  <style>
    :root { color-scheme: dark light; }
    body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
         margin:0;padding:16px;background:var(--tg-theme-bg-color,#111);
         color:var(--tg-theme-text-color,#eee);}
    h1{font-size:18px;margin:0 0 12px;}
    .card{background:var(--tg-theme-secondary-bg-color,#222);
          border-radius:12px;padding:12px;margin-bottom:10px;}
    .draft{display:flex;gap:10px;align-items:center;}
    .draft video{width:80px;height:80px;object-fit:cover;background:#000;border-radius:8px;}
    .meta{flex:1;font-size:13px;}
    .meta .row{color:var(--tg-theme-hint-color,#999);font-size:11px;margin-top:2px;}
    button{font:inherit;border:0;padding:8px 12px;border-radius:8px;
           background:var(--tg-theme-button-color,#3390ec);
           color:var(--tg-theme-button-text-color,#fff);cursor:pointer;}
    button.ghost{background:transparent;color:var(--tg-theme-link-color,#3390ec);
                 border:1px solid currentColor;}
    button.danger{background:#a23;color:#fff;}
    .actions{display:flex;gap:6px;flex-wrap:wrap;}
    .empty{padding:24px;text-align:center;color:var(--tg-theme-hint-color,#888);}
    .pack a{color:var(--tg-theme-link-color,#5ac8fa);word-break:break-all;}
    .badge{display:inline-block;padding:1px 6px;border-radius:6px;font-size:10px;
           background:#333;color:#bbb;margin-left:4px;}
    .badge.processing{background:#553;color:#ffd;}
    .badge.failed{background:#5a2c2c;color:#fbb;}
    .badge.done{background:#284;color:#dfd;}
  </style>
</head>
<body>
  <h1>🎬 Sticker Maker</h1>

  <div class="card pack" id="pack-card">Loading…</div>

  <div id="drafts"></div>

  <div style="margin-top:20px;text-align:center;">
    <button class="danger" id="delete-all-btn">🗑 Delete all my data</button>
  </div>

<script>
const tg = window.Telegram?.WebApp;
if (tg) { tg.ready(); tg.expand(); }

const initData = tg?.initData || '';

async function api(path, opts = {}) {
  opts.headers = Object.assign({}, opts.headers || {}, {
    'X-Init-Data': initData,
    ...(opts.body ? {'Content-Type': 'application/json'} : {}),
  });
  const r = await fetch(path, opts);
  if (!r.ok) {
    const text = await r.text();
    let detail = text;
    try { detail = JSON.parse(text).detail || detail; } catch (e) {}
    throw new Error(`${r.status}: ${detail}`);
  }
  return await r.json();
}

function fmtSec(n) {
  if (!n) return '—';
  return Number(n).toFixed(1) + 's';
}

function fmtExpires(iso) {
  if (!iso) return '';
  const d = new Date(iso); const now = new Date();
  const mins = Math.max(0, Math.round((d - now) / 60000));
  if (mins < 60) return `expires in ${mins}m`;
  const hrs = Math.floor(mins / 60); const rest = mins % 60;
  return `expires in ${hrs}h${rest}m`;
}

async function refresh() {
  let data;
  try {
    data = await api('/api/sticker_drafts');
  } catch (e) {
    document.getElementById('pack-card').textContent = 'Error: ' + e.message;
    return;
  }
  const packEl = document.getElementById('pack-card');
  if (data.pack && data.pack.telegram_url) {
    packEl.innerHTML = `📦 <strong>${escapeHtml(data.pack.pack_title)}</strong><br>
      <a href="#" id="pack-link">${escapeHtml(data.pack.telegram_url)}</a>`;
    const pl = document.getElementById('pack-link');
    pl.addEventListener('click', ev => {
      ev.preventDefault();
      if (tg && tg.openTelegramLink) tg.openTelegramLink(data.pack.telegram_url);
      else window.open(data.pack.telegram_url, '_blank');
    });
  } else {
    packEl.textContent = 'No sticker pack yet — turn your first video into a sticker to start one.';
  }

  const root = document.getElementById('drafts');
  root.innerHTML = '';
  if (!data.drafts.length) {
    root.innerHTML = '<div class="empty">Send a video or GIF to the bot to get started.</div>';
    return;
  }
  for (const d of data.drafts) {
    const div = document.createElement('div');
    div.className = 'card draft';
    const statusBadge = d.status && d.status !== 'awaiting_edit'
      ? `<span class="badge ${d.status}">${d.status}</span>` : '';
    div.innerHTML = `
      <video data-draft-id="${d.id}" muted playsinline></video>
      <div class="meta">
        <div>${fmtSec(d.duration_s)} · ${(d.width||'?')}×${(d.height||'?')} ${statusBadge}</div>
        <div class="row">${escapeHtml(fmtExpires(d.expires_at))}</div>
        ${d.error ? `<div class="row" style="color:#f88">${escapeHtml(d.error)}</div>` : ''}
        <div class="actions" style="margin-top:6px">
          <button onclick="location.href='/stickers/${d.id}/edit'">Make sticker</button>
          <button class="ghost" onclick="del(${d.id})">Delete</button>
        </div>
      </div>`;
    root.appendChild(div);
    // Telegram WebView cookie isolation: fetch preview manually with the
    // initData header, then hand a blob URL to the <video> element.
    const videoEl = div.querySelector('video');
    fetch(`/api/sticker_drafts/${d.id}/preview`, {
      headers: {'X-Init-Data': initData},
    }).then(r => r.ok ? r.blob() : null)
      .then(b => { if (b) videoEl.src = URL.createObjectURL(b); })
      .catch(() => {});
  }
}

async function del(id) {
  if (!confirm('Delete this draft?')) return;
  try {
    await api(`/api/sticker_drafts/${id}/delete`, {method: 'POST', body: '{}'});
    refresh();
  } catch (e) { alert(e.message); }
}

document.getElementById('delete-all-btn').addEventListener('click', async () => {
  if (!confirm('Wipe ALL your sticker drafts + intermediate files?\\n(Already-published stickers in your pack are unaffected.)')) return;
  try {
    const r = await api('/api/sticker_drafts/delete_all', {method: 'POST', body: '{}'});
    alert(`Deleted ${r.deleted} drafts.`);
    refresh();
  } catch (e) { alert(e.message); }
});

function escapeHtml(s) { return String(s).replace(/[&<>"']/g, c =>
  ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
function escapeAttr(s) { return escapeHtml(s).replace(/"/g, '&quot;'); }

refresh();
</script>
</body></html>
"""


_EDIT_HTML = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Make Sticker</title>
  <script src="https://telegram.org/js/telegram-web-app.js"></script>
  <style>
    :root { color-scheme: dark light; }
    body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
         margin:0;padding:12px;background:var(--tg-theme-bg-color,#111);
         color:var(--tg-theme-text-color,#eee);}
    h1{font-size:16px;margin:0 0 10px;}
    .video-wrap{position:relative;display:inline-block;max-width:100%;
                background:#000;border-radius:8px;overflow:hidden;}
    .video-wrap video{display:block;width:100%;max-height:50vh;}
    .section{margin-top:14px;}
    .section label{display:block;font-size:12px;
                    color:var(--tg-theme-hint-color,#999);margin-bottom:4px;}
    input[type=range]{width:100%;}
    .emojirow{display:flex;gap:6px;flex-wrap:wrap;}
    .emojirow button{font-size:22px;padding:6px 10px;background:#222;border:1px solid #444;
                     border-radius:8px;color:#fff;cursor:pointer;}
    .emojirow button.active{background:#3390ec;border-color:#3390ec;}
    input[type=text]{padding:6px 8px;border-radius:6px;border:1px solid #555;
                     background:#181818;color:#fff;font-size:18px;width:80px;}
    #make-btn{margin-top:16px;font-size:16px;padding:12px 20px;width:100%;
              background:var(--tg-theme-button-color,#3390ec);color:#fff;border:0;
              border-radius:10px;cursor:pointer;}
    #make-btn:disabled{opacity:.6;cursor:wait;}
    #progress{margin-top:10px;font-size:13px;color:var(--tg-theme-hint-color,#999);}
    .trim{display:flex;gap:8px;align-items:center;font-size:12px;}
    .trim-times{font-variant-numeric:tabular-nums;color:var(--tg-theme-hint-color,#999);}
    .back{display:inline-block;margin-bottom:8px;color:#5ac8fa;cursor:pointer;}
  </style>
</head>
<body>
  <div class="back" onclick="location.href='/app?tab=stickers'">← Back to drafts</div>
  <h1>Make sticker</h1>

  <div class="video-wrap">
    <video id="vid" controls muted playsinline></video>
  </div>
  <div style="font-size:11px;color:var(--tg-theme-hint-color,#888);margin-top:6px;">
    The center of the video fills the 512×512 sticker frame (sides are cropped).
  </div>

  <div class="section">
    <label>Trim (≤ 3 seconds)</label>
    <div class="trim">
      <span>Start</span>
      <input type="range" id="t-start" min="0" max="100" step="0.05" value="0">
      <span class="trim-times" id="t-start-l">0.0s</span>
    </div>
    <div class="trim">
      <span>End</span>
      <input type="range" id="t-end" min="0" max="100" step="0.05" value="3">
      <span class="trim-times" id="t-end-l">3.0s</span>
    </div>
  </div>

  <div class="section">
    <label>Emoji (tap one or paste your own)</label>
    <div class="emojirow" id="emoji-grid">
      <button data-emoji="🎬">🎬</button>
      <button data-emoji="😂">😂</button>
      <button data-emoji="🔥">🔥</button>
      <button data-emoji="💯">💯</button>
      <button data-emoji="🎉">🎉</button>
      <button data-emoji="✨">✨</button>
      <button data-emoji="😎">😎</button>
      <button data-emoji="❤️">❤️</button>
      <input id="emoji-custom" type="text" maxlength="4" placeholder="🎯">
    </div>
  </div>

  <button id="make-btn">✨ Make sticker</button>
  <div id="progress"></div>

<script>
const tg = window.Telegram?.WebApp;
if (tg) { tg.ready(); tg.expand(); }
const initData = tg?.initData || '';

const DRAFT_ID = {{DRAFT_ID}};

async function api(path, opts = {}) {
  opts.headers = Object.assign({}, opts.headers || {}, {
    'X-Init-Data': initData,
    ...(opts.body ? {'Content-Type': 'application/json'} : {}),
  });
  const r = await fetch(path, opts);
  if (!r.ok) {
    const text = await r.text();
    let detail = text;
    try { detail = JSON.parse(text).detail || detail; } catch (e) {}
    throw new Error(`${r.status}: ${detail}`);
  }
  return await r.json();
}

const vid = document.getElementById('vid');
const tStart = document.getElementById('t-start');
const tEnd   = document.getElementById('t-end');
const tStartL = document.getElementById('t-start-l');
const tEndL   = document.getElementById('t-end-l');
const emojiGrid = document.getElementById('emoji-grid');
const emojiCustom = document.getElementById('emoji-custom');
const makeBtn = document.getElementById('make-btn');
const progressEl = document.getElementById('progress');

let chosenEmoji = '🎬';
emojiGrid.querySelector('button[data-emoji="🎬"]').classList.add('active');

emojiGrid.addEventListener('click', e => {
  const btn = e.target.closest('button[data-emoji]');
  if (!btn) return;
  chosenEmoji = btn.dataset.emoji;
  emojiCustom.value = '';
  emojiGrid.querySelectorAll('button').forEach(b => b.classList.toggle('active', b === btn));
});
emojiCustom.addEventListener('input', () => {
  const v = emojiCustom.value.trim();
  if (v) {
    chosenEmoji = v;
    emojiGrid.querySelectorAll('button').forEach(b => b.classList.remove('active'));
  }
});

// Telegram WebView doesn't share cookies with the system browser, so the
// preview endpoint requires X-Init-Data — but a plain <video src> request
// can't set custom headers. Fetch the bytes ourselves and feed them to the
// player as a blob URL.
(async () => {
  try {
    const r = await fetch(`/api/sticker_drafts/${DRAFT_ID}/preview`, {
      headers: {'X-Init-Data': initData},
    });
    if (!r.ok) {
      progressEl.textContent = `❌ Preview load failed: ${r.status} ${r.statusText}`;
      return;
    }
    const blob = await r.blob();
    vid.src = URL.createObjectURL(blob);
  } catch (e) {
    progressEl.textContent = '❌ Preview load failed: ' + e.message;
  }
})();

vid.addEventListener('loadedmetadata', () => {
  const dur = vid.duration || 3;
  tStart.max = dur.toFixed(2);
  tEnd.max   = dur.toFixed(2);
  tEnd.value = Math.min(dur, 3).toFixed(2);
  updateTrimLabels();
});

function updateTrimLabels() {
  let s = +tStart.value, e = +tEnd.value;
  if (e - s > 3) { e = s + 3; tEnd.value = e.toFixed(2); }
  if (s > e)     { s = e; tStart.value = s.toFixed(2); }
  tStartL.textContent = s.toFixed(1) + 's';
  tEndL.textContent   = e.toFixed(1) + 's';
  vid.currentTime = s;
}
tStart.addEventListener('input', updateTrimLabels);
tEnd.addEventListener('input', updateTrimLabels);

makeBtn.addEventListener('click', async () => {
  makeBtn.disabled = true;
  progressEl.textContent = 'Encoding sticker… (5–20s)';
  const body = {
    emoji:      chosenEmoji,
    trim_start: +tStart.value,
    trim_end:   +tEnd.value,
  };
  try {
    const r = await api(`/api/sticker_drafts/${DRAFT_ID}/make`, {
      method: 'POST', body: JSON.stringify(body),
    });
    // Use the Telegram WebApp SDK to open t.me/addstickers/... in the
    // native sticker viewer; a plain <a href> would try to render the
    // URL inside this WebView, where Telegram just shows the cover and
    // an inert "+" button.
    progressEl.innerHTML = `✅ Added! <a href="#" id="open-pack-link" style="color:#5ac8fa">Open your pack</a><br><span style="font-size:12px">You can make another variant — different emoji / trim — or go back.</span>`;
    const link = document.getElementById('open-pack-link');
    link.addEventListener('click', ev => {
      ev.preventDefault();
      if (tg && tg.openTelegramLink) tg.openTelegramLink(r.set_url);
      else window.open(r.set_url, '_blank');
    });
    if (tg && tg.HapticFeedback) tg.HapticFeedback.notificationOccurred('success');
    makeBtn.disabled = false;
    makeBtn.textContent = '✨ Make another';
  } catch (e) {
    progressEl.textContent = '❌ ' + e.message;
    makeBtn.disabled = false;
  }
});
</script>
</body></html>
"""


# ── Background TTL cleanup ──────────────────────────────────────────────────

STICKER_CLEANUP_INTERVAL_SECONDS = 30 * 60   # 30 min


async def cleanup_once() -> int:
    """Delete expired drafts + their on-disk files. Returns count purged.
    Stickers already added to a Telegram pack stay — only drafts have TTL."""
    expired = await _db.sticker_drafts_expired()
    if not expired:
        return 0
    ids: list[int] = []
    for d in expired:
        ids.append(int(d["id"]))
        p = d.get("file_path")
        if p:
            try:
                Path(p).unlink(missing_ok=True)
            except Exception as e:
                logger.warning("sticker cleanup unlink failed for %s: %s", p, e)
    await _db.sticker_drafts_purge(ids)
    logger.info("sticker cleanup: purged %d expired draft(s)", len(ids))
    return len(ids)


async def start_cleanup_loop() -> None:
    """Long-lived background task — sweeps expired drafts on a fixed interval."""
    logger.info("sticker cleanup loop started "
                "(every %d s)", STICKER_CLEANUP_INTERVAL_SECONDS)
    while True:
        try:
            await cleanup_once()
        except Exception as e:
            logger.exception("sticker cleanup iteration failed: %s", e)
        await asyncio.sleep(STICKER_CLEANUP_INTERVAL_SECONDS)


@router.get("/stickers")
async def stickers_page():
    """Standalone drafts list folded into /app?tab=stickers 2026-06-01.

    The list now lives as a tab in the unified SMDL Mini App; this URL
    stays as a 302 so existing inline buttons (the bot's "Open sticker
    editor", any historical menu entry, old shared links) continue to
    land in the right place. _LIST_HTML is retained in the module so the
    standalone surface can be brought back trivially if needed."""
    return RedirectResponse(url="/app?tab=stickers", status_code=302)


@router.get("/stickers/{draft_id}/edit", response_class=HTMLResponse)
async def edit_page(draft_id: int):
    # No initData check here — the page itself loads the draft via the
    # JSON API which IS auth-guarded. Mirrors how /app works in miniapp.py.
    html = _EDIT_HTML.replace("{{DRAFT_ID}}", str(int(draft_id)))
    return HTMLResponse(html)
