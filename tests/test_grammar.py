from dataclasses import replace
from decimal import Decimal

import pytest

from v8_next.domain.market import Candle, CausalFrame
from v8_next.economics.grammar import grammar_opportunity


def frame(prices, widths=None):
    widths = widths or [2] * len(prices)
    bars = tuple(
        Candle(
            "BTCUSDT-PERP.BINANCE",
            i,
            i + 1,
            Decimal(p),
            Decimal(p) + Decimal(w) / 2,
            Decimal(p) - Decimal(w) / 2,
            Decimal(p),
            Decimal(1),
            i + 1,
            i + 1,
            "fixture",
        )
        for i, (p, w) in enumerate(zip(prices, widths, strict=True))
    )
    return CausalFrame("BTCUSDT-PERP.BINANCE", len(bars), bars)


@pytest.mark.parametrize("mirror", [False, True])
def test_mean_reversion_is_available_without_a_channel_breakout(mirror):
    prices = [100] * 23 + [112, 110]
    f = frame([200 - p for p in prices] if mirror else prices)
    opportunity = grammar_opportunity(f, "mean-reversion-v2")
    assert opportunity.direction == ("LONG" if mirror else "SHORT")
    assert opportunity.expires_ns - opportunity.anchor_ns == 12
    assert grammar_opportunity(f, "range-breakout-48-v1") is None
    assert grammar_opportunity(replace(f, decision_ns=30), "mean-reversion-v2") == opportunity


def test_trend_and_extreme_have_distinct_measurement_identities():
    f = frame(list(range(100, 125)))
    trend = grammar_opportunity(f, "trend-continuation-v2")
    assert trend.direction == "LONG"
    extreme = grammar_opportunity(frame([100] * 24 + [120]), "volatility-extreme-v2")
    assert extreme.direction == "LONG" and extreme.identity_status == "CANONICAL"
    assert grammar_opportunity(frame([100] * 25), "volatility-extreme-v2") is None
    neutral = grammar_opportunity(frame([99, 101] * 12 + [100]), "volatility-extreme-v2")
    assert neutral.direction == "NEUTRAL" and neutral.identity_status == "UNKNOWN"


def test_compression_requires_complete_nonconstant_ranges_and_price_move():
    f = frame([100] * 61 + [101], [10] * 20 + [2] * 42)
    result = grammar_opportunity(f, "compression-expansion-v2")
    assert result.direction == "LONG"
    assert grammar_opportunity(frame([100] * 61 + [101]), "compression-expansion-v2") is None
    early = replace(f, decision_ns=61, candles=f.candles[:-1])
    assert grammar_opportunity(early, "compression-expansion-v2") is None


def test_grammar_rejects_unknown_policy():
    with pytest.raises(ValueError):
        grammar_opportunity(frame([100] * 25), "invented")


@pytest.mark.parametrize(
    "policy,prices",
    [
        ("range-breakout-48-v1", [100] * 48 + [120]),
        ("volatility-extreme-v2", [100] * 24 + [120]),
        ("trend-continuation-v2", list(range(100, 125))),
        ("mean-reversion-v2", [100] * 23 + [112, 110]),
    ],
)
def test_eth_opportunity_identity_is_separate_from_btc(policy, prices):
    btc = frame(prices)
    eth = replace(
        btc,
        instrument_id="ETHUSDT-PERP.BINANCE",
        candles=tuple(replace(c, instrument_id="ETHUSDT-PERP.BINANCE") for c in btc.candles),
    )
    first, second = grammar_opportunity(btc, policy), grammar_opportunity(eth, policy)
    assert first is not None and second is not None
    assert first.direction == second.direction
    assert second.exposure_id == "ETH/USD:linear-perpetual"
    assert first.opportunity_id != second.opportunity_id
    assert (
        grammar_opportunity(
            replace(
                eth,
                instrument_id="UNKNOWN",
                candles=tuple(replace(c, instrument_id="UNKNOWN") for c in eth.candles),
            ),
            policy,
        )
        is None
    )
