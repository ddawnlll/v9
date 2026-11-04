"""Tests for ExpertEnsembleStrategy: 28-expert integration with NautilusTrader."""

from decimal import Decimal

import pytest
from nautilus_trader.model import BarType, InstrumentId

from v8_next.adapters.expert_strategy import (
    ExpertEnsembleStrategy,
    ExpertStrategyConfig,
    run_expert_strategy_backtest,
)
from v8_next.domain.market import Candle
from v8_next.experts.registry import CANONICAL_28_EXPERTS

HOUR_NS = 3600 * 10**9


def make_test_candle(
    i: int,
    open_: float,
    high: float,
    low: float,
    close: float,
    volume: float = 100.0,
    instrument_id: str = "BTCUSDT-PERP.BINANCE",
) -> Candle:
    return Candle(
        instrument_id,
        i * HOUR_NS,
        (i + 1) * HOUR_NS,
        Decimal(str(open_)),
        Decimal(str(high)),
        Decimal(str(low)),
        Decimal(str(close)),
        Decimal(str(volume)),
        (i + 1) * HOUR_NS,
        (i + 1) * HOUR_NS,
        "test-strategy-fixture",
    )


def test_strategy_lifecycle_and_subscription():
    config = ExpertStrategyConfig(
        instrument_id="BTCUSDT-PERP.BINANCE",
        bar_type_str="BTCUSDT-PERP.BINANCE-1-HOUR-LAST-EXTERNAL",
    )
    strategy = ExpertEnsembleStrategy(config)
    assert strategy.instrument_id == InstrumentId.from_str("BTCUSDT-PERP.BINANCE")
    assert strategy.bar_type == BarType.from_str("BTCUSDT-PERP.BINANCE-1-HOUR-LAST-EXTERNAL")
    assert len(strategy.candles) == 0
    assert len(strategy.decisions) == 0


def test_every_bar_invokes_all_28_canonical_active_experts():
    """Invariant: on every native Bar tick, all 28 canonical active witnesses are invoked."""
    candles = tuple(
        make_test_candle(i, 100 + i * 0.1, 101 + i * 0.1, 99 + i * 0.1, 100 + i * 0.1)
        for i in range(55)
    )
    result = run_expert_strategy_backtest(
        candles,
        ExpertStrategyConfig(min_support_quorum=999),  # Observation only, no trades
        maker_fee=Decimal("0.0002"),
        taker_fee=Decimal("0.0005"),
        initial_balance=Decimal("10000"),
    )

    assert result["total_bars"] == 55
    assert result["decisions_count"] == 55
    decisions = result["decisions"]

    for d in decisions:
        assert d["support_count"] + d["contradict_count"] + d["abstain_count"] == 28
        assert len(d["stances"]) == 28
        observer_ids = [s["observer_id"] for s in d["stances"]]
        assert observer_ids == list(CANONICAL_28_EXPERTS)


def test_supported_opportunity_triggers_native_order_and_fill():
    """When an opportunity is supported by quorum, NautilusTrader submits and fills native orders."""
    # Build 48 flat bars around 100, bar 49 breaks out strongly to 120 (LONG breakout)
    candles_list = [make_test_candle(i, 100, 100.5, 99.5, 100) for i in range(48)]
    # Bar 49 breaks high of prior 48 bars (close=120 > 100.5)
    candles_list.append(make_test_candle(48, 101, 122, 100, 120, volume=500.0))
    # Bar 50 continues
    candles_list.append(make_test_candle(49, 120, 125, 119, 123, volume=200.0))
    candles = tuple(candles_list)

    config = ExpertStrategyConfig(
        min_support_quorum=1,
        max_contradiction_tolerance=28,  # allow trade if at least 1 expert supports
        order_quantity=Decimal("0.010"),
    )

    result = run_expert_strategy_backtest(candles, config)

    # Opportunity was detected on bar 49
    decisions = result["decisions"]
    breakout_decision = decisions[48]
    assert breakout_decision["opportunity"] is not None
    assert breakout_decision["opportunity"]["direction"] == "LONG"
    assert breakout_decision["support_count"] >= 1
    assert breakout_decision["consensus_supported"] is True
    assert "SUBMITTED_MARKET_BUY" in breakout_decision["action"]

    # Nautilus native execution filled the order and opened a position
    assert len(result["order_events"]) > 0
    assert len(result["opened_positions"]) >= 1
    opened = result["opened_positions"][0]
    assert opened["side"] == "LONG"
    assert Decimal(opened["quantity"]) == Decimal("0.010")


def test_bracket_orders_submitted_when_protection_configured():
    """When bracket stops/targets are configured, Strategy submits native bracket order lists."""
    candles_list = [make_test_candle(i, 100, 100.5, 99.5, 100) for i in range(48)]
    candles_list.append(make_test_candle(48, 101, 122, 100, 120, volume=500.0))
    candles = tuple(candles_list)

    config = ExpertStrategyConfig(
        min_support_quorum=1,
        max_contradiction_tolerance=28,
        order_quantity=Decimal("0.010"),
        bracket_stop_pct=Decimal("0.02"),  # 2% stop
        bracket_target_pct=Decimal("0.05"),  # 5% target
    )

    result = run_expert_strategy_backtest(candles, config)
    breakout_decision = result["decisions"][48]
    assert "SUBMITTED_BRACKET_MARKET_BUY" in breakout_decision["action"]


def test_fail_closed_on_insufficient_quorum_submits_zero_orders():
    """Fail-closed contract: when support quorum is not reached, zero orders are submitted."""
    candles = tuple(
        make_test_candle(i, 100, 100.5, 99.5, 100)
        for i in range(60)
    )
    config = ExpertStrategyConfig(min_support_quorum=5)
    result = run_expert_strategy_backtest(candles, config)

    assert len(result["order_events"]) == 0
    assert len(result["opened_positions"]) == 0
    assert all(d["action"] == "NO_ACTION" for d in result["decisions"])


def test_end_to_end_backtest_with_economic_accounting():
    """End-to-end backtest verifies NautilusTrader native economic accounting (margin, equity, fees)."""
    candles_list = [make_test_candle(i, 100, 100.5, 99.5, 100) for i in range(48)]
    candles_list.append(make_test_candle(48, 101, 122, 100, 120, volume=500.0))
    candles_list.append(make_test_candle(49, 120, 125, 119, 123, volume=200.0))
    candles = tuple(candles_list)

    initial_balance = Decimal("10000")
    maker_fee = Decimal("0.0002")
    taker_fee = Decimal("0.0005")

    result = run_expert_strategy_backtest(
        candles,
        ExpertStrategyConfig(
            min_support_quorum=1,
            max_contradiction_tolerance=28,
            order_quantity=Decimal("0.010"),
        ),
        maker_fee=maker_fee,
        taker_fee=taker_fee,
        initial_balance=initial_balance,
    )

    account = result["account"]
    assert account["currency"] == "USDT"
    # An open position was created, so taker fee was paid from balance_total
    balance_amount = Decimal(account["balance_total"].split()[0])
    assert balance_amount < initial_balance
    assert len(account["orders"]) >= 1
    assert len(account["positions"]) >= 1


def test_half_configured_bracket_fails_closed() -> None:
    with pytest.raises(ValueError, match="bracket_stop_pct and bracket_target_pct"):
        ExpertStrategyConfig(bracket_stop_pct=Decimal("0.02"))
    with pytest.raises(ValueError, match="bracket_stop_pct and bracket_target_pct"):
        ExpertStrategyConfig(bracket_target_pct=Decimal("0.04"))
