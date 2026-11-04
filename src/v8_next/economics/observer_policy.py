"""Explicit experiment selection; observer choice never supplies calibration."""

from v8_next.domain.market import CausalFrame
from v8_next.domain.positioning import PositioningReading
from v8_next.economics.decisions import (
    Opportunity,
    Stance,
    observe_breakout_baseline,
    observe_squeeze,
)
from v8_next.experts.catalog import observe_all

# Family identifiers are stable experiment inputs, not a claim that variants
# within a family are independent evidence. Reconciliation retains dependency groups.
FAMILIES = frozenset(
    {
        "squeeze-swing",
        "divergence-12-setups",
        "pandf-breakout",
        "funding-crowding-reversal",
        "open-interest-divergence",
        "pattern-measuring-objective",
        "failed-breakout-2b",
        "market-profile-value-area",
        "volume-climax-reversal",
        "fib-rsi-bb-confluence",
        "fib-retracement-continuation",
        "fib-projection-reversal",
        "obv-adl-regime",
        "macd-stoch-trend",
        "floor-trader-pivot",
        "range-breakout-1to1",
        "ichimoku-cloud",
        "gap-exhaustion",
        "bollinger-breakout",
        "candlestick-reversal",
        "donchian-breakout",
        "bollinger-reversion",
        "rsi-stoch-reversion",
        "failed-breakout",
        "volume-confirmed-breakout",
        "trend-pullback",
        "trend-pullback-depth",
        "liquidity-sweep-reclaim",
        "breakout-retest",
    }
)


def validate_observer_policy(policy: str) -> str:
    if policy in {"squeeze", "squeeze:m1", "squeeze:m2", "squeeze:m3", "breakout_baseline"}:
        return policy
    if not policy.startswith("families:"):
        raise ValueError("unknown observer policy")
    families = policy.removeprefix("families:").split(",")
    if not set(families) <= FAMILIES or len(set(families)) != len(families):
        raise ValueError("unknown or duplicate observer family")
    if families != sorted(families):
        raise ValueError("observer families must use canonical sorted order")
    return policy


def policy_stances(
    frame: CausalFrame,
    opportunity: Opportunity | None,
    policy: str,
    *,
    readings: tuple[PositioningReading, ...] = (),
) -> tuple[Stance, ...]:
    validate_observer_policy(policy)
    if policy == "squeeze":
        return (observe_squeeze(frame, opportunity),)
    if policy.startswith("squeeze:"):
        return (observe_squeeze(frame, opportunity, variant=policy.split(":")[1]),)
    if policy == "breakout_baseline":
        return (observe_breakout_baseline(frame, opportunity),)
    families = set(policy.removeprefix("families:").split(","))
    return tuple(
        s for s in observe_all(frame, opportunity, readings=readings) if s.observer_id in families
    )
