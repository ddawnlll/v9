"""Focused tests for the reusable research harness.

These tests pin the *contract* of the research layer: preregistration rules,
causal timing rules and the "missing is null, never zero" rule. They use small
deterministic fixtures and never touch the production replay path.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from v8_next.research import arms as research_arms
from v8_next.research import contract as research_contract
from v8_next.research import metrics as research_metrics
from v8_next.research import screen


def _bars(rows: list[tuple[float, float, float, float]], step_ns: int = 3_600_000_000_000):
    return [
        screen.Bar(
            end_ns=(index + 1) * step_ns,
            open=float(o),
            high=float(h),
            low=float(low),
            close=float(c),
            volume=1.0,
        )
        for index, (o, h, low, c) in enumerate(rows)
    ]


# --------------------------------------------------------------------------- #
# contract
# --------------------------------------------------------------------------- #
def _minimal_contract() -> dict:
    return {
        "contract_version": "t",
        "research_run_id": "r",
        "source_revision": "rev",
        "dataset_ids": {"d": "path"},
        "authorized_dev_status": "DEV_ONLY",
        "instrument_universe": ["BTCUSDT"],
        "time_windows": {},
        "bar_granularity": "1h",
        "execution_profile": {},
        "account_configuration": {},
        "risk_convention": {},
        "fee_convention": {"maker": "0.0002", "taker": "0.0005"},
        "slippage_convention": {},
        "funding_convention": "UNAVAILABLE",
        "intrabar_ordering_convention": "STOP_FIRST",
        "baseline_policy": {},
        "experiment_arms": {"A": {"arms": [{"id": "A0", "kind": "native", "is_baseline": True}]}},
        "fixed_parameters": {},
        "metrics": ["net_expectancy_r"],
        "rejection_criteria": {"statistical": []},
        "statistical_method": {},
        "random_seeds": {"bootstrap": 7},
        "maximum_replays": 4,
        "maximum_parameter_variations": 3,
    }


def test_contract_validates_and_hashes_deterministically() -> None:
    contract = _minimal_contract()
    assert research_contract.validate_contract(contract) == []
    first = research_contract.contract_sha256(contract)
    second = research_contract.contract_sha256(dict(reversed(list(contract.items()))))
    assert first == second
    assert len(first) == 64


def test_contract_rejects_missing_declared_assumptions() -> None:
    contract = _minimal_contract()
    del contract["funding_convention"]
    del contract["intrabar_ordering_convention"]
    problems = research_contract.validate_contract(contract)
    assert any("funding_convention" in p for p in problems)
    assert any("intrabar_ordering_convention" in p for p in problems)


def test_contract_rejects_arm_and_replay_budget_overrun() -> None:
    contract = _minimal_contract()
    over_arms = research_contract.MAX_ARMS_PER_EXPERIMENT + 1
    over_native = research_contract.MAX_NON_BASELINE_REPLAYS_PER_EXPERIMENT + 1
    contract["experiment_arms"] = {
        "A": {
            "arms": [
                {"id": "A0", "kind": "native", "is_baseline": True},
                *[
                    {"id": f"A{i}", "kind": "native", "is_baseline": False}
                    for i in range(1, over_native + 1)
                ],
                *[
                    {"id": f"S{i}", "kind": "screen", "is_baseline": False}
                    for i in range(over_arms)
                ],
            ]
        }
    }
    problems = research_contract.validate_contract(contract)
    assert any("exceeds MAX_ARMS_PER_EXPERIMENT" in p for p in problems)
    assert any("non-baseline native" in p for p in problems)


def test_arm_registry_respects_preregistered_budgets() -> None:
    for experiment in ("A", "B", "C", "D"):
        arms = research_arms.arms_for(experiment)
        assert 1 <= len(arms) <= research_contract.MAX_ARMS_PER_EXPERIMENT
        native = [a for a in arms if a.kind == "native" and not a.is_baseline]
        assert len(native) <= research_contract.MAX_NON_BASELINE_REPLAYS_PER_EXPERIMENT
        assert sum(1 for a in arms if a.is_baseline) == 1


# --------------------------------------------------------------------------- #
# causal screening rules
# --------------------------------------------------------------------------- #
def test_entry_bar_extremes_are_not_counted_as_excursion() -> None:
    bars = _bars([(100, 150, 40, 100), (100, 104, 96, 102)])
    result = screen.evaluate_path(
        bars,
        entry_ns=bars[0].end_ns,
        entry=100.0,
        direction=1,
        stop=90.0,
        target=120.0,
    )
    # Only the post-entry bar counts: mark-to-market at its close (+2 on 10 risk).
    assert result["gross_r"] == pytest.approx(0.2)
    assert result["bars_measured"] == 1
    assert result["outcome"] == "OPEN_AT_CUTOFF_MTM"


def test_breakeven_stop_becomes_effective_only_on_the_next_bar() -> None:
    # Bar 1 touches +1R (110) but also sweeps to the original stop (90) in the
    # same bar. The stop may not be moved before that information is known, so
    # the campaign still exits at the ORIGINAL stop.
    bars = _bars([
        (100, 100, 100, 100),
        (100, 112, 88, 100),
        (100, 112, 88, 100),
    ])
    result = screen.evaluate_path(
        bars,
        entry_ns=bars[0].end_ns,
        entry=100.0,
        direction=1,
        stop=90.0,
        target=130.0,
        policy="BREAKEVEN_1R",
    )
    assert result["outcome"] == "STOP"
    assert result["net_r"] == pytest.approx(-1.0, abs=0.02)


def test_breakeven_protects_a_later_reversal() -> None:
    bars = _bars([
        (100, 100, 100, 100),
        (100, 111, 100, 110),   # trigger +1R, no stop breach
        (110, 112, 95, 96),     # reversal: stop is now at breakeven
    ])
    result = screen.evaluate_path(
        bars,
        entry_ns=bars[0].end_ns,
        entry=100.0,
        direction=1,
        stop=90.0,
        target=130.0,
        policy="BREAKEVEN_1R",
    )
    assert result["stop_moved"] is True
    assert result["outcome"] == "BREAKEVEN_STOP"
    # Breakeven sits one round-trip of taker fees above entry, not exactly at it.
    assert result["gross_r"] == pytest.approx(0.0, abs=0.02)


def test_retest_lifecycle_expires_without_a_touch_and_keeps_the_denominator() -> None:
    bars = _bars([
        (100, 100, 100, 100),
        (101, 103, 101, 102),
        (102, 104, 102, 103),
    ])
    opportunities = [
        {"opportunity_id": "O1", "direction": "LONG", "event_time_ns": bars[0].end_ns,
         "entry_price": 100.0},
    ]
    rows = screen.retest_rows(opportunities, bars, ttl_bars=2)
    assert len(rows) == 1
    assert rows[0]["outcome"] == "EXPIRED"
    assert rows[0]["entry_price"] is None
    assert rows[0]["net_r"] is None


def test_retest_lifecycle_fills_on_touch_and_reports_the_model() -> None:
    bars = _bars([
        (100, 100, 100, 100),
        (100, 101, 99, 100),    # touches 100 -> fill
        (100, 105, 100, 104),
    ])
    opportunities = [
        {"opportunity_id": "O2", "direction": "LONG", "event_time_ns": bars[0].end_ns,
         "entry_price": 100.0},
    ]
    rows = screen.retest_rows(opportunities, bars, ttl_bars=2)
    assert rows[0]["state"] == "ENTERED"
    assert rows[0]["limit_fill_model"] == "TOUCH_FILL_MODEL"
    assert rows[0]["bars_watched"] == 1


def test_ambiguous_stop_and_target_bar_is_stop_first() -> None:
    bars = _bars([(100, 100, 100, 100), (100, 120, 80, 100)])
    result = screen.evaluate_path(
        bars, entry_ns=bars[0].end_ns, entry=100.0, direction=1, stop=90.0, target=110.0
    )
    assert result["outcome"] == "STOP"
    assert result["net_r"] == pytest.approx(-1.0, abs=0.02)


# --------------------------------------------------------------------------- #
# metrics
# --------------------------------------------------------------------------- #
def test_missing_values_stay_null_and_never_become_zero() -> None:
    summary = research_metrics.summarize([{"realized_net_r": None, "direction": "LONG"}])
    assert summary["net_expectancy_r"] is None
    assert summary["net_profit_factor"] is None
    assert summary["trade_count"] == 1


def test_bootstrap_ci_is_deterministic_and_refuses_tiny_samples() -> None:
    values = [1.0, -0.5, 0.4, -0.2, 0.9, 0.1]
    first = research_metrics.bootstrap_mean_ci(values, seed=7, resamples=200)
    second = research_metrics.bootstrap_mean_ci(values, seed=7, resamples=200)
    assert first == second
    assert first is not None and first["n"] == len(values)
    assert research_metrics.bootstrap_mean_ci([1.0, 2.0], seed=7) is None


def test_common_result_rejects_an_illegal_status() -> None:
    base = {"trade_count": 1}
    with pytest.raises(ValueError):
        research_metrics.common_result(
            experiment_id="X",
            status="PROFITABLE",
            baseline_policy_id="b",
            candidate_policy_id="c",
            dataset_id="d",
            baseline_metrics=base,
        )
    row = research_metrics.common_result(
        experiment_id="X",
        status="INCONCLUSIVE",
        baseline_policy_id="b",
        candidate_policy_id="c",
        dataset_id="d",
        baseline_metrics=base,
    )
    assert row["net_expectancy_r_candidate"] is None


def test_load_bars_ignores_unclosed_and_foreign_instruments(tmp_path: Path) -> None:
    tape = tmp_path / "tape.jsonl"
    tape.write_text(
        "\n".join(
            [
                '{"channel":"kline","instrument":"BTCUSDT","event_time":3600000000000,'
                '"payload":{"open":1,"high":2,"low":0.5,"close":1.5,"volume":1,"closed":true}}',
                '{"channel":"kline","instrument":"BTCUSDT","event_time":7200000000000,'
                '"payload":{"open":1,"high":2,"low":0.5,"close":1.5,"volume":1,"closed":false}}',
                '{"channel":"kline","instrument":"ETHUSDT","event_time":3600000000000,'
                '"payload":{"open":1,"high":2,"low":0.5,"close":1.5,"volume":1,"closed":true}}',
                '{"channel":"funding","instrument":"BTCUSDT","event_time":1,"payload":{}}',
            ]
        ),
        encoding="utf-8",
    )
    bars = screen.load_bars(tape)
    assert len(bars) == 1
    assert bars[0].close == 1.5


# --------------------------------------------------------------------------- #
# research hooks on the native strategy (default-off guarantees)
# --------------------------------------------------------------------------- #
def test_research_hooks_are_disabled_by_default() -> None:
    from v8_next.adapters.expert_strategy import ExpertStrategyConfig

    config = ExpertStrategyConfig()
    assert config.research_exit_policy is None
    assert config.research_require_confirmation is False


def test_unknown_research_exit_policy_fails_closed() -> None:
    from v8_next.adapters.expert_strategy import ExpertStrategyConfig

    with pytest.raises(ValueError):
        ExpertStrategyConfig(research_exit_policy="TRAILING_MAGIC")
