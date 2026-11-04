"""Gap-sequence observations over continuous bars; price gaps aren't data gaps."""

from dataclasses import dataclass
from decimal import Decimal

from v8_next.domain.market import Candle, CausalFrame
from v8_next.economics.decisions import Opportunity, Stance, numeric, stance_with
from v8_next.experts.common import context_reason, directional_stance


@dataclass(frozen=True)
class GapSetup:
    variant: str
    direction: str
    top: Decimal
    bottom: Decimal
    stop_reference: Decimal
    completed_ns: int
    same_direction_count: int


def gap_direction(previous: Candle, current: Candle) -> int:
    return 1 if current.open > previous.high else -1 if current.open < previous.low else 0


def gap_setup(frame: CausalFrame, variant: str = "a") -> GapSetup | None:
    if variant not in {"a", "b", "c"}:
        raise ValueError("unsupported gap variant")
    if not frame.continuous:
        raise ValueError("source gap")
    # Twenty gap transitions require twenty-one OHLC observations.
    if len(frame.candles) < 21:
        return None
    bars = frame.candles[-21:]
    current, previous = bars[-1], bars[-2]
    gap = gap_direction(previous, current)
    if gap == 0:
        return None
    count = sum(gap_direction(a, b) == gap for a, b in zip(bars, bars[1:], strict=False))
    top, bottom = (current.open, previous.high) if gap == 1 else (previous.low, current.open)
    if variant == "a":
        if count < 3 or not (
            current.close < current.open if gap == 1 else current.close > current.open
        ):
            return None
        direction = "SHORT" if gap == 1 else "LONG"
    else:
        if count != (1 if variant == "b" else 2):
            return None
        if not (current.close > top if gap == 1 else current.close < bottom):
            return None
        prev_high = Decimal(str(numeric(frame.df["high"].slice(-21, 20).max())))
        prev_low = Decimal(str(numeric(frame.df["low"].slice(-21, 20).min())))
        if variant == "b" and not (
            current.open > prev_high if gap == 1 else current.open < prev_low
        ):
            return None
        direction = "LONG" if gap == 1 else "SHORT"
    stop = bottom if direction == "LONG" else top
    return GapSetup(variant, direction, top, bottom, stop, current.end_ns, count)


def observe_gap(
    frame: CausalFrame, opportunity: Opportunity | None, *, variant: str = "a"
) -> Stance:
    if variant not in {"a", "b", "c"}:
        raise ValueError("unsupported gap variant")
    reason = context_reason(frame, opportunity, 21)
    setup = None
    if reason is None:
        setup = gap_setup(frame, variant)
        reason = "GAP_SEQUENCE" if setup else "NO_GAP_SEQUENCE"
    stance = directional_stance(
        frame,
        opportunity,
        family="gap-exhaustion",
        dependency="ohlcv-gap-sequence-v1",
        mechanism="gap-reaction-hypothesis",
        direction=setup.direction if setup else None,
        reason=reason,
    )
    return stance_with(stance, variant_id=variant)
