"""Confirmed impulse geometry and active Fibonacci reclaim/rejection rules."""

from dataclasses import dataclass
from decimal import Decimal

from v8_next.domain.market import CausalFrame
from v8_next.economics.decisions import Opportunity, Stance
from v8_next.experts.common import context_reason, directional_stance
from v8_next.experts.patterns import pattern_pivots


@dataclass(frozen=True)
class FibImpulse:
    direction: str
    origin: Decimal
    extreme: Decimal
    origin_ns: int
    extreme_ns: int
    confirmed_ns: int

    def retracement(self, ratio: Decimal) -> Decimal:
        return self.extreme - (self.extreme - self.origin) * ratio

    def extension(self, ratio: Decimal) -> Decimal:
        return self.origin + (self.extreme - self.origin) * ratio


def fib_impulse(frame: CausalFrame) -> FibImpulse | None:
    if not frame.continuous:
        raise ValueError("source gap")
    if len(frame.candles) < 21:
        return None
    highs = pattern_pivots(frame, high=True, strength=10)
    lows = pattern_pivots(frame, high=False, strength=10)
    if not highs or not lows or highs[-1] == lows[-1]:
        return None
    high_index, low_index = highs[-1], lows[-1]
    high, low = frame.candles[high_index], frame.candles[low_index]
    if high.high <= low.low:
        return None
    if high_index > low_index:
        return FibImpulse(
            "LONG",
            low.low,
            high.high,
            low.end_ns,
            high.end_ns,
            frame.candles[high_index + 10].end_ns,
        )
    return FibImpulse(
        "SHORT", high.high, low.low, high.end_ns, low.end_ns, frame.candles[low_index + 10].end_ns
    )


def observe_fib_retracement(frame: CausalFrame, opportunity: Opportunity | None) -> Stance:
    reason = context_reason(frame, opportunity, 21)
    direction = None
    if reason is None:
        impulse = fib_impulse(frame)
        reason = "MISSING_CONFIRMED_IMPULSE"
        if impulse is not None:
            level = impulse.retracement(Decimal("0.382"))
            current = frame.candles[-1]
            hit = (
                current.low <= level < current.close
                if impulse.direction == "LONG"
                else current.high >= level > current.close
            )
            direction = impulse.direction if hit else None
            reason = "FIB_RETRACEMENT_RECLAIM" if hit else "NO_FIB_RECLAIM"
    return directional_stance(
        frame,
        opportunity,
        family="fib-retracement-continuation",
        dependency="ohlcv-fib-impulse-10-v1",
        mechanism="retracement-continuation-hypothesis",
        direction=direction,
        reason=reason,
    )


def observe_fib_projection(frame: CausalFrame, opportunity: Opportunity | None) -> Stance:
    reason = context_reason(frame, opportunity, 21)
    direction = None
    if reason is None:
        impulse = fib_impulse(frame)
        reason = "MISSING_CONFIRMED_IMPULSE"
        if impulse is not None:
            level = impulse.extension(Decimal("1.618"))
            current = frame.candles[-1]
            hit = (
                current.high >= level > current.close
                if impulse.direction == "LONG"
                else current.low <= level < current.close
            )
            if hit:
                direction = "SHORT" if impulse.direction == "LONG" else "LONG"
            reason = "FIB_EXTENSION_REJECTION" if hit else "NO_FIB_REJECTION"
    return directional_stance(
        frame,
        opportunity,
        family="fib-projection-reversal",
        dependency="ohlcv-fib-impulse-10-v1",
        mechanism="extension-reversal-hypothesis",
        direction=direction,
        reason=reason,
    )
