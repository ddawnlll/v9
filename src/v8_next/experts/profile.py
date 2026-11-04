"""Prior-session TPO profile methodology over NumPy histogram arithmetic."""

from dataclasses import dataclass
from decimal import ROUND_CEILING, Decimal

import numpy as np

from v8_next.domain.market import Candle, CausalFrame
from v8_next.economics.decisions import Opportunity, Stance, stance_with
from v8_next.experts.common import context_reason, directional_stance
from v8_next.experts.levels import previous_session_bars


@dataclass(frozen=True)
class TpoProfile:
    poc: Decimal
    value_low: Decimal
    value_high: Decimal
    day_low: Decimal
    day_high: Decimal
    bucket: Decimal
    total: int
    covered: int
    above: int
    below: int


def tpo_profile(bars: tuple[Candle, ...], bucket: Decimal) -> TpoProfile | None:
    if not bucket.is_finite() or bucket <= 0:
        raise ValueError("positive finite profile bucket required")
    if not bars:
        return None
    bounds = [(int(c.low // bucket), int(c.high // bucket)) for c in bars]
    origin = min(a for a, _ in bounds)
    last = max(b for _, b in bounds)
    size = last - origin + 1
    if size > 100_000:
        raise ValueError("profile exceeds 100000 price buckets")
    # Each candle adds one TPO to every touched bucket. Difference arrays avoid
    # a Python loop over every price in every candle.
    delta = np.zeros(size + 1, dtype=np.int64)
    np.add.at(delta, [a - origin for a, _ in bounds], 1)
    np.add.at(delta, [b - origin + 1 for _, b in bounds], -1)
    counts = np.cumsum(delta[:-1])
    total = int(counts.sum())
    day_low, day_high = min(c.low for c in bars), max(c.high for c in bars)
    middle = int(((day_high + day_low) / 2) // bucket) - origin
    candidates = np.flatnonzero(counts == counts.max())
    poc = min((int(i) for i in candidates), key=lambda i: (abs(i - middle), i))
    target = int((Decimal(total) * Decimal("0.68")).to_integral_value(rounding=ROUND_CEILING))
    low = high = poc
    covered = int(counts[poc])
    while covered < target:
        left = int(counts[low - 1]) if low else 0
        right = int(counts[high + 1]) if high + 1 < size else 0
        if left == right == 0:
            return None  # Do not label a disconnected underfilled area as 68%.
        if right > left:
            high += 1
            covered += right
        else:
            low -= 1
            covered += left
    return TpoProfile(
        (origin + poc) * bucket,
        (origin + low) * bucket,
        (origin + high) * bucket,
        day_low,
        day_high,
        bucket,
        total,
        covered,
        int(counts[poc + 1 :].sum()),
        int(counts[:poc].sum()),
    )


def profile_direction(profile: TpoProfile, close: Decimal, variant: str) -> str | None:
    if variant not in {"a", "b", "c", "d"}:
        raise ValueError("unsupported profile variant")
    if not profile.day_low < close < profile.day_high:
        return None
    if variant == "a":
        return "LONG" if close < profile.poc else "SHORT" if close > profile.poc else None
    if variant == "b":
        return (
            "LONG" if close < profile.value_low else "SHORT" if close > profile.value_high else None
        )
    if variant == "c":
        tails = profile.above + profile.below
        if (
            tails
            and Decimal(profile.above) / tails >= Decimal("0.55")
            and close > profile.value_high
        ):
            return "LONG"
        if (
            tails
            and Decimal(profile.below) / tails >= Decimal("0.55")
            and close < profile.value_low
        ):
            return "SHORT"
        return None
    distance = profile.bucket / 2
    return (
        "LONG"
        if close < profile.poc - distance
        else "SHORT"
        if close > profile.poc + distance
        else None
    )


def observe_profile(
    frame: CausalFrame, opportunity: Opportunity | None, *, variant: str = "a"
) -> Stance:
    if variant not in {"a", "b", "c", "d"}:
        raise ValueError("unsupported profile variant")
    reason = context_reason(frame, opportunity, 25)
    direction = None
    if reason is None:
        prior = previous_session_bars(frame)
        bucket = sum((c.high - c.low for c in frame.candles[-14:]), Decimal(0)) / 14
        reason = "MISSING_COMPLETE_PREVIOUS_DAY"
        if prior is not None:
            profile = tpo_profile(prior, bucket) if bucket > 0 else None
            if profile is None:
                reason = "UNAVAILABLE_VALUE_AREA"
            else:
                direction = profile_direction(profile, frame.candles[-1].close, variant)
                reason = "PROFILE_REACTION" if direction else "NO_PROFILE_REACTION"
    stance = directional_stance(
        frame,
        opportunity,
        family="market-profile-value-area",
        dependency="ohlcv-utc-tpo-v2",
        mechanism="value-area-reaction-hypothesis",
        direction=direction,
        reason=reason,
    )
    return stance_with(stance, variant_id=variant, version="market-profile-complete-session-v2")
