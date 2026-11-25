from __future__ import annotations

import pytest

from scrapers.utils import parse_clp_price


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("$1.299.990", 1_299_990),
        ("$ 99.990", 99_990),
        ("$100", 100),
        ("CLP 49.990", 49_990),
        ("100.000", 100_000),
        ("1.299.990,00", 1_299_990),
        ("99990.00", 99_990),
        ("$100.000 - $120.000", 100_000),
        (549_990, 549_990),
        (100.0, 100),
    ],
)
def test_valid_clp_prices(raw: object, expected: int) -> None:
    assert parse_clp_price(raw) == expected  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "raw",
    [None, "", "   ", "$0", "0", 0, -500, "-$10.000", "Agotado", "N/A", True, False, float("nan")],
)
def test_invalid_clp_prices_return_none(raw: object) -> None:
    assert parse_clp_price(raw) is None  # type: ignore[arg-type]
