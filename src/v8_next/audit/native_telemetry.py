"""Native NautilusTrader execution telemetry extraction and reconciliation.

The installed engine (2.0.0rc4) exposes ``generate_fills_report`` (one row per
fill), ``generate_order_fills_report`` (one row per order), ``generate_orders_report``
and ``generate_positions_report``. They return pandas DataFrames, so the report
path requires pandas; that ImportError is reported verbatim instead of being
swallowed into an empty success.
"""

from __future__ import annotations

import math
from typing import Any


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return None if math.isnan(value) or math.isinf(value) else value
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    try:
        if value != value:  # NaN from pandas
            return None
    except Exception:  # pragma: no cover - defensive
        pass
    return str(value)


def _records(report: Any) -> list[dict[str, Any]]:
    if report is None:
        return []
    if hasattr(report, "to_dicts"):
        return [dict(row) for row in report.to_dicts()]
    if hasattr(report, "to_dict"):
        try:
            return [dict(row) for row in report.to_dict("records")]
        except TypeError:  # pragma: no cover - non-tabular mapping
            return [dict(report.to_dict())]
    if isinstance(report, (list, tuple)):
        return [dict(row) for row in report if isinstance(row, dict)]
    return []


def _probe(engine: Any, method_name: str) -> tuple[list[dict[str, Any]], str]:
    """Call one native report method and classify the outcome."""
    method = getattr(engine, method_name, None)
    if method is None:
        return [], f"API_ABSENT:{method_name}"
    try:
        report = method()
    except ImportError as exc:  # the observed pandas dependency failure
        return [], f"IMPORT_ERROR:{exc}"
    except Exception as exc:  # noqa: BLE001 - reported, never hidden
        return [], f"UNAVAILABLE:{type(exc).__name__}:{exc}"
    if report is None:
        return [], "EMPTY"
    rows = _records(report)
    if not rows:
        return [], "EMPTY"
    return [{str(k): _json_safe(v) for k, v in row.items()} for row in rows], type(report).__name__


#: Report method -> logical family. Order matters: fills first (individual fill
#: rows), then order-level rows, then orders and positions.
REPORT_METHODS: tuple[tuple[str, str], ...] = (
    ("generate_fills_report", "fills"),
    ("generate_order_fills_report", "order_fills"),
    ("generate_orders_report", "orders"),
    ("generate_positions_report", "positions"),
    ("generate_account_report", "account"),
)


def extract_native_reports(engine: Any) -> dict[str, Any]:
    """Extract every available native report, preserving availability status."""
    records: dict[str, list[dict[str, Any]]] = {}
    statuses: dict[str, str] = {}
    for method_name, family in REPORT_METHODS:
        rows, status = _probe(engine, method_name)
        records[family] = rows
        statuses[family] = status
    return {"records": records, "status": statuses}


def _fill_identity(row: dict[str, Any]) -> str:
    for key in ("trade_id", "venue_order_id", "client_order_id", "order_id"):
        value = row.get(key)
        if value:
            return f"{key}:{value}"
    return ""


def reconcile_fills(
    native_fills: list[dict[str, Any]],
    audit_fills: list[dict[str, Any]],
) -> dict[str, Any]:
    """Count native vs audited fills and publish every mismatch class.

    Identity prefers trade_id, then venue_order_id, then client_order_id. An
    unmatched record stays unmatched: nothing is de-duplicated away silently.
    """
    native_ids = [ident for ident in (_fill_identity(r) for r in native_fills) if ident]
    audit_ids = [ident for ident in (_fill_identity(r) for r in audit_fills) if ident]
    native_counts: dict[str, int] = {}
    for ident in native_ids:
        native_counts[ident] = native_counts.get(ident, 0) + 1
    audit_counts: dict[str, int] = {}
    for ident in audit_ids:
        audit_counts[ident] = audit_counts.get(ident, 0) + 1
    duplicates = sorted(k for k, n in native_counts.items() if n > 1)
    unmatched_native = sorted(set(native_ids) - set(audit_ids))
    unmatched_audit = sorted(set(audit_ids) - set(native_ids))
    return {
        "native_fill_count": len(native_fills),
        "audited_fill_count": len(audit_fills),
        "native_identified": len(native_ids),
        "audit_identified": len(audit_ids),
        "unmatched_native_fills": len(unmatched_native),
        "unmatched_audit_fills": len(unmatched_audit),
        "unmatched_native_examples": unmatched_native[:10],
        "unmatched_audit_examples": unmatched_audit[:10],
        "duplicate_fill_ids": len(duplicates),
        "duplicate_fill_id_examples": duplicates[:10],
        "identity_rule": "trade_id > venue_order_id > client_order_id > order_id",
    }
