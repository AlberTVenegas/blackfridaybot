from __future__ import annotations

import asyncio
import logging
import signal
from datetime import UTC, datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from config.settings import Settings
from scheduler.pipeline import MonitoringPipeline

logger = logging.getLogger(__name__)


async def run_scheduler_forever(
    *,
    settings: Settings,
    pipeline: MonitoringPipeline,
) -> None:
    """Start APScheduler with independent watchlist and category discovery jobs."""
    await pipeline.hydrate_global_rate_limits()

    scheduler = AsyncIOScheduler(timezone=UTC)
    now = datetime.now(UTC)

    scheduler.add_job(
        pipeline.run_watchlist_cycle,
        trigger=IntervalTrigger(minutes=settings.watchlist_interval_minutes),
        id="watchlist_monitor",
        name="High-frequency Watchlist Monitor",
        next_run_time=now,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=60,
    )

    scheduler.add_job(
        pipeline.run_category_cycle,
        trigger=IntervalTrigger(minutes=settings.category_interval_minutes),
        id="category_discovery",
        name="Category Discovery Monitor",
        next_run_time=now,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=120,
    )

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:
            pass

    scheduler.start()
    logger.info(
        "APScheduler started (watchlist every %dm, categories every %dm, "
        "extreme threshold=%s%%, twilio global cooldown=%ds)",
        settings.watchlist_interval_minutes,
        settings.category_interval_minutes,
        settings.extreme_discount_threshold,
        settings.twilio_global_cooldown_seconds,
    )

    try:
        await stop_event.wait()
    finally:
        logger.info("Shutting down APScheduler...")
        scheduler.shutdown(wait=True)
