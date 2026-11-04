"""Three-leg hypothesis; agreement is not independent statistical evidence."""

from decimal import Decimal

import polars as pl

from v8_next.domain.market import CausalFrame
from v8_next.economics.decisions import Opportunity, Stance, numeric, stance_with
from v8_next.experts.common import context_reason, directional_stance
from v8_next.experts.features import close_series, wilder_rsi
from v8_next.experts.fibonacci import fib_impulse
from v8_next.experts.reversion import bollinger_direction


def recovery_direction(values: tuple[float | None, ...]) -> str | None:
    if not values or values[-1] is None:
        return None
    s = pl.Series("rsi", [v for v in values], dtype=pl.Float64)
    for side, threshold in (("LONG", 30), ("SHORT", 70)):
        rec_mask = ((s > threshold) if side == "LONG" else (s < threshold)).fill_null(False)
        if not rec_mask[-1]:
            continue
        false_indices = (~rec_mask).arg_true()
        start = int(false_indices[-1]) + 1 if len(false_indices) else 0
        if start > 0 and values[start - 1] is not None:
            return side
    return None


def confluence_direction(
    votes: tuple[str | None, str | None, str | None], variant: str
) -> str | None:
    if variant not in {"a", "b"}:
        raise ValueError("unsupported confluence variant")
    if any(v not in {None, "LONG", "SHORT"} for v in votes):
        raise ValueError("invalid observation direction")
    for direction in ("LONG", "SHORT"):
        if votes.count(direction) >= (3 if variant == "a" else 2):
            return direction
    return None


def confluence_legs(frame: CausalFrame) -> tuple[str | None, str | None, str | None] | None:
    impulse = fib_impulse(frame)
    if impulse is None or len(frame.candles) < 21:
        return None
    closes = close_series(frame)
    bb = bollinger_direction(
        numeric(closes[-1]), numeric(closes.tail(20).mean()), numeric(closes.tail(20).std(ddof=0))
    )
    rsi = recovery_direction(wilder_rsi(closes))
    level = impulse.retracement(Decimal("0.786"))
    current = frame.candles[-1]
    reclaimed = (
        current.low <= level < current.close
        if impulse.direction == "LONG"
        else current.high >= level > current.close
    )
    return bb, rsi, impulse.direction if reclaimed else None


def observe_confluence(
    frame: CausalFrame, opportunity: Opportunity | None, *, variant: str = "a"
) -> Stance:
    if variant not in {"a", "b"}:
        raise ValueError("unsupported confluence variant")
    reason = context_reason(frame, opportunity, 21)
    direction = None
    if reason is None:
        legs = confluence_legs(frame)
        if legs is None:
            reason = "MISSING_CONFIRMED_IMPULSE"
        else:
            direction = confluence_direction(legs, variant)
            reason = "INDICATOR_CONFLUENCE" if direction else "NO_CONFLUENCE"
    stance = directional_stance(
        frame,
        opportunity,
        family="fib-rsi-bb-confluence",
        dependency="ohlcv-fib-rsi-bands-v1",
        mechanism="retracement-reversion-confluence-hypothesis",
        direction=direction,
        reason=reason,
    )
    return stance_with(stance, variant_id=variant)
