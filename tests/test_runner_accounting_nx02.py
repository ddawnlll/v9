"""NX02 (#423) R1–R6 — one lifecycle/accounting contract for both report paths.

Evidence classes:

* **mechanics** — chronological pairing under a reused ``position_id``,
  instrument disambiguation, absence-instead-of-zero parsing, cutoff bar-end
  marking of open campaigns, unit separation, named unsupported lifecycle cases
  and the native-balance reconciliation identity. Synthetic dicts only; no
  evaluation.
* **evaluative** — a real 500-bar BTCUSDT benchmark through ``BenchmarkRunner``:
  the runner's own accounting must reconcile against the native engine account
  on real tape. Skips when the tape is absent.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from v8_next.adapters.expert_strategy import ExpertStrategyConfig
from v8_next.evaluation.economic_benchmark import (
    ACCOUNTING_UNITS,
    BarView,
    campaign_accounting,
    parse_money,
    reconcile_native_account,
)
from v8_next.evaluation.gate_resolution import DEFAULT_TAPE_PATH, load_tape_candles
from v8_next.evaluation.runner import BenchmarkCase, BenchmarkRunner

REAL_BARS = 500


def _bars(prices: list[float], start_ns: int = 1_000, step_ns: int = 100) -> list[BarView]:
    return [
        BarView(end_ns=start_ns + i * step_ns, open=p, high=p, low=p, close=p)
        for i, p in enumerate(prices)
    ]


def _open(position_id: str, event_ns: int, *, instrument: str = "BTCUSDT-PERP.BINANCE",
          side: str = "LONG", qty: str = "1", px: str = "100") -> dict[str, object]:
    return {
        "position_id": position_id,
        "instrument_id": instrument,
        "side": side,
        "quantity": qty,
        "avg_px_open": px,
        "event_ns": event_ns,
    }


def _close(position_id: str, event_ns: int, pnl: str,
           *, instrument: str = "BTCUSDT-PERP.BINANCE") -> dict[str, object]:
    return {
        "position_id": position_id,
        "instrument_id": instrument,
        "realized_pnl": f"{pnl} USDT",
        "event_ns": event_ns,
    }


# --------------------------------------------------------------------------- #
# mechanics
# --------------------------------------------------------------------------- #
def test_pairing_is_chronological_when_the_position_id_is_reused() -> None:
    """MECHANICS ONLY: NETTING reuses the id; first-id-match would double-count."""
    opened = [_open("P", 1_000), _open("P", 3_000)]
    closed = [_close("P", 2_000, "5"), _close("P", 4_000, "-3")]
    accounting = campaign_accounting(
        opened, closed, bars=_bars([100.0]), cutoff_ns=10_000
    )
    assert accounting.realized_pnl_usdt == (5.0, -3.0)
    assert accounting.closed_campaigns == 2
    assert accounting.open_campaigns == 0
    # the historical behaviour (first matching id for both opens) would give (5.0, 5.0)
    assert accounting.realized_pnl_usdt != (5.0, 5.0)


def test_pairing_keeps_instruments_apart() -> None:
    """MECHANICS ONLY: the same id on two instruments is two campaigns."""
    opened = [_open("P", 1_000, instrument="BTCUSDT-PERP.BINANCE"),
              _open("P", 1_000, instrument="ETHUSDT-PERP.BINANCE")]
    closed = [_close("P", 2_000, "5", instrument="BTCUSDT-PERP.BINANCE"),
              _close("P", 2_000, "7", instrument="ETHUSDT-PERP.BINANCE")]
    accounting = campaign_accounting(opened, closed, bars=_bars([100.0]), cutoff_ns=10_000)
    assert sorted(accounting.realized_pnl_usdt) == [5.0, 7.0]
    assert accounting.open_campaigns == 0


def test_unparsable_pnl_is_absence_never_zero() -> None:
    """MECHANICS ONLY: a parse failure is not a zero return."""
    opened = [_open("A", 1_000), _open("B", 1_000)]
    closed = [_close("A", 2_000, "4"), {"position_id": "B", "instrument_id":
              "BTCUSDT-PERP.BINANCE", "realized_pnl": "not-a-number", "event_ns": 2_000}]
    accounting = campaign_accounting(opened, closed, bars=_bars([100.0]), cutoff_ns=10_000)
    assert accounting.realized_pnl_usdt == (4.0,)
    assert 0.0 not in accounting.realized_pnl_usdt
    assert any(u.startswith("CLOSE_PNL_UNPARSABLE:B") for u in accounting.unparsable)


def test_open_campaign_is_marked_at_the_cutoff_bar_end() -> None:
    """MECHANICS ONLY: cutoff marking uses no future price and no artificial close."""
    opened = [_open("O", 100, qty="2", px="100")]
    bars = _bars([100.0, 110.0, 130.0])
    # cutoff at the second bar's end -> mark 110, not 130 (the bar after the cutoff)
    early = campaign_accounting(opened, [], bars=bars, cutoff_ns=bars[1].end_ns)
    late = campaign_accounting(opened, [], bars=bars, cutoff_ns=bars[2].end_ns)
    assert early.open_campaigns == 1
    assert early.open_risk_usdt == pytest.approx((110.0 - 100.0) * 2.0)
    assert late.open_risk_usdt == pytest.approx((130.0 - 100.0) * 2.0)
    assert early.trade_count == 0  # an open campaign is not a completed trade


def test_units_are_declared_and_kept_apart() -> None:
    """MECHANICS ONLY: absolute USDT and dimensionless returns are never mixed."""
    opened = [_open("C", 1_000, qty="2", px="100")]
    closed = [_close("C", 2_000, "10")]
    accounting = campaign_accounting(opened, closed, bars=_bars([100.0]), cutoff_ns=10_000)
    assert accounting.units == ACCOUNTING_UNITS
    assert accounting.units["pnl"] == "USDT"
    assert accounting.units["return"] == "dimensionless"
    assert accounting.realized_pnl_usdt == (10.0,)
    # 10 USDT on 2 x 100 USDT notional
    assert accounting.campaign_returns == pytest.approx((0.05,))
    assert accounting.realized_pnl_usdt != accounting.campaign_returns


def test_scale_in_and_orphan_cases_are_named() -> None:
    """MECHANICS ONLY: unsupported lifecycle cases are named, not absorbed."""
    opened = [_open("S", 1_000), _open("S", 2_000)]
    closed = [_close("S", 3_000, "1")]
    accounting = campaign_accounting(opened, closed, bars=_bars([100.0]), cutoff_ns=10_000)
    assert any(u.startswith("SCALE_IN_OR_PARTIAL_REDUCTION") for u in accounting.unsupported)
    assert accounting.supported is False

    orphan = campaign_accounting(
        [_open("X", 5_000)], [_close("Y", 1_000, "9")], bars=_bars([100.0]), cutoff_ns=10_000
    )
    assert orphan.orphan_closes == ("Y",)
    assert orphan.realized_pnl_usdt == ()


def test_reconcile_matches_the_measured_native_identity() -> None:
    """MECHANICS ONLY: balance_total == initial + realized + funding adjustments."""
    account = {
        "balance_total": "9999.45052650 USDT",
        "positions": [
            {"realized_pnl": "-0.54947350 USDT", "is_closed": False, "adjustments": []}
        ],
    }
    accounting = campaign_accounting([], [], bars=_bars([100.0]), cutoff_ns=10_000)
    report = reconcile_native_account(account, accounting=accounting, initial_balance=10000.0)
    assert report["status"] == "MATCHED"
    assert report["reasons"] == []
    assert report["components"]["sum_realized_pnl_usdt"] == "-0.54947350"


def test_reconcile_fails_closed_on_count_and_parse_faults() -> None:
    """MECHANICS ONLY: divergence is reported with named reasons."""
    accounting = campaign_accounting([], [], bars=_bars([100.0]), cutoff_ns=10_000)
    lost_campaign = {
        "balance_total": "10005 USDT",
        "positions": [{"realized_pnl": "5 USDT", "is_closed": True, "adjustments": []}],
    }
    report = reconcile_native_account(lost_campaign, accounting=accounting)
    assert report["status"] == "DIVERGED"
    assert any(r.startswith("CLOSED_CAMPAIGN_COUNT_MISMATCH") for r in report["reasons"])

    unparsable = {
        "balance_total": "not-money",
        "positions": [{"realized_pnl": "?", "is_closed": True, "adjustments": []}],
    }
    report = reconcile_native_account(unparsable, accounting=accounting)
    assert report["status"] == "DIVERGED"
    assert "NATIVE_BALANCE_UNPARSABLE" in report["reasons"]


def test_parse_money_never_invents_a_value() -> None:
    assert parse_money("12.5 USDT") == Decimal("12.5")
    assert parse_money("") is None
    assert parse_money("abc") is None
    assert parse_money(None) is None
    assert parse_money(float("nan")) is None
    assert parse_money(float("inf")) is None
    assert parse_money(True) is None


# --------------------------------------------------------------------------- #
# evaluative — real tape
# --------------------------------------------------------------------------- #
def test_real_runner_accounting_reconciles(tmp_path: Path) -> None:
    if not DEFAULT_TAPE_PATH.exists():
        pytest.skip(f"real tape absent at {DEFAULT_TAPE_PATH}")
    candles = load_tape_candles(DEFAULT_TAPE_PATH, limit=REAL_BARS)
    case = BenchmarkCase(
        case_id="BC-NX02-REAL-01",
        policy_id="pol_28_expert_ensemble",
        dataset_name="BTCUSDT-1H-REAL",
        allowed_populations=("BurnedDiagnosticReal",),
        strategy_config=ExpertStrategyConfig(min_support_quorum=1, max_contradiction_tolerance=28),
    )
    runner = BenchmarkRunner(output_dir=tmp_path / "benchmarks")
    result = runner.run(case, candles)

    accounting = result.accounting
    assert accounting is not None
    assert accounting["units"]["pnl"] == "USDT"
    assert accounting["units"]["return"] == "dimensionless"
    assert accounting["trade_count"] == len(accounting["realized_pnl_usdt"])
    assert result.total_trades == accounting["trade_count"]
    # open campaigns are reported separately from completed trades
    assert accounting["open_campaigns"] >= 0
    assert accounting["open_risk_usdt"] is not None or accounting["open_campaigns"] == 0
    assert accounting["orphan_closes"] == []

    reconciliation = result.reconciliation
    assert reconciliation is not None
    assert reconciliation["status"] == "MATCHED", reconciliation["reasons"]
    assert reconciliation["units"]["pnl"] == "USDT"
    # the native identity holds exactly on real tape
    assert abs(Decimal(reconciliation["balance_delta_usdt"])) <= Decimal(
        reconciliation["atol_usdt"]
    )

pytestmark = pytest.mark.slow  # #469: tape/engine file, fast loop excludes via -m "not slow"
