"""SM-DL service — FastAPI health endpoint + Telegram bot lifecycle."""

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from . import database as db
from . import file_serve
from . import interceptor  # noqa: F401 — triggers plugin auto-load at startup
from . import stream_monitor
from .bot import build
from .downloader import start_cleanup_loop

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await db.init_db()
    asyncio.create_task(start_cleanup_loop())

    # Bot initialization is best-effort. A bad/missing token must NOT crash
    # the FastAPI lifespan — keep the /health endpoint up so the operator
    # can curl it, check container logs, and fix the token without
    # container-crashloop noise. Same logic helps the fresh-install test
    # spin up without a real BotFather token.
    tg_app = None
    polling_task = None
    monitor_task = None
    try:
        tg_app = await build()
        await tg_app.initialize()
        await tg_app.start()
        polling_task = asyncio.create_task(
            tg_app.updater.start_polling(drop_pending_updates=True)
        )
        monitor_task = asyncio.create_task(stream_monitor.monitor_loop(tg_app))

        def _on_task_done(t: asyncio.Task):
            if not t.cancelled() and t.exception():
                logger.error("Background task crashed: %s", t.exception(), exc_info=t.exception())

        polling_task.add_done_callback(_on_task_done)
        monitor_task.add_done_callback(_on_task_done)
        logger.info("SM-DL bot polling started + stream monitor running")
    except Exception as e:
        logger.error(
            "Bot startup failed (%s: %s). FastAPI continues running for "
            "diagnostics — /health endpoint stays up, but Telegram features "
            "are unavailable until the underlying issue is fixed.",
            type(e).__name__, e,
        )

    yield

    if polling_task and not polling_task.done():
        polling_task.cancel()
    if monitor_task and not monitor_task.done():
        monitor_task.cancel()
    if tg_app is not None:
        try:
            await tg_app.updater.stop()
            await tg_app.stop()
            await tg_app.shutdown()
        except Exception as e:
            logger.warning("bot shutdown raised: %s", e)
    logger.info("SM-DL bot shut down")


app = FastAPI(title="SM-DL — Social Media Downloader", lifespan=lifespan)
app.include_router(file_serve.router)


@app.get("/health")
async def health():
    return {"status": "ok", "service": "sm-dl"}
