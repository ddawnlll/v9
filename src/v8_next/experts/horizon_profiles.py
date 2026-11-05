"""Experimental expert input contracts and PIT multi-horizon dispatch.

Profiles are research defaults, not calibrated routing or a claim of edge.
Confirmation alignments are exposed as context/provenance; legacy observers
currently consume only their primary frame. No orders or beliefs are created.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass
from types import MappingProxyType

from v8_next.domain.horizons import HorizonContext, Timeframe, assemble_context
from v8_next.domain.market import CausalFrame
from v8_next.domain.observation import ObservationView, QualityState, observation_id
from v8_next.domain.positioning import Metric, PositioningReading, positioning_at
from v8_next.economics.decisions import Opportunity, Stance, StanceKind
from v8_next.experts.cache import FeatureCache
from v8_next.experts.registry import CANONICAL_28_EXPERTS, get_expert, observe_expert
from v8_next.telemetry.identity import canonical_digest

PROFILE_VERSION = "experimental-horizons-v2"

# Local price-action and liquidity patterns use the opportunity horizon. Longer
# trend, regime, and positioning witnesses use the intermediate horizon. These
# assignments are hypotheses based on observer mechanisms, not optimized values.
_M15 = frozenset({
    "breakout_retest", "candlestick_reversal", "failed_breakout", "failed_breakout_2b",
    "floor_trader_pivot", "gap_exhaustion", "liquidity_sweep_reclaim",
    "market_profile_value_area", "range_breakout_1to1", "volume_climax_reversal",
    "volume_confirmed_breakout",
})


@dataclass(frozen=True, slots=True)
class ExpertInputProfile:
    expert_id: str
    version: str
    required_data: tuple[str, ...]
    primary: Timeframe
    confirmation: tuple[Timeframe, ...]
    warmup_bars: int
    habitat: str
    triggers: tuple[str, ...]
    max_staleness_ns: int
    required_auxiliary: tuple[Metric, ...]
    optional_data: tuple[Metric, ...]
    missing_behavior: str
    rationale: str


def _profile(expert_id: str) -> ExpertInputProfile:
    spec = get_expert(expert_id)
    primary = Timeframe.M15 if expert_id in _M15 else Timeframe.H1
    auxiliary: tuple[Metric, ...] = ("settled_funding_rate",) if expert_id == "funding_crowding_reversal" else (
        ("open_interest", "long_short_ratio") if expert_id == "open_interest_divergence" else ()
    )
    return ExpertInputProfile(
        expert_id=expert_id,
        version=PROFILE_VERSION,
        required_data=("ohlcv", *spec.requires),
        primary=primary,
        confirmation=(Timeframe.H1,) if primary is Timeframe.M15 else (Timeframe.H4,),
        warmup_bars=spec.warmup,
        habitat=spec.behavior_family,
        triggers=("primary_completed_bar", "confirmation_completed_bar", "opportunity_change", "positioning_change", "quality_change"),
        max_staleness_ns=primary.duration_ns,
        required_auxiliary=auxiliary,
        optional_data=(),
        missing_behavior="ABSTAIN on missing, stale, incomplete or insufficient primary data; auxiliary-dependent witnesses abstain without a valid reading",
        rationale=("local structure and event patterns" if primary is Timeframe.M15 else "intermediate trend, volatility, structure or positioning"),
    )


EXPERT_INPUT_PROFILES: Mapping[str, ExpertInputProfile] = MappingProxyType(
    {expert_id: _profile(expert_id) for expert_id in CANONICAL_28_EXPERTS}
)


@dataclass(frozen=True, slots=True)
class ExpertContext:
    profile: ExpertInputProfile
    alignment: HorizonContext
    primary_frame: CausalFrame | None
    available_confirmations: tuple[Timeframe, ...]
    input_provenance: str


@dataclass(frozen=True, slots=True)
class HorizonWitness:
    context: ExpertContext
    stance: Stance


def _abstain(expert_id: str, decision_ns: int, opportunity: Opportunity | None, reason: str) -> Stance:
    spec = get_expert(expert_id)
    return Stance(expert_id, spec.dependency_group, StanceKind.ABSTAIN, reason,
                  opportunity.opportunity_id if opportunity else None, decision_ns,
                  spec.behavior_family, spec.mechanism_family, spec.version,
                  spec.default_variant or "UNRESOLVED")


def observe_multi_horizon(
    *, views: Mapping[Timeframe, ObservationView], frames: Mapping[Timeframe, CausalFrame],
    context: HorizonContext, caches: Mapping[Timeframe, FeatureCache],
    opportunity: Opportunity | None = None, readings: tuple[PositioningReading, ...] = (),
) -> tuple[HorizonWitness, ...]:
    """Dispatch all 28 once on verified primary feeds, abstaining on shortfalls.

    Caller supplies one cache per horizon. Confirmations are optional and do not
    alter legacy observer logic. The context is independently reassembled from
    source views; frame bars must match those views exactly through the selected
    completed bar, including source/availability identity.
    """
    instrument, cut = context.instrument_id, context.decision_time_ns
    if opportunity is not None and (
        opportunity.instrument_id != instrument or opportunity.anchor_ns > cut
        or opportunity.expires_ns <= cut or opportunity.direction not in {"LONG", "SHORT"}
    ):
        raise ValueError("opportunity outside observer domain")
    if len({id(cache) for cache in caches.values()}) != len(caches):
        raise ValueError("each horizon needs a distinct feature cache")
    if any(view.cut_time_ns > cut for view in views.values()):
        raise ValueError("future view")
    if assemble_context(views, instrument_id=instrument, decision_time_ns=cut) != context:
        raise ValueError("context does not match source views")
    valid: dict[Timeframe, CausalFrame] = {}
    for horizon, supplied_frame in frames.items():
        if not isinstance(horizon, Timeframe) or horizon not in views:
            raise ValueError("frame has no matching view")
        if supplied_frame.instrument_id != instrument or supplied_frame.decision_ns != cut:
            raise ValueError("frame identity or cut mismatch")
        alignment = context.alignment(horizon)
        expected = tuple(bar for bar in views[horizon].bars(instrument)
                         if bar.end_ns <= alignment.as_of_ns and bar.available_ns is not None
                         and bar.available_ns <= cut)
        if supplied_frame.candles != expected:
            raise ValueError("frame is not the selected view prefix")
        if any(bar.end_ns - bar.start_ns != horizon.duration_ns or
               bar.received_ns > cut or bar.available_ns is None or
               bar.available_ns < bar.received_ns for bar in supplied_frame.candles):
            raise ValueError("invalid horizon or availability clocks")
        if supplied_frame.candles and supplied_frame.candles[-1].end_ns != alignment.as_of_ns:
            raise ValueError("frame does not end at selected alignment")
        # Reconstruct from validated candles; caller-owned prefix tables and
        # continuity/order hints are not evidence and must not affect dispatch.
        valid[horizon] = CausalFrame(instrument, cut, supplied_frame.candles)
    known_readings = tuple(r for r in readings if r.instrument_id == instrument
                           and r.available_ns is not None and r.available_ns <= cut
                           and r.event_ns <= cut)
    # Hash each feed once per cut, rather than rescanning full histories for
    # every expert. The digest includes both the numeric frame and observation
    # identities so revisions to content or publication lineage are visible.
    feed_digests = {
        horizon: canonical_digest("expert-horizon-feed-v1", {
            "cut": view.cut_time_ns,
            "observations": sorted(observation_id(o) for o in view.observations(instrument)),
            "bars": [[c.start_ns, c.end_ns, c.received_ns, c.available_ns, c.source_hash,
                      str(c.open), str(c.high), str(c.low), str(c.close), str(c.volume)]
                     for c in valid[horizon].candles] if horizon in valid else None,
        })
        for horizon, view in views.items()
    }
    results: list[HorizonWitness] = []
    for expert_id in CANONICAL_28_EXPERTS:
        profile = EXPERT_INPUT_PROFILES[expert_id]
        alignment = context.alignment(profile.primary)
        frame = valid.get(profile.primary)
        confirmations = tuple(h for h in profile.confirmation if context.alignment(h).complete
                              and context.alignment(h).quality is QualityState.OK
                              and h in valid)
        provenance = canonical_digest("expert-horizon-input-v2", {
            "profile": asdict(profile), "expert": expert_id, "cut": cut,
            "expert_version": get_expert(expert_id).version,
            "variant": get_expert(expert_id).default_variant,
            "cache_version": caches[profile.primary].version if profile.primary in caches else None,
            "opportunity": asdict(opportunity) if opportunity is not None else None,
            "feed_digests": {h.value: feed_digests[h]
                             for h in (profile.primary, *profile.confirmation) if h in feed_digests},
            "primary": profile.primary.value,
            "alignments": context.model_dump(mode="json"),
            "readings": [[r.metric, str(r.value), r.event_ns, r.received_ns, r.available_ns, r.valid_until_ns, r.source_hash] for r in known_readings],
        })
        expert_context = ExpertContext(profile, context, frame, confirmations, provenance)
        reason = None
        if frame is None or not frame.candles or not alignment.complete or alignment.quality is not QualityState.OK:
            reason = "HORIZON_UNAVAILABLE"
        elif cut - alignment.as_of_ns >= profile.max_staleness_ns:
            reason = "HORIZON_STALE"
        elif len(frame.candles) < profile.warmup_bars:
            reason = "WARMUP_INPUT"
        elif profile.required_auxiliary and not all(positioning_at(known_readings, instrument, metric, cut) is not None for metric in profile.required_auxiliary):
            reason = "MISSING_INPUT"
        elif profile.primary not in caches:
            raise ValueError("missing feature cache for available horizon")
        if reason is not None:
            stance = _abstain(expert_id, cut, opportunity, reason)
        else:
            assert frame is not None
            with caches[profile.primary].scoped(frame):
                stance = observe_expert(expert_id, frame, opportunity, readings=known_readings)
        results.append(HorizonWitness(expert_context, stance))
    return tuple(results)
