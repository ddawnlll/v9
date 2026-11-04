"""Compare a complete recorded DEVELOPMENT family; never grant economic authority."""

import argparse
import hashlib
import json
import time
from decimal import Decimal
from pathlib import Path

from v8_next.evaluation.family import compare_family
from v8_next.evaluation.overfitting import CSCVPlan
from v8_next.evaluation.reference import DSRReference
from v8_next.evaluation.store import ResearchStore, canonical


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("results", nargs="+", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--store", required=True, type=Path)
    parser.add_argument("--family", required=True)
    parser.add_argument("--baseline", required=True, help="Registered baseline trial ID")
    parser.add_argument("--block-size", required=True, type=int)
    parser.add_argument("--reps", required=True, type=int)
    parser.add_argument("--seed", required=True, type=int)
    parser.add_argument("--pbo-plan", type=Path, help="Explicit CSCV JSON plan; baseline excluded")
    parser.add_argument(
        "--dsr-reference", type=Path, help="Explicit DSR plan and reference returns"
    )
    args = parser.parse_args()
    if args.output.exists():
        raise ValueError("comparison output already exists")
    if not args.store.is_file():
        raise ValueError("existing research registry required")
    records = []
    inputs = []
    for path in args.results:
        raw = path.read_bytes()
        records.append(json.loads(raw))
        inputs.append({"path": str(path.resolve()), "sha256": hashlib.sha256(raw).hexdigest()})
    pbo_plan = None
    if args.pbo_plan is not None:
        raw = args.pbo_plan.read_bytes()
        plan = json.loads(raw)
        required_fields = {"partitions", "metric", "max_splits", "registered_variants"}
        allowed_fields = required_fields | {"purge_bars", "embargo_bars"}
        if not (required_fields <= set(plan) <= allowed_fields):
            raise ValueError("PBO plan requires exactly the documented fields")
        if (
            type(plan["partitions"]) is not int
            or type(plan["max_splits"]) is not int
            or plan["metric"] not in {"mean_return", "sharpe"}
            or not isinstance(plan["registered_variants"], list)
            or any(not isinstance(v, str) or not v for v in plan["registered_variants"])
            or type(plan.get("purge_bars", 0)) is not int
            or type(plan.get("embargo_bars", 0)) is not int
        ):
            raise ValueError("invalid typed PBO plan")
        pbo_plan = CSCVPlan(
            plan["partitions"],
            plan["metric"],
            plan["max_splits"],
            tuple(plan["registered_variants"]),
            purge_bars=plan.get("purge_bars", 0),
            embargo_bars=plan.get("embargo_bars", 0),
        )
        inputs.append(
            {"path": str(args.pbo_plan.resolve()), "sha256": hashlib.sha256(raw).hexdigest()}
        )
    dsr_reference = None
    if args.dsr_reference is not None:
        raw = args.dsr_reference.read_bytes()
        dsr_reference = DSRReference.model_validate_json(raw)
        if any(
            Decimal(r["frozen_policy"]["config"]["initial_balance"]) != dsr_reference.capital
            for r in records
        ):
            raise ValueError("DSR reference capital differs from trial capital")
        inputs.append(
            {"path": str(args.dsr_reference.resolve()), "sha256": hashlib.sha256(raw).hexdigest()}
        )
    store = ResearchStore(args.store)
    try:
        decision_ns = time.time_ns()
        report = compare_family(
            records,
            store=store,
            family=args.family,
            baseline_trial_id=args.baseline,
            decision_ns=decision_ns,
            block_size=args.block_size,
            reps=args.reps,
            seed=args.seed,
            pbo_plan=pbo_plan,
            dsr_plan=dsr_reference.plan() if dsr_reference else None,
            reference_losses=dsr_reference.losses() if dsr_reference else None,
            reference_basis=dsr_reference.reference_basis if dsr_reference else None,
        )
        if dsr_reference is not None:
            report["dsr_reference_metadata"] = dsr_reference.model_dump(
                mode="json", exclude={"intervals"}
            )
        report["inputs"] = inputs
        report["decision_ns"] = decision_ns
        serialized = canonical(report)
        with args.output.open("x") as stream:
            stream.write(serialized + "\n")
    finally:
        store.close()


if __name__ == "__main__":
    main()
