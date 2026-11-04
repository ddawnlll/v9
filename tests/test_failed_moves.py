from dataclasses import replace
from decimal import Decimal

import pytest

from v8_next.domain.market import Candle, CausalFrame
from v8_next.economics.decisions import Opportunity, StanceKind
from v8_next.experts.failed_moves import failed_move, observe_failed_move


def context(rows):
    bars = tuple(
        Candle("i", i, i + 1, *(Decimal(v) for v in row), Decimal(10), i + 1, i + 1, "fixture")
        for i, row in enumerate(rows)
    )
    return CausalFrame("i", len(bars), bars), Opportunity(
        "o", "e", "i", "LONG", len(bars), len(bars) + 10
    )


@pytest.mark.parametrize("delay", [1, 2, 3, 4])
def test_hikkake_recent_reclaim_and_mirror(delay):
    rows = (
        [(100, 110, 90, 100), (100, 105, 95, 100), (94, 96, 92, 94)]
        + [(100, 101, 99, 100)] * (delay - 1)
        + [(106, 107, 105, 106)]
    )
    frame, opportunity = context(rows)
    stance = observe_failed_move(frame, opportunity, variant="c")
    assert (stance.kind == StanceKind.SUPPORT) is (delay <= 3)
    mirrored, opposite = context([(200 - o, 200 - low, 200 - h, 200 - c) for o, h, low, c in rows])
    assert (
        observe_failed_move(mirrored, replace(opposite, direction="SHORT"), variant="d").kind
        == StanceKind.SUPPORT
    ) is (delay <= 3)


@pytest.mark.parametrize(
    "variant,prefix,prior,current,side,level",
    [
        ("e", 0, (100, 101, 99, 100), (98, 101, 97, 100), "LONG", 99),
        ("e", 0, (100, 101, 99, 100), (102, 103, 99, 100), "SHORT", 101),
        ("f", 26, (102, 103, 101, 102), (98, 99, 97, 98), "SHORT", 100),
        ("g", 20, (98, 100, 80, 98), (100, 101, 99, 100), "LONG", 99),
        ("g", 20, (102, 120, 100, 102), (100, 101, 99, 100), "SHORT", 101),
    ],
)
def test_reference_excludes_false_move_bar(variant, prefix, prior, current, side, level):
    frame, opportunity = context([(100, 101, 99, 100)] * prefix + [prior, current])
    setup = failed_move(frame, variant)
    assert setup.direction == side and setup.reference == level
    assert (
        observe_failed_move(frame, replace(opportunity, direction=side), variant=variant).kind
        == StanceKind.SUPPORT
    )


def test_b_uses_confirmed_significant_swing():
    rows = [(100, 101, 99, 100)] * 40
    rows[15] = (100, 101, 90, 100)
    rows[-2:] = [(89, 90, 88, 89), (91, 92, 90, 91)]
    frame, opportunity = context(rows)
    assert failed_move(frame, "b").reference == 90
    assert observe_failed_move(frame, opportunity, variant="b").kind == StanceKind.SUPPORT
    with pytest.raises(ValueError, match="unsupported"):
        failed_move(frame, "a")


@pytest.mark.parametrize("variant", list("bcdefg"))
def test_failed_move_campaigns_use_declared_unit_geometry_with_full_volatility(variant):
    from v8_next.economics.protection import protection_at

    rows = [(100, 101, 99, 100)] * 40
    side = "LONG"
    if variant == "b":
        rows[15] = (100, 101, 90, 100)
        rows[-2:] = [(89, 90, 88, 89), (91, 92, 90, 91)]
    elif variant in {"c", "d"}:
        rows[-4:] = [
            (100, 110, 90, 100),
            (100, 105, 95, 100),
            (94, 96, 92, 94),
            (106, 107, 105, 106),
        ]
        if variant == "d":
            rows = [(200 - o, 200 - low, 200 - high, 200 - close) for o, high, low, close in rows]
            side = "SHORT"
    elif variant == "e":
        rows[-1] = (98, 101, 97, 100)
    elif variant == "f":
        rows[-2:] = [(102, 103, 101, 102), (98, 99, 97, 98)]
        side = "SHORT"
    else:
        rows[-2:] = [(98, 100, 80, 98), (100, 101, 99, 100)]
    frame, opportunity = context(rows)
    protection = protection_at(
        frame, replace(opportunity, direction=side), f"failed-move:{variant}:v2", Decimal(".01")
    )
    assert protection is not None
    assert protection.close_invalidation_price == failed_move(frame, variant).reference
    span = sum((c.high - c.low for c in frame.candles[-14:]), Decimal(0)) / 14
    assert 0 <= span - abs(frame.candles[-1].close - protection.stop_price) < Decimal(".01")
    assert protection.expires_ns == frame.decision_ns + 8
    short, opportunity = context(rows[-4:])
    assert (
        protection_at(
            short, replace(opportunity, direction=side), f"failed-move:{variant}:v2", Decimal(".01")
        )
        is None
    )
