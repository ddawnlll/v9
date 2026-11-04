"""Ports of the v8-core audit kernel and system proving ground (D-132, D-147/D-149).

These tests assert the *port's* behaviour matches the rules the Rust side states, including the
adversarial direction: the auditors must reject what they are meant to reject. The failure
domain names, the conservation invariant and the receipt digest scheme are the Rust ones.
"""

from __future__ import annotations

import pytest

from v8_next.audit import (
    AuthorityAuditor,
    CashflowAuditor,
    ConstitutionalViolation,
    IndependenceAuditor,
    LineageAuditor,
    ReconciliationAuditor,
)
from v8_next.audit.cashflow import CashflowConservationViolation
from v8_next.audit.independence import IndependenceViolation
from v8_next.audit.lineage import PointInTimeLeakage
from v8_next.audit.reconciliation import (
    ReconciledOpportunityState,
    ReconciliationFailure,
    ReconciliationReceipt,
)
from v8_next.evaluation.claims import StatutoryClaimClass, StatutoryClaimRecord
from v8_next.system_proving import (
    FailureAttributionBreakdown,
    FailureDomain,
    SystemProvingGroundReceipt,
    SystemRobustnessVector,
)
from v8_next.system_proving.attribution import classify_exit_failure
from v8_next.system_proving.metrics import metrics_from_campaigns


def _claim(claim_class: StatutoryClaimClass, *, parents: tuple[str, ...] = ("r" * 64,), sig: str = "s" * 64) -> StatutoryClaimRecord:
    return StatutoryClaimRecord(
        claim_id="c" * 64,
        claim_class=claim_class,
        numeric_value=1.0,
        units="return",
        parent_receipt_hashes=parents,
        timestamp_utc=0,
        signature=sig,
        allowed_rendering_header=claim_class.canonical_header(),
    )


# --- authority -----------------------------------------------------------------------


def test_output_may_not_exceed_the_lowest_input_authority() -> None:
    inputs = [_claim(StatutoryClaimClass.SimulatedCashflow)]
    AuthorityAuditor.audit_monotonicity(_claim(StatutoryClaimClass.SimulatedCashflow), inputs)
    with pytest.raises(ConstitutionalViolation) as excinfo:
        AuthorityAuditor.audit_monotonicity(_claim(StatutoryClaimClass.SupportedEdge), inputs)
    assert excinfo.value.code == "DECISION_AUTHORITY_ESCALATION"
    with pytest.raises(ConstitutionalViolation) as real:
        AuthorityAuditor.audit_monotonicity(_claim(StatutoryClaimClass.RealizedCashflow), inputs)
    assert real.value.code == "REALIZATION_STATUS_ESCALATION"


def test_claim_without_a_receipt_is_a_violation() -> None:
    report = AuthorityAuditor.audit_claims([_claim(StatutoryClaimClass.DiagnosticSignal, parents=())])
    assert report.passed is False
    assert report.violations
    clean = AuthorityAuditor.audit_claims([_claim(StatutoryClaimClass.DiagnosticSignal)])
    assert clean.passed is True and clean.receipts_checked == 1


# --- cashflow ------------------------------------------------------------------------


def test_double_entry_conservation_closes_and_fails_loudly() -> None:
    report = CashflowAuditor.audit_conservation(
        initial_equity=10_000.0, final_equity=10_050.0, net_cashflows=[30.0, 20.0],
        open_positions_unrealized=0.0,
    )
    assert report.passed and report.discrepancy == 0.0
    with pytest.raises(CashflowConservationViolation):
        CashflowAuditor.audit_conservation(
            initial_equity=10_000.0, final_equity=10_050.0, net_cashflows=[30.0],
            open_positions_unrealized=0.0,
        )


# --- lineage -------------------------------------------------------------------------


def test_pit_causality_rejects_a_future_input() -> None:
    LineageAuditor.audit_pit_causality(1_000, [("bar", 999)])
    with pytest.raises(PointInTimeLeakage) as excinfo:
        LineageAuditor.audit_pit_causality(1_000, [("bar", 1_001)])
    assert "PIT_FUTURE_LEAKAGE" in str(excinfo.value)


# --- independence --------------------------------------------------------------------


def test_dual_key_separation_of_powers() -> None:
    result = IndependenceAuditor.audit_dual_key("w", "a", "d" * 8, "d" * 8, True)
    assert result.authorized and result.verdict_hash
    with pytest.raises(IndependenceViolation):
        IndependenceAuditor.audit_dual_key("same", "same", "d" * 8, "d" * 8, True)
    with pytest.raises(IndependenceViolation):
        IndependenceAuditor.audit_dual_key("w", "a", "d" * 8, "e" * 8, True)
    with pytest.raises(IndependenceViolation):
        IndependenceAuditor.audit_dual_key("w", "a", "d" * 8, "d" * 8, False)


# --- reconciliation ------------------------------------------------------------------


class _Proof:
    def __init__(self, weight: float) -> None:
        self.normalized_effective_weight = weight


def test_clone_collapse_integrity_must_match_the_proof_sum() -> None:
    state = ReconciledOpportunityState(receipt_id="r1", compute_id="r1", reconciled_id="r1")
    receipt = ReconciliationReceipt(
        receipt_id="r1", compute_id="r1", effective_observer_count=1.0,
        collapse_proofs=(_Proof(0.6), _Proof(0.4)),
    )
    ReconciliationAuditor.audit_receipt(state, receipt)
    tampered = ReconciliationReceipt(
        receipt_id="r1", compute_id="r2", effective_observer_count=1.0, collapse_proofs=(_Proof(1.0),)
    )
    with pytest.raises(ReconciliationFailure):
        ReconciliationAuditor.audit_receipt(state, tampered)
    uncollapsed = ReconciliationReceipt(
        receipt_id="r1", compute_id="r1", effective_observer_count=2.0, collapse_proofs=(_Proof(0.6),)
    )
    with pytest.raises(ReconciliationFailure) as excinfo:
        ReconciliationAuditor.audit_receipt(state, uncollapsed)
    assert "CLONE_COLLAPSE_INTEGRITY_FAILURE" in str(excinfo.value)


# --- system proving ground -----------------------------------------------------------


def test_seven_disjoint_domains_and_conservation() -> None:
    assert [domain.value for domain in FailureDomain] == [
        "DETECTION", "REPRESENTATION", "RECONCILIATION", "SELECTION", "ALLOCATION",
        "EXECUTION", "EXIT",
    ]
    breakdown = FailureAttributionBreakdown()
    for domain in (FailureDomain.EXIT, FailureDomain.EXIT, FailureDomain.SELECTION):
        breakdown.record_failure(domain)
    assert breakdown.total_failures == 3
    assert breakdown.verify_conservation() is True
    assert breakdown.as_dict()["counts_by_domain"] == {"EXIT": 2, "SELECTION": 1}


def test_classifier_charges_each_loss_to_exactly_one_domain() -> None:
    assert classify_exit_failure(exit_kind="STOP", net_return=-0.01, has_bracket=False) is FailureDomain.EXIT
    assert classify_exit_failure(exit_kind="EXPIRY", net_return=-0.01, has_bracket=True) is FailureDomain.SELECTION
    assert classify_exit_failure(exit_kind="STOP", net_return=-0.01, has_bracket=True) is FailureDomain.EXECUTION
    with pytest.raises(ValueError):
        classify_exit_failure(exit_kind="STOP", net_return=0.01, has_bracket=True)


def test_receipt_digest_and_double_entry_invariant() -> None:
    metrics = metrics_from_campaigns(
        campaigns=100, failures=94, gross_return_sum=0.66, fee_cost_sum=1.31,
        funding_cost_sum=0.02, max_drawdown_pct=12.0, bars=35_064,
        # #487: a reconciliation verdict needs a MEASURED residual — the producer no longer
        # defaults one to 0.0, so the caller states the residual it actually reconciled.
        cashflow_discrepancy_usdt=0.0,
    )
    assert metrics.is_double_entry_reconciled() is True
    assert metrics.funding_drag_ratio > 0
    assert metrics.regime_stability_score == pytest.approx(0.06)
    receipt = SystemProvingGroundReceipt.new(
        world_id="multi-1h-4y", policy_id="plain_swing", total_trades=100, total_campaigns=100,
        metrics=metrics, attribution=FailureAttributionBreakdown(), exercises_full_pipeline=True,
        timestamp_ns=1_700_000_000,
    )
    assert receipt.receipt_id.startswith("spg-receipt-")
    assert len(receipt.receipt_digest) == 64
    assert receipt.as_dict()["metrics"]["scenario_failure_fraction"] == pytest.approx(0.94)
    unreconciled = SystemRobustnessVector(**{**metrics.as_dict(), "cashflow_discrepancy_usdt": 0.5})
    assert unreconciled.is_double_entry_reconciled() is False

pytestmark = pytest.mark.slow  # #469: tape/engine file, fast loop excludes via -m "not slow"
