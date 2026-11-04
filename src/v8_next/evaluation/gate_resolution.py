"""D-153 / D-152 Full Gate Resolution Pipeline (Rule 12, Rule 30, Rule 57).

Resolves Gates G3 through G9 through genuine empirical calculations:
- G3: Scenario Robustness across 4 market regimes (Bull Trend, Bear Crash, Chop/Range, High-Vol Spill).
- G4: Synthetic / Adversarial Falsification with slippage, latency jitter, drop rate, and bracket stop-loss.
- G5: Selection Control via Deflated Sharpe Ratio (DSR >= 0.95, p <= 0.05) and White's Reality Check (WRC).
- G6: Frozen Out-of-Sample (OOS) Replication with >= 60% performance retention.
- G7: Prospective Shadow Streaming with causal e-process martingale and drift tracking.
- G8: Live Realization (D-152 §5 Diagnostic Fold NOT_APPLICABLE in research phase, or PASS with signed fills).
- G9: Central ClaimRegistry verification of ledger hash chain and StatutoryClaimRecord minting.
"""

from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, Sequence, cast

import numpy as np
import polars as pl
from nautilus_trader.execution import ProbabilisticFillModel, StaticLatencyModel

from v8_next.adapters.expert_strategy import (
    ExpertStrategyConfig,
    run_expert_strategy_backtest,
)
from v8_next.domain.market import Candle
from v8_next.evaluation.alignment import IntervalLoss
from v8_next.evaluation.benchmark_receipt import (
    RECEIPT_CANON_TABLE,
    BenchmarkLedger,
    GateState,
    GateVector,
)
from v8_next.evaluation.claims import ClaimRegistry, StatutoryClaimRecord
from v8_next.evaluation.deflated_sharpe import DSRPlan, deflated_sharpe_diagnostic
from v8_next.evaluation.economic_benchmark import (
    bars_from_candles,
    campaign_accounting,
    parse_money,
)
from v8_next.evaluation.reality_check import reality_check_diagnostic
from v8_next.evaluation.scoring import evaluate_gate_vector
from v8_next.evaluation.statistics_plan import (
    CANONICAL_G5_BLOCK_SIZE,
    SelectionFamily,
    StatisticsPlan,
    g5_plan,
    sample_sufficiency,
)

#: What a gate measured, or why it could not. A gate that cannot bind its verdict
#: to a declared window publishes ``UNMEASURED``: absence of evidence is published
#: as absence, never as a PASS over whatever the caller happened to load.
MEASURED = "measured"
UNMEASURED = "unmeasured"

#: Named reason tokens for the window-binding refusals of G6 and G7 (#406, #421).
#: A caller-loaded slice is not a window declaration, and a gate may only publish a
#: verdict over a window it can name and check against the bars it was handed.
PROTECTED_WINDOW_ABSENT = "PROTECTED_WINDOW_ABSENT"
PROTECTED_WINDOW_MISMATCH = "PROTECTED_WINDOW_MISMATCH"
PROSPECTIVE_WINDOW_ABSENT = "PROSPECTIVE_WINDOW_ABSENT"
PROSPECTIVE_WINDOW_MISMATCH = "PROSPECTIVE_WINDOW_MISMATCH"
PROSPECTIVE_WINDOW_NOT_FORWARD = "PROSPECTIVE_WINDOW_NOT_FORWARD_OF_SCORED_WINDOW"


def window_identity(candles: Sequence[Candle]) -> dict[str, Any] | None:
    """Span + bar count of a bar sequence, or ``None`` when it holds no bars."""
    bars = tuple(candles)
    if not bars:
        return None
    return {
        "start_ns": int(min(c.start_ns for c in bars)),
        "end_ns": int(max(c.end_ns for c in bars)),
        "n_bars": len(bars),
    }


@dataclass(frozen=True)
class WindowBinding:
    """A window a caller declares in explicit units, before any verdict exists.

    ``start_ns``/``end_ns``/``n_bars`` are the whole declaration: a gate compares
    them against the bars it was handed, so its verdict can name the window it was
    measured over instead of inheriting whatever slice the caller loaded (#406 G7's
    ``candles[-100:]``; #421 G6's file-order ``head(limit)`` window). ``origin``
    names where the declaration came from (a run key, a fold id, a test fixture).
    """

    start_ns: int
    end_ns: int
    n_bars: int
    origin: str = ""
    declared_by: str = "caller"

    @classmethod
    def from_candles(
        cls,
        candles: Sequence[Candle],
        *,
        origin: str = "",
        declared_by: str = "caller",
    ) -> WindowBinding:
        identity = window_identity(candles)
        if identity is None:
            raise ValueError("a window binding needs at least one bar")
        return cls(
            start_ns=int(identity["start_ns"]),
            end_ns=int(identity["end_ns"]),
            n_bars=int(identity["n_bars"]),
            origin=origin,
            declared_by=declared_by,
        )

    def mismatch(self, candles: Sequence[Candle]) -> str | None:
        """``None`` when this declaration describes exactly these bars."""
        identity = window_identity(candles)
        if identity is None:
            return "the evaluated window holds no bars"
        declared = (int(self.start_ns), int(self.end_ns), int(self.n_bars))
        evaluated = (int(identity["start_ns"]), int(identity["end_ns"]), int(identity["n_bars"]))
        if declared == evaluated:
            return None
        return f"declared (start_ns, end_ns, n_bars)={declared} != evaluated {evaluated}"

    def as_dict(self) -> dict[str, Any]:
        return {
            "start_ns": int(self.start_ns),
            "end_ns": int(self.end_ns),
            "n_bars": int(self.n_bars),
            "origin": self.origin,
            "declared_by": self.declared_by,
        }


def _default_tape_path() -> Path:
    """Repo-root-anchored default tape, so the path cannot depend on the cwd.

    The default used to be the bare relative path ``research/tape/...``, which
    resolves to nothing whenever a run starts from ``v8-next/`` (the pytest
    rootdir) or from any other directory. Consequences measured on this tree:
    the G5 regime fallback below skips itself with ``if DEFAULT_TAPE_PATH.exists()``
    -- the same nominal run grades a gate differently depending on where it was
    launched -- and the real-tape tests that guard on the same constant skip
    silently instead of running. Both are correctness defects that look like
    speed. ``V8_TAPE_PATH`` overrides it for relocated tapes.
    """
    override = os.environ.get("V8_TAPE_PATH")
    if override:
        return Path(override)
    root = Path(__file__).resolve().parents[3]
    return root / "research" / "tape" / "btcusdt-1h-12m" / "tape.jsonl"


DEFAULT_TAPE_PATH = _default_tape_path()


def extract_account_balance(
    account: dict[str, Any], default: float | None = None
) -> float | None:
    """Extract a measured balance, or ``None`` when the account is unusable.

    A missing or malformed balance must never become a fabricated 10000. A real
    zero is a measurement and is returned as ``0.0``; only absence/parse failure
    falls through to ``default``.
    """
    value = account.get("balance_total")
    if value is not None:
        val_str = str(value).split()[0]
        try:
            return float(val_str)
        except (ValueError, TypeError):
            pass
    value = account.get("balance")
    if value is not None:
        try:
            return float(value)
        except (ValueError, TypeError):
            pass
    return default


def load_tape_candles(
    tape_path: Path | str | None = None,
    limit: int | None = None,
    instrument: str | None = None,
    *,
    start_ms: int | None = None,
    end_ms: int | None = None,
) -> list[Candle]:
    """Load real 1-hour candles from verified venue capture tape using Polars.

    When ``instrument`` is given (e.g. ``\"BTCUSDT\"`` on the multi-symbol
    ``multi-1h-4y`` tape), only kline rows for that instrument are returned
    and the limit applies *after* the filter, so interleaved symbols never
    leak into a single-instrument candle series. ``None`` keeps the legacy
    single-symbol behaviour (BTCUSDT-PERP.BINANCE).
    """
    p = Path(tape_path) if tape_path is not None else DEFAULT_TAPE_PATH
    if not p.exists():
        raise FileNotFoundError(f"Tape dataset not found at {p}")

    if p.is_dir():
        tape_jsonl = p / "tape.jsonl"
        if tape_jsonl.exists():
            p = tape_jsonl
        else:
            import zipfile

            zip_files = sorted(p.glob(f"{instrument or 'BTCUSDT'}-1h-*.zip"))
            if not zip_files:
                raise FileNotFoundError(f"No kline files found in directory {p}")
            candles: list[Candle] = []
            for zf_path in zip_files:
                with zipfile.ZipFile(zf_path) as z:
                    for name in z.namelist():
                        if name.endswith(".csv"):
                            with z.open(name) as f:
                                df_csv = pl.read_csv(f, has_header=True)
                                for row in df_csv.iter_rows(named=True):
                                    candles.append(
                                        Candle(
                                            instrument_id=f"{instrument or 'BTCUSDT'}-PERP.BINANCE",
                                            start_ns=int(row["open_time"]) * 1_000_000,
                                            end_ns=(int(row["close_time"]) + 1) * 1_000_000,
                                            open=Decimal(str(row["open"])),
                                            high=Decimal(str(row["high"])),
                                            low=Decimal(str(row["low"])),
                                            close=Decimal(str(row["close"])),
                                            volume=Decimal(str(row["volume"])),
                                            received_ns=(int(row["close_time"]) + 1) * 1_000_000,
                                            available_ns=(int(row["close_time"]) + 1) * 1_000_000,
                                            source_hash=f"binance-zip-{zf_path.name}",
                                        )
                                    )
                                    if limit is not None and len(candles) >= limit:
                                        return candles
            return candles

    # Lazy scan: filters and the limit are pushed into the reader, so a bounded
    # UTC window never materialises the whole tape (NX05.R3, 226 MB stays bounded).
    lf = pl.scan_ndjson(p).filter(pl.col("channel") == "kline")
    if instrument is not None:
        lf = lf.filter(pl.col("instrument") == instrument)
    if start_ms is not None:
        lf = lf.filter(pl.col("payload").struct.field("open_time_ms") >= start_ms)
    if end_ms is not None:
        lf = lf.filter(pl.col("payload").struct.field("open_time_ms") < end_ms)
    lf = lf.sort("event_time")
    if limit is not None:
        lf = lf.head(limit)
    kline_df = lf.collect()

    payloads = kline_df["payload"].to_list()
    if not payloads:
        raise FileNotFoundError(
            f"no kline rows for instrument={instrument!r} in window "
            f"[{start_ms},{end_ms}) of {p}"
        )
    symbol = instrument or "BTCUSDT"
    candles = [
        Candle(
            instrument_id=f"{symbol}-PERP.BINANCE",
            start_ns=int(row["open_time_ms"]) * 1_000_000,
            end_ns=(int(row["close_time_ms"]) + 1) * 1_000_000,
            open=Decimal(str(row["open"])),
            high=Decimal(str(row["high"])),
            low=Decimal(str(row["low"])),
            close=Decimal(str(row["close"])),
            volume=Decimal(str(row["volume"])),
            received_ns=(int(row["close_time_ms"]) + 1) * 1_000_000,
            available_ns=(int(row["close_time_ms"]) + 1) * 1_000_000,
            source_hash=str(row["payload_hash"]),
        )
        for row in payloads
    ]
    return candles


def classify_market_regimes(
    tape_path: Path | str | None = None,
    candles: Sequence[Candle] | None = None,
    slice_length: int = 100,
) -> dict[str, list[Candle]]:
    """Partition real tape data into 4 canonical market regimes using Polars.

    Regimes:
    1. Bull Trend: sustained positive return momentum.
    2. Bear Crash: severe downward excursion.
    3. Chop/Range: low directional movement / mean-reverting.
    4. High-Vol Spill: upper tail of rolling volatility.
    """
    if candles is None:
        p = Path(tape_path) if tape_path is not None else DEFAULT_TAPE_PATH
        all_candles = load_tape_candles(p)
    else:
        all_candles = list(candles)

    n = len(all_candles)
    eff_len = min(slice_length, max(1, n // 4))

    if n < 48:
        quarter = max(1, n // 4)
        return {
            "Bull Trend": all_candles[0:quarter],
            "Bear Crash": all_candles[quarter : 2 * quarter],
            "Chop/Range": all_candles[2 * quarter : 3 * quarter],
            "High-Vol Spill": all_candles[3 * quarter :],
        }

    records = [
        {
            "idx": i,
            "close": float(c.close),
            "high": float(c.high),
            "low": float(c.low),
        }
        for i, c in enumerate(all_candles)
    ]

    df = (
        pl.DataFrame(records)
        .with_columns(
            (pl.col("close").log() - pl.col("close").shift(1).log()).alias("log_ret"),
            (pl.col("close") / pl.col("close").shift(eff_len) - 1.0).alias("ret_w"),
        )
        .with_columns(
            pl.col("log_ret").rolling_std(window_size=eff_len).alias("vol_w"),
        )
    )

    valid_df = df.with_row_index().filter(
        pl.col("ret_w").is_not_null() & pl.col("vol_w").is_not_null()
    )
    if len(valid_df) == 0:
        quarter = max(1, n // 4)
        return {
            "Bull Trend": all_candles[0:quarter],
            "Bear Crash": all_candles[quarter : 2 * quarter],
            "Chop/Range": all_candles[2 * quarter : 3 * quarter],
            "High-Vol Spill": all_candles[3 * quarter :],
        }

    # 1. Bull Trend: window with maximum cumulative positive return
    arg_bull = valid_df["ret_w"].arg_max()
    idx_bull_end = int(valid_df["index"][arg_bull if arg_bull is not None else 0] or 0)
    bull_start = max(0, idx_bull_end - eff_len)

    # 2. Bear Crash: window with minimum (most negative) return
    arg_bear = valid_df["ret_w"].arg_min()
    idx_bear_end = int(valid_df["index"][arg_bear if arg_bear is not None else 0] or 0)
    bear_start = max(0, idx_bear_end - eff_len)

    # 3. High-Vol Spill: window with maximum rolling volatility
    arg_vol = valid_df["vol_w"].arg_max()
    idx_vol_end = int(valid_df["index"][arg_vol if arg_vol is not None else 0] or 0)
    vol_start = max(0, idx_vol_end - eff_len)

    # 4. Chop/Range: window with lowest absolute return among sub-median volatility
    med_val = valid_df["vol_w"].median()
    med_vol = float(str(med_val)) if med_val is not None else 0.01
    chop_pool = valid_df.filter(pl.col("vol_w") <= med_vol)
    if len(chop_pool) == 0:
        chop_pool = valid_df
    arg_chop = chop_pool["ret_w"].abs().arg_min()
    idx_chop_end = int(chop_pool["index"][arg_chop if arg_chop is not None else 0] or 0)
    chop_start = max(0, idx_chop_end - eff_len)

    return {
        "Bull Trend": all_candles[bull_start : bull_start + eff_len],
        "Bear Crash": all_candles[bear_start : bear_start + eff_len],
        "Chop/Range": all_candles[chop_start : chop_start + eff_len],
        "High-Vol Spill": all_candles[vol_start : vol_start + eff_len],
    }


#: G3 accounting contract. Every observation is monetary USDT relative to this
#: declared starting equity; the variance series is the dimensionless campaign
#: return on that same equity. The two are never mixed in one list.
G3_INITIAL_EQUITY = 10000.0
G3_TAKER_FEE = 0.0005
G3_PNL_UNIT = "USDT"
G3_RETURN_UNIT = "fraction_of_initial_equity"


def _g3_money(value: Any) -> float | None:
    parsed = parse_money(value)
    return None if parsed is None else float(parsed)


def _g3_open_mark_pnl(
    record: dict[str, Any], mark: float, taker_fee: float
) -> tuple[float, float] | None:
    """Open-position PnL and entry notional in USDT, or None if unmeasurable.

    The mark-to-market is ``(mark - entry) * quantity * direction`` minus the
    declared entry taker fee. It is a monetary value, never a bare return.
    """
    quantity = _g3_money(record.get("quantity"))
    entry = _g3_money(record.get("avg_px_open"))
    if quantity is None or entry is None or quantity <= 0 or entry <= 0:
        return None
    side = str(record.get("side", "")).upper()
    if side in ("BUY", "LONG"):
        direction = 1.0
    elif side in ("SELL", "SHORT"):
        direction = -1.0
    else:
        return None
    notional = abs(quantity) * entry
    gross = (mark - entry) * quantity * direction
    fee = notional * taker_fee
    return gross - fee, notional


def _g3_pair_regime(
    opened: Sequence[dict[str, Any]],
    closed: Sequence[dict[str, Any]],
) -> tuple[list[tuple[int, float | None, float | None, str, dict[str, Any]]], list[str]]:
    """Pair campaigns chronologically and return (ts, pnl_usdt, notional, kind).

    Closes are consumed once per open on the same position slot, in event order.
    A close that no open can consume, or a second close of an already reduced
    slot, is reported instead of being silently folded into another campaign.
    """
    opens = sorted(
        (o for o in opened if isinstance(o, dict)),
        key=lambda o: int(o.get("event_ns", 0) or 0),
    )
    closes_by_slot: dict[str, list[dict[str, Any]]] = {}
    for close in sorted(
        (c for c in closed if isinstance(c, dict)),
        key=lambda c: int(c.get("event_ns", 0) or 0),
    ):
        closes_by_slot.setdefault(str(close.get("position_id", "")), []).append(close)
    observations: list[
        tuple[int, float | None, float | None, str, dict[str, Any]]
    ] = []
    problems: list[str] = []
    consumed: set[int] = set()
    for open_rec in opens:
        slot = str(open_rec.get("position_id", ""))
        open_ns = int(open_rec.get("event_ns", 0) or 0)
        match = None
        for close in closes_by_slot.get(slot, []):
            if id(close) in consumed:
                continue
            if int(close.get("event_ns", 0) or 0) < open_ns:
                continue
            match = close
            break
        if match is None:
            observations.append((open_ns, None, None, "open", open_rec))
            continue
        consumed.add(id(match))
        pnl = _g3_money(match.get("realized_pnl"))
        entry_notional = None
        quantity = _g3_money(open_rec.get("quantity"))
        entry = _g3_money(open_rec.get("avg_px_open"))
        if quantity is not None and entry is not None:
            entry_notional = abs(quantity) * entry
        if pnl is None:
            problems.append(f"CLOSE_PNL_UNPARSABLE:{slot}")
            continue
        observations.append(
            (
                int(match.get("event_ns", 0) or 0),
                pnl,
                entry_notional or 0.0,
                "closed",
                open_rec,
            )
        )
    for slot, closes in closes_by_slot.items():
        for close in closes:
            if id(close) not in consumed:
                problems.append(f"ORPHAN_CLOSE:{slot}")
    return observations, problems


def evaluate_g3_scenario_robustness(
    regimes: dict[str, list[Candle]],
    strategy_config: ExpertStrategyConfig | None = None,
    max_drawdown_limit: float = 0.10,
    min_trades_per_regime: int = 1,
    *,
    initial_equity: float = G3_INITIAL_EQUITY,
    taker_fee: float = G3_TAKER_FEE,
) -> tuple[GateState, dict[str, Any]]:
    """G3: Scenario Robustness (g3_scenario_robustness).

    Executes backtests across the 4 primary market regimes (Bull Trend, Bear Crash,
    Chop/Range, High-Vol Spill) and verifies that maximum drawdown and return
    variance do not explode across regimes. Every evaluated regime must contribute
    at least `min_trades_per_regime` trades: robustness across a regime with zero
    trades is unmeasurable and fails closed with INSUFFICIENT_REGIME_TRADES.

    Accounting contract (unit-consistent):
    * every PnL observation is absolute USDT;
    * open positions are marked at the last scenario close and charged the
      declared entry taker fee;
    * observations are ordered by their own event timestamp (close event for a
      realized campaign, scenario cutoff for an open one);
    * drawdown is computed on that chronological equity path from the declared
      initial equity;
    * variance is computed on dimensionless campaign returns
      (PnL / initial equity), never on monetary PnL.

    Missing quantity/price/mark evidence blocks the regime; it never becomes a
    zero PnL or a percentage masquerading as USDT.
    """
    cfg = strategy_config or ExpertStrategyConfig(
        min_support_quorum=1,
        max_contradiction_tolerance=28,
        bracket_stop_pct=Decimal("0.02"),
        bracket_target_pct=Decimal("0.04"),
    )
    if initial_equity <= 0 or taker_fee < 0:
        raise ValueError("invalid G3 accounting contract")
    regime_results: dict[str, dict[str, float]] = {}
    drawdowns: list[float] = []
    return_variances: list[float] = []
    accounting_problems: list[str] = []

    for name, candles in regimes.items():
        if not candles:
            continue
        res = run_expert_strategy_backtest(tuple(candles), cfg)
        opened = [o for o in res.get("opened_positions", []) if isinstance(o, dict)]
        closed = [c for c in res.get("closed_positions", []) if isinstance(c, dict)]
        mark = float(candles[-1].close)
        cutoff_ns = int(candles[-1].end_ns)
        observations, problems = _g3_pair_regime(opened, closed)
        accounting_problems.extend(f"{name}:{p}" for p in problems)

        points: list[tuple[int, float, str]] = []
        for ts, pnl, _notional, kind, record in observations:
            if kind == "open":
                marked = _g3_open_mark_pnl(record, mark, taker_fee)
                if marked is None:
                    accounting_problems.append(
                        f"{name}:OPEN_MTM_UNMEASURED:{record.get('position_id', ts)}"
                    )
                    continue
                pnl, _notional = marked
                points.append((cutoff_ns, pnl, f"open:{ts}"))
            else:
                assert pnl is not None
                points.append((ts, pnl, f"closed:{ts}"))

        points.sort(key=lambda p: (p[0], p[2]))
        equity = float(initial_equity)
        peak = equity
        max_dd = 0.0
        returns: list[float] = []
        for _ts, pnl, _kind in points:
            equity += pnl
            returns.append(pnl / initial_equity)
            if equity > peak:
                peak = equity
            dd = (peak - equity) / peak if peak > 0 else 1.0
            max_dd = max(max_dd, dd)

        var_ret = float(np.var(returns)) if len(returns) > 1 else 0.0
        drawdowns.append(max_dd)
        return_variances.append(var_ret)
        regime_results[name] = {
            "bars": len(candles),
            "trades": len(opened),
            "observations": len(points),
            "max_drawdown": round(max_dd, 6),
            "return_variance": round(var_ret, 8),
            "realized_pnl_usdt": round(
                sum(p[1] for p in points if p[2].startswith("closed")), 6
            ),
            "open_mtm_usdt": round(
                sum(p[1] for p in points if p[2].startswith("open")), 6
            ),
            "final_equity_usdt": round(equity, 6),
        }

    max_observed_dd = max(drawdowns) if drawdowns else 0.0
    max_observed_var = max(return_variances) if return_variances else 0.0

    empty_regimes = sorted(
        n for n, r in regime_results.items() if r["trades"] < min_trades_per_regime
    )
    min_regime_trades = min((r["trades"] for r in regime_results.values()), default=0)
    accounting_measured = not accounting_problems

    passed = (
        len(regime_results) == 4
        and not empty_regimes
        and accounting_measured
        and max_observed_dd <= max_drawdown_limit
        and math.isfinite(max_observed_var)
        and max_observed_var <= 0.20
    )

    state = GateState.PASS if passed else GateState.BLOCKED
    if empty_regimes:
        reason = f"INSUFFICIENT_REGIME_TRADES: {empty_regimes} below {min_trades_per_regime}"
    elif not accounting_measured:
        reason = "ACCOUNTING_EVIDENCE_MISSING: " + "; ".join(sorted(set(accounting_problems)))
    elif not passed:
        reason = f"DRAWDOWN_OR_VARIANCE_BREACH: dd={max_observed_dd:.4f} var={max_observed_var:.6f}"
    else:
        reason = None
    metrics = {
        "regimes": regime_results,
        "max_observed_drawdown": round(max_observed_dd, 8),
        "max_observed_variance": round(max_observed_var, 8),
        "max_drawdown_limit": max_drawdown_limit,
        "min_trades_per_regime": min_trades_per_regime,
        "min_regime_trades": min_regime_trades,
        "empty_regimes": empty_regimes,
        "accounting_measured": accounting_measured,
        "accounting_problems": sorted(set(accounting_problems)),
        "pnl_unit": G3_PNL_UNIT,
        "return_unit": G3_RETURN_UNIT,
        "initial_equity_usdt": initial_equity,
        "taker_fee": taker_fee,
        "observation_basis": "chronological_campaign_close_or_cutoff_mark",
        "funding_included": False,
        "funding_evidence": "NOT_SUPPLIED_TO_G3",
        "reason": reason,
        "passed": passed,
    }
    return state, metrics


def evaluate_g4_synthetic_falsification(
    candles: Sequence[Candle],
    strategy_config: ExpertStrategyConfig | None = None,
    min_closed_trades: int = 2,
) -> tuple[GateState, dict[str, Any]]:
    """G4: Synthetic Falsification (g4_structural_robustness).

    Injects adversarial execution shocks into NautilusTrader:
    - Slippage shock: spread x3 + 100% prob slippage
    - Latency shock: 500ms base + 200ms insert latency jitter
    - Drop rate: 5% random order drop/unfill
    - Verifies bracket stop-loss protects capital and account does not bust.

    Survival without executions proves nothing: at least `min_closed_trades`
    shocked fills must close, else INSUFFICIENT_SHOCK_SAMPLE fails closed.
    """
    bracket_cfg = strategy_config or ExpertStrategyConfig(
        min_support_quorum=1,
        max_contradiction_tolerance=28,
        bracket_stop_pct=Decimal("0.02"),
        bracket_target_pct=Decimal("0.04"),
    )

    fill_model = ProbabilisticFillModel(prob_fill_on_limit=0.95, prob_slippage=1.0)
    latency_model = StaticLatencyModel(
        base_latency_nanos=500_000_000, insert_latency_nanos=200_000_000
    )
    shock_taker_fee = Decimal("0.0015")  # 3x standard fee

    eval_candles = tuple(candles[:200]) if len(candles) >= 200 else tuple(candles)

    backtest_result = run_expert_strategy_backtest(
        eval_candles,
        bracket_cfg,
        taker_fee=shock_taker_fee,
        fill_model=fill_model,
        latency_model=latency_model,
        initial_balance=Decimal("10000"),
    )

    account = backtest_result["account"]
    opened = backtest_result["opened_positions"]
    closed = backtest_result["closed_positions"]
    final_equity = extract_account_balance(account)
    if final_equity is None:
        return GateState.BLOCKED, {
            "final_equity": None,
            "total_trades": len(opened),
            "closed_trades": len(closed),
            "min_closed_trades": min_closed_trades,
            "reason": "ACCOUNT_BALANCE_UNAVAILABLE: no measured balance in account",
            "measurement": UNMEASURED,
            "shock_taker_fee": float(shock_taker_fee),
            "adversarial_models": [
                "ProbabilisticFill(drop=0.05, slip=1.0)",
                "StaticLatency(500ms+200ms)",
            ],
            "bracket_stop_loss_pct": float(bracket_cfg.bracket_stop_pct or Decimal("0.02")),
            "account_survived": False,
        }

    # Invariant: Account did not bust (equity remains > 70% initial), stop-losses functioned,
    # AND the shock battery actually executed (survival with no fills is vacuous).
    no_bust = final_equity >= 7000.0
    all_finite = math.isfinite(final_equity)
    enough_shock_sample = len(closed) >= min_closed_trades

    passed = no_bust and all_finite and enough_shock_sample
    state = GateState.PASS if passed else GateState.BLOCKED
    metrics = {
        "final_equity": round(final_equity, 2),
        "total_trades": len(opened),
        "closed_trades": len(closed),
        "min_closed_trades": min_closed_trades,
        "reason": None
        if passed
        else (
            f"INSUFFICIENT_SHOCK_SAMPLE: {len(closed)} closed < {min_closed_trades}"
            if not enough_shock_sample
            else f"SHOCK_BUST_OR_NONFINITE: equity={final_equity:.2f}"
        ),
        "shock_taker_fee": float(shock_taker_fee),
        "adversarial_models": [
            "ProbabilisticFill(drop=0.05, slip=1.0)",
            "StaticLatency(500ms+200ms)",
        ],
        "bracket_stop_loss_pct": float(bracket_cfg.bracket_stop_pct or Decimal("0.02")),
        "account_survived": no_bust,
    }
    return state, metrics


#: Declared unit of the G5 sample series (#407). Both sources -- the caller's own
#: track and the regime fallback -- are the dimensionless per-campaign return of
#: the shared accounting contract (NX02.R3). The estimator must never hold an
#: absolute USDT PnL series (the scoring path's quantity) at the same time.
G5_SERIES_UNIT = "dimensionless_campaign_return"
G5_SERIES_BASIS = "per-campaign realized PnL / entry notional (NX02 accounting contract)"

#: Registered proportional execution frictions, expressed in the declared series
#: unit: 1bp taker fee and 2bp adverse slippage per campaign.
G5_FEE_STRESS_RETURN = 0.0001
G5_SLIPPAGE_STRESS_RETURN = 0.0002


def _campaign_returns_from_run(
    candles: Sequence[Candle], cfg: ExpertStrategyConfig
) -> list[float]:
    """Per-campaign returns for one engine execution on ``candles`` (NX02.R3).

    Replaces the historical ``realized_pnl / 10000`` extraction, which emitted a
    capital fraction into a series that an absolute USDT PnL series could also
    occupy -- one estimator, two units (#407).
    """
    if not candles:
        return []
    result = run_expert_strategy_backtest(tuple(candles), cfg)
    accounting = campaign_accounting(
        result.get("opened_positions") or [],
        result.get("closed_positions") or [],
        bars=bars_from_candles(tuple(candles)),
        cutoff_ns=candles[-1].end_ns,
    )
    return list(accounting.campaign_returns)


def evaluate_g5_selection_control(
    campaign_return_series: Sequence[float],
    candles: Sequence[Candle] | None = None,
    *,
    plan: StatisticsPlan,
    allow_regime_fallback: bool = False,
    fallback_basis: str = "",
    fallback_tape: Path | str | None = None,
    selection_family: SelectionFamily | None = None,
) -> tuple[GateState, dict[str, Any]]:
    """G5: Selection Control (g5_statistical_credibility).

    Enforces Constitution Rule 12: Deflated Sharpe Ratio (DSR) and White's Reality Check (WRC).
    Zero synthetic formulas (no sine/cosine oscillators). Uses genuine empirical returns across
    registered parameter/execution variants.

    Selection family: a passing DSR/Bonferroni result on four stress transformations does not
    describe a search history. ``selection_family`` is the only route to a PASS: it names the
    members that were actually considered and the instant the family was registered, and its
    identity is derived from that content. Without it the gate returns ``BLOCKED`` with
    ``MISSING_PROVENANCE_BOUND_SELECTION_HISTORY`` and still publishes the diagnostics.

    The supplied series must be the ``G5_SERIES_UNIT`` quantity: dimensionless
    per-campaign returns from the shared accounting contract (NX02.R3). Both the
    own-track input and the regime fallback are produced in that one unit --
    absolute USDT PnL belongs to the scoring path and is never mixed in here
    (#407).

    NX07.R3/R4: block length, resampling count, seed and trial multiplicity come
    from an immutable pre-registered ``plan`` -- this function has no statistical
    constants of its own left to tune. The regime fallback is never silent: it
    runs only when the caller authorizes it *and* states a basis, and an
    underpowered own track that is not authorized returns ``UNKNOWN`` rather than
    a substituted series. The plan also declares, as data, which statistics are
    authority conditions and which are diagnostics; both are reported.
    """
    if not isinstance(plan, StatisticsPlan):
        raise TypeError("G5 requires a pinned StatisticsPlan, not loose parameters")
    if allow_regime_fallback and not fallback_basis.strip():
        raise ValueError("an authorized regime fallback requires a stated basis")
    if allow_regime_fallback and fallback_tape is None and candles is None:
        raise ValueError(
            "an authorized regime fallback requires an explicit same-run source "
            "(fallback_tape or candles); there is no default tape"
        )
    raw_series = [float(value) for value in campaign_return_series]
    own_sample_count = len(raw_series)
    G5_MIN_OWN_INTERVALS = 20
    sample_source = "own_track"

    # NX07.R3: no silent substitution. A short own track is either explicitly
    # authorized to use the regime series, or it is UNKNOWN -- never reported as
    # a measurement it is not. (4 is the DSR moment floor: below that the gate
    # reports BLOCKED further down, so this branch covers 4 <= n < 20.)
    G5_MOMENT_FLOOR_INTERVALS = 4
    if own_sample_count < G5_MIN_OWN_INTERVALS and not allow_regime_fallback:
        if own_sample_count < G5_MOMENT_FLOOR_INTERVALS:
            # fewer intervals than the DSR moments need: blocked, not scored
            return GateState.BLOCKED, {
                "error": (
                    "INSUFFICIENT_TRADE_INTERVALS: At least 4 return intervals "
                    "required for DSR moments"
                ),
                "samples": own_sample_count,
                "own_sample_count": own_sample_count,
                "sample_source": "own_track",
                "passed": False,
                "plan_id": plan.identity(),
                "series_unit": G5_SERIES_UNIT,
                "series_basis": G5_SERIES_BASIS,
            }
        return GateState.UNKNOWN, {
            "error": (
                f"OWN_TRACK_UNDERPOWERED: {own_sample_count} intervals < "
                f"{G5_MIN_OWN_INTERVALS} and the regime fallback was not authorized"
            ),
            "samples": own_sample_count,
            "own_sample_count": own_sample_count,
            "sample_source": "own_track",
            "passed": False,
            "series_unit": G5_SERIES_UNIT,
            "series_basis": G5_SERIES_BASIS,
            "plan_id": plan.identity(),
            "authority_conditions": list(plan.authority_conditions),
            "diagnostics": list(plan.diagnostics),
        }

    if own_sample_count < G5_MIN_OWN_INTERVALS:
        sample_source = "regime_fallback"
        cfg = ExpertStrategyConfig(
            min_support_quorum=1,
            max_contradiction_tolerance=28,
            bracket_stop_pct=Decimal("0.02"),
            bracket_target_pct=Decimal("0.04"),
        )
        extracted: list[float] = []
        # NX08.R4: the fallback reads the *run's own* declared tape (or its own
        # candles) -- never a default path that has nothing to do with this run.
        if fallback_tape is not None:
            regimes = classify_market_regimes(tape_path=fallback_tape, slice_length=500)
            for _name, c_list in regimes.items():
                extracted.extend(_campaign_returns_from_run(tuple(c_list), cfg))
            fallback_source = f"regime_regimes_of:{fallback_tape}"
        elif candles is not None and len(candles) >= 12:
            extracted.extend(_campaign_returns_from_run(tuple(candles), cfg))
            fallback_source = "own_run_candles"
        else:
            fallback_source = "NO_SOURCE"
        if len(extracted) >= 4:
            raw_series = extracted

    # Rule 12 fail-closed: if fewer than 4 intervals, DSR moment math is impossible
    if len(raw_series) < 4:
        return GateState.BLOCKED, {
            "error": "INSUFFICIENT_TRADE_INTERVALS: At least 4 return intervals required for DSR moments",
            "samples": len(raw_series),
            "passed": False,
            "series_unit": G5_SERIES_UNIT,
            "series_basis": G5_SERIES_BASIS,
        }

    r_champ = np.array(raw_series, dtype=float)
    t_steps = len(r_champ)

    # Construct genuine economic execution variants:
    # 1. Champion (original strategy net returns)
    # 2. Fee stressed: additional taker friction
    # 3. Conservative: conservative sizing (0.95x size)
    # 4. Slippage stressed: adverse execution markout
    r_fee = r_champ - G5_FEE_STRESS_RETURN
    r_cons = 0.95 * r_champ
    r_slip = r_champ - G5_SLIPPAGE_STRESS_RETURN

    losses = {
        "champion": tuple(
            IntervalLoss(i, i + 1, i + 1, -Decimal(str(round(float(r), 8))))
            for i, r in enumerate(r_champ)
        ),
        "fee_stressed": tuple(
            IntervalLoss(i, i + 1, i + 1, -Decimal(str(round(float(r), 8))))
            for i, r in enumerate(r_fee)
        ),
        "conservative": tuple(
            IntervalLoss(i, i + 1, i + 1, -Decimal(str(round(float(r), 8))))
            for i, r in enumerate(r_cons)
        ),
        "slippage_stressed": tuple(
            IntervalLoss(i, i + 1, i + 1, -Decimal(str(round(float(r), 8))))
            for i, r in enumerate(r_slip)
        ),
    }

    variants = tuple(losses.keys())
    num_trials = plan.multiplicity_trials
    # NX07.R2: adequacy counts non-overlapping blocks of the pinned length, never
    # the raw row count, and a series that cannot hold two independent blocks is
    # reported as underdetermined instead of being scored.
    sufficiency = sample_sufficiency(len(raw_series), plan.block_size)
    if sufficiency["independent_samples"] < sufficiency["min_independent_samples"]:
        return GateState.BLOCKED, {
            "error": (
                "UNDERDETERMINED_SAMPLING: "
                f"{sufficiency['independent_samples']} independent blocks of "
                f"{sufficiency['block_size']} intervals"
            ),
            "samples": len(raw_series),
            "own_sample_count": own_sample_count,
            "sample_source": sample_source,
            "sample_sufficiency": sufficiency,
            "passed": False,
            "plan_id": plan.identity(),
            "series_unit": G5_SERIES_UNIT,
            "series_basis": G5_SERIES_BASIS,
            "authority_conditions": list(plan.authority_conditions),
            "diagnostics": list(plan.diagnostics),
        }
    dsr_plan = DSRPlan(
        selected_variant="champion",
        registered_variants=variants,
        effective_independent_trials=plan.effective_independent_trials,
        independence_basis=plan.independence_basis,
    )

    try:
        dsr_result = deflated_sharpe_diagnostic(
            losses,
            plan=dsr_plan,
            frozen_ns=0,
            evaluation_end_ns=t_steps,
            decision_ns=t_steps + 1,
        )

        baseline = tuple(IntervalLoss(i, i + 1, i + 1, Decimal("0")) for i in range(t_steps))
        wrc_result = reality_check_diagnostic(
            baseline,
            {"champion": losses["champion"]},
            frozen_ns=0,
            evaluation_end_ns=t_steps,
            decision_ns=t_steps + 1,
            block_size=max(1, t_steps // plan.wrc_block_divisor),
            reps=plan.wrc_reps,
            seed=plan.wrc_seed,
        )

        dsr_conf = float(dsr_result["dsr_confidence"])
        # Fail closed: a missing champion entry must BLOCK, never fall back to a
        # hardcoded p-value (anti-fabrication: no synthetic statistical outputs).
        try:
            raw_p = float(dsr_result["multiple_testing"]["raw_pvalues"]["champion"])
            bonf_p = float(
                dsr_result["multiple_testing"]["adjustments"]["bonferroni"]["adjusted_pvalues"][
                    "champion"
                ]
            )
        except (KeyError, TypeError, ValueError) as e:
            return GateState.BLOCKED, {
                "error": f"MISSING_CHAMPION_PVALUE: DSR output lacks champion entry: {e}",
                "samples": t_steps,
                "passed": False,
                "series_unit": G5_SERIES_UNIT,
                "series_basis": G5_SERIES_BASIS,
            }
        wrc_p = float(wrc_result["p_value"])

        diagnostic_passed = dsr_conf >= 0.95 and bonf_p <= 0.05
        family_metrics: dict[str, Any] = {
            "selection_family": None,
            "selection_family_status": "MISSING_PROVENANCE_BOUND_SELECTION_HISTORY",
        }
        passed = False
        state = GateState.BLOCKED
        reason: str | None = "MISSING_PROVENANCE_BOUND_SELECTION_HISTORY"
        if selection_family is not None:
            family_metrics = {
                "selection_family": selection_family.as_dict(),
                "selection_family_status": "PROVENANCE_BOUND_FAMILY_DECLARED",
            }
            # A correction can only be as wide as the declared search. A family
            # narrower than the plan's multiplicity would under-correct, and one
            # wider than the sample cannot be represented, so both fail closed.
            if len(selection_family.members) != num_trials:
                family_metrics["selection_family_status"] = (
                    "FAMILY_SIZE_DISAGREES_WITH_PINNED_MULTIPLICITY"
                )
                reason = "FAMILY_SIZE_DISAGREES_WITH_PINNED_MULTIPLICITY"
            else:
                passed = diagnostic_passed
                state = GateState.PASS if passed else GateState.BLOCKED
                if passed:
                    reason = None
                else:
                    reason = (
                        f"SELECTION_CONTROL_BREACH: dsr_conf={dsr_conf:.4f} "
                        f"bonf_p={bonf_p:.6f} (source={sample_source}, own_n={own_sample_count})"
                    )
        metrics = {
            "selection_control_method_version": "G5_SELECTION_FAMILY_REQUIRED_V2",
            **family_metrics,
            "diagnostic_family": list(variants),
            "diagnostic_family_kind": "CHAMPION_AND_STRESS_TRANSFORMATIONS",
            "wrc_family": ["champion"],
            "stress_diagnostic_passed": diagnostic_passed,
            "dsr_confidence": round(dsr_conf, 6),
            "raw_pvalue": round(raw_p, 6),
            "adjusted_bonferroni_pvalue": round(bonf_p, 6),
            "wrc_pvalue": wrc_p,
            "wrc_reporting_version": wrc_result.get("reporting_version", "UNAVAILABLE"),
            "trials": num_trials,
            "plan_id": plan.identity(),
            "plan": plan.as_dict(),
            "authority_conditions": list(plan.authority_conditions),
            "diagnostics": list(plan.diagnostics),
            "sample_intervals": t_steps,
            "sample_sufficiency": sufficiency,
            "own_sample_count": own_sample_count,
            "sample_source": sample_source,
            "fallback_basis": fallback_basis if sample_source == "regime_fallback" else None,
            "fallback_source": fallback_source if sample_source == "regime_fallback" else None,
            "series_unit": G5_SERIES_UNIT,
            "series_basis": G5_SERIES_BASIS,
            "fee_stress_return": G5_FEE_STRESS_RETURN,
            "slippage_stress_return": G5_SLIPPAGE_STRESS_RETURN,
            "reason": reason,
            "passed": passed,
        }
        return state, metrics
    except Exception as e:
        return GateState.BLOCKED, {"error": str(e), "passed": False}


def evaluate_g6_frozen_oos(
    candles: Sequence[Candle],
    strategy_config: ExpertStrategyConfig | None = None,
    min_retention_ratio: float = 0.60,
    *,
    protected_window: WindowBinding | None = None,
) -> tuple[GateState, dict[str, Any]]:
    """G6: Frozen Out-of-Sample (OOS) Replication (g6_protected_oos).

    Splits dataset into In-Sample (IS: first 67% / 8 months) and Frozen OOS (last 33% / 4 months).
    Runs strategy with frozen parameters on OOS and proves it retains at least 60% of IS
    PROFIT (not balance ratio): retention = oos_profit / is_profit. An unprofitable IS
    cannot be replicated (BLOCKED: no edge to retain); an unprofitable OOS retains
    nothing (BLOCKED: negative retention). The blow-up guard (OOS balance >= 8000)
    stays as a separate survival floor.

    #406/#421: the verdict must name the window it was measured over, so the caller
    declares that window explicitly (``protected_window``) and the gate checks the
    declaration against the bars it was handed. With no declaration -- or with one
    that describes other bars -- the gate fails closed with a named reason and
    publishes the retention as ``unmeasured``, so the window a run happened to load
    (``load_tape_candles`` -> ``head(limit)``, i.e. file order) can never be
    published as a protected out-of-sample replicate.
    """
    cfg = strategy_config or ExpertStrategyConfig(
        min_support_quorum=1,
        max_contradiction_tolerance=28,
        bracket_stop_pct=Decimal("0.02"),
        bracket_target_pct=Decimal("0.04"),
    )
    bars = tuple(candles)
    n = len(bars)
    split_idx = int(n * (2.0 / 3.0))

    # NX08.R4: an in-sample window twice the length of the out-of-sample window is
    # not a replication comparison. Both sides are truncated to the same number of
    # bars (the IS tail adjacent to the OOS head, no overlap) and the equality is
    # reported, so a retention ratio can never come from unequal exposure.
    is_all = bars[:split_idx]
    oos_all = bars[split_idx:]
    if not is_all or not oos_all:
        return GateState.BLOCKED, {
            "is_bars": len(is_all),
            "oos_bars": len(oos_all),
            "reason": "INSUFFICIENT_PARTITION: one side of the frozen split is empty",
            "measurement": UNMEASURED,
            "protected_window": protected_window.as_dict() if protected_window else None,
            "retention_ratio": None,
            "passed": False,
        }
    window_bars = min(len(is_all), len(oos_all))

    # The partition geometry is a fact about the bars handed in, so it is reported
    # in every response; the retention numbers are not (#406/#421).
    partition: dict[str, Any] = {
        "is_bars": window_bars,
        "oos_bars": window_bars,
        "window_equality": "EQUAL_BARS",
        "comparison_window_bars": window_bars,
        "is_bars_total": len(is_all),
        "oos_bars_total": len(oos_all),
        "is_profit": None,
        "oos_profit": None,
        "retention_ratio": None,
    }
    evaluated_window = window_identity(bars)
    if protected_window is None:
        return GateState.BLOCKED, {
            **partition,
            "reason": (
                f"{PROTECTED_WINDOW_ABSENT}: no protected window was declared for the "
                f"{len(bars)} evaluated bars, so this gate has no declared "
                "out-of-sample window to publish a retention verdict over"
            ),
            "measurement": UNMEASURED,
            "protected_window": None,
            "evaluated_window": evaluated_window,
            "passed": False,
        }
    window_mismatch = protected_window.mismatch(bars)
    if window_mismatch is not None:
        return GateState.BLOCKED, {
            **partition,
            "reason": f"{PROTECTED_WINDOW_MISMATCH}: {window_mismatch}",
            "measurement": UNMEASURED,
            "protected_window": protected_window.as_dict(),
            "evaluated_window": evaluated_window,
            "passed": False,
        }

    is_candles = is_all[-window_bars:]
    oos_candles = oos_all[:window_bars]

    is_res = run_expert_strategy_backtest(is_candles, cfg)
    oos_res = run_expert_strategy_backtest(oos_candles, cfg)

    is_trades = len(is_res["opened_positions"])
    oos_trades = len(oos_res["opened_positions"])

    is_bars = len(is_candles)
    oos_bars = len(oos_candles)

    # Calculate normalized trade frequency & performance
    is_freq = is_trades / max(1, is_bars)
    oos_freq = oos_trades / max(1, oos_bars)

    freq_retention = oos_freq / is_freq if is_freq > 0 else 1.0
    account_is = is_res["account"]
    account_oos = oos_res["account"]

    is_balance = extract_account_balance(account_is)
    oos_balance = extract_account_balance(account_oos)
    if is_balance is None or oos_balance is None:
        return GateState.BLOCKED, {
            "is_bars": is_bars,
            "is_trades": is_trades,
            "is_final_balance": None,
            "oos_bars": oos_bars,
            "oos_trades": oos_trades,
            "oos_final_balance": None,
            "is_profit": None,
            "oos_profit": None,
            "window_equality": "EQUAL_BARS",
            "comparison_window_bars": window_bars,
            "is_bars_total": len(is_all),
            "oos_bars_total": len(oos_all),
            "retention_ratio": None,
            "measurement": UNMEASURED,
            "protected_window": protected_window.as_dict(),
            "evaluated_window": evaluated_window,
            "reason": "ACCOUNT_BALANCE_UNAVAILABLE: no measured balance in IS/OOS account",
            "passed": False,
        }

    is_profit = is_balance - 10000.0
    oos_profit = oos_balance - 10000.0
    if is_profit <= 0:
        return GateState.BLOCKED, {
            "is_bars": is_bars,
            "is_trades": is_trades,
            "is_final_balance": round(is_balance, 2),
            "oos_bars": oos_bars,
            "oos_trades": oos_trades,
            "oos_final_balance": round(oos_balance, 2),
            "is_profit": round(is_profit, 2),
            "oos_profit": round(oos_profit, 2),
            "window_equality": "EQUAL_BARS",
            "comparison_window_bars": window_bars,
            "is_bars_total": len(is_all),
            "oos_bars_total": len(oos_all),
            "retention_ratio": None,
            "measurement": MEASURED,
            "protected_window": protected_window.as_dict(),
            "evaluated_window": evaluated_window,
            "reason": f"NO_IS_EDGE_TO_RETAIN: is_profit={is_profit:.2f} <= 0",
            "passed": False,
        }
    retention_ratio = oos_profit / is_profit
    no_blowup = oos_balance >= 8000.0

    passed = retention_ratio >= min_retention_ratio and no_blowup
    state = GateState.PASS if passed else GateState.BLOCKED
    metrics = {
        "is_bars": is_bars,
        "is_trades": is_trades,
        "is_final_balance": round(is_balance, 2),
        "oos_bars": oos_bars,
        "oos_trades": oos_trades,
        "oos_final_balance": round(oos_balance, 2),
        "is_profit": round(is_profit, 2),
        "oos_profit": round(oos_profit, 2),
        "retention_ratio": round(retention_ratio, 4),
        "trade_freq_retention": round(freq_retention, 4),
        "window_equality": "EQUAL_BARS",
        "comparison_window_bars": window_bars,
        "is_bars_total": len(is_all),
        "oos_bars_total": len(oos_all),
        "measurement": MEASURED,
        "protected_window": protected_window.as_dict(),
        "evaluated_window": evaluated_window,
        "reason": None
        if passed
        else (
            f"OOS_BLOWUP: balance={oos_balance:.2f}"
            if not no_blowup
            else f"PROFIT_RETENTION_BREACH: {retention_ratio:.4f} < {min_retention_ratio}"
        ),
        "passed": passed,
    }
    return state, metrics


def evaluate_g7_prospective_shadow(
    candles: Sequence[Candle],
    strategy_config: ExpertStrategyConfig | None = None,
    output_dir: Path | None = None,
    *,
    shadow_stream: Sequence[Candle] | None = None,
    shadow_origin: str = "",
    shadow_window: WindowBinding | None = None,
    provenance_verified: bool = False,
    min_exposure_bars: int = 30,
    min_decision_bars: int = 30,
    evidence_threshold: float | None = None,
    taker_fee: float = 0.0005,
) -> tuple[GateState, dict[str, Any]]:
    """G7: Prospective Shadow Succession (g7_generalization).

    Evaluates sequential prospective streaming observations using an e-process
    martingale and drift monitoring, and saves the trajectory to disk.

    NX08.R4: the gate no longer mints a prospective state from the run's own
    historical tail. A ``shadow_stream`` must be declared explicitly and its origin
    named; the gate records that the stream is caller-declared and not independently
    verified. Without a declared stream the state is UNKNOWN with a named reason --
    never PASS -- because "the last 100 bars of the same run" is not prospective
    evidence. A declared stream shorter than 100 bars still fails closed with
    INSUFFICIENT_SHADOW_WINDOW.

    #406/#421: a declared stream must also carry a declared *window*
    (``shadow_window``) that is forward of the scored window, and the verdict is a
    function of that window and of the candidate only:

    * the e-process is updated with the candidate's own admitted direction per bar
      (its measured forward behaviour), never with the price path alone, so two
      candidates with different behaviour cannot share one metric;
    * the drift is the window's maximum cumulative excursion from its opening
      close, not the last bar's |return|;
    * the stream is put in canonical (start_ns, end_ns, hash) order first, so the
      verdict is a function of the bar set and not of the caller's ordering.

    An in-sample slice ``candles[-100:]`` therefore cannot be published as
    prospective generalization: it overlaps the scored window and fails closed with
    a named reason.
    """
    out_dir = output_dir or Path("artifacts/benchmarks")
    out_dir.mkdir(parents=True, exist_ok=True)
    shadow_log = out_dir / "g7_prospective_shadow.jsonl"
    scored_window = window_identity(candles)

    if shadow_stream is None:
        return GateState.UNKNOWN, {
            "reason": "PSEUDO_PROSPECTIVE_HISTORICAL_WINDOW_NOT_ACCEPTED",
            "detail": (
                "no shadow_stream was declared: the run's own historical tail is not "
                "prospective evidence, so G7 stays UNKNOWN instead of passing"
            ),
            "historical_bars_available": len(candles),
            "shadow_origin": shadow_origin or None,
            "provenance_status": "NO_DECLARED_STREAM",
            "shadow_window": None,
            "scored_window": scored_window,
            "measurement": UNMEASURED,
            "passed": False,
        }
    if not shadow_origin.strip():
        raise ValueError("a declared shadow stream requires a named origin")

    if len(shadow_stream) < 100:
        return GateState.BLOCKED, {
            "window_bars": len(shadow_stream),
            "required_window_bars": 100,
            "reason": f"INSUFFICIENT_SHADOW_WINDOW: {len(shadow_stream)} < 100",
            "shadow_origin": shadow_origin,
            "provenance_status": "CALLER_DECLARED_NOT_VERIFIED_BY_GATE",
            "shadow_window": shadow_window.as_dict() if shadow_window else None,
            "scored_window": scored_window,
            "measurement": UNMEASURED,
            "passed": False,
        }

    if shadow_window is None:
        return GateState.BLOCKED, {
            "window_bars": len(shadow_stream),
            "reason": (
                f"{PROSPECTIVE_WINDOW_ABSENT}: the declared shadow stream carries "
                f"{len(shadow_stream)} bars but no declared window, so this gate cannot "
                "name the window it would publish a generalization verdict over"
            ),
            "shadow_origin": shadow_origin,
            "provenance_status": "NO_DECLARED_WINDOW",
            "shadow_window": None,
            "scored_window": scored_window,
            "measurement": UNMEASURED,
            "passed": False,
        }

    stream_window_mismatch = shadow_window.mismatch(shadow_stream)
    if stream_window_mismatch is not None:
        return GateState.BLOCKED, {
            "window_bars": len(shadow_stream),
            "reason": f"{PROSPECTIVE_WINDOW_MISMATCH}: {stream_window_mismatch}",
            "shadow_origin": shadow_origin,
            "provenance_status": "CALLER_DECLARED_NOT_VERIFIED_BY_GATE",
            "shadow_window": shadow_window.as_dict(),
            "scored_window": scored_window,
            "measurement": UNMEASURED,
            "passed": False,
        }

    scored_end_ns = scored_window["end_ns"] if scored_window is not None else None
    if scored_end_ns is not None and int(shadow_window.start_ns) < int(scored_end_ns):
        return GateState.BLOCKED, {
            "window_bars": len(shadow_stream),
            "reason": (
                f"{PROSPECTIVE_WINDOW_NOT_FORWARD}: the declared shadow window starts "
                f"at {int(shadow_window.start_ns)} but the scored window ends at "
                f"{int(scored_end_ns)}; bars the candidate was evaluated on are not "
                "prospective evidence"
            ),
            "shadow_origin": shadow_origin,
            "provenance_status": "CALLER_DECLARED_NOT_VERIFIED_BY_GATE",
            "shadow_window": shadow_window.as_dict(),
            "scored_window": scored_window,
            "measurement": UNMEASURED,
            "passed": False,
        }

    # Canonical order: the verdict is a function of the bar set, not of the order
    # the caller happened to hand it in.
    stream_candles = tuple(
        sorted(
            tuple(shadow_stream),
            key=lambda c: (int(c.start_ns), int(c.end_ns), c.instrument_id, c.source_hash),
        )
    )
    cfg = strategy_config or ExpertStrategyConfig(
        min_support_quorum=1,
        max_contradiction_tolerance=28,
        bracket_stop_pct=Decimal("0.02"),
        bracket_target_pct=Decimal("0.04"),
    )

    # Run Nautilus strategy in forward streaming / paper mode
    res = run_expert_strategy_backtest(stream_candles, cfg)
    decisions = res["decisions"]

    # Initialize e-process martingale E_t = prod_{k=1}^t (1 + lambda_k * (X_k - mu_0))
    e_process_val = 1.0
    # Unclipped twin judged by the gate. The logged trajectory stays clipped
    # for display stability, but judging the clipped value against bounds
    # inside the clip range is a tautology (clip [0.01, 19.0] vs pass
    # [0.01, 20.0) can never fail). The gate must see the real process.
    e_process_raw = 1.0
    # The drift judged by the gate is a property of the whole window: the largest
    # cumulative excursion from the window's opening close. The last bar's
    # |return| is a one-bar fact and cannot describe a drift over 100 bars.
    drift_val = 0.0
    anchor = float(stream_candles[0].close) if stream_candles else 100.0
    candidate_exposure_bars = 0
    previous_exposure = 0
    net_return = 0.0
    candidate_decision_bars = 0

    trajectory: list[dict[str, Any]] = []

    for i, c in enumerate(stream_candles):
        px = float(c.close)
        previous = float(stream_candles[i - 1].close) if i else anchor
        step_return = (px - previous) / previous if previous > 0 else 0.0
        if anchor > 0:
            drift_val = max(drift_val, abs((px - anchor) / anchor))

        # Causal alignment: the decision at bar i-1 is the one actionable for
        # bar i's return. Using decisions[i] would credit the candidate with the
        # return of the very bar whose close formed its signal (look-ahead).
        dec_info = decisions[i - 1] if i > 0 and i - 1 < len(decisions) else {}
        action = dec_info.get("action", "NO_ACTION")
        # The candidate's own measured forward behaviour on this bar: the direction
        # of the opportunity its consensus actually admitted. A candidate that
        # admits nothing carries zero exposure and cannot move the e-process.
        opportunity = dec_info.get("opportunity") or {}
        direction = str(opportunity.get("direction") or "").upper()
        exposure = 0
        if dec_info.get("consensus_supported") and direction in ("LONG", "SHORT"):
            exposure = 1 if direction == "LONG" else -1
        if exposure != 0:
            candidate_exposure_bars += 1
        if action not in (None, "NO_ACTION"):
            candidate_decision_bars += 1
        # Net-of-cost candidate return for this bar. A change of exposure is the
        # only traded event, so the declared taker fee is charged on turnover;
        # holding an unchanged position is not charged again.
        turnover = abs(exposure - previous_exposure)
        net_step = exposure * step_return - taker_fee * turnover
        net_return += net_step
        previous_exposure = exposure

        betting_lambda = 0.1
        update = 1.0 + betting_lambda * np.tanh(exposure * step_return * 10.0)
        e_process_raw = e_process_raw * update
        if not math.isfinite(e_process_raw):
            # Exploded/vanished wealth is a verdict, not a crash: pin it and
            # keep the window (drift is still the window's cumulative excursion).
            e_process_raw = float("inf") if e_process_raw > 0 else 0.0
        e_process_val = float(np.clip(e_process_val * update, 0.01, 19.0))

        step_record = {
            "step": i,
            "timestamp_ns": c.end_ns,
            "e_process": round(e_process_val, 6),
            "drift": round(drift_val, 6),
            "exposure": exposure,
            "action": action,
            "close": px,
        }
        trajectory.append(step_record)

    with open(shadow_log, "w", encoding="utf-8") as f:
        for r in trajectory:
            f.write(json.dumps(r) + "\n")

    # Evidentiary decision rule. A PASS is one-sided evidence *for* the
    # candidate, not merely an e-process inside a wide band:
    #   1. the stream is a declared, forward, bound window (checked above);
    #   2. the caller supplies independently verified provenance;
    #   3. the window contains enough decisions and candidate exposure;
    #   4. net-of-cost candidate return is positive;
    #   5. the e-process reaches a one-sided 20:1 screening threshold;
    #   6. the window drift stays inside its declared guard.
    # The threshold is a screening evidence bar, not economic authority; G9 and
    # the capability score still gate any claim. Zero exposure and adverse
    # evidence cannot satisfy (3)-(5), so absence of evidence is never a PASS.
    if not decisions:
        evidence_state = "NO_DECISIONS"
    elif candidate_exposure_bars == 0:
        evidence_state = "ZERO_EXPOSURE"
    elif len(trajectory) < 100:
        evidence_state = "INSUFFICIENT_OBSERVATIONS"
    elif candidate_exposure_bars < min_exposure_bars:
        evidence_state = "INSUFFICIENT_EXPOSURE"
    elif candidate_decision_bars < min_decision_bars:
        evidence_state = "INSUFFICIENT_DECISIONS"
    elif not provenance_verified:
        evidence_state = "UNVERIFIED_PROVENANCE"
    elif net_return <= 0.0:
        evidence_state = "EVIDENCE_AGAINST_CANDIDATE"
    elif evidence_threshold is None:
        # No pre-registered evidence threshold exists in the contract. Choosing
        # one after seeing the window would be threshold selection, so the gate
        # publishes the diagnostic and fails closed.
        evidence_state = "STATISTICAL_EVIDENCE_RULE_NOT_PREREGISTERED"
    elif not math.isfinite(e_process_raw) or e_process_raw < evidence_threshold:
        evidence_state = "STATISTICAL_EVIDENCE_INSUFFICIENT"
    elif drift_val >= 0.15:
        evidence_state = "DRIFT_EXCEEDED"
    else:
        evidence_state = "VALIDATED_PROSPECTIVE_EVIDENCE"
    passed = evidence_state == "VALIDATED_PROSPECTIVE_EVIDENCE"
    state = GateState.PASS if passed else GateState.BLOCKED
    metrics = {
        "final_e_process": round(e_process_val, 4),
        "final_e_process_raw": (
            round(e_process_raw, 4) if math.isfinite(e_process_raw) else str(e_process_raw)
        ),
        "final_drift": round(drift_val, 6),
        "drift_basis": "max_absolute_cumulative_deviation_from_window_open",
        "exposure_basis": "candidate_admitted_opportunity_direction_per_bar",
        "candidate_exposure_bars": candidate_exposure_bars,
        "candidate_decision_bars": candidate_decision_bars,
        "min_exposure_bars": min_exposure_bars,
        "min_decision_bars": min_decision_bars,
        "evidence_threshold": evidence_threshold,
        "net_return": round(net_return, 10),
        "taker_fee": taker_fee,
        "evidence_state": evidence_state,
        "provenance_status": "CALLER_DECLARED_NOT_VERIFIED_BY_GATE",
        "provenance_verified": bool(provenance_verified),
        "shadow_steps": len(trajectory),
        "log_path": str(shadow_log),
        "shadow_origin": shadow_origin,
        "shadow_window": shadow_window.as_dict(),
        "scored_window": scored_window,
        "measurement": MEASURED,
        "passed": passed,
        "reason": None if passed else evidence_state,
    }
    return state, metrics


def evaluate_g8_live_realization(
    live_fills_path: Path | str | None = None,
    *,
    source: str = "live",
    account: dict[str, Any] | None = None,
    provenance: str = "public_paper",
    account_id: str = "",
    attestation_path: Path | str | None = None,
) -> tuple[GateState, dict[str, Any]]:
    """G8: Live Realization (g8_prospective_shadow).

    Real venue-settled fills vs simulated account reconciliation.
    Fixture files are NEVER counted as live (FIXTURE_NOT_LIVE).
    Absent venue/account => UNRUN_NO_VENUE_ACCOUNT (NOT_APPLICABLE diagnostic fold).
    Documented format/source/command in every branch per SHADOW_LIVE_DATA_SPEC.md.

    NX10.R4: a well-formed file is public paper until an authenticated venue
    statement says otherwise, and public paper is technical evidence -- it is
    NOT_APPLICABLE for live realization, never PASS. Only an authenticated
    provenance with an account identity can reach PASS.
    """
    from v8_next.adapters.shadow_ingest import (
        is_fixture_path,
        load_shadow_fills,
        reconcile_shadow_account,
    )

    spec_ref = "docs/contracts/SHADOW_LIVE_DATA_SPEC.md"
    base_doc = {
        "format": "jsonl per SHADOW_LIVE_DATA_SPEC.md: {fill_id,instrument,price,qty,side,venue_time_ns,order_id}",
        "source": "venue private REST GET /fapi/v1/userTrades (requires BINANCE_API_KEY)",
        "command": "uv run --project v8-next python -m v8_next.adapters.shadow_ingest verify --fills artifacts/shadow_fills.jsonl",
        "spec": spec_ref,
    }

    # Fixture guard: never count fixture as live, even if well-formed
    if live_fills_path is not None and (
        source == "fixture" or is_fixture_path(Path(live_fills_path))
    ):
        return GateState.BLOCKED, {
            **base_doc,
            "mode": "FIXTURE_NOT_LIVE",
            "reason": f"fixture path not counted as live: {live_fills_path}",
            "fixture_guard": True,
            "clause": "fixture never substitutes for venue-settled fills",
        }

    if live_fills_path is not None:
        attestation = None
        if attestation_path is not None:
            try:
                attestation = json.loads(Path(attestation_path).read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as error:
                return GateState.BLOCKED, {
                    **base_doc,
                    "mode": "UNVERIFIED_PROVENANCE",
                    "reason": f"ATTESTATION_UNREADABLE:{type(error).__name__}",
                    "passed": False,
                }
        fills, meta = load_shadow_fills(
            live_fills_path,
            source=source,
            provenance=provenance,
            account_id=account_id,
            attestation=attestation,
        )
        mode = meta.get("mode")
        if mode == "FIXTURE_NOT_LIVE":
            return GateState.BLOCKED, {**base_doc, **meta}
        if mode == "PUBLIC_PAPER_NOT_SETTLED":
            return GateState.NOT_APPLICABLE, {
                **base_doc,
                **meta,
                "mode": "PUBLIC_PAPER_NOT_SETTLED",
                "reason": (
                    "PUBLIC_PAPER_TECHNICAL_EVIDENCE_ONLY: a capture through an "
                    "unauthenticated endpoint is not a venue settlement, so live "
                    "realization is NOT_APPLICABLE rather than PASS (NX10.R4)"
                ),
                "authority": "NONE",
                "next_step": (
                    "an authenticated venue statement requires a trusted-ingestion "
                    "attestation receipt bound to the fill file hash and account; a "
                    "caller label alone never mints one"
                ),
            }
        if mode == "UNVERIFIED_PROVENANCE":
            return GateState.BLOCKED, {
                **base_doc,
                **meta,
                "mode": "UNVERIFIED_PROVENANCE",
                "reason": (
                    "UNVERIFIED_PROVENANCE: "
                    f"{meta.get('reason', 'attestation not verified')}; a caller-supplied "
                    "provenance string, filename, checksum or account id is not venue "
                    "authenticity"
                ),
                "authority": "NONE",
                "passed": False,
            }
        if mode == "LIVE_VENUE_SETTLED":
            # A settlement PASS needs BOTH an independently verified attestation
            # (enforced by the loader) AND a successful account reconciliation.
            if account is None:
                return GateState.BLOCKED, {
                    **base_doc,
                    **meta,
                    "mode": "RECONCILIATION_MISSING",
                    "reason": "RECONCILIATION_MISSING: no account snapshot supplied",
                    "account_reconciled": False,
                    "passed": False,
                }
            if account.get("account_id") is not None and str(
                account.get("account_id")
            ) != account_id:
                return GateState.BLOCKED, {
                    **base_doc,
                    **meta,
                    "mode": "ACCOUNT_MISMATCH",
                    "reason": "ACCOUNT_MISMATCH: snapshot account id differs from attestation",
                    "account_reconciled": False,
                    "passed": False,
                }
            recon = reconcile_shadow_account(fills, account)
            out: dict[str, Any] = {**base_doc, **meta, "mode": "LIVE_VENUE_SETTLED"}
            out["reconciliation"] = recon
            out["account_reconciliation"] = recon  # alias for spec
            out["account_reconciled"] = bool(recon.get("reconciled"))
            if not recon.get("reconciled"):
                return GateState.BLOCKED, {
                    **out,
                    "mode": "RECONCILIATION_FAILED",
                    "reason": (
                        "RECONCILIATION_FAILED: "
                        + (",".join(recon.get("problems", [])) or "unknown mismatch")
                    ),
                    "passed": False,
                }
            out["passed"] = True
            return GateState.PASS, out
        if mode in ("UNRUN_NO_VENUE_ACCOUNT", "MALFORMED_SHADOW_FILE"):
            # Malformed or absent but path was given => BLOCKED (explicit claim attempted)
            state = (
                GateState.BLOCKED if mode == "MALFORMED_SHADOW_FILE" else GateState.NOT_APPLICABLE
            )
            return state, {**base_doc, **meta}

    # No path supplied: research / candidate phase => diagnostic fold.
    # No ambient probe: a stray file in the working directory must never
    # promote a gate. Explicit --live-fills is the only PASS route.
    return GateState.NOT_APPLICABLE, {
        **base_doc,
        "mode": "DIAGNOSTIC_FOLD",
        "status": "UNRUN_NO_VENUE_ACCOUNT",
        "reason": "no venue account / no file at artifacts/shadow_fills.jsonl",
        "expected_command": "uv run --project v8-next python -m v8_next.adapters.shadow_ingest fetch --symbol BTCUSDT --out artifacts/shadow_fills.jsonl  # requires BINANCE_API_KEY",
        "clause": "D-152 §5 (Research / Candidate fold unblocks pipeline without claiming live fills)",
        "funding_note": "quad funding is public in research/tape/quad-1h-12m/tape.jsonl; see funding_history.quad_funding_summary",
        "unrun_detail": "UNRUN_NO_VENUE_ACCOUNT",
    }


def evaluate_g9_certificate_authority(
    ledger: BenchmarkLedger,
    receipt_digest: str,
    gates: GateVector,
    capability_score: float | None,
    output_dir: Path | None = None,
    live_realization_verified: bool = False,
) -> tuple[GateState, dict[str, Any], StatutoryClaimRecord | None]:
    """G9: Certificate Authority (g9_live_realization).

    Enforces Central ClaimRegistry verification:
    1. Validates BenchmarkLedger cryptographic hash chain.
    2. Verifies that G0-G7 hold.
    3. Mints signed StatutoryClaimRecord and registers it in central ledger.
    4. Elevates verdict to READY_NOT_CLAIMED (or SUPPORTED_EDGE if live fills certified).
    """
    out_dir = output_dir or Path("artifacts/benchmarks")
    registry = ClaimRegistry(registry_dir=out_dir)

    success, msg, claim_record = registry.verify_ledger_and_issue_claim(
        ledger=ledger,
        receipt_digest=receipt_digest,
        gates=gates,
        capability_score=capability_score,
        live_realization_verified=live_realization_verified,
    )

    if success and claim_record is not None:
        return (
            GateState.PASS,
            {
                "claim_id": claim_record.claim_id,
                "claim_class": claim_record.claim_class.value,
                "allowed_header": claim_record.allowed_rendering_header,
                "signature_verified": claim_record.verify_signature(),
                "message": msg,
            },
            claim_record,
        )

    return GateState.BLOCKED, {"error": msg}, None


# --------------------------------------------------------------------------- #
# #447 — the structural trio G0/G1/G2 is resolved from measurement, never declared
# --------------------------------------------------------------------------- #

#: Named reason this path publishes when the ledger it was handed carries no entry
#: to verify. An empty ledger is not a verified ledger: absent evidence is named,
#: never read as a PASS.
LEDGER_EMPTY_NO_ENTRIES = "LEDGER_EMPTY_NO_ENTRIES_TO_VERIFY"
#: Named reason prefix for a ledger whose own verification did not hold.
LEDGER_VERIFICATION_FAILED = "LEDGER_VERIFICATION_FAILED"
#: Named reason when a verification report says ``OK`` while a component that verdict
#: is derived from is broken. Publishing ``<verdict>:OK`` would name the failing
#: component as fine (#480), so the disagreement is published as its own name.
LEDGER_VERDICT_INCONSISTENT = "LEDGER_VERDICT_INCONSISTENT"
#: The claim-layer verdict ``verify_report()`` publishes when a *live* score-binding
#: refusal exists (#480 task 2). It is not a byte-history class: routing it through
#: ``LEDGER_VERIFICATION_FAILED:<component>`` would name no component and would make
#: the named live-defect branch below unreachable, so this path reads it out and lets
#: the refusal speak for itself, by component and sequence.
SCORE_BINDING_INVALID = "SCORE_BINDING_INVALID"
#: Named reason G2 carries when the ledger verified but no engine rerun was compared:
#: ledger conservation and engine determinism are two different measurements.
DETERMINISM_RERUN_NOT_PERFORMED = "DETERMINISM_RERUN_NOT_PERFORMED"
#: Named reason G1 carries on this path: no per-bar decision-vs-availability audit
#: is performed here, so the causal-PIT input is unmeasured by construction.
CAUSAL_PIT_NOT_MEASURED = "CAUSAL_PIT_NOT_MEASURED_ON_THIS_PATH"


@dataclass(frozen=True)
class StructuralGateInputs:
    """The measured inputs the canonical scoring rule turns into G0/G1/G2 (#447)."""

    has_continuous_lineage: bool | None
    is_causal_pit: bool | None
    g2_state: GateState


@dataclass(frozen=True)
class StructuralGateResolution:
    """G0/G1/G2 as measured, with the named reason each state carries (#447).

    ``gates`` is the canonical scoring rule's own output on ``inputs``; this battery
    hands the same ``inputs`` to the rule for every vector it publishes, so the
    structural cells of those vectors are this derivation by construction.
    """

    inputs: StructuralGateInputs
    gates: GateVector
    metrics: dict[str, dict[str, Any]]

    def state(self, vector_field: str) -> GateState:
        """The resolved state of one of the three structural cells."""
        if vector_field not in ("g0_identity", "g1_causal_pit", "g2_determinism_ledger"):
            raise ValueError(f"{vector_field!r} is not a structural gate cell")
        return cast(GateState, getattr(self.gates, vector_field))


def _entry_declares_score_evidence(receipt: Any) -> bool:
    """Treat unknown canon versions as live defects rather than exempt history."""
    canon = RECEIPT_CANON_TABLE.get(receipt.digest_version)
    return canon is None or canon.score_evidence == "always_present"


def _score_binding_defects(
    ledger: BenchmarkLedger, report: Any
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Separate actionable score-binding defects from immutable legacy history."""
    live: list[dict[str, Any]] = []
    historical: list[dict[str, Any]] = []
    for entry, verified in zip(ledger.entries, report.entries, strict=True):
        if verified.score_binding_valid:
            continue
        component = verified.score_binding.split(":", 1)[0]
        defect = {
            "sequence": verified.sequence_number,
            "component": component,
            "reason": f"{component}:{verified.sequence_number}",
            "detail": verified.score_binding_detail,
            "digest_version": entry.receipt.digest_version,
            "live": _entry_declares_score_evidence(entry.receipt),
        }
        (live if defect["live"] else historical).append(defect)
    return live, historical


def measure_ledger_parity(ledger: BenchmarkLedger) -> tuple[GateState, dict[str, Any]]:
    """G2's measured input on this path: the ledger this battery was handed, verified.

    #447: the G2 cell of the readiness vector was a literal ``PASS``; here it is a
    state derived from a measurement that can actually be taken on the bars and the
    ledger this battery receives:

    * **no entries** -> ``UNKNOWN`` with ``LEDGER_EMPTY_NO_ENTRIES_TO_VERIFY``. An
      empty ledger verifies vacuously, and a vacuous verification is not evidence.
    * **byte history broken** (chain / digest / bound artifacts) -> ``BLOCKED`` with
      ``LEDGER_VERIFICATION_FAILED:<component>``. This is a measured failure, not an
      absence of measurement, and the component named is a failure -- never ``OK``.
    * **a live score-binding defect** -> ``BLOCKED`` with the refusal named by
      component and sequence, e.g. ``CAPABILITY_SCORE_UNBOUND_TO_EVIDENCE:<seq>`` or
      ``CAPABILITY_SCORE_CONTRADICTS_BOUND_EVIDENCE:<seq>``.
    * **only history remains** (score-binding refusals on entries whose own declared
      version predates ``score_evidence``) -> ``UNKNOWN`` with
      ``DETERMINISM_RERUN_NOT_PERFORMED``.

    #480: the third and fourth cases used to collapse into one. ``verify_report()``
    keeps the byte verdict and the score-binding verdict apart on purpose (a number
    that does not follow from its evidence is a claim defect, not a byte-history
    defect), so the ledger's ``overall`` was ``OK`` while the score bindings were not
    -- and this function published ``LEDGER_VERIFICATION_FAILED:OK``, naming the
    failing component as fine and pinning G2 to BLOCKED forever, because the
    pre-``v7`` entries it refused can never be retro-bound under an append-only
    ledger. Both halves are reported here: a byte failure blocks and names itself; a
    live claim defect blocks and names its component and sequence; a claim defect on
    an entry whose own version never carried the evidence stays named in the metrics
    as history and does not block. A verified ledger still proves only the ledger half
    of ``g2_determinism_ledger``; engine rerun parity is a different measurement (the
    runner's ``measure_determinism``) which this path does not perform, so the history
    case is not read as a PASS either.
    """
    entries = ledger.entries
    if not entries:
        return GateState.UNKNOWN, {
            "status": "UNRUN",
            "reason": LEDGER_EMPTY_NO_ENTRIES,
            "entries": 0,
            "note": "an empty ledger evidences no ledger parity; UNKNOWN, never PASS",
        }
    report = ledger.verify_report()
    live_defects, historical_defects = _score_binding_defects(ledger, report)
    summary = {
        "entries": len(entries),
        "ledger_overall": report.overall,
        "chain_valid": report.chain_valid,
        "digests_valid": report.digests_valid,
        "artifacts_intact": report.artifacts_intact,
        "score_bindings_valid": report.score_bindings_valid,
        "score_binding": report.score_binding,
        "live_score_binding_defects": live_defects,
        "historical_score_binding_defects": historical_defects,
    }

    # 1. The byte history is adjudicated first, and only a named failure is published.
    #    ``report.overall`` carries two kinds of verdict: the byte-history classes and,
    #    since #480 task 2, the claim-layer ``SCORE_BINDING_INVALID``. Only the byte
    #    classes belong in a ``LEDGER_VERIFICATION_FAILED:<component>`` reason -- letting
    #    the claim verdict through here published ``...:SCORE_BINDING_INVALID`` (naming
    #    no entry) and made the named live-defect branch below unreachable, collapsing
    #    again the very distinction the two halves of #480 added. A report that
    #    contradicts its own components is published as that disagreement, never ``:OK``.
    byte_components_ok = (
        report.chain_valid and report.digests_valid and report.artifacts_intact
    )
    bytes_verdict = report.overall
    if bytes_verdict == SCORE_BINDING_INVALID:
        bytes_verdict = (
            LEDGER_VERDICT_INCONSISTENT
            if (not byte_components_ok or report.score_bindings_valid)
            else "OK"
        )
    elif bytes_verdict == "OK" and not byte_components_ok:
        bytes_verdict = LEDGER_VERDICT_INCONSISTENT
    if bytes_verdict != "OK":
        return GateState.BLOCKED, {
            "status": "FAIL",
            "reason": f"{LEDGER_VERIFICATION_FAILED}:{bytes_verdict}",
            **summary,
        }

    # 2. A live claim defect is a measured failure of THIS ledger: block, named.
    if live_defects:
        first = live_defects[0]
        return GateState.BLOCKED, {
            "status": "FAIL",
            "reason": first["reason"],
            "detail": first["detail"],
            "blocked_component": first["component"],
            "blocked_sequence": first["sequence"],
            **summary,
        }

    # 3. Only refusals this path cannot repair remain: the entries predate the field
    #    that would bind them (D-166 retired seq 20 for the same shape). They stay named
    #    in the metrics and never resolve to PASS.
    if historical_defects:
        return GateState.UNKNOWN, {
            "status": "UNRUN",
            "reason": DETERMINISM_RERUN_NOT_PERFORMED,
            **summary,
            "note": (
                "the byte history verified; every remaining score-binding refusal is on "
                "an entry whose own declared digest version predates score_evidence, so "
                "it cannot be bound without rewriting append-only bytes and is history, "
                "not a live defect; no engine rerun was compared either, so this gate "
                "stays unmeasured; UNKNOWN, never PASS"
            ),
        }
    return GateState.UNKNOWN, {
        "status": "UNRUN",
        "reason": DETERMINISM_RERUN_NOT_PERFORMED,
        **summary,
        "note": (
            "the ledger verified; no engine rerun was compared, so the determinism "
            "half of this gate stays unmeasured; UNKNOWN, never PASS"
        ),
    }


def resolve_structural_gates(
    candles: Sequence[Candle], ledger: BenchmarkLedger
) -> StructuralGateResolution:
    """Resolve G0/G1/G2 from what was measured, through the canonical scoring rule.

    #447: the battery used to publish these three cells as literals, so a published
    state stood without a measurement behind it and the registry entry feeding that
    number named no resolver in the tree. Every state here is a measurement or a
    named absence:

    * **G0** — the runner's own candle-lineage measurement
      (:func:`v8_next.evaluation.runner.measure_candle_lineage`), reused rather than
      re-implemented. No candles -> ``None`` -> ``UNKNOWN``, never PASS.
    * **G1** — no per-bar decision-vs-availability audit exists on this path, so the
      input is an explicit ``None``; the rule resolves it to ``UNKNOWN`` with
      ``CAUSAL_PIT_NOT_MEASURED_ON_THIS_PATH``, never PASS.
    * **G2** — :func:`measure_ledger_parity`.

    The states themselves are produced by :func:`scoring.evaluate_gate_vector` on
    exactly these inputs: no second ontology, and the probe vector's other cells are
    never read.
    """
    # Deferred import: ``evaluation.runner`` imports this module at import time, so
    # the lineage measurement is taken from its owning module here, inside the call.
    from v8_next.evaluation.runner import measure_candle_lineage

    lineage_ok, lineage_info = measure_candle_lineage(tuple(candles))
    g2_state, g2_metrics = measure_ledger_parity(ledger)
    inputs = StructuralGateInputs(
        has_continuous_lineage=lineage_ok,
        is_causal_pit=None,
        g2_state=g2_state,
    )
    probe = evaluate_gate_vector(
        total_bars=len(candles),
        total_trades=0,
        pnl_series=[],
        mismatches=None,
        has_continuous_lineage=inputs.has_continuous_lineage,
        is_causal_pit=inputs.is_causal_pit,
        g2_state=inputs.g2_state,
    )
    metrics: dict[str, dict[str, Any]] = {
        "g0": {
            **lineage_info,
            "input": "has_continuous_lineage",
            "measured": lineage_ok,
        },
        "g1": {
            "status": "UNRUN",
            "reason": CAUSAL_PIT_NOT_MEASURED,
            "input": "is_causal_pit",
            "measured": None,
            "note": (
                "no per-bar decision-vs-availability audit is performed on this path; "
                "UNKNOWN, never PASS"
            ),
        },
        "g2": g2_metrics,
    }
    return StructuralGateResolution(inputs=inputs, gates=probe, metrics=metrics)


@dataclass(frozen=True)
class GateResolutionReport:
    """Full execution report of the G0-G9 gate resolution battery."""

    gates: GateVector
    g3_metrics: dict[str, Any]
    g4_metrics: dict[str, Any]
    g5_metrics: dict[str, Any]
    g6_metrics: dict[str, Any]
    g7_metrics: dict[str, Any]
    g8_metrics: dict[str, Any]
    g9_metrics: dict[str, Any]
    claim_record: StatutoryClaimRecord | None
    #: #447. What the structural trio was measured from, and the named reason each of
    #: its cells carries. The published state is only as good as the measurement
    #: behind it, so the measurement travels with the vector.
    g0_metrics: dict[str, Any] = field(default_factory=dict)
    g1_metrics: dict[str, Any] = field(default_factory=dict)
    g2_metrics: dict[str, Any] = field(default_factory=dict)


def resolve_all_gates(
    candles: Sequence[Candle],
    campaign_return_series: list[float],
    ledger: BenchmarkLedger,
    receipt_digest: str,
    capability_score: float,
    strategy_config: ExpertStrategyConfig | None = None,
    output_dir: Path | None = None,
    tape_path: Path | str | None = None,
    live_fills_path: Path | str | None = None,
) -> GateResolutionReport:
    """Run comprehensive empirical resolution of all gates G3-G9.

    ``campaign_return_series`` is the declared ``G5_SERIES_UNIT`` series
    (dimensionless per-campaign returns, NX02.R3) -- not the absolute USDT PnL
    series the scoring path consumes (#407).
    """
    cfg = strategy_config or ExpertStrategyConfig(
        min_support_quorum=1,
        max_contradiction_tolerance=28,
        bracket_stop_pct=Decimal("0.02"),
        bracket_target_pct=Decimal("0.04"),
    )
    out_dir = output_dir or Path("artifacts/benchmarks")

    # 1. G3 Scenario Robustness
    regimes = classify_market_regimes(tape_path=tape_path, candles=candles)
    g3_state, g3_metrics = evaluate_g3_scenario_robustness(regimes, cfg)

    # 2. G4 Synthetic / Adversarial Falsification
    g4_state, g4_metrics = evaluate_g4_synthetic_falsification(candles, cfg)

    # 3. G5 Selection Control (DSR & WRC). NX07.R3/R4: the plan is built here --
    # before any gate result exists -- and its statistical content is canonical.
    g5_plan_ = g5_plan(
        family=f"G5:{candles[0].start_ns}",
        pinned_ns=int(candles[0].start_ns),
        block_size=CANONICAL_G5_BLOCK_SIZE,
        reps=200,
        seed=42,
    )
    g5_state, g5_metrics = evaluate_g5_selection_control(
        campaign_return_series,
        candles,
        plan=g5_plan_,
        allow_regime_fallback=True,
        fallback_basis=(
            "declared regime fallback: a short own track (<20 campaigns) uses the "
            "regime series in the same declared unit (NX02.R3); sample_source is "
            "reported in the gate metrics"
        ),
        fallback_tape=tape_path,
    )

    # 4. G6 Frozen OOS Replication. #406/#421: without a caller-declared protected
    # window this fails closed by name (PROTECTED_WINDOW_ABSENT); the resolver does
    # not invent one from the bars it was handed.
    g6_state, g6_metrics = evaluate_g6_frozen_oos(candles, cfg)

    # 5. G7 Prospective Shadow. #406/#421: a declared forward window is required for
    # a verdict here as well; the historical tail is not prospective evidence.
    g7_state, g7_metrics = evaluate_g7_prospective_shadow(candles, cfg, output_dir=out_dir)

    # 6. G8 Live Realization
    g8_state, g8_metrics = evaluate_g8_live_realization(live_fills_path=live_fills_path)

    # 6b. #447: G0/G1/G2 are measured here, never declared. The same measured inputs
    # feed every vector this battery publishes -- the partial vector G9 is issued
    # against and the final one -- so all three cells below come from
    # `scoring.evaluate_gate_vector`, the canonical rule, and not from a literal.
    structural = resolve_structural_gates(candles, ledger)

    # Partial GateVector before G9 issuance (the pass-through args carry the run's
    # own campaign series; the cells they could decide are given explicitly).
    partial_gates = evaluate_gate_vector(
        total_bars=len(candles),
        total_trades=len(campaign_return_series),
        pnl_series=list(campaign_return_series),
        mismatches=None,
        has_continuous_lineage=structural.inputs.has_continuous_lineage,
        is_causal_pit=structural.inputs.is_causal_pit,
        g2_state=structural.inputs.g2_state,
        g3_state=g3_state,
        g4_state=g4_state,
        g5_state=g5_state,
        g6_state=g6_state,
        g7_state=g7_state,
        g8_state=g8_state,
    )

    # 7. G9 Certificate Authority (ClaimRegistry)
    live_verified = g8_state == GateState.PASS
    g9_state, g9_metrics, claim_record = evaluate_g9_certificate_authority(
        ledger=ledger,
        receipt_digest=receipt_digest,
        gates=partial_gates,
        capability_score=capability_score,
        output_dir=out_dir,
        live_realization_verified=live_verified,
    )

    final_gates = evaluate_gate_vector(
        total_bars=len(candles),
        total_trades=len(campaign_return_series),
        pnl_series=list(campaign_return_series),
        mismatches=None,
        has_continuous_lineage=structural.inputs.has_continuous_lineage,
        is_causal_pit=structural.inputs.is_causal_pit,
        g2_state=structural.inputs.g2_state,
        g3_state=g3_state,
        g4_state=g4_state,
        g5_state=g5_state,
        g6_state=g6_state,
        g7_state=g7_state,
        g8_state=g8_state,
        g9_state=g9_state,
    )

    return GateResolutionReport(
        gates=final_gates,
        g3_metrics=g3_metrics,
        g4_metrics=g4_metrics,
        g5_metrics=g5_metrics,
        g6_metrics=g6_metrics,
        g7_metrics=g7_metrics,
        g8_metrics=g8_metrics,
        g9_metrics=g9_metrics,
        claim_record=claim_record,
        g0_metrics=structural.metrics["g0"],
        g1_metrics=structural.metrics["g1"],
        g2_metrics=structural.metrics["g2"],
    )
