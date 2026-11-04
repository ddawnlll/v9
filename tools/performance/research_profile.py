#!/usr/bin/env python3
"""Exercise BenchmarkRunner, its ledger, receipt and HTML on synthetic data."""
from __future__ import annotations

import argparse
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
    parser.add_argument("--tape", type=Path, help="isolated BTC development archives or tape")
    parser.add_argument("--artifacts", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.bars < 1:
        parser.error("bars must be positive")
    if args.artifacts.exists() and any(args.artifacts.iterdir()):
        parser.error("artifacts directory must be empty to avoid ledger contamination")
    from v8_next.adapters.expert_strategy import ExpertStrategyConfig
    from v8_next.domain.market import Candle
    from v8_next.evaluation.report import generate_forensic_html_report
    from v8_next.evaluation.runner import BenchmarkCase, BenchmarkRunner

    imported = time.perf_counter()
    hour = 3_600_000_000_000
    synthetic_epoch = 1_609_459_200_000_000_000  # 2021-01-01 UTC; fixture, not a market window
    if args.tape:
        from v8_next.evaluation.gate_resolution import load_tape_candles

        candles = tuple(load_tape_candles(args.tape, limit=args.bars, instrument="BTCUSDT"))
        if not candles:
            raise ValueError("tape selection contains no candles")
        dataset = "BTCUSDT_DEVELOPMENT_ARCHIVES"
        population = "BurnedDiagnosticReal"
        workload = "historical development mechanics; no economic claim"
    else:
        candles = tuple(
            Candle("BTCUSDT-PERP.BINANCE", synthetic_epoch + i * hour,
                   synthetic_epoch + (i + 1) * hour,
                   Decimal(100), Decimal(101), Decimal(99), Decimal(100), Decimal(100),
                   synthetic_epoch + (i + 1) * hour, synthetic_epoch + (i + 1) * hour,
                   "synthetic-mechanics")
            for i in range(args.bars)
        )
        dataset = "SYNTHETIC_NOT_MARKET_DATA"
        population = "FoundrySyntheticNovelty"
        workload = "synthetic research mechanics only; not economic evidence"
    case = BenchmarkCase(case_id="historical-development" if args.tape else "synthetic-mechanics",
                         policy_id="ensemble-mechanics",
                         dataset_name=dataset,
                         allowed_populations=(population,),
                         strategy_config=ExpertStrategyConfig())
    prepared = time.perf_counter()
    runner = BenchmarkRunner(output_dir=args.artifacts)
    result = runner.run(case, candles, execution_profile="baseline")
    ran = time.perf_counter()
    result_path = args.artifacts / "result.json"
    result_path.write_text(result.model_dump_json(indent=2) + "\n")
    serialized = time.perf_counter()
    html_path = args.artifacts / "forensic.html"
    valid = generate_forensic_html_report(result.receipt, html_path, result.certificate)
    rendered = time.perf_counter()
    usage = resource.getrusage(resource.RUSAGE_SELF)
    summary = {
        "workload": workload,
        "bars": len(candles),
        "first_start_ns": candles[0].start_ns,
        "last_end_ns": candles[-1].end_ns,
        "trades": result.total_trades,
        "certificate_status": result.certificate.status,
        "receipt_html_verified": valid,
        "artifact_files": sorted(path.name for path in args.artifacts.iterdir() if path.is_file()),
        "artifact_bytes": sum(path.stat().st_size for path in args.artifacts.iterdir() if path.is_file()),
        "imports_s": imported - START,
        "fixture_s": prepared - imported,
        "runner_and_ledger_s": ran - prepared,
        "result_json_write_s": serialized - ran,
        "forensic_html_write_s": rendered - serialized,
        "pre_output_s": rendered - START,
        "user_cpu_s": usage.ru_utime,
        "system_cpu_s": usage.ru_stime,
        "peak_rss_bytes": usage.ru_maxrss if platform.system() == "Darwin" else usage.ru_maxrss * 1024,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
    print(json.dumps(summary, sort_keys=True))
    if not valid:
        raise RuntimeError("forensic report verification failed")


if __name__ == "__main__":
    main()
