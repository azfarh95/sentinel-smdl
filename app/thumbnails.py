"""On-disk thumbnail cache for the Files tab (#32).

Generates 320×320-fit JPEG thumbs for images and videos. Cache key
includes mtime+size so an edited source naturally invalidates without
manual cache busting. Cache lives at ``<DOWNLOADS_DIR>/.thumbnails/``
sharded by sha-prefix so the directory stays under filesystem-friendly
fanout (~256 dirs at the top level).

Image  → Pillow → JPEG q80 (EXIF stripped, EXIF-rotation honoured)
Video  → ffmpeg -ss 1 -vframes 1 -vf scale=320 -f mjpeg
Audio + everything else → returns None; caller falls back to emoji.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp",
              ".heic", ".heif", ".avif", ".tiff", ".tif"}
VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".webm", ".m4v", ".avi",
              ".ts", ".mts", ".m2ts"}

_MAX_SIDE = 320
_JPEG_Q   = 80
_VIDEO_SEEK = "00:00:01"
_VIDEO_TIMEOUT_S = 20


def can_thumb(abs_path: Path) -> bool:
    return abs_path.suffix.lower() in IMAGE_EXTS or abs_path.suffix.lower() in VIDEO_EXTS


def cache_key(abs_path: Path) -> str:
    """sha256 over (path, mtime, size). Edits invalidate the cache."""
    st = abs_path.stat()
    raw = f"{abs_path}|{int(st.st_mtime)}|{st.st_size}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _cache_path(cache_root: Path, sha: str) -> Path:
    return cache_root / sha[:2] / f"{sha}.jpg"


def _make_image_thumb(src: Path, dest: Path) -> bool:
    try:
        from PIL import Image, ImageOps
    except ImportError:
        logger.warning("thumb: Pillow not installed; skipping image %s", src.name)
        return False
    try:
        with Image.open(src) as im:
            im = ImageOps.exif_transpose(im)
            im.thumbnail((_MAX_SIDE, _MAX_SIDE))
            if im.mode not in ("RGB", "L"):
                im = im.convert("RGB")
            dest.parent.mkdir(parents=True, exist_ok=True)
            im.save(dest, "JPEG", quality=_JPEG_Q, optimize=True)
        return True
    except Exception:
        logger.exception("thumb: image gen failed for %s", src)
        return False


def _make_video_thumb(src: Path, dest: Path) -> bool:
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        cmd = [
            "ffmpeg", "-y", "-ss", _VIDEO_SEEK, "-i", str(src),
            "-vframes", "1",
            "-vf", f"scale='min({_MAX_SIDE},iw)':-1",
            "-q:v", "5",
            str(dest),
        ]
        r = subprocess.run(cmd, capture_output=True, timeout=_VIDEO_TIMEOUT_S)
        if r.returncode == 0 and dest.exists() and dest.stat().st_size > 0:
            return True
        # First frame at 1s missed (very short clip) — retry at 0s.
        cmd[3] = "00:00:00"
        r = subprocess.run(cmd, capture_output=True, timeout=_VIDEO_TIMEOUT_S)
        return r.returncode == 0 and dest.exists() and dest.stat().st_size > 0
    except Exception:
        logger.exception("thumb: video gen failed for %s", src)
        return False


async def get_or_make_thumb(abs_path: Path, cache_root: Path) -> Path | None:
    """Return the thumbnail JPEG path for ``abs_path``, or None if the
    source isn't an image/video, doesn't exist, or generation failed.

    Generation runs in the default thread pool so the FastAPI event
    loop isn't blocked on Pillow or ffmpeg syscalls.
    """
    if not can_thumb(abs_path):
        return None
    if not abs_path.exists() or not abs_path.is_file():
        return None
    try:
        sha = cache_key(abs_path)
    except OSError:
        return None
    out = _cache_path(cache_root, sha)
    if out.exists() and out.stat().st_size > 0:
        return out
    loop = asyncio.get_running_loop()
    ext = abs_path.suffix.lower()
    if ext in IMAGE_EXTS:
        ok = await loop.run_in_executor(None, _make_image_thumb, abs_path, out)
    else:
        ok = await loop.run_in_executor(None, _make_video_thumb, abs_path, out)
    return out if ok else None
