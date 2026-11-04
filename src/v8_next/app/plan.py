"""Freeze a future policy experiment before its observation window starts."""

import argparse
from pathlib import Path

from v8_next.app.observe import source_hash
from v8_next.evaluation.forward_plan import ForwardPlan, freeze_forward_plan
from v8_next.evaluation.store import ResearchStore, canonical


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("plan", type=Path)
    parser.add_argument("--id", required=True)
    parser.add_argument("--store", required=True, type=Path)
    args = parser.parse_args()
    plan = ForwardPlan.model_validate_json(args.plan.read_bytes())
    store = ResearchStore(args.store)
    try:
        digest = freeze_forward_plan(store, args.id, plan, source_hash())
        print(
            canonical(
                {
                    "plan_id": args.id,
                    "digest": digest,
                    "status": "PLAN_RECORDED_NOT_EXECUTED",
                    "claim_status": "NO_ECONOMIC_CLAIM",
                }
            )
        )
    finally:
        store.close()


if __name__ == "__main__":
    main()
