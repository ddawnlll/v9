from dataclasses import replace
from decimal import Decimal

import pytest

from v8_next.domain.market import Candle, CausalFrame
from v8_next.economics.decisions import Opportunity, StanceKind
from v8_next.experts.momentum import observe_macd_stoch, observe_obv_adl, regime_hit


def context(prices):
    bars = tuple(
        Candle(
            "i",
            i,
            i + 1,
            Decimal(p) - 1,
            Decimal(p),
            Decimal(p) - 2,
            Decimal(p),
            Decimal(10),
            i + 1,
            i + 1,
            "fixture",
        )
        for i, p in enumerate(prices)
    )
    return CausalFrame("i", len(bars), bars), Opportunity(
        "o", "e", "i", "LONG", len(bars), len(bars) + 10
    )


@pytest.mark.parametrize(
    "close,fast,slow,cmf,net,expected",
    [
        (90, 95, 100, -0.2, -5, ("d", "LONG")),
        (95, 90, 100, 0.1, 0, ("c", "LONG")),
        (105, 110, 100, -0.1, 0, ("c", "SHORT")),
        (90, 95, 100, 0.1, 3, ("b", "LONG")),
        (110, 105, 100, -0.1, -3, ("b", "SHORT")),
        (110, 105, 100, 0.1, 3, ("a", "LONG")),
        (90, 95, 100, -0.1, -3, ("a", "SHORT")),
        (110, 105, 100, 0.1, 2, None),
        (90, 95, 100, -0.15, 0, None),
    ],
)
def test_regime_priority_boundaries(close, fast, slow, cmf, net, expected):
    assert regime_hit(close, fast, slow, cmf, net) == expected


def test_computed_cmf_and_close_count_support_but_missing_volume_does_not():
    frame, opportunity = context(range(100, 130))
    stance = observe_obv_adl(frame, opportunity)
    assert stance.kind == StanceKind.SUPPORT and stance.variant_id == "a"
    zero = replace(frame, candles=tuple(replace(c, volume=Decimal(0)) for c in frame.candles))
    assert observe_obv_adl(zero, opportunity).reason == "MISSING_VOLUME"


def test_macd_stochastic_recovery_and_mirror():
    frame, opportunity = context(list(range(100, 140)) + [137, 135, 140])
    assert observe_macd_stoch(frame, opportunity).kind == StanceKind.SUPPORT
    mirrored = replace(
        frame,
        candles=tuple(
            replace(c, open=250 - c.open, close=250 - c.close, high=250 - c.low, low=250 - c.high)
            for c in frame.candles
        ),
    )
    assert (
        observe_macd_stoch(mirrored, replace(opportunity, direction="SHORT")).kind
        == StanceKind.SUPPORT
    )
    flat, flat_opportunity = context([100] * 40)
    assert observe_macd_stoch(flat, flat_opportunity).kind == StanceKind.ABSTAIN
    warmup = replace(frame, candles=frame.candles[:33])
    assert observe_macd_stoch(warmup, opportunity).reason == "WARMUP"


@pytest.mark.parametrize(
    "prices,policy",
    [
        (list(range(100, 130)), "obv-adl:active:v2"),
        (list(range(100, 140)) + [137, 135, 140], "macd-stoch:active:v2"),
    ],
)
def test_momentum_protected_geometry_requires_real_setup(prices, policy):
    from v8_next.economics.protection import protection_at

    frame, opportunity = context(prices)
    protection = protection_at(frame, opportunity, policy, Decimal(".01"))
    assert protection is not None
    if policy == "obv-adl:active:v2":
        assert protection.close_invalidation_price == frame.candles[-1].low
    assert protection.stop_price == frame.candles[-1].close - 2
    assert protection.target_price == frame.candles[-1].close + 2
    assert protection.expires_ns == frame.decision_ns + 8
    flat, opportunity = context([100] * 40)
    assert protection_at(flat, opportunity, policy, Decimal(".01")) is None
