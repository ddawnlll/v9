"""Capital outcome projection — thin port of v8-core/src/benchmark/projection.rs (D-153).

Empirical-quantile bands plus seed-pinned Monte Carlo futures (``numpy`` owns
the RNG; per-simulation streams via ``SeedSequence.spawn`` mirroring the Rust
splitmix intent). Fail-closed guards preserved verbatim: credibility floor
0.20, synthetic-only rejected (BFS-004), capital > $100k rejected without a
capacity model (BFS-018), n < 5 rejected, extreme quantiles suppressed for
n < 25 (BFS-022). ``is_realized_pnl`` is always False; forward claims stay
unauthorized on diagnostic paths. NO_ECONOMIC_CLAIM throughout.

DIVERGENCE (named): the Rust entry takes ``&BenchmarkReceipt`` (which carries
``composite_capability_score``); the Python receipt has no such field, so this
port takes the capability score + receipt id as primitives. No parallel
receipt ontology is invented.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import StrEnum

import numpy as np

__all__ = [
    "CapitalOutcomeProjection",
    "MonteCarloFutureResult",
    "ProjectedOutcomeBand",
    "ProjectionGrade",
]

CREDIBILITY_FLOOR = 0.20
CAPITAL_BASELINE_USD = 100_000.0
RUIN_EQUITY_FRACTION = 0.70


class ProjectionGrade(StrEnum):
    GRADE_U = "GradeU"
    GRADE_D = "GradeD"
    GRADE_C = "GradeC"
    GRADE_B = "GradeB"
    GRADE_A = "GradeA"


@dataclass(frozen=True)
class ProjectedOutcomeBand:
    percentile: float
    return_bps: float
    max_drawdown_bps: float
    terminal_capital_usd: float

    def as_dict(self) -> dict[str, object]:
        return {
            "percentile": self.percentile,
            "return_bps": self.return_bps,
            "max_drawdown_bps": self.max_drawdown_bps,
            "terminal_capital_usd": self.terminal_capital_usd,
        }


@dataclass(frozen=True)
class MonteCarloFutureResult:
    n_simulations: int
    horizon_trades: int
    initial_capital_usd: float
    p5_terminal_usd: float
    p5_return_pct: float
    p25_terminal_usd: float
    p25_return_pct: float
    p50_terminal_usd: float
    p50_return_pct: float
    p75_terminal_usd: float
    p75_return_pct: float
    p95_terminal_usd: float | None
    p95_return_pct: float | None
    risk_of_ruin_pct: float
    worst_scenario_return_pct: float
    best_scenario_return_pct: float
    conditional_notice: str

    def as_dict(self) -> dict[str, object]:
        return {
            "n_simulations": self.n_simulations,
            "horizon_trades": self.horizon_trades,
            "initial_capital_usd": self.initial_capital_usd,
            "p5_terminal_usd": self.p5_terminal_usd,
            "p5_return_pct": self.p5_return_pct,
            "p25_terminal_usd": self.p25_terminal_usd,
            "p25_return_pct": self.p25_return_pct,
            "p50_terminal_usd": self.p50_terminal_usd,
            "p50_return_pct": self.p50_return_pct,
            "p75_terminal_usd": self.p75_terminal_usd,
            "p75_return_pct": self.p75_return_pct,
            "p95_terminal_usd": self.p95_terminal_usd,
            "p95_return_pct": self.p95_return_pct,
            "risk_of_ruin_pct": self.risk_of_ruin_pct,
            "worst_scenario_return_pct": self.worst_scenario_return_pct,
            "best_scenario_return_pct": self.best_scenario_return_pct,
            "conditional_notice": self.conditional_notice,
        }


@dataclass(frozen=True)
class CapitalOutcomeProjection:
    policy_id: str
    benchmark_receipt_id: str
    is_realized_pnl: bool
    initial_capital_usd: float
    projection_grade: ProjectionGrade
    sample_size: int
    outcome_bands: tuple[ProjectedOutcomeBand, ...]
    epistemic_status: str
    forward_claim_authorized: bool
    monte_carlo_futures: MonteCarloFutureResult | None
    claim: str = "NO_ECONOMIC_CLAIM"

    def as_dict(self) -> dict[str, object]:
        return {
            "policy_id": self.policy_id,
            "benchmark_receipt_id": self.benchmark_receipt_id,
            "is_realized_pnl": self.is_realized_pnl,
            "initial_capital_usd": self.initial_capital_usd,
            "projection_grade": self.projection_grade.value,
            "sample_size": self.sample_size,
            "outcome_bands": [b.as_dict() for b in self.outcome_bands],
            "epistemic_status": self.epistemic_status,
            "forward_claim_authorized": self.forward_claim_authorized,
            "monte_carlo_futures": self.monte_carlo_futures.as_dict()
            if self.monte_carlo_futures
            else None,
            "claim": self.claim,
        }


def _quantile(sorted_vals: list[float], pct: float) -> float:
    if not sorted_vals:
        raise ValueError("PROJECTION_EMPTY: no values for quantile")
    pos = (len(sorted_vals) - 1) * pct
    lo, hi = math.floor(pos), math.ceil(pos)
    if lo == hi:
        return sorted_vals[int(pos)]
    frac = pos - lo
    return sorted_vals[lo] * (1.0 - frac) + sorted_vals[hi] * frac


def project_from_returns(
    *,
    policy_id: str,
    benchmark_receipt_id: str,
    capability_score: float,
    trade_returns_bps: list[float],
    initial_capital_usd: float,
    has_synthetic_population_only: bool,
    grade: ProjectionGrade = ProjectionGrade.GRADE_D,
    mc_simulations: int = 0,
    mc_horizon_trades: int = 0,
    mc_seed: int = 454,
) -> CapitalOutcomeProjection:
    if capability_score < CREDIBILITY_FLOOR:
        raise ValueError("PROJECTION_FLOOR: capability score below 0.20 credibility floor")
    if has_synthetic_population_only:
        raise ValueError("PROJECTION_BFS004: synthetic populations cannot weight forward claims")
    if not math.isfinite(initial_capital_usd) or initial_capital_usd <= 0.0:
        raise ValueError("PROJECTION_CAPITAL: invalid initial capital")
    if initial_capital_usd > CAPITAL_BASELINE_USD:
        raise ValueError("PROJECTION_BFS018: capital exceeds baseline without capacity model")
    n = len(trade_returns_bps)
    if n < 5:
        raise ValueError("PROJECTION_SAMPLE: minimum 5 trades required")
    if any(not math.isfinite(v) for v in trade_returns_bps):
        raise ValueError("PROJECTION_DATA: non-finite trade return")
    ordered = sorted(trade_returns_bps)
    percentiles = (0.25, 0.50, 0.75) if n < 25 else (0.05, 0.25, 0.50, 0.75, 0.95)
    bands = tuple(
        ProjectedOutcomeBand(
            percentile=p,
            return_bps=_quantile(ordered, p),
            max_drawdown_bps=min(0.0, ordered[0]),
            terminal_capital_usd=initial_capital_usd * (1.0 + _quantile(ordered, p) / 10_000.0),
        )
        for p in percentiles
    )
    monte_carlo = None
    if mc_simulations > 0 and mc_horizon_trades > 0:
        monte_carlo = simulate_monte_carlo_futures(
            trade_returns_bps, initial_capital_usd, mc_simulations, mc_horizon_trades, mc_seed
        )
    return CapitalOutcomeProjection(
        policy_id=policy_id,
        benchmark_receipt_id=benchmark_receipt_id,
        is_realized_pnl=False,
        initial_capital_usd=initial_capital_usd,
        projection_grade=grade,
        sample_size=n,
        outcome_bands=bands,
        epistemic_status="DIAGNOSTIC_ONLY",
        forward_claim_authorized=False,
        monte_carlo_futures=monte_carlo,
    )


def simulate_monte_carlo_futures(
    trade_returns_bps: list[float],
    initial_capital_usd: float,
    n_simulations: int,
    horizon_trades: int,
    seed: int,
) -> MonteCarloFutureResult:
    if not trade_returns_bps or any(not math.isfinite(v) for v in trade_returns_bps):
        raise ValueError("PROJECTION_MC_DATA: missing or invalid trade returns")
    if not math.isfinite(initial_capital_usd) or initial_capital_usd <= 0.0:
        raise ValueError("PROJECTION_MC_CAPITAL: invalid initial capital")
    if n_simulations == 0 or horizon_trades == 0:
        raise ValueError("PROJECTION_MC_DIMS: invalid Monte Carlo dimensions")
    ruin_line = initial_capital_usd * RUIN_EQUITY_FRACTION
    returns = np.asarray(trade_returns_bps, dtype=float)
    children = np.random.SeedSequence(seed).spawn(n_simulations)
    terminals: list[float] = []
    ruins = 0
    for child in children:
        rng = np.random.default_rng(child)
        picks = rng.integers(0, len(returns), size=horizon_trades)
        equity = float(initial_capital_usd)
        breached = False
        for ret_bps in returns[picks]:
            equity += equity * (float(ret_bps) / 10_000.0)
            if equity <= ruin_line:
                breached = True
        terminals.append(equity)
        ruins += 1 if breached else 0
    ordered = sorted(terminals)
    rets = sorted((t / initial_capital_usd - 1.0) * 100.0 for t in terminals)
    return MonteCarloFutureResult(
        n_simulations=n_simulations,
        horizon_trades=horizon_trades,
        initial_capital_usd=initial_capital_usd,
        p5_terminal_usd=_quantile(ordered, 0.05),
        p5_return_pct=_quantile(rets, 0.05),
        p25_terminal_usd=_quantile(ordered, 0.25),
        p25_return_pct=_quantile(rets, 0.25),
        p50_terminal_usd=_quantile(ordered, 0.50),
        p50_return_pct=_quantile(rets, 0.50),
        p75_terminal_usd=_quantile(ordered, 0.75),
        p75_return_pct=_quantile(rets, 0.75),
        p95_terminal_usd=_quantile(ordered, 0.95),
        p95_return_pct=_quantile(rets, 0.95),
        risk_of_ruin_pct=ruins / n_simulations * 100.0,
        worst_scenario_return_pct=rets[0],
        best_scenario_return_pct=rets[-1],
        conditional_notice="Conditional on the supplied return distribution repeating; not a future-profit probability.",
    )
