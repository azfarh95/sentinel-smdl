"""Beta-key issuance — owner-only mint of opaque keys that unlock named
extra scopes on a user's existing session, without changing their plan.

Model
-----
Beta keys are a *permissions* primitive, not a billing primitive. They
exist so the operator can give specific users access to scope-gated
features (e.g. an unreleased route guarded by `require_scope("smdl.tv.recorder.beta")`)
without going through the license-key/billing rail or the premium
manifest. Plans are unaffected — the user's entitlement tier stays
exactly what their identity already resolves to.

Key format mirrors license keys:

    SMDL-BETA.<key_id>.<secret>

Only HMAC(secret) is persisted, so a DB leak does not yield usable keys.
Each row carries `extra_scopes` (a JSON list of scope strings) that get
merged into the redeemer's session cookie at issuance time.

Redemption rules
----------------
The redemption endpoint *never* silently switches identity (standing
rule from the 2026-05-27 /auth/redeem incident). If the caller is not
already signed in, redemption fails with `sign_in_required` — the caller
must establish a session first, then redeem. A single key is
single-redemption: `redeemed_by_user_id` pins it on first use; any
subsequent attempt by a different user fails with `already_redeemed`.
The same user re-redeeming the same key is a no-op success.

Scope accumulation
------------------
At cookie issuance time (via `live_extra_scopes_for(user_id)`), every
live (non-revoked, non-expired) beta_key the user has redeemed contributes
its `extra_scopes`. So beta privileges *follow the identity* — a fresh
sign-in re-bakes the augmented scope list into the new cookie. The set
holds until the key is revoked or expires.
"""
from __future__ import annotations

import hmac
import json
import os
import secrets as _secrets
from datetime import datetime, timezone
from hashlib import sha256

import aiosqlite

from .database import DB_PATH


KEY_PREFIX = "SMDL-BETA"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _signing_secret() -> str:
    """HMAC secret for hashing the key secret-half. Reuses OWNER_AUTH_TOKEN
    so we don't introduce yet another deployment env-var. A rotation of
    OWNER_AUTH_TOKEN invalidates outstanding beta keys — that's acceptable
    given beta keys are operator-issued and short-lived by intent."""
    s = os.environ.get("OWNER_AUTH_TOKEN", "")
    if not s:
        raise RuntimeError(
            "OWNER_AUTH_TOKEN must be set to mint or verify beta keys")
    return s


def _hash_secret(secret: str) -> str:
    return hmac.new(_signing_secret().encode(), secret.encode(),
                    sha256).hexdigest()


def _parse_key(plaintext: str) -> tuple[str, str] | None:
    """Split `SMDL-BETA.<id>.<secret>` → (key_id, secret), or None on bad form."""
    s = (plaintext or "").strip()
    parts = s.split(".")
    if len(parts) != 3 or parts[0] != KEY_PREFIX:
        return None
    _, kid, sec = parts
    if not kid or not sec:
        return None
    return kid, sec


def _scopes_to_json(scopes: list[str]) -> str:
    """Canonicalise: strip + dedupe + sort, then JSON-encode."""
    cleaned = sorted({str(s).strip() for s in (scopes or []) if str(s).strip()})
    return json.dumps(cleaned, separators=(",", ":"))


def _parse_scopes(raw: str | None) -> list[str]:
    if not raw:
        return []
    try:
        v = json.loads(raw)
    except Exception:
        return []
    if not isinstance(v, list):
        return []
    return [str(x) for x in v if isinstance(x, str)]


# ── DB layer ────────────────────────────────────────────────────────────────


async def mint(label: str, extra_scopes: list[str], *,
               expires_at: str | None = None,
               created_by: int | None = None,
               note: str | None = None) -> dict:
    """Mint a fresh beta key. Returns the FULL row PLUS the plaintext key
    (only shown once — the caller must surface it to the operator).
    extra_scopes must be non-empty; an empty list would let a key carry
    nothing and is almost certainly a bug."""
    scopes = _scopes_to_json(extra_scopes)
    if scopes == "[]":
        raise ValueError("extra_scopes is required and must be non-empty")
    key_id = _secrets.token_urlsafe(6)
    secret = _secrets.token_urlsafe(24)
    plaintext = f"{KEY_PREFIX}.{key_id}.{secret}"
    secret_hash = _hash_secret(secret)
    now = _now_iso()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO beta_keys
                (key_id, secret_hash, label, extra_scopes,
                 expires_at, created_at, created_by, note)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (key_id, secret_hash, label, scopes, expires_at,
              now, created_by, note))
        await db.commit()
    row = await get(key_id)
    assert row is not None
    return {**row, "key": plaintext}


async def get(key_id: str) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM beta_keys WHERE key_id = ?", (key_id,)
        ) as cur:
            row = await cur.fetchone()
            if row is None:
                return None
            d = dict(row)
            d["extra_scopes"] = _parse_scopes(d.get("extra_scopes"))
            return d


async def list_keys() -> list[dict]:
    out: list[dict] = []
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM beta_keys ORDER BY created_at DESC"
        ) as cur:
            async for row in cur:
                d = dict(row)
                d["extra_scopes"] = _parse_scopes(d.get("extra_scopes"))
                out.append(d)
    return out


async def revoke(key_id: str) -> bool:
    now = _now_iso()
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("""
            UPDATE beta_keys SET revoked_at = ?
            WHERE key_id = ? AND revoked_at IS NULL
        """, (now, key_id))
        await db.commit()
        return (cur.rowcount or 0) > 0


def _is_live(row: dict) -> bool:
    """A key is 'live' if not revoked AND not expired. Redemption status
    is irrelevant to liveness — a redeemed-but-not-revoked key still
    contributes scopes."""
    if row.get("revoked_at"):
        return False
    exp = row.get("expires_at")
    if not exp:
        return True
    try:
        t = datetime.fromisoformat(str(exp).replace("Z", "+00:00"))
    except ValueError:
        return True
    return t > datetime.now(timezone.utc)


# ── Redemption + scope merge ────────────────────────────────────────────────


class RedeemError(Exception):
    """Raised by `redeem()` with a stable .code for the HTTP layer to map."""
    def __init__(self, code: str, message: str | None = None):
        super().__init__(message or code)
        self.code = code


async def redeem(plaintext_key: str, user_id: str) -> dict:
    """Mark a key as redeemed by `user_id`. Returns the row on success.

    NEVER silently switches identity. Re-redeeming the same key with the
    same user_id is idempotent; a different user gets `already_redeemed`.

    Error codes (`RedeemError.code`):
        invalid_format       — string doesn't look like a beta key
        unknown_key          — key_id not in the table
        bad_signature        — secret HMAC mismatch
        revoked              — key has been revoked
        expired              — key has passed its expires_at
        sign_in_required     — caller has no identity (empty user_id)
        already_redeemed     — pinned to a different user
    """
    if not user_id:
        raise RedeemError("sign_in_required")
    parsed = _parse_key(plaintext_key)
    if parsed is None:
        raise RedeemError("invalid_format")
    key_id, secret = parsed
    row = await get(key_id)
    if row is None:
        raise RedeemError("unknown_key")
    if row.get("revoked_at"):
        raise RedeemError("revoked")
    if not _is_live(row):
        raise RedeemError("expired")
    expected = row.get("secret_hash") or ""
    if not hmac.compare_digest(_hash_secret(secret), expected):
        raise RedeemError("bad_signature")
    pinned = row.get("redeemed_by_user_id")
    if pinned and pinned != user_id:
        raise RedeemError("already_redeemed")
    if pinned == user_id:
        return row  # idempotent re-redeem
    now = _now_iso()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            UPDATE beta_keys
            SET redeemed_by_user_id = ?, redeemed_at = ?
            WHERE key_id = ?
        """, (user_id, now, key_id))
        await db.commit()
    refreshed = await get(key_id)
    assert refreshed is not None
    return refreshed


async def live_extra_scopes_for(user_id: str) -> list[str]:
    """Return the union of `extra_scopes` for every live (non-revoked,
    non-expired) beta key this user has redeemed. Empty list for a user
    with no redemptions."""
    if not user_id:
        return []
    out: set[str] = set()
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("""
            SELECT extra_scopes, expires_at, revoked_at
            FROM beta_keys
            WHERE redeemed_by_user_id = ?
        """, (user_id,)) as cur:
            async for row in cur:
                d = dict(row)
                if not _is_live(d):
                    continue
                out.update(_parse_scopes(d.get("extra_scopes")))
    return sorted(out)
