"""Bearer signed-grant transport — the wire contract for entitlement enforcement.

The commercial gate (`entitlements.require_entitlement`, 402) needs to know which
entitlements a request carries. This module is the transport: the APK caches the
signed grant it got from `/api/license/validate` or `/api/billing/play/verify`
and replays it on each request as a header:

    X-Sentinel-Grant: <base64url(JSON grant)>

The grant is self-describing and tamper-evident (HMAC, see
`licensing.verify_grant`), so the server stays stateless — no per-request
registry call. A request with no/invalid grant header is treated as an
anonymous caller carrying exactly the *free* plan's entitlements, so free
capabilities keep working while paid ones 402.

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
import json

from fastapi import Request

from . import edition, entitlements, licensing


GRANT_HEADER = "X-Sentinel-Grant"


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


def enforcement_active() -> bool:
    """Whether the commercial entitlement gate applies on this deployment.

    The private edition is the owner's full box and is not license/plan-gated,
    so commercial caps are never withheld there. Distributed builds enforce.
    """
    return not edition.is_private()


def grant_from_request(request: Request) -> dict:
    """Decode + verify the caller's grant from the header. Returns the verified
    grant, or an anonymous (free) grant when the header is absent, malformed, or
    fails signature/freshness verification. Never raises — routes gate on the
    entitlements in the returned grant, not on its presence.
    """
    raw = request.headers.get(GRANT_HEADER)
    if not raw:
        return _anon_grant()
    try:
        padded = raw + "=" * (-len(raw) % 4)
        grant = json.loads(base64.urlsafe_b64decode(padded))
    except Exception:
        return _anon_grant()
    if not isinstance(grant, dict) or not licensing.verify_grant(grant):
        return _anon_grant()
    return grant


def requires(cap: str):
    """FastAPI dependency factory: 402 unless the caller's verified grant carries
    `cap`. On the private edition (`enforcement_active()` is False) it resolves
    the grant but never blocks. Returns the resolved grant so the route body can
    read plan/seat limits if needed.
    """

    async def _dep(request: Request) -> dict:
        grant = grant_from_request(request)
        if enforcement_active():
            entitlements.require_entitlement(grant, cap)
        return grant

    return _dep
