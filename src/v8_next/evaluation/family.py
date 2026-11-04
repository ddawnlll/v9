"""Complete registered development-trial inputs for numerical comparisons."""

import hashlib
from decimal import Decimal
from typing import Any

from v8_next.evaluation.alignment import IntervalLoss, paired_differentials
from v8_next.evaluation.deflated_sharpe import DSRPlan, deflated_sharpe_diagnostic
from v8_next.evaluation.equity import equity_losses
from v8_next.evaluation.excess import excess_losses
from v8_next.evaluation.overfitting import CSCVPlan
from v8_next.evaluation.store import ResearchStore, canonical


def family_losses(
    results: list[dict[str, Any]], *, store: ResearchStore, family: str, decision_ns: int
) -> dict[str, tuple[IntervalLoss, ...]]:
    """Recompute losses from marks and require every locally registered trial.

    Registry coverage cannot prove undisclosed search history or authenticate
    caller-owned artifacts. Historical development results never become OOS.
    Failed or missing trials cannot silently disappear from this comparison.
    """
    registered = {
        row[0]: row[1:]
        for row in store.db.execute(
            "SELECT trial_id,policy_hash,dataset_hash,role,registered_ns FROM trials WHERE family=?",
            (family,),
        )
    }
    ids = [r["trial_id"] for r in results]
    if len(ids) < 2 or len(set(ids)) != len(ids) or set(ids) != set(registered):
        raise ValueError("incomplete or duplicate registered trial family")
    output = {}
    reference = None
    for result in results:
        trial_id = result["trial_id"]
        frozen = result["frozen_policy"]
        policy_hash = hashlib.sha256(canonical(frozen).encode()).hexdigest()
        expected_id = hashlib.sha256(
            canonical([family, result["dataset_hash"], policy_hash]).encode()
        ).hexdigest()
        if (
            trial_id != expected_id
            or result["family"] != family
            or result["policy_hash"] != policy_hash
            or registered[trial_id]
            != (policy_hash, result["dataset_hash"], "DEVELOPMENT", result["registered_ns"])
            or frozen["role"] != "DEVELOPMENT"
            or result["scope"] != "OFFLINE_COUNTERFACTUAL_POLICY_EXPERIMENT"
            or result["promotion_eligible"] is not False
            or result["calibration_eligible"] is not False
            or not result["registered_ns"] <= result["computed_ns"] < decision_ns
        ):
            raise ValueError("trial identity, role or computation chronology mismatch")
        config = frozen["config"]
        signature = (
            result["dataset_hash"],
            canonical({key: value for key, value in frozen.items() if key != "config"}),
            tuple(config[k] for k in ("initial_balance", "maker_fee", "taker_fee")),
            tuple(
                (m["end_ns"], m["source_hash"], m["close_price"]) for m in result["equity_marks"]
            ),
        )
        if reference is None:
            reference = signature
        elif reference != signature:
            raise ValueError("incompatible data, runtime, valuation source or cost assumptions")
        output[trial_id] = equity_losses(
            result["equity_marks"],
            capital=Decimal(config["initial_balance"]),
            computed_ns=result["computed_ns"],
        )
    baseline = output[ids[0]]
    for losses in output.values():
        paired_differentials(
            baseline,
            losses,
            frozen_ns=baseline[0].start_ns,
            evaluation_end_ns=baseline[-1].end_ns,
            decision_ns=decision_ns,
        )
    return output


def compare_family(
    results: list[dict[str, Any]],
    *,
    store: ResearchStore,
    family: str,
    baseline_trial_id: str,
    decision_ns: int,
    block_size: int,
    reps: int,
    seed: int,
    pbo_plan: CSCVPlan | None = None,
    dsr_plan: DSRPlan | None = None,
    reference_losses: tuple[IntervalLoss, ...] | None = None,
    reference_basis: str | None = None,
) -> dict[str, Any]:
    """Explicit baseline SPA/WRC on the full local development family.

    Undefined/degenerate comparisons propagate as errors. No candidate is
    discarded to obtain a computable or favorable significance result.
    """
    from v8_next.evaluation.inference import spa_diagnostic

    if dsr_plan is None:
        if reference_losses is not None or reference_basis is not None:
            raise ValueError("excess reference supplied without DSR plan")
    elif reference_losses is None or not reference_basis or not reference_basis.strip():
        raise ValueError("DSR requires explicit reference losses and basis")
    losses = family_losses(results, store=store, family=family, decision_ns=decision_ns)
    if baseline_trial_id not in losses:
        raise ValueError("baseline must be an explicit registered family member")
    baseline = losses.pop(baseline_trial_id)
    diagnostic = spa_diagnostic(
        baseline,
        losses,
        frozen_ns=baseline[0].start_ns,
        evaluation_end_ns=baseline[-1].end_ns,
        decision_ns=decision_ns,
        block_size=block_size,
        reps=reps,
        seed=seed,
        pbo_plan=pbo_plan,
    )
    if dsr_plan is not None:
        assert reference_losses is not None
        diagnostic["dsr"] = deflated_sharpe_diagnostic(
            excess_losses(losses, reference_losses, decision_ns=decision_ns),
            plan=dsr_plan,
            frozen_ns=baseline[0].start_ns,
            evaluation_end_ns=baseline[-1].end_ns,
            decision_ns=decision_ns,
        )
        diagnostic["dsr_reference_losses"] = [
            {
                "start_ns": row.start_ns,
                "end_ns": row.end_ns,
                "available_ns": row.available_ns,
                "loss": str(row.loss),
            }
            for row in reference_losses
        ]
        diagnostic["dsr_reference_basis"] = reference_basis
        diagnostic["dsr_reference_status"] = "CALLER_SUPPLIED_NOT_SOURCE_CERTIFIED"
    return {
        "family": family,
        "baseline_trial_id": baseline_trial_id,
        "trial_policies": {
            result["trial_id"]: result["frozen_policy"]["config"]
            for result in sorted(results, key=lambda result: result["trial_id"])
        },
        "registered_trial_ids": sorted([baseline_trial_id, *losses]),
        "scope": "DEVELOPMENT_EXPLORATION_NOT_OOS",
        # NX07.R1: the family is complete with respect to the *local* registry and
        # nothing more. Undisclosed earlier trials cannot be proven absent, so the
        # receipt says so instead of implying the registered set is the whole search.
        "history_completeness": "UNKNOWN_UNDISCLOSED_TRIALS_POSSIBLE",
        "history_completeness_basis": (
            "the trials registry can prove which trials were recorded locally; it "
            "cannot prove that no earlier trial was run outside it"
        ),
        "promotion_eligible": False,
        "calibration_eligible": False,
        "claim_status": "NO_ECONOMIC_CLAIM",
        "loss_definition": "NEGATIVE_EQUITY_CHANGE_OVER_FIXED_INITIAL_CAPITAL",
        "diagnostic": diagnostic,
    }
