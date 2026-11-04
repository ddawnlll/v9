"""Multiple testing adjustments (Bonferroni, Holm, FDR) using statsmodels and scipy.

No economic claim or promotion authority is granted by this diagnostic.
"""

import importlib
import importlib.metadata
from typing import Any, Sequence

import numpy as np

SUPPORTED_METHODS = ("bonferroni", "holm", "fdr_bh", "fdr_by")


def multiple_testing_correction(
    p_values: dict[str, float] | Sequence[float],
    *,
    alpha: float = 0.05,
    methods: tuple[str, ...] = SUPPORTED_METHODS,
) -> dict[str, Any]:
    """Adjust p-values for multiple comparisons across a family of trials.

    Uses statsmodels.stats.multitest.multipletests and cross-validates FDR with
    scipy.stats.false_discovery_control.
    """
    if not 0 < alpha < 1:
        raise ValueError("alpha must be strictly between 0 and 1")
    if not methods:
        raise ValueError("at least one adjustment method must be specified")

    for method in methods:
        if method not in SUPPORTED_METHODS:
            raise ValueError(f"unsupported method '{method}', choose from {SUPPORTED_METHODS}")

    if isinstance(p_values, dict):
        if not p_values:
            raise ValueError("empty p_values dictionary")
        names = list(p_values.keys())
        p_array = np.array([float(p_values[k]) for k in names], dtype=float)
    else:
        if len(p_values) == 0:
            raise ValueError("empty p_values sequence")
        names = [f"trial_{i}" for i in range(len(p_values))]
        p_array = np.array([float(p) for p in p_values], dtype=float)

    if not np.isfinite(p_array).all() or np.any(p_array < 0.0) or np.any(p_array > 1.0):
        raise ValueError("all p-values must be finite and within [0, 1]")

    multitest = importlib.import_module("statsmodels.stats.multitest")
    stats = importlib.import_module("scipy.stats")

    results_by_method: dict[str, Any] = {}

    for method in methods:
        reject, p_adj, alphac_sidak, alphac_bonf = multitest.multipletests(
            p_array,
            alpha=alpha,
            method=method,
            is_sorted=False,
            returnsorted=False,
        )

        p_adj_dict = {names[i]: float(p_adj[i]) for i in range(len(names))}
        reject_dict = {names[i]: bool(reject[i]) for i in range(len(names))}

        results_by_method[method] = {
            "adjusted_pvalues": p_adj_dict,
            "rejected": reject_dict,
            "alphac_bonf": float(alphac_bonf),
            "alphac_sidak": float(alphac_sidak),
        }

    # Cross-check FDR methods with scipy.stats.false_discovery_control
    scipy_fdr: dict[str, dict[str, float]] = {}
    if hasattr(stats, "false_discovery_control"):
        if "fdr_bh" in methods:
            bh_scipy = stats.false_discovery_control(p_array, method="bh")
            scipy_fdr["bh"] = {names[i]: float(bh_scipy[i]) for i in range(len(names))}
        if "fdr_by" in methods:
            by_scipy = stats.false_discovery_control(p_array, method="by")
            scipy_fdr["by"] = {names[i]: float(by_scipy[i]) for i in range(len(names))}

    return {
        "method": "STATSMODELS_MULTIPLETESTS_AND_SCIPY_FDR",
        "family_size": len(names),
        "alpha": alpha,
        "variants": names,
        "raw_pvalues": {names[i]: float(p_array[i]) for i in range(len(names))},
        "adjustments": results_by_method,
        "scipy_cross_check": scipy_fdr,
        "dependency_versions": {
            name: importlib.metadata.version(name)
            for name in ("numpy", "scipy", "statsmodels")
        },
        "claim_status": "NO_ECONOMIC_CLAIM",
        "promotion_eligible": False,
    }
