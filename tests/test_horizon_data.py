"""Causal higher-timeframe aggregation must never invent or leak a bar."""

from __future__ import annotations

from decimal import Decimal

import pytest

from v8_next.domain.horizon_data import (
    aggregate_to_timeframe,
    build_horizon_series,
    infer_timeframe,
)
from v8_next.domain.horizons import Timeframe
from v8_next.domain.market import Candle

H1 = Timeframe.H1.duration_ns
BASE = 1_735_689_600_000_000_000  # 2025-01-01T00:00Z


def _h1(index: int, *, available_offset: int = 0, close: str | None = None) -> Candle:
    start = BASE + index * H1
    price = Decimal(close if close is not None else str(100 + index))
    return Candle(
        instrument_id="BTCUSDT-PERP.BINANCE",
        start_ns=start,
        end_ns=start + H1,
        open=price,
        high=price + Decimal("2"),
        low=price - Decimal("2"),
        close=price + Decimal("1"),
        volume=Decimal("10"),
        received_ns=start + H1,
        available_ns=start + H1 + available_offset,
        source_hash=f"src-{index}",
    )


def test_h1_aggregates_into_one_complete_h4_bar() -> None:
    candles = tuple(_h1(i) for i in range(4))
    h4 = aggregate_to_timeframe(candles, Timeframe.H4)
    assert len(h4) == 1
    bar = h4[0]
    assert bar.start_ns == BASE
    assert bar.end_ns == BASE + Timeframe.H4.duration_ns
    assert bar.open == candles[0].open
    assert bar.close == candles[-1].close
    assert bar.high == max(c.high for c in candles)
    assert bar.low == min(c.low for c in candles)
    assert bar.volume == sum((c.volume for c in candles), Decimal(0))
    # Knowable only when the slowest input is.
    assert bar.available_ns == max(c.available_ns for c in candles)
    assert infer_timeframe(bar) is Timeframe.H4


def test_missing_source_bar_drops_the_whole_bucket() -> None:
    candles = tuple(_h1(i) for i in range(4) if i != 2)
    assert aggregate_to_timeframe(candles, Timeframe.H4) == ()


def test_partial_source_bar_is_not_aggregated() -> None:
    candles = list(_h1(i) for i in range(4))
    partial = candles[2]
    candles[2] = Candle(
        instrument_id=partial.instrument_id,
        start_ns=partial.start_ns,
        end_ns=partial.end_ns - 1,
        open=partial.open,
        high=partial.high,
        low=partial.low,
        close=partial.close,
        volume=partial.volume,
        received_ns=partial.received_ns,
        available_ns=partial.available_ns,
        source_hash="partial",
    )
    assert aggregate_to_timeframe(candles, Timeframe.H4) == ()


def test_delayed_availability_moves_the_aggregate_later() -> None:
    candles = tuple(_h1(i, available_offset=0 if i < 3 else 10**9) for i in range(4))
    bar = aggregate_to_timeframe(candles, Timeframe.H4)[0]
    assert bar.available_ns == BASE + 4 * H1 + 10**9


def test_h1_tape_cannot_be_split_into_m15() -> None:
    series = build_horizon_series(tuple(_h1(i) for i in range(8)))
    assert series.base is Timeframe.H1
    assert Timeframe.M15 not in series.series
    assert series.available_timeframes() == (Timeframe.H1, Timeframe.H4)


def test_aggregation_is_one_way_only() -> None:
    with pytest.raises(ValueError):
        aggregate_to_timeframe(tuple(_h1(i) for i in range(4)), Timeframe.M15)


def test_gapped_series_skips_only_the_affected_bucket() -> None:
    candles = tuple(_h1(i) for i in range(8) if i != 5)
    h4 = aggregate_to_timeframe(candles, Timeframe.H4)
    assert [bar.start_ns for bar in h4] == [BASE]
