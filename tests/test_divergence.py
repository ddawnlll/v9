from dataclasses import replace
from decimal import Decimal

import pytest

from v8_next.domain.market import Candle, CausalFrame
from v8_next.economics.decisions import Opportunity, StanceKind
from v8_next.experts.divergence import divergence_setup, observe_divergence


def fixture(mirror=False):
    closes = list(range(100, 121)) + list(range(119, 99, -1))
    bars = []
    for i, close in enumerate(closes):
        high, low = close + 1, close - 1
        if i == 20:
            high = 130
        if i == 32:
            high = 140
        if mirror:
            close, high, low = 300 - close, 300 - low, 300 - high
        bars.append(
            Candle(
                "i",
                i,
                i + 1,
                Decimal(close),
                Decimal(high),
                Decimal(low),
                Decimal(close),
                Decimal(10),
                i + 1,
                i + 1,
                "fixture",
            )
        )
    return CausalFrame("i", len(bars), tuple(bars))


@pytest.mark.parametrize("variant,mirror,direction", [("a", False, "SHORT"), ("b", True, "LONG")])
def test_actual_rsi_divergence_and_confirmed_geometry(variant, mirror, direction):
    frame = fixture(mirror)
    setup = divergence_setup(frame, variant)
    assert setup is not None
    assert setup.direction == direction
    assert (setup.first_pivot_ns, setup.second_pivot_ns, setup.pivot_confirmed_ns) == (21, 33, 38)
    assert setup.barrier == (108 if not mirror else 192)
    opportunity = Opportunity("o", "e", "i", direction, 41, 50)
    assert observe_divergence(frame, opportunity, variant=variant).kind == StanceKind.SUPPORT
    from v8_next.economics.protection import protection_at

    protection = protection_at(frame, opportunity, f"divergence:{variant}:v2", Decimal(".01"))
    assert protection is not None
    span = sum((c.high - c.low for c in frame.candles[-14:]), Decimal(0)) / 14
    assert 0 <= span - abs(frame.candles[-1].close - protection.stop_price) < Decimal(".01")
    assert 0 <= span - abs(protection.target_price - frame.candles[-1].close) < Decimal(".01")
    expected = (
        max(setup.barrier, setup.extremum)
        if direction == "LONG"
        else min(setup.barrier, setup.extremum)
    )
    assert protection.close_invalidation_price == expected
    from v8_next.domain.campaign import PaperCampaign

    campaign = PaperCampaign(
        "c",
        "o",
        frame.instrument_id,
        direction,
        Decimal(1),
        0,
        1000,
        close_invalidation_price=expected,
    )
    for close in (setup.barrier, setup.extremum, frame.candles[-1].close):
        last = replace(
            frame.candles[-1],
            close=close,
            high=max(close, frame.candles[-1].high),
            low=min(close, frame.candles[-1].low),
        )
        current = replace(frame, candles=(*frame.candles[:-1], last))
        violated = (
            (close <= setup.barrier or close <= setup.extremum)
            if direction == "LONG"
            else (close >= setup.barrier or close >= setup.extremum)
        )
        assert campaign.invalidated_by_close(current) is violated
    assert protection.stop_price != setup.extremum
    assert protection.expires_ns == 49
    # A price break cannot authorize a pivot before its right flank exists.
    early = replace(frame, decision_ns=37, candles=frame.candles[:37])
    assert divergence_setup(early, variant) is None
    # Equality at the barrier is not a close-through confirmation.
    bars = list(frame.candles)
    bars[-1] = replace(
        bars[-1],
        close=setup.barrier,
        high=max(bars[-1].high, setup.barrier),
        low=min(bars[-1].low, setup.barrier),
    )
    assert divergence_setup(replace(frame, candles=tuple(bars)), variant) is None


def test_higher_price_without_lower_rsi_is_not_divergence():
    frame = fixture()
    bars = tuple(
        replace(c, open=Decimal(100), close=Decimal(100), low=min(c.low, Decimal(99)))
        for c in frame.candles
    )
    assert divergence_setup(replace(frame, candles=bars)) is None
