#!/usr/bin/env python3
"""Replay the production multi-instrument portfolio on real hourly development tape.

This is a measurement wrapper, not new backtesting infrastructure: it loads
venue archives and calls the unchanged ``run_portfolio_backtest`` adapter.

Funding rows come from the vendor ``<SYMBOL>-fundingRate-<month>.zip`` archives.
Only the archives in the supplied tape directory are read, so the caller
controls the authorized window. Output is a performance record only; no
economic claim is made or implied.
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
import zipfile
from decimal import Decimal
from pathlib import Path

START = time.perf_counter()
#: Same instant on the system monotonic clock, so a parent sampler can align
#: this process's internal phase offsets with its samples.
START_MONOTONIC = time.monotonic()
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))


def _load_funding(tape_dir: Path, symbols: list[str], start_ms: int, end_ms: int) -> tuple[list, int]:
    import csv
    import io

    from v8_next.evaluation.multitape import FundingRow

    rows: list[FundingRow] = []
    dropped = 0
    for symbol in symbols:
        for zf_path in sorted(tape_dir.glob(f"{symbol}-fundingRate-*.zip")):
            with zipfile.ZipFile(zf_path) as archive:
                for name in archive.namelist():
                    if not name.endswith(".csv"):
                        continue
                    with archive.open(name) as handle:
                        for row in csv.DictReader(io.TextIOWrapper(handle, encoding="utf-8")):
                            try:
                                funding_ms = int(row["calc_time"])
                                rate = Decimal(str(row["last_funding_rate"]))
                                interval = float(row.get("funding_interval_hours") or 8.0)
                            except (KeyError, ValueError):
                                dropped += 1
                                continue
                            if not start_ms <= funding_ms <= end_ms:
                                continue
                            rows.append(
                                FundingRow(
                                    instrument=symbol,
                                    funding_time_ms=funding_ms,
                                    funding_rate=rate,
                                    interval_hours=interval,
                                    received_ns=None,
                                )
                            )
    rows.sort(key=lambda row: (row.funding_time_ms, row.instrument))
    return rows, dropped


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tape", type=Path, required=True, help="isolated directory of monthly archives")
    parser.add_argument("--symbols", required=True, help="comma-separated raw symbols, e.g. BTCUSDT,ETHUSDT")
    parser.add_argument("--bars", type=int, required=True, help="candles per instrument")
    parser.add_argument("--with-funding", action="store_true", help="feed funding settlements from the same archive set")
    parser.add_argument("--profile", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    from v8_next.adapters.portfolio_backtest import SleeveSpec, run_portfolio_backtest
    from v8_next.evaluation.gate_resolution import load_tape_candles

    imported = time.perf_counter()
    symbols = [part.strip() for part in args.symbols.split(",") if part.strip()]
    if not symbols:
        parser.error("--symbols must name at least one instrument")
    legs: dict[str, tuple] = {}
    per_leg = {}
    for symbol in symbols:
        candles = tuple(load_tape_candles(args.tape, limit=args.bars, instrument=symbol))
        if not candles:
            raise ValueError(f"no candles for {symbol} in {args.tape}")
        legs[symbol] = candles
        per_leg[symbol] = len(candles)
    funding = ()
    funding_dropped = 0
    if args.with_funding:
        first = min(leg[0].start_ns for leg in legs.values())
        last = max(leg[-1].end_ns for leg in legs.values())
        funding_rows, funding_dropped = _load_funding(
            args.tape, symbols, first // 1_000_000, last // 1_000_000
        )
        funding = tuple(funding_rows)
    loaded = time.perf_counter()

    profiler = cProfile.Profile() if args.profile else None
    if profiler:
        profiler.enable()
    result = run_portfolio_backtest(
        legs,
        (SleeveSpec("tape-replay", 1, 28, 1.0),),
        funding,
        funding_dropped=funding_dropped,
        execution_profile="baseline",
    )
    if profiler:
        profiler.disable()
        profiler.dump_stats(str(args.profile))
    replayed = time.perf_counter()

    usage = resource.getrusage(resource.RUSAGE_SELF)
    summary = {
        "workload": "historical development tape; performance record only, no economic claim",
        "symbols": symbols,
        "bars_per_instrument": per_leg,
        "bars": result["total_bars"],
        "decisions": result["decisions_count"],
        "orders": len(result["order_events"]) if "order_events" in result else None,
        "fills": len(result["fill_records"]),
        "funding_rows_supplied": len(funding),
        "funding_rows_dropped": funding_dropped,
        "funding_settlements_fed": result.get("funding_settlements_fed"),
        "funding_missing_mark": result.get("funding_missing_mark"),
        "execution_profile": "baseline",
        "profiled": bool(profiler),
        "imports_s": imported - START,
        "fixture_s": loaded - imported,
        "production_backtest_s": replayed - loaded,
        "pre_output_s": replayed - START,
        "user_cpu_s": usage.ru_utime,
        "system_cpu_s": usage.ru_stime,
        "peak_rss_bytes": usage.ru_maxrss if platform.system() == "Darwin" else usage.ru_maxrss * 1024,
        "pid": os.getpid(),
        "start_monotonic_s": START_MONOTONIC,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
    print(json.dumps(summary, sort_keys=True))


if __name__ == "__main__":
    main()
