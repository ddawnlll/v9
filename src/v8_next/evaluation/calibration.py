"""Inspect actual prospective accounting evidence before permitting calibration."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

from v8_next.adapters.accounting_replay import replay_frozen_campaigns
from v8_next.adapters.captured_market import load_candles
from v8_next.adapters.engine_state import reconcile_replay
from v8_next.app.evaluate import evaluate
from v8_next.app.observe import source_hash
from v8_next.app.paper import replay_account
from v8_next.domain.campaign import PaperCampaign
from v8_next.evaluation.store import ResearchStore


def inspect_calibration_source(
    run: Path,
    decision_ns: int,
    *,
    bootstrap_plan: tuple[int, int, int] | None = None,
    training_window: tuple[int, int] | None = None,
    research_store: ResearchStore | None = None,
) -> dict[str, Any]:
    """Recompute source accounting; never trust a serialized verified flag.

    This is evidence admission, not an estimator or a certificate issuer. No
    forecast value is produced until a complete eligible outcome sample exists.
    """
    if training_window is not None and bootstrap_plan is None:
        raise ValueError("training window requires a bootstrap plan")
    frozen = json.loads((run / "policy.json").read_text())
    if frozen["policy"].get("code_and_lock_hash") != source_hash():
        raise ValueError("calibration requires the source run frozen runtime")
    checkpoint = json.loads((run / "paper-state.json").read_text())
    revised = checkpoint["revised_accounting"]
    cutoff = int(revised["accounting_as_of_ns"])
    if cutoff >= decision_ns:
        raise ValueError("calibration evidence reaches decision time or future")
    paths = []
    for name, expected_hash in zip(
        checkpoint["manifests"], checkpoint["manifest_hashes"], strict=True
    ):
        path = (run / name).resolve()
        if not path.is_relative_to(run.resolve()):
            raise ValueError("calibration source escapes run")
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected_hash:
            raise ValueError("calibration source hash mismatch")
        paths.append(path)
    if research_store is not None:
        for path in paths:
            candles = load_candles(path)
            if not candles or len({c.instrument_id for c in candles}) != 1:
                raise ValueError("training source requires single-instrument candle coverage")
            research_store.assert_no_holdout_overlap(
                candles[0].instrument_id,
                min(c.start_ns for c in candles),
                max(c.end_ns for c in candles),
            )
    evaluation = evaluate(run)
    if checkpoint["policy_hash"] != evaluation["policy_hash"]:
        raise ValueError("accounting belongs to another policy")
    recovered_decisions = replay_account(
        paths,
        frozen["policy"]["paper_config"],
        **(
            {"experiment_frozen_ns": frozen["frozen_ns"]}
            if frozen["policy"]["paper_config"].get("experiment_window")
            else {}
        ),
    )
    reconcile_replay(checkpoint["native_state"], recovered_decisions)
    campaigns = tuple(PaperCampaign.from_record(c) for c in recovered_decisions["campaigns"])
    recomputed = replay_frozen_campaigns(paths, campaigns, frozen["policy"]["paper_config"], cutoff)
    reconcile_replay(revised, recomputed)
    positions = recomputed["positions"]
    closed = recomputed["position_closures"]
    open_positions = [p for p in positions if not p["is_closed"]]
    if any(
        p["closed_ns"] is None or not p["opened_ns"] <= p["closed_ns"] <= cutoff for p in closed
    ):
        raise ValueError("closed outcome has invalid accounting clocks")
    blockers = outcome_sample_blockers(
        [{**p, "is_closed": True} for p in closed] + open_positions,
        recomputed["funding_coverage"],
    )
    reason = blockers[0]
    component_estimates = None
    selection_cash_estimate = None
    if bootstrap_plan is not None:
        from v8_next.evaluation.component_estimates import estimate_components
        from v8_next.evaluation.selection_estimates import estimate_selection_cash

        block_size, reps, seed = bootstrap_plan
        selection_cash_estimate = estimate_selection_cash(
            recomputed["outcomes"],
            block_size=block_size,
            reps=reps,
            seed=seed,
            training_window=training_window,
            accounting_as_of_ns=cutoff,
        )
        component_estimates = estimate_components(
            recomputed["outcomes"],
            decision_ns=decision_ns,
            block_size=block_size,
            reps=reps,
            seed=seed,
            training_window=training_window,
        )
    return {
        "claim_status": "NO_ECONOMIC_CLAIM",
        "holdout_overlap_check": "LOCAL_DECLARED_CANDLE_COVERAGE_ONLY"
        if research_store is not None
        else "NOT_CHECKED",
        "source_policy_hash": evaluation["policy_hash"],
        "source_checkpoint_sha256": hashlib.sha256(
            (run / "paper-state.json").read_bytes()
        ).hexdigest(),
        "accounting_recomputed": True,
        "campaign_decisions_recomputed": True,
        "position_records": len(positions),
        "closed_position_records": len(closed),
        "open_position_records": len(open_positions),
        "campaign_outcomes": recomputed["outcomes"],
        "outcome_realization": recomputed["realization"],
        "funding_coverage": recomputed["funding_coverage"],
        "closed_outcomes": sorted(closed, key=lambda p: (p["closed_ns"], p["instrument_id"])),
        "component_estimates": component_estimates,
        "selection_cash_estimate": selection_cash_estimate,
        "eligible_for_utility": False,
        "reason": reason,
        "blockers": blockers,
        "gross_edge": None,
        "uncertainty": None,
    }


def outcome_sample_blockers(positions: list[dict[str, Any]], funding_coverage: str) -> list[str]:
    """Report all unresolved sample gates; closed-only selection can be biased.

    This is not a calibration estimator. Open outcomes need an explicit censoring
    or horizon methodology before closed observations can define a sample.
    """
    blockers = []
    if not positions:
        blockers.append("NO_EXECUTED_OUTCOME_SAMPLE")
    elif not any(p["is_closed"] for p in positions):
        blockers.append("NO_CLOSED_OUTCOME_SAMPLE")
    if any(not p["is_closed"] for p in positions):
        blockers.append("OPEN_OUTCOME_CENSORING_POLICY_REQUIRED")
    if funding_coverage != "COMPLETE" and not (
        not positions and funding_coverage == "NOT_APPLICABLE_NO_POSITION_EXPOSURE"
    ):
        blockers.append("FUNDING_COVERAGE_UNQUALIFIED")
    blockers.append("STATISTICAL_METHOD_AND_TRIAL_FAMILY_REVIEW_REQUIRED")
    return blockers


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_directory", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--decision-ns", type=int, required=True)
    parser.add_argument("--block-size", type=int)
    parser.add_argument("--reps", type=int)
    parser.add_argument("--seed", type=int)
    parser.add_argument("--research-store", type=Path)
    parser.add_argument("--training-start-ns", type=int)
    parser.add_argument("--training-end-ns", type=int)
    args = parser.parse_args()
    plan = (args.block_size, args.reps, args.seed)
    if any(v is not None for v in plan) and any(v is None for v in plan):
        raise ValueError("component estimates require block-size, reps and seed together")
    window = (args.training_start_ns, args.training_end_ns)
    if any(v is not None for v in window) and (any(v is None for v in window) or plan[0] is None):
        raise ValueError("training window requires both endpoints and a bootstrap plan")
    store = None
    if args.research_store is not None:
        if not args.research_store.is_file():
            raise ValueError("research store must already exist")
        store = ResearchStore(args.research_store)
    try:
        result = inspect_calibration_source(
            args.run_directory,
            args.decision_ns,
            bootstrap_plan=plan if plan[0] is not None else None,
            training_window=window if window[0] is not None else None,
            research_store=store,
        )
    finally:
        if store is not None:
            store.close()
    with args.output.open("x") as output:
        json.dump(result, output, indent=2)
        output.write("\n")


if __name__ == "__main__":
    main()
