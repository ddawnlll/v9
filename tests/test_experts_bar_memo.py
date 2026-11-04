"""Per-bar indicator memo must return exactly what the unwrapped function returns.

The optimisation is only allowed to skip *duplicate* work inside one bar, so
these tests compare memoised and direct results value for value, and pin that a
value computed for one bar can never be served for another.
"""
from decimal import Decimal

from v8_next.domain.market import Candle, frame_at
from v8_next.experts.features import (
    _ACTIVE_LAZY,
    _LazyBundle,
    close_series,
    macd_line,
    significant_swings,
    trend_emas,
    wilder_rsi,
)
from v8_next.experts.patterns import pattern_pivots

HOUR = 3_600_000_000_000
IID = "BTCUSDT-PERP.BINANCE"


def _candle(index: int, close: str) -> Candle:
    price = Decimal(close)
    return Candle(
        IID, index * HOUR, (index + 1) * HOUR,
        price, price + 1, price - 1, price, Decimal("10"),
        (index + 1) * HOUR, (index + 1) * HOUR, "test",
    )


def _frame(length: int, base: str = "100") -> "object":
    candles = tuple(
        _candle(index, str(Decimal(base) + (index % 7) - 3)) for index in range(length)
    )
    return frame_at(IID, candles[-1].available_ns, candles)


def test_memoised_values_match_the_unwrapped_results() -> None:
    frame = _frame(80)
    direct = (
        wilder_rsi(close_series(frame)),
        trend_emas(frame),
        significant_swings(frame),
        macd_line(frame),
        pattern_pivots(frame, high=True),
        pattern_pivots(frame, high=False),
    )
    token = _ACTIVE_LAZY.set(_LazyBundle(frame))
    try:
        memoised = (
            wilder_rsi(close_series(frame)),
            trend_emas(frame),
            significant_swings(frame),
            macd_line(frame),
            pattern_pivots(frame, high=True),
            pattern_pivots(frame, high=False),
        )
        # Asking twice inside the same bar is served from the memo.
        assert wilder_rsi(close_series(frame)) is memoised[0]
        assert pattern_pivots(frame, high=True) is memoised[4]
    finally:
        _ACTIVE_LAZY.reset(token)
    assert memoised == direct


def test_a_value_never_crosses_a_bar_boundary() -> None:
    first, second = _frame(80), _frame(80, base="200")
    token = _ACTIVE_LAZY.set(_LazyBundle(first))
    try:
        from_first = significant_swings(first)
    finally:
        _ACTIVE_LAZY.reset(token)
    token = _ACTIVE_LAZY.set(_LazyBundle(second))
    try:
        from_second = significant_swings(second)
    finally:
        _ACTIVE_LAZY.reset(token)
    assert from_first == significant_swings_direct(first)
    assert from_second == significant_swings_direct(second)


def significant_swings_direct(frame) -> tuple[int | None, int | None]:
    token = _ACTIVE_LAZY.set(None)
    try:
        return significant_swings(frame)
    finally:
        _ACTIVE_LAZY.reset(token)
