#!/usr/bin/env python3
"""Compare the production continuity predicate with isolated Goose and NumPy.

Timings cover only the predicate after input preparation; they do not compare
complete frame creation, research engines, input conversion, FFI, or backtests.
Only the explicitly approved BTC Jan–Mar 2025 development archives are accepted.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import resource
import statistics
import subprocess
import sys
import tempfile
import time
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
import numpy as np  # noqa: E402

from v8_next.domain.market import Candle, CausalFrame  # noqa: E402
from v8_next.evaluation.gate_resolution import load_tape_candles  # noqa: E402

EXPECTED = {
    "BTCUSDT-1h-2025-01.zip": "9ebc05c9b3d5ab3591edf65bc5c7e5dbc2f96c1efc4adc4ea198c651a99a41b1",
    "BTCUSDT-1h-2025-02.zip": "077d761098eb5f2dbde65457833b9edab41ea6475284a77fcce0d4bec1bfb5cf",
    "BTCUSDT-1h-2025-03.zip": "08043384c3336b0fa3eeee06e113a6f1eceb6d3fbe7b2971e9b9fa83625fdfb9",
}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tape", required=True, type=Path)
    parser.add_argument("--goose-executable", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--repetitions", type=int, default=5)
    args = parser.parse_args()
    if args.repetitions < 3:
        parser.error("at least three repetitions are required")
    actual_names = {p.name for p in args.tape.glob("BTCUSDT-1h-*.zip")}
    if actual_names != set(EXPECTED):
        raise ValueError("tape directory must contain only the three pinned development archives")
    for name, expected in EXPECTED.items():
        if hashlib.sha256((args.tape / name).read_bytes()).hexdigest() != expected:
            raise ValueError(f"data hash mismatch: {name}")
    candles = tuple(load_tape_candles(args.tape, instrument="BTCUSDT", limit=2160))
    if (len(candles), candles[0].start_ns, candles[-1].end_ns) != (
        2160, 1735689600000000000, 1743465600000000000
    ):
        raise ValueError("development archive window or bar count mismatch")
    csv_data = "".join(f"{c.start_ns},{c.end_ns}\n" for c in candles)
    starts = np.array([c.start_ns for c in candles], dtype=np.int64)
    ends = np.array([c.end_ns for c in candles], dtype=np.int64)
    samples = []
    with tempfile.TemporaryDirectory() as tmp:
        input_path = Path(tmp) / "timestamps.csv"
        fixtures = {
            "empty": [], "single": [(0, 1)], "contiguous": [(0, 1), (1, 2), (2, 3)],
            "early_gap": [(0, 1), (2, 3), (3, 4)],
            "late_gap": [(0, 1), (1, 2), (3, 4)],
        }
        for name, pairs in fixtures.items():
            test_candles = tuple(
                Candle("BTCUSDT-PERP.BINANCE", a, b, Decimal(100), Decimal(101), Decimal(99),
                       Decimal(100), Decimal(1), b, b, "synthetic-parity") for a, b in pairs
            )
            expected = "".join(
                "1" if CausalFrame("BTCUSDT-PERP.BINANCE", test_candles[n - 1].end_ns,
                                   test_candles[:n]).continuous else "0"
                for n in range(1, len(test_candles) + 1)
            )
            input_path.write_text("".join(f"{a},{b}\n" for a, b in pairs))
            checked = subprocess.run([str(args.goose_executable.resolve()), str(input_path)],
                                     check=True, capture_output=True, text=True)
            fixture_fields = dict(line.split("=", 1) for line in checked.stdout.splitlines())
            if fixture_fields["results"] != expected:
                raise ValueError(f"fixture parity failed: {name}")
        input_path.write_text(csv_data)
        for _ in range(args.repetitions):
            # Prepare all prefixes first so the timer isolates the actual cached_property's
            # first computation. This intentionally excludes production frame validation.
            frames = [CausalFrame(candles[0].instrument_id, candles[n - 1].end_ns,
                                  candles[:n], _trusted_order=True)
                      for n in range(1, len(candles) + 1)]
            before = time.perf_counter()
            reference = "".join("1" if frame.continuous else "0" for frame in frames)
            python_s = time.perf_counter() - before
            before = time.perf_counter()
            numpy_bits = np.concatenate((np.array([True]), np.logical_and.accumulate(ends[:-1] == starts[1:])))
            numpy_s = time.perf_counter() - before
            numpy_result = "".join("1" if value else "0" for value in numpy_bits)
            before = time.perf_counter()
            completed = subprocess.run([str(args.goose_executable.resolve()), str(input_path)],
                                       check=True, capture_output=True, text=True)
            goose_process_s = time.perf_counter() - before
            fields = dict(line.split("=", 1) for line in completed.stdout.splitlines())
            if fields["results"] != reference or numpy_result != reference:
                raise ValueError("calculation parity failed")
            samples.append({"python_predicate_s": python_s, "numpy_predicate_s": numpy_s,
                            "goose_predicate_s": int(fields["compute_ns"]) / 1e9,
                            "goose_read_parse_s": int(fields["load_ns"]) / 1e9,
                            "goose_process_s": goose_process_s})
    result = {
        "scope": "prefix continuity predicate only; no economic or engine parity claim",
        "bars": len(candles), "parity": "PASS",
        "synthetic_parity_fixtures": list(fixtures),
        "input_sha256": hashlib.sha256(csv_data.encode()).hexdigest(),
        "output_sha256": hashlib.sha256(reference.encode()).hexdigest(),
        "samples": samples,
        "median_s": {key: statistics.median(s[key] for s in samples) for key in samples[0]},
        "python_self_maxrss_raw": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
        "goose_children_maxrss_raw": resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss,
        "rss_units": "bytes on macOS, KiB on Linux; process high-water, not predicate allocations",
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
