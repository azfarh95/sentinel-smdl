"""Edition flag for Sentinel Media.

Two editions:

  community  (DEFAULT, safe-by-default)
    Engine + UX only. No bundled restream source lists, no torrent /
    Real-Debrid integration, no server-side HLS relay. YouTube is shown
    via the official IFrame Player API (a permitted embedding use), not
    re-served through our relay. This is the build that is safe to share
    or sell as a platform: it ships no content and no grey-area plumbing.

  private    (the operator's own instance)
    Everything: bundled restream catalogues, torrent / RD download +
    cache, the same-origin HLS relay, country quick-refresh, etc. This
    is for personal/home use where the operator supplies their own
    sources and accepts responsibility for them.

The flag is read once from the environment. Default is intentionally
``community`` so a fresh checkout / image is the safe build unless the
operator explicitly opts in by setting SENTINEL_MEDIA_EDITION=private.
"""

import os

EDITION = os.environ.get("SENTINEL_MEDIA_EDITION", "community").strip().lower()

if EDITION not in ("community", "private"):
    EDITION = "community"


def is_private() -> bool:
    """True when running the operator's full-featured private build."""
    return EDITION == "private"


def is_community() -> bool:
    """True for the safe, shippable community build."""
    return EDITION == "community"
