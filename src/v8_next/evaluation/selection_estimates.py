"""Descriptive full-selection cash uncertainty from caller-recomputed outcomes."""

import hashlib
import importlib
import importlib.metadata
from decimal import Decimal
from typing import Any

import numpy as np

from v8_next.evaluation.store import canonical


def estimate_selection_cash(
    outcomes: dict[str, Any],
    *,
    block_size: int,
    reps: int,
    seed: int,
    training_window: tuple[int, int] | None = None,
    accounting_as_of_ns: int | None = None,
) -> dict[str, Any]:
    """No cohort filtering. Nonentry has no cash flow, not a fabricated filled R.

    Caller must recompute native accounting and campaign observations. This
    function cannot authenticate a serialized outcome report or qualify OOS.
    """
    result: dict[str, Any] = dict(
        scope="DESCRIPTIVE_SELECTION_CASH_NOT_EXPECTED_UTILITY",
        claim_status="NO_ECONOMIC_CLAIM",
        eligible_for_utility=False,
        estimate=None,
        source_sha256=hashlib.sha256(canonical(outcomes).encode()).hexdigest(),
    )
    if (
        any(type(v) is not int for v in (block_size, reps, seed))
        or block_size < 1
        or reps < 2
        or seed < 0
    ):
        raise ValueError("invalid resampling plan")
    result.update(block_size=block_size, reps=reps, seed=seed, sample_count=len(outcomes["rows"]))
    rows = outcomes["rows"]
    score = outcomes["selection_cash_scorecard"]
    if (
        not rows
        or score["status"] != "COMPLETE_SELECTED_COHORT"
        or outcomes["reconciliation"] != "CLOSED_CASH_RECONCILED"
    ):
        return {**result, "reason": "COMPLETE_RECONCILED_SELECTION_REQUIRED"}
    identities = {r.get("economic_policy_sha256") for r in rows}
    if len(identities) != 1 or None in identities or "" in identities:
        return {**result, "reason": "SINGLE_KNOWN_ECONOMIC_POLICY_REQUIRED"}
    if len({r["campaign_id"] for r in rows}) != len(rows):
        raise ValueError("duplicate selection")
    if any(type(r.get("decision_ns")) is not int or r["decision_ns"] < 0 for r in rows):
        raise ValueError("selection decision clock unavailable")
    if training_window is not None:
        start, end = training_window
        if any(type(v) is not int for v in (start, end)) or not 0 < start < end:
            raise ValueError("invalid selection training interval")
        result["training_window"] = dict(start_ns=start, end_ns=end)
        if type(accounting_as_of_ns) is not int or accounting_as_of_ns < 0:
            raise ValueError("selection training requires accounting knowledge cutoff")
        if accounting_as_of_ns >= end:
            return {**result, "reason": "TRAINING_ACCOUNTING_NOT_AVAILABLE_AT_CUTOFF"}
        if any(not start <= r["decision_ns"] < end for r in rows):
            return {**result, "reason": "SOURCE_COHORT_OUTSIDE_TRAINING_WINDOW"}
    capital = Decimal(score["initial_capital"])
    if not capital.is_finite() or capital <= 0:
        raise ValueError("invalid selection capital")
    cash = []
    for row in sorted(rows, key=lambda r: (r["decision_ns"], r["campaign_id"])):
        if row["status"] == "CLOSED_UNDER_NATIVE_MODEL":
            value = Decimal(row["native_net_pnl"])
        elif row["status"] == "TERMINAL_WITHOUT_ENTRY":
            value = Decimal(0)  # Verified non-submission produced no campaign cash flow.
        else:
            return {**result, "reason": "UNRESOLVED_SELECTION"}
        if not value.is_finite():
            raise ValueError("invalid selection cash")
        cash.append(value)
    total = sum(cash, Decimal(0))
    if total != Decimal(outcomes["native_cash_change"]) or total / capital / len(rows) != Decimal(
        score["mean_cash_return_per_selection"]
    ):
        raise ValueError("selection cash does not reconcile")
    values = np.asarray([float(v / capital) for v in cash])
    if not np.isfinite(values).all():
        raise ValueError("selection cash conversion overflow")
    if len(rows) <= block_size:
        return {**result, "reason": "INSUFFICIENT_SAMPLES_FOR_FROZEN_BLOCK"}
    bootstrap = importlib.import_module("arch.bootstrap")
    sampler = bootstrap.CircularBlockBootstrap(block_size, values, seed=seed)
    means = np.asarray([args[0].mean() for args, _ in sampler.bootstrap(reps)])
    se = float(means.std(ddof=1))
    if not np.isfinite(se):
        raise ValueError("nonfinite selection uncertainty")
    return {
        **result,
        "reason": "METHOD_AND_OOS_QUALIFICATION_REQUIRED",
        "economic_policy_sha256": next(iter(identities)),
        "sample_count": len(rows),
        "block_size": block_size,
        "reps": reps,
        "seed": seed,
        "method": "CIRCULAR_BLOCK_MEAN_BY_SELECTION_DECISION_ORDER",
        "library_version": importlib.metadata.version("arch"),
        "estimate": {"mean": float(values.mean()), "mean_standard_error": se},
        "limitations": [
            "native_model_not_measured_execution",
            "event_order_not_calendar_blocks",
            "capital_and_sizing_dependent_not_R",
            "no_search_or_holdout_qualification",
        ],
    }
