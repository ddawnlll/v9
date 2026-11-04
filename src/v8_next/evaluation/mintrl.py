"""Declared-assumption minimum track-length estimator.

MinTRL is horizon evidence only.  It does not select a strategy, certify a
Sharpe ratio, or create an economic claim.  Every statistical and dependence
assumption is supplied by the caller; absent inputs fail closed.

Two identities share the name "minimum track record length" and are *not* the
same estimator, so each plan must declare which one it is and carry exactly
that identity's inputs:

``POWER_REQUIRED_N``
    A two-sided size-plus-power required-N
    ``ceil((z_{1-a/2} + z_power)^2 * variance * dependence_factor / gap^2)``.

``BAILEY_LDP_2012``
    The published closed form (Bailey & Lopez de Prado 2012, "The Sharpe Ratio
    Efficient Frontier", J. Risk 15(2), eq. 8)
    ``1 + (1 - g1*SR + ((g2 - 1)/4)*SR^2) * (z_confidence / (SR - SR*))^2``
    with ``SR`` the observed Sharpe, ``SR*`` the benchmark, ``g1`` skewness and
    ``g2`` *Pearson* (non-excess; 3 for a Gaussian) kurtosis.  It carries no
    power term and no dependence term: the closed form is single-trial and
    IID-flavoured, so ``effective_intervals`` stays unset rather than having a
    dependence model assumed for it.
"""

from __future__ import annotations

import hashlib
import math
from statistics import NormalDist
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from v8_next.evaluation.store import canonical

MinTRLIdentity = Literal["POWER_REQUIRED_N", "BAILEY_LDP_2012"]

_POWER_INPUTS = ("null_sharpe", "power", "variance", "dependence_factor")
_CLOSED_FORM_INPUTS = ("observed_sharpe", "return_skew", "return_kurtosis")


class MinTRLPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    identity: MinTRLIdentity
    target_sharpe: float
    confidence: float = Field(gt=0.0, lt=1.0)
    interval_unit: str
    annualization_factor: float = Field(gt=0.0)
    method_version: str
    # POWER_REQUIRED_N inputs
    null_sharpe: float | None = None
    power: float | None = Field(default=None, gt=0.0, lt=1.0)
    variance: float | None = Field(default=None, gt=0.0)
    dependence_factor: float | None = Field(default=None, gt=0.0)
    # BAILEY_LDP_2012 inputs
    observed_sharpe: float | None = None
    return_skew: float | None = None
    return_kurtosis: float | None = None

    @model_validator(mode="after")
    def validate_assumptions(self) -> MinTRLPlan:
        declared = (
            self.target_sharpe,
            self.confidence,
            self.annualization_factor,
            self.null_sharpe,
            self.power,
            self.variance,
            self.dependence_factor,
            self.observed_sharpe,
            self.return_skew,
            self.return_kurtosis,
        )
        if not all(value is None or math.isfinite(value) for value in declared):
            raise ValueError("MinTRL assumptions must be finite")
        if not self.interval_unit.strip() or not self.method_version.strip():
            raise ValueError("interval unit and method version are required")
        required = _POWER_INPUTS if self.identity == "POWER_REQUIRED_N" else _CLOSED_FORM_INPUTS
        unused = _CLOSED_FORM_INPUTS if self.identity == "POWER_REQUIRED_N" else _POWER_INPUTS
        for field in required:
            if getattr(self, field) is None:
                raise ValueError(f"{self.identity} requires declared {field}")
        for field in unused:
            if getattr(self, field) is not None:
                raise ValueError(
                    f"{self.identity} does not consume {field}; declare one identity's inputs only"
                )
        if self.identity == "POWER_REQUIRED_N":
            assert self.null_sharpe is not None
            if self.target_sharpe <= self.null_sharpe:
                raise ValueError("target Sharpe must exceed null Sharpe")
        return self

    @property
    def alpha(self) -> float:
        return 1.0 - self.confidence

    @property
    def plan_hash(self) -> str:
        return hashlib.sha256(canonical(self.model_dump(mode="json")).encode()).hexdigest()


class MinTRLResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["UNDERPOWERED", "SUPPORTED_HORIZON", "UNSUPPORTED"]
    required_intervals: int | None
    observed_intervals: int
    effective_intervals: float | None
    plan_hash: str
    reason: str | None = None


def _unsupported(
    plan: MinTRLPlan, observed_intervals: int, reason: str
) -> MinTRLResult:
    return MinTRLResult(
        status="UNSUPPORTED",
        required_intervals=None,
        observed_intervals=observed_intervals,
        effective_intervals=None,
        plan_hash=plan.plan_hash,
        reason=reason,
    )


def _required_power(plan: MinTRLPlan) -> int:
    assert plan.null_sharpe is not None
    assert plan.power is not None
    assert plan.variance is not None
    assert plan.dependence_factor is not None
    z_alpha = NormalDist().inv_cdf(1.0 - plan.alpha / 2.0)
    z_power = NormalDist().inv_cdf(plan.power)
    delta = plan.target_sharpe - plan.null_sharpe
    return max(
        1,
        math.ceil(
            ((z_alpha + z_power) ** 2 * plan.variance * plan.dependence_factor) / (delta**2)
        ),
    )


def _required_closed_form(plan: MinTRLPlan) -> int | None:
    """Published Bailey & Lopez de Prado (2012) MinTRL; ``None`` when undefined."""

    assert plan.observed_sharpe is not None
    assert plan.return_skew is not None
    assert plan.return_kurtosis is not None
    gap = plan.observed_sharpe - plan.target_sharpe
    if gap <= 0.0:
        return None
    z_alpha = NormalDist().inv_cdf(plan.confidence)
    correction = (
        1.0
        - plan.return_skew * plan.observed_sharpe
        + ((plan.return_kurtosis - 1.0) / 4.0) * plan.observed_sharpe**2
    )
    if correction <= 0.0:
        return None
    return max(1, math.ceil(1.0 + correction * (z_alpha / gap) ** 2))


def estimate_min_trl(plan: MinTRLPlan | None, observed_intervals: int) -> MinTRLResult:
    """Estimate required aligned intervals under the plan's declared identity.

    The caller declares which estimator it means (``plan.identity``); the two
    identities are not interchangeable and are dispatched explicitly here.  A
    plan whose declared inputs are absent fails closed instead of falling back
    to the other estimator's formula.
    """

    if type(observed_intervals) is not int or observed_intervals < 0:
        raise ValueError("observed_intervals must be a non-negative integer")
    if plan is None:
        return MinTRLResult(
            status="UNSUPPORTED",
            required_intervals=None,
            observed_intervals=observed_intervals,
            effective_intervals=None,
            plan_hash="",
            reason="DECLARED_MINTRL_PLAN_REQUIRED",
        )
    if plan.identity == "POWER_REQUIRED_N":
        required = _required_power(plan)
        assert plan.dependence_factor is not None
        effective: float | None = observed_intervals / plan.dependence_factor
    else:
        closed_form = _required_closed_form(plan)
        if closed_form is None:
            return _unsupported(plan, observed_intervals, "MINTRL_CLOSED_FORM_UNDEFINED")
        required = closed_form
        effective = None
    return MinTRLResult(
        status="SUPPORTED_HORIZON" if observed_intervals >= required else "UNDERPOWERED",
        required_intervals=required,
        observed_intervals=observed_intervals,
        effective_intervals=effective,
        plan_hash=plan.plan_hash,
    )
