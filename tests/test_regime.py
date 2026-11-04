from decimal import Decimal as D

import pytest

from v8_next.domain.market import Candle, CausalFrame
from v8_next.domain.positioning import PositioningReading
from v8_next.economics.regime import observe_regime


def test_campaign_freezes_decision_regime_and_restores_it():
    from dataclasses import replace

    from v8_next.domain.campaign import PaperCampaign
    from v8_next.evaluation.outcomes import observed_outcomes

    regime = observe_regime(frame([10] * 20))
    campaign = PaperCampaign("c", "o", "BTC", "LONG", D(1), 100, 200, decision_regime=regime)
    restored = PaperCampaign.from_record(campaign.to_record())
    assert restored == campaign
    with pytest.raises(ValueError, match="regime identity"):
        replace(campaign, decision_ns=101)
    with pytest.raises(ValueError, match="regime identity"):
        replace(campaign, instrument_id="ETH")
    # Later market conditions cannot relabel this campaign.
    assert observe_regime(frame([10] * 19 + [100])).volume == "VolumeExpansion"
    assert restored.decision_regime.volume == "NormalVolume"
    sample = observed_outcomes(
        [restored.to_record()],
        [],
        {
            "currency": "USDT",
            "realization": "SIMULATED",
            "balance_total": "100 USDT",
            "positions": [],
            "orders": [],
        },
        D(100),
    )
    assert sample["rows"][0]["decision_regime"] == campaign.to_record()["decision_regime"]
    assert sample["rows"][0]["net_r"] is None
    assert (
        PaperCampaign.from_record(
            {k: v for k, v in campaign.to_record().items() if k != "decision_regime"}
        ).decision_regime
        is None
    )


def frame(volumes: list[int]) -> CausalFrame:
    return CausalFrame(
        "BTC",
        100,
        tuple(
            Candle("BTC", i, i + 1, D(1), D(1), D(1), D(1), D(v), i + 1, i + 1, "source")
            for i, v in enumerate(volumes)
        ),
    )


def test_volume_window_and_missingness():
    assert observe_regime(frame([10] * 19)).volume is None
    assert observe_regime(frame([0] * 20)).volume is None
    assert observe_regime(frame([10] * 20)).volume == "NormalVolume"
    assert observe_regime(frame([10] * 19 + [20])).volume == "VolumeExpansion"
    assert observe_regime(frame([10] * 19 + [1])).volume == "VolumeDrought"
    # Older volume cannot influence the declared 20-bar window.
    assert observe_regime(frame([999] + [10] * 20)).volume_ratio == "1"
    original = frame([10] * 21)
    gap = CausalFrame("BTC", 100, original.candles[:10] + original.candles[11:])
    assert observe_regime(gap).volume is None


def test_funding_knowledge_expiry_and_thresholds():
    for rate, expected in [
        ("0.00015", "CrowdedLong"),
        ("-0.00015", "CrowdedShort"),
        ("0", "NeutralFunding"),
    ]:
        reading = PositioningReading(
            "BTC", "settled_funding_rate", D(rate), 90, 95, 100, 110, "hash"
        )
        result = observe_regime(frame([]), readings=(reading,))
        assert result.funding == expected
        assert result.trend is None and result.volatility is None
        assert result.habitat_status == "UNQUALIFIED"
        assert observe_regime(CausalFrame("BTC", 99, ()), readings=(reading,)).funding is None
        assert observe_regime(CausalFrame("BTC", 110, ()), readings=(reading,)).funding is None
    assert observe_regime(frame([])).funding is None


def test_trend_and_volatility_regime_classification():
    # Warmup gating: <28 bars for trend, <62 bars for volatility
    candles_20 = tuple(
        Candle("BTC", i, i + 1, D(100), D(105), D(95), D(100), D(10), i + 1, i + 1, "s")
        for i in range(20)
    )
    reg_20 = observe_regime(CausalFrame("BTC", 20, candles_20))
    assert reg_20.trend is None
    assert reg_20.volatility is None

    # Trend requires 28 bars: flat prices -> ADX is 0 -> ChopRange
    candles_28 = tuple(
        Candle("BTC", i, i + 1, D(100), D(105), D(95), D(100), D(10), i + 1, i + 1, "s")
        for i in range(28)
    )
    reg_28 = observe_regime(CausalFrame("BTC", 28, candles_28))
    assert reg_28.trend == "ChopRange"
    assert reg_28.volatility is None

    # A 49-value range14 window requires 62 continuous bars
    candles_61 = tuple(
        Candle("BTC", i, i + 1, D(100), D(105), D(95), D(100), D(10), i + 1, i + 1, "s")
        for i in range(61)
    )
    reg_61 = observe_regime(CausalFrame("BTC", 61, candles_61))
    assert reg_61.volatility is None
    from dataclasses import replace

    extra = replace(candles_61[-1], start_ns=61, end_ns=62, received_ns=62, available_ns=62)
    assert observe_regime(CausalFrame("BTC", 62, candles_61 + (extra,))).volatility == "NormalVol"
    assert observe_regime(frame([10] * 62)).volatility is None
