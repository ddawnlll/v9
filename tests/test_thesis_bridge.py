"""Deterministic bridge fixtures: no native engine and no economic claims."""

from dataclasses import FrozenInstanceError, replace
from decimal import Decimal as D

import pytest

from v8_next.domain.decision import DecisionAction, VenueOrderStatus
from v8_next.economics.thesis import (
    ExpertEvidence,
    InvalidationKind,
    InvalidationRule,
    PositionSnapshot,
    StanceKind,
    ThesisBelief,
    ThesisStatus,
)
from v8_next.evaluation.thesis_bridge import (
    CurrentEvidence,
    EntryEvidence,
    NativeOrderEvidence,
    capture_entry,
    evaluate_snapshot,
)


def order(order_id: str, price: str) -> NativeOrderEvidence:
    return NativeOrderEvidence(order_id, "pos", "BTC", "SHORT", D("2"), D(price),
                               VenueOrderStatus.ACCEPTED, True, 12)


def entry() -> EntryEvidence:
    return EntryEvidence(
        decision_id="dec", opportunity_id="opp", instrument_id="BTC", side="LONG",
        decision_ns=10,
        decision_json='{"decision_id":"dec","opportunity_id":"opp","belief_id":"belief",'
                      '"action":"ENTER","instrument_id":"BTC"}',
        belief=ThesisBelief(belief_id="belief", version_hash="a" * 64, regime="trend", as_of_ns=9),
        experts=(ExpertEvidence(expert_id="e1", stance=StanceKind.SUPPORT),),
        evidence_available_ns=9, policy_version="p1", fill_id="fill", entry_order_filled=True, entry_fill_count=1,
        settlement_currency="USDT", contract_type="LINEAR", contract_multiplier=D("1"), entry_order_id="entry",
        position_id="pos", fill_quantity=D("2"), fill_price=D("100"), fill_ns=11,
        stop=order("stop", "90"), target=order("target", "120"), simulation_only=True,
    )


def capture(e: EntryEvidence | None = None):
    return capture_entry(
        e or entry(), as_of_ns=12, max_order_age_ns=2, max_input_age_ns=2,
        expected_bars=2, max_bars=5,
        invalidation_rules=(InvalidationRule(kind=InvalidationKind.PRICE_STOP),
                            InvalidationRule(kind=InvalidationKind.REGIME_CHANGE)),
    )


def run(receipt, *, stop=None, now=20, regime="trend", observed=20, available=20):
    snapshot = PositionSnapshot(native_position_id="pos", quantity=D("2"), average_price=D("100"),
                                mark_price=D("105"), protective_order_ids=("stop",),
                                latest_belief_id="current", elapsed_bars=1, available_at_ns=observed)
    current = CurrentEvidence(snapshot=snapshot, instrument_id="BTC", side="LONG",
                              stop=stop or replace(order("stop", "90"), observed_ns=observed),
                              venue_status=VenueOrderStatus.ACCEPTED, observed_ns=observed,
                              max_age_ns=3, simulation_only=True)
    belief = ThesisBelief(belief_id="current", version_hash="b" * 64, regime=regime, as_of_ns=available)
    return evaluate_snapshot(receipt, current, belief, now_ns=now, evidence=entry().experts,
                             evidence_available_ns=available, fee_bps=D("1"), lot_step=D("0.1"),
                             tick_size=D("0.01"))


def test_entry_preserves_fill_and_original_risk_in_immutable_receipt() -> None:
    receipt = capture()
    assert receipt.thesis.original_risk_usdt == D("20")
    assert receipt.entry.fill_id == "fill" and receipt.entry.decision_json == entry().decision_json
    assert receipt.evidence_sha256 == capture().evidence_sha256
    with pytest.raises(FrozenInstanceError):
        receipt.entry.fill_id = "different"  # type: ignore[misc]


@pytest.mark.parametrize("change", [
    {"entry_order_filled": False},
    {"entry_fill_count": 2},
    {"fill_id": ""},
    {"evidence_available_ns": 11},
    {"simulation_only": False},
    {"stop": replace(order("stop", "90"), status=VenueOrderStatus.SUBMITTED)},
    {"stop": replace(order("stop", "90"), quantity=D("1"))},
    {"stop": replace(order("stop", "90"), position_id="other")},
    {"target": replace(order("stop", "120"))},
    {"experts": (entry().experts[0], entry().experts[0])},
    {"decision_json": "{}"},
])
def test_entry_refuses_unconfirmed_or_unlinked_evidence(change) -> None:
    with pytest.raises(ValueError):
        capture(replace(entry(), **change))


def test_recurrent_management_is_advisory_and_thesis_driven() -> None:
    receipt = capture()
    evaluation, management = run(receipt, regime="shock")
    assert evaluation.status is ThesisStatus.INVALIDATED
    assert management.advisory.action is DecisionAction.EXIT
    assert management.advisory.advisory_only
    assert receipt.thesis.original_risk_usdt == D("20")


def test_stale_or_widened_stop_is_never_protected() -> None:
    receipt = capture()
    _, stale = run(receipt, now=25)
    _, widened = run(receipt, stop=replace(order("stop", "89"), observed_ns=20))
    assert not stale.admission_ok and not stale.protected
    assert not widened.admission_ok and not widened.protected
