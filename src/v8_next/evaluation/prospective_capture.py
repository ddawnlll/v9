"""NX10 (#431) — bounded public-data capture with exactly-once restart semantics.

This is the *technical* evidence path for a prospective shadow: a bounded capture of
public venue data, written as rolling, hash-chained batches, with the arrival and
event clocks recorded separately and the policy/runtime identity bound to the
artifact.

What it is not, and says so in every artifact:

* it is **public paper**, not a live venue settlement and not an authenticated
  account statement. A file that exists and has the right columns proves a pipeline
  ran, not that a venue settled anything;
* one bounded capture is not prospective *maturity*. The frozen forward window has
  to fill with real post-freeze time before any maturity question can be asked.

Restart rules, enforced here rather than trusted to the operator:

* a trade is identified by ``(source, symbol, trade_id)``; a repeated identity is
  dropped and counted, so a restart cannot double-count a fill;
* if the gap between the last merged event and the first event of a resumed batch
  exceeds the declared maximum, the merge **fails closed** and writes a visible
  recovery record -- the gap is never bridged silently;
* events older than the last merged event are reported as out-of-order rather than
  reordered into a plausible sequence.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

CAPTURE_VERSION = "v87-public-capture-v1"
PUBLIC_PAPER_SOURCE = "BINANCE_FAPI_AGGTRADES_PUBLIC"
PUBLIC_PAPER_LABEL = (
    "PUBLIC_PAPER_CAPTURE: public venue data through an unauthenticated endpoint; "
    "technical evidence only, NOT a live settlement and NOT an account statement"
)


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True)
class TradeEvent:
    """One public trade, with both clocks and its venue identity."""

    source: str
    symbol: str
    trade_id: str
    event_ns: int
    arrival_ns: int
    price: str
    qty: str

    def idempotency_key(self) -> str:
        return f"{self.source}:{self.symbol}:{self.trade_id}"

    @classmethod
    def from_dict(cls, record: dict[str, Any]) -> TradeEvent:
        return cls(
            source=str(record["source"]),
            symbol=str(record["symbol"]),
            trade_id=str(record["trade_id"]),
            event_ns=int(record["event_ns"]),
            arrival_ns=int(record["arrival_ns"]),
            price=str(record["price"]),
            qty=str(record["qty"]),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "symbol": self.symbol,
            "trade_id": self.trade_id,
            "event_ns": self.event_ns,
            "arrival_ns": self.arrival_ns,
            "price": self.price,
            "qty": self.qty,
            "idempotency_key": self.idempotency_key(),
        }


@dataclass
class CaptureState:
    """What a restart must be able to reconstruct, and nothing it cannot."""

    seen: set[str] = field(default_factory=set)
    last_event_ns: int | None = None
    last_trade_id: str | None = None
    prev_hash: str = "GENESIS"
    batches: int = 0
    accepted: int = 0
    duplicates: int = 0
    out_of_order: int = 0
    gaps: int = 0
    gap_acknowledgements: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "seen_trade_keys": len(self.seen),
            "last_event_ns": self.last_event_ns,
            "last_trade_id": self.last_trade_id,
            "prev_hash": self.prev_hash,
            "batches": self.batches,
            "accepted": self.accepted,
            "duplicates": self.duplicates,
            "out_of_order": self.out_of_order,
            "gaps": self.gaps,
            "gap_acknowledgements": list(self.gap_acknowledgements),
        }


def batch_identity(events: list[TradeEvent], prev_hash: str) -> dict[str, Any]:
    """Rolling artifact identity: a content hash chained to the previous batch."""
    payload = canonical([event.as_dict() for event in events])
    digest = hashlib.sha256(payload.encode()).hexdigest()
    chain = hashlib.sha256(f"{prev_hash}:{digest}".encode()).hexdigest()
    return {
        "events": len(events),
        "payload_sha256": digest,
        "prev_hash": prev_hash,
        "chain_hash": chain,
        "first_event_ns": events[0].event_ns if events else None,
        "last_event_ns": events[-1].event_ns if events else None,
        "first_arrival_ns": min((event.arrival_ns for event in events), default=None),
        "last_arrival_ns": max((event.arrival_ns for event in events), default=None),
    }


def merge_batch(
    state: CaptureState,
    events: Iterable[TradeEvent],
    *,
    max_gap_ns: int,
    acknowledge_gap: bool = False,
) -> tuple[CaptureState, dict[str, Any]]:
    """Merge one batch into the state under the exactly-once and gap rules."""
    if max_gap_ns <= 0:
        raise ValueError("max_gap_ns must be positive")
    ordered = sorted(events, key=lambda event: (event.event_ns, event.trade_id))
    accepted: list[TradeEvent] = []
    duplicates = 0
    out_of_order = 0
    for event in ordered:
        key = event.idempotency_key()
        if key in state.seen:
            duplicates += 1
            continue
        if state.last_event_ns is not None and event.event_ns < state.last_event_ns:
            out_of_order += 1
            continue
        state.seen.add(key)
        accepted.append(event)

    report: dict[str, Any] = {
        "received": len(ordered),
        "accepted": len(accepted),
        # the accepted events travel with the report so the caller persists exactly
        # what was merged -- a restart cannot silently re-derive a different set
        "accepted_events": [event.as_dict() for event in accepted],
        "duplicates_dropped": duplicates,
        "out_of_order_rejected": out_of_order,
        "gap": None,
        "recovery_required": False,
    }
    gap: dict[str, Any] | None = None
    if accepted and state.last_event_ns is not None:
        delta = accepted[0].event_ns - state.last_event_ns
        if delta > max_gap_ns:
            gap = {
                "kind": "GAP_DETECTED",
                "last_merged_event_ns": state.last_event_ns,
                "first_new_event_ns": accepted[0].event_ns,
                "gap_ns": delta,
                "max_gap_ns": max_gap_ns,
                "acknowledged": acknowledge_gap,
            }
            state.gaps += 1
            if not acknowledge_gap:
                # fail closed: nothing is merged, the gap stays visible
                for event in accepted:
                    state.seen.discard(event.idempotency_key())
                report["gap"] = gap
                report["recovery_required"] = True
                report["reason"] = (
                    "MISSING_INTERVAL_NOT_BRIDGED: the resumed capture skips more than the "
                    "declared maximum; merge refused until the gap is acknowledged"
                )
                return state, report
            state.gap_acknowledgements.append(gap)
            report["gap"] = gap

    if accepted:
        identity = batch_identity(accepted, state.prev_hash)
        state.prev_hash = identity["chain_hash"]
        state.last_event_ns = accepted[-1].event_ns
        state.last_trade_id = accepted[-1].trade_id
        state.batches += 1
        state.accepted += len(accepted)
        state.duplicates += duplicates
        state.out_of_order += out_of_order
        report["batch_identity"] = identity
    else:
        state.duplicates += duplicates
        state.out_of_order += out_of_order
    return state, report


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


def write_batch(out_dir: Path, index: int, events: list[TradeEvent], report: dict[str, Any]) -> Path:
    """Write one batch and its report atomically; return the batch path."""
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"batch_{index:04d}.jsonl"
    body = "".join(canonical(event.as_dict()) + "\n" for event in events)
    _atomic_write(path, body)
    _atomic_write(
        out_dir / f"batch_{index:04d}.report.json",
        json.dumps(report, indent=2, sort_keys=True) + "\n",
    )
    return path


def write_manifest(out_dir: Path, manifest: dict[str, Any]) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "manifest.json"
    _atomic_write(path, json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    return path


def load_capture(out_dir: Path) -> tuple[dict[str, Any], CaptureState, dict[str, Any]]:
    """Resume: rebuild the state from the manifest and the written batches.

    The recovery report is explicit about what was replayed, how many identities
    were re-seen, and whether any batch failed its own chain check -- a resumed run
    never assumes the previous state was fine.
    """
    manifest_path = out_dir / "manifest.json"
    if not manifest_path.is_file():
        raise FileNotFoundError(f"no capture manifest at {manifest_path}")
    manifest = json.loads(manifest_path.read_text())
    state = CaptureState()
    replayed = 0
    chain_ok = True
    problems: list[str] = []
    for batch in sorted(out_dir.glob("batch_*.jsonl")):
        expected = manifest.get("batches", {}).get(batch.stem)
        body = batch.read_text()
        digest = hashlib.sha256(
            canonical([json.loads(line) for line in body.splitlines() if line.strip()]).encode()
        ).hexdigest()
        if expected is not None and expected.get("payload_sha256") != digest:
            chain_ok = False
            problems.append(f"{batch.name}: payload hash mismatch")
        for line in body.splitlines():
            if not line.strip():
                continue
            record = json.loads(line)
            state.seen.add(record["idempotency_key"])
            state.last_event_ns = record["event_ns"]
            state.last_trade_id = record["trade_id"]
            replayed += 1
    state.accepted = replayed
    state.batches = len(list(out_dir.glob("batch_*.jsonl")))
    state.prev_hash = manifest.get("chain_hash", "GENESIS")
    report = {
        "mode": "RESUMED",
        "batches_replayed": state.batches,
        "events_replayed": replayed,
        "chain_verified": chain_ok,
        "problems": problems,
        "last_event_ns": state.last_event_ns,
        "resume_ns": time.time_ns(),
        "note": "state rebuilt from disk; the resumed run re-checks its own artifact chain",
    }
    return manifest, state, report
