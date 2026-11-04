"""F4: TWAP child-order execution.

Runs on the real single-asset BTC tape (skips when absent). Asserts the parent
is split into step-aligned children whose sum equals the parent, children drain
on consecutive bars, and the whole run is deterministic.
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


def _run(algo: str, slices: int = 4):
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
            execution_algo=algo,
            twap_slices=slices,
        ),
    )


def test_twap_parent_splits_and_children_sum_to_parent() -> None:

    result = _run("TWAP", slices=4)
    actions = [d["action"] for d in result["decisions"]]
    parents = [a for a in actions if a.startswith("TWAP_PARENT_1/")]
    children = [a for a in actions if a.startswith("TWAP_CHILD_")]
    assert parents, "expected a TWAP parent on 120 real bars"
    total = int(parents[0].split("/")[1].split("_")[0])
    assert total == 4
    assert len(children) == total - 1
    # children drain in order on consecutive bars
    assert children == [f"TWAP_CHILD_{i}/4" for i in range(2, 5)]
    print(f"\n[F4] parents={len(parents)} children={children}")


def test_twap_run_is_deterministic() -> None:
    first = _run("TWAP", slices=4)
    second = _run("TWAP", slices=4)
    assert [d["action"] for d in first["decisions"]] == [
        d["action"] for d in second["decisions"]
    ]
    assert first["opened_positions"] == second["opened_positions"]


def test_none_algo_emits_no_twap_actions() -> None:
    result = _run("NONE")
    assert not [a for a in (d["action"] for d in result["decisions"]) if "TWAP" in a]


def test_twap_slices_sum_to_parent_and_respect_step() -> None:
    from v8_next.adapters.expert_strategy import ExpertEnsembleStrategy

    strat = ExpertEnsembleStrategy.__new__(ExpertEnsembleStrategy)
    slices = ExpertEnsembleStrategy._twap_slices(strat, Decimal("0.010"), Decimal("0.001"), 4)
    assert sum(slices) == Decimal("0.010")
    assert all((s / Decimal("0.001")) == int(s / Decimal("0.001")) for s in slices)
    assert ExpertEnsembleStrategy._twap_slices(strat, Decimal("0.010"), Decimal("0.001"), 1) == [
        Decimal("0.010")
    ]

pytestmark = pytest.mark.slow  # #469: tape/engine file, fast loop excludes via -m "not slow"
