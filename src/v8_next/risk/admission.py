"""Exposure admission consumes authoritative snapshots, never observer counts."""

from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal


@dataclass(frozen=True)
class RiskSnapshot:
    equity: Decimal
    gross_notional: Decimal
    exposure_notional: Decimal
    reserved_notional: Decimal
    as_of_ns: int
    reconciled: bool
    # None retains the conservative single-scalar reservation contract.
    exposure_reserved_notional: Decimal | None = None


@dataclass(frozen=True)
class RiskLimits:
    max_gross_fraction: Decimal
    max_exposure_fraction: Decimal
    max_order_notional: Decimal
    max_snapshot_age_ns: int


@dataclass(frozen=True)
class Admission:
    quantity: Decimal | None
    reason: str


def admit(
    snapshot: RiskSnapshot,
    limits: RiskLimits,
    decision_ns: int,
    price: Decimal,
    requested_notional: Decimal,
    step: Decimal,
    min_quantity: Decimal,
    max_quantity: Decimal,
    min_notional: Decimal,
) -> Admission:
    exposure_reserved = (
        snapshot.reserved_notional
        if snapshot.exposure_reserved_notional is None
        else snapshot.exposure_reserved_notional
    )
    values = (
        exposure_reserved,
        snapshot.equity,
        snapshot.gross_notional,
        snapshot.exposure_notional,
        snapshot.reserved_notional,
        limits.max_gross_fraction,
        limits.max_exposure_fraction,
        limits.max_order_notional,
        price,
        requested_notional,
        step,
        min_quantity,
        max_quantity,
        min_notional,
    )
    if any(not value.is_finite() or value < 0 for value in values):
        raise ValueError("risk inputs must be finite and nonnegative")
    if exposure_reserved > snapshot.reserved_notional:
        raise ValueError("exposure reservations exceed global reservations")
    if price == 0 or step == 0 or max_quantity < min_quantity:
        raise ValueError("invalid instrument constraints")
    if limits.max_snapshot_age_ns < 0:
        raise ValueError("invalid freshness policy")
    if not snapshot.reconciled:
        return Admission(None, "UNRECONCILED_ACCOUNT")
    if not 0 <= decision_ns - snapshot.as_of_ns <= limits.max_snapshot_age_ns:
        return Admission(None, "STALE_OR_FUTURE_ACCOUNT")
    capacity = min(
        requested_notional,
        limits.max_order_notional,
        snapshot.equity * limits.max_gross_fraction
        - snapshot.gross_notional
        - snapshot.reserved_notional,
        snapshot.equity * limits.max_exposure_fraction
        - snapshot.exposure_notional
        - exposure_reserved,
    )
    if capacity <= 0:
        return Admission(None, "NO_CAPACITY")
    quantity = min(
        (capacity / price / step).to_integral_value(rounding=ROUND_DOWN) * step,
        (max_quantity / step).to_integral_value(rounding=ROUND_DOWN) * step,
    )
    if quantity <= 0 or quantity < min_quantity or quantity * price < min_notional:
        return Admission(None, "BELOW_VENUE_MINIMUM")
    return Admission(quantity, "PORTFOLIO_FEASIBLE")
