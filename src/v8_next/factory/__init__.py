"""Factory coordination plane: durable issue queue, leases and evidence receipts (F-01).

The coordinator runs unattended engineering, so its state must survive its own crashes:
which issue is claimed, by whom, until when, how much retry budget is left, and what the
worker reported. This package owns that bookkeeping only — it never calls GitHub, never
runs a test, and cannot mark an issue reviewed.
"""

from __future__ import annotations

from v8_next.factory.lease import (
    DEFAULT_MAX_ATTEMPTS,
    EVIDENCE_FIELDS,
    MEASURED,
    NOT_MEASURED,
    CompletionReceipt,
    IssueRecord,
    IssueState,
    Lease,
    LeaseEvent,
    LeaseOutcome,
    OwnerReview,
    evidence_receipt,
)
from v8_next.factory.queue import (
    IssueQueue,
    LeaseMismatchError,
    QueueError,
    UnknownIssueError,
)

__all__ = [
    "DEFAULT_MAX_ATTEMPTS",
    "EVIDENCE_FIELDS",
    "MEASURED",
    "NOT_MEASURED",
    "CompletionReceipt",
    "IssueQueue",
    "IssueRecord",
    "IssueState",
    "Lease",
    "LeaseEvent",
    "LeaseMismatchError",
    "LeaseOutcome",
    "OwnerReview",
    "QueueError",
    "UnknownIssueError",
    "evidence_receipt",
]
