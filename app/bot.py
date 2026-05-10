"""Telegram bot — detects video URLs, downloads, sends back. No AI involved."""

import asyncio
import logging
import os
from pathlib import Path

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from .config import ALLOWED_CHAT_IDS, DELETE_AFTER_SEND, LIVE_ENABLED, OWNER_CHAT_ID
from .downloader import download, identify_post, send_files, _resolve_cookies
from .interceptor import find_video_url
from .live_downloader import detect_live, record_live

# Active livestream recordings, keyed by chat_id.
# Each entry: {"stop_flag": {"stop": False}, "url": str, "started_at": float}
_active_live_jobs: dict[int, dict] = {}

logger = logging.getLogger(__name__)

SMDL_BOT_TOKEN = os.environ["SMDL_BOT_TOKEN"]

_app: Application | None = None


def get_application() -> Application:
    return _app


async def build() -> Application:
    global _app
    _app = Application.builder().token(SMDL_BOT_TOKEN).build()

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

        status_msg = await msg.reply_text(f"Identifying {platform} post...")

        try:
            info = await identify_post(url)
        except Exception as e:
            await status_msg.edit_text(f"Failed to identify post: {e}")
            return

        if info.get("error"):
            is_private = info.get("is_private", False)
            err_text = "Private account — cannot download." if is_private else f"Could not identify post: {info['error'][:200]}"
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
                await status_msg.edit_text(
                    f"{platform} · @{uploader} · 🔴 LIVE\n"
                    f"Live recording is disabled in config (live_enabled=false)."
                )
                return

            await status_msg.edit_text(
                f"{platform} · @{uploader} · 🔴 LIVE\n"
                f"Recording started — heartbeats every 5 min. Will auto-stop on stream end or session failure."
            )

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
                        f"🔴 Recording · @{uploader}\n"
                        f"⏱ {mins} min · 💾 {mb:.0f} MB · still live"
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

            if live_result["abort_reason"] == "stream_ended":
                summary = f"✓ Recording ended naturally · {mins} min · {mb:.0f} MB"
            elif live_result["abort_reason"] == "user_stopped":
                summary = f"⏹ Stopped by /stop_livestream · {mins} min · {mb:.0f} MB saved"
            elif live_result["abort_reason"] == "session_fail":
                summary = (
                    f"⚠ Session/auth failed at {mins} min · {mb:.0f} MB saved\n"
                    f"Cookie likely expired — refresh cookies and retry."
                )
            elif live_result["abort_reason"] == "platform_not_allowed":
                summary = f"⚠ {live_result['detail']}"
            elif live_result["abort_reason"] == "disk_low":
                summary = f"⚠ {live_result['detail']}"
            else:
                summary = f"⚠ Stopped: {live_result['abort_reason']} · {mins} min · {mb:.0f} MB · {live_result.get('detail', '')[:120]}"

            await status_msg.edit_text(summary)

            if files:
                # Files live in /downloads/live — too large for Telegram usually,
                # so just print path. User retrieves via OneDrive/file share.
                first = files[0]
                size_mb = round(Path(first).stat().st_size / 1024 / 1024, 1) if Path(first).exists() else 0
                if size_mb < 50:
                    await ctx.bot.send_video(chat_id=chat_id, video=open(first, "rb"),
                                             caption=info.get("title"),
                                             read_timeout=180, write_timeout=180)
                else:
                    await msg.reply_text(
                        f"📁 Saved to `{first}` ({size_mb} MB) — too big for Telegram inline send."
                    )
            return

        # ── Normal (non-live) download ─────────────────────────────────────────
        try:
            await status_msg.edit_text(
                f"{platform} · @{uploader} · {media_label}\nDownloading..."
            )

            result = await download(url, media_type=media_type, is_owner=is_owner)

            if result.get("error"):
                await status_msg.edit_text(f"Download failed: {result['error']}")
                return

            files = result["files"]
            cached = result.get("cached", False)
            file_count = len(files)
            title = info.get("title", "")

            await status_msg.edit_text(
                f"{'Cached · s' if cached else 'S'}ending {file_count} files..."
                if file_count > 1 else
                f"{'Cached · s' if cached else 'S'}ending {platform} {media_label}..."
            )

            send_result = await send_files(ctx.bot, chat_id, files, caption=title)

            if send_result.get("ok"):
                sent = send_result.get("count", 1)
                size = send_result.get("size_mb")
                cached_tag = " · cached" if cached else ""
                detail = f"{sent} file{'s' if sent > 1 else ''}" + (f" · {size} MB" if size else "") + cached_tag
                await status_msg.edit_text(f"Sent ({detail})")

                if DELETE_AFTER_SEND and not cached:
                    for fp in files:
                        try:
                            Path(fp).unlink()
                        except Exception:
                            pass
            elif send_result.get("error") == "file_too_large":
                await status_msg.edit_text(
                    f"File too large for Telegram ({send_result['size_mb']} MB). "
                    f"Saved locally at {files[0]}"
                )
            else:
                await status_msg.edit_text(f"Send failed: {send_result.get('error')}")

        except Exception as e:
            logger.exception("Download pipeline error")
            await status_msg.edit_text(f"Error: {e}")

    async def handle_stop_livestream(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        chat_id = update.effective_chat.id
        if ALLOWED_CHAT_IDS and chat_id not in ALLOWED_CHAT_IDS:
            return
        job = _active_live_jobs.get(chat_id)
        if not job:
            await update.message.reply_text("No active livestream recording in this chat.")
            return
        job["stop_flag"]["stop"] = True
        elapsed_min = int((__import__("time").time() - job["started_at"]) // 60)
        await update.message.reply_text(
            f"⏹ Stop requested for {job['platform']} · @{job['uploader']} "
            f"({elapsed_min} min in). Finalizing the file…"
        )

    async def handle_live_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        chat_id = update.effective_chat.id
        if ALLOWED_CHAT_IDS and chat_id not in ALLOWED_CHAT_IDS:
            return
        job = _active_live_jobs.get(chat_id)
        if not job:
            await update.message.reply_text("No active livestream recording.")
            return
        elapsed_min = int((__import__("time").time() - job["started_at"]) // 60)
        await update.message.reply_text(
            f"🔴 Recording · {job['platform']} · @{job['uploader']}\n"
            f"⏱ {elapsed_min} min · use /stop_livestream to halt"
        )

    _app.add_handler(CommandHandler("stop_livestream", handle_stop_livestream))
    _app.add_handler(CommandHandler("stop_livestream_download", handle_stop_livestream))  # alias matching user phrasing
    _app.add_handler(CommandHandler("live_status", handle_live_status))
    _app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    return _app
