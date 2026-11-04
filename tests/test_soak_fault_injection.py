"""M4-05: deterministic fault injection, restart recovery and resource honesty.

The soak harness is tested with synthetic loops and injected samplers; no wall-clock hours,
no live venue and no this-host memory. Each fault test asserts the *specific* invariant the
window is supposed to hold, not merely that the runner returned.
"""

from __future__ import annotations

import itertools
import json
from collections.abc import Callable, Iterator

import pytest
from pydantic import ValidationError

from v8_next.domain.decision import DecisionAction
from v8_next.factory.soak import (
    DEFAULT_RSS_GROWTH_LIMIT_BYTES_PER_SAMPLE,
    DecisionObservation,
    ExpectedInvariant,
    FaultKind,
    FaultPlan,
    FaultSchedule,
    SimulatedCrash,
    SoakError,
    SoakReport,
    SoakRunner,
    SoakStatus,
    StepContext,
    StepFunction,
    StepResult,
    default_plan,
)


def _make_runner(
    plan: FaultPlan,
    *,
    sample_every: int = 10,
    rss_sampler: Callable[[], int | None] | None = None,
) -> SoakRunner:
    sampler = rss_sampler if rss_sampler is not None else (lambda: 2_000_000)
    return SoakRunner(
        plan,
        sample_every=sample_every,
        rss_sampler=sampler,
        swap_sampler=lambda: None,
        disk_sampler=lambda: 8_000_000_000,
        cpu_sampler=lambda: 1.0,
    )


def _run(
    plan: FaultPlan,
    step: StepFunction,
    *,
    iterations: int = 10,
    target_s: float = 3600.0,
    sample_every: int = 10,
    rss_sampler: Callable[[], int | None] | None = None,
) -> SoakReport:
    runner = _make_runner(plan, sample_every=sample_every, rss_sampler=rss_sampler)
    return runner.run(step, iterations=iterations, duration_target_s=target_s)


def _wait(context: StepContext) -> StepResult:
    return StepResult(recovered=context.restart)


def _enter(context: StepContext) -> StepResult:
    return StepResult(
        decisions=(
            DecisionObservation(
                decision_id=f"dec-{context.index}",
                event_id=f"evt-{context.index}",
                action=DecisionAction.ENTER,
            ),
        ),
        recovered=context.restart,
    )


def _window(kind: FaultKind, start: int, end: int) -> FaultPlan:
    return FaultPlan(schedules=(FaultSchedule(kind=kind, start_index=start, end_index=end),))


def _violations(report: SoakReport) -> list[tuple[ExpectedInvariant, FaultKind | None]]:
    return [(item.invariant, item.fault) for item in report.invariant_violations]


def test_venue_unknown_refuses_enter() -> None:
    plan = _window(FaultKind.VENUE_UNKNOWN, 0, 4)
    report = _run(plan, _enter, iterations=10)
    assert _violations(report) == [
        (ExpectedInvariant.NO_ENTER, FaultKind.VENUE_UNKNOWN)
    ] * 5
    assert report.fail_closed is False
    safe = _run(plan, _wait, iterations=5)
    assert safe.invariant_violations == ()
    assert safe.fail_closed is True


def test_missed_mark_refuses_enter() -> None:
    plan = _window(FaultKind.MISSED_MARK, 2, 6)
    report = _run(plan, _enter, iterations=10)
    assert {fault for _, fault in _violations(report)} == {FaultKind.MISSED_MARK}
    assert [v.index for v in report.invariant_violations] == [2, 3, 4, 5, 6]
    assert all(v.invariant is ExpectedInvariant.NO_ENTER for v in report.invariant_violations)
    assert report.fail_closed is False


def test_disconnect_refuses_new_risk() -> None:
    plan = _window(FaultKind.DISCONNECT, 3, 7)

    def step(context: StepContext) -> StepResult:
        action = DecisionAction.ENTER if context.index % 2 else DecisionAction.EXIT
        return StepResult(
            decisions=(
                DecisionObservation(
                    decision_id=f"dec-{context.index}",
                    event_id=f"evt-{context.index}",
                    action=action,
                ),
            ),
            recovered=context.restart,
        )

    report = _run(plan, step, iterations=10)
    # ENTER at 3, 5, 7 only; EXIT is allowed while disconnected.
    assert [v.index for v in report.invariant_violations] == [3, 5, 7]
    assert all(v.invariant is ExpectedInvariant.NO_NEW_RISK for v in report.invariant_violations)
    assert all(v.fault is FaultKind.DISCONNECT for v in report.invariant_violations)


def test_protection_gap_refuses_protected_claim() -> None:
    plan = _window(FaultKind.PROTECTION_GAP, 4, 6)

    def protected(context: StepContext) -> StepResult:
        return StepResult(
            decisions=(
                DecisionObservation(
                    decision_id=f"dec-{context.index}",
                    event_id=f"evt-{context.index}",
                    action=DecisionAction.HOLD,
                    protected=True,
                ),
            ),
            recovered=context.restart,
        )

    report = _run(plan, protected, iterations=10)
    assert [v.index for v in report.invariant_violations] == [4, 5, 6]
    assert all(v.invariant is ExpectedInvariant.NO_PROTECTED for v in report.invariant_violations)
    assert all(v.fault is FaultKind.PROTECTION_GAP for v in report.invariant_violations)

    def unprotected(context: StepContext) -> StepResult:
        return StepResult(
            decisions=(
                DecisionObservation(
                    decision_id=f"dec-{context.index}",
                    event_id=f"evt-{context.index}",
                    action=DecisionAction.HOLD,
                ),
            ),
            recovered=context.restart,
        )

    assert _run(plan, unprotected, iterations=10).fail_closed is True


def test_duplicate_delivery_cannot_produce_duplicate_decision_ids() -> None:
    plan = _window(FaultKind.DUPLICATE_DELIVERY, 0, 9)
    delivered: set[str] = set()

    def deduping(context: StepContext) -> StepResult:
        event_id = f"evt-{context.index // 2}"  # every event is delivered twice
        if event_id in delivered:
            return StepResult(recovered=context.restart)
        delivered.add(event_id)
        return StepResult(
            decisions=(
                DecisionObservation(
                    decision_id=f"dec-{event_id}",
                    event_id=event_id,
                    action=DecisionAction.HOLD,
                ),
            ),
            recovered=context.restart,
        )

    report = _run(plan, deduping, iterations=10)
    assert report.duplicate_decision_ids == 0
    assert report.duplicates == ()
    assert report.invariant_violations == ()
    assert report.fail_closed is True


def test_duplicate_delivery_is_flagged_when_the_trader_reemits() -> None:
    plan = _window(FaultKind.DUPLICATE_DELIVERY, 0, 9)

    def naive(context: StepContext) -> StepResult:
        event_id = f"evt-{context.index // 2}"
        return StepResult(
            decisions=(
                DecisionObservation(
                    decision_id=f"dec-{event_id}",  # deterministic id, re-emitted
                    event_id=event_id,
                    action=DecisionAction.HOLD,
                ),
            ),
            recovered=context.restart,
        )

    report = _run(plan, naive, iterations=10)
    assert report.duplicate_decision_ids == 5
    assert len({d.decision_id for d in report.duplicates}) == 5
    assert all(
        violation.invariant is ExpectedInvariant.NO_DUPLICATE_DECISION
        and violation.fault is FaultKind.DUPLICATE_DELIVERY
        for violation in report.invariant_violations
    )
    assert len(report.invariant_violations) == 5
    assert report.fail_closed is False


def test_crash_restart_requires_recovered_state() -> None:
    plan = _window(FaultKind.CRASH_RESTART, 3, 5)

    def not_recovered(context: StepContext) -> StepResult:
        return StepResult(recovered=False)

    report = _run(plan, not_recovered, iterations=8)
    assert len(report.invariant_violations) == 1
    violation = report.invariant_violations[0]
    assert violation.index == 3
    assert violation.invariant is ExpectedInvariant.RECOVERED_STATE
    assert violation.fault is FaultKind.CRASH_RESTART
    assert report.fail_closed is False

    recovered = _run(plan, _wait, iterations=8)
    assert recovered.invariant_violations == ()
    assert recovered.duplicate_decision_ids == 0
    assert recovered.crash_restarts == 1
    assert recovered.fail_closed is True


def test_slow_consumer_refuses_unbounded_or_unmeasured_backlog() -> None:
    plan = FaultPlan(
        schedules=(
            FaultSchedule(kind=FaultKind.SLOW_CONSUMER, start_index=0, end_index=4),
        ),
        max_backlog=4,
    )

    def backlogged(context: StepContext) -> StepResult:
        return StepResult(backlog=5, recovered=context.restart)

    report = _run(plan, backlogged, iterations=5)
    assert [v.index for v in report.invariant_violations] == [0, 1, 2, 3, 4]
    assert all(
        v.invariant is ExpectedInvariant.BOUNDED_BACKLOG and v.fault is FaultKind.SLOW_CONSUMER
        for v in report.invariant_violations
    )

    def unmeasured(context: StepContext) -> StepResult:
        return StepResult(backlog=None, recovered=context.restart)

    unmeasured_report = _run(plan, unmeasured, iterations=2)
    assert all(
        v.invariant is ExpectedInvariant.BOUNDED_BACKLOG
        for v in unmeasured_report.invariant_violations
    )
    assert unmeasured_report.fail_closed is False

    def bounded(context: StepContext) -> StepResult:
        return StepResult(backlog=4, recovered=context.restart)

    assert _run(plan, bounded, iterations=5).fail_closed is True


def test_simulated_crash_mid_loop_recovers_without_duplicate_decisions() -> None:
    emitted: list[str] = []

    class RecoveringTrader:
        def __init__(self) -> None:
            self.seen: set[str] = set()
            self.crashed = False

        def __call__(self, context: StepContext) -> StepResult:
            if context.index == 4 and not self.crashed and not context.restart:
                self.crashed = True
                self.seen.clear()  # transient state dies with the process
                raise SimulatedCrash
            if context.restart:
                self.seen = set(context.journal.ids)  # recovery from the durable journal
            decision_id = f"dec-{context.index}"
            if decision_id in self.seen:
                return StepResult(recovered=True)
            self.seen.add(decision_id)
            emitted.append(decision_id)
            return StepResult(
                decisions=(
                    DecisionObservation(
                        decision_id=decision_id,
                        event_id=f"evt-{context.index}",
                        action=DecisionAction.HOLD,
                    ),
                ),
                recovered=context.restart,
            )

    report = _run(FaultPlan(), RecoveringTrader(), iterations=8)
    assert report.crash_restarts == 1
    assert report.duplicate_decision_ids == 0
    assert report.invariant_violations == ()
    assert report.fail_closed is True
    assert report.iterations == 8
    assert emitted == [f"dec-{index}" for index in range(8)]
    assert len(set(emitted)) == len(emitted)


def test_rss_growth_detector_flags_a_leak_and_accepts_a_stable_loop() -> None:
    counter: Iterator[int] = itertools.count()

    def leaky() -> int:
        return 1_000_000 + 100_000 * next(counter)

    leak = _run(
        FaultPlan(),
        _wait,
        iterations=60,
        sample_every=1,
        rss_sampler=leaky,
    )
    assert leak.memory_growth_detected is True
    assert leak.rss_growth_slope_bytes_per_sample == pytest.approx(100_000)
    assert leak.rss_growth_limit_bytes_per_sample == DEFAULT_RSS_GROWTH_LIMIT_BYTES_PER_SAMPLE
    assert leak.peak_rss_bytes == 1_000_000 + 100_000 * 59
    assert leak.fail_closed is False

    stable = _run(
        FaultPlan(),
        _wait,
        iterations=60,
        sample_every=1,
        rss_sampler=lambda: 5_000_000,
    )
    assert stable.memory_growth_detected is False
    assert stable.rss_growth_slope_bytes_per_sample == pytest.approx(0.0)
    assert stable.peak_rss_bytes == 5_000_000
    assert stable.median_rss_bytes == 5_000_000
    assert stable.fail_closed is True


def test_short_run_is_never_complete() -> None:
    report = _run(FaultPlan(), _wait, iterations=10, target_s=24 * 3600)
    assert report.soak_status is SoakStatus.SHORT_RUN
    assert report.duration_actual_s < report.duration_target_s
    assert report.duration_target_s == 24 * 3600

    payload = report.model_dump(mode="json")
    payload["soak_status"] = SoakStatus.COMPLETE.value
    with pytest.raises(ValidationError, match="short run cannot claim COMPLETE"):
        SoakReport.model_validate(payload)

    empty = _run(FaultPlan(), _wait, iterations=0, target_s=24 * 3600)
    assert empty.soak_status is SoakStatus.NOT_RUN
    assert empty.iterations == 0


def test_report_refuses_a_fabricated_growth_verdict() -> None:
    report = _run(
        FaultPlan(),
        _wait,
        iterations=30,
        sample_every=1,
        rss_sampler=lambda: 1_000_000,
    )
    payload = report.model_dump(mode="json")
    payload["memory_growth_detected"] = True
    with pytest.raises(ValidationError, match="memory_growth_detected"):
        SoakReport.model_validate(payload)


def test_live_authority_claim_is_a_fail_closed_violation() -> None:
    def live(context: StepContext) -> StepResult:
        return StepResult(
            decisions=(
                DecisionObservation(
                    decision_id=f"dec-{context.index}",
                    event_id=f"evt-{context.index}",
                    action=DecisionAction.ENTER,
                    live_authority=True,
                ),
            ),
            recovered=context.restart,
        )

    report = _run(FaultPlan(), live, iterations=3)
    assert report.live_authority_attempts == 3
    assert all(
        v.invariant is ExpectedInvariant.NO_LIVE_AUTHORITY for v in report.invariant_violations
    )
    assert all(v.fault is None for v in report.invariant_violations)
    assert report.fail_closed is False


def test_fault_schedule_cannot_weaken_its_invariant() -> None:
    with pytest.raises(ValidationError, match="cannot declare a different invariant"):
        FaultSchedule(
            kind=FaultKind.PROTECTION_GAP,
            start_index=0,
            end_index=1,
            expected_invariant=ExpectedInvariant.NO_ENTER,
        )
    schedule = FaultSchedule(kind=FaultKind.PROTECTION_GAP, start_index=0, end_index=1)
    assert schedule.invariant is ExpectedInvariant.NO_PROTECTED
    assert schedule.covers(1) and not schedule.covers(2)


def test_plan_windows_are_deterministic_and_inclusive() -> None:
    plan = default_plan(70)
    assert plan.kinds_at(0) == (FaultKind.VENUE_UNKNOWN,)
    assert plan.kinds_at(69) == (FaultKind.SLOW_CONSUMER,)
    assert plan.active_at(-1) == ()
    assert default_plan(70).digest() == plan.digest()
    assert default_plan(0) == FaultPlan()


def test_runner_rejects_invalid_configuration() -> None:
    with pytest.raises(SoakError, match="sample_every"):
        SoakRunner(FaultPlan(), sample_every=0)
    with pytest.raises(SoakError, match="growth limit"):
        SoakRunner(FaultPlan(), rss_growth_limit_bytes_per_sample=0)
    with pytest.raises(SoakError, match="hardware"):
        SoakRunner(FaultPlan(), hardware_profile="  ")


def test_cli_reports_short_run(capsys: pytest.CaptureFixture[str]) -> None:
    from v8_next.factory.soak import main

    assert main(["--duration", "3600", "--iterations", "5", "--sample-every", "1"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["soak_status"] == SoakStatus.SHORT_RUN.value
    assert payload["duration_actual_s"] < payload["duration_target_s"]
    assert payload["iterations"] == 5
    assert payload["declared_hardware_profile"]
    assert payload["rss_source"]
