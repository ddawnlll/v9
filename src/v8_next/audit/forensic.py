"""Native forensic audit: versioned event schema, append-only recorder and store.

This module is the single evidence spine of a v8-next audit run. The strategy
adapter writes domain events at their real causal boundaries; everything else
(family JSONL files, metrics, reports, queries) is derived from the same stream.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections import Counter
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Iterator, Mapping

SCHEMA_VERSION = "AUDIT_EVENT_V1"


class EvidenceClass(StrEnum):
    """What kind of claim an event is allowed to support."""

    OBSERVED_TAPE = "OBSERVED_TAPE"
    OBSERVED_SIMULATION = "OBSERVED_SIMULATION"
    DERIVED = "DERIVED"
    MODEL_ASSUMPTION = "MODEL_ASSUMPTION"


#: The complete expert-evaluation vocabulary. Only these six states may be
#: written to EXPERT_EVALUATED events; unknown witness kinds degrade to
#: NOT_EVALUATED with the raw value preserved in `raw_stance`.
EVALUATION_STATES: frozenset[str] = frozenset(
    {
        "NOT_EVALUATED",
        "NO_OPPORTUNITY",
        "ABSTAIN",
        "CONTRADICT",
        "SUPPORT",
        "EVALUATION_ERROR",
    }
)


def evaluation_state(raw: Any) -> str:
    """Normalise a witness stance kind into the audit vocabulary."""
    if raw is None:
        return "NOT_EVALUATED"
    value = str(getattr(raw, "value", raw)).strip().upper()
    return value if value in EVALUATION_STATES else "NOT_EVALUATED"


EVENT_TYPES: frozenset[str] = frozenset(
    {
        "MARKET_OBSERVED",
        "MARKET_CONTEXT_BUILT",
        "OPPORTUNITY_DETECTED",
        "OPPORTUNITY_UPDATED",
        "OPPORTUNITY_EXPIRED",
        "OPPORTUNITY_INVALIDATED",
        "EXPERT_EVALUATED",
        "CONSENSUS_EVALUATED",
        "ADMISSION_ACCEPTED",
        "ADMISSION_REJECTED",
        "RISK_CHECK_PASSED",
        "RISK_CHECK_FAILED",
        "ORDER_CREATED",
        "ORDER_SUBMITTED",
        "ORDER_ACCEPTED",
        "ORDER_REJECTED",
        "ORDER_CANCELED",
        "ORDER_UPDATED",
        "ORDER_PARTIALLY_FILLED",
        "ORDER_FILLED",
        "POSITION_OPENED",
        "POSITION_CHANGED",
        "POSITION_CLOSED",
        "PROTECTIVE_STOP_CREATED",
        "PROTECTIVE_STOP_UPDATED",
        "PROTECTIVE_STOP_CANCELED",
        "TARGET_CREATED",
        "TARGET_UPDATED",
        "TARGET_CANCELED",
        "CASHFLOW_RECORDED",
        "REPLAY_FINALIZED",
    }
)

#: Mandatory metadata every event carries. Optional entity keys are declared
#: here so a reader can tell "absent" from "empty" without guessing.
MANDATORY_FIELDS: tuple[str, ...] = (
    "schema_version",
    "run_id",
    "sequence_number",
    "event_type",
    "instrument_id",
    "event_time_ns",
    "recorded_time_ns",
    "source_component",
    "evidence_class",
)
OPTIONAL_ENTITY_FIELDS: tuple[str, ...] = (
    "opportunity_id",
    "candidate_id",
    "expert_id",
    "decision_id",
    "campaign_id",
    "client_order_id",
    "venue_order_id",
    "position_id",
    "trade_id",
    "parent_event_id",
)

#: Native order-event class name -> canonical audit event type.
ORDER_EVENT_TYPES: Mapping[str, str] = {
    "OrderSubmitted": "ORDER_SUBMITTED",
    "OrderAccepted": "ORDER_ACCEPTED",
    "OrderRejected": "ORDER_REJECTED",
    "OrderCanceled": "ORDER_CANCELED",
    "OrderExpired": "ORDER_CANCELED",
    "OrderUpdated": "ORDER_UPDATED",
    "OrderPendingUpdate": "ORDER_UPDATED",
    "OrderPartiallyFilled": "ORDER_PARTIALLY_FILLED",
    "OrderFilled": "ORDER_FILLED",
}


class AuditSchemaError(ValueError):
    """Raised when an event cannot be admitted into the ledger."""


def _json_default(value: Any) -> Any:
    if isinstance(value, tuple):
        return list(value)
    if hasattr(value, "as_decimal"):
        return str(value.as_decimal())
    if isinstance(value, (set, frozenset)):
        return sorted(value)
    return str(value)


def make_event(
    *,
    run_id: str,
    sequence_number: int,
    event_type: str,
    instrument_id: str,
    event_time_ns: int,
    recorded_time_ns: int,
    source_component: str,
    evidence_class: EvidenceClass,
    fields: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build one validated audit event dict."""
    if event_type not in EVENT_TYPES:
        raise AuditSchemaError(f"unregistered event_type: {event_type}")
    if not isinstance(event_time_ns, int) or event_time_ns < 0:
        raise AuditSchemaError(f"event_time_ns must be a non-negative int, got {event_time_ns!r}")
    event: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "sequence_number": sequence_number,
        "event_type": event_type,
        "instrument_id": instrument_id,
        "event_time_ns": event_time_ns,
        "recorded_time_ns": recorded_time_ns,
        "source_component": source_component,
        "evidence_class": str(evidence_class),
    }
    for key, value in (fields or {}).items():
        if key in event:
            raise AuditSchemaError(f"field {key!r} collides with mandatory metadata")
        if key in {"fields", "event"}:
            raise AuditSchemaError(f"illegal field name {key!r}")
        event[key] = value
    return event


@dataclass
class AuditRecorder:
    """Append-only event recorder with monotonic sequence numbering.

    The recorder never owns strategy state: it observes and appends. Trading
    behaviour is unaffected whether or not a recorder is attached.
    """

    run_id: str
    events_path: Path
    instrument_id: str = ""
    _sequence: int = 0
    _counts: Counter[str] = field(default_factory=Counter)
    _handle: Any = None

    def __post_init__(self) -> None:
        self.events_path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = self.events_path.open("a", encoding="utf-8", newline="\n")

    # -- lifecycle -------------------------------------------------------
    def flush(self) -> None:
        if self._handle is not None:
            self._handle.flush()

    def close(self) -> None:
        if self._handle is not None:
            self._handle.flush()
            self._handle.close()
            self._handle = None

    def __enter__(self) -> AuditRecorder:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- recording -------------------------------------------------------
    def record(
        self,
        event_type: str,
        *,
        event_time_ns: int,
        source_component: str,
        evidence_class: EvidenceClass,
        instrument_id: str | None = None,
        **fields: Any,
    ) -> dict[str, Any]:
        self._sequence += 1
        event = make_event(
            run_id=self.run_id,
            sequence_number=self._sequence,
            event_type=event_type,
            instrument_id=str(instrument_id or self.instrument_id),
            event_time_ns=int(event_time_ns),
            recorded_time_ns=time.time_ns(),
            source_component=source_component,
            evidence_class=evidence_class,
            fields=fields,
        )
        if self._handle is None:
            raise AuditSchemaError("recorder is closed")
        self._handle.write(
            json.dumps(event, sort_keys=True, separators=(",", ":"), default=_json_default)
            + "\n"
        )
        self._counts[event_type] += 1
        return event

    @property
    def counts(self) -> dict[str, int]:
        return dict(self._counts)

    @property
    def sequence(self) -> int:
        return self._sequence


class AuditStore:
    """Read-only access to a persisted audit directory."""

    def __init__(self, run_dir: Path) -> None:
        self.run_dir = Path(run_dir)

    def path(self, name: str) -> Path:
        return self.run_dir / name

    def exists(self, name: str) -> bool:
        return self.path(name).is_file()

    def read_json(self, name: str) -> Any:
        return json.loads(self.path(name).read_text(encoding="utf-8"))

    def iter_jsonl(self, name: str) -> Iterator[dict[str, Any]]:
        target = self.path(name)
        if not target.is_file():
            return
        with target.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if line:
                    yield json.loads(line)

    def iter_events(self) -> Iterator[dict[str, Any]]:
        return self.iter_jsonl("events.jsonl")

    def sha256(self, name: str) -> str:
        digest = hashlib.sha256()
        with self.path(name).open("rb") as handle:
            for chunk in iter(lambda: handle.read(1 << 20), b""):
                digest.update(chunk)
        return digest.hexdigest()

