"""Legal action set and conflict resolution — fail-closed, table-driven.

``LegalActionTable.legal`` answers "which advisory actions are permitted given
what we believe about the position, what the venue last reported, whether the
inputs are stale, and whether an opportunity is armed". The answer is data: a
module-level frozen mapping with one row per ``(position, venue)`` pair plus two
frozen gate sets. No branch of the lookup is allowed to widen the set.

Guarantees, pinned by ``tests/test_legal_actions.py`` over the whole table:

* ``VenueOrderStatus.UNKNOWN`` ⇒ only ``WAIT``/``ABSTAIN``.
* ``stale=True`` ⇒ ``ENTER`` never legal.
* no position (``NONE``) ⇒ ``HOLD``/``REDUCE``/``EXIT`` never legal.
* open position (``OPEN``) ⇒ ``ENTER`` never legal (scale-in is not enabled).
* ``UNCERTAIN`` position ⇒ only ``WAIT``/``ABSTAIN``.

``resolve_conflict`` is a deterministic precedence order over candidates, never
a vote: duplicates collapse and candidate order is irrelevant. Precedence is
risk-first — ``EXIT`` > ``REDUCE`` > ``ABSTAIN`` > ``WAIT`` > ``HOLD`` > ``ENTER``
— so a consensus of entries can never outrank an exit, and any single
``ABSTAIN``/``WAIT`` candidate outranks a new entry. Consensus still cannot
override legality: call :func:`resolve_legal_conflict` to intersect candidates
with the legal set first.
"""

from __future__ import annotations

from enum import StrEnum
from types import MappingProxyType
from typing import Final, Mapping, Sequence

from v8_next.domain.decision import DecisionAction, VenueOrderStatus

__all__ = [
    "LEGAL_ACTION_TABLE",
    "PRECEDENCE",
    "LegalActionTable",
    "PositionPresence",
    "resolve_conflict",
    "resolve_legal_conflict",
]


class PositionPresence(StrEnum):
    """What we believe about our own exposure."""

    NONE = "NONE"
    OPEN = "OPEN"
    PENDING_INTENT = "PENDING_INTENT"
    UNCERTAIN = "UNCERTAIN"


#: No action at all: the fail-closed answer whenever the world is unreadable.
_NO_ACTION: Final[frozenset[DecisionAction]] = frozenset(
    {DecisionAction.WAIT, DecisionAction.ABSTAIN}
)
#: Flat and allowed to open risk (entry still gated by staleness and arming).
_FLAT_ENTRY_ALLOWED: Final[frozenset[DecisionAction]] = _NO_ACTION | {DecisionAction.ENTER}
#: Exposure exists; every action except opening more risk.
_POSITION_MANAGED: Final[frozenset[DecisionAction]] = _NO_ACTION | {
    DecisionAction.HOLD,
    DecisionAction.REDUCE,
    DecisionAction.EXIT,
}

#: Rows are one per ``(position, venue)`` pair. ``UNKNOWN`` is always
#: ``_NO_ACTION``; ``UNCERTAIN`` position is always ``_NO_ACTION``.
LEGAL_ACTION_TABLE: Final[
    Mapping[tuple[PositionPresence, VenueOrderStatus], frozenset[DecisionAction]]
] = MappingProxyType(
    {
        # No position believed.
        (PositionPresence.NONE, VenueOrderStatus.SUBMITTED): _NO_ACTION,
        (PositionPresence.NONE, VenueOrderStatus.ACCEPTED): _NO_ACTION,
        (PositionPresence.NONE, VenueOrderStatus.PARTIALLY_FILLED): _NO_ACTION,
        (PositionPresence.NONE, VenueOrderStatus.FILLED): _NO_ACTION,
        (PositionPresence.NONE, VenueOrderStatus.CANCELED): _FLAT_ENTRY_ALLOWED,
        (PositionPresence.NONE, VenueOrderStatus.REJECTED): _FLAT_ENTRY_ALLOWED,
        (PositionPresence.NONE, VenueOrderStatus.UNKNOWN): _NO_ACTION,
        # Position believed open.
        (PositionPresence.OPEN, VenueOrderStatus.SUBMITTED): _POSITION_MANAGED,
        (PositionPresence.OPEN, VenueOrderStatus.ACCEPTED): _POSITION_MANAGED,
        (PositionPresence.OPEN, VenueOrderStatus.PARTIALLY_FILLED): _POSITION_MANAGED,
        (PositionPresence.OPEN, VenueOrderStatus.FILLED): _POSITION_MANAGED,
        (PositionPresence.OPEN, VenueOrderStatus.CANCELED): _POSITION_MANAGED,
        (PositionPresence.OPEN, VenueOrderStatus.REJECTED): _POSITION_MANAGED,
        (PositionPresence.OPEN, VenueOrderStatus.UNKNOWN): _NO_ACTION,
        # Entry submitted but outcome not yet terminal. A partial fill is real
        # exposure, so it is managed like an open position; a pure submission
        # or acceptance cannot justify a second order, nor a blind exit.
        (PositionPresence.PENDING_INTENT, VenueOrderStatus.SUBMITTED): _NO_ACTION,
        (PositionPresence.PENDING_INTENT, VenueOrderStatus.ACCEPTED): _NO_ACTION,
        (PositionPresence.PENDING_INTENT, VenueOrderStatus.PARTIALLY_FILLED): _POSITION_MANAGED,
        (PositionPresence.PENDING_INTENT, VenueOrderStatus.FILLED): _POSITION_MANAGED,
        (PositionPresence.PENDING_INTENT, VenueOrderStatus.CANCELED): _FLAT_ENTRY_ALLOWED,
        (PositionPresence.PENDING_INTENT, VenueOrderStatus.REJECTED): _FLAT_ENTRY_ALLOWED,
        (PositionPresence.PENDING_INTENT, VenueOrderStatus.UNKNOWN): _NO_ACTION,
        # Position presence unreadable: never act on unreadable exposure.
        (PositionPresence.UNCERTAIN, VenueOrderStatus.SUBMITTED): _NO_ACTION,
        (PositionPresence.UNCERTAIN, VenueOrderStatus.ACCEPTED): _NO_ACTION,
        (PositionPresence.UNCERTAIN, VenueOrderStatus.PARTIALLY_FILLED): _NO_ACTION,
        (PositionPresence.UNCERTAIN, VenueOrderStatus.FILLED): _NO_ACTION,
        (PositionPresence.UNCERTAIN, VenueOrderStatus.CANCELED): _NO_ACTION,
        (PositionPresence.UNCERTAIN, VenueOrderStatus.REJECTED): _NO_ACTION,
        (PositionPresence.UNCERTAIN, VenueOrderStatus.UNKNOWN): _NO_ACTION,
    }
)

#: Gate data, not control flow: actions removed when the inputs are stale.
STALE_FORBIDDEN: Final[frozenset[DecisionAction]] = frozenset({DecisionAction.ENTER})
#: Gate data: actions removed unless an opportunity is explicitly armed.
UNARMED_FORBIDDEN: Final[frozenset[DecisionAction]] = frozenset({DecisionAction.ENTER})

#: Deterministic risk-first precedence; earlier wins. Covers every action once.
PRECEDENCE: Final[tuple[DecisionAction, ...]] = (
    DecisionAction.EXIT,
    DecisionAction.REDUCE,
    DecisionAction.ABSTAIN,
    DecisionAction.WAIT,
    DecisionAction.HOLD,
    DecisionAction.ENTER,
)
_RANK: Final[Mapping[DecisionAction, int]] = MappingProxyType(
    {action: rank for rank, action in enumerate(PRECEDENCE)}
)
if set(_RANK) != set(DecisionAction):  # pragma: no cover - import guard
    raise AssertionError("PRECEDENCE must cover DecisionAction exactly")
if len(LEGAL_ACTION_TABLE) != len(PositionPresence) * len(VenueOrderStatus):
    raise AssertionError("LEGAL_ACTION_TABLE must be total over position x venue")


class LegalActionTable:
    """Namespace over the frozen table; instantiation carries no state."""

    __slots__ = ()

    @staticmethod
    def legal(
        position: PositionPresence,
        *,
        venue: VenueOrderStatus,
        stale: bool,
        armed_opportunity: bool,
    ) -> frozenset[DecisionAction]:
        """Legal actions for one world state.

        Foreign strings are rejected rather than coerced, so a caller cannot
        smuggle an unlisted venue status past the closed enum.
        """
        if not isinstance(position, PositionPresence):
            raise TypeError(f"position must be PositionPresence, got {type(position).__name__}")
        if not isinstance(venue, VenueOrderStatus):
            raise TypeError(f"venue must be VenueOrderStatus, got {type(venue).__name__}")
        if not isinstance(stale, bool) or not isinstance(armed_opportunity, bool):
            raise TypeError("stale and armed_opportunity must be bool")
        actions = LEGAL_ACTION_TABLE[(position, venue)]
        if stale:
            actions = actions - STALE_FORBIDDEN
        if not armed_opportunity:
            actions = actions - UNARMED_FORBIDDEN
        return actions


def resolve_conflict(candidates: Sequence[DecisionAction]) -> DecisionAction:
    """Resolve competing advisory actions to one, deterministically.

    Precedence is :data:`PRECEDENCE`. An empty or duplicate-heavy candidate
    list is fine — duplicates collapse and order is irrelevant — but a list with
    no candidates at all has nothing to resolve and raises.
    """
    seen: set[DecisionAction] = set()
    for candidate in candidates:
        if not isinstance(candidate, DecisionAction):
            raise TypeError(f"candidate must be DecisionAction, got {type(candidate).__name__}")
        seen.add(candidate)
    if not seen:
        raise ValueError("resolve_conflict requires at least one candidate")
    return min(seen, key=_RANK.__getitem__)


def resolve_legal_conflict(
    candidates: Sequence[DecisionAction], *, legal: frozenset[DecisionAction]
) -> DecisionAction:
    """Resolve conflict only among legal actions; otherwise fail closed to ABSTAIN.

    Consensus cannot override legality: candidates that the table forbids are
    dropped before precedence is applied, and if nothing legal remains the
    answer is ``ABSTAIN`` — never a forbidden action.
    """
    permitted = [candidate for candidate in candidates if candidate in legal]
    if permitted:
        return resolve_conflict(permitted)
    return DecisionAction.ABSTAIN
