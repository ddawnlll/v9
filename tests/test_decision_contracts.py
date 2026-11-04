"""M2-02: typed TraderDecision / intent / venue / reason contracts.

These tests pin vocabulary closure, rejection of mixed or order-shaped states,
deterministic replay identity and byte-stable JSON roundtrips. Nothing here
exercises an execution path: a ``TraderDecision`` is advisory data only.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

import pytest
from pydantic import ValidationError

from v8_next.domain.decision import (
    SCHEMA_VERSION,
    DecisionAction,
    IntentKind,
    OpportunityLifecycle,
    ReasonCode,
    TraderDecision,
    VenueOrderStatus,
    make_decision_id,
)

HEX64 = "a" * 64
_EXECUTION_INTENTS = tuple(
    intent for intent in IntentKind if intent is not IntentKind.NONE
)
_EXECUTION_ACTIONS = (
    DecisionAction.ENTER,
    DecisionAction.HOLD,
    DecisionAction.REDUCE,
    DecisionAction.EXIT,
)
_BASE: dict[str, Any] = {
    "decision_id": "d" * 64,
    "decision_time_ns": 1_700_000_000_000_000_000,
    "instrument_id": "BTCUSDT-PERP.BINANCE",
    "opportunity_id": "opp-1",
    "action": DecisionAction.ENTER,
    "intent_kind": IntentKind.MARKET_ENTRY,
    "reason_codes": (ReasonCode.BELIEF_STALE,),
    "policy_version": "policy-v1",
    "belief_version_hash": HEX64,
}


def make_decision(**overrides: Any) -> TraderDecision:
    fields = dict(_BASE)
    fields.update(overrides)
    return TraderDecision(**fields)


# ---------------------------------------------------------------------------
# Vocabulary closure
# ---------------------------------------------------------------------------


def test_vocabularies_are_closed_str_enums() -> None:
    assert {action.value for action in DecisionAction} == {
        "WAIT",
        "ENTER",
        "HOLD",
        "REDUCE",
        "EXIT",
        "ABSTAIN",
    }
    assert {intent.value for intent in IntentKind} == {
        "NONE",
        "MARKET_ENTRY",
        "LIMIT_ENTRY",
        "PROTECTIVE",
        "REDUCE",
        "EXIT",
        "CANCEL",
    }
    assert {state.value for state in OpportunityLifecycle} == {
        "WATCHING",
        "ARMED",
        "ADMITTED",
        "INVALIDATED",
        "EXPIRED",
    }
    assert {status.value for status in VenueOrderStatus} == {
        "SUBMITTED",
        "ACCEPTED",
        "PARTIALLY_FILLED",
        "FILLED",
        "CANCELED",
        "REJECTED",
        "UNKNOWN",
    }
    assert isinstance(DecisionAction.WAIT, str)


def test_reason_codes_are_a_stable_superset_of_the_contract() -> None:
    required = {
        "BELIEF_STALE",
        "VENUE_UNKNOWN",
        "RISK_DENIED",
        "NO_OPPORTUNITY",
        "OPPORTUNITY_EXPIRED",
        "OPPORTUNITY_INVALIDATED",
        "POLICY_ABSTAIN",
        "CONFIDENCE_INSUFFICIENT",
        "PROTECTIVE_REQUIRED",
        "PROTECTIVE_MISSING",
        "POSITION_OPEN",
        "POSITION_NONE",
        "INTENT_PENDING",
        "DUPLICATE_INTENT",
        "DATA_MISSING",
        "DATA_CONTRADICTORY",
        "EXIT_REQUIRED",
        "HOLD_THESIS_STABLE",
        "ENTRY_PRICE_UNAVAILABLE",
    }
    assert required <= {code.value for code in ReasonCode}


def test_venue_status_is_not_a_decision_action() -> None:
    assert VenueOrderStatus.UNKNOWN.value not in {action.value for action in DecisionAction}
    assert not set(DecisionAction) & set(VenueOrderStatus)
    with pytest.raises(ValueError):
        DecisionAction(VenueOrderStatus.UNKNOWN.value)
    with pytest.raises(ValidationError):
        make_decision(action=VenueOrderStatus.UNKNOWN)


# ---------------------------------------------------------------------------
# Legal combinations
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("action", _EXECUTION_ACTIONS)
@pytest.mark.parametrize("intent", _EXECUTION_INTENTS)
def test_every_execution_action_intent_pair_is_legal(
    action: DecisionAction, intent: IntentKind
) -> None:
    decision = make_decision(action=action, intent_kind=intent)
    assert decision.action is action
    assert decision.intent_kind is intent


@pytest.mark.parametrize("action", [DecisionAction.WAIT, DecisionAction.ABSTAIN])
def test_none_intent_is_legal_for_wait_and_abstain(action: DecisionAction) -> None:
    decision = make_decision(
        action=action,
        intent_kind=IntentKind.NONE,
        opportunity_id=None,
        reason_codes=(ReasonCode.NO_OPPORTUNITY, ReasonCode.POLICY_ABSTAIN),
    )
    assert decision.intent_kind is IntentKind.NONE
    assert decision.opportunity_id is None


def test_wide_reason_tuple_preserves_order() -> None:
    codes = (ReasonCode.POSITION_OPEN, ReasonCode.HOLD_THESIS_STABLE, ReasonCode.VENUE_UNKNOWN)
    decision = make_decision(
        action=DecisionAction.HOLD,
        intent_kind=IntentKind.PROTECTIVE,
        reason_codes=codes,
    )
    assert decision.reason_codes == codes
    assert isinstance(decision.reason_codes, tuple)


def test_string_inputs_are_accepted_as_enum_members() -> None:
    decision = make_decision(action="HOLD", intent_kind="PROTECTIVE", reason_codes=["POSITION_OPEN"])
    assert decision.action is DecisionAction.HOLD
    assert decision.intent_kind is IntentKind.PROTECTIVE
    assert decision.reason_codes == (ReasonCode.POSITION_OPEN,)


# ---------------------------------------------------------------------------
# Rejection rules
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "action",
    [DecisionAction.ENTER, DecisionAction.HOLD, DecisionAction.REDUCE, DecisionAction.EXIT],
)
def test_none_intent_is_rejected_outside_wait_abstain(action: DecisionAction) -> None:
    with pytest.raises(ValidationError):
        make_decision(action=action, intent_kind=IntentKind.NONE)


@pytest.mark.parametrize("intent", _EXECUTION_INTENTS)
def test_wait_rejects_execution_intents(intent: IntentKind) -> None:
    with pytest.raises(ValidationError):
        make_decision(action=DecisionAction.WAIT, intent_kind=intent)


@pytest.mark.parametrize("intent", _EXECUTION_INTENTS)
def test_abstain_implies_none_intent(intent: IntentKind) -> None:
    with pytest.raises(ValidationError):
        make_decision(action=DecisionAction.ABSTAIN, intent_kind=intent)


@pytest.mark.parametrize("intent", _EXECUTION_INTENTS)
def test_enter_requires_non_null_opportunity(intent: IntentKind) -> None:
    with pytest.raises(ValidationError):
        make_decision(action=DecisionAction.ENTER, intent_kind=intent, opportunity_id=None)


def test_enter_rejects_none_intent() -> None:
    with pytest.raises(ValidationError):
        make_decision(
            action=DecisionAction.ENTER,
            intent_kind=IntentKind.NONE,
            opportunity_id="opp-1",
        )


def test_duplicate_reason_codes_are_rejected() -> None:
    with pytest.raises(ValidationError):
        make_decision(
            reason_codes=(ReasonCode.BELIEF_STALE, ReasonCode.BELIEF_STALE),
        )
    with pytest.raises(ValidationError):
        make_decision(reason_codes=("BELIEF_STALE", ReasonCode.BELIEF_STALE))


@pytest.mark.parametrize("reason_codes", [(), []])
def test_empty_reason_codes_are_rejected(reason_codes: Any) -> None:
    with pytest.raises(ValidationError):
        make_decision(reason_codes=reason_codes)


def test_advisory_only_false_is_rejected() -> None:
    with pytest.raises(ValidationError):
        make_decision(advisory_only=False)
    with pytest.raises(ValidationError):
        make_decision(advisory_only=1)


@pytest.mark.parametrize("value", [-1, -1_000_000])
def test_negative_decision_time_is_rejected(value: int) -> None:
    with pytest.raises(ValidationError):
        make_decision(decision_time_ns=value)


@pytest.mark.parametrize("value", ["1", 1.0, True, None])
def test_decision_time_is_strictly_an_integer(value: Any) -> None:
    with pytest.raises(ValidationError):
        make_decision(decision_time_ns=value)


@pytest.mark.parametrize(
    "belief_hash",
    [
        "A" * 64,
        "a" * 63,
        "a" * 65,
        "g" * 64,
        "0x" + "a" * 62,
        "",
    ],
)
def test_belief_hash_must_be_64_lowercase_hex(belief_hash: str) -> None:
    with pytest.raises(ValidationError):
        make_decision(belief_version_hash=belief_hash)


def test_belief_hash_may_be_absent() -> None:
    assert make_decision(belief_version_hash=None).belief_version_hash is None


@pytest.mark.parametrize(
    "field",
    ["decision_id", "instrument_id", "policy_version"],
)
def test_identity_strings_must_be_non_empty(field: str) -> None:
    with pytest.raises(ValidationError):
        make_decision(**{field: ""})


def test_unknown_fields_are_forbidden() -> None:
    with pytest.raises(ValidationError):
        make_decision(quantity=1)


@pytest.mark.parametrize("field,value", [("action", "UNKNOWN"), ("intent_kind", "MAYBE")])
def test_unknown_vocabulary_values_are_rejected(field: str, value: str) -> None:
    with pytest.raises(ValidationError):
        make_decision(**{field: value})


def test_decision_is_frozen() -> None:
    decision = make_decision()
    with pytest.raises(ValidationError):
        decision.instrument_id = "ETHUSDT-PERP.BINANCE"


# ---------------------------------------------------------------------------
# JSON roundtrip and receipts
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "overrides",
    [
        {},
        {
            "action": DecisionAction.WAIT,
            "intent_kind": IntentKind.NONE,
            "opportunity_id": None,
            "belief_version_hash": None,
            "reason_codes": (ReasonCode.NO_OPPORTUNITY,),
        },
        {
            "action": DecisionAction.ABSTAIN,
            "intent_kind": IntentKind.NONE,
            "reason_codes": (ReasonCode.POLICY_ABSTAIN, ReasonCode.DATA_MISSING),
        },
        {
            "action": DecisionAction.EXIT,
            "intent_kind": IntentKind.EXIT,
            "reason_codes": (ReasonCode.EXIT_REQUIRED,),
        },
    ],
)
def test_json_roundtrip_is_byte_stable_and_preserves_tuples(
    overrides: dict[str, Any],
) -> None:
    decision = make_decision(**overrides)
    encoded = decision.model_dump_json()
    restored = TraderDecision.model_validate_json(encoded)
    assert restored == decision
    assert isinstance(restored.reason_codes, tuple)
    assert all(isinstance(code, ReasonCode) for code in restored.reason_codes)
    assert restored.model_dump_json() == encoded
    assert isinstance(json.loads(encoded)["reason_codes"], list)


def test_receipt_is_plain_json_with_schema_version() -> None:
    decision = make_decision()
    receipt = decision.to_receipt()
    assert receipt["schema_version"] == SCHEMA_VERSION == "trader-decision-v1"
    assert receipt["action"] == "ENTER"
    assert receipt["intent_kind"] == "MARKET_ENTRY"
    assert receipt["reason_codes"] == ["BELIEF_STALE"]
    assert receipt["advisory_only"] is True
    # Plain types only: roundtrips through the stdlib encoder without helpers.
    assert json.loads(json.dumps(receipt)) == receipt


# ---------------------------------------------------------------------------
# Deterministic decision ids
# ---------------------------------------------------------------------------


def test_decision_id_is_deterministic_and_matches_canonical_digest() -> None:
    args = (1_700_000_000_000_000_000, "BTCUSDT-PERP.BINANCE", "opp-1")
    first = make_decision_id(
        *args,
        DecisionAction.ENTER,
        IntentKind.MARKET_ENTRY,
        "policy-v1",
    )
    second = make_decision_id(
        *args,
        DecisionAction.ENTER,
        IntentKind.MARKET_ENTRY,
        "policy-v1",
    )
    assert first == second
    assert len(first) == 64
    assert first == first.lower()
    int(first, 16)

    payload = [
        1_700_000_000_000_000_000,
        "BTCUSDT-PERP.BINANCE",
        "opp-1",
        "ENTER",
        "MARKET_ENTRY",
        "policy-v1",
    ]
    canonical = json.dumps(payload, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    assert first == hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@pytest.mark.parametrize(
    "position,replacement",
    [
        (0, 1_700_000_000_000_000_001),
        (1, "ETHUSDT-PERP.BINANCE"),
        (2, "opp-2"),
        (3, DecisionAction.HOLD),
        (4, IntentKind.PROTECTIVE),
        (5, "policy-v2"),
    ],
)
def test_any_changed_identity_field_changes_decision_id(
    position: int, replacement: object
) -> None:
    base: list[Any] = [
        1_700_000_000_000_000_000,
        "BTCUSDT-PERP.BINANCE",
        "opp-1",
        DecisionAction.ENTER,
        IntentKind.MARKET_ENTRY,
        "policy-v1",
    ]
    changed = list(base)
    changed[position] = replacement
    assert make_decision_id(*base) != make_decision_id(*changed)  # type: ignore[arg-type]


def test_none_opportunity_is_encoded_as_json_null() -> None:
    decision_id = make_decision_id(
        0,
        "BTCUSDT-PERP.BINANCE",
        None,
        DecisionAction.WAIT,
        IntentKind.NONE,
        "policy-v1",
    )
    canonical = json.dumps(
        [0, "BTCUSDT-PERP.BINANCE", None, "WAIT", "NONE", "policy-v1"],
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    )
    assert decision_id == hashlib.sha256(canonical.encode("utf-8")).hexdigest()
