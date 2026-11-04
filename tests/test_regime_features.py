from decimal import Decimal

import pytest

from v8_next.domain.market import Candle, CausalFrame
from v8_next.economics.regime import observe_regime
from v8_next.experts.features import adx_series, simple_atr_series


@pytest.mark.parametrize("sign,label", [(1, "BullTrend"), (-1, "BearTrend")])
def test_directional_adx_and_causal_prefix(sign, label):
    closes = [200.0 + sign * i for i in range(80)]
    highs, lows = [v + 1 for v in closes], [v - 1 for v in closes]
    full = adx_series(highs, lows, closes)
    assert full[:27] == [None] * 27
    assert full[27:] == pytest.approx([100.0] * 53)
    assert adx_series(highs[:40], lows[:40], closes[:40]) == full[:40]
    candles = tuple(
        Candle(
            "BTC",
            i,
            i + 1,
            Decimal(c),
            Decimal(high),
            Decimal(low),
            Decimal(c),
            Decimal(10),
            i + 1,
            i + 1,
            "test-only",
        )
        for i, (high, low, c) in enumerate(zip(highs, lows, closes, strict=True))
    )
    result = observe_regime(CausalFrame("BTC", 80, candles))
    assert result.trend == label
    assert result.volatility == "NormalVol"
    assert result.version == "adx14-range14-median49-volume20-funding-v2"


def test_range_is_not_gap_aware_atr_and_requires_complete_windows():
    assert simple_atr_series([11, 102, 15], [9, 98, 9], 2) == [3, 5]
    assert simple_atr_series([11], [9], 2) == []


@pytest.mark.parametrize("period", [0, -1, True])
def test_invalid_period_rejected(period):
    with pytest.raises(ValueError, match="period"):
        adx_series([], [], [], period)


def test_invalid_inputs_rejected_instead_of_normal_regime():
    with pytest.raises(ValueError, match="aligned"):
        adx_series([1], [], [])
    with pytest.raises(ValueError, match="non-finite"):
        simple_atr_series([float("inf")], [1])
    with pytest.raises(ValueError, match="price range"):
        adx_series([1], [2], [1])
