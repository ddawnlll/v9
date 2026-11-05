"""Factual occupancy attribution and blocked portfolio opportunities (M3-04).

The question this module answers is narrow and audit-oriented: *for each
opportunity at each decision instant, was capital actually committed, and if not,
what factual state denied it?* It is not a profitability model, not a sizing
model and not an order path.

Three logs are kept apart on purpose, because mixing them is how counterfactual
money becomes "realized" in a report:

* **Factual decision log** — :meth:`OccupancyLedger.decision_log`: one immutable
  :class:`OccupancyEntry` per evaluation, carrying the outcome, the blocking
  reason, the committed capital/exposure at that instant and a PIT digest of the
  capacity state used. ``realized_pnl`` is filled only by
  :meth:`OccupancyLedger.realize` for an opportunity that was actually admitted.
* **Matched alternatives** — :meth:`OccupancyLedger.matched_alternatives`: the
  subset of factual entries that were denied by *actual* capacity, an existing
  position or a risk reservation. They are facts about denial, not money.
* **Simulated counterfactual performance** —
  :meth:`OccupancyLedger.counterfactual_performance`: explicitly simulated PnL
  for denied alternatives, stored in its own map. A hypothetical number is never
  written onto a factual entry and never enters ``realized_pnl_total``.

Fail-closed rules:

* A capacity state that is not yet available at the decision clock (future
  publication), claims an ``as_of`` after the clock, is missing a required
  field, reports an ``UNCERTAIN`` position or is internally inconsistent
  (``committed > equity``) yields ``UNKNOWN`` — never ``ADMITTED``.
* An opportunity already admitted once cannot be admitted again; the duplicate
  evaluation is recorded as ``OCCUPIED_CAPACITY`` and commits nothing.
* Total committed notional can never exceed equity; a breach raises
  :class:`DoubleSpendError` instead of being recorded.
* A denied opportunity can never be realized: :meth:`OccupancyLedger.realize`
  raises :class:`NotAdmittedError` for it.

Money is ``Decimal`` throughout. Counterfactual PnL defaults to ``None``; it is
never fabricated from price moves.
"""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum
from typing import Final, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from v8_next.economics.legality import PositionPresence
from v8_next.opportunities.models import OpportunityStatus
from v8_next.telemetry.identity import canonical_digest

__all__ = [
    "CapacityState",
    "CounterfactualNotApplicableError",
    "CounterfactualOnAdmittedError",
    "CounterfactualPerformance",
    "DoubleSpendError",
    "DuplicateRealizationError",
    "NotAdmittedError",
    "OccupancyEntry",
    "OccupancyLedger",
    "OccupancyOutcome",
    "OccupancyReason",
    "OccupancyRequest",
    "UnknownOpportunityError",
]

CAPACITY_TAG: Final[str] = "occupancy-capacity-state-v1"
ENTRY_TAG: Final[str] = "occupancy-entry-v1"
ZERO = Decimal("0")


class OccupancyOutcome(StrEnum):
    """What actually happened to an opportunity at one decision instant."""

    ADMITTED = "ADMITTED"
    OCCUPIED_CAPACITY = "OCCUPIED_CAPACITY"
    BLOCKED_RISK = "BLOCKED_RISK"
    BLOCKED_NO_CAPITAL = "BLOCKED_NO_CAPITAL"
    BLOCKED_POSITION = "BLOCKED_POSITION"
    EXPIRED = "EXPIRED"
    INVALIDATED = "INVALIDATED"
    UNKNOWN = "UNKNOWN"

    @property
    def is_admitted(self) -> bool:
        return self is OccupancyOutcome.ADMITTED

    @property
    def is_blocked(self) -> bool:
        """Every non-admission is a denial, including terminal states."""
        return not self.is_admitted


class OccupancyReason(StrEnum):
    """Stable, replayable discriminators for the outcome. Never prose."""

    CAPACITY_AVAILABLE = "CAPACITY_AVAILABLE"
    CAPACITY_CONSUMED = "CAPACITY_CONSUMED"
    DUPLICATE_OPPORTUNITY = "DUPLICATE_OPPORTUNITY"
    RISK_RESERVED = "RISK_RESERVED"
    NO_CAPITAL = "NO_CAPITAL"
    POSITION_OPEN = "POSITION_OPEN"
    POSITION_PENDING = "POSITION_PENDING"
    CAPACITY_UNKNOWN = "CAPACITY_UNKNOWN"
    CAPACITY_FUTURE = "CAPACITY_FUTURE"
    CAPACITY_INCONSISTENT = "CAPACITY_INCONSISTENT"
    OPPORTUNITY_EXPIRED = "OPPORTUNITY_EXPIRED"
    OPPORTUNITY_INVALIDATED = "OPPORTUNITY_INVALIDATED"


_ALTERNATIVE_OUTCOMES = frozenset(
    {
        OccupancyOutcome.OCCUPIED_CAPACITY,
        OccupancyOutcome.BLOCKED_RISK,
        OccupancyOutcome.BLOCKED_NO_CAPITAL,
        OccupancyOutcome.BLOCKED_POSITION,
        OccupancyOutcome.UNKNOWN,
    }
)


class OccupancyError(ValueError):
    """Base class for refused occupancy operations."""

    code: str = "OCCUPANCY_ERROR"


class DoubleSpendError(OccupancyError):
    """Committed notional would exceed equity."""

    code = "OCCUPANCY_DOUBLE_SPEND"


class NotAdmittedError(OccupancyError):
    """Realized PnL was claimed for an opportunity that was never admitted."""

    code = "OCCUPANCY_NOT_ADMITTED"


class DuplicateRealizationError(OccupancyError):
    """A second, different realization was claimed for the same opportunity."""

    code = "OCCUPANCY_DUPLICATE_REALIZATION"


class DuplicateCounterfactualError(OccupancyError):
    """A second, different simulated PnL was claimed for the same denial."""

    code = "OCCUPANCY_DUPLICATE_COUNTERFACTUAL"


class CounterfactualOnAdmittedError(OccupancyError):
    """A simulated counterfactual was claimed for an actually admitted opportunity."""

    code = "OCCUPANCY_COUNTERFACTUAL_ON_ADMITTED"


class CounterfactualNotApplicableError(OccupancyError):
    """No counterfactual can be attributed to this outcome (e.g. expiry)."""

    code = "OCCUPANCY_COUNTERFACTUAL_NOT_APPLICABLE"


class UnknownOpportunityError(OccupancyError):
    """The opportunity was never evaluated by this ledger."""

    code = "OCCUPANCY_UNKNOWN_OPPORTUNITY"


def _require_finite(name: str, value: Decimal) -> Decimal:
    if not value.is_finite():
        raise ValueError(f"{name} must be finite")
    return value


class OccupancyRequest(BaseModel):
    """An opportunity asking for portfolio capital at one decision instant."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    opportunity_id: str = Field(min_length=1)
    instrument_id: str = Field(min_length=1)
    requested_notional: Decimal
    expires_at_ns: int = Field(ge=0)
    status: OpportunityStatus = OpportunityStatus.CANDIDATE

    @model_validator(mode="after")
    def _valid(self) -> Self:
        _require_finite("requested_notional", self.requested_notional)
        if self.requested_notional <= ZERO:
            raise ValueError("requested_notional must be positive")
        return self

    @property
    def is_terminal(self) -> bool:
        return self.status in (OpportunityStatus.EXPIRED, OpportunityStatus.INVALIDATED)


class CapacityState(BaseModel):
    """Observed portfolio capacity, PIT annotated. Missing values stay ``None``.

    ``committed_notional`` is capital already committed elsewhere, ``reserved``
    is risk reservations and ``risk_budget_notional`` (when supplied) is the
    risk-plane ceiling expressed in the same notional unit. The ledger takes the
    conservative union of its own ledger and the observed commitment, so a
    stale lower reading cannot free capital.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    as_of_ns: int = Field(ge=0)
    available_at_ns: int = Field(ge=0)
    source: str = Field(min_length=1)
    position: PositionPresence
    equity: Decimal | None = None
    committed_notional: Decimal | None = None
    reserved_notional: Decimal | None = None
    risk_budget_notional: Decimal | None = None
    position_notional: Decimal | None = None

    @model_validator(mode="after")
    def _valid(self) -> Self:
        for name in (
            "equity",
            "committed_notional",
            "reserved_notional",
            "risk_budget_notional",
            "position_notional",
        ):
            value = getattr(self, name)
            if value is not None:
                _require_finite(name, value)
                if value < ZERO:
                    raise ValueError(f"{name} must be non-negative")
        return self

    def digest(self) -> str:
        """Deterministic identity of the capacity reading used for an entry."""
        return canonical_digest(
            CAPACITY_TAG,
            [
                self.as_of_ns,
                self.available_at_ns,
                self.source,
                self.position.value,
                str(self.equity) if self.equity is not None else None,
                (str(self.committed_notional) if self.committed_notional is not None else None),
                (str(self.reserved_notional) if self.reserved_notional is not None else None),
                (str(self.risk_budget_notional) if self.risk_budget_notional is not None else None),
                (str(self.position_notional) if self.position_notional is not None else None),
            ],
        )

    def missing_fields(self) -> tuple[str, ...]:
        """Required capacity fields absent from this reading, in fixed order."""
        names = ("equity", "committed_notional", "reserved_notional", "position_notional")
        return tuple(name for name in names if getattr(self, name) is None)


class OccupancyEntry(BaseModel):
    """One immutable occupancy decision record.

    ``realized_pnl`` is only ever set for an ``ADMITTED`` opportunity and only by
    :meth:`OccupancyLedger.realize`. ``hypothetical_pnl`` is never set on a
    factual entry; the ledger keeps simulated numbers in separate records.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    entry_id: str = Field(min_length=1)
    opportunity_id: str = Field(min_length=1)
    instrument_id: str = Field(min_length=1)
    decision_time_ns: int = Field(ge=0)
    outcome: OccupancyOutcome
    reason: OccupancyReason
    requested_notional: Decimal
    committed_before: Decimal
    committed_after: Decimal
    exposure_before: Decimal
    exposure_after: Decimal
    available_capacity_after: Decimal
    equity: Decimal | None = None
    capacity_digest: str = Field(min_length=1)
    realized_pnl: Decimal | None = None
    hypothetical_pnl: Decimal | None = None

    @model_validator(mode="after")
    def _separation(self) -> Self:
        if self.realized_pnl is not None and self.hypothetical_pnl is not None:
            raise ValueError("realized and hypothetical PnL can never share one record")
        if self.realized_pnl is not None and not self.outcome.is_admitted:
            raise ValueError("realized PnL is only attributable to an admitted opportunity")
        if self.hypothetical_pnl is not None and self.outcome.is_admitted:
            raise ValueError("a hypothetical PnL cannot be attributed to an admission")
        return self

    @property
    def is_blocked(self) -> bool:
        return self.outcome.is_blocked


class CounterfactualPerformance(BaseModel):
    """Explicitly simulated performance of a denied alternative. Never money."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    opportunity_id: str = Field(min_length=1)
    entry_id: str = Field(min_length=1)
    decision_time_ns: int = Field(ge=0)
    outcome: OccupancyOutcome
    reason: OccupancyReason
    hypothetical_pnl: Decimal

    @model_validator(mode="after")
    def _not_realized(self) -> Self:
        if self.outcome.is_admitted:
            raise ValueError("counterfactual performance cannot describe an admission")
        _require_finite("hypothetical_pnl", self.hypothetical_pnl)
        return self


class OccupancyLedger:
    """Append-only factual occupancy log with separated simulated performance."""

    def __init__(self) -> None:
        self._entries: list[OccupancyEntry] = []
        self._entries_by_opportunity: dict[str, list[int]] = {}
        self._admitted: dict[str, int] = {}
        self._realized: dict[str, Decimal] = {}
        self._counterfactual: dict[str, CounterfactualPerformance] = {}
        self._committed: Decimal = ZERO

    # ---------------------------------------------------------------- evaluate

    def evaluate(
        self,
        request: OccupancyRequest,
        capacity: CapacityState,
        *,
        decision_time_ns: int,
    ) -> OccupancyEntry:
        """Record one factual occupancy decision. Deterministic for fixed inputs."""
        if decision_time_ns < 0:
            raise ValueError("decision_time_ns must be non-negative")
        outcome, reason = self._classify(request, capacity, decision_time_ns)
        committed_before = max(
            self._committed,
            capacity.committed_notional if capacity.committed_notional is not None else ZERO,
        )
        reserved = capacity.reserved_notional or ZERO
        position_notional = capacity.position_notional or ZERO
        exposure_before = committed_before + reserved + position_notional
        if outcome.is_admitted:
            committed_after = committed_before + request.requested_notional
            if capacity.equity is not None and committed_after > capacity.equity:
                # Unreachable while _classify is correct; fail closed if it is not.
                raise DoubleSpendError(
                    f"admitting {request.opportunity_id} would commit "
                    f"{committed_after} against equity {capacity.equity}"
                )
        else:
            committed_after = committed_before
        equity = capacity.equity if capacity.equity is not None else committed_after
        available_after = max(equity - committed_after, ZERO)
        exposure_after = (
            exposure_before + request.requested_notional if outcome.is_admitted else exposure_before
        )
        capacity_digest = capacity.digest()
        entry = OccupancyEntry(
            entry_id=canonical_digest(
                ENTRY_TAG,
                [
                    request.opportunity_id,
                    request.instrument_id,
                    decision_time_ns,
                    outcome.value,
                    reason.value,
                    str(request.requested_notional),
                    str(committed_before),
                    str(committed_after),
                    str(exposure_before),
                    str(exposure_after),
                    str(capacity.equity) if capacity.equity is not None else None,
                    capacity_digest,
                ],
            ),
            opportunity_id=request.opportunity_id,
            instrument_id=request.instrument_id,
            decision_time_ns=decision_time_ns,
            outcome=outcome,
            reason=reason,
            requested_notional=request.requested_notional,
            committed_before=committed_before,
            committed_after=committed_after,
            exposure_before=exposure_before,
            exposure_after=exposure_after,
            available_capacity_after=available_after,
            equity=capacity.equity,
            capacity_digest=capacity_digest,
        )
        self._entries_by_opportunity.setdefault(request.opportunity_id, []).append(
            len(self._entries)
        )
        self._entries.append(entry)
        if outcome.is_admitted:
            self._committed = committed_after
            self._admitted.setdefault(request.opportunity_id, len(self._entries) - 1)
        return entry

    def _classify(
        self,
        request: OccupancyRequest,
        capacity: CapacityState,
        decision_time_ns: int,
    ) -> tuple[OccupancyOutcome, OccupancyReason]:
        if request.status is OpportunityStatus.INVALIDATED:
            return OccupancyOutcome.INVALIDATED, OccupancyReason.OPPORTUNITY_INVALIDATED
        if request.status is OpportunityStatus.EXPIRED or (
            request.expires_at_ns < decision_time_ns
        ):
            return OccupancyOutcome.EXPIRED, OccupancyReason.OPPORTUNITY_EXPIRED
        if capacity.available_at_ns > decision_time_ns or capacity.as_of_ns > decision_time_ns:
            return OccupancyOutcome.UNKNOWN, OccupancyReason.CAPACITY_FUTURE
        if capacity.missing_fields() or capacity.position is PositionPresence.UNCERTAIN:
            return OccupancyOutcome.UNKNOWN, OccupancyReason.CAPACITY_UNKNOWN
        assert capacity.equity is not None
        assert capacity.committed_notional is not None
        if capacity.committed_notional > capacity.equity:
            return OccupancyOutcome.UNKNOWN, OccupancyReason.CAPACITY_INCONSISTENT
        if capacity.position is PositionPresence.OPEN:
            return OccupancyOutcome.BLOCKED_POSITION, OccupancyReason.POSITION_OPEN
        if capacity.position is PositionPresence.PENDING_INTENT:
            return OccupancyOutcome.BLOCKED_POSITION, OccupancyReason.POSITION_PENDING
        if request.opportunity_id in self._admitted:
            return OccupancyOutcome.OCCUPIED_CAPACITY, OccupancyReason.DUPLICATE_OPPORTUNITY
        committed_before = max(self._committed, capacity.committed_notional)
        reserved = capacity.reserved_notional or ZERO
        if (
            capacity.risk_budget_notional is not None
            and committed_before + reserved + request.requested_notional
            > capacity.risk_budget_notional
        ):
            return OccupancyOutcome.BLOCKED_RISK, OccupancyReason.RISK_RESERVED
        if committed_before + request.requested_notional > capacity.equity:
            if committed_before > ZERO:
                return OccupancyOutcome.OCCUPIED_CAPACITY, OccupancyReason.CAPACITY_CONSUMED
            return OccupancyOutcome.BLOCKED_NO_CAPITAL, OccupancyReason.NO_CAPITAL
        return OccupancyOutcome.ADMITTED, OccupancyReason.CAPACITY_AVAILABLE

    # ------------------------------------------------------- factual outcomes

    def realize(
        self,
        opportunity_id: str,
        *,
        realized_pnl: Decimal,
        decision_time_ns: int,
    ) -> None:
        """Attach *actual* realized PnL to an admitted opportunity.

        Denied opportunities raise :class:`NotAdmittedError`; a second different
        number raises :class:`DuplicateRealizationError`. Repeating the same
        realization is idempotent.
        """
        if opportunity_id not in self._admitted:
            raise NotAdmittedError(
                f"{opportunity_id} was never admitted; realized PnL is not attributable"
            )
        if decision_time_ns < 0:
            raise ValueError("decision_time_ns must be non-negative")
        _require_finite("realized_pnl", realized_pnl)
        prior = self._realized.get(opportunity_id)
        if prior is not None:
            if prior == realized_pnl:
                return
            raise DuplicateRealizationError(
                f"{opportunity_id} already realized {prior}, got {realized_pnl}"
            )
        self._realized[opportunity_id] = realized_pnl
        index = self._admitted[opportunity_id]
        self._entries[index] = self._entries[index].model_copy(
            update={"realized_pnl": realized_pnl}
        )

    def record_counterfactual(
        self,
        opportunity_id: str,
        *,
        hypothetical_pnl: Decimal,
        decision_time_ns: int,
    ) -> None:
        """Record *simulated* PnL for a capacity-denied alternative.

        Stored apart from every factual entry. Refused for admitted
        opportunities and for outcomes that are not denials by portfolio state
        (expiry/invalidation have no counterfactual here).
        """
        indices = self._entries_by_opportunity.get(opportunity_id)
        if not indices:
            raise UnknownOpportunityError(f"{opportunity_id} was never evaluated by this ledger")
        if opportunity_id in self._admitted:
            raise CounterfactualOnAdmittedError(
                f"{opportunity_id} was admitted; use realize() for factual PnL"
            )
        latest = self._entries[indices[-1]]
        if latest.outcome not in _ALTERNATIVE_OUTCOMES:
            raise CounterfactualNotApplicableError(
                f"outcome {latest.outcome.value} is not a capacity denial"
            )
        if decision_time_ns < 0:
            raise ValueError("decision_time_ns must be non-negative")
        _require_finite("hypothetical_pnl", hypothetical_pnl)
        prior = self._counterfactual.get(opportunity_id)
        if prior is not None:
            if prior.hypothetical_pnl == hypothetical_pnl:
                return
            raise DuplicateCounterfactualError(
                f"{opportunity_id} already has a simulated "
                f"{prior.hypothetical_pnl}, got {hypothetical_pnl}"
            )
        self._counterfactual[opportunity_id] = CounterfactualPerformance(
            opportunity_id=opportunity_id,
            entry_id=latest.entry_id,
            decision_time_ns=decision_time_ns,
            outcome=latest.outcome,
            reason=latest.reason,
            hypothetical_pnl=hypothetical_pnl,
        )

    # ---------------------------------------------------------------- queries

    def decision_log(self) -> tuple[OccupancyEntry, ...]:
        """Factual log of every evaluation, in decision order."""
        return tuple(self._entries)

    def matched_alternatives(self) -> tuple[OccupancyEntry, ...]:
        """Denials by observed portfolio state (capacity, position, risk)."""
        return tuple(entry for entry in self._entries if entry.outcome in _ALTERNATIVE_OUTCOMES)

    def counterfactual_performance(self) -> tuple[CounterfactualPerformance, ...]:
        """Simulated performance of denied alternatives, kept apart from facts."""
        return tuple(self._counterfactual.values())

    def entry(self, opportunity_id: str) -> OccupancyEntry | None:
        indices = self._entries_by_opportunity.get(opportunity_id)
        if not indices:
            return None
        return self._entries[indices[-1]]

    def realized_pnl(self, opportunity_id: str) -> Decimal | None:
        return self._realized.get(opportunity_id)

    def realized_pnl_total(self) -> Decimal:
        """Sum of factual realized PnL only. Hypotheticals cannot reach it."""
        return sum(self._realized.values(), ZERO)

    def hypothetical_pnl_total(self) -> Decimal:
        """Sum of simulated PnL only. Never mixed into realized totals."""
        return sum((record.hypothetical_pnl for record in self._counterfactual.values()), ZERO)

    @property
    def committed_notional(self) -> Decimal:
        return self._committed

    def assert_no_double_spend(self) -> None:
        """Committed notional may never exceed the equity seen at admission."""
        if self._committed < ZERO:
            raise DoubleSpendError("committed notional cannot be negative")
        for entry in self._entries:
            if entry.outcome.is_admitted:
                if entry.equity is not None and entry.committed_after > entry.equity:
                    raise DoubleSpendError(
                        f"{entry.opportunity_id} records committed "
                        f"{entry.committed_after} above equity {entry.equity}"
                    )
                if entry.committed_after != entry.committed_before + entry.requested_notional:
                    raise DoubleSpendError("an admission must add exactly the requested notional")
            elif entry.committed_after != entry.committed_before:
                raise DoubleSpendError("a denial cannot change committed capital")
        admitted_after = [
            entry.committed_after for entry in self._entries if entry.outcome.is_admitted
        ]
        if admitted_after and self._committed != max(admitted_after):
            raise DoubleSpendError("ledger commitment disagrees with the decision log")
