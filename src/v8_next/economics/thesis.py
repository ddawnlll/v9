"""Immutable position thesis and recurrent evaluation (M3-02).

A position is opened by a decision, but it is managed against the *thesis* that
justified it. This module keeps two things strictly apart:

* :class:`PositionThesis` — frozen entry rationale (decision, opportunity, belief,
  original stop, original risk, target plan, invalidation rules, policy version).
  Nothing here can rewrite it, and no function in this module mutates a stop.
* :class:`PositionSnapshot` — mutable-by-replacement native state (quantity, mark,
  protective orders, funding, elapsed bars) supplied by the caller.

:func:`evaluate_position` is a pure recurrent check: given the thesis, the current
native snapshot and the current belief/evidence, it returns an explanatory
:class:`PositionEvaluation` with advisory action proposals. Unknown or future
inputs produce ``MISSING_INPUT``; they never become a silent ``HOLD``. Actual
stop ownership and order side effects remain with the single native risk/execution
authority.
"""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from enum import StrEnum
from typing import Any, Final

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from v8_next.domain.decision import DecisionAction

SCHEMA_VERSION: Final[str] = "position-thesis-v1"
ZERO = Decimal("0")


class ThesisStatus(StrEnum):
    HOLDING = "HOLDING"
    WEAKENING = "WEAKENING"
    INVALIDATED = "INVALIDATED"
    MISSING_INPUT = "MISSING_INPUT"
    CLOSED = "CLOSED"


class ThesisReason(StrEnum):
    THESIS_INTACT = "THESIS_INTACT"
    STOP_BREACHED = "STOP_BREACHED"
    REGIME_CHANGED = "REGIME_CHANGED"
    EXPERT_CONTRADICTION = "EXPERT_CONTRADICTION"
    TIME_LIMIT = "TIME_LIMIT"
    PROTECTION_GAP = "PROTECTION_GAP"
    BELIEF_MISSING = "BELIEF_MISSING"
    BELIEF_STALE = "BELIEF_STALE"
    BELIEF_ABSTAIN = "BELIEF_ABSTAIN"
    FUTURE_INPUT = "FUTURE_INPUT"
    POSITION_ID_MISMATCH = "POSITION_ID_MISMATCH"
    EVIDENCE_INVALID = "EVIDENCE_INVALID"
    POSITION_CLOSED = "POSITION_CLOSED"


class StopKind(StrEnum):
    STRUCTURAL = "STRUCTURAL"
    NATIVE_PROTECTIVE = "NATIVE_PROTECTIVE"
    UNKNOWN = "UNKNOWN"


class InvalidationKind(StrEnum):
    PRICE_STOP = "PRICE_STOP"
    REGIME_CHANGE = "REGIME_CHANGE"
    EXPERT_CONTRADICTION = "EXPERT_CONTRADICTION"
    TIME_LIMIT = "TIME_LIMIT"
    PROTECTION_GAP = "PROTECTION_GAP"


class StanceKind(StrEnum):
    SUPPORT = "SUPPORT"
    CONTRADICT = "CONTRADICT"
    ABSTAIN = "ABSTAIN"


class TargetPlan(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    target_price: Decimal
    expected_bars: int = Field(ge=1)
    max_bars: int = Field(ge=1)
    r_multiple: Decimal | None = None

    @model_validator(mode="after")
    def _ordered(self) -> TargetPlan:
        if not self.target_price.is_finite() or self.target_price <= ZERO:
            raise ValueError("target_price must be finite and positive")
        if self.max_bars < self.expected_bars:
            raise ValueError("max_bars cannot precede expected_bars")
        if self.r_multiple is not None and not self.r_multiple.is_finite():
            raise ValueError("r_multiple must be finite")
        return self


class InvalidationRule(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: InvalidationKind
    threshold: Decimal | None = None

    @model_validator(mode="after")
    def _threshold(self) -> InvalidationRule:
        if self.kind is InvalidationKind.EXPERT_CONTRADICTION:
            if self.threshold is None or not (ZERO < self.threshold <= Decimal("1")):
                raise ValueError("expert-contradiction threshold must be in (0, 1]")
        elif self.threshold is not None:
            raise ValueError(f"{self.kind.value} cannot carry a numeric threshold")
        return self


class ExpertEvidence(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    expert_id: str = Field(min_length=1)
    stance: StanceKind
    weight: Decimal = Decimal("1")

    @field_validator("weight")
    @classmethod
    def _weight(cls, value: Decimal) -> Decimal:
        if not value.is_finite() or value < ZERO:
            raise ValueError("weight must be finite and non-negative")
        return value


class ThesisBelief(BaseModel):
    """Minimal belief projection used by the position plane.

    Adapters construct this from the belief ledger; the position plane does not
    import the belief schema, so a belief evolution cannot silently change what a
    historical evaluation meant.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    belief_id: str = Field(min_length=1)
    version_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    regime: str = Field(min_length=1)
    as_of_ns: int = Field(ge=0)
    stale: bool = False
    abstain: bool = False


def _digest(payload: dict[str, Any]) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


def make_thesis_id(
    *,
    entry_decision_id: str,
    original_opportunity_id: str,
    original_market_belief_id: str,
    native_position_id: str,
    policy_version: str,
    created_ns: int,
) -> str:
    return _digest(
        {
            "schema_version": SCHEMA_VERSION,
            "entry_decision_id": entry_decision_id,
            "original_opportunity_id": original_opportunity_id,
            "original_market_belief_id": original_market_belief_id,
            "native_position_id": native_position_id,
            "policy_version": policy_version,
            "created_ns": created_ns,
        }
    )


class PositionThesis(BaseModel):
    """Frozen entry rationale. There is deliberately no stop-mutation method."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    thesis_id: str = Field(min_length=1)
    entry_decision_id: str = Field(min_length=1)
    original_opportunity_id: str = Field(min_length=1)
    original_market_belief_id: str = Field(min_length=1)
    native_position_id: str = Field(min_length=1)
    instrument_id: str = Field(min_length=1)
    side: str
    entry_price: Decimal
    initial_stop_price: Decimal
    initial_stop_kind: StopKind
    original_risk_usdt: Decimal
    target_plan: TargetPlan
    invalidation_rules: tuple[InvalidationRule, ...] = Field(min_length=1)
    entry_regime: str = Field(min_length=1)
    policy_version: str = Field(min_length=1)
    created_ns: int = Field(ge=0)
    advisory_only: bool = True

    @model_validator(mode="after")
    def _validate(self) -> PositionThesis:
        if self.side not in {"LONG", "SHORT"}:
            raise ValueError("side must be LONG or SHORT")
        if self.advisory_only is not True:
            raise ValueError("position thesis never grants order authority")
        for name in ("entry_price", "initial_stop_price", "original_risk_usdt"):
            value = getattr(self, name)
            if not value.is_finite():
                raise ValueError(f"{name} must be finite")
        if self.entry_price <= ZERO or self.initial_stop_price <= ZERO:
            raise ValueError("prices must be positive")
        if self.original_risk_usdt <= ZERO:
            raise ValueError("original risk must be positive")
        if self.side == "LONG" and self.initial_stop_price >= self.entry_price:
            raise ValueError("LONG stop must be below entry")
        if self.side == "SHORT" and self.initial_stop_price <= self.entry_price:
            raise ValueError("SHORT stop must be above entry")
        kinds = [rule.kind for rule in self.invalidation_rules]
        if len(set(kinds)) != len(kinds):
            raise ValueError("invalidation rules must be unique by kind")
        expected = make_thesis_id(
            entry_decision_id=self.entry_decision_id,
            original_opportunity_id=self.original_opportunity_id,
            original_market_belief_id=self.original_market_belief_id,
            native_position_id=self.native_position_id,
            policy_version=self.policy_version,
            created_ns=self.created_ns,
        )
        if expected != self.thesis_id:
            raise ValueError("thesis_id does not re-derive from its identity fields")
        return self

    @property
    def original_stop_distance(self) -> Decimal:
        return abs(self.entry_price - self.initial_stop_price)

    def to_receipt(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "thesis_id": self.thesis_id,
            "entry_decision_id": self.entry_decision_id,
            "original_opportunity_id": self.original_opportunity_id,
            "original_market_belief_id": self.original_market_belief_id,
            "native_position_id": self.native_position_id,
            "instrument_id": self.instrument_id,
            "side": self.side,
            "entry_price": str(self.entry_price),
            "initial_stop_price": str(self.initial_stop_price),
            "original_risk_usdt": str(self.original_risk_usdt),
            "policy_version": self.policy_version,
            "advisory_only": self.advisory_only,
        }


def build_thesis(
    *,
    entry_decision_id: str,
    original_opportunity_id: str,
    original_market_belief_id: str,
    native_position_id: str,
    instrument_id: str,
    side: str,
    entry_price: Decimal,
    initial_stop_price: Decimal,
    initial_stop_kind: StopKind,
    original_risk_usdt: Decimal,
    target_plan: TargetPlan,
    invalidation_rules: tuple[InvalidationRule, ...],
    entry_regime: str,
    policy_version: str,
    created_ns: int,
) -> PositionThesis:
    thesis_id = make_thesis_id(
        entry_decision_id=entry_decision_id,
        original_opportunity_id=original_opportunity_id,
        original_market_belief_id=original_market_belief_id,
        native_position_id=native_position_id,
        policy_version=policy_version,
        created_ns=created_ns,
    )
    return PositionThesis(
        thesis_id=thesis_id,
        entry_decision_id=entry_decision_id,
        original_opportunity_id=original_opportunity_id,
        original_market_belief_id=original_market_belief_id,
        native_position_id=native_position_id,
        instrument_id=instrument_id,
        side=side,
        entry_price=entry_price,
        initial_stop_price=initial_stop_price,
        initial_stop_kind=initial_stop_kind,
        original_risk_usdt=original_risk_usdt,
        target_plan=target_plan,
        invalidation_rules=invalidation_rules,
        entry_regime=entry_regime,
        policy_version=policy_version,
        created_ns=created_ns,
    )


class PositionSnapshot(BaseModel):
    """Current native state, supplied by the execution authority."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    native_position_id: str = Field(min_length=1)
    quantity: Decimal
    average_price: Decimal
    mark_price: Decimal
    protective_order_ids: tuple[str, ...] = ()
    latest_belief_id: str | None = None
    funding_exposure: Decimal = ZERO
    elapsed_bars: int = Field(ge=0)
    available_at_ns: int = Field(ge=0)

    @model_validator(mode="after")
    def _validate(self) -> PositionSnapshot:
        for name in ("quantity", "average_price", "mark_price", "funding_exposure"):
            if not getattr(self, name).is_finite():
                raise ValueError(f"{name} must be finite")
        if self.mark_price <= ZERO:
            raise ValueError("mark_price must be positive")
        if self.quantity != ZERO and self.average_price <= ZERO:
            raise ValueError("average_price must be positive for an open quantity")
        if len(set(self.protective_order_ids)) != len(self.protective_order_ids):
            raise ValueError("protective order ids must be unique")
        return self


class PositionEvaluation(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    evaluation_id: str
    thesis_id: str
    native_position_id: str
    evaluated_ns: int
    status: ThesisStatus
    reasons: tuple[ThesisReason, ...]
    proposals: tuple[DecisionAction, ...]
    admission_ok: bool
    advisory_only: bool = True

    @model_validator(mode="after")
    def _validate(self) -> PositionEvaluation:
        if self.advisory_only is not True:
            raise ValueError("position evaluation is advisory only")
        if not self.reasons:
            raise ValueError("an evaluation must explain itself")
        if self.status is ThesisStatus.HOLDING and self.proposals != (DecisionAction.HOLD,):
            raise ValueError("HOLDING must propose exactly HOLD")
        if self.status is ThesisStatus.INVALIDATED and DecisionAction.HOLD in self.proposals:
            raise ValueError("an invalidated thesis cannot propose HOLD")
        if self.status is ThesisStatus.MISSING_INPUT and self.admission_ok:
            raise ValueError("missing input cannot be admitted")
        return self


def _evaluation_id(
    *,
    thesis_id: str,
    native_position_id: str,
    evaluated_ns: int,
    status: ThesisStatus,
    reasons: tuple[ThesisReason, ...],
    proposals: tuple[DecisionAction, ...],
) -> str:
    return _digest(
        {
            "schema_version": SCHEMA_VERSION,
            "thesis_id": thesis_id,
            "native_position_id": native_position_id,
            "evaluated_ns": evaluated_ns,
            "status": status.value,
            "reasons": [item.value for item in reasons],
            "proposals": [item.value for item in proposals],
        }
    )


def _rule(thesis: PositionThesis, kind: InvalidationKind) -> InvalidationRule | None:
    for rule in thesis.invalidation_rules:
        if rule.kind is kind:
            return rule
    return None


def evaluate_position(
    thesis: PositionThesis,
    snapshot: PositionSnapshot,
    belief: ThesisBelief | None,
    *,
    now_ns: int,
    evidence: tuple[ExpertEvidence, ...] = (),
) -> PositionEvaluation:
    """Explain what is happening to the thesis; never mutate the stop or trade."""

    def missing(reason: ThesisReason) -> PositionEvaluation:
        reasons = (reason,)
        proposals = (DecisionAction.WAIT,)
        return PositionEvaluation(
            evaluation_id=_evaluation_id(
                thesis_id=thesis.thesis_id,
                native_position_id=snapshot.native_position_id,
                evaluated_ns=now_ns,
                status=ThesisStatus.MISSING_INPUT,
                reasons=reasons,
                proposals=proposals,
            ),
            thesis_id=thesis.thesis_id,
            native_position_id=snapshot.native_position_id,
            evaluated_ns=now_ns,
            status=ThesisStatus.MISSING_INPUT,
            reasons=reasons,
            proposals=proposals,
            admission_ok=False,
        )

    if snapshot.native_position_id != thesis.native_position_id:
        return missing(ThesisReason.POSITION_ID_MISMATCH)
    if now_ns < snapshot.available_at_ns:
        return missing(ThesisReason.FUTURE_INPUT)
    if now_ns < thesis.created_ns:
        return missing(ThesisReason.FUTURE_INPUT)
    if snapshot.quantity == ZERO:
        closed_reasons = (ThesisReason.POSITION_CLOSED,)
        return PositionEvaluation(
            evaluation_id=_evaluation_id(
                thesis_id=thesis.thesis_id,
                native_position_id=snapshot.native_position_id,
                evaluated_ns=now_ns,
                status=ThesisStatus.CLOSED,
                reasons=closed_reasons,
                proposals=(DecisionAction.WAIT,),
            ),
            thesis_id=thesis.thesis_id,
            native_position_id=snapshot.native_position_id,
            evaluated_ns=now_ns,
            status=ThesisStatus.CLOSED,
            reasons=closed_reasons,
            proposals=(DecisionAction.WAIT,),
            admission_ok=True,
        )
    if belief is None:
        return missing(ThesisReason.BELIEF_MISSING)
    if belief.as_of_ns > now_ns:
        return missing(ThesisReason.FUTURE_INPUT)
    if belief.stale:
        return missing(ThesisReason.BELIEF_STALE)
    if belief.abstain:
        return missing(ThesisReason.BELIEF_ABSTAIN)
    if snapshot.latest_belief_id is not None and snapshot.latest_belief_id != belief.belief_id:
        return missing(ThesisReason.BELIEF_MISSING)
    if any(
        item.stance is not StanceKind.ABSTAIN and not item.weight.is_finite()
        for item in evidence
    ):
        return missing(ThesisReason.EVIDENCE_INVALID)
    if len({item.expert_id for item in evidence}) != len(evidence):
        return missing(ThesisReason.EVIDENCE_INVALID)

    invalid: list[ThesisReason] = []
    weakening: list[ThesisReason] = []

    stop_rule = _rule(thesis, InvalidationKind.PRICE_STOP)
    if stop_rule is not None and snapshot.mark_price != ZERO:
        breached = (
            thesis.side == "LONG" and snapshot.mark_price <= thesis.initial_stop_price
        ) or (thesis.side == "SHORT" and snapshot.mark_price >= thesis.initial_stop_price)
        if breached:
            invalid.append(ThesisReason.STOP_BREACHED)

    regime_rule = _rule(thesis, InvalidationKind.REGIME_CHANGE)
    if regime_rule is not None and belief.regime != thesis.entry_regime:
        invalid.append(ThesisReason.REGIME_CHANGED)

    contradiction_rule = _rule(thesis, InvalidationKind.EXPERT_CONTRADICTION)
    if contradiction_rule is not None:
        support = sum(
            (item.weight for item in evidence if item.stance is StanceKind.SUPPORT), ZERO
        )
        contradict = sum(
            (item.weight for item in evidence if item.stance is StanceKind.CONTRADICT), ZERO
        )
        total = support + contradict
        if total > ZERO:
            ratio = contradict / total
            assert contradiction_rule.threshold is not None
            if ratio >= contradiction_rule.threshold:
                weakening.append(ThesisReason.EXPERT_CONTRADICTION)

    time_rule = _rule(thesis, InvalidationKind.TIME_LIMIT)
    if time_rule is not None and snapshot.elapsed_bars >= thesis.target_plan.max_bars:
        weakening.append(ThesisReason.TIME_LIMIT)

    protection_rule = _rule(thesis, InvalidationKind.PROTECTION_GAP)
    if protection_rule is not None and not snapshot.protective_order_ids:
        weakening.append(ThesisReason.PROTECTION_GAP)

    reasons: tuple[ThesisReason, ...]
    proposals: tuple[DecisionAction, ...]
    admission: bool
    if invalid:
        status = ThesisStatus.INVALIDATED
        reasons = tuple(sorted(set(invalid), key=lambda item: item.value))
        proposals = (DecisionAction.EXIT,)
        admission = False
    elif weakening:
        status = ThesisStatus.WEAKENING
        reasons = tuple(sorted(set(weakening), key=lambda item: item.value))
        if ThesisReason.PROTECTION_GAP in reasons:
            proposals = (DecisionAction.WAIT,)
            admission = False
        else:
            proposals = (DecisionAction.REDUCE,)
            admission = True
    else:
        status = ThesisStatus.HOLDING
        reasons = (ThesisReason.THESIS_INTACT,)
        proposals = (DecisionAction.HOLD,)
        admission = True

    return PositionEvaluation(
        evaluation_id=_evaluation_id(
            thesis_id=thesis.thesis_id,
            native_position_id=snapshot.native_position_id,
            evaluated_ns=now_ns,
            status=status,
            reasons=reasons,
            proposals=proposals,
        ),
        thesis_id=thesis.thesis_id,
        native_position_id=snapshot.native_position_id,
        evaluated_ns=now_ns,
        status=status,
        reasons=reasons,
        proposals=proposals,
        admission_ok=admission,
    )


class ThesisHistory(BaseModel):
    """Append-only evaluation history for one thesis; replay-identical."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    thesis_id: str = Field(min_length=1)
    evaluations: tuple[PositionEvaluation, ...] = ()

    def record(self, evaluation: PositionEvaluation) -> ThesisHistory:
        if evaluation.thesis_id != self.thesis_id:
            raise ValueError("evaluation belongs to a different thesis")
        if any(item.evaluation_id == evaluation.evaluation_id for item in self.evaluations):
            return self
        return ThesisHistory(
            thesis_id=self.thesis_id,
            evaluations=(*self.evaluations, evaluation),
        )

    @property
    def digest(self) -> str:
        return _digest(
            {
                "thesis_id": self.thesis_id,
                "evaluations": [item.evaluation_id for item in self.evaluations],
            }
        )
