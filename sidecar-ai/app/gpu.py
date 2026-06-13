"""Physical VRAM-headroom preflight — catches GPU users the broker can't see.

The watchdog broker only knows about consumers that LEASE (FLUX) or that it
actuates (Qwen via llama-swap). A consumer that holds VRAM WITHOUT leasing — the
owner's arcade-forge cockpit keeps the ~12 GB FLUX model resident in ComfyUI even
when its queue is idle — is invisible to the broker, so `qwen_allowed` reads true
while the card is half full. Loading the 27B (~16 GB) into the ~12 GB that's left
then fails (llama-swap "health check timed out"). VERIFIED 2026-06-13: that exact
collision.

So before invoking Qwen, media-ai also checks the PHYSICAL free VRAM and defers
when there isn't enough headroom — regardless of what the broker believes.

Source: ComfyUI's /system_stats (the VRAM oracle already running on this box).
Best-effort + degrade-dark: MEDIA_AI_GPU_STATS_URL unset or unreachable ->
free_vram_gb() returns None and the caller skips the check (broker gate only).
"""
from __future__ import annotations

import json
import logging
import urllib.request

from . import config

logger = logging.getLogger("media-ai.gpu")


def free_vram_gb() -> float | None:
    """Free VRAM in GB from ComfyUI /system_stats, or None if not configured /
    unreachable / unparseable."""
    if not config.GPU_STATS_URL:
        return None
    try:
        with urllib.request.urlopen(config.GPU_STATS_URL + "/system_stats", timeout=4) as r:
            d = json.loads(r.read())
        dev = (d.get("devices") or [])[0]
        return float(dev["vram_free"]) / 1e9
    except Exception as e:  # noqa: BLE001
        logger.warning("vram stats unavailable (%s)", e)
        return None


def vram_gate() -> tuple[bool, str]:
    """Is there enough free VRAM to load the LLM? Returns (ok, reason). Skipped
    (ok=True) when no stats source is configured/reachable."""
    free = free_vram_gb()
    if free is None:
        return True, "vram check skipped (no stats source)"
    if free < config.MIN_VRAM_GB:
        return False, (f"only {free:.1f} GB VRAM free (< {config.MIN_VRAM_GB:.0f} GB "
                       "needed) - a resident FLUX model the broker can't see is "
                       "likely holding the card")
    return True, f"{free:.1f} GB VRAM free"
