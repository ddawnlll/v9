"""Active Tenkan/Kijun crossover hypothesis (not a displaced-cloud model)."""

from v8_next.domain.market import CausalFrame
from v8_next.economics.decisions import Opportunity, Stance, numeric, stance_with
from v8_next.experts.common import context_reason, directional_stance


def observe_ichimoku(frame: CausalFrame, opportunity: Opportunity | None) -> Stance:
    reason = context_reason(frame, opportunity, 27)
    direction = None
    if reason is None:
        highs = frame.df["high"]
        lows = frame.df["low"]
        if not highs.is_finite().all() or not lows.is_finite().all():
            raise ValueError("price outside finite float domain")
        tenkan = (highs.rolling_max(9) + lows.rolling_min(9)) / 2
        kijun = (highs.rolling_max(26) + lows.rolling_min(26)) / 2
        t, k, prev_t, prev_k = (numeric(v) for v in (tenkan[-1], kijun[-1], tenkan[-2], kijun[-2]))
        close = float(frame.candles[-1].close)
        if t > k and prev_t <= prev_k and close > k:
            direction, reason = "LONG", "TENKAN_KIJUN_CROSS"
        elif t < k and prev_t >= prev_k and close < k:
            direction, reason = "SHORT", "TENKAN_KIJUN_CROSS"
        else:
            reason = "NO_ALIGNED_CROSS"
    stance = directional_stance(
        frame,
        opportunity,
        family="ichimoku-cloud",
        dependency="ohlcv-midrange-9-26-v1",
        mechanism="midrange-cross-hypothesis",
        direction=direction,
        reason=reason,
    )
    return stance_with(stance, variant_id="v2")
