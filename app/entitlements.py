"""Plan → capability map and the entitlement gate.

This is the *commercial* rail of the two-rail model (see the planning doc
`sentinel-docs/docs/planning/media-licensing-entitlements.md`). It answers
"is this grant allowed to use capability X?" and is sellable per-plan. It is
deliberately separate from the *legal-boundary* rail (edition.py + the
middleware path gate in main.py), which is a per-deployment hard 404 and is
NEVER a plan/SKU.

Design constraints held here:
- Capabilities are a flat, namespaced set `smdl.<area>.<cap>` so new media
  verticals plug in as data, not schema changes. Names stay consistent with
  the existing principal scope namespace (smdl.iptv, smdl.stickers, …).
- Plans are CUMULATIVE: each higher plan is a superset of the one below, so
  membership is a single frozenset test.
- Plan derives from the key tier as a documented BOOTSTRAP (community→free,
  family→family) until the central registry returns an authoritative plan.
  When the registry is wired, `enrich` will prefer an explicit row["plan"].
- This module stays dependency-light: stdlib + fastapi.HTTPException only. It
  does NOT import licensing, so licensing.py keeps its stdlib-pure contract.
  Grant enrichment happens in the HTTP layer, not in licensing.build_grant.
"""
from __future__ import annotations

from fastapi import HTTPException

# --- Capabilities (commercial rail; namespaced, flat) ---------------------
# TV / IPTV
CAP_TV_BROWSE = "smdl.tv.browse"       # browse + search guide (free baseline)
CAP_TV_PLAY = "smdl.tv.play"           # play a stream
CAP_TV_EPG = "smdl.tv.epg"             # rich EPG / now-next
CAP_TV_FAVORITES = "smdl.tv.favorites"  # saved channels (needs an account)
CAP_TV_RECORDER = "smdl.tv.recorder"   # cloud/local recording
CAP_TV_MULTIVIEW = "smdl.tv.multiview"  # simultaneous streams

# Downloader / library
CAP_LIBRARY_BROWSE = "smdl.library.browse"
CAP_DOWNLOAD = "smdl.download"          # initiate a download
CAP_DOWNLOAD_HD = "smdl.download.hd"    # high-bitrate / batch

# Stickers
CAP_STICKERS = "smdl.stickers"

# Watchlist / sync (account-tier conveniences)
CAP_WATCHLIST = "smdl.watchlist"
CAP_SYNC = "smdl.sync"

# --- Plans (cumulative supersets) -----------------------------------------
# free:       anonymous + free-registered baseline. Near-zero-cost acts.
# registered: free + account conveniences (favorites, watchlist, sync).
# plus:       paid individual — recorders, HD, multiview, stickers.
# family:     plus + multi-seat (the Family/private tier).
_FREE = frozenset({
    CAP_TV_BROWSE,
    CAP_TV_PLAY,
    CAP_LIBRARY_BROWSE,
    CAP_DOWNLOAD,
})
_REGISTERED = _FREE | {
    CAP_TV_FAVORITES,
    CAP_TV_EPG,
    CAP_WATCHLIST,
    CAP_SYNC,
}
_PLUS = _REGISTERED | {
    CAP_TV_RECORDER,
    CAP_TV_MULTIVIEW,
    CAP_DOWNLOAD_HD,
    CAP_STICKERS,
}
_FAMILY = _PLUS  # same caps; difference is seats, carried in limits

PLANS: dict[str, frozenset[str]] = {
    "free": _FREE,
    "registered": _REGISTERED,
    "plus": _PLUS,
    "family": _FAMILY,
}

# Bootstrap mapping from license tier → plan, used until the registry returns
# an authoritative plan for the key. Keep in sync with licensing.TIER_* values.
_PLAN_BY_TIER = {
    "community": "free",
    "family": "family",
}

_DEFAULT_PLAN = "free"


def resolve_plan(tier: str | None, plan: str | None = None) -> str:
    """Pick the effective plan for a key.

    An explicit, known `plan` (e.g. from the registry) wins. Otherwise derive
    from the key `tier` as a bootstrap. Unknown values fall back to free.

    >>> resolve_plan("community")
    'free'
    >>> resolve_plan("family")
    'family'
    >>> resolve_plan("community", "plus")
    'plus'
    >>> resolve_plan("family", "bogus")
    'family'
    >>> resolve_plan(None)
    'free'
    """
    if plan and plan in PLANS:
        return plan
    if tier:
        mapped = _PLAN_BY_TIER.get(tier.strip().lower())
        if mapped:
            return mapped
    return _DEFAULT_PLAN


def entitlements_for(plan: str) -> list[str]:
    """Sorted list of capabilities granted by a plan (unknown → free).

    >>> entitlements_for("free") == sorted(PLANS["free"])
    True
    >>> "smdl.tv.recorder" in entitlements_for("plus")
    True
    >>> "smdl.tv.recorder" in entitlements_for("free")
    False
    """
    return sorted(PLANS.get(plan, PLANS[_DEFAULT_PLAN]))


# --- Capability catalog (UI-facing metadata) ------------------------------
# Plans low→high, so min_plan_for() reports the cheapest tier that unlocks a
# cap. Keep aligned with PLANS above.
_PLAN_ORDER = ("free", "registered", "plus", "family")

# cap → (human label, area). Drives the lock badges + upsell copy in the UI.
CAP_META: dict[str, tuple[str, str]] = {
    CAP_TV_BROWSE:      ("Browse the TV guide", "tv"),
    CAP_TV_PLAY:        ("Play a channel", "tv"),
    CAP_TV_EPG:         ("Rich EPG / now-next", "tv"),
    CAP_TV_FAVORITES:   ("Save favourite channels", "tv"),
    CAP_TV_RECORDER:    ("Record live TV", "tv"),
    CAP_TV_MULTIVIEW:   ("Watch multiple streams at once", "tv"),
    CAP_LIBRARY_BROWSE: ("Browse the library", "library"),
    CAP_DOWNLOAD:       ("Download a link", "downloader"),
    CAP_DOWNLOAD_HD:    ("HD / batch downloads", "downloader"),
    CAP_STICKERS:       ("Make stickers", "stickers"),
    CAP_WATCHLIST:      ("Stream watchlist", "watchlist"),
    CAP_SYNC:           ("Cross-device sync", "watchlist"),
}


def min_plan_for(cap: str) -> str | None:
    """The cheapest plan that unlocks `cap`, or None if no plan grants it.

    >>> min_plan_for("smdl.tv.play")
    'free'
    >>> min_plan_for("smdl.tv.recorder")
    'plus'
    >>> min_plan_for("smdl.tv.favorites")
    'registered'
    >>> min_plan_for("smdl.nope") is None
    True
    """
    for plan in _PLAN_ORDER:
        if cap in PLANS[plan]:
            return plan
    return None


def catalog() -> list[dict]:
    """Static capability catalog: every known cap with its label, area, and the
    minimum plan that unlocks it. The UI overlays the caller's grant on top to
    decide what to badge as locked / show an upsell for."""
    return [
        {"cap": cap, "label": label, "area": area, "min_plan": min_plan_for(cap)}
        for cap, (label, area) in CAP_META.items()
    ]


def enrich(row: dict) -> dict:
    """Build the entitlement block to merge into a validate grant.

    `row` is the persisted license row. Uses row["plan"] when present (registry
    path), else bootstraps from row["tier"]. Seats come from the row so the
    client can enforce multiview/family limits locally within the grace window.
    """
    plan = resolve_plan(row.get("tier"), row.get("plan"))
    seats = row.get("seats")
    try:
        seats = int(seats) if seats is not None else 1
    except (TypeError, ValueError):
        seats = 1
    return {
        "plan": plan,
        "entitlements": entitlements_for(plan),
        "limits": {"seats": seats},
    }


def has_entitlement(grant: dict, cap: str) -> bool:
    """True if an (already-validated) grant carries capability `cap`.

    Prefers the grant's own entitlement list (so a client/registry can grant
    extras beyond the plan); falls back to the plan map.

    >>> has_entitlement({"entitlements": ["smdl.tv.play"]}, "smdl.tv.play")
    True
    >>> has_entitlement({"plan": "plus"}, "smdl.tv.recorder")
    True
    >>> has_entitlement({"plan": "free"}, "smdl.tv.recorder")
    False
    """
    if not grant or not grant.get("valid", True):
        return False
    listed = grant.get("entitlements")
    if isinstance(listed, (list, tuple, set, frozenset)):
        return cap in listed
    return cap in PLANS.get(grant.get("plan", _DEFAULT_PLAN), PLANS[_DEFAULT_PLAN])


def require_entitlement(grant: dict, cap: str) -> None:
    """Raise 402 unless the grant carries `cap`. The commercial-rail gate.

    402 (Payment Required) is the sellable signal — distinct from the
    legal-boundary rail's 404 (capability does not exist on this deployment).

    The detail is a STRUCTURED object so the client can render a paywall sheet
    without string-parsing: {error, cap, required_plan}. `required_plan` is the
    cheapest plan that unlocks the cap (the upsell target).
    """
    if not has_entitlement(grant, cap):
        raise HTTPException(
            status_code=402,
            detail={
                "error": "entitlement_required",
                "cap": cap,
                "required_plan": min_plan_for(cap),
            },
        )
