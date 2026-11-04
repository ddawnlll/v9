from dataclasses import replace
from decimal import Decimal

import pytest

from v8_next.domain.market import Candle, CausalFrame
from v8_next.economics.decisions import Opportunity, StanceKind
from v8_next.experts.reclaim import observe_breakout_retest, observe_liquidity_reclaim


def context(closes):
    bars = tuple(
        Candle(
            "i",
            i,
            i + 1,
            Decimal(c),
            Decimal(c) + 1,
            Decimal(c) - 1,
            Decimal(c),
            Decimal(10),
            i + 1,
            i + 1,
            "fixture",
        )
        for i, c in enumerate(closes)
    )
    return CausalFrame("i", len(bars), bars), Opportunity(
        "o", "e", "i", "LONG", len(bars), len(bars) + 10
    )


def test_sweep_requires_strict_penetration_and_close_reclaim_long_first():
    frame, opportunity = context([100] * 20 + [100])
    both = replace(frame.candles[-1], low=Decimal(98), high=Decimal(102))
    assert (
        observe_liquidity_reclaim(
            replace(frame, candles=(*frame.candles[:-1], both)), opportunity
        ).kind
        == StanceKind.SUPPORT
    )
    high_only = replace(both, low=Decimal(99))
    assert (
        observe_liquidity_reclaim(
            replace(frame, candles=(*frame.candles[:-1], high_only)), opportunity
        ).kind
        == StanceKind.CONTRADICT
    )
    equal_close = replace(both, close=Decimal(99), high=Decimal(101))
    assert (
        observe_liquidity_reclaim(
            replace(frame, candles=(*frame.candles[:-1], equal_close)), opportunity
        ).kind
        == StanceKind.ABSTAIN
    )


@pytest.mark.parametrize("distance,expected", [(1, True), (6, True), (7, False)])
def test_retest_needs_prior_breach_within_six_bars(distance, expected):
    frame, opportunity = context([100] * 45)
    bars = list(frame.candles)
    bars[15] = replace(bars[15], high=Decimal(110))
    j = len(bars) - 1 - distance
    bars[j] = replace(
        bars[j], open=Decimal(112), close=Decimal(112), high=Decimal(113), low=Decimal(111)
    )
    bars[-1] = replace(
        bars[-1], open=Decimal(111), close=Decimal(111), high=Decimal(112), low=Decimal(110)
    )
    stance = observe_breakout_retest(replace(frame, candles=tuple(bars)), opportunity)
    assert (stance.kind == StanceKind.SUPPORT) is expected
    from v8_next.economics.protection import protection_at

    protection = protection_at(
        replace(frame, candles=tuple(bars)), opportunity, "breakout-retest:a:v2", Decimal(".01")
    )
    assert (protection is not None) is expected
    if protection is not None:
        assert protection.close_invalidation_price == 110
        assert protection.stop_price == 108
        assert protection.target_price == 113
    # A mirrored downside setup must produce a SHORT observation.
    mirrored = tuple(
        replace(c, open=200 - c.open, high=200 - c.low, low=200 - c.high, close=200 - c.close)
        for c in bars
    )
    short = observe_breakout_retest(
        replace(frame, candles=mirrored), replace(opportunity, direction="SHORT")
    )
    assert (short.kind == StanceKind.SUPPORT) is expected


def test_retest_current_breach_alone_is_insufficient():
    frame, opportunity = context([100] * 45)
    bars = list(frame.candles)
    bars[15] = replace(bars[15], high=Decimal(110))
    bars[-1] = replace(
        bars[-1], open=Decimal(111), close=Decimal(111), high=Decimal(112), low=Decimal(110)
    )
    assert (
        observe_breakout_retest(replace(frame, candles=tuple(bars)), opportunity).kind
        == StanceKind.ABSTAIN
    )


@pytest.mark.parametrize("variant", ["b", "c"])
def test_pattern_retests_and_mirrors(variant):
    frame, opportunity = context([100] * 34)
    bars = list(frame.candles)
    for i, high in ((5, 120), (15, 140 if variant == "c" else 120), (25, 120)):
        bars[i] = replace(bars[i], high=Decimal(high))
    bars[10] = replace(bars[10], low=Decimal(90))
    bars[20] = replace(bars[20], low=Decimal(95))
    bars[29] = replace(
        bars[29], open=Decimal(90), close=Decimal(90), low=Decimal(89), high=Decimal(91)
    )
    bars[-1] = replace(
        bars[-1], open=Decimal(90), close=Decimal(90), low=Decimal(89), high=Decimal(95)
    )
    result = observe_breakout_retest(
        replace(frame, candles=tuple(bars)),
        replace(opportunity, direction="SHORT"),
        variant=variant,
    )
    assert result.kind == StanceKind.SUPPORT
    assert result.variant_id == variant
    from v8_next.economics.protection import protection_at

    protection = protection_at(
        replace(frame, candles=tuple(bars)),
        replace(opportunity, direction="SHORT"),
        f"breakout-retest:{variant}:v2",
        Decimal(".01"),
    )
    assert protection is not None
    assert protection.close_invalidation_price == 95
    assert protection.target_price == (65 if variant == "b" else 45)
    assert protection.expires_ns == frame.decision_ns + 8
    mirror = tuple(
        replace(c, open=250 - c.open, close=250 - c.close, low=250 - c.high, high=250 - c.low)
        for c in bars
    )
    assert (
        observe_breakout_retest(replace(frame, candles=mirror), opportunity, variant=variant).kind
        == StanceKind.SUPPORT
    )
    bars[-1] = replace(bars[-1], high=Decimal(94))
    assert (
        observe_breakout_retest(
            replace(frame, candles=tuple(bars)), opportunity, variant=variant
        ).kind
        == StanceKind.ABSTAIN
    )


def test_unknown_retest_variant_rejected():
    frame, opportunity = context([100] * 30)
    with pytest.raises(ValueError, match="unsupported retest variant"):
        observe_breakout_retest(frame, opportunity, variant="unknown")


def test_sweep_campaign_stop_is_prior_level_not_sweep_extreme():
    from v8_next.economics.protection import protection_at

    frame, opportunity = context([100] * 21)
    bars = (*frame.candles[:-1], replace(frame.candles[-1], low=Decimal(95)))
    frame = replace(frame, candles=bars)
    protection = protection_at(frame, opportunity, "liquidity-reclaim:a:v2", Decimal(".01"))
    assert protection is not None and protection.stop_price == 99
    assert protection.close_invalidation_price == 99
    mirrored = replace(
        frame,
        candles=tuple(
            replace(c, open=200 - c.open, close=200 - c.close, high=200 - c.low, low=200 - c.high)
            for c in bars
        ),
    )
    short = protection_at(
        mirrored, replace(opportunity, direction="SHORT"), "liquidity-reclaim:a:v2", Decimal(".01")
    )
    assert short is not None and short.stop_price == 101
    assert short.close_invalidation_price == 101
