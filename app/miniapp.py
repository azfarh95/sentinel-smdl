"""Telegram Mini App — owner-only dashboard for SM-DL.

Mounted at /app (HTML) + /api/miniapp/* (JSON).

Features (v1):
  • Recent downloads list (from url_cache)
  • Stream watchlist add/remove (delegates to stream_monitor)
  • Start/stop live recordings (delegates to recorder_bridge.bridge)
  • Supported & configured platforms (from live_downloader registry + config)

Auth: validates Telegram WebApp initData (HMAC-SHA256 with bot token).
Owner-only — initData.user.id must match config.OWNER_CHAT_ID.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import secrets
import time
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import parse_qsl

import aiosqlite
from fastapi import APIRouter, HTTPException, Request, Header
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel

from . import config as _cfg
from . import database as _db
from . import stream_monitor
from . import auth as _auth
from .database import DB_PATH
from .live_downloader import (
    _PLATFORM_LABELS,    # we read but don't mutate
)
from .recorder_bridge import bridge

CONFIG_FILE = os.environ.get("CONFIG_FILE", "/config/smdl.json")
DOWNLOADS_DIR = os.environ.get("DOWNLOADS_DIR", "/downloads")

# config.py exposes UPPER_SNAKE module constants, not a .get() function.
# Some keys also rename between the JSON schema and the module constants.
_KEY_TO_ATTR = {
    "max_concurrent_downloads": "MAX_CONCURRENT",
    # everything else: key.upper() works
}


def _cfg_get(key: str, default=None):
    """Read a config value. Order: JSON file (so UI edits are live) → module
    constant (loaded at import) → caller-provided default."""
    file_cfg = _read_config_file_safe()
    if key in file_cfg:
        return file_cfg[key]
    attr = _KEY_TO_ATTR.get(key, key.upper())
    return getattr(_cfg, attr, default)


def _read_config_file_safe() -> dict:
    """Forward-declared shim so _cfg_get can call into the file reader
    defined later in the module."""
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            import json as _j
            return _j.load(f)
    except Exception:
        return {}

logger = logging.getLogger(__name__)

router = APIRouter()

# ── Telegram initData validation ─────────────────────────────────────────────


def _validate_init_data(init_data: str, bot_token: str, max_age_s: int = 3600) -> dict:
    """Parse + verify Telegram WebApp initData.

    Returns the parsed payload dict (with 'user' nested as dict) on success.
    Raises HTTPException(401) on failure.
    """
    if not init_data:
        raise HTTPException(status_code=401, detail="missing initData")
    try:
        pairs = dict(parse_qsl(init_data, strict_parsing=False))
    except Exception:
        raise HTTPException(status_code=401, detail="malformed initData")

    received_hash = pairs.pop("hash", None)
    if not received_hash:
        raise HTTPException(status_code=401, detail="initData hash missing")

    # Reconstruct data-check-string (sorted by key, joined by \n)
    data_check = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret_key, data_check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(received_hash, expected):
        raise HTTPException(status_code=401, detail="initData signature invalid")

    # Freshness — Telegram's auth_date is unix seconds
    try:
        auth_date = int(pairs.get("auth_date", "0"))
    except ValueError:
        auth_date = 0
    if auth_date and (time.time() - auth_date) > max_age_s:
        raise HTTPException(status_code=401, detail="initData expired")

    # Parse user
    user = {}
    if "user" in pairs:
        try:
            user = json.loads(pairs["user"])
        except Exception:
            pass
    pairs["user"] = user
    return pairs


def _is_owner(user_id: int) -> bool:
    owner = _cfg_get("owner_chat_id")
    return owner is not None and int(user_id) == int(owner)


def _allowed_users() -> set[int]:
    """User IDs allowed in the Mini App: owner ∪ ALLOWED_CHAT_IDS."""
    out: set[int] = set()
    owner = _cfg_get("owner_chat_id")
    if owner is not None:
        try: out.add(int(owner))
        except Exception: pass
    for u in (_cfg.ALLOWED_CHAT_IDS or set()):
        try: out.add(int(u))
        except Exception: pass
    return out


async def _check_access(payload: dict) -> int:
    """Returns the caller's user_id if authorised. Routes the decision through
    auth.classify() so the Mini App and the bot agree on who's in/out.
    Raises 403 for banned + unseen users, 503 for admin-only-mode lockout."""
    user_id = (payload.get("user") or {}).get("id")
    if not user_id:
        raise HTTPException(status_code=403, detail="no user in initData")
    decision = await _auth.classify(int(user_id))
    if decision == "allow":
        return int(user_id)
    if decision == "deny_admin_only":
        raise HTTPException(status_code=503,
                            detail="Service is in admin-only mode.")
    if decision == "deny_pending":
        raise HTTPException(status_code=403,
                            detail="Your access is pending owner approval. Send /start to the bot for an approval code.")
    if decision == "deny_unknown":
        raise HTTPException(status_code=403,
                            detail="No bot interaction yet. Send /start to the bot first.")
    # Any banned status (or unexpected denial)
    raise HTTPException(status_code=403,
                        detail="Access denied.")


def _require_owner(payload: dict) -> int:
    """Owner-only guard for settings + onedrive routes."""
    user_id = (payload.get("user") or {}).get("id")
    if not user_id or not _is_owner(int(user_id)):
        raise HTTPException(status_code=403, detail="owner-only")
    return int(user_id)


# ── APK cookie auth (shared across all *.az-sentinel.xyz subdomains) ─────────
# Same scheme is implemented in sentinel-vpn-dashboard/app.py and
# sentinel-miniapp-v2/bridge.py. With the cookie set on .az-sentinel.xyz,
# the Mini App can be opened directly from the Suite APK (no Telegram wrapper).
OWNER_AUTH_TOKEN = os.environ.get("OWNER_AUTH_TOKEN", "")
COOKIE_NAME      = "sentinel_apk_session"
COOKIE_DOMAIN    = ".az-sentinel.xyz"
COOKIE_TTL_SEC   = 90 * 24 * 3600


def _issue_apk_cookie() -> str:
    ts    = str(int(time.time()))
    nonce = secrets.token_urlsafe(16)
    body  = f"{ts}.{nonce}"
    sig   = hmac.new(OWNER_AUTH_TOKEN.encode(), body.encode(), hashlib.sha256).hexdigest()
    return f"{body}.{sig}"


def _verify_apk_cookie(val: str) -> bool:
    """Legacy v1 cookie check — kept for backwards compat with anything
    still calling it directly. New code should use _parse_session_cookie
    (which handles both v1 and v2 via the auth_v2 helper)."""
    if not val or not OWNER_AUTH_TOKEN:
        return False
    try:
        body, sig = val.rsplit(".", 1)
        ts_s, _   = body.split(".", 1)
        expected  = hmac.new(OWNER_AUTH_TOKEN.encode(), body.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, sig):
            return False
        return (time.time() - int(ts_s)) < COOKIE_TTL_SEC
    except Exception:
        return False


def _parse_session_cookie(val: str) -> dict | None:
    """Return the v2 auth_v2-parsed payload if the cookie is valid + not
    expired, else None. Handles BOTH v1 (legacy owner-only, scopes=['*'])
    and v2 (scoped beta users). Per auth-perms-v2 §6."""
    if not val or not OWNER_AUTH_TOKEN:
        return None
    try:
        from .auth_v2 import parse_session_cookie
        payload = parse_session_cookie(val, OWNER_AUTH_TOKEN)
    except Exception:
        return None
    if payload.get("expired"):
        return None
    return payload


def _owner_payload_from_cookie(session: dict | None = None) -> dict:
    """Synthesise the FastAPI-route payload that downstream guards expect.
    Mirrors the shape of a real initData payload (user.id) and additionally
    embeds the parsed session (auth_v2) at payload['session'] so per-route
    require_scope() checks can read it without re-parsing the cookie."""
    owner = _cfg_get("owner_chat_id")
    if owner is None:
        owner = os.environ.get("OWNER_CHAT_ID", "")
    out: dict = {"user": {"id": int(owner)} if owner else {}}
    if session is not None:
        out["session"] = session
    return out


async def _verify(request: Request) -> dict:
    """Common request guard: HMAC validation + allowed-user check. Owner-only
    routes must call _require_owner(payload) themselves on top of this.

    Auth precedence:
      1. Cookie `sentinel_apk_session` (v1 owner or v2 scoped) — APK path
      2. `X-Init-Data` header (Telegram WebApp) — Mini App path

    Returns a payload dict with `user.id` plus (for cookie-auth paths) a
    `session` field carrying the parsed v1/v2 cookie — used by
    require_scope() to enforce per-route permissions."""
    # Path 1 — session cookie (v1 owner OR v2 scoped beta user).
    cookie_val = request.cookies.get(COOKIE_NAME, "")
    session = _parse_session_cookie(cookie_val)
    if session is not None:
        payload = _owner_payload_from_cookie(session)
        if payload["user"].get("id"):
            return payload
        # If owner_chat_id isn't configured we can't synthesise the
        # Telegram-style user.id — fall through to initData path.

    # Path 2 — Telegram initData (Mini App opened from inside Telegram).
    # bot.py reads SMDL_BOT_TOKEN — keep this in sync. Fall back to the
    # generic names for cross-deployment portability.
    bot_token = (
        os.environ.get("SMDL_BOT_TOKEN")
        or os.environ.get("BOT_TOKEN")
        or os.environ.get("TELEGRAM_BOT_TOKEN")
        or ""
    )
    if not bot_token:
        raise HTTPException(status_code=503, detail="bot token not configured")
    init_data = request.headers.get("x-init-data") or ""
    payload = _validate_init_data(init_data, bot_token)
    await _check_access(payload)
    # initData auth implies owner — synthesise a wildcard session so
    # require_scope() lets everything through.
    payload["session"] = {
        "version": "initdata", "user_id": "owner",
        "scopes": ["*"], "jti": "", "iat": 0, "expired": False,
    }
    return payload


def require_scope(payload: dict, scope: str) -> None:
    """Per-route scope enforcement. Raises HTTPException(403) if the
    payload's session doesn't grant the required scope. No-op for
    payloads with the wildcard '*' (owner cookie, initData)."""
    from .auth_v2 import require_scope as _rs
    session = payload.get("session") or {"scopes": ["*"]}
    _rs(session, scope)


# ── Helpers ──────────────────────────────────────────────────────────────────


async def _list_recent_downloads(limit: int = 50,
                                   chat_id: Optional[int] = None) -> list[dict]:
    """Return the most recent N entries the user can see.

    When `chat_id` is provided (typical Mini App flow), reads from
    download_history filtered to that user — matching the semantics of
    /downloads/clear which wipes per-user history. Without chat_id (e.g.
    legacy callers / boot smoke), falls back to url_cache global view.

    Bug history (2026-05-28): Recent Downloads displayed url_cache
    (global content cache, never user-filtered) while Clear wiped
    download_history (per-user). Result: user clicks Clear → sees
    "Cleared N rows" → list reappears because url_cache wasn't touched.
    The fix is to source the display from the same table Clear acts on.
    """
    out = []
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        if chat_id is not None:
            sql = ("SELECT url, files, platform, uploader, downloaded_at AS created_at "
                   "FROM download_history WHERE chat_id = ? "
                   "ORDER BY downloaded_at DESC LIMIT ?")
            params: tuple = (chat_id, limit)
        else:
            sql = ("SELECT url, files, platform, uploader, created_at "
                   "FROM url_cache ORDER BY created_at DESC LIMIT ?")
            params = (limit,)
        async with db.execute(sql, params) as cur:
            async for row in cur:
                d = dict(row)
                try: d["files"] = json.loads(d.get("files") or "[]")
                except Exception: d["files"] = []
                out.append(d)
    return out


def _list_platforms() -> dict:
    """Return supported (from live_downloader) + configured (from config) platforms."""
    configured_live = list(_cfg_get("live_platforms") or [])
    # Registered labels — flatten host_substrings + label
    registered = [
        {"label": label, "hosts": list(hosts)}
        for hosts, label in _PLATFORM_LABELS
    ]
    return {
        "configured_for_live": configured_live,
        "registered_labels": registered,
        # Anything yt-dlp's 1700+ extractors recognise is technically supported,
        # but only those matching a registered label render a friendly name.
        "note": (
            "yt-dlp covers 1700+ sites. The labels list below names the most common; "
            "other URLs route through 'other' but still work if yt-dlp has an extractor."
        ),
    }


def _job_to_dict(job) -> dict:
    return {
        "job_id": job.job_id,
        "chat_id": job.chat_id,
        "url": job.url,
        "platform": job.platform,
        "uploader": job.uploader,
        "started_at": job.started_at,
        "elapsed_sec": int(time.time() - job.started_at),
        "bytes": job.bytes_downloaded,
        "filepath": job.filepath,
        "stop_requested_at": job.stop_requested_at,
        "abort_reason": job.abort_reason,
    }


# ── Request models ───────────────────────────────────────────────────────────


class WatchAddBody(BaseModel):
    url: str
    label: Optional[str] = None


class StreamStartBody(BaseModel):
    url: str


class StreamStopBody(BaseModel):
    chat_id: Optional[int] = None   # default: owner's chat


# ── Routes ───────────────────────────────────────────────────────────────────


@router.get("/api/miniapp/whoami")
async def whoami(request: Request):
    p = await _verify(request)
    uid = int(p["user"]["id"])
    return {
        "user": p.get("user"),
        "owner_chat_id": _cfg_get("owner_chat_id"),
        "is_owner": _is_owner(uid),
        "allowed_users_count": len(_allowed_users()),
    }


SHARE_SIZE_THRESHOLD = 50 * 1024 * 1024  # 50 MB


def _enrich_with_share_url(row: dict) -> dict:
    """If the row represents a live recording or a large download, attach
    a signed share URL + size, so the Mini App can render a tappable link
    that streams over the public tunnel. Small reels/photos get nothing —
    Telegram already delivered them inline; no actionable Mini App link."""
    from pathlib import Path as _P
    from .file_serve import sign_share_url, DOWNLOADS_DIR as _DLDIR
    files = row.get("files") or []
    if not files:
        return row
    first = files[0]
    try:
        p = _P(first)
        if not p.exists():
            return row
        norm = first.replace("\\", "/")
        is_live = "/live/" in norm
        size = p.stat().st_size
        if is_live or size >= SHARE_SIZE_THRESHOLD:
            try:
                rel = str(p.relative_to(_DLDIR))
            except ValueError:
                # File not under DOWNLOADS_DIR (shouldn't happen but be safe).
                return row
            url = sign_share_url(rel)
            if url:
                row["share_url"] = url
                row["size_mb"]   = round(size / 1024**2, 1)
                row["is_live_recording"] = is_live
    except Exception as _e:
        logger.debug("share_url enrich failed for %s: %s", first, _e)
    return row


# ── Stremio module (P1 + P3) ─────────────────────────────────────────────────
# Backend endpoints for the Stremio Mini App tile. The actual UI is a
# Svelte sub-app under static/stremio/ — these endpoints feed it.
#
# Auth: same _verify() initData gate as the rest of /api/miniapp/*.
# Authorisation: owner-only (RD token + G:\ writes shouldn't be exposed
# to allowed-users until we add per-user budgets).

@router.get("/api/miniapp/stremio/account")
async def stremio_account(request: Request):
    """Real-Debrid account check — token validity, premium days remaining.
    Used by the Mini App settings page + as a boot health check."""
    p = await _verify(request)
    _require_owner(p)
    from . import realdebrid as _rd
    try:
        a = _rd.get_account()
    except _rd.RealDebridError as e:
        return {"ok": False, "error": str(e)}
    return {
        "ok": True,
        "username": a.username,
        "email": a.email,
        "type": a.type,
        "is_premium": a.is_premium,
        "expiration": a.expiration_iso,
        "days_left": round(a.premium_seconds_left / 86400, 1),
        "points": a.points,
    }


@router.get("/api/miniapp/stremio/search")
async def stremio_search(request: Request, q: str = "", type: str = "movie",
                          limit: int = 24):
    """Cinemeta search. Returns a list of MetaItems (id, name, year, poster,
    imdb_rating, genres). The Svelte UI renders these as poster tiles."""
    p = await _verify(request)
    _require_owner(p)
    from . import stremio as _st
    q = (q or "").strip()
    if not q:
        return {"results": []}
    try:
        items = await asyncio.to_thread(_st.search, q, type, None, limit)
    except Exception as e:
        logger.exception("stremio search failed")
        raise HTTPException(500, f"search failed: {e!s}")
    return {"results": [
        {"id": m.id, "type": m.type, "name": m.name, "year": m.year,
         "poster": m.poster, "description": m.description,
         "imdb_rating": m.imdb_rating, "genres": m.genres}
        for m in items
    ]}


@router.get("/api/miniapp/stremio/episodes")
async def stremio_episodes(request: Request, imdb_id: str = ""):
    """Series episode list. Used by the Detail view when type='series'.

    Returns episodes sorted (S1E1, S1E2, ..., S2E1, ...) — each with the
    Stremio addon `id` (e.g. 'tt0903747:1:1') ready to feed into
    /streams for resolution."""
    p = await _verify(request)
    _require_owner(p)
    from . import stremio as _st
    imdb_id = (imdb_id or "").strip()
    if not imdb_id.startswith("tt"):
        raise HTTPException(400, "imdb_id must start with 'tt'")
    try:
        eps = await asyncio.to_thread(_st.get_series_episodes, imdb_id, None)
    except Exception as e:
        logger.exception("stremio episodes failed")
        raise HTTPException(500, f"episodes failed: {e!s}")
    return {"episodes": [
        {"id": e.id, "season": e.season, "episode": e.episode,
         "title": e.title, "released": e.released, "overview": e.overview,
         "thumbnail": e.thumbnail, "runtime": e.runtime}
        for e in eps
    ]}


@router.get("/api/miniapp/stremio/streams")
async def stremio_streams(request: Request, imdb_id: str = "",
                           type: str = "movie",
                           quality: str = "1080p"):
    """Fan out across stream-provider addons (Torrentio/Comet/MediaFusion),
    re-rank by preferred quality + seeders, return the top N for the UI."""
    p = await _verify(request)
    _require_owner(p)
    from . import stremio as _st
    imdb_id = (imdb_id or "").strip()
    if not imdb_id.startswith("tt"):
        raise HTTPException(400, "imdb_id must start with 'tt'")
    try:
        raw = await asyncio.to_thread(_st.get_streams, imdb_id, type, None)
    except Exception as e:
        logger.exception("stremio streams failed")
        raise HTTPException(500, f"streams failed: {e!s}")
    ranked = _st.rank_streams(raw, preferred_quality=quality)
    return {"streams": [
        {"title": s.title, "infohash": s.infohash, "has_magnet": bool(s.magnet),
         "size_bytes": s.size_bytes, "seeders": s.seeders, "quality": s.quality,
         "source_addon": s.source_addon, "file_index": s.file_index}
        for s in ranked[:40]
    ]}


class _StremioGrabBody(BaseModel):
    infohash: Optional[str] = None
    magnet: Optional[str] = None
    title: Optional[str] = None
    file_index: Optional[int] = None


@router.post("/api/miniapp/stremio/grab")
async def stremio_grab(body: _StremioGrabBody, request: Request):
    """Resolve a magnet/infohash through Real-Debrid → return the direct
    streamable URL(s). Caller then either feeds the URL into <video> for
    immediate playback or hands it to the SMDL download manager for
    cache-to-G:\ (P5).

    Long-poll: RD can take 30s–5min for uncached torrents. The UI should
    show a spinner with the RD progress (P4 will surface that via a
    separate status endpoint)."""
    p = await _verify(request)
    _require_owner(p)
    from . import stremio as _st  # for infohash → magnet helper
    from . import realdebrid as _rd

    magnet = body.magnet
    if not magnet and body.infohash:
        magnet = f"magnet:?xt=urn:btih:{body.infohash.lower()}"
    if not magnet:
        raise HTTPException(400, "either magnet or infohash required")

    try:
        files = await asyncio.to_thread(_rd.magnet_to_direct_urls, magnet,
                                          timeout=300)
    except _rd.RealDebridError as e:
        return {"ok": False, "error": str(e)}
    return {
        "ok": True,
        "files": [
            {"filename": f.filename, "filesize": f.filesize,
             "direct_url": f.direct_url, "mime_type": f.mime_type}
            for f in files
        ],
    }


# ── Theater P7 — Settings + resume position routes ─────────────────────────

@router.get("/api/miniapp/stremio/settings")
async def stremio_settings_get(request: Request):
    p = await _verify(request)
    _require_owner(p)
    from . import stremio_settings as _ss
    return {"settings": await _ss.get_all()}


class _StremioSettingsPatch(BaseModel):
    default_quality: Optional[str] = None
    cache_max_gb: Optional[float] = None
    addons: Optional[list[str]] = None
    auto_grab_top_seeded: Optional[bool] = None


@router.post("/api/miniapp/stremio/settings")
async def stremio_settings_set(body: _StremioSettingsPatch, request: Request):
    p = await _verify(request)
    _require_owner(p)
    from . import stremio_settings as _ss
    patch = {k: v for k, v in body.model_dump().items() if v is not None}
    return {"settings": await _ss.update(patch)}


class _StremioPositionBody(BaseModel):
    imdb_id: str
    position_seconds: float
    duration_seconds: Optional[float] = None


@router.post("/api/miniapp/stremio/position")
async def stremio_position_save(body: _StremioPositionBody, request: Request):
    """Persist playback position for resume. Frontend fires this on
    timeupdate (throttled) and on player pause/close."""
    p = await _verify(request)
    _require_owner(p)
    from . import stremio_settings as _ss
    await _ss.save_position(body.imdb_id, body.position_seconds, body.duration_seconds)
    return {"ok": True}


@router.get("/api/miniapp/stremio/position/{imdb_id:path}")
async def stremio_position_get(imdb_id: str, request: Request):
    """Read last position so the player can `currentTime = X` on load."""
    p = await _verify(request)
    _require_owner(p)
    from . import stremio_settings as _ss
    pos = await _ss.get_position(imdb_id)
    return {"position": pos}


# ── Theater P6 — Trakt sync routes ─────────────────────────────────────────

@router.get("/api/miniapp/stremio/trakt/status")
async def stremio_trakt_status(request: Request):
    """Connected? Token valid? Days until refresh."""
    p = await _verify(request)
    _require_owner(p)
    from . import trakt as _t
    tok = _t.load_token()
    if not tok:
        return {"connected": False}
    return {
        "connected": True,
        "expires_at": tok.expires_at,
        "expires_in_days": round((tok.expires_at - int(__import__("time").time())) / 86400, 1),
        "scope": tok.scope,
    }


@router.post("/api/miniapp/stremio/trakt/connect/start")
async def stremio_trakt_connect_start(request: Request):
    """Kick off Trakt device-code OAuth. Returns the user_code +
    verification_url. UI shows these; user opens URL, types code; we
    poll /connect/poll until token comes back."""
    p = await _verify(request)
    _require_owner(p)
    from . import trakt as _t
    try:
        dc = await asyncio.to_thread(_t.device_code_init)
    except _t.TraktError as e:
        return {"ok": False, "error": str(e)}
    return {
        "ok": True,
        "device_code": dc.device_code,
        "user_code": dc.user_code,
        "verification_url": dc.verification_url,
        "expires_in": dc.expires_in,
        "interval": dc.interval,
    }


class _TraktPollBody(BaseModel):
    device_code: str


@router.post("/api/miniapp/stremio/trakt/connect/poll")
async def stremio_trakt_connect_poll(body: _TraktPollBody, request: Request):
    """Poll the device-code flow. UI calls every `interval` seconds.
    Returns {ok, status: 'pending'|'connected'|'error'}."""
    p = await _verify(request)
    _require_owner(p)
    from . import trakt as _t
    try:
        tok = await asyncio.to_thread(_t.device_code_check, body.device_code)
    except _t.TraktError as e:
        return {"ok": False, "status": "error", "error": str(e)}
    if tok is None:
        return {"ok": True, "status": "pending"}
    return {"ok": True, "status": "connected"}


@router.post("/api/miniapp/stremio/trakt/disconnect")
async def stremio_trakt_disconnect(request: Request):
    p = await _verify(request)
    _require_owner(p)
    from . import trakt as _t
    _t.clear_token()
    return {"ok": True}


class _TraktScrobbleBody(BaseModel):
    imdb_id: str
    type: str = "movie"            # 'movie' | 'series'
    season: Optional[int] = None
    episode: Optional[int] = None
    progress_pct: float = 0.0
    event: str = "start"            # 'start' | 'pause' | 'stop'


@router.post("/api/miniapp/stremio/trakt/scrobble")
async def stremio_trakt_scrobble(body: _TraktScrobbleBody, request: Request):
    """Fire a Trakt scrobble event. The frontend wires this to
    <video> play/pause/ended events so the user's Trakt timeline
    reflects Theater playback."""
    p = await _verify(request)
    _require_owner(p)
    from . import trakt as _t
    tok = _t.load_token()
    if not tok:
        return {"ok": False, "error": "trakt not connected"}
    try:
        tok = await asyncio.to_thread(_t.refresh_if_needed, tok)
        fn = {"start": _t.scrobble_start, "pause": _t.scrobble_pause,
              "stop": _t.scrobble_stop}.get(body.event)
        if fn is None:
            raise HTTPException(400, "event must be start|pause|stop")
        out = await asyncio.to_thread(
            fn, tok, imdb_id=body.imdb_id, type_=body.type,
            season=body.season, episode=body.episode,
            progress_pct=body.progress_pct,
        )
    except _t.TraktError as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True, "response": out}


@router.get("/api/miniapp/stremio/trakt/watchlist")
async def stremio_trakt_watchlist(request: Request, type: str = "movies"):
    """Render the user's Trakt watchlist inside Theater's Library tab."""
    p = await _verify(request)
    _require_owner(p)
    from . import trakt as _t
    tok = _t.load_token()
    if not tok:
        return {"ok": False, "error": "trakt not connected", "items": []}
    try:
        tok = await asyncio.to_thread(_t.refresh_if_needed, tok)
        data = await asyncio.to_thread(_t.watchlist, tok, type_=type)
    except _t.TraktError as e:
        return {"ok": False, "error": str(e), "items": []}
    # Map Trakt's shape to MetaItem-ish for the UI
    items = []
    for entry in (data or []):
        section = entry.get("movie") or entry.get("show") or {}
        ids = section.get("ids") or {}
        items.append({
            "id": ids.get("imdb") or "",
            "type": "movie" if "movie" in entry else "series",
            "name": section.get("title") or "",
            "year": section.get("year"),
        })
    return {"ok": True, "items": items}


# ── Stremio P4 — queue + cache routes ──────────────────────────────────────

class _StremioQueueBody(BaseModel):
    imdb_id: str
    type: str = "movie"
    title: str = ""
    infohash: Optional[str] = None
    magnet: Optional[str] = None
    file_index: Optional[int] = None
    source_stream_title: Optional[str] = None
    quality: Optional[str] = None
    expected_size: Optional[int] = None


@router.post("/api/miniapp/stremio/queue")
async def stremio_queue_enqueue(body: _StremioQueueBody, request: Request):
    """Enqueue a grab. Returns the new job_id. Caller polls /jobs/{id}
    until status becomes 'streaming' (direct_url available — playback
    can start) or 'cached' (file on disk — local play)."""
    p = await _verify(request)
    _require_owner(p)
    from . import stremio_queue as _sq
    magnet = body.magnet
    if not magnet and body.infohash:
        magnet = f"magnet:?xt=urn:btih:{body.infohash.lower()}"
    if not magnet or not body.infohash:
        raise HTTPException(400, "infohash (and ideally magnet) required")
    job_id = await _sq.enqueue(
        imdb_id=body.imdb_id, type_=body.type, title=body.title,
        infohash=body.infohash, magnet=magnet,
        file_index=body.file_index,
        source_stream_title=body.source_stream_title,
        quality=body.quality, expected_size=body.expected_size,
    )
    job = await _sq.get_job(job_id)
    return {"ok": True, "job_id": job_id, "job": _job_to_dict(job)}


@router.get("/api/miniapp/stremio/jobs")
async def stremio_jobs_list(request: Request, limit: int = 50):
    """Recent jobs across all states. Active first, then most recent."""
    p = await _verify(request)
    _require_owner(p)
    from . import stremio_queue as _sq
    jobs = await _sq.list_jobs(limit=limit)
    return {"jobs": [_job_to_dict(j) for j in jobs]}


@router.get("/api/miniapp/stremio/jobs/{job_id}")
async def stremio_jobs_get(job_id: int, request: Request):
    p = await _verify(request)
    _require_owner(p)
    from . import stremio_queue as _sq
    job = await _sq.get_job(job_id)
    if not job:
        raise HTTPException(404, "no such job")
    return {"job": _job_to_dict(job)}


@router.get("/api/miniapp/stremio/file/{infohash}")
async def stremio_file_stream(infohash: str, request: Request):
    """Range-served local file for a cached Stremio grab. Phones can
    seek mid-stream because we honour the HTTP Range header."""
    p = await _verify(request)
    _require_owner(p)
    from . import stremio_cache as _cache
    entry = _cache.find_by_infohash(infohash)
    if not entry or not entry.file_path.exists():
        raise HTTPException(404, "not cached")
    _cache.touch_last_played(infohash)
    return _serve_with_range(entry.file_path, entry.mime or "application/octet-stream",
                              request.headers.get("range"))


@router.get("/api/miniapp/stremio/cache")
async def stremio_cache_list(request: Request):
    """List everything in G:\\YT-DLP\\Stremio\\ — what's currently on disk.
    Used by the Library view to show 'cached' badges + click-to-rewatch."""
    p = await _verify(request)
    _require_owner(p)
    from . import stremio_cache as _cache
    entries = _cache.list_entries()
    total, used, free = _cache._disk_usage_bytes()
    return {
        "entries": [
            {"imdb_id": e.imdb_id, "infohash": e.infohash, "title": e.title,
             "filename": e.filename, "filesize": e.filesize, "mime": e.mime,
             "grabbed_at": e.grabbed_at, "last_played": e.last_played}
            for e in sorted(entries, key=lambda x: x.last_played, reverse=True)
        ],
        "disk": {"total": total, "used": used, "free": free,
                  "pct_used": (used / total * 100) if total else 0},
    }


# ── Helpers ────────────────────────────────────────────────────────────────

def _job_to_dict(job) -> dict:
    """StremioJob → dict for JSON response. Mirrors the dataclass shape."""
    if job is None:
        return {}
    return {
        "id": job.id, "imdb_id": job.imdb_id, "type": job.type,
        "title": job.title, "infohash": job.infohash,
        "file_index": job.file_index,
        "source_stream_title": job.source_stream_title,
        "quality": job.quality, "expected_size": job.expected_size,
        "status": job.status, "progress": job.progress,
        "direct_url": job.direct_url,
        "filename": job.filename, "filesize": job.filesize,
        "error": job.error,
        "created_at": job.created_at, "updated_at": job.updated_at,
    }


# ── #41: file-restructure migration (owner-only) ────────────────────────────
# Re-path the existing library to match the current download_path_template.
# Preview is read-only; apply journals every move so it can be rolled back.

@router.get("/api/miniapp/restructure/preview")
async def restructure_preview(request: Request, include_unmatched: bool = False,
                              limit: int = 400):
    p = await _verify(request)
    _require_owner(p)
    from . import restructure as rs
    from . import database as db
    from .downloader import DOWNLOADS_DIR
    template = _cfg_get("download_path_template") or "{platform}/{uploader}/{title}.{ext}"
    meta = await rs.build_metadata_index(db)
    plan = await asyncio.to_thread(
        rs.build_plan, template, DOWNLOADS_DIR, meta, include_unmatched)
    rel = lambda pth: os.path.relpath(pth, DOWNLOADS_DIR)  # noqa: E731
    # Surface the moves first (what the user cares about), then conflicts.
    ordered = ([it for it in plan if it.action == "move"]
               + [it for it in plan if it.action == "conflict"]
               + [it for it in plan if it.action in ("skip", "noop")])
    items = [{"action": it.action, "reason": it.reason, "matched": it.matched,
              "src": rel(it.src), "dst": rel(it.dst)} for it in ordered[:limit]]
    return {"ok": True, "template": template,
            "summary": rs.plan_summary(plan), "truncated": len(plan) > limit,
            "items": items}


@router.post("/api/miniapp/restructure/apply")
async def restructure_apply(request: Request):
    p = await _verify(request)
    _require_owner(p)
    from . import restructure as rs
    from . import database as db
    from .downloader import DOWNLOADS_DIR
    try:
        body = await request.json()
    except Exception:
        body = {}
    include_unmatched = bool(body.get("include_unmatched"))
    template = _cfg_get("download_path_template") or "{platform}/{uploader}/{title}.{ext}"
    meta = await rs.build_metadata_index(db)
    job_id = await rs.run_migration(template, DOWNLOADS_DIR, meta, include_unmatched)
    return {"ok": True, "job_id": job_id}


@router.get("/api/miniapp/restructure/progress/{job_id}")
async def restructure_progress(request: Request, job_id: str):
    p = await _verify(request)
    _require_owner(p)
    from fastapi.responses import StreamingResponse
    from . import restructure as rs

    async def _events():
        last = None
        while True:
            job = rs.get_job(job_id)
            if job is None:
                yield 'data: {"status":"unknown"}\n\n'
                return
            snap = json.dumps({k: job[k] for k in
                               ("status", "total", "done", "moved", "errors", "manifest")})
            if snap != last:
                yield f"data: {snap}\n\n"
                last = snap
            if job["status"] in ("done", "error"):
                return
            if await request.is_disconnected():
                return
            await asyncio.sleep(0.4)

    return StreamingResponse(_events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


@router.post("/api/miniapp/restructure/rollback")
async def restructure_rollback(request: Request):
    p = await _verify(request)
    _require_owner(p)
    from . import restructure as rs
    from .downloader import DOWNLOADS_DIR
    return await rs.rollback(DOWNLOADS_DIR)


def _serve_with_range(path, media_type: str, range_header: Optional[str]):
    """Tiny range-served file response. Phones (Stremio, native players,
    Chrome) issue Range requests; we honour them so seek works.

    Returns a Starlette/FastAPI streaming response."""
    from fastapi.responses import StreamingResponse, Response
    import re
    size = os.path.getsize(path)
    if not range_header:
        # Full body — but still advertise byte-range support so the next
        # seek-request picks up.
        def _stream_full():
            with open(path, "rb") as f:
                while True:
                    chunk = f.read(1024 * 1024)
                    if not chunk: break
                    yield chunk
        return StreamingResponse(_stream_full(), media_type=media_type,
                                   headers={"Accept-Ranges": "bytes",
                                              "Content-Length": str(size)})
    m = re.match(r"bytes=(\d*)-(\d*)", range_header)
    if not m:
        raise HTTPException(416, "bad Range")
    start_s, end_s = m.group(1), m.group(2)
    start = int(start_s) if start_s else 0
    end = int(end_s) if end_s else size - 1
    start = max(0, start); end = min(end, size - 1)
    if start > end:
        raise HTTPException(416, "Range not satisfiable")
    length = end - start + 1

    def _stream_range():
        with open(path, "rb") as f:
            f.seek(start)
            remaining = length
            while remaining > 0:
                chunk = f.read(min(1024 * 1024, remaining))
                if not chunk: break
                remaining -= len(chunk)
                yield chunk

    return StreamingResponse(
        _stream_range(), status_code=206, media_type=media_type,
        headers={
            "Accept-Ranges": "bytes",
            "Content-Range": f"bytes {start}-{end}/{size}",
            "Content-Length": str(length),
        },
    )


@router.post("/api/miniapp/downloads/clear")
async def downloads_clear(request: Request):
    """Wipe the current user's download history. Global url_cache is
    untouched (it's a content cache, not personal history)."""
    p = await _verify(request)
    require_scope(p, "smdl.downloader")
    uid = int(p["user"]["id"])
    n = await _db.clear_download_history(uid)
    return {"ok": True, "deleted": n}


@router.post("/api/miniapp/downloads/batch")
async def downloads_batch(request: Request):
    """Queue 1+ URLs for download independent of the Telegram bot.

    Body: {urls: ["...", ...]}  — accepts a list. Whitespace/newline
    splitting + dedup is done client-side; this endpoint validates shape
    and kicks off downloader.download() per URL as fire-and-forget tasks.
    The global _semaphore in downloader.py (MAX_CONCURRENT) backpressures
    them so the same cap that applies to bot-initiated downloads applies
    here. History rows are written to the DB by downloader.download() on
    completion — the existing GET /downloads endpoint picks them up.

    Returns immediately with {accepted, rejected, accepted_urls}. The UI
    polls GET /downloads to surface progress.
    """
    from . import downloader as _dl

    p = await _verify(request)
    require_scope(p, "smdl.downloader")
    uid = int(p["user"]["id"])
    is_owner_flag = bool(p.get("user", {}).get("is_owner"))

    body = await request.json()
    raw_urls = body.get("urls") or []
    if not isinstance(raw_urls, list):
        return JSONResponse({"detail": "urls must be a list"}, status_code=400)

    # Validate shape + dedup. Trust the client to have split on whitespace.
    seen: set[str] = set()
    accepted: list[str] = []
    rejected: list[dict] = []
    for u in raw_urls:
        if not isinstance(u, str):
            continue
        u = u.strip()
        if not u:
            continue
        if not (u.startswith("http://") or u.startswith("https://")):
            rejected.append({"url": u, "reason": "not http(s)"})
            continue
        if u in seen:
            continue
        seen.add(u)
        accepted.append(u)

    if not accepted:
        return JSONResponse({"detail": "no valid URLs"}, status_code=400)

    # Cap per-request to keep one user from queueing thousands at once.
    MAX_PER_REQUEST = 50
    if len(accepted) > MAX_PER_REQUEST:
        rejected.extend({"url": u, "reason": "exceeds per-request cap"}
                        for u in accepted[MAX_PER_REQUEST:])
        accepted = accepted[:MAX_PER_REQUEST]

    async def _run_one(url: str):
        try:
            res = await _dl.download(url=url, is_owner=is_owner_flag)
            if res.get("files"):
                try:
                    await _db.record_download(
                        chat_id=uid, url=url, files=res["files"],
                        platform=_dl._platform_from_url(url), uploader=None,
                    )
                except Exception:
                    logger.exception("paste-batch: history insert failed for %s", url)
            else:
                logger.warning("paste-batch: download returned no files for %s: %s",
                               url, res.get("error"))
        except Exception:
            logger.exception("paste-batch: download failed for %s", url)

    for u in accepted:
        asyncio.create_task(_run_one(u))

    return {
        "accepted": len(accepted),
        "rejected": len(rejected),
        "accepted_urls": accepted,
        "rejected_urls": rejected,
    }


@router.get("/api/miniapp/files/list")
async def files_list(request: Request, path: str = ""):
    """Browse the host's /downloads directory. Returns folders + files
    at the given relative path. Path is resolved against DOWNLOADS_DIR
    with the same traversal-safe logic as file_serve. Owner-only since
    this exposes the whole download tree."""
    from pathlib import Path as _Path
    from .file_serve import sign_share_url, DOWNLOADS_DIR

    p = await _verify(request)
    require_scope(p, "smdl.admin")
    _require_owner(p)

    root = _Path(DOWNLOADS_DIR).resolve()
    rel = (path or "").strip("/").replace("\\", "/")
    target = (root / rel).resolve() if rel else root
    # Path-traversal guard: target must be inside the downloads root.
    try:
        target.relative_to(root)
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid path")
    if not target.exists() or not target.is_dir():
        raise HTTPException(status_code=404, detail="not a directory")

    folders, files = [], []
    try:
        for entry in target.iterdir():
            # Skip hidden + the noisy backfill log
            if entry.name.startswith(".") or entry.name == "_backfill.log":
                continue
            try:
                st = entry.stat()
            except OSError:
                continue
            rel_entry = str(entry.relative_to(root)).replace("\\", "/")
            if entry.is_dir():
                folders.append({
                    "name":  entry.name,
                    "path":  rel_entry,
                    "type":  "dir",
                    "mtime": int(st.st_mtime),
                })
            elif entry.is_file():
                share_url = sign_share_url(rel_entry)
                files.append({
                    "name":      entry.name,
                    "path":      rel_entry,
                    "type":      "file",
                    "size":      st.st_size,
                    "mtime":     int(st.st_mtime),
                    "share_url": share_url,
                })
    except PermissionError:
        raise HTTPException(status_code=403, detail="permission denied")

    folders.sort(key=lambda d: d["name"].lower())
    files.sort(key=lambda d: d["mtime"], reverse=True)

    # Breadcrumbs: list of {name, path} from root → current
    crumbs = [{"name": "/", "path": ""}]
    if rel:
        parts = rel.split("/")
        acc = []
        for p_ in parts:
            acc.append(p_)
            crumbs.append({"name": p_, "path": "/".join(acc)})

    return {
        "cwd":     rel,
        "crumbs":  crumbs,
        "folders": folders,
        "files":   files,
    }


@router.get("/api/miniapp/files/thumb")
async def files_thumb(request: Request, path: str):
    """Cached thumbnail for image/video files under DOWNLOADS_DIR (#32).

    Auth: same `_verify()` gate as /files/list (cookie or initData). The
    browser sends the sentinel_apk_session cookie automatically when an
    <img src=...> is same-origin, which is the common case in the TWA /
    desktop wrapper / browser. Telegram Mini App context (no cookie + no
    way to add X-Init-Data to <img>) falls back to the emoji icon on the
    client side via the <img onerror>.

    Strong ETag + 30-day Cache-Control so once a tile is on screen, the
    browser doesn't re-fetch on every tab switch."""
    from pathlib import Path as _Path
    from fastapi.responses import FileResponse
    from .file_serve import DOWNLOADS_DIR
    from . import thumbnails as _thumbs

    p = await _verify(request)
    require_scope(p, "smdl.admin")
    _require_owner(p)

    root = _Path(DOWNLOADS_DIR).resolve()
    rel = (path or "").strip("/").replace("\\", "/")
    if not rel:
        raise HTTPException(status_code=400, detail="path required")
    abs_path = (root / rel).resolve()
    try:
        abs_path.relative_to(root)
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid path")
    if not abs_path.exists() or not abs_path.is_file():
        raise HTTPException(status_code=404, detail="not found")
    if not _thumbs.can_thumb(abs_path):
        raise HTTPException(status_code=415, detail="not thumbable")

    cache_root = root / ".thumbnails"
    thumb = await _thumbs.get_or_make_thumb(abs_path, cache_root)
    if thumb is None:
        raise HTTPException(status_code=415, detail="thumb generation failed")

    etag = f'"{thumb.stem}"'
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304)
    return FileResponse(
        thumb, media_type="image/jpeg",
        headers={
            "ETag":          etag,
            "Cache-Control": "public, max-age=2592000",
        },
    )


@router.get("/api/miniapp/downloads")
async def downloads(request: Request, limit: int = 50):
    """Per-user download history.

    Bug history (2026-05-28): previously fell back to the global
    `url_cache` when the user's history was empty AND they were the owner.
    That broke the Clear button — Clear wipes download_history; if history
    went empty, the fallback re-populated the view from url_cache, so the
    user saw "Cleared N rows" followed by the same entries reappearing.
    Now: empty history = empty view. Period.

    Large downloads + live recordings get a signed share URL attached so the
    Mini App can render a tappable link that streams over the public tunnel."""
    p = await _verify(request)
    require_scope(p, "smdl.downloader")
    uid = int(p["user"]["id"])
    rows = await _db.list_download_history(uid, limit=max(1, min(limit, 200)))
    rows = [_enrich_with_share_url(r) for r in rows]
    return {"items": rows, "count": len(rows), "user_id": uid}


def _enrich_watchlist_items(items: list[dict]) -> list[dict]:
    """Decorate each watchlist entry with `username`, `platform` (display
    name), and `status` ('live'/'offline'/'unknown', from the monitor's
    in-memory cache). `muted` is read off the entry itself."""
    statuses = stream_monitor.get_status_map()
    snoozed_threshold = time.time()
    out = []
    for e in items:
        url = e.get("url") or ""
        snoozed_until = int(e.get("snoozed_until") or 0)
        out.append({
            **e,
            "username":    stream_monitor.extract_username(url),
            "platform":    stream_monitor.extract_platform(url),
            "status":      statuses.get(url, "unknown"),
            "muted":       bool(e.get("muted")),
            "auto_record": bool(e.get("auto_record")),
            "snoozed":     snoozed_until > snoozed_threshold,
            "snoozed_until": snoozed_until or None,
        })
    # Sort group-first (platform), then username within group, both case-insensitive.
    out.sort(key=lambda x: ((x.get("platform") or "Other").lower(),
                            (x.get("username") or "").lower()))
    return out


def _active_by_url_for_user(uid: int, is_owner: bool) -> dict[str, dict]:
    """Map url → active-job dict for jobs visible to this user. Owner sees
    every job; non-owner sees only their own."""
    out: dict[str, dict] = {}
    for j in bridge.list_active():
        if not is_owner and j.chat_id != uid:
            continue
        out[j.url] = _job_to_dict(j)
    return out


@router.get("/api/miniapp/watchlist")
async def watchlist(request: Request):
    p = await _verify(request)
    require_scope(p, "smdl.streamtracker")
    uid = int(p["user"]["id"])
    is_own = _is_owner(uid)
    # Owner sees the global list; everyone else sees only their own entries.
    items = stream_monitor.list_watchlist(chat_id=None if is_own else uid)
    enriched = _enrich_watchlist_items(items)
    # Hide blocked-platform entries from non-owners (they still live in the
    # JSON file — owner can see + edit them, just shielded from regular users).
    if not is_own:
        bl = set(await _auth.get_site_blocklist())
        if bl:
            enriched = [w for w in enriched if w.get("platform") not in bl]
    return {
        "items":   enriched,
        "active":  _active_by_url_for_user(uid, is_own),
        "user_id": uid,
        "scope":   "all" if is_own else "mine",
    }


@router.post("/api/miniapp/watchlist/add")
async def watchlist_add(request: Request, body: WatchAddBody):
    p = await _verify(request)
    require_scope(p, "smdl.streamtracker")
    uid = int(p["user"]["id"])
    if not _is_owner(uid) and await _auth.is_platform_blocked(body.url):
        return JSONResponse({"ok": False,
                             "error": f"{stream_monitor.extract_platform(body.url)} is disabled by the admin."},
                            status_code=403)
    ok, msg = stream_monitor.add_to_watchlist(body.url, body.label, added_by=uid)
    if not ok:
        return JSONResponse({"ok": False, "error": msg}, status_code=400)
    items = stream_monitor.list_watchlist(chat_id=None if _is_owner(uid) else uid)
    return {"ok": True, "msg": msg, "items": _enrich_watchlist_items(items)}


@router.post("/api/miniapp/watchlist/remove")
async def watchlist_remove(request: Request, body: WatchAddBody):
    p = await _verify(request)
    require_scope(p, "smdl.streamtracker")
    uid = int(p["user"]["id"])
    # Owner can remove anything; non-owner can only remove their own entries.
    ok, msg = stream_monitor.remove_from_watchlist(body.url,
                                                    chat_id=None if _is_owner(uid) else uid)
    if not ok:
        return JSONResponse({"ok": False, "error": msg}, status_code=400)
    items = stream_monitor.list_watchlist(chat_id=None if _is_owner(uid) else uid)
    return {"ok": True, "msg": msg, "items": _enrich_watchlist_items(items)}


class WatchEditBody(BaseModel):
    url:     str          # current URL (identifier)
    new_url: Optional[str] = None
    label:   Optional[str] = None


class WatchMuteBody(BaseModel):
    url:   str
    muted: bool


@router.post("/api/miniapp/watchlist/edit")
async def watchlist_edit(request: Request, body: WatchEditBody):
    """Edit the URL or label of an existing entry. Used by the Mini App's
    inline edit dropdown — lets the user fix a typo without removing + re-adding."""
    p = await _verify(request)
    require_scope(p, "smdl.streamtracker")
    uid = int(p["user"]["id"])
    ok, msg = stream_monitor.update_watchlist_entry(
        body.url,
        new_url=(body.new_url.strip() if body.new_url else None) or None,
        label=body.label,
        chat_id=None if _is_owner(uid) else uid,
    )
    if not ok:
        return JSONResponse({"ok": False, "error": msg}, status_code=400)
    items = stream_monitor.list_watchlist(chat_id=None if _is_owner(uid) else uid)
    return {"ok": True, "msg": msg, "items": _enrich_watchlist_items(items)}


@router.post("/api/miniapp/watchlist/mute")
async def watchlist_mute(request: Request, body: WatchMuteBody):
    """Toggle the mute flag. Muted streamers are still polled (so the status
    dot stays current) but won't trigger Telegram LIVE prompts."""
    p = await _verify(request)
    require_scope(p, "smdl.streamtracker")
    uid = int(p["user"]["id"])
    ok, msg = stream_monitor.set_muted(
        body.url, body.muted,
        chat_id=None if _is_owner(uid) else uid,
    )
    if not ok:
        return JSONResponse({"ok": False, "error": msg}, status_code=400)
    items = stream_monitor.list_watchlist(chat_id=None if _is_owner(uid) else uid)
    return {"ok": True, "msg": msg, "items": _enrich_watchlist_items(items)}


class WatchAutoRecordBody(BaseModel):
    url: str
    auto_record: bool


@router.post("/api/miniapp/watchlist/auto_record")
async def watchlist_auto_record(request: Request, body: WatchAutoRecordBody):
    """Toggle the auto_record flag (#30). When true + not muted, the
    monitor skips the Yes/No DM prompt and fires record_live() directly
    on the OFFLINE→LIVE transition."""
    p = await _verify(request)
    require_scope(p, "smdl.streamtracker")
    uid = int(p["user"]["id"])
    ok, msg = stream_monitor.set_auto_record(
        body.url, body.auto_record,
        chat_id=None if _is_owner(uid) else uid,
    )
    if not ok:
        return JSONResponse({"ok": False, "error": msg}, status_code=400)
    items = stream_monitor.list_watchlist(chat_id=None if _is_owner(uid) else uid)
    return {"ok": True, "msg": msg, "items": _enrich_watchlist_items(items)}


class WatchBulkMuteBody(BaseModel):
    platform: str
    muted: bool


@router.post("/api/miniapp/watchlist/bulk_mute")
async def watchlist_bulk_mute(request: Request, body: WatchBulkMuteBody):
    """Mute or unmute every watchlist entry on a given platform (#30).
    Non-owners only affect their own entries."""
    p = await _verify(request)
    require_scope(p, "smdl.streamtracker")
    uid = int(p["user"]["id"])
    count, affected = stream_monitor.set_bulk_mute_by_platform(
        body.platform, body.muted,
        chat_id=None if _is_owner(uid) else uid,
    )
    items = stream_monitor.list_watchlist(chat_id=None if _is_owner(uid) else uid)
    return {
        "ok": True,
        "count": count,
        "affected": affected,
        "items": _enrich_watchlist_items(items),
    }


@router.get("/api/miniapp/active")
async def active_streams(request: Request):
    p = await _verify(request)
    require_scope(p, "smdl.streamtracker")
    uid = int(p["user"]["id"])
    jobs = bridge.list_active()
    # Non-owner sees only their own recording. Owner sees all.
    if not _is_owner(uid):
        jobs = [j for j in jobs if j.chat_id == uid]
    return {"items": [_job_to_dict(j) for j in jobs], "scope": "all" if _is_owner(uid) else "mine"}


@router.post("/api/miniapp/stream/stop")
async def stream_stop(request: Request, body: StreamStopBody):
    p = await _verify(request)
    require_scope(p, "smdl.streamtracker")
    uid = int(p["user"]["id"])
    target = int(body.chat_id) if body.chat_id else uid
    # Non-owner can only stop their own.
    if not _is_owner(uid) and target != uid:
        raise HTTPException(status_code=403, detail="cannot stop another user's recording")
    status = await bridge.stop(target)
    if status is None:
        return JSONResponse({"ok": False, "error": "no active job for this chat"}, status_code=404)
    return {"ok": True, "chat_id": target, "status": {
        "elapsed_seconds": status.elapsed_seconds,
        "bytes": status.bytes,
        "platform": status.platform,
        "uploader": status.uploader,
    }}


@router.post("/api/miniapp/stream/start")
async def stream_start(request: Request, body: StreamStartBody):
    p = await _verify(request)
    require_scope(p, "smdl.streamtracker")
    uid = int(p["user"]["id"])
    url = body.url.strip()
    if not url:
        return JSONResponse({"ok": False, "error": "url required"}, status_code=400)
    if not _is_owner(uid) and await _auth.is_platform_blocked(url):
        return JSONResponse({"ok": False,
                             "error": f"{stream_monitor.extract_platform(url)} is disabled by the admin."},
                            status_code=403)
    if bridge.has_job(uid):
        return JSONResponse({"ok": False, "error": "a recording is already active for this user"}, status_code=409)
    asyncio.create_task(bridge.record(chat_id=uid, url=url))
    return {"ok": True, "queued": True, "chat_id": uid, "url": url}


@router.get("/api/miniapp/sites")
async def sites(request: Request):
    """Compact, screenshot-clean view. Strips any adult-category names from
    the response (HIDDEN_FROM_SITES_TAB). Owner-management of those still
    happens in the Admin tab. Also drops non-owner-blocked platforms from
    the visible list."""
    p = await _verify(request)
    uid = int(p["user"]["id"])
    data = _list_platforms()

    # Always-redacted set (adult cam sites).
    redact = set(_auth.HIDDEN_FROM_SITES_TAB)
    # Plus blocklist filter for non-owners.
    if not _is_owner(uid):
        redact |= set(await _auth.get_site_blocklist())
    if redact:
        # registered_labels entries use lowercase label strings; HIDDEN set
        # uses TitleCase. Normalize for comparison.
        redact_lc = {x.lower() for x in redact}
        data["configured_for_live"] = [
            p for p in data.get("configured_for_live", [])
            if p.lower() not in redact_lc
        ]
        data["registered_labels"] = [
            lbl for lbl in data.get("registered_labels", [])
            if (lbl.get("label") or "").lower() not in redact_lc
        ]
    # The verbose yt-dlp note in _list_platforms() isn't shown anymore;
    # the compact UI renders its own one-liner. Return it anyway for any
    # downstream caller that depends on it.
    return data


class TestUrlBody(BaseModel):
    url: str


@router.post("/api/miniapp/test_url")
async def test_url(request: Request, body: TestUrlBody):
    """Classify a pasted URL: platform, live-recording eligibility, and
    whether it's available to the caller. Deliberately silent about adult-
    category platforms — they're treated as 'not available' without naming."""
    p = await _verify(request)
    require_scope(p, "smdl.downloader")
    uid = int(p["user"]["id"])
    url = (body.url or "").strip()
    if not url:
        return {"ok": False, "error": "Paste a URL to test."}

    platform_raw = stream_monitor.extract_platform(url)
    is_owner_user = _is_owner(uid)
    hidden = platform_raw in _auth.HIDDEN_FROM_SITES_TAB
    is_known = platform_raw in {n for _, n in stream_monitor._PLATFORM_MAP}
    live_supported = (
        platform_raw.lower() in {p.lower() for p in (_cfg_get("live_platforms") or [])}
    )

    # Redact platform name in the response if it's adult-category; the
    # availability/recognition info is still accurate (so owner gets the
    # truth, just without the name).
    platform_display = "private category" if hidden else (platform_raw or "other")

    # Availability: owner always passes the gate. Non-owner is subject to
    # the admin blocklist.
    if is_owner_user:
        available = True
        reason: Optional[str] = None
    else:
        blocked = await _auth.is_platform_blocked(url)
        available = not blocked
        reason = ("Not available on this account." if blocked else None)

    if is_known:
        return {
            "ok": True,
            "platform": platform_display,
            "recognised": True,
            "live_supported": live_supported,
            "available": available,
            "reason": reason,
        }

    # Unknown hostname — yt-dlp may still handle it. Don't claim certainty.
    return {
        "ok": True,
        "platform": platform_display,
        "recognised": False,
        "live_supported": False,
        "available": available,
        "reason": (reason or
                   "Unknown site. The bot will try yt-dlp's generic extractor "
                   "— it covers 1700+ sites, but some require cookies or fail."),
    }


# ── Settings / config ────────────────────────────────────────────────────────


# Subset of config keys we expose to the Mini App (numeric/string editable).
# Settings marked needs_restart=True are read once at module import — UI shows
# a "restart required" badge so the user knows.
EDITABLE_SETTINGS = [
    # `admin` flag = surfaces only in the Admin tab (owner-only writes).
    {"key": "max_concurrent_downloads", "label": "Max concurrent downloads",
     "type": "int", "min": 1, "max": 10, "needs_restart": True, "admin": True},
    {"key": "live_max_concurrent", "label": "Max concurrent live recordings",
     "type": "int", "min": 1, "max": 5, "needs_restart": True, "admin": True},
    {"key": "default_quality", "label": "Default download resolution",
     "type": "choice", "choices": ["best", "1080p", "720p", "480p", "360p"]},
    {"key": "live_max_height", "label": "Live recording max height (px, 0=source)",
     "type": "int", "min": 0, "max": 2160, "needs_restart": True, "admin": True},
    {"key": "temp_ttl_hours", "label": "Temp file TTL (hours)",
     "type": "int", "min": 1, "max": 168},
    {"key": "delete_after_send", "label": "Delete files after Telegram send",
     "type": "bool"},
    {"key": "monitor_poll_interval_seconds", "label": "Stream monitor poll interval (s)",
     "type": "int", "min": 60, "max": 3600, "needs_restart": True, "admin": True},
    {"key": "language", "label": "Language",
     "type": "choice", "choices": ["en", "ru"], "needs_restart": True, "admin": True},
    {"key": "timezone", "label": "Timezone (IANA name, e.g. Asia/Singapore)",
     "type": "choice",
     "choices": ["UTC", "Asia/Singapore", "Asia/Kuala_Lumpur", "Asia/Jakarta",
                 "Asia/Hong_Kong", "Asia/Tokyo", "Asia/Dubai", "Asia/Kolkata",
                 "Europe/London", "Europe/Berlin", "Europe/Moscow",
                 "America/New_York", "America/Los_Angeles", "Australia/Sydney"],
     "needs_restart": True, "admin": True},
    {"key": "onedrive_mode", "label": "OneDrive upload mode",
     "type": "choice",
     "choices": ["disabled", "auto_after_send", "on_demand"],
     "admin": True},
    {"key": "onedrive_folder", "label": "OneDrive base folder",
     "type": "string", "admin": True},
    {"key": "onedrive_delete_after_upload",
     "label": "Delete local file after successful OneDrive upload",
     "type": "bool", "admin": True},
    # #34 — user-editable path template. Tokens: {service} {platform}
    # {uploader} {title} {date} {ext}. Translated to yt-dlp's outtmpl
    # format in downloader.py via path_template.compile_template().
    # Default matches the historical hard-coded layout so existing files
    # don't need re-pathing.
    {"key": "download_path_template",
     "label": "Download path template (tokens: {service} {platform} {uploader} {title} {date} {ext})",
     "type": "string", "default": "{platform}/{uploader}/{title}.{ext}"},
]


def compile_path_template(template: str, service: str = "ytdlp") -> str:
    """Translate the user-friendly template into yt-dlp's outtmpl format.

    Tokens (in `template`):  yt-dlp expansion:
      {service}              → static service name (ytdlp / iptv / live)
      {platform}             → %(extractor)s
      {uploader}             → %(uploader,uploader_id)s
      {title}                → %(title).80s
      {date}                 → %(upload_date)s   (YYYYMMDD)
      {ext}                  → %(ext)s

    Anything that doesn't match a token passes through unchanged, so a
    user can hard-code static segments like "Photos/" in their template.
    Returns just the path portion (caller prepends out_dir + leading
    slash).
    """
    if not template:
        template = "{platform}/{uploader}/{title}.{ext}"
    mapping = {
        "{service}":  (service or "ytdlp").upper(),
        "{platform}": "%(extractor)s",
        "{uploader}": "%(uploader,uploader_id)s",
        "{title}":    "%(title).80s",
        "{date}":     "%(upload_date)s",
        "{ext}":      "%(ext)s",
    }
    out = template
    for k, v in mapping.items():
        out = out.replace(k, v)
    return out.lstrip("/")


def _read_config_file() -> dict:
    """Read the smdl.json file. Returns empty dict if missing."""
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except Exception as e:
        logger.warning("config read failed: %s", e)
        return {}


def _write_config_file(updates: dict) -> dict:
    """Merge `updates` into smdl.json and write atomically. Returns the merged config."""
    try:
        from pathlib import Path
        cfg_path = Path(CONFIG_FILE)
        cfg_path.parent.mkdir(parents=True, exist_ok=True)
        current = _read_config_file()
        current.update(updates)
        tmp = cfg_path.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(current, f, indent=2, sort_keys=True)
        tmp.replace(cfg_path)
        return current
    except Exception as e:
        logger.error("config write failed: %s", e)
        raise HTTPException(status_code=500, detail=f"config write failed: {e}")


def _disk_usage_gb(path: str) -> dict:
    try:
        import shutil
        total, used, free = shutil.disk_usage(path)
        return {"total_gb": round(total / 1024**3, 1),
                "used_gb":  round(used  / 1024**3, 1),
                "free_gb":  round(free  / 1024**3, 1)}
    except Exception:
        return {"total_gb": None, "used_gb": None, "free_gb": None}


@router.get("/api/miniapp/config")
async def get_config(request: Request):
    await _verify(request)
    # Current values come from the loaded _cfg module (single source of truth).
    current_values = {}
    for s in EDITABLE_SETTINGS:
        current_values[s["key"]] = _cfg_get(s["key"])
    return {
        "settings": EDITABLE_SETTINGS,
        "values": current_values,
        "paths": {
            "downloads_dir": DOWNLOADS_DIR,
            "downloads_dir_writable": os.access(DOWNLOADS_DIR, os.W_OK) if os.path.exists(DOWNLOADS_DIR) else False,
            "config_file": CONFIG_FILE,
        },
        "disk": _disk_usage_gb(DOWNLOADS_DIR),
    }


class ConfigUpdateBody(BaseModel):
    updates: dict


@router.post("/api/miniapp/config")
async def update_config(request: Request, body: ConfigUpdateBody):
    p = await _verify(request)
    _require_owner(p)
    # Validate each update against EDITABLE_SETTINGS
    schema = {s["key"]: s for s in EDITABLE_SETTINGS}
    validated = {}
    errors = []
    needs_restart = []
    for k, v in (body.updates or {}).items():
        if k not in schema:
            errors.append(f"{k}: not editable")
            continue
        s = schema[k]
        try:
            if s["type"] == "int":
                vv = int(v)
                if vv < s.get("min", -10**9) or vv > s.get("max", 10**9):
                    raise ValueError(f"out of range [{s.get('min')}, {s.get('max')}]")
                validated[k] = vv
            elif s["type"] == "bool":
                validated[k] = bool(v)
            elif s["type"] == "choice":
                if v not in s["choices"]:
                    raise ValueError(f"not in {s['choices']}")
                validated[k] = v
            else:
                validated[k] = v
        except Exception as e:
            errors.append(f"{k}: {e}")
            continue
        if s.get("needs_restart"):
            needs_restart.append(k)
    if errors:
        return JSONResponse({"ok": False, "errors": errors}, status_code=400)
    merged = _write_config_file(validated)
    return {"ok": True, "saved": validated, "needs_restart": needs_restart,
            "merged_config": merged}


# OneDrive — placeholder for Phase 2. Returns status only.
@router.get("/api/miniapp/onedrive/status")
async def onedrive_status(request: Request):
    p = await _verify(request)
    require_scope(p, "smdl.downloader")
    _require_owner(p)
    from . import onedrive as _od
    return await _od.get_status()


@router.post("/api/miniapp/onedrive/connect")
async def onedrive_connect(request: Request):
    """Owner-only. Kicks off the MSAL device-code flow. Returns the user_code
    and verification URL — the UI shows them and polls /status until
    `configured` flips true."""
    p = await _verify(request)
    require_scope(p, "smdl.downloader")
    _require_owner(p)
    from . import onedrive as _od
    try:
        return {"ok": True, **(await _od.start_device_flow())}
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@router.post("/api/miniapp/onedrive/disconnect")
async def onedrive_disconnect(request: Request):
    p = await _verify(request)
    require_scope(p, "smdl.downloader")
    _require_owner(p)
    from . import onedrive as _od
    removed = _od.disconnect()
    return {"ok": True, "removed": removed}


@router.post("/api/miniapp/onedrive/test_upload")
async def onedrive_test(request: Request):
    p = await _verify(request)
    require_scope(p, "smdl.downloader")
    _require_owner(p)
    from . import onedrive as _od
    try:
        result = await _od.test_upload()
        return {"ok": True, "name": result.get("name"),
                "webUrl": result.get("webUrl"), "size": result.get("size")}
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


class OneDriveUploadBody(BaseModel):
    url:      str        # the original download URL (key for the history lookup)


@router.post("/api/miniapp/onedrive/upload")
async def onedrive_upload(request: Request, body: OneDriveUploadBody):
    """On-demand upload: any allowed user can push one of THEIR OWN history
    rows to OneDrive. The token is owner-scoped (owner's OneDrive), so non-
    owners are effectively contributing into the owner's drive — by design.

    Returns counts; runs synchronously so the toast tells the user what
    happened. For huge multi-file batches that'd block, the bg auto-mirror
    path (auto_after_send) is the right tool."""
    p = await _verify(request)
    require_scope(p, "smdl.downloader")
    uid = int(p["user"]["id"])
    is_own = _is_owner(uid)

    # Look up the history row by (chat_id, url) so users can only push files
    # they actually downloaded.
    rows = await _db.list_download_history(uid, limit=200)
    target = None
    for r in rows:
        if r.get("url") == body.url:
            target = r; break
    if target is None and is_own:
        # Owner fallback: check url_cache for pre-history rows they own.
        cached = await _list_recent_downloads(limit=200)
        for r in cached:
            if r.get("url") == body.url:
                target = r; break
    if target is None:
        return JSONResponse({"ok": False,
                             "error": "Download not found in your history."},
                            status_code=404)

    from . import onedrive as _od
    folder       = _cfg_get("onedrive_folder") or "/SMDL"
    delete_after = bool(_cfg_get("onedrive_delete_after_upload"))
    try:
        summary = await _od.auto_upload_files(
            target.get("files") or [],
            target.get("platform"),
            target.get("uploader"),
            base_folder=folder,
            delete_after_upload=delete_after,
        )
        return {"ok": True, **summary}
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


# ── Admin: user management + admin-only-mode + site blocklist ────────────────


class UserStatusBody(BaseModel):
    chat_id: int
    reason:  Optional[str] = None


class AdminModeBody(BaseModel):
    enabled: bool
    reason:  Optional[str] = None


class SiteBlocklistBody(BaseModel):
    blocked: list[str]


@router.get("/api/miniapp/admin/users")
async def admin_list_users(request: Request):
    p = await _verify(request)
    require_scope(p, "smdl.admin")
    _require_owner(p)
    rows = await _db.list_users()
    owner_id = _cfg_get("owner_chat_id")
    for r in rows:
        r["is_owner"] = (owner_id is not None and int(r.get("chat_id") or 0) == int(owner_id))
    return {"items": rows, "count": len(rows)}


@router.post("/api/miniapp/admin/users/ban")
async def admin_ban_user(request: Request, body: UserStatusBody):
    p = await _verify(request)
    require_scope(p, "smdl.admin")
    _require_owner(p)
    if _auth.is_owner(body.chat_id):
        return JSONResponse({"ok": False, "error": "Cannot ban the owner."}, status_code=400)
    ok = await _db.set_user_status(body.chat_id, "banned", body.reason)
    if not ok:
        return JSONResponse({"ok": False, "error": "No such user."}, status_code=404)
    return {"ok": True}


@router.post("/api/miniapp/admin/users/unban")
async def admin_unban_user(request: Request, body: UserStatusBody):
    p = await _verify(request)
    require_scope(p, "smdl.admin")
    _require_owner(p)
    ok = await _db.set_user_status(body.chat_id, "active")
    if not ok:
        return JSONResponse({"ok": False, "error": "No such user."}, status_code=404)
    return {"ok": True}


class ApproveByCodeBody(BaseModel):
    code: str


@router.post("/api/miniapp/admin/users/approve")
async def admin_approve_user(request: Request, body: UserStatusBody):
    p = await _verify(request)
    require_scope(p, "smdl.admin")
    _require_owner(p)
    ok = await _db.approve_user(body.chat_id)
    if not ok:
        return JSONResponse({"ok": False,
                             "error": "User not found, or is banned (unban first)."},
                            status_code=400)
    return {"ok": True}


class DenyUserBody(BaseModel):
    chat_id: int
    reason: str = ""


@router.post("/api/miniapp/admin/users/deny")
async def admin_deny_user(request: Request, body: DenyUserBody):
    """Reject a pending join request. Removes the user row (they can
    re-request later). Distinguished from ban/revoke which keeps the row
    for audit purposes — deny is for users who never had access in the
    first place."""
    p = await _verify(request)
    require_scope(p, "smdl.admin")
    _require_owner(p)
    # Use the existing ban path under the hood with a 'denied at pending'
    # marker; this keeps a paper trail without inventing a new status enum.
    ok = await _db.ban_user(body.chat_id, reason=f"DENIED@pending: {body.reason}".strip())
    if not ok:
        return JSONResponse({"ok": False, "error": "User not found"}, status_code=404)
    return {"ok": True}


@router.post("/api/miniapp/admin/users/approve_by_code")
async def admin_approve_by_code(request: Request, body: ApproveByCodeBody):
    """Owner pastes the 9-digit code a pending user sent them out-of-band.
    We look up the matching pending row and promote it to 'active'.
    Fail-closed: bad/expired/already-used codes return 404 with a generic
    error message — no oracle for code-guessing attackers."""
    p = await _verify(request)
    require_scope(p, "smdl.admin")
    _require_owner(p)
    row = await _db.find_user_by_pending_code(body.code or "")
    if row is None:
        return JSONResponse({"ok": False,
                             "error": "Code not recognised, expired, or already used."},
                            status_code=404)
    await _db.approve_user(int(row["chat_id"]))
    return {
        "ok": True,
        "chat_id": int(row["chat_id"]),
        "username": row.get("username"),
        "first_name": row.get("first_name"),
    }


# ── Admin: approved groups ───────────────────────────────────────────────────


class GroupApproveBody(BaseModel):
    chat_id: int
    label:   Optional[str] = None


class GroupUnapproveBody(BaseModel):
    chat_id: int


@router.get("/api/miniapp/admin/groups")
async def admin_list_groups(request: Request):
    p = await _verify(request)
    require_scope(p, "smdl.admin")
    _require_owner(p)
    rows = await _db.list_approved_groups()
    return {"items": rows, "count": len(rows)}


@router.post("/api/miniapp/admin/groups/approve")
async def admin_approve_group(request: Request, body: GroupApproveBody):
    p = await _verify(request)
    require_scope(p, "smdl.admin")
    uid = _require_owner(p)
    if body.chat_id >= 0:
        return JSONResponse({"ok": False,
                             "error": "Group chat_ids are negative. Did you mean to approve a user?"},
                            status_code=400)
    ok = await _db.approve_group(body.chat_id, body.label, uid)
    if not ok:
        return JSONResponse({"ok": False, "error": "Invalid chat_id."},
                            status_code=400)
    return {"ok": True}


@router.post("/api/miniapp/admin/groups/unapprove")
async def admin_unapprove_group(request: Request, body: GroupUnapproveBody):
    p = await _verify(request)
    require_scope(p, "smdl.admin")
    _require_owner(p)
    ok = await _db.unapprove_group(body.chat_id)
    if not ok:
        return JSONResponse({"ok": False, "error": "Group not found."},
                            status_code=404)
    return {"ok": True}


# ── Admin: bot-token rotation drill ──────────────────────────────────────────


@router.get("/api/miniapp/admin/security")
async def admin_security(request: Request):
    p = await _verify(request)
    require_scope(p, "smdl.admin")
    _require_owner(p)
    return await _auth.get_token_health()


@router.post("/api/miniapp/admin/security/pin")
async def admin_pin_token(request: Request):
    p = await _verify(request)
    require_scope(p, "smdl.admin")
    _require_owner(p)
    return {"ok": True, **(await _auth.pin_current_token())}


@router.get("/api/miniapp/admin/mode")
async def admin_get_mode(request: Request):
    p = await _verify(request)
    require_scope(p, "smdl.admin")
    _require_owner(p)
    return await _auth.get_admin_only_mode()


@router.post("/api/miniapp/admin/mode")
async def admin_set_mode(request: Request, body: AdminModeBody):
    p = await _verify(request)
    require_scope(p, "smdl.admin")
    _require_owner(p)
    await _auth.set_admin_only_mode(body.enabled, body.reason)
    return {"ok": True, **(await _auth.get_admin_only_mode())}


@router.get("/api/miniapp/admin/sites")
async def admin_get_sites(request: Request):
    """Return ALL known platforms + which ones are currently blocked, with
    a `category` tag per platform so the UI can group them (Adult cam vs
    Live streaming vs Social vs Regional (CN), etc.).
    Source of truth for "all platforms" is stream_monitor's hostname map
    (the same lookup used for grouping the watchlist UI)."""
    p = await _verify(request)
    require_scope(p, "smdl.admin")
    _require_owner(p)
    known = sorted({label for _, label in stream_monitor._PLATFORM_MAP})
    blocked = set(await _auth.get_site_blocklist())
    return {
        "platforms": [
            {
                "name":     k,
                "blocked":  (k in blocked),
                "category": _auth.PLATFORM_CATEGORY.get(k, "Other"),
            }
            for k in known
        ],
        "blocked_count": len(blocked),
        "defaults_seeded": (await _db.get_setting("site_blocklist_seeded", "false")).lower() == "true",
    }


@router.post("/api/miniapp/admin/sites")
async def admin_set_sites(request: Request, body: SiteBlocklistBody):
    p = await _verify(request)
    require_scope(p, "smdl.admin")
    _require_owner(p)
    persisted = await _auth.set_site_blocklist(body.blocked or [])
    return {"ok": True, "blocked": persisted}


# ── Profile scraper admin endpoints (owner-only) ────────────────────────────


class ScraperProfileBody(BaseModel):
    url:    str
    label:  Optional[str] = None


class ScraperToggleBody(BaseModel):
    paused: bool


@router.get("/api/miniapp/admin/scraper")
async def admin_scraper_get(request: Request):
    """Snapshot for the Admin tab card: profile list (per-platform), cookie
    health, and the global pause flag."""
    p = await _verify(request)
    require_scope(p, "smdl.admin")
    _require_owner(p)
    from . import profile_monitor as _pm
    profiles = await _db.scraper_list_profiles()
    # Per-platform cookie health (file presence + age + cooldown).
    import os as _os
    from datetime import datetime as _dt, timezone as _tz
    cookies = []
    for key in ("instagram", "tiktok"):
        cookie_file = f"{_pm.COOKIES_DIR}/{key}.txt"
        file_exists = _os.path.exists(cookie_file)
        file_age_days = None
        if file_exists:
            try:
                file_age_days = (
                    (_dt.now(_tz.utc) -
                     _dt.fromtimestamp(_os.path.getmtime(cookie_file), _tz.utc))
                    .days
                )
            except Exception:
                pass
        state = await _db.cookie_get(key)
        cooldown_seconds = None
        if state and state.get("cooldown_until"):
            try:
                cd = _dt.fromisoformat(state["cooldown_until"])
                remaining = (cd - _dt.now(_tz.utc)).total_seconds()
                if remaining > 0:
                    cooldown_seconds = int(remaining)
            except Exception:
                pass
        cookies.append({
            "key":              key,
            "file_exists":      file_exists,
            "file_age_days":    file_age_days,
            "first_seen_at":    state.get("first_seen_at") if state else None,
            "probes_today":     int(state.get("probes_today") or 0) if state else 0,
            "consecutive_blocks": int(state.get("consecutive_blocks") or 0) if state else 0,
            "cooldown_seconds": cooldown_seconds,
            "alerted_at":       state.get("alerted_at") if state else None,
        })
    return {
        "config_enabled":   bool(_pm.SCRAPER_ENABLED),
        "runtime_paused":   await _pm.is_runtime_paused(),
        "profiles":         profiles,
        "cookies":          cookies,
        "active_hours":     f"{_pm.SCRAPER_HUMAN_HOURS_START}-{_pm.SCRAPER_HUMAN_HOURS_END}",
        "timezone":         _pm.SCRAPER_TIMEZONE,
        "daily_sessions":   _pm.SCRAPER_DAILY_SESSIONS,
    }


@router.post("/api/miniapp/admin/scraper/toggle")
async def admin_scraper_toggle(request: Request, body: ScraperToggleBody):
    """Pause or resume the scraper globally. Survives restart via settings table."""
    p = await _verify(request)
    require_scope(p, "smdl.admin")
    _require_owner(p)
    from . import profile_monitor as _pm
    await _pm.set_runtime_paused(bool(body.paused))
    return {"ok": True, "runtime_paused": await _pm.is_runtime_paused()}


@router.post("/api/miniapp/admin/scraper/add")
async def admin_scraper_add(request: Request, body: ScraperProfileBody):
    p = await _verify(request)
    require_scope(p, "smdl.admin")
    owner = _require_owner(p)
    from . import profile_monitor as _pm
    ok, msg = await _pm.add_profile(body.url, added_by=owner, label=body.label)
    if not ok:
        raise HTTPException(status_code=400, detail=msg)
    return {"ok": True, "msg": msg}


@router.post("/api/miniapp/admin/scraper/remove")
async def admin_scraper_remove(request: Request, body: ScraperProfileBody):
    p = await _verify(request)
    require_scope(p, "smdl.admin")
    _require_owner(p)
    from . import profile_monitor as _pm
    ok, msg = await _pm.remove_profile(body.url)
    if not ok:
        raise HTTPException(status_code=404, detail=msg)
    return {"ok": True}


@router.post("/api/miniapp/admin/scraper/pause")
async def admin_scraper_pause(request: Request, body: ScraperProfileBody):
    p = await _verify(request)
    require_scope(p, "smdl.admin")
    _require_owner(p)
    from . import profile_monitor as _pm
    ok, msg = await _pm.pause_profile(body.url)
    if not ok:
        raise HTTPException(status_code=404, detail=msg)
    return {"ok": True}


@router.post("/api/miniapp/admin/scraper/resume")
async def admin_scraper_resume(request: Request, body: ScraperProfileBody):
    p = await _verify(request)
    require_scope(p, "smdl.admin")
    _require_owner(p)
    from . import profile_monitor as _pm
    ok, msg = await _pm.resume_profile(body.url)
    if not ok:
        raise HTTPException(status_code=404, detail=msg)
    return {"ok": True}


@router.post("/api/miniapp/admin/scraper/probe")
async def admin_scraper_probe(request: Request, body: ScraperProfileBody):
    """Run a single probe right now (outside the burst-session schedule).
    Equivalent to /scrape_now in the bot."""
    p = await _verify(request)
    require_scope(p, "smdl.admin")
    _require_owner(p)
    from . import bot as _bot
    from . import profile_monitor as _pm
    app = _bot.get_application()
    if app is None:
        raise HTTPException(status_code=503, detail="bot not running")
    ok, msg = await _pm.probe_now(app, body.url)
    if not ok:
        raise HTTPException(status_code=400, detail=msg)
    return {"ok": True, "msg": msg}


@router.post("/api/miniapp/admin/scraper/backfill")
async def admin_scraper_backfill(request: Request, body: ScraperProfileBody):
    """Spawn gallery-dl against the entire profile for historical content.
    The regular scraper is forward-looking (baselines on first probe);
    this endpoint complements it by pulling everything that already exists.
    Runs in the background — returns immediately."""
    p = await _verify(request)
    require_scope(p, "smdl.admin")
    _require_owner(p)
    from . import profile_monitor as _pm
    ok, msg = await _pm.start_backfill(body.url)
    if not ok:
        raise HTTPException(status_code=400, detail=msg)
    return {"ok": True, "msg": msg}


@router.get("/api/miniapp/admin/scraper/backfill_status")
async def admin_scraper_backfill_status(request: Request):
    """In-memory status dict for all known backfills (running + recent).
    Resets on daemon restart."""
    p = await _verify(request)
    require_scope(p, "smdl.admin")
    _require_owner(p)
    from . import profile_monitor as _pm
    return _pm.backfill_status()


# ── Live-recording repair (owner-only) ──────────────────────────────────────


@router.get("/api/miniapp/admin/recordings/pending")
async def admin_recordings_pending(request: Request):
    """How many .mp4.part files are sitting in /downloads/live/Chaturbate/NA
    waiting to be remuxed. Drives the Admin tab badge."""
    p = await _verify(request)
    require_scope(p, "smdl.admin")
    _require_owner(p)
    from . import repair_live_parts as _r
    return _r.scan_pending()


@router.post("/api/miniapp/admin/recordings/repair")
async def admin_recordings_repair(request: Request):
    """Fire-and-forget the ffmpeg remux pass. Returns immediately with the
    count of files queued; the actual work runs in a background task and
    can take 10+ minutes per GB. Refresh the Admin tab afterwards to see
    the pending count shrink to 0."""
    p = await _verify(request)
    require_scope(p, "smdl.admin")
    _require_owner(p)
    from . import repair_live_parts as _r
    pending = _r.scan_pending()
    if pending["count"] == 0:
        return {"started": False, "queued": 0,
                "msg": "Nothing to repair — no .mp4.part files."}

    async def _run():
        # repair_all is sync (uses subprocess.run for ffmpeg) — push to a
        # thread so we don't block the event loop.
        try:
            await asyncio.to_thread(_r.repair_all, _r.DEFAULT_DIR, False, logger)
        except Exception as e:
            logger.exception("repair_all background task crashed: %s", e)

    asyncio.create_task(_run())
    return {"started": True, "queued": pending["count"],
            "total_bytes": pending["total_bytes"],
            "msg": f"Queued {pending['count']} file(s) for repair."}


@router.post("/api/miniapp/restart")
async def restart_service(request: Request):
    """Graceful container restart (owner-only). The container's restart_policy
    in docker-compose (unless-stopped) brings it back up automatically. This
    is required for settings whose Python module reads them at import time
    (anything with needs_restart=True)."""
    p = await _verify(request)
    require_scope(p, "smdl.admin")
    _require_owner(p)
    logger.info("restart_service: SIGTERM scheduled by owner")

    # Defer the SIGTERM by a moment so the HTTP response can flush.
    async def _terminate_later():
        await asyncio.sleep(0.5)
        import signal
        os.kill(1, signal.SIGTERM)
    asyncio.create_task(_terminate_later())
    return {"ok": True, "msg": "Restart scheduled — container will come back up automatically."}


# ── HTML (inline single-page app) ────────────────────────────────────────────


HTML = """<!doctype html>
<html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no">
<title>SM-DL</title>
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<style>
:root {
  --bg: var(--tg-theme-bg-color, #1c1c1e);
  --fg: var(--tg-theme-text-color, #e8e8ea);
  --muted: var(--tg-theme-hint-color, #8e8e93);
  --link: var(--tg-theme-link-color, #2997ff);
  --button: var(--tg-theme-button-color, #2997ff);
  --button-text: var(--tg-theme-button-text-color, #fff);
  --section: var(--tg-theme-section-bg-color, #2c2c2e);
  --separator: var(--tg-theme-section-separator-color, #38383a);
  --destructive: #ff453a;
  --success: #34c759;
}
* { box-sizing: border-box; -webkit-tap-highlight-color: transparent; }
body { margin: 0; padding: env(safe-area-inset-top, 0) 0 env(safe-area-inset-bottom, 0) 56px;
       font: 15px/1.4 -apple-system, system-ui, "Segoe UI", Roboto, sans-serif;
       background: var(--bg); color: var(--fg); min-height: 100vh;
       transition: padding-left 0.2s ease; }
body.sidebar-collapsed { padding-left: 28px; }
/* Left sidebar (was bottom tabbar). 56px wide normal, 28px collapsed
   (icons only). Settings pinned at the bottom via flex spacer.
   safe-area padding on top so the first nav item doesn't sit behind
   the device status bar / Telegram chrome. */
.sidebar { position: fixed; left: 0; top: 0; bottom: 0; width: 56px;
           background: var(--section); border-right: 1px solid var(--separator);
           display: flex; flex-direction: column; z-index: 10;
           padding: calc(env(safe-area-inset-top, 0px) + 8px) 0 env(safe-area-inset-bottom, 0px);
           transition: width 0.2s ease; overflow: hidden; }
body.sidebar-collapsed .sidebar { width: 28px; }
.sidebar-spacer { flex: 1; }
.sidebar-divider { height: 1px; background: var(--separator); margin: 6px 8px; }
body.sidebar-collapsed .sidebar-divider { margin: 6px 4px; }
.sidebar-toggle { display: flex; align-items: center; justify-content: center;
                  padding: 8px 0; color: var(--muted); cursor: pointer;
                  user-select: none; font-size: 14px; line-height: 1;
                  border-bottom: 1px solid var(--separator); margin-bottom: 4px; }
.sidebar-toggle:hover { color: var(--button); }
.sidebar-item { display: flex; flex-direction: column; align-items: center;
                padding: 9px 4px; color: var(--muted); cursor: pointer;
                user-select: none; border-left: 3px solid transparent;
                text-align: center; gap: 3px; transition: background 0.12s; }
.sidebar-item:hover { background: rgba(255,255,255,0.03); }
.sidebar-item.active { color: var(--button); border-left-color: var(--button);
                       background: rgba(41,151,255,0.10); }
.sidebar-item .icon { font-size: 20px; line-height: 1; }
.sidebar-item .label { font-size: 9.5px; line-height: 1.05; letter-spacing: 0.1px; }
/* Icons-only mode: shrink padding, hide labels, slightly smaller icons. */
body.sidebar-collapsed .sidebar-item { padding: 9px 2px; gap: 0; border-left-width: 2px; }
body.sidebar-collapsed .sidebar-item .label { display: none; }
body.sidebar-collapsed .sidebar-item .icon { font-size: 16px; }
body.sidebar-collapsed .sidebar-toggle { padding: 6px 0; font-size: 12px; }
/* Home tile grid — landing page for the Mini App. 2 cols on phones. */
.home-tiles { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; margin-top: 6px; }
.home-tile { background: var(--section); border-radius: 12px; padding: 16px 12px;
             cursor: pointer; border: 1px solid var(--separator); position: relative;
             text-align: left; transition: transform 0.1s, background 0.12s;
             color: var(--fg); }
.home-tile:active { transform: scale(0.98); background: rgba(255,255,255,0.04); }
.home-tile .ico { font-size: 30px; line-height: 1; margin-bottom: 8px; }
.home-tile .name { font-size: 14px; font-weight: 600; margin-bottom: 2px; }
.home-tile .desc { font-size: 11px; color: var(--muted); line-height: 1.3; }
.sidebar-item.admin-only { display: none; }
.sidebar-item.admin-only.show { display: flex; }
.home-tile.admin-only { display: none; }
.home-tile.admin-only.show { display: block; }
/* #41 — drag-to-reorder. Pointer-event based (HTML5 draggable is unreliable
   in the Telegram mobile WebView). Edit mode disables the wiggle's transition
   jank, kills page-scroll under the finger (touch-action:none), and shows a
   grab handle. Order persists per-device in localStorage. */
#tiles-arrange-btn.on { background: var(--button); color: #fff; border-color: var(--button); }
.home-tiles.editing .home-tile { cursor: grab; touch-action: none;
  animation: tile-wiggle 0.45s ease-in-out infinite alternate; }
.home-tiles.editing .home-tile::after { content: '⠿'; position: absolute;
  top: 6px; right: 9px; color: var(--muted); font-size: 15px; line-height: 1; }
.home-tile.dragging { opacity: 0.65; transform: scale(1.05); z-index: 5;
  cursor: grabbing; box-shadow: 0 8px 22px rgba(0,0,0,0.45); animation: none; }
@keyframes tile-wiggle { from { transform: rotate(-0.7deg); } to { transform: rotate(0.7deg); } }
.page { display: none; padding: max(12px, calc(env(safe-area-inset-top, 0px) + 4px)) 12px 12px; }
.page.active { display: block; }
.subtabs { display: flex; gap: 6px; margin: 0 0 14px; overflow-x: auto;
           -webkit-overflow-scrolling: touch; scrollbar-width: none; }
.subtabs::-webkit-scrollbar { display: none; }
.subtab { padding: 7px 13px; border: 1px solid var(--separator); border-radius: 16px;
          background: var(--card); color: var(--fg); font-size: 12px; cursor: pointer;
          white-space: nowrap; transition: background 0.15s; }
.subtab:hover { background: rgba(255,255,255,0.04); }
.subtab.active { background: var(--button); color: var(--button-text); border-color: var(--button); }
.subtab-pane { display: none; }
.subtab-pane.active { display: block; }
h1 { font-size: 1.3em; margin: 6px 0 14px; }
.card { background: var(--section); border-radius: 10px; padding: 12px; margin-bottom: 10px; }
.row { display: flex; align-items: center; gap: 10px; }
.row .grow { flex: 1; min-width: 0; }
.row .name { font-weight: 600; word-break: break-word; }
.row .meta { font-size: 12px; color: var(--muted); margin-top: 2px; word-break: break-all; }
button { background: var(--button); color: var(--button-text); border: 0; padding: 9px 14px;
         border-radius: 8px; font-size: 14px; font-weight: 500; cursor: pointer; touch-action: manipulation; }
button:active { transform: scale(0.97); }
button.sec { background: transparent; color: var(--button); border: 1px solid var(--button); }
button.danger { background: var(--destructive); }
button.small { padding: 6px 10px; font-size: 12px; }
input { width: 100%; padding: 10px 12px; border: 1px solid var(--separator); border-radius: 8px;
        background: var(--bg); color: var(--fg); font-size: 14px; }
input:focus { outline: none; border-color: var(--button); }
.field { font-size: 12px; color: var(--muted); margin: 4px 4px 4px; }
.dot { width: 10px; height: 10px; border-radius: 50%; display: inline-block; margin-right: 8px; vertical-align: middle; }
.dot.live    { background: var(--success); box-shadow: 0 0 6px var(--success); animation: pulse 1.4s infinite; }
.dot.offline { background: var(--destructive); }
.dot.unknown { background: var(--muted); }
.dot.idle    { background: var(--muted); }
.wl-group-head { font-size: 11px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.6px;
    color: var(--muted); margin: 14px 4px 6px; display: flex; align-items: center; gap: 6px; }
.wl-group-head:first-child { margin-top: 4px; }
.wl-group-count { background: var(--separator); color: var(--muted); border-radius: 10px;
    padding: 1px 7px; font-size: 10px; font-weight: 600; letter-spacing: 0; }
.card.recording { box-shadow: inset 3px 0 0 0 var(--success); }
.rec-tag { color: var(--success); font-weight: 700; letter-spacing: 0.4px; }
.icon-btn.rec-on { color: var(--destructive); border-color: var(--destructive); }
.wl-row { display: flex; align-items: center; gap: 8px; }
.wl-row .grow { flex: 1; min-width: 0; }
.wl-row .username { font-weight: 600; font-size: 15px; }
.wl-row .u-link { color: var(--fg); text-decoration: none; cursor: pointer; -webkit-tap-highlight-color: rgba(41,151,255,0.2); }
.wl-row .u-link:active { color: var(--button); }
.wl-row .sub { font-size: 11px; color: var(--muted); margin-top: 2px; }
.wl-row button.icon-btn { background: transparent; color: var(--muted); border: 1px solid var(--separator);
    padding: 5px 9px; font-size: 14px; line-height: 1; border-radius: 6px; min-width: 36px; }
.wl-row button.icon-btn.on { color: #ff9500; border-color: #ff9500; }
.wl-row button.icon-btn:hover { color: var(--button); border-color: var(--button); }
.wl-edit { margin-top: 8px; padding-top: 8px; border-top: 1px dashed var(--separator); display: none; }
.wl-edit.open { display: block; }
.wl-edit input { font-size: 12px; padding: 7px 10px; margin-bottom: 6px; }
.wl-edit .row { gap: 6px; }
.wl-edit button { font-size: 12px; padding: 6px 10px; }
.restart-banner { background: rgba(255,149,0,0.15); color: #ff9500; padding: 8px 12px; border-radius: 8px;
    margin: 10px 0; font-size: 12px; display: none; }
.restart-banner.show { display: block; }
.btn-row { display: flex; gap: 8px; margin: 14px 0; }
.btn-row button { flex: 1; }
button.warn { background: #ff9500; color: #fff; }
.lockdown-banner { background: rgba(255,69,58,0.18); color: var(--destructive); padding: 10px 12px;
    border-radius: 8px; margin: 10px 0; font-weight: 600; font-size: 13px; }
.lockdown-banner .reason { font-weight: 400; font-size: 12px; margin-top: 4px; opacity: 0.85; }
.user-row { display: flex; align-items: center; gap: 10px; }
.user-row .meta { font-size: 11px; color: var(--muted); }
.user-row .ban-badge { background: rgba(255,69,58,0.15); color: var(--destructive);
    padding: 2px 7px; border-radius: 6px; font-size: 10px; font-weight: 700; letter-spacing: 0.4px; }
.user-row .owner-badge { background: rgba(52,199,89,0.15); color: var(--success);
    padding: 2px 7px; border-radius: 6px; font-size: 10px; font-weight: 700; letter-spacing: 0.4px; }
/* Settings tile layout (#34) — 2-up grid on wide viewports, stack on narrow.
   OneDrive ‖ Downloads land side-by-side; General settings go full-width. */
.set-grid { display: grid; gap: 12px;
    grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
    align-items: start; }
.set-tile { background: var(--card); border: 1px solid var(--separator);
    border-radius: 10px; padding: 12px 14px; }
.set-tile .head { display: flex; align-items: center; gap: 6px;
    font-size: 12px; font-weight: 700; color: var(--muted);
    text-transform: uppercase; letter-spacing: 0.6px; margin-bottom: 8px; }
.set-tile.full { grid-column: 1 / -1; }
.set-pt-preview { font-family: ui-monospace, monospace; font-size: 11px;
    color: var(--muted); background: var(--bg); padding: 6px 8px;
    border-radius: 6px; margin-top: 6px;
    overflow-x: auto; white-space: nowrap; }
.set-pt-tokens { display: flex; flex-wrap: wrap; gap: 4px; margin-top: 6px; }
.set-pt-tokens .tok { background: var(--separator); color: var(--muted);
    padding: 2px 8px; border-radius: 99px; font-size: 10px;
    font-family: ui-monospace, monospace; cursor: pointer; }
.set-pt-tokens .tok:hover { color: var(--button); }

/* #41 Part 2 — restructure preview / progress */
.rs-summary { font-size: 12px; color: var(--muted); margin-bottom: 8px; }
.rs-summary b { color: var(--text); }
.rs-list { max-height: 300px; overflow-y: auto; border: 1px solid var(--separator);
    border-radius: 8px; }
.rs-row { display: flex; gap: 8px; align-items: baseline; padding: 6px 8px;
    border-bottom: 1px solid var(--separator); font-size: 11px; }
.rs-row:last-child { border-bottom: none; }
.rs-act { flex: 0 0 auto; text-transform: uppercase; font-size: 9px;
    font-weight: 700; letter-spacing: 0.04em; padding: 1px 6px; border-radius: 99px;
    color: #fff; }
.rs-move .rs-act { background: #34c759; }
.rs-conflict .rs-act { background: #ff9500; }
.rs-skip .rs-act { background: var(--separator); color: var(--muted); }
.rs-path { flex: 1 1 auto; min-width: 0; font-family: ui-monospace, monospace;
    word-break: break-all; }
.rs-src { color: var(--muted); }
.rs-dst { color: var(--text); }
.rs-arrow { color: var(--button); margin: 0 4px; }
.rs-reason { display: block; color: var(--muted); opacity: 0.7; font-size: 10px; }
.rs-bar { height: 8px; background: var(--separator); border-radius: 99px;
    overflow: hidden; margin: 6px 0; }
.rs-bar-fill { height: 100%; width: 0; background: var(--button);
    transition: width 0.25s ease; }
.rs-bar-fill.ok { background: #34c759; }
.rs-bar-fill.err { background: #ff3b30; }

/* Watchlist tile grid (#30) — replaces full-width per-streamer cards */
.wl-site-section { margin-bottom: 14px; }
.wl-site-bar { display: flex; align-items: center; gap: 8px; padding: 8px 4px;
    cursor: pointer; user-select: none; }
.wl-site-bar .caret { font-size: 10px; color: var(--muted); width: 12px;
    transition: transform 150ms ease; }
.wl-site-section.collapsed .wl-site-bar .caret { transform: rotate(-90deg); }
.wl-site-section.collapsed .wl-tiles { display: none; }
.wl-site-bar .title { font-size: 11px; font-weight: 700; text-transform: uppercase;
    letter-spacing: 0.6px; color: var(--muted); }
.wl-site-bar .count { background: var(--separator); color: var(--muted);
    border-radius: 10px; padding: 1px 7px; font-size: 10px; font-weight: 600; }
.wl-site-bar .bulk { margin-left: auto; display: flex; gap: 4px; }
.wl-site-bar .bulk button { background: transparent; color: var(--muted);
    border: 1px solid var(--separator); padding: 3px 8px; font-size: 10px;
    line-height: 1; border-radius: 6px; cursor: pointer; }
.wl-site-bar .bulk button:hover { color: var(--button); border-color: var(--button); }
.wl-tiles { display: grid;
    grid-template-columns: repeat(auto-fill, minmax(170px, 1fr));
    gap: 8px; }
.wl-tile { background: var(--card); border: 1px solid var(--separator);
    border-radius: 10px; padding: 8px 10px; display: flex; flex-direction: column;
    gap: 4px; min-width: 0; }
.wl-tile.recording { box-shadow: inset 3px 0 0 0 var(--success); }
.wl-tile .uname { display: flex; align-items: center; gap: 6px; font-weight: 600;
    font-size: 13px; min-width: 0; overflow: hidden; text-overflow: ellipsis;
    white-space: nowrap; }
.wl-tile .uname .dot { margin-right: 0; flex-shrink: 0; }
.wl-tile .uname .u-link { color: var(--fg); text-decoration: none; cursor: pointer;
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.wl-tile .uname .u-link:active { color: var(--button); }
.wl-tile .sub { font-size: 10px; color: var(--muted); line-height: 1.3;
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.wl-tile .sub .rec-tag { color: var(--success); font-weight: 700; }
.wl-tile .actions { display: flex; gap: 4px; margin-top: 2px; }
.wl-tile .actions button { background: transparent; color: var(--muted);
    border: 1px solid var(--separator); padding: 4px 6px; font-size: 13px;
    line-height: 1; border-radius: 5px; flex: 1; min-width: 0; cursor: pointer; }
.wl-tile .actions button:hover { color: var(--button); border-color: var(--button); }
.wl-tile .actions button.on { color: #ff9500; border-color: #ff9500; }
.wl-tile .actions button.rec-on { color: var(--destructive); border-color: var(--destructive); }
.wl-tile .actions button.auto-on { color: var(--success); border-color: var(--success); }
.wl-tile .wl-edit { margin-top: 4px; padding-top: 4px; border-top: 1px dashed var(--separator);
    display: none; }
.wl-tile .wl-edit.open { display: block; }
.wl-tile .wl-edit input { font-size: 11px; padding: 5px 7px; margin-bottom: 4px; }
.wl-tile .wl-edit button { font-size: 10px; padding: 3px 6px; }

/* Profile Scraper — compact one-line row (replaces stacked layout, #33) */
.scraper-row { display: flex; align-items: center; gap: 8px; padding: 6px 0;
    border-top: 1px solid var(--separator); min-height: 36px; }
.scraper-row:first-child { border-top: 0; }
.scraper-row .uname { font-weight: 600; font-size: 14px; flex: 1; min-width: 0;
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.scraper-row .chips { display: flex; align-items: center; gap: 4px; flex-shrink: 0; }
.scraper-row .chip { background: var(--separator); color: var(--muted); border-radius: 10px;
    padding: 1px 7px; font-size: 10px; font-weight: 600; letter-spacing: 0.2px; line-height: 1.5;
    white-space: nowrap; }
.scraper-row .chip.due { background: rgba(41,151,255,0.15); color: var(--button); }
.scraper-row .chip.warn { background: rgba(255,149,0,0.18); color: #ff9500; }
.scraper-row .chip.err { background: rgba(255,69,58,0.18); color: var(--destructive); }
.scraper-row .actions { display: flex; align-items: center; gap: 4px; flex-shrink: 0; }
.scraper-row .actions .icon-btn { background: transparent; color: var(--muted);
    border: 1px solid var(--separator); padding: 4px 7px; font-size: 13px; line-height: 1;
    border-radius: 6px; min-width: 30px; }
.scraper-row .actions .icon-btn:hover { color: var(--button); border-color: var(--button); }
.scraper-row .actions .icon-btn.primary { color: var(--success); border-color: var(--success); }
.scraper-row .actions .icon-btn.danger { color: var(--destructive); border-color: var(--destructive); }
.site-toggle { display: flex; align-items: center; justify-content: space-between; gap: 8px;
    padding: 8px 0; border-bottom: 1px solid var(--separator); }
.site-toggle:last-child { border-bottom: 0; }
.switch { position: relative; width: 44px; height: 24px; }
.switch input { opacity: 0; width: 0; height: 0; }
.switch .slider { position: absolute; cursor: pointer; inset: 0; background: var(--separator);
    border-radius: 24px; transition: 0.2s; }
.switch .slider::before { content: ''; position: absolute; left: 3px; top: 3px;
    width: 18px; height: 18px; background: #fff; border-radius: 50%; transition: 0.2s; }
.switch input:checked + .slider { background: var(--success); }
.switch input:checked + .slider::before { transform: translateX(20px); }
.switch.danger input:checked + .slider { background: var(--destructive); }
@keyframes pulse { 50% { opacity: 0.5; } }
.empty { text-align: center; color: var(--muted); padding: 40px 20px; font-size: 14px; }
.msg { padding: 10px 14px; border-radius: 8px; margin: 10px 0; font-size: 13px; }
.msg.ok { background: rgba(52,199,89,0.15); color: var(--success); }
.msg.err { background: rgba(255,69,58,0.15); color: var(--destructive); }
.platform-pill { display: inline-block; background: var(--bg); border: 1px solid var(--separator);
                 padding: 4px 8px; border-radius: 6px; margin: 2px; font-size: 12px; }
.platform-pill.live { border-color: var(--success); color: var(--success); }
.file-list { font-size: 12px; color: var(--muted); margin-top: 4px; }
.file-list a { color: var(--link); display: block; word-break: break-all; }
.timeago { font-size: 11px; color: var(--muted); }
.spin { display: inline-block; width: 14px; height: 14px; border: 2px solid var(--separator);
        border-top-color: var(--button); border-radius: 50%; animation: sp 0.8s linear infinite;
        vertical-align: middle; }
@keyframes sp { to { transform: rotate(360deg); } }
.url { font-family: ui-monospace, "SF Mono", Menlo, monospace; font-size: 11px; }
/* Page header — h1 + actions on the right (e.g. Downloads clear button) */
.page-header { display: flex; align-items: center; gap: 10px; margin: 6px 0 14px; }
.page-header h1 { margin: 0; flex: 1; }
/* Simplified download row — single clickable line: @user · description */
.dl-row { padding: 10px 12px; border-radius: 8px; background: var(--section);
          margin-bottom: 6px; }
.dl-row a { color: var(--fg); text-decoration: none; display: block; }
.dl-row a:active { color: var(--button); }
.dl-row .user { font-weight: 600; }
.dl-row .desc { color: var(--muted); font-size: 13px; margin-top: 2px;
                word-break: break-all; }
.dl-row .when { color: var(--muted); font-size: 11px; margin-top: 4px; }
/* Files page */
.files-crumbs { display: flex; flex-wrap: wrap; align-items: center; gap: 4px;
                font-size: 13px; margin-bottom: 12px; color: var(--muted); }
.files-crumbs a { color: var(--link); text-decoration: none; cursor: pointer; }
.files-crumbs .sep { color: var(--separator); margin: 0 2px; }
.file-row { display: flex; align-items: center; gap: 10px; padding: 10px 12px;
            border-bottom: 1px solid var(--separator); cursor: pointer;
            transition: background 0.12s; }
.file-row:hover { background: rgba(255,255,255,0.03); }
.file-row:last-child { border-bottom: 0; }
.file-row .file-ico { font-size: 20px; line-height: 1; width: 24px; text-align: center; }
.file-row .grow { flex: 1; min-width: 0; }
.file-row .file-name { font-size: 14px; word-break: break-all; }
.file-row .file-meta { font-size: 11px; color: var(--muted); margin-top: 2px; }
/* View-mode selector for Files page */
.files-view-select { background: var(--section); border: 1px solid var(--separator);
                     color: var(--fg); padding: 6px 8px; border-radius: 6px;
                     font-size: 12px; cursor: pointer; }
/* Tile view modes */
.files-grid-sm { display: grid;
                 grid-template-columns: repeat(auto-fill, minmax(82px, 1fr));
                 gap: 6px; }
.files-grid-md { display: grid;
                 grid-template-columns: repeat(auto-fill, minmax(140px, 1fr));
                 gap: 8px; }
.file-tile { background: var(--section); border-radius: 8px; cursor: pointer;
             overflow: hidden; display: flex; flex-direction: column;
             aspect-ratio: 1 / 1; border: 1px solid var(--separator);
             transition: transform 0.1s; }
.file-tile:active { transform: scale(0.97); }
.file-tile .thumb { flex: 1; display: flex; align-items: center;
                    justify-content: center; background: var(--bg);
                    overflow: hidden; min-height: 0; }
.file-tile .thumb img { width: 100%; height: 100%; object-fit: cover; }
.file-tile .thumb .emoji { font-size: 32px; }
.files-grid-sm .file-tile .thumb .emoji { font-size: 22px; }
.file-tile .label { font-size: 10px; padding: 4px 6px; color: var(--fg);
                    text-align: center; line-height: 1.2; white-space: nowrap;
                    overflow: hidden; text-overflow: ellipsis;
                    border-top: 1px solid var(--separator); }
.files-grid-sm .file-tile .label { font-size: 9px; padding: 3px 4px; }
.file-folder-tile { display: flex; flex-direction: column; align-items: center;
                    justify-content: center; background: var(--section);
                    border-radius: 8px; cursor: pointer; aspect-ratio: 1 / 1;
                    border: 1px solid var(--separator); padding: 6px;
                    transition: transform 0.1s; text-align: center; }
.file-folder-tile:active { transform: scale(0.97); }
.file-folder-tile .emoji { font-size: 32px; line-height: 1; }
.files-grid-sm .file-folder-tile .emoji { font-size: 22px; }
.file-folder-tile .label { font-size: 10px; margin-top: 4px; color: var(--fg);
                           white-space: nowrap; overflow: hidden;
                           text-overflow: ellipsis; max-width: 100%; }
/* Inline file preview modal — opens when the user taps a media file in
   the Files browser. Videos / images / audio play right here without
   bouncing to an external browser. */
.preview-modal { display: none; position: fixed; inset: 0;
                 background: rgba(0,0,0,0.92); z-index: 100;
                 padding: env(safe-area-inset-top, 0) 0 env(safe-area-inset-bottom, 0); }
.preview-modal.open { display: flex; flex-direction: column; }
.preview-head { display: flex; align-items: center; gap: 10px; padding: 10px 14px;
                background: rgba(0,0,0,0.5); color: var(--fg); }
.preview-head .name { flex: 1; font-size: 13px; word-break: break-all; }
.preview-head button { background: transparent; border: 1px solid var(--separator);
                       color: var(--fg); padding: 6px 10px; font-size: 13px;
                       border-radius: 6px; }
.preview-body { flex: 1; display: flex; align-items: center; justify-content: center;
                overflow: auto; padding: 8px; }
.preview-body video, .preview-body img { max-width: 100%; max-height: 100%;
                                          object-fit: contain; }
.preview-body audio { width: 90%; max-width: 500px; }
.preview-body .non-media { color: var(--muted); text-align: center; padding: 40px 20px; }
</style>
</head><body>

<div id=app>
  <div id=msg></div>

  <div class="page active" id=page-home>
    <div class=page-header>
      <h1>Sentinel Media</h1>
      <button class="small sec" id=tiles-arrange-btn onclick="toggleTileEdit()" title="Drag tiles to reorder · saved on this device">✥ Arrange</button>
    </div>
    <div class=home-tiles id=home-tiles>
      <div class=home-tile data-tile=downloads onclick="goto('downloads')">
        <div class=ico>📥</div>
        <div class=name>Downloads</div>
        <div class=desc>Recent yt-dlp / gallery-dl jobs · file delivery links</div>
      </div>
      <div class=home-tile data-tile=streams onclick="goto('watchlist')">
        <div class=ico>👁</div>
        <div class=name>Streams</div>
        <div class=desc>Auto-record streams from twitch · youtube · kick</div>
      </div>
      <div class=home-tile data-tile=theater onclick="location.href='/app/stremio'">
        <div class=ico>🎬</div>
        <div class=name>Theater</div>
        <div class=desc>Movies + series · stream &amp; cache to G:\</div>
      </div>
      <div class=home-tile data-tile=iptv onclick="location.href='/iptv'">
        <div class=ico>📺</div>
        <div class=name>IPTV</div>
        <div class=desc>11k+ public channels · EPG · scheduled DVR</div>
      </div>
      <div class="home-tile admin-only" data-tile=files id=tile-files onclick="goto('files')">
        <div class=ico>📁</div>
        <div class=name>Files</div>
        <div class=desc>Browse + fetch from /downloads (SFTP-style)</div>
      </div>
      <div class="home-tile admin-only" data-tile=scraper id=tile-scraper onclick="goto('scraper')">
        <div class=ico>🤖</div>
        <div class=name>Scraper</div>
        <div class=desc>Profile monitoring · age-gated platforms</div>
      </div>
      <div class="home-tile admin-only" data-tile=server id=tile-admin onclick="goto('admin')">
        <div class=ico>🛡</div>
        <div class=name>Server</div>
        <div class=desc>Server settings · users · site blocklist · kill switch</div>
      </div>
    </div>
  </div>

  <div class=page id=page-downloads>
    <div class=page-header>
      <h1>Recent Downloads</h1>
      <button class="small sec" onclick="clearDownloadHistory()" title="Wipe your download history">🗑 Clear</button>
    </div>
    <div class=card style="margin-bottom:14px">
      <div class=meta style="margin-bottom:6px">Paste one or more URLs (newline / space / comma-separated). Up to 50 at a time. Live-recording URLs go through the watchlist, not this box.</div>
      <textarea id=dl-batch-input rows=3 placeholder="https://... &#10;https://..." style="width:100%;box-sizing:border-box;padding:8px;background:var(--bg);border:1px solid var(--border);border-radius:8px;color:var(--text);font-family:ui-monospace,monospace;font-size:13px;resize:vertical"></textarea>
      <div style="display:flex;gap:8px;margin-top:8px;align-items:center">
        <button class="primary" onclick="submitDownloadBatch()" id=dl-batch-go>⬇ Download</button>
        <div class=meta id=dl-batch-status></div>
      </div>
    </div>
    <div id=downloads-list><div class=empty><span class=spin></span> Loading…</div></div>
  </div>

  <div class=page id=page-files>
    <div class=page-header>
      <h1>Files</h1>
      <select id=files-view class=files-view-select onchange="setFilesView(this.value)">
        <option value=list>List</option>
        <option value=small>Small tiles</option>
        <option value=medium>Medium tiles</option>
      </select>
      <button class="small sec" onclick="loadFiles(filesCwd)" title="Refresh">🔄</button>
    </div>
    <div id=files-crumbs class=files-crumbs></div>
    <div id=files-list><div class=empty><span class=spin></span> Loading…</div></div>
  </div>

  <div class=page id=page-watchlist>
    <h1>Stream Watchlist</h1>
    <div class=card>
      <div class=field>Streamer / channel URL</div>
      <input id=watch-url placeholder="https://twitch.tv/...">
      <div class=btn-row style="margin-top:8px;gap:6px">
        <button class=sec onclick=testWatchUrl()>🔗 Test</button>
        <button onclick=addWatch()>+ Add to watchlist</button>
      </div>
      <div id=watch-test-result style="margin-top:10px"></div>
      <div id=watch-info-footer class=meta style="margin-top:10px;border-top:1px dashed var(--separator);padding-top:8px">
        📡 Live recording: <b>youtube · twitch · kick</b><br>
        🎥 Anything else: 1700+ sites via yt-dlp
      </div>
    </div>
    <div id=watchlist-list><div class=empty><span class=spin></span> Loading…</div></div>
  </div>

  <div class=page id=page-scraper>
    <h1>Profile Scraper</h1>
    <div id=scraper-content><div class=empty><span class=spin></span> Loading…</div></div>
  </div>

  <div class=page id=page-settings>
    <h1>Settings</h1>
    <div id=settings-content><div class=empty><span class=spin></span> Loading…</div></div>
  </div>

  <div class=page id=page-admin>
    <h1>Server <span class=meta style="font-weight:400;font-size:13px;color:var(--muted);margin-left:8px">(owner-only · server-wide controls)</span></h1>
    <div id=admin-content><div class=empty><span class=spin></span> Loading…</div></div>
  </div>
</div>

<div class=preview-modal id=preview-modal>
  <div class=preview-head>
    <div class=name id=preview-name></div>
    <button onclick="downloadCurrentPreview()">⬇ Download</button>
    <button onclick="closePreview()">✕</button>
  </div>
  <div class=preview-body id=preview-body></div>
</div>

<div class=sidebar>
  <div class=sidebar-toggle id=nav-toggle onclick="toggleSidebar()" title="Collapse / expand nav">
    <span id=nav-toggle-icon>«</span>
  </div>
  <div class="sidebar-item active" id=nav-home onclick="goto('home')">
    <div class=icon>🏠</div><div class=label>Home</div>
  </div>
  <div class=sidebar-item id=nav-downloads onclick="goto('downloads')">
    <div class=icon>📥</div><div class=label>DL</div>
  </div>
  <div class=sidebar-item id=nav-watchlist onclick="goto('watchlist')">
    <div class=icon>👁</div><div class=label>Streams</div>
  </div>
  <div class=sidebar-item id=nav-live onclick="location.href='/iptv'">
    <div class=icon>📺</div><div class=label>IPTV</div>
  </div>
  <div class="sidebar-item admin-only" id=nav-files onclick="goto('files')">
    <div class=icon>📁</div><div class=label>Files</div>
  </div>
  <div class="sidebar-item admin-only" id=tab-scraper onclick="goto('scraper')">
    <div class=icon>🤖</div><div class=label>Scrape</div>
  </div>
  <div class="sidebar-item admin-only" id=tab-admin onclick="goto('admin')">
    <div class=icon>🛡</div><div class=label>Server</div>
  </div>
  <div class=sidebar-spacer></div>
  <div class=sidebar-divider></div>
  <div class=sidebar-item id=nav-settings onclick="goto('settings')">
    <div class=icon>⚙️</div><div class=label>Settings</div>
  </div>
</div>

<script>
const tg = window.Telegram?.WebApp;
if (tg) { tg.ready(); tg.expand(); }
const initData = tg?.initData || '';
let current = 'home';

// ── Page history stack ─────────────────────────────────────────────────
// Maintains a back-navigation trail for the device back button (Android)
// and Telegram's BackButton chrome. On every goto() we push the page
// we're LEAVING; the back button pops it. Home is the implicit floor —
// when the stack empties, BackButton hides so the next device-back
// closes the Mini App.
const _pageHistory = [];
const _MAX_HISTORY = 25;
let _suppressHistory = false;   // set during back-pop so we don't re-push

function pushHistory(fromPage) {
  if (_suppressHistory) return;
  if (!fromPage) return;
  // De-dup: don't push the same page twice in a row
  if (_pageHistory[_pageHistory.length - 1] === fromPage) return;
  _pageHistory.push(fromPage);
  if (_pageHistory.length > _MAX_HISTORY) _pageHistory.shift();
  updateBackButton();
}

function popHistory() {
  // If the file-preview modal is open, back closes it first — don't
  // pop the page stack until the user is back at the Files page.
  const modal = document.getElementById('preview-modal');
  if (modal && modal.classList.contains('open')) {
    closePreview();
    return;
  }
  // Inside the Files page in a subfolder, back walks UP one folder
  // before leaving the page entirely. Mirrors normal file-manager UX.
  if (current === 'files' && filesCwd) {
    const parts = filesCwd.split('/').filter(Boolean);
    parts.pop();
    const parent = parts.join('/');
    loadFiles(parent);
    return;
  }
  if (!_pageHistory.length) return;
  const prev = _pageHistory.pop();
  _suppressHistory = true;
  try { goto(prev); } finally { _suppressHistory = false; }
  updateBackButton();
}

function updateBackButton() {
  if (!tg || !tg.BackButton) return;
  if (_pageHistory.length > 0) tg.BackButton.show();
  else                          tg.BackButton.hide();
}

if (tg && tg.BackButton) {
  try { tg.BackButton.onClick(popHistory); } catch(e) { /* older TG client */ }
}

// Sidebar collapse preference — persisted across sessions in localStorage.
// Default = expanded (false). Restored on page load so the layout doesn't
// flicker between expanded and collapsed states.
function applySidebarState(collapsed) {
  document.body.classList.toggle('sidebar-collapsed', !!collapsed);
  const ico = document.getElementById('nav-toggle-icon');
  if (ico) ico.textContent = collapsed ? '»' : '«';
}
function toggleSidebar() {
  const next = !document.body.classList.contains('sidebar-collapsed');
  applySidebarState(next);
  try { localStorage.setItem('smdl_sidebar_collapsed', next ? '1' : '0'); } catch {}
}
try {
  applySidebarState(localStorage.getItem('smdl_sidebar_collapsed') === '1');
} catch {}

// ── #41: drag-to-reorder home tiles ─────────────────────────────────────────
// Pointer events (not HTML5 draggable) so it works under touch in the Telegram
// WebView as well as with a mouse. Order is a per-device preference in
// localStorage; it stores tile *keys* (data-tile) so it survives markup edits
// and new tiles append at the end rather than breaking the saved layout.
const TILE_ORDER_KEY = 'smdl_tile_order';
let _tileEdit = false;
let _tileDrag = null;

function _tileContainer() { return document.getElementById('home-tiles'); }

function applyTileOrder() {
  const c = _tileContainer();
  if (!c) return;
  let saved = [];
  try { saved = JSON.parse(localStorage.getItem(TILE_ORDER_KEY) || '[]'); } catch {}
  if (!Array.isArray(saved) || !saved.length) return;
  const present = [...c.querySelectorAll('.home-tile')];
  const byKey = new Map(present.map(el => [el.dataset.tile, el]));
  const ordered = [];
  saved.forEach(k => { if (byKey.has(k)) { ordered.push(byKey.get(k)); byKey.delete(k); } });
  present.forEach(el => { if (byKey.has(el.dataset.tile)) ordered.push(el); }); // unsaved → keep at end
  ordered.forEach(el => c.appendChild(el));
}

function saveTileOrder() {
  const c = _tileContainer();
  if (!c) return;
  const order = [...c.querySelectorAll('.home-tile')].map(el => el.dataset.tile);
  try { localStorage.setItem(TILE_ORDER_KEY, JSON.stringify(order)); } catch {}
}

function toggleTileEdit() {
  _tileEdit = !_tileEdit;
  const c = _tileContainer();
  const btn = document.getElementById('tiles-arrange-btn');
  if (c) c.classList.toggle('editing', _tileEdit);
  if (btn) { btn.classList.toggle('on', _tileEdit); btn.textContent = _tileEdit ? '✓ Done' : '✥ Arrange'; }
  if (!_tileEdit) saveTileOrder();
}

function initTileReorder() {
  const c = _tileContainer();
  if (!c) return;
  applyTileOrder();
  // Capture-phase click guard: in edit mode, swallow the click before it
  // reaches a tile's inline onclick so dragging never navigates away.
  c.addEventListener('click', e => {
    if (_tileEdit) { e.preventDefault(); e.stopPropagation(); }
  }, true);
  c.addEventListener('pointerdown', e => {
    if (!_tileEdit) return;
    const tile = e.target.closest('.home-tile');
    if (!tile) return;
    _tileDrag = tile;
    tile.classList.add('dragging');
    try { tile.setPointerCapture(e.pointerId); } catch {}
  });
  c.addEventListener('pointermove', e => {
    if (!_tileDrag) return;
    e.preventDefault();
    const over = document.elementFromPoint(e.clientX, e.clientY)?.closest?.('.home-tile');
    if (!over || over === _tileDrag || over.parentElement !== c) return;
    const tiles = [...c.querySelectorAll('.home-tile')];
    if (tiles.indexOf(_tileDrag) < tiles.indexOf(over)) c.insertBefore(_tileDrag, over.nextSibling);
    else c.insertBefore(_tileDrag, over);
  });
  const endDrag = () => {
    if (!_tileDrag) return;
    _tileDrag.classList.remove('dragging');
    _tileDrag = null;
    saveTileOrder();
  };
  c.addEventListener('pointerup', endDrag);
  c.addEventListener('pointercancel', endDrag);
}

let watchlistTimer = null;

function api(path, opts = {}) {
  return fetch(path, {
    ...opts,
    headers: {
      'X-Init-Data': initData,
      'Content-Type': 'application/json',
      ...(opts.headers || {}),
    },
  }).then(async r => {
    if (r.ok) return r.json();
    // Error path: body may be JSON (FastAPI {"detail":...}), plain text
    // (uvicorn "Internal Server Error"), or HTML (proxy error page). Read
    // as text first so a non-JSON 5xx never raises SyntaxError at the
    // call site — that used to surface as "SyntaxError: Unexpected token
    // 'I', \"Internal S\"... is not valid JSON" at the top of the page.
    const txt = await r.text();
    let msg = 'HTTP ' + r.status;
    try {
      const j = JSON.parse(txt);
      msg = j.detail || j.error || msg;
    } catch {
      if (txt) msg += ': ' + txt.slice(0, 120).replace(/\s+/g, ' ').trim();
    }
    return Promise.reject(msg);
  });
}

function showOk(t) { const m = document.getElementById('msg'); m.className = 'msg ok'; m.textContent = t; setTimeout(()=>m.className='', 3500); }
function showErr(t) { const m = document.getElementById('msg'); m.className = 'msg err'; m.textContent = String(t); setTimeout(()=>m.className='', 5500); }

function esc(s) { return String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
function timeago(iso) {
  if (!iso) return '';
  const t = new Date(iso).getTime();
  const s = Math.floor((Date.now()-t)/1000);
  if (s<60) return s+'s ago';
  if (s<3600) return Math.floor(s/60)+'m ago';
  if (s<86400) return Math.floor(s/3600)+'h ago';
  return Math.floor(s/86400)+'d ago';
}
function bytes(n) { if (!n) return '0 B'; const u = ['B','KB','MB','GB']; let i = 0; while (n>=1024 && i<u.length-1) { n/=1024; i++; } return n.toFixed(1)+' '+u[i]; }
function duration(s) { if (s<60) return s+'s'; const m = Math.floor(s/60); const sec = s%60; if (m<60) return m+'m '+sec+'s'; return Math.floor(m/60)+'h '+(m%60)+'m'; }

function goto(page) {
  if (page !== current) pushHistory(current);
  current = page;
  document.querySelectorAll('.page').forEach(p => p.classList.toggle('active', p.id === 'page-'+page));
  // Mark the sidebar entry active. Map page name → element id; 'live'
  // never lands here because it navigates away via location.href, so
  // we never light up nav-live from this function.
  const navMap = {
    home: 'nav-home', downloads: 'nav-downloads', watchlist: 'nav-watchlist',
    files: 'nav-files', scraper: 'tab-scraper', admin: 'tab-admin',
    settings: 'nav-settings',
  };
  const targetId = navMap[page];
  document.querySelectorAll('.sidebar-item').forEach(el =>
    el.classList.toggle('active', el.id === targetId));
  if (page === 'downloads') loadDownloads();
  else if (page === 'watchlist') loadWatchlist();
  else if (page === 'files') loadFiles(filesCwd);
  else if (page === 'scraper') loadScraper();
  else if (page === 'settings') loadSettings();
  else if (page === 'admin') loadAdmin();

  // Watchlist auto-refresh so an in-progress recording's size + duration
  // tick up and a streamer going LIVE flips colour without manual reload.
  if (watchlistTimer) { clearInterval(watchlistTimer); watchlistTimer = null; }
  if (page === 'watchlist') watchlistTimer = setInterval(loadWatchlist, 5000);
}

async function loadDownloads() {
  try {
    const j = await api('/api/miniapp/downloads?limit=50');
    const root = document.getElementById('downloads-list');
    if (!j.items.length) {
      root.innerHTML = '<div class=empty>No downloads yet.</div>';
      return;
    }
    // Simplified row: @username · description as one clickable line.
    // Description = trailing URL segment (post shortcode / filename basename).
    root.innerHTML = j.items.map(d => {
      const url  = d.url || '';
      const user = d.uploader || d.platform || 'unknown';
      // Pick a description: last meaningful path segment from the URL.
      let desc = '';
      try {
        const parts = new URL(url).pathname.split('/').filter(Boolean);
        // Skip platform-noise segments like "p", "reel", "@user" — take the last identifier.
        desc = parts[parts.length - 1] || parts[parts.length - 2] || '';
      } catch { desc = url; }
      if (!desc && (d.files || []).length) desc = d.files[0].split('/').pop();
      const u = encodeURIComponent(url);
      return `
        <div class=dl-row>
          <a onclick="openExternal('${u}')">
            <div class=user>@${esc(user)}</div>
            <div class=desc>${esc(desc || url)}</div>
            <div class=when>${timeago(d.downloaded_at || d.created_at)}</div>
          </a>
        </div>`;
    }).join('');
  } catch(e) { showErr('Load failed: '+e); }
}

async function clearDownloadHistory() {
  if (!confirm('Wipe your entire download history? The actual files on disk stay; only the in-app history rows are deleted.')) return;
  try {
    const r = await api('/api/miniapp/downloads/clear', { method: 'POST' });
    showOk(`Cleared ${r.deleted || 0} row(s)`);
    loadDownloads();
  } catch(e) { showErr(e); }
}

async function submitDownloadBatch() {
  const ta  = document.getElementById('dl-batch-input');
  const go  = document.getElementById('dl-batch-go');
  const stt = document.getElementById('dl-batch-status');
  const raw = (ta.value || '').trim();
  if (!raw) return;
  // Split on any whitespace OR comma OR semicolon. Filter to look-like-URLs.
  const urls = Array.from(new Set(
    raw.split(/[\s,;]+/).map(s => s.trim()).filter(s => /^https?:\/\//i.test(s))
  ));
  if (!urls.length) { showErr('No http(s) URLs found in input.'); return; }
  go.disabled = true; stt.textContent = `Queueing ${urls.length}…`;
  try {
    const r = await api('/api/miniapp/downloads/batch', {
      method: 'POST', body: JSON.stringify({ urls })
    });
    ta.value = '';
    const rej = r.rejected ? ` · ${r.rejected} rejected` : '';
    stt.textContent = `✓ Queued ${r.accepted}${rej} — refresh below as they land.`;
    setTimeout(() => { stt.textContent = ''; loadDownloads(); }, 1200);
    setTimeout(() => loadDownloads(), 5000);
    setTimeout(() => loadDownloads(), 15000);
  } catch(e) {
    stt.textContent = '';
    showErr('Batch failed: ' + e);
  } finally {
    go.disabled = false;
  }
}

// ── Files browser (SFTP-style /downloads access) ────────────────────────
let filesCwd = '';
let filesViewMode = 'list';
try {
  const saved = localStorage.getItem('smdl_files_view');
  if (saved === 'small' || saved === 'medium' || saved === 'list') filesViewMode = saved;
} catch {}

function setFilesView(mode) {
  if (!['list','small','medium'].includes(mode)) return;
  filesViewMode = mode;
  try { localStorage.setItem('smdl_files_view', mode); } catch {}
  loadFiles(filesCwd);
}

function fmtSize(n) {
  if (!n) return '0 B';
  const u = ['B', 'KB', 'MB', 'GB'];
  let i = 0; while (n >= 1024 && i < u.length-1) { n /= 1024; i++; }
  return n.toFixed(n < 10 && i ? 1 : 0) + ' ' + u[i];
}
function fmtDate(unix) {
  if (!unix) return '';
  const d = new Date(unix * 1000);
  return d.toLocaleString();
}

async function loadFiles(path) {
  filesCwd = path || '';
  const listRoot   = document.getElementById('files-list');
  const crumbsRoot = document.getElementById('files-crumbs');
  listRoot.innerHTML   = '<div class=empty><span class=spin></span> Loading…</div>';
  crumbsRoot.innerHTML = '';
  try {
    const q = filesCwd ? '?path=' + encodeURIComponent(filesCwd) : '';
    const j = await api('/api/miniapp/files/list' + q);
    // Breadcrumbs
    crumbsRoot.innerHTML = j.crumbs.map((c, i) => {
      const sep = i > 0 ? `<span class=sep>/</span>` : '';
      const safePath = c.path.replace(/'/g, "\\'");
      return `${sep}<a onclick="loadFiles('${safePath}')">${esc(c.name === '/' ? '📁 root' : c.name)}</a>`;
    }).join('');
    // Sync the dropdown to the persisted state
    const sel = document.getElementById('files-view');
    if (sel) sel.value = filesViewMode;

    // Kind classifier — used in both list and tile renderers.
    // thumbable=true → grid view requests a server-side thumbnail (#32)
    // via /api/miniapp/files/thumb. Image AND video both qualify.
    const kindOf = (name) => {
      const ext = (name.split('.').pop() || '').toLowerCase();
      if (['mp4','mov','mkv','webm','m4v','avi','ts','mts','m2ts'].includes(ext)) return {ico:'🎬', isImg:false, thumbable:true};
      if (['jpg','jpeg','png','gif','webp','heic','heif','avif','bmp','tiff','tif'].includes(ext)) return {ico:'🖼', isImg:true, thumbable:true};
      if (['mp3','m4a','aac','flac','wav','opus','ogg'].includes(ext)) return {ico:'🎵', isImg:false, thumbable:false};
      if (['zip','tar','gz','7z'].includes(ext)) return {ico:'📦', isImg:false, thumbable:false};
      return {ico:'📄', isImg:false, thumbable:false};
    };

    const onClickFile = (f) => f.share_url
      ? `openPreview('${encodeURIComponent(f.share_url)}', '${encodeURIComponent(f.name)}')`
      : `showErr('No share URL — SHARE_SECRET/PUBLIC_BASE_URL not configured')`;

    if (j.folders.length === 0 && j.files.length === 0) {
      listRoot.innerHTML = '<div class=empty>Folder is empty.</div>';
      return;
    }

    if (filesViewMode === 'list') {
      // Original list view (single column rows)
      const rows = [];
      for (const d of j.folders) {
        const safePath = d.path.replace(/'/g, "\\'");
        rows.push(`
          <div class=file-row onclick="loadFiles('${safePath}')">
            <div class=file-ico>📂</div>
            <div class=grow>
              <div class=file-name>${esc(d.name)}/</div>
              <div class=file-meta>${fmtDate(d.mtime)}</div>
            </div>
          </div>`);
      }
      for (const f of j.files) {
        const k = kindOf(f.name);
        rows.push(`
          <div class=file-row onclick="${onClickFile(f)}">
            <div class=file-ico>${k.ico}</div>
            <div class=grow>
              <div class=file-name>${esc(f.name)}</div>
              <div class=file-meta>${fmtSize(f.size)} · ${fmtDate(f.mtime)}</div>
            </div>
          </div>`);
      }
      listRoot.innerHTML = rows.join('');
    } else {
      // Tile views — small or medium grid. Image AND video files render as
      // <img> via the server-side thumbnailer (#32) which generates JPEG
      // thumbs lazily and serves them with a 30-day Cache-Control. Other
      // kinds (audio, archive, doc) render as a centered emoji.
      // On 4xx/5xx from the thumb endpoint, the <img onerror> falls back
      // to the emoji icon so missing/corrupt files don't leave a broken-
      // image placeholder.
      const gridClass = filesViewMode === 'small' ? 'files-grid-sm' : 'files-grid-md';
      const tiles = [];
      for (const d of j.folders) {
        const safePath = d.path.replace(/'/g, "\\'");
        tiles.push(`
          <div class=file-folder-tile onclick="loadFiles('${safePath}')">
            <div class=emoji>📂</div>
            <div class=label>${esc(d.name)}</div>
          </div>`);
      }
      for (const f of j.files) {
        const k = kindOf(f.name);
        const thumb = k.thumbable
          ? `<img loading=lazy alt="${esc(f.name)}"
                 src="/api/miniapp/files/thumb?path=${encodeURIComponent(f.path)}"
                 onerror="this.outerHTML='<div class=emoji>${k.ico}</div>'">`
          : `<div class=emoji>${k.ico}</div>`;
        tiles.push(`
          <div class=file-tile onclick="${onClickFile(f)}">
            <div class=thumb>${thumb}</div>
            <div class=label title="${esc(f.name)}">${esc(f.name)}</div>
          </div>`);
      }
      listRoot.innerHTML = `<div class="${gridClass}">${tiles.join('')}</div>`;
    }
  } catch(e) { showErr('Load files failed: '+e); }
}

async function uploadToOneDrive(encodedUrl, btn) {
  const url = decodeURIComponent(encodedUrl);
  const original = btn.textContent;
  btn.disabled = true; btn.textContent = '…';
  try {
    const r = await api('/api/miniapp/onedrive/upload', {
      method: 'POST', body: JSON.stringify({url}),
    });
    if (r.failed_count) {
      showErr(`Uploaded ${r.sent_count}, ${r.failed_count} failed`);
    } else {
      showOk(`Uploaded ${r.sent_count} file${r.sent_count===1?'':'s'} · ${bytes(r.total_bytes)}`);
    }
    btn.textContent = '✓';
  } catch(e) {
    showErr(e); btn.textContent = original; btn.disabled = false;
  }
}

function statusLabel(s) {
  if (s === 'live') return 'LIVE';
  if (s === 'offline') return 'offline';
  return 'unknown';
}

// Platform → emoji prefix for group headers. Falls through to a generic icon.
const PLATFORM_ICON = {
  'Chaturbate': '🎥', 'Stripchat': '🎥', 'BongaCams': '🎥', 'Cam4': '🎥',
  'Twitch': '🎮', 'Kick': '🥊', 'YouTube': '▶', 'Instagram': '📷',
  'TikTok': '🎵', 'Twitter/X': '𝕏', 'Facebook': '👤', 'Reddit': '🤖',
  'Vimeo': '🎞', 'Rumble': '🎬', 'DLive': '📡', 'Trovo': '📡',
  'Bilibili': '📺', 'Douyu': '📺',
};

// Watchlist tile grid (#30). One small tile per streamer:
//   line 1: 🟢 @username
//   line 2: LIVE/offline + label + muted/auto chips
//   line 3: ▶/⏹ rec  ·  🔔/🔕 mute  ·  🎬 auto-rec  ·  ✎ edit  ·  🗑 remove
// Per-platform sections are collapsible (state persisted in
// localStorage as smdl_wl_collapsed:<platform>). Each section has its
// own Mute-All / Unmute-All buttons.
function wlCollapsedKey(platform) { return 'smdl_wl_collapsed:' + (platform || 'Other'); }
function wlIsCollapsed(platform) {
  try { return localStorage.getItem(wlCollapsedKey(platform)) === '1'; } catch { return false; }
}
function wlToggleCollapse(platform) {
  const cur = wlIsCollapsed(platform);
  try { localStorage.setItem(wlCollapsedKey(platform), cur ? '0' : '1'); } catch {}
  const sec = document.getElementById('wl-sec-' + cssEscapeId(platform));
  if (sec) sec.classList.toggle('collapsed', !cur);
}
function cssEscapeId(s) { return String(s).replace(/[^a-z0-9_-]/gi, '_'); }

async function loadWatchlist() {
  try {
    const j = await api('/api/miniapp/watchlist');
    const root = document.getElementById('watchlist-list');
    if (!j.items.length) { root.innerHTML = '<div class=empty>Watchlist is empty.</div>'; return; }
    const active = j.active || {};

    // Group by platform; server already sorted by (platform, username).
    const groups = new Map();
    for (const w of j.items) {
      const k = w.platform || 'Other';
      if (!groups.has(k)) groups.set(k, []);
      groups.get(k).push(w);
    }

    let idx = 0;
    const sections = [];
    for (const [platform, rows] of groups) {
      const icon = PLATFORM_ICON[platform] || '🌐';
      const collapsed = wlIsCollapsed(platform);
      const platformLit = JSON.stringify(platform);
      const allMuted = rows.every(w => !!w.muted);
      const bulkBtn = allMuted
        ? `<button onclick='bulkMute(${platformLit}, false)' title="Unmute every ${esc(platform)} entry">🔔 Unmute all</button>`
        : `<button onclick='bulkMute(${platformLit}, true)' title="Mute every ${esc(platform)} entry">🔕 Mute all</button>`;
      const tiles = rows.map(w => {
        const i = idx++;
        const status = w.status || 'unknown';
        const muted  = !!w.muted;
        const auto   = !!w.auto_record;
        const u = encodeURIComponent(w.url);
        const job = active[w.url];
        const recording = !!job;
        let sub;
        if (recording) {
          sub = `<span class=rec-tag>● REC</span> · ${duration(job.elapsed_sec)} · ${bytes(job.bytes)}`;
        } else {
          const pieces = [statusLabel(status)];
          if (auto)  pieces.push('🎬 auto');
          if (muted) pieces.push('🔕');
          sub = pieces.join(' · ');
        }
        const actionBtn = recording
          ? `<button class="rec-on" title="Stop recording" onclick="stopFromWatchlist(${job.chat_id})">⏹</button>`
          : `<button title="Start recording" onclick="recFromWatchlist('${u}')">▶</button>`;
        return `
        <div class="wl-tile ${recording?'recording':''}">
          <div class=uname>
            <span class="dot ${esc(status)}" title="${esc(statusLabel(status))}"></span>
            <a class=u-link onclick="openExternal('${u}')" title="${esc(w.url)}">${esc(w.username || w.url)}</a>
          </div>
          <div class=sub title="${esc(w.label || '')}">${sub}</div>
          <div class=actions>
            ${actionBtn}
            <button class="${muted?'on':''}" title="${esc(muted?'Unmute':'Mute notifications')}"
                    onclick="toggleMute('${u}', ${muted?'false':'true'})">${muted?'🔕':'🔔'}</button>
            <button class="${auto?'auto-on':''}" title="Auto-record when live (skip Yes/No prompt)"
                    onclick="toggleAutoRecord('${u}', ${auto?'false':'true'})">🎬</button>
            <button title="Edit URL" onclick="toggleEdit(${i})">✎</button>
            <button title="Remove" onclick="removeWatch('${u}')">🗑</button>
          </div>
          <div class="wl-edit" id="wl-edit-${i}">
            <div class=field>URL</div>
            <input id="wl-url-${i}" value="${esc(w.url)}">
            <div class=field>Label (optional)</div>
            <input id="wl-label-${i}" value="${esc(w.label || '')}">
            <div class=row style="display:flex;gap:4px;margin-top:4px">
              <button onclick="toggleEdit(${i})">Cancel</button>
              <button onclick="saveEdit(${i}, '${u}')">Save</button>
            </div>
          </div>
        </div>`;
      }).join('');
      const safeId = cssEscapeId(platform);
      sections.push(`
        <div class="wl-site-section ${collapsed?'collapsed':''}" id="wl-sec-${safeId}">
          <div class=wl-site-bar>
            <span class=caret onclick='wlToggleCollapse(${platformLit})'>▼</span>
            <span class=title onclick='wlToggleCollapse(${platformLit})'>${icon} ${esc(platform)}</span>
            <span class=count>${rows.length}</span>
            <span class=bulk>${bulkBtn}</span>
          </div>
          <div class=wl-tiles>${tiles}</div>
        </div>`);
    }
    root.innerHTML = sections.join('');
  } catch(e) { showErr('Load failed: '+e); }
}

async function toggleAutoRecord(encodedUrl, on) {
  const url = decodeURIComponent(encodedUrl);
  try {
    await api('/api/miniapp/watchlist/auto_record', {
      method: 'POST', body: JSON.stringify({url, auto_record: !!on}),
    });
    showOk(on ? '🎬 Auto-record enabled' : 'Auto-record disabled');
    loadWatchlist();
  } catch(e) { showErr(e); }
}

async function bulkMute(platform, muted) {
  try {
    const r = await api('/api/miniapp/watchlist/bulk_mute', {
      method: 'POST', body: JSON.stringify({platform, muted}),
    });
    showOk(`${muted ? '🔕 Muted' : '🔔 Unmuted'} ${r.count} ${esc(platform)} entr${r.count===1?'y':'ies'}`);
    loadWatchlist();
  } catch(e) { showErr(e); }
}

async function recFromWatchlist(encodedUrl) {
  const url = decodeURIComponent(encodedUrl);
  try {
    await api('/api/miniapp/stream/start', {
      method: 'POST',
      body: JSON.stringify({url}),
    });
    showOk('Recording queued · ' + url);
    setTimeout(loadWatchlist, 1200);
  } catch(e) { showErr(e); }
}

async function stopFromWatchlist(chat_id) {
  try {
    const j = await api('/api/miniapp/stream/stop', {
      method: 'POST',
      body: JSON.stringify({chat_id}),
    });
    showOk('Stop sent · ' + duration(j.status.elapsed_seconds));
    setTimeout(loadWatchlist, 1000);
  } catch(e) { showErr(e); }
}

function toggleEdit(i) {
  const el = document.getElementById('wl-edit-' + i);
  if (el) el.classList.toggle('open');
}

async function saveEdit(i, encodedOldUrl) {
  const oldUrl = decodeURIComponent(encodedOldUrl);
  const newUrl = document.getElementById('wl-url-'   + i).value.trim();
  const label  = document.getElementById('wl-label-' + i).value.trim();
  if (!newUrl) { showErr('URL cannot be empty'); return; }
  try {
    await api('/api/miniapp/watchlist/edit', {
      method: 'POST',
      body: JSON.stringify({url: oldUrl, new_url: newUrl, label: label}),
    });
    showOk('Updated');
    loadWatchlist();
  } catch(e) { showErr(e); }
}

// ── Inline file preview ────────────────────────────────────────────────
// Plays / shows media inline using <video>/<img>/<audio> tags. Since
// these embed the resource (rather than navigate to it), the browser
// ignores the FileResponse's Content-Disposition:attachment and just
// renders the file. For non-media types, shows a friendly "use the
// download button" message + still gives access via the modal header.
let _previewUrl = '';
let _previewName = '';

function openPreview(encodedUrl, encodedName) {
  _previewUrl  = decodeURIComponent(encodedUrl);
  _previewName = decodeURIComponent(encodedName);
  const ext = (_previewName.split('.').pop() || '').toLowerCase();
  const body = document.getElementById('preview-body');
  const nameEl = document.getElementById('preview-name');
  nameEl.textContent = _previewName;

  let inner;
  if (['mp4','mov','mkv','webm','m4v'].includes(ext)) {
    inner = `<video src="${_previewUrl}" controls autoplay playsinline></video>`;
  } else if (['jpg','jpeg','png','gif','webp','heic','avif','bmp'].includes(ext)) {
    inner = `<img src="${_previewUrl}" alt="${esc(_previewName)}">`;
  } else if (['mp3','m4a','aac','flac','wav','opus','ogg'].includes(ext)) {
    inner = `<audio src="${_previewUrl}" controls autoplay></audio>`;
  } else {
    inner = `<div class=non-media>
      No inline preview for <code>.${esc(ext || 'file')}</code> files.<br>
      Use the ⬇ Download button above to fetch it.
    </div>`;
  }
  body.innerHTML = inner;
  document.getElementById('preview-modal').classList.add('open');
}

function closePreview() {
  const body = document.getElementById('preview-body');
  // Stop any playing media before the modal closes
  body.querySelectorAll('video, audio').forEach(el => { try { el.pause(); el.src = ''; } catch{} });
  body.innerHTML = '';
  document.getElementById('preview-modal').classList.remove('open');
  _previewUrl = '';
  _previewName = '';
}

function downloadCurrentPreview() {
  if (!_previewUrl) return;
  // tg.openLink fires the browser-level navigation that respects
  // Content-Disposition:attachment and triggers a real download.
  openExternal(encodeURIComponent(_previewUrl));
}

function openExternal(encodedUrl) {
  // Open the URL in the user's external browser. Inside Telegram, prefer
  // tg.openLink (gives the user the "Open in Chrome / Safari" prompt with
  // their default browser); fall back to window.open elsewhere.
  let url = decodeURIComponent(encodedUrl);
  // Defensive: if the stored URL has no scheme (e.g. "www.twitch.tv/foo"),
  // tg.openLink treats it as a relative path → resolves against the Mini
  // App's own origin → 404. Add https:// when scheme is missing.
  const lc = url.toLowerCase();
  if (!(lc.startsWith('http://') || lc.startsWith('https://'))) {
    url = 'https://' + url.replace(/^\/+/, '');
  }
  try {
    if (tg && tg.openLink) tg.openLink(url);
    else window.open(url, '_blank', 'noopener,noreferrer');
  } catch(e) {
    window.open(url, '_blank', 'noopener,noreferrer');
  }
}

async function toggleMute(encodedUrl, muted) {
  const url = decodeURIComponent(encodedUrl);
  try {
    await api('/api/miniapp/watchlist/mute', {
      method: 'POST',
      body: JSON.stringify({url, muted: (muted === 'true' || muted === true)}),
    });
    loadWatchlist();
  } catch(e) { showErr(e); }
}

async function addWatch() {
  const url = document.getElementById('watch-url').value.trim();
  if (!url) { showErr('URL required'); return; }
  try {
    // Label is now auto-extracted server-side from the URL (e.g.
    // chaturbate.com/dewdropdoll → dewdropdoll). Rename later via the
    // ✎ button on the row if you want something custom.
    await api('/api/miniapp/watchlist/add', { method: 'POST', body: JSON.stringify({url, label: null}) });
    showOk('Added');
    document.getElementById('watch-url').value = '';
    const r = document.getElementById('watch-test-result');
    if (r) r.innerHTML = '';
    loadWatchlist();
  } catch(e) { showErr(e); }
}

async function testWatchUrl() {
  const inputEl = document.getElementById('watch-url');
  const resultEl = document.getElementById('watch-test-result');
  const url = (inputEl?.value || '').trim();
  if (!url) { showErr('Paste a URL first.'); return; }
  resultEl.innerHTML = '<div class=meta><span class=spin></span> Probing…</div>';
  try {
    const r = await api('/api/miniapp/test_url', {
      method: 'POST', body: JSON.stringify({url}),
    });
    if (!r.ok) {
      resultEl.innerHTML = `<div class=meta style="color:var(--destructive)">${esc(r.error || 'Failed')}</div>`;
      return;
    }
    const ok = '<span style="color:var(--success)">✓</span>';
    const no = '<span style="color:var(--destructive)">✗</span>';
    const q = '<span style="color:#ff9500">?</span>';
    const lines = [];
    lines.push(`${r.recognised ? ok : q} <b>${esc(r.platform || 'unknown')}</b>${r.recognised ? '' : ' <span class=meta>(unknown site)</span>'}`);
    if (r.recognised && r.platform !== 'private category') {
      lines.push(`${r.live_supported ? ok : no} Live recording ${r.live_supported ? 'supported' : 'not supported'}`);
    }
    lines.push(`${r.available ? ok : no} ${r.available ? 'Available to you' : 'Not available'}`);
    if (r.reason) lines.push(`<div class=meta style="margin-top:6px">${esc(r.reason)}</div>`);
    resultEl.innerHTML = lines.map(l => `<div style="margin:4px 0">${l}</div>`).join('');
  } catch(e) {
    resultEl.innerHTML = `<div class=meta style="color:var(--destructive)">${esc(String(e))}</div>`;
  }
}

async function removeWatch(encodedUrl) {
  const url = decodeURIComponent(encodedUrl);
  if (tg?.showConfirm) {
    tg.showConfirm('Remove this from watchlist?', async (ok) => { if (ok) await _doRemoveWatch(url); });
  } else if (confirm('Remove ' + url + '?')) {
    await _doRemoveWatch(url);
  }
}

async function _doRemoveWatch(url) {
  try {
    await api('/api/miniapp/watchlist/remove', { method: 'POST', body: JSON.stringify({url}) });
    showOk('Removed');
    loadWatchlist();
  } catch(e) { showErr(e); }
}

// The Live tab was removed — recording is now triggered from the Watchlist
// row (and from bot inline prompts when a stream goes live). The
// /api/miniapp/stream/* + /api/miniapp/active endpoints stay live for those
// flows; they just aren't surfaced in a dedicated tab anymore.

// The old loadSites/testSiteUrl functions were removed when the Sites tab
// was consolidated into the Watchlist add card. testWatchUrl() replaces
// testSiteUrl(); the Sites tab no longer exists.

function _renderSettingField(s, v, idPrefix) {
  const id = idPrefix + s.key;
  let input;
  if (s.type === 'choice') {
    input = `<select id="${id}">${s.choices.map(c => `<option ${c===v?'selected':''} value="${esc(c)}">${esc(c)}</option>`).join('')}</select>`;
  } else if (s.type === 'bool') {
    input = `<select id="${id}"><option value=true ${v?'selected':''}>Yes</option><option value=false ${!v?'selected':''}>No</option></select>`;
  } else if (s.type === 'string') {
    input = `<input id="${id}" type=text value="${esc(v ?? '')}">`;
  } else {
    input = `<input id="${id}" type=number ${s.min!=null?'min='+s.min:''} ${s.max!=null?'max='+s.max:''} value="${v ?? ''}">`;
  }
  const restart = s.needs_restart ? ' <span style="color:#ff9500;font-size:11px">· restart required</span>' : '';
  return `<div class=card>
    <div class=field>${esc(s.label)}${restart}</div>
    ${input}
  </div>`;
}

async function loadSettings() {
  const root = document.getElementById('settings-content');
  root.innerHTML = '<div class=empty><span class=spin></span> Loading…</div>';
  try {
    const cfg = await api('/api/miniapp/config');
    // Settings tab = only NON-admin keys. Admin keys live on the Admin tab.
    const visible = cfg.settings.filter(s => !s.admin);
    const fields = visible.map(s => _renderSettingField(s, cfg.values[s.key], 'set-')).join('');
    const disk = cfg.disk;
    const diskHtml = disk.free_gb != null
      ? `<div class=meta>${disk.free_gb} GB free of ${disk.total_gb} GB · ${disk.used_gb} GB used</div>`
      : `<div class=meta>(disk usage unavailable)</div>`;

    // OneDrive — moved here from Admin tab (it's an integration setting,
    // not an admin-only operational tool). Only the owner gets the full
    // connect/disconnect controls; non-owner sees a read-only status.
    let odHtml = '';
    try {
      const od = await api('/api/miniapp/onedrive/status');
      let odBody;
      if (od.device_flow) {
        odBody = `
          <div class=name>⏳ Awaiting authorization</div>
          <div style="margin-top:10px;padding:10px;background:var(--bg);border-radius:8px">
            <div class=meta>Open this URL on any device:</div>
            <div style="margin:6px 0;font-size:14px;word-break:break-all">
              <a href="${esc(od.device_flow.verification_uri)}" target=_blank>${esc(od.device_flow.verification_uri)}</a>
            </div>
            <div class=meta>Enter this code:</div>
            <div style="font-family:ui-monospace;font-size:22px;letter-spacing:3px;font-weight:700;margin-top:4px">
              ${esc(od.device_flow.user_code)}
            </div>
            <div class=meta style="margin-top:6px">Expires in <span id=od-expires>${od.device_flow.expires_in}</span>s.</div>
          </div>`;
        if (!window._odPoll) {
          window._odPoll = setInterval(_pollOneDriveDuringConnect, 3000);
          setTimeout(() => { if (window._odPoll) { clearInterval(window._odPoll); window._odPoll = null; } }, 12*60*1000);
        }
      } else if (od.configured) {
        if (window._odPoll) { clearInterval(window._odPoll); window._odPoll = null; }
        const q = od.quota;
        odBody = `
          <div class=name>✅ Connected · ${esc(od.account || od.display_name || '?')}</div>
          ${q ? `<div class=meta style="margin-top:4px">${q.free_gb} GB free of ${q.total_gb} GB · ${q.used_gb} GB used ${q.state ? '· ' + esc(q.state) : ''}</div>` : ''}
          <div class=meta>app …${esc(od.client_id_tail)} ${od.token_valid ? '· token healthy' : '· ⚠ refresh failed'}</div>
          ${isOwner ? `
            <div class=btn-row style="margin-top:8px">
              <button class=sec onclick=testOneDrive()>🧪 Test upload</button>
              <button class="small danger" onclick=disconnectOneDrive()>Disconnect</button>
            </div>` : ''}`;
      } else {
        if (window._odPoll) { clearInterval(window._odPoll); window._odPoll = null; }
        odBody = `
          <div class=name>⚪ Not connected</div>
          <div class=meta style="margin-top:4px">Azure app …${esc(od.client_id_tail)} · Files.ReadWrite scope</div>
          ${od.last_error ? `<div class=meta style="color:var(--destructive);margin-top:4px">Last error: ${esc(od.last_error)}</div>` : ''}
          ${isOwner ? `
            <div style="margin-top:8px"><button onclick=connectOneDrive()>🔗 Connect OneDrive</button></div>
            <div class=meta style="margin-top:6px">You'll get a 6-character code to type at microsoft.com/devicelogin.</div>` : ''}`;
      }
      odHtml = `
        <div class=card>
          <div class=field>📁 OneDrive integration</div>
          ${odBody}
        </div>`;
    } catch(_e) { /* status fetch best-effort — non-owners get 403 */ }

    // Path-template card (#34). Lives inside the Downloads tile.
    const tplVal = esc(cfg.values.download_path_template || '{platform}/{uploader}/{title}.{ext}');
    const downloadsTile = `
      <div class=set-tile>
        <div class=head>⬇ Downloads</div>
        <div class=meta><span class=url>${esc(cfg.paths.downloads_dir)}</span>
          ${cfg.paths.downloads_dir_writable ? '<span style="color:var(--success)">· writable</span>' : '<span style="color:var(--destructive)">· not writable</span>'}</div>
        ${diskHtml}
        <div class=field style="margin-top:10px">Path template</div>
        <input id="set-download_path_template" value="${tplVal}" oninput="updatePathTemplatePreview()" placeholder="{platform}/{uploader}/{title}.{ext}">
        <div class=set-pt-tokens>
          <span class=tok onclick="insertPtToken('{service}')">{service}</span>
          <span class=tok onclick="insertPtToken('{platform}')">{platform}</span>
          <span class=tok onclick="insertPtToken('{uploader}')">{uploader}</span>
          <span class=tok onclick="insertPtToken('{title}')">{title}</span>
          <span class=tok onclick="insertPtToken('{date}')">{date}</span>
          <span class=tok onclick="insertPtToken('{ext}')">{ext}</span>
        </div>
        <div class=set-pt-preview id=set-pt-preview></div>
        <div class=meta style="margin-top:6px">Root path: edit <code>DOWNLOADS_DIR</code> in docker-compose and restart.</div>
        ${isOwner ? `
        <div style="margin-top:12px;padding-top:10px;border-top:1px dashed var(--separator)">
          <div class=field>🗂 Migrate existing files</div>
          <div class=meta>Re-path files already on disk to match the template above. Preview is read-only; every move is journaled so you can undo it.</div>
          <label class=meta style="display:flex;gap:6px;align-items:center;margin-top:8px;cursor:pointer">
            <input type=checkbox id=rs-unmatched> Include files with no download record (best-effort)
          </label>
          <div class=btn-row style="margin-top:8px">
            <button class=sec onclick="restructurePreview()">🔍 Preview</button>
            <button class="small danger" onclick="restructureRollback()">↩ Undo last</button>
          </div>
          <div id=rs-result style="margin-top:10px"></div>
        </div>` : ''}
      </div>`;

    const oneDriveTile = odHtml ? `<div class=set-tile>${odHtml.replace(/<div class=card>([\s\S]*?)<\/div>$/, '<div class=head>📁 OneDrive</div>$1')}</div>` : '';

    root.innerHTML = `
      <div class=set-tile class=full>
        <div class=head>⚙ General</div>
        ${fields}
        <div class=restart-banner id=restart-banner>
          ⚠ Some settings require a service restart to take effect.
        </div>
        <div class=btn-row style="margin-top:10px">
          <button onclick="saveSettings('set-')">💾 Save changes</button>
        </div>
      </div>

      <div class=set-grid>
        ${oneDriveTile}
        ${downloadsTile}
      </div>
    `;
    updatePathTemplatePreview();
  } catch(e) { showErr('Load failed: '+e); }
}

function updatePathTemplatePreview() {
  const el  = document.getElementById('set-download_path_template');
  const out = document.getElementById('set-pt-preview');
  if (!el || !out) return;
  const tpl = el.value || '{platform}/{uploader}/{title}.{ext}';
  // Client-side preview — must match compile_path_template() in miniapp.py
  // semantics for the same inputs.
  const sample = tpl
    .replaceAll('{service}',  'YTDLP')
    .replaceAll('{platform}', 'twitch')
    .replaceAll('{uploader}', 'somestreamer')
    .replaceAll('{title}',    'Best moments compilation')
    .replaceAll('{date}',     '20260528')
    .replaceAll('{ext}',      'mp4');
  out.textContent = '/downloads/' + sample;
}

function insertPtToken(tok) {
  const el = document.getElementById('set-download_path_template');
  if (!el) return;
  const start = el.selectionStart ?? el.value.length;
  const end   = el.selectionEnd   ?? el.value.length;
  el.value = el.value.slice(0, start) + tok + el.value.slice(end);
  el.focus();
  el.setSelectionRange(start + tok.length, start + tok.length);
  updatePathTemplatePreview();
}

// ── #41 Part 2: file-restructure migration (owner-only) ──────────────────────
let _rsLastPlan = null;

function _rsActClass(a) {
  return a === 'move' ? 'rs-move' : a === 'conflict' ? 'rs-conflict' : 'rs-skip';
}

async function restructurePreview() {
  const out = document.getElementById('rs-result');
  if (!out) return;
  const inc = document.getElementById('rs-unmatched')?.checked ? 'true' : 'false';
  out.innerHTML = '<div class=meta>Scanning library…</div>';
  try {
    const r = await api('/api/miniapp/restructure/preview?include_unmatched=' + inc);
    _rsLastPlan = r;
    const s = r.summary || {};
    const rows = (r.items || []).map(it => `
      <div class="rs-row ${_rsActClass(it.action)}">
        <span class=rs-act>${esc(it.action)}</span>
        <span class=rs-path>
          <span class=rs-src>${esc(it.src)}</span>
          ${it.action === 'move' ? '<span class=rs-arrow>→</span><span class=rs-dst>' + esc(it.dst) + '</span>' : ''}
          ${it.reason ? '<span class=rs-reason>' + esc(it.reason) + '</span>' : ''}
        </span>
      </div>`).join('');
    const canApply = (s.move || 0) > 0;
    out.innerHTML = `
      <div class=rs-summary>
        <b>${s.total || 0}</b> files ·
        <span class=rs-move>${s.move || 0} to move</span> ·
        <span class=rs-skip>${s.noop || 0} already correct</span> ·
        <span class=rs-conflict>${s.conflict || 0} conflicts</span> ·
        <span class=rs-skip>${s.skip || 0} skipped</span>
      </div>
      ${r.truncated ? '<div class=meta>Showing first ' + (r.items || []).length + ' of ' + s.total + '.</div>' : ''}
      <div class=rs-list>${rows || '<div class=meta>Nothing to show.</div>'}</div>
      ${canApply ? '<div class=btn-row style="margin-top:8px"><button class=danger onclick="restructureApply()">📦 Migrate ' + s.move + ' file' + (s.move === 1 ? '' : 's') + '</button></div>'
                 : '<div class=meta style="margin-top:8px">No moves needed — library already matches the template.</div>'}`;
  } catch (e) { out.innerHTML = '<div class="msg err">Preview failed: ' + esc(e) + '</div>'; }
}

function restructureApply() {
  const inc = document.getElementById('rs-unmatched')?.checked;
  const n = _rsLastPlan?.summary?.move || 0;
  const msg = 'Move ' + n + ' file' + (n === 1 ? '' : 's') + ' to match the template?\n\nEvery move is journaled — you can undo it afterwards.';
  const go = async () => {
    const out = document.getElementById('rs-result');
    try {
      const r = await api('/api/miniapp/restructure/apply', {
        method: 'POST', body: JSON.stringify({ include_unmatched: !!inc }),
      });
      if (out) out.innerHTML = '<div class=rs-summary>Migrating…</div><div class=rs-bar><div class=rs-bar-fill id=rs-bar-fill></div></div><div id=rs-prog class=meta></div>';
      streamRestructureProgress(r.job_id);
    } catch (e) { if (out) out.innerHTML = '<div class="msg err">Migrate failed: ' + esc(e) + '</div>'; }
  };
  if (tg?.showConfirm) tg.showConfirm(msg, ok => { if (ok) go(); });
  else if (confirm(msg)) go();
}

async function streamRestructureProgress(jobId) {
  // EventSource cannot send the X-Init-Data header the owner gate needs, so we
  // consume the SSE stream over fetch()+ReadableStream and parse data: frames.
  try {
    const resp = await fetch('/api/miniapp/restructure/progress/' + encodeURIComponent(jobId), {
      headers: { 'X-Init-Data': initData },
    });
    if (!resp.ok || !resp.body) throw new Error('HTTP ' + resp.status);
    const reader = resp.body.getReader();
    const dec = new TextDecoder();
    let buf = '';
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      let idx;
      while ((idx = buf.indexOf('\n\n')) >= 0) {
        const frame = buf.slice(0, idx);
        buf = buf.slice(idx + 2);
        const line = frame.split('\n').find(l => l.startsWith('data:'));
        if (!line) continue;
        try { renderRsProgress(JSON.parse(line.slice(5).trim())); } catch {}
      }
    }
  } catch (e) {
    const out = document.getElementById('rs-prog');
    if (out) out.innerHTML = '<span class="msg err">Progress stream lost: ' + esc(e) + '</span>';
  }
}

function renderRsProgress(st) {
  const fill = document.getElementById('rs-bar-fill');
  const prog = document.getElementById('rs-prog');
  if (st.status === 'unknown') {
    if (prog) prog.innerHTML = '<span class="msg err">Job not found (service may have restarted).</span>';
    return;
  }
  const total = st.total || 0;
  const done = st.done || 0;
  const pct = total ? Math.round((done / total) * 100) : (st.status === 'done' ? 100 : 0);
  if (fill) fill.style.width = pct + '%';
  const errs = Array.isArray(st.errors) ? st.errors.length : 0;
  if (st.status === 'done') {
    if (fill) fill.classList.add('ok');
    if (prog) prog.innerHTML = '✅ Moved <b>' + (st.moved || 0) + '</b> of ' + total +
      (errs ? ' · <span class=rs-conflict>' + errs + ' error' + (errs === 1 ? '' : 's') + '</span>' : '') +
      '. <button class="small" onclick="restructurePreview()">Re-scan</button>';
    showOk('Migration complete — ' + (st.moved || 0) + ' file(s) moved.');
  } else if (st.status === 'error') {
    if (fill) fill.classList.add('err');
    if (prog) prog.innerHTML = '<span class="msg err">Migration failed' + (errs ? ' (' + errs + ' error(s))' : '') + '.</span>';
  } else if (prog) {
    prog.textContent = 'Moving… ' + done + ' / ' + total + (errs ? ' (' + errs + ' error(s))' : '');
  }
}

function restructureRollback() {
  const msg = 'Undo the most recent migration?\n\nFiles will be moved back to their original paths.';
  const go = async () => {
    const out = document.getElementById('rs-result');
    if (out) out.innerHTML = '<div class=meta>Rolling back…</div>';
    try {
      const r = await api('/api/miniapp/restructure/rollback', { method: 'POST' });
      if (!r.ok && r.error) { if (out) out.innerHTML = '<div class="msg err">' + esc(r.error) + '</div>'; return; }
      const errs = Array.isArray(r.errors) ? r.errors.length : 0;
      if (out) out.innerHTML = '<div class=rs-summary>↩ Reversed <b>' + (r.reversed || 0) + '</b> move(s)' +
        (errs ? ' · <span class=rs-conflict>' + errs + ' skipped</span>' : '') + '.</div>';
      showOk('Rolled back ' + (r.reversed || 0) + ' move(s).');
    } catch (e) { if (out) out.innerHTML = '<div class="msg err">Rollback failed: ' + esc(e) + '</div>'; }
  };
  if (tg?.showConfirm) tg.showConfirm(msg, ok => { if (ok) go(); });
  else if (confirm(msg)) go();
}

async function saveSettings(prefix) {
  prefix = prefix || 'set-';
  const cfg = await api('/api/miniapp/config');
  const updates = {};
  for (const s of cfg.settings) {
    const el = document.getElementById(prefix + s.key);
    if (!el) continue;
    let v = el.value;
    if (s.type === 'int') v = parseInt(v, 10);
    else if (s.type === 'bool') v = (v === 'true' || v === true);
    updates[s.key] = v;
  }
  try {
    const j = await api('/api/miniapp/config', { method: 'POST', body: JSON.stringify({updates}) });
    const inAdmin = (prefix === 'adm-');
    if (j.needs_restart && j.needs_restart.length) {
      showOk('Saved · restart required for: ' + j.needs_restart.join(', '));
      if (inAdmin) {
        await loadAdmin();
        const banner = document.getElementById('admin-restart-banner');
        if (banner) {
          banner.classList.add('show');
          banner.textContent = '⚠ Restart required for: ' + j.needs_restart.join(', ');
        }
      } else {
        await loadSettings();
        const banner = document.getElementById('restart-banner');
        if (banner) {
          banner.classList.add('show');
          banner.textContent = '⚠ Restart required for: ' + j.needs_restart.join(', ');
        }
      }
    } else {
      showOk('Saved');
      inAdmin ? loadAdmin() : loadSettings();
    }
  } catch(e) {
    if (Array.isArray(e)) showErr(e.join(' · '));
    else showErr(e.errors ? e.errors.join(' · ') : e);
  }
}

// ── Admin tab ─────────────────────────────────────────────────────────────

let isOwner = false;
let adminBootstrapped = false;

async function bootstrapWhoami() {
  try {
    const j = await api('/api/miniapp/whoami');
    isOwner = !!j.is_owner;
    // Toggle the sidebar entries AND the home tiles together so owner-only
    // surfaces appear in both places at once.
    ['tab-admin', 'tab-scraper', 'nav-files',
     'tile-admin', 'tile-scraper', 'tile-files'].forEach(id => {
      const el = document.getElementById(id);
      if (el) el.classList.toggle('show', isOwner);
    });
  } catch(e) { /* owner-flag is best-effort; admin surfaces stay hidden on failure */ }
}

async function loadAdmin() {
  if (!isOwner) {
    document.getElementById('admin-content').innerHTML =
      '<div class=empty>Server tab is owner-only.</div>';
    return;
  }
  const root = document.getElementById('admin-content');
  root.innerHTML = '<div class=empty><span class=spin></span> Loading…</div>';
  try {
    const [mode, users, groups, sites, od, cfg] = await Promise.all([
      api('/api/miniapp/admin/mode'),
      api('/api/miniapp/admin/users'),
      api('/api/miniapp/admin/groups'),
      api('/api/miniapp/admin/sites'),
      api('/api/miniapp/onedrive/status'),
      api('/api/miniapp/config'),
    ]);

    // 1. Admin-only-mode (kill switch)
    const modeHtml = `
      <div class=card>
        <div class=row>
          <div class=grow>
            <div class=name>🔒 Admin-only session</div>
            <div class=meta>When enabled, only you can use the bot/Mini App. Anyone else gets a "service in admin-only mode" notice.</div>
          </div>
          <label class="switch danger">
            <input type=checkbox id=admin-mode-toggle ${mode.enabled?'checked':''} onchange="setAdminMode(this.checked)">
            <span class=slider></span>
          </label>
        </div>
        <div class=field style="margin-top:10px">Reason (optional, shown to no one — for your records)</div>
        <input id=admin-mode-reason placeholder="e.g. investigating activity 2026-05-16" value="${esc(mode.reason || '')}">
        <div style="margin-top:8px"><button class=sec onclick=saveAdminModeReason()>Save reason</button></div>
      </div>`;

    // 2a. Pending approvals — show the access codes; legitimate path is
    //     "user DM's me the code, I paste it into the input below". The
    //     per-row Approve button is for cases where I already know who's
    //     who and just want to promote them by chat_id.
    const pending = users.items.filter(u => u.status === 'pending');
    const pendingHtml = `
      <div class=card>
        <div class=field>🔓 Pending approvals (${pending.length})</div>
        <div class=meta style="margin-bottom:8px">Paste the 9-digit code a user sent you out-of-band:</div>
        <input id=approve-code-input placeholder="123-456-789" style="font-family:ui-monospace;font-size:16px;letter-spacing:1px">
        <div style="margin-top:8px"><button onclick=approveByCode()>✅ Approve by code</button></div>
        ${pending.length === 0
          ? '<div class=meta style="margin-top:10px">No pending users.</div>'
          : pending.map(u => {
              const handle = u.username ? '@' + u.username : (u.first_name || ('chat ' + u.chat_id));
              const codeStr = u.pending_code || '(no code)';
              const expired = u.pending_expires_at && (new Date(u.pending_expires_at) < new Date());
              return `
              <div class="user-row" style="padding:10px 0;border-top:1px solid var(--separator)">
                <div class=grow>
                  <div class=name>${esc(handle)} <span class=ban-badge style="margin-left:6px;background:rgba(255,149,0,0.18);color:#ff9500">PENDING</span></div>
                  <div class=meta>chat_id ${u.chat_id} · ${u.interaction_count}× · last /start ${timeago(u.last_seen)}</div>
                  <div class=meta style="font-family:ui-monospace;color:${expired?'var(--destructive)':'var(--fg)'}">
                    code ${esc(codeStr)} ${expired ? '· EXPIRED' : ''}
                  </div>
                </div>
                <span style="display:flex;gap:6px">
                  <button onclick="approveUser(${u.chat_id})">Approve</button>
                  <button class="sec" onclick="denyUser(${u.chat_id})">Deny</button>
                </span>
              </div>`;
            }).join('')}
      </div>`;

    // 2b. Existing users (active + revoked)
    const others = users.items.filter(u => u.status !== 'pending');
    const usersHtml = `
      <div class=card>
        <div class=field>👥 Users (${others.length})</div>
        ${others.length === 0
          ? '<div class=meta>No approved users yet.</div>'
          : others.map(u => {
              const revoked = (u.status === 'banned');   // status string kept for backend compat
              const owner = !!u.is_owner;
              const handle = u.username ? '@' + u.username : (u.first_name || ('chat ' + u.chat_id));
              return `
              <div class="user-row" style="padding:10px 0;border-top:1px solid var(--separator)">
                <div class=grow>
                  <div class=name>${esc(handle)}
                    ${owner ? '<span class=owner-badge style="margin-left:6px">OWNER</span>' : ''}
                    ${revoked ? '<span class=ban-badge style="margin-left:6px">REVOKED</span>' : ''}
                  </div>
                  <div class=meta>chat_id ${u.chat_id} · ${u.interaction_count}× · last seen ${timeago(u.last_seen)}</div>
                  ${u.banned_reason ? `<div class=meta>Reason: ${esc(u.banned_reason)}</div>` : ''}
                </div>
                ${owner ? '' : (revoked
                  ? `<button class=sec onclick="unbanUser(${u.chat_id})">Restore</button>`
                  : `<button class="small danger" onclick="banUser(${u.chat_id})">Revoke</button>`)}
              </div>`;
            }).join('')}
      </div>`;

    // 2c. Approved groups — Telegram groups the owner trusts. Members can
    //     use the bot without per-user codes; bot replies are visible to
    //     the whole group.
    const groupsHtml = `
      <div class=card>
        <div class=field>👥 Approved groups (${groups.count})</div>
        <div class=meta style="margin-bottom:8px">Add a group by chat ID (negative number). Send /start in the target group to see its ID.</div>
        <div class=row style="gap:6px">
          <input id=group-chat-id placeholder="-1001234567890" style="flex:1;font-family:ui-monospace">
          <input id=group-label placeholder="Label (e.g. Friends)" style="flex:1">
        </div>
        <div style="margin-top:8px"><button onclick=approveGroup()>+ Approve group</button></div>
        ${groups.items.length === 0
          ? '<div class=meta style="margin-top:10px">No approved groups yet.</div>'
          : groups.items.map(g => `
              <div class="user-row" style="padding:10px 0;border-top:1px solid var(--separator)">
                <div class=grow>
                  <div class=name>${esc(g.label || '(no label)')}</div>
                  <div class=meta>chat_id ${g.chat_id} · added ${timeago(g.approved_at)}</div>
                </div>
                <button class="small danger" onclick="unapproveGroup(${g.chat_id})">Remove</button>
              </div>`).join('')}
      </div>`;

    // 3. Site management — toggles grouped by category for clarity.
    // Order: Adult cam first (so it's at the top with sensitive defaults
    // visible), then Video, Live streaming, Social, Regional, Other.
    const CAT_ORDER = ['Adult cam', 'Live streaming', 'Video', 'Social', 'Regional (CN)', 'Other'];
    const CAT_ICON = {
      'Adult cam':     '🔞',
      'Live streaming':'📡',
      'Video':         '▶',
      'Social':        '📱',
      'Regional (CN)': '🇨🇳',
      'Other':         '🌐',
    };
    const byCat = new Map();
    for (const p of sites.platforms) {
      const c = p.category || 'Other';
      if (!byCat.has(c)) byCat.set(c, []);
      byCat.get(c).push(p);
    }
    const orderedCats = CAT_ORDER.filter(c => byCat.has(c))
      .concat([...byCat.keys()].filter(c => !CAT_ORDER.includes(c)));
    const siteSections = orderedCats.map(cat => {
      const rows = byCat.get(cat).map(p => `
        <div class=site-toggle>
          <div>${esc(p.name)}</div>
          <label class=switch>
            <input type=checkbox ${p.blocked ? '' : 'checked'}
                   onchange="toggleSite('${esc(p.name)}', this.checked)">
            <span class=slider></span>
          </label>
        </div>`).join('');
      return `<div class=wl-group-head>${CAT_ICON[cat] || '🌐'} ${esc(cat)}</div>${rows}`;
    }).join('');
    const sitesHtml = `
      <div class=card>
        <div class=field>🌐 Site allowlist (toggle off to hide from non-owner users)</div>
        ${siteSections || '<div class=meta>No known platforms yet.</div>'}
        <div class=meta style="margin-top:8px">${sites.blocked_count} site${sites.blocked_count===1?'':'s'} currently blocked. Owner is never affected.</div>
      </div>`;

    // 4. Server settings (admin-flagged keys)
    const adminFields = cfg.settings.filter(s => s.admin)
      .map(s => _renderSettingField(s, cfg.values[s.key], 'adm-')).join('');
    const settingsHtml = `
      <div class=card>
        <div class=field>⚙ Server settings (owner-only)</div>
      </div>
      ${adminFields}
      <div class=restart-banner id=admin-restart-banner>
        ⚠ Some settings require a service restart to take effect.
      </div>
      <div class=btn-row>
        <button onclick="saveSettings('adm-')">💾 Save changes</button>
        <button class=warn onclick=restartService()>♻ Restart service</button>
      </div>`;

    // 5. OneDrive (admin-only) — real connect flow.
    let odBody;
    if (od.device_flow) {
      // Mid-authorization: show the code + URL prominently. Background poll
      // hits ONLY /onedrive/status (not the 6-endpoint loadAdmin sweep) and
      // only triggers a full re-render when status actually flips.
      odBody = `
        <div class=name>⏳ Awaiting authorization</div>
        <div style="margin-top:10px;padding:10px;background:var(--bg);border-radius:8px">
          <div class=meta>Open this URL on any device:</div>
          <div style="margin:6px 0;font-size:14px;word-break:break-all">
            <a href="${esc(od.device_flow.verification_uri)}" target=_blank>${esc(od.device_flow.verification_uri)}</a>
          </div>
          <div class=meta>Enter this code:</div>
          <div style="font-family:ui-monospace;font-size:22px;letter-spacing:3px;font-weight:700;margin-top:4px">
            ${esc(od.device_flow.user_code)}
          </div>
          <div class=meta style="margin-top:6px">Expires in <span id=od-expires>${od.device_flow.expires_in}</span>s. Page will update automatically.</div>
        </div>`;
      if (!window._odPoll) {
        window._odPoll = setInterval(_pollOneDriveDuringConnect, 3000);
        setTimeout(() => { if (window._odPoll) { clearInterval(window._odPoll); window._odPoll = null; } }, 12*60*1000);
      }
    } else if (od.configured) {
      if (window._odPoll) { clearInterval(window._odPoll); window._odPoll = null; }
      const q = od.quota;
      odBody = `
        <div class=name>✅ Connected · ${esc(od.account || od.display_name || '?')}</div>
        ${q ? `<div class=meta style="margin-top:4px">${q.free_gb} GB free of ${q.total_gb} GB · ${q.used_gb} GB used ${q.state ? '· ' + esc(q.state) : ''}</div>` : ''}
        <div class=meta>app …${esc(od.client_id_tail)} ${od.token_valid ? '· token healthy' : '· ⚠ refresh failed'}</div>
        <div class=btn-row style="margin-top:8px">
          <button class=sec onclick=testOneDrive()>🧪 Test upload</button>
          <button class="small danger" onclick=disconnectOneDrive()>Disconnect</button>
        </div>`;
    } else {
      if (window._odPoll) { clearInterval(window._odPoll); window._odPoll = null; }
      odBody = `
        <div class=name>⚪ Not connected</div>
        <div class=meta style="margin-top:4px">Azure app …${esc(od.client_id_tail)} · Files.ReadWrite scope</div>
        ${od.last_error ? `<div class=meta style="color:var(--destructive);margin-top:4px">Last error: ${esc(od.last_error)}</div>` : ''}
        <div style="margin-top:8px"><button onclick=connectOneDrive()>🔗 Connect OneDrive</button></div>
        <div class=meta style="margin-top:6px">You'll get a 6-character code to type at microsoft.com/devicelogin.</div>`;
    }
    const odHtml = `
      <div class=card>
        <div class=field>📁 OneDrive integration</div>
        ${odBody}
      </div>`;

    // 6. Bot-token security card (rotation drift detector)
    let securityHtml = '';
    try {
      const sec = await api('/api/miniapp/admin/security');
      const badge =
        sec.status === 'in_sync' ? '<span style="color:var(--success)">✓ in sync</span>'
        : sec.status === 'drift' ? '<span style="color:var(--destructive)">⚠ DRIFT — env token does not match pinned hash</span>'
        : '<span style="color:#ff9500">⚪ unpinned — pin the current token to enable drift detection</span>';
      securityHtml = `
      <div class=card>
        <div class=field>🔐 Bot token health</div>
        <div class=name>${badge}</div>
        <div class=meta style="margin-top:4px">
          live token …${esc(sec.live_hash || '(unset)')}
          ${sec.pinned_hash ? '· pinned …' + esc(sec.pinned_hash) : ''}
          ${sec.pinned_at ? '· pinned ' + timeago(sec.pinned_at) : ''}
        </div>
        <div style="margin-top:8px"><button class=sec onclick=pinToken()>📌 Pin current token</button></div>
        <details style="margin-top:10px;font-size:12px;color:var(--muted)">
          <summary style="cursor:pointer">Rotation procedure</summary>
          <ol style="padding-left:20px;margin-top:6px;line-height:1.5">
            <li>BotFather → /mybots → SM-DL → API Token → Revoke current token</li>
            <li>Copy the new token</li>
            <li>Update SMDL_BOT_TOKEN in WCM + .env.local (run sync_env_from_wcm.ps1)</li>
            <li><code>docker compose restart smdl</code></li>
            <li>Return here, hit "Pin current token"</li>
          </ol>
        </details>
      </div>`;
    } catch(_e) { /* security card best-effort */ }

    // 7. Live-recording repair — count of *.mp4.part files + one-click button.
    //    Repair runs in a background task on the server; the page doesn't
    //    block. User re-opens the Admin tab to see the count shrink.
    let repairHtml = '';
    try {
      const rep = await api('/api/miniapp/admin/recordings/pending');
      const cnt = rep.count || 0;
      const bytesGb = (rep.total_bytes || 0) / 1024 / 1024 / 1024;
      const badgeColor = cnt === 0 ? 'rgba(52,199,89,0.18);color:var(--success)'
                       : cnt >= 5  ? 'rgba(255,69,58,0.18);color:var(--destructive)'
                       :             'rgba(255,204,0,0.18);color:#ffcc00';
      const subline = cnt === 0
        ? 'No interrupted recordings.'
        : `${bytesGb.toFixed(2)} GB total · ffmpeg remux ~30-60s per GB`;
      repairHtml = `
      <div class=card>
        <div class=row>
          <div class=grow>
            <div class=name>🎞 Live-recording repair
              <span style="margin-left:6px;padding:1px 6px;border-radius:8px;font-size:11px;font-weight:600;background:${badgeColor}">${cnt} pending</span>
            </div>
            <div class=meta>${esc(subline)}</div>
          </div>
          ${cnt > 0
            ? '<button onclick=repairRecordings()>🔧 Repair</button>'
            : '<button class=sec disabled style="opacity:0.5">✓ Done</button>'}
        </div>
        <details style="margin-top:10px;font-size:12px;color:var(--muted)">
          <summary style="cursor:pointer">What this does</summary>
          <div style="margin-top:6px;line-height:1.5">
            Container restarts during a live recording leave behind <code>.mp4.part</code>
            files that players refuse to open. This re-muxes them with ffmpeg
            stream-copy (no quality loss, no re-encode) and writes a proper
            moov atom. Originals are moved to
            <code>_repaired_originals/</code> in the same folder.
          </div>
        </details>
      </div>`;
      // Color the badge — reuse the .ok/.warn/.due pattern from elsewhere.
      // Inline styles since the Admin tab CSS lives in home._layout.
    } catch(_e) {
      console.warn('repair card failed:', _e);
    }

    // Sub-tab layout. Each pill swaps which pane is visible without
    // re-fetching the data. State (current pill) is preserved across
    // loadAdmin() refreshes via the `_adminActiveSubtab` module global.
    const lockdownBanner = mode.enabled
      ? `<div class=lockdown-banner>🔒 Admin-only session is ACTIVE${mode.reason ? `<div class=reason>${esc(mode.reason)}</div>` : ''}</div>`
      : '';
    const subtabsNav = `
      <div class=subtabs>
        <button class=subtab data-pane="approval"    onclick="adminGoto('approval')">📋 Approval list</button>
        <button class=subtab data-pane="permissions" onclick="adminGoto('permissions')">🌐 Permissions</button>
        <button class=subtab data-pane="tools"       onclick="adminGoto('tools')">🛠 Admin tools</button>
        <button class=subtab data-pane="server"      onclick="adminGoto('server')">⚙ Server</button>
      </div>`;

    root.innerHTML =
      lockdownBanner
      + subtabsNav
      + `<div class=subtab-pane id=subpane-approval>${pendingHtml + usersHtml + groupsHtml}</div>`
      + `<div class=subtab-pane id=subpane-permissions>${sitesHtml}</div>`
      + `<div class=subtab-pane id=subpane-tools>${modeHtml + repairHtml}</div>`
      + `<div class=subtab-pane id=subpane-server>${securityHtml + settingsHtml}</div>`;

    // Restore the active sub-tab (default: approval).
    adminGoto(_adminActiveSubtab || 'approval');
  } catch(e) { showErr('Load failed: ' + e); }
}

let _adminActiveSubtab = 'approval';
function adminGoto(pane) {
  _adminActiveSubtab = pane;
  document.querySelectorAll('.subtab').forEach(b =>
    b.classList.toggle('active', b.dataset.pane === pane));
  document.querySelectorAll('.subtab-pane').forEach(p =>
    p.classList.toggle('active', p.id === 'subpane-' + pane));
}

async function repairRecordings() {
  const proceed = await new Promise(res => {
    if (tg?.showConfirm) tg.showConfirm('Start ffmpeg remux on all pending .mp4.part files? Big files (1+ GB) can take 10+ minutes — runs in the background, refresh to check progress.', ok => res(!!ok));
    else res(confirm('Start repair?'));
  });
  if (!proceed) return;
  try {
    const r = await api('/api/miniapp/admin/recordings/repair', { method: 'POST', body: '{}' });
    if (r.started) {
      showOk(r.msg || `Queued ${r.queued} file(s).`);
    } else {
      showOk(r.msg || 'Nothing pending.');
    }
    // Refresh the count after a short delay (it'll still be N until the
    // first file finishes, but visually confirms the click registered).
    setTimeout(loadAdmin, 800);
  } catch(e) { showErr(e); }
}

// ── Profile scraper page (own tab, owner-only) ─────────────────────────────

async function loadScraper() {
  if (!isOwner) {
    document.getElementById('scraper-content').innerHTML =
      '<div class=empty>Scraper is owner-only.</div>';
    return;
  }
  const root = document.getElementById('scraper-content');
  root.innerHTML = '<div class=empty><span class=spin></span> Loading…</div>';
  try {
    const sc = await api('/api/miniapp/admin/scraper');
    const pausedBadge = sc.runtime_paused
      ? '<span style="color:#ff9500">⏸ paused</span>'
      : '<span style="color:var(--success)">▶ running</span>';
    const profByPlat = {instagram: [], tiktok: []};
    for (const p of (sc.profiles || [])) {
      const plat = (p.platform || 'other').toLowerCase();
      (profByPlat[plat] || (profByPlat[plat] = [])).push(p);
    }
    const renderProfile = (p) => {
      // Compact single-line row (#33). URL hidden in title= tooltip; error
      // in title= on the warning chip. Counters become pills.
      const enabled = !!p.enabled;
      const dotColor = enabled ? (p.failure_count > 0 ? '#ff9500' : 'var(--success)') : 'var(--muted)';
      const uname = p.username || p.label || p.url;
      let dueChip = '';
      if (p.next_probe_at) {
        const mins = Math.floor((new Date(p.next_probe_at).getTime() - Date.now()) / 60000);
        let label;
        if (mins < 0)         label = 'due now';
        else if (mins < 60)   label = `${mins}m`;
        else if (mins < 1440) label = `${Math.floor(mins/60)}h`;
        else                  label = `${Math.floor(mins/1440)}d`;
        dueChip = `<span class="chip due" title="Next probe">${label}</span>`;
      }
      const pulledChip = `<span class="chip" title="Posts pulled so far">${p.downloaded_count || 0}↓</span>`;
      const failChip = p.failure_count > 0
        ? `<span class="chip err" title="${esc(String(p.last_error || '').slice(0, 220))}">⚠${p.failure_count}</span>`
        : '';
      const u = JSON.stringify(p.url);
      const pauseBtn = enabled
        ? `<button class="icon-btn" title="Pause" onclick='scraperPause(${u})'>⏸</button>`
        : `<button class="icon-btn primary" title="Resume" onclick='scraperResume(${u})'>▶</button>`;
      return `
      <div class="scraper-row" title="${esc(p.url)}">
        <span class="dot" style="background:${dotColor};margin-right:0"></span>
        <span class="uname">@${esc(uname)}</span>
        <span class="chips">${pulledChip}${dueChip}${failChip}</span>
        <span class="actions">
          <button class="icon-btn" title="Probe now" onclick='scraperProbeNow(${u})'>🔄</button>
          <button class="icon-btn" title="Backfill from oldest" onclick='scraperBackfill(${u})'>📦</button>
          ${pauseBtn}
          <button class="icon-btn danger" title="Remove" onclick='scraperRemove(${u})'>🗑</button>
        </span>
      </div>`;
    };
    const igRows  = (profByPlat.instagram || []).map(renderProfile).join('');
    const ttRows  = (profByPlat.tiktok    || []).map(renderProfile).join('');
    const totalProfiles = (sc.profiles || []).length;
    const cookieHtml = (sc.cookies || []).map(c => {
      const label = c.key.charAt(0).toUpperCase() + c.key.slice(1);
      let status, cls;
      if (!c.file_exists) {
        status = `⚠ missing /cookies/${c.key}.txt`; cls = 'var(--destructive)';
      } else if (c.cooldown_seconds && c.cooldown_seconds > 0) {
        const h = Math.ceil(c.cooldown_seconds / 3600);
        status = `⏸ cooldown ${h}h (${c.consecutive_blocks} block${c.consecutive_blocks===1?'':'s'})`;
        cls = 'var(--destructive)';
      } else {
        let warmup = '';
        if (c.first_seen_at) {
          const days = Math.floor((Date.now() - new Date(c.first_seen_at).getTime()) / 86400000);
          warmup = days < 7 ? ` · warmup day ${days+1}/7` : '';
        }
        status = `✓ ${c.probes_today} probes today${warmup}`;
        cls = 'var(--success)';
      }
      return `
      <div class=row style="padding:6px 0;border-top:1px solid var(--separator)">
        <div class=grow>
          <div class=name>${label}</div>
          <div class=meta style="color:${cls}">${status}</div>
        </div>
        <div class=meta>${c.file_age_days != null ? c.file_age_days + 'd old' : ''}</div>
      </div>`;
    }).join('');
    root.innerHTML = `
      <div class=card>
        <div class=row>
          <div class=grow>
            <div class=name>Status</div>
            <div class=meta>${pausedBadge} · ${totalProfiles} profile${totalProfiles===1?'':'s'} · ${sc.daily_sessions} sessions/day · ${esc(sc.active_hours)} ${esc(sc.timezone)}</div>
          </div>
          <label class=switch>
            <input type=checkbox id=scraper-pause-toggle ${sc.runtime_paused?'':'checked'} onchange="setScraperPaused(!this.checked)">
            <span class=slider></span>
          </label>
        </div>
      </div>

      <div class=card>
        <div class=field>Add Instagram or TikTok profile</div>
        <input id=scraper-add-url placeholder="https://www.instagram.com/someuser  or  https://www.tiktok.com/@someuser">
        <input id=scraper-add-label placeholder="Label (optional)" style="margin-top:6px">
        <div style="margin-top:8px"><button onclick=scraperAdd()>+ Add to scraper</button></div>
      </div>

      ${totalProfiles === 0
        ? '<div class=card><div class=meta>No profiles yet. First probe records a baseline; subsequent probes auto-download new posts.</div></div>'
        : ''}
      ${igRows ? `<div class=card><div class=wl-group-head>📸 Instagram (${(profByPlat.instagram||[]).length})</div>${igRows}</div>` : ''}
      ${ttRows ? `<div class=card><div class=wl-group-head>🎵 TikTok (${(profByPlat.tiktok||[]).length})</div>${ttRows}</div>` : ''}

      <div class=card>
        <div class=field>Cookies</div>
        ${cookieHtml || '<div class=meta>No cookies seen yet.</div>'}
        <div class=meta style="margin-top:8px">Drop fresh files in <code>/cookies/&lt;platform&gt;.txt</code> (host: <code>G:\\YT-DLP\\cookies\\</code>). Re-export every 1-2 weeks for IG, 30+ days for TikTok.</div>
      </div>`;
  } catch(e) {
    if (String(e).includes('owner')) {
      root.innerHTML = '<div class=empty>Scraper is owner-only.</div>';
    } else {
      showErr('Load failed: ' + e);
    }
  }
}

// ── Profile scraper actions ────────────────────────────────────────────────

async function setScraperPaused(paused) {
  try {
    await api('/api/miniapp/admin/scraper/toggle', {
      method: 'POST', body: JSON.stringify({paused}),
    });
    showOk(paused ? '⏸ Scraper paused' : '▶ Scraper running');
    loadScraper();
  } catch(e) { showErr(e); loadScraper(); }
}

async function scraperAdd() {
  const url = (document.getElementById('scraper-add-url')?.value || '').trim();
  const label = (document.getElementById('scraper-add-label')?.value || '').trim() || null;
  if (!url) { showErr('URL required'); return; }
  try {
    const r = await api('/api/miniapp/admin/scraper/add', {
      method: 'POST', body: JSON.stringify({url, label}),
    });
    showOk(r.msg || 'Added');
    document.getElementById('scraper-add-url').value = '';
    document.getElementById('scraper-add-label').value = '';
    loadScraper();
  } catch(e) { showErr(e); }
}

async function scraperRemove(url) {
  const proceed = await new Promise(res => {
    if (tg?.showConfirm) tg.showConfirm('Stop scraping this profile? Past downloads stay.', ok => res(!!ok));
    else res(confirm('Stop scraping this profile?'));
  });
  if (!proceed) return;
  try {
    await api('/api/miniapp/admin/scraper/remove', {
      method: 'POST', body: JSON.stringify({url}),
    });
    showOk('Removed');
    loadScraper();
  } catch(e) { showErr(e); }
}

async function scraperPause(url) {
  try {
    await api('/api/miniapp/admin/scraper/pause', {
      method: 'POST', body: JSON.stringify({url}),
    });
    showOk('Paused');
    loadScraper();
  } catch(e) { showErr(e); }
}

async function scraperResume(url) {
  try {
    await api('/api/miniapp/admin/scraper/resume', {
      method: 'POST', body: JSON.stringify({url}),
    });
    showOk('Resumed (failure count reset)');
    loadScraper();
  } catch(e) { showErr(e); }
}

async function scraperProbeNow(url) {
  try {
    showOk('⏳ Probing…');
    const r = await api('/api/miniapp/admin/scraper/probe', {
      method: 'POST', body: JSON.stringify({url}),
    });
    showOk(r.msg || 'Probed');
    loadScraper();
  } catch(e) { showErr(e); }
}

async function scraperBackfill(url) {
  // Confirm — backfill can pull hundreds of items and take a while.
  if (!confirm('Backfill the entire profile history? This runs gallery-dl in the background and can take several minutes for large profiles.')) return;
  try {
    showOk('📦 Starting backfill…');
    const r = await api('/api/miniapp/admin/scraper/backfill', {
      method: 'POST', body: JSON.stringify({url}),
    });
    showOk(r.msg || 'Backfill started');
  } catch(e) { showErr(e); }
}

async function approveUser(chat_id) {
  try {
    await api('/api/miniapp/admin/users/approve', {
      method: 'POST', body: JSON.stringify({chat_id}),
    });
    showOk('Approved');
    loadAdmin();
  } catch(e) { showErr(e); }
}

async function approveByCode() {
  const input = document.getElementById('approve-code-input');
  const code = (input?.value || '').trim();
  if (!code) { showErr('Paste the 9-digit code'); return; }
  try {
    const r = await api('/api/miniapp/admin/users/approve_by_code', {
      method: 'POST', body: JSON.stringify({code}),
    });
    const who = r.username ? '@' + r.username : (r.first_name || ('chat ' + r.chat_id));
    showOk('Approved ' + who);
    if (input) input.value = '';
    loadAdmin();
  } catch(e) { showErr(e); }
}

async function approveGroup() {
  const idEl = document.getElementById('group-chat-id');
  const labelEl = document.getElementById('group-label');
  const chat_id = parseInt((idEl?.value || '').trim(), 10);
  const label = (labelEl?.value || '').trim() || null;
  if (!chat_id || chat_id >= 0) {
    showErr('Group chat IDs are negative numbers (e.g. -1001234567890)');
    return;
  }
  try {
    await api('/api/miniapp/admin/groups/approve', {
      method: 'POST', body: JSON.stringify({chat_id, label}),
    });
    showOk('Group approved');
    if (idEl) idEl.value = '';
    if (labelEl) labelEl.value = '';
    loadAdmin();
  } catch(e) { showErr(e); }
}

async function unapproveGroup(chat_id) {
  const proceed = await new Promise(res => {
    if (tg?.showConfirm) tg.showConfirm('Revoke this group’s access?', ok => res(!!ok));
    else res(confirm('Revoke this group’s access?'));
  });
  if (!proceed) return;
  try {
    await api('/api/miniapp/admin/groups/unapprove', {
      method: 'POST', body: JSON.stringify({chat_id}),
    });
    showOk('Group revoked');
    loadAdmin();
  } catch(e) { showErr(e); }
}

// OneDrive lives in the Settings tab now (moved from Admin). All callbacks
// refresh loadSettings() instead of loadAdmin().
async function connectOneDrive() {
  try {
    const r = await api('/api/miniapp/onedrive/connect', { method: 'POST', body: '{}' });
    showOk('Code issued: ' + r.user_code);
    loadSettings();  // surface the code in the OneDrive card
  } catch(e) { showErr(e); }
}

// Lightweight poll during the device-flow window. Hits only /onedrive/status
// (no full loadSettings sweep), tweaks the countdown in place, and triggers
// a full re-render only when the state actually transitions (configured /
// error) — so the page stops feeling like it's reloading.
async function _pollOneDriveDuringConnect() {
  try {
    const s = await api('/api/miniapp/onedrive/status');
    if (s.configured || s.last_error) {
      // Transition — re-render so success / error UI appears.
      if (window._odPoll) { clearInterval(window._odPoll); window._odPoll = null; }
      loadSettings();
      if (s.configured) showOk('OneDrive connected');
      else if (s.last_error) showErr('OneDrive: ' + s.last_error);
      return;
    }
    const exp = document.getElementById('od-expires');
    if (exp && s.device_flow && s.device_flow.expires_in != null) {
      exp.textContent = s.device_flow.expires_in;
    }
  } catch(e) {
    console.warn('OneDrive poll:', e);
  }
}

async function disconnectOneDrive() {
  const proceed = await new Promise(res => {
    if (tg?.showConfirm) tg.showConfirm('Disconnect OneDrive? The refresh token will be wiped — reconnect later if needed.', ok => res(!!ok));
    else res(confirm('Disconnect OneDrive?'));
  });
  if (!proceed) return;
  try {
    await api('/api/miniapp/onedrive/disconnect', { method: 'POST', body: '{}' });
    showOk('Disconnected');
    loadSettings();
  } catch(e) { showErr(e); }
}

async function testOneDrive() {
  try {
    const r = await api('/api/miniapp/onedrive/test_upload', { method: 'POST', body: '{}' });
    showOk('Uploaded ' + (r.name || 'healthcheck'));
  } catch(e) { showErr(e); }
}

async function pinToken() {
  const proceed = await new Promise(res => {
    if (tg?.showConfirm) tg.showConfirm('Pin the current SMDL_BOT_TOKEN hash? Do this only after a deliberate rotation.', ok => res(!!ok));
    else res(confirm('Pin the current SMDL_BOT_TOKEN hash?'));
  });
  if (!proceed) return;
  try {
    await api('/api/miniapp/admin/security/pin', { method: 'POST', body: '{}' });
    showOk('Token hash pinned');
    loadAdmin();
  } catch(e) { showErr(e); }
}

async function setAdminMode(enabled) {
  const reasonEl = document.getElementById('admin-mode-reason');
  const reason = reasonEl ? reasonEl.value.trim() : '';
  try {
    await api('/api/miniapp/admin/mode', {
      method: 'POST', body: JSON.stringify({enabled, reason}),
    });
    showOk(enabled ? '🔒 Admin-only mode ON' : '🔓 Admin-only mode OFF');
    loadAdmin();
  } catch(e) { showErr(e); loadAdmin(); }
}

async function saveAdminModeReason() {
  const enabledEl = document.getElementById('admin-mode-toggle');
  const enabled = !!(enabledEl && enabledEl.checked);
  const reason = document.getElementById('admin-mode-reason').value.trim();
  try {
    await api('/api/miniapp/admin/mode', {
      method: 'POST', body: JSON.stringify({enabled, reason}),
    });
    showOk('Reason saved');
  } catch(e) { showErr(e); }
}

async function banUser(chat_id) {
  if (!confirm("Revoke this user's access? They will lose all SMDL access.")) return;
  const reason = prompt('Reason (optional, internal):') || '';
  try {
    await api('/api/miniapp/admin/users/ban', {
      method: 'POST', body: JSON.stringify({chat_id, reason}),
    });
    showOk('Revoked');
    loadAdmin();
  } catch(e) { showErr(e); }
}

async function unbanUser(chat_id) {
  try {
    await api('/api/miniapp/admin/users/unban', {
      method: 'POST', body: JSON.stringify({chat_id}),
    });
    showOk('Restored');
    loadAdmin();
  } catch(e) { showErr(e); }
}

async function denyUser(chat_id) {
  if (!confirm('Deny this pending request? The user can re-request later.')) return;
  const reason = prompt('Reason (optional, sent to user):') || '';
  try {
    await api('/api/miniapp/admin/users/deny', {
      method: 'POST', body: JSON.stringify({chat_id, reason}),
    });
    showOk('Denied');
    loadAdmin();
  } catch(e) { showErr(e); }
}

async function toggleSite(name, enabled) {
  try {
    // Read current state, flip this one, persist.
    const sites = await api('/api/miniapp/admin/sites');
    const next = new Set(sites.platforms.filter(p => p.blocked).map(p => p.name));
    if (enabled) next.delete(name); else next.add(name);
    await api('/api/miniapp/admin/sites', {
      method: 'POST', body: JSON.stringify({blocked: [...next]}),
    });
    showOk(enabled ? name + ' enabled' : name + ' blocked');
  } catch(e) { showErr(e); loadAdmin(); }
}

async function restartService() {
  const proceed = await new Promise(res => {
    if (tg?.showConfirm) tg.showConfirm('Restart the SM-DL service now? Active recordings will be interrupted.', ok => res(!!ok));
    else res(confirm('Restart the SM-DL service now? Active recordings will be interrupted.'));
  });
  if (!proceed) return;
  try {
    await api('/api/miniapp/restart', { method: 'POST', body: '{}' });
    showOk('Restart scheduled · service will be back in ~5s');
  } catch(e) { showErr(e); }
}

// Surface the Admin tab if we're owner. Best-effort — failures stay silent.
bootstrapWhoami();
// Apply the saved home-tile order + wire up drag-to-reorder (#41).
initTileReorder();
// Boot navigation: land on Home. Earlier this was hardcoded to
// 'watchlist' from when SMDL was primarily a stream-watcher; with the
// Theater + IPTV + Files + Scraper modules in place, Home (the tile
// grid) is the right entry point.
goto('home');
</script>
</body></html>"""


@router.get("/")
async def miniapp_root_redirect():
    """Bare-domain landing → Mini App home. The SMDL TWA points at this
    URL on install; without this redirect the user sees a FastAPI 404.

    Hash-strip is intentional: TG-WebApp initData arrives on /app's hash,
    not the / hash. The browser handles forwarding the hash through 302
    redirects natively, so we just emit the path."""
    return RedirectResponse(url="/app", status_code=302)


@router.get("/app", response_class=HTMLResponse)
async def miniapp_index():
    return HTMLResponse(HTML)


@router.get("/app/", response_class=HTMLResponse)
async def miniapp_index_slash():
    return HTMLResponse(HTML)


@router.get("/app/stremio/assets/{filename:path}")
async def miniapp_stremio_asset(filename: str):
    """Serve the Svelte bundle's hashed assets (CSS, JS, sourcemaps,
    chunk JS). Path-traversal guarded — only filenames inside the
    static/stremio/assets directory are served."""
    from fastapi.responses import FileResponse
    base = os.path.abspath(os.path.join(os.path.dirname(__file__),
                                         "..", "static", "stremio", "assets"))
    target = os.path.abspath(os.path.join(base, filename))
    if not target.startswith(base + os.sep) or not os.path.isfile(target):
        raise HTTPException(404, "asset not found")
    media = "application/javascript" if target.endswith(".js") \
        else "text/css"  if target.endswith(".css") \
        else "application/json" if target.endswith(".map") \
        else "application/octet-stream"
    return FileResponse(target, media_type=media)


@router.get("/app/stremio", response_class=HTMLResponse)
@router.get("/app/stremio/", response_class=HTMLResponse)
async def miniapp_stremio():
    """Sentinel Media — Stremio sub-app shell.

    Serves the Svelte 5 + shadcn-svelte single-page bundle from
    /static/stremio/index.html. The Svelte app drives:
      • Search box (Cinemeta)
      • Poster grid → detail view
      • Stream picker (Torrentio/Comet/MediaFusion)
      • Grab button → RD resolve → playback / cache to G:\

    Auth handoff: Telegram WebApp.initData arrives in the URL hash on
    the first load (TG mini app convention). The Svelte app reads it
    and includes it as `X-Telegram-Init-Data` on every /api/miniapp/*
    call (same pattern as the main /app HTML)."""
    path = os.path.join(os.path.dirname(__file__), "..", "static", "stremio", "index.html")
    path = os.path.abspath(path)
    try:
        with open(path, "r", encoding="utf-8") as f:
            return HTMLResponse(f.read())
    except FileNotFoundError:
        # Pre-build placeholder so the route works even before the Svelte
        # bundle has been built. Shows a friendly "still building" stub.
        return HTMLResponse(
            """<!doctype html><meta charset=utf-8>
<title>Sentinel Media · Theater</title>
<style>body{font:15px system-ui;background:#0c0c0e;color:#e8e8ea;
text-align:center;padding:50px 22px;line-height:1.6}
a{color:#5b9dff;text-decoration:none}
code{background:#1c1c1e;padding:2px 6px;border-radius:4px;font-size:13px}</style>
<h2>🎬 Sentinel Media · Theater</h2>
<p>The Svelte bundle isn't built yet.</p>
<p>Run from <code>sentinel-smdl/stremio-ui/</code>:</p>
<p><code>pnpm install &amp;&amp; pnpm build</code></p>
<p>then reload this page.</p>
<p style=margin-top:30px><a href="/app">← back to Sentinel Media</a></p>"""
        )


def _set_apk_cookie(resp, request: Request):
    host = (request.url.hostname or "").lower()
    domain = COOKIE_DOMAIN if host.endswith("az-sentinel.xyz") else None
    resp.set_cookie(
        key=COOKIE_NAME,
        value=_issue_apk_cookie(),
        max_age=COOKIE_TTL_SEC,
        domain=domain,
        path="/",
        secure=domain is not None,
        httponly=True,
        samesite="lax",
    )


def _safe_token_eq(a: str, b: str) -> bool:
    """Constant-time token comparison tolerant of pasted unicode (mobile
    keyboards). See sentinel-vpn-dashboard/app.py for the rationale."""
    if not a or not b:
        return False
    try:
        a_clean = "".join(ch for ch in a if 32 <= ord(ch) < 127)
        return hmac.compare_digest(a_clean.encode("utf-8"), b.encode("utf-8"))
    except Exception:
        return False


@router.post("/auth/setup")
async def auth_setup(request: Request):
    """Validate owner token → set domain-wide session cookie → 303 to next.
    Twin of the same endpoint on the Suite launcher and Sentinel AI; with a
    domain-wide cookie, one setup hop authorises all four tiles."""
    try:
        form = await request.form()
    except Exception:
        return JSONResponse({"error": "bad_request"}, status_code=400)
    token = (form.get("token") or "").strip()
    nxt   = (form.get("next") or "/app").strip()
    if not nxt.startswith("/"):
        nxt = "/app"
    if not _safe_token_eq(token, OWNER_AUTH_TOKEN):
        return JSONResponse({"error": "invalid_token"}, status_code=401)
    resp = RedirectResponse(url=nxt, status_code=303)
    _set_apk_cookie(resp, request)
    return resp


@router.get("/auth/check")
async def auth_check(request: Request):
    """Lightweight cookie probe — returns whether the APK session is recognised."""
    return {"authenticated": _verify_apk_cookie(request.cookies.get(COOKIE_NAME, ""))}
