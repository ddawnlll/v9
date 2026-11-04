"""Economic exposure identity, alias resolution, and False-Collapse Protection (Rule 16, 26).

Maps venue tickers to canonical economic factor exposures and prevents basis spreads
from false collapsing into naive scalar zero-exposure positions.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from v8_next.opportunities.models import (
    EconomicExposureStructure,
    ExposureDirection,
    InstrumentType,
)


class UnresolvedExposureError(LookupError):
    """Raised when a symbol/venue combination cannot be resolved to an exposure."""

    def __init__(self, symbol: str, venue: str, reason: str = "") -> None:
        super().__init__(f"Unresolved exposure for {symbol} on {venue}: {reason}")
        self.symbol = symbol
        self.venue = venue
        self.reason = reason


@dataclass(frozen=True)
class SymbolDescriptor:
    """Metadata mapping for a known symbol/ticker on a venue."""

    symbol: str
    venue: str
    underlying_factor: str
    instrument_type: InstrumentType
    settlement_asset: str


class ExposureResolver:
    """Resolves venue tickers and pairs to canonical EconomicExposureStructure."""

    _DEFAULT_UNIVERSE: ClassVar[tuple[tuple[str, str, str, InstrumentType, str], ...]] = (
        ("BTCUSDT", "binance-um", "BTC", InstrumentType.PERPETUAL, "USDT"),
        ("BTCUSDT", "binance-spot", "BTC", InstrumentType.SPOT, "USDT"),
        ("BTC_ALIAS1", "binance-um", "BTC", InstrumentType.PERPETUAL, "USDT"),
        ("BTC_ALIAS2", "binance-um", "BTC", InstrumentType.PERPETUAL, "USDT"),
        ("BTC_ALIAS3", "binance-um", "BTC", InstrumentType.PERPETUAL, "USDT"),
        ("ETHUSDT", "binance-um", "ETH", InstrumentType.PERPETUAL, "USDT"),
        ("ETHUSDT", "binance-spot", "ETH", InstrumentType.SPOT, "USDT"),
        ("SOLUSDT", "binance-um", "SOL", InstrumentType.PERPETUAL, "USDT"),
        ("SOLUSDT", "binance-spot", "SOL", InstrumentType.SPOT, "USDT"),
        ("BNBUSDT", "binance-um", "BNB", InstrumentType.PERPETUAL, "USDT"),
        ("XRPUSDT", "binance-um", "XRP", InstrumentType.PERPETUAL, "USDT"),
        ("DOGEUSDT", "binance-um", "DOGE", InstrumentType.PERPETUAL, "USDT"),
        ("AVAXUSDT", "binance-um", "AVAX", InstrumentType.PERPETUAL, "USDT"),
    )

    def __init__(self) -> None:
        self._descriptors: dict[tuple[str, str], SymbolDescriptor] = {}
        self.register_standard_crypto_universe()

    def register_standard_crypto_universe(self) -> None:
        for sym, ven, fac, itype, sett in self._DEFAULT_UNIVERSE:
            self.register(
                SymbolDescriptor(
                    symbol=sym,
                    venue=ven,
                    underlying_factor=fac,
                    instrument_type=itype,
                    settlement_asset=sett,
                )
            )

    def register(self, descriptor: SymbolDescriptor) -> None:
        self._descriptors[(descriptor.symbol, descriptor.venue)] = descriptor

    def resolve_ticker(
        self,
        symbol: str,
        venue: str,
        direction: ExposureDirection,
    ) -> EconomicExposureStructure:
        desc = self._descriptors.get((symbol, venue))
        if desc is None:
            raise UnresolvedExposureError(
                symbol=symbol,
                venue=venue,
                reason="Symbol descriptor not registered in ExposureResolver",
            )

        if desc.instrument_type == InstrumentType.SPOT:
            return EconomicExposureStructure.single_spot(
                symbol=desc.symbol,
                factor=desc.underlying_factor,
                venue=desc.venue,
                settlement=desc.settlement_asset,
                direction=direction,
            )
        elif desc.instrument_type == InstrumentType.PERPETUAL:
            return EconomicExposureStructure.single_perp(
                symbol=desc.symbol,
                factor=desc.underlying_factor,
                venue=desc.venue,
                settlement=desc.settlement_asset,
                direction=direction,
            )
        else:
            raise UnresolvedExposureError(
                symbol=symbol,
                venue=venue,
                reason=f"Unsupported instrument type {desc.instrument_type}",
            )

    def resolve_basis_spread(
        self,
        spot_symbol: str,
        spot_venue: str,
        perp_symbol: str,
        perp_venue: str,
    ) -> EconomicExposureStructure:
        spot_desc = self._descriptors.get((spot_symbol, spot_venue))
        if spot_desc is None:
            raise UnresolvedExposureError(spot_symbol, spot_venue, "Spot leg descriptor missing")

        perp_desc = self._descriptors.get((perp_symbol, perp_venue))
        if perp_desc is None:
            raise UnresolvedExposureError(perp_symbol, perp_venue, "Perp leg descriptor missing")

        if spot_desc.underlying_factor != perp_desc.underlying_factor:
            raise ValueError(
                f"Cannot build basis spread on mismatched factors: "
                f"{spot_desc.underlying_factor} vs {perp_desc.underlying_factor}"
            )

        return EconomicExposureStructure.spot_perp_basis(
            factor=spot_desc.underlying_factor,
            spot_symbol=spot_desc.symbol,
            spot_venue=spot_desc.venue,
            perp_symbol=perp_desc.symbol,
            perp_venue=perp_desc.venue,
            settlement=perp_desc.settlement_asset,
        )
