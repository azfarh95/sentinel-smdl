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


def _uid_tok(user_id: int) -> str:
    """Short, stable, lowercase per-user token (base36 of the id). Folded into
    generated pack names so two users NEVER collide onto the same Telegram
    sticker set — even when their display names slugify identically (emoji /
    stylised / non-Latin names all collapse to the 'u' fallback above)."""
    n = abs(int(user_id))
    if n == 0:
        return "0"
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    out = []
    while n:
        n, r = divmod(n, 36)
        out.append(digits[r])
    return "".join(reversed(out))


# Per-kind naming + title suffix so the three packs are obviously distinct
# both as URLs and in Telegram's UI.
# A 'regular' pack mixes video + static (Telegram allows it). It reuses the old
# video pack's "pack" suffix so existing video packs ARE the regular pack — no
# data migration on Telegram's side. custom_emoji stays its own pack type.
_KIND_SUFFIX = {
    "regular":      ("pack",  "SMDL Stickers"),
    "custom_emoji": ("emoji", "SMDL Emoji"),
}


def _canon_kind(kind: str | None) -> str:
    """video + static → one 'regular' pack; custom_emoji stays separate."""
    k = (kind or "regular").strip().lower()
    if k in ("video", "static", "regular"):
        return "regular"
    if k == "custom_emoji":
        return "custom_emoji"
    return "regular"


async def resolve_pack(bot: Bot, user_id: int, first_name: str | None,
                       kind: str = "video") -> dict:
    """Return the user's pack info for a given KIND, creating the canonical
    name if missing. Does NOT call Telegram yet — that happens on first
    sticker. Returns {pack_name, pack_title, telegram_url, pack_kind,
    exists_in_db}."""
    k = _canon_kind(kind)
    existing = await _db.sticker_pack_get(user_id, k)
    if existing:
        return {**existing, "pack_kind": k, "exists_in_db": True}

    bot_username = await get_bot_username(bot)
    suffix, title_suffix = _KIND_SUFFIX[k]
    # The pack name MUST be unique per user. Deriving it from the display name
    # alone let any two users whose names slugify the same (emoji / stylised /
    # non-Latin → 'u') generate the SAME set name and share one pack. Fold in a
    # per-user token so that can never happen. (Cap the name part so the full
    # `..._by_<bot>` stays within Telegram's 64-char limit.)
    base = _slug(first_name or "")[:16].strip("_")
    tok = _uid_tok(user_id)
    name_part = f"{base}_{tok}" if (base and base != "u") else f"u{tok}"
    pack_name  = f"{name_part}_{suffix}_by_{bot_username}"
    pack_title = f"{(first_name or 'My').strip()}'s {title_suffix}"
    telegram_url = f"https://t.me/addstickers/{pack_name}"
    return {
        "pack_name":    pack_name,
        "pack_title":   pack_title,
        "telegram_url": telegram_url,
        "pack_kind":    k,
        "exists_in_db": False,
    }


async def create_named_pack(bot: Bot, user_id: int, first_name: str | None,
                            title: str, kind: str = "regular") -> dict:
    """Reserve a NEW named pack (fresh Telegram set name) and make it the user's
    active pack. The Telegram set is created lazily on the first sticker added.
    Returns the same shape as resolve_pack (exists_in_db=True)."""
    k = _canon_kind(kind)
    bot_username = await get_bot_username(bot)
    # Per-user token prefix so two users never collide onto one set name (the
    # while-loop below also guards, but this makes uniqueness structural).
    _nm = _slug(first_name or "")[:12].strip("_")
    user_slug = f"{(_nm if _nm and _nm != 'u' else 'u')}_{_uid_tok(user_id)}"
    title = (title or "").strip()[:64] or "My Stickers"
    tslug = _slug(title)
    suffix, _ts = _KIND_SUFFIX[k]

    def _candidate(n: int) -> str:
        base = f"{user_slug}_{tslug}".strip("_") or user_slug
        base = base[:40].strip("_") or user_slug
        if n:
            base = f"{base[:37]}{n}"
        return f"{base}_{suffix}_by_{bot_username}"

    name = _candidate(0)
    i = 2
    while await _db.sticker_pack_name_exists(name):
        name = _candidate(i)
        i += 1
        if i > 99:
            break
    telegram_url = f"https://t.me/addstickers/{name}"
    await _db.sticker_pack_create(user_id, name, title, telegram_url,
                                  kind=k, make_active=True)
    return {
        "pack_name":    name,
        "pack_title":   title,
        "telegram_url": telegram_url,
        "pack_kind":    k,
        "exists_in_db": True,
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

    Returns (telegram_file_id, set_url). The returned file_id is the
    PACK-BOUND sticker id (i.e. the one inside the just-created/extended
    set), NOT the upload file_id — that distinction matters for
    `send_sticker`, which renders the pack-bound id as a real sticker but
    treats the raw-upload id as a .webm/.webp attachment.
    Raises TelegramError on hard failure.
    """
    # 1. Upload the raw file. The file_id returned here represents the
    #    RAW UPLOAD — usable as a reference in add/create calls, but NOT
    #    a sticker-set member yet. send_sticker(file_id=<upload_id>) ends
    #    up rendering as a .webm document attachment in chats.
    with open(file_path, "rb") as f:
        uploaded = await bot.upload_sticker_file(
            user_id=user_id,
            sticker=InputFile(f, filename=file_path.name),
            sticker_format=sticker_format,
        )
    upload_file_id = uploaded.file_id

    sticker_obj = InputSticker(
        sticker=upload_file_id,
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

    # 3. Resolve the pack-bound file_id. add/create don't return it — we
    #    have to re-read the set and grab the last sticker (TG always
    #    appends new stickers at the end). If anything goes wrong here,
    #    fall back to the upload file_id — the sticker IS in the pack
    #    even if the preview render is degraded.
    pack_file_id = upload_file_id
    try:
        tg_set = await bot.get_sticker_set(name=pack_name)
        if tg_set and getattr(tg_set, "stickers", None):
            pack_file_id = tg_set.stickers[-1].file_id
    except Exception as e:
        logger.warning("post-add set re-read failed for %s: %s", pack_name, e)

    set_url = f"https://t.me/addstickers/{pack_name}"
    return pack_file_id, set_url
