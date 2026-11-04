"""Differential replay harness: V8 policy-off parity and V9 shadow invariance (M2-06 / #21).

What this module answers
------------------------
Two questions, kept deliberately separate:

1. *Is the V8 baseline unchanged?* :func:`capture_replay` turns a typed, deterministic
   event fixture into a :class:`ReplaySignature` -- one canonical digest per native
   evidence stream (opportunity selection, native orders, stop/protection orders,
   fills, funding rows) plus the audit-lineage verdict. :func:`pin` serialises that
   signature as canonical JSON, and :func:`assert_pinned` re-derives it so a later
   wave that shifts a price by one tick or a fee by one cent fails loudly instead of
   drifting quietly. :func:`compare` is the differential gate over two signatures.
2. *Does V9 shadow mutate anything?* Under ``policy_state="SHADOW"`` the adaptive
   chain (belief -> evidence -> policy -> decision receipt) runs over the same
   fixture's decision cuts and populates a decision stream. The run is handed a
   live :class:`NativeSession` -- the only native surface it can reach -- and that
   session's own digest is taken *before* and *after* the chain and must be
   identical. A session is required for ``SHADOW``: a digest pair over a frozen
   fixture would be a gate that cannot observe anything. Nothing in the chain can
   construct an order because this module imports no execution adapter and no
   order type (pinned by ``tests/test_shadow_parity.py`` at the AST level); the
   sink is an injected protocol, so a mutating adaptive step is *detectable*
   rather than impossible to write.
   :func:`assert_shadow_unmutated` is the purpose-built check for that pair.

Why not the existing gates
--------------------------
:func:`v8_next.evaluation.resource_baseline.parity_gate` compares five *runtime*
streams (``decision``/``order``/``fill``/``funding``/``report``) from measured
receipts; its :class:`~v8_next.evaluation.resource_baseline.GateStatus` and
:class:`~v8_next.evaluation.resource_baseline.ParityGateResult` vocabulary is
reused here verbatim, but its stream set cannot express opportunity selection,
protection orders or lineage verdicts -- so this module owns its own signature
and gate over six native streams. No measurement, no tape download and no engine
start: the fixture *is* the input.

Order sensitivity (stated, not assumed)
---------------------------------------
Every native stream is content-addressed: rows are canonicalised by a natural key
(``(submitted_ns, client_order_id)`` for orders, ``(event_time_ns, fill_id)`` for
fills, and so on) and hashed as ``Decimal`` numeric values, so ``1.0`` and ``1.00``
are the same amount while ``1.00`` and ``1.01`` are not. Reordering a fixture's
tuples therefore cannot change a native digest, and neither can it change the
PIT view the adaptive chain sees (``build_view`` sorts internally).

The adaptive decision stream *is* order-sensitive, by construction: belief update
is stateful (``advance(previous, ...)``), so the sequence of cuts is part of the
evidence. That is why a script must give every cut a distinct ``decision_time_ns``
and why the chain replays cuts in time order -- the signature binds the order
exactly once, instead of leaving two orderings silently equivalent.

Everything here is advisory: a decision receipt is not an order, and this module
cannot submit, amend or cancel anything.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from types import MappingProxyType
from typing import Annotated, Final, Literal, Protocol, Self, TypeAlias

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    ValidationError,
    model_validator,
)

from v8_next.audit.lineage import (
    CompletenessVerdict,
    LineageCashflow,
    LineageCertification,
    LineageChain,
    LineageDecision,
    LineageFill,
    LineageOpportunity,
    LineageOrder,
    certify,
)
from v8_next.belief import (
    BeliefStatus,
    MarketBeliefV1,
    UncertaintyState,
    advance,
)
from v8_next.domain.decision import OpportunityLifecycle, VenueOrderStatus
from v8_next.domain.horizons import Timeframe, assemble_context
from v8_next.domain.observation import (
    Observation,
    ObservationKind,
    ObservationView,
    QualityState,
    build_view,
)
from v8_next.economics.legality import PositionPresence
from v8_next.economics.policy import (
    CapacityState,
    DecisionRequest,
    PortfolioState,
    PositionState,
    TraderDecisionPolicy,
)
from v8_next.evaluation.resource_baseline import (
    GateStatus,
    ParityGateResult,
    StreamVerdict,
)
from v8_next.evaluation.store import canonical
from v8_next.telemetry.decisions import (
    DecisionInputRef,
    DecisionLedger,
    DecisionReceipt,
    ReceiptInputs,
)
from v8_next.telemetry.identity import canonical_digest

__all__ = [
    "DIGEST_VERSION",
    "NATIVE_STREAMS",
    "POLICY_OFF",
    "POLICY_SHADOW",
    "STREAMS",
    "BarEvent",
    "CashflowEvent",
    "DecisionCutEvent",
    "FillEvent",
    "FundingRowEvent",
    "LineageNotCertifiable",
    "NativeDecisionEvent",
    "NativeOrderEvent",
    "NativeSession",
    "OpportunityEvent",
    "ParityVerdict",
    "PinnedReferenceDrift",
    "PinnedReferenceMalformed",
    "PolicyState",
    "ProtectionOrderEvent",
    "ReplayScript",
    "ReplaySignature",
    "ShadowMutationError",
    "ShadowNotExercised",
    "ShadowParityError",
    "ShadowStep",
    "assert_pinned",
    "assert_shadow_unmutated",
    "capture_replay",
    "compare",
    "pin",
    "shadow_decisions",
]

#: Schema tag of the digest payloads in this module. Bumping it invalidates every
#: pinned reference on purpose: a fixture that changed meaning is a new baseline.
DIGEST_VERSION: Final[str] = "v9.shadow-parity.1"

#: Native evidence streams a policy-off baseline must reproduce. Order is the
#: comparison order (mirrors ``resource_baseline.REQUIRED_PARITY_STREAMS``).
NATIVE_STREAMS: Final[tuple[str, ...]] = (
    "opportunity",
    "orders",
    "protection",
    "fills",
    "funding",
    "lineage",
)

#: Every stream a signature can carry. ``decisions`` is adaptive, never native.
STREAMS: Final[tuple[str, ...]] = (*NATIVE_STREAMS, "decisions")

PolicyState = Literal["OFF", "SHADOW"]

#: V8 behaviour: the adaptive chain is not evaluated and no decision stream exists.
POLICY_OFF: Final[PolicyState] = "OFF"
#: V9 behaviour: the adaptive chain is evaluated, advisory only.
POLICY_SHADOW: Final[PolicyState] = "SHADOW"

#: Bayesian-belief uncertainty is a state enum, not a variance. The policy asks for
#: a ``Decimal`` in ``[0, 1]``, so each state maps to its declared band midpoint.
#: Declared, never fitted: the mapping is ordinal and exists only to keep the
#: policy's evidence ladder from being fed an invented probability.
UNCERTAINTY_BANDS: Final[Mapping[UncertaintyState, Decimal]] = MappingProxyType(
    {
        UncertaintyState.LOW: Decimal("0.10"),
        UncertaintyState.MEDIUM: Decimal("0.40"),
        UncertaintyState.HIGH: Decimal("0.75"),
        UncertaintyState.UNKNOWN: Decimal("1.00"),
    }
)

#: Weight of the structure-direction expert. A constant, not a fit: it makes the
#: structure contribution weaker than a full-confidence regime reading.
STRUCTURE_WEIGHT: Final[Decimal] = Decimal("0.40")

#: Precision kept on an evidence weight, so the same belief yields the same stance.
WEIGHT_QUANTUM: Final[Decimal] = Decimal("0.0001")

#: Stance words the policy accepts (``v8_next.economics.decisions.StanceKind``).
SUPPORT: Final[str] = "SUPPORT"
CONTRADICT: Final[str] = "CONTRADICT"
ABSTAIN: Final[str] = "ABSTAIN"

_STRUCTURE_EXPERT: Final[str] = "structure"
_REGIME_EXPERT: Final[str] = "regime"
_FIXTURE_SOURCE: Final[str] = "shadow-parity-fixture"


# --------------------------------------------------------------------------- #
# the native sink a shadow pass is measured against
# --------------------------------------------------------------------------- #
class NativeSession(Protocol):
    """The only native surface a shadow pass may be handed.

    A shadow run must be able to *observe* the native state it claims not to
    change. ``digest`` is a content digest of that state; ``submit`` and
    ``cancel`` are the only mutating verbs the adaptive path could ever reach,
    which is why they are the ones declared here. This module implements no
    session, imports no execution adapter and names no order type: the session is
    injected by the caller (a paper engine in production, a recording double in
    tests). Zero mutation is therefore *measured* on a live sink, not argued from
    a frozen fixture.
    """

    def digest(self) -> str:
        """Content digest of the native state this session currently holds."""
        ...

    def submit(self, intent: object) -> None:
        """Ask the native authority to act on an intent. A shadow run never may."""
        ...

    def cancel(self, client_order_id: str) -> None:
        """Ask the native authority to cancel an order. A shadow run never may."""
        ...


#: One adaptive pass: cut -> belief -> evidence -> policy -> receipts, with the
#: native sink in scope so a violation is observable. The production pass
#: (:func:`shadow_decisions`) never calls the sink; a test can inject one that does.
ShadowStep: TypeAlias = Callable[["ReplayScript", NativeSession], "tuple[DecisionReceipt, ...]"]


# --------------------------------------------------------------------------- #
# errors
# --------------------------------------------------------------------------- #
class ShadowParityError(ValueError):
    """A parity/invariance contract was violated.

    ``code`` is the machine-readable discriminator; ``reasons`` names every
    individual failure (a drift is reported per stream, never collapsed).
    """

    code: str = "SHADOW_PARITY_ERROR"

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        reasons: Sequence[str] = (),
    ) -> None:
        resolved = code or type(self).code
        super().__init__(f"{resolved}: {message}")
        self.code = resolved
        self.reasons = tuple(reasons)


class LineageNotCertifiable(ShadowParityError):
    """The fixture's own cashflow lineage is not ``COMPLETE``; it cannot be a baseline."""

    code = "LINEAGE_NOT_CERTIFIABLE"


class ShadowNotExercised(ShadowParityError):
    """A shadow replay produced no decision, so invariance would be vacuous."""

    code = "SHADOW_NOT_EXERCISED"


class ShadowMutationError(ShadowParityError):
    """A policy-on run changed a native stream or failed the shadow contract."""

    code = "SHADOW_NATIVE_MUTATION"


class PinnedReferenceMalformed(ShadowParityError):
    """The pinned reference is not a readable :class:`ReplaySignature` document."""

    code = "PINNED_REFERENCE_MALFORMED"


class PinnedReferenceDrift(ShadowParityError):
    """A captured signature no longer reproduces its pinned reference."""

    code = "PINNED_REFERENCE_DRIFT"


# --------------------------------------------------------------------------- #
# primitives
# --------------------------------------------------------------------------- #
def _require_decimal(value: object) -> object:
    """Reject float/str laundering: money and prices are ``Decimal`` at the boundary."""
    if isinstance(value, bool) or not isinstance(value, Decimal):
        raise ValueError("money/price values must be Decimal, never float or str")
    if not value.is_finite():
        raise ValueError("money/price values must be finite")
    return value


#: ``Decimal`` that refuses float coercion; deterministic by construction.
Amount = Annotated[Decimal, BeforeValidator(_require_decimal), Field(allow_inf_nan=False)]


def _amount(value: Decimal) -> str:
    """Numeric identity of an amount: ``1.0``, ``1.00`` and ``1`` are the same value."""
    if not value.is_finite():  # pragma: no cover - guarded at construction
        raise ValueError("non-finite amount")
    if value == 0:
        return "0"
    return format(value.normalize(), "f")


def _is_digest(value: str) -> bool:
    return len(value.strip()) >= 32 and all(char in "0123456789abcdef" for char in value)


def _duplicates(values: Sequence[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    repeated: set[str] = set()
    for value in values:
        if value in seen:
            repeated.add(value)
        seen.add(value)
    return tuple(sorted(repeated))


def _stream_digest(stream: str, payload: object) -> str:
    if stream not in NATIVE_STREAMS:
        raise ValueError(f"unknown native stream {stream!r}")
    return canonical_digest(f"{DIGEST_VERSION}.{stream}", payload)


# --------------------------------------------------------------------------- #
# event fixtures (typed; no engine, no wall clock, no market download)
# --------------------------------------------------------------------------- #
class BarEvent(BaseModel):
    """One completed bar fixture, converted to a PIT observation on demand."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    instrument_id: str
    start_ns: int = Field(ge=0)
    end_ns: int = Field(ge=0)
    open: Amount
    high: Amount
    low: Amount
    close: Amount
    volume: Amount
    sequence: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def _validate_bar(self) -> Self:
        if not self.instrument_id.strip():
            raise ValueError("BAR: instrument_id cannot be empty")
        if self.end_ns <= self.start_ns:
            raise ValueError("BAR: end_ns must be after start_ns")
        if self.high < self.low:
            raise ValueError("BAR: high cannot be below low")
        if not (self.low <= self.open <= self.high):
            raise ValueError("BAR: open must lie inside [low, high]")
        if not (self.low <= self.close <= self.high):
            raise ValueError("BAR: close must lie inside [low, high]")
        if self.volume < 0:
            raise ValueError("BAR: volume cannot be negative")
        return self

    def to_observation(self) -> Observation:
        """The canonical BAR observation: available only once the bar has closed."""
        return Observation(
            instrument_id=self.instrument_id,
            kind=ObservationKind.BAR,
            event_time=self.start_ns,
            available_at=self.end_ns,
            received_at=self.end_ns,
            published_at=self.end_ns,
            source=_FIXTURE_SOURCE,
            sequence=self.sequence,
            quality=QualityState.OK,
            payload={
                "start_ns": self.start_ns,
                "end_ns": self.end_ns,
                "open": str(self.open),
                "high": str(self.high),
                "low": str(self.low),
                "close": str(self.close),
                "volume": str(self.volume),
                "source_hash": f"shadow-parity-bar-{self.instrument_id}-{self.start_ns}",
            },
        )


class OpportunityEvent(BaseModel):
    """One opportunity selection observation from the native book."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    opportunity_id: str
    campaign_id: str
    instrument_id: str
    direction: str
    lifecycle: OpportunityLifecycle
    selected_ns: int = Field(ge=0)
    available_at_ns: int = Field(ge=0)

    @model_validator(mode="after")
    def _validate_ids(self) -> Self:
        for name in ("opportunity_id", "campaign_id", "instrument_id", "direction"):
            if not getattr(self, name).strip():
                raise ValueError(f"OPPORTUNITY: {name} cannot be empty")
        if self.available_at_ns < self.selected_ns:
            raise ValueError("OPPORTUNITY: availability cannot precede selection")
        return self


class NativeDecisionEvent(BaseModel):
    """A V8 native decision: the layer that authored the orders in the fixture."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    decision_id: str
    opportunity_id: str
    campaign_id: str
    instrument_id: str
    decision_time_ns: int = Field(ge=0)
    available_at_ns: int = Field(ge=0)

    @model_validator(mode="after")
    def _validate(self) -> Self:
        for name in ("decision_id", "opportunity_id", "campaign_id", "instrument_id"):
            if not getattr(self, name).strip():
                raise ValueError(f"NATIVE_DECISION: {name} cannot be empty")
        if self.available_at_ns < self.decision_time_ns:
            raise ValueError("NATIVE_DECISION: availability cannot precede the decision")
        return self


class NativeOrderEvent(BaseModel):
    """A venue order emitted by V8: client order id, quantity and price are its identity."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    client_order_id: str
    decision_id: str
    campaign_id: str
    instrument_id: str
    side: Literal["BUY", "SELL"]
    order_type: Literal["LIMIT", "MARKET"]
    quantity: Amount
    price: Amount
    submitted_ns: int = Field(ge=0)
    available_at_ns: int = Field(ge=0)

    @model_validator(mode="after")
    def _validate(self) -> Self:
        for name in ("client_order_id", "decision_id", "campaign_id", "instrument_id"):
            if not getattr(self, name).strip():
                raise ValueError(f"ORDER: {name} cannot be empty")
        if self.quantity <= 0:
            raise ValueError("ORDER: quantity must be positive")
        if self.price <= 0:
            raise ValueError("ORDER: price must be positive")
        if self.available_at_ns < self.submitted_ns:
            raise ValueError("ORDER: availability cannot precede submission")
        return self


class ProtectionOrderEvent(BaseModel):
    """A native stop/target order attached to a parent client order id."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    client_order_id: str
    parent_client_order_id: str
    kind: Literal["STOP_LOSS", "TAKE_PROFIT"]
    side: Literal["BUY", "SELL"]
    trigger_price: Amount
    quantity: Amount
    submitted_ns: int = Field(ge=0)
    available_at_ns: int = Field(ge=0)

    @model_validator(mode="after")
    def _validate(self) -> Self:
        for name in ("client_order_id", "parent_client_order_id"):
            if not getattr(self, name).strip():
                raise ValueError(f"PROTECTION: {name} cannot be empty")
        if self.client_order_id == self.parent_client_order_id:
            raise ValueError("PROTECTION: a protective order cannot be its own parent")
        if self.trigger_price <= 0:
            raise ValueError("PROTECTION: trigger_price must be positive")
        if self.quantity <= 0:
            raise ValueError("PROTECTION: quantity must be positive")
        if self.available_at_ns < self.submitted_ns:
            raise ValueError("PROTECTION: availability cannot precede submission")
        return self


class FillEvent(BaseModel):
    """One native execution, carrying its fee as ``Decimal`` in a named currency."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    fill_id: str
    client_order_id: str
    decision_id: str
    campaign_id: str
    instrument_id: str
    quantity: Amount
    price: Amount
    fee_amount: Amount
    fee_currency: str
    event_time_ns: int = Field(ge=0)
    available_at_ns: int = Field(ge=0)

    @model_validator(mode="after")
    def _validate(self) -> Self:
        for name in (
            "fill_id",
            "client_order_id",
            "decision_id",
            "campaign_id",
            "instrument_id",
            "fee_currency",
        ):
            if not getattr(self, name).strip():
                raise ValueError(f"FILL: {name} cannot be empty")
        if self.quantity <= 0:
            raise ValueError("FILL: quantity must be positive")
        if self.price <= 0:
            raise ValueError("FILL: price must be positive")
        if self.fee_amount < 0:
            raise ValueError("FILL: fee_amount cannot be negative")
        if self.available_at_ns < self.event_time_ns:
            raise ValueError("FILL: availability cannot precede the fill")
        return self


class CashflowEvent(BaseModel):
    """A terminal cash movement produced by one fill (the lineage chain's last layer)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    cashflow_id: str
    fill_id: str
    campaign_id: str
    amount: Amount
    event_time_ns: int = Field(ge=0)
    available_at_ns: int = Field(ge=0)

    @model_validator(mode="after")
    def _validate(self) -> Self:
        for name in ("cashflow_id", "fill_id", "campaign_id"):
            if not getattr(self, name).strip():
                raise ValueError(f"CASHFLOW: {name} cannot be empty")
        if self.available_at_ns < self.event_time_ns:
            raise ValueError("CASHFLOW: availability cannot precede the event")
        return self


class FundingRowEvent(BaseModel):
    """One native funding settlement row (signed amount, rate, currency)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    funding_id: str
    instrument_id: str
    amount: Amount
    currency: str
    rate: Amount
    event_time_ns: int = Field(ge=0)
    available_at_ns: int = Field(ge=0)

    @model_validator(mode="after")
    def _validate(self) -> Self:
        for name in ("funding_id", "instrument_id", "currency"):
            if not getattr(self, name).strip():
                raise ValueError(f"FUNDING: {name} cannot be empty")
        if self.available_at_ns < self.event_time_ns:
            raise ValueError("FUNDING: availability cannot precede the event")
        return self


class DecisionCutEvent(BaseModel):
    """One decision cut: the world state the adaptive chain evaluates at ``decision_time_ns``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    decision_time_ns: int = Field(ge=0)
    instrument_id: str
    venue_status: VenueOrderStatus
    position_presence: PositionPresence
    equity: Amount
    capacity: CapacityState
    position_quantity: Amount = Decimal("0")
    position_exposure: Amount = Decimal("0")
    committed_exposure: Amount = Decimal("0")
    opportunity_id: str | None = None

    @model_validator(mode="after")
    def _validate(self) -> Self:
        if not self.instrument_id.strip():
            raise ValueError("CUT: instrument_id cannot be empty")
        if self.position_exposure < 0:
            raise ValueError("CUT: position_exposure cannot be negative")
        if self.committed_exposure < 0:
            raise ValueError("CUT: committed_exposure cannot be negative")
        if self.opportunity_id is not None and not self.opportunity_id.strip():
            raise ValueError("CUT: opportunity_id cannot be blank")
        return self


class ReplayScript(BaseModel):
    """A deterministic replay fixture: the whole input to :func:`capture_replay`.

    No engine, no tape, no clock. Every layer is explicit because the lineage
    auditor refuses to infer a join from a neighbour.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    instrument_id: str
    as_of_ns: int = Field(ge=0)
    bars: tuple[BarEvent, ...] = ()
    opportunities: tuple[OpportunityEvent, ...] = ()
    native_decisions: tuple[NativeDecisionEvent, ...] = ()
    orders: tuple[NativeOrderEvent, ...] = ()
    protection: tuple[ProtectionOrderEvent, ...] = ()
    fills: tuple[FillEvent, ...] = ()
    cashflows: tuple[CashflowEvent, ...] = ()
    funding: tuple[FundingRowEvent, ...] = ()
    cuts: tuple[DecisionCutEvent, ...] = ()

    @model_validator(mode="after")
    def _validate_script(self) -> Self:
        if not self.instrument_id.strip():
            raise ValueError("SCRIPT: instrument_id cannot be empty")
        for layer, ids in (
            ("opportunity", tuple(event.opportunity_id for event in self.opportunities)),
            ("native decision", tuple(event.decision_id for event in self.native_decisions)),
            ("order", tuple(event.client_order_id for event in self.orders)),
            ("protection", tuple(event.client_order_id for event in self.protection)),
            ("fill", tuple(event.fill_id for event in self.fills)),
            ("cashflow", tuple(event.cashflow_id for event in self.cashflows)),
            ("funding", tuple(event.funding_id for event in self.funding)),
        ):
            repeated = _duplicates(ids)
            if repeated:
                raise ValueError(f"SCRIPT: duplicate {layer} id(s) {list(repeated)}")
        cut_times = tuple(cut.decision_time_ns for cut in self.cuts)
        if len(set(cut_times)) != len(cut_times):
            repeated_times = sorted({value for value in cut_times if cut_times.count(value) > 1})
            raise ValueError(
                "SCRIPT: decision cuts need distinct decision_time_ns "
                f"(repeated {[str(value) for value in repeated_times]}); two cuts at one "
                "instant have no replay order"
            )
        return self


# --------------------------------------------------------------------------- #
# signature and verdict
# --------------------------------------------------------------------------- #
class ReplaySignature(BaseModel):
    """Frozen digest set of one replay, bound to the policy state that produced it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    policy_state: PolicyState
    opportunity: str | None = None
    orders: str | None = None
    protection: str | None = None
    fills: str | None = None
    funding: str | None = None
    lineage: str | None = None
    #: Adaptive stream; populated only by a policy-on shadow replay.
    decisions: str | None = None
    #: The produced decision ids, in replay order, so a consumer can enumerate them.
    decision_ids: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _validate_signature(self) -> Self:
        for stream in STREAMS:
            digest = getattr(self, stream)
            if digest is not None and not _is_digest(digest):
                raise ValueError(f"SIGNATURE: blank or malformed digest for stream {stream!r}")
        if len(set(self.decision_ids)) != len(self.decision_ids):
            raise ValueError("SIGNATURE: duplicate decision ids")
        if self.policy_state == POLICY_OFF:
            if self.decisions is not None or self.decision_ids:
                raise ValueError(
                    "SIGNATURE: a policy-off replay carries no adaptive decision stream"
                )
        elif self.decisions is None or not self.decision_ids:
            raise ValueError(
                "SIGNATURE: a shadow replay must populate the adaptive decision stream"
            )
        return self

    def digest(self, stream: str) -> str | None:
        """Digest of ``stream``, or ``None`` when the stream was not captured."""
        if stream not in STREAMS:
            raise ValueError(f"unknown replay stream {stream!r}")
        value: str | None = getattr(self, stream)
        return value

    def missing(self) -> tuple[str, ...]:
        """Streams with no digest, in comparison order."""
        return tuple(stream for stream in STREAMS if self.digest(stream) is None)

    def native_missing(self) -> tuple[str, ...]:
        """Native streams with no digest; a baseline needs every one of them."""
        return tuple(stream for stream in NATIVE_STREAMS if self.digest(stream) is None)


class ParityVerdict(ParityGateResult):
    """Differential verdict over the native streams, plus the state policy applied.

    Reuses the resource-baseline gate vocabulary (``GateStatus``/``StreamVerdict``):
    ``FAIL`` means observed drift, ``BLOCKED`` means no evidence either way, and
    only six matching native streams from a policy-off reference and a policy-off
    candidate can be ``PASS``.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    reference_policy_state: PolicyState
    candidate_policy_state: PolicyState


# --------------------------------------------------------------------------- #
# capture
# --------------------------------------------------------------------------- #
def _opportunity_payload(events: ReplayScript) -> list[list[object]]:
    ordered = sorted(events.opportunities, key=lambda row: (row.selected_ns, row.opportunity_id))
    return [
        [
            row.opportunity_id,
            row.campaign_id,
            row.instrument_id,
            row.direction,
            row.lifecycle.value,
            row.selected_ns,
            row.available_at_ns,
        ]
        for row in ordered
    ]


def _orders_payload(events: ReplayScript) -> list[list[object]]:
    ordered = sorted(events.orders, key=lambda row: (row.submitted_ns, row.client_order_id))
    return [
        [
            row.client_order_id,
            row.decision_id,
            row.campaign_id,
            row.instrument_id,
            row.side,
            row.order_type,
            _amount(row.quantity),
            _amount(row.price),
            row.submitted_ns,
            row.available_at_ns,
        ]
        for row in ordered
    ]


def _protection_payload(events: ReplayScript) -> list[list[object]]:
    ordered = sorted(events.protection, key=lambda row: (row.submitted_ns, row.client_order_id))
    return [
        [
            row.client_order_id,
            row.parent_client_order_id,
            row.kind,
            row.side,
            _amount(row.trigger_price),
            _amount(row.quantity),
            row.submitted_ns,
            row.available_at_ns,
        ]
        for row in ordered
    ]


def _fills_payload(events: ReplayScript) -> list[list[object]]:
    ordered = sorted(events.fills, key=lambda row: (row.event_time_ns, row.fill_id))
    return [
        [
            row.fill_id,
            row.client_order_id,
            row.decision_id,
            row.campaign_id,
            row.instrument_id,
            _amount(row.quantity),
            _amount(row.price),
            _amount(row.fee_amount),
            row.fee_currency,
            row.event_time_ns,
            row.available_at_ns,
        ]
        for row in ordered
    ]


def _funding_payload(events: ReplayScript) -> list[list[object]]:
    ordered = sorted(events.funding, key=lambda row: (row.event_time_ns, row.funding_id))
    return [
        [
            row.funding_id,
            row.instrument_id,
            _amount(row.amount),
            row.currency,
            _amount(row.rate),
            row.event_time_ns,
            row.available_at_ns,
        ]
        for row in ordered
    ]


def _lineage_chain(events: ReplayScript) -> LineageChain:
    """The fixture's five explicit layers; joins are ids, never inference."""
    return LineageChain(
        opportunities=tuple(
            LineageOpportunity(
                opportunity_id=row.opportunity_id,
                campaign_id=row.campaign_id,
                instrument_id=row.instrument_id,
                created_ns=row.selected_ns,
                available_ns=row.available_at_ns,
            )
            for row in events.opportunities
        ),
        decisions=tuple(
            LineageDecision(
                decision_id=row.decision_id,
                opportunity_id=row.opportunity_id,
                campaign_id=row.campaign_id,
                instrument_id=row.instrument_id,
                decision_time_ns=row.decision_time_ns,
                available_at_ns=row.available_at_ns,
            )
            for row in events.native_decisions
        ),
        orders=tuple(
            LineageOrder(
                order_id=row.client_order_id,
                decision_id=row.decision_id,
                campaign_id=row.campaign_id,
                instrument_id=row.instrument_id,
                event_time_ns=row.submitted_ns,
                available_at_ns=row.available_at_ns,
            )
            for row in events.orders
        ),
        fills=tuple(
            LineageFill(
                fill_id=row.fill_id,
                order_id=row.client_order_id,
                decision_id=row.decision_id,
                campaign_id=row.campaign_id,
                instrument_id=row.instrument_id,
                event_time_ns=row.event_time_ns,
                available_at_ns=row.available_at_ns,
            )
            for row in events.fills
        ),
        cashflows=tuple(
            LineageCashflow(
                cashflow_id=row.cashflow_id,
                fill_id=row.fill_id,
                campaign_id=row.campaign_id,
                event_time_ns=row.event_time_ns,
                available_at_ns=row.available_at_ns,
                amount=row.amount,
            )
            for row in events.cashflows
        ),
    )


def _lineage_certification(events: ReplayScript) -> LineageCertification:
    """Certify the fixture's lineage at its declared cut; never a stale ``COMPLETE``."""
    return certify(_lineage_chain(events), as_of_ns=events.as_of_ns)


def _lineage_payload(events: ReplayScript) -> dict[str, object]:
    certification = _lineage_certification(events)
    if certification.verdict is not CompletenessVerdict.COMPLETE:
        raise LineageNotCertifiable(
            f"lineage verdict is {certification.verdict.value}; a baseline needs COMPLETE",
            reasons=tuple(violation.code.value for violation in certification.violations),
        )
    return {
        "verdict": certification.verdict.value,
        "provenance_digest": certification.provenance_digest,
        "joins_checked": certification.joins_checked,
        "campaign_ids": list(certification.campaign_ids),
        "violations": [violation.code.value for violation in certification.violations],
    }


def _native_digests(events: ReplayScript) -> dict[str, str]:
    """The six native digests, computed from the frozen fixture only."""
    return {
        "opportunity": _stream_digest("opportunity", _opportunity_payload(events)),
        "orders": _stream_digest("orders", _orders_payload(events)),
        "protection": _stream_digest("protection", _protection_payload(events)),
        "fills": _stream_digest("fills", _fills_payload(events)),
        "funding": _stream_digest("funding", _funding_payload(events)),
        "lineage": _stream_digest("lineage", _lineage_payload(events)),
    }


@dataclass(slots=True)
class _BeliefFactsView:
    """``v8_next.economics.policy.BeliefView`` over a belief snapshot.

    Not frozen on purpose: the injected protocol declares settable attributes, so a
    frozen dataclass does not type-check as a view here. Instances are constructed
    per cut and never mutated.
    """

    version_hash: str
    regime: str
    uncertainty: Decimal
    stale: bool
    abstain: bool
    as_of_ns: int


@dataclass(slots=True)
class _OpportunityFactsView:
    """``v8_next.economics.policy.OpportunityView`` over a fixture selection."""

    opportunity_id: str
    lifecycle: OpportunityLifecycle
    as_of_ns: int


@dataclass(slots=True)
class _StanceView:
    """``v8_next.economics.policy.ExpertStanceView`` derived from the belief."""

    expert_id: str
    stance: str
    weight: Decimal


def _stance(direction: str) -> str:
    if direction == "UP":
        return SUPPORT
    if direction == "DOWN":
        return CONTRADICT
    return ABSTAIN


def _evidence(belief: MarketBeliefV1) -> tuple[_StanceView, ...]:
    """Belief -> evidence: two declared witnesses, no fitted weights.

    The structure witness votes with the window direction at a fixed weight; the
    regime witness votes with the named regime at that regime's score. A regime
    word the policy would read as a probability is never produced here: scores are
    called scores, and an unnamed regime abstains.
    """
    regime = belief.regime_label.value
    if regime == "TREND":
        regime_stance = SUPPORT
    elif regime in {"RANGE", "SHOCK"}:
        regime_stance = CONTRADICT
    else:
        regime_stance = ABSTAIN
    regime_weight = belief.regime.score(belief.regime_label).quantize(WEIGHT_QUANTUM)
    return (
        _StanceView(
            expert_id=_STRUCTURE_EXPERT,
            stance=_stance(belief.structure.direction.value),
            weight=STRUCTURE_WEIGHT,
        ),
        _StanceView(expert_id=_REGIME_EXPERT, stance=regime_stance, weight=regime_weight),
    )


def _belief_view(belief: MarketBeliefV1) -> _BeliefFactsView:
    """Belief -> the policy's coarse ``(stale, abstain)`` facts, fail-closed.

    Only a ``FRESH`` belief of ``OK`` quality is usable evidence. Every other
    status is unusable: ``UNKNOWN`` (the aggregate quality is unreadable while a
    regime is still named) is not a milder ``STALE`` -- presenting it as fresh
    let the policy emit a bare ``HOLD`` on unreadable evidence. ``ABSTAIN`` keeps
    its own, more specific branch, so it is not collapsed into ``stale`` here.
    """
    abstain = belief.status is BeliefStatus.ABSTAIN
    stale = not abstain and (
        belief.status is not BeliefStatus.FRESH or belief.quality is not QualityState.OK
    )
    return _BeliefFactsView(
        version_hash=belief.version_hash,
        regime=belief.regime_label.value,
        uncertainty=UNCERTAINTY_BANDS[belief.uncertainty],
        stale=stale,
        abstain=abstain,
        as_of_ns=belief.max_input_available_at,
    )


def _opportunity_view(events: ReplayScript, cut: DecisionCutEvent) -> _OpportunityFactsView | None:
    """The latest selection of the cut's opportunity that was available at the cut."""
    if cut.opportunity_id is None:
        return None
    available = [
        row
        for row in events.opportunities
        if row.opportunity_id == cut.opportunity_id and row.available_at_ns <= cut.decision_time_ns
    ]
    if not available:
        return None
    latest = max(available, key=lambda row: (row.available_at_ns, row.selected_ns))
    return _OpportunityFactsView(
        opportunity_id=latest.opportunity_id,
        lifecycle=latest.lifecycle,
        as_of_ns=latest.available_at_ns,
    )


def _belief_at(
    events: ReplayScript,
    cut: DecisionCutEvent,
    previous: MarketBeliefV1 | None,
) -> tuple[MarketBeliefV1, ObservationView]:
    """Advance belief from only the bars that had closed by the cut (PIT by construction)."""
    observations = [
        bar.to_observation()
        for bar in events.bars
        if bar.instrument_id == cut.instrument_id and bar.end_ns <= cut.decision_time_ns
    ]
    view = build_view(observations, cut_time_ns=cut.decision_time_ns)
    context = assemble_context(
        {Timeframe.M15: view},
        instrument_id=cut.instrument_id,
        decision_time_ns=cut.decision_time_ns,
    )
    return advance(previous, view, context, now_ns=cut.decision_time_ns), view


def _input_refs(belief: MarketBeliefV1, view: ObservationView) -> tuple[DecisionInputRef, ...]:
    """The belief's own observation references, with the clock each became usable."""
    return tuple(
        DecisionInputRef(
            observation_id=observation_id,
            available_at_ns=view.require_observation(observation_id).available_at,
        )
        for observation_id in belief.input_observation_ids
    )


def _decision_request(
    events: ReplayScript, cut: DecisionCutEvent, belief: MarketBeliefV1
) -> DecisionRequest:
    """Assemble the policy request for one cut from PIT facts only."""
    return DecisionRequest(
        decision_time_ns=cut.decision_time_ns,
        instrument_id=cut.instrument_id,
        venue_status=cut.venue_status,
        position=PositionState(
            presence=cut.position_presence,
            quantity=cut.position_quantity,
            exposure=cut.position_exposure,
        ),
        portfolio=PortfolioState(
            equity=cut.equity,
            committed_exposure=cut.committed_exposure,
            capacity=cut.capacity,
        ),
        opportunity=_opportunity_view(events, cut),
        belief=_belief_view(belief),
        expert_stances=_evidence(belief),
    )


def shadow_decisions(events: ReplayScript) -> tuple[DecisionReceipt, ...]:
    """Run belief -> evidence -> policy -> receipt for every cut, in time order.

    Nothing here can construct an order: the only outputs are advisory decisions
    and immutable receipts. The ledger rejects an unverifiable or non-PIT receipt.

    A receipt archives the provenance that shaped the decision, not just its
    verdict: the active constraint set the policy reported (``VENUE_UNKNOWN``,
    ``STALE``, ``GROSS_EXPOSURE_EXCEEDED``, ...) is copied into
    :class:`ReceiptInputs`, so the receipt digest covers it. Dropping it left a
    receipt that could not explain why an action was unavailable.
    """
    if not events.cuts:
        raise ShadowNotExercised(
            "a shadow replay needs at least one decision cut; an empty run proves nothing"
        )
    policy = TraderDecisionPolicy()
    ledger = DecisionLedger()
    previous: MarketBeliefV1 | None = None
    for cut in sorted(events.cuts, key=lambda row: row.decision_time_ns):
        if cut.instrument_id != events.instrument_id:
            raise ShadowParityError(
                f"cut instrument {cut.instrument_id!r} is outside the script instrument "
                f"{events.instrument_id!r}",
                code="CUT_INSTRUMENT_MISMATCH",
                reasons=(f"CUT_INSTRUMENT_MISMATCH:{cut.instrument_id}",),
            )
        belief, view = _belief_at(events, cut, previous)
        previous = belief
        request = _decision_request(events, cut, belief)
        decision = policy.evaluate(request)
        explanation = policy.explain(request)
        ledger.append(
            decision,
            ReceiptInputs(inputs=_input_refs(belief, view), constraints=explanation.constraints),
        )
    return ledger.receipts()


def _decisions_digest(receipts: Sequence[DecisionReceipt]) -> str:
    """Content identity of the adaptive stream, in replay order (order-sensitive)."""
    return canonical_digest(
        f"{DIGEST_VERSION}.decisions",
        [[receipt.decision_id, receipt.digest] for receipt in receipts],
    )


def _signature(
    *,
    policy_state: PolicyState,
    native: Mapping[str, str],
    decisions: str | None,
    decision_ids: tuple[str, ...],
) -> ReplaySignature:
    """Assemble a signature from a native digest map plus the adaptive stream."""
    return ReplaySignature(
        policy_state=policy_state,
        opportunity=native["opportunity"],
        orders=native["orders"],
        protection=native["protection"],
        fills=native["fills"],
        funding=native["funding"],
        lineage=native["lineage"],
        decisions=decisions,
        decision_ids=decision_ids,
    )


def _sink_digest(session: NativeSession, *, moment: str) -> str:
    """Read the native sink's digest, refusing a sink that cannot be observed."""
    value = session.digest()
    if not isinstance(value, str) or not _is_digest(value):
        raise ShadowParityError(
            f"native session returns no usable digest {moment}",
            code="SHADOW_SINK_MALFORMED",
            reasons=(f"SHADOW_SINK_MALFORMED:{moment}",),
        )
    return value


def capture_replay(
    events: ReplayScript,
    *,
    policy_state: PolicyState,
    native_session: NativeSession | None = None,
    shadow_step: ShadowStep | None = None,
) -> ReplaySignature:
    """Capture the digest set of one replay of ``events`` under ``policy_state``.

    ``OFF`` never evaluates the adaptive chain and therefore carries no decision
    stream; ``native_session`` is unused there.

    ``SHADOW`` evaluates the chain against a live :class:`NativeSession` and
    compares that session's own digest before and after the pass. The sink is
    required: a digest pair over the frozen fixture cannot change, so it would be
    a gate that can never fail. ``shadow_step`` defaults to the production pass
    (:func:`shadow_decisions`) and exists so a test can inject a step that does
    reach for ``submit``/``cancel`` -- which is how the gate is proven to work.

    Capture fails closed -- a ``SHADOW`` script with no cuts is not evidence that
    shadow ran, a run with no observable sink proves nothing, and a fixture whose
    own lineage cannot be certified ``COMPLETE`` cannot be a baseline.
    """
    if not isinstance(events, ReplayScript):
        raise TypeError(f"expected ReplayScript, got {type(events).__name__}")
    if policy_state == POLICY_OFF:
        return _signature(
            policy_state=POLICY_OFF,
            native=_native_digests(events),
            decisions=None,
            decision_ids=(),
        )
    if policy_state != POLICY_SHADOW:
        raise ShadowParityError(
            f"unknown policy state {policy_state!r}; expected {POLICY_OFF!r} or {POLICY_SHADOW!r}",
            code="UNKNOWN_POLICY_STATE",
            reasons=(f"UNKNOWN_POLICY_STATE:{policy_state}",),
        )
    if native_session is None:
        raise ShadowParityError(
            "a shadow run needs an observable native sink; without one the "
            "zero-mutation gate cannot fail",
            code="SHADOW_SINK_MISSING",
            reasons=("SHADOW_SINK_MISSING",),
        )

    before = _sink_digest(native_session, moment="before")
    receipts = (
        shadow_decisions(events) if shadow_step is None else shadow_step(events, native_session)
    )
    after = _sink_digest(native_session, moment="after")
    if before != after:
        raise ShadowMutationError(
            "the adaptive chain changed the native sink during the shadow pass",
            reasons=("DIGEST_DRIFT:native_session",),
        )
    return _signature(
        policy_state=POLICY_SHADOW,
        native=_native_digests(events),
        decisions=_decisions_digest(receipts),
        decision_ids=tuple(receipt.decision_id for receipt in receipts),
    )


# --------------------------------------------------------------------------- #
# differential comparison
# --------------------------------------------------------------------------- #
def compare(baseline: ReplaySignature, candidate: ReplaySignature) -> ParityVerdict:
    """Differential gate over two signatures' six native streams.

    Precedence is fail-closed and mirrors ``resource_baseline.parity_gate``: an
    observed drift is ``FAIL``; otherwise a missing digest is ``BLOCKED``; only a
    policy-off reference compared with a policy-off candidate whose six native
    streams all match is ``PASS``. A shadow candidate is never certified here --
    it proves *non-mutation* against the reference, which is what
    :func:`assert_shadow_unmutated` exists for, and certifying it as the baseline
    parity result would conflate the two.
    """
    verdicts: list[StreamVerdict] = []
    drift: list[str] = []
    missing: list[str] = []
    for stream in NATIVE_STREAMS:
        base = baseline.digest(stream)
        cand = candidate.digest(stream)
        if base is not None and cand is not None and base != cand:
            reason = f"DIGEST_DRIFT:{stream}"
            drift.append(reason)
            verdicts.append(
                StreamVerdict(
                    stream=stream,
                    status=GateStatus.FAIL,
                    baseline_digest=base,
                    candidate_digest=cand,
                    reason=reason,
                )
            )
            continue
        if base is None or cand is None:
            absent = [
                side for side, value in (("baseline", base), ("candidate", cand)) if value is None
            ]
            reason = f"DIGEST_MISSING:{stream}:{'+'.join(absent)}"
            missing.append(reason)
            verdicts.append(
                StreamVerdict(
                    stream=stream,
                    status=GateStatus.BLOCKED,
                    baseline_digest=base,
                    candidate_digest=cand,
                    reason=reason,
                )
            )
            continue
        verdicts.append(
            StreamVerdict(
                stream=stream,
                status=GateStatus.PASS,
                baseline_digest=base,
                candidate_digest=cand,
            )
        )

    policy_reasons: list[str] = []
    if baseline.policy_state != POLICY_OFF:
        policy_reasons.append(f"REFERENCE_NOT_POLICY_OFF:{baseline.policy_state}")
    if candidate.policy_state != POLICY_OFF:
        policy_reasons.append(f"CANDIDATE_NOT_POLICY_OFF:{candidate.policy_state}")

    if drift:
        status = GateStatus.FAIL
    elif missing or policy_reasons:
        status = GateStatus.BLOCKED
    else:
        status = GateStatus.PASS
    return ParityVerdict(
        status=status,
        streams=tuple(verdicts),
        blocking_reasons=tuple([*drift, *missing, *policy_reasons]),
        reference_policy_state=baseline.policy_state,
        candidate_policy_state=candidate.policy_state,
    )


def assert_shadow_unmutated(reference: ReplaySignature, shadow: ReplaySignature) -> ReplaySignature:
    """Require a shadow run to leave every native stream exactly as the baseline did.

    Fails closed on: a reference that is not policy-off, a candidate that is not
    shadow, an unpopulated decision stream (a no-op proves nothing), and any
    native digest that is missing or different. Returns ``shadow`` for chaining.
    """
    reasons: list[str] = []
    if reference.policy_state != POLICY_OFF:
        reasons.append(f"REFERENCE_NOT_POLICY_OFF:{reference.policy_state}")
    if shadow.policy_state != POLICY_SHADOW:
        reasons.append(f"CANDIDATE_NOT_SHADOW:{shadow.policy_state}")
    if shadow.decisions is None:
        reasons.append("SHADOW_DECISION_STREAM_MISSING")
    if not shadow.decision_ids:
        reasons.append("SHADOW_DECISION_IDS_EMPTY")
    for stream in NATIVE_STREAMS:
        base = reference.digest(stream)
        cand = shadow.digest(stream)
        if base is None or cand is None:
            absent = [
                side for side, value in (("baseline", base), ("shadow", cand)) if value is None
            ]
            reasons.append(f"DIGEST_MISSING:{stream}:{'+'.join(absent)}")
        elif base != cand:
            reasons.append(f"DIGEST_DRIFT:{stream}")
    if reasons:
        raise ShadowMutationError(
            f"shadow run is not native-invariant: {', '.join(reasons)}", reasons=reasons
        )
    return shadow


# --------------------------------------------------------------------------- #
# pinned references
# --------------------------------------------------------------------------- #
def pin(signature: ReplaySignature) -> str:
    """Canonical JSON reference of ``signature``; byte-stable across round trips."""
    return canonical(signature.model_dump(mode="json"))


def _load_reference(reference: str | Mapping[str, object] | ReplaySignature) -> ReplaySignature:
    if isinstance(reference, ReplaySignature):
        return reference
    if isinstance(reference, str):
        try:
            payload: object = json.loads(reference)
        except json.JSONDecodeError as exc:
            raise PinnedReferenceMalformed(f"reference is not JSON: {exc}") from exc
    elif isinstance(reference, Mapping):
        payload = dict(reference)
    else:
        raise TypeError(
            f"reference must be a ReplaySignature, JSON text or mapping, got "
            f"{type(reference).__name__}"
        )
    if not isinstance(payload, Mapping):
        raise PinnedReferenceMalformed("reference document must be a JSON object")
    try:
        return ReplaySignature.model_validate(dict(payload))
    except ValidationError as exc:
        raise PinnedReferenceMalformed(f"reference is not a ReplaySignature: {exc}") from exc


def assert_pinned(
    signature: ReplaySignature,
    reference: str | Mapping[str, object] | ReplaySignature,
) -> None:
    """Assert ``signature`` reproduces the pinned reference byte for byte.

    Drift names every stream that moved, so a one-tick price change cannot pass as
    "a rounding difference". Both sides must be policy-off baselines: a shadow run
    is checked by :func:`assert_shadow_unmutated`, not certified against a pin.
    """
    pinned = _load_reference(reference)
    if pinned.policy_state != POLICY_OFF:
        raise PinnedReferenceDrift(
            "the pinned reference is not a policy-off baseline",
            reasons=(f"REFERENCE_NOT_POLICY_OFF:{pinned.policy_state}",),
        )
    if signature.policy_state != POLICY_OFF:
        raise PinnedReferenceDrift(
            "a shadow signature cannot be certified against a policy-off pin",
            reasons=(f"CANDIDATE_NOT_POLICY_OFF:{signature.policy_state}",),
        )
    if pin(signature) == pin(pinned):
        return
    drifted: list[str] = []
    for stream in STREAMS:
        if signature.digest(stream) != pinned.digest(stream):
            drifted.append(stream)
    if signature.decision_ids != pinned.decision_ids:
        drifted.append("decision_ids")
    if not drifted:  # pragma: no cover - canonical JSON equality implies field equality
        drifted.append("canonical_json")
    reasons = tuple(f"PINNED_DRIFT:{name}" for name in drifted)
    raise PinnedReferenceDrift(
        f"pinned reference drift in {', '.join(drifted)}",
        reasons=reasons,
    )
