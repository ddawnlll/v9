"""Versioned, lexicographic UtilityContract — port of v8-core/src/oracle/utility.rs.

(TARGET_ORACLE_SPEC §7; spec rule 15: NetUtility is primary, hard constraints lexicographic.)

This type validates the declared feasible set only. It intentionally has no policy ranking
or selection operation: a policy breaching a hard constraint can never outrank a compliant
one on scalar return, and that ordering is enforced by refusing to rank here at all.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from v8_next.oracle.authority import OracleRefused
from v8_next.oracle.taxonomy import OracleRefusal

__all__ = [
    "HardConstraints",
    "ModelIds",
    "ScalarPenalties",
    "UtilityContract",
]

#: The only primary objective this contract version admits.
PRIMARY_OBJECTIVE = "AFTER_COST_NET_UTILITY"


@dataclass(frozen=True)
class ModelIds:
    """Cost-model identity: fee / funding / slippage / impact model ids."""

    fee_model_id: str
    funding_model_id: str
    slippage_model_id: str
    impact_model_id: str

    def is_complete(self) -> bool:
        return bool(
            self.fee_model_id
            and self.funding_model_id
            and self.slippage_model_id
            and self.impact_model_id
        )


@dataclass(frozen=True)
class HardConstraints:
    """Lexicographic hard constraints (spec rule 15)."""

    drawdown_max: float
    tail_risk_max: float
    capacity_max: float
    portfolio_heat_max: float
    coverage_min: float
    operational_rule_id: str

    def is_feasible(self) -> bool:
        return (
            self.drawdown_max >= 0.0
            and self.tail_risk_max >= 0.0
            and self.capacity_max > 0.0
            and self.portfolio_heat_max > 0.0
            and 0.0 <= self.coverage_min <= 1.0
        )

    def ranks_above(self, breached: bool) -> bool:
        """Lexicographic gate: any breach outranks scalar return (I3)."""
        return not breached


@dataclass(frozen=True)
class ScalarPenalties:
    """Optional scalar sub-objective weights with their sensitivity bands."""

    names: tuple[str, ...] = ()
    weights: tuple[float, ...] = ()
    sensitivity_band: tuple[tuple[float, float], ...] = ()

    def is_consistent(self) -> bool:
        return (
            len(self.names) == len(self.weights) == len(self.sensitivity_band)
            and all(math.isfinite(w) for w in self.weights)
            and all(
                math.isfinite(lo) and math.isfinite(hi) and lo <= hi
                for lo, hi in self.sensitivity_band
            )
        )


@dataclass(frozen=True)
class UtilityContract:
    """The versioned utility contract a Target Oracle is conditional on."""

    contract_id: str
    version: str
    primary_objective: str
    horizon: str
    accounting_currency: str
    models: ModelIds
    hard_constraints: HardConstraints
    stress_grid_id: str
    effective_from: int
    optional_scalar_penalties: ScalarPenalties | None = None

    def validate(self) -> None:
        """Validate the declared feasible set; refuse infeasible contracts."""
        penalties_ok = (
            self.optional_scalar_penalties is None
            or self.optional_scalar_penalties.is_consistent()
        )
        complete = (
            bool(self.contract_id)
            and bool(self.version)
            and self.primary_objective == PRIMARY_OBJECTIVE
            and bool(self.horizon)
            and bool(self.accounting_currency)
            and self.models.is_complete()
            and bool(self.hard_constraints.operational_rule_id)
            and bool(self.stress_grid_id)
        )
        if not (complete and self.hard_constraints.is_feasible() and penalties_ok):
            raise OracleRefused(
                OracleRefusal.CONSTRAINT_INFEASIBLE,
                f"utility contract {self.contract_id!r} declares an infeasible set",
            )

    def as_dict(self) -> dict[str, object]:
        return {
            "contract_id": self.contract_id,
            "version": self.version,
            "primary_objective": self.primary_objective,
            "horizon": self.horizon,
            "accounting_currency": self.accounting_currency,
            "models": {
                "fee_model_id": self.models.fee_model_id,
                "funding_model_id": self.models.funding_model_id,
                "slippage_model_id": self.models.slippage_model_id,
                "impact_model_id": self.models.impact_model_id,
            },
            "hard_constraints": {
                "drawdown_max": self.hard_constraints.drawdown_max,
                "tail_risk_max": self.hard_constraints.tail_risk_max,
                "capacity_max": self.hard_constraints.capacity_max,
                "portfolio_heat_max": self.hard_constraints.portfolio_heat_max,
                "coverage_min": self.hard_constraints.coverage_min,
                "operational_rule_id": self.hard_constraints.operational_rule_id,
            },
            "stress_grid_id": self.stress_grid_id,
            "effective_from": self.effective_from,
        }
