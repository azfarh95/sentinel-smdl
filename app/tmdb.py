"""TMDB "where to watch" client — the legal streaming-availability layer.

Why
---
On the community deployment, torrents / Real-Debrid are deliberately out of
scope, so Theater can't offer a torrent stream picker there. What it CAN offer,
legally, is "this title is available on X in your region → tap to go there".
TMDB exposes JustWatch's watch-provider data per region, including the
JustWatch deep-link for the title. We surface that and **link out** — we never
embed or restream a third-party paid service (that needs Widevine/PlayReady and
is forbidden; see ADR MED-002). This is the one honest, DRM-respecting answer to
"where can I watch this".

Identity bridge
---------------
SMDL addresses titles by IMDB id (``tt…``) via the Stremio/Cinemeta layer, but
TMDB's ``watch/providers`` resource is keyed by TMDB id, so we hop
``imdb → tmdb`` through ``/find/{imdb_id}?external_source=imdb_id`` first.

Auth
----
Uses the TMDB **v4 read access token** (a Bearer JWT) from ``TMDB_API_KEY``.
Stdlib-only (urllib + json) to match :mod:`app.stremio` — importable from any
surface without adding dependencies, and never raises (graceful degradation:
an unset key or a network blip just yields "not found", the panel hides).
"""
from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Optional

logger = logging.getLogger(__name__)

_API_ROOT = "https://api.themoviedb.org/3"
_IMG_ROOT = "https://image.tmdb.org/t/p/original"
_TIMEOUT = 10  # seconds — TMDB usually answers in <1s

# Owner's home region (Singapore). Used when the caller / settings don't pin one.
DEFAULT_REGION = "SG"

# The provider buckets TMDB/JustWatch return, in the order we want to show them:
# subscription streaming first, then free, ad-supported, rent, buy.
_BUCKETS = ("flatrate", "free", "ads", "rent", "buy")


def _token() -> Optional[str]:
    t = (os.environ.get("TMDB_API_KEY") or "").strip()
    return t or None


def is_configured() -> bool:
    """True iff a TMDB token is present. The route uses this to return a clean
    ``configured: false`` (so the UI can stay silent) instead of erroring."""
    return _token() is not None


def _get(path: str, params: Optional[dict] = None) -> Optional[dict]:
    """GET ``{API_ROOT}{path}`` with Bearer auth → JSON, or None on any failure
    (logged at debug). Never raises."""
    tok = _token()
    if not tok:
        return None
    url = f"{_API_ROOT}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    try:
        req = urllib.request.Request(url, headers={
            "Authorization": f"Bearer {tok}",
            "Accept": "application/json",
            "User-Agent": "SMDL/TMDB-Client (+https://media.az-sentinel.xyz)",
        })
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as r:
            return json.loads(r.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError) as e:
        logger.debug("tmdb GET %s failed: %s", path, e)
        return None
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        logger.debug("tmdb GET %s — bad JSON: %s", path, e)
        return None


def find_by_imdb(imdb_id: str) -> Optional[dict]:
    """Resolve an IMDB id to ``{tmdb_id, media_type, title}`` via TMDB ``/find``.

    Accepts a Stremio content id (``tt123:1:4`` for an episode); the season/
    episode suffix is stripped so we resolve to the parent movie/series. Returns
    None when it isn't an ``tt`` id or TMDB has no match.
    """
    base = (imdb_id or "").split(":", 1)[0].strip()
    if not base.startswith("tt"):
        return None
    data = _get(f"/find/{base}", {"external_source": "imdb_id"})
    if not data:
        return None
    for results_key, media_type in (("movie_results", "movie"), ("tv_results", "tv")):
        arr = data.get(results_key) or []
        if arr:
            first = arr[0]
            return {
                "tmdb_id": first.get("id"),
                "media_type": media_type,
                "title": first.get("title") or first.get("name"),
            }
    return None


def _norm_provider(p: dict) -> dict:
    logo_path = p.get("logo_path")
    return {
        "id": p.get("provider_id"),
        "name": p.get("provider_name"),
        "logo": f"{_IMG_ROOT}{logo_path}" if logo_path else None,
        "priority": p.get("display_priority"),
    }


def watch_providers_for_imdb(imdb_id: str, region: str = DEFAULT_REGION) -> dict:
    """imdb → tmdb → watch providers for ``region``.

    Always returns the same shape so the caller never branches on errors::

        {configured, found, region, link, title, media_type,
         flatrate, free, ads, rent, buy}   # each bucket: list[provider]

    ``link`` is the JustWatch page for the title in that region (the deep-link-
    out target). ``found`` is True only when the region actually has providers.
    """
    region = (region or DEFAULT_REGION).strip().upper()[:2] or DEFAULT_REGION
    out: dict = {
        "configured": is_configured(),
        "found": False,
        "region": region,
        "link": None,
        "title": None,
        "media_type": None,
    }
    for b in _BUCKETS:
        out[b] = []
    if not out["configured"]:
        return out

    hit = find_by_imdb(imdb_id)
    if not hit or not hit.get("tmdb_id"):
        return out
    out["media_type"] = hit["media_type"]
    out["title"] = hit.get("title")

    data = _get(f"/{hit['media_type']}/{hit['tmdb_id']}/watch/providers")
    results = (data or {}).get("results") or {}
    region_data = results.get(region)
    if not region_data:
        return out

    out["link"] = region_data.get("link")
    for b in _BUCKETS:
        out[b] = [_norm_provider(p) for p in (region_data.get(b) or [])]
    # "found" means there's at least one actual provider to show.
    out["found"] = any(out[b] for b in _BUCKETS)
    return out
