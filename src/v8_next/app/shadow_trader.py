"""Adaptive Shadow Trader — the end-to-end vertical slice (M3-07 / #28).

What this module is
-------------------
One thin orchestrator over the layers that already exist. It adds no second
engine, no second order-management system and no second ledger; it wires the
delivered contracts in the order a live decision goes through them:

``PIT observations`` -> ``MarketBeliefV1`` -> ``28 experts`` -> ``OpportunityBook``
-> ``TraderDecision`` -> ``EntryIntent`` / ``PositionAdvisory`` -> ``portfolio
challenger`` -> ``DecisionReceipt``.

The V8 native replay stays the validated control arm: the caller supplies a
frozen :class:`~v8_next.evaluation.shadow_parity.ReplayScript` and this module
captures its six native digests *before* and *after* the adaptive chain with
:func:`~v8_next.evaluation.shadow_parity.capture_replay` (policy ``OFF``), then
requires the two captures to be identical and the resulting shadow signature to
pass :func:`~v8_next.evaluation.shadow_parity.assert_shadow_unmutated`. Every
novel adaptive decision is recorded in shadow only.

What it deliberately is not
---------------------------
* **Not an order path.** The module imports no execution adapter, no broker, no
  order type and no venue client (pinned by an AST test). ``ShadowTrace`` carries
  ``native_side_effects`` as a structurally empty tuple: there is no field
  through which an order could be reported, and the model refuses a non-empty
  value.
* **Not a risk authority.** The hard-risk plane is an *input*
  (:class:`RiskObservation`) that this slice reads and reports. It never grants
  permission: an ``UNKNOWN`` or ``DENIED`` reading blocks the portfolio plane
  outright, and every proposal keeps ``advisory_only`` pinned, with
  ``requires_risk_permission`` set where the proposal type defines one.
* **Not an entry veto.** ``WAIT``/``ENTER``/``HOLD``/``REDUCE``/``EXIT``/
  ``ABSTAIN`` are all first class: an open position is *managed* (thesis
  evaluation + advisory), and the portfolio plane allocates across the cut's
  simultaneous entry candidates instead of rejecting them one at a time.

Event model
-----------
A script is a sequence of typed :class:`MarketEvent` records — position, venue,
protection, hard-risk, opportunity, bar, quote/mark, funding and explicit
decision-cut events. The run canonicalises the sequence to a total order
``(event_time_ns, kind rank, instrument, payload digest)``; that ordering is part
of the declared contract, so *reordering the caller's independent events yields
an equal trace*, and an ambiguous duplicate fails closed rather than being
resolved by input order.

Point-in-time discipline is not re-implemented here: market data goes through
:func:`~v8_next.domain.observation.build_view` (which rejects future records and
keeps them typed), position/venue/protection/risk readings are selected by
``available_at_ns <= cut``, the opportunity view comes from
:meth:`~v8_next.opportunities.book.OpportunityBook.snapshot`, and every step
records the newest input it used so ``max_input_available_at <= cut`` can be
asserted over the whole trace.

Evidence is bound to one decision clock
---------------------------------------
:func:`~v8_next.experts.evidence.produce_evidence` requires the belief to name
the *same* decision time as the frame. Belief advancement re-derives at every cut
(its dependency fingerprint includes the decision time), so in normal operation
the two always agree. The slice keeps the guard anyway: a belief stamped for
another instant, or a cut with no closed bar at all, cannot produce
context-bound evidence, so it records the shortfall explicitly
(``evidence_coverage = 0``) and lets the policy fail closed instead of crashing
mid-trace.

Witness weights are not fitted: each evidence row contributes the determinacy it
already carries (``1`` when the expert asserted, ``0`` when it abstained), so an
abstaining witness is counted in coverage and cannot vote.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum
from types import MappingProxyType
from typing import Final, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator

from v8_next.belief import BeliefStatus, MarketBeliefV1, advance
from v8_next.domain.decision import (
    DecisionAction,
    IntentKind,
    OpportunityLifecycle,
    ReasonCode,
    TraderDecision,
    VenueOrderStatus,
    lifecycle_of,
)
from v8_next.domain.horizons import HorizonContext, Timeframe, assemble_context
from v8_next.domain.market import Candle, CausalFrame, build_candle_dataframe
from v8_next.domain.observation import (
    Observation,
    ObservationKind,
    ObservationView,
    QualityState,
    build_view,
)
from v8_next.economics.decisions import Opportunity
from v8_next.economics.entry import (
    EntryEvaluation,
    EntryIntent,
    FrictionStatus,
    MarketQuote,
    OrderPolicy,
    build_intent,
    evaluate_entry,
)
from v8_next.economics.legality import PositionPresence
from v8_next.economics.management import (
    ManagementDecision,
    ProtectionState,
    evaluate_management,
)
from v8_next.economics.policy import (
    CapacityState,
    DecisionRequest,
    PolicyExplanation,
    PortfolioState,
    PositionState,
    StrictDecimal,
    TraderDecisionPolicy,
)
from v8_next.economics.portfolio_challenger import (
    AllocationCandidate,
    ChallengeResult,
    allocate_challenger,
)
from v8_next.economics.thesis import (
    ExpertEvidence as ThesisEvidence,
)
from v8_next.economics.thesis import (
    InvalidationKind,
    InvalidationRule,
    PositionEvaluation,
    PositionSnapshot,
    PositionThesis,
    StopKind,
    TargetPlan,
    ThesisBelief,
    build_thesis,
    evaluate_position,
)
from v8_next.economics.thesis import (
    StanceKind as ThesisStanceKind,
)
from v8_next.evaluation.shadow_parity import (
    NATIVE_STREAMS,
    POLICY_OFF,
    POLICY_SHADOW,
    UNCERTAINTY_BANDS,
    ReplayScript,
    ReplaySignature,
    ShadowNotExercised,
    assert_shadow_unmutated,
    capture_replay,
    pin,
)
from v8_next.evaluation.store import canonical
from v8_next.experts.cache import FeatureCache
from v8_next.experts.evidence import EvidenceBundle, produce_evidence
from v8_next.opportunities.book import EpisodeSnapshot, OpportunityBook, OpportunitySnapshot
from v8_next.opportunities.models import (
    EconomicExposureStructure,
    ExposureDirection,
    OpportunityRecord,
    OpportunityStatus,
)
from v8_next.opportunities.recurrent import (
    RecurrentOpportunityState,
    bootstrap,
)
from v8_next.opportunities.recurrent import (
    advance as advance_opportunity,
)
from v8_next.telemetry.decisions import (
    DecisionInputRef,
    DecisionLedger,
    DecisionReceipt,
    ReceiptInputs,
)
from v8_next.telemetry.identity import canonical_digest

__all__ = [
    "ALLOCATION_FRACTION",
    "BarUpdate",
    "CutUpdate",
    "DEFAULT_FEE_BPS",
    "DEFAULT_LOT_STEP",
    "DEFAULT_TICK_SIZE",
    "ENTRY_BAND_BPS",
    "ENTRY_TTL_NS",
    "EventKind",
    "FundingUpdate",
    "HardRiskLimits",
    "MarketEvent",
    "OpportunityUpdate",
    "ParityBlock",
    "PositionUpdate",
    "ProtectionUpdate",
    "QuoteUpdate",
    "RiskObservation",
    "RiskStatus",
    "RiskUpdate",
    "SLICE_VERSION",
    "ShadowScriptError",
    "ShadowTrace",
    "ShadowTrader",
    "TraceStep",
    "VenueUpdate",
]

#: Schema/behaviour identity of this slice. Bumped when the wiring changes meaning.
SLICE_VERSION: Final[str] = "v9.shadow-trader-slice.1"

#: Declared policy constants of the slice. None is fitted to data: each is a
#: documented behaviour of the shadow product, and changing one changes the trace.
ENTRY_BAND_BPS: Final[Decimal] = Decimal("25")
ENTRY_TTL_NS: Final[int] = 15 * 60 * 1_000_000_000
DEFAULT_FEE_BPS: Final[Decimal] = Decimal("4")
DEFAULT_LOT_STEP: Final[Decimal] = Decimal("0.001")
DEFAULT_TICK_SIZE: Final[Decimal] = Decimal("0.01")
#: An account/risk reading is only good for this long. Older is *unknown*, which
#: blocks entry and the portfolio plane rather than silently spending stale capital.
RISK_MAX_AGE_NS: Final[int] = 2 * 60 * 60 * 1_000_000_000
ALLOCATION_FRACTION: Final[Decimal] = Decimal("0.10")
SIZE_POLICY_ID: Final[str] = "shadow-size-policy-v1"
VENUE_VERSION: Final[str] = "shadow-venue-v1"
BPS: Final[Decimal] = Decimal("10000")
ZERO: Final[Decimal] = Decimal("0")
MONEY_QUANTUM: Final[Decimal] = Decimal("0.00000001")

#: Reported when an entry proposal exists but the portfolio plane was not
#: attempted at all (no fresh risk reading, or a reading that is not ``AVAILABLE``).
ALLOCATION_NOT_ATTEMPTED: Final[str] = "NOT_ATTEMPTED"

#: Reason recorded when the newest risk reading is older than its useful age.
STALE_RISK_REASON: Final[str] = "STALE_RISK_READING"
#: Reason recorded when the script carries no risk reading for the instrument.
MISSING_RISK_REASON: Final[str] = "NO_RISK_READING"

#: There is no code path in this module that can produce one of these.
NO_NATIVE_SIDE_EFFECTS: Final[tuple[()]] = ()


class ShadowScriptError(ValueError):
    """The script, or the trace it produced, violates the slice contract."""

    code: str = "SHADOW_SLICE_ERROR"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        resolved = code or type(self).code
        super().__init__(f"{resolved}: {message}")
        self.code = resolved


# --------------------------------------------------------------------------- #
# event vocabulary
# --------------------------------------------------------------------------- #
class EventKind(StrEnum):
    """Channels a shadow script can carry, in declared ordering rank."""

    POSITION = "POSITION"
    VENUE = "VENUE"
    PROTECTION = "PROTECTION"
    RISK = "RISK"
    OPPORTUNITY = "OPPORTUNITY"
    BAR = "BAR"
    QUOTE = "QUOTE"
    FUNDING = "FUNDING"
    CUT = "CUT"


#: Total order applied to a caller-supplied script. State readings at an instant
#: are applied before the observations and the cut taken at the same instant, so a
#: status promotion is never evaluated after the candle that witnessed it.
_KIND_RANK: Final[Mapping[EventKind, int]] = MappingProxyType(
    {kind: rank for rank, kind in enumerate(EventKind)}
)


class RiskStatus(StrEnum):
    """The hard-risk plane's reading. Never a permission this slice can grant."""

    AVAILABLE = "AVAILABLE"
    DENIED = "DENIED"
    UNKNOWN = "UNKNOWN"


_CAPACITY_BY_RISK: Final[Mapping[RiskStatus, CapacityState]] = MappingProxyType(
    {
        RiskStatus.AVAILABLE: CapacityState.AVAILABLE,
        RiskStatus.DENIED: CapacityState.FULL,
        RiskStatus.UNKNOWN: CapacityState.UNKNOWN,
    }
)

_BAR_BUILDABLE: Final[frozenset[QualityState]] = frozenset({QualityState.OK, QualityState.STALE})

#: Modes in which an entry proposal is a real, priceable proposal.
_PROPOSED_ENTRY_MODES: Final[frozenset[str]] = frozenset({"MARKET_NOW", "LIMIT_WAIT"})


class HardRiskLimits(BaseModel):
    """Account and venue constraint reading supplied by the risk plane.

    Every field is optional on purpose: ``None`` is *unknown*, and every consumer
    downstream treats unknown as a denial. There is no defaulted equity, cap or
    lot grid anywhere in this slice.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    equity: StrictDecimal | None = None
    committed_exposure: StrictDecimal | None = None
    max_gross_exposure: StrictDecimal | None = None
    max_per_sector: StrictDecimal | None = None
    risk_budget: StrictDecimal | None = None
    sector: str | None = None
    correlation_group: str | None = None
    step_size: StrictDecimal | None = None
    min_size: StrictDecimal | None = None
    max_size: StrictDecimal | None = None
    tick_size: StrictDecimal | None = None
    min_notional: StrictDecimal | None = None


class RiskObservation(BaseModel):
    """One read-only hard-risk verdict at one clock reading."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: RiskStatus
    reason: str = Field(min_length=1)
    as_of_ns: StrictInt = Field(ge=0)
    limits: HardRiskLimits = Field(default_factory=HardRiskLimits)
    #: An observation of the risk plane, never a grant.
    advisory_only: Literal[True] = True


class MarketEvent(BaseModel):
    """Base of every scripted event: a fact time and the time it became knowable."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: EventKind
    event_time_ns: StrictInt = Field(ge=0)
    available_at_ns: StrictInt = Field(ge=0)

    @model_validator(mode="after")
    def _pit_consistent(self) -> Self:
        if self.available_at_ns < self.event_time_ns:
            raise ValueError("availability cannot precede the event")
        return self


class PositionUpdate(MarketEvent):
    """One native position reading (open or close)."""

    kind: Literal[EventKind.POSITION] = EventKind.POSITION
    instrument_id: str = Field(min_length=1)
    presence: PositionPresence
    quantity: StrictDecimal = ZERO
    exposure: StrictDecimal = ZERO
    average_price: StrictDecimal = ZERO
    side: Literal["LONG", "SHORT"] | None = None
    initial_stop_price: StrictDecimal | None = None
    native_position_id: str | None = None
    elapsed_bars: StrictInt = Field(default=0, ge=0)

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        empty = self.quantity == ZERO and self.exposure == ZERO
        if self.exposure < ZERO:
            raise ValueError("exposure cannot be negative")
        if self.presence is PositionPresence.OPEN:
            if empty:
                raise ValueError("an OPEN reading must carry quantity or exposure")
            if self.side is None or self.initial_stop_price is None:
                raise ValueError("an OPEN reading must carry side and initial stop")
            if self.native_position_id is None or not self.native_position_id.strip():
                raise ValueError("an OPEN reading must name the native position")
            if self.average_price <= ZERO:
                raise ValueError("an OPEN reading must carry a positive average price")
        else:
            if not empty:
                raise ValueError(f"{self.presence.value} reading cannot carry exposure")
            if self.side is not None or self.initial_stop_price is not None:
                raise ValueError(f"{self.presence.value} reading cannot carry thesis fields")
        return self


class VenueUpdate(MarketEvent):
    """One venue status reading, never a request."""

    kind: Literal[EventKind.VENUE] = EventKind.VENUE
    instrument_id: str = Field(min_length=1)
    status: VenueOrderStatus


class ProtectionUpdate(MarketEvent):
    """One native protection reading."""

    kind: Literal[EventKind.PROTECTION] = EventKind.PROTECTION
    instrument_id: str = Field(min_length=1)
    state: ProtectionState
    stop_price: StrictDecimal | None = None

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if self.state is ProtectionState.PROTECTED:
            if self.stop_price is None or self.stop_price <= ZERO:
                raise ValueError("a PROTECTED reading must name its stop price")
        elif self.stop_price is not None:
            raise ValueError(f"{self.state.value} reading cannot name a stop price")
        return self


class RiskUpdate(MarketEvent):
    """One hard-risk verdict."""

    kind: Literal[EventKind.RISK] = EventKind.RISK
    instrument_id: str = Field(min_length=1)
    observation: RiskObservation

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if self.observation.as_of_ns > self.available_at_ns:
            raise ValueError("a risk reading cannot be available before its snapshot")
        return self


class OpportunityUpdate(MarketEvent):
    """A market-created opportunity episode, or a reassessment of it."""

    kind: Literal[EventKind.OPPORTUNITY] = EventKind.OPPORTUNITY
    instrument_id: str = Field(min_length=1)
    direction: ExposureDirection
    entry_price: StrictDecimal = Field(gt=ZERO)
    as_of_time_ns: StrictInt = Field(ge=0)
    valid_until_ns: StrictInt = Field(ge=0)
    status: OpportunityStatus
    stop_price: StrictDecimal | None = None
    target_price: StrictDecimal | None = None
    expected_horizon_bars: StrictInt = Field(default=24, ge=1)
    witness_id: str | None = None

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if self.as_of_time_ns > self.event_time_ns:
            raise ValueError("an episode cannot be created after the event that reports it")
        if self.valid_until_ns < self.as_of_time_ns:
            raise ValueError("valid_until_ns cannot precede as_of_time_ns")
        for name in ("stop_price", "target_price"):
            value = getattr(self, name)
            if value is not None and value <= ZERO:
                raise ValueError(f"{name} must be positive")
        if self.witness_id is not None and not self.witness_id.strip():
            raise ValueError("witness_id cannot be blank")
        return self

    def to_record(self) -> OpportunityRecord:
        """The canonical episode record; its id is minted by the model, never here."""
        exposure = EconomicExposureStructure.single_perp(
            symbol=self.instrument_id,
            factor=self.instrument_id,
            venue="shadow-venue",
            settlement="USDT",
            direction=self.direction,
        )
        return OpportunityRecord.create(
            exposure=exposure,
            instrument_id=self.instrument_id,
            direction=self.direction,
            entry_price=self.entry_price,
            as_of_time_ns=self.as_of_time_ns,
            valid_until_ns=self.valid_until_ns,
            stop_price=self.stop_price,
            target_price=self.target_price,
            expected_horizon_bars=self.expected_horizon_bars,
            status=self.status,
        )

    def canonical_witness(self) -> str:
        if self.witness_id is not None:
            return self.witness_id
        return f"shadow-witness:{self.instrument_id}:{self.as_of_time_ns}"


class BarUpdate(MarketEvent):
    """One completed bar."""

    kind: Literal[EventKind.BAR] = EventKind.BAR
    instrument_id: str = Field(min_length=1)
    start_ns: StrictInt = Field(ge=0)
    end_ns: StrictInt = Field(ge=0)
    open: StrictDecimal
    high: StrictDecimal
    low: StrictDecimal
    close: StrictDecimal
    volume: StrictDecimal
    sequence: StrictInt = Field(default=0, ge=0)
    quality: QualityState = QualityState.OK

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if self.end_ns <= self.start_ns:
            raise ValueError("end_ns must be after start_ns")
        if self.high < self.low:
            raise ValueError("high cannot be below low")
        if not (self.low <= self.open <= self.high and self.low <= self.close <= self.high):
            raise ValueError("open/close must lie inside [low, high]")
        if min(self.open, self.high, self.low, self.close) <= ZERO:
            raise ValueError("prices must be positive")
        if self.volume < ZERO:
            raise ValueError("volume cannot be negative")
        if self.quality in _BAR_BUILDABLE:
            self.to_candle()
        return self

    def source_hash(self) -> str:
        return f"{SLICE_VERSION}-bar-{self.instrument_id}-{self.start_ns}"

    def to_candle(self) -> Candle:
        return Candle(
            instrument_id=self.instrument_id,
            start_ns=self.start_ns,
            end_ns=self.end_ns,
            open=self.open,
            high=self.high,
            low=self.low,
            close=self.close,
            volume=self.volume,
            received_ns=self.available_at_ns,
            available_ns=self.available_at_ns,
            source_hash=self.source_hash(),
        )

    def to_observation(self) -> Observation:
        return Observation(
            instrument_id=self.instrument_id,
            kind=ObservationKind.BAR,
            event_time=self.start_ns,
            available_at=self.available_at_ns,
            received_at=self.available_at_ns,
            published_at=self.available_at_ns,
            source="shadow-slice",
            sequence=self.sequence,
            quality=self.quality,
            payload={
                "start_ns": self.start_ns,
                "end_ns": self.end_ns,
                "open": str(self.open),
                "high": str(self.high),
                "low": str(self.low),
                "close": str(self.close),
                "volume": str(self.volume),
                "source_hash": self.source_hash(),
            },
        )


class QuoteUpdate(MarketEvent):
    """One price observation (mark and, when the venue states it, the touch)."""

    kind: Literal[EventKind.QUOTE] = EventKind.QUOTE
    instrument_id: str = Field(min_length=1)
    mark: StrictDecimal
    bid: StrictDecimal | None = None
    ask: StrictDecimal | None = None
    source: str = "shadow-slice"

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        for name in ("mark", "bid", "ask"):
            value = getattr(self, name)
            if value is not None and value <= ZERO:
                raise ValueError(f"{name} must be positive")
        if self.bid is not None and self.ask is not None and self.bid > self.ask:
            raise ValueError("bid cannot exceed ask")
        return self

    def to_observation(self) -> Observation:
        return Observation(
            instrument_id=self.instrument_id,
            kind=ObservationKind.QUOTE,
            event_time=self.event_time_ns,
            available_at=self.available_at_ns,
            received_at=self.available_at_ns,
            published_at=self.available_at_ns,
            source=self.source,
            sequence=0,
            quality=QualityState.OK,
            payload={
                "mark": str(self.mark),
                "bid": None if self.bid is None else str(self.bid),
                "ask": None if self.ask is None else str(self.ask),
            },
        )


class FundingUpdate(MarketEvent):
    """One funding-rate observation for a perpetual."""

    kind: Literal[EventKind.FUNDING] = EventKind.FUNDING
    instrument_id: str = Field(min_length=1)
    rate: StrictDecimal
    source: str = "shadow-slice"

    def to_observation(self) -> Observation:
        return Observation(
            instrument_id=self.instrument_id,
            kind=ObservationKind.FUNDING,
            event_time=self.event_time_ns,
            available_at=self.available_at_ns,
            received_at=self.available_at_ns,
            published_at=self.available_at_ns,
            source=self.source,
            sequence=0,
            quality=QualityState.OK,
            payload={"rate": str(self.rate)},
        )


class CutUpdate(MarketEvent):
    """An explicit decision cut over one or more instruments."""

    kind: Literal[EventKind.CUT] = EventKind.CUT
    instruments: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        for instrument in self.instruments:
            if not instrument.strip():
                raise ValueError("cut instrument cannot be blank")
        return self

    @property
    def decision_time_ns(self) -> int:
        return self.event_time_ns


def _event_instrument(event: MarketEvent) -> str:
    if isinstance(event, CutUpdate):
        return "+".join(event.instruments)
    instrument = getattr(event, "instrument_id", "")
    return instrument if isinstance(instrument, str) else ""


def _event_key(event: MarketEvent) -> tuple[int, int, str, str]:
    return (
        event.event_time_ns,
        _KIND_RANK[event.kind],
        _event_instrument(event),
        canonical_digest(f"{SLICE_VERSION}.event", event.model_dump(mode="json")),
    )


def _canonical_events(script: Sequence[MarketEvent]) -> tuple[MarketEvent, ...]:
    """Total, declared order of a script; one input set yields one sequence."""
    if not script:
        raise ShadowScriptError("a shadow script cannot be empty", code="EMPTY_SCRIPT")
    for event in script:
        if not isinstance(event, MarketEvent):
            raise ShadowScriptError(
                f"expected MarketEvent, got {type(event).__name__}", code="FOREIGN_EVENT"
            )
    ordered = tuple(sorted(script, key=_event_key))
    keys = [_event_key(event) for event in ordered]
    for previous, current in zip(keys, keys[1:], strict=False):
        if previous == current:
            raise ShadowScriptError(
                f"two events share the ordering key {current[:3]}; the script is ambiguous",
                code="AMBIGUOUS_EVENT_ORDER",
            )
    return ordered


# --------------------------------------------------------------------------- #
# trace surface
# --------------------------------------------------------------------------- #
class ParityBlock(BaseModel):
    """The control-arm comparison captured around one adaptive run."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    policy_state: str
    verification: str
    native_streams: tuple[str, ...]
    reference: str
    observed: str
    unchanged: bool
    shadow: str | None = None


class TraceStep(BaseModel):
    """One decision cut's complete, auditable outcome for one instrument."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    cut_time_ns: StrictInt = Field(ge=0)
    instrument_id: str = Field(min_length=1)
    belief_version_hash: str | None = None
    belief_status: str
    belief_quality: str
    evidence_coverage: int = Field(ge=0)
    evidence_abstain_count: int = Field(ge=0)
    opportunity_id: str | None = None
    opportunity_status: str | None = None
    lifecycle: OpportunityLifecycle | None = None
    position_presence: PositionPresence
    venue_status: VenueOrderStatus
    risk_status: RiskStatus
    risk_reason: str
    decision_id: str | None = None
    action: DecisionAction | None = None
    intent_kind: IntentKind | None = None
    reason_codes: tuple[ReasonCode, ...] = ()
    entry_intent_id: str | None = None
    entry_mode: str | None = None
    entry_reason: str | None = None
    management_id: str | None = None
    advisory_action: DecisionAction | None = None
    advisory_proposal_ids: tuple[str, ...] = ()
    allocation_outcome: str | None = None
    allocation_id: str | None = None
    #: Every proposal this step emitted is advisory; there is no other mode.
    advisory_only: Literal[True] = True
    #: True when any emitted proposal declares that the risk plane must permit it
    #: before it could ever be acted on. The slice never treats this as a grant.
    requires_risk_permission: bool = False
    receipt_digest: str | None = None
    max_input_available_at: StrictInt = Field(ge=0)
    position_available_at_ns: StrictInt | None = None
    quote_available_at_ns: StrictInt | None = None
    protection_available_at_ns: StrictInt | None = None
    risk_as_of_ns: StrictInt | None = None

    @model_validator(mode="after")
    def _pit(self) -> Self:
        if self.max_input_available_at > self.cut_time_ns:
            raise ValueError("a trace step cannot consume an input from after its cut")
        for name in (
            "position_available_at_ns",
            "quote_available_at_ns",
            "protection_available_at_ns",
            "risk_as_of_ns",
        ):
            value = getattr(self, name)
            if value is not None and value > self.cut_time_ns:
                raise ValueError(f"{name} is after the decision cut")
        if (self.action is None) != (self.decision_id is None):
            raise ValueError("a decision action and its id appear together")
        return self


class ShadowTrace(BaseModel):
    """The frozen, replayable record of one adaptive shadow run."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    slice_version: str = SLICE_VERSION
    adaptive_layer: bool
    steps: tuple[TraceStep, ...] = ()
    receipt_ids: tuple[str, ...] = ()
    #: Structurally empty: nothing in this module can emit a native side effect.
    native_side_effects: tuple[str, ...] = NO_NATIVE_SIDE_EFFECTS
    parity: ParityBlock

    @model_validator(mode="after")
    def _contract(self) -> Self:
        if self.native_side_effects:
            raise ValueError("a shadow trace cannot carry native side effects")
        if len(set(self.receipt_ids)) != len(self.receipt_ids):
            raise ValueError("receipt ids must be unique")
        if not self.adaptive_layer:
            if self.steps or self.receipt_ids:
                raise ValueError("a disabled adaptive layer produces no decisions")
            return self
        if not self.steps:
            raise ValueError("an enabled adaptive layer must produce at least one step")
        seen: set[tuple[int, str]] = set()
        previous = -1
        for step in self.steps:
            if step.cut_time_ns < previous:
                raise ValueError("trace steps must be ordered by cut time")
            previous = step.cut_time_ns
            key = (step.cut_time_ns, step.instrument_id)
            if key in seen:
                raise ValueError(f"duplicate trace step for {key}")
            seen.add(key)
        decided = tuple(step.decision_id for step in self.steps if step.decision_id is not None)
        if decided != self.receipt_ids:
            raise ValueError("receipt ids must enumerate the steps' decisions in order")
        return self

    def actions(self) -> tuple[DecisionAction, ...]:
        """The decisions this trace reached, in order (empty when disabled)."""
        return tuple(step.action for step in self.steps if step.action is not None)

    def to_json(self) -> str:
        """Canonical JSON: sorted keys, no whitespace, decimals as strings."""
        return canonical(self.model_dump(mode="json"))


# --------------------------------------------------------------------------- #
# policy views
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class _BeliefView:
    """``v8_next.economics.policy.BeliefView`` over the belief snapshot.

    Not frozen: the injected protocol declares settable attributes. Instances are
    built per cut and never mutated.
    """

    version_hash: str
    regime: str
    uncertainty: Decimal
    stale: bool
    abstain: bool
    as_of_ns: int


@dataclass(slots=True)
class _OpportunityView:
    opportunity_id: str
    lifecycle: OpportunityLifecycle
    as_of_ns: int


@dataclass(slots=True)
class _StanceView:
    expert_id: str
    stance: str
    weight: Decimal


def _belief_stale(belief: MarketBeliefV1) -> bool:
    return belief.status is BeliefStatus.STALE or belief.quality is QualityState.STALE


def _belief_view(belief: MarketBeliefV1) -> _BeliefView:
    return _BeliefView(
        version_hash=belief.version_hash,
        regime=belief.regime_label.value,
        uncertainty=UNCERTAINTY_BANDS[belief.uncertainty],
        stale=_belief_stale(belief),
        abstain=belief.status is BeliefStatus.ABSTAIN,
        as_of_ns=belief.max_input_available_at,
    )


def _thesis_belief(belief: MarketBeliefV1) -> ThesisBelief:
    return ThesisBelief(
        belief_id=belief.version_hash,
        version_hash=belief.version_hash,
        regime=belief.regime_label.value,
        as_of_ns=belief.max_input_available_at,
        stale=_belief_stale(belief),
        abstain=belief.status is BeliefStatus.ABSTAIN,
    )


def _stances(bundle: EvidenceBundle | None) -> tuple[_StanceView, ...]:
    """Witness -> panel stance, weighted by the determinacy the row already carries.

    An abstaining witness contributes its zero determinacy: it is present in the
    panel and in the trace's coverage, it cannot vote. No weight is fitted.
    """
    if bundle is None:
        return ()
    return tuple(
        _StanceView(expert_id=row.expert_id, stance=row.stance.value, weight=row.confidence)
        for row in bundle.evidence
    )


def _thesis_witnesses(bundle: EvidenceBundle | None) -> tuple[ThesisEvidence, ...]:
    if bundle is None:
        return ()
    return tuple(
        ThesisEvidence(
            expert_id=row.expert_id,
            stance=ThesisStanceKind(row.stance.value),
            weight=row.confidence,
        )
        for row in bundle.evidence
    )


def _quote_from(view: ObservationView, instrument_id: str) -> MarketQuote | None:
    """Newest usable QUOTE observation at the view's own cut."""
    latest: Observation | None = None
    for observation in view.observations(instrument_id):
        if observation.kind is not ObservationKind.QUOTE:
            continue
        if latest is None or observation.available_at >= latest.available_at:
            latest = observation
    if latest is None:
        return None
    payload = latest.payload
    mark = payload.get("mark")
    if not isinstance(mark, str):
        return None
    bid = payload.get("bid")
    ask = payload.get("ask")
    return MarketQuote(
        instrument_id=instrument_id,
        bid=None if bid is None else Decimal(bid),
        ask=None if ask is None else Decimal(ask),
        mark=Decimal(mark),
        available_at_ns=latest.available_at,
        event_time_ns=latest.event_time,
        source=latest.source,
    )


# --------------------------------------------------------------------------- #
# run state
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class _EntryRecord:
    """What a shadow ENTER proposed, kept for the eventual position thesis."""

    decision_id: str
    opportunity_id: str
    belief_version_hash: str
    regime: str
    side: str


@dataclass(frozen=True, slots=True)
class _Candidate:
    """One priceable entry proposal entering the portfolio plane."""

    opportunity_id: str
    instrument: str
    direction: str
    reference_price: Decimal
    confidence: Decimal


@dataclass(frozen=True, slots=True)
class _Proposal:
    step: TraceStep
    candidate: _Candidate | None


@dataclass(slots=True)
class _InstrumentRun:
    """Per-instrument mutable state of one run."""

    instrument_id: str
    cache: FeatureCache = field(default_factory=FeatureCache)
    belief: MarketBeliefV1 | None = None
    thesis: PositionThesis | None = None
    last_entry: _EntryRecord | None = None


def _latest(
    readings: Mapping[EventKind, Sequence[MarketEvent]],
    kind: EventKind,
    instrument_id: str,
    cut: int,
) -> MarketEvent | None:
    """Newest reading of ``kind`` for ``instrument_id`` available at ``cut``."""
    latest: MarketEvent | None = None
    for event in readings.get(kind, ()):
        if _event_instrument(event) != instrument_id:
            continue
        if event.available_at_ns > cut:
            continue
        if latest is None or (event.event_time_ns, event.available_at_ns) >= (
            latest.event_time_ns,
            latest.available_at_ns,
        ):
            latest = event
    return latest


def _risk_reading(event: MarketEvent | None, at: int) -> tuple[RiskObservation | None, str]:
    """Usable hard-risk reading at ``at``, or the reason there is none.

    A reading the risk plane published too long ago is *unknown*, never extended:
    an unreadable account snapshot must not license a decision.
    """
    if not isinstance(event, RiskUpdate):
        return None, MISSING_RISK_REASON
    observation = event.observation
    if at - observation.as_of_ns > RISK_MAX_AGE_NS:
        return None, STALE_RISK_REASON
    return observation, observation.reason


def _episode_for(snapshot: OpportunitySnapshot, instrument_id: str) -> EpisodeSnapshot | None:
    """The newest episode of ``instrument_id`` known at the snapshot's cut."""
    episodes = [
        episode
        for episode in snapshot.episodes
        if episode.opportunity.instrument_id == instrument_id
    ]
    if not episodes:
        return None
    return max(
        episodes,
        key=lambda episode: (episode.opportunity.as_of_time_ns, episode.opportunity.opportunity_id),
    )


def _band(mark: Decimal, tick_size: Decimal) -> tuple[Decimal, Decimal]:
    """Declared acceptable-price band around ``mark``, snapped onto the tick."""
    half = mark * ENTRY_BAND_BPS / BPS
    low = ((mark - half) / tick_size).to_integral_value(rounding=ROUND_HALF_UP) * tick_size
    high = ((mark + half) / tick_size).to_integral_value(rounding=ROUND_HALF_UP) * tick_size
    if low <= ZERO:
        low = tick_size
    return low, high


def _money(value: Decimal) -> Decimal:
    return value.quantize(MONEY_QUANTUM, rounding=ROUND_HALF_UP)


# --------------------------------------------------------------------------- #
# the trader
# --------------------------------------------------------------------------- #
class ShadowTrader:
    """Deterministic shadow orchestrator: one script in, one frozen trace out.

    The instance owns only declared policy constants and the V8 native control
    arm. :meth:`run` builds every piece of mutable state locally, so two runs of
    the same script are byte-identical and a run cannot leak into the next.
    """

    __slots__ = ("_control", "_fee_bps", "_lot_step", "_tick_size")

    def __init__(
        self,
        *,
        native_control: ReplayScript,
        fee_bps: Decimal = DEFAULT_FEE_BPS,
        lot_step: Decimal = DEFAULT_LOT_STEP,
        tick_size: Decimal = DEFAULT_TICK_SIZE,
    ) -> None:
        if not isinstance(native_control, ReplayScript):
            raise TypeError(
                f"native_control must be a ReplayScript, got {type(native_control).__name__}"
            )
        for name, value in (
            ("fee_bps", fee_bps),
            ("lot_step", lot_step),
            ("tick_size", tick_size),
        ):
            if not isinstance(value, Decimal):
                raise TypeError(f"{name} must be Decimal, not {type(value).__name__}")
            if not value.is_finite() or value <= ZERO:
                raise ValueError(f"{name} must be finite and positive")
        if fee_bps >= BPS:
            raise ValueError("fee_bps must stay below 10000")
        self._control = native_control
        self._fee_bps = fee_bps
        self._lot_step = lot_step
        self._tick_size = tick_size

    # ------------------------------------------------------------------ public

    def run(self, script: Sequence[MarketEvent], *, adaptive: bool = True) -> ShadowTrace:
        """Evaluate ``script`` and return the frozen trace.

        ``adaptive=False`` is the baseline arm: nothing is evaluated, no receipt
        exists, and the trace proves the native control is byte-identical to the
        policy-off signature.
        """
        events = _canonical_events(script)
        reference = capture_replay(self._control, policy_state=POLICY_OFF)
        steps: tuple[TraceStep, ...] = ()
        receipts: tuple[DecisionReceipt, ...] = ()
        if adaptive:
            steps, receipts = self._evaluate(events)
        observed = capture_replay(self._control, policy_state=POLICY_OFF)
        parity = self._parity(reference, observed, receipts, adaptive)
        return ShadowTrace(
            adaptive_layer=adaptive,
            steps=steps,
            receipt_ids=tuple(receipt.decision_id for receipt in receipts),
            parity=parity,
        )

    # ----------------------------------------------------------------- control

    def _parity(
        self,
        reference: ReplaySignature,
        observed: ReplaySignature,
        receipts: Sequence[DecisionReceipt],
        adaptive: bool,
    ) -> ParityBlock:
        if reference != observed:
            raise ShadowScriptError(
                "the native control arm changed during the adaptive run",
                code="NATIVE_CONTROL_DRIFT",
            )
        if not adaptive:
            return ParityBlock(
                policy_state=POLICY_OFF,
                verification="POLICY_OFF_CONTROL_UNCHANGED",
                native_streams=NATIVE_STREAMS,
                reference=pin(reference),
                observed=pin(observed),
                unchanged=True,
            )
        shadow = ReplaySignature(
            policy_state=POLICY_SHADOW,
            opportunity=observed.opportunity,
            orders=observed.orders,
            protection=observed.protection,
            fills=observed.fills,
            funding=observed.funding,
            lineage=observed.lineage,
            decisions=canonical_digest(
                f"{SLICE_VERSION}.decisions",
                [[receipt.decision_id, receipt.digest] for receipt in receipts],
            ),
            decision_ids=tuple(receipt.decision_id for receipt in receipts),
        )
        assert_shadow_unmutated(reference, shadow)
        return ParityBlock(
            policy_state=POLICY_SHADOW,
            verification="SHADOW_UNMUTATED",
            native_streams=NATIVE_STREAMS,
            reference=pin(reference),
            observed=pin(observed),
            unchanged=True,
            shadow=pin(shadow),
        )

    # ---------------------------------------------------------------- the run

    def _evaluate(
        self, events: tuple[MarketEvent, ...]
    ) -> tuple[tuple[TraceStep, ...], tuple[DecisionReceipt, ...]]:
        readings: dict[EventKind, list[MarketEvent]] = {kind: [] for kind in EventKind}
        observations: list[Observation] = []
        runs: dict[str, _InstrumentRun] = {}
        book = OpportunityBook()
        recurrent: dict[str, RecurrentOpportunityState] = {}
        ledger = DecisionLedger()
        steps: list[TraceStep] = []
        cuts = 0

        for event in events:
            if isinstance(event, CutUpdate):
                cuts += 1
                steps.extend(
                    self._cut(
                        event,
                        observations=tuple(observations),
                        readings=readings,
                        runs=runs,
                        book=book,
                        recurrent=recurrent,
                        ledger=ledger,
                    )
                )
                continue
            readings[event.kind].append(event)
            if isinstance(event, BarUpdate):
                observations.append(event.to_observation())
                if event.quality in _BAR_BUILDABLE:
                    book.on_candle(event.to_candle())
            elif isinstance(event, (QuoteUpdate, FundingUpdate)):
                observations.append(event.to_observation())
            elif isinstance(event, OpportunityUpdate):
                _apply_opportunity(event, book=book, recurrent=recurrent)

        if cuts == 0:
            raise ShadowNotExercised(
                "a shadow run needs at least one decision cut; an empty run proves nothing"
            )
        return tuple(steps), ledger.receipts()

    # ----------------------------------------------------------------- a cut

    def _cut(
        self,
        cut: CutUpdate,
        *,
        observations: Sequence[Observation],
        readings: Mapping[EventKind, Sequence[MarketEvent]],
        runs: dict[str, _InstrumentRun],
        book: OpportunityBook,
        recurrent: dict[str, RecurrentOpportunityState],
        ledger: DecisionLedger,
    ) -> list[TraceStep]:
        at = cut.decision_time_ns
        snapshot = book.snapshot(at)
        for episode in snapshot.episodes:
            state = recurrent.get(episode.opportunity.opportunity_id)
            if state is None or state.last_evaluation_ns == at:
                # The bootstrap already recorded this instant (an episode created
                # or reshaped at the cut has one evaluation, never two).
                continue
            recurrent[episode.opportunity.opportunity_id] = advance_opportunity(
                state, evaluation_time_ns=at, status=episode.status
            )

        proposals = [
            self._evaluate_instrument(
                cut,
                instrument,
                observations=observations,
                readings=readings,
                runs=runs,
                book=book,
                snapshot=snapshot,
                ledger=ledger,
            )
            for instrument in cut.instruments
        ]
        candidates = tuple(
            proposal.candidate for proposal in proposals if proposal.candidate is not None
        )
        allocation = self._allocate(candidates, cut.instruments, readings, at)
        steps: list[TraceStep] = []
        for proposal in proposals:
            outcome_value: str | None = None
            allocation_id: str | None = None
            if proposal.candidate is not None:
                if allocation is None:
                    outcome_value = ALLOCATION_NOT_ATTEMPTED
                else:
                    allocation_id = allocation.digest
                    outcome = allocation.outcome_for(proposal.candidate.opportunity_id)
                    if outcome is not None:
                        outcome_value = outcome.outcome.value
                    else:
                        outcome_value = ALLOCATION_NOT_ATTEMPTED
            steps.append(
                proposal.step.model_copy(
                    update={
                        "allocation_outcome": outcome_value,
                        "allocation_id": allocation_id,
                    }
                )
            )
        return steps

    def _risk_at(
        self,
        readings: Mapping[EventKind, Sequence[MarketEvent]],
        instruments: Sequence[str],
        at: int,
    ) -> RiskObservation | None:
        """Newest hard-risk reading across the cut's instruments (account-wide)."""
        best: tuple[int, int, str, RiskObservation] | None = None
        for instrument in sorted(instruments):
            event = _latest(readings, EventKind.RISK, instrument, at)
            observation, _ = _risk_reading(event, at)
            if observation is None or not isinstance(event, RiskUpdate):
                continue
            key = (event.event_time_ns, event.available_at_ns, instrument, observation)
            if best is None or key[:3] >= best[:3]:
                best = key
        return None if best is None else best[3]

    def _allocate(
        self,
        candidates: Sequence[_Candidate],
        instruments: Sequence[str],
        readings: Mapping[EventKind, Sequence[MarketEvent]],
        at: int,
    ) -> ChallengeResult | None:
        """Run the portfolio challenger, or refuse to run it.

        The risk plane never grants permission here: only an explicit
        ``AVAILABLE`` reading opens the allocation path at all, and every candidate
        must carry its own complete venue grid from a fresh reading. Capacity and
        caps come from the account-wide reading of the cut; the lot grid and size
        bounds come from each candidate's own instrument reading, because a grid is
        a venue property, not an account one. An incomplete grid refuses the whole
        batch instead of allocating the other candidates on a guessed grid.
        """
        if not candidates:
            return None
        account = self._risk_at(readings, instruments, at)
        if account is None or account.status is not RiskStatus.AVAILABLE:
            return None
        limits = account.limits
        equity = limits.equity
        committed = limits.committed_exposure
        if equity is None:
            return None
        requested = _money(equity * ALLOCATION_FRACTION)
        if requested <= ZERO:
            return None
        allocation_candidates: list[AllocationCandidate] = []
        step_sizes: dict[str, Decimal] = {}
        for candidate in candidates:
            own = self._risk_at(readings, (candidate.instrument,), at)
            venue = own.limits if own is not None else HardRiskLimits()
            grid = (
                venue.step_size,
                venue.min_size,
                venue.max_size,
                venue.tick_size,
                venue.min_notional,
            )
            if any(value is None for value in grid):
                return None
            step_size, min_size, max_size, tick_size, min_notional = grid
            assert step_size is not None
            assert min_size is not None
            assert max_size is not None
            assert tick_size is not None
            assert min_notional is not None
            step_sizes[candidate.instrument] = step_size
            allocation_candidates.append(
                AllocationCandidate(
                    opportunity_id=candidate.opportunity_id,
                    instrument=candidate.instrument,
                    direction=candidate.direction,
                    requested_notional=requested,
                    risk_priority=0,
                    sector=venue.sector or limits.sector or "UNKNOWN",
                    correlation_group=venue.correlation_group or "UNKNOWN",
                    reference_price=candidate.reference_price,
                    confidence=candidate.confidence,
                    step_size=step_size,
                    min_size=min_size,
                    max_size=max_size,
                    tick_size=tick_size,
                    min_notional=min_notional,
                )
            )
        instrument_committed = None
        sector_committed = None
        if committed is not None and committed > ZERO:
            sector = limits.sector or "UNKNOWN"
            instrument_committed = {
                candidate.instrument: committed for candidate in allocation_candidates
            }
            sector_committed = {sector: committed}
        return allocate_challenger(
            tuple(allocation_candidates),
            equity=equity,
            committed=committed,
            max_gross_exposure=limits.max_gross_exposure,
            max_per_sector=limits.max_per_sector,
            risk_budget=limits.risk_budget,
            step_sizes=step_sizes,
            instrument_committed=instrument_committed,
            sector_committed=sector_committed,
        )

    # ----------------------------------------------------------- one instrument

    def _evaluate_instrument(  # noqa: C901 - one linear cut, kept readable
        self,
        cut: CutUpdate,
        instrument_id: str,
        *,
        observations: Sequence[Observation],
        readings: Mapping[EventKind, Sequence[MarketEvent]],
        runs: dict[str, _InstrumentRun],
        book: OpportunityBook,
        snapshot: OpportunitySnapshot,
        ledger: DecisionLedger,
    ) -> _Proposal:
        at = cut.decision_time_ns
        run = runs.setdefault(instrument_id, _InstrumentRun(instrument_id=instrument_id))

        view = build_view(
            (
                observation
                for observation in observations
                if observation.instrument_id == instrument_id
            ),
            cut_time_ns=at,
        )
        context = assemble_context(
            {Timeframe.M15: view}, instrument_id=instrument_id, decision_time_ns=at
        )
        belief = advance(run.belief, view, context, now_ns=at)
        run.belief = belief

        episode = _episode_for(snapshot, instrument_id)
        evidence = self._evidence(run, view, context, belief, episode, instrument_id, at)

        position_event = _latest(readings, EventKind.POSITION, instrument_id, at)
        venue_event = _latest(readings, EventKind.VENUE, instrument_id, at)
        protection_event = _latest(readings, EventKind.PROTECTION, instrument_id, at)
        risk_event = _latest(readings, EventKind.RISK, instrument_id, at)
        position = position_event if isinstance(position_event, PositionUpdate) else None
        protection = protection_event if isinstance(protection_event, ProtectionUpdate) else None
        risk, risk_reason = _risk_reading(risk_event, at)
        limits = risk.limits if risk is not None else HardRiskLimits()
        venue = (
            venue_event.status if isinstance(venue_event, VenueUpdate) else VenueOrderStatus.UNKNOWN
        )
        presence = position.presence if position is not None else PositionPresence.UNCERTAIN
        quantity = position.quantity if position is not None else ZERO
        exposure = position.exposure if position is not None else ZERO

        # Position lifecycle: a close retires the thesis, an open builds one from the
        # shadow ENTER that preceded it. Without such an ENTER no thesis exists and
        # management is explicitly not evaluated (the step records the absence).
        if presence is PositionPresence.NONE:
            run.thesis = None
            run.last_entry = None
        elif (
            presence is PositionPresence.OPEN
            and run.thesis is None
            and run.last_entry is not None
            and position is not None
        ):
            run.thesis = _build_thesis(entry=run.last_entry, position=position, at=at)

        lifecycle = lifecycle_of(episode.status) if episode is not None else None
        quote = _quote_from(view, instrument_id)
        stale = _belief_stale(belief)
        capacity = _CAPACITY_BY_RISK[risk.status if risk is not None else RiskStatus.UNKNOWN]

        request = DecisionRequest(
            decision_time_ns=at,
            instrument_id=instrument_id,
            venue_status=venue,
            position=PositionState(presence=presence, quantity=quantity, exposure=exposure),
            portfolio=PortfolioState(
                equity=limits.equity if limits.equity is not None else ZERO,
                committed_exposure=(
                    limits.committed_exposure if limits.committed_exposure is not None else ZERO
                ),
                capacity=capacity,
            ),
            opportunity=(
                _OpportunityView(
                    opportunity_id=episode.opportunity.opportunity_id,
                    lifecycle=lifecycle,
                    as_of_ns=at,
                )
                if episode is not None and lifecycle is not None
                else None
            ),
            belief=_belief_view(belief),
            expert_stances=_stances(evidence),
        )
        policy = TraderDecisionPolicy()
        decision: TraderDecision = policy.evaluate(request)
        explanation: PolicyExplanation = policy.explain(request)
        receipt = ledger.append(
            decision,
            ReceiptInputs(
                inputs=self._input_refs(belief, view), constraints=explanation.constraints
            ),
        )
        if decision.opportunity_id is not None:
            book.link_decision(decision.opportunity_id, decision.decision_id, event_time_ns=at)

        entry_intent: EntryIntent | None = None
        entry: EntryEvaluation | None = None
        candidate: _Candidate | None = None
        management: ManagementDecision | None = None

        if decision.action is DecisionAction.ENTER:
            entry_intent, entry = self._propose_entry(
                decision,
                episode=episode,
                quote=quote,
                venue=venue,
                stale=stale,
                at=at,
                tick_size=limits.tick_size,
            )
            if entry is not None and entry.mode.value in _PROPOSED_ENTRY_MODES:
                direction = _episode_direction(episode)
                if direction is not None and quote is not None and quote.mark is not None:
                    candidate = _Candidate(
                        opportunity_id=decision.opportunity_id or instrument_id,
                        instrument=instrument_id,
                        direction=direction,
                        reference_price=quote.mark,
                        confidence=max(ZERO, min(Decimal(1), explanation.net_evidence_ratio)),
                    )
                    run.last_entry = _EntryRecord(
                        decision_id=decision.decision_id,
                        opportunity_id=candidate.opportunity_id,
                        belief_version_hash=belief.version_hash,
                        regime=belief.regime_label.value,
                        side=candidate.direction,
                    )
        elif decision.action in (
            DecisionAction.HOLD,
            DecisionAction.REDUCE,
            DecisionAction.EXIT,
        ):
            management = self._manage(
                run,
                decision=decision,
                instrument_id=instrument_id,
                position=position,
                protection=protection,
                belief=belief,
                evidence=evidence,
                quote=quote,
                venue=venue,
                presence=presence,
                limits=limits,
                at=at,
            )

        max_available = max(
            view.max_input_available_at,
            belief.max_input_available_at,
            quote.available_at_ns if quote is not None else 0,
            position.available_at_ns if position is not None else 0,
            protection.available_at_ns if protection is not None else 0,
            risk.as_of_ns if risk is not None else 0,
        )
        step = TraceStep(
            cut_time_ns=at,
            instrument_id=instrument_id,
            belief_version_hash=belief.version_hash,
            belief_status=belief.status.value,
            belief_quality=belief.quality.value,
            evidence_coverage=evidence.coverage if evidence is not None else 0,
            evidence_abstain_count=evidence.abstain_count if evidence is not None else 0,
            opportunity_id=(episode.opportunity.opportunity_id if episode is not None else None),
            opportunity_status=episode.status.value if episode is not None else None,
            lifecycle=lifecycle,
            position_presence=presence,
            venue_status=venue,
            risk_status=risk.status if risk is not None else RiskStatus.UNKNOWN,
            risk_reason=risk_reason,
            decision_id=decision.decision_id,
            action=decision.action,
            intent_kind=decision.intent_kind,
            reason_codes=decision.reason_codes,
            entry_intent_id=entry_intent.intent_id if entry_intent is not None else None,
            entry_mode=entry.mode.value if entry is not None else None,
            entry_reason=entry.reason.value if entry is not None else None,
            management_id=management.management_id if management is not None else None,
            advisory_action=management.advisory.action if management is not None else None,
            advisory_proposal_ids=(
                tuple(item.proposal_id for item in management.proposals)
                if management is not None
                else ()
            ),
            requires_risk_permission=(
                any(item.requires_risk_permission for item in management.proposals)
                if management is not None
                else False
            ),
            receipt_digest=receipt.digest,
            max_input_available_at=max_available,
            position_available_at_ns=position.available_at_ns if position is not None else None,
            quote_available_at_ns=quote.available_at_ns if quote is not None else None,
            protection_available_at_ns=(
                protection.available_at_ns if protection is not None else None
            ),
            risk_as_of_ns=risk.as_of_ns if risk is not None else None,
        )
        return _Proposal(step=step, candidate=candidate)

    # -------------------------------------------------------------- evidence

    def _evidence(
        self,
        run: _InstrumentRun,
        view: ObservationView,
        context: HorizonContext,
        belief: MarketBeliefV1,
        episode: EpisodeSnapshot | None,
        instrument_id: str,
        at: int,
    ) -> EvidenceBundle | None:
        """Context-bound evidence, or ``None`` when the belief did not advance."""
        if belief.decision_time_ns != at:
            return None
        candles = view.bars(instrument_id)
        if not candles:
            return None
        frame = CausalFrame.from_ordered_prefix(
            instrument_id, at, candles, build_candle_dataframe(candles)
        )
        identity = (
            Opportunity(
                opportunity_id=episode.opportunity.opportunity_id,
                exposure_id=episode.opportunity.exposure.exposure_id,
                instrument_id=instrument_id,
                direction=episode.opportunity.direction.value,
                anchor_ns=episode.opportunity.as_of_time_ns,
                expires_ns=episode.opportunity.valid_until_ns,
            )
            if episode is not None
            else None
        )
        return produce_evidence(
            view=view,
            context=context,
            frame=frame,
            cache=run.cache,
            belief=belief,
            opportunity=identity,
        )

    # ------------------------------------------------------------------ entry

    def _propose_entry(
        self,
        decision: TraderDecision,
        *,
        episode: EpisodeSnapshot | None,
        quote: MarketQuote | None,
        venue: VenueOrderStatus,
        stale: bool,
        at: int,
        tick_size: Decimal | None,
    ) -> tuple[EntryIntent | None, EntryEvaluation | None]:
        direction = _episode_direction(episode)
        if direction is None or decision.opportunity_id is None:
            return None, None
        if quote is None or quote.mark is None:
            return None, None
        grid = tick_size if tick_size is not None and tick_size > ZERO else self._tick_size
        low, high = _band(quote.mark, grid)
        intent = build_intent(
            decision_id=decision.decision_id,
            opportunity_id=decision.opportunity_id,
            instrument_id=decision.instrument_id,
            side=direction,
            order_policy=OrderPolicy.MARKET,
            valid_after_ns=at,
            expires_at_ns=at + ENTRY_TTL_NS,
            acceptable_price_low=low,
            acceptable_price_high=high,
            structural_stop_ref=_episode_stop_ref(episode),
            size_policy_id=SIZE_POLICY_ID,
            friction_status=(
                FrictionStatus.OBSERVED if quote.has_spread else FrictionStatus.MISSING
            ),
            venue_version=VENUE_VERSION,
            policy_version=decision.policy_version,
            created_ns=at,
            kill_conditions=("BELIEF_STALE", "OPPORTUNITY_INVALIDATED", "RISK_DENIED"),
        )
        evaluation = evaluate_entry(
            intent,
            quote,
            now_ns=at,
            venue_status=venue,
            belief_stale=stale,
            belief_available=True,
        )
        return intent, evaluation

    # ------------------------------------------------------------- management

    def _manage(
        self,
        run: _InstrumentRun,
        *,
        decision: TraderDecision,
        instrument_id: str,
        position: PositionUpdate | None,
        protection: ProtectionUpdate | None,
        belief: MarketBeliefV1,
        evidence: EvidenceBundle | None,
        quote: MarketQuote | None,
        venue: VenueOrderStatus,
        presence: PositionPresence,
        limits: HardRiskLimits,
        at: int,
    ) -> ManagementDecision | None:
        thesis = run.thesis
        if thesis is None or position is None or quote is None or quote.mark is None:
            return None
        if position.presence is not PositionPresence.OPEN:
            return None
        mark = quote.mark
        snapshot = PositionSnapshot(
            native_position_id=thesis.native_position_id,
            quantity=position.quantity,
            average_price=position.average_price,
            mark_price=mark,
            protective_order_ids=(),
            latest_belief_id=None,
            elapsed_bars=position.elapsed_bars,
            available_at_ns=position.available_at_ns,
        )
        evaluation: PositionEvaluation = evaluate_position(
            thesis,
            snapshot,
            _thesis_belief(belief),
            now_ns=at,
            evidence=_thesis_witnesses(evidence),
        )
        return evaluate_management(
            evaluation,
            protection_state=(
                protection.state if protection is not None else ProtectionState.UNKNOWN
            ),
            fee_bps=self._fee_bps,
            mark_price=mark,
            side=thesis.side,
            entry_price=thesis.entry_price,
            quantity=position.quantity,
            initial_stop_price=thesis.initial_stop_price,
            instrument_id=instrument_id,
            protective_stop_price=(protection.stop_price if protection is not None else None),
            lot_step=(limits.step_size if limits.step_size is not None else self._lot_step),
            tick_size=(limits.tick_size if limits.tick_size is not None else self._tick_size),
            venue_status=venue,
            position_presence=presence,
            stale=_belief_stale(belief),
            armed_opportunity=False,
            competing_actions=(decision.action,),
        )

    # --------------------------------------------------------------- receipts

    @staticmethod
    def _input_refs(belief: MarketBeliefV1, view: ObservationView) -> tuple[DecisionInputRef, ...]:
        refs: list[DecisionInputRef] = []
        for observation_id in belief.input_observation_ids:
            try:
                observation = view.require_observation(observation_id)
            except KeyError:
                continue
            refs.append(
                DecisionInputRef(
                    observation_id=observation_id,
                    available_at_ns=observation.available_at,
                )
            )
        return tuple(refs)


def _apply_opportunity(
    event: OpportunityUpdate,
    *,
    book: OpportunityBook,
    recurrent: dict[str, RecurrentOpportunityState],
) -> None:
    """Insert the market-created episode, or record its reassessment."""
    record = event.to_record()
    witness = event.canonical_witness()
    if book.get(record.opportunity_id) is None:
        book.insert(record, witness_ids=(witness,))
        recurrent[record.opportunity_id] = bootstrap(record, witness_ids=(witness,))
        return
    book.record_status(
        record.opportunity_id,
        event.status,
        event_time_ns=event.event_time_ns,
        available_at_ns=event.available_at_ns,
        witness_ids=(witness,),
        reason="SHADOW_REASSESSMENT",
    )


def _episode_direction(episode: EpisodeSnapshot | None) -> str | None:
    if episode is None:
        return None
    direction = episode.opportunity.direction
    if direction is ExposureDirection.LONG:
        return "LONG"
    if direction is ExposureDirection.SHORT:
        return "SHORT"
    return None


def _episode_stop_ref(episode: EpisodeSnapshot | None) -> str:
    if episode is not None and episode.opportunity.stop_price is not None:
        return f"opportunity-stop:{episode.opportunity.opportunity_id}"
    return "opportunity-stop:none"


def _build_thesis(
    *,
    entry: _EntryRecord,
    position: PositionUpdate,
    at: int,
) -> PositionThesis | None:
    """Build the frozen entry thesis from the shadow ENTER and the native open."""
    if position.side is None or position.initial_stop_price is None:
        return None
    if position.native_position_id is None:
        return None
    distance = abs(position.average_price - position.initial_stop_price)
    if distance <= ZERO:
        return None
    target = (
        position.average_price + distance * Decimal("2")
        if position.side == "LONG"
        else position.average_price - distance * Decimal("2")
    )
    return build_thesis(
        entry_decision_id=entry.decision_id,
        original_opportunity_id=entry.opportunity_id,
        original_market_belief_id=entry.belief_version_hash,
        native_position_id=position.native_position_id,
        instrument_id=position.instrument_id,
        side=position.side,
        entry_price=position.average_price,
        initial_stop_price=position.initial_stop_price,
        initial_stop_kind=StopKind.STRUCTURAL,
        original_risk_usdt=_money(distance * abs(position.quantity)),
        target_plan=TargetPlan(
            target_price=target,
            expected_bars=8,
            max_bars=16,
            r_multiple=Decimal("2"),
        ),
        invalidation_rules=(
            InvalidationRule(kind=InvalidationKind.PRICE_STOP),
            InvalidationRule(kind=InvalidationKind.REGIME_CHANGE),
        ),
        entry_regime=entry.regime,
        policy_version=SLICE_VERSION,
        created_ns=at,
    )
