"""Causal multi-horizon context: 15m / 1h / 4h (M1-02).

``assemble_context`` answers one question per timeframe: *which bar could a
decision at ``decision_time_ns`` have seen, and was that bar finished?* The
answer is derived only from the point-in-time views, but the decision time --
not the view cut -- is the gate. A 4h view captured at 21:00 still contains the
16:00-20:00 bar; a decision at 15:40 must not see it. The admissibility rule is
therefore two-sided:

* the bar's ``start_ns + timeframe.duration_ns <= decision_time_ns`` (the
  timeframe has fully elapsed by the decision), and
* the bar's ``end_ns <= decision_time_ns`` and ``available_ns <= decision_time_ns``
  (the value existed and was received before the decision).

The selected bar is the latest admissible one by ``(start_ns, end_ns)``, which
makes off-grid and gapped inputs deterministic. When no bar is admissible the
alignment is explicitly marked with ``as_of_ns = 0``, ``complete = False`` and a
``MISSING`` quality -- a missing horizon is never represented by an invented bar
and never silently omitted from the context.

``complete`` records whether the selected bar spans at least one full timeframe
window. ``quality`` is the worst state among the view's observations available by
the decision time (later quality judgements never leak backwards) combined with:

* ``MISSING`` when no bar is admissible, and
* ``STALE`` when the selected bar is partial or when at least one further full
  timeframe window has elapsed since its close without newer data (a gap or a
  late publication).

The context schema intentionally knows nothing about strategies; later waves
read alignments and decide what to do with them.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, model_validator

from v8_next.domain.observation import (
    ObservationView,
    QualityState,
    StrictIntValue,
    worst_quality,
)

__all__ = [
    "HorizonAlignment",
    "HorizonContext",
    "Timeframe",
    "assemble_context",
]

_NS_PER_SECOND = 1_000_000_000
_NS_PER_MINUTE = 60 * _NS_PER_SECOND
_NS_PER_HOUR = 60 * _NS_PER_MINUTE


def _strict_int(value: object) -> int:
    """Accept exact integers (including ``__index__`` types), reject bool/float/str."""
    if isinstance(value, bool):
        raise ValueError("boolean is not an integer")
    if isinstance(value, int):
        return int(value)
    index = getattr(value, "__index__", None)
    if index is None:
        raise ValueError(f"integer required, got {type(value).__name__}")
    return int(index())


class Timeframe(StrEnum):
    """The three causal horizons carried by a context snapshot."""

    M15 = "M15"
    H1 = "H1"
    H4 = "H4"

    @property
    def duration_ns(self) -> int:
        """Nominal duration of one bar, in nanoseconds."""
        if self is Timeframe.M15:
            return 15 * _NS_PER_MINUTE
        if self is Timeframe.H1:
            return _NS_PER_HOUR
        return 4 * _NS_PER_HOUR


class HorizonAlignment(BaseModel):
    """One timeframe's causal alignment at a decision time."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    timeframe: Timeframe
    #: Close time of the selected bar, or ``0`` when no bar is admissible.
    as_of_ns: StrictIntValue
    #: Whether the selected bar spans at least one full timeframe window.
    complete: bool
    quality: QualityState


class HorizonContext(BaseModel):
    """Immutable per-instrument context for a single decision time."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    instrument_id: str
    decision_time_ns: StrictIntValue
    alignments: dict[Timeframe, HorizonAlignment]

    @model_validator(mode="after")
    def _check_alignments(self) -> Self:
        if not self.instrument_id.strip():
            raise ValueError("instrument_id cannot be empty")
        for timeframe in Timeframe:
            alignment = self.alignments.get(timeframe)
            if alignment is None:
                raise ValueError(f"missing alignment for {timeframe.value}")
            if alignment.timeframe is not timeframe:
                raise ValueError(
                    f"alignment key {timeframe.value} carries {alignment.timeframe.value}"
                )
        return self

    def alignment(self, timeframe: Timeframe) -> HorizonAlignment:
        """Return the alignment for ``timeframe`` (always present)."""
        return self.alignments[timeframe]


def _alignment(
    timeframe: Timeframe,
    view: ObservationView | None,
    instrument_id: str,
    decision_time_ns: int,
) -> HorizonAlignment:
    if view is None:
        return HorizonAlignment(
            timeframe=timeframe,
            as_of_ns=0,
            complete=False,
            quality=QualityState.MISSING,
        )

    duration = timeframe.duration_ns
    admissible = [
        bar
        for bar in view.bars(instrument_id)
        if bar.start_ns + duration <= decision_time_ns
        and bar.end_ns <= decision_time_ns
        and bar.available_ns is not None
        and bar.available_ns <= decision_time_ns
    ]
    quality = view.quality_at(instrument_id, decision_time_ns)
    if not admissible:
        return HorizonAlignment(
            timeframe=timeframe,
            as_of_ns=0,
            complete=False,
            quality=worst_quality(quality, QualityState.MISSING),
        )

    selected = max(admissible, key=lambda bar: (bar.start_ns, bar.end_ns))
    complete = selected.end_ns - selected.start_ns >= duration
    if not complete:
        quality = worst_quality(quality, QualityState.STALE)
    if selected.end_ns + duration <= decision_time_ns:
        quality = worst_quality(quality, QualityState.STALE)
    return HorizonAlignment(
        timeframe=timeframe,
        as_of_ns=selected.end_ns,
        complete=complete,
        quality=quality,
    )


def assemble_context(
    views: Mapping[Timeframe, ObservationView],
    *,
    instrument_id: str,
    decision_time_ns: int,
) -> HorizonContext:
    """Assemble the causal 15m/1h/4h context for ``instrument_id`` at a decision time.

    Every :class:`Timeframe` appears in the result. A timeframe absent from
    ``views`` is explicitly ``MISSING``/``complete=False`` with ``as_of_ns = 0``.
    Bars that had not fully elapsed, had not been received, or were not available
    by ``decision_time_ns`` are never selected, regardless of how late the
    supplying views were captured.
    """
    if not isinstance(instrument_id, str) or not instrument_id.strip():
        raise ValueError("instrument_id must be a non-empty string")
    decision = _strict_int(decision_time_ns)
    for timeframe in views:
        if not isinstance(timeframe, Timeframe):
            raise ValueError(f"unknown timeframe key: {timeframe!r}")

    alignments = {
        timeframe: _alignment(timeframe, views.get(timeframe), instrument_id, decision)
        for timeframe in Timeframe
    }
    return HorizonContext(
        instrument_id=instrument_id,
        decision_time_ns=decision,
        alignments=alignments,
    )
