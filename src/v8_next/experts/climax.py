"""Active v2 volume-climax gates, with declared e/d/c/b/a precedence."""

from dataclasses import replace

from v8_next.domain.market import CausalFrame
from v8_next.economics.decisions import Opportunity, Stance, numeric
from v8_next.experts.common import context_reason, directional_stance
from v8_next.experts.features import trend_emas


def climax_hit(
    close: float,
    opening: float,
    fast: float,
    slow: float,
    z: float | None,
    proximity: float | None,
    reversal: bool,
) -> tuple[str, str] | None:
    if z is not None and z >= 3:
        if fast < slow:
            return "e", "LONG"
        if fast > slow:
            return "e", "SHORT"
    if z is not None and z >= 2 and reversal:
        if close > opening:
            return "d", "LONG"
        if close < opening:
            return "d", "SHORT"
    if proximity is not None and proximity < 0.4:
        if close < slow:
            return "c", "LONG"
        if close > slow:
            return "c", "SHORT"
    if z is not None and z >= 2:
        if fast > slow:
            return "b", "SHORT"
        if fast < slow:
            return "a", "LONG"
    return None


def observe_volume_climax(frame: CausalFrame, opportunity: Opportunity | None) -> Stance:
    reason = context_reason(frame, opportunity, 100)
    hit = None
    if reason is None:
        volumes = frame.df["volume"].tail(100)
        ranges = (frame.df["high"] - frame.df["low"]).tail(100)
        if not volumes.is_finite().all() or not ranges.is_finite().all():
            raise ValueError("non-finite native feature input")
        sd = numeric(volumes.std(ddof=0))
        lo, hi, current_volume = (
            numeric(volumes.min()),
            numeric(volumes.max()),
            numeric(volumes[-1]),
        )
        z = (current_volume - numeric(volumes.mean())) / sd if sd > 0 else None
        proximity = (current_volume - lo) / (hi - lo) if hi > lo else None
        reason = "MISSING_VOLUME_DISPERSION"
        if z is not None or proximity is not None:
            volume_rank = numeric((volumes <= current_volume).mean())
            range_rank = numeric((ranges <= numeric(ranges[-1])).mean())
            current = frame.candles[-1]
            rising = current.close > frame.candles[-6].close
            reversal = current.close < current.open if rising else current.close > current.open
            high_volume_reversal = reversal and (volume_rank >= 0.8 or range_rank >= 0.8)
            fast, slow = trend_emas(frame)
            hit = climax_hit(
                float(current.close),
                float(current.open),
                fast,
                slow,
                z,
                proximity,
                high_volume_reversal,
            )
            reason = "VOLUME_CLIMAX" if hit else "NO_CLIMAX"
    stance = directional_stance(
        frame,
        opportunity,
        family="volume-climax-reversal",
        dependency="ohlcv-volume-climax-v2",
        mechanism="volume-exhaustion-hypothesis",
        direction=hit[1] if hit else None,
        reason=reason,
    )
    return replace(
        stance, version="volume-climax-observer-v2", variant_id=hit[0] if hit else "UNRESOLVED"
    )
