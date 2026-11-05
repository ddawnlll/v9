"""Causal multi-horizon bar construction (RFC 1.4.1 §4.4, M1).

The decision cut sees a set of *closed* base bars (the H1 tape, for example).
This module answers one question per higher timeframe: **can a full bar be built
from bars that were already complete and available?**

Rules, all fail-closed:

* Aggregation is only ever **upwards** (``H1 -> H4``). A finer timeframe cannot be
  derived from a coarser tape: ``M15`` must come from a real M15 source, never
  from splitting an H1 candle. :func:`build_horizon_series` therefore only emits
  the base timeframe plus strictly coarser ones.
* A target bucket is emitted only when **every** source slot in it is present,
  contiguous and complete. A missing source bar drops the whole bucket; a
  partially covered bucket is never published with a shorter span.
* ``available_ns`` of the aggregate is the **maximum** source ``available_ns``
  and ``received_ns`` the maximum source receipt time. The aggregate is knowable
  no earlier than its slowest input, so a downstream PIT check can never read it
  early.
* Prices and volume are summed/selected with :class:`Decimal`; no float
  round-trip. ``source_hash`` is a digest of the ordered source hashes so an
  aggregate can be traced to the exact bars it came from.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Final, Sequence

from v8_next.domain.horizons import Timeframe
from v8_next.domain.market import Candle
from v8_next.telemetry.identity import canonical_digest

__all__ = [
    "HorizonSeries",
    "aggregate_to_timeframe",
    "build_horizon_series",
    "infer_timeframe",
]

_NS_PER_MINUTE: Final = 60 * 1_000_000_000
_KNOWN_DURATIONS: Final[dict[int, Timeframe]] = {
    timeframe.duration_ns: timeframe for timeframe in Timeframe
}
#: Explicit finer-than-H1 resolution carried only by a real source tape.
_KNOWN_DURATIONS[15 * _NS_PER_MINUTE] = Timeframe.M15


def infer_timeframe(candle: Candle) -> Timeframe | None:
    """The declared horizon a candle belongs to, or ``None`` when non-standard."""
    duration = candle.end_ns - candle.start_ns
    return _KNOWN_DURATIONS.get(duration)


def _bucket_bounds(start_ns: int, duration_ns: int) -> tuple[int, int]:
    start = (start_ns // duration_ns) * duration_ns
    return start, start + duration_ns


def aggregate_to_timeframe(
    source: Sequence[Candle],
    target: Timeframe,
    *,
    source_timeframe: Timeframe | None = None,
) -> tuple[Candle, ...]:
    """Aggregate complete ``source`` bars into ``target`` bars, dropping gaps.

    ``source`` must be ordered by ``start_ns`` and every bar must be complete
    (``end_ns - start_ns == source duration``). A bucket is emitted only when its
    source slots are all present and contiguous. The result carries the maximum
    input availability, never an earlier timestamp.
    """
    if not source:
        return ()
    source_tf = source_timeframe or infer_timeframe(source[0])
    if source_tf is None:
        raise ValueError("source timeframe is not one of M15/H1/H4")
    if target.duration_ns <= source_tf.duration_ns:
        raise ValueError(
            f"cannot aggregate {source_tf.value} into {target.value}: only coarser "
            "timeframes may be derived from a source tape"
        )
    if target.duration_ns % source_tf.duration_ns:
        raise ValueError(f"{target.value} is not an integer multiple of {source_tf.value}")
    if source[0].instrument_id == "":
        raise ValueError("source candles must carry an instrument id")
    instrument_id = source[0].instrument_id

    expected_per_bucket = target.duration_ns // source_tf.duration_ns
    buckets: dict[int, list[Candle]] = {}
    for candle in source:
        if candle.instrument_id != instrument_id:
            raise ValueError("source candles span more than one instrument")
        if candle.end_ns - candle.start_ns != source_tf.duration_ns:
            # A partial source bar cannot contribute to a complete aggregate.
            continue
        if candle.available_ns is None:
            continue
        start, _end = _bucket_bounds(candle.start_ns, target.duration_ns)
        buckets.setdefault(start, []).append(candle)

    out: list[Candle] = []
    for start in sorted(buckets):
        members = sorted(buckets[start], key=lambda c: c.start_ns)
        if len(members) != expected_per_bucket:
            continue
        contiguous = all(
            b.start_ns == a.end_ns for a, b in zip(members, members[1:], strict=False)
        )
        if not contiguous:
            continue
        if members[0].start_ns != start or members[-1].end_ns != start + target.duration_ns:
            continue
        availability = [int(c.available_ns) for c in members if c.available_ns is not None]
        if len(availability) != expected_per_bucket:
            continue
        out.append(
            Candle(
                instrument_id=instrument_id,
                start_ns=start,
                end_ns=start + target.duration_ns,
                open=members[0].open,
                high=max(c.high for c in members),
                low=min(c.low for c in members),
                close=members[-1].close,
                volume=sum((c.volume for c in members), Decimal(0)),
                received_ns=max(int(c.received_ns) for c in members),
                available_ns=max(availability),
                source_hash=canonical_digest(
                    "horizon-aggregate",
                    [target.value, [c.source_hash for c in members]],
                ),
            )
        )
    return tuple(out)


@dataclass(frozen=True, slots=True)
class HorizonSeries:
    """Derived bar series for one instrument, keyed by horizon.

    Only timeframes the source tape can actually support appear here. A horizon
    that would need a finer tape (``M15`` from an ``H1`` base) is absent, which the
    caller must represent as ``MISSING`` context rather than inventing bars.
    """

    instrument_id: str
    base: Timeframe
    series: dict[Timeframe, tuple[Candle, ...]]

    def candles(self, timeframe: Timeframe) -> tuple[Candle, ...]:
        return self.series.get(timeframe, ())

    def available_timeframes(self) -> tuple[Timeframe, ...]:
        return tuple(tf for tf in Timeframe if self.series.get(tf))


def build_horizon_series(
    candles: Sequence[Candle],
    *,
    base: Timeframe | None = None,
) -> HorizonSeries:
    """Build every timeframes derivable from ``candles`` (base + coarser)."""
    if not candles:
        raise ValueError("cannot build a horizon series from no candles")
    base_tf = base or infer_timeframe(candles[0])
    if base_tf is None:
        raise ValueError("base timeframe is not one of M15/H1/H4")
    series: dict[Timeframe, tuple[Candle, ...]] = {base_tf: tuple(candles)}
    for target in Timeframe:
        if target.duration_ns > base_tf.duration_ns:
            aggregated = aggregate_to_timeframe(candles, target, source_timeframe=base_tf)
            if aggregated:
                series[target] = aggregated
    return HorizonSeries(
        instrument_id=candles[0].instrument_id, base=base_tf, series=series
    )
