"""Telegram bot — detects video URLs, downloads, sends back. No AI involved."""

import asyncio
import logging
import os
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from .config import ALLOWED_CHAT_IDS, DELETE_AFTER_SEND, LIVE_ENABLED, OWNER_CHAT_ID
from .downloader import download, identify_post, send_files, _resolve_cookies
from .i18n import LANG_LABELS, SUPPORTED_LANGS, get_lang, set_lang, t
from .interceptor import find_video_url
from .live_downloader import detect_live, record_live
from . import file_serve, stream_monitor, telethon_uploader

DOWNLOADS_DIR = os.environ.get("DOWNLOADS_DIR", "/downloads")


def _build_delivery_links(filepath: str) -> dict:
    """Given an absolute filepath under DOWNLOADS_DIR, build the tailnet + share URLs.

    Returns {"tailnet": str | None, "share": str | None, "rel": str}.
    Both URLs are 'optional' — if Tailscale isn't bound or share secret missing,
    the corresponding entry is None.
    """
    try:
        rel = str(Path(filepath).resolve().relative_to(Path(DOWNLOADS_DIR).resolve()))
    except ValueError:
        rel = Path(filepath).name  # fall back to basename if outside downloads root

    out = {"rel": rel, "tailnet": None, "share": None}

    # Path 2 — tailnet. Resolve the host's tailnet IP from env var (set by
    # docker-compose once Phase 1.5 binds smdl to the tailnet IP). If unset,
    # we still emit a hostname fallback that works once MagicDNS is on.
    tailnet_host = os.environ.get("SMDL_TAILNET_HOST", "sentinel-host.tail.az-sentinel.xyz")
    out["tailnet"] = f"http://{tailnet_host}:8096/m/{rel}"

    # Path 1 — public signed share. Requires SMDL_PUBLIC_BASE_URL + share secret.
    share = file_serve.sign_share_url(rel)
    if share:
        out["share"] = share

    return out


def _format_delivery_message(size_mb: float, links: dict, expires_hours: int = 24, lang: str = "en") -> str:
    parts = [t("file_ready", lang, size_mb=size_mb)]
    if links.get("tailnet"):
        parts.append(t("tailnet_link", lang, url=links["tailnet"]))
    if links.get("share"):
        parts.append(t("share_link", lang, url=links["share"], hours=expires_hours))
    if not links.get("tailnet") and not links.get("share"):
        parts.append(t("no_delivery", lang, rel=links["rel"]))
    return "\n\n".join(parts)

# Active livestream recordings, keyed by chat_id.
# Each entry: {"stop_flag": {"stop": False}, "url": str, "started_at": float}
_active_live_jobs: dict[int, dict] = {}

# Per-URL no-extractor fail counter (chat_id -> {url: n}). Resets on success.
# After 3 consecutive 'no_extractor' failures we tell the user the site isn't
# supported, instead of letting them keep trying. Only no_extractor counts;
# auth/disk/transient failures are user-fixable and don't increment.
LIVE_NO_EXTRACTOR_RETRY_BUDGET = 3
_live_url_fail_count: dict[tuple[int, str], int] = {}

logger = logging.getLogger(__name__)

SMDL_BOT_TOKEN = os.environ["SMDL_BOT_TOKEN"]

_app: Application | None = None


def get_application() -> Application:
    return _app


async def build() -> Application:
    global _app
    # concurrent_updates=True is REQUIRED — without it, python-telegram-bot
    # processes updates sequentially. A long-running live recording would
    # block /stop_livestream and any other incoming message until the
    # recording finishes, defeating the whole point of having a stop command.
    _app = Application.builder().token(SMDL_BOT_TOKEN).concurrent_updates(True).build()

    async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        msg = update.effective_message
        logger.info("Received update: chat=%s text=%r",
                    msg.chat_id if msg else None,
                    (msg.text or "")[:80] if msg else None)

        if not msg or not msg.text:
            return

        chat_id = msg.chat_id
        if ALLOWED_CHAT_IDS and chat_id not in ALLOWED_CHAT_IDS:
            logger.info("Ignoring chat_id %s (not in ALLOWED_CHAT_IDS)", chat_id)
            return

        result = find_video_url(msg.text)
        if not result:
            logger.info("No video URL found in: %r", msg.text[:80])
            return

        platform, url = result
        logger.info("Video URL detected [%s]: %s", platform, url[:80])
        lang = get_lang(chat_id)

        status_msg = await msg.reply_text(t("identifying", lang, platform=platform))

        try:
            info = await identify_post(url)
        except Exception as e:
            await status_msg.edit_text(t("identify_failed", lang, error=str(e)))
            return

        if info.get("error"):
            is_private = info.get("is_private", False)
            err_text = (
                t("private_account", lang) if is_private
                else t("could_not_identify", lang, error=info["error"][:200])
            )
            await status_msg.edit_text(err_text)
            return

        media_type = info.get("media_type", "video")
        count = info.get("count", 1)
        uploader = info.get("uploader") or info.get("uploader_id") or platform
        is_live  = bool(info.get("is_live"))
        media_label = {
            "photo":    "photo",
            "carousel": f"carousel ({count} items)",
            "video":    "video",
            "live":     "🔴 LIVE",
        }.get(media_type, "media")

        is_owner = (OWNER_CHAT_ID is not None and chat_id == OWNER_CHAT_ID)

        # ── Live recording branch ──────────────────────────────────────────────
        if is_live:
            if not LIVE_ENABLED:
                await status_msg.edit_text(t("live_disabled", lang, platform=platform, uploader=uploader))
                return

            # Retry budget — if we've already failed 3+ times on THIS url for THIS
            # chat with 'no_extractor' (yt-dlp doesn't support the site), tell
            # the user upfront instead of trying again.
            fail_key = (chat_id, url)
            if _live_url_fail_count.get(fail_key, 0) >= LIVE_NO_EXTRACTOR_RETRY_BUDGET:
                await status_msg.edit_text(
                    t("live_site_unsupported", lang, platform=platform, budget=LIVE_NO_EXTRACTOR_RETRY_BUDGET)
                )
                return

            await status_msg.edit_text(t("live_started", lang, platform=platform, uploader=uploader))

            cookiepath = _resolve_cookies(url)

            # Job tracking for /stop_livestream
            stop_flag = {"stop": False}
            _active_live_jobs[chat_id] = {
                "stop_flag":  stop_flag,
                "url":        url,
                "started_at": __import__("time").time(),
                "uploader":   uploader,
                "platform":   platform,
            }

            # Throttled progress callback — edits the same message in place
            async def _on_progress(p):
                elapsed = p.get("elapsed_seconds", 0)
                bytes_  = p.get("bytes", 0)
                mb      = bytes_ / (1024 * 1024) if bytes_ else 0
                mins    = elapsed // 60
                try:
                    await status_msg.edit_text(
                        t("live_progress", lang, uploader=uploader, mins=mins, mb=mb)
                    )
                except Exception:
                    pass  # rate-limited or message gone

            try:
                live_result = await record_live(url, cookiepath, on_progress=_on_progress, stop_flag=stop_flag)
            finally:
                _active_live_jobs.pop(chat_id, None)

            mins  = live_result["duration_seconds"] // 60
            mb    = live_result["bytes_downloaded"] / (1024 * 1024)
            files = live_result.get("files") or []

            # Track per-URL no_extractor count. Success/auth-fail/disk-low/etc
            # don't count — only the "yt-dlp can't extract this site" cases.
            if live_result["abort_reason"] == "no_extractor":
                _live_url_fail_count[fail_key] = _live_url_fail_count.get(fail_key, 0) + 1
            elif live_result["abort_reason"] in ("stream_ended", "user_stopped"):
                _live_url_fail_count.pop(fail_key, None)  # reset on confirmed-working

            reason = live_result["abort_reason"]
            if reason == "stream_ended":
                summary = t("live_ended_natural", lang, mins=mins, mb=mb)
            elif reason == "user_stopped":
                summary = t("live_user_stopped", lang, mins=mins, mb=mb)
            elif reason == "session_fail":
                summary = t("live_session_fail", lang, mins=mins, mb=mb)
            elif reason == "no_extractor":
                attempts = _live_url_fail_count[fail_key]
                if attempts >= LIVE_NO_EXTRACTOR_RETRY_BUDGET:
                    summary = t("live_no_extractor_final", lang, attempts=attempts)
                else:
                    remaining = LIVE_NO_EXTRACTOR_RETRY_BUDGET - attempts
                    summary = t(
                        "live_no_extractor_retry", lang,
                        attempts=attempts, budget=LIVE_NO_EXTRACTOR_RETRY_BUDGET, remaining=remaining,
                    )
            elif reason == "platform_not_allowed":
                summary = t("live_platform_not_allowed", lang, detail=live_result["detail"])
            elif reason == "disk_low":
                summary = t("live_disk_low", lang, detail=live_result["detail"])
            else:
                summary = t(
                    "live_other_abort", lang,
                    reason=reason, mins=mins, mb=mb, detail=live_result.get("detail", "")[:120],
                )

            await status_msg.edit_text(summary)

            if files:
                first = files[0]
                first_path = Path(first)
                size_mb = round(first_path.stat().st_size / 1024 / 1024, 1) if first_path.exists() else 0
                if size_mb < 50:
                    # Bot API fits — inline send
                    with open(first, "rb") as f:
                        await ctx.bot.send_video(chat_id=chat_id, video=f,
                                                 caption=info.get("title"),
                                                 read_timeout=180, write_timeout=180)
                else:
                    # Too big for bot API. Send delivery links (tailnet + signed share).
                    # Skip telethon upload for live recordings (Twitch can be hours long;
                    # signed URLs scale better than waiting on a 2 GB upload).
                    links = _build_delivery_links(first)
                    await msg.reply_text(_format_delivery_message(size_mb, links, lang=lang))
            return

        # ── Normal (non-live) download ─────────────────────────────────────────
        try:
            await status_msg.edit_text(
                t("downloading", lang, platform=platform, uploader=uploader, media_label=media_label)
            )

            result = await download(url, media_type=media_type, is_owner=is_owner)

            if result.get("error"):
                await status_msg.edit_text(t("download_failed", lang, error=result["error"]))
                return

            files = result["files"]
            cached = result.get("cached", False)
            file_count = len(files)
            title = info.get("title", "")

            prefix = "Cached · s" if cached else "S"
            await status_msg.edit_text(
                t("sending_files", lang, prefix=prefix, count=file_count)
                if file_count > 1 else
                t("sending_one", lang, prefix=prefix, platform=platform, media_label=media_label)
            )

            send_result = await send_files(ctx.bot, chat_id, files, caption=title)

            if send_result.get("ok"):
                sent = send_result.get("count", 1)
                size = send_result.get("size_mb")
                cached_tag = " · cached" if cached else ""
                detail = f"{sent} file{'s' if sent > 1 else ''}" + (f" · {size} MB" if size else "") + cached_tag
                await status_msg.edit_text(t("sent_short", lang, detail=detail))

                if DELETE_AFTER_SEND and not cached:
                    for fp in files:
                        try:
                            Path(fp).unlink()
                        except Exception:
                            pass
            elif send_result.get("error") == "file_too_large":
                size_mb_local = send_result['size_mb']
                if telethon_uploader.is_configured() and size_mb_local < 1900:  # leave headroom under 2 GB
                    await status_msg.edit_text(t("uploading_telethon", lang, size_mb=size_mb_local))
                    up = await telethon_uploader.upload_file(
                        files[0], chat_id, caption=info.get("title"),
                    )
                    if up.get("ok"):
                        await status_msg.edit_text(t("uploaded_telethon", lang, size_mb=size_mb_local))
                    else:
                        links = _build_delivery_links(files[0])
                        await status_msg.edit_text(_format_delivery_message(size_mb_local, links, lang=lang))
                else:
                    links = _build_delivery_links(files[0])
                    await status_msg.edit_text(_format_delivery_message(size_mb_local, links, lang=lang))
            else:
                await status_msg.edit_text(t("send_failed", lang, error=send_result.get("error")))

        except Exception as e:
            logger.exception("Download pipeline error")
            await status_msg.edit_text(t("error_generic", lang, error=str(e)))

    async def handle_stop_livestream(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        chat_id = update.effective_chat.id
        active_chats = list(_active_live_jobs.keys())
        logger.info("CMD /stop_livestream from chat=%s | active_jobs=%s", chat_id, active_chats)
        if ALLOWED_CHAT_IDS and chat_id not in ALLOWED_CHAT_IDS:
            logger.info("  rejected: chat not in ALLOWED_CHAT_IDS=%s", ALLOWED_CHAT_IDS)
            return
        lang = get_lang(chat_id)
        job = _active_live_jobs.get(chat_id)
        if not job:
            logger.info("  no active job for this chat — replying 'No active livestream'")
            await update.message.reply_text(t("no_active_live", lang))
            return
        job["stop_flag"]["stop"] = True
        elapsed_min = int((__import__("time").time() - job["started_at"]) // 60)
        logger.info("  stop_flag set; %s min elapsed; replying confirmation", elapsed_min)
        await update.message.reply_text(
            t("stop_requested", lang, platform=job["platform"], uploader=job["uploader"], elapsed_min=elapsed_min)
        )

    async def handle_live_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        chat_id = update.effective_chat.id
        logger.info("CMD /live_status from chat=%s | active_jobs=%s", chat_id, list(_active_live_jobs.keys()))
        if ALLOWED_CHAT_IDS and chat_id not in ALLOWED_CHAT_IDS:
            return
        lang = get_lang(chat_id)
        job = _active_live_jobs.get(chat_id)
        if not job:
            await update.message.reply_text(t("no_active_live_short", lang))
            return
        elapsed_min = int((__import__("time").time() - job["started_at"]) // 60)
        await update.message.reply_text(
            t("live_status_active", lang, platform=job["platform"], uploader=job["uploader"], elapsed_min=elapsed_min)
        )

    # ── Stream monitor commands ────────────────────────────────────────────
    def _is_owner(chat_id: int) -> bool:
        # Watchlist is a global single-list resource — owner-only by design (V1).
        return OWNER_CHAT_ID is not None and chat_id == OWNER_CHAT_ID

    async def handle_watch(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        chat_id = update.effective_chat.id
        lang = get_lang(chat_id)
        if not _is_owner(chat_id):
            await update.message.reply_text(t("owner_only", lang))
            return
        if not ctx.args:
            await update.message.reply_text(t("watch_usage", lang))
            return
        url = ctx.args[0]
        label = " ".join(ctx.args[1:]) if len(ctx.args) > 1 else None
        added, key = stream_monitor.add_to_watchlist(url, label=label, added_by=chat_id)
        if added:
            await update.message.reply_text("✅ " + t("watch_added", lang, url=url))
        else:
            await update.message.reply_text("ℹ " + t("watch_already", lang, url=url))

    async def handle_unwatch(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        chat_id = update.effective_chat.id
        lang = get_lang(chat_id)
        if not _is_owner(chat_id):
            await update.message.reply_text(t("owner_only", lang))
            return
        if not ctx.args:
            await update.message.reply_text(t("unwatch_usage", lang))
            return
        url = ctx.args[0]
        removed, key = stream_monitor.remove_from_watchlist(url)
        if removed:
            await update.message.reply_text("🗑 " + t("watch_removed", lang, url=url))
        else:
            await update.message.reply_text("ℹ " + t("watch_not_found", lang, url=url))

    async def handle_watchlist(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        chat_id = update.effective_chat.id
        lang = get_lang(chat_id)
        if not _is_owner(chat_id):
            await update.message.reply_text(t("owner_only", lang))
            return
        entries = stream_monitor.list_watchlist()
        if not entries:
            await update.message.reply_text(t("watchlist_empty", lang))
            return
        lines = [t("watchlist_header", lang, count=len(entries))]
        for e in entries:
            label = e.get("label") or e.get("url") or "?"
            url = e.get("url") or "?"
            status = stream_monitor._last_status.get(url, "?")
            badge = {"live": "🔴", "offline": "⚫", "?": "⚪"}.get(status, "⚪")
            lines.append(f"{badge} {label}\n   {url}")
        await update.message.reply_text("\n".join(lines), disable_web_page_preview=True)

    async def handle_language(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        chat_id = update.effective_chat.id
        if ALLOWED_CHAT_IDS and chat_id not in ALLOWED_CHAT_IDS:
            return
        # Direct form: /language en  or  /language ru
        if ctx.args:
            requested = ctx.args[0].lower()
            if set_lang(chat_id, requested):
                key = f"lang_set_{requested}"
                await update.message.reply_text(t(key, requested))
            else:
                await update.message.reply_text(
                    t("lang_unknown", get_lang(chat_id),
                      lang=requested, supported=", ".join(SUPPORTED_LANGS))
                )
            return
        # Picker form: inline keyboard
        lang = get_lang(chat_id)
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton(t("btn_lang_en", lang), callback_data="lang:set:en"),
            InlineKeyboardButton(t("btn_lang_ru", lang), callback_data="lang:set:ru"),
        ]])
        await update.message.reply_text(t("lang_picker", lang), reply_markup=keyboard)

    async def handle_language_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        if query is None:
            return
        await query.answer()
        chat_id = query.message.chat_id if query.message else None
        if chat_id is None:
            return
        if ALLOWED_CHAT_IDS and chat_id not in ALLOWED_CHAT_IDS:
            return
        data = query.data or ""
        if not data.startswith("lang:set:"):
            return
        new_lang = data[len("lang:set:"):]
        if set_lang(chat_id, new_lang):
            try:
                await query.edit_message_reply_markup(reply_markup=None)
                await query.edit_message_text(t(f"lang_set_{new_lang}", new_lang))
            except Exception:
                pass

    async def _run_monitor_recording(ctx: ContextTypes.DEFAULT_TYPE, chat_id: int, url: str):
        """Background task spawned from monitor 'Yes' button. Keeps the live
        flow self-contained — does NOT share retry-budget state with manual
        flow (monitor URLs are owner-vetted, no retry-budget gate needed)."""
        lang = get_lang(chat_id)
        platform = stream_monitor._probe_is_live(url)  # cheap re-probe
        uploader = (platform or {}).get("uploader") or "stream"
        status_msg = await ctx.bot.send_message(
            chat_id=chat_id,
            text=t("monitor_record_starting", lang, uploader=uploader),
        )
        cookiepath = _resolve_cookies(url)
        stop_flag = {"stop": False}
        _active_live_jobs[chat_id] = {
            "stop_flag":  stop_flag,
            "url":        url,
            "started_at": __import__("time").time(),
            "uploader":   uploader,
            "platform":   "monitor",
        }

        async def _on_progress(p):
            elapsed = p.get("elapsed_seconds", 0)
            mb = (p.get("bytes", 0)) / (1024 * 1024)
            mins = elapsed // 60
            try:
                await status_msg.edit_text(
                    t("live_progress", lang, uploader=uploader, mins=mins, mb=mb)
                )
            except Exception:
                pass

        try:
            live_result = await record_live(url, cookiepath, on_progress=_on_progress, stop_flag=stop_flag)
        except Exception as e:
            await status_msg.edit_text(t("monitor_recording_crashed", lang, error=str(e)))
            return
        finally:
            _active_live_jobs.pop(chat_id, None)

        mins = live_result["duration_seconds"] // 60
        mb = live_result["bytes_downloaded"] / (1024 * 1024)
        files = live_result.get("files") or []
        reason = live_result["abort_reason"]
        if reason == "stream_ended":
            summary = t("live_ended_natural", lang, mins=mins, mb=mb)
        elif reason == "user_stopped":
            summary = t("live_user_stopped", lang, mins=mins, mb=mb)
        elif reason == "session_fail":
            summary = t("live_session_fail", lang, mins=mins, mb=mb)
        else:
            summary = t(
                "live_other_abort", lang,
                reason=reason, mins=mins, mb=mb,
                detail=live_result.get("detail", "")[:120],
            )
        await status_msg.edit_text(summary)

        if files:
            first = files[0]
            first_path = Path(first)
            size_mb = round(first_path.stat().st_size / 1024 / 1024, 1) if first_path.exists() else 0
            if size_mb < 50:
                with open(first, "rb") as f:
                    await ctx.bot.send_video(
                        chat_id=chat_id, video=f,
                        read_timeout=180, write_timeout=180,
                    )
            else:
                links = _build_delivery_links(first)
                await ctx.bot.send_message(chat_id=chat_id, text=_format_delivery_message(size_mb, links, lang=lang))

    async def handle_monitor_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        if query is None:
            return
        await query.answer()
        chat_id = query.message.chat_id if query.message else None
        if chat_id is None or not _is_owner(chat_id):
            try:
                await query.answer(t("owner_only", get_lang(chat_id or 0)), show_alert=True)
            except Exception:
                pass
            return
        lang = get_lang(chat_id)
        data = query.data or ""
        if not data.startswith("mon:"):
            return
        parts = data.split(":", 2)
        if len(parts) < 3:
            return
        action, url = parts[1], parts[2]
        if action == "skip":
            try:
                await query.edit_message_reply_markup(reply_markup=None)
                await query.edit_message_text(
                    text=(query.message.text or "") + "\n\n" + t("monitor_skipped", lang),
                    disable_web_page_preview=True,
                )
            except Exception:
                pass
            return
        if action == "rec":
            try:
                await query.edit_message_reply_markup(reply_markup=None)
                await query.edit_message_text(
                    text=(query.message.text or "") + "\n\n" + t("monitor_starting", lang),
                    disable_web_page_preview=True,
                )
            except Exception:
                pass
            # Fire-and-forget — record_live runs for hours and must not block
            # other update processing.
            asyncio.create_task(_run_monitor_recording(ctx, chat_id, url))

    _app.add_handler(CommandHandler("stop_livestream", handle_stop_livestream))
    _app.add_handler(CommandHandler("stop_livestream_download", handle_stop_livestream))  # alias matching user phrasing
    _app.add_handler(CommandHandler("live_status", handle_live_status))
    _app.add_handler(CommandHandler("watch", handle_watch))
    _app.add_handler(CommandHandler("unwatch", handle_unwatch))
    _app.add_handler(CommandHandler("watchlist", handle_watchlist))
    _app.add_handler(CommandHandler("language", handle_language))
    _app.add_handler(CallbackQueryHandler(handle_monitor_callback, pattern=r"^mon:"))
    _app.add_handler(CallbackQueryHandler(handle_language_callback, pattern=r"^lang:"))
    _app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    return _app
