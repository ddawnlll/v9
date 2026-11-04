"""Independent funding-boundary and causal-equity reconciliation (M0-06).

The engine already plans funding events at bar boundaries
(``adapters/portfolio_backtest.py``; regression
``tests/test_funding_boundary_attribution_20261104.py``). This module is a second,
independent ledger over hand-supplied records. It answers three separate questions
without collapsing them:

1. **Boundary attribution** — was the settlement computed from the position that
   existed at the venue boundary, using a mark that was already available then?
2. **Receipt finality** — did the settlement reach us after the boundary (delayed),
   and was it received before the accounting cut it is claimed under?
3. **Equity identity** — does the supplied final equity equal
   ``initial + realized cashflows + funding + mark-to-market change``?

Unknown exposure and a missing causal mark are typed ``BLOCKED`` outcomes. They are
never treated as zero funding. A settlement whose venue boundary is after the
as-of cut is likewise not attributable: it is typed
``FUNDING_BOUNDARY_IN_FUTURE``, excluded from ``funding_total`` and the equity
identity, and the report cannot pass. Nothing here places orders or reads an
engine report.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator

ZERO = Decimal("0")
EPSILON = Decimal("1e-8")


class FundingIssueCode(StrEnum):
    DUPLICATE_SETTLEMENT = "DUPLICATE_SETTLEMENT"
    DUPLICATE_BOUNDARY_CLAIM = "DUPLICATE_BOUNDARY_CLAIM"
    MISSING_EXPOSURE = "MISSING_EXPOSURE"
    NO_CAUSAL_MARK = "NO_CAUSAL_MARK"
    NOT_FINAL_AT_ACCOUNTING_TIME = "NOT_FINAL_AT_ACCOUNTING_TIME"
    FUNDING_MISMATCH = "FUNDING_MISMATCH"
    INSTRUMENT_MISMATCH = "INSTRUMENT_MISMATCH"
    CAMPAIGN_MISMATCH = "CAMPAIGN_MISMATCH"
    MISSING_FINAL_MARK = "MISSING_FINAL_MARK"
    EQUITY_MISMATCH = "EQUITY_MISMATCH"
    FUNDING_BOUNDARY_IN_FUTURE = "FUNDING_BOUNDARY_IN_FUTURE"
    INVALID_RECORD = "INVALID_RECORD"


class FundingReportStatus(StrEnum):
    PASS = "PASS"
    BLOCKED = "BLOCKED"
    MISMATCH = "MISMATCH"


class FundingIssue(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    code: FundingIssueCode
    record_id: str
    detail: str


class PositionEvent(BaseModel):
    """One causal quantity change, available at ``available_at_ns``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    event_id: str = Field(min_length=1)
    campaign_id: str = Field(min_length=1)
    instrument_id: str = Field(min_length=1)
    quantity_delta: Decimal
    event_time_ns: int = Field(ge=0)
    available_at_ns: int = Field(ge=0)

    @field_validator("quantity_delta")
    @classmethod
    def _finite(cls, value: Decimal) -> Decimal:
        if not value.is_finite():
            raise ValueError("quantity_delta must be finite")
        return value


class PriceMark(BaseModel):
    """One mark; ``available_at_ns`` is when a decision could first use it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    instrument_id: str = Field(min_length=1)
    price: Decimal
    event_time_ns: int = Field(ge=0)
    available_at_ns: int = Field(ge=0)

    @field_validator("price")
    @classmethod
    def _positive(cls, value: Decimal) -> Decimal:
        if not value.is_finite() or value <= ZERO:
            raise ValueError("mark price must be finite and positive")
        return value

    @field_validator("available_at_ns")
    @classmethod
    def _availability(cls, value: int) -> int:
        if value < 0:
            raise ValueError("availability must be non-negative")
        return value


class FundingSettlement(BaseModel):
    """A venue funding settlement with its own boundary and receipt clocks."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    settlement_id: str = Field(min_length=1)
    campaign_id: str = Field(min_length=1)
    instrument_id: str = Field(min_length=1)
    amount: Decimal
    rate: Decimal
    boundary_time_ns: int = Field(ge=0)
    received_at_ns: int = Field(ge=0)
    accounting_time_ns: int | None = Field(default=None, ge=0)

    @field_validator("amount", "rate")
    @classmethod
    def _finite(cls, value: Decimal) -> Decimal:
        if not value.is_finite():
            raise ValueError("amount and rate must be finite")
        return value


class FundingAttribution(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    settlement_id: str
    campaign_id: str
    instrument_id: str
    quantity_at_boundary: Decimal
    mark_price: Decimal
    expected_amount: Decimal
    actual_amount: Decimal
    difference: Decimal
    delayed: bool
    agreed: bool


class EquityReconciliation(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    initial_equity: Decimal
    realized_cashflows: Decimal
    funding_total: Decimal
    unrealized_change: Decimal
    expected_final_equity: Decimal
    provided_final_equity: Decimal | None
    difference: Decimal | None

    @property
    def agreed(self) -> bool:
        return self.difference is not None and self.difference <= EPSILON


class FundingBoundaryReport(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    status: FundingReportStatus
    as_of_ns: int
    attributions: tuple[FundingAttribution, ...]
    equity: EquityReconciliation | None
    issues: tuple[FundingIssue, ...] = ()

    @property
    def passed(self) -> bool:
        return self.status is FundingReportStatus.PASS


def _position_events_by_key(
    events: Iterable[PositionEvent],
) -> tuple[dict[tuple[str, str], tuple[PositionEvent, ...]], list[FundingIssue]]:
    grouped: dict[tuple[str, str], list[PositionEvent]] = {}
    seen: set[str] = set()
    issues: list[FundingIssue] = []
    for event in events:
        if event.event_id in seen:
            issues.append(
                FundingIssue(
                    code=FundingIssueCode.INVALID_RECORD,
                    record_id=event.event_id,
                    detail="duplicate position event id",
                )
            )
            continue
        seen.add(event.event_id)
        grouped.setdefault((event.campaign_id, event.instrument_id), []).append(event)
    return (
        {
            key: tuple(sorted(items, key=lambda item: (item.event_time_ns, item.event_id)))
            for key, items in grouped.items()
        },
        issues,
    )


def _quantity_at(
    events: tuple[PositionEvent, ...] | None, cut_ns: int
) -> tuple[Decimal, bool]:
    """Return (quantity, known). ``known`` is false when nothing was available at cut."""
    if not events:
        return ZERO, False
    available = [
        item
        for item in events
        if item.available_at_ns <= cut_ns and item.event_time_ns <= cut_ns
    ]
    if not available:
        return ZERO, False
    return sum((item.quantity_delta for item in available), ZERO), True


def _mark_at(
    marks: tuple[PriceMark, ...] | None, cut_ns: int
) -> Decimal | None:
    """Latest mark whose event and availability both precede ``cut_ns``."""
    if not marks:
        return None
    causal = [
        item
        for item in marks
        if item.event_time_ns <= cut_ns and item.available_at_ns <= cut_ns
    ]
    if not causal:
        return None
    return max(causal, key=lambda item: (item.event_time_ns, item.available_at_ns)).price


def reconcile_funding_boundary(
    *,
    initial_equity: Decimal,
    settlements: Iterable[FundingSettlement],
    position_events: Iterable[PositionEvent],
    marks: Iterable[PriceMark],
    as_of_ns: int,
    opening_time_ns: int = 0,
    realized_cashflows: Decimal = ZERO,
    final_equity: Decimal | None = None,
    position_history_complete: bool = True,
) -> FundingBoundaryReport:
    """Independently attribute funding and reconcile the equity identity."""
    issues: list[FundingIssue] = []
    settlement_list = list(settlements)
    mark_list = list(marks)
    grouped_events, grouping_issues = _position_events_by_key(position_events)
    issues.extend(grouping_issues)
    marks_by_instrument: dict[str, tuple[PriceMark, ...]] = {}
    for instrument_id in {item.instrument_id for item in mark_list}:
        marks_by_instrument[instrument_id] = tuple(
            sorted(
                (item for item in mark_list if item.instrument_id == instrument_id),
                key=lambda item: (item.event_time_ns, item.available_at_ns),
            )
        )

    seen_settlements: set[str] = set()
    seen_boundaries: dict[tuple[str, str, int], str] = {}
    attributions: list[FundingAttribution] = []
    funding_total = ZERO
    for settlement in settlement_list:
        if settlement.settlement_id in seen_settlements:
            issues.append(
                FundingIssue(
                    code=FundingIssueCode.DUPLICATE_SETTLEMENT,
                    record_id=settlement.settlement_id,
                    detail="settlement id charged twice",
                )
            )
            continue
        seen_settlements.add(settlement.settlement_id)
        if settlement.boundary_time_ns > as_of_ns:
            # A settlement whose venue boundary lies beyond the accounting cut
            # cannot be attributed at this cut: its quantity and mark would be
            # read after the cut (look-ahead), and folding its cashflow into
            # ``funding_total`` would move an as-of equity by money not yet
            # earned. Record the typed issue and exclude it from the identity.
            issues.append(
                FundingIssue(
                    code=FundingIssueCode.FUNDING_BOUNDARY_IN_FUTURE,
                    record_id=settlement.settlement_id,
                    detail=(
                        f"boundary {settlement.boundary_time_ns} is after the as-of cut "
                        f"{as_of_ns}; settlement is not attributable at this cut"
                    ),
                )
            )
            continue
        boundary_key = (
            settlement.campaign_id,
            settlement.instrument_id,
            settlement.boundary_time_ns,
        )
        previous = seen_boundaries.get(boundary_key)
        if previous is not None:
            issues.append(
                FundingIssue(
                    code=FundingIssueCode.DUPLICATE_BOUNDARY_CLAIM,
                    record_id=settlement.settlement_id,
                    detail=f"boundary already claimed by {previous}",
                )
            )
            continue
        seen_boundaries[boundary_key] = settlement.settlement_id

        events = grouped_events.get((settlement.campaign_id, settlement.instrument_id))
        quantity, known = _quantity_at(events, settlement.boundary_time_ns)
        if not known:
            issues.append(
                FundingIssue(
                    code=FundingIssueCode.MISSING_EXPOSURE,
                    record_id=settlement.settlement_id,
                    detail="no position evidence was available at the funding boundary",
                )
            )
            continue
        mark = _mark_at(marks_by_instrument.get(settlement.instrument_id), settlement.boundary_time_ns)
        if mark is None:
            issues.append(
                FundingIssue(
                    code=FundingIssueCode.NO_CAUSAL_MARK,
                    record_id=settlement.settlement_id,
                    detail="no mark was available at the funding boundary; a future mark is not used",
                )
            )
            continue

        expected = -quantity * mark * settlement.rate
        difference = abs(settlement.amount - expected)
        delayed = settlement.received_at_ns > settlement.boundary_time_ns
        agreed = difference <= EPSILON
        if not agreed:
            issues.append(
                FundingIssue(
                    code=FundingIssueCode.FUNDING_MISMATCH,
                    record_id=settlement.settlement_id,
                    detail=(
                        f"expected {expected} from qty {quantity} x mark {mark} x rate "
                        f"{settlement.rate}, got {settlement.amount}"
                    ),
                )
            )
        if (
            settlement.accounting_time_ns is not None
            and settlement.received_at_ns > settlement.accounting_time_ns
        ):
            issues.append(
                FundingIssue(
                    code=FundingIssueCode.NOT_FINAL_AT_ACCOUNTING_TIME,
                    record_id=settlement.settlement_id,
                    detail=(
                        f"received at {settlement.received_at_ns} after accounting cut "
                        f"{settlement.accounting_time_ns}; funding is not final there"
                    ),
                )
            )
        funding_total += settlement.amount
        attributions.append(
            FundingAttribution(
                settlement_id=settlement.settlement_id,
                campaign_id=settlement.campaign_id,
                instrument_id=settlement.instrument_id,
                quantity_at_boundary=quantity,
                mark_price=mark,
                expected_amount=expected,
                actual_amount=settlement.amount,
                difference=difference,
                delayed=delayed,
                agreed=agreed,
            )
        )

    market_value_change = ZERO
    instruments = set(marks_by_instrument) | {key[1] for key in grouped_events}
    for instrument_id in sorted(instruments):
        instrument_events = tuple(
            event
            for key, items in grouped_events.items()
            if key[1] == instrument_id
            for event in items
        )
        quantity_initial, known_initial = _quantity_at(instrument_events, opening_time_ns)
        quantity_final, known_final = _quantity_at(instrument_events, as_of_ns)
        if not position_history_complete and not (known_initial and known_final):
            issues.append(
                FundingIssue(
                    code=FundingIssueCode.MISSING_EXPOSURE,
                    record_id=instrument_id,
                    detail="position history is incomplete; market value is unknown",
                )
            )
            continue
        initial_mark: Decimal | None = (
            _mark_at(marks_by_instrument.get(instrument_id), opening_time_ns)
            if quantity_initial != ZERO
            else ZERO
        )
        final_mark: Decimal | None = (
            _mark_at(marks_by_instrument.get(instrument_id), as_of_ns)
            if quantity_final != ZERO
            else ZERO
        )
        if initial_mark is None or final_mark is None:
            issues.append(
                FundingIssue(
                    code=FundingIssueCode.MISSING_FINAL_MARK,
                    record_id=instrument_id,
                    detail="open quantity has no causal mark at an accounting cut",
                )
            )
            continue
        market_value_change += quantity_final * final_mark - quantity_initial * initial_mark

    expected_equity = initial_equity + realized_cashflows + funding_total + market_value_change
    equity = EquityReconciliation(
        initial_equity=initial_equity,
        realized_cashflows=realized_cashflows,
        funding_total=funding_total,
        unrealized_change=market_value_change,
        expected_final_equity=expected_equity,
        provided_final_equity=final_equity,
        difference=None if final_equity is None else abs(final_equity - expected_equity),
    )
    if final_equity is not None and equity.difference is not None and equity.difference > EPSILON:
        issues.append(
            FundingIssue(
                code=FundingIssueCode.EQUITY_MISMATCH,
                record_id="equity",
                detail=f"expected {expected_equity}, supplied {final_equity}",
            )
        )

    codes = {issue.code for issue in issues}
    hard_mismatch = {
        FundingIssueCode.FUNDING_MISMATCH,
        FundingIssueCode.EQUITY_MISMATCH,
        FundingIssueCode.DUPLICATE_SETTLEMENT,
        FundingIssueCode.DUPLICATE_BOUNDARY_CLAIM,
    }
    if codes & hard_mismatch:
        status = FundingReportStatus.MISMATCH
    elif codes:
        status = FundingReportStatus.BLOCKED
    else:
        status = FundingReportStatus.PASS
    return FundingBoundaryReport(
        status=status,
        as_of_ns=as_of_ns,
        attributions=tuple(attributions),
        equity=equity,
        issues=tuple(issues),
    )


def issues_by_code(report: FundingBoundaryReport) -> Mapping[FundingIssueCode, tuple[str, ...]]:
    grouped: dict[FundingIssueCode, list[str]] = {}
    for issue in report.issues:
        grouped.setdefault(issue.code, []).append(issue.record_id)
    return {code: tuple(ids) for code, ids in grouped.items()}
