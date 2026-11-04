"""Swing engine callback state transitions that do not require a tape run."""

from types import SimpleNamespace

from v8_next.adapters.swing_engine import SwingEngineEvents, SwingEngineStrategy


def _strategy(plan: dict) -> SwingEngineStrategy:
    strategy = SwingEngineStrategy.__new__(SwingEngineStrategy)
    strategy.events = SwingEngineEvents()
    strategy.plan = plan
    return strategy


def test_denied_entry_clears_the_plan_and_allows_a_later_signal() -> None:
    strategy = _strategy(
        {
            "status": "ENTRY_SUBMITTED",
            "entry_client_order_id": "O-ENTRY",
            "direction": "LONG",
        }
    )

    strategy.on_order_denied(
        SimpleNamespace(client_order_id="O-ENTRY", reason="margin", ts_event=123)
    )

    assert strategy.plan is None
    assert strategy.events.order_events[-1]["event_type"] == "OrderDenied"
    assert strategy.events.decisions[-1]["status"] == "ENTRY_DENIED"
    assert strategy.events.decisions[-1]["failure_reason"] == "margin"
    assert strategy.events.decisions[-1]["failure_ns"] == 123


def test_rejected_entry_clears_the_plan() -> None:
    strategy = _strategy(
        {
            "status": "ENTRY_SUBMITTED",
            "entry_client_order_id": "O-ENTRY",
            "direction": "SHORT",
        }
    )

    strategy.on_order_rejected(
        SimpleNamespace(client_order_id="O-ENTRY", reason="price", ts_event=456)
    )

    assert strategy.plan is None
    assert strategy.events.decisions[-1]["status"] == "ENTRY_REJECTED"


def test_protection_failure_does_not_discard_an_open_position_plan() -> None:
    plan = {
        "status": "POSITION_OPEN",
        "entry_client_order_id": "O-ENTRY",
        "direction": "LONG",
    }
    strategy = _strategy(plan)

    strategy.on_order_rejected(
        SimpleNamespace(client_order_id="O-STOP", reason="reduce-only", ts_event=789)
    )

    assert strategy.plan is plan
    assert strategy.events.order_events[-1]["event_type"] == "OrderRejected"
    assert strategy.events.decisions == []
