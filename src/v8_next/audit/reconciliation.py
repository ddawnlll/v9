"""Reconciliation receipt and clone-collapse auditor — port of v8-core/src/audit/reconciliation.rs.

Faithful in structure: the receipt id must match the state, the receipt payload must hash back
to its own id, the state identity must hash back to itself, and the effective observer count
must equal the sum of the collapse-proof weights (no clone survives uncollapsed).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

CLONE_COLLAPSE_EPSILON = 1e-6


class CollapseProof(Protocol):
    normalized_effective_weight: float


@dataclass(frozen=True)
class ReconciliationReceipt:
    receipt_id: str
    compute_id: str
    effective_observer_count: float
    collapse_proofs: tuple[CollapseProof, ...]


@dataclass(frozen=True)
class ReconciledOpportunityState:
    receipt_id: str
    compute_id: str
    reconciled_id: str


class ReconciliationFailure(ValueError):
    """Raised when a receipt or a clone-collapse proof does not verify (Rules 20/22)."""


class ReconciliationAuditor:
    """Port of ``v8_core::audit::ReconciliationAuditor``."""

    @staticmethod
    def audit_receipt(
        state: ReconciledOpportunityState,
        receipt: ReconciliationReceipt,
    ) -> None:
        if state.receipt_id != receipt.receipt_id:
            raise ReconciliationFailure("RECEIPT_ID_MISMATCH")
        if receipt.compute_id != receipt.receipt_id:
            raise ReconciliationFailure("RECEIPT_PAYLOAD_TAMPERED")
        if state.compute_id != state.reconciled_id:
            raise ReconciliationFailure("STATE_IDENTITY_TAMPERED")
        proof_sum = float(sum(p.normalized_effective_weight for p in receipt.collapse_proofs))
        if abs(receipt.effective_observer_count - proof_sum) > CLONE_COLLAPSE_EPSILON:
            raise ReconciliationFailure(
                "CLONE_COLLAPSE_INTEGRITY_FAILURE: "
                f"EffectiveCount={receipt.effective_observer_count}, ProofSum={proof_sum}"
            )
