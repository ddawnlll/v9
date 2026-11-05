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
    native = SimpleNamespace(bar_type=BarType.from_str(TYPE), ts_event=source.end_ns, ts_init=source.end_ns)
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


def test_native_shadow_rejects_different_native_prices_before_processing():
    from types import SimpleNamespace

    source = candle(0)
    config = SimArmConfig(enabled=True, multi_horizon_shadow=True, bar_type_str=TYPE)
    strategy = AdaptiveSimStrategy(config, source_candles={source.end_ns: source},
                                   source_quality={source.end_ns: QualityState.OK})
    price = lambda value: SimpleNamespace(as_decimal=lambda: value)  # noqa: E731
    native = SimpleNamespace(bar_type=BarType.from_str(TYPE), ts_event=source.end_ns, ts_init=source.end_ns,
                             open=price(source.open), high=price(source.high),
                             low=price(source.low), close=price(source.close + 1),
                             volume=price(source.volume))
    with pytest.raises(SimArmRefused, match="SHADOW_SOURCE_BAR_MISMATCH"):
        strategy.on_bar(native)
    assert not strategy.candles


@pytest.mark.parametrize("bar_type", [
    "ETHUSDT-PERP.BINANCE-15-MINUTE-LAST-EXTERNAL",
    f"{IID}-1-HOUR-LAST-EXTERNAL",
    f"{IID}-15-MINUTE-MID-EXTERNAL",
])
def test_native_shadow_rejects_wrong_native_bar_identity(bar_type):
    from types import SimpleNamespace

    source = candle(0)
    config = SimArmConfig(enabled=True, multi_horizon_shadow=True, bar_type_str=TYPE)
    strategy = AdaptiveSimStrategy(config, source_candles={source.end_ns: source},
                                   source_quality={source.end_ns: QualityState.OK})
    price = lambda value: SimpleNamespace(as_decimal=lambda: value)  # noqa: E731
    spoof = SimpleNamespace(bar_type=BarType.from_str(bar_type), ts_event=source.end_ns,
                            ts_init=source.end_ns, open=price(source.open), high=price(source.high),
                            low=price(source.low), close=price(source.close), volume=price(source.volume))
    with pytest.raises(SimArmRefused, match="SHADOW_BAR_TYPE_MISMATCH"):
        strategy.on_bar(spoof)
    assert not strategy.candles and not strategy.arm_decisions


def test_book_opportunity_binds_the_grammar_identity():
    from v8_next.economics.decisions import Opportunity

    source = candle(0)
    config = SimArmConfig(enabled=True, multi_horizon_shadow=True, bar_type_str=TYPE)
    strategy = AdaptiveSimStrategy(config, source_candles={source.end_ns: source},
                                   source_quality={source.end_ns: QualityState.OK})
    opportunity = Opportunity("grammar-id", "exposure", IID, "LONG", source.end_ns,
                              source.end_ns + M15_NS)
    record = strategy._remember_opportunity(opportunity, source)
    assert record is not None
    assert record.opportunity_id != opportunity.opportunity_id
    assert record.lineage_hash == opportunity.opportunity_id
    assert record.metadata["shadow_source_opportunity_id"] == opportunity.opportunity_id


def test_shadow_rejects_witness_identity_and_opportunity_mismatches():
    from types import SimpleNamespace

    from v8_next.adapters.horizon_shadow import _verify_witness_bindings
    from v8_next.economics.decisions import Opportunity, StanceKind
    from v8_next.experts.registry import CANONICAL_28_EXPERTS

    cut = candle(0).end_ns
    opportunity = Opportunity("grammar-id", "exposure", IID, "LONG", cut, cut + M15_NS)
    witnesses = tuple(SimpleNamespace(
        context=SimpleNamespace(profile=SimpleNamespace(expert_id=expert)),
        stance=SimpleNamespace(observer_id=expert, opportunity_id="grammar-id",
                               decision_ns=cut, kind=StanceKind.SUPPORT),
    ) for expert in CANONICAL_28_EXPERTS)
    _verify_witness_bindings(witnesses, opportunity, cut)
    altered = list(witnesses)
    altered[0] = SimpleNamespace(context=witnesses[0].context,
        stance=SimpleNamespace(observer_id=CANONICAL_28_EXPERTS[0], opportunity_id="wrong",
                               decision_ns=cut, kind=StanceKind.SUPPORT))
    with pytest.raises(ValueError, match="opportunity mismatch"):
        _verify_witness_bindings(tuple(altered), opportunity, cut)
    with pytest.raises(ValueError, match="identity or order"):
        _verify_witness_bindings(witnesses[:-1] + witnesses[:1], opportunity, cut)
    with pytest.raises(ValueError, match="opportunity mismatch"):
        _verify_witness_bindings(witnesses, None, cut)
