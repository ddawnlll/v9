"""Economic projection of native state for replay reconciliation, not a ledger."""

from typing import Any

from nautilus_trader.backtest import BacktestEngine
from nautilus_trader.model import Currency, Venue


def economic_state(engine: BacktestEngine, venue: Venue, currency: Currency) -> dict[str, Any]:
    account = engine.cache.account_for_venue(venue)
    if account is None:
        raise ValueError("native account missing")
    orders = []
    for order in engine.cache.orders():
        orders.append(
            {
                "client_order_id": str(order.client_order_id),
                "instrument_id": str(order.instrument_id),
                "side": str(order.side),
                "status": str(order.status),
                "quantity": str(order.quantity),
                "filled_qty": str(order.filled_qty),
                "average_price": str(order.avg_px) if order.avg_px is not None else None,
            }
        )
    positions = []
    for position in engine.cache.positions():
        positions.append(
            {
                "instrument_id": str(position.instrument_id),
                "side": str(position.side),
                "quantity": str(position.quantity),
                "opened_ns": position.ts_opened,
                "closed_ns": position.ts_closed,
                "is_closed": position.is_closed,
                "average_open_price": str(position.avg_px_open),
                "average_close_price": (
                    str(position.avg_px_close) if position.avg_px_close is not None else None
                ),
                "peak_quantity": str(position.peak_qty),
                "realized_pnl": str(position.realized_pnl),
                "commissions": sorted(str(c) for c in position.commissions()),
                # Engine-generated event UUIDs and the funding reason's UUID suffix
                # vary between replay instances. Compare economic identity instead.
                "adjustments": [
                    {
                        key: value
                        for key, value in a.to_dict().items()
                        if key not in {"event_id", "reason"}
                    }
                    for a in position.adjustments()
                ],
            }
        )
    return {
        "claim_status": "NO_ECONOMIC_CLAIM",
        "realization": "SIMULATED",
        "currency": str(currency),
        "balance_total": str(account.balance_total(currency)),
        "orders": sorted(orders, key=lambda item: item["client_order_id"]),
        "positions": sorted(positions, key=lambda item: (item["instrument_id"], item["side"])),
    }


def reconcile_replay(expected: dict[str, Any], recovered: dict[str, Any]) -> None:
    if expected != recovered:
        raise ValueError("native replay diverged; further execution prohibited")
