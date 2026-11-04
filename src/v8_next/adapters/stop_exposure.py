"""Read native protective-order coverage; never maintain a second position ledger."""

from decimal import Decimal
from typing import Any

from nautilus_trader.model import OrderSide, PositionSide, StopMarketOrder

from v8_next.domain.campaign import PaperCampaign
from v8_next.risk.sizing import StopExposure


def native_stop_exposure(
    cache: Any,
    campaigns: tuple[PaperCampaign, ...],
    *,
    pending_campaigns: bool,
    observed_ns: int,
    pending_ids: frozenset[str] = frozenset(),
) -> StopExposure | None:
    """Return absence for unpriced reservations or incomplete native protection.

    Current scope is native netting positions with campaign-owned stop-market
    orders. Nominal absolute entry-to-stop risk matches the legacy heat rule;
    no profit offset, gap guarantee or funding reconciliation is implied.

    Reservation bases (deliberate, document the transition):
    - Pending (unsubmitted, fill price unknown): worst-case band
      qty*|target-stop|. Fill can occur anywhere inside the permitted band,
      so the reservation assumes the far edge.
    - Open (entry known): realized distance qty*|entry-stop|.
    On fill the booked heat therefore drops from band to distance. That is a
    genuine risk reduction (entry realized inside the band), not freed
    headroom from nowhere; consumers must expect the step, not treat it as
    spare limit. allocation.allocate_ordered books the pending (band) basis
    for in-batch reservations to match this function.
    """
    if cache.orders_inflight():
        return None  # Submitted entries/amendments have no qualified reservation bound yet.
    if pending_campaigns and not pending_ids:
        return None
    if pending_ids and not pending_campaigns:
        raise ValueError("inconsistent pending campaign declaration")
    owners = {c.campaign_id: c for c in campaigns}
    if len(owners) != len(campaigns):
        raise ValueError("duplicate campaign ownership")
    orders = {str(o.client_order_id): o for o in cache.orders_open()}
    recognized = set()
    total = Decimal(0)
    if not pending_ids <= owners.keys():
        raise ValueError("unknown pending campaign")
    known_order_ids = {str(order_id) for order_id in cache.client_order_ids()} if pending_ids else set()
    for pending_id in pending_ids:
        pending = owners[pending_id]
        if (
            pending.stop_price is None
            or pending.target_price is None
            or not pending.decision_ns <= observed_ns < pending.expires_ns
        ):
            return None
        # No fill is predicted: reserve the full permitted reference-price band.
        # Native slippage/gaps can exceed nominal stop risk after submission.
        if pending_id in known_order_ids:
            return None  # Submitted/terminal entry is not an unsubmitted reservation.
        total += pending.quantity * abs(pending.target_price - pending.stop_price)
    positions = cache.positions_open()
    for position in positions:
        owner_id = str(position.opening_order_id)
        if owner_id in pending_ids:
            return None
        owner = owners.get(owner_id)
        stop = orders.get(owner_id + "-stop")
        if owner is None or stop is None or not isinstance(stop, StopMarketOrder):
            return None
        expected_side = OrderSide.SELL if position.side == PositionSide.LONG else OrderSide.BUY
        if (
            position.side not in {PositionSide.LONG, PositionSide.SHORT}
            or str(position.instrument_id) != owner.instrument_id
            or stop.instrument_id != position.instrument_id
            or not stop.is_reduce_only
            or stop.side != expected_side
            or stop.leaves_qty.as_decimal() < position.quantity.as_decimal()
            or owner.stop_price is None
            or stop.trigger_price.as_decimal() != owner.stop_price
        ):
            return None
        entry = Decimal(str(position.avg_px_open))
        quantity = position.quantity.as_decimal()
        if not entry.is_finite() or entry <= 0 or quantity <= 0:
            raise ValueError("invalid native position valuation")
        total += quantity * abs(entry - stop.trigger_price.as_decimal())
        recognized.add(owner_id + "-stop")
        target_id = owner_id + "-target"
        if target_id in orders:
            target = orders[target_id]
            if (
                not target.is_reduce_only
                or target.side != expected_side
                or target.instrument_id != position.instrument_id
            ):
                return None
            recognized.add(target_id)
    if set(orders) - recognized:
        return None  # Entry/unknown reservations need a separate authoritative risk bound.
    return StopExposure(total, len(positions) + len(pending_ids), observed_ns, True)
