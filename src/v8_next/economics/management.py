"""Recurrent position management advisories (M3-03).

M3-02 explains *what is happening to the thesis*. This module turns that
explanation into three things that stay strictly apart:

* :class:`PositionAdvisory` — one typed ``HOLD`` / ``REDUCE`` / ``EXIT``
  advisory. It carries a reason-code tuple, a thesis status and, for a partial
  reduction only, a lot-rounded quantity plus the fraction of the open position
  that quantity represents. It carries no order, no price and no venue verb.
* :class:`ProtectiveProposal` — a *proposal* to (re)place or tighten native
  protection. It is never submitted, it always carries
  ``requires_risk_permission=True``, and it is the only thing here that names a
  price. The native risk/execution authority owns whether the stop is placed,
  how it is typed, and whether the position is allowed to exist at all.
* :class:`ManagementDecision` — the envelope binding the two, plus the legal
  action set the advisory was checked against.

Three invariants are structural, not conventional:

1. **Protection is read, never assumed.** A protection reading that is
   ``UNKNOWN``, ``REJECTED``, ``GAP``, ``PARTIAL`` or ``UNPROTECTED`` is never
   reported as protected and can never produce a ``HOLD``: the advisory fails
   closed to ``EXIT`` and ``fail_closed`` is set. ``PARTIAL`` -- a live stop that
   covers less than the filled position -- is the state that must force a
   protective resize, so it is grouped with the denied states rather than read as
   "mostly protected". A ``PROTECTED`` reading whose stop price is absent or
   already through the mark is self-contradictory and is downgraded to
   ``UNKNOWN`` before it reaches any consumer. The enum itself is the domain's
   (:mod:`v8_next.domain.protection`), shared verbatim with the native checker,
   so a producer's ``PARTIAL`` is a consumer's ``PARTIAL``.
2. **Nothing widens risk autonomously.** A break-even move is proposed only in
   the protective direction (a ``LONG`` stop can only move up, a ``SHORT`` stop
   can only move down), only when it is still on the safe side of the mark, and
   only when the break-even level includes the round-trip fee at the supplied
   ``fee_bps``. A move that would widen the stop is never proposed.
3. **No order exists in this module.** There is no execution adapter import, no
   order object and no submission function. Every model here validates
   ``advisory_only is True``.

Conflict behaviour is the domain's, not a local vote: candidate actions resolve
through :func:`v8_next.economics.legality.resolve_conflict`, whose precedence is
risk-first (``EXIT`` > ``REDUCE`` > ``ABSTAIN`` > ``WAIT`` > ``HOLD`` >
``ENTER``). An ``ENTER`` candidate can therefore never outrank an ``EXIT``, and
``ENTER`` is not in this module's advisory vocabulary at all.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from enum import StrEnum
from types import MappingProxyType
from typing import Final, Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from v8_next.domain.decision import DecisionAction, ReasonCode, VenueOrderStatus
from v8_next.domain.protection import ProtectionState
from v8_next.economics.legality import (
    PRECEDENCE,
    LegalActionTable,
    PositionPresence,
    resolve_conflict,
)
from v8_next.economics.thesis import PositionEvaluation, ThesisReason, ThesisStatus
from v8_next.telemetry.identity import canonical_digest

__all__ = [
    "ADVISORY_TAG",
    "MANAGEMENT_POLICY_VERSION",
    "MANAGEMENT_TAG",
    "PROTECTIVE_TAG",
    "REDUCE_FRACTION",
    "SCHEMA_VERSION",
    "ManagementDecision",
    "PositionAdvisory",
    "ProtectionState",
    "ProtectiveIntent",
    "ProtectiveKind",
    "ProtectiveProposal",
    "break_even_price",
    "evaluate_management",
    "make_protective_intent_id",
    "plan_partial_reduction",
]

SCHEMA_VERSION: Final[str] = "position-management-v1"
#: Version of the advisory geometry rules below (not a venue/policy version).
MANAGEMENT_POLICY_VERSION: Final[str] = "position-management-policy-v1"

#: Identity domains for the three deterministic digests in this module.
ADVISORY_TAG: Final[str] = "position-advisory-v1"
PROTECTIVE_TAG: Final[str] = "protective-proposal-v1"
MANAGEMENT_TAG: Final[str] = "position-management-v1"

ZERO: Final[Decimal] = Decimal("0")
ONE: Final[Decimal] = Decimal("1")
BPS: Final[Decimal] = Decimal("10000")

#: Fraction of the open position a ``REDUCE`` advisory asks to close before the
#: lot rule is applied. Half the position is the documented weakening response;
#: a full closure is ``EXIT`` and is never expressed as ``REDUCE``.
REDUCE_FRACTION: Final[Decimal] = Decimal("0.5")

#: Advisory envelope lifetime for a protective proposal, measured from the
#: decision timestamp. It is a TTL on the *proposal*, not on the native order.
PROTECTIVE_TTL_NS: Final[int] = 3_600 * 1_000_000_000
_DEFAULT_LOT_STEP: Final[Decimal] = Decimal("0.001")
_DEFAULT_TICK_SIZE: Final[Decimal] = Decimal("0.01")
_QUANTITY_PRECISION: Final[Decimal] = Decimal("0.000000001")


class ProtectiveKind(StrEnum):
    """The two protective-intent proposals this module may raise."""

    PROTECTIVE_BREAK_EVEN = "PROTECTIVE_BREAK_EVEN"
    PROTECTIVE_ADD = "PROTECTIVE_ADD"


#: Reason code each management action must always carry.
_ACTION_REASON: Final[Mapping[DecisionAction, ReasonCode]] = MappingProxyType(
    {
        DecisionAction.HOLD: ReasonCode.POSITION_OPEN,
        DecisionAction.REDUCE: ReasonCode.POSITION_REDUCE_REQUIRED,
        DecisionAction.EXIT: ReasonCode.POSITION_EXIT_REQUIRED,
    }
)

#: Total projection of thesis reasons onto the decision vocabulary. The decision
#: reason set is closed, so the mapping is explicit and pinned by test: adding a
#: :class:`ThesisReason` without mapping it breaks the import guard below.
_THESIS_REASON_CODE: Final[Mapping[ThesisReason, ReasonCode]] = MappingProxyType(
    {
        ThesisReason.THESIS_INTACT: ReasonCode.POSITION_OPEN,
        ThesisReason.STOP_BREACHED: ReasonCode.POSITION_EXIT_REQUIRED,
        ThesisReason.REGIME_CHANGED: ReasonCode.EVIDENCE_CONFLICT,
        ThesisReason.EXPERT_CONTRADICTION: ReasonCode.POSITION_REDUCE_REQUIRED,
        ThesisReason.TIME_LIMIT: ReasonCode.POSITION_REDUCE_REQUIRED,
        ThesisReason.PROTECTION_GAP: ReasonCode.RISK_UNKNOWN,
        ThesisReason.BELIEF_MISSING: ReasonCode.BELIEF_MISSING,
        ThesisReason.BELIEF_STALE: ReasonCode.BELIEF_STALE,
        ThesisReason.BELIEF_ABSTAIN: ReasonCode.BELIEF_MISSING,
        ThesisReason.FUTURE_INPUT: ReasonCode.INPUT_FUTURE,
        ThesisReason.POSITION_ID_MISMATCH: ReasonCode.POSITION_UNCERTAIN,
        ThesisReason.EVIDENCE_INVALID: ReasonCode.EVIDENCE_CONFLICT,
        ThesisReason.POSITION_CLOSED: ReasonCode.POSITION_NONE,
    }
)
if set(_THESIS_REASON_CODE) != set(ThesisReason):  # pragma: no cover - import guard
    raise AssertionError("every ThesisReason needs exactly one decision reason code")

#: Reason code each non-protected protection reading contributes.
_PROTECTION_REASONS: Final[Mapping[ProtectionState, tuple[ReasonCode, ...]]] = MappingProxyType(
    {
        ProtectionState.PROTECTED: (),
        ProtectionState.UNPROTECTED: (ReasonCode.RISK_DENIED,),
        ProtectionState.PARTIAL: (ReasonCode.RISK_DENIED,),
        ProtectionState.UNKNOWN: (ReasonCode.RISK_UNKNOWN,),
        ProtectionState.REJECTED: (ReasonCode.VENUE_REJECTED, ReasonCode.RISK_DENIED),
        ProtectionState.GAP: (ReasonCode.RISK_UNKNOWN,),
    }
)

#: Actions a :class:`PositionAdvisory` is allowed to carry. ``ENTER`` is absent
#: by construction: management never opens or adds risk.
_ADVISORY_ACTIONS: Final[tuple[DecisionAction, ...]] = (
    DecisionAction.HOLD,
    DecisionAction.REDUCE,
    DecisionAction.EXIT,
)

#: Declaration order of ``ReasonCode``, used to sort every emitted tuple so the
#: output never depends on the order the inputs happened to arrive in.
_REASON_RANK: Final[Mapping[ReasonCode, int]] = MappingProxyType(
    {code: rank for rank, code in enumerate(ReasonCode)}
)


def _ordered_reasons(codes: Iterable[ReasonCode]) -> tuple[ReasonCode, ...]:
    """De-duplicate and sort reason codes into their stable declaration order."""
    unique = set(codes)
    return tuple(sorted(unique, key=_REASON_RANK.__getitem__))


def _require_finite_positive(name: str, value: Decimal) -> None:
    if not isinstance(value, Decimal):
        raise TypeError(f"{name} must be Decimal, got {type(value).__name__}")
    if not value.is_finite() or value <= ZERO:
        raise ValueError(f"{name} must be finite and positive")


def _require_finite_non_negative(name: str, value: Decimal) -> None:
    if not isinstance(value, Decimal):
        raise TypeError(f"{name} must be Decimal, got {type(value).__name__}")
    if not value.is_finite() or value < ZERO:
        raise ValueError(f"{name} must be finite and non-negative")


def _round_to_tick(value: Decimal, tick_size: Decimal, *, upward: bool) -> Decimal:
    """Snap ``value`` onto the instrument tick, never crossing the fee boundary.

    A protective stop must *cover* its fee budget, so the break-even level is
    rounded away from the entry: up for a ``LONG`` stop, down for a ``SHORT``.
    """
    steps = (value / tick_size).to_integral_value(rounding=ROUND_CEILING if upward else ROUND_FLOOR)
    return steps * tick_size


# --------------------------------------------------------------------- advisory


class PositionAdvisory(BaseModel):
    """One frozen ``HOLD`` / ``REDUCE`` / ``EXIT`` advisory.

    ``fraction`` is the share of the *open* position the advisory asks to close
    and is only non-zero for ``REDUCE``. ``reduce_quantity`` is the lot-rounded
    absolute size that fraction was derived from, so a size-aware consumer never
    has to re-derive precision. Both are capped by the open quantity by
    construction (see :func:`plan_partial_reduction`).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    advisory_id: str = Field(min_length=1)
    decision_time_ns: int = Field(ge=0)
    position_id: str = Field(min_length=1)
    thesis_id: str = Field(min_length=1)
    action: DecisionAction
    fraction: Decimal = ZERO
    reduce_quantity: Decimal | None = None
    reason_codes: tuple[ReasonCode, ...] = Field(min_length=1)
    thesis_status: ThesisStatus
    advisory_only: Literal[True] = True

    @field_validator("action")
    @classmethod
    def _management_only(cls, value: DecisionAction) -> DecisionAction:
        if value not in _ADVISORY_ACTIONS:
            raise ValueError(f"{value.value} is not a management advisory action")
        return value

    @field_validator("fraction")
    @classmethod
    def _fraction_range(cls, value: Decimal) -> Decimal:
        if not value.is_finite() or value < ZERO or value > ONE:
            raise ValueError("fraction must be finite and within [0, 1]")
        return value

    @model_validator(mode="after")
    def _consistent(self) -> PositionAdvisory:
        if self.action is DecisionAction.REDUCE:
            if self.fraction <= ZERO:
                raise ValueError("REDUCE must carry a positive fraction")
            if self.reduce_quantity is None or self.reduce_quantity <= ZERO:
                raise ValueError("REDUCE must carry a positive reduce_quantity")
        else:
            if self.fraction != ZERO:
                raise ValueError(f"{self.action.value} cannot carry a reduction fraction")
            if self.reduce_quantity is not None:
                raise ValueError(f"{self.action.value} cannot carry a reduce_quantity")
        if len(set(self.reason_codes)) != len(self.reason_codes):
            raise ValueError("advisory reason codes must be unique")
        return self


# --------------------------------------------------------------------- proposals


class ProtectiveIntent(BaseModel):
    """Typed protective-order *proposal*, reduce-only and advisory.

    This is deliberately not an :class:`~v8_next.economics.entry.EntryIntent`: an
    entry intent names an opportunity, an entry mode and an acceptable entry
    band, none of which describe a stop. The identity discipline is the entry
    module's, though — the id is the digest of the record minus the id itself, so
    every field that defines the proposal contributes to it.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    intent_id: str = Field(min_length=1)
    decision_id: str = Field(min_length=1)
    native_position_id: str = Field(min_length=1)
    instrument_id: str = Field(min_length=1)
    side: str
    trigger_price: Decimal
    quantity: Decimal
    reduce_only: Literal[True] = True
    valid_from_ns: int = Field(ge=0)
    expires_at_ns: int
    structural_stop_ref: str = Field(min_length=1)
    policy_version: str = Field(min_length=1)
    created_ns: int = Field(ge=0)
    advisory_only: Literal[True] = True

    @model_validator(mode="after")
    def _validate(self) -> ProtectiveIntent:
        if self.side not in {"LONG", "SHORT"}:
            raise ValueError("side must be LONG or SHORT")
        _require_finite_positive("trigger_price", self.trigger_price)
        _require_finite_positive("quantity", self.quantity)
        if self.expires_at_ns <= self.created_ns:
            raise ValueError("a protective proposal must expire after it is created")
        if self.valid_from_ns > self.expires_at_ns:
            raise ValueError("valid_from_ns cannot follow expiry")
        return self


_PENDING_INTENT_ID: Final[str] = "pending-protective-identity"


def make_protective_intent_id(record: ProtectiveIntent) -> str:
    """Deterministic identity over the whole intent record minus its id."""
    payload = record.model_dump(mode="json")
    del payload["intent_id"]
    return canonical_digest(PROTECTIVE_TAG, payload)


def _stamp_intent(record: ProtectiveIntent) -> ProtectiveIntent:
    return record.model_copy(update={"intent_id": make_protective_intent_id(record)})


def _build_protective_intent(
    *,
    decision_id: str,
    native_position_id: str,
    instrument_id: str,
    side: str,
    trigger_price: Decimal,
    quantity: Decimal,
    structural_stop_ref: str,
    now_ns: int,
) -> ProtectiveIntent:
    proposal = ProtectiveIntent(
        intent_id=_PENDING_INTENT_ID,
        decision_id=decision_id,
        native_position_id=native_position_id,
        instrument_id=instrument_id,
        side=side,
        trigger_price=trigger_price,
        quantity=quantity,
        valid_from_ns=now_ns,
        expires_at_ns=now_ns + PROTECTIVE_TTL_NS,
        structural_stop_ref=structural_stop_ref,
        policy_version=MANAGEMENT_POLICY_VERSION,
        created_ns=now_ns,
    )
    return _stamp_intent(proposal)


class ProtectiveProposal(BaseModel):
    """A proposed protective placement. Not an order, not submitted, not armed.

    ``requires_risk_permission`` is a ``Literal[True]``: no construction of this
    model can claim permission it does not have. The price is a *trigger
    reference*; order type, venue acceptance and sizing authority all remain
    native.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    proposal_id: str = Field(min_length=1)
    kind: ProtectiveKind
    intent: ProtectiveIntent
    native_position_id: str = Field(min_length=1)
    quantity: Decimal
    price: Decimal
    reason: ReasonCode
    requires_risk_permission: Literal[True] = True
    advisory_only: Literal[True] = True

    @model_validator(mode="after")
    def _coherent(self) -> ProtectiveProposal:
        _require_finite_positive("quantity", self.quantity)
        _require_finite_positive("price", self.price)
        if self.intent.native_position_id != self.native_position_id:
            raise ValueError("proposal and intent must name the same position")
        if self.intent.quantity != self.quantity:
            raise ValueError("proposal and intent must agree on quantity")
        if self.intent.trigger_price != self.price:
            raise ValueError("proposal and intent must agree on trigger price")
        return self


def _proposal_id(
    *,
    kind: ProtectiveKind,
    intent_id: str,
    native_position_id: str,
    quantity: Decimal,
    price: Decimal,
    reason: ReasonCode,
) -> str:
    return canonical_digest(
        PROTECTIVE_TAG,
        {
            "schema_version": SCHEMA_VERSION,
            "kind": kind.value,
            "intent_id": intent_id,
            "native_position_id": native_position_id,
            "quantity": str(quantity),
            "price": str(price),
            "reason": reason.value,
            "requires_risk_permission": True,
            "advisory_only": True,
        },
    )


# --------------------------------------------------------------------- decision


class ManagementDecision(BaseModel):
    """Envelope binding one advisory to the proposals and legal set behind it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    management_id: str = Field(min_length=1)
    schema_version: str = SCHEMA_VERSION
    evaluation_id: str = Field(min_length=1)
    thesis_id: str = Field(min_length=1)
    position_id: str = Field(min_length=1)
    decision_time_ns: int = Field(ge=0)
    advisory: PositionAdvisory
    protection_state: ProtectionState
    protected: bool
    proposals: tuple[ProtectiveProposal, ...] = ()
    legal_actions: tuple[DecisionAction, ...] = ()
    fail_closed: bool
    admission_ok: bool
    advisory_only: Literal[True] = True

    @model_validator(mode="after")
    def _invariants(self) -> ManagementDecision:
        if self.advisory.position_id != self.position_id:
            raise ValueError("advisory belongs to a different position")
        if self.advisory.thesis_id != self.thesis_id:
            raise ValueError("advisory belongs to a different thesis")
        if self.protected and self.protection_state is not ProtectionState.PROTECTED:
            raise ValueError("only a PROTECTED reading may be reported as protected")
        if self.fail_closed and self.advisory.action is not DecisionAction.EXIT:
            raise ValueError("a fail-closed management state must advise EXIT")
        if self.admission_ok and (self.fail_closed or not self.protected):
            raise ValueError("an admitted advisory must be protected and not fail-closed")
        if self.admission_ok and self.advisory.action not in self.legal_actions:
            raise ValueError("an admitted advisory must be a legal action")
        return self


def _management_id(
    *,
    evaluation_id: str,
    advisory_id: str,
    protection_state: ProtectionState,
    protected: bool,
    proposal_ids: Sequence[str],
    legal_actions: Sequence[DecisionAction],
    fail_closed: bool,
    admission_ok: bool,
) -> str:
    return canonical_digest(
        MANAGEMENT_TAG,
        {
            "schema_version": SCHEMA_VERSION,
            "evaluation_id": evaluation_id,
            "advisory_id": advisory_id,
            "protection_state": protection_state.value,
            "protected": protected,
            "proposal_ids": list(proposal_ids),
            "legal_actions": [action.value for action in legal_actions],
            "fail_closed": fail_closed,
            "admission_ok": admission_ok,
        },
    )


# ------------------------------------------------------------------ partial size


def plan_partial_reduction(
    quantity: Decimal,
    lot_step: Decimal,
    *,
    fraction: Decimal = REDUCE_FRACTION,
) -> Decimal | None:
    """Lot-rounded absolute size for a partial reduction, or ``None``.

    Rule (documented, deterministic):

    * the requested size is ``|quantity| * fraction``;
    * it is floored onto the venue lot grid — never rounded up — so the plan can
      never exceed the open quantity or request an unrepresentable size;
    * a plan that floors to zero, or that would close the whole position, is not
      a partial reduction and returns ``None`` (the caller must ``EXIT``).
    """
    _require_finite_positive("lot_step", lot_step)
    if not isinstance(quantity, Decimal):
        raise TypeError(f"quantity must be Decimal, got {type(quantity).__name__}")
    if not quantity.is_finite():
        raise ValueError("quantity must be finite")
    if not fraction.is_finite() or fraction <= ZERO or fraction > ONE:
        raise ValueError("fraction must be finite and within (0, 1]")
    size = abs(quantity)
    if size <= ZERO:
        return None
    steps = (size * fraction / lot_step).to_integral_value(rounding=ROUND_FLOOR)
    planned = steps * lot_step
    if planned <= ZERO or planned >= size:
        return None
    return planned


def break_even_price(
    side: str,
    entry_price: Decimal,
    fee_bps: Decimal,
    *,
    tick_size: Decimal = _DEFAULT_TICK_SIZE,
) -> Decimal:
    """Round-trip-fee-inclusive break-even level for ``side``.

    Fees are charged on both legs, at entry and at exit, so the level that
    returns exactly the fee budget is ``E*(1+r)/(1-r)`` for a ``LONG`` and
    ``E*(1-r)/(1+r)`` for a ``SHORT`` with ``r = fee_bps / 10_000``. The result
    is snapped *away* from the entry (up for ``LONG``, down for ``SHORT``) so the
    proposal always covers the fee rather than sitting a tick inside it.
    """
    if side not in {"LONG", "SHORT"}:
        raise ValueError("side must be LONG or SHORT")
    _require_finite_positive("entry_price", entry_price)
    _require_finite_non_negative("fee_bps", fee_bps)
    _require_finite_positive("tick_size", tick_size)
    if fee_bps >= BPS:
        raise ValueError("fee_bps must stay below 10000")
    rate = fee_bps / BPS
    if side == "LONG":
        level = _round_to_tick(entry_price * (ONE + rate) / (ONE - rate), tick_size, upward=True)
    else:
        level = _round_to_tick(entry_price * (ONE - rate) / (ONE + rate), tick_size, upward=False)
    if level <= ZERO:
        raise ValueError("break-even level must be positive")
    return level


# ------------------------------------------------------------------ evaluation


def _thesis_action(evaluation: PositionEvaluation) -> DecisionAction | None:
    """Risk-first resolution over the management candidates the thesis proposed.

    ``WAIT``/``ABSTAIN`` are not management instructions: a thesis that can only
    say "wait" yields ``None``, which the caller must treat as fail-closed rather
    than as a silent ``HOLD``.
    """
    candidates = [item for item in evaluation.proposals if item in _ADVISORY_ACTIONS]
    if not candidates:
        return None
    return resolve_conflict(candidates)


def _read_protection(
    state: ProtectionState,
    stop_price: Decimal | None,
    *,
    side: str,
    mark_price: Decimal,
) -> tuple[ProtectionState, bool]:
    """Normalise the protection reading; only ``PROTECTED`` may read protected.

    A ``PROTECTED`` claim whose stop price is missing or has already been passed
    by the mark contradicts itself, so it is downgraded to ``UNKNOWN``.
    """
    if state is not ProtectionState.PROTECTED:
        return state, False
    if stop_price is None:
        return ProtectionState.UNKNOWN, False
    if not stop_price.is_finite() or stop_price <= ZERO:
        return ProtectionState.UNKNOWN, False
    if side == "LONG" and stop_price >= mark_price:
        return ProtectionState.UNKNOWN, False
    if side == "SHORT" and stop_price <= mark_price:
        return ProtectionState.UNKNOWN, False
    return ProtectionState.PROTECTED, True


def evaluate_management(
    evaluation: PositionEvaluation,
    *,
    protection_state: ProtectionState,
    fee_bps: Decimal,
    mark_price: Decimal,
    side: str,
    entry_price: Decimal,
    quantity: Decimal,
    initial_stop_price: Decimal,
    instrument_id: str,
    protective_stop_price: Decimal | None = None,
    lot_step: Decimal = _DEFAULT_LOT_STEP,
    tick_size: Decimal = _DEFAULT_TICK_SIZE,
    venue_status: VenueOrderStatus = VenueOrderStatus.UNKNOWN,
    position_presence: PositionPresence = PositionPresence.UNCERTAIN,
    stale: bool = True,
    armed_opportunity: bool = False,
    competing_actions: tuple[DecisionAction, ...] = (),
) -> ManagementDecision:
    """Translate one thesis evaluation into a management advisory.

    Safety inputs carry no permissive default: omitting the venue status reads as
    ``UNKNOWN``, omitting presence reads as ``UNCERTAIN``, and omitting freshness
    reads as stale, so the unqualified call can be neither admitted nor
    ``HOLD``-ing. Malformed *caller* values raise; unreadable *market/protection*
    data fails closed to a typed state.

    ``competing_actions`` lets another plane (an entry or opportunity plane, say)
    submit candidates. They are resolved by the domain's risk-first precedence and
    can only ever escalate: a candidate outside the management vocabulary is
    ignored, so a competing ``ENTER`` can never become an advisory.
    """
    if not isinstance(evaluation, PositionEvaluation):
        raise TypeError("evaluation must be a PositionEvaluation")
    if not isinstance(protection_state, ProtectionState):
        raise TypeError("protection_state must be a ProtectionState")
    if not isinstance(venue_status, VenueOrderStatus):
        raise TypeError("venue_status must be VenueOrderStatus")
    if not isinstance(position_presence, PositionPresence):
        raise TypeError("position_presence must be PositionPresence")
    if not isinstance(stale, bool) or not isinstance(armed_opportunity, bool):
        raise TypeError("stale and armed_opportunity must be bool")
    if side not in {"LONG", "SHORT"}:
        raise ValueError("side must be LONG or SHORT")
    _require_finite_positive("mark_price", mark_price)
    _require_finite_positive("entry_price", entry_price)
    _require_finite_positive("initial_stop_price", initial_stop_price)
    _require_finite_positive("lot_step", lot_step)
    _require_finite_positive("tick_size", tick_size)
    _require_finite_non_negative("fee_bps", fee_bps)
    if fee_bps >= BPS:
        raise ValueError("fee_bps must stay below 10000")
    if not isinstance(quantity, Decimal):
        raise TypeError("quantity must be Decimal")
    if not quantity.is_finite():
        raise ValueError("quantity must be finite")
    if not instrument_id:
        raise ValueError("instrument_id is required")
    for candidate in competing_actions:
        if not isinstance(candidate, DecisionAction):
            raise TypeError(
                f"competing action must be DecisionAction, got {type(candidate).__name__}"
            )

    now_ns = evaluation.evaluated_ns
    legal = LegalActionTable.legal(
        position_presence,
        venue=venue_status,
        stale=stale,
        armed_opportunity=armed_opportunity,
    )
    legal_actions = tuple(action for action in PRECEDENCE if action in legal)

    effective_state, protected = _read_protection(
        protection_state, protective_stop_price, side=side, mark_price=mark_price
    )

    closed = evaluation.status is ThesisStatus.CLOSED
    fail_closed = False
    action = DecisionAction.HOLD
    reason_codes: list[ReasonCode] = [_THESIS_REASON_CODE[reason] for reason in evaluation.reasons]
    reduce_quantity: Decimal | None = None
    fraction = ZERO
    proposals: tuple[ProtectiveProposal, ...] = ()

    if closed:
        # Nothing is open: no management instruction exists, and the placeholder
        # HOLD is never admitted.
        reason_codes = [_THESIS_REASON_CODE[ThesisReason.POSITION_CLOSED]]
    else:
        thesis_action = _thesis_action(evaluation)
        if thesis_action is None:
            # The thesis could only say WAIT; that is not permission to hold.
            action = DecisionAction.EXIT
            fail_closed = True
        else:
            action = thesis_action

        if not protected:
            # Unreadable, refused or absent protection is never a reason to sit
            # still, whatever the thesis says.
            action = DecisionAction.EXIT
            fail_closed = True

        if competing_actions:
            resolution = resolve_conflict((action, *competing_actions))
            if resolution in _ADVISORY_ACTIONS:
                action = resolution

        reason_codes.extend(_PROTECTION_REASONS[effective_state])
        reason_codes.append(_ACTION_REASON[action])

        if action is DecisionAction.REDUCE:
            reduce_quantity = plan_partial_reduction(quantity, lot_step)
            if reduce_quantity is None:
                # A reduction smaller than one lot cannot be expressed: the only
                # lot-valid way to cut risk is to close the position.
                action = DecisionAction.EXIT
                reason_codes.append(_ACTION_REASON[action])
            else:
                fraction = (reduce_quantity / abs(quantity)).quantize(
                    _QUANTITY_PRECISION, rounding=ROUND_FLOOR
                )

        reason_codes = list(_ordered_reasons(reason_codes))

    advisory = PositionAdvisory(
        advisory_id=canonical_digest(
            ADVISORY_TAG,
            {
                "schema_version": SCHEMA_VERSION,
                "evaluation_id": evaluation.evaluation_id,
                "decision_time_ns": now_ns,
                "position_id": evaluation.native_position_id,
                "thesis_id": evaluation.thesis_id,
                "action": action.value,
                "fraction": str(fraction),
                "reduce_quantity": (None if reduce_quantity is None else str(reduce_quantity)),
                "reason_codes": [code.value for code in reason_codes],
                "thesis_status": evaluation.status.value,
                "advisory_only": True,
            },
        ),
        decision_time_ns=now_ns,
        position_id=evaluation.native_position_id,
        thesis_id=evaluation.thesis_id,
        action=action,
        fraction=fraction,
        reduce_quantity=reduce_quantity,
        reason_codes=tuple(reason_codes),
        thesis_status=evaluation.status,
    )

    if not closed:
        if protected and action is not DecisionAction.EXIT:
            proposal = _break_even_proposal(
                advisory_id=advisory.advisory_id,
                native_position_id=evaluation.native_position_id,
                thesis_id=evaluation.thesis_id,
                instrument_id=instrument_id,
                side=side,
                entry_price=entry_price,
                fee_bps=fee_bps,
                tick_size=tick_size,
                mark_price=mark_price,
                quantity=abs(quantity),
                current_stop_price=protective_stop_price,
                reason=_ACTION_REASON[action],
                now_ns=now_ns,
            )
        elif not protected:
            proposal = _protective_add_proposal(
                advisory_id=advisory.advisory_id,
                native_position_id=evaluation.native_position_id,
                thesis_id=evaluation.thesis_id,
                instrument_id=instrument_id,
                side=side,
                initial_stop_price=initial_stop_price,
                mark_price=mark_price,
                quantity=abs(quantity),
                protection_state=effective_state,
                now_ns=now_ns,
            )
        else:
            proposal = None
        if proposal is not None:
            proposals = (proposal,)

    action_legal = action in legal
    admission_ok = (
        not closed and not fail_closed and protected and action_legal and evaluation.admission_ok
    )

    return ManagementDecision(
        management_id=_management_id(
            evaluation_id=evaluation.evaluation_id,
            advisory_id=advisory.advisory_id,
            protection_state=effective_state,
            protected=protected,
            proposal_ids=[item.proposal_id for item in proposals],
            legal_actions=legal_actions,
            fail_closed=fail_closed,
            admission_ok=admission_ok,
        ),
        evaluation_id=evaluation.evaluation_id,
        thesis_id=evaluation.thesis_id,
        position_id=evaluation.native_position_id,
        decision_time_ns=now_ns,
        advisory=advisory,
        protection_state=effective_state,
        protected=protected,
        proposals=proposals,
        legal_actions=legal_actions,
        fail_closed=fail_closed,
        admission_ok=admission_ok,
    )


def _protective_side(side: str) -> str:
    """The side that *closes* a position on ``side``."""
    return "SHORT" if side == "LONG" else "LONG"


def _break_even_proposal(
    *,
    advisory_id: str,
    native_position_id: str,
    thesis_id: str,
    instrument_id: str,
    side: str,
    entry_price: Decimal,
    fee_bps: Decimal,
    tick_size: Decimal,
    mark_price: Decimal,
    quantity: Decimal,
    current_stop_price: Decimal | None,
    reason: ReasonCode,
    now_ns: int,
) -> ProtectiveProposal | None:
    """Break-even proposal, only when it tightens and covers round-trip fees.

    Returns ``None`` — never a widened or unbounded stop — when the current stop
    is absent, already at least as tight as break-even, or when the fee-inclusive
    break-even level is not still on the safe side of the mark (there, moving the
    stop there would trigger immediately and is not protection).
    """
    if quantity <= ZERO or current_stop_price is None:
        return None
    level = break_even_price(side, entry_price, fee_bps, tick_size=tick_size)
    if side == "LONG":
        if level <= current_stop_price:
            return None
        if level >= mark_price:
            return None
    else:
        if level >= current_stop_price:
            return None
        if level <= mark_price:
            return None
    return _make_proposal(
        kind=ProtectiveKind.PROTECTIVE_BREAK_EVEN,
        reason=reason,
        advisory_id=advisory_id,
        native_position_id=native_position_id,
        thesis_id=thesis_id,
        instrument_id=instrument_id,
        side=side,
        price=level,
        quantity=quantity,
        now_ns=now_ns,
    )


def _protective_add_proposal(
    *,
    advisory_id: str,
    native_position_id: str,
    thesis_id: str,
    instrument_id: str,
    side: str,
    initial_stop_price: Decimal,
    mark_price: Decimal,
    quantity: Decimal,
    protection_state: ProtectionState,
    now_ns: int,
) -> ProtectiveProposal | None:
    """Restore the thesis' own structural stop after a protection failure.

    The price is always the *original* thesis stop, never a level derived from
    the current mark, so a repair cannot drift wider. If that stop has already
    been passed by the market it is not protection any more and no proposal is
    made.
    """
    if quantity <= ZERO:
        return None
    if side == "LONG" and initial_stop_price >= mark_price:
        return None
    if side == "SHORT" and initial_stop_price <= mark_price:
        return None
    reason = (
        ReasonCode.RISK_DENIED
        if protection_state
        in {
            ProtectionState.UNPROTECTED,
            ProtectionState.PARTIAL,
            ProtectionState.REJECTED,
        }
        else ReasonCode.RISK_UNKNOWN
    )
    return _make_proposal(
        kind=ProtectiveKind.PROTECTIVE_ADD,
        reason=reason,
        advisory_id=advisory_id,
        native_position_id=native_position_id,
        thesis_id=thesis_id,
        instrument_id=instrument_id,
        side=side,
        price=initial_stop_price,
        quantity=quantity,
        now_ns=now_ns,
    )


def _make_proposal(
    *,
    kind: ProtectiveKind,
    reason: ReasonCode,
    advisory_id: str,
    native_position_id: str,
    thesis_id: str,
    instrument_id: str,
    side: str,
    price: Decimal,
    quantity: Decimal,
    now_ns: int,
) -> ProtectiveProposal:
    intent = _build_protective_intent(
        decision_id=advisory_id,
        native_position_id=native_position_id,
        instrument_id=instrument_id,
        side=_protective_side(side),
        trigger_price=price,
        quantity=quantity,
        structural_stop_ref=f"thesis:{thesis_id}",
        now_ns=now_ns,
    )
    return ProtectiveProposal(
        proposal_id=_proposal_id(
            kind=kind,
            intent_id=intent.intent_id,
            native_position_id=native_position_id,
            quantity=quantity,
            price=price,
            reason=reason,
        ),
        kind=kind,
        intent=intent,
        native_position_id=native_position_id,
        quantity=quantity,
        price=price,
        reason=reason,
    )
