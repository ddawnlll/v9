#!/usr/bin/env python
"""NX07 (#428) — a real fold family, its pinned plan, and its statistics table.

The family is real tape and real costs, one variant per pre-registered swing
policy: ``cash`` (baseline), ``causal_trend``, and three squeeze variants of
``plain_swing`` (``baseline``, ``m1``, ``m2``). The statistics plan is pinned and
written **before** ``run_statistics`` is called, and the emitted table records,
per method, whether it is an authority condition or a diagnostic, its inputs,
its provenance and whether the sample was adequate.

A null, degenerate or underpowered result is a complete delivery for this issue;
nothing here is tuned so that a p-value passes.

Usage (from the repository root):

    uv run --project . --extra dev --extra research \\
        python tools/nx07_family_statistics.py
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any

from v8_next.domain.market import Candle, CausalFrame, frame_at_incremental
from v8_next.economics.swing_baseline import (
    SHARED_CONTRACT,
    SwingPolicySpec,
    replay_bracket,
    swing_signal,
)
from v8_next.evaluation import economic_benchmark as eb
from v8_next.evaluation.gate_resolution import load_tape_candles
from v8_next.evaluation.statistics_plan import (
    CANONICAL_G5_BLOCK_SIZE,
    g5_plan,
    require_pinned_before_results,
)

HOUR_NS = 3_600 * 10**9
WARMUP_BARS = 96
STARTING_CAPITAL = Decimal(SHARED_CONTRACT["initial_balance_usdt"])
RISK_FRACTION = Decimal(SHARED_CONTRACT["risk_per_trade_fraction"])
TAKER_FEE = Decimal(SHARED_CONTRACT["taker_fee"])

FAMILY: tuple[SwingPolicySpec, ...] = (
    SwingPolicySpec(
        policy_id="cash",
        grammar_policy=None,
        protection_policy=None,
        description="no exposure; the baseline of the family",
    ),
    SwingPolicySpec(
        policy_id="causal_trend",
        grammar_policy="trend-continuation-v2",
        protection_policy="timeout-only-v1",
        description="causal trend grammar with timeout-only protection",
    ),
    SwingPolicySpec(
        policy_id="swing_squeeze_baseline",
        grammar_policy="range-breakout-48-v1",
        protection_policy="squeeze:baseline:v2",
        description="48-bar range breakout, squeeze baseline protection",
    ),
    SwingPolicySpec(
        policy_id="swing_squeeze_m1",
        grammar_policy="range-breakout-48-v1",
        protection_policy="squeeze:m1:v2",
        description="48-bar range breakout, squeeze m1 protection",
    ),
    SwingPolicySpec(
        policy_id="swing_squeeze_m2",
        grammar_policy="range-breakout-48-v1",
        protection_policy="squeeze:m2:v2",
        description="48-bar range breakout, squeeze m2 protection",
    ),
)


def _parse_utc_ms(value: str) -> int:
    import datetime

    parsed = datetime.datetime.fromisoformat(value.strip()).replace(tzinfo=datetime.timezone.utc)
    return int(parsed.timestamp() * 1000)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def equity_curve(
    spec: SwingPolicySpec, series: list[Candle], *, instrument_id: str, window_bars: int
) -> list[float]:
    """Cumulative equity of one policy on the same real bars and the same budget.

    One campaign at a time; the risk per campaign is the shared contract's
    fraction of the *initial* capital, so no policy compounds differently from
    another. The curve is the decision-plane replay of NX06, not engine fills.
    """
    equity = float(STARTING_CAPITAL)
    curve = [equity]
    risk_notional = float(STARTING_CAPITAL * RISK_FRACTION)
    index = 62
    bars = 0
    parent_frame: CausalFrame | None = None
    while index < len(series) - 1 and bars < window_bars:
        decision_ns = series[index].end_ns
        prefix = tuple(series[: index + 1])
        frame = frame_at_incremental(parent_frame, instrument_id, decision_ns, prefix)
        parent_frame = frame
        decision = swing_signal(frame, spec, bar_ns=HOUR_NS)
        bars += 1
        if decision is None:
            index += 1
            curve.append(equity)
            continue
        has_bracket = spec.protection_policy not in (None, "timeout-only-v1")
        outcome = replay_bracket(
            decision, series[index + 1 :], bar_ns=HOUR_NS, bps_fee=TAKER_FEE, has_bracket=has_bracket
        )
        equity += risk_notional * outcome.net_return
        for _ in range(max(1, outcome.bars_held)):
            curve.append(equity)
        index += max(1, outcome.bars_held) + 1
    return curve


def method_table(stats: dict[str, Any], plan: Any, sufficiency: dict[str, Any]) -> list[dict[str, Any]]:
    """One row per method: class, inputs, provenance and adequacy."""
    rows = [
        {
            "method": "deflated_sharpe_ratio",
            "class": "AUTHORITY_CONDITION" if "deflated_sharpe_confidence>=0.95" in plan.authority_conditions else "DIAGNOSTIC",
            "receipt_key": "dsr",
            "verdict": (stats.get("dsr") or {}).get("verdict", "MISSING"),
            "inputs": {
                "multiplicity_trials": plan.multiplicity_trials,
                "effective_independent_trials": plan.effective_independent_trials,
                "independence_basis": plan.independence_basis,
            },
            "provenance": "real family curves on the shared tape, same evaluation chronology",
            "adequacy": sufficiency,
        },
        {
            "method": "white_reality_check",
            "class": "DIAGNOSTIC",
            "receipt_key": "spa",
            "verdict": (stats.get("spa") or {}).get("verdict", "MISSING"),
            "inputs": {
                "block_size": plan.block_size,
                "reps": plan.reps,
                "seed": plan.seed,
            },
            "provenance": "stationary bootstrap on paired differentials, shared frozen/eval chronology",
            "adequacy": sufficiency,
        },
        {
            "method": "probability_of_backtest_overfitting",
            "class": "DIAGNOSTIC",
            "receipt_key": "pbo",
            "verdict": (stats.get("pbo") or {}).get("verdict", "MISSING"),
            "inputs": {
                "partitions": plan.pbo_partitions,
                "metric": plan.pbo_metric,
                "max_splits": plan.pbo_max_splits,
            },
            "provenance": "CSCV over the registered variants only",
            "adequacy": sufficiency,
        },
    ]
    return rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", default=None)
    parser.add_argument("--tape", default="research/tape/multi-1h-4y/tape.jsonl")
    parser.add_argument("--instrument", default="BTCUSDT")
    parser.add_argument("--start-utc", default="2025-01-01")
    parser.add_argument("--end-utc", default="2025-02-01")
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--out", default="docs/evidence/v87/NX07")
    args = parser.parse_args(argv)

    repo_root = (
        Path(args.repo_root).resolve() if args.repo_root else Path(__file__).resolve().parents[1]
    )
    out_dir = (repo_root / args.out).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    tape = (repo_root / args.tape).resolve()
    if not tape.is_file():
        print(f"[NX07] FAIL: tape absent at {tape}")
        return 2

    start_ms, end_ms = _parse_utc_ms(args.start_utc), _parse_utc_ms(args.end_utc)
    window = load_tape_candles(tape, instrument=args.instrument, start_ms=start_ms, end_ms=end_ms)
    history = load_tape_candles(
        tape, instrument=args.instrument, start_ms=start_ms - WARMUP_BARS * 3_600_000, end_ms=start_ms
    )
    series = history + window
    instrument_id = series[0].instrument_id

    family_manifest = {
        "family": "v87-nx07-swing",
        "about_to_be_measured_on": {
            "tape": str(tape),
            "start_utc": args.start_utc,
            "end_utc": args.end_utc,
            "bars": len(window),
        },
        "registered_before_measurement": True,
        "variants": [spec.as_dict() for spec in FAMILY],
        "baseline_id": "cash",
        "history_completeness": "UNKNOWN_UNDISCLOSED_TRIALS_POSSIBLE",
        "note": "no result is in this file; the measurement is written separately",
    }
    manifest_path = out_dir / "family_manifest.json"
    manifest_path.write_text(json.dumps(family_manifest, indent=2, sort_keys=True) + "\n")

    # ---- the plan is pinned and written before any curve or statistic exists ----
    plan = g5_plan(
        family="v87-nx07-swing",
        pinned_ns=min(int(c.start_ns) for c in window),
        block_size=CANONICAL_G5_BLOCK_SIZE,
        reps=eb.BOOTSTRAP_REPS,
        seed=args.seed,
    )
    plan_path = out_dir / "statistics_plan.json"
    plan_path.write_text(json.dumps(plan.as_dict(), indent=2, sort_keys=True) + "\n")

    family_equity: dict[str, list[float]] = {}
    for spec in FAMILY:
        curve = equity_curve(spec, series, instrument_id=instrument_id, window_bars=len(window))
        family_equity[spec.policy_id] = curve
    end_ns = [int(c.end_ns) for c in series[62 : 62 + len(max(family_equity.values(), key=len))]]
    end_ns = end_ns[: len(max(family_equity.values(), key=len))]

    # align every curve to the shortest common length so the paired chronology holds
    common = min(len(curve) for curve in family_equity.values())
    family_equity = {name: curve[:common] for name, curve in family_equity.items()}
    end_ns = end_ns[:common]
    require_pinned_before_results(plan, computed_ns=end_ns[-1])

    stats = eb.run_statistics(family_equity, end_ns, "cash", plan=plan)
    sufficiency = stats["sample_sufficiency"]
    table = method_table(stats, plan, sufficiency)

    baseline_excess = {
        name: (
            (curve[-1] / curve[0] - 1.0) - (family_equity["cash"][-1] / family_equity["cash"][0] - 1.0)
        )
        for name, curve in family_equity.items()
        if name != "cash"
    }
    ci = {
        "method": "block_bootstrap_ci_on_paired_differentials",
        "block_size": plan.block_size,
        "reps": plan.reps,
        "seed": plan.seed,
        "status": "COMPUTED"
        if (stats.get("spa") or {}).get("verdict") == "COMPUTED"
        else "NOT_COMPUTED_SEE_SPA_VERDICT",
    }

    receipt = {
        "family": "v87-nx07-swing",
        "window": family_manifest["about_to_be_measured_on"],
        "intervals": stats.get("intervals_per_variant"),
        "interval_bars": stats.get("interval_bars"),
        "plan_id": plan.identity(),
        "plan_file_sha256": _sha256(plan_path),
        "family_manifest_sha256": _sha256(manifest_path),
        "authority_conditions": stats["authority_conditions"],
        "diagnostics": stats["diagnostics"],
        "sample_sufficiency": sufficiency,
        "baseline_excess_return": {k: round(v, 8) for k, v in sorted(baseline_excess.items())},
        "confidence_interval": ci,
        "methods": table,
        "statistics": {key: stats[key] for key in ("dsr", "pbo", "spa") if key in stats},
        "history_completeness": family_manifest["history_completeness"],
        "evidence_class": (
            "DECISION_PLANE_DIAGNOSTIC on real bars and real taker fees; not engine "
            "fills, not a venue settlement, funding MISSING (not zero)"
        ),
        "economic_claim": "NONE",
        "success_condition": (
            "a negative or underpowered finding is a complete technical delivery; no "
            "estimator was chosen so that a p-value passes"
        ),
    }
    receipt_path = out_dir / "statistics_receipt.json"
    receipt_path.write_text(json.dumps(receipt, indent=2, sort_keys=True, default=str) + "\n")

    print(f"[NX07] bars={len(window)} variants={len(family_equity)} intervals={receipt['intervals']}")
    for row in table:
        print(f"[NX07] {row['method']:<32} {row['class']:<20} verdict={row['verdict']}")
    print(f"[NX07] sufficiency={sufficiency['verdict']} independent_samples={sufficiency['independent_samples']}")
    print(f"[NX07] baseline_excess={receipt['baseline_excess_return']}")
    print(f"[NX07] plan    sha256:{_sha256(plan_path)}")
    print(f"[NX07] receipt sha256:{_sha256(receipt_path)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
