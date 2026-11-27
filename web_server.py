"""Servidor HTTP ligero (FastAPI) para desplegar gratis en Render (512 MB RAM).

Endpoints:
  - GET  /health           -> Health check para Render
  - GET/POST /run?key=...  -> Dispara un ciclo completo en background (para cron-job.org)
  - POST /telegram/webhook -> Webhook de Telegram (/ejecutar, /estado, /historial y botones)
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request

from config.settings import get_settings
from db.session import DatabaseManager
from main import build_components, configure_logging
from notifier.telegram_bot import handle_telegram_update, webhook_secret_token
from scheduler.pipeline import MonitoringPipeline

logger = logging.getLogger(__name__)

app = FastAPI(title="Black Friday Price Hunter", docs_url=None, redoc_url=None)

_cycle_lock = asyncio.Lock()
_active_task: asyncio.Task[None] | None = None


def _expected_cron_key() -> str:
    explicit = os.getenv("CRON_SECRET", "").strip()
    if explicit:
        return explicit
    return webhook_secret_token(get_settings().telegram_bot_token)


async def _run_full_cycle() -> None:
    if _cycle_lock.locked():
        logger.info("Monitoring cycle already running; skipping duplicate trigger.")
        return

    async with _cycle_lock:
        get_settings.cache_clear()
        settings = get_settings()
        configure_logging(settings.log_level)

        db, detector, dispatcher = build_components(settings)
        try:
            pipeline = MonitoringPipeline(
                settings=settings,
                db=db,
                detector=detector,
                dispatcher=dispatcher,
            )
            await pipeline.hydrate_global_rate_limits()
            watchlist_stats = await pipeline.run_watchlist_cycle()
            category_stats = await pipeline.run_category_cycle()
            await pipeline.prune_old_history()

            logger.info(
                "Render cycle complete | watchlist(scraped=%d, alerts=%d, errors=%d) | "
                "categories(scraped=%d, recorded=%d, alerts=%d, errors=%d)",
                watchlist_stats.products_scraped,
                watchlist_stats.alerts_triggered,
                watchlist_stats.errors,
                category_stats.products_scraped,
                category_stats.prices_recorded,
                category_stats.alerts_triggered,
                category_stats.errors,
            )

            total_alerts = watchlist_stats.alerts_triggered + category_stats.alerts_triggered
            if total_alerts == 0 and settings.notify_on_no_deals:
                await dispatcher.telegram.send_no_deals_summary(
                    products_scraped=(
                        watchlist_stats.products_scraped + category_stats.products_scraped
                    ),
                    prices_recorded=(
                        watchlist_stats.prices_recorded + category_stats.prices_recorded
                    ),
                    errors=watchlist_stats.errors + category_stats.errors,
                )
        except Exception:
            logger.exception("Unhandled error during monitoring cycle")
        finally:
            await db.dispose()


def _spawn_cycle() -> bool:
    """Schedule a monitoring cycle in the background if none is currently running."""
    global _active_task
    if _cycle_lock.locked() or (_active_task is not None and not _active_task.done()):
        return False
    _active_task = asyncio.create_task(_run_full_cycle())
    return True


@app.get("/")
@app.get("/health")
async def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "cycle_running": _cycle_lock.locked(),
    }


@app.api_route("/run", methods=["GET", "POST"])
async def trigger_run(
    request: Request,
    key: str = Query(default=""),
) -> dict[str, Any]:
    provided = key.strip() or request.headers.get("X-Cron-Key", "").strip()
    expected = _expected_cron_key()
    if not expected or provided != expected:
        raise HTTPException(status_code=403, detail="Forbidden")

    started = _spawn_cycle()
    return {
        "ok": True,
        "started": started,
        "already_running": not started,
    }


@app.post("/telegram/webhook")
async def telegram_webhook(payload: dict[str, Any]) -> dict[str, bool]:
    get_settings.cache_clear()
    settings = get_settings()
    configure_logging(settings.log_level)

    db = DatabaseManager(settings)
    try:
        await handle_telegram_update(
            payload,
            settings=settings,
            db=db,
            trigger_cycle=_spawn_cycle,
        )
    finally:
        await db.dispose()
    return {"ok": True}
