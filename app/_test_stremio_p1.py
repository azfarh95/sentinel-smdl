"""P1 smoke test: search → pick → resolve → print direct URL.

Run from inside the SMDL container (it has RD_API_TOKEN in env):
    docker exec -e PYTHONIOENCODING=utf-8 smdl python -m app._test_stremio_p1 inception

Or with an explicit IMDB id (faster, skips the search step):
    docker exec smdl python -m app._test_stremio_p1 --imdb tt1375666

Verifies:
  1. Cinemeta search returns hits
  2. Torrentio / Comet return torrent streams sorted by ranking
  3. Real-Debrid resolves the top magnet to a direct HTTPS URL
  4. RD account is reachable + premium-valid

Read-only — does not enqueue downloads or write to the SMDL DB.
"""
from __future__ import annotations

import argparse
import logging
import sys
from typing import Optional

from app import stremio, realdebrid


def _fmt_size(n: int | None) -> str:
    if not n:
        return "?"
    for u in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {u}"
        n /= 1024
    return f"{n:.1f} PB"


def _account_check() -> bool:
    print("─── 1. Real-Debrid account check ─────────────────────────────")
    try:
        acct = realdebrid.get_account()
    except realdebrid.RealDebridError as e:
        print(f"  ✗ FAILED: {e}")
        print("    → set RD_API_TOKEN in .env.local (get token from "
              "https://real-debrid.com/apitoken)")
        return False
    print(f"  ✓ user={acct.username}  email={acct.email}  type={acct.type}")
    if not acct.is_premium:
        print(f"  ⚠ NOT PREMIUM — magnet resolution will fail. Subscribe at real-debrid.com.")
        return False
    days_left = acct.premium_seconds_left / 86400
    print(f"  ✓ premium expires {acct.expiration_iso or '?'}  "
          f"({days_left:.0f}d left)  points={acct.points}")
    return True


def _search(query: str) -> Optional[stremio.MetaItem]:
    print(f"─── 2. Cinemeta search for {query!r} ──────────────────────────")
    hits = stremio.search(query, type_="movie", limit=8)
    if not hits:
        print("  ✗ No hits.")
        return None
    for i, h in enumerate(hits, 1):
        yr = f" ({h.year})" if h.year else ""
        rt = f"  IMDB {h.imdb_rating}" if h.imdb_rating else ""
        print(f"  {i:>2}. {h.id}  {h.name}{yr}{rt}")
    pick = hits[0]
    print(f"  → picking top hit: {pick.id}  {pick.name}")
    return pick


def _streams(imdb_id: str, quality: str = "1080p") -> Optional[stremio.StreamEntry]:
    print(f"─── 3. Stream fan-out for {imdb_id} (pref {quality}) ──────────")
    streams = stremio.get_streams(imdb_id, type_="movie")
    if not streams:
        print("  ✗ No streams returned by any addon.")
        return None
    streams = stremio.rank_streams(streams, preferred_quality=quality)
    print(f"  Found {len(streams)} streams. Top 8:")
    for i, s in enumerate(streams[:8], 1):
        size = _fmt_size(s.size_bytes)
        seed = f"{s.seeders}↑" if s.seeders else "?↑"
        print(f"  {i:>2}. [{s.source_addon[:20]:<20}] {s.quality or '?':<6} "
              f"{size:>9}  {seed:<8}  {s.title[:60]}")
    pick = streams[0]
    print(f"  → picking: {pick.title[:80]!r}")
    if not pick.magnet:
        print(f"  ✗ Top pick has no magnet (infohash={pick.infohash})")
        return None
    return pick


def _resolve(stream: stremio.StreamEntry) -> None:
    print(f"─── 4. Real-Debrid resolution ─────────────────────────────────")
    print(f"  Submitting magnet (first 80 chars): {stream.magnet[:80]}...")
    try:
        files = realdebrid.magnet_to_direct_urls(stream.magnet, timeout=120)
    except realdebrid.RealDebridError as e:
        print(f"  ✗ RD failed: {e}")
        return
    print(f"  ✓ Got {len(files)} direct URL(s):")
    for f in files:
        print(f"     {f.filename}  ({_fmt_size(f.filesize)})  type={f.mime_type}")
        print(f"     → {f.direct_url[:120]}...")
    print()
    print("  Next: feed this URL into <video src=...> or aria2c.")


def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    ap = argparse.ArgumentParser(description="SMDL Stremio + RD P1 smoke test")
    ap.add_argument("query", nargs="?", help="Movie title to search for")
    ap.add_argument("--imdb", help="Skip search, use this IMDB id directly")
    ap.add_argument("--quality", default="1080p", help="Preferred quality")
    ap.add_argument("--no-resolve", action="store_true",
                     help="Skip RD step (catalog/stream only)")
    args = ap.parse_args()

    if not (args.query or args.imdb):
        print("Pass a search query or --imdb <id>", file=sys.stderr)
        sys.exit(2)

    if not args.no_resolve:
        if not _account_check():
            sys.exit(1)
        print()

    if args.imdb:
        imdb_id = args.imdb
    else:
        pick = _search(args.query)
        if not pick:
            sys.exit(1)
        imdb_id = pick.id
        print()

    stream = _streams(imdb_id, quality=args.quality)
    if not stream:
        sys.exit(1)
    print()

    if not args.no_resolve:
        _resolve(stream)


if __name__ == "__main__":
    main()
