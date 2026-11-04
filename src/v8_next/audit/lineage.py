"""Point-in-time and zero-leakage lineage auditor — port of v8-core/src/audit/lineage.rs.

No input may be available later than the decision clock it was used at.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class LineageAuditReport:
    passed: bool
    records_checked: int
    pit_leak_violations: list[str] = field(default_factory=list)


class PointInTimeLeakage(ValueError):
    """Raised when an input's availability exceeds the decision clock (Rule 3)."""


class LineageAuditor:
    """Port of ``v8_core::audit::LineageAuditor``."""

    @staticmethod
    def audit_pit_causality(
        decision_clock: int,
        input_available_times: list[tuple[str, int]],
    ) -> LineageAuditReport:
        violations: list[str] = []
        for name, available in input_available_times:
            if available > decision_clock:
                violations.append(
                    "PIT_FUTURE_LEAKAGE: "
                    f"Input '{name}' available at {available} > decision clock {decision_clock}"
                )
        if violations:
            raise PointInTimeLeakage("; ".join(violations))
        return LineageAuditReport(
            passed=True, records_checked=len(input_available_times), pit_leak_violations=[]
        )
