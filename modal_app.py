"""Despliegue serverless gratuito en Modal.com para Black Friday Price Hunter Bot.

Uso:
  1. Autenticarse:          modal setup
  2. Crear el secreto:      modal secret create bf-price-hunter ... (o desde modal.com/secrets)
  3. Probar alerta:         modal run modal_app.py --test-alert
  4. Probar 1 ciclo:        modal run modal_app.py
  5. Desplegar (cada 10m):  modal deploy modal_app.py
"""

from __future__ import annotations

import logging

import modal

APP_NAME = "bf-price-hunter"
SECRET_NAME = "bf-price-hunter"

app = modal.App(APP_NAME)

image = (
    modal.Image.debian_slim(python_version="3.12")
    .pip_install_from_requirements("requirements.txt")
    .run_commands("playwright install --with-deps chromium")
    .env({"PYTHONPATH": "/app", "PYTHONUNBUFFERED": "1"})
    .workdir("/app")
    .add_local_dir(
        ".",
        remote_path="/app",
        ignore=[
            ".git",
            ".github",
            ".venv",
            ".env",
            ".pytest_cache",
            ".ruff_cache",
            "__pycache__",
            "**/*.pyc",
            "tests",
        ],
    )
)


@app.function(
    image=image,
    secrets=[modal.Secret.from_name(SECRET_NAME)],
    schedule=modal.Period(minutes=10),
    timeout=540,
    max_containers=1,
    cpu=0.5,
    memory=1024,
)
async def run_monitor_cycle() -> None:
    """Ejecuta un ciclo completo (watchlist + categorías) cada 10 minutos."""
    from config.settings import get_settings
    from main import build_components, configure_logging
    from scheduler.pipeline import MonitoringPipeline

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
        logging.getLogger(__name__).info(
            "Modal cycle complete | watchlist(scraped=%d, alerts=%d, errors=%d) | "
            "categories(scraped=%d, recorded=%d, alerts=%d, errors=%d)",
            watchlist_stats.products_scraped,
            watchlist_stats.alerts_triggered,
            watchlist_stats.errors,
            category_stats.products_scraped,
            category_stats.prices_recorded,
            category_stats.alerts_triggered,
            category_stats.errors,
        )
    finally:
        await db.dispose()


@app.function(
    image=image,
    secrets=[modal.Secret.from_name(SECRET_NAME)],
    timeout=60,
    cpu=0.25,
    memory=512,
)
async def run_test_alert_remote() -> None:
    """Envía una alerta simulada desde Modal para verificar Telegram (y Twilio si está activo)."""
    from config.settings import get_settings
    from main import build_components, configure_logging, run_test_alert

    get_settings.cache_clear()
    settings = get_settings()
    configure_logging(settings.log_level)

    db, _, dispatcher = build_components(settings)
    try:
        await run_test_alert(dispatcher)
    finally:
        await db.dispose()


@app.local_entrypoint()
def main(test_alert: bool = False) -> None:
    """Permite probar desde CLI: `modal run modal_app.py [--test-alert]`."""
    if test_alert:
        run_test_alert_remote.remote()
    else:
        run_monitor_cycle.remote()
