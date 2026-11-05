"""Economic comparison refuses differently sized or incomplete replays."""

from dataclasses import replace
from decimal import Decimal

from v8_next.evaluation.accounting_contract import EconomicContract, compare_economic_contracts


def _contract() -> EconomicContract:
    return EconomicContract(
        tape_sha256="abc", window="[start,end)", instrument="BTCUSDT-PERP",
        initial_balance=Decimal("10000"), sizing_policy="target-notional:1000",
        execution_profile="bar-model-v1", maker_fee=Decimal("0.0002"),
        taker_fee=Decimal("0.0005"), funding_model="settlements-v1",
        mark_model="asof-close", capital_and_exposure_basis="single-net-position",
        outcome_basis="native-balance-delta", cutoff_policy="open-position-retained",
    )


def test_equal_declared_assumptions_are_not_a_claim_of_accounting_validation() -> None:
    result = compare_economic_contracts(_contract(), _contract())
    assert result.status == "COMPARABLE_ASSUMPTIONS"
    assert result.missing == result.mismatched == ()


def test_historical_sizing_and_outcome_mismatch_blocks_comparison() -> None:
    old = replace(_contract(), sizing_policy="fixed-quantity:0.01 BTC")
    marked = replace(_contract(), outcome_basis="marked-equity-delta")
    assert compare_economic_contracts(old, _contract()).mismatched == ("sizing_policy",)
    assert compare_economic_contracts(_contract(), marked).mismatched == ("outcome_basis",)
    assert compare_economic_contracts(old, _contract()).status == "BLOCKED"


def test_unknown_on_both_sides_still_blocks_and_reports_all_mismatches() -> None:
    left = replace(_contract(), funding_model=None, initial_balance=Decimal("100"))
    right = replace(_contract(), funding_model=None, initial_balance=Decimal("10000"))
    result = compare_economic_contracts(left, right)
    assert result.status == "BLOCKED"
    assert result.missing == ("funding_model",)
    assert result.mismatched == ("initial_balance",)


def test_funding_snapshot_refuses_multiple_netting_campaigns() -> None:
    from v8_next.evaluation.sim_arm import _funding_total

    # Mechanical fixture: campaign 1 received -1, then a second campaign reused
    # the position ID; final cache now contains only campaign 2, with no funding.
    final_cache = [{"adjustments": []}]
    assert _funding_total(final_cache, campaign_openings=2) == (None, 0)
    assert _funding_total([], campaign_openings=1) == (None, 0)
    assert _funding_total(final_cache, campaign_openings=0) == (None, 0)
    assert _funding_total(final_cache, campaign_openings=1) == (0.0, 0)
    malformed = [{"adjustments": [{"adjustment_type": "FUNDING", "pnl_change": None}]}]
    assert _funding_total(malformed, campaign_openings=1) == (None, 1)
