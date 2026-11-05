"""Bounded, event-driven aggregation of completed, actually observed bars.

This component has no access to orders, clocks or networks. Its caller supplies
one instrument's source bars in continuous event order and their processing cut.
Gaps and out-of-order arrivals fail closed; recovery requires a new instance
and an independently verified replay. No incomplete target window is emitted.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from v8_next.domain.market import Candle
from v8_next.domain.observation import QualityState
from v8_next.telemetry.identity import canonical_digest

AGGREGATION_VERSION = "completed-source-bars-v1"
MINUTE_NS = 60_000_000_000
M15_NS = 15 * MINUTE_NS
H1_NS = 60 * MINUTE_NS
H4_NS = 4 * H1_NS
_ALLOWED_SOURCE_DURATIONS = frozenset({MINUTE_NS, M15_NS, H1_NS})
_ALLOWED_TARGET_DURATIONS = frozenset({M15_NS, H1_NS, H4_NS})


@dataclass(frozen=True, slots=True)
class CompletedHorizonBar:
    """A computed candle and its exact source lineage in event order."""

    duration_ns: int
    candle: Candle
    source_ids: tuple[str, ...]
    aggregation_version: str = AGGREGATION_VERSION


class HorizonAggregator:
    """Incrementally aggregate one source feed without resampling or filling.

    ``advance`` produces only newly completed target bars; source bars remain
    the caller's authoritative source feed. The processor cut is an explicit
    observation time, so an aggregate processed late cannot be backdated to its
    source close. Memory is bounded by the configured target/source ratios.
    Starting mid-window discards that *partial target*, without synthesizing its
    missing prefix. An actual gap after startup raises and leaves state unchanged.
    """

    def __init__(self, instrument_id: str, *, source_duration_ns: int,
                 target_durations_ns: tuple[int, ...]) -> None:
        if not instrument_id.strip():
            raise ValueError("instrument_id cannot be empty")
        if source_duration_ns not in _ALLOWED_SOURCE_DURATIONS:
            raise ValueError("unsupported source duration")
        if (not target_durations_ns or len(set(target_durations_ns)) != len(target_durations_ns)
            or any(target not in _ALLOWED_TARGET_DURATIONS or target <= source_duration_ns
                   or target % source_duration_ns for target in target_durations_ns)):
            raise ValueError("invalid target durations")
        self.instrument_id = instrument_id
        self.source_duration_ns = source_duration_ns
        self.target_durations_ns = tuple(sorted(target_durations_ns))
        self._pending: dict[int, list[Candle]] = {duration: [] for duration in self.target_durations_ns}
        self._last_end_ns: int | None = None
        self._last_processing_ns: int | None = None

    @property
    def pending_counts(self) -> dict[int, int]:
        """Current bounded counts; returns a copy and exposes no mutable state."""
        return {duration: len(bars) for duration, bars in self._pending.items()}

    def advance(self, candle: Candle, *, processing_ns: int,
                quality: QualityState = QualityState.OK) -> tuple[CompletedHorizonBar, ...]:
        """Consume one known, closed source bar and emit completed targets."""
        if isinstance(processing_ns, bool) or not isinstance(processing_ns, int):
            raise ValueError("processing_ns must be an integer")
        if candle.instrument_id != self.instrument_id:
            raise ValueError("instrument mismatch")
        if quality is not QualityState.OK:
            raise ValueError("untrusted source quality")
        if (candle.start_ns < 0 or candle.start_ns % self.source_duration_ns
            or candle.end_ns - candle.start_ns != self.source_duration_ns):
            raise ValueError("source interval is not aligned and complete")
        if (not candle.source_hash or candle.available_ns is None
            or candle.received_ns < candle.start_ns
            or candle.available_ns < candle.received_ns or candle.available_ns > processing_ns
            or candle.received_ns > processing_ns):
            raise ValueError("source unavailable or invalid availability lineage")
        if self._last_processing_ns is not None and processing_ns < self._last_processing_ns:
            raise ValueError("processing time moved backwards")
        if self._last_end_ns is not None and candle.start_ns != self._last_end_ns:
            raise ValueError("source gap, duplicate or out-of-order event")

        # Compute candidate transitions before mutating state. Every emitted
        # interval is epoch-aligned and contains exactly the required sequence.
        next_pending: dict[int, list[Candle]] = {}
        emitted: list[CompletedHorizonBar] = []
        for duration in self.target_durations_ns:
            start = candle.start_ns - candle.start_ns % duration
            previous = self._pending[duration]
            bars = [*previous, candle] if previous and previous[0].start_ns == start else [candle]
            if bars[0].start_ns != start:
                # Startup in a partial window; do not carry an unusable prefix.
                next_pending[duration] = []
                continue
            count = duration // self.source_duration_ns
            if len(bars) == count:
                source_ids = tuple(_source_id(bar) for bar in bars)
                digest = canonical_digest("aggregate-bar", {
                    "version": AGGREGATION_VERSION, "instrument": self.instrument_id,
                    "duration_ns": duration, "source_ids": source_ids,
                    "processing_ns": processing_ns,
                })
                aggregate = Candle(
                    instrument_id=self.instrument_id, start_ns=start, end_ns=start + duration,
                    open=bars[0].open, high=max(b.high for b in bars),
                    low=min(b.low for b in bars), close=bars[-1].close,
                    volume=sum((b.volume for b in bars), Decimal(0)),
                    received_ns=processing_ns, available_ns=processing_ns, source_hash=digest,
                )
                emitted.append(CompletedHorizonBar(duration, aggregate, source_ids))
                next_pending[duration] = []
            else:
                next_pending[duration] = bars
        self._pending = next_pending
        self._last_end_ns = candle.end_ns
        self._last_processing_ns = processing_ns
        return tuple(emitted)


def _source_id(bar: Candle) -> str:
    return canonical_digest("aggregate-source-bar-v1", {
        "instrument": bar.instrument_id, "start_ns": bar.start_ns, "end_ns": bar.end_ns,
        "open": str(bar.open), "high": str(bar.high), "low": str(bar.low),
        "close": str(bar.close), "volume": str(bar.volume),
        "received_ns": bar.received_ns, "available_ns": bar.available_ns,
        "source_hash": bar.source_hash,
    })
