"""Check yt-dlp / gallery-dl against PyPI; prompt the owner to update via Telegram.

Mirrors stream_monitor's poll-loop + inline-keyboard-prompt shape (same
OWNER_CHAT_ID target, same settings-table dedup idiom as cookie_mark_alerted).

The "Update now" button runs `pip install --upgrade` in-process, then hard-exits.
The owner-box `smdl` service is `restart: unless-stopped` and runs as `user:
"0:0"`, so the exit relaunches the same container (not a fresh image pull) with
the just-installed package already on disk — no docker.sock access needed.
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _installed_version

import httpx
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application

from . import database
from .config import OWNER_CHAT_ID
from .i18n import get_lang, t

logger = logging.getLogger(__name__)

PACKAGES = ("yt-dlp", "gallery-dl")
CHECK_INTERVAL_SECONDS = 6 * 60 * 60  # release cadence doesn't need tighter
_NOTIFIED_KEY = "pkgupdate_last_notified:{pkg}"


def installed_version(pkg: str) -> str | None:
    try:
        return _installed_version(pkg)
    except PackageNotFoundError:
        return None


async def _latest_pypi_version(pkg: str) -> str | None:
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(f"https://pypi.org/pypi/{pkg}/json")
            resp.raise_for_status()
            return resp.json()["info"]["version"]
    except Exception as e:
        logger.warning("version_check: PyPI lookup failed for %s: %s", pkg, e)
        return None


async def _check_once(app: Application) -> None:
    if OWNER_CHAT_ID is None:
        return
    for pkg in PACKAGES:
        installed = installed_version(pkg)
        latest = await _latest_pypi_version(pkg)
        if not installed or not latest or installed == latest:
            continue
        setting_key = _NOTIFIED_KEY.format(pkg=pkg)
        if await database.get_setting(setting_key, "") == latest:
            continue  # already prompted for this exact version
        lang = get_lang(OWNER_CHAT_ID)
        text = t("pkg_update_available", lang, pkg=pkg, installed=installed, latest=latest)
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton(t("btn_update_now", lang), callback_data=f"pkgupd:go:{pkg}"),
            InlineKeyboardButton(t("btn_dismiss", lang), callback_data=f"pkgupd:skip:{pkg}"),
        ]])
        try:
            await app.bot.send_message(
                chat_id=OWNER_CHAT_ID, text=text, reply_markup=keyboard,
                disable_web_page_preview=True,
            )
            await database.set_setting(setting_key, latest)
            logger.info("version_check: %s %s -> %s — prompt sent", pkg, installed, latest)
        except Exception as e:
            logger.error("version_check: failed to send prompt for %s: %s", pkg, e)


async def check_loop(app: Application) -> None:
    """Forever loop. Sleeps between checks. Cancellable."""
    logger.info("version_check: started (interval=%ds, packages=%s)",
                CHECK_INTERVAL_SECONDS, PACKAGES)
    try:
        while True:
            await _check_once(app)
            await asyncio.sleep(CHECK_INTERVAL_SECONDS)
    except asyncio.CancelledError:
        logger.info("version_check: cancelled")
        raise
    except Exception as e:
        logger.exception("version_check: loop crashed: %s", e)


async def apply_update(pkg: str) -> tuple[bool, str]:
    """pip install --upgrade <pkg>. Returns (ok, new-version-or-error-tail)."""
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "pip", "install", "--upgrade", "--no-cache-dir", pkg,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    out, _ = await proc.communicate()
    if proc.returncode != 0:
        return False, out.decode(errors="replace")[-500:]
    return True, installed_version(pkg) or "?"


def restart_process() -> None:
    """Hard-exit so `restart: unless-stopped` relaunches the container with
    the just-upgraded package on disk."""
    os._exit(0)
