from __future__ import annotations

import logging
import re

import httpx

from analyzer.models import AlertType, PaymentMethodType
from notifier.base import AlertPayload, BaseNotifier, format_clp

logger = logging.getLogger(__name__)

# Characters that must be escaped in Telegram MarkdownV2 outside code/link entities
_MD_V2_SPECIAL = re.compile(r"([_\*\[\]\(\)~`>#+\-=|{}\.!\\])")


def escape_markdown_v2(text: str) -> str:
    """Escape special characters for Telegram MarkdownV2 parse mode."""
    return _MD_V2_SPECIAL.sub(r"\\\1", text)


def escape_markdown_v2_url(url: str) -> str:
    """Escape ')' and '\\' inside MarkdownV2 inline link URLs."""
    return url.replace("\\", "\\\\").replace(")", "\\)")


class TelegramNotifier(BaseNotifier):
    """Async Telegram Bot API notifier sending MarkdownV2 formatted alerts."""

    API_BASE = "https://api.telegram.org"

    def __init__(
        self,
        *,
        bot_token: str,
        chat_id: str,
        timeout: float = 15.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._bot_token = bot_token.strip()
        self._chat_id = chat_id.strip()
        self._timeout = timeout
        self._external_client = client

    @property
    def is_configured(self) -> bool:
        return bool(self._bot_token and self._chat_id)

    @staticmethod
    def _to_hashtag(label: str) -> str:
        import unicodedata

        normalized = unicodedata.normalize("NFKD", label)
        ascii_only = normalized.encode("ascii", "ignore").decode("ascii")
        cleaned = re.sub(r"[^A-Za-z0-9]+", "_", ascii_only).strip("_").upper()
        return f"#{cleaned}" if cleaned else ""

    def format_message(self, payload: AlertPayload) -> str:
        if payload.alert_type == AlertType.PRICE_ERROR:
            header = "🚨 *ERROR DE PRECIO / OFERTA EXTREMA* 🚨"
            level_tag = "#ERROR_DE_PRECIO"
        elif payload.alert_type == AlertType.STRONG_DEAL:
            header = "🔥 *OFERTA FUERTE \\(40%\\-69%\\)* 🔥"
            level_tag = "#OFERTA_FUERTE"
        else:
            header = "📉 *MÍNIMO HISTÓRICO DETECTADO*"
            level_tag = "#MINIMO_HISTORICO"

        store = escape_markdown_v2(payload.retailer_name)
        name = escape_markdown_v2(payload.product_name)
        current_price_str = escape_markdown_v2(f"{format_clp(payload.current_price)} CLP")

        lines = [
            header,
            "",
            f"🏪 *Tienda:* {store}",
            f"📦 *Producto:* {name}",
        ]

        if payload.category:
            lines.append(f"🏷️ *Categoría:* {escape_markdown_v2(payload.category)}")

        lines.append(f"💰 *Precio actual:* *{current_price_str}*")

        if payload.reference_price is not None:
            ref_label = escape_markdown_v2(payload.reference_label)
            ref_val = escape_markdown_v2(f"{format_clp(payload.reference_price)} CLP")
            lines.append(f"📊 *{ref_label}:* {ref_val}")

        if payload.discount_pct is not None:
            pct_str = escape_markdown_v2(f"{payload.discount_pct:.1f}%")
            lines.append(f"🔥 *Descuento:* *{pct_str}*")

        if payload.avg_30d_price is not None and payload.history_samples > 0:
            avg_str = escape_markdown_v2(f"{format_clp(payload.avg_30d_price)} CLP")
            if payload.drop_vs_avg_pct is not None and payload.drop_vs_avg_pct >= 0:
                detail = f"(-{payload.drop_vs_avg_pct:.1f}% bajo promedio · {payload.history_samples} capturas)"
            elif payload.drop_vs_avg_pct is not None:
                detail = f"(+{abs(payload.drop_vs_avg_pct):.1f}% sobre promedio · {payload.history_samples} capturas)"
            else:
                detail = f"({payload.history_samples} capturas)"
            lines.append(f"📈 *Promedio histórico 30d:* {avg_str} {escape_markdown_v2(detail)}")

        if payload.min_30d_price is not None and payload.history_samples > 0:
            min_str = escape_markdown_v2(f"{format_clp(payload.min_30d_price)} CLP")
            lines.append(f"⬇️ *Mínimo previo 30d:* {min_str}")

        # Payment method clarity (Store card vs All payment methods)
        payment_icon = (
            "💳" if payload.payment_method == PaymentMethodType.STORE_CARD else "✅"
        )
        payment_text = escape_markdown_v2(payload.payment_method_label)
        lines.append(f"{payment_icon} *Medio de pago:* {payment_text}")

        if (
            payload.payment_method == PaymentMethodType.STORE_CARD
            and payload.all_payment_price_clp is not None
            and payload.all_payment_price_clp != payload.current_price
        ):
            alt_str = escape_markdown_v2(f"{format_clp(payload.all_payment_price_clp)} CLP")
            lines.append(f"💵 *Todo medio de pago:* {alt_str}")

        if payload.is_cold_start:
            lines.append(escape_markdown_v2("⚡ Detectado en arranque en frío (primera captura)"))

        tags = [level_tag, self._to_hashtag(payload.retailer_name)]
        if payload.category:
            tags.append(self._to_hashtag(payload.category))
        hashtags_line = " ".join(escape_markdown_v2(t) for t in tags if t)

        safe_url = escape_markdown_v2_url(payload.url)
        lines.extend(
            [
                "",
                f"🔗 [Ir directo a la oferta]({safe_url})",
                hashtags_line,
            ]
        )

        return "\n".join(lines)

    async def send(self, payload: AlertPayload) -> bool:
        if not self.is_configured:
            logger.warning("TelegramNotifier is not configured; skipping message.")
            return False

        endpoint = f"{self.API_BASE}/bot{self._bot_token}/sendMessage"
        body = {
            "chat_id": self._chat_id,
            "text": self.format_message(payload),
            "parse_mode": "MarkdownV2",
            "disable_web_page_preview": False,
        }

        if self._external_client is not None:
            resp = await self._external_client.post(endpoint, json=body, timeout=self._timeout)
        else:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.post(endpoint, json=body)

        resp.raise_for_status()
        logger.info(
            "Telegram alert sent [%s] for %s (%s) at %s",
            payload.alert_type.value,
            payload.retailer_name,
            payload.sku,
            format_clp(payload.current_price),
        )
        return True
