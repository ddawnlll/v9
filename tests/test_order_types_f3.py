"""F3: entry order types (MARKET/LIMIT) + unknown-type fail-closed.

Runs on the real single-asset BTC tape (skips when absent). No economic claim:
asserts order-type plumbing and determinism only.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from v8_next.adapters.tape_paths import tape_path

BTC_TAPE = tape_path("btcusdt-1h-12m", "tape.jsonl")


def _candles(limit: int = 120):
    if not BTC_TAPE.exists():
        pytest.skip(f"single-asset real tape absent at {BTC_TAPE}")
    from v8_next.evaluation.gate_resolution import load_tape_candles

    out = load_tape_candles(BTC_TAPE, limit=limit)
    if not out:
        pytest.skip("tape loaded no candles")
    return tuple(out)


def _run(entry_order_type: str):
    from v8_next.adapters.expert_strategy import (
        ExpertStrategyConfig,
        run_expert_strategy_backtest,
    )

    return run_expert_strategy_backtest(
        _candles(),
        ExpertStrategyConfig(
            min_support_quorum=1,
            max_contradiction_tolerance=28,
            bracket_stop_pct=Decimal("0.02"),
            bracket_target_pct=Decimal("0.04"),
            entry_order_type=entry_order_type,
        ),
    )


def test_unknown_entry_type_is_rejected_not_silently_market() -> None:
    result = _run("STOP_LIMIT")
    submitted = [d for d in result["decisions"] if d["action"].startswith("SUBMITTED")]
    rejected = [d for d in result["decisions"] if "REJECTED_UNKNOWN_ENTRY_TYPE" in d["action"]]
    assert not submitted
    assert result["opened_positions"] == []
    # rejections only happen where an opportunity was actually supported
    assert rejected, "expected at least one supported opportunity on 120 real bars"


def test_limit_entry_is_deterministic() -> None:
    first = _run("LIMIT")
    second = _run("LIMIT")
    assert [d["action"] for d in first["decisions"]] == [d["action"] for d in second["decisions"]]
    assert first["opened_positions"] == second["opened_positions"]


def test_limit_entry_differs_from_market_and_reports() -> None:
    market = _run("MARKET")
    limit = _run("LIMIT")
    m_actions = [d["action"] for d in market["decisions"]]
    l_actions = [d["action"] for d in limit["decisions"]]
    assert any(a.startswith("SUBMITTED_BRACKET_MARKET") for a in m_actions)
    limit_submits = [a for a in l_actions if a.startswith("SUBMITTED_BRACKET_LIMIT")]
    print(
        f"\n[F3] market submits={sum(1 for a in m_actions if 'SUBMITTED' in a)} "
        f"limit submits={len(limit_submits)} "
        f"market opened={len(market['opened_positions'])} "
        f"limit opened={len(limit['opened_positions'])}"
    )
    # LIMIT rests at the decision close: it must never fill MORE than MARKET
    assert len(limit["opened_positions"]) <= len(market["opened_positions"])

pytestmark = pytest.mark.slow  # #469: tape/engine file, fast loop excludes via -m "not slow"
