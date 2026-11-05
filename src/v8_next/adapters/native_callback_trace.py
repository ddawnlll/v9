"""Immutable observations of selected native callback fields, in arrival order.

This is evidence only. Values come directly from the callback, never from the
order cache, local identity maps, or later position snapshots. Missing attributes
are omitted and explicit ``None`` is preserved. String conversion makes native
identifiers and decimal types serializable; ``event_text`` is the native string
representation, not a substitute for a complete native event serialization.
"""

from __future__ import annotations

from typing import Any

_FIELDS = (
    "event_id", "ts_event", "ts_init", "trader_id", "strategy_id", "account_id",
    "instrument_id", "client_order_id", "venue_order_id", "position_id", "trade_id",
    "opening_order_id", "closing_order_id", "order_side", "side", "order_type",
    "quantity", "signed_qty", "last_qty", "last_px", "avg_px_open", "price",
    "trigger_price", "commission", "reason", "reconciliation", "reduce_only",
)


def capture_native_callback(trace: list[dict[str, Any]], callback: str, event: Any) -> None:
    """Append one callback without inferring or editing any of its native fields."""
    fields: dict[str, Any] = {}
    for name in _FIELDS:
        if not hasattr(event, name):
            continue
        value = getattr(event, name)
        fields[name] = value if value is None or type(value) in (bool, int, str) else str(value)
    trace.append(
        {
            "sequence": len(trace),
            "callback": callback,
            "event_type": type(event).__name__,
            "event_text": str(event),
            "fields": fields,
        }
    )
