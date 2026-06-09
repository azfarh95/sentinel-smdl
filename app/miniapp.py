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
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from pydantic import BaseModel

from . import config as _cfg
from . import database as _db
from . import stream_monitor
from . import auth as _auth
from . import edition as _edition
from . import profile as _profile
from .database import DB_PATH
from .live_downloader import (
    _PLATFORM_LABELS,    # we read but don't mutate
)
from .recorder_bridge import bridge

CONFIG_FILE = os.environ.get("CONFIG_FILE", "/config/smdl.json")
DOWNLOADS_DIR = os.environ.get("DOWNLOADS_DIR", "/downloads")

# ── Branding (#74): owner-uploadable app logo ───────────────────────────────
# Stored under /data (bind-mounted, survives container restarts). The file is
# served by a PUBLIC GET so the WebView <img> can load it without an
# X-Init-Data header (same reasoning as cached-file serving). Upload + delete
# are owner-only. SVG is intentionally excluded (XSS via inline <script>).
from pathlib import Path as _Path
_BRANDING_DIR = _Path(DB_PATH).parent / "branding"
_LOGO_STEM = "app_logo"
_LOGO_MIME_EXT = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/webp": "webp",
    "image/gif": "gif",
}
_LOGO_MAX_BYTES = 2 * 1024 * 1024  # 2 MB


def _logo_file() -> "_Path | None":
    """The on-disk logo path if one exists, else None."""
    try:
        for ext in ("png", "jpg", "webp", "gif"):
            p = _BRANDING_DIR / f"{_LOGO_STEM}.{ext}"
            if p.exists():
                return p
    except Exception:
        pass
    return None

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


# Surface scopes a non-owner Telegram user gets on the community build. These
# are the user-facing pillars (browse/play/download/stickers/streamtracker);
# they deliberately EXCLUDE the owner-sensitive scopes ("*", "smdl.admin",
# "smdl.license") so owner-only routes still 403. Paid features *within* these
# surfaces are gated separately by the entitlement rail (402), not by scope.
COMMUNITY_USER_SCOPES = (
    "smdl.iptv",
    "smdl.downloader",
    "smdl.stickers",
    "smdl.streamtracker",
)


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


def _payload_from_cookie(session: dict | None = None) -> dict:
    """Synthesise the FastAPI-route payload from a parsed session cookie,
    resolving to the cookie's REAL identity. A genuine OWNER session (v1
    cookie, user_id 'owner', or wildcard '*' scope) maps to OWNER_CHAT_ID; a
    scoped v2 cookie maps to its OWN telegram id. A scoped cookie must NEVER be
    silently promoted to the owner — that leaked owner data (sticker packs,
    downloads, …) to any holder of a valid cookie. Embeds the parsed session at
    payload['session'] so per-route require_scope() can read it."""
    sess = session or {}
    uid_raw = str(sess.get("user_id") or "")
    is_owner_session = (
        sess.get("version") == "v1"
        or uid_raw == "owner"
        or "*" in (sess.get("scopes") or [])
    )
    user_id: int | None = None
    if is_owner_session:
        owner = _cfg_get("owner_chat_id")
        if owner is None:
            owner = os.environ.get("OWNER_CHAT_ID", "")
        if owner:
            user_id = int(owner)
    else:
        # Non-owner scoped cookie → its own telegram id (tg:<n> or numeric).
        s = uid_raw.split(":", 1)[1] if uid_raw.startswith("tg:") else uid_raw
        if s.isdigit():
            user_id = int(s)
        # google:<sub> / beta-slug carry no telegram id → leave None so the
        # caller falls through rather than impersonating anyone.
    out: dict = {"user": {"id": user_id} if user_id is not None else {}}
    if session is not None:
        out["session"] = session
    return out


# Back-compat alias — historical name used elsewhere (grant_transport etc.).
_owner_payload_from_cookie = _payload_from_cookie


async def _verify(request: Request) -> dict:
    """Common request guard: HMAC validation + allowed-user check. Owner-only
    routes must call _require_owner(payload) themselves on top of this.

    Auth precedence:
      1. `X-Init-Data` header (Telegram WebApp) — the ACTIVE Telegram account
      2. Cookie `sentinel_apk_session` (v1 owner or v2 scoped) — APK / web

    initData is checked FIRST and on purpose: the Telegram in-app WebView shares
    ONE cookie jar across accounts, so an ambient `sentinel_apk_session` cookie
    left while signed in as the owner would otherwise authenticate a DIFFERENT
    active account as the owner (cross-account leak — a second account seeing
    the owner's data). The per-request, Telegram-signed initData reflects who is
    ACTUALLY active, so it must win.

    Returns a payload dict with `user.id` plus a `session` field carrying the
    parsed cookie/synthetic session — used by require_scope() to enforce
    per-route permissions."""
    # bot.py reads SMDL_BOT_TOKEN — keep this in sync. Fall back to the generic
    # names for cross-deployment portability.
    bot_token = (
        os.environ.get("SMDL_BOT_TOKEN")
        or os.environ.get("BOT_TOKEN")
        or os.environ.get("TELEGRAM_BOT_TOKEN")
        or ""
    )
    # Canonical header is X-Init-Data (used across all SMDL surfaces). The
    # Stremio sub-app historically sent X-Telegram-Init-Data, so accept both.
    init_data = (request.headers.get("x-init-data")
                 or request.headers.get("x-telegram-init-data") or "")

    # Path 1 (PREFERRED) — Telegram initData (Mini App). The authoritative
    # identity of the active account; wins over any ambient session cookie.
    if init_data:
        if not bot_token:
            raise HTTPException(status_code=503, detail="bot token not configured")
        payload = _validate_init_data(init_data, bot_token)
        await _check_access(payload)
        # Synthesise a session for require_scope(). The OWNER gets a wildcard so
        # everything passes. A non-owner is only reachable here on the community
        # build (where _check_access opens the gate to all Telegram users); they
        # get the user-facing surface scopes only — never owner-sensitive ones —
        # so owner-only routes still 403. Paid caps are gated by entitlements.
        uid = (payload.get("user") or {}).get("id")
        is_owner_user = bool(uid) and _is_owner(int(uid))
        if is_owner_user:
            scopes: list[str] = ["*"]
            user_id_str = "owner"
        else:
            # Merge any live (redeemed, non-revoked, non-expired) beta-key extras
            # into the community surface — a TG user who redeemed a beta key gets
            # that scope without re-login (every request rebuilds this session).
            user_id_str = str(uid)
            from . import beta_keys as _bk
            extras = await _bk.live_extra_scopes_for(user_id_str)
            scopes = sorted(set(COMMUNITY_USER_SCOPES) | set(extras))
        payload["session"] = {
            "version": "initdata",
            "user_id": user_id_str,
            "scopes": scopes,
            "jti": "", "iat": 0, "expired": False,
        }
        return payload

    # Path 2 — session cookie (APK / web login; no initData present). Resolves
    # to the cookie's OWN identity (a scoped cookie never becomes the owner).
    cookie_val = request.cookies.get(COOKIE_NAME, "")
    session = _parse_session_cookie(cookie_val)
    if session is not None:
        payload = _payload_from_cookie(session)
        if payload["user"].get("id"):
            return payload

    if not bot_token:
        raise HTTPException(status_code=503, detail="bot token not configured")
    raise HTTPException(status_code=401, detail="authentication required")


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
    # Owner is the union of two signals so this never disagrees with
    # /auth/session (which keys off the cookie's wildcard scope):
    #   • a wildcard-scope session — the v1 APK owner cookie or an
    #     initData session, both of which mean "this caller is the owner"
    #     regardless of whether owner_chat_id happens to be configured;
    #   • the legacy chat-id match for the Telegram bot path.
    # Without the scope check, an owner-cookie session whose synthesised
    # uid can't be matched would render is_owner=False and silently hide
    # the Files / Scraper / Admin surfaces.
    scopes = (p.get("session") or {}).get("scopes") or []
    is_owner = ("*" in scopes) or _is_owner(uid)
    return {
        "user": p.get("user"),
        "owner_chat_id": _cfg_get("owner_chat_id"),
        "is_owner": is_owner,
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
    from . import stremio as _st, stremio_settings as _ss
    q = (q or "").strip()
    if not q:
        return {"results": []}
    addons = _effective_addons(await _ss.get_all())
    try:
        items = await asyncio.to_thread(_st.search, q, type, addons, limit)
    except Exception as e:
        logger.exception("stremio search failed")
        raise HTTPException(500, f"search failed: {e!s}")
    return {"results": [
        {"id": m.id, "type": m.type, "name": m.name, "year": m.year,
         "poster": m.poster, "description": m.description,
         "imdb_rating": m.imdb_rating, "genres": m.genres}
        for m in items
    ]}


def _meta_to_dict(m) -> dict:
    return {"id": m.id, "type": m.type, "name": m.name, "year": m.year,
            "poster": m.poster, "description": m.description,
            "imdb_rating": m.imdb_rating, "genres": m.genres}


def _trakt_progress_row(it: dict) -> Optional[dict]:
    """Map one Trakt /sync/playback item → the local resume-row shape, keyed by
    the Stremio content_id (`tt…` for a movie, `tt…:S:E` for an episode) so it
    dedupes cleanly against the local resume table. None if not IMDb-addressable."""
    typ = it.get("type")
    pct = float(it.get("progress") or 0.0)
    updated = it.get("paused_at") or ""
    if typ == "movie":
        cid = (((it.get("movie") or {}).get("ids") or {}).get("imdb")) or ""
    elif typ == "episode":
        show_imdb = (((it.get("show") or {}).get("ids") or {}).get("imdb")) or ""
        ep = it.get("episode") or {}
        s, n = ep.get("season"), ep.get("number")
        cid = f"{show_imdb}:{s}:{n}" if show_imdb and s is not None and n is not None else ""
    else:
        return None
    if not cid.startswith("tt"):
        return None
    return {"imdb_id": cid, "progress_pct": round(pct, 1),
            "position_seconds": None, "duration_seconds": None,
            "updated_at": updated, "_src": "trakt"}


async def _trakt_progress_rows() -> list[dict]:
    """Trakt's cross-device resume list, mapped to resume-row shape. Best-effort:
    not connected / network blip / API error all degrade to [] so Continue
    Watching still renders from local progress alone."""
    try:
        from . import trakt as _t
        tok = _t.load_token()
        if not tok:
            return []
        tok = await asyncio.to_thread(_t.refresh_if_needed, tok)
        items = await asyncio.wait_for(
            asyncio.to_thread(_t.playback_progress, tok), timeout=6)
    except Exception:
        logger.debug("trakt playback_progress unavailable", exc_info=True)
        return []
    rows = [_trakt_progress_row(it) for it in (items or [])]
    # Match the local list_progress filter: drop watched-through (>92%) and
    # not-really-started (<1%) so a merged row stays "actually resumable".
    return [r for r in rows if r and 1.0 <= r["progress_pct"] < 92.0]


def _merge_progress(local: list[dict], trakt: list[dict]) -> list[dict]:
    """Union local + Trakt resume rows, deduped by content_id. The
    most-recently-updated side wins the progress %, but exact
    `position_seconds` (only the local table has it) is always carried over so
    resume-to-the-second still works for titles Trakt also knows about.
    Newest-first, capped at 12."""
    by_id: dict[str, dict] = {}
    for r in [*local, *trakt]:
        cid = r.get("imdb_id")
        if not cid:
            continue
        prev = by_id.get(cid)
        if prev is None:
            by_id[cid] = dict(r)
            continue
        newer, older = ((r, prev) if (r.get("updated_at") or "") > (prev.get("updated_at") or "")
                        else (prev, r))
        merged = dict(newer)
        if merged.get("position_seconds") is None and older.get("position_seconds") is not None:
            merged["position_seconds"] = older["position_seconds"]
            merged["duration_seconds"] = older.get("duration_seconds")
        by_id[cid] = merged
    return sorted(by_id.values(), key=lambda x: x.get("updated_at") or "", reverse=True)[:12]


@router.get("/api/miniapp/stremio/discover")
async def stremio_discover(request: Request):
    """Stremio-style discovery home. Returns three rows:
      • continue_watching — in-progress titles MERGED from the local resume
        table AND Trakt's cross-device playback progress (a title started on
        the TV/phone resumes here), enriched with poster/name via Cinemeta
        meta (each carries a progress_pct + resume_id for one-tap continue).
      • popular_movies / popular_series — Cinemeta "top" catalog.
    Each row is independent: a failure in one returns [] for that row
    rather than failing the whole page."""
    p = await _verify(request)
    _require_owner(p)
    from . import stremio as _st
    from . import stremio_settings as _ss

    addons = _effective_addons(await _ss.get_all())

    async def _popular(type_: str) -> list[dict]:
        try:
            items = await asyncio.to_thread(_st.get_catalog, type_, None, addons, 20)
            return [_meta_to_dict(m) for m in items]
        except Exception:
            logger.exception("discover popular %s failed", type_)
            return []

    async def _continue() -> list[dict]:
        try:
            local, trakt = await asyncio.gather(
                _ss.list_progress(limit=12), _trakt_progress_rows())
        except Exception:
            logger.exception("discover continue-watching list failed")
            return []
        prog = _merge_progress(local, trakt)

        async def _enrich(row: dict) -> Optional[dict]:
            rid = row["imdb_id"]
            parent = rid.split(":")[0]
            type_ = "series" if ":" in rid else "movie"
            if not parent.startswith("tt"):
                return None
            try:
                m = await asyncio.to_thread(_st.get_meta, parent, type_, None)
            except Exception:
                return None
            if not m:
                return None
            d = _meta_to_dict(m)
            d["resume_id"] = rid
            d["progress_pct"] = row.get("progress_pct", 0.0)
            d["position_seconds"] = row.get("position_seconds")
            return d

        enriched = await asyncio.gather(*[_enrich(r) for r in prog])
        return [d for d in enriched if d]

    cont, movies, series = await asyncio.gather(
        _continue(), _popular("movie"), _popular("series"))
    return {"continue_watching": cont,
            "popular_movies": movies,
            "popular_series": series}


@router.get("/api/miniapp/stremio/episodes")
async def stremio_episodes(request: Request, imdb_id: str = ""):
    """Series episode list. Used by the Detail view when type='series'.

    Returns episodes sorted (S1E1, S1E2, ..., S2E1, ...) — each with the
    Stremio addon `id` (e.g. 'tt0903747:1:1') ready to feed into
    /streams for resolution."""
    p = await _verify(request)
    _require_owner(p)
    from . import stremio as _st, stremio_settings as _ss
    imdb_id = (imdb_id or "").strip()
    if not imdb_id.startswith("tt"):
        raise HTTPException(400, "imdb_id must start with 'tt'")
    addons = _effective_addons(await _ss.get_all())
    try:
        eps = await asyncio.to_thread(_st.get_series_episodes, imdb_id, addons)
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
    from . import stremio as _st, stremio_settings as _ss
    imdb_id = (imdb_id or "").strip()
    if not imdb_id.startswith("tt"):
        raise HTTPException(400, "imdb_id must start with 'tt'")
    addons = _effective_addons(await _ss.get_all())
    try:
        raw = await asyncio.to_thread(_st.get_streams, imdb_id, type, addons)
    except Exception as e:
        logger.exception("stremio streams failed")
        raise HTTPException(500, f"streams failed: {e!s}")
    ranked = _st.rank_streams(raw, preferred_quality=quality)
    top = ranked[:40]
    # Annotate releases RD has already refused (learned from past grabs) so the
    # client can grey them out — no live RD probe (instantAvailability is dead).
    from . import stremio_queue as _sq
    blocked = await _sq.blocked_infohashes([s.infohash for s in top if s.infohash])
    return {"streams": [
        {"title": s.title, "infohash": s.infohash, "has_magnet": bool(s.magnet),
         "size_bytes": s.size_bytes, "seeders": s.seeders, "quality": s.quality,
         "source_addon": s.source_addon, "file_index": s.file_index,
         "rd_blocked": bool(s.infohash and s.infohash.lower() in blocked)}
        for s in top
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
        if e.is_infringing:
            from . import stremio_queue as _sq
            ih = (body.infohash or "").lower()
            if ih:
                await _sq.block_infohash(ih, reason="rd_infringing")
            return {
                "ok": False,
                "error_kind": "rd_infringing",
                "error": "Not cached on Real-Debrid — try another source",
            }
        return {"ok": False, "error_kind": "rd_error", "error": str(e)}
    return {
        "ok": True,
        "files": [
            {"filename": f.filename, "filesize": f.filesize,
             "direct_url": f.direct_url, "mime_type": f.mime_type}
            for f in files
        ],
    }


# ── Theater P7 — Settings + resume position routes ─────────────────────────

async def _settings_with_cache(_ss, _sc) -> dict:
    s = await _ss.get_all()
    return {**s, "cache_root": str(_sc.current_root()),
            "cache_path": _sc.root_override()}


@router.get("/api/miniapp/stremio/settings")
async def stremio_settings_get(request: Request):
    p = await _verify(request)
    _require_owner(p)
    from . import stremio_settings as _ss, stremio_cache as _sc
    return {"settings": await _settings_with_cache(_ss, _sc)}


class _StremioSettingsPatch(BaseModel):
    default_quality: Optional[str] = None
    cache_max_gb: Optional[float] = None
    cache_path: Optional[str] = None
    addons: Optional[list[str]] = None
    auto_grab_top_seeded: Optional[bool] = None


@router.post("/api/miniapp/stremio/settings")
async def stremio_settings_set(body: _StremioSettingsPatch, request: Request):
    p = await _verify(request)
    _require_owner(p)
    from . import stremio_settings as _ss, stremio_cache as _sc
    # exclude_unset keeps explicitly-sent nulls (e.g. clearing the hard cap)
    # while dropping keys the client didn't touch.
    patch = body.model_dump(exclude_unset=True)
    # cache_path is not a stored setting — it drives the cache root override.
    if "cache_path" in patch:
        new_path = patch.pop("cache_path")
        try:
            await asyncio.to_thread(_sc.set_root, new_path)
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
    if patch:
        await _ss.update(patch)
    return {"settings": await _settings_with_cache(_ss, _sc)}


# ── Theater — Addons tab (#66) ──────────────────────────────────────────────
def _effective_addons(settings: dict):
    """The addon set actually used for queries: the owner's configured list,
    or None (→ stremio.DEFAULT_ADDONS) when they haven't customised it."""
    return (settings.get("addons") or None)


@router.get("/api/miniapp/stremio/addons")
async def stremio_addons_list(request: Request):
    """Installed addons (resolved to name/logo/resources) + a curated
    discovery catalog to add from."""
    p = await _verify(request)
    _require_owner(p)
    from . import stremio as _st, stremio_settings as _ss
    s = await _ss.get_all()
    installed_urls = s.get("addons") or list(_st.DEFAULT_ADDONS)
    using_defaults = not s.get("addons")
    resolved = await asyncio.gather(
        *[asyncio.to_thread(_st.manifest_summary, u) for u in installed_urls])
    installed = [x for x in resolved if x]
    installed_set = {x["url"] for x in installed}
    cat_resolved = await asyncio.gather(
        *[asyncio.to_thread(_st.manifest_summary, u) for u in _st.CURATED_ADDONS])
    catalog = [{**x, "installed": x["url"] in installed_set}
               for x in cat_resolved if x]
    return {"installed": installed, "catalog": catalog,
            "using_defaults": using_defaults}


class _AddonBody(BaseModel):
    url: str


@router.post("/api/miniapp/stremio/addons/add")
async def stremio_addons_add(body: _AddonBody, request: Request):
    p = await _verify(request)
    _require_owner(p)
    from . import stremio as _st, stremio_settings as _ss
    url = (body.url or "").strip()
    if not url:
        return JSONResponse({"ok": False, "error": "empty url"}, status_code=400)
    if not url.endswith("manifest.json"):
        url = url.rstrip("/") + "/manifest.json"
    summ = await asyncio.to_thread(_st.manifest_summary, url)
    if not summ:
        return JSONResponse(
            {"ok": False, "error": "no valid Stremio manifest at that URL"},
            status_code=400)
    s = await _ss.get_all()
    current = s.get("addons") or []
    if not current:
        # Seed with the defaults so adding a custom addon doesn't silently
        # drop Cinemeta (metadata) and break search.
        current = list(_st.DEFAULT_ADDONS)
    if url not in current:
        current.append(url)
    s2 = await _ss.update({"addons": current})
    return {"ok": True, "addon": summ, "addons": s2.get("addons")}


@router.post("/api/miniapp/stremio/addons/remove")
async def stremio_addons_remove(body: _AddonBody, request: Request):
    p = await _verify(request)
    _require_owner(p)
    from . import stremio as _st, stremio_settings as _ss
    url = (body.url or "").strip()
    s = await _ss.get_all()
    current = s.get("addons") or list(_st.DEFAULT_ADDONS)
    current = [u for u in current if u != url]
    s2 = await _ss.update({"addons": current})
    return {"ok": True, "addons": s2.get("addons")}


@router.get("/api/miniapp/stremio/rd-token")
async def stremio_rd_token_status(request: Request):
    """Whether a Real-Debrid token is configured (never returns the value)."""
    p = await _verify(request)
    _require_owner(p)
    from . import realdebrid as _rd
    return _rd.token_status()


class _RDTokenBody(BaseModel):
    token: str


@router.post("/api/miniapp/stremio/rd-token")
async def stremio_rd_token_set(body: _RDTokenBody, request: Request):
    """Owner pastes their personal RD token (real-debrid.com/apitoken).
    Persisted to the bind-mounted token file, then validated by hitting
    /user. Returns the new status + account check so the UI can confirm."""
    p = await _verify(request)
    _require_owner(p)
    from . import realdebrid as _rd
    tok = (body.token or "").strip()
    if not tok:
        return JSONResponse({"ok": False, "error": "empty token"}, status_code=400)
    try:
        _rd.set_token(tok)
    except _rd.RealDebridError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    except OSError as e:
        return JSONResponse({"ok": False, "error": f"could not save token: {e}"},
                            status_code=500)
    # Validate against RD immediately so the owner gets instant feedback.
    try:
        a = _rd.get_account()
        account = {"ok": True, "username": a.username, "is_premium": a.is_premium,
                   "days_left": round(a.premium_seconds_left / 86400, 1)}
    except _rd.RealDebridError as e:
        account = {"ok": False, "error": str(e)}
    return {"ok": True, "status": _rd.token_status(), "account": account}


@router.get("/api/miniapp/stremio/pia")
async def stremio_pia_status(request: Request):
    """Whether PIA VPN creds are configured (read-only; never returns secrets).
    Creds are managed in WCM/.env.local — this just surfaces status for the
    Settings page. Feeds the gluetun-gated direct-torrent fallback."""
    p = await _verify(request)
    _require_owner(p)
    from . import pia as _pia
    return _pia.status()


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
    """The user's Trakt watchlist, ENRICHED with Cinemeta posters/meta so it
    renders as a poster row in Theater's Library tab — tap a poster to drop
    straight into the detail/grab flow. Per-item enrichment is best-effort:
    a title Cinemeta doesn't know falls back to bare name/year."""
    p = await _verify(request)
    _require_owner(p)
    from . import trakt as _t
    from . import stremio as _st
    tok = _t.load_token()
    if not tok:
        return {"ok": False, "error": "trakt not connected", "items": []}
    try:
        tok = await asyncio.to_thread(_t.refresh_if_needed, tok)
        data = await asyncio.to_thread(_t.watchlist, tok, type_=type)
    except _t.TraktError as e:
        return {"ok": False, "error": str(e), "items": []}

    async def _enrich(entry: dict) -> Optional[dict]:
        section = entry.get("movie") or entry.get("show") or {}
        imdb = (section.get("ids") or {}).get("imdb") or ""
        if not imdb.startswith("tt"):
            return None
        type_ = "movie" if "movie" in entry else "series"
        try:
            m = await asyncio.to_thread(_st.get_meta, imdb, type_, None)
        except Exception:
            m = None
        if m:
            return _meta_to_dict(m)
        return {"id": imdb, "type": type_, "name": section.get("title") or "",
                "year": section.get("year"), "poster": None, "description": None,
                "imdb_rating": None, "genres": []}

    enriched = await asyncio.gather(*[_enrich(e) for e in (data or [])])
    return {"ok": True, "items": [d for d in enriched if d]}


# ── Follow-a-show (Sonarr-lite auto-download of new episodes) ────────────────
class _FollowBody(BaseModel):
    imdb_id: str
    follow: bool = True
    title: str = ""
    poster: str = ""


@router.post("/api/miniapp/stremio/follow")
async def stremio_follow(body: _FollowBody, request: Request):
    """Follow/unfollow a series. While followed, a background loop auto-grabs
    newly-aired episodes into your Library (best stream → RD → cache)."""
    p = await _verify(request)
    _require_owner(p)
    from . import follows as _f
    imdb = (body.imdb_id or "").split(":")[0].strip()   # the SHOW, not an episode id
    if not imdb.startswith("tt"):
        raise HTTPException(400, "imdb_id must start with 'tt'")
    if body.follow:
        await _f.follow(imdb, title=body.title, poster=body.poster)
    else:
        await _f.unfollow(imdb)
    return {"ok": True, "following": body.follow}


@router.get("/api/miniapp/stremio/follow/status")
async def stremio_follow_status(request: Request, imdb_id: str = ""):
    p = await _verify(request)
    _require_owner(p)
    from . import follows as _f
    imdb = (imdb_id or "").split(":")[0].strip()
    return {"following": await _f.is_following(imdb)}


@router.get("/api/miniapp/stremio/follows")
async def stremio_follows(request: Request):
    p = await _verify(request)
    _require_owner(p)
    from . import follows as _f
    return {"follows": await _f.list_follows()}


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
    return {"ok": True, "job_id": job_id, "job": _stremio_job_to_dict(job)}


@router.get("/api/miniapp/stremio/jobs")
async def stremio_jobs_list(request: Request, limit: int = 50):
    """Recent jobs across all states. Active first, then most recent."""
    p = await _verify(request)
    _require_owner(p)
    from . import stremio_queue as _sq
    jobs = await _sq.list_jobs(limit=limit)
    return {"jobs": [_stremio_job_to_dict(j) for j in jobs]}


@router.get("/api/miniapp/stremio/jobs/{job_id}")
async def stremio_jobs_get(job_id: int, request: Request):
    p = await _verify(request)
    _require_owner(p)
    from . import stremio_queue as _sq
    job = await _sq.get_job(job_id)
    if not job:
        raise HTTPException(404, "no such job")
    return {"job": _stremio_job_to_dict(job)}


@router.get("/api/miniapp/stremio/file/{infohash}")
async def stremio_file_stream(infohash: str, request: Request):
    """Range-served video for a Stremio grab. Two sources, transparently:

    1. Fully cached file on disk → plain range serve (seek anywhere).
    2. A torrent still downloading via the qB-behind-PIA fallback → progressive
       serve: we hand out only bytes whose covering pieces are already on disk
       and block until the rest arrives. Same URL, so the player connects once
       and keeps playing as 'streaming' flips to 'cached'.

    Phones seek mid-stream because we honour the HTTP Range header in both."""
    p = await _verify(request)
    _require_owner(p)
    from . import stremio_cache as _cache
    entry = _cache.find_by_infohash(infohash)
    if entry and entry.file_path.exists():
        _cache.touch_last_played(infohash)
        return _serve_with_range(entry.file_path, entry.mime or "application/octet-stream",
                                  request.headers.get("range"))
    # Not cached (yet). If a live torrent is mid-download, serve it progressively.
    from . import qbittorrent as _qb
    if await asyncio.to_thread(_qb.torrent, infohash) is not None:
        resp = _serve_progressive_torrent(infohash, request.headers.get("range"))
        if resp is not None:
            return resp
    # Finalize race: the torrent may have just moved into the cache between the
    # two checks. Give it a brief window before declaring 404.
    for _ in range(6):
        await asyncio.sleep(0.5)
        entry = _cache.find_by_infohash(infohash)
        if entry and entry.file_path.exists():
            _cache.touch_last_played(infohash)
            return _serve_with_range(entry.file_path, entry.mime or "application/octet-stream",
                                      request.headers.get("range"))
    raise HTTPException(404, "not cached")


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
        "root": str(_cache.current_root()),
    }


@router.post("/api/miniapp/stremio/cache/purge")
async def stremio_cache_purge(request: Request):
    """Delete every cached file (#67). Returns count + bytes freed."""
    p = await _verify(request)
    _require_owner(p)
    from . import stremio_cache as _cache
    result = await asyncio.to_thread(_cache.purge_all)
    return {"ok": True, **result}


# ── #74: editable app logo (branding) ───────────────────────────────────────
# GET is PUBLIC (no auth) so the WebView <img> tag can load it directly.
# POST/DELETE are owner-only.

@router.get("/api/miniapp/branding/logo")
async def branding_logo_get():
    """Serve the uploaded app logo, or 404 if none set. Public — the page
    <img> loads this without an X-Init-Data header."""
    p = _logo_file()
    if p is None:
        raise HTTPException(status_code=404, detail="no logo")
    ext = p.suffix.lstrip(".").lower()
    mime = {"png": "image/png", "jpg": "image/jpeg",
            "webp": "image/webp", "gif": "image/gif"}.get(ext, "image/png")
    try:
        data = p.read_bytes()
    except Exception:
        raise HTTPException(status_code=404, detail="no logo")
    return Response(content=data, media_type=mime,
                    headers={"Cache-Control": "no-cache"})


@router.get("/api/miniapp/branding/status")
async def branding_status(request: Request):
    """Whether a logo is set (drives the Settings tile preview)."""
    await _verify(request)
    return {"has_logo": _logo_file() is not None}


@router.post("/api/miniapp/branding/logo")
async def branding_logo_set(request: Request):
    """Owner-only. Body: {data_url: "data:image/png;base64,...."}.
    Validates mime (png/jpg/webp/gif — no svg) + 2 MB cap. Replaces any
    existing logo (only one is kept)."""
    p = await _verify(request)
    _require_owner(p)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid JSON")
    data_url = (body or {}).get("data_url") or ""
    if not isinstance(data_url, str) or not data_url.startswith("data:"):
        raise HTTPException(status_code=400, detail="expected a data: URL")
    try:
        header, b64 = data_url.split(",", 1)
        mime = header[5:].split(";", 1)[0].strip().lower()
    except Exception:
        raise HTTPException(status_code=400, detail="malformed data URL")
    ext = _LOGO_MIME_EXT.get(mime)
    if not ext:
        raise HTTPException(status_code=400,
                            detail="unsupported image type (png/jpg/webp/gif only)")
    import base64 as _b64
    try:
        raw = _b64.b64decode(b64, validate=True)
    except Exception:
        raise HTTPException(status_code=400, detail="bad base64 payload")
    if len(raw) > _LOGO_MAX_BYTES:
        raise HTTPException(status_code=413, detail="logo exceeds 2 MB")
    try:
        _BRANDING_DIR.mkdir(parents=True, exist_ok=True)
        # Remove any prior logo (different extension) before writing the new one.
        for old_ext in ("png", "jpg", "webp", "gif"):
            (_BRANDING_DIR / f"{_LOGO_STEM}.{old_ext}").unlink(missing_ok=True)
        (_BRANDING_DIR / f"{_LOGO_STEM}.{ext}").write_bytes(raw)
    except Exception as ex:
        raise HTTPException(status_code=500, detail=f"could not save logo: {ex}")
    return {"ok": True, "has_logo": True}


@router.delete("/api/miniapp/branding/logo")
async def branding_logo_delete(request: Request):
    """Owner-only. Remove the uploaded logo (revert to text wordmark)."""
    p = await _verify(request)
    _require_owner(p)
    removed = False
    try:
        for ext in ("png", "jpg", "webp", "gif"):
            fp = _BRANDING_DIR / f"{_LOGO_STEM}.{ext}"
            if fp.exists():
                fp.unlink(missing_ok=True)
                removed = True
    except Exception as ex:
        raise HTTPException(status_code=500, detail=f"could not remove logo: {ex}")
    return {"ok": True, "removed": removed, "has_logo": False}


# ── Helpers ────────────────────────────────────────────────────────────────

def _stremio_job_to_dict(job) -> dict:
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


def _serve_progressive_torrent(infohash: str, range_header: Optional[str]):
    """Range-serve the picked video of a still-downloading torrent, gating every
    chunk on piece availability. Returns a StreamingResponse, or None if the
    torrent/metadata isn't ready enough to serve (caller falls back to 404).

    qB writes pieces as they land; sequentialDownload fills the file
    front-to-back and firstLastPiecePrio pulls the tail early — so the apparent
    file size lies (a sparse hole sits in the middle). We advertise the TRUE
    final size (from the file row) so the player knows the duration, then stream
    sequentially, blocking per-chunk until pieces_ready() says the covering
    pieces are all on disk. When the torrent vanishes (finalize moved it to the
    cache) our already-open fd survives the rename, so we read to the end."""
    from fastapi.responses import StreamingResponse
    from . import qbittorrent as _qb
    import mimetypes

    t = _qb.torrent(infohash)
    if t is None:
        return None
    rows = _qb.files(infohash)
    vid = _qb.pick_video_file(rows)
    props = _qb.properties(infohash)
    piece_size = int(props.get("piece_size") or 0)
    save_path = t.get("save_path") or props.get("save_path") or "/downloads"
    if not vid or int(vid.get("size") or 0) <= 0 or piece_size <= 0:
        return None

    size = int(vid["size"])
    name = vid["name"]
    file_offset = _qb.file_offset_in_torrent(rows, vid)
    media_type = mimetypes.guess_type(name)[0] or "application/octet-stream"

    start, end = 0, size - 1
    status_code = 200
    if range_header:
        import re
        m = re.match(r"bytes=(\d*)-(\d*)", range_header)
        if not m:
            raise HTTPException(416, "bad Range")
        start = int(m.group(1)) if m.group(1) else 0
        end = int(m.group(2)) if m.group(2) else size - 1
        start = max(0, start); end = min(end, size - 1)
        if start > end:
            raise HTTPException(416, "Range not satisfiable")
        status_code = 206
    length = end - start + 1

    chunk = 1024 * 1024
    # How long to wait for a single chunk's pieces before giving up. The queue's
    # own stall guard fails the job sooner; this is just a backstop so a dead
    # connection doesn't hang a worker forever.
    piece_wait_deadline = 180

    def _stream():
        from pathlib import Path
        fh = None
        try:
            pos = start
            remaining = length
            while remaining > 0:
                want = min(chunk, remaining)
                rng_end = pos + want - 1
                states = _qb.piece_states(infohash)
                if not states:
                    # Torrent gone — finalize moved the file out (our open fd, if
                    # any, survives the rename) or it was killed. Read whatever is
                    # on disk; a short read just ends the stream.
                    if fh is None:
                        path = _qb.live_path(save_path, name)
                        if path is None:
                            return
                        fh = open(path, "rb")
                        fh.seek(pos)
                    data = fh.read(want)
                    if not data:
                        return
                    pos += len(data); remaining -= len(data)
                    yield data
                    continue
                if not _qb.pieces_ready(states, piece_size, file_offset, pos, rng_end):
                    # Pieces not down yet — wait for them, bounded.
                    waited = 0.0
                    while waited < piece_wait_deadline:
                        time.sleep(1.0)
                        waited += 1.0
                        states = _qb.piece_states(infohash)
                        if not states:
                            break  # torrent gone; loop top re-resolves via fd
                        if _qb.pieces_ready(states, piece_size, file_offset, pos, rng_end):
                            break
                    else:
                        return  # backstop timeout — end the stream
                    if not states:
                        continue
                if fh is None:
                    path = _qb.live_path(save_path, name)
                    if path is None:
                        time.sleep(1.0)
                        continue
                    fh = open(path, "rb")
                    fh.seek(pos)
                data = fh.read(want)
                if not data:
                    # File on disk shorter than the pieces claim (rename in
                    # flight) — reopen on next pass.
                    try:
                        fh.close()
                    except Exception:
                        pass
                    fh = None
                    time.sleep(0.5)
                    continue
                pos += len(data); remaining -= len(data)
                yield data
        finally:
            if fh is not None:
                try:
                    fh.close()
                except Exception:
                    pass

    headers = {"Accept-Ranges": "bytes", "Content-Length": str(length)}
    if status_code == 206:
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"
    return StreamingResponse(_stream(), status_code=status_code,
                             media_type=media_type, headers=headers)


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


@router.post("/api/miniapp/downloads/redeliver")
async def downloads_redeliver(request: Request):
    """Re-send a past download's file(s) to the requesting user's Telegram
    chat — no re-download, just push the bytes already on disk.

    Body: {id}  — a download_history row id, scoped to the caller's chat_id
    so a user can only re-deliver their own history.

    Files that still exist on disk and fit under Telegram's upload cap are
    sent inline via the bot. If a file is gone we say so; if it's too big we
    hand back its signed share URL so the Mini App can render a tap link
    (same threshold logic as the live download flow)."""
    from . import bot as _bot
    from . import downloader as _dl

    p = await _verify(request)
    require_scope(p, "smdl.downloader")
    uid = int(p["user"]["id"])

    body = await request.json()
    hist_id = body.get("id")
    if not isinstance(hist_id, int):
        try:
            hist_id = int(hist_id)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="id required")

    row = await _db.get_download(uid, hist_id)
    if row is None:
        raise HTTPException(status_code=404, detail="not in your history")

    files = row.get("files") or []
    from pathlib import Path as _P
    existing = [f for f in files if _P(f).exists()]
    if not existing:
        raise HTTPException(
            status_code=410,
            detail="files no longer on disk — re-download from the source link",
        )

    app = _bot.get_application()
    if app is None or app.bot is None:
        raise HTTPException(status_code=503, detail="bot not running")

    caption = row.get("url") or None
    res = await _dl.send_files(app.bot, uid, existing, caption=caption)

    # File(s) too large for Telegram → fall back to the signed share link.
    if res.get("error") == "file_too_large":
        enriched = _enrich_with_share_url(row)
        share = enriched.get("share_url")
        if share:
            return {"ok": True, "delivered": "link", "share_url": share,
                    "size_mb": enriched.get("size_mb")}
        raise HTTPException(
            status_code=413,
            detail=f"file too large to re-send ({res.get('size_mb', '?')} MB)",
        )
    if res.get("error"):
        raise HTTPException(status_code=502, detail=res["error"])

    return {"ok": True, "delivered": "file", "count": len(existing)}


@router.get("/api/miniapp/notifications")
async def notifications_feed(request: Request, limit: int = 40):
    """Consolidated, read-only activity feed for the calling user.

    Merges events that already live in other tables — no per-event write
    path — into one newest-first stream:
      • downloads   (this user's download_history)
      • recordings  (owner only: iptv_recordings state changes)
      • approvals   (owner only: auth_events)

    Unread = events newer than the user's last feed-open marker. Opening the
    feed (POST /notifications/seen) advances that marker."""
    p = await _verify(request)
    require_scope(p, "smdl.downloader")
    uid = int(p["user"]["id"])
    is_owner = bool(p.get("user", {}).get("is_owner"))
    limit = max(1, min(limit, 100))

    items: list[dict] = []

    # — Downloads (per-user) —
    try:
        for d in await _db.list_download_history(uid, limit=limit):
            files = d.get("files") or []
            items.append({
                "type":     "download",
                "ts":       d.get("downloaded_at"),
                "title":    d.get("uploader") or d.get("platform") or "Download",
                "subtitle": (files[0].split("/")[-1] if files else (d.get("url") or "")),
                "status":   "ok",
            })
    except Exception:
        logger.exception("notifications: downloads merge failed")

    # — Recordings + approvals (owner only) —
    if is_owner:
        try:
            from . import iptv as _iptv
            for r in await _iptv.list_iptv_recordings(limit=limit):
                ts = r.get("finished_at") or r.get("started_at") or r.get("requested_at")
                items.append({
                    "type":     "recording",
                    "ts":       ts,
                    "title":    r.get("channel_id") or "Recording",
                    "subtitle": f"{r.get('duration_min', '?')}m · {r.get('status', '')}",
                    "status":   r.get("status") or "queued",
                })
        except Exception:
            logger.exception("notifications: recordings merge failed")
        try:
            for a in await _db.list_auth_events(limit=limit):
                items.append({
                    "type":     "auth",
                    "ts":       a.get("created_at"),
                    "title":    (a.get("action") or "auth").replace("_", " "),
                    "subtitle": (a.get("detail") or (f"user {a.get('chat_id')}" if a.get("chat_id") else "")),
                    "status":   a.get("action") or "",
                })
        except Exception:
            logger.exception("notifications: auth merge failed")

    # Newest first (ISO-8601 UTC sorts lexically). Drop ts-less rows to the end.
    items.sort(key=lambda x: x.get("ts") or "", reverse=True)
    items = items[:limit]

    seen_at = await _db.get_notifications_seen_at(uid)
    unread = sum(1 for it in items if it.get("ts") and (not seen_at or it["ts"] > seen_at))

    return {"items": items, "unread": unread, "seen_at": seen_at}


@router.post("/api/miniapp/notifications/seen")
async def notifications_seen(request: Request):
    """Advance the user's feed read marker to now — clears the unread badge."""
    p = await _verify(request)
    require_scope(p, "smdl.downloader")
    uid = int(p["user"]["id"])
    now = datetime.now(timezone.utc).isoformat()
    await _db.mark_notifications_seen(uid, now)
    return {"ok": True, "seen_at": now}


@router.get("/api/miniapp/search")
async def unified_search(request: Request, q: str = "", limit: int = 8):
    """One search box across SMDL's content pillars. Fans out to the
    existing per-module queries and returns grouped results:
      • channels   — IPTV channels (all users)
      • theater    — Cinemeta movies + series (owner only)
      • downloads  — the caller's own download history (in-memory filter)
      • watchlist  — tracked streamers (scoped: owner=all, else own)

    Each group is independently best-effort: one failing source returns []
    for that group rather than failing the whole search."""
    p = await _verify(request)
    require_scope(p, "smdl.downloader")
    uid = int(p["user"]["id"])
    is_owner = _is_owner(uid)
    needle = (q or "").strip()
    limit = max(1, min(limit, 20))
    if not needle:
        return {"q": "", "groups": {}, "total": 0}
    nlow = needle.lower()

    groups: dict[str, list[dict]] = {}

    # — IPTV channels —
    try:
        from . import iptv as _iptv
        chans = await _iptv.list_channels(q=needle, limit=limit)
        groups["channels"] = [{
            "id": c.id, "name": c.name, "country": getattr(c, "country", None),
            "logo": getattr(c, "logo", None),
        } for c in chans]
    except Exception:
        logger.exception("search: channels failed")
        groups["channels"] = []

    # — Theater (Cinemeta), owner only —
    if is_owner:
        try:
            from . import stremio as _st, stremio_settings as _ss
            addons = _effective_addons(await _ss.get_all())
            movies = await asyncio.to_thread(_st.search, needle, "movie", addons, limit)
            series = await asyncio.to_thread(_st.search, needle, "series", addons, limit)
            merged = (list(movies) + list(series))[: limit]
            groups["theater"] = [{
                "id": m.id, "type": m.type, "name": m.name, "year": m.year,
                "poster": m.poster, "imdb_rating": m.imdb_rating,
            } for m in merged]
        except Exception:
            logger.exception("search: theater failed")
            groups["theater"] = []

    # — Download history (this user, in-memory filter) —
    try:
        hist = await _db.list_download_history(uid, limit=200)
        hits = []
        for d in hist:
            files = d.get("files") or []
            hay = " ".join([
                d.get("url") or "", d.get("uploader") or "",
                d.get("platform") or "", " ".join(files),
            ]).lower()
            if nlow in hay:
                hits.append({
                    "url": d.get("url"),
                    "uploader": d.get("uploader") or d.get("platform"),
                    "file": (files[0].split("/")[-1] if files else ""),
                    "downloaded_at": d.get("downloaded_at"),
                })
            if len(hits) >= limit:
                break
        groups["downloads"] = hits
    except Exception:
        logger.exception("search: downloads failed")
        groups["downloads"] = []

    # — Watchlist (scoped) —
    try:
        wl = stream_monitor.list_watchlist(chat_id=None if is_owner else uid)
        whits = []
        for w in wl:
            url = w.get("url") or ""
            uname = stream_monitor.extract_username(url)
            plat = stream_monitor.extract_platform(url)
            if nlow in (url + " " + (uname or "") + " " + (plat or "")).lower():
                whits.append({"url": url, "username": uname, "platform": plat})
            if len(whits) >= limit:
                break
        groups["watchlist"] = whits
    except Exception:
        logger.exception("search: watchlist failed")
        groups["watchlist"] = []

    total = sum(len(v) for v in groups.values())
    return {"q": needle, "groups": groups, "total": total}


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


# Media-library index. Extension → kind. .ts included for IPTV DVR captures.
_LIB_KINDS = {
    "video": {".mp4", ".mov", ".avi", ".mkv", ".webm", ".ts", ".m4v"},
    "audio": {".mp3", ".m4a", ".ogg", ".flac", ".wav", ".opus"},
    "image": {".jpg", ".jpeg", ".png", ".webp", ".gif"},
}
_LIB_EXT_KIND = {ext: kind for kind, exts in _LIB_KINDS.items() for ext in exts}
# Single-root in-memory cache so polling the page doesn't re-walk the tree.
_LIBRARY_CACHE: dict = {"ts": 0.0, "entries": None}
_LIBRARY_TTL_S = 30.0


def _scan_library() -> list[dict]:
    """Recursively index media files under DOWNLOADS_DIR, newest-first.
    Cached for _LIBRARY_TTL_S to keep repeated page loads cheap. Each entry:
    {name, path (rel), ext, kind, size, mtime, share_url}."""
    import os, time as _t
    from pathlib import Path as _Path
    from .file_serve import sign_share_url, DOWNLOADS_DIR

    now = _t.time()
    cached = _LIBRARY_CACHE.get("entries")
    if cached is not None and (now - _LIBRARY_CACHE["ts"]) < _LIBRARY_TTL_S:
        return cached

    root = _Path(DOWNLOADS_DIR).resolve()
    entries: list[dict] = []
    for dirpath, dirnames, filenames in os.walk(root):
        # Prune hidden dirs + the thumbnail cache in place.
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for fn in filenames:
            ext = _Path(fn).suffix.lower()
            kind = _LIB_EXT_KIND.get(ext)
            if not kind:
                continue
            ap = _Path(dirpath) / fn
            try:
                st = ap.stat()
            except OSError:
                continue
            rel = str(ap.relative_to(root)).replace("\\", "/")
            entries.append({
                "name":      fn,
                "path":      rel,
                "ext":       ext,
                "kind":      kind,
                "size":      st.st_size,
                "mtime":     int(st.st_mtime),
                "share_url": sign_share_url(rel),
            })
    entries.sort(key=lambda e: e["mtime"], reverse=True)
    _LIBRARY_CACHE["entries"] = entries
    _LIBRARY_CACHE["ts"] = now
    return entries


@router.get("/api/miniapp/library")
async def library_index(request: Request, kind: str = "all",
                        limit: int = 120, offset: int = 0):
    """Personal media-server view over the download tree. Returns a flat,
    newest-first list of media files (optionally filtered by kind) plus a
    per-kind summary (count + total bytes) so the UI can render section
    tabs. Owner-only — same boundary as the Files browser."""
    p = await _verify(request)
    require_scope(p, "smdl.admin")
    _require_owner(p)

    limit = max(1, min(limit, 300))
    offset = max(0, offset)

    entries = await asyncio.to_thread(_scan_library)

    summary = {k: {"count": 0, "bytes": 0} for k in _LIB_KINDS}
    for e in entries:
        s = summary[e["kind"]]
        s["count"] += 1
        s["bytes"] += e["size"]

    if kind in _LIB_KINDS:
        filtered = [e for e in entries if e["kind"] == kind]
    else:
        filtered = entries

    page = filtered[offset:offset + limit]
    return {
        "items":   page,
        "total":   len(filtered),
        "offset":  offset,
        "limit":   limit,
        "summary": summary,
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


@router.get("/api/client/build")
async def get_build_descriptor():
    """Public, unauthenticated build descriptor for the app shell (PWA/TWA).

    The store shell reads this BEFORE any auth to learn how to render itself:
    which paid rail to offer (Play Billing vs license keys), whether to show a
    key-redeem surface at all, and which media verticals this build fronts. A
    play build must never show off-store pricing or a redeem field — the client
    keys those off `allow_key_redeem` / `billing_rail` here, so policy posture
    has a single server-side source of truth.
    """
    return {
        "edition": _edition.EDITION,
        "profile": _profile.PROFILE or "default",
        "is_play": _profile.is_play(),
        "billing_rail": _profile.billing_rail(),
        "allow_key_redeem": _profile.allow_key_redeem(),
        "allow_off_store_pricing": _profile.allow_off_store_pricing(),
        "surfaces": sorted(_profile.surfaces()),
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
    actor = _require_owner(p)
    if _auth.is_owner(body.chat_id):
        return JSONResponse({"ok": False, "error": "Cannot ban the owner."}, status_code=400)
    ok = await _db.set_user_status(body.chat_id, "banned", body.reason)
    if not ok:
        return JSONResponse({"ok": False, "error": "No such user."}, status_code=404)
    await _db.log_auth_event("revoke", chat_id=body.chat_id, actor_id=actor,
                             detail=(body.reason or None))
    return {"ok": True}


@router.post("/api/miniapp/admin/users/unban")
async def admin_unban_user(request: Request, body: UserStatusBody):
    p = await _verify(request)
    require_scope(p, "smdl.admin")
    actor = _require_owner(p)
    ok = await _db.set_user_status(body.chat_id, "active")
    if not ok:
        return JSONResponse({"ok": False, "error": "No such user."}, status_code=404)
    await _db.log_auth_event("restore", chat_id=body.chat_id, actor_id=actor)
    return {"ok": True}


class ApproveByCodeBody(BaseModel):
    code: str


@router.post("/api/miniapp/admin/users/approve")
async def admin_approve_user(request: Request, body: UserStatusBody):
    p = await _verify(request)
    require_scope(p, "smdl.admin")
    actor = _require_owner(p)
    ok = await _db.approve_user(body.chat_id)
    if not ok:
        return JSONResponse({"ok": False,
                             "error": "User not found, or is banned (unban first)."},
                            status_code=400)
    await _db.log_auth_event("approve", chat_id=body.chat_id, actor_id=actor)
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
    actor = _require_owner(p)
    # Use the existing ban path under the hood with a 'denied at pending'
    # marker; this keeps a paper trail without inventing a new status enum.
    ok = await _db.set_user_status(body.chat_id, "banned",
                                   f"DENIED@pending: {body.reason}".strip())
    if not ok:
        return JSONResponse({"ok": False, "error": "User not found"}, status_code=404)
    await _db.log_auth_event("deny", chat_id=body.chat_id, actor_id=actor,
                             detail=(body.reason or None))
    return {"ok": True}


@router.post("/api/miniapp/admin/users/approve_by_code")
async def admin_approve_by_code(request: Request, body: ApproveByCodeBody):
    """Owner pastes the 9-digit code a pending user sent them out-of-band.
    We look up the matching pending row and promote it to 'active'.
    Fail-closed: bad/expired/already-used codes return 404 with a generic
    error message — no oracle for code-guessing attackers."""
    p = await _verify(request)
    require_scope(p, "smdl.admin")
    actor = _require_owner(p)
    row = await _db.find_user_by_pending_code(body.code or "")
    if row is None:
        return JSONResponse({"ok": False,
                             "error": "Code not recognised, expired, or already used."},
                            status_code=404)
    await _db.approve_user(int(row["chat_id"]))
    await _db.log_auth_event("approve_by_code", chat_id=int(row["chat_id"]),
                             actor_id=actor)
    return {
        "ok": True,
        "chat_id": int(row["chat_id"]),
        "username": row.get("username"),
        "first_name": row.get("first_name"),
    }


@router.get("/api/miniapp/admin/audit")
async def admin_audit(request: Request):
    """Recent moderation events (approve/deny/revoke/restore). Owner-only."""
    p = await _verify(request)
    require_scope(p, "smdl.admin")
    _require_owner(p)
    rows = await _db.list_auth_events(limit=50)
    return {"items": rows, "count": len(rows)}


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
<!-- #77b favicon reuses the owner-uploaded app logo (#74). 404s to the browser
     default when no logo is set; refreshed live by applyBrandLogo(). -->
<link rel="icon" id="favicon" href="/api/miniapp/branding/logo">
<link rel="apple-touch-icon" id="favicon-apple" href="/api/miniapp/branding/logo">
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<script>
/* Apply the saved appearance before first paint (no flash-of-default-theme).
   Defaults: chrome palette + bold intensity (the futuristic look out of the box). */
(function(){ try {
  var d = document.documentElement;
  d.dataset.theme = localStorage.getItem('smdl_theme') || '@@DEFAULT_THEME@@';
  d.dataset.fx    = localStorage.getItem('smdl_fx')    || '@@DEFAULT_FX@@';
} catch (e) {} })();
</script>
<style>
/* ── Theme engine ───────────────────────────────────────────────────────────
   Black / metallic / futuristic. Palette is PINNED (no longer defers to the
   Telegram client theme) so the look is identical in the Android APK, the
   Windows desktop wrap, and in-Telegram. Two axes, both per-device via
   localStorage and switchable live in Settings → Appearance:
     data-theme: chrome | graphite | obsidian | gunmetal   (accent + metal hue)
     data-fx:    bold | refined                            (glow / sheen / shape)
   The boot <script> in <head> sets both before first paint to avoid a flash.
   The :root / [data-theme] / [data-fx] blocks below are GENERATED from
   app/theme_tokens.json by app/themes.py — edit the JSON, not this CSS. */
/*@@THEME_CSS@@*/
* { box-sizing: border-box; -webkit-tap-highlight-color: transparent; }
body { margin: 0; padding: env(safe-area-inset-top, 0) 0 env(safe-area-inset-bottom, 0) 56px;
       font: 15px/1.4 -apple-system, system-ui, "Segoe UI", Roboto, sans-serif;
       color: var(--fg); min-height: 100vh;
       background:
         radial-gradient(1100px 720px at 78% -12%, rgba(var(--accent-rgb), 0.07), transparent 60%),
         radial-gradient(900px 600px at -10% 110%, rgba(var(--accent-rgb), 0.045), transparent 55%),
         var(--bg);
       background-attachment: fixed;
       transition: padding-left 0.2s ease; }
body.sidebar-collapsed { padding-left: 28px; }
/* Left sidebar (was bottom tabbar). 56px wide normal, 28px collapsed
   (icons only). Settings pinned at the bottom via flex spacer.
   safe-area padding on top so the first nav item doesn't sit behind
   the device status bar / Telegram chrome. */
.sidebar { position: fixed; left: 0; top: 0; bottom: 0; width: 56px;
           background: var(--surface); border-right: 1px solid var(--separator);
           box-shadow: inset -1px 0 0 rgba(255,255,255,calc(0.05 * var(--sheen))),
                       2px 0 16px rgba(0,0,0,0.4);
           display: flex; flex-direction: column; z-index: 10;
           padding: calc(env(safe-area-inset-top, 0px) + 8px) 0 env(safe-area-inset-bottom, 0px);
           transition: width 0.2s ease; overflow: hidden; }
body.sidebar-collapsed .sidebar { width: 28px; }
.sidebar-spacer { flex: 1; }

/* ── Bottom tab bar (replaces the left rail). The HOME stays the cluster-tile
      launcher (one big tile per surface → tap opens that cluster's sub-hub),
      which is what it has always been; the bottom tabs are a parallel quick
      switch. The left rail + its flyout are removed. 2026-06-09 nav revamp. ── */
.sidebar, .subsidebar, .sidebar-toggle { display: none !important; }
body, body.sidebar-collapsed { padding-left: 0 !important;
  padding-bottom: calc(60px + env(safe-area-inset-bottom, 0px)) !important; }
/* Modern bottom tab bar — typography + SVG icon language borrowed from the
   IPTV page nav (system font stack, 1.7-stroke line icons, #5ac8fa accent),
   so /app and the standalone IPTV pages read as one app. */
.bottom-nav { position: fixed; left: 0; right: 0; bottom: 0; z-index: 40;
  display: flex; background: color-mix(in srgb, var(--surface) 92%, transparent);
  backdrop-filter: blur(12px); -webkit-backdrop-filter: blur(12px);
  border-top: 1px solid var(--separator); box-shadow: 0 -2px 16px rgba(0,0,0,0.4);
  padding: 7px 4px calc(7px + env(safe-area-inset-bottom, 0px));
  font: 10px/1.1 -apple-system, system-ui, "Segoe UI", Roboto, sans-serif; }
.bottom-nav .bn-item { position: relative; flex: 1; display: flex; flex-direction: column; align-items: center;
  gap: 4px; padding: 3px 0; cursor: pointer; color: var(--text-muted);
  letter-spacing: .2px; font-weight: 600; border: 0; background: none;
  transition: color .14s ease, transform .1s ease; -webkit-tap-highlight-color: transparent; }
.bottom-nav .bn-ico { display: flex; align-items: center; justify-content: center; }
.bottom-nav .bn-ico svg { width: 23px; height: 23px; display: block; }
.bottom-nav .bn-item.active { color: #5ac8fa; }
.bottom-nav .bn-item.active .bn-ico svg { filter: drop-shadow(0 0 6px rgba(90,200,250,.45)); }
.bottom-nav .bn-item:active { transform: scale(.9); }
.bottom-nav .bn-badge { position: absolute; top: 0; right: 26%; min-width: 7px; height: 7px;
  border-radius: 99px; background: #ff453a; display: none; }
.topright { position: fixed; top: calc(env(safe-area-inset-top, 0px) + 8px); right: 10px;
  z-index: 41; display: flex; gap: 6px; }
.topright button { background: var(--surface); border: 1px solid var(--separator);
  color: var(--text); width: 34px; height: 34px; border-radius: 50%; cursor: pointer;
  display: flex; align-items: center; justify-content: center; font-size: 15px; }
.topright button:active { transform: scale(.92); }
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
.sidebar-item.active { color: var(--accent); border-left-color: var(--accent);
                       background: var(--accent-soft);
                       box-shadow: inset 0 0 18px rgba(var(--accent-rgb), calc(0.12 * var(--sheen))); }
.sidebar-item .icon { font-size: 20px; line-height: 1; }
.sidebar-item .label { font-size: 9.5px; line-height: 1.05; letter-spacing: 0.1px; }
/* Icons-only mode: shrink padding, hide labels, slightly smaller icons. */
body.sidebar-collapsed .sidebar-item { padding: 9px 2px; gap: 0; border-left-width: 2px; }
body.sidebar-collapsed .sidebar-item .label { display: none; }
body.sidebar-collapsed .sidebar-item .icon { font-size: 16px; }
body.sidebar-collapsed .sidebar-toggle { padding: 6px 0; font-size: 12px; }
/* Home tile grid — landing page for the Mini App. 2 cols on phones. */
.home-tiles { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; margin-top: 6px; }
/* Cluster home tiles — single column, wider, more vertical padding. The
   description lists the actual sub-pages in the cluster so the home view
   teaches the structure without a separate label row. */
.home-clusters { display: grid; grid-template-columns: 1fr; gap: 10px; margin-top: 6px; }
/* ── Phase-1 cohesive home: content rows + first-run welcome ──
   The home is the cluster-tile launcher; these discovery rows are currently
   dormant (not populated on home). Collapse the empty container so there's no
   gap under the tiles. Re-enable by calling loadHomeRows() in goto('home'). */
.home-rows:empty { display: none; }
.home-rows { margin-top: 18px; display: flex; flex-direction: column; gap: 18px; }
.home-row-title { font-size: 14px; font-weight: 700; margin: 0 0 8px; }
.home-row-scroll { display: flex; gap: 10px; overflow-x: auto; -webkit-overflow-scrolling: touch;
  scrollbar-width: none; padding-bottom: 2px; }
.home-row-scroll::-webkit-scrollbar { display: none; }
.home-card { flex: 0 0 auto; width: 108px; cursor: pointer; }
.home-card-logo, .home-card-poster { width: 108px; border-radius: 10px; background-size: cover;
  background-position: center; border: 1px solid var(--separator); transition: transform .12s ease; }
.home-card-logo { height: 68px; background-color: #fff; background-size: contain; background-repeat: no-repeat; }
.home-card-poster { height: 152px; background-color: var(--surface); }
.home-card:active .home-card-logo, .home-card:active .home-card-poster { transform: scale(.96); }
.home-card-blank { display: flex; align-items: center; justify-content: center; font-size: 34px;
  font-weight: 700; color: var(--text-muted); }
.home-card-label { font-size: 12px; font-weight: 600; margin-top: 5px; line-height: 1.25;
  display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }
.home-card-sub { font-size: 10.5px; color: var(--text-muted); margin-top: 1px; }
.home-row-empty { display: flex; align-items: center; gap: 12px; flex-wrap: wrap;
  padding: 14px; border: 1px dashed var(--separator); border-radius: 12px; color: var(--text-muted);
  font-size: 13px; }
.home-row-empty button { background: var(--accent, #5ac8fa); color: #04121b; border: 0;
  padding: 8px 14px; border-radius: 8px; font-weight: 700; cursor: pointer; font-size: 13px; }
.welcome-scrim { position: fixed; inset: 0; z-index: 9998; display: none; align-items: center;
  justify-content: center; background: rgba(0,0,0,.6); padding: 20px; }
.welcome-scrim.show { display: flex; }
.welcome-card { background: var(--surface, #15191f); border: 1px solid var(--separator);
  border-radius: 16px; padding: 22px 20px; max-width: 360px; width: 100%; text-align: center;
  box-shadow: 0 14px 50px rgba(0,0,0,.5); }
.welcome-emoji { font-size: 40px; }
.welcome-title { font-size: 19px; font-weight: 700; margin: 6px 0 12px; }
.welcome-body { text-align: left; font-size: 13.5px; line-height: 1.6; }
.welcome-line { margin: 3px 0; }
.welcome-free { margin-top: 12px; padding-top: 12px; border-top: 1px solid var(--separator);
  color: var(--text-muted); font-size: 12.5px; }
.welcome-cta { margin-top: 16px; width: 100%; background: var(--accent, #5ac8fa); color: #04121b;
  border: 0; padding: 11px; border-radius: 10px; font-weight: 700; font-size: 15px; cursor: pointer; }
.home-cluster-tile { background: var(--surface); border: 1px solid var(--separator);
                      border-radius: var(--tile-radius); padding: 16px;
                      display: flex; gap: 14px; align-items: center;
                      cursor: pointer; box-shadow: var(--glow);
                      transition: border-color .15s, transform .08s;
                      -webkit-tap-highlight-color: transparent; color: var(--fg); }
.home-cluster-tile:hover { border-color: var(--accent-line); box-shadow: var(--glow-strong); }
.home-cluster-tile:active { transform: scale(0.99); }
.home-cluster-tile .ico { flex: 0 0 auto; width: 36px; height: 36px;
                           color: var(--accent); display: flex; align-items: center; justify-content: center; }
.home-cluster-tile .ico svg { width: 100%; height: 100%; }
.home-cluster-tile .meta { flex: 1; min-width: 0; }
.home-cluster-tile .name { font-size: 16px; font-weight: 600; margin-bottom: 3px; }
.home-cluster-tile .desc { font-size: 12px; color: var(--muted); line-height: 1.4; }
/* Sub-sidebar (flyout) — slides in to the right of the main 56px-wide
   icon strip when a cluster icon is tapped. Position: fixed so it floats
   above page content without shifting the layout. */
.subsidebar { position: fixed; top: 0; left: 56px; bottom: 0;
              width: 0; background: var(--surface);
              border-right: 1px solid var(--separator);
              overflow: hidden; transition: width .18s ease;
              z-index: 5; padding-top: env(safe-area-inset-top, 0); }
body.sidebar-collapsed .subsidebar { left: 28px; }
.subsidebar.show { width: 168px; box-shadow: 4px 0 18px rgba(0,0,0,0.45); }
.subsidebar-header { font-size: 11px; color: var(--muted); text-transform: uppercase;
                      letter-spacing: 0.08em; padding: 14px 14px 8px; opacity: 0.75; }
.subsidebar-item { display: flex; align-items: center; gap: 10px;
                    padding: 10px 14px; cursor: pointer; color: var(--fg);
                    -webkit-tap-highlight-color: transparent;
                    transition: background .12s; user-select: none; }
.subsidebar-item:hover, .subsidebar-item.current { background: var(--section); }
.subsidebar-item.current { color: var(--button); font-weight: 600; }
.subsidebar-item .icon { width: 18px; height: 18px; flex: 0 0 auto; opacity: 0.85; }
.subsidebar-item .icon svg { width: 100%; height: 100%; }
/* Main-sidebar cluster icon: "expanded" state highlights when its
   sub-sidebar flyout is open. */
.sidebar-item { position: relative; }
.sidebar-item.expanded { background: var(--section); color: var(--button); }
.home-tile { background: var(--surface); border-radius: var(--tile-radius); padding: 16px 12px;
             cursor: pointer; border: 1px solid var(--separator); position: relative;
             overflow: hidden; text-align: left; color: var(--fg);
             box-shadow: var(--glow);
             transition: transform 0.1s, border-color 0.15s, box-shadow 0.18s; }
.home-tile::before { content: ''; position: absolute; inset: 0; pointer-events: none;
             border-radius: inherit;
             background: linear-gradient(180deg, rgba(255,255,255,calc(0.08 * var(--sheen))) 0%, transparent 38%); }
.home-tile:hover { border-color: var(--accent-line); box-shadow: var(--glow-strong); }
.home-tile:active { transform: scale(0.98); }
.home-tile .ico { font-size: 30px; line-height: 1; margin-bottom: 8px; position: relative; }
/* #72 futuristic line-icons: neon accent stroke + soft glow. */
.home-tile .ico svg { width: 30px; height: 30px; display: block; color: var(--accent);
  filter: drop-shadow(0 0 4px var(--accent-line)); transition: filter .15s, transform .15s; }
.home-tile:hover .ico svg { filter: drop-shadow(0 0 7px var(--accent)); transform: translateY(-1px); }
.home-tile .name { font-size: 14px; font-weight: 600; margin-bottom: 2px; position: relative; }
.home-tile .desc { font-size: 11px; color: var(--muted); line-height: 1.3; position: relative; }
.sidebar-item .icon svg { width: 21px; height: 21px; display: block; }
body.sidebar-collapsed .sidebar-item .icon svg { width: 17px; height: 17px; }
.sidebar-item.admin-only { display: none; }
.sidebar-item.admin-only.show { display: flex; }
.home-tile.admin-only { display: none; }
.home-tile.admin-only.show { display: block; }
/* #41 — drag-to-reorder. Pointer-event based (HTML5 draggable is unreliable
   in the Telegram mobile WebView). Edit mode disables the wiggle's transition
   jank, kills page-scroll under the finger (touch-action:none), and shows a
   grab handle. Order persists per-device in localStorage. */
#tiles-arrange-btn.on { background: var(--button); color: var(--button-text); border-color: var(--button); box-shadow: var(--glow); }
.home-tiles.editing .home-tile { cursor: grab; touch-action: none;
  animation: tile-wiggle 0.45s ease-in-out infinite alternate; }
.home-tiles.editing .home-tile::after { content: '⠿'; position: absolute;
  top: 6px; right: 9px; color: var(--muted); font-size: 15px; line-height: 1; }
/* #76 free hold-and-carry: the grabbed tile lifts into a fixed-position
   ghost that tracks the finger, while the original stays in the grid as an
   invisible placeholder so the remaining tiles reflow around the gap. */
.home-tile.dragging { visibility: hidden; animation: none; }
.home-tiles.editing .home-tile.dragging::after { display: none; }
.tile-ghost { box-shadow: 0 12px 30px rgba(0,0,0,0.5); cursor: grabbing;
  border-color: var(--accent-line); opacity: 0.97; will-change: transform;
  animation: none; transition: none; pointer-events: none; }
@keyframes tile-wiggle { from { transform: rotate(-0.7deg); } to { transform: rotate(0.7deg); } }
.page { display: none; padding: max(12px, calc(env(safe-area-inset-top, 0px) + 4px)) 12px 12px; }
.page.active { display: block; }
/* Sticker Maker section nav (top, scrollable) */
.stk-nav { display: flex; gap: 5px; overflow-x: auto; margin: 0 0 12px; padding-bottom: 2px; scrollbar-width: none; }
.stk-nav::-webkit-scrollbar { display: none; }
.stk-nav button { flex: 0 0 auto; font-size: 12.5px; padding: 8px 13px; border-radius: 999px;
  background: var(--surface); border: 1px solid var(--separator); color: var(--fg); cursor: pointer; white-space: nowrap; }
.stk-nav button.on { background: linear-gradient(180deg, var(--accent), var(--accent-2));
  color: var(--button-text); border-color: transparent; box-shadow: var(--glow); }
.stk-sec { display: none; }
.stk-sec.active { display: block; }
/* Sticker Maker header + top-right pack switcher (global; visible on every
   section so you can re-target where new stickers land from anywhere). */
.stk-head { display: flex; align-items: center; gap: 8px; margin: 0 0 10px; }
.stk-pack-dd { position: relative; flex: 0 0 auto; }
.stk-pack-btn { font: inherit; font-size: 12.5px; font-weight: 600; cursor: pointer;
  display: inline-flex; align-items: center; gap: 5px; padding: 6px 12px; border-radius: 999px;
  background: var(--surface); border: 1px solid var(--separator); color: var(--text);
  max-width: 52vw; white-space: nowrap; }
.stk-pack-btn #stk-pack-label { overflow: hidden; text-overflow: ellipsis; max-width: 38vw; }
.stk-pack-menu { position: absolute; top: calc(100% + 6px); right: 0; z-index: 50;
  min-width: 180px; max-height: 52vh; overflow-y: auto; display: none; padding: 6px;
  background: var(--surface); border: 1px solid var(--separator); border-radius: 12px;
  box-shadow: 0 10px 30px rgba(0,0,0,0.45); }
.stk-pack-dd.open .stk-pack-menu { display: block; }
.stk-pack-item { display: block; width: 100%; text-align: left; font: inherit; font-size: 13px;
  cursor: pointer; padding: 8px 10px; border: 0; background: none; color: var(--text);
  border-radius: 8px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.stk-pack-item:hover { background: rgba(255,255,255,0.05); }
.stk-pack-item.active { color: var(--accent); font-weight: 600; }
.stk-pack-new { margin-top: 4px; border-top: 1px solid var(--separator); color: var(--accent); }
.subtabs { display: flex; gap: 6px; margin: 0 0 14px; overflow-x: auto;
           -webkit-overflow-scrolling: touch; scrollbar-width: none; }
.subtabs::-webkit-scrollbar { display: none; }
.subtab { padding: 7px 13px; border: 1px solid var(--separator); border-radius: 16px;
          background: var(--surface); color: var(--fg); font-size: 12px; cursor: pointer;
          white-space: nowrap; transition: background 0.15s, border-color 0.15s, box-shadow 0.18s; }
.subtab:hover { border-color: var(--accent-line); }
.subtab.active { background: linear-gradient(180deg, var(--accent), var(--accent-2));
          color: var(--button-text); border-color: var(--accent); box-shadow: var(--glow); }
.subtab-pane { display: none; }
.subtab-pane.active { display: block; }
h1 { font-size: 1.3em; margin: 6px 0 14px; }
.card { background: var(--surface); border: 1px solid var(--separator);
        border-radius: var(--radius); padding: 12px; margin-bottom: 10px; box-shadow: var(--glow); }
.row { display: flex; align-items: center; gap: 10px; }
.row .grow { flex: 1; min-width: 0; }
.row .name { font-weight: 600; word-break: break-word; }
.row .meta { font-size: 12px; color: var(--muted); margin-top: 2px; word-break: break-all; }
button { background: linear-gradient(180deg, var(--accent), var(--accent-2));
         color: var(--button-text); border: 0; padding: 9px 14px;
         border-radius: 10px; font-size: 14px; font-weight: 600; cursor: pointer;
         letter-spacing: var(--label-ls); box-shadow: var(--glow);
         touch-action: manipulation; transition: transform 0.08s, box-shadow 0.18s, filter 0.15s; }
button:hover { box-shadow: var(--glow-strong); filter: brightness(1.06); }
button:active { transform: scale(0.97); }
button.sec { background: transparent; color: var(--accent); border: 1px solid var(--accent-line);
         box-shadow: none; }
button.sec:hover { background: var(--accent-soft); border-color: var(--accent); filter: none; }
button.danger { background: linear-gradient(180deg, var(--destructive), #c4332b); color: #fff; }
button.small { padding: 6px 10px; font-size: 12px; }
input { width: 100%; padding: 10px 12px; border: 1px solid var(--separator); border-radius: 10px;
        background: var(--bg-elev); color: var(--fg); font-size: 14px;
        transition: border-color 0.15s, box-shadow 0.18s; }
input:focus { outline: none; border-color: var(--accent);
        box-shadow: 0 0 0 3px var(--accent-soft); }
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
.set-tile { background: var(--surface); border: 1px solid var(--separator);
    border-radius: var(--radius); padding: 12px 14px; box-shadow: var(--glow); }
.set-tile .head { display: flex; align-items: center; gap: 6px;
    font-size: 12px; font-weight: 700; color: var(--accent);
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

/* Appearance picker — theme swatches + intensity toggle */
.appearance .lbl { font-size: 11px; color: var(--muted); text-transform: uppercase;
    letter-spacing: 0.6px; margin: 4px 0 6px; }
.theme-swatches { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; }
.swatch { position: relative; display: flex; align-items: center; gap: 8px;
    padding: 10px; border-radius: 10px; border: 1px solid var(--separator);
    cursor: pointer; color: var(--fg); text-align: left; box-shadow: none;
    font-size: 12px; font-weight: 600; overflow: hidden; }
.swatch::before { content: ''; position: absolute; inset: 0;
    background: linear-gradient(180deg, rgba(255,255,255,0.06), transparent 45%); }
.swatch.active { border-color: var(--accent);
    box-shadow: 0 0 0 1px var(--accent), 0 0 16px rgba(var(--accent-rgb), 0.3); }
.swatch .sw-dot { width: 14px; height: 14px; border-radius: 50%; flex: 0 0 auto;
    position: relative; }
.swatch .sw-name { position: relative; white-space: nowrap; }
.fx-toggle { display: flex; gap: 8px; margin-top: 4px; }
.fx-toggle button { flex: 1; background: var(--surface); color: var(--fg);
    border: 1px solid var(--separator); box-shadow: none; font-weight: 500; }
.fx-toggle button.active { background: linear-gradient(180deg, var(--accent), var(--accent-2));
    color: var(--button-text); border-color: var(--accent); box-shadow: var(--glow); }

/* #41 Part 2 — restructure preview / progress */
.rs-summary { font-size: 12px; color: var(--muted); margin-bottom: 8px; }
.rs-summary b { color: var(--fg); }
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
.scraper-row a.uname { color: var(--fg); text-decoration: none; cursor: pointer;
    -webkit-tap-highlight-color: rgba(41,151,255,0.2); }
.scraper-row a.uname:hover { color: var(--button); }
.scraper-row a.uname:active { color: var(--accent); }
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
.page-header #brand-logo { max-height: 40px; max-width: 60%; flex: 1; object-fit: contain; object-position: left center; }
/* Simplified download row — single clickable line: @user · description */
.dl-row { padding: 10px 12px; border-radius: 8px; background: var(--section);
          margin-bottom: 6px; display: flex; align-items: center; gap: 10px; }
.dl-row a { color: var(--fg); text-decoration: none; display: block; flex: 1; min-width: 0; }
.dl-row a:active { color: var(--button); }
.dl-row .user { font-weight: 600; }
.dl-row .desc { color: var(--muted); font-size: 13px; margin-top: 2px;
                word-break: break-all; }
.dl-row .when { color: var(--muted); font-size: 11px; margin-top: 4px; }
.dl-row .redeliver { flex: 0 0 auto; font: inherit; font-size: 12px;
                     padding: 8px 11px; border-radius: 8px; cursor: pointer;
                     border: 1px solid var(--button); background: transparent;
                     color: var(--button); }
.dl-row .redeliver:disabled { opacity: .5; cursor: default; }
/* Activity feed */
.notif-row { display: flex; align-items: flex-start; gap: 10px; padding: 10px 12px;
             border-radius: 8px; background: var(--section); margin-bottom: 6px; }
.notif-row.notif-new { box-shadow: inset 3px 0 0 var(--button); }
.notif-ico { flex: 0 0 auto; font-size: 16px; line-height: 1.4; width: 20px; text-align: center; }
.notif-body { flex: 1; min-width: 0; }
.notif-title { font-weight: 600; text-transform: capitalize; }
.notif-sub { color: var(--muted); font-size: 13px; margin-top: 2px; word-break: break-all; }
.notif-meta { flex: 0 0 auto; text-align: right; }
.notif-status { display: block; font-size: 11px; text-transform: capitalize; }
.notif-when { color: var(--muted); font-size: 11px; }
.tile-badge { position: absolute; top: -6px; right: -6px; min-width: 18px; height: 18px;
              padding: 0 5px; border-radius: 9px; background: #d33; color: #fff;
              font-size: 11px; line-height: 18px; text-align: center; font-weight: 700; }
.home-tile .ico { position: relative; }
/* Unified search */
.search-group { margin-bottom: 14px; }
.search-group-head { font-size: 11px; letter-spacing: .06em; text-transform: uppercase;
                     color: var(--muted); margin: 0 2px 6px; }
.search-row { padding: 10px 12px; border-radius: 8px; background: var(--section);
              margin-bottom: 6px; cursor: pointer; }
.search-row:active { background: var(--bg); }
.search-title { font-weight: 600; }
.search-sub { color: var(--muted); font-size: 13px; margin-top: 2px; word-break: break-all; }
.search-tag { background: var(--bg); padding: 1px 6px; border-radius: 4px; font-size: 11px; }
/* Library page */
.lib-tabs { display: flex; gap: 6px; flex-wrap: wrap; margin-bottom: 14px; }
.lib-tab { padding: 6px 12px; border-radius: 999px; background: var(--section);
           border: 1px solid var(--border); color: var(--muted); cursor: pointer;
           font-size: 13px; user-select: none; }
.lib-tab.active { background: var(--button); border-color: var(--button); color: #fff; }
.lib-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(150px, 1fr));
            gap: 10px; }
.lib-card { background: var(--section); border-radius: 10px; overflow: hidden;
            cursor: pointer; border: 1px solid var(--border); }
.lib-card:active { background: var(--bg); }
.lib-thumb { position: relative; width: 100%; aspect-ratio: 16/10; background: var(--bg);
             display: flex; align-items: center; justify-content: center; font-size: 34px;
             overflow: hidden; }
.lib-thumb img { width: 100%; height: 100%; object-fit: cover; display: block; }
.lib-kind-tag { position: absolute; left: 6px; top: 6px; background: rgba(0,0,0,.55);
                color: #fff; font-size: 11px; padding: 1px 6px; border-radius: 4px; }
.lib-body { padding: 8px 10px; }
.lib-name { font-size: 13px; font-weight: 600; white-space: nowrap; overflow: hidden;
            text-overflow: ellipsis; }
.lib-meta { color: var(--muted); font-size: 12px; margin-top: 2px; }
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
/* #77 gallery nav: prev/next chevrons + position counter. */
.preview-count { color: var(--muted); font-size: 12px; flex: none; font-variant-numeric: tabular-nums; }
.preview-nav { display: none; position: absolute; top: 50%; transform: translateY(-50%);
               width: 46px; height: 70px; align-items: center; justify-content: center;
               background: rgba(0,0,0,0.34); color: #fff; border: none; border-radius: 12px;
               font-size: 36px; line-height: 1; cursor: pointer; z-index: 3;
               -webkit-tap-highlight-color: transparent; user-select: none; }
.preview-nav:active { background: rgba(0,0,0,0.6); }
.preview-modal.gallery #preview-prev, .preview-modal.gallery #preview-next { display: flex; }
#preview-prev { left: 8px; }
#preview-next { right: 8px; }
</style>
</head><body>

<div id=app>
  <div id=msg></div>

  <div class="page active" id=page-home>
    <div class=page-header>
      <img id=brand-logo alt="" style="display:none" />
      <h1 id=brand-text>Sentinel Media</h1>
      <button class="small sec" id=tiles-arrange-btn onclick="toggleTileEdit()" title="Drag tiles to reorder · saved on this device" style="display:none">✥ Arrange</button>
    </div>
    <!-- Cluster home: 5 big tiles. Tap a tile → navigate to that cluster's
         default sub-page; the sub-sidebar (or the page's own subnav, if
         any) handles further hops within the cluster. The description on
         each tile lists the actual sub-pages so the home view teaches
         the structure without a separate label row. -->
    <div class=home-clusters id=home-tiles>
      <div class=home-cluster-tile data-tile=cluster-watch onclick="clusterEnter('watch')">
        <div class=ico><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><rect x="2" y="6" width="20" height="12" rx="2"/><path d="M8 21h8"/><path d="M12 18v3"/></svg></div>
        <div class=meta>
          <div class=name>🎬 Watch</div>
          <div class=desc>IPTV · Theater · Streams</div>
        </div>
      </div>
      <div class=home-cluster-tile data-tile=cluster-get onclick="clusterEnter('get')">
        <div class=ico><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3v10"/><path d="m8 9 4 4 4-4"/><path d="M4 17v2a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-2"/></svg></div>
        <div class=meta>
          <div class=name>📥 Get</div>
          <div class=desc>Downloads · Search<span class=admin-only> · Library · Files</span></div>
        </div>
      </div>
      <div class=home-cluster-tile data-tile=cluster-make onclick="clusterEnter('make')">
        <div class=ico><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><path d="M14 8.5h.01"/><path d="M9 9.5h.01"/><path d="M8.5 14a4 4 0 0 0 7 0"/></svg></div>
        <div class=meta>
          <div class=name>🎨 Make</div>
          <div class=desc>Stickers · Streamer (Twitch opt-in)</div>
        </div>
      </div>
      <div class=home-cluster-tile data-tile=cluster-inbox onclick="clusterEnter('inbox')">
        <div class=ico style="position:relative"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M18 8a6 6 0 0 0-12 0c0 7-3 9-3 9h18s-3-2-3-9"/><path d="M13.7 21a2 2 0 0 1-3.4 0"/></svg><span class=tile-badge id=notif-badge style="display:none"></span></div>
        <div class=meta>
          <div class=name>🔔 Inbox</div>
          <div class=desc>Downloads · recordings · approvals — one feed</div>
        </div>
      </div>
      <div class="home-cluster-tile admin-only" data-tile=cluster-admin onclick="clusterEnter('admin')">
        <div class=ico><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3 5 6v5c0 4 3 7 7 9 4-2 7-5 7-9V6z"/><path d="m9 12 2 2 4-4"/></svg></div>
        <div class=meta>
          <div class=name>⚙️ Admin</div>
          <div class=desc>Server · Scraper · Settings</div>
        </div>
      </div>
    </div>
    <!-- Phase-1 cohesive home: content rows below the cluster tiles. Populated
         by loadHomeRows() on goto('home'); each row degrades to a "Start here"
         empty state so a cold beta user always has a next tap. -->
    <div id=home-rows class=home-rows></div>
  </div>

  <!-- First-run welcome (one-time, dismissable). Shown by maybeShowWelcome()
       keyed off localStorage so it appears once per device. -->
  <div id=welcome-scrim class=welcome-scrim onclick="if(event.target===this)dismissWelcome()">
    <div class=welcome-card>
      <div class=welcome-emoji>👋</div>
      <div class=welcome-title>Welcome to Sentinel Media</div>
      <div class=welcome-body>
        <div class=welcome-line>📺 <b>Live TV</b> — browse &amp; watch free channels</div>
        <div class=welcome-line>📥 <b>Downloader</b> — grab a link</div>
        <div class=welcome-line>🎨 <b>Stickers</b> — make your own packs</div>
        <div class=welcome-line>📚 <b>Library</b> — your saved content</div>
        <div class=welcome-free>All of the above is <b>free</b>. Premium adds recording, multiview, HD &amp; batch downloads — see <a href="/app/entitlements" target=_blank>Plans</a>.</div>
      </div>
      <button class=welcome-cta onclick="dismissWelcome()">Let's go →</button>
    </div>
  </div>

  <!-- Cluster sub-hub: a second screen of tiles (one per sub-page) reached
       by tapping a home cluster-tile. Populated by _renderClusterHub().
       Gives a tap-through alternative to the sidebar flyout sub-nav. -->
  <div class=page id=page-cluster>
    <div class=page-header>
      <h1 id=cluster-title>Section</h1>
      <button class="small sec" onclick="clusterHubBack()" title="Back to home">← Home</button>
    </div>
    <div class=home-clusters id=cluster-tiles></div>
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

  <div class=page id=page-notifications>
    <div class=page-header>
      <h1>Activity</h1>
      <button class="small sec" onclick="loadNotifications()" title="Refresh">🔄</button>
    </div>
    <div id=notifications-list><div class=empty><span class=spin></span> Loading…</div></div>
  </div>

  <div class=page id=page-search>
    <div class=page-header><h1>Search</h1></div>
    <div class=card style="margin-bottom:14px">
      <input id=search-input placeholder="Search channels, movies, downloads, streams…"
             autocomplete=off oninput="onSearchInput()"
             style="width:100%;box-sizing:border-box;padding:10px;background:var(--bg);border:1px solid var(--border);border-radius:8px;color:var(--text);font-size:14px">
    </div>
    <div id=search-results><div class=empty>Type at least 2 characters to search.</div></div>
  </div>

  <div class=page id=page-library>
    <div class=page-header>
      <h1>Library</h1>
      <button class="small sec" onclick="loadLibrary(libKind, true)" title="Rescan">🔄</button>
    </div>
    <div class=lib-tabs id=lib-tabs></div>
    <div id=library-grid><div class=empty><span class=spin></span> Scanning…</div></div>
    <div id=library-more style="text-align:center;margin-top:12px"></div>
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

  <div class=page id=page-stickers>
    <div class=stk-head>
      <h1 style="margin:0">Sticker Maker</h1>
      <span style="flex:1"></span>
      <div class=stk-pack-dd id=stk-pack-dd>
        <button class=stk-pack-btn id=stk-pack-btn onclick="stkPackMenuToggle(event)" title="Switch where new stickers land">📦 <span id=stk-pack-label>Pack</span> <span style="opacity:.55">▾</span></button>
        <div class=stk-pack-menu id=stk-pack-menu></div>
      </div>
    </div>
    <div class=stk-nav id=stk-nav>
      <button data-sec=home onclick="stkSection('home')">🏠 Home</button>
      <button data-sec=add onclick="stkSection('add')">＋ Add</button>
      <button data-sec=stickers onclick="stkSection('stickers')">🎞 Stickers</button>
    </div>

    <div class=stk-sec data-section=stickers>
      <div id=stickers-import-banner style="display:none;margin:0 2px 10px;padding:11px 13px;border:1px solid var(--accent);border-radius:10px;background:rgba(80,120,255,0.10)">
        <div style="font-weight:600;margin-bottom:3px" id=stickers-import-title>Import a pack</div>
        <div class=meta id=stickers-import-sub style="font-size:12px;margin-bottom:9px;color:var(--muted)"></div>
        <div style="display:flex;gap:6px;flex-wrap:wrap;align-items:center">
          <button id=stickers-import-go onclick="stickersDoImport()">📦 Import whole pack</button>
          <button class=sec onclick="stickersDismissImport()" style="font-size:12px">Dismiss</button>
          <span style="flex:1"></span>
          <span id=stickers-import-status class=meta style="font-size:11px;color:var(--muted)"></span>
        </div>
      </div>
      <div class=card id=stickers-pack-card style="margin-bottom:10px">
        <div class=empty><span class=spin></span> Loading…</div>
      </div>
      <h2 style="margin:2px 4px 8px;font-size:15px;color:var(--muted);font-weight:600;display:flex;align-items:center;gap:8px">
        <span>In your pack</span>
        <span id=stickers-pack-count class=meta style="font-size:11px;color:var(--muted)"></span>
        <span style="flex:1"></span>
        <button class=sec id=stickers-refresh-btn onclick=stickersLoadPackContents() style="font-size:11px">↻ Refresh</button>
      </h2>
      <div id=stickers-pack-grid>
        <div class=empty>Loading…</div>
      </div>
      <h2 style="margin:16px 4px 8px;font-size:15px;color:var(--muted);font-weight:600;display:flex;align-items:center;gap:8px">
        <span>🔎 Search your library</span>
        <span style="flex:1"></span>
        <button class=sec id=stk-trash-toggle onclick="stkLibToggleTrash()" title="Show trashed stickers" style="font-size:11px">🗑 Trash</button>
      </h2>
      <input type=text id=stk-search-input placeholder="Find across all packs — emoji or tag…"
             oninput="stkLibSearchDebounced()"
             style="width:100%;box-sizing:border-box;padding:8px 11px;border-radius:9px;border:1px solid var(--separator);background:var(--surface);color:var(--fg);font-size:13px;margin:0 0 8px">
      <div id=stk-search-results></div>
    </div>

    <div class=stk-sec data-section=add>
      <div class=card>
        <div style="display:flex;align-items:center;gap:8px;margin-bottom:8px;flex-wrap:wrap">
          <span style="font-weight:600">Add to your pack</span>
          <span style="flex:1"></span>
          <span class=pill data-mode=instant onclick="stickersSetMode('instant')" style="font-size:11px;background:#222;border:1px solid #333;border-radius:999px;padding:3px 10px;color:#bbb;cursor:pointer;user-select:none">⚡ Instant</span>
          <span class=pill data-mode=manual onclick="stickersSetMode('manual')" title="Open the editor: scrubber · crop · shapes · background cutout" style="font-size:11px;background:#222;border:1px solid #333;border-radius:999px;padding:3px 10px;color:#bbb;cursor:pointer;user-select:none">✂️ Edit / crop</span>
        </div>
        <input type=file id=stickers-file accept="video/*,image/gif" multiple style="display:none">
        <input type=file id=stickers-camera accept="video/*" capture="environment" style="display:none">
        <div id=stickers-dropzone style="border:2px dashed var(--separator);border-radius:10px;padding:18px;text-align:center;cursor:pointer;transition:border-color .15s,background .15s">
          <div style="font-size:30px;line-height:1;margin-bottom:6px">📎</div>
          <div style="font-weight:600">Tap to pick · drag &amp; drop · or paste</div>
          <div class=meta style="margin-top:4px;font-size:12px;color:var(--muted)">Videos / GIFs, ≤ 50 MB each. Drop multiple at once.</div>
        </div>
        <div style="display:flex;gap:6px;flex-wrap:wrap;margin-top:8px">
          <button class=sec onclick="document.getElementById('stickers-camera').click()" style="font-size:12px">📷 Record</button>
          <button class=sec onclick="document.getElementById('stickers-file').click()" style="font-size:12px">📁 Pick files</button>
          <span style="flex:1"></span>
          <span id=stickers-queue-pill class=meta style="font-size:11px;color:var(--muted)"></span>
        </div>
        <div id=stickers-upload-progress style="display:none;margin-top:10px">
          <div style="background:#222;border-radius:6px;height:6px;overflow:hidden">
            <div id=stickers-upload-bar style="background:var(--accent);height:100%;width:0;transition:width .15s"></div>
          </div>
          <div id=stickers-upload-status class=meta style="margin-top:4px;font-size:12px"></div>
        </div>
      </div>
      <div class=card style="margin-top:10px">
        <div style="display:flex;align-items:center;gap:8px;margin-bottom:8px;flex-wrap:wrap">
          <span style="font-weight:600">From a GIF library</span>
          <span style="flex:1"></span>
          <span class=pill data-gifsrc=giphy onclick="stkGifSource('giphy')" style="font-size:11px;background:#222;border:1px solid #333;border-radius:999px;padding:3px 10px;color:#bbb;cursor:pointer;user-select:none">GIPHY</span>
          <span class=pill data-gifsrc=tenor onclick="stkGifSource('tenor')" style="font-size:11px;background:#222;border:1px solid #333;border-radius:999px;padding:3px 10px;color:#bbb;cursor:pointer;user-select:none">Tenor</span>
        </div>
        <input type=text id=stk-gif-q placeholder="Search GIFs — or leave blank for trending…"
               oninput="stkGifSearchDebounced()" onkeydown="if(event.key==='Enter')stkGifSearch()"
               style="width:100%;box-sizing:border-box;padding:8px 11px;border-radius:9px;border:1px solid var(--separator);background:var(--surface);color:var(--fg);font-size:13px">
        <div id=stk-gif-results style="display:grid;grid-template-columns:repeat(auto-fill,minmax(92px,1fr));gap:6px;margin-top:8px"></div>
        <div id=stk-gif-status class=meta style="font-size:11px;margin-top:6px;color:var(--muted)"></div>
        <div class=meta style="font-size:10px;margin-top:4px;color:var(--muted)">Tap a GIF to turn it into a sticker. Powered by GIPHY &amp; Tenor.</div>
      </div>
      <h2 style="margin:16px 4px 8px;font-size:15px;color:var(--muted);font-weight:600">Drafts</h2>
      <div id=stickers-drafts>
        <div class=empty>Drop a video above, or send one to <b>@Sentinel_Media_bot</b>, to start a draft.</div>
      </div>
    </div>

    <div class="stk-sec active" data-section=home>
      <div class=card>
        <div style="display:flex;align-items:center;gap:8px;margin-bottom:8px;flex-wrap:wrap">
          <span style="font-weight:600">Defaults</span>
          <span style="flex:1"></span>
          <span class=pill data-mode=instant onclick="stickersSetMode('instant')" style="font-size:11px;background:#222;border:1px solid #333;border-radius:999px;padding:3px 10px;color:#bbb;cursor:pointer;user-select:none">⚡ Instant</span>
          <span class=pill data-mode=manual onclick="stickersSetMode('manual')" title="Open the editor: scrubber · crop · shapes · background cutout" style="font-size:11px;background:#222;border:1px solid #333;border-radius:999px;padding:3px 10px;color:#bbb;cursor:pointer;user-select:none">✂️ Edit / crop</span>
          <button class=sec id=stk-info-btn onclick="stickersToggleHint()" title="What do Instant / Edit &amp; crop do?" style="font-size:13px;padding:2px 8px;line-height:1">🛈</button>
        </div>
        <div class=meta id=stickers-mode-hint style="display:none;font-size:11px;margin-bottom:8px;color:var(--muted)"></div>
        <div style="display:flex;align-items:center;gap:8px">
          <span class=meta style="font-size:12px;color:var(--muted)">Default emoji <span style="opacity:.7">(Instant mode)</span></span>
          <span style="flex:1"></span>
          <input type=text id=stickers-default-emoji maxlength=8 value="🎬" title="Default emoji used in Instant mode" style="width:54px;padding:4px 6px;border-radius:6px;border:1px solid var(--separator);background:var(--surface);color:var(--fg);font-size:18px;text-align:center">
        </div>
      </div>
      <div class=card style="margin-top:10px;margin-bottom:10px">
        <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">
          <span style="font-weight:600">Send a sticker to the bot</span>
          <span style="flex:1"></span>
          <span class=pill data-imp=single onclick="stickersSetImportPref('single')" style="font-size:11px;background:#222;border:1px solid #333;border-radius:999px;padding:3px 10px;color:#bbb;cursor:pointer;user-select:none">＋ Add one</span>
          <span class=pill data-imp=all onclick="stickersSetImportPref('all')" style="font-size:11px;background:#222;border:1px solid #333;border-radius:999px;padding:3px 10px;color:#bbb;cursor:pointer;user-select:none">📦 Whole pack</span>
        </div>
        <div class=meta style="font-size:11px;margin-top:6px;color:var(--muted)">Sending any sticker to <b>@Sentinel_Media_bot</b> clones it to you. <b>Add one</b> drops just that sticker into your active pack (and offers a button to grab the rest). <b>Whole pack</b> clones the entire set into a brand-new pack automatically.</div>
      </div>
      <div style="display:flex;gap:6px;margin:10px 0;flex-wrap:wrap">
        <button class=sec onclick="stickersToggleLookup()" style="font-size:12px" title="View any TG pack + clone stickers into yours">🔍 Look up pack</button>
      </div>
      <div id=stickers-lookup-card class=card style="display:none;margin-bottom:10px">
        <div style="display:flex;gap:6px;align-items:center;margin-bottom:6px">
          <span style="font-weight:600;flex:1">Look up any sticker pack</span>
          <button class=sec onclick="stickersToggleLookup()" style="font-size:11px">✕</button>
        </div>
        <div class=meta style="font-size:11px;margin-bottom:8px;color:var(--muted)">
          Paste a <code>t.me/addstickers/...</code> URL or just the pack name. Telegram limits us to read-only for packs not created by <b>@Sentinel_Media_bot</b>, but you can <i>clone</i> any sticker into one of your own packs and then fully edit it from there.
        </div>
        <div style="display:flex;gap:6px">
          <input id=stickers-lookup-input type=text placeholder="t.me/addstickers/yourpack or yourpack_name" style="flex:1;padding:6px 8px;border-radius:6px;border:1px solid var(--separator);background:var(--surface);color:var(--fg);font-size:13px">
          <button onclick="stickersDoLookup()">Look up</button>
        </div>
        <div id=stickers-lookup-meta style="margin-top:10px;display:none">
          <div id=stickers-lookup-title style="font-weight:600;margin-bottom:4px"></div>
          <div id=stickers-lookup-status class=meta style="font-size:11px;margin-bottom:6px"></div>
        </div>
        <div id=stickers-lookup-grid></div>
      </div>
      <div style="margin-top:16px;padding:12px;border:1px solid #4a2222;border-radius:10px;background:rgba(120,30,30,0.08)">
        <div style="font-weight:600;color:#e88;margin-bottom:6px">Danger zone</div>
        <div class=meta style="font-size:12px;margin-bottom:10px;color:var(--muted)">These actions can't be undone.</div>
        <div style="display:flex;gap:6px;flex-wrap:wrap">
          <button class=sec onclick=stickersDeleteAll() style="color:#e88">🗑 Delete all my drafts</button>
          <button class=sec onclick=stickersDeletePack() style="color:#e88">💥 Delete entire pack</button>
        </div>
      </div>
    </div>
  </div>

  <div class=page id=page-streamer>
    <h1>Streamer Console</h1>
    <div class=meta style="font-size:12px;color:var(--muted);margin-bottom:14px">
      Sign in with Twitch to opt your channel into community recording. Your consent is the license recorders operate under, so you stay in control of duration, who can record, and when to revoke.
    </div>
    <div id=streamer-content><div class=empty><span class=spin></span> Loading…</div></div>
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
    <div class=card id=viewas-card style="margin-bottom:12px">
      <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">
        <span style="font-weight:600">👁 Preview as</span>
        <span class=meta style="font-size:11px;color:var(--muted);flex:1;min-width:140px">Simulate a community plan to see the paywall gates fire on your own box. Owner-only · downgrade-only · reversible.</span>
      </div>
      <div style="display:flex;gap:6px;margin-top:8px;flex-wrap:wrap;align-items:center">
        <select id=viewas-select style="padding:6px 8px;border-radius:8px;border:1px solid var(--separator);background:var(--surface);color:var(--fg);font-size:13px">
          <option value=owner>Owner — full access (no gating)</option>
          <option value=free>Free</option>
          <option value=registered>Registered</option>
          <option value=plus>Plus</option>
          <option value=family>Family</option>
        </select>
        <button onclick=applyViewAs()>Apply</button>
        <span id=viewas-status class=meta style="font-size:11px;color:var(--muted)"></span>
      </div>
    </div>
    <div id=admin-content><div class=empty><span class=spin></span> Loading…</div></div>
  </div>
</div>

<!-- Owner "preview as <plan>" banner — shown whenever a simulation cookie is set. -->
<div id=viewas-banner style="display:none;position:fixed;left:0;right:0;top:0;z-index:9999;background:#8a5200;color:#fff;padding:6px 12px;font-size:12px;text-align:center;box-shadow:0 2px 10px rgba(0,0,0,.45)">
  <span id=viewas-banner-text></span>
  <button onclick=exitViewAs() style="margin-left:10px;background:rgba(255,255,255,.2);border:0;color:#fff;border-radius:6px;padding:2px 9px;cursor:pointer;font-size:11px">Exit preview</button>
</div>

<!-- Paywall upgrade sheet — pops on a structured 402 from any gated route. -->
<div id=upgrade-modal style="display:none;position:fixed;inset:0;z-index:10000;background:rgba(0,0,0,.62);align-items:center;justify-content:center" onclick="if(event.target===this)closeUpgrade()">
  <div style="background:var(--surface);border:1px solid var(--separator);border-radius:14px;max-width:330px;width:90%;padding:22px;box-shadow:0 12px 44px rgba(0,0,0,.5)">
    <div style="font-size:34px;text-align:center;line-height:1">🔒</div>
    <div id=upgrade-title style="font-size:17px;font-weight:600;text-align:center;margin:8px 0 4px"></div>
    <div id=upgrade-body class=meta style="font-size:13px;text-align:center;color:var(--muted);margin-bottom:16px"></div>
    <button id=upgrade-cta style="width:100%" onclick=upgradeCta()></button>
    <button class=sec style="width:100%;margin-top:8px" onclick=closeUpgrade()>Not now</button>
  </div>
</div>

<div class=preview-modal id=preview-modal>
  <div class=preview-head>
    <div class=name id=preview-name></div>
    <span class=preview-count id=preview-count></span>
    <button onclick="downloadCurrentPreview()">⬇ Download</button>
    <button onclick="closePreview()">✕</button>
  </div>
  <div class=preview-body id=preview-body></div>
  <button class=preview-nav id=preview-prev onclick="galleryNav(-1)" aria-label="Previous">‹</button>
  <button class=preview-nav id=preview-next onclick="galleryNav(1)" aria-label="Next">›</button>
</div>

<div class=sidebar>
  <div class=sidebar-toggle id=nav-toggle onclick="toggleSidebar()" title="Collapse / expand nav">
    <span id=nav-toggle-icon>«</span>
  </div>
  <!-- Sidebar now collapses to 6 entries: Home + 5 cluster icons. The
       cluster icons expand a flyout sub-sidebar (#subsidebar) with the
       actual sub-pages. Direct goto() targets (nav-watchlist, nav-files,
       etc.) are kept available via the sub-sidebar — no functional loss,
       just one extra tap to switch surfaces. -->
  <div class="sidebar-item active" id=nav-home onclick="clusterNavHome()" title="Home">
    <div class=icon><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M3 11l9-7 9 7"/><path d="M5 10v9a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1v-9"/><path d="M9 21v-6h6v6"/></svg></div><div class=label>Home</div>
  </div>
  <div class=sidebar-item id=nav-cluster-watch onclick="clusterOpen('watch')" title="Watch — Theater / IPTV / Streams">
    <div class=icon><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><rect x="2" y="6" width="20" height="12" rx="2"/><path d="M8 21h8"/><path d="M12 18v3"/></svg></div><div class=label>Watch</div>
  </div>
  <div class=sidebar-item id=nav-cluster-get onclick="clusterOpen('get')" title="Get — Downloads / Search / Library / Files">
    <div class=icon><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3v10"/><path d="m8 9 4 4 4-4"/><path d="M4 17v2a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-2"/></svg></div><div class=label>Get</div>
  </div>
  <div class=sidebar-item id=nav-cluster-make onclick="clusterOpen('make')" title="Make — Stickers / Streamer">
    <div class=icon><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><path d="M14 8.5h.01"/><path d="M9 9.5h.01"/><path d="M8.5 14a4 4 0 0 0 7 0"/></svg></div><div class=label>Make</div>
  </div>
  <div class=sidebar-item id=nav-cluster-inbox onclick="clusterOpen('inbox')" title="Inbox — Activity">
    <div class=icon style="position:relative"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M18 8a6 6 0 0 0-12 0c0 7-3 9-3 9h18s-3-2-3-9"/><path d="M13.7 21a2 2 0 0 1-3.4 0"/></svg><span class=tile-badge id=notif-badge-nav style="display:none"></span></div><div class=label>Inbox</div>
  </div>
  <div class="sidebar-item admin-only" id=nav-cluster-admin onclick="clusterOpen('admin')" title="Admin — Server / Scraper / Settings">
    <div class=icon><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3 5 6v5c0 4 3 7 7 9 4-2 7-5 7-9V6z"/><path d="m9 12 2 2 4-4"/></svg></div><div class=label>Admin</div>
  </div>
  <div class=sidebar-spacer></div>
  <div class=sidebar-divider></div>
  <div class=sidebar-item id=nav-account onclick="location.href='/account'" title="Account">
    <div class=icon><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="8" r="4"/><path d="M4 20a8 8 0 0 1 16 0"/></svg></div><div class=label>Account</div>
  </div>
</div>

<!-- Sub-sidebar flyout. Hidden by default. Populated by _renderSubsidebar()
     when a cluster icon is tapped; closes via outside click, navigation,
     or tapping the same cluster icon again. -->
<div class=subsidebar id=subsidebar></div>

<!-- Bottom tab bar (primary nav). Home → content feed; the four clusters open
     their sub-hub via clusterEnter(). Admin + Account live top-right. -->
<nav class=bottom-nav id=bottom-nav>
  <button class="bn-item active" data-tab=home onclick="clusterNavHome()"><span class=bn-ico><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M3 11l9-7 9 7"/><path d="M5 10v9a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1v-9"/><path d="M9 21v-6h6v6"/></svg></span>Home</button>
  <button class=bn-item data-tab=watch onclick="clusterEnter('watch')"><span class=bn-ico><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><rect x="2" y="6" width="20" height="12" rx="2"/><path d="M8 21h8"/><path d="M12 18v3"/><path d="M6 12a4 4 0 0 1 4-4"/></svg></span>Watch</button>
  <button class=bn-item data-tab=get onclick="clusterEnter('get')"><span class=bn-ico><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3v10"/><path d="m8 9 4 4 4-4"/><path d="M4 17v2a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-2"/></svg></span>Get</button>
  <button class=bn-item data-tab=make onclick="clusterEnter('make')"><span class=bn-ico><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><path d="M14 8.5h.01"/><path d="M9 9.5h.01"/><path d="M8.5 14a4 4 0 0 0 7 0"/></svg></span>Make</button>
  <button class=bn-item data-tab=inbox onclick="clusterEnter('inbox')"><span class=bn-ico><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M18 8a6 6 0 0 0-12 0c0 7-3 9-3 9h18s-3-2-3-9"/><path d="M13.7 21a2 2 0 0 1-3.4 0"/></svg></span><span class=bn-badge id=bn-inbox-badge></span>Inbox</button>
</nav>
<div class=topright>
  <button class=admin-only onclick="clusterEnter('admin')" title="Admin">⚙️</button>
  <button onclick="location.href='/account'" title="Account">👤</button>
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
let _tileDrag = null;    // original tile — hidden placeholder during a carry
let _tileGhost = null;   // fixed-position clone that follows the pointer
let _grabDX = 0, _grabDY = 0;

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
    const r = tile.getBoundingClientRect();
    _grabDX = e.clientX - r.left;
    _grabDY = e.clientY - r.top;
    // Build the floating ghost that the finger carries around. It lives on
    // <body> (escaping the grid) and is pointer-events:none so elementFromPoint
    // sees the tiles underneath it.
    const g = tile.cloneNode(true);
    g.classList.add('tile-ghost');
    g.style.position = 'fixed';
    g.style.left = '0'; g.style.top = '0';
    g.style.width = r.width + 'px'; g.style.height = r.height + 'px';
    g.style.margin = '0'; g.style.zIndex = '60';
    g.style.transform = 'translate(' + r.left + 'px,' + r.top + 'px) scale(1.05)';
    document.body.appendChild(g);
    _tileGhost = g;
    tile.classList.add('dragging');   // becomes the invisible placeholder
    try { tile.setPointerCapture(e.pointerId); } catch {}
    e.preventDefault();
  });
  c.addEventListener('pointermove', e => {
    if (!_tileDrag) return;
    e.preventDefault();
    if (_tileGhost) {
      _tileGhost.style.transform =
        'translate(' + (e.clientX - _grabDX) + 'px,' + (e.clientY - _grabDY) + 'px) scale(1.05)';
    }
    const over = document.elementFromPoint(e.clientX, e.clientY)?.closest?.('.home-tile');
    if (!over || over === _tileDrag || over.parentElement !== c) return;
    const tiles = [...c.querySelectorAll('.home-tile')];
    if (tiles.indexOf(_tileDrag) < tiles.indexOf(over)) c.insertBefore(_tileDrag, over.nextSibling);
    else c.insertBefore(_tileDrag, over);
  });
  const endDrag = () => {
    if (!_tileDrag) return;
    _tileDrag.classList.remove('dragging');
    if (_tileGhost) { _tileGhost.remove(); _tileGhost = null; }
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
      const d = j.detail;
      // Structured entitlement 402 (object detail) → pop the paywall sheet and
      // reject with a sentinel so callers can choose to stay silent.
      if (r.status === 402 && d && typeof d === 'object' && d.error === 'entitlement_required') {
        try { showUpgradeSheet(d); } catch (e) {}
        return Promise.reject('upgrade_required');
      }
      msg = (typeof d === 'object' ? (d && (d.detail || d.error)) : d) || j.error || msg;
      if (typeof msg === 'object') msg = JSON.stringify(msg);
    } catch {
      if (txt) msg += ': ' + txt.slice(0, 120).replace(/\s+/g, ' ').trim();
    }
    return Promise.reject(msg);
  });
}

function showOk(t) { const m = document.getElementById('msg'); m.className = 'msg ok'; m.textContent = t; setTimeout(()=>m.className='', 3500); }
function showErr(t) { const m = document.getElementById('msg'); m.className = 'msg err'; m.textContent = String(t); setTimeout(()=>m.className='', 5500); }

// ── Entitlement preview ("view as") + paywall upgrade sheet ───────────────
// The owner can simulate a community plan to watch the paywall gates fire on
// their own box. The banner reads the cookie directly — it's not a secret; the
// server only honours it for the owner identity. The upgrade sheet pops on any
// structured 402 (see api() below).
function _viewAsCookie() {
  const m = document.cookie.match(/(?:^|;\\s*)smdl_view_as=([^;]+)/);
  return m ? decodeURIComponent(m[1]).trim().toLowerCase() : '';
}
function renderViewAsBanner() {
  const b = document.getElementById('viewas-banner');
  if (!b) return;
  const tier = _viewAsCookie();
  if (tier && tier !== 'owner') {
    const t = document.getElementById('viewas-banner-text');
    if (t) t.textContent = '👁 Previewing as ' + tier.toUpperCase() +
      ' — paid features lock exactly as a community ' + tier + ' user sees them.';
    b.style.display = 'block';
  } else {
    b.style.display = 'none';
  }
}
function initViewAsControl() {
  const sel = document.getElementById('viewas-select');
  if (sel) sel.value = _viewAsCookie() || 'owner';
}
async function applyViewAs() {
  const sel = document.getElementById('viewas-select');
  const st = document.getElementById('viewas-status');
  if (!sel) return;
  try {
    await api('/api/admin/view_as', { method: 'POST', body: JSON.stringify({ tier: sel.value }) });
    if (st) st.textContent = 'Applied — reloading…';
    location.reload();
  } catch (e) { if (st) st.textContent = 'Failed: ' + e; }
}
async function exitViewAs() {
  try { await api('/api/admin/view_as', { method: 'POST', body: JSON.stringify({ tier: 'owner' }) }); } catch (e) {}
  location.reload();
}
function showUpgradeSheet(ent) {
  ent = ent || {};
  const plan = ent.required_plan || 'plus';
  const planTitle = plan.charAt(0).toUpperCase() + plan.slice(1);
  const rail = ent.rail || 'license';
  const title = document.getElementById('upgrade-title');
  const body = document.getElementById('upgrade-body');
  const cta = document.getElementById('upgrade-cta');
  if (title) title.textContent = ent.label || 'Premium feature';
  if (body) {
    let msg = 'This needs the ' + planTitle + ' plan.';
    const sim = _viewAsCookie();
    if (sim && sim !== 'owner') msg += ' You\\'re previewing as ' + sim.toUpperCase() + '.';
    body.textContent = msg;
  }
  if (cta) {
    cta.textContent = (rail === 'play') ? '⭐ Upgrade with Google Play' : '⭐ Upgrade to ' + planTitle;
    cta.dataset.rail = rail;
  }
  const m = document.getElementById('upgrade-modal');
  if (m) m.style.display = 'flex';
}
function closeUpgrade() { const m = document.getElementById('upgrade-modal'); if (m) m.style.display = 'none'; }
function upgradeCta() {
  closeUpgrade();
  const sim = _viewAsCookie();
  // While the owner is previewing, "unlock" = drop the preview back to full.
  if (sim && sim !== 'owner') { exitViewAs(); return; }
  const cta = document.getElementById('upgrade-cta');
  const rail = cta ? cta.dataset.rail : 'license';
  showOk(rail === 'play' ? 'Google Play Billing flow — wiring pending' : 'Upgrade flow — wiring pending');
}

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

// ── Cluster nav (5-tile home + sidebar flyout sub-sidebar) ────────────────
// Pages are grouped into 5 clusters. The HOME tiles call clusterEnter()
// which navigates to the cluster's default sub-page. The SIDEBAR cluster
// icons call clusterOpen() which toggles a flyout sub-sidebar listing the
// cluster's pages. The same key feeds both: order pages by most-likely
// default-of-cluster so clusterEnter lands on the right thing.

// Each page entry: [pageId, label, emoji, description, ownerOnly?]. The
// emoji + description drive the sub-hub tiles; pageId + label still drive
// the sidebar flyout (_renderSubsidebar) and _PAGE_TO_CLUSTER, which only
// read [0] and [1], so the extra fields are backward-compatible.
const _CLUSTERS = {
  watch: { label: '🎬 Watch', pages: [
    ['iptv',      'IPTV',      '📺', 'Live TV channels · EPG · DVR'],
    ['theater',   'Theater',   '🍿', 'Movies & shows via Stremio'],
    ['watchlist', 'Streams',   '📡', 'Track streamers & recordings'],
  ]},
  get:   { label: '📥 Get', pages: [
    ['downloads', 'Downloads', '⬇️', 'Paste a URL to download'],
    ['search',    'Search',    '🔎', 'Find across everything'],
    ['library',   'Library',   '📚', 'Your downloaded media', 'owner'],
    ['files',     'Files',     '📁', 'Browse the file store', 'owner'],
  ]},
  make:  { label: '🎨 Make', pages: [
    ['stickers',  'Stickers',  '🎨', 'Build your sticker packs'],
    ['streamer',  'Streamer',  '🎙️', 'Twitch opt-in console'],
  ]},
  inbox: { label: '🔔 Inbox', pages: [
    ['notifications', 'Activity', '🔔', 'Downloads · recordings · approvals'],
  ]},
  admin: { label: '⚙️ Admin', pages: [
    ['admin',     'Server',    '🖥️', 'Status & server controls', 'owner'],
    ['scraper',   'Scraper',   '🕷️', 'Profile scrape jobs',      'owner'],
    ['settings',  'Settings',  '⚙️', 'Preferences & config',     'owner'],
  ]},
};

// Reverse index for sub-page → cluster lookup (used in goto() and
// _renderSubsidebar to highlight the active cluster icon + sub-page).
const _PAGE_TO_CLUSTER = (() => {
  const m = {};
  for (const [key, cluster] of Object.entries(_CLUSTERS)) {
    for (const entry of cluster.pages) m[entry[0]] = key;
  }
  return m;
})();

let _openCluster = null;

function _clusterNavigate(pageId) {
  // Theater + IPTV leave the SPA; everything else routes through goto().
  if (pageId === 'theater') { location.href = '/app/stremio'; return; }
  if (pageId === 'iptv')    { location.href = '/iptv'; return; }
  goto(pageId);
}

let _clusterHubKey = null;   // which cluster the sub-hub page is showing

function clusterEnter(key) {
  // From a home cluster-tile click: open the cluster's SUB-HUB page — a
  // second screen of tiles, one per sub-page — so the whole app is
  // reachable by tapping through the main page. The sidebar flyout
  // (clusterOpen) stays as the quick alternative sub-nav. A cluster with
  // only ONE sub-page skips the hub and goes straight there.
  const c = _CLUSTERS[key];
  if (!c || !c.pages.length) return;
  clusterClose();
  if (c.pages.length === 1) { _clusterNavigate(c.pages[0][0]); return; }
  _renderClusterHub(key);
  goto('cluster');
}

function _renderClusterHub(key) {
  const c = _CLUSTERS[key];
  if (!c) return;
  _clusterHubKey = key;
  const titleEl = document.getElementById('cluster-title');
  if (titleEl) titleEl.textContent = c.label;
  const root = document.getElementById('cluster-tiles');
  if (!root) return;
  let html = '';
  for (const entry of c.pages) {
    // Owner-only sub-pages (Library/Files, the Admin cluster) are hidden
    // from non-owner community users — same gate as the home tiles.
    if (entry[4] === 'owner' && !isOwner) continue;
    const pageId = entry[0], label = entry[1];
    const emoji = entry[2] || '▸', desc = entry[3] || '';
    html += '<div class=home-cluster-tile onclick="clusterNav(\\'' + pageId + '\\')">' +
              '<div class=ico style="font-size:26px">' + emoji + '</div>' +
              '<div class=meta><div class=name>' + esc(label) + '</div>' +
              (desc ? '<div class=desc>' + esc(desc) + '</div>' : '') +
            '</div></div>';
  }
  root.innerHTML = html || '<div class=empty>Nothing here for your account.</div>';
}

function clusterHubBack() {
  // Prefer the history stack (avoids a redundant push); fall back to Home
  // for web/desktop where the Telegram BackButton + stack may be absent.
  if (_pageHistory.length) popHistory();
  else goto('home');
}

function clusterNavHome() {
  // Sidebar Home icon: close any open flyout, then navigate to home.
  clusterClose();
  goto('home');
}

// ── Phase-1 cohesive home: content rows + first-run welcome ──────────────────
let _homeRowsLoaded = false, _homeClickBound = false;

function maybeShowWelcome() {
  try {
    if (localStorage.getItem('smdl_welcomed')) return;
    const w = document.getElementById('welcome-scrim');
    if (w) w.classList.add('show');
  } catch (e) {}
}
function dismissWelcome() {
  try { localStorage.setItem('smdl_welcomed', '1'); } catch (e) {}
  const w = document.getElementById('welcome-scrim');
  if (w) w.classList.remove('show');
}

function _homeCard(c) {
  const bg = (c.logo || c.poster || '').replace(/['"]/g, '');
  const cls = c.logo ? 'home-card-logo' : 'home-card-poster';
  const img = bg
    ? `<div class="${cls}" style="background-image:url('${bg}')"></div>`
    : `<div class="home-card-poster home-card-blank">${esc((c.label || '?').slice(0, 1))}</div>`;
  const attrs = c.act === 'play'
    ? `data-act=play data-id="${esc(String(c.id))}"`
    : `data-act=page data-page="${esc(c.page || 'home')}"`;
  return `<div class=home-card ${attrs}>${img}`
       + `<div class=home-card-label>${esc(c.label || '')}</div>`
       + (c.sub ? `<div class=home-card-sub>${esc(c.sub)}</div>` : '')
       + `</div>`;
}
function _homeRowShell(title) {
  const sec = document.createElement('div');
  sec.className = 'home-row';
  sec.innerHTML = `<div class=home-row-title>${esc(title)}</div><div class=home-row-scroll></div>`;
  document.getElementById('home-rows').appendChild(sec);
  return sec.querySelector('.home-row-scroll');
}
function _homeEmptyRow(title, msg, action, label) {
  const sec = document.createElement('div');
  sec.className = 'home-row';
  sec.innerHTML = `<div class=home-row-title>${esc(title)}</div>`
    + `<div class=home-row-empty><span>${esc(msg)}</span>`
    + `<button onclick="${action}">${esc(label)}</button></div>`;
  document.getElementById('home-rows').appendChild(sec);
}
async function _homeRow(title, url, mapFn, emptyMsg, action, label) {
  try {
    const r = await fetch(url, { headers: { 'X-Init-Data': initData } });
    const cards = mapFn(await r.json()) || [];
    if (!cards.length) { _homeEmptyRow(title, emptyMsg, action, label); return; }
    _homeRowShell(title).innerHTML = cards.map(_homeCard).join('');
  } catch (e) { _homeEmptyRow(title, emptyMsg, action, label); }
}
async function _homeForYou() {
  try {
    const r = await fetch('/api/iptv/for_you', { headers: { 'X-Init-Data': initData } });
    const rows = (await r.json()).rows || [];
    if (!rows.length) { _homeEmptyRow('Trending TV near you', "Watch a few channels and we'll tailor this.", "clusterEnter('watch')", '▶ Browse Live TV'); return; }
    rows.slice(0, 2).forEach(row => {
      _homeRowShell(row.title || 'Trending TV').innerHTML =
        (row.channels || []).map(ch => _homeCard(
          { label: ch.name, sub: ch.country || '', logo: ch.logo, act: 'play', id: ch.id })).join('');
    });
  } catch (e) { _homeEmptyRow('Trending TV near you', 'Live TV suggestions will appear here.', "clusterEnter('watch')", '▶ Browse Live TV'); }
}
async function loadHomeRows() {
  maybeShowWelcome();
  const host = document.getElementById('home-rows');
  if (!host) return;
  if (!_homeClickBound) {
    _homeClickBound = true;
    host.addEventListener('click', e => {
      const card = e.target.closest('.home-card');
      if (!card) return;
      if (card.dataset.act === 'play') location.href = '/iptv/play/' + encodeURIComponent(card.dataset.id);
      else goto(card.dataset.page || 'home');
    });
  }
  if (_homeRowsLoaded) return;
  _homeRowsLoaded = true;
  host.innerHTML = '';
  // Trending FIRST — works for everyone (cross-user, with a curated fallback),
  // so a brand-new user with no history still has real discovery on open.
  _homeRow('🔥 Trending now', '/api/iptv/trending?limit=15',
    d => (d.items || []).map(it => ({ label: it.name, sub: it.country || '', logo: it.logo, act: 'play', id: it.id })),
    'Live TV is warming up…', "clusterEnter('watch')", '▶ Browse Live TV');
  _homeRow('Continue watching', '/api/iptv/last_watched?limit=10',
    d => (d.items || []).map(it => ({ label: it.name || ('Channel ' + it.channel_id), sub: 'Live TV', logo: it.logo, act: 'play', id: it.channel_id })),
    'No history yet.', "clusterEnter('watch')", '▶ Browse Live TV');
  _homeForYou();
  _homeRow('New in your library', '/api/miniapp/library?kind=all',
    d => (d.items || []).slice(0, 12).map(it => ({ label: it.title || it.name || it.filename || 'Item', sub: it.kind || '', poster: it.poster || it.thumb, act: 'page', page: 'library' })),
    'Your library is empty.', "clusterEnter('get')", '📥 Get something');
  _homeRow('Your sticker packs', '/api/sticker_packs',
    d => (d.packs || []).slice(0, 12).map(p => ({ label: p.title || p.name || 'Pack', sub: (p.count != null ? p.count + ' stickers' : ''), poster: p.thumb, act: 'page', page: 'stickers' })),
    'No packs yet.', "clusterEnter('make')", '🎨 Make a sticker');
}

function clusterOpen(key) {
  // Sidebar cluster icon: toggle the flyout sub-sidebar. Doesn't
  // navigate — user picks the sub-page from the flyout.
  if (_openCluster === key) { clusterClose(); return; }
  _openCluster = key;
  _renderSubsidebar(key);
  document.getElementById('subsidebar').classList.add('show');
  document.querySelectorAll('.sidebar-item').forEach(el => el.classList.remove('expanded'));
  const el = document.getElementById('nav-cluster-' + key);
  if (el) el.classList.add('expanded');
}

function clusterClose() {
  if (!_openCluster) return;
  _openCluster = null;
  document.getElementById('subsidebar').classList.remove('show');
  document.querySelectorAll('.sidebar-item').forEach(el => el.classList.remove('expanded'));
}

function _renderSubsidebar(key) {
  const cluster = _CLUSTERS[key];
  const root = document.getElementById('subsidebar');
  if (!cluster) { root.innerHTML = ''; return; }
  let html = '<div class=subsidebar-header>' + esc(cluster.label) + '</div>';
  for (const entry of cluster.pages) {
    const pageId = entry[0], label = entry[1];
    const cls = pageId === current ? 'subsidebar-item current' : 'subsidebar-item';
    // Sub-sidebar entries close the flyout on tap so the user sees the
    // page immediately. clusterNav() centralises that.
    html += '<div class="' + cls + '" onclick="clusterNav(\\'' + pageId + '\\')">' + esc(label) + '</div>';
  }
  root.innerHTML = html;
}

function clusterNav(pageId) {
  clusterClose();
  _clusterNavigate(pageId);
}

// Click outside the sidebar/subsidebar closes the flyout.
document.addEventListener('click', (e) => {
  if (!_openCluster) return;
  const sidebar = document.querySelector('.sidebar');
  const subsidebar = document.getElementById('subsidebar');
  if (sidebar && sidebar.contains(e.target)) return;
  if (subsidebar && subsidebar.contains(e.target)) return;
  clusterClose();
});

function goto(page) {
  if (page !== current) pushHistory(current);
  current = page;
  document.querySelectorAll('.page').forEach(p => p.classList.toggle('active', p.id === 'page-'+page));
  // Mark the sidebar entry active. Map page name → element id; 'live'
  // never lands here because it navigates away via location.href, so
  // we never light up nav-live from this function.
  // After the cluster reorg: every sub-page maps to its CLUSTER icon in
  // the main sidebar. Home is the only page with its own dedicated icon.
  // _PAGE_TO_CLUSTER is built once from _CLUSTERS above.
  const targetId = page === 'home'
    ? 'nav-home'
    : page === 'cluster'
      ? ('nav-cluster-' + (_clusterHubKey || ''))
      : ('nav-cluster-' + (_PAGE_TO_CLUSTER[page] || ''));
  document.querySelectorAll('.sidebar-item').forEach(el =>
    el.classList.toggle('active', el.id === targetId));
  // Bottom-nav active sync (mirrors the sidebar→cluster mapping).
  const _tabKey = page === 'home' ? 'home'
    : page === 'cluster' ? (_clusterHubKey || '')
    : (_PAGE_TO_CLUSTER[page] || '');
  document.querySelectorAll('.bn-item').forEach(b =>
    b.classList.toggle('active', b.dataset.tab === _tabKey));
  // If the sub-sidebar flyout is open while we navigate, refresh its
  // "current" highlight so the active page is marked.
  if (_openCluster) _renderSubsidebar(_openCluster);
  // Home is the cluster-tile launcher. The discovery rows (loadHomeRows) are
  // dormant — re-enable by restoring the call here if we want a hybrid home.
  if (page === 'downloads') loadDownloads();
  else if (page === 'search') { const si = document.getElementById('search-input'); if (si) si.focus(); }
  else if (page === 'notifications') loadNotifications();
  else if (page === 'library') loadLibrary(libKind);
  else if (page === 'watchlist') loadWatchlist();
  else if (page === 'files') loadFiles(filesCwd);
  else if (page === 'scraper') loadScraper();
  else if (page === 'settings') loadSettings();
  else if (page === 'admin') loadAdmin();
  else if (page === 'stickers') loadStickers();
  else if (page === 'streamer') loadStreamer();

  // Watchlist auto-refresh so an in-progress recording's size + duration
  // tick up and a streamer going LIVE flips colour without manual reload.
  if (watchlistTimer) { clearInterval(watchlistTimer); watchlistTimer = null; }
  if (page === 'watchlist') watchlistTimer = setInterval(loadWatchlist, 5000);
}

const NOTIF_ICONS = {
  download:  { e: '⬇', c: 'var(--button)' },
  recording: { e: '⏺', c: '#e85' },
  auth:      { e: '👤', c: '#7c9' },
};
const NOTIF_STATUS_COLOR = {
  finished: '#5b8', failed: '#d66', cancelled: '#b98', recording: '#e85',
  queued: 'var(--muted)', approve: '#5b8', approve_by_code: '#5b8',
  deny: '#d66', revoke: '#d66', restore: '#5b8',
};

function setNotifBadge(n) {
  const txt = n > 99 ? '99+' : String(n);
  ['notif-badge', 'notif-badge-nav'].forEach(id => {
    const b = document.getElementById(id);
    if (!b) return;
    if (n > 0) { b.textContent = txt; b.style.display = ''; }
    else b.style.display = 'none';
  });
  // Bottom-nav Inbox tab carries a dot (no count) so the tab bar stays clean.
  const dot = document.getElementById('bn-inbox-badge');
  if (dot) dot.style.display = n > 0 ? 'block' : 'none';
}

async function refreshNotifBadge() {
  try {
    const j = await api('/api/miniapp/notifications?limit=40');
    setNotifBadge(j.unread || 0);
  } catch (e) { /* badge is best-effort */ }
}

async function loadNotifications() {
  const root = document.getElementById('notifications-list');
  try {
    const j = await api('/api/miniapp/notifications?limit=40');
    if (!j.items.length) {
      root.innerHTML = '<div class=empty>No activity yet.</div>';
    } else {
      root.innerHTML = j.items.map(it => {
        const ic = NOTIF_ICONS[it.type] || { e: '•', c: 'var(--muted)' };
        const sc = NOTIF_STATUS_COLOR[it.status] || 'var(--muted)';
        const fresh = (it.ts && (!j.seen_at || it.ts > j.seen_at)) ? ' notif-new' : '';
        return `
          <div class="notif-row${fresh}">
            <div class=notif-ico style="color:${ic.c}">${ic.e}</div>
            <div class=notif-body>
              <div class=notif-title>${esc(it.title || '')}</div>
              <div class=notif-sub>${esc(it.subtitle || '')}</div>
            </div>
            <div class=notif-meta>
              ${it.status ? `<span class=notif-status style="color:${sc}">${esc(it.status)}</span>` : ''}
              <span class=notif-when>${timeago(it.ts)}</span>
            </div>
          </div>`;
      }).join('');
    }
    // Opening the feed clears unread — mark seen, then drop the badge.
    try { await api('/api/miniapp/notifications/seen', { method: 'POST' }); } catch (e) {}
    setNotifBadge(0);
  } catch (e) { showErr('Load failed: ' + e); }
}

let _searchTimer = null;
let _searchSeq = 0;
function onSearchInput() {
  const q = (document.getElementById('search-input').value || '').trim();
  if (_searchTimer) clearTimeout(_searchTimer);
  const root = document.getElementById('search-results');
  if (q.length < 2) {
    root.innerHTML = '<div class=empty>Type at least 2 characters to search.</div>';
    return;
  }
  _searchTimer = setTimeout(() => runSearch(q), 300);
}

const SEARCH_GROUP_META = {
  channels:  { label: 'IPTV channels', icon: '📺' },
  theater:   { label: 'Theater',       icon: '🎬' },
  downloads: { label: 'Your downloads',icon: '⬇' },
  watchlist: { label: 'Streams',       icon: '🔴' },
};

async function runSearch(q) {
  const seq = ++_searchSeq;
  const root = document.getElementById('search-results');
  root.innerHTML = '<div class=empty><span class=spin></span> Searching…</div>';
  let j;
  try {
    j = await api('/api/miniapp/search?q=' + encodeURIComponent(q) + '&limit=8');
  } catch (e) { if (seq === _searchSeq) showErr('Search failed: ' + e); return; }
  if (seq !== _searchSeq) return;   // a newer query already superseded this one
  const groups = j.groups || {};
  if (!j.total) {
    root.innerHTML = '<div class=empty>No results for “' + esc(q) + '”.</div>';
    return;
  }
  let html = '';
  for (const key of ['channels', 'theater', 'downloads', 'watchlist']) {
    const items = groups[key] || [];
    if (!items.length) continue;
    const meta = SEARCH_GROUP_META[key];
    html += '<div class=search-group><div class=search-group-head>' +
            meta.icon + ' ' + meta.label + ' · ' + items.length + '</div>';
    html += items.map(it => renderSearchItem(key, it)).join('');
    html += '</div>';
  }
  root.innerHTML = html;
}

function renderSearchItem(kind, it) {
  if (kind === 'channels') {
    const cc = it.country ? ('<span class=search-tag>' + esc(it.country) + '</span>') : '';
    return '<div class=search-row onclick="location.href=\\'/iptv/play/' +
      encodeURIComponent(it.id) + '\\'">' +
      '<div class=search-title>' + esc(it.name || it.id) + '</div>' +
      '<div class=search-sub>' + cc + '</div></div>';
  }
  if (kind === 'theater') {
    const yr = it.year ? (' · ' + esc(String(it.year))) : '';
    const rt = it.imdb_rating ? (' · ★ ' + esc(String(it.imdb_rating))) : '';
    return '<div class=search-row onclick="location.href=\\'/app/stremio\\'">' +
      '<div class=search-title>' + esc(it.name || '') + '</div>' +
      '<div class=search-sub>' + esc(it.type || '') + yr + rt + '</div></div>';
  }
  if (kind === 'downloads') {
    const u = encodeURIComponent(it.url || '');
    return '<div class=search-row onclick="openExternal(\\'' + u + '\\')">' +
      '<div class=search-title>@' + esc(it.uploader || 'unknown') + '</div>' +
      '<div class=search-sub>' + esc(it.file || it.url || '') + '</div></div>';
  }
  if (kind === 'watchlist') {
    return '<div class=search-row onclick="goto(\\'watchlist\\')">' +
      '<div class=search-title>' + esc(it.username || it.url || '') + '</div>' +
      '<div class=search-sub>' + esc(it.platform || '') + '</div></div>';
  }
  return '';
}

// ── Library / media-server index (#133) ────────────────────────────────
let libKind = 'all';
let _libItems = [];          // accumulated across "load more" pages
let _libOffset = 0;
let _libTotal = 0;
let _libBusy = false;
const _LIB_PAGE = 120;
const LIB_KIND_META = {
  all:   { label: 'All',    ico: '🗂' },
  video: { label: 'Videos', ico: '🎬' },
  audio: { label: 'Audio',  ico: '🎵' },
  image: { label: 'Images', ico: '🖼' },
};
const LIB_THUMBABLE = { video: true, image: true, audio: false };

function renderLibTabs(summary) {
  const tabs = document.getElementById('lib-tabs');
  if (!tabs) return;
  const counts = { all: 0 };
  for (const k of ['video', 'audio', 'image']) {
    const c = (summary && summary[k] && summary[k].count) || 0;
    counts[k] = c; counts.all += c;
  }
  tabs.innerHTML = ['all', 'video', 'audio', 'image'].map(k => {
    const m = LIB_KIND_META[k];
    const cls = 'lib-tab' + (k === libKind ? ' active' : '');
    return '<div class="' + cls + '" onclick="setLibKind(\\'' + k + '\\')">' +
           m.ico + ' ' + m.label + ' · ' + counts[k] + '</div>';
  }).join('');
}

function setLibKind(k) {
  if (k === libKind) return;
  libKind = k;
  loadLibrary(k);
}

function renderLibCard(it) {
  const thumbable = LIB_THUMBABLE[it.kind];
  const m = LIB_KIND_META[it.kind] || { ico: '📄' };
  let inner;
  if (thumbable) {
    inner = '<img loading=lazy src="/api/miniapp/files/thumb?path=' +
            encodeURIComponent(it.path) + '" ' +
            'onerror="this.replaceWith(document.createTextNode(\\'' + m.ico + '\\'))">';
  } else {
    inner = m.ico;
  }
  const u = encodeURIComponent(it.share_url || '');
  const n = encodeURIComponent(it.name || '');
  const click = it.share_url
    ? 'openPreview(\\'' + u + '\\', \\'' + n + '\\')'
    : "showErr('No share URL — SHARE_SECRET/PUBLIC_BASE_URL not configured')";
  return '<div class=lib-card onclick="' + click + '">' +
    '<div class=lib-thumb>' + inner +
      '<span class=lib-kind-tag>' + esc(it.ext || '') + '</span></div>' +
    '<div class=lib-body><div class=lib-name>' + esc(it.name) + '</div>' +
    '<div class=lib-meta>' + fmtSize(it.size) + ' · ' + fmtDate(it.mtime) + '</div>' +
    '</div></div>';
}

async function loadLibrary(kind, force) {
  if (_libBusy) return;
  _libBusy = true;
  libKind = kind || 'all';
  _libOffset = 0;
  _libItems = [];
  const grid = document.getElementById('library-grid');
  const more = document.getElementById('library-more');
  if (more) more.innerHTML = '';
  grid.innerHTML = '<div class=empty><span class=spin></span> Scanning…</div>';
  try {
    const j = await api('/api/miniapp/library?kind=' + encodeURIComponent(libKind) +
                        '&limit=' + _LIB_PAGE + '&offset=0');
    renderLibTabs(j.summary);
    _libItems = j.items || [];
    _libOffset = (j.offset || 0) + _libItems.length;
    _libTotal = j.total || 0;
    if (!_libItems.length) {
      grid.innerHTML = '<div class=empty>Nothing cached in this category yet.</div>';
    } else {
      grid.innerHTML = '<div class=lib-grid>' +
        _libItems.map(renderLibCard).join('') + '</div>';
    }
    renderLibMore();
  } catch (e) {
    grid.innerHTML = '<div class=empty>Library failed to load: ' + esc(String(e)) + '</div>';
  } finally {
    _libBusy = false;
  }
}

function renderLibMore() {
  const more = document.getElementById('library-more');
  if (!more) return;
  if (_libOffset < _libTotal) {
    more.innerHTML = '<button class="small sec" onclick="loadMoreLibrary()">Load more · ' +
      (_libTotal - _libOffset) + ' left</button>';
  } else {
    more.innerHTML = _libTotal ? '<div class=meta>' + _libTotal + ' items</div>' : '';
  }
}

async function loadMoreLibrary() {
  if (_libBusy || _libOffset >= _libTotal) return;
  _libBusy = true;
  const more = document.getElementById('library-more');
  if (more) more.innerHTML = '<span class=spin></span>';
  try {
    const j = await api('/api/miniapp/library?kind=' + encodeURIComponent(libKind) +
                        '&limit=' + _LIB_PAGE + '&offset=' + _libOffset);
    const items = j.items || [];
    _libItems = _libItems.concat(items);
    _libOffset += items.length;
    _libTotal = j.total || _libTotal;
    const gridInner = document.querySelector('#library-grid .lib-grid');
    if (gridInner) gridInner.insertAdjacentHTML('beforeend', items.map(renderLibCard).join(''));
    renderLibMore();
  } catch (e) {
    if (more) more.innerHTML = '<div class=meta>Load more failed: ' + esc(String(e)) + '</div>';
  } finally {
    _libBusy = false;
  }
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
      const redeliver = d.id != null
        ? `<button class=redeliver onclick="redeliverDownload(${d.id}, this)">Re-deliver</button>`
        : '';
      // Sticker shortcut — only on rows where the first file looks
      // sticker-eligible (video / GIF / still image). Extension sniff is
      // cheap and avoids surfacing a button for audio-only downloads.
      const firstFile = (d.files || [])[0] || '';
      const ext = firstFile.toLowerCase().match(/\\.([a-z0-9]+)$/);
      const stickerable = ext && ['mp4','mov','webm','mkv','gif','jpg','jpeg','png','webp'].includes(ext[1]);
      const stickerBtn = stickerable
        ? `<button class=redeliver style="background:#444" onclick="stickersFromDownloadPath('${encodeURIComponent(firstFile)}', this)" title="Make sticker from this">🎬</button>`
        : '';
      return `
        <div class=dl-row>
          <a onclick="openExternal('${u}')">
            <div class=user>@${esc(user)}</div>
            <div class=desc>${esc(desc || url)}</div>
            <div class=when>${timeago(d.downloaded_at || d.created_at)}</div>
          </a>
          ${stickerBtn}
          ${redeliver}
        </div>`;
    }).join('');
  } catch(e) { showErr('Load failed: '+e); }
}

async function stickersFromDownloadPath(encPath, btn) {
  const file_path = decodeURIComponent(encPath);
  if (btn) { btn.disabled = true; btn.textContent = '…'; }
  try {
    const r = await api('/api/sticker_drafts/from_download', {
      method: 'POST',
      body: JSON.stringify({ file_path }),
    });
    showOk('Draft created — opening editor');
    location.href = '/stickers/' + r.id + '/edit?kind=video';
  } catch (e) {
    showErr('Make sticker failed: ' + e);
    if (btn) { btn.disabled = false; btn.textContent = '🎬'; }
  }
}

// ── Stickers tab ──────────────────────────────────────────────────────────
// The drafts list used to live on a standalone /stickers page; folded into
// /app 2026-06-01 so the four community surfaces share one Mini App. The
// canvas editor at /stickers/{id}/edit stays a sub-page (different layout).

function _fmtStickerDur(n) {
  if (!n) return '—';
  return Number(n).toFixed(1) + 's';
}

function _fmtStickerExpires(iso) {
  if (!iso) return '';
  const d = new Date(iso); const now = new Date();
  const mins = Math.max(0, Math.round((d - now) / 60000));
  if (mins < 60) return `expires in ${mins}m`;
  const hrs = Math.floor(mins / 60); const rest = mins % 60;
  return `expires in ${hrs}h${rest}m`;
}

let _stickersPackUrl = '';   // remembered between renders so click-to-copy + rename work
let _stickersPackName = '';
let _stickersPackTitle = '';
let _stickersUploadWired = false;
// Active pack kind across all sticker-tab calls. Switching this re-fetches
// the pack + contents but doesn't touch the drafts list (drafts are
// pack-kind-agnostic; the destination is chosen at /make time).
let _stickersKind = 'video';

function stickersSwitchKind(k) {
  if (!['video','static','custom_emoji'].includes(k)) return;
  _stickersKind = k;
  // Active-state the segmented buttons.
  document.querySelectorAll('#page-stickers button[data-kind]').forEach(b => {
    b.style.background = b.dataset.kind === k ? 'var(--button)' : '';
    b.style.color      = b.dataset.kind === k ? 'var(--button-text)' : '';
  });
  // Hint on the upload card so dropping a still picks the right destination
  // even before the file is probed.
  const dz = document.getElementById('stickers-dropzone');
  if (dz) {
    const hint = dz.querySelector('.meta');
    if (hint) hint.textContent = (k === 'static')
      ? 'Image (PNG/JPG/GIF) → static sticker · 512×512 · ≤ 512 KB'
      : (k === 'custom_emoji')
        ? 'Video/image → custom emoji · 100×100 · ≤ 64 KB'
        : 'Videos / GIFs, ≤ 50 MB each. Drop multiple at once.';
  }
  loadStickers();
}

// ── Streamer tab (Twitch sign-in + recording-consent dashboard) ───────────
// The signed-in identity is whatever the Mini App resolved (TG initData,
// Google OIDC, or the user_id from the v2 session cookie). To set
// recording consent on a Twitch channel, the caller MUST have a Twitch
// session — proves they control that Twitch user_id. We trial-fetch
// /api/streamer/me; 401 means no Twitch link yet → show the sign-in CTA.

async function loadStreamer() {
  const root = document.getElementById('streamer-content');
  root.innerHTML = '<div class=empty><span class=spin></span> Loading…</div>';
  // Direct fetch instead of api() — api() drops the HTTP status code into
  // a single string ("twitch_signin_required") and we lose the ability to
  // distinguish 401 (no Twitch session yet → show sign-in CTA) from 4xx/5xx
  // (real error → show the message). The streamer tab MUST paint the
  // sign-in branch on 401.
  let r;
  try {
    r = await fetch('/api/streamer/me', { headers: { 'X-Init-Data': initData } });
  } catch (e) {
    root.innerHTML = '<div class=empty style="color:#e88">Network error: ' + esc(String(e)) + '</div>';
    return;
  }
  if (r.status === 401) {
    // 401 here means EITHER (a) no v2 session cookie at all OR (b) we have
    // a session but it's not Twitch — both end at the sign-in CTA.
    _renderTwitchSignIn(root);
    return;
  }
  if (!r.ok) {
    let detail = '';
    try { detail = (await r.json()).detail || ''; }
    catch { detail = await r.text().catch(() => '') || ''; }
    root.innerHTML = '<div class=empty style="color:#e88">Load failed (' + r.status + '): ' + esc(detail) + '</div>';
    return;
  }
  let data = null;
  try { data = await r.json(); }
  catch (e) {
    root.innerHTML = '<div class=empty style="color:#e88">Bad response: ' + esc(String(e)) + '</div>';
    return;
  }
  _renderStreamerDashboard(root, data);
}

function _renderTwitchSignIn(root) {
  // Round-trip to the consent dashboard after sign-in.
  const next = encodeURIComponent('/app?tab=streamer');
  root.innerHTML =
    '<div class=card style="padding:20px;text-align:center">' +
      '<div style="font-size:46px;line-height:1;margin-bottom:8px">📺</div>' +
      '<div style="font-weight:600;font-size:16px;margin-bottom:4px">Sign in with Twitch</div>' +
      '<div class=meta style="font-size:12px;color:var(--muted);margin-bottom:14px">' +
        'Twitch confirms you own your channel so we can attach your consent to your authentic identity. We only ask for basic profile (login, display name, email).' +
      '</div>' +
      '<a id=tw-signin class=btn style="display:inline-block;padding:10px 18px;background:#9146FF;color:white;border-radius:8px;text-decoration:none;font-weight:600">' +
        '🟣 Sign in with Twitch' +
      '</a>' +
      '<div class=meta style="margin-top:14px;font-size:11px;color:var(--muted)">' +
        'You can revoke consent at any time. Existing recordings made under a prior grant are not affected; revoke only blocks future records.' +
      '</div>' +
    '</div>';
  const a = document.getElementById('tw-signin');
  if (a) a.href = '/auth/twitch/start?next=' + next;
}

function _renderStreamerDashboard(root, data) {
  const ident = data.identity || {};
  const consent = data.consent || null;
  const has = consent && consent.allow_recording && !consent.revoked;
  // Identity card.
  let html = '<div class=card style="display:flex;gap:12px;align-items:center;margin-bottom:12px">';
  if (ident.profile_image_url) {
    html += '<img src="' + esc(ident.profile_image_url) + '" alt="" style="width:54px;height:54px;border-radius:50%;object-fit:cover">';
  }
  html += '<div style="flex:1">' +
            '<div style="font-weight:600;font-size:15px">' + esc(ident.twitch_display || ident.twitch_login) + '</div>' +
            '<div class=meta style="font-size:12px;color:var(--muted)">@' + esc(ident.twitch_login || '') +
              (ident.broadcaster_type ? (' · <span style="color:#9146FF">' + esc(ident.broadcaster_type) + '</span>') : '') +
            '</div>' +
          '</div>' +
          '<button class=sec onclick=streamerSignOut() style="font-size:11px;align-self:flex-start">Sign out</button>' +
          '</div>';
  // Consent toggle card.
  html += '<div class=card style="margin-bottom:12px">' +
            '<div style="display:flex;align-items:center;gap:10px;margin-bottom:10px">' +
              '<span style="font-weight:600;flex:1">Allow community recording</span>' +
              '<label class=switch style="position:relative;display:inline-block;width:42px;height:22px;cursor:pointer">' +
                '<input type=checkbox id=streamer-allow ' + (has ? 'checked' : '') + ' style="opacity:0;width:0;height:0">' +
                '<span id=streamer-allow-slider style="position:absolute;inset:0;background:' + (has ? '#284' : '#444') + ';border-radius:22px;transition:background .15s"></span>' +
                '<span style="position:absolute;top:3px;left:' + (has ? '23px' : '3px') + ';width:16px;height:16px;background:white;border-radius:50%;transition:left .15s" id=streamer-allow-knob></span>' +
              '</label>' +
            '</div>' +
            '<div class=field style="font-size:12px;color:var(--muted);margin-bottom:6px">Max recording duration per job (1 – 720 minutes)</div>' +
            '<input id=streamer-max-dur type=number min=1 max=720 value=' + ((consent && consent.max_duration_min) || 240) + ' style="padding:6px 8px;border-radius:6px;border:1px solid var(--separator);background:var(--surface);color:var(--fg);width:120px;font-size:13px">' +
            '<div class=field style="font-size:12px;color:var(--muted);margin-top:10px;margin-bottom:6px">Who can record</div>' +
            '<div style="display:flex;gap:8px;flex-wrap:wrap">' +
              '<label style="display:flex;align-items:center;gap:6px;font-size:13px;cursor:pointer"><input type=radio name=streamer-who value=all ' + ((!consent || consent.allow_all_users) ? 'checked' : '') + '> Anyone signed in</label>' +
              '<label style="display:flex;align-items:center;gap:6px;font-size:13px;cursor:pointer"><input type=radio name=streamer-who value=list ' + ((consent && !consent.allow_all_users) ? 'checked' : '') + '> Only specific users</label>' +
            '</div>' +
            '<div id=streamer-list-wrap style="margin-top:8px;display:' + ((consent && !consent.allow_all_users) ? 'block' : 'none') + '">' +
              '<input id=streamer-list type=text placeholder="comma-separated session ids, e.g. twitch:12345, 898259417" value="' + esc(((consent && (consent.allowed_users || [])).join(', ')) || '') + '" style="width:100%;padding:6px 8px;border-radius:6px;border:1px solid var(--separator);background:var(--surface);color:var(--fg);font-size:12px">' +
            '</div>' +
            '<div class=field style="font-size:12px;color:var(--muted);margin-top:10px;margin-bottom:6px">Notes shown to recorders (optional, ≤ 512 chars)</div>' +
            '<textarea id=streamer-notes rows=3 placeholder="e.g. OK for personal archives, no re-uploads please" style="width:100%;padding:6px 8px;border-radius:6px;border:1px solid var(--separator);background:var(--surface);color:var(--fg);font-size:13px;resize:vertical">' + esc((consent && consent.notes) || '') + '</textarea>' +
            '<div style="display:flex;gap:6px;margin-top:12px;align-items:center">' +
              '<button onclick=streamerSaveConsent() id=streamer-save>💾 Save</button>' +
              '<button class=sec onclick=streamerRevoke() style="color:#e88">🚫 Revoke consent</button>' +
              '<span style="flex:1"></span>' +
              '<span id=streamer-save-status class=meta style="font-size:11px"></span>' +
            '</div>' +
          '</div>';
  // Audit list of recordings.
  html += '<h2 style="margin:18px 4px 8px;font-size:15px;color:var(--muted);font-weight:600">Recent recordings of your channel</h2>' +
          '<div id=streamer-recordings><div class=empty>Loading…</div></div>';
  root.innerHTML = html;
  // Wire the "who" radio toggle.
  document.querySelectorAll('input[name=streamer-who]').forEach(r => r.addEventListener('change', () => {
    document.getElementById('streamer-list-wrap').style.display =
      r.value === 'list' && r.checked ? 'block' : (r.value === 'list' ? 'none' : 'none');
    document.getElementById('streamer-list-wrap').style.display =
      (document.querySelector('input[name=streamer-who]:checked') || {}).value === 'list' ? 'block' : 'none';
  }));
  // Visual slider state — toggle the slider + knob when the checkbox flips.
  const allow = document.getElementById('streamer-allow');
  if (allow) allow.addEventListener('change', () => {
    const on = allow.checked;
    document.getElementById('streamer-allow-slider').style.background = on ? '#284' : '#444';
    document.getElementById('streamer-allow-knob').style.left = on ? '23px' : '3px';
  });
  // Audit list — fire async, don't block the dashboard paint.
  _loadStreamerRecordings();
}

async function _loadStreamerRecordings() {
  const wrap = document.getElementById('streamer-recordings');
  if (!wrap) return;
  try {
    const r = await api('/api/streamer/recordings');
    const list = r.recordings || [];
    if (!list.length) {
      wrap.innerHTML = '<div class=empty>Nobody has recorded your channel yet.</div>';
      return;
    }
    wrap.innerHTML = list.map(rec =>
      '<div class=card style="padding:8px;margin-bottom:6px;display:flex;align-items:center;gap:8px">' +
        '<div style="flex:1;min-width:0">' +
          '<div style="font-size:13px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">' + esc(rec.url || '') + '</div>' +
          '<div class=meta style="font-size:11px;color:var(--muted)">recorded ' + timeago(rec.downloaded_at || '') + ' · by chat_id ' + esc(String(rec.chat_id || '?')) + '</div>' +
        '</div>' +
      '</div>'
    ).join('');
  } catch (e) {
    wrap.innerHTML = '<div class=empty style="color:#e88">Couldn\\'t load history: ' + esc(String(e)) + '</div>';
  }
}

async function streamerSaveConsent() {
  const btn = document.getElementById('streamer-save');
  const status = document.getElementById('streamer-save-status');
  const allow = document.getElementById('streamer-allow').checked;
  const max = parseInt(document.getElementById('streamer-max-dur').value || '240', 10);
  const who = (document.querySelector('input[name=streamer-who]:checked') || {}).value || 'all';
  const list = (document.getElementById('streamer-list').value || '').split(',').map(s => s.trim()).filter(Boolean);
  const notes = (document.getElementById('streamer-notes').value || '').trim();
  if (max < 1 || max > 720) { showErr('Duration must be 1 – 720 minutes'); return; }
  btn.disabled = true; status.textContent = 'saving…';
  try {
    await api('/api/streamer/consent', { method: 'POST',
      body: JSON.stringify({
        allow_recording: allow,
        max_duration_min: max,
        allow_all_users: who === 'all',
        allowed_users: list,
        notes: notes || null,
      }) });
    status.textContent = '✓ saved';
    setTimeout(() => { status.textContent = ''; }, 2000);
  } catch (e) {
    status.textContent = '';
    showErr('Save failed: ' + e);
  } finally { btn.disabled = false; }
}

async function streamerRevoke() {
  if (!confirm('Revoke your recording consent? Future recordings will be blocked. Existing recordings are unaffected.')) return;
  try {
    await api('/api/streamer/revoke', { method: 'POST', body: '{}' });
    showOk('Consent revoked');
    loadStreamer();
  } catch (e) { showErr('Revoke failed: ' + e); }
}

async function streamerSignOut() {
  if (!confirm('Sign out of your Twitch session on this Mini App?')) return;
  try {
    await fetch('/auth/twitch/signout', { method: 'POST', headers: { 'X-Init-Data': initData } });
    showOk('Signed out');
    loadStreamer();
  } catch (e) { showErr('Sign-out failed: ' + e); }
}

// Sticker Maker top-nav: switch the active section (remembered per device).
function stkSection(sec) {
  if (sec === 'pack') sec = 'home';   // migrate old saved/deep-linked section
  const valid = ['home', 'add', 'stickers'];
  if (!valid.includes(sec)) sec = 'home';
  document.querySelectorAll('#page-stickers .stk-sec').forEach(s =>
    s.classList.toggle('active', s.dataset.section === sec));
  document.querySelectorAll('#stk-nav button').forEach(b =>
    b.classList.toggle('on', b.dataset.sec === sec));
  try { localStorage.setItem('smdl_stk_section', sec); } catch (e) {}
  // Lazy-load trending GIFs the first time the Add section is opened.
  if (sec === 'add' && !_stkGifInit) { _stkGifInit = true; _stkGifSyncPills(); stkGifSearch(); }
}

// ── GIF library (Giphy / Tenor) → sticker ────────────────────────────────
// Search a provider, tap a result, and from_url fetches it into a draft that
// flows through the normal make pipeline (Instant mode auto-makes + DMs).
let _stkGifSource = 'giphy';
let _stkGifTimer = null;
let _stkGifInit = false;
function _stkGifSyncPills() {
  document.querySelectorAll('#page-stickers .pill[data-gifsrc]').forEach(el => {
    const on = el.dataset.gifsrc === _stkGifSource;
    el.style.background = on ? '#284' : '#222';
    el.style.borderColor = on ? '#284' : '#333';
    el.style.color = on ? '#dfd' : '#bbb';
  });
}
function stkGifSource(s) {
  _stkGifSource = (s === 'tenor') ? 'tenor' : 'giphy';
  _stkGifSyncPills();
  stkGifSearch();
}
function stkGifSearchDebounced() {
  clearTimeout(_stkGifTimer);
  _stkGifTimer = setTimeout(stkGifSearch, 350);
}
async function stkGifSearch() {
  const qEl = document.getElementById('stk-gif-q');
  const res = document.getElementById('stk-gif-results');
  const st  = document.getElementById('stk-gif-status');
  if (!res || !st) return;
  const q = (qEl && qEl.value || '').trim();
  const label = _stkGifSource === 'giphy' ? 'GIPHY' : 'Tenor';
  st.textContent = 'Searching ' + label + '…';
  res.innerHTML = '';
  try {
    const d = await api('/api/stickers/gif_search?source=' + _stkGifSource + '&q=' + encodeURIComponent(q));
    const items = d.items || [];
    if (!items.length) { st.textContent = 'No GIFs found — try another search.'; return; }
    st.textContent = label + ' · tap a GIF to add it';
    res.innerHTML = items.map(it =>
      `<div class=stk-gif-cell data-url="${esc(it.url)}" title="${esc(it.title || '')}" style="aspect-ratio:1/1;border-radius:8px;cursor:pointer;background:#0d0f14 center/cover no-repeat;background-image:url('${esc(it.preview)}');border:1px solid var(--separator)"></div>`).join('');
    res.querySelectorAll('.stk-gif-cell').forEach(el =>
      el.addEventListener('click', () => stkGifImport(el.dataset.url, el)));
  } catch (e) {
    const msg = ('' + e);
    if (/not configured|api key|GIPHY_API_KEY|TENOR_API_KEY|invalid|banned|rejected/i.test(msg)) {
      st.innerHTML = '⚠ ' + label + ' search needs an API key — the owner adds '
        + '<code>GIPHY_API_KEY</code> / <code>TENOR_API_KEY</code> to <code>.env.local</code> (both free).';
    } else {
      st.textContent = 'Search failed: ' + msg;
    }
  }
}
async function stkGifImport(url, el) {
  const st = document.getElementById('stk-gif-status');
  if (el) el.style.opacity = '.45';
  try {
    if (st) st.textContent = 'Importing GIF…';
    const d = await api('/api/sticker_drafts/from_url', { method: 'POST', body: JSON.stringify({ url }) });
    if (_stickersMode === 'instant') {
      if (st) st.textContent = '⚡ Converting & sending…';
      await _stickersInstantMake(d.id);
      try { await api('/api/sticker_drafts/' + d.id + '/delete', { method: 'POST', body: '{}' }); } catch (e) {}
      if (st) st.textContent = '✓ Added to your ' + _stickersKind + ' pack';
    } else {
      if (st) st.innerHTML = '✓ Draft added — see <b>Drafts</b> below to refine.';
      try { loadStickers(); } catch (e) {}
    }
  } catch (e) {
    if (st) st.textContent = '❌ ' + e;
  } finally {
    if (el) el.style.opacity = '';
  }
}

// ── v2.7-D — cross-pack library search + tag + trash/restore ────────────────
let _stkLibShowTrash = false;
let _stkLibTimer = null;
function stkLibSearchDebounced() {
  clearTimeout(_stkLibTimer);
  _stkLibTimer = setTimeout(stkLibSearch, 250);
}
function stkLibToggleTrash() {
  _stkLibShowTrash = !_stkLibShowTrash;
  const b = document.getElementById('stk-trash-toggle');
  if (b) b.classList.toggle('on', _stkLibShowTrash);
  stkLibSearch();
}
async function stkLibSearch() {
  const inp = document.getElementById('stk-search-input');
  const box = document.getElementById('stk-search-results');
  if (!box) return;
  const q = inp ? inp.value.trim() : '';
  if (!q && !_stkLibShowTrash) { box.innerHTML = ''; return; }
  box.innerHTML = '<div class=empty>Searching…</div>';
  let data;
  try {
    data = await api('/api/stickers/search?include_deleted=' +
      (_stkLibShowTrash ? '1' : '0') + '&q=' + encodeURIComponent(q));
  } catch (e) { box.innerHTML = ''; showErr(e); return; }
  const rows = (data.stickers || []).filter(s => _stkLibShowTrash ? s.deleted : !s.deleted);
  if (!rows.length) {
    box.innerHTML = '<div class=empty>' +
      (_stkLibShowTrash ? 'Trash is empty.' : 'No matches.') + '</div>';
    return;
  }
  box.innerHTML = '';
  const grid = document.createElement('div');
  grid.id = 'stk-search-grid';
  rows.forEach(s => grid.appendChild(_stkLibCard(s)));
  box.appendChild(grid);
}
function _stkLibCard(s) {
  const card = document.createElement('div');
  card.className = 'card';
  const media = document.createElement(s.sticker_format === 'video' ? 'video' : 'img');
  if (s.sticker_format === 'video') {
    media.autoplay = true; media.loop = true; media.muted = true; media.playsInline = true;
  }
  media.style.width = '100%';
  if (s.file_id) {
    fetch('/api/sticker_pack/sticker_file/' + encodeURIComponent(s.file_id),
          { headers: { 'X-Init-Data': initData } })
      .then(r => r.ok ? r.blob() : null)
      .then(b => { if (b) media.src = URL.createObjectURL(b); })
      .catch(() => {});
  }
  card.appendChild(media);
  const meta = document.createElement('div');
  meta.className = 'meta';
  meta.style.cssText = 'font-size:11px;color:var(--muted);margin-top:3px';
  meta.textContent = (s.emoji || '🎬') + ' · ' + (s.tags || 'no tags');
  card.appendChild(meta);
  const row = document.createElement('div');
  row.style.cssText = 'display:flex;gap:4px;margin-top:4px;flex-wrap:wrap';
  if (s.deleted) {
    const r = document.createElement('button');
    r.className = 'sec'; r.textContent = '♻ Restore'; r.style.fontSize = '11px';
    r.onclick = () => stkLibAction(s.id, 'restore'); row.appendChild(r);
  } else {
    const t = document.createElement('button');
    t.className = 'sec'; t.textContent = '🏷 Tag'; t.style.fontSize = '11px';
    t.onclick = () => stkLibTag(s.id, s.tags || ''); row.appendChild(t);
    const d = document.createElement('button');
    d.className = 'sec'; d.textContent = '🗑 Trash'; d.style.fontSize = '11px';
    d.onclick = () => stkLibAction(s.id, 'trash'); row.appendChild(d);
  }
  card.appendChild(row);
  return card;
}
async function stkLibTag(id, cur) {
  const v = prompt('Tags (space or comma separated):', cur);
  if (v === null) return;
  try {
    await api('/api/stickers/' + id + '/tags', { method: 'POST', body: JSON.stringify({ tags: v }) });
    showOk('Tagged'); stkLibSearch();
  } catch (e) { showErr(e); }
}
async function stkLibAction(id, action) {
  try {
    await api('/api/stickers/' + id + '/' + action, { method: 'POST', body: '{}' });
    showOk(action === 'trash' ? 'Moved to trash' : 'Restored'); stkLibSearch();
  } catch (e) { showErr(e); }
}

// ── Pack picker (multiple named packs; the active one is where new stickers go) ──
async function stickersLoadPacks() {
  // Populate the top-right pack dropdown (label = active pack; menu = switch /
  // create). The pack chooser is global — visible from every sticker section.
  const label = document.getElementById('stk-pack-label');
  const menu = document.getElementById('stk-pack-menu');
  if (!menu) return;
  let data;
  try { data = await api('/api/sticker_packs?kind=regular'); }
  catch (e) { return; }
  const packs = data.packs || [];
  const active = packs.find(p => p.is_active);
  if (label) label.textContent = active ? (active.pack_title || 'Pack') : (packs.length ? 'Pack' : 'No pack');
  menu.innerHTML = '';
  packs.forEach(p => {
    const it = document.createElement('button');
    it.className = 'stk-pack-item' + (p.is_active ? ' active' : '');
    it.textContent = (p.is_active ? '✓ ' : '') + (p.pack_title || 'Pack');
    it.onclick = () => { stkPackMenuClose(); stickersActivatePack(p.pack_name); };
    menu.appendChild(it);
  });
  const nb = document.createElement('button');
  nb.className = 'stk-pack-item stk-pack-new';
  nb.textContent = '＋ New pack';
  nb.onclick = () => { stkPackMenuClose(); stickersNewPack(); };
  menu.appendChild(nb);
}

// ── Pack dropdown (top-right) open/close ───────────────────────────────────
function stkPackMenuToggle(ev) {
  if (ev) ev.stopPropagation();
  const dd = document.getElementById('stk-pack-dd');
  if (dd) dd.classList.toggle('open');
}
function stkPackMenuClose() {
  const dd = document.getElementById('stk-pack-dd');
  if (dd) dd.classList.remove('open');
}
// Close the pack menu on any outside click.
document.addEventListener('click', (e) => {
  const dd = document.getElementById('stk-pack-dd');
  if (dd && dd.classList.contains('open') && !dd.contains(e.target)) dd.classList.remove('open');
});

async function stickersActivatePack(name) {
  try {
    await api('/api/sticker_pack/activate', { method: 'POST', body: JSON.stringify({ pack_name: name }) });
    _stickersContentsKindShown = null;     // force the grid to reload for the new pack
    await loadStickers();
    await stickersLoadPackContents();
    await stickersLoadPacks();
  } catch (e) { showErr('Switch failed: ' + e); }
}

async function stickersNewPack() {
  const title = prompt('Name your new pack:', '');
  if (title == null) return;
  if (!title.trim()) { showErr('Pack name required'); return; }
  try {
    await api('/api/sticker_pack/create', { method: 'POST', body: JSON.stringify({ title: title.trim(), kind: 'regular' }) });
    showOk('Pack created — it\\'s now active');
    _stickersContentsKindShown = null;
    await loadStickers();
    await stickersLoadPackContents();
    await stickersLoadPacks();
  } catch (e) { showErr('Create failed: ' + e); }
}

// ── Send-a-sticker import preference (single vs whole-pack) ──
let _stkImportPref = 'single';
function _paintImportPref() {
  document.querySelectorAll('#page-stickers .pill[data-imp]').forEach(p => {
    const on = p.dataset.imp === _stkImportPref;
    p.style.background = on ? 'linear-gradient(180deg,var(--accent),var(--accent-2))' : '#222';
    p.style.color = on ? 'var(--button-text)' : '#bbb';
    p.style.borderColor = on ? 'transparent' : '#333';
  });
}
async function stickersLoadImportPref() {
  try {
    const r = await api('/api/sticker_import_pref');
    _stkImportPref = (r && r.mode === 'all') ? 'all' : 'single';
  } catch (e) { _stkImportPref = 'single'; }
  _paintImportPref();
}
async function stickersSetImportPref(mode) {
  const prev = _stkImportPref;
  _stkImportPref = (mode === 'all') ? 'all' : 'single';
  _paintImportPref();
  try {
    await api('/api/sticker_import_pref', { method: 'POST', body: JSON.stringify({ mode: _stkImportPref }) });
    showOk(_stkImportPref === 'all' ? 'Sending a sticker now imports the whole pack' : 'Sending a sticker now adds just that one');
  } catch (e) { _stkImportPref = prev; _paintImportPref(); showErr('Save failed: ' + e); }
}

// ── Import-whole-pack banner (deep-linked from the bot via ?import=) ──
let _stkImportSource = '';
function stickersShowImportBanner(setName) {
  _stkImportSource = (setName || '').trim();
  const b = document.getElementById('stickers-import-banner');
  if (!b || !_stkImportSource) return;
  document.getElementById('stickers-import-title').textContent = '📦 Import “' + _stkImportSource + '”';
  document.getElementById('stickers-import-sub').textContent =
    'Clone the whole set into a brand-new pack named after the original. Your existing packs are untouched.';
  document.getElementById('stickers-import-status').textContent = '';
  const go = document.getElementById('stickers-import-go');
  if (go) { go.disabled = false; go.textContent = '📦 Import whole pack'; }
  b.style.display = '';
}
function stickersDismissImport() {
  _stkImportSource = '';
  const b = document.getElementById('stickers-import-banner');
  if (b) b.style.display = 'none';
}
async function stickersDoImport() {
  if (!_stkImportSource) return;
  const go = document.getElementById('stickers-import-go');
  const st = document.getElementById('stickers-import-status');
  if (go) { go.disabled = true; go.textContent = 'Importing…'; }
  if (st) st.textContent = 'Cloning stickers — this can take a moment.';
  try {
    const r = await api('/api/sticker_pack/import_set', { method: 'POST', body: JSON.stringify({ source: _stkImportSource }) });
    const pack = r.pack || {};
    let extra = '';
    if (r.skipped) extra += ' · ' + r.skipped + ' skipped';
    if (r.errors && r.errors.length) extra += ' · ' + r.errors.length + ' failed';
    showOk('Imported ' + (r.added || 0) + ' of ' + (r.source_total || 0) + ' into "' + (pack.pack_title || 'new pack') + '"' + extra);
    stickersDismissImport();
    _stickersContentsKindShown = null;
    await loadStickers();
    await stickersLoadPackContents();
    await stickersLoadPacks();
  } catch (e) {
    if (st) st.textContent = '';
    if (go) { go.disabled = false; go.textContent = '📦 Import whole pack'; }
    showErr('Import failed: ' + e);
  }
}

async function loadStickers() {
  const packEl = document.getElementById('stickers-pack-card');
  const draftsEl = document.getElementById('stickers-drafts');
  if (!_stickersUploadWired) {
    _wireStickersUpload();
    // Highlight the default-active kind button. We CAN'T call
    // stickersSwitchKind() here because it calls loadStickers() back,
    // and at this point in the bootstrap _stickersUploadWired is still
    // false — the recursive loadStickers re-enters this block, calls
    // stickersSwitchKind again, and so on, causing the "In your pack"
    // grid to wipe-and-reload-spam itself (visible as flicker). Set
    // the wired flag FIRST, then just do the synchronous button-paint
    // that stickersSwitchKind would have done.
    _stickersUploadWired = true;
    document.querySelectorAll('#page-stickers button[data-kind]').forEach(b => {
      const active = b.dataset.kind === _stickersKind;
      b.style.background = active ? 'var(--button)' : '';
      b.style.color      = active ? 'var(--button-text)' : '';
    });
    // Reflect mode persisted across sessions.
    stickersSetMode(_stickersMode);
    // Initialise the section nav (remembered per device; default Home).
    let _sec; try { _sec = localStorage.getItem('smdl_stk_section'); } catch (e) {}
    stkSection(_sec || 'home');
    stickersLoadPacks();
    stickersLoadImportPref();
  }
  let data;
  try {
    data = await api('/api/sticker_drafts?kind=' + encodeURIComponent(_stickersKind));
  } catch (e) {
    packEl.innerHTML = '<div class=empty style="color:#e88">Load failed: ' + esc(String(e)) + '</div>';
    return;
  }
  if (data.pack && data.pack.telegram_url) {
    _stickersPackUrl = data.pack.telegram_url || '';
    _stickersPackName = data.pack.pack_name || '';
    _stickersPackTitle = data.pack.pack_title || '';
    // Fire-and-forget: load the pack contents alongside the drafts list so
    // both surfaces are live by the time the page paints. The function
    // is idempotent — duplicate triggers (e.g. from refresh button) are fine.
    stickersLoadPackContents();
    // Card layout: title row with copy hint, URL row, action row.
    // The whole card is click-to-copy via stickersCopyPackLink(); the Open
    // button bypasses with stopPropagation so users can still jump to TG.
    packEl.style.cursor = 'pointer';
    packEl.title = 'Tap to copy pack link';
    packEl.onclick = stickersCopyPackLink;
    packEl.innerHTML =
      '<div style="font-size:13px;color:var(--muted);margin-bottom:4px">Your sticker pack</div>' +
      '<div style="display:flex;align-items:center;gap:8px;margin-bottom:6px">' +
        '<span style="font-size:22px;line-height:1">📦</span>' +
        '<span id=stickers-pack-title style="font-weight:600;flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">' + esc(_stickersPackTitle) + '</span>' +
        '<span class=meta style="font-size:11px;color:var(--muted);white-space:nowrap">tap to copy</span>' +
      '</div>' +
      '<div style="color:var(--accent);word-break:break-all;font-size:12px;margin-bottom:8px">' + esc(_stickersPackUrl) + '</div>' +
      '<div style="display:flex;gap:6px;flex-wrap:wrap">' +
        '<button class=sec onclick="event.stopPropagation();stickersRenamePack()">✎ Rename</button>' +
        '<button class=sec onclick="event.stopPropagation();stickersOpenPack()">↗ Open in Telegram</button>' +
      '</div>';
  } else {
    _stickersPackUrl = '';
    _stickersPackName = '';
    _stickersPackTitle = '';
    packEl.style.cursor = '';
    packEl.onclick = null;
    packEl.innerHTML = '<div class=empty>No sticker pack yet — finalise your first draft to start one.</div>';
    // Empty-pack state for the contents section.
    const grid = document.getElementById('stickers-pack-grid');
    const countEl = document.getElementById('stickers-pack-count');
    if (grid) grid.innerHTML = '<div class=empty>No pack yet — make your first sticker to start one.</div>';
    if (countEl) countEl.textContent = '';
  }
  const drafts = data.drafts || [];
  if (!drafts.length) {
    draftsEl.innerHTML = '<div class=empty>Drop a video above, or send one to <b>@Sentinel_Media_bot</b>, to start a draft.</div>';
    return;
  }
  draftsEl.innerHTML = '';
  for (const d of drafts) {
    const div = document.createElement('div');
    div.className = 'card';
    div.style.cssText = 'display:flex;gap:10px;align-items:center;margin-bottom:8px;padding:10px;';
    const status = d.status && d.status !== 'awaiting_edit'
      ? '<span style="display:inline-block;padding:1px 6px;border-radius:6px;font-size:10px;background:#333;color:#bbb;margin-left:4px">' + esc(d.status) + '</span>'
      : '';
    const err = d.error
      ? '<div style="color:#f88;font-size:11px;margin-top:2px">' + esc(d.error) + '</div>'
      : '';
    div.innerHTML =
      '<video data-draft-id="' + d.id + '" muted playsinline ' +
      'style="width:80px;height:80px;object-fit:cover;background:#000;border-radius:8px;flex:0 0 auto"></video>' +
      '<div style="flex:1;font-size:13px;min-width:0">' +
      '<div>' + _fmtStickerDur(d.duration_s) + ' · ' + (d.width || '?') + '×' + (d.height || '?') + status + '</div>' +
      '<div style="color:var(--muted);font-size:11px;margin-top:2px">' + esc(_fmtStickerExpires(d.expires_at)) + '</div>' +
      err +
      '<div style="display:flex;gap:6px;flex-wrap:wrap;margin-top:6px">' +
      '<button onclick="location.href=\\'/stickers/' + d.id + '/edit?kind=' + _stickersKind + '\\'" title="Scrubber · crop · shapes · cutout">✂️ Edit &amp; crop</button>' +
      '<button class=sec onclick="stickersDeleteDraft(' + d.id + ')">Delete</button>' +
      '</div></div>';
    draftsEl.appendChild(div);
    const videoEl = div.querySelector('video');
    fetch('/api/sticker_drafts/' + d.id + '/preview', {
      headers: { 'X-Init-Data': initData },
    }).then(r => r.ok ? r.blob() : null)
      .then(b => { if (b) videoEl.src = URL.createObjectURL(b); })
      .catch(() => {});
  }
}

async function stickersCopyPackLink() {
  if (!_stickersPackUrl) return;
  try {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      await navigator.clipboard.writeText(_stickersPackUrl);
    } else {
      // Fallback for older WebViews — execCommand still works on textareas.
      const ta = document.createElement('textarea');
      ta.value = _stickersPackUrl;
      ta.style.position = 'fixed'; ta.style.left = '-9999px';
      document.body.appendChild(ta); ta.focus(); ta.select();
      document.execCommand('copy');
      document.body.removeChild(ta);
    }
    showOk('Pack link copied');
  } catch (e) { showErr('Copy failed: ' + e); }
}

function stickersOpenPack() {
  if (!_stickersPackUrl) return;
  if (tg && tg.openTelegramLink) tg.openTelegramLink(_stickersPackUrl);
  else window.open(_stickersPackUrl, '_blank');
}

async function stickersRenamePack() {
  if (!_stickersPackName) { showErr('No pack to rename yet'); return; }
  const next = prompt('Rename your sticker pack (max 64 chars):', _stickersPackTitle || '');
  if (next == null) return;
  const title = String(next).trim();
  if (!title) { showErr('Title cannot be empty'); return; }
  if (title === _stickersPackTitle) return;
  try {
    await api('/api/sticker_pack/rename?kind=' + encodeURIComponent(_stickersKind), { method: 'POST', body: JSON.stringify({ title }) });
    showOk('Renamed to ' + title);
    loadStickers();
  } catch (e) { showErr('Rename failed: ' + e); }
}

async function stickersDeleteDraft(id) {
  if (!confirm('Delete this draft?')) return;
  try {
    await api('/api/sticker_drafts/' + id + '/delete', { method: 'POST', body: '{}' });
    loadStickers();
  } catch (e) { showErr('Delete failed: ' + e); }
}

async function stickersDeleteAll() {
  if (!confirm('Wipe ALL your sticker drafts + intermediate files?\\nAlready-published stickers in your pack are unaffected.')) return;
  try {
    const r = await api('/api/sticker_drafts/delete_all', { method: 'POST', body: '{}' });
    showOk('Deleted ' + (r.deleted || 0) + ' drafts.');
    loadStickers();
  } catch (e) { showErr('Wipe failed: ' + e); }
}

// ── Pack contents (Batch 1) ───────────────────────────────────────────────
// Lives in /api/sticker_pack/contents — Telegram is the SoT for what's
// actually in the pack. The local `stickers` table is best-effort audit
// only, so we never read from it in the UI.

let _stickersPackContents = null;   // last loaded {name, title, sticker_type, stickers[]}
let _stickersContentsBusy = false;  // dedupe overlapping fetches
let _stickersContentsKindShown = null;  // pack kind currently painted

async function stickersLoadPackContents() {
  const grid = document.getElementById('stickers-pack-grid');
  const countEl = document.getElementById('stickers-pack-count');
  if (!_stickersPackName) {
    grid.innerHTML = '<div class=empty>No pack yet — make your first sticker to start one.</div>';
    countEl.textContent = '';
    _stickersContentsKindShown = null;
    return;
  }
  if (_stickersContentsBusy) return;   // an earlier call is still in flight
  _stickersContentsBusy = true;
  // Only show the "Loading…" placeholder when the grid is currently
  // showing the EMPTY or DIFFERENT-KIND state — refreshing the same kind
  // shouldn't flash. Switching kinds genuinely changes what's shown, so
  // a brief spinner there is correct.
  const switchingKind = _stickersContentsKindShown !== _stickersKind;
  if (switchingKind || !document.getElementById('stickers-pack-grid-inner')) {
    grid.innerHTML = '<div class=empty><span class=spin></span> Loading pack contents…</div>';
  }
  try {
    const data = await api('/api/sticker_pack/contents?kind=' + encodeURIComponent(_stickersKind));
    _stickersPackContents = data;
    _stickersContentsKindShown = _stickersKind;
    const stickers = data.stickers || [];
    countEl.textContent = stickers.length ? '· ' + stickers.length + ' / 120' : '';
    if (!stickers.length) {
      grid.innerHTML = '<div class=empty>Pack is empty.</div>';
      return;
    }
    // Render into a detached node, then swap with the inner grid in one
    // assignment — avoids the visible reflow of clearing then appending
    // children one-by-one which is what was causing the flicker.
    const inner = document.createElement('div');
    inner.id = 'stickers-pack-grid-inner';
    inner.style.cssText = 'display:grid;grid-template-columns:repeat(auto-fill,minmax(120px,1fr));gap:8px';
    stickers.forEach((s, idx) => inner.appendChild(_renderPackSticker(s, idx, data.sticker_type)));
    grid.innerHTML = '';
    grid.appendChild(inner);
  } catch (e) {
    grid.innerHTML = '<div class=empty style="color:#e88">Pack load failed: ' + esc(String(e)) + '</div>';
    _stickersContentsKindShown = null;
  } finally {
    _stickersContentsBusy = false;
  }
}

function _renderPackSticker(s, idx, packType) {
  const card = document.createElement('div');
  card.className = 'card';
  card.style.cssText = 'padding:8px;display:flex;flex-direction:column;gap:6px;position:relative';
  card.dataset.fileId = s.file_id;
  const tagEl = s.is_video ? 'video' : 'img';
  const media = document.createElement(tagEl);
  media.style.cssText = 'width:100%;aspect-ratio:1;object-fit:contain;background:#0c0c0c;border-radius:6px';
  if (s.is_video) {
    media.setAttribute('muted',''); media.setAttribute('playsinline','');
    media.setAttribute('loop',''); media.setAttribute('autoplay','');
    // Mobile WebViews leave a <video> black until a frame is decoded+painted —
    // which only happens on play/seek. If autoplay is throttled in a grid, nudge
    // currentTime so at least the first frame shows instead of a black square.
    media.addEventListener('loadeddata', () => {
      try { if (media.paused) media.currentTime = 0.04; } catch (e) {}
    }, { once: true });
  }
  // Stream the sticker bytes via our own proxy (TG file_path URLs leak the token).
  fetch('/api/sticker_pack/sticker_file/' + encodeURIComponent(s.file_id), {
    headers: { 'X-Init-Data': initData },
  }).then(r => r.ok ? r.blob() : null)
    .then(b => { if (b) media.src = URL.createObjectURL(b); })
    .catch(() => {});
  card.appendChild(media);
  const meta = document.createElement('div');
  meta.style.cssText = 'display:flex;align-items:center;gap:4px;font-size:13px';
  meta.innerHTML = '<span style="font-size:18px">' + esc(s.emoji || '🎬') + '</span>' +
    '<span class=meta style="font-size:11px;color:var(--muted)">#' + (idx + 1) + '</span>' +
    '<span style="flex:1"></span>' +
    '<span title="' + (s.is_video ? 'video' : 'static') + '" style="font-size:12px;opacity:.8">' +
      (s.is_video ? '🎬' : '🖼') + '</span>';
  card.appendChild(meta);
  // Declutter: per-sticker actions are hidden until you tap the card.
  const actions = document.createElement('div');
  actions.style.cssText = 'display:none;flex-wrap:wrap;gap:4px;margin-top:2px';
  actions.innerHTML =
    '<button class=sec style="font-size:11px;padding:4px 6px" title="Open in the editor — re-crop, trim, shape, cut-out" onclick="stickersEditInEditor(this)">✂️ Edit</button>' +
    '<button class=sec style="font-size:11px;padding:4px 6px" title="Change emoji" onclick="stickersEditEmoji(this)">✎</button>' +
    '<button class=sec style="font-size:11px;padding:4px 6px" title="Keywords" onclick="stickersEditKeywords(this)">🔑</button>' +
    '<button class=sec style="font-size:11px;padding:4px 6px" title="Set as cover" onclick="stickersSetCover(this)">⭐</button>' +
    '<button class=sec style="font-size:11px;padding:4px 6px;color:#e88" title="Remove from pack" onclick="stickersRemoveFromPack(this)">🗑</button>';
  card.appendChild(actions);
  card.addEventListener('click', (e) => {
    if (e.target.closest('button')) return;   // let the action buttons work
    actions.style.display = actions.style.display === 'none' ? 'flex' : 'none';
  });
  return card;
}

function _packCardFileId(btn) {
  const card = btn.closest('[data-file-id]');
  return card ? card.dataset.fileId : null;
}

async function stickersEditEmoji(btn) {
  const file_id = _packCardFileId(btn);
  if (!file_id) return;
  const raw = prompt('New emojis (space-separated, up to 20):', '🎬');
  if (raw == null) return;
  // Split on whitespace and chunk into individual emoji code-points-ish.
  // Telegram counts each emoji separately even if compound; the parsing
  // mirrors what's reasonable to type by hand.
  const emojis = raw.trim().split(/\\s+/).filter(Boolean);
  if (!emojis.length) { showErr('At least one emoji'); return; }
  try {
    await api('/api/sticker_pack/sticker/emojis?kind=' + encodeURIComponent(_stickersKind), { method: 'POST',
      body: JSON.stringify({ file_id, emojis }) });
    showOk('Emojis updated');
    stickersLoadPackContents();
  } catch (e) { showErr('Update failed: ' + e); }
}

async function stickersEditKeywords(btn) {
  const file_id = _packCardFileId(btn);
  if (!file_id) return;
  const raw = prompt('Search keywords (comma-separated, ≤ 20 entries, ≤ 64 chars total):', '');
  if (raw == null) return;
  const keywords = raw.split(',').map(s => s.trim()).filter(Boolean);
  try {
    await api('/api/sticker_pack/sticker/keywords?kind=' + encodeURIComponent(_stickersKind), { method: 'POST',
      body: JSON.stringify({ file_id, keywords }) });
    showOk('Keywords updated');
  } catch (e) { showErr('Update failed: ' + e); }
}

async function stickersSetCover(btn) {
  const file_id = _packCardFileId(btn);
  if (!file_id) return;
  try {
    await api('/api/sticker_pack/sticker/set_cover?kind=' + encodeURIComponent(_stickersKind), { method: 'POST',
      body: JSON.stringify({ file_id }) });
    showOk('Pack cover updated');
  } catch (e) { showErr('Set cover failed: ' + e); }
}

async function stickersRemoveFromPack(btn) {
  const file_id = _packCardFileId(btn);
  if (!file_id) return;
  if (!confirm('Remove this sticker from your pack?\\nThe pack stays; only this entry goes.')) return;
  try {
    await api('/api/sticker_pack/sticker/delete?kind=' + encodeURIComponent(_stickersKind), { method: 'POST',
      body: JSON.stringify({ file_id }) });
    showOk('Removed from pack');
    stickersLoadPackContents();
  } catch (e) { showErr('Remove failed: ' + e); }
}

// Open a published pack sticker in the Studio editor. Telegram can't edit a
// sticker's bytes in place, so the backend re-imports its bytes into a fresh
// draft and we jump to the editor on that draft. Re-making there appends a new
// variant to the pack (use 🗑 to drop the original if you don't want both).
async function stickersEditInEditor(btn) {
  const file_id = _packCardFileId(btn);
  if (!file_id) return;
  showOk('Opening in editor…');
  try {
    const r = await api('/api/sticker_pack/sticker/to_draft', { method: 'POST',
      body: JSON.stringify({ file_id }) });
    const kind = r.is_video ? 'video' : 'static';
    location.href = '/stickers/' + r.id + '/edit?kind=' + kind;
  } catch (e) { showErr('Couldn\\'t open editor: ' + e); }
}

// ── Look-up + clone for packs not created by our bot ─────────────────────
// TG API: a bot can only mutate packs it created. So for packs we don't
// own, we expose a read-only view and a "Clone into your own pack" button
// per-sticker — the bytes get re-uploaded into one of THIS user's owned
// packs (video/static/emoji), where every existing edit affordance works.

let _stickersLookupOpen = false;
let _stickersLookupData = null;     // last successful lookup result

function stickersToggleLookup() {
  _stickersLookupOpen = !_stickersLookupOpen;
  const card = document.getElementById('stickers-lookup-card');
  card.style.display = _stickersLookupOpen ? '' : 'none';
  if (_stickersLookupOpen) {
    setTimeout(() => document.getElementById('stickers-lookup-input').focus(), 50);
  }
}

async function stickersDoLookup() {
  const inp = document.getElementById('stickers-lookup-input');
  const meta = document.getElementById('stickers-lookup-meta');
  const titleEl = document.getElementById('stickers-lookup-title');
  const status = document.getElementById('stickers-lookup-status');
  const grid = document.getElementById('stickers-lookup-grid');
  const raw = (inp.value || '').trim();
  if (!raw) { showErr('Paste a pack URL or name first'); return; }
  meta.style.display = 'block';
  titleEl.textContent = 'Looking up…';
  status.textContent = '';
  grid.innerHTML = '<div class=empty><span class=spin></span> Fetching pack…</div>';
  try {
    const data = await api('/api/sticker_set/lookup?name=' + encodeURIComponent(raw));
    _stickersLookupData = data;
    titleEl.textContent = '📦 ' + data.title;
    const stickers = data.stickers || [];
    const ownership = data.owned_by_us
      ? '✓ This bot owns this pack — switch to that kind to edit it fully.'
      : '🔒 Read-only — pack owned by another bot. Clone individual stickers into yours to edit.';
    // Sniff what the bulk-clone destinations would be from the sticker_type
    // and the first sticker's format. Hide the bulk button entirely on
    // packs of all-animated stickers (nothing to clone).
    const cloneAble = stickers.some(s => !s.is_animated);
    const bulkBtns = cloneAble
      ? ' · <button class=sec onclick="stickersCloneWholePack(\\'video\\')" style="font-size:11px;padding:3px 8px">📥 Clone all → Video</button>'
        + ' <button class=sec onclick="stickersCloneWholePack(\\'static\\')" style="font-size:11px;padding:3px 8px">📥 → Static</button>'
        + ' <button class=sec onclick="stickersCloneWholePack(\\'custom_emoji\\')" style="font-size:11px;padding:3px 8px">📥 → 😀 Emoji</button>'
      : '';
    status.innerHTML = esc(ownership) + ' · ' + stickers.length + ' sticker' + (stickers.length === 1 ? '' : 's') +
      ' · <a href="#" id=stickers-lookup-tg style="color:var(--accent)">' + esc(data.url) + '</a>' +
      bulkBtns;
    const link = document.getElementById('stickers-lookup-tg');
    if (link) link.addEventListener('click', ev => {
      ev.preventDefault();
      if (tg && tg.openTelegramLink) tg.openTelegramLink(data.url);
      else window.open(data.url, '_blank');
    });
    if (!stickers.length) {
      grid.innerHTML = '<div class=empty>Pack has no stickers.</div>';
      return;
    }
    const inner = document.createElement('div');
    inner.style.cssText = 'display:grid;grid-template-columns:repeat(auto-fill,minmax(120px,1fr));gap:8px';
    stickers.forEach((s, idx) => inner.appendChild(_renderLookupSticker(s, idx, data)));
    grid.innerHTML = '';
    grid.appendChild(inner);
  } catch (e) {
    grid.innerHTML = '<div class=empty style="color:#e88">Lookup failed: ' + esc(String(e)) + '</div>';
    titleEl.textContent = '';
    status.textContent = '';
  }
}

function _renderLookupSticker(s, idx, packData) {
  const card = document.createElement('div');
  card.className = 'card';
  card.style.cssText = 'padding:8px;display:flex;flex-direction:column;gap:6px;position:relative';
  card.dataset.fileId = s.file_id;
  const tagEl = s.is_video ? 'video' : 'img';
  const media = document.createElement(tagEl);
  media.style.cssText = 'width:100%;aspect-ratio:1;object-fit:contain;background:#000;border-radius:6px';
  if (s.is_video) { media.setAttribute('muted',''); media.setAttribute('playsinline',''); media.setAttribute('loop',''); media.setAttribute('autoplay',''); }
  // Reuse the existing proxy (any caller can fetch any sticker by file_id —
  // file_ids are stable, not enumerable).
  fetch('/api/sticker_pack/sticker_file/' + encodeURIComponent(s.file_id), {
    headers: { 'X-Init-Data': initData },
  }).then(r => r.ok ? r.blob() : null)
    .then(b => { if (b) media.src = URL.createObjectURL(b); })
    .catch(() => {});
  card.appendChild(media);
  const metaRow = document.createElement('div');
  metaRow.style.cssText = 'display:flex;align-items:center;gap:4px;font-size:13px';
  metaRow.innerHTML = '<span style="font-size:18px">' + esc(s.emoji || '🎬') + '</span>' +
    '<span class=meta style="font-size:11px;color:var(--muted)">#' + (idx + 1) + '</span>';
  card.appendChild(metaRow);
  // Clone button. Target kind defaults match the source format —
  // video → video, static → static. Emoji pack is always available.
  const compatibleVideo  = !!s.is_video;
  const compatibleStatic = !s.is_video && !s.is_animated;
  const actions = document.createElement('div');
  actions.style.cssText = 'display:flex;flex-wrap:wrap;gap:4px';
  let buttons = '';
  if (compatibleVideo) {
    buttons += '<button class=sec style="font-size:11px;padding:4px 6px" onclick="stickersCloneTo(this,\\'video\\')" title="Clone into your Video pack">→ Video</button>';
    buttons += '<button class=sec style="font-size:11px;padding:4px 6px" onclick="stickersCloneTo(this,\\'custom_emoji\\')" title="Clone into your Emoji pack">→ 😀 Emoji</button>';
  } else if (compatibleStatic) {
    buttons += '<button class=sec style="font-size:11px;padding:4px 6px" onclick="stickersCloneTo(this,\\'static\\')" title="Clone into your Static pack">→ Static</button>';
    buttons += '<button class=sec style="font-size:11px;padding:4px 6px" onclick="stickersCloneTo(this,\\'custom_emoji\\')" title="Clone into your Emoji pack">→ 😀 Emoji</button>';
  }
  actions.innerHTML = buttons || '<span class=meta style="font-size:11px;color:var(--muted)">animated — clone not supported</span>';
  card.appendChild(actions);
  return card;
}

async function stickersCloneWholePack(targetKind) {
  if (!_stickersLookupData) { showErr('Look up a pack first'); return; }
  const src = _stickersLookupData;
  const compatibleCount = (src.stickers || []).filter(s => {
    if (s.is_animated) return false;
    if (targetKind === 'custom_emoji') return true;
    const srcFmt = s.is_video ? 'video' : 'static';
    return srcFmt === targetKind;
  }).length;
  if (!compatibleCount) {
    showErr('No compatible stickers for target ' + targetKind);
    return;
  }
  const msg = 'Clone ~' + compatibleCount + ' sticker(s) from "' + src.title + '" into your ' + targetKind + ' pack?\\nEach source sticker keeps its emoji. Incompatible ones are skipped.';
  if (!confirm(msg)) return;
  // The request can take a long time for a big pack (1–2s per sticker
  // at TG's rate). Pop a banner so the user knows we're working.
  const status = document.getElementById('stickers-lookup-status');
  const prev = status.innerHTML;
  status.innerHTML = '<span class=spin></span> Cloning ' + compatibleCount + ' sticker(s)… this can take a minute.';
  try {
    const r = await api('/api/sticker_pack/clone_pack', {
      method: 'POST',
      body: JSON.stringify({ source: src.name, target_kind: targetKind, limit: 50 }),
    });
    let summary = '✓ Added ' + r.added + ' · skipped ' + r.skipped;
    if (r.truncated) summary += ' · capped at ' + r.processed + ' of ' + r.source_total + ' (re-run to continue)';
    if (r.errors && r.errors.length) summary += ' · ' + r.errors.length + ' error(s)';
    showOk(summary);
    if (r.errors && r.errors.length) {
      // Log to console so the user can inspect; toasting all of them
      // would be noisy.
      console.warn('clone_pack errors:', r.errors);
    }
    if (_stickersKind === targetKind) loadStickers();
  } catch (e) {
    showErr('Bulk clone failed: ' + e);
  } finally {
    status.innerHTML = prev;
  }
}

async function stickersCloneTo(btn, targetKind) {
  const card = btn.closest('[data-file-id]');
  if (!card) return;
  const file_id = card.dataset.fileId;
  const emoji = (prompt('Emoji for the cloned sticker:', '🎬') || '').trim();
  if (!emoji) { showErr('Cancelled'); return; }
  btn.disabled = true; const orig = btn.textContent; btn.textContent = '…';
  try {
    const r = await api('/api/sticker_pack/clone_sticker', {
      method: 'POST',
      body: JSON.stringify({ source_file_id: file_id, target_kind: targetKind, emoji }),
    });
    showOk('Cloned to your ' + targetKind + ' pack');
    // If the active kind matches the clone target, refresh the contents
    // grid so the user sees the new sticker land immediately.
    if (_stickersKind === targetKind) loadStickers();
  } catch (e) {
    showErr('Clone failed: ' + e);
  } finally {
    btn.disabled = false; btn.textContent = orig;
  }
}

async function stickersDeletePack() {
  if (!_stickersPackName) { showErr('No pack to delete'); return; }
  if (!confirm('Delete the ENTIRE sticker pack "' + (_stickersPackTitle || _stickersPackName) + '"?\\nThis is irreversible — every sticker in the pack will be gone.')) return;
  // Double-tap confirmation: type "delete" to proceed.
  const c = prompt('Type "delete" (lowercase) to confirm:');
  if ((c || '').trim().toLowerCase() !== 'delete') { showErr('Cancelled'); return; }
  try {
    await api('/api/sticker_pack/delete?kind=' + encodeURIComponent(_stickersKind), { method: 'POST',
      body: JSON.stringify({ confirm: true }) });
    showOk('Pack deleted');
    loadStickers();
  } catch (e) { showErr('Delete pack failed: ' + e); }
}

// ── Upload wiring (file picker + drag-and-drop + paste + bulk + auto-make) ─
// Bound once per session via `_stickersUploadWired`. Sources of upload:
//   1. Dropzone click → hidden <input type=file multiple>
//   2. 📷 Record button → camera-capture input (mobile)
//   3. Drag-and-drop (multi-file)
//   4. Paste from clipboard (Stickers tab focus only)
// All four funnel into _stickersEnqueue() which serialises uploads so we
// never run two XHRs in parallel.

let _stickersUploadQueue = [];   // pending File objects
let _stickersUploadActive = false;
// Mode: 'instant' = upload → /make → DM, skipping the editor entirely.
//       'manual'  = drafts queue (current behavior), user clicks edit.
// Persists across sessions in localStorage. Default = instant for the
// casual single-tap flow that's most users' actual intent.
let _stickersMode = 'instant';
try {
  const saved = localStorage.getItem('smdl_stickers_mode');
  if (saved === 'instant' || saved === 'manual') _stickersMode = saved;
  const savedEmoji = localStorage.getItem('smdl_stickers_default_emoji');
  if (savedEmoji) document.addEventListener('DOMContentLoaded', () => {
    const el = document.getElementById('stickers-default-emoji');
    if (el) el.value = savedEmoji;
  });
} catch (e) {}

function stickersSetMode(m) {
  if (m !== 'instant' && m !== 'manual') return;
  _stickersMode = m;
  try { localStorage.setItem('smdl_stickers_mode', m); } catch (e) {}
  document.querySelectorAll('#page-stickers .pill[data-mode]').forEach(el => {
    const on = el.dataset.mode === m;
    el.style.background = on ? '#284' : '#222';
    el.style.borderColor = on ? '#284' : '#333';
    el.style.color = on ? '#dfd' : '#bbb';
  });
  // Default-emoji input is only meaningful in instant mode; in manual the
  // editor's per-sticker emoji picker takes over.
  const emojiInput = document.getElementById('stickers-default-emoji');
  if (emojiInput) emojiInput.style.display = m === 'instant' ? '' : 'none';
  const hint = document.getElementById('stickers-mode-hint');
  if (hint) hint.innerHTML = m === 'instant'
    ? '⚡ <b>Instant</b>: auto-makes a sticker (centre crop, first 3s) and DMs it to you — also leaves a draft to refine.'
    : '✂️ <b>Edit / crop</b>: upload makes a draft; open the editor to trim, crop, shape, or cut out the background.';
}

// ℹ️ toggle: show/hide the compact mode explanation next to the pills.
function stickersToggleHint() {
  const h = document.getElementById('stickers-mode-hint');
  if (h) h.style.display = (h.style.display === 'none' || !h.style.display) ? 'block' : 'none';
}

// Persist default emoji on change.
document.addEventListener('input', e => {
  if (e.target && e.target.id === 'stickers-default-emoji') {
    try { localStorage.setItem('smdl_stickers_default_emoji', e.target.value || '🎬'); } catch (err) {}
  }
});

function _stickersQueuePillUpdate() {
  const pill = document.getElementById('stickers-queue-pill');
  if (!pill) return;
  const q = _stickersUploadQueue.length;
  pill.textContent = (q || _stickersUploadActive) ? (q + ' queued') : '';
}

function _stickersEnqueue(files) {
  let added = 0;
  for (const f of (files || [])) {
    if (!f) continue;
    if (f.size > 50 * 1024 * 1024) { showErr('Skipped ' + (f.name || 'file') + ': over 50 MB'); continue; }
    _stickersUploadQueue.push(f); added++;
  }
  if (!added) return;
  _stickersQueuePillUpdate();
  if (!_stickersUploadActive) _stickersDrainQueue();
}

async function _stickersDrainQueue() {
  if (_stickersUploadActive) return;
  _stickersUploadActive = true;
  // A lone drop keeps its draft so the user has an "✂️ Edit & crop" card to
  // re-crop / shape / cut out even in Instant mode. A bulk drop cleans up so
  // 20 files don't litter the Drafts list.
  const keepDrafts = _stickersUploadQueue.length === 1;
  while (_stickersUploadQueue.length) {
    const f = _stickersUploadQueue.shift();
    _stickersQueuePillUpdate();
    try {
      await stickersUploadFile(f, keepDrafts);
    } catch (e) {
      // stickersUploadFile already toasts the user — keep draining.
    }
  }
  _stickersUploadActive = false;
  _stickersQueuePillUpdate();
  // One refresh at the end of the batch is cheaper than per-file.
  loadStickers();
}

function _wireStickersUpload() {
  const dz = document.getElementById('stickers-dropzone');
  const fi = document.getElementById('stickers-file');
  const cam = document.getElementById('stickers-camera');
  if (!dz || !fi) return;
  dz.addEventListener('click', () => fi.click());
  fi.addEventListener('change', () => {
    if (fi.files && fi.files.length) {
      const arr = Array.from(fi.files); fi.value = '';
      _stickersEnqueue(arr);
    }
  });
  if (cam) cam.addEventListener('change', () => {
    if (cam.files && cam.files.length) {
      const arr = Array.from(cam.files); cam.value = '';
      _stickersEnqueue(arr);
    }
  });
  // Drag-and-drop. preventDefault on dragover is what allows drop to fire.
  ['dragenter','dragover'].forEach(ev => dz.addEventListener(ev, e => {
    e.preventDefault(); e.stopPropagation();
    dz.style.borderColor = 'var(--accent)';
    dz.style.background = 'rgba(255,255,255,0.03)';
  }));
  ['dragleave','drop'].forEach(ev => dz.addEventListener(ev, e => {
    e.preventDefault(); e.stopPropagation();
    dz.style.borderColor = ''; dz.style.background = '';
  }));
  dz.addEventListener('drop', e => {
    const dt = e.dataTransfer;
    if (dt && dt.files && dt.files.length) _stickersEnqueue(Array.from(dt.files));
  });
  // Paste handler — bound to document, but only consumes the event when the
  // Stickers tab is the active page. Keeps clipboard paste available on
  // other tabs (e.g. text fields) without conflict.
  document.addEventListener('paste', e => {
    if (current !== 'stickers') return;
    const items = (e.clipboardData && e.clipboardData.items) || [];
    const files = [];
    for (const it of items) {
      if (it.kind === 'file') {
        const f = it.getAsFile();
        if (f && (f.type.startsWith('video/') || f.type === 'image/gif')) files.push(f);
      }
    }
    if (files.length) {
      e.preventDefault();
      _stickersEnqueue(files);
    }
  });
}

async function stickersUploadFile(file, keepDraft) {
  const progEl = document.getElementById('stickers-upload-progress');
  const barEl = document.getElementById('stickers-upload-bar');
  const statEl = document.getElementById('stickers-upload-status');
  statEl.style.color = '';
  if (file.size > 50 * 1024 * 1024) {
    showErr('File too large (max 50 MB)');
    return;
  }
  progEl.style.display = 'block';
  barEl.style.width = '0';
  statEl.textContent = 'Uploading ' + file.name + ' (' + Math.round(file.size / 1024) + ' KB)…';
  const fd = new FormData();
  fd.append('file', file, file.name);
  const result = await new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open('POST', '/api/sticker_drafts');
    xhr.setRequestHeader('X-Init-Data', initData);
    xhr.upload.addEventListener('progress', ev => {
      if (ev.lengthComputable) {
        const pct = Math.round((ev.loaded / ev.total) * 100);
        barEl.style.width = pct + '%';
        statEl.textContent = 'Uploading ' + pct + '% (' + Math.round(ev.loaded / 1024) + ' / ' + Math.round(ev.total / 1024) + ' KB)';
      }
    });
    xhr.onload = () => {
      if (xhr.status >= 200 && xhr.status < 300) {
        try { resolve(JSON.parse(xhr.responseText)); }
        catch (e) { resolve({}); }
      } else {
        let detail = xhr.responseText;
        try { detail = JSON.parse(xhr.responseText).detail || detail; } catch (e) {}
        statEl.textContent = 'Failed: ' + detail;
        statEl.style.color = '#e88';
        showErr('Upload failed: ' + detail);
        reject(new Error(detail));
      }
    };
    xhr.onerror = () => {
      statEl.textContent = 'Network error during upload';
      statEl.style.color = '#e88';
      showErr('Upload network error');
      reject(new Error('network'));
    };
    xhr.send(fd);
  });
  barEl.style.width = '100%';
  if (_stickersMode === 'instant') {
    // Skip the editor entirely. Convert with sane defaults, push to the
    // active-kind pack, then drop the draft so the queue doesn't bloat.
    statEl.textContent = '⚡ Converting & sending…';
    try {
      await _stickersInstantMake(result.id);
      // A lone drop keeps its draft so the user can re-crop / shape / cut it
      // out from the editor; a bulk drop cleans up so the Drafts list doesn't
      // fill with 20 expended source files.
      if (!keepDraft) {
        try { await api('/api/sticker_drafts/' + result.id + '/delete', { method: 'POST', body: '{}' }); }
        catch (e) { /* draft cleanup is best-effort */ }
        statEl.textContent = '✓ Added to your ' + _stickersKind + ' pack';
      } else {
        statEl.innerHTML = '✓ Added to your ' + _stickersKind + ' pack — tap <b>✂️ Edit &amp; crop</b> below to refine.';
      }
    } catch (e) {
      statEl.textContent = '❌ ' + e;
      statEl.style.color = '#e88';
    }
  } else {
    statEl.textContent = 'Uploaded — refreshing drafts…';
  }
  setTimeout(() => { progEl.style.display = 'none'; }, 400);
}

async function _stickersInstantMake(draftId) {
  // Pull the default emoji from the input; fall back to 🎬 if blank.
  // No prompt — Instant mode is supposed to be zero-tap after the drop.
  const emojiEl = document.getElementById('stickers-default-emoji');
  let emoji = (emojiEl && emojiEl.value || '').trim();
  if (!emoji) emoji = '🎬';
  return await api('/api/sticker_drafts/' + draftId + '/make', {
    method: 'POST',
    body: JSON.stringify({
      emoji,
      trim_start: 0,
      trim_end:   3,
      pack_kind:  _stickersKind,
    }),
  });
}

async function redeliverDownload(id, btn) {
  if (btn) { btn.disabled = true; btn.textContent = 'Sending…'; }
  try {
    const r = await api('/api/miniapp/downloads/redeliver', {
      method: 'POST', body: JSON.stringify({ id })
    });
    if (r.delivered === 'link' && r.share_url) {
      showOk('File too large to re-send — opening share link');
      openExternal(encodeURIComponent(r.share_url));
    } else {
      showOk('Re-delivered to your chat');
    }
    if (btn) btn.textContent = 'Sent ✓';
  } catch(e) {
    showErr('Re-deliver failed: ' + e);
    if (btn) { btn.disabled = false; btn.textContent = 'Re-deliver'; }
  }
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

    // #77 — gallery list (images + videos, in render order) so the preview
    // lightbox can swipe / arrow between them. thumbable == image|video.
    _galleryItems = j.files
      .filter(f => f.share_url && kindOf(f.name).thumbable)
      .map(f => ({ url: f.share_url, name: f.name }));

    const onClickFile = (f) => {
      if (!f.share_url) return `showErr('No share URL — SHARE_SECRET/PUBLIC_BASE_URL not configured')`;
      const gi = _galleryItems.findIndex(g => g.url === f.share_url);
      if (gi >= 0) return `openPreviewAt(${gi})`;
      return `openPreview('${encodeURIComponent(f.share_url)}', '${encodeURIComponent(f.name)}')`;
    };

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
let _galleryItems = [];   // #77 — [{url,name}] previewable media in the folder
let _galleryIndex = -1;   // index into _galleryItems, or -1 for a standalone open

// Render the media for one item into the open modal. Shared by the standalone
// openPreview() and the gallery openPreviewAt().
function _renderPreview(url, name) {
  _previewUrl = url; _previewName = name;
  const ext = (name.split('.').pop() || '').toLowerCase();
  const body = document.getElementById('preview-body');
  const nameEl = document.getElementById('preview-name');
  nameEl.textContent = name;
  // Stop any media still playing from the previous gallery item.
  body.querySelectorAll('video, audio').forEach(el => { try { el.pause(); el.src = ''; } catch{} });

  let inner;
  if (['mp4','mov','mkv','webm','m4v'].includes(ext)) {
    inner = `<video src="${url}" controls autoplay playsinline></video>`;
  } else if (['jpg','jpeg','png','gif','webp','heic','avif','bmp'].includes(ext)) {
    inner = `<img src="${url}" alt="${esc(name)}">`;
  } else if (['mp3','m4a','aac','flac','wav','opus','ogg'].includes(ext)) {
    inner = `<audio src="${url}" controls autoplay></audio>`;
  } else {
    inner = `<div class=non-media>
      No inline preview for <code>.${esc(ext || 'file')}</code> files.<br>
      Use the ⬇ Download button above to fetch it.
    </div>`;
  }
  body.innerHTML = inner;
}

function _updateGalleryNav() {
  const modal = document.getElementById('preview-modal');
  const count = document.getElementById('preview-count');
  const show = _galleryIndex >= 0 && _galleryItems.length > 1;
  if (modal) modal.classList.toggle('gallery', show);
  if (count) count.textContent = show ? (_galleryIndex + 1) + ' / ' + _galleryItems.length : '';
}

// Standalone open (audio / docs / non-gallery files).
function openPreview(encodedUrl, encodedName) {
  _galleryIndex = -1;
  _renderPreview(decodeURIComponent(encodedUrl), decodeURIComponent(encodedName));
  _updateGalleryNav();
  document.getElementById('preview-modal').classList.add('open');
}

// Open the gallery at a given index (image / video files).
function openPreviewAt(idx) {
  if (idx < 0 || idx >= _galleryItems.length) return;
  _galleryIndex = idx;
  const it = _galleryItems[idx];
  _renderPreview(it.url, it.name);
  _updateGalleryNav();
  document.getElementById('preview-modal').classList.add('open');
}

// Step through the gallery (wraps at both ends). delta is -1 / +1.
function galleryNav(delta) {
  if (_galleryIndex < 0 || _galleryItems.length < 2) return;
  let n = (_galleryIndex + delta) % _galleryItems.length;
  if (n < 0) n += _galleryItems.length;
  openPreviewAt(n);
}

function closePreview() {
  const body = document.getElementById('preview-body');
  // Stop any playing media before the modal closes
  body.querySelectorAll('video, audio').forEach(el => { try { el.pause(); el.src = ''; } catch{} });
  body.innerHTML = '';
  const modal = document.getElementById('preview-modal');
  modal.classList.remove('open');
  modal.classList.remove('gallery');
  _previewUrl = '';
  _previewName = '';
  _galleryIndex = -1;
}

// #77 — arrow keys + horizontal swipe move through the gallery; Esc closes.
function initPreviewGestures() {
  document.addEventListener('keydown', e => {
    const modal = document.getElementById('preview-modal');
    if (!modal || !modal.classList.contains('open')) return;
    if (e.key === 'ArrowLeft')  { e.preventDefault(); galleryNav(-1); }
    else if (e.key === 'ArrowRight') { e.preventDefault(); galleryNav(1); }
    else if (e.key === 'Escape') { closePreview(); }
  });
  const body = document.getElementById('preview-body');
  if (!body) return;
  let x0 = null, y0 = null;
  body.addEventListener('touchstart', e => {
    if (_galleryIndex < 0) { x0 = null; return; }
    const t = e.changedTouches[0]; x0 = t.clientX; y0 = t.clientY;
  }, { passive: true });
  body.addEventListener('touchend', e => {
    if (x0 === null) return;
    const t = e.changedTouches[0];
    const dx = t.clientX - x0, dy = t.clientY - y0;
    x0 = null;
    // Only count clearly-horizontal swipes so we don't fight scroll/zoom.
    if (Math.abs(dx) > 50 && Math.abs(dx) > Math.abs(dy) * 1.5) {
      galleryNav(dx < 0 ? 1 : -1);
    }
  }, { passive: true });
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

    // Branding tile (#74) — owner-only app-logo upload.
    const brandingTile = isOwner ? `
      <div class=set-tile id=branding-tile>
        <div class=head>✦ Branding</div>
        <div class=meta>Replace the “Sentinel Media” wordmark on the home header with your own logo. PNG / JPG / WebP / GIF · max 2&nbsp;MB.</div>
        <div id=brand-preview style="margin-top:10px;min-height:46px;display:flex;align-items:center">
          <span class=meta>Loading…</span>
        </div>
        <input type=file id=brand-file accept="image/png,image/jpeg,image/webp,image/gif" style="display:none" onchange="uploadLogo(this)">
        <div class=btn-row style="margin-top:10px">
          <button class=sec onclick="document.getElementById('brand-file').click()">⬆ Upload logo</button>
          <button class="small danger" id=brand-remove onclick="removeLogo()" style="display:none">🗑 Remove</button>
        </div>
        <div id=brand-msg class=meta style="margin-top:6px"></div>
      </div>` : '';

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
        <div class=set-tile id=appearance-tile>
          <div class=head>🎨 Appearance</div>
          ${appearanceInner()}
        </div>
        ${brandingTile}
        ${oneDriveTile}
        ${downloadsTile}
      </div>
    `;
    updatePathTemplatePreview();
    refreshBrandPreview();
  } catch(e) { showErr('Load failed: '+e); }
}

// ── #74: app-logo branding ───────────────────────────────────────────────────
const BRAND_LOGO_URL = '/api/miniapp/branding/logo';

// Apply the logo to the home header (called on boot + after upload/remove).
function applyBrandLogo() {
  const img = document.getElementById('brand-logo');
  const txt = document.getElementById('brand-text');
  if (!img || !txt) return;
  const bust = BRAND_LOGO_URL + '?t=' + Date.now();
  const probe = new Image();
  probe.onload = () => {
    img.src = probe.src; img.style.display = 'block'; txt.style.display = 'none';
    // Point the favicon + apple-touch-icon at the (now-confirmed) logo.
    ['favicon', 'favicon-apple'].forEach(id => {
      const link = document.getElementById(id);
      if (link) link.href = bust;
    });
  };
  probe.onerror = () => { img.style.display = 'none'; txt.style.display = ''; };
  probe.src = bust;
}

function refreshBrandPreview() {
  const box = document.getElementById('brand-preview');
  const rm  = document.getElementById('brand-remove');
  if (!box) return;
  const probe = new Image();
  probe.onload = () => {
    box.innerHTML = '<img src="' + probe.src + '" style="max-height:46px;max-width:100%;object-fit:contain">';
    if (rm) rm.style.display = '';
  };
  probe.onerror = () => {
    box.innerHTML = '<span class=meta>No logo set — showing the “Sentinel Media” wordmark.</span>';
    if (rm) rm.style.display = 'none';
  };
  probe.src = BRAND_LOGO_URL + '?t=' + Date.now();
}

async function uploadLogo(input) {
  const file = input.files && input.files[0];
  input.value = '';
  const msg = document.getElementById('brand-msg');
  if (!file) return;
  const okTypes = ['image/png','image/jpeg','image/webp','image/gif'];
  if (okTypes.indexOf(file.type) < 0) {
    if (msg) msg.textContent = 'Unsupported type — use PNG, JPG, WebP or GIF.';
    return;
  }
  if (file.size > 2*1024*1024) {
    if (msg) msg.textContent = 'Too large — keep it under 2 MB.';
    return;
  }
  if (msg) msg.textContent = 'Uploading…';
  const reader = new FileReader();
  reader.onload = () => {
    api('/api/miniapp/branding/logo', {
      method: 'POST',
      body: JSON.stringify({ data_url: reader.result }),
    }).then(() => {
      if (msg) msg.textContent = 'Saved.';
      refreshBrandPreview();
      applyBrandLogo();
    }).catch(e => { if (msg) msg.textContent = 'Upload failed: ' + e; });
  };
  reader.onerror = () => { if (msg) msg.textContent = 'Could not read file.'; };
  reader.readAsDataURL(file);
}

function removeLogo() {
  if (!confirm('Remove the custom logo and revert to the Sentinel Media wordmark?')) return;
  const msg = document.getElementById('brand-msg');
  api('/api/miniapp/branding/logo', { method: 'DELETE' }).then(() => {
    if (msg) msg.textContent = 'Removed.';
    refreshBrandPreview();
    applyBrandLogo();
  }).catch(e => { if (msg) msg.textContent = 'Remove failed: ' + e; });
}

// ── Appearance: black/metallic/futuristic theme engine (per-device) ──────────
// THEMES is generated from app/theme_tokens.json (see app/themes.py).
const THEMES = @@THEME_SWATCHES@@;

function appearanceInner() {
  const cur = document.documentElement.dataset.theme || 'chrome';
  const fx  = document.documentElement.dataset.fx || 'bold';
  const swatches = THEMES.map(t => `
    <button class="swatch ${t.id === cur ? 'active' : ''}" onclick="setTheme('${t.id}')"
      style="background:linear-gradient(160deg, ${t.surf}, ${t.bg})">
      <span class=sw-dot style="background:${t.accent}; box-shadow:0 0 8px ${t.accent}"></span>
      <span class=sw-name>${t.name}</span>
    </button>`).join('');
  return `
    <div class=appearance>
      <div class=lbl>Palette</div>
      <div class=theme-swatches>${swatches}</div>
      <div class=lbl style="margin-top:12px">Intensity</div>
      <div class=fx-toggle>
        <button class="${fx === 'bold' ? 'active' : ''}" onclick="setFx('bold')">⚡ Bold</button>
        <button class="${fx === 'refined' ? 'active' : ''}" onclick="setFx('refined')">✦ Refined</button>
      </div>
      ${isOwner ? `<a class=lbl style="display:inline-block;margin-top:12px;color:var(--accent);text-decoration:none" href="/app/sitebuilder">⬡ Open Sitebuilder — edit palettes &amp; intensities →</a>` : ''}
    </div>`;
}

function renderAppearance() {
  const tile = document.getElementById('appearance-tile');
  if (tile) tile.innerHTML = '<div class=head>🎨 Appearance</div>' + appearanceInner();
}

function setTheme(id) {
  document.documentElement.dataset.theme = id;
  try { localStorage.setItem('smdl_theme', id); } catch (e) {}
  renderAppearance();
}

function setFx(mode) {
  document.documentElement.dataset.fx = mode;
  try { localStorage.setItem('smdl_fx', mode); } catch (e) {}
  renderAppearance();
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
  const msg = 'Move ' + n + ' file' + (n === 1 ? '' : 's') + ' to match the template?\\n\\nEvery move is journaled — you can undo it afterwards.';
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
      while ((idx = buf.indexOf('\\n\\n')) >= 0) {
        const frame = buf.slice(0, idx);
        buf = buf.slice(idx + 2);
        const line = frame.split('\\n').find(l => l.startsWith('data:'));
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
  const msg = 'Undo the most recent migration?\\n\\nFiles will be moved back to their original paths.';
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

    // 2d. Moderation audit — recent approve/deny/revoke/restore actions.
    //     Best-effort: a failed fetch hides the card but never blocks the
    //     rest of the admin load.
    let auditHtml = '';
    try {
      const audit = await api('/api/miniapp/admin/audit');
      const ACT = {
        approve:         { icon: '✅', label: 'Approved',        color: 'var(--success)' },
        approve_by_code: { icon: '✅', label: 'Approved by code', color: 'var(--success)' },
        deny:            { icon: '🚫', label: 'Denied',          color: 'var(--destructive)' },
        revoke:          { icon: '⛔', label: 'Revoked',         color: 'var(--destructive)' },
        restore:         { icon: '↩', label: 'Restored',        color: '#ff9500' },
      };
      const rows = (audit.items || []).map(ev => {
        const a = ACT[ev.action] || { icon: '•', label: ev.action, color: 'var(--fg)' };
        return `
          <div class="user-row" style="padding:8px 0;border-top:1px solid var(--separator)">
            <div class=grow>
              <div class=name><span style="color:${a.color}">${a.icon} ${esc(a.label)}</span>
                <span class=meta style="font-family:ui-monospace;margin-left:6px">chat ${ev.chat_id ?? '—'}</span>
              </div>
              <div class=meta>${timeago(ev.created_at)}${ev.detail ? ' · ' + esc(ev.detail) : ''}</div>
            </div>
          </div>`;
      }).join('');
      auditHtml = `
        <div class=card>
          <div class=field>🧾 Moderation log (${audit.count || 0})</div>
          ${rows || '<div class=meta style="margin-top:6px">No moderation actions recorded yet.</div>'}
        </div>`;
    } catch(_e) {
      console.warn('audit card failed:', _e);
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
      + `<div class=subtab-pane id=subpane-approval>${pendingHtml + usersHtml + auditHtml + groupsHtml}</div>`
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
      const ue = encodeURIComponent(p.url);
      const pauseBtn = enabled
        ? `<button class="icon-btn" title="Pause" onclick='scraperPause(${u})'>⏸</button>`
        : `<button class="icon-btn primary" title="Resume" onclick='scraperResume(${u})'>▶</button>`;
      return `
      <div class="scraper-row" title="${esc(p.url)}">
        <span class="dot" style="background:${dotColor};margin-right:0"></span>
        <a class="uname u-link" onclick="openExternal('${ue}')" title="Open profile · ${esc(p.url)}">@${esc(uname)}</a>
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
// Show the "previewing as <plan>" banner + sync the Admin selector if the
// owner has a simulation cookie set.
renderViewAsBanner();
initViewAsControl();
// Apply the custom app logo to the home header if one is set (#74).
applyBrandLogo();
// Apply the saved home-tile order + wire up drag-to-reorder (#41).
initTileReorder();
// Wire arrow-key + swipe nav for the file-preview gallery (#77).
initPreviewGestures();
// Boot navigation: land on Home unless the URL deep-links a specific
// tab via ?tab=<name>. Deep-links are how external entry points (the
// bot's "Open sticker editor" button, a redirected /stickers URL, etc.)
// route into the SPA without duplicating Mini App surfaces.
const _bootTabs = new Set(['home','downloads','notifications','search','watchlist','library','stickers','streamer','files','scraper','settings','admin']);
let _bootTab = 'home';
let _bootImport = '';
try {
  const qp = new URLSearchParams(window.location.search);
  const t = (qp.get('tab') || '').trim().toLowerCase();
  if (t && _bootTabs.has(t)) _bootTab = t;
  // Deep-link from the bot's "Import the whole pack" button.
  _bootImport = (qp.get('import') || '').trim();
  if (_bootImport) {
    _bootTab = 'stickers';
    // loadStickers()'s init reads this to pick its section — pin it to
    // 'stickers' so it doesn't restore a remembered 'pack'/'add' and hide
    // the import banner.
    try { localStorage.setItem('smdl_stk_section', 'stickers'); } catch (e) {}
  }
} catch (e) { /* old browsers: stay on home */ }
goto(_bootTab);
if (_bootImport) {
  try { stkSection('stickers'); } catch (e) {}
  // loadStickers() (fired by goto) wires the section; show the banner now —
  // it only needs DOM, and the import call itself re-auths server-side.
  setTimeout(function () { try { stickersShowImportBanner(_bootImport); } catch (e) {} }, 60);
}
// Surface the unread-activity badge on the home tile (best-effort).
refreshNotifBadge();
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


def _render_app_html() -> str:
    """Inject the generated theme CSS + tokens into the static HTML shell.
    Cheap string substitution; themes.load_tokens() hot-reloads on file change
    so editing theme_tokens.json restyles the app without a restart."""
    from . import themes
    tokens = themes.load_tokens()
    dft_theme, dft_fx = themes.defaults(tokens)
    return (HTML
            .replace("/*@@THEME_CSS@@*/", themes.render_theme_css(tokens))
            .replace("@@DEFAULT_THEME@@", dft_theme)
            .replace("@@DEFAULT_FX@@", dft_fx)
            .replace("@@THEME_SWATCHES@@", themes.swatches_js(tokens)))


# The app shell is a tiny, server-rendered HTML string that gates owner-only
# nav at runtime via whoami. It MUST NOT be cached by the WebView: a stale
# copy strands the owner on an old build (missing nav, missing Account hatch)
# and is exactly the "somehow I am on a stale version" failure. no-store is
# cheap here — the heavy assets (thumbs, stremio bundle) carry their own
# long-lived cache headers separately.
_NO_STORE = {"Cache-Control": "no-store, must-revalidate", "Pragma": "no-cache"}


@router.get("/app", response_class=HTMLResponse)
async def miniapp_index():
    return HTMLResponse(_render_app_html(), headers=_NO_STORE)


@router.get("/app/", response_class=HTMLResponse)
async def miniapp_index_slash():
    return HTMLResponse(_render_app_html(), headers=_NO_STORE)


@router.get("/app/sitebuilder", response_class=HTMLResponse)
@router.get("/app/sitebuilder/", response_class=HTMLResponse)
async def miniapp_sitebuilder():
    """Sentinel Sitebuilder — owner-gated theme-token editor.

    Served from SMDL itself (same origin as the token API → reuses cookie /
    X-Init-Data auth). The page is harmless without auth: its GET/POST to
    /api/miniapp/theme-tokens are the gated boundary (POST is owner-only).
    The Tauri desktop window (desktop/) points at this URL."""
    from . import sitebuilder
    return HTMLResponse(sitebuilder.SITEBUILDER_HTML)


@router.get("/api/miniapp/theme-tokens")
async def get_theme_tokens(request: Request):
    """Read the live design tokens. Auth'd (not owner-only) so any client —
    a future Sitebuilder, another pillar — can mirror the palette."""
    await _verify(request)
    from . import themes
    return themes.load_tokens()


@router.post("/api/miniapp/theme-tokens")
async def put_theme_tokens(request: Request):
    """Overwrite the design tokens (owner-only). Restyles the app immediately —
    this is the 'change themes without rewiring code' hook the builder drives."""
    p = await _verify(request)
    _require_owner(p)
    from . import themes
    body = await request.json()
    try:
        saved = themes.save_tokens(body)
    except ValueError as e:
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True, "tokens": saved}


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


def _clear_apk_cookie(resp, request: Request) -> None:
    """Delete the session cookie. Must mirror the domain/path used by
    _set_apk_cookie or the browser keeps the old cookie."""
    host = (request.url.hostname or "").lower()
    domain = COOKIE_DOMAIN if host.endswith("az-sentinel.xyz") else None
    resp.delete_cookie(key=COOKIE_NAME, domain=domain, path="/")


@router.get("/auth/session")
async def auth_session(request: Request):
    """Describe the CURRENT session so the Account panel can show who you
    are and offer Logout / re-key. Never raises — an absent/invalid cookie
    just reports kind='none'. Owner = v1 cookie or v2 with the '*' scope."""
    session = _parse_session_cookie(request.cookies.get(COOKIE_NAME, ""))
    if session is None:
        return {"authenticated": False, "kind": "none",
                "version": None, "scopes": [], "user_id": None}
    scopes = session.get("scopes") or []
    is_owner = "*" in scopes
    return {
        "authenticated": True,
        "kind": "owner" if is_owner else "guest",
        "version": session.get("version"),
        "scopes": scopes,
        "user_id": session.get("user_id"),
    }


@router.post("/auth/logout")
async def auth_logout(request: Request):
    """Drop the session cookie (e.g. to leave a guest session before
    pasting the owner key)."""
    resp = JSONResponse({"ok": True})
    _clear_apk_cookie(resp, request)
    return resp


class _AccountDeleteBody(BaseModel):
    confirm: bool = False


@router.post("/api/account/delete")
async def account_delete(request: Request, body: _AccountDeleteBody):
    """Delete the CALLER'S OWN account + all their personal data.

    Mandatory for Play (the free-registered tier creates accounts). Acts only
    on the authenticated principal — never deletes another user — honouring the
    no-silent-identity-switch rule. Requires an explicit confirm flag, then
    drops the session cookie so the deleted session can't keep acting.
    """
    p = await _verify(request)
    uid = int(p["user"]["id"])
    if not body.confirm:
        return JSONResponse(
            {"ok": False, "error": "confirmation_required",
             "detail": "POST {confirm:true} to delete your account and data."},
            status_code=400,
        )
    # The owner account is config-anchored and re-created on next interaction;
    # refuse here so an owner session can't be silently wiped by a stray tap.
    if _is_owner(uid):
        return JSONResponse(
            {"ok": False, "error": "owner_account",
             "detail": "The owner account is managed by the operator, not self-deletable here."},
            status_code=403,
        )
    result = await _db.delete_user_account(uid)
    resp = JSONResponse({"ok": True, "deleted": result})
    _clear_apk_cookie(resp, request)
    return resp


_ACCOUNT_DELETE_HTML = """<!doctype html>
<html lang=en><head>
<meta charset=utf-8>
<meta name=viewport content="width=device-width, initial-scale=1">
<title>Delete your account — Sentinel Media</title>
<style>
  :root { color-scheme: dark; }
  body { margin:0; background:#0b0e14; color:#e6e6e6; font:16px/1.5 Inter,system-ui,sans-serif; }
  .wrap { max-width:640px; margin:0 auto; padding:32px 20px 64px; }
  h1 { font-size:22px; margin:0 0 16px; }
  .card { background:#141925; border:1px solid #232a3a; border-radius:12px; padding:20px; margin:18px 0; }
  ul { padding-left:20px; }
  li { margin:4px 0; }
  button { font:inherit; border:0; border-radius:8px; padding:12px 18px; cursor:pointer; }
  .danger { background:#b3261e; color:#fff; }
  .danger:disabled { opacity:.5; cursor:not-allowed; }
  .msg { margin-top:14px; min-height:1.4em; }
  .ok { color:#5ad27a; } .err { color:#ff6b6b; } .muted { color:#9aa4b2; }
  code { background:#1c2230; padding:1px 6px; border-radius:5px; }
</style></head>
<body><div class=wrap>
  <h1>Delete your Sentinel Media account</h1>
  <p>This permanently erases your account and all associated personal data. This cannot be undone.</p>
  <div class=card>
    <strong>What gets deleted</strong>
    <ul>
      <li>Your account profile and registration</li>
      <li>Your download history</li>
      <li>Your sticker packs, drafts and stickers</li>
    </ul>
    <p class=muted>We do not sell your data. Aggregate, non-identifying logs may persist as required for security and abuse prevention.</p>
  </div>
  <div class=card>
    <p id=who class=muted>Checking your session…</p>
    <label><input type=checkbox id=confirm> I understand this is permanent.</label>
    <div style="margin-top:14px">
      <button class=danger id=btn disabled>Delete my account</button>
    </div>
    <p class="msg" id=msg></p>
  </div>
  <p class=muted>Signed out? Open the app, sign in, then return to this page (or use <code>Account → Delete account</code> in the app).</p>
</div>
<script>
(function(){
  var who = document.getElementById('who');
  var cb  = document.getElementById('confirm');
  var btn = document.getElementById('btn');
  var msg = document.getElementById('msg');
  var authed = false;
  function hdrs(){ return { 'Content-Type': 'application/json' }; }
  fetch('/auth/session', { credentials: 'include' })
    .then(function(r){ return r.json(); })
    .then(function(s){
      authed = !!(s && s.authenticated);
      who.textContent = authed
        ? 'Signed in. You can delete your account below.'
        : 'You are not signed in. Sign in first, then return here.';
      sync();
    })
    .catch(function(){ who.textContent = 'Could not check your session.'; });
  function sync(){ btn.disabled = !(authed && cb.checked); }
  cb.addEventListener('change', sync);
  btn.addEventListener('click', function(){
    btn.disabled = true; msg.className='msg muted'; msg.textContent='Deleting…';
    fetch('/api/account/delete', {
      method:'POST', credentials:'include', headers: hdrs(),
      body: JSON.stringify({ confirm: true })
    }).then(function(r){ return r.json().then(function(j){ return { ok:r.ok, j:j }; }); })
      .then(function(res){
        if (res.ok && res.j && res.j.ok) {
          msg.className='msg ok';
          msg.textContent='Your account and data have been deleted.';
          cb.disabled = true;
        } else {
          msg.className='msg err';
          msg.textContent = (res.j && (res.j.detail || res.j.error)) || 'Deletion failed.';
          btn.disabled = false;
        }
      })
      .catch(function(){ msg.className='msg err'; msg.textContent='Network error.'; btn.disabled=false; });
  });
})();
</script>
</body></html>"""


@router.get("/account/delete", response_class=HTMLResponse)
async def account_delete_page():
    """Public account-deletion page — the stable URL disclosed in the Play
    Data Safety form. Explains what is deleted and lets a signed-in user do it;
    signed-out users are told how to sign in first."""
    return HTMLResponse(_ACCOUNT_DELETE_HTML, headers=_NO_STORE)


class _AuthLoginBody(BaseModel):
    token: str


@router.post("/auth/login")
async def auth_login(body: _AuthLoginBody, request: Request):
    """JSON twin of /auth/setup for in-app re-keying: paste the 64-char
    owner token → set the owner cookie → return JSON (no redirect, so the
    Account panel can confirm inline)."""
    if not _safe_token_eq((body.token or "").strip(), OWNER_AUTH_TOKEN):
        return JSONResponse({"ok": False, "error": "invalid_token"}, status_code=401)
    resp = JSONResponse({"ok": True, "kind": "owner"})
    _set_apk_cookie(resp, request)
    return resp


# Self-contained — NO @@placeholder@@ tokens (those are serve-time
# substituted in the main /app HTML and can mangle JS). Plain f-string-free
# string so nothing here depends on the inline-app JS or owner perms.
_ACCOUNT_HTML = """<!doctype html><html lang=en><head>
<meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>Account · Sentinel Media</title>
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<style>
:root{color-scheme:dark}
*{box-sizing:border-box}
body{font:15px/1.55 system-ui,-apple-system,Segoe UI,Roboto,sans-serif;
background:#0c0c0e;color:#e8e8ea;margin:0;padding:24px 18px;max-width:560px;
margin-left:auto;margin-right:auto}
h1{font-size:22px;margin:4px 0 18px}
.card{background:#161618;border:1px solid #26262a;border-radius:14px;
padding:16px 16px;margin:0 0 16px}
.lbl{font-size:12px;text-transform:uppercase;letter-spacing:.05em;color:#8a8a90;
margin:0 0 6px}
.val{font-size:16px;font-weight:600}
.badge{display:inline-block;padding:3px 10px;border-radius:999px;font-size:13px;
font-weight:600}
.badge.owner{background:#10331c;color:#5fd38a;border:1px solid #1f5c33}
.badge.guest{background:#33270f;color:#e7b15f;border:1px solid #5c451f}
.badge.none{background:#26262a;color:#9a9aa0;border:1px solid #38383c}
.muted{color:#8a8a90;font-size:13px;margin:8px 0 0}
input{width:100%;background:#0c0c0e;border:1px solid #38383c;color:#e8e8ea;
border-radius:10px;padding:12px 12px;font-size:15px;font-family:ui-monospace,
SFMono-Regular,Menlo,monospace;margin:0 0 10px}
button{width:100%;border:0;border-radius:10px;padding:12px 12px;font-size:15px;
font-weight:600;cursor:pointer}
.primary{background:#5b9dff;color:#06121f}
.danger{background:#2a1416;color:#ff8b8b;border:1px solid #5c2226}
.ghost{background:transparent;color:#5b9dff;text-decoration:none;display:block;
text-align:center;padding:12px;font-weight:600}
.msg{font-size:13px;margin:8px 0 0;min-height:16px}
.msg.ok{color:#5fd38a}.msg.err{color:#ff8b8b}
.row{margin:0 0 10px}
</style></head><body>
<h1>Account</h1>

<div class=card id=status-card>
  <p class=lbl>Session</p>
  <p class=val><span class="badge none" id=kind-badge>checking…</span></p>
  <p class=muted id=scope-line></p>
</div>

<div class=card id=logout-card style="display:none">
  <p class=lbl>Leave this session</p>
  <button class=danger id=btn-logout>Log out</button>
  <p class="msg" id=logout-msg></p>
</div>

<div class=card>
  <p class=lbl>Sign in as owner</p>
  <div class=row>
    <input id=owner-key type=password autocomplete=off autocapitalize=off
      autocorrect=off spellcheck=false placeholder="64-character owner key">
  </div>
  <button class=primary id=btn-login>Set owner key</button>
  <p class="msg" id=login-msg></p>
</div>

<a class=ghost href="/app">Back to Sentinel Media</a>

<script>
var tg = (window.Telegram && window.Telegram.WebApp) || null;
if (tg) { try { tg.ready(); tg.expand(); } catch (e) {} }

function hdrs(extra) {
  var h = { "Accept": "application/json" };
  if (tg && tg.initData) h["X-Init-Data"] = tg.initData;
  if (extra) for (var k in extra) h[k] = extra[k];
  return h;
}

function renderSession(s) {
  var badge = document.getElementById("kind-badge");
  var scope = document.getElementById("scope-line");
  var logoutCard = document.getElementById("logout-card");
  var kind = s && s.kind ? s.kind : "none";
  badge.className = "badge " + kind;
  if (kind === "owner") {
    badge.textContent = "Owner";
    scope.textContent = "Full access. Re-key below if you need to rotate.";
    logoutCard.style.display = "";
  } else if (kind === "guest") {
    badge.textContent = "Guest";
    var sc = (s.scopes || []).join(", ") || "limited";
    scope.textContent = "Scoped session (" + sc + "). Log out, then paste the owner key.";
    logoutCard.style.display = "";
  } else {
    badge.textContent = "Not signed in";
    scope.textContent = "Paste the owner key below to sign in.";
    logoutCard.style.display = "none";
  }
}

function loadSession() {
  fetch("/auth/session", { headers: hdrs(), credentials: "include", cache: "no-store" })
    .then(function (r) { return r.json(); })
    .then(renderSession)
    .catch(function () {
      document.getElementById("kind-badge").textContent = "unknown";
    });
}

document.getElementById("btn-logout").addEventListener("click", function () {
  var m = document.getElementById("logout-msg");
  m.className = "msg"; m.textContent = "Logging out…";
  fetch("/auth/logout", { method: "POST", headers: hdrs(), credentials: "include" })
    .then(function (r) { return r.json(); })
    .then(function () {
      m.className = "msg ok"; m.textContent = "Logged out.";
      loadSession();
    })
    .catch(function () { m.className = "msg err"; m.textContent = "Logout failed."; });
});

document.getElementById("btn-login").addEventListener("click", function () {
  var token = document.getElementById("owner-key").value.trim();
  var m = document.getElementById("login-msg");
  if (!token) { m.className = "msg err"; m.textContent = "Enter the owner key."; return; }
  m.className = "msg"; m.textContent = "Verifying…";
  fetch("/auth/login", {
    method: "POST",
    headers: hdrs({ "Content-Type": "application/json" }),
    credentials: "include",
    body: JSON.stringify({ token: token }),
  })
    .then(function (r) { return r.json().then(function (j) { return { ok: r.ok, j: j }; }); })
    .then(function (res) {
      if (res.ok && res.j && res.j.ok) {
        m.className = "msg ok"; m.textContent = "Signed in. Loading…";
        window.location.href = "/app";
      } else {
        m.className = "msg err";
        m.textContent = (res.j && res.j.error === "invalid_token")
          ? "That key did not match." : "Sign-in failed.";
      }
    })
    .catch(function () { m.className = "msg err"; m.textContent = "Network error."; });
});

loadSession();
</script>
</body></html>"""


@router.get("/account", response_class=HTMLResponse)
async def account_page():
    """Standalone, perms-independent account/login page. Reached from the
    main app via location.href (the one nav style that works for a guest /
    non-owner who cannot use the goto()-driven tabs). Served no-store so a
    stale cached copy can never lock the owner out again."""
    return HTMLResponse(
        _ACCOUNT_HTML,
        headers={"Cache-Control": "no-store, must-revalidate", "Pragma": "no-cache"},
    )
