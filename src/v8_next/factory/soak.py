"""Long-running shadow soak harness with deterministic fault injection (M4-05).

A soak is only evidence when it runs the *same* shadow loop for the whole window under
injected venue/data/process failures and keeps sampling the process. This module is the
harness, not the trader: :class:`SoakRunner` drives a caller-supplied step function (the
production shadow loop is deliberately not wired here) and judges what that step emitted.
Nothing in this module trades, holds credentials or grants authority; every observation is
advisory and a ``live_authority`` claim is itself a fail-closed violation.

Determinism
-----------
Faults are declared as inclusive index windows in :class:`FaultPlan`; there is no randomness.
Given the same plan and step, the same indices see the same faults in the same order. The
journal of decision ids is the durable surface that survives a restart, so recovery is
observable rather than asserted: on a crash or a ``CRASH_RESTART`` window the step is handed
the surviving journal and must return ``recovered=True``.

What is measured, and what is not
---------------------------------
* **RSS** is read without ``psutil``. Linux reads current RSS from ``/proc/self/statm``;
  elsewhere ``resource.getrusage(RUSAGE_SELF).ru_maxrss`` is used, which on macOS is a peak
  high-water mark. A peak sampler can prove growth but cannot prove a leak shrank, so the
  report records ``rss_source``. A missing sample is ``None`` — never zero.
* **Swap** is sampled from ``/proc/meminfo`` and is ``None`` off Linux; the declared hardware
  profile reports the target, not a measurement.
* **CPU** is cumulative ``time.process_time()`` milliseconds; throughput is iterations per
  wall second.
* **Disk** is free bytes at the sampling root via :func:`shutil.disk_usage`.

Unbounded growth
----------------
``RSS_GROWTH_LIMIT_BYTES_PER_SAMPLE`` (64 KiB per sample index) is the documented ceiling on
the least-squares RSS slope. It is a slope over *sample indices*, not a peak: a loop whose RSS
climbs by more than 64 KiB per sampling step is treated as growing without bound. The
threshold and the fitted slope both travel in the report so a reader can disagree with the
verdict, not just trust it.

Status honesty
--------------
:class:`SoakReport` refuses to exist with a dishonest ``soak_status``: ``COMPLETE`` requires a
positive target and ``duration_actual_s >= duration_target_s``. A shorter run is
``SHORT_RUN``; a run that never executed is ``NOT_RUN``. A real 24-48 h shadow soak cannot be
produced in this environment (no production shadow loop, no multi-hour wall clock budget), so
it is reported as ``NOT_RUN`` in ``reports/overnight/evidence/M4-05.md``. The CLI exercises the
harness only and will normally emit ``SHORT_RUN``.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import resource
import shutil
import statistics
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, model_validator

from v8_next.domain.decision import DecisionAction
from v8_next.telemetry.identity import canonical_digest

__all__ = [
    "DEFAULT_HARDWARE_PROFILE",
    "DEFAULT_RSS_GROWTH_LIMIT_BYTES_PER_SAMPLE",
    "DEFAULT_SAMPLE_EVERY",
    "MIN_GROWTH_SAMPLES",
    "DecisionJournal",
    "DecisionObservation",
    "DuplicateDecision",
    "ExpectedInvariant",
    "FaultKind",
    "FaultPlan",
    "FaultSchedule",
    "InvariantViolation",
    "SoakError",
    "SoakReport",
    "SoakRunner",
    "SoakSample",
    "SoakStatus",
    "StepContext",
    "StepResult",
    "SimulatedCrash",
    "default_plan",
    "main",
    "read_cpu_ms",
    "read_disk_free_bytes",
    "read_rss_bytes",
    "read_swap_used_bytes",
]

#: Documented ceiling for the least-squares RSS slope, in bytes per sample index.
DEFAULT_RSS_GROWTH_LIMIT_BYTES_PER_SAMPLE = 64 * 1024

#: A slope is only fitted once this many RSS samples exist.
MIN_GROWTH_SAMPLES = 5

#: Default sampling cadence, in loop iterations.
DEFAULT_SAMPLE_EVERY = 100

#: The product target; the host this runs on is recorded separately when known.
DEFAULT_HARDWARE_PROFILE = (
    "declared target: 4 GiB RSS / 2 vCPU VPS; "
    "this harness measures the host it runs on and does not enforce a cgroup"
)


class SoakError(ValueError):
    """The soak configuration cannot produce an honest report."""


class FaultKind(StrEnum):
    """Deterministic fault windows injected into the shadow loop."""

    VENUE_UNKNOWN = "VENUE_UNKNOWN"
    MISSED_MARK = "MISSED_MARK"
    DISCONNECT = "DISCONNECT"
    PROTECTION_GAP = "PROTECTION_GAP"
    DUPLICATE_DELIVERY = "DUPLICATE_DELIVERY"
    CRASH_RESTART = "CRASH_RESTART"
    SLOW_CONSUMER = "SLOW_CONSUMER"


class ExpectedInvariant(StrEnum):
    """The fail-closed property a fault window asserts over emitted decisions."""

    NO_ENTER = "NO_ENTER"
    NO_NEW_RISK = "NO_NEW_RISK"
    NO_PROTECTED = "NO_PROTECTED"
    NO_DUPLICATE_DECISION = "NO_DUPLICATE_DECISION"
    RECOVERED_STATE = "RECOVERED_STATE"
    BOUNDED_BACKLOG = "BOUNDED_BACKLOG"
    NO_LIVE_AUTHORITY = "NO_LIVE_AUTHORITY"


DEFAULT_INVARIANT: Mapping[FaultKind, ExpectedInvariant] = {
    FaultKind.VENUE_UNKNOWN: ExpectedInvariant.NO_ENTER,
    FaultKind.MISSED_MARK: ExpectedInvariant.NO_ENTER,
    FaultKind.DISCONNECT: ExpectedInvariant.NO_NEW_RISK,
    FaultKind.PROTECTION_GAP: ExpectedInvariant.NO_PROTECTED,
    FaultKind.DUPLICATE_DELIVERY: ExpectedInvariant.NO_DUPLICATE_DECISION,
    FaultKind.CRASH_RESTART: ExpectedInvariant.RECOVERED_STATE,
    FaultKind.SLOW_CONSUMER: ExpectedInvariant.BOUNDED_BACKLOG,
}
"""One invariant per fault. The plan cannot weaken it (see :class:`FaultSchedule`)."""


class FaultSchedule(BaseModel):
    """One inclusive index window during which a fault is active."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: FaultKind
    start_index: int = Field(ge=0)
    end_index: int = Field(ge=0)
    expected_invariant: ExpectedInvariant | None = None

    @model_validator(mode="after")
    def _ordered_and_unweakened(self) -> FaultSchedule:
        if self.end_index < self.start_index:
            raise ValueError("SOAK_FAULT: end_index must be >= start_index")
        if self.expected_invariant is not None and (
            self.expected_invariant is not DEFAULT_INVARIANT[self.kind]
        ):
            raise ValueError(
                "SOAK_FAULT: a fault window cannot declare a different invariant than "
                f"{DEFAULT_INVARIANT[self.kind].value} for {self.kind.value}"
            )
        return self

    @property
    def invariant(self) -> ExpectedInvariant:
        """The invariant enforced for this window (never weaker than the default)."""
        return DEFAULT_INVARIANT[self.kind]

    def covers(self, index: int) -> bool:
        """Whether ``index`` falls inside the inclusive window."""
        return self.start_index <= index <= self.end_index


class FaultPlan(BaseModel):
    """Deterministic fault windows plus the backlog ceiling for a soak run."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schedules: tuple[FaultSchedule, ...] = ()
    max_backlog: int = Field(default=64, ge=0)

    def active_at(self, index: int) -> tuple[FaultSchedule, ...]:
        """Schedules active at ``index``, in declaration order."""
        return tuple(schedule for schedule in self.schedules if schedule.covers(index))

    def kinds_at(self, index: int) -> tuple[FaultKind, ...]:
        """Active fault kinds at ``index``, in declaration order."""
        return tuple(schedule.kind for schedule in self.active_at(index))

    def digest(self) -> str:
        """Content identity of the plan; identical plans reproduce the identical digest."""
        return canonical_digest("v8-next-soak-plan", self.model_dump(mode="json"))


def default_plan(
    iterations: int, *, kinds: Sequence[FaultKind] = tuple(FaultKind)
) -> FaultPlan:
    """Spread ``kinds`` over contiguous, disjoint windows of ``iterations`` steps."""
    if iterations <= 0 or not kinds:
        return FaultPlan()
    width = max(1, iterations // len(kinds))
    schedules: list[FaultSchedule] = []
    for position, kind in enumerate(kinds):
        start = position * width
        end = iterations - 1 if position == len(kinds) - 1 else min(iterations - 1, start + width - 1)
        if start > iterations - 1:
            break
        schedules.append(FaultSchedule(kind=kind, start_index=start, end_index=end))
    return FaultPlan(schedules=tuple(schedules))


@dataclass(frozen=True, slots=True)
class DecisionObservation:
    """One advisory decision emitted by the shadow loop under test.

    ``protected`` is the trader's claim that a live position carries protection; it is only
    ever *checked*, never acted on. ``live_authority`` marks a claim to trade for real and is
    a fail-closed violation regardless of the active fault window.
    """

    decision_id: str
    event_id: str
    action: DecisionAction
    protected: bool = False
    live_authority: bool = False


@dataclass(frozen=True, slots=True)
class StepResult:
    """What one shadow-loop iteration produced.

    ``backlog`` is the consumer depth *after* the step. ``None`` means unmeasured, which is
    not bounded: a ``SLOW_CONSUMER`` window treats it as a violation.
    """

    decisions: tuple[DecisionObservation, ...] = ()
    recovered: bool = False
    backlog: int | None = 0


class SimulatedCrash(RuntimeError):
    """Raised by a step function to model a process death at this iteration."""


class DecisionJournal:
    """Append-only durable surface of decision ids; the restart evidence.

    The runner owns the only writer (``_record``). Step functions receive this object and
    may only read it: membership, length and ``ids`` are the recovery surface.
    """

    __slots__ = ("_first_index", "_ids", "_seen")

    def __init__(self) -> None:
        self._ids: list[str] = []
        self._seen: set[str] = set()
        self._first_index: dict[str, int] = {}

    def __contains__(self, decision_id: object) -> bool:
        return decision_id in self._seen

    def __len__(self) -> int:
        return len(self._ids)

    @property
    def ids(self) -> tuple[str, ...]:
        """Every committed decision id, in first-emission order."""
        return tuple(self._ids)

    def first_index_of(self, decision_id: str) -> int | None:
        """Index at which ``decision_id`` was first committed, if any."""
        return self._first_index.get(decision_id)

    def _record(self, decision_id: str, index: int) -> int | None:
        """Commit ``decision_id``; return the first index when this is a duplicate."""
        if decision_id in self._seen:
            return self._first_index[decision_id]
        self._seen.add(decision_id)
        self._ids.append(decision_id)
        self._first_index[decision_id] = index
        return None


@dataclass(frozen=True, slots=True)
class StepContext:
    """Read-only view handed to the step function for one iteration."""

    index: int
    active_faults: tuple[FaultKind, ...]
    restart: bool
    generation: int
    journal: DecisionJournal


class InvariantViolation(BaseModel):
    """A fail-closed property the shadow loop broke, attributed when possible."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    index: int = Field(ge=0)
    fault: FaultKind | None
    invariant: ExpectedInvariant
    detail: str


class DuplicateDecision(BaseModel):
    """A decision id emitted more than once, with both indices."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    index: int = Field(ge=0)
    first_index: int = Field(ge=0)
    decision_id: str


class SoakSample(BaseModel):
    """One periodic measurement of the soak process."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    index: int = Field(ge=0)
    elapsed_s: float
    rss_bytes: int | None = None
    cpu_ms: float | None = None
    throughput_per_s: float | None = None
    swap_used_bytes: int | None = None
    disk_free_bytes: int | None = None
    backlog: int | None = None


class SoakStatus(StrEnum):
    """How much of the declared window actually ran."""

    COMPLETE = "COMPLETE"
    SHORT_RUN = "SHORT_RUN"
    NOT_RUN = "NOT_RUN"


def _status_for(iterations: int, target_s: float, actual_s: float) -> SoakStatus:
    if iterations <= 0 or target_s <= 0:
        return SoakStatus.NOT_RUN
    if actual_s >= target_s:
        return SoakStatus.COMPLETE
    return SoakStatus.SHORT_RUN


class SoakReport(BaseModel):
    """Frozen result of one soak run.

    The validators re-derive ``soak_status``, ``memory_growth_detected`` and ``fail_closed``
    from the measurements, so a caller cannot hand-build a report that claims more than the
    run observed.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    iterations: int = Field(ge=0)
    elapsed_s: float = Field(ge=0)
    duration_target_s: float = Field(ge=0)
    duration_actual_s: float = Field(ge=0)
    samples: tuple[SoakSample, ...] = ()
    peak_rss_bytes: int | None = None
    median_rss_bytes: int | None = None
    rss_growth_slope_bytes_per_sample: float | None = None
    rss_growth_limit_bytes_per_sample: int = DEFAULT_RSS_GROWTH_LIMIT_BYTES_PER_SAMPLE
    memory_growth_detected: bool = False
    duplicate_decision_ids: int = Field(default=0, ge=0)
    duplicates: tuple[DuplicateDecision, ...] = ()
    invariant_violations: tuple[InvariantViolation, ...] = ()
    crash_restarts: int = Field(default=0, ge=0)
    live_authority_attempts: int = Field(default=0, ge=0)
    throughput_per_s: float | None = None
    declared_hardware_profile: str
    rss_source: str
    soak_status: SoakStatus
    fail_closed: bool = True

    @model_validator(mode="after")
    def _honest(self) -> SoakReport:
        if self.soak_status is not _status_for(
            self.iterations, self.duration_target_s, self.duration_actual_s
        ):
            raise ValueError(
                "SOAK_REPORT: soak_status does not match the declared target and measured "
                "duration; a short run cannot claim COMPLETE"
            )
        if self.duplicate_decision_ids != len(self.duplicates):
            raise ValueError("SOAK_REPORT: duplicate_decision_ids must match the duplicate rows")
        expected_growth = (
            self.rss_growth_slope_bytes_per_sample is not None
            and self.rss_growth_slope_bytes_per_sample > self.rss_growth_limit_bytes_per_sample
        )
        if self.memory_growth_detected is not expected_growth:
            raise ValueError(
                "SOAK_REPORT: memory_growth_detected must follow from the fitted slope and limit"
            )
        expected_fail_closed = (
            not self.invariant_violations
            and self.duplicate_decision_ids == 0
            and not self.memory_growth_detected
        )
        if self.fail_closed is not expected_fail_closed:
            raise ValueError(
                "SOAK_REPORT: fail_closed must follow from violations, duplicates and "
                "measured RSS growth"
            )
        if self.peak_rss_bytes is None and self.median_rss_bytes is not None:
            raise ValueError("SOAK_REPORT: median RSS requires a peak RSS")
        return self


RssSampler = Callable[[], int | None]
SwapSampler = Callable[[], int | None]
DiskSampler = Callable[[], int | None]
CpuSampler = Callable[[], float | None]
Clock = Callable[[], float]
StepFunction = Callable[[StepContext], StepResult]


def read_rss_bytes() -> int | None:
    """Current RSS in bytes where possible; ``None`` when the platform has no source."""
    try:
        if sys.platform.startswith("linux"):
            with open("/proc/self/statm", encoding="ascii") as handle:
                resident_pages = int(handle.read().split()[1])
            return resident_pages * int(os.sysconf("SC_PAGE_SIZE"))
        usage = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
        # ru_maxrss is bytes on macOS and KiB on the other POSIX platforms.
        return usage if sys.platform == "darwin" else usage * 1024
    except (OSError, ValueError, AttributeError):
        return None


def read_rss_source() -> str:
    """Which sampler :func:`read_rss_bytes` used on this host."""
    if sys.platform.startswith("linux"):
        return "/proc/self/statm (current RSS)"
    if sys.platform == "darwin":
        return "resource.getrusage ru_maxrss (peak high-water on macOS)"
    return "resource.getrusage ru_maxrss (peak high-water)"


def read_cpu_ms() -> float | None:
    """Cumulative process CPU time in milliseconds."""
    try:
        return time.process_time() * 1000.0
    except (OSError, ValueError):
        return None


def read_swap_used_bytes() -> int | None:
    """Used swap in bytes from ``/proc/meminfo``; ``None`` off Linux."""
    if not sys.platform.startswith("linux"):
        return None
    try:
        values: dict[str, int] = {}
        with open("/proc/meminfo", encoding="ascii") as handle:
            for line in handle:
                key, _, rest = line.partition(":")
                parts = rest.split()
                if parts and parts[0].isdigit():
                    values[key.strip()] = int(parts[0]) * 1024
        total = values.get("SwapTotal")
        free = values.get("SwapFree")
        if total is None or free is None:
            return None
        return max(0, total - free)
    except (OSError, ValueError):
        return None


def read_disk_free_bytes(path: Path | None = None) -> int | None:
    """Free bytes on the filesystem holding ``path`` (defaults to the CWD)."""
    try:
        return int(shutil.disk_usage(path or Path.cwd()).free)
    except (OSError, ValueError):
        return None


def _fit_slope(samples: Sequence[SoakSample]) -> float | None:
    """Least-squares RSS slope over sample indices, or ``None`` when unmeasured."""
    points = [(sample.index, sample.rss_bytes) for sample in samples if sample.rss_bytes is not None]
    if len(points) < MIN_GROWTH_SAMPLES:
        return None
    count = len(points)
    mean_x = math.fsum(x for x, _ in points) / count
    mean_y = math.fsum(float(y) for _, y in points) / count
    sxx = math.fsum((x - mean_x) ** 2 for x, _ in points)
    if sxx == 0:
        return None
    sxy = math.fsum((x - mean_x) * (float(y) - mean_y) for x, y in points)
    return sxy / sxx


def _rss_stats(samples: Sequence[SoakSample]) -> tuple[int | None, int | None]:
    values = [sample.rss_bytes for sample in samples if sample.rss_bytes is not None]
    if not values:
        return None, None
    return max(values), int(statistics.median(values))


class SoakRunner:
    """Drive a shadow step under a :class:`FaultPlan` and judge the result.

    Single runner, repeatable: every :meth:`run` starts from an empty journal and empty
    violation lists. The clock, samplers and hardware declaration are injectable so a test
    can exercise the harness without waiting hours or reading this host's memory.
    """

    def __init__(
        self,
        plan: FaultPlan,
        *,
        sample_every: int = DEFAULT_SAMPLE_EVERY,
        rss_growth_limit_bytes_per_sample: int = DEFAULT_RSS_GROWTH_LIMIT_BYTES_PER_SAMPLE,
        hardware_profile: str = DEFAULT_HARDWARE_PROFILE,
        rss_sampler: RssSampler = read_rss_bytes,
        swap_sampler: SwapSampler = read_swap_used_bytes,
        disk_sampler: DiskSampler = read_disk_free_bytes,
        cpu_sampler: CpuSampler = read_cpu_ms,
        clock: Clock = time.monotonic,
        rss_source: str | None = None,
    ) -> None:
        if sample_every < 1:
            raise SoakError("SOAK_RUNNER: sample_every must be >= 1")
        if rss_growth_limit_bytes_per_sample <= 0:
            raise SoakError("SOAK_RUNNER: rss growth limit must be positive")
        if not hardware_profile.strip():
            raise SoakError("SOAK_RUNNER: declared_hardware_profile cannot be blank")
        self._plan = plan
        self._sample_every = sample_every
        self._limit = rss_growth_limit_bytes_per_sample
        self._hardware_profile = hardware_profile
        self._rss_sampler = rss_sampler
        self._swap_sampler = swap_sampler
        self._disk_sampler = disk_sampler
        self._cpu_sampler = cpu_sampler
        self._clock = clock
        self._rss_source = rss_source or read_rss_source()
        self._journal = DecisionJournal()
        self._violations: list[InvariantViolation] = []
        self._duplicates: list[DuplicateDecision] = []
        self._live_authority_attempts = 0
        self._generation = 0
        self._samples: list[SoakSample] = []

    @property
    def plan(self) -> FaultPlan:
        """The injected fault plan."""
        return self._plan

    def run(
        self,
        step: StepFunction,
        *,
        iterations: int,
        duration_target_s: float,
    ) -> SoakReport:
        """Run ``step`` for at most ``iterations`` steps or ``duration_target_s`` wall seconds."""
        if iterations < 0:
            raise SoakError("SOAK_RUNNER: iterations must be >= 0")
        if duration_target_s < 0:
            raise SoakError("SOAK_RUNNER: duration_target_s must be >= 0")
        self._reset()
        started = self._clock()
        executed = 0
        for index in range(iterations):
            if index > 0 and duration_target_s > 0 and self._clock() - started >= duration_target_s:
                break
            executed = index + 1
            schedules = self._plan.active_at(index)
            restart = any(
                schedule.kind is FaultKind.CRASH_RESTART and schedule.start_index == index
                for schedule in schedules
            )
            if restart:
                self._generation += 1
            context = StepContext(
                index=index,
                active_faults=tuple(schedule.kind for schedule in schedules),
                restart=restart,
                generation=self._generation,
                journal=self._journal,
            )
            result, used_context = self._invoke(step, context)
            if used_context.restart and not result.recovered:
                self._violations.append(
                    InvariantViolation(
                        index=index,
                        fault=FaultKind.CRASH_RESTART,
                        invariant=ExpectedInvariant.RECOVERED_STATE,
                        detail="restart iteration did not report recovered state",
                    )
                )
            self._absorb(index, schedules, result)
            if index == 0 or (index + 1) % self._sample_every == 0 or index == iterations - 1:
                self._sample(index, executed, started, result)
        elapsed = max(0.0, self._clock() - started)
        duration_actual = elapsed
        peak, median = _rss_stats(self._samples)
        slope = _fit_slope(self._samples)
        growth = slope is not None and slope > self._limit
        throughput = executed / elapsed if elapsed > 0 and executed > 0 else None
        return SoakReport(
            iterations=executed,
            elapsed_s=elapsed,
            duration_target_s=duration_target_s,
            duration_actual_s=duration_actual,
            samples=tuple(self._samples),
            peak_rss_bytes=peak,
            median_rss_bytes=median,
            rss_growth_slope_bytes_per_sample=slope,
            rss_growth_limit_bytes_per_sample=self._limit,
            memory_growth_detected=growth,
            duplicate_decision_ids=len(self._duplicates),
            duplicates=tuple(self._duplicates),
            invariant_violations=tuple(self._violations),
            crash_restarts=self._generation,
            live_authority_attempts=self._live_authority_attempts,
            throughput_per_s=throughput,
            declared_hardware_profile=self._hardware_profile,
            rss_source=self._rss_source,
            soak_status=_status_for(executed, duration_target_s, duration_actual),
            fail_closed=not self._violations and not self._duplicates and not growth,
        )

    def _reset(self) -> None:
        self._journal = DecisionJournal()
        self._violations = []
        self._duplicates = []
        self._live_authority_attempts = 0
        self._generation = 0
        self._samples = []

    def _invoke(
        self, step: StepFunction, context: StepContext
    ) -> tuple[StepResult, StepContext]:
        try:
            return step(context), context
        except SimulatedCrash:
            self._generation += 1
            retry = replace(context, restart=True, generation=self._generation)
            # The journal is durable across the crash; a second failure is unrecoverable.
            return step(retry), retry

    def _absorb(
        self,
        index: int,
        schedules: Sequence[FaultSchedule],
        result: StepResult,
    ) -> None:
        duplicate_invariant = any(
            schedule.invariant is ExpectedInvariant.NO_DUPLICATE_DECISION for schedule in schedules
        )
        duplicate_fault = next(
            (s.kind for s in schedules if s.invariant is ExpectedInvariant.NO_DUPLICATE_DECISION),
            None,
        )
        if result.backlog is not None and result.backlog < 0:
            raise SoakError("SOAK_RUNNER: consumer backlog cannot be negative")
        if result.backlog is None:
            for schedule in schedules:
                if schedule.invariant is ExpectedInvariant.BOUNDED_BACKLOG:
                    self._violations.append(
                        InvariantViolation(
                            index=index,
                            fault=schedule.kind,
                            invariant=ExpectedInvariant.BOUNDED_BACKLOG,
                            detail="consumer backlog was not measured; unmeasured is not bounded",
                        )
                    )
        else:
            for schedule in schedules:
                if (
                    schedule.invariant is ExpectedInvariant.BOUNDED_BACKLOG
                    and result.backlog > self._plan.max_backlog
                ):
                    self._violations.append(
                        InvariantViolation(
                            index=index,
                            fault=schedule.kind,
                            invariant=ExpectedInvariant.BOUNDED_BACKLOG,
                            detail=(
                                f"backlog {result.backlog} exceeds ceiling "
                                f"{self._plan.max_backlog}"
                            ),
                        )
                    )
        for observation in result.decisions:
            if not observation.decision_id.strip():
                raise SoakError("SOAK_RUNNER: emitted decision with a blank decision_id")
            if observation.live_authority:
                self._live_authority_attempts += 1
                self._violations.append(
                    InvariantViolation(
                        index=index,
                        fault=None,
                        invariant=ExpectedInvariant.NO_LIVE_AUTHORITY,
                        detail=f"decision {observation.decision_id} claimed live authority",
                    )
                )
            first = self._journal._record(observation.decision_id, index)
            if first is not None:
                self._duplicates.append(
                    DuplicateDecision(
                        index=index,
                        first_index=first,
                        decision_id=observation.decision_id,
                    )
                )
                if duplicate_invariant:
                    self._violations.append(
                        InvariantViolation(
                            index=index,
                            fault=duplicate_fault,
                            invariant=ExpectedInvariant.NO_DUPLICATE_DECISION,
                            detail=(
                                f"decision {observation.decision_id} was already emitted at "
                                f"index {first}"
                            ),
                        )
                    )
            for schedule in schedules:
                self._check_observation(index, schedule, observation)

    def _check_observation(
        self,
        index: int,
        schedule: FaultSchedule,
        observation: DecisionObservation,
    ) -> None:
        invariant = schedule.invariant
        breached = ""
        if invariant is ExpectedInvariant.NO_ENTER and observation.action is DecisionAction.ENTER:
            breached = "ENTER while entry is forbidden by the active fault"
        elif (
            invariant is ExpectedInvariant.NO_NEW_RISK
            and observation.action is DecisionAction.ENTER
        ):
            breached = "new risk while the venue link is down"
        elif invariant is ExpectedInvariant.NO_PROTECTED and observation.protected:
            breached = "PROTECTED claim while protection is known missing"
        if breached:
            self._violations.append(
                InvariantViolation(
                    index=index,
                    fault=schedule.kind,
                    invariant=invariant,
                    detail=f"decision {observation.decision_id}: {breached}",
                )
            )

    def _sample(self, index: int, executed: int, started: float, result: StepResult) -> None:
        elapsed = max(0.0, self._clock() - started)
        self._samples.append(
            SoakSample(
                index=index,
                elapsed_s=elapsed,
                rss_bytes=self._rss_sampler(),
                cpu_ms=self._cpu_sampler(),
                throughput_per_s=executed / elapsed if elapsed > 0 else None,
                swap_used_bytes=self._swap_sampler(),
                disk_free_bytes=self._disk_sampler(),
                backlog=result.backlog,
            )
        )


def _demo_step(context: StepContext) -> StepResult:
    """No-op shadow step used by the CLI: emits nothing, recovers on restart."""
    return StepResult(recovered=context.restart, backlog=0)


def main(argv: Sequence[str] | None = None) -> int:
    """Exercise the soak harness. A real 24-48 h shadow soak is NOT_RUN here."""
    parser = argparse.ArgumentParser(
        prog="python -m v8_next.factory.soak",
        description=(
            "Run the M4-05 soak harness against a no-op shadow step. The harness is real; "
            "the production shadow loop is not wired, so a 24-48 h qualification run is "
            "NOT_RUN in this environment."
        ),
        epilog=(
            "Pass --iterations as a safety cap. The run stops at --duration or the cap, and "
            "reports SHORT_RUN unless the full duration actually elapsed."
        ),
    )
    parser.add_argument(
        "--duration",
        type=float,
        default=86400.0,
        help="duration_target_s in seconds (default: 86400)",
    )
    parser.add_argument(
        "--iterations",
        type=int,
        default=1000,
        help="maximum loop iterations before stopping (default: 1000)",
    )
    parser.add_argument(
        "--sample-every",
        type=int,
        default=DEFAULT_SAMPLE_EVERY,
        help="RSS/CPU/swap/disk sampling cadence in iterations",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="write the JSON report to this path instead of stdout",
    )
    args = parser.parse_args(argv)
    if args.iterations < 0:
        parser.error("--iterations must be >= 0")
    if args.duration < 0:
        parser.error("--duration must be >= 0")
    if args.sample_every < 1:
        parser.error("--sample-every must be >= 1")
    plan = default_plan(max(1, args.iterations))
    runner = SoakRunner(plan, sample_every=args.sample_every)
    report = runner.run(
        _demo_step,
        iterations=args.iterations,
        duration_target_s=args.duration,
    )
    payload = json.dumps(report.model_dump(mode="json"), indent=2, sort_keys=True)
    if args.out is None:
        print(payload)
    else:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(payload + "\n", encoding="utf-8")
    return 0 if report.fail_closed else 1


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
