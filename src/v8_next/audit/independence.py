"""Adversarial separation and dual-key independence auditor — port of v8-core/src/audit/independence.rs.

IMPLEMENTER != AUDITOR != VERDICT: the worker may not grade itself, the auditor's replay must
match the implementation digest exactly, and the zero-synthetic check must be certified by the
auditor. The Rust verdict hash uses blake3; Python's standard library has no blake3, so the
digest algorithm is declared as sha256 here and the substitution is stated rather than hidden.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

VERDICT_DIGEST_ALGORITHM = "sha256 (blake3 is not in the Python standard library)"


class IndependenceViolation(Exception):
    """Raised when separation of powers is broken (Rule 32)."""


@dataclass(frozen=True)
class DualKeyVerificationResult:
    authorized: bool
    implementer_receipt_id: str
    auditor_receipt_id: str
    verdict_hash: str


class IndependenceAuditor:
    """Port of ``v8_core::audit::IndependenceAuditor``."""

    @staticmethod
    def audit_dual_key(
        worker_agent_id: str,
        auditor_agent_id: str,
        impl_digest: str,
        audit_replay_digest: str,
        zero_synthetic_verified: bool,
    ) -> DualKeyVerificationResult:
        if worker_agent_id == auditor_agent_id:
            raise IndependenceViolation(
                "SELF_GRADING_PROHIBITED: "
                f"Worker '{worker_agent_id}' cannot act as independent auditor"
            )
        if impl_digest != audit_replay_digest:
            raise IndependenceViolation(
                f"REPLAY_DIGEST_MISMATCH: Impl='{impl_digest}', Audit='{audit_replay_digest}'"
            )
        if not zero_synthetic_verified:
            raise IndependenceViolation(
                "SYNTHETIC_DATA_LEAKAGE_DETECTED: Auditor failed zero-synthetic check"
            )
        payload = "|".join(("DualKeyVerdict", worker_agent_id, auditor_agent_id, impl_digest))
        return DualKeyVerificationResult(
            authorized=True,
            implementer_receipt_id=f"impl_{impl_digest}",
            auditor_receipt_id=f"audit_{audit_replay_digest}",
            verdict_hash=hashlib.sha256(payload.encode()).hexdigest(),
        )
