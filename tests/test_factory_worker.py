"""F-02: bounded issue-to-PR worker — pipeline, budget, authorization, restart safety.

The pipeline steps are fakes: F-02 is the *driver* contract, so the tests assert what the
worker does with a step's declared cost, request and result — not that git or pytest ran.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from v8_next.factory.lease import MEASURED, IssueState, LeaseOutcome, OwnerReview
from v8_next.factory.memory import FindingOutcome, MemoryKind, ResearchMemory
from v8_next.factory.queue import IssueQueue, LeaseMismatchError
from v8_next.factory.worker import (
    FORBIDDEN_CAPABILITIES,
    PHASES,
    BoundedWorker,
    Capability,
    FunctionStep,
    StepContext,
    StepPhase,
    StepResult,
    TaskBudget,
    WorkerAuthorizationError,
    WorkerTerminal,
)

BASE_SHA = "a" * 40
HEAD_SHA = "b" * 40
PR_URL = "https://example.invalid/ddawnlll/v9/pull/99"
TTL_NS = 60 * 1_000_000_000
START_NS = 1_000_000_000


def _prepare_result(**overrides: object) -> StepResult:
    fields: dict[str, object] = {
        "phase": StepPhase.PREPARE,
        "ok": True,
        "tokens": 10,
        "wall_ms": 100,
        "cpu_ms": 50,
        "peak_rss_kb": 1024,
        "commands": ("git switch -c feat/f02",),
        "branch": "feat/f02",
        "base_sha": BASE_SHA,
    }
    fields.update(overrides)
    return StepResult(**fields)  # type: ignore[arg-type]


def _implement_result(**overrides: object) -> StepResult:
    fields: dict[str, object] = {
        "phase": StepPhase.IMPLEMENT,
        "ok": True,
        "tokens": 100,
        "wall_ms": 1000,
        "cpu_ms": 500,
        "peak_rss_kb": 2048,
        "commands": ("pytest -q tests/test_factory_worker.py",),
        "head_sha": HEAD_SHA,
    }
    fields.update(overrides)
    return StepResult(**fields)  # type: ignore[arg-type]


def _verify_result(**overrides: object) -> StepResult:
    fields: dict[str, object] = {
        "phase": StepPhase.VERIFY,
        "ok": True,
        "tokens": 40,
        "wall_ms": 400,
        "cpu_ms": 200,
        "peak_rss_kb": 1500,
        "executed_tests": ("tests/test_factory_worker.py::test_happy",),
    }
    fields.update(overrides)
    return StepResult(**fields)  # type: ignore[arg-type]


def _publish_result(**overrides: object) -> StepResult:
    fields: dict[str, object] = {
        "phase": StepPhase.PUBLISH,
        "ok": True,
        "tokens": 20,
        "wall_ms": 200,
        "cpu_ms": 100,
        "peak_rss_kb": 1200,
        "commands": ("gh pr create --draft",),
        "pr_url": PR_URL,
        "draft": True,
    }
    fields.update(overrides)
    return StepResult(**fields)  # type: ignore[arg-type]


def _fixed(result: StepResult) -> Callable[[StepContext], StepResult]:
    def run(context: StepContext) -> StepResult:
        return result

    return run


def _steps(
    *,
    prepare: StepResult | None = None,
    implement: StepResult | None = None,
    verify: StepResult | None = None,
    publish: StepResult | None = None,
) -> dict[StepPhase, FunctionStep]:
    supplied = {
        StepPhase.PREPARE: prepare if prepare is not None else _prepare_result(),
        StepPhase.IMPLEMENT: implement if implement is not None else _implement_result(),
        StepPhase.VERIFY: verify if verify is not None else _verify_result(),
        StepPhase.PUBLISH: publish if publish is not None else _publish_result(),
    }
    return {phase: FunctionStep(phase, _fixed(result)) for phase, result in supplied.items()}


def _worker(
    queue: IssueQueue,
    tmp_path: Path,
    *,
    steps: dict[StepPhase, FunctionStep] | None = None,
    budget: TaskBudget | None = None,
    memory: ResearchMemory | None = None,
    worker_id: str = "w1",
) -> BoundedWorker:
    path = tmp_path / "memory.sqlite3"
    return BoundedWorker(
        queue,
        worker_id=worker_id,
        budget=budget if budget is not None else TaskBudget(max_tokens=10_000, max_wall_s=60),
        steps=_steps() if steps is None else steps,
        lease_ttl_ns=TTL_NS,
        memory=memory if memory is not None else ResearchMemory(path),
    )


def _queue(tmp_path: Path) -> IssueQueue:
    return IssueQueue(tmp_path / "queue.sqlite3")


def test_happy_path_reaches_published_draft_with_full_receipt(tmp_path: Path) -> None:
    queue = _queue(tmp_path)
    memory = ResearchMemory(tmp_path / "memory.sqlite3")
    worker = _worker(queue, tmp_path, memory=memory)
    worker.register("i1", {"repo": "ddawnlll/v9", "files": ["src/x.py"]})

    run = worker.run_once(now_ns=START_NS)
    assert run is not None
    receipt = run.receipt

    assert receipt.terminal is WorkerTerminal.PUBLISHED_DRAFT
    assert receipt.published is True
    assert receipt.draft_pr_url == PR_URL
    assert receipt.branch == "feat/f02"
    assert receipt.base_sha == BASE_SHA
    assert receipt.head_sha == HEAD_SHA
    assert receipt.code_sha == HEAD_SHA
    assert receipt.executed_tests == ("tests/test_factory_worker.py::test_happy",)
    assert receipt.commands == (
        "git switch -c feat/f02",
        "pytest -q tests/test_factory_worker.py",
        "gh pr create --draft",
    )
    assert receipt.tokens_used == 170
    assert receipt.walltime_ms == 1700
    assert receipt.cpu_ms == 850
    assert receipt.peak_rss_kb == 2048
    assert receipt.budget == worker.budget
    assert receipt.config_sha == worker.budget.digest
    assert receipt.attempt == 1
    assert receipt.durable_state == IssueState.COMPLETED.value
    assert receipt.failure is None
    assert receipt.budget_violation is None
    assert [phase.phase for phase in receipt.phases] == list(PHASES)
    assert receipt.owner_review_required is True
    assert receipt.owner_review is OwnerReview.REQUIRED
    assert receipt.measurement()["code_sha"] == MEASURED
    assert receipt.measurement()["executed_tests"] == MEASURED
    assert len(receipt.digest) == 64
    assert receipt.budget_use["tokens"] == {"used": 170, "limit": 10_000, "within": True}
    assert receipt.budget_use["peak_rss_kb"] == {
        "used": 2048,
        "limit": worker.budget.max_rss_kb,
        "within": True,
    }

    assert run.durable_state is IssueState.COMPLETED
    assert run.completion is not None
    assert run.completion.idempotent_replay is False
    assert run.completion.owner_review is OwnerReview.REQUIRED

    record = queue.snapshot("i1")
    assert record.state is IssueState.COMPLETED
    assert record.result is not None
    assert record.result["terminal"] == WorkerTerminal.PUBLISHED_DRAFT.value
    assert record.result["receipt_sha"] == receipt.digest
    assert record.result["budget_use"]["tokens"]["used"] == 170
    assert record.result["owner_review"] == OwnerReview.REQUIRED.value
    assert queue.history("i1")[-1].outcome is LeaseOutcome.COMPLETED

    events = memory.events("i1")
    assert [event.kind for event in events] == [MemoryKind.ATTEMPT, MemoryKind.OUTCOME]
    assert events[0].attempt == 1
    # A draft publish requires owner review: it is recorded as PENDING_REVIEW, never as a
    # verified finding.
    assert events[-1].outcome is FindingOutcome.PENDING_REVIEW
    assert events[-1].pr_url == PR_URL

    # Nothing is left claimable and the run cannot be replayed into a second attempt.
    assert worker.run_once(now_ns=START_NS + 1) is None
    queue.close()
    memory.close()


def test_receipt_is_deterministic_for_identical_runs(tmp_path: Path) -> None:
    digests = []
    for name in ("a", "b"):
        queue = IssueQueue(tmp_path / f"{name}.sqlite3")
        worker = _worker(queue, tmp_path)
        worker.register("i1")
        run = worker.run_once(now_ns=START_NS)
        assert run is not None
        digests.append(run.receipt.digest)
        queue.close()
    assert digests[0] == digests[1]


def test_verify_failure_blocks_with_failure_preserved_and_no_pr(tmp_path: Path) -> None:
    queue = _queue(tmp_path)
    memory = ResearchMemory(tmp_path / "memory.sqlite3")
    published: list[StepPhase] = []

    def publish(context: StepContext) -> StepResult:
        published.append(StepPhase.PUBLISH)
        return _publish_result()

    steps = _steps(
        verify=_verify_result(
            ok=False,
            failure="FAILED tests/test_factory_worker.py::test_happy - AssertionError",
            tokens=40,
        )
    )
    steps[StepPhase.PUBLISH] = FunctionStep(StepPhase.PUBLISH, publish)
    worker = _worker(queue, tmp_path, steps=steps, memory=memory)
    worker.register("i1")

    run = worker.run_once(now_ns=START_NS)
    assert run is not None
    receipt = run.receipt

    assert receipt.terminal is WorkerTerminal.BLOCKED
    assert receipt.published is False
    assert receipt.draft_pr_url is None
    assert receipt.failure == (
        "STEP_FAILED:verify:FAILED tests/test_factory_worker.py::test_happy - AssertionError"
    )
    assert receipt.executed_tests == ("tests/test_factory_worker.py::test_happy",)
    assert receipt.head_sha == HEAD_SHA
    assert receipt.durable_state == IssueState.BLOCKED.value
    assert run.completion is None
    assert run.durable_state is IssueState.BLOCKED
    assert published == []

    record = queue.snapshot("i1")
    assert record.state is IssueState.BLOCKED
    assert record.result is None
    assert record.last_error is not None
    assert "STEP_FAILED:verify" in record.last_error
    assert queue.history("i1")[-1].outcome is LeaseOutcome.BLOCKED
    assert worker.run_once(now_ns=START_NS + 1) is None

    outcome = memory.latest_outcome("i1")
    assert outcome is not None
    assert outcome.outcome is FindingOutcome.BLOCKED
    assert outcome.detail is not None and "AssertionError" in outcome.detail
    queue.close()
    memory.close()


def test_budget_exhaustion_is_exhausted_and_never_publishes(tmp_path: Path) -> None:
    queue = _queue(tmp_path)
    memory = ResearchMemory(tmp_path / "memory.sqlite3")
    published: list[StepPhase] = []

    def publish(context: StepContext) -> StepResult:
        published.append(StepPhase.PUBLISH)
        return _publish_result()

    steps = _steps(implement=_implement_result(tokens=10), verify=_verify_result(tokens=90))
    steps[StepPhase.PUBLISH] = FunctionStep(StepPhase.PUBLISH, publish)
    worker = _worker(
        queue,
        tmp_path,
        steps=steps,
        budget=TaskBudget(max_tokens=100, max_attempts=1, max_wall_s=60),
        memory=memory,
    )
    worker.register("i1")

    run = worker.run_once(now_ns=START_NS)
    assert run is not None
    receipt = run.receipt

    assert receipt.terminal is WorkerTerminal.EXHAUSTED
    assert receipt.published is False
    assert receipt.draft_pr_url is None
    assert receipt.budget_violation is not None
    assert receipt.budget_violation.startswith("BUDGET_EXHAUSTED:tokens:")
    assert receipt.tokens_used == 110
    assert receipt.executed_tests == ("tests/test_factory_worker.py::test_happy",)
    assert published == []
    assert run.durable_state is IssueState.EXHAUSTED
    assert receipt.retry_allowed is False
    assert queue.state("i1") is IssueState.EXHAUSTED
    assert queue.history("i1")[-1].outcome is LeaseOutcome.FAILED
    assert worker.run_once(now_ns=START_NS + 1) is None

    outcome = memory.latest_outcome("i1")
    assert outcome is not None
    assert outcome.outcome is FindingOutcome.EXHAUSTED
    queue.close()
    memory.close()


@pytest.mark.parametrize(
    ("budget", "step_overrides", "dimension"),
    [
        (
            TaskBudget(max_tokens=10_000, max_wall_s=2, max_attempts=1),
            {"wall_ms": 3_000},
            "wall_ms",
        ),
        (
            TaskBudget(max_tokens=10_000, max_cpu_s=1, max_attempts=1),
            {"cpu_ms": 1_001},
            "cpu_ms",
        ),
        (
            TaskBudget(max_tokens=10_000, max_rss_kb=2_400, max_attempts=1),
            {"peak_rss_kb": 2_401},
            "peak_rss_kb",
        ),
    ],
)
def test_each_budget_dimension_exhausts_before_publish(
    tmp_path: Path, budget: TaskBudget, step_overrides: dict[str, int], dimension: str
) -> None:
    queue = _queue(tmp_path)
    steps = _steps(publish=_publish_result(**step_overrides))
    worker = _worker(queue, tmp_path, steps=steps, budget=budget)
    worker.register("i1")

    run = worker.run_once(now_ns=START_NS)
    assert run is not None
    assert run.receipt.terminal is WorkerTerminal.EXHAUSTED
    assert run.receipt.budget_violation is not None
    assert f"BUDGET_EXHAUSTED:{dimension}:" in run.receipt.budget_violation
    assert run.receipt.published is False
    # publish ran (the cost is only knowable afterwards) but the run is not published.
    assert run.receipt.draft_pr_url == PR_URL
    assert queue.state("i1") is IssueState.EXHAUSTED
    queue.close()


def test_attempt_budget_exhausted_before_any_step(tmp_path: Path) -> None:
    queue = _queue(tmp_path)
    calls: list[StepPhase] = []

    def record(phase: StepPhase, result: StepResult) -> FunctionStep:
        def run(context: StepContext) -> StepResult:
            calls.append(phase)
            return result

        return FunctionStep(phase, run)

    steps = {
        StepPhase.PREPARE: record(StepPhase.PREPARE, _prepare_result()),
        StepPhase.IMPLEMENT: record(StepPhase.IMPLEMENT, _implement_result()),
        StepPhase.VERIFY: record(StepPhase.VERIFY, _verify_result()),
        StepPhase.PUBLISH: record(StepPhase.PUBLISH, _publish_result()),
    }
    worker = _worker(
        queue,
        tmp_path,
        steps=steps,
        budget=TaskBudget(max_tokens=10_000, max_attempts=1),
    )
    worker.register("i1")
    lease = queue.claim("w1", START_NS, TTL_NS)
    assert lease is not None
    queue.fail(lease, "first attempt failed elsewhere")

    # Retry budget (max_attempts=1) is spent, so the queue cannot hand out a second lease.
    assert queue.claim("w2", START_NS + 1, TTL_NS) is None
    assert queue.state("i1") is IssueState.EXHAUSTED
    assert calls == []
    queue.close()


def test_step_result_cost_fields_are_required() -> None:
    with pytest.raises(ValidationError):
        StepResult(phase=StepPhase.PREPARE, ok=True)  # type: ignore[call-arg]
    with pytest.raises(ValidationError):
        TaskBudget(max_tokens=0)
    with pytest.raises(ValidationError):
        TaskBudget(max_attempts=0)


def test_non_draft_publish_is_refused_at_the_contract(tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        _publish_result(draft=False)
    with pytest.raises(ValidationError):
        _publish_result(draft=None)


@pytest.mark.parametrize("capability", sorted(FORBIDDEN_CAPABILITIES, key=lambda c: c.value))
@pytest.mark.parametrize("declared", [True, False])
def test_authorization_guard_refuses_forbidden_capabilities(
    tmp_path: Path, capability: Capability, declared: bool
) -> None:
    """Declared or returned, a forbidden capability stops the run before its effect."""
    published: list[StepPhase] = []
    if declared:
        steps = _steps()
        steps[StepPhase.VERIFY] = FunctionStep(
            StepPhase.VERIFY, _fixed(_verify_result()), frozenset({capability})
        )
    else:
        steps = _steps(verify=_verify_result(requests=(capability,)))

    def publish(context: StepContext) -> StepResult:
        published.append(StepPhase.PUBLISH)
        return _publish_result()

    steps[StepPhase.PUBLISH] = FunctionStep(StepPhase.PUBLISH, publish)
    queue = _queue(tmp_path)
    memory = ResearchMemory(tmp_path / "memory.sqlite3")
    worker = _worker(queue, tmp_path, steps=steps, memory=memory)
    worker.register("i1")

    with pytest.raises(WorkerAuthorizationError) as excinfo:
        worker.run_once(now_ns=START_NS)

    error = excinfo.value
    assert error.capability is capability
    assert error.phase is StepPhase.VERIFY
    assert error.reason == f"UNAUTHORIZED_CAPABILITY:{capability.value}"
    assert error.receipt.terminal is WorkerTerminal.BLOCKED
    assert error.receipt.published is False
    assert error.receipt.draft_pr_url is None
    assert error.receipt.failure == error.reason
    assert published == []
    assert queue.state("i1") is IssueState.BLOCKED
    outcome = memory.latest_outcome("i1")
    assert outcome is not None and outcome.outcome is FindingOutcome.BLOCKED
    queue.close()
    memory.close()


def test_protected_branch_prepare_is_refused(tmp_path: Path) -> None:
    queue = _queue(tmp_path)
    worker = _worker(queue, tmp_path, steps=_steps(prepare=_prepare_result(branch="main")))
    worker.register("i1")

    with pytest.raises(WorkerAuthorizationError) as excinfo:
        worker.run_once(now_ns=START_NS)

    assert excinfo.value.capability is Capability.PUSH_TO_MAIN
    assert excinfo.value.reason == "PROTECTED_BRANCH:main"
    assert queue.state("i1") is IssueState.BLOCKED
    assert excinfo.value.receipt.durable_state == IssueState.BLOCKED.value
    queue.close()


def test_missing_step_blocks(tmp_path: Path) -> None:
    queue = _queue(tmp_path)
    steps = _steps()
    del steps[StepPhase.VERIFY]
    worker = _worker(queue, tmp_path, steps=steps)
    worker.register("i1")

    run = worker.run_once(now_ns=START_NS)
    assert run is not None
    assert run.receipt.terminal is WorkerTerminal.BLOCKED
    assert run.receipt.failure == "STEP_MISSING:verify"
    assert run.receipt.draft_pr_url is None
    assert queue.state("i1") is IssueState.BLOCKED
    queue.close()


def test_raising_step_blocks_with_reason_preserved(tmp_path: Path) -> None:
    class Boom:
        phase = StepPhase.IMPLEMENT
        requests: frozenset[Capability] = frozenset()

        def execute(self, context: StepContext) -> StepResult:
            raise RuntimeError("worktree is dirty")

    steps: dict[StepPhase, FunctionStep] = {
        StepPhase.PREPARE: FunctionStep(StepPhase.PREPARE, _fixed(_prepare_result())),
        StepPhase.IMPLEMENT: Boom(),  # type: ignore[dict-item]
        StepPhase.VERIFY: FunctionStep(StepPhase.VERIFY, _fixed(_verify_result())),
        StepPhase.PUBLISH: FunctionStep(StepPhase.PUBLISH, _fixed(_publish_result())),
    }
    queue = _queue(tmp_path)
    worker = _worker(queue, tmp_path, steps=steps)  # type: ignore[arg-type]
    worker.register("i1")

    run = worker.run_once(now_ns=START_NS)
    assert run is not None
    assert run.receipt.terminal is WorkerTerminal.BLOCKED
    assert run.receipt.failure == "STEP_RAISED:implement:RuntimeError: worktree is dirty"
    assert run.receipt.head_sha is None
    assert queue.state("i1") is IssueState.BLOCKED
    queue.close()


def test_phase_mismatch_blocks(tmp_path: Path) -> None:
    steps: dict[StepPhase, FunctionStep] = {
        StepPhase.PREPARE: FunctionStep(StepPhase.PREPARE, _fixed(_prepare_result())),
        StepPhase.IMPLEMENT: FunctionStep(
            StepPhase.IMPLEMENT, _fixed(_prepare_result())
        ),
        StepPhase.VERIFY: FunctionStep(StepPhase.VERIFY, _fixed(_verify_result())),
        StepPhase.PUBLISH: FunctionStep(StepPhase.PUBLISH, _fixed(_publish_result())),
    }
    queue = _queue(tmp_path)
    worker = _worker(queue, tmp_path, steps=steps)
    worker.register("i1")

    run = worker.run_once(now_ns=START_NS)
    assert run is not None
    assert run.receipt.terminal is WorkerTerminal.BLOCKED
    assert run.receipt.failure == "STEP_PHASE_MISMATCH:implement:prepare"
    queue.close()


def test_worker_rejects_step_registered_under_the_wrong_phase(tmp_path: Path) -> None:
    queue = _queue(tmp_path)
    with pytest.raises(ValueError):
        BoundedWorker(
            queue,
            worker_id="w1",
            steps={StepPhase.VERIFY: FunctionStep(StepPhase.PUBLISH, _fixed(_publish_result()))},
        )
    queue.close()


def test_register_keeps_a_caller_supplied_attempt_budget(tmp_path: Path) -> None:
    queue = _queue(tmp_path)
    worker = _worker(queue, tmp_path)
    worker.register("i1", {"repo": "ddawnlll/v9", "max_attempts": 5})
    record = queue.snapshot("i1")
    assert record.max_attempts == 5
    assert record.meta["repo"] == "ddawnlll/v9"
    assert record.state is IssueState.PENDING
    queue.close()


def test_forced_restart_neither_duplicates_the_claim_nor_loses_memory(tmp_path: Path) -> None:
    queue = _queue(tmp_path)
    memory_path = tmp_path / "memory.sqlite3"
    memory = ResearchMemory(memory_path)

    class Killed:
        phase = StepPhase.VERIFY
        requests: frozenset[Capability] = frozenset()

        def execute(self, context: StepContext) -> StepResult:
            raise KeyboardInterrupt("process killed mid-task")

    steps: dict[StepPhase, Any] = {
        StepPhase.PREPARE: FunctionStep(StepPhase.PREPARE, _fixed(_prepare_result())),
        StepPhase.IMPLEMENT: FunctionStep(StepPhase.IMPLEMENT, _fixed(_implement_result())),
        StepPhase.VERIFY: Killed(),  # type: ignore[dict-item]
        StepPhase.PUBLISH: FunctionStep(StepPhase.PUBLISH, _fixed(_publish_result())),
    }
    worker = BoundedWorker(
        queue, worker_id="w1", steps=steps, lease_ttl_ns=TTL_NS, memory=memory  # type: ignore[arg-type]
    )
    worker.register("i1")

    with pytest.raises(KeyboardInterrupt):
        worker.run_once(now_ns=START_NS)

    # The attempt was recorded before any step ran; the lease is still held by the dead run.
    assert [event.kind for event in memory.events("i1")] == [MemoryKind.ATTEMPT]
    assert queue.state("i1") is IssueState.LEASED
    lease = queue.snapshot("i1").lease
    assert lease is not None and lease.attempt == 1

    queue.close()
    memory.close()

    # --- process restart -------------------------------------------------------
    queue = IssueQueue(tmp_path / "queue.sqlite3")
    memory = ResearchMemory(memory_path)
    restarted = BoundedWorker(
        queue, worker_id="w1", steps=_steps(), lease_ttl_ns=TTL_NS, memory=memory
    )
    # Inside the TTL the lease still belongs to the previous run: no duplicate claim.
    assert restarted.run_once(now_ns=START_NS + 1) is None
    assert restarted.run_once(now_ns=START_NS + TTL_NS - 1) is None
    assert queue.state("i1") is IssueState.LEASED
    assert len([e for e in memory.events("i1") if e.kind is MemoryKind.ATTEMPT]) == 1

    # At expiry the issue is recovered exactly once and resumed as attempt 2.
    run = restarted.run_once(now_ns=START_NS + TTL_NS)
    assert run is not None
    assert run.receipt.attempt == 2
    assert run.receipt.terminal is WorkerTerminal.PUBLISHED_DRAFT
    assert queue.state("i1") is IssueState.COMPLETED

    history = queue.history("i1")
    assert [event.outcome for event in history] == [
        LeaseOutcome.EXPIRED,
        LeaseOutcome.COMPLETED,
    ]
    assert [event.attempt for event in history] == [1, 2]
    assert len({event.token for event in history}) == 2
    assert all(event.released_ns is not None for event in history)

    events = memory.events("i1")
    assert [(event.kind, event.attempt) for event in events] == [
        (MemoryKind.ATTEMPT, 1),
        (MemoryKind.ATTEMPT, 2),
        (MemoryKind.OUTCOME, 2),
    ]
    assert events[-1].outcome is FindingOutcome.PENDING_REVIEW

    report = memory.efficiency()
    assert report.issues == 1
    assert report.attempts == 2
    assert report.retries == 1
    assert report.verified_findings == 0
    assert report.findings == 0
    assert report.completed_issues == 1
    assert report.pending_review == 1
    queue.close()
    memory.close()


def test_nothing_claimable_returns_none(tmp_path: Path) -> None:
    queue = _queue(tmp_path)
    worker = _worker(queue, tmp_path)
    assert worker.run_once(now_ns=START_NS) is None
    queue.close()


def test_a_declared_request_that_is_not_a_capability_fails_at_construction(
    tmp_path: Path,
) -> None:
    """A look-alike string must not survive to the phase that would have used it."""
    queue = _queue(tmp_path)
    steps = _steps()
    steps[StepPhase.VERIFY] = FunctionStep(
        StepPhase.VERIFY,
        _fixed(_verify_result()),
        frozenset({"sealed_oos_access"}),  # type: ignore[arg-type]
    )
    with pytest.raises(TypeError) as raised:
        BoundedWorker(queue, worker_id="w1", steps=steps)
    assert "sealed_oos_access" in str(raised.value)
    assert "not a Capability" in str(raised.value)

    forbidden_string = _steps()
    forbidden_string[StepPhase.PUBLISH] = FunctionStep(
        StepPhase.PUBLISH,
        _fixed(_publish_result()),
        frozenset({"MAIN_MERGE"}),  # type: ignore[arg-type]
    )
    with pytest.raises(TypeError) as raised:
        BoundedWorker(queue, worker_id="w1", steps=forbidden_string)
    assert "MAIN_MERGE" in str(raised.value)

    # Nothing was claimed, so no issue is left holding an unusable lease.
    queue.enqueue("i1", now_ns=START_NS)
    assert queue.state("i1") is IssueState.PENDING
    queue.close()


def test_a_shape_shifting_declaration_blocks_and_releases_the_lease(
    tmp_path: Path,
) -> None:
    """A step that passes construction and then declares a non-member must not leak a lease."""

    class ShapeShifting:
        phase = StepPhase.VERIFY

        def __init__(self) -> None:
            self.reads = 0

        @property
        def requests(self) -> frozenset[Capability]:
            self.reads += 1
            # Honest at construction, a look-alike string when the phase runs.
            if self.reads == 1:
                return frozenset()
            return frozenset({"sealed_oos_access"})  # type: ignore[return-value]

        def execute(self, context: StepContext) -> StepResult:
            return _verify_result()

    steps: dict[StepPhase, Any] = {
        StepPhase.PREPARE: FunctionStep(StepPhase.PREPARE, _fixed(_prepare_result())),
        StepPhase.IMPLEMENT: FunctionStep(StepPhase.IMPLEMENT, _fixed(_implement_result())),
        StepPhase.VERIFY: ShapeShifting(),
        StepPhase.PUBLISH: FunctionStep(StepPhase.PUBLISH, _fixed(_publish_result())),
    }
    queue = _queue(tmp_path)
    worker = BoundedWorker(queue, worker_id="w1", steps=steps, lease_ttl_ns=TTL_NS)
    worker.register("i1")

    with pytest.raises(TypeError):
        worker.run_once(now_ns=START_NS)

    # The unexpected driver failure still left a terminal state behind: no held lease.
    assert queue.state("i1") is IssueState.BLOCKED
    assert worker.run_once(now_ns=START_NS + 1) is None
    queue.close()


def test_a_failed_step_still_has_its_returned_capabilities_audited(tmp_path: Path) -> None:
    """The refusal names the capability, even when the step also failed."""
    queue = _queue(tmp_path)
    steps = _steps(
        verify=_verify_result(
            ok=False,
            failure="FAILED tests/test_x.py::test_y",
            requests=(Capability.MAIN_MERGE,),
        )
    )
    worker = _worker(queue, tmp_path, steps=steps)
    worker.register("i1")

    with pytest.raises(WorkerAuthorizationError) as excinfo:
        worker.run_once(now_ns=START_NS)

    error = excinfo.value
    assert error.capability is Capability.MAIN_MERGE
    assert error.reason == "UNAUTHORIZED_CAPABILITY:MAIN_MERGE"
    assert error.phase is StepPhase.VERIFY
    assert error.receipt.terminal is WorkerTerminal.BLOCKED
    assert queue.state("i1") is IssueState.BLOCKED
    queue.close()


def test_a_stale_worker_cannot_block_the_new_owner(tmp_path: Path) -> None:
    queue = _queue(tmp_path)
    queue.enqueue("i1", now_ns=START_NS)
    first = queue.claim("w1", START_NS, TTL_NS)
    assert first is not None
    assert queue.release_expired(START_NS + TTL_NS) == ("i1",)
    second = queue.claim("w2", START_NS + TTL_NS, TTL_NS)
    assert second is not None and second.token != first.token

    with pytest.raises(LeaseMismatchError):
        queue.block_lease(first, "late block from the expired worker")

    record = queue.snapshot("i1")
    assert record.state is IssueState.LEASED
    assert record.lease is not None and record.lease.token == second.token
    assert queue.history("i1")[-1].outcome is LeaseOutcome.ACTIVE

    # The same refusal through the worker's own terminal path: its BLOCKED transition is
    # lease-scoped, so the newer owner keeps the issue.
    memory = ResearchMemory(tmp_path / "memory.sqlite3")
    stale = BoundedWorker(
        queue,
        worker_id="w1",
        steps=_steps(
            verify=_verify_result(ok=False, failure="FAILED tests/test_x.py::test_y")
        ),
        lease_ttl_ns=TTL_NS,
        memory=memory,
    )
    with pytest.raises(LeaseMismatchError):
        stale.run_lease(first)

    record = queue.snapshot("i1")
    assert record.state is IssueState.LEASED
    assert record.lease is not None and record.lease.token == second.token
    queue.close()
    memory.close()
