"""E-10 active v1 observation: close above the preceding 20-bar channel.

Legacy risk geometry belongs to a separate campaign policy. This observer has
no expected-return, sizing or order authority; ATR is not needed to establish
whether the price-channel observation is present.
"""

from v8_next.domain.market import CausalFrame
from v8_next.economics.decisions import Opportunity, Stance, StanceKind, numeric


def observe_donchian(frame: CausalFrame, opportunity: Opportunity | None) -> Stance:
    kind, reason = StanceKind.ABSTAIN, "NO_OPPORTUNITY"
    if opportunity is not None and (
        opportunity.instrument_id != frame.instrument_id
        or opportunity.anchor_ns > frame.decision_ns
        or opportunity.expires_ns <= frame.decision_ns
        or opportunity.direction not in {"LONG", "SHORT"}
    ):
        raise ValueError("opportunity outside observer domain")
    if not frame.continuous:
        reason = "SOURCE_GAP"
    elif len(frame.candles) < 21:
        reason = "WARMUP"
    elif opportunity is not None:
        channel_high_val = numeric(frame.df["high"].slice(-21, 20).max())
        if numeric(frame.df["close"][-1]) <= channel_high_val:
            reason = "NO_CHANNEL_BREAKOUT"
        else:
            kind = StanceKind.SUPPORT if opportunity.direction == "LONG" else StanceKind.CONTRADICT
            reason = "LONG_CHANNEL_BREAKOUT"
    return Stance(
        observer_id="donchian-breakout",
        dependency_group="ohlcv-channel-20-v1",
        kind=kind,
        reason=reason,
        opportunity_id=opportunity.opportunity_id if opportunity else None,
        decision_ns=frame.decision_ns,
        behavior_family="channel-breakout",
        mechanism_family="price-continuation-hypothesis",
        version="donchian-observer-v1",
        variant_id="a",
    )
