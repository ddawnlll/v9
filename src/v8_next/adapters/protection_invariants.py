"""Native-agnostic protection invariants over an observed order/position event log.

The single native execution authority (NautilusTrader) owns order lifecycle, fills and
position accounting. This module owns *nothing*: it is a pure, side-effect-free checker
that consumes the events the authority already emits and answers one question — is the
open position actually protected right now, and if not, exactly how did that happen?

Event vocabulary
----------------
``ProtectionEvent`` is a frozen, validated record. It is keyed by the native
``position_id`` and ``client_order_id`` the venue reports; it never invents an identity.
The ``kind`` field is one of

``ORDER_SUBMITTED``, ``ACK``, ``PARTIAL_FILL``, ``FILL``, ``CANCEL_REQUEST``,
``CANCELED``, ``REJECTED``, ``POSITION_CHANGED``.

Field semantics, pinned by construction (unknown values raise ``ValueError``):

* ``quantity`` is an order's *total* quantity on ``ORDER_SUBMITTED``/``ACK``. A
  protective leg that the venue (or the resizing adapter) has amended re-emits ``ACK``
  with its new total quantity; that is how a partial entry fill resizes protection.
* ``filled_quantity`` is the order's *cumulative* filled quantity on
  ``PARTIAL_FILL``/``FILL``. Cumulative, not incremental, so replay is idempotent and a
  duplicate callback cannot double-count a fill.
* ``REJECTED`` with ``cancel_reject=True`` means the venue refused a *cancel request*:
  the order is still live, so protection is intact. ``cancel_reject=False`` means the
  order itself was rejected: it cannot protect anything.
* ``reduce_only`` is the order's native reduce-only flag. Protective legs are expected
  to carry it; a protective leg that can add risk is an invariant violation.

Protection vocabulary
---------------------
:class:`ProtectionState` is not defined here: it is the canonical
:class:`v8_next.domain.protection.ProtectionState`, imported and re-exported so the
producer of a verdict and its consumer (``v8_next.economics.management``) cannot
drift into two vocabularies. ``PARTIAL`` in particular must survive the join: it is
the state that forces a protective resize downstream.

Invariants
----------
1. The live, acknowledged stop quantity MUST equal the actual filled position quantity.
   A partial entry fill makes the incumbent protection under-sized until it is resized;
   that state is ``PARTIAL`` and is named as a violation, never rounded away.
2. A reduce-only order whose remaining quantity exceeds the open position is a
   violation, and a fill beyond the open quantity is clamped at flat: the checker never
   reports a reversal. The ambition to reverse is recorded, the position is not flipped.
3. A live stop and a live target must be on the protective side of the position and on
   the correct side of each other (``stop < target`` long, ``stop > target`` short).
4. A ``CANCEL_REQUEST`` never removes protection; only ``CANCELED`` does. A rejected
   cancel leaves the order live and therefore leaves protection intact.
5. A protective order that was rejected, unknown, unacknowledged, unverifiable or
   under-sized cannot yield ``PROTECTED``. ``REJECTED`` outranks ``UNKNOWN`` outranks
   ``GAP`` outranks ``PARTIAL``; only a clean, fully acknowledged, correctly sized,
   reduce-only stop (and any live target) yields ``PROTECTED``.

Determinism and replay
----------------------
The checker is a pure reducer. The same event sequence yields the same state, the same
violation list and the same :meth:`BracketInvariantChecker.digest`, on any machine.

* Re-applying an event that was already applied is a no-op, so a restarted process can
  replay the full journal onto a fresh checker (or the same one) and land on the same
  state. Event identity is the canonical payload of the event, not object identity.
* A late fill whose cumulative quantity goes *backwards* is a distinct event and is
  reported as ``STALE_FILL_IGNORED``; its quantity is never applied.
* A fill/cancel/reject for an order never submitted, or a protective event for a
  position never observed, is ``UNKNOWN_ORDER_REFERENCE`` and poisons ``PROTECTED`` for
  that position.
* A position snapshot (``POSITION_CHANGED``) is authoritative: it resets the
  fill-derived baseline, and a disagreement with fills applied since the previous
  snapshot is ``POSITION_QUANTITY_CONFLICT`` (the snapshot is adopted, the conflict is
  recorded). Protection that survives its position's close is marked stale at that
  moment and can never cover a new ``position_id`` (or a reopen of the same one): it is
  reported as ``STALE_PROTECTION_ORDER`` and excluded from every live quantity; a fill
  belonging to stale protection is ``STALE_FILL_IGNORED`` and is never applied to the
  current position.
* A bracket-leg race (both legs report fills) closes the position on the first fill;
  the second fill is ``PROTECTIVE_FILL_AFTER_FLAT`` and is never applied, so the
  position cannot reverse. Both orderings end flat with exactly that one violation.

No order is constructed, submitted, amended or canceled here, and nothing in this module
imports an execution library.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Final

from v8_next.domain.protection import ProtectionState
from v8_next.telemetry.identity import canonical_digest

__all__ = [
    "BracketInvariantChecker",
    "Direction",
    "OrderRole",
    "ProtectionEvent",
    "ProtectionEventKind",
    "ProtectionState",
    "ProtectionViolation",
    "Side",
    "ViolationCode",
    "ZERO",
]

#: Exact decimal zero. Money and quantities are ``Decimal`` only; floats are rejected.
ZERO: Final[Decimal] = Decimal("0")


class ProtectionEventKind(StrEnum):
    """The observed native event kinds this checker reduces."""

    ORDER_SUBMITTED = "ORDER_SUBMITTED"
    ACK = "ACK"
    PARTIAL_FILL = "PARTIAL_FILL"
    FILL = "FILL"
    CANCEL_REQUEST = "CANCEL_REQUEST"
    CANCELED = "CANCELED"
    REJECTED = "REJECTED"
    POSITION_CHANGED = "POSITION_CHANGED"


class OrderRole(StrEnum):
    """The economic role of an order inside a bracket."""

    ENTRY = "ENTRY"
    STOP = "STOP"
    TARGET = "TARGET"
    OTHER = "OTHER"


class Direction(StrEnum):
    """The direction of the native position."""

    LONG = "LONG"
    SHORT = "SHORT"


class Side(StrEnum):
    """The native order side."""

    BUY = "BUY"
    SELL = "SELL"


_PROTECTIVE_ROLES: Final[frozenset[OrderRole]] = frozenset(
    {OrderRole.STOP, OrderRole.TARGET}
)


class ViolationCode(StrEnum):
    """Stable, machine-readable invariant violations.

    Codes are replay surface: names are wire values and must not be renamed.
    """

    PROTECTION_QUANTITY_MISMATCH = "PROTECTION_QUANTITY_MISMATCH"
    REDUCE_ONLY_OVER_QUANTITY = "REDUCE_ONLY_OVER_QUANTITY"
    REDUCE_ONLY_REVERSAL_BLOCKED = "REDUCE_ONLY_REVERSAL_BLOCKED"
    PROTECTIVE_FILL_AFTER_FLAT = "PROTECTIVE_FILL_AFTER_FLAT"
    PROTECTIVE_ORDER_REJECTED = "PROTECTIVE_ORDER_REJECTED"
    PROTECTIVE_ORDER_NOT_REDUCE_ONLY = "PROTECTIVE_ORDER_NOT_REDUCE_ONLY"
    PROTECTIVE_SIDE_INCONSISTENT = "PROTECTIVE_SIDE_INCONSISTENT"
    BRACKET_GEOMETRY_INCONSISTENT = "BRACKET_GEOMETRY_INCONSISTENT"
    DIRECTION_UNKNOWN = "DIRECTION_UNKNOWN"
    MISSING_ACK = "MISSING_ACK"
    UNKNOWN_ORDER_REFERENCE = "UNKNOWN_ORDER_REFERENCE"
    ORDER_IDENTITY_CONFLICT = "ORDER_IDENTITY_CONFLICT"
    POSITION_QUANTITY_CONFLICT = "POSITION_QUANTITY_CONFLICT"
    POSITION_DIRECTION_CONFLICT = "POSITION_DIRECTION_CONFLICT"
    STALE_PROTECTION_ORDER = "STALE_PROTECTION_ORDER"
    CANCEL_REJECT_ORDER_STILL_LIVE = "CANCEL_REJECT_ORDER_STILL_LIVE"
    CANCEL_REJECT_WITHOUT_REQUEST = "CANCEL_REJECT_WITHOUT_REQUEST"
    CANCELED_AFTER_FILL = "CANCELED_AFTER_FILL"
    STALE_FILL_IGNORED = "STALE_FILL_IGNORED"
    FILL_QUANTITY_CONFLICT = "FILL_QUANTITY_CONFLICT"


@dataclass(frozen=True, slots=True)
class ProtectionViolation:
    """One recorded anomaly. Membership is deduplicated by identity, not by time."""

    code: ViolationCode
    ts_ns: int
    position_id: str
    client_order_id: str
    detail: str


def _decimal(value: object, *, name: str) -> Decimal:
    """Parse a non-negative finite decimal, rejecting floats and booleans."""
    if isinstance(value, Decimal):
        parsed = value
    elif isinstance(value, bool):
        raise ValueError(f"{name} must be a decimal quantity, not a bool")
    elif isinstance(value, int):
        parsed = Decimal(value)
    elif isinstance(value, str):
        try:
            parsed = Decimal(value)
        except InvalidOperation as exc:
            raise ValueError(f"{name} is not a decimal quantity: {value!r}") from exc
    else:
        raise ValueError(f"{name} must be Decimal, int or str, not {type(value).__name__}")
    if not parsed.is_finite() or parsed < ZERO:
        raise ValueError(f"{name} must be finite and non-negative: {parsed}")
    return parsed


def _optional_decimal(value: object | None, *, name: str) -> Decimal | None:
    return None if value is None else _decimal(value, name=name)


def _decimal_text(value: Decimal) -> str:
    """Stable decimal rendering for digests (``0.50`` and ``0.5`` are one value)."""
    return format(value.normalize(), "f")


@dataclass(frozen=True, slots=True)
class ProtectionEvent:
    """One observed native event, validated at construction.

    Unknown enum values, negative quantities, floats and fills without a cumulative
    quantity raise ``ValueError``: an unreadable event is refused, never defaulted.
    """

    kind: ProtectionEventKind
    ts_ns: int
    client_order_id: str = ""
    position_id: str = ""
    role: OrderRole = OrderRole.ENTRY
    direction: Direction | None = None
    side: Side | None = None
    quantity: Decimal = ZERO
    filled_quantity: Decimal = ZERO
    price: Decimal | None = None
    reduce_only: bool = False
    cancel_reject: bool = False
    reason: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "kind", ProtectionEventKind(self.kind))
        object.__setattr__(self, "role", OrderRole(self.role))
        if self.direction is not None:
            object.__setattr__(self, "direction", Direction(self.direction))
        if self.side is not None:
            object.__setattr__(self, "side", Side(self.side))
        object.__setattr__(self, "quantity", _decimal(self.quantity, name="quantity"))
        object.__setattr__(
            self, "filled_quantity", _decimal(self.filled_quantity, name="filled_quantity")
        )
        price = _optional_decimal(self.price, name="price")
        if price is not None and price <= ZERO:
            raise ValueError(f"price must be positive: {price}")
        object.__setattr__(self, "price", price)
        if type(self.ts_ns) is not int or self.ts_ns < 0:
            raise ValueError(f"ts_ns must be a non-negative int: {self.ts_ns!r}")
        if self.kind is ProtectionEventKind.POSITION_CHANGED:
            if not self.position_id:
                raise ValueError("POSITION_CHANGED requires a native position id")
        else:
            if not self.client_order_id:
                raise ValueError(f"{self.kind.value} requires a client order id")
        if self.kind is ProtectionEventKind.ORDER_SUBMITTED and self.quantity <= ZERO:
            raise ValueError("ORDER_SUBMITTED requires a positive order quantity")
        if (
            self.kind in (ProtectionEventKind.PARTIAL_FILL, ProtectionEventKind.FILL)
            and self.filled_quantity <= ZERO
        ):
            raise ValueError("fill events require a positive cumulative filled quantity")

    def identity(self) -> str:
        """Canonical payload identity: equal payloads are the same event on replay."""
        return canonical_digest(
            "protection_invariants.event",
            {
                "kind": self.kind.value,
                "ts_ns": self.ts_ns,
                "client_order_id": self.client_order_id,
                "position_id": self.position_id,
                "role": self.role.value,
                "direction": self.direction.value if self.direction is not None else None,
                "side": self.side.value if self.side is not None else None,
                "quantity": _decimal_text(self.quantity),
                "filled_quantity": _decimal_text(self.filled_quantity),
                "price": _decimal_text(self.price) if self.price is not None else None,
                "reduce_only": self.reduce_only,
                "cancel_reject": self.cancel_reject,
                "reason": self.reason,
            },
        )


@dataclass(slots=True)
class _Position:
    position_id: str
    direction: Direction | None = None
    quantity: Decimal = ZERO
    entry_filled: Decimal = ZERO
    dirty_since_snapshot: bool = False
    conflicted: bool = False
    closed: bool = False


@dataclass(slots=True)
class _Order:
    client_order_id: str
    role: OrderRole = OrderRole.ENTRY
    position_id: str = ""
    direction: Direction | None = None
    side: Side | None = None
    price: Decimal | None = None
    quantity: Decimal = ZERO
    filled: Decimal = ZERO
    reduce_only: bool = False
    acked: bool = False
    cancel_requested: bool = False
    canceled: bool = False
    rejected: bool = False
    unknown: bool = False
    stale: bool = False

    @property
    def remaining(self) -> Decimal:
        remaining = self.quantity - self.filled
        return remaining if remaining > ZERO else ZERO

    @property
    def live(self) -> bool:
        """Live means: readable, un-canceled, un-rejected, un-stale, with quantity."""
        return (
            not (self.canceled or self.rejected or self.unknown or self.stale)
            and self.quantity > ZERO
            and self.remaining > ZERO
        )


class BracketInvariantChecker:
    """Pure reducer over ``ProtectionEvent``s for one bracketed native position.

    The checker holds no order authority. It derives, from the observed events, the
    filled position quantity, the live acknowledged protective quantity, the position's
    protection state and the full anomaly log. The final state is :attr:`state` (the
    most recently opened position) or :meth:`state_of` per native position id.
    """

    def __init__(self) -> None:
        self._positions: dict[str, _Position] = {}
        self._orders: dict[str, _Order] = {}
        self._violations: list[ProtectionViolation] = []
        self._violation_keys: set[tuple[str, str, str, str]] = set()
        self._applied: set[str] = set()
        self._active_position_id: str | None = None
        self._suppress_records = False

    # ------------------------------------------------------------------ public
    @property
    def state(self) -> ProtectionState:
        """Protection state of the most recently opened position (flat ⇒ UNPROTECTED)."""
        if self._active_position_id is None:
            return ProtectionState.UNPROTECTED
        return self.state_of(self._active_position_id)

    @property
    def violations(self) -> tuple[ProtectionViolation, ...]:
        """Every distinct anomaly observed, in event order."""
        return tuple(self._violations)

    @property
    def events_applied(self) -> int:
        """Number of distinct events applied (replayed events are not re-counted)."""
        return len(self._applied)

    def state_of(self, position_id: str) -> ProtectionState:
        """Protection state of one native position id (unknown id ⇒ UNKNOWN)."""
        position = self._positions.get(position_id)
        if position is None:
            return ProtectionState.UNKNOWN
        if position.quantity <= ZERO:
            return ProtectionState.UNPROTECTED
        protective = [
            order for order in self._protective_orders(position_id) if not order.stale
        ]
        if any(order.rejected for order in protective):
            return ProtectionState.REJECTED
        if any(order.unknown for order in protective):
            return ProtectionState.UNKNOWN
        if position.direction is None or position.conflicted:
            return ProtectionState.UNKNOWN
        live = [order for order in protective if order.live]
        if self._side_or_geometry_broken(position, live):
            return ProtectionState.UNKNOWN
        if any(
            order.reduce_only and order.remaining > position.quantity for order in live
        ):
            return ProtectionState.UNKNOWN
        if any(not order.acked for order in live):
            return ProtectionState.GAP
        if any(not order.reduce_only for order in live):
            return ProtectionState.UNKNOWN
        stops = [order for order in live if order.role is OrderRole.STOP]
        targets = [order for order in live if order.role is OrderRole.TARGET]
        if not stops:
            return ProtectionState.UNPROTECTED
        if sum((order.remaining for order in stops), ZERO) != position.quantity:
            return ProtectionState.PARTIAL
        if targets and sum((order.remaining for order in targets), ZERO) != position.quantity:
            return ProtectionState.PARTIAL
        return ProtectionState.PROTECTED

    def position_quantity(self, position_id: str | None = None) -> Decimal:
        """Filled quantity of ``position_id`` (default: active position); 0 if absent."""
        resolved = position_id if position_id is not None else self._active_position_id
        if resolved is None:
            return ZERO
        position = self._positions.get(resolved)
        return ZERO if position is None else position.quantity

    def protective_quantity(self, position_id: str | None = None) -> Decimal:
        """Remaining quantity of the live, acknowledged STOP leg.

        This is the quantity that must equal :meth:`position_quantity`.
        """
        return self._live_role_quantity(position_id, OrderRole.STOP)

    def target_quantity(self, position_id: str | None = None) -> Decimal:
        """Remaining quantity of the live, acknowledged TARGET leg."""
        return self._live_role_quantity(position_id, OrderRole.TARGET)

    def order_remaining(self, client_order_id: str) -> Decimal:
        """Remaining quantity of one order (0 for an id never observed)."""
        order = self._orders.get(client_order_id)
        return ZERO if order is None else order.remaining

    def apply(self, event: ProtectionEvent) -> ProtectionState:
        """Apply one event; exact replays are no-ops. Returns the resulting state."""
        identity = event.identity()
        if identity in self._applied:
            return self.state
        self._applied.add(identity)
        match event.kind:
            case ProtectionEventKind.POSITION_CHANGED:
                self._on_position_changed(event)
            case ProtectionEventKind.ORDER_SUBMITTED:
                self._on_submitted(event)
            case ProtectionEventKind.ACK:
                self._on_ack(event)
            case ProtectionEventKind.PARTIAL_FILL | ProtectionEventKind.FILL:
                self._on_fill(event)
            case ProtectionEventKind.CANCEL_REQUEST:
                self._on_cancel_request(event)
            case ProtectionEventKind.CANCELED:
                self._on_canceled(event)
            case ProtectionEventKind.REJECTED:
                self._on_rejected(event)
        self._recheck(event)
        return self.state

    def apply_all(self, events: Iterable[ProtectionEvent]) -> ProtectionState:
        """Apply a journal in order; returns the final state."""
        state = self.state
        for event in events:
            state = self.apply(event)
        return state

    def apply_batch(self, events: Iterable[ProtectionEvent]) -> ProtectionState:
        """Apply one engine-step batch, recording only its final-state violations.

        Native callbacks for a bracket leg can arrive in the same engine step as
        the leg's submission. Applying them one by one records a transient
        ``MISSING_ACK`` that the ACK in the same step immediately resolves. A
        batch is the honest unit for that observation: intermediate microstates
        are not published, but the final state (and a genuinely unacknowledged
        leg) still records every violation. Per-event ``apply``/``apply_all``
        semantics are unchanged.
        """
        batch = list(events)
        affected: set[str] = set()
        previous = self._suppress_records
        self._suppress_records = True
        try:
            for event in batch:
                self.apply(event)
                if event.position_id:
                    affected.add(event.position_id)
                if event.client_order_id:
                    order = self._orders.get(event.client_order_id)
                    if order is not None and order.position_id:
                        affected.add(order.position_id)
        finally:
            self._suppress_records = previous
        if affected:
            ts_ns = batch[-1].ts_ns if batch else 0
            for position_id in sorted(affected):
                self._validate(position_id, ts_ns)
        return self.state

    def snapshot(self) -> dict[str, object]:
        """Canonical JSON-safe view of the derived state (no wall clock)."""
        return {
            "active_position_id": self._active_position_id,
            "positions": {
                position_id: {
                    "direction": (
                        position.direction.value if position.direction is not None else None
                    ),
                    "quantity": _decimal_text(position.quantity),
                    "state": self.state_of(position_id).value,
                }
                for position_id, position in sorted(self._positions.items())
            },
            "orders": {
                client_order_id: {
                    "role": order.role.value,
                    "position_id": order.position_id,
                    "quantity": _decimal_text(order.quantity),
                    "filled": _decimal_text(order.filled),
                    "remaining": _decimal_text(order.remaining),
                    "acked": order.acked,
                    "reduce_only": order.reduce_only,
                    "cancel_requested": order.cancel_requested,
                    "canceled": order.canceled,
                    "rejected": order.rejected,
                    "unknown": order.unknown,
                    "stale": order.stale,
                }
                for client_order_id, order in sorted(self._orders.items())
            },
            "violations": [
                {
                    "code": violation.code.value,
                    "ts_ns": violation.ts_ns,
                    "position_id": violation.position_id,
                    "client_order_id": violation.client_order_id,
                    "detail": violation.detail,
                }
                for violation in self._violations
            ],
        }

    def digest(self) -> str:
        """Deterministic identity of the derived state and anomaly log."""
        return canonical_digest("protection_invariants", self.snapshot())

    # ---------------------------------------------------------------- internals
    def _live_role_quantity(
        self, position_id: str | None, role: OrderRole
    ) -> Decimal:
        resolved = position_id if position_id is not None else self._active_position_id
        if resolved is None:
            return ZERO
        return sum(
            (
                order.remaining
                for order in self._protective_orders(resolved)
                if order.role is role and order.live and order.acked
            ),
            ZERO,
        )

    def _protective_orders(self, position_id: str) -> list[_Order]:
        orders = [
            order
            for order in self._orders.values()
            if order.position_id == position_id and order.role in _PROTECTIVE_ROLES
        ]
        orders.sort(key=lambda order: order.client_order_id)
        return orders

    def _position(self, position_id: str) -> _Position:
        position = self._positions.get(position_id)
        if position is None:
            position = _Position(position_id=position_id)
            self._positions[position_id] = position
        return position

    def _record(
        self,
        code: ViolationCode,
        *,
        ts_ns: int,
        position_id: str,
        client_order_id: str,
        detail: str,
    ) -> None:
        if self._suppress_records:
            return
        key = (code.value, position_id, client_order_id, detail)
        if key in self._violation_keys:
            return
        self._violation_keys.add(key)
        self._violations.append(
            ProtectionViolation(
                code=code,
                ts_ns=ts_ns,
                position_id=position_id,
                client_order_id=client_order_id,
                detail=detail,
            )
        )

    def _record_event(
        self,
        code: ViolationCode,
        event: ProtectionEvent,
        *,
        detail: str,
        position_id: str | None = None,
        client_order_id: str | None = None,
    ) -> None:
        self._record(
            code,
            ts_ns=event.ts_ns,
            position_id=event.position_id if position_id is None else position_id,
            client_order_id=(
                event.client_order_id if client_order_id is None else client_order_id
            ),
            detail=detail,
        )

    def _side_or_geometry_broken(
        self, position: _Position, live: list[_Order]
    ) -> bool:
        if position.direction is None:
            return True
        expected_side = Side.SELL if position.direction is Direction.LONG else Side.BUY
        for order in live:
            if order.side is not expected_side or order.price is None:
                return True
        stops = [order for order in live if order.role is OrderRole.STOP]
        targets = [order for order in live if order.role is OrderRole.TARGET]
        if stops and targets:
            stop_price = stops[0].price
            target_price = targets[0].price
            if stop_price is None or target_price is None:
                return True
            sign = 1 if position.direction is Direction.LONG else -1
            if sign * (target_price - stop_price) <= ZERO:
                return True
        return False

    def _on_position_changed(self, event: ProtectionEvent) -> None:
        position = self._position(event.position_id)
        # Conflict is a property of the current snapshot, not a permanent brand.
        # A later authoritative snapshot that reconciles with the fills clears it;
        # a snapshot that still disagrees sets it again below.
        position.conflicted = False
        if event.direction is not None:
            if position.direction is not None and position.direction is not event.direction:
                self._record_event(
                    ViolationCode.POSITION_DIRECTION_CONFLICT,
                    event,
                    detail=(
                        f"snapshot direction {event.direction.value} contradicts "
                        f"{position.direction.value}"
                    ),
                )
                position.conflicted = True
            position.direction = event.direction
        if position.dirty_since_snapshot and event.quantity != position.quantity:
            self._record_event(
                ViolationCode.POSITION_QUANTITY_CONFLICT,
                event,
                detail=f"snapshot={event.quantity} fill-derived={position.quantity}",
            )
            position.conflicted = True
        position.quantity = event.quantity
        position.entry_filled = ZERO
        position.dirty_since_snapshot = False
        if event.quantity <= ZERO:
            position.closed = True
            for order in self._protective_orders(position.position_id):
                if order.live:
                    self._record_event(
                        ViolationCode.STALE_PROTECTION_ORDER,
                        event,
                        position_id=position.position_id,
                        client_order_id=order.client_order_id,
                        detail="position closed with live protection",
                    )
                order.stale = True
            return
        position.closed = False
        self._active_position_id = event.position_id

    def _on_submitted(self, event: ProtectionEvent) -> None:
        order = self._orders.get(event.client_order_id)
        if order is None:
            order = _Order(
                client_order_id=event.client_order_id,
                role=event.role,
                position_id=event.position_id,
                direction=event.direction,
                side=event.side,
                price=event.price,
                quantity=event.quantity,
                reduce_only=event.reduce_only,
            )
            self._orders[event.client_order_id] = order
        else:
            if order.role is not event.role:
                self._record_event(
                    ViolationCode.ORDER_IDENTITY_CONFLICT,
                    event,
                    detail=f"resubmitted as {event.role.value}, first seen as {order.role.value}",
                )
                order.unknown = True
                return
            if order.position_id and event.position_id and order.position_id != event.position_id:
                self._record_event(
                    ViolationCode.ORDER_IDENTITY_CONFLICT,
                    event,
                    detail=(
                        f"resubmitted for position {event.position_id}, "
                        f"first seen for {order.position_id}"
                    ),
                )
                order.unknown = True
                return
            order.quantity = event.quantity
            if event.direction is not None:
                order.direction = event.direction
            if event.side is not None:
                order.side = event.side
            if event.price is not None:
                order.price = event.price
            if event.reduce_only:
                order.reduce_only = True
            if not order.position_id:
                order.position_id = event.position_id
        if order.role in _PROTECTIVE_ROLES and not order.position_id:
            self._record_event(
                ViolationCode.UNKNOWN_ORDER_REFERENCE,
                event,
                detail="protective order submitted without a native position id",
            )

    def _on_ack(self, event: ProtectionEvent) -> None:
        order = self._orders.get(event.client_order_id)
        if order is None:
            unknown = event.role in _PROTECTIVE_ROLES
            order = _Order(
                client_order_id=event.client_order_id,
                role=event.role,
                position_id=event.position_id,
                direction=event.direction,
                side=event.side,
                price=event.price,
                quantity=event.quantity,
                reduce_only=event.reduce_only,
                unknown=unknown,
            )
            self._orders[event.client_order_id] = order
            if unknown:
                self._record_event(
                    ViolationCode.UNKNOWN_ORDER_REFERENCE,
                    event,
                    detail="acknowledgement for a protective order never submitted",
                )
        # A callback for the same client id cannot authenticate a different
        # protective role or position. Never let a mismatched ACK establish
        # coverage for the original position.
        if (
            order.role is not event.role
            or (order.position_id and event.position_id and order.position_id != event.position_id)
        ):
            self._record_event(
                ViolationCode.ORDER_IDENTITY_CONFLICT,
                event,
                position_id=order.position_id,
                detail=(
                    f"ack role={event.role.value} position={event.position_id!r} "
                    f"does not match submitted role={order.role.value} "
                    f"position={order.position_id!r}"
                ),
            )
            order.unknown = True
            return
        order.acked = True
        if event.quantity > ZERO:
            order.quantity = event.quantity
        if event.direction is not None:
            order.direction = event.direction
        if event.side is not None:
            order.side = event.side
        if event.price is not None:
            order.price = event.price
        if event.reduce_only:
            order.reduce_only = True
        if not order.position_id:
            order.position_id = event.position_id

    def _on_fill(self, event: ProtectionEvent) -> None:
        order = self._orders.get(event.client_order_id)
        if order is None:
            if event.role in _PROTECTIVE_ROLES:
                self._orders[event.client_order_id] = _Order(
                    client_order_id=event.client_order_id,
                    role=event.role,
                    position_id=event.position_id,
                    direction=event.direction,
                    side=event.side,
                    price=event.price,
                    quantity=max(event.quantity, event.filled_quantity),
                    filled=event.filled_quantity,
                    unknown=True,
                )
                self._record_event(
                    ViolationCode.UNKNOWN_ORDER_REFERENCE,
                    event,
                    detail="fill for a protective order never submitted",
                )
                return
            order = _Order(
                client_order_id=event.client_order_id,
                role=event.role,
                position_id=event.position_id,
                direction=event.direction,
                side=event.side,
                price=event.price,
                quantity=max(event.quantity, event.filled_quantity),
                reduce_only=event.reduce_only,
            )
            self._orders[event.client_order_id] = order
        cumulative = event.filled_quantity
        delta = cumulative - order.filled
        if delta < ZERO:
            self._record_event(
                ViolationCode.STALE_FILL_IGNORED,
                event,
                detail=f"cumulative={cumulative} already-applied={order.filled}",
            )
            return
        if delta == ZERO:
            return
        if order.quantity > ZERO and cumulative > order.quantity:
            self._record_event(
                ViolationCode.FILL_QUANTITY_CONFLICT,
                event,
                detail=f"cumulative={cumulative} order-quantity={order.quantity}",
            )
        order.filled = cumulative
        # A venue fill is stronger evidence of acceptance than the ACK callback:
        # an order can fill before (or without) an observable OrderAccepted
        # callback, and reporting that as MISSING_ACK would be a false gap.
        order.acked = True
        if order.stale:
            self._record_event(
                ViolationCode.STALE_FILL_IGNORED,
                event,
                detail=(
                    f"{order.role.value} fill belongs to a closed position; "
                    "never applied to the current one"
                ),
            )
            return
        if order.role is OrderRole.ENTRY:
            position_id = order.position_id or event.position_id
            if not position_id:
                self._record_event(
                    ViolationCode.UNKNOWN_ORDER_REFERENCE,
                    event,
                    detail="entry fill without a native position id",
                )
                return
            position = self._position(position_id)
            position.quantity += delta
            position.entry_filled += delta
            position.dirty_since_snapshot = True
            if event.direction is not None:
                position.direction = event.direction
            if (
                self._active_position_id in (None, position_id)
                or self.position_quantity(self._active_position_id) <= ZERO
            ):
                self._active_position_id = position_id
            return
        position_id = order.position_id or event.position_id
        held = self._positions.get(position_id) if position_id else None
        if held is None:
            self._record_event(
                ViolationCode.UNKNOWN_ORDER_REFERENCE,
                event,
                detail=f"{order.role.value} fill without a known native position",
            )
            return
        if order.role is OrderRole.OTHER and not order.reduce_only:
            held.quantity += delta
            held.dirty_since_snapshot = True
            return
        if held.quantity <= ZERO:
            self._record_event(
                ViolationCode.PROTECTIVE_FILL_AFTER_FLAT,
                event,
                detail=(
                    f"{order.role.value} filled {delta} after the position was already flat"
                ),
            )
            return
        if delta > held.quantity:
            code = (
                ViolationCode.REDUCE_ONLY_REVERSAL_BLOCKED
                if order.reduce_only
                else ViolationCode.PROTECTIVE_FILL_AFTER_FLAT
            )
            self._record_event(
                code,
                event,
                detail=f"fill={delta} exceeds position={held.quantity}",
            )
            held.quantity = ZERO
        else:
            held.quantity -= delta
        held.dirty_since_snapshot = True

    def _on_cancel_request(self, event: ProtectionEvent) -> None:
        order = self._orders.get(event.client_order_id)
        if order is None:
            self._orders[event.client_order_id] = _Order(
                client_order_id=event.client_order_id,
                role=event.role,
                position_id=event.position_id,
                side=event.side,
                price=event.price,
                quantity=event.quantity,
                reduce_only=event.reduce_only,
                unknown=True,
            )
            self._record_event(
                ViolationCode.UNKNOWN_ORDER_REFERENCE,
                event,
                detail="cancel requested for an order never submitted",
            )
            return
        order.cancel_requested = True

    def _on_canceled(self, event: ProtectionEvent) -> None:
        order = self._orders.get(event.client_order_id)
        if order is None:
            self._orders[event.client_order_id] = _Order(
                client_order_id=event.client_order_id,
                role=event.role,
                position_id=event.position_id,
                side=event.side,
                price=event.price,
                quantity=event.quantity,
                reduce_only=event.reduce_only,
                unknown=True,
            )
            self._record_event(
                ViolationCode.UNKNOWN_ORDER_REFERENCE,
                event,
                detail="cancel confirmation for an order never submitted",
            )
            return
        if order.canceled:
            return
        order.canceled = True
        order.cancel_requested = False
        if order.quantity > ZERO and order.filled >= order.quantity:
            self._record_event(
                ViolationCode.CANCELED_AFTER_FILL,
                event,
                detail="cancel confirmation for an already fully filled order",
            )

    def _on_rejected(self, event: ProtectionEvent) -> None:
        order = self._orders.get(event.client_order_id)
        if order is None:
            unknown = event.role in _PROTECTIVE_ROLES
            order = _Order(
                client_order_id=event.client_order_id,
                role=event.role,
                position_id=event.position_id,
                side=event.side,
                price=event.price,
                quantity=event.quantity,
                reduce_only=event.reduce_only,
                unknown=unknown,
            )
            self._orders[event.client_order_id] = order
            if unknown:
                self._record_event(
                    ViolationCode.UNKNOWN_ORDER_REFERENCE,
                    event,
                    detail="rejection for a protective order never submitted",
                )
            return
        if event.cancel_reject:
            if not order.cancel_requested:
                self._record_event(
                    ViolationCode.CANCEL_REJECT_WITHOUT_REQUEST,
                    event,
                    detail="venue rejected a cancel that was never requested",
                )
            order.cancel_requested = False
            self._record_event(
                ViolationCode.CANCEL_REJECT_ORDER_STILL_LIVE,
                event,
                detail=f"cancel rejected ({event.reason or 'no reason'}); order remains live",
            )
            return
        order.rejected = True
        if order.role in _PROTECTIVE_ROLES:
            self._record_event(
                ViolationCode.PROTECTIVE_ORDER_REJECTED,
                event,
                detail=f"{order.role.value} rejected: {event.reason or 'no reason'}",
            )

    def _recheck(self, event: ProtectionEvent) -> None:
        affected: set[str] = set()
        if event.position_id:
            affected.add(event.position_id)
        if event.client_order_id:
            order = self._orders.get(event.client_order_id)
            if order is not None and order.position_id:
                affected.add(order.position_id)
        for position_id in sorted(affected):
            self._validate(position_id, event.ts_ns)

    def _validate(self, position_id: str, ts_ns: int) -> None:
        position = self._positions.get(position_id)
        if position is None or position.quantity <= ZERO:
            return
        quantity = position.quantity
        protective = self._protective_orders(position_id)
        live = [order for order in protective if order.live]
        expected_side = (
            Side.SELL
            if position.direction is Direction.LONG
            else Side.BUY
            if position.direction is Direction.SHORT
            else None
        )
        if expected_side is None:
            self._record(
                ViolationCode.DIRECTION_UNKNOWN,
                ts_ns=ts_ns,
                position_id=position_id,
                client_order_id="",
                detail="open position without an observed direction",
            )
        for order in live:
            if expected_side is not None and order.side is not expected_side:
                observed = order.side.value if order.side is not None else "missing"
                self._record(
                    ViolationCode.PROTECTIVE_SIDE_INCONSISTENT,
                    ts_ns=ts_ns,
                    position_id=position_id,
                    client_order_id=order.client_order_id,
                    detail=f"side={observed} expected={expected_side.value}",
                )
            if not order.reduce_only:
                self._record(
                    ViolationCode.PROTECTIVE_ORDER_NOT_REDUCE_ONLY,
                    ts_ns=ts_ns,
                    position_id=position_id,
                    client_order_id=order.client_order_id,
                    detail=f"{order.role.value} is live without reduce-only",
                )
            if not order.acked:
                self._record(
                    ViolationCode.MISSING_ACK,
                    ts_ns=ts_ns,
                    position_id=position_id,
                    client_order_id=order.client_order_id,
                    detail="protective order is live without a venue acknowledgement",
                )
        stops = [order for order in live if order.role is OrderRole.STOP]
        targets = [order for order in live if order.role is OrderRole.TARGET]
        if stops and targets and expected_side is not None:
            stop_price = stops[0].price
            target_price = targets[0].price
            if stop_price is None or target_price is None:
                self._record(
                    ViolationCode.BRACKET_GEOMETRY_INCONSISTENT,
                    ts_ns=ts_ns,
                    position_id=position_id,
                    client_order_id=stops[0].client_order_id,
                    detail="live stop/target without prices",
                )
            else:
                sign = 1 if position.direction is Direction.LONG else -1
                if sign * (target_price - stop_price) <= ZERO:
                    self._record(
                        ViolationCode.BRACKET_GEOMETRY_INCONSISTENT,
                        ts_ns=ts_ns,
                        position_id=position_id,
                        client_order_id=stops[0].client_order_id,
                        detail=(
                            f"stop={_decimal_text(stop_price)} "
                            f"target={_decimal_text(target_price)} "
                            f"direction={position.direction.value if position.direction else 'UNKNOWN'}"
                        ),
                    )
        for order in self._orders.values():
            if order.position_id != position_id or not order.reduce_only or not order.live:
                continue
            if order.remaining > quantity:
                self._record(
                    ViolationCode.REDUCE_ONLY_OVER_QUANTITY,
                    ts_ns=ts_ns,
                    position_id=position_id,
                    client_order_id=order.client_order_id,
                    detail=(
                        f"remaining={_decimal_text(order.remaining)} "
                        f"position={_decimal_text(quantity)}"
                    ),
                )
        for role, leg in ((OrderRole.STOP, stops), (OrderRole.TARGET, targets)):
            acknowledged = [order for order in leg if order.acked]
            if not acknowledged:
                continue
            total = sum((order.remaining for order in acknowledged), ZERO)
            if total != quantity:
                self._record(
                    ViolationCode.PROTECTION_QUANTITY_MISMATCH,
                    ts_ns=ts_ns,
                    position_id=position_id,
                    client_order_id="",
                    detail=(
                        f"{role.value.lower()}={_decimal_text(total)} "
                        f"position={_decimal_text(quantity)}"
                    ),
                )
