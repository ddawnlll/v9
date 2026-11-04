"""Counterfactual authority and fail-closed outcome semantics — port of
v8-core/src/oracle/authority.rs (TARGET_ORACLE_SPEC §8, §16, §18.1).

Authority level and identifiability status are strictly orthogonal. ``OracleOutcome`` is a
closed set of four shapes and **UNKNOWN is first-class**: it carries a typed refusal and never
collapses into a zero point estimate. Every constructor refuses the states the Rust side
refuses, with the same refusal code (``INSUFFICIENT_SUPPORT``).
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from enum import StrEnum

from v8_next.oracle.artifacts import OracleEvaluationRecord
from v8_next.oracle.taxonomy import (
    AuthorityLevel,
    Identifiability,
    OracleContext,
    OracleRefusal,
    ValueNotion,
)


class OracleRefused(Exception):
    """Raised when an outcome is asked for a state the data does not support."""

    def __init__(self, refusal: OracleRefusal, message: str = "") -> None:
        super().__init__(f"{refusal.value}{': ' + message if message else ''}")
        self.refusal = refusal


@dataclass(frozen=True)
class CounterfactualAuthority:
    """Explicit counterfactual authority composition (the Rust *struct*, not the axis enum)."""

    oracle_authority_level: AuthorityLevel
    identifiability_status: Identifiability
    support_rule_id: str
    environment_model_id: str
    assumptions: tuple[str, ...] = ()

    def identity(self) -> str:
        payload = json.dumps(
            {
                "oracle_authority_level": self.oracle_authority_level.name,
                "identifiability_status": self.identifiability_status.value,
                "support_rule_id": self.support_rule_id,
                "environment_model_id": self.environment_model_id,
                "assumptions": list(self.assumptions),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha1(f"counterfactual-authority-v1|{payload}".encode()).hexdigest()

    def is_identified(self) -> bool:
        return self.identifiability_status is Identifiability.IDENTIFIED

    def as_dict(self) -> dict[str, object]:
        return {
            "oracle_authority_level": self.oracle_authority_level.name,
            "identifiability_status": self.identifiability_status.value,
            "support_rule_id": self.support_rule_id,
            "environment_model_id": self.environment_model_id,
            "assumptions": list(self.assumptions),
            "identity": self.identity(),
        }


class OracleOutcomeKind(StrEnum):
    IDENTIFIED = "IDENTIFIED"
    PARTIALLY_IDENTIFIED = "PARTIALLY_IDENTIFIED"
    MODEL_DERIVED = "MODEL_DERIVED"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class OracleOutcome:
    """A counterfactual answer, or a typed refusal. UNKNOWN is never a zero."""

    kind: OracleOutcomeKind
    authority: CounterfactualAuthority
    point_estimate: float | None = None
    lower_bound: float | None = None
    upper_bound: float | None = None
    refusal: OracleRefusal | None = None
    supporting_evidence: dict[str, object] = field(default_factory=dict)

    # -- constructors, each refusing what the Rust side refuses ----------------------

    @classmethod
    def identified(cls, point: float, authority: CounterfactualAuthority) -> OracleOutcome:
        if authority.identifiability_status is not Identifiability.IDENTIFIED:
            raise OracleRefused(OracleRefusal.INSUFFICIENT_SUPPORT, "authority is not identified")
        if not math.isfinite(point):
            raise OracleRefused(OracleRefusal.INSUFFICIENT_SUPPORT, "non-finite point estimate")
        return cls(OracleOutcomeKind.IDENTIFIED, authority, point_estimate=point)

    @classmethod
    def partially_identified(
        cls, lower: float, upper: float, authority: CounterfactualAuthority
    ) -> OracleOutcome:
        if authority.identifiability_status is not Identifiability.PARTIALLY_IDENTIFIED:
            raise OracleRefused(OracleRefusal.INSUFFICIENT_SUPPORT, "authority is not partial")
        if not (math.isfinite(lower) and math.isfinite(upper)) or lower > upper:
            raise OracleRefused(OracleRefusal.INSUFFICIENT_SUPPORT, "invalid bounds")
        return cls(
            OracleOutcomeKind.PARTIALLY_IDENTIFIED, authority, lower_bound=lower, upper_bound=upper
        )

    @classmethod
    def model_derived(
        cls,
        point: float | None,
        lower: float | None,
        upper: float | None,
        authority: CounterfactualAuthority,
    ) -> OracleOutcome:
        if authority.identifiability_status is not Identifiability.MODEL_DERIVED:
            raise OracleRefused(OracleRefusal.INSUFFICIENT_SUPPORT, "authority is not model-derived")
        return cls(
            OracleOutcomeKind.MODEL_DERIVED,
            authority,
            point_estimate=point,
            lower_bound=lower,
            upper_bound=upper,
        )

    @classmethod
    def unknown(
        cls, refusal: OracleRefusal, authority: CounterfactualAuthority
    ) -> OracleOutcome:
        return cls(OracleOutcomeKind.UNKNOWN, authority, refusal=refusal)

    # -- read side --------------------------------------------------------------------

    def is_unknown(self) -> bool:
        return self.kind is OracleOutcomeKind.UNKNOWN

    def is_identified(self) -> bool:
        return self.kind is OracleOutcomeKind.IDENTIFIED

    def bounds(self) -> tuple[float, float] | None:
        if self.lower_bound is None or self.upper_bound is None:
            return None
        return (self.lower_bound, self.upper_bound)

    def refusal_reason(self) -> OracleRefusal | None:
        return self.refusal

    def to_evaluation_record(
        self,
        context: OracleContext,
        candidate_population_hash: str,
        action_manifest_hash: str,
        simulator_or_receipt_hash: str,
        code_hash: str,
        config_hash: str,
        value_notion: ValueNotion,
        lineage_id: str,
    ) -> OracleEvaluationRecord:
        """Bind this outcome to its authority lineage (port of the Rust method).

        UNKNOWN carries no point estimate and no bounds — the refusal is the record.
        """
        if self.kind is OracleOutcomeKind.IDENTIFIED:
            point, lower, upper = self.point_estimate, None, None
        elif self.kind is OracleOutcomeKind.PARTIALLY_IDENTIFIED:
            point, lower, upper = None, self.lower_bound, self.upper_bound
        elif self.kind is OracleOutcomeKind.MODEL_DERIVED:
            point, lower, upper = self.point_estimate, self.lower_bound, self.upper_bound
        else:
            point, lower, upper = None, None, None
        record = OracleEvaluationRecord(
            oracle_role=context.role,
            authority_level=self.authority.oracle_authority_level,
            identifiability_status=self.authority.identifiability_status,
            information_contract_id=context.information_contract_id,
            opportunity_universe_id=context.opportunity_universe_id,
            utility_contract_id=context.utility_contract_id,
            policy_class_id=context.policy_class_id,
            cost_model_id=context.cost_model_id,
            capacity_model_id=context.capacity_model_id,
            environment_target_id=context.environment_target_id,
            candidate_population_hash=candidate_population_hash,
            action_manifest_hash=action_manifest_hash,
            simulator_or_receipt_hash=simulator_or_receipt_hash,
            code_hash=code_hash,
            config_hash=config_hash,
            value_notion=value_notion,
            point_estimate=point,
            lower_bound=lower,
            upper_bound=upper,
            refusal_reason=None if self.refusal is None else self.refusal.value,
            assumptions=list(self.authority.assumptions),
            lineage_id=lineage_id,
        )
        record.bind_identity()
        return record

    def as_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind.value,
            "point_estimate": self.point_estimate,
            "bounds": None if self.bounds() is None else list(self.bounds() or ()),
            "refusal": None if self.refusal is None else self.refusal.value,
            "authority": self.authority.as_dict(),
            "supporting_evidence": self.supporting_evidence,
            "note": (
                "an UNKNOWN outcome carries a typed refusal; it is never a zero point estimate"
                if self.is_unknown()
                else None
            ),
        }
