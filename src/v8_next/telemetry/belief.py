"""Canonical decision belief ledger & epistemic receipts — port of
``v8-core/src/telemetry/belief.rs`` (EEO-002, D-136, D-136-RP-001 §7).

Owning authority: V8 Constitution Rules 1, 3, 4, 18, 20, 21, 24, 28, 35; D-136.

Constitutional invariants upheld here, and *how* they are upheld:

1. **No fabricated beliefs (anti-hallucination)** — only ex-ante signals V8 actually computes
   Point-In-Time are recorded. Dimensions V8 does not compute are left ``None`` and named in
   :data:`UNAVAILABLE_EX_ANTE_DIMENSIONS`; :meth:`BeliefReceipt.verify` fails closed if any of
   them is ever populated.
2. **Append-only immutability** — :class:`BeliefReceipt` is a frozen dataclass. A later price
   path, Oracle, Audit or execution outcome cannot mutate one. The ledger appends; re-appending a
   receipt whose id already exists is idempotent only when the content is identical, and a
   differing body under the same id is refused by name.
3. **PIT / hindsight firewall** — a receipt can only be built from a PIT :class:`DecisionStage`.
   An evidence-plane stage raises :class:`EvidencePlaneStageRejected` (there is no silent
   acceptance path, and no evidence-stage member maps onto a :class:`BeliefStage`).
4. **Full funnel coverage** — a rejected opportunity still leaves a final immutable ex-ante
   receipt (``is_rejection`` with a named reason), so downstream Oracle Gap analysis is not
   biased toward executed trades. ``rejection_reason`` stays ``None`` when nothing named the
   reason rather than borrowing the stage name.
5. **Identity lineage decoupling** — ``BeliefReceiptId != EconomicTraceId != SpanId !=
   TraceProvenance``, and the receipt digest covers content only: no wall clock, no ledger
   position, no insertion order.

Rust constructors ``from_opportunity`` / ``from_witnesses`` / ``from_reconciliation`` /
``from_utility`` / ``from_portfolio_feasibility`` are deliberately NOT ported: they consume
``OpportunityEpisode``, ``ObserverEvidence``, ``ReconciledOpportunityState``,
``SelectiveUtilityDecision``, ``FrictionModel`` and ``CampaignIntent``, none of which exist in
``v8-next`` (checked: no counterpart module defines them). Snapshotting is therefore done through
:meth:`BeliefReceipt.new` with values the caller actually measured — see the module report for
the ambiguity note.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, fields, replace
from enum import StrEnum
from typing import Any, Mapping

from v8_next.telemetry.identity import (
    EconomicTraceId,
    SpanId,
    TraceLineageError,
    TraceProvenance,
    canonical_digest,
)
from v8_next.telemetry.span import DecisionStage, EvidenceStage, SpanKind


class EvidencePlaneStageRejected(TraceLineageError):
    """An evidence-plane stage was offered where a PIT decision stage is required (invariant 3).

    Raised by :func:`require_pit_decision_stage`, :meth:`BeliefStage.from_decision_stage` and
    :meth:`BeliefReceipt.new`. Hindsight evaluation is a different plane with zero ex-ante
    decision authority; accepting it here would back-fill a belief with the outcome it is
    supposed to precede.
    """

    code = "EVIDENCE_PLANE_STAGE_REJECTED"


class FabricatedBeliefError(TraceLineageError):
    """An ex-ante dimension V8 does not compute was populated (invariant 1)."""

    code = "FABRICATED_EX_ANTE_DIMENSION"


class BeliefReceiptVerificationError(TraceLineageError):
    """A receipt's identity does not re-derive from its content — the receipt is not intact."""

    code = "BELIEF_RECEIPT_DIGEST_MISMATCH"


class ConflictingBeliefReceiptError(TraceLineageError):
    """A different body was appended under an existing receipt id (invariant 2)."""

    code = "CONFLICTING_BELIEF_RECEIPT"


#: Ex-ante dimensions V8 does NOT compute Point-In-Time. Rust declares them as fields that are
#: always ``None``; naming them here keeps "unavailable" an explicit, checkable state instead of
#: an empty slot a future contributor could quietly fill (invariant 1).
UNAVAILABLE_EX_ANTE_DIMENSIONS: tuple[str, ...] = (
    "outcome_probabilities",
    "expected_mfe_r",
    "expected_mae_r",
)


class BeliefStage(StrEnum):
    """Checkpoint in the PIT economic pipeline where an ex-ante belief receipt is captured."""

    #: Initial opportunity episode detection.
    OpportunityDetected = "OpportunityDetected"
    #: Post-witness ensemble observation.
    PostWitnessObservation = "PostWitnessObservation"
    #: Post-dependence reconciliation.
    PostReconciliation = "PostReconciliation"
    #: Post-selective utility net hurdle evaluation.
    PostSelectiveUtility = "PostSelectiveUtility"
    #: Pre-execution portfolio feasibility check.
    PortfolioFeasibilityEvaluated = "PortfolioFeasibilityEvaluated"
    #: Execution campaign authorized & dispatched.
    CampaignDispatched = "CampaignDispatched"

    def as_str(self) -> str:
        return str(self.value)

    @classmethod
    def from_decision_stage(
        cls, stage: DecisionStage | EvidenceStage | SpanKind
    ) -> BeliefStage:
        """Map a PIT :class:`DecisionStage` onto its belief checkpoint.

        Strictly rejects post-outcome evidence stages with :class:`EvidencePlaneStageRejected`
        (invariant 3). The Rust signature can only be handed a ``DecisionStage``; this port
        widens the input so that an evidence-plane stage is *refused by name* rather than
        accidentally being someone else's type error.
        """
        return _BELIEF_STAGE_BY_DECISION[require_pit_decision_stage(stage)]


#: The Rust ``match`` arm, verbatim: ``PositionManagement`` and ``CashflowSettlement`` fold onto
#: ``CampaignDispatched`` there as well, which is why a settlement snapshot is a dispatch
#: checkpoint and not a new belief stage.
_BELIEF_STAGE_BY_DECISION: dict[DecisionStage, BeliefStage] = {
    DecisionStage.MarketState: BeliefStage.OpportunityDetected,
    DecisionStage.OpportunityDetection: BeliefStage.OpportunityDetected,
    DecisionStage.WitnessObservation: BeliefStage.PostWitnessObservation,
    DecisionStage.EvidenceReconciliation: BeliefStage.PostReconciliation,
    DecisionStage.SelectiveUtility: BeliefStage.PostSelectiveUtility,
    DecisionStage.PortfolioFeasibility: BeliefStage.PortfolioFeasibilityEvaluated,
    DecisionStage.CampaignAdmission: BeliefStage.CampaignDispatched,
    DecisionStage.OrderDispatch: BeliefStage.CampaignDispatched,
    DecisionStage.ExecutionFill: BeliefStage.CampaignDispatched,
    DecisionStage.PositionManagement: BeliefStage.CampaignDispatched,
    DecisionStage.CashflowSettlement: BeliefStage.CampaignDispatched,
}


def require_pit_decision_stage(
    stage: DecisionStage | EvidenceStage | SpanKind | str,
) -> DecisionStage:
    """The PIT decision stage, or :class:`EvidencePlaneStageRejected` for an evidence stage.

    The single implementation of the firewall (invariant 3). Anything that is not a PIT decision
    stage fails closed: an evidence-plane stage is refused by name, and an unrecognised value
    raises :class:`TraceLineageError` instead of defaulting to a stage.
    """
    if isinstance(stage, SpanKind):
        if stage.is_evidence():
            raise EvidencePlaneStageRejected(
                f"evidence-plane stage {stage.as_str()} cannot carry PIT decision authority"
            )
        decision = stage.decision_stage
        if decision is None:
            raise TraceLineageError("SpanKind carries no decision stage")
        return decision
    if isinstance(stage, EvidenceStage):
        raise EvidencePlaneStageRejected(
            f"evidence-plane stage {stage.as_str()} cannot carry PIT decision authority"
        )
    if isinstance(stage, DecisionStage):
        return stage
    try:
        return DecisionStage(str(stage))
    except ValueError as exc:
        raise TraceLineageError(f"unknown decision stage {stage!r}") from exc


def _require_belief_stage(stage: BeliefStage | EvidenceStage | str) -> BeliefStage:
    """The belief checkpoint, or a named refusal for an evidence-plane / unknown stage."""
    if isinstance(stage, EvidenceStage):
        raise EvidencePlaneStageRejected(
            f"evidence-plane stage {stage.as_str()} cannot populate an ex-ante belief receipt"
        )
    if isinstance(stage, BeliefStage):
        return stage
    try:
        return BeliefStage(str(stage))
    except ValueError as exc:
        raise TraceLineageError(f"unknown belief stage {stage!r}") from exc


class ChosenActionKind(StrEnum):
    """Discriminator for the Rust ``enum ChosenAction`` (the variant tag)."""

    OpportunityIdentified = "OpportunityIdentified"
    WitnessEvaluated = "WitnessEvaluated"
    Reconciled = "Reconciled"
    SelectiveUtilityDecision = "SelectiveUtilityDecision"
    PortfolioDecision = "PortfolioDecision"


@dataclass(frozen=True)
class ChosenAction:
    """Canonical ex-ante action chosen at a decision checkpoint.

    A Rust data-carrying enum becomes one frozen record with a :class:`ChosenActionKind`
    discriminator: the JSON shape (``{"kind": "...", ...variant fields}``) is the tagged encoding
    a consumer needs, and it keeps the trail a single serialisable type instead of a union.
    Variant fields that do not apply stay ``None``.
    """

    kind: ChosenActionKind
    # -- WitnessEvaluated
    participating_witnesses: int | None = None
    supporting_witnesses: int | None = None
    contradicting_witnesses: int | None = None
    abstaining_witnesses: int | None = None
    # -- Reconciled
    stance: str | None = None
    effective_observer_count: float | None = None
    support_weight: float | None = None
    contradict_weight: float | None = None
    contradiction_entropy: float | None = None
    # -- SelectiveUtilityDecision
    action: str | None = None
    expected_net_utility_r: float | None = None
    # -- PortfolioDecision
    admitted: bool | None = None
    allocated_capital_usdt: float | None = None
    target_risk_r: float | None = None
    # -- SelectiveUtilityDecision / PortfolioDecision
    rejection_reason: str | None = None

    @classmethod
    def opportunity_identified(cls) -> ChosenAction:
        """Episode acknowledged / queued."""
        return cls(kind=ChosenActionKind.OpportunityIdentified)

    @classmethod
    def witness_evaluated(
        cls,
        *,
        participating_witnesses: int,
        supporting_witnesses: int,
        contradicting_witnesses: int,
        abstaining_witnesses: int,
    ) -> ChosenAction:
        """Witness stances collected."""
        return cls(
            kind=ChosenActionKind.WitnessEvaluated,
            participating_witnesses=participating_witnesses,
            supporting_witnesses=supporting_witnesses,
            contradicting_witnesses=contradicting_witnesses,
            abstaining_witnesses=abstaining_witnesses,
        )

    @classmethod
    def reconciled(
        cls,
        *,
        stance: str,
        effective_observer_count: float,
        support_weight: float,
        contradict_weight: float,
        contradiction_entropy: float,
    ) -> ChosenAction:
        """Reconciled aggregate verdict."""
        return cls(
            kind=ChosenActionKind.Reconciled,
            stance=stance,
            effective_observer_count=effective_observer_count,
            support_weight=support_weight,
            contradict_weight=contradict_weight,
            contradiction_entropy=contradiction_entropy,
        )

    @classmethod
    def selective_utility_decision(
        cls,
        *,
        action: str,
        expected_net_utility_r: float,
        rejection_reason: str | None = None,
    ) -> ChosenAction:
        """Selective utility evaluation result."""
        return cls(
            kind=ChosenActionKind.SelectiveUtilityDecision,
            action=action,
            expected_net_utility_r=expected_net_utility_r,
            rejection_reason=rejection_reason,
        )

    @classmethod
    def portfolio_decision(
        cls,
        *,
        admitted: bool,
        allocated_capital_usdt: float,
        target_risk_r: float,
        rejection_reason: str | None = None,
    ) -> ChosenAction:
        """Portfolio feasibility admission or capacity reject."""
        return cls(
            kind=ChosenActionKind.PortfolioDecision,
            admitted=admitted,
            allocated_capital_usdt=allocated_capital_usdt,
            target_risk_r=target_risk_r,
            rejection_reason=rejection_reason,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            field.name: (
                self.kind.value if field.name == "kind" else getattr(self, field.name)
            )
            for field in fields(self)
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> ChosenAction:
        return cls(
            kind=ChosenActionKind(str(payload["kind"])),
            participating_witnesses=payload.get("participating_witnesses"),
            supporting_witnesses=payload.get("supporting_witnesses"),
            contradicting_witnesses=payload.get("contradicting_witnesses"),
            abstaining_witnesses=payload.get("abstaining_witnesses"),
            stance=payload.get("stance"),
            effective_observer_count=payload.get("effective_observer_count"),
            support_weight=payload.get("support_weight"),
            contradict_weight=payload.get("contradict_weight"),
            contradiction_entropy=payload.get("contradiction_entropy"),
            action=payload.get("action"),
            expected_net_utility_r=payload.get("expected_net_utility_r"),
            admitted=payload.get("admitted"),
            allocated_capital_usdt=payload.get("allocated_capital_usdt"),
            target_risk_r=payload.get("target_risk_r"),
            rejection_reason=payload.get("rejection_reason"),
        )


@dataclass(frozen=True)
class ExAnteCostExpectation:
    """Canonical ex-ante cost expectation calculated prior to order dispatch.

    A plain record: the Rust side fills ``total_friction_bps`` from
    ``FrictionModel::total_friction_bps()``, and that model lives in the opportunity plane, which
    has no v8-next counterpart. So the total is supplied by the caller who measured it rather than
    re-summed here into a second, silently diverging friction convention.
    """

    total_friction_bps: float
    entry_fee_bps: float
    exit_fee_bps: float
    bid_ask_spread_bps: float
    funding_rate_est: float
    slippage_bps: float
    uncertainty_buffer_bps: float

    def as_dict(self) -> dict[str, Any]:
        return {field.name: getattr(self, field.name) for field in fields(self)}

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> ExAnteCostExpectation:
        return cls(
            total_friction_bps=payload["total_friction_bps"],
            entry_fee_bps=payload["entry_fee_bps"],
            exit_fee_bps=payload["exit_fee_bps"],
            bid_ask_spread_bps=payload["bid_ask_spread_bps"],
            funding_rate_est=payload["funding_rate_est"],
            slippage_bps=payload["slippage_bps"],
            uncertainty_buffer_bps=payload["uncertainty_buffer_bps"],
        )


@dataclass(frozen=True)
class ExAnteUncertainty:
    """Canonical ex-ante uncertainty metrics calculated Point-In-Time.

    ``uncertainty_penalty_r`` stays ``None`` at the reconciliation checkpoint: the Rust comment is
    explicit that the utility stage computes the penalty, so an earlier receipt must not carry one.
    """

    contradiction_entropy: float
    uncertainty_penalty_r: float | None
    effective_observer_count: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "contradiction_entropy": self.contradiction_entropy,
            "uncertainty_penalty_r": self.uncertainty_penalty_r,
            "effective_observer_count": self.effective_observer_count,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> ExAnteUncertainty:
        return cls(
            contradiction_entropy=payload["contradiction_entropy"],
            uncertainty_penalty_r=payload.get("uncertainty_penalty_r"),
            effective_observer_count=payload["effective_observer_count"],
        )


@dataclass(frozen=True, order=True)
class BeliefReceiptId:
    """Canonical belief receipt identifier (digest-derived hex).

    A distinct type from :class:`~v8_next.telemetry.identity.EconomicTraceId` and
    :class:`~v8_next.telemetry.identity.SpanId` (invariant 5), with its own digest domain and its
    own field set.
    """

    value: str

    @classmethod
    def new(cls, identifier: str) -> BeliefReceiptId:
        return cls(str(identifier))

    @classmethod
    def compute(
        cls,
        trace_id: EconomicTraceId,
        opportunity_id: str,
        span_id: SpanId,
        stage: BeliefStage,
        pit_timestamp: int,
        provenance_hash: str,
    ) -> BeliefReceiptId:
        """Deterministic identity of one ex-ante snapshot.

        Inputs are content only. No wall clock, no ledger position, no insertion order: two
        receipts recorded at different times over the same content share this identity.
        """
        return cls(
            canonical_digest(
                "BeliefReceiptId",
                [
                    trace_id.as_str(),
                    opportunity_id,
                    span_id.as_str(),
                    stage.as_str(),
                    int(pit_timestamp),
                    provenance_hash,
                ],
            )
        )

    def as_str(self) -> str:
        return self.value

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True)
class BeliefReceipt:
    """Point-In-Time snapshot of V8's ex-ante epistemic state (EEO-002, D-136-RP-001 §7).

    Contains only signals canonically computed prior to outcome realization. Frozen: once written,
    no later path, Oracle, Audit or execution outcome can mutate it (invariant 2). Build it with
    :meth:`new` or :meth:`from_dict`, then call :meth:`verify` before trusting it.
    """

    receipt_id: BeliefReceiptId
    trace_id: EconomicTraceId
    opportunity_id: str
    span_id: SpanId
    stage: BeliefStage
    pit_timestamp: int
    provenance: TraceProvenance
    expected_gross_edge_bps: float | None
    expected_net_utility_r: float | None
    expected_horizon_bars: int
    cost_expectation: ExAnteCostExpectation | None
    uncertainty: ExAnteUncertainty | None
    chosen_action: ChosenAction
    is_rejection: bool
    rejection_reason: str | None
    outcome_probabilities: tuple[float, ...] | None = None
    expected_mfe_r: float | None = None
    expected_mae_r: float | None = None
    #: Digest of the whole published body (see :meth:`compute_content_hash`). Additive to the
    #: Rust record, which has no such field: the Rust ``BeliefReceiptId`` binds only the lineage
    #: coordinates, so a body-only edit would leave the receipt identity intact.
    content_hash: str = ""

    @classmethod
    def new(
        cls,
        *,
        trace_id: EconomicTraceId,
        opportunity_id: str,
        span_id: SpanId,
        stage: BeliefStage,
        pit_timestamp: int,
        provenance: TraceProvenance,
        expected_gross_edge_bps: float | None,
        expected_net_utility_r: float | None,
        expected_horizon_bars: int,
        cost_expectation: ExAnteCostExpectation | None,
        uncertainty: ExAnteUncertainty | None,
        chosen_action: ChosenAction,
        is_rejection: bool,
        rejection_reason: str | None,
    ) -> BeliefReceipt:
        """Construct and fingerprint an immutable receipt.

        The three unavailable dimensions are set to ``None`` here and nowhere else: a caller
        cannot pass them. A stage that is not a :class:`BeliefStage` is refused by name, so an
        evidence-plane stage never reaches the record (invariant 3).
        """
        if not opportunity_id:
            raise TraceLineageError("opportunity_id cannot be empty in BeliefReceipt")
        resolved_stage = _require_belief_stage(stage)
        provenance_hash = provenance.compute_hash()
        draft = cls(
            receipt_id=BeliefReceiptId.compute(
                trace_id, opportunity_id, span_id, resolved_stage, pit_timestamp, provenance_hash
            ),
            trace_id=trace_id,
            opportunity_id=str(opportunity_id),
            span_id=span_id,
            stage=resolved_stage,
            pit_timestamp=int(pit_timestamp),
            provenance=provenance,
            expected_gross_edge_bps=expected_gross_edge_bps,
            expected_net_utility_r=expected_net_utility_r,
            expected_horizon_bars=int(expected_horizon_bars),
            cost_expectation=cost_expectation,
            uncertainty=uncertainty,
            chosen_action=chosen_action,
            is_rejection=bool(is_rejection),
            rejection_reason=rejection_reason,
            outcome_probabilities=None,
            expected_mfe_r=None,
            expected_mae_r=None,
        )
        return replace(draft, content_hash=draft.compute_content_hash())

    def compute_content_hash(self) -> str:
        """Digest of every published field of this receipt except ``content_hash`` itself.

        The Rust receipt identity covers the lineage coordinates (trace, opportunity, span, stage,
        PIT timestamp, provenance) and nothing else, so an edit confined to the ex-ante body would
        still re-derive to the same ``receipt_id``. This digest closes that gap: it covers
        ``as_dict()``, which is exactly what a reader receives, so the body and the identity are
        both bound to the record a consumer was handed.
        """
        body = {key: value for key, value in self.as_dict().items() if key != "content_hash"}
        return canonical_digest("BeliefReceiptContent", body)

    def verify(self) -> None:
        """Re-derive the receipt identity from content and fail closed on any mismatch.

        Three checks, all fail-closed:

        * the receipt id must re-derive from the recorded lineage fields (a receipt whose
          coordinates were rewritten after the fact does not);
        * each :data:`UNAVAILABLE_EX_ANTE_DIMENSIONS` entry must still be ``None``, because V8
          does not compute it ex ante and a populated value would be a fabricated belief;
        * the content digest must re-derive from the recorded body (a receipt whose ex-ante values
          were rewritten does not, even though its id still would). This check runs last so a
          fabricated dimension is reported as what it is rather than as a generic mismatch.
        """
        expected = BeliefReceiptId.compute(
            self.trace_id,
            self.opportunity_id,
            self.span_id,
            self.stage,
            self.pit_timestamp,
            self.provenance.compute_hash(),
        )
        if expected != self.receipt_id:
            raise BeliefReceiptVerificationError(
                f"BeliefReceipt {self.receipt_id} does not re-derive from its content "
                f"(content yields {expected})"
            )
        for name, value in (
            ("outcome_probabilities", self.outcome_probabilities),
            ("expected_mfe_r", self.expected_mfe_r),
            ("expected_mae_r", self.expected_mae_r),
        ):
            if value is not None:
                raise FabricatedBeliefError(
                    f"BeliefReceipt {self.receipt_id} carries {name}, which V8 does not "
                    f"compute Point-In-Time"
                )
        recomputed_content = self.compute_content_hash()
        if self.content_hash != recomputed_content:
            raise BeliefReceiptVerificationError(
                f"BeliefReceipt {self.receipt_id} body does not re-derive from its content "
                f"(recorded {self.content_hash}, content yields {recomputed_content})"
            )

    def unavailable_dimensions(self) -> tuple[str, ...]:
        """The ex-ante dimensions this receipt leaves explicitly unavailable."""
        return UNAVAILABLE_EX_ANTE_DIMENSIONS

    def as_dict(self) -> dict[str, Any]:
        return {
            "receipt_id": self.receipt_id.as_str(),
            "trace_id": self.trace_id.as_str(),
            "opportunity_id": self.opportunity_id,
            "span_id": self.span_id.as_str(),
            "stage": self.stage.as_str(),
            "pit_timestamp": self.pit_timestamp,
            "provenance": self.provenance.as_dict(),
            "expected_gross_edge_bps": self.expected_gross_edge_bps,
            "expected_net_utility_r": self.expected_net_utility_r,
            "expected_horizon_bars": self.expected_horizon_bars,
            "cost_expectation": (
                None if self.cost_expectation is None else self.cost_expectation.as_dict()
            ),
            "uncertainty": None if self.uncertainty is None else self.uncertainty.as_dict(),
            "chosen_action": self.chosen_action.as_dict(),
            "is_rejection": self.is_rejection,
            "rejection_reason": self.rejection_reason,
            "outcome_probabilities": (
                None if self.outcome_probabilities is None else list(self.outcome_probabilities)
            ),
            "expected_mfe_r": self.expected_mfe_r,
            "expected_mae_r": self.expected_mae_r,
            "content_hash": self.content_hash,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> BeliefReceipt:
        """Rebuild a receipt from :meth:`as_dict` output; call :meth:`verify` to check it."""
        cost = payload.get("cost_expectation")
        uncertainty = payload.get("uncertainty")
        probabilities = payload.get("outcome_probabilities")
        return cls(
            receipt_id=BeliefReceiptId(str(payload["receipt_id"])),
            trace_id=EconomicTraceId(str(payload["trace_id"])),
            opportunity_id=str(payload["opportunity_id"]),
            span_id=SpanId(str(payload["span_id"])),
            stage=_require_belief_stage(str(payload["stage"])),
            pit_timestamp=int(payload["pit_timestamp"]),
            provenance=TraceProvenance.new(
                str(payload["provenance"]["tape_hash"]),
                str(payload["provenance"]["policy_hash"]),
                str(payload["provenance"]["constitution_hash"]),
                str(payload["provenance"]["code_hash"]),
            ),
            expected_gross_edge_bps=payload.get("expected_gross_edge_bps"),
            expected_net_utility_r=payload.get("expected_net_utility_r"),
            expected_horizon_bars=int(payload["expected_horizon_bars"]),
            cost_expectation=None if cost is None else ExAnteCostExpectation.from_dict(cost),
            uncertainty=None if uncertainty is None else ExAnteUncertainty.from_dict(uncertainty),
            chosen_action=ChosenAction.from_dict(payload["chosen_action"]),
            is_rejection=bool(payload["is_rejection"]),
            rejection_reason=payload.get("rejection_reason"),
            outcome_probabilities=(
                None if probabilities is None else tuple(float(p) for p in probabilities)
            ),
            expected_mfe_r=payload.get("expected_mfe_r"),
            expected_mae_r=payload.get("expected_mae_r"),
            content_hash=str(payload.get("content_hash", "")),
        )


class DecisionBeliefLedger:
    """Append-only decision belief ledger (EEO-002, D-136-RP-001 §7).

    Stores and indexes ex-ante epistemic snapshots across the canonical decision trajectory. The
    ledger grows; it never rewrites. ``append`` of a receipt whose id already exists is idempotent
    when the body is byte-identical and refused by name otherwise, and
    :meth:`validate_lineage` re-verifies every stored receipt identity from its content.
    """

    def __init__(self) -> None:
        self._receipts: list[BeliefReceipt] = []
        self._receipts_by_id: dict[BeliefReceiptId, int] = {}
        self._by_trace: dict[EconomicTraceId, list[int]] = {}
        self._by_opportunity: dict[str, list[int]] = {}

    def append(self, receipt: BeliefReceipt) -> None:
        """Append one immutable receipt (idempotent for an identical retry)."""
        existing = self._receipts_by_id.get(receipt.receipt_id)
        if existing is not None:
            if self._receipts[existing] != receipt:
                raise ConflictingBeliefReceiptError(
                    f"Conflicting BeliefReceipt mutation attempt for receipt_id "
                    f"{receipt.receipt_id}"
                )
            return
        index = len(self._receipts)
        self._by_trace.setdefault(receipt.trace_id, []).append(index)
        self._by_opportunity.setdefault(receipt.opportunity_id, []).append(index)
        self._receipts_by_id[receipt.receipt_id] = index
        self._receipts.append(receipt)

    def get(self, receipt_id: BeliefReceiptId) -> BeliefReceipt | None:
        index = self._receipts_by_id.get(receipt_id)
        return None if index is None else self._receipts[index]

    def receipts_for_trace(self, trace_id: EconomicTraceId) -> tuple[BeliefReceipt, ...]:
        return tuple(self._receipts[i] for i in self._by_trace.get(trace_id, ()))

    def receipts_for_opportunity(self, opportunity_id: str) -> tuple[BeliefReceipt, ...]:
        return tuple(self._receipts[i] for i in self._by_opportunity.get(opportunity_id, ()))

    def final_belief_for_opportunity(self, opportunity_id: str) -> BeliefReceipt | None:
        """The last receipt recorded for an opportunity (invariant 4: rejections included)."""
        indices = self._by_opportunity.get(opportunity_id)
        if not indices:
            return None
        return self._receipts[indices[-1]]

    def all(self) -> tuple[BeliefReceipt, ...]:
        return tuple(self._receipts)

    def is_empty(self) -> bool:
        return not self._receipts

    def __len__(self) -> int:
        return len(self._receipts)

    def validate_lineage(self) -> None:
        """Validate epistemic and temporal integrity of the whole ledger.

        The Rust version checks a non-empty opportunity id and uncorrupted provenance hashes. This
        port also re-derives each receipt identity (:meth:`BeliefReceipt.verify`), because a
        lineage check that never re-derives the identity it is validating accepts a receipt that
        was rewritten after it was recorded.
        """
        for receipt in self._receipts:
            if not receipt.opportunity_id:
                raise TraceLineageError(
                    f"BeliefReceipt {receipt.receipt_id} has empty opportunity_id"
                )
            if (
                not receipt.provenance.tape_hash
                or not receipt.provenance.policy_hash
                or not receipt.provenance.constitution_hash
                or not receipt.provenance.code_hash
            ):
                raise TraceLineageError(
                    f"BeliefReceipt {receipt.receipt_id} has corrupted provenance hashes"
                )
            receipt.verify()

    def as_dict(self) -> dict[str, Any]:
        return {
            "receipts": [receipt.as_dict() for receipt in self._receipts],
            "receipt_count": len(self._receipts),
            "ledger_digest": canonical_digest(
                "DecisionBeliefLedger",
                [
                    receipt.as_dict()["receipt_id"]
                    for receipt in self._receipts
                ],
            ),
        }

    def to_json(self) -> str:
        """Pretty JSON of the whole ledger (the Rust ``to_json``)."""
        return json.dumps(self.as_dict(), indent=2, sort_keys=True)

    @classmethod
    def from_json(cls, payload: str) -> DecisionBeliefLedger:
        """Rebuild a ledger from :meth:`to_json` output, preserving append order."""
        document = json.loads(payload)
        ledger = cls()
        for item in document["receipts"]:
            ledger.append(BeliefReceipt.from_dict(item))
        return ledger
