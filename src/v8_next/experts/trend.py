"""Active v1 long-only trend pullback and confirmed-swing depth observations."""

from decimal import Decimal

from v8_next.domain.market import CausalFrame
from v8_next.economics.decisions import Opportunity, Stance
from v8_next.experts.common import context_reason, directional_stance
from v8_next.experts.features import significant_swings, trend_emas


def observe_trend_pullback(frame: CausalFrame, opportunity: Opportunity | None) -> Stance:
    reason = context_reason(frame, opportunity, 20)
    direction = None
    if reason is None:
        fast, slow = trend_emas(frame)
        if fast > slow and float(frame.candles[-1].close) < slow:
            direction, reason = "LONG", "UPTREND_BELOW_SLOW_EMA"
        else:
            reason = "NO_PULLBACK"
    return directional_stance(
        frame,
        opportunity,
        family="trend-pullback",
        dependency="ohlcv-ema-5-20-v1",
        mechanism="trend-pullback-hypothesis",
        direction=direction,
        reason=reason,
    )


def observe_trend_depth(frame: CausalFrame, opportunity: Opportunity | None) -> Stance:
    reason = context_reason(frame, opportunity, 21)
    direction = None
    if reason is None:
        fast, slow = trend_emas(frame)
        high_index, low_index = significant_swings(frame)
        reason = "MISSING_CONFIRMED_SWING"
        if high_index is not None and low_index is not None:
            high, low = frame.candles[high_index].high, frame.candles[low_index].low
            close = frame.candles[-1].close
            if high <= low:
                reason = "INVALID_SWING_RANGE"
            elif fast > slow and high - Decimal("0.382") * (high - low) <= close < high:
                direction, reason = "LONG", "SHALLOW_TREND_PULLBACK"
            else:
                reason = "NO_SHALLOW_PULLBACK"
    return directional_stance(
        frame,
        opportunity,
        family="trend-pullback-depth",
        dependency="ohlcv-ema-swing-10-v1",
        mechanism="shallow-retracement-hypothesis",
        direction=direction,
        reason=reason,
    )
