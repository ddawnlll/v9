"""Canonical decision spans, evidence spans and semantic graph links — port of
``v8-core/src/telemetry/span.rs`` (EEO-001H, D-136).

Constitutional invariants carried over from the Rust module:

1. **Epistemic separation** — Point-In-Time economic decision stages (:class:`DecisionStage`)
   are strictly isolated from post-outcome evidence and Oracle/Audit evaluation
   (:class:`EvidenceStage`). The two are different enums with no shared member and no implicit
   conversion.
2. **Ancestry integrity** — a PIT decision span can never take a post-outcome evidence or Oracle
   span as an upstream parent. The Rust module states this in its header without enforcing it;
   :func:`require_pit_ancestor` is the Python check that makes it executable where an ancestor's
   kind is actually known.
3. **Counterfactual lineage** — :attr:`SpanLinkType.CounterfactualBranch` distinguishes a
   counterfactual derivation from observed execution, so ``explain`` can read the branch back
   out of a trail instead of recomputing it.
4. **Many-to-many graph topology** — :class:`SpanLink` connects aggregated campaigns without
   assuming one opportunity equals one trade.

Append-only note: spans are frozen. The Rust ``with_*`` builders consume ``self`` and ``close``
mutates in place; here every one of them returns a new span and leaves the original untouched,
so a span that a consumer is reading cannot be rewritten underneath it.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Any

from v8_next.telemetry.identity import EconomicTraceId, SpanId, TraceLineageError


class DecisionStage(StrEnum):
    """Canonical Point-In-Time economic decision stage in the V8.3 trajectory.

    Oracle hindsight and Audit adjudication are strictly EXCLUDED from this enum (invariant 1).
    """

    MarketState = "MarketState"
    OpportunityDetection = "OpportunityDetection"
    WitnessObservation = "WitnessObservation"
    EvidenceReconciliation = "EvidenceReconciliation"
    SelectiveUtility = "SelectiveUtility"
    PortfolioFeasibility = "PortfolioFeasibility"
    CampaignAdmission = "CampaignAdmission"
    OrderDispatch = "OrderDispatch"
    ExecutionFill = "ExecutionFill"
    PositionManagement = "PositionManagement"
    CashflowSettlement = "CashflowSettlement"

    def as_str(self) -> str:
        return str(self.value)

    def is_pit_decision(self) -> bool:
        """Confirms that this is a PIT economic decision stage. Always true by construction."""
        return True


class EvidenceStage(StrEnum):
    """Post-outcome & evidence plane evaluation stage (D-136-RP-001 §5.2, §5.3).

    Operates strictly as downstream observation/adjudication over frozen decision spans.
    """

    #: Hindsight frontier opportunity evaluation (Target Oracle).
    TargetOracleHindsight = "TargetOracleHindsight"
    #: Realized path markout and MFE/MAE analysis.
    HindsightPathAnalysis = "HindsightPathAnalysis"
    #: Audit invariant, provenance and claim adjudication.
    AuditAdjudication = "AuditAdjudication"
    #: Versioned Evidence Provider evaluation (P01-P12).
    ProviderEvaluation = "ProviderEvaluation"
    #: Multiplicity and search space accounting.
    MultiplicityAccounting = "MultiplicityAccounting"

    def as_str(self) -> str:
        return str(self.value)

    def is_post_outcome(self) -> bool:
        """Confirms that this is post-outcome evidence with zero PIT decision authority."""
        return True


class PitAncestryError(TraceLineageError):
    """A PIT decision span was given a post-outcome evidence span as an ancestor (invariant 2)."""

    code = "EVIDENCE_PLANE_ANCESTRY"


@dataclass(frozen=True)
class SpanKind:
    """Classification of span category: the Rust ``enum SpanKind { Decision, Evidence }``.

    A Rust data-carrying enum becomes one frozen record holding exactly one of the two stage
    types; ``__post_init__`` refuses a kind that carries neither or both, so the discriminator
    cannot drift from the payload.
    """

    decision_stage: DecisionStage | None = None
    evidence_stage: EvidenceStage | None = None

    def __post_init__(self) -> None:
        if (self.decision_stage is None) == (self.evidence_stage is None):
            raise TraceLineageError(
                "SpanKind must carry exactly one of a DecisionStage or an EvidenceStage"
            )

    @classmethod
    def for_decision(cls, stage: DecisionStage) -> SpanKind:
        return cls(decision_stage=stage)

    @classmethod
    def for_evidence(cls, stage: EvidenceStage) -> SpanKind:
        return cls(evidence_stage=stage)

    def is_decision(self) -> bool:
        return self.decision_stage is not None

    def is_evidence(self) -> bool:
        return self.evidence_stage is not None

    def is_pit_decision(self) -> bool:
        return self.is_decision()

    def as_str(self) -> str:
        if self.decision_stage is not None:
            return self.decision_stage.as_str()
        if self.evidence_stage is not None:
            return self.evidence_stage.as_str()
        raise TraceLineageError("SpanKind carries no stage")  # pragma: no cover - post_init guards


def require_pit_ancestor(ancestor: SpanKind | None) -> None:
    """Refuse a post-outcome evidence/Oracle span as an upstream ancestor (invariant 2).

    ``None`` (no ancestor) is accepted. The Rust header states this rule; this is the check a
    consumer runs where it actually holds the ancestor's kind.
    """
    if ancestor is not None and ancestor.is_evidence():
        raise PitAncestryError(
            f"PIT decision span cannot descend from evidence-plane stage {ancestor.as_str()}"
        )


class SpanLinkType(StrEnum):
    """Semantic relationship type between decision spans and opportunities."""

    #: Multiple opportunities aggregated into a single execution campaign.
    AggregatedIntoCampaign = "AggregatedIntoCampaign"
    #: Upstream opportunity episode that parented or catalysed this decision.
    ParentOpportunity = "ParentOpportunity"
    #: Transformed or decomposed exposure structure.
    TransformedExposure = "TransformedExposure"
    #: Co-temporal correlated market episode.
    CorrelatedEpisode = "CorrelatedEpisode"
    #: Branch under counterfactual replay or candidate intervention.
    CounterfactualBranch = "CounterfactualBranch"
    #: Causal dependency or related decision.
    RelatedDecision = "RelatedDecision"
    #: Post-outcome observation or evidence attachment.
    PostOutcomeEvidenceLink = "PostOutcomeEvidenceLink"

    def as_str(self) -> str:
        return str(self.value)

    def is_counterfactual_branch(self) -> bool:
        return self is SpanLinkType.CounterfactualBranch


@dataclass(frozen=True)
class SpanLink:
    """Explicit many-to-many link between decision spans and opportunity traces."""

    target_trace_id: EconomicTraceId
    target_span_id: SpanId | None
    opportunity_id: str
    link_type: SpanLinkType
    attributes: tuple[tuple[str, str], ...] = ()

    @classmethod
    def new(
        cls,
        target_trace_id: EconomicTraceId,
        target_span_id: SpanId | None,
        opportunity_id: str,
        link_type: SpanLinkType,
    ) -> SpanLink:
        return cls(
            target_trace_id=target_trace_id,
            target_span_id=target_span_id,
            opportunity_id=str(opportunity_id),
            link_type=link_type,
        )

    def with_attribute(self, key: str, value: str) -> SpanLink:
        """Return a copy carrying one more attribute (the Rust consuming builder)."""
        return replace(self, attributes=(*self.attributes, (str(key), str(value))))

    def is_counterfactual_branch(self) -> bool:
        return self.link_type.is_counterfactual_branch()

    def as_dict(self) -> dict[str, Any]:
        return {
            "target_trace_id": self.target_trace_id.as_str(),
            "target_span_id": None if self.target_span_id is None else self.target_span_id.as_str(),
            "opportunity_id": self.opportunity_id,
            "link_type": self.link_type.as_str(),
            "is_counterfactual_branch": self.is_counterfactual_branch(),
            "attributes": [list(pair) for pair in self.attributes],
        }


@dataclass(frozen=True)
class DecisionSpan:
    """Canonical unit of work in the PIT economic decision path."""

    span_id: SpanId
    trace_id: EconomicTraceId
    parent_span_id: SpanId | None
    stage: DecisionStage
    start_time: int
    end_time: int | None = None
    receipt_id: str | None = None
    links: tuple[SpanLink, ...] = ()
    attributes: tuple[tuple[str, str], ...] = ()

    @classmethod
    def new(
        cls,
        trace_id: EconomicTraceId,
        parent_span_id: SpanId | None,
        stage: DecisionStage,
        start_time: int,
        disambiguator: str,
    ) -> DecisionSpan:
        """Open a PIT decision span with a deterministic span identity.

        ``stage`` is a :class:`DecisionStage`, so the evidence plane is excluded by the type;
        a caller holding a raw string goes through ``DecisionStage(...)`` and fails closed there.
        """
        return cls(
            span_id=SpanId.compute(
                trace_id, parent_span_id, stage.as_str(), start_time, disambiguator
            ),
            trace_id=trace_id,
            parent_span_id=parent_span_id,
            stage=stage,
            start_time=int(start_time),
        )

    def with_link(self, link: SpanLink) -> DecisionSpan:
        return replace(self, links=(*self.links, link))

    def with_attribute(self, key: str, value: str) -> DecisionSpan:
        return replace(self, attributes=(*self.attributes, (str(key), str(value))))

    def with_receipt(self, receipt_id: str) -> DecisionSpan:
        return replace(self, receipt_id=str(receipt_id))

    def close(self, end_time: int) -> DecisionSpan:
        """Close the span with monotonic time validation, returning a new span.

        The Rust ``close`` takes ``&mut self``; this port returns the closed copy and leaves the
        open span readable, because invariant 2 (append-only) applies to spans a consumer may
        already hold.
        """
        if end_time < self.start_time:
            raise TraceLineageError(
                f"DecisionSpan {self.span_id} end_time ({end_time}) cannot precede "
                f"start_time ({self.start_time})"
            )
        return replace(self, end_time=int(end_time))

    @property
    def is_closed(self) -> bool:
        return self.end_time is not None

    @property
    def counterfactual_branches(self) -> tuple[SpanLink, ...]:
        return tuple(link for link in self.links if link.is_counterfactual_branch())

    def as_dict(self) -> dict[str, Any]:
        return {
            "span_id": self.span_id.as_str(),
            "trace_id": self.trace_id.as_str(),
            "parent_span_id": None if self.parent_span_id is None else self.parent_span_id.as_str(),
            "plane": "DECISION",
            "stage": self.stage.as_str(),
            "start_time": self.start_time,
            "end_time": self.end_time,
            "receipt_id": self.receipt_id,
            "links": [link.as_dict() for link in self.links],
            "attributes": [list(pair) for pair in self.attributes],
        }


@dataclass(frozen=True)
class EvidenceSpan:
    """Post-outcome evidence span observing a completed decision span.

    Bound to the telemetry & evidence plane (D-136-RP-001 §5.2). It has no path back into a
    belief receipt: see ``belief.EvidencePlaneStageRejected``.
    """

    span_id: SpanId
    trace_id: EconomicTraceId
    observed_decision_span_id: SpanId
    stage: EvidenceStage
    evaluation_time: int
    receipt_id: str | None = None
    claims: tuple[str, ...] = ()
    attributes: tuple[tuple[str, str], ...] = ()

    @classmethod
    def new(
        cls,
        trace_id: EconomicTraceId,
        observed_decision_span_id: SpanId,
        stage: EvidenceStage,
        evaluation_time: int,
        disambiguator: str,
    ) -> EvidenceSpan:
        """Open an evidence span whose ancestor is always the observed decision span.

        The ancestor is a decision-span identity by parameter type, which is the enforceable half
        of ancestry integrity (invariant 2): there is no way to hand an evidence span its own
        kind as an upstream parent.
        """
        return cls(
            span_id=SpanId.compute(
                trace_id,
                observed_decision_span_id,
                stage.as_str(),
                evaluation_time,
                disambiguator,
            ),
            trace_id=trace_id,
            observed_decision_span_id=observed_decision_span_id,
            stage=stage,
            evaluation_time=int(evaluation_time),
        )

    def with_claim(self, claim: str) -> EvidenceSpan:
        return replace(self, claims=(*self.claims, str(claim)))

    def with_receipt(self, receipt_id: str) -> EvidenceSpan:
        return replace(self, receipt_id=str(receipt_id))

    def with_attribute(self, key: str, value: str) -> EvidenceSpan:
        return replace(self, attributes=(*self.attributes, (str(key), str(value))))

    def as_dict(self) -> dict[str, Any]:
        return {
            "span_id": self.span_id.as_str(),
            "trace_id": self.trace_id.as_str(),
            "observed_decision_span_id": self.observed_decision_span_id.as_str(),
            "plane": "EVIDENCE",
            "stage": self.stage.as_str(),
            "evaluation_time": self.evaluation_time,
            "receipt_id": self.receipt_id,
            "claims": list(self.claims),
            "attributes": [list(pair) for pair in self.attributes],
        }
