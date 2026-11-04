"""Point-in-time altcoin-perpetual universe and order-feasibility qualification (M3-06).

Scope: Binance USDT-M linear perpetual research at small capital (the 20 USDT class). Two
separate questions, answered separately:

* :func:`eligibility` — *may this contract be evaluated at all at ``as_of_ns``?* It is a
  point-in-time question. A metadata record that only became available after the decision
  instant is ``UNKNOWN_METADATA`` and is never used; listing/delisting boundaries, the age of
  the 24h quote-volume snapshot and metadata-version ambiguity are checked explicitly. The
  check never guesses a symbol's state from a later record.
* :func:`feasibility` — *if it may be evaluated, can an order of the requested notional
  actually be expressed?* Size is quantized **down** to the venue tick/lot, minimum notional
  and minimum quantity are enforced, the structural stop must sit strictly inside the
  isolated-margin liquidation distance, and round-trip fee+slippage must not consume the whole
  stop distance. The quantized sizing spec is returned only on success.
* :func:`qualify_universe` — runs both across a symbol map and reports coverage. Absent
  metadata is reported as ``BLOCKED`` (declared missing) or ``NOT_RUN`` (nothing submitted);
  it is never replaced by a synthetic stand-in.

Status vocabulary (``EligibilityStatus``) is fixed; detailed causes ride alongside in
``UniverseReason`` so a consumer never branches on prose. Where a leverage/margin geometric
failure has no dedicated status slot it is mapped to ``INSUFFICIENT_EQUITY``:

==============================  ==============================  ==============================
Cause                           Status                          Reason
==============================  ==============================  ==============================
requested leverage > venue max  INSUFFICIENT_EQUITY             LEVERAGE_ABOVE_VENUE_MAX
margin at venue max insufficient INSUFFICIENT_EQUITY            EQUITY_BELOW_INITIAL_MARGIN
stop at/through liquidation     INSUFFICIENT_EQUITY             STOP_BEYOND_LIQUIDATION
1x margin <= maintenance rate   INSUFFICIENT_EQUITY             LEVERAGE_AT_OR_BEYOND_MMR
fees+slippage consume the stop  EXCESSIVE_FRICTION              FRICTION_EXCEEDS_STOP_DISTANCE
==============================  ==============================  ==============================

Epistemic status: this repository contains **no real altcoin-perpetual tape** (the captured
fixtures are BTC/ETH-class). *Economic* qualification of a small-capital altcoin universe is
therefore ``NOT_RUN`` here — an M4-04 dependency — and every :class:`UniverseReport` says so in
``economic_qualification``. Only the rule engine is exercised, against synthetic metadata in
the tests. No economic or live-trading claim is made.

Inert by construction: no execution adapter is imported, no venue order object exists, and
the returned spec is advisory data only. All money/size arithmetic is ``Decimal``; no float
enters a decision.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from decimal import Decimal
from enum import StrEnum
from typing import Any, Final, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from v8_next.telemetry.identity import canonical_digest

SCHEMA_VERSION: Final[str] = "universe-feasibility-v1"
#: Research assumption: a 24h quote-volume snapshot older than one hour cannot describe the
#: current liquidity regime, so it is rejected instead of treated as "roughly current". This
#: is a staleness rule, not an economic threshold — no minimum volume is asserted here.
MAX_VOLUME_AGE_NS: Final[int] = 3_600 * 1_000_000_000
#: No altcoin tape exists in this repository; economic qualification is an M4-04 dependency.
ECONOMIC_QUALIFICATION_BLOCKER: Final[str] = "M4-04:NO_ALTCOIN_TAPE_IN_REPO"
SUPPORTED_CONTRACT_TYPE: Final[str] = "PERPETUAL"

BPS: Final[Decimal] = Decimal(10_000)
ZERO: Final[Decimal] = Decimal(0)
ONE: Final[Decimal] = Decimal(1)


class EligibilityStatus(StrEnum):
    """Coarse, exclusive verdict for one symbol at one decision instant."""

    ELIGIBLE = "ELIGIBLE"
    NOT_LISTED = "NOT_LISTED"
    DELISTED = "DELISTED"
    STALE_VOLUME = "STALE_VOLUME"
    BELOW_MIN_NOTIONAL = "BELOW_MIN_NOTIONAL"
    CONTRACT_CHANGED = "CONTRACT_CHANGED"
    UNKNOWN_METADATA = "UNKNOWN_METADATA"
    INSUFFICIENT_EQUITY = "INSUFFICIENT_EQUITY"
    SIZE_BELOW_MIN = "SIZE_BELOW_MIN"
    EXCESSIVE_FRICTION = "EXCESSIVE_FRICTION"


class ResolutionStatus(StrEnum):
    """Whether an as-of metadata version could be established from a history."""

    RESOLVED = "RESOLVED"
    UNKNOWN_METADATA = "UNKNOWN_METADATA"
    CONTRACT_CHANGED = "CONTRACT_CHANGED"


class UniverseReason(StrEnum):
    """Machine-readable cause carried by every verdict and report entry."""

    ELIGIBLE = "ELIGIBLE"
    FEASIBLE = "FEASIBLE"
    NOT_YET_LISTED = "NOT_YET_LISTED"
    DELISTED_AT_INSTANT = "DELISTED_AT_INSTANT"
    METADATA_NOT_AVAILABLE_YET = "METADATA_NOT_AVAILABLE_YET"
    UNSUPPORTED_CONTRACT_TYPE = "UNSUPPORTED_CONTRACT_TYPE"
    NO_VOLUME_SNAPSHOT = "NO_VOLUME_SNAPSHOT"
    VOLUME_SNAPSHOT_FUTURE = "VOLUME_SNAPSHOT_FUTURE"
    VOLUME_SNAPSHOT_STALE = "VOLUME_SNAPSHOT_STALE"
    NO_METADATA_IN_HISTORY = "NO_METADATA_IN_HISTORY"
    CONTRADICTORY_METADATA_VERSION = "CONTRADICTORY_METADATA_VERSION"
    AMBIGUOUS_AS_OF_VERSION = "AMBIGUOUS_AS_OF_VERSION"
    SUPERSEDED_METADATA_VERSION = "SUPERSEDED_METADATA_VERSION"
    TARGET_BELOW_MIN_NOTIONAL = "TARGET_BELOW_MIN_NOTIONAL"
    QUANTIZED_NOTIONAL_BELOW_MIN = "QUANTIZED_NOTIONAL_BELOW_MIN"
    QUANTITY_ROUNDED_TO_ZERO = "QUANTITY_ROUNDED_TO_ZERO"
    QUANTITY_BELOW_MIN_QTY = "QUANTITY_BELOW_MIN_QTY"
    LEVERAGE_ABOVE_VENUE_MAX = "LEVERAGE_ABOVE_VENUE_MAX"
    EQUITY_BELOW_INITIAL_MARGIN = "EQUITY_BELOW_INITIAL_MARGIN"
    LEVERAGE_AT_OR_BEYOND_MMR = "LEVERAGE_AT_OR_BEYOND_MMR"
    STOP_BEYOND_LIQUIDATION = "STOP_BEYOND_LIQUIDATION"
    FRICTION_EXCEEDS_STOP_DISTANCE = "FRICTION_EXCEEDS_STOP_DISTANCE"
    METADATA_ABSENT = "METADATA_ABSENT"
    SYMBOL_MISMATCH = "SYMBOL_MISMATCH"
    FEASIBILITY_NOT_RUN = "FEASIBILITY_NOT_RUN"


class CoverageState(StrEnum):
    """How far one symbol was qualified inside a report."""

    ELIGIBLE = "ELIGIBLE"
    REJECTED = "REJECTED"
    BLOCKED = "BLOCKED"
    NOT_RUN = "NOT_RUN"


class UniverseReportStatus(StrEnum):
    QUALIFIED = "QUALIFIED"
    PARTIAL = "PARTIAL"
    BLOCKED = "BLOCKED"
    NOT_RUN = "NOT_RUN"


def _normalized(value: Decimal) -> str:
    """Exponent-insensitive textual form, so ``0.010`` and ``0.01`` are one term."""
    return str(value.normalize())


def _require_decimal(value: Any, name: str) -> Decimal:
    if not isinstance(value, Decimal):
        raise TypeError(f"{name} must be a Decimal, never float/int: {type(value).__name__}")
    if not value.is_finite():
        raise ValueError(f"{name} must be finite")
    return value


def _floor_to_increment(value: Decimal, increment: Decimal) -> Decimal:
    """Largest multiple of ``increment`` at or below ``value`` (never rounds up)."""
    if value < ZERO or increment <= ZERO:
        raise ValueError("quantization requires non-negative value and positive increment")
    return (value // increment) * increment


def _stamped_id(tag: str, model: BaseModel, id_field: str) -> str:
    payload = model.model_dump(mode="json")
    payload.pop(id_field)
    return canonical_digest(tag, payload)


class InstrumentMetadata(BaseModel):
    """Point-in-time venue metadata for one perpetual contract.

    ``available_at`` is *when the record became knowable*, never the event time inside it: a
    record fetched after the decision instant is inadmissible for that instant even if its
    contents describe an older market state.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    symbol: str = Field(min_length=1)
    venue: str = Field(min_length=1)
    contract_type: str = Field(min_length=1)
    listed_at: int = Field(ge=0)
    delisted_at: int | None = Field(default=None, ge=0)
    tick_size: Decimal
    step_size: Decimal
    min_notional: Decimal
    min_qty: Decimal
    quote_volume_24h: Decimal | None = None
    quote_volume_as_of_ns: int | None = Field(default=None, ge=0)
    leverage_max: Decimal
    maintenance_margin_rate: Decimal
    metadata_version: str = Field(min_length=1)
    available_at: int = Field(ge=0)

    @model_validator(mode="after")
    def _validate(self) -> Self:
        for name in ("tick_size", "step_size", "min_notional", "min_qty"):
            value = getattr(self, name)
            if not isinstance(value, Decimal) or not value.is_finite() or value <= ZERO:
                raise ValueError(f"{name} must be a finite positive Decimal")
        for name in ("leverage_max", "maintenance_margin_rate"):
            value = getattr(self, name)
            if not isinstance(value, Decimal) or not value.is_finite():
                raise ValueError(f"{name} must be a finite Decimal")
        if self.leverage_max < ONE:
            raise ValueError("leverage_max must be at least 1x")
        if not ZERO <= self.maintenance_margin_rate < ONE:
            raise ValueError("maintenance_margin_rate must be in [0, 1)")
        if self.delisted_at is not None and self.delisted_at <= self.listed_at:
            raise ValueError("delisted_at must follow listed_at")
        if self.quote_volume_24h is not None:
            if not self.quote_volume_24h.is_finite() or self.quote_volume_24h < ZERO:
                raise ValueError("quote_volume_24h must be finite and non-negative")
            if self.quote_volume_as_of_ns is None:
                raise ValueError("quote_volume_24h requires quote_volume_as_of_ns")
        elif self.quote_volume_as_of_ns is not None:
            raise ValueError("quote_volume_as_of_ns requires quote_volume_24h")
        if not self.symbol.strip() or not self.venue.strip() or not self.metadata_version.strip():
            raise ValueError("symbol, venue and metadata_version cannot be blank")
        return self

    @property
    def contract_digest(self) -> str:
        """Digest of the *terms* (not the availability stamp) used for change detection."""
        return canonical_digest(
            "universe-contract-v1",
            {
                "symbol": self.symbol,
                "venue": self.venue,
                "contract_type": self.contract_type.strip().upper(),
                "tick_size": _normalized(self.tick_size),
                "step_size": _normalized(self.step_size),
                "min_notional": _normalized(self.min_notional),
                "min_qty": _normalized(self.min_qty),
                "leverage_max": _normalized(self.leverage_max),
                "maintenance_margin_rate": _normalized(self.maintenance_margin_rate),
            },
        )


class UniverseVerdict(BaseModel):
    """Eligibility of one symbol at one PIT instant (no size, no order authority)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    verdict_id: str = Field(min_length=1)
    symbol: str = Field(min_length=1)
    venue: str = Field(min_length=1)
    as_of_ns: int = Field(ge=0)
    metadata_version: str = Field(min_length=1)
    status: EligibilityStatus
    reasons: tuple[UniverseReason, ...] = ()
    quote_volume_24h: Decimal | None = None
    volume_age_ns: int | None = None
    contract_digest: str = Field(min_length=1)

    @property
    def eligible(self) -> bool:
        return self.status is EligibilityStatus.ELIGIBLE


class OrderSpec(BaseModel):
    """Inert, quantized sizing specification: no venue order, no execution authority."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    symbol: str = Field(min_length=1)
    venue: str = Field(min_length=1)
    metadata_version: str = Field(min_length=1)
    quantity: Decimal
    price: Decimal
    notional: Decimal
    leverage: Decimal
    initial_margin: Decimal
    tick_size: Decimal
    step_size: Decimal
    estimated_round_trip_cost: Decimal
    advisory_only: bool = True

    @model_validator(mode="after")
    def _validate(self) -> Self:
        if self.advisory_only is not True:
            raise ValueError("OrderSpec never grants order authority")
        for name in ("quantity", "price", "notional", "leverage", "initial_margin", "tick_size"):
            value = getattr(self, name)
            if not isinstance(value, Decimal) or not value.is_finite() or value <= ZERO:
                raise ValueError(f"{name} must be a finite positive Decimal")
        if self.step_size <= ZERO or self.estimated_round_trip_cost < ZERO:
            raise ValueError("invalid sizing spec")
        if self.quantity % self.step_size != ZERO:
            raise ValueError("quantity is not a whole lot multiple")
        if self.price % self.tick_size != ZERO:
            raise ValueError("price is not a whole tick multiple")
        if self.notional != self.quantity * self.price:
            raise ValueError("notional must equal quantity * price")
        return self


class FeasibilityVerdict(BaseModel):
    """Whether the requested notional can be expressed at the requested size/leverage."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    verdict_id: str = Field(min_length=1)
    symbol: str = Field(min_length=1)
    venue: str = Field(min_length=1)
    metadata_version: str = Field(min_length=1)
    status: EligibilityStatus
    reasons: tuple[UniverseReason, ...] = ()
    equity: Decimal
    target_notional: Decimal
    entry_price: Decimal
    fee_bps: Decimal
    slippage_bps: Decimal
    stop_distance_bps: Decimal
    leverage: Decimal
    round_trip_friction_bps: Decimal
    friction_to_stop_ratio: Decimal
    liquidation_distance_bps: Decimal
    quantized_price: Decimal | None = None
    quantized_quantity: Decimal | None = None
    quantized_notional: Decimal | None = None
    initial_margin: Decimal | None = None
    spec: OrderSpec | None = None

    @model_validator(mode="after")
    def _spec_matches_status(self) -> Self:
        if (self.status is EligibilityStatus.ELIGIBLE) != (self.spec is not None):
            raise ValueError("a sizing spec exists if and only if the verdict is ELIGIBLE")
        return self

    @property
    def feasible(self) -> bool:
        return self.status is EligibilityStatus.ELIGIBLE


class MetadataResolution(BaseModel):
    """Result of establishing the as-of metadata version for one symbol/venue."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    symbol: str = Field(min_length=1)
    venue: str = Field(min_length=1)
    as_of_ns: int = Field(ge=0)
    status: ResolutionStatus
    reasons: tuple[UniverseReason, ...] = ()
    record: InstrumentMetadata | None = None

    @model_validator(mode="after")
    def _record_matches_status(self) -> Self:
        if (self.status is ResolutionStatus.RESOLVED) != (self.record is not None):
            raise ValueError("a resolution record exists if and only if the status is RESOLVED")
        return self


class CoverageSummary(BaseModel):
    """Counts over the submitted universe; ``total`` is the only authoritative aggregate."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    total: int = Field(ge=0)
    eligible: int = Field(ge=0)
    rejected: int = Field(ge=0)
    blocked: int = Field(ge=0)
    not_run: int = Field(ge=0)

    @model_validator(mode="after")
    def _counts_consistent(self) -> Self:
        if self.eligible + self.rejected + self.blocked + self.not_run != self.total:
            raise ValueError("coverage counts must sum to total")
        return self

    @property
    def evaluated(self) -> int:
        """Symbols that reached a rule decision (eligible or rejected)."""
        return self.eligible + self.rejected

    @property
    def evaluated_fraction(self) -> Decimal:
        if self.total == 0:
            return ZERO
        return Decimal(self.evaluated) / Decimal(self.total)


class UniverseEntry(BaseModel):
    """One symbol's qualification inside a report."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    symbol: str = Field(min_length=1)
    coverage: CoverageState
    eligibility: UniverseVerdict | None = None
    feasibility: FeasibilityVerdict | None = None
    #: Set only when a metadata history could not yield an as-of version.
    resolution: ResolutionStatus | None = None
    reasons: tuple[UniverseReason, ...] = ()

    @model_validator(mode="after")
    def _coverage_consistent(self) -> Self:
        if self.coverage is CoverageState.ELIGIBLE and self.eligibility is None:
            raise ValueError("an ELIGIBLE coverage requires an eligibility verdict")
        return self


class UniverseReport(BaseModel):
    """Qualification report over a submitted symbol map, with explicit coverage."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    report_id: str = Field(min_length=1)
    as_of_ns: int = Field(ge=0)
    status: UniverseReportStatus
    coverage: CoverageSummary
    entries: tuple[UniverseEntry, ...] = ()
    #: Structural: no altcoin tape exists in-repo, so no economic claim is possible (M4-04).
    economic_qualification: Literal["NOT_RUN"] = "NOT_RUN"
    economic_blocker: str = ECONOMIC_QUALIFICATION_BLOCKER

    @model_validator(mode="after")
    def _entries_match_counts(self) -> Self:
        if len(self.entries) != self.coverage.total:
            raise ValueError("coverage total must equal the number of entries")
        return self

    def entry(self, symbol: str) -> UniverseEntry | None:
        return next((e for e in self.entries if e.symbol == symbol), None)


def resolve_metadata(
    history: Sequence[InstrumentMetadata],
    *,
    as_of_ns: int,
) -> MetadataResolution:
    """Establish which metadata version applied at ``as_of_ns``.

    Rules, all fail closed:

    * records available after ``as_of_ns`` are invisible — they are excluded *before*
      any contradiction scan, so a future record cannot change an as-of verdict;
    * one ``metadata_version`` carrying two different term sets among the records
      available at ``as_of_ns`` is contradictory evidence;
    * two different term sets sharing the newest availability stamp leave the as-of version
      ambiguous — ``CONTRACT_CHANGED`` (never "pick one");
    * otherwise the newest available version wins (deterministic tie-break on the version
      string, which can only happen when the terms are identical).
    """
    if type(as_of_ns) is not int or as_of_ns < 0:
        raise ValueError("as_of_ns must be a non-negative int")
    records = tuple(history)
    if not records:
        raise ValueError("metadata history is empty")
    symbols = {r.symbol for r in records}
    venues = {r.venue for r in records}
    if len(symbols) != 1 or len(venues) != 1:
        raise ValueError("metadata history must describe exactly one symbol and venue")
    symbol = records[0].symbol
    venue = records[0].venue

    terms_by_version: dict[str, set[str]] = {}
    for record in records:
        if record.available_at > as_of_ns:
            # A record the decision could not have seen cannot make the as-of
            # terms contradictory; it is not evidence at this instant.
            continue
        terms_by_version.setdefault(record.metadata_version, set()).add(record.contract_digest)
    if any(len(digests) > 1 for digests in terms_by_version.values()):
        return MetadataResolution(
            symbol=symbol,
            venue=venue,
            as_of_ns=as_of_ns,
            status=ResolutionStatus.CONTRACT_CHANGED,
            reasons=(UniverseReason.CONTRADICTORY_METADATA_VERSION,),
        )

    usable = tuple(r for r in records if r.available_at <= as_of_ns)
    if not usable:
        return MetadataResolution(
            symbol=symbol,
            venue=venue,
            as_of_ns=as_of_ns,
            status=ResolutionStatus.UNKNOWN_METADATA,
            reasons=(UniverseReason.METADATA_NOT_AVAILABLE_YET,),
        )

    newest = max(r.available_at for r in usable)
    latest = tuple(r for r in usable if r.available_at == newest)
    if len({r.contract_digest for r in latest}) > 1:
        return MetadataResolution(
            symbol=symbol,
            venue=venue,
            as_of_ns=as_of_ns,
            status=ResolutionStatus.CONTRACT_CHANGED,
            reasons=(UniverseReason.AMBIGUOUS_AS_OF_VERSION,),
        )
    chosen = max(latest, key=lambda r: r.metadata_version)
    return MetadataResolution(
        symbol=symbol,
        venue=venue,
        as_of_ns=as_of_ns,
        status=ResolutionStatus.RESOLVED,
        reasons=(UniverseReason.ELIGIBLE,),
        record=chosen,
    )


def _volume_age_ns(metadata: InstrumentMetadata, as_of_ns: int) -> int | None:
    stamp = metadata.quote_volume_as_of_ns
    if stamp is None or stamp > as_of_ns:
        return None
    return as_of_ns - stamp


def _universe_verdict(
    metadata: InstrumentMetadata,
    *,
    as_of_ns: int,
    status: EligibilityStatus,
    reasons: tuple[UniverseReason, ...],
) -> UniverseVerdict:
    probe = UniverseVerdict(
        verdict_id="pending-identity",
        symbol=metadata.symbol,
        venue=metadata.venue,
        as_of_ns=as_of_ns,
        metadata_version=metadata.metadata_version,
        status=status,
        reasons=reasons,
        quote_volume_24h=metadata.quote_volume_24h,
        volume_age_ns=_volume_age_ns(metadata, as_of_ns),
        contract_digest=metadata.contract_digest,
    )
    return probe.model_copy(
        update={"verdict_id": _stamped_id("universe-verdict-v1", probe, "verdict_id")}
    )


def eligibility(
    metadata: InstrumentMetadata,
    *,
    as_of_ns: int,
    history: Sequence[InstrumentMetadata] | None = None,
    max_volume_age_ns: int = MAX_VOLUME_AGE_NS,
) -> UniverseVerdict:
    """PIT eligibility for one symbol. Never uses a record that was not yet knowable.

    ``history`` is optional. When supplied, the as-of version is resolved from it and the
    supplied ``metadata`` must *be* that version; otherwise the symbol is
    ``CONTRACT_CHANGED`` (the caller's terms are stale or ambiguous). With no history the
    supplied record is treated as the caller's asserted as-of record and only its own
    ``available_at`` is checked.
    """
    if type(as_of_ns) is not int or as_of_ns < 0:
        raise ValueError("as_of_ns must be a non-negative int")
    if type(max_volume_age_ns) is not int or max_volume_age_ns < 0:
        raise ValueError("max_volume_age_ns must be a non-negative int")

    record = metadata
    if history:
        resolution = resolve_metadata(history, as_of_ns=as_of_ns)
        if resolution.status is ResolutionStatus.UNKNOWN_METADATA:
            return _universe_verdict(
                metadata,
                as_of_ns=as_of_ns,
                status=EligibilityStatus.UNKNOWN_METADATA,
                reasons=resolution.reasons,
            )
        if resolution.status is ResolutionStatus.CONTRACT_CHANGED:
            return _universe_verdict(
                metadata,
                as_of_ns=as_of_ns,
                status=EligibilityStatus.CONTRACT_CHANGED,
                reasons=resolution.reasons,
            )
        resolved = resolution.record
        assert resolved is not None  # guaranteed by MetadataResolution validation
        if resolved.metadata_version != metadata.metadata_version:
            return _universe_verdict(
                metadata,
                as_of_ns=as_of_ns,
                status=EligibilityStatus.CONTRACT_CHANGED,
                reasons=(UniverseReason.SUPERSEDED_METADATA_VERSION,),
            )
        record = resolved

    if record.available_at > as_of_ns:
        return _universe_verdict(
            record,
            as_of_ns=as_of_ns,
            status=EligibilityStatus.UNKNOWN_METADATA,
            reasons=(UniverseReason.METADATA_NOT_AVAILABLE_YET,),
        )
    if record.contract_type.strip().upper() != SUPPORTED_CONTRACT_TYPE:
        return _universe_verdict(
            record,
            as_of_ns=as_of_ns,
            status=EligibilityStatus.UNKNOWN_METADATA,
            reasons=(UniverseReason.UNSUPPORTED_CONTRACT_TYPE,),
        )
    if as_of_ns < record.listed_at:
        return _universe_verdict(
            record,
            as_of_ns=as_of_ns,
            status=EligibilityStatus.NOT_LISTED,
            reasons=(UniverseReason.NOT_YET_LISTED,),
        )
    if record.delisted_at is not None and as_of_ns >= record.delisted_at:
        return _universe_verdict(
            record,
            as_of_ns=as_of_ns,
            status=EligibilityStatus.DELISTED,
            reasons=(UniverseReason.DELISTED_AT_INSTANT,),
        )
    if record.quote_volume_24h is None or record.quote_volume_as_of_ns is None:
        return _universe_verdict(
            record,
            as_of_ns=as_of_ns,
            status=EligibilityStatus.UNKNOWN_METADATA,
            reasons=(UniverseReason.NO_VOLUME_SNAPSHOT,),
        )
    if record.quote_volume_as_of_ns > as_of_ns:
        return _universe_verdict(
            record,
            as_of_ns=as_of_ns,
            status=EligibilityStatus.UNKNOWN_METADATA,
            reasons=(UniverseReason.VOLUME_SNAPSHOT_FUTURE,),
        )
    if as_of_ns - record.quote_volume_as_of_ns > max_volume_age_ns:
        return _universe_verdict(
            record,
            as_of_ns=as_of_ns,
            status=EligibilityStatus.STALE_VOLUME,
            reasons=(UniverseReason.VOLUME_SNAPSHOT_STALE,),
        )
    return _universe_verdict(
        record,
        as_of_ns=as_of_ns,
        status=EligibilityStatus.ELIGIBLE,
        reasons=(UniverseReason.ELIGIBLE,),
    )


def _feasibility_verdict(
    metadata: InstrumentMetadata,
    *,
    status: EligibilityStatus,
    reasons: tuple[UniverseReason, ...],
    equity: Decimal,
    target_notional: Decimal,
    entry_price: Decimal,
    fee_bps: Decimal,
    slippage_bps: Decimal,
    stop_distance_bps: Decimal,
    leverage: Decimal,
    round_trip_friction_bps: Decimal,
    friction_to_stop_ratio: Decimal,
    liquidation_distance_bps: Decimal,
    quantized_price: Decimal,
    quantized_quantity: Decimal,
    quantized_notional: Decimal,
    initial_margin: Decimal,
    spec: OrderSpec | None,
) -> FeasibilityVerdict:
    probe = FeasibilityVerdict(
        verdict_id="pending-identity",
        symbol=metadata.symbol,
        venue=metadata.venue,
        metadata_version=metadata.metadata_version,
        status=status,
        reasons=reasons,
        equity=equity,
        target_notional=target_notional,
        entry_price=entry_price,
        fee_bps=fee_bps,
        slippage_bps=slippage_bps,
        stop_distance_bps=stop_distance_bps,
        leverage=leverage,
        round_trip_friction_bps=round_trip_friction_bps,
        friction_to_stop_ratio=friction_to_stop_ratio,
        liquidation_distance_bps=liquidation_distance_bps,
        quantized_price=quantized_price,
        quantized_quantity=quantized_quantity,
        quantized_notional=quantized_notional,
        initial_margin=initial_margin,
        spec=spec,
    )
    return probe.model_copy(
        update={"verdict_id": _stamped_id("universe-feasibility-v1", probe, "verdict_id")}
    )


def feasibility(
    metadata: InstrumentMetadata,
    *,
    equity: Decimal,
    target_notional: Decimal,
    entry_price: Decimal,
    fee_bps: Decimal,
    slippage_bps: Decimal,
    stop_distance_bps: Decimal,
    leverage: Decimal,
) -> FeasibilityVerdict:
    """Can ``target_notional`` be expressed on this contract at ``equity``?

    Adverse fees/slippage are charged per side and applied twice (entry + exit); round-trip
    friction must stay strictly below the structural stop distance, or the trade cannot pay
    for its own costs. The liquidation distance is the isolated-margin approximation
    ``(1/leverage - maintenance_margin_rate)``; funding and mark-price basis are *not* modelled
    (no tape here), and the stop must sit strictly inside it.

    Invalid *inputs* (non-positive, non-finite, price below one tick) raise ``ValueError`` —
    they are caller contract violations, not market verdicts. Every admissible input yields a
    typed verdict; failures are explicit rejections, never a silent default.
    """
    _require_decimal(equity, "equity")
    _require_decimal(target_notional, "target_notional")
    _require_decimal(entry_price, "entry_price")
    _require_decimal(fee_bps, "fee_bps")
    _require_decimal(slippage_bps, "slippage_bps")
    _require_decimal(stop_distance_bps, "stop_distance_bps")
    _require_decimal(leverage, "leverage")
    if equity <= ZERO or target_notional <= ZERO or entry_price <= ZERO:
        raise ValueError("equity, target_notional and entry_price must be positive")
    if fee_bps < ZERO or slippage_bps < ZERO:
        raise ValueError("fee_bps and slippage_bps must be non-negative")
    if stop_distance_bps <= ZERO:
        raise ValueError("stop_distance_bps must be positive")
    if leverage < ONE:
        raise ValueError("leverage must be at least 1x")
    if entry_price < metadata.tick_size:
        raise ValueError("entry_price is below one tick of this contract")

    price = _floor_to_increment(entry_price, metadata.tick_size)
    if price <= ZERO:
        raise ValueError("entry_price quantizes to zero")
    round_trip_friction_bps = (fee_bps + slippage_bps) * 2
    friction_to_stop_ratio = round_trip_friction_bps / stop_distance_bps
    one_over_leverage = ONE / leverage
    liquidation_distance_bps = (one_over_leverage - metadata.maintenance_margin_rate) * BPS
    raw_quantity = target_notional / price
    quantity = _floor_to_increment(raw_quantity, metadata.step_size)
    notional = quantity * price
    initial_margin = notional / leverage
    round_trip_cost = notional * round_trip_friction_bps / BPS

    def reject(
        status: EligibilityStatus,
        reason: UniverseReason,
    ) -> FeasibilityVerdict:
        return _feasibility_verdict(
            metadata,
            status=status,
            reasons=(reason,),
            equity=equity,
            target_notional=target_notional,
            entry_price=entry_price,
            fee_bps=fee_bps,
            slippage_bps=slippage_bps,
            stop_distance_bps=stop_distance_bps,
            leverage=leverage,
            round_trip_friction_bps=round_trip_friction_bps,
            friction_to_stop_ratio=friction_to_stop_ratio,
            liquidation_distance_bps=liquidation_distance_bps,
            quantized_price=price,
            quantized_quantity=quantity,
            quantized_notional=notional,
            initial_margin=initial_margin,
            spec=None,
        )

    if target_notional < metadata.min_notional:
        return reject(
            EligibilityStatus.BELOW_MIN_NOTIONAL, UniverseReason.TARGET_BELOW_MIN_NOTIONAL
        )
    if leverage > metadata.leverage_max:
        return reject(
            EligibilityStatus.INSUFFICIENT_EQUITY, UniverseReason.LEVERAGE_ABOVE_VENUE_MAX
        )
    if equity * leverage < target_notional:
        return reject(
            EligibilityStatus.INSUFFICIENT_EQUITY, UniverseReason.EQUITY_BELOW_INITIAL_MARGIN
        )
    if quantity <= ZERO:
        return reject(EligibilityStatus.SIZE_BELOW_MIN, UniverseReason.QUANTITY_ROUNDED_TO_ZERO)
    if quantity < metadata.min_qty:
        return reject(EligibilityStatus.SIZE_BELOW_MIN, UniverseReason.QUANTITY_BELOW_MIN_QTY)
    if notional < metadata.min_notional:
        return reject(
            EligibilityStatus.BELOW_MIN_NOTIONAL, UniverseReason.QUANTIZED_NOTIONAL_BELOW_MIN
        )
    if round_trip_friction_bps >= stop_distance_bps:
        return reject(
            EligibilityStatus.EXCESSIVE_FRICTION,
            UniverseReason.FRICTION_EXCEEDS_STOP_DISTANCE,
        )
    if one_over_leverage <= metadata.maintenance_margin_rate:
        return reject(
            EligibilityStatus.INSUFFICIENT_EQUITY,
            UniverseReason.LEVERAGE_AT_OR_BEYOND_MMR,
        )
    if stop_distance_bps >= liquidation_distance_bps:
        return reject(EligibilityStatus.INSUFFICIENT_EQUITY, UniverseReason.STOP_BEYOND_LIQUIDATION)
    if initial_margin > equity:
        return reject(
            EligibilityStatus.INSUFFICIENT_EQUITY, UniverseReason.EQUITY_BELOW_INITIAL_MARGIN
        )

    spec = OrderSpec(
        symbol=metadata.symbol,
        venue=metadata.venue,
        metadata_version=metadata.metadata_version,
        quantity=quantity,
        price=price,
        notional=notional,
        leverage=leverage,
        initial_margin=initial_margin,
        tick_size=metadata.tick_size,
        step_size=metadata.step_size,
        estimated_round_trip_cost=round_trip_cost,
    )
    return _feasibility_verdict(
        metadata,
        status=EligibilityStatus.ELIGIBLE,
        reasons=(UniverseReason.FEASIBLE,),
        equity=equity,
        target_notional=target_notional,
        entry_price=entry_price,
        fee_bps=fee_bps,
        slippage_bps=slippage_bps,
        stop_distance_bps=stop_distance_bps,
        leverage=leverage,
        round_trip_friction_bps=round_trip_friction_bps,
        friction_to_stop_ratio=friction_to_stop_ratio,
        liquidation_distance_bps=liquidation_distance_bps,
        quantized_price=price,
        quantized_quantity=quantity,
        quantized_notional=notional,
        initial_margin=initial_margin,
        spec=spec,
    )


class FeasibilityInputs(BaseModel):
    """The feasibility kwargs accepted by :func:`qualify_universe`, validated once."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    equity: Decimal
    target_notional: Decimal
    entry_price: Decimal
    fee_bps: Decimal
    slippage_bps: Decimal
    stop_distance_bps: Decimal
    leverage: Decimal


def _parse_feasibility_kwargs(kwargs: Mapping[str, Any]) -> FeasibilityInputs | None:
    if not kwargs:
        return None
    for name, value in kwargs.items():
        if not isinstance(value, Decimal):
            raise TypeError(
                f"feasibility input {name!r} must be a Decimal, never float/int: "
                f"{type(value).__name__}"
            )
    return FeasibilityInputs(**kwargs)


def _blocked_entry(
    symbol: str,
    *,
    reasons: tuple[UniverseReason, ...],
    resolution: ResolutionStatus | None = None,
) -> UniverseEntry:
    """No usable metadata: BLOCKED, with no fabricated verdict or terms."""
    return UniverseEntry(
        symbol=symbol, coverage=CoverageState.BLOCKED, resolution=resolution, reasons=reasons
    )


def _evaluate_symbol(
    symbol: str,
    value: InstrumentMetadata | Sequence[InstrumentMetadata] | None,
    *,
    as_of_ns: int,
    inputs: FeasibilityInputs | None,
) -> UniverseEntry:
    record: InstrumentMetadata
    history: tuple[InstrumentMetadata, ...] = ()
    if value is None:
        return _blocked_entry(symbol, reasons=(UniverseReason.METADATA_ABSENT,))
    if isinstance(value, InstrumentMetadata):
        record = value
    else:
        records = tuple(value)
        if not records:
            return _blocked_entry(symbol, reasons=(UniverseReason.NO_METADATA_IN_HISTORY,))
        resolution = resolve_metadata(records, as_of_ns=as_of_ns)
        if resolution.status is not ResolutionStatus.RESOLVED:
            return _blocked_entry(symbol, reasons=resolution.reasons, resolution=resolution.status)
        resolved = resolution.record
        assert resolved is not None  # guaranteed by MetadataResolution validation
        record = resolved
        history = records

    if record.symbol != symbol:
        return _blocked_entry(symbol, reasons=(UniverseReason.SYMBOL_MISMATCH,))

    verdict = eligibility(record, as_of_ns=as_of_ns, history=history or None)
    if verdict.status is not EligibilityStatus.ELIGIBLE:
        return UniverseEntry(
            symbol=symbol,
            coverage=CoverageState.REJECTED,
            eligibility=verdict,
            feasibility=None,
            reasons=verdict.reasons,
        )
    if inputs is None:
        return UniverseEntry(
            symbol=symbol,
            coverage=CoverageState.NOT_RUN,
            eligibility=verdict,
            feasibility=None,
            reasons=(UniverseReason.FEASIBILITY_NOT_RUN,),
        )
    feasibility_verdict = feasibility(record, **inputs.model_dump())
    coverage = (
        CoverageState.ELIGIBLE
        if feasibility_verdict.status is EligibilityStatus.ELIGIBLE
        else CoverageState.REJECTED
    )
    return UniverseEntry(
        symbol=symbol,
        coverage=coverage,
        eligibility=verdict,
        feasibility=feasibility_verdict,
        reasons=feasibility_verdict.reasons,
    )


def _summarize(entries: Sequence[UniverseEntry]) -> CoverageSummary:
    return CoverageSummary(
        total=len(entries),
        eligible=sum(1 for e in entries if e.coverage is CoverageState.ELIGIBLE),
        rejected=sum(1 for e in entries if e.coverage is CoverageState.REJECTED),
        blocked=sum(1 for e in entries if e.coverage is CoverageState.BLOCKED),
        not_run=sum(1 for e in entries if e.coverage is CoverageState.NOT_RUN),
    )


def _report_status(
    entries: Sequence[UniverseEntry], coverage: CoverageSummary
) -> UniverseReportStatus:
    if not entries:
        return UniverseReportStatus.NOT_RUN
    if coverage.eligible == coverage.total:
        return UniverseReportStatus.QUALIFIED
    if coverage.blocked == coverage.total:
        return UniverseReportStatus.BLOCKED
    return UniverseReportStatus.PARTIAL


def qualify_universe(
    metadata_by_symbol: Mapping[str, InstrumentMetadata | Sequence[InstrumentMetadata] | None],
    as_of_ns: int,
    **feasibility_kwargs: Any,
) -> UniverseReport:
    """Qualify every submitted symbol; absent metadata is reported, never invented.

    ``metadata_by_symbol`` maps symbol -> one PIT record, a metadata history, or ``None``.
    ``None`` (and an empty history) is ``BLOCKED``; an empty mapping is ``NOT_RUN``. Without
    feasibility kwargs the report is eligibility-only: each eligible symbol stays ``NOT_RUN``
    for coverage rather than being called feasible on partial evidence.

    ``feasibility_kwargs`` are the keyword inputs of :func:`feasibility` (all of them, or
    none); anything else raises a validation error.
    """
    if type(as_of_ns) is not int or as_of_ns < 0:
        raise ValueError("as_of_ns must be a non-negative int")
    inputs = _parse_feasibility_kwargs(feasibility_kwargs)
    if not metadata_by_symbol:
        entries: tuple[UniverseEntry, ...] = ()
    else:
        entries = tuple(
            _evaluate_symbol(symbol, metadata_by_symbol[symbol], as_of_ns=as_of_ns, inputs=inputs)
            for symbol in sorted(metadata_by_symbol)
        )
    coverage = _summarize(entries)
    probe = UniverseReport(
        report_id="pending-identity",
        as_of_ns=as_of_ns,
        status=_report_status(entries, coverage),
        coverage=coverage,
        entries=entries,
    )
    return probe.model_copy(
        update={"report_id": _stamped_id("universe-report-v1", probe, "report_id")}
    )
