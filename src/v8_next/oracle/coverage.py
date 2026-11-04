"""Representational coverage reconciliation — port of the receipt core of
v8-core/src/oracle/coverage.rs (TARGET_ORACLE_SPEC §5.8, §9).

Reconciles the frozen supported Opportunity Universe population against same-event shipped
Expert proposals. Emits contract-bound coverage receipts labeled NO_ECONOMIC_CLAIM.

SPLIT (per issue §16, not silent): only the receipt core is ported
(``OpportunityCoverageMember`` / ``UnrepresentedCluster`` / ``CoverageReceipt`` /
``reconcile_coverage``). The Rust ``save_to_bundle`` persistence (v8.eval.v1 bundle,
parquet artifacts, schema cache, authority surface, lineage DAG, temporal receipt) has no
v8-next counterpart — receipts bind via sha256 digest like ``evaluation/parity.py`` and are
written as JSON. Expert proposals use the minimal ``ExpertProposal`` shape below (expert id,
CANDIDATE decision, LONG/SHORT direction) instead of ``ExpertEval`` + simulator ``Draft``,
which likewise have no O1-side Python counterpart.

DIVERGENCES: identity digests use hashlib (sha256); reproducible here, not bit-equal to Rust.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Sequence

from v8_next.oracle.artifacts import OpportunityUniverseVersion, OracleEvaluationRecord
from v8_next.oracle.authority import OracleOutcome, OracleRefused
from v8_next.oracle.opportunity import Direction, GrammarCandidate
from v8_next.oracle.support import Action, SupportClassifier
from v8_next.oracle.taxonomy import (
    AuthorityLevel,
    Identifiability,
    OracleContext,
    OracleRefusal,
    ValueNotion,
)

__all__ = [
    "CoverageReceipt",
    "ExpertProposal",
    "OpportunityCoverageMember",
    "UnrepresentedCluster",
    "reconcile_coverage",
]

#: The claim every coverage receipt carries (spec §5.8, §18.2).
NO_ECONOMIC_CLAIM = "NO_ECONOMIC_CLAIM"


@dataclass(frozen=True)
class ExpertProposal:
    """Same-event shipped expert proposal (minimal O1-side shape).

    Mirrors the ``ExpertEval`` fields ``reconcile_coverage`` reads (``decision`` and the
    draft ``direction``) without the simulator ``Draft``.
    """

    expert_id: str
    decision: str  # "CANDIDATE" to represent
    direction: Direction


@dataclass(frozen=True)
class OpportunityCoverageMember:
    grammar_candidate_id: str
    template_id: str
    instrument: str
    timeframe: str
    direction: Direction
    decision_time: int
    is_supported: bool
    is_represented: bool
    representing_expert_id: str | None
    authority_level: AuthorityLevel
    identifiability_status: Identifiability
    refusal_reason: str | None
    point_estimate: float | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "grammar_candidate_id": self.grammar_candidate_id,
            "template_id": self.template_id,
            "instrument": self.instrument,
            "timeframe": self.timeframe,
            "direction": self.direction.value,
            "decision_time": self.decision_time,
            "is_supported": self.is_supported,
            "is_represented": self.is_represented,
            "representing_expert_id": self.representing_expert_id,
            "authority_level": self.authority_level.name,
            "identifiability_status": self.identifiability_status.value,
            "refusal_reason": self.refusal_reason,
            "point_estimate": self.point_estimate,
        }


@dataclass(frozen=True)
class UnrepresentedCluster:
    template_id: str
    direction: Direction
    count: int


@dataclass
class CoverageReceipt:
    receipt_id: str = ""
    universe_id: str = ""
    population_hash: str = ""
    claim: str = NO_ECONOMIC_CLAIM
    total_opportunity_count: int = 0
    supported_opportunity_count: int = 0
    unsupported_opportunity_count: int = 0
    represented_supported_count: int = 0
    unrepresented_supported_count: int = 0
    representational_coverage: float = 0.0
    representational_coverage_gap: float = 1.0
    unrepresented_clusters: list[UnrepresentedCluster] = field(default_factory=list)
    members: list[OpportunityCoverageMember] = field(default_factory=list)

    def identity(self) -> str:
        blob = json.dumps(
            {
                "universe_id": self.universe_id,
                "population_hash": self.population_hash,
                "claim": self.claim,
                "total_opportunity_count": self.total_opportunity_count,
                "supported_opportunity_count": self.supported_opportunity_count,
                "unsupported_opportunity_count": self.unsupported_opportunity_count,
                "represented_supported_count": self.represented_supported_count,
                "unrepresented_supported_count": self.unrepresented_supported_count,
                "representational_coverage": f"{self.representational_coverage:.6f}",
                "representational_coverage_gap": f"{self.representational_coverage_gap:.6f}",
                "unrepresented_clusters": [
                    {"template_id": c.template_id, "direction": c.direction.value, "count": c.count}
                    for c in self.unrepresented_clusters
                ],
                "members_count": len(self.members),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(f"coverage-receipt-v1|{blob}".encode()).hexdigest()

    def bind_identity(self) -> None:
        self.receipt_id = self.identity()

    def check_numerators(self) -> None:
        """Every represented member is a supported opportunity (hard invariant)."""
        for member in self.members:
            if member.is_represented and not member.is_supported:
                raise ValueError(
                    f"represented member {member.grammar_candidate_id!r} is not supported"
                )

    def as_dict(self) -> dict[str, Any]:
        return {
            "receipt_id": self.receipt_id,
            "universe_id": self.universe_id,
            "population_hash": self.population_hash,
            "claim": self.claim,
            "total_opportunity_count": self.total_opportunity_count,
            "supported_opportunity_count": self.supported_opportunity_count,
            "unsupported_opportunity_count": self.unsupported_opportunity_count,
            "represented_supported_count": self.represented_supported_count,
            "unrepresented_supported_count": self.unrepresented_supported_count,
            "representational_coverage": self.representational_coverage,
            "representational_coverage_gap": self.representational_coverage_gap,
            "unrepresented_clusters": [
                {"template_id": c.template_id, "direction": c.direction.value, "count": c.count}
                for c in self.unrepresented_clusters
            ],
            "members": [m.as_dict() for m in self.members],
        }


def reconcile_coverage(
    universe: OpportunityUniverseVersion,
    grammar_candidates: Sequence[GrammarCandidate],
    classifier: SupportClassifier,
    expert_proposals: Sequence[ExpertProposal],
    future_bars_available: int | None,
    requested_authority: AuthorityLevel,
    context: OracleContext,
    lineage_id: str,
) -> tuple[CoverageReceipt, list[OracleEvaluationRecord]]:
    """Reconcile representational coverage deterministically (receipt + eval records)."""
    from v8_next.oracle.opportunity import OpportunityGrammar

    population_hash = OpportunityGrammar.population_hash(list(grammar_candidates))
    members: list[OpportunityCoverageMember] = []
    eval_records: list[OracleEvaluationRecord] = []
    clusters: dict[tuple[str, Direction], int] = {}
    actual = Action(action_id="ACTUAL", kind="ACTUAL", provenance="ACTUAL", override_geom={})

    for candidate in grammar_candidates:
        authority, refusal = classifier.evaluate_support(
            candidate, actual, requested_authority, future_bars_available
        )
        is_supported = (
            authority.identifiability_status is Identifiability.IDENTIFIED and refusal is None
        )
        representing: str | None = None
        if is_supported:
            for proposal in expert_proposals:
                if proposal.decision == "CANDIDATE" and proposal.direction is candidate.direction:
                    representing = proposal.expert_id
                    break
        is_represented = representing is not None
        if is_supported and not is_represented:
            key = (candidate.template_id, candidate.direction)
            clusters[key] = clusters.get(key, 0) + 1
        if is_supported:
            try:
                outcome = OracleOutcome.identified(0.0, authority)
            except OracleRefused:
                outcome = OracleOutcome.unknown(OracleRefusal.INSUFFICIENT_SUPPORT, authority)
        else:
            outcome = OracleOutcome.unknown(
                refusal if refusal is not None else OracleRefusal.INSUFFICIENT_SUPPORT,
                authority,
            )
        eval_records.append(
            outcome.to_evaluation_record(
                context,
                population_hash,
                "action-manifest-actual-v1",
                "simulator-l1-canonical-v1",
                universe.code_hash,
                "config-oracle-v1",
                ValueNotion.RETROSPECTIVE,
                lineage_id,
            )
        )
        members.append(
            OpportunityCoverageMember(
                grammar_candidate_id=candidate.grammar_candidate_id,
                template_id=candidate.template_id,
                instrument=candidate.instrument,
                timeframe=candidate.timeframe,
                direction=candidate.direction,
                decision_time=candidate.decision_time,
                is_supported=is_supported,
                is_represented=is_represented,
                representing_expert_id=representing,
                authority_level=authority.oracle_authority_level,
                identifiability_status=authority.identifiability_status,
                refusal_reason=refusal.value if refusal is not None else None,
                point_estimate=0.0 if is_supported else None,
            )
        )

    total = len(members)
    supported = sum(1 for m in members if m.is_supported)
    represented = sum(1 for m in members if m.is_supported and m.is_represented)
    coverage = (represented / supported) if supported > 0 else 0.0
    receipt = CoverageReceipt(
        universe_id=universe.universe_id,
        population_hash=population_hash,
        claim=NO_ECONOMIC_CLAIM,
        total_opportunity_count=total,
        supported_opportunity_count=supported,
        unsupported_opportunity_count=total - supported,
        represented_supported_count=represented,
        unrepresented_supported_count=supported - represented,
        representational_coverage=coverage,
        representational_coverage_gap=1.0 - coverage,
        unrepresented_clusters=[
            UnrepresentedCluster(template_id=t, direction=d, count=c)
            for (t, d), c in sorted(clusters.items(), key=lambda kv: (kv[0][0], kv[0][1].value))
        ],
        members=members,
    )
    receipt.bind_identity()
    receipt.check_numerators()
    return receipt, eval_records
