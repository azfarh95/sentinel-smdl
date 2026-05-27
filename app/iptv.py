"""IPTV channel registry + iptv-org bootstrap.

Pulls the canonical free-stream registry maintained by `iptv-org/iptv`
(MIT, updated daily by their CI) — channels.json + streams.json joined
into a per-channel record. Persists into a small SQLite table and
exposes probe / list / record helpers.

Why iptv-org and not "free IPTV M3U" sites?
- License-clean (only public, embassy, government, free-promotional streams)
- Daily-checked alive-flag; we re-probe on demand for the last mile
- Stable URLs that don't rot in a week
- No DRM — works with yt-dlp / ffmpeg natively

Bootstrap pattern: caller invokes `refresh_from_iptv_org()` either on
demand (button in the Mini App) or on a schedule (e.g. weekly cron). The
job inserts new channels, marks vanished ones inactive, and resets the
alive flag from the source's own daily probe.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Iterable

import aiosqlite
import httpx

from . import database as db


logger = logging.getLogger(__name__)


IPTV_ORG_CHANNELS_URL = "https://iptv-org.github.io/api/channels.json"
IPTV_ORG_STREAMS_URL  = "https://iptv-org.github.io/api/streams.json"

# Per-row caps so a hostile iptv-org payload (or a typo) can't blow the DB
_MAX_NAME = 200
_MAX_URL  = 1024


@dataclass
class IptvChannel:
    """One iptv-org-sourced channel row in `iptv_channels`."""
    id: str            # source-prefixed unique id (e.g. "iptv-org:CNA.sg", "mjh:pluto-...")
    name: str
    country: str | None
    languages: str | None  # comma-joined ISO codes
    categories: str | None
    url: str | None    # picked primary stream URL
    logo: str | None    # source-provided logo URL
    is_nsfw: bool
    alive: bool | None     # last known from upstream's daily probe
    status: str         # 'unprobed' | 'alive' | 'dead' | 'error'
    last_check_at: str | None
    last_error: str | None
    source: str         # 'iptv-org' | 'free-tv' | 'mjh-all' — which catalogue

    @classmethod
    def from_row(cls, r: aiosqlite.Row) -> "IptvChannel":
        # `logo` + `source` are added by later migrations; tolerate older
        # rows where one or both are missing.
        try:
            logo = r["logo"]
        except (IndexError, KeyError):
            logo = None
        try:
            source = r["source"] or "iptv-org"
        except (IndexError, KeyError):
            source = "iptv-org"
        return cls(
            id=r["id"], name=r["name"], country=r["country"],
            languages=r["languages"], categories=r["categories"],
            url=r["url"], logo=logo, is_nsfw=bool(r["is_nsfw"]),
            alive=(None if r["alive"] is None else bool(r["alive"])),
            status=r["status"], last_check_at=r["last_check_at"],
            last_error=r["last_error"], source=source,
        )

    def to_dict(self) -> dict:
        return {
            "id": self.id, "name": self.name, "country": self.country,
            "languages": (self.languages or "").split(",") if self.languages else [],
            "categories": (self.categories or "").split(",") if self.categories else [],
            "url": self.url, "logo": self.logo, "is_nsfw": self.is_nsfw, "alive": self.alive,
            "status": self.status, "last_check_at": self.last_check_at,
            "last_error": self.last_error, "source": self.source,
        }


# ── Schema ──────────────────────────────────────────────────────────


# ── EPG sources ─────────────────────────────────────────────────────


EPG_SOURCES: dict[str, str] = {
    "mjh":          "https://i.mjh.nz/all/epg.xml.gz",
    "epgshare-sg":  "https://epgshare01.online/epgshare01/epg_ripper_SG1.xml.gz",
    "epgshare-my":  "https://epgshare01.online/epgshare01/epg_ripper_MY1.xml.gz",
    "epgshare-id":  "https://epgshare01.online/epgshare01/epg_ripper_ID1.xml.gz",
}


async def init_iptv_schema() -> None:
    """Idempotent — call from the app lifespan after db.init_db()."""
    async with aiosqlite.connect(db.DB_PATH) as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS iptv_channels (
                id            TEXT PRIMARY KEY,
                name          TEXT NOT NULL,
                country       TEXT,
                languages     TEXT,
                categories    TEXT,
                url           TEXT,
                logo          TEXT,
                is_nsfw       INTEGER NOT NULL DEFAULT 0,
                alive         INTEGER,
                status        TEXT NOT NULL DEFAULT 'unprobed',
                last_check_at TEXT,
                last_error    TEXT,
                last_seen_at  TEXT NOT NULL
            )
        """)
        # Migrations: add `logo` + `source` columns to pre-existing installs.
        # SQLite ALTER is cheap when the column is missing; otherwise swallow
        # the "duplicate column name" error.
        for sql in (
            "ALTER TABLE iptv_channels ADD COLUMN logo TEXT",
            "ALTER TABLE iptv_channels ADD COLUMN source TEXT NOT NULL DEFAULT 'iptv-org'",
        ):
            try:
                await conn.execute(sql)
            except Exception:
                pass
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_iptv_country
                ON iptv_channels(country)
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_iptv_source
                ON iptv_channels(source)
        """)
        # One-shot id migration: old iptv-org rows were saved with bare
        # ids like 'CNA.sg'; the new scheme prefixes with '<source>:' so
        # multiple sources can coexist without collisions. The check is
        # `id NOT LIKE '%:%'` which is cheap (no FTS needed).
        try:
            await conn.execute("""
                UPDATE iptv_channels
                   SET id = 'iptv-org:' || id
                 WHERE source = 'iptv-org' AND id NOT LIKE '%:%'
            """)
        except Exception as exc:
            logger.warning("iptv id-prefix migration skipped: %s", exc)
        # mjh-all is now a FETCH source only — rows fan out to one of
        # five canonical sub-source buckets. Two legacy shapes need
        # purging on first run: (a) source='mjh-all' from before the
        # split, (b) over-split single-channel buckets from an
        # intermediate iteration. Purging is idempotent — once the
        # legacy rows are gone, this DELETE is a no-op.
        _MJH_VALID = (
            "mjh-radio", "mjh-sky-fast", "mjh-au", "mjh-nz", "mjh-other",
        )
        try:
            placeholders = ",".join("?" for _ in _MJH_VALID)
            cur = await conn.execute(
                f"""DELETE FROM iptv_channels
                     WHERE source = 'mjh-all'
                        OR (source LIKE 'mjh-%'
                            AND source NOT IN ({placeholders}))""",
                _MJH_VALID,
            )
            if cur.rowcount:
                logger.info(
                    "iptv migration: cleared %d legacy mjh-* rows "
                    "(out-of-spec source ids) — will repopulate on next refresh",
                    cur.rowcount,
                )
        except Exception as exc:
            logger.warning("mjh-* cleanup skipped: %s", exc)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_iptv_status
                ON iptv_channels(status)
        """)
        # EPG programmes — XMLTV-imported. Keyed by (tvg_id, start_utc)
        # so we can idempotently re-import without dup rows.
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS iptv_programmes (
                tvg_id      TEXT NOT NULL,
                start_utc   TEXT NOT NULL,
                end_utc     TEXT NOT NULL,
                title       TEXT NOT NULL,
                subtitle    TEXT,
                description TEXT,
                category    TEXT,
                source      TEXT NOT NULL,
                PRIMARY KEY (tvg_id, start_utc)
            )
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_iptv_prog_tvg_time
                ON iptv_programmes(tvg_id, start_utc)
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_iptv_prog_window
                ON iptv_programmes(end_utc)
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS iptv_recordings (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                channel_id    TEXT NOT NULL,
                job_id        TEXT NOT NULL,
                duration_min  INTEGER NOT NULL,
                requested_at  TEXT NOT NULL,
                started_at    TEXT,
                finished_at   TEXT,
                status        TEXT NOT NULL DEFAULT 'queued',
                output_path   TEXT,
                error         TEXT
            )
        """)
        await conn.commit()
    logger.info("IPTV schema ready at %s", db.DB_PATH)


# ── M3U parsing ─────────────────────────────────────────────────────


_EXTINF_ATTR_RE = re.compile(r'([a-zA-Z\-_]+)\s*=\s*"([^"]*)"')


def parse_m3u(text: str) -> list[dict]:
    """Parse a #EXTINF M3U playlist into a list of channel dicts.

    Returns dicts with keys: name, url, logo, group, tvg_id, country (if
    derivable from tvg-id suffix / group-title). Supports the common
    `#EXTVLCOPT:` ahead-of-URL lines that mjh / xtream playlists use —
    we just skip them (we don't currently honour custom UA / referrer).
    """
    out: list[dict] = []
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if line.startswith("#EXTINF"):
            # Format:  #EXTINF:<dur> [k="v" k="v" ...], <display name>
            attrs: dict[str, str] = {}
            name = ""
            # Split off the trailing ", display-name"
            comma_idx = line.rfind(",")
            if comma_idx > 0:
                name = line[comma_idx + 1:].strip()
                head = line[:comma_idx]
            else:
                head = line
            for m in _EXTINF_ATTR_RE.finditer(head):
                attrs[m.group(1).lower()] = m.group(2)
            # Skip any options lines, then the next non-blank non-comment
            # line is the URL.
            url = ""
            j = i + 1
            while j < len(lines):
                cand = lines[j].strip()
                j += 1
                if not cand:
                    continue
                if cand.startswith("#"):
                    continue
                url = cand
                break
            i = j
            if not url or not name:
                continue
            # Some playlists postfix `|seekable=0` or similar — strip it
            # because httpx + ffmpeg both choke on unknown pipe-args.
            if "|" in url and "://" in url:
                url = url.split("|", 1)[0]
            country = ""
            tvg_id = attrs.get("tvg-id", "")
            # iptv-org-style ids end in ".cc"; mjh ids don't.
            if "." in tvg_id and len(tvg_id.split(".")[-1]) == 2:
                country = tvg_id.split(".")[-1].upper()
            out.append({
                "name": name,
                "url": url,
                "logo": attrs.get("tvg-logo") or None,
                "group": attrs.get("group-title") or "",
                "tvg_id": tvg_id,
                "country": country or (attrs.get("tvg-country") or "").upper(),
            })
        else:
            i += 1
    return out


# ── Source registry ─────────────────────────────────────────────────
#
# Each source knows how to fetch its catalogue + return a list of
# normalised channel dicts. The upsert loop below is shared.


SOURCES: dict[str, dict] = {
    "iptv-org": {
        "name": "iptv-org (global)",
        "url":  "https://iptv-org.github.io/iptv/",
        "kind": "json",
    },
    "free-tv": {
        "name": "Free-TV/IPTV (community-curated)",
        "url":  "https://raw.githubusercontent.com/Free-TV/IPTV/master/playlist.m3u8",
        "kind": "m3u",
    },
    "mjh-all": {
        "name": "i.mjh.nz (Pluto/Samsung/Plex/Sky/Foxtel aggregate)",
        "url":  "https://i.mjh.nz/all/kodi.m3u8",
        "kind": "m3u",
    },
    "fanmingming": {
        "name": "fanmingming/live (CCTV + Asia + global)",
        "url":  "https://raw.githubusercontent.com/fanmingming/live/main/tv/m3u/itv.m3u",
        "kind": "m3u",
    },
    "yuechan": {
        "name": "YueChan/Live (curated global)",
        "url":  "https://raw.githubusercontent.com/YueChan/Live/main/Global.m3u",
        "kind": "m3u",
    },
    "openiptvitaly": {
        "name": "OpenIPTVItaly (Italian curated + EPG channel numbers)",
        "url":  "https://raw.githubusercontent.com/xN1ckuz/OpenIPTVItaly/main/OpenIPTVItaly.m3u",
        "kind": "m3u",
    },
}


# ── Per-country iptv-org slices ─────────────────────────────────────
#
# These are CURATED subsets that iptv-org publishes at
# /iptv/countries/{cc}.m3u — different URLs (often higher quality) than
# the global JSON catalogue. We register them as separate source ids so
# they coexist with the global rows: same channel name might appear from
# both sources with different streams.

IPTV_ORG_COUNTRY_BASE = "https://iptv-org.github.io/iptv/countries"

# Pre-baked quick-action set — surfaced as buttons in the Mini App.
# refresh_iptv_org_country() will accept any ISO-3166 alpha-2 code.
IPTV_ORG_COUNTRY_QUICK = ["sg", "my", "id"]


def _country_source_id(cc: str) -> str:
    return f"iptv-org-{cc.lower()}"


def country_source_meta(cc: str) -> dict:
    """Synthesised SOURCES-style entry for a country slice — drives the
    `/api/iptv/sources` listing without bloating the static SOURCES dict
    with one row per country code."""
    cc_l = cc.lower()
    return {
        "id":   _country_source_id(cc_l),
        "name": f"iptv-org/countries/{cc_l}.m3u (curated)",
        "kind": "m3u",
        "url":  f"{IPTV_ORG_COUNTRY_BASE}/{cc_l}.m3u",
    }


# ── iptv-org bootstrap ─────────────────────────────────────────────


async def refresh_from_iptv_org(
    country: str | None = None,
    include_nsfw: bool = False,
    timeout_s: float = 20.0,
) -> dict:
    """Fetch channels.json + streams.json, join, upsert into iptv_channels.

    `country` is an ISO code (e.g. "SG"); None = all countries.
    Returns a small summary dict for the API response.
    """
    t0 = time.time()
    async with httpx.AsyncClient(timeout=timeout_s) as client:
        ch_resp, st_resp = await asyncio.gather(
            client.get(IPTV_ORG_CHANNELS_URL),
            client.get(IPTV_ORG_STREAMS_URL),
        )
    ch_resp.raise_for_status()
    st_resp.raise_for_status()
    channels = ch_resp.json()
    streams = st_resp.json()

    # Pre-build: streams keyed by channel id, prefer ones with alive=true.
    # iptv-org's streams.json doesn't carry a per-row alive flag historically;
    # presence in the list is the signal. We pick the first URL per channel.
    streams_by_channel: dict[str, str] = {}
    for s in streams:
        ch_id = s.get("channel") or ""
        url = s.get("url") or ""
        if not ch_id or not url:
            continue
        if ch_id in streams_by_channel:
            continue   # first-write-wins; the order of streams.json is the source's preference
        if len(url) > _MAX_URL:
            continue
        streams_by_channel[ch_id] = url

    now_iso = _iso_now()
    upserted = 0
    skipped_no_url = 0
    skipped_country = 0
    skipped_nsfw = 0
    target_country = (country or "").upper().strip() or None

    async with aiosqlite.connect(db.DB_PATH) as conn:
        for ch in channels:
            ch_id = ch.get("id") or ""
            if not ch_id:
                continue
            ch_country = (ch.get("country") or "").upper()
            if target_country and ch_country != target_country:
                skipped_country += 1
                continue
            is_nsfw = bool(ch.get("is_nsfw", False))
            if is_nsfw and not include_nsfw:
                skipped_nsfw += 1
                continue
            url = streams_by_channel.get(ch_id)
            if not url:
                skipped_no_url += 1
                continue
            name = (ch.get("name") or ch_id)[:_MAX_NAME]
            languages = ",".join((lang or "") for lang in (ch.get("languages") or []) if lang)
            categories = ",".join((cat or "") for cat in (ch.get("categories") or []) if cat)
            logo = ch.get("logo") or None
            if logo and len(logo) > _MAX_URL:
                logo = None
            row_id = f"iptv-org:{ch_id}"
            await conn.execute("""
                INSERT INTO iptv_channels (
                    id, name, country, languages, categories, url, logo,
                    is_nsfw, alive, status, last_check_at, last_error, last_seen_at, source
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, 'unprobed', NULL, NULL, ?, 'iptv-org')
                ON CONFLICT(id) DO UPDATE SET
                    name = excluded.name,
                    country = excluded.country,
                    languages = excluded.languages,
                    categories = excluded.categories,
                    url = excluded.url,
                    logo = excluded.logo,
                    is_nsfw = excluded.is_nsfw,
                    last_seen_at = excluded.last_seen_at,
                    source = excluded.source
            """, (row_id, name, ch_country or None, languages or None,
                  categories or None, url, logo, 1 if is_nsfw else 0, now_iso))
            upserted += 1
        await conn.commit()

    summary = {
        "ok": True,
        "source": "iptv-org",
        "channels_fetched": len(channels),
        "streams_fetched": len(streams),
        "upserted": upserted,
        "skipped_country": skipped_country,
        "skipped_no_url": skipped_no_url,
        "skipped_nsfw": skipped_nsfw,
        "filter_country": target_country,
        "duration_ms": int((time.time() - t0) * 1000),
    }
    logger.info("iptv refresh: %s", summary)
    return summary


# ── M3U-source refresh (Free-TV, mjh-all, ad-hoc URLs) ─────────────


def _derive_subsource(source_id: str, item: dict) -> str:
    """Fan one M3U source out into multiple row-level sources.

    For mjh-all, the natural cleavage is:
      - mjh-radio    — 581 radio stations (kept out of the TV grid)
      - mjh-sky-fast — Sky NZ's FAST channel slate (HGTV, MovieSphere, etc.)
      - mjh-au       — AU public broadcasters (10, Seven, ABC, SBS, …)
      - mjh-nz       — NZ public broadcasters (TVNZ, Three, …)
      - mjh-other    — residual

    Splitting too fine (one bucket per `mjh-<service>` token) gave 108
    buckets, most with 1-2 channels — useless for filtering."""
    if source_id != "mjh-all":
        return source_id
    tvg = (item.get("tvg_id") or "").strip().lower()
    group = (item.get("group") or "").strip().lower()
    country = (item.get("country") or "").upper()
    # Country derivation mirrors refresh_from_m3u's logic for mjh
    # (group is "Nz"/"Au" etc. for tv channels).
    if not country and group and len(group) <= 3 and group.isalpha():
        country = group.upper()

    if tvg.startswith("mjh-radio"):
        return "mjh-radio"
    if tvg.startswith("mjh-sky-"):
        return "mjh-sky-fast"   # Sky NZ's syndicated FAST channels (HGTV etc.)
    if country == "AU":
        return "mjh-au"
    if country == "NZ":
        return "mjh-nz"
    return "mjh-other"


async def refresh_from_m3u(
    source_id: str,
    timeout_s: float = 30.0,
) -> dict:
    """Pull an M3U playlist for the named source and upsert into the
    catalogue. Channel id is `<source>:<hash-of-tvg-id-or-name>` so the
    same playlist re-import is idempotent.

    For `mjh-all`, rows fan out into sub-sources (`mjh-sky`, `mjh-radio`,
    etc.) via _derive_subsource so the Source chip row gives one-tap
    access to e.g. just "Sky NZ FAST" or just "Channel 10 AU".

    Categories are taken from the `group-title` attribute; country from
    a `tvg-id` ending in `.cc` or the `tvg-country` attribute (mjh
    sometimes uses `group-title` as the country bucket — e.g.
    `group-title="Nz"`)."""
    if source_id not in SOURCES:
        raise KeyError(f"unknown source {source_id!r}")
    src = SOURCES[source_id]
    if src["kind"] != "m3u":
        raise ValueError(f"source {source_id} is not an M3U")

    t0 = time.time()
    async with httpx.AsyncClient(timeout=timeout_s, follow_redirects=True) as client:
        r = await client.get(src["url"])
    r.raise_for_status()
    items = parse_m3u(r.text)

    now_iso = _iso_now()
    upserted = 0
    skipped = 0
    async with aiosqlite.connect(db.DB_PATH) as conn:
        for it in items:
            url = it.get("url") or ""
            name = (it.get("name") or "").strip()
            if not url or not name or len(url) > _MAX_URL:
                skipped += 1
                continue
            name = name[:_MAX_NAME]
            group = (it.get("group") or "").strip()
            # mjh playlist uses group-title for country (capitalised ISO
            # word). If group is a 2-3 char string that looks like a code,
            # fall through; if it's longer treat it as a category instead.
            country = (it.get("country") or "").upper() or None
            if not country and group and len(group) <= 3 and group.isalpha():
                country = group.upper()
            category = group.lower() if (group and country != group.upper()) else None
            tvg_id = it.get("tvg_id") or ""
            # Stable id: tvg-id if present, else slug of the display name
            slug = tvg_id or re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
            row_source = _derive_subsource(source_id, it)
            row_id = f"{row_source}:{slug}"
            logo = it.get("logo") or None
            await conn.execute("""
                INSERT INTO iptv_channels (
                    id, name, country, languages, categories, url, logo,
                    is_nsfw, alive, status, last_check_at, last_error, last_seen_at, source
                ) VALUES (?, ?, ?, NULL, ?, ?, ?, 0, NULL, 'unprobed', NULL, NULL, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    name = excluded.name,
                    country = excluded.country,
                    categories = excluded.categories,
                    url = excluded.url,
                    logo = excluded.logo,
                    last_seen_at = excluded.last_seen_at,
                    source = excluded.source
            """, (row_id, name, country, category, url, logo, now_iso, row_source))
            upserted += 1
        await conn.commit()

    summary = {
        "ok": True,
        "source": source_id,
        "channels_fetched": len(items),
        "upserted": upserted,
        "skipped": skipped,
        "duration_ms": int((time.time() - t0) * 1000),
    }
    logger.info("iptv refresh: %s", summary)
    return summary


# ── EPG (XMLTV) ─────────────────────────────────────────────────────


def _parse_xmltv_time(s: str) -> str:
    """XMLTV uses `20260527103000 +0000` — return ISO 8601 UTC for SQLite."""
    s = (s or "").strip()
    if not s:
        return ""
    from datetime import datetime, timezone
    # Format:  YYYYMMDDhhmmss <offset>  (offset may be missing)
    try:
        if " " in s:
            ts, off = s.split(" ", 1)
        else:
            ts, off = s, "+0000"
        dt = datetime.strptime(ts, "%Y%m%d%H%M%S")
        # Apply offset → UTC
        sign = 1 if off.startswith("+") else -1
        oh = int(off[1:3]); om = int(off[3:5])
        from datetime import timedelta
        dt = dt.replace(tzinfo=timezone.utc) - sign * timedelta(hours=oh, minutes=om)
        return dt.isoformat()
    except Exception:
        return ""


async def refresh_epg(
    source_id: str,
    timeout_s: float = 60.0,
) -> dict:
    """Fetch a gzipped XMLTV from EPG_SOURCES[source_id], parse, upsert
    programmes. Memory-efficient streaming parse via iterparse."""
    if source_id not in EPG_SOURCES:
        raise KeyError(f"unknown EPG source {source_id!r}")
    url = EPG_SOURCES[source_id]
    t0 = time.time()
    async with httpx.AsyncClient(timeout=timeout_s, follow_redirects=True) as client:
        r = await client.get(url)
    r.raise_for_status()
    raw = r.content
    # Auto-decompress gzip — both Python's gzip and the lighter
    # xml.etree.ElementTree.parse can chew it.
    import gzip, io
    import xml.etree.ElementTree as ET
    if url.endswith(".gz") or (len(raw) >= 2 and raw[:2] == b"\x1f\x8b"):
        try:
            raw = gzip.decompress(raw)
        except Exception:
            pass   # already-decompressed or junk
    n_chan = 0
    n_prog = 0
    n_kept = 0
    # Stream-parse — XMLTV files can be 10+ MB
    src = io.BytesIO(raw)
    # Keep only programmes from now-1hr to now+24hr to bound the DB
    from datetime import datetime, timezone, timedelta
    horizon_start = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    horizon_end   = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()
    async with aiosqlite.connect(db.DB_PATH) as conn:
        # Delete the prior window for this source so re-imports drop
        # stale rows (yesterday's schedule). Same source is the natural
        # boundary — channels in the SG feed don't get touched by the MY
        # feed's import, etc.
        await conn.execute(
            "DELETE FROM iptv_programmes WHERE source = ?",
            (source_id,),
        )
        for _, elem in ET.iterparse(src, events=("end",)):
            tag = elem.tag
            if tag == "channel":
                n_chan += 1
                elem.clear()
                continue
            if tag != "programme":
                continue
            n_prog += 1
            ch = (elem.get("channel") or "").strip()
            start = _parse_xmltv_time(elem.get("start") or "")
            stop  = _parse_xmltv_time(elem.get("stop") or "")
            if not ch or not start or not stop:
                elem.clear(); continue
            # Skip rows outside our retention window
            if stop < horizon_start or start > horizon_end:
                elem.clear(); continue
            title_el = elem.find("title")
            title = (title_el.text or "").strip() if title_el is not None else ""
            if not title:
                elem.clear(); continue
            subt_el = elem.find("sub-title")
            subt = (subt_el.text or "").strip() if subt_el is not None else None
            desc_el = elem.find("desc")
            desc = (desc_el.text or "").strip() if desc_el is not None else None
            cat_el = elem.find("category")
            cat  = (cat_el.text or "").strip() if cat_el is not None else None
            await conn.execute("""
                INSERT INTO iptv_programmes
                       (tvg_id, start_utc, end_utc, title, subtitle, description, category, source)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(tvg_id, start_utc) DO UPDATE SET
                    end_utc = excluded.end_utc,
                    title = excluded.title,
                    subtitle = excluded.subtitle,
                    description = excluded.description,
                    category = excluded.category,
                    source = excluded.source
            """, (ch, start, stop, title[:300],
                  (subt or "")[:300] or None,
                  (desc or "")[:2000] or None,
                  (cat or "")[:80] or None,
                  source_id))
            n_kept += 1
            elem.clear()
        await conn.commit()

    return {
        "ok": True,
        "epg_source": source_id,
        "channels_in_xml": n_chan,
        "programmes_in_xml": n_prog,
        "programmes_kept": n_kept,
        "duration_ms": int((time.time() - t0) * 1000),
    }


async def refresh_all_epg() -> list[dict]:
    """Refresh every EPG source in EPG_SOURCES. Run after channel
    catalogue refresh so the EPG points at known tvg-ids."""
    out: list[dict] = []
    for sid in EPG_SOURCES:
        try:
            out.append(await refresh_epg(sid))
        except Exception as exc:
            logger.exception("EPG refresh %s failed", sid)
            out.append({"ok": False, "epg_source": sid, "error": str(exc)})
    return out


# ── Recording (ffmpeg subprocess) ───────────────────────────────────


import os as _os

IPTV_DOWNLOAD_DIR = _os.environ.get("DOWNLOADS_DIR", "/downloads")


async def _record_worker(
    job_id: int,
    channel_id: str,
    channel_name: str,
    url: str,
    duration_min: int,
    out_path: str,
) -> None:
    """ffmpeg subprocess body — updates the iptv_recordings row as it
    transitions through queued → recording → finished/failed."""
    started = _iso_now()
    async with aiosqlite.connect(db.DB_PATH) as conn:
        await conn.execute(
            "UPDATE iptv_recordings SET status='recording', started_at=? WHERE id=?",
            (started, job_id),
        )
        await conn.commit()
    # -c copy keeps the original codec (no re-encode) → ~6 Mbps HLS for ~1080p.
    # `-t` is a hard time-based stop; ffmpeg will exit cleanly with a finalized
    # mp4/ts. We use .ts to avoid mp4-moov-atom-at-end problems on hard kill.
    args = [
        "ffmpeg", "-hide_banner", "-loglevel", "warning",
        "-rw_timeout", "10000000",   # 10s read timeout
        "-reconnect", "1",
        "-reconnect_streamed", "1",
        "-reconnect_delay_max", "5",
        "-i", url,
        "-c", "copy",
        "-t", str(duration_min * 60),
        "-y",
        out_path,
    ]
    logger.info("iptv recording job=%d  %s → %s (%dm)", job_id, channel_name, out_path, duration_min)
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    err_buf: list[bytes] = []
    try:
        async def _drain():
            assert proc.stderr is not None
            while True:
                chunk = await proc.stderr.read(4096)
                if not chunk: break
                err_buf.append(chunk)
        await asyncio.gather(_drain(), proc.wait())
    except Exception:
        logger.exception("iptv recording worker crashed")
    finally:
        rc = proc.returncode if proc.returncode is not None else -1
        finished = _iso_now()
        err_msg = (b"".join(err_buf[-4096:]).decode("utf-8", "replace") or "").strip()
        # ffmpeg exits 0 on graceful stop AND on -t timeout. Anything else
        # is a real failure (network died, codec error, etc.).
        status = "finished" if rc == 0 else "failed"
        async with aiosqlite.connect(db.DB_PATH) as conn:
            await conn.execute(
                "UPDATE iptv_recordings SET status=?, finished_at=?, error=? WHERE id=?",
                (status, finished, err_msg[:500] if status == "failed" else None, job_id),
            )
            await conn.commit()
        logger.info("iptv recording job=%d → %s (rc=%d, %d bytes stderr)", job_id, status, rc, sum(len(c) for c in err_buf))


async def start_iptv_recording(
    channel_id: str,
    duration_min: int,
    download_dir: str | None = None,
) -> dict:
    """Queue a recording. Returns the new iptv_recordings row id +
    target path. Caller doesn't wait; the worker runs in background."""
    duration_min = max(1, min(int(duration_min or 5), 240))
    ch = await get_channel(channel_id)
    if not ch or not ch.url:
        raise KeyError(f"channel {channel_id} not found or has no URL")
    safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", ch.name)[:60].strip("._-") or "iptv"
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    base = download_dir or IPTV_DOWNLOAD_DIR
    out_dir = _os.path.join(base, "iptv")
    _os.makedirs(out_dir, exist_ok=True)
    out_path = _os.path.join(out_dir, f"{safe_name}_{stamp}.ts")
    job_uuid = stamp + "-" + re.sub(r"[^a-z0-9]+", "-", channel_id.lower())[:32].strip("-")
    requested_at = _iso_now()
    async with aiosqlite.connect(db.DB_PATH) as conn:
        cur = await conn.execute("""
            INSERT INTO iptv_recordings (channel_id, job_id, duration_min, requested_at, status, output_path)
            VALUES (?, ?, ?, ?, 'queued', ?)
        """, (channel_id, job_uuid, duration_min, requested_at, out_path))
        await conn.commit()
        row_id = cur.lastrowid
    asyncio.create_task(_record_worker(row_id, channel_id, ch.name, ch.url, duration_min, out_path))
    return {
        "ok": True,
        "id": row_id,
        "job_id": job_uuid,
        "channel_id": channel_id,
        "channel_name": ch.name,
        "duration_min": duration_min,
        "output_path": out_path,
        "status": "queued",
    }


async def list_iptv_recordings(limit: int = 50) -> list[dict]:
    async with aiosqlite.connect(db.DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute("""
            SELECT * FROM iptv_recordings
             ORDER BY id DESC
             LIMIT ?
        """, (int(limit),))
        rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def get_now_next(tvg_id: str, lookahead_count: int = 3) -> list[dict]:
    """Return the currently-airing + next N programmes for a tvg_id.
    Caller passes the raw tvg-id (no source prefix); we also try a few
    common variants (with/without `@SD`, `@HD`) since iptv-org country
    feeds use different suffixes than the global JSON."""
    from datetime import datetime, timezone
    now_iso = datetime.now(timezone.utc).isoformat()
    # Try a few key variants in order. First hit wins.
    variants = [tvg_id]
    if "@" in tvg_id:
        variants.append(tvg_id.split("@", 1)[0])
    else:
        # iptv-org global → epgshare01 sometimes uses `.cc@SD` suffix
        variants.append(f"{tvg_id}@SD")
    async with aiosqlite.connect(db.DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        for v in variants:
            cur = await conn.execute("""
                SELECT * FROM iptv_programmes
                 WHERE tvg_id = ? AND end_utc >= ?
                 ORDER BY start_utc ASC
                 LIMIT ?
            """, (v, now_iso, int(lookahead_count) + 1))
            rows = await cur.fetchall()
            if rows:
                return [dict(r) for r in rows]
    return []


async def refresh_iptv_org_country(
    cc: str,
    timeout_s: float = 20.0,
) -> dict:
    """Fetch iptv-org/countries/{cc}.m3u and upsert with source=`iptv-org-{cc}`.

    These are CURATED per-country subsets — different URLs from the
    global JSON, often more reliable. Country code is normalised to
    lowercase for the URL and uppercase for the country column.
    """
    cc = (cc or "").strip().lower()
    if len(cc) != 2 or not cc.isalpha():
        raise ValueError(f"invalid country code {cc!r} — expected ISO 3166 alpha-2")
    url = f"{IPTV_ORG_COUNTRY_BASE}/{cc}.m3u"
    source_id = _country_source_id(cc)

    t0 = time.time()
    async with httpx.AsyncClient(timeout=timeout_s, follow_redirects=True) as client:
        r = await client.get(url)
    if r.status_code == 404:
        raise KeyError(f"iptv-org has no slice for country {cc!r}")
    r.raise_for_status()
    items = parse_m3u(r.text)

    now_iso = _iso_now()
    upserted = 0
    skipped = 0
    cc_upper = cc.upper()

    async with aiosqlite.connect(db.DB_PATH) as conn:
        for it in items:
            url_v = it.get("url") or ""
            name = (it.get("name") or "").strip()
            if not url_v or not name or len(url_v) > _MAX_URL:
                skipped += 1
                continue
            name = name[:_MAX_NAME]
            tvg_id = it.get("tvg_id") or ""
            # iptv-org country tvg-ids look like "CNA.sg@SD" — strip the
            # quality suffix to get the canonical id.
            base_id = tvg_id.split("@", 1)[0] if tvg_id else ""
            slug = base_id or re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
            row_id = f"{source_id}:{slug}"
            logo = it.get("logo") or None
            group = (it.get("group") or "").strip().lower() or None
            await conn.execute("""
                INSERT INTO iptv_channels (
                    id, name, country, languages, categories, url, logo,
                    is_nsfw, alive, status, last_check_at, last_error, last_seen_at, source
                ) VALUES (?, ?, ?, NULL, ?, ?, ?, 0, NULL, 'unprobed', NULL, NULL, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    name = excluded.name,
                    country = excluded.country,
                    categories = excluded.categories,
                    url = excluded.url,
                    logo = excluded.logo,
                    last_seen_at = excluded.last_seen_at,
                    source = excluded.source
            """, (row_id, name, cc_upper, group, url_v, logo, now_iso, source_id))
            upserted += 1
        await conn.commit()

    return {
        "ok": True,
        "source": source_id,
        "country": cc_upper,
        "channels_fetched": len(items),
        "upserted": upserted,
        "skipped": skipped,
        "duration_ms": int((time.time() - t0) * 1000),
    }


async def refresh_all_sources() -> list[dict]:
    """Run every registered source's refresh, plus the SG/MY/ID quick
    country slices. Returns per-source summaries."""
    out: list[dict] = []
    try:
        out.append(await refresh_from_iptv_org())
    except Exception as exc:
        out.append({"ok": False, "source": "iptv-org", "error": str(exc)})
    for sid, meta in SOURCES.items():
        if meta["kind"] != "m3u":
            continue
        try:
            out.append(await refresh_from_m3u(sid))
        except Exception as exc:
            out.append({"ok": False, "source": sid, "error": str(exc)})
    for cc in IPTV_ORG_COUNTRY_QUICK:
        try:
            out.append(await refresh_iptv_org_country(cc))
        except Exception as exc:
            out.append({"ok": False, "source": _country_source_id(cc), "error": str(exc)})
    return out


async def source_counts() -> dict[str, int]:
    """Per-source channel counts — drives the UI source-filter chips."""
    async with aiosqlite.connect(db.DB_PATH) as conn:
        cur = await conn.execute(
            "SELECT source, COUNT(*) FROM iptv_channels GROUP BY source"
        )
        rows = await cur.fetchall()
    return {r[0]: int(r[1]) for r in rows}


# ── Background probe-all sweep ──────────────────────────────────────


_probe_all_state: dict = {
    "running": False,
    "started_at": None,
    "total": 0,
    "checked": 0,
    "alive": 0,
    "dead": 0,
    "last_channel": "",
    "scope": "",
}


def probe_all_status() -> dict:
    return dict(_probe_all_state)


# Channels whose last_check_at is within this window are considered
# "fresh" and skipped by start_probe_all() unless force_recheck=True.
# Dead and unprobed channels are always re-probed (status filter inside
# the skip predicate). 6h = "manual sweep around once a day catches all
# stale rows but doesn't redo today's work twice".
PROBE_FRESH_WINDOW_HOURS = 6
# DB writes are batched through a single writer task — sqlite serialises
# writes anyway, but batching cuts commit() overhead by ~50x at scale
# (one fsync per batch instead of one per probe).
PROBE_DB_BATCH_SIZE = 50


def _is_probe_fresh(channel: "IptvChannel", window_hours: int) -> bool:
    """A channel's status is "fresh" if it was alive-probed within the
    window. Dead/unprobed channels are never fresh — we want aggressive
    rechecks for those (a dead stream may have come back)."""
    if channel.status != "alive" or not channel.last_check_at:
        return False
    try:
        from datetime import datetime, timezone, timedelta
        last = datetime.fromisoformat(channel.last_check_at)
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - last) < timedelta(hours=window_hours)
    except Exception:
        return False


async def _probe_db_writer(queue: asyncio.Queue) -> int:
    """Consumes probe results from the queue and batch-writes them.
    Exits when it sees the sentinel None. Returns total rows written."""
    written = 0
    batch: list[tuple] = []
    async with aiosqlite.connect(db.DB_PATH) as conn:
        while True:
            try:
                item = await asyncio.wait_for(queue.get(), timeout=2.0)
            except asyncio.TimeoutError:
                item = "FLUSH"   # sentinel for "no new items, just flush"
            if item is None:    # caller signalled "done"
                if batch:
                    await conn.executemany(
                        "UPDATE iptv_channels SET status=?, last_check_at=?, "
                        "last_error=?, alive=? WHERE id=?", batch)
                    await conn.commit()
                    written += len(batch)
                return written
            if item != "FLUSH":
                batch.append(item)
            if len(batch) >= PROBE_DB_BATCH_SIZE or item == "FLUSH":
                if batch:
                    await conn.executemany(
                        "UPDATE iptv_channels SET status=?, last_check_at=?, "
                        "last_error=?, alive=? WHERE id=?", batch)
                    await conn.commit()
                    written += len(batch)
                    batch.clear()


async def _probe_all_worker(
    source: str | None,
    country: str | None,
    concurrency: int,
    timeout_s: float,
    force_recheck: bool,
    fresh_window_hours: int,
) -> None:
    """Long-running task body. Pulls channel ids in scope, probes each
    via _probe_url_only(), feeds results into a single DB writer task
    that batches UPDATEs. Skips channels with status='alive' inside the
    freshness window (unless force_recheck=True)."""
    try:
        all_chans = await list_channels(
            source=source, country=country, limit=20000,
        )
        if not force_recheck:
            chans = [c for c in all_chans
                     if not _is_probe_fresh(c, fresh_window_hours)]
            skipped = len(all_chans) - len(chans)
        else:
            chans = all_chans
            skipped = 0

        _probe_all_state.update({
            "running": True,
            "started_at": _iso_now(),
            "total": len(chans),
            "checked": 0,
            "alive": 0,
            "dead": 0,
            "skipped_fresh": skipped,
            "last_channel": "",
            "scope": f"source={source or 'all'} country={country or 'all'} force={force_recheck}",
        })
        if not chans:
            return

        sem = asyncio.Semaphore(concurrency)
        write_queue: asyncio.Queue = asyncio.Queue(maxsize=concurrency * 4)
        writer_task = asyncio.create_task(_probe_db_writer(write_queue))

        # Single shared HTTP client → connection-pool reuse across probes.
        # Without this every probe opens its own TCP/TLS handshake.
        limits = httpx.Limits(max_connections=concurrency * 2,
                              max_keepalive_connections=concurrency)
        shared_client = httpx.AsyncClient(
            timeout=timeout_s, follow_redirects=True, limits=limits,
        )

        async def _one(cid: str, curl: str, cname: str):
            async with sem:
                try:
                    status, err = await _probe_url_only(
                        curl, timeout_s=timeout_s, client=shared_client,
                    )
                except Exception as exc:
                    status, err = "dead", str(exc)[:300]
                _probe_all_state["last_channel"] = cname
                if status == "alive":
                    _probe_all_state["alive"] += 1
                else:
                    _probe_all_state["dead"] += 1
                _probe_all_state["checked"] += 1
                await write_queue.put(
                    (status, _iso_now(), err,
                     1 if status == "alive" else 0, cid)
                )

        try:
            await asyncio.gather(*(
                _one(c.id, c.url or "", c.name) for c in chans if c.url
            ))
        finally:
            await shared_client.aclose()
            # signal writer to flush + exit
            await write_queue.put(None)
            await writer_task
    except Exception:
        logger.exception("probe_all worker crashed")
    finally:
        _probe_all_state["running"] = False


def start_probe_all(
    source: str | None = None,
    country: str | None = None,
    concurrency: int = 32,
    timeout_s: float = 6.0,
    force_recheck: bool = False,
    fresh_window_hours: int = PROBE_FRESH_WINDOW_HOURS,
) -> dict:
    """Kick off a background sweep. Idempotent — refuses to start a new
    sweep if one is already running (returns the current status).

    force_recheck=False (default) skips channels whose status='alive' was
    set within the last `fresh_window_hours` (default 6h). Dead +
    unprobed channels are always re-probed regardless."""
    if _probe_all_state.get("running"):
        return {"already_running": True, **probe_all_status()}
    asyncio.create_task(_probe_all_worker(
        source, country, concurrency, timeout_s, force_recheck, fresh_window_hours,
    ))
    return {
        "started": True,
        "scope": f"source={source} country={country}",
        "concurrency": concurrency, "timeout_s": timeout_s,
        "force_recheck": force_recheck,
        "fresh_window_hours": fresh_window_hours,
    }


# ── Channel listing / lookup ───────────────────────────────────────


async def list_channels(
    country: str | None = None,
    status: str | None = None,
    category: str | None = None,
    source: str | None = None,
    q: str | None = None,
    limit: int = 200,
) -> list[IptvChannel]:
    where = []
    params: list = []
    if country:
        where.append("country = ?")
        params.append(country.upper())
    if status:
        where.append("status = ?")
        params.append(status)
    if category:
        where.append("categories LIKE ?")
        params.append(f"%{category.lower()}%")
    if source:
        where.append("source = ?")
        params.append(source)
    if q:
        # SQL-level LIKE so the filter runs BEFORE LIMIT — otherwise the
        # exact-id lookup on the play page misses anything outside the
        # first `limit` rows ordered by (country, name).
        needle = q.strip().lower()
        if needle:
            where.append("(LOWER(name) LIKE ? OR LOWER(id) LIKE ?)")
            params.append(f"%{needle}%")
            params.append(f"%{needle}%")
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    sql = f"""
        SELECT * FROM iptv_channels
        {where_sql}
        ORDER BY country, name
        LIMIT ?
    """
    params.append(int(limit))
    async with aiosqlite.connect(db.DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute(sql, params)
        rows = await cur.fetchall()
    return [IptvChannel.from_row(r) for r in rows]


async def get_channel(channel_id: str) -> IptvChannel | None:
    async with aiosqlite.connect(db.DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute(
            "SELECT * FROM iptv_channels WHERE id = ?", (channel_id,),
        )
        row = await cur.fetchone()
    return IptvChannel.from_row(row) if row else None


# ── Probe (HEAD + first-segment fetch) ─────────────────────────────


async def _probe_url_only(
    url: str,
    timeout_s: float = 7.0,
    client: httpx.AsyncClient | None = None,
) -> tuple[str, str | None]:
    """Pure I/O probe — no DB write. Returns (status, error_message).
    Optionally takes a shared client so the caller can reuse the connection
    pool across many probes (probe_all worker does this)."""
    own_client = client is None
    if own_client:
        client = httpx.AsyncClient(timeout=timeout_s, follow_redirects=True)
    status = "dead"
    err: str | None = None
    try:
        # HEAD first — some HLS servers don't support HEAD, fall through to GET
        r = await client.head(url)
        if r.status_code >= 400:
            async with client.stream("GET", url) as gr:
                if gr.status_code >= 400:
                    raise RuntimeError(f"HTTP {gr.status_code}")
                n = 0
                async for chunk in gr.aiter_bytes(chunk_size=4096):
                    n += len(chunk)
                    if n >= 16_384:
                        break
                if n == 0:
                    raise RuntimeError("empty body")
        status = "alive"
    except Exception as exc:
        err = str(exc)[:300]
    finally:
        if own_client:
            await client.aclose()
    return status, err


async def probe_channel(channel_id: str, timeout_s: float = 7.0) -> IptvChannel:
    """HEAD the channel's M3U8 URL, then GET the first ~16KB to confirm
    the stream is actually responding. Persists the result on the row.

    Doesn't validate codec/content — just `it's reachable AND returns
    some bytes`. Good enough as a recording-readiness signal."""
    ch = await get_channel(channel_id)
    if ch is None or not ch.url:
        raise KeyError(f"channel {channel_id} not found or has no URL")
    status, err = await _probe_url_only(ch.url, timeout_s=timeout_s)
    now = _iso_now()
    async with aiosqlite.connect(db.DB_PATH) as conn:
        await conn.execute(
            """UPDATE iptv_channels
                   SET status = ?, last_check_at = ?, last_error = ?,
                       alive = ?
                 WHERE id = ?""",
            (status, now, err, 1 if status == "alive" else 0, channel_id),
        )
        await conn.commit()
    refreshed = await get_channel(channel_id)
    assert refreshed is not None
    return refreshed


async def probe_many(
    channel_ids: Iterable[str],
    concurrency: int = 8,
    timeout_s: float = 7.0,
) -> list[IptvChannel]:
    """Probe channels in parallel — bounded by `concurrency`."""
    sem = asyncio.Semaphore(concurrency)
    async def _one(cid: str) -> IptvChannel | None:
        async with sem:
            try:
                return await probe_channel(cid, timeout_s=timeout_s)
            except Exception as exc:
                logger.warning("probe %s failed: %s", cid, exc)
                return None
    results = await asyncio.gather(*(_one(cid) for cid in channel_ids))
    return [r for r in results if r is not None]


# ── Helpers ─────────────────────────────────────────────────────────


def _iso_now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()
