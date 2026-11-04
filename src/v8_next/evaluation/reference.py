"""Declared DSR reference artifact; schema validity is not source certification."""

from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator

from v8_next.evaluation.alignment import IntervalLoss
from v8_next.evaluation.deflated_sharpe import DSRPlan


class ReferenceInterval(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)
    start_ns: StrictInt = Field(ge=0)
    end_ns: StrictInt = Field(gt=0)
    available_ns: StrictInt = Field(gt=0)
    loss: Decimal


class DSRReference(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)
    currency: Literal["USDT"]
    convention: Literal["NEGATIVE_REFERENCE_RETURN_OVER_FIXED_INITIAL_CAPITAL"]
    capital: Decimal = Field(gt=0)
    selected_variant: str
    registered_variants: tuple[str, ...]
    effective_independent_trials: float = Field(ge=1)
    independence_basis: str
    reference_basis: str
    source_identity: str
    intervals: tuple[ReferenceInterval, ...] = Field(min_length=4)

    @field_validator("selected_variant", "independence_basis", "reference_basis", "source_identity")
    @classmethod
    def nonempty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("explicit nonempty reference/plan metadata required")
        return value

    def plan(self) -> DSRPlan:
        return DSRPlan(
            self.selected_variant,
            self.registered_variants,
            self.effective_independent_trials,
            self.independence_basis,
        )

    def losses(self) -> tuple[IntervalLoss, ...]:
        return tuple(
            IntervalLoss(r.start_ns, r.end_ns, r.available_ns, r.loss) for r in self.intervals
        )
