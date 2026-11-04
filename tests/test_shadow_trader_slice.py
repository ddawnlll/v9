"""M3-07 / #28 — end-to-end Adaptive Shadow Trader vertical slice.

One deterministic multi-event trace over the whole chain: PIT observations ->
``MarketBeliefV1`` -> 28 experts -> ``OpportunityBook`` -> ``TraderDecision`` ->
entry / management proposals -> portfolio challenger -> decision receipts. The V8
native replay is the control arm; the slice may never touch it.

Mechanics (synthetic, declared): every assertion below is a property of the
*wiring*, not of market skill — the scripted tape only has to exercise the
branches. No tape, no engine, no sealed OOS, no live order. Where a witness
cannot be observed (no altcoin tape, no soak) the test says nothing rather than
inventing a pass.
"""

from __future__ import annotations

import ast
import json
import random
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from v8_next.app import shadow_trader as slice_module
from v8_next.app.shadow_trader import (
    ALLOCATION_NOT_ATTEMPTED,
    BarUpdate,
    CutUpdate,
    FundingUpdate,
    HardRiskLimits,
    OpportunityUpdate,
    PositionUpdate,
    ProtectionUpdate,
    QuoteUpdate,
    RiskObservation,
    RiskStatus,
    RiskUpdate,
    ShadowScriptError,
    ShadowTrace,
    ShadowTrader,
    TraceStep,
    VenueUpdate,
)
from v8_next.domain.decision import DecisionAction, OpportunityLifecycle, VenueOrderStatus
from v8_next.economics.legality import PositionPresence
from v8_next.economics.management import ProtectionState
from v8_next.evaluation import shadow_parity as sp
from v8_next.opportunities.models import ExposureDirection, OpportunityStatus

NS = 1_000_000_000
M15 = 15 * 60 * NS
IID = "BTCUSDT-PERP.BINANCE"
CAMPAIGN = "camp-shadow-1"
BASE = 1_700_000_000 * NS

#: The bar whose close starts the drawdown that breaches the thesis stop.
CRASH = 99
LAST_BAR = 120

#: The scripted decision cuts, in closed-bar counts.
CUTS = (4, 12, 20, 90, 95, 96, 100, 104, 120, 130)

#: (closed-bar count, equity) of each hard-risk republication.
RISK_READINGS = (
    (0, "1200"),
    (4, "1200"),
    (20, "1200"),
    (88, "1200"),
    (95, "1200"),
    (96, "1000"),
    (100, "1000"),
    (120, "1000"),
)

ALL_SIX_ACTIONS = frozenset(
    {
        DecisionAction.WAIT,
        DecisionAction.ENTER,
        DecisionAction.HOLD,
        DecisionAction.REDUCE,
        DecisionAction.EXIT,
        DecisionAction.ABSTAIN,
    }
)


# --------------------------------------------------------------------------- #
# the scripted tape
# --------------------------------------------------------------------------- #
def _close(index: int) -> Decimal:
    if index < CRASH:
        return Decimal(100) + Decimal("0.5") * index
    if index == CRASH:
        return Decimal("96")
    return Decimal("96") + Decimal("0.1") * (index - CRASH)


def _bar(index: int) -> BarUpdate:
    close = _close(index)
    previous = _close(index - 1) if index else close - Decimal("0.1")
    return BarUpdate(
        event_time_ns=BASE + index * M15,
        available_at_ns=BASE + (index + 1) * M15,
        instrument_id=IID,
        start_ns=BASE + index * M15,
        end_ns=BASE + (index + 1) * M15,
        open=previous,
        high=max(previous, close) + Decimal("0.2"),
        low=min(previous, close) - Decimal("0.2"),
        close=close,
        volume=Decimal("10") + Decimal(index % 5),
        sequence=index,
    )


def _quote(index: int) -> QuoteUpdate:
    close = _close(index)
    return QuoteUpdate(
        event_time_ns=BASE + (index + 1) * M15,
        available_at_ns=BASE + (index + 1) * M15,
        instrument_id=IID,
        mark=close,
        bid=close - Decimal("0.05"),
        ask=close + Decimal("0.05"),
    )


def _limits(equity: str = "1200") -> HardRiskLimits:
    return HardRiskLimits(
        equity=Decimal(equity),
        committed_exposure=Decimal("0"),
        max_gross_exposure=Decimal("900"),
        max_per_sector=Decimal("600"),
        risk_budget=Decimal("300"),
        sector="CRYPTO",
        correlation_group="BTC",
        step_size=Decimal("0.001"),
        min_size=Decimal("0.001"),
        max_size=Decimal("100"),
        tick_size=Decimal("0.01"),
        min_notional=Decimal("10"),
    )


def _risk(event_time: int, equity: str = "1200") -> RiskUpdate:
    return RiskUpdate(
        event_time_ns=event_time,
        available_at_ns=event_time,
        instrument_id=IID,
        observation=RiskObservation(
            status=RiskStatus.AVAILABLE,
            reason="PORTFOLIO_FEASIBLE",
            as_of_ns=event_time,
            limits=_limits(equity),
        ),
    )


def _opportunity(status: OpportunityStatus, event_time: int) -> OpportunityUpdate:
    """One episode (fixed identity) plus each reassessment of it."""
    return OpportunityUpdate(
        event_time_ns=event_time,
        available_at_ns=event_time,
        instrument_id=IID,
        direction=ExposureDirection.LONG,
        entry_price=Decimal("110"),
        as_of_time_ns=BASE + 20 * M15,
        valid_until_ns=BASE + 140 * M15,
        status=status,
        stop_price=Decimal("99"),
        target_price=Decimal("130"),
        expected_horizon_bars=16,
    )


def _script() -> list[object]:
    events: list[object] = [
        PositionUpdate(
            event_time_ns=BASE,
            available_at_ns=BASE,
            instrument_id=IID,
            presence=PositionPresence.NONE,
        ),
        VenueUpdate(
            event_time_ns=BASE,
            available_at_ns=BASE,
            instrument_id=IID,
            status=VenueOrderStatus.CANCELED,
        ),
        ProtectionUpdate(
            event_time_ns=BASE,
            available_at_ns=BASE,
            instrument_id=IID,
            state=ProtectionState.UNPROTECTED,
        ),
        FundingUpdate(
            event_time_ns=BASE,
            available_at_ns=BASE,
            instrument_id=IID,
            rate=Decimal("0.0001"),
        ),
    ]
    # The account/risk plane republishes; a reading older than its useful age is
    # treated as unknown by the slice, so the tape keeps it fresh across the cuts.
    for closed, equity in RISK_READINGS:
        events.append(_risk(BASE + closed * M15, equity=equity))
    events.extend(_bar(index) for index in range(LAST_BAR))
    events.extend(_quote(index) for index in range(84, LAST_BAR))
    # The market creates the opportunity at bar 20 and reassesses it twice.
    events.append(_opportunity(OpportunityStatus.CANDIDATE, BASE + 20 * M15))
    events.append(_opportunity(OpportunityStatus.CONFIRMED, BASE + 88 * M15))
    events.append(_opportunity(OpportunityStatus.INVALIDATED, BASE + 100 * M15))
    events.append(
        PositionUpdate(
            event_time_ns=BASE + 95 * M15,
            available_at_ns=BASE + 95 * M15,
            instrument_id=IID,
            presence=PositionPresence.OPEN,
            quantity=Decimal("6"),
            exposure=Decimal("867"),
            average_price=Decimal("144.5"),
            side="LONG",
            initial_stop_price=Decimal("140"),
            native_position_id="POS-shadow-1",
            elapsed_bars=1,
        )
    )
    events.append(
        ProtectionUpdate(
            event_time_ns=BASE + 95 * M15,
            available_at_ns=BASE + 95 * M15,
            instrument_id=IID,
            state=ProtectionState.PROTECTED,
            stop_price=Decimal("140"),
        )
    )
    events.append(
        PositionUpdate(
            event_time_ns=BASE + 104 * M15,
            available_at_ns=BASE + 104 * M15,
            instrument_id=IID,
            presence=PositionPresence.NONE,
        )
    )
    events.append(
        VenueUpdate(
            event_time_ns=BASE + 104 * M15,
            available_at_ns=BASE + 104 * M15,
            instrument_id=IID,
            status=VenueOrderStatus.UNKNOWN,
        )
    )
    events.append(
        ProtectionUpdate(
            event_time_ns=BASE + 104 * M15,
            available_at_ns=BASE + 104 * M15,
            instrument_id=IID,
            state=ProtectionState.UNPROTECTED,
        )
    )
    events.extend(
        CutUpdate(
            event_time_ns=BASE + closed * M15,
            available_at_ns=BASE + closed * M15,
            instruments=(IID,),
        )
        for closed in CUTS
    )
    return events


# --------------------------------------------------------------------------- #
# the V8 native control arm (validated, policy-off)
# --------------------------------------------------------------------------- #
def _native_control() -> sp.ReplayScript:
    """The frozen V8 replay: one campaign, two orders, two fills, one funding row."""

    def bar(index: int) -> sp.BarEvent:
        close = Decimal(100 + index)
        return sp.BarEvent(
            instrument_id=IID,
            start_ns=BASE + index * M15,
            end_ns=BASE + (index + 1) * M15,
            open=close - Decimal("0.5"),
            high=close + Decimal("0.25"),
            low=close - Decimal("0.75"),
            close=close,
            volume=Decimal("10"),
            sequence=index,
        )

    def at(cut: int) -> int:
        return BASE + cut * M15

    return sp.ReplayScript(
        instrument_id=IID,
        as_of_ns=at(31),
        bars=tuple(bar(index) for index in range(30)),
        opportunities=(
            sp.OpportunityEvent(
                opportunity_id="opp-native-1",
                campaign_id=CAMPAIGN,
                instrument_id=IID,
                direction="LONG",
                lifecycle=OpportunityLifecycle.ARMED,
                selected_ns=at(5),
                available_at_ns=at(10),
            ),
        ),
        native_decisions=(
            sp.NativeDecisionEvent(
                decision_id="dec-native-1",
                opportunity_id="opp-native-1",
                campaign_id=CAMPAIGN,
                instrument_id=IID,
                decision_time_ns=at(20),
                available_at_ns=at(20),
            ),
        ),
        orders=(
            sp.NativeOrderEvent(
                client_order_id="O-1",
                decision_id="dec-native-1",
                campaign_id=CAMPAIGN,
                instrument_id=IID,
                side="BUY",
                order_type="LIMIT",
                quantity=Decimal("1"),
                price=Decimal("119"),
                submitted_ns=at(20),
                available_at_ns=at(20),
            ),
            sp.NativeOrderEvent(
                client_order_id="O-2",
                decision_id="dec-native-1",
                campaign_id=CAMPAIGN,
                instrument_id=IID,
                side="SELL",
                order_type="LIMIT",
                quantity=Decimal("1"),
                price=Decimal("124"),
                submitted_ns=at(26),
                available_at_ns=at(26),
            ),
        ),
        protection=(
            sp.ProtectionOrderEvent(
                client_order_id="P-1",
                parent_client_order_id="O-1",
                kind="STOP_LOSS",
                side="SELL",
                trigger_price=Decimal("118"),
                quantity=Decimal("1"),
                submitted_ns=at(20),
                available_at_ns=at(20),
            ),
            sp.ProtectionOrderEvent(
                client_order_id="P-2",
                parent_client_order_id="O-1",
                kind="TAKE_PROFIT",
                side="SELL",
                trigger_price=Decimal("124"),
                quantity=Decimal("1"),
                submitted_ns=at(20),
                available_at_ns=at(20),
            ),
        ),
        fills=(
            sp.FillEvent(
                fill_id="F-1",
                client_order_id="O-1",
                decision_id="dec-native-1",
                campaign_id=CAMPAIGN,
                instrument_id=IID,
                quantity=Decimal("1"),
                price=Decimal("119"),
                fee_amount=Decimal("0.0595"),
                fee_currency="USDT",
                event_time_ns=at(20),
                available_at_ns=at(20),
            ),
            sp.FillEvent(
                fill_id="F-2",
                client_order_id="O-2",
                decision_id="dec-native-1",
                campaign_id=CAMPAIGN,
                instrument_id=IID,
                quantity=Decimal("1"),
                price=Decimal("124"),
                fee_amount=Decimal("0.062"),
                fee_currency="USDT",
                event_time_ns=at(26),
                available_at_ns=at(26),
            ),
        ),
        cashflows=(
            sp.CashflowEvent(
                cashflow_id="C-1",
                fill_id="F-1",
                campaign_id=CAMPAIGN,
                amount=Decimal("-0.0595"),
                event_time_ns=at(20),
                available_at_ns=at(20),
            ),
            sp.CashflowEvent(
                cashflow_id="C-2",
                fill_id="F-2",
                campaign_id=CAMPAIGN,
                amount=Decimal("4.938"),
                event_time_ns=at(26),
                available_at_ns=at(26),
            ),
        ),
        funding=(
            sp.FundingRowEvent(
                funding_id="FU-1",
                instrument_id=IID,
                amount=Decimal("-0.12"),
                currency="USDT",
                rate=Decimal("-0.0001"),
                event_time_ns=at(24),
                available_at_ns=at(24),
            ),
        ),
    )


def _trader() -> ShadowTrader:
    return ShadowTrader(native_control=_native_control())


def _trace() -> ShadowTrace:
    return _trader().run(_script())


# --------------------------------------------------------------------------- #
# the six actions and the opportunity reassessment
# --------------------------------------------------------------------------- #
def test_one_trace_reaches_all_six_actions() -> None:
    trace = _trace()

    actions = trace.actions()
    assert set(actions) == ALL_SIX_ACTIONS, [action.value for action in actions]
    assert len(trace.steps) == len(CUTS)
    assert len(trace.receipt_ids) == len(trace.steps)
    assert len(set(trace.receipt_ids)) == len(trace.receipt_ids)


def test_opportunity_lifecycle_is_reassessed_and_never_regresses() -> None:
    trace = _trace()
    observed = [step.lifecycle for step in trace.steps]

    assert observed[0] is None  # before the market created the episode
    assert OpportunityLifecycle.WATCHING in observed
    assert OpportunityLifecycle.ARMED in observed
    assert observed[-1] is OpportunityLifecycle.INVALIDATED

    order = {
        OpportunityLifecycle.WATCHING: 0,
        OpportunityLifecycle.ARMED: 1,
        OpportunityLifecycle.ADMITTED: 2,
        OpportunityLifecycle.INVALIDATED: 3,
        OpportunityLifecycle.EXPIRED: 3,
    }
    ranks = [order[lifecycle] for lifecycle in observed if lifecycle is not None]
    assert ranks == sorted(ranks), "a non-terminal lifecycle may never regress"
    assert {step.opportunity_id for step in trace.steps if step.opportunity_id} == {
        trace.steps[2].opportunity_id
    }


def test_entry_step_carries_a_priceable_proposal_not_a_veto() -> None:
    trace = _trace()
    (enter,) = [step for step in trace.steps if step.action is DecisionAction.ENTER]

    assert enter.entry_intent_id is not None
    assert enter.entry_mode in {"MARKET_NOW", "LIMIT_WAIT"}
    assert enter.entry_reason == "ENTRY_READY"
    assert enter.lifecycle is OpportunityLifecycle.ARMED
    assert enter.allocation_outcome == "ADMITTED"
    assert enter.allocation_id is not None
    assert enter.requires_risk_permission is False  # an entry intent carries none
    assert enter.advisory_only is True


def test_management_steps_carry_advisories_and_protective_proposals() -> None:
    trace = _trace()
    managed = [
        step
        for step in trace.steps
        if step.action in (DecisionAction.HOLD, DecisionAction.REDUCE, DecisionAction.EXIT)
    ]

    assert [step.action for step in managed] == [
        DecisionAction.HOLD,
        DecisionAction.REDUCE,
        DecisionAction.EXIT,
    ]
    for step in managed:
        assert step.management_id is not None
        assert step.advisory_action is not None
        assert step.advisory_only is True
    exit_step = managed[-1]
    assert exit_step.advisory_action is DecisionAction.EXIT
    assert "OPPORTUNITY_INVALIDATED" in {code.value for code in exit_step.reason_codes}
    # An exit whose stop the market already passed proposes no protection: there is
    # nothing left to place, so the envelope carries no proposal id.
    assert exit_step.advisory_proposal_ids == ()
    assert exit_step.requires_risk_permission is False

    # A protective proposal is never submitted: it says the risk plane must permit it.
    for step in managed:
        if step.advisory_action is DecisionAction.EXIT:
            continue
        assert step.requires_risk_permission is True
        assert step.advisory_proposal_ids


# --------------------------------------------------------------------------- #
# zero native side effects
# --------------------------------------------------------------------------- #
def test_trace_reports_no_native_side_effects() -> None:
    trace = _trace()

    assert trace.native_side_effects == ()
    assert isinstance(trace.native_side_effects, tuple)
    assert trace.parity.unchanged is True
    assert trace.parity.verification == "SHADOW_UNMUTATED"
    assert trace.parity.reference == trace.parity.observed
    assert trace.parity.shadow is not None


def test_native_control_matches_the_policy_off_capture_around_the_run() -> None:
    trace = _trace()
    policy_off = sp.capture_replay(_native_control(), policy_state=sp.POLICY_OFF)

    assert trace.parity.reference == sp.pin(policy_off)
    assert trace.parity.observed == sp.pin(policy_off)
    assert trace.parity.policy_state == sp.POLICY_SHADOW
    assert trace.parity.native_streams == sp.NATIVE_STREAMS
    for stream in sp.NATIVE_STREAMS:
        assert policy_off.digest(stream) is not None

    # The shadow signature built by the slice is *not* native and proves it.
    shadow = sp.ReplaySignature.model_validate(json.loads(trace.parity.shadow or "{}"))
    sp.assert_shadow_unmutated(policy_off, shadow)
    assert shadow.decision_ids == trace.receipt_ids


def test_trace_model_refuses_a_native_side_effect() -> None:
    trace = _trace()
    payload = trace.model_dump()
    payload["native_side_effects"] = ("order-0001",)
    with pytest.raises(ValidationError, match="native side effects"):
        ShadowTrace(**payload)


def test_slice_module_imports_no_execution_surface() -> None:
    source = Path(slice_module.__file__).read_text()
    tree = ast.parse(source)
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module)
    forbidden = ("execution", "broker", "nautilus", "binance", "okx", "ccxt", "venue_client")
    assert not [name for name in imported if any(token in name for token in forbidden)]
    verbs = ("place_", "submit_", "execute_", "send_", "cancel_", "amend_")
    assert not [name for name in dir(slice_module) if name.startswith(verbs)]
    assert not hasattr(slice_module, "native_side_effects")


# --------------------------------------------------------------------------- #
# point-in-time validity of every link
# --------------------------------------------------------------------------- #
def test_every_trace_link_is_pit_valid() -> None:
    trace = _trace()

    for step in trace.steps:
        assert step.max_input_available_at <= step.cut_time_ns
        for value in (
            step.position_available_at_ns,
            step.quote_available_at_ns,
            step.protection_available_at_ns,
            step.risk_as_of_ns,
        ):
            assert value is None or value <= step.cut_time_ns
    cuts = [step.cut_time_ns for step in trace.steps]
    assert cuts == sorted(cuts)
    assert len(set(cuts)) == len(cuts)


def test_trace_step_model_rejects_a_future_input() -> None:
    trace = _trace()
    payload = trace.steps[0].model_dump()
    payload["max_input_available_at"] = payload["cut_time_ns"] + 1
    with pytest.raises(ValidationError, match="after its cut"):
        TraceStep(**payload)

    payload = trace.steps[2].model_dump()
    payload["quote_available_at_ns"] = payload["cut_time_ns"] + 1
    with pytest.raises(ValidationError, match="after the decision cut"):
        TraceStep(**payload)


def test_evidence_covers_the_canonical_28_witnesses() -> None:
    trace = _trace()

    assert all(step.evidence_coverage == 28 for step in trace.steps)
    assert any(step.evidence_abstain_count > 0 for step in trace.steps)


# --------------------------------------------------------------------------- #
# determinism
# --------------------------------------------------------------------------- #
def test_two_runs_are_byte_identical() -> None:
    first = _trace().to_json()
    second = _trace().to_json()

    assert first == second
    assert " " not in first and "\n" not in first
    assert json.loads(first) == json.loads(second)


def test_reordering_independent_events_yields_an_equal_trace() -> None:
    script = _script()
    shuffled = list(script)
    random.Random(20261105).shuffle(shuffled)
    assert [str(event) for event in shuffled] != [str(event) for event in script]

    assert _trader().run(shuffled).to_json() == _trader().run(script).to_json()


def test_duplicate_events_are_refused_rather_than_ordered_by_input_position() -> None:
    bar = _bar(0)
    with pytest.raises(ShadowScriptError, match="ambiguous"):
        _trader().run([bar, bar])


def test_empty_script_and_missing_cut_fail_closed() -> None:
    with pytest.raises(ShadowScriptError, match="cannot be empty"):
        _trader().run([])
    with pytest.raises(sp.ShadowNotExercised):
        _trader().run([_bar(0)])


# --------------------------------------------------------------------------- #
# baseline parity with the adaptive layer disabled
# --------------------------------------------------------------------------- #
def test_disabled_adaptive_layer_produces_no_decision_and_no_native_drift() -> None:
    trace = _trader().run(_script(), adaptive=False)
    policy_off = sp.capture_replay(_native_control(), policy_state=sp.POLICY_OFF)

    assert trace.adaptive_layer is False
    assert trace.steps == ()
    assert trace.receipt_ids == ()
    assert trace.native_side_effects == ()
    assert trace.actions() == ()
    assert trace.parity.policy_state == sp.POLICY_OFF
    assert trace.parity.verification == "POLICY_OFF_CONTROL_UNCHANGED"
    assert trace.parity.reference == trace.parity.observed == sp.pin(policy_off)
    assert trace.parity.shadow is None

    payload = trace.model_dump()
    payload["steps"] = (_trace().steps[0].model_dump(),)
    with pytest.raises(ValidationError, match="disabled adaptive layer"):
        ShadowTrace(**payload)


# --------------------------------------------------------------------------- #
# adversarial: unreadable world never enters
# --------------------------------------------------------------------------- #
def test_stale_belief_cut_abstains_and_never_enters() -> None:
    trace = _trace()
    stale = [step for step in trace.steps if step.belief_status == "STALE"]

    assert stale, "the scripted tape must exercise the stale-belief cut"
    for step in stale:
        assert step.action in (DecisionAction.WAIT, DecisionAction.ABSTAIN)
        assert "BELIEF_STALE" in {code.value for code in step.reason_codes}
        assert step.entry_intent_id is None


def test_unknown_venue_cut_abstains_and_never_enters() -> None:
    trace = _trace()
    unknown = [step for step in trace.steps if step.venue_status is VenueOrderStatus.UNKNOWN]

    assert unknown, "the scripted tape must exercise an unreadable venue"
    for step in unknown:
        assert step.action is DecisionAction.ABSTAIN
        assert "VENUE_UNKNOWN" in {code.value for code in step.reason_codes}
        assert step.entry_intent_id is None


def test_cut_on_an_instrument_with_no_reading_abstains() -> None:
    script = [
        CutUpdate(event_time_ns=BASE, available_at_ns=BASE, instruments=("ETHUSDT-PERP.BINANCE",))
    ]
    trace = _trader().run(script)

    (step,) = trace.steps
    assert step.action is DecisionAction.ABSTAIN
    assert step.position_presence is PositionPresence.UNCERTAIN
    assert step.evidence_coverage == 0
    assert step.entry_intent_id is None


def test_a_denied_batch_reading_blocks_the_portfolio_plane() -> None:
    """One account-wide plane, two instruments: the newest reading governs the batch.

    The instrument whose own reading permits still proposes an entry (the risk
    plane is not an entry veto), but the portfolio plane refuses to allocate under
    a denied batch reading instead of quietly spending capital.
    """
    other = "ETHUSDT-PERP.BINANCE"
    script = [
        event
        for event in _script()
        if not (isinstance(event, CutUpdate) and event.event_time_ns == BASE + 90 * M15)
    ]
    script.append(
        CutUpdate(
            event_time_ns=BASE + 90 * M15,
            available_at_ns=BASE + 90 * M15,
            instruments=(IID, other),
        )
    )
    script.append(
        RiskUpdate(
            event_time_ns=BASE + 90 * M15,
            available_at_ns=BASE + 90 * M15,
            instrument_id=other,
            observation=RiskObservation(
                status=RiskStatus.DENIED,
                reason="EXPOSURE_LIMIT",
                as_of_ns=BASE + 90 * M15,
                limits=_limits("1200"),
            ),
        )
    )
    trace = _trader().run(script)

    (enter,) = [step for step in trace.steps if step.action is DecisionAction.ENTER]
    assert enter.allocation_outcome == ALLOCATION_NOT_ATTEMPTED
    assert enter.allocation_id is None
    assert all(
        step.allocation_id is None
        for step in trace.steps
        if step.action is not DecisionAction.ENTER
    )


# --------------------------------------------------------------------------- #
# simultaneous candidates reach the portfolio plane together
# --------------------------------------------------------------------------- #
def _mirror(instrument_id: str) -> list[object]:
    """The same tape for a second instrument: nothing but identity changes."""
    events: list[object] = [
        PositionUpdate(
            event_time_ns=BASE,
            available_at_ns=BASE,
            instrument_id=instrument_id,
            presence=PositionPresence.NONE,
        ),
        VenueUpdate(
            event_time_ns=BASE,
            available_at_ns=BASE,
            instrument_id=instrument_id,
            status=VenueOrderStatus.CANCELED,
        ),
        ProtectionUpdate(
            event_time_ns=BASE,
            available_at_ns=BASE,
            instrument_id=instrument_id,
            state=ProtectionState.UNPROTECTED,
        ),
    ]
    for closed, equity in RISK_READINGS:
        events.append(
            RiskUpdate(
                event_time_ns=BASE + closed * M15,
                available_at_ns=BASE + closed * M15,
                instrument_id=instrument_id,
                observation=RiskObservation(
                    status=RiskStatus.AVAILABLE,
                    reason="PORTFOLIO_FEASIBLE",
                    as_of_ns=BASE + closed * M15,
                    limits=_limits(equity),
                ),
            )
        )
    for index in range(LAST_BAR):
        events.append(_bar(index).model_copy(update={"instrument_id": instrument_id}))
    for index in range(84, LAST_BAR):
        events.append(_quote(index).model_copy(update={"instrument_id": instrument_id}))
    for status, closed in (
        (OpportunityStatus.CANDIDATE, 20),
        (OpportunityStatus.CONFIRMED, 88),
    ):
        events.append(
            _opportunity(status, BASE + closed * M15).model_copy(
                update={"instrument_id": instrument_id}
            )
        )
    return events


def test_simultaneous_candidates_are_allocated_by_the_portfolio_plane() -> None:
    other = "ETHUSDT-PERP.BINANCE"
    script = [
        event
        for event in _script()
        if not (isinstance(event, CutUpdate) and event.event_time_ns == BASE + 90 * M15)
    ]
    script.append(
        CutUpdate(
            event_time_ns=BASE + 90 * M15,
            available_at_ns=BASE + 90 * M15,
            instruments=(IID, other),
        )
    )
    # The second instrument needs its own belief history, so it is cut at the same
    # early instants the first one is.
    script.extend(
        CutUpdate(
            event_time_ns=BASE + closed * M15,
            available_at_ns=BASE + closed * M15,
            instruments=(other,),
        )
        for closed in (4, 12, 20)
    )
    script.extend(_mirror(other))
    trace = _trader().run(script)

    entries = [step for step in trace.steps if step.action is DecisionAction.ENTER]
    assert {step.instrument_id for step in entries} == {IID, other}
    assert len(entries) == 2
    (allocation_ids,) = {step.allocation_id for step in entries}
    assert allocation_ids is not None
    assert {step.allocation_outcome for step in entries} == {"ADMITTED"}
    assert all(step.entry_intent_id is not None for step in entries)
    assert [step.cut_time_ns for step in entries] == [BASE + 90 * M15, BASE + 90 * M15]


# --------------------------------------------------------------------------- #
# fail-closed construction
# --------------------------------------------------------------------------- #
def test_constructor_rejects_a_non_decimal_fee() -> None:
    with pytest.raises(TypeError, match="must be Decimal"):
        ShadowTrader(native_control=_native_control(), fee_bps=4.0)  # type: ignore[arg-type]


def test_open_position_reading_must_carry_the_thesis_inputs() -> None:
    payload = {
        "event_time_ns": BASE,
        "available_at_ns": BASE,
        "instrument_id": IID,
        "presence": PositionPresence.OPEN,
        "quantity": Decimal("1"),
        "exposure": Decimal("100"),
    }
    with pytest.raises(ValidationError, match="side and initial stop"):
        PositionUpdate(**payload)


def test_protected_reading_must_name_its_stop() -> None:
    with pytest.raises(ValidationError, match="must name its stop price"):
        ProtectionUpdate(
            event_time_ns=BASE,
            available_at_ns=BASE,
            instrument_id=IID,
            state=ProtectionState.PROTECTED,
        )


def test_future_market_data_is_refused_at_construction() -> None:
    with pytest.raises(ValidationError, match="availability cannot precede"):
        QuoteUpdate(
            event_time_ns=BASE + M15,
            available_at_ns=BASE,
            instrument_id=IID,
            mark=Decimal("100"),
        )
