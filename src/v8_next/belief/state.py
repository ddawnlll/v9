"""Versioned market-state view — ``MarketBeliefV1`` (M1-03).

What this type is
-----------------
One instrument's *epistemic* market state at one decision time, built only from
point-in-time inputs (:class:`~v8_next.domain.observation.ObservationView` and
:class:`~v8_next.domain.horizons.HorizonContext`). It is a *view*, not a
forecast: it names what the data supports, how strong that support is, which
dimensions are missing, and how uncertain the whole picture is.

What this type is not
---------------------
* **Not a probability model.** ``RegimeScores`` carries ``EpistemicLabel.SCORE``.
  Scores are bounded heuristics that need not sum to one and have no calibration
  evidence; :class:`EpistemicLabel.PROBABILITY` is rejected by this schema
  version so a consumer cannot read a score as a calibrated probability by
  accident.
* **Not an order.** ``advisory_only`` is pinned ``True`` and ``order_authority``
  is pinned ``False``; construction fails otherwise. Nothing downstream may
  treat a belief as permission to trade.
* **Not an opinion about missing data.** A dimension whose inputs are absent is
  reported as ``MISSING``/``UNKNOWN`` with ``None`` values. This module never
  substitutes a default price, a zero spread or an inferred quote.

Identity
--------
``version_hash`` is the canonical digest of the belief's own content plus
``schema_version``. Identical evidence produces an identical hash on any
machine; a persisted belief whose hash no longer re-derives is rejected
(:meth:`MarketBeliefV1` validation and :func:`verify_version_hash`). Because the
hash covers content only, replaying the same inputs reproduces it exactly.

Staleness is content, not a clock reading
-----------------------------------------
``advance`` (see :mod:`v8_next.belief.update`) compares ``now_ns`` against
``max_input_available_at`` and, when the evidence is too old, produces a belief
whose ``status`` is ``STALE``. A stale belief can never be promoted back to
``FRESH`` without new evidence; :func:`MarketBeliefV1` enforces the
``STALE``⇏``FRESH`` direction by refusing a ``FRESH`` status on a belief whose
``max_input_available_at`` lies beyond its staleness horizon only through the
builder -- the schema-level guard is that ``max_input_available_at`` may not
exceed ``decision_time_ns``.
"""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum
from typing import Any, Final, Self

from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator

from v8_next.domain.horizons import HorizonAlignment, Timeframe
from v8_next.domain.observation import QualityState, worst_quality
from v8_next.telemetry.identity import canonical_digest

__all__ = [
    "BELIEF_SCHEMA_VERSION",
    "BeliefIntegrityError",
    "BeliefProvenance",
    "BeliefStatus",
    "BreakoutState",
    "DataRole",
    "EpistemicLabel",
    "FRESH_QUALITY",
    "HorizonConsistency",
    "HorizonsView",
    "LiquidityAvailability",
    "LiquidityView",
    "MarketBeliefV1",
    "RegimeKind",
    "RegimeScores",
    "StateOrigin",
    "StructureDirection",
    "StructureView",
    "SwingState",
    "TransitionEvidence",
    "TransitionReason",
    "UncertaintyState",
    "VolatilityChangeState",
    "VolatilityLevel",
    "VolatilityView",
    "belief_version_hash",
    "transition_event_id",
    "verify_version_hash",
]

#: Schema identity. Bumped only with an incompatible change to this module; a
#: belief carrying any other value is rejected instead of coerced.
BELIEF_SCHEMA_VERSION: Final[str] = "MarketBeliefV1/1"

#: Fixed-point scales for every stored ``Decimal``. Quantising at construction
#: keeps ``version_hash`` stable across equal values that arrived with different
#: textual precision (``"0.30"`` vs ``"0.3"``).
SCORE_QUANTUM: Final[Decimal] = Decimal("0.000001")
BPS_QUANTUM: Final[Decimal] = Decimal("0.0001")
PRICE_QUANTUM: Final[Decimal] = Decimal("0.00000001")

#: Documented meaning of the regime scores (also carried on every belief).
REGIME_SCORE_DEFINITION: Final[str] = (
    "bounded efficiency-ratio heuristic (window path efficiency, last-move ratio, "
    "sign-flip rate); scores are not probabilities, are not calibrated and do not sum to one"
)

#: Quality states that still permit a usable fresh view. ``UNKNOWN`` is excluded:
#: a state nobody could establish is not evidence, so the belief must not be FRESH.
FRESH_QUALITY: Final[frozenset[QualityState]] = frozenset(
    {QualityState.OK, QualityState.STALE}
)

class BeliefIntegrityError(ValueError):
    """A belief's content no longer re-derives its ``version_hash``."""

    code = "BELIEF_INTEGRITY_ERROR"


class EpistemicLabel(StrEnum):
    """What a score is allowed to claim."""

    #: Bounded heuristic score. The only label ``MarketBeliefV1`` accepts.
    SCORE = "SCORE"
    #: Calibrated probability. Rejected until independent validation exists.
    PROBABILITY = "PROBABILITY"


class RegimeKind(StrEnum):
    """Market regime named by the belief, or an explicit absence of one."""

    TREND = "TREND"
    RANGE = "RANGE"
    TRANSITION = "TRANSITION"
    SHOCK = "SHOCK"
    UNKNOWN = "UNKNOWN"


class StructureDirection(StrEnum):
    UP = "UP"
    DOWN = "DOWN"
    FLAT = "FLAT"
    UNKNOWN = "UNKNOWN"


class SwingState(StrEnum):
    NEAR_HIGH = "NEAR_HIGH"
    NEAR_LOW = "NEAR_LOW"
    MIDDLE = "MIDDLE"
    UNKNOWN = "UNKNOWN"


class BreakoutState(StrEnum):
    UP = "UP"
    DOWN = "DOWN"
    NONE = "NONE"
    UNKNOWN = "UNKNOWN"


class VolatilityLevel(StrEnum):
    LOW = "LOW"
    NORMAL = "NORMAL"
    HIGH = "HIGH"
    UNKNOWN = "UNKNOWN"


class VolatilityChangeState(StrEnum):
    RISING = "RISING"
    FALLING = "FALLING"
    FLAT = "FLAT"
    UNKNOWN = "UNKNOWN"


class LiquidityAvailability(StrEnum):
    """Whether liquidity *evidence* exists — never an assumption that liquidity does."""

    #: Every proxy channel (mark, quote, open interest, funding) was readable.
    PRESENT = "PRESENT"
    #: Some channels are readable, some are absent.
    PARTIAL = "PARTIAL"
    #: No liquidity channel was carried by the input snapshot at all.
    MISSING = "MISSING"
    #: A channel was carried but unreadable/malformed: no value may be invented.
    UNKNOWN = "UNKNOWN"


class BeliefStatus(StrEnum):
    """Actionability gate for the belief as a whole."""

    FRESH = "FRESH"
    STALE = "STALE"
    #: Required inputs are absent; the belief abstains instead of guessing.
    ABSTAIN = "ABSTAIN"
    #: Inputs exist but their quality state forbids naming a state.
    UNKNOWN = "UNKNOWN"


class StateOrigin(StrEnum):
    """How this belief instance came to be, for update audits."""

    INITIAL = "INITIAL"
    UPDATED = "UPDATED"
    UNCHANGED = "UNCHANGED"
    STALE = "STALE"
    MISSING_INPUT = "MISSING_INPUT"


class UncertaintyState(StrEnum):
    """Epistemic uncertainty as a state enum (not a variance or a confidence interval).

    ``unknown`` when the belief carries no actionable evidence; ``high`` when the
    evidence is stale or the top two regime scores are within the high margin;
    ``medium`` when the margin is thin or horizons/liquidity are degraded;
    ``low`` otherwise.
    """

    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    UNKNOWN = "UNKNOWN"


class TransitionReason(StrEnum):
    """Why a regime transition was accepted."""

    #: A competing regime beat the incumbent by at least the hysteresis band,
    #: for at least the minimum candidate streak.
    SCORE_BAND_EXCEEDED = "SCORE_BAND_EXCEEDED"
    #: The previous belief had no regime and evidence now supports one.
    RECOVERY_FROM_UNKNOWN = "RECOVERY_FROM_UNKNOWN"


class HorizonConsistency(StrEnum):
    ALIGNED = "ALIGNED"
    DEGRADED = "DEGRADED"
    MISSING = "MISSING"
    UNKNOWN = "UNKNOWN"


class DataRole(StrEnum):
    """Lineage role of the belief's inputs."""

    #: Derived from observations available at the decision cut.
    OBSERVED = "OBSERVED"
    #: Derived from other derived state.
    DERIVED = "DERIVED"


class RegimeScores(BaseModel):
    """Bounded regime evidence. Scores are *not* probabilities and do not sum to one."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    label: EpistemicLabel = EpistemicLabel.SCORE
    trend: Decimal = Field(ge=0, le=1)
    range: Decimal = Field(ge=0, le=1)
    transition: Decimal = Field(ge=0, le=1)
    shock: Decimal = Field(ge=0, le=1)
    #: Evidence-insufficiency mass: ``1`` when no regime could be scored at all.
    unknown: Decimal = Field(ge=0, le=1)
    definition: str = REGIME_SCORE_DEFINITION

    @model_validator(mode="after")
    def _check_label(self) -> Self:
        if self.label is not EpistemicLabel.SCORE:
            raise ValueError(
                "regime scores are not calibrated probabilities; "
                f"{self.label.value} is not accepted by {BELIEF_SCHEMA_VERSION}"
            )
        return self

    def score(self, regime: RegimeKind) -> Decimal:
        """Return the score attached to ``regime``; ``UNKNOWN`` maps to ``unknown``."""
        if regime is RegimeKind.TREND:
            return self.trend
        if regime is RegimeKind.RANGE:
            return self.range
        if regime is RegimeKind.TRANSITION:
            return self.transition
        if regime is RegimeKind.SHOCK:
            return self.shock
        return self.unknown


class StructureView(BaseModel):
    """Market structure at the window level; prices are ``None`` when unmeasured."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    direction: StructureDirection
    swing_state: SwingState
    breakout_state: BreakoutState
    window_low: Decimal | None = None
    window_high: Decimal | None = None
    reference_price: Decimal | None = None
    quality: QualityState

    @model_validator(mode="after")
    def _check_references(self) -> Self:
        if (self.window_low is None) != (self.window_high is None):
            raise ValueError("window bounds must be present or absent together")
        if self.window_low is not None and self.window_high is not None:
            if self.window_low > self.window_high:
                raise ValueError("window_low cannot exceed window_high")
        if self.quality is QualityState.MISSING and (
            self.window_low is not None
            or self.window_high is not None
            or self.reference_price is not None
        ):
            raise ValueError("MISSING structure quality cannot carry measured prices")
        if self.quality is QualityState.MISSING and self.direction is not StructureDirection.UNKNOWN:
            raise ValueError("MISSING structure quality cannot name a direction")
        return self


class VolatilityView(BaseModel):
    """Volatility evidence; ``None`` means unmeasured, never zero."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    level: VolatilityLevel
    #: Mean high-low range of the window, in basis points of close.
    range_bps: Decimal | None = Field(default=None, ge=0)
    #: Share of window bars whose range is no larger than the latest bar's range.
    range_percentile: Decimal | None = Field(default=None, ge=0, le=1)
    change_state: VolatilityChangeState
    quality: QualityState

    @model_validator(mode="after")
    def _check_missing(self) -> Self:
        if self.level is VolatilityLevel.UNKNOWN:
            if self.range_bps is not None or self.range_percentile is not None:
                raise ValueError("UNKNOWN volatility cannot carry measured values")
            if self.change_state is not VolatilityChangeState.UNKNOWN:
                raise ValueError("UNKNOWN volatility cannot name a change state")
        return self


class LiquidityView(BaseModel):
    """Liquidity *evidence*, proxy-only (OHLCV + venue-published channels).

    Every numeric field is ``None`` when its channel was absent. ``proxy_only``
    records that no L2 book, fill model or sponsorship estimate exists.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    availability: LiquidityAvailability
    proxy_only: bool = True
    mark_price: Decimal | None = Field(default=None, gt=0)
    spread_bps: Decimal | None = Field(default=None, ge=0)
    open_interest: Decimal | None = Field(default=None, ge=0)
    funding_rate: Decimal | None = None
    #: Channels that were absent or unreadable, e.g. ``("MARK", "QUOTE:spread")``.
    missing_dimensions: tuple[str, ...] = ()
    quality: QualityState

    @model_validator(mode="after")
    def _check_availability(self) -> Self:
        if self.proxy_only is not True:
            raise ValueError(
                "this belief version carries liquidity proxies only; an L2 book or fill "
                "model is not available and cannot be claimed"
            )
        values = (
            self.mark_price,
            self.spread_bps,
            self.open_interest,
            self.funding_rate,
        )
        present = sum(1 for value in values if value is not None)
        if self.availability is LiquidityAvailability.MISSING:
            if present:
                raise ValueError("MISSING liquidity cannot carry any proxy value")
            if self.quality is not QualityState.MISSING:
                raise ValueError("MISSING liquidity must carry MISSING quality")
        if self.availability is LiquidityAvailability.PRESENT and present != len(values):
            raise ValueError("PRESENT liquidity requires every proxy channel")
        if self.availability is LiquidityAvailability.PARTIAL and not 0 < present < len(values):
            raise ValueError("PARTIAL liquidity requires some channels and some absences")
        if self.availability is LiquidityAvailability.UNKNOWN and present == len(values):
            raise ValueError("UNKNOWN liquidity must have at least one unreadable channel")
        if (
            self.availability in (LiquidityAvailability.PARTIAL, LiquidityAvailability.UNKNOWN)
            and not self.missing_dimensions
        ):
            raise ValueError("missing_dimensions must name absent/unreadable channels")
        return self


class HorizonsView(BaseModel):
    """15m/1h/4h alignment as handed over by :class:`HorizonContext`."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    alignments: tuple[HorizonAlignment, ...]
    consistency: HorizonConsistency
    complete_count: StrictInt = Field(ge=0, le=3)
    as_of_max_ns: StrictInt

    @model_validator(mode="after")
    def _check_alignments(self) -> Self:
        expected = tuple(Timeframe)
        if len(self.alignments) != len(expected):
            raise ValueError("belief requires exactly one alignment per timeframe")
        for timeframe, alignment in zip(expected, self.alignments, strict=True):
            if alignment.timeframe is not timeframe:
                raise ValueError(
                    f"alignment for {timeframe.value} carries {alignment.timeframe.value}"
                )
        complete = sum(1 for alignment in self.alignments if alignment.complete)
        if complete != self.complete_count:
            raise ValueError("complete_count does not match the alignments")
        as_of_max = max(alignment.as_of_ns for alignment in self.alignments)
        if as_of_max != self.as_of_max_ns:
            raise ValueError("as_of_max_ns does not match the alignments")
        return self


def transition_event_id(
    *,
    previous_state_id: str,
    from_regime: RegimeKind,
    to_regime: RegimeKind,
    reason: TransitionReason,
    hysteresis_band: Decimal,
    candidate_score: Decimal,
    incumbent_score: Decimal,
    trigger_observation_ids: tuple[str, ...],
    occurred_at_ns: int,
) -> str:
    """Deterministic identity of one audited regime transition."""
    return canonical_digest(
        "market_belief_transition",
        {
            "previous_state_id": previous_state_id,
            "from_regime": from_regime.value,
            "to_regime": to_regime.value,
            "reason": reason.value,
            "hysteresis_band": str(hysteresis_band),
            "candidate_score": str(candidate_score),
            "incumbent_score": str(incumbent_score),
            "trigger_observation_ids": list(trigger_observation_ids),
            "occurred_at_ns": occurred_at_ns,
        },
    )


class TransitionEvidence(BaseModel):
    """Audit record of one accepted regime change.

    Suppressed candidates are *not* transitions: they stay observable through
    ``MarketBeliefV1.regime_candidate``/``candidate_streak`` instead.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    event_id: str
    previous_state_id: str
    from_regime: RegimeKind
    to_regime: RegimeKind
    reason: TransitionReason
    hysteresis_band: Decimal = Field(ge=0)
    candidate_score: Decimal = Field(ge=0, le=1)
    incumbent_score: Decimal = Field(ge=0, le=1)
    trigger_observation_ids: tuple[str, ...]
    occurred_at_ns: StrictInt

    @model_validator(mode="after")
    def _check_event_id(self) -> Self:
        if self.from_regime is self.to_regime:
            raise ValueError("a transition must change the regime")
        expected = transition_event_id(
            previous_state_id=self.previous_state_id,
            from_regime=self.from_regime,
            to_regime=self.to_regime,
            reason=self.reason,
            hysteresis_band=self.hysteresis_band,
            candidate_score=self.candidate_score,
            incumbent_score=self.incumbent_score,
            trigger_observation_ids=self.trigger_observation_ids,
            occurred_at_ns=self.occurred_at_ns,
        )
        if self.event_id != expected:
            raise BeliefIntegrityError(
                f"transition event id does not re-derive from content "
                f"({self.event_id} != {expected})"
            )
        return self


class BeliefProvenance(BaseModel):
    """Where the belief came from: inputs, policy, model and schema versions."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source_hash: str
    feature_version: str
    model_version: str
    policy_version: str
    policy_digest: str
    data_role: DataRole = DataRole.OBSERVED


class MarketBeliefV1(BaseModel):
    """Versioned, PIT-bound market-state view for one instrument at one decision time."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = BELIEF_SCHEMA_VERSION
    instrument_id: str
    decision_time_ns: StrictInt
    #: Latest availability clock among the inputs this belief actually used.
    max_input_available_at: StrictInt
    #: Content ids of the inputs this belief actually used, sorted and unique.
    input_observation_ids: tuple[str, ...]
    #: Cheap digest of the dependency set; equal fingerprints mean "no recompute".
    dependency_fingerprint: str
    policy_version: str
    status: BeliefStatus
    origin: StateOrigin
    #: Fail-closed aggregate of the evidence quality (bars + horizons).
    quality: QualityState
    regime_label: RegimeKind
    regime: RegimeScores
    regime_candidate: RegimeKind
    candidate_streak: StrictInt = Field(ge=0)
    structure: StructureView
    volatility: VolatilityView
    liquidity: LiquidityView
    horizons: HorizonsView
    uncertainty: UncertaintyState
    transition: TransitionEvidence | None = None
    changed_fields: tuple[str, ...] = ()
    missing_inputs: tuple[str, ...] = ()
    provenance: BeliefProvenance
    version_hash: str = ""
    #: This record is a view, never an order or a permission.
    advisory_only: bool = True
    order_authority: bool = False

    @model_validator(mode="after")
    def _check_contract(self) -> Self:
        if not self.instrument_id.strip():
            raise ValueError("instrument_id cannot be empty")
        if self.schema_version != BELIEF_SCHEMA_VERSION:
            raise ValueError(
                f"unknown belief schema version {self.schema_version!r}; "
                f"expected {BELIEF_SCHEMA_VERSION!r}"
            )
        if self.max_input_available_at > self.decision_time_ns:
            raise ValueError("max_input_available_at cannot exceed decision_time_ns")
        if self.max_input_available_at < 0:
            raise ValueError("max_input_available_at must be non-negative")
        if list(self.input_observation_ids) != sorted(set(self.input_observation_ids)):
            raise ValueError("input_observation_ids must be sorted and unique")
        if list(self.changed_fields) != sorted(set(self.changed_fields)):
            raise ValueError("changed_fields must be sorted and unique")
        if list(self.missing_inputs) != sorted(set(self.missing_inputs)):
            raise ValueError("missing_inputs must be sorted and unique")
        if self.advisory_only is not True:
            raise ValueError("a market belief is advisory only and cannot be actionable")
        if self.order_authority is not False:
            raise ValueError("a market belief carries no order authority")
        if self.status is BeliefStatus.ABSTAIN:
            if self.regime_label is not RegimeKind.UNKNOWN:
                raise ValueError("an ABSTAIN belief cannot name a regime")
            if not self.missing_inputs:
                raise ValueError("an ABSTAIN belief must name the missing inputs")
        if self.status is BeliefStatus.FRESH:
            if self.quality not in FRESH_QUALITY:
                raise ValueError(
                    f"a FRESH belief cannot carry {self.quality.value} evidence quality"
                )
            if not self.missing_inputs == ():
                raise ValueError("a FRESH belief cannot declare missing inputs")
        if self.transition is not None and self.status is not BeliefStatus.FRESH:
            raise ValueError("only a FRESH belief can carry a transition event")
        if self.transition is not None:
            if self.transition.occurred_at_ns != self.decision_time_ns:
                raise ValueError("transition occurred_at_ns must equal decision_time_ns")
            if self.transition.to_regime is not self.regime_label:
                raise ValueError("transition target must be the belief's regime")
        if self.regime_candidate is RegimeKind.UNKNOWN and self.candidate_streak != 0:
            raise ValueError("an UNKNOWN candidate cannot carry a streak")
        expected = belief_version_hash(self)
        if not self.version_hash:
            object.__setattr__(self, "version_hash", expected)
        elif self.version_hash != expected:
            raise BeliefIntegrityError(
                f"version_hash does not re-derive from content "
                f"({self.version_hash} != {expected})"
            )
        return self

    def is_actionable_evidence(self) -> bool:
        """``True`` only for a fresh belief; stale/abstained beliefs are not evidence."""
        return self.status is BeliefStatus.FRESH

    def verify(self) -> None:
        """Raise :class:`BeliefIntegrityError` if the content no longer re-derives."""
        verify_version_hash(self)


def belief_version_hash(belief: MarketBeliefV1) -> str:
    """Canonical digest of ``belief``'s content plus its schema version."""
    payload: dict[str, Any] = belief.model_dump(mode="json", exclude={"version_hash"})
    payload["schema_version"] = BELIEF_SCHEMA_VERSION
    return canonical_digest("market_belief", payload)


def verify_version_hash(belief: MarketBeliefV1) -> None:
    """Fail closed when a persisted belief's hash does not match its content."""
    expected = belief_version_hash(belief)
    if belief.version_hash != expected:
        raise BeliefIntegrityError(
            f"belief {belief.instrument_id}@{belief.decision_time_ns} has a stale or "
            f"tampered version_hash ({belief.version_hash} != {expected})"
        )


def aggregate_quality(states: tuple[QualityState, ...]) -> QualityState:
    """Fail-closed worst-of, tolerating an empty tuple as ``MISSING``."""
    if not states:
        return QualityState.MISSING
    return worst_quality(states[0], *states[1:])
