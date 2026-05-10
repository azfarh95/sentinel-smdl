"""Operational config — reads /config/smdl.json at startup.

Edit smdl.json and restart the container to apply changes.
Sensitive values (bot token, paths) stay in docker-compose env vars.
"""

import json
import logging
import os
from pathlib import Path

_CONFIG_FILE = os.environ.get("CONFIG_FILE", "/config/smdl.json")

_DEFAULTS: dict = {
    "delete_after_send":      False,
    "default_quality":        "1080p",
    "max_concurrent_downloads": 2,
    "temp_ttl_hours":         24,
    "owner_chat_id":          None,
    "allowed_chat_ids":       [],
    # Livestream recording (v2)
    "live_enabled":           True,
    "live_max_concurrent":    1,
    "live_heartbeat_seconds": 300,
    "live_min_free_disk_gb":  10,
    "live_abort_on_session_fail": True,
    # Whitelist of platforms where live recording is permitted.
    # TikTok/Instagram excluded by design — both have hostile anti-scraping
    # and unreliable extractors; cookies failing mid-stream is the norm.
    "live_platforms":         ["youtube", "twitch", "kick"],
}

logger = logging.getLogger(__name__)


def _load() -> dict:
    cfg = dict(_DEFAULTS)
    path = Path(_CONFIG_FILE)
    if not path.exists():
        logger.warning("Config file not found at %s — using defaults", path)
        return cfg
    try:
        with open(path) as f:
            overrides: dict = json.load(f)
        unknown = set(overrides) - set(_DEFAULTS)
        if unknown:
            logger.warning("Unknown config keys (ignored): %s", ", ".join(sorted(unknown)))
        cfg.update({k: v for k, v in overrides.items() if k in _DEFAULTS})
        logger.info("Config loaded from %s", path)
    except Exception as e:
        logger.error("Failed to load config %s: %s — using defaults", path, e)
    return cfg


_cfg = _load()

DELETE_AFTER_SEND: bool      = bool(_cfg["delete_after_send"])
DEFAULT_QUALITY:   str       = str(_cfg["default_quality"])
MAX_CONCURRENT:    int       = int(_cfg["max_concurrent_downloads"])
TEMP_TTL_HOURS:    int       = int(_cfg["temp_ttl_hours"])
OWNER_CHAT_ID:     int | None = int(_cfg["owner_chat_id"]) if _cfg["owner_chat_id"] is not None else None
ALLOWED_CHAT_IDS:  set[int]  = {int(x) for x in _cfg["allowed_chat_ids"]}

# Livestream recording (v2)
LIVE_ENABLED:               bool      = bool(_cfg["live_enabled"])
LIVE_MAX_CONCURRENT:        int       = int(_cfg["live_max_concurrent"])
LIVE_HEARTBEAT_SECONDS:     int       = int(_cfg["live_heartbeat_seconds"])
LIVE_MIN_FREE_DISK_GB:      int       = int(_cfg["live_min_free_disk_gb"])
LIVE_ABORT_ON_SESSION_FAIL: bool      = bool(_cfg["live_abort_on_session_fail"])
LIVE_PLATFORMS:             set[str]  = {str(p).lower() for p in _cfg["live_platforms"]}
