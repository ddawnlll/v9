"""Audit callback chronology and identity without a native replay."""

from decimal import Decimal

from v8_next.adapters.protection_invariants import (
    Direction,
    OrderRole,
    ProtectionEvent,
    ProtectionEventKind,
    ProtectionState,
    Side,
    ViolationCode,
)
from v8_next.evaluation.sim_arm import _audit_protection


def _event(kind: ProtectionEventKind, ts: int, **kwargs: object) -> ProtectionEvent:
    return ProtectionEvent(kind=kind, ts_ns=ts, **kwargs)  # type: ignore[arg-type]


def test_submission_resolution_does_not_hide_interleaved_position_conflict() -> None:
    events = [
        _event(ProtectionEventKind.POSITION_CHANGED, 1, position_id="P", quantity=Decimal("1"), direction=Direction.LONG),
        _event(ProtectionEventKind.ORDER_SUBMITTED, 2, client_order_id="S", position_id="P", role=OrderRole.STOP, quantity=Decimal("1"), side=Side.SELL, price=Decimal("95"), reduce_only=True),
        _event(ProtectionEventKind.ORDER_SUBMITTED, 3, client_order_id="E", position_id="P", role=OrderRole.ENTRY, quantity=Decimal("1")),
        _event(ProtectionEventKind.FILL, 4, client_order_id="E", position_id="P", role=OrderRole.ENTRY, quantity=Decimal("1"), filled_quantity=Decimal("1")),
        _event(ProtectionEventKind.POSITION_CHANGED, 5, position_id="P", quantity=Decimal("1"), direction=Direction.LONG),
        _event(ProtectionEventKind.ACK, 6, client_order_id="S", position_id="P", role=OrderRole.STOP, quantity=Decimal("1"), side=Side.SELL, price=Decimal("95"), reduce_only=True),
    ]
    checker, transitions = _audit_protection(events)
    codes = {item.code for item in checker.violations}
    assert ViolationCode.MISSING_ACK in codes
    assert ViolationCode.POSITION_QUANTITY_CONFLICT in codes
    assert any(item["state"] == ProtectionState.GAP.value for item in transitions)
    assert checker.state is ProtectionState.UNKNOWN


def test_ack_for_another_position_cannot_confirm_stop() -> None:
    events = [
        _event(ProtectionEventKind.POSITION_CHANGED, 1, position_id="P", quantity=Decimal("1"), direction=Direction.LONG),
        _event(ProtectionEventKind.ORDER_SUBMITTED, 2, client_order_id="S", position_id="P", role=OrderRole.STOP, quantity=Decimal("1"), side=Side.SELL, price=Decimal("95"), reduce_only=True),
        _event(ProtectionEventKind.ACK, 3, client_order_id="S", position_id="OTHER", role=OrderRole.STOP, quantity=Decimal("1"), side=Side.SELL, price=Decimal("95"), reduce_only=True),
    ]
    checker, _ = _audit_protection(events)
    assert checker.state_of("P") is ProtectionState.UNKNOWN
    assert any(v.code is ViolationCode.ORDER_IDENTITY_CONFLICT for v in checker.violations)
