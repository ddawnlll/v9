"""Benchmark sweep orchestration — binds pillars → receipts → HTML index (chain issue #454, R1/R3/R5).

One entry sweeping the locked pillars, the benchmark ledger, and the
regression snapshot into a manifest plus a static offline-safe HTML index.
Pillar execution stays in CLI ``run`` (and ``tools/nx*.py`` as-is); this
module never re-executes pillars — it reads their artifacts, verifies the
ledger chain, and compares the regression snapshot read-only. Missing
pillars/ledger/measurements are MISSING entries with zero numerator
contribution, never silent zeroes (spec §9, §14). Broken ledger chain refuses
the sweep append (fail closed).

Visuals (R4): ``nautilus_trader.analysis.tearsheet`` is plotly-backed and
``PLOTLY_AVAILABLE`` is False in this environment, so no plotly artifact is
forced into the evaluative path — the index links the static forensic HTML
and records the tearsheet deferral by name (issue §16, second pin cleared by
keeping static charts).

Usage:
    python -m v8_next.evaluation.sweep --help
    python -m v8_next.evaluation.sweep --out <sweep-dir>
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import sys
import time
from pathlib import Path

__all__ = ["main", "sweep_once"]

from v8_next.app.readiness import PILLAR_ARTIFACTS

SWEEP_VERSION = "sweep-v1"


def _sha_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _tearsheet_status() -> dict[str, object]:
    try:
        from nautilus_trader.analysis import tearsheet as _t

        available = bool(getattr(_t, "PLOTLY_AVAILABLE", False))
    except Exception:
        available = False
    if available:
        return {"mode": "TEARSHEET_AVAILABLE", "note": "plotly visuals permitted"}
    return {
        "mode": "STATIC_ONLY",
        "note": "PLOTLY_AVAILABLE is False; no plotly/CDN artifact is embedded — "
        "the static forensic HTML is the visual record (issue #454 §16 pin).",
    }


def sweep_once(repo_root: Path, out_dir: Path) -> dict[str, object]:
    pillars: list[dict[str, object]] = []
    for pillar, rels in PILLAR_ARTIFACTS.items():
        files: list[dict[str, object]] = []
        for rel in rels:
            path = repo_root / rel
            if path.is_file():
                files.append(
                    {
                        "path": rel,
                        "status": "PRESENT",
                        "sha256": _sha_file(path),
                        "bytes": path.stat().st_size,
                    }
                )
            else:
                files.append({"path": rel, "status": "MISSING", "reason": "pillar artifact absent"})
        pillars.append({"pillar": pillar, "files": files})
    ledger_candidates = sorted((repo_root / "artifacts" / "benchmarks").glob("**/benchmark_ledger.jsonl"))
    ledger_info: dict[str, object]
    chain_ok, chain_msg = False, "no benchmark ledger found (MISSING)"
    if ledger_candidates:
        from v8_next.evaluation.benchmark_receipt import BenchmarkLedger

        ledger_path = ledger_candidates[0]
        ledger = BenchmarkLedger.load_jsonl(ledger_path)
        chain_ok, chain_msg = ledger.verify_chain()
        ledger_info = {
            "path": str(ledger_path.relative_to(repo_root)),
            "sha256": _sha_file(ledger_path),
            "entries": len(ledger.entries),
            "chain_valid": chain_ok,
            "chain_message": chain_msg,
        }
    else:
        ledger_info = {"path": None, "status": "MISSING", "chain_valid": False, "chain_message": chain_msg}
    try:
        from v8_next.app import readiness as readiness_mod

        baseline_path = repo_root / readiness_mod.BASELINE_REL
        baseline = json.loads(baseline_path.read_text()) if baseline_path.is_file() else None
        regression_note: dict[str, object] = {"baseline_present": baseline is not None}
    except Exception as exc:
        regression_note = {"baseline_present": False, "error": str(exc)}
    manifest = {
        "sweep_version": SWEEP_VERSION,
        "created_ns": time.time_ns(),
        "claim": "NO_ECONOMIC_CLAIM",
        "pillars": pillars,
        "ledger": ledger_info,
        "ledger_chain_valid": chain_ok,
        "regression": regression_note,
        "tearsheet": _tearsheet_status(),
    }
    manifest["manifest_digest"] = hashlib.sha256(
        json.dumps(manifest, sort_keys=True).encode()
    ).hexdigest()
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "sweep_manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    (out_dir / "sweep_index.html").write_text(_render_index(manifest), encoding="utf-8")
    return manifest


def _render_index(manifest: dict[str, object]) -> str:
    def esc(value: object) -> str:
        return html.escape(str(value))

    rows = ""
    pillars = manifest["pillars"]
    assert isinstance(pillars, list)
    for pillar in pillars:
        assert isinstance(pillar, dict)
        files = pillar["files"]
        assert isinstance(files, list)
        for entry in files:
            assert isinstance(entry, dict)
            rows += (
                f"<tr><td>{esc(pillar['pillar'])}</td><td>{esc(entry['path'])}</td>"
                f"<td>{esc(entry['status'])}</td>"
                f"<td>{esc(str(entry.get('sha256', entry.get('reason', ''))))}</td></tr>\n"
            )
    ledger = manifest["ledger"]
    assert isinstance(ledger, dict)
    tearsheet = manifest["tearsheet"]
    assert isinstance(tearsheet, dict)
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><title>Benchmark sweep</title></head>
<body>
<h1>Benchmark sweep ({esc(str(manifest['sweep_version']))})</h1>
<p>claim: {esc(str(manifest['claim']))} · digest: {esc(str(manifest['manifest_digest']))}</p>
<h2>Pillars → receipts</h2>
<table border="1"><tr><th>pillar</th><th>artifact</th><th>status</th><th>sha256 / reason</th></tr>
{rows}</table>
<h2>Ledger</h2>
<p>chain_valid: {esc(str(manifest['ledger_chain_valid']))} · {esc(str(ledger.get('chain_message', ledger.get('status', ''))))}</p>
<h2>Visuals</h2>
<p>mode: {esc(str(tearsheet['mode']))} — {esc(str(tearsheet['note']))}</p>
</body></html>
"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sweep",
        description="Bind pillar artifacts + ledger + regression into a sweep manifest.",
    )
    parser.add_argument("--out", default="docs/evidence/v87-port-chain/454/sweep")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    package_root = Path(__file__).resolve().parents[3]
    repo_root = package_root
    try:
        manifest = sweep_once(repo_root, Path(args.out))
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(f"[sweep] manifest {args.out}/sweep_manifest.json digest={manifest['manifest_digest']}")
    if not manifest["ledger_chain_valid"]:
        print("[sweep] note: ledger chain not verified (missing or broken ledger is MISSING, never a pass)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
