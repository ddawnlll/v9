#!/usr/bin/env python3
"""Sample one child replay's memory, threads and CPU about every 100 ms.

The parent adds no instrumentation to the child: it reads ``/proc/<pid>/statm``,
``/proc/<pid>/status`` and ``/proc/<pid>/stat`` from outside. Samples are
timestamped on ``time.monotonic()``, the same clock the child records its own
timer origin on, so phases can be joined to samples without guessing offsets.

Rows are written incrementally, so a run that is killed still leaves evidence.
``--interval`` is a target: each row records the measured gap, because an
overshoot is not hidden by relabelling it.

OS-level high-water marks (``ru_maxrss``) and cgroup counters are recorded
separately in the summary; they are different measurements from the samples and
are never added to them.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import platform
import resource
import subprocess
import sys
import time
from pathlib import Path

PAGE_SIZE = os.sysconf("SC_PAGE_SIZE")
CLOCK_TICKS = os.sysconf("SC_CLK_TCK")


def _read(path: str) -> str | None:
    try:
        return Path(path).read_text()
    except OSError:
        return None


def sample(pid: int) -> dict[str, object] | None:
    statm = _read(f"/proc/{pid}/statm")
    status = _read(f"/proc/{pid}/status")
    stat = _read(f"/proc/{pid}/stat")
    if statm is None or status is None or stat is None:
        return None
    statm_fields = statm.split()
    values: dict[str, int | float | None] = {}
    try:
        values["vms_bytes"] = int(statm_fields[0]) * PAGE_SIZE
        values["rss_bytes"] = int(statm_fields[1]) * PAGE_SIZE
    except (IndexError, ValueError):
        values["vms_bytes"] = None
        values["rss_bytes"] = None
    for line in status.splitlines():
        key, _, rest = line.partition(":")
        rest = rest.strip()
        if key == "Threads":
            values["threads"] = int(rest)
        elif key == "VmHWM":
            values["vm_hwm_bytes"] = int(rest.split()[0]) * 1024
        elif key == "VmRSS":
            values["vm_rss_bytes"] = int(rest.split()[0]) * 1024
        elif key == "voluntary_ctxt_switches":
            values["voluntary_ctxt_switches"] = int(rest)
        elif key == "nonvoluntary_ctxt_switches":
            values["nonvoluntary_ctxt_switches"] = int(rest)
    try:
        # Field 22 of /proc/<pid>/stat is starttime; everything before it may
        # contain spaces inside the comm field, so split after the last ")".
        tail = stat.rsplit(")", 1)[1].split()
        values["utime_s"] = int(tail[11]) / CLOCK_TICKS
        values["stime_s"] = int(tail[12]) / CLOCK_TICKS
        values["minor_faults"] = int(tail[7])
        values["major_faults"] = int(tail[9])
        values["rss_pages_high_water"] = int(tail[21])
    except (IndexError, ValueError):
        pass
    return values


def cgroup_value(name: str) -> int | None:
    raw = _read(f"/sys/fs/cgroup/{name}")
    if raw is None:
        return None
    first = raw.strip().split()
    if not first:
        return None
    try:
        return int(first[0])
    except ValueError:
        return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--timeline", required=True, type=Path)
    parser.add_argument("--measurement", required=True, type=Path)
    parser.add_argument("--interval", type=float, default=0.1)
    parser.add_argument("--script", required=True)
    parser.add_argument("args", nargs=argparse.REMAINDER)
    parsed = parser.parse_args()
    if parsed.interval <= 0:
        parser.error("--interval must be positive")
    child_args = parsed.args
    if child_args and child_args[0] == "--":
        child_args = child_args[1:]

    command = [sys.executable, parsed.script, *child_args]
    parsed.timeline.parent.mkdir(parents=True, exist_ok=True)
    cgroup_events_before = _read("/sys/fs/cgroup/memory.events")
    peak_before = cgroup_value("memory.peak")
    ru_before = resource.getrusage(resource.RUSAGE_CHILDREN)
    spawn = time.monotonic()
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    columns = [
        "sample_index", "monotonic_s", "since_spawn_s", "rss_bytes", "vm_rss_bytes",
        "vm_hwm_bytes", "vms_bytes", "threads", "utime_s", "stime_s",
        "voluntary_ctxt_switches", "nonvoluntary_ctxt_switches",
        "minor_faults", "major_faults",
    ]
    rows = 0
    peaks: dict[str, int] = {}
    with parsed.timeline.open("w", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(columns)
        missing = 0
        while True:
            taken = sample(process.pid)
            now = time.monotonic()
            if taken is None or taken.get("rss_bytes") == 0:
                # A zero RSS sample means the process is already a zombie: it is
                # the exit, not a measurement, so it is not written as memory.
                missing += 1
                if process.poll() is not None:
                    break
            else:
                record = {
                    "sample_index": rows,
                    "monotonic_s": f"{now:.6f}",
                    "since_spawn_s": f"{now - spawn:.6f}",
                    **taken,
                }
                writer.writerow([record.get(column) for column in columns])
                stream.flush()
                rows += 1
                for key in ("rss_bytes", "vm_rss_bytes", "vms_bytes", "threads", "major_faults"):
                    value = taken.get(key)
                    if isinstance(value, int):
                        peaks[key] = max(peaks.get(key, value), value)
            if process.poll() is not None:
                break
            time.sleep(parsed.interval)
    stdout, stderr = process.communicate()
    ended = time.monotonic()
    ru_after = resource.getrusage(resource.RUSAGE_CHILDREN)

    summary = {
        "command": command,
        "interval_target_s": parsed.interval,
        "samples": rows,
        "samples_missing_process": missing,
        "child_wall_s": ended - spawn,
        "child_user_cpu_s": ru_after.ru_utime - ru_before.ru_utime,
        "child_system_cpu_s": ru_after.ru_stime - ru_before.ru_stime,
        "child_os_peak_rss_bytes": int(
            ru_after.ru_maxrss * (1 if platform.system() == "Darwin" else 1024)
        ),
        "sampled_peak_rss_bytes": peaks.get("rss_bytes"),
        "sampled_peak_vm_rss_bytes": peaks.get("vm_rss_bytes"),
        "sampled_peak_vms_bytes": peaks.get("vms_bytes"),
        "sampled_peak_threads": peaks.get("threads"),
        "sampled_major_faults_last": peaks.get("major_faults"),
        "cgroup_memory_peak_before_bytes": peak_before,
        "cgroup_memory_peak_after_bytes": cgroup_value("memory.peak"),
        "cgroup_memory_current_bytes": cgroup_value("memory.current"),
        "cgroup_memory_events_before": cgroup_events_before,
        "cgroup_memory_events_after": _read("/sys/fs/cgroup/memory.events"),
        "returncode": process.returncode,
        "stdout": stdout,
        "stderr": stderr,
        "measurement_write_included": False,
    }
    parsed.measurement.parent.mkdir(parents=True, exist_ok=True)
    parsed.measurement.write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary, sort_keys=True))
    return process.returncode


if __name__ == "__main__":
    raise SystemExit(main())
