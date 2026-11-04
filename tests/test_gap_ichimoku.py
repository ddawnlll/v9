from dataclasses import replace
from decimal import Decimal

import pytest

from v8_next.domain.market import Candle, CausalFrame
from v8_next.economics.decisions import Opportunity, StanceKind
from v8_next.experts.gaps import gap_setup, observe_gap
from v8_next.experts.ichimoku import observe_ichimoku


def context(closes):
    bars = tuple(
        Candle(
            "i",
            i,
            i + 1,
            Decimal(p),
            Decimal(p) + 1,
            Decimal(p) - 1,
            Decimal(p),
            Decimal(10),
            i + 1,
            i + 1,
            "fixture",
        )
        for i, p in enumerate(closes)
    )
    return CausalFrame("i", len(bars), bars), Opportunity(
        "o", "e", "i", "LONG", len(bars), len(bars) + 10
    )


def mirror(frame):
    return replace(
        frame,
        candles=tuple(
            replace(c, open=200 - c.open, close=200 - c.close, high=200 - c.low, low=200 - c.high)
            for c in frame.candles
        ),
    )


@pytest.mark.parametrize(
    "variant,count,direction", [("a", 3, "SHORT"), ("b", 1, "LONG"), ("c", 2, "LONG")]
)
def test_gap_sequence_and_directional_mirror(variant, count, direction):
    frame, opportunity = context([100] * (21 - count) + [110 + 10 * i for i in range(count)])
    bars = list(frame.candles)
    bars[-1] = replace(bars[-1], close=bars[-1].open + (-1 if variant == "a" else 1))
    frame = replace(frame, candles=tuple(bars))
    setup = gap_setup(frame, variant)
    assert setup is not None and setup.same_direction_count == count
    assert setup.direction == direction
    assert setup.top > setup.bottom
    assert (
        observe_gap(frame, replace(opportunity, direction=direction), variant=variant).kind
        == StanceKind.SUPPORT
    )
    inverse = "SHORT" if direction == "LONG" else "LONG"
    assert (
        observe_gap(mirror(frame), replace(opportunity, direction=inverse), variant=variant).kind
        == StanceKind.SUPPORT
    )
    wrong = "b" if variant != "b" else "c"
    assert gap_setup(frame, wrong) is None


def test_gap_count_requires_full_window_and_data_gaps_reject():
    frame, opportunity = context([100] * 20 + [110])
    assert gap_setup(replace(frame, candles=frame.candles[1:]), "b") is None
    gap = replace(frame, candles=frame.candles[:5] + frame.candles[6:])
    assert observe_gap(gap, opportunity, variant="b").reason == "SOURCE_GAP"
    with pytest.raises(ValueError, match="unsupported gap variant"):
        gap_setup(frame, "unknown")


def test_ichimoku_requires_cross_not_persistent_alignment():
    frame, opportunity = context([110] * 10 + [100] * 16 + [125])
    bars = list(frame.candles)
    bars[12] = replace(bars[12], low=Decimal(90))
    frame = replace(frame, candles=tuple(bars))
    assert observe_ichimoku(frame, opportunity).kind == StanceKind.SUPPORT
    assert (
        observe_ichimoku(mirror(frame), replace(opportunity, direction="SHORT")).kind
        == StanceKind.SUPPORT
    )
    next_bar = replace(bars[-1], start_ns=27, end_ns=28, available_ns=28, received_ns=28)
    continued = replace(frame, candles=(*frame.candles, next_bar), decision_ns=28)
    assert observe_ichimoku(continued, opportunity).kind == StanceKind.ABSTAIN
    assert (
        observe_ichimoku(replace(frame, candles=frame.candles[:-1]), opportunity).reason == "WARMUP"
    )


@pytest.mark.parametrize(
    "variant,count,direction", [("a", 3, "SHORT"), ("b", 1, "LONG"), ("c", 2, "LONG")]
)
@pytest.mark.parametrize("inverse", [False, True])
def test_gap_campaign_keeps_zone_stop_and_one_range_target(variant, count, direction, inverse):
    from v8_next.economics.protection import protection_at

    frame, opportunity = context([100] * (21 - count) + [110 + 10 * i for i in range(count)])
    bars = list(frame.candles)
    bars[-1] = replace(bars[-1], close=bars[-1].open + (-1 if variant == "a" else 1))
    frame = replace(frame, candles=tuple(bars))
    if inverse:
        frame = mirror(frame)
        direction = "SHORT" if direction == "LONG" else "LONG"
    opportunity = replace(opportunity, direction=direction)
    protection = protection_at(frame, opportunity, f"gap:{variant}:v2", Decimal(1))
    setup = gap_setup(frame, variant)
    assert protection.close_invalidation_price == (
        setup.bottom if direction == "LONG" else setup.top
    )
    assert protection.stop_price == setup.stop_reference
    assert protection.target_price == frame.candles[-1].close + (2 if direction == "LONG" else -2)


@pytest.mark.parametrize("inverse", [False, True])
def test_ichimoku_campaign_clamps_kijun_distance_and_targets_one_point_five_ranges(inverse):
    from v8_next.economics.protection import protection_at

    frame, opportunity = context([110] * 10 + [100] * 16 + [125])
    bars = list(frame.candles)
    bars[12] = replace(bars[12], low=Decimal(90))
    frame = replace(frame, candles=tuple(bars))
    if inverse:
        frame = mirror(frame)
        opportunity = replace(opportunity, direction="SHORT")
    protection = protection_at(frame, opportunity, "ichimoku:cross:v2", Decimal(".01"))
    assert protection is not None
    assert protection.validity_indicator == "kijun26"
    # Kijun is far away; stop caps at 2 ranges, target is 1.5 ranges.
    span = sum((c.high - c.low for c in frame.candles[-14:]), Decimal(0)) / 14
    close = frame.candles[-1].close
    assert abs(close - protection.stop_price) == 2 * span
    assert abs(close - protection.target_price) == Decimal("1.5") * span


@pytest.mark.parametrize("side", ["LONG", "SHORT"])
def test_live_kijun_validity_updates_with_current_window(side):
    from v8_next.domain.campaign import PaperCampaign

    frame, _ = context([100] * 26)
    campaign = PaperCampaign(
        "c",
        "o",
        frame.instrument_id,
        side,
        Decimal(1),
        0,
        frame.decision_ns + 10,
        validity_indicator="kijun26",
    )
    assert campaign.invalidated_by_close(frame) is True  # equality invalidates both sides
    moved = replace(
        frame, candles=(*frame.candles[:-1], replace(frame.candles[-1], high=Decimal(110)))
    )
    assert campaign.invalidated_by_close(moved) is (side == "LONG")
    assert campaign.invalidated_by_close(replace(frame, candles=frame.candles[-25:])) is None
    assert PaperCampaign.from_record(campaign.to_record()) == campaign
