"""Build sub-profile for Sentinel Media — layered on top of edition.py.

`edition.py` answers the *legal-boundary* question (community vs private:
which source classes the deployment is allowed to reach). `profile.py` answers
the *distribution* question: how this same community build is packaged and
which store policy it must satisfy.

Profiles:

  ""      (DEFAULT, no sub-profile)
      The build behaves exactly per its edition. Self-hosted APK / web.

  "play"  (Google Play Store build — a TIGHTENED community sub-profile)
      A build flag, NOT a third app. Reuses the edition/manifest machinery.
      `play` = community MINUS two things, to satisfy Play policy:
        1. third-party aggregation — forced off even if mis-deployed on a
           private edition (belt-and-suspenders; see private_sources_allowed).
        2. external-key redeem UI / off-Play pricing links — the only paid
           rail in a Play build is Google Play Billing (Play's payments rule).

Surface (TV-first rollout):
      We ship Sentinel Media service-by-service. SENTINEL_MEDIA_SURFACE picks
      which vertical(s) a build fronts. The first Play app is "tv" only. A
      play build defaults to the "tv" surface; other builds default to "all".

Everything is read once from the environment, mirroring edition.py.
"""

import os

from . import edition

PROFILE = os.environ.get("SENTINEL_MEDIA_PROFILE", "").strip().lower()

if PROFILE not in ("", "play"):
    PROFILE = ""

_ALL_SURFACES = frozenset({"tv", "downloader", "stickers", "library"})


def is_play() -> bool:
    """True when this is the tightened Google Play Store build."""
    return PROFILE == "play"


def private_sources_allowed() -> bool:
    """Whether bundled third-party aggregation catalogues may load.

    A play build can NEVER reach them, regardless of edition — this is the
    hard gate that keeps grey plumbing out of a store-distributed binary.
    """
    return edition.is_private() and not is_play()


def allow_key_redeem() -> bool:
    """Whether the external license-key redeem UI may be shown.

    Off in a play build: Play forbids steering users to off-store payment.
    Paid unlocks in a play build go through Play Billing only.
    """
    return not is_play()


def allow_off_store_pricing() -> bool:
    """Whether off-store pricing / 'buy on our website' links may be shown."""
    return not is_play()


def billing_rail() -> str:
    """The purchase rail this build uses: 'play' (Play Billing) or 'license'."""
    return "play" if is_play() else "license"


def surfaces() -> frozenset:
    """Which media verticals this build exposes.

    SENTINEL_MEDIA_SURFACE is a comma list (e.g. "tv" or "tv,library"); "all"
    or empty means every vertical. A play build defaults to "tv" so the first
    store app is Sentinel Media TV only; other builds default to "all".
    """
    raw = (os.environ.get("SENTINEL_MEDIA_SURFACE") or "").strip().lower()
    if not raw:
        return frozenset({"tv"}) if is_play() else _ALL_SURFACES
    if raw == "all":
        return _ALL_SURFACES
    picked = {s.strip() for s in raw.split(",") if s.strip()}
    valid = picked & _ALL_SURFACES
    return frozenset(valid) if valid else _ALL_SURFACES


def surface_enabled(name: str) -> bool:
    """True if vertical `name` (e.g. 'tv') is exposed in this build."""
    return name.strip().lower() in surfaces()
