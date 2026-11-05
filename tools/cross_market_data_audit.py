#!/usr/bin/env python
"""Data-quality audit for the 10-symbol 2025-H1 tape set.

Reads the verified tapes (never the raw archives) and reports, per symbol and
channel: bar counts, coverage against the calendar, missing/duplicate slots,
timeliness of the event clock, close/open discontinuities, funding coverage and
the derivable completed-H4 count.

Nothing is repaired here. A failed check is reported as a gap so the affected
experiment can be marked ``INVALID``/``NOT_RUN`` instead of silently measured.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Sequence

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from v8_next.evaluation.instrument_metadata import filters_for  # noqa: E402
from v8_next.evaluation.multitape import load_multitape  # noqa: E402

UNIVERSE = [
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
]

WINDOW_START_MS = 1_735_689_600_000  # 2025-01-01T00:00:00Z
WINDOW_END_MS = 1_751_328_000_000  # 2025-07-01T00:00:00Z (exclusive)
H1_MS = 3_600_000
M15_MS = 900_000


def iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def audit_channel(
    candles: Sequence[Any], step_ms: int, tick: Decimal | None = None
) -> dict[str, Any]:
    starts = [c.start_ns // 1_000_000 for c in candles]
    ends = [c.end_ns // 1_000_000 for c in candles]
    expected = (WINDOW_END_MS - WINDOW_START_MS) // step_ms
    present = set(starts)
    missing: list[int] = []
    cursor = WINDOW_START_MS
    while cursor < WINDOW_END_MS:
        if cursor not in present:
            missing.append(cursor)
        cursor += step_ms
    duplicates = len(starts) - len(present)
    out_of_order = sum(1 for a, b in zip(starts, starts[1:], strict=False) if b <= a)
    # A perp kline's close and the next kline's open are the last and first trade
    # prices of adjacent intervals, so a one-tick difference is normal
    # microstructure, not a data gap. Only differences beyond a tick are reported,
    # in ticks and in basis points of price.
    boundary: list[dict[str, Any]] = []
    boundary_bps: list[float] = []
    for i in range(len(candles) - 1):
        prev_close, next_open = candles[i].close, candles[i + 1].open
        if prev_close == next_open:
            continue
        delta = abs(next_open - prev_close)
        bps = float(delta / prev_close * 10_000) if prev_close else 0.0
        boundary_bps.append(bps)
        if tick is None or delta > tick:
            boundary.append(
                {
                    "at": iso(starts[i + 1]),
                    "prev_close": str(prev_close),
                    "next_open": str(next_open),
                    "ticks": None if tick is None else float(delta / tick),
                    "bps": round(bps, 4),
                }
            )
    price_shocks = [
        {
            "at": iso(starts[i + 1]),
            "gap_pct": float((candles[i + 1].open - candles[i].close) / candles[i].close * 100),
        }
        for i in range(len(candles) - 1)
        if candles[i].close and abs(candles[i + 1].open - candles[i].close) / candles[i].close > 0.10
    ]
    non_positive = sum(1 for c in candles if c.low <= 0 or c.high < c.low)
    bad_ohlc = sum(1 for c in candles if not (c.low <= c.open <= c.high and c.low <= c.close <= c.high))
    return {
        "bars": len(candles),
        "expected_bars": expected,
        "coverage_pct": 100.0 * len(candles) / expected,
        "first_event": iso(starts[0]) if starts else None,
        "last_event_end": iso(ends[-1]) if ends else None,
        "event_time_rule": "event_time = bar close + 1 ms (a bar is knowable only when closed)",
        "missing_slots": len(missing),
        "missing_examples": [iso(m) for m in missing[:5]],
        "duplicate_slots": duplicates,
        "out_of_order": out_of_order,
        "boundary_diffs": len(boundary_bps),
        "boundary_diff_max_bps": round(max(boundary_bps), 4) if boundary_bps else 0.0,
        "boundary_diff_mean_bps": round(sum(boundary_bps) / len(boundary_bps), 4)
        if boundary_bps
        else 0.0,
        "boundary_diffs_beyond_one_tick": len(boundary),
        "boundary_diff_examples": boundary[:3],
        "abs_gap_over_10pct": len(price_shocks),
        "gap_examples": price_shocks[:3],
        "non_positive_or_inverted_bars": non_positive,
        "ohlc_ordering_violations": bad_ohlc,
        "monotonic": out_of_order == 0,
        "complete": len(missing) == 0 and duplicates == 0 and len(candles) == expected,
    }


def audit_funding(rows: list[Any]) -> dict[str, Any]:
    times = sorted(int(r.funding_time_ms) for r in rows)
    intervals = [
        round((b - a) / 3_600_000, 3) for a, b in zip(times, times[1:], strict=False)
    ]
    buckets: dict[float, int] = {}
    for value in intervals:
        buckets[value] = buckets.get(value, 0) + 1
    in_window = [t for t in times if WINDOW_START_MS <= t < WINDOW_END_MS]
    days = (WINDOW_END_MS - WINDOW_START_MS) / 86_400_000
    expected = days * 3
    receipt_unknown = sum(1 for r in rows if getattr(r, "received_ns", None) is None)
    return {
        "rows_total": len(times),
        "rows_in_window": len(in_window),
        "expected_8h_settlements": int(expected),
        "coverage_pct": 100.0 * len(in_window) / expected,
        "first": iso(times[0]) if times else None,
        "last": iso(times[-1]) if times else None,
        "interval_histogram_hours": buckets,
        "receipt_time_unknown": receipt_unknown,
        "complete": len(in_window) == int(expected),
    }


def completed_h4(candles: Sequence[Any]) -> dict[str, Any]:
    """H4 bars derivable from four consecutive complete H1 bars, and their count."""
    completed = 0
    blocks_with_gaps = 0
    for index in range(3, len(candles)):
        block = candles[index - 3 : index + 1]
        contiguous = all(
            block[i].end_ns == block[i + 1].start_ns for i in range(3)
        )
        aligned = block[0].start_ns % (4 * H1_MS * 1_000_000) == 0
        if contiguous and aligned:
            completed += 1
        elif aligned:
            blocks_with_gaps += 1
    return {
        "completed_h4_bars": completed,
        "expected_h4_bars": 181 * 6,
        "aligned_blocks_with_gaps": blocks_with_gaps,
        "rule": "H4 published only when four contiguous complete H1 bars exist "
        "and the 4h window starts on a 00:00/04:00/... boundary",
    }


def audit_symbol(symbol: str) -> dict[str, Any]:
    h1_dir = REPO / "research" / "tape" / f"{symbol.lower()}-1h-2025H1"
    m15_dir = REPO / "research" / "tape" / f"{symbol.lower()}-15m-2025H1"
    for directory in (h1_dir, m15_dir):
        if not (directory / "tape.jsonl").is_file():
            return {"symbol": symbol, "status": "TAPE_MISSING", "dir": str(directory)}
    h1 = load_multitape(h1_dir / "tape.jsonl")
    m15 = load_multitape(m15_dir / "tape.jsonl")
    h1_candles = h1.candles[symbol]
    m15_candles = m15.candles[symbol]
    durations_ok = all(
        c.end_ns - c.start_ns == H1_MS * 1_000_000 for c in h1_candles
    ) and all(c.end_ns - c.start_ns == M15_MS * 1_000_000 for c in m15_candles)
    return {
        "symbol": symbol,
        "status": "OK",
        "h1": audit_channel(h1_candles, H1_MS, filters_for(symbol).price_increment),
        "m15": audit_channel(m15_candles, M15_MS, filters_for(symbol).price_increment),
        "funding": audit_funding([r for r in h1.funding if r.instrument == symbol]),
        "h4": completed_h4(h1_candles),
        "bar_durations_exact": durations_ok,
        "h1_manifest": json.loads((h1_dir / "tape.manifest.json").read_text()),
        "m15_manifest_sha": json.loads((m15_dir / "tape.manifest.json").read_text())["tape_sha256"],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=REPO / "reports" / "v9-cross-market-validation" / "data" / "data_audit.json")
    parser.add_argument("--symbols", nargs="*", default=None)
    args = parser.parse_args()
    report = {}
    for symbol in args.symbols or UNIVERSE:
        entry = audit_symbol(symbol)
        report[symbol] = entry
        if entry["status"] != "OK":
            print(json.dumps({"symbol": symbol, "status": entry["status"]}), flush=True)
            continue
        print(
            json.dumps(
                {
                    "symbol": symbol,
                    "h1_bars": entry["h1"]["bars"],
                    "h1_missing": entry["h1"]["missing_slots"],
                    "h1_dupes": entry["h1"]["duplicate_slots"],
                    "m15_bars": entry["m15"]["bars"],
                    "m15_missing": entry["m15"]["missing_slots"],
                    "funding_rows": entry["funding"]["rows_in_window"],
                    "funding_complete": entry["funding"]["complete"],
                    "boundary_max_bps": entry["h1"]["boundary_diff_max_bps"],
                    "boundary_beyond_tick": entry["h1"]["boundary_diffs_beyond_one_tick"],
                    "h4_bars": entry["h4"]["completed_h4_bars"],
                }
            ),
            flush=True,
        )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=1, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
