"""Execute every frozen forward policy; modeled replay remains diagnostic."""

import argparse
import hashlib
import time
from pathlib import Path
from typing import Any

from v8_next.adapters.captured_market import load_candles
from v8_next.app.observe import source_hash
from v8_next.app.trial import _run_trial
from v8_next.evaluation.equity import equity_losses
from v8_next.evaluation.forward_plan import bind_forward_data
from v8_next.evaluation.inference import spa_diagnostic
from v8_next.evaluation.store import ResearchStore, canonical


def run_forward(manifest: Path, store: ResearchStore, plan_id: str) -> dict[str, Any]:
    dataset_hash = hashlib.sha256(manifest.read_bytes()).hexdigest()
    candles = load_candles(manifest)
    code_hash = source_hash()
    plan = bind_forward_data(store, plan_id, dataset_hash, candles, code_hash)
    results = {}
    for name, policy in sorted(plan.policies.items()):
        if (
            source_hash() != code_hash
            or hashlib.sha256(manifest.read_bytes()).hexdigest() != dataset_hash
        ):
            raise ValueError("forward input/runtime changed during execution")
        result = _run_trial(
            manifest,
            policy,
            store,
            "forward:" + plan_id,
            "HOLDOUT",
            selection_start_ns=plan.start_ns,
            selection_end_ns=plan.end_ns,
        )
        if result["frozen_policy"]["code_and_lock_hash"] != code_hash:
            raise ValueError("forward runtime changed during trial")
        results[name] = result
    losses = {
        name: equity_losses(
            [
                mark
                for mark in result["equity_marks"]
                if plan.start_ns <= mark["end_ns"] <= plan.end_ns
            ],
            capital=plan.policies[name].initial_balance,
            computed_ns=result["computed_ns"],
        )
        for name, result in results.items()
    }
    baseline = losses.pop(plan.baseline)
    diagnostic = spa_diagnostic(
        baseline,
        losses,
        frozen_ns=baseline[0].start_ns,
        evaluation_end_ns=baseline[-1].end_ns,
        decision_ns=time.time_ns(),
        block_size=plan.block_size,
        reps=plan.reps,
        seed=plan.seed,
    )
    return {
        "plan_id": plan_id,
        "dataset_hash": dataset_hash,
        "selection_window": {
            "start_ns": plan.start_ns,
            "end_ns": plan.end_ns,
            "interval": "[start,end)",
        },
        "warmup_bars": plan.warmup_bars,
        "scope": "PREREGISTERED_FORWARD_WINDOW_MODELED_REPLAY",
        "claim_status": "NO_ECONOMIC_CLAIM",
        "promotion_eligible": False,
        "calibration_eligible": False,
        "trials": results,
        "diagnostic": diagnostic,
        "limitations": [
            "historical_close_availability_is_modeled",
            "local_registry_and_clock_trust",
            "external_prior_access_unverified",
            "venue_cost_and_fill_qualification_incomplete",
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--plan-id", required=True)
    parser.add_argument("--store", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists() or not args.store.is_file():
        raise ValueError("new output and existing plan registry required")
    store = ResearchStore(args.store)
    try:
        report = run_forward(args.manifest, store, args.plan_id)
        text = canonical(report)
        with args.output.open("x") as stream:
            stream.write(text + "\n")
    finally:
        store.close()


if __name__ == "__main__":
    main()
