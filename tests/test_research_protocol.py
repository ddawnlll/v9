"""M4-03: dataset roles, sealed access, reuse, correction, walk-forward, verdicts."""

from decimal import Decimal
from typing import Any

import pytest
from pydantic import ValidationError

from v8_next.evaluation.preregistration import (
    Arm,
    ArmKind,
    CostModel,
    Direction,
    ExperimentResult,
    HypothesisBudget,
    MatchedDimensions,
    Preregistration,
    PreregistrationError,
)
from v8_next.evaluation.protocol import (
    OPEN_ROLES,
    ROLE_ORDER,
    AccessLedger,
    AccessToken,
    CorrectionMethod,
    DatasetRole,
    HypothesisBudgetViolation,
    HypothesisFamily,
    ResearchProtocolError,
    ReuseReport,
    SealedAccessError,
    SealedAuthorization,
    SealedReuseViolation,
    TestingReport,
    Verdict,
    VerdictRefused,
    WalkForwardError,
    assess_verdict,
    authorize_sealed,
    build_result_report,
    multiple_testing,
    purged_walk_forward,
    require_primary_edge,
)

MATCHED = MatchedDimensions(
    calendar="NX01",
    capital=Decimal("10000"),
    venue="BINANCE",
    opportunity_exposure="all_qualified_opportunities",
    exit_policy="bracket_2r",
)


def registration(
    *, role: DatasetRole = DatasetRole.SEALED_OOS, **overrides: Any
) -> Preregistration:
    payload: dict[str, Any] = {
        "hypothesis_id": "H-001",
        "statement": "V9 behavioral gating raises per-opportunity net return",
        "intervention": Arm(
            arm_id="v9",
            kind=ArmKind.V9_INTERVENTION,
            description="behavioral gating",
            matched=MATCHED,
        ),
        "control_arms": (
            Arm(arm_id="v8", kind=ArmKind.V8_BASELINE, description="baseline", matched=MATCHED),
            Arm(
                arm_id="random",
                kind=ArmKind.RANDOM_ENTRY_MATCHED_EXITS,
                description="random entry",
                matched=MATCHED,
            ),
        ),
        "unit_of_comparison": "per_opportunity_net_return",
        "dataset_role": role,
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
        "decision_rule": "PASS on corrected rejection; NO_EDGE otherwise; STOP on DD breach",
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


def family(size: int = 1, name: str = "behavioral") -> HypothesisFamily:
    return HypothesisFamily(
        name=name, hypotheses=tuple(f"H-{index:03d}" for index in range(1, size + 1))
    )


# --- access ledger -----------------------------------------------------------


def test_development_and_validation_are_open() -> None:
    ledger = AccessLedger()
    development = ledger.request_access(
        DatasetRole.DEVELOPMENT, "explore features", 50, slice_id="dev-tape"
    )
    validation = ledger.request_access(
        DatasetRole.VALIDATION, "select policy", 10, slice_id="val-tape"
    )
    assert development.role is DatasetRole.DEVELOPMENT
    assert validation.role is DatasetRole.VALIDATION
    assert development.authorization_id is None
    assert len(ledger) == 2
    assert {DatasetRole.DEVELOPMENT, DatasetRole.VALIDATION} == OPEN_ROLES


def test_sealed_access_without_authorization_is_refused() -> None:
    ledger = AccessLedger()
    with pytest.raises(SealedAccessError) as raised:
        ledger.request_access(DatasetRole.SEALED_OOS, "final evaluation", 1, slice_id="sealed")
    assert raised.value.code == "SEALED_ACCESS_DENIED"
    assert raised.value.field == "authorization"
    assert len(ledger) == 0


def test_sealed_authorization_cannot_be_fabricated() -> None:
    with pytest.raises(ValidationError):
        SealedAuthorization(
            authorization_id="forged",
            granted_by="me",
            purpose="final evaluation",
            reference="self-granted",
            max_slices=1,
            issued_ns=0,
        )

    minted = authorize_sealed(
        granted_by="research-director",
        purpose="final evaluation",
        reference="prereg H-001",
    )
    assert isinstance(minted, SealedAuthorization)
    assert minted.digest() == minted.digest()
    assert minted.authorization_id != "forged"
    assert minted.max_slices == 1


def test_sealed_authorization_purpose_must_match() -> None:
    ledger = AccessLedger()
    authorization = authorize_sealed(
        granted_by="director", purpose="final evaluation", reference="prereg H-001"
    )
    with pytest.raises(SealedAccessError) as raised:
        ledger.request_access(
            DatasetRole.SEALED_OOS,
            "exploratory peek",
            1,
            slice_id="sealed",
            authorization=authorization,
        )
    assert raised.value.field == "purpose"


def test_sealed_slice_reuse_is_refused_under_same_authorization() -> None:
    ledger = AccessLedger()
    authorization = authorize_sealed(
        granted_by="director", purpose="final evaluation", reference="prereg H-001", max_slices=2
    )
    ledger.request_access(
        DatasetRole.SEALED_OOS,
        "final evaluation",
        1,
        slice_id="sealed",
        authorization=authorization,
    )
    with pytest.raises(SealedReuseViolation) as raised:
        ledger.request_access(
            DatasetRole.SEALED_OOS,
            "final evaluation",
            1,
            slice_id="sealed",
            authorization=authorization,
        )
    assert raised.value.code == "SEALED_SLICE_REUSE"
    assert raised.value.slice_id == "sealed"
    assert len(ledger) == 1


def test_one_authorization_cannot_open_two_slices() -> None:
    ledger = AccessLedger()
    authorization = authorize_sealed(
        granted_by="director", purpose="final evaluation", reference="prereg H-001", max_slices=2
    )
    ledger.request_access(
        DatasetRole.SEALED_OOS,
        "final evaluation",
        1,
        slice_id="sealed",
        authorization=authorization,
    )
    with pytest.raises(SealedReuseViolation):
        ledger.request_access(
            DatasetRole.SEALED_OOS,
            "final evaluation",
            1,
            slice_id="sealed-v2",
            authorization=authorization,
        )
    assert ledger.sealed_uses(authorization) == 1


def test_reauthorized_reread_is_recorded_and_permitted() -> None:
    ledger = AccessLedger()
    purpose = "final evaluation"
    first = authorize_sealed(granted_by="director", purpose=purpose, reference="prereg H-001")
    ledger.request_access(
        DatasetRole.SEALED_OOS, purpose, 1, slice_id="sealed", authorization=first
    )
    second = authorize_sealed(
        granted_by="director",
        purpose=purpose,
        reference="prereg H-001 revision 2 (registered before read)",
    )
    assert second.authorization_id != first.authorization_id
    ledger.request_access(
        DatasetRole.SEALED_OOS, purpose, 1, slice_id="sealed", authorization=second
    )

    (report,) = ledger.reuse_detection()
    assert isinstance(report, ReuseReport)
    assert report.reused is True
    assert report.permitted is True
    assert report.accesses == 2
    assert report.authorization_ids == (first.authorization_id, second.authorization_id)


def test_persisted_ledger_reuse_is_detected_on_rehydration() -> None:
    ledger = AccessLedger()
    authorization = authorize_sealed(
        granted_by="director", purpose="final evaluation", reference="prereg H-001"
    )
    first = ledger.request_access(
        DatasetRole.SEALED_OOS,
        "final evaluation",
        1,
        slice_id="sealed",
        authorization=authorization,
    )
    duplicated = AccessToken(
        token_id="forged-second-read",
        seq=2,
        role=first.role,
        purpose=first.purpose,
        budget=1,
        slice_id=first.slice_id,
        issued_ns=0,
        authorization_id=first.authorization_id,
    )
    with pytest.raises(SealedReuseViolation):
        AccessLedger.from_entries([first, duplicated])


def test_report_states_consumed_roles_and_lineage() -> None:
    ledger = AccessLedger()
    ledger.request_access(DatasetRole.DEVELOPMENT, "explore", 50, slice_id="dev-tape")
    ledger.request_access(DatasetRole.VALIDATION, "select", 10, slice_id="val-tape")
    authorization = authorize_sealed(
        granted_by="director", purpose="final evaluation", reference="prereg H-001"
    )
    ledger.request_access(
        DatasetRole.SEALED_OOS,
        "final evaluation",
        1,
        slice_id="sealed",
        authorization=authorization,
    )

    report = ledger.report()
    assert report.consumed_roles == ROLE_ORDER
    assert report.entries == 3
    assert report.sealed_slices == ("sealed",)
    assert tuple(summary.role for summary in report.by_role) == ROLE_ORDER
    assert report.for_role(DatasetRole.DEVELOPMENT).accesses == 1
    assert report.for_role(DatasetRole.SEALED_OOS).budget_total == 1
    assert report.lineage_digest == ledger.report().lineage_digest


def test_report_zero_role_is_distinguished_from_unused() -> None:
    ledger = AccessLedger()
    ledger.request_access(DatasetRole.DEVELOPMENT, "explore", 5)
    report = ledger.report()
    assert report.consumed_roles == (DatasetRole.DEVELOPMENT,)
    assert report.for_role(DatasetRole.VALIDATION).accesses == 0
    assert report.for_role(DatasetRole.SEALED_OOS).accesses == 0


# --- purged walk-forward -----------------------------------------------------


@pytest.mark.parametrize(
    "total_bars, folds, purge, embargo",
    [
        (100, 5, 3, 2),
        (240, 4, 12, 6),
        (50, 3, 0, 0),
        (61, 5, 2, 7),
    ],
)
def test_purged_walk_forward_never_leaks(
    total_bars: int, folds: int, purge: int, embargo: int
) -> None:
    plan = purged_walk_forward(
        total_bars=total_bars, folds=folds, purge_bars=purge, embargo_bars=embargo
    )
    assert len(plan) == folds
    for fold in plan:
        assert fold.purge_bars == purge
        assert fold.embargo_bars == embargo
        for index in fold.train_indices():
            assert index < fold.test.start
            for other in plan:
                assert not (other.test.start - purge <= index < other.test.start)
                assert not (other.test.end <= index < other.test.end + embargo)
        assert (
            fold.train_bars
            == sum(len(segment) for segment in fold.train)
            == len(fold.train_indices())
        )


def test_purged_walk_forward_is_deterministic() -> None:
    first = purged_walk_forward(total_bars=120, folds=6, purge_bars=4, embargo_bars=3)
    second = purged_walk_forward(total_bars=120, folds=6, purge_bars=4, embargo_bars=3)
    assert first == second
    assert [fold.fold_id for fold in first] == [f"fold_{i:02d}" for i in range(6)]


def test_purge_and_embargo_actually_remove_bars() -> None:
    loose = purged_walk_forward(total_bars=120, folds=4, purge_bars=0, embargo_bars=0)
    tight = purged_walk_forward(total_bars=120, folds=4, purge_bars=5, embargo_bars=3)
    assert [fold.train_bars for fold in tight] != [fold.train_bars for fold in loose]
    assert tight[-1].train_bars <= loose[-1].train_bars


@pytest.mark.parametrize(
    "kwargs",
    [
        {"total_bars": 1, "folds": 1},
        {"total_bars": 10, "folds": 0},
        {"total_bars": 10, "folds": 20},
        {"total_bars": 10, "folds": 2, "purge_bars": -1},
    ],
)
def test_walk_forward_refuses_impossible_plan(kwargs: dict[str, int]) -> None:
    arguments: dict[str, int] = {"purge_bars": 0, "embargo_bars": 0, **kwargs}
    with pytest.raises(WalkForwardError) as raised:
        purged_walk_forward(**arguments)
    assert raised.value.code == "WALK_FORWARD_INVALID"


def test_walk_forward_refuses_when_purge_consumes_history() -> None:
    with pytest.raises(WalkForwardError) as raised:
        purged_walk_forward(total_bars=30, folds=5, purge_bars=10, embargo_bars=5)
    assert raised.value.field == "min_train_bars"


# --- multiple testing --------------------------------------------------------


def test_family_correction_reduces_significance() -> None:
    declared = family(5)
    raw = {name: 0.03 for name in declared.hypotheses}
    bonferroni = multiple_testing(raw, declared, alpha=0.05, method=CorrectionMethod.BONFERRONI)
    holm = multiple_testing(raw, declared, alpha=0.05, method=CorrectionMethod.HOLM)
    single = multiple_testing(
        {"H-001": 0.03}, family(1), alpha=0.05, method=CorrectionMethod.BONFERRONI
    )

    assert bonferroni.adjusted["H-001"] == pytest.approx(0.15)
    assert not bonferroni.any_rejected()
    assert not holm.any_rejected()
    assert single.any_rejected()
    assert bonferroni.degrees_of_freedom == 5
    assert bonferroni.family_size == 5


def test_holm_adjustment_is_monotone_and_never_below_raw() -> None:
    declared = family(4)
    raw = {"H-001": 0.01, "H-002": 0.04, "H-003": 0.03, "H-004": 0.20}
    report = multiple_testing(raw, declared, alpha=0.05, method=CorrectionMethod.HOLM)
    assert report.adjusted["H-001"] == pytest.approx(0.04)
    assert report.adjusted["H-003"] == pytest.approx(0.09)
    assert report.adjusted["H-004"] == pytest.approx(0.20)
    for name, value in raw.items():
        assert report.adjusted[name] >= value


def test_uncorrected_claim_is_invalid() -> None:
    declared = family(3)
    report = multiple_testing({name: 0.001 for name in declared.hypotheses}, declared, method=None)
    assert report.corrected is False
    assert report.method is None
    assert report.verdict_for("H-001", preregistered=True) is Verdict.INVALID
    assert report.verdict_for("H-001", preregistered=False) is Verdict.INVALID


def test_corrected_claim_outside_the_preregistration_is_invalid() -> None:
    report = multiple_testing({"H-001": 0.001}, family(1), method=CorrectionMethod.HOLM)
    assert report.verdict_for("H-001", preregistered=False) is Verdict.INVALID
    assert report.verdict_for("H-001", preregistered=True) is Verdict.PASS


def test_testing_a_set_that_is_not_the_declared_family_is_refused() -> None:
    with pytest.raises(HypothesisBudgetViolation) as raised:
        multiple_testing({"H-001": 0.01, "H-002": 0.02}, family(1))
    assert raised.value.code == "HYPOTHESIS_BUDGET_VIOLATION"


def test_correction_matches_the_repository_multitest_helper_when_available() -> None:
    """Cross-check against ``evaluation.multitest`` (statsmodels-backed) when installed."""
    pytest.importorskip("statsmodels")
    from v8_next.evaluation.multitest import multiple_testing_correction

    declared = family(5)
    raw = {"H-001": 0.004, "H-002": 0.011, "H-003": 0.019, "H-004": 0.03, "H-005": 0.6}
    reference = multiple_testing_correction(raw, alpha=0.05, methods=("bonferroni", "holm"))
    ours_holm = multiple_testing(raw, declared, alpha=0.05, method=CorrectionMethod.HOLM)
    ours_bonf = multiple_testing(raw, declared, alpha=0.05, method=CorrectionMethod.BONFERRONI)

    for name in declared.hypotheses:
        assert ours_holm.adjusted[name] == pytest.approx(
            reference["adjustments"]["holm"]["adjusted_pvalues"][name], abs=1e-9
        )
        assert ours_bonf.adjusted[name] == pytest.approx(
            reference["adjustments"]["bonferroni"]["adjusted_pvalues"][name], abs=1e-9
        )
        assert ours_holm.rejected[name] == reference["adjustments"]["holm"]["rejected"][name]


def test_unknown_hypothesis_cannot_be_scored() -> None:
    report = multiple_testing({"H-001": 0.01}, family(1))
    with pytest.raises(ResearchProtocolError) as raised:
        report.verdict_for("H-999", preregistered=True)
    assert raised.value.field == "hypothesis"


# --- verdict gate ------------------------------------------------------------


def sealed_token(ledger: AccessLedger, purpose: str = "final evaluation") -> AccessToken:
    authorization = authorize_sealed(
        granted_by="director", purpose=purpose, reference=f"prereg {purpose}"
    )
    return ledger.request_access(
        DatasetRole.SEALED_OOS, purpose, 1, slice_id="sealed", authorization=authorization
    )


def test_no_edge_and_stop_are_accepted_first_class_outcomes() -> None:
    ledger = AccessLedger()
    token = sealed_token(ledger)
    registration_ = registration()
    for outcome in (Verdict.NO_EDGE, Verdict.STOP):
        result = assess_verdict(
            preregistration=registration_, ledger=ledger, token=token, claimed=outcome
        )
        assert result.verdict is outcome
        assert result.code == "ACCEPTED_NEGATIVE_OUTCOME"
        assert result.primary_edge_eligible is False
        assert result.preregistration_digest == registration_.digest()


def test_inconclusive_is_accepted_but_not_a_negative_outcome() -> None:
    ledger = AccessLedger()
    token = sealed_token(ledger)
    result = assess_verdict(
        preregistration=registration(),
        ledger=ledger,
        token=token,
        claimed=Verdict.INCONCLUSIVE,
    )
    assert result.verdict is Verdict.INCONCLUSIVE
    assert result.code == "ACCEPTED_OUTCOME"


def test_pass_without_correction_is_invalid() -> None:
    ledger = AccessLedger()
    token = sealed_token(ledger)
    result = assess_verdict(
        preregistration=registration(),
        ledger=ledger,
        token=token,
        claimed=Verdict.PASS,
        testing=None,
    )
    assert result.verdict is Verdict.INVALID
    assert result.code == "UNCORRECTED_CLAIM"
    assert result.primary_edge_eligible is False

    uncorrected = multiple_testing({"H-001": 0.001}, family(1), method=None)
    result = assess_verdict(
        preregistration=registration(),
        ledger=ledger,
        token=token,
        claimed=Verdict.PASS,
        testing=uncorrected,
    )
    assert result.verdict is Verdict.INVALID
    assert result.code == "UNCORRECTED_CLAIM"


def test_pass_is_refused_without_a_registered_preregistration() -> None:
    ledger = AccessLedger()
    token = sealed_token(ledger)
    incomplete = registration(control_arms=(), costs=None, accepted_outcomes=())
    testing = multiple_testing({"H-001": 0.001}, family(1), method=CorrectionMethod.HOLM)
    with pytest.raises(PreregistrationError):
        assess_verdict(
            preregistration=incomplete,
            ledger=ledger,
            token=token,
            claimed=Verdict.PASS,
            testing=testing,
        )


def test_pass_is_refused_without_a_permitted_role_token() -> None:
    ledger = AccessLedger()
    seal = sealed_token(ledger)
    development = ledger.request_access(DatasetRole.DEVELOPMENT, "explore", 5)
    testing = multiple_testing({"H-001": 0.001}, family(1), method=CorrectionMethod.HOLM)

    with pytest.raises(VerdictRefused) as raised:
        assess_verdict(
            preregistration=registration(role=DatasetRole.DEVELOPMENT),
            ledger=ledger,
            token=seal,
            claimed=Verdict.PASS,
            testing=testing,
        )
    assert raised.value.code == "DATASET_ROLE_MISMATCH"

    forged = development.model_copy(update={"token_id": "0" * 64})
    with pytest.raises(VerdictRefused) as raised:
        assess_verdict(
            preregistration=registration(role=DatasetRole.DEVELOPMENT),
            ledger=ledger,
            token=forged,
            claimed=Verdict.PASS,
            testing=testing,
        )
    assert raised.value.code == "UNREGISTERED_ACCESS_TOKEN"


def test_pass_with_corrected_family_and_sealed_role_is_eligible_for_primary_edge() -> None:
    ledger = AccessLedger()
    token = sealed_token(ledger)
    registration_ = registration()
    testing = multiple_testing({"H-001": 0.001}, family(1), method=CorrectionMethod.HOLM)
    result = assess_verdict(
        preregistration=registration_,
        ledger=ledger,
        token=token,
        claimed=Verdict.PASS,
        testing=testing,
    )
    assert result.verdict is Verdict.PASS
    assert result.code == "PASS_WITH_REGISTERED_PREREGISTRATION"
    assert result.primary_edge_eligible is True
    assert require_primary_edge(result) is result


def test_development_pass_cannot_claim_primary_oos_edge() -> None:
    ledger = AccessLedger()
    token = ledger.request_access(DatasetRole.DEVELOPMENT, "explore", 5)
    registration_ = registration(role=DatasetRole.DEVELOPMENT)
    testing = multiple_testing({"H-001": 0.001}, family(1), method=CorrectionMethod.HOLM)
    result = assess_verdict(
        preregistration=registration_,
        ledger=ledger,
        token=token,
        claimed=Verdict.PASS,
        testing=testing,
    )
    assert result.verdict is Verdict.PASS
    assert result.primary_edge_eligible is False
    with pytest.raises(VerdictRefused) as raised:
        require_primary_edge(result)
    assert raised.value.code == "NOT_PRIMARY_OOS_EVIDENCE"


def test_correction_must_match_the_registered_method_and_budget() -> None:
    ledger = AccessLedger()
    token = sealed_token(ledger)
    registration_ = registration()
    mismatched = multiple_testing({"H-001": 0.001}, family(1), method=CorrectionMethod.BONFERRONI)
    with pytest.raises(VerdictRefused) as raised:
        assess_verdict(
            preregistration=registration_,
            ledger=ledger,
            token=token,
            claimed=Verdict.PASS,
            testing=mismatched,
        )
    assert raised.value.code == "CORRECTION_MISMATCH"

    oversized = multiple_testing(
        {name: 0.001 for name in family(6).hypotheses},
        family(6),
        method=CorrectionMethod.HOLM,
    )
    with pytest.raises(VerdictRefused) as raised:
        assess_verdict(
            preregistration=registration_,
            ledger=ledger,
            token=token,
            claimed=Verdict.PASS,
            testing=oversized,
        )
    assert raised.value.code == "HYPOTHESIS_BUDGET_EXCEEDED"


def test_pass_claim_with_no_rejected_hypothesis_is_invalid() -> None:
    ledger = AccessLedger()
    token = sealed_token(ledger)
    testing = multiple_testing({"H-001": 0.4}, family(1), method=CorrectionMethod.HOLM)
    result = assess_verdict(
        preregistration=registration(),
        ledger=ledger,
        token=token,
        claimed=Verdict.PASS,
        testing=testing,
    )
    assert result.verdict is Verdict.INVALID
    assert result.code == "CLAIM_NOT_SUPPORTED"
    assert result.primary_edge_eligible is False


def test_registered_hypothesis_outside_the_tested_family_is_invalid() -> None:
    ledger = AccessLedger()
    token = sealed_token(ledger)
    other = HypothesisFamily(name="behavioral", hypotheses=("H-999",))
    testing = multiple_testing({"H-999": 0.001}, other, method=CorrectionMethod.HOLM)
    result = assess_verdict(
        preregistration=registration(),
        ledger=ledger,
        token=token,
        claimed=Verdict.PASS,
        testing=testing,
    )
    assert result.verdict is Verdict.INVALID
    assert result.code == "HYPOTHESIS_NOT_TESTED"


def test_family_name_must_match_the_registered_budget() -> None:
    ledger = AccessLedger()
    token = sealed_token(ledger)
    testing = multiple_testing(
        {"H-001": 0.001},
        HypothesisFamily(name="other-family", hypotheses=("H-001",)),
        method=CorrectionMethod.HOLM,
    )
    with pytest.raises(VerdictRefused) as raised:
        assess_verdict(
            preregistration=registration(),
            ledger=ledger,
            token=token,
            claimed=Verdict.PASS,
            testing=testing,
        )
    assert raised.value.code == "FAMILY_MISMATCH"


def test_lineage_digest_changes_when_a_read_is_recorded() -> None:
    ledger = AccessLedger()
    before = ledger.report().lineage_digest
    ledger.request_access(DatasetRole.DEVELOPMENT, "explore", 5, slice_id="dev-tape")
    after = ledger.report().lineage_digest
    assert before != after
    assert after == ledger.report().lineage_digest


def test_result_report_separates_development_validation_and_sealed() -> None:
    ledger = AccessLedger()
    ledger.request_access(DatasetRole.DEVELOPMENT, "explore", 50, slice_id="dev-tape")
    ledger.request_access(DatasetRole.VALIDATION, "select", 10, slice_id="val-tape")
    token = sealed_token(ledger)
    registration_ = registration()
    testing = multiple_testing({"H-001": 0.001}, family(1), method=CorrectionMethod.HOLM)
    result = assess_verdict(
        preregistration=registration_,
        ledger=ledger,
        token=token,
        claimed=Verdict.PASS,
        testing=testing,
    )
    report = build_result_report(
        preregistration=registration_, ledger=ledger, result=result, testing=testing
    )

    assert report.dataset_role is DatasetRole.SEALED_OOS
    assert report.consumed_roles == ROLE_ORDER
    assert tuple(summary.role for summary in report.by_role) == ROLE_ORDER
    assert report.for_role(DatasetRole.SEALED_OOS).accesses == 1
    assert report.verdict is Verdict.PASS
    assert report.primary_edge_eligible is True
    assert report.degrees_of_freedom == 1
    assert report.lineage_digest == ledger.report().lineage_digest
    assert report.preregistration_digest == registration_.digest()


def test_testing_report_is_frozen_and_json_safe() -> None:
    report = multiple_testing({"H-001": 0.01}, family(1))
    assert isinstance(report, TestingReport)
    with pytest.raises(ValidationError):
        report.alpha = 0.5  # type: ignore[misc]
    assert report.model_dump(mode="json")["family_size"] == 1


def test_verdict_result_is_frozen() -> None:
    ledger = AccessLedger()
    token = sealed_token(ledger)
    result = assess_verdict(
        preregistration=registration(),
        ledger=ledger,
        token=token,
        claimed=Verdict.NO_EDGE,
    )
    with pytest.raises(ValidationError):
        result.verdict = Verdict.PASS  # type: ignore[misc]


def test_foreign_result_digest_is_refused_by_comparison() -> None:
    """A result carrying a mismatched digest is not evidence for this registration."""
    registration_ = registration()
    foreign = ExperimentResult(
        experiment_id="exp-x",
        hypothesis_id="H-001",
        preregistration_digest="f" * 64,
        gates=registration_.gates(),
    )
    with pytest.raises(PreregistrationError) as raised:
        registration_.compare_against_result(foreign)
    assert "preregistration_digest" in raised.value.fields
