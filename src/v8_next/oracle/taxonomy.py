"""Controlled oracle taxonomy and refusal vocabulary — port of v8-core/src/oracle/taxonomy.rs.

Public vocabulary is closed: the refusal codes and the four orthogonal dimensions are the Rust
ones, spelled the same, so a report from either system reads identically. Note that the Rust
tree carries two distinct types named ``CounterfactualAuthority`` (an enum in taxonomy.rs, a
struct in authority.rs); this port keeps the same split, and the package exports the enum as
``CounterfactualAuthorityLevel`` to avoid the ambiguity at the boundary.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum, StrEnum


class OracleRole(StrEnum):
    PARITY = "PARITY"
    HINDSIGHT = "HINDSIGHT"
    TARGET = "TARGET"


class AuthorityLevel(IntEnum):
    """Ordered: an output may not exceed the authority of its inputs."""

    L1 = 0
    L2 = 1
    L3 = 2
    LIVE_RECEIPT = 3


class Identifiability(StrEnum):
    IDENTIFIED = "IDENTIFIED"
    PARTIALLY_IDENTIFIED = "PARTIALLY_IDENTIFIED"
    MODEL_DERIVED = "MODEL_DERIVED"
    NOT_IDENTIFIABLE = "NOT_IDENTIFIABLE"


class ValueNotion(IntEnum):
    RETROSPECTIVE = 0
    REPLICATION = 1
    PROSPECTIVE_SHADOW = 2
    LIVE_REALIZED = 3


class CounterfactualAuthority(StrEnum):
    """Axis 3: counterfactual and microstructure authority level."""

    IDENTIFIED = "IDENTIFIED"
    PARTIALLY_IDENTIFIED = "PARTIALLY_IDENTIFIED"
    MODEL_DERIVED = "MODEL_DERIVED"
    NOT_IDENTIFIABLE = "NOT_IDENTIFIABLE"


class VerificationDimension(StrEnum):
    """Axis 1: code and implementation verification."""

    CONTRACT_VERIFIED = "CONTRACT_VERIFIED"
    IMPLEMENTATION_PARITY = "IMPLEMENTATION_PARITY"
    METAMORPHIC_INVARIANT = "METAMORPHIC_INVARIANT"


class EconomicEvidenceStage(StrEnum):
    """Axis 2: economic evidence and promotion stage (Constitution Rule 12)."""

    NO_ECONOMIC_CLAIM = "NO_ECONOMIC_CLAIM"
    RECOVERABLE_WITHIN_CLASS = "RECOVERABLE_WITHIN_CLASS"
    PROMOTABLE_WITHIN_CONTRACT = "PROMOTABLE_WITHIN_CONTRACT"
    SHADOW_SUPPORTED = "SHADOW_SUPPORTED"
    LIVE_SUPPORTED = "LIVE_SUPPORTED"


class StatisticalVerdict(StrEnum):
    """Axis 4: statistical hypothesis testing verdict."""

    SUPPORTED = "SUPPORTED"
    REFUTED = "REFUTED"
    INCONCLUSIVE_UNDERPOWERED = "INCONCLUSIVE_UNDERPOWERED"


class OracleRefusal(StrEnum):
    """Canonical fail-closed vocabulary; a refusal is typed, never an ad-hoc string."""

    MISSING_DECISION_TIME_DATA = "MISSING_DECISION_TIME_DATA"
    OUT_OF_SUPPORT_ACTION = "OUT_OF_SUPPORT_ACTION"
    EXECUTION_AUTHORITY_TOO_WEAK = "EXECUTION_AUTHORITY_TOO_WEAK"
    UNDEFINED_FUTURE = "UNDEFINED_FUTURE"
    NON_IDENTIFIABLE_FILL = "NON_IDENTIFIABLE_FILL"
    CONSTRAINT_INFEASIBLE = "CONSTRAINT_INFEASIBLE"
    PROTECTED_SLICE_ALREADY_CONSUMED = "PROTECTED_SLICE_ALREADY_CONSUMED"
    MODEL_ONLY_COUNTERFACTUAL = "MODEL_ONLY_COUNTERFACTUAL"
    INSUFFICIENT_SUPPORT = "INSUFFICIENT_SUPPORT"

    def code(self) -> str:
        return self.value


#: the same vocabulary, seen from the "why is this unknown" side
UnknownReasonCode = OracleRefusal


@dataclass(frozen=True)
class OracleContext:
    """The declared context a counterfactual is evaluated inside."""

    role: OracleRole
    authority: AuthorityLevel
    information_contract_id: str
    opportunity_universe_id: str
    utility_contract_id: str
    policy_class_id: str
    cost_model_id: str
    capacity_model_id: str
    environment_target_id: str


class AuthorityError(Exception):
    """Invalid taxonomy state or a Rule 12 violation."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class AuditState:
    """The full orthogonal product-space state (I1 invariant)."""

    verification: VerificationDimension
    economic_stage: EconomicEvidenceStage
    counterfactual_authority: CounterfactualAuthority
    statistical_verdict: StatisticalVerdict

    def validate_rule12(self) -> None:
        """Constitution Rule 12: no uncertified economic edge claim.

        A supported or live-supported stage may not rest on a model-derived or
        not-identifiable counterfactual, nor on a verdict that is not supported.
        """
        claims_edge = self.economic_stage in (
            EconomicEvidenceStage.SHADOW_SUPPORTED,
            EconomicEvidenceStage.LIVE_SUPPORTED,
        )
        weak_authority = self.counterfactual_authority in (
            CounterfactualAuthority.MODEL_DERIVED,
            CounterfactualAuthority.NOT_IDENTIFIABLE,
        )
        if claims_edge and (weak_authority or self.statistical_verdict is not StatisticalVerdict.SUPPORTED):
            raise AuthorityError(
                "UNCERTIFIED_ECONOMIC_CLAIM",
                f"Cannot claim {self.economic_stage.value} when authority is "
                f"{self.counterfactual_authority.value} and statistical verdict is "
                f"{self.statistical_verdict.value}",
            )
