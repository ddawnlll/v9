"""Independent two-campaign accounting oracle (M0-02, issue #3).

A second, deliberately independent recomputation of campaign accounting over
hand-constructed source records: per-order fills, fees, funding, realized PnL, the final
mark and final equity, for two concurrent or sequential campaigns.

Independence is structural, not a claim: this module imports no engine aggregation path
(``adapters.accounting_replay``, ``audit.pipeline``, ``audit.trade_metrics``) and takes
plain frozen records as input. Every number it returns is derived from the records below
with exact :class:`~decimal.Decimal` arithmetic — never floats, never rounding. The only
repo primitive reused is the provenance digest helper from ``telemetry.identity``.

Position accounting is FIFO: realized PnL is attributed to the *closing* order, open
lots are valued at the campaign's single final mark. That choice is deterministic and
division-free, so the oracle's arithmetic is exactly reproducible on any machine.

Fail-closed join rules (each raises a typed :class:`OracleViolation`):

* duplicated fills, fees, orders, funding rows or campaign ids are hard failures —
  a duplicated row would double-count cash;
* a fill must join exactly one order whose campaign and instrument it matches; a fill
  that disagrees with its order's campaign is a *swapped campaign id* and fails;
* every record must belong to a declared campaign — the oracle never infers or
  substitutes a campaign from its neighbours;
* a campaign needs exactly one final mark taken at or after its last fill.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum

from v8_next.telemetry.identity import canonical_digest

ZERO = Decimal("0")

#: Domain separator for :attr:`OracleReport.provenance_digest`.
PROVENANCE_TAG = "accounting-oracle-two-campaign"


class OrderSide(StrEnum):
    """Side of a venue order and of each fill against it."""

    BUY = "BUY"
    SELL = "SELL"

    @property
    def sign(self) -> int:
        """``+1`` for a buy, ``-1`` for a sell."""
        return 1 if self is OrderSide.BUY else -1


class OracleViolationCode(StrEnum):
    """Machine-readable discriminator carried by every :class:`OracleViolation`."""

    DUPLICATE_CAMPAIGN = "DUPLICATE_CAMPAIGN"
    DUPLICATE_ORDER = "DUPLICATE_ORDER"
    DUPLICATE_FILL = "DUPLICATE_FILL"
    DUPLICATE_FEE = "DUPLICATE_FEE"
    DUPLICATE_FUNDING = "DUPLICATE_FUNDING"
    DUPLICATE_MARK = "DUPLICATE_MARK"
    ORPHAN_ORDER = "ORPHAN_ORDER"
    ORPHAN_FILL = "ORPHAN_FILL"
    ORPHAN_FUNDING = "ORPHAN_FUNDING"
    ORPHAN_MARK = "ORPHAN_MARK"
    CAMPAIGN_MISMATCH = "CAMPAIGN_MISMATCH"
    INSTRUMENT_MISMATCH = "INSTRUMENT_MISMATCH"
    SIDE_MISMATCH = "SIDE_MISMATCH"
    OVERFILL = "OVERFILL"
    FEE_PROVENANCE_MISSING = "FEE_PROVENANCE_MISSING"
    EVENT_TIME_OUT_OF_RANGE = "EVENT_TIME_OUT_OF_RANGE"
    MARK_BEFORE_FILL = "MARK_BEFORE_FILL"
    MISSING_MARK = "MISSING_MARK"
    ARITHMETIC_MISMATCH = "ARITHMETIC_MISMATCH"


class OracleViolation(ValueError):
    """A typed accounting-oracle failure; ``code`` is the branchable discriminator."""

    code: OracleViolationCode
    record_id: str
    detail: str

    def __init__(self, code: OracleViolationCode, record_id: str, detail: str) -> None:
        super().__init__(f"{code.value}: {record_id}: {detail}")
        self.code = code
        self.record_id = record_id
        self.detail = detail


def _require_text(value: object, name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name}: non-empty string required")


def _require_int(value: object, name: str) -> None:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"{name}: int required")


def _require_money(value: object, name: str) -> None:
    if not isinstance(value, Decimal):
        raise ValueError(f"{name}: Decimal required (floats are not money)")
    if not value.is_finite():
        raise ValueError(f"{name}: finite Decimal required")


def _require_positive(value: Decimal, name: str) -> None:
    if value <= ZERO:
        raise ValueError(f"{name}: strictly positive required")


# --------------------------------------------------------------------------- #
# input records (hand-constructed source evidence)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class CampaignRecord:
    """One campaign under audit: its starting cash and its single marked instrument."""

    campaign_id: str
    instrument_id: str
    initial_equity: Decimal
    opened_ns: int

    def __post_init__(self) -> None:
        _require_text(self.campaign_id, "campaign_id")
        _require_text(self.instrument_id, "instrument_id")
        _require_money(self.initial_equity, "initial_equity")
        _require_int(self.opened_ns, "opened_ns")


@dataclass(frozen=True, slots=True)
class OrderRecord:
    """A submitted venue order; fills must join to exactly one of these."""

    order_id: str
    campaign_id: str
    instrument_id: str
    side: OrderSide
    quantity: Decimal
    submitted_ns: int

    def __post_init__(self) -> None:
        _require_text(self.order_id, "order_id")
        _require_text(self.campaign_id, "campaign_id")
        _require_text(self.instrument_id, "instrument_id")
        _require_money(self.quantity, "quantity")
        _require_positive(self.quantity, "quantity")
        _require_int(self.submitted_ns, "submitted_ns")


@dataclass(frozen=True, slots=True)
class FillRecord:
    """One execution. ``fee_id`` is the provenance key of the fee charge it carries."""

    fill_id: str
    order_id: str
    campaign_id: str
    instrument_id: str
    side: OrderSide
    quantity: Decimal
    price: Decimal
    fee: Decimal
    fee_id: str
    event_time_ns: int

    def __post_init__(self) -> None:
        _require_text(self.fill_id, "fill_id")
        _require_text(self.order_id, "order_id")
        _require_text(self.campaign_id, "campaign_id")
        _require_text(self.instrument_id, "instrument_id")
        _require_money(self.quantity, "quantity")
        _require_positive(self.quantity, "quantity")
        _require_money(self.price, "price")
        _require_positive(self.price, "price")
        _require_money(self.fee, "fee")
        _require_text(self.fee_id, "fee_id")
        _require_int(self.event_time_ns, "event_time_ns")
        if self.fee < ZERO:
            raise ValueError("fee: non-negative required")


@dataclass(frozen=True, slots=True)
class FundingRecord:
    """A signed funding settlement: negative pays, positive receives."""

    funding_id: str
    campaign_id: str
    instrument_id: str
    amount: Decimal
    event_time_ns: int

    def __post_init__(self) -> None:
        _require_text(self.funding_id, "funding_id")
        _require_text(self.campaign_id, "campaign_id")
        _require_text(self.instrument_id, "instrument_id")
        _require_money(self.amount, "amount")
        _require_int(self.event_time_ns, "event_time_ns")


@dataclass(frozen=True, slots=True)
class MarkRecord:
    """The single final mark used to value a campaign's open position."""

    mark_id: str
    campaign_id: str
    instrument_id: str
    price: Decimal
    mark_time_ns: int

    def __post_init__(self) -> None:
        _require_text(self.mark_id, "mark_id")
        _require_text(self.campaign_id, "campaign_id")
        _require_text(self.instrument_id, "instrument_id")
        _require_money(self.price, "price")
        _require_positive(self.price, "price")
        _require_int(self.mark_time_ns, "mark_time_ns")


# --------------------------------------------------------------------------- #
# independent ledger result
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class OrderLedger:
    """Independently recomputed per-order execution facts."""

    order_id: str
    side: OrderSide
    filled_quantity: Decimal
    notional: Decimal
    fees: Decimal
    realized_pnl: Decimal
    fill_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CampaignLedger:
    """Independently recomputed campaign accounting, one row per campaign."""

    campaign_id: str
    instrument_id: str
    initial_equity: Decimal
    realized_pnl: Decimal
    funding: Decimal
    fees: Decimal
    unrealized_pnl: Decimal
    open_quantity: Decimal
    mark_price: Decimal
    final_equity: Decimal
    orders: tuple[OrderLedger, ...]
    fill_ids: tuple[str, ...]
    fee_ids: tuple[str, ...]
    funding_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class OracleReport:
    """The complete independent recomputation, with provenance and totals."""

    ledgers: tuple[CampaignLedger, ...]
    total_initial_equity: Decimal
    total_final_equity: Decimal
    provenance_digest: str = field(default="")

    def ledger(self, campaign_id: str) -> CampaignLedger:
        for ledger in self.ledgers:
            if ledger.campaign_id == campaign_id:
                return ledger
        raise KeyError(f"unknown campaign: {campaign_id}")


# --------------------------------------------------------------------------- #
# internal accumulators
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class _Lot:
    """One open FIFO lot; ``quantity`` is signed (+ long, - short)."""

    quantity: Decimal
    price: Decimal


@dataclass(slots=True)
class _OrderAccumulator:
    order: OrderRecord
    filled_quantity: Decimal = ZERO
    notional: Decimal = ZERO
    fees: Decimal = ZERO
    realized_pnl: Decimal = ZERO
    fill_ids: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# validation and recomputation
# --------------------------------------------------------------------------- #
def _index_campaigns(
    campaigns: Sequence[CampaignRecord],
) -> dict[str, CampaignRecord]:
    indexed: dict[str, CampaignRecord] = {}
    for campaign in campaigns:
        if campaign.campaign_id in indexed:
            raise OracleViolation(
                OracleViolationCode.DUPLICATE_CAMPAIGN,
                campaign.campaign_id,
                "campaign id declared twice",
            )
        indexed[campaign.campaign_id] = campaign
    return indexed


def _index_orders(
    orders: Sequence[OrderRecord], campaigns: Mapping[str, CampaignRecord]
) -> dict[str, OrderRecord]:
    indexed: dict[str, OrderRecord] = {}
    for order in orders:
        if order.order_id in indexed:
            raise OracleViolation(
                OracleViolationCode.DUPLICATE_ORDER,
                order.order_id,
                "order id declared twice",
            )
        campaign = campaigns.get(order.campaign_id)
        if campaign is None:
            raise OracleViolation(
                OracleViolationCode.ORPHAN_ORDER,
                order.order_id,
                f"order campaign {order.campaign_id!r} is not declared",
            )
        if order.instrument_id != campaign.instrument_id:
            raise OracleViolation(
                OracleViolationCode.INSTRUMENT_MISMATCH,
                order.order_id,
                f"order instrument {order.instrument_id!r} != campaign instrument "
                f"{campaign.instrument_id!r}",
            )
        if order.submitted_ns < campaign.opened_ns:
            raise OracleViolation(
                OracleViolationCode.EVENT_TIME_OUT_OF_RANGE,
                order.order_id,
                f"submitted {order.submitted_ns} precedes campaign open {campaign.opened_ns}",
            )
        indexed[order.order_id] = order
    return indexed


def _validate_fills(
    fills: Sequence[FillRecord],
    orders: Mapping[str, OrderRecord],
    campaigns: Mapping[str, CampaignRecord],
) -> None:
    seen_fills: set[str] = set()
    seen_fees: set[str] = set()
    for fill in fills:
        if fill.fill_id in seen_fills:
            raise OracleViolation(
                OracleViolationCode.DUPLICATE_FILL,
                fill.fill_id,
                "fill id appears more than once; fills must not be double counted",
            )
        seen_fills.add(fill.fill_id)
        if not fill.fee_id.strip():
            raise OracleViolation(
                OracleViolationCode.FEE_PROVENANCE_MISSING,
                fill.fill_id,
                "fee charge has no provenance id",
            )
        if fill.fee_id in seen_fees:
            raise OracleViolation(
                OracleViolationCode.DUPLICATE_FEE,
                fill.fee_id,
                f"fee id {fill.fee_id!r} charged more than once",
            )
        seen_fees.add(fill.fee_id)
        order = orders.get(fill.order_id)
        if order is None:
            raise OracleViolation(
                OracleViolationCode.ORPHAN_FILL,
                fill.fill_id,
                f"fill order {fill.order_id!r} does not exist",
            )
        if fill.campaign_id != order.campaign_id:
            raise OracleViolation(
                OracleViolationCode.CAMPAIGN_MISMATCH,
                fill.fill_id,
                f"fill campaign {fill.campaign_id!r} != order campaign "
                f"{order.campaign_id!r} (swapped campaign id)",
            )
        if fill.campaign_id not in campaigns:
            raise OracleViolation(
                OracleViolationCode.ORPHAN_FILL,
                fill.fill_id,
                f"fill campaign {fill.campaign_id!r} is not declared",
            )
        if fill.instrument_id != order.instrument_id:
            raise OracleViolation(
                OracleViolationCode.INSTRUMENT_MISMATCH,
                fill.fill_id,
                f"fill instrument {fill.instrument_id!r} != order instrument "
                f"{order.instrument_id!r}",
            )
        if fill.side != order.side:
            raise OracleViolation(
                OracleViolationCode.SIDE_MISMATCH,
                fill.fill_id,
                f"fill side {fill.side.value} != order side {order.side.value}",
            )
        if fill.event_time_ns < order.submitted_ns:
            raise OracleViolation(
                OracleViolationCode.EVENT_TIME_OUT_OF_RANGE,
                fill.fill_id,
                f"fill {fill.event_time_ns} precedes order submission {order.submitted_ns}",
            )


def _validate_funding(
    funding: Sequence[FundingRecord], campaigns: Mapping[str, CampaignRecord]
) -> None:
    seen: set[str] = set()
    for row in funding:
        if row.funding_id in seen:
            raise OracleViolation(
                OracleViolationCode.DUPLICATE_FUNDING,
                row.funding_id,
                "funding id appears more than once",
            )
        seen.add(row.funding_id)
        campaign = campaigns.get(row.campaign_id)
        if campaign is None:
            raise OracleViolation(
                OracleViolationCode.ORPHAN_FUNDING,
                row.funding_id,
                f"funding campaign {row.campaign_id!r} is not declared",
            )
        if row.instrument_id != campaign.instrument_id:
            raise OracleViolation(
                OracleViolationCode.INSTRUMENT_MISMATCH,
                row.funding_id,
                f"funding instrument {row.instrument_id!r} != campaign instrument "
                f"{campaign.instrument_id!r}",
            )
        if row.event_time_ns < campaign.opened_ns:
            raise OracleViolation(
                OracleViolationCode.EVENT_TIME_OUT_OF_RANGE,
                row.funding_id,
                f"funding {row.event_time_ns} precedes campaign open {campaign.opened_ns}",
            )


def _index_marks(
    marks: Sequence[MarkRecord],
    campaigns: Mapping[str, CampaignRecord],
    last_fill_ns: Mapping[str, int],
) -> dict[str, MarkRecord]:
    indexed: dict[str, MarkRecord] = {}
    for mark in marks:
        if mark.campaign_id in indexed:
            raise OracleViolation(
                OracleViolationCode.DUPLICATE_MARK,
                mark.mark_id,
                f"campaign {mark.campaign_id!r} has more than one final mark",
            )
        campaign = campaigns.get(mark.campaign_id)
        if campaign is None:
            raise OracleViolation(
                OracleViolationCode.ORPHAN_MARK,
                mark.mark_id,
                f"mark campaign {mark.campaign_id!r} is not declared",
            )
        if mark.instrument_id != campaign.instrument_id:
            raise OracleViolation(
                OracleViolationCode.INSTRUMENT_MISMATCH,
                mark.mark_id,
                f"mark instrument {mark.instrument_id!r} != campaign instrument "
                f"{campaign.instrument_id!r}",
            )
        floor_ns = max(campaign.opened_ns, last_fill_ns.get(mark.campaign_id, campaign.opened_ns))
        if mark.mark_time_ns < floor_ns:
            raise OracleViolation(
                OracleViolationCode.MARK_BEFORE_FILL,
                mark.mark_id,
                f"mark at {mark.mark_time_ns} precedes campaign evidence at {floor_ns}",
            )
        indexed[mark.campaign_id] = mark
    for campaign_id in campaigns:
        if campaign_id not in indexed:
            raise OracleViolation(
                OracleViolationCode.MISSING_MARK,
                campaign_id,
                "campaign has no final mark; equity cannot be certified",
            )
    return indexed


def _apply_fill(lots: list[_Lot], fill: FillRecord) -> Decimal:
    """Apply one fill to the FIFO lot book; return PnL realized by this fill."""
    remaining = fill.side.sign * fill.quantity
    realized = ZERO
    while remaining != ZERO and lots and (lots[0].quantity > ZERO) != (remaining > ZERO):
        lot = lots[0]
        lot_sign = 1 if lot.quantity > ZERO else -1
        closing = min(abs(lot.quantity), abs(remaining))
        realized += (fill.price - lot.price) * closing * lot_sign
        lot.quantity -= closing * lot_sign
        remaining += closing * lot_sign
        if lot.quantity == ZERO:
            lots.pop(0)
    if remaining != ZERO:
        lots.append(_Lot(quantity=remaining, price=fill.price))
    return realized


def _recompute_campaign(
    campaign: CampaignRecord,
    orders: Sequence[OrderRecord],
    fills: Sequence[FillRecord],
    funding: Sequence[FundingRecord],
    mark: MarkRecord,
) -> CampaignLedger:
    accumulators = {order.order_id: _OrderAccumulator(order=order) for order in orders}
    lots: list[_Lot] = []
    realized_pnl = ZERO
    fees = ZERO
    ordered_fills = sorted(fills, key=lambda row: (row.event_time_ns, row.fill_id))
    for fill in ordered_fills:
        accumulator = accumulators[fill.order_id]
        next_filled = accumulator.filled_quantity + fill.quantity
        if next_filled > accumulator.order.quantity:
            raise OracleViolation(
                OracleViolationCode.OVERFILL,
                fill.fill_id,
                f"order {fill.order_id!r} filled {next_filled} > submitted "
                f"{accumulator.order.quantity}",
            )
        closed_pnl = _apply_fill(lots, fill)
        realized_pnl += closed_pnl
        accumulator.realized_pnl += closed_pnl
        accumulator.filled_quantity = next_filled
        accumulator.notional += fill.quantity * fill.price
        accumulator.fees += fill.fee
        accumulator.fill_ids.append(fill.fill_id)
        fees += fill.fee

    funding_total = sum((row.amount for row in funding), ZERO)
    unrealized_pnl = sum(
        ((mark.price - lot.price) * lot.quantity for lot in lots),
        ZERO,
    )
    open_quantity = sum((lot.quantity for lot in lots), ZERO)
    final_equity = campaign.initial_equity + realized_pnl + funding_total - fees + unrealized_pnl
    return CampaignLedger(
        campaign_id=campaign.campaign_id,
        instrument_id=campaign.instrument_id,
        initial_equity=campaign.initial_equity,
        realized_pnl=realized_pnl,
        funding=funding_total,
        fees=fees,
        unrealized_pnl=unrealized_pnl,
        open_quantity=open_quantity,
        mark_price=mark.price,
        final_equity=final_equity,
        orders=tuple(
            OrderLedger(
                order_id=accumulator.order.order_id,
                side=accumulator.order.side,
                filled_quantity=accumulator.filled_quantity,
                notional=accumulator.notional,
                fees=accumulator.fees,
                realized_pnl=accumulator.realized_pnl,
                fill_ids=tuple(accumulator.fill_ids),
            )
            for accumulator in sorted(accumulators.values(), key=lambda row: row.order.order_id)
        ),
        fill_ids=tuple(fill.fill_id for fill in ordered_fills),
        fee_ids=tuple(fill.fee_id for fill in ordered_fills),
        funding_ids=tuple(sorted(row.funding_id for row in funding)),
    )


def _provenance_digest(
    campaigns: Sequence[CampaignRecord],
    orders: Sequence[OrderRecord],
    fills: Sequence[FillRecord],
    funding: Sequence[FundingRecord],
    marks: Sequence[MarkRecord],
) -> str:
    payload = {
        "campaigns": [
            [c.campaign_id, c.instrument_id, str(c.initial_equity), c.opened_ns]
            for c in sorted(campaigns, key=lambda row: row.campaign_id)
        ],
        "orders": [
            [
                o.order_id,
                o.campaign_id,
                o.instrument_id,
                o.side.value,
                str(o.quantity),
                o.submitted_ns,
            ]
            for o in sorted(orders, key=lambda row: row.order_id)
        ],
        "fills": [
            [
                f.fill_id,
                f.order_id,
                f.campaign_id,
                f.instrument_id,
                f.side.value,
                str(f.quantity),
                str(f.price),
                str(f.fee),
                f.fee_id,
                f.event_time_ns,
            ]
            for f in sorted(fills, key=lambda row: row.fill_id)
        ],
        "funding": [
            [r.funding_id, r.campaign_id, r.instrument_id, str(r.amount), r.event_time_ns]
            for r in sorted(funding, key=lambda row: row.funding_id)
        ],
        "marks": [
            [m.mark_id, m.campaign_id, m.instrument_id, str(m.price), m.mark_time_ns]
            for m in sorted(marks, key=lambda row: row.mark_id)
        ],
    }
    return canonical_digest(PROVENANCE_TAG, payload)


def recompute(
    *,
    campaigns: Sequence[CampaignRecord],
    orders: Sequence[OrderRecord],
    fills: Sequence[FillRecord],
    funding: Sequence[FundingRecord] = (),
    marks: Sequence[MarkRecord],
) -> OracleReport:
    """Independently recompute two-campaign accounting from source records.

    Raises :class:`OracleViolation` on the first typed failure; returns the complete
    ledger only when every join resolves and the arithmetic identity closes.
    """
    campaign_index = _index_campaigns(campaigns)
    order_index = _index_orders(orders, campaign_index)
    _validate_fills(fills, order_index, campaign_index)
    _validate_funding(funding, campaign_index)

    last_fill_ns: dict[str, int] = {}
    for fill in fills:
        previous = last_fill_ns.get(fill.campaign_id)
        if previous is None or fill.event_time_ns > previous:
            last_fill_ns[fill.campaign_id] = fill.event_time_ns
    mark_index = _index_marks(marks, campaign_index, last_fill_ns)

    by_campaign_orders: dict[str, list[OrderRecord]] = {cid: [] for cid in campaign_index}
    for order in orders:
        by_campaign_orders[order.campaign_id].append(order)
    by_campaign_fills: dict[str, list[FillRecord]] = {cid: [] for cid in campaign_index}
    for fill in fills:
        by_campaign_fills[fill.campaign_id].append(fill)
    by_campaign_funding: dict[str, list[FundingRecord]] = {cid: [] for cid in campaign_index}
    for row in funding:
        by_campaign_funding[row.campaign_id].append(row)

    ledgers = tuple(
        _recompute_campaign(
            campaign_index[campaign_id],
            by_campaign_orders[campaign_id],
            by_campaign_fills[campaign_id],
            by_campaign_funding[campaign_id],
            mark_index[campaign_id],
        )
        for campaign_id in sorted(campaign_index)
    )

    total_initial = sum((ledger.initial_equity for ledger in ledgers), ZERO)
    total_final = sum((ledger.final_equity for ledger in ledgers), ZERO)
    identity = sum(
        (
            ledger.initial_equity
            + ledger.realized_pnl
            + ledger.funding
            - ledger.fees
            + ledger.unrealized_pnl
            for ledger in ledgers
        ),
        ZERO,
    )
    if identity != total_final:
        raise OracleViolation(
            OracleViolationCode.ARITHMETIC_MISMATCH,
            "<report>",
            f"equity identity {identity} != summed final equity {total_final}",
        )

    return OracleReport(
        ledgers=ledgers,
        total_initial_equity=total_initial,
        total_final_equity=total_final,
        provenance_digest=_provenance_digest(campaigns, orders, fills, funding, marks),
    )


def reconcile_aggregate(report: OracleReport, aggregate: Mapping[str, Decimal]) -> None:
    """Compare the independent report against an engine aggregate, campaign by campaign.

    Never imports the engine; the aggregate is supplied as data. A missing, extra or
    unequal campaign is an :class:`OracleViolation` — reconciliation is exact.
    """
    oracle_ids = {ledger.campaign_id for ledger in report.ledgers}
    supplied_ids = set(aggregate)
    if oracle_ids != supplied_ids:
        raise OracleViolation(
            OracleViolationCode.ARITHMETIC_MISMATCH,
            "<aggregate>",
            f"campaign set differs: oracle={sorted(oracle_ids)} aggregate={sorted(supplied_ids)}",
        )
    for ledger in report.ledgers:
        supplied = aggregate[ledger.campaign_id]
        if not isinstance(supplied, Decimal):
            raise OracleViolation(
                OracleViolationCode.ARITHMETIC_MISMATCH,
                ledger.campaign_id,
                "aggregate equity must be a Decimal",
            )
        if supplied != ledger.final_equity:
            raise OracleViolation(
                OracleViolationCode.ARITHMETIC_MISMATCH,
                ledger.campaign_id,
                f"aggregate final equity {supplied} != independent {ledger.final_equity}",
            )


__all__ = [
    "CampaignLedger",
    "CampaignRecord",
    "FillRecord",
    "FundingRecord",
    "MarkRecord",
    "OracleReport",
    "OracleViolation",
    "OracleViolationCode",
    "OrderLedger",
    "OrderRecord",
    "OrderSide",
    "recompute",
    "reconcile_aggregate",
]
