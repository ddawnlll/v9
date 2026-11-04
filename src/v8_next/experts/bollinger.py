"""Bollinger breakout a/b/c with band references frozen at setup-run start."""

from dataclasses import dataclass

from v8_next.domain.market import CausalFrame
from v8_next.economics.decisions import Opportunity, Stance, numeric, stance_with
from v8_next.experts.common import context_reason, directional_stance
from v8_next.experts.features import close_series


@dataclass(frozen=True)
class BandSetup:
    variant: str
    direction: str
    anchor_ns: int
    mid_reference: float
    sd_reference: float
    mean_range_reference: float
    stop_r: float
    target_r: float
    expiry_bars: int = 8


def band_setup(frame: CausalFrame, variant: str = "a") -> BandSetup | None:
    if variant not in {"a", "b", "c"}:
        raise ValueError("unsupported Bollinger variant")
    closes = close_series(frame)
    if len(closes) < (31 if variant == "c" else 20):
        return None
    mid, sd = closes.rolling_mean(20), closes.rolling_std(20, ddof=0)
    bandwidth = 4 * sd / mid
    # Population bands, with no imputed percentage for degenerate widths.
    pct = (closes - (mid - 2 * sd)) / (4 * sd)
    long = (closes > mid) & (pct >= 0.75) if variant == "a" else pct > 1
    short = (closes < mid) & (pct <= 0.25) if variant == "a" else pct < 0
    if variant == "c":
        squeeze = bandwidth.shift(1) < bandwidth.shift(2).rolling_min(10)
        long, short = long & squeeze, short & squeeze
    long = (long & (sd > 0)).fill_null(False)
    short = (short & (sd > 0)).fill_null(False)
    if long[-1]:
        direction, mask = "LONG", long
    elif short[-1]:
        direction, mask = "SHORT", short
    else:
        return None
    false_indices = (~mask).arg_true()
    anchor = int(false_indices[-1]) + 1 if len(false_indices) else 0
    ranges = frame.df["high"] - frame.df["low"]
    mean_range = numeric(ranges.slice(anchor - 13, 14).mean())
    anchor_sd = numeric(sd[anchor])
    if mean_range <= 0 or anchor_sd <= 0:
        return None
    distance = anchor_sd / mean_range * (1 if variant == "a" else 2)
    risk = min(2.0, max(0.8, distance))
    return BandSetup(
        variant,
        direction,
        frame.candles[anchor].end_ns,
        numeric(mid[anchor]),
        anchor_sd,
        mean_range,
        risk,
        risk,
    )


def observe_bollinger_breakout(
    frame: CausalFrame, opportunity: Opportunity | None, *, variant: str = "a"
) -> Stance:
    if variant not in {"a", "b", "c"}:
        raise ValueError("unsupported Bollinger variant")
    reason = context_reason(frame, opportunity, 31 if variant == "c" else 20)
    setup = None
    if reason is None:
        setup = band_setup(frame, variant)
        reason = "BAND_BREAKOUT" if setup else "NO_BAND_BREAKOUT"
    stance = directional_stance(
        frame,
        opportunity,
        family="bollinger-breakout",
        dependency="ohlcv-band-20-v1",
        mechanism="band-expansion-hypothesis",
        direction=setup.direction if setup else None,
        reason=reason,
    )
    return stance_with(stance, variant_id=variant)
