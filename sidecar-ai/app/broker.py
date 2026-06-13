"""GPU broker gate (watchdog v2).

IMPORTANT — how the broker actually models the GPU (verified against
sentinel-watchdog/daemon/gpu_broker.py):

  * Only **FLUX** acquires/releases a lease (`acquire_lease` rejects any consumer
    that isn't 'flux'). So media-ai must NOT try to lease — it is an LLM-class
    consumer (it drives Qwen via llama-swap, same VRAM class as Qwen).
  * The broker GATES Qwen by policy: in auto mode it blocks Qwen whenever FLUX or
    gaming holds the card (`qwen_allowed = not gaming and not flux`), actuating
    the gate by POSTing /infer_block + evicting the model.
  * Therefore the correct citizen behaviour for a Qwen consumer is: read
    `GET /api/v2/gpu-broker/status`, and if `qwen_allowed` is false, DEFER (don't
    load Qwen) rather than collide. Once we do load Qwen, the broker's own
    `_qwen_loaded` probe makes it deny new FLUX leases — so the protection is
    mutual for any consumer that leases.

Blind spot to be aware of: a GPU user that does NOT lease (e.g. the owner's
arcade-forge cockpit driving ComfyUI directly) is invisible to the broker, so
`qwen_allowed` can read true while that render is live. The fix is to make such
users lease; this gate honours whatever the broker can see.

FAIL-OPEN: if the broker is disabled or unreachable, the gate returns allowed so
media-ai keeps working when the watchdog is down (matches the FLUX path's
fail-open and the suite's "non-critical features degrade, don't hard-fail").
"""
from __future__ import annotations

import json
import logging
import urllib.request

from . import config

logger = logging.getLogger("media-ai.broker")


def status() -> dict | None:
    """GET the broker status, or None if disabled/unreachable."""
    if not config.GPU_BROKER_ENABLED:
        return None
    url = f"{config.GPU_BROKER_URL}/api/v2/gpu-broker/status"
    headers = {}
    if config.GPU_BROKER_TOKEN:
        headers["X-Sentinel-Service-Token"] = config.GPU_BROKER_TOKEN
    try:
        req = urllib.request.Request(url, headers=headers, method="GET")
        with urllib.request.urlopen(req, timeout=5) as r:
            return json.loads(r.read())
    except Exception as e:  # noqa: BLE001
        logger.warning("gpu broker status failed (%s)", e)
        return None


def gpu_gate() -> tuple[bool, str]:
    """May an LLM-class job (Qwen summary) use the GPU right now?

    Returns (allowed, reason). Honours the broker's `qwen_allowed`. Fail-open:
    broker disabled/unreachable -> allowed (with a reason saying so)."""
    if not config.GPU_BROKER_ENABLED:
        return True, "broker disabled (fail-open)"
    st = status()
    if st is None:
        return True, "broker unreachable (fail-open)"
    allowed = bool(st.get("qwen_allowed", True))
    reason = st.get("reason", "")
    if not allowed:
        # Surface what's holding the card so the deferral is explainable.
        bits = []
        if st.get("flux_active"):
            bits.append("flux render active")
        if st.get("gaming_active"):
            bits.append(f"gaming ({st.get('gaming_game') or 'a game'})")
        why = ", ".join(bits) or reason or "gpu busy"
        return False, f"broker: {why}"
    return True, f"broker: {reason}" if reason else "broker: allowed"
