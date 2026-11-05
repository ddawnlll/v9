"""The native evidence stream must not inherit corrected or inferred identities."""

from decimal import Decimal
from types import SimpleNamespace

from v8_next.adapters.native_callback_trace import capture_native_callback


def test_trace_captures_callback_order_and_values_without_backfilling() -> None:
    trace: list[dict[str, object]] = []
    event = SimpleNamespace(
        event_id="event-1", client_order_id="stop-1", position_id=None,
        ts_event=12, ts_init=15, quantity=Decimal("0.50"), reduce_only=False,
    )
    capture_native_callback(trace, "on_order_event", event)
    event.position_id = "P-1"
    capture_native_callback(trace, "on_position_opened", event)
    first, second = trace
    assert first["sequence"] == 0
    assert second["sequence"] == 1
    assert first["callback"] == "on_order_event"
    assert first["fields"] == {
        "event_id": "event-1", "ts_event": 12, "ts_init": 15,
        "client_order_id": "stop-1", "position_id": None,
        "quantity": "0.50", "reduce_only": False,
    }
    assert second["fields"]["position_id"] == "P-1"  # type: ignore[index]
