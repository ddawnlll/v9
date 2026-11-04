"""Shared observer boundary; no confidence, allocation or execution policy."""

from v8_next.domain.market import CausalFrame
from v8_next.economics.decisions import Opportunity, Stance, StanceKind


def context_reason(frame: CausalFrame, opportunity: Opportunity | None, warmup: int) -> str | None:
    if opportunity is not None and (
        opportunity.instrument_id != frame.instrument_id
        or opportunity.anchor_ns > frame.decision_ns
        or opportunity.expires_ns <= frame.decision_ns
        or opportunity.direction not in {"LONG", "SHORT"}
    ):
        raise ValueError("opportunity outside observer domain")
    if not frame.continuous:
        return "SOURCE_GAP"
    if len(frame.candles) < warmup:
        return "WARMUP"
    return "NO_OPPORTUNITY" if opportunity is None else None


def directional_stance(
    frame: CausalFrame,
    opportunity: Opportunity | None,
    *,
    family: str,
    dependency: str,
    mechanism: str,
    direction: str | None,
    reason: str,
) -> Stance:
    if direction not in {None, "LONG", "SHORT"}:
        raise ValueError("invalid observation direction")
    kind = StanceKind.ABSTAIN
    if direction is not None and opportunity is not None:
        kind = StanceKind.SUPPORT if direction == opportunity.direction else StanceKind.CONTRADICT
    return Stance(
        observer_id=family,
        dependency_group=dependency,
        kind=kind,
        reason=reason,
        opportunity_id=opportunity.opportunity_id if opportunity else None,
        decision_ns=frame.decision_ns,
        behavior_family=family,
        mechanism_family=mechanism,
        version=f"{family}-observer-v1",
        variant_id="a",
    )
