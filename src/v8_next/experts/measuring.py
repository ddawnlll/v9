"""Pattern breakout objectives, distinct from retest admission."""

from dataclasses import dataclass
from decimal import Decimal

from v8_next.domain.market import CausalFrame
from v8_next.economics.decisions import Opportunity, Stance, numeric, stance_with
from v8_next.experts.common import context_reason, directional_stance
from v8_next.experts.patterns import pattern_pivots, pattern_structures

VARIANTS = ("head_shoulders", "double_top", "triangle")


@dataclass(frozen=True)
class MeasuringSetup:
    direction: str
    level: Decimal
    stop_reference: Decimal
    target_distance: Decimal
    completed_ns: int


def measuring_setup(frame: CausalFrame, variant: str = "head_shoulders") -> MeasuringSetup | None:
    if variant not in VARIANTS:
        raise ValueError("unsupported measuring variant")
    if not frame.continuous:
        raise ValueError("source gap")
    if len(frame.candles) < 21:
        return None
    bars = frame.candles
    current, previous = bars[-1], bars[-2]
    if variant != "triangle":
        for shape in pattern_structures(frame, "c" if variant == "head_shoulders" else "b"):
            short = shape.direction == "SHORT"
            beyond = current.close < shape.level if short else current.close > shape.level
            crossed = previous.close >= shape.level if short else previous.close <= shape.level
            if beyond and (crossed or len(bars) - 1 - shape.right_index <= 3):
                return MeasuringSetup(
                    shape.direction,
                    shape.level,
                    shape.extreme,
                    abs(shape.extreme - shape.level),
                    current.end_ns,
                )
        return None
    start = len(bars) - 21
    highs = [i for i in pattern_pivots(frame, high=True) if i >= start]
    lows = [i for i in pattern_pivots(frame, high=False) if i >= start]
    if len(highs) < 2 or len(lows) < 2:
        return None
    if not (bars[highs[0]].high > bars[highs[-1]].high and bars[lows[0]].low < bars[lows[-1]].low):
        return None
    high = Decimal(str(numeric(frame.df["high"].slice(-21, 20).max())))
    low = Decimal(str(numeric(frame.df["low"].slice(-21, 20).min())))
    height = high - low
    if height <= 0 or height / current.close > Decimal("0.03") or not low <= previous.close <= high:
        return None
    if current.close > high:
        return MeasuringSetup("LONG", high, low, height, current.end_ns)
    if current.close < low:
        return MeasuringSetup("SHORT", low, high, height, current.end_ns)
    return None


def observe_measuring(
    frame: CausalFrame, opportunity: Opportunity | None, *, variant: str = "head_shoulders"
) -> Stance:
    if variant not in VARIANTS:
        raise ValueError("unsupported measuring variant")
    reason = context_reason(frame, opportunity, 21)
    setup = None
    if reason is None:
        setup = measuring_setup(frame, variant)
        reason = "PATTERN_MEASURING_BREAKOUT" if setup else "NO_MEASURING_BREAKOUT"
    stance = directional_stance(
        frame,
        opportunity,
        family="pattern-measuring-objective",
        dependency="ohlcv-pattern-pivots-v1",
        mechanism="pattern-projection-hypothesis",
        direction=setup.direction if setup else None,
        reason=reason,
    )
    return stance_with(stance, variant_id=variant)
