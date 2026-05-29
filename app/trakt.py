"""Trakt.tv sync client — watch progress + scrobble for the Theater module.

Trakt is the de-facto watch-tracking service for movies/series. Sentinel
Media uses it to:
  • Scrobble what's being watched (so the user's Trakt timeline reflects
    Theater playback alongside Plex / Stremio / etc).
  • Surface the user's watchlist as a starter catalog inside Theater.
  • Record watched-state per episode so we know what to resume.

Auth: device-code OAuth flow. The user pastes the Trakt-shown code at
https://trakt.tv/activate; we poll /oauth/device/token until a refresh
token comes back. Token + refresh persisted to /config/trakt_token.json.

API docs: https://trakt.docs.apiary.io/

Single-user / personal use — no shared client.
"""
from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


_API_BASE = "https://api.trakt.tv"
_DEFAULT_TIMEOUT = 30
_TOKEN_PATH = Path(os.environ.get("TRAKT_TOKEN_PATH", "/config/trakt_token.json"))


def _client_id() -> Optional[str]:
    """Trakt API client id (the public part — embedded in the Mini App,
    safe to ship). Owner provides via env."""
    return (os.environ.get("TRAKT_CLIENT_ID") or "").strip() or None


def _client_secret() -> Optional[str]:
    """Trakt API client secret. Needed for the device-token exchange."""
    return (os.environ.get("TRAKT_CLIENT_SECRET") or "").strip() or None


class TraktError(Exception):
    pass


# ── Token persistence ──────────────────────────────────────────────────────
@dataclass
class TraktToken:
    access_token: str
    refresh_token: str
    expires_at: int     # epoch seconds
    scope: str = ""


def load_token() -> Optional[TraktToken]:
    """Read the cached token from /config/trakt_token.json. Returns None if
    not present or unparseable. Does NOT validate against the API."""
    if not _TOKEN_PATH.exists():
        return None
    try:
        with open(_TOKEN_PATH, "r", encoding="utf-8") as f:
            d = json.load(f)
        return TraktToken(
            access_token=d["access_token"],
            refresh_token=d["refresh_token"],
            expires_at=int(d.get("expires_at") or 0),
            scope=d.get("scope") or "",
        )
    except Exception as e:
        logger.warning("trakt: failed to load token: %s", e)
        return None


def save_token(t: TraktToken) -> None:
    _TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(_TOKEN_PATH, "w", encoding="utf-8") as f:
        json.dump({
            "access_token": t.access_token,
            "refresh_token": t.refresh_token,
            "expires_at": t.expires_at,
            "scope": t.scope,
        }, f, indent=2)


def clear_token() -> None:
    try:
        _TOKEN_PATH.unlink(missing_ok=True)
    except Exception:
        pass


# ── HTTP helpers ───────────────────────────────────────────────────────────
def _request(method: str, path: str, *, body: Optional[dict] = None,
              token: Optional[str] = None, timeout: int = _DEFAULT_TIMEOUT,
              ) -> dict:
    cid = _client_id()
    if not cid:
        raise TraktError("TRAKT_CLIENT_ID not configured")
    url = f"{_API_BASE}{path}"
    data = json.dumps(body).encode() if body is not None else None
    headers = {
        "Content-Type": "application/json",
        "trakt-api-version": "2",
        "trakt-api-key": cid,
        "User-Agent": "SMDL/Theater",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
    except urllib.error.HTTPError as e:
        body_text = ""
        try: body_text = e.read().decode()
        except Exception: pass
        raise TraktError(f"HTTP {e.code} — {e.reason} :: {body_text[:200]}")
    except urllib.error.URLError as e:
        raise TraktError(f"Network: {e}")
    if not raw:
        return {}
    try:
        return json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError as e:
        raise TraktError(f"Bad JSON: {e}")


# ── Device-code OAuth ──────────────────────────────────────────────────────
@dataclass
class DeviceCode:
    device_code: str          # the long code we poll with
    user_code: str            # the short code the user types at trakt.tv/activate
    verification_url: str     # always "https://trakt.tv/activate"
    expires_in: int
    interval: int             # seconds between polls


def device_code_init() -> DeviceCode:
    """Start the device-code flow. UI shows user_code + verification_url
    to the user; meanwhile we poll device_code_check(device_code) until
    they finish authorising on the trakt.tv page."""
    cid = _client_id()
    if not cid:
        raise TraktError("TRAKT_CLIENT_ID not configured")
    body = {"client_id": cid}
    # device/code uses unauthenticated POST
    req = urllib.request.Request(
        f"{_API_BASE}/oauth/device/code",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "User-Agent": "SMDL/Theater"},
    )
    with urllib.request.urlopen(req, timeout=_DEFAULT_TIMEOUT) as r:
        d = json.loads(r.read().decode())
    return DeviceCode(
        device_code=d["device_code"],
        user_code=d["user_code"],
        verification_url=d.get("verification_url") or "https://trakt.tv/activate",
        expires_in=int(d.get("expires_in") or 600),
        interval=int(d.get("interval") or 5),
    )


def device_code_check(device_code: str) -> Optional[TraktToken]:
    """Poll once. Returns None if user hasn't authorised yet (caller polls
    again after `interval` seconds). Returns the token once they do."""
    cid = _client_id(); secret = _client_secret()
    if not cid or not secret:
        raise TraktError("TRAKT_CLIENT_ID + TRAKT_CLIENT_SECRET required")
    body = {
        "code": device_code,
        "client_id": cid,
        "client_secret": secret,
    }
    req = urllib.request.Request(
        f"{_API_BASE}/oauth/device/token",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "User-Agent": "SMDL/Theater"},
    )
    try:
        with urllib.request.urlopen(req, timeout=_DEFAULT_TIMEOUT) as r:
            d = json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        # 400 = pending, 404 = expired, 409 = already used, etc — caller polls again
        if e.code == 400:
            return None
        raise TraktError(f"device/token HTTP {e.code}")
    tok = TraktToken(
        access_token=d["access_token"],
        refresh_token=d["refresh_token"],
        expires_at=int(time.time()) + int(d.get("expires_in") or 0),
        scope=d.get("scope") or "",
    )
    save_token(tok)
    return tok


def refresh_if_needed(t: TraktToken, *, leeway_s: int = 600) -> TraktToken:
    """Refresh the access token if within `leeway_s` of expiry."""
    if t.expires_at - time.time() > leeway_s:
        return t
    cid = _client_id(); secret = _client_secret()
    if not cid or not secret:
        raise TraktError("client creds missing")
    body = {
        "refresh_token": t.refresh_token,
        "client_id": cid,
        "client_secret": secret,
        "redirect_uri": "urn:ietf:wg:oauth:2.0:oob",
        "grant_type": "refresh_token",
    }
    j = _request("POST", "/oauth/token", body=body)
    nt = TraktToken(
        access_token=j["access_token"],
        refresh_token=j.get("refresh_token") or t.refresh_token,
        expires_at=int(time.time()) + int(j.get("expires_in") or 0),
        scope=j.get("scope") or t.scope,
    )
    save_token(nt)
    return nt


# ── Scrobble ───────────────────────────────────────────────────────────────
def _scrobble_payload(*, imdb_id: str, type_: str, season: Optional[int],
                      episode: Optional[int], progress_pct: float) -> dict:
    """Build the Trakt scrobble payload. Movie OR episode shape — both
    addressed by their imdb_id."""
    body: dict = {"progress": max(0.0, min(100.0, progress_pct))}
    # Strip the SxxExx suffix from a Stremio content_id if present
    parent_imdb = imdb_id.split(":")[0]
    if type_ == "movie":
        body["movie"] = {"ids": {"imdb": parent_imdb}}
    else:
        # Series episode — addresses the show by imdb; the season/episode
        # numbers identify the specific entry.
        body["show"] = {"ids": {"imdb": parent_imdb}}
        body["episode"] = {"season": season or 0, "number": episode or 0}
    return body


def scrobble_start(token: TraktToken, *, imdb_id: str, type_: str,
                    season: Optional[int] = None, episode: Optional[int] = None,
                    progress_pct: float = 0.0) -> dict:
    """POST /scrobble/start — opens a 'now watching' state."""
    return _request("POST", "/scrobble/start", token=token.access_token,
                     body=_scrobble_payload(imdb_id=imdb_id, type_=type_,
                                              season=season, episode=episode,
                                              progress_pct=progress_pct))


def scrobble_pause(token: TraktToken, *, imdb_id: str, type_: str,
                    season: Optional[int] = None, episode: Optional[int] = None,
                    progress_pct: float = 0.0) -> dict:
    return _request("POST", "/scrobble/pause", token=token.access_token,
                     body=_scrobble_payload(imdb_id=imdb_id, type_=type_,
                                              season=season, episode=episode,
                                              progress_pct=progress_pct))


def scrobble_stop(token: TraktToken, *, imdb_id: str, type_: str,
                   season: Optional[int] = None, episode: Optional[int] = None,
                   progress_pct: float = 100.0) -> dict:
    """POST /scrobble/stop — finalises as watched if progress > 80%."""
    return _request("POST", "/scrobble/stop", token=token.access_token,
                     body=_scrobble_payload(imdb_id=imdb_id, type_=type_,
                                              season=season, episode=episode,
                                              progress_pct=progress_pct))


# ── Watchlist + playback (read-side helpers) ───────────────────────────────
def watchlist(token: TraktToken, *, type_: str = "movies") -> list[dict]:
    """User's Trakt watchlist. Returned raw — caller maps to MetaItem shape."""
    return _request("GET", f"/sync/watchlist/{type_}", token=token.access_token)


def playback_progress(token: TraktToken) -> list[dict]:
    """All in-progress titles across devices — Trakt's resume API.
    Theater Library view shows these alongside cached-only entries."""
    return _request("GET", "/sync/playback", token=token.access_token)
