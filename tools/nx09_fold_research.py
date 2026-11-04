#!/usr/bin/env python
"""NX09 (#430) — the registered four-fold swing research, run for real.

The freeze comes first and is written to disk before any fold result exists:

* the NX01 calendar is rebuilt from the tape inventory and the measured burn
  segments, the 24/12/12 walk-forward plan is built from it, and the plan is
  registered immutably in a store beside the evidence;
* the family (cash / causal trend / three protection variants) and the two
  pre-registered ablations are written with their content hashes, the resampling
  plan (block size, reps, seed) and the source hashes.

Then each non-final fold is measured on real bars for two symbols, and the report
carries per-fold and per-regime breakdowns (net excess, costs, drawdown, holding
distribution, sample sufficiency) instead of one pooled number.

The final window is NOT opened: the tape's measured role makes it ineligible, and
that is recorded as the ``NO_PROTECTED_FINAL`` metadata field -- not as a GateState
value. This is a diagnostic research delivery with an open prospective backlog;
it makes no economic claim and mints no readiness.

Usage (from the repository root):

    uv run --project . --extra dev --extra research \\
        python tools/nx09_fold_research.py
"""

from __future__ import annotations

import argparse
import hashlib
import json
import statistics
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any

import numpy as np

from v8_next.domain.market import Candle, CausalFrame, frame_at_incremental
from v8_next.economics.grammar import POLICY_REQUIRED_BARS
from v8_next.economics.swing_baseline import (
    SHARED_CONTRACT,
    SwingPolicySpec,
    family_registry,
    replay_bracket,
    swing_signal,
)
from v8_next.evaluation import economic_benchmark as eb
from v8_next.evaluation.gate_resolution import classify_market_regimes, load_tape_candles
from v8_next.evaluation.historical_plan import (
    build_historical_plan,
    freeze_historical_plan,
    readback_historical_plan,
)
from v8_next.evaluation.store import ResearchStore
from v8_next.evaluation.tape_identity import (
    burn_segments,
    inventory_tape,
    measure_covering_tape,
    policy_lineage_burn_table,
    swing_calendar,
)

HOUR_NS = 3_600 * 10**9
TAIL_TAPES = (
    Path("research/tape/quad-1h-12m"),
    Path("research/tape/sol-dev-solusdt-2025-07-2026-07"),
)
PRIMARY = "BTCUSDT"
SECONDARY = "ETHUSDT"
BASELINE_POLICY = "causal_trend"

FAMILY: tuple[SwingPolicySpec, ...] = (
    SwingPolicySpec("cash", None, None, "no exposure; the comparison floor"),
    SwingPolicySpec(
        "causal_trend", "trend-continuation-v2", "timeout-only-v1", "trend grammar, timeout-only"
    ),
    SwingPolicySpec(
        "swing_squeeze_baseline",
        "range-breakout-48-v1",
        "squeeze:baseline:v2",
        "registered swing policy",
    ),
    SwingPolicySpec(
        "swing_squeeze_m1", "range-breakout-48-v1", "squeeze:m1:v2", "pre-registered ablation m1"
    ),
    SwingPolicySpec(
        "swing_squeeze_m2", "range-breakout-48-v1", "squeeze:m2:v2", "pre-registered ablation m2"
    ),
)

ABLATIONS = ("swing_squeeze_m1", "swing_squeeze_m2")
BLOCK_SIZE = eb.BOOTSTRAP_BLOCK
REPS = eb.BOOTSTRAP_REPS
SEED = eb.BOOTSTRAP_SEED


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _drawdown(curve: list[float]) -> float:
    peak = curve[0] if curve else 0.0
    worst = 0.0
    for value in curve:
        peak = max(peak, value)
        if peak > 0:
            worst = min(worst, value / peak - 1.0)
    return round(worst, 8)


def replay_policy(
    spec: SwingPolicySpec,
    series: list[Candle],
    *,
    instrument_id: str,
    scored_start_ns: int,
    scored_end_ns: int,
    initial_capital: float,
    risk_fraction: float,
    taker_fee: Decimal,
) -> dict[str, Any]:
    """Replay one policy on one fold, counting only campaigns opened in the fold."""
    if spec.grammar_policy is None:
        return {
            "policy_id": spec.policy_id,
            "campaigns": 0,
            "equity": [initial_capital],
            "net_returns": [],
            "holding_bars": [],
            "exposure_bars": 0,
            "fee_cost": 0.0,
            "net_return": 0.0,
            "max_drawdown": 0.0,
            "holding_median_bars": None,
            "warmup_bars": 0,
            "warmup_source": "no grammar; nothing to warm up",
            "note": "no exposure by construction; not a measurement",
        }
    equity = initial_capital
    curve = [equity]
    nets: list[float] = []
    holdings: list[int] = []
    exposure = 0
    fees = 0.0
    risk_notional = initial_capital * risk_fraction
    # declared requirement, not a local constant (see the NX06 fix and the revision note)
    index = int(POLICY_REQUIRED_BARS[spec.grammar_policy])
    parent_frame: CausalFrame | None = None
    while index < len(series) - 1:
        candle = series[index]
        if candle.end_ns >= scored_end_ns:
            break
        prefix = tuple(series[: index + 1])
        frame = frame_at_incremental(parent_frame, instrument_id, candle.end_ns, prefix)
        parent_frame = frame
        decision = swing_signal(frame, spec, bar_ns=HOUR_NS)
        if decision is None or candle.start_ns < scored_start_ns:
            index += 1
            continue
        has_bracket = spec.protection_policy not in (None, "timeout-only-v1")
        outcome = replay_bracket(
            decision,
            series[index + 1 :],
            bar_ns=HOUR_NS,
            bps_fee=taker_fee,
            has_bracket=has_bracket,
        )
        # a campaign that would resolve after the fold ends is left open at the edge
        if outcome.exit_ns > scored_end_ns:
            outcome = type(outcome)(
                exit_kind="OPEN_AT_CUTOFF",
                exit_ns=scored_end_ns,
                exit_price=outcome.exit_price,
                bars_held=outcome.bars_held,
                hours_held=outcome.hours_held,
                gross_return=outcome.gross_return,
                fee_cost_return=outcome.fee_cost_return,
                net_return=outcome.net_return,
                gap_through_stop=outcome.gap_through_stop,
            )
        equity += risk_notional * outcome.net_return
        nets.append(outcome.net_return)
        holdings.append(outcome.bars_held)
        exposure += outcome.bars_held
        fees += outcome.fee_cost_return
        curve.append(equity)
        index += max(1, outcome.bars_held) + 1
    return {
        "policy_id": spec.policy_id,
        "campaigns": len(nets),
        "equity": curve,
        "net_returns": nets,
        "holding_bars": holdings,
        "exposure_bars": exposure,
        "fee_cost": round(fees, 8),
        "net_return": round((equity / initial_capital) - 1.0, 8),
        "max_drawdown": _drawdown(curve),
        "holding_median_bars": float(statistics.median(holdings)) if holdings else None,
    }


def paired_bootstrap_ci(
    candidate: list[float], baseline: list[float], *, block_size: int, reps: int, seed: int
) -> dict[str, Any]:
    """Block bootstrap CI on the paired difference of two campaign-return series.

    Pairs only the overlapping prefix (same fold, same order of campaigns), and says
    so when the pairing is too short to mean anything rather than returning a
    confidence interval over three points.
    """
    n = min(len(candidate), len(baseline))
    if n < 8:
        return {
            "status": "UNDERPOWERED",
            "paired_campaigns": n,
            "reason": "fewer than 8 paired campaigns; no interval is reported",
        }
    diff = np.asarray(candidate[:n], dtype=np.float64) - np.asarray(baseline[:n], dtype=np.float64)
    rng = np.random.default_rng(seed)
    blocks = int(np.ceil(n / block_size))
    means = np.empty(reps)
    for r in range(reps):
        starts = rng.integers(0, n, size=blocks)
        sample = np.concatenate(
            [np.take(diff, np.arange(s, s + block_size), mode="wrap") for s in starts]
        )[:n]
        means[r] = sample.mean()
    lo, hi = np.percentile(means, [2.5, 97.5])
    return {
        "status": "COMPUTED",
        "paired_campaigns": n,
        "mean_paired_difference": round(float(diff.mean()), 8),
        "ci_low": round(float(lo), 8),
        "ci_high": round(float(hi), 8),
        "block_size": block_size,
        "reps": reps,
        "seed": seed,
        "note": "paired on the same fold and campaign order; not an economic claim",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", default=None)
    parser.add_argument("--tape", default="research/tape/multi-1h-4y/tape.jsonl")
    parser.add_argument("--out", default="docs/evidence/v87/NX09")
    args = parser.parse_args(argv)

    repo_root = (
        Path(args.repo_root).resolve() if args.repo_root else Path(__file__).resolve().parents[1]
    )
    out_dir = (repo_root / args.out).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    tape = (repo_root / args.tape).resolve()
    if not tape.is_file():
        print(f"[NX09] FAIL: tape absent at {tape}")
        return 2

    # ---- freeze: calendar -> plan -> immutable registration ----
    inventory = inventory_tape(tape)
    tail = [
        measure_covering_tape((repo_root / p).resolve())
        for p in TAIL_TAPES
        if ((repo_root / p) / "tape.jsonl").is_file()
    ]
    verified = policy_lineage_burn_table(repo_root=repo_root, tape_path=tape)
    segments = burn_segments(
        inventory=inventory,
        verified_accesses=verified,
        tail_burned_from_ms=min(t.window_start_ms for t in tail),
        tail_evidence=tuple(t.path for t in tail),
    )
    calendar = swing_calendar(inventory=inventory, segments=segments)
    plan = build_historical_plan(
        calendar=calendar,
        dataset_id=inventory.data_id,
        instrument="BTCUSDT-PERP.BINANCE",
        policies=("range-breakout-48-v1", "trend-continuation-v2", "mean-reversion-v2"),
        baseline="range-breakout-48-v1",
        initial_balance_usdt=SHARED_CONTRACT["initial_balance_usdt"],
        maker_fee=SHARED_CONTRACT["maker_fee"],
        taker_fee=SHARED_CONTRACT["taker_fee"],
        bar_ns=HOUR_NS,
        aggregation="INDEPENDENT_FOLD_RESET",
        plan_id="NX09-REGISTERED-01",
        code_and_lock_hash=hashlib.sha256(
            (repo_root / "uv.lock").read_bytes()
        ).hexdigest(),
    )
    store = ResearchStore(out_dir / "research.sqlite")
    digest = freeze_historical_plan(store, plan)
    readback = readback_historical_plan(store, plan.plan_id)
    assert readback.digest() == digest

    fold_freeze = {
        "plan_id": plan.plan_id,
        "plan_digest": digest,
        "frozen_before_any_fold_result": True,
        "dataset_id": plan.dataset_id,
        "instrument": PRIMARY,
        "folds": [window.as_dict() for window in plan.folds],
        "final_eligible": plan.final_eligible,
        "final_eligibility_reason": plan.final_eligibility_reason,
        "NO_PROTECTED_FINAL": not plan.final_eligible,
        "baseline_policy": BASELINE_POLICY,
        "family_registry": family_registry(),
        "ablations": list(ABLATIONS),
        "family_contract": SHARED_CONTRACT,
        "resampling_plan": {"block_size": BLOCK_SIZE, "reps": REPS, "seed": SEED},
        "source_hashes": {
            "tape": str(tape),
            "tape_sha256": hashlib.sha256(tape.read_bytes()).hexdigest(),
            "code_and_lock_hash": plan.code_and_lock_hash,
            "calendar_digest": plan.calendar_digest,
        },
        "note": (
            "no fold result is in this file; the measurement is written separately. "
            "NO_PROTECTED_FINAL is metadata, not a GateState value"
        ),
    }
    freeze_path = out_dir / "fold_freeze.json"
    freeze_path.write_text(json.dumps(fold_freeze, indent=2, sort_keys=True) + "\n")
    print(f"[NX09] frozen plan={plan.plan_id} folds={len(plan.folds)} final_eligible={plan.final_eligible}")

    # ---- measure each non-final fold ----
    initial_capital = float(SHARED_CONTRACT["initial_balance_usdt"])
    risk_fraction = float(SHARED_CONTRACT["risk_per_trade_fraction"])
    taker_fee = Decimal(SHARED_CONTRACT["taker_fee"])
    results: dict[str, Any] = {}
    for window in plan.folds:
        if window.role == "FINAL":
            continue
        fold_entry: dict[str, Any] = {
            "fold": window.as_dict(),
            "symbols": {},
        }
        for symbol in (PRIMARY, SECONDARY):
            scored_start_ns = window.scored_start_ns
            scored_end_ns = window.scored_end_ns
            candles = load_tape_candles(
                tape,
                instrument=symbol,
                start_ms=window.warmup_start_ns // 1_000_000,
                end_ms=scored_end_ns // 1_000_000,
            )
            if not candles:
                fold_entry["symbols"][symbol] = {"status": "NO_DATA_IN_FOLD"}
                continue
            instrument_id = candles[0].instrument_id
            per_policy: dict[str, Any] = {}
            for spec in FAMILY:
                per_policy[spec.policy_id] = replay_policy(
                    spec,
                    candles,
                    instrument_id=instrument_id,
                    scored_start_ns=scored_start_ns,
                    scored_end_ns=scored_end_ns,
                    initial_capital=initial_capital,
                    risk_fraction=risk_fraction,
                    taker_fee=taker_fee,
                )
            baseline = per_policy[BASELINE_POLICY]
            for policy_id, row in per_policy.items():
                if policy_id == BASELINE_POLICY:
                    continue
                row["excess_vs_baseline"] = round(
                    row["net_return"] - baseline["net_return"], 8
                )
                row["paired_ci"] = paired_bootstrap_ci(
                    row["net_returns"], baseline["net_returns"],
                    block_size=BLOCK_SIZE, reps=REPS, seed=SEED,
                )
                row.pop("net_returns", None)
            baseline.pop("net_returns", None)
            regimes = classify_market_regimes(candles=candles, slice_length=max(20, len(candles) // 4))
            fold_entry["symbols"][symbol] = {
                "status": "MEASURED",
                "bars": len(candles),
                "instrument_id": instrument_id,
                "policies": per_policy,
                "regimes": {
                    name: {
                        "bars": len(c_list),
                        "start_utc": c_list[0].start_ns // 1_000_000,
                        "end_utc": c_list[-1].end_ns // 1_000_000,
                    }
                    for name, c_list in regimes.items()
                },
                "sample_sufficiency": eb.block_bootstrap_ci(
                    [row["net_return"] for row in per_policy.values()],
                    block=BLOCK_SIZE,
                    reps=REPS,
                    seed=SEED,
                )[0]
                is not None,
            }
        results[window.fold_id] = fold_entry

    receipt = {
        "plan_id": plan.plan_id,
        "plan_digest": digest,
        "fold_freeze_sha256": _sha256(freeze_path),
        "final_eligible": plan.final_eligible,
        "final_eligibility_reason": plan.final_eligibility_reason,
        "NO_PROTECTED_FINAL": not plan.final_eligible,
        "baseline_policy": BASELINE_POLICY,
        "ablations": list(ABLATIONS),
        "resampling_plan": {"block_size": BLOCK_SIZE, "reps": REPS, "seed": SEED},
        "folds": results,
        "selected_from_fold_results": "NONE (no policy was selected or promoted from these results)",
        "evidence_class": (
            "DIAGNOSTIC: registered four-fold research on real bars with real taker "
            "fees; decision-plane replay, not engine fills; funding MISSING"
        ),
        "prospective_backlog": (
            "prospective maturity remains open (NX10); G7/live readiness and economic "
            "edge are NOT certified by this delivery"
        ),
        "economic_claim": "NONE",
        "success_condition": (
            "a negative or flat finding is delivered as-is; reaching a minimum economic "
            "result is not a closing condition"
        ),
    }
    receipt_path = out_dir / "fold_results.json"
    receipt_path.write_text(json.dumps(receipt, indent=2, sort_keys=True, default=str) + "\n")

    for fold_id, entry in results.items():
        for symbol, row in entry["symbols"].items():
            if row.get("status") != "MEASURED":
                print(f"[NX09] {fold_id} {symbol}: {row.get('status')}")
                continue
            nets = {p: r["net_return"] for p, r in row["policies"].items()}
            print(f"[NX09] {fold_id} {symbol}: {nets}")
    print(f"[NX09] final_eligible={plan.final_eligible} reason={plan.final_eligibility_reason}")
    print(f"[NX09] freeze  sha256:{_sha256(freeze_path)}")
    print(f"[NX09] results sha256:{_sha256(receipt_path)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
