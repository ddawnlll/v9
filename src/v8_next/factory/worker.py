"""Bounded issue-to-PR worker with no production authority (F-02).

One assigned issue at a time: claim it from the durable queue (F-01), run a pluggable
``prepare -> implement -> verify -> publish`` pipeline, and report a typed terminal state
with a receipt. The worker is the *executor*; :class:`~v8_next.factory.queue.IssueQueue` is
the ownership record and :class:`~v8_next.factory.memory.ResearchMemory` is the research
log.

Three properties are structural rather than policy:

* **No production authority.** :class:`Capability` is a closed set and the four capabilities
  that would reach production (``MAIN_MERGE``, ``PUSH_TO_MAIN``, ``SEALED_OOS_ACCESS``,
  ``LIVE_CREDENTIALS``) plus ``DEPLOY`` are in :data:`FORBIDDEN_CAPABILITIES`. A step that
  declares or returns one of them is refused with :class:`WorkerAuthorizationError` *before*
  the effect and the issue is durably blocked; the error carries the blocked receipt, so the
  audit trail is not lost on the way out.
* **Bounded per task.** :class:`TaskBudget` fixes token, wall-clock, CPU, RSS and attempt
  ceilings. The budget is checked before *every* phase, so an over-budget run can never
  reach ``publish`` — it ends ``EXHAUSTED`` instead. A published draft is only reported when
  the whole run fitted the budget.
* **Fail closed.** A missing step, a step that raises, a phase mismatch, or a failed verify
  ends ``BLOCKED`` with the failure preserved and no PR state. Only a run that completed all
  four phases successfully *and* stayed inside budget reports ``PUBLISHED_DRAFT``.

``PUBLISHED_DRAFT`` means "a draft PR was opened, tests ran, and owner review is required";
it never means merged, deployed or reviewed. ``owner_review_required`` is pinned ``True`` on
every receipt.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Final, Literal, Protocol, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from v8_next.factory.lease import (
    MEASURED,
    NOT_MEASURED,
    CompletionReceipt,
    IssueState,
    Lease,
    OwnerReview,
)
from v8_next.factory.memory import FindingOutcome, MemoryKind, ResearchMemory
from v8_next.factory.queue import IssueQueue
from v8_next.telemetry.identity import canonical_digest

__all__ = [
    "FORBIDDEN_CAPABILITIES",
    "PHASES",
    "PROTECTED_BRANCHES",
    "BoundedWorker",
    "Capability",
    "FunctionStep",
    "StepContext",
    "StepPhase",
    "StepResult",
    "TaskBudget",
    "WorkerAuthorizationError",
    "WorkerReceipt",
    "WorkerRun",
    "WorkerStep",
    "WorkerTerminal",
]


class StepPhase(StrEnum):
    """Ordered pipeline phases. Each phase has at most one step implementation."""

    PREPARE = "prepare"
    IMPLEMENT = "implement"
    VERIFY = "verify"
    PUBLISH = "publish"


#: The pipeline runs in this order; nothing is skipped. ``verify`` must precede ``publish``
#: because the publish step is only reached after a passing verify result.
PHASES: Final[tuple[StepPhase, ...]] = (
    StepPhase.PREPARE,
    StepPhase.IMPLEMENT,
    StepPhase.VERIFY,
    StepPhase.PUBLISH,
)


class WorkerTerminal(StrEnum):
    """Terminal state of one worker run. There is no "probably fine" state."""

    #: An open, draft PR exists and every executed test passed. Owner review still required.
    PUBLISHED_DRAFT = "PUBLISHED_DRAFT"
    #: The run stopped for a reason an owner must look at. No PR state.
    BLOCKED = "BLOCKED"
    #: The task budget (tokens/wall/CPU/RSS/attempts) ran out. Nothing was published.
    EXHAUSTED = "EXHAUSTED"


class Capability(StrEnum):
    """What a step is allowed to ask for. The set is closed and mostly forbidden."""

    REPO_READ = "REPO_READ"
    BRANCH_WRITE = "BRANCH_WRITE"
    TEST_EXECUTION = "TEST_EXECUTION"
    DRAFT_PR_CREATE = "DRAFT_PR_CREATE"
    MAIN_MERGE = "MAIN_MERGE"
    PUSH_TO_MAIN = "PUSH_TO_MAIN"
    SEALED_OOS_ACCESS = "SEALED_OOS_ACCESS"
    LIVE_CREDENTIALS = "LIVE_CREDENTIALS"
    DEPLOY = "DEPLOY"


#: Never granted to an automated worker, at any phase, under any budget.
FORBIDDEN_CAPABILITIES: Final[frozenset[Capability]] = frozenset(
    {
        Capability.MAIN_MERGE,
        Capability.PUSH_TO_MAIN,
        Capability.SEALED_OOS_ACCESS,
        Capability.LIVE_CREDENTIALS,
        Capability.DEPLOY,
    }
)

#: Branches a worker may never prepare, whatever capability it claims.
PROTECTED_BRANCHES: Final[frozenset[str]] = frozenset({"main", "master"})


class TaskBudget(BaseModel):
    """Explicit per-task ceilings. Every value is a hard limit, not a target.

    ``max_attempts`` is the total number of attempts the task may spend, including the
    current one; it is mirrored into the queue's retry budget by
    :meth:`BoundedWorker.register`.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    max_tokens: int = Field(default=2_000_000, gt=0)
    max_wall_s: int = Field(default=1_800, gt=0)
    max_rss_kb: int = Field(default=2_097_152, gt=0)
    max_cpu_s: int = Field(default=1_800, gt=0)
    max_attempts: int = Field(default=3, ge=1)

    @property
    def digest(self) -> str:
        """Content digest of the budget, recorded on every receipt as the config hash."""
        return canonical_digest("factory-task-budget", self.model_dump(mode="json"))


class StepResult(BaseModel):
    """What one step reports back: outcome, its own cost, and any capability it needed.

    The cost fields are required. A step that cannot say what it consumed has no business
    running under a budget, so there is no default to fall back on.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    phase: StepPhase
    ok: bool
    requests: tuple[Capability, ...] = ()
    tokens: int = Field(ge=0)
    wall_ms: int = Field(ge=0)
    cpu_ms: int = Field(ge=0)
    peak_rss_kb: int = Field(ge=0)
    commands: tuple[str, ...] = ()
    executed_tests: tuple[str, ...] = ()
    branch: str | None = None
    base_sha: str | None = None
    head_sha: str | None = None
    pr_url: str | None = None
    draft: bool | None = None
    failure: str | None = None
    detail: str | None = None

    @field_validator("commands", "executed_tests", mode="after")
    @classmethod
    def _ordered_unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        ordered: list[str] = []
        for item in value:
            if not isinstance(item, str) or not item.strip():
                raise ValueError("STEP: entries must be non-empty strings")
            if item not in ordered:
                ordered.append(item)
        return tuple(ordered)

    @field_validator("requests", mode="after")
    @classmethod
    def _unique_requests(cls, value: tuple[Capability, ...]) -> tuple[Capability, ...]:
        ordered: list[Capability] = []
        for item in value:
            if not isinstance(item, Capability):
                raise ValueError("STEP: requests must be Capability members")
            if item not in ordered:
                ordered.append(item)
        return tuple(ordered)

    @model_validator(mode="after")
    def _phase_contract(self) -> Self:
        if not self.ok:
            if self.failure is None or not self.failure.strip():
                raise ValueError("STEP: a failed step must carry a failure reason")
            return self
        if self.failure is not None:
            raise ValueError("STEP: a successful step must not carry a failure reason")
        if self.phase is StepPhase.PREPARE:
            if not self.branch or not self.base_sha:
                raise ValueError("STEP: prepare must report branch and base_sha")
        elif self.phase is StepPhase.IMPLEMENT:
            if not self.head_sha:
                raise ValueError("STEP: implement must report head_sha")
        elif self.phase is StepPhase.VERIFY:
            if not self.executed_tests:
                raise ValueError("STEP: verify must report the tests it executed")
        elif self.phase is StepPhase.PUBLISH:
            if not self.pr_url:
                raise ValueError("STEP: publish must report the draft PR url")
            if self.draft is not True:
                raise ValueError("STEP: publish is draft-only; non-draft PRs are refused")
        return self


class StepContext(BaseModel):
    """Everything a step may know. It carries no credential and no venue handle."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    issue_id: str
    worker_id: str
    lease_token: str = Field(repr=False)
    attempt: int = Field(ge=1)
    branch: str | None = None
    budget: TaskBudget
    prior: tuple[StepResult, ...] = ()
    meta: Mapping[str, Any] = Field(default_factory=dict)


class WorkerStep(Protocol):
    """A pluggable pipeline phase. Tests inject fakes; production injects real tooling."""

    phase: StepPhase
    requests: frozenset[Capability]

    def execute(self, context: StepContext) -> StepResult: ...


@dataclass(frozen=True, slots=True)
class FunctionStep:
    """Adapter so a plain callable can be a :class:`WorkerStep`."""

    phase: StepPhase
    run: Callable[[StepContext], StepResult]
    requests: frozenset[Capability] = frozenset()

    def execute(self, context: StepContext) -> StepResult:
        return self.run(context)


class WorkerAuthorizationError(RuntimeError):
    """A step asked for authority the automated worker never holds.

    The effect was refused before it happened, the issue is durably blocked, and the
    blocked receipt travels with the error so the caller still has the audit trail.
    """

    def __init__(
        self,
        reason: str,
        *,
        capability: Capability,
        phase: StepPhase,
        receipt: WorkerReceipt,
    ) -> None:
        super().__init__(reason)
        self.reason = reason
        self.capability = capability
        self.phase = phase
        self.receipt = receipt


class WorkerReceipt(BaseModel):
    """Immutable evidence for one worker run.

    ``code_sha`` is the head commit the worker produced (``None`` when it never got that
    far), ``config_sha`` is the budget digest, and :meth:`measurement` marks every field the
    run could not measure as ``NOT_MEASURED`` rather than implying a value.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    issue_id: str
    worker_id: str
    attempt: int = Field(ge=1)
    terminal: WorkerTerminal
    branch: str | None = None
    base_sha: str | None = None
    head_sha: str | None = None
    code_sha: str | None = None
    config_sha: str
    draft_pr_url: str | None = None
    published: bool = False
    executed_tests: tuple[str, ...] = ()
    commands: tuple[str, ...] = ()
    tokens_used: int = Field(ge=0)
    walltime_ms: int = Field(ge=0)
    cpu_ms: int = Field(ge=0)
    peak_rss_kb: int = Field(ge=0)
    budget: TaskBudget
    budget_violation: str | None = None
    failure: str | None = None
    phases: tuple[StepResult, ...] = ()
    durable_state: str
    retry_allowed: bool = False
    owner_review_required: Literal[True] = True
    owner_review: OwnerReview = OwnerReview.REQUIRED

    @property
    def digest(self) -> str:
        """Content digest of the receipt (deterministic; excludes no field)."""
        return canonical_digest("factory-worker-receipt", self.model_dump(mode="json"))

    def measurement(self) -> dict[str, str]:
        """``MEASURED``/``NOT_MEASURED`` per evidence field, mirroring F-01 receipts."""
        fields: dict[str, object] = {
            "code_sha": self.code_sha,
            "config_sha": self.config_sha,
            "base_sha": self.base_sha,
            "head_sha": self.head_sha,
            "executed_tests": self.executed_tests or None,
            "walltime_ms": self.walltime_ms,
            "peak_rss_kb": self.peak_rss_kb,
            "tokens_used": self.tokens_used,
        }
        return {
            field: (NOT_MEASURED if value is None else MEASURED)
            for field, value in fields.items()
        }

    @property
    def budget_use(self) -> dict[str, dict[str, int | bool]]:
        """Consumption against every ceiling, so a receipt can be audited without maths."""
        return {
            "tokens": {
                "used": self.tokens_used,
                "limit": self.budget.max_tokens,
                "within": self.tokens_used <= self.budget.max_tokens,
            },
            "wall_ms": {
                "used": self.walltime_ms,
                "limit": self.budget.max_wall_s * 1000,
                "within": self.walltime_ms <= self.budget.max_wall_s * 1000,
            },
            "cpu_ms": {
                "used": self.cpu_ms,
                "limit": self.budget.max_cpu_s * 1000,
                "within": self.cpu_ms <= self.budget.max_cpu_s * 1000,
            },
            "peak_rss_kb": {
                "used": self.peak_rss_kb,
                "limit": self.budget.max_rss_kb,
                "within": self.peak_rss_kb <= self.budget.max_rss_kb,
            },
            "attempts": {
                "used": self.attempt,
                "limit": self.budget.max_attempts,
                "within": self.attempt <= self.budget.max_attempts,
            },
        }

    def to_result(self) -> dict[str, Any]:
        """JSON-safe mapping recorded by :meth:`IssueQueue.complete`."""
        payload = self.model_dump(mode="json")
        payload["receipt_sha"] = self.digest
        payload["budget_use"] = self.budget_use
        return payload


@dataclass(frozen=True, slots=True)
class WorkerRun:
    """One run's receipt plus the durable hand-off (if the queue recorded one)."""

    receipt: WorkerReceipt
    completion: CompletionReceipt | None
    durable_state: IssueState


def _declared_requests(requests: object, *, phase: StepPhase) -> tuple[Capability, ...]:
    """Normalise a step's declared requests, refusing anything that is not a member.

    Membership is an identity question, not a string comparison: ``"MAIN_MERGE"``
    compares equal to :attr:`Capability.MAIN_MERGE` but is not a capability, and
    ``"sealed_oos_access"`` compares equal to nothing at all, so a substring check
    would silently drop the request. Either case is a specification error in the
    step and fails closed here, before any run starts.
    """
    if isinstance(requests, (str, bytes)) or not isinstance(requests, Iterable):
        raise TypeError(
            f"WORKER: step for {phase.value} must declare an iterable of Capability, "
            f"got {type(requests).__name__}"
        )
    declared: list[Capability] = []
    for request in requests:
        if not isinstance(request, Capability):
            raise TypeError(
                f"WORKER: step for {phase.value} declared {request!r}, which is not a "
                "Capability member; a capability must name a member of the closed set"
            )
        if request not in declared:
            declared.append(request)
    return tuple(declared)


_DEFAULT_LEASE_TTL_NS: Final[int] = 15 * 60 * 1_000_000_000


class _Refusal(Exception):
    """Internal signal: a step asked for authority the worker never holds."""

    def __init__(self, capability: Capability, phase: StepPhase, reason: str) -> None:
        super().__init__(reason)
        self.capability = capability
        self.phase = phase
        self.reason = reason


@dataclass
class _Progress:
    """Mutable accumulator for one run; never leaves :class:`BoundedWorker`."""

    branch: str | None = None
    base_sha: str | None = None
    head_sha: str | None = None
    pr_url: str | None = None
    phases: list[StepResult] = field(default_factory=list)
    tokens: int = 0
    wall_ms: int = 0
    cpu_ms: int = 0
    rss_kb: int = 0

    def absorb(self, result: StepResult) -> None:
        self.tokens += result.tokens
        self.wall_ms += result.wall_ms
        self.cpu_ms += result.cpu_ms
        self.rss_kb = max(self.rss_kb, result.peak_rss_kb)


class BoundedWorker:
    """Drives one issue through ``prepare -> implement -> verify -> publish``.

    The worker never enqueues, merges, deploys or reviews: :meth:`register` seeds the queue,
    :meth:`run_once` claims one issue, and the terminal state is recorded as one of
    ``COMPLETED`` (draft PR opened, owner review still required), ``BLOCKED`` (owner
    escalation) or ``EXHAUSTED``/``PENDING`` (budget spent).
    """

    def __init__(
        self,
        queue: IssueQueue,
        *,
        worker_id: str,
        budget: TaskBudget | None = None,
        steps: Mapping[StepPhase, WorkerStep] | None = None,
        lease_ttl_ns: int = _DEFAULT_LEASE_TTL_NS,
        memory: ResearchMemory | None = None,
        clock: Callable[[], int] = time.time_ns,
    ) -> None:
        if not isinstance(worker_id, str) or not worker_id.strip():
            raise ValueError("WORKER: worker_id must be a non-empty string")
        if not isinstance(lease_ttl_ns, int) or isinstance(lease_ttl_ns, bool):
            raise ValueError("WORKER: lease_ttl_ns must be an int")
        if lease_ttl_ns < 1:
            raise ValueError("WORKER: lease_ttl_ns must be positive")
        if steps is not None:
            if not isinstance(steps, Mapping):
                raise TypeError("WORKER: steps must be a mapping of phase -> step")
            for phase, step in steps.items():
                if not isinstance(phase, StepPhase):
                    raise TypeError("WORKER: step keys must be StepPhase members")
                if getattr(step, "phase", None) is not phase:
                    raise ValueError(
                        f"WORKER: step for {phase.value} reports a different phase"
                    )
                # Validate the declaration before any run can use it: an unrecognised
                # request must not wait until the phase it belongs to is reached.
                _declared_requests(getattr(step, "requests", ()), phase=phase)
        self._queue = queue
        self._worker_id = worker_id.strip()
        self._budget = budget if budget is not None else TaskBudget()
        self._steps: dict[StepPhase, WorkerStep] = dict(steps or {})
        self._lease_ttl_ns = lease_ttl_ns
        self._memory = memory
        self._clock = clock

    # -- configuration -------------------------------------------------------------

    @property
    def worker_id(self) -> str:
        return self._worker_id

    @property
    def budget(self) -> TaskBudget:
        return self._budget

    @property
    def steps(self) -> Mapping[StepPhase, WorkerStep]:
        return dict(self._steps)

    # -- queue ---------------------------------------------------------------------

    def register(
        self,
        issue_id: str,
        meta: Mapping[str, Any] | None = None,
        *,
        now_ns: int | None = None,
    ) -> None:
        """Enqueue the issue with this worker's attempt budget unless the caller set one."""
        payload = dict(meta or {})
        payload.setdefault("max_attempts", self._budget.max_attempts)
        self._queue.enqueue(issue_id, payload, now_ns=now_ns)

    def run_once(self, *, now_ns: int | None = None) -> WorkerRun | None:
        """Claim one eligible issue and run it. ``None`` when nothing is claimable."""
        now = self._clock() if now_ns is None else now_ns
        lease = self._queue.claim(self._worker_id, now, self._lease_ttl_ns)
        if lease is None:
            return None
        return self.run_lease(lease)

    def run_lease(self, lease: Lease) -> WorkerRun:
        """Run the pipeline under an already-claimed lease.

        A refusal raises :class:`WorkerAuthorizationError` *after* the issue has been
        durably blocked and its outcome recorded, so the caller can neither proceed nor lose
        the audit trail.
        """
        if not isinstance(lease, Lease):
            raise TypeError("WORKER: lease must be a Lease")
        meta = dict(self._queue.snapshot(lease.issue_id).meta)
        self._record_attempt(lease)
        progress = _Progress()
        try:
            terminal, failure, violation = self._drive(lease, meta, progress)
        except _Refusal as refusal:
            run = self._terminate(
                lease,
                progress,
                terminal=WorkerTerminal.BLOCKED,
                failure=refusal.reason,
                violation=None,
            )
            raise WorkerAuthorizationError(
                refusal.reason,
                capability=refusal.capability,
                phase=refusal.phase,
                receipt=run.receipt,
            ) from None
        except Exception as exc:
            # An unexpected failure inside the driver (a mutated declaration, a step
            # object that is not what it claimed) must not leave the lease held with no
            # recorded terminal state. The original exception still propagates.
            self._terminate(
                lease,
                progress,
                terminal=WorkerTerminal.BLOCKED,
                failure=f"DRIVER_FAILED:{type(exc).__name__}: {exc}",
                violation=None,
            )
            raise
        return self._terminate(
            lease,
            progress,
            terminal=terminal,
            failure=failure,
            violation=violation,
        )

    # -- pipeline ------------------------------------------------------------------

    def _drive(
        self, lease: Lease, meta: Mapping[str, Any], progress: _Progress
    ) -> tuple[WorkerTerminal, str | None, str | None]:
        """Run every phase in order; return ``(terminal, failure, budget_violation)``."""
        if lease.attempt > self._budget.max_attempts:
            return (
                WorkerTerminal.EXHAUSTED,
                "BUDGET_EXHAUSTED:attempts",
                "BUDGET_EXHAUSTED:attempts",
            )
        for phase in PHASES:
            violation = self._budget_violation(progress)
            if violation is not None:
                return WorkerTerminal.EXHAUSTED, violation, violation
            step = self._steps.get(phase)
            if step is None:
                return WorkerTerminal.BLOCKED, f"STEP_MISSING:{phase.value}", None
            self._authorize(phase, _declared_requests(getattr(step, "requests", ()), phase=phase))
            context = StepContext(
                issue_id=lease.issue_id,
                worker_id=self._worker_id,
                lease_token=lease.token,
                attempt=lease.attempt,
                branch=progress.branch,
                budget=self._budget,
                prior=tuple(progress.phases),
                meta=meta,
            )
            try:
                result = step.execute(context)
            except Exception as exc:  # a broken step is a blocked issue, not a crash
                return (
                    WorkerTerminal.BLOCKED,
                    f"STEP_RAISED:{phase.value}:{type(exc).__name__}: {exc}",
                    None,
                )
            if result.phase is not phase:
                return (
                    WorkerTerminal.BLOCKED,
                    f"STEP_PHASE_MISMATCH:{phase.value}:{result.phase.value}",
                    None,
                )
            progress.absorb(result)
            progress.phases.append(result)
            # A failed step still gets its requests audited: the refusal must name the
            # capability it reached for, even though the run is already ending.
            self._authorize(phase, result.requests)
            if not result.ok:
                return (
                    WorkerTerminal.BLOCKED,
                    f"STEP_FAILED:{phase.value}:{result.failure}",
                    None,
                )
            if phase is StepPhase.PREPARE:
                progress.branch = result.branch
                progress.base_sha = result.base_sha
                if progress.branch in PROTECTED_BRANCHES:
                    raise _Refusal(
                        Capability.PUSH_TO_MAIN,
                        phase,
                        f"PROTECTED_BRANCH:{progress.branch}",
                    )
            elif phase is StepPhase.IMPLEMENT:
                progress.head_sha = result.head_sha
            elif phase is StepPhase.PUBLISH:
                progress.pr_url = result.pr_url
        violation = self._budget_violation(progress)
        if violation is not None:
            return WorkerTerminal.EXHAUSTED, violation, violation
        return WorkerTerminal.PUBLISHED_DRAFT, None, None

    def _budget_violation(self, progress: _Progress) -> str | None:
        """First exceeded ceiling, or ``None`` when every dimension is inside budget."""
        budget = self._budget
        if progress.tokens > budget.max_tokens:
            return f"BUDGET_EXHAUSTED:tokens:{progress.tokens}>{budget.max_tokens}"
        wall_limit = budget.max_wall_s * 1000
        if progress.wall_ms > wall_limit:
            return f"BUDGET_EXHAUSTED:wall_ms:{progress.wall_ms}>{wall_limit}"
        cpu_limit = budget.max_cpu_s * 1000
        if progress.cpu_ms > cpu_limit:
            return f"BUDGET_EXHAUSTED:cpu_ms:{progress.cpu_ms}>{cpu_limit}"
        if progress.rss_kb > budget.max_rss_kb:
            return f"BUDGET_EXHAUSTED:peak_rss_kb:{progress.rss_kb}>{budget.max_rss_kb}"
        return None

    @staticmethod
    def _authorize(phase: StepPhase, requests: Iterable[Capability]) -> None:
        """Raise :class:`_Refusal` before any forbidden effect can happen."""
        refused = [cap for cap in requests if cap in FORBIDDEN_CAPABILITIES]
        if not refused:
            return
        reason = f"UNAUTHORIZED_CAPABILITY:{','.join(sorted(cap.value for cap in refused))}"
        raise _Refusal(refused[0], phase, reason)

    # -- recording -----------------------------------------------------------------

    def _terminate(
        self,
        lease: Lease,
        progress: _Progress,
        *,
        terminal: WorkerTerminal,
        failure: str | None,
        violation: str | None,
    ) -> WorkerRun:
        """Commit the durable transition, then build the receipt and memory row."""
        completion: CompletionReceipt | None = None
        if terminal is WorkerTerminal.EXHAUSTED:
            state = self._queue.fail(lease, failure or "BUDGET_EXHAUSTED")
            durable = state
            retry_allowed = not state.terminal
            receipt = self._receipt(
                lease,
                progress,
                terminal=terminal,
                failure=failure,
                violation=violation,
                durable=durable,
                retry_allowed=retry_allowed,
            )
        elif terminal is WorkerTerminal.BLOCKED:
            self._queue.block_lease(lease, failure or "BLOCKED")
            durable = IssueState.BLOCKED
            retry_allowed = False
            receipt = self._receipt(
                lease,
                progress,
                terminal=terminal,
                failure=failure,
                violation=violation,
                durable=durable,
                retry_allowed=retry_allowed,
            )
        else:
            durable = IssueState.COMPLETED
            retry_allowed = False
            receipt = self._receipt(
                lease,
                progress,
                terminal=terminal,
                failure=None,
                violation=None,
                durable=durable,
                retry_allowed=retry_allowed,
            )
            completion = self._queue.complete(lease, receipt.to_result())
        self._record_outcome(receipt)
        return WorkerRun(receipt=receipt, completion=completion, durable_state=durable)

    def _receipt(
        self,
        lease: Lease,
        progress: _Progress,
        *,
        terminal: WorkerTerminal,
        failure: str | None,
        violation: str | None,
        durable: IssueState,
        retry_allowed: bool,
    ) -> WorkerReceipt:
        commands: list[str] = []
        tests: list[str] = []
        for result in progress.phases:
            for command in result.commands:
                if command not in commands:
                    commands.append(command)
            for test in result.executed_tests:
                if test not in tests:
                    tests.append(test)
        return WorkerReceipt(
            issue_id=lease.issue_id,
            worker_id=self._worker_id,
            attempt=lease.attempt,
            terminal=terminal,
            branch=progress.branch,
            base_sha=progress.base_sha,
            head_sha=progress.head_sha,
            code_sha=progress.head_sha,
            config_sha=self._budget.digest,
            draft_pr_url=progress.pr_url,
            published=terminal is WorkerTerminal.PUBLISHED_DRAFT,
            executed_tests=tuple(tests),
            commands=tuple(commands),
            tokens_used=progress.tokens,
            walltime_ms=progress.wall_ms,
            cpu_ms=progress.cpu_ms,
            peak_rss_kb=progress.rss_kb,
            budget=self._budget,
            budget_violation=violation,
            failure=failure,
            phases=tuple(progress.phases),
            durable_state=durable.value,
            retry_allowed=retry_allowed,
        )

    def _record_attempt(self, lease: Lease) -> None:
        """Write the attempt row before any step runs, so a crash still leaves a trace."""
        if self._memory is None:
            return
        self._memory.record(
            lease.issue_id,
            MemoryKind.ATTEMPT,
            attempt=lease.attempt,
            worker_id=self._worker_id,
            provenance={
                "budget_sha": self._budget.digest,
                "lease_expires_ns": lease.expires_ns,
            },
        )

    def _record_outcome(self, receipt: WorkerReceipt) -> None:
        if self._memory is None:
            return
        outcome = {
            # A draft publish is a hand-off, not a verified finding: the owner has not
            # reviewed it, so it is recorded as PENDING_REVIEW and never counted as a
            # verified finding.
            WorkerTerminal.PUBLISHED_DRAFT: FindingOutcome.PENDING_REVIEW,
            WorkerTerminal.BLOCKED: FindingOutcome.BLOCKED,
            WorkerTerminal.EXHAUSTED: FindingOutcome.EXHAUSTED,
        }[receipt.terminal]
        self._memory.record(
            receipt.issue_id,
            MemoryKind.OUTCOME,
            attempt=receipt.attempt,
            outcome=outcome,
            worker_id=receipt.worker_id,
            detail=receipt.failure,
            branch=receipt.branch,
            base_sha=receipt.base_sha,
            head_sha=receipt.head_sha,
            pr_url=receipt.draft_pr_url,
            tokens=receipt.tokens_used,
            wall_ms=receipt.walltime_ms,
            cpu_ms=receipt.cpu_ms,
            peak_rss_kb=receipt.peak_rss_kb,
            evidence={
                "terminal": receipt.terminal.value,
                "budget_violation": receipt.budget_violation,
                "executed_tests": list(receipt.executed_tests),
                "commands": list(receipt.commands),
            },
            provenance={
                "config_sha": receipt.config_sha,
                "receipt_sha": receipt.digest,
                "durable_state": receipt.durable_state,
            },
        )
