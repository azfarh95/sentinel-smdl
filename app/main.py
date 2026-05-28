"""SM-DL service — FastAPI health endpoint + Telegram bot lifecycle."""

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from . import database as db
from . import file_serve
from . import interceptor  # noqa: F401 — triggers plugin auto-load at startup
from . import iptv
from . import iptv_routes
from . import miniapp
from . import profile_monitor
from . import sticker_routes
from . import stream_monitor
from .bot import build
from .downloader import start_cleanup_loop
from .sticker_routes import start_cleanup_loop as start_sticker_cleanup_loop

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await db.init_db()
    await iptv.init_iptv_schema()

    # Theater P4 — queue table + background worker. The worker pops
    # queued rows from stremio_jobs and runs resolve → stream → cache.
    try:
        from . import stremio_queue as _sq
        from . import stremio_settings as _ss
        await _sq.init_schema()
        await _ss.init_schema()
        _sq.start_worker()
        logger.info("Theater queue worker started (max_concurrent=%d)", _sq.MAX_CONCURRENT)
    except Exception as e:
        logger.warning("Theater queue startup failed: %s", e)
    # First-boot: default-block adult cam platforms so they don't appear in
    # non-owner UX. Owner can flip them back on in Admin → Sites.
    try:
        from . import auth as _auth
        if await _auth.seed_default_blocklist_if_unset():
            logger.info("Seeded default site blocklist: %s",
                        _auth.DEFAULT_BLOCKED_PLATFORMS)
    except Exception as _e:
        logger.warning("seed_default_blocklist_if_unset failed: %s", _e)

    # Self-heal: an earlier release (pre-fix) inserted the owner's row as
    # 'pending'. Flip any owner row back to active so it stops showing in
    # the Admin → Pending list. Also drop any pending row whose chat_id is
    # negative — those are Telegram group chats wrongly recorded by an
    # earlier bug.
    try:
        from .config import OWNER_CHAT_ID
        import aiosqlite as _aio
        async with _aio.connect(db.DB_PATH) as conn:
            if OWNER_CHAT_ID is not None:
                cur = await conn.execute(
                    "UPDATE users SET status='active', pending_code=NULL, "
                    "pending_expires_at=NULL "
                    "WHERE chat_id = ? AND status = 'pending'",
                    (int(OWNER_CHAT_ID),),
                )
                if cur.rowcount:
                    logger.info("Healed owner row: flipped %d pending row(s) to active", cur.rowcount)
            cur = await conn.execute("DELETE FROM users WHERE chat_id < 0")
            if cur.rowcount:
                logger.info("Cleaned up %d group-chat user rows (chat_id < 0)", cur.rowcount)
            await conn.commit()
    except Exception as _e:
        logger.warning("user-row self-heal failed: %s", _e)
    asyncio.create_task(start_cleanup_loop())
    asyncio.create_task(start_sticker_cleanup_loop())
    # IPTV auto-probe — ticks every 12h with a 10h fresh-skip window so
    # we get one real sweep per day + cheap "skip" calls in between.
    iptv_auto_task = asyncio.create_task(iptv.auto_probe_loop())
    iptv_auto_task.add_done_callback(
        lambda t: t.cancelled() or t.exception() and
        logger.error("IPTV auto-probe loop crashed: %s", t.exception(), exc_info=t.exception())
    )
    # IPTV scheduled-DVR ticker — wakes every 60s, fires pending records.
    try:
        from . import iptv_routes as _iptv_routes
        await _iptv_routes.start_scheduler_loop()
    except Exception as _e:
        logger.warning("IPTV scheduled-DVR loop failed to start: %s", _e)

    # Bot initialization runs in a background task with exponential backoff
    # so a transient network blip at startup (e.g. PIA VPN not yet up, DNS
    # not yet resolving api.telegram.org) doesn't permanently kill the bot
    # until the next container restart. The FastAPI lifespan completes
    # immediately — /health stays up — and the bot retries until it
    # connects. Real misconfigurations (bad token) eventually surface as
    # 401 Unauthorized from Telegram, which we log but keep retrying for
    # in case it's a transient credential reload; a hard exit would just
    # hide the issue.
    state: dict = {
        "tg_app": None,
        "polling_task": None,
        "monitor_task": None,
        "scraper_task": None,
        "ready": False,
        "last_error": None,
    }
    init_task = asyncio.create_task(_init_bot_with_retry(state))
    app.state.bot_state = state

    yield

    init_task.cancel()
    polling_task = state.get("polling_task")
    monitor_task = state.get("monitor_task")
    scraper_task = state.get("scraper_task")
    tg_app       = state.get("tg_app")
    if polling_task and not polling_task.done():
        polling_task.cancel()
    if monitor_task and not monitor_task.done():
        monitor_task.cancel()
    if scraper_task and not scraper_task.done():
        scraper_task.cancel()
    if tg_app is not None:
        try:
            await tg_app.updater.stop()
            await tg_app.stop()
            await tg_app.shutdown()
        except Exception as e:
            logger.warning("bot shutdown raised: %s", e)
    logger.info("SM-DL bot shut down")


async def _init_bot_with_retry(state: dict) -> None:
    """Build + start the Telegram bot. Retry with exponential backoff on
    failure (2s, 4s, 8s, … capped at 60s). Loops forever until success or
    the task is cancelled at shutdown.

    Records progress on the shared `state` dict so the lifespan's shutdown
    path can clean up whatever made it through. `state['ready']` flips True
    once the bot is fully running; before that, /health/bot reports the
    last error so the operator can see why.
    """
    delay = 2.0
    attempt = 0
    while True:
        attempt += 1
        try:
            tg_app = await build()
            await tg_app.initialize()
            await tg_app.start()
            polling_task = asyncio.create_task(
                tg_app.updater.start_polling(drop_pending_updates=True)
            )
            monitor_task = asyncio.create_task(stream_monitor.monitor_loop(tg_app))
            scraper_task = asyncio.create_task(profile_monitor.scraper_loop(tg_app))

            def _on_task_done(t: asyncio.Task):
                if not t.cancelled() and t.exception():
                    logger.error("Background task crashed: %s", t.exception(), exc_info=t.exception())

            polling_task.add_done_callback(_on_task_done)
            monitor_task.add_done_callback(_on_task_done)
            scraper_task.add_done_callback(_on_task_done)

            state["tg_app"] = tg_app
            state["polling_task"] = polling_task
            state["monitor_task"] = monitor_task
            state["scraper_task"] = scraper_task
            state["ready"] = True
            state["last_error"] = None
            if attempt == 1:
                logger.info("SM-DL bot polling started + stream monitor + profile scraper running")
            else:
                logger.info("SM-DL bot polling started on attempt %d (after retries)", attempt)
            return
        except asyncio.CancelledError:
            raise
        except Exception as e:
            state["last_error"] = f"{type(e).__name__}: {e}"
            logger.error(
                "Bot startup attempt %d failed (%s: %s) — retrying in %.0fs",
                attempt, type(e).__name__, e, delay,
            )
            try:
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                raise
            delay = min(delay * 2, 60.0)


app = FastAPI(title="SM-DL — Social Media Downloader", lifespan=lifespan)
app.include_router(file_serve.router)
app.include_router(miniapp.router)
app.include_router(sticker_routes.router)
app.include_router(iptv_routes.router)


@app.get("/health")
async def health():
    return {"status": "ok", "service": "sm-dl"}


@app.get("/health/bot")
async def health_bot():
    """Reports whether the Telegram bot polling actually came online.
    Useful diagnostic when /health is OK but messages get no reply
    (typically DNS to api.telegram.org failed at startup; the retry loop
    will eventually recover, but the operator can spot it sooner here).
    """
    state = getattr(app.state, "bot_state", None) or {}
    return {
        "ready":      bool(state.get("ready")),
        "last_error": state.get("last_error"),
    }
