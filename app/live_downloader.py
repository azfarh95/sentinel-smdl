"""Livestream recording for SMDL v2.

Design contract (per user 2026-05-10):
- ZERO retries on session/auth failures. If cookies expire or yt-dlp gets a
  401/403/private/login error mid-stream, abort cleanly with the bytes we
  already have. Don't waste hours retrying a failed login.
- Platform whitelist. TikTok and Instagram are explicitly OFF — both have
  hostile anti-bot infra and live recording fails unpredictably. YouTube /
  Twitch / Kick work reliably.
- Disk pre-check. Refuse to start if free space < LIVE_MIN_FREE_DISK_GB.
- Heartbeat every LIVE_HEARTBEAT_SECONDS, not per chunk — Telegram rate
  limits + reduced noise.
- One job per chat at a time (LIVE_MAX_CONCURRENT). Live URLs are long-
  running; queueing them implicitly via the global semaphore would block
  regular downloads.

This module is intentionally separate from `downloader.py` because live is
a different paradigm: long-lived async generator vs short-lived future.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import signal
import time
from pathlib import Path
from typing import AsyncIterator

import yt_dlp

from .config import (
    LIVE_ABORT_ON_SESSION_FAIL,
    LIVE_HEARTBEAT_SECONDS,
    LIVE_MAX_CONCURRENT,
    LIVE_MAX_HEIGHT,
    LIVE_MIN_FREE_DISK_GB,
    LIVE_PLATFORMS,
)

logger = logging.getLogger(__name__)

DOWNLOADS_DIR = os.environ.get("DOWNLOADS_DIR", "/downloads")
LIVE_DIR      = os.path.join(DOWNLOADS_DIR, "live")

# Patterns in yt-dlp errors that indicate session/auth failure rather than
# transient network issues. On any of these, abort instantly — retrying
# will not help.
_AUTH_FAIL_PATTERNS = re.compile(
    r"(?i)("
    r"login\s*required|"
    r"sign\s*in\s*required|"
    r"private\s*video|"
    r"members\s*only|"
    r"403\s*forbidden|"
    r"401\s*unauthorized|"
    r"cookie.*invalid|"
    r"cookies?\s+have\s+expired|"
    r"unable\s+to\s+download.*authentication|"
    r"this\s+content\s+is\s+only\s+available"
    r")"
)


_live_semaphore: asyncio.Semaphore | None = None


def _get_live_semaphore() -> asyncio.Semaphore:
    global _live_semaphore
    if _live_semaphore is None:
        _live_semaphore = asyncio.Semaphore(LIVE_MAX_CONCURRENT)
    return _live_semaphore


def _platform_allowed(url: str) -> tuple[bool, str]:
    u = url.lower()
    if "youtube.com" in u or "youtu.be" in u:
        return ("youtube" in LIVE_PLATFORMS, "youtube")
    if "twitch.tv" in u:
        return ("twitch" in LIVE_PLATFORMS, "twitch")
    if "kick.com" in u:
        return ("kick" in LIVE_PLATFORMS, "kick")
    if "tiktok.com" in u:
        return ("tiktok" in LIVE_PLATFORMS, "tiktok")
    if "instagram.com" in u:
        return ("instagram" in LIVE_PLATFORMS, "instagram")
    if "facebook.com" in u or "fb.com" in u:
        return ("facebook" in LIVE_PLATFORMS, "facebook")
    return (False, "unknown")


def _free_disk_gb(path: str) -> float:
    try:
        usage = shutil.disk_usage(path)
        return usage.free / (1024 ** 3)
    except Exception:
        return -1.0


def detect_live(info: dict | None) -> bool:
    """Return True if yt-dlp's extracted info dict says this is a live stream."""
    if not info:
        return False
    if info.get("is_live"):
        return True
    status = (info.get("live_status") or "").lower()
    return status in ("is_live", "is_upcoming", "post_live")


class LiveAbort(Exception):
    """Raised to signal an abort that should NOT trigger a retry."""
    def __init__(self, reason: str, detail: str = ""):
        self.reason = reason
        self.detail = detail
        super().__init__(f"{reason}: {detail}" if detail else reason)


def _is_auth_failure(err_text: str) -> bool:
    return bool(_AUTH_FAIL_PATTERNS.search(err_text))


def _kill_orphan_ffmpeg_children() -> int:
    """Defensive cleanup: SIGTERM any ffmpeg/ffprobe child of THIS process.

    Even with hls_prefer_native=True, certain code paths (post-process mux,
    fragment merge) can spawn ffmpeg. If LiveAbort fires before yt-dlp
    cleans those up, we'd leak orphan processes that keep writing.

    Returns count of processes signaled. Linux-only (uses /proc).
    """
    if not os.path.isdir("/proc"):
        return 0
    my_pid = os.getpid()
    killed = 0
    try:
        for entry in os.listdir("/proc"):
            if not entry.isdigit():
                continue
            try:
                with open(f"/proc/{entry}/status") as f:
                    status = f.read()
                if f"PPid:\t{my_pid}\n" not in status:
                    continue
                with open(f"/proc/{entry}/comm") as f:
                    comm = f.read().strip()
                if comm in ("ffmpeg", "ffprobe"):
                    pid = int(entry)
                    os.kill(pid, signal.SIGTERM)
                    killed += 1
                    logger.info("killed orphan %s PID %d", comm, pid)
            except (FileNotFoundError, ProcessLookupError, PermissionError):
                continue  # process exited between listdir and inspection
    except Exception as e:
        logger.warning("orphan cleanup failed: %s", e)
    return killed


async def record_live(
    url: str,
    cookiepath: str | None,
    on_progress: callable | None = None,
    stop_flag: dict | None = None,
) -> dict:
    """Record an active livestream.

    Returns:
        {
            "ok": bool,
            "files": [filepath],
            "duration_seconds": int,
            "bytes_downloaded": int,
            "abort_reason": str | None,  # one of: 'session_fail', 'disk_full',
                                          #         'stream_ended', 'unknown'
        }

    on_progress is an optional async callback receiving:
        {"status": "recording" | "ended" | "aborted",
         "elapsed_seconds": int,
         "bytes": int,
         "detail": str}
    Called at most every LIVE_HEARTBEAT_SECONDS.
    """
    allowed, platform = _platform_allowed(url)
    if not allowed:
        return {
            "ok": False,
            "files": [],
            "duration_seconds": 0,
            "bytes_downloaded": 0,
            "abort_reason": "platform_not_allowed",
            "detail": f"Live recording disabled for {platform}. Whitelist: {sorted(LIVE_PLATFORMS)}.",
        }

    free_gb = _free_disk_gb(DOWNLOADS_DIR)
    if 0 < free_gb < LIVE_MIN_FREE_DISK_GB:
        return {
            "ok": False,
            "files": [],
            "duration_seconds": 0,
            "bytes_downloaded": 0,
            "abort_reason": "disk_low",
            "detail": f"Only {free_gb:.1f} GB free; need ≥ {LIVE_MIN_FREE_DISK_GB} GB.",
        }

    Path(LIVE_DIR).mkdir(parents=True, exist_ok=True)

    # Capture the *running* event loop on the calling (asyncio) thread BEFORE
    # we hand off to run_in_executor. The hook runs in the executor's worker
    # thread where asyncio.get_event_loop() returns a fresh, non-running loop
    # on Python 3.12 — calling run_coroutine_threadsafe on that is a silent
    # no-op. Capturing here and closing over it fixes that.
    main_loop = asyncio.get_running_loop()

    state = {
        "started_at": time.time(),
        "last_heartbeat": 0.0,
        "bytes": 0,
        "filepath": None,
        "abort_reason": None,
        "abort_detail": "",
    }

    def _maybe_emit(now: float):
        if on_progress is None:
            return
        if now - state["last_heartbeat"] < LIVE_HEARTBEAT_SECONDS:
            return
        state["last_heartbeat"] = now
        elapsed = int(now - state["started_at"])
        try:
            asyncio.run_coroutine_threadsafe(
                on_progress({
                    "status":          "recording",
                    "elapsed_seconds": elapsed,
                    "bytes":           state["bytes"],
                    "detail":          "live",
                }),
                main_loop,
            )
        except RuntimeError:
            # Loop not running — fire-and-forget.
            pass

    def hook(d):
        # User-requested stop wins over everything else.
        if stop_flag is not None and stop_flag.get("stop"):
            state["abort_reason"] = "user_stopped"
            raise LiveAbort("user_stopped", "stop requested by user")
        # Throttled heartbeat. yt-dlp calls this many times per second on
        # active live streams; we only emit every LIVE_HEARTBEAT_SECONDS.
        now = time.time()
        if d.get("status") == "downloading":
            state["bytes"] = d.get("downloaded_bytes") or state["bytes"]
            _maybe_emit(now)
        elif d.get("status") == "finished":
            state["filepath"] = d.get("filename")
        elif d.get("status") == "error":
            err = str(d.get("info_dict", {}).get("error") or d)
            if LIVE_ABORT_ON_SESSION_FAIL and _is_auth_failure(err):
                state["abort_reason"] = "session_fail"
                state["abort_detail"] = err[:300]
                raise LiveAbort("session_fail", err[:300])

    # Bridge yt-dlp's own logger into ours so we can SEE what it's doing.
    # With quiet: True (the previous default) yt-dlp errors got swallowed,
    # leaving 0-byte .part files with no diagnostic trail.
    class _YtdlpLogger:
        def debug(self, msg):
            if msg.startswith("[debug]"):
                logger.debug("yt-dlp: %s", msg)
            else:
                logger.info("yt-dlp: %s", msg)
        def info(self, msg):    logger.info("yt-dlp: %s", msg)
        def warning(self, msg): logger.warning("yt-dlp: %s", msg)
        def error(self, msg):   logger.error("yt-dlp: %s", msg)

    # Build format selector from live_max_height. 0 = no cap.
    if LIVE_MAX_HEIGHT > 0:
        format_selector = f"bestvideo[height<={LIVE_MAX_HEIGHT}]+bestaudio/best[height<={LIVE_MAX_HEIGHT}]/best"
    else:
        format_selector = "bestvideo+bestaudio/best"

    ydl_opts = {
        "format":               format_selector,
        "outtmpl":              f"{LIVE_DIR}/%(extractor)s/%(uploader,uploader_id)s/%(title).80s.%(timestamp)s.%(ext)s",
        "merge_output_format":  "mp4",
        "logger":               _YtdlpLogger(),
        "quiet":                False,
        "no_warnings":          False,
        "progress_hooks":       [hook],
        "wait_for_video":       (1, 30),  # if 'is_upcoming', poll up to 30s — anything longer, give up
        # CRITICAL: zero retries. Auth/session failures should NOT loop.
        "retries":              0,
        "fragment_retries":     0,
        "extractor_retries":    0,
        "file_access_retries":  0,
        "skip_unavailable_fragments": False,
        "abort_on_unavailable_fragment": True,
        # Force native Python HLS downloader instead of ffmpeg subprocess.
        # Reason: when the user calls /stop_livestream, the progress hook
        # raises LiveAbort INSIDE Python — but if yt-dlp had delegated to
        # ffmpeg, ffmpeg keeps writing fragments forever as an orphan child.
        # Native downloader runs in-process so the exception interrupts it.
        "hls_prefer_native":    True,
        "external_downloader":  {"m3u8": "native", "default": "native"},
    }
    # `live_from_start` is YouTube-only. Twitch / Kick raise
    # "no formats that can be downloaded from the start" if it's set —
    # those platforms only support recording from "now" forward. Only
    # enable for YouTube, where it materially improves "joined late"
    # recordings.
    if platform == "youtube":
        ydl_opts["live_from_start"] = True
    if cookiepath:
        ydl_opts["cookiefile"] = cookiepath

    async with _get_live_semaphore():
        loop = asyncio.get_running_loop()

        def _run():
            try:
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    ydl.download([url])
            except LiveAbort:
                raise
            except yt_dlp.utils.DownloadError as e:
                msg = str(e)
                if _is_auth_failure(msg):
                    raise LiveAbort("session_fail", msg[:300])
                # Stream-ended is the canonical "this is fine" terminal state
                if any(k in msg.lower() for k in ("ended", "no longer live", "stream is offline")):
                    return  # natural end
                raise LiveAbort("download_error", msg[:300])
            except Exception as e:
                raise LiveAbort("unknown", str(e)[:300])

        try:
            await loop.run_in_executor(None, _run)
            abort_reason = state.get("abort_reason") or "stream_ended"
        except LiveAbort as e:
            abort_reason = e.reason
            state["abort_detail"] = e.detail
        except Exception as e:
            logger.exception("Unexpected live recording failure")
            abort_reason = "unknown"
            state["abort_detail"] = str(e)[:300]
        finally:
            # Defensive: kill any ffmpeg/ffprobe children. With native HLS
            # downloader this should be a no-op, but if a code path slips
            # through to ffmpeg we don't leak orphan recorders.
            killed = _kill_orphan_ffmpeg_children()
            if killed:
                logger.info("orphan cleanup terminated %d ffmpeg child(ren)", killed)

    elapsed = int(time.time() - state["started_at"])
    files = []
    if state["filepath"] and Path(state["filepath"]).exists():
        files = [state["filepath"]]
    else:
        # Recording may have been written to a partial file under LIVE_DIR
        try:
            for f in Path(LIVE_DIR).rglob("*.mp4"):
                if f.stat().st_mtime >= state["started_at"]:
                    files.append(str(f))
        except Exception:
            pass

    bytes_total = state["bytes"]
    if not bytes_total and files:
        try:
            bytes_total = sum(Path(f).stat().st_size for f in files)
        except Exception:
            pass

    return {
        "ok":               abort_reason == "stream_ended",
        "files":            files,
        "duration_seconds": elapsed,
        "bytes_downloaded": bytes_total,
        "abort_reason":     abort_reason,
        "detail":           state["abort_detail"],
        "platform":         platform,
    }
