"""D-153 benchmark-fabric delta contracts (MECHANICS ONLY where synthetic).

Hand-built fixtures exercising the ported arithmetic (observation clamps,
synthetic asymmetry, partitioner shapes, projection guards, minerva seal,
kaizen debt penalty, disagreement refusal); no assertion carries economic or
evaluative weight. Real-window receipts live in
``docs/evidence/v87-port-chain/454/``.
"""

from __future__ import annotations

import pytest

from v8_next.evaluation import external as ext
from v8_next.evaluation import kaizen_delta as kz
from v8_next.evaluation import minerva as mv
from v8_next.evaluation import observation as obs
from v8_next.evaluation import population as pop
from v8_next.evaluation import projection as proj
from v8_next.evaluation import synthetic as syn
from v8_next.evaluation.benchmark_receipt import GateState
from v8_next.evaluation.scoring import CapabilityDomain


def test_observation_scores_clamp_and_registry_absent_is_not_zero() -> None:
    made = obs.MetricObservation.create(
        "m", CapabilityDomain.RegimeRobustness, "auth", "DEVELOPMENT",
        1.5, 1.4, -0.2, 2.0, 10, 8.0, True,
    )
    assert made.normalized_score == 1.0
    assert made.lower_bound_95 == 0.0
    assert made.upper_bound_95 == 1.0
    registry = obs.ObservationRegistry()
    assert registry.for_domain(CapabilityDomain.RegimeRobustness) == ()
    registry.record(made)
    assert registry.for_domain(CapabilityDomain.RegimeRobustness) == (made,)


def test_synthetic_asymmetry_and_gate() -> None:
    passed = syn.SyntheticEvaluationResult.evaluate("g", True, True)
    assert passed.epistemic_weight == 0.0
    failed = syn.SyntheticEvaluationResult.evaluate("g", True, False, "WS-1")
    assert failed.epistemic_weight == 1.0
    assert failed.claim == "NO_ECONOMIC_CLAIM"
    with pytest.raises(ValueError, match="WORLD_REJECTED"):
        syn.SyntheticEvaluationResult.evaluate("g", False, True)


def test_population_frozen_oos_role_refused() -> None:
    seg = pop.PopulationSegment(
        pop.EvaluationPopulation.PROTECTED_FROZEN_OOS, "s", 0, 10, pop.DataRole.DEVELOPMENT
    )
    with pytest.raises(ValueError, match="FROZEN_OOS_MISMATCH"):
        seg.audit_access()
    ok_seg = pop.PopulationSegment(
        pop.EvaluationPopulation.PROTECTED_FROZEN_OOS, "s", 0, 10, pop.DataRole.FROZEN_OOS
    )
    ok_seg.audit_access()
    assert pop.EvaluationPopulation.FOUNDRY_SYNTHETIC_NOVELTY.is_synthetic()
    assert not pop.EvaluationPopulation.BURNED_DIAGNOSTIC_REAL.is_synthetic()


def test_cpcv_and_walk_forward_shapes() -> None:
    cpcv = pop.CpcvPartitioner(4, 1, 0, 0)
    splits = cpcv.generate_splits(0, 400)
    assert len(splits) == 4  # C(4,1)
    assert all(len(s.test_segments) == 1 for s in splits)
    with pytest.raises(ValueError, match="CPCV_CONFIG"):
        pop.CpcvPartitioner(1, 1, 0, 0)
    wf = pop.WalkForwardPartitioner(3, True, 0.7, 0, 0)
    assert len(wf.generate_splits(0, 400)) == 3
    with pytest.raises(ValueError, match="WF_CONFIG"):
        pop.WalkForwardPartitioner(0, True, 0.7, 0, 0)


def test_projection_guards_and_bands() -> None:
    returns = [12.0, -8.0, 25.0, -15.0, 30.0, -5.0]
    made = proj.project_from_returns(
        policy_id="p", benchmark_receipt_id="r", capability_score=0.5,
        trade_returns_bps=returns, initial_capital_usd=10000.0,
        has_synthetic_population_only=False,
    )
    assert made.is_realized_pnl is False
    assert made.forward_claim_authorized is False
    assert [b.percentile for b in made.outcome_bands] == [0.25, 0.50, 0.75]  # n<25 suppresses extremes
    with pytest.raises(ValueError, match="PROJECTION_FLOOR"):
        proj.project_from_returns(
            policy_id="p", benchmark_receipt_id="r", capability_score=0.1,
            trade_returns_bps=returns, initial_capital_usd=10000.0,
            has_synthetic_population_only=False,
        )
    with pytest.raises(ValueError, match="BFS004"):
        proj.project_from_returns(
            policy_id="p", benchmark_receipt_id="r", capability_score=0.5,
            trade_returns_bps=returns, initial_capital_usd=10000.0,
            has_synthetic_population_only=True,
        )
    with pytest.raises(ValueError, match="BFS018"):
        proj.project_from_returns(
            policy_id="p", benchmark_receipt_id="r", capability_score=0.5,
            trade_returns_bps=returns, initial_capital_usd=500000.0,
            has_synthetic_population_only=False,
        )


def test_monte_carlo_deterministic_and_ruin() -> None:
    returns = [10.0, -5.0, 20.0, -12.0, 8.0, 15.0, -3.0, 25.0]
    once = proj.simulate_monte_carlo_futures(returns, 10000.0, 64, 12, 454)
    twice = proj.simulate_monte_carlo_futures(returns, 10000.0, 64, 12, 454)
    assert once.p50_terminal_usd == twice.p50_terminal_usd
    assert 0.0 <= once.risk_of_ruin_pct <= 100.0
    with pytest.raises(ValueError, match="PROJECTION_MC_DIMS"):
        proj.simulate_monte_carlo_futures(returns, 10000.0, 0, 12, 454)


def test_minerva_seal_needs_all_gates_and_score() -> None:
    denied = mv.MinervaEvaluator.evaluate(0.5, 0.9, 0.4, 10.0, 100.0, -5000.0, -1500.0)
    assert denied.seal_granted is False
    assert denied.effective_score < 80.0
    assert denied.gate_vector.failed_gate_count() == 5
    assert denied.claim == "NO_ECONOMIC_CLAIM"
    with pytest.raises(ValueError, match="MINERVA_INPUT"):
        mv.MinervaEvaluator.evaluate(float("nan"), 0.1, 0.01, 200.0, 100.0, 0.0, -1500.0)


def test_kaizen_delta_pareto_and_debt_penalty() -> None:
    inc = {d: 0.5 for d in CapabilityDomain}
    chal = {d: 0.6 for d in CapabilityDomain}
    delta = kz.compute_delta("i", "c", inc, chal, 0.5, 0.6, 10)
    assert delta.challenger_is_pareto_superior is True
    assert delta.accrued_research_debt_penalty == pytest.approx(0.046, abs=0.005)
    assert delta.composite_delta == pytest.approx(0.1 - delta.accrued_research_debt_penalty)
    degraded = dict(chal)
    degraded[CapabilityDomain.RegimeRobustness] = 0.4
    not_pareto = kz.compute_delta("i", "c", inc, degraded, 0.5, 0.6, 1)
    assert not_pareto.challenger_is_pareto_superior is False
    assert not_pareto.accrued_research_debt_penalty == 0.0
    with pytest.raises(ValueError, match="KAIZEN_TRIALS"):
        kz.compute_delta("i", "c", inc, chal, 0.5, 0.6, 0)


def test_disagreement_detector_refuses_blocked_and_diverged() -> None:
    from v8_next.evaluation.parity import ParityOutcomeKind

    class _Receipt:
        def __init__(self, outcome: ParityOutcomeKind) -> None:
            self.outcome = outcome

        def is_agreement(self) -> bool:
            return self.outcome == ParityOutcomeKind.EXACT_MATCH

    ext.DisagreementDetector.assert_parity(_Receipt(ParityOutcomeKind.EXACT_MATCH))  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="PARITY_DATA_BLOCKED"):
        ext.DisagreementDetector.assert_parity(_Receipt(ParityOutcomeKind.DATA_BLOCKED))  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="PARITY_DIVERGED"):
        ext.DisagreementDetector.assert_parity(_Receipt(ParityOutcomeKind.DIVERGED))  # type: ignore[arg-type]
    assert ext.DisagreementDetector.check_order_semantics(("LIMIT",), "LIMIT") is None
    finding = ext.DisagreementDetector.check_order_semantics(("LIMIT",), "STOP")
    assert finding is not None and finding.finding == "UNSUPPORTED_ORDER_SEMANTICS"


def test_gate_state_is_pass_only_for_pass() -> None:
    assert GateState.PASS.is_pass()
    assert not GateState.BLOCKED.is_pass()
    assert not GateState.UNKNOWN.is_pass()
