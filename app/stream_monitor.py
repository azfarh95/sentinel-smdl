"""Stream monitor (V1) — polls a watchlist of channel/streamer URLs and
DMs the user when one goes live.

Design contract:
- One watchlist per bot (owner-only). V1 doesn't support multi-user lists.
- Poll cadence is conservative (5 min default). Each probe is yt-dlp
  extract_info(download=False) — costs ~1-3s and a small HTTP request,
  no scraping HTML directly.
- State is OFFLINE / LIVE per entry. On OFFLINE → LIVE transition, send
  a Telegram DM with inline keyboard "Yes — record" / "No — skip".
- LIVE → OFFLINE just resets state silently (no "stream ended" spam).
- Watchlist file at /data/watchlist.json — JSON list of {url, label,
  added_by, added_at}. Survives container restart. Hand-editable.
- Probes that error out (timeout, rate-limit, network) are logged but
  treated as 'still offline'. We don't notify on errors — too noisy.

V2 ideas (not built):
- Multi-user lists (per-chat-id watchlists)
- Persistent state across restart (currently re-detects "live" on restart
  and re-prompts — V2 could remember the prompt was already sent)
- Adaptive poll cadence (faster when streamer is "usually live around now")
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path
from typing import Any

import yt_dlp
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application

from .config import (
    MONITOR_ENABLED,
    MONITOR_POLL_INTERVAL_SECONDS,
    MONITOR_PROBE_TIMEOUT_SECONDS,
    OWNER_CHAT_ID,
)
from .downloader import _resolve_cookies
from .i18n import get_lang, t

logger = logging.getLogger(__name__)

WATCHLIST_FILE = Path(os.environ.get("WATCHLIST_FILE", "/data/watchlist.json"))


def _load_watchlist() -> list[dict[str, Any]]:
    if not WATCHLIST_FILE.exists():
        return []
    try:
        with open(WATCHLIST_FILE) as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception as e:
        logger.error("Failed to read watchlist %s: %s", WATCHLIST_FILE, e)
        return []


def _save_watchlist(entries: list[dict[str, Any]]) -> None:
    WATCHLIST_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = WATCHLIST_FILE.with_suffix(".json.tmp")
    with open(tmp, "w") as f:
        json.dump(entries, f, indent=2)
    tmp.replace(WATCHLIST_FILE)


def add_to_watchlist(url: str, label: str | None = None, added_by: int | None = None) -> tuple[bool, str]:
    """Returns (added, message). Idempotent — duplicate URL returns (False, ...)."""
    entries = _load_watchlist()
    if any(e.get("url") == url for e in entries):
        return False, f"Already watching {url}"
    entries.append({
        "url":      url,
        "label":    label or url,
        "added_by": added_by,
        "added_at": int(time.time()),
    })
    _save_watchlist(entries)
    return True, f"Now watching {url}"


def remove_from_watchlist(url: str) -> tuple[bool, str]:
    entries = _load_watchlist()
    new = [e for e in entries if e.get("url") != url]
    if len(new) == len(entries):
        return False, f"Not in watchlist: {url}"
    _save_watchlist(new)
    return True, f"Removed {url}"


def list_watchlist() -> list[dict[str, Any]]:
    return _load_watchlist()


def snooze_streamer(url: str, minutes: int) -> int:
    """Set snoozed_until on a watchlist entry to now + minutes. Returns the
    epoch seconds at which the snooze expires (0 if URL not in watchlist)."""
    entries = _load_watchlist()
    expires_at = int(time.time() + minutes * 60)
    found = False
    for e in entries:
        if e.get("url") == url:
            e["snoozed_until"] = expires_at
            found = True
            break
    if found:
        _save_watchlist(entries)
        return expires_at
    return 0


def is_snoozed(entry: dict[str, Any]) -> bool:
    snoozed_until = int(entry.get("snoozed_until") or 0)
    return snoozed_until > time.time()


def _probe_is_live(url: str) -> dict[str, Any]:
    """Synchronous yt-dlp probe. Returns {is_live, title, uploader, error}.

    Errors are NOT raised — they're returned in the dict so the caller can
    decide policy (typically: log + treat as offline).
    """
    cookiepath = _resolve_cookies(url)
    opts: dict = {"quiet": True, "no_warnings": True, "socket_timeout": MONITOR_PROBE_TIMEOUT_SECONDS}
    if cookiepath:
        opts["cookiefile"] = cookiepath
    # Cloudflare-protected sites need Chrome TLS impersonation (HTTP 406 otherwise).
    from .live_downloader import _add_impersonate_if_needed
    from . import stripchat_patch  # noqa: F401 — applies extractor patch on import
    _add_impersonate_if_needed(opts, url)
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except yt_dlp.utils.DownloadError as e:
        return {"is_live": False, "error": str(e)[:200]}
    except Exception as e:
        return {"is_live": False, "error": str(e)[:200]}
    if not info:
        return {"is_live": False, "error": "no info"}
    is_live = bool(info.get("is_live")) or (info.get("live_status") or "").lower() in ("is_live",)
    return {
        "is_live":  is_live,
        "title":    info.get("title"),
        "uploader": info.get("uploader") or info.get("channel"),
        "error":    None,
    }


# In-memory state of last-seen status per URL. Keys are URLs; values are
# 'live' or 'offline'. Resets on container restart (intentional V1 trade-off).
_last_status: dict[str, str] = {}


async def _poll_once(app: Application, entries: list[dict[str, Any]]) -> None:
    """Probe every watchlist entry once, dispatch transitions to OWNER_CHAT_ID."""
    if not entries:
        return
    if OWNER_CHAT_ID is None:
        logger.warning("monitor: OWNER_CHAT_ID not set — skipping prompts")
        return

    loop = asyncio.get_running_loop()
    for entry in entries:
        url = entry.get("url")
        label = entry.get("label") or url
        if not url:
            continue
        try:
            result = await loop.run_in_executor(None, _probe_is_live, url)
        except Exception as e:
            logger.warning("monitor: probe %s crashed: %s", url, e)
            continue

        if result.get("error"):
            logger.debug("monitor: %s probe error (treating as offline): %s", label, result["error"])
            _last_status[url] = "offline"
            continue

        is_live = result["is_live"]
        prev = _last_status.get(url)
        new = "live" if is_live else "offline"
        _last_status[url] = new

        # Snooze check: if user explicitly snoozed this streamer, skip the
        # prompt regardless of state transition. We still update _last_status
        # above so that when snooze expires we don't immediately re-prompt
        # for a streamer who's been live the whole time.
        if is_snoozed(entry):
            if prev != "live" and is_live:
                until = int(entry.get("snoozed_until") or 0)
                logger.info(
                    "monitor: %s went LIVE but is snoozed until %s — skipping prompt",
                    label, until,
                )
            continue

        if prev != "live" and is_live:
            # OFFLINE → LIVE transition. Notify owner with inline keyboard.
            uploader = result.get("uploader") or label
            title = (result.get("title") or "")[:120]
            owner_lang = get_lang(OWNER_CHAT_ID)
            text = t(
                "monitor_live_prompt", owner_lang,
                uploader=uploader, title=title, url=url,
            )
            keyboard = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(t("btn_yes_record", owner_lang), callback_data=f"mon:rec:{url}"),
                    InlineKeyboardButton(t("btn_skip", owner_lang),       callback_data=f"mon:skip:{url}"),
                ],
                [
                    InlineKeyboardButton(t("btn_snooze_1h", owner_lang), callback_data=f"mon:snooze1h:{url}"),
                    InlineKeyboardButton(t("btn_snooze_8h", owner_lang), callback_data=f"mon:snooze8h:{url}"),
                ],
            ])
            try:
                await app.bot.send_message(
                    chat_id=OWNER_CHAT_ID,
                    text=text,
                    reply_markup=keyboard,
                    disable_web_page_preview=True,
                )
                logger.info("monitor: %s went LIVE — prompt sent", label)
            except Exception as e:
                logger.error("monitor: failed to send live notification for %s: %s", label, e)
        elif prev == "live" and not is_live:
            logger.info("monitor: %s went OFFLINE", label)


async def monitor_loop(app: Application) -> None:
    """Forever loop. Sleeps between polls. Cancellable."""
    if not MONITOR_ENABLED:
        logger.info("monitor: disabled in config")
        return
    logger.info(
        "monitor: started (interval=%ds, watchlist=%s)",
        MONITOR_POLL_INTERVAL_SECONDS, WATCHLIST_FILE,
    )
    try:
        while True:
            entries = _load_watchlist()
            if entries:
                logger.debug("monitor: polling %d entries", len(entries))
                await _poll_once(app, entries)
            await asyncio.sleep(MONITOR_POLL_INTERVAL_SECONDS)
    except asyncio.CancelledError:
        logger.info("monitor: cancelled")
        raise
    except Exception as e:
        logger.exception("monitor: loop crashed: %s", e)
        raise
