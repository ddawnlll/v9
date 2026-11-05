#!/usr/bin/env python
"""Preregistered screen of raw directional signals on the development window.

The three candidates are evaluated with the *same* static bracket, the same
holding limit, the same cost model and the same execution assumptions, so their
numbers are comparable. Each candidate is also compared with a genuine
eligible-time random-entry control: a pool of every legal entry bar priced with
both directions under the identical policy, sampled ``draws`` times at the
candidate's own trade count.

No order is submitted and no engine is constructed: the evaluator is the
research screen (:mod:`v8_next.research.screen`), every row is
``MODEL_DERIVED``, and the maximum claim this tool can make is
``DEV_CANDIDATE``. Only a native replay may promote a candidate.

Usage:
    python tools/signal_screen.py --candidate bollinger_reversion
    python tools/signal_screen.py --candidate all --out reports/research/screen
"""

from __future__ import annotations

import argparse
import json
import os
import random
import statistics
import subprocess
import sys
from pathlib import Path
from typing import Any, Final

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from v8_next.research import metrics, screen, signals  # noqa: E402

UNIVERSE: Final[tuple[str, ...]] = (
    "ETHUSDT",
    "SOLUSDT",
    "BNBUSDT",
    "XRPUSDT",
    "DOGEUSDT",
    "ADAUSDT",
    "AVAXUSDT",
    "LINKUSDT",
    "DOTUSDT",
    "LTCUSDT",
)

TAPE_DIR: Final[Path] = REPO / "research" / "tape"

#: Frozen design. Changing any value invalidates the preregistration.
CONFIG: Final[dict[str, Any]] = {
    "stop_pct": 0.02,
    "target_pct": 0.04,
    "max_bars": 48,
    "maker_fee_bps": 2.0,
    "taker_fee_bps": 5.0,
    "slippage_bps": 2.0,
    "risk_budget_usdt": 100.0,
    "draws": 200,
    "control_seed": 20260101,
    "bootstrap_resamples": 2000,
    "bootstrap_seed": 20260101,
    "minimum_trades": 120,
    "minimum_positive_symbols": 6,
    "alpha": 0.05,
}

DEV_START_MS: Final[int] = 1_735_689_600_000  # 2025-01-01T00:00:00Z
DEV_END_MS: Final[int] = 1_746_057_600_000  # 2025-05-01T00:00:00Z
HOLDOUT_START_MS: Final[int] = DEV_END_MS
HOLDOUT_END_MS: Final[int] = 1_751_328_000_000  # 2025-07-01T00:00:00Z


class SealedWindowRefusal(RuntimeError):
    """The holdout was requested without an explicit authorization."""


def _fee_model() -> screen.FeeModel:
    return screen.FeeModel(
        maker=CONFIG["maker_fee_bps"] / 10_000.0,
        taker=CONFIG["taker_fee_bps"] / 10_000.0,
    )


def _code_sha() -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=REPO,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return ""


def _tape_path(symbol: str) -> Path:
    return TAPE_DIR / f"{symbol.lower()}-1h-2025H1"


def _price_entry(
    bars: list[screen.Bar],
    funding: tuple[tuple[int, float], ...],
    *,
    index: int,
    direction: int,
) -> dict[str, Any]:
    """Price a market entry at the open of ``index + 1`` under the shared policy."""
    entry_bar = bars[index + 1]
    entry = entry_bar.open
    stop, target = screen.static_bracket(
        entry,
        direction,
        stop_pct=CONFIG["stop_pct"],
        target_pct=CONFIG["target_pct"],
    )
    result = screen.evaluate_path(
        bars,
        entry_ns=bars[index].end_ns,
        entry=entry,
        direction=direction,
        stop=stop,
        target=target,
        policy="TIME_STOP",
        risk_budget_usdt=CONFIG["risk_budget_usdt"],
        fee_model=_fee_model(),
        max_bars=CONFIG["max_bars"],
        slippage_bps=CONFIG["slippage_bps"],
        funding=funding,
        target_maker=True,
    )
    return {
        "bar_index": index,
        "direction": "LONG" if direction > 0 else "SHORT",
        "entry_ns": bars[index].end_ns,
        "entry_price": entry,
        "stop_price": stop,
        "target_price": target,
        "holding_bars": result["bars_measured"],
        "realized_net_r": result["net_r"],
        "realized_gross_r": result["gross_r"],
        "realized_net_pnl_usdt": result["net_usdt"],
        "fees_usdt": result["fees_usdt"],
        "slippage_usdt": result["slippage_usdt"],
        "funding_usdt": result["funding_usdt"],
        "initial_risk_usdt": result["risk_usdt"],
        "outcome": result["outcome"],
    }


def _load_symbol(symbol: str, *, start_ms: int, end_ms: int) -> tuple[list[screen.Bar], tuple[tuple[int, float], ...]]:
    tape = _tape_path(symbol) / "tape.jsonl"
    bars = screen.load_bars(tape, instrument=symbol, start_ms=start_ms, end_ms=end_ms)
    funding = screen.load_funding(tape, instrument=symbol, start_ms=start_ms, end_ms=end_ms)
    if len(bars) < 120:
        raise RuntimeError(f"{symbol}: only {len(bars)} bars in window; refusing")
    return bars, funding


def _treatment_rows(
    bars: list[screen.Bar],
    funding: tuple[tuple[int, float], ...],
    candidate_id: str,
    *,
    overrides: dict[str, float] | None = None,
) -> list[dict[str, Any]]:
    built = signals.build_signals(candidate_id, bars, **(overrides or {}))
    rows: list[dict[str, Any]] = []
    for signal in built:
        if signal.bar_index + 1 >= len(bars):
            continue
        row = _price_entry(bars, funding, index=signal.bar_index, direction=signal.direction)
        if row["realized_net_r"] is None:
            continue
        row["reason"] = signal.reason
        rows.append(row)
    return rows


def _control_pool(
    bars: list[screen.Bar],
    funding: tuple[tuple[int, float], ...],
    *,
    warmup: int,
) -> list[dict[str, Any]]:
    pool: list[dict[str, Any]] = []
    for index in range(warmup, len(bars) - 1):
        for direction in (signals.LONG, signals.SHORT):
            row = _price_entry(bars, funding, index=index, direction=direction)
            if row["realized_net_r"] is not None:
                pool.append(row)
    return pool


def _pooled_mean_net_r(rows: list[dict[str, Any]]) -> float | None:
    values = [row["realized_net_r"] for row in rows if row["realized_net_r"] is not None]
    return statistics.fmean(values) if values else None


def run_candidate(
    candidate_id: str,
    *,
    start_ms: int,
    end_ms: int,
    with_sensitivity: bool = True,
) -> dict[str, Any]:
    spec = signals.SIGNAL_SPECS[candidate_id]
    per_symbol: dict[str, Any] = {}
    treatment_by_symbol: dict[str, list[dict[str, Any]]] = {}
    pooled_control_means: list[float] = []
    rng = random.Random(CONFIG["control_seed"])

    for symbol in UNIVERSE:
        bars, funding = _load_symbol(symbol, start_ms=start_ms, end_ms=end_ms)
        rows = _treatment_rows(bars, funding, candidate_id)
        pool = _control_pool(bars, funding, warmup=spec.warmup_bars)
        treatment_by_symbol[symbol] = rows
        n = len(rows)
        if n == 0:
            per_symbol[symbol] = {
                "trades": 0,
                "net_expectancy_r": None,
                "control_mean_r": None,
                "pool_size": len(pool),
            }
            continue
        n = min(n, len(pool))
        draw_means: list[float] = []
        for _ in range(CONFIG["draws"]):
            picked = rng.sample(pool, n)
            value = _pooled_mean_net_r(picked)
            draw_means.append(float(value) if value is not None else 0.0)
        pooled_control_means.append(statistics.fmean(draw_means))
        summary = metrics.summarize(rows, net_key="realized_net_r")
        per_symbol[symbol] = {
            "trades": n,
            "window_bars": len(bars),
            "net_expectancy_r": summary["net_expectancy_r"],
            "gross_expectancy_r": summary["gross_expectancy_r"],
            "win_rate": summary["win_rate"],
            "net_profit_factor": summary["net_profit_factor"],
            "total_net_pnl_usdt": summary["total_net_pnl"],
            "maximum_drawdown_usdt": summary["maximum_drawdown"],
            "median_holding_bars": summary["median_holding_bars"],
            "fee_drag_r": summary["fee_drag_r"],
            "pool_size": len(pool),
            "control_mean_r": statistics.fmean(draw_means),
            "control_q05_r": sorted(draw_means)[int(0.05 * len(draw_means))],
            "control_q95_r": sorted(draw_means)[min(len(draw_means) - 1, int(0.95 * len(draw_means)))],
            "percentile_vs_control": (
                sum(1 for value in draw_means if value < float(summary["net_expectancy_r"] or 0.0))
                / len(draw_means)
            ),
        }

    all_rows = [row for rows in treatment_by_symbol.values() for row in rows]
    summary = metrics.summarize(all_rows, net_key="realized_net_r")
    symbol_means = [
        value
        for value in (
            data.get("net_expectancy_r") for data in per_symbol.values()
        )
        if value is not None
    ]
    pooled_ci = metrics.bootstrap_mean_ci(
        symbol_means,
        seed=CONFIG["bootstrap_seed"],
        resamples=CONFIG["bootstrap_resamples"],
    )
    trade_ci = metrics.bootstrap_mean_ci(
        [row["realized_net_r"] for row in all_rows],
        seed=CONFIG["bootstrap_seed"] + 1,
        resamples=CONFIG["bootstrap_resamples"],
    )
    control_mean = statistics.fmean(pooled_control_means) if pooled_control_means else None
    observed = summary["net_expectancy_r"]

    sensitivity: dict[str, Any] = {}
    if with_sensitivity:
        for variant in signals.SENSITIVITY_GRID[candidate_id]:
            variant_rows: list[dict[str, Any]] = []
            for symbol in UNIVERSE:
                bars, funding = _load_symbol(symbol, start_ms=start_ms, end_ms=end_ms)
                variant_rows.extend(_treatment_rows(bars, funding, candidate_id, overrides=variant))
            value = _pooled_mean_net_r(variant_rows)
            sensitivity[json.dumps(variant, sort_keys=True)] = {
                "trades": len(variant_rows),
                "pooled_net_expectancy_r": value,
                "net_profit_factor": metrics.profit_factor(
                    [row["realized_net_r"] for row in variant_rows]
                ),
            }

    result: dict[str, Any] = {
        "candidate_id": candidate_id,
        "hypothesis": spec.hypothesis,
        "parameters": spec.params,
        "window": {"start_ms": start_ms, "end_ms": end_ms},
        "config": CONFIG,
        "code_sha": _code_sha(),
        "tape_sha256": {},
        "symbols": per_symbol,
        "pooled": {
            "trades": summary["trade_count"],
            "net_expectancy_r": observed,
            "gross_expectancy_r": summary["gross_expectancy_r"],
            "win_rate": summary["win_rate"],
            "net_profit_factor": summary["net_profit_factor"],
            "total_net_pnl_usdt": summary["total_net_pnl"],
            "maximum_drawdown_usdt": summary["maximum_drawdown"],
            "symbol_mean_ci": pooled_ci,
            "trade_mean_ci": trade_ci,
            "positive_symbols": sum(1 for value in symbol_means if value > 0),
            "symbols_with_trades": len(symbol_means),
            "control_pooled_mean_r": control_mean,
        },
        "sensitivity": sensitivity,
        "claim_status": "MODEL_DERIVED_DEV_SCREEN",
    }
    for symbol in UNIVERSE:
        manifest = json.loads((_tape_path(symbol) / "tape.manifest.json").read_text())
        result["tape_sha256"][symbol] = manifest.get("tape_sha256", "")
    return result


def assess(result: dict[str, Any], *, control_p_value: float | None) -> dict[str, Any]:
    pooled = result["pooled"]
    trades = pooled["trades"]
    observed = pooled["net_expectancy_r"]
    control = pooled["control_pooled_mean_r"]
    reasons: list[str] = []
    if observed is None or trades == 0:
        return {"status": "INCONCLUSIVE", "reasons": ["NO_TRADES"]}
    if trades < CONFIG["minimum_trades"]:
        reasons.append(f"UNDERPOWERED:{trades}<{CONFIG['minimum_trades']}")
    if observed <= 0:
        reasons.append("NON_POSITIVE_EXPECTANCY")
    if control is not None and observed <= control:
        reasons.append("NOT_ABOVE_RANDOM_ENTRY")
    if pooled["positive_symbols"] < CONFIG["minimum_positive_symbols"]:
        reasons.append(
            f"CONCENTRATED:{pooled['positive_symbols']}<{CONFIG['minimum_positive_symbols']}"
        )
    if control_p_value is None or control_p_value >= CONFIG["alpha"]:
        reasons.append("CONTROL_P_VALUE_NOT_SIGNIFICANT")
    status = "DEV_CANDIDATE" if not reasons else (
        "INCONCLUSIVE" if trades < CONFIG["minimum_trades"] else "NO_EDGE"
    )
    return {"status": status, "reasons": reasons, "control_p_value": control_p_value}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate", default="all")
    parser.add_argument("--out", type=Path, default=REPO / "reports" / "research" / "screen")
    parser.add_argument("--start-ms", type=int, default=DEV_START_MS)
    parser.add_argument("--end-ms", type=int, default=DEV_END_MS)
    parser.add_argument("--no-sensitivity", action="store_true")
    args = parser.parse_args()

    if args.start_ms >= HOLDOUT_START_MS and os.environ.get("V9_HOLDOUT_AUTHORIZED") != "1":
        raise SealedWindowRefusal(
            "the holdout window is sealed; set V9_HOLDOUT_AUTHORIZED=1 only after a "
            "candidate and its parameters are frozen in the ledger"
        )

    candidates = (
        list(signals.SIGNAL_SPECS) if args.candidate == "all" else [args.candidate]
    )
    args.out.mkdir(parents=True, exist_ok=True)
    results: dict[str, Any] = {}
    for candidate_id in candidates:
        result = run_candidate(
            candidate_id,
            start_ms=args.start_ms,
            end_ms=args.end_ms,
            with_sensitivity=not args.no_sensitivity,
        )
        # One-sided control p-value: share of null draws at least as good as the
        # treatment, from the same eligible pool and cost model.
        per_symbol_p = [
            data.get("percentile_vs_control")
            for data in result["symbols"].values()
            if data.get("percentile_vs_control") is not None
        ]
        p_value = (
            1.0 - statistics.fmean(per_symbol_p) if per_symbol_p else None
        )
        verdict = assess(result, control_p_value=p_value)
        result["assessment"] = verdict
        (args.out / f"{candidate_id}.json").write_text(
            json.dumps(result, indent=2, sort_keys=True), encoding="utf-8"
        )
        results[candidate_id] = {
            "trades": result["pooled"]["trades"],
            "net_expectancy_r": result["pooled"]["net_expectancy_r"],
            "control_mean_r": result["pooled"]["control_pooled_mean_r"],
            "symbols_positive": result["pooled"]["positive_symbols"],
            "p_value": p_value,
            "status": verdict["status"],
            "reasons": verdict["reasons"],
            "sensitivity": result["sensitivity"],
        }

    # Holm correction across the candidates tested in this run.
    ordered = sorted(
        results.items(),
        key=lambda item: float(item[1]["p_value"] if item[1]["p_value"] is not None else 1.0),
    )
    for rank, (_candidate_id, payload) in enumerate(ordered):
        p_value = payload["p_value"]
        if p_value is None:
            payload["holm_p"] = None
            continue
        adjusted = min(1.0, p_value * (len(ordered) - rank))
        payload["holm_p"] = adjusted
        if adjusted >= CONFIG["alpha"] and payload["status"] == "DEV_CANDIDATE":
            payload["status"] = "NO_EDGE"
            payload["reasons"].append("HOLM_NOT_SIGNIFICANT")

    print(json.dumps(results, indent=2, sort_keys=True))
    (args.out / "summary.json").write_text(
        json.dumps(
            {
                "config": CONFIG,
                "code_sha": _code_sha(),
                "candidates": results,
                "alpha_after_holm": CONFIG["alpha"],
            },
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
