"""F-01: durable issue queue, lease recovery and owner-review result contracts.

Everything here runs against a real SQLite file under ``tmp_path``; the concurrency tests
use separate connections (threads) and a forked process so the lock being exercised is
SQLite's, not a Python mutex.
"""

from __future__ import annotations

import multiprocessing
import sqlite3
import threading
from dataclasses import FrozenInstanceError, replace
from pathlib import Path

import pytest

from v8_next.factory import (
    DEFAULT_MAX_ATTEMPTS,
    EVIDENCE_FIELDS,
    MEASURED,
    NOT_MEASURED,
    CompletionReceipt,
    IssueQueue,
    IssueState,
    Lease,
    LeaseMismatchError,
    LeaseOutcome,
    OwnerReview,
    UnknownIssueError,
    evidence_receipt,
)
from v8_next.factory.queue import QueueError


def _db(tmp_path: Path) -> Path:
    return tmp_path / "queue.sqlite3"


def _active(queue: IssueQueue, issue_id: str) -> list[LeaseOutcome]:
    return [
        event.outcome
        for event in queue.history(issue_id)
        if event.outcome is LeaseOutcome.ACTIVE
    ]


def _claim_in_child(args: tuple[str, str, int, int]) -> str | None:
    """Fork target: open its own handle, claim once, return the token (or None)."""
    path, worker, now_ns, ttl_ns = args
    queue = IssueQueue(path)
    try:
        lease = queue.claim(worker, now_ns, ttl_ns)
        return None if lease is None else lease.token
    finally:
        queue.close()


def test_enqueue_is_idempotent_and_keeps_first_metadata(tmp_path: Path) -> None:
    queue = IssueQueue(_db(tmp_path))
    queue.enqueue("i1", {"repo": "ddawnlll/v9", "files": ["a.py"]}, now_ns=1)
    queue.enqueue("i1", {"repo": "someone/else"}, now_ns=2)

    snapshot = queue.snapshot("i1")
    assert snapshot.state is IssueState.PENDING
    assert snapshot.meta == {"repo": "ddawnlll/v9", "files": ["a.py"]}
    assert snapshot.attempts == 0
    assert snapshot.max_attempts == DEFAULT_MAX_ATTEMPTS
    assert snapshot.budget_remaining == DEFAULT_MAX_ATTEMPTS
    assert snapshot.lease is None
    queue.close()


def test_unknown_issue_raises(tmp_path: Path) -> None:
    queue = IssueQueue(_db(tmp_path))
    with pytest.raises(UnknownIssueError):
        queue.state("missing")
    with pytest.raises(KeyError):
        queue.snapshot("missing")
    queue.close()


def test_claim_order_is_priority_then_enqueue_sequence(tmp_path: Path) -> None:
    queue = IssueQueue(_db(tmp_path))
    queue.enqueue("a", now_ns=1)
    queue.enqueue("b", now_ns=1)
    queue.enqueue("urgent", {"priority": -1}, now_ns=2)
    queue.enqueue("c", now_ns=3)

    order = []
    for index in range(4):
        lease = queue.claim(f"w{index}", 10, 100)
        assert lease is not None
        order.append(lease.issue_id)
    assert order == ["urgent", "a", "b", "c"]
    assert queue.claim("w9", 10, 100) is None
    queue.close()


def test_two_handles_two_threads_claim_exactly_once(tmp_path: Path) -> None:
    path = _db(tmp_path)
    first = IssueQueue(path)
    second = IssueQueue(path)
    first.enqueue("i1", now_ns=0)

    barrier = threading.Barrier(2)
    grants: list[Lease | None] = []
    lock = threading.Lock()

    def run(queue: IssueQueue, worker: str) -> None:
        barrier.wait()
        lease = queue.claim(worker, 100, 50)
        with lock:
            grants.append(lease)

    threads = [
        threading.Thread(target=run, args=(first, "w1")),
        threading.Thread(target=run, args=(second, "w2")),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    winners = [lease for lease in grants if lease is not None]
    assert len(winners) == 1
    assert first.state("i1") is IssueState.LEASED
    assert len(first.history("i1")) == 1
    assert _active(first, "i1") == [LeaseOutcome.ACTIVE]
    assert first.snapshot("i1").attempts == 1
    first.close()
    second.close()


def test_many_threads_never_share_an_issue(tmp_path: Path) -> None:
    path = _db(tmp_path)
    setup = IssueQueue(path)
    for index in range(8):
        setup.enqueue(f"i{index}", now_ns=index)
    setup.close()

    barrier = threading.Barrier(4)
    tokens: list[str] = []
    issues: list[str] = []
    lock = threading.Lock()

    def run(worker: str) -> None:
        queue = IssueQueue(path)
        barrier.wait()
        while True:
            lease = queue.claim(worker, 1000, 500)
            if lease is None:
                break
            with lock:
                tokens.append(lease.token)
                issues.append(lease.issue_id)
        queue.close()

    threads = [threading.Thread(target=run, args=(f"w{i}",)) for i in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(issues) == 8
    assert len(set(issues)) == 8
    assert len(set(tokens)) == 8

    verify = IssueQueue(path)
    for issue in issues:
        assert verify.state(issue) is IssueState.LEASED
        assert verify.snapshot(issue).attempts == 1
        assert _active(verify, issue) == [LeaseOutcome.ACTIVE]
    verify.close()


@pytest.mark.filterwarnings("ignore:This process .* is multi-threaded.*:DeprecationWarning")
def test_cross_process_claims_yield_one_owner(tmp_path: Path) -> None:
    if "fork" not in multiprocessing.get_all_start_methods():
        pytest.skip("fork start method unavailable on this platform")
    path = _db(tmp_path)
    queue = IssueQueue(path)
    queue.enqueue("i1", now_ns=0)
    context = multiprocessing.get_context("fork")
    with context.Pool(2) as pool:
        tokens = pool.map(
            _claim_in_child,
            [(str(path), "p1", 100, 50), (str(path), "p2", 100, 50)],
        )
    assert len([token for token in tokens if token is not None]) == 1
    assert _active(queue, "i1") == [LeaseOutcome.ACTIVE]
    assert queue.snapshot("i1").attempts == 1
    queue.close()


def test_release_expired_is_idempotent(tmp_path: Path) -> None:
    queue = IssueQueue(_db(tmp_path))
    queue.enqueue("i1", now_ns=0)
    lease = queue.claim("w1", 100, 50)
    assert lease is not None

    assert queue.release_expired(149) == ()
    assert queue.state("i1") is IssueState.LEASED
    assert queue.release_expired(150) == ("i1",)
    assert queue.state("i1") is IssueState.PENDING
    assert queue.release_expired(150) == ()
    assert queue.release_expired(10**12) == ()

    events = queue.history("i1")
    assert [event.outcome for event in events] == [LeaseOutcome.EXPIRED]
    assert events[-1].released_ns == 150
    assert queue.snapshot("i1").attempts == 1
    assert queue.snapshot("i1").lease is None

    replacement = queue.claim("w2", 200, 50)
    assert replacement is not None
    assert replacement.attempt == 2
    assert replacement.token != lease.token
    assert _active(queue, "i1") == [LeaseOutcome.ACTIVE]
    queue.close()


def test_crash_and_reopen_resume_without_duplicate_ownership(tmp_path: Path) -> None:
    path = _db(tmp_path)
    queue = IssueQueue(path)
    queue.enqueue("i1", {"files": ["x.py"]}, now_ns=0)
    crashed = queue.claim("w1", 100, 10)
    assert crashed is not None
    queue.close()  # simulated coordinator crash: the object and connection are gone

    resumed = IssueQueue(path)
    assert resumed.state("i1") is IssueState.LEASED
    assert resumed.snapshot("i1").meta == {"files": ["x.py"]}
    # Replaying the eligibility list must not reset the durable ownership record.
    resumed.enqueue("i1", {"files": ["x.py"]}, now_ns=5)
    assert resumed.snapshot("i1").attempts == 1
    assert resumed.claim("w2", 109, 10) is None
    assert _active(resumed, "i1") == [LeaseOutcome.ACTIVE]

    assert resumed.release_expired(110) == ("i1",)
    reclaimed = resumed.claim("w2", 110, 10)
    assert reclaimed is not None
    assert reclaimed.attempt == 2
    assert len(_active(resumed, "i1")) == 1
    assert [event.outcome for event in resumed.history("i1")] == [
        LeaseOutcome.EXPIRED,
        LeaseOutcome.ACTIVE,
    ]
    resumed.close()


def test_retry_budget_exhausts_and_never_reclaims(tmp_path: Path) -> None:
    path = _db(tmp_path)
    queue = IssueQueue(path)
    queue.enqueue("i1", {"max_attempts": 2}, now_ns=0)

    first = queue.claim("w1", 100, 10)
    assert first is not None
    assert first.attempt == 1
    assert queue.fail(first, "auditor: missing evidence") is IssueState.PENDING
    assert queue.snapshot("i1").budget_remaining == 1
    # Replaying the failure for the same token must not spend a second attempt.
    assert queue.fail(first, "auditor: missing evidence") is IssueState.PENDING
    assert queue.snapshot("i1").attempts == 1

    second = queue.claim("w2", 200, 10)
    assert second is not None
    assert second.attempt == 2
    assert queue.fail(second, "auditor: still missing") is IssueState.EXHAUSTED
    assert queue.snapshot("i1").budget_remaining == 0
    assert queue.claim("w3", 300, 10) is None
    assert queue.release_expired(10**12) == ()
    assert queue.state("i1") is IssueState.EXHAUSTED

    queue.close()
    reopened = IssueQueue(path)
    reopened.enqueue("i1", {"max_attempts": 2}, now_ns=400)
    assert reopened.state("i1") is IssueState.EXHAUSTED
    assert reopened.claim("w4", 500, 10) is None
    reopened.close()


def test_expired_lease_spends_the_last_attempt(tmp_path: Path) -> None:
    queue = IssueQueue(_db(tmp_path))
    queue.enqueue("i1", {"max_attempts": 1}, now_ns=0)
    lease = queue.claim("w1", 100, 10)
    assert lease is not None

    assert queue.release_expired(110) == ("i1",)
    assert queue.state("i1") is IssueState.EXHAUSTED
    assert queue.claim("w2", 200, 10) is None
    assert queue.history("i1")[-1].outcome is LeaseOutcome.EXPIRED
    assert queue.snapshot("i1").budget_remaining == 0
    queue.close()


def test_complete_is_token_bound_and_idempotent(tmp_path: Path) -> None:
    path = _db(tmp_path)
    queue = IssueQueue(path)
    queue.enqueue("i1", now_ns=0)
    lease = queue.claim("w1", 100, 50)
    assert lease is not None
    assert queue.snapshot("i1").lease == lease

    result = {"branch": "wave1/w4", "tests": ["tests/test_factory_queue_lease.py"]}
    receipt = queue.complete(lease, result, completed_ns=120)
    assert isinstance(receipt, CompletionReceipt)
    assert receipt.owner_review_required is True
    assert receipt.owner_review is OwnerReview.REQUIRED
    assert receipt.attempts_used == 1
    assert receipt.max_attempts == DEFAULT_MAX_ATTEMPTS
    assert receipt.completed_ns == 120
    assert queue.state("i1") is IssueState.COMPLETED
    assert queue.claim("w2", 200, 50) is None

    replay = queue.complete(lease, result, completed_ns=999)
    assert replay.idempotent_replay is True
    assert replay.completed_ns == 120
    with pytest.raises(LeaseMismatchError):
        queue.complete(lease, {"branch": "other"}, completed_ns=999)

    foreign = replace(lease, token="0" * 32)
    with pytest.raises(LeaseMismatchError):
        queue.complete(foreign, result, completed_ns=999)
    stale = replace(lease, attempt=2)
    with pytest.raises(LeaseMismatchError):
        queue.complete(stale, result, completed_ns=999)

    queue.close()
    reopened = IssueQueue(path)
    snapshot = reopened.snapshot("i1")
    assert snapshot.state is IssueState.COMPLETED
    assert snapshot.result == result
    assert snapshot.result is not None
    with pytest.raises(TypeError):
        snapshot.result["branch"] = "mutated"  # type: ignore[index]
    reopened.close()


def test_expired_lease_cannot_complete_or_fail(tmp_path: Path) -> None:
    queue = IssueQueue(_db(tmp_path))
    queue.enqueue("i1", now_ns=0)
    lease = queue.claim("w1", 100, 10)
    assert lease is not None
    assert queue.release_expired(110) == ("i1",)
    with pytest.raises(LeaseMismatchError):
        queue.complete(lease, {"ok": True}, completed_ns=111)
    with pytest.raises(LeaseMismatchError):
        queue.fail(lease, "late failure")
    assert queue.state("i1") is IssueState.PENDING
    assert queue.snapshot("i1").attempts == 1
    queue.close()


def test_block_is_terminal_and_closes_the_active_lease(tmp_path: Path) -> None:
    queue = IssueQueue(_db(tmp_path))
    queue.enqueue("i1", now_ns=0)
    lease = queue.claim("w1", 100, 50)
    assert lease is not None
    assert queue.block("i1", "owner: needs design decision") is IssueState.BLOCKED
    assert queue.claim("w2", 200, 50) is None
    assert queue.state("i1") is IssueState.BLOCKED
    assert queue.history("i1")[-1].outcome is LeaseOutcome.BLOCKED
    with pytest.raises(LeaseMismatchError):
        queue.complete(lease, {"ok": True}, completed_ns=201)

    queue.enqueue("i2", now_ns=0)
    done = queue.claim("w3", 300, 50)
    assert done is not None
    queue.complete(done, {"ok": True}, completed_ns=310)
    with pytest.raises(QueueError):
        queue.block("i2", "owner: late escalation")
    assert queue.state("i2") is IssueState.COMPLETED
    queue.close()


def test_database_enforces_single_active_owner(tmp_path: Path) -> None:
    path = _db(tmp_path)
    queue = IssueQueue(path)
    queue.enqueue("i1", now_ns=0)
    assert queue.claim("w1", 100, 50) is not None

    raw = sqlite3.connect(path)
    try:
        assert raw.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        with pytest.raises(sqlite3.IntegrityError):
            raw.execute(
                "INSERT INTO lease_events (token, issue_id, worker_id, attempt,"
                " acquired_ns, expires_ns, outcome)"
                " VALUES ('t2', 'i1', 'w2', 1, 100, 200, 'ACTIVE')"
            )
        raw.rollback()
        assert raw.execute("SELECT COUNT(*) FROM issues").fetchone()[0] == 1
    finally:
        raw.close()
    queue.close()


def test_owner_review_cannot_be_waived(tmp_path: Path) -> None:
    queue = IssueQueue(_db(tmp_path))
    queue.enqueue("i1", now_ns=0)
    lease = queue.claim("w1", 100, 50)
    assert lease is not None
    receipt = queue.complete(lease, {"ok": True}, completed_ns=120)
    queue.close()

    with pytest.raises(FrozenInstanceError):
        receipt.owner_review_required = False  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        receipt.result = {}  # type: ignore[misc]
    with pytest.raises(ValueError):
        replace(receipt, owner_review_required=False)
    with pytest.raises(ValueError):
        replace(receipt, owner_review="DONE")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        CompletionReceipt(
            issue_id="i1",
            worker_id="w1",
            token="t",
            attempt=1,
            attempts_used=1,
            max_attempts=3,
            completed_ns=0,
            result={},
            owner_review_required=False,
        )


def test_evidence_receipt_marks_every_missing_measurement() -> None:
    receipt = evidence_receipt()
    assert set(EVIDENCE_FIELDS) <= set(receipt)
    for field in EVIDENCE_FIELDS:
        assert receipt[field] is None
        assert receipt["measurement"][field] == NOT_MEASURED
    assert receipt["budget"] == {"spent": None, "limit": None, "remaining": None}
    assert receipt["measurement"]["budget"] == NOT_MEASURED
    assert receipt["owner_review_required"] is True
    assert receipt["owner_review"] == OwnerReview.REQUIRED.value


def test_evidence_receipt_only_carries_supplied_measurements() -> None:
    receipt = evidence_receipt(
        code_sha="a" * 40,
        config_sha="b" * 40,
        executed_tests=["tests/test_factory_queue_lease.py::test_x"] * 2,
        walltime_ms=1234,
        peak_rss_kb=98_304,
        budget_spent=2,
        budget_limit=3,
    )
    assert receipt["code_sha"] == "a" * 40
    assert receipt["config_sha"] == "b" * 40
    assert receipt["executed_tests"] == ["tests/test_factory_queue_lease.py::test_x"]
    assert receipt["walltime_ms"] == 1234
    assert receipt["peak_rss_kb"] == 98_304
    assert receipt["budget"] == {"spent": 2, "limit": 3, "remaining": 1}
    assert all(value == MEASURED for value in receipt["measurement"].values())


def test_evidence_receipt_rejects_inconsistent_inputs() -> None:
    with pytest.raises(ValueError):
        evidence_receipt(walltime_ms=-1)
    with pytest.raises(ValueError):
        evidence_receipt(peak_rss_kb=-1)
    with pytest.raises(ValueError):
        evidence_receipt(code_sha="   ")
    with pytest.raises(ValueError):
        evidence_receipt(executed_tests="tests/test_x.py")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        evidence_receipt(budget_limit=0)
    with pytest.raises(ValueError):
        evidence_receipt(budget_spent=-1)


def test_lease_and_queue_input_validation(tmp_path: Path) -> None:
    queue = IssueQueue(_db(tmp_path))
    with pytest.raises(ValueError):
        queue.claim("", 100, 10)
    with pytest.raises(ValueError):
        queue.claim("w1", 100, 0)
    with pytest.raises(ValueError):
        queue.claim("w1", -1, 10)
    with pytest.raises(ValueError):
        queue.enqueue("", {})
    with pytest.raises(ValueError):
        queue.enqueue("i1", {"max_attempts": 0})
    with pytest.raises(ValueError):
        queue.enqueue("i1", {"priority": "high"})
    queue.close()

    with pytest.raises(ValueError):
        Lease("i1", "w1", "t", 10, 10, 1)
    with pytest.raises(ValueError):
        Lease("i1", "w1", "t", 10, 20, 0)
    lease = Lease("i1", "w1", "t", 10, 20, 1)
    assert lease.is_expired(19) is False
    assert lease.is_expired(20) is True


def test_queue_errors_share_a_base(tmp_path: Path) -> None:
    queue = IssueQueue(_db(tmp_path))
    with pytest.raises(QueueError):
        queue.state("missing")
    queue.enqueue("i1", now_ns=0)
    lease = queue.claim("w1", 100, 10)
    assert lease is not None
    with pytest.raises(QueueError):
        queue.complete(replace(lease, token="deadbeef"), {"ok": True})
    queue.close()
