from dataclasses import replace
from decimal import Decimal

import pytest

from v8_next.domain.market import Candle, CausalFrame
from v8_next.economics.decisions import Opportunity, StanceKind
from v8_next.experts.climax import climax_hit, observe_volume_climax


@pytest.mark.parametrize(
    "close,opening,fast,slow,z,prox,reversal,expected",
    [
        (100, 99, 90, 95, 3, 0.1, True, ("e", "LONG")),
        (100, 99, 105, 95, 3, 0.1, True, ("e", "SHORT")),
        (100, 99, 105, 95, 2, 0.1, True, ("d", "LONG")),
        (100, 101, 105, 95, 2, 0.1, True, ("d", "SHORT")),
        (90, 90, 95, 100, None, 0.39, False, ("c", "LONG")),
        (110, 110, 105, 100, None, 0.39, False, ("c", "SHORT")),
        (110, 110, 105, 100, 2, 0.4, False, ("b", "SHORT")),
        (90, 90, 95, 100, 2, 0.4, False, ("a", "LONG")),
        (100, 100, 100, 100, 3, None, False, None),
    ],
)
def test_all_climax_gates_priority_and_boundaries(
    close, opening, fast, slow, z, prox, reversal, expected
):
    assert climax_hit(close, opening, fast, slow, z, prox, reversal) == expected


def test_native_volume_statistics_drive_strict_climax_and_flat_volume_abstains():
    bars = tuple(
        Candle(
            "i",
            i,
            i + 1,
            Decimal(100 + i),
            Decimal(102 + i),
            Decimal(99 + i),
            Decimal(101 + i),
            Decimal(10),
            i + 1,
            i + 1,
            "fixture",
        )
        for i in range(100)
    )
    frame = CausalFrame("i", 100, bars)
    opportunity = Opportunity("o", "e", "i", "SHORT", 100, 110)
    assert observe_volume_climax(frame, opportunity).reason == "MISSING_VOLUME_DISPERSION"
    spike = replace(frame, candles=(*bars[:-1], replace(bars[-1], volume=Decimal(100))))
    stance = observe_volume_climax(spike, opportunity)
    assert stance.kind == StanceKind.SUPPORT and stance.variant_id == "e"
    assert stance.decision_ns == 100
    from v8_next.economics.protection import protection_at

    protection = protection_at(spike, opportunity, "volume-climax:active:v2", Decimal(".01"))
    assert protection is not None
    assert protection.close_invalidation_price == spike.candles[-1].high
    assert protection.stop_price == spike.candles[-1].close + 3
    assert protection.target_price == spike.candles[-1].close - 3
    assert protection.expires_ns == 108
    assert protection_at(frame, opportunity, "volume-climax:active:v2", Decimal(".01")) is None
