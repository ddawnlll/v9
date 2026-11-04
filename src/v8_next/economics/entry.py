"""Price-aware entry proposals (M3-01).

The proposal plane sits between an advisory ``TraderDecision`` (ENTER) and any
risk/native execution authority. It answers a narrower question than "should we
trade?": *at which prices and for how long would this entry still be worth
expressing?*

Invariants:

- An :class:`EntryIntent` is inert. It carries no venue order, no size and no
  risk permission; ``advisory_only`` is pinned to ``True``.
- Every price/TTL revision is a **new immutable proposal** with a new
  ``intent_id``. The original is never mutated.
- Price evidence must be point-in-time: a quote whose ``available_at_ns`` is
  later than the evaluation clock cannot be used, even if its event time is old.
- A touched limit price is not a fill. Without book depth the fill certainty is
  ``UNKNOWN``; with bid/ask it is at best ``TOUCHED_ONLY``.
- Unknown venue status blocks every new entry (fail closed).
"""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from enum import StrEnum
from typing import Any, Final

from pydantic import BaseModel, ConfigDict, Field, model_validator

from v8_next.domain.decision import VenueOrderStatus

SCHEMA_VERSION: Final[str] = "entry-intent-v1"
ZERO = Decimal("0")


class EntryMode(StrEnum):
    """The five RFC §8.3 entry decisions plus a not-yet-valid state."""

    MARKET_NOW = "MARKET_NOW"
    LIMIT_WAIT = "LIMIT_WAIT"
    SKIP_PRICE = "SKIP_PRICE"
    WAIT_BELIEF = "WAIT_BELIEF"
    CANCEL_STALE = "CANCEL_STALE"
    NOT_YET_VALID = "NOT_YET_VALID"


class OrderPolicy(StrEnum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"
    POST_ONLY = "POST_ONLY"


class ProposalState(StrEnum):
    PROPOSED = "PROPOSED"
    CANCELLED = "CANCELLED"
    SUPERSEDED = "SUPERSEDED"
    EXPIRED = "EXPIRED"


class FrictionStatus(StrEnum):
    """Whether observed cost evidence exists for this proposal."""

    OBSERVED = "OBSERVED"
    MISSING = "MISSING"
    STALE = "STALE"


class FillCertainty(StrEnum):
    """What can honestly be said about execution, never a probability."""

    NOT_APPLICABLE = "NOT_APPLICABLE"
    UNKNOWN = "UNKNOWN"
    TOUCHED_ONLY = "TOUCHED_ONLY"


class EntryReason(StrEnum):
    ENTRY_READY = "ENTRY_READY"
    ENTRY_EXPIRED = "ENTRY_EXPIRED"
    ENTRY_NOT_YET_VALID = "ENTRY_NOT_YET_VALID"
    PRICE_OUTSIDE_BAND = "PRICE_OUTSIDE_BAND"
    DATA_MISSING = "DATA_MISSING"
    DATA_CONTRADICTORY = "DATA_CONTRADICTORY"
    FUTURE_QUOTE = "FUTURE_QUOTE"
    VENUE_UNKNOWN = "VENUE_UNKNOWN"
    BELIEF_STALE = "BELIEF_STALE"


class MarketQuote(BaseModel):
    """One PIT price observation. Absent fields stay ``None``, never guessed."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    instrument_id: str = Field(min_length=1)
    bid: Decimal | None = None
    ask: Decimal | None = None
    mark: Decimal | None = None
    available_at_ns: int
    event_time_ns: int
    source: str = Field(min_length=1)

    @model_validator(mode="after")
    def _consistent(self) -> MarketQuote:
        for name in ("bid", "ask", "mark"):
            value = getattr(self, name)
            if value is not None and (not value.is_finite() or value <= ZERO):
                raise ValueError(f"{name} must be finite and positive")
        if self.bid is not None and self.ask is not None and self.bid > self.ask:
            raise ValueError("bid cannot exceed ask")
        if self.available_at_ns < 0 or self.event_time_ns < 0:
            raise ValueError("timestamps must be non-negative")
        if self.available_at_ns < self.event_time_ns:
            raise ValueError("availability cannot precede the event")
        return self

    @property
    def has_spread(self) -> bool:
        return self.bid is not None and self.ask is not None

    @property
    def spread(self) -> Decimal | None:
        if self.bid is None or self.ask is None:
            return None
        return self.ask - self.bid


def _digest(payload: dict[str, Any]) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


class EntryIntent(BaseModel):
    """Immutable, advisory entry proposal."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    intent_id: str = Field(min_length=1)
    decision_id: str = Field(min_length=1)
    opportunity_id: str = Field(min_length=1)
    instrument_id: str = Field(min_length=1)
    side: str
    mode: EntryMode
    order_policy: OrderPolicy
    valid_after_ns: int = Field(ge=0)
    expires_at_ns: int = Field(gt=0)
    acceptable_price_low: Decimal
    acceptable_price_high: Decimal
    structural_stop_ref: str = Field(min_length=1)
    size_policy_id: str = Field(min_length=1)
    friction_status: FrictionStatus
    venue_version: str = Field(min_length=1)
    policy_version: str = Field(min_length=1)
    kill_conditions: tuple[str, ...] = ()
    cancel_reasons: tuple[str, ...] = ()
    revision: int = Field(ge=0)
    created_ns: int = Field(ge=0)
    state: ProposalState = ProposalState.PROPOSED
    advisory_only: bool = True

    @model_validator(mode="after")
    def _validate(self) -> EntryIntent:
        if self.side not in {"LONG", "SHORT"}:
            raise ValueError("side must be LONG or SHORT")
        if self.advisory_only is not True:
            raise ValueError("EntryIntent never grants order authority")
        if not (self.acceptable_price_low.is_finite() and self.acceptable_price_high.is_finite()):
            raise ValueError("price band must be finite")
        if self.acceptable_price_low <= ZERO:
            raise ValueError("price band must be positive")
        if self.acceptable_price_low > self.acceptable_price_high:
            raise ValueError("acceptable price band is inverted")
        if self.expires_at_ns <= self.valid_after_ns:
            raise ValueError("expires_at_ns must follow valid_after_ns")
        if self.created_ns >= self.expires_at_ns:
            raise ValueError("proposal cannot be created at or after expiry")
        if self.mode is EntryMode.MARKET_NOW and self.order_policy is not OrderPolicy.MARKET:
            raise ValueError("MARKET_NOW requires MARKET policy")
        if len(set(self.kill_conditions)) != len(self.kill_conditions):
            raise ValueError("kill_conditions must be unique")
        return self

    def to_receipt(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "intent_id": self.intent_id,
            "decision_id": self.decision_id,
            "opportunity_id": self.opportunity_id,
            "instrument_id": self.instrument_id,
            "side": self.side,
            "mode": self.mode.value,
            "order_policy": self.order_policy.value,
            "valid_after_ns": self.valid_after_ns,
            "expires_at_ns": self.expires_at_ns,
            "acceptable_price_low": str(self.acceptable_price_low),
            "acceptable_price_high": str(self.acceptable_price_high),
            "friction_status": self.friction_status.value,
            "revision": self.revision,
            "state": self.state.value,
            "advisory_only": self.advisory_only,
        }


def make_intent_id(
    *,
    decision_id: str,
    opportunity_id: str,
    instrument_id: str,
    side: str,
    order_policy: OrderPolicy | str,
    valid_after_ns: int,
    expires_at_ns: int,
    acceptable_price_low: Decimal | str,
    acceptable_price_high: Decimal | str,
    revision: int,
) -> str:
    """Deterministic proposal identity. No wall clock, no randomness."""
    payload = {
        "decision_id": decision_id,
        "opportunity_id": opportunity_id,
        "instrument_id": instrument_id,
        "side": side,
        "order_policy": OrderPolicy(order_policy).value,
        "valid_after_ns": valid_after_ns,
        "expires_at_ns": expires_at_ns,
        "acceptable_price_low": str(acceptable_price_low),
        "acceptable_price_high": str(acceptable_price_high),
        "revision": revision,
    }
    return _digest(payload)


def build_intent(
    *,
    decision_id: str,
    opportunity_id: str,
    instrument_id: str,
    side: str,
    order_policy: OrderPolicy,
    valid_after_ns: int,
    expires_at_ns: int,
    acceptable_price_low: Decimal,
    acceptable_price_high: Decimal,
    structural_stop_ref: str,
    size_policy_id: str,
    friction_status: FrictionStatus,
    venue_version: str,
    policy_version: str,
    created_ns: int,
    kill_conditions: tuple[str, ...] = (),
    revision: int = 0,
) -> EntryIntent:
    intent_id = make_intent_id(
        decision_id=decision_id,
        opportunity_id=opportunity_id,
        instrument_id=instrument_id,
        side=side,
        order_policy=order_policy,
        valid_after_ns=valid_after_ns,
        expires_at_ns=expires_at_ns,
        acceptable_price_low=acceptable_price_low,
        acceptable_price_high=acceptable_price_high,
        revision=revision,
    )
    return EntryIntent(
        intent_id=intent_id,
        decision_id=decision_id,
        opportunity_id=opportunity_id,
        instrument_id=instrument_id,
        side=side,
        mode=EntryMode.MARKET_NOW if order_policy is OrderPolicy.MARKET else EntryMode.LIMIT_WAIT,
        order_policy=order_policy,
        valid_after_ns=valid_after_ns,
        expires_at_ns=expires_at_ns,
        acceptable_price_low=acceptable_price_low,
        acceptable_price_high=acceptable_price_high,
        structural_stop_ref=structural_stop_ref,
        size_policy_id=size_policy_id,
        friction_status=friction_status,
        venue_version=venue_version,
        policy_version=policy_version,
        kill_conditions=kill_conditions,
        created_ns=created_ns,
        revision=revision,
    )


class EntryEvaluation(BaseModel):
    """Outcome of evaluating one proposal against one PIT quote."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    intent_id: str
    revision: int
    evaluated_ns: int
    mode: EntryMode
    reason: EntryReason
    reference_price: Decimal | None = None
    limit_price: Decimal | None = None
    fill_certainty: FillCertainty
    friction_status: FrictionStatus
    advisory_only: bool = True

    @model_validator(mode="after")
    def _no_order_authority(self) -> EntryEvaluation:
        if self.advisory_only is not True:
            raise ValueError("evaluation is advisory only")
        return self


def _reference_price(intent: EntryIntent, quote: MarketQuote) -> Decimal | None:
    if intent.side == "LONG":
        return quote.ask if quote.ask is not None else quote.mark
    return quote.bid if quote.bid is not None else quote.mark


def evaluate_entry(
    intent: EntryIntent,
    quote: MarketQuote | None,
    *,
    now_ns: int,
    venue_status: VenueOrderStatus = VenueOrderStatus.ACCEPTED,
    belief_stale: bool = False,
    belief_available: bool = True,
) -> EntryEvaluation:
    """Evaluate a proposal without creating any native side effect."""

    def result(
        mode: EntryMode,
        reason: EntryReason,
        *,
        reference: Decimal | None = None,
        limit: Decimal | None = None,
        certainty: FillCertainty = FillCertainty.NOT_APPLICABLE,
        friction: FrictionStatus | None = None,
    ) -> EntryEvaluation:
        return EntryEvaluation(
            intent_id=intent.intent_id,
            revision=intent.revision,
            evaluated_ns=now_ns,
            mode=mode,
            reason=reason,
            reference_price=reference,
            limit_price=limit,
            fill_certainty=certainty,
            friction_status=intent.friction_status if friction is None else friction,
        )

    if intent.state is not ProposalState.PROPOSED:
        return result(EntryMode.CANCEL_STALE, EntryReason.ENTRY_EXPIRED)
    if now_ns < intent.valid_after_ns:
        return result(EntryMode.NOT_YET_VALID, EntryReason.ENTRY_NOT_YET_VALID)
    if now_ns >= intent.expires_at_ns:
        return result(EntryMode.CANCEL_STALE, EntryReason.ENTRY_EXPIRED)
    if venue_status is VenueOrderStatus.UNKNOWN:
        return result(EntryMode.CANCEL_STALE, EntryReason.VENUE_UNKNOWN)
    if belief_stale:
        return result(EntryMode.WAIT_BELIEF, EntryReason.BELIEF_STALE)
    if not belief_available:
        return result(EntryMode.WAIT_BELIEF, EntryReason.DATA_MISSING)
    if quote is None:
        return result(EntryMode.WAIT_BELIEF, EntryReason.DATA_MISSING)
    if quote.instrument_id != intent.instrument_id:
        return result(EntryMode.WAIT_BELIEF, EntryReason.DATA_CONTRADICTORY)
    if quote.available_at_ns > now_ns:
        return result(EntryMode.WAIT_BELIEF, EntryReason.FUTURE_QUOTE)

    reference = _reference_price(intent, quote)
    if reference is None:
        return result(EntryMode.WAIT_BELIEF, EntryReason.DATA_MISSING)
    if reference < intent.acceptable_price_low or reference > intent.acceptable_price_high:
        return result(EntryMode.SKIP_PRICE, EntryReason.PRICE_OUTSIDE_BAND, reference=reference)

    friction = (
        FrictionStatus.OBSERVED if quote.has_spread else FrictionStatus.MISSING
    )
    if intent.order_policy is OrderPolicy.MARKET:
        if not quote.has_spread:
            # A market entry without an observed touch cannot be priced honestly.
            return result(
                EntryMode.WAIT_BELIEF,
                EntryReason.DATA_MISSING,
                reference=reference,
                friction=friction,
            )
        return result(
            EntryMode.MARKET_NOW,
            EntryReason.ENTRY_READY,
            reference=reference,
            certainty=FillCertainty.NOT_APPLICABLE,
            friction=friction,
        )
    certainty = FillCertainty.TOUCHED_ONLY if quote.has_spread else FillCertainty.UNKNOWN
    return result(
        EntryMode.LIMIT_WAIT,
        EntryReason.ENTRY_READY,
        reference=reference,
        limit=reference,
        certainty=certainty,
        friction=friction,
    )


def revise(
    intent: EntryIntent,
    *,
    created_ns: int,
    acceptable_price_low: Decimal | None = None,
    acceptable_price_high: Decimal | None = None,
    valid_after_ns: int | None = None,
    expires_at_ns: int | None = None,
) -> EntryIntent:
    """Return a new revision; the input proposal is unchanged (superseded)."""
    low = intent.acceptable_price_low if acceptable_price_low is None else acceptable_price_low
    high = intent.acceptable_price_high if acceptable_price_high is None else acceptable_price_high
    valid_after = intent.valid_after_ns if valid_after_ns is None else valid_after_ns
    expires = intent.expires_at_ns if expires_at_ns is None else expires_at_ns
    revision = intent.revision + 1
    if (low, high, valid_after, expires) == (
        intent.acceptable_price_low,
        intent.acceptable_price_high,
        intent.valid_after_ns,
        intent.expires_at_ns,
    ):
        raise ValueError("a revision must change at least one price/TTL field")
    return build_intent(
        decision_id=intent.decision_id,
        opportunity_id=intent.opportunity_id,
        instrument_id=intent.instrument_id,
        side=intent.side,
        order_policy=intent.order_policy,
        valid_after_ns=valid_after,
        expires_at_ns=expires,
        acceptable_price_low=low,
        acceptable_price_high=high,
        structural_stop_ref=intent.structural_stop_ref,
        size_policy_id=intent.size_policy_id,
        friction_status=intent.friction_status,
        venue_version=intent.venue_version,
        policy_version=intent.policy_version,
        created_ns=created_ns,
        kill_conditions=intent.kill_conditions,
        revision=revision,
    )


def cancel(intent: EntryIntent, reason: str) -> EntryIntent:
    """Cancel a proposal by creating a terminal copy; no native cancel is sent."""
    if not reason:
        raise ValueError("cancel reason is required")
    return intent.model_copy(
        update={
            "state": ProposalState.CANCELLED,
            "cancel_reasons": (*intent.cancel_reasons, reason),
        }
    )


def is_duplicate(first: EntryIntent, second: EntryIntent) -> bool:
    """Idempotence check for repeated signals for the same proposal revision."""
    return first.intent_id == second.intent_id and first.revision == second.revision
