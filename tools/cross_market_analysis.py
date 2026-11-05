#!/usr/bin/env python
"""Synthesis: per-symbol table, paired arm deltas, statistics and benchmarks.

Consumes only recorded measurements (``data/matrix``, ``data/ablations``,
``data/census``, ``data/data_audit.json``) and emits the machine-readable tables
plus ``findings.json`` from which every report in
``reports/v9-cross-market-validation`` is written. No number is invented: a metric
that was not measured stays ``None`` and is listed as unmeasured.

Statistical choices, stated once here so the reports can cite them:

* **Clustered, not pooled.** The ten symbols trade the same six months, so their
  outcomes share market regimes and are not independent replications. Cross-symbol
  uncertainty is reported as a bootstrap over *symbols* (the clustering unit) and
  separately as a month-block bootstrap of pooled campaign results.
* **Sign test** on the per-symbol sign of an arm's net R, with the exact binomial
  tail. With ten correlated markets it is descriptive, not confirmatory.
* **Random-entry null** is simulated on the verified H1 bars with the same bracket
  (stop 2 %, target 4 %, stop evaluated first inside a bar) and the arm's observed
  direction mix and holding horizon. It is a bar-level replay, not native
  execution, and it is used only to ask whether directional *selection* beat
  arbitrary entries with the same exits.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import random
import statistics
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Sequence

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from v8_next.evaluation.multitape import load_multitape  # noqa: E402

ROOT = REPO / "reports" / "v9-cross-market-validation"
DATA = ROOT / "data"

ARMS = ("A", "B", "C", "D")
PAIRS = (("B", "A"), ("C", "B"), ("D", "C"), ("D", "A"))
STOP_PCT = 0.02
TARGET_PCT = 0.04
NULL_DRAWS = 2000
NULL_SEED = 20251105
MIN_BARS_FOR_NULL = 200


def load_matrix() -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for path in sorted((DATA / "matrix").glob("*.json")):
        document = json.loads(path.read_text())
        out[document["symbol"]] = document
    return out


def load_ablations() -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    directory = DATA / "ablations"
    if not directory.is_dir():
        return out
    for path in sorted(directory.glob("*.json")):
        document = json.loads(path.read_text())
        out[document["symbol"]] = document
    return out


def bootstrap_mean(values: Sequence[float], *, resamples: int = 5000, seed: int = 7) -> dict[str, Any]:
    if not values:
        return {"n": 0, "mean": None, "ci_low": None, "ci_high": None}
    rng = random.Random(seed)
    n = len(values)
    means = []
    for _ in range(resamples):
        means.append(statistics.fmean(values[rng.randrange(n)] for _ in range(n)))
    means.sort()
    lo = means[int(0.025 * resamples)]
    hi = means[int(0.975 * resamples) - 1]
    return {
        "n": n,
        "mean": statistics.fmean(values),
        "median": statistics.median(values),
        "ci_low": lo,
        "ci_high": hi,
        "ci_method": "percentile bootstrap of the mean, clustering unit = symbol",
        "resamples": resamples,
    }


def binom_two_sided(successes: int, trials: int) -> float:
    """Exact two-sided binomial tail under p=0.5."""
    if trials == 0:
        return 1.0

    def pmf(k: int) -> float:
        return math.comb(trials, k) * 0.5**trials

    observed = pmf(successes)
    total = sum(pmf(k) for k in range(trials + 1) if pmf(k) <= observed + 1e-12)
    return min(1.0, total)


def month_of(ns: int) -> str:
    return datetime.fromtimestamp(ns / 1e9, tz=timezone.utc).strftime("%Y-%m")


def funding_estimate(
    arm: dict[str, Any], funding_rows: Sequence[tuple[int, float]]
) -> dict[str, Any]:
    """Independent funding estimate from the tape, applied to each campaign.

    The engine's ``Position.adjustments()`` are only readable on the position
    records the cache still holds, so ``booked_funding`` is a floor rather than a
    complete total (the strategies reuse position ids). This estimate integrates the
    recorded settlement rates over each campaign's holding window at its entry
    notional: a long pays a positive rate, a short receives it. It is an estimate of
    magnitude, not a substitute for the engine's own accounting.
    """
    if not funding_rows:
        return {"estimated_funding_usdt": None, "status": "NO_FUNDING_ROWS"}
    total = 0.0
    covered = 0
    for campaign in arm["campaigns"]:
        if campaign["open"] or not campaign["close_ns"]:
            continue
        start, end = int(campaign["open_ns"]), int(campaign["close_ns"])
        notional = abs(
            float(Decimal(campaign["quantity"])) * float(Decimal(campaign["entry_px"]))
        )
        if notional <= 0:
            continue
        sign = 1.0 if str(campaign["side"]).upper().endswith("LONG") else -1.0
        rate_sum = sum(rate for ts, rate in funding_rows if start < ts <= end)
        total += -sign * notional * rate_sum
        covered += 1
    return {
        "estimated_funding_usdt": round(total, 6),
        "campaigns_with_funding_window": covered,
        "sign_convention": "negative = the strategy paid funding",
    }


def funding_series(symbol: str) -> list[tuple[int, float]]:
    tape = REPO / "research" / "tape" / f"{symbol.lower()}-1h-2025H1" / "tape.jsonl"
    if not tape.is_file():
        return []
    loaded = load_multitape(tape)
    rows = [
        (int(row.funding_time_ms), float(row.funding_rate))
        for row in loaded.funding
        if row.instrument == symbol
    ]
    rows.sort()
    return rows


def per_symbol_rows(
    matrix: dict[str, dict[str, Any]],
    funding: dict[str, list[tuple[int, float]]] | None = None,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for symbol, document in matrix.items():
        for arm in ARMS:
            a = document["arms"][arm]
            estimate = funding_estimate(a, (funding or {}).get(symbol, []))
            turnover = 2.0 * sum(
                abs(float(Decimal(c["quantity"])) * float(Decimal(c["entry_px"])))
                for c in a["campaigns"]
                if c.get("quantity") and c.get("entry_px")
            )
            rows.append(
                {
                    "symbol": symbol,
                    "arm": arm,
                    "trades": a["trades_closed"],
                    "open_at_cutoff": a["open_campaigns"],
                    "long": a["long_trades"],
                    "short": a["short_trades"],
                    "net_pnl_usdt": round(a["net_pnl"], 6),
                    "return_pct": round(a["return_pct"], 6),
                    "gross_price_pnl_usdt": round(a["gross_price_pnl"], 6),
                    "commissions_usdt": round(a["commissions"], 6),
                    "funding_usdt_booked_engine": None
                    if a["booked_funding"] is None
                    else round(a["booked_funding"], 6),
                    "funding_usdt_estimated": estimate.get("estimated_funding_usdt"),
                    "turnover_usdt": round(turnover, 4),
                    "residual_pct_of_turnover": None
                    if a["reconciliation_residual"] is None or turnover <= 0
                    else round(100.0 * a["reconciliation_residual"] / turnover, 6),
                    "reconciliation_residual": None
                    if a["reconciliation_residual"] is None
                    else round(a["reconciliation_residual"], 9),
                    "net_r_total": None if a["net_r_total"] is None else round(a["net_r_total"], 6),
                    "net_r_mean": None if a["net_r_mean"] is None else round(a["net_r_mean"], 6),
                    "profit_factor": None
                    if a["profit_factor"] is None
                    else round(a["profit_factor"], 4),
                    "win_rate_pct": None
                    if a["win_rate"] is None
                    else round(100 * a["win_rate"], 4),
                    "max_drawdown_pct": None
                    if a["max_drawdown_pct"] is None
                    else round(a["max_drawdown_pct"], 6),
                    "time_in_market_pct": None
                    if a["time_in_market_pct"] is None
                    else round(a["time_in_market_pct"], 4),
                    "holding_hours_mean": None
                    if a["holding_hours_mean"] is None
                    else round(a["holding_hours_mean"], 4),
                    "holding_hours_median": None
                    if a["holding_hours_median"] is None
                    else round(a["holding_hours_median"], 4),
                    "stop_amendments": a["amendments"].get("STOP", 0)
                    + a["amendments"].get("STOP_AMEND", 0),
                    "target_amendments": a["amendments"].get("TARGET", 0)
                    + a["amendments"].get("TARGET_AMEND", 0),
                    "protection_state": a["protection_state"],
                    "protection_violations": a["protection_violations"],
                    "fills": a["fills"],
                    "wall_s": a.get("wall_s"),
                    "peak_rss_mib": a.get("peak_rss_mib_run"),
                    "trade_digest": a["trade_digest"],
                }
            )
    return rows


def paired_rows(matrix: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for symbol, document in matrix.items():
        for high, low in PAIRS:
            hi_arm, lo_arm = document["arms"][high], document["arms"][low]
            hr, lr = hi_arm["net_r_total"], lo_arm["net_r_total"]
            rows.append(
                {
                    "symbol": symbol,
                    "pair": f"{high}-{low}",
                    "delta_net_pnl": round(hi_arm["net_pnl"] - lo_arm["net_pnl"], 6),
                    "delta_net_r": None if hr is None or lr is None else round(hr - lr, 6),
                    "delta_trades": hi_arm["trades_closed"] - lo_arm["trades_closed"],
                    "delta_drawdown_pct": None
                    if hi_arm["max_drawdown_pct"] is None
                    or lo_arm["max_drawdown_pct"] is None
                    else round(
                        hi_arm["max_drawdown_pct"] - lo_arm["max_drawdown_pct"], 6
                    ),
                }
            )
    return rows


def null_benchmark(symbol: str, arm: dict[str, Any], candles: Sequence[Any]) -> dict[str, Any]:
    """Bar-level random-entry null with the arm's direction mix and holding horizon."""
    if arm["trades_closed"] < 10:
        return {"symbol": symbol, "status": "INSUFFICIENT_TREATMENT_TRADES"}
    if len(candles) < MIN_BARS_FOR_NULL:
        return {"symbol": symbol, "status": "INSUFFICIENT_BARS"}
    long_share = arm["long_trades"] / max(1, arm["long_trades"] + arm["short_trades"])
    hold = arm["holding_hours_median"] or 6.0
    hold_bars = max(1, int(round(hold)))
    n_trades = arm["trades_closed"]
    rng = random.Random(NULL_SEED)
    means: list[float] = []
    totals: list[float] = []
    highs = [float(c.high) for c in candles]
    lows = [float(c.low) for c in candles]
    closes = [float(c.close) for c in candles]
    last_entry = len(candles) - hold_bars - 1
    if last_entry <= 1:
        return {"symbol": symbol, "status": "INSUFFICIENT_BARS"}
    for _ in range(NULL_DRAWS):
        total_r = 0.0
        for _ in range(n_trades):
            index = rng.randrange(1, last_entry)
            entry = closes[index]
            long_side = rng.random() < long_share
            stop = entry * (1 - STOP_PCT) if long_side else entry * (1 + STOP_PCT)
            target = entry * (1 + TARGET_PCT) if long_side else entry * (1 - TARGET_PCT)
            outcome: float | None = None
            for j in range(index + 1, min(index + 1 + hold_bars, len(candles))):
                hit_stop = lows[j] <= stop if long_side else highs[j] >= stop
                hit_target = highs[j] >= target if long_side else lows[j] <= target
                if hit_stop:  # stop first inside a bar: the conservative assumption
                    outcome = -1.0
                    break
                if hit_target:
                    outcome = TARGET_PCT / STOP_PCT
                    break
            if outcome is None:
                # horizon exit at the last visible close, in R units
                exit_index = min(index + hold_bars, len(candles) - 1)
                move = (closes[exit_index] - entry) / entry
                outcome = (move if long_side else -move) / STOP_PCT
            total_r += outcome
        totals.append(total_r)
        means.append(total_r / n_trades)
    means.sort()
    totals.sort()
    return {
        "symbol": symbol,
        "status": "OK",
        "draws": NULL_DRAWS,
        "seed": NULL_SEED,
        "trades_per_draw": n_trades,
        "hold_bars": hold_bars,
        "long_share": round(long_share, 4),
        "null_net_r_total_mean": round(statistics.fmean(totals), 4),
        "null_net_r_total_p05": round(totals[int(0.05 * NULL_DRAWS)], 4),
        "null_net_r_total_p95": round(totals[int(0.95 * NULL_DRAWS) - 1], 4),
        "treatment_net_r_total": None
        if arm["net_r_total"] is None
        else round(arm["net_r_total"], 4),
        "treatment_percentile": None
        if arm["net_r_total"] is None
        else round(
            100.0 * sum(1 for t in totals if t <= arm["net_r_total"]) / NULL_DRAWS, 2
        ),
        "basis": "bar-level bracket replay on verified H1 bars; stop evaluated before "
        "target inside a bar; not native execution",
    }


def regime_grouping(matrix: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Pooled campaign R by calendar month, per arm (regime proxy, not a label)."""
    out: dict[str, dict[str, Any]] = {}
    for arm in ARMS:
        months: dict[str, list[float]] = {}
        for document in matrix.values():
            a = document["arms"][arm]
            for campaign in a["campaigns"]:
                if campaign["open"] or not campaign["realized_pnl"]:
                    continue
                key = month_of(int(campaign["close_ns"]))
                risk = abs(
                    float(Decimal(campaign["quantity"])) * float(Decimal(campaign["entry_px"]))
                ) * STOP_PCT
                if risk <= 0:
                    continue
                pnl = float(Decimal(campaign["realized_pnl"].split()[0]))
                months.setdefault(key, []).append(pnl / risk)
        out[arm] = {
            month: {
                "campaigns": len(values),
                "net_r_total": round(sum(values), 4),
                "net_r_mean": round(statistics.fmean(values), 6),
            }
            for month, values in sorted(months.items())
        }
    return out


def sign_summary(values: dict[str, float | None]) -> dict[str, Any]:
    usable = {k: v for k, v in values.items() if v is not None}
    positives = sum(1 for v in usable.values() if v > 0)
    negatives = sum(1 for v in usable.values() if v < 0)
    return {
        "symbols": len(usable),
        "positive": positives,
        "negative": negatives,
        "flat": len(usable) - positives - negatives,
        "p_two_sided": round(binom_two_sided(positives, positives + negatives), 6)
        if positives + negatives
        else None,
        "detail": {k: round(v, 6) for k, v in usable.items()},
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=DATA)
    args = parser.parse_args()

    matrix = load_matrix()
    ablations = load_ablations()
    if not matrix:
        raise SystemExit("no matrix results in data/matrix")

    funding = {symbol: funding_series(symbol) for symbol in matrix}
    rows = per_symbol_rows(matrix, funding)
    csv_path = ROOT / "PER_SYMBOL_RESULTS.csv"
    with csv_path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)

    pairs = paired_rows(matrix)
    with (args.out / "paired_arm_deltas.csv").open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(pairs[0]))
        writer.writeheader()
        writer.writerows(pairs)

    # Per-arm cross-symbol summary
    summary: dict[str, Any] = {}
    for arm in ARMS:
        net_r_totals = {
            symbol: document["arms"][arm]["net_r_total"] for symbol, document in matrix.items()
        }
        pnls = {symbol: document["arms"][arm]["net_pnl"] for symbol, document in matrix.items()}
        usable = [v for v in net_r_totals.values() if v is not None]
        summary[arm] = {
            "net_r_total_by_symbol": sign_summary(net_r_totals),
            "net_pnl_total_usdt": round(sum(pnls.values()), 6),
            "net_pnl_by_symbol": {k: round(v, 4) for k, v in pnls.items()},
            "net_r_bootstrap_over_symbols": bootstrap_mean(usable),
            "trades_total": sum(
                document["arms"][arm]["trades_closed"] for document in matrix.values()
            ),
            "symbols_with_open_position_at_cutoff": sum(
                1
                for document in matrix.values()
                if document["arms"][arm]["open_campaigns"] > 0
            ),
            "protection_violations": sum(
                document["arms"][arm]["protection_violations"] for document in matrix.values()
            ),
        }

    paired_summary: dict[str, Any] = {}
    for high, low in PAIRS:
        key = f"{high}-{low}"
        deltas = {row["symbol"]: row["delta_net_r"] for row in pairs if row["pair"] == key}
        d_pnl = {row["symbol"]: row["delta_net_pnl"] for row in pairs if row["pair"] == key}
        usable = [v for v in deltas.values() if v is not None]
        paired_summary[key] = {
            "sign": sign_summary(deltas),
            "delta_net_pnl_total": round(sum(d_pnl.values()), 6),
            "delta_net_r_bootstrap_over_symbols": bootstrap_mean(usable),
        }

    # Random-entry null per symbol for the adaptive arm and the V8 arm
    nulls: dict[str, Any] = {}
    for symbol, document in matrix.items():
        tape = REPO / "research" / "tape" / f"{symbol.lower()}-1h-2025H1" / "tape.jsonl"
        if not tape.is_file():
            nulls[symbol] = {"status": "TAPE_MISSING"}
            continue
        candles = load_multitape(tape).candles[symbol]
        nulls[symbol] = {
            "D": null_benchmark(symbol, document["arms"]["D"], candles),
            "A": null_benchmark(symbol, document["arms"]["A"], candles),
        }

    # Entry-matched management diagnostic (C -> D)
    matched: dict[str, Any] = {}
    for symbol, document in ablations.items():
        block = document.get("entry_matched", {}).get("D_vs_C")
        if block:
            matched[symbol] = block
    matched_totals = {
        "symbols": len(matched),
        "matched_campaigns": sum(v["matched_campaigns"] for v in matched.values()),
        "matched_delta": round(sum(v["matched_delta"] for v in matched.values()), 6),
        "detail": {
            k: {
                "matched_campaigns": v["matched_campaigns"],
                "matched_delta": round(v["matched_delta"], 4),
                "left_only": v["unmatched_left_only"],
                "right_only": v["unmatched_right_only"],
            }
            for k, v in matched.items()
        },
    }

    findings = {
        "generated_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "symbols": sorted(matrix),
        "arm_summary": summary,
        "paired_summary": paired_summary,
        "regime_by_month": regime_grouping(matrix),
        "random_entry_null": nulls,
        "entry_matched_D_vs_C": matched_totals,
        "ablations_present": sorted(ablations),
    }
    (args.out / "findings.json").write_text(json.dumps(findings, indent=1, sort_keys=True))
    print(json.dumps({"csv_rows": len(rows), "paired_rows": len(pairs), "symbols": len(matrix)}))
    for arm in ARMS:
        s = summary[arm]
        print(
            json.dumps(
                {
                    "arm": arm,
                    "net_pnl_total": s["net_pnl_total_usdt"],
                    "net_r_sign": {
                        "pos": s["net_r_total_by_symbol"]["positive"],
                        "neg": s["net_r_total_by_symbol"]["negative"],
                        "p": s["net_r_total_by_symbol"]["p_two_sided"],
                    },
                    "trades": s["trades_total"],
                    "violations": s["protection_violations"],
                }
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
