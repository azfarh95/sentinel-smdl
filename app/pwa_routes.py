"""PWA + TWA plumbing for the Sentinel Media TV store build.

Serves the three things a Trusted Web Activity / installable PWA needs:

  GET /manifest.webmanifest      installability metadata (name, icons, scope)
  GET /sw.js                     service worker — network-first nav + offline shell
  GET /.well-known/assetlinks.json   Digital Asset Links binding the TWA package
                                     + signing cert to this origin (kills the URL bar)
  GET /icons/sentinel-tv.svg     scalable maskable app icon for the PWA install path

assetlinks needs the Play app-signing cert SHA-256, which only exists once the
app is uploaded — so it's templated from the environment and returns an empty
(valid) list until the operator supplies it:

  TWA_PACKAGE_NAME=com.azsentinel.smdltv
  TWA_SHA256_CERT_FINGERPRINTS=AA:BB:...,CC:DD:...   (upload key AND Play key)
"""
from __future__ import annotations

import os
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import FileResponse, JSONResponse, Response

router = APIRouter()

_ASSETS = Path(__file__).resolve().parent / "assets"
_ICON_PNG_512 = _ASSETS / "sentinel-tv-512.png"

_THEME = "#0b0e14"
_NAME = "Sentinel Media TV"
_SHORT = "Sentinel TV"
_START_URL = "/iptv"

_ICON_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512">
<rect width="512" height="512" rx="96" fill="#0b0e14"/>
<rect x="96" y="136" width="320" height="208" rx="20" fill="none" stroke="#5ad27a" stroke-width="20"/>
<path d="M224 212 L312 256 L224 300 Z" fill="#5ad27a"/>
<rect x="196" y="372" width="120" height="20" rx="10" fill="#5ad27a"/>
</svg>"""


@router.get("/icons/sentinel-tv.svg")
async def pwa_icon():
    return Response(
        _ICON_SVG,
        media_type="image/svg+xml",
        headers={"Cache-Control": "public, max-age=86400"},
    )


@router.get("/icons/sentinel-tv-512.png")
async def pwa_icon_png():
    """512x512 maskable PNG — the raster icon Bubblewrap consumes to generate
    the Android launcher icons (it doesn't rasterise the SVG)."""
    return FileResponse(
        _ICON_PNG_512,
        media_type="image/png",
        headers={"Cache-Control": "public, max-age=86400"},
    )


@router.get("/manifest.webmanifest")
async def web_manifest():
    manifest = {
        "name": _NAME,
        "short_name": _SHORT,
        "description": "Live TV — browse and watch public broadcasts.",
        "start_url": _START_URL,
        "scope": "/",
        "display": "standalone",
        "orientation": "any",
        "background_color": _THEME,
        "theme_color": _THEME,
        "categories": ["entertainment", "video"],
        "icons": [
            {
                "src": "/icons/sentinel-tv-512.png",
                "sizes": "512x512",
                "type": "image/png",
                "purpose": "any maskable",
            },
            {
                "src": "/icons/sentinel-tv.svg",
                "sizes": "any",
                "type": "image/svg+xml",
                "purpose": "any maskable",
            },
        ],
    }
    return JSONResponse(
        manifest,
        media_type="application/manifest+json",
        headers={"Cache-Control": "public, max-age=3600"},
    )


# Network-first for navigations (always try fresh TV data, fall back to the
# cached offline shell when offline); cache-first for static/icon assets. The
# offline shell is embedded so it works on the very first offline load.
_SW_JS = """
const CACHE = 'sentinel-tv-v1';
const OFFLINE_URL = '/offline.html';
self.addEventListener('install', (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.add(OFFLINE_URL)).then(() => self.skipWaiting()));
});
self.addEventListener('activate', (e) => {
  e.waitUntil(
    caches.keys().then((keys) => Promise.all(
      keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))
    )).then(() => self.clients.claim())
  );
});
self.addEventListener('fetch', (e) => {
  const req = e.request;
  if (req.method !== 'GET') return;
  if (req.mode === 'navigate') {
    e.respondWith(fetch(req).catch(() => caches.match(OFFLINE_URL)));
    return;
  }
  const url = new URL(req.url);
  if (url.pathname.startsWith('/icons/') || url.pathname === '/manifest.webmanifest') {
    e.respondWith(
      caches.match(req).then((hit) => hit || fetch(req).then((res) => {
        const copy = res.clone();
        caches.open(CACHE).then((c) => c.put(req, copy));
        return res;
      }))
    );
  }
});
""".lstrip()


@router.get("/sw.js")
async def service_worker():
    return Response(
        _SW_JS,
        media_type="application/javascript",
        headers={
            "Cache-Control": "no-cache",
            "Service-Worker-Allowed": "/",
        },
    )


_OFFLINE_HTML = """<!doctype html>
<html lang=en><head>
<meta charset=utf-8>
<meta name=viewport content="width=device-width, initial-scale=1">
<meta name=theme-color content="#0b0e14">
<title>Offline — Sentinel Media TV</title>
<style>
  html,body{height:100%;margin:0}
  body{display:grid;place-items:center;background:#0b0e14;color:#e6e6e6;
       font:16px/1.5 Inter,system-ui,sans-serif;text-align:center;padding:24px}
  h1{font-size:20px;margin:0 0 8px}.muted{color:#9aa4b2}
</style></head>
<body><div>
  <h1>You're offline</h1>
  <p class=muted>Live TV needs a connection. Reconnect and try again.</p>
</div></body></html>"""


@router.get("/offline.html")
async def offline_page():
    return Response(
        _OFFLINE_HTML,
        media_type="text/html",
        headers={"Cache-Control": "public, max-age=3600"},
    )


def _cert_fingerprints() -> list[str]:
    raw = (os.environ.get("TWA_SHA256_CERT_FINGERPRINTS") or "").strip()
    return [f.strip() for f in raw.split(",") if f.strip()]


@router.get("/.well-known/assetlinks.json")
async def asset_links():
    """Digital Asset Links for the TWA. Returns an empty (valid) list until the
    operator supplies the package name + signing-cert fingerprints — at which
    point the TWA verifies and runs chromeless."""
    pkg = (os.environ.get("TWA_PACKAGE_NAME") or "").strip()
    fingerprints = _cert_fingerprints()
    statements = []
    if pkg and fingerprints:
        statements.append({
            "relation": ["delegate_permission/common.handle_all_urls"],
            "target": {
                "namespace": "android_app",
                "package_name": pkg,
                "sha256_cert_fingerprints": fingerprints,
            },
        })
    return JSONResponse(statements, headers={"Cache-Control": "public, max-age=300"})
