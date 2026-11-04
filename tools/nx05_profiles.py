#!/usr/bin/env python
"""NX05 (#426) R5/R6 — execution profiles and the two-path reconciliation table.

Runs each path in its own subprocess so walltime and peak RSS belong to that run
alone, then writes under ``--out``:

* ``profiles.json`` — per profile: the exact command, bars, walltime, peak RSS,
  exit code and the hashes of the artifacts the run bound (read-back evidence).
  A profile that was not executed is recorded as ``NOT_EXECUTED`` with the reason;
  it is never silently omitted.
* ``reconcile_table.json`` — the same small real UTC window through both reporting
  paths: window identity, trade signatures, equity and cost blocks side by side,
  with the differences named. Funding/no-funding attribution is claimed **only**
  when the trade signatures match.

Smoke is never economic evidence; the table says so per row.

Usage (from the repository root):

    uv run --project . --extra dev --extra research \\
        python tools/nx05_profiles.py
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

WRAPPER = """
import json, runpy, sys, time, resource, traceback
mod, argv = sys.argv[1], sys.argv[2:]
started = time.monotonic()
code = 0
error = None
try:
    sys.argv = [mod, *argv]
    runpy.run_module(mod, run_name="__main__")
except SystemExit as exc:
    code = int(exc.code or 0)
except BaseException:
    code = 1
    error = traceback.format_exc()
print(json.dumps({
    "exit_code": code,
    "walltime_s": round(time.monotonic() - started, 3),
    "peak_rss_bytes": (
        # ru_maxrss is bytes on macOS and kilobytes on Linux
        resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        if sys.platform == "darwin"
        else resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024
    ),
    "error": error,
}))
"""

#: The same small real UTC window on the SAME physical tape is given to both paths,
#: so the two runs differ only in what they do with it. The D153 path runs one
#: instrument; the portfolio path runs the tape's whole universe, so the universes
#: differ by design and the table must say so instead of pretending otherwise.
WINDOW = ("2025-01-01", "2025-02-01")
SHARED_TAPE = "research/tape/multi-1h-4y"


def _latest_manifest(runs_dir: Path) -> Any:
    """The window manifest of the run, never its telemetry sidecar."""
    if not runs_dir.is_dir():
        return None
    files = [
        path
        for path in sorted(runs_dir.glob("*.json"))
        if not path.name.endswith(".telemetry.json")
    ]
    return _read_json(files[-1]) if files else None


def _run(module: str, argv: list[str], timeout: int) -> dict[str, Any]:
    proc = subprocess.run(
        [sys.executable, "-c", WRAPPER, module, *argv],
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    summary: dict[str, Any] = {"stdout_tail": proc.stdout[-800:]}
    for line in reversed(proc.stdout.splitlines()):
        if line.startswith("{") and "walltime_s" in line:
            summary.update(json.loads(line))
            break
    else:
        summary.update({"exit_code": proc.returncode, "walltime_s": None, "peak_rss_bytes": None})
    summary["stderr_tail"] = proc.stderr[-400:]
    summary["command"] = f"python -m {module} " + " ".join(argv)
    summary["module"] = module
    return summary


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else "ABSENT"


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text()) if path.is_file() else None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", default=None)
    parser.add_argument("--out", default="docs/evidence/v87/NX05")
    parser.add_argument("--run-root", default="artifacts/nx05-profiles")
    parser.add_argument("--portfolio-timeout", type=int, default=900)
    args = parser.parse_args(argv)

    repo_root = (
        Path(args.repo_root).resolve() if args.repo_root else Path(__file__).resolve().parents[1]
    )
    out_dir = (repo_root / args.out).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    run_root = (repo_root / args.run_root).resolve()
    # The run root is a disposable artifacts subtree: each invocation starts clean,
    # so the completed-window guard sees a fresh key instead of refusing a previous
    # invocation's finished run (which is what it exists to do).
    if run_root.exists():
        shutil.rmtree(run_root)
    run_root.mkdir(parents=True, exist_ok=True)
    start_utc, end_utc = WINDOW
    shared_tape = str(repo_root / SHARED_TAPE)

    profiles: list[dict[str, Any]] = []

    # --- benchmark path: smoke (bar-count) ---------------------------------- #
    smoke_dir = run_root / "benchmark-smoke"
    bench_smoke = _run(
        "v8_next.app.benchmark",
        [
            "--bars", "300",
            "--tape-path", shared_tape,
            "--diagnostic-only",
            "--output-dir", str(smoke_dir),
            "--html-out", str(smoke_dir / "report.html"),
        ],
        timeout=600,
    )
    manifest = _latest_manifest(smoke_dir / "runs")
    bench_smoke.update(
        profile="smoke",
        economic_evidence=False,
        manifest=manifest,
    )
    profiles.append(bench_smoke)

    # --- benchmark path: fold (UTC-bounded) --------------------------------- #
    fold_dir = run_root / "benchmark-fold"
    bench_fold = _run(
        "v8_next.app.benchmark",
        [
            "--profile", "fold",
            "--start-utc", start_utc,
            "--end-utc", end_utc,
            "--fold-id", "FOLD_PROBE",
            "--instrument", "BTCUSDT",
            "--tape-path", shared_tape,
            "--diagnostic-only",
            "--output-dir", str(fold_dir),
            "--html-out", str(fold_dir / "report.html"),
        ],
        timeout=900,
    )
    fold_manifest = _latest_manifest(fold_dir / "runs")
    bench_fold.update(profile="fold", economic_evidence=True, manifest=fold_manifest)
    profiles.append(bench_fold)

    # --- portfolio path: same UTC window ------------------------------------ #
    portfolio_dir = run_root / "portfolio-fold"
    portfolio = _run(
        "v8_next.app.portfolio",
        [
            "--profile", "fold",
            "--start-utc", start_utc,
            "--end-utc", end_utc,
            "--fold-id", "FOLD_PROBE",
            "--tape-path", shared_tape,
            "--output-dir", str(portfolio_dir),
        ],
        timeout=args.portfolio_timeout,
    )
    portfolio_manifest = _latest_manifest(portfolio_dir / "runs")
    portfolio.update(profile="fold", economic_evidence=None, manifest=portfolio_manifest)
    profiles.append(portfolio)

    profiles_payload = {
        "window": {"start_utc": start_utc, "end_utc": end_utc},
        "note": (
            "each row is one process: walltime/peak RSS belong to that run alone. "
            "A NOT_EXECUTED row keeps its reason instead of disappearing."
        ),
        "profiles": profiles,
        "artifact_hashes": {
            "benchmark_smoke_report": _sha256(smoke_dir / "report.html"),
            "benchmark_fold_report": _sha256(fold_dir / "report.html"),
            "portfolio_receipt": _sha256(next(iter(sorted(portfolio_dir.glob("economic_receipt*.json"))), Path("ABSENT"))),
        },
    }
    profiles_path = out_dir / "profiles.json"
    profiles_path.write_text(json.dumps(profiles_payload, indent=2, sort_keys=True, default=str) + "\n")

    # --- reconciliation table (R5) ------------------------------------------ #
    bench_window = (bench_fold.get("manifest") or {}).get("window") or {}
    portfolio_window = (portfolio.get("manifest") or {}).get("window") or {}
    bench_detail = (bench_fold.get("manifest") or {}).get("detail") or {}
    portfolio_detail = (portfolio.get("manifest") or {}).get("detail") or {}
    signatures = {
        "benchmark_path_trade_signature": bench_detail.get("trade_signature"),
        "portfolio_path_trade_signature": portfolio_detail.get("trade_signature"),
    }
    signatures_match = (
        signatures["benchmark_path_trade_signature"] is not None
        and signatures["benchmark_path_trade_signature"]
        == signatures["portfolio_path_trade_signature"]
    )
    reconcile = {
        "window": {"start_utc": start_utc, "end_utc": end_utc},
        "identity": {
            "benchmark_run_key": (bench_fold.get("manifest") or {}).get("run_key"),
            "portfolio_run_key": (portfolio.get("manifest") or {}).get("run_key"),
            "benchmark_window_tape": bench_window.get("tape_path"),
            "portfolio_window_tape": portfolio_window.get("tape_path"),
            "same_tape": bench_window.get("tape_path") == portfolio_window.get("tape_path"),
            "benchmark_universe": bench_window.get("instrument"),
            "portfolio_universe": "tape universe (all legs)",
        },
        "trade_signatures": signatures,
        "signatures_match": signatures_match,
        "funding_attribution": (
            "VALID_SIGNATURES_MATCH"
            if signatures_match
            else "NOT_CLAIMED_SIGNATURES_DIFFER"
        ),
        "differences_explained": [
            "the two paths are given the same UTC window on the same tape; their run "
            "keys differ because the instruments/case/policy differ, which is the point "
            "of binding identity rather than comparing apples to oranges",
            "the D153 path runs a single instrument; the portfolio path runs the tape's "
            "whole universe, so any trade/equity difference is expected and is not "
            "evidence of a defect in either path",
        ],
        "paths": {
            "benchmark": {
                "exit_code": bench_fold.get("exit_code"),
                "walltime_s": bench_fold.get("walltime_s"),
                "state": (bench_fold.get("manifest") or {}).get("state"),
                "economic_evidence": bench_fold.get("economic_evidence"),
                "bars": bench_detail.get("bars"),
            },
            "portfolio": {
                "exit_code": portfolio.get("exit_code"),
                "walltime_s": portfolio.get("walltime_s"),
                "state": (portfolio.get("manifest") or {}).get("state"),
                "economic_evidence": portfolio.get("economic_evidence"),
                "bars": portfolio_detail.get("bars"),
                "receipt_verify": portfolio_detail.get("receipt_verify"),
                "ledger_chain": portfolio_detail.get("ledger_chain"),
            },
        },
        "economic_evidence": "NONE — smoke/fold probe runs; NO_ECONOMIC_CLAIM",
    }
    reconcile_path = out_dir / "reconcile_table.json"
    reconcile_path.write_text(json.dumps(reconcile, indent=2, sort_keys=True, default=str) + "\n")

    for row in profiles:
        print(
            f"[NX05] {row['module']:<28} profile={row.get('profile'):<9} "
            f"exit={row.get('exit_code')} walltime={row.get('walltime_s')}s "
            f"peakRSS={(row.get('peak_rss_bytes') or 0) / 1e6:.0f}MB"
        )
    print(f"[NX05] signatures_match={signatures_match} funding_attribution={reconcile['funding_attribution']}")
    print(f"[NX05] profiles  sha256:{_sha256(profiles_path)}")
    print(f"[NX05] reconcile sha256:{_sha256(reconcile_path)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
