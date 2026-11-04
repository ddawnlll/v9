"""Point-in-time observation contract (M1-01).

Every test here pins one sentence of the contract: availability gates use,
future records are rejected *with a type* rather than silently absent,
contradictions and amendments fail closed, quality is surfaced rather than
filtered, and a view is an immutable snapshot that later records cannot rewrite.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from decimal import Decimal
from typing import Any

import pytest
from pydantic import ValidationError

from v8_next.domain.observation import (
    Observation,
    ObservationKind,
    ObservationView,
    PitViolationError,
    QualityState,
    RejectionReason,
    build_view,
    observation_id,
)

M15 = 15 * 60 * 1_000_000_000
IID = "BTCUSDT-PERP.BINANCE"


def _payload(start_ns: int, duration_ns: int = M15, *, close: str = "100.5") -> dict[str, Any]:
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
    *,
    duration_ns: int = M15,
    instrument: str = IID,
    available_at: int | None = None,
    quality: QualityState = QualityState.OK,
    sequence: int = 0,
    source: str = "tape",
    close: str = "100.5",
    payload: dict[str, Any] | None = None,
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
        source=source,
        sequence=sequence,
        quality=quality,
        payload=_payload(start_ns, duration_ns, close=close) if payload is None else payload,
    )


def _non_bar(
    kind: ObservationKind,
    event_time: int,
    *,
    available_at: int,
    quality: QualityState,
    instrument: str = IID,
    payload: dict[str, Any] | None = None,
) -> Observation:
    return Observation(
        instrument_id=instrument,
        kind=kind,
        event_time=event_time,
        available_at=available_at,
        received_at=max(event_time, available_at),
        published_at=event_time,
        source="aux-feed",
        sequence=0,
        quality=quality,
        payload={} if payload is None else payload,
    )


def test_usable_and_rejected_partitions_are_explicit() -> None:
    known = _bar(0, available_at=M15)
    future = _bar(M15, available_at=2 * M15)
    view = build_view([known, future], cut_time_ns=M15)

    bars = view.bars(IID)
    assert len(bars) == 1
    assert bars[0].start_ns == 0
    assert view.max_input_available_at == M15
    assert view.quality(IID) is QualityState.OK

    rejections = view.rejections()
    assert len(rejections) == 1
    assert rejections[0].reason is RejectionReason.NOT_YET_AVAILABLE
    assert rejections[0].observation.available_at == 2 * M15
    assert view.observation_ids() == (observation_id(known),)

    future_id = observation_id(future)
    with pytest.raises(PitViolationError) as excinfo:
        view.require_observation(future_id)
    assert excinfo.value.code == RejectionReason.NOT_YET_AVAILABLE.value

    with pytest.raises(PitViolationError) as excinfo:
        view.require_bar(IID, start_ns=M15)
    assert excinfo.value.code == RejectionReason.NOT_YET_AVAILABLE.value

    with pytest.raises(KeyError):
        view.require_bar(IID, start_ns=7 * M15)
    with pytest.raises(KeyError):
        view.require_observation("not-an-observation-id")


def test_contradictory_duplicates_raise() -> None:
    first = _bar(0, close="100.5", source="feed-a", sequence=1)
    second = _bar(0, close="222.0", source="feed-b", sequence=2)
    with pytest.raises(PitViolationError) as excinfo:
        build_view([first, second], cut_time_ns=M15)
    assert excinfo.value.code == QualityState.CONTRADICTORY.value


def test_contradictory_future_duplicate_does_not_poison_the_earlier_view() -> None:
    known = _bar(0, close="100.5", source="feed-a", sequence=1)
    future_duplicate = _bar(
        0, close="222.0", source="feed-b", sequence=2, available_at=10 * M15
    )
    view = build_view([known, future_duplicate], cut_time_ns=M15)

    assert len(view.bars(IID)) == 1
    assert view.bars(IID)[0].close == Decimal("100.5")
    assert len(view.rejections()) == 1
    assert view.rejections()[0].observation.payload["close"] == "222.0"


def test_amended_future_observation_raises_even_when_requested_late() -> None:
    amended = _bar(0, quality=QualityState.AMENDED_FUTURE)
    with pytest.raises(PitViolationError) as excinfo:
        build_view([amended], cut_time_ns=10 * M15)
    assert excinfo.value.code == QualityState.AMENDED_FUTURE.value


def test_missing_and_stale_are_surfaced_not_dropped_or_fabricated() -> None:
    stale = _bar(0, quality=QualityState.STALE)
    missing = _bar(2 * M15, quality=QualityState.MISSING, payload={})
    view = build_view([stale, missing], cut_time_ns=3 * M15)

    bars = view.bars(IID)
    assert len(bars) == 1
    assert bars[0].start_ns == 0
    assert view.quality(IID) is QualityState.MISSING

    with pytest.raises(PitViolationError) as excinfo:
        view.require_bar(IID, start_ns=2 * M15)
    assert excinfo.value.code == RejectionReason.QUALITY_BLOCKED.value

    # The missing observation is still part of the view, not silently dropped.
    assert len(view.observations(IID)) == 2
    assert len(view.observation_ids()) == 2


def test_stale_bars_remain_queryable_with_stale_quality() -> None:
    view = build_view([_bar(0, quality=QualityState.STALE)], cut_time_ns=M15)
    assert len(view.bars(IID)) == 1
    assert view.quality(IID) is QualityState.STALE


def test_identical_duplicate_payloads_dedupe_deterministically() -> None:
    first = _bar(0, source="feed-a", sequence=1)
    duplicate = _bar(0, source="feed-b", sequence=2)
    view = build_view([first, duplicate], cut_time_ns=M15)
    assert len(view.observation_ids()) == 1
    assert len(view.bars(IID)) == 1


def test_view_is_immutable_and_survives_input_mutation() -> None:
    payload = _payload(0)
    observation = _bar(0, payload=payload)
    source = [observation]
    view = build_view(source, cut_time_ns=M15)

    payload["close"] = "999"
    source.append(_bar(M15, available_at=2 * M15))

    assert view.bars(IID)[0].close == Decimal("100.5")
    assert view.observations(IID)[0].payload["close"] == "100.5"
    assert len(view.observations(IID)) == 1

    with pytest.raises(FrozenInstanceError):
        view.cut_time_ns = 1  # type: ignore[misc]
    with pytest.raises(TypeError):
        view._bars[IID] = ()  # type: ignore[index]


def test_observation_identity_is_content_addressed_and_order_independent() -> None:
    first = _bar(0)
    second = _bar(M15)
    forward = build_view([first, second], cut_time_ns=2 * M15)
    backward = build_view([second, first], cut_time_ns=2 * M15)

    assert forward.observation_ids() == backward.observation_ids()
    assert forward.bars(IID) == backward.bars(IID)
    assert observation_id(first) != observation_id(_bar(0, close="101.0"))


def test_quality_at_never_leaks_later_quality_backwards() -> None:
    view = build_view(
        [
            _bar(0, quality=QualityState.OK),
            _bar(M15, quality=QualityState.STALE, available_at=2 * M15),
        ],
        cut_time_ns=2 * M15,
    )
    assert view.quality(IID) is QualityState.STALE
    assert view.quality_at(IID, M15) is QualityState.OK
    assert view.quality_at(IID, 2 * M15) is QualityState.STALE
    assert view.quality_at("OTHER", 2 * M15) is QualityState.MISSING
    assert view.quality_at(IID, 99 * M15) is QualityState.STALE


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("available_at", M15 - 1),
        ("received_at", -1),
        ("published_at", 10 * M15),
    ],
)
def test_observation_lineage_causality_is_enforced(field: str, value: int) -> None:
    kwargs: dict[str, Any] = {
        "instrument_id": IID,
        "kind": ObservationKind.BAR,
        "event_time": 0,
        "available_at": M15,
        "received_at": M15,
        "published_at": M15,
        "source": "tape",
        "sequence": 0,
        "quality": QualityState.OK,
        "payload": _payload(0),
    }
    kwargs[field] = value
    with pytest.raises(ValidationError):
        Observation(**kwargs)


def test_boolean_timestamps_are_not_integers() -> None:
    with pytest.raises(ValidationError):
        Observation(
            instrument_id=IID,
            kind=ObservationKind.BAR,
            event_time=True,
            available_at=M15,
            received_at=M15,
            published_at=M15,
            source="tape",
            sequence=0,
            quality=QualityState.OK,
            payload=_payload(0),
        )


@pytest.mark.parametrize(
    "mutate",
    [
        lambda payload: payload.pop("close"),
        lambda payload: payload.update({"high": "90"}),
        lambda payload: payload.update({"start_ns": 1}),
        lambda payload: payload.update({"volume": "-1"}),
    ],
)
def test_malformed_bar_payloads_fail_closed(mutate: Any) -> None:
    payload = _payload(0)
    mutate(payload)
    with pytest.raises(PitViolationError):
        build_view([_bar(0, payload=payload)], cut_time_ns=M15)


def test_non_canonical_payload_fails_closed() -> None:
    payload = _payload(0)
    payload["extra"] = {1, 2}
    with pytest.raises(PitViolationError) as excinfo:
        build_view([_bar(0, payload=payload)], cut_time_ns=M15)
    assert excinfo.value.code == "MALFORMED_PAYLOAD"


def test_empty_view_reports_missing_everything() -> None:
    view = build_view([], cut_time_ns=M15)
    assert view.bars(IID) == ()
    assert view.observation_ids() == ()
    assert view.rejections() == ()
    assert view.quality(IID) is QualityState.MISSING
    assert view.max_input_available_at == M15


def test_view_rejects_non_observation_input() -> None:
    with pytest.raises(TypeError):
        build_view([_payload(0)], cut_time_ns=M15)  # type: ignore[list-item]


def test_cut_time_must_be_an_exact_integer() -> None:
    with pytest.raises(ValueError):
        build_view([], cut_time_ns=True)


def test_observation_view_public_queries_are_read_only() -> None:
    view: ObservationView = build_view([_bar(0)], cut_time_ns=M15)
    assert view.cut_time_ns == M15
    assert view.max_input_available_at == M15
