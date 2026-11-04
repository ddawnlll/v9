"""Decision-trail telemetry substrate — Python port of ``v8-core/src/telemetry``
(EEO-001H, EEO-002, D-136, D-136-RP-001).

Why this package exists: a trade explanation is only auditable if the decision that produced it
can be read back as a typed trail. This package is that trail — the canonical Point-In-Time
decision stages, the trace identity/provenance bundle, and the ex-ante belief receipts recorded
along the way.

The three planes are kept apart on purpose:

* :mod:`v8_next.telemetry.span` — PIT decision stages, post-outcome evidence stages, and the links
  between spans. Counterfactual lineage is an explicit link type
  (:attr:`~v8_next.telemetry.span.SpanLinkType.CounterfactualBranch`), never an inferred one.
* :mod:`v8_next.telemetry.identity` — trace identity, trajectory modality and cryptographic
  provenance. Identity is derived from content, and no identity takes a wall clock.
* :mod:`v8_next.telemetry.belief` — append-only ex-ante belief receipts and the ledger that holds
  them. An evidence-plane stage is refused by name, and a dimension V8 does not compute stays
  explicitly unavailable rather than defaulting to a number.

The binding onto a decision record lives in
``v8_next.adapters.execution_telemetry.attach_decision_trail``.
"""

from v8_next.telemetry.belief import (
    UNAVAILABLE_EX_ANTE_DIMENSIONS,
    BeliefReceipt,
    BeliefReceiptId,
    BeliefReceiptVerificationError,
    BeliefStage,
    ChosenAction,
    ChosenActionKind,
    ConflictingBeliefReceiptError,
    DecisionBeliefLedger,
    EvidencePlaneStageRejected,
    ExAnteCostExpectation,
    ExAnteUncertainty,
    FabricatedBeliefError,
    require_pit_decision_stage,
)
from v8_next.telemetry.identity import (
    EconomicTraceContext,
    EconomicTraceId,
    OpportunityEpisodeLike,
    SpanId,
    TraceLineageError,
    TraceProvenance,
    TrajectoryType,
    canonical_digest,
)
from v8_next.telemetry.span import (
    DecisionSpan,
    DecisionStage,
    EvidenceSpan,
    EvidenceStage,
    PitAncestryError,
    SpanKind,
    SpanLink,
    SpanLinkType,
    require_pit_ancestor,
)

__all__ = [
    "BeliefReceipt",
    "BeliefReceiptId",
    "BeliefReceiptVerificationError",
    "BeliefStage",
    "ChosenAction",
    "ChosenActionKind",
    "ConflictingBeliefReceiptError",
    "DecisionBeliefLedger",
    "DecisionSpan",
    "DecisionStage",
    "EconomicTraceContext",
    "EconomicTraceId",
    "EvidencePlaneStageRejected",
    "EvidenceSpan",
    "EvidenceStage",
    "ExAnteCostExpectation",
    "ExAnteUncertainty",
    "FabricatedBeliefError",
    "OpportunityEpisodeLike",
    "PitAncestryError",
    "SpanId",
    "SpanKind",
    "SpanLink",
    "SpanLinkType",
    "TraceLineageError",
    "TraceProvenance",
    "TrajectoryType",
    "UNAVAILABLE_EX_ANTE_DIMENSIONS",
    "canonical_digest",
    "require_pit_ancestor",
    "require_pit_decision_stage",
]
