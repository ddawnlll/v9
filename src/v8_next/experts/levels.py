"""Session pivot reaction and volume-qualified narrow-range breakout."""

from dataclasses import dataclass
from decimal import Decimal

from v8_next.domain.market import Candle, CausalFrame
from v8_next.economics.decisions import Opportunity, Stance, numeric, stance_with
from v8_next.experts.common import context_reason, directional_stance

HOUR_NS = 3600 * 10**9
DAY_NS = 24 * HOUR_NS


@dataclass(frozen=True)
class PivotLevels:
    session_start_ns: int
    pivot: Decimal
    resistance1: Decimal
    support1: Decimal


def previous_session_bars(frame: CausalFrame) -> tuple[Candle, ...] | None:
    """Previous complete UTC day's H/L/final close, fixed throughout the session."""
    if not frame.continuous:
        raise ValueError("source gap")
    if not frame.candles:
        return None
    session = frame.candles[-1].start_ns // DAY_NS * DAY_NS
    previous = tuple(
        c for c in frame.candles if session - DAY_NS <= c.start_ns and c.end_ns <= session
    )
    if (
        len(previous) != 24
        or previous[0].start_ns != session - DAY_NS
        or previous[-1].end_ns != session
        or any(c.end_ns - c.start_ns != HOUR_NS for c in previous)
    ):
        return None
    return previous


def daily_pivots(frame: CausalFrame) -> PivotLevels | None:
    if not frame.continuous:
        raise ValueError("source gap")
    df = frame.df
    if len(df) == 0:
        return None
    session = int(df["start_ns"][-1]) // DAY_NS * DAY_NS
    prev_mask = (df["start_ns"] >= session - DAY_NS) & (df["end_ns"] <= session)
    prev_indices = prev_mask.arg_true()
    if len(prev_indices) != 24:
        return None
    start_idx, end_idx = int(prev_indices[0]), int(prev_indices[-1])
    if int(df["start_ns"][start_idx]) != session - DAY_NS or int(df["end_ns"][end_idx]) != session:
        return None
    durations = df["end_ns"].slice(start_idx, 24) - df["start_ns"].slice(start_idx, 24)
    if not (durations == HOUR_NS).all():
        return None
    high = Decimal(str(numeric(df["high"].slice(start_idx, 24).max())))
    low = Decimal(str(numeric(df["low"].slice(start_idx, 24).min())))
    close = Decimal(str(numeric(df["close"][end_idx])))
    pivot = (high + low + close) / 3
    end_ns = int(df["end_ns"][end_idx])
    return PivotLevels(end_ns, pivot, 2 * pivot - low, 2 * pivot - high)


def observe_floor_pivot(frame: CausalFrame, opportunity: Opportunity | None) -> Stance:
    reason = context_reason(frame, opportunity, 25)
    direction = None
    if reason is None:
        levels = daily_pivots(frame)
        reason = "MISSING_COMPLETE_PREVIOUS_DAY"
        if levels is not None:
            current = frame.candles[-1]
            if levels.pivot < current.open < current.close < levels.resistance1:
                direction, reason = "LONG", "PIVOT_DRIFT"
            elif levels.support1 < current.close < current.open < levels.pivot:
                direction, reason = "SHORT", "PIVOT_DRIFT"
            else:
                reason = "NO_POSITIVE_PIVOT_TARGET"
    stance = directional_stance(
        frame,
        opportunity,
        family="floor-trader-pivot",
        dependency="ohlcv-utc-day-pivot-v2",
        mechanism="session-pivot-drift-hypothesis",
        direction=direction,
        reason=reason,
    )
    return stance_with(stance, version="floor-trader-pivot-utc-session-v2")


def observe_range_breakout(frame: CausalFrame, opportunity: Opportunity | None) -> Stance:
    reason = context_reason(frame, opportunity, 100)
    direction = None
    if reason is None:
        current = frame.candles[-1]
        high = Decimal(str(numeric(frame.df["high"].slice(-21, 20).max())))
        low = Decimal(str(numeric(frame.df["low"].slice(-21, 20).min())))
        volumes = frame.df["volume"].tail(100)
        if not volumes.is_finite().all():
            raise ValueError("volume outside finite float domain")
        sd = numeric(volumes.std(ddof=0))
        reason = "NO_NARROW_RANGE"
        if high > low and (high - low) / current.close <= Decimal("0.03"):
            if sd <= 0:
                reason = "MISSING_VOLUME_DISPERSION"
            elif (numeric(volumes[-1]) - numeric(volumes.mean())) / sd < 0.20:
                reason = "NO_VOLUME_EXPANSION"
            else:
                side = "LONG" if current.close > high else "SHORT" if current.close < low else None
                previous = frame.candles[-2]
                prec_high = Decimal(str(numeric(frame.df["high"].slice(-22, 20).max())))
                prec_low = Decimal(str(numeric(frame.df["low"].slice(-22, 20).min())))
                prior_broke = (
                    previous.close > prec_high if side == "LONG" else previous.close < prec_low
                )
                if side and not prior_broke:
                    direction, reason = side, "FRESH_NARROW_RANGE_BREAKOUT"
                else:
                    reason = "NO_FRESH_BREAKOUT"
    return directional_stance(
        frame,
        opportunity,
        family="range-breakout-1to1",
        dependency="ohlcv-volume-channel-v1",
        mechanism="range-measuring-objective-hypothesis",
        direction=direction,
        reason=reason,
    )
