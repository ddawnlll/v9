"""M2-01: recurrent opportunity identity, PIT status history and archive traces.

Covers the canonical book extension (append-only status events, as-of snapshots,
decision-linkable archive) and the pure recurrent evaluation state. Every
refusal is asserted as a typed error, not as a silent default.
"""

from decimal import Decimal

import pytest
from pydantic import ValidationError

from v8_next.domain.decision import OpportunityLifecycle, lifecycle_of
from v8_next.domain.market import Candle
from v8_next.opportunities.book import (
    OpportunityBook,
    StatusEventError,
    canonical_witness_ids,
)
from v8_next.opportunities.models import (
    EconomicExposureStructure,
    ExposureDirection,
    OpportunityRecord,
    OpportunityStatus,
)
from v8_next.opportunities.recurrent import (
    NonPitEvaluationError,
    RecurrentConflictError,
    RecurrentLifecycleError,
    RecurrentOpportunityState,
    advance,
    bootstrap,
    witness_digest,
)

HOUR_NS = 3600 * 10**9
INSTRUMENT = "BTCUSDT-PERP.BINANCE"


def make_record(
    *,
    as_of_ns: int = HOUR_NS,
    valid_until_ns: int = 5 * HOUR_NS,
    entry_price: str = "100",
    stop_price: str | None = "95",
    status: OpportunityStatus = OpportunityStatus.CANDIDATE,
) -> OpportunityRecord:
    exposure = EconomicExposureStructure.single_perp(
        "BTCUSDT", "BTC", "binance-um", "USDT", ExposureDirection.LONG
    )
    return OpportunityRecord.create(
        exposure=exposure,
        instrument_id=INSTRUMENT,
        direction=ExposureDirection.LONG,
        entry_price=Decimal(entry_price),
        stop_price=Decimal(stop_price) if stop_price is not None else None,
        target_price=Decimal("110"),
        as_of_time_ns=as_of_ns,
        valid_until_ns=valid_until_ns,
        status=status,
    )


def make_candle(i: int, *, low: str, high: str = "102", close: str = "101") -> Candle:
    return Candle(
        INSTRUMENT,
        i * HOUR_NS,
        (i + 1) * HOUR_NS,
        Decimal("100"),
        Decimal(high),
        Decimal(low),
        Decimal(close),
        Decimal("100"),
        (i + 1) * HOUR_NS,
        (i + 1) * HOUR_NS,
        "test-opp-fixture",
    )


# --------------------------------------------------------------- duplicate evidence


def test_duplicate_witness_cannot_create_a_second_opportunity():
    book = OpportunityBook()
    first = make_record()
    assert book.insert(first, witness_ids=("w-btc", "w-btc")) is first
    assert len(book) == 1
    assert book.insert(first, witness_ids=("w-btc",)) is first
    assert len(book) == 1
    assert len(book.status_history(first.opportunity_id)) == 1

    # A differently-shaped record witnessed by an already-bound witness is not
    # a new opportunity: the canonical episode wins and the new record is dropped.
    other = make_record(entry_price="101")
    canonical = book.insert(other, witness_ids=("w-btc",))
    assert canonical.opportunity_id == first.opportunity_id
    assert book.get(other.opportunity_id) is None
    assert len(book) == 1

    # A new witness on the existing episode binds without minting an event.
    book.insert(first, witness_ids=("w-aux",))
    assert len(book) == 1
    assert len(book.status_history(first.opportunity_id)) == 1


def test_witness_ids_are_canonical_and_blank_witness_fails_closed():
    assert canonical_witness_ids(("b", "a", "b")) == ("a", "b")
    with pytest.raises(StatusEventError):
        canonical_witness_ids(("ok", "   "))

    book = OpportunityBook()
    record = make_record()
    book.insert(record, witness_ids=("w2", "w1", "w2"))
    event = book.status_history(record.opportunity_id)[0]
    assert event.witness_ids == ("w1", "w2")
    assert book.snapshot(2 * HOUR_NS).get(record.opportunity_id).witness_ids == ("w1", "w2")


# ------------------------------------------------------------------- as-of views


def test_snapshot_never_exposes_a_later_status_change():
    book = OpportunityBook()
    record = make_record(as_of_ns=HOUR_NS)
    book.insert(record)
    opp_id = record.opportunity_id

    assert book.snapshot(0).episodes == ()
    assert book.snapshot(HOUR_NS).statuses() == {opp_id: OpportunityStatus.CANDIDATE}

    # Fact at 2h, evidence published at 3h: a 2h cut cannot know it.
    book.record_status(
        opp_id,
        OpportunityStatus.CONFIRMED,
        event_time_ns=2 * HOUR_NS,
        available_at_ns=3 * HOUR_NS,
        reason="ARMED",
    )
    assert book.get(opp_id).status is OpportunityStatus.CONFIRMED
    assert book.snapshot(2 * HOUR_NS).statuses() == {opp_id: OpportunityStatus.CANDIDATE}
    assert book.snapshot(3 * HOUR_NS).statuses() == {opp_id: OpportunityStatus.CONFIRMED}
    assert book.snapshot(3 * HOUR_NS).get(opp_id).opportunity.status is OpportunityStatus.CONFIRMED

    book.record_status(
        opp_id,
        OpportunityStatus.INVALIDATED,
        event_time_ns=4 * HOUR_NS,
        available_at_ns=5 * HOUR_NS,
        reason="STOP",
    )
    assert book.snapshot(4 * HOUR_NS).statuses() == {opp_id: OpportunityStatus.CONFIRMED}
    assert book.snapshot(5 * HOUR_NS).statuses() == {opp_id: OpportunityStatus.INVALIDATED}


def test_status_events_are_append_only_and_monotone():
    book = OpportunityBook()
    record = make_record(as_of_ns=HOUR_NS)
    book.insert(record)
    opp_id = record.opportunity_id
    book.record_status(opp_id, OpportunityStatus.CONFIRMED, event_time_ns=3 * HOUR_NS)
    before = book.status_history(opp_id)

    # A status change whose fact precedes the newest recorded event is refused,
    # and the refusal does not half-apply the lifecycle change.
    with pytest.raises(StatusEventError):
        book.record_status(opp_id, OpportunityStatus.EXPIRED, event_time_ns=2 * HOUR_NS)

    assert book.get(opp_id).status is OpportunityStatus.CONFIRMED
    assert book.status_history(opp_id) == before

    # The same status is a no-op (no event inflation); new witnesses still bind.
    assert (
        book.record_status(opp_id, OpportunityStatus.CONFIRMED, event_time_ns=4 * HOUR_NS) is False
    )
    assert book.status_history(opp_id) == before


def test_snapshot_is_deterministic_and_carries_versioned_event_references():
    def build() -> OpportunityBook:
        book = OpportunityBook()
        record = make_record(as_of_ns=HOUR_NS)
        book.insert(record)
        book.record_status(
            record.opportunity_id,
            OpportunityStatus.CONFIRMED,
            event_time_ns=2 * HOUR_NS,
            reason="ARMED",
        )
        return book

    first = build()
    second = build()
    opp_id = first.all()[0].opportunity_id
    snap_a = first.snapshot(2 * HOUR_NS)
    snap_b = second.snapshot(2 * HOUR_NS)

    assert snap_a == snap_b
    assert snap_a.snapshot_id == snap_b.snapshot_id
    episode = snap_a.get(opp_id)
    assert episode is not None
    assert len(episode.event_ids) == 2
    assert len(set(episode.event_ids)) == 2
    assert all(len(event_id) == 64 for event_id in episode.event_ids)
    assert episode.event_ids == tuple(event.event_id for event in first.status_history(opp_id))


# ------------------------------------------------------- invalidation / expiry


def _replay_invalidation() -> tuple[OpportunityBook, OpportunityRecord]:
    book = OpportunityBook()
    record = make_record(as_of_ns=HOUR_NS, stop_price="95")
    book.insert(record)
    assert book.on_candle(make_candle(2, low="98")) == []
    invalidated = book.on_candle(make_candle(3, low="94"))
    assert len(invalidated) == 1
    return book, record


def test_invalidation_is_deterministic_across_replays():
    book_a, record_a = _replay_invalidation()
    book_b, record_b = _replay_invalidation()

    assert record_a.opportunity_id == record_b.opportunity_id
    assert book_a.snapshot(4 * HOUR_NS) == book_b.snapshot(4 * HOUR_NS)
    assert book_a.snapshot(4 * HOUR_NS).statuses() == {
        record_a.opportunity_id: OpportunityStatus.INVALIDATED
    }
    # Before the breach candle closes, a cut still sees the candidate.
    assert book_a.snapshot(3 * HOUR_NS).statuses() == {
        record_a.opportunity_id: OpportunityStatus.CANDIDATE
    }
    history = book_a.status_history(record_a.opportunity_id)
    assert [event.reason for event in history] == [
        "OPPORTUNITY_OBSERVED",
        "PRICE_INVALIDATION",
    ]
    assert book_a.archive() == book_b.archive()


def test_expiry_is_deterministic_and_reasoned():
    def replay() -> OpportunityBook:
        book = OpportunityBook()
        record = make_record(as_of_ns=HOUR_NS, valid_until_ns=3 * HOUR_NS, stop_price=None)
        book.insert(record)
        assert book.on_candle(make_candle(2, low="99")) == []
        transitioned = book.on_candle(make_candle(4, low="99"))
        assert [ep.status for ep in transitioned] == [OpportunityStatus.EXPIRED]
        return book

    first = replay()
    second = replay()
    opp_id = first.all()[0].opportunity_id
    assert first.snapshot(5 * HOUR_NS) == second.snapshot(5 * HOUR_NS)
    # The expiry fact is stamped by the closing candle (end = 5h), so a 4h cut
    # still sees the candidate: knowledge never arrives before the candle closes.
    assert first.snapshot(4 * HOUR_NS).statuses() == {opp_id: OpportunityStatus.CANDIDATE}
    assert first.snapshot(5 * HOUR_NS).statuses() == {opp_id: OpportunityStatus.EXPIRED}
    assert first.status_history(opp_id)[-1].reason == "TIME_EXPIRY"


# --------------------------------------------------------------------- archive


def test_archive_traces_terminal_episode_to_decision_receipts():
    book = OpportunityBook()
    record = make_record(as_of_ns=HOUR_NS)
    book.insert(record)
    opp_id = record.opportunity_id
    book.on_candle(make_candle(2, low="98"))
    book.on_candle(make_candle(3, low="94"))

    # Decision receipt available only at 5h.
    assert book.link_decision(
        opp_id, "decision-receipt-1", event_time_ns=4 * HOUR_NS, available_at_ns=5 * HOUR_NS
    )
    # Replaying the same receipt is idempotent, not additive.
    assert book.link_decision(
        opp_id, "decision-receipt-1", event_time_ns=4 * HOUR_NS, available_at_ns=5 * HOUR_NS
    )
    assert len(book.decision_links(opp_id)) == 1

    archived_at_four = book.archive(4 * HOUR_NS)
    assert len(archived_at_four) == 1
    assert archived_at_four[0].decision_ids == ()

    archived = book.archive(5 * HOUR_NS)
    assert len(archived) == 1
    entry = archived[0]
    assert entry.opportunity_id == opp_id
    assert entry.status is OpportunityStatus.INVALIDATED
    assert entry.decision_ids == ("decision-receipt-1",)
    assert entry.terminal_event_id == book.status_history(opp_id)[-1].event_id
    assert len(entry.event_ids) == 2

    # Terminal at 4h already; the cut decides what is *traceable*, not what is terminal.
    assert book.archive(4 * HOUR_NS)[0].terminal_event_id == entry.terminal_event_id


def test_link_decision_rejects_unknown_episode_and_blank_id():
    book = OpportunityBook()
    record = make_record()
    book.insert(record)
    assert book.link_decision("missing", "d1", event_time_ns=HOUR_NS) is False
    with pytest.raises(ValueError):
        book.link_decision(record.opportunity_id, "   ", event_time_ns=HOUR_NS)
    with pytest.raises(ValueError):
        book.link_decision(
            record.opportunity_id, "d1", event_time_ns=HOUR_NS, available_at_ns=HOUR_NS - 1
        )


# ---------------------------------------------------------- recurrent advance


def test_recurrent_advance_is_idempotent_per_evaluation_time():
    record = make_record()
    state = bootstrap(record, witness_ids=("w1",))
    assert state.lifecycle is OpportunityLifecycle.WATCHING

    armed = advance(
        state,
        evaluation_time_ns=2 * HOUR_NS,
        status=OpportunityStatus.CONFIRMED,
        witness_ids=("w1",),
        reason="ARMED",
    )
    assert armed.lifecycle is OpportunityLifecycle.ARMED
    assert len(armed.history) == 2
    assert armed.last_evaluation_ns == 2 * HOUR_NS

    replay = advance(
        armed,
        evaluation_time_ns=2 * HOUR_NS,
        status=OpportunityStatus.CONFIRMED,
        witness_ids=("w1",),
        reason="ARMED",
    )
    assert replay is armed
    assert len(replay.history) == 2

    with pytest.raises(RecurrentConflictError):
        advance(
            armed,
            evaluation_time_ns=2 * HOUR_NS,
            status=OpportunityStatus.CONFIRMED,
            witness_ids=("w1", "w2"),
        )
    with pytest.raises(RecurrentConflictError):
        advance(
            armed,
            evaluation_time_ns=2 * HOUR_NS,
            status=OpportunityStatus.ADMITTED,
            witness_ids=("w1",),
        )


def test_recurrent_advance_refuses_non_pit_inputs():
    record = make_record(as_of_ns=2 * HOUR_NS)
    state = bootstrap(record)

    with pytest.raises(NonPitEvaluationError):
        advance(state, evaluation_time_ns=HOUR_NS, status=OpportunityStatus.CONFIRMED)
    with pytest.raises(NonPitEvaluationError):
        advance(
            state,
            evaluation_time_ns=3 * HOUR_NS,
            available_at_ns=4 * HOUR_NS,
            status=OpportunityStatus.CONFIRMED,
        )
    with pytest.raises(NonPitEvaluationError):
        advance(state, evaluation_time_ns=-1, status=OpportunityStatus.CONFIRMED)


def test_recurrent_lifecycle_is_monotone_and_terminal_absorbing():
    state = bootstrap(make_record())
    armed = advance(state, evaluation_time_ns=2 * HOUR_NS, status=OpportunityLifecycle.ARMED)
    with pytest.raises(RecurrentLifecycleError):
        advance(armed, evaluation_time_ns=3 * HOUR_NS, status=OpportunityLifecycle.WATCHING)

    admitted = advance(armed, evaluation_time_ns=3 * HOUR_NS, status=OpportunityLifecycle.ADMITTED)
    invalidated = advance(
        admitted, evaluation_time_ns=4 * HOUR_NS, status=OpportunityLifecycle.INVALIDATED
    )
    assert invalidated.lifecycle is OpportunityLifecycle.INVALIDATED

    with pytest.raises(RecurrentLifecycleError):
        advance(invalidated, evaluation_time_ns=5 * HOUR_NS, status=OpportunityLifecycle.ARMED)
    terminal_replay = advance(
        invalidated, evaluation_time_ns=5 * HOUR_NS, status=OpportunityLifecycle.INVALIDATED
    )
    assert terminal_replay.lifecycle is OpportunityLifecycle.INVALIDATED
    assert len(terminal_replay.history) == 5


def test_recurrent_witness_digest_is_order_insensitive_and_history_bound():
    record = make_record()
    left = bootstrap(record, witness_ids=("w2", "w1"))
    right = bootstrap(record, witness_ids=("w1", "w2", "w1"))
    assert left == right
    assert left.witness_digest == right.witness_digest == witness_digest(("w1", "w2"))

    extended = advance(
        left,
        evaluation_time_ns=2 * HOUR_NS,
        status=OpportunityStatus.CONFIRMED,
        witness_ids=("w3",),
    )
    assert extended.witness_digest == witness_digest(("w1", "w2", "w3"))
    assert extended.witness_digest != left.witness_digest

    with pytest.raises(ValidationError):
        RecurrentOpportunityState(
            opportunity_id=record.opportunity_id,
            lifecycle=OpportunityLifecycle.ARMED,
            history=left.history,
            last_evaluation_ns=left.last_evaluation_ns,
            witness_digest="0" * 64,
        )
    with pytest.raises(ValidationError):
        RecurrentOpportunityState(
            opportunity_id=record.opportunity_id,
            lifecycle=OpportunityLifecycle.WATCHING,
            history=left.history,
            last_evaluation_ns=2 * HOUR_NS,
            witness_digest=left.witness_digest,
        )


def test_recurrent_state_matches_book_as_of_replay():
    book = OpportunityBook()
    record = make_record(as_of_ns=HOUR_NS)
    book.insert(record, witness_ids=("w-market",))
    opp_id = record.opportunity_id
    book.record_status(
        opp_id,
        OpportunityStatus.CONFIRMED,
        event_time_ns=2 * HOUR_NS,
        available_at_ns=2 * HOUR_NS,
        reason="ARMED",
    )
    book.record_status(
        opp_id,
        OpportunityStatus.INVALIDATED,
        event_time_ns=4 * HOUR_NS,
        available_at_ns=4 * HOUR_NS,
        reason="STOP",
    )

    state = bootstrap(record, witness_ids=("w-market",))
    for cut in (HOUR_NS, 2 * HOUR_NS, 4 * HOUR_NS):
        episode = book.snapshot(cut).get(opp_id)
        assert episode is not None
        status = episode.status
        if lifecycle_of(status) is state.lifecycle:
            continue
        state = advance(
            state,
            evaluation_time_ns=cut,
            status=status,
            reason="BOOK_SNAPSHOT",
        )

    assert state.lifecycle is OpportunityLifecycle.INVALIDATED
    assert state.last_evaluation_ns == 4 * HOUR_NS
    assert book.snapshot(4 * HOUR_NS).statuses()[opp_id] is OpportunityStatus.INVALIDATED
    assert [event.status for event in state.history] == [
        OpportunityStatus.CANDIDATE,
        OpportunityStatus.CONFIRMED,
        OpportunityStatus.INVALIDATED,
    ]
