"""Versioned position lifecycle: policies are bounded, one-way and fail-closed."""

from __future__ import annotations

from decimal import Decimal

from v8_next.domain.decision import DecisionAction, VenueOrderStatus
from v8_next.domain.market import Candle
from v8_next.domain.protection import ProtectionState
from v8_next.economics.legality import PositionPresence
from v8_next.economics.management import ManagementDecision
from v8_next.economics.position_lifecycle import (
    LIFECYCLE_VERSION,
    InitialStop,
    StopPolicyV1,
    TargetPolicyV1,
    assess_position,
    build_position_thesis,
    structural_objective,
)
from v8_next.economics.thesis import (
    PositionSnapshot,
    StopKind,
    TargetPlan,
    ThesisBelief,
)

D = Decimal
BASE = 1_735_689_600_000_000_000
H1 = 3_600 * 10**9


def _bar(index: int, low: str, high: str) -> Candle:
    start = BASE + index * H1
    return Candle(
        instrument_id="BTCUSDT-PERP.BINANCE",
        start_ns=start,
        end_ns=start + H1,
        open=D("100"),
        high=D(high),
        low=D(low),
        close=D("100"),
        volume=D("1"),
        received_ns=start + H1,
        available_ns=start + H1,
        source_hash=f"b{index}",
    )


def _belief(regime: str = "TREND") -> ThesisBelief:
    return ThesisBelief(
        belief_id="a" * 64,
        version_hash="a" * 64,
        regime=regime,
        as_of_ns=BASE,
    )


def test_long_stop_is_structural_and_clamped() -> None:
    policy = StopPolicyV1(max_distance=D("0.10"))
    bars = [_bar(i, "95", "105") for i in range(24)]
    stop = policy.initial_stop(side="LONG", entry_price=D("100"), bars=bars)
    assert stop.kind is StopKind.STRUCTURAL
    assert stop.price == D("95")
    assert stop.distance == D("5")


def test_stop_distance_is_clamped_in_both_directions() -> None:
    policy = StopPolicyV1(min_distance=D("0.005"), max_distance=D("0.03"))
    tight = policy.initial_stop(
        side="LONG", entry_price=D("100"), bars=[_bar(0, "99.9", "100.1")]
    )
    wide = policy.initial_stop(
        side="LONG", entry_price=D("100"), bars=[_bar(0, "50", "100.1")]
    )
    assert tight.distance == D("0.5") and tight.basis == "CLAMPED_MIN_DISTANCE"
    assert wide.distance == D("3") and wide.basis == "CLAMPED_MAX_DISTANCE"


def test_stop_falls_back_without_history() -> None:
    stop = StopPolicyV1().initial_stop(side="SHORT", entry_price=D("100"), bars=[])
    assert stop.kind is StopKind.NATIVE_PROTECTIVE
    assert stop.price == D("102")
    assert stop.basis == "FALLBACK_BRACKET_PCT"


def test_initial_target_is_two_r_or_a_structural_objective() -> None:
    policy = TargetPolicyV1()
    plan = policy.initial_target(
        side="LONG", entry_price=D("100"), stop_price=D("95")
    )
    assert plan.target_price == D("110")
    assert plan.r_multiple == D("2")
    structural = policy.initial_target(
        side="LONG", entry_price=D("100"), stop_price=D("95"), objective=D("120")
    )
    assert structural.target_price == D("120")
    assert structural.r_multiple == D("4")


def test_target_extension_is_one_way_and_bounded() -> None:
    policy = TargetPolicyV1(max_amendments=2, min_extension_r=D("0.5"))
    common = dict(
        side="LONG",
        entry_price=D("100"),
        stop_price=D("95"),
        current_target=D("110"),
        mark_price=D("105"),
    )
    assert policy.amendment(objective=D("112.4"), amendments=0, **common) is None
    assert policy.amendment(objective=D("112.6"), amendments=0, **common) == D("112.6")
    assert policy.amendment(objective=D("120"), amendments=2, **common) is None
    # Never behind the mark.
    assert policy.amendment(objective=D("104"), amendments=0, **common) is None


def test_structural_objective_reads_the_prior_range() -> None:
    bars = [_bar(i, "90", str(100 + i)) for i in range(20)]
    assert structural_objective("LONG", bars) == D("119")
    assert structural_objective("SHORT", bars) == D("90")
    assert structural_objective("LONG", []) is None


def _thesis():
    stop = InitialStop(
        price=D("95"), kind=StopKind.STRUCTURAL, distance=D("5"), basis="TEST"
    )
    return build_position_thesis(
        entry_decision_id="d-1",
        opportunity_id="o-1",
        belief=_belief(),
        native_position_id="p-1",
        instrument_id="BTCUSDT-PERP.BINANCE",
        side="LONG",
        entry_price=D("100"),
        initial_stop=stop,
        quantity=D("1"),
        target=TargetPlan(target_price=D("110"), expected_bars=24, max_bars=96),
        created_ns=BASE,
    )


def _assessment(protection: ProtectionState) -> object:
    thesis = _thesis()
    snapshot = PositionSnapshot(
        native_position_id="p-1",
        quantity=D("1"),
        average_price=D("100"),
        mark_price=D("105"),
        protective_order_ids=("s-1",) if protection is ProtectionState.PROTECTED else (),
        elapsed_bars=5,
        available_at_ns=BASE + 5 * H1,
    )
    return assess_position(
        thesis=thesis,
        snapshot=snapshot,
        belief=_belief(),
        evidence=(),
        protection_state=protection,
        protective_stop_price=D("95") if protection is ProtectionState.PROTECTED else None,
        fee_bps=D("5"),
        lot_step=D("0.001"),
        tick_size=D("0.1"),
        target_policy=TargetPolicyV1(),
        objective=None,
        target_amendments=0,
        now_ns=BASE + 5 * H1,
        venue_status=VenueOrderStatus.ACCEPTED,
        position_presence=PositionPresence.OPEN,
        stale=False,
    )


def test_protected_intact_thesis_holds() -> None:
    result = _assessment(ProtectionState.PROTECTED)
    assert result.action is DecisionAction.HOLD
    assert result.management.protected is True
    assert result.management.admission_ok is True
    assert result.thesis.policy_version == LIFECYCLE_VERSION


def test_unprotected_position_fails_closed_to_exit() -> None:
    result = _assessment(ProtectionState.UNPROTECTED)
    management: ManagementDecision = result.management
    assert management.protected is False
    assert result.action is DecisionAction.EXIT
    assert management.admission_ok is False
