from __future__ import annotations

from decimal import Decimal
from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from environment variables and `.env`."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # Database (Supabase PostgreSQL)
    database_url: str = Field(
        default="postgresql+asyncpg://postgres:postgres@localhost:5432/postgres",
        description="Async SQLAlchemy PostgreSQL connection URI.",
    )
    db_pool_size: int = Field(default=5, ge=1, le=50)
    db_max_overflow: int = Field(default=10, ge=0, le=50)

    # Anomaly detection thresholds
    extreme_discount_threshold: Decimal = Field(
        default=Decimal(70),
        ge=Decimal(1),
        le=Decimal("99.99"),
        description="Percentage drop (>=) that classifies an offer as PRICE_ERROR.",
    )
    strong_deal_threshold: Decimal = Field(
        default=Decimal(40),
        ge=Decimal(1),
        le=Decimal("99.0"),
        description="Percentage drop (>= and < extreme) that classifies an offer as STRONG_DEAL.",
    )
    historical_low_min_drop: Decimal = Field(
        default=Decimal(10),
        ge=Decimal("0.1"),
        le=Decimal(99),
        description="Minimum percentage drop vs 30d median to alert HISTORICAL_LOW.",
    )
    history_window_days: int = Field(default=30, ge=1, le=365)
    alert_cooldown_hours: int = Field(default=6, ge=0, le=168)
    cold_start_absurd_price_clp: int = Field(
        default=1000,
        ge=1,
        description=(
            "Maximum CLP price (in stock) that immediately triggers PRICE_ERROR "
            "on cold start when no prior history exists."
        ),
    )

    # Telegram Bot API
    telegram_bot_token: str = Field(default="")
    telegram_chat_id: str = Field(default="")

    # Twilio Voice API
    twilio_enabled: bool = Field(default=True)
    twilio_account_sid: str = Field(default="")
    twilio_auth_token: str = Field(default="")
    twilio_from_number: str = Field(default="")
    twilio_to_number: str = Field(default="")
    twilio_voice_name: str = Field(default="Polly.Mia")
    twilio_voice_language: str = Field(default="es-MX")
    twilio_global_cooldown_seconds: int = Field(
        default=180,
        ge=0,
        description=(
            "Global minimum interval in seconds between Twilio voice calls "
            "regardless of product (default 180s = 3 minutes)."
        ),
    )

    # CallMeBot (free Telegram voice calls alternative to Twilio)
    callmebot_enabled: bool = Field(default=False)
    callmebot_telegram_user: str = Field(default="")
    callmebot_lang: str = Field(default="es-US-Standard-A")
    callmebot_repeat: int = Field(default=2, ge=1, le=5)
    callmebot_global_cooldown_seconds: int = Field(default=180, ge=65)

    # Scraping & anti-blocking
    proxy_urls: str = Field(
        default="",
        description="Comma-separated list of proxy URLs for rotation.",
    )
    max_concurrent_requests: int = Field(default=4, ge=1, le=32)
    request_min_delay_sec: float = Field(default=1.0, ge=0.0)
    request_max_delay_sec: float = Field(default=3.0, ge=0.0)
    http_timeout_seconds: float = Field(default=25.0, ge=5.0, le=120.0)
    playwright_fallback_enabled: bool = Field(default=True)
    include_store_card_prices: bool = Field(
        default=True,
        description="Whether to consider exclusive store-card prices (e.g. CMR) when lower.",
    )

    # Scheduler & runtime
    targets_config_path: Path = Field(default=Path("config/targets.yaml"))
    watchlist_interval_minutes: int = Field(default=3, ge=1, le=1440)
    category_interval_minutes: int = Field(default=10, ge=1, le=1440)
    log_level: str = Field(default="INFO")

    @field_validator("database_url")
    @classmethod
    def normalize_asyncpg_url(cls, value: str) -> str:
        """Ensure standard postgres:// URLs from Railway/Supabase use asyncpg driver."""
        if value.startswith("postgres://"):
            return value.replace("postgres://", "postgresql+asyncpg://", 1)
        if value.startswith("postgresql://") and "+asyncpg" not in value:
            return value.replace("postgresql://", "postgresql+asyncpg://", 1)
        return value

    @property
    def parsed_proxy_urls(self) -> list[str]:
        if not self.proxy_urls.strip():
            return []
        return [p.strip() for p in self.proxy_urls.split(",") if p.strip()]

    @property
    def telegram_configured(self) -> bool:
        return bool(self.telegram_bot_token.strip() and self.telegram_chat_id.strip())

    @property
    def twilio_configured(self) -> bool:
        return bool(
            self.twilio_enabled
            and self.twilio_account_sid.strip()
            and self.twilio_auth_token.strip()
            and self.twilio_from_number.strip()
            and self.twilio_to_number.strip()
        )

    @property
    def callmebot_configured(self) -> bool:
        return bool(self.callmebot_enabled and self.callmebot_telegram_user.strip())


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
