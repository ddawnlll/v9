"""MECHANICS ONLY: decision-trail firewall, append-only receipts, identity re-derivation.

No tape, no network, no economic assertion. These tests exercise the substrate's type and
arithmetic contracts only:

* the PIT/hindsight firewall refuses an evidence-plane stage by name (invariant 3);
* a written receipt cannot be mutated, and the ledger refuses to rewrite one (invariant 2);
* a receipt identity re-derives from its content, and fails closed when it does not;
* two receipts differing only in wall clock share an identity, and so do two trails;
* an ex-ante dimension V8 does not compute stays explicitly unavailable (invariant 1).
"""

from __future__ import annotations

import dataclasses
import json
import time
from typing import Any

import pytest

from v8_next.adapters.execution_telemetry import (
    TRAIL_DIGEST_FIELD,
    TRAIL_DIGEST_MISMATCH,
    TRAIL_DIGEST_MISSING,
    TRAIL_SPAN_NOT_PIT_DECISION,
    TRAIL_TRACE_LINEAGE_MISMATCH,
    attach_decision_trail,
    verify_decision_trail,
)
from v8_next.telemetry import (
    UNAVAILABLE_EX_ANTE_DIMENSIONS,
    BeliefReceipt,
    BeliefReceiptId,
    BeliefReceiptVerificationError,
    BeliefStage,
    ChosenAction,
    ConflictingBeliefReceiptError,
    DecisionBeliefLedger,
    DecisionSpan,
    DecisionStage,
    EconomicTraceContext,
    EconomicTraceId,
    EvidencePlaneStageRejected,
    EvidenceSpan,
    EvidenceStage,
    ExAnteCostExpectation,
    ExAnteUncertainty,
    FabricatedBeliefError,
    PitAncestryError,
    SpanId,
    SpanKind,
    SpanLink,
    SpanLinkType,
    TraceLineageError,
    TraceProvenance,
    TrajectoryType,
    canonical_digest,
    require_pit_ancestor,
    require_pit_decision_stage,
)

PIT_NS = 1_700_000_000_000_000_000


def _provenance() -> TraceProvenance:
    return TraceProvenance.new("tape-hash-a", "policy-hash-b", "constitution-hash-c", "code-hash-d")


def _context(
    trajectory_type: TrajectoryType = TrajectoryType.Observed,
    trajectory_tag: str = "canonical_observed",
    opportunity_id: str = "opp-1",
) -> EconomicTraceContext:
    return EconomicTraceContext.new(
        opportunity_id, trajectory_type, trajectory_tag, PIT_NS, _provenance()
    )


def _span(
    context: EconomicTraceContext,
    stage: DecisionStage = DecisionStage.SelectiveUtility,
    disambiguator: str = "span-0",
) -> DecisionSpan:
    return DecisionSpan.new(context.trace_id, None, stage, PIT_NS, disambiguator)


def _receipt(context: EconomicTraceContext, span: DecisionSpan, **overrides: Any) -> BeliefReceipt:
    kwargs: dict[str, Any] = {
        "trace_id": context.trace_id,
        "opportunity_id": context.opportunity_id,
        "span_id": span.span_id,
        "stage": BeliefStage.PostSelectiveUtility,
        "pit_timestamp": span.start_time,
        "provenance": context.provenance,
        "expected_gross_edge_bps": 12.5,
        "expected_net_utility_r": 0.31,
        "expected_horizon_bars": 24,
        "cost_expectation": ExAnteCostExpectation(
            total_friction_bps=8.5,
            entry_fee_bps=2.0,
            exit_fee_bps=2.0,
            bid_ask_spread_bps=1.5,
            funding_rate_est=0.5,
            slippage_bps=2.0,
            uncertainty_buffer_bps=0.5,
        ),
        "uncertainty": ExAnteUncertainty(
            contradiction_entropy=0.42,
            uncertainty_penalty_r=0.05,
            effective_observer_count=2.5,
        ),
        "chosen_action": ChosenAction.selective_utility_decision(
            action="UtilityAction::Executable", expected_net_utility_r=0.31
        ),
        "is_rejection": False,
        "rejection_reason": None,
    }
    kwargs.update(overrides)
    return BeliefReceipt.new(**kwargs)


# ------------------------------------------------------------------ vocabulary (EEO-001H)


def test_decision_stage_vocabulary_is_the_eleven_pit_stages() -> None:
    stages = list(DecisionStage)
    assert [stage.as_str() for stage in stages] == [
        "MarketState",
        "OpportunityDetection",
        "WitnessObservation",
        "EvidenceReconciliation",
        "SelectiveUtility",
        "PortfolioFeasibility",
        "CampaignAdmission",
        "OrderDispatch",
        "ExecutionFill",
        "PositionManagement",
        "CashflowSettlement",
    ]
    assert all(stage.is_pit_decision() for stage in stages)
    assert len(stages) == 11


def test_evidence_stage_vocabulary_is_disjoint_from_the_pit_stages() -> None:
    evidence = [stage.as_str() for stage in EvidenceStage]
    assert evidence == [
        "TargetOracleHindsight",
        "HindsightPathAnalysis",
        "AuditAdjudication",
        "ProviderEvaluation",
        "MultiplicityAccounting",
    ]
    assert all(stage.is_post_outcome() for stage in EvidenceStage)
    assert not set(evidence) & {stage.as_str() for stage in DecisionStage}


def test_span_link_type_carries_an_explicit_counterfactual_branch() -> None:
    assert SpanLinkType.CounterfactualBranch.is_counterfactual_branch()
    assert not SpanLinkType.RelatedDecision.is_counterfactual_branch()
    assert len(list(SpanLinkType)) == 7


def test_span_kind_carries_exactly_one_plane() -> None:
    assert SpanKind.for_decision(DecisionStage.MarketState).is_pit_decision()
    assert SpanKind.for_evidence(EvidenceStage.AuditAdjudication).is_evidence()
    with pytest.raises(TraceLineageError):
        SpanKind()
    with pytest.raises(TraceLineageError):
        SpanKind(
            decision_stage=DecisionStage.MarketState,
            evidence_stage=EvidenceStage.AuditAdjudication,
        )


# ------------------------------------------------------------------ firewall (invariant 3)


def test_evidence_plane_stage_is_rejected_by_name() -> None:
    with pytest.raises(EvidencePlaneStageRejected) as excinfo:
        BeliefStage.from_decision_stage(EvidenceStage.ProviderEvaluation)
    assert excinfo.value.code == "EVIDENCE_PLANE_STAGE_REJECTED"

    with pytest.raises(EvidencePlaneStageRejected):
        require_pit_decision_stage(EvidenceStage.TargetOracleHindsight)
    with pytest.raises(EvidencePlaneStageRejected):
        require_pit_decision_stage(SpanKind.for_evidence(EvidenceStage.HindsightPathAnalysis))


def test_a_belief_receipt_cannot_be_built_from_an_evidence_plane_stage() -> None:
    context = _context()
    span = _span(context)
    with pytest.raises(EvidencePlaneStageRejected):
        _receipt(context, span, stage=EvidenceStage.ProviderEvaluation)
    with pytest.raises(TraceLineageError):
        _receipt(context, span, stage="NotABeliefStage")


def test_every_pit_stage_maps_onto_a_belief_checkpoint() -> None:
    expected = {
        DecisionStage.MarketState: BeliefStage.OpportunityDetected,
        DecisionStage.OpportunityDetection: BeliefStage.OpportunityDetected,
        DecisionStage.WitnessObservation: BeliefStage.PostWitnessObservation,
        DecisionStage.EvidenceReconciliation: BeliefStage.PostReconciliation,
        DecisionStage.SelectiveUtility: BeliefStage.PostSelectiveUtility,
        DecisionStage.PortfolioFeasibility: BeliefStage.PortfolioFeasibilityEvaluated,
        DecisionStage.CampaignAdmission: BeliefStage.CampaignDispatched,
        DecisionStage.OrderDispatch: BeliefStage.CampaignDispatched,
        DecisionStage.ExecutionFill: BeliefStage.CampaignDispatched,
        DecisionStage.PositionManagement: BeliefStage.CampaignDispatched,
        DecisionStage.CashflowSettlement: BeliefStage.CampaignDispatched,
    }
    assert {stage: BeliefStage.from_decision_stage(stage) for stage in DecisionStage} == expected


def test_a_pit_span_cannot_descend_from_the_evidence_plane() -> None:
    require_pit_ancestor(None)
    require_pit_ancestor(SpanKind.for_decision(DecisionStage.WitnessObservation))
    with pytest.raises(PitAncestryError) as excinfo:
        require_pit_ancestor(SpanKind.for_evidence(EvidenceStage.AuditAdjudication))
    assert excinfo.value.code == "EVIDENCE_PLANE_ANCESTRY"


# ------------------------------------------------------------------ append-only (invariant 2)


def test_a_written_receipt_cannot_be_mutated() -> None:
    context = _context()
    receipt = _receipt(context, _span(context))
    with pytest.raises(dataclasses.FrozenInstanceError):
        receipt.is_rejection = True  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        receipt.expected_gross_edge_bps = 999.0  # type: ignore[misc]


def test_a_decision_span_closes_into_a_new_span_and_validates_monotonic_time() -> None:
    context = _context()
    open_span = _span(context)
    assert not open_span.is_closed
    closed = open_span.close(PIT_NS + 60)
    assert closed.end_time == PIT_NS + 60
    assert open_span.end_time is None  # the span a consumer holds was not rewritten
    with pytest.raises(TraceLineageError):
        open_span.close(PIT_NS - 1)


def test_the_ledger_appends_and_refuses_to_rewrite_a_receipt() -> None:
    context = _context()
    receipt = _receipt(context, _span(context))
    ledger = DecisionBeliefLedger()
    ledger.append(receipt)
    ledger.append(receipt)  # identical retry is idempotent
    assert len(ledger) == 1 and not ledger.is_empty()

    tampered = dataclasses.replace(receipt, expected_gross_edge_bps=999.0)
    assert tampered.receipt_id == receipt.receipt_id
    with pytest.raises(ConflictingBeliefReceiptError) as excinfo:
        ledger.append(tampered)
    assert excinfo.value.code == "CONFLICTING_BELIEF_RECEIPT"
    assert len(ledger) == 1
    assert ledger.get(receipt.receipt_id) == receipt


def test_a_rejected_opportunity_still_leaves_a_final_receipt() -> None:
    context = _context()
    span = _span(context, DecisionStage.EvidenceReconciliation, "span-rejected")
    rejected = _receipt(
        context,
        span,
        stage=BeliefStage.PostReconciliation,
        expected_gross_edge_bps=None,
        expected_net_utility_r=None,
        cost_expectation=None,
        uncertainty=None,
        chosen_action=ChosenAction.reconciled(
            stance="Contradicted",
            effective_observer_count=1.5,
            support_weight=0.2,
            contradict_weight=1.3,
            contradiction_entropy=0.7,
        ),
        is_rejection=True,
        rejection_reason="ReconciledStance::Contradicted",
    )
    ledger = DecisionBeliefLedger()
    ledger.append(rejected)
    final = ledger.final_belief_for_opportunity("opp-1")
    assert final is not None and final.is_rejection
    assert final.rejection_reason == "ReconciledStance::Contradicted"


# ------------------------------------------------------------------ identity (invariant 5)


def test_receipt_identity_re_derives_from_its_content() -> None:
    context = _context()
    receipt = _receipt(context, _span(context))
    receipt.verify()  # intact

    tampered = dataclasses.replace(receipt, expected_net_utility_r=0.99)
    with pytest.raises(BeliefReceiptVerificationError) as excinfo:
        tampered.verify()
    assert excinfo.value.code == "BELIEF_RECEIPT_DIGEST_MISMATCH"

    restored = BeliefReceipt.from_dict(json.loads(json.dumps(receipt.as_dict())))
    restored.verify()
    assert restored == receipt

    ledger = DecisionBeliefLedger()
    ledger.append(receipt)
    document = json.loads(ledger.to_json())
    document["receipts"][0]["expected_gross_edge_bps"] = 999.0
    rewritten = DecisionBeliefLedger.from_json(json.dumps(document))
    with pytest.raises(BeliefReceiptVerificationError):
        rewritten.validate_lineage()


def test_wall_clock_is_not_an_identity_input() -> None:
    context = _context()
    span = _span(context)
    before = time.time_ns()
    first = _receipt(context, span)
    time.sleep(0.001)
    after = time.time_ns()
    second = _receipt(context, span)

    assert after > before  # the two receipts really were built at different wall-clock times
    assert first.receipt_id == second.receipt_id
    assert first == second
    assert first.provenance.compute_hash() == second.provenance.compute_hash()
    assert context.compute_hash() == EconomicTraceContext.new(
        "opp-1", TrajectoryType.Observed, "canonical_observed", PIT_NS, _provenance()
    ).compute_hash()
    assert not {"created_ns", "recorded_ns", "wall_clock_ns", "created_at"} & set(first.as_dict())

    # negative control: identity is content-bound, so a real field change does move it
    assert _receipt(context, span, pit_timestamp=PIT_NS + 1).receipt_id != first.receipt_id
    changed_body = _receipt(context, span, expected_gross_edge_bps=12.6)
    # ... and a change confined to the ex-ante body leaves the lineage id alone but moves the
    # content digest, which is exactly why verify() checks both.
    assert changed_body.receipt_id == first.receipt_id
    assert changed_body.content_hash != first.content_hash
    changed_body.verify()  # its own body still re-derives: nothing was rewritten in place


def test_identity_lineage_types_are_distinct() -> None:
    context = _context()
    span = _span(context)
    receipt = _receipt(context, span)
    assert type(receipt.receipt_id) is BeliefReceiptId
    assert type(context.trace_id) is EconomicTraceId
    assert type(span.span_id) is SpanId
    assert type(context.provenance) is TraceProvenance
    assert canonical_digest("EconomicTraceId", ["x"]) != canonical_digest("SpanId", ["x"])
    assert canonical_digest("BeliefReceiptId", ["x"]) != canonical_digest("EconomicTraceId", ["x"])


def test_uncomputed_ex_ante_dimensions_stay_unavailable() -> None:
    context = _context()
    receipt = _receipt(context, _span(context))
    assert len(UNAVAILABLE_EX_ANTE_DIMENSIONS) == 3
    assert receipt.outcome_probabilities is None
    assert receipt.expected_mfe_r is None
    assert receipt.expected_mae_r is None
    assert receipt.unavailable_dimensions() == UNAVAILABLE_EX_ANTE_DIMENSIONS

    fabricated = dataclasses.replace(receipt, expected_mfe_r=1.5)
    with pytest.raises(FabricatedBeliefError) as excinfo:
        fabricated.verify()
    assert excinfo.value.code == "FABRICATED_EX_ANTE_DIMENSION"


# ------------------------------------------------------------------ trail binding


def _counterfactual_link(context: EconomicTraceContext) -> SpanLink:
    other = EconomicTraceContext.new(
        "opp-1", TrajectoryType.Counterfactual, "challenger_contract_b", PIT_NS, _provenance()
    )
    return SpanLink.new(
        other.trace_id, None, "opp-1", SpanLinkType.CounterfactualBranch
    ).with_attribute("branch", "challenger_contract_b")


def test_attach_decision_trail_is_additive_and_hash_bound() -> None:
    context = _context()
    spans = (
        _span(context, DecisionStage.WitnessObservation, "span-0"),
        _span(context, DecisionStage.SelectiveUtility, "span-1").with_link(
            _counterfactual_link(context)
        ),
    )
    payload: dict[str, Any] = {"policy_id": "swing-v1", "index": 3, "actual": {"net_return": -0.01}}
    original = json.loads(json.dumps(payload))

    bound = attach_decision_trail(payload, trace_context=context, spans=spans)
    assert payload == original  # the input record is not mutated
    assert bound["policy_id"] == "swing-v1"
    trail = bound["decision_trail"]
    assert trail["stages"] == ["WitnessObservation", "SelectiveUtility"]
    assert trail["open_spans"] == 2
    assert trail["is_observed"] is True and trail["is_counterfactual"] is False
    assert trail["trace_context"]["trace_id"] == context.trace_id.as_str()
    assert [link["link_type"] for link in trail["counterfactual_branches"]] == [
        "CounterfactualBranch"
    ]
    assert verify_decision_trail(trail) == trail[TRAIL_DIGEST_FIELD]
    json.dumps(bound)  # the trail is JSON-serialisable as attached


def test_attach_decision_trail_refuses_a_foreign_or_evidence_span() -> None:
    context = _context()
    foreign = _context(opportunity_id="opp-2")
    with pytest.raises(TraceLineageError) as excinfo:
        attach_decision_trail({}, trace_context=context, spans=[_span(foreign)])
    assert excinfo.value.code == TRAIL_TRACE_LINEAGE_MISMATCH

    evidence = EvidenceSpan.new(
        context.trace_id, _span(context).span_id, EvidenceStage.ProviderEvaluation, PIT_NS + 1, "e0"
    )
    with pytest.raises(TraceLineageError) as excinfo:
        attach_decision_trail({}, trace_context=context, spans=[evidence])  # type: ignore[list-item]
    assert excinfo.value.code == TRAIL_SPAN_NOT_PIT_DECISION


def test_verify_decision_trail_fails_closed_when_edited() -> None:
    context = _context()
    trail = attach_decision_trail(
        {}, trace_context=context, spans=[_span(context)]
    )["decision_trail"]

    edited = dict(trail)
    edited["stages"] = ["MarketState"]
    with pytest.raises(TraceLineageError) as excinfo:
        verify_decision_trail(edited)
    assert excinfo.value.code == TRAIL_DIGEST_MISMATCH

    undigested = {key: value for key, value in trail.items() if key != TRAIL_DIGEST_FIELD}
    with pytest.raises(TraceLineageError) as excinfo:
        verify_decision_trail(undigested)
    assert excinfo.value.code == TRAIL_DIGEST_MISSING


def test_a_counterfactual_trail_is_not_an_observed_trail() -> None:
    observed = _context()
    counterfactual = _context(TrajectoryType.Counterfactual, "challenger_contract_b")
    assert observed.trace_id != counterfactual.trace_id
    assert counterfactual.trajectory_type.is_counterfactual()
    assert not counterfactual.trajectory_type.is_observed()
    assert TrajectoryType.Observed.as_str() == "Observed"
