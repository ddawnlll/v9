"""M3-02: immutable thesis and recurrent, explanatory position evaluation."""

from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError

from v8_next.domain.decision import DecisionAction
from v8_next.economics.thesis import (
    ExpertEvidence,
    InvalidationKind,
    InvalidationRule,
    PositionSnapshot,
    PositionThesis,
    StanceKind,
    StopKind,
    TargetPlan,
    ThesisBelief,
    ThesisHistory,
    ThesisReason,
    ThesisStatus,
    build_thesis,
    evaluate_position,
    make_thesis_id,
)

D = Decimal
BELIEF_HASH = "a" * 64
NOW = 1_000


def rules() -> tuple[InvalidationRule, ...]:
    return (
        InvalidationRule(kind=InvalidationKind.PRICE_STOP),
        InvalidationRule(kind=InvalidationKind.REGIME_CHANGE),
        InvalidationRule(kind=InvalidationKind.EXPERT_CONTRADICTION, threshold=D("0.5")),
        InvalidationRule(kind=InvalidationKind.TIME_LIMIT),
        InvalidationRule(kind=InvalidationKind.PROTECTION_GAP),
    )


def thesis(
    *,
    side: str = "LONG",
    entry: str = "50000",
    stop: str = "49000",
    created_ns: int = 100,
) -> PositionThesis:
    return build_thesis(
        entry_decision_id="dec-" + "b" * 60,
        original_opportunity_id="opp-1",
        original_market_belief_id=BELIEF_HASH,
        native_position_id="pos-1",
        instrument_id="BTCUSDT-PERP.BINANCE",
        side=side,
        entry_price=D(entry),
        initial_stop_price=D(stop),
        initial_stop_kind=StopKind.NATIVE_PROTECTIVE,
        original_risk_usdt=D("100"),
        target_plan=TargetPlan(target_price=D("52000"), expected_bars=8, max_bars=24),
        invalidation_rules=rules(),
        entry_regime="TREND_UP",
        policy_version="thesis-policy-v1",
        created_ns=created_ns,
    )


def belief(
    *, regime: str = "TREND_UP", stale: bool = False, abstain: bool = False, as_of: int = NOW
) -> ThesisBelief:
    return ThesisBelief(
        belief_id="belief-1",
        version_hash=BELIEF_HASH,
        regime=regime,
        as_of_ns=as_of,
        stale=stale,
        abstain=abstain,
    )


def snapshot(**overrides: object) -> PositionSnapshot:
    kwargs: dict[str, object] = {
        "native_position_id": "pos-1",
        "quantity": D("0.1"),
        "average_price": D("50000"),
        "mark_price": D("50500"),
        "protective_order_ids": ("stop-order-1",),
        "latest_belief_id": "belief-1",
        "funding_exposure": D("-0.5"),
        "elapsed_bars": 5,
        "available_at_ns": 900,
    }
    kwargs.update(overrides)
    return PositionSnapshot(**kwargs)  # type: ignore[arg-type]


def evaluate(**overrides: object):
    snap = overrides.pop("snapshot", None)
    kwargs: dict[str, object] = {
        "belief": belief(),
        "now_ns": NOW,
        "evidence": (),
    }
    kwargs.update(overrides)
    return evaluate_position(
        thesis(), snapshot() if snap is None else snap, **kwargs  # type: ignore[arg-type]
    )


def test_thesis_id_is_deterministic_and_receipt_is_advisory() -> None:
    first, second = thesis(), thesis()
    assert first.thesis_id == second.thesis_id
    assert first.thesis_id == make_thesis_id(
        entry_decision_id=first.entry_decision_id,
        original_opportunity_id=first.original_opportunity_id,
        original_market_belief_id=first.original_market_belief_id,
        native_position_id=first.native_position_id,
        policy_version=first.policy_version,
        created_ns=first.created_ns,
    )
    assert first.to_receipt()["advisory_only"] is True
    assert first.original_stop_distance == D("1000")


def test_thesis_is_immutable_and_rejects_order_authority() -> None:
    item = thesis()
    with pytest.raises(ValidationError):
        item.initial_stop_price = D("48000")  # type: ignore[misc]
    with pytest.raises(ValidationError):
        PositionThesis.model_validate({**item.model_dump(), "advisory_only": False})
    with pytest.raises(ValidationError):
        PositionThesis.model_validate({**item.model_dump(), "thesis_id": "0" * 64})


def test_stop_must_be_on_the_correct_side() -> None:
    with pytest.raises(ValidationError, match="LONG stop"):
        thesis(stop="51000")
    with pytest.raises(ValidationError, match="SHORT stop"):
        thesis(side="SHORT", stop="49000")


def test_healthy_thesis_holds() -> None:
    result = evaluate()
    assert result.status is ThesisStatus.HOLDING
    assert result.reasons == (ThesisReason.THESIS_INTACT,)
    assert result.proposals == (DecisionAction.HOLD,)
    assert result.admission_ok


def test_stop_breach_invalidates_and_proposes_exit_only() -> None:
    result = evaluate(snapshot=snapshot(mark_price=D("48999")))
    assert result.status is ThesisStatus.INVALIDATED
    assert result.reasons == (ThesisReason.STOP_BREACHED,)
    assert result.proposals == (DecisionAction.EXIT,)
    assert not result.admission_ok


def test_regime_change_invalidates() -> None:
    result = evaluate(belief=belief(regime="RANGE"))
    assert result.status is ThesisStatus.INVALIDATED
    assert ThesisReason.REGIME_CHANGED in result.reasons


def test_expert_contradiction_weakens() -> None:
    evidence = (
        ExpertEvidence(expert_id="e1", stance=StanceKind.SUPPORT, weight=D("1")),
        ExpertEvidence(expert_id="e2", stance=StanceKind.CONTRADICT, weight=D("2")),
    )
    result = evaluate(evidence=evidence)
    assert result.status is ThesisStatus.WEAKENING
    assert result.reasons == (ThesisReason.EXPERT_CONTRADICTION,)
    assert result.proposals == (DecisionAction.REDUCE,)


def test_time_limit_weakens() -> None:
    result = evaluate(snapshot=snapshot(elapsed_bars=24))
    assert result.status is ThesisStatus.WEAKENING
    assert result.reasons == (ThesisReason.TIME_LIMIT,)


def test_protection_gap_waits_instead_of_reducing_blindly() -> None:
    result = evaluate(snapshot=snapshot(protective_order_ids=()))
    assert result.status is ThesisStatus.WEAKENING
    assert result.reasons == (ThesisReason.PROTECTION_GAP,)
    assert result.proposals == (DecisionAction.WAIT,)
    assert not result.admission_ok


def test_stale_or_missing_belief_is_missing_input_not_hold() -> None:
    stale = evaluate(belief=belief(stale=True))
    missing = evaluate(belief=None)
    abstain = evaluate(belief=belief(abstain=True))
    for result, reason in (
        (stale, ThesisReason.BELIEF_STALE),
        (missing, ThesisReason.BELIEF_MISSING),
        (abstain, ThesisReason.BELIEF_ABSTAIN),
    ):
        assert result.status is ThesisStatus.MISSING_INPUT
        assert result.reasons == (reason,)
        assert result.proposals == (DecisionAction.WAIT,)
        assert not result.admission_ok


def test_future_belief_is_rejected() -> None:
    result = evaluate(belief=belief(as_of=NOW + 1))
    assert result.status is ThesisStatus.MISSING_INPUT
    assert result.reasons == (ThesisReason.FUTURE_INPUT,)


def test_native_position_identity_mismatch_fails_closed() -> None:
    result = evaluate(snapshot=snapshot(native_position_id="pos-other"))
    assert result.status is ThesisStatus.MISSING_INPUT
    assert result.reasons == (ThesisReason.POSITION_ID_MISMATCH,)


def test_closed_position_is_explicit() -> None:
    result = evaluate(snapshot=snapshot(quantity=D("0"), average_price=D("0")))
    assert result.status is ThesisStatus.CLOSED
    assert result.proposals == (DecisionAction.WAIT,)


def test_duplicate_expert_evidence_is_invalid() -> None:
    evidence = (
        ExpertEvidence(expert_id="e1", stance=StanceKind.SUPPORT),
        ExpertEvidence(expert_id="e1", stance=StanceKind.CONTRADICT),
    )
    result = evaluate(evidence=evidence)
    assert result.status is ThesisStatus.MISSING_INPUT
    assert result.reasons == (ThesisReason.EVIDENCE_INVALID,)


def test_reason_order_is_input_order_independent() -> None:
    evidence = (
        ExpertEvidence(expert_id="e1", stance=StanceKind.SUPPORT, weight=D("1")),
        ExpertEvidence(expert_id="e2", stance=StanceKind.CONTRADICT, weight=D("2")),
    )
    forward = evaluate(
        snapshot=snapshot(elapsed_bars=24, protective_order_ids=()), evidence=evidence
    )
    reverse = evaluate(
        snapshot=snapshot(elapsed_bars=24, protective_order_ids=()),
        evidence=tuple(reversed(evidence)),
    )
    assert forward.reasons == reverse.reasons
    assert forward.evaluation_id == reverse.evaluation_id


def test_evaluation_does_not_mutate_thesis_and_replays_identically() -> None:
    item = thesis()
    before = item.model_dump()
    first = evaluate_position(
        item, snapshot(), belief(), now_ns=NOW, evidence=()
    )
    second = evaluate_position(
        item, snapshot(), belief(), now_ns=NOW, evidence=()
    )
    assert item.model_dump() == before
    assert first.evaluation_id == second.evaluation_id
    assert first.model_dump() == second.model_dump()


def test_history_is_append_only_and_idempotent() -> None:
    item = thesis()
    history = ThesisHistory(thesis_id=item.thesis_id)
    evaluation = evaluate_position(item, snapshot(), belief(), now_ns=NOW)
    once = history.record(evaluation)
    twice = once.record(evaluation)
    assert once == twice
    assert twice.digest == once.digest
    assert len(twice.evaluations) == 1
    later = evaluate_position(
        item, snapshot(mark_price=D("48900")), belief(), now_ns=NOW + 1
    )
    extended = twice.record(later)
    assert len(extended.evaluations) == 2
    assert extended.digest != twice.digest


def test_history_rejects_another_thesis_evaluation() -> None:
    history = ThesisHistory(thesis_id="other")
    evaluation = evaluate_position(thesis(), snapshot(), belief(), now_ns=NOW)
    with pytest.raises(ValueError, match="different thesis"):
        history.record(evaluation)


def test_invalidation_rule_threshold_validation() -> None:
    with pytest.raises(ValidationError):
        InvalidationRule(kind=InvalidationKind.EXPERT_CONTRADICTION, threshold=D("0"))
    with pytest.raises(ValidationError):
        InvalidationRule(kind=InvalidationKind.EXPERT_CONTRADICTION, threshold=D("1.1"))
    with pytest.raises(ValidationError):
        InvalidationRule(kind=InvalidationKind.PRICE_STOP, threshold=D("0.5"))
