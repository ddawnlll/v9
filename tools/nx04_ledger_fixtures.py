#!/usr/bin/env python
"""NX04 (#425) R5 — extract golden digest fixtures from the REAL ledger, read-only.

For every supported digest version, one real entry is copied verbatim (its receipt
fields and its stored ``receipt_digest``) into
``tests/fixtures/ledger_canon/<version>.json``. Artifact bindings are kept
as metadata but their files are never touched: ``verify_digest`` is a pure digest
recomputation, so the fixture stays self-contained.

The digest in each fixture was produced by the historical producer revision of
that era, not by this tree -- that is what makes it golden. Rewriting a digest
here would destroy the only independent evidence the canon table has.

Usage (from the repository root):

    uv run --project . --extra dev --extra research \\
        python tools/nx04_ledger_fixtures.py
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

DEFAULT_LEDGER = "artifacts/benchmarks/benchmark_ledger.jsonl"

#: Digest versions whose layout has been proven (see RECEIPT_CANON_TABLE).
SUPPORTED_VERSIONS = ("v8.5-digest-v2", "v8.5-digest-v3", "v8.5-digest-v4")


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", default=None)
    parser.add_argument("--ledger", default=DEFAULT_LEDGER)
    parser.add_argument("--out", default="tests/fixtures/ledger_canon")
    args = parser.parse_args(argv)

    repo_root = (
        Path(args.repo_root).resolve() if args.repo_root else Path(__file__).resolve().parents[1]
    )
    ledger_path = (repo_root / args.ledger).resolve()
    if not ledger_path.is_file():
        print(f"[NX04] FAIL: ledger absent at {ledger_path}")
        return 2
    ledger_sha = _sha256(ledger_path)
    out_dir = (repo_root / args.out).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    rows = [
        json.loads(line) for line in ledger_path.read_text().splitlines() if line.strip()
    ]
    written: dict[str, str] = {}
    for version in SUPPORTED_VERSIONS:
        match = next(
            (
                (index, row)
                for index, row in enumerate(rows)
                if row["receipt"].get("digest_version") == version
            ),
            None,
        )
        if match is None:
            print(f"[NX04] FAIL: no real entry carries {version}; canon is unproven")
            return 3
        index, row = match
        receipt = row["receipt"]
        fixture = {
            "fixture_source": f"{args.ledger} entry {index} (verbatim, read-only)",
            "fixture_ledger_sha256": ledger_sha,
            "digest_version": version,
            "sequence_number": row["sequence_number"],
            "entry_hash": row["entry_hash"],
            "parent_entry_hash": row["parent_entry_hash"],
            "receipt": receipt,
        }
        path = out_dir / f"{version}.json"
        path.write_text(json.dumps(fixture, indent=2, sort_keys=True) + "\n")
        written[version] = _sha256(path)
        print(f"[NX04] {version}: ledger entry {index} -> {path.name} sha256:{written[version]}")

    index_manifest = out_dir / "MANIFEST.json"
    index_manifest.write_text(
        json.dumps(
            {
                "ledger": args.ledger,
                "ledger_sha256": ledger_sha,
                "note": (
                    "digests are the historical producers' own; extracted read-only, never "
                    "re-derived or re-written"
                ),
                "fixtures": written,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )
    print(f"[NX04] manifest sha256:{_sha256(index_manifest)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
