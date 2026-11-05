"""Versioned adaptive position lifecycle (RFC 1.4.1 §9, M3).

This module is the *policy* layer between a native position and the execution
adapter. It does not submit orders and it cannot move a stop by itself: it
produces a :class:`~v8_next.economics.thesis.PositionThesis` at entry, a
:class:`~v8_next.economics.management.ManagementDecision` on every reassessment,
and, when the target policy says so, a proposed target amendment. The adapter is
responsible for native protection coverage.

Two policies are versioned here and nothing else is hard-coded:

``STOP_POLICY_VERSION``
    Chooses the *initial* stop from recent structure, clamped between a minimum
    and maximum distance so a structural wick cannot silently multiply the risk
    budget. Falls back to the declared bracket percentage when the frame does not
    carry enough bars. The initial stop is the R denominator for the whole
    position; later amendments never change original-R accounting.

``TARGET_POLICY_VERSION``
    Sets the initial target at a fixed R multiple, bounded by a structural
    objective when one exists, and permits at most ``max_amendments`` extensions
    that must each add at least ``min_extension_r`` of reward beyond the current
    target and remain on the profitable side of the mark. A target is never
    tightened by this policy.

Both are *candidates*: the adapter enables them with a feature flag, the
simulation records the flag in its provenance, and no result here is an economic
claim.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Final, Sequence

from v8_next.domain.decision import DecisionAction
from v8_next.domain.market import Candle
from v8_next.domain.protection import ProtectionState
from v8_next.economics.management import ManagementDecision, evaluate_management
from v8_next.economics.thesis import (
    ExpertEvidence,
    InvalidationKind,
    InvalidationRule,
    PositionEvaluation,
    PositionSnapshot,
    PositionThesis,
    StopKind,
    TargetPlan,
    ThesisBelief,
    build_thesis,
    evaluate_position,
)

__all__ = [
    "LIFECYCLE_VERSION",
    "STOP_POLICY_VERSION",
    "TARGET_POLICY_VERSION",
    "InitialStop",
    "LifecycleAssessment",
    "StopPolicyV1",
    "TargetPolicyV1",
    "build_position_thesis",
    "structural_objective",
]

LIFECYCLE_VERSION: Final = "v9.position-lifecycle.v1"
STOP_POLICY_VERSION: Final = "v9.stop.structural.v1"
TARGET_POLICY_VERSION: Final = "v9.target.structural.v1"

_ZERO: Final = Decimal("0")


@dataclass(frozen=True, slots=True)
class InitialStop:
    price: Decimal
    kind: StopKind
    distance: Decimal
    basis: str


@dataclass(frozen=True, slots=True)
class StopPolicyV1:
    """Structural stop with hard distance bounds.

    ``lookback_bars`` is read on the *decision* horizon (H1 for this arm). The
    structural level is the lowest low (LONG) / highest high (SHORT) of the prior
    ``lookback_bars`` excluding the entry bar. The distance is clamped to
    ``[min_distance, max_distance]`` of entry price: a wick closer than
    ``min_distance`` is not a tradable invalidation, and a level beyond
    ``max_distance`` would breach the risk budget.
    """

    lookback_bars: int = 24
    min_distance: Decimal = Decimal("0.005")
    max_distance: Decimal = Decimal("0.03")
    fallback_pct: Decimal = Decimal("0.02")
    version: str = STOP_POLICY_VERSION

    def initial_stop(
        self, *, side: str, entry_price: Decimal, bars: Sequence[Candle]
    ) -> InitialStop:
        if side not in {"LONG", "SHORT"}:
            raise ValueError("side must be LONG or SHORT")
        if entry_price <= _ZERO:
            raise ValueError("entry price must be positive")
        history = tuple(bars)[-self.lookback_bars :]
        structural: Decimal | None = None
        if history:
            structural = (
                min(candle.low for candle in history)
                if side == "LONG"
                else max(candle.high for candle in history)
            )
        if structural is None:
            distance = entry_price * self.fallback_pct
            price = entry_price - distance if side == "LONG" else entry_price + distance
            return InitialStop(price, StopKind.NATIVE_PROTECTIVE, distance, "FALLBACK_BRACKET_PCT")
        distance = abs(entry_price - structural)
        floor = entry_price * self.min_distance
        ceiling = entry_price * self.max_distance
        clamped = min(max(distance, floor), ceiling)
        price = entry_price - clamped if side == "LONG" else entry_price + clamped
        basis = (
            "STRUCTURAL_LOOKBACK"
            if clamped == distance
            else "CLAMPED_MIN_DISTANCE"
            if distance < floor
            else "CLAMPED_MAX_DISTANCE"
        )
        return InitialStop(price, StopKind.STRUCTURAL, clamped, basis)


@dataclass(frozen=True, slots=True)
class TargetPolicyV1:
    """Structural target with a bounded number of extensions."""

    r_multiple: Decimal = Decimal("2")
    min_extension_r: Decimal = Decimal("0.5")
    max_amendments: int = 2
    expected_bars: int = 24
    max_bars: int = 96
    version: str = TARGET_POLICY_VERSION

    def initial_target(
        self,
        *,
        side: str,
        entry_price: Decimal,
        stop_price: Decimal,
        objective: Decimal | None = None,
    ) -> TargetPlan:
        risk = abs(entry_price - stop_price)
        if risk <= _ZERO:
            raise ValueError("stop must differ from entry")
        distance = risk * self.r_multiple
        price = entry_price + distance if side == "LONG" else entry_price - distance
        r_multiple = self.r_multiple
        if objective is not None:
            objective_r = (objective - entry_price) / risk
            if side == "SHORT":
                objective_r = (entry_price - objective) / risk
            if objective_r >= Decimal("1.5"):
                price = objective
                r_multiple = objective_r
        return TargetPlan(
            target_price=price,
            expected_bars=self.expected_bars,
            max_bars=self.max_bars,
            r_multiple=r_multiple,
        )

    def amendment(
        self,
        *,
        side: str,
        entry_price: Decimal,
        stop_price: Decimal,
        current_target: Decimal,
        mark_price: Decimal,
        objective: Decimal | None,
        amendments: int,
    ) -> Decimal | None:
        """A further target level, or ``None`` when no extension is justified.

        Extensions are one-way: the new level must be beyond the current target
        by at least ``min_extension_r`` and still ahead of the mark, and the
        policy stops after ``max_amendments``. No level is ever pulled closer.
        """
        if objective is None or amendments >= self.max_amendments:
            return None
        risk = abs(entry_price - stop_price)
        if risk <= _ZERO:
            return None
        minimum_step = risk * self.min_extension_r
        if side == "LONG":
            if objective <= current_target + minimum_step:
                return None
            if objective <= mark_price:
                return None
        else:
            if objective >= current_target - minimum_step:
                return None
            if objective >= mark_price:
                return None
        return objective


def structural_objective(
    side: str, bars: Sequence[Candle], lookback: int = 20
) -> Decimal | None:
    """Nearest prior swing extreme beyond the current range, or ``None``.

    For a LONG this is the highest high of the lookback window (a measured-move
    objective), for a SHORT the lowest low. It is a *reference*, never a promise:
    the caller decides whether extending a target to it is justified.
    """
    history = tuple(bars)[-lookback:]
    if not history:
        return None
    return max(c.high for c in history) if side == "LONG" else min(c.low for c in history)


def build_position_thesis(
    *,
    entry_decision_id: str,
    opportunity_id: str,
    belief: ThesisBelief,
    native_position_id: str,
    instrument_id: str,
    side: str,
    entry_price: Decimal,
    initial_stop: InitialStop,
    quantity: Decimal,
    target: TargetPlan,
    created_ns: int,
    policy_version: str = LIFECYCLE_VERSION,
) -> PositionThesis:
    """Freeze the entry rationale. No field of the returned record can be edited."""
    risk_usdt = abs(entry_price - initial_stop.price) * abs(quantity)
    if risk_usdt <= _ZERO:
        raise ValueError("original risk must be positive")
    return build_thesis(
        entry_decision_id=entry_decision_id,
        original_opportunity_id=opportunity_id,
        original_market_belief_id=belief.belief_id,
        native_position_id=native_position_id,
        instrument_id=instrument_id,
        side=side,
        entry_price=entry_price,
        initial_stop_price=initial_stop.price,
        initial_stop_kind=initial_stop.kind,
        original_risk_usdt=risk_usdt,
        target_plan=target,
        invalidation_rules=(
            InvalidationRule(kind=InvalidationKind.PRICE_STOP),
            InvalidationRule(
                kind=InvalidationKind.EXPERT_CONTRADICTION, threshold=Decimal("0.8")
            ),
            InvalidationRule(kind=InvalidationKind.PROTECTION_GAP),
            InvalidationRule(kind=InvalidationKind.TIME_LIMIT),
        ),
        entry_regime=belief.regime,
        policy_version=policy_version,
        created_ns=created_ns,
    )


@dataclass(frozen=True, slots=True)
class LifecycleAssessment:
    """One reassessment: thesis evaluation, management advisory, target proposal."""

    evaluation: PositionEvaluation
    management: ManagementDecision
    thesis: PositionThesis
    target_amendment: Decimal | None = None
    template_reasons: tuple[str, ...] = field(default_factory=tuple)

    @property
    def action(self) -> DecisionAction:
        return self.management.advisory.action

    def to_receipt(self) -> dict[str, object]:
        return {
            "lifecycle_version": LIFECYCLE_VERSION,
            "thesis_id": self.thesis.thesis_id,
            "evaluation_id": self.evaluation.evaluation_id,
            "management_id": self.management.management_id,
            "status": self.evaluation.status.value,
            "thesis_reasons": [reason.value for reason in self.evaluation.reasons],
            "action": self.action.value,
            "reason_codes": [code.value for code in self.management.advisory.reason_codes],
            "protection_state": self.management.protection_state.value,
            "protected": self.management.protected,
            "admission_ok": self.management.admission_ok,
            "proposal_ids": [p.proposal_id for p in self.management.proposals],
            "target_amendment": (
                None if self.target_amendment is None else str(self.target_amendment)
            ),
        }


def assess_position(
    *,
    thesis: PositionThesis,
    snapshot: PositionSnapshot,
    belief: ThesisBelief,
    evidence: tuple[ExpertEvidence, ...],
    protection_state: ProtectionState,
    protective_stop_price: Decimal | None,
    fee_bps: Decimal,
    lot_step: Decimal,
    tick_size: Decimal,
    target_policy: TargetPolicyV1,
    objective: Decimal | None,
    target_amendments: int,
    now_ns: int,
    venue_status: object,
    position_presence: object,
    stale: bool,
) -> LifecycleAssessment:
    """Evaluate the thesis and translate it into one management advisory."""
    evaluation = evaluate_position(
        thesis,
        snapshot,
        belief,
        now_ns=now_ns,
        evidence=evidence,
    )
    management = evaluate_management(
        evaluation,
        protection_state=protection_state,
        fee_bps=fee_bps,
        mark_price=snapshot.mark_price,
        side=thesis.side,
        entry_price=thesis.entry_price,
        quantity=snapshot.quantity,
        initial_stop_price=thesis.initial_stop_price,
        instrument_id=thesis.instrument_id,
        protective_stop_price=protective_stop_price,
        lot_step=lot_step,
        tick_size=tick_size,
        venue_status=venue_status,  # type: ignore[arg-type]
        position_presence=position_presence,  # type: ignore[arg-type]
        stale=stale,
    )
    amendment: Decimal | None = None
    if (
        management.protected
        and management.advisory.action is DecisionAction.HOLD
        and snapshot.quantity != _ZERO
    ):
        amendment = target_policy.amendment(
            side=thesis.side,
            entry_price=thesis.entry_price,
            stop_price=thesis.initial_stop_price,
            current_target=thesis.target_plan.target_price,
            mark_price=snapshot.mark_price,
            objective=objective,
            amendments=target_amendments,
        )
    return LifecycleAssessment(
        evaluation=evaluation,
        management=management,
        thesis=thesis,
        target_amendment=amendment,
        template_reasons=tuple(reason.value for reason in evaluation.reasons),
    )
