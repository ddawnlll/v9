"""UtilityContract + support classifier contracts (MECHANICS ONLY).

Hand-written fixtures exercising the validation algebra and the seven support gates; no
assertion carries evaluative weight. Real-data coverage lives in
`docs/evidence/v87-port-chain/452/`.
"""

from __future__ import annotations

import pytest

from v8_next.oracle import (
    Action,
    AuthorityLevel,
    CounterfactualAuthority,
    Direction,
    GrammarCandidate,
    HardConstraints,
    Identifiability,
    ModelIds,
    OracleRefusal,
    OracleRefused,
    ScalarPenalties,
    SupportClassifier,
    UtilityContract,
)


def _contract(**overrides: object) -> UtilityContract:
    base: dict[str, object] = {
        "contract_id": "utility-v1",
        "version": "1",
        "primary_objective": "AFTER_COST_NET_UTILITY",
        "horizon": "1h",
        "accounting_currency": "USDT",
        "models": ModelIds(
            fee_model_id="fees-v1",
            funding_model_id="funding-v1",
            slippage_model_id="slip-v1",
            impact_model_id="impact-v1",
        ),
        "hard_constraints": HardConstraints(
            drawdown_max=1.0,
            tail_risk_max=1.0,
            capacity_max=1.0,
            portfolio_heat_max=1.0,
            coverage_min=0.1,
            operational_rule_id="ops-v1",
        ),
        "stress_grid_id": "stress-v1",
        "effective_from": 1,
    }
    base.update(overrides)
    return UtilityContract(**base)  # type: ignore[arg-type]


def test_contract_rejects_incomplete_or_infeasible_without_ranking() -> None:
    valid = _contract()
    valid.validate()
    assert not hasattr(valid, "rank")
    bad_coverage = _contract(
        hard_constraints=HardConstraints(
            drawdown_max=1.0,
            tail_risk_max=1.0,
            capacity_max=1.0,
            portfolio_heat_max=1.0,
            coverage_min=1.1,
            operational_rule_id="ops-v1",
        )
    )
    with pytest.raises(OracleRefused) as excinfo:
        bad_coverage.validate()
    assert excinfo.value.refusal is OracleRefusal.CONSTRAINT_INFEASIBLE
    bad_models = _contract(
        models=ModelIds(
            fee_model_id="fees-v1",
            funding_model_id="funding-v1",
            slippage_model_id="slip-v1",
            impact_model_id="",
        )
    )
    with pytest.raises(OracleRefused):
        bad_models.validate()
    bad_objective = _contract(primary_objective="MAXIMIZE_PNL")
    with pytest.raises(OracleRefused):
        bad_objective.validate()


def test_scalar_penalties_must_be_consistent() -> None:
    inconsistent = _contract(
        optional_scalar_penalties=ScalarPenalties(
            names=("a",), weights=(0.5, 0.5), sensitivity_band=((0.0, 1.0),)
        )
    )
    with pytest.raises(OracleRefused):
        inconsistent.validate()
    consistent = _contract(
        optional_scalar_penalties=ScalarPenalties(
            names=("a",), weights=(0.5,), sensitivity_band=((0.0, 1.0),)
        )
    )
    consistent.validate()


def test_hard_constraint_breach_outranks_scalar_return() -> None:
    constraints = HardConstraints(
        drawdown_max=1.0,
        tail_risk_max=1.0,
        capacity_max=1.0,
        portfolio_heat_max=1.0,
        coverage_min=0.1,
        operational_rule_id="ops-v1",
    )
    assert constraints.ranks_above(breached=True) is False
    assert constraints.ranks_above(breached=False) is True


def _candidate(decision_time: int = 100) -> GrammarCandidate:
    return GrammarCandidate(
        grammar_candidate_id="test-cand-01",
        universe_id="universe-v1",
        template_id="template-breakout",
        instrument="BTCUSDT",
        timeframe="1h",
        direction=Direction.LONG,
        decision_time=decision_time,
        parameters={},
    )


def _action(**geom: object) -> Action:
    return Action(
        action_id="ACTUAL", kind="ACTUAL", provenance="ACTUAL", override_geom=dict(geom)
    )


def test_l1_request_for_l3_is_too_weak() -> None:
    classifier = SupportClassifier.canonical_l1()
    authority, refusal = classifier.evaluate_support(
        _candidate(), _action(), AuthorityLevel.L3, 10
    )
    assert authority.oracle_authority_level is AuthorityLevel.L3
    assert authority.identifiability_status is Identifiability.NOT_IDENTIFIABLE
    assert refusal is OracleRefusal.EXECUTION_AUTHORITY_TOO_WEAK


def test_negative_decision_time_is_missing_data() -> None:
    classifier = SupportClassifier.canonical_l1()
    authority, refusal = classifier.evaluate_support(
        _candidate(decision_time=-1), _action(), AuthorityLevel.L1, 10
    )
    assert authority.identifiability_status is Identifiability.NOT_IDENTIFIABLE
    assert refusal is OracleRefusal.MISSING_DECISION_TIME_DATA


def test_unsupported_size_or_queue_fill_fails_support() -> None:
    classifier = SupportClassifier.canonical_l1()
    _, refusal_size = classifier.evaluate_support(
        _candidate(), _action(size=-1.0), AuthorityLevel.L1, 10
    )
    assert refusal_size is OracleRefusal.OUT_OF_SUPPORT_ACTION
    _, refusal_queue = classifier.evaluate_support(
        _candidate(), _action(queue_priority=1), AuthorityLevel.L1, 10
    )
    assert refusal_queue is OracleRefusal.NON_IDENTIFIABLE_FILL


def test_insufficient_future_horizon_is_undefined_future() -> None:
    classifier = SupportClassifier.canonical_l1()
    authority, refusal = classifier.evaluate_support(
        _candidate(), _action(expiry_bars=24), AuthorityLevel.L1, 5
    )
    assert authority.identifiability_status is Identifiability.NOT_IDENTIFIABLE
    assert refusal is OracleRefusal.UNDEFINED_FUTURE


def test_model_only_counterfactual_is_model_derived() -> None:
    classifier = SupportClassifier.canonical_l1()
    authority, refusal = classifier.evaluate_support(
        _candidate(), _action(model_counterfactual=True), AuthorityLevel.L1, 10
    )
    assert authority.identifiability_status is Identifiability.MODEL_DERIVED
    assert refusal is OracleRefusal.MODEL_ONLY_COUNTERFACTUAL


def test_supported_action_is_identified_without_refusal() -> None:
    classifier = SupportClassifier.canonical_l1()
    authority, refusal = classifier.evaluate_support(
        _candidate(), _action(), AuthorityLevel.L1, 10
    )
    assert authority.identifiability_status is Identifiability.IDENTIFIED
    assert refusal is None
    assert isinstance(
        classifier.classify(_candidate(), _action(), AuthorityLevel.L1),
        CounterfactualAuthority,
    )
