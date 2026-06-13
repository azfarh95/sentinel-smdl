"""GPU broker lease client (watchdog v2).

Ported from SMDL's flux_forge.broker_lease. Used by the (future) GPU
transcription engine and by Phase-2 Qwen summary so a job doesn't fight Qwen /
FLUX / a game for the 24 GB card. FAILS OPEN: if the broker is disabled or
unreachable, acquire returns True so the sidecar keeps working when the watchdog
is down. Phase 1 is CPU-only and never calls this.
"""
from __future__ import annotations

import json
import logging
import urllib.request

from . import config

logger = logging.getLogger("media-ai.broker")


def lease(action: str, *, consumer: str, holder: str = "media-ai") -> bool:
    """action in {'acquire','release'}. Returns True on acquire iff the job may
    proceed on the GPU. consumer is the broker's resource class ('llm' for
    Qwen/whisper.cpp inference, 'flux' for image gen)."""
    if not config.GPU_BROKER_ENABLED:
        return True
    url = f"{config.GPU_BROKER_URL}/api/v2/gpu-broker/lease/{action}"
    headers = {"Content-Type": "application/json"}
    if config.GPU_BROKER_TOKEN:
        headers["X-Sentinel-Service-Token"] = config.GPU_BROKER_TOKEN
    try:
        req = urllib.request.Request(
            url,
            data=json.dumps({"consumer": consumer, "holder": holder}).encode(),
            headers=headers,
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as r:
            d = json.loads(r.read())
        if action == "acquire":
            granted = bool(d.get("granted"))
            if not granted:
                logger.info("gpu broker denied %s lease: %s", consumer, d.get("reason", ""))
            return granted
        return True
    except Exception as e:  # noqa: BLE001
        logger.warning("gpu broker %s failed (%s) — fail-open", action, e)
        return True
