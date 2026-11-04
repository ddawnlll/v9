"""#406 / #421 — a gate may only publish a verdict over a window it can name.

The defect this file pins: ``g7_generalization`` was graded from
``candles[-100:]`` (the run's own in-sample tail) with a criterion that is a
function of the price path alone, and ``g6_protected_oos`` labelled whatever
slice ``load_tape_candles`` happened to return (``head(limit)``, i.e. file order)
as frozen out-of-sample. In both cases the verdict did not depend on a declared
window, and in G7's case not on the candidate either.

What is asserted here, in the order of the card's acceptance criteria:

1. an undeclared G6 window mints no PASS (named ``PROTECTED_WINDOW_ABSENT``) and
   mints no claim;
2. a G6 declaration that describes other bars fails closed, while an exact
   declaration leaves the criterion to decide;
3. two candidates with different forward behaviour cannot share one G7 metric on
   the same declared window, and the fixed in-sample slice cannot PASS;
4. the G7 verdict and metrics are byte-identical for the same bar set handed in a
   different order;
5. a fresh canonical-case run publishes no G6/G7 PASS, its inputs carry the named
   reason, its own chain verifies, and the published ledger is not rewritten.

Evaluative assertions run on the real venue tape; test-local bar sets would be
MECHANICS ONLY and carry no evaluative weight. There are none in this file.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from v8_next.adapters.expert_strategy import ExpertStrategyConfig
from v8_next.app import cli
from v8_next.domain.market import Candle
from v8_next.evaluation.benchmark_receipt import BenchmarkLedger, GateState
from v8_next.evaluation.gate_resolution import (
    DEFAULT_TAPE_PATH,
    WindowBinding,
    evaluate_g6_frozen_oos,
    evaluate_g7_prospective_shadow,
    load_tape_candles,
)
from v8_next.evaluation.runner import BenchmarkCase, BenchmarkRunner

#: The canonical case the card names, on the real tape.
CASE = BenchmarkCase(
    case_id="BC-D153-CANONICAL-01",
    policy_id="pol_28_expert_ensemble",
    dataset_name="BTCUSDT-1H",
    strategy_config=ExpertStrategyConfig(min_support_quorum=1, max_contradiction_tolerance=28),
)

#: 150 scored bars + 100 forward bars, all of them real venue capture (#406 needs a
#: window the candidate was not scored on; synthetic candles are not evidence).
SCORED_BARS = 150
FORWARD_BARS = 100


def _published_ledger_path() -> Path:
    """The canonical ledger under the repository root, when this tree has one."""
    return Path(__file__).resolve().parents[2] / "artifacts" / "benchmarks" / "benchmark_ledger.jsonl"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture(scope="module")
def real_bars() -> list[Candle]:
    if not DEFAULT_TAPE_PATH.exists():
        pytest.skip(f"real tape absent at {DEFAULT_TAPE_PATH}")
    bars = load_tape_candles(DEFAULT_TAPE_PATH, limit=SCORED_BARS + FORWARD_BARS)
    if len(bars) < SCORED_BARS + FORWARD_BARS:
        pytest.skip(f"tape holds {len(bars)} bars, need {SCORED_BARS + FORWARD_BARS}")
    return bars


@pytest.fixture(scope="module")
def scored(real_bars: list[Candle]) -> tuple[Candle, ...]:
    return tuple(real_bars[:SCORED_BARS])


@pytest.fixture(scope="module")
def forward(real_bars: list[Candle]) -> tuple[Candle, ...]:
    return tuple(real_bars[SCORED_BARS : SCORED_BARS + FORWARD_BARS])


# --------------------------------------------------------------------------- #
# 1. An undeclared window publishes no verdict and mints no claim
# --------------------------------------------------------------------------- #


def test_g6_without_a_declared_window_publishes_no_pass_and_mints_no_claim(
    tmp_path: Path, scored: tuple[Candle, ...]
) -> None:
    """#406/#421 (1): the run's own loaded slice is not a protected window."""
    cfg = ExpertStrategyConfig(min_support_quorum=1, max_contradiction_tolerance=28)
    state, metrics = evaluate_g6_frozen_oos(scored, cfg)

    assert state != GateState.PASS
    assert state == GateState.BLOCKED
    assert "PROTECTED_WINDOW_ABSENT" in metrics["reason"]
    assert metrics["measurement"] == "unmeasured"
    assert metrics["retention_ratio"] is None
    assert metrics["is_profit"] is None and metrics["oos_profit"] is None
    assert metrics["passed"] is False

    # and the same refusal reaches the runner: no claim is minted from it
    runner = BenchmarkRunner(output_dir=tmp_path / "benchmarks")
    result = runner.run(CASE, scored, resolve_gates=True)
    gate_metrics = result.gate_metrics or {}
    assert result.gates.g6_protected_oos == GateState.BLOCKED
    assert "PROTECTED_WINDOW_ABSENT" in gate_metrics["g6"]["reason"]
    assert result.claim_record is None
    assert not (tmp_path / "benchmarks" / "claims_registry.jsonl").exists()


# --------------------------------------------------------------------------- #
# 2. The declaration must describe the window actually evaluated
# --------------------------------------------------------------------------- #


def test_g6_declaration_must_describe_the_window_it_measures(scored: tuple[Candle, ...]) -> None:
    """#406/#421 (2): a declaration of other bars fails closed; an exact one does not."""
    cfg = ExpertStrategyConfig(min_support_quorum=1, max_contradiction_tolerance=28)
    declared = WindowBinding.from_candles(scored, origin="test_exact", declared_by="test")

    exact_state, exact = evaluate_g6_frozen_oos(scored, cfg, protected_window=declared)
    assert exact["measurement"] == "measured"
    assert exact["protected_window"] == declared.as_dict()
    assert exact["evaluated_window"] == {
        "start_ns": declared.start_ns,
        "end_ns": declared.end_ns,
        "n_bars": declared.n_bars,
    }
    # the binding does not decide the verdict, the criterion does
    assert exact["passed"] == (exact_state == GateState.PASS)
    assert "PROTECTED_WINDOW_MISMATCH" not in str(exact.get("reason"))

    for wrong in (
        WindowBinding(
            start_ns=declared.start_ns,
            end_ns=declared.end_ns,
            n_bars=declared.n_bars - 1,
            origin="off_by_one_bar",
        ),
        WindowBinding(
            start_ns=declared.start_ns + 3_600_000_000_000,
            end_ns=declared.end_ns,
            n_bars=declared.n_bars,
            origin="shifted_one_bar",
        ),
    ):
        state, metrics = evaluate_g6_frozen_oos(scored, cfg, protected_window=wrong)
        assert state == GateState.BLOCKED
        assert "PROTECTED_WINDOW_MISMATCH" in metrics["reason"]
        assert metrics["measurement"] == "unmeasured"
        assert metrics["retention_ratio"] is None
        assert metrics["passed"] is False


# --------------------------------------------------------------------------- #
# 3. G7 discriminates candidates on a declared forward window
# --------------------------------------------------------------------------- #


def test_g7_two_candidates_cannot_share_one_metric_on_a_declared_window(
    tmp_path: Path,
    scored: tuple[Candle, ...],
    forward: tuple[Candle, ...],
) -> None:
    """#406/#421 (3): the verdict is the candidate's, on evidence it was not scored on."""
    window = WindowBinding.from_candles(forward, origin="forward_window_406", declared_by="test")
    strict = ExpertStrategyConfig(min_support_quorum=1, max_contradiction_tolerance=0)
    permissive = ExpertStrategyConfig(min_support_quorum=1, max_contradiction_tolerance=28)

    state_a, metrics_a = evaluate_g7_prospective_shadow(
        scored,
        strict,
        output_dir=tmp_path / "candidate-a",
        shadow_stream=forward,
        shadow_origin="FORWARD_TEST_WINDOW",
        shadow_window=window,
    )
    state_b, metrics_b = evaluate_g7_prospective_shadow(
        scored,
        permissive,
        output_dir=tmp_path / "candidate-b",
        shadow_stream=forward,
        shadow_origin="FORWARD_TEST_WINDOW",
        shadow_window=window,
    )

    assert metrics_a["measurement"] == "measured"
    assert metrics_b["measurement"] == "measured"
    assert metrics_a["exposure_basis"] == "candidate_admitted_opportunity_direction_per_bar"
    # the forward window really is forward of the scored window
    assert metrics_a["shadow_window"]["start_ns"] >= metrics_a["scored_window"]["end_ns"]

    measured_a = (
        metrics_a["final_e_process"],
        metrics_a["final_e_process_raw"],
        metrics_a["final_drift"],
        metrics_a["candidate_exposure_bars"],
    )
    measured_b = (
        metrics_b["final_e_process"],
        metrics_b["final_e_process_raw"],
        metrics_b["final_drift"],
        metrics_b["candidate_exposure_bars"],
    )
    assert measured_a != measured_b or (
        state_a != GateState.PASS and state_b != GateState.PASS
    ), "two candidates with different forward behaviour shared one G7 metric and a PASS"
    # ...and on this reference tape the difference is the measured one: the strict
    # admission rule carries fewer admitted directional bars into the window than the
    # permissive one, which is exactly the candidate dependence #406 asked for.
    assert metrics_a["candidate_exposure_bars"] != metrics_b["candidate_exposure_bars"]

    # the fixed in-sample slice cannot pass, however it is declared
    tail = tuple(scored[-100:])
    tail_state, tail_metrics = evaluate_g7_prospective_shadow(
        scored,
        strict,
        output_dir=tmp_path / "in-sample-tail",
        shadow_stream=tail,
        shadow_origin="SAME_RUN_TAIL",
        shadow_window=WindowBinding.from_candles(tail, origin="same_run_tail"),
    )
    assert tail_state != GateState.PASS
    assert tail_metrics["measurement"] == "unmeasured"
    assert "PROSPECTIVE_WINDOW_NOT_FORWARD_OF_SCORED_WINDOW" in tail_metrics["reason"]


def test_g7_published_trajectory_is_the_declared_forward_window_only(
    tmp_path: Path,
    scored: tuple[Candle, ...],
    forward: tuple[Candle, ...],
) -> None:
    """The G7 receipt on disk holds exactly the declared forward bars."""
    window = WindowBinding.from_candles(forward, origin="forward_window_406", declared_by="test")
    out_dir = tmp_path / "shadow"
    _, metrics = evaluate_g7_prospective_shadow(
        scored,
        output_dir=out_dir,
        shadow_stream=forward,
        shadow_origin="FORWARD_TEST_WINDOW",
        shadow_window=window,
    )

    assert metrics["provenance_status"] == "CALLER_DECLARED_NOT_VERIFIED_BY_GATE"
    assert metrics["shadow_origin"] == "FORWARD_TEST_WINDOW"
    log_path = Path(metrics["log_path"])
    assert log_path.is_file()
    recorded = [json.loads(line) for line in log_path.read_text().strip().splitlines()]
    assert len(recorded) == len(forward)
    scored_end_ns = metrics["scored_window"]["end_ns"]
    assert all(int(step["timestamp_ns"]) > scored_end_ns for step in recorded)
    assert "exposure" in recorded[0]


# --------------------------------------------------------------------------- #
# 4. Order invariance
# --------------------------------------------------------------------------- #


def test_g7_verdict_is_a_function_of_the_bar_set_not_the_order(
    tmp_path: Path,
    scored: tuple[Candle, ...],
    forward: tuple[Candle, ...],
) -> None:
    """#406/#421 (4): the same bars in another order give byte-identical results."""
    window = WindowBinding.from_candles(forward, origin="order_invariance", declared_by="test")
    cfg = ExpertStrategyConfig(min_support_quorum=1, max_contradiction_tolerance=28)

    base_state, base = evaluate_g7_prospective_shadow(
        scored,
        cfg,
        output_dir=tmp_path / "order-base",
        shadow_stream=forward,
        shadow_origin="ORDER_INVARIANCE",
        shadow_window=window,
    )
    reversed_state, reversed_metrics = evaluate_g7_prospective_shadow(
        scored,
        cfg,
        output_dir=tmp_path / "order-reversed",
        shadow_stream=tuple(reversed(forward)),
        shadow_origin="ORDER_INVARIANCE",
        shadow_window=window,
    )

    assert reversed_state == base_state
    assert {k: v for k, v in reversed_metrics.items() if k != "log_path"} == {
        k: v for k, v in base.items() if k != "log_path"
    }
    assert (tmp_path / "order-reversed" / "g7_prospective_shadow.jsonl").read_text() == (
        tmp_path / "order-base" / "g7_prospective_shadow.jsonl"
    ).read_text()


# --------------------------------------------------------------------------- #
# 5. The canonical chain: verified, no fresh G6/G7 PASS, canon ledger untouched
# --------------------------------------------------------------------------- #


def test_fresh_canonical_run_mints_no_g6_g7_pass_and_leaves_the_canon_alone(
    tmp_path: Path,
    scored: tuple[Candle, ...],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """#406/#421 (5): the canonical chain stays VERIFIED, and its inputs name the reason."""
    canon = _published_ledger_path()
    canon_before = _sha256(canon) if canon.is_file() else None

    runner = BenchmarkRunner(output_dir=tmp_path / "benchmarks")
    result = runner.run(
        CASE,
        scored,
        resolve_gates=True,
        run_identity={
            "window_tape_path": str(DEFAULT_TAPE_PATH),
            "window_bars": str(len(scored)),
            "profile": "benchmark",
        },
        scored_window=WindowBinding.from_candles(
            scored, origin="test_scored_window", declared_by="test"
        ),
    )
    gate_metrics = result.gate_metrics or {}

    assert result.gates.g6_protected_oos != GateState.PASS
    assert result.gates.g7_generalization != GateState.PASS
    assert "PROTECTED_WINDOW_ABSENT" in gate_metrics["g6"]["reason"]
    assert gate_metrics["g7"]["reason"] == "PSEUDO_PROSPECTIVE_HISTORICAL_WINDOW_NOT_ACCEPTED"
    assert result.claim_record is None

    binding = gate_metrics["window_binding"]
    assert binding["scored_window"]["n_bars"] == len(scored)
    assert binding["declared_scored_window"]["declared_by"] == "test"
    assert binding["declared_window"]["profile"] == "benchmark"
    assert binding["protected_window"] is None
    assert binding["prospective_window"] is None

    # the receipt the fresh ledger carries keeps both states off PASS
    ledger_path = tmp_path / "benchmarks" / "benchmark_ledger.jsonl"
    ledger = BenchmarkLedger.load_jsonl(ledger_path)
    chain_ok, chain_msg = ledger.verify_chain()
    assert chain_ok is True, chain_msg
    last = ledger.entries[-1].receipt
    assert last.gates.g6_protected_oos != GateState.PASS
    assert last.gates.g7_generalization != GateState.PASS

    # `status` reads that same fresh ledger and reports the chain VERIFIED
    assert cli.main(["status", "--output-dir", str(tmp_path / "benchmarks")]) == 0
    assert "chain: VERIFIED" in capsys.readouterr().out

    # and the published canon was never rewritten by this run
    if canon_before is not None:
        assert _sha256(canon) == canon_before

pytestmark = pytest.mark.slow  # #469: tape/engine file, fast loop excludes via -m "not slow"
