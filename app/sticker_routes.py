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
import json
import logging
import os
import uuid
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel
from telegram.error import TelegramError

from . import database as _db
from . import sticker_processor as _sp
from . import sticker_telegram as _st
from . import sticker_jobs as _sj
from .sticker_jobs import StickerBuildError
from . import miniapp as _mini   # reuse _verify, _is_owner

logger = logging.getLogger(__name__)

router = APIRouter()

DOWNLOADS_DIR = os.environ.get("DOWNLOADS_DIR", "/downloads")
DRAFTS_DIR    = Path(os.environ.get("STICKER_DRAFTS_DIR",
                                     "/data/sticker_drafts"))
OUTPUT_DIR    = Path(os.environ.get("STICKER_OUTPUT_DIR",
                                     "/data/stickers"))

# ── GIF library sources (Giphy / Tenor) → sticker ──────────────────────────
# Keys come from the environment (WCM-sourced via .env.local). BOTH need a real
# key — Giphy's old public beta key (dc6zaTOxFJmzC) is now BANNED (403), so we
# no longer ship it; search stays dark until a key is set. Both are free:
# developers.giphy.com (instant) and Google Cloud → Tenor API. The keyless
# from_url importer still works for pasted media links (SSRF-guarded below).
GIPHY_API_KEY = os.environ.get("GIPHY_API_KEY", "").strip()
TENOR_API_KEY = os.environ.get("TENOR_API_KEY", "").strip()
_GIF_FETCH_HOSTS = (".giphy.com", ".tenor.com", ".tenorapi.com", ".googleapis.com")


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
    # Which pack this lands in. Each kind is a separate TG sticker pack:
    #   video        — webm 512×512 ≤3s   (default)
    #   static       — webp 512×512 still
    #   custom_emoji — 100×100 webm or webp, lands in the user's emoji pack
    pack_kind: str = "video"
    # Crop is in source-pixel coordinates; the frontend computes them from
    # the video's intrinsic width/height + the overlay position.
    crop_x: int | None = None
    crop_y: int | None = None
    crop_w: int | None = None
    crop_h: int | None = None
    # Trim in seconds. Frontend clamps to [0, duration] before posting.
    trim_start: float = 0.0
    trim_end:   float = 3.0
    # Shape & cutout.
    #   shape:  None/'square' = full frame; circle|triangle|diamond|heart|star|custom
    #   points: normalised [[x,y],…] polygon (0..1, output-square space) for 'custom'
    #   cutout: rembg background removal → transparent subject (STATIC only — webp
    #           carries alpha; webm here can't, see feedback_smdl_sticker_alpha).
    # Shapes work for STATIC (true transparent corners) AND VIDEO (opaque: the
    # area outside the shape is filled per `fill`, since webm has no alpha).
    #   fill:   'blur' (blurred copy of the video) or a hex colour — VIDEO only.
    shape:  str | None = None
    points: list | None = None
    cutout: bool = False
    fill:   str | None = None
    # Inset the shape so it floats free of the frame edges (a breathing margin)
    # instead of being inscribed edge-to-edge. Applies to static + video shapes.
    padding: bool = False
    # v2.7-C: "best quality" matte — heavier rembg model + temporal de-flicker.
    # Only meaningful when cutout is on; ignored otherwise. Runs as a B job.
    hq: bool = False


def _coerce_points(raw) -> list[tuple[float, float]] | None:
    """Normalise the custom-shape polygon from the editor into clamped
    (x, y) tuples in 0..1. Accepts [[x,y],…] or [{x,y},…]. Returns None
    unless at least 3 valid vertices survive."""
    if not isinstance(raw, list):
        return None
    pts: list[tuple[float, float]] = []
    for p in raw:
        if isinstance(p, (list, tuple)) and len(p) >= 2:
            x, y = p[0], p[1]
        elif isinstance(p, dict):
            x, y = p.get("x"), p.get("y")
        else:
            continue
        try:
            x = float(x); y = float(y)
        except (TypeError, ValueError):
            continue
        pts.append((min(1.0, max(0.0, x)), min(1.0, max(0.0, y))))
    return pts if len(pts) >= 3 else None


_SHAPE_NONE = {"", "square", "none", "rect", "rectangle"}
# Breathing margin applied when the per-sticker "padding" toggle is on.
_SHAPE_PAD = 0.07


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


class CloneStickerBody(BaseModel):
    # The Telegram file_id of the sticker we're copying. The bot fetches
    # the bytes via get_file → downloads → re-uploads as a NEW sticker in
    # the caller's pack-of-`target_kind`. The original pack is untouched.
    source_file_id: str
    # video / static / custom_emoji
    target_kind: str = "video"
    # Emoji to assign to the cloned sticker. Multi-emoji can be set later
    # via /api/sticker_pack/sticker/emojis on the copy.
    emoji: str = "🎬"


class ClonePackBody(BaseModel):
    # URL or name of the source pack. Same shapes accepted as the lookup
    # endpoint (t.me/addstickers/<name>, tg://addstickers?set=<name>, or
    # bare <name>).
    source: str
    # video / static / custom_emoji
    target_kind: str = "video"
    # Hard cap on stickers cloned per call. Telegram's per-set ceiling is
    # 120; we default lower to keep the request inside reasonable time
    # budgets (CF tunnel + proxy timeouts). Caller can retry for more.
    limit: int = 50


class FromDownloadBody(BaseModel):
    # Absolute path or a path under DOWNLOADS_DIR — the upload-from-download
    # flow trusts paths that come from the user's own download history (they
    # listed and saw the path). We resolve and re-check that they live under
    # DOWNLOADS_DIR before opening anything, so a manipulated payload can't
    # exfiltrate arbitrary files.
    file_path: str


class FromUrlBody(BaseModel):
    # A Giphy/Tenor media URL the user picked from the in-app GIF search. The
    # importer re-checks the host against _GIF_FETCH_HOSTS before fetching, so
    # a hand-crafted payload can't turn this into an SSRF.
    url: str


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


def _kind_param(raw: str | None) -> str:
    """Validate + normalise the `kind` query/body parameter."""
    k = (raw or "video").strip().lower()
    return k if k in ("video", "static", "custom_emoji") else "video"


@router.get("/api/sticker_drafts")
async def list_drafts(request: Request, kind: str | None = "video"):
    """List drafts + the pack info for the requested kind.

    `kind` defaults to 'video' for backward compat. The drafts table isn't
    kind-scoped — a draft is just a source clip; the user picks the
    destination pack on `/make`. So the kind param only affects which
    pack row is returned in `pack`.
    """
    payload = await _mini._verify(request)
    _mini.require_scope(payload, "smdl.stickers")
    user_id = int(payload["user"]["id"])
    drafts = await _db.sticker_draft_list(user_id)
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
    k = _kind_param(kind)
    pack = await _db.sticker_pack_get(user_id, k)
    return {
        "drafts":     out,
        "pack":       pack,
        "pack_kind":  k,
    }


@router.get("/api/sticker_packs")
async def list_packs(request: Request, kind: str | None = None) -> dict:
    """The user's packs (optionally one kind), active first. Drives the pack
    picker in the Mini App."""
    payload = await _mini._verify(request)
    _mini.require_scope(payload, "smdl.stickers")
    user_id = int(payload["user"]["id"])
    rows = await _db.sticker_packs_list(user_id, kind=kind)
    return {"packs": rows}


class CreatePackBody(BaseModel):
    title: str
    kind: str = "regular"


@router.post("/api/sticker_pack/create")
async def create_pack(body: CreatePackBody, request: Request) -> dict:
    """Reserve a NEW named pack and make it active. The Telegram set is created
    when the first sticker lands in it."""
    payload = await _mini._verify(request)
    _mini.require_scope(payload, "smdl.stickers")
    user_id = int(payload["user"]["id"])
    first_name = (payload.get("user") or {}).get("first_name")
    title = (body.title or "").strip()
    if not title:
        raise HTTPException(status_code=400, detail="Pack name required")
    from .bot import get_application
    tg_app = get_application()
    if tg_app is None:
        raise HTTPException(status_code=503, detail="bot not running")
    pack = await _st.create_named_pack(tg_app.bot, user_id, first_name, title, kind=body.kind)
    return {"ok": True, "pack": pack}


class ActivatePackBody(BaseModel):
    pack_name: str


@router.post("/api/sticker_pack/activate")
async def activate_pack(body: ActivatePackBody, request: Request) -> dict:
    """Switch which pack new stickers land in (the editor inherits this)."""
    payload = await _mini._verify(request)
    _mini.require_scope(payload, "smdl.stickers")
    user_id = int(payload["user"]["id"])
    ok = await _db.sticker_pack_set_active(user_id, (body.pack_name or "").strip())
    if not ok:
        raise HTTPException(status_code=404, detail="pack not found")
    return {"ok": True}


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


# ── Shared build/publish pipeline ────────────────────────────────────────────
# Used by BOTH the synchronous /make fast-path (cheap static) and the
# background job worker in sticker_jobs.py (heavy video). Keeping it here, next
# to the request model + processor helpers it already uses, means the worker
# only needs a lazy `import sticker_routes` to avoid an import cycle.


def _resolve_kind_ext(body: "MakeStickerBody", d: dict) -> tuple[str, str]:
    """Map the request + draft to (pack_kind, output extension). Different kinds
    produce different on-disk containers so re-making the same draft as
    video + static + emoji doesn't smash one over the other. custom_emoji rides
    webp for single-frame image sources, webm otherwise."""
    pack_kind = (body.pack_kind or "video").lower()
    if pack_kind not in ("video", "static", "custom_emoji"):
        pack_kind = "video"
    out_ext = {"video": "webm", "static": "webp", "custom_emoji": "webm"}[pack_kind]
    if pack_kind == "custom_emoji":
        d_mime = (d.get("mime_type") or "").lower()
        if d_mime.startswith("image/") and not d_mime.startswith("image/gif"):
            out_ext = "webp"
    return pack_kind, out_ext


def _is_heavy_encode(pack_kind: str, out_ext: str) -> bool:
    """Video-container encodes (cutout / transparent / plain video / animated
    custom-emoji) are the 5–30 s jobs that go async. Single-frame webp encodes
    stay on the synchronous fast-path."""
    return pack_kind == "video" or (pack_kind == "custom_emoji" and out_ext == "webm")


async def _publish_sticker(user_id: int, first_name: str | None, draft_id: int,
                           dst: Path, *, emoji: str, pack_kind: str,
                           sticker_format: str,
                           progress: "Callable[[float], None] | None" = None) -> dict:
    """Upload the freshly-encoded `dst` to the user's Telegram pack, persist the
    pack + sticker rows, reset the draft, and push a confirmation DM. Returns
    {sticker_file_id, set_url, pack_name}. Raises StickerBuildError on expected
    failure (bot down, Telegram API error) so both callers can surface it."""
    from .bot import get_application
    tg_app = get_application()
    if tg_app is None:
        await _db.sticker_draft_set_status(draft_id, "failed", "bot not running")
        raise StickerBuildError("bot not running", status=503)

    pack = await _st.resolve_pack(tg_app.bot, user_id, first_name, kind=pack_kind)
    try:
        file_id, set_url = await _st.upload_and_add(
            tg_app.bot, user_id, dst,
            emoji=(emoji or "🎬"),
            pack_name=pack["pack_name"],
            pack_title=pack["pack_title"],
            sticker_format=sticker_format,
            sticker_type=("custom_emoji" if pack_kind == "custom_emoji" else "regular"),
        )
    except TelegramError as e:
        msg = str(e)
        logger.warning("Telegram sticker API failed for u=%s d=%s: %s",
                       user_id, draft_id, msg)
        await _db.sticker_draft_set_status(draft_id, "failed", msg)
        # PEER_ID_INVALID = user never DM'd the bot — surface that clearly.
        if "PEER_ID_INVALID" in msg or "user not found" in msg.lower():
            raise StickerBuildError(
                "Send /start to the bot first, then try again.", status=400)
        raise StickerBuildError(f"Telegram: {msg}", status=502)

    if progress:
        progress(95)

    # Persist pack + sticker rows
    if not pack.get("exists_in_db"):
        await _db.sticker_pack_create(
            user_id, pack["pack_name"], pack["pack_title"], set_url, kind=pack_kind)
    await _db.sticker_record(
        user_id=user_id, pack_name=pack["pack_name"], source_draft_id=draft_id,
        emoji=emoji or "🎬", telegram_file_id=file_id, webm_path=str(dst),
        sticker_format=sticker_format)
    # Reset to 'awaiting_edit' so the user can keep making variants from the
    # same draft (different emoji / trim / crop). It auto-purges on its 6h TTL.
    await _db.sticker_draft_set_status(draft_id, "awaiting_edit")

    # Confirmation sticker + link back to the user's DM. This is the "push on
    # completion" that lets the user leave the Mini App during a long encode.
    try:
        await tg_app.bot.send_sticker(chat_id=user_id, sticker=file_id)
        await tg_app.bot.send_message(
            chat_id=user_id,
            text=f"✅ Added to your pack:\n{set_url}",
            disable_web_page_preview=True,
        )
    except Exception as e:
        logger.warning("confirmation DM failed for u=%s: %s", user_id, e)

    return {"sticker_file_id": file_id, "set_url": set_url,
            "pack_name": pack["pack_name"]}


async def build_and_publish(user_id: int, first_name: str | None, draft_id: int,
                            body: "MakeStickerBody", *,
                            progress: "Callable[[float], None] | None" = None) -> dict:
    """Full encode → Telegram-publish pipeline for one sticker. Shared by the
    synchronous /make fast-path (cheap static) and the background worker
    (heavy video). Re-fetches the draft so the worker survives a restart.
    `progress`, if given, is called with an absolute 0..100 percent. Raises
    StickerBuildError on expected failure (the worker records it on the job;
    the sync route maps it to an HTTPException)."""
    d = await _db.sticker_draft_get(draft_id, user_id)
    if not d:
        raise StickerBuildError("draft not found", status=404)
    src = Path(d["file_path"])
    if not src.exists():
        raise StickerBuildError("source file missing on disk", status=410)

    # Validate the crop box if all four were sent. The frontend always sends
    # them together; missing → no crop, encode whole frame.
    crop = None
    if (body.crop_x is not None and body.crop_y is not None
            and body.crop_w is not None and body.crop_h is not None):
        crop = (int(body.crop_x), int(body.crop_y),
                int(body.crop_w), int(body.crop_h))

    await _db.sticker_draft_set_status(draft_id, "processing")
    pack_kind, out_ext = _resolve_kind_ext(body, d)
    dst = OUTPUT_DIR / str(user_id) / f"{draft_id}.{out_ext}"
    if progress:
        progress(10)

    if pack_kind == "video":
        # Shaped video. Two roads for the area OUTSIDE the shape:
        #   fill='transparent' → a REAL alpha channel (VP9 + BlockAdditional via
        #                        webm_alpha) — genuinely see-through corners.
        #   fill='blur'/'#hex' → opaque fill (webm-without-alpha), the corners
        #                        are a blurred copy of the video or a colour.
        v_shape = (body.shape or "").strip().lower()
        v_fill = (body.fill or "blur").strip().lower()
        v_mask: Path | None = None
        v_tmp: list[Path] = []
        if v_shape and v_shape not in _SHAPE_NONE:
            pts = None
            if v_shape == "custom":
                pts = _coerce_points(body.points)
                if not pts:
                    await _db.sticker_draft_set_status(
                        draft_id, "failed", "custom shape needs >=3 points")
                    raise StickerBuildError(
                        "custom shape needs at least 3 points", status=422)
            mask_png = dst.with_name(f"{draft_id}_vmask.png")
            mres = _sp.build_shape_mask(v_shape, mask_png, points=pts,
                                        pad=(_SHAPE_PAD if body.padding else 0.0))
            if mres is None:
                await _db.sticker_draft_set_status(
                    draft_id, "failed", f"unsupported shape: {v_shape}")
                raise StickerBuildError(f"unsupported shape: {v_shape}", status=422)
            v_mask = mask_png
            v_tmp.append(mask_png)
        v_want_cutout = bool(body.cutout)
        try:
            if v_want_cutout:
                # Per-frame background removal → transparent animated subject
                # (optionally intersected with the chosen shape). The slowest
                # path (~6-20 s); reports fine-grained progress (10→75%).
                ok, err = await _sp.make_video_cutout_sticker(
                    src, dst,
                    start=float(body.trim_start or 0.0),
                    end=float(body.trim_end or 3.0),
                    crop=crop,
                    shape_mask=v_mask,
                    hq=bool(body.hq),
                    progress_cb=((lambda f: progress(10.0 + f * 65.0))
                                 if progress else None),
                )
            elif v_mask is not None and v_fill == "transparent":
                if progress:
                    progress(40)
                ok, err = await _sp.make_transparent_video_sticker(
                    src, v_mask, dst,
                    start=float(body.trim_start or 0.0),
                    end=float(body.trim_end or 3.0),
                    crop=crop,
                )
            else:
                if progress:
                    progress(40)
                ok, err = await _sp.make_video_sticker(
                    src, dst,
                    start=float(body.trim_start or 0.0),
                    end=float(body.trim_end or 3.0),
                    crop=crop,
                    shape_mask=v_mask,
                    fill=v_fill,
                )
        finally:
            for f in v_tmp:
                try:
                    f.unlink(missing_ok=True)
                except Exception:
                    pass
        sticker_format = "video"
    elif pack_kind == "static":
        # Shape mask + background cutout are static-only (webp alpha). Build a
        # mask and/or a transparent cutout source, then encode. Temp artefacts
        # are cleaned up regardless of outcome.
        shape = (body.shape or "").strip().lower()
        want_shape = shape not in _SHAPE_NONE
        want_cutout = bool(body.cutout)
        static_src = src
        static_seek = float(body.trim_start or 0.0)
        mask_path: Path | None = None
        tmp_files: list[Path] = []
        try:
            if want_cutout:
                # rembg needs a still — pull the chosen frame to PNG first
                # (handles both image and video sources), then segment it.
                frame_png = dst.with_name(f"{draft_id}_frame.png")
                fok, ferr = await _sp.extract_frame(src, frame_png, seek_s=static_seek)
                if not fok:
                    await _db.sticker_draft_set_status(draft_id, "failed", ferr)
                    raise StickerBuildError(ferr or "frame extract failed", status=422)
                tmp_files.append(frame_png)
                cut_png = dst.with_name(f"{draft_id}_cut.png")
                cok, cerr = await _sp.bg_remove(frame_png, cut_png, hq=bool(body.hq))
                if not cok:
                    await _db.sticker_draft_set_status(draft_id, "failed", cerr)
                    raise StickerBuildError(cerr or "background removal failed", status=422)
                tmp_files.append(cut_png)
                static_src = cut_png
                static_seek = 0.0   # cut_png already holds the chosen frame
            if want_shape:
                pts = None
                if shape == "custom":
                    pts = _coerce_points(body.points)
                    if not pts:
                        await _db.sticker_draft_set_status(
                            draft_id, "failed", "custom shape needs >=3 points")
                        raise StickerBuildError(
                            "custom shape needs at least 3 points", status=422)
                mask_png = dst.with_name(f"{draft_id}_mask.png")
                mres = _sp.build_shape_mask(shape, mask_png, points=pts,
                                            pad=(_SHAPE_PAD if body.padding else 0.0))
                if mres is None:
                    await _db.sticker_draft_set_status(
                        draft_id, "failed", f"unsupported shape: {shape}")
                    raise StickerBuildError(f"unsupported shape: {shape}", status=422)
                tmp_files.append(mask_png)
                mask_path = mask_png
            ok, err = await _sp.make_static_sticker(
                static_src, dst, crop=crop, seek_s=static_seek,
                mask=mask_path, keep_alpha=want_cutout,
            )
        finally:
            for f in tmp_files:
                try:
                    f.unlink(missing_ok=True)
                except Exception:
                    pass
        sticker_format = "static"
    else:  # custom_emoji
        is_video = (out_ext == "webm")
        ok, err = await _sp.make_custom_emoji_sticker(
            src, dst, is_video=is_video, crop=crop,
            start=float(body.trim_start or 0.0),
            end=float(body.trim_end or 3.0),
        )
        sticker_format = "video" if is_video else "static"
    if not ok:
        await _db.sticker_draft_set_status(draft_id, "failed", err)
        raise StickerBuildError(err or "ffmpeg failed", status=422)
    if progress:
        progress(78)

    return await _publish_sticker(
        user_id, first_name, draft_id, dst,
        emoji=body.emoji, pack_kind=pack_kind,
        sticker_format=sticker_format, progress=progress)


async def build_overlay_and_publish(user_id: int, first_name: str | None,
                                    draft_id: int, params: dict, *,
                                    progress: "Callable[[float], None] | None" = None) -> dict:
    """v2.7-A — composite the Studio overlay (saved PNG, path in params) onto
    every frame of the source video → animated webm, then publish. Runs on the
    background worker (heavy). `params`: overlay_path, emoji, trim_start/end,
    crop_x/y/w/h, cutout. Cleans up the overlay PNG afterward."""
    d = await _db.sticker_draft_get(draft_id, user_id)
    if not d:
        raise StickerBuildError("draft not found", status=404)
    src = Path(d["file_path"])
    if not src.exists():
        raise StickerBuildError("source file missing on disk", status=410)
    overlay_png = Path(params.get("overlay_path") or "")
    if not overlay_png.exists():
        raise StickerBuildError("overlay image missing on disk", status=410)

    crop = None
    cx, cy, cw, ch = (params.get("crop_x"), params.get("crop_y"),
                      params.get("crop_w"), params.get("crop_h"))
    if None not in (cx, cy, cw, ch):
        crop = (int(cx), int(cy), int(cw), int(ch))

    await _db.sticker_draft_set_status(draft_id, "processing")
    dst = OUTPUT_DIR / str(user_id) / f"{draft_id}.webm"
    if progress:
        progress(10)
    anim_overlays = params.get("anim_overlays") or []
    try:
        ok, err = await _sp.make_video_overlay_sticker(
            src, dst, overlay_png,
            start=float(params.get("trim_start") or 0.0),
            end=float(params.get("trim_end") or 3.0),
            crop=crop, cutout=bool(params.get("cutout")), hq=bool(params.get("hq")),
            anim_overlays=anim_overlays,
            progress_cb=((lambda f: progress(10.0 + f * 65.0)) if progress else None),
        )
    finally:
        try:
            overlay_png.unlink(missing_ok=True)
        except Exception:
            pass
        for ao in anim_overlays:
            try:
                Path(ao.get("path") or "").unlink(missing_ok=True)
            except Exception:
                pass
    if not ok:
        await _db.sticker_draft_set_status(draft_id, "failed", err)
        raise StickerBuildError(err or "overlay encode failed", status=422)
    if progress:
        progress(78)
    return await _publish_sticker(
        user_id, first_name, draft_id, dst,
        emoji=params.get("emoji") or "🎬", pack_kind="video",
        sticker_format="video", progress=progress)


@router.post("/api/sticker_drafts/{draft_id}/make")
async def make_sticker(draft_id: int, body: MakeStickerBody, request: Request):
    """Heavy video encodes are enqueued on the background worker (returns
    {ok, job_id, async}); cheap single-frame static encodes run inline on the
    fast-path (returns {ok, sticker_file_id, set_url, pack_name}). The editor
    polls /api/sticker_jobs/{id} for the async path."""
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

    pack_kind, out_ext = _resolve_kind_ext(body, d)
    if _is_heavy_encode(pack_kind, out_ext):
        # Label the job for the UI / logs.
        if pack_kind == "video" and body.cutout:
            kind = "video_cutout"
        elif (pack_kind == "video"
              and (body.shape or "").strip().lower() not in _SHAPE_NONE
              and (body.fill or "").strip().lower() == "transparent"):
            kind = "video_transparent"
        else:
            kind = pack_kind
        params = body.model_dump()
        params["_first_name"] = first_name
        job_id = await _sj.enqueue(user_id=user_id, draft_id=draft_id,
                                   kind=kind, params=params)
        return {"ok": True, "job_id": job_id, "async": True}

    # Cheap static / static custom-emoji — run inline on the fast-path.
    try:
        result = await build_and_publish(user_id, first_name, draft_id, body)
    except StickerBuildError as e:
        raise HTTPException(status_code=e.status, detail=e.detail)
    return {"ok": True, **result}


@router.get("/api/sticker_jobs/{job_id}")
async def sticker_job_status(job_id: int, request: Request):
    """Poll a background sticker-encode job. Returns status / progress (0..100)
    / error / result. Owner-scoped: a user only sees their own jobs."""
    payload = await _mini._verify(request)
    _mini.require_scope(payload, "smdl.stickers")
    user_id = int(payload["user"]["id"])
    job = await _sj.get_job(job_id)
    if not job or job.user_id != user_id:
        raise HTTPException(status_code=404, detail="job not found")
    result = None
    if job.result_json:
        try:
            result = json.loads(job.result_json)
        except Exception:
            result = None
    return {
        "ok": True,
        "job_id": job.id,
        "status": job.status,
        "progress": round(float(job.progress or 0.0), 1),
        "kind": job.kind,
        "error": job.error,
        "result": result,
    }


# ── v2.7-D library depth: search · tags · trash/restore · use · presets ───────

class _TagsBody(BaseModel):
    tags: str = ""


class _ReorderBody(BaseModel):
    pack_name: str
    ordered_ids: list[int] = []


class _PresetBody(BaseModel):
    name: str
    recipe: dict = {}


def _sticker_pub(r: dict) -> dict:
    return {
        "id": r["id"], "emoji": r.get("emoji"), "tags": r.get("tags") or "",
        "pack_name": r.get("pack_name"), "file_id": r.get("telegram_file_id"),
        "sticker_format": r.get("sticker_format"),
        "use_count": r.get("use_count") or 0,
        "deleted": bool(r.get("deleted_at")),
    }


@router.get("/api/stickers/search")
async def stickers_search(request: Request, q: str = "", include_deleted: bool = False):
    """Cross-pack search over the user's created stickers (emoji + tags + pack).
    Empty q → the whole live library, most-used first. (v2.7-D)"""
    payload = await _mini._verify(request)
    _mini.require_scope(payload, "smdl.stickers")
    user_id = int(payload["user"]["id"])
    rows = await _db.sticker_search(user_id, q, include_deleted=bool(include_deleted))
    return {"ok": True, "stickers": [_sticker_pub(r) for r in rows], "count": len(rows)}


@router.post("/api/stickers/{sticker_id}/tags")
async def stickers_set_tags(sticker_id: int, body: _TagsBody, request: Request):
    payload = await _mini._verify(request)
    _mini.require_scope(payload, "smdl.stickers")
    user_id = int(payload["user"]["id"])
    if not await _db.sticker_set_tags(sticker_id, user_id, body.tags or ""):
        raise HTTPException(status_code=404, detail="sticker not found")
    return {"ok": True}


@router.post("/api/stickers/{sticker_id}/trash")
async def stickers_trash(sticker_id: int, request: Request):
    """Soft-delete (recoverable) — hides from the library without removing the
    sticker from the Telegram pack. See /restore."""
    payload = await _mini._verify(request)
    _mini.require_scope(payload, "smdl.stickers")
    user_id = int(payload["user"]["id"])
    if not await _db.sticker_set_trashed(sticker_id, user_id, True):
        raise HTTPException(status_code=404, detail="sticker not found")
    return {"ok": True}


@router.post("/api/stickers/{sticker_id}/restore")
async def stickers_restore(sticker_id: int, request: Request):
    payload = await _mini._verify(request)
    _mini.require_scope(payload, "smdl.stickers")
    user_id = int(payload["user"]["id"])
    if not await _db.sticker_set_trashed(sticker_id, user_id, False):
        raise HTTPException(status_code=404, detail="sticker not found")
    return {"ok": True}


@router.post("/api/stickers/{sticker_id}/use")
async def stickers_use(sticker_id: int, request: Request):
    """Bump a sticker's use_count (drives most-used ranking in search)."""
    payload = await _mini._verify(request)
    _mini.require_scope(payload, "smdl.stickers")
    user_id = int(payload["user"]["id"])
    await _db.sticker_increment_use(sticker_id, user_id)
    return {"ok": True}


@router.post("/api/stickers/reorder")
async def stickers_reorder(body: _ReorderBody, request: Request):
    """Persist a new in-pack order (sort_order). Telegram-position sync + a
    drag UI are a follow-up; this stores the canonical order."""
    payload = await _mini._verify(request)
    _mini.require_scope(payload, "smdl.stickers")
    user_id = int(payload["user"]["id"])
    n = await _db.sticker_reorder(user_id, body.pack_name,
                                  [int(i) for i in (body.ordered_ids or [])])
    return {"ok": True, "updated": n}


# Editor recipes — save the current setup (shape/crop/emoji/fill/cutout/hq) and
# re-apply it one-tap to a future draft.
_PRESET_KEYS = {"shape", "points", "crop_x", "crop_y", "crop_w", "crop_h",
                "emoji", "fill", "padding", "cutout", "hq", "pack_kind",
                "trim_start", "trim_end"}


@router.get("/api/sticker_presets")
async def sticker_presets_list(request: Request):
    payload = await _mini._verify(request)
    _mini.require_scope(payload, "smdl.stickers")
    user_id = int(payload["user"]["id"])
    out = []
    for r in await _db.sticker_preset_list(user_id):
        try:
            recipe = json.loads(r["recipe_json"])
        except Exception:
            recipe = {}
        out.append({"id": r["id"], "name": r["name"], "recipe": recipe})
    return {"ok": True, "presets": out}


@router.post("/api/sticker_presets")
async def sticker_presets_create(body: _PresetBody, request: Request):
    payload = await _mini._verify(request)
    _mini.require_scope(payload, "smdl.stickers")
    user_id = int(payload["user"]["id"])
    name = (body.name or "").strip()[:60] or "Preset"
    recipe = {k: v for k, v in (body.recipe or {}).items() if k in _PRESET_KEYS}
    pid = await _db.sticker_preset_create(user_id, name, json.dumps(recipe))
    return {"ok": True, "id": pid}


@router.post("/api/sticker_presets/{preset_id}/delete")
async def sticker_presets_delete(preset_id: int, request: Request):
    payload = await _mini._verify(request)
    _mini.require_scope(payload, "smdl.stickers")
    user_id = int(payload["user"]["id"])
    if not await _db.sticker_preset_delete(preset_id, user_id):
        raise HTTPException(status_code=404, detail="preset not found")
    return {"ok": True}


class ComposeBody(BaseModel):
    # data-URL or raw base64 of the composited 512×512 PNG from the Studio canvas.
    png_b64: str
    emoji: str = "🎬"
    # Die-cut sticker outline (applied server-side on the composited PNG's
    # alpha silhouette). Only shows where the composition is transparent.
    outline: bool = False
    outline_color: str = "#ffffff"
    outline_width: int = 12


@router.post("/api/sticker_drafts/{draft_id}/compose")
async def compose_sticker(draft_id: int, body: ComposeBody, request: Request):
    """Studio export. Unlike /make (which re-processes the source frame), this
    takes a fully-composited 512×512 PNG (cutout + text + overlays from the
    Fabric.js canvas) and encodes IT directly to a static webp sticker, then
    adds it to the user's static pack — same pack-add path as /make."""
    payload = await _mini._verify(request)
    _mini.require_scope(payload, "smdl.stickers")
    user_id = int(payload["user"]["id"])
    first_name = (payload.get("user") or {}).get("first_name")

    d = await _db.sticker_draft_get(draft_id, user_id)
    if not d:
        raise HTTPException(status_code=404, detail="draft not found")

    import base64
    raw = body.png_b64.split(",", 1)[-1] if body.png_b64.startswith("data:") else body.png_b64
    try:
        png_bytes = base64.b64decode(raw, validate=False)
    except Exception:
        raise HTTPException(status_code=400, detail="invalid png_b64")
    if not png_bytes or len(png_bytes) > 8_000_000:
        raise HTTPException(status_code=400, detail="composited PNG missing or too large")

    await _db.sticker_draft_set_status(draft_id, "processing")
    out_dir = OUTPUT_DIR / str(user_id)
    out_dir.mkdir(parents=True, exist_ok=True)
    comp_png = out_dir / f"{draft_id}_studio.png"
    dst = out_dir / f"{draft_id}.webp"
    try:
        comp_png.write_bytes(png_bytes)
        # Optional die-cut outline — runs on the composited PNG before encode.
        if body.outline:
            loop = asyncio.get_running_loop()
            dok, derr = await loop.run_in_executor(
                None, lambda: _sp.apply_die_cut(
                    comp_png, comp_png, color=body.outline_color or "#ffffff",
                    width=int(body.outline_width or 12)))
            if not dok:
                logger.warning("die-cut failed for d=%s: %s", draft_id, derr)
        ok, err = await _sp.make_static_sticker(
            comp_png, dst, crop=None, seek_s=0.0, mask=None, keep_alpha=True)
    finally:
        try:
            comp_png.unlink(missing_ok=True)
        except Exception:
            pass
    if not ok:
        await _db.sticker_draft_set_status(draft_id, "failed", err)
        raise HTTPException(status_code=422, detail=err or "encode failed")

    # Static export is cheap — publish inline (shares the /make publish path).
    try:
        result = await _publish_sticker(
            user_id, first_name, draft_id, dst,
            emoji=body.emoji, pack_kind="static", sticker_format="static")
    except StickerBuildError as e:
        raise HTTPException(status_code=e.status, detail=e.detail)
    return {"ok": True, **result}


class CutoutBody(BaseModel):
    # data-URL or raw base64 of the frame the Studio wants the subject cut from.
    png_b64: str


@router.post("/api/sticker_drafts/{draft_id}/cutout")
async def cutout_frame(draft_id: int, body: CutoutBody, request: Request):
    """Studio helper: background-remove a posted frame and hand back a
    transparent PNG (data-URL) the canvas swaps in as its base layer. Lets the
    die-cut outline hug the subject instead of the square edge. Does NOT touch
    the user's pack — purely an image transform."""
    payload = await _mini._verify(request)
    _mini.require_scope(payload, "smdl.stickers")
    user_id = int(payload["user"]["id"])
    if not await _db.sticker_draft_get(draft_id, user_id):
        raise HTTPException(status_code=404, detail="draft not found")

    import base64
    raw = body.png_b64.split(",", 1)[-1] if body.png_b64.startswith("data:") else body.png_b64
    try:
        png_bytes = base64.b64decode(raw, validate=False)
    except Exception:
        raise HTTPException(status_code=400, detail="invalid png_b64")
    if not png_bytes or len(png_bytes) > 8_000_000:
        raise HTTPException(status_code=400, detail="frame missing or too large")

    out_dir = OUTPUT_DIR / str(user_id)
    out_dir.mkdir(parents=True, exist_ok=True)
    in_png = out_dir / f"{draft_id}_cutin.png"
    cut_png = out_dir / f"{draft_id}_cutout.png"
    try:
        in_png.write_bytes(png_bytes)
        ok, err = await _sp.bg_remove(in_png, cut_png)
        if not ok:
            raise HTTPException(status_code=422, detail=err or "cutout failed")
        data = cut_png.read_bytes()
    finally:
        for f in (in_png, cut_png):
            try:
                f.unlink(missing_ok=True)
            except Exception:
                pass
    b64 = base64.b64encode(data).decode("ascii")
    return {"ok": True, "png_b64": f"data:image/png;base64,{b64}"}


class ComposeVideoBody(BaseModel):
    # data-URL or raw base64 of the Studio's overlay layers (text/emoji/image)
    # rendered on a TRANSPARENT 512² canvas — the base video frame is excluded.
    overlay_b64: str
    emoji: str = "🎬"
    trim_start: float = 0.0
    trim_end:   float = 3.0
    crop_x: int | None = None
    crop_y: int | None = None
    crop_w: int | None = None
    crop_h: int | None = None
    # Also matte the subject out per-frame (transparent subject + overlay on top).
    cutout: bool = False
    # v2.7-C "best quality" matte (only meaningful with cutout).
    hq: bool = False
    # ANIMATED overlays (Giphy stickers placed via the Assets tool): each is a
    # remote GIF + its placement in 512² space. The client EXCLUDES these from
    # the flattened overlay_b64 PNG; the server loops each GIF over the clip
    # (ffmpeg -stream_loop) so the overlay itself animates. Max 3; ignored when
    # cutout=True (those fall back to the static flatten client-side).
    anim_overlays: list[dict] | None = None


@router.post("/api/sticker_drafts/{draft_id}/compose_video")
async def compose_video_sticker(draft_id: int, body: ComposeVideoBody, request: Request):
    """Studio export for a VIDEO draft (v2.7-A). Takes the composited overlay
    layers (transparent PNG) and enqueues a background job that composites them
    onto every frame of the source video → an animated webm. Returns {job_id};
    the editor polls /api/sticker_jobs/{id}. (Static drafts still use /compose.)"""
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

    import base64
    raw = body.overlay_b64.split(",", 1)[-1] if body.overlay_b64.startswith("data:") else body.overlay_b64
    try:
        png_bytes = base64.b64decode(raw, validate=False)
    except Exception:
        raise HTTPException(status_code=400, detail="invalid overlay_b64")
    if not png_bytes or len(png_bytes) > 8_000_000:
        raise HTTPException(status_code=400, detail="overlay PNG missing or too large")

    out_dir = OUTPUT_DIR / str(user_id)
    out_dir.mkdir(parents=True, exist_ok=True)
    overlay_png = out_dir / f"{draft_id}_overlay.png"
    overlay_png.write_bytes(png_bytes)

    params = {
        "overlay_path": str(overlay_png),
        "emoji": body.emoji,
        "trim_start": body.trim_start, "trim_end": body.trim_end,
        "crop_x": body.crop_x, "crop_y": body.crop_y,
        "crop_w": body.crop_w, "crop_h": body.crop_h,
        "cutout": bool(body.cutout), "hq": bool(body.hq),
        "_first_name": first_name,
    }

    # Animated overlays: download each placed Giphy sticker's GIF (same SSRF
    # guard as from_url) next to the flat PNG; pass path + 512²-space geometry
    # to the encoder. Cutout exports don't take this path (client flattens).
    anim_params: list[dict] = []
    if body.anim_overlays and not body.cutout:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            for i, ao in enumerate((body.anim_overlays or [])[:3]):
                url = str(ao.get("url") or "").strip()
                host = (urlparse(url).hostname or "").lower()
                if not url.startswith(("http://", "https://")) or not any(
                        host == h.lstrip(".") or host.endswith(h) for h in _GIF_FETCH_HOSTS):
                    continue
                try:
                    r = await client.get(url)
                    r.raise_for_status()
                    if len(r.content) > 8_000_000:
                        continue
                    gp = out_dir / f"{draft_id}_anim{i}.gif"
                    gp.write_bytes(r.content)
                    anim_params.append({
                        "path": str(gp),
                        "x": max(-512, min(512, int(ao.get("x") or 0))),
                        "y": max(-512, min(512, int(ao.get("y") or 0))),
                        "w": max(8, min(512, int(ao.get("w") or 128))),
                        "h": max(8, min(512, int(ao.get("h") or 128))),
                    })
                except (httpx.HTTPError, OSError, ValueError) as e:
                    logger.warning("anim overlay %d fetch failed: %s", i, e)
    if anim_params:
        params["anim_overlays"] = anim_params

    job_id = await _sj.enqueue(user_id=user_id, draft_id=draft_id,
                               kind="video_overlay", params=params)
    return {"ok": True, "job_id": job_id, "async": True}


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


# ── Lookup any sticker set + clone individual stickers ────────────────────
#
# Telegram's bot API only lets the bot that CREATED a pack mutate it
# (delete/rename/re-emoji/etc.). For packs owned by other bots — including
# the user's own packs created with @Stickers or with the owner-only
# @azsmdl_bot on the other deployment — we can still READ the set and
# CLONE individual stickers into one of THIS bot's owned packs, where
# the user gets full edit power again.


def _parse_pack_name(url_or_name: str) -> str:
    """Accept either a t.me/addstickers/<name>(?...) URL or a bare pack
    name. Returns the canonical pack name. Invalid input → empty string."""
    s = (url_or_name or "").strip()
    if not s:
        return ""
    # Drop URL prefix variants.
    for prefix in ("https://t.me/addstickers/", "http://t.me/addstickers/",
                   "t.me/addstickers/", "tg://addstickers?set="):
        if s.startswith(prefix):
            s = s[len(prefix):]
            break
    # Trim querystring / fragment if any.
    for sep in ("?", "#", "/"):
        if sep in s:
            s = s.split(sep, 1)[0]
    return s


async def _owned_by_us(bot, pack_name: str) -> bool:
    """Heuristic: our bot's packs always end in `_by_<bot_username>`. TG
    doesn't expose pack ownership directly; this naming convention is the
    only reliable signal short of attempting a mutation."""
    try:
        u = await get_bot_username_safe(bot)
    except Exception:
        return False
    return bool(u) and pack_name.lower().endswith(f"_by_{u.lower()}")


async def get_bot_username_safe(bot) -> str:
    """Cached bot username. Tries the existing cache in sticker_telegram
    first so we don't re-hit get_me when sticker_telegram already did."""
    from .sticker_telegram import _BOT_USERNAME, get_bot_username
    if _BOT_USERNAME:
        return _BOT_USERNAME
    return await get_bot_username(bot)


@router.get("/api/sticker_set/lookup")
async def lookup_sticker_set(request: Request, name: str | None = "") -> dict:
    """Look up ANY Telegram sticker set by URL or name. Returns the same
    serialised shape as /api/sticker_pack/contents plus an `owned_by_us`
    flag the frontend uses to decide whether to enable mutation buttons.
    """
    payload = await _mini._verify(request)
    _mini.require_scope(payload, "smdl.stickers")
    pack_name = _parse_pack_name(name or "")
    if not pack_name:
        raise HTTPException(400, "URL or pack name is required")
    tg_app = _require_bot()
    try:
        tg_set = await tg_app.bot.get_sticker_set(name=pack_name)
    except TelegramError as e:
        msg = str(e).lower()
        if "not found" in msg or "stickerset_invalid" in msg:
            raise HTTPException(404, f"pack '{pack_name}' not found")
        raise HTTPException(502, f"Telegram: {e}")
    owned = await _owned_by_us(tg_app.bot, tg_set.name)
    return {
        "name":         tg_set.name,
        "title":        tg_set.title,
        "sticker_type": getattr(tg_set, "sticker_type", "regular"),
        "stickers":     [_serialise_sticker(s) for s in (tg_set.stickers or [])],
        "owned_by_us":  owned,
        "url":          f"https://t.me/addstickers/{tg_set.name}",
    }


def _sniff_sticker_format(data: bytes) -> tuple[str, str] | None:
    """Magic-byte sniff for the formats Telegram supports as stickers.
    Returns (sticker_format, file_extension) or None on no match."""
    if data[:4] == b"\x1aE\xdf\xa3":                   # EBML / WebM
        return "video", ".webm"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":  # RIFF WEBP
        return "static", ".webp"
    if data[:8] == b"\x89PNG\r\n\x1a\n":               # PNG
        return "static", ".png"
    return None


async def _clone_one_sticker(tg_app, user_id: int, first_name: str | None,
                              source_file_id: str, target_kind: str,
                              emoji: str) -> dict:
    """Fetch one sticker by file_id, re-upload into the caller's pack
    of `target_kind`. Returns a result dict; raises HTTPException on
    fatal errors so the single-clone endpoint can propagate unchanged."""
    try:
        src_file = await tg_app.bot.get_file(source_file_id)
        data = bytes(await src_file.download_as_bytearray())
    except TelegramError as e:
        raise HTTPException(404, f"source sticker not fetchable: {e}")
    if not data:
        raise HTTPException(404, "source sticker is empty")
    sniff = _sniff_sticker_format(data)
    if sniff is None:
        raise HTTPException(415, "unsupported source format (not webm/webp/png)")
    src_format, ext = sniff
    # The unified 'regular' pack accepts BOTH video and static (Telegram allows
    # mixed), and custom_emoji keeps its source format — so we never reject on
    # format. Clone in the source's own format.
    target_sticker_format = src_format
    import uuid as _uuid
    scratch_dir = DRAFTS_DIR / str(user_id)
    scratch_dir.mkdir(parents=True, exist_ok=True)
    scratch = scratch_dir / f"_clone_{_uuid.uuid4().hex}{ext}"
    try:
        scratch.write_bytes(data)
        pack = await _st.resolve_pack(tg_app.bot, user_id, first_name, kind=target_kind)
        try:
            file_id, set_url = await _st.upload_and_add(
                tg_app.bot, user_id, scratch,
                emoji=(emoji or "🎬"),
                pack_name=pack["pack_name"],
                pack_title=pack["pack_title"],
                sticker_format=target_sticker_format,
                sticker_type=("custom_emoji" if target_kind == "custom_emoji" else "regular"),
            )
        except TelegramError as e:
            msg = str(e)
            if "PEER_ID_INVALID" in msg or "user not found" in msg.lower():
                raise HTTPException(400, "Send /start to the bot first, then try again.")
            raise HTTPException(502, f"Telegram: {msg}")
        if not pack.get("exists_in_db"):
            await _db.sticker_pack_create(
                user_id, pack["pack_name"], pack["pack_title"], set_url,
                kind=target_kind,
            )
        await _db.sticker_record(
            user_id=user_id, pack_name=pack["pack_name"],
            source_draft_id=None, emoji=(emoji or "🎬"),
            telegram_file_id=file_id, webm_path=str(scratch),
        )
        return {
            "file_id":        file_id,
            "set_url":        set_url,
            "target_format":  target_sticker_format,
        }
    finally:
        try: scratch.unlink(missing_ok=True)
        except Exception: pass


@router.post("/api/sticker_pack/clone_sticker")
async def clone_sticker(body: CloneStickerBody, request: Request) -> dict:
    """Copy a sticker we don't own into a pack we do own."""
    payload = await _mini._verify(request)
    _mini.require_scope(payload, "smdl.stickers")
    user_id = int(payload["user"]["id"])
    first_name = (payload.get("user") or {}).get("first_name")
    target_kind = _kind_param(body.target_kind)
    tg_app = _require_bot()
    r = await _clone_one_sticker(tg_app, user_id, first_name,
                                  body.source_file_id, target_kind, body.emoji)
    return {"ok": True, "target_kind": target_kind, **r}


@router.post("/api/sticker_pack/clone_pack")
async def clone_pack(body: ClonePackBody, request: Request) -> dict:
    """Clone every (compatible) sticker from `body.source` into the caller's
    pack of `body.target_kind`. Preserves each source sticker's emoji.

    What this does:
      - Looks up the source pack (`get_sticker_set`).
      - For each sticker, attempts a single clone via _clone_one_sticker.
      - Skips anything incompatible (animated, wrong format for the
        target kind) and counts it.
      - Caps at `body.limit` to keep the request inside reasonable time
        budgets — caller can re-issue with more.
    Returns: {added, skipped, errors[], target_kind, target_url}.

    What this does NOT do:
      - Create a SEPARATE per-source pack — clones land in the caller's
        single pack-of-kind (so capacity is shared with their other
        stickers). The TG ceiling of 120 per pack still applies; if it
        bites mid-batch, the remaining clones surface in `errors[]` and
        the caller can free space + retry.
    """
    payload = await _mini._verify(request)
    _mini.require_scope(payload, "smdl.stickers")
    user_id = int(payload["user"]["id"])
    first_name = (payload.get("user") or {}).get("first_name")
    target_kind = _kind_param(body.target_kind)
    tg_app = _require_bot()

    pack_name = _parse_pack_name(body.source)
    if not pack_name:
        raise HTTPException(400, "source is required")
    try:
        src_set = await tg_app.bot.get_sticker_set(name=pack_name)
    except TelegramError as e:
        msg = str(e).lower()
        if "not found" in msg or "stickerset_invalid" in msg:
            raise HTTPException(404, f"source pack '{pack_name}' not found")
        raise HTTPException(502, f"Telegram: {e}")

    limit = max(1, min(int(body.limit or 50), 120))
    stickers = list(src_set.stickers or [])[:limit]
    added = 0
    skipped = 0
    errors: list[dict] = []
    target_url: str | None = None
    for idx, s in enumerate(stickers):
        if getattr(s, "is_animated", False):
            skipped += 1
            errors.append({"index": idx, "reason": "animated stickers can't be cloned"})
            continue
        # No format precheck — a 'regular' pack mixes video + static. (Animated
        # .tgs is filtered above; it can't be cloned as webm/webp.)
        try:
            r = await _clone_one_sticker(
                tg_app, user_id, first_name,
                s.file_id, target_kind,
                emoji=(getattr(s, "emoji", None) or "🎬"),
            )
            added += 1
            target_url = r["set_url"]
        except HTTPException as e:
            errors.append({"index": idx, "reason": str(e.detail)})
            # STICKERS_TOO_MUCH from TG means the target pack hit 120.
            # No point hammering further calls — bail and surface.
            if "TOO_MUCH" in str(e.detail).upper() or "STICKERS_TOO_MUCH" in str(e.detail).upper():
                errors.append({"index": idx,
                               "reason": "target pack is full (120 sticker limit) — remove some and retry"})
                break
        except Exception as e:
            errors.append({"index": idx, "reason": f"{type(e).__name__}: {e}"})
    truncated = len(src_set.stickers or []) > len(stickers)
    return {
        "ok":          True,
        "added":       added,
        "skipped":     skipped,
        "errors":      errors,
        "target_kind": target_kind,
        "target_url":  target_url,
        "source_total":  len(src_set.stickers or []),
        "processed":     len(stickers),
        "truncated":     truncated,
    }


async def import_whole_set(tg_app, user_id: int, first_name: str | None,
                           source: str, limit: int = 120) -> dict:
    """Import a whole source pack into a NEW pack of the user's, named after it.
    Mints a fresh (active) pack from the source title, then clones every
    compatible sticker into it. Shared by the API endpoint and the bot's
    send-a-sticker 'import all' flow. Raises HTTPException on a bad/missing
    source (the endpoint propagates it; the bot catches it)."""
    pack_name = _parse_pack_name(source)
    if not pack_name:
        raise HTTPException(status_code=400, detail="source is required")
    try:
        src_set = await tg_app.bot.get_sticker_set(name=pack_name)
    except TelegramError as e:
        msg = str(e).lower()
        if "not found" in msg or "stickerset_invalid" in msg:
            raise HTTPException(status_code=404, detail=f"source pack '{pack_name}' not found")
        raise HTTPException(status_code=502, detail=f"Telegram: {e}")

    title = (getattr(src_set, "title", None) or "Imported pack").strip()[:60]
    new_pack = await _st.create_named_pack(tg_app.bot, user_id, first_name, title, kind="regular")
    limit = max(1, min(int(limit or 120), 120))
    stickers = list(src_set.stickers or [])[:limit]
    added = 0
    skipped = 0
    errors: list[dict] = []
    target_url = new_pack["telegram_url"]
    for idx, s in enumerate(stickers):
        if getattr(s, "is_animated", False):
            skipped += 1
            continue
        try:
            r = await _clone_one_sticker(
                tg_app, user_id, first_name, s.file_id, "regular",
                emoji=(getattr(s, "emoji", None) or "🎬"))
            added += 1
            target_url = r["set_url"]
        except HTTPException as e:
            errors.append({"index": idx, "reason": str(e.detail)})
            if "TOO_MUCH" in str(e.detail).upper():
                break
        except Exception as e:
            errors.append({"index": idx, "reason": f"{type(e).__name__}: {e}"})
    return {
        "pack": {**new_pack, "telegram_url": target_url},
        "added": added, "skipped": skipped, "errors": errors,
        "source_total": len(src_set.stickers or []),
        "source_title": title,
    }


class ImportSetBody(BaseModel):
    source: str            # pack short-name or a t.me/addstickers/... URL
    limit: int = 120


@router.post("/api/sticker_pack/import_set")
async def import_set(body: ImportSetBody, request: Request) -> dict:
    """Import a whole source pack into a NEW pack of yours named after it.
    Drives the 'import whole pack' button from the send-a-sticker flow."""
    payload = await _mini._verify(request)
    _mini.require_scope(payload, "smdl.stickers")
    user_id = int(payload["user"]["id"])
    first_name = (payload.get("user") or {}).get("first_name")
    tg_app = _require_bot()
    r = await import_whole_set(tg_app, user_id, first_name, body.source, limit=body.limit)
    return {"ok": True, **r}


class ImportPrefBody(BaseModel):
    mode: str              # 'single' | 'all'


@router.get("/api/sticker_import_pref")
async def get_import_pref(request: Request) -> dict:
    """Per-user default for sending a sticker to the bot (single vs whole pack)."""
    payload = await _mini._verify(request)
    _mini.require_scope(payload, "smdl.stickers")
    user_id = int(payload["user"]["id"])
    mode = await _db.get_setting(f"sticker_import_{user_id}")
    return {"mode": mode if mode in ("single", "all") else "single"}


@router.post("/api/sticker_import_pref")
async def set_import_pref(body: ImportPrefBody, request: Request) -> dict:
    payload = await _mini._verify(request)
    _mini.require_scope(payload, "smdl.stickers")
    user_id = int(payload["user"]["id"])
    mode = body.mode if body.mode in ("single", "all") else "single"
    await _db.set_setting(f"sticker_import_{user_id}", mode)
    return {"ok": True, "mode": mode}


# ── Smart trim (ffmpeg scene-detect → "best" 3s window) ────────────────────


async def _best_window_for(src: Path, target_s: float = 3.0) -> tuple[float, float]:
    """Pick a `target_s`-second window from `src` that's likely the most
    interesting bit. Heuristic:

      1. ffmpeg select='gt(scene,0.3)' lists scene-change timestamps.
      2. For each candidate, score = number of scene-changes inside
         (timestamp, timestamp + target_s). Highest score wins.
      3. Fall back to the middle of the clip if scene detect found
         nothing (typical for very static clips or single-shot videos).
    """
    import shutil as _sh
    import asyncio as _asyncio
    if not _sh.which("ffmpeg"):
        return 0.0, target_s
    # ffmpeg scene change detector — emit pts_time on the stderr-printed
    # log via showinfo, then we parse.
    cmd = [
        "ffmpeg", "-nostats", "-loglevel", "info",
        "-i", str(src),
        "-vf", "select='gt(scene,0.3)',showinfo",
        "-f", "null", "-",
    ]
    try:
        proc = await _asyncio.create_subprocess_exec(
            *cmd,
            stdout=_asyncio.subprocess.DEVNULL,
            stderr=_asyncio.subprocess.PIPE,
        )
        try:
            _, err = await _asyncio.wait_for(proc.communicate(), timeout=30)
        except _asyncio.TimeoutError:
            proc.kill(); await proc.wait()
            err = b""
    except Exception as e:
        logger.debug("smart-trim ffmpeg launched failed: %s", e)
        return 0.0, target_s
    text = err.decode("utf-8", errors="replace")
    # ffmpeg's showinfo prints lines like:
    # [Parsed_showinfo_1 @ 0x...] n:  12 pts:  19260 pts_time:0.642
    import re as _re
    timestamps: list[float] = []
    for m in _re.finditer(r"pts_time:\s*(\d+(?:\.\d+)?)", text):
        try: timestamps.append(float(m.group(1)))
        except Exception: pass
    # Always include duration from ffmpeg's "Duration:" header so we can
    # clamp the window to the clip length.
    dur_match = _re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", text)
    duration = 0.0
    if dur_match:
        h, m, s = dur_match.groups()
        duration = int(h) * 3600 + int(m) * 60 + float(s)
    if duration <= 0:
        # Last resort — fall back to the probe helper.
        meta = await probe(src)
        duration = float(meta.get("duration_s") or 0.0)
    if duration <= target_s:
        return 0.0, min(target_s, duration or target_s)
    if not timestamps:
        # No scene changes detected — middle of the clip.
        mid = duration / 2.0
        start = max(0.0, mid - target_s / 2.0)
        return start, start + target_s
    # Score each candidate start = how many scene-changes fall inside
    # [start, start + target_s]. Best score wins.
    best_start = timestamps[0]
    best_score = 0
    for t in timestamps:
        if t + target_s > duration:
            break
        score = sum(1 for x in timestamps if t <= x < t + target_s)
        if score > best_score:
            best_start = t
            best_score = score
    return best_start, min(duration, best_start + target_s)


# Re-export probe so the helper above can call it without an extra import dance.
probe = _sp.probe


@router.get("/api/sticker_drafts/{draft_id}/best_window")
async def best_window(draft_id: int, request: Request,
                      target: float = 3.0) -> dict:
    """Recommend a `target`-second start/end window for the editor's
    "smart trim" button. Scoped to the caller's drafts."""
    payload = await _mini._verify(request)
    _mini.require_scope(payload, "smdl.stickers")
    user_id = int(payload["user"]["id"])
    d = await _db.sticker_draft_get(int(draft_id), user_id)
    if not d:
        raise HTTPException(404, "draft not found")
    src = Path(d["file_path"])
    if not src.exists():
        raise HTTPException(410, "draft file gone")
    t_secs = max(0.5, min(float(target), 3.0))
    start, end = await _best_window_for(src, target_s=t_secs)
    return {"start": round(start, 3), "end": round(end, 3)}


# ── Make a draft from an existing SMDL download ────────────────────────────


_DL_MIME_EXT_HINT = {
    ".mp4": "video/mp4", ".mov": "video/quicktime", ".webm": "video/webm",
    ".mkv": "video/x-matroska", ".gif": "image/gif",
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".png": "image/png", ".webp": "image/webp",
}


def _is_under_downloads(p: Path) -> bool:
    """True if `p` resolves to somewhere underneath DOWNLOADS_DIR (resolved).
    Used to defeat ../ tricks in the from_download payload."""
    try:
        root = Path(DOWNLOADS_DIR).resolve()
        return root in p.resolve().parents or p.resolve() == root
    except Exception:
        return False


@router.post("/api/sticker_drafts/from_download")
async def draft_from_download(body: FromDownloadBody, request: Request) -> dict:
    """Hardlink (or copy as fallback) a file the user already downloaded into
    DRAFTS_DIR and create a draft row. Skips the upload round-trip — a 200 MB
    movie from Theater can become a sticker without re-uploading bytes.
    """
    payload = await _mini._verify(request)
    _mini.require_scope(payload, "smdl.stickers")
    user_id = int(payload["user"]["id"])

    raw = (body.file_path or "").strip()
    if not raw:
        raise HTTPException(400, "file_path is required")
    # Accept absolute or relative-to-DOWNLOADS_DIR.
    p = Path(raw)
    if not p.is_absolute():
        p = Path(DOWNLOADS_DIR) / raw
    if not _is_under_downloads(p):
        raise HTTPException(403, "path is outside the downloads root")
    if not p.exists() or not p.is_file():
        raise HTTPException(404, f"source not found: {p}")

    # Mime hint from extension; the real content negotiation happens later
    # in the encoder.
    ext = p.suffix.lower() or ".bin"
    mime = _DL_MIME_EXT_HINT.get(ext, "application/octet-stream")
    if not (mime.startswith("video/") or mime in ("image/gif", "image/jpeg",
                                                   "image/png", "image/webp")):
        raise HTTPException(415, f"unsupported source: {ext}")

    meta = await _sp.probe(p)

    draft_id = await _db.sticker_draft_insert(
        user_id=user_id,
        telegram_file_id="from_download",  # sentinel — see upload_draft
        file_path="",
        mime_type=mime,
        duration_s=meta.get("duration_s"),
        width=meta.get("width"),
        height=meta.get("height"),
    )
    final_path = draft_path(user_id, draft_id, ext.lstrip("."))
    final_path.parent.mkdir(parents=True, exist_ok=True)
    # Hardlink for speed (same filesystem, no bytes copied); fall back to a
    # streaming copy on EXDEV or anything else weird.
    try:
        os.link(p, final_path)
    except OSError:
        try:
            import shutil as _sh
            _sh.copy2(p, final_path)
        except Exception as e:
            logger.warning("from_download copy failed u=%s d=%s: %s", user_id, draft_id, e)
            raise HTTPException(500, "draft persist failed")
    import aiosqlite
    async with aiosqlite.connect(_db.DB_PATH) as dbh:
        await dbh.execute(
            "UPDATE sticker_drafts SET file_path = ? WHERE id = ?",
            (str(final_path), draft_id),
        )
        await dbh.commit()

    logger.info("from_download OK u=%s d=%s src=%s", user_id, draft_id, p.name)
    return {
        "id":         draft_id,
        "mime_type":  mime,
        "duration_s": meta.get("duration_s"),
        "width":      meta.get("width"),
        "height":     meta.get("height"),
        "source":     p.name,
    }


# ── GIF library search (Giphy / Tenor) ─────────────────────────────────────


@router.get("/api/stickers/gif_search")
async def gif_search(request: Request, q: str = "", source: str = "giphy",
                     limit: int = 24) -> dict:
    """Proxy a Giphy/Tenor search (or trending when q is empty) and return a
    normalised list of {id, preview, url, is_mp4, title}. `url` prefers the
    MP4 form (smaller, cleaner alpha-free encode for the sticker pipeline),
    falling back to the GIF. The browser never sees the API key."""
    payload = await _mini._verify(request)
    _mini.require_scope(payload, "smdl.stickers")
    q = (q or "").strip()
    src = (source or "giphy").lower()
    limit = max(1, min(int(limit or 24), 50))
    items: list[dict] = []
    try:
        async with httpx.AsyncClient(timeout=12, follow_redirects=True) as client:
            if src == "giphy":
                if not GIPHY_API_KEY:
                    raise HTTPException(503, "Giphy not configured — set GIPHY_API_KEY")
                ep = "search" if q else "trending"
                params = {"api_key": GIPHY_API_KEY, "limit": limit, "rating": "pg-13"}
                if q:
                    params["q"] = q
                r = await client.get(f"https://api.giphy.com/v1/gifs/{ep}", params=params)
                body = {}
                try:
                    body = r.json()
                except Exception:
                    body = {}
                gmeta = body.get("meta") or {}
                if r.status_code != 200 or gmeta.get("status", 200) != 200:
                    bad = (gmeta.get("msg") or "").upper()
                    if r.status_code in (401, 403) or "BANNED" in bad or "KEY" in bad:
                        raise HTTPException(503, "Giphy key rejected (invalid/banned) — set a valid GIPHY_API_KEY")
                    raise HTTPException(502, f"giphy error: {gmeta.get('msg') or r.status_code}")
                for g in (body.get("data") or []):
                    imgs = g.get("images") or {}
                    prev = ((imgs.get("fixed_width_small") or imgs.get("fixed_width")
                             or imgs.get("preview_gif") or {}).get("url"))
                    mp4 = ((imgs.get("original_mp4") or imgs.get("original") or {}).get("mp4"))
                    gif = ((imgs.get("original") or {}).get("url"))
                    items.append({"id": g.get("id"), "preview": prev,
                                  "url": mp4 or gif, "is_mp4": bool(mp4),
                                  "title": g.get("title") or ""})
            elif src == "tenor":
                if not TENOR_API_KEY:
                    raise HTTPException(503, "Tenor not configured — set TENOR_API_KEY")
                ep = "search" if q else "featured"
                params = {"key": TENOR_API_KEY, "limit": limit, "client_key": "smdl",
                          "media_filter": "tinygif,mp4,gif", "contentfilter": "medium"}
                if q:
                    params["q"] = q
                r = await client.get(f"https://tenor.googleapis.com/v2/{ep}", params=params)
                r.raise_for_status()
                for g in (r.json().get("results") or []):
                    mf = g.get("media_formats") or {}
                    prev = ((mf.get("tinygif") or mf.get("nanogif")
                             or mf.get("gifpreview") or {}).get("url"))
                    mp4 = (mf.get("mp4") or {}).get("url")
                    gif = (mf.get("gif") or {}).get("url")
                    items.append({"id": g.get("id"), "preview": prev,
                                  "url": mp4 or gif, "is_mp4": bool(mp4),
                                  "title": g.get("content_description") or ""})
            elif src == "giphy_stickers":
                # Giphy STICKERS — transparent, ideal as editor overlays.
                if not GIPHY_API_KEY:
                    raise HTTPException(503, "Giphy not configured — set GIPHY_API_KEY")
                ep = "search" if q else "trending"
                params = {"api_key": GIPHY_API_KEY, "limit": limit, "rating": "pg-13"}
                if q:
                    params["q"] = q
                r = await client.get(f"https://api.giphy.com/v1/stickers/{ep}", params=params)
                body = {}
                try:
                    body = r.json()
                except Exception:
                    body = {}
                gmeta = body.get("meta") or {}
                if r.status_code != 200 or gmeta.get("status", 200) != 200:
                    bad = (gmeta.get("msg") or "").upper()
                    if r.status_code in (401, 403) or "BANNED" in bad or "KEY" in bad:
                        raise HTTPException(503, "Giphy key rejected (invalid/banned) — set a valid GIPHY_API_KEY")
                    raise HTTPException(502, f"giphy error: {gmeta.get('msg') or r.status_code}")
                for g in (body.get("data") or []):
                    imgs = g.get("images") or {}
                    prev = ((imgs.get("fixed_width_small") or imgs.get("fixed_width")
                             or imgs.get("preview_gif") or {}).get("url"))
                    # Transparent STILL (PNG) makes a clean Fabric overlay; fall
                    # back to the animated transparent webp / gif first frame.
                    still = ((imgs.get("fixed_width_still") or imgs.get("original_still") or {}).get("url"))
                    anim = ((imgs.get("fixed_width") or imgs.get("original") or {}).get("webp")
                            or (imgs.get("original") or {}).get("url"))
                    # The animated transparent GIF — ffmpeg-friendly (loops via
                    # -ignore_loop) for the editor's animated-overlay export.
                    anim_gif = ((imgs.get("original") or imgs.get("fixed_width") or {}).get("url"))
                    items.append({"id": g.get("id"), "preview": prev,
                                  "url": still or anim, "anim_url": anim_gif,
                                  "is_mp4": False, "title": g.get("title") or ""})
            else:
                raise HTTPException(400, f"unknown source: {src}")
    except HTTPException:
        raise
    except httpx.HTTPError as e:
        logger.warning("gif_search %s failed: %s", src, e)
        raise HTTPException(502, f"{src} search failed")
    return {"ok": True, "source": src,
            "items": [i for i in items if i.get("url") and i.get("preview")]}


@router.get("/api/stickers/asset_proxy")
async def asset_proxy(request: Request, u: str = ""):
    """Stream a Giphy/Tenor image SAME-ORIGIN so the editor's Fabric canvas can
    use it as an overlay without tainting the export (cross-origin images break
    canvas.toDataURL). SSRF-guarded to the same hosts as from_url; images only."""
    payload = await _mini._verify(request)
    _mini.require_scope(payload, "smdl.stickers")
    url = (u or "").strip()
    if not url.startswith(("http://", "https://")):
        raise HTTPException(400, "url must be http(s)")
    host = (urlparse(url).hostname or "").lower()
    if not any(host == h.lstrip(".") or host.endswith(h) for h in _GIF_FETCH_HOSTS):
        raise HTTPException(403, "url host not allowed")
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            r = await client.get(url)
            r.raise_for_status()
    except httpx.HTTPError:
        raise HTTPException(502, "asset fetch failed")
    if len(r.content) > _UPLOAD_MAX_BYTES:
        raise HTTPException(413, "asset too large")
    ctype = (r.headers.get("content-type") or "").split(";")[0].lower()
    if not ctype.startswith("image/"):
        low = url.lower().split("?")[0]
        if not low.endswith((".png", ".webp", ".gif", ".jpg", ".jpeg")):
            raise HTTPException(415, "not an image")
        ctype = "image/png"
    from fastapi.responses import Response as _Resp
    return _Resp(content=r.content, media_type=ctype,
                 headers={"Cache-Control": "public, max-age=86400"})


@router.post("/api/sticker_drafts/from_url")
async def draft_from_url(body: FromUrlBody, request: Request) -> dict:
    """Fetch a Giphy/Tenor media URL (chosen from the in-app GIF search) into
    DRAFTS_DIR and create a draft row — same shape as a direct upload, so the
    normal make-sticker flow takes over. SSRF-guarded to _GIF_FETCH_HOSTS."""
    payload = await _mini._verify(request)
    _mini.require_scope(payload, "smdl.stickers")
    user_id = int(payload["user"]["id"])

    url = (body.url or "").strip()
    if not url.startswith(("http://", "https://")):
        raise HTTPException(400, "url must be http(s)")
    host = (urlparse(url).hostname or "").lower()
    if not any(host == h.lstrip(".") or host.endswith(h) for h in _GIF_FETCH_HOSTS):
        raise HTTPException(403, "url host not allowed (Giphy/Tenor only)")

    user_dir = DRAFTS_DIR / str(user_id)
    user_dir.mkdir(parents=True, exist_ok=True)
    tmp_path = user_dir / f"_gif_{uuid.uuid4().hex}.bin"
    written = 0
    mime = ""
    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            async with client.stream("GET", url) as resp:
                resp.raise_for_status()
                mime = (resp.headers.get("content-type") or "").split(";")[0].lower()
                with open(tmp_path, "wb") as out:
                    async for chunk in resp.aiter_bytes(64 * 1024):
                        written += len(chunk)
                        if written > _UPLOAD_MAX_BYTES:
                            raise HTTPException(413, "remote file too large (max 50 MB)")
                        out.write(chunk)
    except HTTPException:
        try: tmp_path.unlink(missing_ok=True)
        except Exception: pass
        raise
    except httpx.HTTPError as e:
        try: tmp_path.unlink(missing_ok=True)
        except Exception: pass
        logger.warning("from_url fetch failed u=%s: %s", user_id, e)
        raise HTTPException(502, "could not fetch the GIF")

    if written == 0:
        try: tmp_path.unlink(missing_ok=True)
        except Exception: pass
        raise HTTPException(400, "empty download")

    # Normalise mime: CDNs serve image/gif or video/mp4; fall back to the URL
    # suffix, then to mp4 (Giphy/Tenor's mp4 form is what we usually request).
    if not (mime.startswith("video/") or mime == "image/gif"):
        low = url.lower().split("?")[0]
        if low.endswith(".mp4"):    mime = "video/mp4"
        elif low.endswith(".gif"):  mime = "image/gif"
        elif low.endswith(".webm"): mime = "video/webm"
        else:                       mime = "video/mp4"
    ext = _MIME_EXT.get(mime, ".mp4")

    meta = await _sp.probe(tmp_path)
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
        try: tmp_path.unlink(missing_ok=True)
        except Exception: pass
        logger.warning("from_url rename failed u=%s d=%s: %s", user_id, draft_id, e)
        raise HTTPException(500, "draft persist failed")
    import aiosqlite
    async with aiosqlite.connect(_db.DB_PATH) as dbh:
        await dbh.execute(
            "UPDATE sticker_drafts SET file_path = ? WHERE id = ?",
            (str(final_path), draft_id),
        )
        await dbh.commit()

    logger.info("from_url OK u=%s d=%s mime=%s bytes=%d", user_id, draft_id, mime, written)
    return {
        "id":         draft_id,
        "mime_type":  mime,
        "duration_s": meta.get("duration_s"),
        "width":      meta.get("width"),
        "height":     meta.get("height"),
    }


# ── Pack rename (Telegram setStickerSetTitle) ──────────────────────────────


@router.post("/api/sticker_pack/rename")
async def rename_pack(body: RenamePackBody, request: Request,
                      kind: str | None = "video") -> dict:
    """Rename the caller's sticker pack on Telegram AND in the local DB.

    Telegram caps `set_sticker_set_title` at 64 chars; reject longer input
    locally so we don't burn a Telegram API call to learn that."""
    payload = await _mini._verify(request)
    _mini.require_scope(payload, "smdl.stickers")
    user_id = int(payload["user"]["id"])
    k = _kind_param(kind)

    new_title = (body.title or "").strip()
    if not new_title:
        raise HTTPException(400, "title is required")
    if len(new_title) > 64:
        raise HTTPException(400, "title is over 64 chars (Telegram limit)")

    pack = await _db.sticker_pack_get(user_id, k)
    if not pack:
        raise HTTPException(404, "no sticker pack to rename (make one sticker first)")

    tg_app = _require_bot()

    try:
        await tg_app.bot.set_sticker_set_title(
            name=pack["pack_name"], title=new_title,
        )
    except TelegramError as e:
        msg = str(e)
        logger.warning("set_sticker_set_title failed u=%s: %s", user_id, msg)
        raise HTTPException(502, f"Telegram: {msg}")

    await _db.sticker_pack_create(
        user_id=user_id,
        pack_name=pack["pack_name"],
        pack_title=new_title,
        telegram_url=pack.get("telegram_url") or f"https://t.me/addstickers/{pack['pack_name']}",
        kind=k,
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


async def _resolve_pack_or_404(user_id: int, kind: str = "video"):
    """Look up the caller's pack of a given kind and refuse cleanly if
    they don't have one yet. Returns the local row."""
    pack = await _db.sticker_pack_get(user_id, kind)
    if not pack:
        raise HTTPException(404, f"no {kind} sticker pack yet (make one sticker first)")
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
async def pack_contents(request: Request, kind: str | None = "video") -> dict:
    """Return the live contents of the caller's sticker pack for a given
    kind. Telegram is the SoT for which stickers actually exist in the
    set; the local `stickers` table is best-effort audit only.
    """
    payload = await _mini._verify(request)
    _mini.require_scope(payload, "smdl.stickers")
    user_id = int(payload["user"]["id"])
    k = _kind_param(kind)
    pack = await _resolve_pack_or_404(user_id, k)
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


class StickerToDraftBody(BaseModel):
    file_id: str


@router.post("/api/sticker_pack/sticker/to_draft")
async def pack_sticker_to_draft(body: StickerToDraftBody, request: Request) -> dict:
    """Re-import a published pack sticker's bytes into a fresh editable DRAFT so
    the Studio editor (/stickers/{id}/edit) can re-crop / re-trim / re-emoji /
    cut-out it. Telegram can't mutate a published sticker's bytes in place, so
    this opens an editable COPY — re-making it appends a new variant to the pack
    (the user can remove the original afterwards). Returns
    {id, mime_type, is_video, duration_s, width, height}."""
    payload = await _mini._verify(request)
    _mini.require_scope(payload, "smdl.stickers")
    user_id = int(payload["user"]["id"])
    file_id = (body.file_id or "").strip()
    if not file_id:
        raise HTTPException(400, "file_id required")
    tg_app = _require_bot()
    try:
        f = await tg_app.bot.get_file(file_id)
        data = bytes(await f.download_as_bytearray())
    except TelegramError as e:
        raise HTTPException(502, f"Telegram file fetch failed: {e}")
    if not data:
        raise HTTPException(502, "empty sticker file")
    # Detect container from magic bytes — the same formats pack_sticker_file
    # handles (webm / webp / gif / png).
    if data[:4] == b"\x1aE\xdf\xa3":              # EBML / Matroska / WebM
        mime, ext = "video/webm", ".webm"
    elif data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        mime, ext = "image/webp", ".webp"
    elif data[:6] in (b"GIF87a", b"GIF89a"):
        mime, ext = "image/gif", ".gif"
    elif data[:8] == b"\x89PNG\r\n\x1a\n":
        mime, ext = "image/png", ".png"
    else:
        mime, ext = "application/octet-stream", ".bin"
    # Insert the row first to learn the canonical id, then write the file into
    # the <id>.<ext> slot draft_path() predicts (mirrors upload_draft()).
    (DRAFTS_DIR / str(user_id)).mkdir(parents=True, exist_ok=True)
    draft_id = await _db.sticker_draft_insert(
        user_id=user_id,
        telegram_file_id=_WEB_UPLOAD_SENTINEL,
        file_path="",
        mime_type=mime,
        duration_s=None, width=None, height=None,
    )
    final_path = draft_path(user_id, draft_id, ext)
    try:
        final_path.write_bytes(data)
    except Exception as e:
        logger.warning("to_draft persist failed u=%s d=%s: %s", user_id, draft_id, e)
        raise HTTPException(500, "draft persist failed")
    # Best-effort probe now the source is on disk (the editor falls back to
    # client-side decode for dims/duration if this misses).
    meta = await _sp.probe(final_path)
    import aiosqlite
    async with aiosqlite.connect(_db.DB_PATH) as dbh:
        await dbh.execute(
            "UPDATE sticker_drafts SET file_path = ?, duration_s = ?, "
            "width = ?, height = ? WHERE id = ?",
            (str(final_path), meta.get("duration_s"), meta.get("width"),
             meta.get("height"), draft_id),
        )
        await dbh.commit()
    is_video = mime.startswith("video/") or mime == "image/gif"
    logger.info("pack sticker → draft u=%s d=%s mime=%s bytes=%d",
                user_id, draft_id, mime, len(data))
    return {
        "id":         draft_id,
        "mime_type":  mime,
        "is_video":   is_video,
        "duration_s": meta.get("duration_s"),
        "width":      meta.get("width"),
        "height":     meta.get("height"),
    }


@router.post("/api/sticker_pack/sticker/delete")
async def pack_sticker_delete(body: StickerByIdBody, request: Request,
                              kind: str | None = "video") -> dict:
    payload = await _mini._verify(request)
    _mini.require_scope(payload, "smdl.stickers")
    user_id = int(payload["user"]["id"])
    await _resolve_pack_or_404(user_id, _kind_param(kind))
    tg_app = _require_bot()
    try:
        await tg_app.bot.delete_sticker_from_set(sticker=body.file_id)
    except TelegramError as e:
        raise HTTPException(502, f"Telegram: {e}")
    return {"ok": True}


@router.post("/api/sticker_pack/sticker/position")
async def pack_sticker_position(body: StickerPositionBody, request: Request,
                                kind: str | None = "video") -> dict:
    payload = await _mini._verify(request)
    _mini.require_scope(payload, "smdl.stickers")
    user_id = int(payload["user"]["id"])
    await _resolve_pack_or_404(user_id, _kind_param(kind))
    tg_app = _require_bot()
    try:
        await tg_app.bot.set_sticker_position_in_set(
            sticker=body.file_id, position=int(body.position),
        )
    except TelegramError as e:
        raise HTTPException(502, f"Telegram: {e}")
    return {"ok": True}


@router.post("/api/sticker_pack/sticker/emojis")
async def pack_sticker_emojis(body: StickerEmojisBody, request: Request,
                              kind: str | None = "video") -> dict:
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
async def pack_sticker_keywords(body: StickerKeywordsBody, request: Request,
                                kind: str | None = "video") -> dict:
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
async def pack_sticker_set_cover(body: StickerByIdBody, request: Request,
                                  kind: str | None = "video") -> dict:
    """Use this sticker's file_id as the pack thumbnail. Telegram requires
    the thumbnail format to match the pack's sticker type."""
    payload = await _mini._verify(request)
    _mini.require_scope(payload, "smdl.stickers")
    user_id = int(payload["user"]["id"])
    pack = await _resolve_pack_or_404(user_id, _kind_param(kind))
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
async def pack_delete(body: DeletePackBody, request: Request,
                      kind: str | None = "video") -> dict:
    """Nuke the entire pack of a given kind on Telegram + drop the local
    row. Other kinds are unaffected. Irreversible — caller MUST send
    `{confirm: true}`."""
    payload = await _mini._verify(request)
    _mini.require_scope(payload, "smdl.stickers")
    user_id = int(payload["user"]["id"])
    if not body.confirm:
        raise HTTPException(400, "destructive — pass {\"confirm\": true}")
    k = _kind_param(kind)
    pack = await _resolve_pack_or_404(user_id, k)
    tg_app = _require_bot()
    try:
        await tg_app.bot.delete_sticker_set(name=pack["pack_name"])
    except TelegramError as e:
        msg = str(e).lower()
        if "not found" not in msg and "stickerset_invalid" not in msg:
            raise HTTPException(502, f"Telegram: {e}")
    # Clear local rows for THIS kind only — other packs the user owns stay.
    import aiosqlite
    async with aiosqlite.connect(_db.DB_PATH) as dbh:
        await dbh.execute(
            "DELETE FROM stickers WHERE user_id = ? AND pack_name = ?",
            (user_id, pack["pack_name"]),
        )
        await dbh.execute(
            "DELETE FROM sticker_packs WHERE user_id = ? AND pack_kind = ?",
            (user_id, k),
        )
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
  <script src="https://cdn.jsdelivr.net/npm/fabric@5.3.0/dist/fabric.min.js"></script>
  <style>
    :root { color-scheme: dark light; }
    /* ── Theme tokens (overridden per skin) ──────────────────────────── */
    body{
      --bg: var(--tg-theme-bg-color,#111);
      --text: var(--tg-theme-text-color,#eee);
      --dim: var(--tg-theme-hint-color,#999);
      --surface: transparent; --surf2:#222; --surf3:#181818;
      --border:#333;
      --accent: var(--tg-theme-button-color,#3390ec);
      --accent2:#2a8a44; --onacc:#fff;
      --radius:8px; --radius-lg:10px; --pill-radius:999px;
      --pad:0px; --shadow:none;
      font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
      margin:0;padding:12px;background:var(--bg);color:var(--text);
      touch-action:manipulation;transition:background .2s,color .2s;
    }
    /* Simple — playful, rounded, colourful */
    body.skin-playful{
      --bg:#140f1d; --text:#f6eefb; --dim:#b6a8c8;
      --surface:rgba(255,255,255,0.045); --surf2:rgba(255,255,255,0.09);
      --surf3:rgba(255,255,255,0.06); --border:rgba(255,255,255,0.14);
      --accent:#ff5fa2; --accent2:#ff5fa2; --onacc:#23071a;
      --radius:16px; --radius-lg:22px; --pill-radius:999px;
      --pad:14px; --shadow:0 6px 18px rgba(0,0,0,0.32);
      background:
        radial-gradient(1100px 560px at 0% -10%, rgba(255,95,162,0.16), transparent 60%),
        radial-gradient(900px 480px at 100% 0%, rgba(124,92,255,0.15), transparent 55%),
        var(--bg);
    }
    /* Standard — Telegram-native, flat, system theme */
    body.skin-native{
      --bg: var(--tg-theme-bg-color,#111);
      --text: var(--tg-theme-text-color,#eee);
      --dim: var(--tg-theme-hint-color,#8a8a8a);
      --surface: transparent;
      --surf2: rgba(128,128,128,0.14); --surf3: rgba(128,128,128,0.10);
      --border: rgba(128,128,128,0.26);
      --accent: var(--tg-theme-button-color,#3390ec);
      --accent2: var(--tg-theme-button-color,#3390ec);
      --onacc: var(--tg-theme-button-text-color,#fff);
      --radius:10px; --radius-lg:12px; --pill-radius:999px;
      --pad:0px; --shadow:none;
    }
    /* Pro — dark studio, cards, accent glow */
    body.skin-studio{
      --bg:#0e0e11; --text:#f2f2f6; --dim:#8b8b97;
      --surface:#16161b; --surf2:#212129; --surf3:#1a1a21;
      --border:#2a2a33;
      --accent:#7c5cff; --accent2:#7c5cff; --onacc:#fff;
      --radius:10px; --radius-lg:14px; --pill-radius:9px;
      --pad:12px; --shadow:0 2px 10px rgba(0,0,0,0.45);
    }
    h1{font-size:16px;margin:0 0 10px;}
    body.skin-studio h1{letter-spacing:.14em;text-transform:uppercase;
      font-size:13px;color:var(--dim);}
    body.skin-playful h1{font-size:19px;}
    /* Top bar: Back + mode selector on one line */
    .topbar{display:flex;align-items:center;justify-content:space-between;
      gap:10px;flex-wrap:wrap;margin-bottom:8px;}
    .topbar .back{margin:0;}
    .topbar .skin-switch{margin:0;}
    /* Skin switcher (segmented control) */
    .skin-switch{display:inline-flex;gap:3px;background:var(--surf2);
      border:1px solid var(--border);border-radius:999px;padding:3px;margin:0 0 12px;}
    .skin-switch button{font-size:12px;padding:5px 12px;border:0;border-radius:999px;
      background:transparent;color:var(--dim);cursor:pointer;white-space:nowrap;
      transition:background .12s,color .12s;}
    .skin-switch button.on{background:var(--accent);color:var(--onacc);font-weight:600;}
    /* Mode complexity — gate which sections appear:
       Simple = essentials only · Standard = + editing · Pro = everything. */
    body.skin-playful [data-tier="std"],
    body.skin-playful [data-tier="pro"],
    body.skin-native  [data-tier="pro"]{display:none !important;}
    /* Simple also hides the advanced bits INSIDE the Shape section (cutout,
       corner fill, padding, custom freehand) — it auto-uses a transparent cut. */
    body.skin-playful .adv-only{display:none !important;}
    .simple-only{display:none;}
    body.skin-playful .simple-only{display:block;}
    .video-wrap{position:relative;display:inline-block;max-width:100%;
                background:#000;border-radius:var(--radius);overflow:hidden;
                touch-action:none;}
    .video-wrap video{display:block;width:100%;max-height:50vh;}
    /* Editor canvas (left) + live result preview (right) */
    .studio-row{display:flex;gap:12px;align-items:flex-start;flex-wrap:wrap;}
    .studio-row .video-wrap{flex:0 1 auto;max-height:50vh;}
    .result-pane{flex:0 0 auto;display:flex;flex-direction:column;
                 align-items:center;}
    #result-preview{width:170px;height:170px;border-radius:var(--radius);
                    background:#0c0c0c;border:1px solid var(--border);}
    .result-cap{font-size:11px;color:var(--dim);margin-top:4px;}
    .crop-overlay{position:absolute;inset:0;pointer-events:none;}
    .crop-overlay.on{pointer-events:auto;}
    .crop-box{position:absolute;border:2px solid #fff;
              box-shadow:0 0 0 9999px rgba(0,0,0,0.45);
              box-sizing:border-box;cursor:move;
              transition:border-color .12s;}
    .crop-box.dragging{border-color:var(--accent);}
    .crop-handle{position:absolute;width:18px;height:18px;background:#fff;
                 border-radius:4px;border:1px solid #333;touch-action:none;}
    /* Invisible enlarged hit-area so corners are easy to grab on touch. */
    .crop-handle::after{content:'';position:absolute;inset:-13px;}
    .crop-handle.nw{left:-9px;top:-9px;cursor:nw-resize;}
    .crop-handle.ne{right:-9px;top:-9px;cursor:ne-resize;}
    .crop-handle.sw{left:-9px;bottom:-9px;cursor:sw-resize;}
    .crop-handle.se{right:-9px;bottom:-9px;cursor:se-resize;}
    .draw-canvas{position:absolute;inset:0;display:none;
                 touch-action:none;cursor:crosshair;}
    .draw-canvas.on{display:block;}
    /* Preset-shape preview: shown but click-through so the crop box stays draggable. */
    .draw-canvas.preview{display:block;pointer-events:none;cursor:default;}
    .section{margin-top:14px;background:var(--surface);
             border-radius:var(--radius-lg);padding:var(--pad);
             box-shadow:var(--shadow);}
    body.skin-studio .section,body.skin-playful .section{border:1px solid var(--border);}
    body.skin-native .section{margin-top:18px;}
    .section label{display:block;font-size:12px;color:var(--dim);
                    margin-bottom:6px;font-weight:600;}
    .row{display:flex;gap:8px;align-items:center;flex-wrap:wrap;}
    .meta{font-size:11px;color:var(--dim);}
    .pill{font-size:12px;background:var(--surf2);border:1px solid var(--border);
          border-radius:var(--pill-radius);padding:5px 12px;color:var(--text);
          cursor:pointer;user-select:none;
          transition:transform .08s,background .12s,border-color .12s;}
    .pill:active{transform:scale(.94);}
    .pill.on{background:var(--accent2);border-color:var(--accent2);color:var(--onacc);}
    body.skin-studio .pill.on{box-shadow:0 0 0 1px var(--accent),0 0 12px -3px var(--accent);}
    /* Scrubber timeline */
    .timeline{position:relative;height:42px;margin-top:6px;user-select:none;
              touch-action:none;}
    .timeline-track{position:absolute;left:0;right:0;top:18px;height:6px;
                    background:var(--surf2);border-radius:3px;}
    .timeline-range{position:absolute;top:0;bottom:0;
                    background:rgba(255,255,255,0.12);
                    border:1px solid var(--accent);border-radius:3px;}
    .timeline-cursor{position:absolute;top:-6px;width:2px;height:18px;
                     background:var(--text);pointer-events:none;}
    .timeline-handle{position:absolute;top:6px;width:18px;height:30px;
                     background:var(--accent);border-radius:6px;
                     transform:translateX(-50%);cursor:ew-resize;
                     box-shadow:0 1px 3px rgba(0,0,0,0.7);
                     display:flex;align-items:center;justify-content:center;
                     font-size:10px;color:var(--onacc);}
    .timeline-handle.dragging{background:var(--text);}
    .timeline-labels{display:flex;justify-content:space-between;
                     font-size:11px;color:var(--dim);
                     font-variant-numeric:tabular-nums;}
    .emojirow{display:flex;gap:6px;flex-wrap:wrap;}
    .emojirow button{font-size:22px;padding:6px 10px;background:var(--surf2);
                     border:1px solid var(--border);border-radius:var(--radius);
                     color:var(--text);cursor:pointer;}
    .emojirow button.active{background:var(--accent);border-color:var(--accent);
                     color:var(--onacc);}
    input[type=text]{padding:6px 8px;border-radius:var(--radius);
                     border:1px solid var(--border);background:var(--surf3);
                     color:var(--text);font-size:18px;width:80px;}
    button.action{font-size:13px;padding:8px 14px;background:var(--surf2);
                  border:1px solid var(--border);color:var(--text);
                  border-radius:var(--radius);cursor:pointer;transition:filter .12s;}
    button.action:disabled{opacity:.5;cursor:default;}
    button.action:hover{filter:brightness(1.14);}
    #make-btn{margin-top:16px;font-size:16px;padding:13px 20px;width:100%;
              background:var(--accent);color:var(--onacc);border:0;
              border-radius:var(--radius-lg);cursor:pointer;font-weight:600;}
    body.skin-studio #make-btn{box-shadow:0 0 18px -4px var(--accent);}
    body.skin-playful #make-btn{padding:15px 20px;font-size:17px;
              box-shadow:0 8px 22px -7px var(--accent);}
    #make-btn:disabled{opacity:.6;cursor:wait;}
    #progress{margin-top:10px;font-size:13px;color:var(--dim);}
    .back{display:inline-block;margin-bottom:8px;color:var(--accent);cursor:pointer;}
    /* 🎨 Studio (Fabric.js compositing) — canvas pinned in the dock */
    #studio-stage{justify-content:center;background:#000;
                  border-radius:var(--radius);padding:8px;}
    body.studio-active #studio-stage{display:flex;}
    #studio-stage .canvas-container{touch-action:none;}
    #studio-stage canvas{touch-action:none;border-radius:4px;}
    /* Video drafts: the live preview plays BEHIND a transparent Fabric canvas
       so Text/Image/Stickers animate (last known working). Static drafts keep
       the opaque base. (Emoji + the Configure rail use the video-wrap view.) */
    #studio-frame{position:relative;line-height:0;}
    #studio-frame .canvas-container{position:relative;z-index:1;}
    #studio-vid{position:absolute;inset:0;width:100%;height:100%;object-fit:cover;
                border-radius:4px;z-index:0;display:none;background:#000;}
    body.studio-video #studio-vid{display:block;}
    #studio-panel select,#studio-panel input[type=text]{padding:6px 8px;
        border-radius:var(--radius);border:1px solid var(--border);
        background:var(--surf3);color:var(--text);font-size:14px;}
    #studio-panel input[type=color]{width:30px;height:30px;padding:0;border:0;
        background:none;border-radius:6px;cursor:pointer;}
    #studio-export-btn{margin-top:10px;width:100%;background:var(--accent);
        border:1px solid var(--accent);color:var(--onacc);font-size:15px;
        padding:11px;border-radius:var(--radius-lg);cursor:pointer;font-weight:600;}
    #studio-export-btn:disabled{opacity:.6;cursor:wait;}
    /* ── App shell: sticky preview · swappable tool panels · fixed tab bar ── */
    body{padding-bottom:150px;}                 /* clear the fixed toolbar */
    .preview-dock{position:sticky;top:0;z-index:40;background:var(--bg);
      padding:2px 0 8px;}
    .preview-dock .video-wrap{max-height:36vh;}
    .preview-dock .video-wrap video{max-height:36vh;width:auto;max-width:100%;}
    .preview-dock #result-preview{width:150px;height:150px;}
    .tool-panel{display:none;}
    body[data-tab="trim"]    [data-panel="trim"],
    body[data-tab="crop"]    [data-panel="crop"],
    body[data-tab="shape"]   [data-panel="shape"],
    body[data-tab="emoji"]   [data-panel="emoji"],
    body[data-tab="text"]    [data-panel="text"],
    body[data-tab="image"]   [data-panel="image"],
    body[data-tab="cutout"]  [data-panel="cutout"],
    body[data-tab="outline"] [data-panel="outline"],
    body[data-tab="layers"]  [data-panel="layers"],
    body[data-tab="assets"]  [data-panel="assets"]{display:block;}
    /* Studio tools pin the compositor canvas in the dock (and hide the video). */
    #studio-stage{display:none;}
    body.studio-active .studio-row{display:none;}
    body.studio-active #studio-stage{display:flex;}
    .toolbar{position:fixed;left:0;right:0;bottom:0;z-index:50;background:var(--bg);
      border-top:1px solid var(--border);padding:8px 12px;
      box-shadow:0 -4px 18px rgba(0,0,0,0.35);}
    .tabs{display:flex;gap:5px;overflow-x:auto;margin-bottom:8px;
      scrollbar-width:none;}
    .tabs::-webkit-scrollbar{display:none;}
    .tabs button{flex:1 0 auto;min-width:56px;font-size:12px;padding:8px 8px;
      border-radius:var(--radius);background:var(--surf2);border:1px solid var(--border);
      color:var(--text);cursor:pointer;white-space:nowrap;
      transition:background .12s;}
    .tabs button.on{background:var(--accent);border-color:var(--accent);
      color:var(--onacc);font-weight:600;}
    .toolbar #make-btn{margin-top:0;}
    .toolbar #progress{margin:0 0 6px;}
    /* Tool area: setup rail (Pro) + the active tool's controls, fixed height. */
    .tool-area{display:flex;gap:8px;align-items:stretch;}
    .tool-panels{flex:1 1 auto;min-width:0;height:30vh;overflow-y:auto;
      -webkit-overflow-scrolling:touch;order:1;}
    /* Configure rail sits on the RIGHT of the tool dock (order:2). */
    .setup-rail{display:none;flex-direction:column;gap:6px;flex:0 0 auto;order:2;}
    body.skin-studio .setup-rail{display:flex;}
    .setup-rail button{width:34px;height:34px;font-size:14px;line-height:1;
      padding:0;border-radius:var(--radius);background:var(--surf2);
      border:1px solid var(--border);color:var(--text);cursor:pointer;}
    .setup-rail button.on{background:var(--accent);border-color:var(--accent);
      color:var(--onacc);box-shadow:0 0 12px -3px var(--accent);}
    /* In Pro the global configurators live on the right rail → drop them from
       the bottom bar, which keeps only the ADDITIVE tools (Emoji · Text ·
       Image — and future GIF / sticker-on-sticker). */
    body.skin-studio #tool-tabs button[data-tab="trim"],
    body.skin-studio #tool-tabs button[data-tab="crop"],
    body.skin-studio #tool-tabs button[data-tab="shape"],
    body.skin-studio #tool-tabs button[data-tab="cutout"],
    body.skin-studio #tool-tabs button[data-tab="outline"],
    body.skin-studio #tool-tabs button[data-tab="layers"]{display:none;}
    /* ── 2-mode (Pro): explicit Studio (layers) vs Layout (framing the source) ── */
    .mode-switch{display:none;justify-content:flex-end;gap:5px;margin:8px 12px 2px;}
    body.skin-studio .mode-switch{display:flex;}
    .mode-switch button{font-size:12px;padding:6px 18px;border:1px solid var(--border);
      border-radius:999px;background:var(--surf2);color:var(--text);cursor:pointer;}
    .mode-switch button.on{background:var(--accent);color:var(--onacc);
      border-color:var(--accent);font-weight:600;}
    /* One tool bar, switched by mode — fold the right rail into the bottom bar. */
    body.skin-studio.pmode-studio .setup-rail,
    body.skin-studio.pmode-layout .setup-rail{display:none;}
    body.skin-studio.pmode-studio #tool-tabs button.layout-tool{display:none!important;}
    body.skin-studio.pmode-studio #tool-tabs button.studio-tool{display:inline-block!important;}
    body.skin-studio.pmode-layout #tool-tabs button.studio-tool{display:none!important;}
    body.skin-studio.pmode-layout #tool-tabs button.layout-tool{display:inline-block!important;}
    /* Layout = one big framing canvas + a SMALL floating "Live result" thumbnail
       (PiP, bottom-right of the video) so the composite stays visible while you
       frame, without pushing the layout around. */
    body.skin-studio.pmode-layout .video-wrap,
    body.skin-studio.pmode-layout .video-wrap video{max-height:54vh;}
    body.skin-studio.pmode-layout .studio-row{position:relative;}
    body.skin-studio.pmode-layout .result-pane{position:absolute;right:8px;bottom:8px;
      z-index:5;margin:0;padding:0;width:auto;}
    body.skin-studio.pmode-layout #result-preview{width:88px!important;height:88px!important;
      border:2px solid var(--accent);border-radius:8px;box-shadow:0 2px 12px rgba(0,0,0,.55);}
    body.skin-studio.pmode-layout .result-cap{display:none;}
  </style>
</head>
<body>
  <div class="topbar">
    <div class="back" onclick="location.href='/app?tab=stickers'">← Back</div>
    <div class="skin-switch" id="skin-switch">
      <button data-skin="playful">😊 Simple</button>
      <button data-skin="native">✈️ Standard</button>
      <button data-skin="studio">⚡ Pro</button>
    </div>
  </div>

  <div class="mode-switch" id="mode-switch">
    <button data-mode="layout">✂️ Layout</button>
    <button data-mode="studio">🎨 Studio</button>
  </div>

  <div class="preview-dock">
    <div class="studio-row">
      <div class="video-wrap" id="video-wrap">
        <video id="vid" muted playsinline preload="auto"></video>
        <div class="crop-overlay" id="crop-overlay">
          <div class="crop-box" id="crop-box">
            <div class="crop-handle nw" data-h="nw"></div>
            <div class="crop-handle ne" data-h="ne"></div>
            <div class="crop-handle sw" data-h="sw"></div>
            <div class="crop-handle se" data-h="se"></div>
          </div>
        </div>
        <canvas class="draw-canvas" id="draw-canvas"></canvas>
      </div>
      <div class="result-pane" data-tier="std">
        <canvas id="result-preview" width="256" height="256"></canvas>
        <div class="result-cap">▶ Live result (your layers)</div>
      </div>
    </div>
    <div id="studio-stage" data-tier="pro">
      <div id="studio-frame">
        <video id="studio-vid" muted playsinline loop preload="auto"></video>
        <canvas id="studio-canvas" width="320" height="320"></canvas>
      </div>
    </div>
    <div class="meta simple-only" style="margin-top:6px">
      ✨ Pick a shape &amp; emoji below, then Make. Want trim &amp; crop? Switch to <b>⚡ Pro</b>.
    </div>
  </div>

  <div class="tool-area">
    <!-- Setup rail (Pro only): video-sticker tools, icon-only, left of the controls -->
    <!-- Configure rail (Pro): global modifiers that change the whole sticker /
         manage the stack — Shape · Crop · Clip · Cutout · Outline · Layers.
         The additive tools (Emoji · Text · Image) stay on the bottom studio
         bar. 2026-06-10 studio-bar split. -->
    <div class="setup-rail" id="setup-rail">
      <button data-tab="shape"   title="Shape / frame">◯</button>
      <button data-tab="crop"    data-tier="std" title="Crop">⛶</button>
      <button data-tab="trim"    data-tier="std" title="Clip — trim to 3s">✂️</button>
      <button data-tab="cutout"  title="Cutout — remove background">🪄</button>
      <button data-tab="outline" title="Outline — die-cut border">🔲</button>
      <button data-tab="layers"  title="Layers — undo / reorder">▤</button>
    </div>
    <div class="tool-panels">
  <div class="section tool-panel" data-panel="trim" data-tier="std">
    <label>Pick your 3 seconds <span class="meta">(Telegram caps video stickers at 3s)</span></label>
    <div class="timeline" id="timeline">
      <div class="timeline-track">
        <div class="timeline-range" id="timeline-range"></div>
      </div>
      <div class="timeline-cursor" id="timeline-cursor" style="left:0"></div>
      <div class="timeline-handle" id="handle-start" style="left:0%">⟨</div>
      <div class="timeline-handle" id="handle-end" style="left:100%">⟩</div>
    </div>
    <div class="timeline-labels">
      <span id="t-start-l">0.0s</span>
      <span id="t-cur-l" style="color:#fff">—</span>
      <span id="t-end-l">3.0s</span>
    </div>
    <div class="row" style="margin-top:8px">
      <button class="action" id="smart-trim-btn">🎯 Smart trim</button>
      <button class="action" id="preview-btn">⏸ Pause</button>
      <span class="meta" id="smart-trim-status"></span>
    </div>
  </div>

  <div class="section tool-panel" data-panel="crop" data-tier="std">
    <label>Crop</label>
    <div class="row">
      <span class="pill" id="crop-mode-toggle">Fixed</span>
      <span class="pill on" id="aspect-lock-toggle">🔒 Square</span>
      <span class="meta">Toggle "Unfixed" to move &amp; resize a box on the video.</span>
    </div>
    <div class="row" id="crop-size-row" style="display:none;margin-top:8px;align-items:center;gap:8px">
      <span class="meta" style="min-width:34px">Size</span>
      <input type="range" id="crop-size" min="20" max="100" value="60" style="flex:1;accent-color:var(--accent)">
    </div>
    <div class="meta" id="crop-mode-hint" style="margin-top:6px">Fixed: the centre of the video is auto-cropped square into the sticker.</div>
  </div>

  <div class="section tool-panel" data-panel="shape" id="shape-section">
    <label id="shape-label">Shape <span class="meta">(tap one — Simple keeps a clean transparent cut)</span></label>
    <div class="row" id="shape-row">
      <span class="pill on" data-shape="square">▢ Square</span>
      <span class="pill" data-shape="circle">◯ Circle</span>
      <span class="pill" data-shape="triangle">△ Triangle</span>
      <span class="pill" data-shape="star">⭐ Star</span>
      <span class="pill" data-shape="heart">♥ Heart</span>
      <span class="pill" data-shape="diamond">◆ Diamond</span>
      <span class="pill adv-only" data-shape="custom">✎ Custom</span>
    </div>
    <div class="row adv-only" style="margin-top:8px">
      <span class="pill" id="cutout-toggle">✂️ Remove background</span>
      <span class="pill" id="hq-toggle" style="display:none" title="Heavier matte model + temporal de-flicker — cleaner edges, a little slower">✨ Best quality</span>
      <span class="pill" id="draw-clear" style="display:none">↺ Redraw</span>
    </div>
    <div class="row adv-only" id="fill-row" style="margin-top:8px;display:none">
      <span class="meta">Corners:</span>
      <span class="pill on" data-fill="blur">🌫 Blur</span>
      <span class="pill" data-fill="color">🎨 Colour</span>
      <span class="pill" data-fill="transparent">⬚ Transparent</span>
      <input type="color" id="fill-color" value="#ffffff" style="display:none">
    </div>
    <div class="row adv-only" id="pad-row" style="margin-top:8px;display:none">
      <span class="pill" id="pad-toggle">⊡ Padding</span>
      <span class="meta">float the shape free of the frame edges</span>
    </div>
    <div class="meta adv-only" id="shape-hint" style="margin-top:6px">Square = the full (cropped) frame.</div>
  </div>

  <div class="section tool-panel" data-panel="emoji">
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

  <!-- Studio compositor — split into bottom-nav tools (canvas lives in the dock). -->
  <div class="section tool-panel" data-panel="text" data-tier="pro" id="studio-section">
    <label>Text &amp; emoji layers</label>
    <div class="row">
      <input id="studio-text" type="text" maxlength="60" placeholder="Caption…"
             style="flex:1;min-width:120px;width:auto;font-size:15px">
      <button class="action" id="studio-add-text">➕ Add text</button>
    </div>
    <div class="row" style="margin-top:8px">
      <span class="meta">Fill</span><input type="color" id="studio-fill" value="#ffffff">
      <span class="meta">Outline</span><input type="color" id="studio-stroke" value="#000000">
      <span class="pill" id="studio-bold">𝐁 Bold</span>
      <select id="studio-font" title="Font">
        <option value="Impact" selected>Impact</option>
        <option value="Arial">Arial</option>
        <option value="Georgia">Georgia</option>
        <option value="'Comic Sans MS',cursive">Comic</option>
        <option value="'Courier New',monospace">Mono</option>
      </select>
    </div>
    <div class="row" style="margin-top:8px">
      <input id="studio-emoji" type="text" maxlength="8" placeholder="😀"
             style="width:54px;text-align:center;font-size:18px">
      <button class="action" id="studio-add-emoji">➕ Add emoji</button>
    </div>
  </div>

  <div class="section tool-panel" data-panel="image" data-tier="pro">
    <label>Image overlay</label>
    <div class="row">
      <label class="action" for="studio-img" style="cursor:pointer">🖼 Upload image…</label>
      <input id="studio-img" type="file" accept="image/*" style="display:none">
      <span class="meta">Drop a logo or photo onto the canvas.</span>
    </div>
  </div>

  <div class="section tool-panel" data-panel="assets" data-tier="pro">
    <label>Stickers &amp; assets <span class="meta">(transparent — tap to drop a layer)</span></label>
    <div class="row">
      <input id="asset-q" type="text" placeholder="Search stickers — cat, fire, heart…"
             style="flex:1;min-width:120px;width:auto">
      <button class="action" id="asset-go">🔎</button>
    </div>
    <div id="asset-grid" style="display:grid;grid-template-columns:repeat(auto-fill,minmax(72px,1fr));gap:6px;margin-top:8px"></div>
    <div class="meta" id="asset-status" style="margin-top:6px">Powered by GIPHY stickers · more sources soon.</div>
  </div>

  <div class="section tool-panel" data-panel="cutout" data-tier="pro">
    <label>Cut out subject</label>
    <div class="row">
      <button class="action" id="studio-cutout">✂️ Remove background</button>
      <span class="meta">Isolate the person/object from the base frame.</span>
    </div>
  </div>

  <div class="section tool-panel" data-panel="outline" data-tier="pro">
    <label>Die-cut outline</label>
    <div class="row">
      <span class="pill" id="studio-outline-toggle">🔲 Outline</span>
      <input type="color" id="studio-outline-color" value="#ffffff" title="Outline colour">
      <span class="meta">Width</span>
      <input type="range" id="studio-outline-width" min="4" max="32" value="12"
             style="flex:1;min-width:70px">
      <span class="meta" id="studio-outline-w-l">12px</span>
    </div>
    <div class="meta" id="studio-outline-hint" style="margin-top:4px">
      Outline hugs transparent edges — pairs with “Cut out subject”.
    </div>
  </div>

  <div class="section tool-panel" data-panel="layers" data-tier="pro">
    <label>Layers</label>
    <div class="row">
      <button class="action" id="studio-undo">↶ Undo</button>
      <button class="action" id="studio-redo">↷ Redo</button>
      <button class="action" id="studio-del">🗑 Delete</button>
      <button class="action" id="studio-front">⬆ Front</button>
      <button class="action" id="studio-back">⬇ Back</button>
    </div>
  </div>

    </div>
  </div>

  <div class="toolbar">
    <div id="progress"></div>
    <div class="tabs" id="tool-tabs">
      <button data-tab="trim"  data-tier="std" class="layout-tool">✂️ Clip</button>
      <button data-tab="crop"  data-tier="std" class="layout-tool">⛶ Crop</button>
      <button data-tab="shape" class="layout-tool">◯ Shape</button>
      <button data-tab="emoji" class="studio-tool">😀 Emoji</button>
      <button data-tab="text"    data-tier="pro" class="studio-tool">➕ Text</button>
      <button data-tab="image"   data-tier="pro" class="studio-tool">🖼 Image</button>
      <button data-tab="assets"  data-tier="pro" class="studio-tool">✨ Stickers</button>
      <button data-tab="cutout"  data-tier="pro" class="layout-tool">✂️ Cutout</button>
      <button data-tab="outline" data-tier="pro" class="studio-tool">🔲 Outline</button>
      <button data-tab="layers"  data-tier="pro" class="studio-tool">▤ Layers</button>
    </div>
    <button id="make-btn">✨ Make sticker</button>
  </div>

<script>
const tg = window.Telegram?.WebApp;
if (tg) { tg.ready(); tg.expand(); }
// Telegram BackButton — one Android system-back press returns to the Stickers
// tab instead of TG minimising the whole Mini App from this editor sub-page.
if (tg && tg.BackButton) {
  try {
    tg.BackButton.show();
    tg.BackButton.onClick(function () {
      if (window.history.length > 1) window.history.back();
      else location.href = '/app?tab=stickers';
    });
  } catch (e) {}
}
const initData = tg?.initData || '';

const DRAFT_ID = {{DRAFT_ID}};

// ── Skin (Simple / Standard / Pro) — remembered per device ────────────────
const SKINS = ['playful', 'native', 'studio'];
function applySkin(s) {
  if (!SKINS.includes(s)) s = 'native';
  document.body.classList.remove('skin-playful', 'skin-native', 'skin-studio');
  document.body.classList.add('skin-' + s);
  document.querySelectorAll('#skin-switch button').forEach(
    b => b.classList.toggle('on', b.dataset.skin === s));
  try { localStorage.setItem('smdl_sticker_skin', s); } catch (e) {}
}
document.getElementById('skin-switch').addEventListener('click', e => {
  const b = e.target.closest('button[data-skin]');
  if (!b) return;
  applySkin(b.dataset.skin);
  // Pro uses the 2-mode (Studio/Layout) tool split; other tiers use one row.
  if (b.dataset.skin === 'studio') {
    setMode((() => { try { return localStorage.getItem('smdl_sticker_pmode'); } catch (e) { return null; } })() || 'studio');
  } else {
    setTab(document.body.dataset.tab || 'shape');
  }
  try { _refreshShapePreview(); } catch (_) {}
});
applySkin((() => { try { return localStorage.getItem('smdl_sticker_skin'); }
                   catch (e) { return null; } })() || 'playful');

// ── Tool tabs (sticky preview · bottom tab bar) ───────────────────────────
// The Studio compositor's tools live in the same scrollable row; selecting one
// pins the canvas in the dock and turns the action button into Export.
const STUDIO_TABS = ['emoji', 'text', 'image', 'cutout', 'outline', 'layers', 'assets'];
const _TAB_SEL = '#tool-tabs [data-tab], #setup-rail [data-tab]';
function _tabVisible(tab) {
  // visible if ANY copy of the tab (bottom bar OR the Pro setup rail) is shown
  return Array.from(document.querySelectorAll(
    '#tool-tabs [data-tab="' + tab + '"], #setup-rail [data-tab="' + tab + '"]'))
    .some(b => getComputedStyle(b).display !== 'none');
}
function setTab(tab) {
  if (!_tabVisible(tab)) tab = 'shape';   // Shape is in every tier
  document.body.dataset.tab = tab;
  document.querySelectorAll(_TAB_SEL).forEach(
    b => b.classList.toggle('on', b.dataset.tab === tab));
  // Video cutout is animated (per-frame /make), NOT a static Studio composite —
  // so the Cutout tab on a video must not flip into studio/export mode.
  const studio = STUDIO_TABS.includes(tab)
              && !(tab === 'cutout' && _editorPackKind === 'video');
  document.body.classList.toggle('studio-active', studio);   // dock → canvas
  const mk = document.getElementById('make-btn');
  // Video drafts export an ANIMATED sticker (overlay composited onto every
  // frame, v2.7-A); static drafts a flat one. Badge the button accordingly.
  if (mk) mk.textContent = studio
    ? (_editorPackKind === 'video' ? '🎬 Export animated' : '📤 Export sticker')
    : '✨ Make sticker';
  if (studio) {
    try { ensureStudio(); } catch (_) {}                     // build canvas once
    if (_isVideoKind) {                                       // keep the stacked live video rolling
      const sv = document.getElementById('studio-vid');
      if (sv) sv.play().catch(() => {});
    }
  }
  // On the Cutout tab for video, mirror the cutout flag so the main Make
  // button produces the same animated per-frame cutout as the in-panel button.
  if (tab === 'cutout' && _editorPackKind === 'video') {
    cutout = true;
    if (cutoutToggle) cutoutToggle.classList.add('on');
    if (hqToggle) hqToggle.style.display = '';   // expose "best quality"
  }
  // Lazy-load trending stickers the first time the Assets tab is opened.
  if (tab === 'assets' && !_assetInit) { _assetInit = true; try { assetSearch(); } catch (_) {} }
}
document.getElementById('tool-tabs').addEventListener('click', e => {
  const b = e.target.closest('button[data-tab]');
  if (b) setTab(b.dataset.tab);
});
document.getElementById('setup-rail').addEventListener('click', e => {
  const b = e.target.closest('button[data-tab]');
  if (b) setTab(b.dataset.tab);
});
setTab('shape');

// ── 2-mode (Pro): Studio = compositing layers · Layout = framing the source ──
// Explicit modes so the two surfaces (square compositor vs full-frame video)
// aren't confusingly swapped per-tab. The surface follows the chosen tool, so
// each mode lands on its own tool set + a sensible default tab.
const STUDIO_MODE_TABS = ['emoji', 'text', 'image', 'assets', 'outline', 'layers'];
const LAYOUT_MODE_TABS = ['trim', 'crop', 'shape', 'cutout'];
let _pmode = 'studio';
function setMode(m) {
  _pmode = (m === 'layout') ? 'layout' : 'studio';
  document.body.classList.toggle('pmode-studio', _pmode === 'studio');
  document.body.classList.toggle('pmode-layout', _pmode === 'layout');
  document.querySelectorAll('#mode-switch button').forEach(
    b => b.classList.toggle('on', b.dataset.mode === _pmode));
  try { localStorage.setItem('smdl_sticker_pmode', _pmode); } catch (e) {}
  const tabs = (_pmode === 'studio') ? STUDIO_MODE_TABS : LAYOUT_MODE_TABS;
  const cur = document.body.dataset.tab;
  setTab(tabs.includes(cur) ? cur : tabs[0]);
}
document.getElementById('mode-switch').addEventListener('click', e => {
  const b = e.target.closest('button[data-mode]');
  if (b) setMode(b.dataset.mode);
});
// If we booted straight into Pro, land in the remembered/default mode.
if (document.body.classList.contains('skin-studio')) {
  setMode((() => { try { return localStorage.getItem('smdl_sticker_pmode'); } catch (e) { return null; } })() || 'studio');
}

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
const wrap = document.getElementById('video-wrap');
const tStartL = document.getElementById('t-start-l');
const tEndL   = document.getElementById('t-end-l');
const tCurL   = document.getElementById('t-cur-l');
const emojiGrid = document.getElementById('emoji-grid');
const emojiCustom = document.getElementById('emoji-custom');
const makeBtn = document.getElementById('make-btn');
const progressEl = document.getElementById('progress');

let chosenEmoji = '🎬';
let _previewBlobUrl = null;   // same-origin blob URL of the preview — Studio base layer
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
    _previewBlobUrl = URL.createObjectURL(blob);
    vid.src = _previewBlobUrl;
  } catch (e) {
    progressEl.textContent = '❌ Preview load failed: ' + e.message;
  }
})();

// ── Scrubber state ──────────────────────────────────────────────────────
const MAX_TRIM_S = 3.0;
let videoDur = 0;
let trimStart = 0;
let trimEnd   = MAX_TRIM_S;

const timeline = document.getElementById('timeline');
const handleStart = document.getElementById('handle-start');
const handleEnd   = document.getElementById('handle-end');
const tRange      = document.getElementById('timeline-range');
const tCursor     = document.getElementById('timeline-cursor');

function _fmt(t) { return (Math.max(0, t)).toFixed(1) + 's'; }

function syncScrubberUI() {
  const w = timeline.clientWidth || 1;
  const pStart = videoDur ? (trimStart / videoDur) : 0;
  const pEnd   = videoDur ? (trimEnd   / videoDur) : 1;
  handleStart.style.left = (pStart * 100) + '%';
  handleEnd.style.left   = (pEnd   * 100) + '%';
  tRange.style.left  = (pStart * 100) + '%';
  tRange.style.width = ((pEnd - pStart) * 100) + '%';
  tStartL.textContent = _fmt(trimStart);
  tEndL.textContent   = _fmt(trimEnd);
}

function setTrim(s, e, opts) {
  if (videoDur <= 0) { trimStart = s; trimEnd = e; syncScrubberUI(); return; }
  s = Math.max(0, Math.min(s, videoDur));
  e = Math.max(0, Math.min(e, videoDur));
  if (e < s) { e = s; }
  // Clamp the window to MAX_TRIM_S — when the user is dragging the start
  // and the window would exceed, push the end with it. Same in reverse.
  if (e - s > MAX_TRIM_S) {
    if (opts && opts.movingEnd) s = e - MAX_TRIM_S;
    else e = s + MAX_TRIM_S;
  }
  trimStart = s; trimEnd = e;
  syncScrubberUI();
  if (opts && opts.seekVideo !== false) {
    try { vid.currentTime = (opts && opts.movingEnd) ? trimEnd : trimStart; }
    catch (err) {}
  }
}

// Gently loop the trim window (muted) so the editor AND the result preview
// stay live — the "mini studio" feel. Muted+playsinline → autoplay allowed.
let _autoLoop = true;
function _startAutoLoop() {
  if (!_autoLoop) return;
  try { vid.currentTime = trimStart; } catch (e) {}
  const pr = vid.play(); if (pr && pr.catch) pr.catch(() => {});
}

vid.addEventListener('loadedmetadata', () => {
  videoDur = vid.duration || 0;
  // Initial window: [0, min(3, dur)].
  setTrim(0, Math.min(MAX_TRIM_S, videoDur), { seekVideo: false });
  // Mobile WebViews leave the <video> black until the first frame is decoded
  // AND painted — which only happens on a seek or play. Nudge currentTime just
  // off zero to force a first-frame paint, then start the loop.
  try { vid.currentTime = videoDur ? Math.min(0.04, videoDur / 2) : 0.04; }
  catch (e) {}
  _startAutoLoop();
});

vid.addEventListener('timeupdate', () => {
  if (!videoDur) return;
  const p = vid.currentTime / videoDur;
  tCursor.style.left = (p * 100) + '%';
  tCurL.textContent = _fmt(vid.currentTime);
  // Loop the window — but never fight an active scrub (trim handle or crop box).
  if (_autoLoop && !_dragHandle && !_boxDrag && !_handleDrag &&
      (vid.currentTime >= trimEnd - 0.03 || vid.currentTime < trimStart - 0.15)) {
    try { vid.currentTime = trimStart; } catch (e) {}
  }
});

let _dragHandle = null;
function _xToTime(clientX) {
  const r = timeline.getBoundingClientRect();
  const x = Math.max(0, Math.min(clientX - r.left, r.width));
  return (x / r.width) * videoDur;
}
function _startHandleDrag(which, ev) {
  ev.preventDefault();
  _dragHandle = which;
  (which === 'start' ? handleStart : handleEnd).classList.add('dragging');
  try { (which === 'start' ? handleStart : handleEnd).setPointerCapture(ev.pointerId); } catch (e) {}
}
function _moveHandleDrag(ev) {
  if (!_dragHandle) return;
  const t = _xToTime(ev.clientX);
  if (_dragHandle === 'start') setTrim(t, trimEnd, { movingEnd: false });
  else                          setTrim(trimStart, t, { movingEnd: true });
}
function _endHandleDrag(ev) {
  if (!_dragHandle) return;
  (_dragHandle === 'start' ? handleStart : handleEnd).classList.remove('dragging');
  _dragHandle = null;
}
handleStart.addEventListener('pointerdown', e => _startHandleDrag('start', e));
handleEnd.addEventListener('pointerdown',   e => _startHandleDrag('end',   e));
window.addEventListener('pointermove', _moveHandleDrag);
window.addEventListener('pointerup',   _endHandleDrag);
window.addEventListener('pointercancel', _endHandleDrag);

// Tap on the empty part of the track to seek the video (handy for preview).
timeline.addEventListener('click', e => {
  if (_dragHandle) return;
  if (e.target.classList.contains('timeline-handle')) return;
  try { vid.currentTime = _xToTime(e.clientX); } catch (err) {}
});

// Play / pause the looping preview.
document.getElementById('preview-btn').addEventListener('click', () => {
  const btn = document.getElementById('preview-btn');
  _autoLoop = !_autoLoop;
  if (_autoLoop) { btn.textContent = '⏸ Pause'; _startAutoLoop(); }
  else { btn.textContent = '▶ Play'; try { vid.pause(); } catch (e) {} }
});

// Smart trim — backend ffmpeg scene-detect picks a 3-second window.
document.getElementById('smart-trim-btn').addEventListener('click', async () => {
  const btn = document.getElementById('smart-trim-btn');
  const status = document.getElementById('smart-trim-status');
  btn.disabled = true; status.textContent = 'analysing…';
  try {
    const r = await api(`/api/sticker_drafts/${DRAFT_ID}/best_window?target=${MAX_TRIM_S}`);
    setTrim(+r.start, +r.end);
    status.textContent = `picked ${r.start.toFixed(1)}–${r.end.toFixed(1)}s`;
  } catch (e) {
    status.textContent = '✗ ' + e.message;
  } finally { btn.disabled = false; }
});

// ── Crop overlay state ──────────────────────────────────────────────────
const cropOverlay = document.getElementById('crop-overlay');
const cropBox     = document.getElementById('crop-box');
const cropModeToggle = document.getElementById('crop-mode-toggle');
const aspectLockToggle = document.getElementById('aspect-lock-toggle');
const cropHint    = document.getElementById('crop-mode-hint');
const cropSizeRow = document.getElementById('crop-size-row');
const cropSize    = document.getElementById('crop-size');

let cropMode = false;     // false = center-fill (no client crop), true = pick region
let aspectLock = true;    // 1:1 default
// Box position in *displayed pixels* — converted to source pixels on submit.
let cropX = 0, cropY = 0, cropW = 0, cropH = 0;

function refreshCropOverlay() {
  cropOverlay.classList.toggle('on', cropMode);
  cropBox.style.display = cropMode ? '' : 'none';
  if (cropSizeRow) cropSizeRow.style.display = cropMode ? 'flex' : 'none';
  if (!cropMode) {
    cropHint.textContent = 'Fixed: the centre of the video is auto-cropped square into the sticker.';
    return;
  }
  cropHint.textContent = aspectLock
    ? 'Unfixed (square): drag to move, use the Size bar to resize.'
    : 'Unfixed (free): drag to move, Size bar (or corners) to resize.';
  // First-time positioning: center the box at 60% of the SHORTER displayed dim.
  if (cropW === 0 || cropH === 0) {
    const vr = vid.getBoundingClientRect();
    const wr = wrap.getBoundingClientRect();
    if (!vr.width || !vr.height) return;
    const short = Math.min(vr.width, vr.height) * 0.6;
    cropW = aspectLock ? short : vr.width * 0.6;
    cropH = aspectLock ? short : vr.height * 0.6;
    cropX = (vr.width  - cropW) / 2 + (vr.left - wr.left);
    cropY = (vr.height - cropH) / 2 + (vr.top  - wr.top);
  }
  syncCropSizeSlider();
  drawCropBox();
}

// Keep the external "Size" bar in sync with the box's current size.
function syncCropSizeSlider() {
  if (!cropSize) return;
  const vr = vid.getBoundingClientRect();
  if (!vr.width || !vr.height) return;
  const ref   = aspectLock ? Math.min(cropW, cropH) : cropW;
  const denom = aspectLock ? Math.min(vr.width, vr.height) : vr.width;
  if (denom) cropSize.value = Math.round(Math.max(20, Math.min(100, (ref / denom) * 100)));
}

// External "Size" bar — resize the crop box around its centre (the easy,
// touch-friendly alternative to grabbing the corner handles on the image).
function _applyCropSize() {
  if (!cropMode || !cropSize) return;
  const vr = vid.getBoundingClientRect();
  if (!vr.width || !vr.height) return;
  const cx = cropX + cropW / 2, cy = cropY + cropH / 2;
  const frac = (parseFloat(cropSize.value) || 60) / 100;
  if (aspectLock) {
    const s = Math.max(40, Math.min(vr.width, vr.height) * frac);
    cropW = s; cropH = s;
  } else {
    cropW = Math.max(40, vr.width  * frac);
    cropH = Math.max(40, vr.height * frac);
  }
  cropX = cx - cropW / 2; cropY = cy - cropH / 2;
  clampCropToVideo(); drawCropBox(); _refreshShapePreview();
}
if (cropSize) cropSize.addEventListener('input', _applyCropSize);
function drawCropBox() {
  cropBox.style.left   = cropX + 'px';
  cropBox.style.top    = cropY + 'px';
  cropBox.style.width  = cropW + 'px';
  cropBox.style.height = cropH + 'px';
}
function clampCropToVideo() {
  const vr = vid.getBoundingClientRect();
  const wr = wrap.getBoundingClientRect();
  const vL = vr.left - wr.left, vT = vr.top - wr.top;
  cropW = Math.min(cropW, vr.width);
  cropH = Math.min(cropH, vr.height);
  cropX = Math.max(vL, Math.min(cropX, vL + vr.width  - cropW));
  cropY = Math.max(vT, Math.min(cropY, vT + vr.height - cropH));
  // Snap flush to an edge when within a few px — makes reaching the exact
  // edge / corner effortless on touch (no "can't quite get there" gap).
  const SNAP = 9;
  if (cropX - vL <= SNAP) cropX = vL;
  else if ((vL + vr.width) - (cropX + cropW) <= SNAP) cropX = vL + vr.width - cropW;
  if (cropY - vT <= SNAP) cropY = vT;
  else if ((vT + vr.height) - (cropY + cropH) <= SNAP) cropY = vT + vr.height - cropH;
}

cropModeToggle.addEventListener('click', () => {
  cropMode = !cropMode;
  cropModeToggle.classList.toggle('on', cropMode);
  cropModeToggle.textContent = cropMode ? 'Unfixed' : 'Fixed';
  refreshCropOverlay();
  _refreshShapePreview();   // output square moved (box ⇄ centred-video)
});
aspectLockToggle.addEventListener('click', () => {
  aspectLock = !aspectLock;
  aspectLockToggle.classList.toggle('on', aspectLock);
  aspectLockToggle.textContent = aspectLock ? '🔒 Square' : '↔ Free';
  if (cropMode && aspectLock) {
    const m = Math.min(cropW, cropH);
    cropW = m; cropH = m;
    drawCropBox();
  }
  refreshCropOverlay();
  _refreshShapePreview();
});

// Drag the whole box.
let _boxDrag = null;
cropBox.addEventListener('pointerdown', e => {
  if (e.target.classList.contains('crop-handle')) return; // handle drag covered below
  e.preventDefault(); e.stopPropagation();
  _boxDrag = { dx: e.clientX - cropBox.getBoundingClientRect().left,
               dy: e.clientY - cropBox.getBoundingClientRect().top };
  cropBox.classList.add('dragging');
  try { cropBox.setPointerCapture(e.pointerId); } catch (err) {}
});
window.addEventListener('pointermove', e => {
  if (_boxDrag) {
    const wr = wrap.getBoundingClientRect();
    cropX = e.clientX - wr.left - _boxDrag.dx;
    cropY = e.clientY - wr.top  - _boxDrag.dy;
    clampCropToVideo(); drawCropBox();
  }
  if (_handleDrag) {
    const wr = wrap.getBoundingClientRect();
    const x = e.clientX - wr.left, y = e.clientY - wr.top;
    const r = _handleDrag.start;
    let nx = r.x, ny = r.y, nw = r.w, nh = r.h;
    if (_handleDrag.h.includes('w')) { nx = x; nw = r.x + r.w - x; }
    if (_handleDrag.h.includes('e')) { nw = x - r.x; }
    if (_handleDrag.h.includes('n')) { ny = y; nh = r.y + r.h - y; }
    if (_handleDrag.h.includes('s')) { nh = y - r.y; }
    if (aspectLock) {
      const sz = Math.max(40, Math.min(nw, nh));
      nw = sz; nh = sz;
      // Re-anchor so the dragged corner stays under the pointer.
      if (_handleDrag.h.includes('w')) nx = r.x + r.w - nw;
      if (_handleDrag.h.includes('n')) ny = r.y + r.h - nh;
    }
    cropX = nx; cropY = ny; cropW = Math.max(40, nw); cropH = Math.max(40, nh);
    clampCropToVideo(); drawCropBox();
  }
  if (_boxDrag || _handleDrag) _refreshShapePreview();   // keep the shape outline glued to the box
});
window.addEventListener('pointerup', () => {
  if (_boxDrag) { _boxDrag = null; cropBox.classList.remove('dragging'); }
  if (_handleDrag) { _handleDrag = null; syncCropSizeSlider(); }
});

// Resize via corner handles.
let _handleDrag = null;
cropBox.querySelectorAll('.crop-handle').forEach(h => {
  h.addEventListener('pointerdown', e => {
    e.preventDefault(); e.stopPropagation();
    _handleDrag = { h: h.dataset.h, start: { x: cropX, y: cropY, w: cropW, h: cropH } };
    try { h.setPointerCapture(e.pointerId); } catch (err) {}
  });
});

// Convert displayed-pixel crop to source-pixel crop for the encoder.
function cropToSourcePixels() {
  if (!cropMode || !cropW || !cropH || !vid.videoWidth) return null;
  const vr = vid.getBoundingClientRect();
  const wr = wrap.getBoundingClientRect();
  const vL = vr.left - wr.left, vT = vr.top - wr.top;
  const sx = vid.videoWidth  / vr.width;
  const sy = vid.videoHeight / vr.height;
  return {
    x: Math.round(Math.max(0, (cropX - vL) * sx)),
    y: Math.round(Math.max(0, (cropY - vT) * sy)),
    w: Math.round(cropW * sx),
    h: Math.round(cropH * sy),
  };
}

// Pack-kind query param → make body.
const _editorPackKind = (() => {
  try {
    const k = new URLSearchParams(location.search).get('kind') || '';
    return ['video','static','custom_emoji'].includes(k) ? k : 'video';
  } catch (e) { return 'video'; }
})();

// ── Shape & cutout ────────────────────────────────────────────────────────
// STATIC: shapes/cutout cut TRANSPARENT corners (webp alpha).
// VIDEO:  shapes either FILL the corners (blur/colour) or go TRANSPARENT (real
//         VP9 alpha); background-removal ("Remove background") now works too via
//         a per-frame alpha cutout that keeps the sticker animated.
const shapeSection = document.getElementById('shape-section');
const shapeRow     = document.getElementById('shape-row');
const cutoutToggle = document.getElementById('cutout-toggle');
const hqToggle     = document.getElementById('hq-toggle');
const shapeHint    = document.getElementById('shape-hint');
const shapeLabel   = document.getElementById('shape-label');
const drawCanvas   = document.getElementById('draw-canvas');
const drawClearBtn = document.getElementById('draw-clear');
const fillRow      = document.getElementById('fill-row');
const fillColor    = document.getElementById('fill-color');
const padRow       = document.getElementById('pad-row');
const padToggle    = document.getElementById('pad-toggle');

const SHAPE_PAD = 0.07;       // matches backend _SHAPE_PAD (breathing margin)
let chosenShape = 'square';   // square|circle|triangle|star|heart|diamond|custom
let cutout = false;
let hqMatte = false;          // v2.7-C "best quality" matte (only when cutout on)
let chosenFill = 'blur';      // VIDEO corner fill: 'blur' | 'color' | 'transparent'
let chosenPad = false;        // float the shape free of the frame edges
let customPoints = [];        // normalised [[x,y],…] in output-square space

// Simple mode (playful skin) hides the corner picker and auto-uses a clean
// transparent cut for any shape; other modes use the chosen Corners fill.
function effectiveFill() {
  if (document.body.classList.contains('skin-playful')) return 'transparent';
  return chosenFill;
}

const _isVideoKind = (_editorPackKind === 'video');
// Show the per-shape extras (corner fill for video, padding for any shape)
// only once a non-square shape is picked.
function _refreshShapeExtras() {
  const shaped = chosenShape !== 'square';
  if (_isVideoKind) fillRow.style.display = shaped ? '' : 'none';
  padRow.style.display = shaped ? '' : 'none';
}
// custom-emoji keeps it simple (no shapes); static & video both get shapes.
if (_editorPackKind === 'custom_emoji') {
  shapeSection.style.display = 'none';
  const _st = document.querySelector('#tool-tabs [data-tab="shape"]');
  if (_st) _st.style.display = 'none';
  if (document.body.dataset.tab === 'shape') setTab('emoji');
} else if (_isVideoKind) {
  // Video CAN be background-removed now — per-frame alpha keeps it animated.
  cutoutToggle.textContent = '✂️ Remove background';
  shapeLabel.innerHTML = 'Shape <span class="meta">(video — frame it, or cut out the subject)</span>';
  shapeHint.textContent = 'Square = the full (cropped) frame. Pick a shape to frame it, or “Remove background” to cut out the moving subject (stays animated, ~10–20s).';
  // The dedicated Studio "Cutout" tab is a static composite — for video it
  // instead runs the animated per-frame cutout. Relabel so that's clear.
  const _cb = document.getElementById('studio-cutout');
  if (_cb) _cb.textContent = '✂️ Remove background (keep animated)';
  const _cm = document.querySelector('[data-panel="cutout"] .meta');
  if (_cm) _cm.textContent = 'Removes the background from every frame — the sticker stays animated. (~10–20s)';
}
_refreshShapeExtras();

const SHAPE_HINTS = {
  square:   'Square = the full (cropped) frame.',
  circle:   'Circle cut from the frame — transparent corners.',
  triangle: 'Triangle cutout — transparent outside.',
  star:     'Star cutout — transparent outside.',
  heart:    'Heart cutout — transparent outside.',
  diamond:  'Diamond cutout — transparent outside.',
  custom:   'Draw a shape over the image (drag a loop), then Make.',
};
const SHAPE_HINTS_VIDEO = {
  square:   'Square = the full (cropped) frame.',
  circle:   'Circle of video; corners filled (blur/colour).',
  triangle: 'Triangle of video; corners filled.',
  star:     'Star of video; corners filled.',
  heart:    'Heart of video; corners filled.',
  diamond:  'Diamond of video; corners filled.',
  custom:   'Draw a shape over the video (drag a loop), then Make.',
};

shapeRow.addEventListener('click', e => {
  const pill = e.target.closest('[data-shape]');
  if (!pill) return;
  chosenShape = pill.dataset.shape;
  shapeRow.querySelectorAll('[data-shape]').forEach(p => p.classList.toggle('on', p === pill));
  shapeHint.textContent = (_isVideoKind ? SHAPE_HINTS_VIDEO : SHAPE_HINTS)[chosenShape] || '';
  _refreshShapeExtras();   // fill (video) + padding controls, non-square only
  const drawing = chosenShape === 'custom';
  const preset  = !drawing && chosenShape !== 'square';   // circle/heart/star/…
  drawCanvas.classList.toggle('on', drawing);             // freehand draw (captures input)
  drawCanvas.classList.toggle('preview', preset);         // shape preview (click-through)
  drawClearBtn.style.display = drawing ? '' : 'none';
  if (drawing) { resizeDrawCanvas(); redrawCustom(); }
  else if (preset) { redrawShapePreview(); }
  else {           // square: no overlay
    customPoints = [];
    const ctx = drawCanvas.getContext('2d');
    ctx.clearRect(0, 0, drawCanvas.width, drawCanvas.height);
  }
});
cutoutToggle.addEventListener('click', () => {
  cutout = !cutout;
  cutoutToggle.classList.toggle('on', cutout);
  // "Best quality" only applies to a cutout — reveal it with the cutout, and
  // clear it when the cutout is turned off.
  if (hqToggle) {
    hqToggle.style.display = cutout ? '' : 'none';
    if (!cutout) { hqMatte = false; hqToggle.classList.remove('on'); }
  }
});
if (hqToggle) hqToggle.addEventListener('click', () => {
  hqMatte = !hqMatte;
  hqToggle.classList.toggle('on', hqMatte);
});
fillRow.addEventListener('click', e => {
  const pill = e.target.closest('[data-fill]');
  if (!pill) return;
  chosenFill = pill.dataset.fill;
  fillRow.querySelectorAll('[data-fill]').forEach(p => p.classList.toggle('on', p === pill));
  fillColor.style.display = (chosenFill === 'color') ? '' : 'none';
  _refreshShapePreview();           // WYSIWYG: margin now shows the new fill
});
fillColor.addEventListener('input', () => { if (chosenFill === 'color') _refreshShapePreview(); });
padToggle.addEventListener('click', () => {
  chosenPad = !chosenPad;
  padToggle.classList.toggle('on', chosenPad);
  _refreshShapePreview();   // preview the new margin live
});
drawClearBtn.addEventListener('click', () => { customPoints = []; redrawCustom(); });

// The custom polygon is captured in the *output square* — the region that
// actually becomes the 512×512 sticker. That's the crop box when "Pick
// region" is on, else the centred square of the video (center-fill cover).
function outputSquareRect() {
  const vr = vid.getBoundingClientRect();
  if (cropMode && cropW && cropH) {
    const wr = wrap.getBoundingClientRect();
    const side = Math.min(cropW, cropH);
    // The encoder cover-scales the crop box then CENTER-crops it to a square,
    // so the square that becomes the sticker is the CENTRE of the box (not its
    // corner). Centre it here so the shape preview matches the real output.
    return { left: wr.left + cropX + (cropW - side) / 2,
             top:  wr.top  + cropY + (cropH - side) / 2, side };
  }
  const side = Math.min(vr.width, vr.height);
  return { left: vr.left + (vr.width - side) / 2, top: vr.top + (vr.height - side) / 2, side };
}
function resizeDrawCanvas() {
  const wr = wrap.getBoundingClientRect();
  drawCanvas.width = wr.width; drawCanvas.height = wr.height;
}
function redrawCustom() {
  const ctx = drawCanvas.getContext('2d');
  ctx.clearRect(0, 0, drawCanvas.width, drawCanvas.height);
  if (!customPoints.length) return;
  const sq = outputSquareRect();
  const wr = wrap.getBoundingClientRect();
  ctx.beginPath();
  customPoints.forEach((p, i) => {
    const x = sq.left + p[0] * sq.side - wr.left;
    const y = sq.top  + p[1] * sq.side - wr.top;
    if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
  });
  ctx.closePath();
  ctx.fillStyle = 'rgba(90,200,250,0.25)';
  ctx.strokeStyle = '#5ac8fa'; ctx.lineWidth = 2;
  ctx.fill(); ctx.stroke();
}

// Preset-shape polygons in 0..1 output-square space — MUST mirror the backend
// _preset_points so the preview matches the encoded mask exactly.
const SHAPE_PRESETS = {
  triangle: [[0.5, 0], [0, 1], [1, 1]],
  diamond:  [[0.5, 0], [1, 0.5], [0.5, 1], [0, 0.5]],
  heart:    [[0.50, 0.95], [0.06, 0.52], [0.06, 0.30], [0.22, 0.16],
             [0.40, 0.18], [0.50, 0.30], [0.60, 0.18], [0.78, 0.16],
             [0.94, 0.30], [0.94, 0.52]],
  star:     (() => {
    const p = [], cx = 0.5, cy = 0.5, oR = 0.5, iR = 0.21;
    for (let i = 0; i < 10; i++) {
      const r = (i % 2 === 0) ? oR : iR;
      const a = -Math.PI / 2 + i * Math.PI / 5;
      p.push([cx + r * Math.cos(a), cy + r * Math.sin(a)]);
    }
    return p;
  })(),
};
function _shapePath2D(ox, oy, s) {
  const p = new Path2D();
  if (chosenShape === 'circle') {
    p.ellipse(ox + s / 2, oy + s / 2, s / 2, s / 2, 0, 0, Math.PI * 2);
    return p;
  }
  const pts = SHAPE_PRESETS[chosenShape];
  if (!pts) return p;
  pts.forEach((pt, i) => {
    const x = ox + pt[0] * s, y = oy + pt[1] * s;
    if (i === 0) p.moveTo(x, y); else p.lineTo(x, y);
  });
  p.closePath();
  return p;
}
function _drawChecker(ctx, W, H) {
  const c = 11;
  for (let y = 0; y < H; y += c) {
    for (let x = 0; x < W; x += c) {
      ctx.fillStyle = (((x / c | 0) + (y / c | 0)) % 2 === 0)
        ? 'rgba(255,255,255,0.16)' : 'rgba(0,0,0,0.32)';
      ctx.fillRect(x, y, c, c);
    }
  }
}
// WYSIWYG preview: the shape stays clear (sharp video shows through), and the
// MARGIN renders the actual Corners fill — blurred video / solid colour /
// checkerboard for transparent — so what you see is what Make produces.
function redrawShapePreview() {
  const wr = wrap.getBoundingClientRect();
  const W = Math.round(wr.width), H = Math.round(wr.height);
  if (drawCanvas.width !== W || drawCanvas.height !== H) {
    drawCanvas.width = W; drawCanvas.height = H;
  }
  const ctx = drawCanvas.getContext('2d');
  ctx.clearRect(0, 0, W, H);
  if (chosenShape === 'square' || chosenShape === 'custom') return;
  const sq = outputSquareRect();
  if (!sq.side) return;
  let ox = sq.left - wr.left, oy = sq.top - wr.top, s = sq.side;
  if (chosenPad) { const m = s * SHAPE_PAD; ox += m; oy += m; s -= 2 * m; }  // breathing margin
  const shape = _shapePath2D(ox, oy, s);
  // Clip to the EXTERIOR (frame minus shape) via even-odd, then paint the fill.
  const ext = new Path2D();
  ext.rect(0, 0, W, H);
  ext.addPath(shape);
  // static shapes are always transparent; video uses the Corners choice.
  const fillMode = _isVideoKind ? effectiveFill() : 'transparent';
  ctx.save();
  ctx.clip(ext, 'evenodd');
  if (fillMode === 'color') {
    ctx.globalAlpha = 0.92; ctx.fillStyle = fillColor.value;
    ctx.fillRect(0, 0, W, H); ctx.globalAlpha = 1;
  } else if (fillMode === 'blur') {
    try {
      const vr = vid.getBoundingClientRect();
      ctx.filter = 'blur(7px) brightness(0.92) saturate(0.85)';
      ctx.drawImage(vid, vr.left - wr.left, vr.top - wr.top, vr.width, vr.height);
      ctx.filter = 'none';
    } catch (e) {
      ctx.fillStyle = 'rgba(0,0,0,0.5)'; ctx.fillRect(0, 0, W, H);
    }
  } else {                                  // transparent
    _drawChecker(ctx, W, H);
  }
  ctx.restore();
  ctx.strokeStyle = '#5ac8fa'; ctx.lineWidth = 2; ctx.stroke(shape);
}
function _refreshShapePreview() {
  if (drawCanvas.classList.contains('preview')) redrawShapePreview();
}

// ── Live result preview (right pane) — exactly what the sticker will look ────
const resultCanvas = document.getElementById('result-preview');
const resultCtx = resultCanvas ? resultCanvas.getContext('2d') : null;
// Cached snapshot of the Studio overlay layers (text/emoji/image/stickers) on
// a transparent 512² canvas, drawn on TOP of the live video in the preview so
// the composite is visible on EVERY tab — not just the Fabric editor tabs.
// Regenerated only when the canvas changes (object add/modify/remove/undo).
let _overlayImg = null, _overlaySnapTimer = 0;
function _updateOverlaySnapshot() {
  if (!fcanvas) { _overlayImg = null; return; }
  clearTimeout(_overlaySnapTimer);
  _overlaySnapTimer = setTimeout(() => {
    if (!fcanvas) return;
    const base = fcanvas.getObjects().find(o => o.studioRole === 'base');
    const bv = base ? base.visible : false;
    const pbg = fcanvas.backgroundColor;
    try {
      if (base) base.visible = false;
      fcanvas.backgroundColor = null;
      fcanvas.renderAll();
      const others = fcanvas.getObjects().filter(o => o.studioRole !== 'base');
      if (!others.length) { _overlayImg = null; }
      else {
        const url = fcanvas.toDataURL({ format: 'png', multiplier: 512 / STUDIO_PX });
        const im = new Image();
        im.onload = () => { _overlayImg = im; };
        im.onerror = () => { _overlayImg = null; };
        im.src = url;
      }
    } catch (e) { /* keep the previous snapshot */ }
    finally {
      if (base) base.visible = bv;
      fcanvas.backgroundColor = pbg;
      fcanvas.renderAll();
    }
  }, 60);
}

// The square region of the SOURCE the encoder turns into the 512² sticker:
// the centre square of the crop box (pick-region) or of the whole video.
function sourceSquare() {
  const vw = vid.videoWidth, vh = vid.videoHeight;
  if (!vw || !vh) return null;
  let rx = 0, ry = 0, rw = vw, rh = vh;
  const c = cropToSourcePixels();
  if (c) { rx = c.x; ry = c.y; rw = c.w; rh = c.h; }
  const side = Math.min(rw, rh);
  return { sx: rx + (rw - side) / 2, sy: ry + (rh - side) / 2, side };
}

// Shape path in result-canvas space (0..R), padding-inset to match the encoder.
function _resultShapePath2D(R) {
  const p = new Path2D();
  const pad = chosenPad ? SHAPE_PAD : 0;
  const m = pad * R, inner = R - 2 * m;
  if (chosenShape === 'circle') {
    p.ellipse(R / 2, R / 2, inner / 2, inner / 2, 0, 0, Math.PI * 2);
    return p;
  }
  const pts = (chosenShape === 'custom')
    ? (customPoints.length >= 3 ? customPoints : null)
    : SHAPE_PRESETS[chosenShape];
  if (!pts) return null;
  pts.forEach((pt, i) => {
    const x = m + pt[0] * inner, y = m + pt[1] * inner;
    if (i === 0) p.moveTo(x, y); else p.lineTo(x, y);
  });
  p.closePath();
  return p;
}

function renderResult() {
  if (!resultCtx) return;
  const R = resultCanvas.width;
  resultCtx.clearRect(0, 0, R, R);
  const sq = sourceSquare();
  if (!sq) return;
  const shaped = (chosenShape !== 'square');
  const fillMode = shaped ? (_isVideoKind ? effectiveFill() : 'transparent') : 'opaque';
  // 1) margin fill (only when shaped) — mirrors the Corners choice
  if (shaped) {
    if (fillMode === 'transparent') _drawChecker(resultCtx, R, R);
    else if (fillMode === 'color') { resultCtx.fillStyle = fillColor.value; resultCtx.fillRect(0, 0, R, R); }
    else if (fillMode === 'blur') {
      try {
        resultCtx.filter = 'blur(6px) brightness(0.92) saturate(0.85)';
        resultCtx.drawImage(vid, sq.sx, sq.sy, sq.side, sq.side, 0, 0, R, R);
        resultCtx.filter = 'none';
      } catch (e) { resultCtx.fillStyle = '#1a1a1a'; resultCtx.fillRect(0, 0, R, R); }
    }
  }
  // 2) sharp video, clipped to the shape (or the full square for 'square')
  resultCtx.save();
  const path = shaped ? _resultShapePath2D(R) : null;
  if (path) resultCtx.clip(path);
  try { resultCtx.drawImage(vid, sq.sx, sq.sy, sq.side, sq.side, 0, 0, R, R); } catch (e) {}
  resultCtx.restore();
  // 3) Studio overlays ON TOP (unclipped, like the export) — so the live
  //    preview shows your stickers/text/emoji on EVERY tab.
  if (_overlayImg && _overlayImg.naturalWidth) {
    try { resultCtx.drawImage(_overlayImg, 0, 0, R, R); } catch (e) {}
  }
}

let _resultRAF = 0;
function _resultLoop() { renderResult(); _resultRAF = requestAnimationFrame(_resultLoop); }
if (resultCtx) _resultLoop();
let _drawing = false;
function _addCustomPoint(e) {
  const sq = outputSquareRect();
  if (!sq.side) return;
  const nx = Math.max(0, Math.min(1, (e.clientX - sq.left) / sq.side));
  const ny = Math.max(0, Math.min(1, (e.clientY - sq.top)  / sq.side));
  const last = customPoints[customPoints.length - 1];
  if (last && Math.hypot(nx - last[0], ny - last[1]) < 0.012) return;
  customPoints.push([nx, ny]);
}
drawCanvas.addEventListener('pointerdown', e => {
  if (chosenShape !== 'custom') return;
  e.preventDefault();
  _drawing = true; customPoints = [];
  try { drawCanvas.setPointerCapture(e.pointerId); } catch (_) {}
  _addCustomPoint(e); redrawCustom();
});
drawCanvas.addEventListener('pointermove', e => {
  if (!_drawing) return;
  _addCustomPoint(e); redrawCustom();
});
drawCanvas.addEventListener('pointerup', () => { _drawing = false; redrawCustom(); });
window.addEventListener('resize', () => {
  if (chosenShape === 'custom') { resizeDrawCanvas(); redrawCustom(); }
  else _refreshShapePreview();
});

// ── Background-job progress (v2.7-B) ──────────────────────────────────────
// Heavy video encodes return {job_id} and run on the server's worker; poll
// the job for a live progress bar + Telegram push on completion. Cheap static
// encodes still return the sticker inline (no job_id).
function renderStickerBar(pct, label) {
  pct = Math.max(0, Math.min(100, Math.round(pct || 0)));
  progressEl.innerHTML = `<div style="font-size:13px;margin-bottom:6px">${label || 'Working…'} ${pct}%</div><div style="height:8px;background:#2a2a2e;border-radius:5px;overflow:hidden"><div style="height:100%;width:${pct}%;background:#5ac8fa;transition:width .35s"></div></div>`;
}
async function pollStickerJob(jobId) {
  const labels = { queued: 'Queued…', running: 'Encoding…', done: 'Done', error: 'Failed' };
  renderStickerBar(3, 'Queued…');
  for (let i = 0; i < 200; i++) {
    await new Promise(res => setTimeout(res, 1200));
    let j;
    try { j = await api(`/api/sticker_jobs/${jobId}`); }
    catch (e) { continue; }   // transient blip — keep polling
    renderStickerBar(j.progress, labels[j.status] || 'Working…');
    if (j.status === 'done') return j.result || {};
    if (j.status === 'error') throw new Error(j.error || 'encode failed');
  }
  throw new Error('timed out waiting for the encode');
}
function showStickerSuccess(r) {
  r = r || {};
  progressEl.innerHTML = `✅ Added! <a href="#" id="open-pack-link" style="color:#5ac8fa">Open your pack</a><br><span style="font-size:12px">You can make another variant — different emoji / trim / crop — or go back.</span>`;
  const link = document.getElementById('open-pack-link');
  if (link) link.addEventListener('click', ev => {
    ev.preventDefault();
    if (tg && tg.openTelegramLink) tg.openTelegramLink(r.set_url);
    else window.open(r.set_url, '_blank');
  });
  if (tg && tg.HapticFeedback) tg.HapticFeedback.notificationOccurred('success');
  makeBtn.disabled = false;
  makeBtn.textContent = '✨ Make another';
}

makeBtn.addEventListener('click', async () => {
  // In a Studio tool the action button exports the canvas instead of /make.
  if (document.body.classList.contains('studio-active')) { studioExport(); return; }
  makeBtn.disabled = true;
  const _cutoutVid = (_editorPackKind === 'video' && cutout);
  const _transp = (_editorPackKind === 'video' && chosenShape !== 'square' && effectiveFill() === 'transparent');
  progressEl.textContent = _cutoutVid
    ? 'Cutting out the subject frame-by-frame… (10–20s)'
    : _transp
    ? 'Encoding transparent sticker… (10–30s)'
    : 'Encoding sticker… (5–20s)';
  const body = {
    emoji:      chosenEmoji,
    trim_start: trimStart,
    trim_end:   trimEnd,
    pack_kind:  _editorPackKind,
  };
  const crop = cropToSourcePixels();
  if (crop) Object.assign(body, { crop_x: crop.x, crop_y: crop.y, crop_w: crop.w, crop_h: crop.h });
  // Shapes apply to static (transparent) AND video (opaque fill). Square = none.
  if (_editorPackKind === 'static' || _editorPackKind === 'video') {
    if (chosenShape && chosenShape !== 'square') body.shape = chosenShape;
    if (chosenShape === 'custom') {
      if (customPoints.length < 3) {
        progressEl.textContent = '✏️ Draw a shape first — drag a loop over the image.';
        makeBtn.disabled = false;
        return;
      }
      body.points = customPoints;
    }
    // cutout (background removal) → transparent. Static = webp alpha; video =
    // per-frame alpha (the subject is matted out of every frame).
    if ((_editorPackKind === 'static' || _editorPackKind === 'video') && cutout) {
      body.cutout = true;
      if (hqMatte) body.hq = true;   // v2.7-C best-quality matte
    }
    // corner fill → video only, non-square shape, when NOT cutting out (cutout
    // removes the whole background, so the corner-fill choice is moot).
    if (_editorPackKind === 'video' && chosenShape !== 'square' && !cutout) {
      const ef = effectiveFill();
      body.fill = (ef === 'color') ? fillColor.value : ef;
    }
    // breathing-margin toggle (static + video), non-square shapes only.
    if (chosenShape !== 'square' && chosenPad) body.padding = true;
  }
  try {
    const r = await api(`/api/sticker_drafts/${DRAFT_ID}/make`, {
      method: 'POST', body: JSON.stringify(body),
    });
    // Heavy encodes come back as {job_id} → poll for progress + completion.
    // Cheap static encodes return the sticker inline (no job_id).
    const result = r.job_id ? await pollStickerJob(r.job_id) : r;
    showStickerSuccess(result);
  } catch (e) {
    progressEl.textContent = '❌ ' + e.message;
    makeBtn.disabled = false;
  }
});

// ── 🎨 Studio (Fabric.js compositing → static webp) ───────────────────────
// A free-form layer compositor: a base frame + draggable/resizable text,
// emoji and image layers, optional background cut-out, and a server-side
// die-cut outline. Export rasterises the canvas to a 512² PNG and POSTs it
// to /compose, which encodes it to a static sticker.
let STUDIO_PX = 320;          // on-screen canvas (sized to fit at build); exported ×(512/STUDIO_PX)
let fcanvas = null;           // fabric.Canvas (lazily built on first open)
let _studioBold = false;
let _studioBuilt = false;
let _outline = false;         // die-cut outline toggle

// ── Undo / redo (JSON snapshots) ──────────────────────────────────────────
let _hist = [], _redo = [], _restoring = false;
function _snap() {
  if (_restoring || !fcanvas) return;
  try { _hist.push(JSON.stringify(fcanvas.toJSON(['studioRole', 'animUrl']))); }
  catch (e) { return; }
  if (_hist.length > 40) _hist.shift();
  _redo = [];
}
function _loadState(json) {
  _restoring = true;
  fcanvas.loadFromJSON(json, () => { fcanvas.renderAll(); _restoring = false; _updateOverlaySnapshot(); });
}
function studioUndo() {
  if (_hist.length < 2) return;          // keep at least the base state
  _redo.push(_hist.pop());
  _loadState(_hist[_hist.length - 1]);
}
function studioRedo() {
  if (!_redo.length) return;
  const s = _redo.pop(); _hist.push(s); _loadState(s);
}

// Always hand Fabric a data-URL (not a blob: URL) so layers survive the
// toJSON/loadFromJSON round-trip that undo/redo relies on.
function _studioBaseDataUrl() {
  if (vid.videoWidth > 0) {
    const c = document.createElement('canvas');
    c.width = vid.videoWidth; c.height = vid.videoHeight;
    try { c.getContext('2d').drawImage(vid, 0, 0); return Promise.resolve(c.toDataURL('image/png')); }
    catch (e) {}
  }
  if (!_previewBlobUrl) return Promise.resolve(null);
  return new Promise(res => {
    const im = new Image();
    im.onload = () => {
      const c = document.createElement('canvas');
      c.width = im.naturalWidth; c.height = im.naturalHeight;
      try { c.getContext('2d').drawImage(im, 0, 0); res(c.toDataURL('image/png')); }
      catch (e) { res(null); }
    };
    im.onerror = () => res(null);
    im.src = _previewBlobUrl;
  });
}

// Add an image layer. cover=true fits it to fill the square (base frames);
// otherwise it's dropped at ~45% size as a movable overlay.
function _addImage(url, opts = {}) {
  return new Promise(resolve => {
    if (!url || !fcanvas) { resolve(null); return; }
    fabric.Image.fromURL(url, img => {
      if (!img || !img.width) { resolve(null); return; }
      const s = opts.cover
        ? Math.max(STUDIO_PX / img.width, STUDIO_PX / img.height)
        : (STUDIO_PX * 0.45) / Math.max(img.width, img.height);
      img.set({ left: STUDIO_PX / 2, top: STUDIO_PX / 2,
                originX: 'center', originY: 'center',
                scaleX: s, scaleY: s, selectable: true });
      if (opts.role) img.studioRole = opts.role;
      fcanvas.add(img);
      if (opts.toBack) fcanvas.sendToBack(img);
      if (!opts.silent) fcanvas.setActiveObject(img);
      fcanvas.renderAll();
      resolve(img);
    });
  });
}

// Build the Fabric compositor once, lazily — called when the first Studio
// tool (Text/Image/Cutout/Outline/Layers) is opened. The canvas lives in the
// preview dock; the tools are bottom-nav tabs.
async function ensureStudio() {
  if (_studioBuilt) return;
  _studioBuilt = true;
  // Size the square canvas to the (full-width) dock, capped at 320.
  const stage = document.getElementById('studio-stage');
  const avail = stage ? stage.clientWidth : 320;
  STUDIO_PX = Math.max(240, Math.min(320, Math.round(avail) - 18));
  const cv = document.getElementById('studio-canvas');
  cv.width = STUDIO_PX; cv.height = STUDIO_PX;
  fcanvas = new fabric.Canvas('studio-canvas', {
    backgroundColor: _isVideoKind ? null : '#000', preserveObjectStacking: true,
    width: STUDIO_PX, height: STUDIO_PX,
  });
  // Sticker maker: scale only from corners (uniform) — hide the middle handles
  // so a layer can't be stretched into a distorted thin sliver.
  fcanvas.on('object:added', (e) => {
    if (e && e.target && e.target.setControlsVisibility) {
      e.target.setControlsVisibility({ mt: false, mb: false, ml: false, mr: false });
    }
    _snap(); _updateOverlaySnapshot();
  });
  fcanvas.on('object:modified', () => { _snap(); _updateOverlaySnapshot(); });
  fcanvas.on('object:removed', () => { _snap(); _updateOverlaySnapshot(); });
  // Keep the live preview in step while a layer is dragged/scaled, not just at
  // transform-end — cheap (debounced snapshot), big UX win.
  fcanvas.on('object:moving', _updateOverlaySnapshot);
  fcanvas.on('object:scaling', _updateOverlaySnapshot);
  fcanvas.on('object:rotating', _updateOverlaySnapshot);
  _restoring = true;                       // don't snapshot the initial base add
  if (_isVideoKind) {
    // Live preview behind the transparent compositor → Text/Image/Stickers
    // animate. (Last known working; Emoji + the Configure rail use video-wrap.)
    document.body.classList.add('studio-video');
    const frame = document.getElementById('studio-frame');
    if (frame) { frame.style.width = STUDIO_PX + 'px'; frame.style.height = STUDIO_PX + 'px'; }
    const sv = document.getElementById('studio-vid');
    if (sv && _previewBlobUrl) {
      if (sv.src !== _previewBlobUrl) sv.src = _previewBlobUrl;
      sv.play().catch(() => {});
    }
  } else {
    const url = await _studioBaseDataUrl();
    if (url) await _addImage(url, { role: 'base', cover: true, toBack: true, silent: true });
  }
  _restoring = false;
  _snap();                                 // history seed = base (static) or empty (video)
}

function _studioActiveText() {
  const o = fcanvas && fcanvas.getActiveObject();
  return o && o.type === 'i-text' ? o : null;
}

function studioAddText() {
  if (!fcanvas) return;
  const inp = document.getElementById('studio-text');
  const txt = (inp.value || '').trim() || 'TEXT';
  const t = new fabric.IText(txt, {
    left: STUDIO_PX / 2, top: STUDIO_PX * 0.82,
    originX: 'center', originY: 'center', textAlign: 'center',
    fontFamily: document.getElementById('studio-font').value,
    fontWeight: _studioBold ? 'bold' : 'normal',
    fill: document.getElementById('studio-fill').value,
    stroke: document.getElementById('studio-stroke').value,
    strokeWidth: 2.4, paintFirst: 'stroke', strokeLineJoin: 'round',
    fontSize: 40, shadow: 'rgba(0,0,0,0.5) 0 2px 4px',
  });
  fcanvas.add(t); fcanvas.setActiveObject(t); fcanvas.renderAll();
  inp.value = '';
}

function studioAddEmoji() {
  if (!fcanvas) return;
  const inp = document.getElementById('studio-emoji');
  const e = (inp.value || '').trim();
  if (!e) return;
  // Emoji are colour glyphs — no stroke (an outline looks wrong on them).
  const t = new fabric.IText(e, {
    left: STUDIO_PX / 2, top: STUDIO_PX / 2,
    originX: 'center', originY: 'center', fontSize: 96, editable: false,
  });
  fcanvas.add(t); fcanvas.setActiveObject(t); fcanvas.renderAll();
  inp.value = '';
}

// Animated per-frame cutout for VIDEO: posts /make with cutout, adds the
// transparent webm straight to the pack (no static Studio composite).
async function videoCutoutMake() {
  const prog = document.getElementById('progress');
  const btn = document.getElementById('studio-cutout');
  if (btn) btn.disabled = true;
  prog.textContent = 'Cutting out the subject frame-by-frame… (10–20s)';
  const body = {
    emoji: chosenEmoji, trim_start: trimStart, trim_end: trimEnd,
    pack_kind: 'video', cutout: true,
  };
  const crop = cropToSourcePixels();
  if (crop) Object.assign(body, { crop_x: crop.x, crop_y: crop.y, crop_w: crop.w, crop_h: crop.h });
  if (chosenShape && chosenShape !== 'square') {
    body.shape = chosenShape;
    if (chosenShape === 'custom' && customPoints.length >= 3) body.points = customPoints;
  }
  try {
    const r = await api(`/api/sticker_drafts/${DRAFT_ID}/make`, {
      method: 'POST', body: JSON.stringify(body),
    });
    prog.innerHTML = '✅ Added (animated)! <a href="#" id="vc-pack-link" style="color:#5ac8fa">Open your pack</a>';
    const link = document.getElementById('vc-pack-link');
    if (link) link.addEventListener('click', ev => {
      ev.preventDefault();
      if (tg && tg.openTelegramLink) tg.openTelegramLink(r.set_url);
      else window.open(r.set_url, '_blank');
    });
    if (tg && tg.HapticFeedback) tg.HapticFeedback.notificationOccurred('success');
  } catch (e) { prog.textContent = '❌ ' + e.message; }
  finally { if (btn) btn.disabled = false; }
}

async function studioCutout() {
  // Video → animated per-frame cutout instead of the static Fabric cutout.
  if (_editorPackKind === 'video') return videoCutoutMake();
  if (!fcanvas) return;
  const prog = document.getElementById('progress');
  const base = fcanvas.getObjects().find(o => o.studioRole === 'base')
            || fcanvas.getObjects().find(o => o.type === 'image');
  // Cut from the base's full-resolution element (same-origin data-URL → no
  // taint); fall back to a fresh frame grab if there's no base layer.
  let srcUrl = null;
  if (base && base.getElement) {
    const el = base.getElement();
    const c = document.createElement('canvas');
    c.width = el.naturalWidth || el.width; c.height = el.naturalHeight || el.height;
    try { c.getContext('2d').drawImage(el, 0, 0); srcUrl = c.toDataURL('image/png'); }
    catch (e) { srcUrl = null; }
  }
  if (!srcUrl) srcUrl = await _studioBaseDataUrl();
  if (!srcUrl) { prog.textContent = '❌ No base image to cut.'; return; }
  const btn = document.getElementById('studio-cutout');
  btn.disabled = true; prog.textContent = 'Removing background… (5–15s)';
  try {
    const r = await api(`/api/sticker_drafts/${DRAFT_ID}/cutout`, {
      method: 'POST', body: JSON.stringify({ png_b64: srcUrl }),
    });
    _restoring = true;
    if (base) fcanvas.remove(base);
    await _addImage(r.png_b64, { role: 'base', cover: true, toBack: true, silent: true });
    _restoring = false; _snap();
    if (!_outline) {     // a cut-out is what makes the outline worth having
      _outline = true;
      document.getElementById('studio-outline-toggle').classList.add('on');
    }
    prog.textContent = '✅ Background removed — die-cut outline enabled.';
    if (tg && tg.HapticFeedback) tg.HapticFeedback.notificationOccurred('success');
  } catch (e) { prog.textContent = '❌ ' + e.message; }
  finally { btn.disabled = false; }
}

async function studioExport() {
  if (!fcanvas) return;
  const prog = document.getElementById('progress');
  const btn = document.getElementById('make-btn');
  fcanvas.discardActiveObject(); fcanvas.renderAll();
  const animated = (_editorPackKind === 'video');
  // For a VIDEO draft, export ONLY the overlay layers on a transparent canvas
  // (hide the base video frame) so the server composites them onto every frame
  // → an animated sticker (v2.7-A). Static drafts export the flattened canvas.
  const base = animated ? fcanvas.getObjects().find(o => o.studioRole === 'base') : null;
  const baseVis = base ? (base.visible !== false) : false;
  // ANIMATED overlays (Giphy stickers with animUrl): on a non-cutout video
  // export they're sent as looping GIF layers (so they keep animating) and
  // EXCLUDED from the flat PNG — hidden during toDataURL alongside the base.
  // Rotated ones stay in the PNG (the ffmpeg path doesn't rotate yet).
  const animObjs = (animated && !cutout)
    ? fcanvas.getObjects().filter(o => o.animUrl && Math.abs(o.angle || 0) < 1)
    : [];
  const k512 = 512 / STUDIO_PX;
  const animOverlays = animObjs.map(o => ({
    url: o.animUrl,
    x: Math.round((o.left - o.getScaledWidth() / 2) * k512),
    y: Math.round((o.top - o.getScaledHeight() / 2) * k512),
    w: Math.round(o.getScaledWidth() * k512),
    h: Math.round(o.getScaledHeight() * k512),
  }));
  // The editor canvas sits on an opaque #000 background so the dock looks right.
  // For a VIDEO export we hide the base frame and the overlay PNG MUST be
  // transparent — otherwise that black fills the canvas and the server stamps
  // it over every frame (the "static sticker on black" bug: the animated video
  // is hidden behind the black). Null the bg for the export, then restore it.
  const _prevBg = fcanvas.backgroundColor;
  const _animVis = animObjs.map(o => o.visible !== false);
  let png;
  try {
    if (base) base.visible = false;
    animObjs.forEach(o => { o.visible = false; });
    if (animated) fcanvas.backgroundColor = null;
    fcanvas.renderAll();
    png = fcanvas.toDataURL({ format: 'png', multiplier: 512 / STUDIO_PX });
  } catch (e) {
    if (base) base.visible = baseVis;
    animObjs.forEach((o, i) => { o.visible = _animVis[i]; });
    fcanvas.backgroundColor = _prevBg; fcanvas.renderAll();
    prog.textContent = '❌ Export blocked (image security): ' + e.message;
    return;
  }
  if (base) base.visible = baseVis;
  animObjs.forEach((o, i) => { o.visible = _animVis[i]; });
  fcanvas.backgroundColor = _prevBg; fcanvas.renderAll();
  btn.disabled = true;
  try {
    let result;
    if (animated) {
      prog.textContent = 'Compositing overlay onto every frame…';
      const body = { overlay_b64: png, emoji: chosenEmoji,
                     trim_start: trimStart, trim_end: trimEnd,
                     cutout: !!cutout, hq: !!hqMatte };
      if (animOverlays.length) body.anim_overlays = animOverlays;
      const crop = cropToSourcePixels();
      if (crop) Object.assign(body, { crop_x: crop.x, crop_y: crop.y, crop_w: crop.w, crop_h: crop.h });
      const r = await api(`/api/sticker_drafts/${DRAFT_ID}/compose_video`, {
        method: 'POST', body: JSON.stringify(body),
      });
      result = r.job_id ? await pollStickerJob(r.job_id) : r;
    } else {
      const body = { png_b64: png, emoji: chosenEmoji };
      if (_outline) {
        body.outline = true;
        body.outline_color = document.getElementById('studio-outline-color').value;
        body.outline_width = parseInt(document.getElementById('studio-outline-width').value, 10) || 12;
      }
      prog.textContent = 'Encoding sticker… (5–15s)';
      result = await api(`/api/sticker_drafts/${DRAFT_ID}/compose`, {
        method: 'POST', body: JSON.stringify(body),
      });
    }
    showStickerSuccess(result);
  } catch (e) {
    prog.textContent = '❌ ' + e.message;
  } finally { btn.disabled = false; }
}

document.getElementById('studio-add-text').addEventListener('click', studioAddText);
document.getElementById('studio-text').addEventListener('keydown', e => {
  if (e.key === 'Enter') { e.preventDefault(); studioAddText(); }
});
document.getElementById('studio-add-emoji').addEventListener('click', studioAddEmoji);
document.getElementById('studio-emoji').addEventListener('keydown', e => {
  if (e.key === 'Enter') { e.preventDefault(); studioAddEmoji(); }
});
document.getElementById('studio-img').addEventListener('change', e => {
  const f = e.target.files && e.target.files[0];
  if (f && fcanvas) {
    const rd = new FileReader();
    rd.onload = () => _addImage(rd.result, { cover: false });
    rd.readAsDataURL(f);
  }
  e.target.value = '';            // allow re-picking the same file
});

// ── Assets tool — drop a transparent Giphy sticker as an overlay layer.
// Searches Giphy STICKERS (transparent), proxies the chosen image same-origin
// (so canvas export isn't tainted), then adds it via _addImage. ──
var _assetInit = false;
async function assetSearch() {
  const grid = document.getElementById('asset-grid');
  const st = document.getElementById('asset-status');
  if (!grid || !st) return;
  const q = (document.getElementById('asset-q').value || '').trim();
  st.textContent = 'Searching…'; grid.innerHTML = '';
  try {
    const d = await api('/api/stickers/gif_search?source=giphy_stickers&q=' + encodeURIComponent(q));
    const items = d.items || [];
    if (!items.length) { st.textContent = 'Nothing found — try another word.'; return; }
    st.textContent = 'Tap a sticker to drop it on the canvas';
    grid.innerHTML = items.map(it => {
      const u = (it.url || '').replace(/['"]/g, '');
      const a = (it.anim_url || '').replace(/['"]/g, '');
      const p = (it.preview || '').replace(/['"]/g, '');
      const t = (it.title || '').replace(/['"]/g, '');
      return '<button class="asset-cell" data-url="' + u + '" data-anim="' + a + '" title="' + t + '" '
        + "style=\"border:0;padding:0;cursor:pointer;aspect-ratio:1/1;border-radius:8px;"
        + "background:#0d0f14 center/contain no-repeat;background-image:url('" + p + "')\"></button>";
    }).join('');
    grid.querySelectorAll('.asset-cell').forEach(el =>
      el.addEventListener('click', () => addAssetOverlay(el.dataset.url, el, el.dataset.anim)));
  } catch (e) {
    const m = '' + e;
    st.textContent = /not configured|key|banned|rejected/i.test(m)
      ? 'Stickers need the Giphy key (GIPHY_API_KEY).' : 'Search failed: ' + m;
  }
}
async function addAssetOverlay(url, el, animUrl) {
  if (!url) return;
  const st = document.getElementById('asset-status');
  if (el) el.style.opacity = '.5';
  try {
    await ensureStudio();
    const img = await _addImage('/api/stickers/asset_proxy?u=' + encodeURIComponent(url), { cover: false });
    // Remember the animated source: on a video export this layer is sent as a
    // looping GIF overlay (it keeps animating) instead of being flattened.
    if (img && animUrl) img.animUrl = animUrl;
    if (st) st.textContent = img ? '✓ Added — drag / resize on the canvas' : 'Could not add that one.';
    if (img && tg && tg.HapticFeedback) { try { tg.HapticFeedback.impactOccurred('light'); } catch (_) {} }
  } catch (e) { if (st) st.textContent = '❌ ' + e; }
  finally { if (el) el.style.opacity = ''; }
}
document.getElementById('asset-go').addEventListener('click', assetSearch);
document.getElementById('asset-q').addEventListener('keydown', e => {
  if (e.key === 'Enter') { e.preventDefault(); assetSearch(); }
});
document.getElementById('studio-cutout').addEventListener('click', studioCutout);
document.getElementById('studio-fill').addEventListener('input', e => {
  const t = _studioActiveText(); if (t) { t.set('fill', e.target.value); fcanvas.renderAll(); }
});
document.getElementById('studio-fill').addEventListener('change', _snap);
document.getElementById('studio-stroke').addEventListener('input', e => {
  const t = _studioActiveText(); if (t) { t.set('stroke', e.target.value); fcanvas.renderAll(); }
});
document.getElementById('studio-stroke').addEventListener('change', _snap);
document.getElementById('studio-font').addEventListener('change', e => {
  const t = _studioActiveText(); if (t) { t.set('fontFamily', e.target.value); fcanvas.renderAll(); _snap(); }
});
document.getElementById('studio-bold').addEventListener('click', () => {
  _studioBold = !_studioBold;
  document.getElementById('studio-bold').classList.toggle('on', _studioBold);
  const t = _studioActiveText();
  if (t) { t.set('fontWeight', _studioBold ? 'bold' : 'normal'); fcanvas.renderAll(); _snap(); }
});
document.getElementById('studio-outline-toggle').addEventListener('click', () => {
  _outline = !_outline;
  document.getElementById('studio-outline-toggle').classList.toggle('on', _outline);
});
document.getElementById('studio-outline-width').addEventListener('input', e => {
  document.getElementById('studio-outline-w-l').textContent = e.target.value + 'px';
});
document.getElementById('studio-undo').addEventListener('click', studioUndo);
document.getElementById('studio-redo').addEventListener('click', studioRedo);
document.getElementById('studio-del').addEventListener('click', () => {
  if (!fcanvas) return;
  const o = fcanvas.getActiveObject();
  if (o) { fcanvas.remove(o); fcanvas.renderAll(); }
});
document.getElementById('studio-front').addEventListener('click', () => {
  const o = fcanvas && fcanvas.getActiveObject();
  if (o) { fcanvas.bringToFront(o); fcanvas.renderAll(); _snap(); }
});
document.getElementById('studio-back').addEventListener('click', () => {
  const o = fcanvas && fcanvas.getActiveObject();
  if (o) { fcanvas.sendToBack(o); fcanvas.renderAll(); _snap(); }
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
