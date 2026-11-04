"""Confirmed strength-five RSI divergence, with frozen price references."""

from dataclasses import dataclass, replace
from decimal import Decimal

from v8_next.domain.market import CausalFrame
from v8_next.economics.decisions import Opportunity, Stance
from v8_next.experts.common import context_reason, directional_stance
from v8_next.experts.features import close_series, wilder_rsi
from v8_next.experts.patterns import pattern_pivots


@dataclass(frozen=True)
class DivergenceSetup:
    direction: str
    barrier: Decimal
    extremum: Decimal
    first_pivot_ns: int
    second_pivot_ns: int
    pivot_confirmed_ns: int
    observed_ns: int


def divergence_setup(frame: CausalFrame, variant: str = "a") -> DivergenceSetup | None:
    if variant not in ("a", "b"):
        raise ValueError("unsupported divergence variant")
    if not frame.continuous:
        raise ValueError("source gap")
    bars = frame.candles
    if len(bars) < 21:
        return None
    threshold = sum((c.high - c.low for c in bars[-14:]), Decimal(0)) / 14
    if threshold <= 0:
        return None
    short = variant == "a"
    pivots = [
        i
        for i in pattern_pivots(frame, high=short, strength=5)
        if bars[i].high - bars[i].low >= threshold
    ]
    if len(pivots) < 2:
        return None
    first, second = pivots[-2:]
    rsi = wilder_rsi(close_series(frame))
    left, right = rsi[first], rsi[second]
    if left is None or right is None:
        return None
    between = bars[first + 1 : second]
    if not between:
        return None
    if short:
        if not (bars[second].high > bars[first].high and right < left):
            return None
        barrier = min(c.low for c in between)
        if bars[-1].close >= barrier:
            return None
        extreme = bars[second].high
    else:
        if not (bars[second].low < bars[first].low and right > left):
            return None
        barrier = max(c.high for c in between)
        if bars[-1].close <= barrier:
            return None
        extreme = bars[second].low
    return DivergenceSetup(
        "SHORT" if short else "LONG",
        barrier,
        extreme,
        bars[first].end_ns,
        bars[second].end_ns,
        bars[second + 5].end_ns,
        bars[-1].end_ns,
    )


def observe_divergence(
    frame: CausalFrame, opportunity: Opportunity | None, *, variant: str = "a"
) -> Stance:
    if variant not in ("a", "b"):
        raise ValueError("unsupported divergence variant")
    reason = context_reason(frame, opportunity, 21)
    setup = None
    if reason is None:
        setup = divergence_setup(frame, variant)
        reason = "CONFIRMED_RSI_DIVERGENCE" if setup else "NO_CONFIRMED_DIVERGENCE"
    return replace(
        directional_stance(
            frame,
            opportunity,
            family="divergence-12-setups",
            dependency="ohlcv-rsi-pivots-v1",
            mechanism="momentum-divergence",
            direction=setup.direction if setup else None,
            reason=reason,
        ),
        variant_id=variant,
    )
