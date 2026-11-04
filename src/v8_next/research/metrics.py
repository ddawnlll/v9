"""Shared economic metrics, uncertainty intervals and the common result schema.

Every experiment reports through :func:`common_result` so the cross-experiment
comparison is machine-readable and cannot drift. Missing values stay ``None``;
they are never coerced to zero.
"""

from __future__ import annotations

import math
import random
import statistics
from typing import Any, Iterable, Mapping, Sequence

METRIC_FIELDS: tuple[str, ...] = (
    "trade_count",
    "independent_opportunity_count",
    "wins",
    "losses",
    "win_rate",
    "average_win_r",
    "average_loss_r",
    "gross_expectancy_r",
    "net_expectancy_r",
    "net_profit_factor",
    "total_net_pnl",
    "maximum_drawdown",
    "median_holding_bars",
    "fee_drag_r",
    "long_trade_count",
    "short_trade_count",
    "long_net_expectancy_r",
    "short_net_expectancy_r",
)


def _finite(values: Iterable[Any]) -> list[float]:
    out: list[float] = []
    for value in values:
        if value is None:
            continue
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(number):
            out.append(number)
    return out


def mean(values: Iterable[Any]) -> float | None:
    data = _finite(values)
    return statistics.fmean(data) if data else None


def median(values: Iterable[Any]) -> float | None:
    data = _finite(values)
    return statistics.median(data) if data else None


def profit_factor(net_values: Sequence[float]) -> float | None:
    gains = sum(v for v in net_values if v > 0)
    losses = -sum(v for v in net_values if v < 0)
    if losses == 0:
        return None if gains == 0 else float("inf")
    return gains / losses


def max_drawdown(net_values: Sequence[float]) -> float | None:
    """Peak-to-trough on the cumulative net series, in the same units as input."""
    if not net_values:
        return None
    cumulative = 0.0
    peak = 0.0
    worst = 0.0
    for value in net_values:
        cumulative += value
        peak = max(peak, cumulative)
        worst = min(worst, cumulative - peak)
    return abs(worst)


def bootstrap_mean_ci(
    values: Sequence[float],
    *,
    seed: int,
    resamples: int = 1500,
    alpha: float = 0.05,
) -> dict[str, Any] | None:
    """Deterministic percentile bootstrap for the mean of ``values``.

    Refuses to invent precision from a handful of trades: fewer than 5 finite
    observations returns ``None`` and the caller must report the sample size.
    """
    data = _finite(values)
    if len(data) < 5 or resamples <= 0:
        return None
    rng = random.Random(seed)
    n = len(data)
    means: list[float] = []
    for _ in range(resamples):
        total = 0.0
        for _ in range(n):
            total += data[rng.randrange(n)]
        means.append(total / n)
    means.sort()
    low = means[int(math.floor(alpha / 2 * resamples))]
    high = means[min(resamples - 1, int(math.ceil((1 - alpha / 2) * resamples)) - 1)]
    return {
        "estimate": statistics.fmean(data),
        "ci_low": low,
        "ci_high": high,
        "alpha": alpha,
        "resamples": resamples,
        "seed": seed,
        "n": n,
        "contains_zero": low <= 0.0 <= high,
    }


def paired_delta_ci(
    pairs: Sequence[tuple[float | None, float | None]],
    *,
    seed: int,
    resamples: int = 1500,
) -> dict[str, Any] | None:
    """Bootstrap CI of the per-unit delta (candidate - baseline), paired by unit."""
    deltas = [b - a for a, b in pairs if a is not None and b is not None]
    return bootstrap_mean_ci(deltas, seed=seed, resamples=resamples)


def paired_delta_ci_multi(
    pairs: Sequence[tuple[float, float, float]],
    *,
    seed: int,
    resamples: int = 1500,
) -> dict[str, Any] | None:
    """Paired delta over ``k`` arms sharing the same units.

    ``pairs`` rows are ``(baseline, arm_a, arm_b)``; the reported deltas are
    chunked so each ``k - 1`` treatment is compared with the same baseline.
    """
    if not pairs:
        return None
    base = [row[0] for row in pairs]
    out: dict[str, Any] = {}
    for index in range(1, len(pairs[0])):
        deltas = [row[index] - row[0] for row in pairs]
        out[f"arm_{index}_minus_baseline"] = bootstrap_mean_ci(
            deltas, seed=seed + index, resamples=resamples
        )
    out["baseline_mean"] = mean(base)
    out["n_pairs"] = len(pairs)
    return out


def summarize(
    rows: Sequence[Mapping[str, Any]],
    *,
    net_key: str = "realized_net_r",
    gross_key: str = "realized_gross_r",
    pnl_key: str = "realized_net_pnl_usdt",
    fee_key: str = "fees_usdt",
    direction_key: str = "direction",
    holding_key: str = "holding_bars",
) -> dict[str, Any]:
    """Economic summary of trade-level rows. Null means unknown, never zero."""
    net = _finite(row.get(net_key) for row in rows)
    gross = _finite(row.get(gross_key) for row in rows)
    pnl = _finite(row.get(pnl_key) for row in rows)
    wins = [v for v in net if v > 0]
    losses = [v for v in net if v <= 0]

    def direction_expectancy(label: str) -> tuple[int | None, float | None]:
        subset = [
            float(row[net_key])
            for row in rows
            if str(row.get(direction_key) or "").upper().startswith(label)
            and row.get(net_key) is not None
        ]
        return (len(subset) or None, statistics.fmean(subset) if subset else None)

    long_count, long_exp = direction_expectancy("LONG")
    short_count, short_exp = direction_expectancy("SHORT")
    fee_drag = [
        float(row[fee_key]) / float(row["initial_risk_usdt"])
        for row in rows
        if row.get(fee_key) is not None and row.get("initial_risk_usdt")
    ]

    return {
        "trade_count": len(rows),
        "independent_opportunity_count": len(
            {str(row.get("opportunity_id")) for row in rows if row.get("opportunity_id")}
        ) or None,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": (len(wins) / len(net)) if net else None,
        "average_win_r": mean(wins),
        "average_loss_r": mean(losses),
        "gross_expectancy_r": mean(gross),
        "net_expectancy_r": mean(net),
        "net_profit_factor": profit_factor(net),
        "total_net_pnl": sum(pnl) if pnl else None,
        "maximum_drawdown": max_drawdown(pnl) if pnl else None,
        "median_holding_bars": median(row.get(holding_key) for row in rows),
        "fee_drag_r": mean(fee_drag) if fee_drag else None,
        "long_trade_count": long_count,
        "short_trade_count": short_count,
        "long_net_expectancy_r": long_exp,
        "short_net_expectancy_r": short_exp,
    }


def common_result(
    *,
    experiment_id: str,
    status: str,
    baseline_policy_id: str,
    candidate_policy_id: str | None,
    dataset_id: str,
    baseline_metrics: Mapping[str, Any] | None,
    candidate_metrics: Mapping[str, Any] | None = None,
    paired_delta_net_r: float | None = None,
    uncertainty_interval: Mapping[str, Any] | None = None,
    economic_evidence_class: str = "MODEL_DERIVED",
    causal_validity: str = "NOT_YET_VALIDATED",
    native_replay_validated: bool = False,
    limitations: Sequence[str] = (),
    evidence_paths: Sequence[str] = (),
) -> dict[str, Any]:
    """One canonical cross-experiment result row (schema §23)."""
    if status not in {"SUPPORTED", "REJECTED", "INCONCLUSIVE", "BLOCKED"}:
        raise ValueError(f"illegal experiment status: {status}")
    base = baseline_metrics or {}
    cand = candidate_metrics or {}
    return {
        "experiment_id": experiment_id,
        "status": status,
        "baseline_policy_id": baseline_policy_id,
        "candidate_policy_id": candidate_policy_id,
        "dataset_id": dataset_id,
        "entry_count_baseline": base.get("trade_count"),
        "entry_count_candidate": cand.get("trade_count"),
        "net_expectancy_r_baseline": base.get("net_expectancy_r"),
        "net_expectancy_r_candidate": cand.get("net_expectancy_r"),
        "paired_delta_net_r": paired_delta_net_r,
        "net_profit_factor_baseline": base.get("net_profit_factor"),
        "net_profit_factor_candidate": cand.get("net_profit_factor"),
        "maximum_drawdown_baseline": base.get("maximum_drawdown"),
        "maximum_drawdown_candidate": cand.get("maximum_drawdown"),
        "fee_drag_difference_r": (
            None
            if base.get("fee_drag_r") is None or cand.get("fee_drag_r") is None
            else cand["fee_drag_r"] - base["fee_drag_r"]
        ),
        "uncertainty_interval": dict(uncertainty_interval) if uncertainty_interval else None,
        "economic_evidence_class": economic_evidence_class,
        "causal_validity": causal_validity,
        "native_replay_validated": native_replay_validated,
        "limitations": list(limitations),
        "evidence_paths": list(evidence_paths),
    }
