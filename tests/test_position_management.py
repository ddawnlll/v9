"""M3-03: HOLD/REDUCE/EXIT advisories and protective-intent proposals.

Every claim in the module docstring is exercised here: the status table, the
lot/precision rule, the EXIT-over-ENTER precedence, the fee-aware break-even
geometry (including widening refusal), the fail-closed protection states, and
the structural absence of any native submission path.
"""

from __future__ import annotations

import ast
from decimal import ROUND_CEILING, Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from v8_next.adapters.protection_invariants import (
    BracketInvariantChecker,
    Direction,
    OrderRole,
    ProtectionEvent,
    ProtectionEventKind,
    Side,
)
from v8_next.adapters.protection_invariants import (
    ProtectionState as NativeProtectionState,
)
from v8_next.domain.decision import DecisionAction, ReasonCode, VenueOrderStatus
from v8_next.domain.protection import ProtectionState as DomainProtectionState
from v8_next.economics import management as management_module
from v8_next.economics.legality import PositionPresence
from v8_next.economics.management import (
    REDUCE_FRACTION,
    ManagementDecision,
    PositionAdvisory,
    ProtectionState,
    ProtectiveKind,
    ProtectiveProposal,
    break_even_price,
    evaluate_management,
    make_protective_intent_id,
    plan_partial_reduction,
)
from v8_next.economics.thesis import (
    ExpertEvidence,
    InvalidationKind,
    InvalidationRule,
    PositionEvaluation,
    PositionSnapshot,
    PositionThesis,
    StanceKind,
    StopKind,
    TargetPlan,
    ThesisBelief,
    ThesisStatus,
    build_thesis,
    evaluate_position,
)

D = Decimal
BELIEF_HASH = "a" * 64
NOW = 1_000
INSTRUMENT = "BTCUSDT-PERP.BINANCE"
ZERO = D("0")
# Module-level singletons: ruff's B008 forbids call expressions in defaults.
_STOP = D("49000")
_MARK = D("50000")
_ENTRY = D("50000")
_NO_FEE = D("0")
_ONE_LOT = D("1")
_LOT_STEP = D("0.001")
_TICK = D("0.01")


# --------------------------------------------------------------------- fixtures


def rules() -> tuple[InvalidationRule, ...]:
    return (
        InvalidationRule(kind=InvalidationKind.PRICE_STOP),
        InvalidationRule(kind=InvalidationKind.REGIME_CHANGE),
        InvalidationRule(kind=InvalidationKind.EXPERT_CONTRADICTION, threshold=D("0.5")),
        InvalidationRule(kind=InvalidationKind.TIME_LIMIT),
        InvalidationRule(kind=InvalidationKind.PROTECTION_GAP),
    )


def thesis(*, side: str = "LONG", entry: str = "50000", stop: str = "49000") -> PositionThesis:
    return build_thesis(
        entry_decision_id="dec-" + "b" * 60,
        original_opportunity_id="opp-1",
        original_market_belief_id=BELIEF_HASH,
        native_position_id="pos-1",
        instrument_id=INSTRUMENT,
        side=side,
        entry_price=D(entry),
        initial_stop_price=D(stop),
        initial_stop_kind=StopKind.NATIVE_PROTECTIVE,
        original_risk_usdt=D("100"),
        target_plan=TargetPlan(target_price=D("52000"), expected_bars=8, max_bars=24),
        invalidation_rules=rules(),
        entry_regime="TREND_UP",
        policy_version="thesis-policy-v1",
        created_ns=100,
    )


def belief(*, regime: str = "TREND_UP", stale: bool = False, abstain: bool = False) -> ThesisBelief:
    return ThesisBelief(
        belief_id="belief-1",
        version_hash=BELIEF_HASH,
        regime=regime,
        as_of_ns=NOW,
        stale=stale,
        abstain=abstain,
    )


def snapshot(
    *,
    quantity: str = "1",
    mark: str = "50000",
    protective: tuple[str, ...] = ("stop-1",),
    elapsed: int = 0,
) -> PositionSnapshot:
    return PositionSnapshot(
        native_position_id="pos-1",
        quantity=D(quantity),
        average_price=D("50000"),
        mark_price=D(mark),
        protective_order_ids=protective,
        latest_belief_id="belief-1",
        elapsed_bars=elapsed,
        available_at_ns=500,
    )


def evaluation(
    *,
    snap: PositionSnapshot | None = None,
    bel: ThesisBelief | None = None,
    evidence: tuple[ExpertEvidence, ...] = (),
    side: str = "LONG",
) -> PositionEvaluation:
    return evaluate_position(
        thesis(side=side),
        snapshot() if snap is None else snap,
        belief() if bel is None else bel,
        now_ns=NOW,
        evidence=evidence,
    )


def manage(
    outcome: PositionEvaluation,
    *,
    protection_state: ProtectionState = ProtectionState.PROTECTED,
    protective_stop_price: Decimal | None = _STOP,
    fee_bps: Decimal = _NO_FEE,
    mark_price: Decimal = _MARK,
    side: str = "LONG",
    entry_price: Decimal = _ENTRY,
    quantity: Decimal = _ONE_LOT,
    initial_stop_price: Decimal = _STOP,
    lot_step: Decimal = _LOT_STEP,
    tick_size: Decimal = _TICK,
    venue_status: VenueOrderStatus = VenueOrderStatus.ACCEPTED,
    position_presence: PositionPresence = PositionPresence.OPEN,
    stale: bool = False,
    armed: bool = False,
    competing: tuple[DecisionAction, ...] = (),
) -> ManagementDecision:
    return evaluate_management(
        outcome,
        protection_state=protection_state,
        fee_bps=fee_bps,
        mark_price=mark_price,
        side=side,
        entry_price=entry_price,
        quantity=quantity,
        initial_stop_price=initial_stop_price,
        instrument_id=INSTRUMENT,
        protective_stop_price=protective_stop_price,
        lot_step=lot_step,
        tick_size=tick_size,
        venue_status=venue_status,
        position_presence=position_presence,
        stale=stale,
        armed_opportunity=armed,
        competing_actions=competing,
    )


# ------------------------------------------------------------- advisory status


def test_stable_thesis_proposes_hold_with_no_reduction() -> None:
    outcome = evaluation()
    assert outcome.status.value == "HOLDING"

    decision = manage(outcome)

    advisory = decision.advisory
    assert advisory.action is DecisionAction.HOLD
    assert advisory.reason_codes == (ReasonCode.POSITION_OPEN,)
    assert advisory.fraction == ZERO
    assert advisory.reduce_quantity is None
    assert advisory.thesis_status.value == "HOLDING"
    assert advisory.advisory_only is True
    assert decision.fail_closed is False
    assert decision.protected is True
    assert decision.admission_ok is True
    assert decision.proposals == ()


@pytest.mark.parametrize(
    ("snap", "bel", "evidence"),
    [
        # Weakening by time limit.
        (snapshot(elapsed=24), belief(), ()),
        # Weakening by expert contradiction (ratio 1.0 >= threshold 0.5).
        (
            snapshot(),
            belief(),
            (ExpertEvidence(expert_id="e1", stance=StanceKind.CONTRADICT, weight=D("1")),),
        ),
    ],
    ids=["time-limit", "expert-contradiction"],
)
def test_weakening_thesis_proposes_half_reduction(
    snap: PositionSnapshot,
    bel: ThesisBelief,
    evidence: tuple[ExpertEvidence, ...],
) -> None:
    outcome = evaluation(snap=snap, bel=bel, evidence=evidence)
    assert outcome.status.value == "WEAKENING"

    advisory = manage(outcome).advisory

    assert advisory.action is DecisionAction.REDUCE
    assert advisory.reason_codes == (ReasonCode.POSITION_REDUCE_REQUIRED,)
    assert advisory.fraction == REDUCE_FRACTION
    assert advisory.reduce_quantity == D("0.5")


def test_invalidated_thesis_proposes_exit() -> None:
    outcome = evaluation(bel=belief(regime="TREND_DOWN"))
    assert outcome.status.value == "INVALIDATED"

    decision = manage(outcome)

    assert decision.advisory.action is DecisionAction.EXIT
    assert decision.advisory.reason_codes == (
        ReasonCode.EVIDENCE_CONFLICT,
        ReasonCode.POSITION_EXIT_REQUIRED,
    )
    assert decision.advisory.fraction == ZERO
    assert decision.advisory.reduce_quantity is None
    # The stop is intact and still on the safe side of the mark, so the reading
    # is honest even though the thesis is dead; the exit itself is advisory.
    assert decision.protected is True
    assert decision.fail_closed is False
    assert decision.admission_ok is False
    assert decision.proposals == ()


def test_missing_input_fails_closed_to_exit() -> None:
    outcome = evaluation(bel=belief(stale=True))
    assert outcome.status.value == "MISSING_INPUT"

    decision = manage(outcome)

    assert decision.advisory.action is DecisionAction.EXIT
    assert decision.advisory.reason_codes == (
        ReasonCode.BELIEF_STALE,
        ReasonCode.POSITION_EXIT_REQUIRED,
    )
    assert decision.fail_closed is True
    assert decision.admission_ok is False


def test_closed_position_has_no_admitted_advisory() -> None:
    outcome = evaluation(snap=snapshot(quantity="0"))
    assert outcome.status.value == "CLOSED"

    decision = manage(
        outcome, protection_state=ProtectionState.UNPROTECTED, protective_stop_price=None
    )

    assert decision.advisory.action is DecisionAction.HOLD
    assert decision.advisory.reason_codes == (ReasonCode.POSITION_NONE,)
    assert decision.advisory.fraction == ZERO
    assert decision.fail_closed is False
    assert decision.protected is False
    assert decision.admission_ok is False
    assert decision.proposals == ()


def test_thesis_that_can_only_wait_fails_closed() -> None:
    # A protection-gap weakening makes the thesis itself say WAIT, which is not
    # permission to hold: management must still fail closed.
    outcome = evaluation(snap=snapshot(protective=()), bel=belief())
    assert outcome.status.value == "WEAKENING"
    assert outcome.proposals == (DecisionAction.WAIT,)

    decision = manage(outcome, protection_state=ProtectionState.GAP, protective_stop_price=None)

    assert decision.advisory.action is DecisionAction.EXIT
    assert decision.fail_closed is True
    assert decision.advisory.reason_codes == (
        ReasonCode.RISK_UNKNOWN,
        ReasonCode.POSITION_EXIT_REQUIRED,
    )


# ------------------------------------------------------------ partial reduction


@pytest.mark.parametrize(
    ("quantity", "lot_step", "expected"),
    [
        ("1", "0.001", "0.5"),
        ("0.003", "0.001", "0.001"),
        ("1", "0.3", "0.3"),
        ("2.5", "1", "1"),
        ("-1", "0.001", "0.5"),  # a short's signed size reduces by magnitude
        ("0.001", "0.001", None),  # floors onto zero: not expressible
        ("0", "0.001", None),
    ],
)
def test_partial_reduction_lot_rule(quantity: str, lot_step: str, expected: str | None) -> None:
    planned = plan_partial_reduction(D(quantity), D(lot_step))
    assert planned == (None if expected is None else D(expected))
    if planned is not None:
        assert planned <= abs(D(quantity))


def test_partial_reduction_fraction_never_exceeds_open_quantity() -> None:
    outcome = evaluation(snap=snapshot(elapsed=24))
    quantity = D("0.003")

    advisory = manage(outcome, quantity=quantity).advisory

    assert advisory.action is DecisionAction.REDUCE
    assert advisory.reduce_quantity == D("0.001")
    assert advisory.reduce_quantity <= quantity
    assert advisory.fraction == D("0.333333333")
    assert advisory.fraction * quantity <= quantity


def test_reduction_smaller_than_one_lot_escalates_to_exit() -> None:
    outcome = evaluation(snap=snapshot(elapsed=24))

    advisory = manage(outcome, quantity=D("0.001"), lot_step=D("0.001")).advisory

    assert advisory.action is DecisionAction.EXIT
    assert advisory.fraction == ZERO
    assert advisory.reduce_quantity is None
    assert ReasonCode.POSITION_EXIT_REQUIRED in advisory.reason_codes


@pytest.mark.parametrize(("quantity", "lot_step"), [("0", "0.001"), ("0.0005", "0.001")])
def test_partial_reduction_rejects_unrepresentable_open_size(quantity: str, lot_step: str) -> None:
    assert plan_partial_reduction(D(quantity), D(lot_step)) is None


def test_partial_reduction_requires_a_positive_lot_step() -> None:
    with pytest.raises(ValueError, match="lot_step"):
        plan_partial_reduction(D("1"), ZERO)


# ----------------------------------------------------------- conflict precedence


def test_conflicting_exit_and_enter_resolves_to_exit() -> None:
    outcome = evaluation(bel=belief(regime="TREND_DOWN"))  # INVALIDATED -> EXIT

    for competing in (
        (DecisionAction.ENTER,),
        (DecisionAction.ENTER, DecisionAction.ENTER),
    ):
        decision = manage(outcome, competing=competing)

        assert decision.advisory.action is DecisionAction.EXIT
        assert DecisionAction.ENTER not in decision.legal_actions
        assert DecisionAction.ENTER is not decision.advisory.action


def test_competing_exit_escalates_a_hold_but_enter_never_wins() -> None:
    outcome = evaluation()

    held = manage(outcome, competing=(DecisionAction.ENTER,)).advisory
    exited = manage(outcome, competing=(DecisionAction.ENTER, DecisionAction.EXIT)).advisory

    assert held.action is DecisionAction.HOLD
    assert exited.action is DecisionAction.EXIT


def test_competing_wait_cannot_relax_a_management_action() -> None:
    outcome = evaluation()

    decision = manage(outcome, competing=(DecisionAction.WAIT, DecisionAction.ABSTAIN))

    assert decision.advisory.action is DecisionAction.HOLD


def test_advisory_action_vocabulary_excludes_enter_and_abstain() -> None:
    for action in (DecisionAction.ENTER, DecisionAction.WAIT, DecisionAction.ABSTAIN):
        with pytest.raises(ValidationError):
            PositionAdvisory(
                advisory_id="x",
                decision_time_ns=NOW,
                position_id="pos-1",
                thesis_id="thesis-1",
                action=action,
                reason_codes=(ReasonCode.POSITION_OPEN,),
                thesis_status=ThesisStatus.HOLDING,
            )


# ----------------------------------------------------------------- break-even


def test_break_even_includes_round_trip_fees_and_rounds_outward() -> None:
    long_level = break_even_price("LONG", D("50000"), D("10"), tick_size=D("0.01"))
    short_level = break_even_price("SHORT", D("50000"), D("10"), tick_size=D("0.01"))

    # 50000 * 1.001 / 0.999 = 50100.1001..., rounded up to the tick.
    assert long_level == D("50100.11")
    # 50000 * 0.999 / 1.001 = 49900.0999..., rounded down to the tick.
    assert short_level == D("49900.09")
    assert long_level > D("50000") > short_level
    # Fees are charged on both legs, so the level must cover r * (entry + level):
    # the outward tick rounding keeps the proposal on the covering side.
    rate = D("10") / D("10000")
    assert long_level - D("50000") >= rate * (D("50000") + long_level)
    assert D("50000") - short_level >= rate * (D("50000") + short_level)


def test_break_even_proposal_is_emitted_when_fees_are_covered() -> None:
    outcome = evaluation()

    decision = manage(
        outcome,
        fee_bps=D("10"),
        mark_price=D("50500"),
        protective_stop_price=D("49500"),
        initial_stop_price=D("49000"),
    )

    assert len(decision.proposals) == 1
    proposal = decision.proposals[0]
    assert proposal.kind is ProtectiveKind.PROTECTIVE_BREAK_EVEN
    assert proposal.price == D("50100.11")
    assert proposal.quantity == D("1")
    assert proposal.reason is ReasonCode.POSITION_OPEN
    assert proposal.requires_risk_permission is True
    assert proposal.advisory_only is True
    assert proposal.intent.reduce_only is True
    assert proposal.intent.side == "SHORT"  # closes the LONG position
    assert proposal.intent.trigger_price == proposal.price
    assert proposal.intent.decision_id == decision.advisory.advisory_id


def test_break_even_proposal_never_widens_an_existing_stop() -> None:
    outcome = evaluation()

    # Current stop (50200) is already tighter than the fee-inclusive break-even
    # (50100.11), so moving to break-even would widen risk: no proposal.
    decision = manage(
        outcome,
        fee_bps=D("10"),
        mark_price=D("50500"),
        protective_stop_price=D("50200"),
    )

    assert decision.proposals == ()


def test_break_even_proposal_withheld_when_fees_are_not_covered() -> None:
    outcome = evaluation()

    # 1% per leg: break-even is 51010.11, above the mark, so a stop there would
    # trigger immediately rather than cover the fee budget.
    level = break_even_price("LONG", D("50000"), D("100"))
    assert level == D("51010.11")

    decision = manage(
        outcome,
        fee_bps=D("100"),
        mark_price=D("50500"),
        protective_stop_price=D("49500"),
    )

    assert decision.proposals == ()


def test_short_break_even_proposal_moves_down_only() -> None:
    short_outcome = evaluate_position(
        thesis(side="SHORT", entry="50000", stop="51000"),
        PositionSnapshot(
            native_position_id="pos-1",
            quantity=D("1"),
            average_price=D("50000"),
            mark_price=D("49500"),
            protective_order_ids=("stop-1",),
            latest_belief_id="belief-1",
            elapsed_bars=0,
            available_at_ns=500,
        ),
        belief(),
        now_ns=NOW,
    )

    decision = manage(
        short_outcome,
        side="SHORT",
        entry_price=D("50000"),
        initial_stop_price=D("51000"),
        protective_stop_price=D("50500"),
        mark_price=D("49500"),
        fee_bps=D("10"),
    )

    proposal = decision.proposals[0]
    assert proposal.kind is ProtectiveKind.PROTECTIVE_BREAK_EVEN
    assert proposal.price == D("49900.09")
    assert proposal.price < D("50500")  # tightened, never widened
    assert proposal.price > D("49500")
    assert proposal.intent.side == "LONG"


def test_break_even_level_is_below_mark_only() -> None:
    outcome = evaluation()

    # Break-even sits above the current mark: refused, not clamped.
    decision = manage(
        outcome,
        fee_bps=D("10"),
        mark_price=D("50050"),
        protective_stop_price=D("49500"),
    )

    assert decision.proposals == ()


# ------------------------------------------------------- fail-closed protection


@pytest.mark.parametrize(
    "state",
    [
        ProtectionState.UNKNOWN,
        ProtectionState.REJECTED,
        ProtectionState.GAP,
        ProtectionState.PARTIAL,
    ],
)
def test_unreadable_or_refused_protection_is_never_reported_protected(
    state: ProtectionState,
) -> None:
    outcome = evaluation()

    decision = manage(outcome, protection_state=state, protective_stop_price=None)

    assert decision.protected is False
    assert decision.protection_state is state
    assert decision.advisory.action is DecisionAction.EXIT
    assert decision.fail_closed is True
    assert decision.admission_ok is False
    assert decision.advisory.action is not DecisionAction.HOLD


def test_stop_rejection_carries_the_venue_and_risk_reasons() -> None:
    outcome = evaluation()

    decision = manage(
        outcome, protection_state=ProtectionState.REJECTED, protective_stop_price=None
    )

    # The thesis is still intact (POSITION_OPEN is a fact about the position, not
    # an instruction), but protection is refused, so the advisory is EXIT.
    assert decision.advisory.reason_codes == (
        ReasonCode.VENUE_REJECTED,
        ReasonCode.RISK_DENIED,
        ReasonCode.POSITION_OPEN,
        ReasonCode.POSITION_EXIT_REQUIRED,
    )
    assert decision.protected is False


def test_partial_protection_is_fail_closed_never_hold_never_protected() -> None:
    """A live stop that covers less than the position must force EXIT, not HOLD."""

    decision = manage(
        evaluation(), protection_state=ProtectionState.PARTIAL, protective_stop_price=None
    )

    assert decision.protection_state is ProtectionState.PARTIAL
    assert decision.protected is False
    assert decision.fail_closed is True
    assert decision.admission_ok is False
    assert decision.advisory.action is DecisionAction.EXIT
    assert decision.advisory.action is not DecisionAction.HOLD
    assert ReasonCode.RISK_DENIED in decision.advisory.reason_codes

    proposal = decision.proposals[0]
    assert proposal.kind is ProtectiveKind.PROTECTIVE_ADD
    assert proposal.reason is ReasonCode.RISK_DENIED


def test_protection_state_is_one_shared_enum_not_two_vocabularies() -> None:
    assert management_module.ProtectionState is DomainProtectionState
    assert NativeProtectionState is DomainProtectionState
    assert ProtectionState is DomainProtectionState


def test_a_partial_verdict_from_the_native_checker_needs_no_translation() -> None:
    checker = BracketInvariantChecker()
    checker.apply_all(
        (
            ProtectionEvent(
                kind=ProtectionEventKind.ORDER_SUBMITTED,
                ts_ns=1,
                client_order_id="E-1",
                position_id="P-1",
                role=OrderRole.ENTRY,
                side=Side.BUY,
                quantity=D("2"),
                reduce_only=False,
                direction=Direction.LONG,
            ),
            ProtectionEvent(
                kind=ProtectionEventKind.FILL,
                ts_ns=2,
                client_order_id="E-1",
                position_id="P-1",
                role=OrderRole.ENTRY,
                side=Side.BUY,
                price=D("100"),
                quantity=D("2"),
                filled_quantity=D("1"),
                reduce_only=False,
                direction=Direction.LONG,
            ),
            ProtectionEvent(
                kind=ProtectionEventKind.ORDER_SUBMITTED,
                ts_ns=3,
                client_order_id="S-1",
                position_id="P-1",
                role=OrderRole.STOP,
                side=Side.SELL,
                price=D("95"),
                quantity=D("0.5"),
                reduce_only=True,
            ),
            ProtectionEvent(
                kind=ProtectionEventKind.ACK,
                ts_ns=4,
                client_order_id="S-1",
                position_id="P-1",
                role=OrderRole.STOP,
                side=Side.SELL,
                price=D("95"),
                quantity=D("0.5"),
                reduce_only=True,
            ),
        )
    )
    assert checker.state is ProtectionState.PARTIAL

    # The producer's verdict is the consumer's argument: same object, no adapter.
    decision = manage(evaluation(), protection_state=checker.state, protective_stop_price=None)
    assert decision.protection_state is ProtectionState.PARTIAL
    assert decision.fail_closed is True
    assert decision.advisory.action is DecisionAction.EXIT


def test_unprotected_position_proposes_restoring_the_original_stop() -> None:
    outcome = evaluation()

    decision = manage(
        outcome,
        protection_state=ProtectionState.UNPROTECTED,
        protective_stop_price=None,
    )

    assert decision.advisory.action is DecisionAction.EXIT
    assert decision.fail_closed is True
    assert decision.protected is False

    proposal = decision.proposals[0]
    assert proposal.kind is ProtectiveKind.PROTECTIVE_ADD
    assert proposal.price == D("49000")  # the thesis stop, never a wider level
    assert proposal.reason is ReasonCode.RISK_DENIED
    assert proposal.requires_risk_permission is True
    assert proposal.intent.side == "SHORT"


def test_unprotected_position_does_not_propose_a_breached_stop() -> None:
    outcome = evaluation()

    # The thesis stop is already above the mark, so it is not protection any
    # more: no repair proposal is fabricated.
    decision = manage(
        outcome,
        protection_state=ProtectionState.UNPROTECTED,
        protective_stop_price=None,
        mark_price=D("48500"),
    )

    assert decision.advisory.action is DecisionAction.EXIT
    assert decision.proposals == ()


@pytest.mark.parametrize("stop", [None, "51000"])
def test_contradictory_protected_reading_is_downgraded(stop: str | None) -> None:
    outcome = evaluation()

    decision = manage(
        outcome,
        protection_state=ProtectionState.PROTECTED,
        protective_stop_price=None if stop is None else D(stop),
    )

    assert decision.protected is False
    assert decision.protection_state is ProtectionState.UNKNOWN
    assert decision.advisory.action is DecisionAction.EXIT
    assert decision.fail_closed is True


def test_unsafe_defaults_cannot_be_admitted() -> None:
    outcome = evaluation()

    decision = evaluate_management(
        outcome,
        protection_state=ProtectionState.PROTECTED,
        fee_bps=D("0"),
        mark_price=D("50000"),
        side="LONG",
        entry_price=D("50000"),
        quantity=D("1"),
        initial_stop_price=D("49000"),
        instrument_id=INSTRUMENT,
        protective_stop_price=D("49000"),
    )

    assert decision.admission_ok is False
    # Presence is unreadable by default, so the legal set is the no-action set and
    # nothing this advisory says can be admitted.
    assert set(decision.legal_actions) == {DecisionAction.WAIT, DecisionAction.ABSTAIN}
    assert decision.advisory.action is DecisionAction.HOLD
    assert decision.advisory.action not in decision.legal_actions


# ------------------------------------------------------------------ invariants


def test_management_decision_invariants_are_enforced() -> None:
    decision = manage(evaluation())
    payload = decision.model_dump()

    fail_closed_hold = {**payload, "advisory": {**payload["advisory"]}, "fail_closed": True}
    with pytest.raises(ValidationError, match="fail-closed"):
        ManagementDecision.model_validate(fail_closed_hold)

    protected_without_state = {
        **payload,
        "advisory": {**payload["advisory"]},
        "protection_state": ProtectionState.UNKNOWN.value,
    }
    with pytest.raises(ValidationError, match="PROTECTED"):
        ManagementDecision.model_validate(protected_without_state)


def test_protective_proposal_demands_risk_permission() -> None:
    proposal = manage(
        evaluation(),
        fee_bps=D("10"),
        mark_price=D("50500"),
        protective_stop_price=D("49500"),
    ).proposals[0]
    payload = proposal.model_dump()

    with pytest.raises(ValidationError):
        ProtectiveProposal.model_validate({**payload, "requires_risk_permission": False})


def test_identities_are_deterministic_and_content_addressed() -> None:
    first = manage(
        evaluation(), fee_bps=D("10"), mark_price=D("50500"), protective_stop_price=D("49500")
    )
    second = manage(
        evaluation(), fee_bps=D("10"), mark_price=D("50500"), protective_stop_price=D("49500")
    )
    other = manage(
        evaluation(), protection_state=ProtectionState.UNKNOWN, protective_stop_price=None
    )

    assert first.advisory.advisory_id == second.advisory.advisory_id
    assert first.management_id == second.management_id
    assert first.proposals[0].proposal_id == second.proposals[0].proposal_id
    assert first.management_id != other.management_id
    assert first.advisory.advisory_id != other.advisory.advisory_id


def test_protective_intent_identity_is_its_own_digest() -> None:
    proposal = manage(
        evaluation(),
        fee_bps=D("10"),
        mark_price=D("50500"),
        protective_stop_price=D("49500"),
    ).proposals[0]

    assert proposal.intent.intent_id == make_protective_intent_id(proposal.intent)
    changed = proposal.intent.model_copy(update={"trigger_price": D("50100.12")})
    assert make_protective_intent_id(changed) != proposal.intent.intent_id


# --------------------------------------------------- no native submission path

FORBIDDEN_IMPORT_PREFIXES = (
    "nautilus_trader",
    "v8_next.adapters",
    "v8_next.execution",
    "v8_next.risk",
)
FORBIDDEN_FUNCTION_NAMES = {
    "submit",
    "submit_order",
    "place_order",
    "send_order",
    "execute",
    "execute_order",
    "cancel_order",
}


def test_module_has_no_order_or_execution_path() -> None:
    source = Path(management_module.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)

    imported: set[str] = set()
    defined_functions: set[str] = set()
    defined_classes: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            imported.add(node.module)
        elif isinstance(node, ast.FunctionDef):
            defined_functions.add(node.name)
        elif isinstance(node, ast.ClassDef):
            defined_classes.add(node.name)

    assert [name for name in imported if name.startswith(FORBIDDEN_IMPORT_PREFIXES)] == []
    assert defined_functions & FORBIDDEN_FUNCTION_NAMES == set()
    assert [name for name in defined_classes if "Order" in name or "Fill" in name] == []


def test_every_emitted_record_is_advisory_only() -> None:
    decision = manage(
        evaluation(),
        fee_bps=D("10"),
        mark_price=D("50500"),
        protective_stop_price=D("49500"),
    )

    assert decision.advisory_only is True
    assert decision.advisory.advisory_only is True
    for proposal in decision.proposals:
        assert proposal.advisory_only is True
        assert proposal.intent.advisory_only is True
        assert proposal.requires_risk_permission is True
        assert proposal.intent.reduce_only is True


def test_rounding_of_break_even_is_tick_aligned() -> None:
    level = break_even_price("LONG", D("50000"), D("10"), tick_size=D("0.25"))
    steps = (level / D("0.25")).to_integral_value(rounding=ROUND_CEILING)
    assert level == steps * D("0.25")
