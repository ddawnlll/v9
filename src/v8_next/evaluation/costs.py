"""Physical venue-cost calibration and operating-net accounting.

Cost identity discipline (sources: SEC EMSAC maker-taker memo 2015 — fee
schedules are dated, venue-specific artifacts; Malinova & Park 2015 — a fee
schedule is not a cost measurement; Battalio et al. 2016 — fill quality is
venue/take-fee dependent, so a scalar fee collapses what must travel
together): a calibration receipt is bound to a physical fee schedule and
carries its venue, tier and effective date, and it separates maker from taker
fills.  A blended quantity-weighted rate is still published, but it is no
longer the only number available.

Fail-closed: a caller that declares its P&L already net of friction must
supply the artifact that establishes that basis; otherwise the ledger would
silently drop explicit fees (the spread double-count the friction flag exists
to prevent).
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from v8_next.evaluation.parity import ArtifactBinding

LiquidityRole = Literal["MAKER", "TAKER"]


class VenueFeeSchedule(BaseModel):
    """Dated, venue-specific fee schedule the fills were executed under."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    venue: str
    fee_tier: str
    effective_date: str
    source_artifact: ArtifactBinding

    @model_validator(mode="after")
    def validate_identity(self) -> VenueFeeSchedule:
        if not self.venue.strip() or not self.fee_tier.strip():
            raise ValueError("venue and fee tier identity are required")
        try:
            date.fromisoformat(self.effective_date)
        except ValueError as exc:
            raise ValueError("effective_date must be an ISO-8601 date") from exc
        return self


class VenueFillCost(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    quantity: Decimal = Field(gt=0)
    fee: Decimal = Field(ge=0)
    fill_price: Decimal = Field(gt=0)
    reference_mid: Decimal | None = Field(default=None, gt=0)
    liquidity_role: LiquidityRole


class VenueCostReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["CALIBRATED", "DATA_BLOCKED"]
    venue: str
    fee_tier: str
    fee_effective_date: str
    maker_fee_rate: Decimal | None
    taker_fee_rate: Decimal | None
    fee_rate: Decimal | None
    fill_vs_mid_bps: Decimal | None
    source_artifact: ArtifactBinding
    calibration_version: str


class OperatingExpense(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    amount: Decimal = Field(ge=0)
    currency: str
    period_start_ns: int
    period_end_ns: int
    source_artifact: ArtifactBinding

    @model_validator(mode="after")
    def validate_period(self) -> OperatingExpense:
        if self.period_end_ns <= self.period_start_ns or not self.currency.strip():
            raise ValueError("operating expense period/currency is invalid")
        return self


class OperatingNetResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["NO_ECONOMIC_CLAIM", "DATA_BLOCKED"]
    transaction_pnl: Decimal
    funding: Decimal
    explicit_fees: Decimal
    strategy_net: Decimal
    operating_costs: Decimal
    operating_net: Decimal
    currency: str
    period_start_ns: int
    period_end_ns: int
    price_pnl_already_net_of_friction: bool = False
    reason: str | None = None


def _quantity_weighted_fee_rate(fills: tuple[VenueFillCost, ...]) -> Decimal | None:
    if not fills:
        return None
    quantity = sum((row.quantity for row in fills), Decimal(0))
    return sum((row.fee for row in fills), Decimal(0)) / quantity


def calibrate_venue_cost(
    fills: tuple[VenueFillCost, ...], schedule: VenueFeeSchedule
) -> VenueCostReceipt:
    verified, detail = schedule.source_artifact.verify()
    if not verified:
        raise ValueError(f"DATA_BLOCKED_COST_ARTIFACT: {detail}")
    blocked = VenueCostReceipt(
        status="DATA_BLOCKED",
        venue=schedule.venue,
        fee_tier=schedule.fee_tier,
        fee_effective_date=schedule.effective_date,
        maker_fee_rate=None,
        taker_fee_rate=None,
        fee_rate=None,
        fill_vs_mid_bps=None,
        source_artifact=schedule.source_artifact,
        calibration_version="venue-cost.v2",
    )
    if not fills:
        return blocked
    maker_fills = tuple(row for row in fills if row.liquidity_role == "MAKER")
    taker_fills = tuple(row for row in fills if row.liquidity_role == "TAKER")
    with_mid = [row for row in fills if row.reference_mid is not None]
    if not with_mid:
        return blocked
    friction_total = Decimal(0)
    for row in with_mid:
        reference_mid = row.reference_mid
        assert reference_mid is not None
        friction_total += abs(row.fill_price - reference_mid) / reference_mid * Decimal(10_000)
    friction = friction_total / Decimal(len(with_mid))
    return VenueCostReceipt(
        status="CALIBRATED",
        venue=schedule.venue,
        fee_tier=schedule.fee_tier,
        fee_effective_date=schedule.effective_date,
        maker_fee_rate=_quantity_weighted_fee_rate(maker_fills),
        taker_fee_rate=_quantity_weighted_fee_rate(taker_fills),
        fee_rate=_quantity_weighted_fee_rate(fills),
        fill_vs_mid_bps=friction,
        source_artifact=schedule.source_artifact,
        calibration_version="venue-cost.v2",
    )


def compute_operating_net(
    *,
    transaction_pnl: Decimal,
    funding: Decimal | None,
    explicit_fees: Decimal | None,
    expenses: tuple[OperatingExpense, ...],
    currency: str,
    price_pnl_already_net_of_friction: bool = False,
    friction_net_evidence: ArtifactBinding | None = None,
) -> OperatingNetResult:
    def blocked(reason: str) -> OperatingNetResult:
        return OperatingNetResult(
            status="DATA_BLOCKED",
            transaction_pnl=transaction_pnl,
            funding=funding or Decimal(0),
            explicit_fees=explicit_fees or Decimal(0),
            strategy_net=Decimal(0),
            operating_costs=Decimal(0),
            operating_net=Decimal(0),
            currency=currency,
            period_start_ns=0,
            period_end_ns=0,
            price_pnl_already_net_of_friction=price_pnl_already_net_of_friction,
            reason=reason,
        )

    if funding is None or explicit_fees is None or not expenses:
        return blocked("MISSING_FUNDING_FEES_OR_EXPENSES")
    if not currency.strip() or any(exp.currency != currency for exp in expenses):
        raise ValueError("operating-net currency mismatch")
    for expense in expenses:
        verified, detail = expense.source_artifact.verify()
        if not verified:
            raise ValueError(f"DATA_BLOCKED_COST_ARTIFACT: {detail}")
    if price_pnl_already_net_of_friction:
        # The flag is the only thing standing between the ledger and a spread
        # double-count, so it must be bound to the artifact that establishes
        # the net-of-friction basis instead of being taken on trust.
        if friction_net_evidence is None:
            return blocked("FRICTION_NET_BASIS_NOT_EVIDENCED")
        verified, detail = friction_net_evidence.verify()
        if not verified:
            raise ValueError(f"DATA_BLOCKED_COST_ARTIFACT: {detail}")
    if not all(value.is_finite() for value in (transaction_pnl, funding, explicit_fees)):
        raise ValueError("operating-net values must be finite")
    strategy_net = transaction_pnl + funding
    if not price_pnl_already_net_of_friction:
        strategy_net -= explicit_fees
    operating_costs = sum((exp.amount for exp in expenses), Decimal(0))
    start = min(exp.period_start_ns for exp in expenses)
    end = max(exp.period_end_ns for exp in expenses)
    return OperatingNetResult(
        status="NO_ECONOMIC_CLAIM",
        transaction_pnl=transaction_pnl,
        funding=funding,
        explicit_fees=explicit_fees,
        strategy_net=strategy_net,
        operating_costs=operating_costs,
        operating_net=strategy_net - operating_costs,
        currency=currency,
        period_start_ns=start,
        period_end_ns=end,
        price_pnl_already_net_of_friction=price_pnl_already_net_of_friction,
    )
