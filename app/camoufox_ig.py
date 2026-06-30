"""Instagram capture via the Camoufox stealth browser (ADR MED-012).

Why this exists
---------------
Instagram flags the account when ``yt-dlp`` presents the session cookie with a
**non-browser fingerprint** — valid session, obviously-not-a-browser client.
The fix is not to drop cookies (that only breaks logged-in fetches); it is to
move IG auth *into a real browser*. This module routes IG — and ONLY IG —
through the ``camofox`` service (Camoufox = Firefox with C++-level fingerprint
spoofing). The IG session lives inside a **persistent Camoufox profile** logged
in once via noVNC (ADR MED-012 step C), so SMDL itself holds no IG
``cookies.txt`` at all.

Contract
--------
- ``handles(url)`` is True only when ``SMDL_IG_CAMOUFOX=1`` *and* the URL is IG.
- ``download_via_camoufox`` / ``identify_via_camoufox`` mirror the shapes of
  ``downloader.download`` / ``downloader.identify_post`` so the fork in
  ``downloader.py`` is a thin branch.
- **No silent yt-dlp fallback for IG.** When the flag is on, an IG failure
  surfaces as an error rather than re-running the fingerprint that got flagged.
  To revert to yt-dlp, flip the flag off (``SMDL_IG_CAMOUFOX=0``).

The DOM-extraction selectors in ``_MEDIA_EXTRACT_JS`` are the one part expected
to need tuning once a real logged-in IG profile exists (step C) — they are
isolated here on purpose, with layered fallbacks (video src → og meta →
embedded JSON).
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)

CAMOFOX_URL        = os.environ.get("CAMOFOX_URL", "http://camofox:9377").rstrip("/")
CAMOFOX_ACCESS_KEY = os.environ.get("CAMOFOX_ACCESS_KEY", "")
IG_SESSION_USER    = os.environ.get("CAMOFOX_IG_SESSION", "ig-owner")
ENABLED            = os.environ.get("SMDL_IG_CAMOUFOX", "0").lower() in ("1", "true", "yes", "on")

_IG_HOSTS = ("instagram.com",)
# A real Firefox UA for the direct CDN byte-fetch (IG CDN URLs are signed and
# fetch fine without cookies for a short window; the UA just avoids a naked
# python-httpx signature on the media GET).
_BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:135.0) "
               "Gecko/20100101 Firefox/135.0")

# Pulled from the loaded post page inside the logged-in session. Layered so a
# single IG layout change doesn't take out every shape. Returns a JSON-able
# {"kind": "video|image|carousel", "urls": [...]} or {"urls": []}.
_MEDIA_EXTRACT_JS = r"""
(() => {
  const out = [];
  // 1) Direct <video> elements (reels / single video posts).
  for (const v of document.querySelectorAll('video')) {
    if (v.src && v.src.startsWith('http')) out.push(v.src);
    for (const s of v.querySelectorAll('source')) {
      if (s.src && s.src.startsWith('http')) out.push(s.src);
    }
  }
  // 2) og:video / og:image meta (single-media posts).
  const og = (p) => Array.from(document.querySelectorAll(`meta[property="${p}"]`))
                         .map(m => m.content).filter(Boolean);
  const ogVideo = og('og:video').concat(og('og:video:secure_url'));
  const ogImage = og('og:image');
  // 3) Embedded JSON (carousels / when the DOM hasn't painted media yet).
  const json = [];
  for (const sc of document.querySelectorAll('script[type="application/json"]')) {
    const t = sc.textContent || '';
    for (const m of t.matchAll(/"(?:video_url|src_url|display_url)":"(https:[^"]+)"/g)) {
      json.push(m[1].replace(/\\u0026/g, '&').replace(/\\\//g, '/'));
    }
  }
  let urls = [], kind = 'image';
  if (out.length)        { urls = out;            kind = 'video'; }
  else if (ogVideo.length){ urls = ogVideo;       kind = 'video'; }
  else if (json.length)  { urls = json;           kind = json.length > 1 ? 'carousel' : 'video'; }
  else if (ogImage.length){ urls = ogImage;       kind = 'image'; }
  // de-dup, preserve order
  urls = Array.from(new Set(urls));
  return { kind, urls };
})()
"""


def handles(url: str) -> bool:
    """True only when the flag is on AND this is an Instagram URL."""
    u = (url or "").lower()
    return ENABLED and any(h in u for h in _IG_HOSTS)


def _headers() -> dict:
    h = {"Content-Type": "application/json"}
    if CAMOFOX_ACCESS_KEY:
        h["Authorization"] = f"Bearer {CAMOFOX_ACCESS_KEY}"
    return h


class CamofoxError(RuntimeError):
    pass


async def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=httpx.Timeout(45.0, connect=10.0))


async def health() -> bool:
    """Is the camofox service reachable? (/health is exempt from the bearer.)"""
    try:
        async with httpx.AsyncClient(timeout=5.0) as c:
            r = await c.get(f"{CAMOFOX_URL}/health")
            return r.status_code == 200
    except Exception:
        return False


async def _open_tab(c: httpx.AsyncClient, url: str | None = None) -> str:
    body = {"userId": IG_SESSION_USER, "sessionKey": "ig"}
    if url:
        body["url"] = url
    r = await c.post(f"{CAMOFOX_URL}/tabs", json=body, headers=_headers())
    if r.status_code >= 400:
        raise CamofoxError(f"open_tab {r.status_code}: {r.text[:200]}")
    return r.json()["tabId"]


async def _navigate(c: httpx.AsyncClient, tab: str, url: str) -> None:
    r = await c.post(f"{CAMOFOX_URL}/tabs/{tab}/navigate",
                     json={"userId": IG_SESSION_USER, "url": url}, headers=_headers())
    if r.status_code >= 400:
        raise CamofoxError(f"navigate {r.status_code}: {r.text[:200]}")


async def _evaluate(c: httpx.AsyncClient, tab: str, expression: str):
    r = await c.post(f"{CAMOFOX_URL}/tabs/{tab}/evaluate",
                     json={"userId": IG_SESSION_USER, "expression": expression},
                     headers=_headers())
    if r.status_code >= 400:
        raise CamofoxError(f"evaluate {r.status_code}: {r.text[:200]}")
    data = r.json()
    if not data.get("ok", True):
        raise CamofoxError(f"evaluate not ok: {str(data)[:200]}")
    return data.get("result")


async def _close_tab(c: httpx.AsyncClient, tab: str) -> None:
    try:
        await c.request("DELETE", f"{CAMOFOX_URL}/tabs/{tab}",
                        params={"userId": IG_SESSION_USER}, headers=_headers())
    except Exception:
        pass  # best-effort; idle tabs are reaped by TAB_INACTIVITY_MS anyway


async def _extract_media_urls(url: str) -> dict:
    """Drive the logged-in session to the post and pull media URLs out of it."""
    async with httpx.AsyncClient(timeout=httpx.Timeout(45.0, connect=10.0)) as c:
        tab = await _open_tab(c, url)
        try:
            # Give IG's client-side render a beat to paint <video>/JSON.
            await asyncio.sleep(2.5)
            res = await _evaluate(c, tab, _MEDIA_EXTRACT_JS)
        finally:
            await _close_tab(c, tab)
    if not isinstance(res, dict):
        raise CamofoxError(f"unexpected extract result: {str(res)[:200]}")
    urls = [u for u in (res.get("urls") or []) if isinstance(u, str) and u.startswith("http")]
    if not urls:
        raise CamofoxError("no media URLs found on IG post (selectors may need tuning — ADR MED-012 step C)")
    return {"kind": res.get("kind") or "image", "urls": urls}


def _ext_for(url: str, kind: str) -> str:
    m = re.search(r"\.(mp4|jpg|jpeg|png|webp|heic)(?:\?|$)", url.lower())
    if m:
        return m.group(1).replace("jpeg", "jpg")
    return "mp4" if kind == "video" else "jpg"


async def _fetch_to(url: str, dest: Path) -> None:
    async with httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=10.0),
                                 follow_redirects=True,
                                 headers={"User-Agent": _BROWSER_UA,
                                          "Referer": "https://www.instagram.com/"}) as c:
        async with c.stream("GET", url) as r:
            if r.status_code >= 400:
                raise CamofoxError(f"media fetch {r.status_code} for {url[:80]}")
            dest.parent.mkdir(parents=True, exist_ok=True)
            with open(dest, "wb") as fh:
                async for chunk in r.aiter_bytes(1 << 16):
                    fh.write(chunk)


async def download_via_camoufox(url: str, out_dir: str) -> dict:
    """Capture an IG post through the stealth browser. Returns the same shape as
    ``downloader.download``: {"files": [...], "cached": False} or {"error": str}."""
    try:
        media = await _extract_media_urls(url)
    except Exception as e:
        logger.warning("camoufox IG extract failed for %s: %s", url[:80], e)
        return {"error": f"camoufox IG capture failed: {str(e)[:300]}"}

    kind = media["kind"]
    stamp = int(time.time())
    files: list[str] = []
    base = Path(out_dir)
    try:
        for i, murl in enumerate(media["urls"]):
            ext = _ext_for(murl, kind)
            dest = base / f"ig_{stamp}_{i}.{ext}"
            await _fetch_to(murl, dest)
            files.append(str(dest))
    except Exception as e:
        logger.warning("camoufox IG download failed for %s: %s", url[:80], e)
        # Keep whatever did land; report partial as error so caller sees it.
        if not files:
            return {"error": f"camoufox IG download failed: {str(e)[:300]}"}
    logger.info("camoufox IG capture ok: %s → %d file(s)", url[:80], len(files))
    return {"files": files, "cached": False}


async def identify_via_camoufox(url: str) -> dict:
    """Lightweight 'what is this' probe mirroring ``downloader.identify_post``.
    Returns {"media_type": "video|photo|carousel", "count": n} or {"error": str}."""
    try:
        media = await _extract_media_urls(url)
    except Exception as e:
        return {"error": str(e)[:300]}
    n = len(media["urls"])
    kind = media["kind"]
    mtype = "carousel" if (kind == "carousel" or n > 1) else ("video" if kind == "video" else "photo")
    return {
        "platform": "Instagram",
        "uploader": None, "uploader_id": None,
        "media_type": mtype, "count": n,
        "is_private": False, "is_live": False, "title": None,
    }
