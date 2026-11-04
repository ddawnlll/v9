"""Support classifier and evaluability validation — port of v8-core/src/oracle/support.rs.

(TARGET_ORACLE_SPEC §5.7, §8, §16.2.)

Whether a candidate/action pair is supported by the declared data, execution, and
environment models — without fabricating point estimates. Unsupported profitable hindsight
paths are model-derived or unknown, never opportunities.

The ``Action`` shape mirrors ``regret::Action`` (the A(C) legal-action manifest); v8-next
carries no regret plane, so the minimal kind/provenance/geometry-overrides surface lives
here rather than as a parallel interface.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

from v8_next.oracle.authority import CounterfactualAuthority
from v8_next.oracle.opportunity import GrammarCandidate
from v8_next.oracle.taxonomy import AuthorityLevel, Identifiability, OracleRefusal

__all__ = [
    "Action",
    "SupportClassifier",
    "SupportRule",
]


@dataclass(frozen=True)
class Action:
    """One legal action from the A(C) manifest (kind/provenance/geometry overrides)."""

    action_id: str
    kind: str  # NO_TRADE | ACTUAL | GEOMETRY_VARIANT
    provenance: str  # ACTUAL | DECLARED_VARIANT
    override_geom: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SupportRule:
    rule_id: str
    environment_model_id: str
    max_allowed_authority: AuthorityLevel
    min_future_horizon_bars: int
    allowed_actions: tuple[str, ...]
    allows_model_counterfactuals: bool
    assumptions: tuple[str, ...] = ()

    def identity(self) -> str:
        blob = json.dumps(
            {
                "rule_id": self.rule_id,
                "environment_model_id": self.environment_model_id,
                "max_allowed_authority": self.max_allowed_authority.name,
                "min_future_horizon_bars": self.min_future_horizon_bars,
                "allowed_actions": list(self.allowed_actions),
                "allows_model_counterfactuals": self.allows_model_counterfactuals,
                "assumptions": list(self.assumptions),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(f"support-rule-v1|{blob}".encode()).hexdigest()


def _refuse(
    rule: SupportRule,
    requested: AuthorityLevel,
    identifiability: Identifiability,
    refusal: OracleRefusal,
    extra_assumption: str | None = None,
) -> tuple[CounterfactualAuthority, OracleRefusal]:
    assumptions = list(rule.assumptions)
    if extra_assumption is not None:
        assumptions.append(extra_assumption)
    return (
        CounterfactualAuthority(
            oracle_authority_level=requested,
            identifiability_status=identifiability,
            support_rule_id=rule.rule_id,
            environment_model_id=rule.environment_model_id,
            assumptions=tuple(assumptions),
        ),
        refusal,
    )


class SupportClassifier:
    """Classify candidate/action support under a declared environment model."""

    def __init__(self, rule: SupportRule) -> None:
        self.rule = rule

    @classmethod
    def canonical_l1(cls) -> SupportClassifier:
        return cls(
            SupportRule(
                rule_id="canonical-l1-support-v1",
                environment_model_id="binance-usdt-perp-l1",
                max_allowed_authority=AuthorityLevel.L1,
                min_future_horizon_bars=1,
                allowed_actions=("NO_TRADE", "ACTUAL", "GEOMETRY_VARIANT"),
                allows_model_counterfactuals=False,
                assumptions=(
                    "L1_BAR_CLOSE_FILL",
                    "STATIC_SPREAD_FEE",
                    "NO_ENDOGENOUS_IMPACT",
                ),
            )
        )

    def classify(
        self,
        candidate: GrammarCandidate,
        action: Action,
        requested_authority: AuthorityLevel,
    ) -> CounterfactualAuthority:
        authority, _ = self.evaluate_support(candidate, action, requested_authority, None)
        return authority

    def evaluate_support(
        self,
        candidate: GrammarCandidate,
        action: Action,
        requested_authority: AuthorityLevel,
        future_bars_available: int | None,
    ) -> tuple[CounterfactualAuthority, OracleRefusal | None]:
        rule = self.rule
        # 1. Authority level check: a model cannot support claims above its level.
        exceeded = (
            (rule.max_allowed_authority is AuthorityLevel.L1 and requested_authority is not AuthorityLevel.L1)
            or (
                rule.max_allowed_authority is AuthorityLevel.L2
                and requested_authority
                in (AuthorityLevel.L3, AuthorityLevel.LIVE_RECEIPT)
            )
            or (
                rule.max_allowed_authority is AuthorityLevel.L3
                and requested_authority is AuthorityLevel.LIVE_RECEIPT
            )
        )
        if exceeded:
            return _refuse(
                rule,
                requested_authority,
                Identifiability.NOT_IDENTIFIABLE,
                OracleRefusal.EXECUTION_AUTHORITY_TOO_WEAK,
                "REQUESTED_AUTHORITY_EXCEEDS_SUPPORTED_MODEL",
            )
        # 2. Decision-time data check.
        if candidate.decision_time < 0:
            return _refuse(
                rule,
                requested_authority,
                Identifiability.NOT_IDENTIFIABLE,
                OracleRefusal.MISSING_DECISION_TIME_DATA,
            )
        # 3. Action-kind support.
        if not any(a == action.kind or a == action.provenance for a in rule.allowed_actions):
            return _refuse(
                rule,
                requested_authority,
                Identifiability.NOT_IDENTIFIABLE,
                OracleRefusal.OUT_OF_SUPPORT_ACTION,
            )
        # 4. Action parameters and fill feasibility.
        size = action.override_geom.get("size")
        if size is not None and (
            isinstance(size, bool) or not isinstance(size, (int, float)) or not size > 0.0
        ):
            return _refuse(
                rule,
                requested_authority,
                Identifiability.NOT_IDENTIFIABLE,
                OracleRefusal.OUT_OF_SUPPORT_ACTION,
            )
        if "queue_priority" in action.override_geom or "partial_fill_ratio" in action.override_geom:
            return _refuse(
                rule,
                requested_authority,
                Identifiability.NOT_IDENTIFIABLE,
                OracleRefusal.NON_IDENTIFIABLE_FILL,
            )
        # 5. Future-horizon check.
        expiry = action.override_geom.get("expiry_bars")
        if isinstance(expiry, bool) or not isinstance(expiry, int):
            expiry = rule.min_future_horizon_bars
        if future_bars_available is not None and (
            future_bars_available < expiry
            or future_bars_available < rule.min_future_horizon_bars
        ):
            return _refuse(
                rule,
                requested_authority,
                Identifiability.NOT_IDENTIFIABLE,
                OracleRefusal.UNDEFINED_FUTURE,
            )
        # 6. Model-only counterfactual check.
        if "model_counterfactual" in action.override_geom and not rule.allows_model_counterfactuals:
            return _refuse(
                rule,
                requested_authority,
                Identifiability.MODEL_DERIVED,
                OracleRefusal.MODEL_ONLY_COUNTERFACTUAL,
            )
        # 7. Fully supported.
        return (
            CounterfactualAuthority(
                oracle_authority_level=requested_authority,
                identifiability_status=Identifiability.IDENTIFIED,
                support_rule_id=rule.rule_id,
                environment_model_id=rule.environment_model_id,
                assumptions=rule.assumptions,
            ),
            None,
        )
