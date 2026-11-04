"""Synthetic callback inputs remain isolated from research artifacts."""

from dataclasses import replace
from decimal import Decimal

from nautilus_trader.model import Bar, BarType, Price, Quantity

from v8_next.app.backtest import HistoricalObserver
from v8_next.domain.market import Candle


def test_historical_callback_prefix_is_invariant_and_preserves_unknown_clock():
    hour = 3600 * 10**9
    candles = [
        Candle(
            "BTCUSDT-PERP.BINANCE",
            i * hour,
            (i + 1) * hour,
            Decimal(100 + i),
            Decimal(102 + i),
            Decimal(99 + i),
            Decimal(101 + i),
            Decimal(20),
            100 * hour,
            None,
            "TEST_ONLY",
        )
        for i in range(80)
    ]
    changed = candles[:70] + [
        replace(c, open=c.open * 2, high=c.high * 2, low=c.low * 2, close=c.close * 2)
        for c in candles[70:]
    ]

    def run(source):
        observer = HistoricalObserver()
        observer.source = {c.end_ns: c for c in source}
        for c in source:
            observer.on_bar(
                Bar(
                    BarType.from_str("BTCUSDT-PERP.BINANCE-1-HOUR-LAST-EXTERNAL"),
                    Price.from_str(str(c.open)),
                    Price.from_str(str(c.high)),
                    Price.from_str(str(c.low)),
                    Price.from_str(str(c.close)),
                    Quantity.from_str(str(c.volume)),
                    c.end_ns,
                    c.end_ns,
                )
            )
        return observer.decisions

    prefix = run(candles[:70])
    assert prefix == run(candles)[:70] == run(changed)[:70]
    assert len(prefix) == 70
    assert all(c.available_ns is None for c in candles)
    assert all(d["historical_available_ns"] is None for d in prefix)
    assert all(d["execution"] == "NOT_SUBMITTED" for d in prefix)
    assert all(d["claim_status"] == "NO_ECONOMIC_CLAIM" for d in prefix)
    assert all(d["admission"] == "REJECTED_MISSING_CALIBRATION" for d in prefix)

    assert all(len(d["expert_diagnostics"]) == 64 for d in prefix)
    assert {s["observer_id"] for s in prefix[-1]["expert_diagnostics"]} == {
        "squeeze-swing",
        "pandf-breakout",
        "divergence-12-setups",
        "funding-crowding-reversal",
        "open-interest-divergence",
        "pattern-measuring-objective",
        "failed-breakout-2b",
        "market-profile-value-area",
        "volume-climax-reversal",
        "fib-rsi-bb-confluence",
        "fib-retracement-continuation",
        "fib-projection-reversal",
        "obv-adl-regime",
        "macd-stoch-trend",
        "floor-trader-pivot",
        "range-breakout-1to1",
        "ichimoku-cloud",
        "gap-exhaustion",
        "bollinger-breakout",
        "candlestick-reversal",
        "donchian-breakout",
        "bollinger-reversion",
        "rsi-stoch-reversion",
        "failed-breakout",
        "volume-confirmed-breakout",
        "trend-pullback",
        "trend-pullback-depth",
        "liquidity-sweep-reclaim",
        "breakout-retest",
    }
