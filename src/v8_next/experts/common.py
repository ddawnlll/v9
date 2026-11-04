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


#: Observer version strings are a pure function of the family and were rebuilt
#: on every one of the 583k ``directional_stance`` calls in a 3-month ten-leg
#: replay. The value is unchanged; only the formatting is hoisted.
_OBSERVER_VERSIONS: dict[str, str] = {}


def _observer_version(family: str) -> str:
    version = _OBSERVER_VERSIONS.get(family)
    if version is None:
        version = f"{family}-observer-v1"
        _OBSERVER_VERSIONS[family] = version
    return version


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
    # Positional construction: same fields, same order, no keyword binding.
    return Stance(
        family,
        dependency,
        kind,
        reason,
        opportunity.opportunity_id if opportunity else None,
        frame.decision_ns,
        family,
        mechanism,
        _observer_version(family),
        "a",
    )
