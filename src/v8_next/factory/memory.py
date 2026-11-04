"""Durable append-only research memory and efficiency accounting (F-03).

The factory burns compute on issues that mostly do *not* produce a shipped change: a
negative result, a ``NO_EDGE`` finding or a blocked run is the common outcome. Remembering
those outcomes is what stops the next shift from paying for the same dead end again, so the
memory has to survive the process that wrote it — unlike a log line, it is an input.

Storage mirrors :mod:`v8_next.factory.queue` deliberately: one SQLite file, WAL journal,
``synchronous=FULL`` and ``BEGIN IMMEDIATE`` for writers. The same trade-off applies (a JSON
file plus ``fcntl`` would have to reimplement torn-write recovery for a component whose whole
job is not to lose rows when a worker dies), and one storage discipline is easier to audit
than two. Rationale is recorded in ``reports/overnight/evidence/F-03.md``.

Invariants enforced here, not by convention:

* **Append-only.** No ``UPDATE`` and no ``DELETE`` is issued by this module; a correction is
  a new row. :class:`MemoryEvent` is frozen so a caller cannot mutate a returned record and
  believe the store changed.
* **Replay-idempotent.** ``event_id`` is a canonical digest of the *content* (never the wall
  clock), and the insert is ``ON CONFLICT DO NOTHING``. Replaying a ``record`` call after a
  crash returns the stored row instead of duplicating the attempt or the finding.
* **Unmeasured is not zero.** A metric with any missing sample is reported as
  :data:`~v8_next.factory.lease.NOT_MEASURED` rather than as a partial sum that would read
  like a real measurement.

Every number in :meth:`ResearchMemory.efficiency` comes from a recorded row; nothing is
probed, estimated or filled in from a default.
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from enum import StrEnum
from pathlib import Path
from types import TracebackType
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from v8_next.evaluation.store import canonical
from v8_next.factory.lease import MEASURED, NOT_MEASURED, decode_mapping
from v8_next.telemetry.identity import canonical_digest

__all__ = [
    "COMPLETION_OUTCOMES",
    "EfficiencyReport",
    "FindingOutcome",
    "MEASURED",
    "Measurement",
    "MeasurementState",
    "MemoryError",
    "MemoryEvent",
    "MemoryKind",
    "NOT_MEASURED",
    "ResearchMemory",
    "measured",
    "not_measured",
]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS research_events (
    seq             INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id        TEXT NOT NULL UNIQUE,
    issue_id        TEXT NOT NULL,
    kind            TEXT NOT NULL,
    attempt         INTEGER NOT NULL,
    outcome         TEXT,
    worker_id       TEXT,
    detail          TEXT,
    branch          TEXT,
    base_sha        TEXT,
    head_sha        TEXT,
    pr_url          TEXT,
    pr_accepted     INTEGER,
    tokens          INTEGER,
    wall_ms         INTEGER,
    cpu_ms          INTEGER,
    peak_rss_kb     INTEGER,
    evidence_json   TEXT NOT NULL,
    provenance_json TEXT NOT NULL,
    recorded_ns     INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS research_events_issue ON research_events(issue_id, seq);
CREATE INDEX IF NOT EXISTS research_events_outcome ON research_events(outcome);
"""

#: Digest domain tag, so a memory id cannot collide with another factory identity.
_EVENT_TAG = "factory-research-memory"


class MemoryError(RuntimeError):
    """Base class for research-memory contract violations."""


class MemoryKind(StrEnum):
    """What one append-only row asserts about an issue."""

    #: Work started (or was resumed) on this issue+attempt. Outcome still unknown.
    ATTEMPT = "ATTEMPT"
    #: Terminal disposition of one attempt.
    OUTCOME = "OUTCOME"
    #: A verified or negative research finding.
    FINDING = "FINDING"


class FindingOutcome(StrEnum):
    """Terminal disposition; the negatives are first-class, not a text field.

    ``NO_EDGE`` is the honest answer most of the time: the test was run and the edge is
    not there. ``BLOCKED`` and ``INVALID`` mean the run could not conclude, which is
    different from a negative result and must not be counted as one. ``PENDING_REVIEW``
    is a draft publish: the work exists, but nothing is verified until the owner reviews
    it, so it is not a finding and never counts as one.
    """

    VERIFIED = "VERIFIED"
    NO_EDGE = "NO_EDGE"
    BLOCKED = "BLOCKED"
    INVALID = "INVALID"
    EXHAUSTED = "EXHAUSTED"
    PENDING_REVIEW = "PENDING_REVIEW"


#: Outcomes that mean the issue reached a completed hand-off (a published draft, or a
#: verified finding recorded against it). ``completed_issues`` counts these, and only on
#: ``OUTCOME`` rows, so a finding can never inflate it.
COMPLETION_OUTCOMES: frozenset[FindingOutcome] = frozenset(
    {FindingOutcome.VERIFIED, FindingOutcome.PENDING_REVIEW}
)


class MeasurementState(StrEnum):
    """Whether a reported number was actually measured."""

    MEASURED = MEASURED
    NOT_MEASURED = NOT_MEASURED


class Measurement(BaseModel):
    """A caller-supplied number that is either measured or explicitly absent.

    ``value`` is ``None`` exactly when ``state`` is ``NOT_MEASURED``, so there is no way to
    read a plausible default out of an unmeasured metric.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    value: int | float | None = None
    state: MeasurementState = MeasurementState.NOT_MEASURED

    @model_validator(mode="after")
    def _consistent(self) -> Measurement:
        if self.state is MeasurementState.NOT_MEASURED and self.value is not None:
            raise ValueError("MEASUREMENT: NOT_MEASURED must not carry a value")
        if self.state is MeasurementState.MEASURED and self.value is None:
            raise ValueError("MEASUREMENT: MEASURED requires a value")
        return self

    @property
    def is_measured(self) -> bool:
        """True only when a real number is present."""
        return self.state is MeasurementState.MEASURED


def measured(value: int | float) -> Measurement:
    """Report ``value`` as a real measurement."""
    return Measurement(value=value, state=MeasurementState.MEASURED)


def not_measured() -> Measurement:
    """Report a metric the caller could not measure. Never substituted."""
    return Measurement()


class MemoryEvent(BaseModel):
    """One immutable row of the research memory."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    event_id: str
    issue_id: str
    kind: MemoryKind
    attempt: int = Field(ge=1)
    outcome: FindingOutcome | None = None
    worker_id: str | None = None
    detail: str | None = None
    branch: str | None = None
    base_sha: str | None = None
    head_sha: str | None = None
    pr_url: str | None = None
    pr_accepted: bool | None = None
    tokens: int | None = Field(default=None, ge=0)
    wall_ms: int | None = Field(default=None, ge=0)
    cpu_ms: int | None = Field(default=None, ge=0)
    peak_rss_kb: int | None = Field(default=None, ge=0)
    evidence: Mapping[str, Any] = Field(default_factory=dict)
    provenance: Mapping[str, Any] = Field(default_factory=dict)
    recorded_ns: int = Field(ge=0)

    @property
    def is_negative(self) -> bool:
        """True for the outcomes that assert the work did *not* ship an edge."""
        return self.outcome in (
            FindingOutcome.NO_EDGE,
            FindingOutcome.BLOCKED,
            FindingOutcome.INVALID,
            FindingOutcome.EXHAUSTED,
        )


class EfficiencyReport(BaseModel):
    """Per-compute-hour accounting over the whole memory.

    Counts (issues, attempts, retries, outcomes) are structurally known from the rows and
    are plain ints. Costs are :class:`Measurement` values because a row that never reported
    a token count makes the total unknown — and an unknown total is reported as unknown.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    events: int
    issues: int
    attempts: int
    retries: int
    verified_findings: int
    findings: int
    completed_issues: int
    blocked: int
    no_edge: int
    invalid: int
    exhausted: int
    pending_review: int
    accepted_prs: Measurement
    reported_prs: int
    tokens: Measurement
    wall_hours: Measurement
    cpu_hours: Measurement
    memory_high_water_kb: Measurement
    findings_per_compute_hour: Measurement


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"MEMORY: {name} must be a non-empty string")
    return value.strip()


def _optional_text(value: object, name: str) -> str | None:
    if value is None:
        return None
    return _text(value, name)


def _optional_count(value: object, name: str) -> int | None:
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"MEMORY: {name} must be an int when supplied")
    if value < 0:
        raise ValueError(f"MEMORY: {name} must be non-negative")
    return value


def _json_object(value: Mapping[str, Any] | None, name: str) -> str:
    payload: dict[str, Any] = {} if value is None else dict(value)
    canonical(payload)  # rejects NaN/inf and non-serialisable payloads
    return canonical(payload)


def _event(row: sqlite3.Row) -> MemoryEvent:
    accepted = row["pr_accepted"]
    outcome = row["outcome"]
    return MemoryEvent(
        event_id=str(row["event_id"]),
        issue_id=str(row["issue_id"]),
        kind=MemoryKind(str(row["kind"])),
        attempt=int(row["attempt"]),
        outcome=None if outcome is None else FindingOutcome(str(outcome)),
        worker_id=None if row["worker_id"] is None else str(row["worker_id"]),
        detail=None if row["detail"] is None else str(row["detail"]),
        branch=None if row["branch"] is None else str(row["branch"]),
        base_sha=None if row["base_sha"] is None else str(row["base_sha"]),
        head_sha=None if row["head_sha"] is None else str(row["head_sha"]),
        pr_url=None if row["pr_url"] is None else str(row["pr_url"]),
        pr_accepted=None if accepted is None else bool(accepted),
        tokens=None if row["tokens"] is None else int(row["tokens"]),
        wall_ms=None if row["wall_ms"] is None else int(row["wall_ms"]),
        cpu_ms=None if row["cpu_ms"] is None else int(row["cpu_ms"]),
        peak_rss_kb=None if row["peak_rss_kb"] is None else int(row["peak_rss_kb"]),
        evidence=dict(decode_mapping(str(row["evidence_json"]))),
        provenance=dict(decode_mapping(str(row["provenance_json"]))),
        recorded_ns=int(row["recorded_ns"]),
    )


class ResearchMemory:
    """Append-only research memory over one SQLite file.

    One instance owns one connection. Separate processes (or a restarted worker) open their
    own instance on the same path; SQL's file lock, not the Python object, serialises
    writers. ``record`` is safe to replay after a crash.
    """

    def __init__(self, path: str | Path, *, busy_timeout_ms: int = 5000) -> None:
        if not isinstance(busy_timeout_ms, int) or isinstance(busy_timeout_ms, bool):
            raise ValueError("MEMORY: busy_timeout_ms must be an int")
        if busy_timeout_ms < 1:
            raise ValueError("MEMORY: busy_timeout_ms must be positive")
        self.path = Path(path).expanduser()
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(
            self.path,
            isolation_level=None,
            timeout=busy_timeout_ms / 1000,
            check_same_thread=False,
        )
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA foreign_keys=ON")
        self._db.execute("PRAGMA busy_timeout=%d" % busy_timeout_ms)
        if str(self.path) != ":memory:":
            self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=FULL")
        self._db.executescript(_SCHEMA)

    # -- lifecycle -----------------------------------------------------------------

    def close(self) -> None:
        self._db.close()

    def __enter__(self) -> ResearchMemory:
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
        self._db.execute("BEGIN IMMEDIATE")
        try:
            yield self._db
        except BaseException:
            self._db.execute("ROLLBACK")
            raise
        self._db.execute("COMMIT")

    @contextmanager
    def _read(self) -> Iterator[sqlite3.Connection]:
        """One consistent snapshot for a multi-statement report."""
        self._db.execute("BEGIN")
        try:
            yield self._db
        except BaseException:
            self._db.execute("ROLLBACK")
            raise
        self._db.execute("COMMIT")

    # -- writes --------------------------------------------------------------------

    def record(
        self,
        issue_id: str,
        kind: MemoryKind,
        *,
        attempt: int = 1,
        outcome: FindingOutcome | None = None,
        worker_id: str | None = None,
        detail: str | None = None,
        branch: str | None = None,
        base_sha: str | None = None,
        head_sha: str | None = None,
        pr_url: str | None = None,
        pr_accepted: bool | None = None,
        tokens: int | None = None,
        wall_ms: int | None = None,
        cpu_ms: int | None = None,
        peak_rss_kb: int | None = None,
        evidence: Mapping[str, Any] | None = None,
        provenance: Mapping[str, Any] | None = None,
        recorded_ns: int | None = None,
    ) -> MemoryEvent:
        """Append one row, or return the identical row a previous replay appended.

        Identity is content-addressed: ``event_id`` is a digest over every supplied field
        *except* the wall clock, so a coordinator that dies after the commit and retries the
        same call finds one row, not two. Two genuinely distinct attempts differ in
        ``attempt`` and are stored separately. ``recorded_ns`` defaults to the current time
        and is never part of the identity.
        """
        issue = _text(issue_id, "issue_id")
        if not isinstance(kind, MemoryKind):
            raise TypeError("MEMORY: kind must be a MemoryKind")
        if not isinstance(attempt, int) or isinstance(attempt, bool) or attempt < 1:
            raise ValueError("MEMORY: attempt must be an int >= 1")
        if outcome is not None and not isinstance(outcome, FindingOutcome):
            raise TypeError("MEMORY: outcome must be a FindingOutcome")
        if kind is MemoryKind.ATTEMPT and outcome is not None:
            raise ValueError("MEMORY: an ATTEMPT row has no outcome yet")
        if kind is not MemoryKind.ATTEMPT and outcome is None:
            raise ValueError(f"MEMORY: a {kind.value} row requires an outcome")
        if pr_accepted is not None and not isinstance(pr_accepted, bool):
            raise TypeError("MEMORY: pr_accepted must be a bool when supplied")
        ts = time.time_ns() if recorded_ns is None else recorded_ns
        if not isinstance(ts, int) or isinstance(ts, bool) or ts < 0:
            raise ValueError("MEMORY: recorded_ns must be a non-negative int")

        worker = _optional_text(worker_id, "worker_id")
        note = _optional_text(detail, "detail")
        branch_name = _optional_text(branch, "branch")
        base = _optional_text(base_sha, "base_sha")
        head = _optional_text(head_sha, "head_sha")
        url = _optional_text(pr_url, "pr_url")
        token_count = _optional_count(tokens, "tokens")
        wall = _optional_count(wall_ms, "wall_ms")
        cpu = _optional_count(cpu_ms, "cpu_ms")
        rss = _optional_count(peak_rss_kb, "peak_rss_kb")
        evidence_json = _json_object(evidence, "evidence")
        provenance_json = _json_object(provenance, "provenance")

        identity = {
            "issue_id": issue,
            "kind": kind.value,
            "attempt": attempt,
            "outcome": None if outcome is None else outcome.value,
            "worker_id": worker,
            "detail": note,
            "branch": branch_name,
            "base_sha": base,
            "head_sha": head,
            "pr_url": url,
            "pr_accepted": pr_accepted,
            "tokens": token_count,
            "wall_ms": wall,
            "cpu_ms": cpu,
            "peak_rss_kb": rss,
            "evidence": evidence_json,
            "provenance": provenance_json,
        }
        event_id = canonical_digest(_EVENT_TAG, identity)
        with self._write():
            self._db.execute(
                "INSERT INTO research_events (event_id, issue_id, kind, attempt, outcome,"
                " worker_id, detail, branch, base_sha, head_sha, pr_url, pr_accepted,"
                " tokens, wall_ms, cpu_ms, peak_rss_kb, evidence_json, provenance_json,"
                " recorded_ns)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(event_id) DO NOTHING",
                (
                    event_id,
                    issue,
                    kind.value,
                    attempt,
                    None if outcome is None else outcome.value,
                    worker,
                    note,
                    branch_name,
                    base,
                    head,
                    url,
                    None if pr_accepted is None else int(pr_accepted),
                    token_count,
                    wall,
                    cpu,
                    rss,
                    evidence_json,
                    provenance_json,
                    ts,
                ),
            )
            row = self._db.execute(
                "SELECT * FROM research_events WHERE event_id = ?", (event_id,)
            ).fetchone()
        if row is None:  # pragma: no cover - the row was just inserted
            raise MemoryError(f"MEMORY: event {event_id} vanished after insert")
        return _event(row)

    # -- reads ---------------------------------------------------------------------

    def events(self, issue_id: str) -> tuple[MemoryEvent, ...]:
        """Every row recorded for the issue, oldest first."""
        issue = _text(issue_id, "issue_id")
        rows = self._db.execute(
            "SELECT * FROM research_events WHERE issue_id = ? ORDER BY seq ASC", (issue,)
        ).fetchall()
        return tuple(_event(row) for row in rows)

    def issues(self) -> tuple[str, ...]:
        """Issue ids with at least one row, in first-seen order."""
        rows = self._db.execute(
            "SELECT issue_id FROM research_events GROUP BY issue_id ORDER BY MIN(seq) ASC"
        ).fetchall()
        return tuple(str(row["issue_id"]) for row in rows)

    def findings(
        self, issue_id: str, *, outcome: FindingOutcome | None = None
    ) -> tuple[MemoryEvent, ...]:
        """Research findings for the issue, including the negative ones."""
        issue = _text(issue_id, "issue_id")
        if outcome is None:
            rows = self._db.execute(
                "SELECT * FROM research_events WHERE issue_id = ? AND kind = ?"
                " ORDER BY seq ASC",
                (issue, MemoryKind.FINDING.value),
            ).fetchall()
        else:
            if not isinstance(outcome, FindingOutcome):
                raise TypeError("MEMORY: outcome must be a FindingOutcome")
            rows = self._db.execute(
                "SELECT * FROM research_events WHERE issue_id = ? AND kind = ?"
                " AND outcome = ? ORDER BY seq ASC",
                (issue, MemoryKind.FINDING.value, outcome.value),
            ).fetchall()
        return tuple(_event(row) for row in rows)

    def verified_findings(self, issue_id: str) -> tuple[MemoryEvent, ...]:
        """Findings that actually verified a hypothesis (not a negative result)."""
        return self.findings(issue_id, outcome=FindingOutcome.VERIFIED)

    def outcomes(self, issue_id: str) -> tuple[MemoryEvent, ...]:
        """Terminal attempt dispositions for the issue, oldest first."""
        issue = _text(issue_id, "issue_id")
        rows = self._db.execute(
            "SELECT * FROM research_events WHERE issue_id = ? AND kind = ?"
            " ORDER BY seq ASC",
            (issue, MemoryKind.OUTCOME.value),
        ).fetchall()
        return tuple(_event(row) for row in rows)

    def latest_outcome(self, issue_id: str) -> MemoryEvent | None:
        """Most recent terminal disposition, or ``None`` when the issue is only attempted."""
        rows = self.outcomes(issue_id)
        return None if not rows else rows[-1]

    def efficiency(self) -> EfficiencyReport:
        """Findings, costs and outcomes per compute-hour across the whole memory.

        Read inside one transaction so a concurrent writer cannot make the counts and the
        cost sums disagree with each other.
        """
        with self._read():
            return self._report()

    def _report(self) -> EfficiencyReport:
        totals = self._db.execute(
            "SELECT COUNT(*) AS n,"
            " COUNT(DISTINCT issue_id) AS issues,"
            " COUNT(tokens) AS tokens_n, COALESCE(SUM(tokens), 0) AS tokens_sum,"
            " COUNT(wall_ms) AS wall_n, COALESCE(SUM(wall_ms), 0) AS wall_sum,"
            " COUNT(cpu_ms) AS cpu_n, COALESCE(SUM(cpu_ms), 0) AS cpu_sum,"
            " COUNT(peak_rss_kb) AS rss_n, COALESCE(MAX(peak_rss_kb), 0) AS rss_max,"
            " COUNT(pr_accepted) AS pr_n,"
            " COALESCE(SUM(CASE WHEN pr_accepted = 1 THEN 1 ELSE 0 END), 0) AS pr_ok"
            " FROM research_events"
        ).fetchone()
        assert totals is not None
        attempts_row = self._db.execute(
            "SELECT COALESCE(SUM(c), 0) AS attempts,"
            " COALESCE(SUM(c - 1), 0) AS retries FROM ("
            "  SELECT COUNT(DISTINCT attempt) AS c FROM research_events GROUP BY issue_id"
            ")"
        ).fetchone()
        assert attempts_row is not None
        by_outcome = {
            str(row["outcome"]): int(row["n"])
            for row in self._db.execute(
                "SELECT outcome, COUNT(*) AS n FROM research_events"
                " WHERE outcome IS NOT NULL GROUP BY outcome"
            ).fetchall()
        }
        verified_row = self._db.execute(
            "SELECT COUNT(*) AS n FROM research_events"
            " WHERE kind = ? AND outcome = ?",
            (MemoryKind.FINDING.value, FindingOutcome.VERIFIED.value),
        ).fetchone()
        assert verified_row is not None
        completion_placeholders = ", ".join("?" for _ in COMPLETION_OUTCOMES)
        completed_row = self._db.execute(
            "SELECT COUNT(DISTINCT issue_id) AS completed FROM research_events"
            f" WHERE kind = ? AND outcome IN ({completion_placeholders})",
            (
                MemoryKind.OUTCOME.value,
                *(outcome.value for outcome in sorted(COMPLETION_OUTCOMES)),
            ),
        ).fetchone()
        assert completed_row is not None
        findings_row = self._db.execute(
            "SELECT COUNT(*) AS n FROM research_events WHERE kind = ?",
            (MemoryKind.FINDING.value,),
        ).fetchone()
        assert findings_row is not None

        events = int(totals["n"])
        tokens = _aggregate(
            events,
            int(totals["tokens_n"]),
            int(totals["tokens_sum"]),
        )
        wall_hours = _hours(int(totals["wall_n"]), int(totals["wall_sum"]), events)
        cpu_hours = _hours(int(totals["cpu_n"]), int(totals["cpu_sum"]), events)
        rss = _aggregate(events, int(totals["rss_n"]), int(totals["rss_max"]))
        accepted_prs = _reported_accepted(int(totals["pr_n"]), int(totals["pr_ok"]))
        verified_findings = int(verified_row["n"])
        findings = int(findings_row["n"])
        if verified_findings > findings:  # pragma: no cover - the query makes this impossible
            raise ValueError(
                "MEMORY: verified findings exceed recorded findings; the aggregate is "
                "counting a non-FINDING row"
            )
        per_hour = not_measured()
        if isinstance(cpu_hours.value, float) and cpu_hours.value > 0:
            per_hour = measured(verified_findings / cpu_hours.value)
        return EfficiencyReport(
            events=events,
            issues=int(totals["issues"]),
            attempts=int(attempts_row["attempts"]),
            retries=int(attempts_row["retries"]),
            verified_findings=verified_findings,
            findings=findings,
            completed_issues=int(completed_row["completed"]),
            blocked=by_outcome.get(FindingOutcome.BLOCKED.value, 0),
            no_edge=by_outcome.get(FindingOutcome.NO_EDGE.value, 0),
            invalid=by_outcome.get(FindingOutcome.INVALID.value, 0),
            exhausted=by_outcome.get(FindingOutcome.EXHAUSTED.value, 0),
            pending_review=by_outcome.get(FindingOutcome.PENDING_REVIEW.value, 0),
            accepted_prs=accepted_prs,
            reported_prs=int(totals["pr_n"]),
            tokens=tokens,
            wall_hours=wall_hours,
            cpu_hours=cpu_hours,
            memory_high_water_kb=rss,
            findings_per_compute_hour=per_hour,
        )


def _aggregate(events: int, present: int, total: int) -> Measurement:
    """Sum ``total`` only when every one of ``events`` rows reported the field."""
    if events == 0 or present != events:
        return not_measured()
    return measured(total)


def _reported_accepted(present: int, accepted: int) -> Measurement:
    """PR acceptance is only defined for rows that report it; forward-only, unlike cost."""
    return not_measured() if present == 0 else measured(accepted)


def _hours(present: int, total_ms: int, events: int) -> Measurement:
    if events == 0 or present != events:
        return not_measured()
    return measured(total_ms / 3_600_000.0)
