from dataclasses import replace
from decimal import Decimal

import pytest

from v8_next.domain.market import Candle, CausalFrame
from v8_next.economics.decisions import Opportunity, StanceKind
from v8_next.experts.fibonacci import fib_impulse, observe_fib_projection, observe_fib_retracement


def context():
    bars = [
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
        for i in range(37)
    ]
    bars[10] = replace(bars[10], low=Decimal(70))
    bars[25] = replace(bars[25], high=Decimal(150))
    return CausalFrame("i", 37, tuple(bars)), Opportunity("o", "e", "i", "LONG", 37, 100)


def test_impulse_waits_for_confirmation_and_preserves_origin_geometry():
    frame, _ = context()
    assert fib_impulse(replace(frame, candles=frame.candles[:35], decision_ns=35)) is None
    impulse = fib_impulse(frame)
    assert impulse.direction == "LONG"
    assert (impulse.origin, impulse.extreme, impulse.confirmed_ns) == (70, 150, 36)
    assert impulse.retracement(Decimal("0.382")) == Decimal("119.440")
    assert impulse.retracement(Decimal("0.786")) == Decimal("87.120")
    assert impulse.extension(Decimal("1.618")) == Decimal("199.440")
    assert impulse == fib_impulse(replace(frame, candles=frame.candles[:36], decision_ns=36))


@pytest.mark.parametrize(
    "observer,close,high,low,direction",
    [
        (observe_fib_retracement, 125, 126, 119, "LONG"),
        (observe_fib_projection, 190, 200, 189, "SHORT"),
    ],
)
def test_real_pivot_feature_to_reclaim_and_projection_both_directions(
    observer, close, high, low, direction
):
    frame, opportunity = context()
    last = replace(
        frame.candles[-1],
        open=Decimal(close),
        close=Decimal(close),
        high=Decimal(high),
        low=Decimal(low),
    )
    frame = replace(frame, candles=(*frame.candles[:-1], last))
    assert observer(frame, replace(opportunity, direction=direction)).kind == StanceKind.SUPPORT
    from v8_next.economics.protection import protection_at

    policy = (
        "fib-retracement:a:v2" if observer is observe_fib_retracement else "fib-projection:a:v2"
    )
    protection = protection_at(
        frame, replace(opportunity, direction=direction), policy, Decimal(".01")
    )
    assert protection is not None
    impulse = fib_impulse(frame)
    expected = (
        impulse.retracement(Decimal(".786"))
        if observer is observe_fib_retracement
        else impulse.extension(Decimal("1.618"))
    )
    assert protection.close_invalidation_price == expected
    span = sum((c.high - c.low for c in frame.candles[-14:]), Decimal(0)) / 14
    assert 0 <= span - abs(Decimal(close) - protection.stop_price) < Decimal(".01")
    assert 0 <= span - abs(protection.target_price - Decimal(close)) < Decimal(".01")
    assert protection.expires_ns == 45
    mirrored = replace(
        frame,
        candles=tuple(
            replace(c, open=250 - c.open, close=250 - c.close, high=250 - c.low, low=250 - c.high)
            for c in frame.candles
        ),
    )
    assert (
        protection_at(
            mirrored,
            replace(opportunity, direction="SHORT" if direction == "LONG" else "LONG"),
            policy,
            Decimal(".01"),
        )
        is not None
    )
    assert (
        observer(
            mirrored, replace(opportunity, direction="SHORT" if direction == "LONG" else "LONG")
        ).kind
        == StanceKind.SUPPORT
    )


def test_same_bar_pivot_pair_is_not_a_time_ordered_impulse():
    frame, _ = context()
    bars = list(frame.candles)
    bars[25] = replace(bars[25], low=Decimal(60))
    assert fib_impulse(replace(frame, candles=tuple(bars))) is None
