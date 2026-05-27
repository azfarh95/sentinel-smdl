"""IPTV Mini App — Netflix-style browser + watch/record endpoints.

Mounted into the SMDL FastAPI app alongside miniapp.py / sticker_routes.py.
Shares the same `_verify()` auth gate so owner-only access matches the
rest of the app.

Routes
------
HTML
    GET  /iptv                         — top-level browser (country chips + grid)
    GET  /iptv/play/{channel_id}       — interstitial page that hands off to VLC

JSON (all owner-gated)
    POST /api/iptv/refresh             — pull channels.json+streams.json from iptv-org
    GET  /api/iptv/countries           — distinct country codes + counts
    GET  /api/iptv/categories          — distinct categories + counts
    GET  /api/iptv/channels            — list with country/category/status/search filters
    POST /api/iptv/channels/{id}/probe — HEAD + first-segment fetch
    POST /api/iptv/channels/{id}/record — kick off ffmpeg-via-yt-dlp recording

The "open in VLC" handoff isn't a redirect-to-vlc:// (those URI schemes are
inconsistent across platforms). Instead the play page surfaces three
actions: native player launch via `tg.openLink` (system handles the .m3u8
MIME → VLC if installed), Copy URL, and an inline `<video>` fallback for
WebKit-based platforms that grok HLS natively.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import httpx

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, Response
from pydantic import BaseModel

import aiosqlite

from . import database as _db
from . import iptv as _iptv
from . import iptv_dedup as _dedup
from . import miniapp as _mini   # reuse _verify + require_scope

logger = logging.getLogger(__name__)

router = APIRouter()


# All IPTV routes require the same scope. Owner cookie + initData both
# carry the wildcard '*' scope, so this is a no-op for owner access;
# beta users without 'smdl.iptv' in their scopes_b64 get HTTP 403 here.
# Per spec docs/auth-perms-v2.md §6.
async def _verify_iptv(request: Request) -> dict:
    payload = await _mini._verify(request)
    _mini.require_scope(payload, "smdl.iptv")
    return payload


# ── JSON ────────────────────────────────────────────────────────────


class RefreshBody(BaseModel):
    country: str | None = None
    include_nsfw: bool = False
    source: str | None = None  # 'iptv-org' | 'free-tv' | 'mjh-all' | None=all


@router.post("/api/iptv/refresh")
async def iptv_refresh(body: RefreshBody, request: Request):
    """Refresh one source (if body.source is set) or all sources (if not).
    Returns a per-source summary list."""
    await _verify_iptv(request)
    summaries: list[dict] = []
    try:
        if body.source is None:
            summaries = await _iptv.refresh_all_sources()
        elif body.source == "iptv-org":
            summaries = [await _iptv.refresh_from_iptv_org(
                country=body.country, include_nsfw=body.include_nsfw,
            )]
        elif body.source == "youtube-live":
            summaries = [await _iptv.refresh_from_youtube_yaml()]
        else:
            summaries = [await _iptv.refresh_from_m3u(body.source)]
    except KeyError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        logger.exception("iptv refresh failed")
        raise HTTPException(status_code=502, detail=f"fetch failed: {exc}")
    return {"ok": True, "summaries": summaries}


def _dynamic_source_name(sid: str) -> str:
    """Friendly label for a populated source that isn't in the static
    SOURCES dict. Currently covers `iptv-org-{cc}` country slices and
    `mjh-{bucket}` sub-sources created by the mjh-all fan-out."""
    if sid.startswith("iptv-org-"):
        cc = sid.split("-", 2)[-1]
        return f"iptv-org · {cc.upper()} curated"
    if sid == "mjh-radio":     return "i.mjh.nz · Radio"
    if sid == "mjh-sky-fast":  return "i.mjh.nz · Sky NZ FAST"
    if sid == "mjh-au":        return "i.mjh.nz · Australia"
    if sid == "mjh-nz":        return "i.mjh.nz · New Zealand"
    if sid == "mjh-other":     return "i.mjh.nz · other"
    return sid


@router.get("/api/iptv/sources")
async def iptv_sources(request: Request):
    """List sources with at least one row. Static SOURCES entries with
    count=0 are skipped (e.g. mjh-all, which is fetch-only — its rows
    fan out to mjh-radio/au/nz/sky-fast/other). Anything not in static
    SOURCES gets a synthesised friendly name."""
    await _verify_iptv(request)
    counts = await _iptv.source_counts()
    out = []
    seen: set[str] = set()
    for sid, meta in _iptv.SOURCES.items():
        n = counts.get(sid, 0)
        if n == 0:
            continue   # fetch-only sources (mjh-all) — hide from filter chips
        out.append({
            "id":    sid,
            "name":  meta["name"],
            "kind":  meta["kind"],
            "count": n,
        })
        seen.add(sid)
    for sid, n in counts.items():
        if sid in seen:
            continue
        out.append({
            "id":    sid,
            "name":  _dynamic_source_name(sid),
            "kind":  "m3u",
            "count": n,
        })
    out.sort(key=lambda s: -s["count"])
    return {"sources": out, "total": sum(counts.values()),
            "country_quick": _iptv.IPTV_ORG_COUNTRY_QUICK}


class RefreshCountryBody(BaseModel):
    country: str   # ISO 3166-1 alpha-2 (e.g. "SG", "MY", "ID")


@router.post("/api/iptv/refresh_country")
async def iptv_refresh_country(body: RefreshCountryBody, request: Request):
    """Refresh ONE iptv-org per-country slice (cheap, sub-second)."""
    await _verify_iptv(request)
    try:
        summary = await _iptv.refresh_iptv_org_country(body.country)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"no iptv-org slice for {body.country}")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        logger.exception("iptv-org country refresh failed")
        raise HTTPException(status_code=502, detail=f"fetch failed: {exc}")
    return summary


@router.get("/api/iptv/countries")
async def iptv_countries(request: Request):
    await _verify_iptv(request)
    async with aiosqlite.connect(_db.DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute("""
            SELECT country, COUNT(*) AS n
              FROM iptv_channels
             WHERE country IS NOT NULL
             GROUP BY country
             ORDER BY n DESC
        """)
        rows = await cur.fetchall()
    return {
        "countries": [
            {"code": r["country"], "count": int(r["n"])} for r in rows
        ],
    }


@router.get("/api/iptv/categories")
async def iptv_categories(request: Request):
    await _verify_iptv(request)
    # categories is a comma-joined column — explode in Python (sqlite doesn't
    # have STRING_SPLIT). With ~7k rows this is fine.
    async with aiosqlite.connect(_db.DB_PATH) as conn:
        cur = await conn.execute("SELECT categories FROM iptv_channels WHERE categories IS NOT NULL")
        rows = await cur.fetchall()
    counts: dict[str, int] = {}
    for (cats,) in rows:
        for c in (cats or "").split(","):
            c = c.strip().lower()
            if c:
                counts[c] = counts.get(c, 0) + 1
    out = sorted(counts.items(), key=lambda kv: -kv[1])
    return {"categories": [{"name": k, "count": v} for k, v in out]}


@router.get("/api/iptv/channels")
async def iptv_channels(
    request: Request,
    country: str | None = None,
    category: str | None = None,
    status: str | None = None,
    source: str | None = None,
    q: str | None = None,
    limit: int = 200,
):
    await _verify_iptv(request)
    chans = await _iptv.list_channels(
        country=country, status=status, category=category,
        source=source, q=q, limit=int(limit),
    )
    return {"channels": [c.to_dict() for c in chans]}


@router.get("/api/iptv/whereami")
async def iptv_whereami(request: Request):
    """Return the requester's effective country + IP, derived from
    Cloudflare's CF-IPCountry / CF-Connecting-IP headers when present.
    Falls back to the raw client.host when called directly (e.g. local
    LAN). Drives the per-channel "exit mismatch" warning."""
    await _verify_iptv(request)
    cf_country = request.headers.get("cf-ipcountry") or None
    cf_ip      = request.headers.get("cf-connecting-ip") or None
    client_ip  = request.client.host if request.client else None
    return {
        "country": cf_country,        # None means we couldn't detect
        "ip":      cf_ip or client_ip,
        "via_cf":  bool(cf_country),
    }


@router.get("/api/iptv/channels/{channel_id}")
async def iptv_channel_get(channel_id: str, request: Request):
    """Direct lookup by primary key — the play page uses this so it
    doesn't fight the (country, name) ORDER BY + LIMIT clause."""
    await _verify_iptv(request)
    ch = await _iptv.get_channel(channel_id)
    if not ch:
        raise HTTPException(status_code=404, detail="channel not found")
    return ch.to_dict()


# ── iptv-aggregator-v2 Phase 1 endpoints ────────────────────────────


@router.get("/api/iptv/v2/channels")
async def iptv_v2_channels(
    request: Request,
    country: str | None = None,
    category: str | None = None,
    source: str | None = None,
    is_curated: int | None = None,
    q: str | None = None,
    limit: int = 300,
):
    """List LOGICAL channels (deduplicated). The new grid endpoint.
    Each row aggregates source counts + alive-count via the SQL view
    `v_channels_with_status`. Phase 1 ships this alongside the legacy
    /api/iptv/channels; Phase 2 flips the UI."""
    await _verify_iptv(request)
    where = []
    params: list = []
    if country:
        where.append("country = ?")
        params.append(country.upper())
    if category:
        where.append("categories LIKE ?")
        params.append(f"%{category.lower()}%")
    if is_curated is not None:
        where.append("is_curated = ?")
        params.append(1 if is_curated else 0)
    if source:
        # "Show logical channels where AT LEAST ONE source has source=X".
        # EXISTS subquery against iptv_channels — uses idx_iptv_source +
        # idx_iptv_channel_id so it stays cheap even on full catalogue.
        where.append(
            "EXISTS (SELECT 1 FROM iptv_channels cs "
            "         WHERE cs.channel_id = v_channels_with_status.id "
            "           AND cs.source = ?)"
        )
        params.append(source)
    if q:
        needle = q.strip().lower()
        if needle:
            where.append("(LOWER(name) LIKE ? OR LOWER(id) LIKE ?)")
            params.append(f"%{needle}%")
            params.append(f"%{needle}%")
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    sql = f"""
        SELECT id, name, country, languages, categories, logo, aliases,
               is_curated, source_count, alive_count_srcs, last_alive_at
          FROM v_channels_with_status
        {where_sql}
         ORDER BY is_curated DESC, name ASC
         LIMIT ?
    """
    params.append(int(limit))
    import aiosqlite
    async with aiosqlite.connect(_db.DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute(sql, params)
        rows = await cur.fetchall()
    out = []
    for r in rows:
        d = dict(r)
        # Normalise: rename SQL alias_count_srcs → alive_count for the
        # frontend; parse aliases JSON.
        d["alive_count"] = int(d.pop("alive_count_srcs") or 0)
        d["source_count"] = int(d.get("source_count") or 0)
        try:
            d["aliases"] = json.loads(d.get("aliases") or "[]")
        except Exception:
            d["aliases"] = []
        d["categories"] = [c for c in (d.get("categories") or "").split(",") if c]
        out.append(d)
    return {"channels": out, "total_returned": len(out)}


@router.get("/api/iptv/channels/{channel_id}/sources")
async def iptv_channel_sources(channel_id: str, request: Request):
    """List all source rows backing a logical channel — for the source
    picker dropdown on the play page (Phase 2 UI)."""
    await _verify_iptv(request)
    sources = await _dedup.list_sources_for_channel(channel_id)
    if not sources:
        raise HTTPException(status_code=404, detail="no sources for channel")
    return {"channel_id": channel_id, "sources": sources}


@router.get("/api/iptv/v2/channels/{channel_id}")
async def iptv_v2_channel_detail(channel_id: str, request: Request):
    """Combined channel detail — logical channel metadata + all sources
    in one call. Drives the new play page in Phase 2."""
    await _verify_iptv(request)
    import aiosqlite
    async with aiosqlite.connect(_db.DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute("""
            SELECT id, name, country, languages, categories, logo, aliases,
                   is_curated, updated_at
              FROM logical_channels
             WHERE id = ?
        """, (channel_id,))
        ch_row = await cur.fetchone()
    if not ch_row:
        raise HTTPException(status_code=404, detail="channel not found")
    sources = await _dedup.list_sources_for_channel(channel_id)
    ch = dict(ch_row)
    try:
        ch["aliases"] = json.loads(ch.get("aliases") or "[]")
    except Exception:
        ch["aliases"] = []
    ch["categories"] = [c for c in (ch.get("categories") or "").split(",") if c]
    return {"channel": ch, "sources": sources}


class ResolveChannelsBody(BaseModel):
    source_ids: list[str]


@router.post("/api/iptv/sources/resolve_channels")
async def iptv_resolve_channels(body: ResolveChannelsBody, request: Request):
    """Translate a list of legacy source-row IDs (e.g. 'iptv-org:CNA.sg')
    to the new logical channel IDs they belong to. Used by the Phase 2
    UI on first load to migrate localStorage favorites from the old
    source-prefixed scheme to the new logical-channel scheme."""
    await _verify_iptv(request)
    if not body.source_ids:
        return {"mapping": {}}
    import aiosqlite
    async with aiosqlite.connect(_db.DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        placeholders = ",".join("?" * len(body.source_ids))
        cur = await conn.execute(
            f"SELECT id, channel_id FROM iptv_channels WHERE id IN ({placeholders})",
            body.source_ids,
        )
        rows = await cur.fetchall()
    mapping = {r["id"]: r["channel_id"] for r in rows if r["channel_id"]}
    return {"mapping": mapping, "input_count": len(body.source_ids),
            "resolved_count": len(mapping)}


@router.get("/api/iptv/channels/{channel_id}/play")
async def iptv_channel_play(channel_id: str, request: Request):
    """Server-side "best alive source" picker. Returns the chosen
    source URL + a list of alternates so the client can failover."""
    await _verify_iptv(request)
    pick = await _dedup.pick_best_source(channel_id)
    if not pick:
        raise HTTPException(status_code=404, detail="no playable source for channel")
    return {"channel_id": channel_id, **pick}


@router.post("/api/iptv/sources/{source_id}/report_failure")
async def iptv_report_source_failure(source_id: str, request: Request):
    """Client-reported failure — demote a source after the player
    couldn't play it. Mark dead so the next /play call skips it; the
    auto-probe loop re-checks within 12 h."""
    await _verify_iptv(request)
    ch = await _iptv.get_channel(source_id)
    if not ch:
        raise HTTPException(status_code=404, detail="source not found")
    import aiosqlite
    async with aiosqlite.connect(_db.DB_PATH) as conn:
        await conn.execute(
            "UPDATE iptv_channels SET status = 'dead', "
            "last_error = 'client-reported failure', last_check_at = ? "
            "WHERE id = ?",
            (_iptv._iso_now(), source_id),
        )
        await conn.commit()
    return {"ok": True, "source_id": source_id, "status": "dead"}


class CurateBody(BaseModel):
    name:       str | None = None        # override (default = current name)
    country:    str | None = None        # override (default = current country)
    categories: list[str] | None = None  # override (default = current cats)
    aliases:    list[str] | None = None  # extra aliases to add
    extra_source_ids: list[str] | None = None  # add sources beyond current
    priority_overrides: dict[str, int] | None = None


@router.post("/api/iptv/channels/{channel_id}/curate")
async def iptv_curate_channel(channel_id: str, body: CurateBody, request: Request):
    """Add the logical channel to data/channel_aliases.yaml (curated
    overrides). Owner-only. Idempotent — re-curating an already-curated
    channel updates its entry. Body is mostly optional: if omitted,
    we infer everything from the current logical_channel + its sources.

    After the YAML mutation, dedup runs synchronously so the new
    curated state takes effect immediately (no container restart)."""
    payload = await _verify_iptv(request)
    _mini.require_scope(payload, "*")   # owner-only — beta users can't edit YAML

    # Look up the current logical channel + its sources
    import aiosqlite
    async with aiosqlite.connect(_db.DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute(
            "SELECT id, name, country, categories, logo, aliases, is_curated "
            "  FROM logical_channels WHERE id = ?", (channel_id,))
        ch = await cur.fetchone()
    if not ch:
        raise HTTPException(status_code=404, detail="channel not found")
    sources = await _dedup.list_sources_for_channel(channel_id)
    if not sources:
        raise HTTPException(status_code=400, detail="channel has no sources to pin")

    # Build the YAML entry — body wins over current state where set.
    cur_aliases = []
    try:
        cur_aliases = json.loads(ch["aliases"] or "[]")
    except Exception:
        pass
    name       = (body.name or ch["name"] or channel_id).strip()
    country    = ((body.country or ch["country"] or "") or "").upper() or None
    categories = body.categories
    if categories is None:
        categories = [c for c in (ch["categories"] or "").split(",") if c]
    new_aliases = list({a for a in (cur_aliases + (body.aliases or [])) if a})
    if name not in new_aliases:
        new_aliases.insert(0, name)
    src_ids = [s["id"] for s in sources]
    for sid in (body.extra_source_ids or []):
        if sid not in src_ids:
            src_ids.append(sid)
    prio = body.priority_overrides or {}

    # Read existing YAML — preserve commenting + key ordering by using
    # ruamel.yaml if available, else PyYAML (which strips comments). We
    # only commit to PyYAML since it's already a deployed dep.
    import yaml
    from pathlib import Path
    yaml_path = Path("/app/data/channel_aliases.yaml")
    try:
        text = yaml_path.read_text(encoding="utf-8")
        data = yaml.safe_load(text) or {"channels": {}}
    except FileNotFoundError:
        data = {"channels": {}}
    if "channels" not in data:
        data["channels"] = {}
    entry: dict = {
        "name":    name,
        "country": country,
        "aliases": new_aliases,
        "categories": categories or [],
        "sources": src_ids,
    }
    if prio:
        entry["priority_overrides"] = {k: int(v) for k, v in prio.items()}
    data["channels"][channel_id] = entry

    # Write back. The PyYAML default order isn't guaranteed but
    # `sort_keys=False` preserves dict insertion order (Python 3.7+).
    new_text = yaml.safe_dump(data, sort_keys=False, allow_unicode=True,
                               default_flow_style=False)
    yaml_path.write_text(new_text, encoding="utf-8")

    # Re-run dedup so the curated status takes effect immediately.
    summary = await _dedup.run_dedup()
    return {
        "ok": True,
        "channel_id": channel_id,
        "curated_entry": entry,
        "dedup_summary": summary,
    }


@router.post("/api/iptv/dedup/run")
async def iptv_dedup_run(request: Request):
    """Manually re-run the dedup pipeline. Owner-only — beta users
    shouldn't trigger schema-mutating jobs. Cheap (~1 sec on 12k
    rows) but not idempotent against in-flight refreshes; serialise
    your own dedup + refresh calls."""
    payload = await _verify_iptv(request)
    # Extra check: dedup is sensitive — only the owner runs it.
    _mini.require_scope(payload, "*")
    try:
        return await _dedup.run_dedup()
    except Exception as exc:
        logger.exception("dedup pipeline crashed")
        raise HTTPException(status_code=500, detail=f"dedup failed: {exc}")


@router.get("/api/iptv/channels/{channel_id}/resolve_url")
async def iptv_channel_resolve_url(channel_id: str, request: Request):
    """Return a freshly-resolved playable URL for the channel.

    For static-URL sources (iptv-org, free-tv, mjh, …) this just
    passes through the stored URL — cheap, ~1ms.

    For source='youtube-live' it invokes yt-dlp on the @handle page
    to get the current m3u8 manifest (YouTube live URLs are signed
    and rotate; can't store them statically). Results cached in-memory
    for 30 min so rapid taps don't fan out to multiple yt-dlp procs."""
    await _verify_iptv(request)
    ch = await _iptv.get_channel(channel_id)
    if not ch or not ch.url:
        raise HTTPException(status_code=404, detail="channel not found / no URL")
    if ch.source != "youtube-live":
        return {"url": ch.url, "resolved": False, "source": ch.source}
    from . import iptv_youtube
    try:
        url = await iptv_youtube.resolve_live_url(ch.url)
    except Exception as exc:
        logger.warning("youtube resolve %s failed: %s", channel_id, exc)
        raise HTTPException(status_code=502, detail=f"resolve failed: {exc}")
    return {"url": url, "resolved": True, "source": ch.source,
            "original": ch.url}


class ProbeAllBody(BaseModel):
    source: str | None = None
    country: str | None = None
    concurrency: int = 32
    timeout_s: float = 6.0
    force_recheck: bool = False     # set True to re-probe channels alive within the freshness window
    fresh_window_hours: int = 6


@router.post("/api/iptv/probe_all")
async def iptv_probe_all(body: ProbeAllBody, request: Request):
    """Kick off a background sweep that probes every channel in scope.
    Returns immediately; poll /api/iptv/probe_all/status for progress."""
    await _verify_iptv(request)
    return _iptv.start_probe_all(
        source=body.source, country=body.country,
        concurrency=max(1, min(int(body.concurrency or 32), 128)),
        timeout_s=float(body.timeout_s or 6.0),
        force_recheck=bool(body.force_recheck),
        fresh_window_hours=max(1, min(int(body.fresh_window_hours or 6), 168)),
    )


@router.get("/api/iptv/probe_all/status")
async def iptv_probe_all_status(request: Request):
    await _verify_iptv(request)
    return _iptv.probe_all_status()


@router.get("/api/iptv/channels/{channel_id}/epg")
async def iptv_channel_epg(channel_id: str, request: Request, n: int = 3):
    """Return now + next-N programmes for the channel.  Derives the
    tvg_id from the channel id by stripping the `<source>:` prefix."""
    await _verify_iptv(request)
    tvg = channel_id.split(":", 1)[-1] if ":" in channel_id else channel_id
    progs = await _iptv.get_now_next(tvg, lookahead_count=max(1, min(int(n or 3), 20)))
    return {"tvg_id": tvg, "programmes": progs}


class EpgRefreshBody(BaseModel):
    source: str | None = None   # None=all EPG sources


@router.post("/api/iptv/epg/refresh")
async def iptv_epg_refresh(body: EpgRefreshBody, request: Request):
    """Refresh one or all EPG feeds — separate from channel refresh
    because EPG fetches are heavier (multi-MB XMLTV gz)."""
    await _verify_iptv(request)
    try:
        if body.source is None:
            summaries = await _iptv.refresh_all_epg()
        else:
            summaries = [await _iptv.refresh_epg(body.source)]
    except KeyError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        logger.exception("EPG refresh failed")
        raise HTTPException(status_code=502, detail=f"EPG fetch failed: {exc}")
    return {"ok": True, "summaries": summaries}


@router.post("/api/iptv/channels/{channel_id}/probe")
async def iptv_probe(channel_id: str, request: Request):
    await _verify_iptv(request)
    try:
        ch = await _iptv.probe_channel(channel_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="channel not found")
    return ch.to_dict()


class RecordBody(BaseModel):
    duration_min: int = 5


@router.post("/api/iptv/channels/{channel_id}/record")
async def iptv_record(channel_id: str, body: RecordBody, request: Request):
    """Queue an ffmpeg recording of the channel. Returns immediately;
    job lands in iptv_recordings table + the file appears in
    /downloads/iptv/. Poll GET /api/iptv/recordings for status."""
    await _verify_iptv(request)
    try:
        result = await _iptv.start_iptv_recording(
            channel_id, duration_min=body.duration_min or 5,
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="channel not found / no URL")
    except Exception as exc:
        logger.exception("iptv recording start failed")
        raise HTTPException(status_code=500, detail=f"record failed: {exc}")
    return result


@router.get("/api/iptv/recordings")
async def iptv_recordings(request: Request, limit: int = 50):
    """List queued + in-progress + finished IPTV recordings."""
    await _verify_iptv(request)
    return {"recordings": await _iptv.list_iptv_recordings(limit=limit)}


# ── Enhancement pass 2026-05-27: logo cache + now-playing + play history
#    + scheduled DVR + M3U import + SG-curated ──────────────────────


LOGO_CACHE_DIR = Path(os.environ.get("IPTV_LOGO_CACHE_DIR", "/data/iptv_logos"))


_TILE_COLORS = [
    "#3390ec", "#34c759", "#ff9f0a", "#ff453a", "#5ac8fa", "#bf5af2",
    "#ffd60a", "#30d158", "#ff375f", "#64d2ff", "#a162e8", "#ff6b35",
]


def _letter_tile_svg(channel_name: str, size: int = 96) -> bytes:
    """Two-letter colored SVG tile. Used as logo fallback when no origin
    URL exists or its fetch failed. Inline SVG is browser-renderable
    in <img>, no Pillow dep required."""
    name = (channel_name or "?").strip()
    parts = [p for p in name.replace("-", " ").replace("_", " ").split() if p]
    initials = "".join(p[0] for p in parts[:2])[:2].upper() or "?"
    color_idx = sum(ord(c) for c in name) % len(_TILE_COLORS)
    color = _TILE_COLORS[color_idx]
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {size} {size}">'
        f'<rect width="{size}" height="{size}" rx="{size//6}" fill="{color}"/>'
        f'<text x="50%" y="52%" dy="0.36em" text-anchor="middle" '
        f'fill="white" font-family="-apple-system,system-ui,Arial,sans-serif" '
        f'font-size="{int(size*0.42)}" font-weight="700">{initials}</text>'
        f'</svg>'
    ).encode("utf-8")


def _safe_logo_filename(channel_id: str) -> str:
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in channel_id)[:64]


@router.get("/iptv/logo/{channel_id:path}")
async def iptv_logo(channel_id: str, request: Request):
    """Serve a channel logo from on-disk cache, fetching the origin URL
    on first miss. Falls back to a generated letter-tile SVG when there's
    no usable origin or the fetch fails. Same scope gate as the rest of
    /iptv. Browser sends the auth cookie via <img src>."""
    await _verify_iptv(request)
    LOGO_CACHE_DIR.mkdir(parents=True, exist_ok=True)

    safe = _safe_logo_filename(channel_id)
    cache_path = LOGO_CACHE_DIR / f"{safe}.bin"
    meta_path  = LOGO_CACHE_DIR / f"{safe}.mime"

    if cache_path.is_file() and meta_path.is_file():
        try:
            mime = (meta_path.read_text(encoding="utf-8").strip()
                    or "image/png")
            return FileResponse(str(cache_path), media_type=mime,
                                headers={"Cache-Control": "public, max-age=86400"})
        except Exception:
            pass

    origin_url = None
    channel_name = channel_id
    async with aiosqlite.connect(_db.DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute(
            "SELECT lc.name AS name, ic.logo AS logo "
            "FROM logical_channels lc "
            "LEFT JOIN iptv_channels ic ON ic.channel_id = lc.id "
            "WHERE lc.id = ? AND ic.logo IS NOT NULL AND ic.logo != '' "
            "LIMIT 1",
            (channel_id,),
        )
        row = await cur.fetchone()
        if row:
            origin_url, channel_name = row["logo"], row["name"]
        else:
            cur = await conn.execute(
                "SELECT name, logo FROM iptv_channels WHERE id = ? LIMIT 1",
                (channel_id,),
            )
            row = await cur.fetchone()
            if row:
                origin_url, channel_name = row["logo"], row["name"]

    if origin_url and origin_url.startswith(("http://", "https://")):
        try:
            async with httpx.AsyncClient(timeout=4.0, follow_redirects=True) as cli:
                r = await cli.get(origin_url)
            if r.status_code == 200 and len(r.content) > 100:
                mime = r.headers.get("content-type", "image/png").split(";")[0].strip()
                if mime.startswith("image/"):
                    cache_path.write_bytes(r.content)
                    meta_path.write_text(mime, encoding="utf-8")
                    return FileResponse(str(cache_path), media_type=mime,
                                        headers={"Cache-Control": "public, max-age=86400"})
        except Exception:
            pass

    svg = _letter_tile_svg(channel_name)
    cache_path.write_bytes(svg)
    meta_path.write_text("image/svg+xml", encoding="utf-8")
    return FileResponse(str(cache_path), media_type="image/svg+xml",
                        headers={"Cache-Control": "public, max-age=3600"})


@router.get("/api/iptv/now_playing")
async def iptv_now_playing(request: Request):
    """Map logical-channel-id → current programme. Driven by EPG
    (iptv_programmes). One SQL query, no N+1. Used by the grid to
    decorate each card with a "what's on now" line."""
    await _verify_iptv(request)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    out: dict[str, dict] = {}
    async with aiosqlite.connect(_db.DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        # Logical channels don't carry tvg_id directly; their source rows do
        # (the tvg_id is the suffix after `<source>:` in iptv_channels.id).
        # Strip the source prefix off via REPLACE() to JOIN on iptv_programmes.
        cur = await conn.execute(
            """
            SELECT lc.id AS channel_id,
                   ip.title AS title,
                   ip.end_utc AS end_utc,
                   ip.description AS description
              FROM logical_channels lc
              JOIN iptv_channels ic ON ic.channel_id = lc.id
              JOIN iptv_programmes ip
                ON ip.tvg_id = REPLACE(ic.id, ic.source || ':', '')
             WHERE ip.start_utc <= ?
               AND ip.end_utc > ?
             GROUP BY lc.id
            """,
            (now, now),
        )
        for r in await cur.fetchall():
            out[r["channel_id"]] = {
                "title":       r["title"],
                "end_utc":     r["end_utc"],
                "description": r["description"],
            }
    return {"now_playing": out, "count": len(out), "as_of": now}


# ── Play history (drives the "Last watched" row at top of grid) ──


async def _ensure_play_history_table():
    async with aiosqlite.connect(_db.DB_PATH) as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS iptv_play_history (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                channel_id  TEXT NOT NULL,
                played_at   TEXT NOT NULL,
                source_id   TEXT
            )
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_iptv_play_history_ts
              ON iptv_play_history(played_at DESC)
        """)
        await conn.commit()


class PlayLogBody(BaseModel):
    source_id: str | None = None


@router.post("/api/iptv/channels/{channel_id}/played")
async def iptv_log_play(channel_id: str, body: PlayLogBody, request: Request):
    """Lightweight beacon — frontend POSTs after a successful inline-play
    or external-player handoff. Drives the "Last watched" pinned row."""
    await _verify_iptv(request)
    await _ensure_play_history_table()
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    async with aiosqlite.connect(_db.DB_PATH) as conn:
        await conn.execute(
            "INSERT INTO iptv_play_history (channel_id, played_at, source_id) "
            "VALUES (?, ?, ?)",
            (channel_id, now, body.source_id),
        )
        await conn.commit()
    return {"ok": True, "played_at": now}


@router.get("/api/iptv/last_watched")
async def iptv_last_watched(request: Request, limit: int = 8):
    """Last N distinct channels played, newest first. Joined with
    logical_channels so the grid can render name + logo lookup key."""
    await _verify_iptv(request)
    await _ensure_play_history_table()
    limit = max(1, min(int(limit or 8), 24))
    async with aiosqlite.connect(_db.DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute(
            """
            SELECT ph.channel_id, MAX(ph.played_at) AS played_at,
                   COALESCE(lc.name, ic.name) AS name,
                   COALESCE(lc.country, ic.country) AS country
              FROM iptv_play_history ph
         LEFT JOIN logical_channels lc ON lc.id = ph.channel_id
         LEFT JOIN iptv_channels ic ON ic.id = ph.channel_id
             GROUP BY ph.channel_id
             ORDER BY MAX(ph.played_at) DESC
             LIMIT ?
            """,
            (limit,),
        )
        rows = await cur.fetchall()
    return {"items": [dict(r) for r in rows], "count": len(rows)}


# ── Scheduled DVR ───────────────────────────────────────────────


async def _ensure_scheduled_table():
    async with aiosqlite.connect(_db.DB_PATH) as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS iptv_scheduled (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                channel_id    TEXT NOT NULL,
                start_at      TEXT NOT NULL,
                duration_min  INTEGER NOT NULL,
                padding_pre   INTEGER NOT NULL DEFAULT 0,
                padding_post  INTEGER NOT NULL DEFAULT 0,
                programme     TEXT,
                status        TEXT NOT NULL DEFAULT 'pending',
                created_at    TEXT NOT NULL,
                triggered_at  TEXT,
                job_id        TEXT,
                error         TEXT
            )
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_iptv_sched_pending
              ON iptv_scheduled(status, start_at)
        """)
        await conn.commit()


class ScheduleBody(BaseModel):
    channel_id:    str
    start_at:      str            # ISO-8601 UTC ("2026-05-27T20:00:00Z")
    duration_min:  int            # minutes to record (excluding padding)
    padding_pre:   int = 0
    padding_post:  int = 0
    programme:     str | None = None


@router.post("/api/iptv/schedule")
async def iptv_schedule(body: ScheduleBody, request: Request):
    """Create a future recording job. The background tick (started in
    main.py's lifespan) wakes once a minute, kicks off any 'pending'
    rows whose effective start_at minus padding_pre is in the past."""
    await _verify_iptv(request)
    await _ensure_scheduled_table()
    if body.duration_min <= 0 or body.duration_min > 12 * 60:
        raise HTTPException(400, "duration_min must be 1..720")
    try:
        # Validate start_at parses; the column stores the raw input.
        datetime.strptime(body.start_at.rstrip("Z"), "%Y-%m-%dT%H:%M:%S")
    except Exception:
        raise HTTPException(400, "start_at must be ISO-8601 UTC like 2026-05-27T20:00:00Z")
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    async with aiosqlite.connect(_db.DB_PATH) as conn:
        cur = await conn.execute(
            "INSERT INTO iptv_scheduled "
            "(channel_id, start_at, duration_min, padding_pre, padding_post, "
            " programme, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)",
            (body.channel_id, body.start_at, body.duration_min,
             max(0, body.padding_pre), max(0, body.padding_post),
             body.programme, now),
        )
        await conn.commit()
        sched_id = cur.lastrowid
    return {"ok": True, "id": sched_id, "status": "pending"}


@router.get("/api/iptv/scheduled")
async def iptv_scheduled_list(request: Request, limit: int = 100):
    """List recently-created scheduled records, newest first.  Frontend
    uses this for the "Upcoming" section on the recordings page."""
    await _verify_iptv(request)
    await _ensure_scheduled_table()
    limit = max(1, min(int(limit or 100), 500))
    async with aiosqlite.connect(_db.DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute(
            "SELECT id, channel_id, start_at, duration_min, padding_pre, "
            "       padding_post, programme, status, created_at, "
            "       triggered_at, job_id, error "
            "FROM iptv_scheduled ORDER BY start_at DESC LIMIT ?",
            (limit,),
        )
        rows = await cur.fetchall()
    return {"items": [dict(r) for r in rows], "count": len(rows)}


@router.delete("/api/iptv/scheduled/{sched_id}")
async def iptv_scheduled_cancel(sched_id: int, request: Request):
    """Cancel a pending scheduled recording. Only 'pending' rows can be
    cancelled; once 'triggered' the actual recording is managed by the
    existing iptv_recordings flow."""
    await _verify_iptv(request)
    await _ensure_scheduled_table()
    async with aiosqlite.connect(_db.DB_PATH) as conn:
        cur = await conn.execute(
            "UPDATE iptv_scheduled SET status='cancelled' "
            "WHERE id=? AND status='pending'",
            (sched_id,),
        )
        await conn.commit()
        changed = cur.rowcount or 0
    if not changed:
        raise HTTPException(404, "no pending row with that id")
    return {"ok": True, "cancelled": sched_id}


# Module-level holder for the lifespan-started background task. Main app
# starts it once via `await iptv_routes.start_scheduler_loop()`.
_scheduler_task: asyncio.Task | None = None


async def _scheduler_tick_once():
    """Inspect iptv_scheduled, fire anything due. Tolerates per-row
    failures (one bad schedule shouldn't block the rest)."""
    await _ensure_scheduled_table()
    now_dt = datetime.now(timezone.utc)
    now_s = now_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    async with aiosqlite.connect(_db.DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute(
            "SELECT id, channel_id, start_at, duration_min, "
            "       padding_pre, padding_post "
            "FROM iptv_scheduled "
            "WHERE status='pending'",
        )
        rows = await cur.fetchall()
    for r in rows:
        try:
            start_dt = datetime.strptime(
                r["start_at"].rstrip("Z"), "%Y-%m-%dT%H:%M:%S"
            ).replace(tzinfo=timezone.utc)
        except Exception:
            continue
        effective_start = start_dt.timestamp() - (r["padding_pre"] or 0) * 60
        if now_dt.timestamp() < effective_start:
            continue
        total_min = (r["duration_min"] or 0) + (r["padding_pre"] or 0) + (r["padding_post"] or 0)
        if total_min <= 0:
            continue
        try:
            result = await _iptv.record_channel(r["channel_id"], int(total_min))
        except Exception as exc:
            logger.exception("scheduled DVR row %s failed", r["id"])
            async with aiosqlite.connect(_db.DB_PATH) as conn:
                await conn.execute(
                    "UPDATE iptv_scheduled SET status='failed', "
                    "triggered_at=?, error=? WHERE id=?",
                    (now_s, str(exc)[:300], r["id"]),
                )
                await conn.commit()
            continue
        job_id = (result or {}).get("job_id") or (result or {}).get("id")
        async with aiosqlite.connect(_db.DB_PATH) as conn:
            await conn.execute(
                "UPDATE iptv_scheduled SET status='triggered', "
                "triggered_at=?, job_id=? WHERE id=?",
                (now_s, str(job_id) if job_id else None, r["id"]),
            )
            await conn.commit()


async def _scheduler_loop():
    while True:
        try:
            await _scheduler_tick_once()
        except Exception:
            logger.exception("scheduler tick crashed")
        await asyncio.sleep(60)


async def start_scheduler_loop():
    """Idempotent: starts the background DVR scheduler once.
    Called from app.main lifespan."""
    global _scheduler_task
    if _scheduler_task and not _scheduler_task.done():
        return
    await _ensure_scheduled_table()
    await _ensure_play_history_table()
    _scheduler_task = asyncio.create_task(_scheduler_loop())
    logger.info("iptv: scheduled-DVR tick loop started")


# ── M3U / Xtream import ─────────────────────────────────────────


class M3UImportBody(BaseModel):
    label: str                     # operator-friendly name for the playlist
    m3u_url: str | None = None     # fetch from URL …
    m3u_text: str | None = None    # …or accept inline pasted M3U body


@router.post("/api/iptv/import_m3u")
async def iptv_import_m3u(body: M3UImportBody, request: Request):
    """Ingest a third-party M3U playlist into the iptv_channels table
    under a custom `source` tag. Re-uses the existing iptv.parse_m3u
    helper. After insert, the user can run /api/iptv/dedup/run to
    merge them into logical_channels with the official iptv-org data."""
    await _verify_iptv(request)
    _mini.require_scope(await _mini._verify(request), "*")  # owner-only

    label = (body.label or "").strip().lower().replace(" ", "-")
    if not label or not all(c.isalnum() or c in "-_" for c in label):
        raise HTTPException(400, "label must be alphanumeric+hyphen")

    if body.m3u_text and body.m3u_text.strip():
        text = body.m3u_text
    elif body.m3u_url and body.m3u_url.startswith(("http://", "https://")):
        try:
            async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as cli:
                r = await cli.get(body.m3u_url)
            if r.status_code != 200:
                raise HTTPException(502, f"M3U fetch returned {r.status_code}")
            text = r.text
        except httpx.HTTPError as exc:
            raise HTTPException(502, f"M3U fetch failed: {exc}")
    else:
        raise HTTPException(400, "supply either m3u_url or m3u_text")

    try:
        parsed = _iptv.parse_m3u(text)
    except Exception as exc:
        raise HTTPException(400, f"M3U parse failed: {exc}")

    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    inserted, skipped = 0, 0
    async with aiosqlite.connect(_db.DB_PATH) as conn:
        for ch in parsed:
            ch_name = (ch.get("name") or "").strip()
            ch_url  = (ch.get("url") or "").strip()
            if not ch_name or not ch_url:
                skipped += 1
                continue
            row_id = f"{label}:{ch.get('id') or ch_name}"[:255]
            try:
                await conn.execute(
                    "INSERT OR IGNORE INTO iptv_channels "
                    "(id, name, country, languages, categories, url, logo, "
                    " source, last_seen_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        row_id, ch_name,
                        (ch.get("country") or "").upper() or None,
                        ch.get("languages") or "",
                        ch.get("categories") or "",
                        ch_url, ch.get("logo") or "",
                        label, now,
                    ),
                )
                if conn.total_changes:
                    inserted += 1
                else:
                    skipped += 1
            except Exception:
                skipped += 1
        await conn.commit()
    return {
        "ok": True, "source": label,
        "parsed": len(parsed), "inserted": inserted, "skipped": skipped,
        "hint": "Run POST /api/iptv/dedup/run to merge into logical channels.",
    }


# ── SG-curated pin (data lives in scopes-yaml-adjacent file) ──


SG_CURATED_PATH = Path(os.environ.get(
    "SMDL_SG_CURATED", "/app/data/iptv_sg_curated.yaml"
))


def _load_sg_curated() -> list[str]:
    """List of channel IDs (logical preferred, source-prefixed accepted)
    that should appear on the SG-pinned tab. Order in the file is the
    display order."""
    if not SG_CURATED_PATH.is_file():
        return []
    try:
        import yaml
        with SG_CURATED_PATH.open("r", encoding="utf-8") as f:
            doc = yaml.safe_load(f) or {}
        ch = doc.get("channels") or []
        return [str(x).strip() for x in ch if x]
    except Exception as exc:
        logger.warning("SG curated load failed: %s", exc)
        return []


@router.get("/api/iptv/sg")
async def iptv_sg_curated(request: Request):
    """Curated SG channels — Mediacorp, CNA, Singtel sports, regional news.
    Pulled from data/iptv_sg_curated.yaml (manually maintained).  Each
    entry hydrated with name + logo lookup id so the grid can render
    using existing card markup."""
    await _verify_iptv(request)
    ids = _load_sg_curated()
    if not ids:
        return {"items": [], "count": 0, "note": "no curated list yet"}
    placeholders = ",".join("?" * len(ids))
    out: list[dict] = []
    async with aiosqlite.connect(_db.DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        # Try logical channels first
        cur = await conn.execute(
            f"SELECT id, name, country, categories, is_curated, "
            f"       source_count, alive_count_srcs AS alive_count "
            f"FROM v_channels_with_status WHERE id IN ({placeholders})",
            ids,
        )
        found = {r["id"]: dict(r) for r in await cur.fetchall()}
        # Fall back to source-prefixed rows for any that didn't match
        missing = [i for i in ids if i not in found]
        if missing:
            ph2 = ",".join("?" * len(missing))
            cur = await conn.execute(
                f"SELECT id, name, country, categories, logo "
                f"FROM iptv_channels WHERE id IN ({ph2})",
                missing,
            )
            for r in await cur.fetchall():
                found[r["id"]] = dict(r)
        # Preserve curated YAML order
        for cid in ids:
            row = found.get(cid)
            if not row:
                continue
            row["categories"] = [
                c for c in (row.get("categories") or "").split(",") if c
            ] if isinstance(row.get("categories"), str) else (row.get("categories") or [])
            out.append(row)
    return {"items": out, "count": len(out)}


# ── HTML ────────────────────────────────────────────────────────────


_BROWSE_HTML = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>SMDL · Live TV</title>
  <script src="https://telegram.org/js/telegram-web-app.js"></script>
  <style>
    :root { color-scheme: dark light; --drawer-w: 280px; }
    * { box-sizing: border-box; }
    html, body { margin:0; padding:0; font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
           background:var(--tg-theme-bg-color,#0f1115); color:var(--tg-theme-text-color,#e8eaed); }
    /* Independent scroll containers so position:sticky on the topbar has
       a real viewport to pin to. Without this, scrolling the body past
       the topbar's natural position caused it to disappear. */
    html, body { height:100%; overflow:hidden; }
    body { display:flex; }

    /* ── Drawer / left nav ──────────────────────────────────────── */
    .drawer {
      width:var(--drawer-w); flex-shrink:0; background:#0c0e13;
      border-right:1px solid #1d2129; overflow-y:auto; overflow-x:hidden;
      padding-bottom:24px; height:100vh;
    }
    .drawer .drawer-h {
      display:flex; align-items:center; justify-content:space-between;
      padding:14px; border-bottom:1px solid #1d2129; position:sticky; top:0;
      background:#0c0e13; z-index:2;
    }
    .drawer .drawer-h h1 { margin:0; font-size:17px; }
    .drawer .drawer-h .sub { font-size:10px; color:var(--tg-theme-hint-color,#8a8f99); margin-top:1px; }
    .drawer .close-btn {
      display:none; background:transparent; border:0; color:#8a8f99;
      font-size:22px; cursor:pointer; padding:4px 10px;
    }
    .drawer .section-h {
      padding:14px 14px 6px; font-size:10px; letter-spacing:.1em;
      color:var(--tg-theme-hint-color,#8a8f99); text-transform:uppercase;
    }
    .actions-row { display:flex; gap:6px; padding:8px 14px; flex-wrap:wrap; }
    .actions-row button {
      flex:1 1 auto; min-width:0; font:inherit; border:0; padding:9px 8px;
      border-radius:8px; background:var(--tg-theme-button-color,#3390ec);
      color:#fff; cursor:pointer; font-size:12px;
    }
    .actions-row button.ghost {
      background:transparent; color:var(--tg-theme-link-color,#5ac8fa);
      border:1px solid currentColor;
    }
    .chip-row {
      display:flex; flex-direction:column; gap:4px; padding:0 14px 4px;
    }
    .chip {
      display:block; width:100%; text-align:left; font-size:12px;
      padding:8px 11px; border-radius:8px;
      background:#15181f; border:1px solid #232831; cursor:pointer;
      user-select:none; color:#cfd2d8; transition: background .08s ease, border-color .08s ease;
    }
    .chip:hover { background:#1a1d24; }
    .chip.active { background:#3390ec; border-color:#3390ec; color:#fff; }

    /* ── Filter tiles (collapsed dropdown UX, one per facet) ───── */
    .filter-tile {
      margin:8px 14px 0; background:#15181f; border:1px solid #232831;
      border-radius:10px; overflow:hidden;
    }
    .filter-tile summary {
      list-style:none; cursor:pointer; padding:11px 13px;
      display:flex; align-items:center; gap:10px;
      font-size:13px; user-select:none;
    }
    .filter-tile summary::-webkit-details-marker { display:none; }
    .filter-tile summary::after {
      content: '▾'; margin-left:auto; color:#5ac8fa; font-size:11px;
      transition: transform .15s ease;
    }
    .filter-tile[open] summary::after { transform: rotate(180deg); }
    .filter-tile .ft-label {
      font-size:10px; letter-spacing:.1em; text-transform:uppercase;
      color:var(--tg-theme-hint-color,#8a8f99); margin-right:6px;
    }
    .filter-tile .ft-value {
      flex:1; color:#cfd2d8; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;
    }
    .filter-tile[open] .ft-value { opacity:.6; }
    .filter-tile .ft-search {
      padding:6px 10px 6px; border-top:1px solid #232831;
    }
    .filter-tile .ft-search input {
      width:100%; padding:7px 10px; border-radius:6px; border:1px solid #2a2f3a;
      background:#0d0f14; color:#fff; font-size:12px;
    }
    .filter-tile .ft-options {
      max-height:50vh; overflow-y:auto; padding:6px 10px 10px;
      display:flex; flex-direction:column; gap:4px;
    }

    /* ── Main column ────────────────────────────────────────────── */
    .main {
      flex:1; min-width:0;
      height:100vh; overflow-y:auto; overflow-x:hidden;
    }
    .topbar {
      position:sticky; top:0; z-index:10;
      display:flex; gap:8px; align-items:center;
      padding:10px 14px; background:rgba(15,17,21,.92);
      backdrop-filter:saturate(180%) blur(8px);
      border-bottom:1px solid #1d2129;
    }
    .topbar .hamburger {
      display:none; background:transparent; border:0; color:#cfd2d8;
      font-size:22px; cursor:pointer; padding:0 4px;
    }
    .topbar .search-wrap { flex:1; }
    .topbar input[type=search] {
      width:100%; padding:9px 12px; border-radius:10px; border:1px solid #2a2f3a;
      background:#181b22; color:#fff; font-size:14px;
    }
    .topbar .icon-btn {
      background:transparent; border:0; color:#5ac8fa; font-size:18px;
      padding:6px 10px; cursor:pointer; border-radius:8px;
    }
    .topbar .icon-btn:hover { background:#1a1d24; }
    .section-h.result-h {
      padding:12px 14px 6px; font-size:11px; letter-spacing:.08em;
      color:var(--tg-theme-hint-color,#8a8f99); text-transform:uppercase;
    }
    .grid {
      display:grid; gap:10px; padding:6px 14px 90px;
      grid-template-columns: repeat(auto-fill, minmax(120px, 1fr));
    }

    /* ── Quick-tab strip (All / SG / Favorites / Last watched) ──── */
    .quick-tabs {
      display:flex; gap:6px; padding:10px 14px 0; flex-wrap:wrap;
      border-bottom:1px solid #1d2129;
    }
    .quick-tabs .qt {
      font:inherit; border:0; background:transparent; color:#cfd2d8;
      padding:8px 14px; border-radius:8px 8px 0 0; cursor:pointer;
      font-size:13px; border-bottom:2px solid transparent;
    }
    .quick-tabs .qt:hover { background:#15181f; }
    .quick-tabs .qt.active {
      color:#5ac8fa; border-bottom-color:#5ac8fa; font-weight:600;
    }

    /* ── Last-watched horizontal scroller row ─────────────────── */
    .recent-grid {
      display:flex; gap:10px; padding:6px 14px 8px;
      overflow-x:auto; overflow-y:hidden;
      scrollbar-width:thin;
    }
    .recent-grid .rcard {
      flex:0 0 auto; width:84px;
      background:#181b22; border:1px solid #232831; border-radius:10px;
      padding:8px; cursor:pointer; text-align:center;
    }
    .recent-grid .rcard:active { transform:scale(.96); border-color:#3390ec; }
    .recent-grid .rcard .logo {
      width:64px; height:64px; margin:0 auto 6px; border-radius:8px;
      background:#0d0f14; display:flex; align-items:center; justify-content:center;
      overflow:hidden;
    }
    .recent-grid .rcard .logo img { max-width:88%; max-height:88%; }
    .recent-grid .rcard .rname {
      font-size:10.5px; line-height:1.15;
      max-height:28px; overflow:hidden;
    }

    /* ── Import modal ─────────────────────────────────────────── */
    .import-modal {
      position:fixed; inset:0; background:rgba(8,10,14,.85);
      z-index:80; display:none; align-items:center; justify-content:center; padding:18px;
    }
    .import-modal.show { display:flex; }
    .import-card {
      width:100%; max-width:480px; background:#15181f; border:1px solid #232831;
      border-radius:14px; padding:18px;
    }
    .import-card h3 { font-size:15px; }
    .import-card label { display:block; font-size:11px; letter-spacing:.06em;
                          text-transform:uppercase; color:#8a8f99; margin:10px 0 4px; }
    .import-card label small { text-transform:none; letter-spacing:0;
                                 color:#5a5a5a; font-weight:normal; }
    .import-card input, .import-card textarea {
      width:100%; padding:9px 11px; border-radius:8px; border:1px solid #2a2f3a;
      background:#0d0f14; color:#fff; font:13px monospace; outline:none;
    }
    .import-card input:focus, .import-card textarea:focus { border-color:#3390ec; }
    .import-card button {
      flex:1; font:inherit; border:0; padding:10px 14px; border-radius:8px;
      background:#3390ec; color:#fff; font-size:13px; cursor:pointer;
    }
    .import-card button.ghost {
      background:transparent; color:#5ac8fa; border:1px solid #5ac8fa;
    }

    /* ── Mobile (<768px) — drawer becomes slide-in overlay ──────── */
    @media (max-width: 767px) {
      body { display:block; height:100vh; overflow:hidden; }
      .drawer {
        position:fixed; top:0; bottom:0; left:calc(-1 * var(--drawer-w));
        transition: left .22s ease; z-index:60; height:100vh;
      }
      .drawer.open { left:0; box-shadow:0 0 36px rgba(0,0,0,.55); }
      .drawer .close-btn { display:block; }
      .drawer-backdrop {
        position:fixed; inset:0; background:rgba(0,0,0,.5);
        z-index:55; display:none;
      }
      .drawer-backdrop.show { display:block; }
      .topbar .hamburger { display:block; }
      .main { width:100%; height:100vh; overflow-y:auto; }
    }
    .card {
      background:#181b22; border:1px solid #232831; border-radius:12px;
      padding:10px; cursor:pointer; transition: transform .08s ease, border-color .08s ease;
      display:flex; flex-direction:column; gap:6px; min-height:130px;
    }
    .card:active { transform: scale(.97); border-color:#3390ec; }
    .card { position:relative; }
    .card .star-btn {
      position:absolute; top:6px; right:6px; width:28px; height:28px;
      display:flex; align-items:center; justify-content:center;
      background:rgba(13,15,20,.7); border:0; border-radius:14px;
      cursor:pointer; padding:0; font-size:14px; line-height:1;
      color:#5a5a5a; transition: color .12s ease, background .12s ease;
      backdrop-filter:blur(4px); z-index:1;
    }
    .card .star-btn.on { color:#ffd60a; }
    .card .star-btn:hover { background:rgba(13,15,20,.9); }
    .card .logo-wrap {
      aspect-ratio:1/1; background:#0d0f14; border-radius:8px;
      display:flex; align-items:center; justify-content:center; overflow:hidden;
    }
    .card .logo-wrap img { max-width:80%; max-height:80%; object-fit:contain; }
    .card .logo-wrap .glyph { font-size:32px; opacity:.55; }
    .card .name { font-size:12px; line-height:1.2; font-weight:500;
                   overflow:hidden; text-overflow:ellipsis; display:-webkit-box;
                   -webkit-line-clamp:2; -webkit-box-orient:vertical; }
    .card .meta { font-size:10px; color:var(--tg-theme-hint-color,#8a8f99); }
    .card .np   { font-size:10.5px; color:#a9e8be; line-height:1.2;
                   white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
    .card .badges { display:flex; gap:4px; flex-wrap:wrap; margin-top:auto; }
    .card .badges .b {
      font-size:9px; font-weight:600; padding:1px 5px; border-radius:3px;
      letter-spacing:.04em; line-height:1.3;
    }
    .b.hls   { background:#1f5230; color:#a9e8be; }
    .b.dash  { background:#5a3320; color:#fcc; }
    .b.ts    { background:#3a3a3a; color:#ddd; }
    .b.official { background:#1a3d5c; color:#9ec9ec; }
    .b.restream { background:#3a2a3a; color:#cda6d6; }
    .b.geo   { background:#5a2020; color:#f5b4b4; }
    .b.multi { background:#1f3c5a; color:#b6d5f0; }   /* ×N source-count chip */
    .b.curated { background:#3a3a14; color:#f0e090; } /* curated badge */
    .empty, .loading { text-align:center; padding:40px 16px;
                        color:var(--tg-theme-hint-color,#8a8f99); font-size:13px; }
    .toast {
      position:fixed; left:50%; bottom:24px; transform:translateX(-50%);
      background:#222; color:#fff; padding:10px 16px; border-radius:8px;
      font-size:13px; z-index:50; opacity:0; transition:opacity .25s ease;
      pointer-events:none;
    }
    .toast.show { opacity:1; }
    .back { color:#5ac8fa; cursor:pointer; padding:10px 14px 0; font-size:13px; }
    .login-veil {
      position:fixed; inset:0; background:rgba(8,10,14,.96); z-index:100;
      display:none; align-items:center; justify-content:center; padding:24px;
    }
    .login-veil.show { display:flex; }
    .login-card {
      width:100%; max-width:360px; background:#181b22; border:1px solid #232831;
      border-radius:12px; padding:20px;
    }
    .login-card h2 { font-size:16px; margin:0 0 6px; }
    .login-card .sub { font-size:12px; color:var(--tg-theme-hint-color,#8a8f99); margin-bottom:12px; }
    .login-card input {
      width:100%; padding:11px 12px; border-radius:8px; border:1px solid #2a2f3a;
      background:#0d0f14; color:#fff; font-family:ui-monospace,Menlo,monospace; font-size:12px;
    }
    .login-card button {
      margin-top:10px; width:100%; padding:12px; border:0; border-radius:8px;
      background:#3390ec; color:#fff; font-size:14px; cursor:pointer;
    }
    .login-card .err { color:#f5b4b4; font-size:12px; margin-top:8px; min-height:14px; }
  </style>
</head>
<body>

<div class="login-veil" id="login-veil">
  <div class="login-card">
    <h2>🔑 First-launch setup</h2>
    <div class="sub">
      Paste your owner token. It's stored only in this device's cookie
      (90 days, signed against the server). You only do this once per
      install.
    </div>
    <input id="login-token" type="password" placeholder="OWNER_AUTH_TOKEN" autocomplete="off">
    <button id="login-submit">Sign in</button>
    <div class="err" id="login-err"></div>
  </div>
</div>

<div class="drawer-backdrop" id="drawer-backdrop"></div>

<aside class="drawer" id="drawer">
  <div class="drawer-h">
    <div>
      <h1>📺 Live TV</h1>
      <div class="sub">click a channel to watch in VLC</div>
    </div>
    <button class="close-btn" id="drawer-close" aria-label="close">×</button>
  </div>

  <div class="actions-row">
    <button id="refresh-btn">↻ Refresh all</button>
    <button class="ghost" id="probe-all-btn">🩺 Probe</button>
  </div>
  <div class="actions-row">
    <button class="ghost" id="alive-only-btn">✓ Alive only: off</button>
    <button class="ghost" id="favorites-only-btn">⭐ Favorites: off</button>
  </div>
  <div class="actions-row">
    <button class="ghost" id="import-m3u-btn" title="Import third-party M3U / Xtream playlist">📥 Import M3U…</button>
  </div>
  <div class="actions-row" id="country-quick-row"></div>

  <!-- Probe knobs (collapsible) — defaults work; only open if tuning. -->
  <details class="probe-tune" style="margin:8px 14px 0; font-size:11px; color:var(--tg-theme-hint-color,#8a8f99);">
    <summary style="cursor:pointer; padding:4px 0;">Probe tuning</summary>
    <div style="padding:8px 0 4px;">
      <label style="display:flex; align-items:center; gap:8px; padding:4px 0;">
        <span style="flex:1">Concurrency</span>
        <input type="range" id="probe-conc" min="4" max="128" step="4" value="32" style="width:120px">
        <span id="probe-conc-v" style="font-variant-numeric:tabular-nums; width:32px; text-align:right">32</span>
      </label>
      <label style="display:flex; align-items:center; gap:8px; padding:4px 0;">
        <span style="flex:1">Timeout (s)</span>
        <input type="range" id="probe-to" min="2" max="15" step="1" value="6" style="width:120px">
        <span id="probe-to-v" style="font-variant-numeric:tabular-nums; width:32px; text-align:right">6</span>
      </label>
      <label style="display:flex; align-items:center; gap:8px; padding:4px 0;">
        <span style="flex:1">Fresh-skip (h)</span>
        <input type="range" id="probe-fresh" min="0" max="48" step="1" value="6" style="width:120px">
        <span id="probe-fresh-v" style="font-variant-numeric:tabular-nums; width:32px; text-align:right">6</span>
      </label>
      <label style="display:flex; align-items:center; gap:8px; padding:6px 0;">
        <input type="checkbox" id="probe-force" style="margin:0">
        <span>Force re-probe even fresh channels</span>
      </label>
      <div style="font-size:10px; opacity:.7; line-height:1.5; margin-top:2px;">
        Higher concurrency = faster sweep (residential SG fibre handles ~64 easily).<br>
        Fresh-skip hides channels alive within the window. Set to 0 + ✓ force = full re-probe.
      </div>
    </div>
  </details>

  <div id="probe-status" style="display:none; padding:0 14px; font-size:11px; color:var(--tg-theme-hint-color,#8a8f99);"></div>

  <details class="filter-tile" data-facet="source">
    <summary>
      <span class="ft-label">Source</span>
      <span class="ft-value" id="ft-value-source">All</span>
    </summary>
    <div class="ft-search">
      <input type="search" placeholder="Search sources…" data-target="source-chips">
    </div>
    <div class="ft-options chip-row" id="source-chips"></div>
  </details>

  <details class="filter-tile" data-facet="country">
    <summary>
      <span class="ft-label">Country</span>
      <span class="ft-value" id="ft-value-country">All</span>
    </summary>
    <div class="ft-search">
      <input type="search" placeholder="Search countries…" data-target="country-chips">
    </div>
    <div class="ft-options chip-row" id="country-chips"></div>
  </details>

  <details class="filter-tile" data-facet="category">
    <summary>
      <span class="ft-label">Category</span>
      <span class="ft-value" id="ft-value-category">All</span>
    </summary>
    <div class="ft-search">
      <input type="search" placeholder="Search categories…" data-target="category-chips">
    </div>
    <div class="ft-options chip-row" id="category-chips"></div>
  </details>

  <div class="section-h" style="margin-top:18px; display:flex; gap:14px; padding:14px;">
    <a href="/iptv/recordings" style="color:#5ac8fa; text-decoration:none;">📼 Recordings</a>
    <a href="/app" style="color:#5ac8fa; text-decoration:none; margin-left:auto;">← Back to SMDL</a>
  </div>
</aside>

<main class="main">
  <div class="topbar">
    <button class="hamburger" id="hamburger-btn" aria-label="filters">☰</button>
    <div class="search-wrap">
      <input id="search" type="search" placeholder="Search channels (CNN, BBC, news…)">
    </div>
    <button class="icon-btn" id="refresh-top-btn" title="Refresh all sources" aria-label="refresh">↻</button>
  </div>

  <!-- Quick-tab strip: All / SG / Favorites -->
  <div class="quick-tabs" id="quick-tabs">
    <button class="qt active" data-tab="all">All</button>
    <button class="qt" data-tab="sg">🇸🇬 Singapore</button>
    <button class="qt" data-tab="fav">⭐ Favorites</button>
    <button class="qt" data-tab="recent" id="qt-recent" style="display:none">⏱ Last watched</button>
  </div>

  <!-- "Last watched" pinned row, only rendered when state.tab==='all' -->
  <section class="recent-row" id="recent-row" style="display:none">
    <div class="section-h" style="margin-top:14px">Last watched</div>
    <div class="recent-grid" id="recent-grid"></div>
  </section>

  <div class="section-h result-h" id="result-h">Channels</div>
  <div class="grid" id="grid"><div class="loading">Loading…</div></div>

  <!-- Owner-only import modal (toggled by Admin panel below) -->
  <div class="import-modal" id="import-modal">
    <div class="import-card">
      <h3 style="margin:0 0 10px">Import M3U / Xtream playlist</h3>
      <label>Label <small>(alphanumeric, used as source tag)</small></label>
      <input type="text" id="m3u-label" placeholder="my-iptv-provider" maxlength="32">
      <label>M3U URL <small>(or paste raw M3U below)</small></label>
      <input type="text" id="m3u-url" placeholder="http://provider.example/playlist.m3u">
      <label>or paste M3U body</label>
      <textarea id="m3u-text" rows="6" placeholder="#EXTM3U..."></textarea>
      <div style="display:flex; gap:8px; margin-top:12px">
        <button id="m3u-go">Import</button>
        <button class="ghost" id="m3u-cancel">Cancel</button>
      </div>
      <div class="hint" id="m3u-status" style="margin-top:8px; min-height:18px"></div>
    </div>
  </div>
</main>

<div class="toast" id="toast"></div>

<script>
const tg = window.Telegram?.WebApp;
if (tg) { tg.ready(); tg.expand(); }
const initData = tg?.initData || '';

const STATE_KEY = 'smdl_iptv_filters_v1';
const FAV_KEY_V1 = 'smdl_iptv_favorites_v1';  // legacy — source-prefixed IDs
const FAV_KEY    = 'smdl_iptv_favorites_v2';  // current — logical channel IDs
const _defaultState = {
  country: null, category: null, source: null,
  status: null, q: '', favorites_only: false,
  tab: 'all',                // 'all' | 'sg' | 'fav' | 'recent'
  now_playing: {},           // populated lazily by loadNowPlaying()
};

// ── Favorites — Set of channel ids, persisted to localStorage ──────
let _favorites = (() => {
  try {
    const arr = JSON.parse(localStorage.getItem(FAV_KEY) || '[]');
    return new Set(Array.isArray(arr) ? arr : []);
  } catch { return new Set(); }
})();
function _saveFavorites() {
  try { localStorage.setItem(FAV_KEY, JSON.stringify([..._favorites])); } catch {}
}
function isFavorite(cid) { return _favorites.has(cid); }
function toggleFavorite(cid) {
  if (_favorites.has(cid)) _favorites.delete(cid);
  else _favorites.add(cid);
  _saveFavorites();
}
// Hydrate from localStorage so filters survive across page reloads
// (e.g. tap a channel → land on /iptv/play/... → hit Back → drawer
// still shows the country chip you'd picked).
const state = (() => {
  try {
    const saved = JSON.parse(localStorage.getItem(STATE_KEY) || 'null');
    return saved ? Object.assign({}, _defaultState, saved) : Object.assign({}, _defaultState);
  } catch { return Object.assign({}, _defaultState); }
})();
function _persistState() {
  try { localStorage.setItem(STATE_KEY, JSON.stringify(state)); } catch {}
}

// Country display-name + sort helpers. Modern Intl.DisplayNames maps
// "SG" → "Singapore", "US" → "United States", etc. — used for both the
// chip label and the alphabetical ordering. Falls back to the raw code
// if the runtime doesn't support it (very old WebView).
const _COUNTRY_DN = (() => {
  try { return new Intl.DisplayNames(['en'], { type: 'region' }); }
  catch { return null; }
})();
function countryName(code) {
  if (!code) return '';
  try { return _COUNTRY_DN ? (_COUNTRY_DN.of(code.toUpperCase()) || code) : code; }
  catch { return code; }
}

const SOURCE_LABELS = {
  'iptv-org':       'iptv-org',
  'free-tv':        'Free-TV',
  'mjh-radio':      '📻 mjh radio',
  'mjh-sky-fast':   'mjh Sky-NZ FAST',
  'mjh-au':         '🇦🇺 mjh AU',
  'mjh-nz':         '🇳🇿 mjh NZ',
  'mjh-other':      'mjh (other)',
  'fanmingming':    '凡明明 (CCTV)',
  'yuechan':        'YueChan',
  'openiptvitaly':  '🇮🇹 Italy',
  'iptv-org-sg':    '🇸🇬 SG curated',
  'iptv-org-my':    '🇲🇾 MY curated',
  'iptv-org-id':    '🇮🇩 ID curated',
  'youtube-live':   '📺 YouTube Live',
};

// Friendly flag for any country-slice source the server returns.
function sourceLabel(sid) {
  if (SOURCE_LABELS[sid]) return SOURCE_LABELS[sid];
  if (sid && sid.startsWith('iptv-org-')) {
    const cc = sid.split('-').slice(-1)[0].toUpperCase();
    return `${flag(cc)} ${cc} curated`;
  }
  return sid;
}

async function api(path, opts = {}) {
  opts.credentials = 'same-origin';  // ensure sentinel_apk_session cookie rides along
  opts.headers = Object.assign({}, opts.headers || {}, {
    'X-Init-Data': initData,
    ...(opts.body ? {'Content-Type': 'application/json'} : {}),
  });
  const r = await fetch(path, opts);
  if (r.status === 401) {
    showLogin();
    throw new Error('401 — sign in');
  }
  if (!r.ok) {
    const text = await r.text();
    let detail = text;
    try { detail = JSON.parse(text).detail || detail; } catch (e) {}
    throw new Error(`${r.status}: ${detail}`);
  }
  return await r.json();
}

function toast(msg, ms=2000) {
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.classList.add('show');
  setTimeout(() => t.classList.remove('show'), ms);
}

function showLogin() {
  document.getElementById('login-veil').classList.add('show');
  document.getElementById('login-token').focus();
}

document.getElementById('login-submit')?.addEventListener('click', async () => {
  const btn = document.getElementById('login-submit');
  const errEl = document.getElementById('login-err');
  const tokenEl = document.getElementById('login-token');
  const token = (tokenEl.value || '').trim();
  if (!token) { errEl.textContent = 'paste your token first'; return; }
  btn.disabled = true; btn.textContent = '…';
  errEl.textContent = '';
  try {
    const fd = new FormData();
    fd.append('token', token);
    fd.append('next', '/iptv');
    const r = await fetch('/auth/setup', {
      method: 'POST', body: fd, credentials: 'same-origin', redirect: 'manual',
    });
    // 303 / 0 (opaqueredirect) = success; the cookie was set on the response.
    if (r.status === 303 || r.type === 'opaqueredirect' || r.ok) {
      tokenEl.value = '';
      document.getElementById('login-veil').classList.remove('show');
      loadFilters();
      loadChannels();
    } else if (r.status === 401) {
      errEl.textContent = 'token rejected — check OWNER_AUTH_TOKEN';
    } else {
      errEl.textContent = `unexpected ${r.status}`;
    }
  } catch (e) {
    errEl.textContent = 'network: ' + e.message;
  } finally {
    btn.disabled = false; btn.textContent = 'Sign in';
  }
});

// ISO 3166-1 alpha-2 → flag emoji
function flag(code) {
  if (!code || code.length !== 2) return '🏳️';
  const A = 0x1F1E6;
  return String.fromCodePoint(A + code.charCodeAt(0) - 65)
       + String.fromCodePoint(A + code.charCodeAt(1) - 65);
}

// Stream-type + origin helpers — duplicated in the play page's JS
// (separate <script> scope; one .py file but two HTML strings).
function streamTypeOf(url) {
  if (!url) return 'other';
  const u = url.toLowerCase().split('?')[0].split('#')[0];
  if (u.endsWith('.m3u8') || u.endsWith('.m3u')) return 'hls';
  if (u.endsWith('.mpd')) return 'dash';
  if (u.endsWith('.ts'))  return 'ts';
  return 'other';
}
const _OFFICIAL_HOSTS = [
  'cloudfront.net','akamaized.net','akamai.net','akamaihd.net','fastly.net','fastly.com',
  'amagi.tv','amg01082','amg18481','amg02159','playouts.now','playoutshq','amagi-cdn',
  'streamized.net','mediacorp','mncdn.com',
  'bbc.co.uk','rai.it','iheart.com','tvnz.co.nz','sbs.com.au',
  'abc.net.au','rainz.akamaized.net','live-video.net','wzm.live',
];
const _RESTREAM_HOSTS = [
  'viloud.tv','indihuy','lordstreams','stitcher.com.br','xtreamer','spaghett',
  'streamtape','dropbox','githubusercontent.com','ahmsville',
];
function originOf(url) {
  if (!url) return { kind: 'unknown', host: '' };
  let host = '';
  try { host = new URL(url).hostname.toLowerCase(); } catch { return { kind:'unknown', host:'' }; }
  for (const m of _OFFICIAL_HOSTS) if (host.includes(m)) return { kind: 'official', host };
  for (const m of _RESTREAM_HOSTS) if (host.includes(m)) return { kind: 'restream', host };
  return { kind: 'unknown', host };
}

async function loadFilters() {
  let countries = [], categories = [], sourcesData = { sources: [], country_quick: [] };
  try {
    const [c, cat, sd] = await Promise.all([
      api('/api/iptv/countries').then(j => j.countries),
      api('/api/iptv/categories').then(j => j.categories),
      api('/api/iptv/sources'),
    ]);
    countries = c; categories = cat; sourcesData = sd;
  } catch (e) {
    document.getElementById('country-chips').innerHTML = '';
    document.getElementById('category-chips').innerHTML = '';
    document.getElementById('source-chips').innerHTML = '';
    return;
  }
  const sources = sourcesData.sources || [];
  buildCountryQuickRow(sourcesData.country_quick || []);
  const sc = document.getElementById('source-chips');
  sc.innerHTML = '';
  sc.appendChild(makeChip('All', null, state.source === null, 'source'));
  let sourceLabelText = 'All';
  for (const s of sources) {
    const label = sourceLabel(s.id) + ` (${s.count})`;
    sc.appendChild(makeChip(label, s.id, state.source === s.id, 'source'));
    if (state.source === s.id) sourceLabelText = label;
  }
  const ftSource = document.getElementById('ft-value-source');
  if (ftSource) ftSource.textContent = sourceLabelText;
  const cc = document.getElementById('country-chips');
  cc.innerHTML = '';
  cc.appendChild(makeChip('All', null, state.country === null, 'country'));
  // Sort by full display name (Singapore, United States, ...) rather
  // than by channel count — easier to find a specific country in a
  // 100+ entry list. Top entries used to be 🇺🇸 US (1462) → useless if
  // you actually want 🇸🇬 SG. Counts stay on the chip for context.
  const sortedCountries = countries.slice().sort((a, b) =>
    countryName(a.code).localeCompare(countryName(b.code)));
  let countryLabelText = 'All';
  for (const c of sortedCountries) {
    const name = countryName(c.code);
    const label = `${flag(c.code)} ${name === c.code ? c.code : name + ' · ' + c.code} (${c.count})`;
    cc.appendChild(makeChip(label, c.code, state.country === c.code, 'country'));
    if (state.country === c.code) countryLabelText = label;
  }
  const ftCountry = document.getElementById('ft-value-country');
  if (ftCountry) ftCountry.textContent = countryLabelText;

  const catc = document.getElementById('category-chips');
  catc.innerHTML = '';
  catc.appendChild(makeChip('All', null, state.category === null, 'category'));
  let categoryLabelText = 'All';
  for (const cat of categories.slice(0, 50)) {
    const label = `${cat.name} (${cat.count})`;
    catc.appendChild(makeChip(label, cat.name, state.category === cat.name, 'category'));
    if (state.category === cat.name) categoryLabelText = label;
  }
  const ftCategory = document.getElementById('ft-value-category');
  if (ftCategory) ftCategory.textContent = categoryLabelText;
}

function makeChip(label, value, active, kind) {
  const el = document.createElement('div');
  el.className = 'chip' + (active ? ' active' : '');
  el.textContent = label;
  el.addEventListener('click', () => {
    state[kind] = value;
    _persistState();
    loadChannels();
    document.querySelectorAll(`#${kind}-chips .chip`).forEach(c => c.classList.remove('active'));
    el.classList.add('active');
    // Update the dropdown tile's value label + collapse the tile.
    const ftValue = document.getElementById(`ft-value-${kind}`);
    if (ftValue) ftValue.textContent = label;
    const tile = document.querySelector(`.filter-tile[data-facet="${kind}"]`);
    if (tile) tile.open = false;
    _autoCloseDrawerIfMobile();
  });
  return el;
}

async function loadChannels() {
  const grid = document.getElementById('grid');
  grid.innerHTML = '<div class="loading">Loading…</div>';
  // SG tab takes a dedicated endpoint (curated yaml-driven list).
  // Other tabs use the v2 channels list with state filters.
  let data;
  try {
    if (state.tab === 'sg') {
      const r = await api('/api/iptv/sg');
      data = { channels: r.items || [] };
    } else {
      const params = new URLSearchParams();
      if (state.country) params.set('country', state.country);
      if (state.category) params.set('category', state.category);
      if (state.source) params.set('source', state.source);
      if (state.q) params.set('q', state.q);
      // favorites_only and the fav-tab need a wider fetch — a favorite
      // outside the first 300 by (country,name) would otherwise be invisible.
      const wide = state.favorites_only || state.tab === 'fav';
      params.set('limit', wide ? '20000' : '300');
      data = await api('/api/iptv/v2/channels?' + params.toString());
    }
  } catch (e) {
    grid.innerHTML = `<div class="empty">Error: ${escapeHtml(e.message)}</div>`;
    return;
  }
  // Fetch now-playing + last-watched in parallel; don't block first-paint
  // if either fails.
  loadNowPlaying();
  loadLastWatched();
  // Alive-only filter happens client-side — the v2 endpoint doesn't
  // have a status= param (logical channels don't have a status — only
  // their underlying sources do).
  if (state.status === 'alive') {
    data.channels = (data.channels || []).filter(c => (c.alive_count || 0) > 0);
  }
  let channels = data.channels || [];
  // Favorites-only is purely a client-side filter — the server doesn't
  // know which channels you've starred. Apply after the fetch.
  if (state.favorites_only || state.tab === 'fav') {
    channels = channels.filter(c => isFavorite(c.id));
  }
  document.getElementById('result-h').textContent =
    `Channels · ${channels.length}${channels.length >= 300 ? '+' : ''}${state.favorites_only ? ' ⭐' : ''}`;
  if (!channels.length) {
    grid.innerHTML = state.favorites_only
      ? `<div class="empty">No favorites yet. Tap the ☆ on any channel card to star it.</div>`
      : `<div class="empty">No channels match.<br>
         Try <strong>Refresh catalogue</strong> if this is your first visit.</div>`;
    return;
  }
  grid.innerHTML = '';
  for (const ch of channels) {
    const card = document.createElement('div');
    card.className = 'card';
    card.dataset.channelId = ch.id;
    // Logos are served from local cache (/iptv/logo/<id>) — the endpoint
    // fetches origin once, then serves from disk with a letter-tile fallback.
    // Same-origin <img> sends the auth cookie automatically.
    const logoHtml =
      `<img src="/iptv/logo/${encodeURIComponent(ch.id)}" alt="" loading="lazy"
            onerror="this.replaceWith(Object.assign(document.createElement('div'),{className:'glyph',textContent:'📺'}))">`;
    const np = (state.now_playing || {})[ch.id];
    const npHtml = np
      ? `<div class="np" title="${escapeAttr(np.title)}">● ${escapeHtml((np.title||'').slice(0,40))}${(np.title||'').length>40?'…':''}</div>`
      : '';
    // Logical channels carry source_count + alive_count + curated flag
    // instead of a single url/source. Multi-source badge ("×4") when N>1
    // tells the user this channel has alternate streams behind it.
    const isGeo  = /\[Geo[- ]?blocked\]/i.test(ch.name || '');
    const fav    = isFavorite(ch.id);
    const srcN   = Number(ch.source_count || 1);
    const aliveN = Number(ch.alive_count || 0);
    const badges = [];
    if (ch.is_curated) badges.push(`<span class="b curated">★ curated</span>`);
    if (srcN > 1) badges.push(`<span class="b multi">×${srcN} sources</span>`);
    if (aliveN > 0) badges.push(`<span class="b official">${aliveN} alive</span>`);
    else if (srcN > 0) badges.push(`<span class="b geo">no alive</span>`);
    if (isGeo) badges.push(`<span class="b geo">geo</span>`);
    card.innerHTML = `
      <button class="star-btn ${fav ? 'on' : ''}" aria-label="favorite">${fav ? '★' : '☆'}</button>
      <div class="logo-wrap">${logoHtml}</div>
      <div class="name">${escapeHtml(ch.name)}</div>
      <div class="meta">${flag(ch.country||'')} ${escapeHtml(ch.country||'?')} · ${escapeHtml((ch.categories||[]).slice(0,1).join(''))}</div>
      ${npHtml}
      <div class="badges">${badges.join('')}</div>
    `;
    const starBtn = card.querySelector('.star-btn');
    starBtn.addEventListener('click', (e) => {
      e.stopPropagation();  // don't bubble to card → navigate
      e.preventDefault();
      toggleFavorite(ch.id);
      const on = isFavorite(ch.id);
      starBtn.textContent = on ? '★' : '☆';
      starBtn.classList.toggle('on', on);
      // If we're in "favorites only" view, un-starring removes the card.
      if (state.favorites_only && !on) card.style.display = 'none';
    });
    card.addEventListener('click', () => location.href = `/iptv/play/${encodeURIComponent(ch.id)}`);
    grid.appendChild(card);
  }
}

document.getElementById('search')?.addEventListener('input', (e) => {
  state.q = e.target.value;
  _persistState();
  clearTimeout(window.__qt);
  window.__qt = setTimeout(loadChannels, 200);
});

document.getElementById('refresh-btn')?.addEventListener('click', async () => {
  const btn = document.getElementById('refresh-btn');
  btn.disabled = true; btn.textContent = '↻ Refreshing…';
  try {
    // If a single source is selected, only refresh that one. Else refresh all.
    const body = state.source ? { source: state.source } : {};
    const r = await api('/api/iptv/refresh', { method:'POST', body: JSON.stringify(body) });
    const totals = (r.summaries || []).map(s =>
      s.ok ? `${s.source}: +${s.upserted ?? 0}` : `${s.source}: ✗ ${s.error}`
    ).join(' · ');
    toast(totals || 'done', 4500);
    await loadFilters();
    await loadChannels();
  } catch (e) {
    toast('Refresh failed: ' + e.message, 4000);
  } finally {
    btn.disabled = false; btn.textContent = '↻ Refresh all sources';
  }
});

// ── Drawer toggle (mobile only — sidebar is persistent on tablet+) ─
const _drawer    = document.getElementById('drawer');
const _backdrop  = document.getElementById('drawer-backdrop');
function openDrawer()  { _drawer.classList.add('open');     _backdrop.classList.add('show'); }
function closeDrawer() { _drawer.classList.remove('open');  _backdrop.classList.remove('show'); }
document.getElementById('hamburger-btn')?.addEventListener('click', openDrawer);
document.getElementById('drawer-close')?.addEventListener('click', closeDrawer);
_backdrop?.addEventListener('click', closeDrawer);
// Auto-close drawer after picking a filter on mobile (one less tap to see results).
function _autoCloseDrawerIfMobile() {
  if (window.innerWidth < 768) closeDrawer();
}

// Top-bar refresh icon mirrors the drawer's Refresh button — same click.
document.getElementById('refresh-top-btn')?.addEventListener('click', () => {
  document.getElementById('refresh-btn')?.click();
});

// "Alive only" filter — toggles state.status between null and 'alive'.
// Only meaningful after a probe-all sweep has populated `status`.
document.getElementById('alive-only-btn')?.addEventListener('click', (e) => {
  state.status = state.status === 'alive' ? null : 'alive';
  e.currentTarget.textContent = `✓ Alive only: ${state.status === 'alive' ? 'on' : 'off'}`;
  _persistState();
  loadChannels();
});

document.getElementById('favorites-only-btn')?.addEventListener('click', (e) => {
  state.favorites_only = !state.favorites_only;
  e.currentTarget.textContent = `⭐ Favorites: ${state.favorites_only ? 'on' : 'off'}`;
  _persistState();
  loadChannels();
});

// Per-tile dropdown search — each filter tile has its own search input
// (data-target=<chip-list-id>). Substring-matches against each chip's
// textContent in that tile's list; non-matching get display:none.
document.querySelectorAll('.filter-tile input[type=search]').forEach(inp => {
  inp.addEventListener('input', () => {
    const q = (inp.value || '').toLowerCase().trim();
    const targetId = inp.dataset.target;
    if (!targetId) return;
    document.querySelectorAll(`#${targetId} .chip`).forEach(c => {
      c.style.display = (!q || c.textContent.toLowerCase().includes(q)) ? '' : 'none';
    });
  });
});

// Live-update probe-tune slider labels as user drags.
for (const id of ['conc', 'to', 'fresh']) {
  const inp = document.getElementById('probe-' + id);
  const lbl = document.getElementById('probe-' + id + '-v');
  if (inp && lbl) inp.addEventListener('input', () => { lbl.textContent = inp.value; });
}

// Probe-all sweep — fires the background job in scope of the current
// source/country filters, then polls /status until finished.
let _probeTimer = null;
document.getElementById('probe-all-btn')?.addEventListener('click', async () => {
  const btn = document.getElementById('probe-all-btn');
  const statusEl = document.getElementById('probe-status');
  btn.disabled = true; const orig = btn.textContent; btn.textContent = '🩺 Starting…';
  // Read user-tuned knobs (defaults if the user never opens the panel).
  const conc  = parseInt(document.getElementById('probe-conc')?.value ?? '32', 10) || 32;
  const tout  = parseInt(document.getElementById('probe-to')?.value ?? '6', 10) || 6;
  const fresh = parseInt(document.getElementById('probe-fresh')?.value ?? '6', 10) || 6;
  const force = !!document.getElementById('probe-force')?.checked;
  try {
    await api('/api/iptv/probe_all', {
      method: 'POST',
      body: JSON.stringify({
        source:  state.source,
        country: state.country,
        concurrency: conc,
        timeout_s: tout,
        force_recheck: force,
        fresh_window_hours: fresh,
      }),
    });
    statusEl.style.display = 'block';
    if (_probeTimer) clearInterval(_probeTimer);
    _probeTimer = setInterval(async () => {
      try {
        const s = await api('/api/iptv/probe_all/status');
        const skipNote = s.skipped_fresh ? ` · skipped ${s.skipped_fresh} fresh` : '';
        const byLines = (() => {
          const bs = s.by_source || {};
          const rows = Object.entries(bs)
            .sort((a,b) => (b[1].total - a[1].total))
            .slice(0, 8)
            .map(([sid, b]) => {
              const pct = b.total ? Math.floor(100 * b.checked / b.total) : 0;
              return `<div style="opacity:.85">${escapeHtml(sourceLabel(sid))}: ${b.checked}/${b.total} (${pct}%) · ✓${b.alive} ✗${b.dead}</div>`;
            }).join('');
          return rows ? `<div style="margin-top:6px; font-size:10px; line-height:1.6">${rows}</div>` : '';
        })();
        if (!s.running && s.checked >= s.total && s.total > 0) {
          statusEl.innerHTML = `Sweep complete · ${s.alive} alive · ${s.dead} dead${skipNote}` + byLines;
          clearInterval(_probeTimer); _probeTimer = null;
          btn.disabled = false; btn.textContent = orig;
          loadChannels();
        } else if (!s.running && s.total === 0) {
          statusEl.innerHTML = `No channels needed re-probing${skipNote ? ' (' + skipNote.trim() + ')' : ''}.`;
          clearInterval(_probeTimer); _probeTimer = null;
          btn.disabled = false; btn.textContent = orig;
        } else {
          const pct = s.total ? Math.floor(100 * s.checked / s.total) : 0;
          statusEl.innerHTML =
            `Probing… ${s.checked}/${s.total} (${pct}%) · alive ${s.alive} · dead ${s.dead}${skipNote} · last: ${escapeHtml(s.last_channel || '—')}` +
            byLines;
        }
      } catch (_e) { /* keep polling */ }
    }, 1200);
  } catch (e) {
    toast('Probe-all failed: ' + e.message, 4000);
    btn.disabled = false; btn.textContent = orig;
  }
});

// Per-country iptv-org quick refresh — one button per quick-code the
// server advertises in /api/iptv/sources.country_quick.
async function buildCountryQuickRow(codes) {
  const row = document.getElementById('country-quick-row');
  row.innerHTML = '';
  for (const cc of codes) {
    const btn = document.createElement('button');
    btn.className = 'ghost';
    btn.textContent = `${flag(cc.toUpperCase())} Refresh ${cc.toUpperCase()}`;
    btn.addEventListener('click', async () => {
      btn.disabled = true; const orig = btn.textContent; btn.textContent = '↻ …';
      try {
        const r = await api('/api/iptv/refresh_country', {
          method: 'POST', body: JSON.stringify({ country: cc }),
        });
        toast(`${r.country}: ${r.upserted} channels`, 3000);
        // Filter the grid to the just-refreshed source so the user can
        // see what landed.
        state.source = r.source;
        await loadFilters();
        await loadChannels();
      } catch (e) {
        toast(`${cc.toUpperCase()} refresh failed: ${e.message}`, 4000);
      } finally {
        btn.disabled = false; btn.textContent = orig;
      }
    });
    row.appendChild(btn);
  }
}

function escapeHtml(s) { return String(s ?? '').replace(/[&<>"']/g, c =>
  ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
function escapeAttr(s) { return escapeHtml(s); }

// One-shot migration: translate v1 favorites (source-prefixed IDs
// like 'iptv-org:CNA.sg') to v2 (logical channel IDs like 'cna').
// Runs once per device; subsequent loads find the v2 key already
// populated and skip the API call.
async function _migrateFavoritesIfNeeded() {
  try {
    if (localStorage.getItem(FAV_KEY)) return;  // already migrated
    const oldIds = JSON.parse(localStorage.getItem(FAV_KEY_V1) || '[]');
    if (!Array.isArray(oldIds) || oldIds.length === 0) {
      localStorage.setItem(FAV_KEY, '[]');
      return;
    }
    const r = await api('/api/iptv/sources/resolve_channels', {
      method: 'POST', body: JSON.stringify({ source_ids: oldIds }),
    });
    const newIds = [...new Set(Object.values(r.mapping || {}))].filter(Boolean);
    localStorage.setItem(FAV_KEY, JSON.stringify(newIds));
    // Refresh the in-memory Set so the next render picks up the new IDs.
    _favorites = new Set(newIds);
    if (newIds.length) {
      toast(`Migrated ${newIds.length} favorite(s) to v2`, 3000);
    }
  } catch (e) {
    console.warn('favorites migration failed:', e);
  }
}

// Restore saved-state visual cues before the first paint.
(function _hydrateUi() {
  const s = document.getElementById('search');
  if (s && state.q) s.value = state.q;
  const ab = document.getElementById('alive-only-btn');
  if (ab) ab.textContent = `✓ Alive only: ${state.status === 'alive' ? 'on' : 'off'}`;
  const fb = document.getElementById('favorites-only-btn');
  if (fb) fb.textContent = `⭐ Favorites: ${state.favorites_only ? 'on' : 'off'}`;
  // Highlight the active quick-tab from persisted state.
  document.querySelectorAll('.quick-tabs .qt').forEach(b => {
    b.classList.toggle('active', b.dataset.tab === (state.tab || 'all'));
  });
})();

// ── Quick-tab strip handlers ───────────────────────────────────
document.querySelectorAll('.quick-tabs .qt').forEach(btn => {
  btn.addEventListener('click', () => {
    state.tab = btn.dataset.tab || 'all';
    document.querySelectorAll('.quick-tabs .qt').forEach(b =>
      b.classList.toggle('active', b === btn));
    _persistState();
    loadChannels();
  });
});

// ── "Last watched" pinned row ───────────────────────────────────
async function loadLastWatched() {
  const wrap = document.getElementById('recent-row');
  const grid = document.getElementById('recent-grid');
  const qtRecent = document.getElementById('qt-recent');
  if (!wrap || !grid) return;
  // Show only on the All tab — keeps SG/fav/recent views uncluttered.
  if (state.tab !== 'all') { wrap.style.display = 'none'; return; }
  try {
    const r = await api('/api/iptv/last_watched?limit=8');
    const items = r.items || [];
    if (!items.length) { wrap.style.display = 'none'; if (qtRecent) qtRecent.style.display='none'; return; }
    if (qtRecent) qtRecent.style.display = '';
    grid.innerHTML = items.map(it => `
      <div class="rcard" data-id="${escapeAttr(it.channel_id)}">
        <div class="logo"><img src="/iptv/logo/${encodeURIComponent(it.channel_id)}" alt="" loading="lazy"></div>
        <div class="rname">${escapeHtml(it.name || it.channel_id)}</div>
      </div>`).join('');
    grid.querySelectorAll('.rcard').forEach(el => {
      el.addEventListener('click', () =>
        location.href = `/iptv/play/${encodeURIComponent(el.dataset.id)}`);
    });
    wrap.style.display = '';
  } catch (e) { wrap.style.display = 'none'; }
}

// ── "What's on now" populator ──────────────────────────────────
let _nowPlayingFetchAt = 0;
async function loadNowPlaying() {
  // Cache for 5 min — EPG doesn't change second-by-second.
  if (Date.now() - _nowPlayingFetchAt < 5 * 60 * 1000) return;
  try {
    const r = await api('/api/iptv/now_playing');
    state.now_playing = r.now_playing || {};
    _nowPlayingFetchAt = Date.now();
    // Patch the existing cards in place — avoids a second channels fetch.
    document.querySelectorAll('#grid .card').forEach(card => {
      const cid = card.dataset.channelId;
      if (!cid) return;
      const np = state.now_playing[cid];
      // Remove any pre-existing .np line first (in case of stale data).
      card.querySelectorAll('.np').forEach(n => n.remove());
      if (!np) return;
      const title = (np.title || '').slice(0, 40);
      const more = (np.title || '').length > 40 ? '…' : '';
      const npDiv = document.createElement('div');
      npDiv.className = 'np';
      npDiv.title = np.title || '';
      npDiv.textContent = `● ${title}${more}`;
      // Insert just before the badges block.
      const badges = card.querySelector('.badges');
      if (badges) card.insertBefore(npDiv, badges); else card.appendChild(npDiv);
    });
  } catch (e) { /* silently skip */ }
}

// ── M3U import modal (owner-only — endpoint enforces "*" scope) ─
const _importModal = document.getElementById('import-modal');
function openImportModal() { _importModal && _importModal.classList.add('show'); }
function closeImportModal() {
  if (!_importModal) return;
  _importModal.classList.remove('show');
  ['m3u-label','m3u-url','m3u-text'].forEach(id => {
    const el = document.getElementById(id); if (el) el.value = '';
  });
  const st = document.getElementById('m3u-status'); if (st) st.textContent = '';
}
document.getElementById('m3u-cancel')?.addEventListener('click', closeImportModal);
document.getElementById('m3u-go')?.addEventListener('click', async () => {
  const label = document.getElementById('m3u-label').value.trim();
  const url   = document.getElementById('m3u-url').value.trim();
  const text  = document.getElementById('m3u-text').value.trim();
  const st = document.getElementById('m3u-status');
  if (!label) { st.textContent = '⚠ Need a label.'; return; }
  if (!url && !text) { st.textContent = '⚠ Provide either a URL or pasted M3U.'; return; }
  st.textContent = 'Importing…';
  try {
    const body = { label };
    if (url)  body.m3u_url  = url;
    if (text) body.m3u_text = text;
    const r = await api('/api/iptv/import_m3u', { method:'POST', body: JSON.stringify(body) });
    st.innerHTML = `✓ Imported ${r.inserted} channel(s), skipped ${r.skipped}. <br>${escapeHtml(r.hint||'')}`;
    setTimeout(() => { closeImportModal(); loadChannels(); }, 2500);
  } catch (e) {
    st.textContent = '✗ ' + e.message;
  }
});
// Expose for the drawer admin button (added if present).
window.smdlIptvOpenImport = openImportModal;
document.getElementById('import-m3u-btn')?.addEventListener('click', openImportModal);

(async () => {
  await _migrateFavoritesIfNeeded();
  loadFilters();
  loadChannels();
})();
</script>
</body></html>
"""


_PLAY_HTML = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>SMDL · Watch</title>
  <script src="https://telegram.org/js/telegram-web-app.js"></script>
  <style>
    :root { color-scheme: dark light; }
    body { margin:0; font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
           background:var(--tg-theme-bg-color,#0f1115); color:var(--tg-theme-text-color,#e8eaed); }
    .back { color:#5ac8fa; cursor:pointer; padding:10px 14px 0; font-size:13px; }
    .wrap { padding:14px; }
    .channel-h { display:flex; gap:12px; align-items:center; margin-bottom:14px; }
    .channel-h .logo {
      width:64px; height:64px; background:#0d0f14; border-radius:10px;
      display:flex; align-items:center; justify-content:center; overflow:hidden; flex:0 0 auto;
    }
    .channel-h .logo img { max-width:80%; max-height:80%; object-fit:contain; }
    .channel-h .meta h1 { font-size:18px; margin:0 0 4px; }
    .channel-h .meta .sub { font-size:12px; color:var(--tg-theme-hint-color,#8a8f99); }
    .status-row { display:flex; gap:6px; flex-wrap:wrap; margin:8px 0 16px; font-size:11px; }
    .badge { padding:2px 8px; border-radius:10px; background:#1a1d24; border:1px solid #2a2f3a; }
    .badge.alive { background:#163a23; border-color:#1f5230; color:#a9e8be; }
    .badge.dead  { background:#3a1818; border-color:#522020; color:#f5b4b4; }
    .actions { display:flex; flex-direction:column; gap:8px; }
    .actions button {
      font:inherit; border:0; padding:14px; border-radius:10px;
      background:var(--tg-theme-button-color,#3390ec); color:#fff; font-size:15px; cursor:pointer;
    }
    .actions button.ghost {
      background:transparent; color:var(--tg-theme-link-color,#5ac8fa);
      border:1px solid currentColor;
    }
    .actions button.warn { background:#a23; }
    /* Scheduling modal — overlays the page; matches the import modal style. */
    .sched-modal {
      position:fixed; inset:0; background:rgba(8,10,14,.85); z-index:80;
      display:none; align-items:center; justify-content:center; padding:18px;
    }
    .sched-modal.show { display:flex; }
    .sched-card {
      width:100%; max-width:380px; background:#15181f; border:1px solid #232831;
      border-radius:14px; padding:18px;
    }
    .sched-card h3 { font-size:15px; }
    .sched-card label { display:block; font-size:11px; letter-spacing:.06em;
                         text-transform:uppercase; color:#8a8f99; margin:10px 0 4px; }
    .sched-card label small { text-transform:none; letter-spacing:0;
                                color:#5a5a5a; font-weight:normal; }
    .sched-card input {
      width:100%; padding:9px 11px; border-radius:8px; border:1px solid #2a2f3a;
      background:#0d0f14; color:#fff; font:13px monospace; outline:none;
    }
    .sched-card input:focus { border-color:#3390ec; }
    .sched-card button {
      flex:1; font:inherit; border:0; padding:10px 14px; border-radius:8px;
      background:#3390ec; color:#fff; font-size:13px; cursor:pointer;
    }
    .sched-card button.ghost {
      background:transparent; color:#5ac8fa; border:1px solid #5ac8fa;
    }
    /* Cast button gets a subtle highlight when an active CastSession is up. */
    #cast-btn.casting { background:rgba(41,151,255,.18); color:#fff;
                         border-color:#3390ec; }
    .url-box {
      margin-top:10px; padding:10px; background:#181b22; border:1px solid #232831;
      border-radius:8px; font-family:ui-monospace,Menlo,monospace; font-size:11px;
      word-break:break-all; color:#cfd2d8; user-select:all;
    }
    #inline-video {
      width:100%; aspect-ratio:16/9; background:#000; border-radius:10px;
      margin-top:14px; display:none;
    }
    .stream-type {
      display:inline-block; font-size:10px; font-weight:600; letter-spacing:.05em;
      padding:2px 6px; border-radius:4px; vertical-align:middle; margin-left:6px;
    }
    .stream-type.hls   { background:#1f5230; color:#a9e8be; }
    .stream-type.dash  { background:#5a3320; color:#fcc; }
    .stream-type.other { background:#3a3a3a; color:#ddd; }
    .exit-warning {
      background:#3a2a18; border:1px solid #5a3a20; border-radius:8px;
      padding:10px 12px; margin:10px 0 4px; font-size:12px; color:#fcd9a0;
      display:none;
    }
    .exit-warning.show { display:block; }
    .exit-warning code { background:rgba(0,0,0,.3); padding:1px 4px; border-radius:3px; }
    .source-picker {
      margin:10px 0 4px; padding:10px 12px;
      background:#15181f; border:1px solid #232831; border-radius:10px;
      font-size:12px; display:none;
    }
    .source-picker.show { display:block; }
    .source-picker .label {
      font-size:10px; letter-spacing:.08em; text-transform:uppercase;
      color:var(--tg-theme-hint-color,#8a8f99); margin-bottom:6px;
    }
    .source-picker select {
      width:100%; padding:7px 8px; border-radius:6px;
      background:#0d0f14; color:#fff; border:1px solid #2a2f3a; font-size:12px;
    }
    .source-picker .summary {
      font-size:11px; color:var(--tg-theme-hint-color,#8a8f99); margin-top:6px;
    }
    .hint { font-size:11px; color:var(--tg-theme-hint-color,#8a8f99); margin-top:14px; line-height:1.5; }
    .toast {
      position:fixed; left:50%; bottom:24px; transform:translateX(-50%);
      background:#222; color:#fff; padding:10px 16px; border-radius:8px;
      font-size:13px; z-index:50; opacity:0; transition:opacity .25s ease;
      pointer-events:none;
    }
    .toast.show { opacity:1; }
  </style>
</head>
<body>

<div class="back" onclick="if(window.history.length>1)history.back();else location.href='/iptv'">← Back to channels</div>

<div class="wrap">
  <div class="channel-h" id="header">
    <div class="logo" id="logo"><div style="font-size:28px;opacity:.55">📺</div></div>
    <div class="meta">
      <h1 id="name">Loading…</h1>
      <div class="sub" id="country-meta"></div>
    </div>
  </div>

  <div class="status-row" id="status-row"></div>
  <div class="exit-warning" id="exit-warning"></div>

  <div class="source-picker" id="source-picker">
    <div class="label">Source</div>
    <select id="source-select"></select>
    <div class="summary" id="source-summary"></div>
  </div>

  <div id="epg-block" style="display:none; background:#15181f; border:1px solid #232831; border-radius:10px; padding:10px 12px; margin-bottom:12px;">
    <div style="font-size:10px; letter-spacing:.08em; color:var(--tg-theme-hint-color,#8a8f99); text-transform:uppercase; margin-bottom:6px;">Programme guide</div>
    <div id="epg-content"></div>
  </div>

  <div class="actions">
    <button id="play-inline">▶ Play</button>
    <button class="ghost" id="cast-btn" style="display:none">📺 Cast</button>
    <button class="ghost" id="play-vlc">📤 Open in VLC / external player</button>
    <button class="ghost" id="copy-url">📋 Copy stream URL</button>
    <button class="ghost" id="probe-btn">🩺 Probe stream health</button>
    <button class="ghost" id="curate-btn" style="display:none">★ Curate this channel</button>
    <button class="warn" id="record-btn">⏺ Record 5 min</button>
    <button class="warn ghost" id="schedule-btn">📅 Schedule record…</button>
    <button class="ghost" id="report-bad-btn" title="Report this source as broken">👎 Report bad stream</button>
  </div>

  <!-- Schedule modal (date/time + duration + padding) -->
  <div class="sched-modal" id="sched-modal">
    <div class="sched-card">
      <h3 style="margin:0 0 10px">Schedule recording</h3>
      <div style="font-size:11.5px; color:#8a8f99; margin-bottom:10px" id="sched-channel"></div>
      <label>Start <small>(your local time)</small></label>
      <input type="datetime-local" id="sched-start" step="60">
      <label>Duration (minutes)</label>
      <input type="number" id="sched-duration" min="1" max="720" value="30">
      <label>Padding before / after (minutes)</label>
      <div style="display:flex; gap:8px">
        <input type="number" id="sched-pre"  min="0" max="60" value="0" placeholder="pre">
        <input type="number" id="sched-post" min="0" max="60" value="0" placeholder="post">
      </div>
      <div style="display:flex; gap:8px; margin-top:14px">
        <button id="sched-go">Schedule</button>
        <button class="ghost" id="sched-cancel">Cancel</button>
      </div>
      <div class="hint" id="sched-status" style="margin-top:8px; min-height:18px"></div>
    </div>
  </div>

  <div class="url-box" id="url-box">…</div>

  <video id="inline-video" controls playsinline></video>

  <div class="hint">
    <strong>▶ Play</strong> uses the in-app player (hls.js for HLS, dash.js for DASH) —
    works without any external app installed. First load fetches ~100 KB of JS from a
    CDN; cached after.<br>
    <strong>📤 Open in VLC</strong> hands the URL to your OS chooser with the right
    MIME type. Install <strong>VLC for Android</strong> (or VLC desktop) and it'll be
    offered directly — typically a smoother experience for long sessions or sharing to
    a TV. Without VLC installed, the chooser may fall to your browser which can't
    render <code>.mpd</code> and offers it as a download instead.
  </div>
</div>

<div class="toast" id="toast"></div>

<script>
const tg = window.Telegram?.WebApp;
if (tg) { tg.ready(); tg.expand(); }
const initData = tg?.initData || '';

const CHANNEL_ID = {{CHANNEL_ID_JSON}};

async function api(path, opts = {}) {
  opts.headers = Object.assign({}, opts.headers || {}, {
    'X-Init-Data': initData,
    ...(opts.body ? {'Content-Type': 'application/json'} : {}),
  });
  const r = await fetch(path, opts);
  if (!r.ok) {
    const text = await r.text();
    let detail = text;
    try { detail = JSON.parse(text).detail || detail; } catch (e) {}
    throw new Error(`${r.status}: ${detail}`);
  }
  return await r.json();
}

function toast(msg, ms=2000) {
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.classList.add('show');
  setTimeout(() => t.classList.remove('show'), ms);
}

function flag(code) {
  if (!code || code.length !== 2) return '🏳️';
  const A = 0x1F1E6;
  return String.fromCodePoint(A + code.charCodeAt(0) - 65)
       + String.fromCodePoint(A + code.charCodeAt(1) - 65);
}

function escapeHtml(s) { return String(s ?? '').replace(/[&<>"']/g, c =>
  ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }

let CHANNEL = null;

// In Phase 2 the play page handles BOTH formats of CHANNEL_ID:
//   • new logical-channel ids ("cna", "bbc-news") — use /v2/channels/<id>
//   • legacy source-prefixed ids ("iptv-org:CNA.sg") — use the old
//     /api/iptv/channels/<id> endpoint, single source
// Detection: presence of ":" in the id means it's source-prefixed.
let SOURCES = [];          // all alternates for the logical channel (v2 only)
let CURRENT_SOURCE_IDX = 0;

async function loadChannel() {
  const isLogical = !CHANNEL_ID.includes(':');
  try {
    if (isLogical) {
      const r = await api(`/api/iptv/v2/channels/${encodeURIComponent(CHANNEL_ID)}`);
      CHANNEL = r.channel;
      SOURCES = r.sources || [];
      if (!SOURCES.length) throw new Error('no sources for channel');
      // Default to highest-priority alive source (server already sorted).
      CURRENT_SOURCE_IDX = 0;
      // Merge the selected source's URL + source-source into CHANNEL so
      // the rest of the play-page logic (streamTypeOf, origin badge,
      // etc.) sees a coherent object.
      CHANNEL.url    = SOURCES[0].url;
      CHANNEL.source = SOURCES[0].source;
      CHANNEL.status = SOURCES[0].status;
    } else {
      // Legacy single-source path (kept so deep-link old URLs work).
      CHANNEL = await api(`/api/iptv/channels/${encodeURIComponent(CHANNEL_ID)}`);
      SOURCES = [];
    }
  } catch (e) {
    document.getElementById('name').textContent = 'Error: ' + e.message;
    return;
  }
  const kind = streamTypeOf(CHANNEL.url);
  const kindLabel = { hls: 'HLS', dash: 'DASH', ts: 'TS', other: '?' }[kind];
  document.getElementById('name').innerHTML =
    `${escapeHtml(CHANNEL.name)}<span class="stream-type ${kind === 'hls' ? 'hls' : (kind === 'dash' ? 'dash' : 'other')}">${kindLabel}</span>`;
  document.getElementById('country-meta').textContent =
    `${flag(CHANNEL.country||'')} ${CHANNEL.country||'?'} · ${(CHANNEL.categories||[]).join(', ') || 'no categories'}`;
  if (CHANNEL.logo) {
    document.getElementById('logo').innerHTML =
      `<img src="${CHANNEL.logo}" referrerpolicy="no-referrer" alt="">`;
  }
  const sr = document.getElementById('status-row');
  sr.innerHTML = '';
  function badge(label, kind='') {
    const b = document.createElement('div');
    b.className = 'badge' + (kind ? ' ' + kind : '');
    b.textContent = label;
    sr.appendChild(b);
  }
  badge(CHANNEL.status, CHANNEL.status === 'alive' ? 'alive' : (CHANNEL.status === 'dead' ? 'dead' : ''));
  if (CHANNEL.alive === false) badge('iptv-org: offline', 'dead');
  if (CHANNEL.last_check_at) badge('checked ' + CHANNEL.last_check_at.slice(0,16));
  if (CHANNEL.is_nsfw) badge('NSFW', 'dead');
  // Historical reliability (alive_count / probe_count over time).
  // Only meaningful once we have ≥3 samples; null hides the badge.
  if (CHANNEL.reliability !== null && CHANNEL.reliability !== undefined) {
    const pct = Math.round(CHANNEL.reliability * 100);
    const cls = pct >= 90 ? 'alive' : (pct >= 50 ? '' : 'dead');
    const icon = pct >= 90 ? '🔥' : (pct >= 50 ? '⚡' : '⚠');
    badge(`${icon} reliability ${pct}% (n=${CHANNEL.probe_count})`, cls);
  }
  document.getElementById('url-box').textContent = CHANNEL.url || '(no URL)';
  renderSourcePicker();
  _refreshCurateButton();
  maybeShowExitWarning();
  loadEpg();
}

// Source picker (Phase 2) — only shown when the channel has >1 source.
// Selecting a different source updates CHANNEL.url + URL-box + status
// row; the existing play buttons just use whatever is current.
function renderSourcePicker() {
  const picker = document.getElementById('source-picker');
  const select = document.getElementById('source-select');
  const summary = document.getElementById('source-summary');
  if (!SOURCES || SOURCES.length < 2) {
    picker.classList.remove('show');
    return;
  }
  picker.classList.add('show');
  select.innerHTML = '';
  SOURCES.forEach((s, i) => {
    const aliveTag = s.status === 'alive' ? '✓' :
                     s.status === 'dead'  ? '✗' : '·';
    const opt = document.createElement('option');
    opt.value = String(i);
    opt.textContent = `${aliveTag} ${s.source} · prio ${s.priority}`;
    if (i === CURRENT_SOURCE_IDX) opt.selected = true;
    select.appendChild(opt);
  });
  const aliveCount = SOURCES.filter(s => s.status === 'alive').length;
  summary.textContent = `${SOURCES.length} source(s) · ${aliveCount} alive · auto-failover on player error`;
  select.onchange = () => {
    CURRENT_SOURCE_IDX = parseInt(select.value, 10) || 0;
    const s = SOURCES[CURRENT_SOURCE_IDX];
    CHANNEL.url = s.url;
    CHANNEL.source = s.source;
    CHANNEL.status = s.status;
    document.getElementById('url-box').textContent = s.url;
    toast(`Switched to ${s.source}`, 1500);
  };
}

// Auto-failover — called from playInline when hls.js / dash.js emits
// a fatal error. Reports the current source as failed (server demotes
// it), advances to the next alternate, retries.
async function failoverToNextSource() {
  if (!SOURCES || CURRENT_SOURCE_IDX + 1 >= SOURCES.length) {
    toast('All sources failed.', 4000);
    return false;
  }
  const failed = SOURCES[CURRENT_SOURCE_IDX];
  // Best-effort report — don't block failover if the API errors
  try {
    await api(`/api/iptv/sources/${encodeURIComponent(failed.id)}/report_failure`,
              { method:'POST', body: '{}' });
  } catch (_) { /* shrug */ }
  CURRENT_SOURCE_IDX++;
  const next = SOURCES[CURRENT_SOURCE_IDX];
  CHANNEL.url = next.url;
  CHANNEL.source = next.source;
  CHANNEL.status = next.status;
  document.getElementById('url-box').textContent = next.url;
  const select = document.getElementById('source-select');
  if (select) select.value = String(CURRENT_SOURCE_IDX);
  toast(`${failed.source} failed → trying ${next.source}`, 2500);
  return true;
}

async function loadEpg() {
  let data;
  try {
    data = await api(`/api/iptv/channels/${encodeURIComponent(CHANNEL_ID)}/epg?n=4`);
  } catch (_e) { return; }
  const progs = data.programmes || [];
  if (!progs.length) return;
  const block = document.getElementById('epg-block');
  const content = document.getElementById('epg-content');
  const now = new Date();
  const html = progs.map((p, i) => {
    const start = new Date(p.start_utc);
    const end   = new Date(p.end_utc);
    const isNow = start <= now && now < end;
    const time  = start.toLocaleTimeString([], { hour: '2-digit', minute:'2-digit' });
    const title = escapeHtml(p.title);
    const desc  = p.description ? `<div style="font-size:11px; color:var(--tg-theme-hint-color,#8a8f99); margin-top:2px;">${escapeHtml(p.description.slice(0, 160))}${p.description.length > 160 ? '…' : ''}</div>` : '';
    const flag  = isNow ? '<span style="color:#a9e8be; font-weight:600;">● NOW</span>' : `<span style="color:#8a8f99;">${time}</span>`;
    return `<div style="padding:6px 0; ${i ? 'border-top:1px solid #232831;' : ''}">${flag} <strong>${title}</strong>${desc}</div>`;
  }).join('');
  content.innerHTML = html;
  block.style.display = 'block';
}

// Compare the channel's country against the current effective exit
// country (CF-IPCountry / direct client). Surface a banner when they
// don't match so the user knows up-front that this needs a different
// exit node / VPN. Cached for the page lifetime; no spam.
let _whereCache = null;
async function fetchWhereami() {
  if (_whereCache !== null) return _whereCache;
  try { _whereCache = await api('/api/iptv/whereami'); }
  catch { _whereCache = {}; }
  return _whereCache;
}

async function maybeShowExitWarning() {
  if (!CHANNEL?.country) return;
  const w = await fetchWhereami();
  const here = (w.country || '').toUpperCase();
  const want = CHANNEL.country.toUpperCase();
  if (!here || here === want) return;   // either unknown or we're already in-region
  const isGeo = /\[Geo[- ]?blocked\]/i.test(CHANNEL.name || '');
  if (!isGeo) return;                    // channel doesn't claim geo-restriction; skip
  const el = document.getElementById('exit-warning');
  el.innerHTML = `⚠️ This channel is tagged <strong>[Geo-blocked]</strong> for ${flag(want)} <code>${want}</code>,
                  but you're currently exiting via ${flag(here)} <code>${here}</code>. Streaming may fail —
                  switch Tailscale exit-node to a ${flag(want)} node, or try anyway.`;
  el.classList.add('show');
}

// Are we running inside the SMDL-IPTV APK shell (custom UA suffix) vs
// real Telegram / a system browser? The APK's WebView has a custom
// shouldOverrideUrlLoading that handles HLS/DASH/TS with MIME-typed
// intents preferring VLC; navigating the WebView triggers it. In other
// runtimes, tg.openLink is the right path.
function isApk() {
  return /SMDL-IPTV\//.test(navigator.userAgent || '');
}

document.getElementById('play-vlc')?.addEventListener('click', async () => {
  if (!CHANNEL?.url) return toast('No URL');
  // YouTube-live needs the resolved m3u8 too — VLC can't deep-link into
  // youtube.com/@handle/live.
  const url = await resolveStreamUrl();
  if (!url) return;
  if (isApk()) {
    window.location.href = url;
  } else if (tg?.openLink) {
    tg.openLink(url, { try_instant_view: false });
  } else {
    window.open(url, '_blank');
  }
});

// Stream-type detection from URL extension. Content-Type probe would be
// more authoritative but adds a round-trip; the URL is right ~95% of the time.
function streamTypeOf(url) {
  if (!url) return 'other';
  const u = url.toLowerCase().split('?')[0].split('#')[0];
  if (u.endsWith('.m3u8') || u.endsWith('.m3u')) return 'hls';
  if (u.endsWith('.mpd')) return 'dash';
  if (u.endsWith('.ts'))  return 'ts';
  return 'other';
}

// Host-based heuristic: is this stream coming from the broadcaster's
// official CDN, or a third-party re-streamer? We don't pretend to be
// exhaustive — just enough so users can spot the obvious risks.
const _OFFICIAL_HOSTS = [
  // major CDNs broadcasters actually use
  'cloudfront.net','akamaized.net','akamai.net','akamaihd.net','fastly.net','fastly.com',
  // streaming-platform branded
  'amagi.tv','amg01082','amg18481','amg02159','playouts.now','playoutshq','amagi-cdn',
  'streamized.net','mediacorp','mncdn.com',
  // broadcaster-owned
  'bbc.co.uk','rai.it','akamaihd.net','iheart.com','tvnz.co.nz','sbs.com.au',
  'abc.net.au','rainz.akamaized.net','live-video.net','wzm.live',
];
const _RESTREAM_HOSTS = [
  'viloud.tv','indihuy','lordstreams','stitcher.com.br','xtreamer','spaghett',
  'streamtape','dropbox','githubusercontent.com','ahmsville',
];
function originOf(url) {
  if (!url) return { kind: 'unknown', host: '' };
  let host = '';
  try { host = new URL(url).hostname.toLowerCase(); } catch { return { kind:'unknown', host:'' }; }
  for (const m of _OFFICIAL_HOSTS) if (host.includes(m)) return { kind: 'official', host };
  for (const m of _RESTREAM_HOSTS) if (host.includes(m)) return { kind: 'restream', host };
  return { kind: 'unknown', host };
}

// Lazy-load a script tag once; resolves when the global is ready.
function loadScript(src, globalCheck) {
  return new Promise((resolve, reject) => {
    if (globalCheck && globalCheck()) return resolve();
    const s = document.createElement('script');
    s.src = src; s.async = true;
    s.onload = () => resolve();
    s.onerror = () => reject(new Error('failed to load ' + src));
    document.head.appendChild(s);
  });
}

// For YouTube-live channels the stored URL is the @handle landing page;
// we need to resolve it to a fresh m3u8 before playback. Cached server-
// side for 30 min so repeat plays don't re-spawn yt-dlp.
async function resolveStreamUrl() {
  if (!CHANNEL) return null;
  if (CHANNEL.source !== 'youtube-live') return CHANNEL.url;
  toast('Resolving YouTube live stream…', 2500);
  try {
    const r = await api(`/api/iptv/channels/${encodeURIComponent(CHANNEL_ID)}/resolve_url`);
    return r.url || null;
  } catch (e) {
    toast('Resolve failed: ' + e.message, 4000);
    return null;
  }
}

async function playInline() {
  if (!CHANNEL?.url) return toast('No URL');
  const url = await resolveStreamUrl();
  if (!url) return;
  const v = document.getElementById('inline-video');
  v.style.display = 'block';
  const kind = streamTypeOf(url);
  // Override CHANNEL.url for the rest of this handler so the existing
  // hls.js / dash.js / native branches use the resolved URL.
  const origUrl = CHANNEL.url;
  CHANNEL.url = url;
  try {
    await _playInlineCore(kind, v);
  } finally {
    CHANNEL.url = origUrl;
  }
}

async function _playInlineCore(kind, v) {

  if (kind === 'hls') {
    // Safari/WebKit play HLS natively; everywhere else needs hls.js.
    const native = v.canPlayType('application/vnd.apple.mpegurl');
    if (native) {
      v.src = CHANNEL.url;
      v.play().catch(e => toast('Playback failed: ' + e.message, 3500));
      return;
    }
    try {
      await loadScript('https://cdn.jsdelivr.net/npm/hls.js@1.5.13/dist/hls.min.js',
                        () => window.Hls);
    } catch (e) { return toast('hls.js failed to load: ' + e.message, 4000); }
    if (!window.Hls?.isSupported()) {
      return toast('HLS not supported on this device — try VLC handoff', 4000);
    }
    const hls = new window.Hls({ enableWorker: true });
    hls.loadSource(CHANNEL.url);
    hls.attachMedia(v);
    let _failovered = false;
    hls.on(window.Hls.Events.ERROR, async (_e, data) => {
      if (!data.fatal) return;
      if (_failovered) {
        toast('HLS error: ' + (data.details || data.type), 4500);
        return;
      }
      _failovered = true;
      try { hls.destroy(); } catch (_) {}
      const ok = await failoverToNextSource();
      if (ok) await _playInlineCore('hls', v);   // recurse with new URL
    });
    v.play().catch(() => {});
    return;
  }

  if (kind === 'dash') {
    try {
      await loadScript('https://cdn.dashjs.org/v4.7.4/dash.all.min.js',
                        () => window.dashjs);
    } catch (e) { return toast('dash.js failed to load: ' + e.message, 4000); }
    if (!window.dashjs) return toast('dash.js missing', 3500);
    const player = window.dashjs.MediaPlayer().create();
    player.initialize(v, CHANNEL.url, true);
    let _failovered = false;
    player.on(window.dashjs.MediaPlayer.events.ERROR, async e => {
      if (_failovered) {
        toast('DASH error: ' + (e.error?.message || JSON.stringify(e)), 4500);
        return;
      }
      _failovered = true;
      try { player.destroy(); } catch (_) {}
      const ok = await failoverToNextSource();
      if (ok) await _playInlineCore('dash', v);
    });
    return;
  }

  // TS / unknown — hand to the <video> tag and hope. Many .ts streams
  // need ffmpeg/VLC; the inline player will likely fail and the user
  // should use the "Open in VLC" button instead.
  v.src = CHANNEL.url;
  v.play().catch(() => toast(`Inline play not supported for ${kind} — use VLC handoff`, 4000));
}

document.getElementById('play-inline').addEventListener('click', playInline);

document.getElementById('copy-url').addEventListener('click', async () => {
  if (!CHANNEL?.url) return toast('No URL');
  try {
    await navigator.clipboard.writeText(CHANNEL.url);
    toast('URL copied — paste in VLC → Media → Open Network');
  } catch (e) {
    toast('Clipboard blocked — long-press the URL box');
  }
});

document.getElementById('probe-btn').addEventListener('click', async () => {
  const btn = document.getElementById('probe-btn');
  btn.disabled = true; btn.textContent = '🩺 Probing…';
  try {
    const r = await api(`/api/iptv/channels/${encodeURIComponent(CHANNEL_ID)}/probe`, { method:'POST', body:'{}' });
    CHANNEL = r;
    toast(r.status === 'alive' ? '✅ Stream alive' : `❌ ${r.last_error || 'dead'}`, 3500);
    loadChannel();
  } catch (e) {
    toast('Probe failed: ' + e.message, 3500);
  } finally {
    btn.disabled = false; btn.textContent = '🩺 Probe stream health';
  }
});

// ── ★ Curate this channel (owner-only) ──────────────────────────
// Visible only on logical-channel pages (not legacy source-prefixed
// IDs). Toggles depending on current curated state. The endpoint
// returns 403 for beta users; the catch surfaces a friendly toast.
function _refreshCurateButton() {
  const btn = document.getElementById('curate-btn');
  if (!btn) return;
  const isLogical = !CHANNEL_ID.includes(':');
  if (!isLogical) { btn.style.display = 'none'; return; }
  btn.style.display = '';
  const alreadyCurated = !!(CHANNEL && CHANNEL.is_curated);
  btn.textContent = alreadyCurated
    ? '★ Re-save curated entry'
    : '★ Curate this channel';
}

document.getElementById('curate-btn')?.addEventListener('click', async () => {
  const btn = document.getElementById('curate-btn');
  const orig = btn.textContent;
  btn.disabled = true; btn.textContent = '★ Saving…';
  try {
    const r = await api(`/api/iptv/channels/${encodeURIComponent(CHANNEL_ID)}/curate`, {
      method: 'POST', body: JSON.stringify({}),
    });
    const entry = r.curated_entry || {};
    toast(`★ Curated: ${entry.name || CHANNEL_ID} (${(entry.sources||[]).length} sources)`, 4500);
    // Reload channel detail so the badges reflect curated=true
    await loadChannel();
  } catch (e) {
    if (/403/.test(e.message)) {
      toast('Owner-only — beta users can\\'t edit the curated YAML', 4000);
    } else {
      toast('Curate failed: ' + e.message, 4000);
    }
  } finally {
    btn.disabled = false; btn.textContent = orig;
  }
});

document.getElementById('record-btn').addEventListener('click', async () => {
  if (!confirm('Start a 5-minute recording? Saved to /downloads/iptv/.')) return;
  try {
    const r = await api(`/api/iptv/channels/${encodeURIComponent(CHANNEL_ID)}/record`, {
      method:'POST', body: JSON.stringify({ duration_min: 5 }),
    });
    toast(`⏺ Recording ${r.duration_min}m → ${r.output_path.split('/').pop()}`, 6000);
  } catch (e) {
    toast('Record failed: ' + e.message, 3500);
  }
});

// ── Report bad stream (closes the feedback loop on stale dead sources) ──
document.getElementById('report-bad-btn')?.addEventListener('click', async () => {
  const src = (SOURCES && SOURCES[CURRENT_SOURCE_IDX]) || null;
  if (!src) { toast('No source to report.', 2500); return; }
  if (!confirm(`Report ${src.source} (${src.url.slice(0,50)}…) as bad?\n\n` +
               'This demotes the source so failover prefers others. ' +
               'Useful when probes say "alive" but the stream is silent / black-screen.')) return;
  try {
    await api(`/api/iptv/sources/${encodeURIComponent(src.id)}/report_failure`,
              { method:'POST', body: '{}' });
    toast('✓ Source reported. Failing over…', 3000);
    // Best-effort: bump to the next source if there is one (same UX as
    // auto-failover, but user-initiated).
    if (SOURCES && CURRENT_SOURCE_IDX + 1 < SOURCES.length) await failoverToNextSource();
  } catch (e) {
    toast('Report failed: ' + e.message, 3000);
  }
});

// ── Schedule recording modal ──
const _schedModal = document.getElementById('sched-modal');
document.getElementById('schedule-btn')?.addEventListener('click', () => {
  if (!_schedModal) return;
  // Default start = top of next hour, local time
  const d = new Date(); d.setMinutes(0, 0, 0); d.setHours(d.getHours() + 1);
  const pad = n => String(n).padStart(2,'0');
  const local = `${d.getFullYear()}-${pad(d.getMonth()+1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
  document.getElementById('sched-start').value = local;
  document.getElementById('sched-channel').textContent =
    `${CHANNEL?.name || CHANNEL_ID}`;
  document.getElementById('sched-status').textContent = '';
  _schedModal.classList.add('show');
});
document.getElementById('sched-cancel')?.addEventListener('click', () =>
  _schedModal && _schedModal.classList.remove('show'));
document.getElementById('sched-go')?.addEventListener('click', async () => {
  const localVal = document.getElementById('sched-start').value;
  const dur      = parseInt(document.getElementById('sched-duration').value, 10);
  const pre      = parseInt(document.getElementById('sched-pre').value, 10) || 0;
  const post     = parseInt(document.getElementById('sched-post').value, 10) || 0;
  const st = document.getElementById('sched-status');
  if (!localVal || !dur || dur < 1) { st.textContent = '⚠ Invalid input.'; return; }
  // datetime-local is local time without TZ — convert to UTC ISO.
  const utc = new Date(localVal).toISOString().replace(/\.\d{3}Z$/, 'Z');
  st.textContent = 'Scheduling…';
  try {
    const r = await api('/api/iptv/schedule', {
      method:'POST',
      body: JSON.stringify({
        channel_id: CHANNEL_ID,
        start_at: utc,
        duration_min: dur,
        padding_pre: pre,
        padding_post: post,
      }),
    });
    st.innerHTML = `✓ Scheduled #${r.id} for ${escapeHtml(utc)}.`;
    setTimeout(() => _schedModal.classList.remove('show'), 1800);
  } catch (e) { st.textContent = '✗ ' + e.message; }
});

// ── Chromecast / RemotePlayback hookup ──
//
// Cast SDK lazy-loads; once available we ask if any device on the LAN
// can play this URL. If yes, show the Cast button. On click, hand the
// stream URL to the receiver (works for HLS m3u8 + plain MP4; DASH
// needs a custom receiver app we don't host).
(function _initCast() {
  // Only relevant on https origins served from a real domain — the cast
  // sender refuses to initialise on bare localhost / IP.
  if (location.protocol !== 'https:') return;
  const ctx = window.cast?.framework?.CastContext;
  function activate() {
    const btn = document.getElementById('cast-btn');
    if (!btn) return;
    btn.style.display = '';
    btn.addEventListener('click', () => {
      try {
        const session = cast.framework.CastContext.getInstance().getCurrentSession();
        const url = CHANNEL?.url;
        if (!url) { toast('No stream URL yet.', 2500); return; }
        if (session) {
          const media = new chrome.cast.media.MediaInfo(url,
            url.includes('.m3u8') ? 'application/x-mpegURL' : 'video/mp4');
          const request = new chrome.cast.media.LoadRequest(media);
          session.loadMedia(request)
            .then(() => { btn.classList.add('casting'); toast('Casting…', 2500); })
            .catch(e => toast('Cast load failed: ' + e, 3500));
        } else {
          cast.framework.CastContext.getInstance().requestSession()
            .then(() => btn.click())   // re-attempt with the new session
            .catch(e => toast('No cast device found.', 2500));
        }
      } catch (e) { toast('Cast SDK error: ' + e.message, 3000); }
    });
  }
  // The Cast SDK exposes itself via a global callback when ready.
  window['__onGCastApiAvailable'] = function (isAvailable) {
    if (!isAvailable) return;
    cast.framework.CastContext.getInstance().setOptions({
      receiverApplicationId: chrome.cast.media.DEFAULT_MEDIA_RECEIVER_APP_ID,
      autoJoinPolicy: chrome.cast.AutoJoinPolicy.ORIGIN_SCOPED,
    });
    activate();
  };
  // Inject the sender SDK script.
  const s = document.createElement('script');
  s.src = 'https://www.gstatic.com/cv/js/sender/v1/cast_sender.js?loadCastFramework=1';
  s.async = true;
  document.head.appendChild(s);
})();

// ── Mobile gestures on the inline video ──
//
// Vertical swipe = volume (±0.05 per 6vh dragged).
// Horizontal swipe = ±10s seek (HLS live edge is unseekable; harmless).
// Double-tap left/right edge of the player = ±10s seek (Netflix-style).
(function _initGestures() {
  const v = document.getElementById('inline-video');
  if (!v) return;
  let t0 = null, startX = 0, startY = 0, startVol = 1, mode = null;
  const VOL_SCALE = 0.005;   // volume delta per pixel dragged
  const SEEK_SCALE = 0.1;    // seconds per pixel dragged
  v.addEventListener('touchstart', e => {
    if (e.touches.length !== 1) return;
    const t = e.touches[0]; t0 = Date.now();
    startX = t.clientX; startY = t.clientY; startVol = v.volume;
    mode = null;
  }, { passive:true });
  v.addEventListener('touchmove', e => {
    if (e.touches.length !== 1 || t0 === null) return;
    const t = e.touches[0];
    const dx = t.clientX - startX, dy = t.clientY - startY;
    if (mode === null) {
      if (Math.abs(dx) > 12 || Math.abs(dy) > 12)
        mode = Math.abs(dy) > Math.abs(dx) ? 'vol' : 'seek';
    }
    if (mode === 'vol') {
      v.volume = Math.min(1, Math.max(0, startVol - dy * VOL_SCALE));
    } else if (mode === 'seek' && !isNaN(v.duration) && isFinite(v.duration)) {
      const next = Math.min(v.duration, Math.max(0, v.currentTime + dx * SEEK_SCALE));
      v.currentTime = next;
    }
  }, { passive:true });
  v.addEventListener('touchend', () => { t0 = null; mode = null; });
  // Double-tap-to-seek edges.
  let lastTap = 0, lastTapX = 0;
  v.addEventListener('touchend', e => {
    const now = Date.now();
    const rect = v.getBoundingClientRect();
    const x = (e.changedTouches[0]?.clientX ?? 0) - rect.left;
    if (now - lastTap < 280 && Math.abs(x - lastTapX) < 40) {
      if (!isNaN(v.duration) && isFinite(v.duration)) {
        const left = x < rect.width / 2;
        v.currentTime = Math.max(0, Math.min(v.duration, v.currentTime + (left ? -10 : 10)));
        toast(left ? '⏪ -10s' : '⏩ +10s', 1000);
      }
    }
    lastTap = now; lastTapX = x;
  });
})();

// ── Play-history beacon — fires when a stream actually starts ──
let _beaconFired = false;
async function _maybeFirePlayBeacon() {
  if (_beaconFired) return;
  _beaconFired = true;
  try {
    const src = (SOURCES && SOURCES[CURRENT_SOURCE_IDX]) || null;
    await api(`/api/iptv/channels/${encodeURIComponent(CHANNEL_ID)}/played`, {
      method:'POST',
      body: JSON.stringify({ source_id: src ? src.id : null }),
    });
  } catch (_) { /* shrug */ }
}
document.getElementById('inline-video')?.addEventListener('playing',
  _maybeFirePlayBeacon, { once: true });
document.getElementById('play-vlc')?.addEventListener('click',
  _maybeFirePlayBeacon);

loadChannel();
</script>
</body></html>
"""


# WebView aggressively caches HTML — without these headers, the user
# is stuck on whatever version was first loaded into the cache (we hit
# this in the wild: phone showed pre-country-quick-row layout days after
# the feature shipped). `no-store` prevents both disk + memory caching.
_NO_CACHE_HEADERS = {
    "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
    "Pragma": "no-cache",
    "Expires": "0",
}


_RECORDINGS_HTML = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
  <title>SMDL · IPTV Recordings</title>
  <script src="https://telegram.org/js/telegram-web-app.js"></script>
  <style>
    :root { color-scheme: dark light; }
    * { box-sizing: border-box; }
    html, body { margin:0; padding:0; background:#0f1115; color:#e8eaed;
                 font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif; }
    .topbar {
      position:sticky; top:0; z-index:10;
      padding:12px 14px; background:rgba(15,17,21,.92);
      backdrop-filter:saturate(180%) blur(8px);
      border-bottom:1px solid #1d2129;
      display:flex; align-items:center; gap:10px;
    }
    .topbar a.back { color:#5ac8fa; text-decoration:none; font-size:14px; }
    .topbar h1 { font-size:16px; margin:0 auto; font-weight:600; }
    .topbar .spacer { width:60px; }
    .summary {
      padding:10px 14px; font-size:11px; color:#8a8f99;
      border-bottom:1px solid #1d2129; background:#0c0e13;
    }
    .empty { padding:48px 16px; text-align:center; color:#8a8f99; font-size:13px; }
    .list { padding:8px 14px 90px; }
    .row {
      background:#15181f; border:1px solid #232831; border-radius:10px;
      padding:11px 13px; margin-bottom:8px;
    }
    .row .h { display:flex; gap:8px; align-items:center; }
    .row .h .name { font-weight:600; font-size:14px; flex:1;
                     overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
    .row .h .status {
      font-size:10px; padding:3px 7px; border-radius:5px; letter-spacing:.04em;
      text-transform:uppercase; font-weight:600;
    }
    .status.queued   { background:#3a3a3a; color:#cfd2d8; }
    .status.recording{ background:#5a3a18; color:#ffd9a0; }
    .status.finished { background:#1f5230; color:#a9e8be; }
    .status.failed   { background:#5a2020; color:#f5b4b4; }
    .row .meta {
      display:flex; gap:6px; flex-wrap:wrap; font-size:11px;
      color:#8a8f99; margin-top:6px;
    }
    .row .meta .tag { background:#0d0f14; padding:2px 6px; border-radius:4px; }
    .row .meta .tag.size { color:#9ec9ec; }
    .row .file {
      font-family:ui-monospace,Menlo,monospace; font-size:10px;
      color:#cfd2d8; background:#0d0f14; padding:6px 8px; border-radius:6px;
      word-break:break-all; margin-top:8px;
    }
    .row .err {
      font-size:11px; color:#f5b4b4; background:#2a1818;
      padding:6px 8px; border-radius:6px; margin-top:6px;
    }
    .progress {
      height:3px; background:#15181f; border-radius:2px; margin-top:8px;
      overflow:hidden;
    }
    .progress .bar {
      height:100%; background:#5ac8fa; transition: width .5s linear;
    }
    .live-dot {
      display:inline-block; width:6px; height:6px; border-radius:50%;
      background:#34c759; margin-right:4px;
      animation: pulse 1.4s ease-in-out infinite;
    }
    @keyframes pulse { 50% { opacity:.4; } }
  </style>
</head>
<body>

<div class="topbar">
  <a class="back" href="/iptv">← Live TV</a>
  <h1>📼 Recordings</h1>
  <div class="spacer"></div>
</div>

<div class="summary" id="summary">Loading…</div>

<div class="list" id="list"><div class="empty">Loading…</div></div>

<script>
const tg = window.Telegram?.WebApp;
if (tg) { tg.ready(); tg.expand(); }
const initData = tg?.initData || '';

async function api(path, opts = {}) {
  opts.credentials = 'same-origin';
  opts.headers = Object.assign({}, opts.headers || {}, {
    'X-Init-Data': initData,
    ...(opts.body ? {'Content-Type': 'application/json'} : {}),
  });
  const r = await fetch(path, opts);
  if (!r.ok) {
    let detail = r.statusText;
    try { detail = (await r.json()).detail || detail; } catch (_) {}
    throw new Error(`${r.status}: ${detail}`);
  }
  return await r.json();
}

function escapeHtml(s) {
  return String(s ?? '').replace(/[&<>"']/g, c =>
    ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}

function fmtBytes(n) {
  if (!n || n < 0) return '—';
  const u = ['B','KB','MB','GB']; let i = 0; let v = n;
  while (v >= 1024 && i < u.length-1) { v /= 1024; i++; }
  return `${v.toFixed(v < 10 ? 1 : 0)} ${u[i]}`;
}

function fmtElapsed(startIso, endIso) {
  if (!startIso) return '—';
  const start = new Date(startIso).getTime();
  const end   = endIso ? new Date(endIso).getTime() : Date.now();
  const sec   = Math.max(0, Math.floor((end - start) / 1000));
  if (sec < 60) return sec + 's';
  const m = Math.floor(sec / 60); const s = sec % 60;
  if (m < 60) return `${m}m ${s.toString().padStart(2,'0')}s`;
  const h = Math.floor(m / 60);
  return `${h}h ${(m % 60).toString().padStart(2,'0')}m`;
}

function fmtWhen(iso) {
  if (!iso) return '—';
  const d = new Date(iso);
  return d.toLocaleString([], { dateStyle:'short', timeStyle:'short' });
}

async function refresh() {
  let data;
  try {
    data = await api('/api/iptv/recordings?limit=100');
  } catch (e) {
    document.getElementById('list').innerHTML =
      `<div class="empty">Error: ${escapeHtml(e.message)}</div>`;
    document.getElementById('summary').textContent = '';
    return;
  }
  const rows = data.recordings || [];
  const live  = rows.filter(r => r.status === 'recording').length;
  const queued= rows.filter(r => r.status === 'queued').length;
  const ok    = rows.filter(r => r.status === 'finished').length;
  const bad   = rows.filter(r => r.status === 'failed').length;
  document.getElementById('summary').innerHTML =
    `${rows.length} total · ` +
    (live  ? `<span class="live-dot"></span>${live} recording · ` : '') +
    (queued? `${queued} queued · ` : '') +
    `${ok} finished · ${bad} failed`;
  if (!rows.length) {
    document.getElementById('list').innerHTML =
      `<div class="empty">No recordings yet. Tap <strong>⏺ Record 5 min</strong> on any channel's play page to start one.</div>`;
    return;
  }
  document.getElementById('list').innerHTML = rows.map(r => {
    const status   = r.status || 'queued';
    const fileName = (r.output_path || '').split('/').pop();
    const duration = (r.duration_min || 5) * 60;
    const elapsedNow = (status === 'recording' && r.started_at)
      ? Math.floor((Date.now() - new Date(r.started_at).getTime()) / 1000)
      : 0;
    const pct = (status === 'recording' && duration)
      ? Math.min(100, Math.floor(100 * elapsedNow / duration))
      : (status === 'finished' ? 100 : 0);
    const progressBar = (status === 'recording')
      ? `<div class="progress"><div class="bar" style="width:${pct}%"></div></div>`
      : '';
    const errBlock = (status === 'failed' && r.error)
      ? `<div class="err">${escapeHtml(r.error)}</div>`
      : '';
    const fileBlock = (status === 'finished' && r.output_path)
      ? `<div class="file">${escapeHtml(r.output_path)}</div>`
      : '';
    return `
      <div class="row" data-id="${r.id}">
        <div class="h">
          <div class="name">${escapeHtml(r.channel_id || '')}</div>
          <div class="status ${status}">${status}</div>
        </div>
        <div class="meta">
          <span class="tag">${r.duration_min}m target</span>
          <span class="tag">elapsed ${fmtElapsed(r.started_at, r.finished_at)}</span>
          ${r.requested_at ? `<span class="tag">requested ${fmtWhen(r.requested_at)}</span>` : ''}
          ${fileName ? `<span class="tag size">${escapeHtml(fileName)}</span>` : ''}
        </div>
        ${progressBar}
        ${errBlock}
        ${fileBlock}
      </div>
    `;
  }).join('');
}

let _liveTimer = null;
async function tick() {
  await refresh();
  // Auto-refresh every 3s if at least one recording is in flight;
  // every 30s otherwise (catch the transition queued → recording → finished).
  const isLive = document.querySelector('.status.recording, .status.queued');
  const delay = isLive ? 3000 : 30000;
  _liveTimer = setTimeout(tick, delay);
}
tick();
</script>
</body></html>
"""


@router.get("/iptv/recordings", response_class=HTMLResponse)
async def iptv_recordings_page():
    return HTMLResponse(_RECORDINGS_HTML, headers=_NO_CACHE_HEADERS)


@router.get("/iptv", response_class=HTMLResponse)
async def iptv_browse_page():
    """Top-level browse page. Owner-only check is enforced by the JSON
    APIs the page calls (not by this static HTML responder) — same
    pattern miniapp.py / sticker_routes.py use for their HTML routes."""
    return HTMLResponse(_BROWSE_HTML, headers=_NO_CACHE_HEADERS)


@router.get("/iptv/play/{channel_id}", response_class=HTMLResponse)
async def iptv_play_page(channel_id: str):
    import json
    safe = json.dumps(channel_id)  # JSON-string-encoded, safe for inline JS
    html = _PLAY_HTML.replace("{{CHANNEL_ID_JSON}}", safe)
    return HTMLResponse(html, headers=_NO_CACHE_HEADERS)
