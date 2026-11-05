"""Restart-safe intent journal — a record of what was asked, never an execution authority.

The native Nautilus OMS remains the single execution authority. This module stores the
*intent* to trade together with the evidence the native callback stream delivered, so a
process that dies between the journal write and the venue acknowledgment can recover
without submitting a second order. It does not build orders, holds no credentials and has
no order-submission method.

Identity
--------
``intent_id`` is a domain-separated digest of ``decision_id + instrument + side + quantity
+ price + policy version``; ``client_order_id`` is derived from it, never generated from a
clock or a random source. The same economic intent therefore yields the same client order
id in every process, before and after a restart, so replaying a submission cannot create a
second order.

States
------
``PENDING`` is written before anything reaches the venue. ``SUBMITTING`` is written (and
committed) before the native submit call. A restart therefore observes ``SUBMITTING``
when it cannot know whether the venue received the order; :meth:`Outbox.recover` turns
that (and ``SUBMITTED``/``CANCEL_PENDING``) into ``UNKNOWN``. ``UNKNOWN`` is fail-closed:
a re-submit is refused and the record never becomes terminal without injected venue
evidence. Missing venue evidence leaves it ``UNKNOWN`` forever — absence is not proof of
cancellation or rejection.

Evidence
--------
Duplicate ack/fill callbacks are no-ops. Fill evidence is cumulative and monotonic; an
observation that would exceed the ordered quantity raises :class:`OverfillError` and is
never clamped. An unlisted venue status string is a schema violation, not an implicit
unknown, so the journal fails closed on it.

Everything mutating runs inside ``BEGIN IMMEDIATE`` and a conditional update keyed on the
previous state; two handles over the same database cannot both own one transition.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Final, Self, cast

from v8_next.domain.decision import VenueOrderStatus
from v8_next.telemetry.identity import canonical_digest

__all__ = [
    "DuplicateSubmit",
    "IntentIdentity",
    "IntentRecord",
    "IntentState",
    "InvalidTransition",
    "Outbox",
    "OutboxError",
    "OverfillError",
    "UnknownIntent",
    "VenueEvidenceError",
    "VenueFillObservation",
    "VenueOrderObservation",
    "VenueView",
    "derive_intent_identity",
]

#: Domain tag for the intent identity digest; bump only with an identity migration.
INTENT_ID_TAG: Final[str] = "v8-next.durable-intent.v1"
#: Prefix for native client order ids; 4 + 32 hex chars = 36 chars (128-bit identity).
CLIENT_ORDER_ID_PREFIX: Final[str] = "v9i-"

_SIDE_BUY: Final[str] = "BUY"
_SIDE_SELL: Final[str] = "SELL"


class IntentState(StrEnum):
    """Journal state of one intent; ``UNKNOWN`` is never terminal."""

    PENDING = "PENDING"
    SUBMITTING = "SUBMITTING"
    SUBMITTED = "SUBMITTED"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCEL_PENDING = "CANCEL_PENDING"
    CANCELED = "CANCELED"
    REJECTED = "REJECTED"
    UNKNOWN = "UNKNOWN"


#: Venue-side terminal states of an intent; ``UNKNOWN`` is deliberately absent.
TERMINAL_STATES: Final[frozenset[IntentState]] = frozenset(
    {IntentState.FILLED, IntentState.CANCELED, IntentState.REJECTED}
)
#: States whose true venue outcome cannot be known after a restart.
_RECONCILE_ON_RECOVERY: Final[frozenset[IntentState]] = frozenset(
    {IntentState.SUBMITTING, IntentState.SUBMITTED, IntentState.CANCEL_PENDING}
)


class OutboxError(Exception):
    """Base failure for the intent journal; ``code`` is the stable discriminator."""

    code: str = "OUTBOX_ERROR"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        resolved = code or type(self).code
        super().__init__(f"{resolved}: {message}")
        self.code = resolved


class InvalidTransition(OutboxError):
    """The requested transition is not legal from the recorded state."""

    code = "INVALID_TRANSITION"


class DuplicateSubmit(OutboxError):
    """A second submission was attempted for an intent that may already be at the venue."""

    code = "DUPLICATE_SUBMIT_REFUSED"


class OverfillError(OutboxError):
    """Cumulative fill evidence exceeds the ordered quantity; never clamped."""

    code = "OVERFILL"


class VenueEvidenceError(OutboxError):
    """Injected venue evidence is unknown, ambiguous or internally inconsistent."""

    code = "VENUE_EVIDENCE"


class UnknownIntent(OutboxError):
    """No intent with the requested id is recorded."""

    code = "UNKNOWN_INTENT"


def _require_text(name: str, value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name}: non-empty string required")
    return value


def _require_ns(name: str, value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"{name}: non-negative int nanoseconds required")
    return value


def _require_decimal(name: str, value: object, *, allow_zero: bool) -> Decimal:
    if not isinstance(value, Decimal):
        raise TypeError(f"{name}: Decimal required (binary floats cannot represent money)")
    if not value.is_finite():
        raise ValueError(f"{name}: finite Decimal required")
    if value < 0 or (value == 0 and not allow_zero):
        qualifier = "non-negative" if allow_zero else "positive"
        raise ValueError(f"{name}: {qualifier} Decimal required")
    return value


def _decimal_key(value: Decimal) -> str:
    """Canonical decimal text: ``1.0`` and ``1.00`` share one durable key."""
    return format(value.normalize(), "f")


def _coerce_status(value: object) -> VenueOrderStatus:
    if isinstance(value, VenueOrderStatus):
        return value
    if isinstance(value, str):
        try:
            return VenueOrderStatus(value)
        except ValueError as exc:
            raise VenueEvidenceError(
                f"unlisted venue status {value!r}; refusing to infer UNKNOWN"
            ) from exc
    raise VenueEvidenceError(f"venue status must be VenueOrderStatus, got {type(value).__name__}")


@dataclass(frozen=True)
class IntentIdentity:
    """Deterministic identity of one economic intent."""

    intent_id: str
    client_order_id: str


@dataclass(frozen=True)
class _Request:
    decision_id: str
    instrument_id: str
    side: str
    quantity: Decimal
    price: Decimal | None
    policy_version: str


def _request(
    *,
    decision_id: str,
    instrument_id: str,
    side: str,
    quantity: Decimal,
    price: Decimal | None,
    policy_version: str,
) -> _Request:
    normalized_side = _require_text("side", side).upper()
    if normalized_side not in {_SIDE_BUY, _SIDE_SELL}:
        raise ValueError(f"side: {_SIDE_BUY} or {_SIDE_SELL} required")
    normalized_price = None
    if price is not None:
        normalized_price = _require_decimal("price", price, allow_zero=False)
    return _Request(
        decision_id=_require_text("decision_id", decision_id),
        instrument_id=_require_text("instrument_id", instrument_id),
        side=normalized_side,
        quantity=_require_decimal("quantity", quantity, allow_zero=False),
        price=normalized_price,
        policy_version=_require_text("policy_version", policy_version),
    )


def _derive(request: _Request) -> IntentIdentity:
    payload = {
        "decision_id": request.decision_id,
        "instrument_id": request.instrument_id,
        "policy_version": request.policy_version,
        "price": None if request.price is None else _decimal_key(request.price),
        "quantity": _decimal_key(request.quantity),
        "side": request.side,
    }
    digest = canonical_digest(INTENT_ID_TAG, payload)
    return IntentIdentity(
        intent_id=digest,
        client_order_id=f"{CLIENT_ORDER_ID_PREFIX}{digest[:32]}",
    )


def derive_intent_identity(
    *,
    decision_id: str,
    instrument_id: str,
    side: str,
    quantity: Decimal,
    price: Decimal | None,
    policy_version: str,
) -> IntentIdentity:
    """Derive the replay-stable ``intent_id`` and native ``client_order_id``.

    The identity is content-addressed only: no clock, process id or random source is
    involved, so a restart derives the same ids for the same economic intent.
    """
    return _derive(
        _request(
            decision_id=decision_id,
            instrument_id=instrument_id,
            side=side,
            quantity=quantity,
            price=price,
            policy_version=policy_version,
        )
    )


@dataclass(frozen=True)
class IntentRecord:
    """One durable intent plus the last venue evidence observed for it.

    Frozen by construction; every transition is a new record persisted under a
    conditional update, so a stale copy can never be used to overwrite newer evidence.
    """

    intent_id: str
    client_order_id: str
    decision_id: str
    instrument_id: str
    side: str
    quantity: Decimal
    price: Decimal | None
    policy_version: str
    state: IntentState
    attempt: int
    created_ns: int
    updated_ns: int
    submitted_ns: int | None = None
    acknowledged_ns: int | None = None
    terminal_ns: int | None = None
    venue_status: VenueOrderStatus | None = None
    filled_qty: Decimal = Decimal("0")
    reconcile_note: str | None = None

    def __post_init__(self) -> None:
        _require_text("intent_id", self.intent_id)
        _require_text("client_order_id", self.client_order_id)
        _require_text("decision_id", self.decision_id)
        _require_text("instrument_id", self.instrument_id)
        _require_text("policy_version", self.policy_version)
        if self.side not in {_SIDE_BUY, _SIDE_SELL}:
            raise ValueError("side must be BUY or SELL")
        quantity = _require_decimal("quantity", self.quantity, allow_zero=False)
        if self.price is not None:
            _require_decimal("price", self.price, allow_zero=False)
        _require_ns("created_ns", self.created_ns)
        _require_ns("updated_ns", self.updated_ns)
        for name, value in (
            ("submitted_ns", self.submitted_ns),
            ("acknowledged_ns", self.acknowledged_ns),
            ("terminal_ns", self.terminal_ns),
        ):
            if value is not None:
                _require_ns(name, value)
        if not isinstance(self.state, IntentState):
            raise ValueError("state must be an IntentState")
        if not isinstance(self.attempt, int) or isinstance(self.attempt, bool) or self.attempt < 0:
            raise ValueError("attempt must be a non-negative int")
        filled = _require_decimal("filled_qty", self.filled_qty, allow_zero=True)
        if filled > quantity:
            raise ValueError("filled_qty cannot exceed quantity")
        if self.reconcile_note is not None and not self.reconcile_note.strip():
            raise ValueError("reconcile_note cannot be empty when present")

        if self.state in TERMINAL_STATES:
            if self.terminal_ns is None:
                raise ValueError("terminal state requires terminal_ns")
        elif self.terminal_ns is not None:
            raise ValueError("non-terminal state cannot carry terminal_ns")

        zero_filled_states = {
            IntentState.PENDING,
            IntentState.SUBMITTING,
            IntentState.SUBMITTED,
            IntentState.ACKNOWLEDGED,
        }
        if self.state in zero_filled_states and filled != 0:
            raise ValueError(f"{self.state.value} cannot carry a fill")
        if self.state is IntentState.PARTIALLY_FILLED and not (0 < filled < quantity):
            raise ValueError("PARTIALLY_FILLED requires 0 < filled_qty < quantity")
        if self.state is IntentState.FILLED and filled != quantity:
            raise ValueError("FILLED requires filled_qty == quantity")
        if self.state is IntentState.REJECTED and filled != 0:
            raise ValueError("REJECTED cannot carry a fill")
        if self.state is IntentState.CANCEL_PENDING and not (0 <= filled < quantity):
            raise ValueError("CANCEL_PENDING requires an unfilled remainder")

    @property
    def is_terminal(self) -> bool:
        """True only for FILLED/CANCELED/REJECTED; ``UNKNOWN`` is never terminal."""
        return self.state in TERMINAL_STATES

    @property
    def requires_reconciliation(self) -> bool:
        """True while the venue outcome is unproven."""
        return self.state is IntentState.UNKNOWN or self.state in _RECONCILE_ON_RECOVERY


@dataclass(frozen=True)
class VenueOrderObservation:
    """What the venue reports for one client order id (open or terminal)."""

    client_order_id: str
    status: VenueOrderStatus
    cumulative_qty: Decimal = Decimal("0")

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "client_order_id", _require_text("client_order_id", self.client_order_id)
        )
        object.__setattr__(self, "status", _coerce_status(self.status))
        object.__setattr__(
            self,
            "cumulative_qty",
            _require_decimal("cumulative_qty", self.cumulative_qty, allow_zero=True),
        )


@dataclass(frozen=True)
class VenueFillObservation:
    """One cumulative fill reading for a client order id."""

    client_order_id: str
    cumulative_qty: Decimal

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "client_order_id", _require_text("client_order_id", self.client_order_id)
        )
        object.__setattr__(
            self,
            "cumulative_qty",
            _require_decimal("cumulative_qty", self.cumulative_qty, allow_zero=True),
        )


@dataclass(frozen=True)
class VenueView:
    """Injected venue truth used by :meth:`Outbox.reconcile`.

    ``orders`` is every order the venue can report for the client id (open or terminal);
    ``fills`` is cumulative fill evidence. A client id absent from both is *missing*
    evidence, which never resolves an ``UNKNOWN`` record.
    """

    orders: tuple[VenueOrderObservation, ...] = ()
    fills: tuple[VenueFillObservation, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "orders", tuple(self.orders))
        object.__setattr__(self, "fills", tuple(self.fills))
        for observation in self.orders:
            if not isinstance(observation, VenueOrderObservation):
                raise TypeError("orders must contain VenueOrderObservation")
        for fill in self.fills:
            if not isinstance(fill, VenueFillObservation):
                raise TypeError("fills must contain VenueFillObservation")


_SCHEMA: Final[str] = """
CREATE TABLE IF NOT EXISTS intents (
    intent_id TEXT PRIMARY KEY,
    client_order_id TEXT NOT NULL UNIQUE,
    decision_id TEXT NOT NULL,
    instrument_id TEXT NOT NULL,
    side TEXT NOT NULL,
    quantity TEXT NOT NULL,
    price TEXT,
    policy_version TEXT NOT NULL,
    state TEXT NOT NULL,
    attempt INTEGER NOT NULL,
    created_ns INTEGER NOT NULL,
    updated_ns INTEGER NOT NULL,
    submitted_ns INTEGER,
    acknowledged_ns INTEGER,
    terminal_ns INTEGER,
    venue_status TEXT,
    filled_qty TEXT NOT NULL,
    reconcile_note TEXT
);
CREATE TABLE IF NOT EXISTS intent_fills (
    client_order_id TEXT NOT NULL,
    cumulative_qty TEXT NOT NULL,
    observed_ns INTEGER NOT NULL,
    PRIMARY KEY (client_order_id, cumulative_qty)
);
CREATE INDEX IF NOT EXISTS idx_intents_state ON intents(state);
"""

_COLUMNS: Final[str] = (
    "intent_id,client_order_id,decision_id,instrument_id,side,quantity,price,"
    "policy_version,state,attempt,created_ns,updated_ns,submitted_ns,acknowledged_ns,"
    "terminal_ns,venue_status,filled_qty,reconcile_note"
)


def _row_to_record(row: sqlite3.Row) -> IntentRecord:
    try:
        return IntentRecord(
            intent_id=cast(str, row["intent_id"]),
            client_order_id=cast(str, row["client_order_id"]),
            decision_id=cast(str, row["decision_id"]),
            instrument_id=cast(str, row["instrument_id"]),
            side=cast(str, row["side"]),
            quantity=Decimal(cast(str, row["quantity"])),
            price=None if row["price"] is None else Decimal(cast(str, row["price"])),
            policy_version=cast(str, row["policy_version"]),
            state=IntentState(cast(str, row["state"])),
            attempt=cast(int, row["attempt"]),
            created_ns=cast(int, row["created_ns"]),
            updated_ns=cast(int, row["updated_ns"]),
            submitted_ns=cast(int | None, row["submitted_ns"]),
            acknowledged_ns=cast(int | None, row["acknowledged_ns"]),
            terminal_ns=cast(int | None, row["terminal_ns"]),
            venue_status=(
                None
                if row["venue_status"] is None
                else VenueOrderStatus(cast(str, row["venue_status"]))
            ),
            filled_qty=Decimal(cast(str, row["filled_qty"])),
            reconcile_note=cast(str | None, row["reconcile_note"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise OutboxError(
            "corrupt intent row; refusing to guess its meaning", code="CORRUPT_JOURNAL"
        ) from exc


def _next_time(current: IntentRecord, now_ns: int) -> int:
    """Journal time never moves backwards even if the caller's clock does."""
    return max(_require_ns("now_ns", now_ns), current.updated_ns)


class Outbox:
    """SQLite-backed intent outbox. A journal only — it never submits an order."""

    def __init__(self, path: Path | str, *, busy_timeout_ms: int = 5_000) -> None:
        self.path = Path(path)
        self._db = sqlite3.connect(str(self.path), isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.execute(f"PRAGMA busy_timeout={int(busy_timeout_ms)}")
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=FULL")
        self._db.execute("PRAGMA foreign_keys=ON")
        self._db.executescript(_SCHEMA)

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def close(self) -> None:
        self._db.close()

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        self._db.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self._db.execute("ROLLBACK")
            raise
        else:
            self._db.execute("COMMIT")

    def _row(self, intent_id: str) -> sqlite3.Row | None:
        row = self._db.execute(
            f"SELECT {_COLUMNS} FROM intents WHERE intent_id=?", (intent_id,)
        ).fetchone()
        return cast(sqlite3.Row | None, row)

    def _fetch(self, intent_id: str) -> IntentRecord:
        row = self._row(intent_id)
        if row is None:
            raise UnknownIntent(f"intent {intent_id!r} is not recorded")
        return _row_to_record(row)

    def get(self, intent_id: str) -> IntentRecord | None:
        """Read one intent; ``None`` when absent (never a guessed record)."""
        row = self._row(intent_id)
        return None if row is None else _row_to_record(row)

    def records(self) -> tuple[IntentRecord, ...]:
        """All intents in deterministic replay order."""
        rows = self._db.execute(
            f"SELECT {_COLUMNS} FROM intents ORDER BY created_ns, intent_id"
        ).fetchall()
        return tuple(_row_to_record(cast(sqlite3.Row, row)) for row in rows)

    def cumulative_fills(self, intent_id: str) -> tuple[Decimal, ...]:
        """Distinct cumulative fill readings recorded for an intent, ascending."""
        record = self._fetch(intent_id)
        rows = self._db.execute(
            "SELECT cumulative_qty FROM intent_fills WHERE client_order_id=?",
            (record.client_order_id,),
        ).fetchall()
        values = [Decimal(cast(str, row[0])) for row in rows]
        return tuple(sorted(values))

    def _persist(self, previous: IntentRecord, updated: IntentRecord) -> None:
        cursor = self._db.execute(
            "UPDATE intents SET state=?, attempt=?, updated_ns=?, submitted_ns=?, "
            "acknowledged_ns=?, terminal_ns=?, venue_status=?, filled_qty=?, reconcile_note=? "
            "WHERE intent_id=? AND state=? AND attempt=?",
            (
                updated.state.value,
                updated.attempt,
                updated.updated_ns,
                updated.submitted_ns,
                updated.acknowledged_ns,
                updated.terminal_ns,
                None if updated.venue_status is None else updated.venue_status.value,
                _decimal_key(updated.filled_qty),
                updated.reconcile_note,
                updated.intent_id,
                previous.state.value,
                previous.attempt,
            ),
        )
        if cursor.rowcount != 1:
            raise OutboxError(
                "intent changed underneath this handle; transition ownership refused",
                code="CONCURRENT_TRANSITION",
            )

    def record_intent(
        self,
        *,
        decision_id: str,
        instrument_id: str,
        side: str,
        quantity: Decimal,
        price: Decimal | None,
        policy_version: str,
        now_ns: int,
    ) -> IntentRecord:
        """Record one intent once; an identical retry returns the existing row.

        The client order id is unique in the schema, so two economic intents cannot
        share one native identity.
        """
        request = _request(
            decision_id=decision_id,
            instrument_id=instrument_id,
            side=side,
            quantity=quantity,
            price=price,
            policy_version=policy_version,
        )
        identity = _derive(request)
        timestamp = _require_ns("now_ns", now_ns)
        with self._transaction():
            existing = self.get(identity.intent_id)
            if existing is not None:
                if (
                    existing.client_order_id != identity.client_order_id
                    or existing.decision_id != request.decision_id
                    or existing.instrument_id != request.instrument_id
                    or existing.side != request.side
                    or existing.quantity != request.quantity
                    or existing.price != request.price
                    or existing.policy_version != request.policy_version
                ):
                    raise OutboxError(
                        "intent id resolves to different economic content",
                        code="IDENTITY_COLLISION",
                    )
                return existing
            record = IntentRecord(
                intent_id=identity.intent_id,
                client_order_id=identity.client_order_id,
                decision_id=request.decision_id,
                instrument_id=request.instrument_id,
                side=request.side,
                quantity=request.quantity,
                price=request.price,
                policy_version=request.policy_version,
                state=IntentState.PENDING,
                attempt=0,
                created_ns=timestamp,
                updated_ns=timestamp,
            )
            try:
                self._db.execute(
                    "INSERT INTO intents ("
                    + _COLUMNS
                    + ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        record.intent_id,
                        record.client_order_id,
                        record.decision_id,
                        record.instrument_id,
                        record.side,
                        _decimal_key(record.quantity),
                        None if record.price is None else _decimal_key(record.price),
                        record.policy_version,
                        record.state.value,
                        record.attempt,
                        record.created_ns,
                        record.updated_ns,
                        record.submitted_ns,
                        record.acknowledged_ns,
                        record.terminal_ns,
                        None,
                        _decimal_key(record.filled_qty),
                        None,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise OutboxError(
                    "client_order_id already belongs to another intent",
                    code="IDENTITY_COLLISION",
                ) from exc
        return record

    def begin_submit(self, intent_id: str, *, now_ns: int) -> IntentRecord:
        """Durably mark the submission attempt, then (by the caller) call the native OMS.

        Only a ``PENDING`` intent may begin submitting. Any other state — including
        ``UNKNOWN`` after a crash — refuses with :class:`DuplicateSubmit`: the journal
        cannot prove that a second native submit would not double the exposure.
        """
        with self._transaction():
            current = self._fetch(intent_id)
            if current.state is not IntentState.PENDING:
                raise DuplicateSubmit(
                    f"intent {intent_id!r} is {current.state.value}; re-submission refused"
                )
            timestamp = _next_time(current, now_ns)
            updated = replace(
                current,
                state=IntentState.SUBMITTING,
                attempt=current.attempt + 1,
                updated_ns=timestamp,
                submitted_ns=timestamp,
                reconcile_note=None,
            )
            self._persist(current, updated)
        return updated

    def observe_ack(
        self,
        intent_id: str,
        venue_status: VenueOrderStatus | str,
        *,
        now_ns: int,
    ) -> IntentRecord:
        """Record a venue acknowledgment observation; duplicates are no-ops.

        ``UNKNOWN`` moves a live intent to ``UNKNOWN`` and never terminalizes it.
        Terminal statuses are only accepted when the cumulative fill evidence already
        supports them; otherwise the contradiction raises :class:`VenueEvidenceError`.
        """
        status = _coerce_status(venue_status)
        with self._transaction():
            current = self._fetch(intent_id)
            updated = self._ack(current, status, now_ns)
            if updated is not current:
                self._persist(current, updated)
        return updated

    def _ack(
        self,
        current: IntentRecord,
        status: VenueOrderStatus,
        now_ns: int,
    ) -> IntentRecord:
        if status is VenueOrderStatus.UNKNOWN:
            if current.is_terminal:
                return current
            if current.state is IntentState.PENDING:
                raise InvalidTransition(
                    "an unsubmitted intent cannot receive a venue acknowledgment"
                )
            if current.state is IntentState.UNKNOWN and current.venue_status is status:
                return current
            return replace(
                current,
                state=IntentState.UNKNOWN,
                venue_status=status,
                updated_ns=_next_time(current, now_ns),
                reconcile_note="VENUE_STATUS_UNKNOWN",
            )

        if status is VenueOrderStatus.CANCELED:
            return self._cancel(current, now_ns)

        if current.is_terminal:
            return current

        if status is VenueOrderStatus.REJECTED:
            if current.filled_qty != 0:
                raise VenueEvidenceError(
                    "REJECTED contradicts recorded fill evidence; exposure cannot be discarded"
                )
            return replace(
                current,
                state=IntentState.REJECTED,
                venue_status=status,
                updated_ns=_next_time(current, now_ns),
                terminal_ns=_next_time(current, now_ns),
                reconcile_note=None,
            )

        if status is VenueOrderStatus.FILLED:
            if current.filled_qty != current.quantity:
                raise VenueEvidenceError(
                    "FILLED without cumulative fill evidence; use observe_fill"
                )
            return replace(
                current,
                state=IntentState.FILLED,
                venue_status=status,
                updated_ns=_next_time(current, now_ns),
            )

        if status is VenueOrderStatus.PARTIALLY_FILLED:
            if not (0 < current.filled_qty < current.quantity):
                raise VenueEvidenceError(
                    "PARTIALLY_FILLED without cumulative fill evidence; use observe_fill"
                )
            return replace(
                current,
                state=IntentState.PARTIALLY_FILLED,
                venue_status=status,
                updated_ns=_next_time(current, now_ns),
                reconcile_note=None,
            )

        if current.is_terminal:
            return current
        if current.state is IntentState.PENDING:
            raise InvalidTransition(
                "an unsubmitted intent cannot receive a venue acknowledgment"
            )

        if status is VenueOrderStatus.SUBMITTED:
            if current.state in {
                IntentState.SUBMITTED,
                IntentState.ACKNOWLEDGED,
                IntentState.PARTIALLY_FILLED,
                IntentState.CANCEL_PENDING,
            }:
                return current
            return replace(
                current,
                state=IntentState.SUBMITTED,
                venue_status=status,
                updated_ns=_next_time(current, now_ns),
                reconcile_note=None,
            )

        # ACCEPTED is the only remaining member of the closed enum.
        if current.state is IntentState.ACKNOWLEDGED:
            return current
        if current.state is IntentState.PARTIALLY_FILLED:
            return current
        if current.state is IntentState.CANCEL_PENDING:
            return current
        timestamp = _next_time(current, now_ns)
        return replace(
            current,
            state=IntentState.ACKNOWLEDGED,
            venue_status=status,
            updated_ns=timestamp,
            acknowledged_ns=current.acknowledged_ns if current.acknowledged_ns is not None else timestamp,
            reconcile_note=None,
        )

    def observe_fill(
        self,
        intent_id: str,
        cumulative_qty: Decimal,
        *,
        now_ns: int,
    ) -> IntentRecord:
        """Record cumulative fill evidence, monotonic and capped by the ordered quantity.

        Re-delivery of the same cumulative quantity is a no-op (one fill row). A lower
        cumulative reading is a stale replay: monotonic evidence keeps the higher value
        and the fill row is not duplicated. A reading above the ordered quantity raises
        :class:`OverfillError` before any write.
        """
        cumulative = _require_decimal("cumulative_qty", cumulative_qty, allow_zero=True)
        with self._transaction():
            current = self._fetch(intent_id)
            if cumulative > current.quantity:
                raise OverfillError(
                    f"intent {intent_id!r} ordered {_decimal_key(current.quantity)} "
                    f"but venue reports {_decimal_key(cumulative)}"
                )
            if cumulative == current.filled_qty:
                return current
            if cumulative < current.filled_qty:
                return current
            if current.state is IntentState.PENDING:
                raise InvalidTransition("fill evidence before submission")
            if current.state is IntentState.REJECTED:
                raise VenueEvidenceError("REJECTED intent cannot receive fill evidence")
            timestamp = _next_time(current, now_ns)
            if cumulative > 0:
                self._db.execute(
                    "INSERT OR IGNORE INTO intent_fills "
                    "(client_order_id, cumulative_qty, observed_ns) VALUES (?,?,?)",
                    (current.client_order_id, _decimal_key(cumulative), timestamp),
                )
            if current.state is IntentState.CANCELED:
                updated = replace(
                    current,
                    filled_qty=cumulative,
                    updated_ns=timestamp,
                    reconcile_note="FILL_AFTER_CANCEL",
                )
            elif cumulative == current.quantity:
                updated = replace(
                    current,
                    state=IntentState.FILLED,
                    filled_qty=cumulative,
                    venue_status=VenueOrderStatus.FILLED,
                    updated_ns=timestamp,
                    terminal_ns=timestamp,
                    acknowledged_ns=current.acknowledged_ns if current.acknowledged_ns is not None else timestamp,
                    reconcile_note=None,
                )
            else:
                updated = replace(
                    current,
                    state=IntentState.PARTIALLY_FILLED,
                    filled_qty=cumulative,
                    venue_status=VenueOrderStatus.PARTIALLY_FILLED,
                    updated_ns=timestamp,
                    acknowledged_ns=current.acknowledged_ns if current.acknowledged_ns is not None else timestamp,
                    reconcile_note=None,
                )
            self._persist(current, updated)
        return updated

    def request_cancel(self, intent_id: str, *, now_ns: int) -> IntentRecord:
        """Durably mark an in-flight cancel; terminal and pending intents are untouched.

        An ``UNKNOWN`` intent must be reconciled first: the journal cannot cancel an order
        whose existence at the venue is unproven.
        """
        with self._transaction():
            current = self._fetch(intent_id)
            if current.is_terminal:
                return current
            if current.state is IntentState.PENDING:
                raise InvalidTransition("an unsubmitted intent has nothing to cancel")
            if current.state is IntentState.UNKNOWN:
                raise InvalidTransition("reconcile the UNKNOWN intent before cancelling it")
            if current.state is IntentState.CANCEL_PENDING:
                return current
            updated = replace(
                current,
                state=IntentState.CANCEL_PENDING,
                updated_ns=_next_time(current, now_ns),
                reconcile_note="CANCEL_REQUESTED",
            )
            self._persist(current, updated)
        return updated

    def observe_cancel(self, intent_id: str, *, now_ns: int) -> IntentRecord:
        """Record a terminal cancel; a completed fill is never downgraded."""
        with self._transaction():
            current = self._fetch(intent_id)
            updated = self._cancel(current, now_ns)
            if updated is not current:
                self._persist(current, updated)
        return updated

    def _cancel(self, current: IntentRecord, now_ns: int) -> IntentRecord:
        if current.state is IntentState.FILLED:
            return current
        if current.state in {IntentState.CANCELED, IntentState.REJECTED}:
            return current
        if current.state is IntentState.PENDING:
            raise InvalidTransition("an unsubmitted intent has nothing to cancel")
        timestamp = _next_time(current, now_ns)
        return replace(
            current,
            state=IntentState.CANCELED,
            venue_status=VenueOrderStatus.CANCELED,
            updated_ns=timestamp,
            terminal_ns=timestamp,
            acknowledged_ns=current.acknowledged_ns if current.acknowledged_ns is not None else timestamp,
            reconcile_note=(
                "CANCELED_AFTER_PARTIAL_FILL" if current.filled_qty > 0 else None
            ),
        )

    def recover(self, now_ns: int) -> tuple[IntentRecord, ...]:
        """Return every intent whose venue outcome is unproven, marking it ``UNKNOWN``.

        ``SUBMITTING`` (submit call unobserved), ``SUBMITTED`` (acknowledgment
        unobserved) and ``CANCEL_PENDING`` (cancel outcome unobserved) are moved to
        ``UNKNOWN``. ``PENDING`` stays ``PENDING``: no submit was journaled, so a later
        submit is safe and reuses the same client order id. The returned order is stable.
        """
        timestamp = _require_ns("now_ns", now_ns)
        with self._transaction():
            rows = self._db.execute(
                f"SELECT {_COLUMNS} FROM intents ORDER BY created_ns, intent_id"
            ).fetchall()
            unknown: list[IntentRecord] = []
            for row in rows:
                current = _row_to_record(cast(sqlite3.Row, row))
                if current.state is IntentState.UNKNOWN:
                    unknown.append(current)
                    continue
                if current.state not in _RECONCILE_ON_RECOVERY:
                    continue
                note = {
                    IntentState.SUBMITTING: "RECOVERED_SUBMISSION_UNOBSERVED",
                    IntentState.SUBMITTED: "RECOVERED_ACK_UNOBSERVED",
                    IntentState.CANCEL_PENDING: "RECOVERED_CANCEL_UNOBSERVED",
                }[current.state]
                updated = replace(
                    current,
                    state=IntentState.UNKNOWN,
                    updated_ns=_next_time(current, timestamp),
                    reconcile_note=note,
                )
                self._persist(current, updated)
                unknown.append(updated)
        return tuple(unknown)

    def reconcile(
        self,
        record: IntentRecord,
        venue_view: VenueView,
        *,
        now_ns: int,
    ) -> IntentRecord:
        """Resolve an ``UNKNOWN`` intent only from injected venue evidence.

        ``record`` must still be ``UNKNOWN`` in this database. A client order id absent
        from both the open/terminal orders and the fills stays ``UNKNOWN`` forever; no
        absence is ever read as cancellation or rejection. Ambiguous or contradictory
        evidence raises :class:`VenueEvidenceError` and writes nothing.
        """
        if not isinstance(record, IntentRecord):
            raise TypeError("record must be an IntentRecord")
        if not isinstance(venue_view, VenueView):
            raise TypeError("venue_view must be a VenueView")
        with self._transaction():
            current = self._fetch(record.intent_id)
            if current.client_order_id != record.client_order_id:
                raise OutboxError(
                    "record identity does not match the journal", code="IDENTITY_MISMATCH"
                )
            if current.state is not IntentState.UNKNOWN:
                raise InvalidTransition(
                    f"reconcile resolves UNKNOWN only; recorded state is {current.state.value}"
                )
            orders = [
                order
                for order in venue_view.orders
                if order.client_order_id == current.client_order_id
            ]
            fills = [
                fill
                for fill in venue_view.fills
                if fill.client_order_id == current.client_order_id
            ]
            if not orders and not fills:
                return current

            statuses = {order.status for order in orders}
            if len(statuses) > 1:
                raise VenueEvidenceError(
                    f"ambiguous venue status for {current.client_order_id!r}: "
                    + ", ".join(sorted(status.value for status in statuses))
                )
            status = next(iter(statuses), VenueOrderStatus.UNKNOWN)
            order_qty = max((order.cumulative_qty for order in orders), default=Decimal("0"))
            fill_qty = max((fill.cumulative_qty for fill in fills), default=Decimal("0"))
            cumulative = max(order_qty, fill_qty)
            if cumulative > current.quantity:
                raise OverfillError(
                    f"venue reports {_decimal_key(cumulative)} filled for an order of "
                    f"{_decimal_key(current.quantity)}"
                )

            timestamp = _next_time(current, now_ns)
            if cumulative >= current.quantity:
                state = IntentState.FILLED
                venue_status = VenueOrderStatus.FILLED
                terminal_ns: int | None = timestamp
            elif status is VenueOrderStatus.CANCELED:
                state = IntentState.CANCELED
                venue_status = status
                terminal_ns = timestamp
            elif status is VenueOrderStatus.REJECTED:
                if cumulative != 0:
                    raise VenueEvidenceError(
                        "venue reports REJECTED together with fill evidence"
                    )
                state = IntentState.REJECTED
                venue_status = status
                terminal_ns = timestamp
            elif status is VenueOrderStatus.FILLED:
                raise VenueEvidenceError(
                    "venue reports FILLED below the ordered quantity; evidence is incomplete"
                )
            elif status is VenueOrderStatus.PARTIALLY_FILLED:
                if cumulative == 0:
                    raise VenueEvidenceError(
                        "venue reports PARTIALLY_FILLED with no cumulative quantity"
                    )
                state = IntentState.PARTIALLY_FILLED
                venue_status = status
                terminal_ns = None
            elif status in {VenueOrderStatus.SUBMITTED, VenueOrderStatus.ACCEPTED}:
                state = (
                    IntentState.PARTIALLY_FILLED if cumulative > 0 else IntentState.ACKNOWLEDGED
                )
                venue_status = VenueOrderStatus.PARTIALLY_FILLED if cumulative > 0 else status
                terminal_ns = None
            else:  # UNKNOWN with no order status: concrete fills may still resolve it.
                if cumulative == 0:
                    return current
                state = IntentState.PARTIALLY_FILLED
                venue_status = VenueOrderStatus.PARTIALLY_FILLED
                terminal_ns = None

            for evidence_qty in (
                *(order.cumulative_qty for order in orders),
                *(fill.cumulative_qty for fill in fills),
            ):
                if evidence_qty > 0:
                    self._db.execute(
                        "INSERT OR IGNORE INTO intent_fills "
                        "(client_order_id, cumulative_qty, observed_ns) VALUES (?,?,?)",
                        (
                            current.client_order_id,
                            _decimal_key(evidence_qty),
                            timestamp,
                        ),
                    )
            updated = replace(
                current,
                state=state,
                venue_status=venue_status,
                filled_qty=cumulative,
                updated_ns=timestamp,
                terminal_ns=terminal_ns,
                acknowledged_ns=current.acknowledged_ns if current.acknowledged_ns is not None else timestamp,
                reconcile_note=f"RECONCILED_{state.value}",
            )
            self._persist(current, updated)
        return updated
