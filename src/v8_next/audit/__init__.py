"""Central Constitutional Audit Kernel — Python port of v8-core/src/audit (D-132, Rules 28-35).

Ported unit by unit so the two systems speak one language; each module names its Rust
counterpart in its docstring. The auditing rules are unchanged:

1. Auditing is inline and non-optional across stages.
2. Separation of powers: IMPLEMENTER != AUDITOR != VERDICT AUTHORITY.
3. The audit-of-audit sabotage suite (v8-core/src/audit/sabotage.rs) attacks the auditors.

Authority levels are the existing Python claim ladder (``evaluation.claims.StatutoryClaimClass``),
which plays the role of v8-core's ``authority::Authority`` triple.
"""

from v8_next.audit.authority import (
    AuthorityAuditor,
    AuthorityAuditReport,
    ConstitutionalViolation,
)
from v8_next.audit.cashflow import CashflowAuditor, CashflowConservationReport
from v8_next.audit.independence import DualKeyVerificationResult, IndependenceAuditor
from v8_next.audit.lineage import LineageAuditor, LineageAuditReport
from v8_next.audit.reconciliation import ReconciliationAuditor

__all__ = [
    "AuthorityAuditReport",
    "AuthorityAuditor",
    "CashflowAuditor",
    "CashflowConservationReport",
    "ConstitutionalViolation",
    "DualKeyVerificationResult",
    "IndependenceAuditor",
    "LineageAuditReport",
    "LineageAuditor",
    "ReconciliationAuditor",
]
