"""License-key authority for the Sentinel Media APKs.

This (private operator) instance issues and validates the license keys that
gate the distributed Community and Family APKs. Phase 1 keeps the whole
authority local to SMDL; a later phase upstreams issued/revoked keys to the
central Sentinel License Registry for cross-instance revocation.

Key shape (what the owner hands a user):

    SMDL-FAM.<key_id>.<secret>      # Family (full private edition)
    SMDL-COM.<key_id>.<secret>      # Community (official-iframe edition)

  - key_id  : random hex, the public row id (safe to log)
  - secret  : random url-safe token, the bearer credential
  - We store only HMAC-SHA256(signing_secret, secret) — a DB leak never
    yields usable keys, and the HMAC proves the key was issued by us.

Crypto is stdlib-only (hmac / hashlib / secrets) to match SMDL's deps.

Validation is ONLINE: the APK posts the key + a device id to
/api/license/validate, which checks status + expiry + seat limit and returns
a grant carrying GRACE_SECONDS. The APK caches the grant and keeps working
offline until the grace window lapses, then must re-check — which is how a
revoked or expired key eventually stops working on the device.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
from datetime import datetime, timedelta, timezone

# ── Tiers ────────────────────────────────────────────────────────────────────
TIER_COMMUNITY = "community"
TIER_FAMILY = "family"
TIERS = (TIER_COMMUNITY, TIER_FAMILY)

# tier ↔ key-code prefix
_TIER_TO_PREFIX = {TIER_COMMUNITY: "SMDL-COM", TIER_FAMILY: "SMDL-FAM"}
_PREFIX_TO_TIER = {v: k for k, v in _TIER_TO_PREFIX.items()}

# Offline grace: how long an APK may keep trusting a cached grant before it
# must re-validate online. 7 days balances revocation latency against letting
# a device keep working through a stretch with no connectivity.
GRACE_SECONDS = 7 * 24 * 3600

# Sensible bounds for owner input.
MIN_SEATS = 1
MAX_SEATS = 50
MIN_VALID_DAYS = 1
MAX_VALID_DAYS = 3650  # ~10y; "perpetual-ish" without an unbounded field


class LicensingNotConfigured(RuntimeError):
    """Raised when no signing secret is available — keys can be neither
    issued nor validated until the operator sets one."""


def _signing_secret() -> str:
    """The HMAC key for license secrets. Prefer a dedicated secret; fall back
    to OWNER_AUTH_TOKEN (already present on the operator instance) so the
    feature works out of the box. Raises if neither is set."""
    sec = (os.environ.get("LICENSE_SIGNING_SECRET")
           or os.environ.get("OWNER_AUTH_TOKEN") or "").strip()
    if not sec:
        raise LicensingNotConfigured(
            "set LICENSE_SIGNING_SECRET (or OWNER_AUTH_TOKEN) to issue/validate keys")
    return sec


def is_configured() -> bool:
    try:
        _signing_secret()
        return True
    except LicensingNotConfigured:
        return False


def sign_secret(secret: str) -> str:
    """HMAC-SHA256 of the bearer secret under the signing key. This is what we
    persist; validation recomputes and constant-time compares."""
    return hmac.new(_signing_secret().encode(),
                    secret.encode(), hashlib.sha256).hexdigest()


def verify_secret(secret: str, stored_hash: str) -> bool:
    try:
        return hmac.compare_digest(sign_secret(secret), stored_hash or "")
    except LicensingNotConfigured:
        return False


def normalise_tier(tier: str) -> str:
    t = (tier or "").strip().lower()
    if t not in TIERS:
        raise ValueError(f"unknown tier {tier!r}; expected one of {TIERS}")
    return t


def generate_key(tier: str) -> tuple[str, str, str]:
    """Mint a new key. Returns (key_id, secret, key_code). The caller stores
    key_id + sign_secret(secret); the key_code is shown to the owner ONCE."""
    t = normalise_tier(tier)
    key_id = secrets.token_hex(5)            # 10 hex chars
    secret = secrets.token_urlsafe(20)       # ~27 url-safe chars, no '.'
    key_code = f"{_TIER_TO_PREFIX[t]}.{key_id}.{secret}"
    return key_id, secret, key_code


def parse_key_code(code: str) -> tuple[str, str, str] | None:
    """Split a key code into (tier, key_id, secret). Returns None if the shape
    or prefix is wrong. Does NOT verify the secret — that needs the DB row."""
    if not code:
        return None
    parts = code.strip().split(".")
    if len(parts) != 3:
        return None
    prefix, key_id, secret = parts
    tier = _PREFIX_TO_TIER.get(prefix)
    if not tier or not key_id or not secret:
        return None
    return tier, key_id, secret


# ── Expiry helpers ───────────────────────────────────────────────────────────


def expiry_from_days(days: int, *, now: datetime | None = None) -> str:
    """ISO expiry `days` from now. Every key is time-limited, so this is always
    called at issue time. Clamps to the allowed range."""
    d = max(MIN_VALID_DAYS, min(int(days), MAX_VALID_DAYS))
    base = now or datetime.now(timezone.utc)
    return (base + timedelta(days=d)).isoformat()


def is_expired(expires_at: str, *, now: datetime | None = None) -> bool:
    try:
        exp = datetime.fromisoformat(expires_at)
    except Exception:
        return True  # unparseable expiry = treat as expired (fail closed)
    if exp.tzinfo is None:
        exp = exp.replace(tzinfo=timezone.utc)
    return exp <= (now or datetime.now(timezone.utc))


def clamp_seats(seats: int) -> int:
    return max(MIN_SEATS, min(int(seats), MAX_SEATS))


def build_grant(row: dict) -> dict:
    """Shape the success response the APK caches. `row` is a license_keys row.

    The caller enriches this with plan/entitlements and then runs it through
    `sign_grant` so the cached copy is tamper-evident on a rooted device."""
    return {
        "valid": True,
        "tier": row["tier"],
        "key_id": row["key_id"],
        "expires_at": row["expires_at"],
        "grace_seconds": GRACE_SECONDS,
        "issued_to": row.get("issued_to"),
    }


# ── Signed grants ────────────────────────────────────────────────────────────
# A grant is a flat JSON object the APK caches and replays. Without a signature
# a rooted device could edit `entitlements`/`plan` in its cache and unlock paid
# caps offline. We HMAC the canonical JSON of every field (except the signature
# itself) under the same signing secret, so any edit invalidates it. The same
# secret signs license-key and Play-Billing grants — one verification path.

GRANT_SIG_ALG = "HS256-grant-v1"


def _canonical_grant_bytes(grant: dict) -> bytes:
    """Deterministic bytes to HMAC: every field except `sig`, key-sorted with
    tight separators so sign-time and verify-time serialisation are identical."""
    payload = {k: v for k, v in grant.items() if k != "sig"}
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode()


def sign_grant(grant: dict, *, now: datetime | None = None) -> dict:
    """Attach an HMAC signature so the cached grant can't be forged. Stamps
    `iat` (issued-at, epoch seconds) if absent, sets `sig_alg`, then signs the
    canonical JSON of all fields except `sig`. Mutates and returns `grant`.

    >>> import os
    >>> os.environ["LICENSE_SIGNING_SECRET"] = "doctest-secret"
    >>> g = sign_grant({"valid": True, "plan": "plus", "entitlements": ["a"]})
    >>> verify_grant(g)
    True
    >>> g["plan"] = "family"          # tamper with the cached entitlement
    >>> verify_grant(g)
    False
    """
    if "iat" not in grant:
        base = now or datetime.now(timezone.utc)
        grant["iat"] = int(base.timestamp())
    grant["sig_alg"] = GRANT_SIG_ALG
    grant.pop("sig", None)  # never sign over a stale signature
    grant["sig"] = hmac.new(
        _signing_secret().encode(), _canonical_grant_bytes(grant), hashlib.sha256
    ).hexdigest()
    return grant


def verify_grant(grant: dict, *, now: datetime | None = None) -> bool:
    """True iff the grant carries our signature, its signature is within the
    offline grace window, and the underlying key hasn't expired. Fail-closed on
    anything missing or misconfigured.

    >>> import os
    >>> os.environ["LICENSE_SIGNING_SECRET"] = "doctest-secret"
    >>> verify_grant({"valid": True})            # unsigned
    False
    >>> verify_grant({"valid": True, "sig": "deadbeef", "sig_alg": "HS256-grant-v1"})
    False
    """
    if not isinstance(grant, dict):
        return False
    sig = grant.get("sig")
    if not sig or grant.get("sig_alg") != GRANT_SIG_ALG:
        return False
    try:
        expected = hmac.new(
            _signing_secret().encode(), _canonical_grant_bytes(grant), hashlib.sha256
        ).hexdigest()
    except LicensingNotConfigured:
        return False
    if not hmac.compare_digest(str(sig), expected):
        return False
    iat = grant.get("iat")
    if not isinstance(iat, (int, float)):
        return False
    age = (now or datetime.now(timezone.utc)).timestamp() - float(iat)
    if age < -300 or age > GRACE_SECONDS:  # 5-min skew tolerance, then grace cap
        return False
    exp = grant.get("expires_at")
    if exp and is_expired(exp, now=now):
        return False
    return True
