"""The post-G9 receipt re-states the run it re-mints — including a measured G2.

The defect: when ``resolve_gates`` reaches ``g9_state == PASS``, ``BenchmarkRunner.run``
mints a *second* receipt for the same run and appends it to the same durable ledger.
That re-mint handed the gate rule every structural input the pre-G9 vector had
(``has_continuous_lineage``) except one: ``g2_state``. ``evaluate_gate_vector`` resolves
an absent ``g2_state`` to ``UNKNOWN`` (``mismatches=None`` is not a zero-mismatch count),
so the two ledger entries for one run disagreed about a hard gate — entry N carried the
measured rerun-parity state, entry N+1 carried ``UNKNOWN``.

These tests drive the real runner through that branch. The engine runs for real on the
hand-made bars below and the determinism rerun is the runner's own measurement; the gate
battery G3-G7 and the G9 authority are substituted only because they are preconditions
of entering the branch, not the subject of it:

* G3-G7 need the four-year tape and a declared protected/prospective window, and
* G9 (``ClaimRegistry.verify_ledger_and_issue_claim``) requires G0-G7 ``established``;
  this runner path never measures G1 (``is_causal_pit=None`` -> ``UNKNOWN``), so the
  branch is unreachable on any current input. It is reached here by declaration, and the
  test first asserts that the re-mint actually happened before asserting anything about
  the cell it publishes.

Evidence class: producer wiring (mechanics). The bars are hand-made, marked MECHANICS
ONLY and never quoted as a result. No test here requires a capability score, an economic
number or a G9 PASS of its own; what is under test is *which* inputs decide the G2 cell
of the receipt the branch publishes.
"""

from __future__ import annotations

import ast
from decimal import Decimal
from pathlib import Path

import pytest

from v8_next.adapters import expert_strategy as expert_strategy_module
from v8_next.adapters.expert_strategy import ExpertStrategyConfig
from v8_next.domain.market import Candle
from v8_next.evaluation import runner as runner_module
from v8_next.evaluation.benchmark_receipt import GateState
from v8_next.evaluation.gate_resolution import MEASURED, evaluate_gate_vector
from v8_next.evaluation.runner import BenchmarkCase, BenchmarkRunner
from v8_next.evaluation.scoring import evaluate_gate_vector as scoring_evaluate_gate_vector

HOUR_NS = 3600 * 10**9
#: MECHANICS ONLY: a non-zero epoch for the bars, so the run carries a pinned instant.
BASE_NS = 1_700_000_000_000_000_000

#: The structural cell this card is about.
G2_FIELD = "g2_determinism_ledger"

#: The reason the runner's own rerun publishes when two executions agreed (#runner:507).
RERUN_PARITY_REASON = "DETERMINISM_RERUN_EXACT_FILL_MATCH"


def _mechanics_candle(
    i: int,
    open_: float,
    high: float,
    low: float,
    close: float,
    volume: float = 100.0,
) -> Candle:
    """MECHANICS ONLY: one hand-made bar (no market data, no economic reading)."""
    return Candle(
        "BTCUSDT-PERP.BINANCE",
        BASE_NS + i * HOUR_NS,
        BASE_NS + (i + 1) * HOUR_NS,
        Decimal(str(open_)),
        Decimal(str(high)),
        Decimal(str(low)),
        Decimal(str(close)),
        Decimal(str(volume)),
        BASE_NS + (i + 1) * HOUR_NS,
        BASE_NS + (i + 1) * HOUR_NS,
        "mechanics-only-post-g9",
    )


def _breakout_candles() -> tuple[Candle, ...]:
    """MECHANICS ONLY: 48 flat bars then one breakout, so the engine really fills.

    Without a fill the determinism rerun has no signature to compare and G2 stays
    UNKNOWN, which is not the state the pre-G9 receipt may be in for this test.
    """
    flat = [_mechanics_candle(i, 100.0, 100.5, 99.5, 100.0) for i in range(48)]
    breakout = _mechanics_candle(48, 101.0, 122.0, 100.0, 120.0, volume=500.0)
    follow = _mechanics_candle(49, 120.0, 125.0, 119.0, 123.0, volume=200.0)
    return tuple([*flat, breakout, follow])


def _passing_battery():
    """A G3-G7 evaluator that declares itself measured and passing."""

    def _evaluator(*_args, **_kwargs):
        # #495 R3: the runner publishes the G3 basis beside the state, so a forced
        # PASS declares the regime partition it stands for. The other G4-G7
        # consumers ignore these keys.
        return GateState.PASS, {
            "measurement": MEASURED,
            "reason": "FORCED_BRANCH_PRECONDITION",
            "regimes": {
                "forced": {
                    "bars": 1,
                    "trades": 1,
                    "max_drawdown": 0.0,
                    "return_variance": 0.0,
                }
            },
            "max_observed_drawdown": 0.0,
            "max_observed_variance": 0.0,
            "max_drawdown_limit": 0.10,
            "min_trades_per_regime": 1,
            "min_regime_trades": 1,
            "empty_regimes": [],
        }

    return _evaluator


def _fill_records_from_the_engine_cache(engine):
    """The engine's own fills, read from the objects it recorded (no pandas here).

    ``native_fill_records`` normalises ``engine.generate_order_fills_report()`` into
    records, and that reporter ships only with the optional pandas dependency: in this
    environment it raises ``ImportError`` and *every* run reports ``fills_count == 0``,
    so no run would have a measurable rerun for G2 to be a state of. This reads the same
    fill facts off the engine's own filled orders -- instrument, side, filled quantity,
    average price, venue order id, liquidity side, fill instant -- measured by the
    backtest, not declared by the test. Determinism and rerun parity stay the runner's
    own comparison over these records.
    """
    records = []
    for order in engine.cache.orders():
        if order.filled_qty is None or float(order.filled_qty) <= 0:
            continue
        records.append(
            {
                "instrument_id": str(order.instrument_id),
                "side": order.side.name,
                "quantity": str(order.quantity),
                "filled_qty": str(order.filled_qty),
                "avg_px": str(order.avg_px),
                "venue_order_id": str(order.venue_order_id),
                "liquidity_side": order.liquidity_side.name,
                "ts_last": int(order.ts_last),
            }
        )
    return records, "REAL_ENGINE_CACHE_ORDERS"


def _passing_authority():
    """A G9 authority that grants the certificate, so the branch under test is entered."""

    def _authority(**_kwargs):
        return GateState.PASS, {"reason": "FORCED_BRANCH_PRECONDITION"}, None

    return _authority


def _run_to_the_post_g9_branch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Run the real runner down the post-G9 re-mint branch; return (result, runner)."""
    battery = _passing_battery()
    for name in (
        "evaluate_g3_scenario_robustness",
        "evaluate_g4_synthetic_falsification",
        "evaluate_g5_selection_control",
        "evaluate_g6_frozen_oos",
        "evaluate_g7_prospective_shadow",
    ):
        monkeypatch.setattr(runner_module, name, battery)
    monkeypatch.setattr(runner_module, "evaluate_g9_certificate_authority", _passing_authority())
    monkeypatch.setattr(
        expert_strategy_module, "native_fill_records", _fill_records_from_the_engine_cache
    )

    case = BenchmarkCase(
        case_id="BC-POST-G9-G2",
        policy_id="pol_28_ensemble",
        dataset_name="MECHANICS-ONLY",
        strategy_config=ExpertStrategyConfig(
            min_support_quorum=1,
            max_contradiction_tolerance=28,
            order_quantity=Decimal("0.010"),
        ),
    )
    runner = BenchmarkRunner(output_dir=tmp_path / "benchmarks")
    result = runner.run(case, _breakout_candles(), resolve_gates=True, measure_determinism=True)
    return result, runner


# --------------------------------------------------------------------------- #
# 1 — the re-mint publishes the same measured G2 cell as the run it re-states
# --------------------------------------------------------------------------- #


def test_post_g9_receipt_publishes_the_measured_g2_of_the_run_it_restates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result, runner = _run_to_the_post_g9_branch(tmp_path, monkeypatch)

    # The engine really filled and the runner's own rerun really compared two runs.
    g2_metric = (result.gate_metrics or {}).get("g2") or {}
    assert g2_metric.get("reason") == RERUN_PARITY_REASON, (
        "the fixture must measure G2 on this run's own fills; the runner reported "
        f"{g2_metric.get('reason')!r} instead of {RERUN_PARITY_REASON!r}"
    )

    entries = runner.ledger.entries
    # The branch was actually entered: the pre-G9 entry and the post-G9 re-mint.
    assert len(entries) == 2, (
        "the post-G9 re-mint did not happen, so this test would prove nothing about it; "
        f"ledger entries: {len(entries)}"
    )
    pre_g9 = entries[0].receipt.gates
    post_g9 = entries[1].receipt.gates
    assert pre_g9.g2_determinism_ledger is GateState.PASS

    # Acceptance: the re-mint re-states this same run, so it cannot drop the cell.
    assert post_g9.g2_determinism_ledger is pre_g9.g2_determinism_ledger, (
        "the post-G9 receipt re-states the same run but publishes "
        f"{post_g9.g2_determinism_ledger.value} for {G2_FIELD} while the pre-G9 entry it "
        f"re-mints publishes {pre_g9.g2_determinism_ledger.value}: the re-mint reaches "
        "evaluate_gate_vector without g2_state, and the rule resolves the absent "
        "argument to UNKNOWN"
    )
    assert post_g9.g2_determinism_ledger is not GateState.UNKNOWN

    # Nothing else about the structural trio may diverge either.
    for field in ("g0_identity", "g1_causal_pit", G2_FIELD):
        assert getattr(post_g9, field) is getattr(pre_g9, field), (
            f"the post-G9 receipt disagrees with the pre-G9 entry about {field}"
        )


# --------------------------------------------------------------------------- #
# 2 — why the absent argument mattered: no g2_state -> UNKNOWN, never PASS
# --------------------------------------------------------------------------- #


def test_the_absent_g2_state_is_what_resolved_the_cell_to_unknown() -> None:
    """Documents the defect class: a hard-gate cell decided by an absent argument."""
    rule = scoring_evaluate_gate_vector

    measured = rule(
        total_bars=50,
        total_trades=1,
        pnl_series=[1.0],
        mismatches=None,
        has_continuous_lineage=True,
        is_causal_pit=None,
        g2_state=GateState.PASS,
    )
    assert measured.g2_determinism_ledger is GateState.PASS

    absent = rule(
        total_bars=50,
        total_trades=1,
        pnl_series=[1.0],
        mismatches=None,
        has_continuous_lineage=True,
        is_causal_pit=None,
    )
    assert absent.g2_determinism_ledger is GateState.UNKNOWN

    # The rule itself is untouched by this card: both runner call sites name the input
    # explicitly, so no path depends on this resolution of an absent argument.
    assert evaluate_gate_vector is rule


# --------------------------------------------------------------------------- #
# 3 — the call sites: both gate-rule calls carry the same structural inputs
# --------------------------------------------------------------------------- #


def test_every_runner_gate_rule_call_hands_over_the_measured_g2() -> None:
    source_path = Path(runner_module.__file__)
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "evaluate_gate_vector"
    ]
    # The run's own vector (with and without the G3-G8 battery) and the post-G9 re-mint.
    assert len(calls) == 3, (
        "runner.py reaches the gate rule at three stages (resolve_gates pre-G9, "
        f"no-battery, post-G9 re-mint); found {len(calls)}"
    )

    for call in calls:
        kwargs = {keyword.arg for keyword in call.keywords}
        assert "g2_state" in kwargs, (
            "a runner gate-rule call reaches evaluate_gate_vector without g2_state "
            f"(line {call.lineno}), so the rule's own resolution of the absent argument "
            "decides G2"
        )

    # Every stage names the same measured state, not an independent derivation.
    supplied = {
        ast.unparse(keyword.value).strip()
        for call in calls
        for keyword in call.keywords
        if keyword.arg == "g2_state"
    }
    assert supplied == {"_g2_state"}, (
        f"the runner's gate-rule calls hand over different G2 inputs: {sorted(supplied)}"
    )

    # A zero-mismatch default must not reappear in place of the measured rerun.
    mismatch_values = [
        ast.unparse(keyword.value).strip()
        for call in calls
        for keyword in call.keywords
        if keyword.arg == "mismatches"
    ]
    assert mismatch_values == ["None", "None", "None"], (
        "a runner gate-rule call stopped passing an unmeasured mismatch count: "
        f"{mismatch_values}"
    )
