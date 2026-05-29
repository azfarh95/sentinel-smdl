"""File-restructure migration (#41 part 2).

#34 made the download path template (`download_path_template`) user-editable,
but only *new* downloads land under the chosen layout — files already on disk
keep whatever layout was current when they arrived. This module migrates the
existing library to match the current template.

Design constraints (this touches the user's real media library on G:\\YT-DLP):

  * **Read-only by default.** `build_plan()` never moves anything; the UI's
    "Preview" button calls only this. Nothing is renamed until the user
    reviews the plan and explicitly applies it.
  * **Atomic per file.** `os.replace()` within the same volume is atomic, so a
    crash can never leave a half-moved file.
  * **Reversible.** Every applied run writes a manifest journal under
    `<downloads>/.smdl_restructure/`; `rollback()` reverses the moves.
  * **Conservative.** Files we can't confidently re-path (no DB metadata, so
    {platform}/{uploader} would resolve to "unknown") are SKIPPED unless the
    caller opts in. Existing destinations are never overwritten (marked
    "conflict" and skipped).

Token values for an existing file come from: the SMDL DB (platform, uploader,
keyed by the stored absolute path), the filename (title = stem, ext), and the
file mtime ({date} fallback). {service} is the static "YTDLP".
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

# Filesystem-illegal characters for a single path component (covers Windows,
# which is the real host — G:\\YT-DLP — even though the container is POSIX).
_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

# In-progress / sidecar artefacts that must never be migrated.
_SKIP_EXT = {".part", ".ytdl", ".tmp", ".temp", ".part-frag"}

MANIFEST_DIRNAME = ".smdl_restructure"


def sanitize_component(value: str | None, fallback: str) -> str:
    """Make `value` safe as a single path segment. Empty → `fallback`.

    >>> sanitize_component('a/b:c', 'x')
    'a_b_c'
    >>> sanitize_component('   ', 'untitled')
    'untitled'
    >>> sanitize_component('trailing. ', 'x')
    'trailing'
    """
    s = (value or "").strip()
    s = _ILLEGAL.sub("_", s)
    s = s.strip(". ")
    return s or fallback


def render_path_template(template: str, fields: dict) -> str:
    """Fill {token}s with concrete, filesystem-safe values → a relative path.

    Mirrors the token set of miniapp.compile_path_template, but produces a
    *concrete* path (for an existing file) rather than a yt-dlp outtmpl.

    >>> render_path_template('{platform}/{uploader}/{title}.{ext}',
    ...     {'platform': 'youtube', 'uploader': 'Bob', 'title': 'Clip', 'ext': 'mp4'})
    'youtube/Bob/Clip.mp4'
    >>> render_path_template('{service}/{title}.{ext}',
    ...     {'service': 'ytdlp', 'title': 'x', 'ext': '.MP4'})
    'YTDLP/x.MP4'
    """
    if not template:
        template = "{platform}/{uploader}/{title}.{ext}"
    repl = {
        "{service}":  sanitize_component((fields.get("service") or "ytdlp").upper(), "YTDLP"),
        "{platform}": sanitize_component(fields.get("platform"), "unknown"),
        "{uploader}": sanitize_component(fields.get("uploader"), "unknown"),
        "{title}":    sanitize_component(fields.get("title"), "untitled"),
        "{date}":     sanitize_component(fields.get("date"), ""),
        "{ext}":      sanitize_component((fields.get("ext") or "").lstrip("."), "bin"),
    }
    out = template
    for k, v in repl.items():
        out = out.replace(k, v)
    # Drop empty/././.. segments so a blank {date} can't leave "//" behind.
    parts = [p for p in out.replace("\\", "/").split("/") if p not in ("", ".", "..")]
    return "/".join(parts)


@dataclass
class PlanItem:
    src: str
    dst: str
    action: str             # "move" | "noop" | "conflict" | "skip"
    reason: str = ""
    matched: bool = False    # True if DB metadata resolved platform/uploader

    def as_dict(self) -> dict:
        return {"src": self.src, "dst": self.dst, "action": self.action,
                "reason": self.reason, "matched": self.matched}


def _norm(p: str) -> str:
    return os.path.normpath(p)


async def build_metadata_index(db_module) -> dict[str, dict]:
    """abspath → {platform, uploader} from url_cache + download_history.

    download_history is the richer source (every delivered file); url_cache
    fills gaps. Both store `files` as a JSON list of absolute paths.
    """
    import aiosqlite
    index: dict[str, dict] = {}

    async def _ingest(rows):
        for files_json, platform, uploader in rows:
            try:
                files = json.loads(files_json or "[]")
            except Exception:
                continue
            for f in files:
                if not f:
                    continue
                index.setdefault(_norm(f), {"platform": platform, "uploader": uploader})

    async with aiosqlite.connect(db_module.DB_PATH) as conn:
        for table in ("download_history", "url_cache"):
            try:
                cur = await conn.execute(
                    f"SELECT files, platform, uploader FROM {table}")
                await _ingest(await cur.fetchall())
            except Exception:
                # Table may not exist on a fresh DB — skip silently.
                continue
    return index


def _fields_for(abspath: str, meta: dict | None) -> tuple[dict, bool]:
    name = os.path.basename(abspath)
    stem, ext = os.path.splitext(name)
    matched = bool(meta and (meta.get("platform") or meta.get("uploader")))
    fields = {
        "service": "ytdlp",
        "platform": (meta or {}).get("platform"),
        "uploader": (meta or {}).get("uploader"),
        "title": stem,
        "ext": ext,
    }
    try:
        fields["date"] = time.strftime("%Y%m%d", time.localtime(os.path.getmtime(abspath)))
    except OSError:
        fields["date"] = ""
    return fields, matched


def build_plan(template: str, downloads_dir: str, meta_index: dict[str, dict],
               include_unmatched: bool = False) -> list[PlanItem]:
    """Walk `downloads_dir` and compute the move plan. Pure read-only.

    Skips: the temp/ scratch dir, the manifest dir, dotfiles, and partial
    download sidecars. Files with no DB metadata are SKIPPED unless
    `include_unmatched` (they'd otherwise land under unknown/unknown).
    """
    downloads_dir = _norm(downloads_dir)
    temp_dir = _norm(os.path.join(downloads_dir, "temp"))
    plan: list[PlanItem] = []

    for root, dirs, files in os.walk(downloads_dir):
        # Prune temp/, the manifest journal dir, and any hidden dir in place.
        dirs[:] = [d for d in dirs
                   if not d.startswith(".")
                   and _norm(os.path.join(root, d)) != temp_dir]
        if _norm(root) == temp_dir:
            continue
        for fn in files:
            if fn.startswith("."):
                continue
            ext = os.path.splitext(fn)[1].lower()
            if ext in _SKIP_EXT:
                continue
            src = _norm(os.path.join(root, fn))
            meta = meta_index.get(src)
            fields, matched = _fields_for(src, meta)
            if not matched and not include_unmatched:
                plan.append(PlanItem(src, src, "skip", "no DB metadata", matched))
                continue
            rel = render_path_template(template, fields)
            dst = _norm(os.path.join(downloads_dir, rel))
            if dst == src:
                plan.append(PlanItem(src, dst, "noop", "already in place", matched))
            elif os.path.exists(dst):
                plan.append(PlanItem(src, dst, "conflict", "destination exists", matched))
            else:
                plan.append(PlanItem(src, dst, "move", "", matched))
    return plan


def plan_summary(plan: list[PlanItem]) -> dict:
    counts = {"move": 0, "noop": 0, "conflict": 0, "skip": 0}
    for it in plan:
        counts[it.action] = counts.get(it.action, 0) + 1
    return {"total": len(plan), **counts}


# ── Apply / rollback with manifest journal + in-memory job registry ──────────

_JOBS: dict[str, dict] = {}


def _manifest_dir(downloads_dir: str) -> str:
    return os.path.join(_norm(downloads_dir), MANIFEST_DIRNAME)


def _prune_empty_dirs(downloads_dir: str, dirs: set[str]) -> None:
    """Remove now-empty source dirs, deepest first. Never touches the root."""
    root = _norm(downloads_dir)
    for d in sorted(dirs, key=lambda p: p.count(os.sep), reverse=True):
        cur = _norm(d)
        while cur != root and cur.startswith(root):
            try:
                if not os.listdir(cur):
                    os.rmdir(cur)
                    cur = _norm(os.path.dirname(cur))
                    continue
            except OSError:
                pass
            break


def _do_move(src: str, dst: str) -> None:
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    os.replace(src, dst)   # atomic within the same volume


async def run_migration(template: str, downloads_dir: str,
                        meta_index: dict[str, dict],
                        include_unmatched: bool = False) -> str:
    """Start a background migration. Returns a job_id to poll/stream."""
    plan = await asyncio.to_thread(
        build_plan, template, downloads_dir, meta_index, include_unmatched)
    moves = [it for it in plan if it.action == "move"]
    job_id = uuid.uuid4().hex[:12]
    _JOBS[job_id] = {
        "id": job_id, "status": "running", "total": len(moves), "done": 0,
        "moved": 0, "errors": [], "manifest": None,
        "started": time.time(), "finished": None,
    }
    asyncio.create_task(_run_job(job_id, template, downloads_dir, moves))
    return job_id


async def _run_job(job_id: str, template: str, downloads_dir: str,
                   moves: list[PlanItem]) -> None:
    job = _JOBS[job_id]
    applied: list[dict] = []
    src_dirs: set[str] = set()
    try:
        for it in moves:
            try:
                await asyncio.to_thread(_do_move, it.src, it.dst)
                applied.append({"src": it.src, "dst": it.dst})
                src_dirs.add(os.path.dirname(it.src))
                job["moved"] += 1
            except Exception as e:  # noqa: BLE001 — record + keep going
                job["errors"].append({"src": it.src, "dst": it.dst, "error": str(e)})
            finally:
                job["done"] += 1
        # Journal first (so rollback is possible) then prune empty dirs.
        mdir = _manifest_dir(downloads_dir)
        os.makedirs(mdir, exist_ok=True)
        mpath = os.path.join(mdir, f"manifest_{int(job['started'])}_{job_id}.json")
        with open(mpath, "w", encoding="utf-8") as f:
            json.dump({"created": time.time(), "template": template,
                       "moves": applied}, f, indent=2)
        job["manifest"] = mpath
        await asyncio.to_thread(_prune_empty_dirs, downloads_dir, src_dirs)
        job["status"] = "done"
    except Exception as e:  # noqa: BLE001
        job["status"] = "error"
        job["errors"].append({"error": str(e)})
    finally:
        job["finished"] = time.time()


def get_job(job_id: str) -> dict | None:
    return _JOBS.get(job_id)


def latest_manifest(downloads_dir: str) -> str | None:
    mdir = _manifest_dir(downloads_dir)
    try:
        files = [os.path.join(mdir, f) for f in os.listdir(mdir)
                 if f.startswith("manifest_") and f.endswith(".json")]
    except OSError:
        return None
    if not files:
        return None
    return max(files, key=os.path.getmtime)


async def rollback(downloads_dir: str, manifest_path: str | None = None) -> dict:
    """Reverse the moves in a manifest (latest if not given). Reverses order so
    nested dirs unwind cleanly; never overwrites an existing source."""
    mpath = manifest_path or latest_manifest(downloads_dir)
    if not mpath or not os.path.exists(mpath):
        return {"ok": False, "error": "no manifest to roll back"}
    with open(mpath, encoding="utf-8") as f:
        data = json.load(f)
    moves = data.get("moves", [])
    reversed_ok, errors = 0, []
    dst_dirs: set[str] = set()
    for mv in reversed(moves):
        src, dst = mv["src"], mv["dst"]   # original src ← current dst
        try:
            if not os.path.exists(dst):
                errors.append({"dst": dst, "error": "file missing — moved/deleted since"})
                continue
            if os.path.exists(src):
                errors.append({"src": src, "error": "original path occupied — skipped"})
                continue
            await asyncio.to_thread(_do_move, dst, src)
            dst_dirs.add(os.path.dirname(dst))
            reversed_ok += 1
        except Exception as e:  # noqa: BLE001
            errors.append({"src": src, "dst": dst, "error": str(e)})
    await asyncio.to_thread(_prune_empty_dirs, downloads_dir, dst_dirs)
    if not errors:
        try:
            os.replace(mpath, mpath + ".rolledback")
        except OSError:
            pass
    return {"ok": not errors, "reversed": reversed_ok,
            "errors": errors, "manifest": mpath}
