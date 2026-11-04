from dataclasses import replace
from decimal import Decimal

import pytest

from v8_next.domain.market import Candle, CausalFrame
from v8_next.economics.decisions import Opportunity, StanceKind
from v8_next.experts.bollinger import band_setup, observe_bollinger_breakout


def context(prices):
    bars = tuple(
        Candle(
            "i",
            i,
            i + 1,
            Decimal(str(p)),
            Decimal(str(p)) + 1,
            Decimal(str(p)) - 1,
            Decimal(str(p)),
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


def test_band_references_freeze_at_start_of_consecutive_setup():
    frame, opportunity = context(list(range(100, 140)))
    setup = band_setup(frame)
    first = band_setup(replace(frame, candles=frame.candles[:20], decision_ns=20))
    assert setup == first
    assert setup.anchor_ns == 20
    assert setup.mid_reference == pytest.approx(109.5)
    assert setup.mean_range_reference == 2
    assert observe_bollinger_breakout(frame, opportunity).kind == StanceKind.SUPPORT


@pytest.mark.parametrize("variant", ["a", "b", "c"])
def test_variants_and_mirrored_band_breaks(variant):
    prices = [100 + (-1) ** i * (30 - i) / 10 for i in range(30)] + [110]
    for values, side in ((prices, "LONG"), ([200 - p for p in prices], "SHORT")):
        frame, opportunity = context(values)
        stance = observe_bollinger_breakout(
            frame, replace(opportunity, direction=side), variant=variant
        )
        assert stance.kind == StanceKind.SUPPORT
        assert stance.variant_id == variant
        from v8_next.economics.protection import protection_at

        setup = band_setup(frame, variant)
        protection = protection_at(
            frame, replace(opportunity, direction=side), f"bollinger:{variant}:v2", Decimal(".01")
        )
        expected = Decimal(str(setup.mid_reference))
        if variant != "a":
            expected += (1 if side == "LONG" else -1) * 2 * Decimal(str(setup.sd_reference))
        assert protection.close_invalidation_price == expected


def test_c_requires_prior_fresh_bandwidth_low_not_just_breakout():
    frame, opportunity = context([100] * 30 + [110])
    assert observe_bollinger_breakout(frame, opportunity, variant="b").kind == StanceKind.SUPPORT
    assert observe_bollinger_breakout(frame, opportunity, variant="c").kind == StanceKind.ABSTAIN
    assert band_setup(replace(frame, candles=frame.candles[:-1]), "a") is None
    with pytest.raises(ValueError, match="unsupported Bollinger variant"):
        band_setup(frame, "unknown")
