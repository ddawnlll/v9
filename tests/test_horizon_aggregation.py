"""Synthetic deterministic mechanics tests, not market/economic evidence."""
from dataclasses import replace
from decimal import Decimal

import pytest

from v8_next.domain.horizon_aggregation import H1_NS, H4_NS, M15_NS, MINUTE_NS, HorizonAggregator
from v8_next.domain.market import Candle
from v8_next.domain.observation import QualityState


def bar(i: int, *, duration: int = M15_NS, delay: int = 0) -> Candle:
    start = i * duration
    price = Decimal(100 + i)
    return Candle("BTC", start, start + duration, price, price + 2, price - 1,
                  price + 1, Decimal(i + 1), start + duration, start + duration + delay,
                  f"source-{duration}-{i}")


def test_completed_aggregate_and_availability_are_event_driven():
    aggregate = HorizonAggregator("BTC", source_duration_ns=M15_NS,
                                  target_durations_ns=(H4_NS, H1_NS))
    for i in range(15):
        source = bar(i)
        events = aggregate.advance(source, processing_ns=source.available_ns, quality=QualityState.OK)
        assert [e.duration_ns for e in events] == ([H1_NS] if i % 4 == 3 else [])
        assert aggregate.pending_counts[H1_NS] <= 3
        assert aggregate.pending_counts[H4_NS] <= 15
    source = bar(15, delay=11)
    events = aggregate.advance(source, processing_ns=source.available_ns + 17, quality=QualityState.OK)
    assert [e.duration_ns for e in events] == [H1_NS, H4_NS]
    h4 = events[1]
    assert (h4.candle.start_ns, h4.candle.end_ns) == (0, H4_NS)
    assert (h4.candle.open, h4.candle.high, h4.candle.low, h4.candle.close,
            h4.candle.volume) == (Decimal(100), Decimal(117), Decimal(99), Decimal(116), Decimal(136))
    assert h4.candle.available_ns == source.available_ns + 17
    assert h4.candle.received_ns == source.available_ns + 17
    assert len(h4.source_ids) == 16
    assert aggregate.pending_counts == {H1_NS: 0, H4_NS: 0}


def test_start_mid_window_never_emits_partial_bar():
    aggregate = HorizonAggregator("BTC", source_duration_ns=M15_NS, target_durations_ns=(H1_NS,))
    results = []
    for i in range(1, 8):
        source = bar(i)
        results.extend(aggregate.advance(source, processing_ns=source.available_ns, quality=QualityState.OK))
    assert [(e.candle.start_ns, e.candle.end_ns) for e in results] == [(H1_NS, 2 * H1_NS)]


def test_gap_and_unavailable_events_do_not_mutate_state():
    aggregate = HorizonAggregator("BTC", source_duration_ns=M15_NS, target_durations_ns=(H1_NS,))
    source = bar(0)
    aggregate.advance(source, processing_ns=source.available_ns, quality=QualityState.OK)
    before = aggregate.pending_counts
    with pytest.raises(ValueError, match="gap"):
        aggregate.advance(bar(2), processing_ns=bar(2).available_ns, quality=QualityState.OK)
    with pytest.raises(ValueError, match="unavailable"):
        aggregate.advance(bar(1, delay=100), processing_ns=bar(1).end_ns, quality=QualityState.OK)
    with pytest.raises(ValueError, match="quality"):
        aggregate.advance(bar(1), processing_ns=bar(1).end_ns, quality=QualityState.UNKNOWN)
    assert aggregate.pending_counts == before
    for i in range(1, 4):
        source = bar(i)
        events = aggregate.advance(source, processing_ns=source.available_ns, quality=QualityState.OK)
    assert len(events) == 1


def test_identity_binds_source_content_availability_and_processing_time():
    def run(*, changed: bool = False, extra_delay: int = 0):
        agg = HorizonAggregator("BTC", source_duration_ns=M15_NS, target_durations_ns=(H1_NS,))
        for i in range(4):
            source = bar(i)
            if i == 1 and changed:
                source = replace(source, volume=Decimal("42"))
            events = agg.advance(source, processing_ns=source.available_ns + extra_delay, quality=QualityState.OK)
        return events[0]

    assert run() == run()
    assert run().candle.source_hash != run(changed=True).candle.source_hash
    assert run().candle.source_hash != run(extra_delay=10).candle.source_hash


def test_m1_to_m15_has_exact_count_and_duplicate_rejected():
    agg = HorizonAggregator("BTC", source_duration_ns=MINUTE_NS, target_durations_ns=(M15_NS,))
    for i in range(15):
        source = bar(i, duration=MINUTE_NS)
        events = agg.advance(source, processing_ns=source.available_ns, quality=QualityState.OK)
    assert len(events) == 1 and len(events[0].source_ids) == 15
    with pytest.raises(ValueError, match="gap"):
        agg.advance(source, processing_ns=source.available_ns, quality=QualityState.OK)


@pytest.mark.parametrize("name,value", [
    ("instrument_id", "ETH"),
    ("source_duration_ns", MINUTE_NS),
    ("target_durations_ns", (H4_NS,)),
])
def test_configuration_cannot_change_while_window_is_pending(name, value):
    aggregator = HorizonAggregator("BTC", source_duration_ns=M15_NS,
                                   target_durations_ns=(H1_NS,))
    first = bar(0)
    aggregator.advance(first, processing_ns=first.available_ns, quality=QualityState.OK)
    with pytest.raises(AttributeError):
        setattr(aggregator, name, value)
    # The pending BTC prefix still belongs to the original H1 window.
    for i in range(1, 4):
        source = bar(i)
        events = aggregator.advance(source, processing_ns=source.available_ns,
                                    quality=QualityState.OK)
    assert len(events) == 1
    assert events[0].candle.instrument_id == "BTC"
    assert events[0].duration_ns == H1_NS
    assert len(events[0].source_ids) == 4


def test_closed_source_must_be_available_by_processing_cut():
    source = bar(0)
    with pytest.raises(ValueError, match="before close"):
        replace(source, available_ns=source.end_ns - 1)
    aggregator = HorizonAggregator("BTC", source_duration_ns=M15_NS,
                                   target_durations_ns=(H1_NS,))
    with pytest.raises(ValueError, match="unavailable"):
        aggregator.advance(source, processing_ns=source.end_ns - 1, quality=QualityState.OK)
    assert aggregator.pending_counts == {H1_NS: 0}
