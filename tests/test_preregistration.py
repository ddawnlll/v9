"""M4-01: preregistration content, validation refusals and post-hoc detection."""

from decimal import Decimal
from typing import Any

import pytest
from pydantic import ValidationError

from v8_next.evaluation.preregistration import (
    PREREGISTRATION_VERSION,
    AppliedGates,
    Arm,
    ArmKind,
    ComparisonReceipt,
    CostModel,
    Direction,
    ExperimentResult,
    HypothesisBudget,
    MatchedDimensions,
    PostHocGateChange,
    Preregistration,
    PreregistrationError,
    gate_report,
)
from v8_next.evaluation.protocol import CorrectionMethod, DatasetRole, Verdict

MATCHED = MatchedDimensions(
    calendar="NX01",
    capital=Decimal("10000"),
    venue="BINANCE",
    opportunity_exposure="all_qualified_opportunities",
    exit_policy="bracket_2r",
)


def arms(*, control_capital: Decimal = Decimal("10000")) -> tuple[Arm, ...]:
    matched = (
        MATCHED
        if control_capital == MATCHED.capital
        else MATCHED.model_copy(update={"capital": control_capital})
    )
    return (
        Arm(arm_id="v8", kind=ArmKind.V8_BASELINE, description="V8 baseline", matched=matched),
        Arm(
            arm_id="random",
            kind=ArmKind.RANDOM_ENTRY_MATCHED_EXITS,
            description="random entry, matched exits",
            matched=matched,
        ),
    )


def preregistration(**overrides: Any) -> Preregistration:
    payload: dict[str, Any] = {
        "hypothesis_id": "H-001",
        "statement": "V9 behavioral gating raises per-opportunity net return vs matched controls",
        "intervention": Arm(
            arm_id="v9",
            kind=ArmKind.V9_INTERVENTION,
            description="behavioral gating",
            matched=MATCHED,
        ),
        "control_arms": arms(),
        "unit_of_comparison": "per_opportunity_net_return",
        "dataset_role": DatasetRole.SEALED_OOS,
        "costs": CostModel(
            fee_bps_per_side=Decimal("4"),
            funding_bps_per_day=Decimal("1.5"),
            slippage_bps_per_side=Decimal("2"),
            venue="BINANCE",
        ),
        "minimum_trades": 100,
        "max_drawdown": Decimal("0.20"),
        "primary_metrics": ("net_return",),
        "secondary_metrics": ("sharpe",),
        "expected_direction": Direction.INCREASE,
        "accepted_outcomes": (Verdict.NO_EDGE, Verdict.STOP),
        "decision_rule": (
            "PASS if the corrected primary test rejects at alpha; NO_EDGE when it does not; "
            "STOP when the drawdown gate is breached"
        ),
        "hypothesis_budget": HypothesisBudget(
            family="behavioral",
            max_hypotheses=5,
            max_trials=5,
            alpha=Decimal("0.05"),
            correction=CorrectionMethod.HOLM,
        ),
        "created_ns": 1_760_000_000_000_000_000,
        "code_sha": "a" * 64,
        "config_sha": "b" * 64,
    }
    payload.update(overrides)
    return Preregistration(**payload)


def result_for(registration: Preregistration, **gate_overrides: Any) -> ExperimentResult:
    gates = registration.gates().model_copy(update=gate_overrides)
    return ExperimentResult(
        experiment_id="exp-1",
        hypothesis_id=registration.hypothesis_id,
        preregistration_digest=registration.digest(),
        gates=gates,
        observed_trades=120,
        observed_drawdown=Decimal("0.11"),
        observed_metric=Decimal("0.03"),
        verdict=Verdict.PASS,
    )


def test_valid_preregistration_passes_validation() -> None:
    registration = preregistration()
    assert registration.validate() is registration
    assert registration.version == PREREGISTRATION_VERSION
    assert registration.hypothesis_budget is not None
    assert registration.hypothesis_budget.correction is CorrectionMethod.HOLM


def test_json_round_trip_preserves_digest() -> None:
    registration = preregistration()
    restored = Preregistration.from_json(registration.to_json())
    assert restored == registration
    assert restored.digest() == registration.digest()
    assert restored.validate().digest() == registration.digest()


def test_digest_is_deterministic_and_content_addressed() -> None:
    first = preregistration()
    second = preregistration()
    assert first.digest() == second.digest()

    other_drawdown = preregistration(max_drawdown=Decimal("0.21"))
    assert other_drawdown.digest() != first.digest()

    other_role = preregistration(dataset_role=DatasetRole.VALIDATION)
    assert other_role.digest() != first.digest()

    other_statement = preregistration(statement="a different hypothesis")
    assert other_statement.digest() != first.digest()


def test_gate_vector_records_every_cost_term() -> None:
    gates = preregistration().gates()
    assert gates.fee_bps_per_side == Decimal("4")
    assert gates.funding_bps_per_day == Decimal("1.5")
    assert gates.slippage_bps_per_side == Decimal("2")
    assert gates.accepted_outcomes == (Verdict.NO_EDGE, Verdict.STOP)
    assert set(gate_report(preregistration())) >= {
        "fee_bps_per_side",
        "funding_bps_per_day",
        "slippage_bps_per_side",
    }


def test_validation_rejects_missing_control_arm() -> None:
    with pytest.raises(PreregistrationError) as raised:
        preregistration(control_arms=()).validate()
    assert "control_arms" in raised.value.fields


def test_validation_rejects_control_arm_without_random_entry() -> None:
    only_baseline = (arms()[0],)
    with pytest.raises(PreregistrationError) as raised:
        preregistration(control_arms=only_baseline).validate()
    assert raised.value.field == "control_arms"
    assert "random-entry" in raised.value.message


def test_validation_rejects_unmatched_control_arm() -> None:
    with pytest.raises(PreregistrationError) as raised:
        preregistration(control_arms=arms(control_capital=Decimal("5000"))).validate()
    assert any(field.startswith("control_arms[") for field in raised.value.fields)


@pytest.mark.parametrize(
    "costs, expected_field",
    [
        (None, "costs"),
        (
            CostModel(
                funding_bps_per_day=Decimal("1.5"),
                slippage_bps_per_side=Decimal("2"),
                venue="BINANCE",
            ),
            "costs.fee_bps_per_side",
        ),
        (
            CostModel(
                fee_bps_per_side=Decimal("4"),
                slippage_bps_per_side=Decimal("2"),
                venue="BINANCE",
            ),
            "costs.funding_bps_per_day",
        ),
        (
            CostModel(
                fee_bps_per_side=Decimal("4"),
                funding_bps_per_day=Decimal("1.5"),
                venue="BINANCE",
            ),
            "costs.slippage_bps_per_side",
        ),
    ],
)
def test_validation_rejects_missing_cost_term(costs: CostModel | None, expected_field: str) -> None:
    with pytest.raises(PreregistrationError) as raised:
        preregistration(costs=costs).validate()
    assert expected_field in raised.value.fields


def test_validation_rejects_costs_without_funding() -> None:
    no_funding = CostModel(
        fee_bps_per_side=Decimal("4"),
        funding_bps_per_day=Decimal("0"),
        slippage_bps_per_side=Decimal("2"),
        funding_included=False,
        venue="BINANCE",
    )
    with pytest.raises(PreregistrationError) as raised:
        preregistration(costs=no_funding).validate()
    assert "costs.funding_included" in raised.value.fields


@pytest.mark.parametrize(
    "accepted",
    [
        (),
        (Verdict.NO_EDGE,),
        (Verdict.STOP,),
        (Verdict.INCONCLUSIVE,),
    ],
)
def test_validation_rejects_missing_negative_outcome_rule(
    accepted: tuple[Verdict, ...],
) -> None:
    with pytest.raises(PreregistrationError) as raised:
        preregistration(accepted_outcomes=accepted).validate()
    assert "accepted_outcomes" in raised.value.fields


def test_validation_rejects_blank_decision_rule() -> None:
    with pytest.raises(PreregistrationError) as raised:
        preregistration(decision_rule="   ").validate()
    assert raised.value.field == "decision_rule"


def test_validation_rejects_missing_budget() -> None:
    with pytest.raises(PreregistrationError) as raised:
        preregistration(hypothesis_budget=None).validate()
    assert raised.value.field == "hypothesis_budget"


@pytest.mark.parametrize(
    "budget, expected_field",
    [
        (
            HypothesisBudget(
                family="behavioral",
                max_trials=5,
                alpha=Decimal("0.05"),
                correction=CorrectionMethod.HOLM,
            ),
            "hypothesis_budget.max_hypotheses",
        ),
        (
            HypothesisBudget(
                family="behavioral",
                max_hypotheses=5,
                max_trials=5,
                correction=CorrectionMethod.HOLM,
            ),
            "hypothesis_budget.alpha",
        ),
        (
            HypothesisBudget(
                family="behavioral",
                max_hypotheses=5,
                max_trials=5,
                alpha=Decimal("0.05"),
            ),
            "hypothesis_budget.correction",
        ),
    ],
)
def test_validation_rejects_incomplete_budget(
    budget: HypothesisBudget, expected_field: str
) -> None:
    with pytest.raises(PreregistrationError) as raised:
        preregistration(hypothesis_budget=budget).validate()
    assert expected_field in raised.value.fields


def test_validation_rejects_missing_drawdown_and_trade_gate() -> None:
    with pytest.raises(PreregistrationError) as raised:
        preregistration(max_drawdown=None).validate()
    assert raised.value.field == "max_drawdown"

    with pytest.raises(PreregistrationError) as raised:
        preregistration(minimum_trades=None).validate()
    assert raised.value.field == "minimum_trades"


def test_validation_reports_every_missing_element_at_once() -> None:
    broken = preregistration(
        control_arms=(),
        costs=None,
        accepted_outcomes=(),
        hypothesis_budget=None,
    )
    with pytest.raises(PreregistrationError) as raised:
        broken.validate()
    assert {
        "control_arms",
        "costs",
        "accepted_outcomes",
        "hypothesis_budget",
    } <= set(raised.value.fields)


def test_matching_result_returns_comparison_receipt() -> None:
    registration = preregistration()
    receipt = registration.compare_against_result(result_for(registration))
    assert isinstance(receipt, ComparisonReceipt)
    assert receipt.matched is True
    assert "max_drawdown" in receipt.checked_fields
    assert receipt.preregistration_digest == registration.digest()


@pytest.mark.parametrize(
    "field, mutated",
    [
        ("max_drawdown", Decimal("0.50")),
        ("minimum_trades", 10),
        ("fee_bps_per_side", Decimal("0")),
        ("funding_bps_per_day", Decimal("0")),
        ("slippage_bps_per_side", Decimal("0")),
        ("alpha", Decimal("0.20")),
        ("max_hypotheses", 50),
        ("dataset_role", DatasetRole.DEVELOPMENT),
        ("unit_of_comparison", "per_bar_return"),
        ("primary_metric", "gross_return"),
        ("accepted_outcomes", (Verdict.PASS, Verdict.NO_EDGE)),
    ],
)
def test_post_hoc_gate_change_is_detected_and_named(field: str, mutated: Any) -> None:
    registration = preregistration()
    result = result_for(registration, **{field: mutated})
    with pytest.raises(PostHocGateChange) as raised:
        registration.compare_against_result(result)
    assert raised.value.code == "POST_HOC_GATE_CHANGE"
    assert field in raised.value.fields
    assert field in str(raised.value)


def test_post_hoc_digest_or_hypothesis_swap_is_detected() -> None:
    registration = preregistration()
    swapped = result_for(registration).model_copy(update={"preregistration_digest": "0" * 64})
    with pytest.raises(PostHocGateChange) as raised:
        registration.compare_against_result(swapped)
    assert "preregistration_digest" in raised.value.fields

    renamed = result_for(registration).model_copy(update={"hypothesis_id": "H-OTHER"})
    with pytest.raises(PostHocGateChange) as raised:
        registration.compare_against_result(renamed)
    assert raised.value.fields == ("hypothesis_id",)


def test_gate_vector_covers_every_registered_field() -> None:
    registration = preregistration()
    gates = registration.gates()
    assert tuple(AppliedGates.model_fields) == (
        "dataset_role",
        "unit_of_comparison",
        "primary_metric",
        "minimum_trades",
        "max_drawdown",
        "alpha",
        "max_hypotheses",
        "correction",
        "fee_bps_per_side",
        "funding_bps_per_day",
        "slippage_bps_per_side",
        "accepted_outcomes",
    )
    assert gates.primary_metric == "net_return"
    assert gates.max_drawdown == registration.max_drawdown


def test_registration_model_is_frozen() -> None:
    registration = preregistration()
    with pytest.raises(ValidationError):
        registration.max_drawdown = Decimal("0.99")  # type: ignore[misc]
    with pytest.raises(ValidationError):
        registration.costs = None  # type: ignore[assignment]
