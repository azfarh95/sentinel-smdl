"""Best-effort upstream of issued/revoked keys to the Sentinel License Registry.

SMDL is the issuing authority and the source of truth for license *secrets*.
This module mirrors only metadata + revocation status to the central registry
(watchdog v2, /api/v2/licenses/*) so a revocation is visible suite-wide and the
Suite owner view can list keys across instances.

Hard rule: this is NEVER allowed to break local issuance or revocation. Every
network call is time-boxed and swallows errors — if the registry is down, the
key is still minted/revoked locally and the mirror simply lags until the next
push or a manual `sync`. No secret ever leaves this process.

Config (all optional — absence disables the mirror, no-op):
  LICENSE_REGISTRY_URL    e.g. http://host.docker.internal:8200
  LICENSE_REGISTRY_TOKEN  watchdog v2 service token (X-Sentinel-Service-Token)
  LICENSE_INSTANCE        this issuer's name (default "smdl-operator")
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

import httpx

log = logging.getLogger("smdl.license_registry")

_TIMEOUT = 4.0  # seconds — short; the registry is a best-effort mirror
_pending: set[asyncio.Task] = set()  # hold refs so fire-and-forget tasks aren't GC'd
_warned_disabled = False


def _cfg() -> tuple[str, str, str]:
    url = (os.environ.get("LICENSE_REGISTRY_URL") or "").strip().rstrip("/")
    token = (os.environ.get("LICENSE_REGISTRY_TOKEN") or "").strip()
    instance = (os.environ.get("LICENSE_INSTANCE") or "smdl-operator").strip()
    return url, token, instance


def is_enabled() -> bool:
    url, token, _ = _cfg()
    return bool(url and token)


def status() -> dict[str, Any]:
    """Small, safe summary for the owner page — never exposes the token."""
    url, token, instance = _cfg()
    return {"enabled": bool(url and token), "url": url or None, "instance": instance}


def _payload(row: dict, *, force_revoked: bool = False) -> dict[str, Any]:
    """Map a local license_keys row to the registry's metadata shape."""
    _, _, instance = _cfg()
    local_status = row.get("status", "active")
    status = "revoked" if (force_revoked or local_status != "active") else "active"
    return {
        "key_id": row["key_id"],
        "tier": row["tier"],
        "status": status,
        "expires_at": row.get("expires_at"),
        "issued_to": row.get("issued_to"),
        "instance": instance,
        "note": row.get("note"),
    }


async def _post_upsert(row: dict, *, force_revoked: bool = False) -> bool:
    url, token, _ = _cfg()
    if not (url and token):
        return False
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            r = await client.post(
                f"{url}/api/v2/licenses/upsert",
                json=_payload(row, force_revoked=force_revoked),
                headers={"X-Sentinel-Service-Token": token},
            )
        if r.status_code >= 400:
            log.warning("registry upsert %s -> HTTP %s", row.get("key_id"), r.status_code)
            return False
        return True
    except Exception as e:  # noqa: BLE001 — best-effort, never propagate
        log.warning("registry upsert %s failed: %s", row.get("key_id"), e)
        return False


def fire_upsert(row: dict, *, force_revoked: bool = False) -> None:
    """Schedule a non-blocking mirror push. Safe to call from a request handler;
    returns immediately and never raises. No-op if the registry is unconfigured."""
    global _warned_disabled
    if not is_enabled():
        if not _warned_disabled:
            log.info("license registry mirror disabled (LICENSE_REGISTRY_URL/TOKEN unset)")
            _warned_disabled = True
        return
    try:
        task = asyncio.create_task(_post_upsert(row, force_revoked=force_revoked))
    except RuntimeError:
        return  # no running loop (e.g. called outside async context) — skip
    _pending.add(task)
    task.add_done_callback(_pending.discard)


async def backfill(rows: list[dict]) -> dict[str, Any]:
    """Push every local key to the registry. Used by the owner 'Sync' action and
    safe to re-run (upserts are idempotent; revocation is terminal upstream).
    Awaited because it's an explicit, user-initiated reconcile — but still
    swallows per-key failures so one bad row can't abort the batch."""
    if not is_enabled():
        return {"ok": False, "reason": "registry not configured", "synced": 0, "failed": 0}
    synced = failed = 0
    for row in rows:
        ok = await _post_upsert(row, force_revoked=(row.get("status", "active") != "active"))
        if ok:
            synced += 1
        else:
            failed += 1
    return {"ok": failed == 0, "synced": synced, "failed": failed, "total": len(rows)}
