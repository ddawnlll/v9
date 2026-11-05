#!/usr/bin/env python
"""Cross-sectional relative-strength screen: dollar-neutral, direction-matched.

The four single-symbol candidates failed with the same shape: long cells lost and
short cells won, which is the window's drift rather than a signal. This screen
removes that confound by construction: at every aligned bar each symbol is
ranked by its trailing return, the weakest ``k`` are bought and the strongest
``k`` are sold, so every event carries the same number of long and short legs and
the same risk per leg. A random two-and-two selection at the same bars is the
matched null: it shares direction counts, timing, bracket and costs, and differs
only in which symbols are chosen.

Bar-path only: every row is ``MODEL_DERIVED``, no order is submitted, and the
strongest claim available is ``DEV_CANDIDATE``.

Usage:
    python tools/cross_sectional_screen.py
"""

from __future__ import annotations

import argparse
import json
import random
import statistics
import subprocess
import sys
from pathlib import Path
from typing import Any, Final

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from v8_next.research import metrics, screen  # noqa: E402

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

CONFIG: Final[dict[str, Any]] = {
    "lookback_bars": 24,
    "legs_per_side": 2,
    "stop_pct": 0.02,
    "target_pct": 0.04,
    "max_bars": 48,
    "maker_fee_bps": 2.0,
    "taker_fee_bps": 5.0,
    "slippage_bps": 2.0,
    "risk_budget_usdt": 100.0,
    "control_draws": 200,
    "control_seed": 20260101,
    "bootstrap_resamples": 2000,
    "bootstrap_seed": 20260101,
    "minimum_trades": 120,
    "minimum_positive_symbols": 6,
    "alpha": 0.05,
}

DEV_START_MS: Final[int] = 1_735_689_600_000  # 2025-01-01
DEV_END_MS: Final[int] = 1_746_057_600_000  # 2025-05-01
HOLDOUT_START_MS: Final[int] = DEV_END_MS


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
            ["git", "rev-parse", "HEAD"], cwd=REPO, check=True, capture_output=True, text=True
        ).stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return ""


def load_panel(
    symbols: tuple[str, ...], *, start_ms: int, end_ms: int
) -> tuple[dict[str, list[screen.Bar]], dict[str, tuple[tuple[int, float], ...]]]:
    bars: dict[str, list[screen.Bar]] = {}
    funding: dict[str, tuple[tuple[int, float], ...]] = {}
    for symbol in symbols:
        tape = TAPE_DIR / f"{symbol.lower()}-1h-2025H1" / "tape.jsonl"
        bars[symbol] = screen.load_bars(tape, instrument=symbol, start_ms=start_ms, end_ms=end_ms)
        funding[symbol] = screen.load_funding(tape, instrument=symbol, start_ms=start_ms, end_ms=end_ms)
    lengths = {len(v) for v in bars.values()}
    grids = {tuple(bar.end_ns for bar in v) for v in bars.values()}
    if len(lengths) != 1 or len(grids) != 1:
        raise RuntimeError(
            "symbols do not share one bar grid; a cross-sectional rank would be "
            f"computed on misaligned bars (lengths={sorted(lengths)})"
        )
    return bars, funding


def price_leg(
    bars: dict[str, list[screen.Bar]],
    funding: dict[str, tuple[tuple[int, float], ...]],
    *,
    symbol: str,
    index: int,
    direction: int,
) -> dict[str, Any]:
    series = bars[symbol]
    entry = series[index + 1].open
    stop, target = screen.static_bracket(
        entry,
        direction,
        stop_pct=CONFIG["stop_pct"],
        target_pct=CONFIG["target_pct"],
    )
    result = screen.evaluate_path(
        series,
        entry_ns=series[index].end_ns,
        entry=entry,
        direction=direction,
        stop=stop,
        target=target,
        policy="TIME_STOP",
        risk_budget_usdt=CONFIG["risk_budget_usdt"],
        fee_model=_fee_model(),
        max_bars=CONFIG["max_bars"],
        slippage_bps=CONFIG["slippage_bps"],
        funding=funding[symbol],
        target_maker=True,
    )
    return {
        "symbol": symbol,
        "bar_index": index,
        "entry_ns": series[index].end_ns,
        "direction": "LONG" if direction > 0 else "SHORT",
        "entry_price": entry,
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


def trailing_returns(
    bars: dict[str, list[screen.Bar]], *, symbols: tuple[str, ...], index: int, lookback: int
) -> dict[str, float]:
    return {
        symbol: bars[symbol][index].close / bars[symbol][index - lookback].close - 1.0
        for symbol in symbols
    }


def selection_events(
    bars: dict[str, list[screen.Bar]],
    *,
    symbols: tuple[str, ...],
    lookback: int,
    legs: int,
) -> list[dict[str, Any]]:
    """Bars on which a symbol newly enters an extreme rank.

    A symbol is traded once per entry into an extreme, not on every bar it stays
    there: re-entering the same state is the same information, and repeating it
    would inflate the sample with overlapping copies of one observation.
    """
    size = len(symbols)
    # The first bar on which a ranking exists is itself a transition: every
    # selected symbol moves from "not held" to "held".
    previous: dict[str, int] = {}
    events: list[dict[str, Any]] = []
    for index in range(lookback, len(bars[symbols[0]]) - 1):
        returns = trailing_returns(bars, symbols=symbols, index=index, lookback=lookback)
        ranked = sorted(symbols, key=lambda symbol: returns[symbol])
        state = {}
        for rank, symbol in enumerate(ranked):
            if rank < legs:
                state[symbol] = 1
            elif rank >= size - legs:
                state[symbol] = -1
            else:
                state[symbol] = 0
        for symbol, direction in state.items():
            if direction != 0 and previous.get(symbol) != direction:
                events.append(
                    {
                        "bar_index": index,
                        "symbol": symbol,
                        "direction": direction,
                        "trailing_return": returns[symbol],
                    }
                )
        previous = state
    return events


def event_legs(
    bars: dict[str, list[screen.Bar]],
    funding: dict[str, tuple[tuple[int, float], ...]],
    events: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for event in events:
        row = price_leg(
            bars,
            funding,
            symbol=event["symbol"],
            index=event["bar_index"],
            direction=event["direction"],
        )
        if row["realized_net_r"] is None:
            continue
        row["trailing_return"] = event["trailing_return"]
        rows.append(row)
    return rows


def matched_random_events(
    bars: dict[str, list[screen.Bar]],
    funding: dict[str, tuple[tuple[int, float], ...]],
    *,
    events: list[dict[str, Any]],
    symbols: tuple[str, ...],
    legs: int,
    seed: int,
    draws: int,
) -> list[dict[str, Any]]:
    """Same bar, same long/short counts, random symbols — the timing-matched null."""
    rng = random.Random(seed)
    out: list[dict[str, Any]] = []
    for draw in range(draws):
        draw_rows: list[dict[str, Any]] = []
        for event in events:
            index = event["bar_index"]
            long_pick = rng.sample(list(symbols), legs)
            short_pick = rng.sample([s for s in symbols if s not in long_pick], legs)
            for symbol in long_pick:
                draw_rows.append(
                    price_leg(bars, funding, symbol=symbol, index=index, direction=1)
                )
            for symbol in short_pick:
                draw_rows.append(
                    price_leg(bars, funding, symbol=symbol, index=index, direction=-1)
                )
        values = [row["realized_net_r"] for row in draw_rows if row["realized_net_r"] is not None]
        out.append({"draw": draw, "legs": len(values), "mean_net_r": statistics.fmean(values) if values else None})
    return out


def event_means(rows: list[dict[str, Any]]) -> list[float]:
    grouped: dict[int, list[float]] = {}
    for row in rows:
        if row["realized_net_r"] is None:
            continue
        grouped.setdefault(int(row["bar_index"]), []).append(row["realized_net_r"])
    return [statistics.fmean(values) for values in grouped.values()]


def run(*, start_ms: int, end_ms: int, lookback: int, legs: int) -> dict[str, Any]:
    symbols = UNIVERSE
    bars, funding = load_panel(symbols, start_ms=start_ms, end_ms=end_ms)
    events = selection_events(bars, symbols=symbols, lookback=lookback, legs=legs)
    rows = event_legs(bars, funding, events)
    summary = metrics.summarize(rows, net_key="realized_net_r")
    per_symbol: dict[str, Any] = {}
    for symbol in symbols:
        subset = [row for row in rows if row["symbol"] == symbol]
        per_symbol[symbol] = {
            "trades": len(subset),
            "net_expectancy_r": metrics.mean(row["realized_net_r"] for row in subset),
            "gross_expectancy_r": metrics.mean(row["realized_gross_r"] for row in subset),
        }
    symbol_means = [v["net_expectancy_r"] for v in per_symbol.values() if v["net_expectancy_r"] is not None]
    null = matched_random_events(
        bars,
        funding,
        events=events,
        symbols=symbols,
        legs=legs,
        seed=CONFIG["control_seed"],
        draws=CONFIG["control_draws"],
    )
    null_means = [draw["mean_net_r"] for draw in null if draw["mean_net_r"] is not None]
    treatment_event_means = event_means(rows)
    control_mean = statistics.fmean(null_means) if null_means else None
    p_value = (
        (1 + sum(1 for value in null_means if value >= summary["net_expectancy_r"]))
        / (1 + len(null_means))
        if null_means and summary["net_expectancy_r"] is not None
        else None
    )
    result = {
        "design": {
            "lookback_bars": lookback,
            "legs_per_side": legs,
            "events": len({row["bar_index"] for row in rows}),
            "legs": summary["trade_count"],
            "window": {"start_ms": start_ms, "end_ms": end_ms},
            "config": CONFIG,
        },
        "pooled": {
            "trades": summary["trade_count"],
            "net_expectancy_r": summary["net_expectancy_r"],
            "gross_expectancy_r": summary["gross_expectancy_r"],
            "win_rate": summary["win_rate"],
            "net_profit_factor": summary["net_profit_factor"],
            "total_net_pnl_usdt": summary["total_net_pnl"],
            "maximum_drawdown_usdt": summary["maximum_drawdown"],
            "positive_symbols": sum(1 for value in symbol_means if value > 0),
            "symbols_with_trades": len(symbol_means),
            "symbol_mean_ci": metrics.bootstrap_mean_ci(symbol_means, seed=CONFIG["bootstrap_seed"], resamples=CONFIG["bootstrap_resamples"]),
            "leg_mean_ci": metrics.bootstrap_mean_ci(
                [row["realized_net_r"] for row in rows],
                seed=CONFIG["bootstrap_seed"] + 1,
                resamples=CONFIG["bootstrap_resamples"],
            ),
            "control_mean_r": control_mean,
            "control_p_value": p_value,
            "treatment_event_mean_r": statistics.fmean(treatment_event_means) if treatment_event_means else None,
        },
        "symbols": per_symbol,
        "control": {"draws": CONFIG["control_draws"], "distribution_head": null_means[:5]},
        "code_sha": _code_sha(),
        "claim_status": "MODEL_DERIVED_DEV_SCREEN",
    }
    return result


def assess(result: dict[str, Any]) -> dict[str, Any]:
    pooled = result["pooled"]
    observed = pooled["net_expectancy_r"]
    reasons: list[str] = []
    if observed is None or not pooled["trades"]:
        return {"status": "INCONCLUSIVE", "reasons": ["NO_TRADES"]}
    if pooled["trades"] < CONFIG["minimum_trades"]:
        reasons.append(f"UNDERPOWERED:{pooled['trades']}<{CONFIG['minimum_trades']}")
    if observed <= 0:
        reasons.append("NON_POSITIVE_EXPECTANCY")
    if pooled["control_mean_r"] is not None and observed <= pooled["control_mean_r"]:
        reasons.append("NOT_ABOVE_MATCHED_CONTROL")
    if pooled["positive_symbols"] < CONFIG["minimum_positive_symbols"]:
        reasons.append(f"CONCENTRATED:{pooled['positive_symbols']}<{CONFIG['minimum_positive_symbols']}")
    if pooled["control_p_value"] is None or pooled["control_p_value"] >= CONFIG["alpha"]:
        reasons.append("CONTROL_P_VALUE_NOT_SIGNIFICANT")
    status = "DEV_CANDIDATE" if not reasons else (
        "INCONCLUSIVE" if pooled["trades"] < CONFIG["minimum_trades"] else "NO_EDGE"
    )
    return {"status": status, "reasons": reasons}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=REPO / "reports" / "research" / "cross_sectional")
    parser.add_argument("--start-ms", type=int, default=DEV_START_MS)
    parser.add_argument("--end-ms", type=int, default=DEV_END_MS)
    parser.add_argument("--sensitivity", action="store_true")
    args = parser.parse_args()

    if args.start_ms >= HOLDOUT_START_MS and __import__("os").environ.get("V9_HOLDOUT_AUTHORIZED") != "1":
        raise SealedWindowRefusal("holdout sealed; see reports/research/LEDGER.md")

    args.out.mkdir(parents=True, exist_ok=True)
    primary = run(
        start_ms=args.start_ms,
        end_ms=args.end_ms,
        lookback=int(CONFIG["lookback_bars"]),
        legs=int(CONFIG["legs_per_side"]),
    )
    primary["assessment"] = assess(primary)
    (args.out / "primary.json").write_text(json.dumps(primary, indent=2, sort_keys=True), encoding="utf-8")

    payload: dict[str, Any] = {
        "primary": {
            "lookback_bars": CONFIG["lookback_bars"],
            "legs_per_side": CONFIG["legs_per_side"],
            **primary["pooled"],
            "status": primary["assessment"]["status"],
            "reasons": primary["assessment"]["reasons"],
        }
    }
    if args.sensitivity:
        payload["sensitivity"] = {}
        for lookback, legs in ((4, 2), (72, 2), (24, 1), (24, 3)):
            variant = run(
                start_ms=args.start_ms, end_ms=args.end_ms, lookback=lookback, legs=legs
            )
            payload["sensitivity"][f"L{lookback}_k{legs}"] = {
                "trades": variant["pooled"]["trades"],
                "net_expectancy_r": variant["pooled"]["net_expectancy_r"],
                "control_mean_r": variant["pooled"]["control_mean_r"],
                "positive_symbols": variant["pooled"]["positive_symbols"],
            }
        (args.out / "sensitivity.json").write_text(json.dumps(payload["sensitivity"], indent=2, sort_keys=True), encoding="utf-8")
    (args.out / "summary.json").write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
