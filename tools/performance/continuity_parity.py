#!/usr/bin/env python3
"""Check a narrow Goose continuity prototype against production CausalFrame.

Requires a separately compiled continuity.goose executable. The input is
synthetic and validates only the continuity predicate, not frame construction,
trading decisions, execution, or engine equivalence.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from v8_next.domain.market import Candle, CausalFrame  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--executable", type=Path, required=True)
    args = parser.parse_args()
    cases = {
        "empty": [],
        "single": [(0, 1)],
        "contiguous": [(i, i + 1) for i in range(100)],
        "gap_early": [(0, 1), (2, 3), (3, 4)],
        "gap_late": [(0, 1), (1, 2), (3, 4)],
        "long_contiguous": [(i, i + 1) for i in range(1000)],
    }
    observed = {}
    for name, pairs in cases.items():
        candles = tuple(
            Candle("BTCUSDT-PERP.BINANCE", start, end, Decimal(100), Decimal(101),
                   Decimal(99), Decimal(100), Decimal(1), end, end, "synthetic-continuity")
            for start, end in pairs
        )
        expected = [
            "1" if CausalFrame("BTCUSDT-PERP.BINANCE", candles[n - 1].end_ns,
                               candles[:n]).continuous else "0"
            for n in range(1, len(candles) + 1)
        ]
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "input.csv"
            path.write_text("".join(f"{start},{end}\n" for start, end in pairs))
            result = subprocess.run([str(args.executable.resolve()), str(path)], check=True,
                                    capture_output=True, text=True)
        actual = result.stdout.splitlines()
        if actual != expected:
            raise AssertionError(f"continuity mismatch: {name}")
        observed[name] = len(expected)
    print(json.dumps({"parity": "PASS", "cases": observed, "scope": "continuity predicate only"}))


if __name__ == "__main__":
    main()
