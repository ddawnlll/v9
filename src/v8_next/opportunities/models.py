"""Canonical Opportunity & Exposure Data Models (Rule 4, 16, 26).

Epistemic Invariant:
    Symbol != Instrument != EconomicExposure != Opportunity != Trade
    Market creates the Opportunity first; Observers attach witness evidence subsequently.
"""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class OpportunityStatus(StrEnum):
    """Lifecycle status of an opportunity within the book."""

    CANDIDATE = "CANDIDATE"
    CONFIRMED = "CONFIRMED"
    ADMITTED = "ADMITTED"
    INVALIDATED = "INVALIDATED"
    EXPIRED = "EXPIRED"


class IdentityStatus(StrEnum):
    """Status of the opportunity boundary definition (matching Rust IdentityStatus)."""

    CANONICAL = "CANONICAL"
    AMBIGUOUS = "AMBIGUOUS"
    UNKNOWN = "UNKNOWN"
    FALSIFIED = "FALSIFIED"


class InstrumentType(StrEnum):
    """Instrument category (matching Rust InstrumentType)."""

    SPOT = "SPOT"
    PERPETUAL = "PERPETUAL"
    DELIVERY_FUTURE = "DELIVERY_FUTURE"
    OPTION = "OPTION"
    SYNTHETIC_SPREAD = "SYNTHETIC_SPREAD"


class ExposureDirection(StrEnum):
    """Exposure directionality (matching Rust ExposureDirection)."""

    LONG = "LONG"
    SHORT = "SHORT"
    NEUTRAL = "NEUTRAL"
    BIDIRECTIONAL = "BIDIRECTIONAL"


class PayoffStructure(StrEnum):
    """Payoff curvature / complexity (matching Rust PayoffStructure)."""

    LINEAR = "LINEAR"
    CONVEX = "CONVEX"
    NON_LINEAR = "NON_LINEAR"
    MULTI_LEG = "MULTI_LEG"


class HorizonClass(StrEnum):
    """Expected structural duration (matching Rust HorizonClass)."""

    INTRADAY = "INTRADAY"
    MULTI_DAY = "MULTI_DAY"
    STRUCTURAL = "STRUCTURAL"


class ExposureLeg(BaseModel):
    """One leg of a multi-leg or single-asset exposure structure (Rule 26)."""

    model_config = ConfigDict(frozen=True)

    symbol: str
    venue: str
    instrument_type: InstrumentType
    weight: float


class EconomicExposureStructure(BaseModel):
    """Canonical Economic Exposure Structure (Primitive 2 of 7 / Rust parity)."""

    model_config = ConfigDict(frozen=True)

    exposure_id: str
    underlying_factors: tuple[str, ...]
    instrument_type: InstrumentType
    venue: str
    settlement_asset: str
    direction: ExposureDirection
    payoff_structure: PayoffStructure
    legs: tuple[ExposureLeg, ...]
    horizon_class: HorizonClass

    @classmethod
    def create(
        cls,
        underlying_factors: tuple[str, ...],
        instrument_type: InstrumentType,
        venue: str,
        settlement_asset: str,
        direction: ExposureDirection,
        payoff_structure: PayoffStructure,
        legs: tuple[ExposureLeg, ...],
        horizon_class: HorizonClass,
    ) -> EconomicExposureStructure:
        if not underlying_factors:
            raise ValueError("underlying_factors cannot be empty")
        if not legs:
            raise ValueError("exposure legs cannot be empty")

        sorted_factors = tuple(sorted(underlying_factors))
        legs_data = [
            [leg.symbol, leg.venue, leg.instrument_type.value, float(leg.weight)]
            for leg in legs
        ]
        canon_obj = [
            "EconomicExposureStructure",
            list(sorted_factors),
            instrument_type.value,
            venue,
            settlement_asset,
            direction.value,
            payoff_structure.value,
            horizon_class.value,
            legs_data,
        ]
        canon_bytes = json.dumps(canon_obj, separators=(",", ":")).encode("utf-8")
        exposure_id = hashlib.sha256(canon_bytes).hexdigest()

        return cls(
            exposure_id=exposure_id,
            underlying_factors=sorted_factors,
            instrument_type=instrument_type,
            venue=venue,
            settlement_asset=settlement_asset,
            direction=direction,
            payoff_structure=payoff_structure,
            legs=legs,
            horizon_class=horizon_class,
        )

    @classmethod
    def single_spot(
        cls,
        symbol: str,
        factor: str,
        venue: str,
        settlement: str,
        direction: ExposureDirection,
    ) -> EconomicExposureStructure:
        return cls.create(
            underlying_factors=(factor,),
            instrument_type=InstrumentType.SPOT,
            venue=venue,
            settlement_asset=settlement,
            direction=direction,
            payoff_structure=PayoffStructure.LINEAR,
            legs=(ExposureLeg(symbol=symbol, venue=venue, instrument_type=InstrumentType.SPOT, weight=1.0),),
            horizon_class=HorizonClass.INTRADAY,
        )

    @classmethod
    def single_perp(
        cls,
        symbol: str,
        factor: str,
        venue: str,
        settlement: str,
        direction: ExposureDirection,
    ) -> EconomicExposureStructure:
        return cls.create(
            underlying_factors=(factor,),
            instrument_type=InstrumentType.PERPETUAL,
            venue=venue,
            settlement_asset=settlement,
            direction=direction,
            payoff_structure=PayoffStructure.LINEAR,
            legs=(ExposureLeg(symbol=symbol, venue=venue, instrument_type=InstrumentType.PERPETUAL, weight=1.0),),
            horizon_class=HorizonClass.INTRADAY,
        )

    @classmethod
    def spot_perp_basis(
        cls,
        factor: str,
        spot_symbol: str,
        spot_venue: str,
        perp_symbol: str,
        perp_venue: str,
        settlement: str,
    ) -> EconomicExposureStructure:
        """Rule 26 False-Collapse Protection: Spot-Perp Basis multi-leg structure."""
        return cls.create(
            underlying_factors=(factor, f"{factor}_BASIS"),
            instrument_type=InstrumentType.SYNTHETIC_SPREAD,
            venue=perp_venue,
            settlement_asset=settlement,
            direction=ExposureDirection.NEUTRAL,
            payoff_structure=PayoffStructure.MULTI_LEG,
            legs=(
                ExposureLeg(symbol=spot_symbol, venue=spot_venue, instrument_type=InstrumentType.SPOT, weight=1.0),
                ExposureLeg(symbol=perp_symbol, venue=perp_venue, instrument_type=InstrumentType.PERPETUAL, weight=-1.0),
            ),
            horizon_class=HorizonClass.MULTI_DAY,
        )

    @property
    def is_basis_or_spread(self) -> bool:
        return (
            self.payoff_structure == PayoffStructure.MULTI_LEG
            or self.instrument_type == InstrumentType.SYNTHETIC_SPREAD
            or len(self.legs) > 1
        )

    @property
    def gross_leg_weight(self) -> float:
        return sum(abs(leg.weight) for leg in self.legs)


class OpportunityRecord(BaseModel):
    """Canonical Point-in-Time Opportunity Record (Rust OpportunityEpisode parity).

    Maintains lifecycle state, price invalidation boundaries, and time bounds.
    """

    model_config = ConfigDict(frozen=True)

    opportunity_id: str
    exposure: EconomicExposureStructure
    instrument_id: str
    direction: ExposureDirection
    entry_price: Decimal
    stop_price: Decimal | None = None
    target_price: Decimal | None = None
    as_of_time_ns: int
    valid_until_ns: int
    expected_horizon_bars: int = 24
    status: OpportunityStatus = OpportunityStatus.CANDIDATE
    identity_status: IdentityStatus = IdentityStatus.CANONICAL
    market_state_hash: str = ""
    lineage_hash: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def create(
        cls,
        exposure: EconomicExposureStructure,
        instrument_id: str,
        direction: ExposureDirection,
        entry_price: Decimal,
        as_of_time_ns: int,
        valid_until_ns: int,
        *,
        stop_price: Decimal | None = None,
        target_price: Decimal | None = None,
        expected_horizon_bars: int = 24,
        status: OpportunityStatus = OpportunityStatus.CANDIDATE,
        identity_status: IdentityStatus = IdentityStatus.CANONICAL,
        market_state_hash: str = "",
        lineage_hash: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> OpportunityRecord:
        if valid_until_ns < as_of_time_ns:
            raise ValueError(
                f"valid_until_ns ({valid_until_ns}) cannot precede as_of_time_ns ({as_of_time_ns})"
            )

        canon_obj = [
            "OpportunityEpisode",
            exposure.exposure_id,
            instrument_id,
            direction.value,
            str(entry_price),
            as_of_time_ns,
            valid_until_ns,
            expected_horizon_bars,
            identity_status.value,
            market_state_hash,
            lineage_hash,
        ]
        canon_bytes = json.dumps(canon_obj, separators=(",", ":")).encode("utf-8")
        opportunity_id = hashlib.sha256(canon_bytes).hexdigest()

        return cls(
            opportunity_id=opportunity_id,
            exposure=exposure,
            instrument_id=instrument_id,
            direction=direction,
            entry_price=entry_price,
            stop_price=stop_price,
            target_price=target_price,
            as_of_time_ns=as_of_time_ns,
            valid_until_ns=valid_until_ns,
            expected_horizon_bars=expected_horizon_bars,
            status=status,
            identity_status=identity_status,
            market_state_hash=market_state_hash,
            lineage_hash=lineage_hash,
            metadata=metadata or {},
        )

    def with_status(self, new_status: OpportunityStatus) -> OpportunityRecord:
        """Return a copy with modified status."""
        return self.model_copy(update={"status": new_status})

    def to_legacy_dict(self) -> dict[str, Any]:
        return {
            "opportunity_id": self.opportunity_id,
            "exposure_id": self.exposure.exposure_id,
            "instrument_id": self.instrument_id,
            "direction": self.direction.value,
            "anchor_ns": self.as_of_time_ns,
            "expires_ns": self.valid_until_ns,
            "status": self.status.value,
            "identity_status": self.identity_status.value,
            "entry_price": str(self.entry_price),
            "stop_price": str(self.stop_price) if self.stop_price is not None else None,
            "target_price": str(self.target_price) if self.target_price is not None else None,
        }
