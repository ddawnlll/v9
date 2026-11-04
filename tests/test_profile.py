from dataclasses import replace
from decimal import Decimal

import pytest

from v8_next.domain.market import Candle, CausalFrame
from v8_next.economics.decisions import Opportunity, StanceKind
from v8_next.experts.levels import HOUR_NS
from v8_next.experts.profile import observe_profile, tpo_profile


def candle(i, low, high, close):
    return Candle(
        "i",
        i * HOUR_NS,
        (i + 1) * HOUR_NS,
        Decimal(str(close)),
        Decimal(str(high)),
        Decimal(str(low)),
        Decimal(str(close)),
        Decimal(10),
        (i + 1) * HOUR_NS,
        (i + 1) * HOUR_NS,
        "fixture",
    )


def test_tpo_counts_poc_and_value_area_exact_small_profile():
    prior = (
        candle(0, 90, 100, 100),
        candle(1, 100, 110, 100),
        *(candle(i, 100, 101, 100.5) for i in range(2, 24)),
    )
    profile = tpo_profile(prior, Decimal(1))
    assert profile.total == 66
    assert (profile.poc, profile.value_low, profile.value_high) == (100, 100, 101)
    assert profile.covered == 47
    assert (profile.above, profile.below) == (32, 10)


@pytest.mark.parametrize("variant,close", [("a", 99), ("b", 99), ("c", 102), ("d", 99)])
def test_complete_session_to_each_profile_variant(variant, close):
    prior = (
        candle(0, 90, 100, 100),
        candle(1, 100, 110, 100),
        *(candle(i, 100, 101, 100.5) for i in range(2, 24)),
    )
    frame = CausalFrame("i", 25 * HOUR_NS, (*prior, candle(24, close - 0.5, close + 0.5, close)))
    opportunity = Opportunity("o", "e", "i", "LONG", 25 * HOUR_NS, 26 * HOUR_NS)
    assert observe_profile(frame, opportunity, variant=variant).kind == StanceKind.SUPPORT
    from v8_next.economics.protection import protection_at

    protection = protection_at(frame, opportunity, f"profile:{variant}:v2", Decimal(".01"))
    assert protection is not None
    assert (protection.stop_price, protection.target_price) == (
        (100, 110) if variant == "c" else (90, 100)
    )
    assert protection.close_invalidation_price == (100 if variant == "c" else 90)
    assert protection.expires_ns == opportunity.expires_ns
    mirrored = replace(
        frame,
        candles=tuple(
            replace(c, open=200 - c.open, close=200 - c.close, high=200 - c.low, low=200 - c.high)
            for c in frame.candles
        ),
    )
    short = protection_at(
        mirrored, replace(opportunity, direction="SHORT"), f"profile:{variant}:v2", Decimal(".01")
    )
    assert short is not None
    assert short.close_invalidation_price == (100 if variant == "c" else 110)
    assert (short.stop_price, short.target_price) == ((100, 90) if variant == "c" else (110, 100))
    assert (
        observe_profile(
            replace(frame, candles=frame.candles[1:]), opportunity, variant=variant
        ).kind
        == StanceKind.ABSTAIN
    )


def test_poc_tie_lower_bucket_and_disconnected_undercoverage():
    tied = (candle(0, 99, 100, 99),)
    profile = tpo_profile(tied, Decimal(1))
    assert profile.poc == 99
    assert profile.covered == 2
    separated = (candle(0, 90, 90, 90), candle(1, 110, 110, 110))
    assert tpo_profile(separated, Decimal(1)) is None
    with pytest.raises(ValueError, match="positive finite"):
        tpo_profile(tied, Decimal(0))
