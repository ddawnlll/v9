"""Durable GitHub-issue queue with leases, retry budget and crash recovery (F-01).

The coordinator for unattended engineering needs exactly one worker per issue, an
ownership record that survives a process death, and a retry budget that stops an auditor
loop from spending forever. It must not need the network to do that: eligibility is
injected by the caller, and this module never talks to GitHub.

Storage: SQLite in WAL mode with ``BEGIN IMMEDIATE`` transactions. SQLite ships with
CPython, its file lock serialises writers across processes, and ``UPDATE ... WHERE
issue_id = ? AND state = ? AND token = ?`` is a compare-and-swap, so the claim and the
lease release are single atomic steps. A JSON file plus ``fcntl`` would have to
reimplement transactions (torn writes, fsync ordering, crash between unlink and rename)
for a component whose entire job is not to lose ownership when a worker dies. The
trade-off is recorded in ``reports/overnight/evidence/F-01.md``.

Invariants enforced by the schema, not by convention:

* One ``ACTIVE`` lease row per issue (partial unique index), so no crash can produce two
  owners of the same branch/PR/issue.
* ``COMPLETED``, ``EXHAUSTED`` and ``BLOCKED`` are terminal: no claim path selects them,
  and a duplicate ``enqueue`` never resets an existing row.
* ``attempts`` is incremented at claim time, inside the same transaction; a reclaimed
  expired lease therefore spends budget exactly once.
"""

from __future__ import annotations

import sqlite3
import time
import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from types import TracebackType
from typing import Any

from v8_next.evaluation.store import canonical
from v8_next.factory.lease import (
    DEFAULT_MAX_ATTEMPTS,
    CompletionReceipt,
    IssueRecord,
    IssueState,
    Lease,
    LeaseEvent,
    LeaseOutcome,
    decode_mapping,
    immutable_mapping,
)

__all__ = ["IssueQueue", "LeaseMismatchError", "QueueError", "UnknownIssueError"]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS issues (
    seq          INTEGER NOT NULL UNIQUE,
    issue_id     TEXT PRIMARY KEY,
    meta_json    TEXT NOT NULL,
    state        TEXT NOT NULL,
    priority     INTEGER NOT NULL,
    attempts     INTEGER NOT NULL,
    max_attempts INTEGER NOT NULL,
    enqueued_ns  INTEGER NOT NULL,
    updated_ns   INTEGER NOT NULL,
    worker_id    TEXT,
    token        TEXT,
    acquired_ns  INTEGER,
    expires_ns   INTEGER,
    last_error   TEXT,
    result_json  TEXT,
    completed_ns INTEGER
);

CREATE TABLE IF NOT EXISTS lease_events (
    token       TEXT PRIMARY KEY,
    issue_id    TEXT NOT NULL REFERENCES issues(issue_id),
    worker_id   TEXT NOT NULL,
    attempt     INTEGER NOT NULL,
    acquired_ns INTEGER NOT NULL,
    expires_ns  INTEGER NOT NULL,
    released_ns INTEGER,
    outcome     TEXT NOT NULL,
    note        TEXT
);

-- At most one live owner per issue, across every process and every restart.
CREATE UNIQUE INDEX IF NOT EXISTS lease_events_one_active
    ON lease_events(issue_id) WHERE outcome = 'ACTIVE';

CREATE INDEX IF NOT EXISTS issues_dispatch ON issues(state, priority, seq);
"""

#: Metadata keys consumed by the queue itself and not stored as free-form metadata.
_RESERVED_META = ("max_attempts", "priority")


class QueueError(RuntimeError):
    """Base class for durable-queue contract violations."""


class UnknownIssueError(QueueError, KeyError):
    """The issue id is not present in the queue."""


class LeaseMismatchError(QueueError):
    """The lease token is stale, expired, foreign, or the issue changed state elsewhere."""


def _require_text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"QUEUE: {name} must be a non-empty string")
    return value


def _require_ns(value: object, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"QUEUE: {name} must be a non-negative int (nanoseconds)")
    return value


def _require_positive_int(value: object, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ValueError(f"QUEUE: {name} must be an int >= 1")
    return value


def _optional_text(value: object) -> str | None:
    return None if value is None else str(value)


def _record(row: sqlite3.Row) -> IssueRecord:
    result_text = row["result_json"]
    completed = row["completed_ns"]
    return IssueRecord(
        issue_id=str(row["issue_id"]),
        state=IssueState(str(row["state"])),
        meta=decode_mapping(str(row["meta_json"])),
        priority=int(row["priority"]),
        attempts=int(row["attempts"]),
        max_attempts=int(row["max_attempts"]),
        enqueued_ns=int(row["enqueued_ns"]),
        updated_ns=int(row["updated_ns"]),
        worker_id=_optional_text(row["worker_id"]),
        token=_optional_text(row["token"]),
        acquired_ns=None if row["acquired_ns"] is None else int(row["acquired_ns"]),
        expires_ns=None if row["expires_ns"] is None else int(row["expires_ns"]),
        last_error=_optional_text(row["last_error"]),
        result=decode_mapping(str(result_text)) if result_text is not None else None,
        completed_ns=None if completed is None else int(completed),
    )


class IssueQueue:
    """SQLite-backed claim/lease/result store for the unattended coordinator.

    One instance owns one connection. Threads that share an instance must serialise on the
    database anyway; a coordinator that wants independent handles should construct one
    instance per thread/process — the SQL layer, not the Python object, is the mutex.
    """

    def __init__(
        self,
        path: str | Path,
        *,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        busy_timeout_ms: int = 5000,
    ) -> None:
        self._default_max_attempts = _require_positive_int(max_attempts, "max_attempts")
        busy = _require_positive_int(busy_timeout_ms, "busy_timeout_ms")
        self.path = Path(path).expanduser()
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(
            self.path,
            isolation_level=None,
            timeout=busy / 1000,
            check_same_thread=False,
        )
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA foreign_keys=ON")
        self._db.execute("PRAGMA busy_timeout=%d" % busy)
        if str(self.path) != ":memory:":
            self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=FULL")
        self._db.executescript(_SCHEMA)

    # -- lifecycle -----------------------------------------------------------------

    def close(self) -> None:
        self._db.close()

    def __enter__(self) -> IssueQueue:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        """Serialise one writer (waits up to ``busy_timeout_ms``) or roll back cleanly."""
        self._db.execute("BEGIN IMMEDIATE")
        try:
            yield self._db
        except BaseException:
            self._db.execute("ROLLBACK")
            raise
        self._db.execute("COMMIT")

    # -- queue ---------------------------------------------------------------------

    def enqueue(
        self,
        issue_id: str,
        meta: Mapping[str, Any] | None = None,
        *,
        now_ns: int | None = None,
    ) -> None:
        """Register an eligible issue once.

        Idempotent by construction: an issue already present keeps its lease, attempt
        count and terminal state, so replaying a coordinator's issue list after a crash
        can never duplicate ownership. ``max_attempts`` and ``priority`` are read from
        ``meta`` and are not stored as free-form metadata.
        """
        issue = _require_text(issue_id, "issue_id")
        if meta is not None and not isinstance(meta, Mapping):
            raise TypeError("QUEUE: meta must be a mapping")
        payload = dict(meta or {})
        priority = payload.pop("priority", 0)
        max_attempts = payload.pop("max_attempts", self._default_max_attempts)
        if not isinstance(priority, int) or isinstance(priority, bool):
            raise ValueError("QUEUE: priority must be an int")
        if not isinstance(max_attempts, int) or isinstance(max_attempts, bool):
            raise ValueError("QUEUE: max_attempts must be an int")
        max_attempts = _require_positive_int(max_attempts, "max_attempts")
        meta_json = canonical(payload)
        ts = time.time_ns() if now_ns is None else _require_ns(now_ns, "now_ns")
        with self._write():
            if self._db.execute(
                "SELECT 1 FROM issues WHERE issue_id = ?", (issue,)
            ).fetchone():
                return
            next_row = self._db.execute(
                "SELECT COALESCE(MAX(seq), 0) + 1 AS next_seq FROM issues"
            ).fetchone()
            assert next_row is not None
            seq = int(next_row["next_seq"])
            self._db.execute(
                "INSERT INTO issues (seq, issue_id, meta_json, state, priority, attempts,"
                " max_attempts, enqueued_ns, updated_ns)"
                " VALUES (?, ?, ?, ?, ?, 0, ?, ?, ?)",
                (
                    seq,
                    issue,
                    meta_json,
                    IssueState.PENDING.value,
                    priority,
                    max_attempts,
                    ts,
                    ts,
                ),
            )

    def claim(self, worker_id: str, now_ns: int, ttl_ns: int) -> Lease | None:
        """Atomically claim the best eligible issue, or return ``None``.

        Ordering is ``(priority ASC, enqueue sequence ASC)``, so a single-process caller
        sees a deterministic order. Expired leases are recovered first, in the same
        transaction, so a crashed worker's issue is reclaimable without a separate sweep.
        """
        worker = _require_text(worker_id, "worker_id")
        now = _require_ns(now_ns, "now_ns")
        ttl = _require_positive_int(ttl_ns, "ttl_ns")
        with self._write():
            self._expire_locked(now)
            candidates = self._db.execute(
                "SELECT issue_id, attempts, max_attempts FROM issues"
                " WHERE state = ? ORDER BY priority ASC, seq ASC",
                (IssueState.PENDING.value,),
            ).fetchall()
            for row in candidates:
                issue_id = str(row["issue_id"])
                attempts = int(row["attempts"])
                max_attempts = int(row["max_attempts"])
                if attempts >= max_attempts:
                    self._exhaust(issue_id, now, "RETRY_BUDGET_EXHAUSTED")
                    continue
                token = uuid.uuid4().hex
                attempt = attempts + 1
                updated = self._db.execute(
                    "UPDATE issues SET state = ?, attempts = ?, worker_id = ?, token = ?,"
                    " acquired_ns = ?, expires_ns = ?, updated_ns = ?, last_error = NULL"
                    " WHERE issue_id = ? AND state = ? AND attempts = ?",
                    (
                        IssueState.LEASED.value,
                        attempt,
                        worker,
                        token,
                        now,
                        now + ttl,
                        now,
                        issue_id,
                        IssueState.PENDING.value,
                        attempts,
                    ),
                )
                if updated.rowcount != 1:  # pragma: no cover - guarded by BEGIN IMMEDIATE
                    raise QueueError(f"QUEUE: claim lost the race for {issue_id}")
                self._db.execute(
                    "INSERT INTO lease_events (token, issue_id, worker_id, attempt,"
                    " acquired_ns, expires_ns, outcome) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        token,
                        issue_id,
                        worker,
                        attempt,
                        now,
                        now + ttl,
                        LeaseOutcome.ACTIVE.value,
                    ),
                )
                return Lease(
                    issue_id=issue_id,
                    worker_id=worker,
                    token=token,
                    acquired_ns=now,
                    expires_ns=now + ttl,
                    attempt=attempt,
                )
            return None

    def release_expired(self, now_ns: int) -> tuple[str, ...]:
        """Recover expired leases; returns the issue ids recovered by *this* call.

        Idempotent: a recovered issue becomes ``PENDING`` (or ``EXHAUSTED`` when its retry
        budget is spent) and its ``ACTIVE`` lease row becomes ``EXPIRED`` once. Calling
        again at the same instant finds nothing and returns an empty tuple.
        """
        now = _require_ns(now_ns, "now_ns")
        with self._write():
            return self._expire_locked(now)

    # -- worker results ------------------------------------------------------------

    def complete(
        self,
        lease: Lease,
        result: Mapping[str, Any],
        *,
        completed_ns: int | None = None,
    ) -> CompletionReceipt:
        """Record a successful result under a live lease.

        Idempotent for the same token and the same canonical result, which is what makes a
        coordinator restart after a committed completion safe: replaying ``complete``
        returns the stored receipt instead of creating a second piece of work.
        """
        if not isinstance(lease, Lease):
            raise TypeError("QUEUE: lease must be a Lease")
        if not isinstance(result, Mapping):
            raise TypeError("QUEUE: result must be a mapping")
        result_map = immutable_mapping(result, "result")
        result_json = canonical(dict(result_map))
        ts = time.time_ns() if completed_ns is None else _require_ns(completed_ns, "now")
        with self._write():
            row = self._db.execute(
                "SELECT state, token, worker_id, attempts, max_attempts, result_json,"
                " completed_ns FROM issues WHERE issue_id = ?",
                (lease.issue_id,),
            ).fetchone()
            if row is None:
                raise UnknownIssueError(lease.issue_id)
            state = IssueState(str(row["state"]))
            stored_token = _optional_text(row["token"])
            attempts = int(row["attempts"])
            max_attempts = int(row["max_attempts"])
            if state is IssueState.COMPLETED and stored_token == lease.token:
                if lease.attempt != attempts:
                    raise LeaseMismatchError(
                        f"COMPLETE: attempt {lease.attempt} is stale for {lease.issue_id}"
                    )
                stored_result = row["result_json"]
                if stored_result is None or str(stored_result) != result_json:
                    raise LeaseMismatchError(
                        f"COMPLETE: token {lease.token} already recorded a different result"
                    )
                completed = row["completed_ns"]
                return CompletionReceipt(
                    issue_id=lease.issue_id,
                    worker_id=lease.worker_id,
                    token=lease.token,
                    attempt=attempts,
                    attempts_used=attempts,
                    max_attempts=max_attempts,
                    completed_ns=ts if completed is None else int(completed),
                    result=result_map,
                    idempotent_replay=True,
                )
            self._require_live(row, lease)
            updated = self._db.execute(
                "UPDATE issues SET state = ?, worker_id = NULL, acquired_ns = NULL,"
                " expires_ns = NULL, updated_ns = ?, result_json = ?, completed_ns = ?,"
                " last_error = NULL WHERE issue_id = ? AND state = ? AND token = ?",
                (
                    IssueState.COMPLETED.value,
                    ts,
                    result_json,
                    ts,
                    lease.issue_id,
                    IssueState.LEASED.value,
                    lease.token,
                ),
            )
            if updated.rowcount != 1:  # pragma: no cover - guarded by BEGIN IMMEDIATE
                raise QueueError(f"QUEUE: completion lost the race for {lease.issue_id}")
            self._close_event(lease.token, LeaseOutcome.COMPLETED, ts, None)
            return CompletionReceipt(
                issue_id=lease.issue_id,
                worker_id=lease.worker_id,
                token=lease.token,
                attempt=lease.attempt,
                attempts_used=attempts,
                max_attempts=max_attempts,
                completed_ns=ts,
                result=result_map,
            )

    def fail(self, lease: Lease, reason: str) -> IssueState:
        """Release a lease after a failure and spend one attempt from the retry budget.

        Returns ``PENDING`` when the budget still allows a retry, ``EXHAUSTED`` when it is
        spent. Idempotent: replaying ``fail`` with the same token does not spend a second
        attempt.
        """
        if not isinstance(lease, Lease):
            raise TypeError("QUEUE: lease must be a Lease")
        note = _require_text(reason, "reason")
        with self._write():
            row = self._db.execute(
                "SELECT state, token, worker_id, attempts, max_attempts FROM issues"
                " WHERE issue_id = ?",
                (lease.issue_id,),
            ).fetchone()
            if row is None:
                raise UnknownIssueError(lease.issue_id)
            state = IssueState(str(row["state"]))
            stored_token = _optional_text(row["token"])
            attempts = int(row["attempts"])
            max_attempts = int(row["max_attempts"])
            if state in (IssueState.PENDING, IssueState.EXHAUSTED) and (
                stored_token == lease.token
            ):
                return state  # replay of an already-recorded failure
            self._require_live(row, lease)
            ts = time.time_ns()
            new_state = (
                IssueState.EXHAUSTED if attempts >= max_attempts else IssueState.PENDING
            )
            updated = self._db.execute(
                "UPDATE issues SET state = ?, worker_id = NULL, acquired_ns = NULL,"
                " expires_ns = NULL, updated_ns = ?, last_error = ?"
                " WHERE issue_id = ? AND state = ? AND token = ?",
                (
                    new_state.value,
                    ts,
                    note,
                    lease.issue_id,
                    IssueState.LEASED.value,
                    lease.token,
                ),
            )
            if updated.rowcount != 1:  # pragma: no cover - guarded by BEGIN IMMEDIATE
                raise QueueError(f"QUEUE: failure lost the race for {lease.issue_id}")
            self._close_event(lease.token, LeaseOutcome.FAILED, ts, note)
            return new_state

    def block(self, issue_id: str, reason: str) -> IssueState:
        """Terminal owner escalation; the issue is never claimed automatically again."""
        issue = _require_text(issue_id, "issue_id")
        note = _require_text(reason, "reason")
        now = time.time_ns()
        with self._write():
            row = self._db.execute(
                "SELECT state, token FROM issues WHERE issue_id = ?", (issue,)
            ).fetchone()
            if row is None:
                raise UnknownIssueError(issue)
            if IssueState(str(row["state"])) is IssueState.COMPLETED:
                raise QueueError(f"QUEUE: cannot block completed issue {issue}")
            token = _optional_text(row["token"])
            self._db.execute(
                "UPDATE issues SET state = ?, worker_id = NULL, acquired_ns = NULL,"
                " expires_ns = NULL, updated_ns = ?, last_error = ? WHERE issue_id = ?",
                (IssueState.BLOCKED.value, now, note, issue),
            )
            if token is not None:
                self._close_event(token, LeaseOutcome.BLOCKED, now, note)
            return IssueState.BLOCKED

    # -- reads ---------------------------------------------------------------------

    def state(self, issue_id: str) -> IssueState:
        """Current durable state; raises :class:`UnknownIssueError` for an unknown id."""
        issue = _require_text(issue_id, "issue_id")
        row = self._db.execute(
            "SELECT state FROM issues WHERE issue_id = ?", (issue,)
        ).fetchone()
        if row is None:
            raise UnknownIssueError(issue)
        return IssueState(str(row["state"]))

    def snapshot(self, issue_id: str) -> IssueRecord:
        """Status, budget and last result for one issue."""
        issue = _require_text(issue_id, "issue_id")
        row = self._db.execute(
            "SELECT * FROM issues WHERE issue_id = ?", (issue,)
        ).fetchone()
        if row is None:
            raise UnknownIssueError(issue)
        return _record(row)

    def history(self, issue_id: str) -> tuple[LeaseEvent, ...]:
        """Every attempt recorded for the issue, oldest first."""
        issue = _require_text(issue_id, "issue_id")
        rows = self._db.execute(
            "SELECT issue_id, worker_id, token, attempt, acquired_ns, expires_ns,"
            " released_ns, outcome, note FROM lease_events WHERE issue_id = ?"
            " ORDER BY rowid ASC",
            (issue,),
        ).fetchall()
        return tuple(
            LeaseEvent(
                issue_id=str(row["issue_id"]),
                worker_id=str(row["worker_id"]),
                token=str(row["token"]),
                attempt=int(row["attempt"]),
                acquired_ns=int(row["acquired_ns"]),
                expires_ns=int(row["expires_ns"]),
                released_ns=None if row["released_ns"] is None else int(row["released_ns"]),
                outcome=LeaseOutcome(str(row["outcome"])),
                note=_optional_text(row["note"]),
            )
            for row in rows
        )

    # -- internals -----------------------------------------------------------------

    def _expire_locked(self, now_ns: int) -> tuple[str, ...]:
        """Recover leases past their deadline; caller must hold the write transaction."""
        rows = self._db.execute(
            "SELECT issue_id, token, attempts, max_attempts FROM issues"
            " WHERE state = ? AND expires_ns IS NOT NULL AND expires_ns <= ?"
            " ORDER BY seq ASC",
            (IssueState.LEASED.value, now_ns),
        ).fetchall()
        recovered: list[str] = []
        for row in rows:
            issue_id = str(row["issue_id"])
            token = _optional_text(row["token"])
            attempts = int(row["attempts"])
            max_attempts = int(row["max_attempts"])
            new_state = (
                IssueState.PENDING if attempts < max_attempts else IssueState.EXHAUSTED
            )
            updated = self._db.execute(
                "UPDATE issues SET state = ?, worker_id = NULL, token = NULL,"
                " acquired_ns = NULL, expires_ns = NULL, updated_ns = ?, last_error = ?"
                " WHERE issue_id = ? AND state = ? AND expires_ns <= ?",
                (
                    new_state.value,
                    now_ns,
                    "LEASE_EXPIRED",
                    issue_id,
                    IssueState.LEASED.value,
                    now_ns,
                ),
            )
            if updated.rowcount != 1:  # pragma: no cover - guarded by BEGIN IMMEDIATE
                continue
            if token is not None:
                self._close_event(token, LeaseOutcome.EXPIRED, now_ns, "LEASE_EXPIRED")
            recovered.append(issue_id)
        return tuple(recovered)

    def _exhaust(self, issue_id: str, now_ns: int, reason: str) -> None:
        self._db.execute(
            "UPDATE issues SET state = ?, updated_ns = ?, last_error = ?"
            " WHERE issue_id = ? AND state = ?",
            (IssueState.EXHAUSTED.value, now_ns, reason, issue_id, IssueState.PENDING.value),
        )

    def _close_event(
        self, token: str, outcome: LeaseOutcome, now_ns: int, note: str | None
    ) -> None:
        self._db.execute(
            "UPDATE lease_events SET outcome = ?, released_ns = ?, note = ?"
            " WHERE token = ? AND outcome = ?",
            (outcome.value, now_ns, note, token, LeaseOutcome.ACTIVE.value),
        )

    @staticmethod
    def _require_live(row: sqlite3.Row, lease: Lease) -> None:
        """Raise unless the stored row is the live lease this token belongs to."""
        state = IssueState(str(row["state"]))
        if state is not IssueState.LEASED:
            raise LeaseMismatchError(
                f"LEASE: issue {lease.issue_id} is {state.value}, not LEASED"
            )
        if _optional_text(row["token"]) != lease.token:
            raise LeaseMismatchError(f"LEASE: token {lease.token} is not the active token")
        if _optional_text(row["worker_id"]) != lease.worker_id:
            raise LeaseMismatchError(
                f"LEASE: worker {lease.worker_id} does not own {lease.issue_id}"
            )
        if int(row["attempts"]) != lease.attempt:
            raise LeaseMismatchError(
                f"LEASE: attempt {lease.attempt} is stale for {lease.issue_id}"
            )
