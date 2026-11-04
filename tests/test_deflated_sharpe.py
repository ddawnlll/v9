from dataclasses import replace
from decimal import Decimal

import pytest

from v8_next.evaluation.alignment import IntervalLoss
from v8_next.evaluation.deflated_sharpe import DSRPlan, deflated_sharpe_diagnostic


def losses(values):
    return tuple(IntervalLoss(i, i + 1, i + 1, -Decimal(v)) for i, v in enumerate(values))


def family():
    return {
        "zero": losses([-1, 1] * 10),
        "positive": losses([0, 2] * 10),
        "negative": losses([-2, 0] * 10),
    }


def calculate(values, plan):
    return deflated_sharpe_diagnostic(
        values, plan=plan, frozen_ns=0, evaluation_end_ns=20, decision_ns=21
    )


def test_zero_sharpe_one_effective_trial_is_half_confidence_not_significance():
    pytest.importorskip("scipy")
    plan = DSRPlan("zero", tuple(family()), 1, "test-only one-effective-trial assumption")
    result = calculate(family(), plan)
    assert result["dsr_confidence"] == 0.5
    assert result["selected_sharpe_nonannualized"] == 0
    assert result["expected_max_null_sharpe"] == 0
    assert result["skewness"] == 0 and result["pearson_kurtosis"] == 1
    assert result["quantity_type"] == "CONFIDENCE_NOT_P_VALUE"
    assert not result["promotion_eligible"]
    assert "multiple_testing" in result
    assert set(result["multiple_testing"]["adjustments"]) == {"bonferroni", "holm", "fdr_bh", "fdr_by"}
    assert result["dependency_versions"]["statsmodels"] is not None
    adjusted = calculate(family(), replace(plan, effective_independent_trials=3))
    assert adjusted["expected_max_null_sharpe"] > 0
    assert adjusted["dsr_confidence"] < 0.5


def test_more_trials_raise_threshold_and_positive_scaling_does_not_change_dsr():
    pytest.importorskip("scipy")
    plan = DSRPlan("positive", tuple(family()), 2, "test-only declared assumption")
    two = calculate(family(), plan)
    three = calculate(family(), replace(plan, effective_independent_trials=3))
    assert three["expected_max_null_sharpe"] > two["expected_max_null_sharpe"]
    assert three["dsr_confidence"] < two["dsr_confidence"]
    scaled = {
        name: tuple(replace(r, loss=r.loss * 100) for r in rows) for name, rows in family().items()
    }
    assert calculate(scaled, plan)["dsr_confidence"] == pytest.approx(two["dsr_confidence"])
    assert two == calculate(family(), plan)


def test_incomplete_family_undefined_sharpe_and_unstated_independence_reject():
    plan = DSRPlan("positive", tuple(family()), 2, "test-only")
    with pytest.raises(ValueError, match="complete registered"):
        calculate({"positive": family()["positive"]}, plan)
    for invalid in (0, 1.5, 4, float("nan")):
        with pytest.raises(ValueError, match="independent-trial"):
            calculate(family(), replace(plan, effective_independent_trials=invalid))
    with pytest.raises(ValueError, match="basis"):
        calculate(family(), replace(plan, independence_basis=""))
    values = family()
    values["zero"] = losses([1] * 20)
    with pytest.raises(ValueError, match="undefined Sharpe"):
        calculate(values, plan)
    values["zero"] = values["positive"]
    with pytest.raises(ValueError, match="identical"):
        calculate(values, plan)
    values = family()
    values["zero"] = (replace(values["zero"][0], loss=None), *values["zero"][1:])
    with pytest.raises(ValueError, match="missing"):
        calculate(values, plan)
