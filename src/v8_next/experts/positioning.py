"""Funding/positioning hypotheses; absent auxiliary data never becomes a signal."""

from decimal import Decimal

import polars as pl

from v8_next.domain.market import CausalFrame
from v8_next.domain.positioning import PositioningReading, positioning_at
from v8_next.economics.decisions import Opportunity, Stance, numeric, stance_with
from v8_next.experts.common import context_reason, directional_stance


def observe_funding(
    frame: CausalFrame,
    opportunity: Opportunity | None,
    *,
    variant: str = "a",
    readings: tuple[PositioningReading, ...] = (),
) -> Stance:
    if variant not in {"a", "b", "c", "d"}:
        raise ValueError("unsupported funding variant")
    reason = context_reason(frame, opportunity, 11)
    direction = None
    if reason is None:
        funding = positioning_at(
            readings, frame.instrument_id, "settled_funding_rate", frame.decision_ns
        )
        oi = (
            positioning_at(readings, frame.instrument_id, "open_interest", frame.decision_ns)
            if variant == "c"
            else None
        )
        reason = "MISSING_POSITIONING"
        if funding is not None and (variant != "c" or oi is not None):
            current = frame.candles[-1]
            prior = frame.candles[-11:-1] if variant == "d" else frame.candles[-6:-1]
            high, low = max(c.high for c in prior), min(c.low for c in prior)
            if funding >= Decimal("0.001"):
                if (variant in {"a", "c"} and current.close < low) or (
                    variant == "d" and current.close > high
                ):
                    direction = "SHORT"
            elif funding <= Decimal("-0.001"):
                if (variant in {"b", "c"} and current.close > high) or (
                    variant == "d" and current.close < low
                ):
                    direction = "LONG"
            reason = "FUNDING_CROWDING" if direction else "NO_FUNDING_CONFIRMATION"
    stance = directional_stance(
        frame,
        opportunity,
        family="funding-crowding-reversal",
        dependency="settled-funding-ohlcv-v1",
        mechanism="funding-crowding-hypothesis",
        direction=direction,
        reason=reason,
    )
    return stance_with(stance, variant_id=variant, version="funding-settled-causal-v2")


def observe_open_interest(
    frame: CausalFrame,
    opportunity: Opportunity | None,
    *,
    variant: str = "a",
    readings: tuple[PositioningReading, ...] = (),
) -> Stance:
    if variant not in {"a", "b", "c", "d"}:
        raise ValueError("unsupported positioning variant")
    reason = context_reason(frame, opportunity, 100)
    direction = None
    if reason is None:
        oi = positioning_at(readings, frame.instrument_id, "open_interest", frame.decision_ns)
        skew = positioning_at(readings, frame.instrument_id, "long_short_ratio", frame.decision_ns)
        reason = "MISSING_POSITIONING"
        if oi is not None and skew is not None:
            volumes = pl.Series([float(c.volume) for c in frame.candles[-100:]])
            if not volumes.is_finite().all():
                raise ValueError("non-finite volume")
            sd = numeric(volumes.std(ddof=0))
            reason = "MISSING_VOLUME_DISPERSION"
            if sd > 0:
                z = (numeric(volumes[-1]) - numeric(volumes.mean())) / sd
                price_up = frame.candles[-1].close > frame.candles[-6].close
                if variant == "a" and price_up and z > 0 and skew >= 1:
                    direction = "LONG"
                elif (variant == "b" and price_up and z < 0 and skew < 1) or (
                    variant == "c" and not price_up and z > 0 and skew >= 1
                ):
                    direction = "SHORT"
                elif variant == "d" and not price_up and z < 0 and skew < 1:
                    direction = "LONG"
                reason = "POSITIONING_VOLUME_PATTERN" if direction else "NO_POSITIONING_PATTERN"
    stance = directional_stance(
        frame,
        opportunity,
        family="open-interest-divergence",
        dependency="oi-skew-volume-v1",
        mechanism="positioning-volume-hypothesis",
        direction=direction,
        reason=reason,
    )
    return stance_with(stance, variant_id=variant)
