from __future__ import annotations

import asyncio
import itertools
import math
import random
import re
from collections.abc import Iterable

# Modern desktop User-Agents for rotation
USER_AGENTS: tuple[str, ...] = (
    (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
    ),
    (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36 Edg/130.0.0.0"
    ),
    (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_7) AppleWebKit/605.1.15 "
        "(KHTML, like Gecko) Version/18.0 Safari/605.1.15"
    ),
)

# Matches the first price token (avoiding concatenating ranges like "$100.000 - $120.000")
_FIRST_PRICE_TOKEN_RE = re.compile(r"(?P<sign>-)?\s*(?:\$|CLP)?\s*(?P<num>\d[\d\.,]*)")
_TRAILING_CENTS_RE = re.compile(r"([.,]\d{2})$")


def parse_clp_price(raw: str | float | None) -> int | None:
    """Normalize Chilean Peso (CLP) prices to a positive integer.

    Examples:
        "$1.299.990"            -> 1299990
        "$ 99.990 CLP"          -> 99990
        "$100"                  -> 100
        "100.000,00"            -> 100000
        "99990.00"              -> 99990
        "$100.000 - $120.000"   -> 100000 (first price in range)
        "$0" / "-500" / "N/A"   -> None
    """
    if raw is None or isinstance(raw, bool):
        return None

    if isinstance(raw, int):
        return raw if raw > 0 else None

    if isinstance(raw, float):
        if raw <= 0 or math.isnan(raw) or math.isinf(raw):
            return None
        return round(raw)

    text = str(raw).strip()
    if not text:
        return None

    # Reject explicit negative values like "-$10.000", "$-10.000", or "-1000"
    if re.match(r"^\s*(?:CLP|\$)?\s*-\s*(?:CLP|\$)?\s*\d", text, re.IGNORECASE):
        return None

    match = _FIRST_PRICE_TOKEN_RE.search(text)
    if not match or match.group("sign"):
        return None

    num_str = match.group("num")

    # Strip trailing decimal cents (e.g. "99990.00" or "1.299.990,00")
    # Note: in Chile, 3 digits after dot/comma (e.g. "100.000") is a thousands separator,
    # whereas 2 digits at the very end (e.g. ",00" or ".00") are decimal cents.
    if _TRAILING_CENTS_RE.search(num_str):
        num_str = _TRAILING_CENTS_RE.sub("", num_str)

    digits_only = re.sub(r"\D", "", num_str)
    if not digits_only:
        return None

    value = int(digits_only)
    return value if value > 0 else None


def get_random_user_agent() -> str:
    return random.choice(USER_AGENTS)


async def random_delay(min_seconds: float, max_seconds: float) -> None:
    if max_seconds <= 0:
        return
    low = max(0.0, min(min_seconds, max_seconds))
    high = max(low, max_seconds)
    await asyncio.sleep(random.uniform(low, high))


class ProxyRotator:
    """Round-robin proxy rotator with optional random fallback."""

    def __init__(self, proxies: Iterable[str] = ()) -> None:
        self._proxies = [p.strip() for p in proxies if p and p.strip()]
        self._cycle = itertools.cycle(self._proxies) if self._proxies else None

    @property
    def has_proxies(self) -> bool:
        return bool(self._proxies)

    def next_proxy(self) -> str | None:
        if self._cycle is None:
            return None
        return next(self._cycle)
