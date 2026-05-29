"""Profile scraper (V1) — owner-only IG/TikTok profile auto-monitor.

Sibling to stream_monitor.py. Different responsibility: stream_monitor
polls for live transitions; this module polls a list of profile URLs and
auto-downloads new posts as they appear.

Design contract (see smdl_scraper_design.md + addendum):

 1. Owner-only. Non-owner has no UI surface, no API endpoints.
 2. Burst-session scheduler. 2-4 sessions/day, each session pulls 2-5
    profiles back-to-back with random 30-180s gaps. NOT a uniform cadence.
 3. Active hours only — defaults 08:00-23:00 local (Asia/Singapore).
    Probes outside the window are deferred to next morning.
 4. Sequential. One yt-dlp probe at a time. Never parallel — IG flags
    concurrent requests from the same IP immediately.
 5. Cookies live in /cookies/<platform>.txt (same mount as downloader.py).
    No cookies → that platform is skipped (logged once per session).
 6. New posts trigger the existing download pipeline: yt-dlp/gallery-dl
    → send_files → OneDrive auto-mirror (if mode==auto_after_send) →
    optional local delete. Zero new code in that path.
 7. Failure-loud. 401/403/429 cluster on a cookie → owner alert + 24h
    cookie cooldown. 5 consecutive failures on a single profile →
    auto-disable + owner alert.
 8. First-probe baseline. On a profile's first-ever successful probe we
    record the post IDs but DON'T download — otherwise adding a profile
    dumps the last N posts in one go (rate-limit + storage burst).

Lifecycle: started from main.py lifespan as a background task. Cancelled
on shutdown. Catastrophic exceptions logged + retried after a 60s sleep
so a bad probe never kills the loop.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import re
import subprocess
import time
from datetime import datetime, time as dtime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yt_dlp
from telegram.ext import Application

from . import database as db
from .config import (
    OWNER_CHAT_ID,
    SCRAPER_CEILING_IG_PER_DAY,
    SCRAPER_CEILING_TT_PER_DAY,
    SCRAPER_COLD_BROWSE_ENABLED,
    SCRAPER_COOLDOWN_HOURS_AFTER_BLOCK,
    SCRAPER_DAILY_SESSIONS,
    SCRAPER_DEFAULT_INTERVAL_HOURS,
    SCRAPER_DISABLE_AFTER_FAILURES,
    SCRAPER_ENABLED,
    SCRAPER_HUMAN_HOURS_END,
    SCRAPER_HUMAN_HOURS_START,
    SCRAPER_INTER_PROBE_GAP_MAX,
    SCRAPER_INTER_PROBE_GAP_MIN,
    SCRAPER_MAX_INTERVAL_HOURS,
    SCRAPER_MIN_INTERVAL_HOURS,
    SCRAPER_NOTIFY_PER_POST,
    SCRAPER_PLAYLIST_END,
    SCRAPER_SUBDIR,
    SCRAPER_TIMEZONE,
    SCRAPER_WARMUP_DAYS,
)
from .downloader import _resolve_cookies, send_files, COOKIES_DIR

logger = logging.getLogger(__name__)


# ── Constants ────────────────────────────────────────────────────────────────

# How often the loop wakes to check "are we due for a session yet?".
# 60s is fine — the session schedule has minute-level granularity at best.
_LOOP_TICK_SECONDS = 60

# yt-dlp probe timeout per profile.
_PROBE_TIMEOUT_SECONDS = 45

# Mobile-realistic UAs. Picked once per cookie at first sight + pinned.
# Recent Chrome on Android (preferred — TikTok is most lenient with Android),
# plus an iPhone Safari fallback. Update list yearly.
_MOBILE_UAS = [
    "Mozilla/5.0 (Linux; Android 14; SM-S921B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Mobile Safari/537.36",
    "Mozilla/5.0 (Linux; Android 13; SM-A536E) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.6778.100 Mobile Safari/537.36",
    "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Mobile Safari/537.36",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.6 Mobile/15E148 Safari/604.1",
]

# Cookie-key resolution. Maps domain → file basename (without .txt).
# Mirrors downloader.py's _SITE_COOKIE_MAP but exposed for scraper-level
# bookkeeping (we key per cookie, not per profile).
_COOKIE_KEY_BY_HOST = {
    "instagram.com": "instagram",
    "tiktok.com":    "tiktok",
}

DOWNLOADS_DIR = os.environ.get("DOWNLOADS_DIR", "/downloads")


# ── Timezone + active hours ──────────────────────────────────────────────────

def _local_now() -> datetime:
    """Return now() in the scraper's configured timezone. Falls back to UTC
    if zoneinfo lookup fails (e.g. tzdata missing on minimal Alpine bases)."""
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo(SCRAPER_TIMEZONE))
    except Exception:
        return datetime.now(timezone.utc)


def _parse_hhmm(s: str) -> dtime:
    h, m = (s.strip() or "00:00").split(":", 1)
    return dtime(int(h), int(m))


def _in_human_hours(now_local: datetime) -> bool:
    start = _parse_hhmm(SCRAPER_HUMAN_HOURS_START)
    end = _parse_hhmm(SCRAPER_HUMAN_HOURS_END)
    t = now_local.time()
    if start <= end:
        return start <= t <= end
    # Window crosses midnight (e.g. 22:00-06:00) — uncommon for our use case,
    # but handled defensively.
    return t >= start or t <= end


def _next_morning_iso(now_local: datetime) -> str:
    """Return an ISO timestamp at the *next* start-of-active-window with a
    small random jitter, expressed in UTC. If today's window hasn't opened
    yet (e.g. it's 02:00 local and start_hour is 08:00), target today;
    otherwise target tomorrow."""
    start = _parse_hhmm(SCRAPER_HUMAN_HOURS_START)
    today_start = now_local.replace(hour=start.hour, minute=start.minute,
                                     second=0, microsecond=0)
    base = today_start if now_local < today_start else today_start + timedelta(days=1)
    target = base + timedelta(seconds=random.randint(0, 1800))
    return target.astimezone(timezone.utc).isoformat()


# ── Cookie-key + UA resolution ───────────────────────────────────────────────

def _cookie_key_from_url(url: str) -> str | None:
    host = (urlparse(url).hostname or "").lower().lstrip(".")
    if host.startswith("www."):
        host = host[4:]
    for needle, key in _COOKIE_KEY_BY_HOST.items():
        if needle in host:
            return key
    return None


def _cookie_path(cookie_key: str) -> str | None:
    p = Path(COOKIES_DIR) / f"{cookie_key}.txt"
    return str(p) if p.exists() else None


def _pick_ua_for_cookie() -> str:
    return random.choice(_MOBILE_UAS)


# ── HTTP error → code ────────────────────────────────────────────────────────

_HTTP_CODE_RE = re.compile(r"HTTP Error (\d{3})|status[:\s]+(\d{3})", re.IGNORECASE)
_HTTP_CODE_BARE_RE = re.compile(r"\b(401|403|404|429)\b")


def _parse_http_code(err: str | None) -> int | None:
    if not err:
        return None
    m = _HTTP_CODE_RE.search(err)
    if m:
        return int(m.group(1) or m.group(2))
    m = _HTTP_CODE_BARE_RE.search(err)
    if m:
        return int(m.group(1))
    return None


# ── Session scheduling ───────────────────────────────────────────────────────

_NEXT_SESSION_AT_KEY = "scraper_next_session_at"
_PAUSED_KEY = "scraper_paused_runtime"  # Admin tab toggle, distinct from config flag


async def is_runtime_paused() -> bool:
    """Owner-controlled pause flag from the Admin tab. Survives restart via
    the settings table. Distinct from SCRAPER_ENABLED (the install-time flag)."""
    return (await db.get_setting(_PAUSED_KEY, "false")).lower() == "true"


async def set_runtime_paused(paused: bool) -> None:
    await db.set_setting(_PAUSED_KEY, "true" if paused else "false")


async def _get_next_session_at() -> datetime | None:
    raw = await db.get_setting(_NEXT_SESSION_AT_KEY, "")
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except Exception:
        return None


async def _set_next_session_at(when: datetime) -> None:
    await db.set_setting(_NEXT_SESSION_AT_KEY,
                          when.astimezone(timezone.utc).isoformat())


def _seconds_between_sessions() -> tuple[int, int]:
    """Active window divided into N sessions gives the mean inter-session gap.
    Add ±25% jitter so consecutive days don't land at the same minute."""
    start = _parse_hhmm(SCRAPER_HUMAN_HOURS_START)
    end = _parse_hhmm(SCRAPER_HUMAN_HOURS_END)
    span_minutes = (end.hour * 60 + end.minute) - (start.hour * 60 + start.minute)
    if span_minutes <= 0:
        span_minutes = 15 * 60  # safe fallback
    n = max(1, int(SCRAPER_DAILY_SESSIONS))
    mean = (span_minutes * 60) // n
    low = max(60, int(mean * 0.75))
    high = max(low + 1, int(mean * 1.25))
    return low, high


async def _schedule_next_session_from(now_local: datetime) -> datetime:
    """Compute when the next burst session should fire. If the next slot would
    land outside human hours, push to tomorrow morning."""
    low, high = _seconds_between_sessions()
    delta = random.randint(low, high)
    candidate = now_local + timedelta(seconds=delta)
    if not _in_human_hours(candidate):
        target_iso = _next_morning_iso(now_local)
        candidate = datetime.fromisoformat(target_iso).astimezone(now_local.tzinfo or timezone.utc)
    await _set_next_session_at(candidate)
    return candidate


# ── Cold browse (tactic 11) + mini-warmup (tactic 8) ─────────────────────────

async def _cold_browse(cookie_key: str, user_agent: str) -> None:
    """Make a few innocuous HTTP requests with the cookie to look like 'user
    opened the app and scrolled the feed before navigating to a profile'.

    Uses stdlib http.cookiejar + urllib so we don't add a new dep. All
    failures are swallowed — this is best-effort behavioural cover, not
    load-bearing for the probe itself."""
    cookiepath = _cookie_path(cookie_key)
    if not cookiepath:
        return
    if cookie_key == "instagram":
        urls = [
            "https://www.instagram.com/",
            "https://www.instagram.com/accounts/edit/",
        ]
        dwells = [(8, 14), (4, 8)]
    elif cookie_key == "tiktok":
        urls = [
            "https://www.tiktok.com/",
            "https://www.tiktok.com/foryou",
        ]
        dwells = [(10, 18), (5, 9)]
    else:
        return
    await _http_browse(cookiepath, user_agent, urls, dwells)


async def _mini_warmup(cookie_key: str, user_agent: str) -> None:
    cookiepath = _cookie_path(cookie_key)
    if not cookiepath:
        return
    host = "instagram.com" if cookie_key == "instagram" else "tiktok.com"
    await _http_browse(cookiepath, user_agent,
                        [f"https://www.{host}/"],
                        [(2, 5)])


def _http_browse_sync(cookiepath: str, ua: str,
                       urls: list[str], dwells: list[tuple[int, int]]) -> None:
    """Synchronous helper, run in executor. Loads cookies once, fetches each
    URL with the pinned UA, waits the dwell interval, moves on."""
    try:
        from http.cookiejar import MozillaCookieJar
        import urllib.request
        cj = MozillaCookieJar(cookiepath)
        try:
            cj.load(ignore_discard=True, ignore_expires=True)
        except Exception as e:
            logger.debug("scraper: cookie load failed for %s: %s", cookiepath, e)
            return
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
        opener.addheaders = [
            ("User-Agent", ua),
            ("Accept", "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"),
            ("Accept-Language", "en-US,en;q=0.9"),
        ]
        for url, dwell in zip(urls, dwells):
            try:
                resp = opener.open(url, timeout=15)
                # Drain a bounded chunk so the request fully completes —
                # body is irrelevant, we just want the GET on the wire.
                resp.read(4096)
                resp.close()
            except Exception as e:
                logger.debug("scraper: browse GET %s failed: %s", url, e)
            time.sleep(random.uniform(dwell[0], dwell[1]))
    except Exception as e:
        logger.debug("scraper: _http_browse_sync crashed: %s", e)


async def _http_browse(cookiepath: str, ua: str,
                        urls: list[str], dwells: list[tuple[int, int]]) -> None:
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, _http_browse_sync, cookiepath, ua, urls, dwells)


# ── Probe execution ──────────────────────────────────────────────────────────
#
# Instagram and TikTok use different probe paths because yt-dlp's
# `instagram:user` extractor is unreliable (it scrapes the HTML profile page
# and the JSON structure changes weekly). gallery-dl is the de-facto right
# tool for IG profile listings and works against the /posts/ subpath.
# TikTok stays on yt-dlp where `tiktok:user` works reliably.


def _probe_instagram_sync(url: str, cookiepath: str | None,
                            user_agent: str | None,
                            playlist_end: int
                            ) -> tuple[list[dict], str | None]:
    """Use gallery-dl to enumerate the last N posts from an IG profile.
    Returns ([{id, url, title}, ...], err)."""
    # gallery-dl wants the /posts/ subpath to enumerate the user's posts feed.
    probe_url = url.rstrip("/")
    if not probe_url.endswith("/posts"):
        probe_url = probe_url + "/posts/"
    cmd = ["gallery-dl", "-j", "--range", f"1-{int(playlist_end)}"]
    if cookiepath:
        cmd += ["--cookies", cookiepath]
    if user_agent:
        cmd += ["--user-agent", user_agent]
    cmd.append(probe_url)
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                               timeout=_PROBE_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        return [], "gallery-dl timeout"
    except Exception as e:
        return [], f"gallery-dl crashed: {e!s:.300}"
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "non-zero exit").strip()
        return [], err[:500]
    try:
        data = json.loads(proc.stdout or "[]")
    except Exception as e:
        return [], f"gallery-dl JSON parse: {e!s:.200}"
    # gallery-dl emits a list of [type_code, payload] tuples. Type 2 entries
    # carry post metadata; other type codes are media URLs / navigation refs.
    posts: list[dict] = []
    seen: set[str] = set()
    for entry in data:
        if not (isinstance(entry, list) and len(entry) >= 2
                and isinstance(entry[1], dict)):
            continue
        meta = entry[1]
        shortcode = meta.get("post_shortcode") or meta.get("post_id")
        if not shortcode:
            continue
        sid = str(shortcode)
        if sid in seen:  # carousels emit multiple type-2 frames per post
            continue
        seen.add(sid)
        posts.append({
            "id":    sid,
            "url":   meta.get("post_url") or f"https://www.instagram.com/p/{sid}/",
            "title": (meta.get("description") or "")[:200],
        })
    return posts, None


def _probe_tiktok_sync(url: str, cookiepath: str | None,
                        user_agent: str | None,
                        playlist_end: int
                        ) -> tuple[list[dict], str | None]:
    """Use yt-dlp to enumerate the last N TikToks from a profile."""
    opts: dict = {
        "quiet":          True,
        "no_warnings":    True,
        "extract_flat":   "in_playlist",
        "playlistend":    playlist_end,
        "socket_timeout": _PROBE_TIMEOUT_SECONDS,
    }
    if cookiepath:
        opts["cookiefile"] = cookiepath
    if user_agent:
        opts["http_headers"] = {"User-Agent": user_agent}
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except yt_dlp.utils.DownloadError as e:
        return [], str(e)[:500]
    except Exception as e:
        return [], str(e)[:500]
    posts: list[dict] = []
    for e in (info.get("entries") or []) if info else []:
        if not e:
            continue
        pid = e.get("id") or e.get("display_id")
        if not pid:
            continue
        posts.append({
            "id":    str(pid),
            "url":   e.get("url") or e.get("webpage_url") or "",
            "title": e.get("title") or "",
        })
    return posts, None


async def _probe_profile(profile: dict, cookie_key: str | None,
                          user_agent: str | None
                          ) -> tuple[list[dict], str | None]:
    """Returns (posts, err). On success err is None; on failure posts is []."""
    cookiepath = _cookie_path(cookie_key) if cookie_key else None
    loop = asyncio.get_running_loop()
    platform = (profile.get("platform") or "").lower()
    if platform == "instagram":
        return await loop.run_in_executor(
            None, _probe_instagram_sync,
            profile["url"], cookiepath, user_agent, SCRAPER_PLAYLIST_END,
        )
    return await loop.run_in_executor(
        None, _probe_tiktok_sync,
        profile["url"], cookiepath, user_agent, SCRAPER_PLAYLIST_END,
    )


# ── Download dispatch ────────────────────────────────────────────────────────

async def _dispatch_new_post(app: Application, profile: dict, post: dict) -> bool:
    """Run the post through the existing download pipeline + deliver +
    record + (optional) OneDrive mirror. Returns True on success."""
    if OWNER_CHAT_ID is None:
        logger.warning("scraper: OWNER_CHAT_ID unset — cannot dispatch %s", post.get("id"))
        return False
    post_url = post.get("url") or ""
    if not post_url:
        logger.warning("scraper: post %s missing url — skipping", post.get("id"))
        return False
    # Quality preference: respect the owner's configured default. The
    # downloader will fall back to bestvideo+bestaudio at 1080p.
    from .downloader import download as _download
    try:
        result = await _download(post_url, is_owner=True)
    except Exception as e:
        logger.exception("scraper: download crashed for %s: %s", post_url, e)
        return False
    if result.get("error"):
        logger.warning("scraper: download failed for %s: %s", post_url, result["error"])
        return False
    files: list[str] = result.get("files") or []
    if not files:
        return False
    title = (post.get("title") or "").strip() or None
    caption = None
    if SCRAPER_NOTIFY_PER_POST:
        platform_label = profile.get("platform") or "Profile"
        uname = profile.get("username") or "?"
        caption = f"📥 {platform_label} · @{uname}"
        if title:
            caption += f"\n{title[:200]}"
    try:
        send_result = await send_files(app.bot, OWNER_CHAT_ID, files, caption=caption)
    except Exception as e:
        logger.exception("scraper: send_files crashed: %s", e)
        send_result = {"error": str(e)[:200]}
    if not send_result.get("ok"):
        err = send_result.get("error") or "unknown"
        logger.warning("scraper: send failed for %s (%s)", post_url, err)
    # Per-user download history — scraper attributes to owner.
    try:
        from pathlib import Path as _P
        uploader = _P(files[0]).parent.name if files else None
        await db.record_download(OWNER_CHAT_ID, post_url, files,
                                  profile.get("platform"), uploader)
    except Exception as e:
        logger.warning("scraper: record_download failed: %s", e)
    # OneDrive mirror — same conditional as bot.py.
    try:
        from .miniapp import _cfg_get as _od_cfg_get
        if (_od_cfg_get("onedrive_mode") or "on_demand").lower() == "auto_after_send":
            from . import onedrive as _od
            folder = _od_cfg_get("onedrive_folder") or "/SMDL"
            delete_after = bool(_od_cfg_get("onedrive_delete_after_upload"))
            async def _mirror():
                try:
                    summary = await _od.auto_upload_files(
                        files, profile.get("platform"),
                        _safe_uploader(files),
                        base_folder=folder,
                        delete_after_upload=delete_after,
                    )
                    if summary.get("sent_count"):
                        logger.info("scraper: OneDrive mirrored %d files",
                                     summary["sent_count"])
                except Exception as _e:
                    logger.warning("scraper: OneDrive mirror failed: %s", _e)
            asyncio.create_task(_mirror())
    except Exception as e:
        logger.warning("scraper: OneDrive mirror dispatch failed: %s", e)
    return bool(send_result.get("ok"))


def _safe_uploader(files: list[str]) -> str | None:
    try:
        return Path(files[0]).parent.name if files else None
    except Exception:
        return None


# ── Session execution ────────────────────────────────────────────────────────

async def _alert_owner(app: Application, text: str) -> None:
    if OWNER_CHAT_ID is None:
        logger.warning("scraper: would alert but OWNER_CHAT_ID unset: %s", text)
        return
    try:
        await app.bot.send_message(chat_id=OWNER_CHAT_ID, text=text,
                                    disable_web_page_preview=True)
    except Exception as e:
        logger.warning("scraper: alert send failed: %s", e)


async def _is_cookie_cooling(cookie_key: str) -> tuple[bool, datetime | None]:
    state = await db.cookie_get(cookie_key)
    if not state:
        return False, None
    cu = state.get("cooldown_until")
    if not cu:
        return False, None
    try:
        cd = datetime.fromisoformat(cu)
    except Exception:
        return False, None
    return (cd > datetime.now(timezone.utc)), cd


def _ceiling_for(cookie_key: str) -> int:
    if cookie_key == "instagram":
        return SCRAPER_CEILING_IG_PER_DAY
    if cookie_key == "tiktok":
        return SCRAPER_CEILING_TT_PER_DAY
    return 100


def _interval_for_platform(platform: str) -> int:
    p = (platform or "").lower()
    hours = SCRAPER_DEFAULT_INTERVAL_HOURS.get(p, 6)
    return max(SCRAPER_MIN_INTERVAL_HOURS,
               min(SCRAPER_MAX_INTERVAL_HOURS, int(hours)))


def _next_probe_at_for(profile: dict, *, success: bool, http_code: int | None
                        ) -> str:
    """Compute the next-probe timestamp with jitter + backoff."""
    base_hours = _interval_for_platform(profile.get("platform") or "")
    if not success:
        # Exponential backoff — double per failure, capped at max.
        fc = int(profile.get("failure_count") or 0) + 1
        base_hours = min(SCRAPER_MAX_INTERVAL_HOURS, base_hours * (2 ** min(fc, 4)))
        if http_code == 429:
            base_hours = SCRAPER_MAX_INTERVAL_HOURS
    # ±20% jitter
    jitter = base_hours * random.uniform(-0.2, 0.2)
    delta = timedelta(hours=base_hours + jitter)
    return (datetime.now(timezone.utc) + delta).isoformat()


def _in_warmup(cookie_state: dict | None) -> bool:
    if not cookie_state:
        return True
    fs = cookie_state.get("first_seen_at")
    if not fs:
        return True
    try:
        first = datetime.fromisoformat(fs)
    except Exception:
        return True
    age = datetime.now(timezone.utc) - first.astimezone(timezone.utc)
    return age < timedelta(days=SCRAPER_WARMUP_DAYS)


async def _run_session(app: Application) -> None:
    """One burst session — pick up to 5 due profiles, probe sequentially."""
    now_iso = datetime.now(timezone.utc).isoformat()
    today_str = _local_now().strftime("%Y-%m-%d")
    due = await db.scraper_due_profiles(now_iso, limit=20)
    if not due:
        logger.debug("scraper: session — no profiles due")
        return
    # Filter out skipped (5-10% per addendum tactic 4)
    skip_prob = 0.07
    due = [p for p in due if random.random() > skip_prob]
    if not due:
        logger.info("scraper: session — all due profiles rolled skip")
        return
    # Shuffle (tactic 3)
    random.shuffle(due)
    # Cap session size to 2-5 (tactic 2)
    cap = random.randint(2, 5)
    batch = due[:cap]
    logger.info("scraper: session start, %d profiles in batch", len(batch))

    cold_browsed: set[str] = set()
    for i, profile in enumerate(batch):
        cookie_key = _cookie_key_from_url(profile["url"])
        if not cookie_key:
            logger.warning("scraper: %s — no platform cookie key resolved", profile["url"])
            await db.scraper_update_probe_result(
                profile["url"],
                next_probe_at=_next_probe_at_for(profile, success=False, http_code=None),
                error="no_cookie_key", failure_increment=True,
            )
            continue
        cookiepath = _cookie_path(cookie_key)
        if not cookiepath:
            logger.info("scraper: %s — cookie file %s/%s.txt missing, skipping",
                         profile["url"], COOKIES_DIR, cookie_key)
            await db.scraper_update_probe_result(
                profile["url"],
                next_probe_at=_next_probe_at_for(profile, success=False, http_code=None),
                error=f"cookie_missing:{cookie_key}", failure_increment=False,
            )
            continue

        # Ensure cookie state exists + UA pinned.
        ua = _pick_ua_for_cookie()
        state = await db.cookie_ensure(cookie_key, ua)
        ua = state.get("user_agent") or ua

        # Cooldown check.
        cooling, cd_until = await _is_cookie_cooling(cookie_key)
        if cooling:
            logger.info("scraper: %s cooling until %s — deferring %s",
                         cookie_key, cd_until, profile["url"])
            await db.scraper_update_probe_result(
                profile["url"],
                next_probe_at=cd_until.isoformat() if cd_until else _next_probe_at_for(
                    profile, success=False, http_code=None),
                error="cookie_cooling",
            )
            continue

        # Daily ceiling (tactic 10) + warmup cap (tactic 6).
        # During warmup, reduce the per-day probe budget materially. Day 1-3
        # gets a hard cap of 3 probes/day on the cookie; day 4-7 caps at 8.
        # After warmup, full ceiling applies.
        ceiling = _ceiling_for(cookie_key)
        in_warmup = _in_warmup(state)
        warmup_cap = ceiling
        if in_warmup:
            try:
                age_days = (datetime.now(timezone.utc) -
                            datetime.fromisoformat(state["first_seen_at"])
                              .astimezone(timezone.utc)).days
            except Exception:
                age_days = 0
            warmup_cap = 3 if age_days < 3 else 8
        probes_today = await db.cookie_record_probe(cookie_key, today_str)
        if probes_today > ceiling:
            logger.info("scraper: %s daily ceiling (%d) hit — deferring",
                         cookie_key, ceiling)
            await db.scraper_update_probe_result(
                profile["url"],
                next_probe_at=_next_morning_iso(_local_now()),
                error="daily_ceiling",
            )
            continue
        if in_warmup and probes_today > warmup_cap:
            logger.info("scraper: %s in warmup (cap %d, today %d) — deferring",
                         cookie_key, warmup_cap, probes_today)
            await db.scraper_update_probe_result(
                profile["url"],
                next_probe_at=_next_morning_iso(_local_now()),
                error="warmup_cap",
            )
            continue

        # Cold browse for the first profile per cookie this session; mini
        # warm-up for subsequent ones.
        try:
            if cookie_key not in cold_browsed and SCRAPER_COLD_BROWSE_ENABLED:
                logger.debug("scraper: cold-browse %s", cookie_key)
                await _cold_browse(cookie_key, ua)
                cold_browsed.add(cookie_key)
            else:
                await _mini_warmup(cookie_key, ua)
        except Exception as e:
            logger.warning("scraper: browse warmup failed: %s", e)

        # Probe.
        posts, err = await _probe_profile(profile, cookie_key, ua)
        http_code = _parse_http_code(err)
        if err:
            logger.info("scraper: %s probe err code=%s: %s",
                         profile["url"], http_code, err[:120])
            if http_code in (401, 403, 429):
                cooldown_until = (datetime.now(timezone.utc)
                                   + timedelta(hours=SCRAPER_COOLDOWN_HOURS_AFTER_BLOCK))
                await db.cookie_mark_block(cookie_key, cooldown_until.isoformat())
                state = await db.cookie_get(cookie_key) or {}
                if int(state.get("consecutive_blocks") or 0) >= 2 and not state.get("alerted_at"):
                    await _alert_owner(app,
                        f"⚠ {cookie_key.capitalize()} cookies appear expired. "
                        f"{state.get('consecutive_blocks')} blocks in a row. "
                        f"Pausing {cookie_key} scraping for "
                        f"{SCRAPER_COOLDOWN_HOURS_AFTER_BLOCK}h. Re-export cookies "
                        f"when you can.")
                    await db.cookie_mark_alerted(cookie_key)
            await db.scraper_update_probe_result(
                profile["url"],
                next_probe_at=_next_probe_at_for(profile, success=False, http_code=http_code),
                http_code=http_code, error=err[:300], failure_increment=True,
            )
            # Auto-disable check.
            updated = await db.scraper_get_profile(profile["url"])
            if updated and int(updated.get("failure_count") or 0) >= SCRAPER_DISABLE_AFTER_FAILURES:
                await db.scraper_set_enabled(profile["url"], False)
                await _alert_owner(app,
                    f"🚫 Auto-disabled scraper for {profile['url']} after "
                    f"{updated['failure_count']} consecutive failures. "
                    f"Last error: {(updated.get('last_error') or '')[:150]}")
        else:
            # Success.
            await db.cookie_mark_success(cookie_key)
            await db.cookie_clear_alerted(cookie_key)
            seen_ids = set(profile.get("last_post_ids") or [])
            new_posts = [p for p in posts if p["id"] not in seen_ids]
            new_ids_full = [p["id"] for p in posts]
            new_downloads = 0
            if seen_ids:
                # Baseline already established — download new posts.
                for post in reversed(new_posts):  # oldest-first delivery
                    ok = await _dispatch_new_post(app, profile, post)
                    if ok:
                        new_downloads += 1
                    # Small gap between downloads of the same profile so we
                    # don't hammer the CDN.
                    if len(new_posts) > 1:
                        await asyncio.sleep(random.uniform(3, 8))
            else:
                # First sight — record baseline only, no downloads.
                logger.info("scraper: %s first sight, recording %d-post baseline",
                             profile["url"], len(posts))
            await db.scraper_update_probe_result(
                profile["url"],
                last_post_ids=new_ids_full or None,
                next_probe_at=_next_probe_at_for(profile, success=True, http_code=200),
                http_code=200, error=None, failure_reset=True,
                new_downloads=new_downloads,
            )

        # Inter-probe gap (tactic 8 baseline + cold-browse follow-up).
        if i < len(batch) - 1:
            gap = random.uniform(SCRAPER_INTER_PROBE_GAP_MIN,
                                  SCRAPER_INTER_PROBE_GAP_MAX)
            logger.debug("scraper: inter-probe gap %.1fs", gap)
            await asyncio.sleep(gap)

    logger.info("scraper: session complete")


# ── Main loop ────────────────────────────────────────────────────────────────

async def scraper_loop(app: Application) -> None:
    """Forever loop. Tick every minute, fire a session when due + in window."""
    if not SCRAPER_ENABLED:
        logger.info("scraper: disabled in config")
        return
    logger.info(
        "scraper: started (tz=%s, hours=%s-%s, sessions=%d/day, cookies_dir=%s)",
        SCRAPER_TIMEZONE, SCRAPER_HUMAN_HOURS_START, SCRAPER_HUMAN_HOURS_END,
        SCRAPER_DAILY_SESSIONS, COOKIES_DIR,
    )
    # On first boot, schedule a session a short jittered delay from now so
    # we don't fire instantly on container restart (which would defeat the
    # human-cadence model).
    if await _get_next_session_at() is None:
        first_when = _local_now() + timedelta(seconds=random.randint(120, 600))
        if not _in_human_hours(first_when):
            first_when = datetime.fromisoformat(_next_morning_iso(_local_now()))
        await _set_next_session_at(first_when)
        logger.info("scraper: first session scheduled at %s",
                     first_when.isoformat())

    try:
        while True:
            try:
                if await is_runtime_paused():
                    # Owner has paused the scraper from the Admin tab. Tick
                    # quietly — don't fire sessions, don't drift the schedule.
                    pass
                else:
                    now_local = _local_now()
                    if _in_human_hours(now_local):
                        next_at = await _get_next_session_at()
                        if next_at and now_local.astimezone(timezone.utc) >= next_at.astimezone(timezone.utc):
                            await _run_session(app)
                            await _schedule_next_session_from(_local_now())
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.exception("scraper: loop iteration crashed: %s", e)
            await asyncio.sleep(_LOOP_TICK_SECONDS)
    except asyncio.CancelledError:
        logger.info("scraper: cancelled")
        raise


# ── Public API used by bot slash commands ────────────────────────────────────

async def add_profile(url: str, added_by: int,
                       label: str | None = None) -> tuple[bool, str]:
    """Validate + add. Returns (ok, message).

    Normalises the URL to canonical form so yt-dlp's extractor doesn't
    inherit Instagram share-token (`?igsh=...`) or TikTok-share `?_t=...`
    query strings — those break extraction by being treated as part of
    the username."""
    url = (url or "").strip()
    if not url:
        return False, "URL required"
    if not re.match(r"^https?://", url, re.IGNORECASE):
        url = "https://" + url.lstrip("/")
    cookie_key = _cookie_key_from_url(url)
    if cookie_key not in ("instagram", "tiktok"):
        return False, "Only Instagram and TikTok profiles are supported in v1"
    # Username extract (the canonical handle, no query string).
    try:
        path = (urlparse(url).path or "").strip("/")
        first = (path.split("/") or [""])[0]
        username = first.lstrip("@") or None
    except Exception:
        username = None
    if not username:
        return False, "Could not extract username from URL"
    platform = "instagram" if cookie_key == "instagram" else "tiktok"
    # Rebuild URL in canonical form so yt-dlp sees just the username.
    if platform == "instagram":
        canonical_url = f"https://www.instagram.com/{username}/"
    else:
        canonical_url = f"https://www.tiktok.com/@{username}"
    label = label or f"@{username}"
    ok = await db.scraper_add_profile(canonical_url, platform, username, label, int(added_by))
    if not ok:
        return False, f"Already monitoring {canonical_url}"
    return True, f"Now monitoring {label} ({platform}). First baseline within next session."


async def remove_profile(url: str) -> tuple[bool, str]:
    url = (url or "").strip()
    if not re.match(r"^https?://", url, re.IGNORECASE):
        url = "https://" + url.lstrip("/")
    ok = await db.scraper_remove_profile(url)
    return ok, ("Removed" if ok else "Not in scrape list")


async def list_profiles() -> list[dict]:
    return await db.scraper_list_profiles()


async def pause_profile(url: str) -> tuple[bool, str]:
    if not re.match(r"^https?://", url or "", re.IGNORECASE):
        url = "https://" + (url or "").lstrip("/")
    ok = await db.scraper_set_enabled(url, False)
    return ok, ("Paused" if ok else "Not in scrape list")


async def resume_profile(url: str) -> tuple[bool, str]:
    if not re.match(r"^https?://", url or "", re.IGNORECASE):
        url = "https://" + (url or "").lstrip("/")
    ok = await db.scraper_set_enabled(url, True, reset_failures=True)
    return ok, ("Resumed (failure_count reset)" if ok else "Not in scrape list")


# ── Historical backfill (one-shot gallery-dl on whole profile) ─────────
# The regular scraper is a forward-looking watcher: first probe baselines
# the existing posts; only NEW posts after that trigger a download. If
# the operator wants the historical content too, start_backfill spawns
# a gallery-dl process against the entire profile. Runs in the background
# (asyncio task) so the API can return immediately. Status is kept in
# memory; resets on daemon restart, which is fine for a one-shot tool.

_active_backfills: dict[str, dict] = {}


def backfill_status() -> dict:
    """Return a copy of the in-memory backfill status dict.
    Keys are profile URLs; values include status / started_at / ended_at /
    items / error."""
    return {u: dict(s) for u, s in _active_backfills.items()}


async def start_backfill(url: str) -> tuple[bool, str]:
    """Spawn gallery-dl against the entire profile so historical content
    lands in /downloads/scraper-backfill/<platform>/<user>/. Returns
    immediately; the task continues in the background."""
    if not re.match(r"^https?://", url or "", re.IGNORECASE):
        url = "https://" + (url or "").lstrip("/")
    url = url.rstrip("/")
    profile = await db.scraper_get_profile(url)
    if not profile:
        return False, "Not in scrape list"
    if _active_backfills.get(url, {}).get("status") == "running":
        return False, "Backfill already running for this profile"

    username = profile.get("username") or "_unknown"
    platform = profile.get("platform") or "instagram"
    cookie_key = _cookie_key_from_url(profile["url"])
    cookiepath = _cookie_path(cookie_key) if cookie_key else None
    output_root = f"/downloads/scraper-backfill/{platform}/{username}"

    cmd = ["gallery-dl", "-D", output_root, "--retries", "3", "--sleep", "2-5"]
    if cookiepath:
        cmd += ["--cookies", cookiepath]
    cmd.append(url + "/")

    async def _run():
        started_at = datetime.now(timezone.utc).isoformat()
        _active_backfills[url] = {
            "status":     "running",
            "started_at": started_at,
            "platform":   platform,
            "username":   username,
            "output":     output_root,
        }
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            out, err = await proc.communicate()
            rc = proc.returncode or 0
            stdout_text = (out or b"").decode("utf-8", errors="replace")
            stderr_text = (err or b"").decode("utf-8", errors="replace")
            # gallery-dl prints one filepath per downloaded item to stdout
            items = sum(1 for ln in stdout_text.splitlines()
                        if ln.startswith("/downloads"))
            entry = _active_backfills.get(url, {})
            entry["ended_at"] = datetime.now(timezone.utc).isoformat()
            entry["items"]    = items
            if rc == 0:
                entry["status"] = "complete"
                logger.info("backfill complete: %s (%s items)", username, items)
            else:
                entry["status"] = "error"
                entry["error"]  = (stderr_text or f"exit {rc}")[:500]
                logger.warning("backfill failed: %s (exit %s) — %s",
                                username, rc, stderr_text[:200])
            _active_backfills[url] = entry
        except Exception as e:
            logger.exception("backfill crashed: %s", username)
            _active_backfills[url].update({
                "status":   "error",
                "error":    str(e)[:500],
                "ended_at": datetime.now(timezone.utc).isoformat(),
            })

    asyncio.create_task(_run())
    return True, f"Backfill started → {output_root}"


async def probe_now(app: Application, url: str) -> tuple[bool, str]:
    """Force a single probe immediately, outside the session schedule. Used by
    /scrape_now. Does NOT alter next_probe_at."""
    if not re.match(r"^https?://", url or "", re.IGNORECASE):
        url = "https://" + (url or "").lstrip("/")
    profile = await db.scraper_get_profile(url)
    if not profile:
        return False, "Not in scrape list"
    cookie_key = _cookie_key_from_url(profile["url"])
    if not cookie_key or not _cookie_path(cookie_key):
        return False, f"Cookie file missing: {cookie_key}.txt"
    ua = _pick_ua_for_cookie()
    state = await db.cookie_ensure(cookie_key, ua)
    ua = state.get("user_agent") or ua
    posts, err = await _probe_profile(profile, cookie_key, ua)
    if err:
        return False, f"Probe error: {err[:160]}"
    seen = set(profile.get("last_post_ids") or [])
    new_posts = [p for p in posts if p["id"] not in seen]
    new_downloads = 0
    if seen:
        for post in reversed(new_posts):
            if await _dispatch_new_post(app, profile, post):
                new_downloads += 1
    await db.scraper_update_probe_result(
        profile["url"],
        last_post_ids=[p["id"] for p in posts] or None,
        next_probe_at=profile.get("next_probe_at")
                       or _next_probe_at_for(profile, success=True, http_code=200),
        http_code=200, error=None, failure_reset=True,
        new_downloads=new_downloads,
    )
    if not seen:
        return True, f"Baseline recorded ({len(posts)} posts). New posts will be downloaded next probe."
    return True, f"Probed. {len(new_posts)} new, {new_downloads} downloaded."
