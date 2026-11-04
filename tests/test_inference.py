from decimal import Decimal

import pytest

from v8_next.evaluation.alignment import IntervalLoss
from v8_next.evaluation.inference import spa_diagnostic


def test_spa_is_repeatable_and_never_promotes_test_fixture():
    pytest.importorskip("arch")
    baseline = tuple(IntervalLoss(i, i + 1, i + 1, Decimal(i % 3)) for i in range(30))
    variant = tuple(IntervalLoss(i, i + 1, i + 1, Decimal(i % 5)) for i in range(30))
    kwargs = dict(frozen_ns=0, evaluation_end_ns=30, decision_ns=31, block_size=3, reps=99, seed=7)
    result = spa_diagnostic(baseline, {"test-only": variant}, **kwargs)
    assert result == spa_diagnostic(baseline, {"test-only": variant}, **kwargs)
    cloned = spa_diagnostic(baseline, {"test-only": variant, "same-feed": variant}, **kwargs)
    assert cloned["pvalues"] == result["pvalues"]
    assert set(result["pvalues"]) == {"lower", "consistent", "upper"}
    assert all(0 <= v <= 1 for v in result["pvalues"].values())
    assert not result["promotion_eligible"]
    assert result["dsr"] is result["pbo"] is None
    assert result["wrc"]["bootstrap"] == "circular_fixed_block_joint_columns"
    assert cloned["wrc"]["p_value"] == result["wrc"]["p_value"]
    with pytest.raises(ValueError, match="degenerate"):
        spa_diagnostic(baseline, {"clone": baseline}, **kwargs)


def test_trajectory_spa_preserves_missingness_and_exploratory_scope():
    from v8_next.evaluation.inference import trajectory_spa_diagnostic

    rows = [
        {
            "start_ns": i,
            "end_ns": i + 1,
            "breakout_baseline": {"incremental_cash_return": str(i % 3)},
            "squeeze": {"incremental_cash_return": str(i % 5)},
        }
        for i in range(30)
    ]
    kwargs = dict(decision_ns=31, block_size=3, reps=99, seed=7)
    rows[0]["squeeze"]["incremental_cash_return"] = None
    missing = trajectory_spa_diagnostic({"rows": rows}, **kwargs)
    assert missing["status"] == "MISSING_CASH_INTERVAL_NO_IMPUTATION"
    assert missing["result"] is None
    rows[0]["squeeze"]["incremental_cash_return"] = "0"
    pytest.importorskip("arch")
    result = trajectory_spa_diagnostic({"rows": rows}, **kwargs)
    assert result["status"] == "COMPUTED_EXPLORATORY_NOT_QUALIFIED"
    assert result["preregistration"] == "NOT_VERIFIED_EXPLORATORY_ONLY"
    assert result["promotion_eligible"] is False
    for row in rows:
        row["squeeze"] = row["breakout_baseline"].copy()
    flat = trajectory_spa_diagnostic({"rows": rows}, **kwargs)
    assert flat["status"] == "DEGENERATE_DIFFERENTIAL_NO_PVALUE"
    assert flat["result"] is None


def test_explicit_cscv_plan_populates_family_diagnostic_without_promoting():
    from v8_next.evaluation.overfitting import CSCVPlan

    pytest.importorskip("arch")
    baseline = tuple(IntervalLoss(i, i + 1, i + 1, Decimal(0)) for i in range(12))
    variants = {
        "a": tuple(IntervalLoss(i, i + 1, i + 1, Decimal(-1 - i % 3)) for i in range(12)),
        "b": tuple(IntervalLoss(i, i + 1, i + 1, Decimal(-1 - i % 2)) for i in range(12)),
    }
    result = spa_diagnostic(
        baseline,
        variants,
        frozen_ns=0,
        evaluation_end_ns=12,
        decision_ns=13,
        block_size=2,
        reps=29,
        seed=2,
        pbo_plan=CSCVPlan(2, "mean_return", 2, ("a", "b")),
    )
    assert result["pbo"]["split_count"] == 2
    assert not result["pbo"]["promotion_eligible"]
    assert result["dsr"] is None


def test_explicit_dsr_plan_populates_confidence_field_separately():
    from v8_next.evaluation.deflated_sharpe import DSRPlan

    pytest.importorskip("arch")
    baseline = tuple(IntervalLoss(i, i + 1, i + 1, Decimal(0)) for i in range(20))
    variants = {
        "a": tuple(IntervalLoss(i, i + 1, i + 1, Decimal(-i % 3)) for i in range(20)),
        "b": tuple(IntervalLoss(i, i + 1, i + 1, Decimal(-1 - i % 2)) for i in range(20)),
    }
    result = spa_diagnostic(
        baseline,
        variants,
        frozen_ns=0,
        evaluation_end_ns=20,
        decision_ns=21,
        block_size=2,
        reps=29,
        seed=2,
        dsr_plan=DSRPlan("b", ("a", "b"), 2, "test-only independent trials"),
    )
    assert 0 <= result["dsr"]["dsr_confidence"] <= 1
    assert "p_value" not in result["dsr"]
    assert result["pbo"] is None
    assert "multiple_testing" in result
    assert set(result["multiple_testing"]["adjustments"]) == {"bonferroni", "holm", "fdr_bh", "fdr_by"}


def test_spa_diagnostic_stationary_bootstrap_and_cpcv_purging():
    from v8_next.evaluation.overfitting import CSCVPlan

    pytest.importorskip("arch")
    baseline = tuple(IntervalLoss(i, i + 1, i + 1, Decimal(0)) for i in range(24))
    variants = {
        "a": tuple(IntervalLoss(i, i + 1, i + 1, Decimal(-1 - i % 3)) for i in range(24)),
        "b": tuple(IntervalLoss(i, i + 1, i + 1, Decimal(-1 - i % 2)) for i in range(24)),
    }
    result = spa_diagnostic(
        baseline,
        variants,
        frozen_ns=0,
        evaluation_end_ns=24,
        decision_ns=25,
        block_size=2,
        reps=29,
        seed=3,
        pbo_plan=CSCVPlan(4, "mean_return", 10, ("a", "b"), purge_bars=1, embargo_bars=1),
        wrc_bootstrap="stationary",
    )
    assert result["wrc"]["bootstrap"] == "stationary_geometric_block_joint_columns"
    assert result["wrc"]["library"] == "arch.bootstrap.StationaryBootstrap"
    assert result["pbo"]["method"] == "CPCV_PURGED_EQUAL_WEIGHT_TIES_V2"
    assert result["pbo"]["purge_bars"] == 1
    assert result["pbo"]["embargo_bars"] == 1
    assert "multiple_testing" in result

