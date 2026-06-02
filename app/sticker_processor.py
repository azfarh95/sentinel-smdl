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
# Static stickers — single-frame WEBP, same 512×512 box, 512 KB ceiling.
STATIC_MAX_BYTES    = 512 * 1024
# Custom-emoji stickers — TG requires exactly 100×100. Otherwise the same
# format constraints apply as regular video/static, just at smaller scale.
EMOJI_FRAME_PX      = 100
EMOJI_MAX_BYTES_VIDEO  = 64 * 1024
EMOJI_MAX_BYTES_STATIC = 64 * 1024

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


def _build_filter(crop: tuple[int, int, int, int] | None, circle: bool = False) -> str:
    """Build the -vf filter chain. crop=(cx, cy, cw, ch) in source-pixel
    coordinates; None means center-crop-to-fill the whole frame into 512×512.

    Center-crop-to-fill is the right default for short-clip stickers — it
    matches what most TG sticker packs look like (content fills the square,
    no black bars). The trade-off is that the long-axis edges of the source
    get cropped. Users who care about preserving the full frame can trim
    their clip in a real editor first.

    circle=True ZOOMS IN on a round video-note to drop its (white/opaque)
    corners. Telegram round video-notes are a circle inscribed in an opaque
    square; we can't make WebM video transparent (libvpx strips alpha), so
    instead we crop the largest centred square that fits INSIDE the inscribed
    circle and scale it back up. The visible square then lies entirely within
    the circle's content — the corners fall outside frame, so no white
    survives. The trade-off is a ~1.45× zoom (the circle's outer rim is lost)."""
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
    if circle:
        # Inscribed square side = 512/√2 ≈ 362. Crop a touch tighter (a ~10px
        # margin absorbs the anti-aliased rim + slightly-smaller circles) so the
        # corners sit safely inside the content, then scale back to fill 512².
        inner = round(STICKER_FRAME_PX / 2 ** 0.5) - 10   # 352
        parts.append(f"crop={inner}:{inner}")
        parts.append(f"scale={STICKER_FRAME_PX}:{STICKER_FRAME_PX}")
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
    circle: bool = False,
) -> tuple[bool, str | None]:
    """Encode `src` into a Telegram video sticker at `dst`.

    Args:
        src:   source video/GIF on disk
        dst:   target webm path (overwritten)
        start: trim start (seconds)
        end:   trim end (seconds); if None, encode min(STICKER_MAX_DUR_S, full)
        crop:  (cx, cy, cw, ch) in source-pixel coords; None = no crop
        circle: zoom-crop a round video-note so its opaque corners fall outside
                the frame (see _build_filter). Stays opaque yuv420p.

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

    vf = _build_filter(crop, circle=circle)
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


# Built-in shape presets. Each is a function (size) -> list of polygon points,
# or the sentinel "ellipse". Custom shapes come in as caller-supplied points.
def _preset_points(shape: str, s: int) -> list[tuple[float, float]] | str | None:
    if shape in ("circle", "ellipse"):
        return "ellipse"
    if shape == "triangle":          # apex top, base along the bottom
        return [(s / 2, 0), (0, s), (s, s)]
    if shape == "diamond":
        return [(s / 2, 0), (s, s / 2), (s / 2, s), (0, s / 2)]
    if shape == "heart":
        # coarse heart polygon (good enough at 512², antialiased on downscale)
        pts = [(0.50, 0.95), (0.06, 0.52), (0.06, 0.30), (0.22, 0.16),
               (0.40, 0.18), (0.50, 0.30), (0.60, 0.18), (0.78, 0.16),
               (0.94, 0.30), (0.94, 0.52)]
        return [(x * s, y * s) for x, y in pts]
    if shape == "star":
        import math
        cx = cy = s / 2
        out_r, in_r = s * 0.5, s * 0.21
        pts = []
        for i in range(10):
            r = out_r if i % 2 == 0 else in_r
            ang = -math.pi / 2 + i * math.pi / 5
            pts.append((cx + r * math.cos(ang), cy + r * math.sin(ang)))
        return pts
    return None


def build_shape_mask(shape: str, out_path: Path, *, size_px: int = STICKER_FRAME_PX,
                     points: list[tuple[float, float]] | None = None) -> Path | None:
    """Render an alpha mask (white = keep, black = transparent) for a shape.

    shape: 'circle' / 'triangle' / 'diamond' / 'heart' / 'star', or 'custom'
    (then `points` is a normalised [(x,y),…] polygon in 0..1). Drawn at 4×
    and downscaled for antialiasing. Returns the path, or None if no shape."""
    try:
        from PIL import Image, ImageDraw
    except Exception as e:
        logger.warning("PIL unavailable for shape mask: %s", e)
        return None
    ss = size_px * 4
    img = Image.new("L", (ss, ss), 0)
    d = ImageDraw.Draw(img)
    if shape == "custom":
        if not points or len(points) < 3:
            return None
        d.polygon([(x * ss, y * ss) for x, y in points], fill=255)
    else:
        spec = _preset_points(shape, ss)
        if spec is None:
            return None
        if spec == "ellipse":
            d.ellipse([0, 0, ss - 1, ss - 1], fill=255)
        else:
            d.polygon(spec, fill=255)
    img = img.resize((size_px, size_px), Image.LANCZOS)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(str(out_path))
    return out_path


# Background removal (subject isolation) for one-tap cutout stickers. Uses
# rembg (onnxruntime + the small u2netp model). CPU-bound → run in an executor.
# The model is baked at $U2NET_HOME so first use doesn't pay a download.
_rembg_session = None


def _rembg_session_get():
    global _rembg_session
    if _rembg_session is None:
        from rembg import new_session
        _rembg_session = new_session("u2netp")
    return _rembg_session


def _bg_remove_sync(src: Path, dst: Path) -> None:
    from rembg import remove
    from PIL import Image
    with Image.open(src) as im:
        out = remove(im.convert("RGBA"), session=_rembg_session_get())
    out.save(str(dst))


async def bg_remove(src: Path, dst: Path) -> tuple[bool, str | None]:
    """Strip the background → a transparent RGBA PNG at `dst`. Feed the result
    to make_static_sticker(keep_alpha=True) for a webp cutout sticker."""
    if not src.exists():
        return False, f"source not found: {src}"
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, _bg_remove_sync, src, dst)
    except Exception as e:
        logger.warning("bg_remove failed on %s: %s", src.name, e)
        return False, f"background removal failed: {e}"
    return (dst.exists(), None if dst.exists() else "no output produced")


def _build_static_filter(crop: tuple[int, int, int, int] | None, size_px: int) -> str:
    """Same shape as _build_filter but without the fps cap (static frames
    don't have one) and targeting a configurable square size so we can
    share this between regular static (512×512) and custom-emoji (100×100)."""
    parts: list[str] = []
    if crop:
        cx, cy, cw, ch = crop
        cw = max(8, int(cw)); ch = max(8, int(ch))
        cx = max(0, int(cx)); cy = max(0, int(cy))
        parts.append(f"crop={cw}:{ch}:{cx}:{cy}")
    parts.append(
        f"scale={size_px}:{size_px}:force_original_aspect_ratio=increase"
    )
    parts.append(f"crop={size_px}:{size_px}")
    return ",".join(parts)


async def make_static_sticker(
    src: Path,
    dst: Path,
    *,
    crop: tuple[int, int, int, int] | None = None,
    size_px: int = STICKER_FRAME_PX,
    max_bytes: int = STATIC_MAX_BYTES,
    seek_s: float = 0.0,
    mask: Path | None = None,
    keep_alpha: bool = False,
) -> tuple[bool, str | None]:
    """Encode a single frame from `src` into a Telegram static sticker WEBP.

    Static stickers can be sourced from images (PNG/JPG/GIF first frame) or
    pulled out of a video at `seek_s`. WEBP quality is dropped on a ladder
    until the result fits under `max_bytes` (the Telegram 512 KB ceiling for
    regular static, 64 KB for custom emoji).

    mask: optional size_px² grayscale alpha mask (white=keep, black=clear) —
    used for shaped/cutout stickers. WEBP carries alpha, so the masked-out
    area becomes genuinely transparent (see build_shape_mask).

    keep_alpha: preserve the SOURCE's own alpha (e.g. a transparent PNG from
    bg_remove or a cut-out source) — encodes yuva420p instead of flattening."""
    if not src.exists():
        return False, f"source not found: {src}"
    if not shutil.which("ffmpeg"):
        return False, "ffmpeg not installed in container"
    vf = _build_static_filter(crop, size_px)
    dst.parent.mkdir(parents=True, exist_ok=True)
    last_size = 0
    last_err = ""
    for quality in (90, 75, 60, 45, 30):
        if mask is not None:
            # Scale/crop the frame, then merge the mask in as the alpha channel.
            cmd = [
                "ffmpeg", "-y",
                "-ss", f"{seek_s:.3f}", "-i", str(src),
                "-i", str(mask),
                "-frames:v", "1",
                "-filter_complex", f"[0:v]{vf}[fg];[fg][1:v]alphamerge",
                "-c:v", "libwebp",
                "-quality", str(quality),
                "-pix_fmt", "yuva420p",
                "-an",
                str(dst),
            ]
        else:
            cmd = [
                "ffmpeg", "-y",
                "-ss", f"{seek_s:.3f}",
                "-i", str(src),
                "-frames:v", "1",
                "-vf", vf,
                "-c:v", "libwebp",
                "-quality", str(quality),
                "-pix_fmt", "yuva420p" if keep_alpha else "yuv420p",
                "-an",
                str(dst),
            ]
        loop = asyncio.get_running_loop()
        try:
            rc, stderr = await loop.run_in_executor(None, lambda: _run(cmd, 30))
        except Exception as e:
            return False, f"ffmpeg crashed: {e!r}"
        if rc != 0:
            last_err = stderr.decode("utf-8", errors="replace")[-400:]
            logger.warning("ffmpeg(static) rc=%s q=%s on %s: %s",
                           rc, quality, src.name, last_err[:200])
            return False, f"ffmpeg failed: {last_err[-200:]}"
        try:
            last_size = dst.stat().st_size
        except FileNotFoundError:
            return False, "ffmpeg ran but produced no output"
        if last_size <= max_bytes:
            logger.info("static sticker ok: %s q=%s → %d bytes",
                        src.name, quality, last_size)
            return True, None
        logger.info("static too big at q=%s: %d > %d", quality, last_size, max_bytes)
    return False, (
        f"Output > {max_bytes} bytes even at quality 30 "
        f"(last size: {last_size}). Pick a simpler frame."
    )


async def make_custom_emoji_sticker(
    src: Path,
    dst: Path,
    *,
    is_video: bool,
    crop: tuple[int, int, int, int] | None = None,
    start: float = 0.0,
    end: float | None = None,
) -> tuple[bool, str | None]:
    """100×100 variant for custom-emoji packs. Routes to either the video or
    static encoder with the smaller frame box + smaller byte ceiling."""
    if is_video:
        # Reuse the video path but target 100×100. The existing
        # make_video_sticker hard-codes STICKER_FRAME_PX; we inline a
        # smaller variant rather than expose another knob.
        if not src.exists():
            return False, f"source not found: {src}"
        if not shutil.which("ffmpeg"):
            return False, "ffmpeg not installed in container"
        duration = STICKER_MAX_DUR_S
        if end is not None and end > start:
            duration = min(end - start, STICKER_MAX_DUR_S)
        if duration <= 0:
            return False, "trim window has zero duration"
        # Crop+scale chain manually because the helper above bakes in
        # STICKER_FRAME_PX. Same shape, smaller target.
        vf_parts: list[str] = []
        if crop:
            cx, cy, cw, ch = crop
            vf_parts.append(f"crop={max(8,int(cw))}:{max(8,int(ch))}:{max(0,int(cx))}:{max(0,int(cy))}")
        vf_parts.append(f"scale={EMOJI_FRAME_PX}:{EMOJI_FRAME_PX}:force_original_aspect_ratio=increase")
        vf_parts.append(f"crop={EMOJI_FRAME_PX}:{EMOJI_FRAME_PX}")
        vf_parts.append(f"fps={STICKER_MAX_FPS}")
        vf = ",".join(vf_parts)
        dst.parent.mkdir(parents=True, exist_ok=True)
        last_size = 0; last_err = ""
        for bitrate in ("80k", "60k", "40k", "30k"):
            cmd = [
                "ffmpeg", "-y", "-ss", f"{start:.3f}", "-i", str(src),
                "-t", f"{duration:.3f}", "-vf", vf,
                "-c:v", "libvpx-vp9", "-b:v", bitrate, "-crf", "32",
                "-an", "-pix_fmt", "yuv420p",
                "-deadline", "good", "-cpu-used", "2", "-row-mt", "1",
                str(dst),
            ]
            loop = asyncio.get_running_loop()
            try:
                rc, stderr = await loop.run_in_executor(None, lambda: _run(cmd, 60))
            except Exception as e:
                return False, f"ffmpeg crashed: {e!r}"
            if rc != 0:
                last_err = stderr.decode("utf-8", errors="replace")[-400:]
                return False, f"ffmpeg failed: {last_err[-200:]}"
            try: last_size = dst.stat().st_size
            except FileNotFoundError: return False, "ffmpeg ran but produced no output"
            if last_size <= EMOJI_MAX_BYTES_VIDEO:
                return True, None
        return False, (f"Custom-emoji video > {EMOJI_MAX_BYTES_VIDEO} bytes "
                       f"even at lowest bitrate (last: {last_size}).")
    # Static path: single-frame, 100×100 WEBP, 64 KB ceiling.
    return await make_static_sticker(
        src, dst, crop=crop,
        size_px=EMOJI_FRAME_PX, max_bytes=EMOJI_MAX_BYTES_STATIC,
        seek_s=start,
    )
