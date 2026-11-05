"""Native identity linkage: decision -> order -> fill -> cashflow and campaigns.

The audit wiring derives its verdict from persisted rows, so a native replay can
only certify when the producers wrote the identities down. These tests pin the
producer side of that contract:

* campaign ``C0001`` starts at the first ``POSITION_OPENED`` (no campaign 0) and
  the entry leg — emitted *before* the open — is attributed forward by its
  decision, not by a client-id suffix or a timestamp coincidence;
* every order and fill carries the decision that originated it, and a row that
  lost its identity stays unattributed (``MISSING_JOIN``) instead of borrowing
  the campaign's only decision;
* campaign ids are a stable, contiguous function of the persisted event order.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from v8_next.audit.lineage import CompletenessVerdict, certify
from v8_next.audit.pipeline import (
    assign_campaign_ids,
    attach_fill_identities,
    derive_audit_inputs,
)
from v8_next.audit.reporting import REPORT_STATUS_COMPLETE, resolve_report_status
from v8_next.audit.verdicts import VerdictState, audit_label

INSTRUMENT = "BTCUSDT-PERP.BINANCE"
COMPLETE = "AUDIT_COMPLETE"


def _event(sequence: int, event_type: str, **fields: Any) -> dict[str, Any]:
    return {
        "schema_version": "AUDIT_EVENT_V1",
        "run_id": "ledger-linkage-fixture",
        "sequence_number": sequence,
        "event_type": event_type,
        "instrument_id": INSTRUMENT,
        "event_time_ns": fields.pop("event_time_ns", 1000),
        "recorded_time_ns": fields.pop("recorded_time_ns", 1100),
        "source_component": "test/ledger-linkage",
        "evidence_class": "OBSERVED_SIMULATION",
        **fields,
    }


def _lifecycle(**overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "position_id": "P-1#1",
        "campaign_id": "C0001",
        "instrument_id": INSTRUMENT,
        "opportunity_id": "OPP-1",
        "decision_id": "D1000",
        "direction": "LONG",
        "side": "LONG",
        "quantity": 1.0,
        "entry_price": 100.0,
        "exit_price": 105.0,
        "opened_ns": 1000,
        "closed_ns": 2000,
        "mfe_price": 6.0,
        "mae_price": -1.0,
        "mfe_r": 3.0,
        "mae_r": -0.5,
        "same_bar_stop_and_target": False,
        "path_model_applied": "STOP_FIRST",
        "path_model_outcome": "STOP_FIRST",
        "risk_denominator_status": "VALID",
        "initial_risk_price": 2.0,
        "gross_pnl_usdt": 5.0,
        "realized_net_pnl_usdt": 4.5,
        "realized_net_r": 2.25,
        "protection_evidence": "NATIVE_ORDER_PRESENT",
        "position_status": "CLOSED",
        "exit_kind": "STOP",
        "fees_usdt": 0.5,
    }
    row.update(overrides)
    return row


def _identified_events() -> list[dict[str, Any]]:
    """One fully identified campaign: decision, entry/exit orders, fills, cashflows."""
    return [
        _event(
            1,
            "OPPORTUNITY_DETECTED",
            opportunity_id="OPP-1",
            event_time_ns=900,
            recorded_time_ns=910,
        ),
        _event(
            2,
            "ADMISSION_ACCEPTED",
            decision_id="D1000",
            opportunity_id="OPP-1",
            event_time_ns=1000,
            recorded_time_ns=1010,
        ),
        # The entry leg is emitted before POSITION_OPENED. Its campaign must come
        # from its decision, never from the sequence interval.
        _event(
            3,
            "ORDER_SUBMITTED",
            decision_id="D1000",
            opportunity_id="OPP-1",
            client_order_id="O-ENTRY",
            venue_order_id="V-ENTRY",
            event_time_ns=1000,
            recorded_time_ns=1020,
        ),
        _event(
            4,
            "ORDER_ACCEPTED",
            decision_id="D1000",
            opportunity_id="OPP-1",
            client_order_id="O-ENTRY",
            venue_order_id="V-ENTRY",
            event_time_ns=1000,
            recorded_time_ns=1030,
        ),
        _event(
            5,
            "POSITION_OPENED",
            decision_id="D1000",
            opportunity_id="OPP-1",
            opening_order_id="O-ENTRY",
            position_id="P-1",
            event_time_ns=1000,
            recorded_time_ns=1040,
        ),
        _event(
            6,
            "ORDER_FILLED",
            decision_id="D1000",
            opportunity_id="OPP-1",
            client_order_id="O-ENTRY",
            venue_order_id="V-ENTRY",
            event_time_ns=1000,
            recorded_time_ns=1050,
        ),
        _event(
            7,
            "ORDER_ACCEPTED",
            decision_id="D1000",
            opportunity_id="OPP-1",
            client_order_id="O-STOP",
            venue_order_id="V-STOP",
            event_time_ns=1000,
            recorded_time_ns=1060,
        ),
        _event(
            8,
            "ORDER_FILLED",
            decision_id="D1000",
            opportunity_id="OPP-1",
            client_order_id="O-STOP",
            venue_order_id="V-STOP",
            event_time_ns=2000,
            recorded_time_ns=2010,
        ),
        _event(
            9,
            "POSITION_CLOSED",
            position_id="P-1",
            event_time_ns=2000,
            recorded_time_ns=2020,
        ),
        _event(
            10,
            "CASHFLOW_RECORDED",
            trade_id="F-1",
            client_order_id="V-ENTRY",
            cashflow_type="COMMISSION",
            amount="-0.5",
            event_time_ns=1000,
            recorded_time_ns=1060,
        ),
        _event(
            11,
            "CASHFLOW_RECORDED",
            trade_id="F-2",
            client_order_id="V-STOP",
            cashflow_type="COMMISSION",
            amount="-0.5",
            event_time_ns=2000,
            recorded_time_ns=2020,
        ),
    ]


def _identified_fills() -> list[dict[str, Any]]:
    return [
        {
            "last_trade_id": "F-1",
            "client_order_id": "O-ENTRY",
            "venue_order_id": "V-ENTRY",
            "ts_last": 1000,
            "instrument_id": INSTRUMENT,
        },
        {
            "last_trade_id": "F-2",
            "client_order_id": "O-STOP",
            "venue_order_id": "V-STOP",
            "ts_last": 2000,
            "instrument_id": INSTRUMENT,
        },
    ]


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows), encoding="utf-8"
    )


def _seed_run(
    run_dir: Path,
    *,
    events: list[dict[str, Any]] | None = None,
    fills: list[dict[str, Any]] | None = None,
    attribute: bool = True,
) -> Path:
    """Persist the artifacts the verdict derivation reads, in pipeline order."""
    run_dir.mkdir(parents=True, exist_ok=True)
    events = _identified_events() if events is None else events
    _write_jsonl(run_dir / "events.jsonl", events)
    assign_campaign_ids(run_dir)
    persisted_events = [
        json.loads(line) for line in (run_dir / "events.jsonl").read_text().splitlines()
    ]
    fill_rows = _identified_fills() if fills is None else fills
    if attribute:
        fill_rows = attach_fill_identities(fill_rows, persisted_events)
    _write_jsonl(run_dir / "fills.jsonl", fill_rows)
    _write_jsonl(run_dir / "trade_lifecycles.jsonl", [_lifecycle()])
    (run_dir / "reconciliation.json").write_text(
        json.dumps(
            {
                "conservation_residual_usdt": "0.0",
                "conservation_tolerance_usdt": "0.01",
                "unexplained_cashflows": 0,
            }
        ),
        encoding="utf-8",
    )
    (run_dir / "economic_metrics.json").write_text(
        json.dumps({"realized_net_trading_pnl": 4.5}), encoding="utf-8"
    )
    (run_dir / "data_quality.json").write_text(
        json.dumps({"bars": 2, "tape_sha256": "a" * 64, "oos_data_touched": False}),
        encoding="utf-8",
    )
    (run_dir / "gate_results.json").write_text(
        json.dumps(
            {
                "gates": [
                    {"gate_id": f"G{index}", "status": "PASS", "explanation": "fixture"}
                    for index in range(1, 13)
                ]
            }
        ),
        encoding="utf-8",
    )
    (run_dir / "validation_results.json").write_text(
        json.dumps({"status": "PASS", "violations": []}), encoding="utf-8"
    )
    return run_dir


def _status(run_dir: Path) -> str:
    inputs = derive_audit_inputs(run_dir)
    certification = (
        certify(inputs.lineage, as_of_ns=inputs.as_of_ns or 0)
        if inputs.lineage is not None
        else None
    )
    return resolve_report_status(
        verdict=inputs.verdict,
        lineage=certification,
        gates=[{"gate_id": "G1", "status": "PASS"}],
        validation={"status": "PASS"},
    )


# --------------------------------------------------------------------------- #
# A. full identity certifies the run
# --------------------------------------------------------------------------- #
def test_fully_identified_run_is_audit_complete(tmp_path: Path) -> None:
    run_dir = _seed_run(tmp_path)
    inputs = derive_audit_inputs(run_dir)

    assert inputs.verdict.completeness is VerdictState.PASS
    assert audit_label(inputs.verdict) == COMPLETE
    assert inputs.lineage is not None
    certification = certify(inputs.lineage, as_of_ns=inputs.as_of_ns or 0)
    assert certification.verdict is CompletenessVerdict.COMPLETE, certification.violations
    assert resolve_report_status(
        verdict=inputs.verdict,
        lineage=certification,
        gates=[{"gate_id": "G1", "status": "PASS"}],
        validation={"status": "PASS"},
    ) == REPORT_STATUS_COMPLETE


def test_every_order_and_fill_carries_its_decision(tmp_path: Path) -> None:
    run_dir = _seed_run(tmp_path)
    events = [json.loads(line) for line in (run_dir / "events.jsonl").read_text().splitlines()]
    order_rows = [
        event
        for event in events
        if event["event_type"].startswith("ORDER_")
        and event.get("client_order_id") is not None
    ]
    assert order_rows, "fixture must contain persisted order rows"
    for row in order_rows:
        assert row.get("decision_id") == "D1000", row
        assert row.get("campaign_id") == "C0001", row

    fills = [json.loads(line) for line in (run_dir / "fills.jsonl").read_text().splitlines()]
    for fill in fills:
        assert fill.get("decision_id") == "D1000", fill
        assert fill.get("campaign_id") == "C0001", fill


# --------------------------------------------------------------------------- #
# B. a lost identity downgrades the report and names the row
# --------------------------------------------------------------------------- #
def test_one_order_without_decision_id_is_incomplete_and_named(tmp_path: Path) -> None:
    events = _identified_events()
    for event in events:
        if event.get("client_order_id") == "O-STOP":
            event.pop("decision_id", None)
    run_dir = _seed_run(tmp_path, events=events)

    inputs = derive_audit_inputs(run_dir)
    assert inputs.lineage is not None
    certification = certify(inputs.lineage, as_of_ns=inputs.as_of_ns or 0)
    assert not certification.complete
    named = [
        violation
        for violation in certification.violations
        if violation.record_id == "O-STOP" and "decision_id" in violation.detail
    ]
    assert named, certification.violations
    assert _status(run_dir) != COMPLETE


# --------------------------------------------------------------------------- #
# C. campaign numbering is forward-attributed, contiguous and stable
# --------------------------------------------------------------------------- #
def _two_campaign_events() -> list[dict[str, Any]]:
    return [
        _event(1, "ADMISSION_ACCEPTED", decision_id="D1000", opportunity_id="OPP-1"),
        _event(2, "ORDER_SUBMITTED", decision_id="D1000", client_order_id="O-1"),
        _event(
            3,
            "POSITION_OPENED",
            decision_id="D1000",
            opening_order_id="O-1",
            position_id="P-1",
        ),
        _event(4, "ORDER_FILLED", decision_id="D1000", client_order_id="O-1"),
        _event(5, "ORDER_CANCELED", decision_id="D1000", client_order_id="O-1"),
        _event(
            6,
            "ADMISSION_ACCEPTED",
            decision_id="D2000",
            opportunity_id="OPP-2",
            event_time_ns=2000,
            recorded_time_ns=2010,
        ),
        _event(
            7,
            "ORDER_SUBMITTED",
            decision_id="D2000",
            client_order_id="O-2",
            event_time_ns=2000,
            recorded_time_ns=2020,
        ),
        _event(
            8,
            "POSITION_OPENED",
            decision_id="D2000",
            opening_order_id="O-2",
            position_id="P-2",
            event_time_ns=2000,
            recorded_time_ns=2030,
        ),
        _event(
            9,
            "ORDER_FILLED",
            decision_id="D2000",
            client_order_id="O-2",
            event_time_ns=2000,
            recorded_time_ns=2040,
        ),
    ]


def test_campaign_ids_start_at_c0001_and_attribute_the_entry_leg(tmp_path: Path) -> None:
    _write_jsonl(tmp_path / "events.jsonl", _two_campaign_events())
    assert assign_campaign_ids(tmp_path) == 2

    events = [json.loads(line) for line in (tmp_path / "events.jsonl").read_text().splitlines()]
    by_sequence = {event["sequence_number"]: event for event in events}

    # Entry legs are emitted before their open: only their decision can place them.
    assert by_sequence[1]["campaign_id"] == "C0001"
    assert by_sequence[2]["campaign_id"] == "C0001"
    assert by_sequence[6]["campaign_id"] == "C0002"
    assert by_sequence[7]["campaign_id"] == "C0002"
    assert sorted({event["campaign_id"] for event in events}) == ["C0001", "C0002"]


def test_campaign_ids_are_stable_across_repeated_derivations(tmp_path: Path) -> None:
    _write_jsonl(tmp_path / "events.jsonl", _two_campaign_events())
    assign_campaign_ids(tmp_path)
    first = [json.loads(line) for line in (tmp_path / "events.jsonl").read_text().splitlines()]
    assign_campaign_ids(tmp_path)
    second = [json.loads(line) for line in (tmp_path / "events.jsonl").read_text().splitlines()]
    assert first == second


# --------------------------------------------------------------------------- #
# D. unresolvable fills stay unattributed and block certification
# --------------------------------------------------------------------------- #
def test_fill_without_resolvable_order_stays_unattributed(tmp_path: Path) -> None:
    orphan_fill = {
        "last_trade_id": "F-9",
        "client_order_id": "O-UNKNOWN",
        "ts_last": 3000,
        "instrument_id": INSTRUMENT,
    }
    run_dir = _seed_run(tmp_path, fills=[*_identified_fills(), orphan_fill])

    fills = [json.loads(line) for line in (run_dir / "fills.jsonl").read_text().splitlines()]
    orphan = next(fill for fill in fills if fill["last_trade_id"] == "F-9")
    assert "campaign_id" not in orphan
    assert "decision_id" not in orphan

    inputs = derive_audit_inputs(run_dir)
    assert inputs.lineage is not None
    certification = certify(inputs.lineage, as_of_ns=inputs.as_of_ns or 0)
    assert not certification.complete
    assert _status(run_dir) != COMPLETE


def test_only_unattributable_fill_evidence_blocks_certification(tmp_path: Path) -> None:
    run_dir = _seed_run(
        tmp_path,
        events=[
            _event(
                1,
                "ORDER_FILLED",
                client_order_id="O-UNKNOWN",
                event_time_ns=3000,
                recorded_time_ns=3010,
            )
        ],
        fills=[
            {
                "last_trade_id": "F-9",
                "client_order_id": "O-UNKNOWN",
                "ts_last": 3000,
                "instrument_id": INSTRUMENT,
            }
        ],
    )
    inputs = derive_audit_inputs(run_dir)
    assert inputs.lineage is not None
    certification = certify(inputs.lineage, as_of_ns=inputs.as_of_ns or 0)
    assert certification.verdict is CompletenessVerdict.BLOCKED
    assert _status(run_dir) != COMPLETE
