from dataclasses import replace
from decimal import Decimal

import pytest

from v8_next.domain.market import Candle, CausalFrame
from v8_next.economics.decisions import Opportunity, StanceKind
from v8_next.experts.breakouts import (
    observe_failed_breakout,
    observe_volume_breakout,
    volume_variant,
)


def inputs(closes, volumes=None):
    if volumes is None:
        volumes = [10] * len(closes)
    candles = tuple(
        Candle(
            "instrument",
            i,
            i + 1,
            Decimal(c),
            Decimal(c) + 1,
            Decimal(c) - 1,
            Decimal(c),
            Decimal(v),
            i + 1,
            i + 1,
            "test-only",
        )
        for i, (c, v) in enumerate(zip(closes, volumes, strict=True))
    )
    return CausalFrame("instrument", len(candles), candles), Opportunity(
        "o", "e", "instrument", "SHORT", len(candles), len(candles) + 10
    )


def test_failed_breakout_freezes_prebreak_level_and_recency():
    for bars_since in range(1, 7):
        frame, opportunity = inputs([100, 103] + [100] * bars_since)
        stance = observe_failed_breakout(frame, opportunity)
        assert stance.kind == (StanceKind.SUPPORT if bars_since <= 5 else StanceKind.ABSTAIN)
    frame, opportunity = inputs([100, 103, 101])
    assert observe_failed_breakout(frame, opportunity).kind == StanceKind.ABSTAIN
    frame, opportunity = inputs([100, 103, 100, 106, 103])
    # Latest breakout freezes 104, superseding old 101.
    assert observe_failed_breakout(frame, opportunity).kind == StanceKind.SUPPORT


@pytest.mark.parametrize(
    "volume,mean,z,proximity,expected",
    [
        (20, 10, 1.9, 0.1, "d"),
        (20, 10, 2, 0.1, "c"),
        (20, 10, None, None, "c"),
        (12, 10, None, None, "c"),
        (11, 10, None, 0.39, "b"),
        (11, 10, None, 0.4, "a"),
        (10, 10, None, None, None),
        (0, 0, None, None, None),
    ],
)
def test_volume_priority_and_boundaries(volume, mean, z, proximity, expected):
    assert volume_variant(volume, mean, z, proximity) == expected


def test_volume_breakout_uses_prior_price_channel_and_current_inclusive_mean():
    for close, side in [(103, "LONG"), (97, "SHORT")]:
        frame, opportunity = inputs([100] * 20 + [close], [10] * 20 + [20])
        stance = observe_volume_breakout(frame, replace(opportunity, direction=side))
        assert stance.kind == StanceKind.SUPPORT
        assert stance.variant_id == "c"
        flat_volume = replace(
            frame, candles=tuple(replace(c, volume=Decimal(10)) for c in frame.candles)
        )
        assert observe_volume_breakout(flat_volume, opportunity).kind == StanceKind.ABSTAIN


@pytest.mark.parametrize("observer", [observe_failed_breakout, observe_volume_breakout])
def test_gap_and_no_opportunity_never_support(observer):
    frame, opportunity = inputs([100] * 20 + [103, 100])
    assert observer(frame, None).kind == StanceKind.ABSTAIN
    gap = replace(frame, candles=frame.candles[:5] + frame.candles[6:])
    assert observer(gap, opportunity).reason == "SOURCE_GAP"


@pytest.mark.parametrize("last_volume,variant", [(50, "d"), (11, "b")])
def test_hundred_bar_volume_statistics_select_specific_variants(last_volume, variant):
    volumes = [1000] * 40 + [0] * 40 + [10] * 19 + [last_volume]
    frame, opportunity = inputs([100] * 99 + [103], volumes)
    stance = observe_volume_breakout(frame, replace(opportunity, direction="LONG"))
    assert stance.kind == StanceKind.SUPPORT
    assert stance.variant_id == variant


def test_volume_campaign_requires_actual_volume_gate_in_each_direction():
    from v8_next.economics.protection import protection_at

    for close, direction in [(103, "LONG"), (97, "SHORT")]:
        frame, opportunity = inputs([100] * 20 + [close], [10] * 20 + [20])
        opportunity = replace(opportunity, direction=direction)
        protection = protection_at(frame, opportunity, "volume-breakout:active:v2", Decimal(".01"))
        assert protection is not None
        sign = 1 if direction == "LONG" else -1
        prior = frame.candles[-21:-1]
        assert protection.close_invalidation_price == (
            max(c.high for c in prior) if sign == 1 else min(c.low for c in prior)
        )
        assert (Decimal(close) - protection.stop_price) * sign == 2
        assert (protection.target_price - Decimal(close)) * sign == 2
        flat_volume = replace(
            frame, candles=tuple(replace(c, volume=Decimal(10)) for c in frame.candles)
        )
        assert (
            protection_at(flat_volume, opportunity, "volume-breakout:active:v2", Decimal(".01"))
            is None
        )


def test_failed_campaign_stop_is_frozen_breakout_level_not_range_multiple():
    from v8_next.economics.protection import protection_at

    frame, opportunity = inputs([100] * 20 + [103, 100])
    protection = protection_at(frame, opportunity, "failed-breakout:a:v2", Decimal(".01"))
    assert protection is not None
    assert protection.close_invalidation_price == 101
    assert protection.stop_price == 101
    assert protection.target_price == 98
    assert protection.expires_ns == frame.decision_ns + 8
    # A newer break supersedes the older reference; do not reuse the first level.
    frame, opportunity = inputs([100] * 20 + [103, 100, 106, 103])
    protection = protection_at(frame, opportunity, "failed-breakout:a:v2", Decimal(".01"))
    assert protection is not None and protection.stop_price == 104
    stale, opportunity = inputs([100] * 20 + [103] + [100] * 6)
    assert protection_at(stale, opportunity, "failed-breakout:a:v2", Decimal(".01")) is None
