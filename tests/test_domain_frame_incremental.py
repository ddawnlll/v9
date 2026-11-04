"""The validated-incremental frame path must accept exactly what frame_at accepts.

`extend_causal_frame` is what the per-bar hot path uses, so these tests pin the
only thing that matters about it: it never invents a verdict. Every case either
extends a frame whose candles are identical to what `frame_at` would build, or it
declines and the caller takes the full path.
"""
from decimal import Decimal

import pytest

from v8_next.domain.market import (
    Candle,
    build_candle_dataframe,
    extend_causal_frame,
    frame_at,
)

HOUR = 3_600_000_000_000
IID = "BTCUSDT-PERP.BINANCE"


def _candle(index: int, *, start: int | None = None, instrument: str = IID,
            available_ns: int | None = None) -> Candle:
    start_ns = index * HOUR if start is None else start
    end_ns = start_ns + HOUR
    return Candle(
        instrument, start_ns, end_ns,
        Decimal("100"), Decimal("101"), Decimal("99"), Decimal("100"), Decimal("10"),
        end_ns, end_ns if available_ns is None else available_ns, "test",
    )


def _chain(count: int) -> list[Candle]:
    return [_candle(index) for index in range(count)]


def test_extending_a_contiguous_prefix_matches_frame_at_on_every_bar() -> None:
    candles = _chain(40)
    df = build_candle_dataframe(tuple(candles))
    frame = frame_at(IID, candles[0].available_ns, tuple(candles[:1]))
    for index, candle in enumerate(candles[1:], start=1):
        extended = extend_causal_frame(frame, candle.available_ns, candle, df)
        assert extended is not None
        full = frame_at(IID, candle.available_ns, tuple(candles[: index + 1]))
        assert extended.candles == full.candles
        assert extended.continuous == full.continuous is True
        assert extended.df.equals(full.df)
        frame = extended


def test_extension_declines_when_the_tail_is_not_admissible() -> None:
    parent = frame_at(IID, 2 * HOUR, tuple(_chain(2)))
    # Duplicate/overlapping bar.
    assert extend_causal_frame(parent, 3 * HOUR, _candle(0), None) is None
    # Wrong instrument.
    assert extend_causal_frame(parent, 3 * HOUR, _candle(2, instrument="ETHUSDT-PERP.BINANCE"), None) is None
    # Not yet available at decision time.
    late = _candle(2, available_ns=3 * HOUR)
    assert extend_causal_frame(parent, 2 * HOUR, late, None) is None
    assert extend_causal_frame(parent, late.available_ns, late, None) is not None
    # Time going backwards.
    assert extend_causal_frame(parent, HOUR // 2, _candle(2), None) is None


def test_extension_declines_when_the_numeric_cache_disagrees() -> None:
    parent = frame_at(IID, 2 * HOUR, tuple(_chain(2)))
    candle = _candle(2)
    decision_ns = candle.available_ns
    df = build_candle_dataframe(tuple(_chain(4)))
    shifted = build_candle_dataframe(tuple(_chain(1) + [_candle(index, start=index * HOUR + 1) for index in range(1, 4)]))
    # The cache row for this position is not this candle: refuse rather than serve it.
    assert extend_causal_frame(parent, decision_ns, candle, shifted) is None
    # A trivially short cache is refused too.
    assert extend_causal_frame(parent, decision_ns, candle, df.head(2)) is None
    assert extend_causal_frame(parent, decision_ns, candle, df) is not None


def test_gap_is_preserved_and_reported_as_not_continuous() -> None:
    parent = frame_at(IID, 2 * HOUR, tuple(_chain(2)))
    gap = _candle(3)  # skips candle 2: start_ns is two hours after the parent's end
    extended = extend_causal_frame(parent, gap.available_ns, gap, None)
    assert extended is not None
    assert extended.continuous is False
    assert frame_at(IID, gap.available_ns, tuple(_chain(2)) + (gap,)).continuous is False


@pytest.mark.parametrize("length", [1, 2, 17])
def test_single_bar_prefixes_extend_identically(length: int) -> None:
    candles = _chain(length)
    frame = frame_at(IID, candles[-1].available_ns, tuple(candles))
    nxt = _candle(length)
    extended = extend_causal_frame(frame, nxt.available_ns, nxt, None)
    assert extended is not None
    assert extended.candles == frame.candles + (nxt,)


def test_a_gap_stays_not_continuous_for_every_later_bar() -> None:
    candles = [_candle(0), _candle(1), _candle(3), _candle(4), _candle(5)]
    df = build_candle_dataframe(tuple(candles))
    frame = frame_at(IID, candles[0].available_ns, tuple(candles[:1]))
    for index in range(1, len(candles)):
        frame = extend_causal_frame(frame, candles[index].available_ns, candles[index], df)  # type: ignore[assignment]
        full = frame_at(IID, candles[index].available_ns, tuple(candles[: index + 1]))
        assert frame.continuous == full.continuous
    # 0-1 chain, then 1H -> 3H is a gap: everything after it is not continuous.
    assert frame.continuous is False
    assert frame.candles == tuple(candles)
