"""Async job queue for Stremio grabs.

A grab = (magnet → RD direct URL → background download to G:\\). Each
takes 30s–5min so we MUST do it async — the Mini App polls /jobs/{id}
for progress while the user waits / starts watching the stream URL.

State machine:
              ┌────────────────────────────────────────────┐
              │                                            ▼
   queued ──► resolving ──► streaming ──► caching ──► cached
                    │                                       ▲
                    └──► error                              │
                                                            │
                  (file already on disk for this infohash) ─┘

States:
  queued      — waiting for worker slot
  resolving   — calling realdebrid.magnet_to_direct_urls()
  streaming   — RD returned direct_url; playable now; background download started
  caching     — same as streaming but the user closed the player; aria2 still running
  cached      — file fully written to G:\\YT-DLP\\Stremio\\<imdb_id>\\
  error       — terminal failure (RD timeout, magnet dead, etc.)

Persistence: jobs are stored in /data/jobs.db (existing SMDL SQLite —
new table `stremio_jobs`). Survives container restarts; on boot, any
`resolving` or `streaming` jobs are re-queued (worker re-runs them).

Concurrency: SEMAPHORE caps simultaneous processing at MAX_CONCURRENT
(default 2 per the smdl.json config). RD allows more but G:\\ writes
serialize on the disk anyway."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import aiosqlite

from .database import DB_PATH
from . import realdebrid as _rd
from . import stremio_cache as _cache

logger = logging.getLogger(__name__)

# ── Tunables (read once at module load; smdl.json overrides) ────────────────
MAX_CONCURRENT  = int(os.environ.get("STREMIO_MAX_CONCURRENT", "2"))
RESOLVE_TIMEOUT = int(os.environ.get("STREMIO_RESOLVE_TIMEOUT", "300"))


@dataclass
class StremioJob:
    id: int = 0
    imdb_id: str = ""
    type: str = "movie"            # 'movie' | 'series'
    title: str = ""                 # display title for UI
    infohash: str = ""
    magnet: str = ""
    file_index: Optional[int] = None
    source_stream_title: Optional[str] = None
    quality: Optional[str] = None
    expected_size: Optional[int] = None

    # State
    status: str = "queued"          # queued|resolving|streaming|caching|cached|error
    progress: float = 0.0           # 0..100
    direct_url: Optional[str] = None
    filename: Optional[str] = None
    filesize: Optional[int] = None
    error: Optional[str] = None
    error_kind: Optional[str] = None   # None|'rd_infringing'|'rd_error' — lets the client auto-advance

    # Timestamps (ISO-8601 UTC)
    created_at: str = ""
    updated_at: str = ""


# ── DB schema ──────────────────────────────────────────────────────────────
_SCHEMA = """
CREATE TABLE IF NOT EXISTS stremio_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    imdb_id TEXT NOT NULL,
    type TEXT NOT NULL DEFAULT 'movie',
    title TEXT NOT NULL DEFAULT '',
    infohash TEXT NOT NULL DEFAULT '',
    magnet TEXT NOT NULL DEFAULT '',
    file_index INTEGER,
    source_stream_title TEXT,
    quality TEXT,
    expected_size INTEGER,
    status TEXT NOT NULL DEFAULT 'queued',
    progress REAL NOT NULL DEFAULT 0,
    direct_url TEXT,
    filename TEXT,
    filesize INTEGER,
    error TEXT,
    error_kind TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_stremio_jobs_status ON stremio_jobs(status);
CREATE INDEX IF NOT EXISTS ix_stremio_jobs_infohash ON stremio_jobs(infohash);

-- Releases Real-Debrid permanently refuses (HTTP 451 / error_code 35).
-- Keyed by infohash so we never waste a resolve round-trip on a known-dead
-- release and the client can skip straight to the next source.
CREATE TABLE IF NOT EXISTS stremio_blocked_infohashes (
    infohash TEXT PRIMARY KEY,
    reason TEXT NOT NULL DEFAULT 'rd_infringing',
    title TEXT,
    blocked_at TEXT NOT NULL
);
"""


async def init_schema() -> None:
    """Create tables if missing. Called at app startup."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(_SCHEMA)
        # Migration: add error_kind to pre-existing stremio_jobs tables.
        cols = [r[1] for r in await (await db.execute(
            "PRAGMA table_info(stremio_jobs)")).fetchall()]
        if "error_kind" not in cols:
            await db.execute("ALTER TABLE stremio_jobs ADD COLUMN error_kind TEXT")
        await db.commit()


# ── Infringing-release blocklist ───────────────────────────────────────────
async def is_infohash_blocked(infohash: str) -> Optional[str]:
    """Return the block reason if this infohash is on the RD takedown list,
    else None."""
    if not infohash:
        return None
    async with aiosqlite.connect(DB_PATH) as db:
        row = await (await db.execute(
            "SELECT reason FROM stremio_blocked_infohashes WHERE infohash = ?",
            (infohash.lower(),),
        )).fetchone()
    return row[0] if row else None


async def blocked_infohashes(infohashes: list[str]) -> set[str]:
    """Return the subset of the given infohashes that are RD-blocked. Used to
    grey out dead sources in the streams list without probing RD."""
    hashes = [h.lower() for h in infohashes if h]
    if not hashes:
        return set()
    placeholders = ",".join("?" * len(hashes))
    async with aiosqlite.connect(DB_PATH) as db:
        rows = await (await db.execute(
            f"SELECT infohash FROM stremio_blocked_infohashes "
            f"WHERE infohash IN ({placeholders})", hashes,
        )).fetchall()
    return {r[0] for r in rows}


async def block_infohash(infohash: str, *, reason: str = "rd_infringing",
                         title: Optional[str] = None) -> None:
    """Record a release as permanently RD-blocked. Idempotent."""
    if not infohash:
        return
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO stremio_blocked_infohashes (infohash, reason, title, blocked_at) "
            "VALUES (?,?,?,?) ON CONFLICT(infohash) DO UPDATE SET "
            "reason=excluded.reason, blocked_at=excluded.blocked_at",
            (infohash.lower(), reason, title, _now()),
        )
        await db.commit()


# ── DB helpers ─────────────────────────────────────────────────────────────
def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _row_to_job(r) -> StremioJob:
    return StremioJob(
        id=r["id"], imdb_id=r["imdb_id"], type=r["type"], title=r["title"],
        infohash=r["infohash"], magnet=r["magnet"], file_index=r["file_index"],
        source_stream_title=r["source_stream_title"], quality=r["quality"],
        expected_size=r["expected_size"], status=r["status"],
        progress=r["progress"], direct_url=r["direct_url"], filename=r["filename"],
        filesize=r["filesize"], error=r["error"],
        error_kind=(r["error_kind"] if "error_kind" in r.keys() else None),
        created_at=r["created_at"], updated_at=r["updated_at"],
    )


async def enqueue(*, imdb_id: str, type_: str, title: str,
                  infohash: str, magnet: str,
                  file_index: Optional[int] = None,
                  source_stream_title: Optional[str] = None,
                  quality: Optional[str] = None,
                  expected_size: Optional[int] = None) -> int:
    """Add a new job. Returns job_id. If a previous successful grab for the
    same infohash exists in cache, returns a pseudo-job with status='cached'
    so the UI can skip directly to playback."""
    # Known-takedown fast path: RD already refused this release once. Skip the
    # round-trip and return a job already flagged so the client auto-advances.
    if await is_infohash_blocked(infohash):
        now = _now()
        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute(
                "INSERT INTO stremio_jobs (imdb_id, type, title, infohash, magnet, "
                "file_index, source_stream_title, quality, expected_size, status, "
                "progress, error, error_kind, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,'error',0,?,'rd_infringing',?,?)",
                (imdb_id, type_, title, infohash, magnet, file_index,
                 source_stream_title, quality, expected_size,
                 "Not cached on Real-Debrid — try another source",
                 now, now),
            )
            await db.commit()
            return cur.lastrowid

    # Cache-hit fast path
    existing = _cache.find_by_infohash(infohash)
    if existing:
        # Touch last_played; record a synthetic cached-job entry so UI
        # gets a consistent shape.
        _cache.touch_last_played(infohash)
        now = _now()
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "INSERT INTO stremio_jobs (imdb_id, type, title, infohash, magnet, "
                "file_index, source_stream_title, quality, expected_size, status, "
                "progress, filename, filesize, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,'cached',100,?,?,?,?)",
                (imdb_id, type_, title, infohash, magnet, file_index,
                 source_stream_title, quality, expected_size,
                 existing.filename, existing.filesize, now, now),
            )
            await db.commit()
            return cur.lastrowid

    # Fresh job
    now = _now()
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "INSERT INTO stremio_jobs (imdb_id, type, title, infohash, magnet, "
            "file_index, source_stream_title, quality, expected_size, "
            "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (imdb_id, type_, title, infohash, magnet, file_index,
             source_stream_title, quality, expected_size, now, now),
        )
        await db.commit()
        return cur.lastrowid


async def get_job(job_id: int) -> Optional[StremioJob]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        row = await (await db.execute(
            "SELECT * FROM stremio_jobs WHERE id = ?", (job_id,)
        )).fetchone()
    return _row_to_job(row) if row else None


async def list_jobs(*, limit: int = 50) -> list[StremioJob]:
    """Recent jobs across all states. Active first, then most recent."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        rows = await (await db.execute(
            "SELECT * FROM stremio_jobs ORDER BY "
            "  CASE status WHEN 'resolving' THEN 0 WHEN 'streaming' THEN 1 "
            "              WHEN 'caching' THEN 2 WHEN 'queued' THEN 3 "
            "              ELSE 4 END, id DESC LIMIT ?", (limit,)
        )).fetchall()
    return [_row_to_job(r) for r in rows]


async def _update(job_id: int, **fields) -> None:
    """Partial-update a job row. Always bumps updated_at."""
    fields["updated_at"] = _now()
    keys = ", ".join(f"{k} = ?" for k in fields.keys())
    values = list(fields.values())
    values.append(job_id)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(f"UPDATE stremio_jobs SET {keys} WHERE id = ?", values)
        await db.commit()


# ── Worker — runs in the FastAPI event loop ────────────────────────────────
_sem: Optional[asyncio.Semaphore] = None
_workers_started = False


def _make_sem() -> asyncio.Semaphore:
    global _sem
    if _sem is None:
        _sem = asyncio.Semaphore(MAX_CONCURRENT)
    return _sem


async def _process_job(job: StremioJob) -> None:
    """One job's lifecycle: resolve → stream → cache → done."""
    sem = _make_sem()
    async with sem:
        try:
            # ── 1. Resolve via RD (long-poll inside realdebrid module) ─────
            await _update(job.id, status="resolving", error=None)
            try:
                files = await asyncio.to_thread(
                    _rd.magnet_to_direct_urls, job.magnet, timeout=RESOLVE_TIMEOUT,
                )
            except _rd.RealDebridError as e:
                if e.is_infringing:
                    # RD has taken this exact release down for copyright. It will
                    # never resolve — block the infohash so it's not retried and
                    # flag the job so the client can jump to another source.
                    await block_infohash(job.infohash, reason="rd_infringing",
                                         title=job.title)
                    await _update(
                        job.id, status="error", error_kind="rd_infringing",
                        error="Not cached on Real-Debrid — try another source",
                    )
                else:
                    await _update(job.id, status="error", error_kind="rd_error",
                                  error=f"RD: {e}")
                return
            if not files:
                await _update(job.id, status="error", error="RD returned no files")
                return
            # Pick the largest playable file (skip extras / samples). RD
            # already applies a min-size filter; here we just take the biggest.
            picked = max(files, key=lambda f: f.filesize)

            # Run LRU before downloading so we don't fail mid-write
            await asyncio.to_thread(_cache.evict_if_needed)

            await _update(job.id,
                          status="streaming",
                          direct_url=picked.direct_url,
                          filename=picked.filename,
                          filesize=picked.filesize)

            # ── 2. Cache to disk via stdlib (range-aware streaming) ────────
            # aria2 would be faster but it'd be an extra subprocess dependency
            # for marginal gain; stdlib + write-to-tempfile-and-rename is
            # bullet-proof at single-user scale.
            await _download_to_cache(job.id, picked.direct_url,
                                       picked.filename, picked.filesize,
                                       imdb_id=job.imdb_id, infohash=job.infohash,
                                       magnet=job.magnet, title=job.title,
                                       mime=picked.mime_type,
                                       source_stream_title=job.source_stream_title)
            await _update(job.id, status="cached", progress=100.0)
        except Exception as e:
            logger.exception("stremio_queue: job %s blew up", job.id)
            await _update(job.id, status="error", error=str(e)[:500])


async def _download_to_cache(job_id: int, url: str, filename: str,
                              expected_size: int, *,
                              imdb_id: str, infohash: str, magnet: str,
                              title: str, mime: Optional[str],
                              source_stream_title: Optional[str]) -> None:
    """Stream the RD URL into G:\\YT-DLP\\Stremio\\<imdb_id>\\filename.

    Writes to a `.part` tempfile and atomically renames on completion so a
    crashed download never leaves a half-written file that looks complete.
    Updates progress every ~2 seconds."""
    cache_dir = _cache.STREMIO_ROOT / imdb_id
    cache_dir.mkdir(parents=True, exist_ok=True)
    target = cache_dir / filename
    part = cache_dir / (filename + ".part")

    def _blocking_download():
        req = urllib.request.Request(url, headers={
            "User-Agent": "SMDL/Stremio-Cache",
            "Accept": "*/*",
        })
        with urllib.request.urlopen(req, timeout=120) as r:
            total = int(r.headers.get("Content-Length") or expected_size or 0)
            written = 0
            last_emit = 0.0
            with open(part, "wb") as f:
                while True:
                    chunk = r.read(1024 * 512)
                    if not chunk:
                        break
                    f.write(chunk)
                    written += len(chunk)
                    now = time.time()
                    if total > 0 and (now - last_emit) > 2.0:
                        pct = max(0.0, min(99.5, 100.0 * written / total))
                        # Async update from sync code — schedule via
                        # asyncio.run_coroutine_threadsafe in the wrapper.
                        _progress_emit(job_id, pct)
                        last_emit = now
            return written
    written = await asyncio.to_thread(_blocking_download)
    # Atomic rename
    part.replace(target)
    # Persist .meta.json
    await asyncio.to_thread(
        _cache.record,
        imdb_id=imdb_id, infohash=infohash, magnet=magnet,
        title=title, filename=filename, filesize=written, mime=mime,
        source_stream_title=source_stream_title,
    )


def _progress_emit(job_id: int, pct: float) -> None:
    """Sync→async bridge. Schedules an UPDATE on the running loop."""
    try:
        loop = asyncio.get_event_loop()
        asyncio.run_coroutine_threadsafe(
            _update(job_id, progress=pct), loop,
        )
    except Exception:
        pass


# ── Scheduler: pick queued jobs off the table and process ──────────────────
async def _scheduler_loop():
    logger.info("stremio_queue: scheduler started (max_concurrent=%d)", MAX_CONCURRENT)
    # Re-queue interrupted jobs from a prior boot
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE stremio_jobs SET status='queued' "
            "WHERE status IN ('resolving','streaming','caching')"
        )
        await db.commit()

    while True:
        try:
            async with aiosqlite.connect(DB_PATH) as db:
                db.row_factory = aiosqlite.Row
                rows = await (await db.execute(
                    "SELECT * FROM stremio_jobs WHERE status='queued' "
                    "ORDER BY id LIMIT 4"
                )).fetchall()
            for r in rows:
                # Fire-and-forget; semaphore in _process_job caps actual
                # concurrency. Status flip to non-queued is also inside
                # _process_job so polls don't pick the same row twice.
                job = _row_to_job(r)
                await _update(job.id, status="resolving")
                asyncio.create_task(_process_job(job))
        except Exception:
            logger.exception("stremio_queue: scheduler tick failed")
        await asyncio.sleep(2.0)


def start_worker(loop: Optional[asyncio.AbstractEventLoop] = None) -> None:
    """Idempotent — fires the scheduler exactly once per process."""
    global _workers_started
    if _workers_started:
        return
    _workers_started = True
    loop = loop or asyncio.get_event_loop()
    loop.create_task(_scheduler_loop())
