"""M2-02: typed TraderDecision / IntentKind / ReasonCode contracts.

Schema-only: every assertion here is about vocabulary closure, replay identity
and rejection of mixed or order-shaped states. No execution path is exercised
or claimed.
"""

from __future__ import annotations

import inspect
import itertools
from typing import Any

import pytest
from pydantic import ValidationError

from v8_next.domain import decision as decision_module
from v8_next.domain.decision import (
    DecisionAction,
    IntentKind,
    OpportunityLifecycle,
    ReasonCode,
    TraderDecision,
    VenueOrderStatus,
    derive_decision_id,
    lifecycle_of,
    opportunity_status_of,
)
from v8_next.opportunities.models import OpportunityStatus

NS = 1_700_000_000_000_000_000
INSTRUMENT = "BTCUSDT-PERP.BINANCE"
OPPORTUNITY = "opp-0001"
POLICY = "policy-v1"
BELIEF = "belief-hash-abc"

WAIT_ACTIONS = frozenset({DecisionAction.WAIT, DecisionAction.ABSTAIN})
INTENT_ACTIONS = frozenset(
    {DecisionAction.ENTER, DecisionAction.HOLD, DecisionAction.REDUCE, DecisionAction.EXIT}
)


def make_decision(**overrides: Any) -> TraderDecision:
    base: dict[str, Any] = {
        "decision_time_ns": NS,
        "instrument_id": INSTRUMENT,
        "opportunity_id": OPPORTUNITY,
        "action": DecisionAction.ENTER,
        "intent_kind": IntentKind.LIMIT_ENTRY,
        "policy_version": POLICY,
        "reason_codes": (ReasonCode.OPPORTUNITY_ARMED,),
    }
    base.update(overrides)
    action = base["action"]
    if "decision_id" not in base:
        base["decision_id"] = (
            derive_decision_id(
                decision_time_ns=base["decision_time_ns"],
                instrument_id=base["instrument_id"],
                opportunity_id=base["opportunity_id"],
                action=action,
                policy_version=base["policy_version"],
            )
            if isinstance(action, DecisionAction)
            else "0" * 64
        )
    return TraderDecision(**base)


# --------------------------------------------------------------------------- vocabulary


def test_action_vocabulary_is_closed_and_verb_only() -> None:
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
    assert {status.value for status in VenueOrderStatus} == {
        "SUBMITTED",
        "ACCEPTED",
        "PARTIALLY_FILLED",
        "FILLED",
        "CANCELED",
        "REJECTED",
        "UNKNOWN",
    }


def test_reason_codes_are_stable_string_codes() -> None:
    for code in ReasonCode:
        assert code.value == code.name
    assert {
        ReasonCode.BELIEF_STALE,
        ReasonCode.VENUE_UNKNOWN,
        ReasonCode.RISK_DENIED,
    } <= set(ReasonCode)


def test_unknown_venue_state_is_rejected_not_coerced() -> None:
    with pytest.raises(ValueError):
        VenueOrderStatus("PENDING_NEW")
    with pytest.raises(ValueError):
        VenueOrderStatus("submitted")
    assert VenueOrderStatus.UNKNOWN.value == "UNKNOWN"


def test_opportunity_lifecycle_maps_onto_the_book_status() -> None:
    assert set(OpportunityLifecycle) == {
        OpportunityLifecycle.WATCHING,
        OpportunityLifecycle.ARMED,
        OpportunityLifecycle.ADMITTED,
        OpportunityLifecycle.INVALIDATED,
        OpportunityLifecycle.EXPIRED,
    }
    for status in OpportunityStatus:
        assert opportunity_status_of(lifecycle_of(status)) is status
    for lifecycle in OpportunityLifecycle:
        assert lifecycle_of(opportunity_status_of(lifecycle)) is lifecycle
    assert lifecycle_of(OpportunityStatus.CANDIDATE) is OpportunityLifecycle.WATCHING
    assert lifecycle_of(OpportunityStatus.CONFIRMED) is OpportunityLifecycle.ARMED
    assert decision_module.TERMINAL_LIFECYCLES == {
        OpportunityLifecycle.INVALIDATED,
        OpportunityLifecycle.EXPIRED,
    }


# ------------------------------------------------------------------- mixed-state rejection


@pytest.mark.parametrize("action", sorted(WAIT_ACTIONS))
def test_none_intent_is_legal_for_passive_actions(action: DecisionAction) -> None:
    decision = make_decision(action=action, intent_kind=IntentKind.NONE, reason_codes=())
    assert decision.intent_kind is IntentKind.NONE
    assert decision.action is action


@pytest.mark.parametrize("action", sorted(INTENT_ACTIONS))
def test_none_intent_rejects_active_actions(action: DecisionAction) -> None:
    with pytest.raises(ValidationError):
        make_decision(action=action, intent_kind=IntentKind.NONE)


@pytest.mark.parametrize("intent", [i for i in IntentKind if i is not IntentKind.NONE])
@pytest.mark.parametrize("action", sorted(WAIT_ACTIONS))
def test_active_intent_rejects_passive_actions(intent: IntentKind, action: DecisionAction) -> None:
    with pytest.raises(ValidationError):
        make_decision(action=action, intent_kind=intent)


@pytest.mark.parametrize("intent", [i for i in IntentKind if i is not IntentKind.NONE])
@pytest.mark.parametrize("action", sorted(INTENT_ACTIONS))
def test_active_intent_accepts_active_actions(intent: IntentKind, action: DecisionAction) -> None:
    assert make_decision(action=action, intent_kind=intent).intent_kind is intent


def test_every_action_intent_pair_has_exactly_one_legal_combination() -> None:
    for intent, action in itertools.product(IntentKind, DecisionAction):
        legal = (intent is IntentKind.NONE and action in WAIT_ACTIONS) or (
            intent is not IntentKind.NONE and action in INTENT_ACTIONS
        )
        try:
            make_decision(action=action, intent_kind=intent)
        except ValidationError:
            assert not legal, f"{intent.value}/{action.value} should have been accepted"
        else:
            assert legal, f"{intent.value}/{action.value} should have been rejected"


# ------------------------------------------------------------------ advisory-only pinning


def test_advisory_only_defaults_true() -> None:
    assert make_decision().advisory_only is True


@pytest.mark.parametrize("value", [False, 0, 1, "true", None])
def test_advisory_only_cannot_be_anything_but_true(value: object) -> None:
    with pytest.raises((ValidationError, TypeError)):
        make_decision(advisory_only=value)


# ------------------------------------------------------------------- no venue order surface


ORDER_SHAPED_FIELDS = (
    "quantity",
    "price",
    "side",
    "order_type",
    "client_order_id",
    "venue_order_id",
    "notional",
    "leverage",
    "time_in_force",
)


@pytest.mark.parametrize("field", ORDER_SHAPED_FIELDS)
def test_order_shaped_extras_are_rejected(field: str) -> None:
    with pytest.raises(ValidationError):
        make_decision(**{field: 1})


def test_schema_carries_no_order_authority() -> None:
    assert set(TraderDecision.model_fields) == {
        "decision_id",
        "decision_time_ns",
        "instrument_id",
        "opportunity_id",
        "action",
        "intent_kind",
        "reason_codes",
        "policy_version",
        "belief_version_hash",
        "advisory_only",
    }
    assert TraderDecision.model_config["extra"] == "forbid"
    assert TraderDecision.model_config["frozen"] is True
    payload = make_decision().model_dump()
    assert not set(payload) & set(ORDER_SHAPED_FIELDS)


def test_module_exposes_no_order_side_effect_path() -> None:
    public = [name for name in vars(decision_module) if not name.startswith("_")]
    forbidden = ("submit", "place", "cancel_order", "amend", "send_order")
    assert not [
        name
        for name in public
        if any(token in name.lower() for token in forbidden)
        and callable(getattr(decision_module, name))
    ]
    for name in public:
        member = getattr(decision_module, name)
        if inspect.isclass(member) and issubclass(member, TraderDecision):
            assert not hasattr(member, "submit")
            assert not hasattr(member, "execute")


# ------------------------------------------------------------------------ determinism


def test_decision_id_is_deterministic_across_replays() -> None:
    first = make_decision()
    second = make_decision()
    assert first.decision_id == second.decision_id
    assert len(first.decision_id) == 64
    assert first.decision_id == derive_decision_id(
        decision_time_ns=NS,
        instrument_id=INSTRUMENT,
        opportunity_id=OPPORTUNITY,
        action=DecisionAction.ENTER,
        policy_version=POLICY,
    )


@pytest.mark.parametrize(
    "overrides",
    [
        {"decision_time_ns": NS + 1},
        {"instrument_id": "ETHUSDT-PERP.BINANCE"},
        {"opportunity_id": "opp-0002"},
        {"opportunity_id": None},
        {"action": DecisionAction.HOLD, "intent_kind": IntentKind.PROTECTIVE},
        {"policy_version": "policy-v2"},
    ],
)
def test_decision_id_changes_with_every_identity_field(overrides: dict[str, Any]) -> None:
    assert make_decision().decision_id != make_decision(**overrides).decision_id


def test_non_identity_bookkeeping_does_not_change_decision_id() -> None:
    base = make_decision()
    varied = make_decision(
        belief_version_hash=BELIEF,
        reason_codes=(ReasonCode.OPPORTUNITY_ARMED, ReasonCode.RISK_DENIED),
    )
    assert base.decision_id == varied.decision_id


def test_decision_id_must_match_its_fields() -> None:
    with pytest.raises(ValidationError):
        make_decision(decision_id="0" * 64)
    with pytest.raises(ValidationError):
        make_decision(decision_id=make_decision(policy_version="policy-v2").decision_id)


def test_policy_version_is_preserved() -> None:
    assert make_decision(policy_version="policy-v9").policy_version == "policy-v9"


# ---------------------------------------------------------------------- reason canonicality


def test_duplicate_reason_codes_are_rejected() -> None:
    with pytest.raises(ValidationError):
        make_decision(reason_codes=(ReasonCode.RISK_DENIED, ReasonCode.RISK_DENIED))


def test_unsorted_reason_codes_are_rejected() -> None:
    with pytest.raises(ValidationError):
        make_decision(reason_codes=(ReasonCode.VENUE_UNKNOWN, ReasonCode.BELIEF_STALE))


def test_create_canonicalises_reason_order_and_duplicates() -> None:
    first = TraderDecision.create(
        decision_time_ns=NS,
        instrument_id=INSTRUMENT,
        opportunity_id=OPPORTUNITY,
        action=DecisionAction.ENTER,
        intent_kind=IntentKind.LIMIT_ENTRY,
        policy_version=POLICY,
        reason_codes=(ReasonCode.VENUE_UNKNOWN, ReasonCode.BELIEF_STALE, ReasonCode.BELIEF_STALE),
    )
    second = TraderDecision.create(
        decision_time_ns=NS,
        instrument_id=INSTRUMENT,
        opportunity_id=OPPORTUNITY,
        action=DecisionAction.ENTER,
        intent_kind=IntentKind.LIMIT_ENTRY,
        policy_version=POLICY,
        reason_codes=(ReasonCode.BELIEF_STALE, ReasonCode.VENUE_UNKNOWN),
    )
    assert first.reason_codes == (ReasonCode.BELIEF_STALE, ReasonCode.VENUE_UNKNOWN)
    assert first == second


# ------------------------------------------------------------------------- schema roundtrip


def test_schema_roundtrip_python() -> None:
    decision = make_decision(belief_version_hash=BELIEF)
    assert TraderDecision.model_validate(decision.model_dump()) == decision
    dumped = decision.model_dump(mode="json")
    assert dumped["action"] == "ENTER"
    assert dumped["advisory_only"] is True
    assert TraderDecision.model_validate(dumped) == decision


def test_schema_roundtrip_json() -> None:
    decision = make_decision(belief_version_hash=None)
    assert TraderDecision.model_validate_json(decision.model_dump_json()) == decision


def test_invalid_field_values_are_rejected() -> None:
    with pytest.raises((ValidationError, TypeError)):
        make_decision(decision_time_ns=-1)
    with pytest.raises((ValidationError, TypeError)):
        make_decision(decision_time_ns="1700000000000000000")
    with pytest.raises(ValidationError):
        make_decision(instrument_id="")
    with pytest.raises(ValidationError):
        make_decision(policy_version=" ")
    with pytest.raises(ValidationError):
        make_decision(belief_version_hash="")
    with pytest.raises(ValidationError):
        make_decision(action="NOT_AN_ACTION")
