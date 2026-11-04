import json
from dataclasses import replace
from decimal import Decimal

import numpy as np
import pytest

from v8_next.evaluation.alignment import IntervalLoss
from v8_next.evaluation.reality_check import reality_check_diagnostic


def series(values):
    return tuple(IntervalLoss(i, i + 1, i + 1, Decimal(v)) for i, v in enumerate(values))


def test_compound_null_joint_draws_and_inclusive_ties(monkeypatch):
    bootstrap = pytest.importorskip("arch.bootstrap")

    class Draws:
        def __init__(self, block, matrix, seed):
            assert block == 1 and seed == 7
            np.testing.assert_array_equal(matrix, [[2, 0], [0, 2], [2, 0], [0, 2]])
            self.matrix = matrix

        def bootstrap(self, reps):
            assert reps == 3
            for indices in ([0, 0, 0, 0], [0, 1, 2, 3], [3, 3, 3, 3]):
                yield (self.matrix[indices],), {}

    monkeypatch.setattr(bootstrap, "CircularBlockBootstrap", Draws)
    result = reality_check_diagnostic(
        series([0] * 4),
        {"b": series([0, -2, 0, -2]), "a": series([-2, 0, -2, 0])},
        frozen_ns=0,
        evaluation_end_ns=4,
        decision_ns=5,
        block_size=1,
        reps=3,
        seed=7,
    )
    # Both all-one-column draws have centered maximum exactly equal to the
    # observed maximum; strict > would incorrectly report zero exceedances.
    assert result["observed_max"] == 1
    assert result["exceedances"] == 2 and result["p_value"] == 3 / 4
    assert result["reporting_version"] == "PLUS_ONE_V1"
    assert result["argmax_variant"] == "a"
    assert result["effect_estimates"] == {
        name: {"mean_baseline_minus_variant_loss": 1.0, "bootstrap_mean_standard_error": 1.0}
        for name in ("a", "b")
    }
    assert not result["promotion_eligible"]


def test_real_library_repeatability_clone_invariance_and_bad_alignment():
    pytest.importorskip("arch")
    baseline = series([i % 3 for i in range(30)])
    candidate = series([i % 5 for i in range(30)])
    plan = dict(frozen_ns=0, evaluation_end_ns=30, decision_ns=31, block_size=3, reps=199, seed=12)
    result = reality_check_diagnostic(baseline, {"a": candidate}, **plan)
    assert result == reality_check_diagnostic(baseline, {"a": candidate}, **plan)
    legacy = reality_check_diagnostic(
        baseline, {"a": candidate}, **plan, reporting_version="LEGACY_EXCEED_OVER_REPS_V1"
    )
    assert legacy["exceedances"] == result["exceedances"]
    assert legacy["p_value"] == legacy["exceedances"] / legacy["reps"]
    assert json.loads(json.dumps(result)) == result
    assert result["method"] != legacy["method"]
    cloned = reality_check_diagnostic(baseline, {"b": candidate, "a": candidate}, **plan)
    assert result["p_value"] == cloned["p_value"]
    assert result["effect_estimates"]["a"] == pytest.approx(cloned["effect_estimates"]["b"])
    expected_mean = np.mean(
        [float(b.loss - c.loss) for b, c in zip(baseline, candidate, strict=True)]
    )
    assert result["effect_estimates"]["a"]["mean_baseline_minus_variant_loss"] == pytest.approx(
        expected_mean
    )
    assert 0 <= result["p_value"] <= 1
    incomplete = (replace(candidate[0], loss=None), *candidate[1:])
    with pytest.raises(ValueError, match="missing"):
        reality_check_diagnostic(baseline, {"a": incomplete}, **plan)
    with pytest.raises(ValueError, match="degenerate"):
        reality_check_diagnostic(baseline, {"a": baseline}, **plan)
    with pytest.raises(ValueError, match="bootstrap"):
        reality_check_diagnostic(baseline, {"a": candidate}, **{**plan, "block_size": 30})


def test_stationary_bootstrap_and_arch_reality_check():
    pytest.importorskip("arch")
    baseline = series([i % 3 for i in range(30)])
    candidate = series([i % 5 for i in range(30)])
    plan = dict(
        frozen_ns=0,
        evaluation_end_ns=30,
        decision_ns=31,
        block_size=3,
        reps=199,
        seed=12,
        bootstrap="stationary",
    )
    result = reality_check_diagnostic(baseline, {"a": candidate}, **plan)
    assert result["bootstrap"] == "stationary_geometric_block_joint_columns"
    assert result["library"] == "arch.bootstrap.StationaryBootstrap"
    assert 0 <= result["p_value"] <= 1
    assert result["arch_reality_check"] is not None
    assert result["arch_reality_check"]["method"] == "arch.bootstrap.RealityCheck"
    assert "upper_wrc_pvalue" in result["arch_reality_check"]
    assert 0 <= result["arch_reality_check"]["upper_wrc_pvalue"] <= 1

