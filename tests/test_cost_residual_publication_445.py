"""#445 — the fail-closed cost residual is published INSIDE the receipt.

The defect: a `MISMATCH`/`UNKNOWN` cost state was published (`metrics.portfolio_P
.cost_basis`, verdict `EXECUTION_UNPROVEN`) while the measurement it rests on --
`|engine_series_residual_usdt| > PORTFOLIO_LOOP_ATOL` -- existed only as a
returned-dict field with no writer. The bound receipt/report carried the decision
and not one `residual` token (grep = 0).

Acceptance (measurable):

1. A curve whose `cost_basis` is `MISMATCH` publishes, INSIDE the receipt,
   `metrics.<curve>.cost_reconciliation.engine_series_residual_usdt` finite and
   not null, with the terms it came from (`closed_loop_terms`), the state
   (`engine_series_ok`) and the bound it was compared against
   (`engine_series_tolerance_usdt`).
2. The published residual is a function of the magnitudes the receipt itself
   carries: recomputed from `closed_loop_terms` it equals the stored value.
3. The `UNKNOWN` (unmeasurable) path stays distinguishable and publishes NO
   residual: `state = NOT_MEASURED`, a null residual and a named reason.
4. The two closed-loop identities in this module -- strategy
   (`balance_delta - sum_realized - open_realized`) and portfolio
   (`balance_delta - sum_realized + open_entry_comm`) -- are measured on ONE
   engine balance: they either agree, or diverge with a named reason. A
   divergence is never reported as agreement.

Mechanics-only (`MECHANICS ONLY`, zero evaluative weight): every engine result
here is a synthetic fixture built to inject ONE discrepancy into an otherwise
closed ledger, so the assertions measure the publication, not the market. The
read-only assertions at the end run against the frozen canonical artifact and
the canonical ledger, and skip when they are absent.
"""

from __future__ import annotations

import hashlib
import json
import math
import subprocess
import sys
from pathlib import Path

import pytest

from v8_next.evaluation import economic_benchmark as eb

CAPITAL = 10000.0
FEE = 0.0005
STEP_NS = 3_600_000_000_000
T0_NS = 1_753_128_000_000_000_000
INST = "MECH-PERP.BINANCE"
CLOSES = [100.0, 101.0, 102.0, 103.0, 104.0]
OPEN_QTY = 2.0
OPEN_PX = 100.0
#: The entry commission the closed ledger carries for the still-open position:
#: |qty| * avg_px_open * taker_fee = 2 * 100 * 0.0005.
OPEN_ENTRY_COMM = OPEN_QTY * OPEN_PX * FEE
#: The charge the account reports for that open position at the same convention.
CLEAN_OPEN_REALIZED = f"-{OPEN_ENTRY_COMM}"
#: An engine whose open-position charge does not match the fee convention the
#: portfolio identity estimates: the two identities cannot agree on this balance.
DISPLACED_OPEN_REALIZED = "-0.3 USDT"
#: `balance_delta = +0.9` on a ledger whose only cost term is 0.1 USDT of entry
#: commission: the closed loop is 1.0 USDT short, far outside every declared
#: loop tolerance (strategy 0.01, portfolio 0.02).
MISMATCH_BALANCE = "10000.9 USDT"
#: The same fixture with the loop closed.
CLEAN_BALANCE = "9999.9 USDT"

#: Canonical evidence this card publishes against, never rewrites.
CANONICAL_RECEIPT = "artifacts/benchmarks/economic_receipt_e45add52.json"
CANONICAL_LEDGER = "artifacts/benchmarks/benchmark_ledger.jsonl"


# --------------------------------------------------------------------------- #
# MECHANICS ONLY fixtures
# --------------------------------------------------------------------------- #
def _end_ns() -> list[int]:
    return [T0_NS + i * STEP_NS for i in range(len(CLOSES))]


def _engine(
    balance_total: str,
    *,
    open_realized: str = CLEAN_OPEN_REALIZED,
    expose_positions: bool = True,
) -> dict:
    """One open position, no closes: `sum_realized_pnl = 0`.

    The account exposes the position (real engines do), so BOTH closed-loop
    identities can be evaluated on this one engine balance.
    """
    account: dict = {
        "balance_total": balance_total,
        "orders": [{"status": "FILLED", "filled_qty": str(OPEN_QTY), "average_price": str(OPEN_PX)}],
    }
    if expose_positions:
        account["positions"] = [
            {
                "instrument_id": INST,
                "is_closed": False,
                "quantity": str(OPEN_QTY),
                "average_open_price": str(OPEN_PX),
                "realized_pnl": open_realized,
            }
        ]
    return {
        "opened_positions": [
            {
                "position_id": "MECH-1",
                "instrument_id": INST,
                "side": "BUY",
                "quantity": str(OPEN_QTY),
                "avg_px_open": str(OPEN_PX),
                "event_ns": _end_ns()[1],
            }
        ],
        "closed_positions": [],
        "account": account,
    }


def _portfolio_series(balance_total: str = MISMATCH_BALANCE, **kwargs) -> dict:
    """The portfolio closed-loop builder on the mechanics fixture."""
    return eb.portfolio_series_from_engine(
        _engine(balance_total, **kwargs),
        {INST: list(CLOSES)},
        _end_ns(),
        CAPITAL,
        FEE,
        None,
        (),
        None,
        False,
    )


def _strategy_series(balance_total: str = MISMATCH_BALANCE, **kwargs) -> dict:
    """The strategy closed-loop builder on the same mechanics fixture."""
    bars = [
        eb.BarView(end_ns=ns, open=c, high=c, low=c, close=c)
        for ns, c in zip(_end_ns(), CLOSES, strict=True)
    ]
    return eb.strategy_series_from_engine(
        _engine(balance_total, **kwargs), bars, CAPITAL, FEE
    )


def _published(series: dict) -> dict:
    """What the writer puts in the receipt for this curve."""
    published = eb.published_cost_reconciliation(
        series.get("cost_reconciliation"), cost_basis=str(series["cost_basis"])
    )
    assert published is not None, series["cost_basis"]
    return published


def _receipt(metrics: dict[str, eb.MetricSet]) -> eb.EconomicReceipt:
    """A receipt container over REAL MetricSets: the writer's own data path.

    MECHANICS ONLY: the container's identity/verdict fields are fixture values,
    the numbers under test are the published ones.
    """
    verdicts = eb.build_verdicts(
        chrono_ok=True,
        chrono_note="mechanics",
        leak_probe=(True, "mechanics"),
        excess=0.0,
        excess_ci=(0.0, 0.0),
        stats={
            "dsr": {"verdict": "COMPUTED", "dsr_confidence": 0.5,
                    "selected_sharpe_nonannualized": -0.1, "selected_variant": "x"},
            "pbo": {"verdict": "COMPUTED"},
            "spa": {"verdict": "COMPUTED", "pvalues": {"consistent": 0.4}},
            "variant_excess_vs_baseline": -0.01,
        },
        mix={"incremental_net": 0.0},
        cost_basis_ok=metrics["portfolio_P"].cost_basis == "VERIFIED_ENGINE_FUNDING",
        funding_missing=False,
        live_fills_present=False,
        parity_ok=True,
    )
    return eb.EconomicReceipt(
        receipt_id="mechanics-445",
        run=eb.RunIdentity(
            dataset=eb.DatasetIdentity(
                tape_path="MECHANICS_ONLY",
                tape_sha256="0" * 64,
                universe=(INST,),
                period_start_ns=_end_ns()[0],
                period_end_ns=_end_ns()[-1],
                n_bars=len(CLOSES),
                source_hashes=(),
            ),
            code=eb.CodeIdentity(
                git_rev="mechanics",
                git_dirty="False",
                config_sha256="0" * 64,
                estimator_versions={},
                source_sha256="0" * 64,
            ),
            seed=1,
            primary_benchmark="equal_weight",
            diagnostic_benchmarks=(),
            strategy_family=("portfolio_P",),
            capital=CAPITAL,
            taker_fee=FEE,
            opex_monthly_usd=0.0,
        ),
        metrics=metrics,
        oos_metrics={},
        verdicts=verdicts,
        statistics={},
        controls={},
        portfolio_mix={},
        capacity_scenarios=[],
        parity={},
        shadow_live={},
        limitations=[],
    )


# --------------------------------------------------------------------------- #
# (1) + (2) MISMATCH publishes the measured residual, its terms and its bound
# --------------------------------------------------------------------------- #
def test_mechanics_mismatch_receipt_publishes_the_measured_residual() -> None:
    """The `MISMATCH` receipt carries the number, the terms and the bound.

    HEAD was RED here: the residual was computed (fixture residual = +1.0 USDT,
    exactly the #436 measurement) and dropped before the receipt was written.
    """
    series = _portfolio_series()
    published = _published(series)

    assert series["cost_basis"] == "MISMATCH"
    assert published["cost_basis"] == "MISMATCH"
    assert published["state"] in eb.RECONCILIATION_STATES
    assert published["state"] == "MEASURED"
    assert published["named_reason"] in eb.RECONCILIATION_REASONS

    # (1) the residual is a real, finite measurement -- not null, not a constant.
    residual = published["engine_series_residual_usdt"]
    assert residual is not None
    assert math.isfinite(residual)
    assert residual == pytest.approx(1.0, abs=1e-12)
    assert published["engine_series_residual_measured"] is True
    assert published["engine_series_ok"] is False
    assert published["named_reason"] == eb.REASON_RESIDUAL_OUTSIDE_TOLERANCE
    assert published["engine_series_tolerance_usdt"] == eb.PORTFOLIO_LOOP_ATOL
    assert published["identity"] == eb.IDENTITY_PORTFOLIO_OPEN_ENTRY_COMMISSIONS

    # The terms travel too, and the bound is the one the decision used.
    terms = published["closed_loop_terms"]
    assert terms is not None
    assert set(terms) >= {"balance_delta", "sum_realized_pnl", "residual_usdt"}
    assert eb.IDENTITY_OPEN_TERM_KEYS[eb.IDENTITY_PORTFOLIO_OPEN_ENTRY_COMMISSIONS] in terms

    # (2) recomputable from what the receipt itself binds.
    recomputed = eb.recompute_closed_loop_residual(terms)
    assert recomputed == pytest.approx(residual, abs=1e-12)
    assert published["recomputed_residual_usdt"] == pytest.approx(residual, abs=1e-12)
    assert published["recompute_matches_stored"] is True


def test_mechanics_receipt_json_carries_the_residual_at_the_published_path() -> None:
    """The serialized receipt carries `...cost_reconciliation.engine_series_residual_usdt`."""
    series = _portfolio_series()
    metrics = {
        "portfolio_P": eb.metrics_for_curve(
            list(series["equity"]), list(series["exposure"]), float(series["turnover"]),
            float(series["commission"]), None, str(series["cost_basis"]),
            [float(v) for v in series["equity"]],
            int(series["n_trades"]),
            cost_reconciliation=_published(series),
        )
    }
    receipt = _receipt(metrics)
    payload = json.loads(receipt.model_dump_json())

    recon = payload["metrics"]["portfolio_P"]["cost_reconciliation"]
    assert recon["cost_basis"] == "MISMATCH"
    assert recon["engine_series_residual_usdt"] == pytest.approx(1.0, abs=1e-12)
    assert math.isfinite(recon["engine_series_residual_usdt"])
    assert recon["closed_loop_terms"]["residual_usdt"] == pytest.approx(1.0, abs=1e-12)
    assert recon["engine_series_ok"] is False
    assert recon["engine_series_tolerance_usdt"] == eb.PORTFOLIO_LOOP_ATOL
    assert recon["named_reason"] == eb.REASON_RESIDUAL_OUTSIDE_TOLERANCE
    assert payload["claim_status"] == "NO_ECONOMIC_CLAIM"

    # The decision the receipt already published is unchanged by the fix.
    assert payload["verdicts"]["execution"] == "EXECUTION_UNPROVEN"

    # Recomputing off the serialized artifact (not the in-memory object) agrees.
    assert eb.recompute_closed_loop_residual(recon["closed_loop_terms"]) == pytest.approx(
        recon["engine_series_residual_usdt"], abs=1e-12
    )

    # And the report the writer renders carries the number and its terms.
    report = eb.render_report(receipt)
    assert "Cost reconciliation" in report
    assert "engine_series_residual_usdt" not in report  # terms are not dumped raw
    published_residual = format(recon["engine_series_residual_usdt"], ".10g")
    assert f"| {published_residual} " in report
    assert eb.IDENTITY_OPEN_TERM_KEYS[eb.IDENTITY_PORTFOLIO_OPEN_ENTRY_COMMISSIONS] in report
    assert eb.REASON_RESIDUAL_OUTSIDE_TOLERANCE in report


def test_mechanics_strategy_path_publishes_its_own_measured_residual() -> None:
    """The same body of work on the strategy-identity path (#1768 terms)."""
    series = _strategy_series()
    published = _published(series)

    assert series["cost_basis"] == "MISMATCH"
    assert published["identity"] == eb.IDENTITY_STRATEGY_OPEN_REALIZED
    assert published["engine_series_tolerance_usdt"] == eb.STRATEGY_LOOP_ATOL
    assert published["engine_series_residual_usdt"] == pytest.approx(1.0, abs=1e-12)
    assert published["engine_series_ok"] is False
    assert published["named_reason"] == eb.REASON_RESIDUAL_OUTSIDE_TOLERANCE
    terms = published["closed_loop_terms"]
    assert "open_position_realized_pnl" in terms
    recomputed = eb.recompute_closed_loop_residual(terms)
    assert recomputed == pytest.approx(terms["residual_usdt"], abs=1e-12)
    assert published["recompute_matches_stored"] is True


def test_mechanics_closed_loop_still_publishes_a_measured_zero() -> None:
    """Control: a closed ledger publishes its measurement, not an absence.

    Without this, (1) would also be satisfied by a publisher that invents a
    number whenever the state is fail-closed.
    """
    series = _portfolio_series(CLEAN_BALANCE)
    published = _published(series)
    assert series["cost_basis"].startswith("VERIFIED_")
    assert published["named_reason"] == eb.REASON_RESIDUAL_WITHIN_TOLERANCE
    assert published["engine_series_ok"] is True
    assert published["engine_series_residual_usdt"] == pytest.approx(0.0, abs=1e-12)
    assert published["recompute_matches_stored"] is True


def test_mechanics_curve_without_a_reconciliation_publishes_none() -> None:
    """An analytic leg reconciled nothing of its own: no block, not an empty one."""
    assert eb.published_cost_reconciliation(None, cost_basis="ANALYTIC_MODEL") is None
    metric = eb.metrics_for_curve(
        [CAPITAL, CAPITAL * 1.01], [0.0, 0.0], 0.0, 0.0, None, "ANALYTIC_MODEL", None, 0
    )
    assert metric.cost_reconciliation is None


# --------------------------------------------------------------------------- #
# (3) UNKNOWN publishes no residual at all, and names why
# --------------------------------------------------------------------------- #
def test_mechanics_unmeasurable_balance_publishes_no_residual_and_names_it() -> None:
    """An unreadable engine balance is absence, not a zero residual (#436/#445).

    The pre-fix series computed the residual off the substituted `capital`, so
    the number was arithmetically present and meaningless; the receipt must not
    carry it.
    """
    series = eb.portfolio_series_from_engine(
        _engine("not-a-number"),
        {INST: list(CLOSES)},
        _end_ns(),
        CAPITAL,
        FEE,
        None,
        (),
        None,
        False,
    )
    assert series["cost_basis"] == "UNKNOWN"
    published = _published(series)

    assert published["cost_basis"] == "UNKNOWN"
    assert published["state"] == "NOT_MEASURED"
    assert published["engine_series_residual_measured"] is False
    # No residual, no terms, no derived state: nothing invented.
    assert published["engine_series_residual_usdt"] is None
    assert published["engine_series_residual_abs_usdt"] is None
    assert published["closed_loop_terms"] is None
    assert published["engine_series_ok"] is None
    assert published["recomputed_residual_usdt"] is None
    assert published["recompute_matches_stored"] is None
    assert published["named_reason"] == eb.REASON_BALANCE_UNMEASURABLE
    # The second identity is not measured either, and says so by name.
    assert published["cross_identity"]["residual_usdt"] is None
    assert published["cross_identity"]["named_reason"] == eb.REASON_BALANCE_UNMEASURABLE
    # `UNKNOWN` remains distinguishable from `MISMATCH`: not a hidden fail-closed.
    assert published["state"] != "MEASURED"

    # An account that does not expose its positions leaves the terms absent too.
    hidden = _published(_portfolio_series(expose_positions=False))
    assert hidden["state"] == "MEASURED"
    assert hidden["closed_loop_terms"] is not None  # the governing identity's own
    assert hidden["cross_identity"]["residual_usdt"] is None
    assert hidden["cross_identity"]["named_reason"] == eb.REASON_OPEN_TERMS_UNMEASURABLE


# --------------------------------------------------------------------------- #
# (4) the two identities on ONE engine balance: agree, or diverge by name
# --------------------------------------------------------------------------- #
def test_mechanics_identities_agree_when_the_open_position_is_charged_as_estimated() -> None:
    """The engine's own open-position charge matches the fee estimate: they agree.

    Portfolio residual `0.9 + 0.1 = 1.0`; strategy residual `0.9 - (-0.1) = 1.0`.
    """
    published = _published(_portfolio_series())
    check = published["identities_check"]

    assert check["status"] == "PASS"
    assert check["reason"] is None
    assert check["comparison_tolerance_usdt"] == eb.PORTFOLIO_LOOP_ATOL
    assert check["difference_usdt"] == pytest.approx(0.0, abs=1e-12)
    assert check["identities"][eb.IDENTITY_PORTFOLIO_OPEN_ENTRY_COMMISSIONS][
        "residual_usdt"
    ] == pytest.approx(1.0, abs=1e-12)
    assert check["identities"][eb.IDENTITY_STRATEGY_OPEN_REALIZED][
        "residual_usdt"
    ] == pytest.approx(1.0, abs=1e-12)


def test_mechanics_identities_divergence_is_named_never_reported_as_agreement() -> None:
    """A displaced open-position charge diverges: named reason + the difference.

    Portfolio residual `0.9 + 0.1 = 1.0`; strategy residual `0.9 - (-0.3) = 1.2`.
    The check must NOT pass: it reports the measured difference under a name.
    """
    series = _portfolio_series(open_realized=DISPLACED_OPEN_REALIZED)
    published = _published(series)
    check = published["identities_check"]

    assert check["status"] == "FAIL"
    assert check["reason"] == eb.REASON_IDENTITIES_DIVERGED
    assert check["reason"] in eb.RECONCILIATION_REASONS
    assert check["difference_usdt"] == pytest.approx(-0.2, abs=1e-12)
    assert abs(check["difference_usdt"]) > check["comparison_tolerance_usdt"]
    assert check["identities"][eb.IDENTITY_STRATEGY_OPEN_REALIZED][
        "residual_usdt"
    ] == pytest.approx(1.2, abs=1e-12)
    # The published decision is unchanged: the cross identity is a measurement,
    # not an input to `cost_basis` or to the loop error.
    assert published["cost_basis"] == "MISMATCH"
    assert published["engine_series_residual_usdt"] == pytest.approx(1.0, abs=1e-12)
    assert published["cross_identity"]["governing"] is False
    assert series["closed_loop_residual_usdt"] == pytest.approx(1.0, abs=1e-12)


def test_mechanics_missing_terms_report_a_named_failure_not_a_silent_pass() -> None:
    """A receipt that publishes no terms cannot report agreement."""
    empty = eb.reconcile_closed_loop_identities({})
    assert empty["status"] == "FAIL"
    assert empty["reason"] == eb.REASON_TERMS_NOT_PUBLISHED
    assert empty["difference_usdt"] is None
    assert eb.reconcile_closed_loop_identities(None) == empty


# --------------------------------------------------------------------------- #
# The frozen canonical evidence: read-only, at the published path
# --------------------------------------------------------------------------- #
def _canonical_root() -> Path | None:
    """The checkout that holds the canonical `artifacts/` (shared by worktrees).

    `artifacts/` is gitignored, so a worktree does not have its own copy: the git
    common dir names the checkout that does.
    """
    here = Path(eb.project_root()).parent
    if (here / CANONICAL_RECEIPT).is_file():
        return here
    try:
        common = subprocess.run(
            ["git", "rev-parse", "--git-common-dir"],
            cwd=here, capture_output=True, text=True, timeout=30, check=True,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    root = (here / common).resolve().parent if not Path(common).is_absolute() else Path(common).parent
    return root if (root / CANONICAL_RECEIPT).is_file() else None


def test_bound_artifact_still_has_no_residual_and_is_not_rewritten() -> None:
    """The canonical `MISMATCH` receipt: the defect, frozen, and un-rewritten.

    `e45add52` publishes the fail-closed decision and not one `residual` token.
    It is evidence for this card, so it is asserted read-only -- and its sha256
    is re-checked against the digest the canonical ledger binds to it.
    """
    root = _canonical_root()
    if root is None:
        pytest.skip("canonical artifacts absent")
    assert root is not None
    receipt_path = root / CANONICAL_RECEIPT

    ledger_path = root / CANONICAL_LEDGER
    if not ledger_path.is_file():
        pytest.skip("canonical ledger absent")

    raw = receipt_path.read_text(encoding="utf-8")
    payload = json.loads(raw)
    portfolio_p = payload["metrics"]["portfolio_P"]
    assert portfolio_p["cost_basis"] == "MISMATCH"
    assert payload["verdicts"]["execution"] == "EXECUTION_UNPROVEN"
    # The defect: the decision is published, the measurement is not there.
    assert "residual" not in raw
    assert "cost_reconciliation" not in portfolio_p

    # (4) on this artifact the two identities are not evaluable at all: the
    # check reports a NAMED failure, never an agreement it did not measure.
    check = eb.reconcile_closed_loop_identities(portfolio_p.get("cost_reconciliation"))
    assert check["status"] == "FAIL"
    assert check["reason"] == eb.REASON_TERMS_NOT_PUBLISHED
    assert check["difference_usdt"] is None

    # Bound by the canonical ledger, and unchanged on disk.
    digest = hashlib.sha256(receipt_path.read_bytes()).hexdigest()
    bound = [
        binding
        for line in ledger_path.read_text(encoding="utf-8").splitlines()
        for binding in json.loads(line)["receipt"].get("artifact_bindings", [])
        if binding.get("role") == "economic_receipt"
        and Path(str(binding.get("path"))) == receipt_path
    ]
    assert bound, "canonical ledger binds no economic_receipt at this path"
    assert bound[-1]["sha256_hex"] == digest


def test_run_scoped_portfolio_receipt_publishes_the_measured_residual(tmp_path: Path) -> None:
    """The real quad tape, run-scoped: the receipt carries the measurement.

    `--output-dir` is a scratch directory: the canonical publish path is never
    written by a test. Skips when the tape is absent.
    """
    from v8_next.app import portfolio as port_mod

    root = _canonical_root() or Path(eb.project_root()).parent
    tape = root / "research" / "tape" / "quad-1h-12m"
    if not (tape / "tape.jsonl").is_file():
        pytest.skip("quad tape absent")

    rc = port_mod.main([
        "--tape-path", str(tape), "--bars", "500",
        "--output-dir", str(tmp_path / "port"),
        "--primary", "equal_weight",
    ])
    assert rc == 0
    receipts = sorted((tmp_path / "port").glob("economic_receipt_*.json"))
    assert receipts, "run-scoped economic receipt missing"
    payload = json.loads(receipts[0].read_text(encoding="utf-8"))
    assert payload["claim_status"] == "NO_ECONOMIC_CLAIM"

    measured = 0
    for name, metric in payload["metrics"].items():
        recon = metric["cost_reconciliation"]
        if recon is None:
            # A curve that reconciled nothing of its own publishes no block.
            assert metric["cost_basis"] == "ANALYTIC_MODEL", name
            continue
        measured += 1
        assert recon["state"] in eb.RECONCILIATION_STATES
        assert recon["named_reason"] in eb.RECONCILIATION_REASONS
        assert recon["identity"] in eb.IDENTITY_OPEN_TERM_KEYS, name
        # (2) recomputable from the magnitudes the receipt itself binds.
        assert recon["recompute_matches_stored"] is True, name
        assert eb.recompute_closed_loop_residual(recon["closed_loop_terms"]) == pytest.approx(
            recon["engine_series_residual_usdt"], abs=1e-9
        ), name
        residual = recon["engine_series_residual_usdt"]
        assert residual is not None and math.isfinite(residual), name
        if recon["cost_basis"] == "MISMATCH":
            # (1) fail-closed is published WITH the measurement that caused it,
            # and the measurement is outside the bound it declares.
            assert recon["engine_series_ok"] is False, name
            assert recon["named_reason"] == eb.REASON_RESIDUAL_OUTSIDE_TOLERANCE, name
            assert abs(residual) > recon["engine_series_tolerance_usdt"], name
        else:
            assert recon["cost_basis"].startswith(("VERIFIED_", "FUNDING_", "ESTIMATED")), name
            assert abs(residual) <= recon["engine_series_tolerance_usdt"], name
        # (4) both identities on one balance: agreement, or a named divergence.
        check = recon["identities_check"]
        assert check["status"] in {"PASS", "FAIL"}, name
        if check["status"] == "PASS":
            assert check["reason"] is None
            assert abs(check["difference_usdt"]) <= check["comparison_tolerance_usdt"]
        else:
            assert check["reason"] in {
                eb.REASON_IDENTITIES_DIVERGED,
                eb.REASON_TERMS_NOT_PUBLISHED,
            }, name
            assert check["difference_usdt"] is None or math.isfinite(check["difference_usdt"])
    assert measured >= 2, "no engine curve published a reconciliation"

    reports = sorted((tmp_path / "port").glob("economic_report_*.md"))
    assert reports, "run-scoped economic report missing"
    report = reports[0].read_text(encoding="utf-8")
    assert "Cost reconciliation" in report
    assert eb.REASON_RESIDUAL_OUTSIDE_TOLERANCE in report or eb.REASON_RESIDUAL_WITHIN_TOLERANCE in report


def test_canonical_ledger_chain_stays_verified_and_entries_are_not_rewritten() -> None:
    """`cli status` over the canonical ledger: chain VERIFIED, entries untouched."""
    root = _canonical_root()
    if root is None:
        pytest.skip("canonical artifacts absent")
    assert root is not None
    out_dir = root / "artifacts" / "benchmarks"

    ledger_path = out_dir / "benchmark_ledger.jsonl"
    if not ledger_path.is_file():
        pytest.skip("canonical ledger absent")

    before = ledger_path.read_bytes()
    proc = subprocess.run(
        [sys.executable, "-m", "v8_next.app.cli", "status", "--output-dir", str(out_dir)],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert proc.returncode == 0, proc.stderr
    assert "chain: VERIFIED" in proc.stdout
    assert "BROKEN" not in proc.stdout
    assert ledger_path.read_bytes() == before

pytestmark = pytest.mark.slow  # #469: tape/engine file, fast loop excludes via -m "not slow"
