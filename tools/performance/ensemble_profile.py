#!/usr/bin/env python3
"""Profile the real single-instrument ensemble with a synthetic mechanics fixture.

The fixture is not market data and the output is not economic evidence. Run in a
fresh process for cold-start timing; cProfile is optional and adds overhead.
"""
from __future__ import annotations

import argparse
import cProfile
import json
import os
import platform
import resource
import statistics
import sys
import time
from decimal import Decimal
from pathlib import Path

START = time.perf_counter()
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bars", type=int, default=100)
    parser.add_argument("--fixture", choices=("flat", "breakout"), default="flat")
    parser.add_argument("--tape", type=Path, help="real hourly tape; omit for synthetic mechanics")
    parser.add_argument("--instrument", default="BTCUSDT")
    parser.add_argument("--start-ms", type=int)
    parser.add_argument("--end-ms", type=int)
    parser.add_argument("--profile", type=Path)
    parser.add_argument("--expert-timings", action="store_true", help="instrument nested expert calls")
    parser.add_argument("--stage-trace", action="store_true", help="trace replay phase boundaries")
    parser.add_argument("--allocations", type=Path, help="write separate tracemalloc snapshot")
    parser.add_argument("--allocation-frames", type=int, default=1, help="tracemalloc stack depth")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.bars < 1:
        parser.error("--bars must be positive")
    if args.allocation_frames < 1:
        parser.error("--allocation-frames must be positive")
    if args.stage_trace and (args.profile or args.expert_timings):
        parser.error("--stage-trace must run separately from other instrumentation")
    if args.allocations and (args.stage_trace or args.profile or args.expert_timings):
        parser.error("--allocations must run separately from other instrumentation")
    from v8_next.adapters.expert_strategy import ExpertStrategyConfig, run_expert_strategy_backtest
    from v8_next.domain.market import Candle

    imported = time.perf_counter()
    if args.allocations:
        import tracemalloc

        tracemalloc.start(args.allocation_frames)
    hour = 3_600_000_000_000
    if args.tape:
        if args.fixture != "flat":
            parser.error("--fixture is only available without --tape")
        if args.tape.is_dir() and (args.start_ms is not None or args.end_ms is not None):
            parser.error("directory loader does not apply window bounds; isolate archives first")
        from v8_next.evaluation.gate_resolution import load_tape_candles

        candles = tuple(load_tape_candles(args.tape, limit=args.bars, instrument=args.instrument,
                                          start_ms=args.start_ms, end_ms=args.end_ms))
        if not candles:
            raise ValueError("tape selection contains no candles")
        workload = "historical tape; availability and provenance require independent verification"
    else:
        if args.instrument != "BTCUSDT" or args.start_ms is not None or args.end_ms is not None:
            parser.error("instrument and window arguments require --tape")
        def fixture_candle(i: int) -> Candle:
            if args.fixture == "breakout" and i == 48:
                op, hi, lo, close, volume = ("101", "122", "100", "120", "500")
            elif args.fixture == "breakout" and i == 49:
                op, hi, lo, close, volume = ("120", "125", "119", "123", "200")
            else:
                op, hi, lo, close, volume = ("100", "100.5", "99.5", "100", "100") if args.fixture == "breakout" else ("100", "101", "99", "100", "100")
            return Candle(
                "BTCUSDT-PERP.BINANCE", i * hour, (i + 1) * hour,
                Decimal(op), Decimal(hi), Decimal(lo), Decimal(close), Decimal(volume),
                (i + 1) * hour, (i + 1) * hour, "synthetic-mechanics",
            )

        candles = tuple(fixture_candle(i) for i in range(args.bars))
        workload = "synthetic mechanics only; not economic evidence"
    prepared = time.perf_counter()
    expert_samples: dict[str, list[float]] = {}
    if args.expert_timings:
        from v8_next.experts import registry

        original_observe = registry.observe_expert

        def timed_observe(expert_id, *observer_args, **observer_kwargs):
            start = time.perf_counter()
            try:
                return original_observe(expert_id, *observer_args, **observer_kwargs)
            finally:
                expert_samples.setdefault(expert_id, []).append(time.perf_counter() - start)

        registry.observe_expert = timed_observe
    profiler = cProfile.Profile() if args.profile else None
    if profiler:
        profiler.enable()
    iid = f"{args.instrument}-PERP.BINANCE"
    config_args = {"instrument_id": iid, "bar_type_str": f"{iid}-1-HOUR-LAST-EXTERNAL"}
    if args.fixture == "breakout":
        config_args.update(max_contradiction_tolerance=28, bracket_stop_pct=Decimal("0.02"),
                           bracket_target_pct=Decimal("0.05"))
    config = ExpertStrategyConfig(**config_args)
    stages = []
    if args.stage_trace:
        from stage_trace import StageTrace

        with StageTrace(run_expert_strategy_backtest) as trace:
            result = run_expert_strategy_backtest(candles, config)
        stages = trace.durations()
    else:
        result = run_expert_strategy_backtest(candles, config)
    if profiler:
        profiler.disable()
        profiler.dump_stats(str(args.profile))
    replayed = time.perf_counter()
    if args.allocations:
        current, peak = tracemalloc.get_traced_memory()
        snapshot = tracemalloc.take_snapshot()
        allocation_summary = {
            "scope": "live traced Python allocations after replay; native allocations are excluded",
            "stack_depth": args.allocation_frames,
            "current_bytes": current,
            "peak_bytes": peak,
            "top_lines": [
                {"location": str(stat.traceback[0]), "bytes": stat.size, "blocks": stat.count}
                for stat in snapshot.statistics("lineno")[:30]
            ],
        }
        args.allocations.parent.mkdir(parents=True, exist_ok=True)
        args.allocations.write_text(json.dumps(allocation_summary, indent=2) + "\n")
        tracemalloc.stop()
    usage = resource.getrusage(resource.RUSAGE_SELF)
    summary = {
        "workload": workload,
        "fixture": args.fixture if not args.tape else None,
        "bars": len(candles),
        "first_start_ns": candles[0].start_ns,
        "last_end_ns": candles[-1].end_ns,
        "decisions": result["decisions_count"],
        "orders": len(result["order_events"]),
        "fills": len(result["fill_records"]),
        "imports_s": imported - START,
        "fixture_s": prepared - imported,
        "production_backtest_s": replayed - prepared,
        "pre_output_s": replayed - START,
        "user_cpu_s": usage.ru_utime,
        "system_cpu_s": usage.ru_stime,
        "peak_rss_bytes": usage.ru_maxrss if platform.system() == "Darwin" else usage.ru_maxrss * 1024,
        "profiled": bool(profiler),
        "expert_timing_instrumented": args.expert_timings,
        "stage_trace_instrumented": args.stage_trace,
        "allocations_instrumented": bool(args.allocations),
        "stages": stages,
        "experts": {
            name: {
                "count": len(samples),
                "inclusive_total_s": sum(samples),
                "mean_s": statistics.mean(samples),
                "p50_s": statistics.median(samples),
                "p95_s": sorted(samples)[max(0, (95 * len(samples) + 99) // 100 - 1)],
                "max_s": max(samples),
                "exclusive_total_s": None,
            }
            for name, samples in expert_samples.items()
        },
        "pid": os.getpid(),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
    print(json.dumps(summary, sort_keys=True))


if __name__ == "__main__":
    main()
