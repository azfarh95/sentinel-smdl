"""Follow-a-show — Sonarr-lite auto-download of new episodes.

Follow a series in Theater → a background loop periodically checks for newly
AIRED episodes (using the same Cinemeta episode list the detail view shows) and
auto-enqueues the best available stream through the existing Theater queue
(Real-Debrid → cache). The grabbed episode then just appears in your Library.

Safety guards (so this can never mass-download or loop):
  * Only episodes whose air date is AFTER you followed AND not in the future
    are grabbed — no back-catalog dump, no pre-airing grabs.
  * Each (show, season, episode) is grabbed at most once (grabbed_episodes).
  * A per-check cap (MAX_GRABS_PER_SHOW) bounds the blast radius if a feed
    misbehaves; anything dropped is logged, never silently skipped.
  * The whole loop is best-effort — any failure is logged and the next show /
    next cycle continues.

Tables live in the shared Theater DB (jobs.db). Owner is notified via the SMDL
bot when an episode is auto-grabbed.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import urllib.request
from datetime import datetime, timezone
from typing import Optional

import aiosqlite

from .database import DB_PATH

logger = logging.getLogger(__name__)

# Tunables (env-overridable).
CHECK_INTERVAL_S = int(os.environ.get("FOLLOW_CHECK_INTERVAL_S", str(6 * 3600)))
FIRST_DELAY_S = int(os.environ.get("FOLLOW_FIRST_DELAY_S", "300"))
MAX_GRABS_PER_SHOW = int(os.environ.get("FOLLOW_MAX_GRABS_PER_SHOW", "5"))
ENABLED = os.environ.get("FOLLOW_AUTODL_ENABLED", "1") not in ("0", "false", "no")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


async def init_db() -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "CREATE TABLE IF NOT EXISTS followed_shows ("
            "  imdb_id TEXT PRIMARY KEY,"
            "  title TEXT,"
            "  poster TEXT,"
            "  added_at TEXT NOT NULL,"
            "  last_checked_at TEXT)"
        )
        await db.execute(
            "CREATE TABLE IF NOT EXISTS grabbed_episodes ("
            "  imdb_id TEXT NOT NULL,"
            "  season INTEGER NOT NULL,"
            "  episode INTEGER NOT NULL,"
            "  content_id TEXT,"
            "  grabbed_at TEXT NOT NULL,"
            "  PRIMARY KEY (imdb_id, season, episode))"
        )
        await db.commit()


# ── Follow CRUD ─────────────────────────────────────────────────────────────

async def follow(imdb_id: str, title: str = "", poster: str = "") -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO followed_shows (imdb_id, title, poster, added_at) "
            "VALUES (?,?,?,?) "
            "ON CONFLICT(imdb_id) DO UPDATE SET title=excluded.title, poster=excluded.poster",
            (imdb_id, title, poster, _now()),
        )
        await db.commit()


async def unfollow(imdb_id: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM followed_shows WHERE imdb_id=?", (imdb_id,))
        await db.commit()


async def is_following(imdb_id: str) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        row = await (await db.execute(
            "SELECT 1 FROM followed_shows WHERE imdb_id=?", (imdb_id,))).fetchone()
    return row is not None


async def list_follows() -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        rows = await (await db.execute(
            "SELECT * FROM followed_shows ORDER BY added_at DESC")).fetchall()
    return [dict(r) for r in rows]


async def _is_grabbed(imdb_id: str, season: int, episode: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        row = await (await db.execute(
            "SELECT 1 FROM grabbed_episodes WHERE imdb_id=? AND season=? AND episode=?",
            (imdb_id, season, episode))).fetchone()
    return row is not None


async def _mark_grabbed(imdb_id: str, season: int, episode: int, content_id: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR IGNORE INTO grabbed_episodes "
            "(imdb_id, season, episode, content_id, grabbed_at) VALUES (?,?,?,?,?)",
            (imdb_id, season, episode, content_id, _now()))
        await db.commit()


# ── The auto-download core ──────────────────────────────────────────────────

async def _grab_episode(content_id: str, label: str, show_title: str) -> bool:
    """Resolve the best stream for one episode and enqueue it through the
    Theater queue (which handles RD → download → cache). Returns True if a job
    was enqueued. Best-effort; never raises."""
    from . import stremio as _st, stremio_settings as _ss, stremio_queue as _sq
    try:
        addons = (await _ss.get_all()).get("addons") or None
        raw = await asyncio.to_thread(_st.get_streams, content_id, "series", addons)
        ranked = _st.rank_streams(raw, preferred_quality="1080p")
    except Exception:
        logger.exception("follow: stream lookup failed for %s", content_id)
        return False
    if not ranked:
        logger.info("follow: no streams found for %s", content_id)
        return False
    blocked = await _sq.blocked_infohashes([s.infohash for s in ranked if s.infohash])
    pick = next((s for s in ranked
                 if s.infohash and s.infohash.lower() not in blocked), None)
    if not pick:
        logger.info("follow: every stream for %s is RD-blocked", content_id)
        return False
    magnet = pick.magnet or (
        f"magnet:?xt=urn:btih:{pick.infohash.lower()}" if pick.infohash else None)
    if not magnet:
        return False
    try:
        await _sq.enqueue(
            imdb_id=content_id, type_="series",
            title=f"{show_title} {label}".strip(),
            infohash=pick.infohash, magnet=magnet,
            file_index=getattr(pick, "file_index", None),
            source_stream_title=pick.title,
            quality=getattr(pick, "quality", None),
            expected_size=getattr(pick, "size_bytes", None))
        return True
    except Exception:
        logger.exception("follow: enqueue failed for %s", content_id)
        return False


async def _check_show(entry: dict) -> list[str]:
    """Find newly-aired, not-yet-grabbed episodes for one followed show and
    enqueue them. Returns the human labels of what was enqueued."""
    from . import stremio as _st
    imdb_id = entry["imdb_id"]
    added_at = entry.get("added_at") or ""
    show_title = entry.get("title") or imdb_id
    now_iso = _now()
    enqueued: list[str] = []
    try:
        eps = await asyncio.to_thread(_st.get_series_episodes, imdb_id)
    except Exception:
        logger.exception("follow: episode list failed for %s", imdb_id)
        eps = []

    # Aired since we followed, oldest-first so we grab in order.
    candidates = []
    for ep in eps:
        released = getattr(ep, "released", None)
        if not released:
            continue
        if released > now_iso:          # hasn't aired yet
            continue
        if released < added_at:         # back-catalog — predates the follow
            continue
        candidates.append(ep)
    candidates.sort(key=lambda e: (e.season, e.episode))

    for ep in candidates:
        if len(enqueued) >= MAX_GRABS_PER_SHOW:
            logger.warning("follow: hit per-show cap (%d) for %s — %d candidates left",
                           MAX_GRABS_PER_SHOW, imdb_id, len(candidates) - len(enqueued))
            break
        if await _is_grabbed(imdb_id, ep.season, ep.episode):
            continue
        content_id = f"{imdb_id}:{ep.season}:{ep.episode}"
        label = f"S{ep.season:02d}E{ep.episode:02d}"
        if await _grab_episode(content_id, label, show_title):
            await _mark_grabbed(imdb_id, ep.season, ep.episode, content_id)
            enqueued.append(f"{show_title} {label}")

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE followed_shows SET last_checked_at=? WHERE imdb_id=?",
                         (now_iso, imdb_id))
        await db.commit()
    return enqueued


async def check_all() -> list[str]:
    """One full sweep across every followed show. Returns enqueued labels."""
    grabbed: list[str] = []
    for entry in await list_follows():
        try:
            grabbed.extend(await _check_show(entry))
        except Exception:
            logger.exception("follow: check failed for %s", entry.get("imdb_id"))
    return grabbed


def _notify_owner(labels: list[str]) -> None:
    """Best-effort Telegram nudge to the owner that episodes were auto-grabbed."""
    from .config import OWNER_CHAT_ID
    token = (os.environ.get("SMDL_BOT_TOKEN") or os.environ.get("BOT_TOKEN")
             or os.environ.get("TELEGRAM_BOT_TOKEN"))
    if not token or not OWNER_CHAT_ID or not labels:
        return
    body = "📺 Auto-grabbed new episode(s):\n" + "\n".join(f"• {l}" for l in labels[:20])
    try:
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data=json.dumps({"chat_id": OWNER_CHAT_ID, "text": body,
                             "disable_web_page_preview": True}).encode(),
            headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=8).read()
    except Exception:
        logger.debug("follow: owner notify failed", exc_info=True)


async def check_loop() -> None:
    """Periodic sweep. First run after a short settle delay, then every
    CHECK_INTERVAL_S. Disabled by FOLLOW_AUTODL_ENABLED=0."""
    if not ENABLED:
        logger.info("follow: auto-download loop disabled (FOLLOW_AUTODL_ENABLED=0)")
        return
    await asyncio.sleep(FIRST_DELAY_S)
    while True:
        try:
            labels = await check_all()
            if labels:
                logger.info("follow: auto-grabbed %d episode(s): %s", len(labels), labels)
                await asyncio.to_thread(_notify_owner, labels)
        except Exception:
            logger.exception("follow: check loop error")
        await asyncio.sleep(CHECK_INTERVAL_S)
