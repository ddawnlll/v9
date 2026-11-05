"""M0-05 — protection invariants over the native event log.

These tests exercise ``v8_next.adapters.protection_invariants`` only: it is a pure
reducer fed typed events. Nothing here constructs, submits or cancels an order; the
native engine remains the single execution authority.
"""

from __future__ import annotations

import ast
from decimal import Decimal
from pathlib import Path

import pytest

from v8_next.adapters.protection_invariants import (
    ZERO,
    BracketInvariantChecker,
    Direction,
    OrderRole,
    ProtectionEvent,
    ProtectionEventKind,
    ProtectionState,
    Side,
    ViolationCode,
)

D = Decimal


def event(kind: ProtectionEventKind, ts: int, **fields: object) -> ProtectionEvent:
    return ProtectionEvent(kind=kind, ts_ns=ts, **fields)  # type: ignore[arg-type]


def submitted(
    cid: str,
    position_id: str,
    role: OrderRole,
    ts: int,
    qty: str,
    *,
    side: Side | None = None,
    price: str | None = None,
    reduce_only: bool = True,
    direction: Direction | None = None,
) -> ProtectionEvent:
    return event(
        ProtectionEventKind.ORDER_SUBMITTED,
        ts,
        client_order_id=cid,
        position_id=position_id,
        role=role,
        side=side,
        price=D(price) if price is not None else None,
        quantity=D(qty),
        reduce_only=reduce_only,
        direction=direction,
    )


def ack(
    cid: str,
    position_id: str,
    role: OrderRole,
    ts: int,
    qty: str,
    *,
    side: Side | None = None,
    price: str | None = None,
    reduce_only: bool = True,
    direction: Direction | None = None,
) -> ProtectionEvent:
    return event(
        ProtectionEventKind.ACK,
        ts,
        client_order_id=cid,
        position_id=position_id,
        role=role,
        side=side,
        price=D(price) if price is not None else None,
        quantity=D(qty),
        reduce_only=reduce_only,
        direction=direction,
    )


def fill(
    cid: str,
    position_id: str,
    role: OrderRole,
    ts: int,
    cumulative: str,
    *,
    order_qty: str | None = None,
    side: Side | None = None,
    price: str | None = None,
    reduce_only: bool = False,
    direction: Direction | None = None,
    kind: ProtectionEventKind = ProtectionEventKind.FILL,
) -> ProtectionEvent:
    return event(
        kind,
        ts,
        client_order_id=cid,
        position_id=position_id,
        role=role,
        side=side,
        price=D(price) if price is not None else None,
        quantity=D(order_qty) if order_qty is not None else D(cumulative),
        filled_quantity=D(cumulative),
        reduce_only=reduce_only,
        direction=direction,
    )


def position_changed(
    position_id: str, ts: int, qty: str, *, direction: Direction | None = None
) -> ProtectionEvent:
    return event(
        ProtectionEventKind.POSITION_CHANGED,
        ts,
        position_id=position_id,
        quantity=D(qty),
        direction=direction,
    )


def cancel_request(cid: str, position_id: str, role: OrderRole, ts: int) -> ProtectionEvent:
    return event(
        ProtectionEventKind.CANCEL_REQUEST,
        ts,
        client_order_id=cid,
        position_id=position_id,
        role=role,
    )


def canceled(cid: str, position_id: str, role: OrderRole, ts: int) -> ProtectionEvent:
    return event(
        ProtectionEventKind.CANCELED,
        ts,
        client_order_id=cid,
        position_id=position_id,
        role=role,
    )


def rejected(
    cid: str,
    position_id: str,
    role: OrderRole,
    ts: int,
    *,
    reason: str = "rejected",
    cancel_reject: bool = False,
) -> ProtectionEvent:
    return event(
        ProtectionEventKind.REJECTED,
        ts,
        client_order_id=cid,
        position_id=position_id,
        role=role,
        reason=reason,
        cancel_reject=cancel_reject,
    )


def _entry_log(position_id: str = "P-1", qty: str = "1") -> list[ProtectionEvent]:
    """A filled long entry order of ``qty`` on ``position_id``."""
    return [
        submitted(
            "E-1",
            position_id,
            OrderRole.ENTRY,
            1,
            qty,
            side=Side.BUY,
            reduce_only=False,
            direction=Direction.LONG,
        ),
        ack(
            "E-1",
            position_id,
            OrderRole.ENTRY,
            2,
            qty,
            side=Side.BUY,
            price="100",
            reduce_only=False,
            direction=Direction.LONG,
        ),
        fill(
            "E-1",
            position_id,
            OrderRole.ENTRY,
            3,
            qty,
            order_qty=qty,
            side=Side.BUY,
            price="100",
            direction=Direction.LONG,
        ),
    ]


def _protected_long_bracket() -> tuple[BracketInvariantChecker, list[ProtectionEvent]]:
    """Open long 1 with an acknowledged stop at 95 and target at 105."""
    log = _entry_log()
    log += [
        submitted("S-1", "P-1", OrderRole.STOP, 4, "1", side=Side.SELL, price="95"),
        ack("S-1", "P-1", OrderRole.STOP, 5, "1", side=Side.SELL, price="95"),
        submitted("T-1", "P-1", OrderRole.TARGET, 6, "1", side=Side.SELL, price="105"),
        ack("T-1", "P-1", OrderRole.TARGET, 7, "1", side=Side.SELL, price="105"),
    ]
    checker = BracketInvariantChecker()
    checker.apply_all(log)
    return checker, log


def _partial_fill_log() -> list[ProtectionEvent]:
    """Entry 2 units, 0.5 filled, stop acknowledged for exactly the filled 0.5."""
    log = [
        submitted(
            "E-1",
            "P-1",
            OrderRole.ENTRY,
            1,
            "2",
            side=Side.BUY,
            reduce_only=False,
            direction=Direction.LONG,
        ),
        ack(
            "E-1",
            "P-1",
            OrderRole.ENTRY,
            2,
            "2",
            side=Side.BUY,
            price="100",
            reduce_only=False,
            direction=Direction.LONG,
        ),
        fill(
            "E-1",
            "P-1",
            OrderRole.ENTRY,
            3,
            "0.5",
            order_qty="2",
            side=Side.BUY,
            price="100",
            direction=Direction.LONG,
            kind=ProtectionEventKind.PARTIAL_FILL,
        ),
        submitted("S-1", "P-1", OrderRole.STOP, 4, "0.5", side=Side.SELL, price="95"),
        ack("S-1", "P-1", OrderRole.STOP, 5, "0.5", side=Side.SELL, price="95"),
    ]
    return log


# --------------------------------------------------------------------------- #
# 1 — partial fills resize protection exactly, step by step
# --------------------------------------------------------------------------- #
def test_partial_fill_resizes_the_protective_quantity_exactly() -> None:
    checker = BracketInvariantChecker()
    checker.apply_all(_partial_fill_log())
    assert checker.position_quantity("P-1") == D("0.5")
    assert checker.protective_quantity("P-1") == D("0.5")
    assert checker.state is ProtectionState.PROTECTED

    checker.apply(
        fill(
            "E-1",
            "P-1",
            OrderRole.ENTRY,
            6,
            "1.0",
            order_qty="2",
            side=Side.BUY,
            price="100",
            direction=Direction.LONG,
            kind=ProtectionEventKind.PARTIAL_FILL,
        )
    )
    assert checker.position_quantity("P-1") == D("1.0")
    assert checker.protective_quantity("P-1") == D("0.5")
    assert checker.state is ProtectionState.PARTIAL
    assert any(
        violation.code is ViolationCode.PROTECTION_QUANTITY_MISMATCH
        and "stop=0.5 position=1" in violation.detail
        for violation in checker.violations
    )

    checker.apply(ack("S-1", "P-1", OrderRole.STOP, 7, "1.0", side=Side.SELL, price="95"))
    assert checker.position_quantity("P-1") == D("1.0")
    assert checker.protective_quantity("P-1") == D("1.0")
    assert checker.state is ProtectionState.PROTECTED

    checker.apply(
        fill(
            "E-1",
            "P-1",
            OrderRole.ENTRY,
            8,
            "2.0",
            order_qty="2",
            side=Side.BUY,
            price="100",
            direction=Direction.LONG,
        )
    )
    assert checker.position_quantity("P-1") == D("2.0")
    assert checker.protective_quantity("P-1") == D("1.0")
    assert checker.state is ProtectionState.PARTIAL
    assert any(
        violation.code is ViolationCode.PROTECTION_QUANTITY_MISMATCH
        and "stop=1 position=2" in violation.detail
        for violation in checker.violations
    )

    checker.apply(ack("S-1", "P-1", OrderRole.STOP, 9, "2.0", side=Side.SELL, price="95"))
    assert checker.position_quantity("P-1") == D("2.0")
    assert checker.protective_quantity("P-1") == D("2.0")
    assert checker.state is ProtectionState.PROTECTED


# --------------------------------------------------------------------------- #
# 2 — reduce-only may never reverse
# --------------------------------------------------------------------------- #
def test_reduce_only_over_quantity_is_a_violation_and_never_a_reversal() -> None:
    checker = BracketInvariantChecker()
    checker.apply_all(_entry_log(qty="1"))
    checker.apply(submitted("S-1", "P-1", OrderRole.STOP, 4, "2", side=Side.SELL, price="90"))
    checker.apply(ack("S-1", "P-1", OrderRole.STOP, 5, "2", side=Side.SELL, price="90"))

    assert checker.state is ProtectionState.UNKNOWN
    assert any(
        violation.code is ViolationCode.REDUCE_ONLY_OVER_QUANTITY
        and "remaining=2 position=1" in violation.detail
        for violation in checker.violations
    )

    checker.apply(
        fill("S-1", "P-1", OrderRole.STOP, 6, "2", order_qty="2", side=Side.SELL, price="90")
    )
    assert checker.position_quantity("P-1") == ZERO
    assert checker.position_quantity("P-1") >= ZERO
    assert checker.state is ProtectionState.UNPROTECTED
    assert any(
        violation.code is ViolationCode.REDUCE_ONLY_REVERSAL_BLOCKED
        for violation in checker.violations
    )


# --------------------------------------------------------------------------- #
# 3 — target/stop race in both orders: deterministic, never reversed
# --------------------------------------------------------------------------- #
def _race(first: OrderRole) -> BracketInvariantChecker:
    checker, _ = _protected_long_bracket()
    stop_fill = fill(
        "S-1", "P-1", OrderRole.STOP, 10, "1", order_qty="1", side=Side.SELL, price="95"
    )
    target_fill = fill(
        "T-1", "P-1", OrderRole.TARGET, 11, "1", order_qty="1", side=Side.SELL, price="105"
    )
    if first is OrderRole.STOP:
        checker.apply(stop_fill)
        checker.apply(target_fill)
    else:
        checker.apply(target_fill)
        checker.apply(stop_fill)
    return checker


@pytest.mark.parametrize("first", [OrderRole.TARGET, OrderRole.STOP])
def test_bracket_leg_race_is_deterministic_and_never_reverses(first: OrderRole) -> None:
    once = _race(first)
    twice = _race(first)
    assert once.digest() == twice.digest(), "same event sequence must replay identically"
    assert once.state is ProtectionState.UNPROTECTED
    assert once.position_quantity("P-1") == ZERO
    assert once.target_quantity("P-1") == ZERO

    after_flat = [
        violation
        for violation in once.violations
        if violation.code is ViolationCode.PROTECTIVE_FILL_AFTER_FLAT
    ]
    assert len(after_flat) == 1, "only the late leg is named; the closing leg is legitimate"
    assert after_flat[0].client_order_id == (
        "T-1" if first is OrderRole.STOP else "S-1"
    )


def test_both_race_orders_preserve_the_same_safety_invariant() -> None:
    target_first = _race(OrderRole.TARGET)
    stop_first = _race(OrderRole.STOP)
    for checker in (target_first, stop_first):
        assert checker.position_quantity("P-1") == ZERO
        assert checker.state is ProtectionState.UNPROTECTED
        assert checker.snapshot()["positions"]["P-1"]["quantity"] == "0"


# --------------------------------------------------------------------------- #
# 4 — a rejected cancel never removes protection
# --------------------------------------------------------------------------- #
def test_cancel_reject_leaves_protection_intact() -> None:
    checker, _ = _protected_long_bracket()
    checker.apply(cancel_request("S-1", "P-1", OrderRole.STOP, 20))
    assert checker.state is ProtectionState.PROTECTED, "a request does not cancel protection"

    checker.apply(
        rejected(
            "S-1",
            "P-1",
            OrderRole.STOP,
            21,
            reason="cancel_already_pending",
            cancel_reject=True,
        )
    )
    assert checker.state is ProtectionState.PROTECTED
    assert checker.protective_quantity("P-1") == D("1")
    assert any(
        violation.code is ViolationCode.CANCEL_REJECT_ORDER_STILL_LIVE
        for violation in checker.violations
    )
    assert not any(
        violation.code is ViolationCode.PROTECTIVE_ORDER_REJECTED
        for violation in checker.violations
    )


def test_order_rejection_poisons_protected_state() -> None:
    checker, _ = _protected_long_bracket()
    checker.apply(rejected("S-1", "P-1", OrderRole.STOP, 21, reason="reduce_only_unsupported"))
    assert checker.state is ProtectionState.REJECTED
    assert any(
        violation.code is ViolationCode.PROTECTIVE_ORDER_REJECTED
        for violation in checker.violations
    )


# --------------------------------------------------------------------------- #
# 5 — restart/replay, gaps and unknown references
# --------------------------------------------------------------------------- #
def test_replay_is_deterministic_and_idempotent() -> None:
    first = BracketInvariantChecker()
    first.apply_all(_partial_fill_log())
    second = BracketInvariantChecker()
    second.apply_all(_partial_fill_log())

    assert first.digest() == second.digest()
    assert first.violations == second.violations
    assert first.events_applied == second.events_applied == len(_partial_fill_log())

    first.apply_all(_partial_fill_log())
    assert first.digest() == second.digest(), "re-applying the journal is a no-op"
    assert first.violations == second.violations
    assert first.events_applied == len(_partial_fill_log())


def test_missing_ack_is_a_gap_never_protected() -> None:
    checker = BracketInvariantChecker()
    checker.apply_all(_entry_log(qty="1"))
    checker.apply(submitted("S-1", "P-1", OrderRole.STOP, 4, "1", side=Side.SELL, price="95"))

    assert checker.state is ProtectionState.GAP
    assert checker.protective_quantity("P-1") == ZERO
    assert any(
        violation.code is ViolationCode.MISSING_ACK for violation in checker.violations
    )


def test_unknown_order_reference_cannot_report_protected() -> None:
    checker, _ = _protected_long_bracket()
    checker.apply(canceled("S-GHOST", "P-1", OrderRole.STOP, 30))
    assert checker.state is ProtectionState.UNKNOWN
    assert any(
        violation.code is ViolationCode.UNKNOWN_ORDER_REFERENCE
        for violation in checker.violations
    )


def test_late_backwards_fill_is_named_and_never_applied() -> None:
    checker = BracketInvariantChecker()
    checker.apply_all(_entry_log(qty="0.9"))
    checker.apply(
        fill(
            "E-1",
            "P-1",
            OrderRole.ENTRY,
            40,
            "0.4",
            order_qty="2",
            side=Side.BUY,
            price="100",
            direction=Direction.LONG,
            kind=ProtectionEventKind.PARTIAL_FILL,
        )
    )
    assert checker.position_quantity("P-1") == D("0.9")
    assert any(
        violation.code is ViolationCode.STALE_FILL_IGNORED
        for violation in checker.violations
    )


# --------------------------------------------------------------------------- #
# 6 — a new native position id never inherits stale protection
# --------------------------------------------------------------------------- #
def test_new_position_id_cannot_inherit_stale_protection() -> None:
    checker = BracketInvariantChecker()
    checker.apply(position_changed("P-1", 1, "1", direction=Direction.LONG))
    checker.apply(submitted("S-1", "P-1", OrderRole.STOP, 2, "1", side=Side.SELL, price="95"))
    checker.apply(ack("S-1", "P-1", OrderRole.STOP, 3, "1", side=Side.SELL, price="95"))
    assert checker.state_of("P-1") is ProtectionState.PROTECTED

    checker.apply(position_changed("P-1", 4, "0", direction=Direction.LONG))
    assert checker.state_of("P-1") is ProtectionState.UNPROTECTED
    assert any(
        violation.code is ViolationCode.STALE_PROTECTION_ORDER
        for violation in checker.violations
    )

    checker.apply(position_changed("P-2", 5, "1", direction=Direction.SHORT))
    assert checker.state is ProtectionState.UNPROTECTED
    assert checker.state_of("P-2") is ProtectionState.UNPROTECTED
    assert checker.protective_quantity("P-2") == ZERO, "P-1's stop must not protect P-2"

    checker.apply(submitted("S-2", "P-2", OrderRole.STOP, 6, "1", side=Side.BUY, price="105"))
    checker.apply(ack("S-2", "P-2", OrderRole.STOP, 7, "1", side=Side.BUY, price="105"))
    assert checker.state_of("P-2") is ProtectionState.PROTECTED
    assert checker.state_of("P-1") is ProtectionState.UNPROTECTED
    assert checker.protective_quantity("P-1") == ZERO, "the leaked leg was retired as stale"


def test_reopening_the_same_position_id_cannot_inherit_a_leaked_leg() -> None:
    checker = BracketInvariantChecker()
    checker.apply(position_changed("P-1", 1, "1", direction=Direction.LONG))
    checker.apply(submitted("S-1", "P-1", OrderRole.STOP, 2, "1", side=Side.SELL, price="95"))
    checker.apply(ack("S-1", "P-1", OrderRole.STOP, 3, "1", side=Side.SELL, price="95"))
    checker.apply(position_changed("P-1", 4, "0", direction=Direction.LONG))
    checker.apply(position_changed("P-1", 5, "1", direction=Direction.LONG))

    assert checker.state is ProtectionState.UNPROTECTED
    assert checker.protective_quantity("P-1") == ZERO
    assert checker.order_remaining("S-1") == D("1"), "the leaked order is still out there"

    checker.apply(
        fill("S-1", "P-1", OrderRole.STOP, 6, "1", order_qty="1", side=Side.SELL, price="95")
    )
    assert checker.position_quantity("P-1") == D("1"), "a leaked leg cannot touch the new position"
    assert any(
        violation.code is ViolationCode.STALE_FILL_IGNORED
        for violation in checker.violations
    )

    checker.apply(submitted("S-2", "P-1", OrderRole.STOP, 7, "1", side=Side.SELL, price="95"))
    checker.apply(ack("S-2", "P-1", OrderRole.STOP, 8, "1", side=Side.SELL, price="95"))
    assert checker.state is ProtectionState.PROTECTED


# --------------------------------------------------------------------------- #
# 7 — direction consistency
# --------------------------------------------------------------------------- #
def test_inverted_stop_and_target_are_unknown_not_protected() -> None:
    checker = BracketInvariantChecker()
    checker.apply(position_changed("P-1", 1, "1", direction=Direction.LONG))
    checker.apply(submitted("S-1", "P-1", OrderRole.STOP, 2, "1", side=Side.SELL, price="105"))
    checker.apply(ack("S-1", "P-1", OrderRole.STOP, 3, "1", side=Side.SELL, price="105"))
    checker.apply(submitted("T-1", "P-1", OrderRole.TARGET, 4, "1", side=Side.SELL, price="95"))
    checker.apply(ack("T-1", "P-1", OrderRole.TARGET, 5, "1", side=Side.SELL, price="95"))

    assert checker.state is ProtectionState.UNKNOWN
    assert any(
        violation.code is ViolationCode.BRACKET_GEOMETRY_INCONSISTENT
        for violation in checker.violations
    )


def test_protective_side_must_oppose_the_position() -> None:
    checker = BracketInvariantChecker()
    checker.apply(position_changed("P-1", 1, "1", direction=Direction.LONG))
    checker.apply(submitted("S-1", "P-1", OrderRole.STOP, 2, "1", side=Side.BUY, price="95"))
    checker.apply(ack("S-1", "P-1", OrderRole.STOP, 3, "1", side=Side.BUY, price="95"))

    assert checker.state is ProtectionState.UNKNOWN
    assert any(
        violation.code is ViolationCode.PROTECTIVE_SIDE_INCONSISTENT
        for violation in checker.violations
    )


# --------------------------------------------------------------------------- #
# 8 — unreadable input fails closed at construction
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "fields",
    [
        {"kind": "NOT_A_KIND", "client_order_id": "X"},
        {
            "kind": ProtectionEventKind.FILL,
            "client_order_id": "X",
            "position_id": "P-1",
            "filled_quantity": 0,
        },
        {"kind": ProtectionEventKind.ORDER_SUBMITTED, "client_order_id": "X", "quantity": "-1"},
        {"kind": ProtectionEventKind.ORDER_SUBMITTED, "client_order_id": "X", "quantity": 1.5},
        {"kind": ProtectionEventKind.POSITION_CHANGED},
    ],
)
def test_unreadable_events_are_refused(fields: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        ProtectionEvent(ts_ns=1, **fields)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# 9 — the checker holds no execution authority
# --------------------------------------------------------------------------- #
def test_module_is_native_agnostic_and_has_no_order_authority() -> None:
    source_path = (
        Path(__file__).resolve().parents[1]
        / "src/v8_next/adapters/protection_invariants.py"
    )
    source = source_path.read_text()
    tree = ast.parse(source)
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            imported.append(node.module)
    assert not any("nautilus" in module for module in imported)
    for forbidden in ("submit_order", "cancel_order", "submit_order_list", "order_factory"):
        assert forbidden not in source


# --------------------------------------------------------------------------- #
# 10 — engine-step batches do not publish transient missing ACKs
# --------------------------------------------------------------------------- #
def test_batch_does_not_record_transient_missing_ack() -> None:
    """A submission and its ACK in one batch is not a genuine protection gap."""
    entry = _entry_log()
    stop = [
        submitted("S-1", "P-1", OrderRole.STOP, 4, "1", side=Side.SELL, price="95"),
        ack("S-1", "P-1", OrderRole.STOP, 5, "1", side=Side.SELL, price="95"),
    ]
    target = [
        submitted("T-1", "P-1", OrderRole.TARGET, 6, "1", side=Side.SELL, price="105"),
        ack("T-1", "P-1", OrderRole.TARGET, 7, "1", side=Side.SELL, price="105"),
    ]
    batched = BracketInvariantChecker()
    batched.apply_all(entry)
    batched.apply_batch(stop)
    batched.apply_batch(target)
    assert batched.state is ProtectionState.PROTECTED
    assert not any(
        violation.code is ViolationCode.MISSING_ACK for violation in batched.violations
    )

    per_event = BracketInvariantChecker()
    per_event.apply_all(entry + stop + target)
    assert any(
        violation.code is ViolationCode.MISSING_ACK for violation in per_event.violations
    ), "per-event semantics must remain unchanged"


def test_a_genuine_unacknowledged_submission_still_fails_in_a_batch() -> None:
    checker = BracketInvariantChecker()
    checker.apply_all(_entry_log())
    checker.apply_batch(
        [submitted("S-1", "P-1", OrderRole.STOP, 4, "1", side=Side.SELL, price="95")]
    )
    assert checker.state is ProtectionState.GAP
    assert any(
        violation.code is ViolationCode.MISSING_ACK for violation in checker.violations
    )


def test_a_fill_is_accepted_as_implicit_acknowledgement() -> None:
    """An order can fill before an observable ACK; the fill is venue proof."""
    entry = _entry_log()
    stop = [
        submitted("S-1", "P-1", OrderRole.STOP, 4, "1", side=Side.SELL, price="95"),
        fill("S-1", "P-1", OrderRole.STOP, 5, "1", side=Side.SELL, price="95", reduce_only=True),
    ]
    target = [
        submitted("T-1", "P-1", OrderRole.TARGET, 6, "1", side=Side.SELL, price="105"),
        ack("T-1", "P-1", OrderRole.TARGET, 7, "1", side=Side.SELL, price="105"),
    ]
    checker = BracketInvariantChecker()
    checker.apply_all(entry)
    checker.apply_batch(stop)
    checker.apply_batch(target)
    assert not any(
        violation.code is ViolationCode.MISSING_ACK for violation in checker.violations
    )
