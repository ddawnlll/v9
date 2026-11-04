"""Constraint-aware portfolio allocation as a shadow challenger (M3-05).

The question is narrow: *given several simultaneous opportunities, one real
equity figure and hard portfolio constraints, which subset can be admitted, at
what rounded-down size, without spending the same capital twice?* The answer is
a shadow proposal. It is never submitted: this module imports no native engine
and constructs no order, adapter, campaign or execution handle. There is no
second portfolio ledger either -- ``committed`` and the per-instrument/per-sector
maps are readings of the inherited account state, and the only money this module
mints is the admitted notional it reports.

Deterministic ranking (declared policy, never input order):

``(-risk_priority, -confidence, opportunity_id, instrument, direction,
requested_notional)``

Higher ``risk_priority`` is considered first; equal priority prefers higher
``confidence``; remaining ties are broken by opportunity id and then by the
candidate's own identity fields, so shuffling the input cannot change the
admitted set. The extra identity fields only disambiguate two candidates that
share an opportunity id (the second is then a duplicate and is denied).

Hard constraints, checked per candidate in this order:

1. duplicate opportunity id in the batch -> ``BLOCKED_DUPLICATE_EXPOSURE``;
2. instrument already admitted in this batch -> ``BLOCKED_DUPLICATE_EXPOSURE``;
3. pre-existing open/reserved notional on the instrument -> ``BLOCKED_EXPOSURE``
   (no netting model exists here, so a scale-in fails closed);
4. unknown sector/correlation group -> ``BLOCKED_RISK``;
5. venue step/tick constraints unknown or disagreeing -> ``BLOCKED_PRECISION``;
6. capacity shortfall -> ``BLOCKED_CAP`` or ``BLOCKED_RISK`` (risk budget);
7. a legal quantity that rounds below the venue minimum -> ``BLOCKED_PRECISION``.

Capacity is ``min(equity, max_gross_exposure) - total_committed_notional``.
``max_per_sector`` is the hard ceiling for **both** the sector bucket and the
correlation-group bucket: two candidates in the same correlation cluster are
treated exactly like two candidates in the same sector. ``risk_budget`` is an
independent notional ceiling from the risk plane; existing committed capital is
charged against it conservatively (it may only make the challenger stricter).

Partial admission is deliberate: when a cap leaves less than the request but
enough for a legal lot, the candidate is admitted at the capped size and the
binding constraint is reported in ``AllocationOutcome.binding``. Residual
capacity below one step is a precision denial, never a silent rounding up.

Money is ``Decimal`` throughout. Counterfactual notes are kept in their own
container and never added to committed or admitted notional. A globally
fail-closed run (unknown equity/committed/cap/step input) records no
counterfactual notes: no allocation was attempted, so there is no denial to
attribute.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Final

from v8_next.economics.allocation import AllocationProposal, allocate_ordered
from v8_next.economics.controller import CampaignDecision
from v8_next.risk.admission import RiskLimits, RiskSnapshot
from v8_next.risk.sizing import StopBudget, StopExposure
from v8_next.telemetry.identity import canonical_digest

__all__ = [
    "AllocationCandidate",
    "AllocationOutcome",
    "BaselineAllocation",
    "ChallengeOutcome",
    "ChallengeReason",
    "ChallengeResult",
    "ChallengerComparison",
    "ComparisonRow",
    "CounterfactualNote",
    "allocate_challenger",
    "compare_to_baseline",
]

CHALLENGE_TAG: Final[str] = "portfolio-challenger-v1"
COMPARISON_NOTE: Final[str] = (
    "comparative-only: no economic superiority is asserted; deltas are accounting "
    "differences in admitted notional, not performance."
)
COUNTERFACTUAL_LABEL: Final[str] = "COUNTERFACTUAL_NOT_COMMITTED"
ZERO = Decimal("0")


class ChallengeOutcome(StrEnum):
    """Closed result set for one candidate. Every non-admission is a denial."""

    ADMITTED = "ADMITTED"
    BLOCKED_CAP = "BLOCKED_CAP"
    BLOCKED_EXPOSURE = "BLOCKED_EXPOSURE"
    BLOCKED_RISK = "BLOCKED_RISK"
    BLOCKED_PRECISION = "BLOCKED_PRECISION"
    BLOCKED_DUPLICATE_EXPOSURE = "BLOCKED_DUPLICATE_EXPOSURE"

    @property
    def is_admitted(self) -> bool:
        return self is ChallengeOutcome.ADMITTED


class ChallengeReason(StrEnum):
    """Stable, replayable discriminators. Never prose."""

    CAPACITY_AVAILABLE = "CAPACITY_AVAILABLE"
    NO_GROSS_CAPACITY = "NO_GROSS_CAPACITY"
    SECTOR_CAP_REACHED = "SECTOR_CAP_REACHED"
    CORRELATION_CAP_REACHED = "CORRELATION_CAP_REACHED"
    RISK_BUDGET_EXHAUSTED = "RISK_BUDGET_EXHAUSTED"
    EXISTING_INSTRUMENT_EXPOSURE = "EXISTING_INSTRUMENT_EXPOSURE"
    UNKNOWN_EXPOSURE_DETAIL = "UNKNOWN_EXPOSURE_DETAIL"
    INCONSISTENT_EXPOSURE_INPUT = "INCONSISTENT_EXPOSURE_INPUT"
    UNKNOWN_CONSTRAINTS = "UNKNOWN_CONSTRAINTS"
    UNKNOWN_SECTOR = "UNKNOWN_SECTOR"
    DUPLICATE_OPPORTUNITY = "DUPLICATE_OPPORTUNITY"
    INSTRUMENT_ALREADY_ADMITTED = "INSTRUMENT_ALREADY_ADMITTED"
    BELOW_VENUE_MINIMUM = "BELOW_VENUE_MINIMUM"
    STEP_SIZE_DISAGREEMENT = "STEP_SIZE_DISAGREEMENT"
    UNKNOWN_STEP_SIZE = "UNKNOWN_STEP_SIZE"
    PRICE_NOT_ON_TICK = "PRICE_NOT_ON_TICK"


_OUTCOME_BY_REASON: Final[dict[ChallengeReason, ChallengeOutcome]] = {
    ChallengeReason.CAPACITY_AVAILABLE: ChallengeOutcome.ADMITTED,
    ChallengeReason.NO_GROSS_CAPACITY: ChallengeOutcome.BLOCKED_CAP,
    ChallengeReason.SECTOR_CAP_REACHED: ChallengeOutcome.BLOCKED_CAP,
    ChallengeReason.CORRELATION_CAP_REACHED: ChallengeOutcome.BLOCKED_CAP,
    ChallengeReason.RISK_BUDGET_EXHAUSTED: ChallengeOutcome.BLOCKED_RISK,
    ChallengeReason.EXISTING_INSTRUMENT_EXPOSURE: ChallengeOutcome.BLOCKED_EXPOSURE,
    ChallengeReason.UNKNOWN_EXPOSURE_DETAIL: ChallengeOutcome.BLOCKED_EXPOSURE,
    ChallengeReason.INCONSISTENT_EXPOSURE_INPUT: ChallengeOutcome.BLOCKED_RISK,
    ChallengeReason.UNKNOWN_CONSTRAINTS: ChallengeOutcome.BLOCKED_RISK,
    ChallengeReason.UNKNOWN_SECTOR: ChallengeOutcome.BLOCKED_RISK,
    ChallengeReason.DUPLICATE_OPPORTUNITY: ChallengeOutcome.BLOCKED_DUPLICATE_EXPOSURE,
    ChallengeReason.INSTRUMENT_ALREADY_ADMITTED: ChallengeOutcome.BLOCKED_DUPLICATE_EXPOSURE,
    ChallengeReason.BELOW_VENUE_MINIMUM: ChallengeOutcome.BLOCKED_PRECISION,
    ChallengeReason.STEP_SIZE_DISAGREEMENT: ChallengeOutcome.BLOCKED_PRECISION,
    ChallengeReason.UNKNOWN_STEP_SIZE: ChallengeOutcome.BLOCKED_PRECISION,
    ChallengeReason.PRICE_NOT_ON_TICK: ChallengeOutcome.BLOCKED_PRECISION,
}

_DIRECTIONS: Final[frozenset[str]] = frozenset({"LONG", "SHORT"})


def _require_decimal(name: str, value: object) -> Decimal:
    if not isinstance(value, Decimal):
        raise TypeError(f"{name} must be Decimal, not {type(value).__name__}")
    if not value.is_finite():
        raise ValueError(f"{name} must be finite")
    return value


def _require_text(name: str, value: object) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be str")
    return value


@dataclass(frozen=True, slots=True)
class AllocationCandidate:
    """One simultaneous opportunity asking for capital.

    ``reference_price`` is the entry basis used to turn a requested notional into
    a venue quantity; it must already be on the venue tick. ``step_size`` is the
    candidate's declared lot step and is cross-checked against the authoritative
    ``step_sizes`` mapping supplied to :func:`allocate_challenger` -- a
    disagreement fails closed rather than silently picking one.
    """

    opportunity_id: str
    instrument: str
    direction: str
    requested_notional: Decimal
    risk_priority: int
    sector: str
    correlation_group: str
    reference_price: Decimal
    confidence: Decimal
    step_size: Decimal
    min_size: Decimal
    max_size: Decimal
    tick_size: Decimal
    min_notional: Decimal

    def __post_init__(self) -> None:
        if not self.opportunity_id:
            raise ValueError("opportunity_id must be non-empty")
        if not self.instrument:
            raise ValueError("instrument must be non-empty")
        if self.direction not in _DIRECTIONS:
            raise ValueError("direction must be LONG or SHORT")
        if type(self.risk_priority) is not int:
            raise TypeError("risk_priority must be int")
        _require_text("sector", self.sector)
        _require_text("correlation_group", self.correlation_group)
        for name in (
            "requested_notional",
            "reference_price",
            "confidence",
            "step_size",
            "min_size",
            "max_size",
            "tick_size",
            "min_notional",
        ):
            _require_decimal(name, getattr(self, name))
        if self.requested_notional <= ZERO:
            raise ValueError("requested_notional must be positive")
        if self.reference_price <= ZERO:
            raise ValueError("reference_price must be positive")
        if not ZERO <= self.confidence <= Decimal(1):
            raise ValueError("confidence must lie in [0, 1]")
        if self.step_size <= ZERO:
            raise ValueError("step_size must be positive")
        if self.tick_size <= ZERO:
            raise ValueError("tick_size must be positive")
        if self.min_size < ZERO or self.max_size < self.min_size:
            raise ValueError("invalid venue size bounds")
        if self.max_size <= ZERO:
            raise ValueError("max_size must be positive")
        if self.min_notional < ZERO:
            raise ValueError("min_notional must be non-negative")


@dataclass(frozen=True, slots=True)
class AllocationOutcome:
    """One candidate's shadow allocation result. Carries no order authority."""

    opportunity_id: str
    instrument: str
    outcome: ChallengeOutcome
    reason: ChallengeReason
    requested_notional: Decimal
    admitted_quantity: Decimal
    admitted_notional: Decimal
    risk_priority: int
    #: Which cap truncated an ADMITTED candidate below its request, if any.
    binding: ChallengeReason | None = None
    #: Named input field(s) behind a fail-closed denial.
    detail: str = ""

    def __post_init__(self) -> None:
        expected = _OUTCOME_BY_REASON[self.reason]
        if expected is not self.outcome:
            raise ValueError("reason and outcome disagree")
        if self.outcome.is_admitted:
            if self.admitted_quantity <= ZERO or self.admitted_notional <= ZERO:
                raise ValueError("an admission must carry positive size")
            if self.binding is ChallengeReason.CAPACITY_AVAILABLE:
                raise ValueError("an admission cannot bind on CAPACITY_AVAILABLE")
        else:
            if self.admitted_quantity != ZERO or self.admitted_notional != ZERO:
                raise ValueError("a denial cannot admit notional")
            if self.binding is not None:
                raise ValueError("a denial cannot carry a binding cap")

    @property
    def is_admitted(self) -> bool:
        return self.outcome.is_admitted


@dataclass(frozen=True, slots=True)
class CounterfactualNote:
    """A denied request, kept apart from committed and admitted money.

    ``hypothetical_notional`` is what the candidate asked for. It is never added
    to ``ChallengeResult.total_committed_notional`` and it is not PnL.
    """

    opportunity_id: str
    instrument: str
    hypothetical_notional: Decimal
    reason: ChallengeReason
    label: str = COUNTERFACTUAL_LABEL

    def __post_init__(self) -> None:
        _require_decimal("hypothetical_notional", self.hypothetical_notional)
        if self.hypothetical_notional <= ZERO:
            raise ValueError("a counterfactual note needs a positive request")
        if self.label != COUNTERFACTUAL_LABEL:
            raise ValueError("counterfactual notes must carry the not-committed label")


@dataclass(frozen=True, slots=True)
class ChallengeResult:
    """The shadow outcome of one allocation run over one account reading."""

    outcomes: tuple[AllocationOutcome, ...]
    opening_committed_notional: Decimal
    admitted_notional: Decimal
    total_committed_notional: Decimal
    remaining_capacity: Decimal
    counterfactual: tuple[CounterfactualNote, ...] = ()
    blocked_inputs: tuple[str, ...] = ()
    digest: str = ""

    def __post_init__(self) -> None:
        for name in (
            "opening_committed_notional",
            "admitted_notional",
            "total_committed_notional",
            "remaining_capacity",
        ):
            value = getattr(self, name)
            _require_decimal(name, value)
            if value < ZERO:
                raise ValueError(f"{name} must be non-negative")
        if self.total_committed_notional != (
            self.opening_committed_notional + self.admitted_notional
        ):
            raise ValueError("committed total must be opening plus admitted notional")
        if self.admitted_notional > ZERO and self.blocked_inputs:
            raise ValueError("a fail-closed run cannot admit notional")
        admitted = {o.opportunity_id for o in self.outcomes if o.is_admitted}
        noted = {n.opportunity_id for n in self.counterfactual}
        unique_ids = len({o.opportunity_id for o in self.outcomes}) == len(self.outcomes)
        if unique_ids and admitted & noted:
            raise ValueError("an admitted candidate cannot also be counterfactual")

    @property
    def fail_closed(self) -> bool:
        return bool(self.blocked_inputs)

    @property
    def admitted_opportunity_ids(self) -> tuple[str, ...]:
        return tuple(o.opportunity_id for o in self.outcomes if o.is_admitted)

    @property
    def counterfactual_hypothetical_notional(self) -> Decimal:
        """Sum of denied requests. Hypothetical only; not committed money."""
        return sum((note.hypothetical_notional for note in self.counterfactual), ZERO)

    def outcome_for(self, opportunity_id: str) -> AllocationOutcome | None:
        for outcome in self.outcomes:
            if outcome.opportunity_id == opportunity_id:
                return outcome
        return None


@dataclass(frozen=True, slots=True)
class BaselineAllocation:
    """The inherited V8 allocator's exact inputs, kept explicit for comparison."""

    proposals: tuple[AllocationProposal, ...]
    snapshots: Mapping[str, RiskSnapshot]
    limits: RiskLimits
    decision_ns: int
    already_allocated: frozenset[str] = frozenset()
    stop_budget: StopBudget | None = None
    stop_exposure: StopExposure | None = None

    def run(self) -> tuple[CampaignDecision, ...]:
        return allocate_ordered(
            self.proposals,
            dict(self.snapshots),
            self.limits,
            decision_ns=self.decision_ns,
            already_allocated=self.already_allocated,
            stop_budget=self.stop_budget,
            stop_exposure=self.stop_exposure,
        )


@dataclass(frozen=True, slots=True)
class ComparisonRow:
    """Baseline versus challenger admitted notional for one opportunity."""

    opportunity_id: str
    instrument: str
    baseline_admitted: bool
    baseline_reason: str
    baseline_quantity: Decimal
    baseline_notional: Decimal
    challenger_outcome: ChallengeOutcome
    challenger_quantity: Decimal
    challenger_notional: Decimal
    delta_notional: Decimal


@dataclass(frozen=True, slots=True)
class ChallengerComparison:
    """Side-by-side accounting comparison. It asserts no economic preference."""

    rows: tuple[ComparisonRow, ...]
    baseline_total_notional: Decimal
    challenger_total_notional: Decimal
    delta_notional: Decimal
    challenger: ChallengeResult
    note: str = COMPARISON_NOTE

    def __post_init__(self) -> None:
        if self.delta_notional != self.challenger_total_notional - self.baseline_total_notional:
            raise ValueError("comparison delta must equal challenger minus baseline")


@dataclass(frozen=True, slots=True)
class _ExposureBook:
    instruments: dict[str, Decimal]
    sectors: dict[str, Decimal]


def _rank_key(candidate: AllocationCandidate) -> tuple[int, Decimal, str, str, str, Decimal]:
    return (
        -candidate.risk_priority,
        -candidate.confidence,
        candidate.opportunity_id,
        candidate.instrument,
        candidate.direction,
        candidate.requested_notional,
    )


def _floor_to_step(value: Decimal, step: Decimal) -> Decimal:
    """Round down to ``step`` exactly; a positive value and step are assumed."""
    return value - (value % step)


def _resolve_constraints(
    *,
    equity: Decimal | None,
    committed: Decimal | None,
    max_gross_exposure: Decimal | None,
    max_per_sector: Decimal | None,
    risk_budget: Decimal | None,
    step_sizes: Mapping[str, Decimal] | None,
) -> tuple[str, ...]:
    """Names of absent or malformed constraint inputs, in declared order."""
    blocked: list[str] = []
    for name, value in (
        ("equity", equity),
        ("committed", committed),
        ("max_gross_exposure", max_gross_exposure),
        ("max_per_sector", max_per_sector),
        ("risk_budget", risk_budget),
    ):
        if not isinstance(value, Decimal) or not value.is_finite() or value < ZERO:
            blocked.append(name)
    if step_sizes is None:
        blocked.append("step_sizes")
    else:
        for instrument, step in step_sizes.items():
            if not instrument or not isinstance(step, Decimal):
                blocked.append("step_sizes")
                break
            if not step.is_finite() or step <= ZERO:
                blocked.append("step_sizes")
                break
    return tuple(blocked)


def _resolve_exposure(
    committed: Decimal,
    instrument_committed: Mapping[str, Decimal] | None,
    sector_committed: Mapping[str, Decimal] | None,
) -> tuple[_ExposureBook, ChallengeReason | None]:
    """Attribute existing commitment per instrument and sector, or fail closed.

    Every committed unit must be attributable to one instrument and one sector:
    the maps must both sum to ``committed``. Untraceable capital could otherwise
    hide inside an unmodelled bucket and let the challenger over-allocate.
    """
    instruments = dict(instrument_committed or {})
    sectors = dict(sector_committed or {})
    if committed > ZERO and (instrument_committed is None or sector_committed is None):
        return _ExposureBook({}, {}), ChallengeReason.UNKNOWN_EXPOSURE_DETAIL
    for book in (instruments, sectors):
        for key, value in book.items():
            if not key or not isinstance(value, Decimal) or not value.is_finite():
                return _ExposureBook({}, {}), ChallengeReason.INCONSISTENT_EXPOSURE_INPUT
            if value < ZERO:
                return _ExposureBook({}, {}), ChallengeReason.INCONSISTENT_EXPOSURE_INPUT
    if sum(instruments.values(), ZERO) != committed:
        return _ExposureBook({}, {}), ChallengeReason.INCONSISTENT_EXPOSURE_INPUT
    if sum(sectors.values(), ZERO) != committed:
        return _ExposureBook({}, {}), ChallengeReason.INCONSISTENT_EXPOSURE_INPUT
    return _ExposureBook(instruments, sectors), None


def _blocked(
    candidate: AllocationCandidate,
    reason: ChallengeReason,
    *,
    detail: str = "",
) -> AllocationOutcome:
    return AllocationOutcome(
        opportunity_id=candidate.opportunity_id,
        instrument=candidate.instrument,
        outcome=_OUTCOME_BY_REASON[reason],
        reason=reason,
        requested_notional=candidate.requested_notional,
        admitted_quantity=ZERO,
        admitted_notional=ZERO,
        risk_priority=candidate.risk_priority,
        detail=detail,
    )


def _result_digest(
    outcomes: Sequence[AllocationOutcome],
    *,
    opening_committed_notional: Decimal,
    admitted_notional: Decimal,
    remaining_capacity: Decimal,
    blocked_inputs: Sequence[str],
) -> str:
    payload: list[list[object]] = [
        [outcome.opportunity_id, outcome.outcome.value, outcome.reason.value]
        + [
            str(outcome.admitted_quantity),
            str(outcome.admitted_notional),
            outcome.binding.value if outcome.binding is not None else None,
        ]
        for outcome in outcomes
    ]
    return canonical_digest(
        CHALLENGE_TAG,
        [
            payload,
            str(opening_committed_notional),
            str(admitted_notional),
            str(remaining_capacity),
            list(blocked_inputs),
        ],
    )


def _fail_closed(
    ordered: Sequence[AllocationCandidate],
    blocked_inputs: Sequence[str],
    committed: Decimal | None,
) -> ChallengeResult:
    detail = ",".join(blocked_inputs)
    outcomes = tuple(
        _blocked(candidate, ChallengeReason.UNKNOWN_CONSTRAINTS, detail=detail)
        for candidate in ordered
    )
    opening = committed if isinstance(committed, Decimal) and committed.is_finite() else ZERO
    return ChallengeResult(
        outcomes=outcomes,
        opening_committed_notional=opening,
        admitted_notional=ZERO,
        total_committed_notional=opening,
        remaining_capacity=ZERO,
        counterfactual=(),
        blocked_inputs=tuple(blocked_inputs),
        digest=_result_digest(
            outcomes,
            opening_committed_notional=opening,
            admitted_notional=ZERO,
            remaining_capacity=ZERO,
            blocked_inputs=blocked_inputs,
        ),
    )


def allocate_challenger(
    candidates: Sequence[AllocationCandidate],
    *,
    equity: Decimal | None,
    committed: Decimal | None,
    max_gross_exposure: Decimal | None,
    max_per_sector: Decimal | None,
    risk_budget: Decimal | None,
    step_sizes: Mapping[str, Decimal] | None,
    instrument_committed: Mapping[str, Decimal] | None = None,
    sector_committed: Mapping[str, Decimal] | None = None,
) -> ChallengeResult:
    """Allocate the candidates under hard constraints. Shadow only.

    Every constraint is mandatory. ``None`` (or a non-finite/negative value) is
    *unknown*, not zero, and yields a fail-closed result in which no candidate is
    admitted. When ``committed`` is positive the caller must also supply both
    per-instrument and per-sector attribution maps that sum to it.

    Existing exposure on a given instrument blocks a new candidate on that same
    instrument: this allocator has no netting or scale-in model, so admitting it
    would be an unmodelled double spend. Existing exposure does count against the
    global gross capacity, the per-sector ceiling and the risk budget.
    """
    ordered = sorted(candidates, key=_rank_key)
    blocked_inputs = _resolve_constraints(
        equity=equity,
        committed=committed,
        max_gross_exposure=max_gross_exposure,
        max_per_sector=max_per_sector,
        risk_budget=risk_budget,
        step_sizes=step_sizes,
    )
    if blocked_inputs:
        return _fail_closed(ordered, blocked_inputs, committed)

    # Narrowed by _resolve_constraints; bound explicitly for the type checker.
    assert equity is not None and committed is not None
    assert max_gross_exposure is not None and max_per_sector is not None
    assert risk_budget is not None and step_sizes is not None

    book, exposure_reason = _resolve_exposure(committed, instrument_committed, sector_committed)
    if exposure_reason is not None:
        blocked_outcomes = tuple(
            _blocked(candidate, exposure_reason) for candidate in ordered
        )
        return ChallengeResult(
            outcomes=blocked_outcomes,
            opening_committed_notional=committed,
            admitted_notional=ZERO,
            total_committed_notional=committed,
            remaining_capacity=ZERO,
            counterfactual=(),
            digest=_result_digest(
                blocked_outcomes,
                opening_committed_notional=committed,
                admitted_notional=ZERO,
                remaining_capacity=ZERO,
                blocked_inputs=(),
            ),
        )

    gross_cap = min(equity, max_gross_exposure)
    total_committed = committed
    sector_used = dict(book.sectors)
    correlation_used: dict[str, Decimal] = {}
    instrument_admitted: set[str] = set()
    seen_ids: set[str] = set()

    outcomes: list[AllocationOutcome] = []
    counterfactual: list[CounterfactualNote] = []
    for candidate in ordered:
        if candidate.opportunity_id in seen_ids:
            outcome = _blocked(candidate, ChallengeReason.DUPLICATE_OPPORTUNITY)
        elif candidate.instrument in instrument_admitted:
            outcome = _blocked(candidate, ChallengeReason.INSTRUMENT_ALREADY_ADMITTED)
        elif book.instruments.get(candidate.instrument, ZERO) > ZERO:
            outcome = _blocked(
                candidate,
                ChallengeReason.EXISTING_INSTRUMENT_EXPOSURE,
                detail=f"{candidate.instrument}={book.instruments[candidate.instrument]}",
            )
        elif not candidate.sector or not candidate.correlation_group:
            outcome = _blocked(
                candidate,
                ChallengeReason.UNKNOWN_SECTOR,
                detail="sector" if not candidate.sector else "correlation_group",
            )
        elif step_sizes.get(candidate.instrument) is None:
            outcome = _blocked(
                candidate,
                ChallengeReason.UNKNOWN_STEP_SIZE,
                detail=candidate.instrument,
            )
        elif step_sizes[candidate.instrument] != candidate.step_size:
            outcome = _blocked(
                candidate,
                ChallengeReason.STEP_SIZE_DISAGREEMENT,
                detail=f"{candidate.instrument}={step_sizes[candidate.instrument]}",
            )
        elif candidate.reference_price % candidate.tick_size != ZERO:
            outcome = _blocked(
                candidate,
                ChallengeReason.PRICE_NOT_ON_TICK,
                detail=f"{candidate.reference_price}/{candidate.tick_size}",
            )
        else:
            outcome = _evaluate(
                candidate,
                gross_cap=gross_cap,
                total_committed=total_committed,
                max_per_sector=max_per_sector,
                risk_budget=risk_budget,
                sector_used=sector_used,
                correlation_used=correlation_used,
            )
        seen_ids.add(candidate.opportunity_id)
        outcomes.append(outcome)
        if outcome.is_admitted:
            instrument_admitted.add(candidate.instrument)
            total_committed += outcome.admitted_notional
            sector_used[candidate.sector] = (
                sector_used.get(candidate.sector, ZERO) + outcome.admitted_notional
            )
            correlation_used[candidate.correlation_group] = (
                correlation_used.get(candidate.correlation_group, ZERO)
                + outcome.admitted_notional
            )
        else:
            counterfactual.append(
                CounterfactualNote(
                    opportunity_id=candidate.opportunity_id,
                    instrument=candidate.instrument,
                    hypothetical_notional=candidate.requested_notional,
                    reason=outcome.reason,
                )
            )

    admitted_notional = total_committed - committed
    remaining = max(ZERO, gross_cap - total_committed)
    return ChallengeResult(
        outcomes=tuple(outcomes),
        opening_committed_notional=committed,
        admitted_notional=admitted_notional,
        total_committed_notional=total_committed,
        remaining_capacity=remaining,
        counterfactual=tuple(counterfactual),
        digest=_result_digest(
            outcomes,
            opening_committed_notional=committed,
            admitted_notional=admitted_notional,
            remaining_capacity=remaining,
            blocked_inputs=(),
        ),
    )


def _evaluate(
    candidate: AllocationCandidate,
    *,
    gross_cap: Decimal,
    total_committed: Decimal,
    max_per_sector: Decimal,
    risk_budget: Decimal,
    sector_used: Mapping[str, Decimal],
    correlation_used: Mapping[str, Decimal],
) -> AllocationOutcome:
    """Capacity and precision evaluation for one candidate."""
    price = candidate.reference_price
    step = candidate.step_size
    max_request = min(candidate.requested_notional, candidate.max_size * price)
    legal_quantity = _floor_to_step(max_request / price, step)
    if (
        legal_quantity <= ZERO
        or legal_quantity < candidate.min_size
        or legal_quantity * price < candidate.min_notional
    ):
        return _blocked(candidate, ChallengeReason.BELOW_VENUE_MINIMUM)

    gross_remaining = gross_cap - total_committed
    sector_remaining = max_per_sector - sector_used.get(candidate.sector, ZERO)
    correlation_remaining = max_per_sector - correlation_used.get(candidate.correlation_group, ZERO)
    risk_remaining = risk_budget - total_committed

    capacity = candidate.requested_notional
    binding: ChallengeReason | None = None
    for remaining, reason in (
        (gross_remaining, ChallengeReason.NO_GROSS_CAPACITY),
        (sector_remaining, ChallengeReason.SECTOR_CAP_REACHED),
        (correlation_remaining, ChallengeReason.CORRELATION_CAP_REACHED),
        (risk_remaining, ChallengeReason.RISK_BUDGET_EXHAUSTED),
    ):
        if remaining < capacity:
            capacity = remaining
            binding = reason

    if capacity <= ZERO:
        return _blocked(candidate, binding or ChallengeReason.NO_GROSS_CAPACITY)

    capped = min(capacity, candidate.max_size * price)
    quantity = _floor_to_step(capped / price, step)
    if quantity <= ZERO or quantity < candidate.min_size:
        return _blocked(candidate, ChallengeReason.BELOW_VENUE_MINIMUM)
    admitted_notional = quantity * price
    if admitted_notional < candidate.min_notional:
        return _blocked(candidate, ChallengeReason.BELOW_VENUE_MINIMUM)
    full_request = admitted_notional >= candidate.requested_notional
    return AllocationOutcome(
        opportunity_id=candidate.opportunity_id,
        instrument=candidate.instrument,
        outcome=ChallengeOutcome.ADMITTED,
        reason=ChallengeReason.CAPACITY_AVAILABLE,
        requested_notional=candidate.requested_notional,
        admitted_quantity=quantity,
        admitted_notional=admitted_notional,
        risk_priority=candidate.risk_priority,
        binding=None if full_request else binding,
    )


def compare_to_baseline(
    candidates: Sequence[AllocationCandidate],
    baseline: BaselineAllocation,
    *,
    equity: Decimal | None,
    committed: Decimal | None,
    max_gross_exposure: Decimal | None,
    max_per_sector: Decimal | None,
    risk_budget: Decimal | None,
    step_sizes: Mapping[str, Decimal] | None,
    instrument_committed: Mapping[str, Decimal] | None = None,
    sector_committed: Mapping[str, Decimal] | None = None,
) -> ChallengerComparison:
    """Run the inherited allocator and the challenger over the same opportunities.

    The opportunity set must match exactly (id, instrument and unique ids on both
    sides), and the challenger's equity must equal the baseline snapshots' equity,
    so a difference in the rows is attributable to the allocation policy rather
    than to different inputs. The comparison reports accounting deltas only: it
    makes no claim about which allocator is economically better.
    """
    proposals = baseline.proposals
    proposal_ids = [proposal.opportunity.opportunity_id for proposal in proposals]
    if len(set(proposal_ids)) != len(proposal_ids):
        raise ValueError("baseline proposals must have unique opportunity ids")
    candidate_ids = [candidate.opportunity_id for candidate in candidates]
    if len(set(candidate_ids)) != len(candidate_ids):
        raise ValueError("comparison requires unique candidate opportunity ids")
    if set(candidate_ids) != set(proposal_ids):
        raise ValueError("comparison requires identical opportunity sets")

    by_id = {proposal.opportunity.opportunity_id: proposal for proposal in proposals}
    for candidate in candidates:
        proposal = by_id[candidate.opportunity_id]
        if candidate.instrument != proposal.opportunity.instrument_id:
            raise ValueError("candidate instrument disagrees with the baseline proposal")
    if equity is not None and any(snapshot.equity != equity for snapshot in baseline.snapshots.values()):
        raise ValueError("comparison requires the baseline and challenger to share equity")

    result = allocate_challenger(
        candidates,
        equity=equity,
        committed=committed,
        max_gross_exposure=max_gross_exposure,
        max_per_sector=max_per_sector,
        risk_budget=risk_budget,
        step_sizes=step_sizes,
        instrument_committed=instrument_committed,
        sector_committed=sector_committed,
    )
    decisions = baseline.run()
    if len(decisions) != len(proposals):
        raise ValueError("baseline allocator returned an incomplete decision set")

    rows: list[ComparisonRow] = []
    baseline_total = ZERO
    for decision, proposal in zip(decisions, proposals, strict=True):
        opportunity_id = proposal.opportunity.opportunity_id
        campaign = decision.campaign
        if campaign is not None and campaign.opportunity_id != opportunity_id:
            raise ValueError("baseline campaign identity disagrees with its proposal")
        quantity = campaign.quantity if campaign is not None else ZERO
        notional = quantity * proposal.price
        challenger = result.outcome_for(opportunity_id)
        if challenger is None:
            raise ValueError("challenger result is missing an opportunity")
        baseline_total += notional
        rows.append(
            ComparisonRow(
                opportunity_id=opportunity_id,
                instrument=proposal.opportunity.instrument_id,
                baseline_admitted=campaign is not None,
                baseline_reason=decision.reason,
                baseline_quantity=quantity,
                baseline_notional=notional,
                challenger_outcome=challenger.outcome,
                challenger_quantity=challenger.admitted_quantity,
                challenger_notional=challenger.admitted_notional,
                delta_notional=challenger.admitted_notional - notional,
            )
        )
    rows.sort(key=lambda row: row.opportunity_id)
    challenger_total = sum((row.challenger_notional for row in rows), ZERO)
    if challenger_total != result.admitted_notional:
        raise ValueError("challenger admitted notional disagrees with its own outcomes")
    return ChallengerComparison(
        rows=tuple(rows),
        baseline_total_notional=baseline_total,
        challenger_total_notional=challenger_total,
        delta_notional=challenger_total - baseline_total,
        challenger=result,
    )
