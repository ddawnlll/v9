"""Dimensional-accounting and trust-boundary regressions for G3/G7/G8.

All fixtures are deterministic and offline. G3 cases pin the USDT accounting
contract; G7 pins fail-closed evidence states; G8 pins the attestation and
reconciliation requirements that replace caller-declared provenance.
"""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from pathlib import Path

import pytest

import v8_next.evaluation.gate_resolution as gr
from v8_next.adapters.shadow_ingest import (
    ATTESTATION_KEY_ENV,
    MODE_PUBLIC_PAPER,
    load_shadow_fills,
    make_venue_attestation,
)
from v8_next.domain.market import Candle
from v8_next.evaluation.benchmark_receipt import (
    BenchmarkLedger,
    BenchmarkReceipt,
    GateState,
    GateVector,
)
from v8_next.evaluation.gate_resolution import (
    WindowBinding,
    evaluate_g3_scenario_robustness,
    evaluate_g7_prospective_shadow,
    evaluate_g8_live_realization,
    evaluate_g9_certificate_authority,
)

H = 3_600_000_000_000
BASE_NS = 1_700_000_000_000_000_000
REGIMES = ("Bull Trend", "Bear Crash", "Chop/Range", "High-Vol Spill")


def candle(index: int, price: float) -> Candle:
    value = Decimal(str(price))
    start = BASE_NS + index * H
    return Candle(
        "BTCUSDT-PERP.BINANCE",
        start,
        start + H,
        value,
        value,
        value,
        value,
        Decimal("1"),
        start + H,
        start + H,
        "mechanics-only",
    )


def _regimes(price: float = 50.0) -> dict[str, list[Candle]]:
    return {name: [candle(i, price)] for i, name in enumerate(REGIMES)}


def _open(qty: str = "100") -> dict:
    return {
        "position_id": "P-1",
        "side": "LONG",
        "quantity": qty,
        "avg_px_open": "100",
        "event_ns": BASE_NS,
    }


def _closed(pnl: str = "-5000 USDT", ts: int = BASE_NS + H) -> dict:
    return {
        "position_id": "P-1",
        "realized_pnl": pnl,
        "event_ns": ts,
    }


def _run_g3(monkeypatch, opened, closed, **kwargs):
    monkeypatch.setattr(
        gr,
        "run_expert_strategy_backtest",
        lambda *args, **kw: {
            "opened_positions": opened,
            "closed_positions": closed,
            "account": {"balance_total": "10000 USDT"},
        },
    )
    return evaluate_g3_scenario_robustness(_regimes(), **kwargs)


def test_g3_equal_loss_has_equal_drawdown_open_and_closed(monkeypatch) -> None:
    open_state, open_metrics = _run_g3(monkeypatch, [_open()], [])
    closed_state, closed_metrics = _run_g3(monkeypatch, [_open()], [_closed()])
    assert open_state == closed_state == gr.GateState.BLOCKED
    assert open_metrics["max_observed_drawdown"] == pytest.approx(0.5005, abs=1e-4)
    assert closed_metrics["max_observed_drawdown"] == pytest.approx(0.5, abs=1e-6)
    assert open_metrics["pnl_unit"] == "USDT"
    assert open_metrics["return_unit"] == "fraction_of_initial_equity"
    assert open_metrics["regimes"]["Bull Trend"]["open_mtm_usdt"] == pytest.approx(-5005.0)
    assert open_metrics["regimes"]["Bull Trend"]["realized_pnl_usdt"] == 0.0


def test_g3_short_mark_and_size_scale_in_usdt(monkeypatch) -> None:
    short = {**_open(), "side": "SHORT", "quantity": "10"}
    _, metrics = _run_g3(monkeypatch, [short], [])
    # short from 100 to 50 gains 500 USDT gross; 0.5 USDT entry fee is charged
    assert metrics["regimes"]["Bull Trend"]["open_mtm_usdt"] == pytest.approx(499.5)
    _, doubled = _run_g3(monkeypatch, [{**_open(), "quantity": "200"}], [])
    assert doubled["regimes"]["Bull Trend"]["open_mtm_usdt"] == pytest.approx(-10010.0)


def test_g3_fee_changes_equity(monkeypatch) -> None:
    _, free = _run_g3(monkeypatch, [_open()], [], taker_fee=0.0)
    _, charged = _run_g3(monkeypatch, [_open()], [], taker_fee=0.01)
    assert charged["regimes"]["Bull Trend"]["final_equity_usdt"] < free["regimes"]["Bull Trend"]["final_equity_usdt"]


def test_g3_observations_are_chronological(monkeypatch) -> None:
    opened = [
        {**_open(), "position_id": "P-1", "event_ns": BASE_NS},
        {**_open(), "position_id": "P-2", "event_ns": BASE_NS + H},
    ]
    closed = [
        {**_closed("2000 USDT", BASE_NS + H), "position_id": "P-1"},
        {**_closed("-3000 USDT", BASE_NS + 2 * H), "position_id": "P-2"},
    ]
    _, metrics = _run_g3(monkeypatch, opened, closed)
    # chronological: +2000 (peak 12000) then -3000 -> 9000, dd = 0.25.
    # monetary-order inversion would give -3000 then +2000 -> 0.30.
    assert metrics["max_observed_drawdown"] == pytest.approx(0.25, abs=1e-9)
    assert metrics["accounting_measured"] is True


def test_g3_missing_open_quantity_blocks(monkeypatch) -> None:
    state, metrics = _run_g3(monkeypatch, [{"position_id": "P", "side": "LONG", "avg_px_open": "100"}], [])
    assert state == gr.GateState.BLOCKED
    assert "ACCOUNTING_EVIDENCE_MISSING" in metrics["reason"]
    assert metrics["passed"] is False


def test_g3_does_not_double_count_a_close(monkeypatch) -> None:
    orphan = {**_closed(), "position_id": "P-NO-OPEN"}
    state, metrics = _run_g3(monkeypatch, [_open()], [_closed(), orphan])
    assert state == gr.GateState.BLOCKED
    assert any("ORPHAN_CLOSE" in problem for problem in metrics["accounting_problems"])


def test_g7_zero_exposure_and_always_wrong_are_blocked(monkeypatch) -> None:
    shadow = tuple(candle(1000 + i, 100 * (0.9995**i)) for i in range(100))
    scored = tuple(candle(i, 100) for i in range(50))
    window = WindowBinding.from_candles(shadow, origin="probe")

    def candidate(decisions):
        monkeypatch.setattr(
            gr,
            "run_expert_strategy_backtest",
            lambda *a, **k: {
                "decisions": decisions,
                "opened_positions": [],
                "closed_positions": [],
                "account": {"balance_total": "10000 USDT"},
            },
        )
        return evaluate_g7_prospective_shadow(
            scored,
            output_dir=Path("/tmp/g7-integrity"),
            shadow_stream=shadow,
            shadow_origin="MECHANICS_ONLY",
            shadow_window=window,
            provenance_verified=True,
        )

    idle_state, idle = candidate([])
    assert idle_state == gr.GateState.BLOCKED
    assert idle["evidence_state"] == "NO_DECISIONS"

    wrong_state, wrong = candidate(
        [
            {"action": "ENTER", "consensus_supported": True, "opportunity": {"direction": "LONG"}}
            for _ in range(100)
        ]
    )
    assert wrong_state == gr.GateState.BLOCKED
    assert wrong["evidence_state"] == "EVIDENCE_AGAINST_CANDIDATE"
    assert wrong["net_return"] < 0

    good_but_unverified_state, good_but_unverified = candidate(
        [
            {"action": "ENTER", "consensus_supported": True, "opportunity": {"direction": "LONG"}}
            for _ in range(100)
        ]
    )
    assert good_but_unverified_state != gr.GateState.PASS
    assert good_but_unverified["provenance_verified"] is True


def test_g7_unverified_provenance_cannot_pass(monkeypatch) -> None:
    shadow = tuple(candle(1000 + i, 100 * (1.0001**i)) for i in range(100))
    scored = tuple(candle(i, 100) for i in range(50))
    monkeypatch.setattr(
        gr,
        "run_expert_strategy_backtest",
        lambda *a, **k: {
            "decisions": [
                {"action": "ENTER", "consensus_supported": True, "opportunity": {"direction": "LONG"}}
                for _ in range(100)
            ],
            "opened_positions": [],
            "closed_positions": [],
            "account": {"balance_total": "10000 USDT"},
        },
    )
    state, metrics = evaluate_g7_prospective_shadow(
        scored,
        output_dir=Path("/tmp/g7-unverified"),
        shadow_stream=shadow,
        shadow_origin="CALLER_DECLARED",
        shadow_window=WindowBinding.from_candles(shadow),
        provenance_verified=False,
    )
    assert state == gr.GateState.BLOCKED
    assert metrics["evidence_state"] == "UNVERIFIED_PROVENANCE"


# ---------------------------------------------------------------------------
# G8 trust boundary
# ---------------------------------------------------------------------------


def _settled_fills() -> list[dict]:
    return [
        {
            "fill_id": "f1",
            "instrument": "BTCUSDT",
            "price": 100.0,
            "qty": 1.0,
            "side": "BUY",
            "venue_time_ns": 1_700_000_000_000_000_000,
            "order_id": "o1",
        }
    ]


def _write_fills(path: Path, records: list[dict]) -> str:
    body = "".join(json.dumps(record) + "\n" for record in records)
    path.write_text(body)
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _account(
    *, qty: str = "1", account_id: str = "acct-1", venue: str = "BINANCE"
) -> dict:
    return {
        "account_id": account_id,
        "balance_total": "10000 USDT",
        "positions": [
            {"side": "LONG", "quantity": qty, "instrument_id": "BTCUSDT-PERP.BINANCE"}
        ],
        "orders": [{"status": "FILLED"}],
        "currency": "USDT",
        "venue": venue,
    }


def _attest(tmp_path: Path, sha: str, key: str = "test-key") -> Path:
    receipt = make_venue_attestation(fills_sha256=sha, account_id="acct-1", key=key)
    path = tmp_path / "fills.attestation.json"
    path.write_text(json.dumps(receipt))
    return path


def test_g8_caller_label_alone_is_unverified(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv(ATTESTATION_KEY_ENV, "test-key")
    fills = tmp_path / "fills.jsonl"
    _write_fills(fills, _settled_fills())
    state, metrics = evaluate_g8_live_realization(
        fills, provenance="authenticated_venue_statement", account_id="acct-1", account=_account()
    )
    assert state == gr.GateState.BLOCKED
    assert metrics["mode"] == "UNVERIFIED_PROVENANCE"
    assert metrics["passed"] is False


def test_g8_verified_attestation_still_requires_reconciliation(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv(ATTESTATION_KEY_ENV, "test-key")
    fills = tmp_path / "fills.jsonl"
    sha = _write_fills(fills, _settled_fills())
    attestation = _attest(tmp_path, sha)
    state, metrics = evaluate_g8_live_realization(
        fills,
        provenance="authenticated_venue_statement",
        account_id="acct-1",
        attestation_path=attestation,
        account=None,
    )
    assert state == gr.GateState.BLOCKED
    assert metrics["mode"] == "RECONCILIATION_MISSING"


def test_g8_mismatched_reconciliation_cannot_pass(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv(ATTESTATION_KEY_ENV, "test-key")
    fills = tmp_path / "fills.jsonl"
    sha = _write_fills(fills, _settled_fills())
    attestation = _attest(tmp_path, sha)
    state, metrics = evaluate_g8_live_realization(
        fills,
        provenance="authenticated_venue_statement",
        account_id="acct-1",
        attestation_path=attestation,
        account=_account(qty="2"),
    )
    assert state == gr.GateState.BLOCKED
    assert metrics["mode"] == "RECONCILIATION_FAILED"
    assert metrics["account_reconciled"] is False


def test_g8_verified_and_reconciled_can_pass(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv(ATTESTATION_KEY_ENV, "test-key")
    fills = tmp_path / "fills.jsonl"
    sha = _write_fills(fills, _settled_fills())
    attestation = _attest(tmp_path, sha)
    state, metrics = evaluate_g8_live_realization(
        fills,
        provenance="authenticated_venue_statement",
        account_id="acct-1",
        attestation_path=attestation,
        account=_account(),
    )
    assert state == gr.GateState.PASS
    assert metrics["mode"] == "LIVE_VENUE_SETTLED"
    assert metrics["account_reconciled"] is True


def test_g8_tampered_attestation_is_rejected(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv(ATTESTATION_KEY_ENV, "test-key")
    fills = tmp_path / "fills.jsonl"
    sha = _write_fills(fills, _settled_fills())
    path = _attest(tmp_path, sha)
    receipt = json.loads(path.read_text())
    receipt["fills_sha256"] = "0" * 64
    path.write_text(json.dumps(receipt))
    state, metrics = evaluate_g8_live_realization(
        fills,
        provenance="authenticated_venue_statement",
        account_id="acct-1",
        attestation_path=path,
        account=_account(),
    )
    assert state == gr.GateState.BLOCKED
    assert metrics["mode"] == "UNVERIFIED_PROVENANCE"


def test_g8_duplicate_fill_ids_are_malformed(tmp_path: Path) -> None:
    fills = tmp_path / "fills.jsonl"
    records = _settled_fills() * 2
    body = "".join(json.dumps(record) + "\n" for record in records)
    fills.write_text(body)
    loaded, meta = load_shadow_fills(fills)
    assert loaded == []
    assert meta["mode"] == "MALFORMED_SHADOW_FILE"
    assert meta["reason"] == "DUPLICATE_FILL_IDS"


def test_g8_wrong_venue_and_instrument_cannot_pass(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv(ATTESTATION_KEY_ENV, "test-key")
    fills = tmp_path / "fills.jsonl"
    sha = _write_fills(fills, _settled_fills())
    attestation = _attest(tmp_path, sha)
    for account in (
        _account(venue="OKX"),
        {**_account(), "positions": [{"side": "LONG", "quantity": "1", "instrument_id": "ETHUSDT-PERP.BINANCE"}]},
    ):
        state, metrics = evaluate_g8_live_realization(
            fills,
            provenance="authenticated_venue_statement",
            account_id="acct-1",
            attestation_path=attestation,
            account=account,
        )
        assert state == gr.GateState.BLOCKED
        assert metrics["mode"] == "RECONCILIATION_FAILED"


def test_g8_invalid_settlement_fields_are_rejected(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv(ATTESTATION_KEY_ENV, "test-key")
    for field, value, reason in (
        ("side", "SIDEWAYS", "SETTLEMENT_SIDE_INVALID"),
        ("venue_time_ns", "not-a-time", "SETTLEMENT_VENUE_TIME_INVALID"),
        ("order_id", "", "SETTLEMENT_ORDER_ID_INVALID"),
    ):
        records = _settled_fills()
        records[0][field] = value
        fills = tmp_path / f"bad-{field}.jsonl"
        sha = _write_fills(fills, records)
        receipt = make_venue_attestation(fills_sha256=sha, account_id="acct-1", key="test-key")
        path = tmp_path / f"bad-{field}.attestation.json"
        path.write_text(json.dumps(receipt))
        state, metrics = evaluate_g8_live_realization(
            fills,
            provenance="authenticated_venue_statement",
            account_id="acct-1",
            attestation_path=path,
            account=_account(),
        )
        assert state == gr.GateState.BLOCKED
        assert reason in metrics["reason"]


def test_unverified_g8_cannot_mint_a_live_certified_claim(tmp_path: Path) -> None:
    """Gate-to-claim boundary: G8 BLOCKED cannot become SUPPORTED_EDGE."""
    gates = GateVector(
        g0_identity=GateState.PASS,
        g1_causal_pit=GateState.PASS,
        g2_determinism_ledger=GateState.PASS,
        g3_scenario_robustness=GateState.PASS,
        g4_structural_robustness=GateState.PASS,
        g5_statistical_credibility=GateState.PASS,
        g6_protected_oos=GateState.PASS,
        g7_generalization=GateState.PASS,
        g8_prospective_shadow=GateState.BLOCKED,
        g9_live_realization=GateState.MISSING,
    )
    ledger = BenchmarkLedger.load_jsonl(tmp_path / "ledger.jsonl")
    receipt = BenchmarkReceipt.create(
        case_id="BC-G8-BOUNDARY",
        policy_id="pol_test",
        capability_score=None,
        gates=gates,
        computed_at_timestamp_ns=1000,
    )
    ledger.append(receipt)
    state, metrics, claim = evaluate_g9_certificate_authority(
        ledger=ledger,
        receipt_digest=receipt.receipt_digest,
        gates=gates,
        capability_score=None,
        output_dir=tmp_path,
        live_realization_verified=True,
    )
    assert state == GateState.BLOCKED
    assert claim is None
    assert "G8" in metrics["error"]


def test_g8_public_paper_remains_diagnostic(tmp_path: Path) -> None:
    fills = tmp_path / "public.jsonl"
    _write_fills(fills, _settled_fills())
    loaded, meta = load_shadow_fills(fills)
    assert loaded
    assert meta["mode"] == MODE_PUBLIC_PAPER
    assert meta["authority"] == "NONE"
