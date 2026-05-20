"""Repair interrupted live recordings — remux *.mp4.part into clean *.mp4.

When the SMDL container is restarted mid-recording, yt-dlp leaves behind
`<stream>.mp4.part` files. These contain valid stream data but the MP4
container's `moov` atom may be missing or pointing at incomplete frames,
making players (Telegram, VLC, browser preview) refuse to open them.

`ffmpeg -i broken.mp4.part -c copy -movflags faststart fixed.mp4`
re-muxes the existing audio/video streams into a fresh container with the
moov atom written upfront. No re-encoding — fast (I/O bound, ~30-60s per GB)
and lossless.

CLI:
    # dry-run report (default)
    docker exec smdl python -m app.repair_live_parts

    # apply: write the repaired .mp4 alongside, move source to
    # `_repaired_originals/` once we've confirmed output is valid
    docker exec smdl python -m app.repair_live_parts --apply

    # pick a different folder (default is /downloads/live/Chaturbate/NA)
    docker exec smdl python -m app.repair_live_parts --dir /downloads/live/Chaturbate/Asia
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


DEFAULT_DIR = Path("/downloads/live/Chaturbate/NA")
ARCHIVE_SUBDIR = "_repaired_originals"


@dataclass
class RepairResult:
    src: Path
    dst: Optional[Path]
    src_size: int
    dst_size: int
    duration_sec: Optional[float]
    ffmpeg_rc: int
    err: str = ""

    @property
    def ok(self) -> bool:
        return self.ffmpeg_rc == 0 and self.dst is not None and self.dst.exists() and self.dst_size > 0


def _probe_duration(path: Path) -> Optional[float]:
    """Return media duration in seconds via ffprobe, or None on failure."""
    try:
        proc = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "json", str(path)],
            capture_output=True, text=True, timeout=30,
        )
        if proc.returncode != 0:
            return None
        data = json.loads(proc.stdout or "{}")
        return float(data.get("format", {}).get("duration") or 0) or None
    except Exception:
        return None


def _human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def _human_duration(s: Optional[float]) -> str:
    if not s:
        return "—"
    s = int(s)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h:d}h{m:02d}m{sec:02d}s" if h else f"{m:d}m{sec:02d}s"


def _collision_safe(dst: Path) -> Path:
    """If dst exists, suffix with '.repaired (1).mp4' etc."""
    if not dst.exists():
        return dst
    stem = dst.stem
    suffix = dst.suffix
    i = 1
    while True:
        cand = dst.parent / f"{stem} ({i}){suffix}"
        if not cand.exists():
            return cand
        i += 1


def repair_one(src: Path, apply: bool) -> RepairResult:
    """Remux a single .mp4.part → .mp4 via ffmpeg stream-copy.
    On dry-run, ffmpeg is NOT called; only metadata is gathered."""
    src_size = src.stat().st_size
    dst_name = src.name[:-len(".part")] if src.name.endswith(".part") else src.name + ".repaired.mp4"
    dst = _collision_safe(src.parent / dst_name)

    if not apply:
        return RepairResult(src=src, dst=dst, src_size=src_size,
                            dst_size=0, duration_sec=None, ffmpeg_rc=-1,
                            err="dry-run (not executed)")

    # ffmpeg incantation:
    #   -err_detect ignore_err    keep going past frame-level corruption
    #   -i <src>                  input
    #   -c copy                   stream-copy (no re-encode)
    #   -movflags +faststart      moov atom written to front of file
    #   -y                        overwrite output if it exists (collision_safe
    #                             already picked a fresh name, but defensive)
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "warning",
        "-err_detect", "ignore_err",
        "-i", str(src),
        "-c", "copy", "-movflags", "+faststart",
        "-y", str(dst),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
    except subprocess.TimeoutExpired:
        return RepairResult(src=src, dst=dst, src_size=src_size,
                            dst_size=0, duration_sec=None, ffmpeg_rc=-1,
                            err="ffmpeg timeout (>1h)")
    except Exception as e:
        return RepairResult(src=src, dst=dst, src_size=src_size,
                            dst_size=0, duration_sec=None, ffmpeg_rc=-1,
                            err=f"ffmpeg crash: {e!s:.200}")

    dst_size = dst.stat().st_size if dst.exists() else 0
    duration = _probe_duration(dst) if dst_size > 0 else None
    err = ""
    if proc.returncode != 0:
        # tail of stderr for actionable error context
        err = (proc.stderr or "")[-300:].strip()
    elif dst_size < 1024 * 100:
        # ffmpeg returned 0 but the output is tiny — treat as failure
        err = "output suspiciously small (<100 KB)"

    return RepairResult(src=src, dst=dst, src_size=src_size,
                        dst_size=dst_size, duration_sec=duration,
                        ffmpeg_rc=proc.returncode, err=err)


def archive_source(src: Path, archive_dir: Path) -> Path:
    """Move the original .mp4.part to `_repaired_originals/`."""
    archive_dir.mkdir(parents=True, exist_ok=True)
    dst = _collision_safe(archive_dir / src.name)
    shutil.move(str(src), str(dst))
    return dst


# ── Public API (called by the Admin Mini App + bot) ────────────────────────


def scan_pending(root: Path | str = DEFAULT_DIR) -> dict:
    """Return {count, total_bytes, paths} for *.mp4.part files under `root`.
    Cheap — no ffmpeg invocation. Used by the Admin tab badge."""
    p = Path(root)
    if not p.exists():
        return {"count": 0, "total_bytes": 0, "paths": []}
    parts = sorted(p.glob("*.mp4.part"))
    total = sum(f.stat().st_size for f in parts)
    return {
        "count":       len(parts),
        "total_bytes": total,
        "paths":       [str(f) for f in parts],
    }


def repair_all(root: Path | str = DEFAULT_DIR,
                keep_source: bool = False,
                logger=None) -> dict:
    """Walk `root`, repair every .mp4.part, optionally archive originals.
    Returns {repaired, failed, total_in_bytes, total_out_bytes, items}.

    Designed to be called from the Mini App's "Repair recordings" button as
    a background task. Each file logs its outcome via `logger` (or prints).
    """
    p = Path(root)
    log = logger.info if logger else print
    if not p.exists():
        log(f"repair_all: dir not found: {p}")
        return {"repaired": 0, "failed": 0, "total_in_bytes": 0,
                "total_out_bytes": 0, "items": []}
    parts = sorted(p.glob("*.mp4.part"))
    items: list[dict] = []
    repaired = failed = total_in = total_out = 0
    archive_dir = p / ARCHIVE_SUBDIR
    for f in parts:
        log(f"repair_all: starting {f.name} ({_human_size(f.stat().st_size)})")
        r = repair_one(f, apply=True)
        total_in  += r.src_size
        total_out += r.dst_size
        item = {
            "src":          str(f),
            "dst":          str(r.dst) if r.dst else None,
            "src_size":     r.src_size,
            "dst_size":     r.dst_size,
            "duration_sec": r.duration_sec,
            "ok":           r.ok,
            "err":          r.err,
        }
        if r.ok:
            repaired += 1
            if not keep_source:
                try:
                    archive_source(f, archive_dir)
                    log(f"repair_all: archived {f.name}")
                except Exception as e:
                    item["archive_err"] = f"{e!s:.200}"
                    log(f"repair_all: archive failed for {f.name}: {e}")
        else:
            failed += 1
            log(f"repair_all: FAIL {f.name} rc={r.ffmpeg_rc} err={r.err[:100]}")
        items.append(item)
    log(f"repair_all: done. repaired={repaired} failed={failed} "
        f"in={_human_size(total_in)} out={_human_size(total_out)}")
    return {
        "repaired":        repaired,
        "failed":          failed,
        "total_in_bytes":  total_in,
        "total_out_bytes": total_out,
        "items":           items,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default=str(DEFAULT_DIR),
                    help=f"Folder to scan (default: {DEFAULT_DIR})")
    ap.add_argument("--apply", action="store_true",
                    help="Actually run ffmpeg + archive source. Default: dry-run report.")
    ap.add_argument("--keep-source", action="store_true",
                    help="With --apply, do NOT move .mp4.part to _repaired_originals/")
    args = ap.parse_args()

    root = Path(args.dir)
    if not root.exists():
        print(f"ERROR: {root} not found", file=sys.stderr)
        return 1

    parts = sorted(root.glob("*.mp4.part"))
    if not parts:
        print(f"No .mp4.part files in {root}")
        return 0

    print(f"{'='*88}")
    print(f"  Live-recording repair · {root}")
    print(f"  Mode: {'APPLY' if args.apply else 'DRY-RUN'}    Files: {len(parts)}")
    print(f"{'='*88}\n")
    print(f"  {'#':>2}  {'Source':<60} {'Size':>8} {'→':<3} {'Repaired size':>13} {'Duration':>11}  Status")
    print(f"  {'-'*2}  {'-'*60} {'-'*8} {'-'*3} {'-'*13} {'-'*11}  ------")

    results: list[RepairResult] = []
    total_in = 0
    total_out = 0
    archive_dir = root / ARCHIVE_SUBDIR

    for i, p in enumerate(parts, 1):
        r = repair_one(p, args.apply)
        results.append(r)
        total_in += r.src_size
        total_out += r.dst_size

        status = "OK"
        if not args.apply:
            status = "would repair"
        elif not r.ok:
            status = f"FAIL: {r.err[:60]}"

        print(f"  {i:>2}  {p.name[:58]:<60} {_human_size(r.src_size):>8}  → "
              f"{_human_size(r.dst_size):>13} {_human_duration(r.duration_sec):>11}  {status}")

        if args.apply and r.ok and not args.keep_source:
            try:
                archived = archive_source(p, archive_dir)
                print(f"        ↳ archived: {archived.relative_to(root)}")
            except Exception as e:
                print(f"        ↳ archive failed: {e!s:.80}")

    print()
    print(f"  Totals: {_human_size(total_in)} in  →  {_human_size(total_out)} out")
    n_ok = sum(1 for r in results if r.ok) if args.apply else 0
    n_fail = sum(1 for r in results if not r.ok) if args.apply else 0
    if args.apply:
        print(f"  Repaired: {n_ok}    Failed: {n_fail}    Archive: {archive_dir}")
    else:
        print(f"\n  DRY-RUN — re-run with --apply to remux + archive.")
    return 0 if (not args.apply or n_fail == 0) else 2


if __name__ == "__main__":
    sys.exit(main())
