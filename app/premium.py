"""Operator premium-user manifest.

The community/play deployments need a way to mark a specific identity
(Telegram chat_id, Google sub, or e-mail) as plus/family WITHOUT going
through the license-key billing rail. This is the manifest: an
owner-managed list of (identity, plan) rows. A lookup at request time
mints a server-trusted grant carrying that plan, so paid caps unlock
without the user having to hold/redeem a license key.

Identity types match the two non-key sign-in surfaces:

  telegram : identity_value = str(chat_id)         (via initData)
  google   : identity_value = str(sub) or 'google:<sub>' (via OIDC)
  email    : identity_value = lowercased e-mail    (fallback / batch-grant)

`plan` is a key of `entitlements.PLANS`. `expires_at` is optional ISO-UTC;
NULL = no expiry. Lookups are cheap (single indexed SELECT) so we don't
cache.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

import aiosqlite

from . import entitlements
from .database import DB_PATH


IdentityType = Literal["telegram", "google", "email"]
_VALID_TYPES: frozenset[str] = frozenset({"telegram", "google", "email"})


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _norm(identity_type: str, identity_value: str) -> tuple[str, str]:
    """Canonical form for (type, value). E-mails are case-insensitive; the
    others are stored verbatim except surrounding whitespace."""
    t = (identity_type or "").strip().lower()
    if t not in _VALID_TYPES:
        raise ValueError(f"unknown identity_type: {identity_type!r}")
    v = (identity_value or "").strip()
    if not v:
        raise ValueError("identity_value is required")
    if t == "email":
        v = v.lower()
    return t, v


async def add(identity_type: str, identity_value: str, plan: str,
              notes: str | None = None,
              expires_at: str | None = None) -> dict:
    """Upsert a premium row. Replacing an existing row updates plan + notes
    + expires_at and bumps updated_at."""
    t, v = _norm(identity_type, identity_value)
    if plan not in entitlements.PLANS:
        raise ValueError(f"unknown plan: {plan!r}")
    now = _now_iso()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO premium_users
                (identity_type, identity_value, plan, notes,
                 expires_at, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(identity_type, identity_value) DO UPDATE SET
                plan       = excluded.plan,
                notes      = excluded.notes,
                expires_at = excluded.expires_at,
                updated_at = excluded.updated_at
        """, (t, v, plan, notes, expires_at, now, now))
        await db.commit()
    row = await get(t, v)
    assert row is not None  # we just wrote it
    return row


async def remove(identity_type: str, identity_value: str) -> bool:
    """Returns True if a row was deleted, False if it didn't exist."""
    t, v = _norm(identity_type, identity_value)
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "DELETE FROM premium_users WHERE identity_type = ? AND identity_value = ?",
            (t, v),
        )
        await db.commit()
        return (cur.rowcount or 0) > 0


async def get(identity_type: str, identity_value: str) -> dict | None:
    t, v = _norm(identity_type, identity_value)
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM premium_users WHERE identity_type = ? AND identity_value = ?",
            (t, v),
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


async def list_all() -> list[dict]:
    """All premium rows, newest first."""
    out: list[dict] = []
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM premium_users ORDER BY updated_at DESC"
        ) as cur:
            async for row in cur:
                out.append(dict(row))
    return out


def _is_expired(row: dict) -> bool:
    exp = row.get("expires_at")
    if not exp:
        return False
    try:
        # Stored as ISO-UTC; tolerate Z suffix.
        t = datetime.fromisoformat(exp.replace("Z", "+00:00"))
    except ValueError:
        return False
    return t <= datetime.now(timezone.utc)


async def lookup_plan(identity_type: str, identity_value: str) -> str | None:
    """Resolve an identity to its premium plan, or None if the row is missing,
    expired, or carries an unknown plan. This is the hot path called by
    grant_transport on every request that has a session — keep it cheap."""
    row = await get(identity_type, identity_value)
    if row is None:
        return None
    if _is_expired(row):
        return None
    plan = row.get("plan")
    if plan not in entitlements.PLANS:
        return None
    return plan


def build_grant(plan: str, *, seats: int = 1) -> dict:
    """Mint a server-trusted (no header) grant for a premium identity.

    Mirrors the shape of `grant_transport._anon_grant()` but carries the
    premium plan + its entitlements. Marked anonymous=False so audit logs
    can distinguish 'manifest-granted' from 'header-presented'."""
    if plan not in entitlements.PLANS:
        plan = "free"
    return {
        "valid": True,
        "plan": plan,
        "entitlements": entitlements.entitlements_for(plan),
        "limits": {"seats": int(seats)},
        "source": "premium_manifest",
    }
