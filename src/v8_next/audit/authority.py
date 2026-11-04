"""Authority audit and receipt-chain validation — port of v8-core/src/audit/authority.rs.

The Rust auditor compares an output ``Authority {evidence, decision, realization}`` against
the minimum of its inputs. In this codebase the authority ladder is the statutory claim
class (``evaluation.claims.StatutoryClaimClass``), so:

* rungs up to ``SIMULATED_CASHFLOW`` are the **evidence** segment,
* ``REALIZED_CASHFLOW`` is the **realization** segment,
* rungs above it (``READY_NOT_CLAIMED``, ``SUPPORTED_EDGE``) are the **decision** segment.

An output may never sit higher on that ladder than the lowest input that produced it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from v8_next.evaluation.claims import StatutoryClaimClass, StatutoryClaimRecord

#: the ladder, weakest first. Index = authority level.
AUTHORITY_LADDER: tuple[StatutoryClaimClass, ...] = (
    StatutoryClaimClass.DiagnosticSignal,
    StatutoryClaimClass.CounterfactualPotential,
    StatutoryClaimClass.RecoverableRegret,
    StatutoryClaimClass.SimulatedCashflow,
    StatutoryClaimClass.RealizedCashflow,
    StatutoryClaimClass.ReadyNotClaimed,
    StatutoryClaimClass.SupportedEdge,
)

_EVIDENCE_TOP = AUTHORITY_LADDER.index(StatutoryClaimClass.SimulatedCashflow)
_REALIZATION = AUTHORITY_LADDER.index(StatutoryClaimClass.RealizedCashflow)

#: a claim at or above this rung needs a real cryptographic receipt, not a placeholder
RECEIPT_REQUIRED_FROM = AUTHORITY_LADDER.index(StatutoryClaimClass.RealizedCashflow)
MIN_RECEIPT_LENGTH = 16


class AuthoritySegment(StrEnum):
    EVIDENCE = "EVIDENCE"
    REALIZATION = "REALIZATION"
    DECISION = "DECISION"


def authority_level(claim_class: StatutoryClaimClass) -> int:
    return AUTHORITY_LADDER.index(claim_class)


def authority_segment(claim_class: StatutoryClaimClass) -> AuthoritySegment:
    level = authority_level(claim_class)
    if level <= _EVIDENCE_TOP:
        return AuthoritySegment.EVIDENCE
    if level <= _REALIZATION:
        return AuthoritySegment.REALIZATION
    return AuthoritySegment.DECISION


class ConstitutionalViolation(Exception):
    """Raised when an output claims more authority than its inputs license (Rule 28)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


@dataclass(frozen=True)
class AuthorityAuditReport:
    passed: bool
    receipts_checked: int
    violations: list[str] = field(default_factory=list)


class AuthorityAuditor:
    """Port of ``v8_core::audit::AuthorityAuditor``."""

    @staticmethod
    def audit_monotonicity(
        output_claim: StatutoryClaimRecord,
        input_claims: list[StatutoryClaimRecord],
    ) -> None:
        """An output may not exceed the minimum authority of the inputs that produced it."""
        if not input_claims:
            return
        min_claim = min(input_claims, key=lambda c: authority_level(c.claim_class))
        output_level = authority_level(output_claim.claim_class)
        min_level = authority_level(min_claim.claim_class)
        if output_level <= min_level:
            return
        segment = authority_segment(output_claim.claim_class)
        code = {
            AuthoritySegment.EVIDENCE: "AUTHORITY_ESCALATION_ATTEMPTED",
            AuthoritySegment.REALIZATION: "REALIZATION_STATUS_ESCALATION",
            AuthoritySegment.DECISION: "DECISION_AUTHORITY_ESCALATION",
        }[segment]
        raise ConstitutionalViolation(
            code,
            f"output {output_claim.claim_class.value} (level {output_level}) exceeds the "
            f"lowest input {min_claim.claim_class.value} (level {min_level})",
        )

    @staticmethod
    def audit_claims(claims: list[StatutoryClaimRecord]) -> AuthorityAuditReport:
        """Every claim must carry its provenance; a strong claim needs a real receipt."""
        violations: list[str] = []
        for index, claim in enumerate(claims):
            if not claim.parent_receipt_hashes:
                violations.append(f"Claim at index {index} has no parent receipt")
            if (
                authority_level(claim.claim_class) >= RECEIPT_REQUIRED_FROM
                and len(claim.signature) < MIN_RECEIPT_LENGTH
            ):
                violations.append(f"Claim at index {index} has invalid cryptographic receipt")
        return AuthorityAuditReport(
            passed=not violations, receipts_checked=len(claims), violations=violations
        )
