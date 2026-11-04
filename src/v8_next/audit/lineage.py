"""Point-in-time and zero-leakage lineage auditor — port of v8-core/src/audit/lineage.rs.

No input may be available later than the decision clock it was used at.

Fail-closed cashflow lineage (M0-03, issue #4)
----------------------------------------------

The second half of this module certifies the join chain

    opportunity -> decision -> order -> fill -> cashflow

over explicitly supplied, typed records. Every record carries its own stable native
identifiers, campaign id, event time and availability time; nothing is inferred from a
neighbour and no campaign is ever substituted from a last-seen value. :func:`certify`
returns an explicit :class:`CompletenessVerdict` and a tuple of typed violations:

* :class:`MissingJoin` — a record does not carry the key of its declared parent, a
  layer is absent while a dependent layer exists, or a fill has no cashflow evidence;
* :class:`OrphanRecord` — a join key is present but the parent record does not exist;
* :class:`DuplicateFill` / :class:`DuplicateRecord` — an id appears twice;
* :class:`LateRecord` — event or availability time lies after the certification cut;
* :class:`CampaignMismatch` — a child's campaign disagrees with its parent's;
* :class:`MislinkedRecord` — transitively inconsistent native ids or instruments;
* :class:`EventTimeInversion` — a child precedes its parent in event time.

A certification can only be ``COMPLETE`` when every join resolves and all four layers
hold evidence. Any missing join leaves it ``INCOMPLETE``; uncertifiable evidence (late
arrival or an empty layer) leaves it ``BLOCKED``. :func:`require_complete` turns a
non-complete verdict into the first typed failure, so a consumer cannot accidentally
treat "not yet proved" as "proved".
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum

from v8_next.telemetry.identity import canonical_digest

__all__ = [
    "CampaignMismatch",
    "CompletenessVerdict",
    "DuplicateFill",
    "DuplicateRecord",
    "EventTimeInversion",
    "LateRecord",
    "LineageAuditReport",
    "LineageAuditor",
    "LineageCashflow",
    "LineageCertification",
    "LineageChain",
    "LineageDecision",
    "LineageFill",
    "LineageOrder",
    "LineageViolation",
    "LineageViolationCode",
    "MislinkedRecord",
    "MissingJoin",
    "OrphanRecord",
    "PointInTimeLeakage",
    "certify",
    "require_complete",
]

LINEAGE_TAG = "cashflow-lineage-chain"


@dataclass(frozen=True)
class LineageAuditReport:
    passed: bool
    records_checked: int
    pit_leak_violations: list[str] = field(default_factory=list)


class PointInTimeLeakage(ValueError):
    """Raised when an input's availability exceeds the decision clock (Rule 3)."""


class LineageAuditor:
    """Port of ``v8_core::audit::LineageAuditor``."""

    @staticmethod
    def audit_pit_causality(
        decision_clock: int,
        input_available_times: list[tuple[str, int]],
    ) -> LineageAuditReport:
        violations: list[str] = []
        for name, available in input_available_times:
            if available > decision_clock:
                violations.append(
                    "PIT_FUTURE_LEAKAGE: "
                    f"Input '{name}' available at {available} > decision clock {decision_clock}"
                )
        if violations:
            raise PointInTimeLeakage("; ".join(violations))
        return LineageAuditReport(
            passed=True, records_checked=len(input_available_times), pit_leak_violations=[]
        )


# --------------------------------------------------------------------------- #
# fail-closed cashflow lineage (M0-03)
# --------------------------------------------------------------------------- #
class LineageViolationCode(StrEnum):
    """Machine-readable discriminator carried by every :class:`LineageViolation`."""

    MISSING_JOIN = "MISSING_JOIN"
    ORPHAN_RECORD = "ORPHAN_RECORD"
    DUPLICATE_FILL = "DUPLICATE_FILL"
    DUPLICATE_RECORD = "DUPLICATE_RECORD"
    LATE_RECORD = "LATE_RECORD"
    CAMPAIGN_MISMATCH = "CAMPAIGN_MISMATCH"
    MISLINKED_RECORD = "MISLINKED_RECORD"
    EVENT_TIME_INVERSION = "EVENT_TIME_INVERSION"


class CompletenessVerdict(StrEnum):
    """Explicit outcome of a lineage certification. Never inferred from absence."""

    #: Every join resolved, all layers carry evidence, no late or duplicate rows.
    COMPLETE = "COMPLETE"
    #: A join is missing, orphaned, duplicated, mismatched or inverted.
    INCOMPLETE = "INCOMPLETE"
    #: Evidence cannot be certified at all: a late record or a structurally empty layer.
    BLOCKED = "BLOCKED"


class LineageViolation(ValueError):
    """Base class for typed, fail-closed lineage failures."""

    code: LineageViolationCode
    record_id: str
    detail: str

    def __init__(
        self,
        code: LineageViolationCode,
        record_id: str,
        detail: str,
    ) -> None:
        super().__init__(f"{code.value}: {record_id}: {detail}")
        self.code = code
        self.record_id = record_id
        self.detail = detail


class MissingJoin(LineageViolation):
    """A record does not carry — or cannot resolve — the key of its declared parent."""

    def __init__(self, record_id: str, detail: str) -> None:
        super().__init__(LineageViolationCode.MISSING_JOIN, record_id, detail)


class OrphanRecord(LineageViolation):
    """A join key is present but no parent record carries it."""

    def __init__(self, record_id: str, detail: str) -> None:
        super().__init__(LineageViolationCode.ORPHAN_RECORD, record_id, detail)


class DuplicateFill(LineageViolation):
    """The same fill id appears more than once."""

    def __init__(self, record_id: str, detail: str) -> None:
        super().__init__(LineageViolationCode.DUPLICATE_FILL, record_id, detail)


class DuplicateRecord(LineageViolation):
    """A decision, order or cashflow id appears more than once."""

    def __init__(self, record_id: str, detail: str) -> None:
        super().__init__(LineageViolationCode.DUPLICATE_RECORD, record_id, detail)


class LateRecord(LineageViolation):
    """A record's event or availability time lies after the certification cut."""

    def __init__(self, record_id: str, detail: str) -> None:
        super().__init__(LineageViolationCode.LATE_RECORD, record_id, detail)


class CampaignMismatch(LineageViolation):
    """A child record's campaign id disagrees with its parent's."""

    def __init__(self, record_id: str, detail: str) -> None:
        super().__init__(LineageViolationCode.CAMPAIGN_MISMATCH, record_id, detail)


class MislinkedRecord(LineageViolation):
    """Native ids or instruments are transitively inconsistent along the chain."""

    def __init__(self, record_id: str, detail: str) -> None:
        super().__init__(LineageViolationCode.MISLINKED_RECORD, record_id, detail)


class EventTimeInversion(LineageViolation):
    """A child record precedes its parent in event time."""

    def __init__(self, record_id: str, detail: str) -> None:
        super().__init__(LineageViolationCode.EVENT_TIME_INVERSION, record_id, detail)


@dataclass(frozen=True, slots=True)
class LineageDecision:
    """An admitted opportunity decision; the root of a cashflow lineage chain."""

    decision_id: str
    opportunity_id: str
    campaign_id: str
    instrument_id: str
    decision_time_ns: int
    available_at_ns: int


@dataclass(frozen=True, slots=True)
class LineageOrder:
    """A venue order emitted for one decision."""

    order_id: str
    decision_id: str
    campaign_id: str
    instrument_id: str
    event_time_ns: int
    available_at_ns: int


@dataclass(frozen=True, slots=True)
class LineageFill:
    """An execution of one order, carrying both its native ids and its event time."""

    fill_id: str
    order_id: str
    decision_id: str
    campaign_id: str
    instrument_id: str
    event_time_ns: int
    available_at_ns: int


@dataclass(frozen=True, slots=True)
class LineageCashflow:
    """A terminal cash movement produced by one fill."""

    cashflow_id: str
    fill_id: str
    campaign_id: str
    event_time_ns: int
    available_at_ns: int
    amount: Decimal


@dataclass(frozen=True, slots=True)
class LineageChain:
    """The four explicitly supplied layers. Order is irrelevant; ids are the joins."""

    decisions: tuple[LineageDecision, ...] = ()
    orders: tuple[LineageOrder, ...] = ()
    fills: tuple[LineageFill, ...] = ()
    cashflows: tuple[LineageCashflow, ...] = ()

    def __post_init__(self) -> None:
        for cashflow in self.cashflows:
            if not isinstance(cashflow.amount, Decimal):
                raise ValueError("cashflow amount: Decimal required in a lineage chain")
            if not cashflow.amount.is_finite():
                raise ValueError("cashflow amount: finite Decimal required")

    def digest(self) -> str:
        """Provenance digest over the records as supplied (order-independent)."""
        payload = {
            "decisions": [
                [
                    row.decision_id,
                    row.opportunity_id,
                    row.campaign_id,
                    row.instrument_id,
                    row.decision_time_ns,
                    row.available_at_ns,
                ]
                for row in sorted(self.decisions, key=lambda item: item.decision_id)
            ],
            "orders": [
                [
                    row.order_id,
                    row.decision_id,
                    row.campaign_id,
                    row.instrument_id,
                    row.event_time_ns,
                    row.available_at_ns,
                ]
                for row in sorted(self.orders, key=lambda item: item.order_id)
            ],
            "fills": [
                [
                    row.fill_id,
                    row.order_id,
                    row.decision_id,
                    row.campaign_id,
                    row.instrument_id,
                    row.event_time_ns,
                    row.available_at_ns,
                ]
                for row in sorted(self.fills, key=lambda item: item.fill_id)
            ],
            "cashflows": [
                [
                    row.cashflow_id,
                    row.fill_id,
                    row.campaign_id,
                    row.event_time_ns,
                    row.available_at_ns,
                    str(row.amount),
                ]
                for row in sorted(self.cashflows, key=lambda item: item.cashflow_id)
            ],
        }
        return canonical_digest(LINEAGE_TAG, payload)


@dataclass(frozen=True, slots=True)
class LineageCertification:
    """Explicit certification outcome; ``COMPLETE`` only when every join resolved."""

    verdict: CompletenessVerdict
    violations: tuple[LineageViolation, ...]
    joins_checked: int
    campaign_ids: tuple[str, ...]
    provenance_digest: str

    @property
    def complete(self) -> bool:
        """True only for an explicit ``COMPLETE`` verdict."""
        return self.verdict is CompletenessVerdict.COMPLETE

    @property
    def certified(self) -> bool:
        """Alias of :attr:`complete` for report consumers."""
        return self.complete

    def failures_of(self, code: LineageViolationCode) -> tuple[LineageViolation, ...]:
        return tuple(violation for violation in self.violations if violation.code is code)


def _is_blank(value: str) -> bool:
    return not value.strip()


def _unique(
    ids: Sequence[str],
    factory: Callable[[str, str], LineageViolation],
    violations: list[LineageViolation],
    layer: str,
) -> bool:
    """Append a duplicate violation for any repeated id; return True if any."""
    seen: set[str] = set()
    duplicated = False
    for record_id in ids:
        if record_id in seen:
            violations.append(factory(record_id, f"{layer} id appears more than once"))
            duplicated = True
        seen.add(record_id)
    return duplicated


def _check_late(
    record_id: str,
    event_time_ns: int,
    available_at_ns: int,
    as_of_ns: int,
    violations: list[LineageViolation],
) -> None:
    if event_time_ns > as_of_ns or available_at_ns > as_of_ns:
        violations.append(
            LateRecord(
                record_id,
                f"event_time={event_time_ns} available_at={available_at_ns} "
                f"exceeds certification cut {as_of_ns}",
            )
        )


def certify(chain: LineageChain, *, as_of_ns: int) -> LineageCertification:
    """Certify the join chain as of ``as_of_ns``; never returns a stale ``COMPLETE``.

    Every record is joined by its own explicit ids. A missing, orphaned, duplicate,
    late, swapped or inverted record produces a typed violation and the verdict can
    never be ``COMPLETE``.
    """
    if not isinstance(as_of_ns, int) or isinstance(as_of_ns, bool):
        raise ValueError("as_of_ns: int required")

    violations: list[LineageViolation] = []
    joins_checked = 0
    structural_gap = False

    # --- layer evidence ---------------------------------------------------- #
    if not chain.decisions:
        violations.append(MissingJoin("<decisions>", "no decision evidence to certify"))
        structural_gap = True
    if not chain.orders:
        violations.append(MissingJoin("<orders>", "no order evidence to certify"))
        structural_gap = True
    if not chain.fills:
        violations.append(MissingJoin("<fills>", "no fill evidence to certify"))
        structural_gap = True
    if not chain.cashflows:
        violations.append(MissingJoin("<cashflows>", "no cashflow evidence to certify"))
        structural_gap = True

    _unique([row.decision_id for row in chain.decisions], DuplicateRecord, violations, "decision")
    _unique([row.order_id for row in chain.orders], DuplicateRecord, violations, "order")
    _unique([row.fill_id for row in chain.fills], DuplicateFill, violations, "fill")
    _unique([row.cashflow_id for row in chain.cashflows], DuplicateRecord, violations, "cashflow")

    decisions = {row.decision_id: row for row in chain.decisions}
    orders = {row.order_id: row for row in chain.orders}
    fills = {row.fill_id: row for row in chain.fills}

    # --- decisions --------------------------------------------------------- #
    for decision in chain.decisions:
        if _is_blank(decision.opportunity_id):
            violations.append(
                MissingJoin(decision.decision_id, "decision carries no opportunity_id")
            )
        if _is_blank(decision.campaign_id):
            violations.append(MissingJoin(decision.decision_id, "decision carries no campaign_id"))
        _check_late(
            decision.decision_id,
            decision.decision_time_ns,
            decision.available_at_ns,
            as_of_ns,
            violations,
        )

    # --- orders ------------------------------------------------------------ #
    for order in chain.orders:
        if _is_blank(order.campaign_id):
            violations.append(MissingJoin(order.order_id, "order carries no campaign_id"))
        if _is_blank(order.decision_id):
            violations.append(MissingJoin(order.order_id, "order carries no decision_id"))
            parent_decision = None
        else:
            parent_decision = decisions.get(order.decision_id)
        if parent_decision is None:
            if not _is_blank(order.decision_id):
                violations.append(
                    OrphanRecord(
                        order.order_id,
                        f"decision {order.decision_id!r} does not exist",
                    )
                )
        else:
            joins_checked += 1
            if order.campaign_id != parent_decision.campaign_id:
                violations.append(
                    CampaignMismatch(
                        order.order_id,
                        f"order campaign {order.campaign_id!r} != decision campaign "
                        f"{parent_decision.campaign_id!r}",
                    )
                )
            if order.instrument_id != parent_decision.instrument_id:
                violations.append(
                    MislinkedRecord(
                        order.order_id,
                        f"order instrument {order.instrument_id!r} != decision instrument "
                        f"{parent_decision.instrument_id!r}",
                    )
                )
            if order.event_time_ns < parent_decision.decision_time_ns:
                violations.append(
                    EventTimeInversion(
                        order.order_id,
                        f"order event_time {order.event_time_ns} precedes decision "
                        f"{parent_decision.decision_time_ns}",
                    )
                )
        _check_late(
            order.order_id, order.event_time_ns, order.available_at_ns, as_of_ns, violations
        )

    # --- fills ------------------------------------------------------------- #
    for fill in chain.fills:
        if _is_blank(fill.campaign_id):
            violations.append(MissingJoin(fill.fill_id, "fill carries no campaign_id"))
        if _is_blank(fill.decision_id):
            violations.append(MissingJoin(fill.fill_id, "fill carries no decision_id"))
        if _is_blank(fill.order_id):
            violations.append(MissingJoin(fill.fill_id, "fill carries no order_id"))
            parent_order = None
        else:
            parent_order = orders.get(fill.order_id)
        if parent_order is None:
            if not _is_blank(fill.order_id):
                violations.append(
                    OrphanRecord(fill.fill_id, f"order {fill.order_id!r} does not exist")
                )
        else:
            joins_checked += 1
            if fill.campaign_id != parent_order.campaign_id:
                violations.append(
                    CampaignMismatch(
                        fill.fill_id,
                        f"fill campaign {fill.campaign_id!r} != order campaign "
                        f"{parent_order.campaign_id!r}",
                    )
                )
            if fill.instrument_id != parent_order.instrument_id:
                violations.append(
                    MislinkedRecord(
                        fill.fill_id,
                        f"fill instrument {fill.instrument_id!r} != order instrument "
                        f"{parent_order.instrument_id!r}",
                    )
                )
            if not _is_blank(fill.decision_id) and fill.decision_id != parent_order.decision_id:
                violations.append(
                    MislinkedRecord(
                        fill.fill_id,
                        f"fill decision {fill.decision_id!r} != order decision "
                        f"{parent_order.decision_id!r}",
                    )
                )
            if fill.event_time_ns < parent_order.event_time_ns:
                violations.append(
                    EventTimeInversion(
                        fill.fill_id,
                        f"fill event_time {fill.event_time_ns} precedes order "
                        f"{parent_order.event_time_ns}",
                    )
                )
        _check_late(fill.fill_id, fill.event_time_ns, fill.available_at_ns, as_of_ns, violations)

    # --- cashflows --------------------------------------------------------- #
    for cashflow in chain.cashflows:
        if _is_blank(cashflow.campaign_id):
            violations.append(MissingJoin(cashflow.cashflow_id, "cashflow carries no campaign_id"))
        if _is_blank(cashflow.fill_id):
            violations.append(MissingJoin(cashflow.cashflow_id, "cashflow carries no fill_id"))
            parent_fill = None
        else:
            parent_fill = fills.get(cashflow.fill_id)
        if parent_fill is None:
            if not _is_blank(cashflow.fill_id):
                violations.append(
                    OrphanRecord(
                        cashflow.cashflow_id,
                        f"fill {cashflow.fill_id!r} does not exist",
                    )
                )
        else:
            joins_checked += 1
            if cashflow.campaign_id != parent_fill.campaign_id:
                violations.append(
                    CampaignMismatch(
                        cashflow.cashflow_id,
                        f"cashflow campaign {cashflow.campaign_id!r} != fill campaign "
                        f"{parent_fill.campaign_id!r}",
                    )
                )
            if cashflow.event_time_ns < parent_fill.event_time_ns:
                violations.append(
                    EventTimeInversion(
                        cashflow.cashflow_id,
                        f"cashflow event_time {cashflow.event_time_ns} precedes fill "
                        f"{parent_fill.event_time_ns}",
                    )
                )
        _check_late(
            cashflow.cashflow_id,
            cashflow.event_time_ns,
            cashflow.available_at_ns,
            as_of_ns,
            violations,
        )

    # --- terminal coverage: every fill must reach a cashflow --------------- #
    if chain.cashflows:
        covered = {cashflow.fill_id for cashflow in chain.cashflows}
        for fill in chain.fills:
            if fill.fill_id not in covered:
                violations.append(MissingJoin(fill.fill_id, "fill has no cashflow evidence"))

    if structural_gap or any(isinstance(row, LateRecord) for row in violations):
        verdict = CompletenessVerdict.BLOCKED
    elif violations:
        verdict = CompletenessVerdict.INCOMPLETE
    else:
        verdict = CompletenessVerdict.COMPLETE

    return LineageCertification(
        verdict=verdict,
        violations=tuple(violations),
        joins_checked=joins_checked,
        campaign_ids=tuple(sorted({row.campaign_id for row in chain.decisions})),
        provenance_digest=chain.digest(),
    )


def require_complete(certification: LineageCertification) -> None:
    """Raise the first typed failure unless the certification is explicitly ``COMPLETE``."""
    if certification.verdict is CompletenessVerdict.COMPLETE:
        return
    if certification.violations:
        raise certification.violations[0]
    raise LineageViolation(
        LineageViolationCode.MISSING_JOIN,
        "<certification>",
        f"verdict {certification.verdict.value} without a recorded violation",
    )
