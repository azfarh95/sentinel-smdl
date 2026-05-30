"""Google Play Billing — purchase-token verification rail.

The second purchase rail (alongside license keys), resolving to the SAME
entitlement source of truth: a verified Play purchase maps product_id -> plan,
then `entitlements.enrich`-style capabilities. See the planning doc
`sentinel-docs/docs/planning/media-licensing-entitlements.md` (Billing section).

  Play Billing purchase ─▶ purchase token ─▶ verify (here) ─▶ plan ─▶ grant
  License key (sideload)  ─▶ validate ───────────────────────────────┘

Design:
- Google is the source of truth for the purchase. We re-verify on demand
  (the client re-checks periodically, mirroring the license grace model), so
  this module is stateless — no DB migration, no local purchase ledger.
- Dependency-OPTIONAL and credential-OPTIONAL. The rail is fully wired here;
  it ACTIVATES when the operator supplies a service account and installs
  google-auth. Until then every verify returns {"ok": False,
  "reason": "not_configured"} — it NEVER silently grants.
- No secret is ever read into the transcript or logged. Credentials come from
  PLAY_SERVICE_ACCOUNT_JSON (a path, or inline JSON) via the environment.

Operator setup (handoff — see launch runbook):
  PLAY_PACKAGE_NAME=com.azsentinel.smdltv
  PLAY_SERVICE_ACCOUNT_JSON=/secrets/play-sa.json   (or inline JSON)
  PLAY_PRODUCT_PLANS={"smdl.tv.plus.monthly":"plus", ...}   (optional override)
  pip install google-auth   (the only extra dep; imported lazily)
"""
from __future__ import annotations

import json
import logging
import os

import httpx

from . import entitlements

log = logging.getLogger("smdl.play_billing")

_API_ROOT = "https://androidpublisher.googleapis.com/androidpublisher/v3"
_SCOPE = "https://www.googleapis.com/auth/androidpublisher"

# Default product_id -> plan map for the TV-first catalogue. Override entirely
# with PLAY_PRODUCT_PLANS (JSON object) when the real SKUs are created in Play.
_DEFAULT_PRODUCT_PLANS = {
    "smdl.tv.plus.monthly": "plus",
    "smdl.tv.plus.yearly": "plus",
    "smdl.tv.family.monthly": "family",
    "smdl.tv.family.yearly": "family",
}


def package_name() -> str:
    return (os.environ.get("PLAY_PACKAGE_NAME") or "").strip()


def product_plan_map() -> dict[str, str]:
    raw = (os.environ.get("PLAY_PRODUCT_PLANS") or "").strip()
    if raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                return {str(k): str(v) for k, v in parsed.items()}
        except Exception:
            log.warning("PLAY_PRODUCT_PLANS is not valid JSON; using defaults")
    return dict(_DEFAULT_PRODUCT_PLANS)


def plan_for_product(product_id: str) -> str | None:
    return product_plan_map().get((product_id or "").strip())


def is_configured() -> bool:
    """True when a package name + a service-account credential are both present.

    Does not import google-auth here (kept lazy) — only checks that the
    operator has supplied the inputs needed to activate the rail.
    """
    return bool(package_name()) and bool(
        (os.environ.get("PLAY_SERVICE_ACCOUNT_JSON") or "").strip()
    )


def _load_sa_info() -> dict | None:
    """Service-account JSON from PLAY_SERVICE_ACCOUNT_JSON (path or inline).

    Returns the parsed dict, or None if absent/unreadable. The credential
    contents are never logged.
    """
    raw = (os.environ.get("PLAY_SERVICE_ACCOUNT_JSON") or "").strip()
    if not raw:
        return None
    try:
        if raw.startswith("{"):
            return json.loads(raw)
        if os.path.exists(raw):
            with open(raw, "r", encoding="utf-8") as fh:
                return json.load(fh)
    except Exception:
        log.warning("PLAY_SERVICE_ACCOUNT_JSON could not be read/parsed")
    return None


def _access_token() -> str | None:
    """Mint an OAuth2 access token for the androidpublisher scope.

    Uses google-auth lazily so it is an OPTIONAL dependency. Returns None when
    the library or the credential is unavailable — callers treat that as
    not_configured, never as a grant.
    """
    info = _load_sa_info()
    if not info:
        return None
    try:
        from google.oauth2 import service_account  # type: ignore
        from google.auth.transport.requests import Request as GoogleRequest  # type: ignore
    except Exception:
        log.warning("google-auth not installed; Play Billing rail inactive")
        return None
    try:
        creds = service_account.Credentials.from_service_account_info(
            info, scopes=[_SCOPE]
        )
        creds.refresh(GoogleRequest())
        return creds.token
    except Exception as exc:
        log.warning("Play service-account token refresh failed: %s", type(exc).__name__)
        return None


async def _get(url: str, token: str) -> tuple[int, dict]:
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(url, headers={"Authorization": f"Bearer {token}"})
    try:
        data = resp.json()
    except Exception:
        data = {}
    return resp.status_code, data


def _grant_from_plan(plan: str, *, source: str, seats: int = 1) -> dict:
    """Build a valid grant for a resolved plan, shaped like the license grant
    so the client treats both rails identically."""
    return {
        "valid": True,
        "source": source,
        "plan": plan,
        "entitlements": entitlements.entitlements_for(plan),
        "limits": {"seats": seats},
    }


async def verify(purchase_token: str, product_id: str, kind: str = "product") -> dict:
    """Verify a Play purchase/subscription token and return a grant.

    Returns a license-shaped grant on success, else {"valid": False, "reason"}.
    `kind` is "product" (one-time / managed) or "subscription".
    """
    purchase_token = (purchase_token or "").strip()
    product_id = (product_id or "").strip()
    if not purchase_token or not product_id:
        return {"valid": False, "reason": "malformed"}

    plan = plan_for_product(product_id)
    if not plan:
        return {"valid": False, "reason": "unknown_product"}

    if not is_configured():
        return {"valid": False, "reason": "not_configured"}

    token = _access_token()
    if not token:
        return {"valid": False, "reason": "not_configured"}

    pkg = package_name()
    if kind == "subscription":
        url = f"{_API_ROOT}/applications/{pkg}/purchases/subscriptionsv2/tokens/{purchase_token}"
    else:
        url = (
            f"{_API_ROOT}/applications/{pkg}/purchases/products/{product_id}"
            f"/tokens/{purchase_token}"
        )

    status, data = await _get(url, token)
    if status == 404:
        return {"valid": False, "reason": "not_found"}
    if status == 401 or status == 403:
        return {"valid": False, "reason": "verify_unauthorized"}
    if status >= 400:
        return {"valid": False, "reason": "verify_error"}

    if not _purchase_is_active(data, kind):
        return {"valid": False, "reason": "not_active"}

    return _grant_from_plan(plan, source="play_billing")


def _purchase_is_active(data: dict, kind: str) -> bool:
    """Interpret the androidpublisher response for an active entitlement.

    Subscriptions v2: subscriptionState ACTIVE or IN_GRACE_PERIOD.
    Products: purchaseState 0 (purchased) and not refunded/pending.
    """
    if kind == "subscription":
        state = data.get("subscriptionState")
        return state in ("SUBSCRIPTION_STATE_ACTIVE", "SUBSCRIPTION_STATE_IN_GRACE_PERIOD")
    # one-time / managed product
    if data.get("purchaseState", 0) != 0:  # 0=purchased, 1=cancelled, 2=pending
        return False
    # acknowledgementState 0=yet-to-acknowledge, 1=acknowledged — both are valid
    # purchases; we just confirm it isn't cancelled/pending above.
    return True
