from __future__ import annotations

import hashlib
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from config.settings import Settings
from db.repository import PriceRepository
from db.session import DatabaseManager
from notifier.base import format_clp
from notifier.telegram import escape_markdown_v2, escape_markdown_v2_url

logger = logging.getLogger(__name__)

CHILE_OFFSET = timedelta(hours=-3)  # Horario de verano Chile (UTC-3 en noviembre)
API_BASE = "https://api.telegram.org"


def webhook_secret_token(bot_token: str) -> str:
    """Derive a deterministic Telegram webhook secret token from the bot token."""
    return hashlib.sha256(f"bf-price-hunter:{bot_token.strip()}".encode()).hexdigest()[:48]


def _collapse_price_timeline(history: list[dict[str, Any]], max_entries: int = 12) -> list[dict[str, Any]]:
    """Collapse consecutive duplicate prices while preserving the newest, oldest, and all transitions.

    `history` is ordered newest-first.
    """
    if not history:
        return []
    collapsed: list[dict[str, Any]] = [history[0]]
    for entry in history[1:]:
        if entry["price"] != collapsed[-1]["price"]:
            collapsed.append(entry)
    # Always include the oldest recorded point if different timestamp from the last shown
    oldest = history[-1]
    if collapsed[-1] is not oldest:
        collapsed.append(oldest)
    return collapsed[:max_entries]


def format_price_history_message(product_data: dict[str, Any]) -> str:
    """Format a MarkdownV2 Telegram summary of a product's historical prices."""
    retailer = escape_markdown_v2(str(product_data["retailer_name"]))
    name = escape_markdown_v2(str(product_data["name"]))
    sku = escape_markdown_v2(str(product_data["sku"]))
    url = escape_markdown_v2_url(str(product_data["url"]))
    history: list[dict[str, Any]] = list(product_data.get("history") or [])

    if not history:
        return (
            f"📊 *HISTORIAL DE PRECIOS*\n\n"
            f"🏪 *Tienda:* {retailer}\n"
            f"📦 *Producto:* {name}\n"
            f"🔖 *SKU:* `{sku}`\n\n"
            f"{escape_markdown_v2('Aún no hay capturas históricas suficientes para este producto.')}\n\n"
            f"🔗 [Ver producto]({url})"
        )

    prices = [int(h["price"]) for h in history if h.get("price")]
    current_price = prices[0]
    min_price = min(prices)
    max_price = max(prices)
    avg_price = round(sum(prices) / len(prices))
    samples = len(prices)

    diff_vs_avg = ((avg_price - current_price) / avg_price) * 100 if avg_price > 0 else 0.0
    if diff_vs_avg >= 0:
        avg_note = f"-{diff_vs_avg:.1f}% bajo promedio"
    else:
        avg_note = f"+{abs(diff_vs_avg):.1f}% sobre promedio"

    lines = [
        "📊 *HISTORIAL DE PRECIOS*",
        "",
        f"🏪 *Tienda:* {retailer}",
        f"📦 *Producto:* {name}",
        f"🔖 *SKU:* `{sku}`",
        "",
        f"💰 *Actual:* *{escape_markdown_v2(format_clp(current_price) + ' CLP')}*",
        f"📈 *Promedio:* {escape_markdown_v2(format_clp(avg_price) + ' CLP')} {escape_markdown_v2(f'({avg_note} · {samples} capturas)')}",
        f"⬇️ *Mínimo:* {escape_markdown_v2(format_clp(min_price))}  \\|  ⬆️ *Máximo:* {escape_markdown_v2(format_clp(max_price))}",
        "",
        "🕒 *Evolución \\(hora Chile\\):*",
    ]

    timeline = _collapse_price_timeline(history, max_entries=10)
    for idx, row in enumerate(timeline):
        ts = row["scraped_at"]
        if isinstance(ts, datetime):
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=UTC)
            ts_cl = ts + CHILE_OFFSET
            ts_str = ts_cl.strftime("%d/%m %H:%M")
        else:
            ts_str = str(ts)[:16]

        price_str = format_clp(int(row["price"]))
        card_flag = " 💳" if row.get("payment_method") == "STORE_CARD" else ""
        delta_str = ""
        if idx + 1 < len(timeline):
            prev_p = int(timeline[idx + 1]["price"])
            if prev_p > 0 and int(row["price"]) != prev_p:
                pct = ((int(row["price"]) - prev_p) / prev_p) * 100
                delta_str = f" ({pct:+.1f}%)"

        bullet = f"• {ts_str} — {price_str}{delta_str}{card_flag}"
        lines.append(escape_markdown_v2(bullet))

    lines.extend(["", f"🔗 [Ir al producto]({url})"])
    return "\n".join(lines)


async def _tg_post(bot_token: str, method: str, payload: dict[str, Any]) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(f"{API_BASE}/bot{bot_token}/{method}", json=payload)
        resp.raise_for_status()
        return resp.json()


async def handle_telegram_update(
    update: dict[str, Any],
    *,
    settings: Settings,
    db: DatabaseManager,
) -> None:
    """Process incoming Telegram webhook updates (inline history button & /historial command)."""
    bot_token = settings.telegram_bot_token.strip()
    if not bot_token:
        return

    # 1. Button click: callback_query ("hist:<product_id>")
    callback = update.get("callback_query")
    if isinstance(callback, dict):
        cb_id = str(callback.get("id") or "")
        data = str(callback.get("data") or "")
        msg = callback.get("message") or {}
        chat_id = (msg.get("chat") or {}).get("id")
        reply_to = msg.get("message_id")

        if data.startswith("hist:") and chat_id is not None:
            pid_str = data.split(":", 1)[1].strip()
            if cb_id:
                try:
                    await _tg_post(
                        bot_token,
                        "answerCallbackQuery",
                        {"callback_query_id": cb_id, "text": "Consultando historial..."},
                    )
                except Exception:
                    logger.debug("answerCallbackQuery failed", exc_info=True)

            if not pid_str.isdigit():
                return

            async with db.session() as session:
                repo = PriceRepository(session)
                product_data = await repo.get_product_price_history(
                    int(pid_str),
                    window_days=settings.history_window_days,
                )

            if product_data is None:
                text = escape_markdown_v2("No encontré historial para ese producto.")
            else:
                text = format_price_history_message(product_data)

            send_payload: dict[str, Any] = {
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "MarkdownV2",
                "disable_web_page_preview": True,
            }
            if reply_to is not None:
                send_payload["reply_to_message_id"] = reply_to
            await _tg_post(bot_token, "sendMessage", send_payload)
        return

    # 2. Text command: /historial <nombre o SKU>
    message = update.get("message")
    if not isinstance(message, dict):
        return

    text_raw = str(message.get("text") or "").strip()
    chat_id = (message.get("chat") or {}).get("id")
    reply_to = message.get("message_id")
    if not text_raw or chat_id is None:
        return

    first_token = text_raw.split()[0].lower()
    if not (first_token == "/historial" or first_token.startswith("/historial@")):
        return

    query = text_raw[len(text_raw.split()[0]):].strip()
    if not query:
        await _tg_post(
            bot_token,
            "sendMessage",
            {
                "chat_id": chat_id,
                "reply_to_message_id": reply_to,
                "text": "Uso: /historial <nombre o SKU>\nEjemplo: /historial Galaxy S25",
            },
        )
        return

    async with db.session() as session:
        repo = PriceRepository(session)
        matches = await repo.search_products_by_query(query, limit=5)

    if not matches:
        await _tg_post(
            bot_token,
            "sendMessage",
            {
                "chat_id": chat_id,
                "reply_to_message_id": reply_to,
                "text": f"No encontré productos monitoreados con '{query}'.",
            },
        )
        return

    if len(matches) == 1:
        async with db.session() as session:
            repo = PriceRepository(session)
            product_data = await repo.get_product_price_history(
                int(matches[0]["id"]),
                window_days=settings.history_window_days,
            )
        if product_data:
            await _tg_post(
                bot_token,
                "sendMessage",
                {
                    "chat_id": chat_id,
                    "reply_to_message_id": reply_to,
                    "text": format_price_history_message(product_data),
                    "parse_mode": "MarkdownV2",
                    "disable_web_page_preview": True,
                },
            )
        return

    # Multiple matches: show inline buttons to pick the exact product
    buttons = [
        [
            {
                "text": f"📊 {m['retailer_name']} · {str(m['name'])[:42]}",
                "callback_data": f"hist:{m['id']}",
            }
        ]
        for m in matches
    ]
    await _tg_post(
        bot_token,
        "sendMessage",
        {
            "chat_id": chat_id,
            "reply_to_message_id": reply_to,
            "text": f"Encontré {len(matches)} productos para '{query}'. Elige cuál ver:",
            "reply_markup": {"inline_keyboard": buttons},
        },
    )
