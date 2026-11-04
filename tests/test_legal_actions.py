"""M2-03: table-driven legal actions and deterministic conflict resolution."""

from __future__ import annotations

import itertools

import pytest

from v8_next.domain.decision import DecisionAction, VenueOrderStatus
from v8_next.economics.legality import (
    LEGAL_ACTION_TABLE,
    PRECEDENCE,
    LegalActionTable,
    PositionPresence,
    resolve_conflict,
    resolve_legal_conflict,
)

WAIT_ONLY = frozenset({DecisionAction.WAIT, DecisionAction.ABSTAIN})
MANAGED = WAIT_ONLY | {DecisionAction.HOLD, DecisionAction.REDUCE, DecisionAction.EXIT}

WORLD_STATES = [
    (position, venue, stale, armed)
    for position in PositionPresence
    for venue in VenueOrderStatus
    for stale in (False, True)
    for armed in (False, True)
]


def legal(
    position: PositionPresence,
    *,
    venue: VenueOrderStatus,
    stale: bool = False,
    armed: bool = True,
) -> frozenset[DecisionAction]:
    return LegalActionTable.legal(position, venue=venue, stale=stale, armed_opportunity=armed)


# ------------------------------------------------------------------------ table shape


def test_table_is_total_and_frozen() -> None:
    assert len(LEGAL_ACTION_TABLE) == len(PositionPresence) * len(VenueOrderStatus)
    assert set(LEGAL_ACTION_TABLE) == set(itertools.product(PositionPresence, VenueOrderStatus))
    with pytest.raises(TypeError):
        LEGAL_ACTION_TABLE[(PositionPresence.NONE, VenueOrderStatus.UNKNOWN)] = WAIT_ONLY  # type: ignore[index]


def test_precedence_covers_every_action_once() -> None:
    assert len(PRECEDENCE) == len(DecisionAction)
    assert set(PRECEDENCE) == set(DecisionAction)
    assert PRECEDENCE == (
        DecisionAction.EXIT,
        DecisionAction.REDUCE,
        DecisionAction.ABSTAIN,
        DecisionAction.WAIT,
        DecisionAction.HOLD,
        DecisionAction.ENTER,
    )


# ------------------------------------------------------------------- fail-closed invariants


@pytest.mark.parametrize(("position", "venue", "stale", "armed"), WORLD_STATES)
def test_unknown_venue_allows_only_wait_or_abstain(
    position: PositionPresence, venue: VenueOrderStatus, stale: bool, armed: bool
) -> None:
    actions = legal(position, venue=venue, stale=stale, armed=armed)
    assert isinstance(actions, frozenset)
    assert actions <= set(DecisionAction)
    if venue is VenueOrderStatus.UNKNOWN:
        assert actions == WAIT_ONLY


@pytest.mark.parametrize(("position", "venue", "stale", "armed"), WORLD_STATES)
def test_stale_inputs_never_enter(
    position: PositionPresence, venue: VenueOrderStatus, stale: bool, armed: bool
) -> None:
    actions = legal(position, venue=venue, stale=stale, armed=armed)
    if stale:
        assert DecisionAction.ENTER not in actions


@pytest.mark.parametrize(("position", "venue", "stale", "armed"), WORLD_STATES)
def test_flat_position_cannot_hold_reduce_or_exit(
    position: PositionPresence, venue: VenueOrderStatus, stale: bool, armed: bool
) -> None:
    actions = legal(position, venue=venue, stale=stale, armed=armed)
    if position is PositionPresence.NONE:
        assert not actions & {DecisionAction.HOLD, DecisionAction.REDUCE, DecisionAction.EXIT}


@pytest.mark.parametrize(("position", "venue", "stale", "armed"), WORLD_STATES)
def test_open_position_cannot_enter(
    position: PositionPresence, venue: VenueOrderStatus, stale: bool, armed: bool
) -> None:
    actions = legal(position, venue=venue, stale=stale, armed=armed)
    if position is PositionPresence.OPEN:
        assert DecisionAction.ENTER not in actions
        assert actions == MANAGED or actions == WAIT_ONLY


@pytest.mark.parametrize(("position", "venue", "stale", "armed"), WORLD_STATES)
def test_uncertain_position_allows_only_wait_or_abstain(
    position: PositionPresence, venue: VenueOrderStatus, stale: bool, armed: bool
) -> None:
    actions = legal(position, venue=venue, stale=stale, armed=armed)
    if position is PositionPresence.UNCERTAIN:
        assert actions == WAIT_ONLY


# --------------------------------------------------------------------------- fixtures


def test_fixture_unknown_venue_state() -> None:
    flat = legal(PositionPresence.NONE, venue=VenueOrderStatus.UNKNOWN)
    opened = legal(PositionPresence.OPEN, venue=VenueOrderStatus.UNKNOWN)
    pending = legal(PositionPresence.PENDING_INTENT, venue=VenueOrderStatus.UNKNOWN)
    uncertain = legal(PositionPresence.UNCERTAIN, venue=VenueOrderStatus.UNKNOWN)
    assert flat == opened == pending == uncertain == WAIT_ONLY
    # Even an armed opportunity cannot re-enable entry while the venue is unreadable.
    assert DecisionAction.ENTER not in legal(
        PositionPresence.NONE, venue=VenueOrderStatus.UNKNOWN, armed=True
    )


def test_fixture_stale_inputs() -> None:
    fresh = legal(PositionPresence.NONE, venue=VenueOrderStatus.CANCELED, stale=False)
    stale = legal(PositionPresence.NONE, venue=VenueOrderStatus.CANCELED, stale=True)
    assert DecisionAction.ENTER in fresh
    assert stale == WAIT_ONLY
    # Staleness never removes risk-reducing actions on an open position.
    assert legal(PositionPresence.OPEN, venue=VenueOrderStatus.ACCEPTED, stale=True) == MANAGED


def test_fixture_open_position() -> None:
    actions = legal(PositionPresence.OPEN, venue=VenueOrderStatus.FILLED)
    assert actions == MANAGED
    assert DecisionAction.ENTER not in actions
    assert {DecisionAction.HOLD, DecisionAction.REDUCE, DecisionAction.EXIT} <= actions


def test_fixture_no_position() -> None:
    assert legal(PositionPresence.NONE, venue=VenueOrderStatus.CANCELED) == (
        WAIT_ONLY | {DecisionAction.ENTER}
    )
    assert legal(PositionPresence.NONE, venue=VenueOrderStatus.REJECTED) == (
        WAIT_ONLY | {DecisionAction.ENTER}
    )


def test_fixture_pending_intent() -> None:
    # In-flight submission: no second order and no blind exit.
    assert legal(PositionPresence.PENDING_INTENT, venue=VenueOrderStatus.SUBMITTED) == WAIT_ONLY
    assert legal(PositionPresence.PENDING_INTENT, venue=VenueOrderStatus.ACCEPTED) == WAIT_ONLY
    # A partial fill is real exposure and is managed like an open position.
    assert (
        legal(PositionPresence.PENDING_INTENT, venue=VenueOrderStatus.PARTIALLY_FILLED) == MANAGED
    )
    # Terminal rejection/cancel returns us to flat, so entry may resume if armed.
    assert legal(PositionPresence.PENDING_INTENT, venue=VenueOrderStatus.CANCELED) == (
        WAIT_ONLY | {DecisionAction.ENTER}
    )


def test_fixture_unarmed_opportunity_blocks_entry() -> None:
    assert DecisionAction.ENTER not in legal(
        PositionPresence.NONE, venue=VenueOrderStatus.CANCELED, armed=False
    )
    assert DecisionAction.ENTER in legal(
        PositionPresence.NONE, venue=VenueOrderStatus.CANCELED, armed=True
    )


def test_fixture_contradictory_flat_claims_fail_closed() -> None:
    # "No position" but the venue reports an order state: unreadable, so wait.
    for venue in (
        VenueOrderStatus.SUBMITTED,
        VenueOrderStatus.ACCEPTED,
        VenueOrderStatus.PARTIALLY_FILLED,
        VenueOrderStatus.FILLED,
    ):
        assert legal(PositionPresence.NONE, venue=venue) == WAIT_ONLY


# ------------------------------------------------------------------ argument hardening


@pytest.mark.parametrize("venue", ["UNKNOWN", "PENDING_NEW", "", None])
def test_foreign_venue_values_are_rejected(venue: object) -> None:
    with pytest.raises((TypeError, ValueError)):
        LegalActionTable.legal(
            PositionPresence.NONE,
            venue=venue,  # type: ignore[arg-type]
            stale=False,
            armed_opportunity=True,
        )


@pytest.mark.parametrize("position", ["NONE", "FLAT", "", None])
def test_foreign_position_values_are_rejected(position: object) -> None:
    with pytest.raises((TypeError, ValueError)):
        LegalActionTable.legal(
            position,  # type: ignore[arg-type]
            venue=VenueOrderStatus.CANCELED,
            stale=False,
            armed_opportunity=True,
        )


def test_non_bool_gates_are_rejected() -> None:
    with pytest.raises(TypeError):
        LegalActionTable.legal(
            PositionPresence.NONE,
            venue=VenueOrderStatus.CANCELED,
            stale="no",  # type: ignore[arg-type]
            armed_opportunity=True,
        )


# -------------------------------------------------------------- deterministic resolution


def test_duplicate_candidates_collapse() -> None:
    assert resolve_conflict([DecisionAction.ENTER, DecisionAction.ENTER]) is DecisionAction.ENTER
    assert (
        resolve_conflict(
            [
                DecisionAction.WAIT,
                DecisionAction.WAIT,
                DecisionAction.WAIT,
                DecisionAction.ENTER,
            ]
        )
        is DecisionAction.WAIT
    )


def test_empty_candidate_list_is_rejected() -> None:
    with pytest.raises(ValueError):
        resolve_conflict([])


def test_non_action_candidates_are_rejected() -> None:
    with pytest.raises(TypeError):
        resolve_conflict([DecisionAction.ENTER, "EXIT"])  # type: ignore[list-item]


def test_conflicting_expert_evidence_is_risk_first() -> None:
    assert resolve_conflict([DecisionAction.ENTER, DecisionAction.EXIT]) is DecisionAction.EXIT
    assert resolve_conflict([DecisionAction.ENTER, DecisionAction.REDUCE]) is DecisionAction.REDUCE
    assert resolve_conflict([DecisionAction.ENTER, DecisionAction.HOLD]) is DecisionAction.HOLD
    assert resolve_conflict([DecisionAction.HOLD, DecisionAction.WAIT]) is DecisionAction.WAIT
    assert (
        resolve_conflict([DecisionAction.ENTER, DecisionAction.WAIT, DecisionAction.ABSTAIN])
        is DecisionAction.ABSTAIN
    )
    # Any single dissenting WAIT/ABSTAIN outranks a wall of entries.
    assert (
        resolve_conflict([DecisionAction.ENTER] * 8 + [DecisionAction.ABSTAIN])
        is DecisionAction.ABSTAIN
    )


@pytest.mark.parametrize(
    "candidates",
    [
        [DecisionAction.ENTER, DecisionAction.HOLD, DecisionAction.WAIT],
        [DecisionAction.EXIT, DecisionAction.REDUCE, DecisionAction.ENTER],
        [DecisionAction.ABSTAIN, DecisionAction.WAIT, DecisionAction.HOLD],
    ],
)
def test_candidate_order_never_changes_the_result(candidates: list[DecisionAction]) -> None:
    results = {resolve_conflict(list(order)) for order in itertools.permutations(candidates)}
    assert len(results) == 1


def test_consensus_cannot_override_venue_uncertainty() -> None:
    unresolved = legal(PositionPresence.OPEN, venue=VenueOrderStatus.UNKNOWN, armed=True)
    assert unresolved == WAIT_ONLY
    assert (
        resolve_legal_conflict(
            [DecisionAction.ENTER, DecisionAction.HOLD, DecisionAction.EXIT], legal=unresolved
        )
        is DecisionAction.ABSTAIN
    )


def test_consensus_cannot_override_staleness() -> None:
    stale_flat = legal(
        PositionPresence.NONE, venue=VenueOrderStatus.CANCELED, stale=True, armed=True
    )
    assert DecisionAction.ENTER not in stale_flat
    assert (
        resolve_legal_conflict([DecisionAction.ENTER, DecisionAction.ENTER], legal=stale_flat)
        is DecisionAction.ABSTAIN
    )


def test_legal_conflict_keeps_only_permitted_actions() -> None:
    flat_armed = legal(PositionPresence.NONE, venue=VenueOrderStatus.CANCELED, armed=True)
    assert (
        resolve_legal_conflict([DecisionAction.ENTER, DecisionAction.WAIT], legal=flat_armed)
        is DecisionAction.WAIT
    )
    assert resolve_legal_conflict([DecisionAction.ENTER], legal=flat_armed) is DecisionAction.ENTER


def test_legal_conflict_is_order_and_duplicate_invariant() -> None:
    actions = legal(PositionPresence.OPEN, venue=VenueOrderStatus.FILLED)
    candidates = [
        DecisionAction.ENTER,
        DecisionAction.EXIT,
        DecisionAction.HOLD,
        DecisionAction.HOLD,
    ]
    results = {
        resolve_legal_conflict(list(order), legal=actions)
        for order in itertools.permutations(candidates)
    }
    assert len(results) == 1
    assert results == {DecisionAction.EXIT}


def test_resolution_never_returns_an_illegal_action() -> None:
    for position, venue, stale, armed in WORLD_STATES:
        permitted = legal(position, venue=venue, stale=stale, armed=armed)
        for candidate in DecisionAction:
            resolved = resolve_legal_conflict([candidate], legal=permitted)
            assert resolved in permitted or resolved is DecisionAction.ABSTAIN


def test_legal_table_rows_match_lookup_without_gates() -> None:
    for position, venue in itertools.product(PositionPresence, VenueOrderStatus):
        row: frozenset[DecisionAction] = LEGAL_ACTION_TABLE[(position, venue)]
        assert legal(position, venue=venue, stale=False, armed=True) == row, (
            f"stale/armed gates changed a fresh-armed row for {position}/{venue}"
        )
        assert row <= set(DecisionAction)
