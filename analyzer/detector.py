from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal

from analyzer.models import (
    AlertHistoryEntry,
    AlertType,
    AnomalyDecision,
    PriceObservation,
)

TWO_DECIMALS = Decimal("0.01")
HUNDRED = Decimal(100)


def _compute_decimal_median(values: Sequence[int]) -> Decimal:
    sorted_vals = sorted(values)
    n = len(sorted_vals)
    mid = n // 2
    if n % 2 == 1:
        return Decimal(sorted_vals[mid])
    return (Decimal(sorted_vals[mid - 1]) + Decimal(sorted_vals[mid])) / Decimal(2)


def _compute_drop_pct(reference: Decimal, current: int) -> Decimal:
    return ((reference - Decimal(current)) / reference) * HUNDRED


def _round_pct(value: Decimal | None) -> Decimal | None:
    if value is None:
        return None
    return value.quantize(TWO_DECIMALS, rounding=ROUND_HALF_UP)


class PriceAnomalyDetector:
    """Stateless, deterministic anomaly & price-error detector.

    Rules:
      1. Anti-false-positive filters:
         - Reject parse errors (`parse_ok=False`), out-of-stock (`in_stock=False`),
           or non-positive prices (`price is None or price <= 0`).
      2. Cold start (no prior 30-day history and no previous price):
         - Compare against store `normal_price`: if drop >= `extreme_discount_threshold`
           -> `PRICE_ERROR`.
         - Or if `current_price <= cold_start_absurd_price_clp` (with stock)
           -> `PRICE_ERROR`.
      3. Steady-state (with history):
         - `PRICE_ERROR`: drop >= `extreme_discount_threshold` (default 70%) vs 30d median
           OR drop >= `extreme_discount_threshold` vs immediately previous price.
         - `HISTORICAL_LOW`: `current_price < min_30d` AND
           `historical_low_min_drop <= drop_vs_median_pct < extreme_discount_threshold`.
      4. Cooldown:
         - Suppress if an alert for the same product and same `price_triggered`
           (with the same or higher severity) was already sent within `cooldown_hours`.
    """

    def __init__(
        self,
        *,
        extreme_discount_threshold: Decimal = Decimal(70),
        strong_deal_threshold: Decimal = Decimal(40),
        historical_low_min_drop: Decimal = Decimal(10),
        cooldown_hours: int = 6,
        cold_start_absurd_price_clp: int = 1000,
    ) -> None:
        self.extreme_discount_threshold = Decimal(str(extreme_discount_threshold))
        self.strong_deal_threshold = Decimal(str(strong_deal_threshold))
        self.historical_low_min_drop = Decimal(str(historical_low_min_drop))
        self.cooldown_hours = cooldown_hours
        self.cold_start_absurd_price_clp = cold_start_absurd_price_clp

    def evaluate(
        self,
        observation: PriceObservation,
        *,
        history_30d: Sequence[int] = (),
        previous_price: int | None = None,
        recent_alerts: Sequence[AlertHistoryEntry] = (),
        now: datetime | None = None,
    ) -> AnomalyDecision:
        # 1. Anti-false-positive filters
        if not observation.parse_ok:
            return AnomalyDecision(
                alert_type=None,
                reason="parse_error",
                current_price=observation.current_price,
            )

        if not observation.in_stock:
            return AnomalyDecision(
                alert_type=None,
                reason="out_of_stock",
                current_price=observation.current_price,
            )

        if observation.current_price is None or observation.current_price <= 0:
            return AnomalyDecision(
                alert_type=None,
                reason="invalid_price",
                current_price=observation.current_price,
            )

        current_price = observation.current_price
        normal_price = (
            observation.normal_price
            if observation.normal_price is not None and observation.normal_price > 0
            else None
        )
        valid_history = [p for p in history_30d if p > 0]
        valid_previous = previous_price if (previous_price is not None and previous_price > 0) else None
        is_cold_start = len(valid_history) == 0 and valid_previous is None

        drop_vs_normal_pct = (
            _compute_drop_pct(Decimal(normal_price), current_price)
            if normal_price is not None
            else None
        )

        # 2. Cold start evaluation (no prior history in DB)
        if is_cold_start:
            candidate: AlertType | None = None
            reason = "cold_start_baseline"
            reference_price: int | None = normal_price
            effective_discount: Decimal | None = drop_vs_normal_pct

            if (
                drop_vs_normal_pct is not None
                and drop_vs_normal_pct >= self.extreme_discount_threshold
            ):
                candidate = AlertType.PRICE_ERROR
                reason = "cold_start_normal_price_drop"
                reference_price = normal_price
                effective_discount = drop_vs_normal_pct
            elif current_price <= self.cold_start_absurd_price_clp:
                candidate = AlertType.PRICE_ERROR
                reason = "cold_start_absurd_price"
                reference_price = normal_price
                effective_discount = (
                    drop_vs_normal_pct if (drop_vs_normal_pct is not None and drop_vs_normal_pct > 0) else None
                )

            return self._apply_cooldown(
                candidate=candidate,
                reason=reason,
                current_price=current_price,
                reference_price=reference_price,
                median_30d=None,
                min_30d=None,
                previous_price=None,
                normal_price=normal_price,
                drop_vs_median_pct=None,
                drop_vs_previous_pct=None,
                drop_vs_normal_pct=drop_vs_normal_pct,
                effective_discount_pct=effective_discount,
                is_cold_start=True,
                recent_alerts=recent_alerts,
                now=now,
            )

        # 3. Steady-state evaluation with historical data
        median_30d = _compute_decimal_median(valid_history) if valid_history else None
        min_30d = min(valid_history) if valid_history else None

        drop_vs_median_pct = (
            _compute_drop_pct(median_30d, current_price)
            if median_30d is not None and median_30d > 0
            else None
        )
        drop_vs_previous_pct = (
            _compute_drop_pct(Decimal(valid_previous), current_price)
            if valid_previous is not None
            else None
        )

        candidate = None
        reason = "within_normal_range"
        reference_price = int(median_30d) if median_30d is not None else valid_previous
        effective_discount = drop_vs_median_pct

        # Priority 1: PRICE_ERROR (>= extreme_discount_threshold vs median OR vs previous price)
        median_triggers_error = (
            drop_vs_median_pct is not None
            and drop_vs_median_pct >= self.extreme_discount_threshold
        )
        previous_triggers_error = (
            drop_vs_previous_pct is not None
            and drop_vs_previous_pct >= self.extreme_discount_threshold
        )

        if median_triggers_error:
            candidate = AlertType.PRICE_ERROR
            reason = "median_30d_extreme_drop"
            reference_price = int(median_30d) if median_30d is not None else valid_previous
            effective_discount = drop_vs_median_pct
        elif previous_triggers_error:
            candidate = AlertType.PRICE_ERROR
            reason = "previous_price_extreme_drop"
            reference_price = valid_previous
            effective_discount = drop_vs_previous_pct
        # Priority 2: STRONG_DEAL (strictly below 30d min, strong_deal_threshold <= drop < extreme)
        elif (
            min_30d is not None
            and current_price < min_30d
            and drop_vs_median_pct is not None
            and self.strong_deal_threshold <= drop_vs_median_pct < self.extreme_discount_threshold
        ):
            candidate = AlertType.STRONG_DEAL
            reason = "strong_deal"
            reference_price = int(median_30d) if median_30d is not None else min_30d
            effective_discount = drop_vs_median_pct
        # Priority 3: HISTORICAL_LOW (strictly below 30d min, 10% <= drop < 40% vs 30d median)
        elif (
            min_30d is not None
            and current_price < min_30d
            and drop_vs_median_pct is not None
            and self.historical_low_min_drop <= drop_vs_median_pct < self.strong_deal_threshold
        ):
            candidate = AlertType.HISTORICAL_LOW
            reason = "historical_low"
            reference_price = int(median_30d) if median_30d is not None else min_30d
            effective_discount = drop_vs_median_pct

        return self._apply_cooldown(
            candidate=candidate,
            reason=reason,
            current_price=current_price,
            reference_price=reference_price,
            median_30d=median_30d,
            min_30d=min_30d,
            previous_price=valid_previous,
            normal_price=normal_price,
            drop_vs_median_pct=drop_vs_median_pct,
            drop_vs_previous_pct=drop_vs_previous_pct,
            drop_vs_normal_pct=drop_vs_normal_pct,
            effective_discount_pct=effective_discount,
            is_cold_start=False,
            recent_alerts=recent_alerts,
            now=now,
        )

    def _apply_cooldown(
        self,
        *,
        candidate: AlertType | None,
        reason: str,
        current_price: int,
        reference_price: int | None,
        median_30d: Decimal | None,
        min_30d: int | None,
        previous_price: int | None,
        normal_price: int | None,
        drop_vs_median_pct: Decimal | None,
        drop_vs_previous_pct: Decimal | None,
        drop_vs_normal_pct: Decimal | None,
        effective_discount_pct: Decimal | None,
        is_cold_start: bool,
        recent_alerts: Sequence[AlertHistoryEntry],
        now: datetime | None,
    ) -> AnomalyDecision:
        if candidate is None:
            return AnomalyDecision(
                alert_type=None,
                reason=reason,
                current_price=current_price,
                reference_price=reference_price,
                median_30d=median_30d,
                min_30d=min_30d,
                previous_price=previous_price,
                normal_price=normal_price,
                drop_vs_median_pct=_round_pct(drop_vs_median_pct),
                drop_vs_previous_pct=_round_pct(drop_vs_previous_pct),
                drop_vs_normal_pct=_round_pct(drop_vs_normal_pct),
                effective_discount_pct=_round_pct(effective_discount_pct),
                is_cold_start=is_cold_start,
            )

        if self.cooldown_hours > 0 and recent_alerts:
            severity_rank = {
                AlertType.HISTORICAL_LOW.value: 1,
                AlertType.STRONG_DEAL.value: 2,
                AlertType.PRICE_ERROR.value: 3,
            }
            candidate_rank = severity_rank.get(candidate.value, 1)
            reference_now = now or datetime.now(UTC)
            cutoff = reference_now - timedelta(hours=self.cooldown_hours)
            for alert in recent_alerts:
                alert_sent_at = alert.sent_at
                if alert_sent_at.tzinfo is None:
                    alert_sent_at = alert_sent_at.replace(tzinfo=UTC)
                if reference_now.tzinfo is None:
                    cutoff = reference_now.replace(tzinfo=UTC) - timedelta(
                        hours=self.cooldown_hours
                    )

                same_price = alert.price_triggered == current_price
                same_or_higher_severity = severity_rank.get(alert.alert_type, 0) >= candidate_rank
                if alert_sent_at >= cutoff and same_price and same_or_higher_severity:
                    return AnomalyDecision(
                        alert_type=None,
                        reason="cooldown_active",
                        current_price=current_price,
                        reference_price=reference_price,
                        median_30d=median_30d,
                        min_30d=min_30d,
                        previous_price=previous_price,
                        normal_price=normal_price,
                        drop_vs_median_pct=_round_pct(drop_vs_median_pct),
                        drop_vs_previous_pct=_round_pct(drop_vs_previous_pct),
                        drop_vs_normal_pct=_round_pct(drop_vs_normal_pct),
                        effective_discount_pct=_round_pct(effective_discount_pct),
                        is_cold_start=is_cold_start,
                        suppressed_by_cooldown=True,
                        suppressed_alert_type=candidate,
                    )

        return AnomalyDecision(
            alert_type=candidate,
            reason=reason,
            current_price=current_price,
            reference_price=reference_price,
            median_30d=median_30d,
            min_30d=min_30d,
            previous_price=previous_price,
            normal_price=normal_price,
            drop_vs_median_pct=_round_pct(drop_vs_median_pct),
            drop_vs_previous_pct=_round_pct(drop_vs_previous_pct),
            drop_vs_normal_pct=_round_pct(drop_vs_normal_pct),
            effective_discount_pct=_round_pct(effective_discount_pct),
            is_cold_start=is_cold_start,
        )
