"""Temporal identity types — thin port of v8-core/src/temporal/mod.rs ids (D-139, Rules 44–50).

``BarId``, ``FundingEventId`` and ``DecisionTime`` are disjoint by
construction: distinct frozen classes over an integer payload, so a funding id
can never resolve as a bar id (I3) — no runtime flag, no stringly-typed
channel. Temporal non-interference itself lives in ``domain/market.frame_at``
(the causal frame admits only versions available at the decision time); these
types are the identity layer that keeps bar, funding, and decision channels
from ever sharing a key space.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["BarId", "DecisionTime", "FundingEventId"]


@dataclass(frozen=True, order=True)
class BarId:
    """Zero-based index of a completed bar in the time series."""

    value: int

    def __post_init__(self) -> None:
        if not isinstance(self.value, int) or isinstance(self.value, bool) or self.value < 0:
            raise ValueError("BAR_ID: non-negative int required")

    def __str__(self) -> str:
        return f"BarId({self.value})"


@dataclass(frozen=True, order=True)
class FundingEventId:
    """Identifier of one asynchronous funding / open-interest event."""

    value: int

    def __post_init__(self) -> None:
        if not isinstance(self.value, int) or isinstance(self.value, bool) or self.value < 0:
            raise ValueError("FUNDING_EVENT_ID: non-negative int required")

    def __str__(self) -> str:
        return f"FundingEventId({self.value})"


@dataclass(frozen=True, order=True)
class DecisionTime:
    """Decision-time timestamp (nanoseconds UTC)."""

    value_ns: int

    def __post_init__(self) -> None:
        if not isinstance(self.value_ns, int) or isinstance(self.value_ns, bool):
            raise ValueError("DECISION_TIME: int nanoseconds required")

    def __str__(self) -> str:
        return f"DecisionTime({self.value_ns} ns)"
