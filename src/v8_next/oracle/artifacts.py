"""Identity-bearing oracle artifacts — port of v8-core/src/oracle/artifacts.rs.

(TARGET_ORACLE_SPEC §17: all economic artifacts are authority-bound.)

``OpportunityUniverseVersion`` freezes the finite, versioned opportunity universe; expanding
grammar, grids, instruments, timeframes, or execution modes creates a new universe version
(spec rule 8). ``OracleEvaluationRecord`` binds one counterfactual answer to its full
authority lineage. O0–O1 intentionally do not serialize these into evaluation bundles.

DIVERGENCES from the Rust module: identity digests use hashlib (sha256) with the same
domain tags; reproducible inside this port, not bit-equal to the Rust Canon digests.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

from v8_next.oracle.taxonomy import (
    AuthorityLevel,
    Identifiability,
    OracleRole,
    ValueNotion,
)

__all__ = [
    "OpportunityUniverseVersion",
    "OracleEvaluationRecord",
]


def _digest(domain: str, payload: Any) -> str:
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False, default=str)
    return hashlib.sha256(f"{domain}|{blob}".encode()).hexdigest()


@dataclass
class OpportunityUniverseVersion:
    """The frozen finite opportunity universe (spec rules 7–8)."""

    universe_id: str = ""
    version: str = ""
    parent_universe_id: str | None = None
    instrument_universe: list[str] = field(default_factory=list)
    timeframe_set: list[str] = field(default_factory=list)
    information_contract_id: str = ""
    primitive_registry_hash: str = ""
    predicate_ir_version: str = ""
    behavior_template_registry_hash: str = ""
    parameter_grid_hash: str = ""
    tradability_rule_id: str = ""
    support_rule_id: str = ""
    authority_contract_id: str = ""
    search_universe_size: int = 0
    complexity_budget: int = 0
    #: Declared configuration timestamp; never filled from a wall clock.
    created_at: int = 0
    code_hash: str = ""
    #: Detection execution mode is hash-bound (changes the finite search frame).
    execution_mode_id: str = ""

    def identity(self) -> str:
        return _digest(
            "opportunity-universe-v1",
            {
                "version": self.version,
                "parent_universe_id": self.parent_universe_id,
                "instrument_universe": self.instrument_universe,
                "timeframe_set": self.timeframe_set,
                "information_contract_id": self.information_contract_id,
                "primitive_registry_hash": self.primitive_registry_hash,
                "predicate_ir_version": self.predicate_ir_version,
                "behavior_template_registry_hash": self.behavior_template_registry_hash,
                "parameter_grid_hash": self.parameter_grid_hash,
                "tradability_rule_id": self.tradability_rule_id,
                "support_rule_id": self.support_rule_id,
                "authority_contract_id": self.authority_contract_id,
                "search_universe_size": self.search_universe_size,
                "complexity_budget": self.complexity_budget,
                "created_at": self.created_at,
                "code_hash": self.code_hash,
                "execution_mode_id": self.execution_mode_id,
            },
        )

    def bind_identity(self) -> None:
        self.instrument_universe = sorted(set(self.instrument_universe))
        self.timeframe_set = sorted(set(self.timeframe_set))
        self.universe_id = self.identity()

    def as_dict(self) -> dict[str, Any]:
        return {
            "universe_id": self.universe_id,
            "version": self.version,
            "parent_universe_id": self.parent_universe_id,
            "instrument_universe": list(self.instrument_universe),
            "timeframe_set": list(self.timeframe_set),
            "information_contract_id": self.information_contract_id,
            "primitive_registry_hash": self.primitive_registry_hash,
            "predicate_ir_version": self.predicate_ir_version,
            "behavior_template_registry_hash": self.behavior_template_registry_hash,
            "parameter_grid_hash": self.parameter_grid_hash,
            "tradability_rule_id": self.tradability_rule_id,
            "support_rule_id": self.support_rule_id,
            "authority_contract_id": self.authority_contract_id,
            "search_universe_size": self.search_universe_size,
            "complexity_budget": self.complexity_budget,
            "created_at": self.created_at,
            "code_hash": self.code_hash,
            "execution_mode_id": self.execution_mode_id,
        }


@dataclass
class OracleEvaluationRecord:
    """One counterfactual answer bound to its authority lineage (spec rule 22)."""

    evaluation_id: str = ""
    oracle_role: OracleRole = OracleRole.HINDSIGHT
    authority_level: AuthorityLevel = AuthorityLevel.L1
    identifiability_status: Identifiability = Identifiability.NOT_IDENTIFIABLE
    information_contract_id: str = ""
    opportunity_universe_id: str = ""
    utility_contract_id: str = ""
    policy_class_id: str = ""
    cost_model_id: str = ""
    capacity_model_id: str = ""
    environment_target_id: str = ""
    candidate_population_hash: str = ""
    action_manifest_hash: str = ""
    simulator_or_receipt_hash: str = ""
    code_hash: str = ""
    config_hash: str = ""
    value_notion: ValueNotion = ValueNotion.RETROSPECTIVE
    point_estimate: float | None = None
    lower_bound: float | None = None
    upper_bound: float | None = None
    uncertainty_artifact_id: str | None = None
    refusal_reason: str | None = None
    assumptions: list[str] = field(default_factory=list)
    lineage_id: str = ""

    def identity(self) -> str:
        return _digest(
            "oracle-evaluation-record-v1",
            {
                "oracle_role": self.oracle_role.name,
                "authority_level": self.authority_level.name,
                "identifiability_status": self.identifiability_status.value,
                "information_contract_id": self.information_contract_id,
                "opportunity_universe_id": self.opportunity_universe_id,
                "utility_contract_id": self.utility_contract_id,
                "policy_class_id": self.policy_class_id,
                "cost_model_id": self.cost_model_id,
                "capacity_model_id": self.capacity_model_id,
                "environment_target_id": self.environment_target_id,
                "candidate_population_hash": self.candidate_population_hash,
                "action_manifest_hash": self.action_manifest_hash,
                "simulator_or_receipt_hash": self.simulator_or_receipt_hash,
                "code_hash": self.code_hash,
                "config_hash": self.config_hash,
                "value_notion": self.value_notion.name,
                "point_estimate": self.point_estimate,
                "lower_bound": self.lower_bound,
                "upper_bound": self.upper_bound,
                "uncertainty_artifact_id": self.uncertainty_artifact_id,
                "refusal_reason": self.refusal_reason,
                "assumptions": self.assumptions,
                "lineage_id": self.lineage_id,
            },
        )

    def bind_identity(self) -> None:
        self.evaluation_id = self.identity()
