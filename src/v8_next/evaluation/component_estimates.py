"""Joint native outcome-component estimates; not a calibrated utility provider."""

import hashlib
import importlib
import importlib.metadata
from decimal import Decimal
from typing import Any

import numpy as np

from v8_next.evaluation.store import canonical

FIELDS = (
    "price_return_on_entry_notional",
    "commission_fraction",
    "funding_return_on_entry_notional",
    "other_adjustment_return_on_entry_notional",
)


def estimate_components(
    outcomes: dict[str, Any],
    *,
    decision_ns: int,
    block_size: int,
    reps: int,
    seed: int,
    training_window: tuple[int, int] | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = dict(
        scope="NATIVE_MODEL_COHORT_COMPONENTS_NOT_EXPECTED_UTILITY",
        claim_status="NO_ECONOMIC_CLAIM",
        eligible_for_utility=False,
        estimates=None,
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
    if training_window is not None:
        start, end = training_window
        if (
            any(type(t) is not int for t in (start, end, decision_ns))
            or not 0 < start < end <= decision_ns
        ):
            raise ValueError("invalid training window")
        result["training_window"] = dict(start_ns=start, end_ns=end, interval="[start,end)")
        if any(type(r.get("decision_ns")) is not int for r in rows):
            return {**result, "reason": "CAMPAIGN_SELECTION_CLOCK_UNAVAILABLE"}
        if any(not start <= r["decision_ns"] < end for r in rows):
            return {**result, "reason": "SOURCE_COHORT_OUTSIDE_TRAINING_WINDOW"}
        if any(type(r.get("observed_ns")) is not int or r["observed_ns"] >= end for r in rows):
            return {**result, "reason": "TRAINING_OUTCOMES_NOT_AVAILABLE_AT_CUTOFF"}

    if not rows or any(r["status"] != "CLOSED_UNDER_NATIVE_MODEL" for r in rows):
        return {**result, "reason": "COMPLETE_CLOSED_COHORT_REQUIRED"}
    if any(not r.get("instrument_id") or r.get("direction") not in {"LONG", "SHORT"} for r in rows):
        return {**result, "reason": "COHORT_IDENTITY_UNAVAILABLE"}
    policy_hashes = {r.get("economic_policy_sha256") for r in rows}
    if None in policy_hashes or "" in policy_hashes:
        return {**result, "reason": "ECONOMIC_POLICY_IDENTITY_UNAVAILABLE"}
    if len(policy_hashes) != 1:
        return {**result, "reason": "EXPLICIT_POLICY_CONDITIONING_REQUIRED"}
    result["economic_policy_sha256"] = next(iter(policy_hashes))
    identities = {(r["instrument_id"], r["direction"]) for r in rows}
    if len(identities) != 1:
        return {**result, "reason": "EXPLICIT_COHORT_CONDITIONING_REQUIRED"}
    instrument, direction = next(iter(identities))
    result["conditioning"] = dict(instrument_id=instrument, direction=direction)
    if outcomes["reconciliation"] != "CLOSED_CASH_RECONCILED":
        return {**result, "reason": "RECONCILED_COHORT_REQUIRED"}
    if any(any(r.get(k) is None for k in FIELDS) for r in rows):
        return {**result, "reason": "COMPONENTS_UNAVAILABLE"}
    if len({r["campaign_id"] for r in rows}) != len(rows):
        raise ValueError("duplicate component sample")
    ordered = sorted(rows, key=lambda r: (r["opened_ns"], r["closed_ns"], r["campaign_id"]))
    values = []
    r_complete = all(
        r.get("net_r") is not None and r.get("initial_filled_stop_risk") is not None for r in rows
    )
    result["r_estimation_status"] = (
        "COMPLETE_PROTECTED_COHORT" if r_complete else "COMPLETE_PROTECTED_COHORT_REQUIRED"
    )
    for row in ordered:
        if training_window is not None and not row["decision_ns"] < row["opened_ns"]:
            raise ValueError("campaign selection must precede entry")
        if not row["opened_ns"] <= row["closed_ns"] <= row["observed_ns"] < decision_ns:
            raise ValueError("component evidence reaches future")
        vector = [Decimal(row[k]) for k in FIELDS]
        net = Decimal(row["net_return_on_entry_notional"])
        if any(not v.is_finite() for v in (*vector, net)) or vector[1] < 0:
            raise ValueError("invalid component value")
        amounts = [
            Decimal(row[k])
            for k in (
                "native_price_pnl",
                "observed_commissions",
                "observed_funding_pnl",
                "observed_other_adjustment_pnl",
                "native_net_pnl",
            )
        ]
        notional = Decimal(row["entry_notional"])
        if not notional.is_finite() or notional <= 0 or any(not v.is_finite() for v in amounts):
            raise ValueError("invalid component cash amounts")
        if amounts[0] - amounts[1] + amounts[2] + amounts[3] != amounts[4]:
            raise ValueError("component cash arithmetic does not reconcile")
        if [v / notional for v in amounts] != [*vector, net]:
            raise ValueError("component fractions do not match native cash amounts")
        sample = [float(v) for v in (*vector, net)]
        if r_complete:
            risk = Decimal(row["initial_filled_stop_risk"])
            net_r = Decimal(row["net_r"])
            if not risk.is_finite() or risk <= 0 or not net_r.is_finite():
                raise ValueError("invalid realized R denominator or value")
            if amounts[4] / risk != net_r:
                raise ValueError("realized R does not match native cash and initial risk")
            sample.append(float(net_r))
        values.append(sample)
    matrix = np.asarray(values)
    if not np.isfinite(matrix).all():
        raise ValueError("component conversion overflow")
    if len(rows) <= block_size:
        return {**result, "reason": "INSUFFICIENT_SAMPLES_FOR_FROZEN_BLOCK"}
    bootstrap = importlib.import_module("arch.bootstrap")
    sampler = bootstrap.CircularBlockBootstrap(block_size, matrix, seed=seed)
    means = np.asarray([positional[0].mean(axis=0) for positional, _ in sampler.bootstrap(reps)])
    standard_errors = means.std(axis=0, ddof=1)
    if not np.isfinite(standard_errors).all():
        raise ValueError("nonfinite component uncertainty")
    names = (*FIELDS, "net_return_on_entry_notional", *(("net_r",) if r_complete else ()))
    return {
        **result,
        "reason": "METHOD_AND_OUT_OF_SAMPLE_QUALIFICATION_REQUIRED",
        "sample_count": len(rows),
        "block_size": block_size,
        "reps": reps,
        "seed": seed,
        "method": "JOINT_CIRCULAR_BLOCK_MEAN_BY_CAMPAIGN_ENTRY_ORDER",
        "library_version": importlib.metadata.version("arch"),
        "numpy_version": np.__version__,
        "weighting": "equal_weight_per_selected_closed_campaign",
        "estimates": {
            name: dict(mean=float(mean), mean_standard_error=float(se))
            for name, mean, se in zip(names, matrix.mean(axis=0), standard_errors, strict=True)
        },
        "limitations": [
            "execution_and_completion_conditioned_sample",
            "native_cost_model_not_measured_market_impact",
            "no_search_adjustment_or_holdout_certification",
            "event_order_blocks_not_calendar_time_blocks",
        ],
    }
