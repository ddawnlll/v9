"""Regression tests for the sim arm's protection telemetry and audit boundary.

Two defects are pinned here, both found while reconciling the reported
``MISSING_ACK`` violations on the full slice:

1. A buffered protective ACK was dropped when the order's fill arrived before the
   pair could be resolved to a position id. The venue *did* acknowledge; the
   ledger lost the ACK and the invariant checker then reported a missing
   acknowledgement for an order that was acknowledged.
2. The measurement boundary batched a submission with an arbitrarily distant
   resolution, which could hide a genuine one-bar acknowledgement gap. Batching is
   now bounded to a single engine step (equal ``ts_ns``).
"""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

from v8_next.adapters.adaptive_strategy import (
    AdaptiveSimStrategy,
    SimArmConfig,
    _OrderFacts,
)
from v8_next.adapters.protection_invariants import (
    Direction,
    OrderRole,
    ProtectionEvent,
    ProtectionEventKind,
    Side,
)
from v8_next.evaluation.sim_arm import audit_protection_events

D = Decimal
CID = "S-test-S"
PID = "POS-1"


def _entry_snapshot(ts: int = 0) -> ProtectionEvent:
    return ProtectionEvent(
        kind=ProtectionEventKind.POSITION_CHANGED,
        ts_ns=ts,
        position_id=PID,
        direction=Direction.LONG,
        quantity=D("1"),
    )


def _submitted(ts: int) -> ProtectionEvent:
    return ProtectionEvent(
        kind=ProtectionEventKind.ORDER_SUBMITTED,
        ts_ns=ts,
        client_order_id=CID,
        position_id=PID,
        role=OrderRole.STOP,
        side=Side.SELL,
        price=D("95"),
        quantity=D("1"),
        direction=Direction.LONG,
        reduce_only=True,
    )


def _ack(ts: int) -> ProtectionEvent:
    return ProtectionEvent(
        kind=ProtectionEventKind.ACK,
        ts_ns=ts,
        client_order_id=CID,
        position_id=PID,
        role=OrderRole.STOP,
        side=Side.SELL,
        price=D("95"),
        quantity=D("1"),
        direction=Direction.LONG,
        reduce_only=True,
    )


def _fill(ts: int) -> ProtectionEvent:
    return ProtectionEvent(
        kind=ProtectionEventKind.FILL,
        ts_ns=ts,
        client_order_id=CID,
        position_id=PID,
        role=OrderRole.STOP,
        side=Side.SELL,
        price=D("95"),
        quantity=D("1"),
        filled_quantity=D("1"),
        direction=Direction.LONG,
        reduce_only=True,
    )


# --------------------------------------------------------------------------- #
# 1 — a buffered ACK survives a fill that arrives before the pair is resolved
# --------------------------------------------------------------------------- #
def test_buffered_ack_is_not_dropped_when_the_fill_arrives_first() -> None:
    strategy = AdaptiveSimStrategy(SimArmConfig(enabled=True))
    facts = _OrderFacts(
        role=OrderRole.STOP,
        arm_role="STOP",
        decision_id="d-1",
        direction=Direction.LONG,
    )
    # Submission and ACK carry no position id yet (Netting: the entry fill that
    # supplies it has not been observed by the telemetry path), so the pair waits
    # in the pending buffers. The fill then resolves the order first.
    strategy._admit_protection_event(replace(_submitted(100), position_id=""), None, facts)
    strategy._admit_protection_event(replace(_ack(100), position_id=""), None, facts)
    strategy._admit_protection_event(_fill(200), None, facts)

    kinds = [
        (event.kind, event.ts_ns)
        for event in strategy.protection_events
        if event.client_order_id == CID
    ]
    assert kinds == [
        (ProtectionEventKind.ORDER_SUBMITTED, 100),
        (ProtectionEventKind.ACK, 100),
        (ProtectionEventKind.FILL, 200),
    ], "the venue acknowledgement must reach the ledger before its resolution"
    assert not strategy._pending_protective_ack


# --------------------------------------------------------------------------- #
# 2 — the audit boundary reports a cross-step acknowledgement gap
# --------------------------------------------------------------------------- #
def test_audit_batches_one_engine_step_only() -> None:
    same_step = audit_protection_events(
        [_entry_snapshot(), _submitted(300), _ack(300)]
    )
    assert same_step["violations"] == []
    assert same_step["state"] == "PROTECTED"

    cross_step = audit_protection_events(
        [_entry_snapshot(), _submitted(300), _ack(300 + 3_600 * 10**9)]
    )
    codes = [violation["code"] for violation in cross_step["violations"]]
    assert codes == ["MISSING_ACK"], (
        "a submission whose ACK arrives in a later engine step is a real gap and "
        f"must not be batched away (got {codes})"
    )


def test_audit_reports_an_order_that_never_resolves() -> None:
    verdict = audit_protection_events([_entry_snapshot(), _submitted(300)])
    assert [violation["code"] for violation in verdict["violations"]] == ["MISSING_ACK"]
