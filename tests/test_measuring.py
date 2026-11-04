from dataclasses import replace
from decimal import Decimal

import pytest

from v8_next.domain.market import Candle, CausalFrame
from v8_next.economics.decisions import Opportunity, StanceKind
from v8_next.experts.measuring import measuring_setup, observe_measuring


def context(count):
    bars = tuple(
        Candle(
            "i",
            i,
            i + 1,
            Decimal(100),
            Decimal("100.2"),
            Decimal("99.8"),
            Decimal(100),
            Decimal(10),
            i + 1,
            i + 1,
            "fixture",
        )
        for i in range(count)
    )
    return CausalFrame("i", count, bars), Opportunity("o", "e", "i", "SHORT", count, count + 10)


@pytest.mark.parametrize("variant,head", [("head_shoulders", 140), ("double_top", 120)])
def test_pattern_break_has_measuring_height_and_rejects_stale_break(variant, head):
    frame, opportunity = context(34)
    bars = list(frame.candles)
    for i, high in ((5, 120), (15, head), (25, 120)):
        bars[i] = replace(bars[i], high=Decimal(high))
    bars[10] = replace(bars[10], low=Decimal(90))
    bars[20] = replace(bars[20], low=Decimal(95))
    bars[-1] = replace(
        bars[-1], open=Decimal(90), close=Decimal(90), low=Decimal(89), high=Decimal(91)
    )
    frame = replace(frame, candles=tuple(bars))
    setup = measuring_setup(frame, variant)
    assert setup.direction == "SHORT" and setup.level == 95
    assert setup.stop_reference == head and setup.target_distance == head - 95
    from v8_next.economics.protection import protection_at

    protection = protection_at(frame, opportunity, f"measuring:{variant}:v2", Decimal(".01"))
    assert protection.close_invalidation_price == 95
    assert observe_measuring(frame, opportunity, variant=variant).kind == StanceKind.SUPPORT
    bars[-2] = replace(
        bars[-2], open=Decimal(90), close=Decimal(90), low=Decimal(89), high=Decimal(91)
    )
    assert measuring_setup(replace(frame, candles=tuple(bars)), variant) is None


def test_triangle_requires_confirmed_convergence_and_narrow_prior_range():
    frame, opportunity = context(41)
    bars = list(frame.candles)
    bars[24] = replace(bars[24], high=Decimal(101))
    bars[28] = replace(bars[28], low=Decimal(99))
    bars[32] = replace(bars[32], high=Decimal("100.8"))
    bars[36] = replace(bars[36], low=Decimal("99.2"))
    bars[-1] = replace(
        bars[-1], open=Decimal(102), close=Decimal(102), high=Decimal(103), low=Decimal(101)
    )
    frame = replace(frame, candles=tuple(bars))
    setup = measuring_setup(frame, "triangle")
    assert (setup.direction, setup.level, setup.stop_reference, setup.target_distance) == (
        "LONG",
        101,
        99,
        2,
    )
    assert (
        observe_measuring(frame, replace(opportunity, direction="LONG"), variant="triangle").kind
        == StanceKind.SUPPORT
    )
    from v8_next.economics.protection import protection_at

    protection = protection_at(
        frame, replace(opportunity, direction="LONG"), "measuring:triangle:v2", Decimal(".01")
    )
    assert protection.close_invalidation_price == 101
    bars[32] = replace(bars[32], high=Decimal("101.1"))
    assert measuring_setup(replace(frame, candles=tuple(bars)), "triangle") is None
