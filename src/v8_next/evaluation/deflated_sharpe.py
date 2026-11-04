"""Deflated Sharpe confidence under an explicit trial-independence assumption."""

import importlib
import importlib.metadata
from dataclasses import dataclass
from math import e, sqrt
from typing import Any

import numpy as np

from v8_next.evaluation.alignment import IntervalLoss, paired_differentials


@dataclass(frozen=True)
class DSRPlan:
    selected_variant: str
    registered_variants: tuple[str, ...]
    effective_independent_trials: float
    independence_basis: str


def deflated_sharpe_diagnostic(
    losses: dict[str, tuple[IntervalLoss, ...]],
    *,
    plan: DSRPlan,
    frozen_ns: int,
    evaluation_end_ns: int,
    decision_ns: int,
) -> dict[str, Any]:
    """Loss must be negative net excess return per equal-duration interval.

    Uses sample SD (ddof=1), central-moment skew/Pearson kurtosis (bias=True),
    and cross-trial sample Sharpe variance. The expected null maximum has zero
    population Sharpe. This confidence is not a p-value or proof of dependence.
    """
    names = sorted(losses)
    trials = plan.effective_independent_trials
    if (
        len(names) < 2
        or len(set(plan.registered_variants)) != len(plan.registered_variants)
        or set(names) != set(plan.registered_variants)
        or plan.selected_variant not in names
    ):
        raise ValueError("complete registered Sharpe family and selected variant required")
    if (
        not plan.independence_basis.strip()
        or not np.isfinite(trials)
        or not (trials == 1 or 2 <= trials <= len(names))
    ):
        raise ValueError("explicit supported independent-trial count and basis required")
    baseline = losses[names[0]]
    if len(baseline) < 4:
        raise ValueError("at least four complete return intervals required for moments")
    for name in names:
        paired_differentials(
            baseline,
            losses[name],
            frozen_ns=frozen_ns,
            evaluation_end_ns=evaluation_end_ns,
            decision_ns=decision_ns,
        )
    if len({r.end_ns - r.start_ns for r in baseline}) != 1:
        raise ValueError("equal-duration return intervals required")
    matrix = -np.asarray(
        [[float(r.loss) for r in losses[name] if r.loss is not None] for name in names]
    ).T
    if not np.isfinite(matrix).all():
        raise ValueError("nonfinite return matrix")
    if np.unique(matrix, axis=1).shape[1] != len(names):
        raise ValueError("identical performance columns need an explicit equivalence policy")
    sd = matrix.std(axis=0, ddof=1)
    if np.any(sd == 0):
        raise ValueError("undefined Sharpe in registered family")
    sharpes = matrix.mean(axis=0) / sd
    variance = float(sharpes.var(ddof=1))
    if not np.isfinite(sharpes).all() or not np.isfinite(variance) or variance <= 0:
        raise ValueError("unidentified cross-trial Sharpe dispersion")
    stats = importlib.import_module("scipy.stats")
    selected = matrix[:, names.index(plan.selected_variant)]
    sr = float(sharpes[names.index(plan.selected_variant)])
    skew = float(stats.skew(selected, bias=True))
    kurtosis = float(stats.kurtosis(selected, fisher=False, bias=True))
    expected_max = (
        0.0
        if trials == 1
        else sqrt(variance)
        * (
            (1 - np.euler_gamma) * stats.norm.isf(1 / trials)
            + np.euler_gamma * stats.norm.isf(1 / (trials * e))
        )
    )
    variance_term = 1 - skew * sr + (kurtosis - 1) * sr * sr / 4
    if not np.isfinite([expected_max, skew, kurtosis, variance_term]).all() or variance_term <= 0:
        raise ValueError("invalid Sharpe sampling variance")
    z = (sr - expected_max) * sqrt(len(baseline) - 1) / sqrt(variance_term)
    confidence = float(stats.norm.cdf(z))
    from v8_next.evaluation.multitest import multiple_testing_correction

    raw_pvalues = {}
    for i, name in enumerate(names):
        ttest_res = stats.ttest_1samp(matrix[:, i], 0.0, alternative="greater")
        raw_pvalues[name] = float(ttest_res.pvalue)

    multi_test_adjustments = multiple_testing_correction(raw_pvalues)

    return {
        "method": "BAILEY_LOPEZ_DE_PRADO_DSR_V1",
        "dsr_confidence": confidence,
        "quantity_type": "CONFIDENCE_NOT_P_VALUE",
        "selected_variant": plan.selected_variant,
        "sample_intervals": len(baseline),
        "selected_sharpe_nonannualized": sr,
        "cross_trial_sharpe_variance_ddof1": variance,
        "expected_max_null_sharpe": float(expected_max),
        "skewness": skew,
        "pearson_kurtosis": kurtosis,
        "registered_variants": names,
        "effective_independent_trials": trials,
        "independence_basis": plan.independence_basis,
        "input_measure": "negative_net_excess_period_return",
        "moment_bias_correction": False,
        "multiple_testing": multi_test_adjustments,
        "dependency_versions": {
            name: importlib.metadata.version(name)
            for name in ("numpy", "scipy", "statsmodels")
        },
        "claim_status": "NO_ECONOMIC_CLAIM",
        "promotion_eligible": False,
        "limitations": [
            "independence_basis_declared_not_verified",
            "no_serial_dependence_correction",
            "expected_maximum_is_an_approximation",
            "small_samples_may_be_underpowered",
            "source_and_search_lineage_not_certified",
        ],
    }
