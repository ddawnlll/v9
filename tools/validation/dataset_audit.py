#!/usr/bin/env python
"""Audit the frozen 2025-H1 per-symbol tapes without synthesising anything.

Checks every tape manifest hash, then per bar leg: duplicates, off-grid
timestamps, expected interval, gaps, monotonicity, OHLC sanity, positivity,
window boundaries, and funding coverage. A failure is reported with the exact
instant, never repaired.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

from v8_next.evaluation.multitape import load_multitape

SYMBOLS = (
    "ETHUSDT",
    "SOLUSDT",
    "BNBUSDT",
    "XRPUSDT",
    "DOGEUSDT",
    "ADAUSDT",
    "AVAXUSDT",
    "LINKUSDT",
    "DOTUSDT",
    "LTCUSDT",
)
H1_NS = 3_600_000_000_000
M15_NS = 900_000_000_000
WINDOW_START_MS = 1735689600000  # 2025-01-01T00:00:00Z
WINDOW_END_MS = 1751328000000  # 2025-07-01T00:00:00Z (exclusive)


def _sha_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _audit_leg(
    candles: tuple[Any, ...],
    *,
    interval_ns: int,
    window_start_ms: int,
    window_end_ms: int,
) -> dict[str, Any]:
    duplicates: list[int] = []
    off_grid: list[int] = []
    gaps: list[dict[str, int]] = []
    ohlc_invalid: list[int] = []
    nonpositive: list[int] = []
    misordered: list[int] = []
    previous_end = None
    seen: set[int] = set()
    for candle in candles:
        start_ms = candle.start_ns // 1_000_000
        if start_ms in seen:
            duplicates.append(start_ms)
        seen.add(start_ms)
        if candle.start_ns % interval_ns != 0:
            off_grid.append(start_ms)
        if previous_end is not None and candle.start_ns < previous_end:
            misordered.append(start_ms)
        previous_end = candle.end_ns
        if candle.end_ns - candle.start_ns != interval_ns:
            off_grid.append(start_ms)
        if not (candle.high >= max(candle.open, candle.close) and candle.low <= min(candle.open, candle.close)):
            ohlc_invalid.append(start_ms)
        if candle.volume < 0 or candle.open <= 0 or candle.high <= 0 or candle.low <= 0 or candle.close <= 0:
            nonpositive.append(start_ms)
    if candles:
        expected = (candles[-1].start_ns - candles[0].start_ns) // interval_ns + 1
        if expected > len(candles):
            gaps.append(
                {
                    "first_ms": candles[0].start_ns // 1_000_000,
                    "last_ms": candles[-1].start_ns // 1_000_000,
                    "expected_bars": int(expected),
                    "observed_bars": len(candles),
                    "missing_bars": int(expected - len(candles)),
                }
            )
    return {
        "bars": len(candles),
        "first_open_ms": candles[0].start_ns // 1_000_000 if candles else None,
        "last_open_ms": candles[-1].start_ns // 1_000_000 if candles else None,
        "last_close_ms": candles[-1].end_ns // 1_000_000 if candles else None,
        "window_start_ok": bool(candles) and candles[0].start_ns // 1_000_000 == window_start_ms,
        "window_end_ok": bool(candles) and candles[-1].end_ns // 1_000_000 == window_end_ms,
        "duplicates": duplicates,
        "off_grid": off_grid,
        "gaps": gaps,
        "ohlc_invalid": ohlc_invalid,
        "nonpositive": nonpositive,
        "misordered": misordered,
        "status": "OK"
        if not (duplicates or off_grid or gaps or ohlc_invalid or nonpositive or misordered)
        else "GAPS_OR_DEFECTS",
    }


def audit_symbol(root: Path, symbol: str) -> dict[str, Any]:
    lower = symbol.lower()
    result: dict[str, Any] = {"symbol": symbol, "legs": {}}
    for family, interval_ns in (("1h", H1_NS), ("15m", M15_NS)):
        directory = root / f"{lower}-{family}-2025H1"
        jsonl = directory / "tape.jsonl"
        manifest_path = directory / "tape.manifest.json"
        if not jsonl.is_file() or not manifest_path.is_file():
            result["legs"][family] = {"status": "MISSING", "path": str(directory)}
            continue
        manifest = json.loads(manifest_path.read_text())
        actual = _sha_file(jsonl)
        integrity = "OK" if manifest.get("tape_sha256") == actual else "HASH_MISMATCH"
        loaded = load_multitape(jsonl)
        if symbol not in loaded.candles:
            result["legs"][family] = {"status": "SYMBOL_MISSING", "integrity": integrity}
            continue
        leg = _audit_leg(
            loaded.candles[symbol],
            interval_ns=interval_ns,
            window_start_ms=WINDOW_START_MS,
            window_end_ms=WINDOW_END_MS,
        )
        leg["integrity"] = integrity
        leg["tape_sha256"] = actual
        leg["manifest_sha256"] = _sha_file(manifest_path)
        leg["rows"] = manifest.get("rows")
        leg["archives"] = manifest.get("archives", {})
        leg["symbols_in_tape"] = sorted(loaded.candles)
        if family == "1h":
            funding = [row for row in loaded.funding if row.instrument == symbol]
            times = sorted(row.funding_time_ms for row in funding)
            expected = (WINDOW_END_MS - WINDOW_START_MS) // (8 * 3_600_000)
            leg["funding"] = {
                "rows": len(funding),
                "expected_8h_rows": expected,
                "first_ms": times[0] if times else None,
                "last_ms": times[-1] if times else None,
                "duplicate_times": len(times) - len(set(times)),
                "interval_defaulted": loaded.funding_interval_defaulted,
                "coverage_ok": len(funding) == expected,
                "status": "OK" if len(funding) == expected and len(times) == len(set(times)) else "PARTIAL",
            }
        result["legs"][family] = leg
    statuses = [leg.get("status") for leg in result["legs"].values()]
    result["status"] = "OK" if all(s == "OK" for s in statuses) else "DEFECT"
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--tape-root", type=Path, default=Path("research/tape"))
    parser.add_argument(
        "--out", type=Path, default=Path("reports/v9-cross-market-validation/raw/dataset_audit.json")
    )
    args = parser.parse_args(argv)
    rows = [audit_symbol(args.tape_root, symbol) for symbol in SYMBOLS]
    payload = {"schema_version": "v9-cross-market-dataset-audit-v1", "symbols": rows}
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    for row in rows:
        print(row["symbol"], row["status"], {k: v.get("status") for k, v in row["legs"].items()})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
