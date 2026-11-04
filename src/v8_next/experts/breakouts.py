"""Failed and volume-confirmed breakout observations from active Rust v1."""

from decimal import Decimal

from v8_next.domain.market import CausalFrame
from v8_next.economics.decisions import Opportunity, Stance, numeric, stance_with
from v8_next.experts.common import context_reason, directional_stance


def last_close_breakout(frame: CausalFrame) -> tuple[int, Decimal] | None:
    """Latest close break and its frozen prior high in the supplied history."""
    if len(frame.candles) < 2:
        return None
    highs = frame.df["high"]
    closes = frame.df["close"]
    priors = highs.cum_max().shift(1)
    breaks = (closes > priors).fill_null(False).arg_true()
    if len(breaks) == 0:
        return None
    index = int(breaks[-1])
    level = Decimal(str(numeric(priors[index])))
    return index, level


def observe_failed_breakout(frame: CausalFrame, opportunity: Opportunity | None) -> Stance:
    reason = context_reason(frame, opportunity, 2)
    direction = None
    if reason is None:
        breakout = last_close_breakout(frame)
        reason = "NO_PRIOR_BREAKOUT"
        if breakout is not None:
            index, level = breakout
            if len(frame.candles) - 1 - index > 5:
                reason = "STALE_BREAKOUT"
            elif frame.candles[-1].close < level:
                direction, reason = "SHORT", "FAILED_CLOSE_BREAKOUT"
            else:
                reason = "NO_RETURN_BELOW_FROZEN_LEVEL"
    return directional_stance(
        frame,
        opportunity,
        family="failed-breakout",
        dependency="ohlcv-failed-breakout-v1",
        mechanism="breakout-trap-hypothesis",
        direction=direction,
        reason=reason,
    )


def volume_variant(
    volume: float, mean: float, z: float | None, proximity: float | None
) -> str | None:
    """Predeclared d/c/b/a priority; missing optional statistics stay absent."""
    if mean <= 0:
        return None
    if volume >= 2 * mean and z is not None and z < 2:
        return "d"
    if volume >= 1.2 * mean:
        return "c"
    if volume > mean and proximity is not None and proximity < 0.4:
        return "b"
    return "a" if volume > mean else None


def observe_volume_breakout(frame: CausalFrame, opportunity: Opportunity | None) -> Stance:
    reason = context_reason(frame, opportunity, 21)
    direction, variant = None, None
    if reason is None:
        current = frame.candles[-1]
        chan_high = Decimal(str(numeric(frame.df["high"].slice(-21, 20).max())))
        chan_low = Decimal(str(numeric(frame.df["low"].slice(-21, 20).min())))
        side = (
            "LONG" if current.close > chan_high else ("SHORT" if current.close < chan_low else None)
        )
        reason = "NO_CHANNEL_BREAKOUT"
        if side is not None:
            volumes = frame.df["volume"]
            if not volumes.is_finite().all():
                raise ValueError("volume outside finite float domain")
            mean = numeric(volumes.tail(20).mean())
            z, proximity = None, None
            if len(volumes) >= 100:
                window = volumes.tail(100)
                sd = numeric(window.std(ddof=0))
                lo, hi = numeric(window.min()), numeric(window.max())
                if sd > 0:
                    z = (numeric(volumes[-1]) - numeric(window.mean())) / sd
                if hi > lo:
                    proximity = (numeric(volumes[-1]) - lo) / (hi - lo)
            variant = volume_variant(numeric(volumes[-1]), mean, z, proximity)
            if variant is not None:
                direction, reason = side, "VOLUME_CONFIRMED_BREAKOUT"
            else:
                reason = "NO_VOLUME_CONFIRMATION"
    stance = directional_stance(
        frame,
        opportunity,
        family="volume-confirmed-breakout",
        dependency="ohlcv-volume-channel-v1",
        mechanism="participation-breakout-hypothesis",
        direction=direction,
        reason=reason,
    )
    return stance_with(stance, variant_id=variant or "UNRESOLVED")
