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
import logging
import os
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from pydantic import BaseModel

import aiosqlite

from . import database as _db
from . import iptv as _iptv
from . import miniapp as _mini   # reuse _verify

logger = logging.getLogger(__name__)

router = APIRouter()


# ── JSON ────────────────────────────────────────────────────────────


class RefreshBody(BaseModel):
    country: str | None = None
    include_nsfw: bool = False
    source: str | None = None  # 'iptv-org' | 'free-tv' | 'mjh-all' | None=all


@router.post("/api/iptv/refresh")
async def iptv_refresh(body: RefreshBody, request: Request):
    """Refresh one source (if body.source is set) or all sources (if not).
    Returns a per-source summary list."""
    await _mini._verify(request)
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
    await _mini._verify(request)
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
    await _mini._verify(request)
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
    await _mini._verify(request)
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
    await _mini._verify(request)
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
    await _mini._verify(request)
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
    await _mini._verify(request)
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
    await _mini._verify(request)
    ch = await _iptv.get_channel(channel_id)
    if not ch:
        raise HTTPException(status_code=404, detail="channel not found")
    return ch.to_dict()


@router.get("/api/iptv/channels/{channel_id}/resolve_url")
async def iptv_channel_resolve_url(channel_id: str, request: Request):
    """Return a freshly-resolved playable URL for the channel.

    For static-URL sources (iptv-org, free-tv, mjh, …) this just
    passes through the stored URL — cheap, ~1ms.

    For source='youtube-live' it invokes yt-dlp on the @handle page
    to get the current m3u8 manifest (YouTube live URLs are signed
    and rotate; can't store them statically). Results cached in-memory
    for 30 min so rapid taps don't fan out to multiple yt-dlp procs."""
    await _mini._verify(request)
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
    await _mini._verify(request)
    return _iptv.start_probe_all(
        source=body.source, country=body.country,
        concurrency=max(1, min(int(body.concurrency or 32), 128)),
        timeout_s=float(body.timeout_s or 6.0),
        force_recheck=bool(body.force_recheck),
        fresh_window_hours=max(1, min(int(body.fresh_window_hours or 6), 168)),
    )


@router.get("/api/iptv/probe_all/status")
async def iptv_probe_all_status(request: Request):
    await _mini._verify(request)
    return _iptv.probe_all_status()


@router.get("/api/iptv/channels/{channel_id}/epg")
async def iptv_channel_epg(channel_id: str, request: Request, n: int = 3):
    """Return now + next-N programmes for the channel.  Derives the
    tvg_id from the channel id by stripping the `<source>:` prefix."""
    await _mini._verify(request)
    tvg = channel_id.split(":", 1)[-1] if ":" in channel_id else channel_id
    progs = await _iptv.get_now_next(tvg, lookahead_count=max(1, min(int(n or 3), 20)))
    return {"tvg_id": tvg, "programmes": progs}


class EpgRefreshBody(BaseModel):
    source: str | None = None   # None=all EPG sources


@router.post("/api/iptv/epg/refresh")
async def iptv_epg_refresh(body: EpgRefreshBody, request: Request):
    """Refresh one or all EPG feeds — separate from channel refresh
    because EPG fetches are heavier (multi-MB XMLTV gz)."""
    await _mini._verify(request)
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
    await _mini._verify(request)
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
    await _mini._verify(request)
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
    await _mini._verify(request)
    return {"recordings": await _iptv.list_iptv_recordings(limit=limit)}


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
  <div class="actions-row" id="country-quick-row"></div>

  <!-- Drawer-search: filters the chip lists below (countries, sources,
       categories). Distinct from the top-bar search which filters the
       channel grid. -->
  <div style="padding:8px 14px 0;">
    <input id="drawer-filter" type="search" placeholder="Filter lists (country, source, …)"
      style="width:100%; padding:8px 10px; border-radius:8px; border:1px solid #2a2f3a;
             background:#15181f; color:#fff; font-size:12px;">
  </div>

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

  <div class="section-h">Country</div>
  <div class="chip-row" id="country-chips"></div>

  <div class="section-h">Source</div>
  <div class="chip-row" id="source-chips"></div>

  <div class="section-h">Category</div>
  <div class="chip-row" id="category-chips"></div>

  <div class="section-h" style="margin-top:12px">
    <a href="/app" style="color:#5ac8fa; text-decoration:none;">← Back to SMDL</a>
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

  <div class="section-h result-h" id="result-h">Channels</div>
  <div class="grid" id="grid"><div class="loading">Loading…</div></div>
</main>

<div class="toast" id="toast"></div>

<script>
const tg = window.Telegram?.WebApp;
if (tg) { tg.ready(); tg.expand(); }
const initData = tg?.initData || '';

const STATE_KEY = 'smdl_iptv_filters_v1';
const FAV_KEY = 'smdl_iptv_favorites_v1';
const _defaultState = {
  country: null, category: null, source: null,
  status: null, q: '', favorites_only: false,
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
  for (const s of sources) {
    const label = sourceLabel(s.id) + ` (${s.count})`;
    sc.appendChild(makeChip(label, s.id, state.source === s.id, 'source'));
  }
  const cc = document.getElementById('country-chips');
  cc.innerHTML = '';
  cc.appendChild(makeChip('All', null, state.country === null, 'country'));
  // Sort by full display name (Singapore, United States, ...) rather
  // than by channel count — easier to find a specific country in a
  // 100+ entry list. Top entries used to be 🇺🇸 US (1462) → useless if
  // you actually want 🇸🇬 SG. Counts stay on the chip for context.
  const sortedCountries = countries.slice().sort((a, b) =>
    countryName(a.code).localeCompare(countryName(b.code)));
  for (const c of sortedCountries) {
    const name = countryName(c.code);
    const label = `${flag(c.code)} ${name === c.code ? c.code : name + ' · ' + c.code} (${c.count})`;
    cc.appendChild(makeChip(label, c.code, state.country === c.code, 'country'));
  }
  const catc = document.getElementById('category-chips');
  catc.innerHTML = '';
  catc.appendChild(makeChip('All', null, state.category === null, 'category'));
  for (const cat of categories.slice(0, 30)) {
    catc.appendChild(makeChip(`${cat.name} (${cat.count})`,
                                cat.name, state.category === cat.name, 'category'));
  }
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
    _autoCloseDrawerIfMobile();
  });
  return el;
}

async function loadChannels() {
  const grid = document.getElementById('grid');
  grid.innerHTML = '<div class="loading">Loading…</div>';
  const params = new URLSearchParams();
  if (state.country) params.set('country', state.country);
  if (state.category) params.set('category', state.category);
  if (state.source) params.set('source', state.source);
  if (state.status) params.set('status', state.status);
  if (state.q) params.set('q', state.q);
  // favorites_only needs a wider fetch since the filter happens client-
  // side — a favorite outside the first 300 by (country,name) order
  // would be invisible at the default limit.
  params.set('limit', state.favorites_only ? '20000' : '300');
  let data;
  try {
    data = await api('/api/iptv/channels?' + params.toString());
  } catch (e) {
    grid.innerHTML = `<div class="empty">Error: ${escapeHtml(e.message)}</div>`;
    return;
  }
  let channels = data.channels || [];
  // Favorites-only is purely a client-side filter — the server doesn't
  // know which channels you've starred. Apply after the fetch.
  if (state.favorites_only) {
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
    const logoHtml = ch.logo
      ? `<img src="${escapeAttr(ch.logo)}" alt="" referrerpolicy="no-referrer"
              onerror="this.replaceWith(Object.assign(document.createElement('div'),{className:'glyph',textContent:'📺'}))">`
      : `<div class="glyph">📺</div>`;
    const kind   = streamTypeOf(ch.url);
    const origin = originOf(ch.url);
    const isGeo  = /\[Geo[- ]?blocked\]/i.test(ch.name || '');
    const fav    = isFavorite(ch.id);
    const badges = [];
    if (kind !== 'other') badges.push(`<span class="b ${kind}">${kind.toUpperCase()}</span>`);
    if (origin.kind === 'official') badges.push(`<span class="b official">CDN</span>`);
    else if (origin.kind === 'restream') badges.push(`<span class="b restream">Re-stream</span>`);
    if (isGeo) badges.push(`<span class="b geo">geo</span>`);
    card.innerHTML = `
      <button class="star-btn ${fav ? 'on' : ''}" aria-label="favorite">${fav ? '★' : '☆'}</button>
      <div class="logo-wrap">${logoHtml}</div>
      <div class="name">${escapeHtml(ch.name)}</div>
      <div class="meta">${flag(ch.country||'')} ${escapeHtml(ch.country||'?')} · ${escapeHtml((ch.categories||[]).slice(0,1).join(''))}</div>
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

// Drawer-search — filters the chip lists (country, source, category)
// by substring match. Non-matching chips hidden with display:none.
// Empty search = show everything.
document.getElementById('drawer-filter')?.addEventListener('input', (e) => {
  const q = (e.target.value || '').toLowerCase().trim();
  for (const sel of ['#country-chips', '#source-chips', '#category-chips']) {
    document.querySelectorAll(`${sel} .chip`).forEach(c => {
      if (!q || c.textContent.toLowerCase().includes(q)) {
        c.style.display = '';
      } else {
        c.style.display = 'none';
      }
    });
  }
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

// Restore saved-state visual cues before the first paint.
(function _hydrateUi() {
  const s = document.getElementById('search');
  if (s && state.q) s.value = state.q;
  const ab = document.getElementById('alive-only-btn');
  if (ab) ab.textContent = `✓ Alive only: ${state.status === 'alive' ? 'on' : 'off'}`;
  const fb = document.getElementById('favorites-only-btn');
  if (fb) fb.textContent = `⭐ Favorites: ${state.favorites_only ? 'on' : 'off'}`;
})();
loadFilters();
loadChannels();
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

  <div id="epg-block" style="display:none; background:#15181f; border:1px solid #232831; border-radius:10px; padding:10px 12px; margin-bottom:12px;">
    <div style="font-size:10px; letter-spacing:.08em; color:var(--tg-theme-hint-color,#8a8f99); text-transform:uppercase; margin-bottom:6px;">Programme guide</div>
    <div id="epg-content"></div>
  </div>

  <div class="actions">
    <button id="play-inline">▶ Play</button>
    <button class="ghost" id="play-vlc">📤 Open in VLC / external player</button>
    <button class="ghost" id="copy-url">📋 Copy stream URL</button>
    <button class="ghost" id="probe-btn">🩺 Probe stream health</button>
    <button class="warn" id="record-btn">⏺ Record 5 min</button>
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

async function loadChannel() {
  try {
    CHANNEL = await api(`/api/iptv/channels/${encodeURIComponent(CHANNEL_ID)}`);
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
  maybeShowExitWarning();
  loadEpg();
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
    hls.on(window.Hls.Events.ERROR, (_e, data) => {
      if (data.fatal) toast('HLS error: ' + (data.details || data.type), 4500);
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
    player.on(window.dashjs.MediaPlayer.events.ERROR, e => {
      toast('DASH error: ' + (e.error?.message || JSON.stringify(e)), 4500);
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
