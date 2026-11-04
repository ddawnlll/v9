"""#407 — per-trade PnL attribution in the receipt-bound trades artifact.

The bound ``native_trades`` JSONL must carry, on every row, THAT trade's own
realized PnL in the single declared unit. This test pins the defect class the
card names: a row whose ``pnl`` was the PnL of a *different* campaign (index
replication), a dimensionless fraction in the same column, or a fabricated zero
in place of an unmeasured value.

Evidence classes:

* **mechanics** — synthetic position/close dicts only (no evaluation, zero
  economic weight): colliding ``position_id`` fixtures, differing sides, the
  tape-range bound, and absence-instead-of-zero publication.
* **evaluative** — a real 500-bar BTCUSDT benchmark through ``BenchmarkRunner``:
  the written artifact itself is re-read and checked for distinct per-trade PnL,
  the range bound and agreement with the accounting aggregate. Skips when the
  real tape is absent.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from v8_next.adapters.expert_strategy import ExpertStrategyConfig
from v8_next.domain.market import Candle
from v8_next.evaluation.economic_benchmark import campaign_accounting
from v8_next.evaluation.gate_resolution import DEFAULT_TAPE_PATH, load_tape_candles
from v8_next.evaluation.runner import (
    PNL_UNMEASURED_OPEN,
    PNL_UNMEASURED_OUT_OF_RANGE,
    PNL_UNMEASURED_UNPARSABLE,
    BenchmarkCase,
    BenchmarkRunner,
    attribute_campaign_pnl,
    instrument_price_ranges,
    summarize_pnl_attribution,
)

INSTRUMENT = "BTCUSDT-PERP.BINANCE"
REAL_BARS = 500


def _open(
    position_id: str,
    event_ns: int,
    *,
    side: str = "LONG",
    qty: str = "1",
    px: str = "100",
    instrument: str = INSTRUMENT,
) -> dict[str, object]:
    return {
        "position_id": position_id,
        "instrument_id": instrument,
        "side": side,
        "quantity": qty,
        "avg_px_open": px,
        "event_ns": event_ns,
    }


def _close(
    position_id: str, event_ns: int, pnl: str, *, instrument: str = INSTRUMENT
) -> dict[str, object]:
    return {
        "position_id": position_id,
        "instrument_id": instrument,
        "realized_pnl": f"{pnl} USDT",
        "event_ns": event_ns,
    }


def _candle(start_ns: int, high: str, low: str) -> Candle:
    high_d, low_d = Decimal(high), Decimal(low)
    close_d = (high_d + low_d) / 2
    return Candle(
        instrument_id=INSTRUMENT,
        start_ns=start_ns,
        end_ns=start_ns + 3_600_000_000_000,
        open=close_d,
        high=high_d,
        low=low_d,
        close=close_d,
        volume=Decimal("1"),
        received_ns=start_ns + 3_600_000_000_000,
        available_ns=None,
        source_hash="fixture",
    )


# --------------------------------------------------------------------------- #
# mechanics
# --------------------------------------------------------------------------- #
def test_colliding_position_id_never_replicates_a_foreign_pnl() -> None:
    """MECHANICS ONLY (#407 acceptance iii): NETTING reuses the id.

    Under the defect, index-aligned rows handed the first campaign's PnL to
    every later row on the same slot (the observed artifact showed one value
    repeated across rows with different sides and fill times).
    """
    opened = [_open("P", 1_000), _open("P", 3_000)]
    closed = [_close("P", 2_000, "5"), _close("P", 4_000, "-3")]

    rows = attribute_campaign_pnl(opened, closed, pnl_unit="USDT")

    assert [row["pnl"] for row in rows] == [5.0, -3.0]
    # the historical replication would have produced [5.0, 5.0]
    assert [row["pnl"] for row in rows] != [5.0, 5.0]
    # each row is bound to the close that consumed it, in chronological order
    assert [row["close_time_ns"] for row in rows] == [2_000, 4_000]
    assert all(row["pnl_unit"] == "USDT" for row in rows)


def test_same_id_different_side_and_time_get_their_own_pnl() -> None:
    """MECHANICS ONLY (#407 acceptance i/iii): distinct side + fill time.

    Two positions on the same reused ``position_id``, opened in opposite
    directions at different times, must not share a PnL value.
    """
    opened = [_open("P", 1_000, side="LONG"), _open("P", 5_000, side="SHORT")]
    closed = [_close("P", 2_000, "12.5"), _close("P", 6_000, "-7.25")]

    rows = attribute_campaign_pnl(opened, closed, pnl_unit="USDT")

    assert len(rows) == 2
    assert rows[0]["side"] == "LONG" and rows[1]["side"] == "SHORT"
    assert rows[0]["fill_time_ns"] != rows[1]["fill_time_ns"]
    assert rows[0]["pnl"] != rows[1]["pnl"]
    # the defect signature: same pnl value under different side/fill time
    pairs = {(row["pnl"], row["side"], row["fill_time_ns"]) for row in rows}
    assert len(pairs) == 2


def test_instrument_isolation_under_a_shared_id() -> None:
    """MECHANICS ONLY: the same id on two instruments is two campaigns."""
    opened = [
        _open("P", 1_000, instrument=INSTRUMENT),
        _open("P", 1_000, instrument="ETHUSDT-PERP.BINANCE"),
    ]
    closed = [
        _close("P", 2_000, "5", instrument=INSTRUMENT),
        _close("P", 2_000, "7", instrument="ETHUSDT-PERP.BINANCE"),
    ]

    rows = attribute_campaign_pnl(opened, closed, pnl_unit="USDT")

    assert sorted(row["pnl"] for row in rows) == [5.0, 7.0]


def test_unclosed_campaign_does_not_shift_later_rows() -> None:
    """MECHANICS ONLY (#407 acceptance i): the replication mechanism.

    Completed campaigns and opened positions are two different lists: with one
    campaign still open, an index-aligned assignment hands every later row its
    predecessor's PnL and leaves the wrong row empty.
    """
    opened = [_open("A", 1_000), _open("B", 3_000), _open("C", 5_000)]
    closed = [_close("B", 4_000, "5"), _close("C", 6_000, "-3")]

    rows = attribute_campaign_pnl(opened, closed, pnl_unit="USDT")
    by_id = {row["trade_id"]: row for row in rows}

    assert by_id["A"]["pnl"] is None
    assert by_id["A"]["pnl_unmeasured_reason"] == PNL_UNMEASURED_OPEN
    assert by_id["B"]["pnl"] == 5.0
    assert by_id["C"]["pnl"] == -3.0
    # the pre-#407 index zip (realized_pnl_usdt[idx]) would have produced
    # A=5.0, B=-3.0, C=None: two rows carrying a foreign campaign's PnL.
    assert [row["pnl"] for row in rows] != [5.0, -3.0, None]


def test_unrealized_and_unparsable_rows_are_absent_not_zero() -> None:
    """MECHANICS ONLY: absence is published as null + a named reason."""
    opened = [_open("OPEN", 1_000), _open("BAD", 1_000), _open("OK", 1_000)]
    closed = [
        {"position_id": "BAD", "instrument_id": INSTRUMENT, "realized_pnl": "?",
         "event_ns": 2_000},
        _close("OK", 2_000, "4"),
    ]

    rows = attribute_campaign_pnl(opened, closed, pnl_unit="USDT")
    by_id = {row["trade_id"]: row for row in rows}

    assert by_id["OPEN"]["pnl"] is None
    assert by_id["OPEN"]["pnl_unmeasured_reason"] == PNL_UNMEASURED_OPEN
    assert by_id["BAD"]["pnl"] is None
    assert by_id["BAD"]["pnl_unmeasured_reason"] == PNL_UNMEASURED_UNPARSABLE
    assert by_id["OK"]["pnl"] == 4.0
    assert by_id["OK"]["campaign_completed"] is True
    assert by_id["OPEN"]["campaign_completed"] is False
    # no row substitutes a fabricated zero for an unmeasured value
    assert [row["pnl"] for row in rows] == [None, None, 4.0]


def test_pnl_beyond_the_tape_range_bound_is_unmeasured() -> None:
    """MECHANICS ONLY (#407 acceptance ii): quantity x (max high - min low).

    A value above the window's own bound cannot be this campaign's realized
    PnL; it is published as unmeasured rather than bound into the receipt.
    """
    price_ranges = {INSTRUMENT: Decimal("1000")}
    opened = [_open("R", 1_000, qty="0.01"), _open("S", 1_000, qty="0.01")]
    closed = [_close("R", 2_000, "-3256.62"), _close("S", 2_000, "8")]

    rows = attribute_campaign_pnl(opened, closed, pnl_unit="USDT", price_ranges=price_ranges)
    by_id = {row["trade_id"]: row for row in rows}

    # 0.01 x 1000 = 10 USDT is the most this position could have realized
    assert by_id["R"]["pnl_tape_range_bound_usdt"] == 10.0
    assert by_id["R"]["pnl"] is None
    assert by_id["R"]["pnl_unmeasured_reason"] == PNL_UNMEASURED_OUT_OF_RANGE
    assert by_id["S"]["pnl"] == 8.0
    assert by_id["S"]["campaign_completed"] is True


def test_price_ranges_are_measured_from_the_window() -> None:
    """MECHANICS ONLY: the bound uses real bar extremes, not a constant."""
    candles = [_candle(0, "110", "90"), _candle(3_600_000_000_000, "150", "80")]
    ranges = instrument_price_ranges(candles)
    assert ranges == {INSTRUMENT: Decimal("70")}


def test_attribution_summary_reconciles_with_the_accounting_contract() -> None:
    """MECHANICS ONLY: the row total is the aggregate the gates consume."""
    opened = [_open("A", 1_000, qty="2", px="100"), _open("B", 1_000, qty="2", px="100")]
    closed = [_close("A", 2_000, "10"), _close("B", 2_000, "4")]
    accounting = campaign_accounting(opened, closed, bars=[], cutoff_ns=10_000)

    rows = attribute_campaign_pnl(opened, closed, pnl_unit="USDT")
    summary = summarize_pnl_attribution(rows, accounting=accounting)

    assert sum(row["pnl"] for row in rows) == sum(accounting.realized_pnl_usdt) == 14.0
    assert summary["consistent_with_accounting"] is True
    assert summary["measured_rows"] == 2
    assert summary["unmeasured_reasons"] == {}
    assert summary["pnl_unit"] == "USDT"

    # a row dropped by the bound shows up as an inconsistency, not as a match
    bounded = attribute_campaign_pnl(
        opened, closed, pnl_unit="USDT", price_ranges={INSTRUMENT: Decimal("1")}
    )
    bounded_summary = summarize_pnl_attribution(
        bounded, accounting=accounting, price_ranges={INSTRUMENT: Decimal("1")}
    )
    assert bounded_summary["measured_rows"] == 0
    assert bounded_summary["unmeasured_reasons"] == {PNL_UNMEASURED_OUT_OF_RANGE: 2}
    assert bounded_summary["consistent_with_accounting"] is False


# --------------------------------------------------------------------------- #
# evaluative — real tape
# --------------------------------------------------------------------------- #
def test_real_artifact_rows_carry_their_own_pnl(tmp_path: Path) -> None:
    """Evaluative: re-read the bound artifact written on real tape."""
    if not DEFAULT_TAPE_PATH.exists():
        pytest.skip(f"real tape absent at {DEFAULT_TAPE_PATH}")
    candles = load_tape_candles(DEFAULT_TAPE_PATH, limit=REAL_BARS)
    case = BenchmarkCase(
        case_id="BC-NX407-REAL-01",
        policy_id="pol_28_expert_ensemble",
        dataset_name="BTCUSDT-1H-REAL",
        strategy_config=ExpertStrategyConfig(
            min_support_quorum=1, max_contradiction_tolerance=28
        ),
    )
    runner = BenchmarkRunner(output_dir=tmp_path / "benchmarks")
    result = runner.run(case, candles)

    artifact = Path(result.native_ledger_binding.path)
    assert artifact.is_file()
    rows = [json.loads(line) for line in artifact.read_text(encoding="utf-8").splitlines() if line]

    assert result.native_ledger_binding.verify()[0] is True
    assert rows, "a real run must publish at least one native position row"
    assert all(row["pnl_unit"] == "USDT" for row in rows)

    # (i) no two rows share a pnl value across different side/fill_time
    seen: dict[tuple[float, str, int], int] = {}
    for row in rows:
        if row["pnl"] is None:
            assert row["pnl_unmeasured_reason"] is not None
            continue
        key = (float(row["pnl"]), str(row["side"]), int(row["fill_time_ns"]))
        seen[key] = seen.get(key, 0) + 1
    assert all(count == 1 for count in seen.values())

    # (ii) every measured row respects the tape range bound it publishes
    for row in rows:
        bound = row["pnl_tape_range_bound_usdt"]
        assert bound is not None and bound > 0.0
        if row["pnl"] is not None:
            assert abs(float(row["pnl"])) <= float(bound)

    # the aggregate the gates consume equals the sum of the published rows
    attribution = (result.gate_metrics or {}).get("pnl_attribution")
    assert attribution is not None
    assert attribution["consistent_with_accounting"] is True, attribution
    assert attribution["rows"] == len(rows)
    measured = sum(Decimal(str(row["pnl"])) for row in rows if row["pnl"] is not None)
    accounting = result.accounting or {}
    assert measured == sum(
        Decimal(str(value)) for value in accounting["realized_pnl_usdt"]
    )

pytestmark = pytest.mark.slow  # #469: tape/engine file, fast loop excludes via -m "not slow"
