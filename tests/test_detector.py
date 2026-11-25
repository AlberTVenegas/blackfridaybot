from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from analyzer.detector import PriceAnomalyDetector
from analyzer.models import AlertHistoryEntry, AlertType, PriceObservation


class TestSteadyStateDetection:
    def test_normal_small_drop_does_not_alert(
        self, detector: PriceAnomalyDetector, base_now: datetime
    ) -> None:
        # Median = 100.000, current = 95.000 -> 5% drop (< 10%)
        obs = PriceObservation(current_price=95_000, in_stock=True)
        decision = detector.evaluate(
            obs,
            history_30d=[100_000, 100_000, 99_000, 101_000],
            previous_price=100_000,
            now=base_now,
        )
        assert decision.alert_type is None
        assert not decision.should_alert
        assert decision.reason == "within_normal_range"

    def test_historical_low_between_10_and_69_9_percent(
        self, detector: PriceAnomalyDetector, base_now: datetime
    ) -> None:
        # Median = 100.000, Min = 92.000, current = 80.000 -> 20% drop & strictly < min
        obs = PriceObservation(current_price=80_000, in_stock=True)
        decision = detector.evaluate(
            obs,
            history_30d=[100_000, 100_000, 92_000, 105_000],
            previous_price=100_000,
            now=base_now,
        )
        assert decision.alert_type == AlertType.HISTORICAL_LOW
        assert decision.effective_discount_pct == Decimal("20.00")
        assert decision.min_30d == 92_000

    def test_strong_deal_upper_boundary_69_9_percent(
        self, detector: PriceAnomalyDetector, base_now: datetime
    ) -> None:
        # Median = 100.000, current = 30.100 -> 69.90% drop -> STRONG_DEAL (not PRICE_ERROR)
        obs = PriceObservation(current_price=30_100, in_stock=True)
        decision = detector.evaluate(
            obs,
            history_30d=[100_000, 100_000, 95_000],
            previous_price=100_000,
            now=base_now,
        )
        assert decision.alert_type == AlertType.STRONG_DEAL
        assert decision.effective_discount_pct == Decimal("69.90")

    def test_strong_deal_exact_40_percent_boundary(
        self, detector: PriceAnomalyDetector, base_now: datetime
    ) -> None:
        # Median = 100.000, current = 60.000 -> 40.00% drop -> STRONG_DEAL
        obs = PriceObservation(current_price=60_000, in_stock=True)
        decision = detector.evaluate(
            obs,
            history_30d=[100_000, 100_000, 95_000],
            previous_price=100_000,
            now=base_now,
        )
        assert decision.alert_type == AlertType.STRONG_DEAL
        assert decision.effective_discount_pct == Decimal("40.00")

    def test_equal_to_previous_min_does_not_alert_historical_low(
        self, detector: PriceAnomalyDetector, base_now: datetime
    ) -> None:
        # Median = 100.000, Min = 80.000, current = 80.000 -> NOT strictly lower than min
        obs = PriceObservation(current_price=80_000, in_stock=True)
        decision = detector.evaluate(
            obs,
            history_30d=[100_000, 100_000, 80_000],
            previous_price=100_000,
            now=base_now,
        )
        assert decision.alert_type is None

    def test_exact_70_percent_boundary_triggers_price_error(
        self, detector: PriceAnomalyDetector, base_now: datetime
    ) -> None:
        # Median = 100.000, current = 30.000 -> EXACT 70.00% drop -> PRICE_ERROR
        obs = PriceObservation(current_price=30_000, in_stock=True)
        decision = detector.evaluate(
            obs,
            history_30d=[100_000, 100_000, 100_000],
            previous_price=100_000,
            now=base_now,
        )
        assert decision.alert_type == AlertType.PRICE_ERROR
        assert decision.effective_discount_pct == Decimal("70.00")
        assert decision.reason == "median_30d_extreme_drop"

    def test_extreme_price_error_100000_to_100_clp(
        self, detector: PriceAnomalyDetector, base_now: datetime
    ) -> None:
        # Classic Black Friday glitch: $100.000 CLP -> $100 CLP (99.90% drop)
        obs = PriceObservation(current_price=100, in_stock=True)
        decision = detector.evaluate(
            obs,
            history_30d=[100_000, 99_990, 100_000, 105_000],
            previous_price=100_000,
            now=base_now,
        )
        assert decision.alert_type == AlertType.PRICE_ERROR
        assert decision.effective_discount_pct == Decimal("99.90")
        assert decision.current_price == 100

    def test_price_error_triggered_by_previous_price_drop(
        self, detector: PriceAnomalyDetector, base_now: datetime
    ) -> None:
        # Even if 30d median is lower (50k), an abrupt drop from previous_price 100k -> 25k (75%)
        # must trigger PRICE_ERROR
        obs = PriceObservation(current_price=25_000, in_stock=True)
        decision = detector.evaluate(
            obs,
            history_30d=[50_000, 50_000, 50_000, 100_000],
            previous_price=100_000,
            now=base_now,
        )
        assert decision.alert_type == AlertType.PRICE_ERROR
        assert decision.reason == "previous_price_extreme_drop"
        assert decision.effective_discount_pct == Decimal("75.00")
        assert decision.reference_price == 100_000

    def test_configurable_extreme_threshold(self, base_now: datetime) -> None:
        custom_detector = PriceAnomalyDetector(extreme_discount_threshold=Decimal(60))
        obs = PriceObservation(current_price=40_000, in_stock=True)
        decision = custom_detector.evaluate(
            obs,
            history_30d=[100_000, 100_000],
            previous_price=100_000,
            now=base_now,
        )
        assert decision.alert_type == AlertType.PRICE_ERROR
        assert decision.effective_discount_pct == Decimal("60.00")


class TestColdStartDetection:
    def test_cold_start_normal_price_drop_ge_70_triggers_price_error(
        self, detector: PriceAnomalyDetector, base_now: datetime
    ) -> None:
        # First capture ever (no history), normalPrice=$100.000 -> price=$29.990 (70.01% drop)
        obs = PriceObservation(
            current_price=29_990,
            normal_price=100_000,
            in_stock=True,
        )
        decision = detector.evaluate(obs, history_30d=[], previous_price=None, now=base_now)
        assert decision.alert_type == AlertType.PRICE_ERROR
        assert decision.is_cold_start is True
        assert decision.reason == "cold_start_normal_price_drop"
        assert decision.reference_price == 100_000
        assert decision.effective_discount_pct == Decimal("70.01")

    def test_cold_start_exact_70_percent_vs_normal_price(
        self, detector: PriceAnomalyDetector, base_now: datetime
    ) -> None:
        obs = PriceObservation(current_price=30_000, normal_price=100_000, in_stock=True)
        decision = detector.evaluate(obs, history_30d=[], previous_price=None, now=base_now)
        assert decision.alert_type == AlertType.PRICE_ERROR
        assert decision.effective_discount_pct == Decimal("70.00")

    def test_cold_start_50_percent_drop_vs_normal_price_builds_baseline_only(
        self, detector: PriceAnomalyDetector, base_now: datetime
    ) -> None:
        # Inflated list prices ("precios inflados") at 50% shouldn't spam on first capture
        obs = PriceObservation(current_price=50_000, normal_price=100_000, in_stock=True)
        decision = detector.evaluate(obs, history_30d=[], previous_price=None, now=base_now)
        assert decision.alert_type is None
        assert decision.is_cold_start is True
        assert decision.reason == "cold_start_baseline"

    @pytest.mark.parametrize("absurd_price", [1, 100, 990, 1000])
    def test_cold_start_absurd_price_le_1000_clp_with_stock_alerts_even_without_normal_price(
        self, detector: PriceAnomalyDetector, base_now: datetime, absurd_price: int
    ) -> None:
        obs = PriceObservation(current_price=absurd_price, normal_price=None, in_stock=True)
        decision = detector.evaluate(obs, history_30d=[], previous_price=None, now=base_now)
        assert decision.alert_type == AlertType.PRICE_ERROR
        assert decision.is_cold_start is True
        assert decision.reason == "cold_start_absurd_price"

    def test_cold_start_1001_clp_without_normal_price_does_not_alert(
        self, detector: PriceAnomalyDetector, base_now: datetime
    ) -> None:
        obs = PriceObservation(current_price=1001, normal_price=None, in_stock=True)
        decision = detector.evaluate(obs, history_30d=[], previous_price=None, now=base_now)
        assert decision.alert_type is None
        assert decision.reason == "cold_start_baseline"

    def test_cold_start_absurd_price_out_of_stock_is_filtered(
        self, detector: PriceAnomalyDetector, base_now: datetime
    ) -> None:
        obs = PriceObservation(current_price=100, normal_price=100_000, in_stock=False)
        decision = detector.evaluate(obs, history_30d=[], previous_price=None, now=base_now)
        assert decision.alert_type is None
        assert decision.reason == "out_of_stock"


class TestFalsePositiveFilters:
    @pytest.mark.parametrize("bad_price", [None, 0, -1, -99_990])
    def test_non_positive_or_missing_price_rejected(
        self, detector: PriceAnomalyDetector, base_now: datetime, bad_price: int | None
    ) -> None:
        obs = PriceObservation(current_price=bad_price, in_stock=True)
        decision = detector.evaluate(
            obs,
            history_30d=[100_000, 100_000],
            previous_price=100_000,
            now=base_now,
        )
        assert decision.alert_type is None
        assert decision.reason == "invalid_price"

    def test_out_of_stock_product_rejected(
        self, detector: PriceAnomalyDetector, base_now: datetime
    ) -> None:
        obs = PriceObservation(current_price=100, in_stock=False)
        decision = detector.evaluate(
            obs,
            history_30d=[100_000, 100_000],
            previous_price=100_000,
            now=base_now,
        )
        assert decision.alert_type is None
        assert decision.reason == "out_of_stock"

    def test_parse_failure_rejected(
        self, detector: PriceAnomalyDetector, base_now: datetime
    ) -> None:
        obs = PriceObservation(current_price=100, in_stock=True, parse_ok=False)
        decision = detector.evaluate(
            obs,
            history_30d=[100_000, 100_000],
            previous_price=100_000,
            now=base_now,
        )
        assert decision.alert_type is None
        assert decision.reason == "parse_error"


class TestCooldownLogic:
    def test_same_product_same_price_within_cooldown_is_suppressed(
        self, detector: PriceAnomalyDetector, base_now: datetime
    ) -> None:
        obs = PriceObservation(current_price=100, in_stock=True)
        recent = [
            AlertHistoryEntry(
                alert_type=AlertType.PRICE_ERROR.value,
                price_triggered=100,
                sent_at=base_now - timedelta(hours=2),
            )
        ]
        decision = detector.evaluate(
            obs,
            history_30d=[100_000, 100_000],
            previous_price=100_000,
            recent_alerts=recent,
            now=base_now,
        )
        assert decision.alert_type is None
        assert decision.suppressed_by_cooldown is True
        assert decision.suppressed_alert_type == AlertType.PRICE_ERROR
        assert decision.reason == "cooldown_active"

    def test_same_product_same_price_after_cooldown_expires_fires_again(
        self, detector: PriceAnomalyDetector, base_now: datetime
    ) -> None:
        obs = PriceObservation(current_price=100, in_stock=True)
        recent = [
            AlertHistoryEntry(
                alert_type=AlertType.PRICE_ERROR.value,
                price_triggered=100,
                sent_at=base_now - timedelta(hours=6, minutes=1),
            )
        ]
        decision = detector.evaluate(
            obs,
            history_30d=[100_000, 100_000],
            previous_price=100_000,
            recent_alerts=recent,
            now=base_now,
        )
        assert decision.alert_type == AlertType.PRICE_ERROR
        assert decision.suppressed_by_cooldown is False

    def test_further_price_drop_bypasses_cooldown(
        self, detector: PriceAnomalyDetector, base_now: datetime
    ) -> None:
        # Earlier alerted at $30.000, now crashed to $100 -> different price -> MUST alert
        obs = PriceObservation(current_price=100, in_stock=True)
        recent = [
            AlertHistoryEntry(
                alert_type=AlertType.PRICE_ERROR.value,
                price_triggered=30_000,
                sent_at=base_now - timedelta(minutes=15),
            )
        ]
        decision = detector.evaluate(
            obs,
            history_30d=[100_000, 100_000],
            previous_price=30_000,
            recent_alerts=recent,
            now=base_now,
        )
        assert decision.alert_type == AlertType.PRICE_ERROR
        assert decision.suppressed_by_cooldown is False
