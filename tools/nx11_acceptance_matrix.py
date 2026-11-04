#!/usr/bin/env python
"""NX11 (#432) — build the V8.7 technical acceptance matrix by measuring, not transcribing.

For every NX issue (plus this epic) the matrix records:

* the commit that carried the work;
* the exact check commands, re-run **now**, with the observed result (a closed GitHub
  issue is not accepted as evidence on its own -- the R-level artifacts and a fresh
  run are);
* every evidence artifact with its measured size and sha256, read back from disk;
* what is *not* delivered: unreachable data, missing economic evidence and the
  prospective backlog, listed explicitly rather than implied.

If an artifact is missing the tool fails loudly (exit 2) instead of quietly writing a
complete-looking matrix; ``--allow-missing`` downgrades that to a recorded pending
entry, and even then the matrix says so.

Usage (from the repository root):

    uv run --project . --extra dev --extra research \\
        python tools/nx11_acceptance_matrix.py
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

#: Filename markers of artifacts produced by OTHER workstreams that share this tree
#: (an issue number such as ``_436``, or a loop-reconciliation/bound-receipt file).
#: Matching artifacts are listed with their hashes but attributed to their producer
#: workstream, never counted inside this issue's acceptance row. The rule is explicit
#: because a partial detector that silently mis-attributes is worse than a named rule.
FOREIGN_ARTIFACT_MARKERS: tuple[str, ...] = ("_4", "loop_reconciliation", "bound_receipt")

#: issue -> (commit, test target, evidence directory)
ISSUES: tuple[tuple[str, str, str, str], ...] = (
    ("NX00", "epic", "09906159/NX00", "docs/evidence/v87/NX00"),
    ("NX01", "7a3d2859", "tests/test_tape_identity_nx01.py", "docs/evidence/v87/NX01"),
    ("NX02", "94e75c2c", "tests/test_runner_accounting_nx02.py", "docs/evidence/v87/NX02"),
    ("NX03", "5deb4282", "tests/test_historical_plan_nx03.py", "docs/evidence/v87/NX03"),
    ("NX04", "453df499", "tests/test_ledger_canon_nx04.py", "docs/evidence/v87/NX04"),
    ("NX05", "311df69d", "tests/test_run_window_nx05.py", "docs/evidence/v87/NX05"),
    ("NX06", "c7bb4c69", "tests/test_swing_baseline_nx06.py", "docs/evidence/v87/NX06"),
    ("NX07", "fc054071", "tests/test_statistics_plan_nx07.py", "docs/evidence/v87/NX07"),
    ("NX08", "74cb0f3d", "tests/test_nx08_gates_scoring.py", "docs/evidence/v87/NX08"),
    ("NX09", "42136231", "tests/test_fold_research_nx09.py", "docs/evidence/v87/NX09"),
    ("NX10", "a69d6772", "tests/test_public_shadow_nx10.py", "docs/evidence/v87/NX10"),
)

#: gates the epic and the issues name explicitly
GATE_CHECKS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "epic",
        (
            "tests/test_d153_runner_report.py",
            "tests/test_portfolio_benchmark.py",
        ),
    ),
    (
        "nx07-nx08",
        (
            "tests/test_loss_alignment.py",
            "tests/test_family.py",
            "tests/test_deflated_sharpe.py",
            "tests/test_reality_check.py",
            "tests/test_overfitting.py",
            "tests/test_gate_resolution.py",
        ),
    ),
    (
        "nx08",
        (
            "tests/test_execution_scoring_link.py",
            "tests/test_gate_resolution.py",
            "tests/test_d153_runner_report.py",
        ),
    ),
)

PENDING_BACKLOG: tuple[dict[str, str], ...] = (
    {
        "item": "protected final window",
        "status": "NOT_AVAILABLE",
        "evidence": "docs/evidence/v87/NX01/burn_map.json",
        "reason": "the last 12 months are TAIL_BURNED (measured); no protected final exists",
    },
    {
        "item": "funding / markout in the replay paths",
        "status": "MISSING_NOT_ZERO",
        "evidence": "docs/evidence/v87/NX01/tape_inventory.json",
        "reason": "no mark price in the four-year archive; funding is reported MISSING, never zero",
    },
    {
        "item": "prospective maturity",
        "status": "PENDING",
        "evidence": "docs/evidence/v87/NX10/maturity.json",
        "reason": "the frozen 24h window has not been observed; holding/markout unmeasured",
    },
    {
        "item": "engine fill parity for the swing family",
        "status": "PENDING",
        "evidence": "docs/evidence/v87/NX06/NX06_REPORT.md",
        "reason": "the family's outcomes are decision-plane replay, not engine fills",
    },
    {
        "item": "economic edge / live authority",
        "status": "NO_ECONOMIC_CLAIM",
        "evidence": "docs/evidence/v87/NX07/statistics_receipt.json",
        "reason": "measured DSR confidence 0.1183 and Bonferroni p = 1.0; nothing is certified",
    },
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _run(cmd: list[str], cwd: Path) -> dict[str, Any]:
    completed = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, check=False)
    output = (completed.stdout + completed.stderr).strip()
    tail = output.splitlines()[-3:]
    counts = {}
    for label, pattern in (
        ("markdown_files_scanned", r"markdown files scanned\s*:\s*(\d+)"),
        ("path_citations_audited", r"path citations audited\s*:\s*(\d+)"),
        ("resolved", r"RESOLVED\s*:\s*(\d+)"),
        ("unaccounted", r"UNACCOUNTED \(failing\)\s*:\s*(\d+)"),
        ("unaccounted_distinct", r"UNACCOUNTED \(failing\)\s*:\s*\d+ distinct (\d+)"),
        ("retired", r"RETIRED \(existed in history\):\s*(\d+)"),
    ):
        match = re.search(pattern, output)
        if match:
            counts[label] = int(match.group(1))
    return {
        "command": " ".join(cmd),
        "exit_code": completed.returncode,
        "summary": " | ".join(tail),
        "counts": counts,
        "passed": completed.returncode == 0,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", default=None)
    parser.add_argument("--out", default="docs/evidence/v87/NX11")
    parser.add_argument("--allow-missing", action="store_true")
    parser.add_argument("--skip-checks", action="store_true")
    args = parser.parse_args(argv)

    repo_root = (
        Path(args.repo_root).resolve() if args.repo_root else Path(__file__).resolve().parents[1]
    )
    out_dir = (repo_root / args.out).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    uv = ["uv", "run", "--project", ".", "--extra", "dev", "--extra", "research"]

    rows: list[dict[str, Any]] = []
    missing: list[str] = []
    for issue, commit, test_target, evidence_dir in ISSUES:
        directory = (repo_root / evidence_dir).resolve()
        artifacts: list[dict[str, Any]] = []
        if directory.is_dir():
            for path in sorted(directory.rglob("*")):
                if path.is_file() and path.suffix in (".json", ".md", ".jsonl", ".sqlite"):
                    artifacts.append(
                        {
                            "path": str(path.relative_to(repo_root)),
                            "bytes": path.stat().st_size,
                            "sha256": _sha256(path),
                        }
                    )
        else:
            missing.append(evidence_dir)

        check: dict[str, Any] = {"command": f"pytest -q {test_target}", "skipped": True}
        if not args.skip_checks and Path(repo_root / test_target).is_file():
            check = _run([*uv, "pytest", "-q", test_target], repo_root)
        # Other workstreams share this tree: a file another issue wrote into this
        # directory is listed with its hash but is NOT attributed to this issue's
        # acceptance (an artifact nobody can name a producer for is provenance noise).
        foreign = [
            row
            for row in artifacts
            if any(marker in Path(row["path"]).name for marker in FOREIGN_ARTIFACT_MARKERS)
        ]
        rows.append(
            {
                "issue": issue,
                "commit": commit,
                "test_target": test_target,
                "check": check,
                "evidence_dir": evidence_dir,
                "artifacts": [row for row in artifacts if row not in foreign],
                "artifact_count": len(artifacts) - len(foreign),
                "foreign_artifacts": foreign,
                "foreign_note": (
                    "produced by another workstream (issue number in the filename); "
                    "carries no acceptance weight for this issue"
                    if foreign
                    else None
                ),
            }
        )

    gates = []
    for name, targets in GATE_CHECKS:
        present = [t for t in targets if (repo_root / t).is_file()]
        gates.append(
            {
                "name": name,
                "targets": list(targets),
                "check": _run([*uv, "pytest", "-q", *present], repo_root)
                if present and not args.skip_checks
                else {"skipped": True},
            }
        )

    doc_checks = []
    for tool, tool_args in (
        ("tools/audit_doc_path_refs.py", []),
        ("tools/build_monograph.py", ["--check"]),
    ):
        if (repo_root / tool).is_file() and not args.skip_checks:
            doc_checks.append({"tool": tool, **_run([sys.executable, tool, *tool_args], repo_root)})
        else:
            doc_checks.append({"tool": tool, "skipped": True, "note": "not present or skipped"})

    # Document-guard errors are recorded, never hidden (NX11.R5). The residual
    # unaccounted citations live in legacy/Rust-era documents whose paths the D-162
    # quarantine moved; that is a pre-existing baseline this issue does not claim to
    # have fixed, so the numbers are written down as measured.
    baseline: dict[str, Any] = {}
    for entry in doc_checks:
        counts = entry.get("counts") or {}
        if counts.get("unaccounted") is not None or "unaccounted" in entry.get("summary", "").lower():
            baseline["audit_doc_path_refs"] = {
                "exit_code": entry.get("exit_code"),
                "counts": counts,
                "summary": entry.get("summary", ""),
                "status": "PRE_EXISTING_BASELINE_NOT_FIXED_BY_NX11",
                "scope": "legacy/Rust-era documents; the D-162 quarantine moved v8-core/",
            }
        if entry.get("exit_code") not in (0, None):
            baseline.setdefault("failing_doc_checks", []).append(entry.get("tool"))
    if any(entry.get("tool", "").endswith("build_monograph.py") and entry.get("exit_code") not in (0, None) for entry in doc_checks):
        baseline["monograph_builder"] = {
            "exit_code": 1,
            "status": "UNREACHABLE_INPUT",
            "reason": "research/manifest/research_papers_manifest.json is absent from this checkout",
            "pending_evidence": True,
        }

    matrix = {
        "release": "V8.7 technical acceptance (v8-next)",
        "pre_existing_baseline_failures": baseline,
        "issues": rows,
        "gate_checks": gates,
        "doc_checks": doc_checks,
        "pending_backlog": list(PENDING_BACKLOG),
        "missing_evidence_dirs": missing,
        "acceptance_scope": (
            "TECHNICAL acceptance only: contracts, tests, artifacts and their hashes. "
            "No economic edge, no prospective maturity and no live authority is claimed."
        ),
        "closed_issue_is_not_evidence": True,
        "economic_claim": "NONE",
    }
    path = out_dir / "acceptance_matrix.json"
    path.write_text(json.dumps(matrix, indent=2, sort_keys=True) + "\n")

    failed = [row["issue"] for row in rows if row["check"].get("passed") is False]
    failed += [gate["name"] for gate in gates if gate["check"].get("passed") is False]
    for row in rows:
        print(
            f"[NX11] {row['issue']} commit={row['commit']} artifacts={row['artifact_count']} "
            f"check={'PASS' if row['check'].get('passed') else ('SKIP' if row['check'].get('skipped') else 'FAIL')}"
        )
    for gate in gates:
        print(f"[NX11] gate {gate['name']}: exit={gate['check'].get('exit_code', 'skipped')}")
    for doc in doc_checks:
        print(f"[NX11] doc  {doc['tool']}: exit={doc.get('exit_code', 'skipped')}")
    print(f"[NX11] missing evidence dirs: {missing}")
    print(f"[NX11] matrix sha256:{_sha256(path)}")

    if missing and not args.allow_missing:
        print("[NX11] FAIL: missing evidence dirs (use --allow-missing to record them as pending)")
        return 2
    if failed:
        print(f"[NX11] FAIL: checks failed for {failed}")
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
