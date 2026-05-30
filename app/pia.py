"""Owner-editable PIA (Private Internet Access) VPN credentials.

Stored as individual secret files under /config (bind-mounted rw, outside the
git repo, survives image rebuilds) so a future gluetun container can consume
them via *_SECRETFILE without the values ever touching the image or a committed
env file. The raw values are never returned to the UI — only a masked tail and
set/unset status.

This drives the VPN-gated direct-torrent fallback: when Real-Debrid can't serve
an uncached release, the magnet is handed to a torrent client running behind
gluetun (PIA + kill-switch), so the home IP never joins the swarm.
"""

import os
from typing import Optional

_CONFIG_DIR = os.environ.get("PIA_CONFIG_DIR", "/config")
_USER_FILE  = os.path.join(_CONFIG_DIR, "pia_user")
_PASS_FILE  = os.path.join(_CONFIG_DIR, "pia_pass")
_DIP_FILE   = os.path.join(_CONFIG_DIR, "pia_dip_token")


def _read(path: str) -> Optional[str]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read().strip() or None
    except (FileNotFoundError, PermissionError):
        return None


def _write(path: str, value: str) -> None:
    """Atomic write + 0600 perms so a crash mid-save can't truncate a secret
    and the file isn't world-readable inside the container."""
    d = os.path.dirname(path) or "."
    os.makedirs(d, exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(value.strip() + "\n")
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _clear(path: str) -> None:
    try:
        os.remove(path)
    except FileNotFoundError:
        pass


def _mask(val: Optional[str]) -> Optional[str]:
    if not val:
        return None
    return f"…{val[-4:]}" if len(val) >= 4 else "set"


def status() -> dict:
    """Set/unset + masked tails. Never exposes raw values. Passwords show only
    'set' (no tail) since a tail leaks more for a secret than for an id/token."""
    user = _read(_USER_FILE)
    pw   = _read(_PASS_FILE)
    dip  = _read(_DIP_FILE)
    return {
        "username":  {"set": bool(user), "masked": _mask(user)},
        "password":  {"set": bool(pw),   "masked": ("set" if pw else None)},
        "dip_token": {"set": bool(dip),  "masked": _mask(dip)},
        "configured": bool(user and pw),
    }


def set_creds(*, username: Optional[str] = None, password: Optional[str] = None,
              dip_token: Optional[str] = None) -> dict:
    """Persist any provided field. A non-empty string sets it; an empty string
    clears it; None leaves it untouched (so the UI can rotate the password
    without re-sending the username)."""
    if username is not None:
        _write(_USER_FILE, username) if username.strip() else _clear(_USER_FILE)
    if password is not None:
        _write(_PASS_FILE, password) if password.strip() else _clear(_PASS_FILE)
    if dip_token is not None:
        _write(_DIP_FILE, dip_token) if dip_token.strip() else _clear(_DIP_FILE)
    return status()
