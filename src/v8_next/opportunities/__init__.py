"""V8.3 Canonical Opportunity Sovereignty Domain (Rust opportunity parity)."""

from v8_next.opportunities.book import OpportunityBook
from v8_next.opportunities.exposure import (
    ExposureResolver,
    SymbolDescriptor,
    UnresolvedExposureError,
)
from v8_next.opportunities.models import (
    EconomicExposureStructure,
    ExposureDirection,
    ExposureLeg,
    HorizonClass,
    IdentityStatus,
    InstrumentType,
    OpportunityRecord,
    OpportunityStatus,
    PayoffStructure,
)

__all__ = [
    "EconomicExposureStructure",
    "ExposureDirection",
    "ExposureLeg",
    "ExposureResolver",
    "HorizonClass",
    "IdentityStatus",
    "InstrumentType",
    "OpportunityBook",
    "OpportunityRecord",
    "OpportunityStatus",
    "PayoffStructure",
    "SymbolDescriptor",
    "UnresolvedExposureError",
]
