"""PIT view fast path: identity/candle caches must not change view semantics."""

from __future__ import annotations

from decimal import Decimal

import pytest

from v8_next.domain.market import Candle
from v8_next.domain.observation import (
    Observation,
    ObservationKind,
    QualityState,
    build_view,
    observation_id,
)

START = 0
END = 3_600_000_000_000


def observation(start: int) -> Observation:
    return Observation(
        instrument_id="BTCUSDT-PERP.BINANCE",
        kind=ObservationKind.BAR,
        event_time=start,
        available_at=start + END,
        received_at=start + END,
        published_at=start + END,
        source="cache-test",
        sequence=start // END,
        quality=QualityState.OK,
        payload={
            "start_ns": start,
            "end_ns": start + END,
            "open": "100",
            "high": "101",
            "low": "99",
            "close": "100.5",
            "volume": "1",
            "source_hash": "cache-test",
        },
    )


def test_observation_id_is_stable_for_the_same_immutable_object() -> None:
    item = observation(START)
    assert observation_id(item) == observation_id(item)


def test_fast_path_matches_the_copying_view() -> None:
    items = tuple(observation(index * END) for index in range(5))
    cut = 5 * END
    default = build_view(items, cut_time_ns=cut)
    fast = build_view(items, cut_time_ns=cut, copy_inputs=False, assume_ordered=True)

    assert fast.observation_ids() == default.observation_ids()
    assert fast.bars("BTCUSDT-PERP.BINANCE") == default.bars("BTCUSDT-PERP.BINANCE")
    assert fast.quality("BTCUSDT-PERP.BINANCE") is default.quality("BTCUSDT-PERP.BINANCE")
    assert fast.max_input_available_at == default.max_input_available_at


def test_default_view_is_still_a_snapshot_of_the_caller_payload() -> None:
    item = observation(START)
    view = build_view((item,), cut_time_ns=END)
    item.payload["close"] = "999"
    bars = view.bars("BTCUSDT-PERP.BINANCE")
    assert bars and bars[0].close == Decimal("100.5")


def test_contradictory_duplicates_still_fail_closed() -> None:
    first = observation(START)
    second = first.model_copy(update={"payload": {**first.payload, "close": "100.6"}})
    with pytest.raises(ValueError, match="CONTRADICTORY"):
        build_view((first, second), cut_time_ns=END)


def test_bar_payload_still_materialises_a_candle() -> None:
    view = build_view((observation(START),), cut_time_ns=END)
    bars = view.bars("BTCUSDT-PERP.BINANCE")
    assert len(bars) == 1
    assert isinstance(bars[0], Candle)
    assert bars[0].available_ns == END
