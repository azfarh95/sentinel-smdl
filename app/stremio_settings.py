"""Theater module owner-configurable settings.

Stored in /data/jobs.db (single-row `stremio_settings` table) so it
persists across container restarts and survives image rebuilds.

Settings:
  • default_quality       (str: '2160p' | '1080p' | '720p' | 'any')
  • cache_max_gb          (float | None — None disables hard cap)
  • addons                (list[str] of manifest URLs — overrides defaults)
  • auto_grab_top_seeded  (bool — for series, grab top-seeded stream automatically per ep)
  • last_position_<imdb>  (dict[str, float] — per-title resume seconds)
"""
from __future__ import annotations

import json
import logging
from typing import Optional

import aiosqlite

from .database import DB_PATH

logger = logging.getLogger(__name__)

_DEFAULTS: dict = {
    "default_quality": "1080p",
    "cache_max_gb": None,
    "addons": [],                    # empty = use stremio.DEFAULT_ADDONS
    "auto_grab_top_seeded": False,
}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS stremio_settings (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    data TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS stremio_progress (
    imdb_id TEXT PRIMARY KEY,        -- raw Stremio content_id (incl. SxxExx)
    position_seconds REAL NOT NULL,
    duration_seconds REAL,
    updated_at TEXT NOT NULL
);
"""


async def init_schema() -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(_SCHEMA)
        # Ensure the singleton row exists
        await db.execute(
            "INSERT OR IGNORE INTO stremio_settings (id, data) VALUES (1, ?)",
            (json.dumps(_DEFAULTS),),
        )
        await db.commit()


async def get_all() -> dict:
    """Current settings, merged with defaults so newly-added keys exist."""
    async with aiosqlite.connect(DB_PATH) as db:
        row = await (await db.execute(
            "SELECT data FROM stremio_settings WHERE id = 1"
        )).fetchone()
    raw = json.loads(row[0]) if row else {}
    return {**_DEFAULTS, **raw}


async def update(patch: dict) -> dict:
    """Merge a partial update into the stored settings. Validates known
    keys; ignores unknown ones silently (forward-compat for future keys)."""
    current = await get_all()
    for k, v in patch.items():
        if k in _DEFAULTS:
            current[k] = v
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE stremio_settings SET data = ? WHERE id = 1",
            (json.dumps(current),),
        )
        await db.commit()
    return current


# ── Resume position helpers ────────────────────────────────────────────────
async def save_position(imdb_id: str, position_s: float,
                          duration_s: Optional[float] = None) -> None:
    """Persist the user's playback position for resume-on-replay."""
    if position_s <= 0:
        return
    import datetime as _dt
    iso = _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO stremio_progress (imdb_id, position_seconds, duration_seconds, updated_at) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(imdb_id) DO UPDATE SET "
            "  position_seconds = excluded.position_seconds, "
            "  duration_seconds = excluded.duration_seconds, "
            "  updated_at = excluded.updated_at",
            (imdb_id, position_s, duration_s, iso),
        )
        await db.commit()


async def get_position(imdb_id: str) -> Optional[dict]:
    """Returns {position_seconds, duration_seconds, updated_at} or None."""
    async with aiosqlite.connect(DB_PATH) as db:
        row = await (await db.execute(
            "SELECT position_seconds, duration_seconds, updated_at "
            "FROM stremio_progress WHERE imdb_id = ?", (imdb_id,)
        )).fetchone()
    if not row:
        return None
    return {"position_seconds": row[0], "duration_seconds": row[1],
            "updated_at": row[2]}


async def clear_position(imdb_id: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM stremio_progress WHERE imdb_id = ?", (imdb_id,))
        await db.commit()
