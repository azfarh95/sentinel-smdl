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
    "temp_ttl_hours":         12,   # non-owner downloads land in TEMP_DIR → reaped after 12h
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
    # Cap on live recording resolution (height in pixels). Trades file size
    # for quality. Twitch 1080p60 source = ~7.5 Mbps = ~56 MB/min, which
    # adds up quickly on long recordings. 720p60 ≈ ~3.5 Mbps = ~25 MB/min.
    # 480p ≈ ~1.5 Mbps = ~12 MB/min. Set to 0 for unlimited (== source).
    "live_max_height":        720,
    # Optional post-recording transcode. Captures at live_max_height (full
    # quality), then re-encodes to a smaller height before delivery.
    # Useful when you want a quality archive but smaller files for Telegram
    # delivery. 0 = off (no transcode). Common values: 480, 240.
    # Cost: CPU time on transcode (~1-2x realtime on libx264 veryfast).
    "live_transcode_height":  0,
    # If True, keep the original at full quality AND produce a transcoded
    # sibling for delivery (archive + reduced). If False, transcode replaces
    # the original (saves disk, archive is the smaller version).
    "live_transcode_keep_original": False,
    # Stream monitor (V1) — polls a watchlist of channel/streamer URLs and
    # DMs the user when one goes live, with Yes/No inline buttons to start
    # recording. Watchlist is owned/managed via /watch /unwatch /watchlist.
    "monitor_enabled":              True,
    "monitor_poll_interval_seconds": 300,   # 5 min — respects rate-limits, low overhead
    "monitor_probe_timeout_seconds": 30,    # max wait per yt-dlp probe before treating as offline
    # OneDrive (Phase 2, wired). Admin tab toggles these without code edits.
    "onedrive_mode":                "on_demand",  # 'on_demand' | 'auto_after_send' | 'disabled'
    "onedrive_folder":              "/SMDL",
    "onedrive_delete_after_upload": False,
    # i18n + tz — read elsewhere via JSON; declared here to suppress the
    # 'Unknown config keys (ignored)' warning at startup.
    "language":                     "en",
    "timezone":                     "Asia/Singapore",
    # Profile scraper (V1) — owner-only IG/TikTok profile auto-monitor with
    # behavioural mimicry. The defaults below assume one IG + one TikTok
    # cookie. Knobs are documented in smdl_scraper_design.md §3 + addendum.
    "scraper_enabled":              True,
    # Active hours — probes outside this window are deferred. Hours are in
    # the timezone given by `scraper_timezone` (defaults to Asia/Singapore;
    # falls back to UTC if zoneinfo lookup fails).
    "scraper_timezone":             "Asia/Singapore",
    "scraper_human_hours_start":    "08:00",
    "scraper_human_hours_end":      "23:00",
    # Burst-session model — 2-4 wake-ups per day, each pulling 2-5 profiles.
    "scraper_daily_sessions":       3,
    # Per-platform default cadence in HOURS. Each profile uses this if its
    # own interval isn't overridden. Minimum-clamped to avoid IG/TT alarm.
    "scraper_default_interval_hours_instagram": 6,
    "scraper_default_interval_hours_tiktok":    4,
    "scraper_min_interval_hours":   2,
    "scraper_max_interval_hours":   48,
    # Within a session — sequential probes with random gap (seconds).
    "scraper_inter_probe_gap_min": 30,
    "scraper_inter_probe_gap_max": 180,
    # Per-probe playlist depth (how many recent posts yt-dlp pulls).
    "scraper_playlist_end": 5,
    # Detection mitigation knobs.
    "scraper_warmup_days":           7,
    "scraper_ceiling_ig_per_day":   30,
    "scraper_ceiling_tt_per_day":   50,
    "scraper_cooldown_hours_after_block": 24,
    # Cold-session FYP browse (tactic 11) — adds ~60s of innocuous HTTP at
    # the start of each session to look like "user opened the app first."
    "scraper_cold_browse_enabled":  True,
    # After this many consecutive failures on a profile, auto-disable.
    "scraper_disable_after_failures": 5,
    # Storage paths (under DOWNLOADS_DIR).
    "scraper_subdir":               "scraper",
    # Notification verbosity.
    "scraper_notify_per_post":      True,
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
LIVE_MAX_HEIGHT:            int       = int(_cfg["live_max_height"])
LIVE_TRANSCODE_HEIGHT:      int       = int(_cfg["live_transcode_height"])
LIVE_TRANSCODE_KEEP_ORIGINAL: bool    = bool(_cfg["live_transcode_keep_original"])

# OneDrive integration (Phase 2 — wired up). Lives in smdl.json so the Admin
# tab can toggle without code edits. _cfg_get() in miniapp reads file first.
ONEDRIVE_MODE:                 str  = str(_cfg.get("onedrive_mode") or "on_demand")
ONEDRIVE_FOLDER:               str  = str(_cfg.get("onedrive_folder") or "/SMDL")
ONEDRIVE_DELETE_AFTER_UPLOAD:  bool = bool(_cfg.get("onedrive_delete_after_upload") or False)

# Stream monitor (V1)
MONITOR_ENABLED:                bool = bool(_cfg["monitor_enabled"])
MONITOR_POLL_INTERVAL_SECONDS:  int  = int(_cfg["monitor_poll_interval_seconds"])
MONITOR_PROBE_TIMEOUT_SECONDS:  int  = int(_cfg["monitor_probe_timeout_seconds"])

# Profile scraper (V1)
SCRAPER_ENABLED:                bool = bool(_cfg["scraper_enabled"])
SCRAPER_TIMEZONE:               str  = str(_cfg["scraper_timezone"])
SCRAPER_HUMAN_HOURS_START:      str  = str(_cfg["scraper_human_hours_start"])
SCRAPER_HUMAN_HOURS_END:        str  = str(_cfg["scraper_human_hours_end"])
SCRAPER_DAILY_SESSIONS:         int  = int(_cfg["scraper_daily_sessions"])
SCRAPER_DEFAULT_INTERVAL_HOURS: dict = {
    "instagram": int(_cfg["scraper_default_interval_hours_instagram"]),
    "tiktok":    int(_cfg["scraper_default_interval_hours_tiktok"]),
}
SCRAPER_MIN_INTERVAL_HOURS:     int  = int(_cfg["scraper_min_interval_hours"])
SCRAPER_MAX_INTERVAL_HOURS:     int  = int(_cfg["scraper_max_interval_hours"])
SCRAPER_INTER_PROBE_GAP_MIN:    int  = int(_cfg["scraper_inter_probe_gap_min"])
SCRAPER_INTER_PROBE_GAP_MAX:    int  = int(_cfg["scraper_inter_probe_gap_max"])
SCRAPER_PLAYLIST_END:           int  = int(_cfg["scraper_playlist_end"])
SCRAPER_WARMUP_DAYS:            int  = int(_cfg["scraper_warmup_days"])
SCRAPER_CEILING_IG_PER_DAY:     int  = int(_cfg["scraper_ceiling_ig_per_day"])
SCRAPER_CEILING_TT_PER_DAY:     int  = int(_cfg["scraper_ceiling_tt_per_day"])
SCRAPER_COOLDOWN_HOURS_AFTER_BLOCK: int = int(_cfg["scraper_cooldown_hours_after_block"])
SCRAPER_COLD_BROWSE_ENABLED:    bool = bool(_cfg["scraper_cold_browse_enabled"])
SCRAPER_DISABLE_AFTER_FAILURES: int  = int(_cfg["scraper_disable_after_failures"])
SCRAPER_SUBDIR:                 str  = str(_cfg["scraper_subdir"])
SCRAPER_NOTIFY_PER_POST:        bool = bool(_cfg["scraper_notify_per_post"])
