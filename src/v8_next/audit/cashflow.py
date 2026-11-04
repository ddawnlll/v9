"""Double-entry cashflow conservation auditor — port of v8-core/src/audit/cashflow.rs.

``Final_Equity == Initial_Equity + Net_Cashflows + Open_Positions_Unrealized``
with the Rust epsilon (1e-8). A violation is an error, never a warning.
"""

from __future__ import annotations

from dataclasses import dataclass

CONSERVATION_EPSILON = 1e-8


@dataclass(frozen=True)
class CashflowConservationReport:
    passed: bool
    initial_equity: float
    final_equity: float
    sum_realized_cashflows: float
    sum_open_position_values: float
    discrepancy: float


class CashflowConservationViolation(ValueError):
    """Raised when the double-entry identity does not close."""


class CashflowAuditor:
    """Port of ``v8_core::audit::CashflowAuditor``."""

    CONSERVATION_EPSILON = CONSERVATION_EPSILON

    @staticmethod
    def audit_conservation(
        initial_equity: float,
        final_equity: float,
        net_cashflows: list[float],
        open_positions_unrealized: float,
    ) -> CashflowConservationReport:
        total = float(sum(net_cashflows))
        expected_final = initial_equity + total + open_positions_unrealized
        discrepancy = abs(final_equity - expected_final)
        report = CashflowConservationReport(
            passed=discrepancy <= CONSERVATION_EPSILON,
            initial_equity=initial_equity,
            final_equity=final_equity,
            sum_realized_cashflows=total,
            sum_open_position_values=open_positions_unrealized,
            discrepancy=discrepancy,
        )
        if not report.passed:
            raise CashflowConservationViolation(
                "CASHFLOW_CONSERVATION_VIOLATION: "
                f"Initial={initial_equity}, Final={final_equity}, SumCF={total}, "
                f"Discrepancy={discrepancy}"
            )
        return report
