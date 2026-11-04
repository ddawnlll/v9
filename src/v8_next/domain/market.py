"""Immutable causal inputs; observation time is never historical availability."""

from dataclasses import dataclass, field, replace
from decimal import Decimal
from functools import cached_property

import polars as pl


@dataclass(frozen=True, slots=True)
class Candle:
    instrument_id: str
    start_ns: int
    end_ns: int
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    received_ns: int
    available_ns: int | None
    source_hash: str

    def __post_init__(self) -> None:
        values = (self.open, self.high, self.low, self.close, self.volume)
        if any(not value.is_finite() for value in values):
            raise ValueError("non-finite candle")
        if min(values[:4]) <= 0 or self.volume < 0:
            raise ValueError("invalid price or volume")
        if not self.low <= min(self.open, self.close) <= max(self.open, self.close) <= self.high:
            raise ValueError("invalid OHLC ordering")
        if self.start_ns >= self.end_ns:
            raise ValueError("invalid bar interval")
        if self.available_ns is not None and self.available_ns < self.end_ns:
            raise ValueError("completed candle cannot be available before close")


def build_candle_dataframe(candles: tuple[Candle, ...]) -> pl.DataFrame:
    """Build the canonical numeric table for a known candle sequence."""
    if not candles:
        return pl.DataFrame(
            schema={
                "start_ns": pl.Int64,
                "end_ns": pl.Int64,
                "open": pl.Float64,
                "high": pl.Float64,
                "low": pl.Float64,
                "close": pl.Float64,
                "volume": pl.Float64,
            }
        )
    return pl.DataFrame(
        {
            "start_ns": [c.start_ns for c in candles],
            "end_ns": [c.end_ns for c in candles],
            "open": [float(c.open) for c in candles],
            "high": [float(c.high) for c in candles],
            "low": [float(c.low) for c in candles],
            "close": [float(c.close) for c in candles],
            "volume": [float(c.volume) for c in candles],
        },
        schema={
            "start_ns": pl.Int64,
            "end_ns": pl.Int64,
            "open": pl.Float64,
            "high": pl.Float64,
            "low": pl.Float64,
            "close": pl.Float64,
            "volume": pl.Float64,
        },
    )


@dataclass(frozen=True)
class CausalFrame:
    instrument_id: str
    decision_ns: int
    candles: tuple[Candle, ...]
    _prefix_df: pl.DataFrame | None = field(default=None, repr=False, compare=False)
    _trusted_order: bool = field(default=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._trusted_order:
            if self.candles:
                last = self.candles[-1]
                if last.instrument_id != self.instrument_id:
                    raise ValueError("mixed instruments")
                if last.available_ns is None or last.available_ns > self.decision_ns:
                    raise ValueError("unavailable input")
            return
        previous_end = -1
        for candle in self.candles:
            if candle.instrument_id != self.instrument_id:
                raise ValueError("mixed instruments")
            if candle.available_ns is None or candle.available_ns > self.decision_ns:
                raise ValueError("unavailable input")
            if candle.start_ns < previous_end:
                raise ValueError("duplicate or overlapping candles")
            previous_end = candle.end_ns

    @cached_property
    def continuous(self) -> bool:
        return all(
            a.end_ns == b.start_ns for a, b in zip(self.candles, self.candles[1:], strict=False)
        )

    @cached_property
    def df(self) -> pl.DataFrame:
        if self._prefix_df is not None:
            return self._prefix_df.head(len(self.candles))
        if not self.candles:
            return pl.DataFrame(
                schema={
                    "start_ns": pl.Int64,
                    "end_ns": pl.Int64,
                    "open": pl.Float64,
                    "high": pl.Float64,
                    "low": pl.Float64,
                    "close": pl.Float64,
                    "volume": pl.Float64,
                }
            )
        return pl.DataFrame(
            {
                "start_ns": [c.start_ns for c in self.candles],
                "end_ns": [c.end_ns for c in self.candles],
                "open": [float(c.open) for c in self.candles],
                "high": [float(c.high) for c in self.candles],
                "low": [float(c.low) for c in self.candles],
                "close": [float(c.close) for c in self.candles],
                "volume": [float(c.volume) for c in self.candles],
            },
            schema={
                "start_ns": pl.Int64,
                "end_ns": pl.Int64,
                "open": pl.Float64,
                "high": pl.Float64,
                "low": pl.Float64,
                "close": pl.Float64,
                "volume": pl.Float64,
            },
        )

    def lazy(self) -> pl.LazyFrame:
        return self.df.lazy()

    @classmethod
    def from_ordered_prefix(
        cls,
        instrument_id: str,
        decision_ns: int,
        candles: tuple[Candle, ...],
        full_df: pl.DataFrame | None = None,
    ) -> "CausalFrame":
        """Validated prefix frame reusing a caller-owned numeric cache.

        `full_df` is a pure numeric cache: every candle is still validated
        (instrument, availability, ordering) and, when a cache is supplied,
        its head rows must exactly match the candle sequence (start/end ns).
        Misalignment raises instead of silently serving wrong rows. Cost is
        O(n) per call; callers needing per-bar speed must route through a
        positional guard and fall back to frame_at on any deviation.
        """
        frame = cls(instrument_id, decision_ns, candles)
        if full_df is not None:
            if len(full_df) < len(candles):
                raise ValueError("prefix cache shorter than candle sequence")
            head = full_df.head(len(candles))
            if (
                head["start_ns"].to_list() != [c.start_ns for c in candles]
                or head["end_ns"].to_list() != [c.end_ns for c in candles]
            ):
                raise ValueError("prefix cache misaligned with candle sequence")
            frame = replace(frame, _prefix_df=full_df, _trusted_order=True)
        return frame


def frame_at(instrument_id: str, decision_ns: int, candles: tuple[Candle, ...]) -> CausalFrame:
    """Select admissible versions; duplicates must agree, never silently overwrite."""
    unique: dict[int, Candle] = {}
    for candle in candles:
        if candle.instrument_id != instrument_id:
            continue
        if candle.available_ns is None or candle.available_ns > decision_ns:
            continue
        previous = unique.get(candle.start_ns)
        if previous is not None and previous != candle:
            raise ValueError("conflicting bar versions require explicit revision policy")
        unique[candle.start_ns] = candle
    return CausalFrame(instrument_id, decision_ns, tuple(unique[t] for t in sorted(unique)))


def frame_at_incremental(
    parent: CausalFrame | None,
    instrument_id: str,
    decision_ns: int,
    prefix: tuple[Candle, ...],
) -> CausalFrame:
    """Validated-incremental frame: trust the validated parent, verify only the tail.

    Identical accept/reject to :func:`frame_at` on every input. Any doubt
    (no parent, instrument change, time going backwards, prefix not extending
    the parent, tail carrying a filtered/duplicate/out-of-order candle)
    falls back to the full :func:`frame_at` path instead of inventing a
    verdict. The fast path therefore never changes PIT/causal guarantees:
    it only skips re-walking a prefix the parent already validated.
    """
    if parent is None:
        return frame_at(instrument_id, decision_ns, prefix)
    if parent.instrument_id != instrument_id:
        return frame_at(instrument_id, decision_ns, prefix)
    if decision_ns < parent.decision_ns:
        return frame_at(instrument_id, decision_ns, prefix)
    if len(prefix) < len(parent.candles):
        return frame_at(instrument_id, decision_ns, prefix)
    if prefix[: len(parent.candles)] != parent.candles:
        return frame_at(instrument_id, decision_ns, prefix)
    tail = prefix[len(parent.candles):]
    if not tail:
        # Same candles, newer decision time: parent availability still holds
        # because decision_ns only moved forward; delegate to full path on
        # any surprise so the verdict stays identical.
        return frame_at(instrument_id, decision_ns, prefix)
    previous_end = parent.candles[-1].end_ns if parent.candles else -1
    # No set of parent starts is built: the parent prefix was already validated
    # in order, and the tail chains off the parent's last end_ns, so any
    # duplicate or overlapping start trips the ordering check below (bar
    # durations are positive, hence a repeated start is always < previous_end).
    # Anything the chained check cannot decide falls back to frame_at.
    for candle in tail:
        # frame_at would filter these out; including them would diverge.
        if candle.instrument_id != instrument_id:
            return frame_at(instrument_id, decision_ns, prefix)
        if candle.available_ns is None or candle.available_ns > decision_ns:
            return frame_at(instrument_id, decision_ns, prefix)
        if candle.start_ns < previous_end:
            return frame_at(instrument_id, decision_ns, prefix)
        previous_end = candle.end_ns
    return CausalFrame(instrument_id, decision_ns, prefix, _trusted_order=True)
