"""Twitch OAuth — sign-in for streamers who want to consent to recording.

Why this exists
---------------
Sentinel Media TV can record public Twitch streams, but Twitch's ToS
forbids recording in general and the legal posture is gray without an
explicit grant from the broadcaster (see the planning conversation
2026-06-01). The clean architectural fix: have the streamer log in via
Twitch and affirmatively opt in. Their identity + opt-in are then the
license the recorder operates under.

This module only handles the IDENTITY half of that — it proves the
caller is who they say they are on Twitch and remembers the channel
metadata. The consent CRUD + recording gate live in a separate module
(streamer_consent.py) so the auth layer stays simple and reusable.

Flow
----
  GET  /auth/twitch/start?next=<safe-path>
       → signed state cookie + 302 to id.twitch.tv/oauth2/authorize

  GET  /auth/twitch/callback?code=...&state=...
       → exchange code for tokens, /oauth2/validate the access_token,
         fetch /helix/users for profile, upsert twitch_identities,
         issue v2 session cookie with user_id="twitch:<twitch_user_id>",
         302 to safe `next`.

  POST /auth/twitch/signout
       → drop the session cookie.

Mirrors the shape of auth_google.py so future readers can recognise the
pattern. Differences are entirely Twitch-API-specific:
  - /oauth2/validate is the token-verification step (Google uses
    tokeninfo); doesn't expose nonce/aud directly, so we verify those
    ourselves by re-checking against the signed state cookie.
  - /helix/users needs both Authorization: Bearer AND Client-Id header.
  - Tokens are short-lived (~4h) but we don't store them — we just need
    the one-shot identity, not ongoing API access.

Env
---
  TWITCH_OAUTH_CLIENT_ID
  TWITCH_OAUTH_CLIENT_SECRET
  TWITCH_OAUTH_REDIRECT_URI   default: derived from request.url

When any of those (or OWNER_AUTH_TOKEN for the session HMAC) is unset,
the routes return 503 so deployments without Twitch sign-in keep working.
"""
from __future__ import annotations

import base64
import hmac
import json
import logging
import os
import secrets as _secrets
import time
from datetime import datetime, timezone
from hashlib import sha256
from urllib.parse import urlencode, urlparse

import aiosqlite
import httpx
from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import RedirectResponse

from . import auth_v2, beta_keys
from .database import DB_PATH


logger = logging.getLogger(__name__)

router = APIRouter()

# KEEP IN SYNC with miniapp.COMMUNITY_USER_SCOPES — duplicated here so this
# module doesn't import miniapp at module-load (avoids the circular pull).
COMMUNITY_USER_SCOPES = (
    "smdl.iptv",
    "smdl.downloader",
    "smdl.stickers",
    "smdl.streamtracker",
)
# Twitch sign-in additionally implies the streamer-consent surface, so
# users who came in via Twitch also have the "streamer" scope letting
# them reach the consent CRUD endpoints. Non-Twitch users don't, which
# is what we want — only authenticated Twitch users can set consent.
STREAMER_SCOPE = "smdl.streamer"

_AUTH_ENDPOINT     = "https://id.twitch.tv/oauth2/authorize"
_TOKEN_ENDPOINT    = "https://id.twitch.tv/oauth2/token"
_VALIDATE_ENDPOINT = "https://id.twitch.tv/oauth2/validate"
_USERS_ENDPOINT    = "https://api.twitch.tv/helix/users"

_STATE_COOKIE = "smdl_tw_oauth_state"
_STATE_TTL_SEC = 600
_SESSION_COOKIE = "sentinel_apk_session"
_SESSION_COOKIE_DOMAIN = ".az-sentinel.xyz"
_SESSION_COOKIE_TTL_SEC = 90 * 24 * 3600


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _client_id() -> str:
    return os.environ.get("TWITCH_OAUTH_CLIENT_ID", "").strip()


def _client_secret() -> str:
    return os.environ.get("TWITCH_OAUTH_CLIENT_SECRET", "").strip()


def _signing_secret() -> str:
    """HMAC secret for the state cookie + the session cookie. Same value
    auth_v2 already uses."""
    return os.environ.get("OWNER_AUTH_TOKEN", "").strip()


def is_configured() -> bool:
    return bool(_client_id() and _client_secret() and _signing_secret())


def _redirect_uri(request: Request) -> str:
    env = os.environ.get("TWITCH_OAUTH_REDIRECT_URI", "").strip()
    if env:
        return env
    base = str(request.base_url).rstrip("/")
    return f"{base}/auth/twitch/callback"


# ── State cookie (signed; carries `next` + nonce across the redirect) ──────


def _sign_state(payload: dict) -> str:
    body_json = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    body_b64 = base64.urlsafe_b64encode(body_json.encode()).rstrip(b"=").decode()
    sig = hmac.new(_signing_secret().encode(), body_b64.encode(), sha256).hexdigest()
    return f"{body_b64}.{sig}"


def _verify_state(token: str) -> dict | None:
    if not token or "." not in token:
        return None
    body_b64, sig = token.rsplit(".", 1)
    expected = hmac.new(_signing_secret().encode(), body_b64.encode(), sha256).hexdigest()
    if not hmac.compare_digest(expected, sig):
        return None
    try:
        body_json = base64.urlsafe_b64decode(body_b64 + "==").decode()
        return json.loads(body_json)
    except Exception:
        return None


def _safe_next(raw: str | None) -> str:
    """Same-origin paths only — defeats open-redirect attacks."""
    if not raw:
        return "/app?tab=streamer"
    parsed = urlparse(raw)
    if parsed.scheme or parsed.netloc:
        return "/app?tab=streamer"
    if not raw.startswith("/"):
        return "/app?tab=streamer"
    return raw


# ── DB helpers ─────────────────────────────────────────────────────────────


async def _upsert_identity(*, twitch_user_id: str, login: str, display: str | None,
                            broadcaster_type: str | None, email: str | None,
                            picture: str | None) -> None:
    now = _now_iso()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO twitch_identities
                (twitch_user_id, twitch_login, twitch_display,
                 broadcaster_type, email, profile_image_url,
                 first_seen, last_seen)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(twitch_user_id) DO UPDATE SET
                twitch_login      = excluded.twitch_login,
                twitch_display    = excluded.twitch_display,
                broadcaster_type  = excluded.broadcaster_type,
                email             = excluded.email,
                profile_image_url = excluded.profile_image_url,
                last_seen         = excluded.last_seen
        """, (twitch_user_id, (login or "").lower(), display,
              broadcaster_type, email, picture, now, now))
        await db.commit()


async def get_identity(twitch_user_id: str) -> dict | None:
    """Look up a Twitch identity row by user id."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM twitch_identities WHERE twitch_user_id = ?",
            (twitch_user_id,),
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


async def get_identity_by_login(login: str) -> dict | None:
    """Look up a Twitch identity by channel name (login). Case-insensitive."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM twitch_identities WHERE twitch_login = ?",
            ((login or "").lower(),),
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


# ── Routes ──────────────────────────────────────────────────────────────────


@router.get("/auth/twitch/start")
async def twitch_start(request: Request, next: str | None = None):
    if not is_configured():
        raise HTTPException(503, "Twitch sign-in is not configured on this deployment")
    nonce = _secrets.token_urlsafe(16)
    state_payload = {
        "n":    nonce,
        "next": _safe_next(next),
        "iat":  int(time.time()),
    }
    state_token = _sign_state(state_payload)
    params = {
        "client_id":     _client_id(),
        "redirect_uri":  _redirect_uri(request),
        "response_type": "code",
        # user:read:email is enough to confirm authentic identity.
        # Add scopes here ONLY if we ever need ongoing Helix API access
        # on the streamer's behalf — currently we don't.
        "scope":         "user:read:email",
        "state":         state_token,
        "force_verify":  "true",
    }
    url = f"{_AUTH_ENDPOINT}?{urlencode(params)}"
    response = RedirectResponse(url=url, status_code=302)
    response.set_cookie(
        _STATE_COOKIE, state_token,
        max_age=_STATE_TTL_SEC, httponly=True, secure=True,
        samesite="lax", path="/auth/twitch",
    )
    return response


@router.get("/auth/twitch/callback")
async def twitch_callback(request: Request, code: str | None = None,
                          state: str | None = None, error: str | None = None):
    if error:
        raise HTTPException(401, f"twitch_oauth_error:{error}")
    if not is_configured():
        raise HTTPException(503, "Twitch sign-in is not configured on this deployment")
    if not code or not state:
        raise HTTPException(400, "missing code/state")

    # 1. CSRF check: cookie state must match URL state, and the signed
    #    payload must verify + be fresh.
    cookie_state = request.cookies.get(_STATE_COOKIE, "")
    if cookie_state != state:
        raise HTTPException(401, "state_mismatch")
    state_payload = _verify_state(state)
    if state_payload is None:
        raise HTTPException(401, "bad_state")
    if (int(time.time()) - int(state_payload.get("iat", 0))) > _STATE_TTL_SEC:
        raise HTTPException(401, "state_expired")
    next_url = _safe_next(state_payload.get("next"))

    # 2. Exchange code → tokens.
    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            tok = await client.post(_TOKEN_ENDPOINT, data={
                "client_id":     _client_id(),
                "client_secret": _client_secret(),
                "code":          code,
                "grant_type":    "authorization_code",
                "redirect_uri":  _redirect_uri(request),
            })
        except httpx.HTTPError as e:
            logger.warning("twitch token exchange failed: %s", e)
            raise HTTPException(502, "twitch_token_exchange_failed")
    if tok.status_code != 200:
        logger.warning("twitch token exchange http %s: %s",
                       tok.status_code, tok.text[:300])
        raise HTTPException(401, "twitch_token_rejected")
    tok_data = tok.json()
    access_token = tok_data.get("access_token")
    if not access_token:
        raise HTTPException(401, "no_access_token")

    # 3. Validate the access_token via /oauth2/validate. This confirms
    #    the token is for OUR client_id (audience check) and is fresh.
    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            v = await client.get(_VALIDATE_ENDPOINT, headers={
                "Authorization": f"OAuth {access_token}",
            })
        except httpx.HTTPError as e:
            logger.warning("twitch validate failed: %s", e)
            raise HTTPException(502, "twitch_validate_failed")
    if v.status_code != 200:
        raise HTTPException(401, "token_invalid")
    val = v.json()
    if val.get("client_id") != _client_id():
        raise HTTPException(401, "client_id_mismatch")
    twitch_user_id_from_validate = str(val.get("user_id") or "")
    if not twitch_user_id_from_validate:
        raise HTTPException(401, "no_user_id_from_validate")

    # 4. Fetch /helix/users for richer profile metadata (display_name,
    #    broadcaster_type, profile_image, email). Bearer + Client-Id —
    #    the Twitch-specific dual-header dance.
    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            u = await client.get(_USERS_ENDPOINT, headers={
                "Authorization": f"Bearer {access_token}",
                "Client-Id":     _client_id(),
            })
        except httpx.HTTPError as e:
            logger.warning("twitch helix/users failed: %s", e)
            raise HTTPException(502, "twitch_users_failed")
    if u.status_code != 200:
        logger.warning("twitch helix/users http %s: %s", u.status_code, u.text[:300])
        raise HTTPException(401, "users_rejected")
    users_data = u.json().get("data") or []
    if not users_data:
        raise HTTPException(401, "no_user_data")
    profile = users_data[0]
    twitch_user_id = str(profile.get("id") or "")
    if twitch_user_id != twitch_user_id_from_validate:
        # Cross-check: validate and users should return the same user.
        # Mismatch means something's wrong (token mix-up, race, etc.).
        raise HTTPException(401, "user_id_cross_check_failed")

    # 5. Upsert identity directory.
    await _upsert_identity(
        twitch_user_id=twitch_user_id,
        login=(profile.get("login") or ""),
        display=(profile.get("display_name") or None),
        broadcaster_type=(profile.get("broadcaster_type") or ""),
        email=(profile.get("email") or None),
        picture=(profile.get("profile_image_url") or None),
    )

    # 6. Mint v2 session cookie. user_id="twitch:<id>" carries the
    #    provider prefix the rest of the auth machinery (grant_transport,
    #    require_scope) already recognises. Add STREAMER_SCOPE on top of
    #    the community surface so this session can reach the consent
    #    CRUD endpoints.
    user_id_str = f"twitch:{twitch_user_id}"
    extras = await beta_keys.live_extra_scopes_for(user_id_str)
    scopes = sorted(set(COMMUNITY_USER_SCOPES) | set(extras) | {STREAMER_SCOPE})
    cookie = auth_v2.issue_v2_cookie(_signing_secret(), user_id_str, scopes)

    response = RedirectResponse(url=next_url, status_code=302)
    response.delete_cookie(_STATE_COOKIE, path="/auth/twitch")
    response.set_cookie(
        _SESSION_COOKIE, cookie,
        max_age=_SESSION_COOKIE_TTL_SEC,
        httponly=True, secure=True, samesite="lax",
        domain=_SESSION_COOKIE_DOMAIN, path="/",
    )
    return response


@router.post("/auth/twitch/signout")
async def twitch_signout(response: Response):
    response.delete_cookie(_SESSION_COOKIE, domain=_SESSION_COOKIE_DOMAIN, path="/")
    return {"ok": True}
