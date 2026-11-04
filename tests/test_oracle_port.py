"""Port of v8-core/src/oracle: taxonomy closure, Rule 12, and fail-closed outcomes."""

from __future__ import annotations

import pytest

from v8_next.oracle import (
    AuditState,
    AuthorityError,
    AuthorityLevel,
    CounterfactualAuthority,
    CounterfactualAuthorityLevel,
    EconomicEvidenceStage,
    Identifiability,
    OracleContext,
    OracleOutcome,
    OracleRefusal,
    OracleRole,
    StatisticalVerdict,
    UnknownReasonCode,
    VerificationDimension,
)
from v8_next.oracle.authority import OracleRefused

_SIMPLE = CounterfactualAuthority(
    oracle_authority_level=AuthorityLevel.L2,
    identifiability_status=Identifiability.IDENTIFIED,
    support_rule_id="rule",
    environment_model_id="candle-replay-v1",
)


def test_refusal_vocabulary_is_closed() -> None:
    assert OracleRefusal.MISSING_DECISION_TIME_DATA.code() == "MISSING_DECISION_TIME_DATA"
    assert OracleRefusal.CONSTRAINT_INFEASIBLE.code() == "CONSTRAINT_INFEASIBLE"
    assert UnknownReasonCode.NON_IDENTIFIABLE_FILL.code() == "NON_IDENTIFIABLE_FILL"
    assert len(list(OracleRefusal)) == 9


def test_authority_ladder_is_ordered_and_roles_are_three() -> None:
    assert AuthorityLevel.L1 < AuthorityLevel.L2 < AuthorityLevel.L3 < AuthorityLevel.LIVE_RECEIPT
    assert len(list(OracleRole)) == 3
    assert CounterfactualAuthorityLevel.IDENTIFIED != CounterfactualAuthorityLevel.NOT_IDENTIFIABLE


def test_identified_outcome_requires_identified_authority() -> None:
    outcome = OracleOutcome.identified(0.25, _SIMPLE)
    assert outcome.is_identified() and outcome.point_estimate == 0.25
    partial = CounterfactualAuthority(
        oracle_authority_level=AuthorityLevel.L2,
        identifiability_status=Identifiability.PARTIALLY_IDENTIFIED,
        support_rule_id="rule",
        environment_model_id="m",
    )
    with pytest.raises(OracleRefused) as excinfo:
        OracleOutcome.identified(0.25, partial)
    assert excinfo.value.refusal is OracleRefusal.INSUFFICIENT_SUPPORT
    with pytest.raises(OracleRefused):
        OracleOutcome.identified(float("nan"), _SIMPLE)


def test_partial_bounds_must_be_ordered_and_finite() -> None:
    partial = CounterfactualAuthority(
        oracle_authority_level=AuthorityLevel.L2,
        identifiability_status=Identifiability.PARTIALLY_IDENTIFIED,
        support_rule_id="rule",
        environment_model_id="m",
    )
    outcome = OracleOutcome.partially_identified(-0.01, 0.02, partial)
    assert outcome.bounds() == (-0.01, 0.02)
    with pytest.raises(OracleRefused):
        OracleOutcome.partially_identified(0.02, -0.01, partial)


def test_unknown_is_first_class_and_carries_a_typed_refusal() -> None:
    unknown_authority = CounterfactualAuthority(
        oracle_authority_level=AuthorityLevel.L1,
        identifiability_status=Identifiability.NOT_IDENTIFIABLE,
        support_rule_id="fill",
        environment_model_id="none",
        assumptions=("no mark price",),
    )
    outcome = OracleOutcome.unknown(OracleRefusal.NON_IDENTIFIABLE_FILL, unknown_authority)
    assert outcome.is_unknown()
    assert outcome.point_estimate is None
    assert outcome.refusal_reason() is OracleRefusal.NON_IDENTIFIABLE_FILL
    assert outcome.as_dict()["kind"] == "UNKNOWN"


def test_rule_twelve_blocks_an_uncertified_edge_claim() -> None:
    AuditState(
        verification=VerificationDimension.CONTRACT_VERIFIED,
        economic_stage=EconomicEvidenceStage.PROMOTABLE_WITHIN_CONTRACT,
        counterfactual_authority=CounterfactualAuthorityLevel.MODEL_DERIVED,
        statistical_verdict=StatisticalVerdict.INCONCLUSIVE_UNDERPOWERED,
    ).validate_rule12()  # promotable is not yet an edge claim
    with pytest.raises(AuthorityError) as excinfo:
        AuditState(
            verification=VerificationDimension.CONTRACT_VERIFIED,
            economic_stage=EconomicEvidenceStage.SHADOW_SUPPORTED,
            counterfactual_authority=CounterfactualAuthorityLevel.MODEL_DERIVED,
            statistical_verdict=StatisticalVerdict.SUPPORTED,
        ).validate_rule12()
    assert excinfo.value.code == "UNCERTIFIED_ECONOMIC_CLAIM"


def test_oracle_context_carries_the_declared_contract_identities() -> None:
    context = OracleContext(
        role=OracleRole.HINDSIGHT, authority=AuthorityLevel.L2,
        information_contract_id="i", opportunity_universe_id="u", utility_contract_id="util",
        policy_class_id="p", cost_model_id="c", capacity_model_id="cap",
        environment_target_id="t",
    )
    assert context.role is OracleRole.HINDSIGHT and context.authority is AuthorityLevel.L2
