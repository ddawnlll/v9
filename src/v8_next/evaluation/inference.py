"""Optional arch SPA calculation; no certification or utility authorization."""

import importlib
import importlib.metadata
from typing import Any

import numpy as np

from v8_next.evaluation.alignment import IntervalLoss, paired_differentials
from v8_next.evaluation.deflated_sharpe import DSRPlan, deflated_sharpe_diagnostic
from v8_next.evaluation.overfitting import CSCVPlan, pbo_diagnostic
from v8_next.evaluation.reality_check import reality_check_diagnostic


def spa_diagnostic(
    baseline: tuple[IntervalLoss, ...],
    variants: dict[str, tuple[IntervalLoss, ...]],
    *,
    frozen_ns: int,
    evaluation_end_ns: int,
    decision_ns: int,
    block_size: int,
    reps: int,
    seed: int,
    pbo_plan: CSCVPlan | None = None,
    dsr_plan: DSRPlan | None = None,
    wrc_bootstrap: str = "circular",
) -> dict[str, Any]:
    """Stationary bootstrap, studentized SPA, all columns resampled jointly.

    Caller owns preregistration, complete search-family provenance, loss definition
    and source admissibility. This function cannot establish them from numbers.
    """
    if not variants or not 1 <= block_size < len(baseline) or reps < 2 or seed < 0:
        raise ValueError("explicit valid bootstrap plan and nonempty family required")
    names = sorted(variants)
    for name in names:
        paired_differentials(
            baseline,
            variants[name],
            frozen_ns=frozen_ns,
            evaluation_end_ns=evaluation_end_ns,
            decision_ns=decision_ns,
        )
    benchmark = np.array([float(r.loss) for r in baseline if r.loss is not None])
    models = np.array(
        [[float(r.loss) for r in variants[name] if r.loss is not None] for name in names]
    ).T
    differences = benchmark[:, None] - models
    if not np.isfinite(differences).all() or np.any(np.std(differences, axis=0) == 0):
        raise ValueError("nonfinite or degenerate loss differentials")
    arch = importlib.import_module("arch")
    bootstrap = importlib.import_module("arch.bootstrap")
    test = bootstrap.SPA(
        benchmark,
        models,
        block_size=block_size,
        reps=reps,
        bootstrap="stationary",
        studentize=True,
        nested=False,
        seed=seed,
    )
    test.compute()
    pvalues = {str(k): float(v) for k, v in test.pvalues.items()}
    if any(not np.isfinite(v) or not 0 <= v <= 1 for v in pvalues.values()):
        raise ValueError("invalid library inference output")

    stats = importlib.import_module("scipy.stats")
    from v8_next.evaluation.multitest import multiple_testing_correction

    raw_variant_pvalues = {}
    for i, name in enumerate(names):
        ttest_res = stats.ttest_1samp(differences[:, i], 0.0, alternative="greater")
        raw_variant_pvalues[name] = float(ttest_res.pvalue)
    multi_test = multiple_testing_correction(raw_variant_pvalues)

    return {
        "claim_status": "NO_ECONOMIC_CLAIM",
        "method": "arch.SPA",
        "library_version": arch.__version__,
        "dependency_versions": {
            name: importlib.metadata.version(name)
            for name in ("arch", "numpy", "scipy", "pandas", "statsmodels")
        },
        "sample_intervals": len(baseline),
        "null": "no_variant_has_positive_expected_baseline_minus_variant_loss",
        "evidence_scope": "NUMERICAL_DIAGNOSTIC_WITHOUT_SOURCE_OR_HOLDOUT_CERTIFICATION",
        "bootstrap": "stationary",
        "studentize": True,
        "nested": False,
        "block_size": block_size,
        "reps": reps,
        "seed": seed,
        "variants": names,
        "pvalues": pvalues,
        "multiple_testing": multi_test,
        "wrc": reality_check_diagnostic(
            baseline,
            variants,
            frozen_ns=frozen_ns,
            evaluation_end_ns=evaluation_end_ns,
            decision_ns=decision_ns,
            block_size=block_size,
            reps=reps,
            seed=seed,
            bootstrap=wrc_bootstrap,
        ),
        "dsr": deflated_sharpe_diagnostic(
            variants,
            plan=dsr_plan,
            frozen_ns=frozen_ns,
            evaluation_end_ns=evaluation_end_ns,
            decision_ns=decision_ns,
        )
        if dsr_plan is not None
        else None,
        "pbo": pbo_diagnostic(
            variants,
            registered_variants=pbo_plan.registered_variants,
            frozen_ns=frozen_ns,
            evaluation_end_ns=evaluation_end_ns,
            decision_ns=decision_ns,
            partitions=pbo_plan.partitions,
            metric=pbo_plan.metric,
            max_splits=pbo_plan.max_splits,
            purge_bars=pbo_plan.purge_bars,
            embargo_bars=pbo_plan.embargo_bars,
        )
        if pbo_plan is not None
        else None,
        "promotion_eligible": False,
    }


def trajectory_spa_diagnostic(
    trajectory: dict[str, Any], *, decision_ns: int, block_size: int, reps: int, seed: int
) -> dict[str, Any]:
    """Explicit exploratory computation, never qualification of the source series.

    Loss is negative incremental cash return per recorded capture interval.
    Irregular sampling and venue revisions remain methodological limitations.
    The caller-supplied plan is not presented as preregistered.
    """
    from decimal import Decimal

    empty: dict[str, Any] = {
        "status": "NO_PAIRED_INTERVALS",
        "result": None,
        "claim_status": "NO_ECONOMIC_CLAIM",
        "promotion_eligible": False,
        "preregistration": "NOT_VERIFIED_EXPLORATORY_ONLY",
        "loss": "negative_incremental_simulated_cash_return_per_capture_interval",
        "limitations": trajectory.get("limitations", []),
    }
    if block_size < 1 or reps < 2 or seed < 0:
        raise ValueError("explicit valid exploratory bootstrap parameters required")
    rows = trajectory["rows"]
    if not rows:
        return empty
    series: dict[str, list[IntervalLoss]] = {"breakout_baseline": [], "squeeze": []}
    for row in rows:
        for name in series:
            value = row[name]["incremental_cash_return"]
            if value is None:
                return {**empty, "status": "MISSING_CASH_INTERVAL_NO_IMPUTATION"}
            series[name].append(
                IntervalLoss(row["start_ns"], row["end_ns"], row["end_ns"], -Decimal(value))
            )
    baseline, variant = tuple(series["breakout_baseline"]), tuple(series["squeeze"])
    differences = paired_differentials(
        baseline,
        variant,
        frozen_ns=rows[0]["start_ns"],
        evaluation_end_ns=rows[-1]["end_ns"],
        decision_ns=decision_ns,
    )
    if len(set(differences)) < 2:
        return {**empty, "status": "DEGENERATE_DIFFERENTIAL_NO_PVALUE"}
    if block_size >= len(rows):
        return {**empty, "status": "INSUFFICIENT_INTERVALS_FOR_REQUESTED_BLOCK"}
    result = spa_diagnostic(
        baseline,
        {"squeeze": variant},
        frozen_ns=rows[0]["start_ns"],
        evaluation_end_ns=rows[-1]["end_ns"],
        decision_ns=decision_ns,
        block_size=block_size,
        reps=reps,
        seed=seed,
    )
    return {**empty, "status": "COMPUTED_EXPLORATORY_NOT_QUALIFIED", "result": result}
