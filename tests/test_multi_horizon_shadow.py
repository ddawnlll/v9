"""Synthetic replay fixtures: mechanics only, never an economic result."""
from dataclasses import replace
from decimal import Decimal

import pytest
from nautilus_trader.model import BarType

from v8_next.adapters.adaptive_strategy import AdaptiveSimStrategy, SimArmConfig, SimArmRefused
from v8_next.adapters.horizon_shadow import MultiHorizonShadowPipeline
from v8_next.domain.horizon_aggregation import M15_NS
from v8_next.domain.horizons import Timeframe
from v8_next.domain.market import Candle
from v8_next.domain.observation import QualityState
from v8_next.evaluation.sim_arm import _drive, _Window, run_baseline_arm, run_sim_arm

IID = "BTCUSDT-PERP.BINANCE"
TYPE = f"{IID}-15-MINUTE-LAST-EXTERNAL"
BASE = 1000 * 4 * 60 * 60 * 1_000_000_000


def candle(index: int) -> Candle:
    start = BASE + index * M15_NS
    price = Decimal("100") + Decimal(index) / 10
    return Candle(IID, start, start + M15_NS, price, price + 1, price - 1,
                  price, Decimal("10"), start + M15_NS, start + M15_NS, f"fixture-{index}")


def test_pipeline_uses_only_completed_horizons_and_28_witnesses():
    pipeline = MultiHorizonShadowPipeline(IID)
    for i in range(16):
        source = candle(i)
        belief, bundle = pipeline.advance(source, cut=source.end_ns, opportunity=None, quality=QualityState.OK)
        assert bundle.coverage == 28
        assert bundle.belief_version_hash == belief.version_hash
        # The first H4 interval is unavailable until the 16th source bar.
        if i < 15:
            assert not pipeline._observations[Timeframe.H4]
    assert len(pipeline._observations[Timeframe.H1]) == 4
    assert len(pipeline._observations[Timeframe.H4]) == 1
    assert bundle.decision_time_ns == candle(15).end_ns


def test_pipeline_rejects_future_input_and_gaps():
    pipeline = MultiHorizonShadowPipeline(IID)
    source = candle(0)
    with pytest.raises(ValueError, match="unavailable"):
        pipeline.advance(replace(source, available_ns=source.end_ns + 1),
                         cut=source.end_ns, opportunity=None, quality=QualityState.OK)
    pipeline.advance(source, cut=source.end_ns, opportunity=None, quality=QualityState.OK)
    with pytest.raises(ValueError, match="gap"):
        pipeline.advance(candle(2), cut=candle(2).end_ns, opportunity=None, quality=QualityState.OK)


def test_config_and_runner_refuse_unverified_or_wrong_resolution(tmp_path):
    with pytest.raises(ValueError, match="M15"):
        SimArmConfig(enabled=True, multi_horizon_shadow=True)
    config = SimArmConfig(enabled=True, multi_horizon_shadow=True, bar_type_str=TYPE)
    with pytest.raises(SimArmRefused, match="SHADOW_TAPE_AVAILABILITY_UNPROVEN"):
        run_sim_arm(tape=tmp_path, config=config)
    with pytest.raises(SimArmRefused, match="SHADOW_TAPE_AVAILABILITY_UNPROVEN"):
        run_baseline_arm(tape=tmp_path, config=config)


def test_short_native_shadow_fixture_routes_events_without_orders():
    candles = tuple(candle(i) for i in range(16))
    config = SimArmConfig(enabled=True, multi_horizon_shadow=True, bar_type_str=TYPE)
    strategy = AdaptiveSimStrategy(config, source_candles={c.end_ns: c for c in candles},
                                   source_quality={c.end_ns: QualityState.OK for c in candles})
    window = _Window(IID, "BTCUSDT", BarType.from_str(TYPE), candles, ())
    _drive(window=window, strategy=strategy, funding_dropped_at_load=0,
           initial_balance=Decimal("10000"), maker_fee=Decimal("0.0002"),
           taker_fee=Decimal("0.0005"), execution_profile="baseline")
    assert len(strategy.arm_decisions) == 16
    assert all(d["executed"] == "SHADOW_NO_EXECUTION" and d["advisory_only"]
               for d in strategy.arm_decisions)
    assert strategy.order_markers == []
    witness_rows = {row["expert_id"]: row for row in strategy.arm_decisions[-1]["shadow_inputs"]}
    assert witness_rows["candlestick_reversal"]["primary"] == "M15"
    assert witness_rows["candlestick_reversal"]["warmup_ok"]
    assert witness_rows["trend_pullback"]["primary"] == "H1"
    assert not witness_rows["trend_pullback"]["warmup_ok"]
    assert strategy._horizon_shadow is not None
    assert len(strategy._horizon_shadow._observations[Timeframe.H4]) == 1


def test_native_shadow_rejects_missing_quality_and_late_source_before_any_bookkeeping():
    from types import SimpleNamespace

    config = SimArmConfig(enabled=True, multi_horizon_shadow=True, bar_type_str=TYPE)
    source = candle(0)
    native = SimpleNamespace(ts_event=source.end_ns, ts_init=source.end_ns)
    strategy = AdaptiveSimStrategy(config, source_candles={source.end_ns: source})
    with pytest.raises(SimArmRefused, match="SHADOW_AVAILABILITY_UNPROVEN"):
        strategy.on_bar(native)
    assert not strategy.candles
    late = replace(source, available_ns=source.end_ns + 1)
    strategy = AdaptiveSimStrategy(config, source_candles={late.end_ns: late},
                                   source_quality={late.end_ns: QualityState.OK})
    with pytest.raises(SimArmRefused, match="SHADOW_AVAILABILITY_UNPROVEN"):
        strategy.on_bar(native)
    assert not strategy.candles
