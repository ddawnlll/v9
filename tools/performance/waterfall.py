#!/usr/bin/env python3
"""Reconcile a traced replay's non-overlapping phases to its child wall time."""
from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--process", type=Path, required=True)
    parser.add_argument("--child", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    process = json.loads(args.process.read_text())
    child = json.loads(args.child.read_text())
    if process["returncode"] != 0 or not child["stage_trace_instrumented"]:
        raise ValueError("requires a successful stage-traced child")
    elapsed = process["child_wall_s"]
    stages = child["stages"]
    stage_total = sum(stage["wall_s"] for stage in stages)
    rows = [
        ("script_imports_after_timer_start", child["imports_s"]),
        ("input_preparation", child["fixture_s"]),
        *((stage["phase"], stage["wall_s"]) for stage in stages),
        ("replay_setup_and_monitoring_unattributed", child["production_backtest_s"] - stage_total),
        ("interpreter_startup_output_and_teardown_unattributed", elapsed - child["pre_output_s"]),
    ]
    if any(seconds < -1e-6 for _, seconds in rows):
        raise ValueError("timers are inconsistent; refusing a misleading waterfall")
    if abs(sum(seconds for _, seconds in rows) - elapsed) > 1e-6:
        raise ValueError("waterfall does not reconcile")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(("phase", "wall_s", "percent_child_wall", "invocations"))
        for name, seconds in rows:
            writer.writerow((name, seconds, 100 * seconds / elapsed, 1))
        writer.writerow(("TOTAL", elapsed, 100, 1))


if __name__ == "__main__":
    main()
