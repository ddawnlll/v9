#!/usr/bin/env python3
"""Run the production multi-instrument portfolio on synthetic mechanics fixtures.

No rows or economic results here constitute historical market evidence. This
fixture includes one breakout and eight-hour synthetic funding updates per leg.
"""
from __future__ import annotations

import argparse
import cProfile
import json
import platform
import resource
import sys
import time
from decimal import Decimal
from pathlib import Path

START = time.perf_counter()
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bars", type=int, default=100)
    parser.add_argument("--instruments", type=int, default=1)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--profile", type=Path)
    args = parser.parse_args()
    if args.bars < 1 or not 1 <= args.instruments <= 10:
        parser.error("bars must be positive; instruments must be between 1 and 10")
    from v8_next.adapters.portfolio_backtest import (
        BASE_CURRENCIES,
        SleeveSpec,
        run_portfolio_backtest,
    )
    from v8_next.domain.market import Candle
    from v8_next.evaluation.multitape import FundingRow

    imported = time.perf_counter()
    symbols = (["BTCUSDT"] + sorted(symbol for symbol in BASE_CURRENCIES if symbol != "BTCUSDT"))[
        :args.instruments
    ]
    hour = 3_600_000_000_000
    legs = {}
    for symbol in symbols:
        candles = []
        for i in range(args.bars):
            vals = ((101, 122, 100, 120, 500) if i == 48 else
                    (120, 121, 119, 120, 200) if 49 <= i < 58 else
                    (100, Decimal("100.5"), Decimal("99.5"), 100, 100))
            end = (i + 1) * hour
            candles.append(Candle(f"{symbol}-PERP.BINANCE", i * hour, end,
                                  *(Decimal(v) for v in vals), end, end, "synthetic-mechanics"))
        legs[symbol] = tuple(candles)
    funding = tuple(
        FundingRow(symbol, boundary // 1_000_000, Decimal("0.0001"), 8.0, boundary)
        for symbol in symbols
        for boundary in range(8 * hour, args.bars * hour + 1, 8 * hour)
    )
    prepared = time.perf_counter()
    profiler = cProfile.Profile() if args.profile else None
    if profiler:
        profiler.enable()
    result = run_portfolio_backtest(legs, (SleeveSpec("mechanics", 1, 28, 1.0),), funding,
                                    execution_profile="baseline")
    if profiler:
        profiler.disable()
        profiler.dump_stats(str(args.profile))
    replayed = time.perf_counter()
    usage = resource.getrusage(resource.RUSAGE_SELF)
    summary = {
        "workload": "synthetic portfolio mechanics only; not economic evidence",
        "symbols": symbols,
        "bars_per_instrument": args.bars,
        "bars": result["total_bars"],
        "decisions": result["decisions_count"],
        "fills": len(result["fill_records"]),
        "opened_positions": len(result["opened_positions"]),
        "closed_positions": len(result["closed_positions"]),
        "native_position_adjustments": sum(
            len(position["adjustments"]) for position in result["account"]["positions"]
        ),
        "funding_rows": len(funding),
        "funding_settlements_fed": result["funding_settlements_fed"],
        "funding_coverage": result["funding_coverage"],
        "funding_missing_mark": result["funding_missing_mark"],
        "execution_profile": "baseline",
        "profiled": bool(profiler),
        "imports_s": imported - START,
        "fixture_s": prepared - imported,
        "production_backtest_s": replayed - prepared,
        "pre_output_s": replayed - START,
        "user_cpu_s": usage.ru_utime,
        "system_cpu_s": usage.ru_stime,
        "peak_rss_bytes": usage.ru_maxrss if platform.system() == "Darwin" else usage.ru_maxrss * 1024,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
    print(json.dumps(summary, sort_keys=True))


if __name__ == "__main__":
    main()
