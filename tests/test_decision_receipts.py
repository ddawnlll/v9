"""M2-05: immutable decision receipts and append-only decision ledger."""

from __future__ import annotations

import ast
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from v8_next.domain.decision import (
    DecisionAction,
    IntentKind,
    ReasonCode,
    TraderDecision,
    VenueOrderStatus,
    derive_decision_id,
)
from v8_next.economics.legality import PositionPresence
from v8_next.economics.policy import (
    POLICY_HASH,
    POLICY_VERSION,
    DecisionRequest,
    PortfolioState,
    PositionState,
    TraderDecisionPolicy,
)
from v8_next.telemetry import decisions as decisions_module
from v8_next.telemetry.decisions import (
    DecisionInputRef,
    DecisionLedger,
    DecisionReceipt,
    OrphanDecision,
    ReceiptConflict,
    ReceiptInputs,
    ReceiptRejected,
    UnknownPolicyVersion,
    is_orphan,
)

NS = 1_700_000_000_000_000_000
INSTRUMENT = "BTCUSDT-PERP.BINANCE"
OPPORTUNITY = "opp-0001"
FORBIDDEN_FIELD_TOKENS = ("fill", "price", "quantity", "qty", "cashflow", "notional", "order")


def make_decision(
    *,
    action: DecisionAction = DecisionAction.ENTER,
    intent: IntentKind = IntentKind.LIMIT_ENTRY,
    opportunity_id: str | None = OPPORTUNITY,
    decision_time_ns: int = NS,
    policy_version: str = POLICY_VERSION,
    reason_codes: tuple[ReasonCode, ...] = (ReasonCode.OPPORTUNITY_ARMED,),
    belief_version_hash: str | None = "belief-a",
) -> TraderDecision:
    return TraderDecision.create(
        decision_time_ns=decision_time_ns,
        instrument_id=INSTRUMENT,
        opportunity_id=opportunity_id,
        action=action,
        intent_kind=intent,
        policy_version=policy_version,
        reason_codes=reason_codes,
        belief_version_hash=belief_version_hash,
    )


def inputs(*refs: tuple[str, int], constraints: tuple[str, ...] = ()) -> ReceiptInputs:
    return ReceiptInputs(
        inputs=tuple(
            DecisionInputRef(observation_id=name, available_at_ns=at) for name, at in refs
        ),
        constraints=constraints,
    )


def make_receipt(
    decision: TraderDecision | None = None, receipt_inputs: ReceiptInputs | None = None
) -> DecisionReceipt:
    return DecisionReceipt.create(
        decision=decision or make_decision(),
        inputs=receipt_inputs or inputs(("obs-2", NS - 1_000), ("obs-1", NS - 2_000)),
    )


# ------------------------------------------------------------------- provenance


def test_receipt_records_the_decision_and_its_inputs() -> None:
    decision = make_decision()
    receipt = make_receipt(decision)

    assert receipt.decision_id == decision.decision_id
    assert receipt.decision_time_ns == decision.decision_time_ns
    assert receipt.instrument_id == decision.instrument_id
    assert receipt.opportunity_id == decision.opportunity_id
    assert receipt.action is decision.action
    assert receipt.proposed_intent is decision.intent_kind
    assert receipt.policy_version == POLICY_VERSION
    assert receipt.policy_hash == POLICY_HASH
    assert receipt.belief_version_hash == decision.belief_version_hash
    assert receipt.observation_ids == ("obs-1", "obs-2")
    assert receipt.input_available_at_ns == (NS - 2_000, NS - 1_000)
    assert receipt.max_input_available_at_ns == NS - 1_000
    assert receipt.reason_codes == decision.reason_codes
    assert receipt.advisory_only is True
    assert receipt.verify() is True


def test_receipt_carries_no_execution_fields() -> None:
    fields = set(DecisionReceipt.model_fields)
    for token in FORBIDDEN_FIELD_TOKENS:
        assert not any(token in name for name in fields), token
    assert "proposed_intent" in fields


def test_receipt_is_frozen() -> None:
    receipt = make_receipt()
    with pytest.raises(ValueError):
        receipt.action = DecisionAction.EXIT  # type: ignore[misc]


def test_belief_hash_is_optional_but_recorded_when_present() -> None:
    receipt = make_receipt(make_decision(belief_version_hash=None))
    assert receipt.belief_version_hash is None
    assert receipt.verify() is True


# -------------------------------------------------------------------- determinism


def test_same_replay_yields_the_same_digest() -> None:
    first = make_receipt()
    second = make_receipt()
    assert first.digest == second.digest
    assert first == second


def test_same_replay_across_ledgers_yields_the_same_digest() -> None:
    decision = make_decision()
    receipt_inputs = inputs(("obs-1", NS - 2_000), ("obs-2", NS - 1_000))
    left = DecisionLedger().append(decision, receipt_inputs)
    right = DecisionLedger().append(decision, receipt_inputs)
    assert left.digest == right.digest
    assert left.decision_id == right.decision_id


def test_input_order_does_not_change_the_digest() -> None:
    decision = make_decision()
    forward = make_receipt(decision, inputs(("obs-1", NS - 2_000), ("obs-2", NS - 1_000)))
    reverse = make_receipt(decision, inputs(("obs-2", NS - 1_000), ("obs-1", NS - 2_000)))
    assert forward.digest == reverse.digest


def test_digest_covers_constraints_and_reason_codes() -> None:
    decision = make_decision()
    base = make_receipt(decision, inputs(("obs-1", NS - 1_000), constraints=("STALE",)))
    other_constraints = make_receipt(
        decision, inputs(("obs-1", NS - 1_000), constraints=("CAPACITY_FULL",))
    )
    assert base.digest != other_constraints.digest

    other_reasons = make_receipt(
        make_decision(reason_codes=(ReasonCode.OPPORTUNITY_ARMED, ReasonCode.VENUE_PENDING))
    )
    assert base.digest != other_reasons.digest


def test_duplicate_observation_reference_is_refused() -> None:
    with pytest.raises(ReceiptRejected) as excinfo:
        make_receipt(receipt_inputs=inputs(("obs-1", NS - 1_000), ("obs-1", NS - 2_000)))
    assert excinfo.value.code == "DUPLICATE_INPUT"


# -------------------------------------------------------------------- append-only


def test_append_is_idempotent_for_identical_replays() -> None:
    ledger = DecisionLedger()
    decision = make_decision()
    receipt_inputs = inputs(("obs-1", NS - 1_000))
    first = ledger.append(decision, receipt_inputs)
    second = ledger.append(decision, receipt_inputs)
    assert first is second
    assert len(ledger) == 1
    assert ledger.get(decision.decision_id) is first


def test_conflicting_duplicate_is_refused() -> None:
    ledger = DecisionLedger()
    decision = make_decision()
    original = ledger.append(decision, inputs(("obs-1", NS - 1_000), constraints=("STALE",)))
    with pytest.raises(ReceiptConflict):
        ledger.append(decision, inputs(("obs-1", NS - 1_000), constraints=("CAPACITY_FULL",)))
    assert len(ledger) == 1
    assert ledger.get(decision.decision_id).digest == original.digest  # type: ignore[union-attr]


def test_receipts_snapshot_is_append_only() -> None:
    ledger = DecisionLedger()
    ledger.append(make_decision(), inputs(("obs-1", NS - 1_000)))
    snapshot = ledger.receipts()
    ledger.append(
        make_decision(action=DecisionAction.WAIT, intent=IntentKind.NONE, reason_codes=()),
        inputs(),
    )
    assert len(snapshot) == 1
    assert len(ledger.receipts()) == 2
    assert isinstance(snapshot, tuple)


# -------------------------------------------------------------------------- PIT


def test_non_pit_receipt_is_rejected_on_append() -> None:
    ledger = DecisionLedger()
    ledger_decisions = make_decision()
    future = inputs(("obs-future", NS + 1))
    receipt = DecisionReceipt.create(decision=ledger_decisions, inputs=future)
    assert receipt.verify() is False
    with pytest.raises(ReceiptRejected) as excinfo:
        ledger.append(ledger_decisions, future)
    assert excinfo.value.code == "NON_PIT_INPUT"
    assert len(ledger) == 0


def test_input_available_exactly_at_the_cut_is_allowed() -> None:
    ledger = DecisionLedger()
    receipt = ledger.append(make_decision(), inputs(("obs-1", NS)))
    assert receipt.max_input_available_at_ns == NS
    assert ledger.verify(receipt) is True


def test_ledger_verify_delegates_to_receipt() -> None:
    receipt = make_receipt()
    assert DecisionLedger().verify(receipt) is True


# ------------------------------------------------------------------ verification


def test_tampered_receipt_fails_verification() -> None:
    receipt = make_receipt()
    tampered = receipt.model_copy(update={"constraints": ("TAMPERED",)})
    assert tampered.verify() is False


def test_unknown_policy_version_cannot_be_recorded() -> None:
    decision = make_decision(policy_version="trader-policy-v0")
    with pytest.raises(UnknownPolicyVersion):
        DecisionReceipt.create(decision=decision, inputs=inputs())


def test_expected_policy_hash_mismatch_fails_verification() -> None:
    receipt = make_receipt()
    assert receipt.verify(expected_policy_hash="0" * 64) is False
    assert receipt.verify(expected_policy_hash=POLICY_HASH) is True


def test_receipt_from_another_policy_hash_fails_verification() -> None:
    receipt = make_receipt()
    tampered = receipt.model_copy(update={"policy_hash": "f" * 64})
    assert tampered.verify() is False


# ---------------------------------------------------------------------- orphans


def test_orphan_enter_is_refused_by_default() -> None:
    ledger = DecisionLedger()
    decision = make_decision(opportunity_id=None)
    with pytest.raises(OrphanDecision):
        ledger.append(decision, inputs(("obs-1", NS - 1_000)))
    assert len(ledger) == 0
    assert ledger.orphans() == ()


def test_orphan_detection_audits_explicitly_admitted_decisions() -> None:
    ledger = DecisionLedger()
    decision = make_decision(opportunity_id=None)
    receipt = ledger.append(decision, inputs(("obs-1", NS - 1_000)), allow_orphans=True)
    assert ledger.orphans() == (receipt.decision_id,)
    assert ledger.verify(receipt) is True


def test_wait_without_opportunity_is_not_an_orphan() -> None:
    ledger = DecisionLedger()
    decision = make_decision(
        action=DecisionAction.WAIT,
        intent=IntentKind.NONE,
        opportunity_id=None,
        reason_codes=(ReasonCode.POLICY_WAIT,),
    )
    receipt = ledger.append(decision, inputs(("obs-1", NS - 1_000)))
    assert ledger.orphans() == ()
    assert is_orphan(receipt.action, receipt.opportunity_id) is False
    assert is_orphan(DecisionAction.EXIT, None) is True


def test_clean_ledger_reports_no_orphans() -> None:
    ledger = DecisionLedger()
    for decision in (
        make_decision(),
        make_decision(
            action=DecisionAction.ABSTAIN,
            intent=IntentKind.NONE,
            reason_codes=(ReasonCode.BELIEF_STALE,),
        ),
    ):
        ledger.append(decision, inputs(("obs-1", NS - 1_000)))
    assert ledger.orphans() == ()


# ------------------------------------------------------------------------ shape


def test_receipt_rejects_unknown_fields() -> None:
    receipt = make_receipt()
    payload = receipt.model_dump()
    payload["fill_price"] = Decimal("1")
    with pytest.raises(ValueError):
        DecisionReceipt(**payload)


def test_decision_id_binding_is_checked() -> None:
    receipt = make_receipt()
    wrong = derive_decision_id(
        decision_time_ns=receipt.decision_time_ns,
        instrument_id=receipt.instrument_id,
        opportunity_id=receipt.opportunity_id,
        action=DecisionAction.EXIT,
        policy_version=receipt.policy_version,
    )
    tampered = receipt.model_copy(update={"decision_id": wrong})
    assert tampered.verify() is False


def test_receipts_module_has_no_execution_path() -> None:
    path = Path(decisions_module.__file__)
    tree = ast.parse(path.read_text())
    imports: set[str] = set()
    calls: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module)
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            calls.add(node.func.attr)
    assert not any("nautilus" in name for name in imports)
    assert not any("adapter" in name for name in imports)
    assert not (calls & {"submit", "submit_order", "place_order", "amend_order", "cancel_order"})


def test_reason_codes_are_copied_for_audit() -> None:
    decision = make_decision(reason_codes=(ReasonCode.OPPORTUNITY_ARMED, ReasonCode.POSITION_OPEN))
    receipt = make_receipt(decision)
    assert receipt.reason_codes == decision.reason_codes
    assert isinstance(receipt.reason_codes, tuple)


def test_actions_without_intent_are_recorded_as_such() -> None:
    ledger = DecisionLedger()
    decision = make_decision(
        action=DecisionAction.ABSTAIN, intent=IntentKind.NONE, reason_codes=(ReasonCode.NO_EDGE,)
    )
    receipt = ledger.append(decision, inputs(("obs-1", NS - 1_000)))
    assert receipt.proposed_intent is IntentKind.NONE
    assert receipt.verify() is True


def test_empty_inputs_are_allowed_for_evidence_free_waits() -> None:
    receipt = make_receipt(
        make_decision(action=DecisionAction.WAIT, intent=IntentKind.NONE, reason_codes=()),
        inputs(),
    )
    assert receipt.observation_ids == ()
    assert receipt.max_input_available_at_ns is None
    assert receipt.verify() is True


def test_receipt_requires_an_instrument_and_time() -> None:
    payload: dict[str, Any] = make_receipt().model_dump()
    for field, bad in (("instrument_id", ""), ("decision_time_ns", -1)):
        mutated = dict(payload)
        mutated[field] = bad
        with pytest.raises(ValueError):
            DecisionReceipt(**mutated)


# ------------------------------------------------------- policy provenance bridge


def test_policy_constraints_are_receipted_and_replayable() -> None:
    request = DecisionRequest(
        decision_time_ns=NS,
        instrument_id=INSTRUMENT,
        venue_status=VenueOrderStatus.REJECTED,
        position=PositionState(presence=PositionPresence.NONE),
        portfolio=PortfolioState(),
        opportunity=None,
        belief=None,
        expert_stances=(),
    )
    policy = TraderDecisionPolicy()
    decision = policy.evaluate(request)
    explanation = policy.explain(request)
    receipt_inputs = inputs(("obs-1", NS - 1_000), constraints=explanation.constraints)

    ledger = DecisionLedger()
    receipt = ledger.append(decision, receipt_inputs)
    replay = DecisionLedger().append(decision, receipt_inputs)

    assert receipt.constraints == explanation.constraints
    assert {"BELIEF_UNUSABLE", "OPPORTUNITY_MISSING"} <= set(receipt.constraints)
    assert receipt.action is DecisionAction.ABSTAIN
    assert receipt.reason_codes == explanation.reason_codes
    assert receipt.policy_hash == explanation.policy_hash == POLICY_HASH
    assert receipt.digest == replay.digest
    assert ledger.verify(receipt) is True
