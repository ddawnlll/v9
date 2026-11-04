"""MECHANICS ONLY: boundary-time funding attribution, no look-ahead."""

from decimal import Decimal

from v8_next.adapters.portfolio_backtest import (
    _causal_funding_proxy,
    _plan_funding_events,
)
from v8_next.evaluation.multitape import Candle, FundingRow

H = 3_600 * 10**9
BASE = 1_700_000_000 * 10**9


def candle(index: int, close: str) -> Candle:
    start = BASE + index * H
    value = Decimal(close)
    return Candle(
        instrument_id="BTCUSDT-PERP.BINANCE",
        start_ns=start,
        end_ns=start + H,
        open=value,
        high=value,
        low=value,
        close=value,
        volume=Decimal("1"),
        received_ns=start + H,
        available_ns=start + H,
        source_hash="mechanics-only",
    )


def funding(boundary_index: int, rate: str, received_ns: int | None) -> FundingRow:
    """A settlement whose venue boundary is the START of bar ``boundary_index``."""
    return FundingRow(
        instrument="BTCUSDT",
        funding_time_ms=(BASE + boundary_index * H) // 1_000_000,
        funding_rate=Decimal(rate),
        interval_hours=8.0,
        received_ns=received_ns,
    )


BARS = (candle(0, "100"), candle(1, "110"), candle(2, "120"), candle(3, "130"))
LEGS = {"BTCUSDT": BARS}
START, END = BARS[0].start_ns, BARS[-1].end_ns
BOUNDARY = BARS[2].start_ns


def test_delayed_settlement_is_charged_at_receipt_not_boundary() -> None:
    prepared, counts = _plan_funding_events(
        (funding(2, "0.001", BOUNDARY + H),), LEGS, START, END
    )
    assert counts["receipt_shifted"] == 1
    assert len(prepared) == 1 and prepared[0][0] == BOUNDARY + H
    assert prepared[0][1] == BOUNDARY  # the venue boundary is still recorded


def test_charge_known_only_after_window_is_not_applied() -> None:
    prepared, counts = _plan_funding_events(
        (funding(2, "0.001", END + H),), LEGS, START, END
    )
    assert prepared == []
    assert counts["out_of_window"] == 1
    assert counts["receipt_shifted"] == 0


def test_unknown_receipt_is_reported_not_assumed_causal() -> None:
    prepared, counts = _plan_funding_events((funding(2, "0.001", None),), LEGS, START, END)
    assert counts["receipt_unknown"] == 1
    assert prepared[0][0] == BOUNDARY


def test_each_settlement_is_charged_once() -> None:
    rows = (funding(2, "0.001", None), funding(2, "0.001", None))
    prepared, counts = _plan_funding_events(rows, LEGS, START, END)
    assert len(prepared) == 1
    assert counts["duplicate_boundary"] == 1
    assert counts["receipt_unknown"] == 1


def test_mark_never_uses_a_future_bar() -> None:
    assert next(c.close for c in BARS if c.end_ns >= BOUNDARY) == Decimal("110")
    assert _causal_funding_proxy(BARS, BOUNDARY) == Decimal("110")
    # One bar of staleness is the documented bound; older is refused, not carried.
    assert _causal_funding_proxy(BARS, BOUNDARY + 3 * H) == Decimal("130")
    assert _causal_funding_proxy(BARS, BOUNDARY + 4 * H) is None


def test_irregular_off_grid_boundary_uses_the_previous_close() -> None:
    # A boundary that is not a bar close (shifted/irregular tape) must still pick
    # the last close known at that instant, never the bar that contains it.
    off_grid = BARS[2].start_ns + H // 3
    assert _causal_funding_proxy(BARS, off_grid) == Decimal("110")
    assert _causal_funding_proxy(BARS, BARS[0].start_ns + 1) is None


def test_unknown_instrument_and_out_of_window_rows_are_counted_not_charged() -> None:
    foreign = FundingRow("ETHUSDT", (BASE + 2 * H) // 1_000_000, Decimal("0.001"), 8.0, None)
    outside = FundingRow("BTCUSDT", (BASE + 9 * H) // 1_000_000, Decimal("0.001"), 8.0, None)
    prepared, counts = _plan_funding_events((foreign, outside), LEGS, START, END)
    assert prepared == []
    assert counts["unknown_leg"] == 1
    assert counts["out_of_window"] == 1
