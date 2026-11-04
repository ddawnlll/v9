from dataclasses import replace
from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from v8_next.domain.market import Candle, CausalFrame, frame_at


def candle(start=0, available=10):
    return Candle(
        "BTCUSDT-PERP.BINANCE",
        start,
        start + 10,
        Decimal(100),
        Decimal(102),
        Decimal(98),
        Decimal(101),
        Decimal(2),
        20,
        available,
        "test-only",
    )


@given(st.integers(min_value=11, max_value=1000000))
def test_future_suffix_cannot_change_frame(future_time):
    first = candle()
    future = candle(10, max(20, future_time))
    assert frame_at(first.instrument_id, 10, (first,)) == frame_at(
        first.instrument_id, 10, (first, future)
    )


def test_unknown_availability_is_not_receipt_time():
    item = candle(available=None)
    assert not frame_at(item.instrument_id, 100, (item,)).candles


def test_direct_frame_rejects_future():
    item = candle()
    with pytest.raises(ValueError, match="unavailable"):
        CausalFrame(item.instrument_id, 9, (item,))


def test_duplicate_feed_is_invariant_but_conflict_fails():
    item = candle()
    assert frame_at(item.instrument_id, 20, (item, item)).candles == (item,)
    with pytest.raises(ValueError, match="conflicting"):
        frame_at(item.instrument_id, 20, (item, replace(item, close=Decimal(100))))


def test_gap_is_explicit():
    a, b = candle(), candle(20, 30)
    assert not frame_at(a.instrument_id, 30, (a, b)).continuous
