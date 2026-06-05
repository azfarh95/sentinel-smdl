"""v3.6-D — rolling-HLS DVR buffer (pause / rewind live TV), self path.

For an opted-in channel we run one ffmpeg process that copies the live stream
into a **rolling HLS window** on disk (`delete_segments` GCs old segments), so the
player can seek back across the buffered window even when the provider has no
catch-up. This is the "self path" of v3.6-D; the cheap "provider catch-up" path
(XMLTV `catchup-source`) lives in `iptv_routes`.

Safety rails (this is the heaviest/riskiest SMDL feature — keep it bounded):
- **Per-channel disk** is bounded by `hls_list_size` (window) — ffmpeg deletes
  rotated-out segments itself.
- **Concurrency cap** `MAX_DVR_BUFFERS` (LRU-evict when exceeded).
- **Idle GC** — a buffer not accessed within `DVR_IDLE_TIMEOUT_S` is stopped +
  its dir removed. Crashed ffmpeg processes are reaped the same way.
- **Opt-in only** — nothing auto-buffers; a buffer starts on an explicit request.
- The buffer root is wiped at startup (stale dirs from a crash) and on shutdown.

Reuses the reliability-ranked source pick from v3.6-B so the buffer captures the
healthiest source (and resolves youtube-live like the relay does).
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import time
from pathlib import Path

from . import iptv as _iptv

logger = logging.getLogger(__name__)

DVR_ROOT            = Path(os.environ.get("IPTV_DVR_DIR", "/downloads/.iptv_dvr"))
DVR_SEGMENT_SECS    = int(os.environ.get("IPTV_DVR_SEGMENT_SECS", "6"))
DVR_WINDOW_SEGMENTS = int(os.environ.get("IPTV_DVR_WINDOW_SEGMENTS", "300"))  # ~30 min
MAX_DVR_BUFFERS     = int(os.environ.get("IPTV_DVR_MAX_BUFFERS", "2"))
DVR_IDLE_TIMEOUT_S  = int(os.environ.get("IPTV_DVR_IDLE_TIMEOUT_S", "180"))

# channel_id -> {proc, dir, started_at, last_access}
_buffers: dict[str, dict] = {}
_lock = asyncio.Lock()


def _safe(cid: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", cid)[:80] or "ch"


async def _resolve_url(channel_id: str) -> str | None:
    """Best playable URL for a channel — reliability-ranked source (v3.6-B),
    resolving youtube-live to its live manifest like the HLS relay does."""
    from . import iptv_dedup as _dedup
    url = src = None
    pick = await _dedup.pick_best_source(channel_id)
    if pick and pick.get("url"):
        url, src = pick["url"], pick.get("source")
    else:
        ch = await _iptv.get_channel(channel_id)
        if ch and ch.url:
            url, src = ch.url, ch.source
    if not url:
        return None
    if src == "youtube-live":
        try:
            from . import iptv_youtube
            return await iptv_youtube.resolve_live_url(url)
        except Exception as exc:
            logger.warning("iptv dvr: youtube resolve failed for %s: %s", channel_id, exc)
            return None
    return url


def status(channel_id: str) -> dict:
    b = _buffers.get(channel_id)
    if not b:
        return {"active": False, "ready": False}
    bdir: Path = b["dir"]
    segs = len(list(bdir.glob("seg_*.ts"))) if bdir.exists() else 0
    alive = b["proc"].returncode is None
    return {
        "active": True,
        "alive": alive,
        "ready": (bdir / "index.m3u8").exists() and segs > 0,
        "segments": segs,
        "window_secs": segs * DVR_SEGMENT_SECS,
        "started_at": b["started_at"],
        "playlist_url": f"/iptv/dvr/{channel_id}/index.m3u8",
    }


async def start(channel_id: str) -> dict:
    """Start (or touch) the rolling buffer for a channel. LRU-evicts when at cap."""
    async with _lock:
        if channel_id in _buffers and _buffers[channel_id]["proc"].returncode is None:
            _buffers[channel_id]["last_access"] = time.monotonic()
            return status(channel_id)
        _buffers.pop(channel_id, None)
        while len(_buffers) >= MAX_DVR_BUFFERS:
            lru = min(_buffers, key=lambda k: _buffers[k]["last_access"])
            await _stop_locked(lru)
        url = await _resolve_url(channel_id)
        if not url:
            raise ValueError("no playable source for channel")
        bdir = DVR_ROOT / _safe(channel_id)
        shutil.rmtree(bdir, ignore_errors=True)
        bdir.mkdir(parents=True, exist_ok=True)
        args = [
            "ffmpeg", "-hide_banner", "-loglevel", "warning",
            "-rw_timeout", "10000000",
            "-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "5",
            "-i", url,
            "-c", "copy",
            "-f", "hls",
            "-hls_time", str(DVR_SEGMENT_SECS),
            "-hls_list_size", str(DVR_WINDOW_SEGMENTS),
            "-hls_flags", "delete_segments+append_list+omit_endlist",
            "-hls_segment_type", "mpegts",
            "-hls_segment_filename", str(bdir / "seg_%05d.ts"),
            str(bdir / "index.m3u8"),
        ]
        proc = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        _buffers[channel_id] = {
            "proc": proc, "dir": bdir,
            "started_at": _iptv._iso_now(), "last_access": time.monotonic(),
        }
        logger.info("iptv dvr: buffer started for %s (pid=%s, cap=%d)",
                    channel_id, proc.pid, MAX_DVR_BUFFERS)
        return status(channel_id)


async def _stop_locked(channel_id: str) -> None:
    b = _buffers.pop(channel_id, None)
    if not b:
        return
    proc = b["proc"]
    try:
        if proc.returncode is None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                proc.kill()
    except Exception:
        pass
    shutil.rmtree(b["dir"], ignore_errors=True)
    logger.info("iptv dvr: buffer stopped for %s", channel_id)


async def stop(channel_id: str) -> None:
    async with _lock:
        await _stop_locked(channel_id)


def touch(channel_id: str) -> None:
    b = _buffers.get(channel_id)
    if b:
        b["last_access"] = time.monotonic()


def playlist_path(channel_id: str) -> Path | None:
    b = _buffers.get(channel_id)
    if not b:
        return None
    p = b["dir"] / "index.m3u8"
    return p if p.exists() else None


def segment_path(channel_id: str, name: str) -> Path | None:
    b = _buffers.get(channel_id)
    if not b or not re.fullmatch(r"seg_\d+\.ts", name or ""):
        return None
    p = b["dir"] / name
    return p if p.exists() else None


async def _gc_loop() -> None:
    while True:
        await asyncio.sleep(30)
        try:
            now = time.monotonic()
            async with _lock:
                stale = [cid for cid, b in _buffers.items()
                         if (now - b["last_access"]) > DVR_IDLE_TIMEOUT_S
                         or b["proc"].returncode is not None]
                for cid in stale:
                    await _stop_locked(cid)
        except Exception:
            logger.exception("iptv dvr: gc tick failed")


_gc_started = False


def start_gc_loop() -> None:
    """Idempotent — wipe any stale buffer dirs from a prior crash + start GC."""
    global _gc_started
    if _gc_started:
        return
    _gc_started = True
    try:
        shutil.rmtree(DVR_ROOT, ignore_errors=True)
        DVR_ROOT.mkdir(parents=True, exist_ok=True)
    except Exception:
        logger.warning("iptv dvr: could not reset buffer root %s", DVR_ROOT)
    asyncio.create_task(_gc_loop())
    logger.info("iptv dvr: gc loop started (max=%d, window=%ds, idle=%ds)",
                MAX_DVR_BUFFERS, DVR_WINDOW_SEGMENTS * DVR_SEGMENT_SECS, DVR_IDLE_TIMEOUT_S)


async def shutdown() -> None:
    async with _lock:
        for cid in list(_buffers):
            await _stop_locked(cid)
