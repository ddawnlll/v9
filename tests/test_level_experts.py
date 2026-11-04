from dataclasses import replace
from decimal import Decimal

from v8_next.domain.market import Candle, CausalFrame
from v8_next.economics.decisions import Opportunity, StanceKind
from v8_next.experts.levels import (
    HOUR_NS,
    daily_pivots,
    observe_floor_pivot,
    observe_range_breakout,
)


def context(count):
    bars = tuple(
        Candle(
            "i",
            i * HOUR_NS,
            (i + 1) * HOUR_NS,
            Decimal(100),
            Decimal(101),
            Decimal(99),
            Decimal(100),
            Decimal(10),
            (i + 1) * HOUR_NS,
            (i + 1) * HOUR_NS,
            "fixture",
        )
        for i in range(count)
    )
    return CausalFrame("i", count * HOUR_NS, bars), Opportunity(
        "o", "e", "i", "LONG", count * HOUR_NS, (count + 10) * HOUR_NS
    )


def test_daily_pivot_uses_last_close_and_stays_fixed_within_session():
    frame, opportunity = context(26)
    bars = list(frame.candles)
    bars[23] = replace(bars[23], close=Decimal(101))
    bars[24] = replace(bars[24], open=Decimal("100.5"), close=Decimal(101))
    bars[25] = replace(bars[25], high=Decimal(120), open=Decimal("100.5"), close=Decimal(101))
    frame = replace(frame, candles=tuple(bars))
    levels = daily_pivots(frame)
    assert levels.pivot == Decimal(301) / 3
    assert (
        daily_pivots(replace(frame, candles=frame.candles[:-1], decision_ns=25 * HOUR_NS)) == levels
    )
    assert observe_floor_pivot(frame, opportunity).kind == StanceKind.SUPPORT
    from v8_next.economics.protection import protection_at

    protection = protection_at(frame, opportunity, "floor-pivot:a:v2", Decimal(".01"))
    assert protection is not None
    assert protection.stop_price == Decimal("100.34")
    assert protection.target_price == Decimal("101.66")
    assert protection.expires_ns == 34 * HOUR_NS
    mirrored = replace(
        frame,
        candles=tuple(
            replace(c, open=250 - c.open, close=250 - c.close, high=250 - c.low, low=250 - c.high)
            for c in frame.candles
        ),
    )
    short = protection_at(
        mirrored, replace(opportunity, direction="SHORT"), "floor-pivot:a:v2", Decimal(".01")
    )
    assert short is not None
    assert protection.close_invalidation_price == levels.pivot
    assert short.close_invalidation_price == daily_pivots(mirrored).pivot
    assert short.stop_price == 250 - protection.stop_price
    assert short.target_price == 250 - protection.target_price
    assert daily_pivots(replace(frame, candles=frame.candles[1:])) is None
    overshot = replace(bars[-1], close=Decimal(110), high=Decimal(120))
    assert (
        observe_floor_pivot(
            replace(frame, candles=(*frame.candles[:-1], overshot)), opportunity
        ).kind
        == StanceKind.ABSTAIN
    )


def test_range_requires_volume_and_fresh_breakout():
    frame, opportunity = context(100)
    frame = replace(
        frame,
        candles=tuple(
            replace(c, high=Decimal("100.5"), low=Decimal("99.5")) for c in frame.candles
        ),
    )
    breakout = replace(
        frame.candles[-1], close=Decimal(102), high=Decimal("102.2"), volume=Decimal(20)
    )
    frame = replace(frame, candles=(*frame.candles[:-1], breakout))
    assert observe_range_breakout(frame, opportunity).kind == StanceKind.SUPPORT
    from v8_next.economics.protection import protection_at

    protection = protection_at(frame, opportunity, "range-breakout:a:v2", Decimal(".01"))
    assert protection is not None
    assert protection.close_invalidation_price == Decimal("100.5")
    assert (protection.stop_price, protection.target_price) == (101, 103)
    mirrored = replace(
        frame,
        candles=tuple(
            replace(c, open=250 - c.open, close=250 - c.close, high=250 - c.low, low=250 - c.high)
            for c in frame.candles
        ),
    )
    short = protection_at(
        mirrored, replace(opportunity, direction="SHORT"), "range-breakout:a:v2", Decimal(".01")
    )
    assert short is not None and (short.stop_price, short.target_price) == (149, 147)
    assert short.close_invalidation_price == Decimal("149.5")
    # One prior-range height around observation close, not the prior low (99.5).
    flat = replace(breakout, volume=Decimal(10))
    assert (
        observe_range_breakout(
            replace(frame, candles=(*frame.candles[:-1], flat)), opportunity
        ).kind
        == StanceKind.ABSTAIN
    )
    next_bar = replace(
        breakout,
        start_ns=100 * HOUR_NS,
        end_ns=101 * HOUR_NS,
        available_ns=101 * HOUR_NS,
        received_ns=101 * HOUR_NS,
        close=Decimal(104),
        high=Decimal(105),
        volume=Decimal(30),
    )
    continued = replace(frame, candles=(*frame.candles, next_bar), decision_ns=101 * HOUR_NS)
    assert observe_range_breakout(continued, opportunity).reason == "NO_FRESH_BREAKOUT"
