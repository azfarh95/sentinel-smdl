"""Telegram Bot API glue for the sticker maker.

Wraps upload + create-set / add-to-set so the route layer doesn't have to
care about the create-vs-add branch or the bot-username suffix gymnastics.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Optional

from telegram import Bot, InputFile, InputSticker
from telegram.error import BadRequest, TelegramError

from . import database as _db

logger = logging.getLogger(__name__)

# Cached bot username so we don't hit get_me on every call.
_BOT_USERNAME: Optional[str] = None


async def get_bot_username(bot: Bot) -> str:
    """Return the bot's username (no @). Cached after the first call."""
    global _BOT_USERNAME
    if _BOT_USERNAME:
        return _BOT_USERNAME
    me = await bot.get_me()
    _BOT_USERNAME = (me.username or "").lstrip("@")
    return _BOT_USERNAME


def _slug(s: str) -> str:
    """Lowercase ASCII slug suitable for a Telegram sticker-set name segment.
    Telegram rule: pack name must be [A-Za-z0-9_], 1-64 chars, must start
    with a letter, and end with `_by_<bot_username>`."""
    s = (s or "").lower()
    s = re.sub(r"[^a-z0-9_]+", "_", s)
    s = re.sub(r"_+", "_", s).strip("_")
    if not s or not s[0].isalpha():
        s = "u" + s
    return s[:32] or "user"


async def resolve_pack(bot: Bot, user_id: int, first_name: str | None) -> dict:
    """Return the user's pack info, creating the canonical name if missing.
    Does NOT call Telegram yet — that happens on first sticker. Returns
    {pack_name, pack_title, telegram_url, exists_in_db}."""
    existing = await _db.sticker_pack_get(user_id)
    if existing:
        return {**existing, "exists_in_db": True}

    bot_username = await get_bot_username(bot)
    slug = _slug(first_name or f"u{user_id}")
    pack_name  = f"{slug}_pack_by_{bot_username}"
    pack_title = f"{(first_name or 'My').strip()}'s SMDL Stickers"
    telegram_url = f"https://t.me/addstickers/{pack_name}"
    return {
        "pack_name":    pack_name,
        "pack_title":   pack_title,
        "telegram_url": telegram_url,
        "exists_in_db": False,
    }


async def _set_exists(bot: Bot, pack_name: str) -> bool:
    """Probe Telegram for an existing sticker set by name."""
    try:
        await bot.get_sticker_set(name=pack_name)
        return True
    except BadRequest as e:
        msg = (str(e) or "").lower()
        if "stickerset_invalid" in msg or "not found" in msg:
            return False
        raise


async def upload_and_add(
    bot: Bot,
    user_id: int,
    webm_path: Path,
    emoji: str,
    pack_name: str,
    pack_title: str,
) -> tuple[str, str]:
    """Upload a webm, create-or-append to the user's sticker set.

    Returns (telegram_file_id, set_url).
    Raises TelegramError on hard failure.
    """
    # 1. Upload the raw file. upload_sticker_file gives back a File with
    #    a reusable file_id that the create/add calls accept by reference.
    with open(webm_path, "rb") as f:
        uploaded = await bot.upload_sticker_file(
            user_id=user_id,
            sticker=InputFile(f, filename=webm_path.name),
            sticker_format="video",
        )
    file_id = uploaded.file_id

    sticker_obj = InputSticker(
        sticker=file_id,
        format="video",
        emoji_list=[emoji or "🎬"],
    )

    # 2. Add or create.
    if await _set_exists(bot, pack_name):
        await bot.add_sticker_to_set(
            user_id=user_id,
            name=pack_name,
            sticker=sticker_obj,
        )
    else:
        await bot.create_new_sticker_set(
            user_id=user_id,
            name=pack_name,
            title=pack_title,
            stickers=[sticker_obj],
        )

    set_url = f"https://t.me/addstickers/{pack_name}"
    return file_id, set_url
