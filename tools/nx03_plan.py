#!/usr/bin/env python
"""NX03 (#424) R6 — build, freeze and read back the real historical walk-forward plan.

Writes under ``--out``:

* ``historical_plan.json`` — the plan as registered (windows in UTC, measured
  warmup/purge requirements, aggregation, final eligibility and its reason);
* ``plan_readback.json`` — the store read-back proof: the registered plan
  re-derived from the store, its digest, and the equality check against the plan
  written above.

The plan is derived from the NX01 calendar on the real tape; nothing is synthesized
and no forward (prospective) table is touched.

Usage (from the repository root):

    uv run --project . --extra dev --extra research \\
        python tools/nx03_plan.py
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

from v8_next.evaluation.historical_plan import (
    build_historical_plan,
    freeze_historical_plan,
    readback_historical_plan,
)
from v8_next.evaluation.store import ResearchStore
from v8_next.evaluation.tape_identity import (
    burn_segments,
    inventory_tape,
    measure_covering_tape,
    policy_lineage_burn_table,
    swing_calendar,
)

HOUR_NS = 3_600 * 10**9
TAIL_TAPES = (
    "research/tape/quad-1h-12m",
    "research/tape/btcusdt-1h-12m",
    "research/tape/sol-dev-solusdt-2025-07-2026-07",
)
#: The policy set the plan names. Kept as one explicit tuple so a typo cannot
#: silently shrink the measured warmup/purge.
PLAN_POLICIES = ("range-breakout-48-v1", "trend-continuation-v2", "mean-reversion-v2")


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", default=None)
    parser.add_argument("--tape-dir", default="research/tape/multi-1h-4y")
    parser.add_argument("--out", default="docs/evidence/v87/NX03")
    parser.add_argument("--plan-id", default="NX03-REAL-01")
    args = parser.parse_args(argv)

    repo_root = (
        Path(args.repo_root).resolve() if args.repo_root else Path(__file__).resolve().parents[1]
    )
    tape = (repo_root / args.tape_dir).resolve() / "tape.jsonl"
    if not tape.is_file():
        print(f"[NX03] FAIL: tape absent at {tape}")
        return 2
    out_dir = (repo_root / args.out).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    inventory = inventory_tape(tape)
    tail = [
        measure_covering_tape(repo_root / rel)
        for rel in TAIL_TAPES
        if (repo_root / rel / "tape.jsonl").is_file()
    ]
    verified = policy_lineage_burn_table(repo_root=repo_root, tape_path=tape)
    segments = burn_segments(
        inventory=inventory,
        verified_accesses=verified,
        tail_burned_from_ms=min(t.window_start_ms for t in tail),
        tail_evidence=tuple(f"{t.path} sha256:{t.sha256}" for t in tail),
    )
    calendar = swing_calendar(inventory=inventory, segments=segments)

    plan = build_historical_plan(
        calendar=calendar,
        dataset_id=inventory.data_id,
        instrument="BTCUSDT-PERP.BINANCE",
        policies=PLAN_POLICIES,
        baseline="range-breakout-48-v1",
        initial_balance_usdt="10000",
        maker_fee="0.0002",
        taker_fee="0.0005",
        bar_ns=HOUR_NS,
        aggregation="INDEPENDENT_FOLD_RESET",
        plan_id=args.plan_id,
        code_and_lock_hash="nx03-plan-tool",
    )
    ok, reason = plan.verify()
    print(f"[NX03] build verify: {ok} {reason}")

    plan_path = out_dir / "historical_plan.json"
    plan_path.write_text(json.dumps(plan.as_dict(), indent=2, sort_keys=True) + "\n")

    store = ResearchStore(out_dir / "plan_store.sqlite")
    try:
        digest = freeze_historical_plan(store, plan, registered_ns=1)
        re_read = readback_historical_plan(store, plan.plan_id)
        forward_rows = store.db.execute("SELECT COUNT(*) FROM forward_plans").fetchone()[0]
        forward_bindings = store.db.execute("SELECT COUNT(*) FROM forward_bindings").fetchone()[0]
        readback = {
            "plan_id": plan.plan_id,
            "registered_digest": digest,
            "readback_digest": re_read.digest(),
            "digests_match": digest == re_read.digest(),
            "plan_payload_matches": re_read.as_dict() == plan.as_dict(),
            "readback_verify": list(re_read.verify()),
            "forward_plans_rows_untouched": forward_rows,
            "forward_bindings_rows_untouched": forward_bindings,
            "windows": [
                {
                    "fold_id": f.fold_id,
                    "role": f.role,
                    "scored_start_utc": f.as_dict()["scored_start_utc"],
                    "scored_end_utc": f.as_dict()["scored_end_utc"],
                    "tape_role": f.tape_role,
                }
                for f in re_read.folds
            ],
        }
    finally:
        store.close()
    readback_path = out_dir / "plan_readback.json"
    readback_path.write_text(json.dumps(readback, indent=2, sort_keys=True) + "\n")

    print(f"[NX03] dataset_id={inventory.data_id}")
    print(f"[NX03] calendar_digest={plan.calendar_digest}")
    print(f"[NX03] requirements {json.dumps(plan.history.as_dict(), sort_keys=True)}")
    print(f"[NX03] folds={len(plan.folds)} final_eligible={plan.final_eligible}")
    print(f"[NX03] reason={plan.final_eligibility_reason}")
    print(f"[NX03] plan_digest={plan.digest()}")
    print(f"[NX03] readback_match={readback['plan_payload_matches']} forward_rows={readback['forward_plans_rows_untouched']}")
    print(f"[NX03] plan     sha256:{_sha256(plan_path)}")
    print(f"[NX03] readback sha256:{_sha256(readback_path)}")
    return 0 if ok and readback["plan_payload_matches"] else 3


if __name__ == "__main__":
    sys.exit(main())
