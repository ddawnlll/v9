"""Typed advisory trader decisions: a vocabulary, never an order.

This module is deliberately inert. A :class:`TraderDecision` records what the
policy would *like* to do and why; it carries no side, quantity, price or
order-type field, and ``extra="forbid"`` rejects order-shaped payloads. The
``advisory_only`` flag is pinned to ``True`` so no downstream consumer can
mistake a decision for risk permission or venue authority.

The enums keep five distinct vocabularies apart:

* :class:`DecisionAction` -- the decision plane's action (WAIT/ENTER/...).
* :class:`IntentKind` -- what an execution layer *would* express, if allowed.
* :class:`OpportunityLifecycle` -- lifecycle of a sovereign opportunity.
* :class:`VenueOrderStatus` -- native venue state, never a decision action.
* :class:`ReasonCode` -- stable, replayable explanations.

``decision_id`` is a replay identity produced by :func:`make_decision_id`; it
never reads a wall clock.
"""

from __future__ import annotations

import hashlib
import json
from enum import StrEnum
from typing import Any, Final, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    model_validator,
)

__all__ = [
    "DecisionAction",
    "IntentKind",
    "OpportunityLifecycle",
    "ReasonCode",
    "SCHEMA_VERSION",
    "TraderDecision",
    "VenueOrderStatus",
    "make_decision_id",
]

#: Receipt schema emitted by :meth:`TraderDecision.to_receipt`.
SCHEMA_VERSION: Final[str] = "trader-decision-v1"

#: Actions the decision plane may emit. ``UNKNOWN`` is not one of them.
class DecisionAction(StrEnum):
    WAIT = "WAIT"
    ENTER = "ENTER"
    HOLD = "HOLD"
    REDUCE = "REDUCE"
    EXIT = "EXIT"
    ABSTAIN = "ABSTAIN"


#: Possible execution intents. ``NONE`` means "no execution intent at all".
class IntentKind(StrEnum):
    NONE = "NONE"
    MARKET_ENTRY = "MARKET_ENTRY"
    LIMIT_ENTRY = "LIMIT_ENTRY"
    PROTECTIVE = "PROTECTIVE"
    REDUCE = "REDUCE"
    EXIT = "EXIT"
    CANCEL = "CANCEL"


#: Lifecycle of the sovereign opportunity a decision may reference.
class OpportunityLifecycle(StrEnum):
    WATCHING = "WATCHING"
    ARMED = "ARMED"
    ADMITTED = "ADMITTED"
    INVALIDATED = "INVALIDATED"
    EXPIRED = "EXPIRED"


#: Native venue order state. This vocabulary is *not* a decision vocabulary.
class VenueOrderStatus(StrEnum):
    SUBMITTED = "SUBMITTED"
    ACCEPTED = "ACCEPTED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELED = "CANCELED"
    REJECTED = "REJECTED"
    UNKNOWN = "UNKNOWN"


#: Stable reason codes. Values are part of the persisted contract: never
#: rename or recycle one; add a new code instead.
class ReasonCode(StrEnum):
    BELIEF_STALE = "BELIEF_STALE"
    VENUE_UNKNOWN = "VENUE_UNKNOWN"
    RISK_DENIED = "RISK_DENIED"
    NO_OPPORTUNITY = "NO_OPPORTUNITY"
    OPPORTUNITY_EXPIRED = "OPPORTUNITY_EXPIRED"
    OPPORTUNITY_INVALIDATED = "OPPORTUNITY_INVALIDATED"
    POLICY_ABSTAIN = "POLICY_ABSTAIN"
    CONFIDENCE_INSUFFICIENT = "CONFIDENCE_INSUFFICIENT"
    PROTECTIVE_REQUIRED = "PROTECTIVE_REQUIRED"
    PROTECTIVE_MISSING = "PROTECTIVE_MISSING"
    POSITION_OPEN = "POSITION_OPEN"
    POSITION_NONE = "POSITION_NONE"
    INTENT_PENDING = "INTENT_PENDING"
    DUPLICATE_INTENT = "DUPLICATE_INTENT"
    DATA_MISSING = "DATA_MISSING"
    DATA_CONTRADICTORY = "DATA_CONTRADICTORY"
    EXIT_REQUIRED = "EXIT_REQUIRED"
    HOLD_THESIS_STABLE = "HOLD_THESIS_STABLE"
    ENTRY_PRICE_UNAVAILABLE = "ENTRY_PRICE_UNAVAILABLE"


_NONE_ONLY_ACTIONS = frozenset({DecisionAction.WAIT, DecisionAction.ABSTAIN})
_INTENT_ACTIONS = frozenset(
    {DecisionAction.ENTER, DecisionAction.HOLD, DecisionAction.REDUCE, DecisionAction.EXIT}
)


def make_decision_id(
    decision_time_ns: int,
    instrument_id: str,
    opportunity_id: str | None,
    action: DecisionAction | str,
    intent_kind: IntentKind | str,
    policy_version: str,
) -> str:
    """Return a deterministic decision id for the supplied identity fields.

    The payload is a canonical JSON list with no whitespace, digested with
    SHA-256. No clock, randomness, process state or environment is consulted:
    identical inputs always mint identical ids, and any changed field changes
    the id.
    """
    payload = [
        decision_time_ns,
        instrument_id,
        opportunity_id,
        DecisionAction(action).value,
        IntentKind(intent_kind).value,
        policy_version,
    ]
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class TraderDecision(BaseModel):
    """Immutable, advisory-only decision record.

    Validation is intentionally strict: unknown fields, empty identities,
    contradictory action/intent pairs and duplicate reasons are rejected rather
    than coerced. See the module docstring for the authority boundary.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    decision_id: str = Field(min_length=1, description="Replay identity, never a venue id.")
    decision_time_ns: StrictInt = Field(ge=0, description="Decision timestamp in ns (>=0).")
    instrument_id: str = Field(min_length=1, description="Instrument the decision concerns.")
    opportunity_id: str | None = Field(
        default=None, description="Sovereign opportunity id; required for ENTER."
    )
    action: DecisionAction
    intent_kind: IntentKind
    reason_codes: tuple[ReasonCode, ...] = Field(
        min_length=1, description="Unique, non-empty explanation codes."
    )
    policy_version: str = Field(min_length=1, description="Policy that produced the decision.")
    belief_version_hash: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
        description="64 lowercase hex characters when present.",
    )
    advisory_only: StrictBool = Field(
        default=True, description="Pinned True: this contract never grants order authority."
    )

    @model_validator(mode="after")
    def _validate_contract(self) -> Self:
        if self.advisory_only is not True:
            raise ValueError(
                "advisory_only must be True: TraderDecision never grants order authority"
            )
        if len(set(self.reason_codes)) != len(self.reason_codes):
            raise ValueError("reason_codes must be unique")
        if self.intent_kind is IntentKind.NONE and self.action not in _NONE_ONLY_ACTIONS:
            raise ValueError(
                f"intent NONE is legal only for WAIT/ABSTAIN, got {self.action.value}"
            )
        if self.action is DecisionAction.ABSTAIN and self.intent_kind is not IntentKind.NONE:
            raise ValueError("ABSTAIN implies intent NONE")
        if self.intent_kind is not IntentKind.NONE and self.action not in _INTENT_ACTIONS:
            raise ValueError(
                f"intent {self.intent_kind.value} requires ENTER/HOLD/REDUCE/EXIT, "
                f"got {self.action.value}"
            )
        if self.action is DecisionAction.ENTER:
            if self.intent_kind is IntentKind.NONE:
                raise ValueError("ENTER requires a non-NONE execution intent")
            if self.opportunity_id is None:
                raise ValueError("ENTER requires a non-null opportunity_id")
        return self

    def to_receipt(self) -> dict[str, Any]:
        """Return a plain JSON-serializable receipt (schema ``trader-decision-v1``)."""
        return {
            "schema_version": SCHEMA_VERSION,
            "decision_id": self.decision_id,
            "decision_time_ns": self.decision_time_ns,
            "instrument_id": self.instrument_id,
            "opportunity_id": self.opportunity_id,
            "action": self.action.value,
            "intent_kind": self.intent_kind.value,
            "reason_codes": [code.value for code in self.reason_codes],
            "policy_version": self.policy_version,
            "belief_version_hash": self.belief_version_hash,
            "advisory_only": self.advisory_only,
        }
