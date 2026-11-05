"""P0 ledger identity: campaign and decision joins must survive native callbacks.

The native audit can only render ``AUDIT_COMPLETE`` when the persisted rows carry
explicit identities. These tests pin both halves of the fix:

* the strategy binds a submitted client order id to the decision that created it;
* the pipeline attributes the pre-open entry leg (and every row naming the same
  decision) to the campaign it creates, so decisions -> orders -> fills ->
  cashflows certify ``COMPLETE`` from disk alone.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from v8_next.adapters.expert_strategy import ExpertEnsembleStrategy
from v8_next.audit.lineage import CompletenessVerdict, certify
from v8_next.audit.pipeline import assign_campaign_ids, derive_audit_inputs


class _Order:
    def __init__(self, client_order_id: str) -> None:
        self.client_order_id = client_order_id


def _event(sequence: int, event_type: str, **fields: Any) -> dict[str, Any]:
    return {
        "sequence_number": sequence,
        "event_type": event_type,
        "event_time_ns": 1_000,
        "recorded_time_ns": 1_000,
        "instrument_id": "BTCUSDT-PERP",
        **fields,
    }


def _write_run(run_dir: Path, events: list[dict[str, Any]], fills: list[dict[str, Any]]) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "events.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in events), encoding="utf-8"
    )
    (run_dir / "fills.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in fills), encoding="utf-8"
    )


def _canonical_events() -> list[dict[str, Any]]:
    return [
        _event(1, "OPPORTUNITY_DETECTED", opportunity_id="OPP-1"),
        _event(
            2,
            "ADMISSION_ACCEPTED",
            decision_id="D1000",
            opportunity_id="OPP-1",
            action="SUBMITTED_MARKET_LONG",
        ),
        # Producer fix: the submitted row carries the client order id, so the
        # native ORDER_ACCEPTED/FILLED rows can join a decision.
        _event(
            3,
            "ORDER_SUBMITTED",
            decision_id="D1000",
            opportunity_id="OPP-1",
            client_order_id="O-1",
        ),
        # The producer also names the decision on the native rows.
        _event(
            4,
            "ORDER_ACCEPTED",
            decision_id="D1000",
            opportunity_id="OPP-1",
            client_order_id="O-1",
        ),
        _event(
            5,
            "ORDER_FILLED",
            decision_id="D1000",
            opportunity_id="OPP-1",
            client_order_id="O-1",
        ),
        _event(
            6,
            "POSITION_OPENED",
            decision_id="D1000",
            opportunity_id="OPP-1",
            position_id="P-1",
        ),
        _event(7, "CASHFLOW_RECORDED", trade_id="T-1", amount="-0.5"),
    ]


def _canonical_fills() -> list[dict[str, Any]]:
    return [
        {
            "last_trade_id": "T-1",
            "client_order_id": "O-1",
            "instrument_id": "BTCUSDT-PERP",
            "ts_last": 1_000,
            "recorded_time_ns": 1_000,
            "quantity": 1.0,
        }
    ]


def test_producer_binds_client_order_id_to_the_entry_decision() -> None:
    strategy = SimpleNamespace(
        _decision_by_client_id={},
        _opportunity_by_client_id={},
        _pending_entry_decision_id=None,
        _pending_entry_opportunity_id=None,
    )

    first = ExpertEnsembleStrategy._bind_entry_identity(
        strategy,
        (_Order("O-1"), _Order("O-1-SL"), _Order("O-1-TP")),
        "D1000",
        "OPP-1",
    )

    assert first == "O-1"
    assert strategy._decision_by_client_id == {
        "O-1": "D1000",
        "O-1-SL": "D1000",
        "O-1-TP": "D1000",
    }
    assert strategy._opportunity_by_client_id["O-1"] == "OPP-1"
    assert strategy._pending_entry_decision_id == "D1000"


def test_first_campaign_entry_leg_is_attributed_before_the_position_opens(tmp_path: Path) -> None:
    _write_run(tmp_path, _canonical_events(), _canonical_fills())

    assert assign_campaign_ids(tmp_path) == 1
    events = [
        json.loads(line)
        for line in (tmp_path / "events.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    by_type = {row["event_type"]: row for row in events}

    assert by_type["ADMISSION_ACCEPTED"]["campaign_id"] == "C0001"
    assert by_type["ORDER_SUBMITTED"]["campaign_id"] == "C0001"
    assert by_type["ORDER_ACCEPTED"]["campaign_id"] == "C0001"
    assert by_type["ORDER_FILLED"]["campaign_id"] == "C0001"
    assert by_type["POSITION_OPENED"]["campaign_id"] == "C0001"


def test_decision_identity_propagates_to_rows_without_a_campaign(tmp_path: Path) -> None:
    events = _canonical_events()
    events.append(
        _event(
            8,
            "ORDER_UPDATED",
            # No client id suffix and no campaign before propagation: only the
            # decision identity can join this row.
            decision_id="D1000",
            client_order_id="O-1-AMEND",
        )
    )
    _write_run(tmp_path, events, _canonical_fills())
    assign_campaign_ids(tmp_path)

    rows = [
        json.loads(line)
        for line in (tmp_path / "events.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    amended = next(row for row in rows if row["event_type"] == "ORDER_UPDATED")
    assert amended["campaign_id"] == "C0001"


def test_persisted_chain_certifies_complete_from_disk(tmp_path: Path) -> None:
    _write_run(tmp_path, _canonical_events(), _canonical_fills())
    assign_campaign_ids(tmp_path)

    inputs = derive_audit_inputs(tmp_path)

    assert inputs.lineage is not None
    assert inputs.as_of_ns is not None
    certification = certify(inputs.lineage, as_of_ns=inputs.as_of_ns)
    assert certification.verdict is CompletenessVerdict.COMPLETE, certification.violations
    assert certification.campaign_ids == ("C0001",)
    assert certification.joins_checked >= 3
