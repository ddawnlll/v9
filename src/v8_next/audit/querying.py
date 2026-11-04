"""Disk-only audit queries. No engine, no re-computation, no database."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterator

from v8_next.audit.forensic import AuditStore


def _store(run_dir: Path | str) -> AuditStore:
    return AuditStore(Path(run_dir))


def _events(run_dir: Path | str) -> Iterator[dict[str, Any]]:
    return _store(run_dir).iter_events()


def campaign_timeline(
    run_dir: Path | str,
    *,
    position_id: str | None = None,
    opportunity_id: str | None = None,
    trade_id: str | None = None,
) -> list[dict[str, Any]]:
    """All events that mention one campaign/position/opportunity/trade identity."""
    keys = {"position_id": position_id, "opportunity_id": opportunity_id, "trade_id": trade_id}
    wanted = {k: v for k, v in keys.items() if v}
    rows = [
        event
        for event in _events(run_dir)
        if all(event.get(key) == value for key, value in wanted.items())
    ]
    return sorted(rows, key=lambda e: (e["event_time_ns"], e["sequence_number"]))


def expert_decisions(run_dir: Path | str, expert_id: str) -> list[dict[str, Any]]:
    return [
        event
        for event in _events(run_dir)
        if event.get("event_type") == "EXPERT_EVALUATED" and event.get("expert_id") == expert_id
    ]


def opportunities_rejected(run_dir: Path | str, reason: str) -> list[dict[str, Any]]:
    return [
        event
        for event in _events(run_dir)
        if event.get("event_type") == "ADMISSION_REJECTED" and event.get("reason_code") == reason
    ]


def trades_by_exit_kind(run_dir: Path | str, exit_kind: str) -> list[dict[str, Any]]:
    return [
        row
        for row in _store(run_dir).iter_jsonl("trade_lifecycles.jsonl")
        if row.get("exit_kind") == exit_kind
    ]


def trades_with_mfe_above_realized(run_dir: Path | str) -> list[dict[str, Any]]:
    rows = []
    for row in _store(run_dir).iter_jsonl("trade_lifecycles.jsonl"):
        mfe = row.get("mfe_price")
        entry, exit_price = row.get("entry_price"), row.get("exit_price")
        direction = row.get("direction")
        if mfe is None or entry is None or exit_price is None:
            continue
        sign = 1 if direction == "LONG" else -1
        if mfe > (exit_price - entry) * sign:
            rows.append(row)
    return rows


def orphan_records(run_dir: Path | str) -> dict[str, Any]:
    """Every reconciliation class that carries an unmatched/orphan count."""
    store = _store(run_dir)
    recon = store.read_json("reconciliation.json") if store.exists("reconciliation.json") else {}
    keys = (
        "unmatched_native_fills",
        "unmatched_audit_fills",
        "unlinked_orders",
        "unlinked_positions",
        "duplicate_fill_ids",
        "unexplained_cashflows",
        "orphan_order_events",
        "orphan_position_events",
    )
    return {"counts": {key: recon.get(key) for key in keys}, "unmatched_examples": {
        "native": recon.get("unmatched_native_examples", []),
        "audit": recon.get("unmatched_audit_examples", []),
    }}


def filter_by(expert_id: str | None = None, instrument_id: str | None = None, **_: Any) -> dict[str, str]:
    """Compose the stable identity filters accepted by :func:`query`."""
    filters: dict[str, str] = {}
    if expert_id:
        filters["expert_id"] = expert_id
    if instrument_id:
        filters["instrument_id"] = instrument_id
    return filters


def query(run_dir: Path | str, **filters: Any) -> list[dict[str, Any]]:
    """Generic identity filter over the event ledger (all keys ANDed)."""
    active = {key: value for key, value in filters.items() if value is not None}
    return [
        event
        for event in _events(run_dir)
        if all(str(event.get(key)) == str(value) for key, value in active.items())
    ]


__all__ = [
    "campaign_timeline",
    "expert_decisions",
    "opportunities_rejected",
    "orphan_records",
    "query",
    "trades_by_exit_kind",
    "trades_with_mfe_above_realized",
]
