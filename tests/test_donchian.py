from dataclasses import replace
from decimal import Decimal

import pytest

from v8_next.domain.market import Candle, CausalFrame
from v8_next.economics.decisions import Opportunity, StanceKind
from v8_next.experts.donchian import observe_donchian


def inputs():
    bars = tuple(
        Candle(
            "BTCUSDT-PERP.BINANCE",
            i,
            i + 1,
            Decimal(100),
            Decimal(101),
            Decimal(99),
            Decimal(100),
            Decimal(1),
            i + 1,
            i + 1,
            "test",
        )
        for i in range(21)
    )
    bars = (*bars[:-1], replace(bars[-1], high=Decimal(103), close=Decimal(102)))
    return CausalFrame(bars[0].instrument_id, 21, bars), Opportunity(
        "existing", "BTC/USD", bars[0].instrument_id, "LONG", 21, 100
    )


def test_prior_channel_excludes_current_and_preserves_observer_boundary():
    frame, opportunity = inputs()
    stance = observe_donchian(frame, opportunity)
    assert stance.kind == StanceKind.SUPPORT
    assert stance.opportunity_id == "existing"
    assert not hasattr(stance, "expected_edge")
    assert not hasattr(stance, "quantity")
    assert (
        observe_donchian(frame, replace(opportunity, direction="SHORT")).kind
        == StanceKind.CONTRADICT
    )
    assert observe_donchian(frame, None).kind == StanceKind.ABSTAIN


def test_boundary_warmup_and_gap_do_not_manufacture_setup():
    frame, opportunity = inputs()
    equal = replace(frame.candles[-1], close=Decimal(101))
    assert (
        observe_donchian(replace(frame, candles=(*frame.candles[:-1], equal)), opportunity).kind
        == StanceKind.ABSTAIN
    )
    assert (
        observe_donchian(replace(frame, candles=frame.candles[1:]), opportunity).reason == "WARMUP"
    )
    assert (
        observe_donchian(
            replace(frame, candles=(*frame.candles[:5], *frame.candles[6:])), opportunity
        ).reason
        == "SOURCE_GAP"
    )


def test_wrong_instrument_future_and_expired_opportunity_reject():
    frame, opportunity = inputs()
    for invalid in (
        replace(opportunity, instrument_id="other"),
        replace(opportunity, anchor_ns=22),
        replace(opportunity, expires_ns=21),
    ):
        with pytest.raises(ValueError, match="outside observer domain"):
            observe_donchian(frame, invalid)
