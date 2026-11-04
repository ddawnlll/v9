"""Six active 2B/false-move hypotheses with causal reference levels."""

from dataclasses import dataclass
from decimal import Decimal

from v8_next.domain.market import CausalFrame
from v8_next.economics.decisions import Opportunity, Stance, numeric, stance_with
from v8_next.experts.common import context_reason, directional_stance
from v8_next.experts.features import significant_swings

WARMUP = {"b": 21, "c": 4, "d": 4, "e": 2, "f": 28, "g": 22}


@dataclass(frozen=True)
class FailedMove:
    direction: str
    reference: Decimal
    variant: str
    completed_ns: int


def failed_move(frame: CausalFrame, variant: str = "b") -> FailedMove | None:
    if variant not in WARMUP:
        raise ValueError("unsupported failed-move variant")
    if not frame.continuous:
        raise ValueError("source gap")
    if len(frame.candles) < WARMUP[variant]:
        return None
    bars = frame.candles
    current, previous = bars[-1], bars[-2]
    hit: tuple[str, Decimal] | None = None
    if variant == "b":
        high_index, low_index = significant_swings(frame)
        if low_index is not None:
            level = bars[low_index].low
            if previous.close < level < current.close:
                hit = "LONG", level
        if hit is None and high_index is not None:
            level = bars[high_index].high
            if previous.close > level > current.close:
                hit = "SHORT", level
    elif variant in {"c", "d"}:
        bullish = variant == "c"
        # Only the three possible recent false moves can qualify; newest wins.
        for j in range(len(bars) - 3, max(0, len(bars) - 6), -1):
            inside, parent, false = bars[j], bars[j - 1], bars[j + 1]
            if not (
                inside.high <= parent.high and inside.low >= parent.low and inside.high > inside.low
            ):
                continue
            if bullish and false.close < inside.low and current.close > inside.high:
                hit = "LONG", inside.low
                break
            if not bullish and false.close > inside.high and current.close < inside.low:
                hit = "SHORT", inside.high
                break
    elif variant == "e":
        if current.open < previous.low < current.close:
            hit = "LONG", previous.low
        elif current.open > previous.high > current.close:
            hit = "SHORT", previous.high
    elif variant == "f":
        cloud_high = Decimal(str(numeric(frame.df["high"].slice(-28, 26).max())))
        cloud_low = Decimal(str(numeric(frame.df["low"].slice(-28, 26).min())))
        level = (cloud_high + cloud_low) / 2
        if previous.close > level > current.close:
            hit = "SHORT", level
    else:
        high = Decimal(str(numeric(frame.df["high"].slice(-22, 20).max())))
        low = Decimal(str(numeric(frame.df["low"].slice(-22, 20).min())))
        if high > low:
            if previous.close < low < current.close:
                hit = "LONG", low
            elif previous.close > high > current.close:
                hit = "SHORT", high
    return FailedMove(hit[0], hit[1], variant, current.end_ns) if hit else None


def observe_failed_move(
    frame: CausalFrame, opportunity: Opportunity | None, *, variant: str = "b"
) -> Stance:
    if variant not in WARMUP:
        raise ValueError("unsupported failed-move variant")
    reason = context_reason(frame, opportunity, WARMUP[variant])
    setup = None
    if reason is None:
        setup = failed_move(frame, variant)
        reason = "FAILED_MOVE_RECLAIM" if setup else "NO_FAILED_MOVE"
    stance = directional_stance(
        frame,
        opportunity,
        family="failed-breakout-2b",
        dependency="ohlcv-false-move-v1",
        mechanism="failed-move-reversal-hypothesis",
        direction=setup.direction if setup else None,
        reason=reason,
    )
    return stance_with(stance, variant_id=variant)
