"""SM-DL service — FastAPI health endpoint + Telegram bot lifecycle."""

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from . import database as db
from . import edition
from . import file_serve
from . import interceptor  # noqa: F401 — triggers plugin auto-load at startup
from . import auth_google
from . import auth_twitch
from . import iptv
from . import iptv_routes
from . import license_routes
from . import miniapp
from . import premium_routes
from . import profile_monitor
from . import pwa_routes
from . import sticker_routes
from . import cookie_routes
from . import streamer_consent
from . import stream_monitor
from .bot import build
from .downloader import start_cleanup_loop
from .sticker_routes import start_cleanup_loop as start_sticker_cleanup_loop

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


async def _warm_rembg():
    """Pre-load + optimise the u2netp session so the first background cutout
    (static webp or per-frame video) doesn't pay model-load on a user request.
    Best-effort, off the critical path."""
    try:
        from . import sticker_processor as _sp
        loop = asyncio.get_running_loop()

        def _warm():
            from PIL import Image
            from rembg import remove
            remove(Image.new("RGB", (64, 64), (128, 128, 128)),
                   session=_sp._rembg_session_get(),
                   only_mask=True, post_process_mask=False)

        await loop.run_in_executor(None, _warm)
        logger.info("rembg/u2netp session pre-warmed")
    except Exception as e:
        logger.warning("rembg pre-warm skipped: %s", e)


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
        # Follow-a-show (Sonarr-lite) — auto-grab newly-aired episodes of
        # followed series through this same queue. Depends on the queue worker.
        from . import follows as _f
        await _f.init_db()
        asyncio.create_task(_f.check_loop())
        logger.info("Follow-a-show loop started (interval=%ds, enabled=%s)",
                    _f.CHECK_INTERVAL_S, _f.ENABLED)
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
    # Sticker job queue (v2.7-B) — heavy encodes (per-frame cutout / transparent
    # / plain video) run on a small background worker so /make returns instantly;
    # the editor polls /api/sticker_jobs/{id} for progress + the user gets a
    # Telegram push on completion. Re-queues interrupted jobs on boot.
    try:
        from . import sticker_jobs as _sj
        await _sj.init_schema()
        _sj.start_worker()
        logger.info("Sticker job worker started (max_concurrent=%d)", _sj.MAX_CONCURRENT)
    except Exception as _e:
        logger.warning("Sticker job worker startup failed: %s", _e)
    asyncio.create_task(start_cleanup_loop())
    asyncio.create_task(start_sticker_cleanup_loop())
    asyncio.create_task(_warm_rembg())
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
    # v3.6-D rolling DVR buffer — wipe stale buffer dirs + start the idle GC.
    try:
        from . import iptv_dvr as _iptv_dvr
        _iptv_dvr.start_gc_loop()
    except Exception as _e:
        logger.warning("IPTV DVR buffer GC failed to start: %s", _e)

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
    # The Telegram operator bot only runs when a token is configured. The
    # public web/TWA build (community + play, TV surface) ships without one —
    # it's reached over HTTP, not Telegram — so we skip bot startup entirely
    # rather than loop forever on a missing-token error.
    from .bot import SMDL_BOT_TOKEN as _bot_token
    init_task = None
    if _bot_token:
        init_task = asyncio.create_task(_init_bot_with_retry(state))
    else:
        state["last_error"] = "disabled: no SMDL_BOT_TOKEN (web/TWA-only build)"
        logger.info("Telegram bot disabled (no SMDL_BOT_TOKEN) — serving web/TWA only")
    app.state.bot_state = state

    yield

    # v3.6-D — tear down any running DVR buffers (kill ffmpeg + clean dirs).
    try:
        from . import iptv_dvr as _iptv_dvr
        await _iptv_dvr.shutdown()
    except Exception:
        pass
    if init_task is not None:
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
            # Idempotent bot presence — commands, menu button, descriptions
            # (English + Russian). Run on every boot so a token rotation or
            # fresh deploy refreshes the bot's user-facing metadata without
            # operator intervention. Non-fatal on failure.
            try:
                from .bot import wire_bot_presence
                await wire_bot_presence(tg_app)
            except Exception as e:
                logger.warning("bot presence wiring failed: %s", e)
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
app.include_router(cookie_routes.router)
app.include_router(iptv_routes.router)
app.include_router(license_routes.router)
app.include_router(premium_routes.router)
app.include_router(auth_google.router)
app.include_router(auth_twitch.router)
app.include_router(streamer_consent.router)
app.include_router(pwa_routes.router)


# ── Edition gate ────────────────────────────────────────────────────
#
# Private-only feature surfaces. The community build ships as a platform
# shell: no torrent / Real-Debrid pipeline and no server-side HLS relay
# (YouTube is embedded via the official IFrame Player instead). We block
# these path prefixes outright in community so the routes can stay in the
# codebase without being reachable. See app/edition.py.
from fastapi.responses import JSONResponse as _EdJSONResponse  # noqa: E402
from fastapi.requests import Request as _EdRequest  # noqa: E402

_PRIVATE_PATH_PREFIXES = (
    "/api/miniapp/stremio/",     # torrent / Real-Debrid / Stremio pipeline
    "/iptv/hls/",                # same-origin HLS relay (community uses iframe)
    "/iptv/dvr/",                # v3.6-D rolling DVR buffer (private only)
    "/api/iptv/dvr/",            # …and its control routes
    "/api/iptv/refresh_country", # iptv-org per-country slices (aggregator)
)

# Carve-outs the COMMUNITY build IS allowed to reach inside an otherwise
# private prefix. The "stripped legal Theater": title search + popular
# discovery + episode metadata + the JustWatch/TMDB "where to watch" deep-link.
# All read-only public metadata — NO torrent/RD/disk. Everything else under
# /api/miniapp/stremio/ (streams, grab, queue, RD token, addons, settings, …)
# stays edition-blocked. The endpoints additionally gate owner-only data
# internally (e.g. discover hides continue-watching from non-owners).
_PRIVATE_PATH_EXCEPTIONS = (
    "/api/miniapp/stremio/search",
    "/api/miniapp/stremio/discover",
    "/api/miniapp/stremio/episodes",
    "/api/miniapp/stremio/watch_providers",
)


@app.middleware("http")
async def _edition_gate(request: _EdRequest, call_next):
    if edition.is_community():
        path = request.url.path
        if any(path.startswith(p) for p in _PRIVATE_PATH_PREFIXES) and not any(
            path.startswith(a) for a in _PRIVATE_PATH_EXCEPTIONS
        ):
            return _EdJSONResponse(
                {"ok": False, "error": "not_available_in_this_edition"},
                status_code=404,
            )
    return await call_next(request)


# Global 5xx → JSON shim (#38). Without this, an unhandled exception inside
# any route handler falls through to uvicorn's default text/plain
# "Internal Server Error" response. Mini App clients that do `await r.json()`
# then choke with "SyntaxError: Unexpected token 'I', \"Internal S\"... is
# not valid JSON" — the api() helper shield (#35) catches that now, but the
# proper fix is to never emit a non-JSON body in the first place. Logs the
# full traceback so we keep the diagnostic but the wire response stays
# JSON-shaped for every API consumer.
from fastapi.responses import JSONResponse as _JSONResponse
from fastapi.requests import Request as _Request
from starlette.exceptions import HTTPException as _StarletteHTTPException

@app.exception_handler(Exception)
async def _all_exception_handler(request: _Request, exc: Exception):
    # Let FastAPI/Starlette's own HTTPException path handle 4xx etc.; only
    # catch the "fell through to 500" path.
    if isinstance(exc, _StarletteHTTPException):
        return _JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
    logger.exception("unhandled exception in %s %s", request.method, request.url.path)
    return _JSONResponse(
        {"detail": f"{type(exc).__name__}: {exc}"[:500]},
        status_code=500,
    )


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


# ── #42 — /internal/reload-env (#27 fanout endpoint) ─────────────────────────
# Loopback + INTERNAL_RELOAD_TOKEN-gated. Called by sentinel-watchdog's
# secrets API after it writes a new value to .env.local, so SMDL can hot-swap
# keys without a container restart. See sentinel-watchdog @ c9d42cf.
from pathlib import Path as _Path
import hmac as _hmac
import os as _os

from . import _reload_env as _renv
from . import file_serve as _file_serve


def _swap_share_secret(v: str) -> None:
    """Rebind the module-level SHARE_SECRET so /share/<token>/<file>
    immediately validates against the new HMAC key. The four usage
    sites in file_serve all look up SHARE_SECRET by name (no captured
    locals), so this single rebind is sufficient."""
    _file_serve.SHARE_SECRET = v


_renv.register_hot_swap("SMDL_SHARE_SECRET", _swap_share_secret)


@app.post("/internal/reload-env")
async def internal_reload_env(request: _Request):
    host = request.client.host if request.client else ""
    # In a container, loopback may show as the container's IP (gateway). Accept
    # the standard loopback aliases AND the docker bridge gateway range.
    # Watchdog reaches us via host.docker.internal which Docker maps to the
    # gateway 172.17.0.1 (or similar) — request.client.host then shows the
    # gateway. The token gate is the real auth boundary.
    if host not in ("127.0.0.1", "::1", "localhost") and not host.startswith("172."):
        raise _StarletteHTTPException(403, f"internal endpoint: loopback only (got {host})")
    expected = _os.environ.get("INTERNAL_RELOAD_TOKEN", "")
    presented = request.headers.get("x-internal-reload-token", "")
    if not expected:
        raise _StarletteHTTPException(503, "INTERNAL_RELOAD_TOKEN not set in env")
    if not _hmac.compare_digest(expected, presented):
        raise _StarletteHTTPException(401, "internal endpoint: token mismatch")

    body = await request.json() if request.headers.get("content-length", "0") != "0" else {}
    keys = body.get("keys") if isinstance(body, dict) else None
    env_path_str = _os.environ.get("ENV_LOCAL_PATH", "/secrets/.env.local")
    result = await asyncio.to_thread(
        _renv.reload_env_in_process, _Path(env_path_str), keys=keys,
    )
    logger.info("reload-env keys=%s applied=%s frozen=%s",
                keys or "all", result["applied"], result["frozen"])
    return {"ok": True, **result}
