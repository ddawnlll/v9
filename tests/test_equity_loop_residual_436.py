"""#436 — the portfolio/strategy equity level must be falsifiable.

Mechanics-only unit tests (`MECHANICS ONLY`, zero evaluative weight): every
engine result here is a synthetic fixture built to inject ONE discrepancy into
an otherwise closed ledger, so the assertions measure the guard, not the market.

Acceptance (a): a balance discrepancy injected into a synthetic engine result
must produce a fail-closed `cost_basis` (`!= VERIFIED_*`) AND publish the
measured residual. Before the fix the funded path published
`VERIFIED_ENGINE_FUNDING` for any residual whenever the funding difference
happened to reconcile, and no residual field existed at all.

Acceptance (b): `closed_loop_residual_explained_by_open_positions` was
identically equal to `open_position_realized_pnl`, one of its own inputs, so it
could never disagree with it. It is gone; the residual now has its own
measurement.

Acceptance (c): the regenerated reconciliation receipt
(`docs/evidence/v87/NX02/loop_reconciliation_436.json`) must expose the engine
closed-loop residual and the funding reconciliation side by side, with `loop_err`
as their conjunction.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from v8_next.evaluation import economic_benchmark as eb

CAPITAL = 10000.0
FEE = 0.0005
STEP_NS = 3_600_000_000_000
T0_NS = 1_753_128_000_000_000_000
INST = "MECH-PERP.BINANCE"
CLOSES = [100.0, 101.0, 102.0, 103.0, 104.0]
#: Entry commission the balance carries for the still-open position:
#: |qty| * avg_px_open * taker_fee = 2 * 100 * 0.0005.
OPEN_ENTRY_COMM = 2 * 100 * FEE
#: The open position's realized_pnl as the account reports it (commission paid).
OPEN_REALIZED = "-0.1 USDT"
#: The value the real NX02 window published for `open_position_realized_pnl`, used
#: by (b) to show the residual can no longer be that input re-published.
NX02_OPEN_REALIZED = "-0.5494735 USDT"


class _FundingRow:
    """One real-shaped funding row: the series reads three attributes off it."""

    def __init__(self, instrument: str, funding_time_ms: int, funding_rate: float) -> None:
        self.instrument = instrument
        self.funding_time_ms = funding_time_ms
        self.funding_rate = funding_rate


def _end_ns() -> list[int]:
    return [T0_NS + i * STEP_NS for i in range(len(CLOSES))]


def _funding_rows() -> list[_FundingRow]:
    """One funding boundary at bar index 2, on the open position's leg."""
    return [_FundingRow("MECH", _end_ns()[2] // 1_000_000, 0.0001)]


def _expected_funding() -> float:
    """-copysign(qty) * |qty| * close[boundary] * rate = -204 * 1e-4."""
    return -2 * CLOSES[2] * 0.0001


def _portfolio_engine(balance_total: str) -> dict:
    """One open position, no closes: the closed-loop identity is exact.

    `balance_delta == sum_realized_pnl - open_entry_commissions == -0.1`, so the
    fixture closes at `9999.9`. Any other `balance_total` is the injected
    discrepancy under test.
    """
    return {
        "opened_positions": [
            {
                "position_id": "MECH-1",
                "instrument_id": INST,
                "side": "BUY",
                "quantity": "2",
                "avg_px_open": "100",
                "event_ns": _end_ns()[1],
            }
        ],
        "closed_positions": [],
        "account": {
            "balance_total": balance_total,
            "orders": [{"status": "FILLED", "filled_qty": "2", "average_price": "100"}],
            "positions": [{"is_closed": False, "realized_pnl": OPEN_REALIZED}],
        },
    }


def _portfolio_series(balance_total: str, *, drag: float | None = None) -> dict:
    end_ns = _end_ns()
    engine_result = _portfolio_engine(balance_total)
    measured_drag = _expected_funding() * -1 if drag is None else drag
    return eb.portfolio_series_from_engine(
        engine_result,
        {INST: list(CLOSES)},
        end_ns,
        CAPITAL,
        FEE,
        None,
        _funding_rows(),
        measured_drag,
        True,
    )


def _bars() -> list[eb.BarView]:
    return [
        eb.BarView(end_ns=ns, open=c, high=c, low=c, close=c)
        for ns, c in zip(_end_ns(), CLOSES, strict=True)
    ]


def _strategy_engine(balance_total: str, open_realized: str = OPEN_REALIZED) -> dict:
    """Same open position on the strategy path; `open_realized` is reported."""
    return {
        "opened_positions": [
            {
                "position_id": "MECH-1",
                "instrument_id": INST,
                "side": "BUY",
                "quantity": "2",
                "avg_px_open": "100",
                "event_ns": _end_ns()[1],
            }
        ],
        "closed_positions": [],
        "account": {
            "balance_total": balance_total,
            "orders": [],
            "positions": [{"is_closed": False, "realized_pnl": open_realized}],
        },
    }


# --------------------------------------------------------------------------- #
# (a) injected balance discrepancy => fail-closed + residual reported
# --------------------------------------------------------------------------- #
def test_mechanics_injected_balance_discrepancy_fails_closed_and_reports_residual() -> None:
    """#436 (a): +1 USDT of engine balance the ledger cannot explain.

    Fixture ledger: `balance_delta = sum_realized - open_entry_comm = -0.1`. The
    fixture is handed `balance_total = 10000.9` instead of `9999.9`, i.e. an
    unexplained residual of +1.0. The funding side still reconciles exactly, so
    the only failing component is the engine<->series residual.
    """
    ser = _portfolio_series("10000.9 USDT")
    recon = ser["cost_reconciliation"]

    # Fail-closed: a discrepancy may never publish a VERIFIED_* basis.
    assert not ser["cost_basis"].startswith("VERIFIED_"), ser["cost_basis"]
    assert ser["cost_basis"] == "MISMATCH"
    assert ser["cost_basis"] in eb.COST_BASIS_STATES

    # The residual is reported, signed and marked as measured.
    assert recon["engine_series_residual_measured"] is True
    assert recon["engine_series_residual_usdt"] == pytest.approx(1.0, abs=1e-9)
    assert recon["engine_series_residual_abs_usdt"] == pytest.approx(1.0, abs=1e-9)
    assert recon["engine_series_ok"] is False
    assert recon["loop_ok"] is False
    assert ser["closed_loop_residual_usdt"] == pytest.approx(1.0, abs=1e-9)
    assert recon["closed_loop_error"] >= abs(recon["engine_series_residual_usdt"])

    # The funding reconciliation is NOT the failing component: that is exactly
    # why it may not stand in for the residual.
    assert ser["funding_reconciled"] is True
    assert recon["funding_ok"] is True
    assert recon["funding_residual_usdt"] == pytest.approx(0.0, abs=1e-9)

    # The published residual is the difference of published terms, not a
    # restatement of one of them.
    terms = recon["closed_loop_terms"]
    assert terms["residual_usdt"] == pytest.approx(
        terms["balance_delta"] - terms["sum_realized_pnl"] + terms["open_entry_commissions_of_open_positions"],
        abs=1e-9,
    )


def test_mechanics_closed_engine_still_verifies_funding() -> None:
    """Control for (a): the same fixture, no discrepancy, still verifies.

    Without this the fail-closed assertion above would be satisfied by a guard
    that simply never verifies anything.
    """
    ser = _portfolio_series("9999.9 USDT")
    recon = ser["cost_reconciliation"]

    assert ser["cost_basis"] == "VERIFIED_ENGINE_FUNDING"
    assert recon["engine_series_residual_usdt"] == pytest.approx(0.0, abs=1e-9)
    assert recon["engine_series_ok"] is True
    assert recon["loop_ok"] is True
    assert recon["funding_ok"] is True


def test_mechanics_unmeasured_balance_is_unknown_never_verified() -> None:
    """An absent/unparsable balance is absence, not a zero residual (#436)."""
    engine_result = _portfolio_engine("10000.9 USDT")
    engine_result["account"]["balance_total"] = "not-a-number"
    ser = eb.portfolio_series_from_engine(
        engine_result, {INST: list(CLOSES)}, _end_ns(), CAPITAL, FEE,
        None, _funding_rows(), _expected_funding() * -1, True,
    )
    assert ser["cost_basis"] == "UNKNOWN"
    assert ser["cost_reconciliation"]["engine_series_residual_measured"] is False
    assert ser["closed_loop_residual_measured"] is False
    assert ser["cost_reconciliation"]["loop_ok"] is False


# --------------------------------------------------------------------------- #
# loop_err is the conjunction of funding AND engine<->series residual
# --------------------------------------------------------------------------- #
def test_mechanics_loop_err_is_conjunction_not_the_funding_difference_alone() -> None:
    """The funding difference may not replace the residual when funding is fed.

    Pre-fix `loop_err` was the funding difference whenever funding rows were
    present (they always are on the real tape), so on the canonical portfolio
    path the engine residual was never surfaced.
    """
    # Engine clean, funding off by exactly 1 USDT.
    funding_off = _portfolio_series("9999.9 USDT", drag=_expected_funding() * -1 - 1.0)
    recon = funding_off["cost_reconciliation"]
    assert recon["engine_series_residual_abs_usdt"] == pytest.approx(0.0, abs=1e-9)
    assert recon["funding_residual_usdt"] == pytest.approx(-1.0, abs=1e-9)
    assert recon["funding_ok"] is False
    assert funding_off["cost_basis"] == "FUNDING_UNVERIFIED"
    assert recon["loop_err_is_conjunction"] is True
    assert recon["loop_err_components_usdt"] == {
        "engine_series_residual_abs": recon["engine_series_residual_abs_usdt"],
        "funding_residual_abs": pytest.approx(1.0, abs=1e-9),
    }
    assert recon["closed_loop_error"] == pytest.approx(1.0, abs=1e-9)

    # Both off: the published error is the LARGER of the two components.
    both = _portfolio_series("10002.9 USDT", drag=_expected_funding() * -1 - 1.0)
    brecon = both["cost_reconciliation"]
    assert brecon["engine_series_residual_abs_usdt"] == pytest.approx(3.0, abs=1e-9)
    assert brecon["loop_err_components_usdt"]["funding_residual_abs"] == pytest.approx(1.0, abs=1e-9)
    assert brecon["closed_loop_error"] == pytest.approx(3.0, abs=1e-9)
    assert both["cost_basis"] == "MISMATCH"


# --------------------------------------------------------------------------- #
# (b) the residual is not one of its own inputs
# --------------------------------------------------------------------------- #
def test_mechanics_unexplained_residual_is_not_the_open_position_term() -> None:
    """#436 (b): the tautology field is gone and the residual is its own number.

    Fixture: `sum_realized_pnl = 0`, `open_position_realized_pnl = -0.5494735`
    (the value the real NX02 window published), `balance_delta = 0`. The
    unexplained residual is therefore `+0.5494735`, while the field that used to
    claim to "explain the residual by open positions" was identically
    `-0.5494735` -- one of the inputs already subtracted, unable to disagree.
    """
    ser = eb.strategy_series_from_engine(
        _strategy_engine("10000 USDT", NX02_OPEN_REALIZED), _bars(), CAPITAL, FEE
    )
    recon = ser["cost_reconciliation"]

    assert "closed_loop_residual_explained_by_open_positions" not in recon
    assert recon["open_position_realized_pnl"] == pytest.approx(-0.5494735, abs=1e-9)
    assert recon["sum_realized_pnl"] == pytest.approx(0.0, abs=1e-9)
    assert recon["engine_series_residual_measured"] is True
    assert recon["engine_series_residual_usdt"] == pytest.approx(0.5494735, abs=1e-9)
    assert recon["engine_series_residual_usdt"] != recon["open_position_realized_pnl"]
    assert recon["closed_loop_terms"]["residual_usdt"] == recon["engine_series_residual_usdt"]
    assert ser["cost_basis"] == "MISMATCH"
    assert ser["closed_loop_residual_usdt"] == pytest.approx(0.5494735, abs=1e-9)


# --------------------------------------------------------------------------- #
# the graft is declared, never silent
# --------------------------------------------------------------------------- #
def test_mechanics_graft_is_declared_with_equity_mtm_and_per_bar_adjustment() -> None:
    """`equity[-1] == balance_total` by construction, so it is named."""
    ser = _portfolio_series("10000.9 USDT")
    adj = ser["reconciliation_adjustment_per_bar"]
    mtm = ser["equity_mtm"]

    assert ser["equity_construction"] == eb.EQUITY_CONSTRUCTION
    assert "equity_mtm" in ser["equity_construction"]
    assert ser["equity"][0] == CAPITAL
    # Reconstruct the published curve from the declared parts.
    rebuilt = [ser["equity"][0]]
    for i in range(1, len(ser["equity"])):
        rebuilt.append(rebuilt[-1] + (mtm[i] - mtm[i - 1]) + adj)
    assert rebuilt[-1] == pytest.approx(ser["equity"][-1], abs=1e-9)
    assert mtm[-1] != ser["equity"][-1]
    recon = ser["cost_reconciliation"]
    assert recon["equity_construction"] == eb.EQUITY_CONSTRUCTION
    assert recon["reconciliation_adjustment_per_bar_usdt"] == adj
    assert recon["equity_mtm_terminal"] == mtm[-1]
    assert recon["equity_mtm_gap_usdt"] == pytest.approx(mtm[-1] - 10000.9, abs=1e-6)
    # The legacy alias still reads the un-grafted path.
    assert ser["raw_equity"] == mtm


def test_mechanics_unverifiable_cost_basis_fails_closed_the_published_verdict() -> None:
    """A MISMATCH basis feeds `cost_basis_ok=False`, which fails closed (#436).

    This is the propagation the bound receipt depends on
    (`app/portfolio.py`: `cost_basis_ok = p_ser["cost_basis"] == "VERIFIED_ENGINE_FUNDING"`).
    """
    kwargs = {
        "chrono_ok": True,
        "chrono_note": "OK",
        "leak_probe": (True, "OK"),
        "excess": 0.02,
        "excess_ci": (0.0, 0.04),
        "stats": {
            "dsr": {"verdict": "COMPUTED", "dsr_confidence": 0.5,
                    "selected_sharpe_nonannualized": -0.1, "selected_variant": "x"},
            "pbo": {"verdict": "COMPUTED"},
            "spa": {"verdict": "COMPUTED", "pvalues": {"consistent": 0.4}},
            "variant_excess_vs_baseline": -0.01,
        },
        "mix": {"incremental_net": 0.0},
        "funding_missing": False,
        "live_fills_present": False,
        "parity_ok": True,
    }
    verified = eb.build_verdicts(**kwargs, cost_basis_ok=True)
    unverified = eb.build_verdicts(**kwargs, cost_basis_ok=False)
    assert verified.execution == "SIM_ONLY"
    assert unverified.execution == "EXECUTION_UNPROVEN"
    assert "cost basis unverified" in unverified.execution_note


# --------------------------------------------------------------------------- #
# (c) the regenerated receipt shows the residual beside the funding reconciliation
# --------------------------------------------------------------------------- #
_RECEIPT_REL = "docs/evidence/v87/NX02/loop_reconciliation_436.json"


def test_evidence_receipt_shows_residual_beside_funding_reconciliation() -> None:
    """#436 (c): the regenerated receipt carries both reconciliations.

    Read-only assertion on the committed artifact; the receipt itself is
    regenerated over the real tape by
    `docs/evidence/v87/NX02/regenerate_loop_reconciliation_receipt.py`, once per
    code phase, and composed from the two measurements.
    """
    root = Path(eb.project_root()).parent
    path = root / _RECEIPT_REL
    if not path.exists():
        pytest.skip(f"#436 reconciliation receipt absent at {path}")
    receipt = json.loads(path.read_text(encoding="utf-8"))

    loop = receipt["loop"]
    assert loop["loop_err_is_conjunction"] is True
    # Side by side, both measured, both published.
    engine_res = abs(receipt["engine_closed_loop"]["residual_usdt"])
    funding_res = abs(receipt["funding_reconciliation"]["residual_usdt"])
    assert engine_res > 0.0 and funding_res > 0.0
    assert loop["closed_loop_error"] == pytest.approx(max(engine_res, funding_res), abs=1e-12)
    assert loop["engine_series_residual_abs_usdt"] == pytest.approx(engine_res, abs=1e-15)
    assert loop["funding_residual_abs_usdt"] == pytest.approx(funding_res, abs=1e-15)
    assert receipt["engine_closed_loop"]["residual_measured"] is True
    assert receipt["engine_closed_loop"]["ok"] is True
    assert receipt["engine_closed_loop"]["tolerance_usdt"] == eb.PORTFOLIO_LOOP_ATOL
    assert receipt["funding_reconciliation"]["ok"] is True
    assert receipt["funding_reconciliation"]["tolerance_usdt"] is not None
    assert receipt["window"]["trade_signature_identical"] is True

    # Before / after, measured on the same real window by the same generator.
    before, after = receipt["before"], receipt["after"]
    before_fields = before["published"]["cost_reconciliation_fields"]
    after_fields = after["published"]["cost_reconciliation_fields"]
    assert "closed_loop_error" in before_fields
    assert "engine_series_residual_usdt" not in before_fields
    assert "engine_series_residual_usdt" in after_fields
    assert "closed_loop_residual_explained_by_open_positions" not in after_fields
    # The number existed before the fix; the artifact just never published it.
    assert before["generator_recomputed_engine_closed_loop"]["residual_usdt"] == pytest.approx(
        after["engine_closed_loop"]["residual_usdt"], abs=1e-12
    )
    assert before["published"]["fail_closed"] is False
    assert after["published"]["fail_closed"] is False
    assert before["source_sha256"] != after["source_sha256"]

    # The graft is named in the receipt the same way the series names it.
    graft = receipt["graft"]
    assert graft["equity_construction"] == eb.EQUITY_CONSTRUCTION
    assert "equity_mtm" in graft["equity_construction"]
    bars = receipt["window"]["bars"]
    assert graft["declared_adjustment_total_usdt"] == pytest.approx(
        -graft["equity_mtm_gap_usdt"], abs=1e-9
    )
    assert graft["declared_adjustment_per_bar_usdt"] == pytest.approx(
        graft["declared_adjustment_total_usdt"] / (bars - 1), rel=1e-9
    )
    assert graft["measured_graft_total_usdt"] == pytest.approx(
        graft["equity_terminal"] - graft["raw_equity_terminal"], abs=1e-6
    )
    assert abs(graft["measured_graft_total_usdt"]) > 1.0  # the smearing is material

    # The canonical bound receipts this reconciliation explains are stored with
    # it, and their recorded digests match the files on disk.
    for phase, block in (("before", before), ("after", after)):
        bound = block["bound_receipt"]
        assert bound is not None, phase
        bound_path = root / bound["path"]
        assert bound_path.exists(), bound_path
        from hashlib import sha256

        assert sha256(bound_path.read_bytes()).hexdigest() == bound["sha256"]
        assert bound["claim_status"] == "NO_ECONOMIC_CLAIM"
    assert before["bound_receipt"]["sha256"] != after["bound_receipt"]["sha256"]
    assert math.isfinite(after["bound_receipt"]["net_return"])
    assert receipt["claim_status"] == "NO_ECONOMIC_CLAIM"
