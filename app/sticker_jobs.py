"""Background job queue for heavy sticker encodes (v2.7 Phase B).

A sticker encode (per-frame cutout, transparent video, plain video sticker)
takes 5–30 s — far too long to hold an HTTP request open while the Mini App
spins. This module moves those encodes onto a small background worker so
`/make` returns instantly with a `job_id`; the editor polls
`/api/sticker_jobs/{id}` for a progress bar and the user gets a Telegram push
when the sticker lands in their pack (so they can leave the Mini App).

Modeled on `stremio_queue.py` (the Theater grab queue): a `sticker_jobs`
table + a semaphore-capped scheduler loop draining `queued` rows. Heavy CPU
work already offloads to `run_in_executor` inside `sticker_processor`, so the
event loop stays responsive; the semaphore just caps how many encodes run at
once.

State machine:
    queued ──► running ──► done
                  └──────► error

Persistence: jobs live in the existing SMDL SQLite (`sticker_jobs` table) so
they survive a container restart. On boot the scheduler re-queues any job left
`running` by a crash/restart (the encode is idempotent — it re-runs from the
draft on disk).

The actual encode + Telegram publish lives in `sticker_routes.build_and_publish`
(next to the request model + processor helpers it already uses); the worker
imports it lazily to avoid an import cycle.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Optional

import aiosqlite

from .database import DB_PATH

logger = logging.getLogger(__name__)

# Small concurrency cap — sticker encodes are CPU-heavy (ffmpeg / rembg /
# vpxenc). CPU matting is parallel-safe (per ADR MED-003), so 2 lets a static
# encode overlap a video one without thrashing the box.
MAX_CONCURRENT = int(os.environ.get("STICKER_MAX_CONCURRENT", "2"))


class StickerBuildError(Exception):
    """Expected, user-surfaceable failure in the encode/publish pipeline.

    Carries an HTTP status so the synchronous `/make` fast-path can map it
    straight to an HTTPException; the worker just records `.detail` on the job.
    """

    def __init__(self, detail: str, *, status: int = 422):
        super().__init__(detail)
        self.detail = detail
        self.status = status


@dataclass
class StickerJob:
    id: int = 0
    user_id: int = 0
    draft_id: int = 0
    kind: str = ""                 # video | video_cutout | video_transparent | custom_emoji
    params_json: str = "{}"        # serialized MakeStickerBody (+ _first_name)
    status: str = "queued"         # queued | running | done | error
    progress: float = 0.0          # 0..100
    result_json: Optional[str] = None   # {sticker_file_id, set_url, pack_name}
    error: Optional[str] = None
    created_at: str = ""
    updated_at: str = ""


# ── DB schema ───────────────────────────────────────────────────────────────
_SCHEMA = """
CREATE TABLE IF NOT EXISTS sticker_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    draft_id INTEGER NOT NULL,
    kind TEXT NOT NULL DEFAULT '',
    params_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'queued',
    progress REAL NOT NULL DEFAULT 0,
    result_json TEXT,
    error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_sticker_jobs_status ON sticker_jobs(status);
CREATE INDEX IF NOT EXISTS ix_sticker_jobs_user ON sticker_jobs(user_id, id);
"""


async def init_schema() -> None:
    """Create the jobs table if missing. Called at app startup."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(_SCHEMA)
        await db.commit()


# ── helpers ──────────────────────────────────────────────────────────────────
def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _row_to_job(r) -> StickerJob:
    return StickerJob(
        id=r["id"], user_id=r["user_id"], draft_id=r["draft_id"],
        kind=r["kind"], params_json=r["params_json"], status=r["status"],
        progress=r["progress"], result_json=r["result_json"], error=r["error"],
        created_at=r["created_at"], updated_at=r["updated_at"],
    )


async def enqueue(*, user_id: int, draft_id: int, kind: str, params: dict) -> int:
    """Add a queued job. Returns job_id. Flips the draft to 'processing' so the
    drafts list reflects the in-flight encode immediately."""
    now = _now()
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "INSERT INTO sticker_jobs (user_id, draft_id, kind, params_json, "
            "status, progress, created_at, updated_at) "
            "VALUES (?,?,?,?,'queued',0,?,?)",
            (user_id, draft_id, kind, json.dumps(params), now, now),
        )
        await db.commit()
        job_id = cur.lastrowid
    try:
        from . import database as _db
        await _db.sticker_draft_set_status(draft_id, "processing")
    except Exception:
        logger.debug("enqueue: draft status flip failed (d=%s)", draft_id)
    return job_id


async def get_job(job_id: int) -> Optional[StickerJob]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        row = await (await db.execute(
            "SELECT * FROM sticker_jobs WHERE id = ?", (job_id,)
        )).fetchone()
    return _row_to_job(row) if row else None


async def _update(job_id: int, **fields) -> None:
    """Partial-update a job row. Always bumps updated_at."""
    fields["updated_at"] = _now()
    keys = ", ".join(f"{k} = ?" for k in fields)
    values = list(fields.values())
    values.append(job_id)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(f"UPDATE sticker_jobs SET {keys} WHERE id = ?", values)
        await db.commit()


def _make_progress_setter(job_id: int,
                          loop: asyncio.AbstractEventLoop) -> Callable[[float], None]:
    """Return a thread-safe absolute-percent (0..100) progress setter.

    `build_and_publish` and the encoders call this with an absolute percent;
    some calls originate inside `run_in_executor` worker threads (per-frame
    matting), so we schedule the DB update back onto the loop via
    `run_coroutine_threadsafe`. Throttled to ≥1% / ≥0.4 s to avoid hammering
    SQLite with a write per frame."""
    state = {"last": -1.0, "last_t": 0.0}

    def setp(pct: float) -> None:
        try:
            pct = max(0.0, min(100.0, float(pct)))
        except (TypeError, ValueError):
            return
        now = time.monotonic()
        if pct < 100.0 and abs(pct - state["last"]) < 1.0 and (now - state["last_t"]) < 0.4:
            return
        state["last"] = pct
        state["last_t"] = now
        try:
            asyncio.run_coroutine_threadsafe(
                _update(job_id, progress=round(pct, 1)), loop)
        except Exception:
            pass

    return setp


# ── Worker ───────────────────────────────────────────────────────────────────
_sem: Optional[asyncio.Semaphore] = None
_workers_started = False


def _make_sem() -> asyncio.Semaphore:
    global _sem
    if _sem is None:
        _sem = asyncio.Semaphore(MAX_CONCURRENT)
    return _sem


async def _process_job(job: StickerJob) -> None:
    """One job's lifecycle: reconstruct the request body → encode → publish to
    the user's Telegram pack → push confirmation. Records progress on the row."""
    sem = _make_sem()
    async with sem:
        loop = asyncio.get_running_loop()
        from . import sticker_routes as _routes   # lazy — avoids import cycle
        try:
            await _update(job.id, status="running", progress=5.0, error=None)
            params = json.loads(job.params_json or "{}")
            first_name = params.pop("_first_name", None)
            body = _routes.MakeStickerBody(**params)
            setp = _make_progress_setter(job.id, loop)
            result = await _routes.build_and_publish(
                job.user_id, first_name, job.draft_id, body, progress=setp)
            await _update(job.id, status="done", progress=100.0,
                          result_json=json.dumps(result), error=None)
            logger.info("sticker job %s done (u=%s d=%s)",
                        job.id, job.user_id, job.draft_id)
        except StickerBuildError as e:
            await _update(job.id, status="error", error=str(e.detail)[:500])
            logger.info("sticker job %s failed: %s", job.id, e.detail)
        except Exception as e:
            logger.exception("sticker_jobs: job %s blew up", job.id)
            await _update(job.id, status="error", error=str(e)[:500])


async def _scheduler_loop() -> None:
    logger.info("sticker_jobs: scheduler started (max_concurrent=%d)", MAX_CONCURRENT)
    # Re-queue jobs interrupted by a prior crash/restart. The encode re-runs
    # from the draft on disk, so this is safe + idempotent.
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute(
                "UPDATE sticker_jobs SET status='queued' WHERE status='running'")
            await db.commit()
            if cur.rowcount:
                logger.info("sticker_jobs: re-queued %d interrupted job(s)", cur.rowcount)
    except Exception:
        logger.exception("sticker_jobs: boot re-queue failed")

    while True:
        try:
            async with aiosqlite.connect(DB_PATH) as db:
                db.row_factory = aiosqlite.Row
                rows = await (await db.execute(
                    "SELECT * FROM sticker_jobs WHERE status='queued' "
                    "ORDER BY id LIMIT 4"
                )).fetchall()
            for r in rows:
                # Flip to 'running' before spawning so the next tick can't pick
                # the same row twice; the semaphore in _process_job caps actual
                # concurrency.
                job = _row_to_job(r)
                await _update(job.id, status="running")
                asyncio.create_task(_process_job(job))
        except Exception:
            logger.exception("sticker_jobs: scheduler tick failed")
        await asyncio.sleep(1.5)


def start_worker(loop: Optional[asyncio.AbstractEventLoop] = None) -> None:
    """Idempotent — fires the scheduler exactly once per process."""
    global _workers_started
    if _workers_started:
        return
    _workers_started = True
    loop = loop or asyncio.get_event_loop()
    loop.create_task(_scheduler_loop())
