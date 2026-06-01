"""Streamer recording consent — the license layer for community Twitch recording.

Why
---
Recording a public Twitch stream without the broadcaster's permission is
ToS-violating and gray-area legally; recording WITH the broadcaster's
affirmative grant is the "licensed" posture that takes the community
deployment out of the gray. Discussion + architecture lives in the
2026-06-01 planning conversation; this is the data + control plane.

Auth model
----------
Identity comes from `auth_twitch` (the OAuth callback issues a session
cookie with user_id="twitch:<id>"). All consent-mutation endpoints
require that the caller's session user_id matches the twitch_user_id
they're consenting for — you can only set consent on your own channel,
proven by Twitch login.

The recording GATE (`is_record_allowed`) is callable by other modules
(bot.py video-record handler, iptv_routes.py recorder route, the
stream_monitor "Yes — Record" callback) so wherever recording starts,
the consent check runs once.

Routes
------
  GET  /api/streamer/me            current Twitch identity + consent state
  POST /api/streamer/consent       create/update consent
  POST /api/streamer/revoke        revoke (sets revoked_at, blocks future records)
  GET  /api/streamer/recordings    audit list of recordings made of this channel
  GET  /api/recording/check?login= pre-flight for the recording UI; no auth needed
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlparse

import aiosqlite
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from . import auth_twitch
from .database import DB_PATH
from .miniapp import _verify


logger = logging.getLogger(__name__)
router = APIRouter()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── URL → channel-login extraction ─────────────────────────────────────────


_TWITCH_PATH_RE = re.compile(r"^/?([A-Za-z0-9][A-Za-z0-9_]{2,24})/?$")
_TWITCH_HOSTS = ("twitch.tv", "www.twitch.tv", "m.twitch.tv", "go.twitch.tv")


def extract_twitch_login(url_or_text: str) -> str | None:
    """Pull a Twitch channel login from a URL like:
        https://twitch.tv/somechannel
        https://www.twitch.tv/somechannel/
        twitch.tv/somechannel?other=stuff
        somechannel                                (bare login)
    Returns the lowercase login, or None if it's not a Twitch URL.

    Bare logins (no host) are accepted IFF they look like a valid Twitch
    login (TG name rules: alnum + underscore, 3–25 chars). That makes
    `/api/recording/check?login=foo` ergonomic without forcing callers
    to construct a URL.
    """
    s = (url_or_text or "").strip()
    if not s:
        return None
    # Try URL form first.
    try:
        u = urlparse(s if "://" in s else f"https://{s}")
        host = (u.hostname or "").lower().lstrip(".")
        if host in _TWITCH_HOSTS or any(host.endswith("." + h) for h in _TWITCH_HOSTS):
            m = _TWITCH_PATH_RE.match(u.path or "/")
            return m.group(1).lower() if m else None
    except Exception:
        pass
    # Bare-login fallback.
    if _TWITCH_PATH_RE.match(s):
        return s.lower()
    return None


# ── DB CRUD ────────────────────────────────────────────────────────────────


async def get_consent(twitch_user_id: str) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM streamer_consents WHERE twitch_user_id = ?",
            (twitch_user_id,),
        ) as cur:
            row = await cur.fetchone()
            return _hydrate(row) if row else None


async def get_consent_by_login(login: str) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM streamer_consents WHERE twitch_login = ?",
            ((login or "").lower(),),
        ) as cur:
            row = await cur.fetchone()
            return _hydrate(row) if row else None


def _hydrate(row) -> dict:
    """Parse allowed_users_json into a list so callers don't repeat the
    json.loads dance."""
    d = dict(row)
    try:
        d["allowed_users"] = json.loads(d.get("allowed_users_json") or "[]")
    except Exception:
        d["allowed_users"] = []
    d["allow_recording"] = bool(d.get("allow_recording"))
    d["allow_all_users"] = bool(d.get("allow_all_users"))
    d["revoked"]         = bool(d.get("revoked_at"))
    return d


async def upsert_consent(twitch_user_id: str, twitch_login: str, *,
                          allow_recording: bool,
                          max_duration_min: int,
                          allow_all_users: bool,
                          allowed_users: list[str],
                          notes: str | None) -> dict:
    now = _now_iso()
    allowed_json = json.dumps(
        sorted({str(u).strip() for u in (allowed_users or []) if str(u).strip()})
    )
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO streamer_consents
                (twitch_user_id, twitch_login, allow_recording,
                 max_duration_min, allow_all_users, allowed_users_json,
                 notes, consented_at, revoked_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)
            ON CONFLICT(twitch_user_id) DO UPDATE SET
                twitch_login       = excluded.twitch_login,
                allow_recording    = excluded.allow_recording,
                max_duration_min   = excluded.max_duration_min,
                allow_all_users    = excluded.allow_all_users,
                allowed_users_json = excluded.allowed_users_json,
                notes              = excluded.notes,
                revoked_at         = NULL,
                updated_at         = excluded.updated_at
        """, (twitch_user_id, (twitch_login or "").lower(), 1 if allow_recording else 0,
              max(1, int(max_duration_min)), 1 if allow_all_users else 0,
              allowed_json, notes, now, now))
        await db.commit()
    out = await get_consent(twitch_user_id)
    assert out is not None
    return out


async def revoke_consent(twitch_user_id: str) -> bool:
    now = _now_iso()
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("""
            UPDATE streamer_consents
            SET allow_recording = 0,
                revoked_at      = ?,
                updated_at      = ?
            WHERE twitch_user_id = ?
        """, (now, now, twitch_user_id))
        await db.commit()
        return (cur.rowcount or 0) > 0


# ── The gate (callable from any record-start path) ────────────────────────


async def is_record_allowed(url_or_login: str,
                             requesting_user_id: str | None,
                             requested_duration_min: int = 240) -> tuple[bool, dict]:
    """The single function the rest of the codebase calls to ask "may this
    user start recording this Twitch channel?".

    Returns (allowed, info). `info` always contains:
        twitch_login         the parsed login (or None if not Twitch)
        is_twitch            True iff the URL is a Twitch URL
        consent_present      True iff a consent row exists
        allowed              same as the bool tuple element
        reason               short machine-readable reason, e.g. "no_consent",
                             "revoked", "user_not_allowed", "ok"
        max_duration_min     cap from consent (when present)
        effective_duration   min(requested, cap)
        notes                streamer's own free-text note (shown to recorder)

    Non-Twitch URLs return (True, {is_twitch:False, reason:"not_twitch"})
    — the gate is Twitch-specific by design. Other source platforms make
    their own policy decisions.
    """
    login = extract_twitch_login(url_or_login)
    if not login:
        return True, {"twitch_login": None, "is_twitch": False,
                       "allowed": True, "reason": "not_twitch",
                       "consent_present": False}

    row = await get_consent_by_login(login)
    if not row:
        return False, {"twitch_login": login, "is_twitch": True,
                        "allowed": False, "reason": "no_consent",
                        "consent_present": False,
                        "message": (
                            f"@{login} hasn't opted in to community recording. "
                            "Ask them to sign in at this app with Twitch and "
                            "toggle consent on their dashboard."
                        )}

    info: dict = {
        "twitch_login":    row["twitch_login"],
        "is_twitch":       True,
        "consent_present": True,
        "max_duration_min": int(row.get("max_duration_min") or 240),
        "notes":           row.get("notes"),
    }
    if not row["allow_recording"] or row["revoked"]:
        info.update(allowed=False, reason="revoked")
        return False, info
    if not row["allow_all_users"] and requesting_user_id:
        if requesting_user_id not in (row.get("allowed_users") or []):
            info.update(allowed=False, reason="user_not_allowed")
            return False, info
    eff = min(int(requested_duration_min or 240), info["max_duration_min"])
    info.update(allowed=True, reason="ok", effective_duration=eff)
    return True, info


# ── Identity gate for the CRUD routes ──────────────────────────────────────


def _twitch_user_id_from_session(payload: dict) -> str | None:
    """Pull the Twitch user id out of a verified _verify() session payload.
    Returns None if the session isn't a Twitch one."""
    sess = (payload or {}).get("session") or {}
    uid = sess.get("user_id") or ""
    if uid.startswith("twitch:"):
        return uid.split(":", 1)[1]
    return None


def _require_twitch_session(payload: dict) -> str:
    uid = _twitch_user_id_from_session(payload)
    if not uid:
        raise HTTPException(401, "twitch_signin_required")
    return uid


# ── Request models ─────────────────────────────────────────────────────────


class ConsentBody(BaseModel):
    allow_recording: bool = True
    max_duration_min: int = 240
    allow_all_users: bool = True
    # Optional whitelist of SMDL user_ids (in the same format auth_v2
    # session payloads use, e.g. "twitch:12345" or "google:abc" or just
    # a numeric TG chat_id). Ignored when allow_all_users is True.
    allowed_users: list[str] = []
    notes: Optional[str] = None


# ── Routes ─────────────────────────────────────────────────────────────────


@router.get("/api/streamer/me")
async def streamer_me(request: Request) -> dict:
    """Current Twitch identity + consent state for the signed-in user.
    Used to render the Streamer dashboard. 401 if the session isn't Twitch.
    """
    payload = await _verify(request)
    twitch_user_id = _require_twitch_session(payload)
    ident = await auth_twitch.get_identity(twitch_user_id)
    if not ident:
        raise HTTPException(404, "twitch_identity_not_found")
    consent = await get_consent(twitch_user_id)
    return {"identity": ident, "consent": consent}


@router.post("/api/streamer/consent")
async def streamer_consent(body: ConsentBody, request: Request) -> dict:
    payload = await _verify(request)
    twitch_user_id = _require_twitch_session(payload)
    ident = await auth_twitch.get_identity(twitch_user_id)
    if not ident:
        raise HTTPException(404, "twitch_identity_not_found")
    if body.max_duration_min < 1 or body.max_duration_min > 720:
        # 12h hard cap — even an opted-in streamer doesn't get to license
        # a 24h archive of their own stream; way too piracy-archive-shaped.
        raise HTTPException(400, "max_duration_min must be between 1 and 720")
    if body.notes is not None and len(body.notes) > 512:
        raise HTTPException(400, "notes too long (max 512 chars)")
    row = await upsert_consent(
        twitch_user_id=twitch_user_id,
        twitch_login=ident["twitch_login"],
        allow_recording=body.allow_recording,
        max_duration_min=body.max_duration_min,
        allow_all_users=body.allow_all_users,
        allowed_users=body.allowed_users or [],
        notes=(body.notes.strip() if body.notes else None),
    )
    return {"ok": True, "consent": row}


@router.post("/api/streamer/revoke")
async def streamer_revoke(request: Request) -> dict:
    """Convenience for "turn off consent." Equivalent to POSTing
    /api/streamer/consent with allow_recording=False, but also stamps
    revoked_at so the timeline of grant/revoke is preserved."""
    payload = await _verify(request)
    twitch_user_id = _require_twitch_session(payload)
    ok = await revoke_consent(twitch_user_id)
    return {"ok": True, "revoked": ok}


@router.get("/api/streamer/recordings")
async def streamer_recordings(request: Request) -> dict:
    """Audit list of recordings made of THIS streamer's channel — the
    streamer's own view of who recorded what. Sourced from the existing
    download_history table filtered to URLs whose hostname is twitch.tv
    and whose channel matches this streamer's login. Best-effort — the
    schema doesn't currently tag recordings with channel + recorder
    explicitly, but the URL field is enough for now."""
    payload = await _verify(request)
    twitch_user_id = _require_twitch_session(payload)
    ident = await auth_twitch.get_identity(twitch_user_id)
    if not ident:
        raise HTTPException(404, "twitch_identity_not_found")
    login = ident["twitch_login"]
    out: list[dict] = []
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT id, chat_id, url, downloaded_at, files "
            "FROM download_history WHERE url LIKE ? "
            "ORDER BY downloaded_at DESC LIMIT 200",
            (f"%twitch.tv/{login}%",),
        ) as cur:
            async for row in cur:
                d = dict(row)
                try:
                    d["files"] = json.loads(d.get("files") or "[]")
                except Exception:
                    d["files"] = []
                out.append(d)
    return {"recordings": out, "channel": login}


@router.get("/api/recording/check")
async def recording_check(login: str | None = None,
                          duration_min: int = 240) -> dict:
    """Pre-flight for the Mini App's record UI — no auth required so the
    UX can show a consent badge BEFORE the user clicks record. Encodes
    the same decision the record-time gate will make."""
    if not login:
        raise HTTPException(400, "login is required")
    allowed, info = await is_record_allowed(login, None, duration_min)
    return info
