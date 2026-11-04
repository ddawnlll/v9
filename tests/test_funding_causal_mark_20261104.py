"""MECHANICS ONLY: causality of a bounded historical OHLC mark proxy."""

from decimal import Decimal

from v8_next.adapters.portfolio_backtest import _causal_funding_proxy
from v8_next.domain.market import Candle


def candle(start: int, end: int, close: str, available: int | None = None) -> Candle:
    price = Decimal(close)
    return Candle("BTCUSDT", start, end, price, price, price, price, Decimal(1), end, available, "fixture")


def test_proxy_never_uses_future_close_or_unavailable_candle() -> None:
    bars = (candle(0, 10, "10"), candle(10, 20, "999"))
    assert _causal_funding_proxy(bars, 10) == Decimal("10")
    # The original expression selects the future bar; changing it changes that result.
    assert next(c.close for c in bars if c.end_ns >= 15) == Decimal("999")
    assert _causal_funding_proxy(bars, 15) == Decimal("10")
    assert _causal_funding_proxy((bars[0], candle(10, 20, "100000")), 15) == Decimal("10")
    assert _causal_funding_proxy((candle(0, 10, "10", 11),), 10) is None


def test_proxy_missing_or_stale_is_unavailable() -> None:
    bars = (candle(0, 10, "10"), candle(30, 45, "20"))
    assert _causal_funding_proxy(bars, 9) is None
    assert _causal_funding_proxy(bars, 21) is None
    assert _causal_funding_proxy(bars, 50) == Decimal("20")
    assert _causal_funding_proxy(bars, 61) is None
