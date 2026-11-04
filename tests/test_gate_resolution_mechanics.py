"""Fast mechanics tests for gate fail-closed and causal-alignment fixes."""

import json
from decimal import Decimal

from v8_next.domain.market import Candle
from v8_next.evaluation import gate_resolution as gr
from v8_next.evaluation.benchmark_receipt import GateState


def test_account_balance_absence_is_none_not_a_fabricated_ten_thousand() -> None:
    assert gr.extract_account_balance({}) is None
    assert gr.extract_account_balance({"balance_total": "not-a-number"}) is None
    assert gr.extract_account_balance({"balance_total": 0.0}) == 0.0
    assert gr.extract_account_balance({"balance_total": "0 USDT"}) == 0.0


def test_g4_missing_balance_blocks_instead_of_passing(monkeypatch) -> None:
    monkeypatch.setattr(
        gr,
        "run_expert_strategy_backtest",
        lambda *args, **kwargs: {
            "account": {},
            "opened_positions": [{}],
            "closed_positions": [{}, {}],
        },
    )
    state, metrics = gr.evaluate_g4_synthetic_falsification(())
    assert state == GateState.BLOCKED
    assert metrics["measurement"] == "unmeasured"
    assert metrics["final_equity"] is None
    assert "ACCOUNT_BALANCE_UNAVAILABLE" in metrics["reason"]


def _candle(index: int, price: Decimal, start_ns: int) -> Candle:
    return Candle(
        "BTCUSDT-PERP.BINANCE",
        start_ns + index * 3_600 * 10**9,
        start_ns + (index + 1) * 3_600 * 10**9,
        price,
        price,
        price,
        price,
        Decimal("1"),
        start_ns + (index + 1) * 3_600 * 10**9,
        start_ns + (index + 1) * 3_600 * 10**9,
        f"hash-{start_ns}-{index}",
    )


def test_g7_does_not_credit_the_same_bar_that_formed_the_decision(monkeypatch, tmp_path) -> None:
    scored = [_candle(0, Decimal("100"), 0)]
    shadow = [
        _candle(i, Decimal("100") if i % 2 == 0 else Decimal("102"), 100 * 3_600 * 10**9)
        for i in range(100)
    ]
    monkeypatch.setattr(
        gr,
        "run_expert_strategy_backtest",
        lambda *args, **kwargs: {
            "decisions": [
                {
                    "action": "SUBMITTED_MARKET",
                    "consensus_supported": True,
                    "opportunity": {"direction": "LONG"},
                }
                for _ in shadow
            ]
        },
    )
    _, metrics = gr.evaluate_g7_prospective_shadow(
        scored,
        output_dir=tmp_path,
        shadow_stream=shadow,
        shadow_origin="MECHANICS_ONLY",
        shadow_window=gr.WindowBinding.from_candles(shadow),
    )
    rows = [
        json.loads(line)
        for line in (tmp_path / "g7_prospective_shadow.jsonl").read_text().splitlines()
        if line
    ]
    # Bar 0 has no prior actionable decision. Bar 1 may use bar 0's decision.
    assert rows[0]["exposure"] == 0
    assert rows[1]["exposure"] == 1
    assert metrics["candidate_exposure_bars"] == 99
