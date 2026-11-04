"""Fail-closed opportunity-to-cashflow lineage certification (M0-03, issue #4)."""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest

from v8_next.audit.lineage import (
    CampaignMismatch,
    CompletenessVerdict,
    DuplicateFill,
    DuplicateRecord,
    EventTimeInversion,
    LateRecord,
    LineageAuditor,
    LineageCashflow,
    LineageChain,
    LineageDecision,
    LineageFill,
    LineageOpportunity,
    LineageOrder,
    LineageViolation,
    LineageViolationCode,
    MislinkedRecord,
    MissingJoin,
    OrphanRecord,
    PointInTimeLeakage,
    certify,
    require_complete,
)

AS_OF_NS = 200

OPPORTUNITY = LineageOpportunity(
    opportunity_id="OPP-1",
    campaign_id="CAMP-1",
    instrument_id="BTCUSDT-PERP",
    created_ns=90,
    available_ns=91,
)

DECISION = LineageDecision(
    decision_id="DEC-1",
    opportunity_id="OPP-1",
    campaign_id="CAMP-1",
    instrument_id="BTCUSDT-PERP",
    decision_time_ns=100,
    available_at_ns=101,
)
ORDER = LineageOrder(
    order_id="ORD-1",
    decision_id="DEC-1",
    campaign_id="CAMP-1",
    instrument_id="BTCUSDT-PERP",
    event_time_ns=110,
    available_at_ns=111,
)
FILL = LineageFill(
    fill_id="FIL-1",
    order_id="ORD-1",
    decision_id="DEC-1",
    campaign_id="CAMP-1",
    instrument_id="BTCUSDT-PERP",
    event_time_ns=120,
    available_at_ns=121,
)
CASHFLOW = LineageCashflow(
    cashflow_id="CF-1",
    fill_id="FIL-1",
    campaign_id="CAMP-1",
    event_time_ns=130,
    available_at_ns=131,
    amount=Decimal("-0.05"),
)


def _chain(
    *,
    opportunities: tuple[LineageOpportunity, ...] = (OPPORTUNITY,),
    decisions: tuple[LineageDecision, ...] = (DECISION,),
    orders: tuple[LineageOrder, ...] = (ORDER,),
    fills: tuple[LineageFill, ...] = (FILL,),
    cashflows: tuple[LineageCashflow, ...] = (CASHFLOW,),
) -> LineageChain:
    return LineageChain(
        opportunities=opportunities,
        decisions=decisions,
        orders=orders,
        fills=fills,
        cashflows=cashflows,
    )


def _codes(violations: tuple[LineageViolation, ...]) -> set[LineageViolationCode]:
    return {violation.code for violation in violations}


# --------------------------------------------------------------------------- #
# A. complete chain
# --------------------------------------------------------------------------- #
def test_complete_chain_certifies_with_every_join_checked() -> None:
    certification = certify(_chain(), as_of_ns=AS_OF_NS)
    assert certification.verdict is CompletenessVerdict.COMPLETE
    assert certification.certified is True
    assert certification.violations == ()
    assert certification.joins_checked == 4
    assert certification.campaign_ids == ("CAMP-1",)
    assert len(certification.provenance_digest) == 64
    require_complete(certification)


def test_provenance_digest_is_order_independent() -> None:
    opportunity_two = replace(
        OPPORTUNITY,
        opportunity_id="OPP-2",
        created_ns=130,
        available_ns=131,
    )
    decision_two = replace(
        DECISION,
        decision_id="DEC-2",
        opportunity_id="OPP-2",
        decision_time_ns=140,
        available_at_ns=141,
    )
    order_two = replace(
        ORDER, order_id="ORD-2", decision_id="DEC-2", event_time_ns=150, available_at_ns=151
    )
    fill_two = replace(
        FILL,
        fill_id="FIL-2",
        order_id="ORD-2",
        decision_id="DEC-2",
        event_time_ns=160,
        available_at_ns=161,
    )
    cashflow_two = replace(
        CASHFLOW,
        cashflow_id="CF-2",
        fill_id="FIL-2",
        event_time_ns=170,
        available_at_ns=171,
    )
    forward = certify(
        LineageChain(
            opportunities=(OPPORTUNITY, opportunity_two),
            decisions=(DECISION, decision_two),
            orders=(ORDER, order_two),
            fills=(FILL, fill_two),
            cashflows=(CASHFLOW, cashflow_two),
        ),
        as_of_ns=AS_OF_NS,
    )
    reversed_chain = LineageChain(
        opportunities=(opportunity_two, OPPORTUNITY),
        decisions=(decision_two, DECISION),
        orders=(order_two, ORDER),
        fills=(fill_two, FILL),
        cashflows=(cashflow_two, CASHFLOW),
    )
    backward = certify(reversed_chain, as_of_ns=AS_OF_NS)
    assert forward.verdict is CompletenessVerdict.COMPLETE
    assert backward.verdict is CompletenessVerdict.COMPLETE
    assert forward.provenance_digest == backward.provenance_digest


# --------------------------------------------------------------------------- #
# B. fail-closed negative cases, one per failure class
# --------------------------------------------------------------------------- #
def test_missing_join_key_cannot_certify_complete() -> None:
    broken = _chain(fills=(replace(FILL, order_id=""),))
    certification = certify(broken, as_of_ns=AS_OF_NS)
    assert certification.verdict is CompletenessVerdict.INCOMPLETE
    assert certification.certified is False
    assert LineageViolationCode.MISSING_JOIN in _codes(certification.violations)
    with pytest.raises(MissingJoin):
        require_complete(certification)


def test_orphaned_record_is_typed_and_not_complete() -> None:
    orphan = replace(CASHFLOW, fill_id="FIL-UNKNOWN")
    certification = certify(_chain(cashflows=(orphan,)), as_of_ns=AS_OF_NS)
    assert certification.verdict is CompletenessVerdict.INCOMPLETE
    assert LineageViolationCode.ORPHAN_RECORD in _codes(certification.violations)
    orphan_violations = [row for row in certification.violations if isinstance(row, OrphanRecord)]
    assert orphan_violations[0].record_id == "CF-1"
    with pytest.raises(OrphanRecord):
        require_complete(certification)


def test_duplicated_fill_is_typed_and_not_complete() -> None:
    certification = certify(_chain(fills=(FILL, FILL)), as_of_ns=AS_OF_NS)
    assert certification.verdict is CompletenessVerdict.INCOMPLETE
    assert LineageViolationCode.DUPLICATE_FILL in _codes(certification.violations)
    assert isinstance(certification.violations[0], DuplicateFill)
    with pytest.raises(DuplicateFill):
        require_complete(certification)


def test_late_record_blocks_certification() -> None:
    late_fill = replace(FILL, available_at_ns=AS_OF_NS + 1)
    certification = certify(_chain(fills=(late_fill,)), as_of_ns=AS_OF_NS)
    assert certification.verdict is CompletenessVerdict.BLOCKED
    assert certification.certified is False
    assert isinstance(certification.violations[0], LateRecord)
    with pytest.raises(LateRecord):
        require_complete(certification)


def test_mislinked_native_id_is_typed() -> None:
    mislinked = replace(FILL, decision_id="DEC-OTHER")
    certification = certify(_chain(fills=(mislinked,)), as_of_ns=AS_OF_NS)
    assert certification.verdict is CompletenessVerdict.INCOMPLETE
    assert isinstance(certification.violations[0], MislinkedRecord)
    with pytest.raises(MislinkedRecord):
        require_complete(certification)


def test_campaign_mismatch_never_substitutes_last_seen_campaign() -> None:
    swapped = replace(FILL, campaign_id="CAMP-2")
    certification = certify(_chain(fills=(swapped,)), as_of_ns=AS_OF_NS)
    assert certification.verdict is CompletenessVerdict.INCOMPLETE
    assert isinstance(certification.violations[0], CampaignMismatch)
    # The certification only ever reports the campaign the decision actually declared.
    assert certification.campaign_ids == ("CAMP-1",)
    with pytest.raises(CampaignMismatch):
        require_complete(certification)


def test_event_time_inversion_is_typed() -> None:
    early_cashflow = replace(CASHFLOW, event_time_ns=100)
    certification = certify(_chain(cashflows=(early_cashflow,)), as_of_ns=AS_OF_NS)
    assert certification.verdict is CompletenessVerdict.INCOMPLETE
    assert isinstance(certification.violations[0], EventTimeInversion)
    with pytest.raises(EventTimeInversion):
        require_complete(certification)


def test_missing_layers_block_and_never_certify_complete() -> None:
    # No decisions at all: the join cannot even be attempted.
    certification = certify(_chain(decisions=()), as_of_ns=AS_OF_NS)
    assert certification.verdict is CompletenessVerdict.BLOCKED
    assert certification.certified is False
    assert LineageViolationCode.MISSING_JOIN in _codes(certification.violations)

    empty = certify(LineageChain(), as_of_ns=AS_OF_NS)
    assert empty.verdict is CompletenessVerdict.BLOCKED
    assert empty.joins_checked == 0


def test_fill_without_cashflow_evidence_is_a_missing_join() -> None:
    extra_fill = replace(FILL, fill_id="FIL-2", event_time_ns=140)
    certification = certify(_chain(fills=(FILL, extra_fill)), as_of_ns=AS_OF_NS)
    assert certification.verdict is CompletenessVerdict.INCOMPLETE
    join_violations = [
        row
        for row in certification.violations
        if isinstance(row, MissingJoin) and row.record_id == "FIL-2"
    ]
    assert join_violations
    with pytest.raises(MissingJoin):
        require_complete(certification)


# --------------------------------------------------------------------------- #
# B2. the opportunity layer is a real, required join
# --------------------------------------------------------------------------- #
def test_decision_with_unknown_opportunity_is_a_typed_missing_join() -> None:
    certification = certify(
        _chain(decisions=(replace(DECISION, opportunity_id="OPP-UNKNOWN"),)),
        as_of_ns=AS_OF_NS,
    )
    assert certification.verdict is CompletenessVerdict.INCOMPLETE
    assert certification.certified is False
    assert LineageViolationCode.MISSING_JOIN in _codes(certification.violations)
    join_violations = [
        row
        for row in certification.violations
        if isinstance(row, MissingJoin) and row.record_id == "DEC-1"
    ]
    assert join_violations
    assert "OPP-UNKNOWN" in join_violations[0].detail
    with pytest.raises(MissingJoin):
        require_complete(certification)


def test_duplicated_opportunity_id_cannot_resolve_to_one_record() -> None:
    duplicate = replace(OPPORTUNITY, available_ns=92)
    certification = certify(
        _chain(opportunities=(OPPORTUNITY, duplicate)),
        as_of_ns=AS_OF_NS,
    )
    assert certification.verdict is CompletenessVerdict.INCOMPLETE
    assert certification.certified is False
    assert LineageViolationCode.DUPLICATE_RECORD in _codes(certification.violations)
    typed = [
        row
        for row in certification.violations
        if isinstance(row, DuplicateRecord) and row.record_id == "OPP-1"
    ]
    assert typed
    # The decision naming the duplicated id cannot bind to a single opportunity.
    ambiguous = [
        row
        for row in certification.violations
        if isinstance(row, MissingJoin) and row.record_id == "DEC-1"
    ]
    assert ambiguous and "duplicated" in ambiguous[0].detail
    with pytest.raises(LineageViolation):
        require_complete(certification)


def test_cross_campaign_opportunity_is_a_campaign_mismatch() -> None:
    foreign = replace(OPPORTUNITY, campaign_id="CAMP-2")
    certification = certify(_chain(opportunities=(foreign,)), as_of_ns=AS_OF_NS)
    assert certification.verdict is CompletenessVerdict.INCOMPLETE
    assert certification.certified is False
    assert LineageViolationCode.CAMPAIGN_MISMATCH in _codes(certification.violations)
    mismatch = [row for row in certification.violations if isinstance(row, CampaignMismatch)]
    assert mismatch[0].record_id == "DEC-1"
    with pytest.raises(CampaignMismatch):
        require_complete(certification)


def test_opportunity_instrument_mismatch_is_mislinked() -> None:
    foreign = replace(OPPORTUNITY, instrument_id="ETHUSDT-PERP")
    certification = certify(_chain(opportunities=(foreign,)), as_of_ns=AS_OF_NS)
    assert certification.verdict is CompletenessVerdict.INCOMPLETE
    mislinked = [
        row
        for row in certification.violations
        if isinstance(row, MislinkedRecord) and row.record_id == "DEC-1"
    ]
    assert mislinked
    with pytest.raises(MislinkedRecord):
        require_complete(certification)


def test_decisions_without_any_opportunity_layer_are_blocked() -> None:
    certification = certify(_chain(opportunities=()), as_of_ns=AS_OF_NS)
    assert certification.verdict is CompletenessVerdict.BLOCKED
    assert certification.certified is False
    layer_gap = [
        row
        for row in certification.violations
        if isinstance(row, MissingJoin) and row.record_id == "<opportunities>"
    ]
    assert layer_gap
    with pytest.raises(MissingJoin):
        require_complete(certification)


# --------------------------------------------------------------------------- #
# C. existing point-in-time behaviour is preserved
# --------------------------------------------------------------------------- #
def test_pit_causality_auditor_behaviour_is_unchanged() -> None:
    report = LineageAuditor.audit_pit_causality(100, [("bar", 99)])
    assert report.passed is True
    assert report.records_checked == 1
    with pytest.raises(PointInTimeLeakage):
        LineageAuditor.audit_pit_causality(100, [("funding", 101)])
