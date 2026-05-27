"""YouTube Live source for the IPTV catalogue.

The catalogue stores YouTube channels with the @handle URL (e.g.
`https://www.youtube.com/@channelnewsasia/live`). At PLAY time the
play page calls /api/iptv/channels/<id>/resolve_url, which invokes
yt-dlp to extract the *current* HLS manifest URL and returns it.

Why per-play resolution rather than refresh-time:
  - YouTube live HLS URLs are signed and rotate periodically. Caching
    them in the channels table would mean serving stale URLs the
    moment the signature expires.
  - yt-dlp takes 1-2s per resolve. Doing it for ~40 channels at refresh
    time = ~40-80s blocking refresh. Doing it on-demand keeps the
    refresh cheap (just YAML re-parse) and only the channels the user
    actually plays incur the cost.

The resolved URL is cached in-memory for `_RESOLVE_TTL_SEC` (30 min)
so rapid taps don't spawn duplicate yt-dlp processes.
"""
from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


_YAML_PATH = Path(__file__).parent.parent / "data" / "youtube_live.yaml"
_RESOLVE_TTL_SEC = 30 * 60   # 30 min — YouTube live manifests usually last longer but be safe
_RESOLVE_TIMEOUT_SEC = 12

# In-memory resolved-URL cache: channel_url → (m3u8_url, expires_at)
_resolve_cache: dict[str, tuple[str, float]] = {}
_resolve_locks: dict[str, asyncio.Lock] = {}


def load_youtube_channels() -> list[dict]:
    """Read data/youtube_live.yaml and return the list of channel dicts.
    Idempotent — refresh_from_youtube_live() calls this each refresh
    so adding a channel just needs the YAML edit, no container restart."""
    import yaml
    if not _YAML_PATH.is_file():
        logger.warning("youtube_live.yaml not found at %s", _YAML_PATH)
        return []
    try:
        with _YAML_PATH.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        return list(data.get("channels", []))
    except Exception:
        logger.exception("youtube_live.yaml parse failed")
        return []


def _channel_url_for(handle: str) -> str:
    """The /live shortcut redirects to whichever stream is currently
    broadcasting. yt-dlp follows it. If the channel ISN'T currently
    live, yt-dlp raises and the play page surfaces the error."""
    return f"https://www.youtube.com/@{handle}/live"


def _resolve_sync(channel_url: str) -> str:
    """Synchronous yt-dlp call — must be run in an executor to avoid
    blocking the event loop. Returns the HLS manifest URL."""
    import yt_dlp
    ydl_opts: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "format": "best[protocol^=m3u8]/best",
        # Don't follow @handle → /videos → individual VOD; we want /live.
        "extract_flat": False,
        "socket_timeout": _RESOLVE_TIMEOUT_SEC,
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(channel_url, download=False)
    if not info:
        raise RuntimeError("yt-dlp returned no info")
    # extract_info on /live can return either the live stream's info
    # dict directly OR a playlist; handle both.
    if info.get("_type") == "playlist":
        entries = info.get("entries") or []
        if not entries:
            raise RuntimeError("channel has no current live stream")
        info = entries[0]
    if not info.get("is_live"):
        raise RuntimeError("channel is not currently live")
    url = info.get("url") or info.get("manifest_url")
    if not url:
        # Some YouTube extractors put the m3u8 inside 'formats'
        for fmt in info.get("formats", []):
            u = fmt.get("url", "")
            if ".m3u8" in u:
                url = u
                break
    if not url:
        raise RuntimeError("no playable stream URL in yt-dlp output")
    return url


async def resolve_live_url(channel_url: str) -> str:
    """Async wrapper with per-URL lock + TTL cache. Multiple concurrent
    callers for the same channel coalesce on one yt-dlp invocation."""
    now = time.time()
    hit = _resolve_cache.get(channel_url)
    if hit and hit[1] > now:
        return hit[0]
    lock = _resolve_locks.setdefault(channel_url, asyncio.Lock())
    async with lock:
        # Re-check after acquiring lock — another coro may have done it
        hit = _resolve_cache.get(channel_url)
        if hit and hit[1] > now:
            return hit[0]
        loop = asyncio.get_event_loop()
        url = await loop.run_in_executor(None, _resolve_sync, channel_url)
        _resolve_cache[channel_url] = (url, now + _RESOLVE_TTL_SEC)
        return url


def clear_resolve_cache() -> int:
    """Wipe the in-memory cache (for testing / forced re-resolve)."""
    n = len(_resolve_cache)
    _resolve_cache.clear()
    return n
