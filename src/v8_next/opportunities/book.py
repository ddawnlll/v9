"""Point-in-Time Canonical Opportunity Book (Rust OpportunityBook parity).

Epistemic Invariant:
    Market creates the Opportunity first; Observers witness and attach evidence subsequently.
    Maintains active, expired, and invalidated episodes deterministically across time and price.

Recurrent evaluation (M2-01) adds three append-only surfaces beside the existing
lifecycle mutators, without changing their semantics:

* :class:`StatusEvent` — one immutable record per status transition with both the
  fact time (``event_time_ns``) and the knowability time (``available_at_ns``).
  Events are append-only and must be non-decreasing in event time per episode;
  late evidence changes availability, never facts already recorded.
* :meth:`OpportunityBook.snapshot` — the book as it was *knowable* at a cut.
  A status change whose evidence became available after the cut is invisible,
  even though the live book already holds it.
* :meth:`OpportunityBook.archive` — terminal episodes (``INVALIDATED``/``EXPIRED``)
  with the terminal event id and every decision id linked to the episode, so an
  archived episode traces to its decision receipts.

Witnesses are evidence identities: the same witness cannot create a second
opportunity, and repeated insertion of one witness id neither appends events nor
commits capital twice. Episode ids remain the market-created
``OpportunityRecord.opportunity_id`` digests; nothing here re-mints them.
"""

from __future__ import annotations

from typing import Final, Iterable, Sequence

from pydantic import BaseModel, ConfigDict, Field, model_validator

from v8_next.domain.market import Candle
from v8_next.opportunities.models import (
    ExposureDirection,
    OpportunityRecord,
    OpportunityStatus,
)
from v8_next.telemetry.identity import canonical_digest

__all__ = [
    "ArchivedEpisode",
    "DecisionLink",
    "EpisodeSnapshot",
    "OpportunityBook",
    "OpportunitySnapshot",
    "StatusEvent",
    "StatusEventError",
    "canonical_witness_ids",
]

STATUS_EVENT_TAG: Final[str] = "opportunity-status-event-v1"
DECISION_LINK_TAG: Final[str] = "opportunity-decision-link-v1"
SNAPSHOT_TAG: Final[str] = "opportunity-snapshot-v1"

#: Statuses that end an episode's active life.
TERMINAL_STATUSES: Final[frozenset[OpportunityStatus]] = frozenset(
    {OpportunityStatus.INVALIDATED, OpportunityStatus.EXPIRED}
)


class StatusEventError(ValueError):
    """A status event is not a valid or monotone PIT record."""

    code = "INVALID_STATUS_EVENT"


def canonical_witness_ids(witness_ids: Iterable[str]) -> tuple[str, ...]:
    """Deduplicate and canonically order witness ids; blank ids fail closed."""
    canonical = tuple(sorted(set(witness_ids)))
    for witness_id in canonical:
        if not witness_id.strip():
            raise StatusEventError("witness id cannot be blank")
    return canonical


class StatusEvent(BaseModel):
    """One append-only, fully PIT-annotated status transition."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    event_id: str = Field(min_length=1)
    opportunity_id: str = Field(min_length=1)
    status: OpportunityStatus
    event_time_ns: int = Field(ge=0)
    available_at_ns: int = Field(ge=0)
    witness_ids: tuple[str, ...] = ()
    reason: str | None = None
    sequence: int = Field(ge=0)

    @model_validator(mode="after")
    def _pit_consistent(self) -> StatusEvent:
        if self.available_at_ns < self.event_time_ns:
            raise ValueError("availability cannot precede the event")
        if self.reason is not None and not self.reason.strip():
            raise ValueError("reason cannot be blank")
        if canonical_witness_ids(self.witness_ids) != self.witness_ids:
            raise ValueError("witness_ids must be sorted and unique")
        return self

    @classmethod
    def create(
        cls,
        *,
        opportunity_id: str,
        status: OpportunityStatus,
        event_time_ns: int,
        available_at_ns: int,
        witness_ids: Sequence[str] = (),
        reason: str | None = None,
        sequence: int,
    ) -> StatusEvent:
        witnesses = canonical_witness_ids(witness_ids)
        event_id = canonical_digest(
            STATUS_EVENT_TAG,
            [
                opportunity_id,
                status.value,
                event_time_ns,
                available_at_ns,
                list(witnesses),
                reason,
                sequence,
            ],
        )
        return cls(
            event_id=event_id,
            opportunity_id=opportunity_id,
            status=status,
            event_time_ns=event_time_ns,
            available_at_ns=available_at_ns,
            witness_ids=witnesses,
            reason=reason,
            sequence=sequence,
        )


class DecisionLink(BaseModel):
    """A decision receipt attached to an episode, itself PIT annotated."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    link_id: str = Field(min_length=1)
    opportunity_id: str = Field(min_length=1)
    decision_id: str = Field(min_length=1)
    event_time_ns: int = Field(ge=0)
    available_at_ns: int = Field(ge=0)

    @model_validator(mode="after")
    def _pit_consistent(self) -> DecisionLink:
        if self.available_at_ns < self.event_time_ns:
            raise ValueError("availability cannot precede the event")
        return self

    @classmethod
    def create(
        cls,
        *,
        opportunity_id: str,
        decision_id: str,
        event_time_ns: int,
        available_at_ns: int,
        sequence: int,
    ) -> DecisionLink:
        return cls(
            link_id=canonical_digest(
                DECISION_LINK_TAG,
                [
                    opportunity_id,
                    decision_id,
                    event_time_ns,
                    available_at_ns,
                    sequence,
                ],
            ),
            opportunity_id=opportunity_id,
            decision_id=decision_id,
            event_time_ns=event_time_ns,
            available_at_ns=available_at_ns,
        )


class EpisodeSnapshot(BaseModel):
    """One episode's as-of view: status at the cut plus versioned references."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    opportunity: OpportunityRecord
    status: OpportunityStatus
    event_ids: tuple[str, ...]
    witness_ids: tuple[str, ...]
    decision_ids: tuple[str, ...]


class OpportunitySnapshot(BaseModel):
    """Immutable book view that exposes only what was knowable at ``as_of_ns``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    snapshot_id: str = Field(min_length=1)
    as_of_ns: int = Field(ge=0)
    episodes: tuple[EpisodeSnapshot, ...]

    def get(self, opportunity_id: str) -> EpisodeSnapshot | None:
        for episode in self.episodes:
            if episode.opportunity.opportunity_id == opportunity_id:
                return episode
        return None

    def statuses(self) -> dict[str, OpportunityStatus]:
        return {ep.opportunity.opportunity_id: ep.status for ep in self.episodes}


class ArchivedEpisode(BaseModel):
    """A terminal episode with the receipts needed to audit it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    opportunity_id: str
    status: OpportunityStatus
    as_of_time_ns: int
    valid_until_ns: int
    terminal_event_id: str
    event_ids: tuple[str, ...]
    decision_ids: tuple[str, ...]
    snapshot_id: str


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
        #: Append-only status history (M2-01).
        self._events: list[StatusEvent] = []
        self._events_by_opportunity: dict[str, list[int]] = {}
        #: Append-only decision receipts (M2-01).
        self._links: list[DecisionLink] = []
        self._links_by_opportunity: dict[str, list[int]] = {}
        #: Witness identity -> canonical episode id. One witness may not mint
        #: or witness two episodes.
        self._witness_index: dict[str, str] = {}
        self._clock_ns: int = 0

    # ------------------------------------------------------------------ insert

    def insert(
        self,
        opportunity: OpportunityRecord,
        *,
        witness_ids: Sequence[str] = (),
    ) -> OpportunityRecord:
        """Idempotently insert an opportunity episode into the book.

        Insertion is idempotent on the episode id and on every witness id: a
        duplicate witness returns the episode it already witnessed instead of
        minting a second opportunity. A witness already bound to a *different*
        episode proves this insertion is the same witnessed episode, so that
        canonical episode is returned and the new record is discarded.
        """
        witnesses = canonical_witness_ids(witness_ids)
        existing_idx = self._by_id.get(opportunity.opportunity_id)
        if existing_idx is not None:
            episode = self._episodes[existing_idx]
            self._bind_witnesses(episode.opportunity_id, witnesses)
            return episode
        for witness in witnesses:
            prior = self._witness_index.get(witness)
            if prior is not None:
                episode = self._episodes[self._by_id[prior]]
                self._bind_witnesses(episode.opportunity_id, witnesses)
                return episode
        idx = len(self._episodes)
        self._by_id[opportunity.opportunity_id] = idx
        self._episodes.append(opportunity)
        if opportunity.status in self._TRANSITION_STATUSES:
            self._active_positions[idx] = len(
                self._active_by_instrument.setdefault(opportunity.instrument_id, [])
            )
            self._active_by_instrument[opportunity.instrument_id].append(idx)
        self._append_status_event(
            opportunity.opportunity_id,
            opportunity.status,
            event_time_ns=opportunity.as_of_time_ns,
            available_at_ns=opportunity.as_of_time_ns,
            witness_ids=witnesses,
            reason="OPPORTUNITY_OBSERVED",
        )
        self._bind_witnesses(opportunity.opportunity_id, witnesses)
        self._clock_ns = max(self._clock_ns, opportunity.as_of_time_ns)
        return opportunity

    def _bind_witnesses(self, opportunity_id: str, witnesses: Sequence[str]) -> None:
        for witness in witnesses:
            self._witness_index.setdefault(witness, opportunity_id)

    # ---------------------------------------------------------------- lifecycle

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

    def update_status(
        self,
        opportunity_id: str,
        new_status: OpportunityStatus,
        *,
        event_time_ns: int | None = None,
        available_at_ns: int | None = None,
        witness_ids: Sequence[str] = (),
        reason: str | None = None,
    ) -> bool:
        """Mutate the live status and append the matching PIT status event.

        ``event_time_ns`` defaults to the book clock (last candle/insert); the
        default is deterministic. ``available_at_ns`` defaults to the event time.
        Recording an event that precedes the episode's newest event is refused:
        replay may add knowledge later, never rewrite chronology.
        """
        idx = self._by_id.get(opportunity_id)
        if idx is None:
            return False
        current = self._episodes[idx]
        witnesses = canonical_witness_ids(witness_ids)
        if current.status != new_status:
            event_time = (
                max(self._clock_ns, current.as_of_time_ns)
                if event_time_ns is None
                else event_time_ns
            )
            available_at = event_time if available_at_ns is None else available_at_ns
            # Build the event first: an invalid PIT record must not half-apply.
            self._append_status_event(
                opportunity_id,
                new_status,
                event_time_ns=event_time,
                available_at_ns=available_at,
                witness_ids=witnesses,
                reason=reason,
            )
            self._clock_ns = max(self._clock_ns, event_time)
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
            self._bind_witnesses(opportunity_id, witnesses)
            return True
        self._bind_witnesses(opportunity_id, witnesses)
        return False

    def record_status(
        self,
        opportunity_id: str,
        new_status: OpportunityStatus,
        *,
        event_time_ns: int,
        available_at_ns: int | None = None,
        witness_ids: Sequence[str] = (),
        reason: str | None = None,
    ) -> bool:
        """Explicit-clock alias of :meth:`update_status` for PIT replays."""
        return self.update_status(
            opportunity_id,
            new_status,
            event_time_ns=event_time_ns,
            available_at_ns=available_at_ns,
            witness_ids=witness_ids,
            reason=reason,
        )

    def _append_status_event(
        self,
        opportunity_id: str,
        status: OpportunityStatus,
        *,
        event_time_ns: int,
        available_at_ns: int,
        witness_ids: Sequence[str],
        reason: str | None,
    ) -> StatusEvent:
        recorded = self._events_by_opportunity.get(opportunity_id)
        if recorded:
            newest = self._events[recorded[-1]]
            if event_time_ns < newest.event_time_ns:
                raise StatusEventError(
                    f"status event at {event_time_ns} precedes the newest recorded "
                    f"event at {newest.event_time_ns} for {opportunity_id}"
                )
        event = StatusEvent.create(
            opportunity_id=opportunity_id,
            status=status,
            event_time_ns=event_time_ns,
            available_at_ns=available_at_ns,
            witness_ids=witness_ids,
            reason=reason,
            sequence=len(self._events),
        )
        self._events_by_opportunity.setdefault(opportunity_id, []).append(len(self._events))
        self._events.append(event)
        return event

    def status_history(self, opportunity_id: str) -> tuple[StatusEvent, ...]:
        """Full recorded status history (not PIT-filtered) for one episode."""
        return tuple(
            self._events[index] for index in self._events_by_opportunity.get(opportunity_id, ())
        )

    def decision_links(self, opportunity_id: str) -> tuple[DecisionLink, ...]:
        """Full recorded decision receipts (not PIT-filtered) for one episode."""
        return tuple(
            self._links[index] for index in self._links_by_opportunity.get(opportunity_id, ())
        )

    def link_decision(
        self,
        opportunity_id: str,
        decision_id: str,
        *,
        event_time_ns: int,
        available_at_ns: int | None = None,
    ) -> bool:
        """Attach a decision receipt to an episode, idempotently.

        The pair ``(opportunity_id, decision_id)`` is unique, so replaying the
        same receipt cannot inflate the archive. Unknown episodes return
        ``False``; blank ids and non-PIT availability raise.
        """
        if opportunity_id not in self._by_id:
            return False
        if not decision_id.strip():
            raise ValueError("decision_id cannot be blank")
        if event_time_ns < 0:
            raise ValueError("event_time_ns must be non-negative")
        availability = event_time_ns if available_at_ns is None else available_at_ns
        if availability < event_time_ns:
            raise ValueError("availability cannot precede the decision")
        existing = self._links_by_opportunity.get(opportunity_id, ())
        if any(self._links[index].decision_id == decision_id for index in existing):
            return True
        link = DecisionLink.create(
            opportunity_id=opportunity_id,
            decision_id=decision_id,
            event_time_ns=event_time_ns,
            available_at_ns=availability,
            sequence=len(self._links),
        )
        self._links_by_opportunity.setdefault(opportunity_id, []).append(len(self._links))
        self._links.append(link)
        self._clock_ns = max(self._clock_ns, event_time_ns)
        return True

    # ---------------------------------------------------------------- on candle

    def on_candle(self, candle: Candle) -> list[OpportunityRecord]:
        """Advance the book given a newly closed Candle.

        Transitions active opportunities:
        1. Price invalidation: if high/low breach stop_price -> INVALIDATED.
        2. Time expiration: if candle.end_ns > valid_until_ns -> EXPIRED.

        Every transition appends a status event stamped ``candle.end_ns`` with
        availability equal to the same instant: a closed candle is knowable when
        it closes.

        Returns list of newly invalidated or expired opportunities on this tick.
        """
        transitioned: list[OpportunityRecord] = []
        self._clock_ns = max(self._clock_ns, candle.end_ns)
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
                self._append_status_event(
                    ep.opportunity_id,
                    OpportunityStatus.INVALIDATED,
                    event_time_ns=candle.end_ns,
                    available_at_ns=candle.end_ns,
                    witness_ids=(),
                    reason="PRICE_INVALIDATION",
                )
                new_ep = ep.with_status(OpportunityStatus.INVALIDATED)
                self._episodes[idx] = new_ep
                self._deactivate(idx)
                transitioned.append(new_ep)
                continue

            # Check time-to-live expiration
            if candle.end_ns > ep.valid_until_ns:
                self._append_status_event(
                    ep.opportunity_id,
                    OpportunityStatus.EXPIRED,
                    event_time_ns=candle.end_ns,
                    available_at_ns=candle.end_ns,
                    witness_ids=(),
                    reason="TIME_EXPIRY",
                )
                new_ep = ep.with_status(OpportunityStatus.EXPIRED)
                self._episodes[idx] = new_ep
                self._deactivate(idx)
                transitioned.append(new_ep)

        return transitioned

    # ------------------------------------------------------------------ queries

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
                    idx for active in self._active_by_instrument.values() for idx in active
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

    def snapshot(self, as_of_ns: int) -> OpportunitySnapshot:
        """Return the book exactly as it was knowable at ``as_of_ns``.

        An episode is present only if some status event about it was available
        by the cut. Its status is the newest event (by fact time, then append
        order) with both ``event_time_ns`` and ``available_at_ns`` at or before
        the cut, so later knowledge never leaks backwards.
        """
        if as_of_ns < 0:
            raise ValueError("as_of_ns must be non-negative")
        episodes: list[EpisodeSnapshot] = []
        for episode in self._episodes:
            recorded = self._events_by_opportunity.get(episode.opportunity_id, ())
            known = [
                self._events[event_idx]
                for event_idx in recorded
                if self._events[event_idx].event_time_ns <= as_of_ns
                and self._events[event_idx].available_at_ns <= as_of_ns
            ]
            if not known:
                continue
            latest = max(known, key=lambda event: (event.event_time_ns, event.sequence))
            episodes.append(
                EpisodeSnapshot(
                    opportunity=episode.with_status(latest.status),
                    status=latest.status,
                    event_ids=tuple(event.event_id for event in known),
                    witness_ids=canonical_witness_ids(
                        witness for event in known for witness in event.witness_ids
                    ),
                    decision_ids=tuple(
                        link.decision_id
                        for link in self.decision_links(episode.opportunity_id)
                        if link.event_time_ns <= as_of_ns and link.available_at_ns <= as_of_ns
                    ),
                )
            )
        snapshot_id = canonical_digest(
            SNAPSHOT_TAG,
            [
                as_of_ns,
                [
                    [
                        snapshot.opportunity.opportunity_id,
                        snapshot.status.value,
                        list(snapshot.event_ids),
                        list(snapshot.decision_ids),
                    ]
                    for snapshot in episodes
                ],
            ],
        )
        return OpportunitySnapshot(
            snapshot_id=snapshot_id, as_of_ns=as_of_ns, episodes=tuple(episodes)
        )

    def archive(self, as_of_ns: int | None = None) -> tuple[ArchivedEpisode, ...]:
        """Terminal episodes at the cut, each traceable to its decision receipts."""
        cut = self._clock_ns if as_of_ns is None else as_of_ns
        snapshot = self.snapshot(cut)
        archived: list[ArchivedEpisode] = []
        for episode in snapshot.episodes:
            if episode.status not in TERMINAL_STATUSES:
                continue
            archived.append(
                ArchivedEpisode(
                    opportunity_id=episode.opportunity.opportunity_id,
                    status=episode.status,
                    as_of_time_ns=episode.opportunity.as_of_time_ns,
                    valid_until_ns=episode.opportunity.valid_until_ns,
                    terminal_event_id=episode.event_ids[-1],
                    event_ids=episode.event_ids,
                    decision_ids=episode.decision_ids,
                    snapshot_id=snapshot.snapshot_id,
                )
            )
        return tuple(archived)

    def all(self) -> list[OpportunityRecord]:
        return list(self._episodes)

    def __len__(self) -> int:
        return len(self._episodes)

    def is_empty(self) -> bool:
        return len(self._episodes) == 0
