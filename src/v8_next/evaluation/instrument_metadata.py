"""Venue instrument filters for multi-symbol measurement (cross-market validation).

Why this module exists
----------------------
:func:`v8_next.adapters.portfolio_backtest._instrument` was written for the single
authorized BTCUSDT development slice and hard-codes BTC-calibrated filters:
``price_precision=2``, ``price_increment=0.01``, ``size_increment=0.001``,
``max_quantity=1000``. Measured against the real USD-M ``exchangeInfo`` filters
for the altcoin universe that is not a rounding detail, it is a different venue:

===========  ==========  ==========  =====================  =================
symbol       real tick   frozen=0.01  tick as bps of price   frozen max_qty
===========  ==========  ==========  =====================  =================
DOGEUSDT     0.00001     0.00001     450 bps at 0.22        5000 units needed
ADAUSDT      0.0001      0.01        1110 bps at 0.90       1111 units needed
XRPUSDT      0.0001      0.01        410 bps at 2.40       417 units
===========  ==========  ==========  =====================  =================

A 2-decimal price grid on a 0.22 instrument quantises every fill by up to 4.5 % of
price, and ``max_quantity=1000`` rejects every DOGE/ADA order outright. Both are
measurement artefacts, not strategy behaviour, so the frozen defaults are kept
byte-identical for BTC and the venue filters are supplied explicitly per symbol.

Semantic parity
---------------
``instrument_filters=None`` reproduces the frozen BTC values exactly (same
precision, same increments, same ``min_notional=1``, same ``margin_maint``), so
the documented BTC Q1 result is reproducible field-for-field. Passing filters
changes only *representable* prices/quantities and the engine's own order
validation; no strategy signal, threshold, stop, target or sizing formula reads
this module.

Provenance
----------
``filters_for`` returns a frozen table transcribed from
``https://fapi.binance.com/fapi/v1/exchangeInfo`` retrieved 2026-11-05 and stored
verbatim at ``reports/v9-cross-market-validation/instrument_metadata.json``.

The table is *current* venue metadata, not a historical snapshot: Binance does not
publish per-date filter archives. Two independent checks justify its use for the
2025-H1 window, and both are recorded in ``DATA_MANIFEST.md``:

* ``onboard_date_ms`` for every symbol precedes 2025-01-01, so the instrument was
  listed for the whole window.
* every OHLC value in the 2025-H1 archives is an exact multiple of the tick used
  here (verified per symbol; SOLUSDT is the one symbol whose archive carries
  4-decimal prices, so its tick is refined to 0.001 from the data itself rather
  than asserting a filter the recorded prices contradict).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Final

__all__ = [
    "InstrumentFilters",
    "LEGACY_BTC_FILTERS",
    "VENUE_FILTERS",
    "InstrumentFiltersMissing",
    "filters_for",
]


class InstrumentFiltersMissing(LookupError):
    """No verified filter table entry for the requested symbol."""

    code = "INSTRUMENT_FILTERS_MISSING"

    def __init__(self, symbol: str) -> None:
        super().__init__(
            f"{self.code}: no verified venue filters for {symbol!r}; refusing to "
            "inherit BTC precision for another instrument"
        )
        self.symbol = symbol


def _decimals(value: Decimal) -> int:
    """Decimal places implied by ``value`` when it is a power of ten (0 for 1)."""
    exponent = value.normalize().as_tuple().exponent
    if not isinstance(exponent, int):
        raise ValueError(f"{value} is not a decimal increment")
    return 0 if exponent >= 0 else -exponent


@dataclass(frozen=True)
class InstrumentFilters:
    """The subset of venue instrument metadata the backtest engine must honour."""

    symbol: str
    base_currency: str
    price_precision: int
    price_increment: Decimal
    size_precision: int
    size_increment: Decimal
    min_quantity: Decimal
    max_quantity: Decimal
    min_notional: Decimal
    onboard_date_ms: int
    maintenance_margin: Decimal
    #: ``exchangeInfo`` when the vendor value is usable, ``data_derived`` when the
    #: 2025-H1 archive proves a finer grid than the current venue filter.
    price_increment_source: str = "exchangeInfo"

    def __post_init__(self) -> None:
        # NautilusTrader quantises a ``Price``/``Quantity`` to *precision* and then
        # rejects any value that is not an exact multiple of the increment. A
        # precision wider than the increment (e.g. Binance's DOGEUSDT
        # ``pricePrecision=6`` against ``tickSize=0.000010``) therefore makes half
        # of all computed stop/target prices unfillable, so the two fields must
        # describe the same grid.
        if _decimals(self.price_increment) != self.price_precision:
            raise ValueError(
                f"{self.symbol}: price_precision={self.price_precision} does not match "
                f"price_increment={self.price_increment} (needs "
                f"{_decimals(self.price_increment)})"
            )
        if _decimals(self.size_increment) != self.size_precision:
            raise ValueError(
                f"{self.symbol}: size_precision={self.size_precision} does not match "
                f"size_increment={self.size_increment} (needs "
                f"{_decimals(self.size_increment)})"
            )
        for name in ("price_increment", "size_increment", "min_quantity", "max_quantity"):
            if getattr(self, name) <= 0:
                raise ValueError(f"{self.symbol}: {name} must be positive")
        if self.min_notional < 0:
            raise ValueError(f"{self.symbol}: min_notional cannot be negative")
        if self.min_quantity > self.max_quantity:
            raise ValueError(f"{self.symbol}: min_quantity exceeds max_quantity")


def _filters(
    symbol: str,
    base: str,
    price_precision: int,
    price_increment: str,
    size_precision: int,
    size_increment: str,
    min_quantity: str,
    max_quantity: str,
    min_notional: str,
    onboard_date_ms: int,
    maintenance_margin: str = "0.0125",
    *,
    price_increment_source: str = "exchangeInfo",
) -> InstrumentFilters:
    return InstrumentFilters(
        symbol=symbol,
        base_currency=base,
        price_precision=price_precision,
        price_increment=Decimal(price_increment),
        size_precision=size_precision,
        size_increment=Decimal(size_increment),
        min_quantity=Decimal(min_quantity),
        max_quantity=Decimal(max_quantity),
        min_notional=Decimal(min_notional),
        onboard_date_ms=onboard_date_ms,
        maintenance_margin=Decimal(maintenance_margin),
        price_increment_source=price_increment_source,
    )


#: The frozen BTCUSDT development-slice instrument exactly as
#: ``portfolio_backtest._instrument`` builds it. Used when no filters are passed,
#: so the documented BTC Q1 measurement stays bit-for-bit reproducible.
LEGACY_BTC_FILTERS: Final[InstrumentFilters] = InstrumentFilters(
    symbol="BTCUSDT",
    base_currency="BTC",
    price_precision=2,
    price_increment=Decimal("0.01"),
    size_precision=3,
    size_increment=Decimal("0.001"),
    min_quantity=Decimal("0.001"),
    max_quantity=Decimal("1000"),
    min_notional=Decimal("1"),
    onboard_date_ms=1567965300000,
    maintenance_margin=Decimal("0.05"),
    price_increment_source="frozen_sim_arm",
)

#: Verified USD-M perpetual filters, retrieved from ``fapi/v1/exchangeInfo`` on
#: 2026-11-05. ``maintenance_margin`` is the venue ``liquidationFee``.
VENUE_FILTERS: Final[dict[str, InstrumentFilters]] = {
    item.symbol: item
    for item in (
        _filters("BTCUSDT", "BTC", 2, "0.01", 3, "0.001", "0.001", "1000", "50", 1567965300000,
                 "0.0125", price_increment_source="window_data_derived"),
        _filters("ETHUSDT", "ETH", 2, "0.01", 3, "0.001", "0.001", "10000", "20", 1574840700000,
                 "0.0125"),
        _filters("SOLUSDT", "SOL", 4, "0.0001", 2, "0.01", "0.01", "1000000", "5", 1600066800000,
                 "0.015", price_increment_source="window_data_derived"),
        _filters("BNBUSDT", "BNB", 2, "0.01", 2, "0.01", "0.01", "100000", "5", 1581321600000),
        _filters("XRPUSDT", "XRP", 4, "0.0001", 1, "0.1", "0.1", "10000000", "5", 1578298800000),
        _filters("DOGEUSDT", "DOGE", 5, "0.00001", 0, "1", "1", "300000000", "5", 1594371600000,
                 "0.015"),
        _filters("ADAUSDT", "ADA", 4, "0.0001", 0, "1", "1", "30000000", "5", 1580457600000),
        _filters("AVAXUSDT", "AVAX", 3, "0.001", 0, "1", "1", "500000", "5", 1600844400000,
                 "0.015"),
        _filters("LINKUSDT", "LINK", 3, "0.001", 2, "0.01", "0.01", "700000", "20", 1579248000000),
        _filters("DOTUSDT", "DOT", 4, "0.0001", 1, "0.1", "0.1", "3000000", "5", 1598079600000),
        _filters("LTCUSDT", "LTC", 2, "0.01", 3, "0.001", "0.001", "170000", "20", 1578557100000),
    )
}


def filters_for(symbol: str) -> InstrumentFilters:
    """Verified filters for ``symbol``; refuses to guess instead of defaulting."""
    try:
        return VENUE_FILTERS[symbol.upper()]
    except KeyError:
        raise InstrumentFiltersMissing(symbol) from None
