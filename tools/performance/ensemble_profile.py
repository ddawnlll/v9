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
    parser.add_argument("--tape", type=Path, help="real hourly tape; omit for synthetic mechanics")
    parser.add_argument("--instrument", default="BTCUSDT")
    parser.add_argument("--start-ms", type=int)
    parser.add_argument("--end-ms", type=int)
    parser.add_argument("--profile", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.bars < 1:
        parser.error("--bars must be positive")
    from v8_next.adapters.expert_strategy import ExpertStrategyConfig, run_expert_strategy_backtest
    from v8_next.domain.market import Candle

    imported = time.perf_counter()
    hour = 3_600_000_000_000
    if args.tape:
        from v8_next.evaluation.gate_resolution import load_tape_candles

        candles = tuple(load_tape_candles(args.tape, limit=args.bars, instrument=args.instrument,
                                          start_ms=args.start_ms, end_ms=args.end_ms))
        if not candles:
            raise ValueError("tape selection contains no candles")
        workload = "historical tape; availability and provenance require independent verification"
    else:
        if args.instrument != "BTCUSDT" or args.start_ms is not None or args.end_ms is not None:
            parser.error("instrument and window arguments require --tape")
        candles = tuple(
        Candle("BTCUSDT-PERP.BINANCE", i * hour, (i + 1) * hour,
               Decimal("100"), Decimal("101"), Decimal("99"), Decimal("100"),
               Decimal("100"), (i + 1) * hour, (i + 1) * hour, "synthetic-mechanics")
        for i in range(args.bars)
        )
        workload = "synthetic mechanics only; not economic evidence"
    prepared = time.perf_counter()
    profiler = cProfile.Profile() if args.profile else None
    if profiler:
        profiler.enable()
    iid = f"{args.instrument}-PERP.BINANCE"
    config = ExpertStrategyConfig(instrument_id=iid, bar_type_str=f"{iid}-1-HOUR-LAST-EXTERNAL")
    result = run_expert_strategy_backtest(candles, config)
    if profiler:
        profiler.disable()
        profiler.dump_stats(str(args.profile))
    replayed = time.perf_counter()
    usage = resource.getrusage(resource.RUSAGE_SELF)
    summary = {
        "workload": workload,
        "bars": len(candles),
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
        "pid": os.getpid(),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
    print(json.dumps(summary, sort_keys=True))


if __name__ == "__main__":
    main()
