from dataclasses import replace
from decimal import Decimal

import pytest

from v8_next.domain.market import Candle, CausalFrame
from v8_next.domain.positioning import PositioningReading, positioning_at
from v8_next.economics.decisions import Opportunity, StanceKind
from v8_next.experts.positioning import observe_funding, observe_open_interest


def reading(metric, value):
    return PositioningReading("i", metric, Decimal(str(value)), 100, 100, 100, 110, "test-source")


def context(close=102, volume=20):
    bars = tuple(
        Candle(
            "i",
            i,
            i + 1,
            Decimal(100),
            Decimal(101),
            Decimal(99),
            Decimal(100),
            Decimal(10),
            i + 1,
            i + 1,
            "fixture",
        )
        for i in range(100)
    )
    last = replace(
        bars[-1],
        open=Decimal(close),
        close=Decimal(close),
        high=Decimal(close) + 1,
        low=Decimal(close) - 1,
        volume=Decimal(volume),
    )
    return CausalFrame("i", 100, (*bars[:-1], last)), Opportunity("o", "e", "i", "LONG", 100, 110)


def test_auxiliary_future_missing_expired_and_conflicting_versions():
    r = reading("open_interest", 1000)
    for unavailable in (
        replace(r, available_ns=None),
        replace(r, available_ns=101),
        replace(r, valid_until_ns=101),
    ):
        when = 101 if unavailable.valid_until_ns == 101 else 100
        assert positioning_at((unavailable,), "i", "open_interest", when) is None
    assert positioning_at((r,), "other", "open_interest", 100) is None
    assert positioning_at((r, r), "i", "open_interest", 100) == 1000
    with pytest.raises(ValueError, match="conflicting"):
        positioning_at((r, replace(r, value=Decimal(2000))), "i", "open_interest", 100)
    with pytest.raises(ValueError, match="before receipt"):
        replace(r, available_ns=99)


@pytest.mark.parametrize(
    "variant,funding,close,side",
    [
        ("a", 0.001, 98, "SHORT"),
        ("b", -0.001, 102, "LONG"),
        ("c", 0.001, 98, "SHORT"),
        ("c", -0.001, 102, "LONG"),
        ("d", 0.001, 102, "SHORT"),
        ("d", -0.001, 98, "LONG"),
    ],
)
def test_each_funding_variant_composes_actual_price_and_auxiliary_readings(
    variant, funding, close, side
):
    frame, opportunity = context(close)
    readings = (reading("settled_funding_rate", funding), reading("open_interest", 1000))
    assert (
        observe_funding(
            frame, replace(opportunity, direction=side), variant=variant, readings=readings
        ).kind
        == StanceKind.SUPPORT
    )
    assert observe_funding(frame, opportunity, variant=variant).kind == StanceKind.ABSTAIN
    if variant == "c":
        assert (
            observe_funding(frame, opportunity, variant=variant, readings=readings[:1]).reason
            == "MISSING_POSITIONING"
        )


@pytest.mark.parametrize(
    "variant,close,volume,skew,side",
    [
        ("a", 102, 20, 1, "LONG"),
        ("b", 102, 0, 0.5, "SHORT"),
        ("c", 98, 20, 1, "SHORT"),
        ("d", 98, 0, 0.5, "LONG"),
    ],
)
def test_all_positioning_volume_patterns(variant, close, volume, skew, side):
    frame, opportunity = context(close, volume)
    readings = (reading("open_interest", 1000), reading("long_short_ratio", skew))
    assert (
        observe_open_interest(
            frame, replace(opportunity, direction=side), variant=variant, readings=readings
        ).kind
        == StanceKind.SUPPORT
    )
    late = tuple(replace(r, available_ns=101) for r in readings)
    assert (
        observe_open_interest(frame, opportunity, variant=variant, readings=late).reason
        == "MISSING_POSITIONING"
    )


@pytest.mark.parametrize(
    "family,metric,value",
    [
        ("funding-crowding-reversal", "settled_funding_rate", -0.001),
        ("open-interest-divergence", "long_short_ratio", 1),
    ],
)
def test_selected_policy_receives_only_causally_available_positioning(family, metric, value):
    from v8_next.economics.observer_policy import policy_stances

    frame, opportunity = context()
    readings = (reading(metric, value), reading("open_interest", 1000))
    selected = policy_stances(frame, opportunity, f"families:{family}", readings=readings)
    assert len(selected) == 4
    assert all(s.observer_id == family for s in selected)
    assert any(s.kind == StanceKind.SUPPORT for s in selected)
    for unavailable in (
        (),
        tuple(replace(r, available_ns=101) for r in readings),
        tuple(replace(r, instrument_id="other") for r in readings),
        tuple(
            replace(r, event_ns=98, received_ns=98, available_ns=98, valid_until_ns=100)
            for r in readings
        ),
    ):
        assert all(
            s.kind == StanceKind.ABSTAIN
            for s in policy_stances(
                frame,
                opportunity,
                f"families:{family}",
                readings=unavailable,
            )
        )


@pytest.mark.parametrize(
    "family,variant,close,volume,side,funding",
    [
        ("funding", "a", 98, 20, "SHORT", 0.001),
        ("funding", "b", 102, 20, "LONG", -0.001),
        ("funding", "c", 98, 20, "SHORT", 0.001),
        ("funding", "d", 102, 20, "SHORT", 0.001),
        ("funding", "d", 98, 20, "LONG", -0.001),
        ("open-interest", "a", 102, 20, "LONG", 0),
        ("open-interest", "b", 102, 0, "SHORT", 0),
        ("open-interest", "c", 98, 20, "SHORT", 0),
        ("open-interest", "d", 98, 0, "LONG", 0),
    ],
)
def test_positioning_campaign_structural_stop(family, variant, close, volume, side, funding):
    from v8_next.economics.protection import protection_at

    frame, opportunity = context(close, volume)
    opportunity = replace(opportunity, direction=side)
    readings = (
        reading("settled_funding_rate", funding),
        reading("open_interest", 1000),
        reading("long_short_ratio", 1 if volume else 0.5),
    )
    policy = f"{family}:{variant}:v2"
    protection = protection_at(frame, opportunity, policy, Decimal(".01"), readings=readings)
    assert protection is not None
    sign = 1 if side == "LONG" else -1
    window = (
        frame.candles[-5:]
        if family == "open-interest"
        else (frame.candles[-10:] if variant == "d" else frame.candles[-6:-1])
    )
    reference = min(c.low for c in window) if sign == 1 else max(c.high for c in window)
    expected = reference - sign * 2 if family == "funding" and variant == "d" else reference
    assert protection.close_invalidation_price == reference
    assert protection.stop_price == expected
    assert protection.target_price == close + sign * 2
    assert protection.expires_ns == 108
    assert protection_at(frame, opportunity, policy, Decimal(".01")) is None
    assert (
        protection_at(
            frame,
            opportunity,
            policy,
            Decimal(".01"),
            readings=tuple(replace(r, available_ns=101) for r in readings),
        )
        is None
    )


def test_repeated_receipts_do_not_create_revisions_or_future_leakage():
    first = reading("settled_funding_rate", 0.001)
    repeated = replace(first, received_ns=102, available_ns=102, source_hash="second-capture")
    args = ("i", "settled_funding_rate")
    assert positioning_at((repeated, first), *args, 100) == first.value
    assert positioning_at((repeated, first), *args, 102) == first.value
    revised = replace(repeated, value=Decimal(".002"))
    assert positioning_at((first, revised), *args, 101) == first.value
    with pytest.raises(ValueError, match="conflicting"):
        positioning_at((first, revised), *args, 102)


@pytest.mark.parametrize("side,close,funding", [("SHORT", 102, 0.001), ("LONG", 98, -0.001)])
def test_funding_d_freezes_distinct_close_thesis_barrier(side, close, funding):
    from v8_next.domain.campaign import PaperCampaign
    from v8_next.economics.protection import protection_at

    frame, opportunity = context(close)
    protection = protection_at(
        frame,
        replace(opportunity, direction=side),
        "funding:d:v2",
        Decimal(".01"),
        readings=(reading("settled_funding_rate", funding),),
    )
    assert protection is not None
    barrier = Decimal(close + (1 if side == "SHORT" else -1))
    assert protection.close_invalidation_price == barrier
    assert protection.stop_price != barrier
    campaign = PaperCampaign(
        "c",
        "o",
        "i",
        side,
        Decimal(1),
        100,
        110,
        protection.stop_price,
        protection.target_price,
        barrier,
    )
    assert PaperCampaign.from_record(campaign.to_record()) == campaign
    assert campaign.invalidated_by_close(frame) is None
    current = replace(
        frame.candles[-1],
        start_ns=100,
        end_ns=101,
        received_ns=101,
        available_ns=101,
        close=barrier,
    )
    later = CausalFrame("i", 101, (*frame.candles, current))
    assert campaign.invalidated_by_close(later) is True
    assert campaign.invalidated_by_close(replace(later, candles=())) is None


def test_live_channel_validity_uses_prior_window_not_current_wick():
    from v8_next.domain.campaign import PaperCampaign

    frame, _ = context()
    campaign = PaperCampaign("c", "o", "i", "LONG", Decimal(1), 1, 200, live_channel_bars=20)
    assert campaign.invalidated_by_close(frame) is False
    broken = replace(
        frame,
        candles=(
            *frame.candles[:-1],
            replace(frame.candles[-1], close=Decimal(99), low=Decimal(50)),
        ),
    )
    assert campaign.invalidated_by_close(broken) is True
    assert campaign.invalidated_by_close(replace(frame, candles=frame.candles[-20:])) is None
    assert PaperCampaign.from_record(campaign.to_record()) == campaign
    with pytest.raises(ValueError, match="ambiguous"):
        replace(campaign, close_invalidation_price=Decimal(90))


def test_ema_validity_does_not_require_a_new_pullback_signal():
    from v8_next.domain.campaign import PaperCampaign

    frame, _ = context()
    campaign = PaperCampaign(
        "c", "o", "i", "LONG", Decimal(1), 1, 200, validity_indicator="ema5-above-ema20"
    )
    # Latest close is above the slow EMA: no pullback entry, intact uptrend.
    assert campaign.invalidated_by_close(frame) is False
    flat = replace(frame, candles=frame.candles[:-1])
    assert campaign.invalidated_by_close(flat) is True
    assert campaign.invalidated_by_close(replace(frame, candles=frame.candles[-19:])) is None
    depth = replace(campaign, close_invalidation_price=Decimal(103))
    assert depth.invalidated_by_close(frame) is True
    assert PaperCampaign.from_record(depth.to_record()) == depth


@pytest.mark.parametrize("side", ["LONG", "SHORT"])
def test_macd_zero_validity_and_warmup(side):
    from v8_next.domain.campaign import PaperCampaign

    frame, _ = context()
    campaign = PaperCampaign(
        "c", "o", "i", side, Decimal(1), 1, 200, validity_indicator="macd-zero"
    )
    assert campaign.invalidated_by_close(frame) is (side == "SHORT")
    assert campaign.invalidated_by_close(replace(frame, candles=frame.candles[:-1])) is True
    assert campaign.invalidated_by_close(replace(frame, candles=frame.candles[-33:])) is None
    assert PaperCampaign.from_record(campaign.to_record()) == campaign


@pytest.mark.parametrize("side", ["LONG", "SHORT"])
def test_rsi_reversion_thesis_tracks_extreme_zone(side):
    from v8_next.domain.campaign import PaperCampaign

    frame, _ = context()
    campaign = PaperCampaign(
        "c", "o", "i", side, Decimal(1), 1, 200, validity_indicator="rsi14-reversion"
    )
    # Flat history followed by a gain produces RSI=100 under the declared seed.
    assert campaign.invalidated_by_close(frame) is (side == "SHORT")
    flat = replace(frame, candles=frame.candles[:-1])
    assert campaign.invalidated_by_close(flat) is False  # flat RSI is neutral 50
    assert campaign.invalidated_by_close(replace(frame, candles=frame.candles[-14:])) is None
    assert PaperCampaign.from_record(campaign.to_record()) == campaign


def test_confluence_equality_and_independent_failure_conditions():
    from v8_next.domain.campaign import PaperCampaign

    frame, _ = context()
    frame = replace(frame, candles=frame.candles[:-1])  # close=100, RSI=50
    campaign = PaperCampaign(
        "c",
        "o",
        "i",
        "LONG",
        Decimal(1),
        1,
        200,
        close_invalidation_price=Decimal(90),
        validity_indicator="rsi14-reversion",
        close_breach_price=Decimal(100),
    )
    assert campaign.invalidated_by_close(frame) is False  # equality at Fib is allowed
    assert replace(campaign, close_breach_price=Decimal(101)).invalidated_by_close(frame) is True
    assert (
        replace(campaign, close_invalidation_price=Decimal(100)).invalidated_by_close(frame) is True
    )
    assert PaperCampaign.from_record(campaign.to_record()) == campaign
    short = replace(campaign, direction="SHORT", close_invalidation_price=Decimal(110))
    assert short.invalidated_by_close(frame) is False
    assert replace(short, close_breach_price=Decimal(99)).invalidated_by_close(frame) is True
