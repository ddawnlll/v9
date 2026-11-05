"""The native audit run supplies its own verdict instead of rendering ``AUDIT_BLOCKED``.

The wave-1 audit repair made the report fail closed: ``write_reports`` without a
verdict can never render ``AUDIT_COMPLETE``. That left the producer unwired —
every native run rendered ``AUDIT_BLOCKED`` even when its artifacts supported a
complete audit. These tests pin the producer side:

* ``derive_audit_inputs`` is a pure function of the persisted run directory;
* the verdict it derives renders the label the evidence justifies
  (``COMPLETE`` / ``INCOMPLETE`` / ``NOT_IDENTIFIABLE`` / ``NOT_RUN``);
* a lineage chain is supplied from persisted rows when the run has one;
* rebuilding from the same directory yields the identical label.
"""

from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path
from typing import Any

import pytest

from v8_next.audit.lineage import LineageChain, certify
from v8_next.audit.pipeline import derive_audit_inputs
from v8_next.audit.reporting import write_reports
from v8_next.audit.verdicts import VerdictState, audit_label

INSTRUMENT = "BTCUSDT-PERP"
COMPLETE = "AUDIT_COMPLETE"


def _status_text(report: str) -> str:
    line = next(row for row in report.splitlines() if row.startswith("**DURUM:**"))
    return line.removeprefix("**DURUM:**").strip()


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


def _event(sequence: int, event_type: str, **fields: Any) -> dict[str, Any]:
    payload = {
        "schema_version": "AUDIT_EVENT_V1",
        "run_id": "wiring-fixture",
        "sequence_number": sequence,
        "event_type": event_type,
        "instrument_id": INSTRUMENT,
        "event_time_ns": fields.pop("event_time_ns", 1000),
        "recorded_time_ns": fields.pop("recorded_time_ns", 1100),
        "source_component": "test/wiring",
        "evidence_class": "OBSERVED_SIMULATION",
        **fields,
    }
    return payload


def _lineage_events() -> list[dict[str, Any]]:
    return [
        _event(
            1,
            "OPPORTUNITY_DETECTED",
            opportunity_id="OPP-1",
            event_time_ns=900,
            recorded_time_ns=950,
        ),
        _event(
            2,
            "ADMISSION_ACCEPTED",
            decision_id="D1000",
            opportunity_id="OPP-1",
            campaign_id="C0001",
            event_time_ns=1000,
            recorded_time_ns=1050,
        ),
        _event(
            3,
            "ORDER_SUBMITTED",
            decision_id="D1000",
            campaign_id="C0001",
            client_order_id="O-1",
            venue_order_id="V-1",
            event_time_ns=1000,
            recorded_time_ns=1060,
        ),
        _event(
            4,
            "CASHFLOW_RECORDED",
            trade_id="F-1",
            campaign_id="C0001",
            amount="-0.5",
            cashflow_type="COMMISSION",
            event_time_ns=2000,
            recorded_time_ns=2100,
        ),
    ]


def _lineage_fills() -> list[dict[str, Any]]:
    return [
        {
            "last_trade_id": "F-1",
            "client_order_id": "O-1",
            "venue_order_id": "V-1",
            "ts_last": 2000,
            "instrument_id": INSTRUMENT,
        }
    ]


def _seed_run(
    run_dir: Path,
    *,
    lifecycles: list[dict[str, Any]] | None = None,
    lineage: bool = True,
    fills: list[dict[str, Any]] | None = None,
    reconciliation_residual: str = "0.0",
    data_quality: dict[str, Any] | None = None,
) -> Path:
    """Persist the minimal artifact set the verdict derivation reads."""
    run_dir.mkdir(parents=True, exist_ok=True)
    rows = [_lifecycle()] if lifecycles is None else lifecycles
    (run_dir / "trade_lifecycles.jsonl").write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows), encoding="utf-8"
    )
    if fills is None:
        fills = _lineage_fills() if lineage else []
    (run_dir / "fills.jsonl").write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in fills), encoding="utf-8"
    )
    (run_dir / "events.jsonl").write_text(
        "".join(
            json.dumps(row, sort_keys=True) + "\n"
            for row in (_lineage_events() if lineage else [])
        ),
        encoding="utf-8",
    )
    (run_dir / "reconciliation.json").write_text(
        json.dumps(
            {
                "conservation_residual_usdt": reconciliation_residual,
                "conservation_tolerance_usdt": 0.01,
                "unexplained_cashflows": 0,
                "fee_total_native_fills": 0.5,
                "realized_pnl_total_native": 4.5,
            }
        ),
        encoding="utf-8",
    )
    (run_dir / "economic_metrics.json").write_text(
        json.dumps({"realized_net_trading_pnl": 4.5, "native_realized_pnl_total": 4.5}),
        encoding="utf-8",
    )
    (run_dir / "data_quality.json").write_text(
        json.dumps(
            data_quality
            if data_quality is not None
            else {
                "bars": 2,
                "tape_sha256": "a" * 64,
                "oos_data_touched": False,
                "funding_rows": 0,
            }
        ),
        encoding="utf-8",
    )
    (run_dir / "gate_results.json").write_text(
        json.dumps(
            {
                "gates": [
                    {
                        "gate_id": f"G{index}",
                        "status": "PASS",
                        "explanation": "fixture",
                        "evidence_paths": ["events.jsonl"],
                        "tested_at_or_run_reference": "fixture",
                    }
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


def _render(run_dir: Path) -> str:
    inputs = derive_audit_inputs(run_dir)
    write_reports(
        run_dir,
        verdict=inputs.verdict,
        lineage=inputs.lineage,
        as_of_ns=inputs.as_of_ns,
    )
    return (run_dir / "FINAL_DELIVERY_TR.md").read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# A. the verdict the persisted artifacts support
# --------------------------------------------------------------------------- #
def test_complete_run_renders_audit_complete(tmp_path: Path) -> None:
    run_dir = _seed_run(tmp_path)
    inputs = derive_audit_inputs(run_dir)
    assert inputs.verdict.mechanical is VerdictState.PASS
    assert inputs.verdict.economic is VerdictState.PASS
    assert inputs.verdict.pit is VerdictState.PASS
    assert inputs.verdict.completeness is VerdictState.PASS
    assert audit_label(inputs.verdict) == COMPLETE
    assert _status_text(_render(run_dir)) == COMPLETE


def test_complete_lifecycles_without_lineage_rows_render_complete(tmp_path: Path) -> None:
    """Acceptance shape: complete lifecycles + source ids, no chain claimed."""
    run_dir = _seed_run(tmp_path, lineage=False)
    inputs = derive_audit_inputs(run_dir)
    assert inputs.lineage is None
    assert audit_label(inputs.verdict) == COMPLETE
    assert _status_text(_render(run_dir)) == COMPLETE


def test_missing_decision_id_renders_incomplete(tmp_path: Path) -> None:
    run_dir = _seed_run(
        tmp_path, lifecycles=[_lifecycle(decision_id=None)], lineage=False
    )
    inputs = derive_audit_inputs(run_dir)
    assert inputs.verdict.completeness is VerdictState.INCOMPLETE
    assert "MISSING_SOURCE_ID:decision_id" in inputs.verdict.reasons
    assert _status_text(_render(run_dir)) == "AUDIT_INCOMPLETE"


def test_mechanical_tie_renders_not_identifiable(tmp_path: Path) -> None:
    run_dir = _seed_run(
        tmp_path, lifecycles=[_lifecycle(same_bar_stop_and_target=True)], lineage=False
    )
    inputs = derive_audit_inputs(run_dir)
    assert inputs.verdict.mechanical is VerdictState.NOT_IDENTIFIABLE
    assert _status_text(_render(run_dir)) == "AUDIT_NOT_IDENTIFIABLE"


def test_run_without_trade_rows_is_never_complete(tmp_path: Path) -> None:
    run_dir = _seed_run(tmp_path, lifecycles=[], lineage=False)
    inputs = derive_audit_inputs(run_dir)
    assert inputs.verdict.mechanical is VerdictState.NOT_RUN
    assert inputs.verdict.completeness is VerdictState.NOT_RUN
    label = _status_text(_render(run_dir))
    assert label in {"AUDIT_NOT_RUN", "AUDIT_BLOCKED"}
    assert label != COMPLETE


def test_unreconciled_economics_are_incomplete(tmp_path: Path) -> None:
    run_dir = _seed_run(tmp_path, reconciliation_residual="1.0", lineage=False)
    inputs = derive_audit_inputs(run_dir)
    assert inputs.verdict.economic is VerdictState.INCOMPLETE
    assert _status_text(_render(run_dir)) == "AUDIT_INCOMPLETE"


def test_closed_row_without_realized_pnl_is_not_economic_pass(tmp_path: Path) -> None:
    run_dir = _seed_run(
        tmp_path,
        lifecycles=[_lifecycle(realized_net_pnl_usdt=None)],
        lineage=False,
    )
    inputs = derive_audit_inputs(run_dir)
    assert inputs.verdict.mechanical is VerdictState.PASS
    assert inputs.verdict.economic is VerdictState.INCOMPLETE
    assert _status_text(_render(run_dir)) == "AUDIT_INCOMPLETE"


def test_look_ahead_flag_fails_the_pit_dimension(tmp_path: Path) -> None:
    run_dir = _seed_run(
        tmp_path,
        lineage=False,
        data_quality={
            "bars": 2,
            "tape_sha256": "b" * 64,
            "oos_data_touched": True,
        },
    )
    inputs = derive_audit_inputs(run_dir)
    assert inputs.verdict.pit is VerdictState.FAIL
    assert "PIT_LOOK_AHEAD_FLAG:oos_data_touched" in inputs.verdict.reasons
    assert _status_text(_render(run_dir)) == "AUDIT_FAIL"


def test_absent_data_quality_is_not_a_pit_pass(tmp_path: Path) -> None:
    run_dir = _seed_run(tmp_path, lineage=False)
    (run_dir / "data_quality.json").unlink()
    inputs = derive_audit_inputs(run_dir)
    assert inputs.verdict.pit is VerdictState.NOT_RUN
    assert inputs.verdict.completeness is VerdictState.NOT_RUN


# --------------------------------------------------------------------------- #
# B. lineage participation
# --------------------------------------------------------------------------- #
def test_lineage_chain_is_built_from_persisted_rows(tmp_path: Path) -> None:
    run_dir = _seed_run(tmp_path)
    inputs = derive_audit_inputs(run_dir)
    chain = inputs.lineage
    assert chain is not None
    assert [row.opportunity_id for row in chain.opportunities] == ["OPP-1"]
    assert [row.decision_id for row in chain.decisions] == ["D1000"]
    assert [row.order_id for row in chain.orders] == ["O-1"]
    assert [row.fill_id for row in chain.fills] == ["F-1"]
    assert [row.cashflow_id for row in chain.cashflows] == ["CF:4"]
    assert inputs.as_of_ns == 2100
    assert certify(chain, as_of_ns=inputs.as_of_ns or 0).complete


def test_run_without_lineage_rows_supplies_no_chain(tmp_path: Path) -> None:
    run_dir = _seed_run(tmp_path, lineage=False)
    inputs = derive_audit_inputs(run_dir)
    assert inputs.lineage is None
    assert inputs.as_of_ns is None


def test_open_only_run_cannot_earn_an_economic_pass(tmp_path: Path) -> None:
    """A run that closed nothing has no realized outcome to reconcile."""
    run_dir = _seed_run(
        tmp_path,
        # The campaign is still open: the reconciling ledger balanced, and the row
        # even carries PnL columns, but no campaign was ever closed.
        lifecycles=[
            _lifecycle(
                position_status="OPEN",
                closed_ns=None,
                exit_price=None,
                exit_kind=None,
            )
        ],
        lineage=False,
    )
    inputs = derive_audit_inputs(run_dir)

    assert inputs.verdict.economic is VerdictState.INCOMPLETE
    assert "ECONOMIC_NO_CLOSED_CAMPAIGN" in inputs.verdict.reasons
    assert audit_label(inputs.verdict) != COMPLETE
    assert _status_text(_render(run_dir)) == "AUDIT_INCOMPLETE"


def test_fills_without_any_identity_declare_a_broken_chain(tmp_path: Path) -> None:
    """Persisted fills that cannot be joined must block, never read as "no claim"."""
    run_dir = _seed_run(
        tmp_path,
        lineage=False,
        # No trade id and no order: every fill row is dropped by the join.
        fills=[{"ts_last": 2000, "instrument_id": INSTRUMENT}],
    )
    inputs = derive_audit_inputs(run_dir)

    assert inputs.lineage == LineageChain()
    assert inputs.as_of_ns == 0
    certification = certify(inputs.lineage, as_of_ns=0)
    assert not certification.complete
    # The run's own dimensions may pass; the declared-but-uncertifiable chain is
    # what blocks the label.
    assert inputs.verdict.completeness is VerdictState.PASS
    label = _status_text(_render(run_dir))
    assert label == "AUDIT_BLOCKED"
    assert label != COMPLETE


def test_unjoined_fill_participates_and_downgrades_the_report(tmp_path: Path) -> None:
    """A chain the ledger cannot join must downgrade the label, never pass silently."""
    run_dir = _seed_run(
        tmp_path,
        # The fill names no order, so its campaign/decision cannot be certified.
        fills=[
            {
                "last_trade_id": "F-1",
                "ts_last": 2000,
                "instrument_id": INSTRUMENT,
            }
        ],
    )
    inputs = derive_audit_inputs(run_dir)
    assert inputs.verdict.completeness is VerdictState.PASS
    assert inputs.lineage is not None
    certification = certify(inputs.lineage, as_of_ns=inputs.as_of_ns or 0)
    assert not certification.complete
    assert _status_text(_render(run_dir)) == "AUDIT_INCOMPLETE"


# --------------------------------------------------------------------------- #
# C. persistence: the same directory always yields the same inputs and label
# --------------------------------------------------------------------------- #
def test_verdict_is_a_pure_function_of_the_persisted_run_dir(tmp_path: Path) -> None:
    run_dir = _seed_run(tmp_path)
    first = derive_audit_inputs(run_dir)
    second = derive_audit_inputs(run_dir)
    assert first == second
    assert first.lineage is not None and second.lineage is not None
    assert first.lineage.digest() == second.lineage.digest()

    assert _status_text(_render(run_dir)) == _status_text(_render(run_dir)) == COMPLETE

    # Changing only the persisted evidence changes the derived label.
    (run_dir / "trade_lifecycles.jsonl").write_text(
        json.dumps(_lifecycle(decision_id=None), sort_keys=True) + "\n", encoding="utf-8"
    )
    rebuilt = derive_audit_inputs(run_dir)
    assert rebuilt != first
    assert audit_label(rebuilt.verdict) == "AUDIT_INCOMPLETE"
    assert _status_text(_render(run_dir)) == "AUDIT_INCOMPLETE"


# --------------------------------------------------------------------------- #
# D. the report-only producer path supplies the verdict too
# --------------------------------------------------------------------------- #
def _seed_validatable_run(run_dir: Path) -> None:
    """A run dir that passes ``validate_audit_dir`` but has no trade rows."""
    run_dir.mkdir(parents=True, exist_ok=True)
    for name in (
        "events.jsonl",
        "market_observations.jsonl",
        "expert_decisions.jsonl",
        "opportunities.jsonl",
        "orders.jsonl",
        "fills.jsonl",
        "positions.jsonl",
        "cashflows.jsonl",
        "trade_lifecycles.jsonl",
    ):
        (run_dir / name).write_text('{"event_type":"MARKET_OBSERVED"}\n', encoding="utf-8")
    for name in (
        "opportunity_funnel",
        "execution_metrics",
        "economic_metrics",
        "mfe_mae_metrics",
        "post_exit_metrics",
        "data_quality",
    ):
        (run_dir / f"{name}.json").write_text("{}\n", encoding="utf-8")
    (run_dir / "reconciliation.json").write_text(
        json.dumps(
            {
                "unmatched_native_fills": 0,
                "unmatched_audit_fills": 0,
                "unlinked_orders": 0,
                "unlinked_positions": 0,
                "duplicate_fill_ids": 0,
                "unexplained_cashflows": 0,
                "orphan_order_events": 0,
                "orphan_position_events": 0,
            }
        ),
        encoding="utf-8",
    )
    (run_dir / "gate_results.json").write_text(
        json.dumps(
            {
                "gates": [
                    {
                        "gate_id": f"G{index}",
                        "status": "PASS",
                        "explanation": "x",
                        "evidence_paths": ["events.jsonl"],
                        "tested_at_or_run_reference": "run",
                    }
                    for index in range(1, 13)
                ]
            }
        ),
        encoding="utf-8",
    )
    (run_dir / "FINAL_DELIVERY_TR.md").write_text("# placeholder\n", encoding="utf-8")
    (run_dir / "AUDIT_REPORT.md").write_text("# report\n", encoding="utf-8")
    (run_dir / "validation_results.json").write_text("{}\n", encoding="utf-8")
    artifacts = {
        path.name: {"sha256": sha256(path.read_bytes()).hexdigest(), "bytes": path.stat().st_size}
        for path in sorted(run_dir.iterdir())
        if path.is_file() and path.name != "run_manifest.json"
    }
    (run_dir / "run_manifest.json").write_text(
        json.dumps({"run_id": "wiring-cli", "artifacts": artifacts}), encoding="utf-8"
    )


def test_report_only_cli_supplies_the_derived_verdict(tmp_path: Path) -> None:
    from v8_next.app import cli

    _seed_validatable_run(tmp_path)
    assert cli.main(["audit", "--report-only", "--run-dir", str(tmp_path)]) == 0
    label = _status_text((tmp_path / "FINAL_DELIVERY_TR.md").read_text(encoding="utf-8"))
    assert label == "AUDIT_NOT_RUN"
    assert label != "AUDIT_BLOCKED"


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__]))
