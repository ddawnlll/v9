"""Causal auxiliary measurements; provider owns explicit validity intervals."""

from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

Metric = Literal["settled_funding_rate", "open_interest", "long_short_ratio"]


@dataclass(frozen=True)
class PositioningReading:
    instrument_id: str
    metric: Metric
    value: Decimal
    event_ns: int
    received_ns: int
    available_ns: int | None
    valid_until_ns: int
    source_hash: str

    def __post_init__(self) -> None:
        if self.metric not in {"settled_funding_rate", "open_interest", "long_short_ratio"}:
            raise ValueError("unsupported positioning metric")
        if not self.value.is_finite() or (self.metric != "settled_funding_rate" and self.value < 0):
            raise ValueError("invalid positioning value")
        if not self.instrument_id or not self.source_hash:
            raise ValueError("missing positioning source identity")
        if (
            self.event_ns < 0
            or self.received_ns < self.event_ns
            or self.valid_until_ns <= self.event_ns
        ):
            raise ValueError("invalid positioning clocks")
        if self.available_ns is not None and self.available_ns < self.received_ns:
            raise ValueError("positioning cannot be known before receipt")


def positioning_at(
    readings: tuple[PositioningReading, ...], instrument: str, metric: Metric, decision_ns: int
) -> Decimal | None:
    known = [
        r
        for r in readings
        if r.instrument_id == instrument
        and r.metric == metric
        and r.available_ns is not None
        and r.available_ns <= decision_ns
        and r.event_ns <= decision_ns
    ]
    by_event: dict[int, PositioningReading] = {}
    for reading in known:
        existing = by_event.get(reading.event_ns)
        if existing is not None:
            if existing.value != reading.value or existing.valid_until_ns != reading.valid_until_ns:
                raise ValueError(
                    "conflicting positioning versions require explicit revision policy"
                )
            # Repeated captures of an unchanged settlement are corroboration,
            # not revisions. Both have already passed decision-time filtering.
            continue
        by_event[reading.event_ns] = reading
    if not by_event:
        return None
    latest = by_event[max(by_event)]
    # Never fall back to an older value when the latest known reading expired.
    return latest.value if decision_ns < latest.valid_until_ns else None
