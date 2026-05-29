"""Filesystem cache for Stremio-grabbed files.

Layout (under DOWNLOADS_DIR, which maps to G:\\YT-DLP\\ on the host):

    /downloads/Stremio/
      tt1375666/
        Inception.2010.1080p.x264-YIFY.mp4
        .meta.json                          # one per folder
      tt0468569/
        ...

`.meta.json` records:
    {
      "imdb_id":  "tt1375666",
      "infohash": "a4d...",
      "magnet":   "magnet:?xt=...",
      "title":    "Inception (2010)",
      "filename": "Inception.2010.1080p.x264-YIFY.mp4",
      "filesize": 6_400_000_000,
      "mime":     "video/mp4",
      "grabbed_at": "2026-05-28T20:14:00Z",
      "last_played": "2026-05-29T08:00:00Z",
      "source_stream_title": "Inception (2010) 1080p BrRip ..."
    }

The infohash → on-disk-path lookup is the key operation — `/file/<infohash>`
streams range-served from the local file when present, else 404 (caller
should re-grab).

LRU eviction (Q3 default: 90% partition full → delete oldest by
last_played) runs whenever a new grab kicks off. Configurable via
`stremio_cache_max_gb` in smdl.json — when set, that's a hard upper
bound on cache size, evaluated BEFORE the partition-% check.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

logger = logging.getLogger(__name__)

# Resolve DOWNLOADS_DIR once at import. Inside the container this is
# /downloads (mapped to ${SENTINEL_MEDIA_ROOT:-G:\YT-DLP}).
DOWNLOADS_DIR = Path(os.environ.get("DOWNLOADS_DIR", "/downloads"))
STREMIO_ROOT = DOWNLOADS_DIR / "Stremio"


def _ensure_root() -> None:
    STREMIO_ROOT.mkdir(parents=True, exist_ok=True)


def _folder_safe(imdb_id: str) -> str:
    """Translate Stremio content_ids (movie: 'tt1375666'; episode:
    'tt0903747:1:1') into Windows-safe folder names.

    Movies stay as-is. Episodes get split into a parent imdb folder +
    Sxx/Eyy subfolders so the on-disk layout reads cleanly:

        Stremio/tt0903747/S01/E01/Breaking.Bad.S01E01.mkv
                tt1375666/Inception.2010.1080p.mkv
    """
    parts = imdb_id.split(":")
    if len(parts) == 1:
        return parts[0]
    if len(parts) == 3:
        # tt<id>:S:E
        try:
            s = int(parts[1]); e = int(parts[2])
            return f"{parts[0]}/S{s:02d}/E{e:02d}"
        except ValueError:
            pass
    # Fallback: replace colons with hyphens
    return imdb_id.replace(":", "-")


@dataclass
class CacheEntry:
    imdb_id: str
    infohash: str
    magnet: Optional[str]
    title: str
    filename: str
    filesize: int
    mime: Optional[str]
    grabbed_at: str
    last_played: str
    source_stream_title: Optional[str] = None

    @property
    def folder(self) -> Path:
        # The on-disk folder uses the colon-translated form. The dataclass's
        # `imdb_id` stays as the raw Stremio content_id (so lookups by
        # infohash still find it).
        return STREMIO_ROOT / _folder_safe(self.imdb_id)

    @property
    def file_path(self) -> Path:
        return self.folder / self.filename


# ── Read / list ─────────────────────────────────────────────────────────────
def _meta_path(folder: Path) -> Path:
    return folder / ".meta.json"


def _load_meta(folder: Path) -> Optional[CacheEntry]:
    p = _meta_path(folder)
    if not p.exists():
        return None
    try:
        with open(p, "r", encoding="utf-8") as f:
            d = json.load(f)
        return CacheEntry(**d)
    except Exception as e:
        logger.warning("cache meta unreadable %s: %s", p, e)
        return None


def list_entries() -> list[CacheEntry]:
    """All currently-cached items, in arbitrary order. Caller sorts.

    Walks the tree to depth 3 so series-episode subfolders (tt.../Sxx/Eyy/)
    are picked up alongside flat movie folders (tt.../)."""
    _ensure_root()
    out: list[CacheEntry] = []
    # rglob is OK at our scale (dozens of entries); cap depth at 3 implicitly
    # by only looking for .meta.json files.
    for meta in STREMIO_ROOT.rglob(".meta.json"):
        try:
            with open(meta, "r", encoding="utf-8") as f:
                d = json.load(f)
            e = CacheEntry(**d)
        except Exception as ex:
            logger.warning("cache meta unreadable %s: %s", meta, ex)
            continue
        if e.file_path.exists():
            out.append(e)
    return out


def find_by_infohash(infohash: str) -> Optional[CacheEntry]:
    """Lookup the cached file (if any) for an infohash. O(N) over cache
    entries — fine at the size we're operating at (single-user, dozens
    of items)."""
    infohash = (infohash or "").lower().strip()
    if not infohash:
        return None
    for e in list_entries():
        if e.infohash.lower() == infohash:
            return e
    return None


# ── Write / record a grab ──────────────────────────────────────────────────
def record(*, imdb_id: str, infohash: str, magnet: Optional[str],
            title: str, filename: str, filesize: int, mime: Optional[str],
            source_stream_title: Optional[str] = None) -> CacheEntry:
    """Persist the .meta.json after a successful download. The actual
    file is downloaded separately by the queue worker — this just
    records the metadata so future lookups can find it."""
    _ensure_root()
    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    entry = CacheEntry(
        imdb_id=imdb_id, infohash=infohash.lower(), magnet=magnet,
        title=title, filename=filename, filesize=filesize, mime=mime,
        grabbed_at=now_iso, last_played=now_iso,
        source_stream_title=source_stream_title,
    )
    entry.folder.mkdir(parents=True, exist_ok=True)
    with open(_meta_path(entry.folder), "w", encoding="utf-8") as f:
        json.dump(asdict(entry), f, indent=2)
    return entry


def touch_last_played(infohash: str) -> None:
    """Bump last_played to now — called from /file/<infohash> range serve
    so LRU eviction sees the file as actively used."""
    e = find_by_infohash(infohash)
    if not e:
        return
    e.last_played = datetime.now(timezone.utc).isoformat(timespec="seconds")
    try:
        with open(_meta_path(e.folder), "w", encoding="utf-8") as f:
            json.dump(asdict(e), f, indent=2)
    except Exception as ex:
        logger.debug("touch_last_played failed: %s", ex)


# ── LRU eviction ───────────────────────────────────────────────────────────
def _disk_usage_bytes() -> tuple[int, int, int]:
    """(total, used, free) for the partition holding STREMIO_ROOT."""
    _ensure_root()
    u = shutil.disk_usage(str(STREMIO_ROOT))
    return u.total, u.used, u.free


def _cache_size_bytes() -> int:
    return sum(e.filesize for e in list_entries())


def evict_if_needed(*, partition_full_pct: float = 0.90,
                    hard_cap_gb: Optional[float] = None) -> int:
    """Run eviction. Returns number of entries deleted.

    Two thresholds, checked in order:
      1. hard_cap_gb (optional) — kick if cache size > N GB
      2. partition_full_pct (default 0.90) — kick if used/total > 90%

    Eviction order: oldest `last_played` first."""
    deleted = 0

    def _oldest_first() -> list[CacheEntry]:
        return sorted(list_entries(), key=lambda e: e.last_played)

    if hard_cap_gb is not None:
        cap_bytes = int(hard_cap_gb * (1024 ** 3))
        while _cache_size_bytes() > cap_bytes:
            entries = _oldest_first()
            if not entries:
                break
            _evict_one(entries[0])
            deleted += 1

    total, used, _free = _disk_usage_bytes()
    while total > 0 and (used / total) > partition_full_pct:
        entries = _oldest_first()
        if not entries:
            break
        _evict_one(entries[0])
        deleted += 1
        total, used, _free = _disk_usage_bytes()

    if deleted:
        logger.info("stremio cache: evicted %d entries", deleted)
    return deleted


def _evict_one(e: CacheEntry) -> None:
    try:
        if e.file_path.exists():
            e.file_path.unlink()
        _meta_path(e.folder).unlink(missing_ok=True)
        # Remove folder if empty (don't recurse — only remove if we left it bare).
        try:
            e.folder.rmdir()
        except OSError:
            pass
        logger.info("stremio cache: evicted %s (%s)", e.imdb_id, e.filename)
    except Exception as ex:
        logger.warning("failed to evict %s: %s", e.imdb_id, ex)
