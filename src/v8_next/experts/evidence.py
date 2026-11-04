"""Context-aware evidence from the canonical 28 expert witnesses (M1-05).

The 28 registry experts are epistemic witnesses, not economic sovereigns
(``v8_next.experts.registry``): they observe a frame and emit a stance, and they
own no capital, no opportunity and no execution path. This module is the bridge
between the V9 context layer -- ``ObservationView``, ``HorizonContext``, an
optional ``MarketBeliefV1`` -- and those witnesses. It adds exactly three
things:

**Coverage.** One :class:`ExpertEvidenceV1` per canonical expert id, in registry
order. Observer names are resolved through the registry, so an alias or a
repeated observer id resolves to the same canonical witness and can never
inflate coverage; the bundle refuses to construct with two rows for one expert
and records the deduplicated ids in ``duplicate_witness_ids``.

**Explicit shortfall.** Warmup shortfall, blocked input quality, an incomplete
decision horizon, an unavailable dependency group and absent positioning
readings each abstain with a typed :class:`EvidenceReason` *before* an expert is
dispatched -- no crash, no fabricated SUPPORT, no expert reasoning about inputs
it does not have.

**Provenance.** Every row is bound to one ``context_ref`` (belief version hash +
horizon as-of times + usable observation ids) and to a deterministic
``evidence_id``; the bundle carries the deduplicated input-dependency groups and
a provenance digest that names the version, the registry, the cache version and
the frame's *content* digest (decision time, bar count, last bar identity, usable
observation ids). The provenance does not depend on how the cache reached the
bar, so a freshly seeded and an extended cache agree on the same frame.

What this module deliberately does not do:

* It creates no opportunity. ``opportunity`` is a pass-through for an identity an
  upstream layer already created; passing ``None`` is the normal evidence path.
* It imports no execution, order, portfolio or capital surface, and every model
  is ``advisory_only`` with ``order_authority`` pinned to ``False``. Evidence is
  input to a decision, never permission for one.
* It never reimplements an expert rule: the only dispatch is
  :func:`~v8_next.experts.registry.observe_expert`, i.e. the registry's own
  observer functions with the registry's own witness metadata.

Contexts are inputs, not opportunity creators: all 28 candidates are evaluated
against the *same* frame, the same context and the same shared
:class:`~v8_next.experts.cache.FeatureCache`, and an unavailable context never
becomes a reason to invent a candidate.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from types import MappingProxyType
from typing import Final, Literal, Self

from pydantic import BaseModel, ConfigDict, model_validator

from v8_next.belief.state import MarketBeliefV1, verify_version_hash
from v8_next.domain.horizons import HorizonContext, Timeframe
from v8_next.domain.market import CausalFrame
from v8_next.domain.observation import (
    ObservationView,
    QualityState,
    observation_id,
    worst_quality,
)
from v8_next.domain.positioning import PositioningReading
from v8_next.economics.decisions import Opportunity, StanceKind
from v8_next.experts.cache import FeatureCache
from v8_next.experts.registry import (
    CANONICAL_28_EXPERTS,
    ExpertSpec,
    get_expert,
    observe_expert,
)
from v8_next.telemetry.identity import canonical_digest

__all__ = [
    "EVIDENCE_VERSION",
    "EvidenceBundle",
    "EvidenceReason",
    "ExpertEvidenceV1",
    "dependency_group_key",
    "produce_evidence",
]

#: Bumped whenever the meaning of an evidence row changes (a stance convention,
#: a gate, a bound input). The literal is carried by every row and the bundle and
#: is part of every ``evidence_id``, so a bump makes old ids unreachable instead
#: of silently reinterpreted.
EVIDENCE_VERSION: Final = "v9.expert-evidence.1"

#: Confidence a row carries when its expert asserts a stance. It is the
#: *determinacy of the assertion* (the expert did or did not make a claim), not a
#: calibrated probability, and this module never derives a probability from it.
_ASSERTED_CONFIDENCE: Final = Decimal(1)
_ABSTAIN_CONFIDENCE: Final = Decimal(0)


class EvidenceReason(StrEnum):
    """Typed reason this module abstained *before* observing (never a verdict).

    The values are deliberately distinct from every expert reason code (the
    registry emits ``WARMUP``/``NO_OPPORTUNITY``/family reasons), so a consumer
    can tell "the layer withheld this witness" from "the witness looked and
    found nothing".
    """

    #: The frame does not carry the history the expert declares in the registry.
    WARMUP_INPUT = "WARMUP_INPUT"
    #: A declared input (positioning readings) was not supplied at all.
    MISSING_INPUT = "MISSING_INPUT"
    #: Known inputs are missing or unknown at the cut, so nothing may be asserted.
    INPUT_QUALITY = "INPUT_QUALITY"
    #: The decision horizon the frame belongs to is absent, incomplete or untrusted.
    CONTEXT_INCOMPLETE = "CONTEXT_INCOMPLETE"
    #: A dependency group the expert depends on was declared unavailable.
    DEPENDENCY_UNAVAILABLE = "DEPENDENCY_UNAVAILABLE"


#: Worst-of quality states that forbid dispatching a witness, mapped to the typed
#: reason. ``STALE`` is deliberately absent: stale-but-usable evidence is still
#: evidence, and every expert decides for itself whether a stale input suffices.
_BLOCKING_QUALITY: Final[Mapping[QualityState, EvidenceReason]] = MappingProxyType(
    {
        QualityState.MISSING: EvidenceReason.INPUT_QUALITY,
        QualityState.UNKNOWN: EvidenceReason.INPUT_QUALITY,
        QualityState.CONTRADICTORY: EvidenceReason.INPUT_QUALITY,
        QualityState.AMENDED_FUTURE: EvidenceReason.INPUT_QUALITY,
    }
)

#: The decision horizon of a frame is the ``Timeframe`` whose nominal duration
#: equals its bars. A frame that belongs to no context horizon skips that gate.
_HORIZON_BY_DURATION: Final[Mapping[int, Timeframe]] = MappingProxyType(
    {timeframe.duration_ns: timeframe for timeframe in Timeframe}
)

_CONFIDENCE_BY_STANCE: Final[Mapping[StanceKind, Decimal]] = MappingProxyType(
    {
        StanceKind.SUPPORT: _ASSERTED_CONFIDENCE,
        StanceKind.CONTRADICT: _ASSERTED_CONFIDENCE,
        StanceKind.ABSTAIN: _ABSTAIN_CONFIDENCE,
    }
)

_REGISTRY_ORDER: Final[Mapping[str, int]] = MappingProxyType(
    {expert_id: index for index, expert_id in enumerate(CANONICAL_28_EXPERTS)}
)

_HEX64: Final = re.compile(r"^[0-9a-f]{64}$")


def dependency_group_key(requires: tuple[str, ...]) -> str:
    """Stable, deduplicated id for one expert's declared input requirements.

    ``ExpertSpec.requires`` is the registry's declared feature groups; sorting
    gives every expert with the same requirement *set* the same group id, so a
    bundle reports dependency groups, never 28 spellings of the same one.
    """
    return "requires:" + "|".join(sorted(requires))


def _duration_timeframe(frame: CausalFrame) -> Timeframe | None:
    """The context horizon this frame's bars belong to, if any."""
    if not frame.candles:
        return None
    last = frame.candles[-1]
    return _HORIZON_BY_DURATION.get(last.end_ns - last.start_ns)


def _horizon_shape(context: HorizonContext) -> list[list[object]]:
    """Deterministic (timeframe, as_of, complete, quality) rows for the digest."""
    return [
        [
            timeframe.value,
            context.alignment(timeframe).as_of_ns,
            context.alignment(timeframe).complete,
            context.alignment(timeframe).quality.value,
        ]
        for timeframe in Timeframe
    ]


def _context_ref(
    view: ObservationView,
    context: HorizonContext,
    belief_version_hash: str,
) -> str:
    """Bind one evidence set to the exact context that produced it."""
    return canonical_digest(
        "expert-evidence-context",
        [
            belief_version_hash or "NO_BELIEF",
            _horizon_shape(context),
            list(view.observation_ids()),
        ],
    )


@dataclass(frozen=True, slots=True)
class _Row:
    """One expert's outcome before it becomes a validated evidence record."""

    spec: ExpertSpec
    stance: StanceKind
    reason: str
    warmup_ok: bool


class ExpertEvidenceV1(BaseModel):
    """One witness's context-bound stance. Advisory: carries no authority."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    #: Canonical registry id (never an alias, never a duplicate within a bundle).
    expert_id: str
    #: The registry's declared witness version for this expert.
    expert_version: str
    stance: StanceKind
    #: Determinacy of the assertion: 1 when the expert asserted, 0 on abstention.
    confidence: Decimal
    #: The expert's own reason code, or a typed :class:`EvidenceReason` on an
    #: explicit pre-dispatch abstention.
    reason: str
    #: Deduplicated id of the requirement group this expert reads (REQUIRES).
    input_dependency_group: str
    #: Whether the frame carried the history the registry declares.
    warmup_ok: bool
    #: Belief version hash + horizon as-of times + usable observation ids.
    context_ref: str
    #: Deterministic digest of this row's content.
    evidence_id: str
    version: str = EVIDENCE_VERSION
    #: This record is a view, never an order or a permission.
    advisory_only: Literal[True] = True
    order_authority: Literal[False] = False

    @model_validator(mode="after")
    def _check_contract(self) -> Self:
        if self.expert_id not in _REGISTRY_ORDER:
            raise ValueError(f"expert {self.expert_id!r} is not one of the 28 canonical witnesses")
        if not self.expert_version.strip():
            raise ValueError("expert_version cannot be empty")
        if not (Decimal(0) <= self.confidence <= Decimal(1)):
            raise ValueError("confidence must be within [0, 1]")
        expected = _CONFIDENCE_BY_STANCE[self.stance]
        if self.confidence != expected:
            raise ValueError(
                f"confidence {self.confidence} contradicts stance {self.stance.value}; "
                f"expected {expected}"
            )
        if not self.reason.strip():
            raise ValueError("reason cannot be empty")
        declared = dependency_group_key(get_expert(self.expert_id).requires)
        if self.input_dependency_group != declared:
            raise ValueError(
                f"dependency group {self.input_dependency_group!r} does not match the registry's "
                f"declared requirements {declared!r}"
            )
        if _HEX64.fullmatch(self.context_ref) is None:
            raise ValueError("context_ref must be a 64-character lowercase hex digest")
        if _HEX64.fullmatch(self.evidence_id) is None:
            raise ValueError("evidence_id must be a 64-character lowercase hex digest")
        if self.version != EVIDENCE_VERSION:
            raise ValueError(
                f"unknown evidence version {self.version!r}; expected {EVIDENCE_VERSION!r}"
            )
        if self.advisory_only is not True:
            raise ValueError("expert evidence is advisory only")
        if self.order_authority is not False:
            raise ValueError("expert evidence carries no order authority")
        return self


class EvidenceBundle(BaseModel):
    """The 28-wide evidence set for one instrument, decision time and context."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    instrument: str
    decision_time_ns: int
    #: Content hash of the belief this evidence was bound to; ``""`` when the
    #: caller supplied no belief (the context layer keeps working, and the
    #: absence is explicit here rather than filled with a permissive default).
    belief_version_hash: str
    context_ref: str
    evidence: tuple[ExpertEvidenceV1, ...]
    abstain_count: int
    #: Canonical expert ids that were supplied more than once and collapsed.
    duplicate_witness_ids: tuple[str, ...]
    #: Number of distinct canonical experts in ``evidence`` (28 when all requested).
    coverage: int
    #: Deduplicated, sorted requirement groups the evidence rows read.
    input_dependency_groups: tuple[str, ...]
    provenance: str
    #: Whether the V9 context gates were applied (False = registry passthrough).
    context_layer_enabled: bool
    version: str = EVIDENCE_VERSION
    advisory_only: Literal[True] = True
    order_authority: Literal[False] = False

    @model_validator(mode="after")
    def _check_contract(self) -> Self:
        if not self.instrument.strip():
            raise ValueError("instrument cannot be empty")
        if self.decision_time_ns < 0:
            raise ValueError("decision_time_ns must be non-negative")
        if self.belief_version_hash and _HEX64.fullmatch(self.belief_version_hash) is None:
            raise ValueError("belief_version_hash must be empty or a 64-character hex digest")
        if _HEX64.fullmatch(self.context_ref) is None:
            raise ValueError("context_ref must be a 64-character lowercase hex digest")
        if _HEX64.fullmatch(self.provenance) is None:
            raise ValueError("provenance must be a 64-character lowercase hex digest")
        if not self.evidence:
            raise ValueError("an evidence bundle cannot be empty")
        ids = tuple(row.expert_id for row in self.evidence)
        if len(ids) != len(set(ids)):
            raise ValueError("evidence bundles cannot carry a duplicate expert row")
        order = tuple(_REGISTRY_ORDER[expert_id] for expert_id in ids)
        if list(order) != sorted(order):
            raise ValueError("evidence rows must follow canonical registry order")
        if self.coverage != len(self.evidence):
            raise ValueError(
                f"coverage {self.coverage} does not match {len(self.evidence)} evidence rows"
            )
        abstains = sum(1 for row in self.evidence if row.stance is StanceKind.ABSTAIN)
        if self.abstain_count != abstains:
            raise ValueError(f"abstain_count {self.abstain_count} does not match {abstains} rows")
        if any(row.context_ref != self.context_ref for row in self.evidence):
            raise ValueError("every evidence row must be bound to the bundle's context_ref")
        if list(self.duplicate_witness_ids) != sorted(set(self.duplicate_witness_ids)):
            raise ValueError("duplicate_witness_ids must be sorted and unique")
        groups = tuple(sorted({row.input_dependency_group for row in self.evidence}))
        if groups != self.input_dependency_groups:
            raise ValueError("input_dependency_groups must be the deduplicated row groups")
        if self.version != EVIDENCE_VERSION:
            raise ValueError(
                f"unknown evidence version {self.version!r}; expected {EVIDENCE_VERSION!r}"
            )
        if self.advisory_only is not True:
            raise ValueError("an evidence bundle is advisory only")
        if self.order_authority is not False:
            raise ValueError("an evidence bundle carries no order authority")
        return self


def _selected_specs(
    observers: Sequence[str] | None,
) -> tuple[tuple[ExpertSpec, ...], tuple[str, ...]]:
    """Resolve observer ids/aliases to the canonical witnesses, deduplicated.

    Unknown ids raise ``KeyError`` (the registry's fail-closed lookup). The
    surviving witnesses are returned in canonical registry order, so the bundle
    is independent of the caller's ordering, aliasing and repetition.
    """
    if observers is None:
        requested = tuple(get_expert(expert_id) for expert_id in CANONICAL_28_EXPERTS)
    else:
        requested = tuple(get_expert(observer) for observer in observers)
    seen: dict[str, ExpertSpec] = {}
    duplicates: set[str] = set()
    for candidate in requested:
        if candidate.expert_id in seen:
            duplicates.add(candidate.expert_id)
            continue
        seen[candidate.expert_id] = candidate
    ordered: list[ExpertSpec] = []
    for expert_id in CANONICAL_28_EXPERTS:
        selected = seen.get(expert_id)
        if selected is not None:
            ordered.append(selected)
    if not ordered:
        raise ValueError("no observers selected")
    return tuple(ordered), tuple(sorted(duplicates))


def _frame_content_digest(frame: CausalFrame, view: ObservationView) -> str:
    """Content identity of the frame an evidence bundle was produced over.

    Deliberately independent of the cache's append-only chain: the same frame
    must yield the same provenance whether the cache was freshly seeded or
    arrived at this bar by extension. Bar count plus the last bar's identity
    and close pin the series; observation ids pin the usable inputs (they are
    content identities, so a revised payload changes them).
    """
    candles = frame.candles
    last = candles[-1] if candles else None
    usable = tuple(
        sorted(
            {
                observation_id(observation)
                for observation in view.observations(frame.instrument_id)
            }
        )
    )
    return canonical_digest(
        "expert-evidence-frame",
        {
            "instrument_id": frame.instrument_id,
            "decision_ns": frame.decision_ns,
            "bar_count": len(candles),
            "last_bar": (
                None
                if last is None
                else {
                    "start_ns": last.start_ns,
                    "end_ns": last.end_ns,
                    "close": str(last.close),
                    "source_hash": last.source_hash,
                }
            ),
            "observation_ids": list(usable),
        },
    )


def _require_aligned(context: HorizonContext, frame: CausalFrame) -> None:
    """A context that names another instrument or decision time is a contract error."""
    if context.instrument_id != frame.instrument_id:
        raise ValueError(
            f"context instrument {context.instrument_id!r} does not match frame "
            f"instrument {frame.instrument_id!r}"
        )
    if context.decision_time_ns != frame.decision_ns:
        raise ValueError(
            f"context decision time {context.decision_time_ns} does not match frame "
            f"decision time {frame.decision_ns}"
        )


def _blocking_quality(
    view: ObservationView,
    frame: CausalFrame,
    belief: MarketBeliefV1 | None,
) -> EvidenceReason | None:
    """Fail-closed quality gate over the view (and belief, when supplied)."""
    quality = view.quality(frame.instrument_id)
    if belief is not None:
        quality = worst_quality(quality, belief.quality)
    return _BLOCKING_QUALITY.get(quality)


def _horizon_reason(context: HorizonContext, frame: CausalFrame) -> EvidenceReason | None:
    """The frame's own horizon must be present, complete and not quality-blocked."""
    timeframe = _duration_timeframe(frame)
    if timeframe is None:
        return None
    alignment = context.alignment(timeframe)
    if not alignment.complete:
        return EvidenceReason.CONTEXT_INCOMPLETE
    if alignment.quality in _BLOCKING_QUALITY:
        return EvidenceReason.CONTEXT_INCOMPLETE
    return None


def _gate_reason(
    spec: ExpertSpec,
    *,
    unavailable: frozenset[str],
    warmup_ok: bool,
    quality_reason: EvidenceReason | None,
    horizon_reason: EvidenceReason | None,
    readings_missing: bool,
) -> EvidenceReason | None:
    """First declared shortfall wins; the order is part of the documented contract."""
    if spec.dependency_group in unavailable or not unavailable.isdisjoint(spec.requires):
        return EvidenceReason.DEPENDENCY_UNAVAILABLE
    if not warmup_ok:
        return EvidenceReason.WARMUP_INPUT
    if quality_reason is not None:
        return quality_reason
    if horizon_reason is not None:
        return horizon_reason
    if spec.takes_readings and readings_missing:
        return EvidenceReason.MISSING_INPUT
    return None


def _evidence_row(
    spec: ExpertSpec,
    stance: StanceKind,
    reason: str,
    *,
    warmup_ok: bool,
    instrument: str,
    decision_ns: int,
    context_ref: str,
) -> ExpertEvidenceV1:
    """Build one validated row; the id is a digest of exactly its content."""
    group = dependency_group_key(spec.requires)
    confidence = _CONFIDENCE_BY_STANCE[stance]
    evidence_id = canonical_digest(
        "expert-evidence",
        [
            EVIDENCE_VERSION,
            instrument,
            decision_ns,
            spec.expert_id,
            spec.version,
            stance.value,
            str(confidence),
            reason,
            group,
            warmup_ok,
            context_ref,
        ],
    )
    return ExpertEvidenceV1(
        expert_id=spec.expert_id,
        expert_version=spec.version,
        stance=stance,
        confidence=confidence,
        reason=reason,
        input_dependency_group=group,
        warmup_ok=warmup_ok,
        context_ref=context_ref,
        evidence_id=evidence_id,
    )


def _baseline_rows(
    specs: tuple[ExpertSpec, ...],
    frame: CausalFrame,
    cache: FeatureCache,
    opportunity: Opportunity | None,
    readings: tuple[PositioningReading, ...],
) -> list[_Row]:
    """V9 context layer disabled: the registry's own output, verbatim.

    This is the comparison path. It calls the same 28-wide registry dispatch the
    strategy uses, keeps every stance kind and reason code untouched, and only
    annotates the rows with the registry's declared warmup and context binding.
    """
    stances = cache.observe_all_28(frame, opportunity, readings=readings)
    by_id = {stance.observer_id: stance for stance in stances}
    candles = len(frame.candles)
    rows: list[_Row] = []
    for spec in specs:
        stance = by_id[spec.expert_id]
        rows.append(_Row(spec, stance.kind, stance.reason, candles >= spec.warmup))
    return rows


def _context_rows(
    specs: tuple[ExpertSpec, ...],
    frame: CausalFrame,
    cache: FeatureCache,
    opportunity: Opportunity | None,
    readings: tuple[PositioningReading, ...],
    *,
    unavailable: frozenset[str],
    quality_reason: EvidenceReason | None,
    horizon_reason: EvidenceReason | None,
) -> list[_Row]:
    """V9 context layer enabled: gate first, dispatch only what has its inputs.

    Every dispatch is the registry's ``observe_expert`` (its own observer
    functions with its own witness metadata) inside the cache's retained
    feature state. A gated expert is never called, so an unavailable dependency
    group abstains instead of crashing or fabricating.
    """
    rows: list[_Row] = []
    candles = len(frame.candles)
    with cache.scoped(frame):
        for spec in specs:
            warmup_ok = candles >= spec.warmup
            reason = _gate_reason(
                spec,
                unavailable=unavailable,
                warmup_ok=warmup_ok,
                quality_reason=quality_reason,
                horizon_reason=horizon_reason,
                readings_missing=not readings,
            )
            if reason is not None:
                rows.append(_Row(spec, StanceKind.ABSTAIN, reason.value, warmup_ok))
                continue
            stance = observe_expert(spec.expert_id, frame, opportunity, readings=readings)
            rows.append(_Row(spec, stance.kind, stance.reason, warmup_ok))
    return rows


def produce_evidence(
    *,
    view: ObservationView,
    context: HorizonContext,
    frame: CausalFrame,
    cache: FeatureCache,
    belief: MarketBeliefV1 | None = None,
    opportunity: Opportunity | None = None,
    readings: tuple[PositioningReading, ...] = (),
    observers: Sequence[str] | None = None,
    unavailable_dependency_groups: Iterable[str] = (),
    context_layer: bool = True,
) -> EvidenceBundle:
    """Produce one context-bound evidence set over the canonical 28 witnesses.

    ``view`` is the point-in-time input snapshot, ``context`` the assembled
    15m/1h/4h context for ``frame.decision_ns`` and ``cache`` the feed's shared
    feature state. ``belief`` is optional and, when given, is verified (a stale
    or tampered ``version_hash`` raises) and bound into ``context_ref`` and the
    bundle.

    ``opportunity`` is a pass-through identity created upstream; this function
    never creates one and adds no capital, size or exposure quantity.

    ``observers`` restricts/duplicates the requested witness ids; aliases and
    repeats resolve to the same canonical expert and are reported in
    ``duplicate_witness_ids``. ``unavailable_dependency_groups`` may name either
    a registry dependency group (``dep_derivatives``) or a declared requirement
    group (``positioning``); dependents abstain with
    ``DEPENDENCY_UNAVAILABLE``.

    ``context_layer=False`` is the parity baseline: no gates are applied and
    every stance kind and reason code is the plain registry output on the same
    frame, opportunity and readings.

    Contract violations fail closed with ``ValueError``: a view cut after the
    decision, a context naming another instrument or decision time, a belief
    naming another instrument or decision time, an unregistered observer id.
    """
    _require_aligned(context, frame)
    if view.cut_time_ns > frame.decision_ns:
        raise ValueError(
            f"view cut {view.cut_time_ns} is after the decision time {frame.decision_ns}: "
            "future input cannot produce evidence"
        )

    belief_hash = ""
    if belief is not None:
        if belief.instrument_id != frame.instrument_id:
            raise ValueError(
                f"belief instrument {belief.instrument_id!r} does not match frame "
                f"instrument {frame.instrument_id!r}"
            )
        if belief.decision_time_ns != frame.decision_ns:
            raise ValueError(
                f"belief decision time {belief.decision_time_ns} does not match frame "
                f"decision time {frame.decision_ns}"
            )
        verify_version_hash(belief)
        belief_hash = belief.version_hash

    specs, duplicates = _selected_specs(observers)
    unavailable = frozenset(unavailable_dependency_groups)
    context_ref = _context_ref(view, context, belief_hash)

    if context_layer:
        rows = _context_rows(
            specs,
            frame,
            cache,
            opportunity,
            readings,
            unavailable=unavailable,
            quality_reason=_blocking_quality(view, frame, belief),
            horizon_reason=_horizon_reason(context, frame),
        )
    else:
        rows = _baseline_rows(specs, frame, cache, opportunity, readings)

    evidence = tuple(
        _evidence_row(
            row.spec,
            row.stance,
            row.reason,
            warmup_ok=row.warmup_ok,
            instrument=frame.instrument_id,
            decision_ns=frame.decision_ns,
            context_ref=context_ref,
        )
        for row in rows
    )
    provenance = canonical_digest(
        "expert-evidence-provenance",
        {
            "version": EVIDENCE_VERSION,
            "registry": [spec.expert_id for spec in specs],
            "expert_versions": [[spec.expert_id, spec.version, spec.dependency_group] for spec in specs],
            "cache_version": cache.version,
            "frame_digest": _frame_content_digest(frame, view),
            "context_ref": context_ref,
            "belief_version_hash": belief_hash,
            "context_layer": context_layer,
            "unavailable_dependency_groups": sorted(unavailable),
            "readings": len(readings),
        },
    )
    return EvidenceBundle(
        instrument=frame.instrument_id,
        decision_time_ns=frame.decision_ns,
        belief_version_hash=belief_hash,
        context_ref=context_ref,
        evidence=evidence,
        abstain_count=sum(1 for row in evidence if row.stance is StanceKind.ABSTAIN),
        duplicate_witness_ids=duplicates,
        coverage=len(evidence),
        input_dependency_groups=tuple(sorted({row.input_dependency_group for row in evidence})),
        provenance=provenance,
        context_layer_enabled=context_layer,
    )
