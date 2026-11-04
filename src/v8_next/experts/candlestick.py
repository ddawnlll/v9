"""Eight declared candle observations and source-defined structural references."""

from dataclasses import dataclass
from decimal import Decimal

from v8_next.domain.market import Candle, CausalFrame
from v8_next.economics.decisions import Opportunity, Stance, stance_with
from v8_next.experts.common import context_reason, directional_stance

VARIANTS = (
    "hammer",
    "shooting_star",
    "bullish_engulfing",
    "bearish_engulfing",
    "bullish_harami",
    "bearish_harami",
    "three_white_soldiers",
    "three_black_crows",
)
LONG_VARIANTS = {"hammer", "bullish_engulfing", "bullish_harami", "three_white_soldiers"}


@dataclass(frozen=True)
class CandlePattern:
    variant: str
    direction: str
    stop_reference: Decimal
    trigger_reference: Decimal
    completed_ns: int


def matches(bars: tuple[Candle, ...], variant: str) -> bool:
    if variant not in VARIANTS:
        raise ValueError("unsupported candlestick variant")
    if len(bars) < 2:
        return False
    c, p = bars[-1], bars[-2]
    body = abs(c.close - c.open)
    upper = c.high - max(c.open, c.close)
    lower = min(c.open, c.close) - c.low
    if variant == "hammer":
        return (
            c.close > c.open
            and 3 * body <= c.high - c.low
            and lower >= 2 * body
            and upper <= body
            and p.close < p.open
        )
    if variant == "shooting_star":
        return (
            c.close < c.open
            and 3 * body <= c.high - c.low
            and upper >= 2 * body
            and lower <= body
            and p.close > p.open
        )
    if variant == "bullish_engulfing":
        return p.close < p.open and c.close > c.open and c.open <= p.close and c.close >= p.open
    if variant == "bearish_engulfing":
        return p.close > p.open and c.close < c.open and c.open >= p.close and c.close <= p.open
    if variant in {"bullish_harami", "bearish_harami"}:
        # Preserve the actual source inequalities, including their asymmetric
        # bearish interpretation; do not silently "correct" the hypothesis.
        nested = min(p.open, p.close) < c.open and c.close < max(p.open, p.close)
        shape = body > 0 and p.high > p.low and 3 * body <= p.high - p.low and nested
        return shape and (
            p.close < p.open and c.close > c.open
            if variant == "bullish_harami"
            else p.close > p.open and c.close < c.open
        )
    if len(bars) < 4:
        return False
    before, first, second, last = bars[-4:]
    if variant == "three_white_soldiers":
        return (
            before.close < before.open
            and first.close < second.close < last.close
            and last.close > second.high
            and all(b.close > b.open and b.high - b.close <= b.close - b.open for b in bars[-3:])
        )
    return (
        before.close > before.open
        and first.close > second.close > last.close
        and last.close < second.low
        and all(b.close < b.open and b.close - b.low <= b.open - b.close for b in bars[-3:])
    )


def candle_pattern(frame: CausalFrame, variant: str | None = None) -> CandlePattern | None:
    if variant is not None and variant not in VARIANTS:
        raise ValueError("unsupported candlestick variant")
    if not frame.continuous:
        raise ValueError("source gap")
    selected = next(
        (v for v in ((variant,) if variant else VARIANTS) if matches(frame.candles, v)), None
    )
    if selected is None:
        return None
    c, p = frame.candles[-1], frame.candles[-2]
    if selected in {"hammer", "bullish_engulfing"}:
        stop, trigger = c.low, c.high
    elif selected == "shooting_star":
        stop, trigger = c.high, p.low
    elif selected == "bearish_engulfing":
        stop, trigger = c.high, c.low
    elif selected == "bullish_harami":
        stop, trigger = p.low, p.high
    elif selected == "bearish_harami":
        stop, trigger = p.high, p.low
    elif selected == "three_white_soldiers":
        stop, trigger = frame.candles[-3].low, p.high
    else:
        stop, trigger = frame.candles[-3].high, p.low
    return CandlePattern(
        selected, "LONG" if selected in LONG_VARIANTS else "SHORT", stop, trigger, c.end_ns
    )


def observe_candlestick(
    frame: CausalFrame, opportunity: Opportunity | None, *, variant: str | None = None
) -> Stance:
    if variant is not None and variant not in VARIANTS:
        raise ValueError("unsupported candlestick variant")
    reason = context_reason(
        frame, opportunity, 4 if variant and variant.startswith("three_") else 2
    )
    pattern = None
    if reason is None:
        pattern = candle_pattern(frame, variant)
        reason = "CANDLE_PATTERN" if pattern else "NO_CANDLE_PATTERN"
    stance = directional_stance(
        frame,
        opportunity,
        family="candlestick-reversal",
        dependency="ohlcv-candle-reversal-v1",
        mechanism="candle-reversal-hypothesis",
        direction=pattern.direction if pattern else None,
        reason=reason,
    )
    return stance_with(stance, variant_id=pattern.variant if pattern else variant or "UNRESOLVED")
