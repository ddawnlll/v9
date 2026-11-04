"""Causal multi-horizon context (M1-02).

These fixtures are built around one decision time, 15:40. At that instant the
15m series can only know through 15:30, the 1h series through 15:00 and the 4h
series through 12:00. Views are deliberately captured *later* so any leak of the
unfinished higher-timeframe bars would be visible; the decision-time gate, not
the view cut, must reject them.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from v8_next.domain.horizons import (
    HorizonAlignment,
    HorizonContext,
    Timeframe,
    assemble_context,
)
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
H4 = 4 * H1
IID = "BTCUSDT-PERP.BINANCE"

#: 15:40, the decision instant used by the aligned fixtures.
DECISION = 15 * H1 + 40 * MIN


def _payload(start_ns: int, duration_ns: int, *, close: str = "100.5") -> dict[str, Any]:
    return {
        "start_ns": start_ns,
        "end_ns": start_ns + duration_ns,
        "open": "100",
        "high": "101",
        "low": "99",
        "close": close,
        "volume": "12.5",
        "source_hash": f"hash-{start_ns}",
    }


def _bar(
    start_ns: int,
    duration_ns: int,
    *,
    instrument: str = IID,
    available_at: int | None = None,
    quality: QualityState = QualityState.OK,
    sequence: int = 0,
) -> Observation:
    end_ns = start_ns + duration_ns
    available = end_ns if available_at is None else available_at
    received = end_ns if available <= end_ns else available
    return Observation(
        instrument_id=instrument,
        kind=ObservationKind.BAR,
        event_time=start_ns,
        available_at=available,
        received_at=received,
        published_at=end_ns,
        source="tape",
        sequence=sequence,
        quality=quality,
        payload=_payload(start_ns, duration_ns),
    )


def _aux(
    kind: ObservationKind,
    event_time: int,
    *,
    available_at: int,
    quality: QualityState,
    instrument: str = IID,
) -> Observation:
    return Observation(
        instrument_id=instrument,
        kind=kind,
        event_time=event_time,
        available_at=available_at,
        received_at=available_at,
        published_at=event_time,
        source="aux-feed",
        sequence=0,
        quality=quality,
        payload={},
    )


def _view(observations: list[Observation], cut_time_ns: int) -> ObservationView:
    return build_view(observations, cut_time_ns=cut_time_ns)


def test_unfinished_higher_timeframe_bars_never_reach_an_earlier_decision() -> None:
    m15 = _view(
        [
            _bar(15 * H1 + 15 * MIN, M15),  # 15:15-15:30, complete at the decision
            _bar(15 * H1 + 30 * MIN, M15),  # 15:30-15:45, unfinished at 15:40
        ],
        cut_time_ns=17 * H1,
    )
    h1 = _view(
        [
            _bar(14 * H1, H1),  # 14:00-15:00, complete
            _bar(15 * H1, H1),  # 15:00-16:00, unfinished at 15:40
        ],
        cut_time_ns=17 * H1,
    )
    h4 = _view(
        [
            _bar(8 * H1, H4),  # 08:00-12:00, complete
            _bar(12 * H1, H4),  # 12:00-16:00, unfinished at 15:40
            _bar(16 * H1, H4),  # 16:00-20:00, later still
        ],
        cut_time_ns=21 * H1,
    )

    context = assemble_context(
        {Timeframe.M15: m15, Timeframe.H1: h1, Timeframe.H4: h4},
        instrument_id=IID,
        decision_time_ns=DECISION,
    )

    assert context.alignment(Timeframe.M15).as_of_ns == 15 * H1 + 30 * MIN
    assert context.alignment(Timeframe.H1).as_of_ns == 15 * H1
    assert context.alignment(Timeframe.H4).as_of_ns == 12 * H1
    assert all(
        context.alignment(timeframe).complete is True for timeframe in Timeframe
    )
    assert all(
        context.alignment(timeframe).quality is QualityState.OK for timeframe in Timeframe
    )


def test_view_cut_at_the_decision_time_rejects_the_in_progress_bar() -> None:
    h1 = _view(
        [
            _bar(14 * H1, H1),
            _bar(15 * H1, H1),  # not available until 16:00 > the cut
        ],
        cut_time_ns=DECISION,
    )
    assert [bar.start_ns for bar in h1.bars(IID)] == [14 * H1]
    assert len(h1.rejections()) == 1

    context = assemble_context(
        {Timeframe.H1: h1}, instrument_id=IID, decision_time_ns=DECISION
    )
    assert context.alignment(Timeframe.H1).as_of_ns == 15 * H1
    assert context.alignment(Timeframe.H1).complete is True


def test_absent_timeframes_are_explicitly_missing() -> None:
    m15 = _view([_bar(15 * H1 + 15 * MIN, M15)], cut_time_ns=DECISION)
    context = assemble_context(
        {Timeframe.M15: m15}, instrument_id=IID, decision_time_ns=DECISION
    )

    assert set(context.alignments) == set(Timeframe)
    assert context.alignment(Timeframe.M15).quality is QualityState.OK
    for timeframe in (Timeframe.H1, Timeframe.H4):
        alignment = context.alignment(timeframe)
        assert alignment.as_of_ns == 0
        assert alignment.complete is False
        assert alignment.quality is QualityState.MISSING

    empty = assemble_context({}, instrument_id=IID, decision_time_ns=DECISION)
    assert all(
        empty.alignment(timeframe).quality is QualityState.MISSING for timeframe in Timeframe
    )


def test_worst_quality_propagates_into_the_alignment() -> None:
    stale = _view(
        [_bar(14 * H1, H1, quality=QualityState.STALE)], cut_time_ns=DECISION
    )
    stale_context = assemble_context(
        {Timeframe.H1: stale}, instrument_id=IID, decision_time_ns=DECISION
    )
    assert stale_context.alignment(Timeframe.H1).complete is True
    assert stale_context.alignment(Timeframe.H1).quality is QualityState.STALE

    missing_aux = _view(
        [
            _bar(14 * H1, H1),
            _aux(
                ObservationKind.OPEN_INTEREST,
                15 * H1 + 20 * MIN,
                available_at=15 * H1 + 20 * MIN,
                quality=QualityState.MISSING,
            ),
        ],
        cut_time_ns=DECISION,
    )
    context = assemble_context(
        {Timeframe.H1: missing_aux}, instrument_id=IID, decision_time_ns=DECISION
    )
    assert context.alignment(Timeframe.H1).complete is True
    assert context.alignment(Timeframe.H1).quality is QualityState.MISSING


def test_off_grid_bars_resolve_deterministically() -> None:
    observations = [
        _bar(13 * H1 + 7 * MIN, H1),
        _bar(13 * H1 + 37 * MIN, H1),
    ]
    first = assemble_context(
        {Timeframe.H1: _view(observations, cut_time_ns=16 * H1)},
        instrument_id=IID,
        decision_time_ns=15 * H1,
    )
    second = assemble_context(
        {Timeframe.H1: _view(list(reversed(observations)), cut_time_ns=16 * H1)},
        instrument_id=IID,
        decision_time_ns=15 * H1,
    )

    assert first.alignment(Timeframe.H1) == second.alignment(Timeframe.H1)
    alignment = first.alignment(Timeframe.H1)
    assert alignment.as_of_ns == 13 * H1 + 97 * MIN
    assert alignment.complete is True
    assert alignment.quality is QualityState.OK


def test_gapped_series_is_complete_but_stale() -> None:
    view = _view(
        [
            _bar(12 * H1, H1),
            _bar(13 * H1, H1),
        ],
        cut_time_ns=17 * H1,
    )
    context = assemble_context(
        {Timeframe.H1: view}, instrument_id=IID, decision_time_ns=16 * H1 + 10 * MIN
    )
    alignment = context.alignment(Timeframe.H1)
    assert alignment.as_of_ns == 14 * H1
    assert alignment.complete is True
    assert alignment.quality is QualityState.STALE


def test_partial_bar_is_incomplete_and_stale() -> None:
    view = _view([_bar(13 * H1 + 30 * MIN, 30 * MIN)], cut_time_ns=15 * H1)
    context = assemble_context(
        {Timeframe.H1: view}, instrument_id=IID, decision_time_ns=15 * H1
    )
    alignment = context.alignment(Timeframe.H1)
    assert alignment.as_of_ns == 14 * H1
    assert alignment.complete is False
    assert alignment.quality is QualityState.STALE


def test_late_availability_is_not_used_and_quality_does_not_leak_backwards() -> None:
    view = _view(
        [
            _bar(13 * H1, H1),
            # Complete and inside the view, but only published at 16:30: a 15:40
            # decision could not have seen it.
            _bar(14 * H1, H1, available_at=16 * H1 + 30 * MIN),
            # A later, contradictory aux record must not colour the earlier cut.
            _aux(
                ObservationKind.FUNDING,
                16 * H1,
                available_at=16 * H1 + 30 * MIN,
                quality=QualityState.CONTRADICTORY,
            ),
        ],
        cut_time_ns=17 * H1,
    )
    assert len(view.bars(IID)) == 2
    assert view.quality(IID) is QualityState.CONTRADICTORY
    assert view.quality_at(IID, DECISION) is QualityState.OK

    context = assemble_context(
        {Timeframe.H1: view}, instrument_id=IID, decision_time_ns=DECISION
    )
    alignment = context.alignment(Timeframe.H1)
    assert alignment.as_of_ns == 14 * H1
    assert alignment.complete is True
    # STALE comes from the one full H1 window that has elapsed since the last
    # visible close (a late publication), never from the future CONTRADICTORY record.
    assert alignment.quality is QualityState.STALE

    cut_view = _view([_bar(14 * H1, H1, available_at=16 * H1 + 30 * MIN)], cut_time_ns=DECISION)
    assert cut_view.rejections()
    cut_context = assemble_context(
        {Timeframe.H1: cut_view}, instrument_id=IID, decision_time_ns=DECISION
    )
    assert cut_context.alignment(Timeframe.H1).as_of_ns == 0
    assert cut_context.alignment(Timeframe.H1).quality is QualityState.MISSING


def test_context_is_immutable_and_survives_later_records() -> None:
    observations = [_bar(14 * H1, H1)]
    view = _view(observations, cut_time_ns=DECISION)
    views = {Timeframe.H1: view}
    context = assemble_context(views, instrument_id=IID, decision_time_ns=DECISION)

    before = context.alignment(Timeframe.H1)
    observations.append(_bar(15 * H1, H1, available_at=16 * H1))
    views[Timeframe.H4] = _view([_bar(8 * H1, H4)], cut_time_ns=DECISION)

    assert context.alignment(Timeframe.H1) == before
    assert context.alignment(Timeframe.H4).quality is QualityState.MISSING

    later = assemble_context(views, instrument_id=IID, decision_time_ns=DECISION)
    assert later.alignment(Timeframe.H1) == before
    # The view snapshot itself never learned about the appended record.
    assert len(view.bars(IID)) == 1

    with pytest.raises(ValidationError):
        context.decision_time_ns = 0  # type: ignore[misc]
    with pytest.raises(ValidationError):
        before.as_of_ns = 0  # type: ignore[misc]


def test_context_schema_rejects_incomplete_or_mismatched_alignments() -> None:
    alignment = HorizonAlignment(
        timeframe=Timeframe.H1, as_of_ns=H1, complete=True, quality=QualityState.OK
    )
    with pytest.raises(ValidationError):
        HorizonContext(
            instrument_id=IID,
            decision_time_ns=DECISION,
            alignments={Timeframe.H1: alignment},
        )
    with pytest.raises(ValidationError):
        HorizonContext(
            instrument_id=IID,
            decision_time_ns=DECISION,
            alignments={
                Timeframe.M15: alignment,
                Timeframe.H1: alignment,
                Timeframe.H4: alignment,
            },
        )


def test_invalid_inputs_fail_closed() -> None:
    view = _view([_bar(14 * H1, H1)], cut_time_ns=DECISION)
    with pytest.raises(ValueError):
        assemble_context({Timeframe.H1: view}, instrument_id="", decision_time_ns=DECISION)
    with pytest.raises(ValueError):
        assemble_context(
            {Timeframe.H1: view}, instrument_id=IID, decision_time_ns=True
        )
    with pytest.raises(ValueError):
        assemble_context(
            {"H1": view},  # type: ignore[dict-item]
            instrument_id=IID,
            decision_time_ns=DECISION,
        )
