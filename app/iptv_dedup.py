"""iptv-aggregator-v2 — dedup pipeline (Phase 1).

Maps the existing per-source `iptv_channels` rows into canonical
`logical_channels`. Runs at the end of refresh_all_sources() (and
manually via POST /api/iptv/dedup/run).

Pipeline order (per spec §5.4):
  1. Reset channel_id = NULL across all source rows (fresh start).
  2. Apply curated overrides from data/channel_aliases.yaml.
  3. tvg-id family pass (CNA.sg + CNA.sg@SD → same channel).
  4. (name-slug + country) bucket pass for everything else.
  5. Drop logical_channels with zero source rows.

Source priorities come from data/source_priorities.yaml, with
per-channel overrides applied from data/channel_aliases.yaml during
step 2.

Idempotent — running it twice produces the same logical_channels
table state.
"""
from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from typing import Any

import aiosqlite

from . import database as db

logger = logging.getLogger(__name__)


_DATA_DIR = Path(__file__).parent.parent / "data"
_SOURCE_PRIORITIES_YAML = _DATA_DIR / "source_priorities.yaml"
_CHANNEL_ALIASES_YAML   = _DATA_DIR / "channel_aliases.yaml"

# Suffixes / qualifiers stripped during slug bucketing. The order
# matters slightly — longer-and-more-specific first so "1080p" doesn't
# leave a stray "1080" behind. Case-insensitive match via .lower().
_NOISE_TAGS = [
    "[geo-blocked]", "(geo-blocked)", "[geo blocked]",
    "[backup]", "(backup)",
    "(1080p)", "(720p)", "(576p)", "(480p)",
    "1080p", "720p", "576p", "480p",
    "fhd", " hd", "(hd)",
]


def _load_yaml(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        import yaml
        with path.open("r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except Exception:
        logger.exception("YAML parse failed: %s", path)
        return {}


def load_source_priorities() -> dict[str, int]:
    """Source-id → default priority. Anything unmapped falls back to 5."""
    raw = _load_yaml(_SOURCE_PRIORITIES_YAML)
    out = {}
    for src, prio in (raw.get("defaults") or {}).items():
        out[str(src)] = int(prio)
    return out


def load_channel_aliases() -> dict[str, dict]:
    """logical_id → {name, country, aliases, sources, priority_overrides…}.
    Empty dict if the YAML is missing or empty."""
    raw = _load_yaml(_CHANNEL_ALIASES_YAML)
    return raw.get("channels") or {}


def normalise_name(name: str) -> str:
    """Lowercase, strip noise tags, collapse non-alnum to '-'. Used as
    the bucket key for un-curated channels."""
    s = (name or "").lower()
    for tag in _NOISE_TAGS:
        s = s.replace(tag, " ")
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s


def tvg_family(tvg_id: str) -> str:
    """Strip @SD/@HD/@1080p/@FHD quality suffix from an iptv-org tvg-id.
    `CNA.sg` and `CNA.sg@SD` share the same family → same channel."""
    if not tvg_id:
        return ""
    return tvg_id.split("@", 1)[0].strip().lower()


def _tvg_id_from_source_row(row: aiosqlite.Row) -> str:
    """Extract a usable tvg-id from a source row's primary key. iptv-org
    sources store it as `<source>:<tvg_id>`, so the slug after the colon
    IS the tvg-id."""
    pk = row["id"] or ""
    return pk.split(":", 1)[-1] if ":" in pk else pk


def _iso_now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


async def run_dedup() -> dict:
    """Execute the dedup pipeline. Returns a summary dict for the
    refresh / /dedup/run API response."""
    t0 = time.time()
    priorities = load_source_priorities()
    curated    = load_channel_aliases()
    now_iso    = _iso_now()

    summary = {
        "ok": True,
        "started_at":   now_iso,
        "curated_channels": 0,
        "tvg_family_groups": 0,
        "slug_groups":  0,
        "logical_channels_total": 0,
        "source_rows_assigned":   0,
        "source_rows_unassigned": 0,
        "duration_ms": 0,
    }

    async with aiosqlite.connect(db.DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row

        # Step 1: reset everything — start fresh so a removed curated
        # entry stops applying its override on the next run.
        await conn.execute("UPDATE iptv_channels SET channel_id = NULL")

        # Pre-load every channel row's metadata we'll need. ~12k rows × a
        # few columns is fine in memory.
        cur = await conn.execute(
            "SELECT id, name, country, languages, categories, logo, source, url "
            "FROM iptv_channels"
        )
        all_rows = await cur.fetchall()

        # Step 2: curated overrides. Highest authority — these win
        # over both tvg-family and slug bucketing.
        curated_assigned: set[str] = set()
        for logical_id, spec in curated.items():
            if not isinstance(spec, dict):
                continue
            source_ids = spec.get("sources") or []
            if not source_ids:
                continue
            cur_aliases = spec.get("aliases") or []
            cur_categories = spec.get("categories") or []
            await conn.execute("""
                INSERT INTO logical_channels (
                    id, name, country, languages, categories, logo, aliases,
                    is_curated, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    name = excluded.name,
                    country = excluded.country,
                    languages = excluded.languages,
                    categories = excluded.categories,
                    logo = excluded.logo,
                    aliases = excluded.aliases,
                    is_curated = 1,
                    updated_at = excluded.updated_at
            """, (
                logical_id,
                spec.get("name") or logical_id,
                (spec.get("country") or "").upper() or None,
                None,
                (",".join(cur_categories) if cur_categories else None),
                spec.get("logo"),
                json.dumps(cur_aliases, ensure_ascii=False),
                now_iso, now_iso,
            ))
            # Apply per-channel priority overrides
            prio_overrides = spec.get("priority_overrides") or {}
            for sid in source_ids:
                row = next((r for r in all_rows if r["id"] == sid), None)
                if not row:
                    continue   # source not in catalogue (yet) — skip silently
                src = row["source"] or ""
                base_prio = priorities.get(src, 5)
                final_prio = int(prio_overrides.get(sid, base_prio))
                await conn.execute(
                    "UPDATE iptv_channels SET channel_id = ?, priority = ? WHERE id = ?",
                    (logical_id, final_prio, sid),
                )
                curated_assigned.add(sid)
            summary["curated_channels"] += 1

        # Re-read to see who's still NULL (assignment changes propagate)
        cur = await conn.execute(
            "SELECT id, name, country, source FROM iptv_channels WHERE channel_id IS NULL"
        )
        uncurated_rows = await cur.fetchall()

        # Step 3: tvg-id family pass — only meaningful for iptv-org-*
        # sources where the row id IS the tvg-id with a quality suffix.
        family_groups: dict[tuple[str, str], list[aiosqlite.Row]] = {}
        for row in uncurated_rows:
            src = row["source"] or ""
            if not src.startswith("iptv-org"):
                continue
            tvg = _tvg_id_from_source_row(row)
            fam = tvg_family(tvg)
            if not fam:
                continue
            country = (row["country"] or "").upper() or "XX"
            family_groups.setdefault((fam, country), []).append(row)

        for (fam, country), rows in family_groups.items():
            if len(rows) < 1:
                continue
            # Mint a stable logical_id from the family + country.
            logical_id = re.sub(r"[^a-z0-9]+", "-", f"{fam}-{country}".lower()).strip("-")
            # Pick the canonical name from the family head (alphabetical
            # by source row id — deterministic across runs).
            head = sorted(rows, key=lambda r: r["id"])[0]
            # INSERT OR IGNORE — if a curated channel already owns this
            # logical_id, leave its metadata alone (curated wins per §5.3).
            await conn.execute("""
                INSERT OR IGNORE INTO logical_channels (
                    id, name, country, languages, categories, logo, aliases,
                    is_curated, created_at, updated_at
                ) VALUES (?, ?, ?, NULL, NULL, NULL, NULL, 0, ?, ?)
            """, (logical_id, head["name"], country if country != "XX" else None,
                  now_iso, now_iso))
            # Refresh updated_at on existing non-curated rows so the
            # view's last-updated heuristic stays accurate.
            await conn.execute(
                "UPDATE logical_channels SET updated_at = ? "
                " WHERE id = ? AND is_curated = 0",
                (now_iso, logical_id),
            )
            for row in rows:
                base_prio = priorities.get(row["source"] or "", 5)
                await conn.execute(
                    "UPDATE iptv_channels SET channel_id = ?, priority = ? WHERE id = ?",
                    (logical_id, base_prio, row["id"]),
                )
            summary["tvg_family_groups"] += 1

        # Step 4: slug + country bucket pass for whatever's still NULL.
        cur = await conn.execute(
            "SELECT id, name, country, source FROM iptv_channels WHERE channel_id IS NULL"
        )
        remaining = await cur.fetchall()
        slug_groups: dict[tuple[str, str], list[aiosqlite.Row]] = {}
        for row in remaining:
            slug = normalise_name(row["name"] or "")
            if not slug:
                continue
            country = (row["country"] or "").upper() or "XX"
            slug_groups.setdefault((slug, country), []).append(row)

        for (slug, country), rows in slug_groups.items():
            if len(rows) < 1:
                continue
            logical_id = re.sub(r"[^a-z0-9]+", "-",
                                 f"{slug}-{country}".lower()).strip("-")
            head = sorted(rows, key=lambda r: r["id"])[0]
            # Same INSERT OR IGNORE as the tvg-family pass — never
            # overwrite a curated channel's metadata.
            await conn.execute("""
                INSERT OR IGNORE INTO logical_channels (
                    id, name, country, languages, categories, logo, aliases,
                    is_curated, created_at, updated_at
                ) VALUES (?, ?, ?, NULL, NULL, NULL, NULL, 0, ?, ?)
            """, (logical_id, head["name"], country if country != "XX" else None,
                  now_iso, now_iso))
            await conn.execute(
                "UPDATE logical_channels SET updated_at = ? "
                " WHERE id = ? AND is_curated = 0",
                (now_iso, logical_id),
            )
            for row in rows:
                base_prio = priorities.get(row["source"] or "", 5)
                await conn.execute(
                    "UPDATE iptv_channels SET channel_id = ?, priority = ? WHERE id = ?",
                    (logical_id, base_prio, row["id"]),
                )
            summary["slug_groups"] += 1

        # Step 5: clean up logical_channels with zero source rows
        # (e.g. a curated entry whose sources are all gone from the catalogue).
        await conn.execute("""
            DELETE FROM logical_channels
             WHERE id NOT IN (SELECT DISTINCT channel_id
                              FROM iptv_channels WHERE channel_id IS NOT NULL)
        """)

        await conn.commit()

        # Final counts
        cur = await conn.execute("SELECT COUNT(*) FROM logical_channels")
        summary["logical_channels_total"] = int((await cur.fetchone())[0])
        cur = await conn.execute(
            "SELECT COUNT(*) FROM iptv_channels WHERE channel_id IS NOT NULL"
        )
        summary["source_rows_assigned"] = int((await cur.fetchone())[0])
        cur = await conn.execute(
            "SELECT COUNT(*) FROM iptv_channels WHERE channel_id IS NULL"
        )
        summary["source_rows_unassigned"] = int((await cur.fetchone())[0])

    summary["duration_ms"] = int((time.time() - t0) * 1000)
    logger.info("iptv dedup: %s", summary)
    return summary


async def pick_best_source(channel_id: str) -> dict | None:
    """Return {url, source_id, status, priority, alternates[]} for the
    logical channel. Sort order: priority desc → alive first → most
    recently checked first. Returns None if the channel has no sources."""
    async with aiosqlite.connect(db.DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute("""
            SELECT id, name, source, url, status, last_check_at, priority,
                   probe_count, alive_count
              FROM iptv_channels
             WHERE channel_id = ? AND url IS NOT NULL AND url != ''
             ORDER BY priority DESC,
                      (status = 'alive') DESC,
                      COALESCE(last_check_at, '1970-01-01') DESC
        """, (channel_id,))
        rows = await cur.fetchall()
    if not rows:
        return None
    head = rows[0]
    return {
        "url":       head["url"],
        "source_id": head["id"],
        "source":    head["source"],
        "status":    head["status"],
        "priority":  head["priority"],
        "alternates": [
            {
                "source_id": r["id"],
                "source":    r["source"],
                "url":       r["url"],
                "status":    r["status"],
                "priority":  r["priority"],
                "probe_count": r["probe_count"],
                "alive_count": r["alive_count"],
            }
            for r in rows[1:]
        ],
    }


async def list_sources_for_channel(channel_id: str) -> list[dict]:
    """All source rows for a logical channel, sorted by play-priority."""
    async with aiosqlite.connect(db.DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute("""
            SELECT id, name, source, url, status, last_check_at, priority,
                   probe_count, alive_count, logo
              FROM iptv_channels
             WHERE channel_id = ?
             ORDER BY priority DESC,
                      (status = 'alive') DESC,
                      COALESCE(last_check_at, '1970-01-01') DESC
        """, (channel_id,))
        rows = await cur.fetchall()
    return [dict(r) for r in rows]
