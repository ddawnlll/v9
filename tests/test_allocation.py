from dataclasses import replace
from decimal import Decimal as D

import pytest

from v8_next.economics.allocation import AllocationProposal, allocate_ordered
from v8_next.economics.controller import InstrumentConstraints
from v8_next.economics.decisions import Opportunity, Stance, StanceKind, UtilityInputs
from v8_next.economics.protection import CampaignProtection
from v8_next.risk.admission import RiskLimits, RiskSnapshot


def proposal(identity, exposure="btc", verified=True):
    instrument = f"{exposure.upper()}USDT-PERP.BINANCE"
    return AllocationProposal(
        Opportunity(identity, exposure, instrument, "LONG", 1, 100),
        (Stance("observer", "group", StanceKind.SUPPORT, "setup", identity, 10),),
        UtilityInputs(D(10), D(1), D(1), D(1), D(1), D(1), "test-only"),
        verified,
        InstrumentConstraints(D("0.1"), D("0.1"), D(100), D(10)),
        D(100),
        D(600),
        CampaignProtection("pandf:a:v2", identity, instrument, "LONG", 10, 50, D(90), D(110)),
    )


def allocate(proposals, snapshots=None, allocated=frozenset()):
    return allocate_ordered(
        tuple(proposals),
        snapshots or {"btc": RiskSnapshot(D(1000), D(0), D(0), D(0), 10, True)},
        RiskLimits(D(1), D(1), D(1000), 10),
        decision_ns=10,
        already_allocated=allocated,
    )


def test_batch_cannot_spend_same_capital_twice():
    results = allocate([proposal("a"), proposal("b"), proposal("c")])
    assert [r.campaign.quantity if r.campaign else None for r in results] == [
        D("5.4"),
        D("3.6"),
        None,
    ]


def test_rejection_does_not_reserve_and_duplicate_cannot_allocate_twice():
    results = allocate([proposal("a", verified=False), proposal("a"), proposal("a"), proposal("b")])
    assert results[0].reason == "UNVERIFIED_CALIBRATION"
    assert results[1].campaign.quantity == D("5.4")
    assert results[2].reason == "DUPLICATE_OPPORTUNITY"
    assert results[3].campaign.quantity == D("3.6")
    assert allocate([proposal("a")], allocated=frozenset({"a"}))[0].campaign is None


def test_account_state_must_be_shared_across_exposures():
    snapshot = RiskSnapshot(D(1000), D(0), D(0), D(0), 10, True)
    proposals = [proposal("a"), proposal("b", "eth")]
    results = allocate(proposals, {"btc": snapshot, "eth": snapshot})
    assert sum(r.campaign.quantity * D(100) for r in results) == D(900)
    with pytest.raises(ValueError, match="global account state"):
        allocate(proposals, {"btc": snapshot, "eth": replace(snapshot, equity=D(2000))})
    with pytest.raises(ValueError, match="missing exposure"):
        allocate(proposals)


def test_lot_rounding_reserves_actual_admitted_quantity():
    first = replace(proposal("a"), requested_notional=D(605))
    results = allocate([first, proposal("b")])
    assert [r.campaign.quantity for r in results] == [D("5.5"), D("3.5")]


def test_batch_stop_heat_and_concurrency_reserve_each_accepted_campaign():
    from v8_next.risk.sizing import StopBudget, StopExposure

    def run(budget, exposure, proposals=None):
        return allocate_ordered(
            tuple(proposals or [proposal("a"), proposal("b"), proposal("c")]),
            {"btc": RiskSnapshot(D(1000), D(0), D(0), D(0), 10, True)},
            RiskLimits(D(1), D(1), D(1000), 10),
            decision_ns=10,
            already_allocated=frozenset(),
            stop_budget=budget,
            stop_exposure=exposure,
        )

    empty = StopExposure(D(0), 0, 10, True)
    results = run(StopBudget(D("0.01"), D("0.02"), 10), empty)
    # Distance-based sizing (budget/distance*price = 10/10*100): qty 1.0.
    # Worst-case band booking (1.0*20) exhausts the 0.02 heat budget at once.
    assert [r.campaign.quantity if r.campaign else None for r in results] == [
        D("1.0"),
        None,
        None,
    ]
    assert results[1].reason == "PORTFOLIO_HEAT_EXCEEDED"
    assert results[2].reason == "PORTFOLIO_HEAT_EXCEEDED"
    results = run(StopBudget(D("0.01"), D("0.1"), 1), empty)
    assert results[1].reason == "CAMPAIGN_CONCURRENCY_LIMIT"
    results = run(
        StopBudget(D("0.01"), D("0.1"), 1), empty, [proposal("a", verified=False), proposal("b")]
    )
    assert results[1].campaign.quantity == D("1.0")
    assert empty.open_and_reserved_risk == 0
    assert run(StopBudget(D("0.01"), D("0.1"), 1), None)[0].reason == "MISSING_STOP_RISK_INPUTS"
    assert run(None, empty)[0].reason == "MISSING_STOP_BUDGET_POLICY"
    assert (
        run(StopBudget(D("0.01"), D("0.1"), 1), replace(empty, as_of_ns=9))[0].reason
        == "STALE_OR_FUTURE_STOP_EXPOSURE"
    )


def test_distinct_exposure_budgets_share_global_reservations_only():
    snapshot = RiskSnapshot(D(1000), D(0), D(0), D(100), 10, True, D(100))
    eth = replace(snapshot, exposure_reserved_notional=D(0))
    results = allocate_ordered(
        (proposal("a"), proposal("b", "eth"), proposal("c")),
        {"btc": snapshot, "eth": eth},
        RiskLimits(D(1), D("0.5"), D(1000), 10),
        decision_ns=10,
        already_allocated=frozenset(),
    )
    assert [r.campaign.quantity if r.campaign else None for r in results] == [
        D("3.6"),
        D("4.5"),
        None,
    ]
    with pytest.raises(ValueError, match="exposure reservations exceed"):
        allocate([proposal("a")], {"btc": replace(snapshot, exposure_reserved_notional=D(101))})


def test_allocation_passes_decision_regime_to_campaign():
    from v8_next.economics.regime import RegimeObservation

    regime = RegimeObservation(
        "BTCUSDT-PERP.BINANCE", 10, "NormalVolume", "NeutralFunding", "1.0", "0.0"
    )
    p = replace(proposal("a"), decision_regime=regime)
    results = allocate([p])
    assert results[0].campaign is not None
    assert results[0].campaign.decision_regime == regime
