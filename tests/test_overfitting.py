from dataclasses import replace
from decimal import Decimal

import pytest

from v8_next.evaluation.alignment import IntervalLoss
from v8_next.evaluation.overfitting import pbo_diagnostic


def losses(returns):
    return tuple(IntervalLoss(i, i + 1, i + 1, Decimal(-r)) for i, r in enumerate(returns))


def evaluate(a, b, *, partitions=2, metric="mean_return"):
    return pbo_diagnostic(
        {"a": losses(a), "b": losses(b)},
        registered_variants=("a", "b"),
        frozen_ns=0,
        evaluation_end_ns=len(a),
        decision_ns=len(a) + 1,
        partitions=partitions,
        metric=metric,
        max_splits=100,
    )


@pytest.mark.parametrize("metric", ["mean_return", "sharpe"])
def test_persistent_winner_and_regime_reversal_have_known_ranks(metric):
    pytest.importorskip("scipy")
    stable = evaluate([4, 3] * 4, [1, 2] * 4, metric=metric)
    assert stable["pbo"] == 0 and stable["split_count"] == 2
    a = [4, 3] * 2 + [-2, -1] * 2
    reversed_regime = evaluate(a, [-v for v in a], metric=metric)
    assert reversed_regime["pbo"] == 1
    all_splits = evaluate(a, [-v for v in a], partitions=4, metric=metric)
    assert all_splits["split_count"] == 6 and all_splits["pbo"] == 1 / 3
    assert all_splits == evaluate(a, [-v for v in a], partitions=4, metric=metric)
    assert not all_splits["promotion_eligible"]


def test_training_ties_share_weight_and_oos_median_ties_are_explicit():
    pytest.importorskip("scipy")
    result = evaluate([1, 3, 3, 5], [0, 4, -1, 1])
    assert result["splits"][0]["selected_variants"] == ["a", "b"]
    assert result["splits"][0]["overfit_weight"] == 0.5
    assert result["splits"][1]["logits"] == [0]
    assert result["pbo"] == 0.75


def test_missing_family_and_data_are_not_dropped():
    a, b = losses([1, 2, 3, 4]), losses([4, 3, 2, 1])
    plan = dict(
        registered_variants=("a", "b"),
        frozen_ns=0,
        evaluation_end_ns=4,
        decision_ns=5,
        partitions=2,
        metric="mean_return",
        max_splits=2,
    )
    with pytest.raises(ValueError, match="complete registered"):
        pbo_diagnostic({"a": a}, **plan)
    with pytest.raises(ValueError, match="missing"):
        pbo_diagnostic({"a": a, "b": (replace(b[0], loss=None), *b[1:])}, **plan)
    with pytest.raises(ValueError, match="identical"):
        pbo_diagnostic({"a": a, "b": a}, **plan)
    with pytest.raises(ValueError, match="budget"):
        pbo_diagnostic({"a": a, "b": b}, **{**plan, "max_splits": 1})
    with pytest.raises(ValueError, match="equal-sized"):
        pbo_diagnostic({"a": a, "b": b}, **{**plan, "partitions": 3})
    pytest.importorskip("scipy")
    with pytest.raises(ValueError, match="undefined Sharpe"):
        evaluate([1] * 4, [2] * 4, metric="sharpe")


def test_cpcv_purging_and_embargo():
    pytest.importorskip("scipy")
    pytest.importorskip("polars")
    a = losses([1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12])
    b = losses([12, 11, 10, 9, 8, 7, 6, 5, 4, 3, 2, 1])
    plan = dict(
        registered_variants=("a", "b"),
        frozen_ns=0,
        evaluation_end_ns=12,
        decision_ns=13,
        partitions=4,
        metric="mean_return",
        max_splits=10,
        purge_bars=1,
        embargo_bars=1,
    )
    result = pbo_diagnostic({"a": a, "b": b}, **plan)  # type: ignore[arg-type]
    assert result["method"] == "CPCV_PURGED_EQUAL_WEIGHT_TIES_V2"
    assert result["purge_bars"] == 1
    assert result["embargo_bars"] == 1
    assert "no_purging_of_overlapping_trade_labels" not in result["limitations"]
    assert "polars" in result["dependency_versions"]
    assert 0 <= result["pbo"] <= 1

    # Negative purge_bars or excessive purge + embargo error
    with pytest.raises(ValueError, match="non-negative"):
        pbo_diagnostic({"a": a, "b": b}, **{**plan, "purge_bars": -1})  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="less than partition block size"):
        pbo_diagnostic({"a": a, "b": b}, **{**plan, "purge_bars": 2, "embargo_bars": 1})  # type: ignore[arg-type]

