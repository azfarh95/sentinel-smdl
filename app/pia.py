"""Read-only view of the PIA (Private Internet Access) VPN credentials that
power the direct-torrent fallback.

Credentials are managed canonically in Windows Credential Manager and synced
into .env.local by scripts/rotate_pia_creds.ps1 — the same store that feeds the
`pia-exit` gluetun exit node. SMDL mounts .env.local (env_file), so the values
arrive as environment variables. This module never stores or returns the raw
secrets — only whether they're configured, plus the non-secret region, so the
Settings page can show status without becoming a second source of truth.
"""

import os
from typing import Optional


def _env(name: str) -> Optional[str]:
    v = (os.environ.get(name) or "").strip()
    return v or None


def _mask(val: Optional[str]) -> Optional[str]:
    if not val:
        return None
    return f"…{val[-4:]}" if len(val) >= 4 else "set"


def status() -> dict:
    """Whether PIA is configured in the canonical store, plus the non-secret
    region. `configured` requires both username and password — the dedicated-IP
    token is not needed for the OpenVPN path (PIA auto-assigns the DIP from the
    account that owns the order)."""
    user = _env("PIA_USER")
    pw   = _env("PIA_PASSWORD")
    return {
        "configured": bool(user and pw),
        "username": _mask(user),
        "region": _env("PIA_REGION") or "Singapore",
        "source": "wcm",
    }
