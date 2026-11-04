"""Focused tests for the native forensic audit subsystem."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from v8_next.audit import querying
from v8_next.audit.forensic import (
    EVALUATION_STATES,
    EVENT_TYPES,
    SCHEMA_VERSION,
    AuditRecorder,
    AuditSchemaError,
    AuditStore,
    EvidenceClass,
    evaluation_state,
    make_event,
)
from v8_next.audit.native_telemetry import reconcile_fills
from v8_next.audit.pipeline import (
    _gate,
    _signature,
    validate_audit_dir,
)
from v8_next.audit.trade_metrics import (
    TradeFacts,
    compute_post_exit,
    compute_trade_metrics,
)


# --------------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------------- #
class Candle:
    def __init__(self, end_ns: int, o: float, h: float, lo: float, c: float) -> None:
        self.end_ns = end_ns
        self.open = o
        self.high = h
        self.low = lo
        self.close = c


def _candles(specs: list[tuple[float, float, float, float]]) -> list[Candle]:
    return [
        Candle((i + 1) * 3_600_000_000_000, o, h, lo, c)
        for i, (o, h, lo, c) in enumerate(specs)
    ]


# --------------------------------------------------------------------------- #
# A. recorder correctness
# --------------------------------------------------------------------------- #
def test_recorder_assigns_identity_sequence_and_metadata(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    with AuditRecorder(run_id="run-1", events_path=path, instrument_id="X") as recorder:
        recorder.record(
            "MARKET_OBSERVED",
            event_time_ns=10,
            source_component="test",
            evidence_class=EvidenceClass.OBSERVED_TAPE,
            open="1",
        )
        recorder.record(
            "REPLAY_FINALIZED",
            event_time_ns=20,
            source_component="test",
            evidence_class=EvidenceClass.DERIVED,
        )
    rows = list(AuditStore(tmp_path).iter_events())
    assert [r["sequence_number"] for r in rows] == [1, 2]
    for row in rows:
        assert row["schema_version"] == SCHEMA_VERSION
        assert row["run_id"] == "run-1"
        assert row["instrument_id"] == "X"
        assert row["event_type"] in EVENT_TYPES
        assert row["recorded_time_ns"] >= row["event_time_ns"]


def test_unknown_event_type_is_rejected() -> None:
    with pytest.raises(AuditSchemaError):
        make_event(
            run_id="r",
            sequence_number=1,
            event_type="MADE_UP_EVENT",
            instrument_id="X",
            event_time_ns=1,
            recorded_time_ns=2,
            source_component="t",
            evidence_class=EvidenceClass.DERIVED,
        )


def test_mandatory_field_collision_is_rejected() -> None:
    with pytest.raises(AuditSchemaError):
        make_event(
            run_id="r",
            sequence_number=1,
            event_type="MARKET_OBSERVED",
            instrument_id="X",
            event_time_ns=1,
            recorded_time_ns=2,
            source_component="t",
            evidence_class=EvidenceClass.DERIVED,
            fields={"run_id": "other"},
        )


# --------------------------------------------------------------------------- #
# B/C/H. decision integrity, transitions and invalid risk denominators
# --------------------------------------------------------------------------- #
def test_long_mfe_mae_and_r_are_direction_adjusted() -> None:
    candles = _candles([(100, 110, 95, 105), (105, 120, 100, 118)])
    facts = TradeFacts(
        position_id="P1",
        instrument_id="X",
        side="LONG",
        quantity=1.0,
        entry_price=100.0,
        opened_ns=candles[0].end_ns,
        closed_ns=candles[-1].end_ns,
        exit_price=115.0,
        initial_stop_price=90.0,
        initial_target_price=120.0,
    )
    row = compute_trade_metrics(facts=facts, candles=candles)
    # The entry bar is the bar whose close the position was opened on, so its
    # low (95) happened BEFORE the position existed and cannot be post-entry
    # MAE. Excursions are measured from the next bar only.
    assert row["mfe_price"] == 20.0
    assert row["mae_price"] == 0.0
    assert row["mfe_r"] == 2.0
    assert row["realized_gross_r"] == 1.5
    assert row["realized_net_r"] == 1.5
    assert row["exit_kind"] == "UNKNOWN"


def test_entry_bar_extrema_are_not_post_entry_excursion() -> None:
    # A LONG opened at the close of a wide bar: the bar's high (150) and low
    # (40) both precede the entry, so neither may become MFE/MAE.
    candles = _candles([(100, 150, 40, 100), (100, 104, 96, 102)])
    facts = TradeFacts(
        position_id="P1e",
        instrument_id="X",
        side="LONG",
        quantity=1.0,
        entry_price=100.0,
        opened_ns=candles[0].end_ns,
        closed_ns=candles[1].end_ns,
        exit_price=102.0,
        initial_stop_price=90.0,
        initial_target_price=120.0,
    )
    row = compute_trade_metrics(facts=facts, candles=candles)
    assert row["mfe_price"] == 4.0
    assert row["mae_price"] == -4.0
    assert row["measured_window_bars"] == 1


def test_short_trade_r_uses_same_denominator_sign() -> None:
    candles = _candles([(100, 105, 90, 95)])
    facts = TradeFacts(
        position_id="P2",
        instrument_id="X",
        side="SHORT",
        quantity=2.0,
        entry_price=100.0,
        opened_ns=candles[0].end_ns,
        closed_ns=candles[0].end_ns,
        exit_price=95.0,
        initial_stop_price=110.0,
    )
    row = compute_trade_metrics(facts=facts, candles=candles)
    # Opened and closed inside one bar: no post-entry bar exists, so the
    # excursion is unmeasurable (null + reason), never zero and never a STOP.
    assert row["mfe_price"] is None
    assert row["mae_price"] is None
    assert row["path_model_outcome"] == "ENTRY_BAR_ONLY"
    assert row["metric_unavailable_reason"] == "NO_POST_ENTRY_BAR"
    assert row["realized_gross_r"] == 0.5
    assert row["gross_pnl_usdt"] == 10.0


def test_invalid_risk_denominator_returns_null_not_zero() -> None:
    candles = _candles([(100, 101, 99, 100)])
    missing = TradeFacts("P3", "X", "LONG", 1.0, 100.0, candles[0].end_ns, None, None, None)
    row = compute_trade_metrics(facts=missing, candles=candles)
    assert row["mfe_r"] is None and row["mae_r"] is None
    assert row["risk_denominator_status"] == "INVALID"
    assert row["risk_invalid_reason"] == "MISSING_INITIAL_STOP"

    zero = TradeFacts("P4", "X", "LONG", 1.0, 100.0, candles[0].end_ns, candles[0].end_ns, 101.0, 100.0)
    row = compute_trade_metrics(facts=zero, candles=candles)
    assert row["risk_denominator_status"] == "INVALID"
    assert row["risk_invalid_reason"] == "ZERO_STOP_DISTANCE"
    assert row["realized_net_r"] is None


def test_post_exit_horizons_are_separated_by_evidence_class() -> None:
    candles = _candles([(100, 101, 99, 100)] + [(100 + i, 110 + i, 100 + i, 105 + i) for i in range(3)])
    facts = TradeFacts(
        "P5", "X", "LONG", 1.0, 100.0, candles[0].end_ns, candles[0].end_ns, 100.0, 95.0, 120.0
    )
    rows = compute_post_exit(facts=facts, candles=candles, horizons=(1, 3, 48))
    assert [r["horizon_bars"] for r in rows] == [1, 3]
    assert all(r["evidence_class_observed"] == "OBSERVED_TAPE_PATH" for r in rows)
    assert all(
        r["evidence_class_counterfactual"] == "LEGAL_POLICY_COUNTERFACTUAL_MODEL" for r in rows
    )
    assert all(r["evidence_class_hindsight"] == "HINDSIGHT_MAXIMUM_MODEL" for r in rows)
    assert rows[-1]["hindsight_maximum_usdt"] >= rows[-1]["observed_post_exit_path_usdt"]
    assert rows[-1]["legal_policy_counterfactual_usdt"] != rows[-1]["hindsight_maximum_usdt"]
    assert rows[0]["hindsight_maximum_usdt"] == 1.0  # exit-bar high 101 vs exit 100, qty 1


# --------------------------------------------------------------------------- #
# F/G. reconciliation and cashflow classes
# --------------------------------------------------------------------------- #
def test_reconcile_fills_publishes_every_mismatch_class() -> None:
    native = [
        {"venue_order_id": "O1", "commissions": "1 USDT"},
        {"venue_order_id": "O1", "commissions": "1 USDT"},
        {"venue_order_id": "O2", "commissions": "1 USDT"},
    ]
    audit = [{"venue_order_id": "O1"}, {"venue_order_id": "O3"}]
    out = reconcile_fills(native, audit)
    assert out["native_fill_count"] == 3
    assert out["audited_fill_count"] == 2
    assert out["duplicate_fill_ids"] == 1
    assert out["unmatched_native_fills"] == 1
    assert out["unmatched_audit_fills"] == 1


# --------------------------------------------------------------------------- #
# J. artifact validation failure modes
# --------------------------------------------------------------------------- #
def _seed_minimal_run(run_dir: Path, *, gate_status: str = "PASS", tr_sections: int = 12) -> None:
    store = AuditStore(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    for name in ("events.jsonl", "market_observations.jsonl", "expert_decisions.jsonl",
                 "opportunities.jsonl", "orders.jsonl", "fills.jsonl", "positions.jsonl",
                 "cashflows.jsonl", "trade_lifecycles.jsonl"):
        (run_dir / name).write_text('{"event_type":"MARKET_OBSERVED"}\n', encoding="utf-8")
    for name in ("opportunity_funnel", "execution_metrics", "economic_metrics",
                 "mfe_mae_metrics", "post_exit_metrics", "data_quality"):
        (run_dir / f"{name}.json").write_text("{}\n", encoding="utf-8")
    (run_dir / "reconciliation.json").write_text(json.dumps({
        "unmatched_native_fills": 0,
        "unmatched_audit_fills": 0,
        "unlinked_orders": 0,
        "unlinked_positions": 0,
        "duplicate_fill_ids": 0,
        "unexplained_cashflows": 0,
        "orphan_order_events": 0,
        "orphan_position_events": 0,
    }), encoding="utf-8")
    gate_rows = [
        {
            "gate_id": f"G{i}",
            "status": gate_status,
            "explanation": "x",
            "evidence_paths": ["events.jsonl"],
            "tested_at_or_run_reference": "run",
        }
        for i in range(1, 13)
    ]
    (run_dir / "gate_results.json").write_text(json.dumps({"gates": gate_rows}), encoding="utf-8")
    headings = [
        "Çalıştırma Kimliği", "Piyasa Verisi Kalitesi", "Uzman Etkinliği", "Fırsat Hunisi",
        "Gerçek Emirler ve Gerçekleşmeler", "Pozisyon Yaşam Döngüsü", "Risk ve Koruma",
        "İşlem Ekonomisi", "MFE, MAE ve Gerçekleşen R", "Kaçırılan ve Reddedilen Fırsatlar",
        "Başarısızlık Atfı", "Yeniden Üretilebilirlik ve Sonraki Adımlar",
    ][:tr_sections]
    (run_dir / "FINAL_DELIVERY_TR.md").write_text(
        "\n".join(f"## {i}. {h}" for i, h in enumerate(headings, 1)), encoding="utf-8"
    )
    (run_dir / "AUDIT_REPORT.md").write_text("# report\n", encoding="utf-8")
    (run_dir / "validation_results.json").write_text("{}\n", encoding="utf-8")
    artifacts = {}
    for path in sorted(run_dir.iterdir()):
        if path.is_file() and path.name != "run_manifest.json":
            data = path.read_bytes()
            artifacts[path.name] = {
                "sha256": __import__("hashlib").sha256(data).hexdigest(),
                "bytes": len(data),
            }
    manifest = {"run_id": "test", "artifacts": artifacts}
    (run_dir / "run_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    _ = store


def test_validation_accepts_complete_run(tmp_path: Path) -> None:
    _seed_minimal_run(tmp_path)
    result = validate_audit_dir(tmp_path)
    assert result["violations"] == []
    assert result["status"] == "PASS"


def test_validation_rejects_illegal_gate_enum_missing_sections_and_missing_orphans(
    tmp_path: Path,
) -> None:
    _seed_minimal_run(tmp_path, gate_status="PASS_WITH_LIMITATION", tr_sections=10)
    (tmp_path / "reconciliation.json").write_text("{}", encoding="utf-8")
    result = validate_audit_dir(tmp_path)
    joined = " ".join(result["violations"])
    assert "ILLEGAL_GATE_STATUS" in joined
    assert "TR_SECTION_MISSING" in joined
    assert "ORPHAN_COUNT_MISSING" in joined
    assert result["status"] == "FAIL"


def test_evaluation_state_vocabulary_is_closed() -> None:
    assert evaluation_state("SUPPORT") == "SUPPORT"
    assert evaluation_state("abstain") == "ABSTAIN"
    assert evaluation_state("SOMETHING_NEW") == "NOT_EVALUATED"
    assert evaluation_state(None) == "NOT_EVALUATED"
    assert EVALUATION_STATES == {
        "NOT_EVALUATED",
        "NO_OPPORTUNITY",
        "ABSTAIN",
        "CONTRADICT",
        "SUPPORT",
        "EVALUATION_ERROR",
    }


def test_validation_rejects_illegal_evaluation_state(tmp_path: Path) -> None:
    _seed_minimal_run(tmp_path)
    with (tmp_path / "events.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {
                    "event_type": "EXPERT_EVALUATED",
                    "stance": "MAYBE",
                    "expert_id": "e1",
                }
            )
            + "\n"
        )
    result = validate_audit_dir(tmp_path)
    assert any(v.startswith("ILLEGAL_EVALUATION_STATE") for v in result["violations"])
    assert result["status"] == "FAIL"


def test_gate_rejects_illegal_status() -> None:
    with pytest.raises(ValueError):
        _gate("G1", "MOSTLY_PASS", "x", [], "run")


def test_validation_rejects_corrupt_hash(tmp_path: Path) -> None:
    _seed_minimal_run(tmp_path)
    manifest = json.loads((tmp_path / "run_manifest.json").read_text(encoding="utf-8"))
    manifest["artifacts"] = {"events.jsonl": {"sha256": "deadbeef", "bytes": 1}}
    (tmp_path / "run_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    result = validate_audit_dir(tmp_path)
    assert any(v.startswith("MANIFEST_HASH_MISMATCH") for v in result["violations"])


# --------------------------------------------------------------------------- #
# K. queryability from persisted artifacts
# --------------------------------------------------------------------------- #
def test_campaign_timeline_and_queries_read_from_disk(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    with AuditRecorder(run_id="q", events_path=path) as recorder:
        recorder.record("POSITION_OPENED", event_time_ns=1, source_component="t",
                        evidence_class=EvidenceClass.OBSERVED_SIMULATION, position_id="P1")
        recorder.record("ORDER_FILLED", event_time_ns=2, source_component="t",
                        evidence_class=EvidenceClass.OBSERVED_SIMULATION, position_id="P1")
        recorder.record("EXPERT_EVALUATED", event_time_ns=3, source_component="t",
                        evidence_class=EvidenceClass.DERIVED, expert_id="EXP",
                        stance="SUPPORT")
        recorder.record("ADMISSION_REJECTED", event_time_ns=4, source_component="t",
                        evidence_class=EvidenceClass.DERIVED, reason_code="POSITION_OCCUPIED")
    timeline = querying.campaign_timeline(tmp_path, position_id="P1")
    assert [e["event_type"] for e in timeline] == ["POSITION_OPENED", "ORDER_FILLED"]
    assert len(querying.expert_decisions(tmp_path, "EXP")) == 1
    assert len(querying.opportunities_rejected(tmp_path, "POSITION_OCCUPIED")) == 1
    assert querying.orphan_records(tmp_path)["counts"]["unmatched_native_fills"] is None


# --------------------------------------------------------------------------- #
# D/E/I. native execution fixture with a real engine
# --------------------------------------------------------------------------- #
HOUR_NS = 3_600_000_000_000


def _fixture_candles(n: int = 90):
    from v8_next.domain.market import Candle as DomainCandle

    rows = []
    price = 100.0
    for i in range(n):
        # Deterministic oscillation with breakouts so entries exist.
        drift = 1.2 if i % 9 < 5 else -1.0
        open_ = price
        close = price + drift
        high = max(open_, close) + 0.6
        low = min(open_, close) - 0.6
        rows.append(
            DomainCandle(
                "BTCUSDT-PERP.BINANCE",
                i * HOUR_NS,
                (i + 1) * HOUR_NS,
                Decimal(str(round(open_, 2))),
                Decimal(str(round(high, 2))),
                Decimal(str(round(low, 2))),
                Decimal(str(round(close, 2))),
                Decimal("100"),
                (i + 1) * HOUR_NS,
                (i + 1) * HOUR_NS,
                "audit-fixture",
            )
        )
        price = close
    return tuple(rows)


def _run_engine(candles, recorder=None):
    from v8_next.adapters.expert_strategy import (
        ExpertStrategyConfig,
        run_expert_strategy_backtest,
    )

    config = ExpertStrategyConfig(
        min_support_quorum=1,
        max_contradiction_tolerance=28,
        bracket_stop_pct=Decimal("0.02"),
        bracket_target_pct=Decimal("0.04"),
        entry_order_type="MARKET",
    )
    return run_expert_strategy_backtest(
        candles,
        config,
        initial_balance=Decimal("10000"),
        audit_recorder=recorder,
    )


def test_native_fill_report_is_available_not_importerror(tmp_path: Path) -> None:
    candles = _fixture_candles()
    result = _run_engine(candles)
    status = result["native_reports"]["status"]
    assert not status["fills"].startswith(("IMPORT_ERROR", "API_ABSENT")), status
    assert result["fill_report_type"]
    records = result["fill_records"]
    assert records, "expected at least one native fill in the deterministic fixture"
    assert "commissions" in records[0]
    assert "liquidity_side" in records[0]


def test_audit_on_off_produces_identical_trading_result(tmp_path: Path) -> None:
    candles = _fixture_candles()
    off = _run_engine(candles)
    recorder = AuditRecorder(run_id="eq", events_path=tmp_path / "events.jsonl")
    on = _run_engine(candles, recorder=recorder)
    recorder.close()
    assert _signature(off) == _signature(on)
    assert recorder.sequence > 0


# --------------------------------------------------------------------------- #
# L. report-only mode never replays the engine
# --------------------------------------------------------------------------- #
def test_report_only_mode_does_not_run_the_engine(tmp_path: Path, monkeypatch) -> None:
    from v8_next.app import cli
    from v8_next.audit import pipeline as pipeline_mod

    _seed_minimal_run(tmp_path)

    def _explode(**_: object) -> None:  # pragma: no cover - must not be called
        raise AssertionError("report-only must not run the engine")

    monkeypatch.setattr(pipeline_mod, "run_native_audit", _explode)
    monkeypatch.setattr("v8_next.adapters.expert_strategy.run_expert_strategy_backtest", _explode)
    code = cli.main(["audit", "--report-only", "--run-dir", str(tmp_path)])
    assert code == 0


# --------------------------------------------------------------------------- #
# M. auditor-remediation behaviour
# --------------------------------------------------------------------------- #
def test_exit_kind_is_classified_from_cycle_tags_not_timestamp_window() -> None:
    from v8_next.audit.trade_metrics import classify_exit_kind

    rows = [
        # previous campaign's stop at exactly this campaign's open stamp
        {"side": "SELL", "status": "FILLED", "tags": ["STOP_LOSS"], "ts_last": 1_000,
         "type": "STOP_MARKET", "venue_order_id": "V1"},
        # this campaign's entry at the same stamp
        {"side": "BUY", "status": "FILLED", "tags": ["ENTRY"], "ts_last": 1_000,
         "type": "MARKET", "venue_order_id": "V2"},
        {"side": "SELL", "status": "FILLED", "tags": ["TAKE_PROFIT"], "ts_last": 5_000,
         "type": "LIMIT", "venue_order_id": "V3"},
    ]
    assert classify_exit_kind(1, rows, opened_ns=1_000) == "TARGET_LIMIT"


def test_ambiguous_stop_and_target_bar_resolves_stop_first() -> None:
    # One bar covers both the stop (95) and the target (110) for a LONG.
    # The declared contract is STOP_FIRST: the stop fills first, MFE is capped
    # at the target distance and MAE at the stop distance.
    candles = _candles([(100, 100, 100, 100), (100, 115, 80, 112)])
    facts = TradeFacts(
        position_id="Pamb",
        instrument_id="X",
        side="LONG",
        quantity=1.0,
        entry_price=100.0,
        opened_ns=candles[0].end_ns,
        closed_ns=None,
        exit_price=None,
        initial_stop_price=95.0,
        initial_target_price=110.0,
    )
    row = compute_trade_metrics(facts=facts, candles=candles)
    assert row["path_model_outcome"] == "STOP_FIRST"
    assert row["mae_r"] == -1.0
    assert row["mfe_r"] == 0.0
    assert row["mae_capped_by_stop"] is True


def test_native_target_exit_is_not_relabelled_as_stop() -> None:
    # A campaign that exited at the target must keep its native exit kind even
    # though the audit path model sees a stop-first bar.
    candles = _candles([(100, 100, 100, 100), (100, 104, 95, 96)])
    facts = TradeFacts(
        position_id="Ptgt",
        instrument_id="X",
        side="SHORT",
        quantity=1.0,
        entry_price=100.0,
        opened_ns=candles[0].end_ns,
        closed_ns=candles[1].end_ns,
        exit_price=96.0,
        initial_stop_price=105.0,
        initial_target_price=96.0,
        exit_kind="TARGET_LIMIT",
    )
    row = compute_trade_metrics(facts=facts, candles=candles)
    assert row["exit_kind"] == "TARGET_LIMIT"
    assert row["realized_gross_r"] == 0.8
    assert row["path_model_outcome"] == "TARGET_FIRST"


def test_campaign_lineage_carries_decision_and_opportunity_ids() -> None:
    from v8_next.audit.trade_metrics import trade_facts_from_replay

    result = {
        "opened_positions": [
            {"position_id": "P", "instrument_id": "X", "side": "LONG", "quantity": "1",
             "avg_px_open": "100", "event_ns": 1_000},
        ],
        "closed_positions": [
            {"position_id": "P", "avg_px_close": "105", "realized_pnl": "4 USDT",
             "event_ns": 9_000},
        ],
        "fill_records": [
            {"position_id": "P", "ts_last": 1_000, "side": "BUY", "tags": ["ENTRY"],
             "commissions": "0", "venue_order_id": "V1", "status": "FILLED"},
            {"position_id": "P", "ts_last": 9_000, "side": "SELL", "tags": ["TAKE_PROFIT"],
             "commissions": "0", "venue_order_id": "V2", "status": "FILLED"},
        ],
        "decisions": [
            {"decision_ns": 1_000, "decision_id": "D1000", "opportunity_id": "OPP1",
             "opportunity": {"opportunity_id": "OPP1"}},
        ],
        "config": {"bracket_stop_pct": "0.02", "bracket_target_pct": "0.04"},
    }
    facts = trade_facts_from_replay(result, cutoff_ns=20_000)
    assert facts[0].decision_id == "D1000"
    assert facts[0].opportunity_id == "OPP1"


def test_stop_first_caps_mae_at_the_stop_distance() -> None:
    candles = _candles([(100, 101, 99, 100), (100, 110, 80, 90)])
    facts = TradeFacts(
        "P6", "X", "LONG", 1.0, 100.0, candles[0].end_ns, candles[1].end_ns, 90.0, 95.0, 200.0
    )
    row = compute_trade_metrics(facts=facts, candles=candles)
    assert row["path_model_applied"] == "STOP_FIRST"
    assert row["mae_r"] == -1.0  # low of 80 is capped by stop-first at 95
    assert row["mae_capped_by_stop"] is True


def test_trade_facts_split_entry_and_exit_fees_and_assign_campaign_ids() -> None:
    from v8_next.audit.trade_metrics import trade_facts_from_replay

    result = {
        "opened_positions": [
            {"position_id": "P", "instrument_id": "X", "side": "LONG", "quantity": "1",
             "avg_px_open": "100", "event_ns": 1_000},
            {"position_id": "P", "instrument_id": "X", "side": "LONG", "quantity": "1",
             "avg_px_open": "110", "event_ns": 10_000},
        ],
        "closed_positions": [
            {"position_id": "P", "avg_px_close": "105", "realized_pnl": "4 USDT",
             "event_ns": 10_000},
        ],
        "fill_records": [
            {"position_id": "P", "ts_last": 1_000, "side": "BUY", "tags": ["ENTRY"],
             "commissions": "1 USDT", "venue_order_id": "V1", "status": "FILLED"},
            {"position_id": "P", "ts_last": 9_000, "side": "SELL", "tags": ["TAKE_PROFIT"],
             "commissions": "2 USDT", "venue_order_id": "V2", "status": "FILLED"},
            # next campaign's entry shares the boundary timestamp
            {"position_id": "P", "ts_last": 10_000, "side": "BUY", "tags": ["ENTRY"],
             "commissions": "3 USDT", "venue_order_id": "V3", "status": "FILLED"},
        ],
        "config": {"bracket_stop_pct": "0.02", "bracket_target_pct": "0.04"},
    }
    facts = trade_facts_from_replay(result, cutoff_ns=20_000)
    assert [f.extra["campaign_id"] for f in facts] == ["C0001", "C0002"]
    assert facts[0].extra["entry_fees"] == 1.0
    assert facts[0].extra["exit_fees"] == 2.0
    assert facts[0].fees == 3.0
    assert facts[1].extra["entry_fees"] == 3.0
    assert facts[1].exit_kind == "UNIDENTIFIED_NO_CLOSING_FILL"


def test_campaign_id_is_queryable_from_the_ledger(tmp_path: Path) -> None:
    store = AuditStore(tmp_path)
    recorder = AuditRecorder(run_id="r", events_path=store.path("events.jsonl"), instrument_id="X")
    recorder.record(
        "POSITION_OPENED", event_time_ns=1_000, source_component="test",
        evidence_class=EvidenceClass.OBSERVED_SIMULATION,
        position_id="P", campaign_id="C0001",
    )
    recorder.record(
        "POSITION_CLOSED", event_time_ns=2_000, source_component="test",
        evidence_class=EvidenceClass.OBSERVED_SIMULATION,
        position_id="P", campaign_id="C0001",
    )
    recorder.close()
    rows = querying.query(tmp_path, campaign_id="C0001")
    assert len(rows) == 2
    assert querying.campaign_timeline(tmp_path, position_id="P") == rows
