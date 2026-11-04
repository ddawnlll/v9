from decimal import Decimal

import pytest

from v8_next.domain.market import Candle, CausalFrame
from v8_next.economics.decisions import Opportunity, StanceKind
from v8_next.experts.pandf import columns, observe_pandf, pandf_setup


def frame_for(prices):
    prices = [100] * 20 + prices
    bars = tuple(
        Candle(
            "i",
            i,
            i + 1,
            Decimal(p),
            Decimal(p) + Decimal(".5"),
            Decimal(p) - Decimal(".5"),
            Decimal(p),
            Decimal(1),
            i + 1,
            i + 1,
            "fixture",
        )
        for i, p in enumerate(prices)
    )
    return CausalFrame("i", len(bars), bars)


@pytest.mark.parametrize(
    "variant,prices,direction,stop,target",
    [
        ("a", [104, 100, 105], "LONG", 101, 113),
        ("b", [104, 100, 104, 99], "SHORT", 103, 91),
        ("c", [104, 100, 105, 100, 106], "LONG", 101, 116),
        ("d", [104, 100, 104, 99, 104, 98], "SHORT", 103, 88),
    ],
)
def test_all_breakouts_and_column_anchored_geometry(variant, prices, direction, stop, target):
    frame = frame_for(prices)
    setup = pandf_setup(frame, variant)
    assert setup is not None
    assert (setup.direction, setup.stop, setup.target, setup.box) == (direction, stop, target, 1)
    assert setup.column_start_ns == len(frame.candles)
    opportunity = Opportunity("o", "e", "i", direction, frame.decision_ns, frame.decision_ns + 10)
    assert observe_pandf(frame, opportunity, variant=variant).kind == StanceKind.SUPPORT
    from v8_next.economics.protection import protection_at

    protection = protection_at(frame, opportunity, f"pandf:{variant}:v2", Decimal(".01"))
    assert protection.close_invalidation_price == stop


def test_reversal_boundary_and_sub_box_movements():
    result = columns(tuple(map(Decimal, [100, 104, 102, 101])), Decimal(1))
    assert len(result) == 2
    assert (result[0].start, result[0].extreme, result[0].steps) == (0, 104, 4)
    assert (result[1].start, result[1].origin, result[1].extreme, result[1].steps) == (
        3,
        103,
        101,
        2,
    )
    assert columns((Decimal(100), Decimal("100.9")), Decimal(1))[0].steps == 0
    assert pandf_setup(frame_for([104, 100, 104]), "a") is None
    assert pandf_setup(frame_for([104, 100, 105]), "c") is None


def test_large_box_count_is_compact_and_bad_box_rejects():
    result = columns((Decimal(1), Decimal(1000000000)), Decimal(".000001"))
    assert len(result) == 1 and result[0].steps == 999999999000000
    with pytest.raises(ValueError):
        columns((Decimal(100),), Decimal(0))
