"""Read-only paper report with recomputed decision and accounting provenance."""

import argparse
import json
from decimal import Decimal
from pathlib import Path
from typing import Any

from v8_next.adapters.accounting_replay import replay_frozen_campaigns
from v8_next.app.evaluate import evaluate
from v8_next.app.paper import replay_account
from v8_next.domain.campaign import PaperCampaign
from v8_next.evaluation.calibration import inspect_calibration_source, outcome_sample_blockers
from v8_next.evaluation.cash_return import terminal_cash_return
from v8_next.evaluation.inference import trajectory_spa_diagnostic
from v8_next.evaluation.trajectory import cash_trajectory


def report(
    run: Path,
    decision_ns: int,
    *,
    include_trajectory: bool = False,
    exploratory_spa: tuple[int, int, int] | None = None,
) -> dict[str, Any]:
    observations = evaluate(run)
    outcomes = inspect_calibration_source(run, decision_ns)
    frozen = json.loads((run / "policy.json").read_text())
    if frozen["policy"]["baseline"] != "range-breakout-without-compression-v1":
        raise ValueError("unsupported frozen baseline definition")
    checkpoint = json.loads((run / "paper-state.json").read_text())
    # The source inspector above verifies these exact captures and the recorded
    # selected-policy decisions. The baseline shares clocks, costs and admission policy.
    baseline = replay_account(
        [run / name for name in checkpoint["manifests"]],
        frozen["policy"]["paper_config"],
        observer="breakout_baseline",
        **(
            {"experiment_frozen_ns": frozen["frozen_ns"]}
            if frozen["policy"]["paper_config"].get("experiment_window")
            else {}
        ),
    )
    baseline_campaigns = tuple(
        PaperCampaign.from_record(campaign) for campaign in baseline["campaigns"]
    )
    baseline_accounting = replay_frozen_campaigns(
        [run / name for name in checkpoint["manifests"]],
        baseline_campaigns,
        frozen["policy"]["paper_config"],
        int(checkpoint["revised_accounting"]["accounting_as_of_ns"]),
    )
    trajectory = (
        cash_trajectory(
            [run / name for name in checkpoint["manifests"]],
            frozen["policy"]["paper_config"],
            int(frozen["frozen_ns"]),
            decision_ns,
        )
        if include_trajectory or exploratory_spa is not None
        else None
    )
    return {
        "schema_version": 1,
        "campaign_policy": frozen["policy"]["paper_config"].get(
            "campaign_policy", "timeout-only-v1"
        ),
        "execution_grammar_policy": frozen["policy"].get(
            "execution_grammar_policy", "range-breakout-48-v1"
        ),
        "execution_observer_policy": frozen["policy"].get("execution_observer_policy", "squeeze"),
        "claim_status": "NO_ECONOMIC_CLAIM",
        "observations": observations,
        "outcomes": outcomes,
        "comparison": {
            "status": "NOT_COMPUTED",
            "reason": outcomes["reason"],
            "variant_blockers": outcomes.get("blockers", [outcomes["reason"]]),
            "baseline_blockers": outcome_sample_blockers(
                baseline_accounting["positions"], baseline_accounting["funding_coverage"]
            )
            if "positions" in baseline_accounting
            else ["BASELINE_OUTCOME_SAMPLE_UNAVAILABLE"],
            "paired_loss_sample": None,
            "cash_trajectory": trajectory,
            "exploratory_spa": trajectory_spa_diagnostic(
                trajectory,
                decision_ns=decision_ns,
                block_size=exploratory_spa[0],
                reps=exploratory_spa[1],
                seed=exploratory_spa[2],
            )
            if exploratory_spa is not None and trajectory is not None
            else None,
            "baseline_cash_return": terminal_cash_return(
                baseline_accounting, Decimal(frozen["policy"]["paper_config"]["initial_balance"])
            ),
            "variant_cash_return": terminal_cash_return(
                checkpoint["revised_accounting"],
                Decimal(frozen["policy"]["paper_config"]["initial_balance"]),
            ),
            "baseline_native_replay": baseline,
            "variant_native_state": checkpoint["native_state"],
            "baseline_revised_accounting": baseline_accounting,
            "variant_revised_accounting": checkpoint["revised_accounting"],
            "replay_scope": "SAME_CAPTURE_AND_ADMISSION_POLICY_SIMULATED_DIAGNOSTIC",
            "loss_status": "NO_QUALIFIED_PAIRED_ACCOUNT_INTERVALS",
            "no_trade_is_not_edge_evidence": True,
            "spa": None,
            "wrc": None,
            "dsr": None,
            "pbo": None,
            "additional_requirement": "verified_aligned_baseline_and_variant_account_intervals",
        },
        "promotion_eligible": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_directory", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--decision-ns", type=int, required=True)
    parser.add_argument(
        "--trajectory",
        action="store_true",
        help="Replay every capture prefix; cost grows with session length",
    )
    parser.add_argument("--exploratory-spa", nargs=3, type=int, metavar=("BLOCK", "REPS", "SEED"))
    args = parser.parse_args()
    result = report(
        args.run_directory,
        args.decision_ns,
        include_trajectory=args.trajectory,
        exploratory_spa=tuple(args.exploratory_spa) if args.exploratory_spa else None,
    )
    with args.output.open("x") as output:
        json.dump(result, output, indent=2)
        output.write("\n")


if __name__ == "__main__":
    main()
