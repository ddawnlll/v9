"""Typed advisory trader decisions — a schema, never an order.

Three authorities are deliberately kept apart:

* **Advisory decision** (this module). ``TraderDecision`` says what the policy
  would *like* to do, why, and under which policy version. It carries no
  quantity, price, side or order-type field, so it cannot be submitted to a
  venue even by accident (``extra="forbid"`` rejects order-shaped input).
* **Risk permission** (``v8_next.risk.admission``). A decision existing does
  not mean the risk plane allowed it; ``advisory_only`` is pinned ``True`` and
  cannot be turned off, so no consumer can mistake the record for permission.
* **Native order side effects** (execution adapters). Nothing in this module
  constructs, submits, cancels or amends an order; ``TraderDecision`` is a
  value, and its ``decision_id`` is a replay identity, not a venue identifier.

Vocabulary is shared with the repo rather than duplicated: opportunity
lifecycle states map onto :class:`v8_next.opportunities.models.OpportunityStatus`
(``CANDIDATE``/``CONFIRMED`` are the book's names for ``WATCHING``/``ARMED``),
and ``decision_id`` is built with the canonical digest primitive from
``v8_next.telemetry.identity`` so identical replays mint identical ids.
"""

from __future__ import annotations

from enum import StrEnum
from types import MappingProxyType
from typing import Final, Literal, Mapping, Self, Sequence

from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator

from v8_next.opportunities.models import OpportunityStatus
from v8_next.telemetry.identity import canonical_digest

__all__ = [
    "DecisionAction",
    "IntentKind",
    "OpportunityLifecycle",
    "ReasonCode",
    "TraderDecision",
    "VenueOrderStatus",
    "derive_decision_id",
    "lifecycle_of",
    "opportunity_status_of",
]


class DecisionAction(StrEnum):
    """What the advisory policy proposes. Closed set; no order verbs."""

    WAIT = "WAIT"
    ENTER = "ENTER"
    HOLD = "HOLD"
    REDUCE = "REDUCE"
    EXIT = "EXIT"
    ABSTAIN = "ABSTAIN"


class IntentKind(StrEnum):
    """The kind of intent a decision would translate into, if ever executed."""

    NONE = "NONE"
    MARKET_ENTRY = "MARKET_ENTRY"
    LIMIT_ENTRY = "LIMIT_ENTRY"
    PROTECTIVE = "PROTECTIVE"
    REDUCE = "REDUCE"
    EXIT = "EXIT"
    CANCEL = "CANCEL"


class OpportunityLifecycle(StrEnum):
    """Decision-side view of the opportunity book's lifecycle.

    Deliberately narrower than arbitrary strings: ``WATCHING``/``ARMED`` are the
    decision vocabulary for the book's ``CANDIDATE``/``CONFIRMED``; the mapping
    is total and bidirectional through :func:`lifecycle_of` and
    :func:`opportunity_status_of` so the two vocabularies cannot drift.
    """

    WATCHING = "WATCHING"
    ARMED = "ARMED"
    ADMITTED = "ADMITTED"
    INVALIDATED = "INVALIDATED"
    EXPIRED = "EXPIRED"


class VenueOrderStatus(StrEnum):
    """Native venue order status as observed, never as requested.

    ``UNKNOWN`` is the only member for "no reliable reading"; an unlisted
    status is a schema violation (constructing this enum with any other value
    raises), not an implicit unknown.
    """

    SUBMITTED = "SUBMITTED"
    ACCEPTED = "ACCEPTED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELED = "CANCELED"
    REJECTED = "REJECTED"
    UNKNOWN = "UNKNOWN"


class ReasonCode(StrEnum):
    """Stable decision reasons.

    Codes are part of the replay surface: names are the wire values and MUST
    NOT be renamed or reused with a different meaning. Adding a code is
    additive; removing one is a breaking policy-version change.
    """

    # Evidence quality.
    BELIEF_STALE = "BELIEF_STALE"
    BELIEF_MISSING = "BELIEF_MISSING"
    BELIEF_CONTRADICTORY = "BELIEF_CONTRADICTORY"
    EVIDENCE_CONFLICT = "EVIDENCE_CONFLICT"
    EVIDENCE_INSUFFICIENT = "EVIDENCE_INSUFFICIENT"
    # Opportunity lifecycle.
    OPPORTUNITY_ARMED = "OPPORTUNITY_ARMED"
    OPPORTUNITY_NOT_ARMED = "OPPORTUNITY_NOT_ARMED"
    OPPORTUNITY_INVALIDATED = "OPPORTUNITY_INVALIDATED"
    OPPORTUNITY_EXPIRED = "OPPORTUNITY_EXPIRED"
    OPPORTUNITY_DUPLICATE = "OPPORTUNITY_DUPLICATE"
    # Venue state.
    VENUE_UNKNOWN = "VENUE_UNKNOWN"
    VENUE_PENDING = "VENUE_PENDING"
    VENUE_REJECTED = "VENUE_REJECTED"
    # Risk permission (reported, never granted here).
    RISK_DENIED = "RISK_DENIED"
    RISK_UNKNOWN = "RISK_UNKNOWN"
    # Position presence.
    POSITION_OPEN = "POSITION_OPEN"
    POSITION_NONE = "POSITION_NONE"
    POSITION_UNCERTAIN = "POSITION_UNCERTAIN"
    POSITION_EXIT_REQUIRED = "POSITION_EXIT_REQUIRED"
    POSITION_REDUCE_REQUIRED = "POSITION_REDUCE_REQUIRED"
    # Policy outcomes.
    NO_EDGE = "NO_EDGE"
    SCALE_IN_DISABLED = "SCALE_IN_DISABLED"
    POLICY_WAIT = "POLICY_WAIT"
    INPUT_FUTURE = "INPUT_FUTURE"


#: Actions that carry no intent at all.
_NO_INTENT_ACTIONS: Final[frozenset[DecisionAction]] = frozenset(
    {DecisionAction.WAIT, DecisionAction.ABSTAIN}
)
#: Actions that must name a concrete intent kind.
_INTENT_ACTIONS: Final[frozenset[DecisionAction]] = frozenset(
    {DecisionAction.ENTER, DecisionAction.HOLD, DecisionAction.REDUCE, DecisionAction.EXIT}
)

#: Opportunity lifecycle states from which no new entry can be armed.
TERMINAL_LIFECYCLES: Final[frozenset[OpportunityLifecycle]] = frozenset(
    {OpportunityLifecycle.INVALIDATED, OpportunityLifecycle.EXPIRED}
)

#: Total, bidirectional mapping between the decision vocabulary and the book's.
_LIFECYCLE_TO_STATUS: Final[Mapping[OpportunityLifecycle, OpportunityStatus]] = MappingProxyType(
    {
        OpportunityLifecycle.WATCHING: OpportunityStatus.CANDIDATE,
        OpportunityLifecycle.ARMED: OpportunityStatus.CONFIRMED,
        OpportunityLifecycle.ADMITTED: OpportunityStatus.ADMITTED,
        OpportunityLifecycle.INVALIDATED: OpportunityStatus.INVALIDATED,
        OpportunityLifecycle.EXPIRED: OpportunityStatus.EXPIRED,
    }
)
_STATUS_TO_LIFECYCLE: Final[Mapping[OpportunityStatus, OpportunityLifecycle]] = MappingProxyType(
    {status: lifecycle for lifecycle, status in _LIFECYCLE_TO_STATUS.items()}
)
if set(_STATUS_TO_LIFECYCLE) != set(OpportunityStatus):  # pragma: no cover - import guard
    raise AssertionError("opportunity lifecycle mapping must cover OpportunityStatus exactly")

#: Domain tag for decision ids; the Rust side pushes the same domain name.
DECISION_ID_TAG: Final[str] = "trader-decision-v1"


def lifecycle_of(status: OpportunityStatus) -> OpportunityLifecycle:
    """Decision-side lifecycle for a book status."""
    return _STATUS_TO_LIFECYCLE[status]


def opportunity_status_of(lifecycle: OpportunityLifecycle) -> OpportunityStatus:
    """Book status for a decision-side lifecycle."""
    return _LIFECYCLE_TO_STATUS[lifecycle]


def derive_decision_id(
    *,
    decision_time_ns: int,
    instrument_id: str,
    opportunity_id: str | None,
    action: DecisionAction,
    policy_version: str,
) -> str:
    """Replay identity of a decision.

    Only the fields named by the contract participate, in a fixed order, so a
    replayed decision with identical inputs mints an identical id regardless of
    reason-code ordering, belief version bookkeeping or wall-clock time.
    """
    return canonical_digest(
        DECISION_ID_TAG,
        [decision_time_ns, instrument_id, opportunity_id, action.value, policy_version],
    )


class TraderDecision(BaseModel):
    """Frozen advisory decision record.

    Construction rejects mixed action/intent states, a non-advisory record, an
    unknown venue state and any order-shaped extra field. ``decision_id`` must
    equal :func:`derive_decision_id` for the record's own fields.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    decision_id: str
    decision_time_ns: StrictInt = Field(ge=0)
    instrument_id: str
    opportunity_id: str | None = None
    action: DecisionAction
    intent_kind: IntentKind
    reason_codes: tuple[ReasonCode, ...] = ()
    policy_version: str
    belief_version_hash: str | None = None
    advisory_only: Literal[True] = True

    @field_validator("advisory_only", mode="before")
    @classmethod
    def _refuse_advisory_override(cls, value: object) -> bool:
        if value is not True:
            raise ValueError("TRADER_DECISION: advisory_only is fixed True")
        return True

    @model_validator(mode="after")
    def _validate_contract(self) -> Self:
        if not self.instrument_id.strip():
            raise ValueError("TRADER_DECISION: instrument_id cannot be empty")
        if not self.policy_version.strip():
            raise ValueError("TRADER_DECISION: policy_version cannot be empty")
        if not self.decision_id.strip():
            raise ValueError("TRADER_DECISION: decision_id cannot be empty")
        if self.belief_version_hash is not None and not self.belief_version_hash.strip():
            raise ValueError("TRADER_DECISION: belief_version_hash cannot be blank")
        if self.intent_kind is IntentKind.NONE:
            if self.action not in _NO_INTENT_ACTIONS:
                raise ValueError(
                    "TRADER_DECISION: NONE intent is legal only for WAIT/ABSTAIN, "
                    f"got {self.action.value}"
                )
        elif self.action not in _INTENT_ACTIONS:
            raise ValueError(
                f"TRADER_DECISION: intent {self.intent_kind.value} requires one of "
                f"ENTER/HOLD/REDUCE/EXIT, got {self.action.value}"
            )
        if len(set(self.reason_codes)) != len(self.reason_codes):
            raise ValueError("TRADER_DECISION: duplicate reason codes")
        if tuple(sorted(self.reason_codes)) != self.reason_codes:
            raise ValueError("TRADER_DECISION: reason codes must be in canonical order")
        expected = derive_decision_id(
            decision_time_ns=self.decision_time_ns,
            instrument_id=self.instrument_id,
            opportunity_id=self.opportunity_id,
            action=self.action,
            policy_version=self.policy_version,
        )
        if self.decision_id != expected:
            raise ValueError(
                "TRADER_DECISION: decision_id is not the deterministic id for these fields"
            )
        return self

    @classmethod
    def create(
        cls,
        *,
        decision_time_ns: int,
        instrument_id: str,
        opportunity_id: str | None,
        action: DecisionAction,
        intent_kind: IntentKind,
        policy_version: str,
        reason_codes: Sequence[ReasonCode] = (),
        belief_version_hash: str | None = None,
    ) -> TraderDecision:
        """Mint a decision with its deterministic id and canonical reason codes."""
        canonical_reasons = tuple(sorted(set(reason_codes)))
        return cls(
            decision_id=derive_decision_id(
                decision_time_ns=decision_time_ns,
                instrument_id=instrument_id,
                opportunity_id=opportunity_id,
                action=action,
                policy_version=policy_version,
            ),
            decision_time_ns=decision_time_ns,
            instrument_id=instrument_id,
            opportunity_id=opportunity_id,
            action=action,
            intent_kind=intent_kind,
            reason_codes=canonical_reasons,
            policy_version=policy_version,
            belief_version_hash=belief_version_hash,
        )
