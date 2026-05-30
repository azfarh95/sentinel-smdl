"""qBittorrent WebUI client — the direct-torrent fallback for releases that
Real-Debrid won't serve (HTTP 451 / "not cached").

qBittorrent runs inside torrent-vpn's network namespace (gluetun → PIA), so
every byte it sends or receives exits through PIA behind a kill-switch: if the
tunnel drops, qBittorrent loses connectivity and the home IP never touches the
swarm. SMDL reaches the WebUI at http://torrent-vpn:8080 over the shared
metamcp-network. Auth is a subnet whitelist (no password) configured in the
qBittorrent.conf inside the qbittorrent_config volume — see the qbittorrent
service in metamcp-local/docker-compose.yml.

Both containers mount the same media disk at /downloads, so a file qBittorrent
finishes at /downloads/<name> is readable by SMDL at the identical path. The
queue (stremio_queue._download_via_torrent) moves the picked video into the
Stremio cache and records it like any other grab, after which the existing
range-aware /file/<infohash> endpoint serves it.

This is a thin, synchronous client (matching realdebrid.py); the queue calls it
via asyncio.to_thread and owns the polling/orchestration.
"""
from __future__ import annotations

import logging
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Optional

logger = logging.getLogger(__name__)

# qBittorrent shares torrent-vpn's netns, so its WebUI answers on the
# torrent-vpn container address. Resolvable by DNS now that SMDL is attached to
# metamcp-network. Overridable for local testing.
_BASE = os.environ.get("QBITTORRENT_URL", "http://torrent-vpn:8080").rstrip("/")
_TIMEOUT = int(os.environ.get("QBITTORRENT_TIMEOUT", "20"))

# A bare `magnet:?xt=urn:btih:<hash>` (built when a stream had no magnet) has no
# trackers and would lean entirely on DHT. Sprinkle in a few well-known public
# trackers so peer discovery is fast and reliable. These are open trackers, not
# content sources.
_PUBLIC_TRACKERS = [
    "udp://tracker.opentrackr.org:1337/announce",
    "udp://open.demonii.com:1337/announce",
    "udp://tracker.openbittorrent.com:6969/announce",
    "udp://exodus.desync.com:6969/announce",
    "udp://tracker.torrent.eu.org:451/announce",
]

_VIDEO_EXTS = {".mkv", ".mp4", ".avi", ".mov", ".m4v", ".webm", ".ts", ".wmv", ".flv", ".mpg", ".mpeg"}


class QBittorrentError(RuntimeError):
    """Any failure talking to the qBittorrent WebUI, or a torrent that the
    client gave up on (dead magnet, no peers)."""


# ── Low-level HTTP ──────────────────────────────────────────────────────────
def _get(path: str, params: Optional[dict] = None) -> str:
    url = f"{_BASE}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"Referer": _BASE})
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as r:
            return r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        raise QBittorrentError(f"GET {path} → HTTP {e.code}") from e
    except (urllib.error.URLError, OSError) as e:
        raise QBittorrentError(f"GET {path} unreachable: {e}") from e


def _post(path: str, data: dict) -> str:
    url = f"{_BASE}{path}"
    body = urllib.parse.urlencode(data).encode()
    # Referer header satisfies any residual CSRF check; HostHeaderValidation is
    # off in the conf, so a plain POST from the whitelisted subnet is accepted.
    req = urllib.request.Request(url, data=body, headers={
        "Content-Type": "application/x-www-form-urlencoded",
        "Referer": _BASE,
    })
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as r:
            return r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        raise QBittorrentError(f"POST {path} → HTTP {e.code}") from e
    except (urllib.error.URLError, OSError) as e:
        raise QBittorrentError(f"POST {path} unreachable: {e}") from e


def _get_json(path: str, params: Optional[dict] = None):
    import json
    return json.loads(_get(path, params))


# ── Helpers ─────────────────────────────────────────────────────────────────
def infohash_from_magnet(magnet: str) -> Optional[str]:
    """Pull the v1 btih out of a magnet URI, lowercased. Returns None for
    base32 (32-char) hashes — qBittorrent will still accept the magnet, but we
    can't pre-compute the v1 hex id, so the caller falls back to name-matching.
    """
    m = re.search(r"xt=urn:btih:([0-9a-fA-F]{40})", magnet or "")
    return m.group(1).lower() if m else None


def _enrich_magnet(magnet: str) -> str:
    """Append public trackers to a trackerless magnet so DHT isn't the only
    discovery path. Leaves magnets that already carry trackers untouched."""
    if not magnet or "&tr=" in magnet or "tr=" in magnet.split("?", 1)[-1]:
        return magnet
    extra = "".join("&tr=" + urllib.parse.quote(t, safe="") for t in _PUBLIC_TRACKERS)
    return magnet + extra


def is_available() -> bool:
    """True if the WebUI answers (torrent-vpn up + qB running + whitelist OK)."""
    try:
        _get("/api/v2/app/version")
        return True
    except QBittorrentError:
        return False


# ── Torrent lifecycle ───────────────────────────────────────────────────────
def add_magnet(magnet: str, *, savepath: Optional[str] = None,
               sequential: bool = True, category: str = "smdl") -> str:
    """Add a magnet. Returns the v1 infohash (lowercased) we'll poll on.

    Sets sequential download + first/last-piece priority so the head and tail
    land early (lets a player start before the whole file is cached). Raises
    QBittorrentError if qB rejects the add."""
    if not magnet or not magnet.startswith("magnet:"):
        raise QBittorrentError("not a magnet URI")
    magnet = _enrich_magnet(magnet)
    data = {
        "urls": magnet,
        "sequentialDownload": "true" if sequential else "false",
        "firstLastPiecePrio": "true" if sequential else "false",
        "category": category,
    }
    if savepath:
        data["savepath"] = savepath
    resp = _post("/api/v2/torrents/add", data).strip()
    if resp.lower().startswith("fail"):
        raise QBittorrentError(f"qB rejected magnet: {resp!r}")
    ih = infohash_from_magnet(magnet)
    if not ih:
        raise QBittorrentError("magnet has no v1 btih (base32 unsupported)")
    return ih


def torrent(infohash: str) -> Optional[dict]:
    """The torrents/info row for one infohash, or None if qB doesn't have it
    (yet). Carries: progress (0..1), state, save_path, content_path, name,
    size, dlspeed, eta, num_seeds, num_leechs."""
    arr = _get_json("/api/v2/torrents/info", {"hashes": infohash.lower()})
    return arr[0] if arr else None


def files(infohash: str) -> list[dict]:
    """Per-file rows: name (relative to save_path, incl. any root folder),
    size, progress, index, priority."""
    try:
        return _get_json("/api/v2/torrents/files", {"hash": infohash.lower()})
    except QBittorrentError:
        return []


def pick_video_file(file_rows: list[dict]) -> Optional[dict]:
    """Largest video file in the torrent (skips samples/extras by size)."""
    vids = [f for f in file_rows
            if os.path.splitext(f.get("name", ""))[1].lower() in _VIDEO_EXTS]
    pool = vids or file_rows
    if not pool:
        return None
    return max(pool, key=lambda f: f.get("size", 0))


def delete(infohash: str, *, with_files: bool = False) -> None:
    """Remove a torrent from qB. with_files=False keeps the downloaded data on
    disk (we move the picked file out first, so leftover data is the other
    files in a multi-file torrent — those we DO want gone, so callers pass
    with_files=True after the move)."""
    try:
        _post("/api/v2/torrents/delete", {
            "hashes": infohash.lower(),
            "deleteFiles": "true" if with_files else "false",
        })
    except QBittorrentError as e:
        logger.warning("qB delete %s failed: %s", infohash, e)


def is_complete(t: dict) -> bool:
    """A torrent is done when progress hits 1.0 or it's in an upload/seed
    state (qB flips to *UP states once the payload is fully written)."""
    if (t.get("progress") or 0) >= 1.0:
        return True
    state = (t.get("state") or "").lower()
    return state in {"uploading", "stalledup", "pausedup", "forcedup",
                     "queuedup", "checkingup"}


# ── Progressive streaming support ───────────────────────────────────────────
# qB writes pieces as they arrive; with sequentialDownload the head fills
# front-to-back and firstLastPiecePrio pulls the tail (an MP4's moov atom)
# early. To stream a partially-downloaded file safely we must serve ONLY bytes
# whose covering pieces are actually on disk — the file's apparent size lies
# once a tail piece lands (a sparse hole sits in the middle). pieceStates +
# piece_size let us check any byte range's pieces precisely.

def properties(infohash: str) -> dict:
    """torrents/properties for one infohash: piece_size, pieces_have,
    pieces_num, total_size, save_path, etc. Empty dict if qB doesn't have it."""
    try:
        return _get_json("/api/v2/torrents/properties", {"hash": infohash.lower()})
    except QBittorrentError:
        return {}


def piece_states(infohash: str) -> list[int]:
    """Per-piece download state: 0=not downloaded, 1=downloading, 2=done.
    Empty list if the torrent is gone (e.g. already removed after finalize)."""
    try:
        out = _get_json("/api/v2/torrents/pieceStates", {"hash": infohash.lower()})
        return out if isinstance(out, list) else []
    except QBittorrentError:
        return []


def file_offset_in_torrent(file_rows: list[dict], picked: dict) -> int:
    """Byte offset of `picked` within the whole torrent payload — the sum of
    every earlier file's size. Needed to map a file-local byte range onto the
    torrent's global piece grid (multi-file torrents). 0 for single-file."""
    idx = picked.get("index")
    if idx is None:
        return 0
    return sum(int(f.get("size") or 0) for f in file_rows
              if (f.get("index") if f.get("index") is not None else 1 << 30) < idx)


def pieces_ready(states: list[int], piece_size: int, file_offset: int,
                 byte_start: int, byte_end: int) -> bool:
    """True iff every piece covering the file-local byte range [byte_start,
    byte_end] is fully downloaded. Conservative: a missing/unknown piece reads
    as not-ready, so we never hand a player bytes from a sparse hole."""
    if piece_size <= 0 or not states:
        return False
    g_start = file_offset + max(0, byte_start)
    g_end = file_offset + max(byte_start, byte_end)
    p0 = g_start // piece_size
    p1 = g_end // piece_size
    if p0 < 0 or p1 >= len(states):
        return False
    for i in range(p0, p1 + 1):
        if states[i] != 2:
            return False
    return True


def live_path(save_path: str, name: str):
    """On-disk path of an in-progress file. qB appends '.!qB' to incomplete
    files when that option is on; we try the plain path first, then the
    suffixed one. Returns a pathlib.Path or None if neither exists yet."""
    from pathlib import Path
    base = Path(save_path) / name
    if base.exists():
        return base
    partial = base.with_name(base.name + ".!qB")
    if partial.exists():
        return partial
    return None
