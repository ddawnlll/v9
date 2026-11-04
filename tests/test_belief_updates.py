"""Incremental belief updates, transitions and abstention (M1-04).

The fixtures pin the policy thresholds (``BELIEF_POLICY_VERSION`` =
``belief-policy/1``, regime floor 0.35, hysteresis band 0.10, candidate streak 2)
so a change to any of them fails here before it can change behaviour silently.
The borderline window below has a trend score of exactly 0.52 against a range
score of 0.48 — a 0.04 margin, i.e. inside the 0.10 band. That is the
false-transition fixture: it documents that a genuinely ambiguous window never
flips the regime, while the same sequence under a *separately versioned* narrow
band does flip, which proves the band is what holds the state.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal
from typing import Any

import pytest

from v8_next.belief import (
    BELIEF_POLICY_VERSION,
    DEFAULT_POLICY,
    BeliefContractError,
    BeliefPolicy,
    BeliefPolicyError,
    BeliefStatus,
    RegimeKind,
    StateOrigin,
    TransitionReason,
    UncertaintyState,
    UpdateTrigger,
    advance,
    policy_digest,
    update_trigger,
)
from v8_next.belief.state import BeliefIntegrityError
from v8_next.domain.horizons import Timeframe, assemble_context
from v8_next.domain.observation import (
    Observation,
    ObservationKind,
    ObservationView,
    QualityState,
    build_view,
)

NS = 1_000_000_000
MIN = 60 * NS
M15 = 15 * MIN
H1 = 60 * MIN
IID = "BTCUSDT-PERP.BINANCE"
BASE = 10 * H1
HORIZON = DEFAULT_POLICY.staleness_horizon_ns

#: 19 deltas, net -1 over a path of 19 -> efficiency 0.05 -> RANGE (0.95).
#: The last five deltas share a sign, so the transition score stays 0.
RANGE_PATH: tuple[Decimal, ...] = (
    *(Decimal("1") for _ in range(9)),
    *(Decimal("-1") for _ in range(10)),
)
#: 19 deltas -> efficiency exactly 0.52 -> TREND 0.52 vs RANGE 0.48 (margin 0.04).
BORDERLINE: tuple[Decimal, ...] = (
    *(Decimal("1.5") for _ in range(12)),
    Decimal("1.0"),
    *(Decimal("-1.0") for _ in range(6)),
)
#: 19 deltas -> efficiency 1.0 -> TREND 1.0 vs RANGE 0.0 (margin 1.0).
TREND_PATH: tuple[Decimal, ...] = tuple(Decimal("1") for _ in range(19))


def _bar(index: int, close: Decimal) -> Observation:
    start = BASE + index * M15
    return Observation(
        instrument_id=IID,
        kind=ObservationKind.BAR,
        event_time=start,
        available_at=start + M15,
        received_at=start + M15,
        published_at=start + M15,
        source="tape",
        sequence=index,
        quality=QualityState.OK,
        payload={
            "start_ns": start,
            "end_ns": start + M15,
            "open": str(close),
            "high": str(close + Decimal("0.05")),
            "low": str(close - Decimal("0.05")),
            "close": str(close),
            "volume": "10",
            "source_hash": f"bar-{start}",
        },
    )


def _series(*segments: Sequence[Decimal]) -> list[Observation]:
    price = Decimal("100")
    prices = [price]
    for segment in segments:
        for delta in segment:
            price += delta
            prices.append(price)
    return [_bar(index, close) for index, close in enumerate(prices)]


def _snapshot(
    bars: Sequence[Observation],
    *,
    cuts_at: int,
    extra: Sequence[Observation] = (),
) -> tuple[ObservationView, Any, int]:
    cut = bars[cuts_at].available_at
    view = build_view([*bars, *extra], cut_time_ns=cut)
    context = assemble_context({Timeframe.M15: view}, instrument_id=IID, decision_time_ns=cut)
    return view, context, cut


def _liquidation(at_ns: int) -> Observation:
    return Observation(
        instrument_id=IID,
        kind=ObservationKind.LIQUIDATION,
        event_time=at_ns,
        available_at=at_ns,
        received_at=at_ns,
        published_at=at_ns,
        source="venue-feed",
        sequence=0,
        quality=QualityState.OK,
        payload={"price": "100", "quantity": "1"},
    )


def test_policy_thresholds_are_pinned_and_versioned() -> None:
    assert BELIEF_POLICY_VERSION == "belief-policy/1"
    assert DEFAULT_POLICY.regime_min_score == Decimal("0.35")
    assert DEFAULT_POLICY.hysteresis_band == Decimal("0.10")
    assert DEFAULT_POLICY.min_candidate_streak == 2
    assert DEFAULT_POLICY.min_bars == 6
    assert DEFAULT_POLICY.window_bars == 20
    assert DEFAULT_POLICY.staleness_horizon_ns == 2 * H1

    # A changed threshold under the default version is refused, so behaviour
    # cannot change without a visible policy identity change.
    with pytest.raises(BeliefPolicyError):
        BeliefPolicy(hysteresis_band=Decimal("0.05"))
    with pytest.raises(BeliefPolicyError):
        BeliefPolicy(regime_min_score=Decimal("0.40"))
    custom = BeliefPolicy(version="belief-policy/2", hysteresis_band=Decimal("0.05"))
    assert custom.version == "belief-policy/2"
    assert policy_digest(custom) != policy_digest(DEFAULT_POLICY)


def test_identical_evidence_is_idempotent() -> None:
    bars = _series(RANGE_PATH)
    view, context, cut = _snapshot(bars, cuts_at=19)

    assert update_trigger(None, view, context, now_ns=cut) is UpdateTrigger.INITIAL
    first = advance(None, view, context, now_ns=cut)
    again = advance(first, view, context, now_ns=cut)

    assert again is first
    assert again.version_hash == first.version_hash
    assert again.dependency_fingerprint == first.dependency_fingerprint
    assert update_trigger(first, view, context, now_ns=cut) is None
    assert first.transition is None
    assert first.status is BeliefStatus.FRESH


def test_reapplying_evidence_mints_one_transition_event() -> None:
    bars = _series(RANGE_PATH, BORDERLINE, BORDERLINE, TREND_PATH, TREND_PATH)
    cuts = (19, 38, 57, 76, 95)
    beliefs = []
    previous = None
    for cuts_at in cuts:
        view, context, cut = _snapshot(bars, cuts_at=cuts_at)
        previous = advance(previous, view, context, now_ns=cut)
        beliefs.append(previous)

    before = beliefs[2]
    switched = beliefs[3]
    assert before.regime_label is RegimeKind.RANGE
    assert before.candidate_streak == 2
    assert switched.regime_label is RegimeKind.TREND
    assert switched.transition is not None
    assert switched.transition.from_regime is RegimeKind.RANGE
    assert switched.transition.to_regime is RegimeKind.TREND
    assert switched.transition.reason is TransitionReason.SCORE_BAND_EXCEEDED
    assert switched.transition.previous_state_id == before.version_hash
    assert switched.transition.hysteresis_band == Decimal("0.10")
    assert switched.transition.occurred_at_ns == switched.decision_time_ns
    assert switched.changed_fields and "regime_label" in switched.changed_fields

    # Same observation set applied a second time: the identical belief and the
    # identical event, never a second event.
    view, context, cut = _snapshot(bars, cuts_at=76)
    repeat = advance(switched, view, context, now_ns=cut)
    assert repeat.version_hash == switched.version_hash
    assert repeat.transition is not None
    assert repeat.transition.event_id == switched.transition.event_id
    assert update_trigger(switched, view, context, now_ns=cut) is None

    # New evidence that confirms the same regime adds no further event.
    confirmed = beliefs[4]
    assert confirmed.transition is None
    assert confirmed.regime_label is RegimeKind.TREND

    event_ids = {belief.transition.event_id for belief in beliefs if belief.transition}
    assert len(event_ids) == 1


def test_clock_only_advance_cannot_advance_the_candidate_streak() -> None:
    """Two cuts over one observation set are one piece of evidence (audit 1 ns case)."""
    bars = _series(RANGE_PATH, TREND_PATH, (Decimal("1"),))
    view, context, cut = _snapshot(bars, cuts_at=19)
    incumbent = advance(None, view, context, now_ns=cut)
    assert incumbent.regime_label is RegimeKind.RANGE

    trend_view, trend_context, trend_cut = _snapshot(bars, cuts_at=38)
    challenger = advance(incumbent, trend_view, trend_context, now_ns=trend_cut)
    assert challenger.regime_label is RegimeKind.RANGE
    assert challenger.regime_candidate is RegimeKind.TREND
    assert challenger.candidate_streak == 1
    assert challenger.transition is None

    # The audit's reproduction: the *same* observations at a decision time one
    # nanosecond later. The old fingerprint folded the clock in, so this second
    # call reported INPUT_CHANGED, incremented the streak to 2 and minted a
    # transition without a new bar.
    clocked_view = build_view([*bars], cut_time_ns=trend_cut + 1)
    clocked_context = assemble_context(
        {Timeframe.M15: clocked_view}, instrument_id=IID, decision_time_ns=trend_cut + 1
    )
    assert update_trigger(challenger, clocked_view, clocked_context, now_ns=trend_cut + 1) is None
    clocked = advance(challenger, clocked_view, clocked_context, now_ns=trend_cut + 1)
    assert clocked is challenger
    assert clocked.decision_time_ns == trend_cut
    assert clocked.candidate_streak == 1
    assert clocked.regime_label is RegimeKind.RANGE
    assert clocked.transition is None

    # A genuine new bar is new evidence and does advance the streak: the second
    # consecutive TREND bar satisfies min_candidate_streak and mints the event.
    next_view, next_context, next_cut = _snapshot(bars, cuts_at=39)
    advanced = advance(challenger, next_view, next_context, now_ns=next_cut)
    assert advanced.regime_label is RegimeKind.TREND
    assert advanced.regime_candidate is RegimeKind.TREND
    assert advanced.transition is not None
    assert advanced.transition.occurred_at_ns == next_cut
    # Acceptance resets the streak; the event itself proves it reached 2.
    assert advanced.candidate_streak == 0


def test_hysteresis_prevents_flip_flopping_on_a_borderline_sequence() -> None:
    bars = _series(RANGE_PATH, BORDERLINE, RANGE_PATH, BORDERLINE, RANGE_PATH)
    cuts = (19, 38, 57, 76, 95)
    beliefs = []
    previous = None
    for cuts_at in cuts:
        view, context, cut = _snapshot(bars, cuts_at=cuts_at)
        previous = advance(previous, view, context, now_ns=cut)
        beliefs.append(previous)

    assert [belief.regime_label for belief in beliefs] == [RegimeKind.RANGE] * len(cuts)
    assert all(belief.transition is None for belief in beliefs)

    challenger = beliefs[1]
    assert challenger.regime_candidate is RegimeKind.TREND
    assert challenger.candidate_streak == 1
    # The challenger is real but inside the band: 0.52 < 0.48 + 0.10.
    assert challenger.regime.trend == Decimal("0.520000")
    assert challenger.regime.range == Decimal("0.480000")
    assert challenger.regime.trend < challenger.regime.range + DEFAULT_POLICY.hysteresis_band
    assert challenger.uncertainty is UncertaintyState.HIGH
    # The streak is reset by the next unambiguous RANGE window.
    assert beliefs[2].candidate_streak == 0
    assert beliefs[3].candidate_streak == 1

    # Same evidence sequence under a *separately versioned* narrow band flips at
    # the second consecutive challenge: the band, not the streak, held the state.
    narrow = BeliefPolicy(version="belief-policy/1-narrow-band", hysteresis_band=Decimal("0.01"))
    narrow_bars = _series(RANGE_PATH, BORDERLINE, BORDERLINE)
    narrow_previous = None
    narrow_beliefs = []
    for cuts_at in (19, 38, 57):
        view, context, cut = _snapshot(narrow_bars, cuts_at=cuts_at)
        narrow_previous = advance(
            narrow_previous, view, context, now_ns=cut, policy=narrow
        )
        narrow_beliefs.append(narrow_previous)
    assert narrow_beliefs[1].regime_label is RegimeKind.RANGE
    assert narrow_beliefs[2].regime_label is RegimeKind.TREND
    assert narrow_beliefs[2].transition is not None
    assert narrow_beliefs[2].transition.hysteresis_band == Decimal("0.01")
    assert narrow_beliefs[2].policy_version == "belief-policy/1-narrow-band"


def test_policy_change_is_an_explicit_rebuild() -> None:
    bars = _series(RANGE_PATH)
    view, context, cut = _snapshot(bars, cuts_at=19)
    belief = advance(None, view, context, now_ns=cut)
    custom = BeliefPolicy(version="belief-policy/2", hysteresis_band=Decimal("0.05"))

    assert update_trigger(belief, view, context, now_ns=cut, policy=custom) is (
        UpdateTrigger.POLICY_CHANGED
    )
    rebuilt = advance(belief, view, context, now_ns=cut, policy=custom)
    assert rebuilt.policy_version == "belief-policy/2"
    assert rebuilt.origin is StateOrigin.UPDATED
    assert rebuilt.transition is None
    assert rebuilt.provenance.policy_digest == policy_digest(custom)
    assert rebuilt.provenance.policy_digest != belief.provenance.policy_digest
    assert rebuilt.version_hash != belief.version_hash
    assert rebuilt.regime_label is belief.regime_label


def test_stale_belief_is_never_fresh() -> None:
    bars = _series(RANGE_PATH)
    view, context, cut = _snapshot(bars, cuts_at=19)
    fresh = advance(None, view, context, now_ns=cut)
    assert fresh.status is BeliefStatus.FRESH
    assert fresh.max_input_available_at == cut

    # At exactly the horizon the evidence is still fresh.
    assert update_trigger(fresh, view, context, now_ns=cut + HORIZON) is None
    # One nanosecond later it is not.
    assert update_trigger(fresh, view, context, now_ns=cut + HORIZON + 1) is (
        UpdateTrigger.STALENESS_CROSSED
    )
    stale = advance(fresh, view, context, now_ns=cut + HORIZON + 1)
    assert stale.status is BeliefStatus.STALE
    assert stale.origin is StateOrigin.STALE
    assert stale.quality is QualityState.STALE
    assert stale.uncertainty is UncertaintyState.HIGH
    assert stale.transition is None
    assert stale.is_actionable_evidence() is False
    assert stale.version_hash != fresh.version_hash
    assert "status" in stale.changed_fields

    # It stays stale under the same evidence, and evidence cannot be re-read
    # backwards in time to recover a fresh state.
    repeat = advance(stale, view, context, now_ns=cut + HORIZON + 2)
    assert repeat.status is BeliefStatus.STALE
    assert repeat.version_hash == stale.version_hash
    with pytest.raises(BeliefContractError) as rewind:
        advance(stale, view, context, now_ns=cut - 1)
    assert rewind.value.code == "NOW_BEFORE_INPUT"

    # New evidence restores a fresh belief; staleness is not sticky once the
    # dependency set changes.
    fresh_bars = _series(RANGE_PATH, RANGE_PATH)
    new_view, new_context, new_cut = _snapshot(fresh_bars, cuts_at=38)
    recovered = advance(stale, new_view, new_context, now_ns=new_cut)
    assert recovered.status is BeliefStatus.FRESH
    assert recovered.origin is StateOrigin.UPDATED
    assert recovered.version_hash != stale.version_hash
    assert recovered.transition is None


def test_irrelevant_observation_does_not_recompute_the_belief() -> None:
    bars = _series(RANGE_PATH)
    view, context, cut = _snapshot(bars, cuts_at=19)
    belief = advance(None, view, context, now_ns=cut)

    noisy_view, noisy_context, noisy_cut = _snapshot(
        bars, cuts_at=19, extra=(_liquidation(cut),)
    )
    assert noisy_cut == cut
    assert len(noisy_view.observation_ids()) == len(view.observation_ids()) + 1

    assert update_trigger(belief, noisy_view, noisy_context, now_ns=cut) is None
    reused = advance(belief, noisy_view, noisy_context, now_ns=cut)
    assert reused is belief
    assert reused.version_hash == belief.version_hash
    assert reused.input_observation_ids == belief.input_observation_ids


def test_missing_inputs_during_an_update_are_observable() -> None:
    bars = _series(RANGE_PATH)
    view, context, cut = _snapshot(bars, cuts_at=19)
    belief = advance(None, view, context, now_ns=cut)

    empty_view = build_view([], cut_time_ns=cut + HORIZON)
    empty_context = assemble_context(
        {}, instrument_id=IID, decision_time_ns=cut + HORIZON
    )
    assert update_trigger(belief, empty_view, empty_context, now_ns=cut + HORIZON) is (
        UpdateTrigger.INPUT_CHANGED
    )
    abstained = advance(belief, empty_view, empty_context, now_ns=cut + HORIZON)
    assert abstained.status is BeliefStatus.ABSTAIN
    assert abstained.origin is StateOrigin.MISSING_INPUT
    assert abstained.missing_inputs == ("bars", "horizon")
    assert abstained.regime_label is RegimeKind.UNKNOWN
    assert abstained.regime_candidate is RegimeKind.UNKNOWN
    assert abstained.transition is None
    assert abstained.input_observation_ids == ()
    assert abstained.uncertainty is UncertaintyState.UNKNOWN
    assert "regime_label" in abstained.changed_fields
    assert "status" in abstained.changed_fields


def test_update_guards_fail_closed() -> None:
    bars = _series(RANGE_PATH)
    view, context, cut = _snapshot(bars, cuts_at=19)

    with pytest.raises(BeliefContractError) as before_inputs:
        advance(None, view, context, now_ns=cut - 1)
    assert before_inputs.value.code == "NOW_BEFORE_INPUT"

    belief = advance(None, view, context, now_ns=cut)
    foreign = assemble_context(
        {Timeframe.M15: view}, instrument_id="ETHUSDT-PERP.BINANCE", decision_time_ns=cut
    )
    with pytest.raises(BeliefContractError) as mismatch:
        advance(belief, view, foreign, now_ns=cut)
    assert mismatch.value.code == "INSTRUMENT_MISMATCH"

    tampered = belief.model_copy(update={"regime_label": RegimeKind.SHOCK})
    with pytest.raises(BeliefIntegrityError):
        advance(tampered, view, context, now_ns=cut)
