"""Per-platform yt-dlp cookie management (owner-only).

The downloader resolves auth cookies from ``/cookies/<name>.txt`` (Netscape /
cookies.txt format) per `downloader._SITE_COOKIE_MAP`. Those expire and have to
be refreshed by hand on the host — this module lets the owner do it from the
phone instead (Mini App paste/upload + a bot document handler), and surfaces
freshness (count + soonest expiry) so you can see *which* ones are stale.

No new storage: it reads/writes the exact files the downloader already uses.
"""
from __future__ import annotations

import time
from pathlib import Path

from .downloader import COOKIES_DIR, _SITE_COOKIE_MAP

# Distinct cookie-file basenames the downloader knows about, sorted for a stable
# UI order: tiktok, instagram, twitter, facebook, twitch, kick, youtube.
PLATFORMS: list[str] = sorted(set(_SITE_COOKIE_MAP.values()))

_NETSCAPE_HEADER = "# Netscape HTTP Cookie File"
_MAX_BYTES = 2 * 1024 * 1024  # a cookies.txt is a few KB; cap paste/upload abuse.


def cookie_path(platform: str) -> Path:
    return Path(COOKIES_DIR) / f"{platform}.txt"


def parse_netscape(text: str) -> tuple[int, int | None]:
    """Return (cookie_count, soonest_nonzero_expiry_unix or None). Lenient: skips
    comments (except the `#HttpOnly_` data lines) and malformed rows. A cookies.txt
    row is 7 TAB-separated fields: domain, flag, path, secure, expiry, name, value."""
    count = 0
    expiries: list[int] = []
    for line in text.splitlines():
        s = line.strip()
        if not s:
            continue
        if s.startswith("#") and not s.startswith("#HttpOnly_"):
            continue
        raw = s[len("#HttpOnly_"):] if s.startswith("#HttpOnly_") else s
        parts = raw.split("\t")
        if len(parts) < 7:
            continue
        count += 1
        try:
            exp = int(parts[4])
            if exp > 0:
                expiries.append(exp)
        except ValueError:
            pass
    return count, (min(expiries) if expiries else None)


def status_one(platform: str) -> dict:
    p = cookie_path(platform)
    if not p.exists():
        return {"platform": platform, "present": False}
    count, soonest = 0, None
    try:
        count, soonest = parse_netscape(p.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        pass
    st = p.stat()
    now = int(time.time())
    expires_in_days = None
    if soonest is not None:
        expires_in_days = round((soonest - now) / 86400, 1)
    return {
        "platform": platform,
        "present": True,
        "updated_at": int(st.st_mtime),
        "age_days": round((now - st.st_mtime) / 86400, 1),
        "count": count,
        "soonest_expiry": soonest,
        "expires_in_days": expires_in_days,
        "size": st.st_size,
    }


def status_all() -> list[dict]:
    return [status_one(p) for p in PLATFORMS]


def save(platform: str, text: str) -> dict:
    """Validate + write a fresh cookies.txt for `platform`. Returns the new status.
    Raises ValueError on an unknown platform, an oversized payload, or text that
    has no recognisable cookie rows (so a stray paste can't blank a working file)."""
    if platform not in PLATFORMS:
        raise ValueError(f"unknown platform '{platform}' (known: {', '.join(PLATFORMS)})")
    text = text or ""
    if len(text.encode("utf-8", errors="ignore")) > _MAX_BYTES:
        raise ValueError("cookie file too large")
    count, _ = parse_netscape(text)
    if count < 1:
        raise ValueError("no valid cookie rows found — paste a Netscape / cookies.txt export")
    # yt-dlp rejects a file without the Netscape header line; add it if missing.
    if not text.lstrip().startswith(("# Netscape", "# HTTP Cookie")):
        text = _NETSCAPE_HEADER + "\n" + text
    if not text.endswith("\n"):
        text += "\n"
    p = cookie_path(platform)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return status_one(platform)


def delete(platform: str) -> bool:
    if platform not in PLATFORMS:
        raise ValueError("unknown platform")
    p = cookie_path(platform)
    try:
        p.unlink(missing_ok=True)
        return True
    except OSError:
        return False


def platform_from_hint(hint: str) -> str | None:
    """Best-effort platform match from a filename / caption (bot upload path).
    e.g. 'youtube_cookies.txt' → 'youtube', 'IG' → None, 'insta' → 'instagram'."""
    h = (hint or "").lower()
    # Exact platform name first, then a couple of common aliases.
    for plat in PLATFORMS:
        if plat in h:
            return plat
    aliases = {"insta": "instagram", "ig": "instagram", "yt": "youtube",
               "fb": "facebook", "x": "twitter", "ttok": "tiktok"}
    for alias, plat in aliases.items():
        if alias in h.split():
            return plat
    return None
