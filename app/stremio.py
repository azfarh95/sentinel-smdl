"""Stremio addon-protocol consumer.

SMDL uses the Stremio addon protocol (https://stremio.github.io/stremio-addon-sdk/)
as an upstream data source — NOT as a server we expose to Stremio clients.
We're the client; Stremio addons (Cinemeta, Torrentio, Comet, etc.) are our
sources. Playback happens in the SMDL Mini App player, not in Stremio's apps.

The protocol is simple HTTPS JSON. Each addon publishes a `/manifest.json`
declaring what resources it provides (catalog / meta / stream / subtitles).
We hit:
  - Cinemeta              — movie/series metadata + search
  - Torrentio, Comet, etc — torrent stream lists

Stream entries from torrent-flavoured addons carry magnet URIs (or magnet
hashes); these get fed to Real-Debrid in `realdebrid.py` to produce a
playable direct HTTPS URL.

All network calls are stdlib-only (urllib + json) so this module is
importable from any surface — bot, miniapp, or CLI smoke test — without
adding dependencies.
"""
from __future__ import annotations

import json
import logging
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Iterable, Optional

logger = logging.getLogger(__name__)

# ── Default addon list ───────────────────────────────────────────────────────
# Public, free, no-auth. Owner can override via smdl.json's "stremio_addons"
# key (added in config defaults in a follow-up; this list is the fallback).
#
# Manifest URLs (we hit the addon's /catalog/, /meta/, /stream/ endpoints
# directly — the manifest URL just identifies the root):
DEFAULT_ADDONS: list[str] = [
    # ─ Catalog / metadata ─
    "https://v3-cinemeta.strem.io/manifest.json",        # movies + series, official
    # ─ Stream providers (torrent-flavoured, RD-friendly) ─
    "https://torrentio.strem.fun/manifest.json",         # broadest selection
    "https://comet.elfhosted.com/manifest.json",         # RD-optimised, fast
    "https://mediafusion.elfhosted.com/manifest.json",   # multi-debrid backup
    # ─ Subtitles ─
    "https://opensubtitles-v3.strem.io/manifest.json",   # multilingual subs
]


# ── HTTP helpers ────────────────────────────────────────────────────────────
_DEFAULT_TIMEOUT = 12  # seconds — Stremio addons usually respond in 1-3s
_USER_AGENT = "SMDL/Stremio-Client (+https://media.az-sentinel.xyz)"


def _http_get_json(url: str, timeout: int = _DEFAULT_TIMEOUT) -> Optional[dict]:
    """GET → JSON. Returns None on any failure (logged at debug). Never raises;
    upstream addons go down all the time and we want graceful degradation."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT,
                                                     "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read()
        return json.loads(body.decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError) as e:
        logger.debug("addon GET %s failed: %s", url, e)
        return None
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        logger.debug("addon GET %s — bad JSON: %s", url, e)
        return None


def _addon_root(manifest_url: str) -> str:
    """Strip `/manifest.json` to get the addon's root URL (where /catalog,
    /meta, /stream live)."""
    if manifest_url.endswith("/manifest.json"):
        return manifest_url[: -len("/manifest.json")]
    return manifest_url.rstrip("/")


# ── Manifest discovery ──────────────────────────────────────────────────────
_manifest_cache: dict[str, dict] = {}


def get_manifest(manifest_url: str) -> Optional[dict]:
    """Fetch + cache an addon's manifest. The manifest tells us which
    resources/types/ids the addon advertises so we only hit relevant
    endpoints. Cached for the process lifetime — restart SMDL to refresh."""
    if manifest_url in _manifest_cache:
        return _manifest_cache[manifest_url]
    data = _http_get_json(manifest_url)
    if data:
        _manifest_cache[manifest_url] = data
    return data


# ── Search (Cinemeta) ───────────────────────────────────────────────────────
@dataclass
class MetaItem:
    """One movie/series result from a Stremio catalog query."""
    id: str                   # IMDB id (tt1375666) or addon-prefixed id
    type: str                 # "movie" | "series"
    name: str
    year: Optional[int]
    poster: Optional[str]
    description: Optional[str]
    imdb_rating: Optional[float]
    genres: list[str]

    @classmethod
    def from_addon_json(cls, j: dict) -> "MetaItem":
        # Year — addons report as int or as range string "2010-2014"
        yr = j.get("year") or j.get("releaseInfo")
        if isinstance(yr, str):
            m = re.match(r"(\d{4})", yr)
            yr = int(m.group(1)) if m else None
        rating = j.get("imdbRating") or j.get("imdb_rating")
        try:
            rating = float(rating) if rating is not None else None
        except (TypeError, ValueError):
            rating = None
        return cls(
            id=j.get("id", ""),
            type=j.get("type", "movie"),
            name=j.get("name", ""),
            year=yr if isinstance(yr, int) else None,
            poster=j.get("poster") or j.get("posterShape"),
            description=j.get("description"),
            imdb_rating=rating,
            genres=j.get("genres") or [],
        )


def search(query: str, type_: str = "movie",
            addons: Optional[Iterable[str]] = None,
            limit: int = 25) -> list[MetaItem]:
    """Search across the configured catalog addons. Cinemeta is the canonical
    metadata source — most queries return from there.

    Addon protocol query shape:
        {root}/catalog/{type}/{catalog_id}/search={query}.json
    Cinemeta's search catalog id is "top" for movies/series.
    """
    addons = list(addons) if addons else DEFAULT_ADDONS
    results: list[MetaItem] = []
    seen_ids: set[str] = set()
    qs = urllib.parse.quote(query.strip())
    if not qs:
        return results
    for manifest_url in addons:
        manifest = get_manifest(manifest_url)
        if not manifest:
            continue
        # Skip if the addon doesn't advertise the requested type or catalog
        types = manifest.get("types") or []
        if type_ not in types:
            continue
        catalogs = manifest.get("catalogs") or []
        # Find a searchable catalog matching our type
        catalog_id = None
        for c in catalogs:
            if c.get("type") != type_:
                continue
            # extra.search OR extraSupported includes "search" → searchable
            extra = c.get("extra") or []
            supports_search = (
                any((e.get("name") == "search") for e in extra)
                or "search" in (c.get("extraSupported") or [])
            )
            if supports_search:
                catalog_id = c.get("id")
                break
        if not catalog_id:
            continue
        root = _addon_root(manifest_url)
        url = f"{root}/catalog/{type_}/{catalog_id}/search={qs}.json"
        data = _http_get_json(url)
        if not data or "metas" not in data:
            continue
        for m in data["metas"]:
            mid = m.get("id")
            if not mid or mid in seen_ids:
                continue
            seen_ids.add(mid)
            results.append(MetaItem.from_addon_json(m))
            if len(results) >= limit:
                return results
    return results


def get_meta(imdb_id: str, type_: str = "movie",
              addons: Optional[Iterable[str]] = None) -> Optional[MetaItem]:
    """Hydrate one title's full metadata. Returns first hit across addons."""
    addons = list(addons) if addons else DEFAULT_ADDONS
    for manifest_url in addons:
        manifest = get_manifest(manifest_url)
        if not manifest:
            continue
        if "meta" not in (manifest.get("resources") or []):
            # `resources` can be ["catalog","meta","stream"] OR a list of dicts
            # — handle both shapes.
            res_dicts = [r for r in (manifest.get("resources") or []) if isinstance(r, dict)]
            if not any(r.get("name") == "meta" for r in res_dicts):
                continue
        root = _addon_root(manifest_url)
        url = f"{root}/meta/{type_}/{imdb_id}.json"
        data = _http_get_json(url)
        if data and "meta" in data:
            return MetaItem.from_addon_json(data["meta"])
    return None


# ── Series episode metadata ────────────────────────────────────────────────
@dataclass
class EpisodeMeta:
    """One episode in a series (Stremio addon convention)."""
    id: str               # 'tt0903747:1:1' — addon stream-content_id
    imdb_id: str          # parent series imdb id
    season: int
    episode: int
    title: str
    released: Optional[str]
    overview: Optional[str]
    thumbnail: Optional[str]
    runtime: Optional[int]   # minutes


def get_series_episodes(imdb_id: str,
                          addons: Optional[Iterable[str]] = None
                          ) -> list[EpisodeMeta]:
    """Pull the full episode list for a series. Cinemeta returns `videos`
    inside the meta payload — each entry is a season/episode tuple. We
    flatten and return ordered (S1E1, S1E2, ..., S2E1, ...).

    The returned id (`tt0903747:1:1`) is exactly what /stream/<type>/<id>
    expects — caller can hand it straight to get_streams()."""
    addons = list(addons) if addons else DEFAULT_ADDONS
    for manifest_url in addons:
        manifest = get_manifest(manifest_url)
        if not manifest:
            continue
        # Need full meta (Cinemeta is the only one that returns videos)
        root = _addon_root(manifest_url)
        url = f"{root}/meta/series/{imdb_id}.json"
        data = _http_get_json(url)
        if not data or "meta" not in data:
            continue
        videos = data["meta"].get("videos") or []
        out: list[EpisodeMeta] = []
        for v in videos:
            season = int(v.get("season") or 0)
            episode = int(v.get("episode") or v.get("number") or 0)
            if season < 1 or episode < 1:
                # Specials / season 0 — skip; caller can filter back in
                continue
            eid = v.get("id") or f"{imdb_id}:{season}:{episode}"
            runtime = v.get("runtime")
            if isinstance(runtime, str):
                m = re.search(r"(\d+)", runtime)
                runtime = int(m.group(1)) if m else None
            out.append(EpisodeMeta(
                id=eid, imdb_id=imdb_id,
                season=season, episode=episode,
                title=v.get("title") or v.get("name") or f"S{season:02d}E{episode:02d}",
                released=v.get("released") or v.get("firstAired"),
                overview=v.get("overview") or v.get("description"),
                thumbnail=v.get("thumbnail"),
                runtime=runtime,
            ))
        out.sort(key=lambda e: (e.season, e.episode))
        if out:
            return out
    return []


# ── Streams (Torrentio, Comet, etc.) ────────────────────────────────────────
@dataclass
class StreamEntry:
    """One torrent stream candidate from a stream-provider addon."""
    title: str                 # human-readable label ("1080p WEB-DL · 8GB · 80↑")
    infohash: Optional[str]    # 40-char hex — the unique torrent id
    magnet: Optional[str]      # full magnet:?xt=... URI
    sources: list[str]         # tracker URLs (Stremio addon convention)
    file_index: Optional[int]  # for multi-file torrents (season packs)
    size_bytes: Optional[int]  # parsed from title when present
    seeders: Optional[int]     # parsed from title when present
    quality: Optional[str]     # "1080p" / "720p" / "4K" parsed from title
    source_addon: str          # which addon returned this stream

    @classmethod
    def from_addon_json(cls, j: dict, addon_name: str) -> "StreamEntry":
        title = j.get("title", "") or j.get("name", "")
        infohash = (j.get("infoHash") or "").lower() or None
        # Build magnet from infohash + sources if not provided directly
        magnet = j.get("url") if (j.get("url") or "").startswith("magnet:") else None
        if not magnet and infohash:
            sources = j.get("sources") or []
            trackers = "&".join(f"tr={urllib.parse.quote(s, safe='')}"
                                  for s in sources if s.startswith("tracker:"))
            # Some addons prefix sources with "tracker:"; others give bare urls.
            if not trackers:
                trackers = "&".join(f"tr={urllib.parse.quote(s, safe='')}"
                                     for s in sources if not s.startswith("dht:"))
            magnet = f"magnet:?xt=urn:btih:{infohash}"
            if trackers:
                magnet += "&" + trackers
        sources = j.get("sources") or []
        # Title parsing for quality/size/seeders — torrent-addon convention
        title_l = title.lower()
        m_size = re.search(r"(\d+(?:\.\d+)?)\s*(gb|mb)", title_l)
        size_bytes = None
        if m_size:
            v = float(m_size.group(1))
            unit = m_size.group(2)
            size_bytes = int(v * (1024 ** 3 if unit == "gb" else 1024 ** 2))
        m_seed = re.search(r"👤\s*(\d+)|seeders?\s*[:=]?\s*(\d+)|\b(\d+)↑", title)
        seeders = None
        if m_seed:
            seeders = int(next(g for g in m_seed.groups() if g))
        qmatch = re.search(r"(2160p|1440p|1080p|720p|480p|4k)", title_l)
        quality = qmatch.group(1).replace("4k", "2160p") if qmatch else None
        return cls(
            title=title, infohash=infohash, magnet=magnet,
            sources=sources,
            file_index=j.get("fileIdx"),
            size_bytes=size_bytes, seeders=seeders, quality=quality,
            source_addon=addon_name,
        )


def get_streams(content_id: str, type_: str = "movie",
                  addons: Optional[Iterable[str]] = None,
                  per_addon_timeout: int = _DEFAULT_TIMEOUT
                  ) -> list[StreamEntry]:
    """Fan-out across stream-provider addons for one title/episode.

    `content_id` is typically an IMDB id (tt1375666) or with season/ep
    (tt1234567:1:5).

    Stream order matters — addons usually pre-sort by their own quality
    heuristic. We preserve that order per-addon and concatenate, dedupe
    by infohash. Caller can re-rank by `quality` / `seeders` / `size`.
    """
    addons = list(addons) if addons else DEFAULT_ADDONS
    out: list[StreamEntry] = []
    seen: set[str] = set()
    for manifest_url in addons:
        manifest = get_manifest(manifest_url)
        if not manifest:
            continue
        # Filter: addon must advertise "stream" for this type
        resources = manifest.get("resources") or []
        provides_stream = (
            "stream" in resources
            or any(isinstance(r, dict) and r.get("name") == "stream"
                   and (not r.get("types") or type_ in r["types"])
                   for r in resources)
        )
        if not provides_stream:
            continue
        addon_name = manifest.get("name") or _addon_root(manifest_url)
        root = _addon_root(manifest_url)
        url = f"{root}/stream/{type_}/{content_id}.json"
        data = _http_get_json(url, timeout=per_addon_timeout)
        if not data or "streams" not in data:
            continue
        for s in data["streams"]:
            entry = StreamEntry.from_addon_json(s, addon_name)
            if entry.infohash and entry.infohash in seen:
                continue
            if entry.infohash:
                seen.add(entry.infohash)
            out.append(entry)
    return out


def rank_streams(streams: list[StreamEntry],
                 preferred_quality: str = "1080p") -> list[StreamEntry]:
    """Re-rank streams: preferred-quality first (by seeders desc), then
    other qualities by quality-desc then seeders-desc. Streams with no
    quality info float to the bottom."""
    quality_rank = {"2160p": 4, "1440p": 3, "1080p": 2, "720p": 1, "480p": 0}

    def key(s: StreamEntry):
        is_preferred = (s.quality == preferred_quality)
        qr = quality_rank.get(s.quality or "", -1)
        seed = s.seeders or 0
        return (0 if is_preferred else 1, -qr, -seed)

    return sorted(streams, key=key)
