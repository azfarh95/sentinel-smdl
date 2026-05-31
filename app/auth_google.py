"""Google OIDC sign-in for the community / play SMDL deployments.

Why
---
The public TV / Play builds front Telegram-less users — the Mini App
isn't the entry-point on a TWA opened from Google Play. Those users
need a way to sign in so the free-registered tier (favorites / sync)
and the premium manifest can attach a plan to their identity. Google
OIDC is the canonical Android-side identity.

Flow
----
    GET  /auth/google/start?next=<safe-path>
         → 302 to Google authorise URL; mints a signed state cookie
           carrying (next, nonce). PKCE not used yet — confidential
           client (server-side secret) is sufficient for now.

    GET  /auth/google/callback?code=…&state=…
         → exchanges code for id_token (POST tokens endpoint),
           verifies the id_token via Google's tokeninfo endpoint
           (one HTTP round-trip, no local JWKS / JWT-RS256 needed),
           upserts oauth_identities, issues a v2 session cookie with
           user_id="google:<sub>" + scopes = COMMUNITY_USER_SCOPES
           ∪ beta_keys.live_extra_scopes_for("google:<sub>"), then
           302s to the saved next URL.

The session cookie HMAC secret is the same OWNER_AUTH_TOKEN used
across all SMDL auth (the secret is per-deployment — smdl-tv has its
own fresh value, distinct from the owner box).

Env
---
    GOOGLE_OAUTH_CLIENT_ID
    GOOGLE_OAUTH_CLIENT_SECRET
    GOOGLE_OAUTH_REDIRECT_URI   default: derived from request.url

Endpoints are no-ops with a clear 503 when any of those is unset, so
deployments without Google sign-in (the owner box, or smdl-tv before
the Google Cloud project is created) keep running.
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

# Mirrors miniapp.COMMUNITY_USER_SCOPES — duplicated here to keep this module
# importable without pulling miniapp at module load. KEEP IN SYNC with
# app/miniapp.py:COMMUNITY_USER_SCOPES.
COMMUNITY_USER_SCOPES = (
    "smdl.iptv",
    "smdl.downloader",
    "smdl.stickers",
    "smdl.streamtracker",
)

_AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
_TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
_TOKENINFO_ENDPOINT = "https://oauth2.googleapis.com/tokeninfo"

_STATE_COOKIE = "smdl_g_oauth_state"
_STATE_TTL_SEC = 600  # state must round-trip in under 10 minutes
_SESSION_COOKIE = "sentinel_apk_session"
_SESSION_COOKIE_DOMAIN = ".az-sentinel.xyz"
_SESSION_COOKIE_TTL_SEC = 90 * 24 * 3600


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _client_id() -> str:
    return os.environ.get("GOOGLE_OAUTH_CLIENT_ID", "").strip()


def _client_secret() -> str:
    return os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET", "").strip()


def _signing_secret() -> str:
    """HMAC secret for the state cookie + the session cookie. Reuses the
    deployment's OWNER_AUTH_TOKEN — same value already used by auth_v2."""
    return os.environ.get("OWNER_AUTH_TOKEN", "").strip()


def is_configured() -> bool:
    return bool(_client_id() and _client_secret() and _signing_secret())


def _redirect_uri(request: Request) -> str:
    """Where Google should send the user back. Prefer the env value (must
    match Google Cloud Console exactly); fall back to deriving from the
    request origin so dev/staging don't need a separate Cloud project."""
    env = os.environ.get("GOOGLE_OAUTH_REDIRECT_URI", "").strip()
    if env:
        return env
    base = str(request.base_url).rstrip("/")
    return f"{base}/auth/google/callback"


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
    """Only allow same-origin paths to defeat open-redirect attacks. Defaults
    to `/` when missing or hostile."""
    if not raw:
        return "/"
    parsed = urlparse(raw)
    if parsed.scheme or parsed.netloc:
        return "/"
    if not raw.startswith("/"):
        return "/"
    return raw


# ── DB helpers (oauth_identities) ───────────────────────────────────────────


async def _upsert_identity(*, provider: str, subject: str,
                            email: str | None, name: str | None,
                            picture: str | None) -> None:
    now = _now_iso()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO oauth_identities
                (provider, subject, email, name, picture_url,
                 first_seen, last_seen)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(provider, subject) DO UPDATE SET
                email       = excluded.email,
                name        = excluded.name,
                picture_url = excluded.picture_url,
                last_seen   = excluded.last_seen
        """, (provider, subject, email, name, picture, now, now))
        await db.commit()


# ── Routes ──────────────────────────────────────────────────────────────────


@router.get("/auth/google/start")
async def google_start(request: Request, next: str | None = None):
    """Begin the OIDC flow. Redirects the user to Google's consent page."""
    if not is_configured():
        raise HTTPException(503, "Google sign-in is not configured on this deployment")
    nonce = _secrets.token_urlsafe(16)
    state_payload = {
        "n": nonce,
        "next": _safe_next(next),
        "iat": int(time.time()),
    }
    state_token = _sign_state(state_payload)
    params = {
        "client_id": _client_id(),
        "redirect_uri": _redirect_uri(request),
        "response_type": "code",
        "scope": "openid email profile",
        "state": state_token,
        "nonce": nonce,
        "prompt": "select_account",
        "access_type": "online",
        "include_granted_scopes": "true",
    }
    url = f"{_AUTH_ENDPOINT}?{urlencode(params)}"
    response = RedirectResponse(url=url, status_code=302)
    response.set_cookie(
        _STATE_COOKIE, state_token,
        max_age=_STATE_TTL_SEC, httponly=True, secure=True,
        samesite="lax", path="/auth/google",
    )
    return response


@router.get("/auth/google/callback")
async def google_callback(request: Request, code: str | None = None,
                          state: str | None = None, error: str | None = None):
    """Exchange the auth code, verify the id_token, issue a session."""
    if error:
        # User declined / consent failed — Google sends ?error=access_denied
        raise HTTPException(401, f"google_oauth_error:{error}")
    if not is_configured():
        raise HTTPException(503, "Google sign-in is not configured on this deployment")
    if not code or not state:
        raise HTTPException(400, "missing code/state")

    # 1. Validate state against the cookie set in /start. Both must verify and
    #    must be the same value — prevents CSRF (attacker can't forge state)
    #    and replay (cookie has 10-minute TTL).
    cookie_state = request.cookies.get(_STATE_COOKIE, "")
    if cookie_state != state:
        raise HTTPException(401, "state_mismatch")
    state_payload = _verify_state(state)
    if state_payload is None:
        raise HTTPException(401, "bad_state")
    if (int(time.time()) - int(state_payload.get("iat", 0))) > _STATE_TTL_SEC:
        raise HTTPException(401, "state_expired")
    nonce_expected = state_payload.get("n") or ""
    next_url = _safe_next(state_payload.get("next"))

    # 2. Exchange code → tokens.
    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            tok = await client.post(_TOKEN_ENDPOINT, data={
                "code": code,
                "client_id": _client_id(),
                "client_secret": _client_secret(),
                "redirect_uri": _redirect_uri(request),
                "grant_type": "authorization_code",
            })
        except httpx.HTTPError as e:
            logger.warning("google token exchange failed: %s", e)
            raise HTTPException(502, "google_token_exchange_failed")
    if tok.status_code != 200:
        logger.warning("google token exchange http %s: %s",
                       tok.status_code, tok.text[:300])
        raise HTTPException(401, "google_token_rejected")
    tok_data = tok.json()
    id_token = tok_data.get("id_token")
    if not id_token:
        raise HTTPException(401, "no_id_token")

    # 3. Verify id_token. Using Google's tokeninfo endpoint as the verifier
    #    is the simplest correct path — Google fully validates signature +
    #    issuer + audience + expiry server-side and returns the parsed
    #    claims as JSON. Trade-off: one HTTP round-trip per sign-in (not
    #    per request). Acceptable here; revisit with local JWKS if QPS
    #    becomes a concern.
    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            ti = await client.get(_TOKENINFO_ENDPOINT,
                                  params={"id_token": id_token})
        except httpx.HTTPError as e:
            logger.warning("google tokeninfo failed: %s", e)
            raise HTTPException(502, "google_tokeninfo_failed")
    if ti.status_code != 200:
        logger.warning("google tokeninfo http %s: %s",
                       ti.status_code, ti.text[:300])
        raise HTTPException(401, "id_token_rejected")
    claims = ti.json()

    if claims.get("aud") != _client_id():
        raise HTTPException(401, "aud_mismatch")
    iss = claims.get("iss", "")
    if iss not in ("accounts.google.com", "https://accounts.google.com"):
        raise HTTPException(401, "iss_mismatch")
    if claims.get("nonce", "") != nonce_expected:
        raise HTTPException(401, "nonce_mismatch")
    sub = (claims.get("sub") or "").strip()
    if not sub:
        raise HTTPException(401, "no_subject")

    # 4. Upsert identity directory.
    await _upsert_identity(
        provider="google",
        subject=sub,
        email=(claims.get("email") or None),
        name=(claims.get("name") or None),
        picture=(claims.get("picture") or None),
    )

    # 5. Mint v2 session cookie. Identity is "google:<sub>" so downstream
    #    (grant_transport, premium lookup) can route on the provider prefix.
    #    Base scopes are the community user surface; extra scopes come from
    #    every live beta key this user has redeemed.
    user_id = f"google:{sub}"
    extras = await beta_keys.live_extra_scopes_for(user_id)
    scopes = sorted(set(COMMUNITY_USER_SCOPES) | set(extras))
    cookie = auth_v2.issue_v2_cookie(_signing_secret(), user_id, scopes)

    response = RedirectResponse(url=next_url, status_code=302)
    response.delete_cookie(_STATE_COOKIE, path="/auth/google")
    response.set_cookie(
        _SESSION_COOKIE, cookie,
        max_age=_SESSION_COOKIE_TTL_SEC,
        httponly=True, secure=True, samesite="lax",
        domain=_SESSION_COOKIE_DOMAIN, path="/",
    )
    return response


@router.post("/auth/google/signout")
async def google_signout(response: Response):
    """Drop the session cookie. Google itself stays signed-in (we don't
    have offline access; that's by design — re-prompt covers it)."""
    response.delete_cookie(_SESSION_COOKIE, domain=_SESSION_COOKIE_DOMAIN, path="/")
    return {"ok": True}
