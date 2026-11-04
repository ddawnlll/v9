"""F-03: durable research memory and the two-arm agent-efficiency benchmark.

The memory tests run against a real SQLite file under ``tmp_path`` and reopen it to prove
that negative outcomes survive a process restart. The benchmark tests feed caller-supplied
measurements only, so every "NOT_MEASURED" assertion below is a real fail-closed path.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from v8_next.factory.benchmark import (
    BenchmarkError,
    BenchmarkVerdict,
    GateState,
    TaskSample,
    iter_report_rows,
    run_benchmark,
)
from v8_next.factory.memory import (
    MEASURED,
    NOT_MEASURED,
    FindingOutcome,
    Measurement,
    MeasurementState,
    MemoryKind,
    ResearchMemory,
)
from v8_next.factory.worker import TaskBudget

PR_URL = "https://example.invalid/ddawnlll/v9/pull/99"


def _memory(tmp_path: Path) -> ResearchMemory:
    return ResearchMemory(tmp_path / "memory.sqlite3")


def test_record_is_replay_idempotent(tmp_path: Path) -> None:
    memory = _memory(tmp_path)
    first = memory.record(
        "i1",
        MemoryKind.ATTEMPT,
        attempt=1,
        worker_id="w1",
        tokens=100,
        provenance={"code_sha": "abc"},
        recorded_ns=5,
    )
    replay = memory.record(
        "i1",
        MemoryKind.ATTEMPT,
        attempt=1,
        worker_id="w1",
        tokens=100,
        provenance={"code_sha": "abc"},
        recorded_ns=999,
    )
    assert replay.event_id == first.event_id
    assert replay.recorded_ns == 5  # the stored row, not the retry's clock
    assert len(memory.events("i1")) == 1
    memory.close()


def test_distinct_attempts_are_distinct_rows(tmp_path: Path) -> None:
    memory = _memory(tmp_path)
    one = memory.record("i1", MemoryKind.ATTEMPT, attempt=1)
    two = memory.record("i1", MemoryKind.ATTEMPT, attempt=2)
    assert one.event_id != two.event_id
    assert [event.attempt for event in memory.events("i1")] == [1, 2]
    memory.close()


def test_negative_outcomes_survive_restart(tmp_path: Path) -> None:
    memory = _memory(tmp_path)
    memory.record("i1", MemoryKind.OUTCOME, attempt=1, outcome=FindingOutcome.BLOCKED,
                  detail="STEP_FAILED:verify")
    memory.record("i2", MemoryKind.OUTCOME, attempt=1, outcome=FindingOutcome.NO_EDGE,
                  detail="edge absent after 3 folds")
    memory.record("i3", MemoryKind.OUTCOME, attempt=1, outcome=FindingOutcome.INVALID,
                  detail="leakage in fold 2")
    memory.record("i3", MemoryKind.FINDING, attempt=1, outcome=FindingOutcome.NO_EDGE,
                  evidence={"folds": 3})
    memory.close()

    reopened = _memory(tmp_path)
    blocked = reopened.outcomes("i1")
    assert [event.outcome for event in blocked] == [FindingOutcome.BLOCKED]
    assert blocked[0].detail == "STEP_FAILED:verify"
    assert blocked[0].is_negative is True

    no_edge = reopened.outcomes("i2")
    assert no_edge[0].outcome is FindingOutcome.NO_EDGE
    assert no_edge[0].is_negative is True

    invalid = reopened.outcomes("i3")
    assert invalid[0].outcome is FindingOutcome.INVALID
    assert invalid[0].is_negative is True

    finding = reopened.findings("i3")
    assert len(finding) == 1
    assert finding[0].evidence == {"folds": 3}
    assert reopened.verified_findings("i3") == ()
    assert reopened.latest_outcome("i3") is not None
    reopened.close()


def test_findings_are_filtered_by_issue_and_outcome(tmp_path: Path) -> None:
    memory = _memory(tmp_path)
    memory.record("i1", MemoryKind.FINDING, attempt=1, outcome=FindingOutcome.VERIFIED,
                  detail="spread capture exists")
    memory.record("i1", MemoryKind.FINDING, attempt=2, outcome=FindingOutcome.NO_EDGE,
                  detail="no spread at 4 ticks")
    memory.record("i2", MemoryKind.FINDING, attempt=1, outcome=FindingOutcome.VERIFIED,
                  detail="unrelated")
    assert len(memory.findings("i1")) == 2
    assert [f.detail for f in memory.verified_findings("i1")] == ["spread capture exists"]
    assert [f.detail for f in memory.findings("i1", outcome=FindingOutcome.NO_EDGE)] == [
        "no spread at 4 ticks"
    ]
    assert memory.findings("i2")[0].issue_id == "i2"
    assert memory.issues() == ("i1", "i2")
    memory.close()


def test_correction_is_a_new_row_not_a_mutation(tmp_path: Path) -> None:
    memory = _memory(tmp_path)
    memory.record("i1", MemoryKind.FINDING, attempt=1, outcome=FindingOutcome.NO_EDGE,
                  detail="first read")
    memory.record("i1", MemoryKind.FINDING, attempt=1, outcome=FindingOutcome.VERIFIED,
                  detail="re-read after fixing the fold")
    assert [f.outcome for f in memory.findings("i1")] == [
        FindingOutcome.NO_EDGE,
        FindingOutcome.VERIFIED,
    ]
    memory.close()


def test_events_are_frozen(tmp_path: Path) -> None:
    memory = _memory(tmp_path)
    event = memory.record("i1", MemoryKind.ATTEMPT, attempt=1)
    with pytest.raises(ValidationError):
        event.recorded_ns = 1  # type: ignore[misc]
    memory.close()


def test_efficiency_counts_a_campaign(tmp_path: Path) -> None:
    memory = _memory(tmp_path)
    # i1: two attempts, verified, PR accepted.
    memory.record("i1", MemoryKind.ATTEMPT, attempt=1, worker_id="w1",
                  tokens=100, wall_ms=1_000, cpu_ms=500, peak_rss_kb=1_000)
    memory.record("i1", MemoryKind.ATTEMPT, attempt=2, worker_id="w1",
                  tokens=200, wall_ms=2_000, cpu_ms=1_000, peak_rss_kb=1_500)
    memory.record("i1", MemoryKind.OUTCOME, attempt=2, outcome=FindingOutcome.VERIFIED,
                  worker_id="w1", tokens=50, wall_ms=500, cpu_ms=250, peak_rss_kb=900,
                  pr_url=PR_URL, pr_accepted=True)
    memory.record("i1", MemoryKind.FINDING, attempt=2, outcome=FindingOutcome.VERIFIED,
                  tokens=0, wall_ms=0, cpu_ms=0, peak_rss_kb=100,
                  detail="carry edge verified")
    # i2: negative result.
    memory.record("i2", MemoryKind.ATTEMPT, attempt=1, worker_id="w2",
                  tokens=10, wall_ms=100, cpu_ms=50, peak_rss_kb=500)
    memory.record("i2", MemoryKind.OUTCOME, attempt=1, outcome=FindingOutcome.NO_EDGE,
                  worker_id="w2", tokens=5, wall_ms=50, cpu_ms=25, peak_rss_kb=400)
    # i3: blocked.
    memory.record("i3", MemoryKind.ATTEMPT, attempt=1, worker_id="w3",
                  tokens=1, wall_ms=10, cpu_ms=5, peak_rss_kb=100)
    memory.record("i3", MemoryKind.OUTCOME, attempt=1, outcome=FindingOutcome.BLOCKED,
                  worker_id="w3", tokens=0, wall_ms=0, cpu_ms=0, peak_rss_kb=200,
                  detail="unauthorized capability refused")

    report = memory.efficiency()
    assert report.events == 8
    assert report.issues == 3
    assert report.attempts == 4
    assert report.retries == 1
    assert report.verified_findings == 2
    assert report.findings == 1
    assert report.completed_issues == 1
    assert report.blocked == 1
    assert report.no_edge == 1
    assert report.invalid == 0
    assert report.exhausted == 0

    assert report.tokens.state is MeasurementState.MEASURED
    assert report.tokens.value == 366
    assert report.memory_high_water_kb.value == 1_500
    assert report.wall_hours.value == pytest.approx(3_660 / 3_600_000)
    assert report.cpu_hours.value == pytest.approx(1_830 / 3_600_000)
    assert report.findings_per_compute_hour.value == pytest.approx(
        2 / (1_830 / 3_600_000)
    )
    assert report.reported_prs == 1
    assert report.accepted_prs.value == 1

    # And it survives reopening, without recounting anything.
    memory.close()
    reopened = _memory(tmp_path)
    assert reopened.efficiency() == report
    reopened.close()


def test_missing_measurement_is_not_measured(tmp_path: Path) -> None:
    memory = _memory(tmp_path)
    memory.record("i1", MemoryKind.ATTEMPT, attempt=1, tokens=5)
    memory.record("i1", MemoryKind.OUTCOME, attempt=1, outcome=FindingOutcome.NO_EDGE)
    report = memory.efficiency()
    assert report.events == 2
    assert report.issues == 1
    assert report.tokens.state is MeasurementState.NOT_MEASURED
    assert report.tokens.value is None
    assert report.wall_hours.state is MeasurementState.NOT_MEASURED
    assert report.cpu_hours.state is MeasurementState.NOT_MEASURED
    assert report.memory_high_water_kb.state is MeasurementState.NOT_MEASURED
    assert report.findings_per_compute_hour.state is MeasurementState.NOT_MEASURED
    assert report.accepted_prs.state is MeasurementState.NOT_MEASURED
    memory.close()


def test_provenance_round_trips_across_restart(tmp_path: Path) -> None:
    memory = _memory(tmp_path)
    memory.record(
        "i1",
        MemoryKind.FINDING,
        attempt=1,
        outcome=FindingOutcome.VERIFIED,
        evidence={"executed_tests": ["tests/test_x.py::test_y"], "p_value": 0},
        provenance={
            "code_sha": "a" * 40,
            "config_sha": "b" * 40,
            "dataset_role": "DEVELOPMENT",
            "oos_access": "NONE",
        },
    )
    memory.close()
    reopened = _memory(tmp_path)
    finding = reopened.verified_findings("i1")[0]
    assert finding.provenance["code_sha"] == "a" * 40
    assert finding.provenance["dataset_role"] == "DEVELOPMENT"
    assert finding.provenance["oos_access"] == "NONE"
    assert finding.evidence["executed_tests"] == ["tests/test_x.py::test_y"]
    reopened.close()


def test_measured_cpu_with_zero_findings_reports_a_zero_rate(tmp_path: Path) -> None:
    memory = _memory(tmp_path)
    memory.record(
        "i1",
        MemoryKind.OUTCOME,
        attempt=1,
        outcome=FindingOutcome.NO_EDGE,
        tokens=1,
        wall_ms=100,
        cpu_ms=3_600_000,
        peak_rss_kb=10,
    )
    report = memory.efficiency()
    assert report.verified_findings == 0
    assert report.cpu_hours.value == pytest.approx(1.0)
    assert report.findings_per_compute_hour.state is MeasurementState.MEASURED
    assert report.findings_per_compute_hour.value == 0.0
    memory.close()


def test_empty_memory_reports_unmeasured_costs_and_zero_counts(tmp_path: Path) -> None:
    memory = _memory(tmp_path)
    report = memory.efficiency()
    assert report.events == 0
    assert report.issues == 0
    assert report.attempts == 0
    assert report.retries == 0
    assert report.verified_findings == 0
    assert report.blocked == 0
    assert report.no_edge == 0
    assert report.tokens == Measurement()
    assert report.tokens.state is MeasurementState.NOT_MEASURED
    memory.close()


@pytest.mark.parametrize(
    ("kwargs", "error"),
    [
        ({"kind": MemoryKind.ATTEMPT, "outcome": FindingOutcome.VERIFIED}, ValueError),
        ({"kind": MemoryKind.OUTCOME}, ValueError),
        ({"kind": MemoryKind.FINDING}, ValueError),
        ({"kind": MemoryKind.ATTEMPT, "tokens": -1}, ValueError),
        ({"kind": MemoryKind.ATTEMPT, "attempt": 0}, ValueError),
        ({"kind": "ATTEMPT"}, TypeError),
        ({"kind": MemoryKind.OUTCOME, "outcome": "VERIFIED"}, TypeError),
        ({"kind": MemoryKind.OUTCOME, "outcome": FindingOutcome.VERIFIED,
          "evidence": {"x": float("nan")}}, ValueError),
        ({"kind": MemoryKind.ATTEMPT, "issue": ""}, ValueError),
    ],
)
def test_record_rejects_invalid_rows(
    tmp_path: Path, kwargs: dict[str, object], error: type[BaseException]
) -> None:
    memory = _memory(tmp_path)
    issue = kwargs.pop("issue", "i1")
    with pytest.raises(error):
        memory.record(issue, **kwargs)  # type: ignore[arg-type]
    assert memory.events("i1") == ()
    memory.close()


# ---------------------------------------------------------------------------
# benchmark harness
# ---------------------------------------------------------------------------


def _sample(
    task_id: str,
    *,
    accepted: bool | None = True,
    correctness_ok: bool | None = True,
    tokens: int | None = 100,
    wall_ms: int | None = 600_000,
    cpu_ms: int | None = 300_000,
    peak_rss_kb: int | None = 1_024,
    retries: int | None = 0,
) -> TaskSample:
    return TaskSample(
        task_id=task_id,
        accepted=accepted,
        correctness_ok=correctness_ok,
        tokens=tokens,
        wall_ms=wall_ms,
        cpu_ms=cpu_ms,
        peak_rss_kb=peak_rss_kb,
        retries=retries,
    )


def _ids(*names: str) -> tuple[str, ...]:
    return names


def test_benchmark_reports_rates_tokens_retries_and_winner() -> None:
    budget = TaskBudget(max_tokens=10_000, max_wall_s=7_200)
    baseline = [
        _sample("t1", accepted=True, wall_ms=900_000),
        _sample("t2", accepted=True, wall_ms=900_000),
        _sample("t3", accepted=False, wall_ms=900_000),
        _sample("t4", accepted=False, wall_ms=900_000),
    ]
    treatment = [
        _sample("t1", accepted=True, wall_ms=450_000, tokens=80, retries=1),
        _sample("t2", accepted=True, wall_ms=450_000, tokens=80, retries=1),
        _sample("t3", accepted=True, wall_ms=450_000, tokens=80, retries=1),
        _sample("t4", accepted=True, wall_ms=450_000, tokens=80, retries=1),
    ]
    report = run_benchmark(baseline, treatment, budget=budget)

    assert report.task_ids == _ids("t1", "t2", "t3", "t4")
    assert report.baseline.accepted.value == 2
    assert report.baseline.accepted_per_hour.value == pytest.approx(2.0)
    assert report.treatment.accepted.value == 4
    assert report.treatment.accepted_per_hour.value == pytest.approx(8.0)
    assert report.baseline.tokens.value == 400
    assert report.treatment.tokens.value == 320
    assert report.treatment.tokens_per_accepted_task.value == pytest.approx(80.0)
    assert report.baseline.retries.value == 0
    assert report.treatment.retries.value == 4
    assert report.baseline.wall_hours.value == pytest.approx(1.0)
    assert report.treatment.cpu_hours.value == pytest.approx(1_200_000 / 3_600_000)
    assert report.baseline.correctness_gate is GateState.PASS
    assert report.treatment.correctness_gate is GateState.PASS
    assert report.baseline.budget_gate is GateState.PASS
    assert report.treatment.budget_gate is GateState.PASS
    assert report.gates() == {"baseline": ("PASS", "PASS"), "treatment": ("PASS", "PASS")}
    assert report.verdict is BenchmarkVerdict.TREATMENT_BETTER
    assert report.winner == "treatment"
    assert report.basis == "accepted_per_hour"


def test_benchmark_tie() -> None:
    samples = [_sample("t1"), _sample("t2")]
    report = run_benchmark(samples, list(samples), budget=TaskBudget())
    assert report.verdict is BenchmarkVerdict.TIE
    assert report.winner is None


def test_benchmark_rejects_different_task_lists() -> None:
    with pytest.raises(BenchmarkError):
        run_benchmark([_sample("t1"), _sample("t2")], [_sample("t1"), _sample("t3")],
                      budget=TaskBudget())


def test_benchmark_rejects_duplicate_tasks_and_empty_arms() -> None:
    with pytest.raises(BenchmarkError):
        run_benchmark([_sample("t1"), _sample("t1")], [_sample("t1")], budget=TaskBudget())
    with pytest.raises(BenchmarkError):
        run_benchmark([], [], budget=TaskBudget())
    with pytest.raises(BenchmarkError):
        run_benchmark([_sample("t1")], [_sample("t1")], budget=TaskBudget(),
                      baseline_label="arm", treatment_label="arm")


def test_benchmark_rejects_a_non_budget() -> None:
    with pytest.raises(TypeError):
        run_benchmark([_sample("t1")], [_sample("t1")], budget="cheap")  # type: ignore[arg-type]


def test_correctness_failure_disqualifies_a_faster_treatment() -> None:
    baseline = [_sample("t1", wall_ms=900_000), _sample("t2", wall_ms=900_000)]
    treatment = [
        _sample("t1", wall_ms=60_000),
        _sample("t2", wall_ms=60_000, correctness_ok=False),
    ]
    report = run_benchmark(baseline, treatment, budget=TaskBudget(max_wall_s=7_200))
    assert report.treatment.correctness_gate is GateState.FAIL
    assert report.treatment.eligible is False
    assert report.verdict is BenchmarkVerdict.BASELINE_BETTER
    assert report.winner == "baseline"
    assert report.basis == "gate"


def test_budget_overrun_disqualifies_a_faster_treatment() -> None:
    baseline = [_sample("t1", wall_ms=900_000), _sample("t2", wall_ms=900_000)]
    treatment = [
        _sample("t1", wall_ms=60_000, tokens=10_000),
        _sample("t2", wall_ms=60_000, tokens=10_000),
    ]
    report = run_benchmark(
        baseline, treatment, budget=TaskBudget(max_tokens=1_000, max_wall_s=7_200)
    )
    assert report.treatment.budget_gate is GateState.FAIL
    assert report.baseline.budget_gate is GateState.PASS
    assert report.verdict is BenchmarkVerdict.BASELINE_BETTER
    assert report.basis == "gate"


def test_missing_measurements_are_not_measured_and_not_comparable() -> None:
    baseline = [_sample("t1"), _sample("t2")]
    treatment = [_sample("t1", tokens=None, wall_ms=None), _sample("t2", tokens=None)]
    report = run_benchmark(baseline, treatment, budget=TaskBudget())

    assert report.treatment.tokens.state is MeasurementState.NOT_MEASURED
    assert report.treatment.tokens.value is None
    assert report.treatment.wall_hours.state is MeasurementState.NOT_MEASURED
    assert report.treatment.accepted_per_hour.state is MeasurementState.NOT_MEASURED
    assert report.treatment.budget_gate is GateState.NOT_MEASURED
    assert report.treatment.eligible is False
    # An unmeasurable arm is not a loss: the comparison is simply not sound.
    assert report.verdict is BenchmarkVerdict.NOT_COMPARABLE
    assert report.winner is None
    assert report.basis == "gate"

    rows = list(iter_report_rows(report))
    assert [row["treatment"] for row in rows] == ["baseline", "treatment"]
    assert rows[1]["tokens"] is None
    assert rows[1]["measurement"]["tokens"] == NOT_MEASURED
    assert rows[0]["measurement"]["tokens"] == MEASURED
    # two accepted tasks over 20 simulated minutes = 6 accepted per hour
    assert rows[0]["accepted_per_hour"] == pytest.approx(6.0)


def test_ungraded_correctness_is_not_a_pass() -> None:
    baseline = [_sample("t1", correctness_ok=None)]
    treatment = [_sample("t1", correctness_ok=None)]
    report = run_benchmark(baseline, treatment, budget=TaskBudget())
    assert report.baseline.correctness_gate is GateState.NOT_MEASURED
    assert report.baseline.eligible is False
    assert report.verdict is BenchmarkVerdict.NOT_COMPARABLE


def test_zero_wall_time_does_not_divide_by_zero() -> None:
    report = run_benchmark(
        [_sample("t1", wall_ms=0)], [_sample("t1", wall_ms=0)], budget=TaskBudget()
    )
    assert report.baseline.accepted_per_hour.state is MeasurementState.NOT_MEASURED
    assert report.verdict is BenchmarkVerdict.NOT_COMPARABLE


def test_partial_acceptance_is_unmeasured() -> None:
    report = run_benchmark(
        [_sample("t1", accepted=True)],
        [_sample("t1", accepted=None)],
        budget=TaskBudget(),
    )
    assert report.treatment.accepted.state is MeasurementState.NOT_MEASURED
    assert report.treatment.tokens_per_accepted_task.state is MeasurementState.NOT_MEASURED
