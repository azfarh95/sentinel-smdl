"""Telegram Mini App — owner-only dashboard for SM-DL.

Mounted at /app (HTML) + /api/miniapp/* (JSON).

Features (v1):
  • Recent downloads list (from url_cache)
  • Stream watchlist add/remove (delegates to stream_monitor)
  • Start/stop live recordings (delegates to recorder_bridge.bridge)
  • Supported & configured platforms (from live_downloader registry + config)

Auth: validates Telegram WebApp initData (HMAC-SHA256 with bot token).
Owner-only — initData.user.id must match config.OWNER_CHAT_ID.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import parse_qsl

import aiosqlite
from fastapi import APIRouter, HTTPException, Request, Header
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

from . import config as _cfg
from . import stream_monitor
from .database import DB_PATH
from .live_downloader import (
    _PLATFORM_LABELS,    # we read but don't mutate
)
from .recorder_bridge import bridge

CONFIG_FILE = os.environ.get("CONFIG_FILE", "/config/smdl.json")
DOWNLOADS_DIR = os.environ.get("DOWNLOADS_DIR", "/downloads")

# config.py exposes UPPER_SNAKE module constants, not a .get() function.
# Some keys also rename between the JSON schema and the module constants.
_KEY_TO_ATTR = {
    "max_concurrent_downloads": "MAX_CONCURRENT",
    # everything else: key.upper() works
}


def _cfg_get(key: str, default=None):
    """Read a config value. Order: JSON file (so UI edits are live) → module
    constant (loaded at import) → caller-provided default."""
    file_cfg = _read_config_file_safe()
    if key in file_cfg:
        return file_cfg[key]
    attr = _KEY_TO_ATTR.get(key, key.upper())
    return getattr(_cfg, attr, default)


def _read_config_file_safe() -> dict:
    """Forward-declared shim so _cfg_get can call into the file reader
    defined later in the module."""
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            import json as _j
            return _j.load(f)
    except Exception:
        return {}

logger = logging.getLogger(__name__)

router = APIRouter()

# ── Telegram initData validation ─────────────────────────────────────────────


def _validate_init_data(init_data: str, bot_token: str, max_age_s: int = 3600) -> dict:
    """Parse + verify Telegram WebApp initData.

    Returns the parsed payload dict (with 'user' nested as dict) on success.
    Raises HTTPException(401) on failure.
    """
    if not init_data:
        raise HTTPException(status_code=401, detail="missing initData")
    try:
        pairs = dict(parse_qsl(init_data, strict_parsing=False))
    except Exception:
        raise HTTPException(status_code=401, detail="malformed initData")

    received_hash = pairs.pop("hash", None)
    if not received_hash:
        raise HTTPException(status_code=401, detail="initData hash missing")

    # Reconstruct data-check-string (sorted by key, joined by \n)
    data_check = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret_key, data_check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(received_hash, expected):
        raise HTTPException(status_code=401, detail="initData signature invalid")

    # Freshness — Telegram's auth_date is unix seconds
    try:
        auth_date = int(pairs.get("auth_date", "0"))
    except ValueError:
        auth_date = 0
    if auth_date and (time.time() - auth_date) > max_age_s:
        raise HTTPException(status_code=401, detail="initData expired")

    # Parse user
    user = {}
    if "user" in pairs:
        try:
            user = json.loads(pairs["user"])
        except Exception:
            pass
    pairs["user"] = user
    return pairs


def _owner_only(payload: dict) -> int:
    """Confirm the validated initData belongs to the configured owner.
    Returns the owner chat_id."""
    owner = _cfg_get("owner_chat_id")
    if owner is None:
        raise HTTPException(status_code=503, detail="OWNER_CHAT_ID not configured")
    user_id = (payload.get("user") or {}).get("id")
    if user_id != owner:
        raise HTTPException(status_code=403, detail="not the bot owner")
    return owner


async def _verify(request: Request) -> dict:
    """Common request guard: pull X-Init-Data header, validate, owner-check."""
    # bot.py reads SMDL_BOT_TOKEN — keep this in sync. Fall back to the
    # generic names for cross-deployment portability.
    bot_token = (
        os.environ.get("SMDL_BOT_TOKEN")
        or os.environ.get("BOT_TOKEN")
        or os.environ.get("TELEGRAM_BOT_TOKEN")
        or ""
    )
    if not bot_token:
        raise HTTPException(status_code=503, detail="bot token not configured")
    init_data = request.headers.get("x-init-data") or ""
    payload = _validate_init_data(init_data, bot_token)
    _owner_only(payload)
    return payload


# ── Helpers ──────────────────────────────────────────────────────────────────


async def _list_recent_downloads(limit: int = 50) -> list[dict]:
    """Return the most recent N entries from url_cache, newest first."""
    out = []
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT url, files, platform, uploader, created_at "
            "FROM url_cache ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ) as cur:
            async for row in cur:
                d = dict(row)
                try: d["files"] = json.loads(d.get("files") or "[]")
                except Exception: d["files"] = []
                out.append(d)
    return out


def _list_platforms() -> dict:
    """Return supported (from live_downloader) + configured (from config) platforms."""
    configured_live = list(_cfg_get("live_platforms") or [])
    # Registered labels — flatten host_substrings + label
    registered = [
        {"label": label, "hosts": list(hosts)}
        for hosts, label in _PLATFORM_LABELS
    ]
    return {
        "configured_for_live": configured_live,
        "registered_labels": registered,
        # Anything yt-dlp's 1700+ extractors recognise is technically supported,
        # but only those matching a registered label render a friendly name.
        "note": (
            "yt-dlp covers 1700+ sites. The labels list below names the most common; "
            "other URLs route through 'other' but still work if yt-dlp has an extractor."
        ),
    }


def _job_to_dict(job) -> dict:
    return {
        "job_id": job.job_id,
        "chat_id": job.chat_id,
        "url": job.url,
        "platform": job.platform,
        "uploader": job.uploader,
        "started_at": job.started_at,
        "elapsed_sec": int(time.time() - job.started_at),
        "bytes": job.bytes_downloaded,
        "filepath": job.filepath,
        "stop_requested_at": job.stop_requested_at,
        "abort_reason": job.abort_reason,
    }


# ── Request models ───────────────────────────────────────────────────────────


class WatchAddBody(BaseModel):
    url: str
    label: Optional[str] = None


class StreamStartBody(BaseModel):
    url: str


class StreamStopBody(BaseModel):
    chat_id: Optional[int] = None   # default: owner's chat


# ── Routes ───────────────────────────────────────────────────────────────────


@router.get("/api/miniapp/whoami")
async def whoami(request: Request):
    p = await _verify(request)
    return {"user": p.get("user"), "owner": _cfg_get("owner_chat_id")}


@router.get("/api/miniapp/downloads")
async def downloads(request: Request, limit: int = 50):
    await _verify(request)
    rows = await _list_recent_downloads(limit=max(1, min(limit, 200)))
    return {"items": rows, "count": len(rows)}


@router.get("/api/miniapp/watchlist")
async def watchlist(request: Request):
    await _verify(request)
    return {"items": stream_monitor.list_watchlist()}


@router.post("/api/miniapp/watchlist/add")
async def watchlist_add(request: Request, body: WatchAddBody):
    p = await _verify(request)
    owner = p["user"]["id"]
    ok, msg = stream_monitor.add_to_watchlist(body.url, body.label, added_by=owner)
    if not ok:
        return JSONResponse({"ok": False, "error": msg}, status_code=400)
    return {"ok": True, "msg": msg, "items": stream_monitor.list_watchlist()}


@router.post("/api/miniapp/watchlist/remove")
async def watchlist_remove(request: Request, body: WatchAddBody):
    await _verify(request)
    ok, msg = stream_monitor.remove_from_watchlist(body.url)
    if not ok:
        return JSONResponse({"ok": False, "error": msg}, status_code=400)
    return {"ok": True, "msg": msg, "items": stream_monitor.list_watchlist()}


@router.get("/api/miniapp/active")
async def active_streams(request: Request):
    await _verify(request)
    return {"items": [_job_to_dict(j) for j in bridge.list_active()]}


@router.post("/api/miniapp/stream/stop")
async def stream_stop(request: Request, body: StreamStopBody):
    p = await _verify(request)
    chat_id = body.chat_id or p["user"]["id"]
    status = await bridge.stop(int(chat_id))
    if status is None:
        return JSONResponse({"ok": False, "error": "no active job for this chat"}, status_code=404)
    return {"ok": True, "status": {
        "elapsed_seconds": status.elapsed_seconds,
        "bytes": status.bytes,
        "platform": status.platform,
        "uploader": status.uploader,
    }}


@router.post("/api/miniapp/stream/start")
async def stream_start(request: Request, body: StreamStartBody):
    p = await _verify(request)
    chat_id = p["user"]["id"]
    url = body.url.strip()
    if not url:
        return JSONResponse({"ok": False, "error": "url required"}, status_code=400)
    if bridge.has_job(chat_id):
        return JSONResponse({"ok": False, "error": "a recording is already active for this chat"}, status_code=409)
    # Fire-and-forget: kick off the recording task; return immediately.
    asyncio.create_task(bridge.record(chat_id=chat_id, url=url))
    return {"ok": True, "queued": True, "chat_id": chat_id, "url": url}


@router.get("/api/miniapp/sites")
async def sites(request: Request):
    await _verify(request)
    return _list_platforms()


# ── Settings / config ────────────────────────────────────────────────────────


# Subset of config keys we expose to the Mini App (numeric/string editable).
# Settings marked needs_restart=True are read once at module import — UI shows
# a "restart required" badge so the user knows.
EDITABLE_SETTINGS = [
    {"key": "max_concurrent_downloads", "label": "Max concurrent downloads",
     "type": "int", "min": 1, "max": 10, "needs_restart": True},
    {"key": "live_max_concurrent", "label": "Max concurrent live recordings",
     "type": "int", "min": 1, "max": 5, "needs_restart": True},
    {"key": "default_quality", "label": "Default download resolution",
     "type": "choice", "choices": ["best", "1080p", "720p", "480p", "360p"]},
    {"key": "live_max_height", "label": "Live recording max height (px, 0=source)",
     "type": "int", "min": 0, "max": 2160, "needs_restart": True},
    {"key": "temp_ttl_hours", "label": "Temp file TTL (hours)",
     "type": "int", "min": 1, "max": 168},
    {"key": "delete_after_send", "label": "Delete files after Telegram send",
     "type": "bool"},
    {"key": "monitor_poll_interval_seconds", "label": "Stream monitor poll interval (s)",
     "type": "int", "min": 60, "max": 3600, "needs_restart": True},
]


def _read_config_file() -> dict:
    """Read the smdl.json file. Returns empty dict if missing."""
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except Exception as e:
        logger.warning("config read failed: %s", e)
        return {}


def _write_config_file(updates: dict) -> dict:
    """Merge `updates` into smdl.json and write atomically. Returns the merged config."""
    try:
        from pathlib import Path
        cfg_path = Path(CONFIG_FILE)
        cfg_path.parent.mkdir(parents=True, exist_ok=True)
        current = _read_config_file()
        current.update(updates)
        tmp = cfg_path.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(current, f, indent=2, sort_keys=True)
        tmp.replace(cfg_path)
        return current
    except Exception as e:
        logger.error("config write failed: %s", e)
        raise HTTPException(status_code=500, detail=f"config write failed: {e}")


def _disk_usage_gb(path: str) -> dict:
    try:
        import shutil
        total, used, free = shutil.disk_usage(path)
        return {"total_gb": round(total / 1024**3, 1),
                "used_gb":  round(used  / 1024**3, 1),
                "free_gb":  round(free  / 1024**3, 1)}
    except Exception:
        return {"total_gb": None, "used_gb": None, "free_gb": None}


@router.get("/api/miniapp/config")
async def get_config(request: Request):
    await _verify(request)
    # Current values come from the loaded _cfg module (single source of truth).
    current_values = {}
    for s in EDITABLE_SETTINGS:
        current_values[s["key"]] = _cfg_get(s["key"])
    return {
        "settings": EDITABLE_SETTINGS,
        "values": current_values,
        "paths": {
            "downloads_dir": DOWNLOADS_DIR,
            "downloads_dir_writable": os.access(DOWNLOADS_DIR, os.W_OK) if os.path.exists(DOWNLOADS_DIR) else False,
            "config_file": CONFIG_FILE,
        },
        "disk": _disk_usage_gb(DOWNLOADS_DIR),
    }


class ConfigUpdateBody(BaseModel):
    updates: dict


@router.post("/api/miniapp/config")
async def update_config(request: Request, body: ConfigUpdateBody):
    await _verify(request)
    # Validate each update against EDITABLE_SETTINGS
    schema = {s["key"]: s for s in EDITABLE_SETTINGS}
    validated = {}
    errors = []
    needs_restart = []
    for k, v in (body.updates or {}).items():
        if k not in schema:
            errors.append(f"{k}: not editable")
            continue
        s = schema[k]
        try:
            if s["type"] == "int":
                vv = int(v)
                if vv < s.get("min", -10**9) or vv > s.get("max", 10**9):
                    raise ValueError(f"out of range [{s.get('min')}, {s.get('max')}]")
                validated[k] = vv
            elif s["type"] == "bool":
                validated[k] = bool(v)
            elif s["type"] == "choice":
                if v not in s["choices"]:
                    raise ValueError(f"not in {s['choices']}")
                validated[k] = v
            else:
                validated[k] = v
        except Exception as e:
            errors.append(f"{k}: {e}")
            continue
        if s.get("needs_restart"):
            needs_restart.append(k)
    if errors:
        return JSONResponse({"ok": False, "errors": errors}, status_code=400)
    merged = _write_config_file(validated)
    return {"ok": True, "saved": validated, "needs_restart": needs_restart,
            "merged_config": merged}


# OneDrive — placeholder for Phase 2. Returns status only.
@router.get("/api/miniapp/onedrive/status")
async def onedrive_status(request: Request):
    await _verify(request)
    return {
        "configured": False,
        "phase": "stub",
        "note": ("OneDrive integration is Phase 2. Will use Microsoft Graph OAuth "
                 "+ /me/drive/root:/SMDL/{platform}/{uploader}/{file}:/content. "
                 "Token persisted to /data/onedrive_token.json. "
                 "Configurable: auto-upload after download / on demand / disabled."),
        "todo": [
            "Register Azure AD app + redirect URI",
            "Add msal Python dependency to requirements.txt",
            "POST /api/miniapp/onedrive/connect → device-code flow",
            "POST /api/miniapp/onedrive/upload/{file_id}",
            "Background uploader that watches DOWNLOADS_DIR for new files",
        ],
    }


# ── HTML (inline single-page app) ────────────────────────────────────────────


HTML = """<!doctype html>
<html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no">
<title>SM-DL</title>
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<style>
:root {
  --bg: var(--tg-theme-bg-color, #1c1c1e);
  --fg: var(--tg-theme-text-color, #e8e8ea);
  --muted: var(--tg-theme-hint-color, #8e8e93);
  --link: var(--tg-theme-link-color, #2997ff);
  --button: var(--tg-theme-button-color, #2997ff);
  --button-text: var(--tg-theme-button-text-color, #fff);
  --section: var(--tg-theme-section-bg-color, #2c2c2e);
  --separator: var(--tg-theme-section-separator-color, #38383a);
  --destructive: #ff453a;
  --success: #34c759;
}
* { box-sizing: border-box; -webkit-tap-highlight-color: transparent; }
body { margin: 0; padding: 0; font: 15px/1.4 -apple-system, system-ui, "Segoe UI", Roboto, sans-serif;
       background: var(--bg); color: var(--fg); padding-bottom: 70px; min-height: 100vh; }
.tabbar { position: fixed; left: 0; right: 0; bottom: 0; background: var(--section);
          border-top: 1px solid var(--separator); display: flex; height: 58px; z-index: 10; }
.tab { flex: 1; display: flex; flex-direction: column; align-items: center; justify-content: center;
       color: var(--muted); cursor: pointer; font-size: 11px; gap: 2px; user-select: none; }
.tab.active { color: var(--button); }
.tab .icon { font-size: 20px; line-height: 1; }
.page { display: none; padding: 12px; }
.page.active { display: block; }
h1 { font-size: 1.3em; margin: 6px 0 14px; }
.card { background: var(--section); border-radius: 10px; padding: 12px; margin-bottom: 10px; }
.row { display: flex; align-items: center; gap: 10px; }
.row .grow { flex: 1; min-width: 0; }
.row .name { font-weight: 600; word-break: break-word; }
.row .meta { font-size: 12px; color: var(--muted); margin-top: 2px; word-break: break-all; }
button { background: var(--button); color: var(--button-text); border: 0; padding: 9px 14px;
         border-radius: 8px; font-size: 14px; font-weight: 500; cursor: pointer; touch-action: manipulation; }
button:active { transform: scale(0.97); }
button.sec { background: transparent; color: var(--button); border: 1px solid var(--button); }
button.danger { background: var(--destructive); }
button.small { padding: 6px 10px; font-size: 12px; }
input { width: 100%; padding: 10px 12px; border: 1px solid var(--separator); border-radius: 8px;
        background: var(--bg); color: var(--fg); font-size: 14px; }
input:focus { outline: none; border-color: var(--button); }
.field { font-size: 12px; color: var(--muted); margin: 4px 4px 4px; }
.dot { width: 8px; height: 8px; border-radius: 50%; display: inline-block; margin-right: 6px; }
.dot.live { background: var(--destructive); box-shadow: 0 0 6px var(--destructive); animation: pulse 1s infinite; }
.dot.idle { background: var(--muted); }
@keyframes pulse { 50% { opacity: 0.5; } }
.empty { text-align: center; color: var(--muted); padding: 40px 20px; font-size: 14px; }
.msg { padding: 10px 14px; border-radius: 8px; margin: 10px 0; font-size: 13px; }
.msg.ok { background: rgba(52,199,89,0.15); color: var(--success); }
.msg.err { background: rgba(255,69,58,0.15); color: var(--destructive); }
.platform-pill { display: inline-block; background: var(--bg); border: 1px solid var(--separator);
                 padding: 4px 8px; border-radius: 6px; margin: 2px; font-size: 12px; }
.platform-pill.live { border-color: var(--success); color: var(--success); }
.file-list { font-size: 12px; color: var(--muted); margin-top: 4px; }
.file-list a { color: var(--link); display: block; word-break: break-all; }
.timeago { font-size: 11px; color: var(--muted); }
.spin { display: inline-block; width: 14px; height: 14px; border: 2px solid var(--separator);
        border-top-color: var(--button); border-radius: 50%; animation: sp 0.8s linear infinite;
        vertical-align: middle; }
@keyframes sp { to { transform: rotate(360deg); } }
.url { font-family: ui-monospace, "SF Mono", Menlo, monospace; font-size: 11px; }
</style>
</head><body>

<div id=app>
  <div id=msg></div>

  <div class=page id=page-downloads>
    <h1>Recent Downloads</h1>
    <div id=downloads-list><div class=empty><span class=spin></span> Loading…</div></div>
  </div>

  <div class=page id=page-watchlist>
    <h1>Stream Watchlist</h1>
    <div class=card>
      <div class=field>Add a streamer/channel URL</div>
      <input id=watch-url placeholder="https://twitch.tv/...">
      <div class=field>Label (optional)</div>
      <input id=watch-label placeholder="Friendly name">
      <div style="margin-top:8px"><button onclick=addWatch()>+ Add to watchlist</button></div>
    </div>
    <div id=watchlist-list><div class=empty><span class=spin></span> Loading…</div></div>
  </div>

  <div class=page id=page-live>
    <h1>Live Streams</h1>
    <div class=card>
      <div class=field>Start a new recording</div>
      <input id=stream-url placeholder="https://twitch.tv/... (live URL)">
      <div style="margin-top:8px"><button onclick=startStream()>▶ Start recording</button></div>
    </div>
    <div id=live-list><div class=empty><span class=spin></span> Loading…</div></div>
  </div>

  <div class=page id=page-sites>
    <h1>Sites</h1>
    <div id=sites-content><div class=empty><span class=spin></span> Loading…</div></div>
  </div>

  <div class=page id=page-settings>
    <h1>Settings</h1>
    <div id=settings-content><div class=empty><span class=spin></span> Loading…</div></div>
  </div>
</div>

<div class=tabbar>
  <div class="tab active" onclick="goto('downloads')"><div class=icon>📥</div><div>Downloads</div></div>
  <div class=tab onclick="goto('watchlist')"><div class=icon>👁</div><div>Watchlist</div></div>
  <div class=tab onclick="goto('live')"><div class=icon>🔴</div><div>Live</div></div>
  <div class=tab onclick="goto('sites')"><div class=icon>🌐</div><div>Sites</div></div>
  <div class=tab onclick="goto('settings')"><div class=icon>⚙️</div><div>Settings</div></div>
</div>

<script>
const tg = window.Telegram?.WebApp;
if (tg) { tg.ready(); tg.expand(); }
const initData = tg?.initData || '';
let current = 'downloads';
let liveTimer = null;

function api(path, opts = {}) {
  return fetch(path, {
    ...opts,
    headers: {
      'X-Init-Data': initData,
      'Content-Type': 'application/json',
      ...(opts.headers || {}),
    },
  }).then(r => r.ok ? r.json() : r.json().then(j => Promise.reject(j.detail || j.error || ('HTTP '+r.status))));
}

function showOk(t) { const m = document.getElementById('msg'); m.className = 'msg ok'; m.textContent = t; setTimeout(()=>m.className='', 3500); }
function showErr(t) { const m = document.getElementById('msg'); m.className = 'msg err'; m.textContent = String(t); setTimeout(()=>m.className='', 5500); }

function esc(s) { return String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
function timeago(iso) {
  if (!iso) return '';
  const t = new Date(iso).getTime();
  const s = Math.floor((Date.now()-t)/1000);
  if (s<60) return s+'s ago';
  if (s<3600) return Math.floor(s/60)+'m ago';
  if (s<86400) return Math.floor(s/3600)+'h ago';
  return Math.floor(s/86400)+'d ago';
}
function bytes(n) { if (!n) return '0 B'; const u = ['B','KB','MB','GB']; let i = 0; while (n>=1024 && i<u.length-1) { n/=1024; i++; } return n.toFixed(1)+' '+u[i]; }
function duration(s) { if (s<60) return s+'s'; const m = Math.floor(s/60); const sec = s%60; if (m<60) return m+'m '+sec+'s'; return Math.floor(m/60)+'h '+(m%60)+'m'; }

function goto(page) {
  current = page;
  document.querySelectorAll('.page').forEach(p => p.classList.toggle('active', p.id === 'page-'+page));
  document.querySelectorAll('.tab').forEach((t,i) => t.classList.toggle('active', ['downloads','watchlist','live','sites','settings'][i] === page));
  if (page === 'downloads') loadDownloads();
  else if (page === 'watchlist') loadWatchlist();
  else if (page === 'live') loadLive();
  else if (page === 'sites') loadSites();
  else if (page === 'settings') loadSettings();

  // start/stop the live refresh timer
  if (liveTimer) { clearInterval(liveTimer); liveTimer = null; }
  if (page === 'live') liveTimer = setInterval(loadLive, 5000);
}

async function loadDownloads() {
  try {
    const j = await api('/api/miniapp/downloads?limit=50');
    const root = document.getElementById('downloads-list');
    if (!j.items.length) { root.innerHTML = '<div class=empty>No downloads yet.</div>'; return; }
    root.innerHTML = j.items.map(d => `
      <div class=card>
        <div class=row>
          <div class=grow>
            <div class=name>${esc(d.platform || 'other')} · @${esc(d.uploader || '?')}</div>
            <div class="meta url">${esc(d.url)}</div>
            <div class="file-list">${(d.files||[]).map(f => `<a href="/m/${encodeURI(f.split('/').pop())}" target=_blank>${esc(f.split('/').pop())}</a>`).join('')}</div>
            <div class=timeago>${timeago(d.created_at)}</div>
          </div>
        </div>
      </div>
    `).join('');
  } catch(e) { showErr('Load failed: '+e); }
}

async function loadWatchlist() {
  try {
    const j = await api('/api/miniapp/watchlist');
    const root = document.getElementById('watchlist-list');
    if (!j.items.length) { root.innerHTML = '<div class=empty>Watchlist is empty.</div>'; return; }
    root.innerHTML = j.items.map(w => `
      <div class=card>
        <div class=row>
          <div class=grow>
            <div class=name>${esc(w.label || w.url)}</div>
            <div class="meta url">${esc(w.url)}</div>
          </div>
          <button class="small danger" onclick="removeWatch('${esc(w.url)}')">Remove</button>
        </div>
      </div>
    `).join('');
  } catch(e) { showErr('Load failed: '+e); }
}

async function addWatch() {
  const url = document.getElementById('watch-url').value.trim();
  const label = document.getElementById('watch-label').value.trim();
  if (!url) { showErr('URL required'); return; }
  try {
    await api('/api/miniapp/watchlist/add', { method: 'POST', body: JSON.stringify({url, label: label || null}) });
    showOk('Added');
    document.getElementById('watch-url').value = '';
    document.getElementById('watch-label').value = '';
    loadWatchlist();
  } catch(e) { showErr(e); }
}

async function removeWatch(url) {
  if (tg?.showConfirm) {
    tg.showConfirm('Remove this from watchlist?', async (ok) => { if (ok) await _doRemoveWatch(url); });
  } else if (confirm('Remove ' + url + '?')) {
    await _doRemoveWatch(url);
  }
}

async function _doRemoveWatch(url) {
  try {
    await api('/api/miniapp/watchlist/remove', { method: 'POST', body: JSON.stringify({url}) });
    showOk('Removed');
    loadWatchlist();
  } catch(e) { showErr(e); }
}

async function loadLive() {
  try {
    const j = await api('/api/miniapp/active');
    const root = document.getElementById('live-list');
    if (!j.items.length) { root.innerHTML = '<div class=empty>No active recordings.</div>'; return; }
    root.innerHTML = j.items.map(s => `
      <div class=card>
        <div class=row>
          <div class=grow>
            <div class=name><span class="dot live"></span>${esc(s.platform || 'recording')} · @${esc(s.uploader || '?')}</div>
            <div class=meta>${duration(s.elapsed_sec)} · ${bytes(s.bytes)} ${s.stop_requested_at ? '· stopping…' : ''}</div>
            <div class="meta url">${esc(s.url)}</div>
          </div>
          <button class="small danger" onclick="stopStream(${s.chat_id})">⏹ Stop</button>
        </div>
      </div>
    `).join('');
  } catch(e) { showErr('Load failed: '+e); }
}

async function startStream() {
  const url = document.getElementById('stream-url').value.trim();
  if (!url) { showErr('URL required'); return; }
  try {
    const j = await api('/api/miniapp/stream/start', { method: 'POST', body: JSON.stringify({url}) });
    showOk('Recording queued · @' + (j.url||''));
    document.getElementById('stream-url').value = '';
    setTimeout(loadLive, 1500);
  } catch(e) { showErr(e); }
}

async function stopStream(chat_id) {
  try {
    const j = await api('/api/miniapp/stream/stop', { method: 'POST', body: JSON.stringify({chat_id}) });
    showOk('Stop sent · ' + duration(j.status.elapsed_seconds));
    setTimeout(loadLive, 1000);
  } catch(e) { showErr(e); }
}

async function loadSites() {
  try {
    const j = await api('/api/miniapp/sites');
    const root = document.getElementById('sites-content');
    const live = (j.configured_for_live || []).map(p => `<span class="platform-pill live">🔴 ${esc(p)}</span>`).join('');
    const labels = (j.registered_labels || []).map(p => `<span class=platform-pill>${esc(p.label)}</span>`).join('');
    root.innerHTML = `
      <div class=card>
        <div class=field>Live recording allowed for:</div>
        <div>${live || '<i>(none configured)</i>'}</div>
      </div>
      <div class=card>
        <div class=field>Registered platform labels (downloads):</div>
        <div>${labels || '<i>(none)</i>'}</div>
      </div>
      <div class=card>
        <div class=meta>${esc(j.note || '')}</div>
      </div>
    `;
  } catch(e) { showErr('Load failed: '+e); }
}

async function loadSettings() {
  const root = document.getElementById('settings-content');
  root.innerHTML = '<div class=empty><span class=spin></span> Loading…</div>';
  try {
    const cfg = await api('/api/miniapp/config');
    const od  = await api('/api/miniapp/onedrive/status');
    const fields = cfg.settings.map(s => {
      const v = cfg.values[s.key];
      const id = 'set-' + s.key;
      let input;
      if (s.type === 'choice') {
        input = `<select id="${id}">${s.choices.map(c => `<option ${c===v?'selected':''} value="${esc(c)}">${esc(c)}</option>`).join('')}</select>`;
      } else if (s.type === 'bool') {
        input = `<select id="${id}"><option value=true ${v?'selected':''}>Yes</option><option value=false ${!v?'selected':''}>No</option></select>`;
      } else {
        input = `<input id="${id}" type=number ${s.min!=null?'min='+s.min:''} ${s.max!=null?'max='+s.max:''} value="${v ?? ''}">`;
      }
      const restart = s.needs_restart ? ' <span style="color:#ff9500;font-size:11px">· restart required</span>' : '';
      return `<div class=card>
        <div class=field>${esc(s.label)}${restart}</div>
        ${input}
      </div>`;
    }).join('');
    const disk = cfg.disk;
    const diskHtml = disk.free_gb != null
      ? `<div class=meta>${disk.free_gb} GB free of ${disk.total_gb} GB · ${disk.used_gb} GB used</div>`
      : `<div class=meta>(disk usage unavailable)</div>`;
    root.innerHTML = `
      ${fields}
      <div style="margin:14px 0"><button onclick=saveSettings()>💾 Save changes</button></div>

      <div class=card>
        <div class=field>Downloads folder (env var, container)</div>
        <div class=meta><span class=url>${esc(cfg.paths.downloads_dir)}</span>
          ${cfg.paths.downloads_dir_writable ? '<span style="color:var(--success)">· writable</span>' : '<span style="color:var(--destructive)">· not writable</span>'}</div>
        ${diskHtml}
        <div class=meta style="margin-top:6px">To change: edit <code>DOWNLOADS_DIR</code> in docker-compose and restart the container.</div>
      </div>

      <div class=card>
        <div class=field>Config file</div>
        <div class=meta><span class=url>${esc(cfg.paths.config_file)}</span></div>
      </div>

      <div class=card>
        <div class=field>OneDrive integration</div>
        <div class=name>${od.configured ? '✅ Connected' : '⚪ Not configured · Phase 2'}</div>
        <div class=meta style="margin-top:4px">${esc(od.note)}</div>
        ${od.configured ? '' : `<div style="margin-top:8px"><button class=sec disabled title="Phase 2">Connect OneDrive (planned)</button></div>`}
      </div>
    `;
  } catch(e) { showErr('Load failed: '+e); }
}

async function saveSettings() {
  const cfg = await api('/api/miniapp/config');
  const updates = {};
  for (const s of cfg.settings) {
    const el = document.getElementById('set-' + s.key);
    if (!el) continue;
    let v = el.value;
    if (s.type === 'int') v = parseInt(v, 10);
    else if (s.type === 'bool') v = (v === 'true' || v === true);
    updates[s.key] = v;
  }
  try {
    const j = await api('/api/miniapp/config', { method: 'POST', body: JSON.stringify({updates}) });
    if (j.needs_restart && j.needs_restart.length) {
      showOk('Saved · restart required for: ' + j.needs_restart.join(', '));
    } else {
      showOk('Saved');
    }
    loadSettings();
  } catch(e) {
    if (Array.isArray(e)) showErr(e.join(' · '));
    else showErr(e.errors ? e.errors.join(' · ') : e);
  }
}

goto('downloads');
</script>
</body></html>"""


@router.get("/app", response_class=HTMLResponse)
async def miniapp_index():
    return HTMLResponse(HTML)


@router.get("/app/", response_class=HTMLResponse)
async def miniapp_index_slash():
    return HTMLResponse(HTML)
