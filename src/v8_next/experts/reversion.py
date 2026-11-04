"""Active v1/a Bollinger fade and RSI recovery expert observations."""

from dataclasses import dataclass
from decimal import Decimal

import polars as pl

from v8_next.domain.market import CausalFrame
from v8_next.economics.decisions import Opportunity, Stance, numeric
from v8_next.experts.common import context_reason, directional_stance
from v8_next.experts.features import close_series, wilder_rsi


def bollinger_direction(close: float, mid: float, sd: float) -> str | None:
    # The outer 3-sigma boundary is open; the inner 2-sigma boundary is closed.
    if sd <= 0:
        return None
    if mid + 2 * sd <= close < mid + 3 * sd:
        return "SHORT"
    if mid - 3 * sd < close <= mid - 2 * sd:
        return "LONG"
    return None


def observe_bollinger_reversion(frame: CausalFrame, opportunity: Opportunity | None) -> Stance:
    reason = context_reason(frame, opportunity, 20)
    direction = None
    if reason is None:
        closes = close_series(frame).tail(20)
        mid, sd = numeric(closes.mean()), numeric(closes.std(ddof=0))
        direction = bollinger_direction(float(closes[-1]), mid, sd)
        reason = "DEGENERATE_FEATURE" if sd <= 0 else "BAND_FADE" if direction else "NO_FADE_ZONE"
    return directional_stance(
        frame,
        opportunity,
        family="bollinger-reversion",
        dependency="ohlcv-band-20-v1",
        mechanism="band-mean-reversion-hypothesis",
        direction=direction,
        reason=reason,
    )


def observe_rsi_reversion(frame: CausalFrame, opportunity: Opportunity | None) -> Stance:
    reason = context_reason(frame, opportunity, 21)
    direction = None
    if reason is None:
        rsi = wilder_rsi(close_series(frame))
        reason = "NO_RECOVERY_TRIGGER"
        # LONG takes precedence as in the active Rust v1/a hypothesis.
        rsi_s = pl.Series("rsi", [v for v in rsi], dtype=pl.Float64)
        for side, threshold in (("LONG", 30), ("SHORT", 70)):
            rec_mask = ((rsi_s > threshold) if side == "LONG" else (rsi_s < threshold)).fill_null(
                False
            )
            if not rec_mask[-1]:
                continue
            false_indices = (~rec_mask).arg_true()
            start = int(false_indices[-1]) + 1 if len(false_indices) else 0
            if start == 0 or rsi[start - 1] is None:
                continue
            signal, latest = frame.candles[start], frame.candles[-1]
            if latest.close > signal.high if side == "LONG" else latest.close < signal.low:
                direction, reason = side, "RSI_RECOVERY_EXTREME_BREAK"
                break
    return directional_stance(
        frame,
        opportunity,
        family="rsi-stoch-reversion",
        dependency="ohlcv-rsi-14-v1",
        mechanism="oscillator-recovery-hypothesis",
        direction=direction,
        reason=reason,
    )


@dataclass(frozen=True)
class FadeGeometry:
    distance: Decimal
    invalidation_price: Decimal


def bollinger_fade_geometry(frame: CausalFrame) -> FadeGeometry | None:
    """Freeze clamped sigma/range geometry at the current fade run's first bar."""
    if len(frame.candles) < 20 or not frame.continuous:
        return None
    closes = close_series(frame)
    means = closes.rolling_mean(20)
    deviations = closes.rolling_std(20, ddof=0)
    # Vectorized direction masks (inner 2-sigma closed, outer 3-sigma open)
    short_mask = (
        (deviations > 0) & (closes >= means + 2 * deviations) & (closes < means + 3 * deviations)
    ).fill_null(False)
    long_mask = (
        (deviations > 0) & (closes > means - 3 * deviations) & (closes <= means - 2 * deviations)
    ).fill_null(False)

    if short_mask[-1]:
        side, mask = "SHORT", short_mask
    elif long_mask[-1]:
        side, mask = "LONG", long_mask
    else:
        return None

    false_indices = (~mask).arg_true()
    anchor = max(19, int(false_indices[-1]) + 1 if len(false_indices) else 19)
    ranges = frame.df["high"] - frame.df["low"]
    span = Decimal(str(round(numeric(ranges.slice(anchor - 13, 14).mean()), 8)))
    sigma = Decimal(str(numeric(deviations[anchor])))
    if span <= 0 or sigma <= 0:
        return None
    distance = min(Decimal(2) * span, max(Decimal(".8") * span, sigma))
    mid = Decimal(str(numeric(means[anchor])))
    return FadeGeometry(distance, mid + (3 if side == "SHORT" else -3) * sigma)


def bollinger_fade_distance(frame: CausalFrame) -> Decimal | None:
    geometry = bollinger_fade_geometry(frame)
    return geometry.distance if geometry else None
