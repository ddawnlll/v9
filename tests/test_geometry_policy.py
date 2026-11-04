from dataclasses import replace
from decimal import Decimal as D

from v8_next.domain.market import Candle, CausalFrame
from v8_next.economics.decisions import Opportunity
from v8_next.economics.protection import protection_at


def context(prices, direction="LONG"):
    bars = tuple(
        Candle(
            "i", i, i + 1, D(p), D(p) + D(".5"), D(p) - D(".5"), D(p), D(1), i + 1, i + 1, "fixture"
        )
        for i, p in enumerate(prices)
    )
    return CausalFrame("i", len(bars), bars), Opportunity(
        "o", "e", "i", direction, len(bars), len(bars) + 100
    )


def test_pandf_rounding_tightens_stop_and_target_and_never_falls_back():
    frame, opportunity = context([100] * 20 + [104, 100, 105])
    protection = protection_at(frame, opportunity, "pandf:a:v2", D(2))
    assert (protection.stop_price, protection.target_price) == (102, 112)
    assert protection_at(frame, opportunity, "pandf:c:v2", D(1)) is None
    assert protection_at(frame, replace(opportunity, direction="SHORT"), "pandf:a:v2", D(1)) is None
    assert protection_at(frame, opportunity, "pandf:a:v2", D(10)) is None


def test_bollinger_geometry_uses_frozen_range_and_eight_bar_expiry():
    frame, opportunity = context(list(range(100, 130)))
    protection = protection_at(frame, opportunity, "bollinger:a:v2", D(".01"))
    # Source clamps declared stop/target to 2R; bar range is 1 here.
    assert (protection.stop_price, protection.target_price) == (127, 131)
    assert protection.expires_ns == frame.decision_ns + 8


def test_measured_pattern_target_is_distance_from_observed_close():
    frame, opportunity = context([100] * 34, "SHORT")
    bars = list(frame.candles)
    for i in (5, 15, 25):
        bars[i] = replace(bars[i], high=D(120))
    bars[10] = replace(bars[10], low=D(90))
    bars[20] = replace(bars[20], low=D(95))
    bars[-1] = replace(bars[-1], open=D(90), close=D(90), high=D(91), low=D(89))
    protection = protection_at(
        replace(frame, candles=tuple(bars)), opportunity, "measuring:double_top:v2", D(1)
    )
    assert (protection.stop_price, protection.target_price) == (120, 65)


def test_donchian_exit_retains_unclamped_channel_low_not_generic_stop():
    frame, opportunity = context([100] * 20 + [110])
    protection = protection_at(frame, opportunity, "donchian:a:v2", D(".01"))
    assert (protection.stop_price, protection.target_price) == (D("99.5"), 111)
    assert (
        protection_at(frame, replace(opportunity, direction="SHORT"), "donchian:a:v2", D(".01"))
        is None
    )
