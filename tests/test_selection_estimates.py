from copy import deepcopy

import pytest

from v8_next.evaluation.selection_estimates import estimate_selection_cash


def test_selection_estimate_keeps_nonentries_in_denominator_and_rejects_missing_cash():
    pytest.importorskip("arch", reason="arch not installed (research extra)")
    source = dict(
        reconciliation="CLOSED_CASH_RECONCILED",
        native_cash_change="6",
        selection_cash_scorecard=dict(
            status="COMPLETE_SELECTED_COHORT",
            initial_capital="100",
            mean_cash_return_per_selection="0.015",
        ),
        rows=[
            dict(
                campaign_id=str(i),
                decision_ns=i,
                economic_policy_sha256="a" * 64,
                status="TERMINAL_WITHOUT_ENTRY" if i == 0 else "CLOSED_UNDER_NATIVE_MODEL",
                native_net_pnl=None if i == 0 else str(i),
            )
            for i in range(4)
        ],
    )
    plan = dict(block_size=2, reps=99, seed=7)
    result = estimate_selection_cash(source, **plan)
    assert result == estimate_selection_cash(source, **plan)
    assert result["sample_count"] == 4
    assert result["estimate"]["mean"] == pytest.approx(0.015)
    assert result["estimate"]["mean_standard_error"] > 0
    assert result["eligible_for_utility"] is False
    missing = deepcopy(source)
    missing["rows"][0]["status"] = "NO_CLOSED_NATIVE_OUTCOME"
    assert estimate_selection_cash(missing, **plan)["estimate"] is None
    bad = deepcopy(source)
    bad["native_cash_change"] = "7"
    with pytest.raises(ValueError, match="reconcile"):
        estimate_selection_cash(bad, **plan)
    mixed = deepcopy(source)
    mixed["rows"][0]["economic_policy_sha256"] = "b" * 64
    assert estimate_selection_cash(mixed, **plan)["estimate"] is None


def test_single_completed_selection_preserves_report_without_bootstrap():
    source = dict(
        reconciliation="CLOSED_CASH_RECONCILED",
        native_cash_change="1",
        selection_cash_scorecard=dict(
            status="COMPLETE_SELECTED_COHORT",
            initial_capital="100",
            mean_cash_return_per_selection="0.01",
        ),
        rows=[
            dict(
                campaign_id="one",
                decision_ns=1,
                economic_policy_sha256="a" * 64,
                status="CLOSED_UNDER_NATIVE_MODEL",
                native_net_pnl="1",
            )
        ],
    )
    result = estimate_selection_cash(source, block_size=2, reps=99, seed=7)
    assert result["estimate"] is None
    assert result["reason"] == "INSUFFICIENT_SAMPLES_FOR_FROZEN_BLOCK"
    assert result["block_size"] == 2 and result["sample_count"] == 1
    source["native_cash_change"] = "2"
    with pytest.raises(ValueError, match="reconcile"):
        estimate_selection_cash(source, block_size=2, reps=99, seed=7)


def test_training_selection_requires_accounting_known_before_end():
    pytest.importorskip("arch", reason="arch not installed (research extra)")
    source = dict(
        reconciliation="CLOSED_CASH_RECONCILED",
        native_cash_change="2",
        selection_cash_scorecard=dict(
            status="COMPLETE_SELECTED_COHORT",
            initial_capital="100",
            mean_cash_return_per_selection="0.01",
        ),
        rows=[
            dict(
                campaign_id=str(i),
                decision_ns=i + 1,
                economic_policy_sha256="a" * 64,
                status="CLOSED_UNDER_NATIVE_MODEL",
                native_net_pnl="1",
            )
            for i in range(2)
        ],
    )
    plan = dict(block_size=1, reps=99, seed=7, training_window=(1, 10))
    assert estimate_selection_cash(source, **plan, accounting_as_of_ns=9)["estimate"] is not None
    assert (
        estimate_selection_cash(source, **plan, accounting_as_of_ns=10)["reason"]
        == "TRAINING_ACCOUNTING_NOT_AVAILABLE_AT_CUTOFF"
    )
    with pytest.raises(ValueError, match="knowledge cutoff"):
        estimate_selection_cash(source, **plan)
    source["rows"][0]["decision_ns"] = 0
    assert (
        estimate_selection_cash(source, **plan, accounting_as_of_ns=9)["reason"]
        == "SOURCE_COHORT_OUTSIDE_TRAINING_WINDOW"
    )
