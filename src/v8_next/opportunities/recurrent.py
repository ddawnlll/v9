"""Recurrent opportunity evaluation state — append-only, PIT, idempotent (M2-01).

An opportunity episode is evaluated many times while it is watched. This module
carries the identity of one episode across those evaluations without letting the
evaluation plane rewrite history:

* **Sovereign identity** — ``opportunity_id`` is minted by the market-facing
  :class:`~v8_next.opportunities.models.OpportunityRecord` (``OpportunityEpisode``
  digest) and is never derived, re-minted or mutated here. An episode keeps one
  id from ``WATCHING`` to its terminal state.
* **Append-only history** — every accepted evaluation appends one immutable
  :class:`RecurrentEvent`; earlier events are never edited or dropped.
* **Point-in-Time only** — an evaluation whose evidence is not available by the
  evaluation clock (``available_at_ns > evaluation_time_ns``) is refused, as is
  any evaluation that moves the clock backwards. Nothing is clamped or guessed.
* **Idempotent per ``(episode, evaluation_time)``** — replaying the same
  evaluation returns the same state and appends nothing. A *different* payload
  at an already-recorded time is a conflict and raises rather than silently
  overwriting the earlier record.
* **Monotone, terminal-absorbing lifecycle** — the non-terminal progression is
  ``WATCHING -> ARMED -> ADMITTED``; ``INVALIDATED``/``EXPIRED`` are absorbing.
  An episode is never resurrected, so invalidation and expiry replay identically.
* **Witness digest** — the union of witness ids is hashed with the repository's
  canonical digest, so the same evidence in a different order digests the same,
  and new evidence changes the digest exactly once.

Vocabulary is imported, never redefined: lifecycle values come from
:class:`v8_next.domain.decision.OpportunityLifecycle` and statuses from
:class:`v8_next.opportunities.models.OpportunityStatus`.
"""

from __future__ import annotations

from typing import Final, Iterable, Sequence

from pydantic import BaseModel, ConfigDict, Field, model_validator

from v8_next.domain.decision import (
    TERMINAL_LIFECYCLES,
    OpportunityLifecycle,
    lifecycle_of,
    opportunity_status_of,
)
from v8_next.opportunities.models import OpportunityRecord, OpportunityStatus
from v8_next.telemetry.identity import canonical_digest

__all__ = [
    "NonPitEvaluationError",
    "RecurrentConflictError",
    "RecurrentEvent",
    "RecurrentLifecycleError",
    "RecurrentOpportunityState",
    "RecurrentStateError",
    "advance",
    "bootstrap",
    "witness_digest",
]

EVENT_TAG: Final[str] = "recurrent-opportunity-event-v1"
WITNESS_TAG: Final[str] = "opportunity-witness-v1"

#: Non-terminal progression rank. Terminal lifecycles are absent on purpose:
#: they are absorbable from anywhere and never lead anywhere else.
_PROGRESSION: Final[dict[OpportunityLifecycle, int]] = {
    OpportunityLifecycle.WATCHING: 0,
    OpportunityLifecycle.ARMED: 1,
    OpportunityLifecycle.ADMITTED: 2,
}


class RecurrentStateError(ValueError):
    """Base class for refused recurrent-evaluation operations."""

    code: str = "RECURRENT_STATE_ERROR"


class NonPitEvaluationError(RecurrentStateError):
    """Evidence is not available at the evaluation clock, or the clock receded."""

    code = "NON_PIT_EVALUATION"


class RecurrentConflictError(RecurrentStateError):
    """A second, different payload was submitted for an already-recorded time."""

    code = "RECURRENT_CONFLICT"


class RecurrentLifecycleError(RecurrentStateError):
    """An evaluation asked for a lifecycle transition the episode cannot make."""

    code = "ILLEGAL_LIFECYCLE_TRANSITION"


def _canonical_witnesses(witness_ids: Iterable[str]) -> tuple[str, ...]:
    """Deduplicate and canonically order witness ids; blank ids are refused."""
    canonical = sorted(set(witness_ids))
    for witness_id in canonical:
        if not witness_id.strip():
            raise RecurrentStateError("witness id cannot be blank")
    return tuple(canonical)


def witness_digest(witness_ids: Iterable[str]) -> str:
    """Order-independent digest of a witness set (the empty set included)."""
    return canonical_digest(WITNESS_TAG, list(_canonical_witnesses(witness_ids)))


def _derive_event_id(
    opportunity_id: str,
    evaluation_time_ns: int,
    status: OpportunityStatus,
    witness_ids: tuple[str, ...],
    reason: str | None,
) -> str:
    return canonical_digest(
        EVENT_TAG,
        [
            opportunity_id,
            evaluation_time_ns,
            status.value,
            list(witness_ids),
            reason,
        ],
    )


class RecurrentEvent(BaseModel):
    """One immutable evaluation of an episode at one PIT clock reading."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    event_id: str = Field(min_length=1)
    evaluation_time_ns: int = Field(ge=0)
    status: OpportunityStatus
    witness_ids: tuple[str, ...] = ()
    reason: str | None = None

    @model_validator(mode="after")
    def _canonical(self) -> RecurrentEvent:
        if self.reason is not None and not self.reason.strip():
            raise ValueError("reason cannot be blank")
        canonical = _canonical_witnesses(self.witness_ids)
        if canonical != self.witness_ids:
            raise ValueError("witness_ids must be sorted and unique")
        return self


class RecurrentOpportunityState(BaseModel):
    """Replayable recurrent state of one opportunity episode."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    opportunity_id: str = Field(min_length=1)
    lifecycle: OpportunityLifecycle
    history: tuple[RecurrentEvent, ...] = ()
    last_evaluation_ns: int | None = Field(default=None, ge=0)
    witness_digest: str = Field(min_length=1)

    @model_validator(mode="after")
    def _consistent_history(self) -> RecurrentOpportunityState:
        previous: int | None = None
        for event in self.history:
            if previous is not None and event.evaluation_time_ns <= previous:
                raise ValueError("history must be strictly ordered by evaluation time")
            previous = event.evaluation_time_ns
        if not self.history:
            if self.last_evaluation_ns is not None:
                raise ValueError("last_evaluation_ns requires at least one event")
        else:
            last = self.history[-1]
            if self.last_evaluation_ns != last.evaluation_time_ns:
                raise ValueError("last_evaluation_ns must match the newest event")
            if lifecycle_of(last.status) is not self.lifecycle:
                raise ValueError("lifecycle must match the newest event status")
        expected = witness_digest(
            witness for event in self.history for witness in event.witness_ids
        )
        if expected != self.witness_digest:
            raise ValueError("witness_digest does not match the recorded witnesses")
        return self


def bootstrap(
    opportunity: OpportunityRecord,
    *,
    witness_ids: Sequence[str] = (),
) -> RecurrentOpportunityState:
    """Start recurrent tracking from the market-created episode record.

    The first event is stamped with the record's own ``as_of_time_ns``: that is
    the instant the market created the opportunity, and the only PIT claim the
    record itself makes.
    """
    witnesses = _canonical_witnesses(witness_ids)
    event = RecurrentEvent(
        event_id=_derive_event_id(
            opportunity.opportunity_id,
            opportunity.as_of_time_ns,
            opportunity.status,
            witnesses,
            "OPPORTUNITY_OBSERVED",
        ),
        evaluation_time_ns=opportunity.as_of_time_ns,
        status=opportunity.status,
        witness_ids=witnesses,
        reason="OPPORTUNITY_OBSERVED",
    )
    return RecurrentOpportunityState(
        opportunity_id=opportunity.opportunity_id,
        lifecycle=lifecycle_of(opportunity.status),
        history=(event,),
        last_evaluation_ns=opportunity.as_of_time_ns,
        witness_digest=witness_digest(witnesses),
    )


def advance(
    state: RecurrentOpportunityState,
    *,
    evaluation_time_ns: int,
    status: OpportunityStatus | OpportunityLifecycle,
    witness_ids: Sequence[str] = (),
    reason: str | None = None,
    available_at_ns: int | None = None,
) -> RecurrentOpportunityState:
    """Append one PIT evaluation, or return ``state`` unchanged on replay.

    Refusals (all typed, all before any state is built):

    * negative clocks, or ``available_at_ns`` after the evaluation clock
      (:class:`NonPitEvaluationError`);
    * an evaluation clock earlier than the newest recorded one
      (:class:`NonPitEvaluationError` — a replay may not rewrite history);
    * a different payload at an already-recorded instant
      (:class:`RecurrentConflictError`);
    * a lifecycle move that is not forward progression or terminal entry
      (:class:`RecurrentLifecycleError`).

    Idempotence: calling twice with the same ``(evaluation_time_ns, status,
    witnesses, reason)`` returns the same state object and appends nothing.
    """
    target = status if isinstance(status, OpportunityLifecycle) else lifecycle_of(status)
    if state.last_evaluation_ns is None:
        # Cannot happen for a bootstrapped state, but a caller-built state with
        # no history must not be advanced silently around its lifecycle.
        raise RecurrentStateError("state has no recorded evaluation to advance from")

    availability = evaluation_time_ns if available_at_ns is None else available_at_ns
    if evaluation_time_ns < 0 or availability < 0:
        raise NonPitEvaluationError("evaluation and availability times must be non-negative")
    if availability > evaluation_time_ns:
        raise NonPitEvaluationError(
            f"evidence available at {availability} is after the evaluation clock "
            f"{evaluation_time_ns}"
        )
    if evaluation_time_ns < state.last_evaluation_ns:
        raise NonPitEvaluationError(
            f"evaluation clock {evaluation_time_ns} precedes the recorded "
            f"{state.last_evaluation_ns}"
        )

    witnesses = _canonical_witnesses(witness_ids)
    last = state.history[-1]
    if evaluation_time_ns == last.evaluation_time_ns:
        if (
            lifecycle_of(last.status) is target
            and last.witness_ids == witnesses
            and last.reason == reason
        ):
            return state
        raise RecurrentConflictError(
            f"episode {state.opportunity_id} already has a different evaluation at "
            f"{evaluation_time_ns}"
        )

    if state.lifecycle in TERMINAL_LIFECYCLES:
        if target is not state.lifecycle:
            raise RecurrentLifecycleError(
                f"episode {state.opportunity_id} is {state.lifecycle.value}; "
                f"cannot become {target.value}"
            )
    else:
        current_rank = _PROGRESSION.get(state.lifecycle)
        target_rank = _PROGRESSION.get(target)
        if target_rank is not None and current_rank is not None:
            if target_rank < current_rank:
                raise RecurrentLifecycleError(
                    f"episode {state.opportunity_id} cannot regress from "
                    f"{state.lifecycle.value} to {target.value}"
                )
        elif target_rank is None and target is not state.lifecycle:
            # Terminal target: entering a terminal state is always allowed.
            pass

    event = RecurrentEvent(
        event_id=_derive_event_id(
            state.opportunity_id,
            evaluation_time_ns,
            opportunity_status_of(target),
            witnesses,
            reason,
        ),
        evaluation_time_ns=evaluation_time_ns,
        status=opportunity_status_of(target),
        witness_ids=witnesses,
        reason=reason,
    )
    union = (
        _canonical_witnesses(
            witness for recorded in state.history for witness in recorded.witness_ids
        )
        + witnesses
    )
    return RecurrentOpportunityState(
        opportunity_id=state.opportunity_id,
        lifecycle=target,
        history=state.history + (event,),
        last_evaluation_ns=evaluation_time_ns,
        witness_digest=witness_digest(union),
    )
