"""Native multi-instrument equity projection at an explicit observation boundary."""

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from nautilus_trader.model import Currency, Price, Venue


@dataclass(frozen=True)
class EquityMark:
    price: Price
    event_ns: int
    observed_ns: int


def native_equity(
    cache: Any,
    marks: dict[str, EquityMark],
    *,
    venue: Venue,
    currency: Currency,
    observed_ns: int,
    max_mark_age_ns: int,
) -> tuple[Decimal, Decimal] | None:
    """Return cash and native unrealized PnL, or absence if coverage is incomplete.

    Caller freezes native state at a chosen evaluation phase. This read-only
    projection does not settle funding, infer FX rates or align event callbacks.
    """
    if observed_ns < 0 or max_mark_age_ns < 0:
        raise ValueError("invalid equity observation clock policy")
    account = cache.account_for_venue(venue)
    if account is None:
        return None
    balance = account.balance_total(currency)
    if balance is None:
        return None
    cash = balance.as_decimal()
    unrealized = Decimal(0)
    for position in cache.positions_open():
        if position.instrument_id.venue != venue:
            continue
        mark = marks.get(str(position.instrument_id))
        if (
            mark is None
            or not 0 <= mark.event_ns <= mark.observed_ns <= observed_ns
            or observed_ns - mark.event_ns > max_mark_age_ns
        ):
            return None
        if mark.price.as_decimal() <= 0:
            raise ValueError("nonpositive equity mark")
        pnl = position.unrealized_pnl(mark.price)
        if pnl.currency != currency:
            return None
        unrealized += pnl.as_decimal()
    if not cash.is_finite() or not unrealized.is_finite():
        raise ValueError("non-finite native equity")
    return cash, unrealized


def aligned_native_equity(
    cache: Any,
    marks: dict[str, EquityMark],
    *,
    required_instruments: frozenset[str],
    boundary_ns: int,
    observed_ns: int,
    venue: Venue,
    currency: Currency,
) -> tuple[Decimal, Decimal] | None:
    """Value one complete market boundary, never forward-fill absent inputs.

    Caller invokes this once at its declared account/decision phase after all
    required inputs are known. This function does not schedule or mutate events.
    Open positions outside the declared universe also prevent partial valuation.
    """
    if not required_instruments or not 0 <= boundary_ns <= observed_ns:
        raise ValueError("invalid aligned equity boundary")
    selected = {}
    for instrument in required_instruments:
        mark = marks.get(instrument)
        if mark is None or mark.event_ns != boundary_ns:
            return None
        if not boundary_ns <= mark.observed_ns <= observed_ns:
            return None
        selected[instrument] = mark
    return native_equity(
        cache,
        selected,
        venue=venue,
        currency=currency,
        observed_ns=observed_ns,
        max_mark_age_ns=observed_ns - boundary_ns,
    )
