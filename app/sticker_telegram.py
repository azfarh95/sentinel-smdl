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


# Per-kind naming + title suffix so the three packs are obviously distinct
# both as URLs and in Telegram's UI.
_KIND_SUFFIX = {
    "video":        ("pack",  "SMDL Stickers"),
    "static":       ("img",   "SMDL Stickers (Images)"),
    "custom_emoji": ("emoji", "SMDL Emoji"),
}


async def resolve_pack(bot: Bot, user_id: int, first_name: str | None,
                       kind: str = "video") -> dict:
    """Return the user's pack info for a given KIND, creating the canonical
    name if missing. Does NOT call Telegram yet — that happens on first
    sticker. Returns {pack_name, pack_title, telegram_url, pack_kind,
    exists_in_db}."""
    k = kind if kind in _KIND_SUFFIX else "video"
    existing = await _db.sticker_pack_get(user_id, k)
    if existing:
        return {**existing, "pack_kind": k, "exists_in_db": True}

    bot_username = await get_bot_username(bot)
    slug = _slug(first_name or f"u{user_id}")
    suffix, title_suffix = _KIND_SUFFIX[k]
    pack_name  = f"{slug}_{suffix}_by_{bot_username}"
    pack_title = f"{(first_name or 'My').strip()}'s {title_suffix}"
    telegram_url = f"https://t.me/addstickers/{pack_name}"
    return {
        "pack_name":    pack_name,
        "pack_title":   pack_title,
        "telegram_url": telegram_url,
        "pack_kind":    k,
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
    file_path: Path,
    emoji: str,
    pack_name: str,
    pack_title: str,
    *,
    sticker_format: str = "video",
    sticker_type: str = "regular",
) -> tuple[str, str]:
    """Upload a sticker file, create-or-append to the user's sticker set.

    sticker_format: 'video' (webm), 'static' (webp/png), or 'animated' (tgs).
    sticker_type:   'regular' or 'custom_emoji'. Custom-emoji packs are a
                    separate pack-type on Telegram and can never be mixed
                    with regular sticker packs.

    Returns (telegram_file_id, set_url).
    Raises TelegramError on hard failure.
    """
    # 1. Upload the raw file. upload_sticker_file returns a File with a
    #    reusable file_id that the create/add calls accept by reference.
    with open(file_path, "rb") as f:
        uploaded = await bot.upload_sticker_file(
            user_id=user_id,
            sticker=InputFile(f, filename=file_path.name),
            sticker_format=sticker_format,
        )
    file_id = uploaded.file_id

    sticker_obj = InputSticker(
        sticker=file_id,
        format=sticker_format,
        emoji_list=[emoji or "🎬"],
    )

    # 2. Add or create. Custom-emoji packs need `sticker_type` flag at
    #    creation; can't be retro-fitted on an existing regular pack.
    if await _set_exists(bot, pack_name):
        await bot.add_sticker_to_set(
            user_id=user_id,
            name=pack_name,
            sticker=sticker_obj,
        )
    else:
        kwargs = dict(
            user_id=user_id,
            name=pack_name,
            title=pack_title,
            stickers=[sticker_obj],
        )
        if sticker_type == "custom_emoji":
            kwargs["sticker_type"] = "custom_emoji"
        await bot.create_new_sticker_set(**kwargs)

    set_url = f"https://t.me/addstickers/{pack_name}"
    return file_id, set_url
