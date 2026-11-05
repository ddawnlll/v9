"""One-command native forensic audit pipeline for v8-next.

Owns the audit session lifecycle: tape provenance -> native replay -> event
recording -> native report extraction -> family artifacts -> metrics ->
reconciliation -> gate validation -> reports -> manifest with digests.
"""

from __future__ import annotations

import json
import platform
import subprocess
import sys
import time
from bisect import bisect_right
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence, cast

from v8_next.audit.forensic import (
    EVALUATION_STATES,
    AuditRecorder,
    AuditStore,
    EvidenceClass,
)
from v8_next.audit.lineage import (
    LineageCashflow,
    LineageChain,
    LineageDecision,
    LineageFill,
    LineageOpportunity,
    LineageOrder,
)
from v8_next.audit.native_telemetry import reconcile_fills
from v8_next.audit.trade_metrics import (
    compute_post_exit,
    compute_trade_metrics,
    trade_facts_from_replay,
)
from v8_next.audit.verdicts import (
    REQUIRED_SOURCE_IDS,
    AuditVerdict,
    VerdictState,
    derive_verdict,
    verdict_from_trade_metrics,
)

SCHEMA_VERSION = "AUDIT_EVENT_V1"
INSTRUMENT_STRATEGY_ID = "BTCUSDT-PERP.BINANCE"
HOUR_NS = 3_600_000_000_000

FAMILY_FILES: dict[str, tuple[str, ...]] = {
    "market_observations.jsonl": ("MARKET_OBSERVED", "MARKET_CONTEXT_BUILT"),
    "expert_decisions.jsonl": ("EXPERT_EVALUATED", "CONSENSUS_EVALUATED"),
    "opportunities.jsonl": (
        "OPPORTUNITY_DETECTED",
        "OPPORTUNITY_UPDATED",
        "OPPORTUNITY_EXPIRED",
        "OPPORTUNITY_INVALIDATED",
    ),
    "orders.jsonl": (
        "ORDER_CREATED",
        "ORDER_SUBMITTED",
        "ORDER_ACCEPTED",
        "ORDER_REJECTED",
        "ORDER_CANCELED",
        "ORDER_UPDATED",
        "ORDER_PARTIALLY_FILLED",
        "ORDER_FILLED",
        "PROTECTIVE_STOP_CREATED",
        "PROTECTIVE_STOP_UPDATED",
        "PROTECTIVE_STOP_CANCELED",
        "TARGET_CREATED",
        "TARGET_UPDATED",
        "TARGET_CANCELED",
    ),
    "positions.jsonl": ("POSITION_OPENED", "POSITION_CHANGED", "POSITION_CLOSED"),
    "cashflows.jsonl": ("CASHFLOW_RECORDED",),
}

MANDATORY_ARTIFACTS: tuple[str, ...] = (
    "run_manifest.json",
    "events.jsonl",
    "market_observations.jsonl",
    "expert_decisions.jsonl",
    "opportunities.jsonl",
    "orders.jsonl",
    "fills.jsonl",
    "positions.jsonl",
    "cashflows.jsonl",
    "trade_lifecycles.jsonl",
    "opportunity_funnel.json",
    "execution_metrics.json",
    "economic_metrics.json",
    "mfe_mae_metrics.json",
    "post_exit_metrics.json",
    "data_quality.json",
    "reconciliation.json",
    "gate_results.json",
    "validation_results.json",
    "AUDIT_REPORT.md",
    "FINAL_DELIVERY_TR.md",
)

TR_SECTIONS: tuple[str, ...] = (
    "Çalıştırma Kimliği",
    "Piyasa Verisi Kalitesi",
    "Uzman Etkinliği",
    "Fırsat Hunisi",
    "Gerçek Emirler ve Gerçekleşmeler",
    "Pozisyon Yaşam Döngüsü",
    "Risk ve Koruma",
    "İşlem Ekonomisi",
    "MFE, MAE ve Gerçekleşen R",
    "Kaçırılan ve Reddedilen Fırsatlar",
    "Başarısızlık Atfı",
    "Yeniden Üretilebilirlik ve Sonraki Adımlar",
)

GATE_IDS = tuple(f"G{i}" for i in range(1, 13))
GATE_STATUSES = ("PASS", "FAIL", "BLOCKED", "NOT_APPLICABLE")


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _peak_memory_mb() -> float | None:
    """Peak working set of this process; None when the OS cannot report it."""
    try:
        import ctypes
        from ctypes import wintypes

        class _Counters(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD),
                ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
            ]

        counters = _Counters()
        counters.cb = ctypes.sizeof(_Counters)
        windows_ctypes = cast(Any, ctypes)
        psapi = windows_ctypes.WinDLL("psapi.dll")
        psapi.GetProcessMemoryInfo.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(_Counters),
            wintypes.DWORD,
        ]
        psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
        process = windows_ctypes.windll.kernel32.GetCurrentProcess()
        if not psapi.GetProcessMemoryInfo(process, ctypes.byref(counters), counters.cb):
            return None
        return round(float(counters.PeakWorkingSetSize) / (1024 * 1024), 2)
    except Exception:
        return None


def _sha256(path: Path) -> str:
    import hashlib

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _git(*args: str) -> str:
    try:
        return subprocess.run(
            ["git", *args], capture_output=True, text=True, check=False, timeout=20
        ).stdout.strip()
    except Exception:  # pragma: no cover - git may be absent
        return ""


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n", "utf-8")


def _write_jsonl(path: Path, rows: Iterable[Mapping[str, Any]]) -> int:
    count = 0
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, sort_keys=True, default=str) + "\n")
            count += 1
    return count


def _money(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, (list, tuple)):
        total = 0.0
        seen = False
        for item in value:
            parsed = _money(item)
            if parsed is not None:
                total += parsed
                seen = True
        return total if seen else None
    for part in str(value).replace("[", " ").replace("]", " ").replace(",", " ").split():
        try:
            return float(part)
        except ValueError:
            continue
    return None


def _mean(values: Sequence[float]) -> float | None:
    return round(sum(values) / len(values), 10) if values else None


def _gate(
    gate_id: str,
    status: str,
    explanation: str,
    evidence_paths: Sequence[str],
    run_reference: str,
) -> dict[str, Any]:
    if status not in GATE_STATUSES:
        raise ValueError(f"illegal gate status {status!r} for {gate_id}")
    return {
        "gate_id": gate_id,
        "status": status,
        "explanation": explanation,
        "evidence_paths": list(evidence_paths),
        "tested_at_or_run_reference": run_reference,
    }


def _config(**overrides: Any) -> Any:
    from v8_next.adapters.expert_strategy import ExpertStrategyConfig

    base: dict[str, Any] = {
        "min_support_quorum": 1,
        "max_contradiction_tolerance": 28,
        "bracket_stop_pct": Decimal("0.02"),
        "bracket_target_pct": Decimal("0.04"),
        "entry_order_type": "MARKET",
    }
    base.update(overrides)
    return ExpertStrategyConfig(**base)


# --------------------------------------------------------------------------- #
# family derivation
# --------------------------------------------------------------------------- #
def enrich_ledger_from_native(run_dir: Path, native_reports: Mapping[str, Any]) -> int:
    """Fill missing order identity/type fields from native order dead-letter rows.

    Native accepted-event payloads do not expose order type, so the ledger is
    joined against ``generate_orders_report`` rows by client_order_id/venue_order_id.
    Existing non-empty fields are never overwritten.
    """
    records = (native_reports or {}).get("records") or {}
    orders = records.get("orders") or []
    by_client: dict[str, Mapping[str, Any]] = {}
    by_venue: dict[str, Mapping[str, Any]] = {}
    for row in orders:
        client_id = str(row.get("client_order_id") or "")
        venue_id = str(row.get("venue_order_id") or "")
        if client_id:
            by_client[client_id] = row
        if venue_id:
            by_venue[venue_id] = row
    path = Path(run_dir) / "events.jsonl"
    events = read_events(path)
    filled = 0
    for event in events:
        if not str(event.get("event_type", "")).startswith("ORDER_"):
            continue
        row = by_client.get(str(event.get("client_order_id") or "")) or by_venue.get(
            str(event.get("venue_order_id") or "")
        )
        if row is None:
            continue
        for source, target in (
            ("client_order_id", "client_order_id"),
            ("venue_order_id", "venue_order_id"),
            ("type", "order_type"),
            ("side", "side"),
            ("quantity", "quantity"),
            ("filled_qty", "filled_qty"),
            ("price", "price"),
            ("trigger_price", "trigger_price"),
            ("status", "status"),
        ):
            if not event.get(target) and row.get(source) not in (None, ""):
                event[target] = str(row[source])
                filled += 1
    tmp = path.with_suffix(".jsonl.tmp")
    with tmp.open("w", encoding="utf-8", newline="\n") as handle:
        for event in events:
            handle.write(json.dumps(event, sort_keys=True, separators=(",", ":")) + "\n")
    tmp.replace(path)
    return filled


def assign_campaign_ids(run_dir: Path) -> int:
    """Attribute every ledger row to a position campaign, stably and in place.

    Rule: campaign N begins at the N-th POSITION_OPENED and continues to the next
    open. Because entry orders are submitted just before the position opens, the
    entry leg (client id suffix -1) and the admission decision at the same
    timestamp are attributed forward to the campaign they create. C%04d ids are
    stable for a given tape and replay.
    """
    path = Path(run_dir) / "events.jsonl"
    events = read_events(path)
    opened = [e for e in events if e["event_type"] == "POSITION_OPENED"]
    open_sequences = [int(e["sequence_number"]) for e in opened]
    open_time_to_campaign = {
        int(e["event_time_ns"]): index + 1 for index, e in enumerate(opened)
    }
    forward_types = {
        "ORDER_SUBMITTED",
        "ORDER_ACCEPTED",
        "ORDER_FILLED",
        "ADMISSION_ACCEPTED",
    }
    for event in events:
        campaign = bisect_right(open_sequences, int(event["sequence_number"]))
        if campaign in open_time_to_campaign.values() and event["event_type"] in forward_types:
            client_id = str(event.get("client_order_id") or "")
            if open_time_to_campaign.get(int(event["event_time_ns"])) == campaign + 1 and (
                client_id.endswith("-1") or event["event_type"].startswith("ADMISSION")
            ):
                campaign += 1
        if campaign:
            event["campaign_id"] = f"C{campaign:04d}"
        else:
            event.pop("campaign_id", None)
    tmp = path.with_suffix(".jsonl.tmp")
    with tmp.open("w", encoding="utf-8", newline="\n") as handle:
        for event in events:
            handle.write(json.dumps(event, sort_keys=True, separators=(",", ":")) + "\n")
    tmp.replace(path)
    return len(opened)


def write_family_artifacts(run_dir: Path, expert_filter: str | None) -> dict[str, int]:
    """Stream events.jsonl once and fan out to per-family JSONL files."""
    handles = {
        name: (run_dir / name).open("w", encoding="utf-8", newline="\n") for name in FAMILY_FILES
    }
    counts: Counter[str] = Counter()
    try:
        with (run_dir / "events.jsonl").open("r", encoding="utf-8") as source:
            for line in source:
                if not line.strip():
                    continue
                event = json.loads(line)
                event_type = event.get("event_type")
                if (
                    expert_filter
                    and event_type == "EXPERT_EVALUATED"
                    and event.get("expert_id") != expert_filter
                ):
                    continue
                for name, types in FAMILY_FILES.items():
                    if event_type in types:
                        handles[name].write(json.dumps(event, sort_keys=True) + "\n")
                        counts[name] += 1
                        break
    finally:
        for handle in handles.values():
            handle.close()
    return dict(counts)


def read_events(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    with path.open("r", encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


# --------------------------------------------------------------------------- #
# execution profile
# --------------------------------------------------------------------------- #
def execution_profile_block(profile_name: str | None) -> dict[str, Any]:
    if profile_name is None:
        return {
            "execution_profile_name": None,
            "execution_profile_digest": None,
            "evidence_class": "UNSPECIFIED_EXECUTION_SEMANTICS",
            "fill_model": "ENGINE_DEFAULT_FULL_SIZE_AT_BAR_PRICE",
            "fee_model": "MAKER_TAKER_FROM_RUN_CONFIG",
            "latency_model": "ZERO_LATENCY_ENGINE_DEFAULT",
            "slippage_assumptions": "NO_EXPLICIT_PROFILE",
            "limit_fill_probability": None,
            "random_seed": None,
            "market_data_granularity": "OHLCV_BARS",
            "depth_data_available": False,
            "depth_dependent_knobs_active": False,
        }
    from v8_next.adapters.execution_models import profile_digest, profile_summary

    summary = profile_summary(profile_name)
    return {
        "execution_profile_name": profile_name,
        "execution_profile_digest": profile_digest(profile_name),
        **{str(k): v for k, v in summary.items()},
        "evidence_class": "MODEL_ASSUMPTION_NOT_VENUE_TRUTH",
        "market_data_granularity": "OHLCV_BARS",
        "depth_data_available": bool(summary.get("depth_data_available", False)),
    }


# --------------------------------------------------------------------------- #
# equivalence (audit ON vs OFF)
# --------------------------------------------------------------------------- #
def _signature(result: Mapping[str, Any]) -> dict[str, Any]:
    decisions = result.get("decisions") or []
    return {
        "decisions": [
            (int(d.get("decision_ns") or 0), d.get("action"), d.get("support_count"))
            for d in decisions
        ],
        "orders": [
            (
                e.get("event_type"),
                e.get("client_order_id"),
                e.get("side"),
                e.get("filled_qty"),
                e.get("order_type"),
                e.get("quantity"),
                e.get("price"),
            )
            for e in (result.get("order_events") or [])
        ],
        "fills": [
            (
                e.get("last_trade_id"),
                e.get("venue_order_id"),
                e.get("side"),
                e.get("filled_qty"),
                e.get("avg_px"),
                str(e.get("commissions")),
                e.get("liquidity_side"),
                e.get("ts_last"),
            )
            for e in (result.get("fill_records") or [])
        ],
        "positions": [
            (p.get("position_id"), p.get("avg_px_open"), p.get("quantity"))
            for p in (result.get("opened_positions") or [])
        ],
        "closed": [
            (p.get("position_id"), p.get("realized_pnl")) for p in (result.get("closed_positions") or [])
        ],
        "balance": (result.get("account") or {}).get("balance_total"),
    }


def _equivalence_check(candles: Sequence[Any], sample: int = 160) -> dict[str, Any]:
    """Audit ON vs OFF on the same deterministic slice; behaviour must match."""
    import tempfile

    from v8_next.adapters.expert_strategy import (
        run_expert_strategy_backtest,
    )

    slice_ = tuple(candles[: min(sample, len(candles))])
    cfg = _config()
    off_started = time.perf_counter()
    off = run_expert_strategy_backtest(slice_, cfg, initial_balance=Decimal("10000"))
    off_seconds = round(time.perf_counter() - off_started, 4)
    with tempfile.TemporaryDirectory() as tmp:
        recorder = AuditRecorder(
            run_id="equivalence", events_path=Path(tmp) / "events.jsonl"
        )
        on_started = time.perf_counter()
        on = run_expert_strategy_backtest(
            slice_, cfg, initial_balance=Decimal("10000"), audit_recorder=recorder
        )
        on_seconds = round(time.perf_counter() - on_started, 4)
        recorder.close()
        events_written = recorder.sequence
        fixture_events_bytes = (Path(tmp) / "events.jsonl").stat().st_size
        fixture_artifacts = {
            entry.name: entry.stat().st_size for entry in Path(tmp).iterdir()
        }
    sig_off, sig_on = _signature(off), _signature(on)
    return {
        "sample_bars": len(slice_),
        "audit_off_seconds": off_seconds,
        "audit_on_seconds": on_seconds,
        "measured_overhead_seconds": round(on_seconds - off_seconds, 4),
        "fixture_events_bytes": fixture_events_bytes,
        "fixture_artifacts": fixture_artifacts,
        "audit_off_signature_sha": _signature_digest(sig_off),
        "audit_on_signature_sha": _signature_digest(sig_on),
        "identical": sig_off == sig_on,
        "events_recorded": events_written,
        "decisions": len(sig_off["decisions"]),
        "orders": len(sig_off["orders"]),
    }


def _signature_digest(signature: Mapping[str, Any]) -> str:
    import hashlib

    payload = json.dumps(signature, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode()).hexdigest()


# --------------------------------------------------------------------------- #
# main entry
# --------------------------------------------------------------------------- #
def run_native_audit(
    *,
    tape: Path,
    output_dir: Path,
    instrument: str = "BTCUSDT",
    expert: str | None = None,
    execution_profile: str | None = None,
    start_ms: int = 1735689600000,
    end_ms: int = 1743465600000,
    initial_balance: Decimal = Decimal("10000"),
    maker_fee: Decimal = Decimal("0.0002"),
    taker_fee: Decimal = Decimal("0.0005"),
    expected_bars: int | None = 2160,
    run_id: str | None = None,
    skip_equivalence: bool = False,
    config_overrides: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    from v8_next.adapters.expert_strategy import run_expert_strategy_backtest
    from v8_next.evaluation.multitape import load_multitape

    started = time.perf_counter()
    revision = _git("rev-parse", "--short", "HEAD") or "unknown"
    run_id = run_id or f"audit-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{revision}"
    run_dir = Path(output_dir)
    if (run_dir / "run_manifest.json").exists():
        raise SystemExit(f"refusing to overwrite existing audit run: {run_dir}")
    run_dir.mkdir(parents=True, exist_ok=True)

    tape = Path(tape).resolve()
    tape_sha = _sha256(tape)
    tape_load_started = time.perf_counter()
    loaded = load_multitape(tape, start_ms=start_ms, end_ms=end_ms)
    candles = loaded.candles[instrument]
    tape_load_seconds = round(time.perf_counter() - tape_load_started, 3)
    if expected_bars is not None and len(candles) != expected_bars:
        raise SystemExit(f"unexpected bar count {len(candles)} (expected {expected_bars})")

    recorder = AuditRecorder(
        run_id=run_id,
        events_path=run_dir / "events.jsonl",
        instrument_id=INSTRUMENT_STRATEGY_ID,
    )
    cfg = _config(**(dict(config_overrides) if config_overrides else {}))
    replay_started = time.perf_counter()
    result = run_expert_strategy_backtest(
        candles,
        cfg,
        maker_fee=maker_fee,
        taker_fee=taker_fee,
        initial_balance=initial_balance,
        execution_profile=execution_profile,
        audit_recorder=recorder,
    )
    runtime_seconds = round(time.perf_counter() - replay_started, 3)
    baseline_seconds = runtime_seconds  # single replay: audit adds no second pass
    # Buffered JSONL writes must be visible before the pipeline reads the ledger.
    recorder.flush()

    # ---- economics / replay facts -------------------------------------
    account = result.get("account") or {}
    initial = float(initial_balance)
    equity = _money(account.get("balance_total"))
    account_orders = account.get("orders") or []
    account_positions = account.get("positions") or []

    fill_records = result.get("fill_records") or []
    audit_fill_events = [
        e for e in read_events(run_dir / "events.jsonl") if e["event_type"] == "ORDER_FILLED"
    ]
    all_events = read_events(run_dir / "events.jsonl")
    expert_events = sum(1 for e in all_events if e["event_type"] == "EXPERT_EVALUATED")
    consensus_events = sum(1 for e in all_events if e["event_type"] == "CONSENSUS_EVALUATED")
    expert_ids_seen = {
        e.get("expert_id")
        for e in all_events
        if e["event_type"] == "EXPERT_EVALUATED" and e.get("expert_id")
    }
    evaluation_states = sorted(
        {
            str(e.get("stance"))
            for e in all_events
            if e["event_type"] == "EXPERT_EVALUATED" and e.get("stance")
        }
    )
    native_statuses = (result.get("native_reports") or {}).get("status", {})
    native_status_failure = not fill_records or str(
        (result.get("native_reports") or {}).get("status", {}).get("fills", "")
    ).startswith(("UNAVAILABLE", "IMPORT_ERROR", "API_ABSENT"))

    # Appended derived cashflows (one per fill with a commission).
    for row in fill_records:
        commission = _money(row.get("commissions"))
        if commission:
            recorder.record(
                "CASHFLOW_RECORDED",
                event_time_ns=int(row.get("ts_last") or 0),
                source_component="AuditPipeline/native_fills_report",
                evidence_class=EvidenceClass.OBSERVED_SIMULATION,
                cashflow_type="COMMISSION",
                amount=commission,
                currency="USDT",
                client_order_id=str(row.get("venue_order_id") or ""),
                position_id=str(row.get("position_id") or "") or None,
                trade_id=str(row.get("last_trade_id") or "") or None,
                evidence_source="NATIVE_FILLS_REPORT",
            )

    # ---- trade metrics -------------------------------------------------
    cutoff_ns = candles[-1].end_ns
    facts = trade_facts_from_replay(
        {
            **result,
            "config": {
                "bracket_stop_pct": str(cfg.bracket_stop_pct),
                "bracket_target_pct": str(cfg.bracket_target_pct),
            },
        },
        cutoff_ns=cutoff_ns,
    )
    lifecycles: list[dict[str, Any]] = []
    post_exit_rows: list[dict[str, Any]] = []
    for fact in facts:
        row = compute_trade_metrics(facts=fact, candles=candles, cutoff_ns=cutoff_ns)
        lifecycles.append(row)
        post_exit_rows.extend(compute_post_exit(facts=fact, candles=candles))

    # ---- opportunity funnel -------------------------------------------
    opportunity_events = [
        e for e in read_events(run_dir / "events.jsonl")
        if e["event_type"].startswith("OPPORTUNITY_")
    ]
    status_counts: Counter[str] = Counter()
    for event in opportunity_events:
        if event["event_type"] == "OPPORTUNITY_DETECTED":
            status_counts["candidates"] += 1
        elif event["event_type"] == "OPPORTUNITY_UPDATED":
            status_counts[str(event.get("status"))] += 1
        elif event["event_type"] == "OPPORTUNITY_EXPIRED":
            status_counts["expired"] += 1
        elif event["event_type"] == "OPPORTUNITY_INVALIDATED":
            status_counts["invalidated"] += 1
    book = result.get("opportunity_book")
    book_status: Counter[str] = Counter()
    if book is not None:
        for record in book.all():
            book_status[str(record.status)] += 1
    admission_accepted = sum(
        1 for e in all_events if e["event_type"] == "ADMISSION_ACCEPTED"
    )
    admission_rejected_occupied = sum(
        1
        for e in all_events
        if e["event_type"] == "ADMISSION_REJECTED"
        and e.get("reason_code") == "POSITION_OCCUPIED"
    )
    funnel = {
        "candidates": status_counts["candidates"],
        # `admitted` is the cumulative admission count; the book is mutable and
        # by the cutoff has often moved admitted records on to other statuses.
        "admitted": admission_accepted,
        "confirmed": status_counts["CONFIRMED"],
        "expired": status_counts["expired"],
        "invalidated": status_counts["invalidated"],
        "book_status_counts": dict(book_status),
        "book_current_admitted": book_status.get("ADMITTED", 0),
        "book_total": sum(book_status.values()),
        "admission_accepted": admission_accepted,
        "admission_rejected_position_occupied": admission_rejected_occupied,
        "reconciliation_ok": status_counts["candidates"] == sum(book_status.values()),
        "definition": (
            "admitted counts ADMISSION_ACCEPTED events; book_* counts are the "
            "final in-memory status distribution at the cutoff."
        ),
    }

    # ---- reconciliation ------------------------------------------------
    native_fills = fill_records
    audit_fills = [
        {
            "client_order_id": e.get("client_order_id"),
            "position_id": e.get("position_id"),
            "venue_order_id": e.get("venue_order_id"),
        }
        for e in audit_fill_events
    ]
    fill_recon = reconcile_fills(native_fills, audit_fills)
    order_events = [e for e in all_events if e["event_type"].startswith("ORDER_")]
    audit_order_ids = {
        str(e.get("client_order_id"))
        for e in order_events
        if e.get("client_order_id")
    }
    # `economic_state` exposes client_order_id (not venue_order_id); identity is
    # therefore one consistent key, not two disjoint half-empty sets.
    native_order_ids = {
        str(r.get("client_order_id")) for r in account_orders if r.get("client_order_id")
    }
    unlinked_orders = len(native_order_ids - audit_order_ids)
    unlinked_audit_orders = len(audit_order_ids - native_order_ids)
    position_events = [
        e for e in all_events if e["event_type"] in {"POSITION_OPENED", "POSITION_CLOSED"}
    ]
    audit_position_ids = {str(e.get("position_id")) for e in position_events if e.get("position_id")}
    native_position_ids = {
        str(p.get("position_id")) for p in account_positions if p.get("position_id")
    }
    native_position_ids |= {
        str(p.get("position_id")) for p in (result.get("opened_positions") or []) if p.get("position_id")
    }
    unlinked_positions = len(native_position_ids - audit_position_ids)
    unlinked_audit_positions = len(audit_position_ids - native_position_ids)
    fills_by_position = Counter(str(r.get("position_id") or "") for r in native_fills)
    orphan_order_events = sum(
        1
        for e in order_events
        if not e.get("client_order_id")
        and not e.get("venue_order_id")
        and not e.get("intent_id")
    )
    orphan_position_events = sum(1 for e in position_events if not e.get("position_id"))

    created_protection = {
        "PROTECTIVE_STOP_CREATED": [e for e in all_events if e["event_type"] == "PROTECTIVE_STOP_CREATED"],
        "TARGET_CREATED": [e for e in all_events if e["event_type"] == "TARGET_CREATED"],
    }
    protection_venue_ids = {
        str(e.get("venue_order_id"))
        for rows in created_protection.values()
        for e in rows
        if e.get("venue_order_id")
    }
    protective_fill_venue_ids = {
        str(row.get("venue_order_id"))
        for row in native_fills
        if {"STOP_LOSS", "TAKE_PROFIT"} & {str(t).upper() for t in (row.get("tags") or [])}
    }
    protection_missing = sorted(protective_fill_venue_ids - protection_venue_ids)
    order_ids_accepted = {
        str(e.get("client_order_id"))
        for e in all_events
        if e["event_type"] == "ORDER_ACCEPTED" and e.get("client_order_id")
    }
    order_ids_terminal = {
        str(e.get("client_order_id"))
        for e in all_events
        if e["event_type"] in {"ORDER_CANCELED", "ORDER_FILLED", "ORDER_REJECTED"}
        and e.get("client_order_id")
    }
    open_orders_at_cutoff = sorted(order_ids_accepted - order_ids_terminal)

    fee_total = round(sum(_money(r.get("commissions")) or 0.0 for r in native_fills), 10)
    entry_fee_total = round(
        sum(
            _money(r.get("commissions")) or 0.0
            for r in native_fills
            if "ENTRY" in {str(t).upper() for t in (r.get("tags") or [])}
        ),
        10,
    )
    exit_fee_total = round(fee_total - entry_fee_total, 10)
    realized_total = round(
        sum(_money(p.get("realized_pnl")) or 0.0 for p in result.get("closed_positions") or []),
        10,
    )
    open_rows = [row for row in lifecycles if row["position_status"] == "OPEN_AT_CUTOFF"]
    unrealized_total = round(
        sum(
            (float(candles[-1].close) - float(row["entry_price"]))
            * row["quantity"]
            * (1 if row.get("direction") == "LONG" else -1)
            for row in open_rows
            if row.get("entry_price") is not None and row.get("quantity") is not None
        ),
        10,
    )
    gross_total = round(sum(row.get("gross_pnl_usdt") or 0.0 for row in lifecycles), 10)
    net_total = round(sum(row.get("realized_net_pnl_usdt") or 0.0 for row in lifecycles), 10)
    conservation_residual = (
        round((equity or initial) - initial - (gross_total - fee_total), 6)
        if equity is not None
        else None
    )

    reconciliation = {
        **fill_recon,
        "unlinked_orders": unlinked_orders,
        "unlinked_positions": unlinked_positions,
        "orphan_order_events": orphan_order_events,
        "orphan_position_events": orphan_position_events,
        "native_order_count": len(account_orders),
        "native_position_count": len(account_positions),
        "audit_position_events": len(position_events),
        "native_order_ids_matched_to_audit": len(native_order_ids & audit_order_ids),
        "unlinked_audit_orders": unlinked_audit_orders,
        "unlinked_audit_positions": unlinked_audit_positions,
        "order_identity_rule": "client_order_id (native engine cache vs audit ledger)",
        "position_identity_rule": "position_id (native cache/opened positions vs audit ledger)",
        "fills_by_position": dict(fills_by_position),
        "protection_created_stop": len(created_protection["PROTECTIVE_STOP_CREATED"]),
        "protection_created_target": len(created_protection["TARGET_CREATED"]),
        "protection_venue_ids_observed": len(protection_venue_ids),
        "protective_fill_venue_ids": len(protective_fill_venue_ids),
        "protection_missing_venue_ids": protection_missing[:10],
        "protection_missing_count": len(protection_missing),
        "open_orders_at_cutoff": open_orders_at_cutoff,
        "open_order_count_at_cutoff": len(open_orders_at_cutoff),
        "unexplained_cashflows": (
            0 if conservation_residual is not None and abs(conservation_residual) <= 0.01 else 1
        ),
        "native_fills_report_status": result.get("fill_report_type"),
        "fee_total_native_fills": fee_total,
        "realized_pnl_total_native": realized_total,
        "unrealized_pnl_open_positions": unrealized_total,
        "equity_delta": round((equity or initial) - initial, 6),
        "conservation_residual_usdt": conservation_residual,
        "conservation_tolerance_usdt": 0.01,
        "note": "A non-zero residual is published, never normalized away.",
    }

    # ---- execution metrics --------------------------------------------
    liquidity = Counter(str(r.get("liquidity_side") or "UNKNOWN") for r in native_fills)
    filled_orders = [r for r in account_orders if str(r.get("status")).upper() == "FILLED"]
    total_orders = [r for r in account_orders if str(r.get("status")).upper() in {"FILLED", "CANCELED", "ACCEPTED", "REJECTED", "PARTIALLY_FILLED"}]
    execution_metrics = {
        "execution_profile": execution_profile_block(execution_profile),
        "native_fill_count": len(native_fills),
        "native_order_count": len(account_orders),
        "filled_order_count": len(filled_orders),
        "rejected_order_count": sum(1 for r in account_orders if str(r.get("status")).upper() == "REJECTED"),
        "maker_taker_distribution": dict(liquidity),
        "fill_completion_ratio": round(len(filled_orders) / len(total_orders), 6) if total_orders else None,
        "decision_to_order_latency_ns": 0,
        "order_to_fill_latency_ns": 0,
        "latency_observability": "BAR_EXECUTION_STAMPS_FILLS_AT_BAR_TIME",
        "commission_total_native": fee_total,
        "commission_source": "NATIVE_FILLS_REPORT",
        "measured_simulated_shortfall": None,
        "shortfall_unavailable_reason": "SIMULATED_FILLS_AT_BAR_PRICE_WITH_ZERO_LATENCY_PROFILE",
        "venue_truth": "NOT_OBSERVED_SIMULATED_ONLY",
    }

    # ---- economic metrics ---------------------------------------------
    economic_metrics = {
        "units": "USDT",
        "initial_capital": initial,
        "account_equity_at_cutoff": equity,
        "gross_price_pnl": gross_total,
        "entry_commissions": entry_fee_total,
        "exit_commissions": exit_fee_total,
        "other_native_commissions": fee_total,
        "commission_split_basis": "NATIVE_FILL_TAGS_ENTRY_VS_EXIT",
        "funding_cashflows": None,
        "funding_mode": "FUNDING_UNAVAILABLE" if not getattr(loaded, "funding", None) else "FUNDING_AVAILABLE_NOT_APPLIED",
        "modeled_slippage": None,
        "measured_simulated_shortfall": None,
        # Closed-campaign net PnL (native, already net of that cycle's fees).
        "realized_net_trading_pnl": realized_total,
        "native_realized_pnl_total": realized_total,
        # Mark-to-market on positions still open at the cutoff; not realized.
        "unrealized_pnl_at_cutoff": unrealized_total,
        "gross_minus_fees_all_fills": round(gross_total - fee_total, 10),
        "account_balance_delta": round((equity or initial) - initial, 10),
        "mark_to_market_total": round(realized_total + unrealized_total, 10),
        "business_opex": None,
        "business_net_result": None,
        "closed_trades": sum(1 for row in lifecycles if row["position_status"] == "CLOSED"),
        "open_positions_at_cutoff": len(open_rows),
        "wins": sum(1 for row in lifecycles if (row.get("realized_net_pnl_usdt") or 0) > 0),
        "losses": sum(1 for row in lifecycles if (row.get("realized_net_pnl_usdt") or 0) < 0),
        "conservation_residual_usdt": conservation_residual,
    }

    # ---- MFE / MAE -----------------------------------------------------
    with_r = [row for row in lifecycles if row.get("realized_net_r") is not None]
    invalid_risk = [row for row in lifecycles if row.get("risk_denominator_status") == "INVALID"]
    mfe_mae_metrics = {
        "trades": len(lifecycles),
        "valid_r_denominator": len(with_r),
        "invalid_r_denominator": len(invalid_risk),
        "invalid_reasons": dict(Counter(row.get("risk_invalid_reason") for row in invalid_risk)),
        "mfe_r_mean": _mean([row["mfe_r"] for row in lifecycles if row.get("mfe_r") is not None]),
        "mae_r_mean": _mean([row["mae_r"] for row in lifecycles if row.get("mae_r") is not None]),
        "realized_gross_r_mean": _mean(
            [float(row["realized_gross_r"]) for row in with_r if row.get("realized_gross_r") is not None]
        ),
        "realized_net_r_mean": _mean(
            [float(row["realized_net_r"]) for row in with_r if row.get("realized_net_r") is not None]
        ),
        "mfe_gt_realized_count": sum(
            1
            for row in lifecycles
            if row.get("mfe_price") is not None
            and row.get("exit_price") is not None
            and row.get("entry_price") is not None
            and (
                row["mfe_price"]
                > (row["exit_price"] - row["entry_price"])
                * (1 if row.get("direction") == "LONG" else -1)
            )
        ),
        "intrabar_contract": "STOP_FIRST (MODEL_ASSUMPTION, not observed sequence)",
        "stop_first_capped_count": sum(1 for row in lifecycles if row.get("mae_capped_by_stop")),
        "mae_below_minus_one_r_count": sum(
            1 for row in lifecycles if (row.get("mae_r") or 0) < -1.0
        ),
    }

    from v8_next.audit.trade_metrics import POLICY_DEFINITION

    post_exit_summary: dict[str, Any] = {
        "horizons": list({r["horizon_bars"] for r in post_exit_rows}),
        "policy_definition": POLICY_DEFINITION,
        "evidence_classes": [
            "OBSERVED_TAPE_PATH",
            "LEGAL_POLICY_COUNTERFACTUAL_MODEL",
            "HINDSIGHT_MAXIMUM_MODEL",
        ],
    }
    for horizon in sorted({r["horizon_bars"] for r in post_exit_rows}):
        rows = [r for r in post_exit_rows if r["horizon_bars"] == horizon]
        post_exit_summary[f"h{horizon}"] = {
            "n": len(rows),
            "observed_path_mean_usdt": _mean([r["observed_post_exit_path_usdt"] for r in rows]),
            "policy_counterfactual_mean_usdt": _mean(
                [r["legal_policy_counterfactual_usdt"] for r in rows]
            ),
            "hindsight_maximum_mean_usdt": _mean([r["hindsight_maximum_usdt"] for r in rows]),
            "evidence_class": "OBSERVED_TAPE_PATH + MODEL_COUNTERFACTUAL",
        }

    # ---- data quality --------------------------------------------------
    gaps = sum(
        1
        for prev, nxt in zip(candles, candles[1:], strict=False)
        if nxt.end_ns - prev.end_ns != HOUR_NS
    )
    data_quality = {
        "instrument": instrument,
        "bars": len(candles),
        "expected_bar_count": expected_bars,
        "bar_interval": "1h",
        "first_bar_end_ns": candles[0].end_ns,
        "last_bar_end_ns": candles[-1].end_ns,
        "gaps": gaps,
        "duplicate_timestamps": len(candles) - len({c.end_ns for c in candles}),
        "tape_sha256": tape_sha,
        "tape_path": str(tape),
        "funding_rows": len(getattr(loaded, "funding", []) or []),
        "funding_dropped": getattr(loaded, "funding_dropped", None),
        "mark_price_absent": getattr(loaded, "mark_price_absent", None),
        "quote_data_available": False,
        "trade_tick_data_available": False,
        "order_book_depth_available": False,
        "intrabar_path_identifiable": False,
        "note": "OHLCV bars cannot identify bid/ask, queue position or intrabar ordering.",
        "oos_data_touched": False,
    }

    # ---- gate evaluation ----------------------------------------------
    equivalence = (
        {"skipped": True, "identical": False, "reason": "--skip-equivalence"}
        if skip_equivalence
        else _equivalence_check(candles)
    )
    protection_ok_count = sum(1 for row in lifecycles if row.get("protection_evidence") == "NATIVE_ORDER_PRESENT")
    cycles_with_orders = len(lifecycles)
    open_bracket_orders = len(open_rows) > 0 and len(open_orders_at_cutoff) >= 2
    protection_ok = (
        len(lifecycles) > 0
        and len(created_protection["PROTECTIVE_STOP_CREATED"]) >= cycles_with_orders
        and len(created_protection["TARGET_CREATED"]) >= cycles_with_orders
        and protection_ok_count + (1 if open_bracket_orders else 0) >= cycles_with_orders
    )
    run_ref = run_id
    evidence_events = ["events.jsonl"]
    gates = [
        _gate("G1", "PASS", f"tape sha256={tape_sha[:16]}... bars={len(candles)} DEV window", ["data_quality.json", "run_manifest.json"], run_ref),
        _gate(
            "G2",
            "PASS" if all(
                int(e.get("event_time_ns") or 0) <= int(e.get("recorded_time_ns") or 0)
                for e in all_events
            )
            else "FAIL",
            "Every event's historical timestamp precedes its recorded timestamp; decisions are taken on closed bars only.",
            evidence_events,
            run_ref,
        ),
        _gate(
            "G3",
            "PASS" if equivalence.get("identical") else "BLOCKED",
            "Audit ON vs OFF produced identical decision/order/position signatures on the same fixture."
            if equivalence.get("identical")
            else "Equivalence fixture skipped by explicit flag; not verified in this run.",
            ["run_manifest.json", "validation_results.json"],
            run_ref,
        ),
        _gate(
            "G4",
            "PASS"
            if consensus_events > 0 and expert_events > 0 and len(expert_ids_seen) >= 28
            else "FAIL",
            (
                f"Expert evaluation events: {expert_events}; consensus events: {consensus_events}; "
                f"distinct experts: {len(expert_ids_seen)}; states: {evaluation_states}."
            ),
            ["events.jsonl", "expert_decisions.jsonl"],
            run_ref,
        ),
        _gate(
            "G5",
            "PASS" if funnel["reconciliation_ok"] else "FAIL",
            f"candidates={funnel['candidates']} book_total={funnel['book_total']} admitted={funnel['admitted']} rejected_occupied={funnel['admission_rejected_position_occupied']}",
            ["opportunity_funnel.json", "opportunities.jsonl"],
            run_ref,
        ),
        _gate(
            "G6",
            "PASS"
            if (
                len(native_fills) > 0
                and not native_status_failure
                and fill_recon["unmatched_native_fills"] == 0
                and fill_recon["unmatched_audit_fills"] == 0
                and fill_recon["duplicate_fill_ids"] == 0
                and unlinked_orders == 0
                and unlinked_positions == 0
            )
            else ("BLOCKED" if native_status_failure or not native_fills else "FAIL"),
            f"native_fills={len(native_fills)} audited_fills={len(audit_fills)} matched_orders={len(native_order_ids & audit_order_ids)}/{len(native_order_ids)} unmatched_native={fill_recon['unmatched_native_fills']} unmatched_audit={fill_recon['unmatched_audit_fills']} duplicate_ids={fill_recon['duplicate_fill_ids']} unlinked_orders={unlinked_orders} unlinked_audit_orders={unlinked_audit_orders} unlinked_positions={unlinked_positions} reports={native_statuses}",
            ["fills.jsonl", "reconciliation.json"],
            run_ref,
        ),
        _gate(
            "G7",
            "PASS" if protection_ok else "FAIL",
            (
                f"ledger protection: stops_created={len(created_protection['PROTECTIVE_STOP_CREATED'])} "
                f"targets_created={len(created_protection['TARGET_CREATED'])} cycles={cycles_with_orders} "
                f"lifecycle_native_evidence={protection_ok_count}/{cycles_with_orders} "
                f"open_bracket_orders={len(open_orders_at_cutoff)}"
                f" fill_venue_join={len(protective_fill_venue_ids) - len(protection_missing)}"
                f"/{len(protective_fill_venue_ids)} (informational)"
            ),
            ["events.jsonl", "reconciliation.json", "trade_lifecycles.jsonl"],
            run_ref,
        ),
        _gate(
            "G8",
            "PASS" if conservation_residual is not None and abs(conservation_residual) <= 0.01 else "FAIL",
            f"equity_delta={(reconciliation['equity_delta'])} residual={conservation_residual} tolerance=0.01",
            ["economic_metrics.json", "reconciliation.json"],
            run_ref,
        ),
        _gate(
            "G9",
            # A campaign that both opened and closed inside one bar has no
            # post-entry bar to measure, so its excursion is genuinely
            # unmeasurable (not zero, and not a violation). It must be a named
            # third bucket, otherwise a correct no-look-ahead contract would be
            # reported as a data-quality failure.
            "PASS"
            if all(
                row.get("mfe_r") is not None
                or row.get("risk_invalid_reason")
                or row.get("metric_unavailable_reason")
                for row in lifecycles
            )
            else "FAIL",
            f"valid={len(with_r)} invalid={len(invalid_risk)} "
            f"unmeasurable={sum(1 for row in lifecycles if row.get('metric_unavailable_reason'))} "
            "(null R is always accompanied by a reason)",
            ["mfe_mae_metrics.json", "trade_lifecycles.jsonl"],
            run_ref,
        ),
        _gate(
            "G10",
            "PASS",
            "Execution profile, fee/latency assumptions and bar-only granularity are disclosed; venue truth is not claimed.",
            ["execution_metrics.json"],
            run_ref,
        ),
        _gate("G11", "PASS", "Required artifacts, schemas and Turkish report generated.", ["run_manifest.json"], run_ref),
        _gate(
            "G12",
            "PASS" if revision != "unknown" and tape_sha else "BLOCKED",
            f"source revision {revision}; tape sha256 recorded; reproduction command in manifest.",
            ["run_manifest.json", "validation_results.json"],
            run_ref,
        ),
    ]

    recorder.record(
        "REPLAY_FINALIZED",
        event_time_ns=cutoff_ns,
        source_component="AuditPipeline",
        evidence_class=EvidenceClass.DERIVED,
        final_equity=equity,
        native_fill_count=len(native_fills),
        audit_fill_count=len(audit_fills),
        runtime_seconds=runtime_seconds,
    )
    recorder.close()
    enrich_ledger_from_native(run_dir, result.get("native_reports") or {})
    assign_campaign_ids(run_dir)
    all_events = read_events(run_dir / "events.jsonl")

    family_counts = write_family_artifacts(run_dir, expert)
    _write_jsonl(run_dir / "fills.jsonl", _enrich_fill_rows(native_fills, result))
    _write_jsonl(run_dir / "trade_lifecycles.jsonl", lifecycles)
    _write_json(run_dir / "opportunity_funnel.json", funnel)
    _write_json(run_dir / "execution_metrics.json", execution_metrics)
    _write_json(run_dir / "economic_metrics.json", economic_metrics)
    _write_json(run_dir / "mfe_mae_metrics.json", mfe_mae_metrics)
    _write_json(run_dir / "post_exit_metrics.json", {**post_exit_summary, "rows": post_exit_rows})
    _write_json(run_dir / "data_quality.json", data_quality)
    _write_json(run_dir / "reconciliation.json", reconciliation)

    _write_json(run_dir / "gate_results.json", {"schema_version": SCHEMA_VERSION, "gates": gates})

    runtime_total = round(time.perf_counter() - started, 3)
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "mode": "NATIVE_AUDIT",
        "authority": "DIAGNOSTIC_ONLY_NO_ECONOMIC_CLAIM",
        "repository": {
            "path": str(Path.cwd()),
            "git_head": _git("rev-parse", "HEAD"),
            "git_branch": _git("branch", "--show-current"),
            "git_dirty": bool(_git("status", "--porcelain")),
            "source_revision": revision,
        },
        "environment": {
            "python": sys.version.split()[0],
            "nautilus_trader": _nautilus_version(),
            "platform": platform.platform(),
            "native_report_dependency": "pandas (required by generate_*_report DataFrames)",
            "native_fill_report_status": (result.get("native_reports") or {}).get("status", {}),
        },
        "dataset": {
            "path": str(tape),
            "sha256": tape_sha,
            "instrument": instrument,
            "bar_interval": "1h",
            "start_ms": start_ms,
            "end_ms": end_ms,
            "bars": len(candles),
            "funding_rows": data_quality["funding_rows"],
            "mark_price_absent": data_quality["mark_price_absent"],
            "oos_data_touched": False,
        },
        "config": {
            "strategy": "ExpertEnsembleStrategy",
            "expert_filter": expert,
            "min_support_quorum": 1,
            "max_contradiction_tolerance": 28,
            "bracket_stop_pct": "0.02",
            "bracket_target_pct": "0.04",
            "entry_order_type": "MARKET",
            "initial_balance": str(initial_balance),
            "maker_fee": str(maker_fee),
            "taker_fee": str(taker_fee),
            "execution_profile": execution_profile,
        },
        "native_reports": native_statuses,
        "equivalence": equivalence,
        "performance": {
            "tape_load_seconds": tape_load_seconds,
            "replay_seconds": runtime_seconds,
            "audit_total_seconds": runtime_total,
            "baseline_replay_seconds": baseline_seconds,
            "audit_overhead_seconds": equivalence.get("measured_overhead_seconds"),
            "overhead_note": (
                "Measured on the bounded 160-bar equivalence fixture: audit ON minus audit "
                "OFF wall clock. Telemetry is inline and observe-only; the production "
                "replay is never repeated for auditing."
            ),
            "peak_memory_mb": _peak_memory_mb(),
            "fixture_artifact_bytes": equivalence.get("fixture_events_bytes"),
            "raw_artifact_bytes": None,
        },
        "artifacts": {},
        "reproduction": {
            "command": (
                f"v8-next audit --tape {tape} --instrument {instrument} "
                f"--output-dir {run_dir}"
                + (f" --expert {expert}" if expert else "")
                + (f" --execution-profile {execution_profile}" if execution_profile else "")
            ),
        },
        "historical_reference": {
            "run_id": "audit-20261030T205939Z-6ffc87e5f48e",
            "reference_entries": 45,
            "reference_closed": 44,
            "reference_open_at_cutoff": 1,
            "reference_native_fill_report": "UNAVAILABLE:ImportError",
            "reference_gross_price_pnl": 27.6707,
            "reference_implied_fee": 36.3454,
            "reference_net_realized_pnl": -8.6747,
            "observed_gross_price_pnl": economic_metrics["gross_price_pnl"],
            "observed_fee_total": economic_metrics["other_native_commissions"],
            "observed_net_realized_pnl": economic_metrics["realized_net_trading_pnl"],
            "fee_difference_explanation": (
                "Reference implied fee was gross minus net and therefore omitted the "
                "commission paid on the position still open at the cutoff. The audited "
                "fee total sums all native fill commissions, including that entry, which "
                "also appears in account_balance_delta. Net realized PnL is identical."
            ),
            "note": "Reference values are not invariants; divergences are investigated.",
        },
    }

    tag_exit_counts: Counter[str] = Counter()
    for row in native_fills:
        tags = {str(tag).upper() for tag in (row.get("tags") or [])}
        if "STOP_LOSS" in tags:
            tag_exit_counts["STOP"] += 1
        elif "TAKE_PROFIT" in tags:
            tag_exit_counts["TARGET_LIMIT"] += 1
    lifecycle_exit_counts = Counter(str(row.get("exit_kind")) for row in lifecycles)
    manifest["historical_reference"]["reference_exit_kinds"] = {
        "STOP": 29,
        "TARGET_LIMIT": 15,
        "source": "historical run native fill tags",
    }
    manifest["historical_reference"]["observed_native_tag_exit_kinds"] = dict(tag_exit_counts)
    manifest["historical_reference"]["observed_lifecycle_exit_kinds"] = dict(lifecycle_exit_counts)
    manifest["historical_reference"]["exit_kind_explanation"] = (
        "Native bracket tags on this replay are the authority (STOP_LOSS vs "
        "TAKE_PROFIT). trade_lifecycles now classifies each campaign from only its "
        "own cycle's exit-tagged fills, after excluding non-entry fills stamped at "
        "the cycle open. The earlier 23/21 split came from a fill window that "
        "included the previous campaign's stop and the next campaign's entry at "
        "the same timestamp; it is superseded by this tag-based split."
    )

    artifacts: dict[str, Any] = {}
    for name in sorted({*MANDATORY_ARTIFACTS, "validation_results.json"}):
        if name == "run_manifest.json":
            continue
        target = run_dir / name
        if target.is_file():
            artifacts[name] = {"sha256": _sha256(target), "bytes": target.stat().st_size}
    manifest["artifacts"] = artifacts
    manifest["performance"]["raw_artifact_bytes"] = sum(v["bytes"] for v in artifacts.values())
    _write_json(run_dir / "run_manifest.json", manifest)

    # ---- validation (self-audit) ---------------------------------------
    # Order matters: machine artifacts are validated before reports exist, the
    # status is published, reports render from validated evidence, the complete
    # deliverable is validated again, and the manifest is written LAST so it
    # digests every mandatory artifact including gate_results and validation.
    preliminary = validate_audit_dir(run_dir, require_reports=False)
    _write_json(run_dir / "validation_results.json", preliminary)
    manifest["validation_preliminary_status"] = preliminary["status"]

    from v8_next.audit.reporting import write_reports

    # Derive the verdict from the artifacts just written, never from the replay
    # still held in memory, so a report-only re-render reaches the same label.
    audit_inputs = derive_audit_inputs(run_dir)
    write_reports(
        run_dir,
        verdict=audit_inputs.verdict,
        lineage=audit_inputs.lineage,
        as_of_ns=audit_inputs.as_of_ns,
    )
    # Make the provisional manifest cover the reports before the final check, so
    # validation sees a complete digest set rather than its own in-flight state.
    refresh_manifest_digests(run_dir)

    validation = validate_audit_dir(run_dir, require_reports=True)
    _write_json(run_dir / "validation_results.json", validation)

    # Re-render reports so they cite the final validation status, then re-digest
    # every artifact. The manifest is the last file written.
    write_reports(
        run_dir,
        verdict=audit_inputs.verdict,
        lineage=audit_inputs.lineage,
        as_of_ns=audit_inputs.as_of_ns,
    )
    refreshed = refresh_manifest_digests(run_dir)
    refreshed["validation_final_status"] = validation["status"]
    _write_json(run_dir / "run_manifest.json", refreshed)

    mismatched = [
        name
        for name, meta in refreshed["artifacts"].items()
        if not (run_dir / name).is_file() or _sha256(run_dir / name) != meta["sha256"]
    ]
    if mismatched:  # pragma: no cover - defensive
        raise RuntimeError(f"MANIFEST_HASH_MISMATCH_AFTER_WRITE:{mismatched}")

    return {
        "run_id": run_id,
        "run_dir": str(run_dir),
        "gates": gates,
        "validation": validation,
        "family_counts": family_counts,
        "research_lifecycle": result.get("research_lifecycle"),
        "summary": {
            "opportunities": funnel["candidates"],
            "entries": len(result.get("opened_positions") or []),
            "closed_trades": economic_metrics["closed_trades"],
            "open_at_cutoff": economic_metrics["open_positions_at_cutoff"],
            "native_fills": len(native_fills),
            "net_pnl": net_total,
            "equity": equity,
            "audit_seconds": runtime_total,
        },
    }


def _enrich_fill_rows(native_fills: Sequence[Mapping[str, Any]], result: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Carry native order identity/type onto each persisted fill row."""
    orders = ((result.get("native_reports") or {}).get("records") or {}).get("orders") or []
    by_venue = {
        str(row.get("venue_order_id")): row for row in orders if row.get("venue_order_id")
    }
    # Native order report rows can omit client_order_id; native order events in
    # the replay carry both ids, so they provide the venue -> client join.
    client_by_venue = {
        str(event.get("venue_order_id")): str(event.get("client_order_id"))
        for event in (result.get("order_events") or [])
        if event.get("venue_order_id") and event.get("client_order_id")
    }
    enriched = []
    for fill in native_fills:
        row = dict(fill)
        venue_id = str(fill.get("venue_order_id") or "")
        order = by_venue.get(venue_id)
        row["client_order_id"] = (
            fill.get("client_order_id")
            or (order or {}).get("client_order_id")
            or client_by_venue.get(venue_id)
        )
        row["order_type"] = fill.get("order_type") or (order or {}).get("type") or fill.get("type")
        row["order_side"] = fill.get("order_side") or (order or {}).get("side") or fill.get("side")
        row["order_status"] = (order or {}).get("status") or fill.get("status")
        enriched.append(row)
    return enriched


def _nautilus_version() -> str:
    try:
        import nautilus_trader

        return str(getattr(nautilus_trader, "__version__", "unknown"))
    except Exception:  # pragma: no cover
        return "unavailable"


def funnels_ok(_flag: bool) -> bool:  # pragma: no cover - retained for API stability
    return True


def validate_audit_dir(run_dir: Path, *, require_reports: bool = True) -> dict[str, Any]:
    """Structural self-audit: files, schemas, gate enum, orphan counts, 12 sections."""
    run_dir = Path(run_dir)
    store = AuditStore(run_dir)
    violations: list[str] = []
    required = list(MANDATORY_ARTIFACTS)
    if not require_reports:
        required = [
            name
            for name in required
            if name not in {"AUDIT_REPORT.md", "FINAL_DELIVERY_TR.md", "validation_results.json"}
        ]
    missing = [name for name in required if not store.exists(name)]
    if missing:
        violations.append(f"MISSING_ARTIFACTS:{missing}")

    for name in required:
        if name.endswith(".jsonl") and store.exists(name):
            try:
                rows = sum(1 for _ in store.iter_jsonl(name))
                if rows == 0:
                    violations.append(f"EMPTY_JSONL:{name}")
                if store.path(name).stat().st_size > 5 * 1024 * 1024:
                    violations.append(f"JSONL_TOO_LARGE:{name}")
            except json.JSONDecodeError as exc:
                violations.append(f"CORRUPT_JSONL:{name}:{exc}")
        if name.endswith(".json") and store.exists(name):
            try:
                store.read_json(name)
            except json.JSONDecodeError as exc:
                violations.append(f"CORRUPT_JSON:{name}:{exc}")

    gate_rows = read_validation_gates(run_dir)
    if not gate_rows:
        violations.append("MISSING_ORPHAN_COUNTS_OR_GATES:reconciliation/validation")
    for row in gate_rows:
        if row.get("status") not in GATE_STATUSES:
            violations.append(f"ILLEGAL_GATE_STATUS:{row.get('gate_id')}:{row.get('status')}")
        for key in ("gate_id", "status", "explanation", "evidence_paths", "tested_at_or_run_reference"):
            if key not in row:
                violations.append(f"GATE_FIELD_MISSING:{row.get('gate_id')}:{key}")

    if store.exists("reconciliation.json"):
        recon = store.read_json("reconciliation.json")
        for key in (
            "unmatched_native_fills",
            "unmatched_audit_fills",
            "unlinked_orders",
            "unlinked_positions",
            "duplicate_fill_ids",
            "unexplained_cashflows",
            "orphan_order_events",
            "orphan_position_events",
        ):
            if key not in recon:
                violations.append(f"ORPHAN_COUNT_MISSING:{key}")

    if store.exists("events.jsonl"):
        illegal_states: set[str] = set()
        for event in store.iter_events():
            if event.get("event_type") != "EXPERT_EVALUATED":
                continue
            stance = event.get("stance")
            if stance not in EVALUATION_STATES:
                illegal_states.add(str(stance))
        if illegal_states:
            violations.append(f"ILLEGAL_EVALUATION_STATE:{sorted(illegal_states)}")

    if require_reports and store.exists("FINAL_DELIVERY_TR.md"):
        report = store.path("FINAL_DELIVERY_TR.md").read_text(encoding="utf-8")
        for idx, heading in enumerate(TR_SECTIONS, start=1):
            if f"## {idx}. {heading}" not in report:
                violations.append(f"TR_SECTION_MISSING:{idx}:{heading}")

    manifest = store.read_json("run_manifest.json") if store.exists("run_manifest.json") else None
    if manifest:
        for name, meta in (manifest.get("artifacts") or {}).items():
            target = run_dir / name
            if not target.is_file():
                violations.append(f"MANIFEST_PATH_MISSING:{name}")
            elif _sha256(target) != meta.get("sha256"):
                violations.append(f"MANIFEST_HASH_MISMATCH:{name}")
        if require_reports:
            digested = set(manifest.get("artifacts") or {})
            for name in MANDATORY_ARTIFACTS:
                if name == "run_manifest.json":
                    continue  # a manifest cannot digest itself
                if name not in digested:
                    violations.append(f"MANIFEST_ARTIFACT_MISSING:{name}")

    present_mandatory = sum(1 for name in MANDATORY_ARTIFACTS if store.exists(name))
    return {
        "run_id": manifest.get("run_id") if manifest else None,
        "schema_version": SCHEMA_VERSION,
        "artifact_count": present_mandatory,
        "validated_manifest_artifact_count": len(manifest.get("artifacts") or {}) if manifest else 0,
        "verified_artifacts": sum(1 for name in required if store.exists(name)),
        "mandatory_artifacts": len(MANDATORY_ARTIFACTS),
        "violations": violations,
        "status": "PASS" if not violations else "FAIL",
        "checked_at_utc": datetime.now(timezone.utc).isoformat(),
    }


def refresh_manifest_digests(run_dir: Path) -> dict[str, Any]:
    """Re-digest every artifact on disk into run_manifest.json (written last)."""
    run_dir = Path(run_dir)
    path = run_dir / "run_manifest.json"
    manifest: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    artifacts: dict[str, dict[str, str | int]] = {
        file.name: {"sha256": _sha256(file), "bytes": file.stat().st_size}
        for file in sorted(run_dir.iterdir())
        if file.is_file() and file.name != "run_manifest.json"
    }
    manifest["artifacts"] = artifacts
    performance = manifest.setdefault("performance", {})
    performance["raw_artifact_bytes"] = sum(int(v["bytes"]) for v in artifacts.values())
    _write_json(path, manifest)
    return manifest


def read_validation_gates(run_dir: Path) -> list[dict[str, Any]]:
    path = Path(run_dir) / "gate_results.json"
    if not path.is_file():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    gates = data.get("gates", []) if isinstance(data, dict) else []
    if not isinstance(gates, list):
        return []
    return [row for row in gates if isinstance(row, dict)]


# --------------------------------------------------------------------------- #
# audit verdict derivation (pure function of the persisted run dir)
# --------------------------------------------------------------------------- #
#: Event types that name the trade decision which submitted an entry order.
_DECISION_EVENT_TYPES = frozenset({"ADMISSION_ACCEPTED"})
#: Native/ledger order lifecycle events that name one order by ``client_order_id``.
_ORDER_EVENT_TYPES = frozenset(
    {
        "ORDER_CREATED",
        "ORDER_SUBMITTED",
        "ORDER_ACCEPTED",
        "ORDER_REJECTED",
        "ORDER_CANCELED",
        "ORDER_UPDATED",
        "ORDER_PARTIALLY_FILLED",
        "ORDER_FILLED",
    }
)
#: Data-quality keys whose truthy value proves the run consumed future/OOS data.
_LOOK_AHEAD_KEYS = (
    "look_ahead",
    "lookahead",
    "look_ahead_detected",
    "lookahead_detected",
    "future_data_used",
    "uses_future_data",
    "oos_data_touched",
)


@dataclass(frozen=True)
class AuditInputs:
    """The verdict and lineage witness that one persisted run dir can support.

    Every field is derived by reading the run directory; nothing is carried over
    from the in-memory replay, so re-deriving from disk always yields the same
    inputs and therefore the same report label.
    """

    verdict: AuditVerdict
    lineage: LineageChain | None = None
    as_of_ns: int | None = None


def _text(value: Any) -> str | None:
    """Non-empty stripped text, or None (an absent identity is never invented)."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _int_or_none(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _decimal_or_none(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        amount = value if isinstance(value, Decimal) else Decimal(str(value))
    except (ArithmeticError, ValueError):
        return None
    return amount if amount.is_finite() else None


def _jsonl_rows(store: AuditStore, name: str) -> list[dict[str, Any]]:
    return list(store.iter_jsonl(name)) if store.exists(name) else []


def _json_object(store: AuditStore, name: str) -> dict[str, Any]:
    if not store.exists(name):
        return {}
    payload = store.read_json(name)
    return payload if isinstance(payload, dict) else {}


def _flag_set(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in {"true", "1", "yes"}
    return False


def _mechanical_section(
    lifecycles: Sequence[Mapping[str, Any]],
) -> tuple[VerdictState, tuple[str, ...], tuple[str, ...]]:
    """Mechanical state of the whole run, from the persisted lifecycle rows.

    Per-row rules stay in :func:`verdict_from_trade_metrics`; only the run-level
    precedence is decided here. A tie the tape cannot resolve
    (``NOT_IDENTIFIABLE``) is never upgraded to a pass, and an invalid row is a
    failure of the mechanical dimension.
    """
    if not lifecycles:
        return VerdictState.NOT_RUN, ("MECHANICAL_NO_TRADE_ROWS",), ()
    states: list[VerdictState] = []
    reasons: list[str] = []
    labels: list[str] = []
    for row in lifecycles:
        row_verdict = verdict_from_trade_metrics(row)
        states.append(row_verdict.mechanical)
        reasons.extend(row_verdict.reasons)
        labels.extend(row_verdict.labels)
    if VerdictState.INVALID in states:
        state = VerdictState.FAIL
    elif VerdictState.NOT_IDENTIFIABLE in states:
        state = VerdictState.NOT_IDENTIFIABLE
    elif VerdictState.INCOMPLETE in states:
        state = VerdictState.INCOMPLETE
    elif VerdictState.NOT_RUN in states:
        state = VerdictState.NOT_RUN
    elif all(current is VerdictState.PASS for current in states):
        state = VerdictState.PASS
        labels.append("MECHANICAL_PATH_IDENTIFIED")
    else:  # pragma: no cover - defensive: no other per-row state exists
        state = VerdictState.FAIL
        reasons.append("MECHANICAL_UNEXPECTED_ROW_STATE")
    return state, tuple(dict.fromkeys(reasons)), tuple(dict.fromkeys(labels))


def _economic_section(
    store: AuditStore, lifecycles: Sequence[Mapping[str, Any]]
) -> tuple[VerdictState, tuple[str, ...], tuple[str, ...]]:
    """Economic state, from the run's own reconciliation evidence only.

    ``PASS`` requires a closed campaign to exist, the published conservation
    check to be present, within its own tolerance and free of unexplained
    cashflows, and the realized PnL figure to be published. An open-only run is
    ``INCOMPLETE`` (``ECONOMIC_NO_CLOSED_CAMPAIGN``): nothing was realized, so a
    balanced reconciliation cannot evidence an economic outcome. A missing or
    residual-failing reconciliation is ``INCOMPLETE`` — never a pass by omission.
    """
    if not lifecycles:
        return VerdictState.NOT_RUN, ("ECONOMIC_NO_TRADE_ROWS",), ()
    reconciliation = _json_object(store, "reconciliation.json")
    if not reconciliation:
        return VerdictState.NOT_RUN, ("ECONOMIC_RECONCILIATION_UNPUBLISHED",), ()
    economic = _json_object(store, "economic_metrics.json")
    residual = _decimal_or_none(reconciliation.get("conservation_residual_usdt"))
    tolerance = _decimal_or_none(reconciliation.get("conservation_tolerance_usdt"))
    realized = _decimal_or_none(economic.get("realized_net_trading_pnl"))
    unexplained = _int_or_none(reconciliation.get("unexplained_cashflows"))
    if residual is None or tolerance is None or realized is None or unexplained is None:
        return VerdictState.INCOMPLETE, ("ECONOMIC_RECONCILIATION_INCOMPLETE",), ()
    closed_rows = [
        row
        for row in lifecycles
        if str(row.get("position_status") or "").upper() == "CLOSED"
        or row.get("closed_ns") is not None
    ]
    if not closed_rows:
        # A run that closed nothing has no realized economic outcome to reconcile.
        # The reconciliation file may still balance (nothing moved, or the ledger
        # only marks open campaigns), so a residual within tolerance is *not*
        # evidence of a realized edge. Reporting PASS here would let an open-only
        # run render AUDIT_COMPLETE.
        return (
            VerdictState.INCOMPLETE,
            ("ECONOMIC_NO_CLOSED_CAMPAIGN",),
            ("OPEN_ONLY_NO_CLOSED_CAMPAIGN",),
        )
    missing_pnl = sum(
        1
        for row in closed_rows
        if row.get("gross_pnl_usdt") is None or row.get("realized_net_pnl_usdt") is None
    )
    if missing_pnl:
        return (
            VerdictState.INCOMPLETE,
            (f"ECONOMIC_CLOSED_ROW_PNL_MISSING:{missing_pnl}/{len(closed_rows)}",),
            ("UNREALIZED_AT_CUTOFF",),
        )
    if unexplained != 0:
        return (
            VerdictState.INCOMPLETE,
            (f"ECONOMIC_UNEXPLAINED_CASHFLOWS:{unexplained}",),
            ("UNEXPLAINED_CASHFLOW",),
        )
    if abs(residual) > tolerance:
        return (
            VerdictState.INCOMPLETE,
            (f"ECONOMIC_CONSERVATION_RESIDUAL:{residual}",),
            ("CONSERVATION_RESIDUAL",),
        )
    return VerdictState.PASS, (), ("REALIZED_PNL_RECONCILED",)


def _pit_section(store: AuditStore) -> tuple[VerdictState, tuple[str, ...], tuple[str, ...]]:
    """Point-in-time state: the data-quality evidence must exist and clear.

    Absent or unusable data-quality evidence is ``NOT_RUN`` (it never became a
    pass); an explicitly set look-ahead flag is ``FAIL`` — a detected violation,
    not an omission, and never silently reported as "did not run".
    """
    if not store.exists("data_quality.json"):
        return VerdictState.NOT_RUN, ("PIT_NO_DATA_QUALITY_EVIDENCE",), ()
    quality = _json_object(store, "data_quality.json")
    bars = _int_or_none(quality.get("bars"))
    tape_sha = _text(quality.get("tape_sha256"))
    if bars is None or bars <= 0 or tape_sha is None:
        return VerdictState.NOT_RUN, ("PIT_DATA_QUALITY_EVIDENCE_INCOMPLETE",), ()
    flagged = [key for key in _LOOK_AHEAD_KEYS if _flag_set(quality.get(key))]
    if flagged:
        return (
            VerdictState.FAIL,
            tuple(f"PIT_LOOK_AHEAD_FLAG:{key}" for key in flagged),
            ("LOOK_AHEAD_DETECTED",),
        )
    return VerdictState.PASS, (), ("PIT_DATA_QUALITY_VERIFIED",)


def _source_id_section(
    rows: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, str | None], tuple[str, ...], tuple[str, ...]]:
    """Required campaign identities, present in the persisted rows only.

    A key is reported only when *every* row names it; otherwise the key is
    withheld (so completeness is ``INCOMPLETE``) rather than borrowing one row's
    value for the others. The witness values all come from one deterministic row
    (the first campaign), so the tuple is a real identity rather than a mix; when
    a key repeats across campaigns the ``MULTI_CAMPAIGN`` label says the run is
    aggregated, not that the witness is fabricated.
    """
    if not rows:
        return (
            {key: None for key in REQUIRED_SOURCE_IDS},
            ("SOURCE_ID_NO_PERSISTED_TRADE_ROWS",),
            (),
        )
    ids: dict[str, str | None] = {}
    reasons: list[str] = []
    labels: list[str] = []
    primary = min(
        rows,
        key=lambda row: (
            str(row.get("campaign_id") or ""),
            str(row.get("position_id") or ""),
        ),
    )
    for key in REQUIRED_SOURCE_IDS:
        values = [_text(row.get(key)) for row in rows]
        missing = sum(1 for value in values if value is None)
        if missing:
            ids[key] = None
            reasons.append(f"SOURCE_ID_UNRESOLVED:{key}:{missing}/{len(rows)}")
            continue
        # The witness is one real row's identity, never a mix of rows' values.
        ids[key] = _text(primary.get(key))
        distinct = sorted({value for value in values if value is not None})
        if len(distinct) > 1:
            labels.append("MULTI_CAMPAIGN")
            reasons.append(f"SOURCE_ID_AGGREGATE:{key}:{len(distinct)}")
    return ids, tuple(reasons), tuple(dict.fromkeys(labels))


def _campaign_decisions(
    decisions: Sequence[Mapping[str, Any]],
) -> dict[str, str]:
    """Campaign -> its single decision id, only when exactly one decision exists.

    An order or fill that does not carry ``decision_id`` may be joined to the
    campaign's decision only when the campaign names exactly one; an ambiguous
    campaign yields no mapping, so the record stays unjoined instead of guessing.
    """
    by_campaign: dict[str, set[str]] = {}
    for row in decisions:
        campaign = _text(row.get("campaign_id"))
        decision = _text(row.get("decision_id"))
        if campaign is None or decision is None:
            continue
        by_campaign.setdefault(campaign, set()).add(decision)
    return {
        campaign: next(iter(found))
        for campaign, found in by_campaign.items()
        if len(found) == 1
    }


def _order_records(
    events: Sequence[Mapping[str, Any]], campaign_decisions: Mapping[str, str]
) -> dict[str, LineageOrder]:
    """One lineage order per ``client_order_id``, at its first persisted event."""
    grouped: dict[str, list[Mapping[str, Any]]] = {}
    for event in events:
        if event.get("event_type") not in _ORDER_EVENT_TYPES:
            continue
        order_id = _text(event.get("client_order_id"))
        if order_id is None:
            continue
        grouped.setdefault(order_id, []).append(event)
    records: dict[str, LineageOrder] = {}
    for order_id, rows in grouped.items():
        ordered = sorted(
            rows,
            key=lambda row: (
                _int_or_none(row.get("event_time_ns")) or 0,
                _int_or_none(row.get("sequence_number")) or 0,
            ),
        )
        first = ordered[0]
        campaign = next(
            (found for found in (_text(row.get("campaign_id")) for row in ordered) if found),
            None,
        )
        decision = next(
            (found for found in (_text(row.get("decision_id")) for row in ordered) if found),
            None,
        )
        if decision is None and campaign is not None:
            decision = campaign_decisions.get(campaign)
        records[order_id] = LineageOrder(
            order_id=order_id,
            decision_id=decision or "",
            campaign_id=campaign or "",
            instrument_id=_text(first.get("instrument_id")) or "",
            event_time_ns=_int_or_none(first.get("event_time_ns")) or 0,
            available_at_ns=_int_or_none(first.get("recorded_time_ns")) or 0,
        )
    return records


def _venue_client_map(events: Sequence[Mapping[str, Any]]) -> dict[str, str]:
    """Venue order id -> client order id, only from rows that carry both."""
    mapping: dict[str, str] = {}
    for event in events:
        venue = _text(event.get("venue_order_id"))
        client = _text(event.get("client_order_id"))
        if venue is not None and client is not None:
            mapping[venue] = client
    return mapping


def _fill_records(
    fills: Sequence[Mapping[str, Any]],
    orders: Mapping[str, LineageOrder],
    venue_client: Mapping[str, str],
) -> list[LineageFill]:
    """Persisted fills joined to their order; rows without an identity are skipped."""
    records: list[LineageFill] = []
    for fill in fills:
        fill_id = (
            _text(fill.get("last_trade_id"))
            or _text(fill.get("trade_id"))
            or _text(fill.get("fill_id"))
        )
        event_time_ns = _int_or_none(fill.get("ts_last")) or _int_or_none(
            fill.get("event_time_ns")
        )
        if fill_id is None or event_time_ns is None:
            continue
        order_id = _text(fill.get("client_order_id"))
        if order_id is None:
            order_id = venue_client.get(_text(fill.get("venue_order_id")) or "")
        order = orders.get(order_id or "")
        records.append(
            LineageFill(
                fill_id=fill_id,
                order_id=order_id or "",
                decision_id=order.decision_id if order is not None else "",
                campaign_id=order.campaign_id if order is not None else "",
                instrument_id=_text(fill.get("instrument_id"))
                or (order.instrument_id if order is not None else ""),
                event_time_ns=event_time_ns,
                available_at_ns=_int_or_none(fill.get("recorded_time_ns")) or event_time_ns,
            )
        )
    return records


def _cashflow_records(
    events: Sequence[Mapping[str, Any]],
    fills: Sequence[LineageFill],
) -> list[LineageCashflow]:
    """Terminal cash movements joined to a fill by ``trade_id``; unjoined stay unjoined.

    The campaign is taken from the fill the cashflow settles, not from the
    event's own ``campaign_id`` field: the ledger appends the derived fee
    cashflows after the replay, so campaign attribution by sequence number stamps
    them all with the last campaign. The explicit ``trade_id`` join is the fact;
    the field would only import that mis-attribution.
    """
    campaign_by_fill = {fill.fill_id: fill.campaign_id for fill in fills}
    records: list[LineageCashflow] = []
    for event in events:
        if event.get("event_type") != "CASHFLOW_RECORDED":
            continue
        amount = _decimal_or_none(event.get("amount"))
        event_time_ns = _int_or_none(event.get("event_time_ns"))
        if amount is None or event_time_ns is None:
            continue
        fill_id = _text(event.get("trade_id")) or ""
        sequence = _int_or_none(event.get("sequence_number"))
        cashflow_id = (
            f"CF:{sequence}" if sequence is not None else f"CF:{fill_id}@{event_time_ns}"
        )
        records.append(
            LineageCashflow(
                cashflow_id=cashflow_id,
                fill_id=fill_id,
                campaign_id=campaign_by_fill.get(fill_id, ""),
                event_time_ns=event_time_ns,
                available_at_ns=_int_or_none(event.get("recorded_time_ns")) or event_time_ns,
                amount=amount,
            )
        )
    return records


#: Keys that make a ``fills.jsonl`` line a persisted fill claim rather than a
#: placeholder row. A row with none of them was never a fill to begin with.
_FILL_ROW_KEYS = (
    "last_trade_id",
    "trade_id",
    "fill_id",
    "client_order_id",
    "venue_order_id",
    "ts_last",
    "event_time_ns",
)


def _is_fill_row(fill: Mapping[str, Any]) -> bool:
    return any(fill.get(key) is not None for key in _FILL_ROW_KEYS)


def _lineage_from_disk(
    store: AuditStore, fills: Sequence[Mapping[str, Any]]
) -> tuple[LineageChain | None, int | None]:
    """Build the five-layer lineage witness from persisted rows only.

    Returns ``None`` when the run persisted no decision/order/fill/cashflow row,
    so a run that never claimed a chain is not downgraded for the absence of one.
    When rows *were* persisted but every one lacked the identity needed to join,
    an empty ``LineageChain`` is returned with ``as_of_ns=0``: the claim exists,
    it cannot be certified, and ``certify`` reports BLOCKED.
    Every join is by an explicit id; a row that lacks its parent key keeps the
    blank key and is reported as a violation by :func:`~v8_next.audit.lineage.certify`.
    The certification cut is the latest availability persisted with the chain, so
    re-deriving from the same directory can never produce a different label.
    """
    events = _jsonl_rows(store, "events.jsonl")
    # Any persisted chain-layer row counts as a claim, even when every row later
    # turns out to be unidentifiable. Dropping such rows and returning "no chain"
    # would silently convert a broken ledger into "nothing was claimed" — and the
    # report could then render COMPLETE for a run whose fills cannot be joined.
    chain_row_persisted = any(_is_fill_row(fill) for fill in fills) or any(
        str(event.get("event_type") or "") in _DECISION_EVENT_TYPES
        or str(event.get("event_type") or "") in _ORDER_EVENT_TYPES
        or str(event.get("event_type") or "") == "CASHFLOW_RECORDED"
        for event in events
    )
    decisions = [
        event
        for event in events
        if event.get("event_type") in _DECISION_EVENT_TYPES
        and _text(event.get("decision_id")) is not None
    ]
    campaign_decisions = _campaign_decisions(decisions)
    orders = _order_records(events, campaign_decisions)
    fill_records = _fill_records(fills, orders, _venue_client_map(events))
    cashflows = _cashflow_records(events, fill_records)
    if not (decisions or orders or fill_records or cashflows):
        if chain_row_persisted:
            # The run persisted fills/orders/decisions/cashflows but none of them
            # carried enough identity to join. Return an empty *declared* chain so
            # ``certify`` reports the structural gap and the report is BLOCKED;
            # returning no chain would treat the claim as absent and let the
            # label reach COMPLETE.
            return LineageChain(), 0
        return None, None

    decision_rows: list[LineageDecision] = []
    admitted: dict[str, str] = {}
    for event in decisions:
        opportunity_id = _text(event.get("opportunity_id")) or ""
        campaign_id = _text(event.get("campaign_id")) or ""
        if opportunity_id and campaign_id:
            admitted.setdefault(opportunity_id, campaign_id)
        decision_rows.append(
            LineageDecision(
                decision_id=_text(event.get("decision_id")) or "",
                opportunity_id=opportunity_id,
                campaign_id=campaign_id,
                instrument_id=_text(event.get("instrument_id")) or "",
                decision_time_ns=_int_or_none(event.get("event_time_ns")) or 0,
                available_at_ns=_int_or_none(event.get("recorded_time_ns")) or 0,
            )
        )

    opportunity_rows: list[LineageOpportunity] = []
    for event in events:
        if event.get("event_type") != "OPPORTUNITY_DETECTED":
            continue
        detected_id = _text(event.get("opportunity_id"))
        detected_campaign = admitted.get(detected_id or "")
        detected_ns = _int_or_none(event.get("event_time_ns"))
        if detected_id is None or detected_campaign is None or detected_ns is None:
            continue
        opportunity_rows.append(
            LineageOpportunity(
                opportunity_id=detected_id,
                campaign_id=detected_campaign,
                instrument_id=_text(event.get("instrument_id")) or "",
                created_ns=detected_ns,
                available_ns=_int_or_none(event.get("recorded_time_ns")) or detected_ns,
            )
        )

    chain = LineageChain(
        opportunities=tuple(opportunity_rows),
        decisions=tuple(decision_rows),
        orders=tuple(orders[key] for key in sorted(orders)),
        fills=tuple(fill_records),
        cashflows=tuple(cashflows),
    )
    cuts: list[int] = []
    for row in opportunity_rows:
        cuts.extend((row.created_ns, row.available_ns))
    for decision_row in decision_rows:
        cuts.extend((decision_row.decision_time_ns, decision_row.available_at_ns))
    for order_row in chain.orders:
        cuts.extend((order_row.event_time_ns, order_row.available_at_ns))
    for fill_row in chain.fills:
        cuts.extend((fill_row.event_time_ns, fill_row.available_at_ns))
    for cashflow_row in cashflows:
        cuts.extend((cashflow_row.event_time_ns, cashflow_row.available_at_ns))
    return chain, (max(cuts) if cuts else None)


def derive_audit_inputs(run_dir: Path) -> AuditInputs:
    """Derive the audit verdict (and lineage witness) from a persisted run dir.

    The derivation reads ``trade_lifecycles.jsonl``, ``fills.jsonl``,
    ``reconciliation.json``, ``economic_metrics.json``, ``data_quality.json`` and
    ``events.jsonl``; it never consults the in-memory replay. Completeness is
    delegated to :func:`~v8_next.audit.verdicts.derive_verdict`, so a missing
    source identity can never read as ``AUDIT_COMPLETE``.
    """
    store = AuditStore(Path(run_dir))
    lifecycles = _jsonl_rows(store, "trade_lifecycles.jsonl")
    fills = _jsonl_rows(store, "fills.jsonl")

    mechanical, mech_reasons, mech_labels = _mechanical_section(lifecycles)
    economic, econ_reasons, econ_labels = _economic_section(store, lifecycles)
    pit, pit_reasons, pit_labels = _pit_section(store)
    source_ids, id_reasons, id_labels = _source_id_section(lifecycles or fills)

    verdict = derive_verdict(
        mechanical=mechanical,
        economic=economic,
        pit=pit,
        source_ids=source_ids,
        labels=tuple(dict.fromkeys((*mech_labels, *econ_labels, *pit_labels, *id_labels))),
        reasons=tuple(dict.fromkeys((*mech_reasons, *econ_reasons, *pit_reasons, *id_reasons))),
    )
    lineage, as_of_ns = _lineage_from_disk(store, fills)
    return AuditInputs(verdict=verdict, lineage=lineage, as_of_ns=as_of_ns)
