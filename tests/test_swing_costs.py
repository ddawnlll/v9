"""Cost-lane tests.

MECHANICS ONLY where noted: the funding rows in the unit tests are hand-written so the
sign, the window rule and the coverage accounting can be probed exactly. The last test
runs against the real tape and is a measurement, not a mechanics check.
"""

from __future__ import annotations

import os
from decimal import Decimal
from pathlib import Path

import pytest

from v8_next.economics.swing_costs import (
    EXECUTION_SHORTFALL_MISSING,
    FUNDING_APPLIED,
    FUNDING_CONFLICTING,
    FUNDING_MISSING_NO_ROWS,
    FUNDING_OFF_GRID,
    FUNDING_PARTIAL,
    SWING_COST_MODEL_VERSION,
    CampaignCost,
    SlippageModel,
    aggregate,
    campaign_cost,
    funding_for_position,
    modelled_slippage_return,
)
from v8_next.evaluation.multitape import FundingRow, load_multitape

HOUR_MS = 3_600_000
HOUR_NS = 3_600 * 10**9
MULTI_TAPE = Path(os.environ.get(
    "V8_MULTI_TAPE_PATH",
    str(Path(__file__).resolve().parents[2] / "research/tape/multi-1h-4y/tape.jsonl"),
))


def row(instrument: str, hours_from_epoch_start: int, rate: str) -> FundingRow:
    """MECHANICS ONLY: one hand-written settlement on the 8h venue grid."""
    return FundingRow(
        instrument=instrument,
        funding_time_ms=hours_from_epoch_start * HOUR_MS,
        funding_rate=Decimal(rate),
        interval_hours=8.0,
    )


# --- funding sign and window rule (MECHANICS ONLY) ------------------------------


def test_long_pays_positive_rate_and_short_receives_it() -> None:
    rows = [row("BTCUSDT", 8, "0.0005")]
    long_result = funding_for_position(
        rows, instrument="BTCUSDT", direction="LONG", entry_ns=0, exit_ns=24 * HOUR_NS
    )
    short_result = funding_for_position(
        rows, instrument="BTCUSDT", direction="SHORT", entry_ns=0, exit_ns=24 * HOUR_NS
    )
    assert long_result.cost_return == pytest.approx(-0.0005)
    assert short_result.cost_return == pytest.approx(+0.0005)
    # the cost of one side is the income of the other: funding moves value, it does not create it
    assert long_result.cost_return + short_result.cost_return == pytest.approx(0.0)


def test_settlement_window_is_open_at_entry_and_closed_at_exit() -> None:
    rows = [row("BTCUSDT", 8, "0.001"), row("BTCUSDT", 16, "0.001"), row("BTCUSDT", 24, "0.001")]
    # entry exactly at settlement 8h: not charged; exit exactly at 24h: charged
    result = funding_for_position(
        rows,
        instrument="BTCUSDT",
        direction="LONG",
        entry_ns=8 * HOUR_NS,
        exit_ns=24 * HOUR_NS,
    )
    assert [charge.settlement_ms for charge in result.charges] == [16 * HOUR_MS, 24 * HOUR_MS]
    assert result.applied == 2
    assert result.expected == 2
    assert result.status == FUNDING_APPLIED


def test_other_instruments_are_not_charged_to_this_position() -> None:
    rows = [row("ETHUSDT", 8, "0.001")]
    result = funding_for_position(
        rows, instrument="BTCUSDT", direction="LONG", entry_ns=0, exit_ns=24 * HOUR_NS
    )
    assert result.applied == 0
    assert result.status == FUNDING_MISSING_NO_ROWS


# --- coverage accounting (MECHANICS ONLY) ---------------------------------------


def test_grid_expectation_counts_venue_settlements_not_entry_aligned_ones() -> None:
    # 25h holding from 1h past the epoch spans settlements at 8h, 16h and 24h -> 3
    result = funding_for_position(
        [row("BTCUSDT", 8, "0.0001"), row("BTCUSDT", 16, "0.0001"), row("BTCUSDT", 24, "0.0001")],
        instrument="BTCUSDT",
        direction="LONG",
        entry_ns=1 * HOUR_NS,
        exit_ns=25 * HOUR_NS,
    )
    assert result.expected == 3
    assert result.applied == 3
    assert result.complete is True


def test_partial_coverage_is_reported_as_partial_not_complete_and_not_zero() -> None:
    rows = [row("BTCUSDT", 8, "0.0004")]  # 16h and 24h are absent from the tape
    result = funding_for_position(
        rows, instrument="BTCUSDT", direction="LONG", entry_ns=0, exit_ns=25 * HOUR_NS
    )
    assert result.expected == 3
    assert result.applied == 1
    assert result.status == FUNDING_PARTIAL
    assert result.complete is False
    # the measured part is still reported; it is not rounded down to zero
    assert result.cost_return == pytest.approx(-0.0004)


def test_a_span_without_a_settlement_is_applied_with_no_charge() -> None:
    result = funding_for_position(
        [], instrument="BTCUSDT", direction="LONG", entry_ns=1 * HOUR_NS, exit_ns=3 * HOUR_NS
    )
    assert result.expected == 0
    assert result.status == FUNDING_APPLIED
    assert result.cost_return == 0.0


def test_dropped_tape_rows_are_carried_into_the_result() -> None:
    result = funding_for_position(
        [], instrument="BTCUSDT", direction="LONG", entry_ns=0, exit_ns=HOUR_NS, dropped_rows_in_tape=7
    )
    assert result.dropped_rows_in_tape == 7
    assert result.status == FUNDING_PARTIAL
    assert result.complete is False


def test_duplicate_settlements_are_charged_once_and_missing_ones_are_not_hidden() -> None:
    duplicate_only = funding_for_position(
        [row("BTCUSDT", 8, "0.001"), row("BTCUSDT", 8, "0.001")],
        instrument="BTCUSDT",
        direction="LONG",
        entry_ns=0,
        exit_ns=16 * HOUR_NS,
    )
    assert duplicate_only.applied == 1
    assert duplicate_only.expected == 2
    assert duplicate_only.status == FUNDING_PARTIAL
    assert duplicate_only.cost_return == pytest.approx(-0.001)

    complete_with_duplicate = funding_for_position(
        [row("BTCUSDT", 8, "0.001"), row("BTCUSDT", 8, "0.001"), row("BTCUSDT", 16, "0.001")],
        instrument="BTCUSDT",
        direction="LONG",
        entry_ns=0,
        exit_ns=16 * HOUR_NS,
    )
    assert complete_with_duplicate.applied == 2
    assert complete_with_duplicate.expected == 2
    assert complete_with_duplicate.status == FUNDING_APPLIED
    assert complete_with_duplicate.cost_return == pytest.approx(-0.002)


def test_conflicting_rates_for_one_settlement_fail_closed() -> None:
    result = funding_for_position(
        [row("BTCUSDT", 8, "0.001"), row("BTCUSDT", 8, "0.002")],
        instrument="BTCUSDT",
        direction="LONG",
        entry_ns=0,
        exit_ns=16 * HOUR_NS,
    )
    assert result.status == FUNDING_CONFLICTING
    assert result.complete is False
    assert result.applied == 1


def test_off_grid_settlement_is_not_reported_complete() -> None:
    result = funding_for_position(
        [row("BTCUSDT", 7, "0.001"), row("BTCUSDT", 15, "0.001")],
        instrument="BTCUSDT",
        direction="LONG",
        entry_ns=0,
        exit_ns=16 * HOUR_NS,
    )
    assert result.applied == 2
    assert result.expected == 2
    assert result.status == FUNDING_OFF_GRID
    assert result.complete is False


def test_off_grid_settlement_with_no_expected_grid_points_is_not_complete() -> None:
    result = funding_for_position(
        [FundingRow("BTCUSDT", HOUR_MS // 2, Decimal("0.001"), 8.0)],
        instrument="BTCUSDT",
        direction="LONG",
        entry_ns=0,
        exit_ns=HOUR_NS,
    )
    assert result.applied == 1
    assert result.expected == 0
    assert result.status == FUNDING_OFF_GRID
    assert result.complete is False


# --- slippage is modelled, never measured (MECHANICS ONLY) ----------------------


def test_slippage_models_are_adverse_and_monotone() -> None:
    zero = modelled_slippage_return(
        SlippageModel(model="none"), entry_price=Decimal("100"), entry_bar_low=None, entry_bar_high=None
    )
    assert zero == 0.0
    small = modelled_slippage_return(
        SlippageModel(model="ticks", ticks=1, tick_size=Decimal("0.1")),
        entry_price=Decimal("100"),
        entry_bar_low=None,
        entry_bar_high=None,
    )
    large = modelled_slippage_return(
        SlippageModel(model="ticks", ticks=5, tick_size=Decimal("0.1")),
        entry_price=Decimal("100"),
        entry_bar_low=None,
        entry_bar_high=None,
    )
    assert small < 0 and large < small


def test_range_fraction_uses_the_measured_bar_range() -> None:
    value = modelled_slippage_return(
        SlippageModel(model="entry_bar_range_fraction", range_fraction=0.25),
        entry_price=Decimal("100"),
        entry_bar_low=Decimal("98"),
        entry_bar_high=Decimal("102"),
    )
    # 25% of a 4-point range on a 100-point price, both legs
    assert value == pytest.approx(-0.02)


def test_range_fraction_needs_the_bar_range() -> None:
    with pytest.raises(ValueError):
        modelled_slippage_return(
            SlippageModel(model="entry_bar_range_fraction", range_fraction=0.1),
            entry_price=Decimal("100"),
            entry_bar_low=None,
            entry_bar_high=None,
        )


def test_unknown_slippage_model_is_refused() -> None:
    with pytest.raises(ValueError):
        SlippageModel(model="mid_price_fantasy")  # type: ignore[arg-type]


# --- separation and provenance of the campaign fields (MECHANICS ONLY) ----------


class _Decision:
    policy_id = "plain_swing"
    instrument_id = "BTCUSDT"
    direction = "LONG"
    decision_ns = 0
    entry_reference = Decimal("100")


class _Outcome:
    exit_ns = 24 * HOUR_NS
    gross_return = 0.01
    fee_cost_return = -0.001


def test_measured_and_modelled_fields_never_merge() -> None:
    cost = campaign_cost(
        decision=_Decision(),
        outcome=_Outcome(),
        funding_rows=[
            row("BTCUSDT", 8, "0.0002"),
            row("BTCUSDT", 16, "0.0002"),
            row("BTCUSDT", 24, "0.0002"),
        ],
        slippage=SlippageModel(model="ticks", ticks=2, tick_size=Decimal("0.1")),
    )
    assert isinstance(cost, CampaignCost)
    assert cost.gross_return == 0.01
    assert cost.fee_cost_return == -0.001
    assert cost.funding_cost_return == pytest.approx(-0.0006)
    assert cost.funding_status == FUNDING_APPLIED
    assert cost.net_return_measured_only == pytest.approx(0.01 - 0.001 - 0.0006)
    assert cost.slippage_modelled_return < 0
    assert cost.net_return_with_modelled_slippage == pytest.approx(
        cost.net_return_measured_only + cost.slippage_modelled_return
    )
    # the measured shortfall is absent, never replaced by the modelled number
    assert cost.execution_shortfall_measured_return is None
    assert cost.execution_shortfall_status == EXECUTION_SHORTFALL_MISSING
    assert cost.model_version == SWING_COST_MODEL_VERSION


def test_aggregate_keeps_fields_apart_and_names_what_is_incomplete() -> None:
    complete = campaign_cost(
        decision=_Decision(),
        outcome=_Outcome(),
        funding_rows=[
            row("BTCUSDT", 8, "0.0002"),
            row("BTCUSDT", 16, "0.0002"),
            row("BTCUSDT", 24, "0.0002"),
        ],
        slippage=SlippageModel(model="none"),
    )
    partial = campaign_cost(
        decision=_Decision(),
        outcome=_Outcome(),
        funding_rows=[],
        slippage=SlippageModel(model="none"),
    )
    totals = aggregate([complete, partial])
    assert totals["campaigns"] == 2
    assert totals["funding_incomplete_campaigns"] == 1
    assert totals["funding_settlements_applied"] == 3
    # both campaigns span 24h -> 3 venue settlements each; the second has no rows at all
    assert totals["funding_settlements_expected"] == 6
    assert totals["slippage_modelled_return_sum"] == 0.0
    assert totals["execution_shortfall_measured_return_sum"] is None
    assert totals["execution_shortfall_status"] == EXECUTION_SHORTFALL_MISSING


def test_breakdown_is_deterministic() -> None:
    kwargs = {
        "decision": _Decision(),
        "outcome": _Outcome(),
        "funding_rows": [row("BTCUSDT", 8, "0.0002")],
        "slippage": SlippageModel(model="ticks", ticks=1, tick_size=Decimal("0.1")),
    }
    first = campaign_cost(**kwargs).as_dict()  # type: ignore[arg-type]
    second = campaign_cost(**kwargs).as_dict()  # type: ignore[arg-type]
    assert first == second


def test_funding_matches_the_tape_symbol_against_a_venue_qualified_id() -> None:
    """MECHANICS ONLY: the funding channel says BTCUSDT, the frame says BTCUSDT-PERP.BINANCE."""
    result = funding_for_position(
        [row("BTCUSDT", 8, "0.0003")],
        instrument="BTCUSDT-PERP.BINANCE",
        direction="LONG",
        entry_ns=0,
        exit_ns=24 * HOUR_NS,
    )
    assert result.applied == 1
    assert result.cost_return == pytest.approx(-0.0003)
    # and a genuinely different instrument is still not charged to this position
    other = funding_for_position(
        [row("ETHUSDT", 8, "0.0003")],
        instrument="BTCUSDT-PERP.BINANCE",
        direction="LONG",
        entry_ns=0,
        exit_ns=24 * HOUR_NS,
    )
    assert other.applied == 0


def test_replayed_fee_magnitude_is_carried_as_a_signed_cost() -> None:
    """MECHANICS ONLY: the replay reports a positive fee magnitude and subtracts it."""

    class _PositiveFeeOutcome(_Outcome):
        fee_cost_return = 0.001

    cost = campaign_cost(
        decision=_Decision(),
        outcome=_PositiveFeeOutcome(),
        funding_rows=[],
        slippage=SlippageModel(model="none"),
    )
    assert cost.fee_cost_return == -0.001
    assert cost.net_return_measured_only == pytest.approx(cost.gross_return - 0.001)


# --- real tape measurement (not mechanics) --------------------------------------


@pytest.mark.slow
@pytest.mark.skipif(not MULTI_TAPE.is_file(), reason="four-year multi tape not on this checkout")
def test_real_tape_funding_is_bound_and_mark_absence_is_declared() -> None:
    tape = load_multitape(MULTI_TAPE)
    assert tape.funding, "the tape carries a funding channel"
    btc_rows = [r for r in tape.funding if r.instrument == "BTCUSDT"]
    assert len(btc_rows) > 1000
    # a real 25h holding window straddling four venue settlements (the row itself, +8h,
    # +16h, +24h): the count is reported against the grid, and the tape satisfies it
    start_ms = btc_rows[5].funding_time_ms
    result = funding_for_position(
        tape.funding,
        instrument="BTCUSDT",
        direction="LONG",
        entry_ns=(start_ms - 1 * HOUR_MS) * 1_000_000,
        exit_ns=(start_ms + 24 * HOUR_MS) * 1_000_000,
    )
    assert result.expected == result.applied == 4
    assert result.status == FUNDING_APPLIED
    assert result.cost_return != 0.0
    # no mark price in this tape: the measured shortfall stays absent
    assert tape.mark_price_absent is True
