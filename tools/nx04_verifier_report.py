#!/usr/bin/env python
"""NX04 (#425) R1/R6 — read-only ledger canonicalization probe and before/after report.

Produces, under ``--out``:

* ``ledger_canon_probe.json`` — per-sequence canonicalization facts for the ORIGINAL
  ledger bytes: digest version, number of fields in the version's proven canon, and
  whether that canon reproduces the stored digest. The ledger is never rewritten.
* ``verifier_report.json`` — the ledger file's sha256 before and after, the legacy
  verifier's verdict (loaded from a pre-NX04 revision via ``git show``), and the
  version-resolved verifier's verdict.

Usage (from the repository root):

    uv run --project . --extra dev --extra research \\
        python tools/nx04_verifier_report.py
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from v8_next.evaluation.benchmark_receipt import (
    RECEIPT_CANON_TABLE,
    BenchmarkLedger,
    build_canon_payload,
)

DEFAULT_LEDGER = "artifacts/benchmarks/benchmark_ledger.jsonl"
DEFAULT_LEGACY_REV = "HEAD"


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _load_legacy_module(repo_root: Path, rev: str) -> Any | None:
    """Load the pre-NX04 verifier straight out of git; no checkout, no tree change."""
    rel = "src/v8_next/evaluation/benchmark_receipt.py"
    try:
        source = subprocess.run(
            ["git", "show", f"{rev}:{rel}"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=True,
        ).stdout
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None
    with tempfile.TemporaryDirectory() as tmp:
        legacy_path = Path(tmp) / "benchmark_receipt_legacy.py"
        legacy_path.write_text(source)
        spec = importlib.util.spec_from_file_location("benchmark_receipt_legacy", legacy_path)
        if spec is None or spec.loader is None:  # pragma: no cover
            return None
        module = importlib.util.module_from_spec(spec)
        sys.modules["benchmark_receipt_legacy"] = module
        spec.loader.exec_module(module)
        return module


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", default=None)
    parser.add_argument("--ledger", default=DEFAULT_LEDGER)
    parser.add_argument("--legacy-rev", default=DEFAULT_LEGACY_REV)
    parser.add_argument("--out", default="docs/evidence/v87/NX04")
    args = parser.parse_args(argv)

    repo_root = (
        Path(args.repo_root).resolve() if args.repo_root else Path(__file__).resolve().parents[1]
    )
    ledger_path = (repo_root / args.ledger).resolve()
    if not ledger_path.is_file():
        print(f"[NX04] FAIL: ledger absent at {ledger_path}")
        return 2
    out_dir = (repo_root / args.out).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    hash_before = _sha256(ledger_path)
    size_before = ledger_path.stat().st_size
    ledger = BenchmarkLedger.load_jsonl(ledger_path)

    probe_entries: list[dict[str, Any]] = []
    for index, entry in enumerate(ledger.entries):
        receipt = entry.receipt
        version = receipt.digest_version
        canon, refusal = build_canon_payload(
            digest_version=version,
            case_id=receipt.case_id,
            policy_id=receipt.policy_id,
            capability_score=receipt.capability_score,
            coverage_factor=receipt.coverage_factor,
            gates=receipt.gates,
            artifact_bindings=receipt.artifact_bindings,
            computed_at_timestamp_ns=receipt.computed_at_timestamp_ns,
            economic_evidence_digest=receipt.economic_evidence_digest,
            economic_receipt_path=receipt.economic_receipt_path,
            input_binding=receipt.input_binding,
        )
        reproduced = None
        if canon is not None:
            digest = hashlib.sha256(
                json.dumps(canon, separators=(",", ":")).encode()
            ).hexdigest()
            reproduced = digest == receipt.receipt_digest
        probe_entries.append(
            {
                "sequence_number": index,
                "digest_version": version,
                "canon_fields": None if canon is None else len(canon),
                "refusal": refusal,
                "stored_digest": receipt.receipt_digest,
                "reproduced": reproduced,
                "entry_hash": entry.entry_hash,
                "parent_entry_hash": entry.parent_entry_hash,
            }
        )

    by_version: dict[str, dict[str, Any]] = {}
    for record in probe_entries:
        bucket = by_version.setdefault(
            record["digest_version"],
            {"sequences": [], "canon_fields": set(), "all_reproduced": True},
        )
        bucket["sequences"].append(record["sequence_number"])
        if record["canon_fields"] is not None:
            bucket["canon_fields"].add(record["canon_fields"])
        bucket["all_reproduced"] = bucket["all_reproduced"] and bool(record["reproduced"])
    version_summary = {
        version: {
            "sequences": bucket["sequences"],
            "min_sequence": min(bucket["sequences"]),
            "max_sequence": max(bucket["sequences"]),
            "canon_fields": sorted(bucket["canon_fields"]),
            "all_entries_reproduce_stored_digest": bucket["all_reproduced"],
            "provenance": RECEIPT_CANON_TABLE[version].provenance
            if version in RECEIPT_CANON_TABLE
            else "NOT_IN_CANON_TABLE",
        }
        for version, bucket in sorted(by_version.items())
    }
    probe = {
        "ledger": args.ledger,
        "ledger_sha256_before": hash_before,
        "ledger_bytes": size_before,
        "entries": len(probe_entries),
        "version_summary": version_summary,
        "per_entry": probe_entries,
        "note": (
            "read-only analysis of the original bytes; the ledger was never rewritten "
            "and no digest was re-derived into it"
        ),
    }
    probe_path = out_dir / "ledger_canon_probe.json"
    probe_path.write_text(json.dumps(probe, indent=2, sort_keys=True) + "\n")

    report = ledger.verify_report()
    legacy = _load_legacy_module(repo_root, args.legacy_rev)
    legacy_verdict: dict[str, Any]
    if legacy is None:
        legacy_verdict = {"available": False}
    else:
        legacy_ledger = legacy.BenchmarkLedger.load_jsonl(ledger_path)
        ok, err = legacy_ledger.verify_chain()
        failing = []
        for index, entry in enumerate(legacy_ledger.entries):
            entry_ok, entry_err = entry.receipt.verify()
            if not entry_ok and not entry_err.startswith("ARTIFACT_TAMPERED"):
                failing.append(
                    {
                        "sequence_number": index,
                        "digest_version": entry.receipt.digest_version,
                        "verdict": entry_err,
                    }
                )
        legacy_verdict = {
            "available": True,
            "revision": args.legacy_rev,
            "verify_chain": {"ok": ok, "message": err},
            "entries_with_digest_failures": len(failing),
            "digest_failures": failing,
        }

    hash_after = _sha256(ledger_path)
    verifier_report = {
        "ledger": args.ledger,
        "ledger_sha256_before": hash_before,
        "ledger_sha256_after": hash_after,
        "ledger_unchanged": hash_before == hash_after,
        "before_legacy_verifier": legacy_verdict,
        "after_version_resolved_verifier": {
            "overall": report.overall,
            "chain_valid": report.chain_valid,
            "digests_valid": report.digests_valid,
            "artifacts_intact": report.artifacts_intact,
            "entries": len(report.entries),
            "invalid_entries": [
                e.as_dict() for e in report.entries if not e.fully_valid
            ],
        },
    }
    report_path = out_dir / "verifier_report.json"
    report_path.write_text(json.dumps(verifier_report, indent=2, sort_keys=True) + "\n")

    print(f"[NX04] ledger {args.ledger}")
    print(f"[NX04] sha256 before/after: {hash_before} / {hash_after} (unchanged={hash_before == hash_after})")
    for version, summary in version_summary.items():
        print(
            f"[NX04]   {version}: seq {summary['min_sequence']}-{summary['max_sequence']} "
            f"canon_fields={summary['canon_fields']} reproduced="
            f"{summary['all_entries_reproduce_stored_digest']}"
        )
    if legacy_verdict.get("available"):
        chain = legacy_verdict["verify_chain"]
        print(f"[NX04] legacy({args.legacy_rev}) verify_chain -> {chain['ok']} {chain['message']}")
        print(f"[NX04] legacy digest failures: {legacy_verdict['entries_with_digest_failures']}")
    print(f"[NX04] new verifier -> {report.overall} (entries={len(report.entries)})")
    print(f"[NX04] probe  sha256:{_sha256(probe_path)}")
    print(f"[NX04] report sha256:{_sha256(report_path)}")
    return 0 if report.ok and hash_before == hash_after else 3


if __name__ == "__main__":
    sys.exit(main())
