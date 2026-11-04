"""Agent-efficiency benchmark over a shared task list (F-03).

Two treatments, one task list, one budget: the only honest way to say a change to the
factory helped is to run both arms on the *same* tasks under the *same* ceilings and compare
accepted work per hour. This module is the comparison, not the measurement: every number
here is supplied by the caller (a test session, a finished campaign), and anything the
caller did not supply is reported as ``NOT_MEASURED`` — never as zero, never as an estimate.

The gates come first on purpose. A treatment that broke correctness or blew the token budget
is not "faster"; it is disqualified, and :class:`BenchmarkVerdict` says so. Only when both
arms pass the correctness gate and stayed inside budget does throughput decide the winner.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from v8_next.factory.memory import MEASURED, NOT_MEASURED, Measurement, measured, not_measured
from v8_next.factory.worker import TaskBudget

__all__ = [
    "BenchmarkError",
    "BenchmarkReport",
    "BenchmarkVerdict",
    "GateState",
    "TaskSample",
    "TreatmentSummary",
    "run_benchmark",
]


class BenchmarkError(ValueError):
    """The two treatments cannot be compared as supplied."""


class GateState(StrEnum):
    """Tri-state gate outcome. ``NOT_MEASURED`` is not a pass."""

    PASS = "PASS"
    FAIL = "FAIL"
    NOT_MEASURED = NOT_MEASURED


class BenchmarkVerdict(StrEnum):
    """Which arm won, or why no comparison is sound."""

    BASELINE_BETTER = "BASELINE_BETTER"
    TREATMENT_BETTER = "TREATMENT_BETTER"
    TIE = "TIE"
    NOT_COMPARABLE = "NOT_COMPARABLE"


class TaskSample(BaseModel):
    """One task's measurement under one treatment. Any field may be unmeasured.

    ``accepted`` is owner review's answer (was the PR accepted?), not the worker's; a worker
    cannot grade its own work. ``correctness_ok`` is the correctness gate for that task.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    task_id: str
    accepted: bool | None = None
    correctness_ok: bool | None = None
    tokens: int | None = Field(default=None, ge=0)
    wall_ms: int | None = Field(default=None, ge=0)
    cpu_ms: int | None = Field(default=None, ge=0)
    peak_rss_kb: int | None = Field(default=None, ge=0)
    retries: int | None = Field(default=None, ge=0)
    notes: str | None = None

    @field_validator("task_id", mode="after")
    @classmethod
    def _non_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("BENCHMARK: task_id must be a non-empty string")
        return value.strip()


class TreatmentSummary(BaseModel):
    """Aggregated measurements for one arm of the benchmark."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    label: str
    tasks: int
    accepted: Measurement
    accepted_per_hour: Measurement
    tokens: Measurement
    tokens_per_accepted_task: Measurement
    retries: Measurement
    wall_hours: Measurement
    cpu_hours: Measurement
    memory_high_water_kb: Measurement
    correctness_gate: GateState
    budget_gate: GateState

    @property
    def eligible(self) -> bool:
        """An arm may only win when correctness passed and the budget was respected."""
        return (
            self.correctness_gate is GateState.PASS
            and self.budget_gate is GateState.PASS
        )


class BenchmarkReport(BaseModel):
    """Two-arm comparison over the same task list and the same budget."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    budget: TaskBudget
    task_ids: tuple[str, ...]
    baseline: TreatmentSummary
    treatment: TreatmentSummary
    verdict: BenchmarkVerdict
    winner: str | None
    basis: str

    def gates(self) -> Mapping[str, tuple[str, str]]:
        """Gate state per arm, keyed by label (``correctness``, ``budget``)."""
        return {
            arm.label: (arm.correctness_gate.value, arm.budget_gate.value)
            for arm in (self.baseline, self.treatment)
        }


@dataclass(frozen=True, slots=True)
class _Totals:
    """Raw caller-supplied aggregates before the fail-closed measurement pass."""

    tasks: int
    accepted: int | None
    tokens: int | None
    wall_ms: int | None
    cpu_ms: int | None
    rss_kb: int | None
    retries: int | None
    correctness: GateState


def _unique_task_ids(samples: Sequence[TaskSample]) -> tuple[str, ...]:
    seen: list[str] = []
    for sample in samples:
        if sample.task_id in seen:
            raise BenchmarkError(
                f"BENCHMARK: duplicate task {sample.task_id!r} in one treatment"
            )
        seen.append(sample.task_id)
    return tuple(seen)


def _sum_field(samples: Sequence[TaskSample], name: str) -> int | None:
    """Sum a metric, or ``None`` when *any* sample left it unmeasured."""
    values: list[int] = []
    for sample in samples:
        value = getattr(sample, name)
        if value is None:
            return None
        values.append(int(value))
    return sum(values)


def _max_field(samples: Sequence[TaskSample], name: str) -> int | None:
    values: list[int] = []
    for sample in samples:
        value = getattr(sample, name)
        if value is None:
            return None
        values.append(int(value))
    return max(values)


def _gate(samples: Sequence[TaskSample], name: str) -> GateState:
    states = [getattr(sample, name) for sample in samples]
    if any(state is False for state in states):
        return GateState.FAIL
    if states and all(state is True for state in states):
        return GateState.PASS
    return GateState.NOT_MEASURED


def _totals(label: str, samples: Sequence[TaskSample]) -> _Totals:
    if not samples:
        raise BenchmarkError(f"BENCHMARK: treatment {label!r} has no samples")
    accepted_values = [sample.accepted for sample in samples]
    accepted = (
        None
        if any(value is None for value in accepted_values)
        else sum(1 for value in accepted_values if value)
    )
    return _Totals(
        tasks=len(samples),
        accepted=accepted,
        tokens=_sum_field(samples, "tokens"),
        wall_ms=_sum_field(samples, "wall_ms"),
        cpu_ms=_sum_field(samples, "cpu_ms"),
        rss_kb=_max_field(samples, "peak_rss_kb"),
        retries=_sum_field(samples, "retries"),
        correctness=_gate(samples, "correctness_ok"),
    )


def _hours(total_ms: int | None) -> Measurement:
    if total_ms is None:
        return not_measured()
    return measured(total_ms / 3_600_000.0)


def _budget_gate(totals: _Totals, budget: TaskBudget) -> GateState:
    if (
        totals.tokens is None
        or totals.wall_ms is None
        or totals.cpu_ms is None
        or totals.rss_kb is None
    ):
        return GateState.NOT_MEASURED
    over = (
        totals.tokens > budget.max_tokens
        or totals.wall_ms > budget.max_wall_s * 1000
        or totals.cpu_ms > budget.max_cpu_s * 1000
        or totals.rss_kb > budget.max_rss_kb
    )
    return GateState.FAIL if over else GateState.PASS


def _summary(label: str, samples: Sequence[TaskSample], budget: TaskBudget) -> TreatmentSummary:
    totals = _totals(label, samples)
    wall_hours = _hours(totals.wall_ms)
    accepted = not_measured() if totals.accepted is None else measured(totals.accepted)
    tokens = not_measured() if totals.tokens is None else measured(totals.tokens)
    retries = not_measured() if totals.retries is None else measured(totals.retries)
    rss = not_measured() if totals.rss_kb is None else measured(totals.rss_kb)
    accepted_per_hour = not_measured()
    if (
        totals.accepted is not None
        and isinstance(wall_hours.value, float)
        and wall_hours.value > 0
    ):
        accepted_per_hour = measured(totals.accepted / wall_hours.value)
    tokens_per_accepted = not_measured()
    if (
        totals.tokens is not None
        and totals.accepted is not None
        and totals.accepted > 0
    ):
        tokens_per_accepted = measured(totals.tokens / totals.accepted)
    return TreatmentSummary(
        label=label,
        tasks=totals.tasks,
        accepted=accepted,
        accepted_per_hour=accepted_per_hour,
        tokens=tokens,
        tokens_per_accepted_task=tokens_per_accepted,
        retries=retries,
        wall_hours=wall_hours,
        cpu_hours=_hours(totals.cpu_ms),
        memory_high_water_kb=rss,
        correctness_gate=totals.correctness,
        budget_gate=_budget_gate(totals, budget),
    )


def _disqualified(arm: TreatmentSummary) -> bool:
    """Known-bad: a gate failed. Different from a gate nobody could measure."""
    return arm.correctness_gate is GateState.FAIL or arm.budget_gate is GateState.FAIL


def _compare(
    baseline: TreatmentSummary, treatment: TreatmentSummary
) -> tuple[BenchmarkVerdict, str | None, str]:
    unknown = [
        arm.label
        for arm in (baseline, treatment)
        if not arm.eligible and not _disqualified(arm)
    ]
    if unknown:
        return BenchmarkVerdict.NOT_COMPARABLE, None, "gate"
    base_out = _disqualified(baseline)
    treat_out = _disqualified(treatment)
    if base_out and not treat_out:
        return BenchmarkVerdict.TREATMENT_BETTER, treatment.label, "gate"
    if treat_out and not base_out:
        return BenchmarkVerdict.BASELINE_BETTER, baseline.label, "gate"
    if base_out and treat_out:
        return BenchmarkVerdict.NOT_COMPARABLE, None, "gate"
    base_rate = baseline.accepted_per_hour
    treat_rate = treatment.accepted_per_hour
    if not (base_rate.is_measured and treat_rate.is_measured):
        return BenchmarkVerdict.NOT_COMPARABLE, None, "accepted_per_hour"
    assert isinstance(base_rate.value, float)
    assert isinstance(treat_rate.value, float)
    if treat_rate.value > base_rate.value:
        return BenchmarkVerdict.TREATMENT_BETTER, treatment.label, "accepted_per_hour"
    if treat_rate.value < base_rate.value:
        return BenchmarkVerdict.BASELINE_BETTER, baseline.label, "accepted_per_hour"
    return BenchmarkVerdict.TIE, None, "accepted_per_hour"


def run_benchmark(
    baseline: Sequence[TaskSample],
    treatment: Sequence[TaskSample],
    *,
    budget: TaskBudget,
    baseline_label: str = "baseline",
    treatment_label: str = "treatment",
) -> BenchmarkReport:
    """Compare two arms measured on the same task list under the same budget.

    Fails closed on anything that makes the comparison unsound: different task lists,
    duplicate tasks, an empty arm, an unmeasured gate, or an unmeasured accepted-per-hour
    rate. A treatment that fails correctness or exceeds the budget can never be declared the
    winner, and a treatment nobody could grade is ``NOT_COMPARABLE`` rather than a loss.
    """
    if not isinstance(budget, TaskBudget):
        raise TypeError("BENCHMARK: budget must be a TaskBudget")
    if baseline_label == treatment_label:
        raise BenchmarkError("BENCHMARK: the two treatments need distinct labels")
    base_ids = _unique_task_ids(baseline)
    treat_ids = _unique_task_ids(treatment)
    if not base_ids or not treat_ids:
        raise BenchmarkError("BENCHMARK: both treatments need at least one sampled task")
    if set(base_ids) != set(treat_ids):
        missing = sorted(set(base_ids) - set(treat_ids))
        extra = sorted(set(treat_ids) - set(base_ids))
        raise BenchmarkError(
            "BENCHMARK: treatments were not measured on the same task list"
            f" (missing={missing}, extra={extra})"
        )
    base_summary = _summary(baseline_label, baseline, budget)
    treat_summary = _summary(treatment_label, treatment, budget)
    verdict, winner, basis = _compare(base_summary, treat_summary)
    return BenchmarkReport(
        budget=budget,
        task_ids=base_ids,
        baseline=base_summary,
        treatment=treat_summary,
        verdict=verdict,
        winner=winner,
        basis=basis,
    )


def iter_report_rows(report: BenchmarkReport) -> Iterable[Mapping[str, Any]]:
    """Flatten the report for a table/evidence file (unmeasured values keep their marker)."""
    for arm in (report.baseline, report.treatment):
        yield {
            "treatment": arm.label,
            "tasks": arm.tasks,
            "accepted": arm.accepted.value,
            "accepted_per_hour": arm.accepted_per_hour.value,
            "tokens": arm.tokens.value,
            "tokens_per_accepted_task": arm.tokens_per_accepted_task.value,
            "retries": arm.retries.value,
            "cpu_hours": arm.cpu_hours.value,
            "memory_high_water_kb": arm.memory_high_water_kb.value,
            "correctness_gate": arm.correctness_gate.value,
            "budget_gate": arm.budget_gate.value,
            "measurement": {
                field: (MEASURED if getattr(arm, field).is_measured else NOT_MEASURED)
                for field in (
                    "accepted",
                    "accepted_per_hour",
                    "tokens",
                    "tokens_per_accepted_task",
                    "retries",
                    "cpu_hours",
                    "memory_high_water_kb",
                )
            },
        }
