from copy import deepcopy

import pytest

from v8_next.evaluation.component_estimates import estimate_components


def sample():
    return dict(
        reconciliation="CLOSED_CASH_RECONCILED",
        rows=[
            dict(
                campaign_id=str(i),
                economic_policy_sha256="a" * 64,
                instrument_id="BTCUSDT-PERP.BINANCE",
                direction="LONG",
                status="CLOSED_UNDER_NATIVE_MODEL",
                decision_ns=i + 1,
                opened_ns=i + 2,
                closed_ns=i + 10,
                observed_ns=20,
                entry_notional="1",
                native_price_pnl=str(i),
                observed_commissions="1",
                observed_funding_pnl="0",
                observed_other_adjustment_pnl="0",
                native_net_pnl=str(i - 1),
                price_return_on_entry_notional=str(i),
                commission_fraction="1",
                funding_return_on_entry_notional="0",
                other_adjustment_return_on_entry_notional="0",
                net_return_on_entry_notional=str(i - 1),
            )
            for i in range(4)
        ],
    )


def test_joint_components_preserve_cost_relation_and_deterministic_uncertainty():
    pytest.importorskip("arch", reason="arch not installed (research extra)")
    a = estimate_components(sample(), decision_ns=30, block_size=2, reps=99, seed=7)
    assert a == estimate_components(sample(), decision_ns=30, block_size=2, reps=99, seed=7)
    estimates = a["estimates"]
    assert estimates["price_return_on_entry_notional"]["mean"] == 1.5
    assert estimates["net_return_on_entry_notional"]["mean"] == 0.5
    assert estimates["commission_fraction"]["mean_standard_error"] == 0
    assert (
        estimates["price_return_on_entry_notional"]["mean_standard_error"]
        == estimates["net_return_on_entry_notional"]["mean_standard_error"]
    )
    assert a["eligible_for_utility"] is False


def test_incomplete_sample_and_unreconciled_fractions_cannot_supply_estimate():
    data = sample()
    data["rows"][0]["status"] = "NO_CLOSED_NATIVE_OUTCOME"
    assert (
        estimate_components(data, decision_ns=30, block_size=2, reps=99, seed=7)["estimates"]
        is None
    )
    data = deepcopy(sample())
    data["rows"][0]["net_return_on_entry_notional"] = "999"
    with pytest.raises(ValueError, match="fractions"):
        estimate_components(data, decision_ns=30, block_size=2, reps=99, seed=7)
    with pytest.raises(ValueError, match="future"):
        estimate_components(sample(), decision_ns=20, block_size=2, reps=99, seed=7)


@pytest.mark.parametrize(
    "change",
    [{"instrument_id": "ETHUSDT-PERP.BINANCE"}, {"direction": "SHORT"}, {"direction": None}],
)
def test_component_estimates_do_not_pool_unlike_or_unknown_cohorts(change):
    data = sample()
    data["rows"][0].update(change)
    report = estimate_components(data, decision_ns=30, block_size=2, reps=99, seed=7)
    assert report["estimates"] is None
    assert report["reason"] in {
        "EXPLICIT_COHORT_CONDITIONING_REQUIRED",
        "COHORT_IDENTITY_UNAVAILABLE",
    }


def test_realized_r_uses_filled_risk_and_keeps_incomplete_cohort_absent():
    pytest.importorskip("arch", reason="arch not installed (research extra)")
    data = sample()
    for row in data["rows"]:
        row.update(initial_filled_stop_risk="0.5", net_r=str(2 * int(row["native_net_pnl"])))
    report = estimate_components(data, decision_ns=30, block_size=2, reps=99, seed=7)
    r = report["estimates"]["net_r"]
    nominal = report["estimates"]["net_return_on_entry_notional"]
    assert r["mean"] == 2 * nominal["mean"]
    assert r["mean_standard_error"] == 2 * nominal["mean_standard_error"]
    assert report["eligible_for_utility"] is False
    data["rows"][0]["net_r"] = "999"
    with pytest.raises(ValueError, match="realized R"):
        estimate_components(data, decision_ns=30, block_size=2, reps=99, seed=7)
    data["rows"][0]["net_r"] = None
    report = estimate_components(data, decision_ns=30, block_size=2, reps=99, seed=7)
    assert "net_r" not in report["estimates"]
    assert report["sample_count"] == 4
    assert report["r_estimation_status"] == "COMPLETE_PROTECTED_COHORT_REQUIRED"


@pytest.mark.parametrize("identity", [None, "b" * 64])
def test_different_or_unavailable_economic_policies_are_not_pooled(identity):
    data = sample()
    data["rows"][0]["economic_policy_sha256"] = identity
    report = estimate_components(data, decision_ns=30, block_size=2, reps=99, seed=7)
    assert report["estimates"] is None
    assert report["reason"] in {
        "ECONOMIC_POLICY_IDENTITY_UNAVAILABLE",
        "EXPLICIT_POLICY_CONDITIONING_REQUIRED",
    }


def test_training_window_rejects_late_labels_and_outside_selections_without_slicing():
    pytest.importorskip("arch", reason="arch not installed (research extra)")
    data = sample()
    kwargs = dict(decision_ns=30, block_size=2, reps=99, seed=7)
    complete = estimate_components(data, **kwargs, training_window=(1, 21))
    assert complete["sample_count"] == 4
    assert complete["training_window"]["end_ns"] == 21
    late = estimate_components(data, **kwargs, training_window=(1, 20))
    assert late["estimates"] is None
    assert late["reason"] == "TRAINING_OUTCOMES_NOT_AVAILABLE_AT_CUTOFF"
    outside = estimate_components(data, **kwargs, training_window=(2, 21))
    assert outside["estimates"] is None
    assert outside["reason"] == "SOURCE_COHORT_OUTSIDE_TRAINING_WINDOW"
    with pytest.raises(ValueError, match="training window"):
        estimate_components(data, **kwargs, training_window=(1, 31))
    del data["rows"][0]["decision_ns"]
    assert (
        estimate_components(data, **kwargs, training_window=(1, 21))["reason"]
        == "CAMPAIGN_SELECTION_CLOCK_UNAVAILABLE"
    )


def test_small_cohort_keeps_frozen_plan_without_manufacturing_uncertainty():
    result = estimate_components(sample(), decision_ns=30, block_size=4, reps=99, seed=7)
    assert result["estimates"] is None
    assert result["reason"] == "INSUFFICIENT_SAMPLES_FOR_FROZEN_BLOCK"
    assert result["sample_count"] == result["block_size"] == 4
    corrupt = sample()
    corrupt["rows"][0]["observed_ns"] = 31
    with pytest.raises(ValueError, match="future"):
        estimate_components(corrupt, decision_ns=30, block_size=4, reps=99, seed=7)
    with pytest.raises(ValueError, match="resampling plan"):
        estimate_components(sample(), decision_ns=30, block_size=True, reps=99, seed=7)
