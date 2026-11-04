"""M4-01 (#29): preregistered hypotheses, matched controls and locked gates.

A research claim is only interpretable if the gate set was fixed *before* any
sealed data was read. This module makes that fixity machine-readable:

* :class:`Preregistration` is the frozen record of one economic hypothesis: the
  intervention, the control arms (including random entry with matched exits), the
  unit of comparison, the dataset role, every cost term (fees, funding,
  slippage), minimum trades, the drawdown gate, primary/secondary metrics,
  expected direction, the accepted negative outcomes (``NO_EDGE``/``STOP``), the
  hypothesis budget, the timestamp, the code/config SHAs and the version.
* :meth:`Preregistration.validate` refuses an incomplete registration: a missing
  control arm, a missing cost term, a missing negative-outcome rule and a missing
  budget each name their own field.
* :meth:`Preregistration.digest` is a domain-separated canonical digest, so the
  same fields reproduce the same identity on any machine and JSON round-trips
  preserve it.
* :meth:`Preregistration.compare_against_result` re-derives the registered gates
  and diffs them against the gates an experiment actually used. A post-hoc change
  (e.g. loosening ``max_drawdown`` after seeing the result) raises
  :class:`PostHocGateChange` naming every changed field.

No order objects, no execution adapter import, no live-trading authority.
"""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum
from typing import Any, Self

from pydantic import BaseModel, ConfigDict

from v8_next.evaluation.protocol import (
    NEGATIVE_OUTCOMES,
    CorrectionMethod,
    DatasetRole,
    Verdict,
)
from v8_next.telemetry.identity import canonical_digest

#: Bumped when the registered field set changes; an old record stays readable but
#: no longer validates, which is the point of recording it.
PREREGISTRATION_VERSION = "1.0"


class PreregistrationError(ValueError):
    """Typed refusal of an incomplete or post-hoc-mutated preregistration.

    ``field`` is the first offending field; ``fields`` names every offending field
    so a caller fixes the whole set, not one symptom.
    """

    code: str = "PREREGISTRATION_INVALID"

    def __init__(
        self,
        message: str,
        *,
        field: str,
        fields: tuple[str, ...] | None = None,
        code: str | None = None,
    ) -> None:
        resolved = code or type(self).code
        super().__init__(f"{resolved}: {message}")
        self.code = resolved
        self.message = message
        self.field = field
        self.fields = fields or (field,)


class PostHocGateChange(PreregistrationError):
    """An experiment's gates differ from the registered ones."""

    code = "POST_HOC_GATE_CHANGE"

    def __init__(self, fields: tuple[str, ...], message: str) -> None:
        super().__init__(message, field=fields[0], fields=fields, code=self.code)


class ArmKind(StrEnum):
    """Which arm of the comparison an entry is."""

    V8_BASELINE = "V8_BASELINE"
    RANDOM_ENTRY_MATCHED_EXITS = "RANDOM_ENTRY_MATCHED_EXITS"
    SIMPLE_CONTROL_POLICY = "SIMPLE_CONTROL_POLICY"
    V9_INTERVENTION = "V9_INTERVENTION"


class Direction(StrEnum):
    """Expected direction of the primary metric for the intervention arm."""

    INCREASE = "INCREASE"
    DECREASE = "DECREASE"
    DIFFERENT = "DIFFERENT"


class MatchedDimensions(BaseModel):
    """The dimensions every arm must share for the comparison to be matched."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    calendar: str
    capital: Decimal
    venue: str
    opportunity_exposure: str
    exit_policy: str


class Arm(BaseModel):
    """One arm: the intervention, or a control matched to it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    arm_id: str
    kind: ArmKind
    description: str
    matched: MatchedDimensions | None = None


class CostModel(BaseModel):
    """Every cost term the comparison charges, in basis points.

    A term is ``None`` until declared; :meth:`Preregistration.validate` refuses
    ``None`` and a model without funding. Zero is a declared value (free tier,
    zero funding) and is legitimate; an absent term is not.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    fee_bps_per_side: Decimal | None = None
    funding_bps_per_day: Decimal | None = None
    slippage_bps_per_side: Decimal | None = None
    funding_included: bool = True
    venue: str = ""


class HypothesisBudget(BaseModel):
    """How many hypotheses and trials the test is allowed to spend."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    family: str = ""
    max_hypotheses: int | None = None
    max_trials: int | None = None
    alpha: Decimal | None = None
    correction: CorrectionMethod | None = None


class AppliedGates(BaseModel):
    """The gates an experiment actually used, as reported by its result."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    dataset_role: DatasetRole | None = None
    unit_of_comparison: str = ""
    primary_metric: str = ""
    minimum_trades: int | None = None
    max_drawdown: Decimal | None = None
    alpha: Decimal | None = None
    max_hypotheses: int | None = None
    correction: CorrectionMethod | None = None
    fee_bps_per_side: Decimal | None = None
    funding_bps_per_day: Decimal | None = None
    slippage_bps_per_side: Decimal | None = None
    accepted_outcomes: tuple[Verdict, ...] = ()


class Preregistration(BaseModel):
    """A frozen, machine-readable registration of one economic hypothesis."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    hypothesis_id: str
    statement: str
    intervention: Arm | None = None
    control_arms: tuple[Arm, ...] = ()
    unit_of_comparison: str = ""
    dataset_role: DatasetRole | None = None
    costs: CostModel | None = None
    minimum_trades: int | None = None
    max_drawdown: Decimal | None = None
    primary_metrics: tuple[str, ...] = ()
    secondary_metrics: tuple[str, ...] = ()
    expected_direction: Direction | None = None
    accepted_outcomes: tuple[Verdict, ...] = ()
    decision_rule: str = ""
    hypothesis_budget: HypothesisBudget | None = None
    created_ns: int | None = None
    code_sha: str = ""
    config_sha: str = ""
    version: str = PREREGISTRATION_VERSION

    # -- identity -------------------------------------------------------------

    def digest(self) -> str:
        """Domain-separated canonical digest of the registered content."""
        return canonical_digest("v8-next-preregistration", self.model_dump(mode="json"))

    def to_json(self) -> str:
        """Stable JSON encoding; ``from_json`` restores an identical digest."""
        return self.model_dump_json()

    @classmethod
    def from_json(cls, payload: str) -> Preregistration:
        return cls.model_validate_json(payload)

    # -- gates ----------------------------------------------------------------

    def primary_metric(self) -> str:
        return self.primary_metrics[0] if self.primary_metrics else ""

    def gates(self) -> AppliedGates:
        """The gate vector this registration fixes, independent of any result."""
        costs = self.costs
        budget = self.hypothesis_budget
        return AppliedGates(
            dataset_role=self.dataset_role,
            unit_of_comparison=self.unit_of_comparison,
            primary_metric=self.primary_metric(),
            minimum_trades=self.minimum_trades,
            max_drawdown=self.max_drawdown,
            alpha=budget.alpha if budget is not None else None,
            max_hypotheses=budget.max_hypotheses if budget is not None else None,
            correction=budget.correction if budget is not None else None,
            fee_bps_per_side=costs.fee_bps_per_side if costs is not None else None,
            funding_bps_per_day=costs.funding_bps_per_day if costs is not None else None,
            slippage_bps_per_side=costs.slippage_bps_per_side if costs is not None else None,
            accepted_outcomes=self.accepted_outcomes,
        )

    # -- validation -----------------------------------------------------------

    def validate(self) -> Self:  # type: ignore[override]  # deliberate: instance gate, not the pydantic classmethod
        """Refuse an incomplete registration, naming every offending field.

        This is deliberately *not* a constructor validator: a registration can be
        assembled field by field, and the refusal must point at the missing
        element rather than at the whole record.
        """
        problems: list[tuple[str, str]] = []

        def require(condition: bool, field: str, message: str) -> None:
            if not condition:
                problems.append((field, message))

        require(bool(self.hypothesis_id.strip()), "hypothesis_id", "hypothesis id is required")
        require(bool(self.statement.strip()), "statement", "a falsifiable statement is required")
        require(
            self.version == PREREGISTRATION_VERSION,
            "version",
            f"unsupported version {self.version!r}; expected {PREREGISTRATION_VERSION!r}",
        )
        require(bool(self.code_sha.strip()), "code_sha", "code SHA is required")
        require(bool(self.config_sha.strip()), "config_sha", "config SHA is required")
        require(
            self.created_ns is not None and self.created_ns >= 0,
            "created_ns",
            "a registration timestamp is required",
        )
        require(
            self.dataset_role is not None,
            "dataset_role",
            "a dataset role (DEVELOPMENT/VALIDATION/SEALED_OOS) is required",
        )
        require(
            bool(self.unit_of_comparison.strip()),
            "unit_of_comparison",
            "the unit of comparison is required",
        )

        intervention = self.intervention
        require(intervention is not None, "intervention", "an intervention arm is required")
        if intervention is not None:
            require(
                intervention.kind is ArmKind.V9_INTERVENTION,
                "intervention.kind",
                "the intervention arm must be a V9_INTERVENTION",
            )
            require(
                bool(intervention.arm_id.strip()),
                "intervention.arm_id",
                "the intervention arm needs an id",
            )
            require(
                intervention.matched is not None,
                "intervention.matched",
                "the intervention must declare its matched dimensions",
            )
        require(bool(self.control_arms), "control_arms", "at least one control arm is required")
        entry_kinds = {arm.kind for arm in self.control_arms}
        require(
            ArmKind.V8_BASELINE in entry_kinds,
            "control_arms",
            "the V8 baseline arm is required for a like-for-like comparison",
        )
        require(
            ArmKind.RANDOM_ENTRY_MATCHED_EXITS in entry_kinds,
            "control_arms",
            "a random-entry arm with matched exits is required",
        )
        require(
            ArmKind.SIMPLE_CONTROL_POLICY in entry_kinds,
            "control_arms",
            "a simple control policy arm is required so the intervention is compared "
            "against a non-V9 policy, not only against noise",
        )
        reference = intervention.matched if intervention is not None else None
        for arm in self.control_arms:
            label = f"control_arms[{arm.arm_id or '?'}]"
            require(
                arm.kind is not ArmKind.V9_INTERVENTION,
                label,
                "a control arm cannot be the intervention",
            )
            require(bool(arm.arm_id.strip()), label, "a control arm needs an id")
            require(arm.matched is not None, f"{label}.matched", "control arm is not matched")
            if arm.matched is not None and reference is not None:
                require(
                    arm.matched == reference,
                    f"{label}.matched",
                    "control arm does not share the same calendar/capital/venue/"
                    "opportunity exposure/exit policy",
                )
            if arm.matched is not None:
                dimensions = arm.matched
                require(
                    bool(dimensions.calendar.strip()),
                    f"{label}.matched.calendar",
                    "control arm has no calendar",
                )
                require(
                    dimensions.capital > 0,
                    f"{label}.matched.capital",
                    "control arm capital must be positive",
                )
                require(
                    bool(dimensions.venue.strip()),
                    f"{label}.matched.venue",
                    "control arm has no venue",
                )
                require(
                    bool(dimensions.opportunity_exposure.strip()),
                    f"{label}.matched.opportunity_exposure",
                    "control arm has no opportunity exposure",
                )
                require(
                    bool(dimensions.exit_policy.strip()),
                    f"{label}.matched.exit_policy",
                    "control arm has no exit policy",
                )

        costs = self.costs
        require(costs is not None, "costs", "a cost model is required")
        if costs is not None:
            require(
                costs.fee_bps_per_side is not None,
                "costs.fee_bps_per_side",
                "the fee term is required (declare 0 explicitly if free)",
            )
            require(
                costs.funding_bps_per_day is not None,
                "costs.funding_bps_per_day",
                "the funding term is required (declare 0 explicitly if none)",
            )
            require(
                costs.slippage_bps_per_side is not None,
                "costs.slippage_bps_per_side",
                "the slippage term is required (declare 0 explicitly if none)",
            )
            require(
                costs.funding_included is True,
                "costs.funding_included",
                "funding must be included in the comparison",
            )
            require(bool(costs.venue.strip()), "costs.venue", "the cost venue is required")
            for term in ("fee_bps_per_side", "funding_bps_per_day", "slippage_bps_per_side"):
                value = getattr(costs, term)
                if value is not None:
                    require(value >= 0, f"costs.{term}", f"{term} must be non-negative")

        require(
            self.minimum_trades is not None and self.minimum_trades >= 1,
            "minimum_trades",
            "a minimum trade count is required",
        )
        require(
            self.max_drawdown is not None,
            "max_drawdown",
            "a maximum drawdown gate is required (declare 1 explicitly if unbounded)",
        )
        if self.max_drawdown is not None:
            require(
                Decimal("0") <= self.max_drawdown <= Decimal("1"),
                "max_drawdown",
                "max drawdown must be a fraction within [0, 1]",
            )
        require(bool(self.primary_metrics), "primary_metrics", "a primary metric is required")
        require(
            all(metric.strip() for metric in self.primary_metrics),
            "primary_metrics",
            "primary metric names cannot be empty",
        )
        require(
            len(set(self.primary_metrics)) == len(self.primary_metrics),
            "primary_metrics",
            "primary metrics must be distinct",
        )
        require(
            all(metric.strip() for metric in self.secondary_metrics),
            "secondary_metrics",
            "secondary metric names cannot be empty",
        )
        require(
            self.expected_direction is not None,
            "expected_direction",
            "an expected direction is required",
        )

        require(bool(self.decision_rule.strip()), "decision_rule", "a decision rule is required")
        missing = tuple(
            outcome.value
            for outcome in sorted(NEGATIVE_OUTCOMES, key=lambda item: item.value)
            if outcome not in self.accepted_outcomes
        )
        require(
            not missing,
            "accepted_outcomes",
            f"the decision rule must accept NO_EDGE and STOP (missing {missing})",
        )
        require(
            len(set(self.accepted_outcomes)) == len(self.accepted_outcomes),
            "accepted_outcomes",
            "accepted outcomes must be distinct",
        )
        require(
            Verdict.PASS not in self.accepted_outcomes,
            "accepted_outcomes",
            "PASS is claimed by evidence, never preregistered as accepted",
        )

        budget = self.hypothesis_budget
        require(budget is not None, "hypothesis_budget", "a hypothesis budget is required")
        if budget is not None:
            require(
                bool(budget.family.strip()),
                "hypothesis_budget.family",
                "the hypothesis family name is required",
            )
            require(
                budget.max_hypotheses is not None and budget.max_hypotheses >= 1,
                "hypothesis_budget.max_hypotheses",
                "a positive hypothesis budget is required",
            )
            require(
                budget.max_trials is not None and budget.max_trials >= 1,
                "hypothesis_budget.max_trials",
                "a positive trial budget is required",
            )
            if budget.max_hypotheses is not None and budget.max_trials is not None:
                require(
                    budget.max_trials >= budget.max_hypotheses,
                    "hypothesis_budget.max_trials",
                    "the trial budget cannot be smaller than the hypothesis budget",
                )
            require(
                budget.alpha is not None and Decimal("0") < budget.alpha < Decimal("1"),
                "hypothesis_budget.alpha",
                "alpha must be strictly between 0 and 1",
            )
            require(
                budget.correction is not None,
                "hypothesis_budget.correction",
                "a multiple-testing correction is required",
            )

        if problems:
            raise PreregistrationError(
                "; ".join(f"{field}: {message}" for field, message in problems),
                field=problems[0][0],
                fields=tuple(field for field, _ in problems),
            )
        return self

    # -- post-hoc detection ---------------------------------------------------

    def compare_against_result(self, result: ExperimentResult) -> ComparisonReceipt:
        """Diff the registered gates against the ones the experiment used.

        Any difference — including a result produced under a different
        preregistration digest, a swapped dataset role, or a loosened cost/drawdown
        gate — raises :class:`PostHocGateChange` naming the changed fields.
        """
        registered = self.gates()
        differing: list[str] = []
        if result.preregistration_digest != self.digest():
            differing.append("preregistration_digest")
        if result.hypothesis_id != self.hypothesis_id:
            differing.append("hypothesis_id")
        for field in AppliedGates.model_fields:
            if getattr(registered, field) != getattr(result.gates, field):
                differing.append(field)
        if differing:
            raise PostHocGateChange(
                tuple(differing),
                "experiment gates differ from the registered preregistration: "
                + ", ".join(differing),
            )
        return ComparisonReceipt(
            preregistration_digest=self.digest(),
            result_digest=result.digest(),
            checked_fields=tuple(AppliedGates.model_fields),
        )


class ExperimentResult(BaseModel):
    """What an experiment reports: the gates it used and what it observed."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    experiment_id: str
    hypothesis_id: str
    preregistration_digest: str
    gates: AppliedGates
    observed_trades: int = 0
    observed_drawdown: Decimal = Decimal("0")
    observed_metric: Decimal = Decimal("0")
    verdict: Verdict = Verdict.INCONCLUSIVE

    def digest(self) -> str:
        return canonical_digest("v8-next-experiment-result", self.model_dump(mode="json"))


class ComparisonReceipt(BaseModel):
    """Proof that a result's gates were compared, field by field, to the registry."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    preregistration_digest: str
    result_digest: str
    checked_fields: tuple[str, ...]
    matched: bool = True


#: Field order used by reports that render the registered gate vector.
GATE_FIELDS: tuple[str, ...] = tuple(AppliedGates.model_fields)


def gate_report(preregistration: Preregistration) -> dict[str, Any]:
    """JSON-safe rendering of the registered gates, for a result report."""
    return preregistration.gates().model_dump(mode="json")
