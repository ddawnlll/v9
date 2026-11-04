#!/usr/bin/env python3
"""Assemble report CSVs from raw measurement evidence.

Reads the JSON/CSV files produced by ``run_process.py``, ``memory_timeline.py``
and the profiling harnesses, and writes ``measurements.csv`` and
``memory_timeline.csv``. Nothing is invented here: a quantity that the evidence
does not contain is left empty and the row records why.

Percentiles use the nearest-rank method. With three repetitions the P95 equals
the maximum, which is reported rather than presented as a stable tail.

Usage::

    python tools/performance/assemble_report.py \
        --evidence reports/performance/evidence --out reports/performance
"""
from __future__ import annotations

import argparse
import csv
import json
import statistics
from pathlib import Path

#: Workloads measured by this run, in the order the report presents them.
SINGLE_LEG = ("b0", "b1")
PORTFOLIO = ("b3",)
REPETITIONS = 3
MIB = 1024 * 1024


def nearest_rank(values: list[float], percentile: int) -> float:
    ordered = sorted(values)
    rank = max(0, (percentile * len(ordered) + 99) // 100 - 1)
    return ordered[rank]


def load(path: Path) -> dict:
    return json.loads(path.read_text())


def process_rows(evidence: Path, tag: str) -> list[dict]:
    rows = []
    for index in range(1, REPETITIONS + 1):
        rows.append(load(evidence / "verify" / f"{tag}-{index}-process.json"))
    return rows


def child_rows(evidence: Path, tag: str) -> list[dict]:
    return [load(evidence / "verify" / f"{tag}-{index}.json") for index in range(1, REPETITIONS + 1)]


def metric_row(
    case_id: str,
    description: str,
    metric: str,
    values: list[float],
    unit: str,
    digits: int,
    note: str,
) -> dict[str, str | int]:
    return {
        "case": case_id,
        "workload": description,
        "metric": metric,
        "unit": unit,
        "repetitions": len(values),
        "min": f"{min(values):.{digits}f}",
        "median": f"{statistics.median(values):.{digits}f}",
        "max": f"{max(values):.{digits}f}",
        "p95": f"{nearest_rank(values, 95):.{digits}f}",
        "note": note,
    }


def measurement_rows(evidence: Path) -> list[dict]:
    cases = [
        ("B0", "1 instrument, 1 month (BTCUSDT 1h, 744 bars)", "b0"),
        ("B1", "1 instrument, 3 months (BTCUSDT 1h, 2160 bars)", "b1"),
        ("B3", "10 instruments, 3 months (1h, 21600 bars total)", "b3"),
    ]
    rows: list[dict] = []
    for case_id, description, tag in cases:
        processes = process_rows(evidence, tag)
        children = child_rows(evidence, tag)
        wall = [row["child_wall_s"] for row in processes]
        user = [row["child_user_cpu_s"] for row in processes]
        system = [row["child_system_cpu_s"] for row in processes]
        cpu_total = [u + s for u, s in zip(user, system, strict=True)]
        peak = [row["child_peak_rss_bytes"] for row in processes]
        bars = children[0]["bars"]
        median_wall = statistics.median(wall)
        rows.append(
            {
                "case": case_id,
                "workload": description,
                "metric": "child_wall_s",
                "unit": "s",
                "repetitions": REPETITIONS,
                "min": f"{min(wall):.4f}",
                "median": f"{median_wall:.4f}",
                "max": f"{max(wall):.4f}",
                "p95": f"{nearest_rank(wall, 95):.4f}",
                "note": "fresh child process per repetition; includes interpreter startup, output and teardown",
            }
        )

        def add(
            metric: str,
            values: list[float],
            unit: str,
            digits: int,
            note: str,
            _case: str = case_id,
            _workload: str = description,
        ) -> None:
            rows.append(metric_row(_case, _workload, metric, values, unit, digits, note))

        add("user_cpu_s", user, "s", 4, "RUSAGE_CHILDREN user time for the measured child")
        add("system_cpu_s", system, "s", 4, "RUSAGE_CHILDREN system time for the measured child")
        add("cpu_total_s", cpu_total, "s", 4, "user + system for the same child")
        add(
            "cpu_utilisation_pct_of_2_vcpu",
            [100 * value / elapsed / 2 for value, elapsed in zip(cpu_total, wall, strict=True)],
            "%",
            1,
            "cpu_total/wall relative to the 2-vCPU cgroup quota; not a host-wide figure",
        )
        add(
            "os_peak_rss",
            [value / MIB for value in peak],
            "MiB",
            1,
            "ru_maxrss high-water mark; not a sampled series",
        )
        add("bars_per_s", [bars / elapsed for elapsed in wall], "bars/s", 1, f"{bars} bars per repetition")
        add("bars", [float(bars)] * REPETITIONS, "bars", 0, "replayed bar count")
        add(
            "decisions",
            [float(child["decisions"]) for child in children],
            "count",
            0,
            "decisions recorded by the production strategy",
        )
        add("fills", [float(child["fills"]) for child in children], "count", 0, "native fill records")

    # Phase waterfall: one separately instrumented run, not a baseline.
    stage_child = load(evidence / "profile" / "b1-stage.json")
    stage_process = load(evidence / "profile" / "b1-stage-process.json")
    total = stage_process["child_wall_s"]
    rows.append(
        {
            "case": "B1-stage-trace",
            "workload": "1 instrument, 3 months (BTCUSDT 1h, 2160 bars)",
            "metric": "instrumented_child_wall_s",
            "unit": "s",
            "repetitions": 1,
            "min": f"{total:.4f}",
            "median": f"{total:.4f}",
            "max": f"{total:.4f}",
            "p95": f"{total:.4f}",
            "note": "sys.monitoring instrumented; not an uninstrumented baseline",
        }
    )
    phases = [
        ("script_imports_after_timer_start", stage_child["imports_s"]),
        ("input_preparation_tape_load_and_candle_build", stage_child["fixture_s"]),
        *((stage["phase"], stage["wall_s"]) for stage in stage_child["stages"]),
    ]
    stage_sum = sum(seconds for _, seconds in phases)
    phases.append(("replay_setup_and_monitoring_unattributed", stage_child["production_backtest_s"] - stage_sum))
    phases.append(
        ("interpreter_startup_output_and_teardown_unattributed", total - stage_child["pre_output_s"])
    )
    for name, seconds in phases:
        if seconds < 0:
            # sys.monitoring inflates the enclosed intervals; a negative residue
            # means the tracer cost cannot be separated. It is reported, not
            # clamped into a tidier number.
            rows.append(
                {
                    "case": "B1-stage-trace",
                    "workload": "1 instrument, 3 months (BTCUSDT 1h, 2160 bars)",
                    "metric": f"instrumentation_inconsistency:{name}",
                    "unit": "s",
                    "repetitions": 1,
                    "min": f"{-seconds:.4f}",
                    "median": f"{-seconds:.4f}",
                    "max": f"{-seconds:.4f}",
                    "p95": f"{-seconds:.4f}",
                    "note": (
                        "stage intervals exceed the enclosing replay span by this much; "
                        "the instrumented waterfall is indicative and does not reconcile"
                    ),
                }
            )
            continue
        rows.append(
            {
                "case": "B1-stage-trace",
                "workload": "1 instrument, 3 months (BTCUSDT 1h, 2160 bars)",
                "metric": f"phase:{name}",
                "unit": "s",
                "repetitions": 1,
                "min": f"{seconds:.4f}",
                "median": f"{seconds:.4f}",
                "max": f"{seconds:.4f}",
                "p95": f"{seconds:.4f}",
                "note": f"non-overlapping interval, {100 * seconds / total:.2f}% of instrumented child wall",
            }
        )

    # Data-size scaling on the same real window.
    for bars in (720, 1440, 2160):
        wall, peak = [], []
        for index in range(1, REPETITIONS + 1):
            row = load(evidence / "scaling" / f"dur-{bars}-{index}-process.json")
            wall.append(row["child_wall_s"])
            peak.append(row["child_peak_rss_bytes"] / MIB)
        median_wall = statistics.median(wall)
        rows.append(
            {
                "case": f"scaling-bars-{bars}",
                "workload": f"1 instrument, first {bars} bars of the 3-month BTCUSDT window",
                "metric": "child_wall_s",
                "unit": "s",
                "repetitions": REPETITIONS,
                "min": f"{min(wall):.4f}",
                "median": f"{median_wall:.4f}",
                "max": f"{max(wall):.4f}",
                "p95": f"{nearest_rank(wall, 95):.4f}",
                "note": f"{bars / median_wall:.1f} bars/s; peak RSS {statistics.median(peak):.1f} MiB",
            }
        )

    # Instrument-count scaling at a fixed 2160 bars per leg.
    for count in (1, 2, 5, 10):
        wall, peak = [], []
        for index in range(1, REPETITIONS + 1):
            row = load(evidence / "scaling" / f"inst-{count}-{index}-process.json")
            wall.append(row["child_wall_s"])
            peak.append(row["child_peak_rss_bytes"] / MIB)
        median_wall = statistics.median(wall)
        rows.append(
            {
                "case": f"scaling-instruments-{count}",
                "workload": f"{count} instrument(s) x 2160 bars, 3-month window, funding fed",
                "metric": "child_wall_s",
                "unit": "s",
                "repetitions": REPETITIONS,
                "min": f"{min(wall):.4f}",
                "median": f"{median_wall:.4f}",
                "max": f"{max(wall):.4f}",
                "p95": f"{nearest_rank(wall, 95):.4f}",
                "note": f"peak RSS {statistics.median(peak):.1f} MiB",
            }
        )

    # Not measured here: keep the gap explicit instead of leaving it blank.
    rows.append(
        {
            "case": "B2",
            "workload": "1 instrument, 1 year",
            "metric": "child_wall_s",
            "unit": "s",
            "repetitions": 0,
            "min": "",
            "median": "",
            "max": "",
            "p95": "",
            "note": "NOT_RUN: no 12-month window inside the authorized BTC development window",
        }
    )
    rows.append(
        {
            "case": "B4",
            "workload": "40 instruments, 3 months",
            "metric": "child_wall_s",
            "unit": "s",
            "repetitions": 0,
            "min": "",
            "median": "",
            "max": "",
            "p95": "",
            "note": "NOT_RUN: real hourly tape exists for 10 symbols only; no synthetic 40-leg run performed",
        }
    )
    rows.append(
        {
            "case": "B5",
            "workload": "40 instruments, 1 year",
            "metric": "child_wall_s",
            "unit": "s",
            "repetitions": 0,
            "min": "",
            "median": "",
            "max": "",
            "p95": "",
            "note": "NOT_RUN: neither the 40-symbol universe nor a 12-month authorized window exists locally",
        }
    )
    return rows


def select_timeline(rows: list[dict], case: str, phase_marks: list[tuple[str, float]]) -> list[dict]:
    """Attach case and phase to each timeline sample.

    Phase marks are absolute monotonic instants taken from the child's own
    record; a sample is labelled with the last phase whose boundary it follows.
    """
    out = []
    for row in rows:
        when = float(row["monotonic_s"])
        phase = "pre_child_timer"
        for name, boundary in phase_marks:
            if when >= boundary:
                phase = name
            else:
                break
        out.append(
            {
                "case": case,
                "sample_index": row["sample_index"],
                "since_spawn_s": row["since_spawn_s"],
                "rss_bytes": row["rss_bytes"],
                "vms_bytes": row["vms_bytes"],
                "threads": row["threads"],
                "utime_s": row["utime_s"],
                "stime_s": row["stime_s"],
                "major_faults": row["major_faults"],
                "voluntary_ctxt_switches": row["voluntary_ctxt_switches"],
                "nonvoluntary_ctxt_switches": row["nonvoluntary_ctxt_switches"],
                "phase": phase,
            }
        )
    return out


def timeline_rows(evidence: Path) -> list[dict]:
    rows: list[dict] = []
    for tag, case in (("b1", "B1"), ("b3", "B3")):
        child = load(evidence / "profile" / f"{tag}-timeline-child.json")
        origin = child["start_monotonic_s"]
        marks = [("script_imports_and_setup", origin)]
        marks.append(("tape_load_and_input_preparation", origin + child["imports_s"]))
        marks.append(("engine_replay", origin + child["imports_s"] + child["fixture_s"]))
        marks.append(("post_replay_serialisation_and_teardown", origin + child["pre_output_s"]))
        with (evidence / "profile" / f"{tag}-timeline.csv").open() as stream:
            rows.extend(select_timeline(list(csv.DictReader(stream)), case, marks))
    return rows


def summarise_timeline(rows: list[dict], case: str) -> dict[str, float]:
    """Time-weighted statistics over one case's samples."""
    samples = [row for row in rows if row["case"] == case]
    weights, values = [], []
    for current, following in zip(samples, samples[1:] + [None], strict=False):
        values.append(int(current["rss_bytes"]))
        weights.append(
            float(following["since_spawn_s"]) - float(current["since_spawn_s"])
            if following is not None
            else 1.0
        )
    total = sum(weights)
    weighted = sum(value * weight for value, weight in zip(values, weights, strict=True)) / total
    return {
        "samples": len(values),
        "first_sample_mib": values[0] / MIB,
        "time_weighted_mean_mib": weighted / MIB,
        "median_mib": statistics.median(values) / MIB,
        "p95_mib": nearest_rank(values, 95) / MIB,
        "peak_sampled_mib": max(values) / MIB,
        "final_sample_mib": values[-1] / MIB,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()

    rows = measurement_rows(args.evidence)
    args.out.mkdir(parents=True, exist_ok=True)
    with (args.out / "measurements.csv").open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)

    timeline = timeline_rows(args.evidence)
    with (args.out / "memory_timeline.csv").open("w", newline="") as stream:
        writer = csv.DictWriter(
            stream,
            fieldnames=[
                "case", "sample_index", "since_spawn_s", "phase", "rss_bytes", "vms_bytes",
                "threads", "utime_s", "stime_s", "major_faults",
                "voluntary_ctxt_switches", "nonvoluntary_ctxt_switches",
            ],
        )
        writer.writeheader()
        writer.writerows(timeline)

    for case in ("B1", "B3"):
        stats = summarise_timeline(timeline, case)
        print(
            f"{case}: samples={stats['samples']} mean={stats['time_weighted_mean_mib']:.1f} "
            f"median={stats['median_mib']:.1f} p95={stats['p95_mib']:.1f} "
            f"peak={stats['peak_sampled_mib']:.1f} MiB"
        )
    print(f"wrote {len(rows)} measurement rows and {len(timeline)} timeline rows to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
