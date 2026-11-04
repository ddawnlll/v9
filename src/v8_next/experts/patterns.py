"""Confirmed structural pattern levels for retest variants b/c.

These are observation hypotheses, not calibrated economic forecasts.
"""

from dataclasses import dataclass
from decimal import Decimal

from v8_next.domain.market import CausalFrame


def pattern_pivots(frame: CausalFrame, *, high: bool, strength: int = 3) -> tuple[int, ...]:
    if strength < 1:
        raise ValueError("positive pivot strength required")
    if not frame.continuous:
        raise ValueError("source gap")
    values = frame.df["high"] if high else frame.df["low"]
    if not values.is_finite().all():
        raise ValueError("price outside finite float domain")
    roll = values.rolling_max(strength) if high else values.rolling_min(strength)
    left, right = roll.shift(1), roll.shift(-strength)
    mask = (values > left) & (values > right) if high else (values < left) & (values < right)
    return tuple(int(i) for i in mask.fill_null(False).arg_true())


def retests(frame: CausalFrame, direction: str, level: Decimal) -> bool:
    current = frame.candles[-1]
    recent_closes = frame.df["close"].slice(-7, 6)
    lvl = float(level)
    if direction == "LONG":
        return current.low <= level < current.close and bool((recent_closes > lvl).any())
    return current.high >= level > current.close and bool((recent_closes < lvl).any())


@dataclass(frozen=True)
class PatternStructure:
    direction: str
    level: Decimal
    extreme: Decimal
    right_index: int
    stop_reference: Decimal


def pattern_structures(frame: CausalFrame, variant: str) -> tuple[PatternStructure, ...]:
    if variant not in {"b", "c"}:
        raise ValueError("unsupported pattern retest variant")
    bars = frame.candles
    structures: list[PatternStructure] = []
    highs, lows = pattern_pivots(frame, high=True), pattern_pivots(frame, high=False)
    # Top before bottom matches the legacy pattern variants' precedence.
    for side, peaks, troughs in (("SHORT", highs, lows), ("LONG", lows, highs)):
        top = side == "SHORT"

        def price(i: int, top: bool = top) -> Decimal:
            return bars[i].high if top else bars[i].low

        if variant == "b":
            if len(peaks) < 2:
                continue
            left, right = peaks[-2:]
            between = bars[left + 1 : right]
            if not between:
                continue
            level = min(c.low for c in between) if top else max(c.high for c in between)
            valid = all(price(i) > level if top else price(i) < level for i in (left, right))
            extreme = max(price(left), price(right)) if top else min(price(left), price(right))
        else:
            if len(peaks) < 3 or len(troughs) < 2:
                continue
            select = max if top else min
            head = select(peaks, key=price)
            lefts, rights = [i for i in peaks if i < head], [i for i in peaks if i > head]
            if not lefts or not rights:
                continue
            left, right = select(lefts, key=price), select(rights, key=price)
            if not all(
                price(i) < price(head) if top else price(i) > price(head) for i in (left, right)
            ):
                continue
            left_levels = [bars[i].low if top else bars[i].high for i in troughs if left < i < head]
            right_levels = [
                bars[i].low if top else bars[i].high for i in troughs if head < i < right
            ]
            if not left_levels or not right_levels:
                continue
            level = (
                max(max(left_levels), max(right_levels))
                if top
                else min(min(left_levels), min(right_levels))
            )
            valid = level < price(head) if top else level > price(head)
            extreme = price(head)
        if valid:
            stop_reference = extreme if variant == "b" else price(right if top else left)
            structures.append(PatternStructure(side, level, extreme, right, stop_reference))
    return tuple(structures)


def pattern_retest_setup(frame: CausalFrame, variant: str) -> PatternStructure | None:
    closes = frame.df["close"]
    n_candles = len(frame.candles)
    for structure in pattern_structures(frame, variant):
        n_bars = n_candles - 1 - structure.right_index
        mid_closes = closes.slice(structure.right_index, n_bars)
        lvl = float(structure.level)
        breached = (
            bool((mid_closes < lvl).any())
            if structure.direction == "SHORT"
            else bool((mid_closes > lvl).any())
        )
        if breached and retests(frame, structure.direction, structure.level):
            return structure
    return None


def pattern_retest_direction(frame: CausalFrame, variant: str) -> str | None:
    setup = pattern_retest_setup(frame, variant)
    return setup.direction if setup is not None else None
