"""Lease, status and owner-review contracts for the durable issue queue (F-01).

F-01 is the coordinator's bookkeeping plane, not an execution authority. It records which
issue a worker owns, for how long, how many attempts that issue has spent, and what the
worker reported. It never calls GitHub, never runs a test, and never decides that an issue
is done: :class:`OwnerReview` is a constant in every result contract, and a completion
receipt cannot be constructed with the review waived.

Storage choice is in :mod:`v8_next.factory.queue`; the rationale is recorded in
``reports/overnight/evidence/F-01.md``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Mapping, Sequence, cast

from v8_next.evaluation.store import canonical

__all__ = [
    "DEFAULT_MAX_ATTEMPTS",
    "EVIDENCE_FIELDS",
    "MEASURED",
    "NOT_MEASURED",
    "CompletionReceipt",
    "IssueRecord",
    "IssueState",
    "Lease",
    "LeaseEvent",
    "LeaseOutcome",
    "OwnerReview",
    "evidence_receipt",
]

#: Retry budget used when ``enqueue``'s metadata does not override it.
DEFAULT_MAX_ATTEMPTS = 3

#: Explicit marker for evidence fields the caller could not measure. A missing number is
#: never replaced by a plausible one.
NOT_MEASURED = "NOT_MEASURED"
MEASURED = "MEASURED"

#: Evidence fields that must be present (possibly as ``None`` + ``NOT_MEASURED``).
EVIDENCE_FIELDS: tuple[str, ...] = (
    "code_sha",
    "config_sha",
    "executed_tests",
    "walltime_ms",
    "peak_rss_kb",
)


class IssueState(StrEnum):
    """Durable lifecycle state of one queued issue."""

    PENDING = "PENDING"
    LEASED = "LEASED"
    COMPLETED = "COMPLETED"
    #: Retry budget spent; never claimed automatically again.
    EXHAUSTED = "EXHAUSTED"
    #: Owner/operator escalation; never claimed automatically again.
    BLOCKED = "BLOCKED"

    @property
    def terminal(self) -> bool:
        """True when no automatic claim may ever touch the issue again."""
        return self in (IssueState.COMPLETED, IssueState.EXHAUSTED, IssueState.BLOCKED)


class LeaseOutcome(StrEnum):
    """Terminal disposition of one lease attempt (one row in the audit trail)."""

    ACTIVE = "ACTIVE"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    EXPIRED = "EXPIRED"
    BLOCKED = "BLOCKED"


class OwnerReview(StrEnum):
    """Owner review is never delegated to the automated coordinator."""

    REQUIRED = "REQUIRED"


@dataclass(frozen=True, slots=True)
class Lease:
    """Exclusive ownership of one issue by one worker until ``expires_ns``.

    ``token`` is the unguessable identity of this attempt: completion and failure are
    accepted only from the token holder, so a worker resurrected after a pause cannot
    overwrite the result of the worker that replaced it.
    """

    issue_id: str
    worker_id: str
    token: str
    acquired_ns: int
    expires_ns: int
    attempt: int

    def __post_init__(self) -> None:
        for name in ("issue_id", "worker_id", "token"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"LEASE: {name} must be a non-empty string")
        for name in ("acquired_ns", "expires_ns", "attempt"):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool):
                raise ValueError(f"LEASE: {name} must be an int")
        if self.acquired_ns < 0:
            raise ValueError("LEASE: acquired_ns must be non-negative")
        if self.expires_ns <= self.acquired_ns:
            raise ValueError("LEASE: expires_ns must be after acquired_ns")
        if self.attempt < 1:
            raise ValueError("LEASE: attempt must be >= 1")

    def is_expired(self, now_ns: int) -> bool:
        """True when ``now_ns`` is at or past the lease deadline (inclusive)."""
        return now_ns >= self.expires_ns


@dataclass(frozen=True, slots=True)
class LeaseEvent:
    """One attempt as recorded in the durable audit trail."""

    issue_id: str
    worker_id: str
    token: str
    attempt: int
    acquired_ns: int
    expires_ns: int
    released_ns: int | None
    outcome: LeaseOutcome
    note: str | None


@dataclass(frozen=True, slots=True)
class IssueRecord:
    """Point-in-time snapshot of one queued issue (status + budget + last result)."""

    issue_id: str
    state: IssueState
    meta: Mapping[str, Any]
    priority: int
    attempts: int
    max_attempts: int
    enqueued_ns: int
    updated_ns: int
    worker_id: str | None
    token: str | None
    acquired_ns: int | None
    expires_ns: int | None
    last_error: str | None
    result: Mapping[str, Any] | None
    completed_ns: int | None

    @property
    def budget_remaining(self) -> int:
        """Attempts left before the issue is exhausted (never negative)."""
        return max(self.max_attempts - self.attempts, 0)

    @property
    def lease(self) -> Lease | None:
        """The live lease, or ``None`` when the issue is not currently leased."""
        if (
            self.state is not IssueState.LEASED
            or self.worker_id is None
            or self.token is None
            or self.acquired_ns is None
            or self.expires_ns is None
        ):
            return None
        return Lease(
            issue_id=self.issue_id,
            worker_id=self.worker_id,
            token=self.token,
            acquired_ns=self.acquired_ns,
            expires_ns=self.expires_ns,
            attempt=self.attempts,
        )


@dataclass(frozen=True, slots=True)
class CompletionReceipt:
    """Immutable result contract returned by ``IssueQueue.complete``.

    ``owner_review_required`` and ``owner_review`` are constants: constructing a receipt
    that waives owner review raises, so no worker can hand back an "approved" result.
    """

    issue_id: str
    worker_id: str
    token: str
    attempt: int
    attempts_used: int
    max_attempts: int
    completed_ns: int
    result: Mapping[str, Any]
    owner_review_required: bool = True
    owner_review: OwnerReview = OwnerReview.REQUIRED
    idempotent_replay: bool = False

    def __post_init__(self) -> None:
        if self.owner_review_required is not True:
            raise ValueError("OWNER_REVIEW: review cannot be waived by a worker result")
        if self.owner_review is not OwnerReview.REQUIRED:
            raise ValueError("OWNER_REVIEW: status must be REQUIRED")
        if not isinstance(self.result, Mapping):
            raise ValueError("OWNER_REVIEW: result must be a mapping")

    @property
    def budget_remaining(self) -> int:
        """Attempts left on the issue at completion time (never negative)."""
        return max(self.max_attempts - self.attempts_used, 0)


def _optional_text(value: object, name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"EVIDENCE: {name} must be a non-empty string when supplied")
    return value.strip()


def _optional_count(value: object, name: str) -> int | None:
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"EVIDENCE: {name} must be an int when supplied")
    if value < 0:
        raise ValueError(f"EVIDENCE: {name} must be non-negative")
    return value


def _executed_tests(values: Sequence[str] | None) -> list[str] | None:
    if values is None:
        return None
    if isinstance(values, str):
        raise ValueError("EVIDENCE: executed_tests must be a sequence of test ids")
    normalized: list[str] = []
    for item in values:
        if not isinstance(item, str) or not item.strip():
            raise ValueError("EVIDENCE: executed_tests entries must be non-empty strings")
        if item not in normalized:
            normalized.append(item)
    if not normalized:
        return None
    return normalized


def evidence_receipt(
    *,
    code_sha: str | None = None,
    config_sha: str | None = None,
    executed_tests: Sequence[str] | None = None,
    walltime_ms: int | None = None,
    peak_rss_kb: int | None = None,
    budget_spent: int | None = None,
    budget_limit: int | None = None,
) -> dict[str, Any]:
    """Build an evidence receipt from caller-supplied measurements only.

    Nothing here is probed, timed, or inferred: a value the caller did not measure stays
    ``None`` and its ``measurement`` status is ``NOT_MEASURED``. ``budget_limit``, when
    supplied, must be positive; an over-spend is reported as a negative ``remaining``
    rather than hidden.
    """
    tests = _executed_tests(executed_tests)
    measured = {
        "code_sha": _optional_text(code_sha, "code_sha"),
        "config_sha": _optional_text(config_sha, "config_sha"),
        "executed_tests": tests,
        "walltime_ms": _optional_count(walltime_ms, "walltime_ms"),
        "peak_rss_kb": _optional_count(peak_rss_kb, "peak_rss_kb"),
    }
    spent = _optional_count(budget_spent, "budget_spent")
    limit = _optional_count(budget_limit, "budget_limit")
    if limit is not None and limit < 1:
        raise ValueError("EVIDENCE: budget_limit must be positive when supplied")
    remaining = None if spent is None or limit is None else limit - spent

    receipt: dict[str, Any] = dict(measured)
    receipt["budget"] = {"spent": spent, "limit": limit, "remaining": remaining}
    receipt["owner_review_required"] = True
    receipt["owner_review"] = OwnerReview.REQUIRED.value
    measurement = {
        field: (NOT_MEASURED if value is None else MEASURED)
        for field, value in measured.items()
    }
    measurement["budget"] = (
        NOT_MEASURED if spent is None or limit is None else MEASURED
    )
    receipt["measurement"] = measurement
    return receipt


def decode_mapping(text: str) -> Mapping[str, Any]:
    """Decode a canonical-JSON object into an immutable mapping."""
    decoded = json.loads(text)
    if not isinstance(decoded, dict):
        raise ValueError("QUEUE: stored payload is not a JSON object")
    return MappingProxyType(cast(dict[str, Any], decoded))


def immutable_mapping(value: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    """Return a read-only copy of ``value`` after canonical-JSON validation."""
    canonical(value)  # rejects non-finite floats / non-serialisable payloads
    return MappingProxyType(dict(value))
