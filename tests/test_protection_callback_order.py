"""Regression for buffered callbacks across one-bar position lifetimes."""
from decimal import Decimal
from types import SimpleNamespace

from v8_next.adapters.adaptive_strategy import AdaptiveSimStrategy, SimArmConfig, _OrderFacts
from v8_next.adapters.expert_strategy import ExpertEnsembleStrategy
from v8_next.adapters.protection_invariants import (
    Direction,
    OrderRole,
    ProtectionEvent,
    ProtectionEventKind,
    Side,
)


def _stop_event(kind: ProtectionEventKind, ts: int) -> ProtectionEvent:
    return ProtectionEvent(kind=kind, ts_ns=ts, client_order_id="S", role=OrderRole.STOP,
                           quantity=Decimal("0.009"), side=Side.SELL,
                           price=Decimal("95"), reduce_only=True)


def test_ack_joins_only_exact_current_entry_decision() -> None:
    strategy = AdaptiveSimStrategy(SimArmConfig(enabled=True))
    facts = _OrderFacts(OrderRole.STOP, "STOP", "entry-decision", Direction.LONG)
    strategy._position_decision["native-P"] = "entry-decision"
    strategy._seen_open_positions.add("native-P")
    strategy._admit_protection_event(_stop_event(ProtectionEventKind.ORDER_SUBMITTED, 1), None, facts)
    strategy._admit_protection_event(_stop_event(ProtectionEventKind.ACK, 2), None, facts)
    assert [(e.kind, e.position_id) for e in strategy.protection_events] == [
        (ProtectionEventKind.ORDER_SUBMITTED, "native-P"),
        (ProtectionEventKind.ACK, "native-P"),
    ]


def test_position_open_snapshot_is_published_before_close(monkeypatch: object) -> None:
    from pytest import MonkeyPatch
    assert isinstance(monkeypatch, MonkeyPatch)
    monkeypatch.setattr(ExpertEnsembleStrategy, "on_position_opened", lambda self, event: None)
    strategy = AdaptiveSimStrategy(SimArmConfig(enabled=True))
    event = SimpleNamespace(position_id="P", ts_event=1, quantity=Decimal("0.009"), side="LONG")
    strategy.on_position_opened(event)
    assert len(strategy.protection_events) == 1
    snapshot = strategy.protection_events[0]
    assert (snapshot.ts_ns, snapshot.position_id, snapshot.quantity, snapshot.direction) == (
        1, "P", Decimal("0.009"), Direction.LONG,
    )


def test_unresolved_ack_is_retained_before_terminal_event() -> None:
    strategy = AdaptiveSimStrategy(SimArmConfig(enabled=True))
    facts = _OrderFacts(OrderRole.STOP, "STOP", "unmatched-decision", Direction.LONG)
    strategy._position_decision["native-P"] = "different-decision"
    strategy._seen_open_positions.add("native-P")
    strategy._admit_protection_event(_stop_event(ProtectionEventKind.ORDER_SUBMITTED, 1), None, facts)
    strategy._admit_protection_event(_stop_event(ProtectionEventKind.ACK, 2), None, facts)
    assert not strategy.protection_events
    terminal = ProtectionEvent(
        kind=ProtectionEventKind.FILL, ts_ns=3, client_order_id="S", position_id="native-P",
        role=OrderRole.STOP, quantity=Decimal("0.009"), filled_quantity=Decimal("0.009"),
        side=Side.SELL, price=Decimal("95"), reduce_only=True,
    )
    strategy._admit_protection_event(terminal, None, facts)
    assert [event.kind for event in strategy.protection_events] == [
        ProtectionEventKind.ORDER_SUBMITTED, ProtectionEventKind.ACK, ProtectionEventKind.FILL,
    ]
    # A later fill must not retroactively authenticate the preceding ACK identity.
    assert strategy.protection_events[1].position_id == ""


def test_unrelated_position_open_cannot_claim_buffered_ack() -> None:
    strategy = AdaptiveSimStrategy(SimArmConfig(enabled=True))
    facts = _OrderFacts(OrderRole.STOP, "STOP", "missing-decision", Direction.LONG)
    strategy._order_facts["S"] = facts
    strategy._admit_protection_event(_stop_event(ProtectionEventKind.ORDER_SUBMITTED, 1), None, facts)
    strategy._admit_protection_event(_stop_event(ProtectionEventKind.ACK, 2), None, facts)
    strategy._position_decision["unrelated-P"] = "other-decision"
    strategy._seen_open_positions.add("unrelated-P")
    strategy._flush_pending_children("unrelated-P")
    assert strategy.protection_events == []
    assert "S" in strategy._pending_protective_ack
