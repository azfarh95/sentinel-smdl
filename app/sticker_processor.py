"""Sticker maker — ffmpeg pipeline that turns user video/GIF into a Telegram
video-sticker WEBM (VP9, 512×512, ≤256 KB, ≤3s, ≤30fps).

Telegram's sticker constraints are unusually strict:
  • Container: WEBM, codec: VP9, no audio
  • Exact 512×512 (transparent pad / aspect-preserving scale)
  • ≤3 seconds, ≤30 fps
  • ≤256 KB on disk

The hard part is the size ceiling — a busy 3-second clip at 250 kbps can
overshoot. We do a bitrate-fallback loop (250k → 80k) and return the first
attempt that fits.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
from pathlib import Path

logger = logging.getLogger(__name__)

# Telegram limits
STICKER_MAX_BYTES   = 256 * 1024
STICKER_MAX_DUR_S   = 3.0
STICKER_FRAME_PX    = 512
STICKER_MAX_FPS     = 30

# Try these bitrates in order. VP9 + transparent padding is heavy; if a 3-second
# busy clip can't fit at 80k, we surface the failure to the user (they should
# trim shorter or pick a less busy section).
_BITRATE_LADDER = ("250k", "180k", "120k", "80k")


def _run(cmd: list[str], timeout: int = 60) -> tuple[int, bytes]:
    """Sync subprocess helper — captures stderr for error reporting."""
    import subprocess
    p = subprocess.run(cmd, capture_output=True, timeout=timeout)
    return p.returncode, p.stderr


async def probe(src: Path) -> dict:
    """Return basic metadata about the source: duration_s, width, height.
    Returns {} on failure — caller treats as 'unknown' and moves on."""
    if not src.exists():
        return {}
    if not shutil.which("ffprobe"):
        return {}
    cmd = [
        "ffprobe", "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=width,height,duration,avg_frame_rate",
        "-show_entries", "format=duration",
        "-of", "json",
        str(src),
    ]
    try:
        loop = asyncio.get_running_loop()
        rc, stderr = await loop.run_in_executor(None, lambda: _run(cmd, 20))
        if rc != 0:
            logger.warning("ffprobe non-zero on %s: %s", src, stderr[:200])
            return {}
        import subprocess
        # We need stdout — re-run capturing it. _run returned stderr only.
        p = subprocess.run(cmd, capture_output=True, timeout=20)
        data = json.loads(p.stdout.decode() or "{}")
    except Exception as e:
        logger.warning("ffprobe parse failed on %s: %s", src, e)
        return {}
    out: dict = {}
    streams = data.get("streams") or []
    if streams:
        s0 = streams[0]
        if s0.get("width"):  out["width"]  = int(s0["width"])
        if s0.get("height"): out["height"] = int(s0["height"])
        if s0.get("duration"):
            try: out["duration_s"] = float(s0["duration"])
            except Exception: pass
    if "duration_s" not in out:
        fmt = data.get("format") or {}
        if fmt.get("duration"):
            try: out["duration_s"] = float(fmt["duration"])
            except Exception: pass
    return out


def _build_filter(crop: tuple[int, int, int, int] | None) -> str:
    """Build the -vf filter chain. crop=(cx, cy, cw, ch) in source-pixel
    coordinates; None means center-crop-to-fill the whole frame into 512×512.

    Center-crop-to-fill is the right default for short-clip stickers — it
    matches what most TG sticker packs look like (content fills the square,
    no black bars). The trade-off is that the long-axis edges of the source
    get cropped. Users who care about preserving the full frame can trim
    their clip in a real editor first."""
    parts: list[str] = []
    if crop:
        cx, cy, cw, ch = crop
        cw = max(8, int(cw)); ch = max(8, int(ch))
        cx = max(0, int(cx)); cy = max(0, int(cy))
        parts.append(f"crop={cw}:{ch}:{cx}:{cy}")
    # Scale so the SHORTER side is 512 (the longer side becomes >512), then
    # center-crop the longer side down to 512. Result: 512×512 with content
    # filling the entire frame, no padding.
    parts.append(
        f"scale={STICKER_FRAME_PX}:{STICKER_FRAME_PX}:force_original_aspect_ratio=increase"
    )
    parts.append(f"crop={STICKER_FRAME_PX}:{STICKER_FRAME_PX}")
    # Cap fps
    parts.append(f"fps={STICKER_MAX_FPS}")
    return ",".join(parts)


async def make_video_sticker(
    src: Path,
    dst: Path,
    *,
    start: float = 0.0,
    end: float | None = None,
    crop: tuple[int, int, int, int] | None = None,
) -> tuple[bool, str | None]:
    """Encode `src` into a Telegram video sticker at `dst`.

    Args:
        src:   source video/GIF on disk
        dst:   target webm path (overwritten)
        start: trim start (seconds)
        end:   trim end (seconds); if None, encode min(STICKER_MAX_DUR_S, full)
        crop:  (cx, cy, cw, ch) in source-pixel coords; None = no crop

    Returns (ok, error_message). On success dst is a valid Telegram video
    sticker (≤256 KB, VP9, 512×512, ≤3s).
    """
    if not src.exists():
        return False, f"source not found: {src}"
    if not shutil.which("ffmpeg"):
        return False, "ffmpeg not installed in container"

    duration = STICKER_MAX_DUR_S
    if end is not None and end > start:
        duration = min(end - start, STICKER_MAX_DUR_S)
    if duration <= 0:
        return False, "trim window has zero duration"

    vf = _build_filter(crop)
    dst.parent.mkdir(parents=True, exist_ok=True)

    last_size = 0
    last_err = ""
    for bitrate in _BITRATE_LADDER:
        cmd = [
            "ffmpeg", "-y",
            "-ss", f"{start:.3f}",
            "-i", str(src),
            "-t",  f"{duration:.3f}",
            "-vf", vf,
            "-c:v", "libvpx-vp9",
            "-b:v", bitrate,
            "-crf", "30",
            "-an",                 # no audio (Telegram strips it anyway)
            # Center-crop-to-fill means no padding → no alpha channel needed.
            # Standard yuv420p keeps the encoder happy and the file small.
            "-pix_fmt", "yuv420p",
            "-deadline", "good",
            "-cpu-used", "2",
            "-row-mt", "1",
            str(dst),
        ]
        loop = asyncio.get_running_loop()
        try:
            rc, stderr = await loop.run_in_executor(None, lambda: _run(cmd, 90))
        except Exception as e:
            return False, f"ffmpeg crashed: {e!r}"

        if rc != 0:
            last_err = stderr.decode("utf-8", errors="replace")[-400:]
            logger.warning("ffmpeg rc=%s at b=%s on %s: %s", rc, bitrate, src.name, last_err[:200])
            # Encoding failure isn't fixable by lowering bitrate — bail.
            return False, f"ffmpeg failed: {last_err[-200:]}"

        try:
            last_size = dst.stat().st_size
        except FileNotFoundError:
            return False, "ffmpeg ran but produced no output file"

        if last_size <= STICKER_MAX_BYTES:
            logger.info("sticker encode ok: %s @ %s → %d bytes",
                        src.name, bitrate, last_size)
            return True, None

        logger.info("sticker too big at %s: %d > %d, dropping bitrate",
                    bitrate, last_size, STICKER_MAX_BYTES)

    return False, (
        f"Output >256KB even at lowest bitrate "
        f"(last size: {last_size} bytes). "
        "Try a shorter clip or a simpler scene."
    )
