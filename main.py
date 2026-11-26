from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from decimal import Decimal

from analyzer.detector import PriceAnomalyDetector
from analyzer.models import AlertType, PaymentMethodType
from config.settings import Settings, get_settings
from db.session import DatabaseManager
from notifier.base import AlertPayload
from notifier.callmebot_voice import CallMeBotVoiceNotifier
from notifier.dispatcher import AlertDispatcher
from notifier.telegram import TelegramNotifier
from notifier.twilio_voice import TwilioVoiceNotifier
from scheduler.pipeline import MonitoringPipeline
from scheduler.runner import run_scheduler_forever


def configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
    )


def build_components(
    settings: Settings,
) -> tuple[DatabaseManager, PriceAnomalyDetector, AlertDispatcher]:
    db = DatabaseManager(settings)
    detector = PriceAnomalyDetector(
        extreme_discount_threshold=settings.extreme_discount_threshold,
        strong_deal_threshold=settings.strong_deal_threshold,
        historical_low_min_drop=settings.historical_low_min_drop,
        cooldown_hours=settings.alert_cooldown_hours,
        cold_start_absurd_price_clp=settings.cold_start_absurd_price_clp,
    )
    telegram = TelegramNotifier(
        bot_token=settings.telegram_bot_token,
        chat_id=settings.telegram_chat_id,
    )
    voice_notifier: TwilioVoiceNotifier | CallMeBotVoiceNotifier
    if settings.twilio_configured:
        voice_notifier = TwilioVoiceNotifier(
            account_sid=settings.twilio_account_sid,
            auth_token=settings.twilio_auth_token,
            from_number=settings.twilio_from_number,
            to_number=settings.twilio_to_number,
            voice_name=settings.twilio_voice_name,
            language=settings.twilio_voice_language,
            global_cooldown_seconds=settings.twilio_global_cooldown_seconds,
            enabled=settings.twilio_enabled,
        )
    elif settings.callmebot_configured:
        voice_notifier = CallMeBotVoiceNotifier(
            telegram_user=settings.callmebot_telegram_user,
            lang=settings.callmebot_lang,
            repeat=settings.callmebot_repeat,
            global_cooldown_seconds=settings.callmebot_global_cooldown_seconds,
            enabled=settings.callmebot_enabled,
        )
    else:
        voice_notifier = TwilioVoiceNotifier(
            account_sid=settings.twilio_account_sid,
            auth_token=settings.twilio_auth_token,
            from_number=settings.twilio_from_number,
            to_number=settings.twilio_to_number,
            voice_name=settings.twilio_voice_name,
            language=settings.twilio_voice_language,
            global_cooldown_seconds=settings.twilio_global_cooldown_seconds,
            enabled=False,
        )
    dispatcher = AlertDispatcher(telegram=telegram, twilio=voice_notifier)
    return db, detector, dispatcher


async def run_test_alert(dispatcher: AlertDispatcher) -> None:
    """Send a synthetic PRICE_ERROR alert to verify Telegram & Twilio credentials."""
    payload = AlertPayload(
        retailer_name="Falabella",
        product_name="[PRUEBA] Notebook Gamer 16GB RTX 4060",
        sku="TEST-SKU-001",
        url="https://www.falabella.com/falabella-cl",
        category="Notebooks",
        alert_type=AlertType.PRICE_ERROR,
        current_price=100,
        reference_price=100000,
        reference_label="Mediana 30 días",
        discount_pct=Decimal("99.90"),
        payment_method=PaymentMethodType.ALL_PAYMENT_METHODS,
        payment_method_label="Todo medio de pago",
        reason="manual_test_alert",
    )
    result = await dispatcher.dispatch(payload)
    logging.getLogger(__name__).info(
        "Test alert completed: telegram_sent=%s, twilio_called=%s, "
        "twilio_rate_limited=%s, errors=%s",
        result.telegram_sent,
        result.twilio_called,
        result.twilio_rate_limited,
        result.errors,
    )


async def async_main(args: argparse.Namespace) -> None:
    settings = get_settings()
    configure_logging(settings.log_level)

    db, detector, dispatcher = build_components(settings)
    try:
        if args.test_alert:
            await run_test_alert(dispatcher)
            return

        pipeline = MonitoringPipeline(
            settings=settings,
            db=db,
            detector=detector,
            dispatcher=dispatcher,
        )

        if args.once:
            await pipeline.hydrate_global_rate_limits()
            watchlist_stats = await pipeline.run_watchlist_cycle()
            category_stats = await pipeline.run_category_cycle()
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
            return

        await run_scheduler_forever(settings=settings, pipeline=pipeline)
    finally:
        await db.dispose()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Black Friday Chile Price Hunter Bot (Async + Supabase + Telegram + Twilio)"
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run a single watchlist + category scraping cycle and exit.",
    )
    parser.add_argument(
        "--test-alert",
        action="store_true",
        help="Send a simulated PRICE_ERROR alert to verify Telegram and Twilio Voice.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    asyncio.run(async_main(args))


if __name__ == "__main__":
    main()
