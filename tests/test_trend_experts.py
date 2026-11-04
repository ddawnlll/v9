from dataclasses import replace
from decimal import Decimal

import pytest

from v8_next.domain.market import Candle, CausalFrame
from v8_next.economics.decisions import Opportunity, StanceKind
from v8_next.experts.features import significant_swings, trend_emas
from v8_next.experts.trend import observe_trend_depth, observe_trend_pullback


def context(closes):
    candles = tuple(
        Candle(
            "i",
            n,
            n + 1,
            Decimal(p),
            Decimal(p) + 1,
            Decimal(p) - 1,
            Decimal(p),
            Decimal(10),
            n + 1,
            n + 1,
            "test",
        )
        for n, p in enumerate(closes)
    )
    return CausalFrame("i", len(candles), candles), Opportunity(
        "o", "e", "i", "LONG", len(candles), len(candles) + 10
    )


def test_ema_seed_matches_declared_recurrence():
    prices = list(range(100, 140)) + [128]
    frame, opportunity = context(prices)
    expected = []
    for period in (5, 20):
        ema = prices[0]
        alpha = 2 / (period + 1)
        for price in prices[1:]:
            ema = price * alpha + ema * (1 - alpha)
        expected.append(ema)
    assert trend_emas(frame) == pytest.approx(expected)
    assert observe_trend_pullback(frame, opportunity).kind == StanceKind.SUPPORT
    mirror, opposite = context([240 - p for p in prices])
    assert (
        observe_trend_pullback(mirror, replace(opposite, direction="SHORT")).kind
        == StanceKind.ABSTAIN
    )


def swing_context():
    frame, opportunity = context([100] * 25 + list(range(119, 131)))
    bars = list(frame.candles)
    bars[10] = replace(bars[10], low=Decimal(70))
    bars[25] = replace(bars[25], high=Decimal(150))
    return replace(frame, candles=tuple(bars)), opportunity


def test_pivot_requires_all_right_confirmation_bars_and_strict_extreme():
    frame, _ = swing_context()
    prefix = replace(frame, decision_ns=35, candles=frame.candles[:35])
    assert significant_swings(prefix)[0] is None
    confirmed = replace(frame, decision_ns=36, candles=frame.candles[:36])
    assert significant_swings(confirmed) == (25, 10)
    tied = list(frame.candles)
    tied[26] = replace(tied[26], high=Decimal(150))
    assert significant_swings(replace(frame, candles=tuple(tied)))[0] is None


def test_depth_uses_confirmed_pivots_not_rolling_extremes():
    frame, opportunity = swing_context()
    assert observe_trend_depth(frame, opportunity).kind == StanceKind.SUPPORT
    # Latest ten-bar extrema have no 150/70 impulse; confirmed older pivots do.
    assert max(c.high for c in frame.candles[-10:]) < Decimal(150)
    assert min(c.low for c in frame.candles[-10:]) > Decimal(70)
    short = replace(frame, candles=frame.candles[:20])
    assert observe_trend_depth(short, opportunity).reason == "WARMUP"
    flat, flat_opportunity = context([100] * 40)
    assert observe_trend_depth(flat, flat_opportunity).reason == "MISSING_CONFIRMED_SWING"


def test_active_trend_campaigns_freeze_one_range_stop_target_and_timeout():
    from v8_next.economics.protection import protection_at

    frame, opportunity = context(list(range(100, 140)) + [128])
    protection = protection_at(frame, opportunity, "trend-pullback:a:v2", Decimal(".01"))
    assert protection is not None
    assert protection.stop_price == Decimal(126)
    assert protection.target_price == Decimal(130)
    assert protection.expires_ns == frame.decision_ns + 8
    assert (
        protection_at(
            frame, replace(opportunity, direction="SHORT"), "trend-pullback:a:v2", Decimal(".01")
        )
        is None
    )
    frame, opportunity = swing_context()
    protection = protection_at(frame, opportunity, "trend-depth:a:v2", Decimal(".01"))
    assert protection is not None
    span = sum((c.high - c.low for c in frame.candles[-14:]), Decimal(0)) / 14
    assert 0 < frame.candles[-1].close - protection.stop_price <= span
    assert 0 < protection.target_price - frame.candles[-1].close <= span
    assert protection.stop_price != frame.candles[10].low  # Active v1 is not the v2 swing stop.
    flat, flat_opportunity = context([100] * 40)
    assert protection_at(flat, flat_opportunity, "trend-depth:a:v2", Decimal(".01")) is None
