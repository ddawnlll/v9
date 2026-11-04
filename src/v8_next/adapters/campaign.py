"""Native simulated execution of pre-admitted campaigns; no economic decisions."""

from decimal import Decimal
from typing import Any

from nautilus_trader.model import (
    ClientOrderId,
    InstrumentId,
    OrderSide,
    OrderType,
    Price,
    Quantity,
    QuoteTick,
)
from nautilus_trader.trading import Strategy

from v8_next.domain.campaign import PaperCampaign
from v8_next.domain.market import CausalFrame


def terminal_unfilled_entry(order: Any) -> bool:
    return (
        order is not None
        and str(order.status) in {"CANCELED", "REJECTED", "DENIED", "EXPIRED"}
        and order.filled_qty.as_decimal() == 0
    )


class PaperCampaignAdapter(Strategy):
    """Use only in BacktestEngine. App must validate receipts before construction.

    Orders wait for a quote strictly after decision time. Deterministic client IDs
    and engine-cache checks prevent duplicate submission during replay. No private
    venue client is configured by this package. Restart must replay engine state;
    this adapter's in-memory state is not itself a recovery mechanism.
    """

    def __init__(self, campaigns: tuple[PaperCampaign, ...]) -> None:
        super().__init__()
        unique = {c.campaign_id: c for c in campaigns}
        if len(unique) != len(campaigns):
            raise ValueError("duplicate campaign identity")
        if len({c.opportunity_id for c in campaigns}) != len(campaigns):
            raise ValueError("duplicate opportunity allocation")
        for index, campaign in enumerate(campaigns):
            for other in campaigns[index + 1 :]:
                if campaign.instrument_id == other.instrument_id and (
                    max(campaign.decision_ns, other.decision_ns)
                    < min(campaign.expires_ns, other.expires_ns)
                ):
                    raise ValueError("overlapping netting campaigns are outside initial scope")
        self.order_events: list[dict[str, Any]] = []
        self.position_closures: dict[str, dict[str, Any]] = {}
        self.callback_failure: str | None = None
        self.campaigns = campaigns
        self.submitted: set[str] = set()
        self.expired: set[str] = set()
        self.invalidated: set[str] = set()
        self.exit_requested: set[str] = set()
        self.validity_frames: dict[tuple[str, int], CausalFrame] = {}
        self.thesis_invalidated: dict[str, int] = {}
        self.exit_order_ids: dict[str, set[str]] = {}

    def on_order_event(self, event: Any) -> None:
        """Observe native transitions; do not infer fills from submission flags."""
        self.order_events.append(
            {
                "event_type": type(event).__name__,
                "client_order_id": str(event.client_order_id),
                "instrument_id": str(event.instrument_id),
                "event_ns": event.ts_event,
                "received_ns": event.ts_init,
                "fill_price": str(event.last_px) if hasattr(event, "last_px") else None,
                "fill_quantity": str(event.last_qty) if hasattr(event, "last_qty") else None,
                "commission": str(event.commission) if hasattr(event, "commission") else None,
            }
        )

    def on_position_closed(self, event: Any) -> None:
        try:
            self.snapshot_position_close(event)
            # A reduce-only stop can close the remaining position with only a
            # partial fill of its original quantity. Native OCO need not cancel
            # siblings on that partial fill; end the campaign's remaining exits.
            owned_exits = self.exit_order_ids.get(str(event.opening_order_id), set())
            for order in self.cache.orders_open():
                if str(order.client_order_id) in owned_exits:
                    self.cancel_order(order.client_order_id)
        except Exception as error:
            self.callback_failure = f"{type(error).__name__}: {error}"
            raise

    def snapshot_position_close(self, event: Any) -> None:
        """Snapshot each native close before a netting ID is reused."""
        opening_id = str(event.opening_order_id)
        position = self.cache.position(event.position_id)
        if position is None:
            raise ValueError("closed native position missing from cache")
        record = {
            "campaign_id": opening_id,
            "instrument_id": str(event.instrument_id),
            "position_id": str(event.position_id),
            "opening_order_id": opening_id,
            "closing_order_id": str(event.closing_order_id),
            "opened_ns": event.ts_opened,
            "closed_ns": event.ts_closed,
            "observed_ns": event.ts_init,
            "entry_side": str(event.entry),
            "average_open_price": str(event.avg_px_open),
            "average_close_price": str(event.avg_px_close),
            "peak_quantity": str(event.peak_qty),
            "realized_pnl": str(event.realized_pnl),
            "currency": str(event.currency),
            "commissions": sorted(str(c) for c in position.commissions()),
            "adjustments": [
                {k: v for k, v in a.to_dict().items() if k not in {"event_id", "reason"}}
                for a in position.adjustments()
            ],
            "realization": "SIMULATED",
            "claim_status": "NO_ECONOMIC_CLAIM",
        }
        key = f"{opening_id}:{event.ts_opened}:{event.ts_closed}"
        if key in self.position_closures and self.position_closures[key] != record:
            raise ValueError("native position closure changed")
        self.position_closures[key] = record

    def closed_position_records(self) -> list[dict[str, Any]]:
        """Stable report ordering; retain native event timestamps unchanged."""
        return sorted(
            self.position_closures.values(),
            key=lambda row: (
                row["closed_ns"],
                row["instrument_id"],
                row["campaign_id"],
                row["opened_ns"],
            ),
        )

    def campaign_observations(self, state: dict[str, Any]) -> list[dict[str, Any]]:
        """Project native entry evidence; submission alone is never a fill."""
        return [
            {
                "campaign_id": c.campaign_id,
                "position_closures": [
                    r for r in self.position_closures.values() if r["campaign_id"] == c.campaign_id
                ],
                "opportunity_id": c.opportunity_id,
                "entry_events": [
                    e for e in self.order_events if e["client_order_id"] == c.campaign_id
                ],
                "exit_events": [
                    e
                    for e in self.order_events
                    if e["client_order_id"] in self.exit_order_ids.get(c.campaign_id, set())
                ],
                "exit_orders": [
                    o
                    for o in state["orders"]
                    if o["client_order_id"] in self.exit_order_ids.get(c.campaign_id, set())
                ],
                "submitted": c.campaign_id in self.submitted,
                "invalidated_before_submission": c.campaign_id in self.invalidated,
                "expired_before_submission": c.campaign_id in self.expired,
                "exit_requested": c.campaign_id in self.exit_requested,
                "thesis_invalidated_ns": self.thesis_invalidated.get(c.campaign_id),
                "entry_order": next(
                    (o for o in state["orders"] if o["client_order_id"] == c.campaign_id), None
                ),
                "realization": "SIMULATED",
            }
            for c in self.campaigns
        ]

    def on_start(self) -> None:
        for instrument in sorted({c.instrument_id for c in self.campaigns}):
            self.subscribe_quotes(InstrumentId.from_str(instrument))

    def on_quote(self, quote: QuoteTick) -> None:
        if self.callback_failure is not None:
            return
        try:
            frame = self.validity_frames.get((str(quote.instrument_id), quote.ts_init))
            if frame is not None:
                if frame.decision_ns != quote.ts_init or frame.instrument_id != str(
                    quote.instrument_id
                ):
                    raise ValueError("validity frame identity differs from native callback")
                self.observe_validity(frame)
            self.advance_campaigns(
                quote.instrument_id,
                quote.ts_init,
                quote.ask_price.as_decimal(),
                quote.bid_price.as_decimal(),
            )
        except Exception as error:
            self.callback_failure = f"{type(error).__name__}: {error}"
            raise

    def observe_validity(self, frame: CausalFrame) -> None:
        """Shared causal close policy for quote and historical bar adapters."""
        for campaign in self.campaigns:
            if campaign.instrument_id == frame.instrument_id and (
                campaign.invalidated_by_close(frame) is True
            ):
                self.thesis_invalidated.setdefault(campaign.campaign_id, frame.decision_ns)

    def has_unprojected_submission(self) -> bool:
        """Do not price dispatched entries as zero while native state is pending."""
        accounted = {row["campaign_id"] for row in self.position_closures.values()}
        accounted.update(str(p.opening_order_id) for p in self.cache.positions_open())
        for identity in self.submitted - accounted:
            order = self.cache.order(ClientOrderId(identity))
            if terminal_unfilled_entry(order):
                continue
            return True
        return False

    def has_unresolved_campaign(self, instrument_id: str) -> bool:
        """Submitted intent remains occupied before native cache catches up."""
        closed = {row["campaign_id"] for row in self.position_closures.values()}
        for campaign in self.campaigns:
            if campaign.instrument_id != instrument_id or campaign.campaign_id in closed:
                continue
            if campaign.campaign_id in self.expired | self.invalidated:
                continue
            order = self.cache.order(ClientOrderId(campaign.campaign_id))
            if terminal_unfilled_entry(order):
                continue
            return True
        return False

    def advance_campaigns(
        self,
        instrument_id: InstrumentId,
        observed_ns: int,
        buy_reference: Decimal,
        sell_reference: Decimal,
    ) -> None:
        """Advance at a native callback; references validate entry, never manufacture fills."""
        for campaign in self.campaigns:
            if campaign.instrument_id != str(instrument_id):
                continue
            if campaign.campaign_id in self.submitted:
                if terminal_unfilled_entry(self.cache.order(ClientOrderId(campaign.campaign_id))):
                    continue
                if any(
                    c["campaign_id"] == campaign.campaign_id
                    for c in self.position_closures.values()
                ):
                    continue  # A native closure ends authority even after a partial exit fill.
                # A native filled exit terminates this campaign's exit authority.
                # Otherwise its later timeout could close a successor netting position.
                if any(
                    (order := self.cache.order(ClientOrderId(exit_id))) is not None
                    and str(order.status) == "FILLED"
                    for exit_id in self.exit_order_ids.get(campaign.campaign_id, set())
                ):
                    continue
                if (
                    observed_ns >= campaign.expires_ns
                    or campaign.campaign_id in self.thesis_invalidated
                ) and campaign.campaign_id not in self.exit_requested:
                    # Initial netting scope: the owning app must admit at most one
                    # active campaign per instrument. Engine owns close execution.
                    self.cancel_all_orders(instrument_id)
                    exit_tag = f"v8-campaign-exit:{campaign.campaign_id}"
                    self.close_all_positions(instrument_id, reduce_only=True, tags=[exit_tag])
                    # Native-generated IDs remain authoritative. Tags bind intent
                    # without guessing ownership from instrument or fill time.
                    self.exit_order_ids.setdefault(campaign.campaign_id, set()).update(
                        {
                            str(order.client_order_id)
                            for order in self.cache.orders()
                            if exit_tag in (order.tags or [])
                        }
                    )
                    self.exit_requested.add(campaign.campaign_id)
                continue
            if campaign.campaign_id in self.thesis_invalidated:
                self.invalidated.add(campaign.campaign_id)
            if campaign.campaign_id in self.expired or campaign.campaign_id in self.invalidated:
                continue
            if observed_ns >= campaign.expires_ns:
                self.expired.add(campaign.campaign_id)
                continue
            if observed_ns <= campaign.decision_ns:
                continue
            client_id = ClientOrderId(campaign.campaign_id)
            if self.cache.order(client_id) is not None:
                self.submitted.add(campaign.campaign_id)
                continue
            instrument = self.cache.instrument(instrument_id)
            if instrument is None:
                raise ValueError("campaign instrument missing")
            quantity_text = format(campaign.quantity, f".{instrument.size_precision}f")
            if Decimal(quantity_text) != campaign.quantity:
                raise ValueError("campaign quantity exceeds venue precision")
            if campaign.stop_price is not None and campaign.target_price is not None:
                prices = [campaign.stop_price, campaign.target_price]
                if any(p % instrument.price_increment.as_decimal() != 0 for p in prices):
                    raise ValueError("campaign protection violates venue price increment")
                entry_price = buy_reference if campaign.direction == "LONG" else sell_reference
                sign = 1 if campaign.direction == "LONG" else -1
                if (entry_price - campaign.stop_price) * sign <= 0 or (
                    campaign.target_price - entry_price
                ) * sign <= 0:
                    self.invalidated.add(campaign.campaign_id)
                    continue
                stop_id = ClientOrderId(campaign.campaign_id + "-stop")
                target_id = ClientOrderId(campaign.campaign_id + "-target")
                self.exit_order_ids[campaign.campaign_id] = {str(stop_id), str(target_id)}
                orders = self.order_factory.bracket(
                    instrument_id=instrument_id,
                    order_side=OrderSide.BUY if campaign.direction == "LONG" else OrderSide.SELL,
                    quantity=Quantity.from_str(quantity_text),
                    entry_order_type=OrderType.MARKET,
                    entry_client_order_id=client_id,
                    sl_trigger_price=Price.from_str(
                        format(campaign.stop_price, f".{instrument.price_precision}f")
                    ),
                    sl_client_order_id=stop_id,
                    tp_price=Price.from_str(
                        format(campaign.target_price, f".{instrument.price_precision}f")
                    ),
                    tp_post_only=False,
                    tp_client_order_id=target_id,
                )
                self.submit_order_list(orders)
                self.submitted.add(campaign.campaign_id)
                continue
            order = self.order_factory.market(
                instrument_id=instrument_id,
                order_side=OrderSide.BUY if campaign.direction == "LONG" else OrderSide.SELL,
                quantity=Quantity.from_str(quantity_text),
                client_order_id=client_id,
            )
            self.submit_order(order)
            self.submitted.add(campaign.campaign_id)
