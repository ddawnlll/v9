from dataclasses import replace
from decimal import Decimal

import pytest

from v8_next.domain.market import Candle, CausalFrame
from v8_next.economics.decisions import Opportunity, StanceKind
from v8_next.experts.candlestick import candle_pattern, observe_candlestick

BULLISH = {
    "hammer": [(102, 103, 99, 100), (100, 101, 97, 101)],
    "bullish_engulfing": [(102, 103, 99, 100), (99, 104, 98, 103)],
    "bullish_harami": [(105, 106, 98, 99), (100, 103, 99, 102)],
    "three_white_soldiers": [
        (105, 106, 98, 99),
        (99, 102, 98, 101),
        (100, 104, 99, 103),
        (102, 106, 101, 105),
    ],
}
MIRRORS = {
    "hammer": "shooting_star",
    "bullish_engulfing": "bearish_engulfing",
    "bullish_harami": "bearish_harami",
    "three_white_soldiers": "three_black_crows",
}


def context(rows):
    bars = tuple(
        Candle("i", i, i + 1, *(Decimal(v) for v in row), Decimal(10), i + 1, i + 1, "fixture")
        for i, row in enumerate(rows)
    )
    return CausalFrame("i", len(bars), bars), Opportunity(
        "o", "e", "i", "LONG", len(bars), len(bars) + 10
    )


@pytest.mark.parametrize("variant", list(BULLISH))
def test_all_patterns_and_mirrors_preserve_direction_and_references(variant):
    frame, opportunity = context(BULLISH[variant])
    pattern = candle_pattern(frame, variant)
    assert pattern is not None and pattern.direction == "LONG"
    assert pattern.stop_reference < frame.candles[-1].close
    assert pattern.completed_ns == frame.candles[-1].end_ns
    assert observe_candlestick(frame, opportunity, variant=variant).kind == StanceKind.SUPPORT
    rows = [(200 - o, 200 - low, 200 - h, 200 - c) for o, h, low, c in BULLISH[variant]]
    mirror, short_opportunity = context(rows)
    mirrored = candle_pattern(mirror, MIRRORS[variant])
    assert mirrored is not None and mirrored.direction == "SHORT"
    assert mirrored.stop_reference > mirror.candles[-1].close
    assert (
        observe_candlestick(
            mirror, replace(short_opportunity, direction="SHORT"), variant=MIRRORS[variant]
        ).kind
        == StanceKind.SUPPORT
    )
    # Pattern completion is an observation, not permission to cross its trigger.
    assert not hasattr(pattern, "quantity")


def test_hammer_decline_context_and_zero_body():
    frame, _ = context(BULLISH["hammer"])
    up = replace(frame.candles[0], open=Decimal(100), close=Decimal(102))
    assert candle_pattern(replace(frame, candles=(up, frame.candles[1])), "hammer") is None
    doji = replace(frame.candles[-1], close=frame.candles[-1].open)
    assert candle_pattern(replace(frame, candles=(frame.candles[0], doji)), "hammer") is None


def test_soldiers_need_four_bars_and_second_high_break():
    frame, _ = context(BULLISH["three_white_soldiers"])
    assert candle_pattern(replace(frame, candles=frame.candles[1:]), "three_white_soldiers") is None
    equal = replace(frame.candles[-1], close=frame.candles[-2].high)
    assert (
        candle_pattern(replace(frame, candles=(*frame.candles[:-1], equal)), "three_white_soldiers")
        is None
    )


def test_auto_selection_and_explicit_variant_do_not_invent_a_hit():
    frame, opportunity = context(BULLISH["hammer"])
    assert candle_pattern(frame).variant == "hammer"
    assert (
        observe_candlestick(frame, opportunity, variant="shooting_star").kind == StanceKind.ABSTAIN
    )
    with pytest.raises(ValueError, match="unsupported candlestick variant"):
        observe_candlestick(frame, opportunity, variant="unknown")
    assert observe_candlestick(frame, None).kind == StanceKind.ABSTAIN


@pytest.mark.parametrize("variant", list(BULLISH))
@pytest.mark.parametrize("short", [False, True])
def test_pattern_campaign_uses_declared_clamp_and_one_range_target(variant, short):
    from v8_next.economics.protection import protection_at

    rows = [(100, 101, 99, 100)] * 14 + BULLISH[variant]
    if short:
        rows = [(200 - o, 200 - low, 200 - high, 200 - close) for o, high, low, close in rows]
        variant = MIRRORS[variant]
    frame, opportunity = context(rows)
    opportunity = replace(opportunity, direction="SHORT" if short else "LONG")
    sign = -1 if short else 1
    protection = protection_at(frame, opportunity, f"candlestick:{variant}:v2", Decimal(".01"))
    assert protection is not None
    span = sum((c.high - c.low for c in frame.candles[-14:]), Decimal(0)) / 14
    close = frame.candles[-1].close
    pattern = candle_pattern(frame, variant)
    assert protection.close_invalidation_price == pattern.trigger_reference
    declared_stop = min(
        2 * span, max(Decimal(".8") * span, (close - pattern.stop_reference) * sign)
    )
    assert 0 <= declared_stop - (close - protection.stop_price) * sign < Decimal(".01")
    assert 0 <= span - (protection.target_price - close) * sign < Decimal(".01")
    assert protection.expires_ns == frame.decision_ns + 8
    assert (
        protection_at(
            replace(frame, candles=frame.candles[-4:]),
            opportunity,
            f"candlestick:{variant}:v2",
            Decimal(".01"),
        )
        is None
    )
