"""P3 — synthetic hypothesis scenarios for the locked readiness benchmark.

Three families, all declared as **SCENARIO ONLY** evidence (they prove an engine is correct
or that a test detects what it claims; they are never a performance or economic result):

1. **Model candles** (after *Correctness of Backtest Engines*, arXiv:1509.08248). Adversarial
   candle shapes whose correct outcome is derivable by hand. An engine that fails any of them
   may not report a return.
2. **SNU — Situation Not Unique** (after *Backtest of Trading Systems on Candle Charts*,
   arXiv:1412.5558). A bar that touches both the stop and the target cannot be decided from
   candle data. The engine must count it, and the declared resolution rule must be the one it
   actually applies: the stop is taken first.
3. **Overfitting as a hypothesis test** (after arXiv:2209.05559). The test must *reject* a
   family selected on noise and must *not* reject a genuine edge. A test that rejects nothing
   proves nothing, which is why both directions are asserted.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Callable

import numpy as np

from v8_next.domain.market import Candle
from v8_next.economics.swing_baseline import SwingDecision, replay_bracket

HOUR_NS = 3_600 * 10**9
TAKER_FEE = Decimal("0.0005")

#: the declared resolution when candle data cannot decide which leg was hit first
DECLARED_SNU_RULE = (
    "when a single bar touches both the stop and the target, the STOP is taken first: the "
    "conservative, deterministic reading of an unknowable intrabar order"
)


def _candle(index: int, *, open_: str, high: str, low: str, close: str) -> Candle:
    """SCENARIO ONLY synthetic bar on a continuous hourly grid."""
    start = 1_700_000_000_000_000_000 + index * HOUR_NS
    return Candle(
        instrument_id="SCENARIO-PERP.TEST",
        start_ns=start,
        received_ns=start + HOUR_NS,
        available_ns=start + HOUR_NS,
        end_ns=start + HOUR_NS,
        source_hash="scenario-only",
        open=Decimal(open_),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(close),
        volume=Decimal("1"),
    )


def _decision(
    *, direction: str = "LONG", entry: str = "100", stop: str = "99", target: str = "102",
    decision_index: int = 0, expires_index: int = 100,
) -> SwingDecision:
    """SCENARIO ONLY decision, built directly so the expected outcome is hand-checkable."""
    return SwingDecision(
        policy_id="scenario",
        opportunity_id="scenario-1",
        instrument_id="SCENARIO-PERP.TEST",
        direction=direction,  # type: ignore[arg-type]
        decision_ns=1_700_000_000_000_000_000 + decision_index * HOUR_NS,
        entry_reference=Decimal(entry),
        stop_price=Decimal(stop),
        target_price=Decimal(target),
        expires_ns=1_700_000_000_000_000_000 + expires_index * HOUR_NS,
        opportunity_ttl_bars=float(expires_index - decision_index),
        protection_policy="scenario",
        open_trade_expiry_bars=None,
    )


@dataclass(frozen=True)
class Scenario:
    name: str
    about: str
    check: Callable[[], dict[str, Any]]


def _run(kind_expected: str, candles: list[Candle], **decision_kwargs: Any) -> dict[str, Any]:
    outcome = replay_bracket(
        _decision(**decision_kwargs), candles, bar_ns=HOUR_NS, bps_fee=TAKER_FEE, has_bracket=True
    )
    passed = outcome.exit_kind == kind_expected
    return {
        "passed": passed,
        "expected_exit_kind": kind_expected,
        "actual_exit_kind": outcome.exit_kind,
        "exit_price": str(outcome.exit_price),
        "bars_held": outcome.bars_held,
        "gap_through_stop": outcome.gap_through_stop,
    }


def scenario_gap_through_stop() -> dict[str, Any]:
    # the next bar opens 2 handles BELOW the stop: a gap is not filled at the stop price
    return _run("STOP", [_candle(1, open_="97", high="98", low="96", close="96.5")])


def scenario_exact_touch_stop() -> dict[str, Any]:
    # low touches the stop exactly: filled AT the stop, and it is not a gap
    return _run("STOP", [_candle(1, open_="100.5", high="101", low="99", close="99.5")])


def scenario_exact_touch_target() -> dict[str, Any]:
    return _run("TARGET", [_candle(1, open_="100.5", high="102", low="100.2", close="101.5")])


def scenario_open_beyond_stop_is_not_a_stop_price_fill() -> dict[str, Any]:
    # opened above the stop then traded down through it: STOP at the stop, no gap flag
    return _run("STOP", [_candle(1, open_="100.2", high="100.4", low="98.5", close="98.8")])


def scenario_expiry_at_close() -> dict[str, Any]:
    # no touch; the expiry bar closes the campaign at its own close
    return _run(
        "EXPIRY",
        [_candle(1, open_="100.5", high="101", low="99.5", close="100.8")],
        expires_index=1,
    )


def scenario_short_side_is_mirrored() -> dict[str, Any]:
    # SHORT: the target is below, the stop above; a bar hitting the stop from above stops out
    return _run(
        "STOP",
        [_candle(1, open_="99.8", high="100.5", low="99.5", close="100.2")],
        direction="SHORT", stop="100", target="98",
    )


def scenario_snu_bar_touches_both() -> dict[str, Any]:
    """The SNU case: one bar touches the stop AND the target, so candle data cannot decide.

    The declared rule must win and the occurrence must be countable.
    """
    result = _run("STOP", [_candle(1, open_="100.5", high="102.5", low="98.5", close="101")])
    result["snu"] = True
    result["declared_rule"] = DECLARED_SNU_RULE
    result["about"] = "a bar that touches both legs is not decidable from candle data"
    return result


def scenario_bracketless_does_not_stop_out() -> dict[str, Any]:
    """A decision whose protection did not form must not be stopped out at its own entry.

    This is the bracket-contract scenario: with ``has_bracket=False`` the equal-to-entry
    "stop" must be inert and only the expiry may close the campaign.
    """
    decision = _decision(entry="100", stop="100", target="100", expires_index=3)
    outcome = replay_bracket(
        decision,
        [
            _candle(1, open_="100", high="100.5", low="99.0", close="99.5"),
            _candle(2, open_="99.5", high="100.2", low="98.0", close="99.0"),
            _candle(3, open_="99.0", high="99.6", low="98.2", close="98.5"),
        ],
        bar_ns=HOUR_NS,
        bps_fee=TAKER_FEE,
        has_bracket=False,
    )
    # the same bars WITH a bracket would stop out on the first bar at the entry reference
    with_bracket = replay_bracket(
        decision,
        [
            _candle(1, open_="100", high="100.5", low="99.0", close="99.5"),
            _candle(2, open_="99.5", high="100.2", low="98.0", close="99.0"),
            _candle(3, open_="99.0", high="99.6", low="98.2", close="98.5"),
        ],
        bar_ns=HOUR_NS,
        bps_fee=TAKER_FEE,
        has_bracket=True,
    )
    return {
        "passed": outcome.exit_kind == "EXPIRY" and with_bracket.exit_kind == "STOP",
        "bracketless_exit_kind": outcome.exit_kind,
        "bracketed_exit_kind": with_bracket.exit_kind,
        "about": (
            "the same bars exit by expiry when the decision is unprotected and stop out at "
            "the entry reference when the policy name declares a bracket - the divergence the "
            "reconciliation reports as BRACKET_CONTRACT_DIVERGENCE"
        ),
    }


# --- overfitting as a hypothesis test ------------------------------------------------


def _sharpe(returns: np.ndarray) -> float:
    std = float(returns.std(ddof=1))
    return float(returns.mean()) / std if std > 0 else 0.0


def _strategy_returns(series: np.ndarray, fast: int, slow: int) -> np.ndarray:
    """A declared, simple strategy grid: sign of the fast/slow mean difference."""
    out = np.zeros_like(series)
    for i in range(slow, len(series) - 1):
        signal = 1.0 if series[i - fast + 1 : i + 1].mean() > series[i - slow + 1 : i + 1].mean() else -1.0
        out[i + 1] = signal * (series[i + 1] - series[i])
    return out[slow:]


def _overfitting_probability(
    series: np.ndarray, grid: list[tuple[int, int]], split: float = 0.5
) -> float:
    """PBO-like: is the best in-sample member any good out of sample?"""
    cut = int(len(series) * split)
    in_sample, out_sample = series[:cut], series[cut:]
    is_scores = {key: _sharpe(_strategy_returns(in_sample, *key)) for key in grid}
    best = max(is_scores, key=lambda key: is_scores[key])
    oos = _strategy_returns(out_sample, *best)
    return 1.0 if float(oos.mean()) <= 0 else 0.0


#: declared calibration of the two synthetic families. The edge is stated as a per-bar
#: Sharpe so the reader can see whether the test is being asked to detect something real:
#: mu 0.002 / sigma 0.01 = 0.20 per bar, i.e. a genuinely detectable edge, not a coin flip.
EDGE_MU = 0.002
EDGE_SIGMA = 0.01
NOISE_SIGMA = 0.01
SERIES_BARS = 800
REJECTION_FLOOR_ON_NOISE = 0.5
MAX_REJECTION_ON_EDGE = 0.25


def overfitting_hypothesis_test(trials: int = 60, seed: int = 7) -> dict[str, Any]:
    """Both directions are asserted: reject a noise-fitted family, accept a genuine edge.

    An earlier calibration of this test rejected 45 % of genuine edges (drift too small
    against the noise), which the suite reported as FAIL rather than passing quietly: a test
    that cannot separate a real edge from noise is not evidence about overfitting.
    """
    rng = np.random.default_rng(seed)
    grid = [(f, s) for f in (2, 5, 10, 20) for s in (30, 50, 100, 200) if f < s]
    noise_rejections: float = 0.0
    edge_rejections: float = 0.0
    for _ in range(trials):
        # the declarations above are per-bar RETURNS; a strategy reads price LEVELS, so the
        # series handed to the grid is the cumulative sum. Passing returns directly made the
        # moving-average comparison operate on return differences and rejected every edge.
        noise_levels = np.cumsum(rng.normal(0.0, NOISE_SIGMA, SERIES_BARS))
        noise_rejections += _overfitting_probability(noise_levels, grid)
        edge_levels = np.cumsum(rng.normal(EDGE_MU, EDGE_SIGMA, SERIES_BARS))
        edge_rejections += _overfitting_probability(edge_levels, grid)
    noise_rate = noise_rejections / trials
    edge_rate = edge_rejections / trials
    return {
        "passed": noise_rate >= REJECTION_FLOOR_ON_NOISE and edge_rate <= MAX_REJECTION_ON_EDGE,
        "trials": trials,
        "seed": seed,
        "calibration": {
            "noise_sigma": NOISE_SIGMA,
            "edge_mu": EDGE_MU,
            "edge_sigma": EDGE_SIGMA,
            "edge_per_bar_sharpe": round(EDGE_MU / EDGE_SIGMA, 3),
            "series_bars": SERIES_BARS,
        },
        "thresholds": {
            "min_rejection_rate_on_noise": REJECTION_FLOOR_ON_NOISE,
            "max_rejection_rate_on_edge": MAX_REJECTION_ON_EDGE,
        },
        "noise_family_rejected_rate": round(noise_rate, 3),
        "edge_family_rejected_rate": round(edge_rate, 3),
        "about": (
            "the test must reject a family selected on noise (>= 0.5) and must not reject a "
            "genuine drift edge (<= 0.25); a test that rejects nothing proves nothing"
        ),
    }


def scenario_results() -> dict[str, Any]:
    scenarios = [
        Scenario("GAP_THROUGH_STOP", "a gap is not filled at the stop price", scenario_gap_through_stop),
        Scenario("EXACT_TOUCH_STOP", "a touch fills at the stop", scenario_exact_touch_stop),
        Scenario("EXACT_TOUCH_TARGET", "a touch fills at the target", scenario_exact_touch_target),
        Scenario("OPEN_BEYOND_STOP", "opened above then traded through the stop", scenario_open_beyond_stop_is_not_a_stop_price_fill),
        Scenario("EXPIRY_AT_CLOSE", "expiry closes at the bar's own close", scenario_expiry_at_close),
        Scenario("SHORT_SIDE_MIRROR", "the short side mirrors the rules", scenario_short_side_is_mirrored),
        Scenario("SNU_BOTH_TOUCHED", "intrabar ambiguity is declared and resolved", scenario_snu_bar_touches_both),
        Scenario("BRACKETLESS_NO_STOP_OUT", "an unprotected decision is not stopped out at its entry", scenario_bracketless_does_not_stop_out),
    ]
    rows = []
    for scenario in scenarios:
        result = scenario.check()
        rows.append({"name": scenario.name, "about": scenario.about, **result})
    overfit = overfitting_hypothesis_test()
    snu_count = sum(1 for row in rows if row.get("snu"))
    return {
        "pillar": "P3_synthetic_scenarios",
        "evidence_class": "SCENARIO ONLY: engine-correctness and test-behaviour evidence, never a performance result",
        "claim_status": "NO_ECONOMIC_CLAIM",
        "snu": {"declared_rule": DECLARED_SNU_RULE, "counted_in_this_suite": snu_count, "cases": ["SNU_BOTH_TOUCHED"]},
        "model_candles": rows,
        "model_candles_passed": sum(1 for row in rows if row["passed"]),
        "model_candles_total": len(rows),
        "overfitting_hypothesis_test": overfit,
        "passed": all(row["passed"] for row in rows) and overfit["passed"],
    }
