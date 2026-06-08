"""Bearer signed-grant transport — the wire contract for entitlement enforcement.

The commercial gate (`entitlements.require_entitlement`, 402) needs to know which
entitlements a request carries. This module is the transport: the APK caches the
signed grant it got from `/api/license/validate` or `/api/billing/play/verify`
and replays it on each request as a header:

    X-Sentinel-Grant: <base64url(JSON grant)>

The grant is self-describing and tamper-evident (HMAC, see
`licensing.verify_grant`), so the server stays stateless — no per-request
registry call.

Resolution order (first match wins):

  1. Owner cookie (v1 session) → wildcard grant; enforcement bypass.
  2. Verified `X-Sentinel-Grant` header → its payload.
  3. Session identity (v2 cookie or initData) found in the premium manifest
     → server-trusted grant carrying that plan.
  4. Otherwise → anonymous free grant.

Free capabilities keep working in all cases; paid ones 402 until one of
(2)/(3) elevates the plan.

Enforcement policy
------------------
Commercial entitlements are a property of the *distributed* builds (community /
play), not the owner's own private box. `enforcement_active()` reflects that:
the private edition is the operator's full deployment and is not license-gated,
so it never 402s on a commercial cap. Community and play builds enforce.

Wiring a paid route is then a one-liner::

    from fastapi import Depends
    from . import grant_transport
    from .entitlements import CAP_TV_RECORDER

    @router.post("/api/iptv/channels/{id}/record",
                 dependencies=[Depends(grant_transport.requires(CAP_TV_RECORDER))])
    async def record(...): ...
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from urllib.parse import parse_qsl

from fastapi import HTTPException, Request

from . import auth_v2, edition, entitlements, licensing, premium
from .config import OWNER_CHAT_ID


GRANT_HEADER = "X-Sentinel-Grant"
_COOKIE_NAME = "sentinel_apk_session"

# Owner "preview as <plan>" simulation cookie. Carries a plan name. It is
# DOWNGRADE-ONLY and honoured exclusively when the request is the owner (see
# `_view_as_tier`), so a non-owner who forges it gets nothing — never an
# upgrade. Non-httponly on purpose: the front-end reads it to show the
# "previewing" banner. The cookie is NOT a credential; all trust is the
# server-side owner check.
VIEW_AS_COOKIE = "smdl_view_as"


def _anon_grant() -> dict:
    """A synthetic, server-trusted grant carrying the free plan. Used when the
    caller presents no (or an unverifiable) grant — anonymous == free tier."""
    return {
        "valid": True,
        "plan": "free",
        "entitlements": entitlements.entitlements_for("free"),
        "limits": {"seats": 1},
        "anonymous": True,
    }


def _owner_grant() -> dict:
    """Full-entitlement grant for an authenticated owner cookie. Mirrors the
    private edition's exemption — an owner remote-administering a community
    deployment via shared OWNER_AUTH_TOKEN gets every cap, never 402s."""
    return {
        "valid": True,
        "plan": "family",
        "entitlements": entitlements.entitlements_for("family"),
        "limits": {"seats": 99},
        "source": "owner_cookie",
    }


def enforcement_active() -> bool:
    """Whether the commercial entitlement gate applies on this deployment.

    The private edition is the owner's full box and is not license/plan-gated,
    so commercial caps are never withheld there. Distributed builds enforce.
    """
    return not edition.is_private()


def _header_grant(request: Request) -> dict | None:
    """Verified `X-Sentinel-Grant` payload, or None on missing/invalid."""
    raw = request.headers.get(GRANT_HEADER)
    if not raw:
        return None
    try:
        padded = raw + "=" * (-len(raw) % 4)
        grant = json.loads(base64.urlsafe_b64decode(padded))
    except Exception:
        return None
    if not isinstance(grant, dict) or not licensing.verify_grant(grant):
        return None
    return grant


def _identity_from_cookie(request: Request) -> tuple[str, str] | None:
    """Pull (identity_type, identity_value) from the session cookie.

    Returns ("owner", "owner") for the v1 owner cookie so callers can fast-path
    to the owner grant. v2 cookies encode the identity in `user_id`:
        google:<sub>   → ("google", "<sub>")
        tg:<chat_id>   → ("telegram", "<chat_id>")
        <numeric>      → ("telegram", "<numeric>")  (legacy)
        <slug>         → None (unmappable, e.g. beta-user slug)
    Returns None when no cookie / expired / unverifiable / unmappable.
    """
    secret = os.environ.get("OWNER_AUTH_TOKEN", "")
    if not secret:
        return None
    val = request.cookies.get(_COOKIE_NAME, "")
    if not val:
        return None
    try:
        payload = auth_v2.parse_session_cookie(val, secret)
    except HTTPException:
        return None
    if payload.get("expired"):
        return None
    # v1 owner cookie short-circuits.
    if payload.get("version") == "v1":
        return ("owner", "owner")
    uid = payload.get("user_id") or ""
    if uid == "owner":
        return ("owner", "owner")
    if uid.startswith("google:"):
        return ("google", uid.split(":", 1)[1])
    if uid.startswith("tg:"):
        return ("telegram", uid.split(":", 1)[1])
    if uid.isdigit():
        return ("telegram", uid)
    return None


def _identity_from_initdata(request: Request) -> tuple[str, str] | None:
    """Pull (telegram, chat_id) from a Telegram WebApp X-Init-Data header.

    Mirrors miniapp._validate_init_data minimally: HMAC + freshness check
    over the same data-check-string. We only need user.id, not the full
    payload, and we want this to stay independent of miniapp.py to avoid
    a circular import."""
    raw = (request.headers.get("x-init-data")
           or request.headers.get("x-telegram-init-data") or "")
    if not raw:
        return None
    bot_token = (
        os.environ.get("SMDL_BOT_TOKEN")
        or os.environ.get("BOT_TOKEN")
        or os.environ.get("TELEGRAM_BOT_TOKEN")
        or ""
    )
    if not bot_token:
        return None
    try:
        pairs = dict(parse_qsl(raw, strict_parsing=False))
    except Exception:
        return None
    received_hash = pairs.pop("hash", None)
    if not received_hash:
        return None
    data_check = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret_key, data_check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(received_hash, expected):
        return None
    try:
        auth_date = int(pairs.get("auth_date", "0"))
    except ValueError:
        return None
    # Same 1-hour freshness window as the Mini App gate.
    if auth_date and (time.time() - auth_date) > 3600:
        return None
    try:
        user = json.loads(pairs.get("user", "{}"))
    except Exception:
        return None
    uid = user.get("id")
    if not uid:
        return None
    return ("telegram", str(int(uid)))


async def _grant_from_session(request: Request) -> dict | None:
    """Session-identity → premium-manifest path. Returns:
        owner grant (full entitlements) on owner cookie;
        premium grant when the identity is listed in premium_users;
        None otherwise (caller falls back to anon)."""
    ident = _identity_from_cookie(request) or _identity_from_initdata(request)
    if ident is None:
        return None
    itype, ivalue = ident
    if itype == "owner":
        return _owner_grant()
    plan = await premium.lookup_plan(itype, ivalue)
    if plan is None:
        return None
    return premium.build_grant(plan)


def _is_owner_request(request: Request) -> bool:
    """True if the request is authenticated as the owner — via the v1 owner
    cookie OR an initData/v2 telegram identity matching OWNER_CHAT_ID. Used to
    gate the preview-as simulation so only the owner can downgrade themselves."""
    ident = _identity_from_cookie(request) or _identity_from_initdata(request)
    if ident is None:
        return False
    itype, ivalue = ident
    if itype == "owner":
        return True
    if itype == "telegram" and OWNER_CHAT_ID is not None:
        try:
            return int(ivalue) == int(OWNER_CHAT_ID)
        except (TypeError, ValueError):
            return False
    return False


def _view_as_tier(request: Request) -> str | None:
    """The owner's active 'preview as' plan, or None. Honoured ONLY for the
    owner and only for a known plan, so it can never escalate a non-owner."""
    raw = (request.cookies.get(VIEW_AS_COOKIE) or "").strip().lower()
    if not raw or raw == "owner" or raw not in entitlements.PLANS:
        return None
    if not _is_owner_request(request):
        return None
    return raw


async def resolve_grant(request: Request) -> dict:
    """Full grant resolution — owner simulation → header → owner-cookie →
    session-identity → anonymous. Never raises; routes gate on the returned
    entitlements, not on the presence of a grant."""
    # Owner "preview as <plan>" takes precedence: it replaces the owner's full
    # grant with a simulated community plan so the gates fire on the owner's
    # own (private) box. Tagged so `requires()` forces enforcement for it.
    sim = _view_as_tier(request)
    if sim is not None:
        g = premium.build_grant(sim)
        g["source"] = "owner_simulation"
        return g
    g = _header_grant(request)
    if g is not None:
        return g
    g = await _grant_from_session(request)
    if g is not None:
        return g
    return _anon_grant()


# Public sync wrapper kept for backwards-compat with the small number of
# call sites that only want the header-decoded grant without the session/
# manifest enrichment. New code should prefer `resolve_grant()` (async).
def grant_from_request(request: Request) -> dict:
    g = _header_grant(request)
    return g if g is not None else _anon_grant()


def requires(cap: str):
    """FastAPI dependency factory: 402 unless the caller's resolved grant
    carries `cap`. Resolution order: header → owner-cookie → session-identity
    premium lookup → anonymous free. On the private edition
    (`enforcement_active()` is False) it resolves the grant but never blocks.
    Returns the resolved grant so the route body can read plan/seat limits if
    needed.
    """

    async def _dep(request: Request) -> dict:
        grant = await resolve_grant(request)
        # Enforce on distributed builds, OR whenever the owner is previewing a
        # community plan on their own box (the simulated grant opts itself in).
        if enforcement_active() or grant.get("source") == "owner_simulation":
            entitlements.require_entitlement(grant, cap)
        return grant

    return _dep
