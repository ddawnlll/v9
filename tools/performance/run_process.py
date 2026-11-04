#!/usr/bin/env python3
"""Measure one complete child replay, including interpreter startup and teardown.

RSS and CPU use resource accounting for this fresh parent's child, not the
parent. On Linux ru_maxrss is KiB; on macOS it is bytes. The JSON file named
by --measurement is written after the child exits and is excluded from wall time.
"""
from __future__ import annotations

import argparse
import json
import platform
import resource
import subprocess
import sys
import time
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--measurement", required=True, type=Path)
    parser.add_argument("--script", choices=("ensemble", "portfolio", "research"), default="ensemble")
    parser.add_argument("args", nargs=argparse.REMAINDER)
    parsed = parser.parse_args()
    child_args = parsed.args
    if child_args and child_args[0] == "--":
        child_args = child_args[1:]
    script = Path(__file__).with_name(f"{parsed.script}_profile.py")
    command = [sys.executable, str(script), *child_args]
    before = resource.getrusage(resource.RUSAGE_CHILDREN)
    start = time.perf_counter()
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    elapsed = time.perf_counter() - start
    after = resource.getrusage(resource.RUSAGE_CHILDREN)
    record = {
        "command": command,
        "child_wall_s": elapsed,
        "child_user_cpu_s": after.ru_utime - before.ru_utime,
        "child_system_cpu_s": after.ru_stime - before.ru_stime,
        "child_peak_rss_bytes": int(after.ru_maxrss * (1 if platform.system() == "Darwin" else 1024)),
        "returncode": result.returncode,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "measurement_write_included": False,
    }
    parsed.measurement.parent.mkdir(parents=True, exist_ok=True)
    parsed.measurement.write_text(json.dumps(record, indent=2) + "\n")
    print(json.dumps(record, sort_keys=True))
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
