#!/usr/bin/env python
"""NX01 (#422) R1/R2/R5/R6 — read-only four-year tape inventory + burn manifest.

Writes physical artifacts under ``--out`` (default ``docs/evidence/v87/NX01``):

* ``tape_inventory.json`` — identity, coverage, archive three-way verification
* ``burn_map.json``        — verified recorded accesses, burn segments, calendar
* ``manifest.json``        — revision-bound ``TapeManifest``
* ``calendar.json``        — the 24/12/12 calendar, written and re-read

Nothing is synthesized. A missing archive, an unverifiable citation or an
unmeasurable slice fails the run instead of producing a partial claim.

Usage (from the repository root):

    uv run --project . --extra dev --extra research \\
        python tools/tape_inventory.py
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path

from v8_next.evaluation.tape_identity import (
    build_manifest,
    burn_segments,
    inventory_tape,
    measure_covering_tape,
    policy_lineage_burn_table,
    readback_calendar,
    runtime_identity,
    swing_calendar,
)

#: Physical tapes whose window sits inside the last 12 months of the four-year
#: window; their existence is the measured evidence that the tail was consumed.
TAIL_TAPES = (
    "research/tape/quad-1h-12m",
    "research/tape/btcusdt-1h-12m",
    "research/tape/sol-dev-solusdt-2025-07-2026-07",
)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _git(repo_root: Path, *args: str) -> str:
    try:
        return subprocess.run(
            ["git", *args], cwd=repo_root, capture_output=True, text=True, check=True
        ).stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return "UNAVAILABLE"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo-root", default=None, help="repository root (default: two levels up)"
    )
    parser.add_argument(
        "--tape-dir", default="research/tape/multi-1h-4y", help="four-year tape directory"
    )
    parser.add_argument("--out", default="docs/evidence/v87/NX01", help="artifact directory")
    parser.add_argument(
        "--skip-archives",
        action="store_true",
        help="skip the 960-archive three-way verification (default: verify)",
    )
    args = parser.parse_args(argv)

    repo_root = (
        Path(args.repo_root).resolve()
        if args.repo_root
        else Path(__file__).resolve().parents[1]
    )
    tape_dir = (repo_root / args.tape_dir).resolve()
    tape = tape_dir / "tape.jsonl"
    out_dir = (repo_root / args.out).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"[NX01] repo_root={repo_root}")
    print(f"[NX01] tape      ={tape}")
    inventory = inventory_tape(tape, verify_archive_set=not args.skip_archives, archive_dir=tape_dir)
    print(
        f"[NX01] sha256={inventory.tape_sha256} rows={inventory.total_rows} "
        f"symbols={len(inventory.symbols)} archives={inventory.archive_matches}"
        f"/{inventory.archive_total}"
    )

    tail = []
    for rel in TAIL_TAPES:
        candidate = repo_root / rel
        if (candidate / "tape.jsonl").is_file():
            tail.append(measure_covering_tape(candidate))
    if not tail:
        print("[NX01] FAIL: no physical tail tape found; the tail burn cannot be claimed")
        return 2
    tail_start = min(t.window_start_ms for t in tail)

    verified = policy_lineage_burn_table(repo_root=repo_root, tape_path=tape)
    segments = burn_segments(
        inventory=inventory,
        verified_accesses=verified,
        tail_burned_from_ms=tail_start,
        tail_evidence=tuple(f"{t.path} sha256:{t.sha256}" for t in tail),
    )
    calendar = swing_calendar(inventory=inventory, segments=segments)
    config_identity = hashlib.sha256(
        json.dumps(
            {
                "loader": "v8_next.evaluation.multitape.load_multitape",
                "strict_intersection": True,
                "timeframe": inventory.interval,
                "window": [inventory.window_start_ms, inventory.window_end_ms],
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    manifest = build_manifest(
        inventory=inventory, calendar=calendar, config_identity=config_identity
    )

    written: dict[str, Path] = {}

    def write(name: str, payload: object) -> None:
        path = out_dir / name
        path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
        written[name] = path

    identity = {
        "checkout_sha": _git(repo_root, "rev-parse", "HEAD"),
        "working_tree_dirty": bool(_git(repo_root, "status", "--porcelain")),
        "uv_lock_sha256": _sha256(repo_root / "uv.lock"),
        "v8_next_uv_lock_sha256": _sha256(repo_root / "uv.lock"),
        "runtime": runtime_identity(),
        "config_identity": config_identity,
    }
    write("tape_inventory.json", {**inventory.as_dict(), "checkout": identity})
    write(
        "burn_map.json",
        {
            "verified_accesses": [
                {
                    "label": access.label,
                    "evidence": f"{access.evidence_file}:{access.evidence_line}",
                    "requires": access.evidence_contains,
                    "slice_rows": access.slice_rows,
                    "measured_from_ms": window[0],
                    "measured_to_ms": window[1],
                }
                for access, window in verified
            ],
            "tail_tapes": [
                {
                    "path": t.path,
                    "sha256": t.sha256,
                    "instruments": list(t.instruments),
                    "window_start_ms": t.window_start_ms,
                    "window_end_ms": t.window_end_ms,
                }
                for t in tail
            ],
            "segments": [s.as_dict() for s in segments],
            "calendar": calendar.as_dict(),
            "no_protected_final": calendar.no_protected_final,
            "final_eligibility_reason": calendar.final_ineligibility_reason,
        },
    )
    write("manifest.json", manifest.as_dict())
    readback = readback_calendar(calendar, out_dir / "calendar.json")
    written["calendar.json"] = out_dir / "calendar.json"
    if readback.as_dict() != calendar.as_dict():
        print("[NX01] FAIL: calendar read-back disagreement")
        return 3

    rows = []
    for name, path in sorted(written.items()):
        rows.append((name, path.stat().st_size, _sha256(path)))
    print("[NX01] artifacts:")
    for name, size, digest in rows:
        print(f"  {name:24s} {size:>8d} B  sha256:{digest}")
    print(f"[NX01] final_eligible={calendar.final_eligible}")
    print(f"[NX01] reason={calendar.final_ineligibility_reason}")
    print(f"[NX01] manifest_identity={manifest.identity_digest()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
