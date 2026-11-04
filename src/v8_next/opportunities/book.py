"""Point-in-Time Canonical Opportunity Book (Rust OpportunityBook parity).

Epistemic Invariant:
    Market creates the Opportunity first; Observers witness and attach evidence subsequently.
    Maintains active, expired, and invalidated episodes deterministically across time and price.
"""

from __future__ import annotations

from typing import Sequence

from v8_next.domain.market import Candle
from v8_next.opportunities.models import (
    ExposureDirection,
    OpportunityRecord,
    OpportunityStatus,
)


class OpportunityBook:
    """Canonical Opportunity Book holding Expert-independent opportunity episodes."""

    #: Statuses scanned by the per-candle transition check. Terminal episodes
    #: leave the active index but stay in ``_episodes`` for audit.
    _TRANSITION_STATUSES = (
        OpportunityStatus.CANDIDATE,
        OpportunityStatus.CONFIRMED,
        OpportunityStatus.ADMITTED,
    )

    def __init__(self) -> None:
        self._episodes: list[OpportunityRecord] = []
        self._by_id: dict[str, int] = {}
        #: Active episode indices per instrument, in insertion order. The
        #: per-candle transition check and active queries walk only this
        #: index instead of rescanning terminal episodes every bar.
        self._active_by_instrument: dict[str, list[int]] = {}
        self._active_positions: dict[int, int] = {}

    def insert(self, opportunity: OpportunityRecord) -> None:
        """Idempotently insert an opportunity episode into the book."""
        if opportunity.opportunity_id in self._by_id:
            return
        idx = len(self._episodes)
        self._by_id[opportunity.opportunity_id] = idx
        self._episodes.append(opportunity)
        if opportunity.status in self._TRANSITION_STATUSES:
            self._active_positions[idx] = len(
                self._active_by_instrument.setdefault(opportunity.instrument_id, [])
            )
            self._active_by_instrument[opportunity.instrument_id].append(idx)

    def get(self, opportunity_id: str) -> OpportunityRecord | None:
        idx = self._by_id.get(opportunity_id)
        if idx is None:
            return None
        return self._episodes[idx]

    def _deactivate(self, idx: int) -> None:
        """Remove ``idx`` from the active index; the episode stays for audit."""
        position = self._active_positions.pop(idx, None)
        if position is None:
            return
        episode = self._episodes[idx]
        active = self._active_by_instrument.get(episode.instrument_id)
        if not active:
            return
        # Swap-remove keeps deactivation O(1); on_candle sorts the small
        # active set per instrument, so transition order stays deterministic.
        last = active.pop()
        if last != idx:
            active[position] = last
            self._active_positions[last] = position
        if not active:
            del self._active_by_instrument[episode.instrument_id]

    def update_status(self, opportunity_id: str, new_status: OpportunityStatus) -> bool:
        idx = self._by_id.get(opportunity_id)
        if idx is None:
            return False
        current = self._episodes[idx]
        if current.status != new_status:
            self._episodes[idx] = current.with_status(new_status)
            if new_status not in self._TRANSITION_STATUSES:
                self._deactivate(idx)
            elif idx not in self._active_positions:
                # Re-admission of a terminal episode: restore index
                # membership so the transition check sees it again.
                episode = self._episodes[idx]
                self._active_positions[idx] = len(
                    self._active_by_instrument.setdefault(episode.instrument_id, [])
                )
                self._active_by_instrument[episode.instrument_id].append(idx)
            return True
        return False

    def on_candle(self, candle: Candle) -> list[OpportunityRecord]:
        """Advance the book given a newly closed Candle.

        Transitions active opportunities:
        1. Price invalidation: if high/low breach stop_price -> INVALIDATED.
        2. Time expiration: if candle.end_ns > valid_until_ns -> EXPIRED.

        Returns list of newly invalidated or expired opportunities on this tick.
        """
        transitioned: list[OpportunityRecord] = []
        active = self._active_by_instrument.get(candle.instrument_id)
        if not active:
            return transitioned
        # Insertion order: indices grow monotonically, so sorting restores the
        # exact scan order of the former full-book loop (which ran in index
        # order) after O(1) swap-removes above.
        for idx in sorted(active):
            ep = self._episodes[idx]
            if ep.status not in self._TRANSITION_STATUSES:
                continue

            # Check price invalidation (stop loss breach)
            invalidated = False
            if ep.stop_price is not None:
                if ep.direction == ExposureDirection.LONG:
                    # For LONG, if low touches or goes below stop_price
                    if candle.low <= ep.stop_price:
                        invalidated = True
                elif ep.direction == ExposureDirection.SHORT:
                    # For SHORT, if high touches or exceeds stop_price
                    if candle.high >= ep.stop_price:
                        invalidated = True

            if invalidated:
                new_ep = ep.with_status(OpportunityStatus.INVALIDATED)
                self._episodes[idx] = new_ep
                self._deactivate(idx)
                transitioned.append(new_ep)
                continue

            # Check time-to-live expiration
            if candle.end_ns > ep.valid_until_ns:
                new_ep = ep.with_status(OpportunityStatus.EXPIRED)
                self._episodes[idx] = new_ep
                self._deactivate(idx)
                transitioned.append(new_ep)

        return transitioned

    def active_opportunities(
        self,
        timestamp_ns: int | None = None,
        *,
        instrument_id: str | None = None,
        statuses: Sequence[OpportunityStatus] = (
            OpportunityStatus.CANDIDATE,
            OpportunityStatus.CONFIRMED,
            OpportunityStatus.ADMITTED,
        ),
    ) -> list[OpportunityRecord]:
        """Query all currently valid/active opportunities matching criteria."""
        wanted = set(statuses)
        if wanted <= set(self._TRANSITION_STATUSES):
            if instrument_id is not None:
                indices = sorted(self._active_by_instrument.get(instrument_id, ()))
            else:
                # Global insertion order, as the former full-book scan.
                indices = sorted(
                    idx
                    for active in self._active_by_instrument.values()
                    for idx in active
                )
            active = []
            for idx in indices:
                ep = self._episodes[idx]
                if ep.status not in wanted:
                    continue
                if timestamp_ns is not None:
                    if not (ep.as_of_time_ns <= timestamp_ns <= ep.valid_until_ns):
                        continue
                active.append(ep)
            return active
        active = []
        for ep in self._episodes:
            if instrument_id is not None and ep.instrument_id != instrument_id:
                continue
            if ep.status not in statuses:
                continue
            if timestamp_ns is not None:
                if not (ep.as_of_time_ns <= timestamp_ns <= ep.valid_until_ns):
                    continue
            active.append(ep)
        return active

    def all(self) -> list[OpportunityRecord]:
        return list(self._episodes)

    def __len__(self) -> int:
        return len(self._episodes)

    def is_empty(self) -> bool:
        return len(self._episodes) == 0
