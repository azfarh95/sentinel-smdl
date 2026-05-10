"""SM-DL service — FastAPI health endpoint + Telegram bot lifecycle."""

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from . import database as db
from . import file_serve
from . import stream_monitor
from . import stripchat_patch  # noqa: F401  — applies extractor monkey-patch on import
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
    yield
    polling_task.cancel()
    monitor_task.cancel()
    await tg_app.updater.stop()
    await tg_app.stop()
    await tg_app.shutdown()
    logger.info("SM-DL bot shut down")


app = FastAPI(title="SM-DL — Social Media Downloader", lifespan=lifespan)
app.include_router(file_serve.router)


@app.get("/health")
async def health():
    return {"status": "ok", "service": "sm-dl"}
