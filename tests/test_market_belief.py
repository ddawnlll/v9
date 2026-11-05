"""``MarketBeliefV1`` schema: roundtrip, determinism, PIT binding, missing data (M1-03).

Every fixture is derived from ``Observation``/``ObservationView``/``HorizonContext``
only, through :func:`v8_next.belief.advance`. Nothing here reaches for an expert
or a strategy: the belief is a view over point-in-time inputs.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal
from typing import Any

import pytest
from pydantic import ValidationError

from v8_next.belief import (
    BELIEF_SCHEMA_VERSION,
    DEFAULT_POLICY,
    BeliefStatus,
    EpistemicLabel,
    LiquidityAvailability,
    MarketBeliefV1,
    RegimeKind,
    RegimeScores,
    StructureDirection,
    UncertaintyState,
    VolatilityLevel,
    advance,
    belief_version_hash,
    policy_digest,
    verify_version_hash,
)
from v8_next.belief.state import BeliefIntegrityError
from v8_next.domain.horizons import HorizonContext, Timeframe, assemble_context
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
#: First bar opens at 10:00; every fixture is deterministic on this grid.
BASE = 10 * H1


def _bar_payload(start_ns: int, close: Decimal) -> dict[str, Any]:
    return {
        "start_ns": start_ns,
        "end_ns": start_ns + M15,
        "open": str(close),
        "high": str(close + Decimal("0.05")),
        "low": str(close - Decimal("0.05")),
        "close": str(close),
        "volume": "10",
        "source_hash": f"bar-{start_ns}",
    }


def _bar(index: int, close: Decimal, *, quality: QualityState = QualityState.OK) -> Observation:
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
        quality=quality,
        payload=_bar_payload(start, close),
    )


def _proxy(kind: ObservationKind, at_ns: int, payload: dict[str, Any]) -> Observation:
    return Observation(
        instrument_id=IID,
        kind=kind,
        event_time=at_ns,
        available_at=at_ns,
        received_at=at_ns,
        published_at=at_ns,
        source="venue-feed",
        sequence=0,
        quality=QualityState.OK,
        payload=payload,
    )


def _prices(start: Decimal, deltas: Sequence[Decimal]) -> list[Decimal]:
    path = [start]
    for delta in deltas:
        path.append(path[-1] + delta)
    return path


def _snapshot(
    deltas: Sequence[Decimal],
    *,
    extra: Sequence[Observation] = (),
    cut_index: int | None = None,
) -> tuple[ObservationView, HorizonContext, int, list[Observation]]:
    path = _prices(Decimal("100"), deltas)
    bars = [_bar(index, close) for index, close in enumerate(path)]
    end_index = len(bars) - 1 if cut_index is None else cut_index
    cut = bars[end_index].available_at
    observations = bars + list(extra)
    view = build_view(observations, cut_time_ns=cut)
    context = assemble_context({Timeframe.M15: view}, instrument_id=IID, decision_time_ns=cut)
    return view, context, cut, bars


#: 19 deltas with path efficiency exactly 0.52: a TREND challenger 0.04 ahead of RANGE.
#: The default hysteresis band is 0.10, so this challenger must never win.
BORDERLINE: tuple[Decimal, ...] = (
    *(Decimal("1.5") for _ in range(12)),
    Decimal("1.0"),
    *(Decimal("-1.0") for _ in range(6)),
)
TREND: tuple[Decimal, ...] = tuple(Decimal("1") for _ in range(19))


def test_schema_roundtrips_and_replay_is_deterministic() -> None:
    view, context, cut, _ = _snapshot(BORDERLINE)

    first = advance(None, view, context, now_ns=cut)
    replay = advance(None, view, context, now_ns=cut)

    assert first.version_hash == replay.version_hash
    assert first == replay
    assert first.version_hash == belief_version_hash(first)
    assert first.schema_version == BELIEF_SCHEMA_VERSION
    assert first.policy_version == DEFAULT_POLICY.version
    assert first.provenance.policy_digest == policy_digest(DEFAULT_POLICY)
    assert first.provenance.feature_version and first.provenance.model_version
    assert first.provenance.source_hash

    roundtripped = MarketBeliefV1.model_validate_json(first.model_dump_json())
    assert roundtripped == first
    assert roundtripped.version_hash == first.version_hash
    roundtripped.verify()

    # Advisory only: no order authority of any kind can be attached.
    assert first.advisory_only is True
    assert first.order_authority is False
    assert first.is_actionable_evidence() is True
    with pytest.raises(ValidationError):
        MarketBeliefV1.model_validate({**first.model_dump(), "advisory_only": False})
    with pytest.raises(ValidationError):
        MarketBeliefV1.model_validate({**first.model_dump(), "order_authority": True})


def test_scores_are_scores_and_tampering_is_rejected() -> None:
    scores = RegimeScores(
        trend=Decimal("0.4"),
        range=Decimal("0.6"),
        transition=Decimal("0"),
        shock=Decimal("0"),
        unknown=Decimal("0"),
    )
    assert scores.label is EpistemicLabel.SCORE
    with pytest.raises(ValidationError):
        RegimeScores(
            label=EpistemicLabel.PROBABILITY,
            trend=Decimal("0.4"),
            range=Decimal("0.6"),
            transition=Decimal("0"),
            shock=Decimal("0"),
            unknown=Decimal("0"),
        )

    view, context, cut, _ = _snapshot(TREND)
    belief = advance(None, view, context, now_ns=cut)
    tampered = belief.model_copy(update={"status": BeliefStatus.STALE})
    with pytest.raises(BeliefIntegrityError):
        verify_version_hash(tampered)
    with pytest.raises(ValidationError):
        MarketBeliefV1.model_validate({**belief.model_dump(), "uncertainty": "HIGH"})


def test_belief_binds_to_available_pit_inputs() -> None:
    view, context, cut, _ = _snapshot(TREND)
    belief = advance(None, view, context, now_ns=cut)

    assert belief.input_observation_ids
    assert belief.max_input_available_at <= belief.decision_time_ns
    assert belief.max_input_available_at == max(
        view.require_observation(identity).available_at
        for identity in belief.input_observation_ids
    )
    for identity in belief.input_observation_ids:
        observation = view.require_observation(identity)
        assert observation.available_at <= belief.decision_time_ns
    assert belief.provenance.data_role.value == "OBSERVED"
    assert belief.status is BeliefStatus.FRESH


def test_future_data_cannot_change_a_belief() -> None:
    view, context, cut, bars = _snapshot(BORDERLINE)
    belief = advance(None, view, context, now_ns=cut)

    # (a) The snapshot's own bars are deep-copied: mutating the caller's records
    # after the view exists cannot reach a belief built from that view.
    bars[0].payload["close"] = "1"
    bars[0].payload["high"] = "2"
    after_mutation = advance(None, view, context, now_ns=cut)
    assert after_mutation.version_hash == belief.version_hash

    # (b) A view captured *later*, containing observations that only became
    # available after the decision time, must produce the same belief.
    later_view, _, later_cut, _ = _snapshot(BORDERLINE + TREND)
    assert later_cut > cut
    leaked_context = assemble_context(
        {Timeframe.M15: later_view}, instrument_id=IID, decision_time_ns=cut
    )
    leaked = advance(None, later_view, leaked_context, now_ns=cut)
    assert leaked.version_hash == belief.version_hash
    assert leaked.input_observation_ids == belief.input_observation_ids


def test_missing_liquidity_is_explicit_and_never_fabricated() -> None:
    view, context, cut, _ = _snapshot(TREND)
    belief = advance(None, view, context, now_ns=cut)

    assert belief.liquidity.availability is LiquidityAvailability.MISSING
    assert belief.liquidity.mark_price is None
    assert belief.liquidity.spread_bps is None
    assert belief.liquidity.open_interest is None
    assert belief.liquidity.funding_rate is None
    assert belief.liquidity.quality is QualityState.MISSING
    assert "MARK" in belief.liquidity.missing_dimensions
    assert "QUOTE" in belief.liquidity.missing_dimensions
    assert belief.status is BeliefStatus.FRESH

    malformed = _proxy(ObservationKind.MARK, cut, {"price": "not-a-number"})
    view_bad, context_bad, cut_bad, _ = _snapshot(TREND, extra=(malformed,))
    bad = advance(None, view_bad, context_bad, now_ns=cut_bad)
    assert bad.liquidity.availability is LiquidityAvailability.UNKNOWN
    assert bad.liquidity.mark_price is None
    assert bad.liquidity.quality is QualityState.UNKNOWN
    assert "MARK" in bad.liquidity.missing_dimensions


def test_present_liquidity_reads_only_published_channels() -> None:
    view, _, cut, _ = _snapshot(TREND)
    extra = (
        _proxy(ObservationKind.MARK, cut, {"price": "101.5"}),
        _proxy(ObservationKind.QUOTE, cut, {"best_bid": "101.49", "best_ask": "101.51"}),
        _proxy(ObservationKind.OPEN_INTEREST, cut, {"open_interest": "12000"}),
        _proxy(ObservationKind.FUNDING, cut, {"funding_rate": "0.0001"}),
    )
    view, context, cut, _ = _snapshot(TREND, extra=extra)
    belief = advance(None, view, context, now_ns=cut)

    assert belief.liquidity.availability is LiquidityAvailability.PRESENT
    assert belief.liquidity.mark_price == Decimal("101.50000000")
    assert belief.liquidity.spread_bps == Decimal("1.9704")
    assert belief.liquidity.open_interest == Decimal("12000")
    assert belief.liquidity.funding_rate == Decimal("0.0001")
    assert belief.liquidity.missing_dimensions == ()
    assert belief.liquidity.proxy_only is True


def test_absent_required_inputs_produce_an_abstain_belief() -> None:
    empty = build_view([], cut_time_ns=BASE)
    context = assemble_context({}, instrument_id=IID, decision_time_ns=BASE)
    belief = advance(None, empty, context, now_ns=BASE)

    assert belief.status is BeliefStatus.ABSTAIN
    assert belief.regime_label is RegimeKind.UNKNOWN
    assert belief.regime.unknown == Decimal("1")
    assert belief.missing_inputs == ("bars", "horizon")
    assert belief.structure.direction is StructureDirection.UNKNOWN
    assert belief.structure.window_low is None
    assert belief.volatility.level is VolatilityLevel.UNKNOWN
    assert belief.volatility.range_bps is None
    assert belief.uncertainty is UncertaintyState.UNKNOWN
    assert belief.input_observation_ids == ()

    # Bars exist but no horizon is complete: still an explicit abstention.
    view, _, cut, _ = _snapshot(TREND)
    no_horizon = assemble_context({}, instrument_id=IID, decision_time_ns=cut)
    partial = advance(None, view, no_horizon, now_ns=cut)
    assert partial.status is BeliefStatus.ABSTAIN
    assert partial.missing_inputs == ("horizon",)


def test_unknown_quality_blocks_a_fresh_state() -> None:
    degraded = _proxy(ObservationKind.MARK, BASE + 20 * M15, {"price": "100"})
    degraded = degraded.model_copy(update={"quality": QualityState.UNKNOWN})
    view, context, cut, _ = _snapshot(TREND, extra=(degraded,))
    belief = advance(None, view, context, now_ns=cut)

    assert belief.status is BeliefStatus.UNKNOWN
    assert belief.quality is QualityState.UNKNOWN
    assert belief.is_actionable_evidence() is False


def test_belief_carries_no_unavailable_ex_ante_dimensions() -> None:
    from v8_next.telemetry.belief import UNAVAILABLE_EX_ANTE_DIMENSIONS

    for name in UNAVAILABLE_EX_ANTE_DIMENSIONS:
        assert name not in MarketBeliefV1.model_fields
    view, context, cut, _ = _snapshot(TREND)
    payload = {**advance(None, view, context, now_ns=cut).model_dump()}
    for name in UNAVAILABLE_EX_ANTE_DIMENSIONS:
        with pytest.raises(ValidationError):
            MarketBeliefV1.model_validate({**payload, name: 0.5})
