"""Focused tests for the preregistered raw-signal screen.

These cover the two things a screening run must get right before its numbers
mean anything: the declared cost/accounting identity, and the causality of the
signal builders (a signal may not depend on a bar that has not closed yet).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import pytest

from v8_next.research import screen, signals


def _bars(rows: Sequence[Sequence[float]]) -> list[screen.Bar]:
    return [
        screen.Bar(
            end_ns=(index + 1) * 3_600_000_000_000,
            open=row[0],
            high=row[1],
            low=row[2],
            close=row[3],
            volume=row[4],
        )
        for index, row in enumerate(rows)
    ]


# --------------------------------------------------------------------------- #
# cost and accounting identity
# --------------------------------------------------------------------------- #
def test_slippage_moves_the_entry_and_reconciles_to_net() -> None:
    bars = _bars([(100, 100, 100, 100, 1), (100, 125, 95, 121, 1)])
    result = screen.evaluate_path(
        bars,
        entry_ns=bars[0].end_ns,
        entry=100.0,
        direction=1,
        stop=90.0,
        target=120.0,
        risk_budget_usdt=100.0,
        slippage_bps=10.0,
        target_maker=True,
    )
    assert result["outcome"] == "TARGET"
    assert result["entry_fill_price"] == pytest.approx(100.1)
    # The target is a resting limit: maker fee, no slippage on the exit.
    assert result["exit_fill_price"] == pytest.approx(120.0)
    qty = 10.0
    assert result["slippage_usdt"] == pytest.approx(qty * 0.1)
    fees = 100.1 * qty * 0.0005 + 120.0 * qty * 0.0002
    assert result["fees_usdt"] == pytest.approx(fees)
    gross = (120.0 - 100.1) * qty
    assert result["gross_usdt"] == pytest.approx(gross)
    assert result["net_usdt"] == pytest.approx(gross - fees)
    assert result["net_r"] == pytest.approx((gross - fees) / 100.0)


def test_funding_is_paid_by_longs_and_received_by_shorts() -> None:
    bars = _bars([(100, 100, 100, 100, 1), (100, 101, 99, 100, 1), (100, 101, 99, 100, 1)])
    settlement_ns = bars[0].end_ns + 1
    funding = ((settlement_ns, 0.001),)
    common: dict[str, Any] = dict(
        entry_ns=bars[0].end_ns,
        entry=100.0,
        stop=90.0,
        target=120.0,
        policy="TIME_STOP",
        max_bars=2,
        risk_budget_usdt=100.0,
        funding=funding,
    )
    long = screen.evaluate_path(bars, direction=1, **common)
    short = screen.evaluate_path(bars, direction=-1, **common)
    # 10 units at 100 notional = 1000 USDT; 0.1% of that is 1 USDT.
    assert long["funding_usdt"] == pytest.approx(1.0)
    assert short["funding_usdt"] == pytest.approx(-1.0)
    assert long["net_usdt"] == pytest.approx(long["gross_usdt"] - long["fees_usdt"] - 1.0)
    assert short["net_usdt"] == pytest.approx(short["gross_usdt"] - short["fees_usdt"] + 1.0)


def test_funding_outside_the_holding_window_is_not_charged() -> None:
    bars = _bars([(100, 100, 100, 100, 1), (100, 101, 99, 100, 1), (100, 101, 99, 100, 1)])
    late = ((bars[2].end_ns + 1, 0.001),)
    result = screen.evaluate_path(
        bars,
        entry_ns=bars[0].end_ns,
        entry=100.0,
        direction=1,
        stop=90.0,
        target=120.0,
        policy="TIME_STOP",
        max_bars=1,
        funding=late,
    )
    assert result["funding_usdt"] == 0.0


# --------------------------------------------------------------------------- #
# causality
# --------------------------------------------------------------------------- #
def _series(count: int) -> list[tuple[float, float, float, float, float]]:
    rows: list[tuple[float, float, float, float, float]] = []
    price = 100.0
    for index in range(count):
        step = 1.5 if index % 3 else -1.2
        open_ = price
        close = price + step
        rows.append((open_, max(open_, close) + 0.3, min(open_, close) - 0.3, close, 100.0))
        price = close
    return rows


@pytest.mark.parametrize("candidate_id", sorted(signals.SIGNAL_SPECS))
def test_signals_do_not_depend_on_later_bars(candidate_id: str) -> None:
    rows = _series(120)
    spec = signals.SIGNAL_SPECS[candidate_id]
    funding: list[tuple[int, float]] = []
    if spec.needs_funding:
        funding = [((index * 8 + 1) * 3_600_000_000_000, 0.0001 * ((index % 7) - 3)) for index in range(20)]
    bars = _bars(rows)
    baseline = signals.build_signals(candidate_id, bars, funding=funding)
    mutated_rows = rows[:-1] + [(rows[-1][0], rows[-1][1] + 50, rows[-1][2] - 50, rows[-1][3], 1e6)]
    mutated = signals.build_signals(candidate_id, _bars(mutated_rows), funding=funding)
    keep = len(rows) - 1
    assert [s for s in baseline if s.bar_index < keep] == [
        s for s in mutated if s.bar_index < keep
    ]


def test_volume_reference_excludes_the_current_bar() -> None:
    rows = [(100, 101, 99, 100, 100.0)] * 10
    # A spike bar whose own volume cannot raise its own reference.
    rows.append((100, 106, 100, 105.5, 500.0))
    built = signals.volume_climax_reversal(_bars(rows), window=10, multiple=4.0)
    assert [s.bar_index for s in built] == [10]
    assert built[0].direction == signals.SHORT


def test_bollinger_band_requires_a_two_sigma_close() -> None:
    rows = [(100, 101, 99, 100 + (0.5 if index % 2 else -0.5), 100.0) for index in range(20)]
    rows.append((100, 100, 94, 95, 100.0))
    built = signals.bollinger_reversion(_bars(rows), window=20, k=2.0)
    assert built == [signals.RawSignal(20, signals.LONG, "close_below_lower_band")]


def test_liquidity_sweep_requires_a_reclaim() -> None:
    rows = [(100, 110, 100, 105, 100.0)] * 6
    # Sweeps 100 but closes at 99: no reclaim, no signal.
    rows.append((105, 106, 99, 99, 100.0))
    # Sweeps 100 and closes back at 101: reclaim long.
    rows.append((99, 103, 98, 101, 100.0))
    built = signals.liquidity_sweep_reclaim(_bars(rows), lookback=6)
    assert built == [signals.RawSignal(7, signals.LONG, "sweep_low_reclaim")]


def test_unknown_candidate_and_parameter_are_refused() -> None:
    with pytest.raises(ValueError, match="unknown candidate"):
        signals.build_signals("does_not_exist", [])
    bars = _bars([(100, 101, 99, 100, 1)] * 30)
    with pytest.raises(ValueError, match="unknown parameters"):
        signals.build_signals("bollinger_reversion", bars, nonsense=1.0)


def test_tape_timestamps_and_funding_share_one_unit(tmp_path) -> None:
    """The tape records ms; the screen must expose ns so funding can be attributed."""
    tape = tmp_path / "tape.jsonl"

    def kline(open_ms: int, close_ms: int) -> str:
        return (
            '{"channel":"kline","instrument":"BTCUSDT","event_time":%d,'
            '"payload":{"open":100,"high":101,"low":99,"close":100,"volume":1,'
            '"open_time_ms":%d,"close_time_ms":%d,"closed":true}}'
            % (close_ms + 1, open_ms, close_ms)
        )

    def funding(funding_ms: int) -> str:
        return (
            '{"channel":"funding","instrument":"BTCUSDT","event_time":%d,'
            '"payload":{"funding_time_ms":%d,"funding_rate":"0.001"}}'
            % (funding_ms, funding_ms)
        )

    tape.write_text(
        "\n".join(
            [
                kline(0, 3_600_000 - 1),
                kline(3_600_000, 7_200_000 - 1),
                funding(4_000_000),
            ]
        ),
        encoding="utf-8",
    )
    bars = screen.load_bars(tape, instrument="BTCUSDT")
    settlements = screen.load_funding(tape, instrument="BTCUSDT")
    assert bars[0].end_ns == 3_600_000 * 1_000_000
    assert settlements == ((4_000_000 * 1_000_000, 0.001),)
    assert bars[0].end_ns < settlements[0][0] <= bars[1].end_ns


def _funding_case(rates: list[float]) -> list[tuple[int, float]]:
    hour_ns = 3_600_000_000_000
    return [((index * 8 + 1) * hour_ns, rate) for index, rate in enumerate(rates)]


def test_funding_extreme_fades_the_crowded_side() -> None:
    hour_ns = 3_600_000_000_000
    bars = _bars([(100, 101, 99, 100, 100.0)] * 60)
    for index, bar in enumerate(bars):
        bars[index] = screen.Bar(
            end_ns=(index + 1) * hour_ns,
            open=bar.open,
            high=bar.high,
            low=bar.low,
            close=bar.close,
            volume=bar.volume,
        )
    high = signals.funding_extreme_reversion(
        bars, _funding_case([0.0001] * 5 + [0.001]), window=5, quantile=0.10
    )
    low = signals.funding_extreme_reversion(
        bars, _funding_case([0.0001] * 5 + [-0.001]), window=5, quantile=0.10
    )
    assert [(s.direction, s.reason) for s in high] == [(signals.SHORT, "funding_crowded_long")]
    assert [(s.direction, s.reason) for s in low] == [(signals.LONG, "funding_crowded_short")]
    # The settlement is only knowable when it prints; the entry bar is later.
    settlement_ns = _funding_case([0.0001] * 6)[-1][0]
    ends = [bar.end_ns for bar in bars]
    assert high[0].bar_index == next(i for i, end in enumerate(ends) if end >= settlement_ns)


def test_constant_funding_is_never_extreme() -> None:
    hour_ns = 3_600_000_000_000
    bars = [
        screen.Bar(end_ns=(i + 1) * hour_ns, open=100, high=101, low=99, close=100, volume=1.0)
        for i in range(40)
    ]
    built = signals.funding_extreme_reversion(
        bars, _funding_case([0.0001] * 12), window=5, quantile=0.10
    )
    assert built == []
