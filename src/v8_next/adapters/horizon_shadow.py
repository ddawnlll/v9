"""PIT multi-horizon advisory pipeline for explicitly selected simulation shadow mode.

No native order authority is present here. H1/H4 are aggregated solely from
completed M15 inputs. This experimental adapter has no persistent checkpoint.
"""
from __future__ import annotations

from v8_next.belief import advance
from v8_next.belief.state import MarketBeliefV1
from v8_next.domain.horizon_aggregation import H1_NS, H4_NS, M15_NS, HorizonAggregator
from v8_next.domain.horizons import Timeframe, assemble_context
from v8_next.domain.market import Candle, CausalFrame
from v8_next.domain.observation import Observation, ObservationKind, QualityState, build_view
from v8_next.domain.positioning import PositioningReading
from v8_next.economics.decisions import Opportunity, StanceKind
from v8_next.experts.cache import FeatureCache
from v8_next.experts.evidence import EvidenceBundle, ExpertEvidenceV1, _evidence_row
from v8_next.experts.horizon_profiles import HorizonWitness, observe_multi_horizon
from v8_next.experts.registry import CANONICAL_28_EXPERTS, get_expert
from v8_next.telemetry.identity import canonical_digest


class MultiHorizonShadowPipeline:
    """One belief and distinct per-horizon feature caches for one instrument."""

    def __init__(self, instrument_id: str) -> None:
        self.instrument_id = instrument_id
        self._aggregator = HorizonAggregator(instrument_id, source_duration_ns=M15_NS,
                                             target_durations_ns=(H1_NS, H4_NS))
        self._observations: dict[Timeframe, list[Observation]] = {h: [] for h in Timeframe}
        self._caches = {h: FeatureCache() for h in Timeframe}
        self.belief: MarketBeliefV1 | None = None
        self.last_witnesses: tuple[HorizonWitness, ...] = ()

    def advance(self, candle: Candle, *, cut: int, opportunity: Opportunity | None,
                quality: QualityState, readings: tuple[PositioningReading, ...] = ()) -> tuple[MarketBeliefV1, EvidenceBundle]:
        """Consume an actually available M15 candle; fail closed on input gaps."""
        events = self._aggregator.advance(candle, processing_ns=cut, quality=quality)
        items = [(Timeframe.M15, candle)]
        by_duration = {H1_NS: Timeframe.H1, H4_NS: Timeframe.H4}
        items.extend((by_duration[event.duration_ns], event.candle) for event in events)
        for horizon, item in items:
            observations = self._observations[horizon]
            assert item.available_ns is not None
            observations.append(Observation(
                instrument_id=item.instrument_id, kind=ObservationKind.BAR,
                event_time=item.start_ns, available_at=item.available_ns,
                received_at=item.received_ns, source=f"shadow-{horizon.value}",
                sequence=len(observations), quality=QualityState.OK,
                payload={"start_ns": item.start_ns, "end_ns": item.end_ns,
                         "open": str(item.open), "high": str(item.high), "low": str(item.low),
                         "close": str(item.close), "volume": str(item.volume),
                         "source_hash": item.source_hash},
            ))
        views = {h: build_view(rows, cut_time_ns=cut) for h, rows in self._observations.items() if rows}
        context = assemble_context(views, instrument_id=self.instrument_id, decision_time_ns=cut)
        frames = {h: CausalFrame(self.instrument_id, cut, view.bars(self.instrument_id))
                  for h, view in views.items()}
        belief = advance(self.belief, views[Timeframe.M15], context, now_ns=cut)
        self.belief = belief
        witnesses = observe_multi_horizon(views=views, frames=frames, context=context,
                                          caches=self._caches, opportunity=opportunity,
                                          readings=readings)
        _verify_witness_bindings(witnesses, opportunity, cut)
        self.last_witnesses = witnesses
        context_ref = canonical_digest("shadow-multihorizon-evidence-v1", {
            "belief": belief.version_hash, "inputs": [w.context.input_provenance for w in witnesses],
        })
        rows: list[ExpertEvidenceV1] = []
        for witness in witnesses:
            spec = get_expert(witness.stance.observer_id)
            warmup_ok = (witness.context.primary_frame is not None and
                         len(witness.context.primary_frame.candles) >= spec.warmup)
            stance = witness.stance.kind if belief.is_actionable_evidence() else StanceKind.ABSTAIN
            reason = witness.stance.reason if belief.is_actionable_evidence() else "BELIEF_NOT_ACTIONABLE"
            rows.append(_evidence_row(spec, stance, reason, warmup_ok=warmup_ok,
                                      instrument=self.instrument_id, decision_ns=cut,
                                      context_ref=context_ref))
        evidence = tuple(rows)
        bundle = EvidenceBundle(
            instrument=self.instrument_id, decision_time_ns=cut,
            belief_version_hash=belief.version_hash, context_ref=context_ref,
            evidence=evidence, abstain_count=sum(r.stance is StanceKind.ABSTAIN for r in evidence),
            duplicate_witness_ids=(), coverage=len(evidence),
            input_dependency_groups=tuple(sorted({r.input_dependency_group for r in evidence})),
            provenance=context_ref, context_layer_enabled=True,
        )
        return belief, bundle


def _verify_witness_bindings(
    witnesses: tuple[HorizonWitness, ...], opportunity: Opportunity | None, cut: int
) -> None:
    """Reject alias, time and opportunity mismatches before building a bundle."""
    ids = tuple(w.stance.observer_id for w in witnesses)
    if ids != CANONICAL_28_EXPERTS:
        raise ValueError("shadow expert identity or order mismatch")
    expected_id = None if opportunity is None else opportunity.opportunity_id
    for witness in witnesses:
        stance = witness.stance
        if witness.context.profile.expert_id != stance.observer_id or stance.decision_ns != cut:
            raise ValueError("shadow expert context or time mismatch")
        if stance.opportunity_id not in (None, expected_id):
            raise ValueError("shadow witness opportunity mismatch")
        if stance.kind is not StanceKind.ABSTAIN and (
            expected_id is None or stance.opportunity_id != expected_id
        ):
            raise ValueError("shadow actionable witness has no bound opportunity")
