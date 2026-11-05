"""Deterministic mechanics fixtures; these are not market or profitability evidence."""
from decimal import Decimal

import pytest

from v8_next.domain.horizons import Timeframe, assemble_context
from v8_next.domain.market import CausalFrame
from v8_next.domain.observation import Observation, ObservationKind, QualityState, build_view
from v8_next.domain.positioning import PositioningReading
from v8_next.experts.cache import FeatureCache
from v8_next.experts.horizon_profiles import EXPERT_INPUT_PROFILES, observe_multi_horizon
from v8_next.experts.registry import CANONICAL_28_EXPERTS

IID = "BTCUSDT-PERP.BINANCE"


def feed(horizon: Timeframe, cut: int, count: int = 60):
    duration = horizon.duration_ns
    observations = []
    for i in range(count):
        start = cut - (count - i) * duration
        observations.append(Observation(
            instrument_id=IID, kind=ObservationKind.BAR, event_time=start,
            received_at=start + duration, available_at=start + duration,
            source="fixture", sequence=i, quality=QualityState.OK,
            payload={"start_ns": start, "end_ns": start + duration, "open": "100",
                     "high": "101", "low": "99", "close": "100", "volume": "10",
                     "source_hash": f"{horizon.value}-{i}"},
        ))
    return build_view(observations, cut_time_ns=cut)


def test_all_profiles_dispatch_on_distinct_horizons_with_stable_provenance():
    cut = 1000 * Timeframe.H4.duration_ns
    views = {h: feed(h, cut) for h in Timeframe}
    frames = {h: CausalFrame(IID, cut, v.bars(IID)) for h, v in views.items()}
    context = assemble_context(views, instrument_id=IID, decision_time_ns=cut)
    caches = {h: FeatureCache() for h in Timeframe}
    rows = observe_multi_horizon(views=views, frames=frames, context=context, caches=caches)
    assert tuple(row.stance.observer_id for row in rows) == CANONICAL_28_EXPERTS
    assert set(EXPERT_INPUT_PROFILES) == set(CANONICAL_28_EXPERTS)
    assert {row.context.profile.primary for row in rows} == {Timeframe.M15, Timeframe.H1}
    by_id = {row.stance.observer_id: row for row in rows}
    assert by_id["liquidity_sweep_reclaim"].context.primary_frame == frames[Timeframe.M15]
    assert by_id["trend_pullback"].context.primary_frame == frames[Timeframe.H1]
    assert by_id["trend_pullback"].context.available_confirmations == (Timeframe.H4,)
    assert [row.context.input_provenance for row in rows] == [
        row.context.input_provenance for row in observe_multi_horizon(
            views=views, frames=frames, context=context, caches=caches)]


def test_missing_horizons_and_future_auxiliary_abstain():
    cut = 1000 * Timeframe.H4.duration_ns
    view = feed(Timeframe.H1, cut)
    views = {Timeframe.H1: view}
    frame = CausalFrame(IID, cut, view.bars(IID))
    context = assemble_context(views, instrument_id=IID, decision_time_ns=cut)
    reading = PositioningReading(IID, "settled_funding_rate", Decimal("0.1"), cut,
                                  cut + 1, cut + 1, cut + 100, "future")
    rows = observe_multi_horizon(views=views, frames={Timeframe.H1: frame}, context=context,
                                 caches={Timeframe.H1: FeatureCache()}, readings=(reading,))
    by_id = {row.stance.observer_id: row for row in rows}
    assert by_id["liquidity_sweep_reclaim"].stance.reason == "HORIZON_UNAVAILABLE"
    assert by_id["funding_crowding_reversal"].stance.reason == "MISSING_INPUT"


def test_forged_frame_is_rejected():
    cut = 1000 * Timeframe.H4.duration_ns
    view = feed(Timeframe.H1, cut)
    context = assemble_context({Timeframe.H1: view}, instrument_id=IID, decision_time_ns=cut)
    with pytest.raises(ValueError, match="selected view prefix"):
        observe_multi_horizon(views={Timeframe.H1: view}, frames={Timeframe.H1: CausalFrame(IID, cut, ())},
                              context=context, caches={})


def test_delayed_htf_is_not_exposed_and_stale_primary_abstains():
    cut = 1000 * Timeframe.H4.duration_ns
    old_view = feed(Timeframe.H1, cut)
    delayed_view = feed(Timeframe.H4, cut + Timeframe.H4.duration_ns)
    later_cut = cut + Timeframe.H1.duration_ns
    # Rebuild the H1 view at the later cut without inserting any newer bar.
    stale_view = build_view(old_view.observations(IID), cut_time_ns=later_cut)
    # A future snapshot cannot be repurposed to smuggle its completed H4 bar.
    context = assemble_context({Timeframe.H1: stale_view}, instrument_id=IID,
                               decision_time_ns=later_cut)
    with pytest.raises(ValueError, match="future view"):
        observe_multi_horizon(views={Timeframe.H1: stale_view, Timeframe.H4: delayed_view},
                              frames={}, context=context, caches={})
    frame = CausalFrame(IID, later_cut, stale_view.bars(IID))
    rows = observe_multi_horizon(views={Timeframe.H1: stale_view},
                                 frames={Timeframe.H1: frame}, context=context,
                                 caches={Timeframe.H1: FeatureCache()})
    trend = next(row for row in rows if row.stance.observer_id == "trend_pullback")
    assert trend.stance.reason == "HORIZON_UNAVAILABLE"
    assert trend.context.available_confirmations == ()
