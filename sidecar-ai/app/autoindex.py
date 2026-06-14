"""Background auto-index sweep (Phase E).

Periodically indexes new long-form video/audio so the owner's library becomes
searchable without manual tapping — DECOUPLED from the download pipeline (it just
watches the read-only media root), so it can never break or slow a download.

Safety rails: degrade-dark (MEDIA_AI_AUTOINDEX), scoped to configured subdirs +
a min file size (skips short music reels), bounded per cycle, one file at a time,
CPU-only (transcribe + translate + embed — never the GPU/summary). Every error is
swallowed so the loop survives a bad file or a transient hiccup.
"""
from __future__ import annotations

import asyncio
import logging
import os

from . import config, store

logger = logging.getLogger("media-ai.autoindex")

_MEDIA_EXT = {".mp4", ".mkv", ".webm", ".mov", ".m4v",
              ".m4a", ".mp3", ".aac", ".flac", ".wav", ".opus", ".ogg"}


def _candidates() -> list[str]:
    """Relative paths of media files under the configured long-form dirs that are
    large enough to be worth indexing."""
    root = config.DOWNLOADS_DIR
    out: list[str] = []
    for sub in config.AUTOINDEX_DIRS:
        base = os.path.join(root, sub)
        if not os.path.isdir(base):
            continue
        for dirpath, _dirs, files in os.walk(base):
            for f in files:
                if os.path.splitext(f)[1].lower() not in _MEDIA_EXT:
                    continue
                full = os.path.join(dirpath, f)
                try:
                    if os.path.getsize(full) < config.AUTOINDEX_MIN_BYTES:
                        continue
                except OSError:
                    continue
                out.append(os.path.relpath(full, root).replace("\\", "/"))
    return out


async def run_loop(index_one):
    """index_one(rel_path) is an async fn that indexes one file. Started as a
    background task from app startup; returns immediately if disabled."""
    if not config.AUTOINDEX:
        logger.info("autoindex disabled")
        return
    logger.info("autoindex ON — dirs=%s every %ss (<%s skipped, %s/cycle)",
                config.AUTOINDEX_DIRS, config.AUTOINDEX_INTERVAL,
                config.AUTOINDEX_MIN_BYTES, config.AUTOINDEX_MAX_PER_CYCLE)
    # Small initial delay so startup/warmup finishes first.
    await asyncio.sleep(15)
    while True:
        try:
            cands = await asyncio.to_thread(_candidates)
            todo: list[str] = []
            if cands:
                status = await asyncio.to_thread(store.media_status, cands)
                todo = [p for p in cands
                        if not status.get(p, {}).get("transcribed")][:config.AUTOINDEX_MAX_PER_CYCLE]
            for p in todo:
                try:
                    res = await index_one(p)
                    logger.info("autoindexed %s (%s)", p, (res or {}).get("indexed"))
                except Exception as e:  # noqa: BLE001
                    logger.warning("autoindex %s failed: %s", p, e)
        except Exception as e:  # noqa: BLE001
            logger.warning("autoindex cycle error: %s", e)
        await asyncio.sleep(config.AUTOINDEX_INTERVAL)
