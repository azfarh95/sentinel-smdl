"""Telegram bot — detects video URLs, downloads, sends back. No AI involved."""

import asyncio
import logging
import os
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update, WebAppInfo
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from .config import ALLOWED_CHAT_IDS, DELETE_AFTER_SEND, LIVE_ENABLED, OWNER_CHAT_ID
from . import auth as _auth
from . import database as _db_users   # ← record_interaction lives here
from .downloader import download, identify_post, send_files, _resolve_cookies
from .i18n import (
    ALLOWED_VIDEO_QUALITIES, LANG_LABELS, SUPPORTED_LANGS,
    format_duration, format_local_time, format_transcode_summary,
    format_tz_offset, format_video_quality_summary,
    get_lang, get_transcode_pref, get_tz_offset, get_video_quality,
    set_lang, set_transcode_pref, set_tz_offset, set_video_quality, t,
)
from .interceptor import find_video_url
from .live_downloader import detect_live
from .recorder_bridge import bridge
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

# Per-URL no-extractor fail counter (chat_id -> {url: n}). Resets on success.
# After 3 consecutive 'no_extractor' failures we tell the user the site isn't
# supported, instead of letting them keep trying. Only no_extractor counts;
# auth/disk/transient failures are user-fixable and don't increment.
LIVE_NO_EXTRACTOR_RETRY_BUDGET = 3
_live_url_fail_count: dict[tuple[int, str], int] = {}

logger = logging.getLogger(__name__)

# Empty in the public web/TWA build (no Telegram operator bot). Required only
# when the bot is actually started — build() raises if it's missing. Keeping
# this a soft read lets the FastAPI app import and serve /iptv, /pwa, etc.
# without a token. See main.py lifespan (bot startup is gated on this value).
SMDL_BOT_TOKEN = os.environ.get("SMDL_BOT_TOKEN", "")

_app: Application | None = None


def get_application() -> Application:
    return _app


async def build() -> Application:
    global _app
    if not SMDL_BOT_TOKEN:
        raise RuntimeError(
            "SMDL_BOT_TOKEN is not set — cannot build the Telegram bot. "
            "This is expected in the public web/TWA build; main.py should "
            "gate bot startup on the token's presence."
        )
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

        # Record the interaction so this user appears in /admin/users. Done
        # BEFORE the auth gate so a freshly-banned user can still be seen in
        # the directory with the latest last_seen timestamp. Skip groups
        # (chat_id there is the group ID, not the user).
        try:
            chat_type = getattr(getattr(msg, "chat", None), "type", "")
            if chat_type == "private":
                u = update.effective_user
                await _db_users.record_interaction(
                    chat_id,
                    username=(u.username if u else None),
                    first_name=(u.first_name if u else None),
                    last_name=(u.last_name if u else None),
                )
        except Exception as _e:
            logger.warning("record_interaction failed: %s", _e)

        # Central auth gate: owner always allowed; banned users rejected;
        # admin-only mode rejects everyone except owner.
        if not await _auth.is_authorized(chat_id):
            decision = await _auth.classify(chat_id)
            logger.info("Rejecting chat_id %s: %s", chat_id, decision)
            try:
                if decision == "deny_admin_only":
                    await msg.reply_text("🔒 Service is in admin-only mode. Try again later.")
                elif decision == "deny_banned":
                    await msg.reply_text("⛔ Your access to this bot has been revoked.")
                elif decision == "deny_pending":
                    # Nudge them toward the handshake — don't leave them confused.
                    await msg.reply_text(
                        "⏳ Your access is pending approval.\n\n"
                        "Send /start to see your access code, then forward it to "
                        "the bot's owner. Codes expire in 1 minute — use "
                        "/regenerate_token if yours did."
                    )
            except Exception: pass
            return

        result = find_video_url(msg.text)
        if not result:
            logger.info("No video URL found in: %r", msg.text[:80])
            return

        platform, url = result
        logger.info("Video URL detected [%s]: %s", platform, url[:80])
        lang = get_lang(chat_id)

        # Per-site blocklist (admin-managed). Owner bypasses.
        if not _auth.is_owner(chat_id) and await _auth.is_platform_blocked(url):
            from .stream_monitor import extract_platform as _ep
            await msg.reply_text(f"⛔ Downloads from {_ep(url)} are disabled by the admin.")
            return

        status_msg = await msg.reply_text(t("identifying", lang, platform=platform))

        try:
            info = await identify_post(url)
        except Exception as e:
            await status_msg.edit_text(t("identify_failed", lang, error=str(e)))
            return

        if info.get("error"):
            is_private = info.get("is_private", False)
            err = info["error"]
            if "mouflon" in err.lower():
                err_text = t("live_mouflon_blocked", lang)
            elif is_private:
                err_text = t("private_account", lang)
            else:
                err_text = t("could_not_identify", lang, error=err[:200])
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

            # Throttled progress callback — edits the same message in place
            async def _on_progress(p):
                elapsed = p.get("elapsed_seconds", 0)
                bytes_  = p.get("bytes", 0)
                mb      = bytes_ / (1024 * 1024) if bytes_ else 0
                try:
                    await status_msg.edit_text(
                        t("live_progress", lang,
                          uploader=uploader, duration=format_duration(elapsed), mb=mb)
                    )
                except Exception:
                    pass  # rate-limited / not modified / message gone

            # Bridge owns job tracking now — no local _active_live_jobs needed.
            # bridge.record() blocks until recording finishes (naturally,
            # via /stop_livestream, or via failure); /stop_livestream is
            # serviced concurrently from a separate handler that calls
            # bridge.stop(chat_id) to set the stop_flag.
            tc_h, tc_keep = get_transcode_pref(chat_id)
            live_result = await bridge.record(
                chat_id, url,
                cookiepath=cookiepath,
                on_progress=_on_progress,
                transcode_height=tc_h,
                transcode_keep_original=tc_keep,
                platform=platform,
                uploader=uploader,
            )

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
            elif reason == "mouflon_blocked":
                summary = t("live_mouflon_blocked", lang)
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

            # Mouflon-blocked recordings are ad/promo content, not what the
            # user asked for — skip file delivery entirely so the bot doesn't
            # spam users with 24-second Stripchat ads.
            skip_delivery = reason == "mouflon_blocked"

            if files and not skip_delivery:
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
            elif files and skip_delivery:
                # Clean up the captured ad file so it doesn't pile up on disk.
                try:
                    Path(files[0]).unlink(missing_ok=True)
                except Exception:
                    pass
            return

        # ── Normal (non-live) download ─────────────────────────────────────────
        try:
            await status_msg.edit_text(
                t("downloading", lang, platform=platform, uploader=uploader, media_label=media_label)
            )

            result = await download(
                url, media_type=media_type, is_owner=is_owner,
                quality=get_video_quality(chat_id),
            )

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

                # Per-user download history (Mini App reads this). Never crash
                # the user flow if telemetry write fails.
                try:
                    from . import database as _db
                    await _db.record_download(chat_id, url, files, platform, uploader)
                except Exception as _e:
                    logger.warning("record_download failed: %s", _e)

                # OneDrive mirror — only when mode is 'auto_after_send'. The
                # on_demand path is triggered from the Mini App's Downloads
                # tab instead. Fire-and-forget so a slow upload doesn't block
                # the Telegram reply path.
                try:
                    from .miniapp import _cfg_get as _od_cfg_get
                    od_mode = (_od_cfg_get("onedrive_mode") or "on_demand").lower()
                    if od_mode == "auto_after_send":
                        from . import onedrive as _od
                        folder = _od_cfg_get("onedrive_folder") or "/SMDL"
                        delete_after = bool(_od_cfg_get("onedrive_delete_after_upload"))
                        async def _mirror():
                            summary = await _od.auto_upload_files(
                                files, platform, uploader,
                                base_folder=folder,
                                delete_after_upload=delete_after,
                            )
                            if summary["sent_count"]:
                                logger.info("OneDrive: mirrored %d files (%.1f MB)",
                                            summary["sent_count"], summary["total_bytes"]/1024**2)
                            if summary["failed_count"]:
                                logger.warning("OneDrive: %d uploads failed: %s",
                                               summary["failed_count"], summary["failed"][:3])
                        asyncio.create_task(_mirror())
                except Exception as _e:
                    logger.warning("OneDrive auto-mirror dispatch failed: %s", _e)

                # Local-cleanup behavior: if OneDrive's delete_after_upload is
                # on, skip the DELETE_AFTER_SEND clear here — the OneDrive
                # task will unlink only AFTER successful upload (safer order).
                _od_will_delete = False
                try:
                    from .miniapp import _cfg_get as _cgc
                    _od_will_delete = (
                        (_cgc("onedrive_mode") or "").lower() == "auto_after_send"
                        and bool(_cgc("onedrive_delete_after_upload"))
                    )
                except Exception: pass
                if DELETE_AFTER_SEND and not cached and not _od_will_delete:
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
        active_chats = [j.chat_id for j in bridge.list_active()]
        logger.info("CMD /stop_livestream from chat=%s | active_jobs=%s", chat_id, active_chats)
        if not await _auth.is_authorized(chat_id):
            logger.info("  rejected: chat_id %s not authorized", chat_id)
            return
        lang = get_lang(chat_id)
        status = await bridge.stop(chat_id)
        if status is None:
            logger.info("  no active job for this chat — replying 'No active livestream'")
            await update.message.reply_text(t("no_active_live", lang))
            return
        logger.info("  stop_flag set; %s sec elapsed; replying confirmation", status.elapsed_seconds)
        await update.message.reply_text(
            t("stop_requested", lang,
              platform=status.platform or "?", uploader=status.uploader or "?",
              duration=format_duration(status.elapsed_seconds))
        )

    async def handle_live_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        chat_id = update.effective_chat.id
        active = [j.chat_id for j in bridge.list_active()]
        logger.info("CMD /live_status from chat=%s | active_jobs=%s", chat_id, active)
        if not await _auth.is_authorized(chat_id):
            return
        lang = get_lang(chat_id)
        status = bridge.status(chat_id)
        if status is None:
            await update.message.reply_text(t("no_active_live_short", lang))
            return
        await update.message.reply_text(
            t("live_status_active", lang,
              platform=status.platform or "?", uploader=status.uploader or "?",
              duration=format_duration(status.elapsed_seconds))
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
            tail = ""
            if stream_monitor.is_snoozed(e):
                until = int(e.get("snoozed_until") or 0)
                tail = f"  💤 until {format_local_time(until, chat_id)}"
            lines.append(f"{badge} {label}{tail}\n   {url}")
        await update.message.reply_text("\n".join(lines), disable_web_page_preview=True)

    # ── Profile scraper commands (owner-only) ───────────────────────────────
    async def handle_scrape_add(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        chat_id = update.effective_chat.id
        if not _is_owner(chat_id):
            return
        if not ctx.args:
            await update.message.reply_text(
                "Usage: /scrape_add <profile-url> [label]\n"
                "Example: /scrape_add https://www.instagram.com/someuser"
            )
            return
        from . import profile_monitor as _pm
        url = ctx.args[0]
        label = " ".join(ctx.args[1:]) if len(ctx.args) > 1 else None
        ok, msg = await _pm.add_profile(url, added_by=chat_id, label=label)
        await update.message.reply_text(("✅ " if ok else "ℹ ") + msg)

    async def handle_scrape_remove(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        chat_id = update.effective_chat.id
        if not _is_owner(chat_id):
            return
        if not ctx.args:
            await update.message.reply_text("Usage: /scrape_remove <profile-url>")
            return
        from . import profile_monitor as _pm
        ok, msg = await _pm.remove_profile(ctx.args[0])
        await update.message.reply_text(("✅ " if ok else "ℹ ") + msg)

    async def handle_scrape_list(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        chat_id = update.effective_chat.id
        if not _is_owner(chat_id):
            return
        from . import profile_monitor as _pm
        rows = await _pm.list_profiles()
        if not rows:
            await update.message.reply_text("📥 Scrape list is empty. Add one with /scrape_add <url>")
            return
        from datetime import datetime as _dt, timezone as _tz
        now = _dt.now(_tz.utc)
        lines = [f"📥 Scrape list ({len(rows)} profile{'s' if len(rows) != 1 else ''})"]
        last_platform = None
        for r in rows:
            plat = (r.get("platform") or "?").lower()
            if plat != last_platform:
                lines.append(f"\n— {plat.capitalize()} —")
                last_platform = plat
            badge = "⏸" if not r.get("enabled") else "✅"
            uname = r.get("username") or r.get("label") or r.get("url")
            fc = int(r.get("failure_count") or 0)
            dl = int(r.get("downloaded_count") or 0)
            tail = ""
            if r.get("next_probe_at"):
                try:
                    npa = _dt.fromisoformat(r["next_probe_at"])
                    delta = npa - now
                    mins = int(delta.total_seconds() // 60)
                    if mins < 0:        tail = " · due now"
                    elif mins < 60:     tail = f" · next ~{mins}m"
                    elif mins < 60*24:  tail = f" · next ~{mins // 60}h"
                    else:               tail = f" · next ~{mins // 1440}d"
                except Exception:
                    pass
            fail_tag = f" · ⚠{fc} fails" if fc else ""
            lines.append(f"{badge} @{uname}  · {dl} pulled{tail}{fail_tag}")
            if r.get("last_error") and fc:
                lines.append(f"   ↳ {(r.get('last_error') or '')[:120]}")
        await update.message.reply_text("\n".join(lines), disable_web_page_preview=True)

    async def handle_scrape_pause(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        chat_id = update.effective_chat.id
        if not _is_owner(chat_id):
            return
        if not ctx.args:
            await update.message.reply_text("Usage: /scrape_pause <profile-url>")
            return
        from . import profile_monitor as _pm
        ok, msg = await _pm.pause_profile(ctx.args[0])
        await update.message.reply_text(("✅ " if ok else "ℹ ") + msg)

    async def handle_scrape_resume(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        chat_id = update.effective_chat.id
        if not _is_owner(chat_id):
            return
        if not ctx.args:
            await update.message.reply_text("Usage: /scrape_resume <profile-url>")
            return
        from . import profile_monitor as _pm
        ok, msg = await _pm.resume_profile(ctx.args[0])
        await update.message.reply_text(("✅ " if ok else "ℹ ") + msg)

    async def handle_scrape_now(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        chat_id = update.effective_chat.id
        if not _is_owner(chat_id):
            return
        if not ctx.args:
            await update.message.reply_text("Usage: /scrape_now <profile-url>")
            return
        from . import profile_monitor as _pm
        await update.message.reply_text("⏳ Probing now…")
        ok, msg = await _pm.probe_now(_app, ctx.args[0])
        await update.message.reply_text(("✅ " if ok else "⚠ ") + msg)

    async def handle_language(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        chat_id = update.effective_chat.id
        if not await _auth.is_authorized(chat_id):
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

    async def handle_timezone(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        chat_id = update.effective_chat.id
        if not await _auth.is_authorized(chat_id):
            return
        lang = get_lang(chat_id)
        if not ctx.args:
            current = format_tz_offset(get_tz_offset(chat_id))
            await update.message.reply_text(
                t("tz_current", lang, tz=current) + "\n\n" + t("tz_usage", lang)
            )
            return
        raw = ctx.args[0]
        try:
            offset = float(raw)
        except ValueError:
            await update.message.reply_text(t("tz_invalid", lang, value=raw))
            return
        if not set_tz_offset(chat_id, offset):
            await update.message.reply_text(t("tz_invalid", lang, value=raw))
            return
        await update.message.reply_text(
            t("tz_set", lang, tz=format_tz_offset(offset))
        )

    async def handle_transcode(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        chat_id = update.effective_chat.id
        if not await _auth.is_authorized(chat_id):
            return
        lang = get_lang(chat_id)
        cur_h, cur_keep = get_transcode_pref(chat_id)
        current = format_transcode_summary(cur_h, cur_keep, lang)

        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton(t("btn_transcode_off", lang),   callback_data="tc:set:0:0")],
            [
                InlineKeyboardButton(t("btn_transcode_480_r", lang), callback_data="tc:set:480:0"),
                InlineKeyboardButton(t("btn_transcode_240_r", lang), callback_data="tc:set:240:0"),
            ],
            [
                InlineKeyboardButton(t("btn_transcode_480_k", lang), callback_data="tc:set:480:1"),
                InlineKeyboardButton(t("btn_transcode_240_k", lang), callback_data="tc:set:240:1"),
            ],
        ])
        await update.message.reply_text(
            t("transcode_picker", lang, current=current),
            reply_markup=keyboard,
        )

    async def handle_transcode_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        if query is None:
            return
        await query.answer()
        chat_id = query.message.chat_id if query.message else None
        if chat_id is None:
            return
        if not await _auth.is_authorized(chat_id):
            return
        data = query.data or ""
        if not data.startswith("tc:set:"):
            return
        parts = data.split(":")
        if len(parts) != 4:
            return
        try:
            height = int(parts[2])
            keep   = parts[3] == "1"
        except ValueError:
            return
        lang = get_lang(chat_id)
        if not set_transcode_pref(chat_id, height, keep):
            return
        summary = format_transcode_summary(height, keep, lang)
        try:
            await query.edit_message_reply_markup(reply_markup=None)
            await query.edit_message_text(t("transcode_set", lang, summary=summary))
        except Exception:
            pass

    async def handle_storage_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        chat_id = update.effective_chat.id
        if not _is_owner(chat_id):
            await update.message.reply_text(t("owner_only", get_lang(chat_id)))
            return
        lang = get_lang(chat_id)
        import shutil, asyncio as _asyncio
        from . import database as db
        from datetime import datetime
        from .i18n import format_local_time

        def _scan():
            base = Path(DOWNLOADS_DIR)
            total = used = free = 0
            try:
                u = shutil.disk_usage(str(base))
                total, used, free = u.total, u.used, u.free
            except Exception:
                pass
            live_dir = base / "live"
            def _stat_dir(d: Path) -> tuple[int, int]:
                count = total_bytes = 0
                if d.exists():
                    for f in d.rglob("*"):
                        try:
                            if f.is_file():
                                count += 1
                                total_bytes += f.stat().st_size
                        except Exception:
                            pass
                return count, total_bytes
            live_count, live_bytes = _stat_dir(live_dir)
            all_count, all_bytes  = _stat_dir(base)
            dl_count  = all_count - live_count
            dl_bytes  = all_bytes  - live_bytes
            return total, free, dl_count, dl_bytes, live_count, live_bytes

        loop = _asyncio.get_running_loop()
        total, free, dl_count, dl_bytes, live_count, live_bytes = await loop.run_in_executor(None, _scan)
        cs = await db.cache_stats()

        def _fmt_size(b: int) -> str:
            for unit in ("B", "KB", "MB", "GB", "TB"):
                if b < 1024 or unit == "TB":
                    return f"{b:.1f} {unit}"
                b /= 1024

        def _fmt_dt(s):
            if not s: return "—"
            try:
                from datetime import datetime, timezone as _tz
                dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
                return format_local_time(dt.timestamp(), chat_id, "%Y-%m-%d %H:%M")
            except Exception:
                return s[:16]

        await update.message.reply_text(t(
            "storage_stats", lang,
            free_gb=free / (1024 ** 3),
            total_gb=total / (1024 ** 3),
            downloads_count=dl_count,
            downloads_size=_fmt_size(dl_bytes),
            live_count=live_count,
            live_size=_fmt_size(live_bytes),
            cache_count=cs["count"],
            cache_oldest=_fmt_dt(cs["oldest"]),
            cache_newest=_fmt_dt(cs["newest"]),
        ))

    async def handle_clear_cache(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        chat_id = update.effective_chat.id
        if not _is_owner(chat_id):
            await update.message.reply_text(t("owner_only", get_lang(chat_id)))
            return
        lang = get_lang(chat_id)
        from . import database as db
        target_url = ctx.args[0] if ctx.args else None
        removed = await db.clear_cache(target_url)
        if target_url and removed == 0:
            await update.message.reply_text(t("cache_url_not_found", lang, url=target_url))
            return
        plural = "y" if removed == 1 else "ies"  # English-only quirk; ru template ignores
        await update.message.reply_text(t("cache_cleared", lang, count=removed, plural=plural))

    async def handle_default_video_size(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        chat_id = update.effective_chat.id
        if not await _auth.is_authorized(chat_id):
            return
        lang = get_lang(chat_id)
        current = format_video_quality_summary(get_video_quality(chat_id), lang)
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton(t("btn_vq_best", lang), callback_data="vq:set:best"),
                InlineKeyboardButton(t("btn_vq_1080", lang), callback_data="vq:set:1080p"),
            ],
            [
                InlineKeyboardButton(t("btn_vq_720",  lang), callback_data="vq:set:720p"),
                InlineKeyboardButton(t("btn_vq_360",  lang), callback_data="vq:set:360p"),
            ],
        ])
        await update.message.reply_text(
            t("vq_picker", lang, current=current),
            reply_markup=keyboard,
        )

    async def handle_default_video_size_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        if query is None:
            return
        await query.answer()
        chat_id = query.message.chat_id if query.message else None
        if chat_id is None:
            return
        if not await _auth.is_authorized(chat_id):
            return
        data = query.data or ""
        if not data.startswith("vq:set:"):
            return
        new_q = data[len("vq:set:"):]
        if not set_video_quality(chat_id, new_q):
            return
        lang = get_lang(chat_id)
        summary = format_video_quality_summary(new_q, lang)
        try:
            await query.edit_message_reply_markup(reply_markup=None)
            await query.edit_message_text(t("vq_set", lang, value=summary))
        except Exception:
            pass

    async def handle_language_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        if query is None:
            return
        await query.answer()
        chat_id = query.message.chat_id if query.message else None
        if chat_id is None:
            return
        if not await _auth.is_authorized(chat_id):
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

        async def _on_progress(p):
            elapsed = p.get("elapsed_seconds", 0)
            mb = (p.get("bytes", 0)) / (1024 * 1024)
            try:
                await status_msg.edit_text(
                    t("live_progress", lang,
                      uploader=uploader, duration=format_duration(elapsed), mb=mb)
                )
            except Exception:
                pass

        tc_h, tc_keep = get_transcode_pref(chat_id)
        try:
            live_result = await bridge.record(
                chat_id, url,
                cookiepath=cookiepath,
                on_progress=_on_progress,
                transcode_height=tc_h,
                transcode_keep_original=tc_keep,
                platform="monitor",
                uploader=uploader,
            )
        except Exception as e:
            await status_msg.edit_text(t("monitor_recording_crashed", lang, error=str(e)))
            return

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
        # Replace the prompt with a single-line confirmation so the chat
        # log stays terse (`[user] — Snoozed 8h (until xx:xx)`), not a
        # growing wall of LIVE-prompt history.
        uploader = stream_monitor.extract_username(url) or "?"
        if action == "skip":
            try:
                await query.edit_message_reply_markup(reply_markup=None)
                await query.edit_message_text(
                    text=t("monitor_skipped", lang, uploader=uploader),
                    disable_web_page_preview=True,
                )
            except Exception:
                pass
            return
        if action in ("snooze1h", "snooze8h"):
            mins = 60 if action == "snooze1h" else 8 * 60
            expires_at = stream_monitor.snooze_streamer(url, mins)
            until_str = format_local_time(expires_at, chat_id) if expires_at else "?"
            duration_label = "1h" if action == "snooze1h" else "8h"
            try:
                await query.edit_message_reply_markup(reply_markup=None)
                await query.edit_message_text(
                    text=t(
                        "monitor_snoozed", lang,
                        uploader=uploader,
                        duration=duration_label, until=until_str,
                    ),
                    disable_web_page_preview=True,
                )
            except Exception:
                pass
            return
        if action == "rec":
            try:
                await query.edit_message_reply_markup(reply_markup=None)
                await query.edit_message_text(
                    text=t("monitor_starting", lang, uploader=uploader),
                    disable_web_page_preview=True,
                )
            except Exception:
                pass
            # Fire-and-forget — record_live runs for hours and must not block
            # other update processing.
            asyncio.create_task(_run_monitor_recording(ctx, chat_id, url))

    async def _record_user(update: Update) -> None:
        """Tiny helper: UPSERT the user into the directory. Skips group and
        channel chats (Telegram chat_id is the GROUP id there, not the user,
        so recording it would create a phantom 'user' per group)."""
        chat = update.effective_chat
        if not chat or getattr(chat, "type", "") != "private":
            return
        try:
            u = update.effective_user
            await _db_users.record_interaction(
                chat.id,
                username=(u.username if u else None),
                first_name=(u.first_name if u else None),
                last_name=(u.last_name if u else None),
            )
        except Exception as _e:
            logger.warning("record_interaction failed: %s", _e)

    def _dashboard_keyboard() -> InlineKeyboardMarkup | None:
        url = os.environ.get("WEBAPP_URL", "").strip()
        if not url:
            return None
        return InlineKeyboardMarkup([[
            InlineKeyboardButton("📱 Open dashboard", web_app=WebAppInfo(url=url))
        ]])

    def _start_keyboard() -> InlineKeyboardMarkup | None:
        """Welcome keyboard: dashboard + a jump straight to the sticker editor."""
        rows = []
        durl = os.environ.get("WEBAPP_URL", "").strip()
        if durl:
            rows.append([InlineKeyboardButton("📱 Open dashboard", web_app=WebAppInfo(url=durl))])
        surl = _stickers_url()
        if surl:
            rows.append([InlineKeyboardButton("🎬 Make a sticker", web_app=WebAppInfo(url=surl))])
        return InlineKeyboardMarkup(rows) if rows else None

    async def _schedule_code_expiry_edit(bot, chat_id: int, message_id: int, lang: str):
        """Wait one TTL window. If the user is still 'pending' (i.e. didn't
        get approved during that minute), edit the original message in place
        so the visible '991-115-289' code is replaced with an 'expired' notice.
        Approved-during-wait users get nothing edited — the original message
        with their (now-stale) code stays as benign chat history; no security
        implication, it's a one-time code that's no longer valid in the DB."""
        try:
            from .database import PENDING_CODE_TTL
            await asyncio.sleep(int(PENDING_CODE_TTL.total_seconds()))
            user = await _db_users.get_user(chat_id)
            if not user or (user.get("status") or "").lower() != "pending":
                return  # approved, banned, or row vanished — don't touch
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=t("access_code_expired", lang),
                parse_mode="Markdown",
            )
        except Exception as _e:
            # Common cases: message deleted, bot lost access, parse_mode quirks.
            # All benign — log and move on.
            logger.debug("pending-code expiry edit failed for %s/%s: %s",
                         chat_id, message_id, _e)

    def _pending_welcome_text(name: str, row: dict) -> str:
        """Welcome shown to pending users. Deliberately does NOT identify
        the bot owner — legitimate users know who to forward the code to."""
        code = row.get("pending_code") or "(error: no code on file)"
        return (
            f"👋 Hi {name}!  This bot is invite-only.\n\n"
            f"Your one-time access code:\n\n"
            f"🔑  *{code}*\n\n"
            "Forward this code to the bot's owner — they're expecting it. "
            "You'll be approved on their side and can use the bot right after.\n\n"
            "⏱ Codes expire in *1 minute*. If yours expires before you can "
            "share it, send /regenerate\\_token for a fresh one."
        )

    async def handle_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        """First-touch handler. Branches on chat type:
          - Group, approved   → welcome (no code, shared usage)
          - Group, unapproved → show chat_id so owner can authorize
          - DM, pending  → approval code + instructions (60s TTL)
          - DM, active   → welcome + dashboard button
          - DM, banned   → polite revoke notice
          - Admin-only mode → "try again later"
        """
        chat = update.effective_chat
        chat_id = chat.id
        u = update.effective_user
        name = (u.first_name if u else None) or "there"

        # ── Group / supergroup branch ─────────────────────────────────────
        if chat and chat.type != "private":
            mode = await _auth.get_admin_only_mode()
            if mode["enabled"]:
                await update.message.reply_text("🔒 Service is in admin-only mode.")
                return
            if await _db_users.is_group_approved(chat_id):
                await update.message.reply_text(
                    "👋 SMDL is active in this group. "
                    "Paste any video URL and I'll fetch it here."
                )
                return
            # Unapproved group — tell the owner how to authorize this group.
            await update.message.reply_markdown(
                f"🔒 *Group not authorized.*\n\n"
                f"Ask the bot owner to add this group to the approved list.\n"
                f"Group chat ID:  `{chat_id}`"
            )
            return

        # ── DM branch (existing logic) ────────────────────────────────────
        row = None
        try:
            row = await _db_users.record_interaction(
                chat_id,
                username=(u.username if u else None),
                first_name=(u.first_name if u else None),
                last_name=(u.last_name if u else None),
            )
        except Exception as _e:
            logger.warning("record_interaction failed in /start: %s", _e)

        decision = await _auth.classify(chat_id)

        if decision == "deny_admin_only":
            await update.message.reply_text("🔒 Service is in admin-only mode. Try again later.")
            return
        if decision == "deny_banned":
            await update.message.reply_text("⛔ Your access to this bot has been revoked.")
            return
        if decision == "deny_pending":
            text = _pending_welcome_text(name, row or {})
            sent = await update.message.reply_markdown(text)
            # Schedule the expired-text edit. Owner approval cancels naturally
            # via the status-check inside the scheduler.
            if sent and getattr(sent, "message_id", None):
                asyncio.create_task(_schedule_code_expiry_edit(
                    ctx.bot, chat_id, sent.message_id, get_lang(chat_id),
                ))
            return

        # decision == "allow" — owner or already-approved user.
        kb = _start_keyboard()
        intro = (
            f"👋 Hi {name}!  Welcome to SMDL.\n\n"
            "Three things I do best:\n"
            "📥 Download — send a public link (YouTube, TikTok, Instagram, "
            "Twitch, X…) and I'll grab it in top quality.\n"
            "🎬 Stickers — send me a photo, video, GIF or circle and I'll turn "
            "it into a Telegram sticker, added to your pack.\n"
            "🔴 Auto-record — track Twitch / YouTube / Kick streamers and "
            "capture their public live streams. Opt-in only — DMCA & privacy "
            "respected.\n\n"
            "📺 Official live TV + 🔍 library search live in the dashboard.\n\n"
        )
        welcome = (
            intro + "👉 Try it now: send a link, or open the dashboard below.  "
            "Type /help any time."
        ) if kb else (
            intro + "👉 Try it now: just send a link.  Type /help any time."
        )
        await update.message.reply_text(welcome, reply_markup=kb)

    async def handle_regenerate_token(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        """Force-rotate the user's pending approval code. Owner-bypasses
        (they don't need a code) and approved users get told they're already in."""
        chat_id = update.effective_chat.id
        u = update.effective_user
        name = (u.first_name if u else None) or "there"
        if _auth.is_owner(chat_id):
            await update.message.reply_text("You're the owner — no code needed.")
            return
        # Refresh contact info + (if pending) rotate via record_interaction
        # path only after we know status. Use rotate_pending_code directly:
        row = await _db_users.rotate_pending_code(chat_id)
        if row is None:
            # Either active or banned (rotate_pending_code only matches pending).
            decision = await _auth.classify(chat_id)
            if decision == "allow":
                await update.message.reply_text("You're already approved — no code needed.")
            elif decision == "deny_banned":
                await update.message.reply_text("⛔ Your access to this bot has been revoked.")
            else:
                # Unknown user (never /started). Suggest the right entrypoint.
                await update.message.reply_text("Send /start first.")
            return
        sent = await update.message.reply_markdown(_pending_welcome_text(name, row))
        if sent and getattr(sent, "message_id", None):
            asyncio.create_task(_schedule_code_expiry_edit(
                ctx.bot, chat_id, sent.message_id, get_lang(chat_id),
            ))

    async def handle_dashboard(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        """Open the Mini App dashboard. Requires WEBAPP_URL env var (or
        webapp_url config key)."""
        await _record_user(update)
        chat_id = update.effective_chat.id
        if not await _auth.is_authorized(chat_id):
            return
        lang = get_lang(chat_id)
        kb = _dashboard_keyboard()
        if kb is None:
            await update.message.reply_text(
                "Mini App URL not configured. Set WEBAPP_URL env var "
                "(e.g. https://media.az-sentinel.xyz/app) and restart the container."
            )
            return
        await update.message.reply_text(
            "Tap below to open the SM-DL dashboard inside Telegram:",
            reply_markup=kb,
        )

    async def handle_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        """Rich 'what can I do' overview + command list + the compliance line.
        Plain text (no markdown) so command names render/link cleanly."""
        await _record_user(update)
        text = (
            "🤖 SMDL — what I can do\n\n"
            "📥 Download — send a public link (YouTube, TikTok, Instagram, "
            "Twitch, X…) and I'll grab it in top quality.\n"
            "🎬 Stickers — send a photo, video, GIF or circle and I'll turn it "
            "into a Telegram sticker, added to your pack ( /pack for the link ).\n"
            "🔴 Auto-record — /watch a Twitch / YouTube / Kick streamer and I'll "
            "capture their public live streams. Opt-in only — DMCA & privacy "
            "respected; /unwatch any time.\n"
            "📺 Live TV — official broadcaster channels, in the dashboard.\n"
            "🔍 Search — find anything you've saved, in the dashboard.\n\n"
            "Commands: /dashboard /stickers /pack /watchlist /watch /unwatch "
            "/live_status /storage_stats /language /delete_data\n\n"
            "SMDL only handles publicly-available content, honours opt-outs & "
            "DMCA, and never bypasses DRM or platform access controls."
        )
        await update.message.reply_text(text, reply_markup=_start_keyboard())

    # ── Sticker maker ──────────────────────────────────────────────────────
    def _stickers_url() -> str | None:
        """Open the unified Mini App at its Stickers tab. WEBAPP_URL is the
        /app route; we deep-link via `?tab=stickers` (the SPA reads the
        query param on boot — see miniapp.py). The standalone /stickers
        page was folded into /app 2026-06-01 — this used to point there.
        Returns None if WEBAPP_URL isn't configured."""
        base = os.environ.get("WEBAPP_URL", "").strip().rstrip("/")
        if not base:
            return None
        # Normalise to .../app (don't double-suffix if already /app).
        if not base.endswith("/app"):
            base = f"{base}/app"
        return f"{base}?tab=stickers"

    def _sticker_keyboard() -> InlineKeyboardMarkup | None:
        url = _stickers_url()
        if not url:
            return None
        return InlineKeyboardMarkup([[
            InlineKeyboardButton("🎬 Open sticker editor", web_app=WebAppInfo(url=url))
        ]])

    def _shape_keyboard(draft_id: int) -> InlineKeyboardMarkup:
        """Cut-out + shape re-make buttons for a photo sticker. Each posts a
        callback that re-makes the static sticker transparent in that shape."""
        cb = lambda s: f"stkshape:{draft_id}:{s}"
        rows = [
            [InlineKeyboardButton("✂️ Cut out", callback_data=cb("cutout")),
             InlineKeyboardButton("◯ Circle", callback_data=cb("circle")),
             InlineKeyboardButton("△ Triangle", callback_data=cb("triangle"))],
            [InlineKeyboardButton("⭐ Star", callback_data=cb("star")),
             InlineKeyboardButton("♥ Heart", callback_data=cb("heart")),
             InlineKeyboardButton("◆ Diamond", callback_data=cb("diamond"))],
        ]
        su = _stickers_url()
        if su:
            rows.append([InlineKeyboardButton("🎬 Editor (crop / custom)",
                                              web_app=WebAppInfo(url=su))])
        return InlineKeyboardMarkup(rows)

    async def handle_shape_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        """Re-make a photo draft as a transparent cut-out / shaped static
        sticker and add it to the user's image pack."""
        from pathlib import Path as _P
        from . import database as _sdb, sticker_processor as _sp
        from . import sticker_telegram as _st
        from .sticker_routes import OUTPUT_DIR as _OUT

        q = update.callback_query
        data = (q.data or "") if q else ""
        try:
            _, draft_id_s, shape = data.split(":", 2)
            draft_id = int(draft_id_s)
        except Exception:
            if q: await q.answer()
            return
        user = update.effective_user
        if not user or not await _auth.is_authorized(user.id):
            await q.answer("Not authorized.", show_alert=True); return
        await q.answer("Working on it…")

        draft = await _sdb.sticker_draft_get(draft_id, user.id)
        if not draft:
            await q.answer("That draft expired — resend the photo.", show_alert=True); return
        src = _P(draft["file_path"])
        if not src.exists():
            await q.answer("Source is gone — resend the photo.", show_alert=True); return

        udir = _OUT / str(user.id); udir.mkdir(parents=True, exist_ok=True)
        dst = udir / f"{draft_id}_{shape}.webp"
        try:
            if shape == "cutout":
                cut = udir / f"{draft_id}_cut.png"
                ok, err = await _sp.bg_remove(src, cut)
                if ok:
                    ok, err = await _sp.make_static_sticker(cut, dst, keep_alpha=True)
            else:
                mask = _sp.build_shape_mask(shape, udir / f"{draft_id}_{shape}_m.png")
                if mask is None:
                    await q.answer("Unknown shape."); return
                ok, err = await _sp.make_static_sticker(src, dst, mask=mask)
        except Exception as e:
            logger.warning("shape make crashed (u=%s d=%s s=%s): %s", user.id, draft_id, shape, e)
            await q.answer(f"Failed: {str(e)[:150]}", show_alert=True); return
        if not ok:
            await q.answer(f"Couldn't make it: {(err or '')[:150]}", show_alert=True); return

        pack = await _st.resolve_pack(ctx.bot, user.id, user.first_name, kind="static")
        try:
            file_id, set_url = await _st.upload_and_add(
                ctx.bot, user.id, dst, emoji="🖼️",
                pack_name=pack["pack_name"], pack_title=pack["pack_title"],
                sticker_format="static",
            )
        except Exception as e:
            await q.answer(f"Telegram rejected it: {str(e)[:140]}", show_alert=True); return
        if not pack.get("exists_in_db"):
            await _sdb.sticker_pack_create(user.id, pack["pack_name"],
                                           pack["pack_title"], set_url, kind="static")
        await _sdb.sticker_record(user_id=user.id, pack_name=pack["pack_name"],
                                  source_draft_id=draft_id, emoji="🖼️",
                                  telegram_file_id=file_id, webm_path=str(dst))
        try:
            await ctx.bot.send_sticker(chat_id=user.id, sticker=file_id)
        except Exception:
            pass
        label = {"cutout": "Cut-out", "circle": "Circle", "triangle": "Triangle",
                 "star": "Star", "heart": "Heart", "diamond": "Diamond"}.get(shape, shape)
        await q.answer(f"{label} sticker added ✨")

    async def handle_sticker_source(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        """Catch any sticker-able media — video / GIF / animation / round
        video-note (circle) → a VIDEO sticker (.webm); photo / image document
        → a STATIC sticker (.webp). Stores a draft (6h TTL), instant-makes the
        sticker, sends it back, and adds it to the user's pack of the right
        kind. Falls back to the MiniApp editor only if encode/upload fails."""
        import uuid as _uuid
        from pathlib import Path as _Path
        from . import database as _stickers_db
        from . import sticker_processor as _sp

        msg = update.effective_message
        chat = update.effective_chat
        if not msg or not chat:
            return
        # Sticker drafts are user-personal — keep groups out of this.
        if chat.type != "private":
            return
        user = update.effective_user
        if not user:
            return
        chat_id = chat.id

        # Record contact info + auth gate (same gate as text downloads).
        try:
            await _db_users.record_interaction(
                chat_id,
                username=user.username, first_name=user.first_name,
                last_name=user.last_name,
            )
        except Exception: pass
        if not await _auth.is_authorized(chat_id):
            return

        # Identify the payload object + mime + sticker KIND.
        #   video / animation / video-note / video-doc / GIF → "video" (.webm)
        #   photo / image-doc                                → "static" (.webp)
        obj = None
        mime = "video/mp4"
        kind = "video"
        is_circle = False
        if msg.video:
            obj = msg.video; mime = obj.mime_type or "video/mp4"
        elif msg.animation:
            obj = msg.animation; mime = obj.mime_type or "video/mp4"
        elif msg.video_note:                       # round video message (circle)
            obj = msg.video_note; mime = "video/mp4"; is_circle = True
        elif msg.document and (msg.document.mime_type or "").startswith("video/"):
            obj = msg.document; mime = msg.document.mime_type or "video/mp4"
        elif msg.document and (msg.document.mime_type or "") == "image/gif":
            obj = msg.document; mime = "image/gif"      # animated GIF → video sticker
        elif msg.photo:                            # compressed photo — largest size
            obj = msg.photo[-1]; mime = "image/jpeg"; kind = "static"
        elif msg.document and (msg.document.mime_type or "").startswith("image/"):
            obj = msg.document
            mime = msg.document.mime_type or "image/png"; kind = "static"
        else:
            return  # nothing sticker-able — ignore (text handler covers URLs)

        # Bot API caps inbound downloads at 20 MB. Reject loudly so the user
        # isn't left wondering why nothing happened.
        if obj.file_size and obj.file_size > 20 * 1024 * 1024:
            await msg.reply_text(
                "⚠ That clip is over 20 MB — Telegram won't let bots fetch "
                "files that large. Trim it down and resend."
            )
            return

        ext_guess = {
            "video/mp4":   ".mp4",
            "video/webm":  ".webm",
            "image/gif":   ".gif",
            "video/quicktime": ".mov",
            "image/jpeg":  ".jpg",
            "image/png":   ".png",
            "image/webp":  ".webp",
        }.get(mime, ".jpg" if kind == "static" else ".mp4")

        from .sticker_routes import DRAFTS_DIR
        udir = DRAFTS_DIR / str(user.id)
        udir.mkdir(parents=True, exist_ok=True)
        tmp_path = udir / f"tmp_{_uuid.uuid4().hex}{ext_guess}"

        try:
            tg_file = await ctx.bot.get_file(obj.file_id)
            await tg_file.download_to_drive(custom_path=str(tmp_path))
        except Exception as e:
            logger.warning("sticker draft download failed: %s", e)
            await msg.reply_text(f"⚠ Couldn't fetch that file: {e}")
            return

        # Probe for duration + dims (best-effort).
        meta = await _sp.probe(_Path(tmp_path))
        dur = meta.get("duration_s") or getattr(obj, "duration", None)
        try: dur = float(dur) if dur is not None else None
        except Exception: dur = None
        width  = meta.get("width")  or getattr(obj, "width", None)
        height = meta.get("height") or getattr(obj, "height", None)

        # Reject pathologically short clips — ffmpeg can't produce a useful
        # sticker. Static images have no duration, so this is video-only.
        if kind == "video" and dur is not None and dur < 0.3:
            try: _Path(tmp_path).unlink(missing_ok=True)
            except Exception: pass
            await msg.reply_text("⚠ Clip is too short (< 0.3 s).")
            return

        try:
            draft_id = await _stickers_db.sticker_draft_insert(
                user_id=user.id,
                telegram_file_id=obj.file_id,
                file_path=str(tmp_path),
                mime_type=mime,
                duration_s=dur,
                width=int(width) if width else None,
                height=int(height) if height else None,
            )
        except Exception as e:
            logger.exception("sticker draft insert failed")
            try: _Path(tmp_path).unlink(missing_ok=True)
            except Exception: pass
            await msg.reply_text(f"⚠ Storage error: {e}")
            return

        # Instant make — mirrors the Mini App's default mode. Encode with sane
        # defaults (centre-crop-to-fill; first 3s for video), push to the user's
        # pack of the right kind, send the sticker back as confirmation, and drop
        # the draft. Falls back to the editor button only if encode/upload fails.
        from . import sticker_telegram as _st
        from .sticker_routes import OUTPUT_DIR as _OUT
        if kind == "static":
            out_ext, emoji, sticker_format = ".webp", "🖼️", "static"
        else:
            out_ext, emoji, sticker_format = ".webm", "🎬", "video"
        instant_ok = False
        instant_err: str | None = None
        try:
            dst = _OUT / str(user.id) / f"{draft_id}{out_ext}"
            if kind == "static":
                ok, err = await _sp.make_static_sticker(_Path(tmp_path), dst, crop=None)
            else:
                ok, err = await _sp.make_video_sticker(
                    _Path(tmp_path), dst, start=0.0, end=3.0, crop=None,
                    circle=is_circle,   # round video-notes → transparent corners
                )
            if not ok:
                instant_err = err or "encode failed"
            else:
                pack = await _st.resolve_pack(
                    ctx.bot, user.id, user.first_name, kind=kind,
                )
                try:
                    file_id, set_url = await _st.upload_and_add(
                        ctx.bot, user.id, dst,
                        emoji=emoji,
                        pack_name=pack["pack_name"],
                        pack_title=pack["pack_title"],
                        sticker_format=sticker_format,
                        sticker_type="regular",
                    )
                except Exception as e:
                    instant_err = str(e)
                else:
                    if not pack.get("exists_in_db"):
                        await _stickers_db.sticker_pack_create(
                            user.id, pack["pack_name"], pack["pack_title"],
                            set_url, kind=kind,
                        )
                    await _stickers_db.sticker_record(
                        user_id=user.id, pack_name=pack["pack_name"],
                        source_draft_id=draft_id, emoji=emoji,
                        telegram_file_id=file_id, webm_path=str(dst),
                    )
                    # Send the sticker back as confirmation + the pack link.
                    try:
                        await ctx.bot.send_sticker(chat_id=user.id, sticker=file_id)
                    except Exception as _e:
                        logger.debug("instant confirm sticker send failed: %s", _e)
                    if kind == "static":
                        await msg.reply_text(
                            f"✨ Added to your pack:\n{set_url}\n\n"
                            "Want a different look? Tap a cut-out or shape:",
                            reply_markup=_shape_keyboard(draft_id),
                            disable_web_page_preview=True,
                        )
                    else:
                        await msg.reply_text(
                            f"✨ Added to your pack:\n{set_url}\n\n"
                            "Tip: open the Mini App to re-crop, change emoji, or "
                            "trim a different window.",
                            reply_markup=_sticker_keyboard(),
                            disable_web_page_preview=True,
                        )
                    instant_ok = True
                    # Video: drop the draft. Static: KEEP it (6h TTL) so the
                    # cut-out / shape buttons can re-make from the same source.
                    if kind != "static":
                        try:
                            path_str = await _stickers_db.sticker_draft_delete(draft_id, user.id)
                            if path_str:
                                _Path(path_str).unlink(missing_ok=True)
                        except Exception:
                            pass
        except Exception as e:
            logger.warning("bot instant-make crashed for u=%s d=%s: %s",
                           user.id, draft_id, e)
            instant_err = f"{type(e).__name__}: {e}"

        if instant_ok:
            return  # all done

        # Fallback path: instant make couldn't finish (encode bailed, TG
        # error, etc.). Keep the draft and offer the editor as before so
        # the user can salvage it manually.
        kb = _sticker_keyboard()
        prefix = f"{emoji} Got it ({dur:.1f}s)" if dur else f"{emoji} Got it"
        if instant_err:
            body = (
                f"{prefix} — auto-make couldn't finish ({instant_err[:120]}). "
                "Open the MiniApp to crop / trim / re-emoji manually.\n\n"
                "Drafts auto-delete after 6 hours."
            )
        else:
            body = (
                f"{prefix} — open the MiniApp to crop, trim, pick an emoji, "
                "and add it to your pack.\n\nDrafts auto-delete after 6 hours."
            )
        await msg.reply_text(body, reply_markup=kb)

    async def handle_stickers_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        """Open the sticker MiniApp."""
        chat_id = update.effective_chat.id
        if not await _auth.is_authorized(chat_id):
            return
        kb = _sticker_keyboard()
        if kb is None:
            await update.message.reply_text(
                "Sticker MiniApp URL not configured (set WEBAPP_URL)."
            )
            return
        await update.message.reply_text(
            "Send me a video or GIF to start, then tap below:",
            reply_markup=kb,
        )

    async def handle_pack_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        """Show the user's existing pack URL (or tell them they don't have one)."""
        from . import database as _stickers_db
        user = update.effective_user
        chat_id = update.effective_chat.id
        if not user or not await _auth.is_authorized(chat_id):
            return
        pack = await _stickers_db.sticker_pack_get(user.id)
        if not pack:
            await update.message.reply_text(
                "You don't have a sticker pack yet. Send me a video or GIF and "
                "make one with /stickers."
            )
            return
        await update.message.reply_text(
            f"📦 {pack['pack_title']}\n{pack['telegram_url']}",
            disable_web_page_preview=False,
        )

    async def handle_delete_data_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        """Wipe all sticker drafts + intermediate files for this user.
        Doesn't touch published stickers — those live in Telegram's CDN."""
        from . import database as _stickers_db
        from pathlib import Path as _Path
        from .sticker_routes import DRAFTS_DIR, OUTPUT_DIR
        user = update.effective_user
        chat_id = update.effective_chat.id
        if not user or not await _auth.is_authorized(chat_id):
            return

        paths = await _stickers_db.sticker_drafts_delete_all(user.id)
        removed = 0
        for p in paths:
            try:
                _Path(p).unlink(missing_ok=True); removed += 1
            except Exception: pass
        # Also wipe any encoded outputs for this user.
        odir = OUTPUT_DIR / str(user.id)
        if odir.exists():
            for f in odir.iterdir():
                try: f.unlink(missing_ok=True)
                except Exception: pass
        await update.message.reply_text(
            f"🗑 Deleted {removed} draft{'s' if removed != 1 else ''}. "
            "Already-published stickers in your Telegram pack are unaffected."
        )

    _app.add_handler(CommandHandler("stickers",    handle_stickers_cmd))
    _app.add_handler(CommandHandler("pack",        handle_pack_cmd))
    _app.add_handler(CommandHandler("delete_data", handle_delete_data_cmd))
    _app.add_handler(MessageHandler(
        (filters.VIDEO | filters.ANIMATION | filters.VIDEO_NOTE | filters.PHOTO
         | filters.Document.VIDEO | filters.Document.IMAGE
         | filters.Document.MimeType("image/gif"))
        & filters.ChatType.PRIVATE,
        handle_sticker_source,
    ))

    _app.add_handler(CommandHandler("start", handle_start))
    _app.add_handler(CommandHandler("help", handle_help))
    _app.add_handler(CallbackQueryHandler(handle_shape_callback, pattern=r"^stkshape:"))
    _app.add_handler(CommandHandler("regenerate_token", handle_regenerate_token))
    _app.add_handler(CommandHandler("dashboard", handle_dashboard))
    _app.add_handler(CommandHandler("app", handle_dashboard))   # alias
    _app.add_handler(CommandHandler("stop_livestream", handle_stop_livestream))
    _app.add_handler(CommandHandler("stop_livestream_download", handle_stop_livestream))  # alias matching user phrasing
    _app.add_handler(CommandHandler("live_status", handle_live_status))
    _app.add_handler(CommandHandler("watch", handle_watch))
    _app.add_handler(CommandHandler("unwatch", handle_unwatch))
    _app.add_handler(CommandHandler("watchlist", handle_watchlist))
    _app.add_handler(CommandHandler("scrape_add", handle_scrape_add))
    _app.add_handler(CommandHandler("scrape_remove", handle_scrape_remove))
    _app.add_handler(CommandHandler("scrape_list", handle_scrape_list))
    _app.add_handler(CommandHandler("scrape_pause", handle_scrape_pause))
    _app.add_handler(CommandHandler("scrape_resume", handle_scrape_resume))
    _app.add_handler(CommandHandler("scrape_now", handle_scrape_now))
    _app.add_handler(CommandHandler("language", handle_language))
    _app.add_handler(CommandHandler("timezone", handle_timezone))
    _app.add_handler(CommandHandler("transcode", handle_transcode))
    _app.add_handler(CommandHandler("default_video_size", handle_default_video_size))
    _app.add_handler(CommandHandler("storage_stats", handle_storage_stats))
    _app.add_handler(CommandHandler("clear_cache", handle_clear_cache))
    _app.add_handler(CallbackQueryHandler(handle_monitor_callback, pattern=r"^mon:"))
    _app.add_handler(CallbackQueryHandler(handle_language_callback, pattern=r"^lang:"))
    _app.add_handler(CallbackQueryHandler(handle_transcode_callback, pattern=r"^tc:"))
    _app.add_handler(CallbackQueryHandler(handle_default_video_size_callback, pattern=r"^vq:"))
    _app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    return _app


# ── Bot-presence wiring: commands, menu button, descriptions ───────────────
# Idempotent — TG upserts. Called from main.py after `start()` on every boot
# so a fresh container picks up the right presentation without anyone
# touching BotFather. Failures here are non-fatal: bot keeps working even
# if a meta field doesn't take.

_BOT_DESC = {
    None: (
        "📥 SMDL — your all-in-one media bot.\n\n"
        "• Paste a public link (YouTube, TikTok, Instagram, Twitch, X…) → "
        "I download it.\n"
        "• Send a photo, video, GIF or circle → an instant Telegram sticker, "
        "added to your pack.\n"
        "• 🔴 Auto-record opted-in Twitch / YouTube / Kick streamers' public "
        "live streams — DMCA & privacy respected.\n"
        "• 📺 Official live TV channels + 🔍 search your library — in the "
        "Mini App.\n\n"
        "Tap \"Open SMDL\" or send a link to start."
    ),
    "ru": (
        "📥 SMDL — ваш универсальный медиа-бот.\n\n"
        "• Пришлите публичную ссылку (YouTube, TikTok, Instagram, Twitch, X…) "
        "→ я скачаю.\n"
        "• Фото, видео, GIF или кружок → мгновенный Telegram-стикер в ваш пак.\n"
        "• 🔴 Авто-запись публичных эфиров согласившихся стримеров Twitch / "
        "YouTube / Kick — с учётом DMCA и приватности.\n"
        "• 📺 Официальные ТВ-каналы + 🔍 поиск по библиотеке — в Mini App.\n\n"
        "Нажмите «Open SMDL» или пришлите ссылку."
    ),
}
_BOT_SHORT_DESC = {
    None: "Download videos, make stickers, auto-record opted-in live streams. Tap Open.",
    "ru": "Скачивайте видео, делайте стикеры, авто-запись согласившихся эфиров. Open.",
}
_BOT_COMMANDS = {
    None: [
        ("start",         "Welcome + dashboard button"),
        ("help",          "What SMDL can do + all commands"),
        ("stickers",      "Open the sticker maker Mini App"),
        ("pack",          "Show your sticker pack link"),
        ("dashboard",     "Open the SMDL Mini App"),
        ("watchlist",     "Auto-record streamers (twitch / youtube / kick)"),
        ("watch",         "Add a streamer to the watchlist"),
        ("unwatch",       "Remove a streamer from the watchlist"),
        ("live_status",   "Status of any in-progress live recording"),
        ("storage_stats", "Disk usage on the box"),
        ("language",      "Switch reply language"),
        ("delete_data",   "Wipe all your data on this bot"),
    ],
    "ru": [
        ("start",         "Приветствие + кнопка панели"),
        ("help",          "Что умеет SMDL + все команды"),
        ("stickers",      "Открыть Mini App для стикеров"),
        ("pack",          "Показать ссылку на ваш стикерпак"),
        ("dashboard",     "Открыть SMDL Mini App"),
        ("watchlist",     "Авто-запись стримеров (twitch / youtube / kick)"),
        ("watch",         "Добавить стримера в список наблюдения"),
        ("unwatch",       "Убрать стримера из списка наблюдения"),
        ("live_status",   "Статус текущей записи стрима"),
        ("storage_stats", "Использование диска на сервере"),
        ("language",      "Сменить язык ответов"),
        ("delete_data",   "Удалить все мои данные у бота"),
    ],
}


async def wire_bot_presence(app: Application) -> None:
    """One-shot at boot: push commands, menu button, descriptions to TG.
    Each call is independent — a single failure doesn't block the rest."""
    from telegram import BotCommand, MenuButtonWebApp, WebAppInfo

    # Commands per language. The None entry is the default (matches users
    # whose TG client language isn't ru); language_code="ru" attaches the
    # localised list. Add more languages by extending _BOT_COMMANDS.
    for lang, cmds in _BOT_COMMANDS.items():
        try:
            kwargs = {"commands": [BotCommand(c, d) for c, d in cmds]}
            if lang:
                kwargs["language_code"] = lang
            await app.bot.set_my_commands(**kwargs)
        except Exception as e:
            logger.warning("set_my_commands lang=%s failed: %s", lang, e)

    webapp_url = (os.environ.get("WEBAPP_URL") or "").strip()
    if webapp_url:
        try:
            await app.bot.set_chat_menu_button(
                menu_button=MenuButtonWebApp(
                    text="📱 Open SMDL",
                    web_app=WebAppInfo(url=webapp_url),
                ),
            )
        except Exception as e:
            logger.warning("set_chat_menu_button failed: %s", e)

    for lang, desc in _BOT_DESC.items():
        try:
            kwargs = {"description": desc}
            if lang:
                kwargs["language_code"] = lang
            await app.bot.set_my_description(**kwargs)
        except Exception as e:
            logger.warning("set_my_description lang=%s failed: %s", lang, e)

    for lang, sdesc in _BOT_SHORT_DESC.items():
        try:
            kwargs = {"short_description": sdesc}
            if lang:
                kwargs["language_code"] = lang
            await app.bot.set_my_short_description(**kwargs)
        except Exception as e:
            logger.warning("set_my_short_description lang=%s failed: %s", lang, e)

    logger.info("bot presence wired (commands + menu + descriptions, langs=%s)",
                list(_BOT_COMMANDS.keys()))
