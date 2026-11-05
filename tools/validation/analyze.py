#!/usr/bin/env python
"""Aggregate cross-market arm measurements into preregistered statistics.

Reads only recorded arm JSON, the dataset audit and the grammar census. It
performs no strategy change and executes no order. Every derived number names
its basis; a missing input is `None`, never zero.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import random
from bisect import bisect_left
from collections import defaultdict
from decimal import Decimal
from pathlib import Path
from statistics import mean, median
from typing import Any, Iterable, Sequence

from v8_next.evaluation.economic_benchmark import pair_positions

SYMBOLS = (
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
CORE_ARMS = ("A", "B", "C", "D")
ABLATION_ARMS = (
    "A1_no_amend",
    "A2_stop_amend_only",
    "A3_target_amend_only",
    "A4_structural_stop_only",
    "A5_structural_target_only",
    "A6_fixed_geometry",
)
STRESS_BPS = (2.0, 5.0, 10.0)
BOOTSTRAP_DRAWS = 5000
RANDOM_DRAWS = 2000
SEED = 20251105


def _load(path: Path) -> Any:
    return json.loads(path.read_text())


def _arm(raw: dict[str, Any], arm: str) -> dict[str, Any] | None:
    record = raw.get("arms", {}).get(arm)
    if record is None or record.get("status") != "MEASURED":
        return None
    return record


def _mean_or_none(values: Iterable[float | None]) -> float | None:
    clean = [v for v in values if v is not None]
    return mean(clean) if clean else None


def _quantile(values: Sequence[float], q: float) -> float:
    if not values:
        raise ValueError("empty sample")
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = q * (len(ordered) - 1)
    low = int(math.floor(position))
    high = int(math.ceil(position))
    if low == high:
        return ordered[low]
    weight = position - low
    return ordered[low] * (1.0 - weight) + ordered[high] * weight


def _cluster_bootstrap(
    rows: Sequence[dict[str, Any]],
    *,
    statistic: str,
    draws: int = BOOTSTRAP_DRAWS,
    seed: int = SEED,
) -> dict[str, float] | None:
    """Resample symbols, not trades: ten correlated coins are not ten replications."""
    usable = [row for row in rows if row.get(statistic) is not None]
    if len(usable) < 2:
        return None
    rng = random.Random(seed)
    values: list[float] = []
    for _ in range(draws):
        sample = [usable[rng.randrange(len(usable))] for _ in usable]
        if statistic == "net_pnl":
            values.append(sum(float(row[statistic]) for row in sample))
        else:
            values.append(float(mean(float(row[statistic]) for row in sample)))
    return {
        "draws": draws,
        "low": _quantile(values, 0.025),
        "median": _quantile(values, 0.5),
        "high": _quantile(values, 0.975),
        "method": "cluster bootstrap over symbols (10 clusters); ignores within-cluster serial dependence",
    }


def _campaign_bootstrap(samples: Sequence[float], draws: int = BOOTSTRAP_DRAWS, seed: int = SEED) -> dict[str, float] | None:
    if len(samples) < 2:
        return None
    rng = random.Random(seed)
    values = [mean([samples[rng.randrange(len(samples))] for _ in samples]) for _ in range(draws)]
    return {
        "n": len(samples),
        "low": _quantile(values, 0.025),
        "median": _quantile(values, 0.5),
        "high": _quantile(values, 0.975),
        "method": "iid percentile bootstrap over campaign net R (ignores serial dependence)",
    }


def _binom_two_sided(k: int, n: int) -> float:
    if n == 0:
        return 1.0
    tail = sum(math.comb(n, i) for i in range(0, min(k, n - k) + 1)) / (2**n)
    return min(1.0, 2.0 * tail)


def _regime_labels(candles: Sequence[Any]) -> list[str]:
    from v8_next.evaluation.control_arms import RegimeBar, label_regimes

    bars = [RegimeBar(close=float(c.close), high=float(c.high), low=float(c.low)) for c in candles]
    return [label.regime.value for label in label_regimes(bars)]


def _load_candles(symbol: str, tape_root: Path) -> tuple[Any, ...]:
    from v8_next.evaluation.grammar_census import _load_verified_candles

    candles, _ = _load_verified_candles(tape_root / f"{symbol.lower()}-1h-2025H1", symbol)
    return candles


def _forward_bracket_outcomes(
    closes: Sequence[float],
    highs: Sequence[float],
    lows: Sequence[float],
    *,
    stop_pct: float = 0.02,
    target_pct: float = 0.04,
    max_bars: int = 96,
) -> tuple[list[float | None], list[float | None]]:
    """Gross R outcome for an entry at each bar close, LONG and SHORT.

    Stop is evaluated before target inside a bar (the same conservative tie rule
    the frozen intrabar basis names). No funding, no context, no expert gate.
    """
    n = len(closes)
    long_out: list[float | None] = [None] * n
    short_out: list[float | None] = [None] * n
    for i in range(n):
        entry = closes[i]
        if entry <= 0:
            continue
        long_stop = entry * (1.0 - stop_pct)
        long_target = entry * (1.0 + target_pct)
        short_stop = entry * (1.0 + stop_pct)
        short_target = entry * (1.0 - target_pct)
        long_done = short_done = False
        last = min(n - 1, i + max_bars)
        for j in range(i + 1, last + 1):
            if not long_done:
                if lows[j] <= long_stop:
                    long_out[i] = -1.0
                    long_done = True
                elif highs[j] >= long_target:
                    long_out[i] = target_pct / stop_pct
                    long_done = True
            if not short_done:
                if highs[j] >= short_stop:
                    short_out[i] = -1.0
                    short_done = True
                elif lows[j] <= short_target:
                    short_out[i] = target_pct / stop_pct
                    short_done = True
            if long_done and short_done:
                break
        if not long_done and last > i:
            long_out[i] = (closes[last] - entry) / (entry * stop_pct)
        if not short_done and last > i:
            short_out[i] = (entry - closes[last]) / (entry * stop_pct)
    return long_out, short_out


def _random_entry_benchmark(
    symbol: str,
    candles: Sequence[Any],
    *,
    trades: int,
    long_trades: int,
    draws: int = RANDOM_DRAWS,
) -> dict[str, Any] | None:
    if trades <= 0 or len(candles) < 200:
        return None
    closes = [float(c.close) for c in candles]
    highs = [float(c.high) for c in candles]
    lows = [float(c.low) for c in candles]
    long_out, short_out = _forward_bracket_outcomes(closes, highs, lows)
    eligible = [i for i in range(48, len(candles) - 97) if long_out[i] is not None]
    if len(eligible) < trades:
        return None
    directions = [1] * min(long_trades, trades) + [-1] * max(0, trades - long_trades)
    directions = directions[:trades]
    rng = random.Random(SEED + sum(ord(ch) for ch in symbol))
    fee_r = 0.05  # round-trip taker proxy: (0.0005+0.0005)/0.02
    null_means: list[float] = []
    for _ in range(draws):
        picks = rng.sample(eligible, trades)
        total = 0.0
        usable = 0
        for bar, direction in zip(picks, directions, strict=True):
            outcome = (long_out if direction == 1 else short_out)[bar]
            if outcome is None:
                continue
            total += outcome - fee_r
            usable += 1
        if usable:
            null_means.append(total / usable)
    return {
        "draws": len(null_means),
        "eligible_entry_bars": len(eligible),
        "fee_r_round_trip": fee_r,
        "null_mean": mean(null_means) if null_means else None,
        "null_p05": _quantile(null_means, 0.05) if null_means else None,
        "null_median": median(null_means) if null_means else None,
        "null_p95": _quantile(null_means, 0.95) if null_means else None,
        "basis": "random entry bar + matched direction count + 2%/4% bracket + 96-bar cap, stop-first, no funding/context",
    }


def _paired(rows: Sequence[dict[str, Any]], left: str, right: str) -> dict[str, Any]:
    deltas = []
    for row in rows:
        a = row["arms"].get(left)
        b = row["arms"].get(right)
        if a is None or b is None:
            continue
        deltas.append(
            {
                "symbol": row["symbol"],
                "net_pnl": a["net_pnl"] - b["net_pnl"],
                "net_r_total": (a["net_r_total"] or 0.0) - (b["net_r_total"] or 0.0),
                "net_r_mean": (a["net_r_mean"] or 0.0) - (b["net_r_mean"] or 0.0),
                "trades": a["trades"] - b["trades"],
                "drawdown": (a["max_drawdown"] or 0.0) - (b["max_drawdown"] or 0.0),
            }
        )
    return {
        "left": left,
        "right": right,
        "symbols": len(deltas),
        "per_symbol": deltas,
        "sum_net_pnl": sum(item["net_pnl"] for item in deltas),
        "mean_net_pnl": mean(item["net_pnl"] for item in deltas) if deltas else None,
        "sum_net_r_total": sum(item["net_r_total"] for item in deltas),
        "positive_symbols": sum(1 for item in deltas if item["net_pnl"] > 0),
        "negative_symbols": sum(1 for item in deltas if item["net_pnl"] < 0),
        "sign_test_p_two_sided": _binom_two_sided(
            sum(1 for item in deltas if item["net_pnl"] > 0), len(deltas)
        ),
    }


def build(tape_root: Path, raw_root: Path) -> dict[str, Any]:
    raws: dict[str, dict[str, Any]] = {}
    for symbol in SYMBOLS:
        path = raw_root / f"{symbol}.json"
        if path.is_file():
            raws[symbol] = _load(path)
    if not raws:
        raise SystemExit(f"no raw arm files under {raw_root}")

    rows: list[dict[str, Any]] = []
    for symbol, raw in raws.items():
        arm_rows: dict[str, Any] = {}
        for arm in (*CORE_ARMS, *ABLATION_ARMS):
            record = _arm(raw, arm)
            if record is None:
                arm_rows[arm] = None
                continue
            m = record["metrics"]
            arm_rows[arm] = {
                "trades": m["trades"],
                "net_pnl": m["net_pnl"],
                "return_pct": m["return_pct"],
                "net_r_mean": m["net_r_mean"],
                "net_r_total": m["net_r_total"],
                "net_r_samples": m["net_r_samples"],
                "max_drawdown": m["max_drawdown"],
                "hit_rate": m["hit_rate"],
                "fee_total": m["fee_total"],
                "funding_total": m["funding_total"],
                "gross_price_pnl": m["gross_price_pnl"],
                "open_campaigns": m["open_campaigns"],
                "long_trades": m["long_trades"],
                "short_trades": m["short_trades"],
                "holding_hours_mean": m["holding_hours_mean"],
                "exposure_pct": m["exposure_pct"],
                "profit_factor": m["profit_factor"],
                "one_way_traded_notional": m["one_way_traded_notional"],
                "breakeven_incremental_bps_per_side": m["breakeven_incremental_bps_per_side"],
                "price_reconstruction_residual": m["price_reconstruction_residual"],
                "closed_realized_vs_net_residual": m["closed_realized_vs_net_residual"],
                "funding_cache_undercount": m["funding_cache_undercount"],
                "protection_state": record["protection"].get("state"),
                "protection_violations": len(record["protection"].get("violations", [])),
                "order_status_counts": record["order_status_counts"],
                "rejected_orders": int((record["order_status_counts"] or {}).get("REJECTED", 0)),
                "lifecycle_event_counts": record["lifecycle_event_counts"],
                "unlinked_orders": record["unlinked_orders"],
                "orphan_closes": record["orphan_closes"],
                "wall_seconds": record["wall_seconds"],
                "process_peak_rss_kb": record["process_peak_rss_kb"],
                "horizon_evidence": record["horizon_evidence"],
                "opened": record["positions"]["opened"],
                "closed": record["positions"]["closed"],
            }
        rows.append({"symbol": symbol, "arms": arm_rows, "instrument_filters": raw.get("instrument_filters")})

    # Aggregate per-arm economics.
    aggregates: dict[str, Any] = {}
    for arm in (*CORE_ARMS, *ABLATION_ARMS):
        measured = [row for row in rows if row["arms"].get(arm) is not None]
        arms = [row["arms"][arm] for row in measured]
        total_trades = sum(a["trades"] for a in arms)
        total_r = sum((a["net_r_total"] or 0.0) for a in arms)
        aggregates[arm] = {
            "symbols": len(measured),
            "trades": total_trades,
            "net_pnl_sum": sum(a["net_pnl"] for a in arms),
            "net_r_total": total_r,
            "net_r_pooled_mean": (total_r / total_trades) if total_trades else None,
            "net_r_symbol_mean": _mean_or_none(a["net_r_mean"] for a in arms),
            "positive_symbols": sum(1 for a in arms if a["net_pnl"] > 0),
            "negative_symbols": sum(1 for a in arms if a["net_pnl"] < 0),
            "sign_test_p_two_sided": _binom_two_sided(
                sum(1 for a in arms if a["net_pnl"] > 0), len(arms)
            ),
            "mean_drawdown": _mean_or_none(a["max_drawdown"] for a in arms),
            "max_drawdown": max((a["max_drawdown"] or 0.0) for a in arms) if arms else None,
            "total_fees": sum(a["fee_total"] for a in arms),
            "total_funding": sum(a["funding_total"] for a in arms),
            "mean_hit_rate": _mean_or_none(a["hit_rate"] for a in arms),
            "mean_exposure_pct": _mean_or_none(a["exposure_pct"] for a in arms),
            "peak_rss_kb": max((a["process_peak_rss_kb"] or 0) for a in arms) if arms else None,
            "wall_seconds_sum": sum(a["wall_seconds"] for a in arms),
        }
        per_symbol = [
            {
                "symbol": row["symbol"],
                "net_pnl": row["arms"][arm]["net_pnl"],
                "net_r_mean": row["arms"][arm]["net_r_mean"],
                "trades": row["arms"][arm]["trades"],
            }
            for row in measured
        ]
        aggregates[arm]["cluster_bootstrap_net_pnl"] = _cluster_bootstrap(per_symbol, statistic="net_pnl")
        aggregates[arm]["cluster_bootstrap_mean_r"] = _cluster_bootstrap(per_symbol, statistic="net_r_mean")
        samples = [value for a in arms for value in (a["net_r_samples"] or [])]
        aggregates[arm]["campaign_bootstrap_mean_r"] = _campaign_bootstrap(samples)

    paired = {
        f"{a}-{b}": _paired(rows, a, b)
        for a, b in (
            ("B", "A"),
            ("C", "B"),
            ("D", "C"),
            ("D", "A"),
            ("D", "A1_no_amend"),
            ("D", "A6_fixed_geometry"),
        )
    }

    # Concentration of D net PnL and D-A improvement.
    d_rows = [(row["symbol"], row["arms"]["D"]["net_pnl"]) for row in rows if row["arms"].get("D")]
    total_d = sum(value for _, value in d_rows)
    abs_total = sum(abs(value) for _, value in d_rows)
    top = max(d_rows, key=lambda item: item[1]) if d_rows else None
    concentration = {
        "d_net_pnl_sum": total_d,
        "d_positive_symbols": sum(1 for _, value in d_rows if value > 0),
        "d_top_symbol": top[0] if top else None,
        "d_top_share_of_positive": (
            top[1] / sum(value for _, value in d_rows if value > 0)
            if top and sum(value for _, value in d_rows if value > 0) > 0
            else None
        ),
        "d_herfindahl_abs": (sum((value / abs_total) ** 2 for _, value in d_rows) if abs_total else None),
        "d_improvement_vs_a_per_symbol": {
            item["symbol"]: item["net_pnl"] for item in paired["D-A"]["per_symbol"]
        },
    }

    # Cost stress (post-hoc linear incremental cost on measured traded notional).
    stress: dict[str, Any] = {"levels_bps_per_side": list(STRESS_BPS), "arms": {}}
    for arm in (*CORE_ARMS, *ABLATION_ARMS):
        measured = [row["arms"][arm] for row in rows if row["arms"].get(arm)]
        if not measured:
            continue
        stress["arms"][arm] = {
            "net_pnl_base": sum(a["net_pnl"] for a in measured),
            "one_way_notional": sum(a["one_way_traded_notional"] for a in measured),
            "breakeven_bps_per_side": (
                sum(a["net_pnl"] for a in measured)
                / sum(a["one_way_traded_notional"] for a in measured)
                * 10_000.0
                if sum(a["one_way_traded_notional"] for a in measured) > 0
                else None
            ),
            "scenarios": {
                str(bps): sum(
                    a["net_pnl"] - (bps / 10_000.0) * a["one_way_traded_notional"] for a in measured
                )
                for bps in STRESS_BPS
            },
            "symbols_turning_negative": {
                str(bps): sum(
                    1
                    for a in measured
                    if a["net_pnl"] - (bps / 10_000.0) * a["one_way_traded_notional"] < 0
                )
                for bps in STRESS_BPS
            },
        }
    stress["basis"] = (
        "net_pnl - bps_per_side/10000 * one_way_traded_notional. This is a linear "
        "post-hoc cost adjustment on measured fills, not a re-executed fill and not "
        "a queue/liquidity claim."
    )

    # Regime split from real H1 bars (PIT labeler).
    regime_rows: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    for row in rows:
        symbol = row["symbol"]
        candles = _load_candles(symbol, tape_root)
        labels = _regime_labels(candles)
        times = [candle.end_ns for candle in candles]
        for arm in ("A", "D"):
            record = row["arms"].get(arm)
            if record is None:
                continue
            for open_record, close_record in pair_positions(record["opened"], record["closed"]):
                if close_record is None:
                    continue
                entry_ns = int(open_record.get("event_ns", 0) or 0)
                index = bisect_left(times, entry_ns)
                label = labels[index] if index < len(labels) else "UNLABELLED"
                pnl = float(Decimal(str(close_record.get("realized_pnl", "0")).split()[0]))
                regime_rows[arm][label].append(pnl)
    regime_summary: dict[str, Any] = {}
    for arm, buckets in regime_rows.items():
        regime_summary[arm] = {}
        for label, values in buckets.items():
            regime_summary[arm][label] = {
                "trades": len(values),
                "net_pnl": sum(values),
                "mean_pnl": mean(values) if values else None,
                "win_rate": sum(1 for v in values if v > 0) / len(values) if values else None,
            }
    regime_summary["basis"] = "PIT regime label at the first bar at/after entry; PnL is closed-campaign realized (net)"

    # Random-entry benchmark against D.
    random_bench: dict[str, Any] = {}
    for row in rows:
        symbol = row["symbol"]
        record = row["arms"].get("D")
        if record is None:
            continue
        candles = _load_candles(symbol, tape_root)
        random_bench[symbol] = _random_entry_benchmark(
            symbol,
            candles,
            trades=record["trades"],
            long_trades=record["long_trades"],
        )
        bench = random_bench[symbol]
        if bench and record["net_r_mean"] is not None and bench["null_mean"] is not None:
            bench["observed_mean_r"] = record["net_r_mean"]
            # Empirical one-sided p from a normal approximation of the null mean
            # is NOT used; the draw distribution is summarised and the report
            # states the position descriptively.
            bench["observed_minus_null_mean"] = record["net_r_mean"] - bench["null_mean"]
    random_bench["basis"] = (
        "matched trade count and long/short counts, random eligible entry bars, same "
        "2%/4% bracket and 96-bar cap, stop-first, 0.05 R round-trip fee proxy, no funding"
    )

    # Entry-matched C vs D diagnostic.
    matched: dict[str, Any] = {}
    for row in rows:
        c = row["arms"].get("C")
        d = row["arms"].get("D")
        if c is None or d is None:
            continue
        c_by_key = {}
        for o, cl in pair_positions(c["opened"], c["closed"]):
            if cl is None:
                continue
            key = (int(o.get("event_ns", 0) or 0), str(o.get("side", "")).upper())
            c_by_key[key] = cl
        matched_count = 0
        c_matched_pnl = d_matched_pnl = 0.0
        for o, cl in pair_positions(d["opened"], d["closed"]):
            if cl is None:
                continue
            key = (int(o.get("event_ns", 0) or 0), str(o.get("side", "")).upper())
            if key in c_by_key:
                matched_count += 1
                d_matched_pnl += float(Decimal(str(cl.get("realized_pnl", "0")).split()[0]))
                c_matched_pnl += float(Decimal(str(c_by_key[key].get("realized_pnl", "0")).split()[0]))
        matched[row["symbol"]] = {
            "d_trades": d["trades"],
            "c_trades": c["trades"],
            "entry_matched": matched_count,
            "entry_unmatched_d": d["trades"] - matched_count,
            "c_net_pnl": c["net_pnl"],
            "d_net_pnl": d["net_pnl"],
            "d_minus_c_total": d["net_pnl"] - c["net_pnl"],
            "matched_c_pnl": c_matched_pnl,
            "matched_d_pnl": d_matched_pnl,
            "matched_d_minus_c": d_matched_pnl - c_matched_pnl,
            "path_effect_unmatched": (d["net_pnl"] - c["net_pnl"]) - (d_matched_pnl - c_matched_pnl),
        }

    # Execution safety and expert coverage aggregates.
    safety = {
        "arms_with_violations": [],
        "arms_with_unlinked_orders": [],
        "arms_with_orphan_closes": [],
        "arms_with_rejected_orders": [],
        "max_abs_price_reconstruction_residual": 0.0,
        "max_abs_closed_realized_vs_net_residual": 0.0,
        "funding_cache_undercount_by_arm": {},
    }
    for row in rows:
        for arm, record in row["arms"].items():
            if record is None:
                continue
            if record["protection_violations"]:
                safety["arms_with_violations"].append(f"{row['symbol']}:{arm}")
            if record["unlinked_orders"]:
                safety["arms_with_unlinked_orders"].append(f"{row['symbol']}:{arm}")
            if record["orphan_closes"]:
                safety["arms_with_orphan_closes"].append(f"{row['symbol']}:{arm}")
            if record["rejected_orders"]:
                safety["arms_with_rejected_orders"].append(f"{row['symbol']}:{arm}")
            safety["max_abs_price_reconstruction_residual"] = max(
                safety["max_abs_price_reconstruction_residual"],
                abs(record["price_reconstruction_residual"] or 0.0),
            )
            safety["max_abs_closed_realized_vs_net_residual"] = max(
                safety["max_abs_closed_realized_vs_net_residual"],
                abs(record["closed_realized_vs_net_residual"] or 0.0),
            )
            under = safety["funding_cache_undercount_by_arm"].setdefault(arm, 0.0)
            safety["funding_cache_undercount_by_arm"][arm] = under + (record["funding_cache_undercount"] or 0.0)
    safety["funding_cache_undercount_by_arm"] = {
        arm: round(value, 6) for arm, value in safety["funding_cache_undercount_by_arm"].items()
    }

    expert: dict[str, Any] = {"by_arm": {}, "profiles": {}, "basis": ""}
    for arm in ("B", "C", "D"):
        stance_totals: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        abstain: dict[str, int] = defaultdict(int)
        profiles_enabled = set()
        bars_by_horizon: dict[str, int] = defaultdict(int)
        for row in rows:
            record = row["arms"].get(arm)
            if record is None:
                continue
            evidence = record.get("horizon_evidence") or {}
            profiles_enabled.add(evidence.get("profiles_enabled"))
            for horizon, counts in (evidence.get("stances_by_primary") or {}).items():
                for stance, value in counts.items():
                    stance_totals[horizon][stance] += int(value)
            for reason, value in (evidence.get("abstain_reasons") or {}).items():
                abstain[reason] += int(value)
            for horizon, value in (evidence.get("bars_by_horizon") or {}).items():
                bars_by_horizon[horizon] = max(bars_by_horizon[horizon], int(value))
        expert["by_arm"][arm] = {
            "profiles_enabled": sorted(str(v) for v in profiles_enabled),
            "stances_by_primary": {h: dict(sorted(c.items())) for h, c in sorted(stance_totals.items())},
            "abstain_reasons": dict(sorted(abstain.items(), key=lambda kv: (-kv[1], kv[0]))),
            "bars_by_horizon": dict(sorted(bars_by_horizon.items())),
        }
    expert["basis"] = (
        "stance counts are pooled over all symbols; per-expert activation frequency is not "
        "retained by the frozen strategy and is therefore NOT_OBSERVABLE rather than inferred"
    )

    # Expert profile table (all 28 IDs).
    try:
        from v8_next.experts.horizon_profiles import profiles_by_primary

        expert["profiles"] = {
            timeframe.value: list(ids) for timeframe, ids in profiles_by_primary().items()
        }
        expert["expert_count"] = sum(len(ids) for ids in expert["profiles"].values())
    except Exception as exc:  # pragma: no cover - environment defect
        expert["profiles_error"] = repr(exc)

    return {
        "schema_version": "v9-cross-market-analysis-v1",
        "symbols": list(raws),
        "rows": rows,
        "aggregates": aggregates,
        "paired": paired,
        "concentration": concentration,
        "cost_stress": stress,
        "regimes": regime_summary,
        "random_entry_benchmark": random_bench,
        "entry_matched_c_vs_d": matched,
        "execution_safety": safety,
        "expert_coverage": expert,
        "hypotheses_tested": {
            "arms": list(CORE_ARMS) + list(ABLATION_ARMS),
            "paired_contrasts": list(paired),
            "stress_levels": list(STRESS_BPS),
            "grammars": 5,
            "note": "exploratory programme; no multiplicity correction applied to the published p-values",
        },
    }


def _write_csv(rows: Sequence[dict[str, Any]], path: Path) -> None:
    fields = [
        "symbol",
        "arm",
        "trades",
        "net_pnl",
        "return_pct",
        "net_r_mean",
        "net_r_total",
        "max_drawdown",
        "hit_rate",
        "fee_total",
        "funding_total",
        "gross_price_pnl",
        "open_campaigns",
        "long_trades",
        "short_trades",
        "holding_hours_mean",
        "exposure_pct",
        "profit_factor",
        "one_way_traded_notional",
        "breakeven_incremental_bps_per_side",
        "price_reconstruction_residual",
        "closed_realized_vs_net_residual",
        "protection_state",
        "protection_violations",
        "unlinked_orders",
        "rejected_orders",
        "orphan_closes",
        "wall_seconds",
        "process_peak_rss_kb",
    ]
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            for arm in (*CORE_ARMS, *ABLATION_ARMS):
                record = row["arms"].get(arm)
                if record is None:
                    continue
                writer.writerow({"symbol": row["symbol"], "arm": arm, **{f: record.get(f) for f in fields[2:]}})


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--tape-root", type=Path, default=Path("research/tape"))
    parser.add_argument("--raw", type=Path, default=Path("reports/v9-cross-market-validation/raw"))
    parser.add_argument("--out", type=Path, default=Path("reports/v9-cross-market-validation"))
    args = parser.parse_args(argv)
    payload = build(args.tape_root, args.raw)
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "summary.json").write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    _write_csv(payload["rows"], args.out / "PER_SYMBOL_RESULTS.csv")
    for arm in CORE_ARMS:
        agg = payload["aggregates"][arm]
        print(
            arm,
            "symbols",
            agg["symbols"],
            "trades",
            agg["trades"],
            "pnl",
            round(agg["net_pnl_sum"], 4),
            "pooledR",
            round(agg["net_r_pooled_mean"] or 0, 6),
            "positive",
            agg["positive_symbols"],
        )
    print(args.out / "summary.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
