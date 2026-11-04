"""NT risk primitives wired into v8 sizing decisions.

Two independent guards, both opt-in and both reported in the decision record:

1. **Risk-fraction sizing** (``FixedRiskSizer``): quantity from entry/stop
   distance and a fixed equity fraction, instead of the flat ``target_notional``.
2. **Max-notional cap** (``RiskEngineConfig.max_notional_per_order``
   equivalent): orders above the cap are denied before submission and counted.

Neither is silent: the sizing mode and any denial appear in the decision, and
when both are unset behaviour is byte-identical to the previous path.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from nautilus_trader.model import InstrumentId, Money, Price
from nautilus_trader.risk import FixedRiskSizer


def risk_sized_quantity(
    instrument: Any,
    entry: Price,
    stop_loss: Price,
    equity: Money,
    risk_fraction: Decimal,
) -> Any:
    """Quantity risking ``risk_fraction`` of equity over entry->stop.

    Raises ValueError for out-of-range fractions instead of sizing nonsense.
    """
    if not Decimal(0) < risk_fraction <= Decimal(1):
        raise ValueError("risk_fraction must be in (0, 1]")
    # unit_batch_size must be the instrument step: the sizer rounds down to
    # whole batches, and the default batch of 1.0 would floor any fractional
    # crypto quantity to zero (measured: $100 risk / $2000 stop -> 0.000).
    step = instrument.size_increment.as_decimal()
    return FixedRiskSizer(instrument).calculate(
        entry, stop_loss, equity, risk_fraction, unit_batch_size=step
    )


def notional_of(quantity: Any, price: Price) -> Decimal:
    """Notional of a quantity at a price, in quote currency."""
    qty = quantity.as_decimal() if hasattr(quantity, "as_decimal") else Decimal(str(quantity))
    px = price.as_decimal() if hasattr(price, "as_decimal") else Decimal(str(price))
    return qty * px


def check_max_notional(
    quantity: Any,
    price: Price,
    max_notional: Decimal | None,
) -> tuple[bool, Decimal]:
    """Return (allowed, notional). None cap means allowed (no guard configured)."""
    notional = notional_of(quantity, price)
    if max_notional is None:
        return True, notional
    return notional <= max_notional, notional


def instrument_id_str(instrument_id: InstrumentId) -> str:
    return str(instrument_id)
