"""The optional forensic tracer must preserve production decision outcomes."""
import sys
from decimal import Decimal
from pathlib import Path

from v8_next.adapters.expert_strategy import ExpertStrategyConfig, run_expert_strategy_backtest
from v8_next.domain.market import Candle

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools" / "performance"))
from expert_trace import ExpertTrace  # noqa: E402
from stage_trace import StageTrace  # noqa: E402


def test_stage_trace_preserves_decisions_and_partitions_the_call() -> None:
    hour = 3_600_000_000_000
    def candle(i):
        values = ((101, 122, 100, 120, 500) if i == 48 else
                  (120, 125, 119, 123, 200) if i == 49 else
                  (100, Decimal("100.5"), Decimal("99.5"), 100, 100))
        return Candle("BTCUSDT-PERP.BINANCE", i * hour, (i + 1) * hour,
                      *(Decimal(v) for v in values), (i + 1) * hour, (i + 1) * hour, "test")

    candles = tuple(candle(i) for i in range(50))
    cfg = ExpertStrategyConfig(max_contradiction_tolerance=28, bracket_stop_pct=Decimal("0.02"),
                               bracket_target_pct=Decimal("0.05"))
    baseline = run_expert_strategy_backtest(candles, cfg)
    with StageTrace(run_expert_strategy_backtest) as trace:
        measured = run_expert_strategy_backtest(candles, cfg)
    with ExpertTrace() as experts:
        expert_measured = run_expert_strategy_backtest(candles, cfg)

    def signatures(result):
        return [(d["action"], d["support_count"], d["contradict_count"], d["abstain_count"],
                 [(s["observer_id"], s["kind"], s["reason"]) for s in d["stances"]])
                for d in result["decisions"]]

    assert signatures(baseline) == signatures(measured)
    assert signatures(baseline) == signatures(expert_measured)
    assert len(baseline["fill_records"]) == len(measured["fill_records"]) == 1
    assert len(expert_measured["fill_records"]) == 1
    samples = experts.results()
    assert len(samples) == 28
    for sample in samples.values():
        assert sample["inclusive_dispatch"]["count"] == len(candles)
        assert sample["observer_self"]["total_s"] <= sample["inclusive_dispatch"]["total_s"]
        assert sample["dispatcher_self"]["total_s"] <= sample["inclusive_dispatch"]["total_s"]
    phases = trace.durations()
    assert len(phases) == 16
    assert all(phase["wall_s"] >= 0 for phase in phases)
    assert abs(sum(phase["wall_s"] for phase in phases)
               - (trace.starts[-1]["timestamp"] - trace.starts[0]["timestamp"])) < 1e-9
    assert {"engine_run", "data_add", "native_reports", "engine_dispose"} <= {
        phase["phase"] for phase in phases
    }
