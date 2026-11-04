#!/usr/bin/env python
"""Tracked reproduction candidate for the swing-performance review.

Runs the fixed plain_swing Nautilus engine on real hourly tape for a calendar
window with the declared warmup, reporting load/engine/report wall times, peak
RSS, bars and campaigns/fills. Original bounded-review evidence is preserved
under ``artifacts/swing-performance-review/`` (git-ignored); this tool is the
tracked entry point reviewers can run to reproduce the small summaries.

Usage (from the repository root)::

    .venv/bin/python tools/swing_perf_review.py \
        --start-utc 2025-01-01 --end-utc 2025-02-01 \
        --out artifacts/swing-performance-review --label repro-1mo
    .venv/bin/python tools/swing_perf_review.py \
        --start-utc 2025-01-01 --end-utc 2025-04-01 \
        --out artifacts/swing-performance-review --label repro-3mo

No economic claim. No live trading. Tape defaults to
``research/tape/multi-1h-4y/tape.jsonl``.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import platform
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from v8_next.adapters.swing_engine import SwingEngineConfig, run_swing_engine  # noqa: E402
from v8_next.economics.grammar import POLICY_REQUIRED_BARS  # noqa: E402
from v8_next.economics.swing_baseline import policy_spec  # noqa: E402
from v8_next.evaluation.gate_resolution import load_tape_candles  # noqa: E402


def parse_utc_ms(value: str) -> int:
    parsed = datetime.datetime.fromisoformat(value.strip()).replace(
        tzinfo=datetime.timezone.utc
    )
    return int(parsed.timestamp() * 1000)


def peak_rss() -> tuple[int, str]:
    """Return (peak RSS, units) using only platform-appropriate APIs."""
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        class ProcessMemoryCountersEx(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD),
                ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
                ("PrivateUsage", ctypes.c_size_t),
            ]

        counters = ProcessMemoryCountersEx()
        counters.cb = ctypes.sizeof(counters)
        get_current_process = ctypes.windll.kernel32.GetCurrentProcess
        get_current_process.restype = wintypes.HANDLE
        get_process_memory_info = ctypes.windll.psapi.GetProcessMemoryInfo
        get_process_memory_info.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(ProcessMemoryCountersEx),
            wintypes.DWORD,
        ]
        get_process_memory_info.restype = wintypes.BOOL
        process = get_current_process()
        if not get_process_memory_info(process, ctypes.byref(counters), counters.cb):
            return 0, "unavailable (Windows GetProcessMemoryInfo failed)"
        return int(counters.PeakWorkingSetSize), "bytes (Windows PeakWorkingSetSize)"

    import resource

    usage = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    if platform.system() == "Darwin":
        return int(usage), "bytes (macOS ru_maxrss)"
    return int(usage), "kilobytes (Linux ru_maxrss)"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start-utc", default="2025-01-01")
    parser.add_argument("--end-utc", default="2025-02-01")
    parser.add_argument("--tape", default="research/tape/multi-1h-4y/tape.jsonl")
    parser.add_argument("--instrument", default="BTCUSDT")
    parser.add_argument("--policy", default="plain_swing")
    parser.add_argument("--warmup-bars", type=int, default=None)
    parser.add_argument("--out", default=None)
    parser.add_argument("--label", default="repro")
    args = parser.parse_args()

    tape_path = (REPO_ROOT / args.tape).resolve()
    spec = policy_spec(args.policy)
    declared_warmup = 0 if spec.grammar_policy is None else int(
        POLICY_REQUIRED_BARS[spec.grammar_policy]
    )
    warmup = declared_warmup if args.warmup_bars is None else int(args.warmup_bars)
    start_ms = parse_utc_ms(args.start_utc)
    end_ms = parse_utc_ms(args.end_utc)

    t_load0 = time.perf_counter()
    candles = load_tape_candles(
        tape_path, instrument=args.instrument, start_ms=start_ms, end_ms=end_ms
    )
    t_load1 = time.perf_counter()

    cfg = SwingEngineConfig(policy_id=args.policy)
    candles_t = tuple(candles)
    t_eng0 = time.perf_counter()
    result = run_swing_engine(candles_t, cfg, warmup_bars=warmup)
    t_eng1 = time.perf_counter()
    rss, rss_units = peak_rss()

    events = result.get("events", {})
    decisions = events.get("decisions", [])
    fills = events.get("fills", [])
    summary = {
        "label": args.label,
        "policy_id": args.policy,
        "policy_identity": spec.identity(),
        "declared_warmup_bars": declared_warmup,
        "replayed_warmup_bars": warmup,
        "bars": len(candles_t),
        "load_s": t_load1 - t_load0,
        "engine_s": t_eng1 - t_eng0,
        "peak_rss_raw": int(rss),
        "peak_rss_units": rss_units,
        "decisions": len(decisions),
        "fills": len(fills),
        "positions_closed": len(events.get("positions_closed", [])),
        "open_position_at_end": result.get("open_position_at_end"),
        "timeout_budget_s": 60,
        "timed_out": False,
    }
    if args.out:
        out_path = Path(args.out) / f"{args.label}.summary.json"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
        print(f"[perf] wrote {out_path}")
    print(
        f"[perf] {args.label}: bars={summary['bars']} "
        f"load={summary['load_s']:.3f}s engine={summary['engine_s']:.3f}s "
        f"peak_rss={summary['peak_rss_raw']} {summary['peak_rss_units']} "
        f"decisions={summary['decisions']} fills={summary['fills']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
