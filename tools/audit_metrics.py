#!/usr/bin/env python
"""Compute trade economics, MFE/MAE, post-exit paths and 20-USDT feasibility."""
from __future__ import annotations

import argparse
import csv
import json
import statistics
from pathlib import Path
from typing import Any

HOUR_NS = 3_600_000_000_000
HORIZONS = (1, 3, 6, 12, 24, 48)


def _jsonl(p: Path) -> list[dict[str, Any]]:
    return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()]


def _json(p: Path) -> Any:
    return json.loads(p.read_text(encoding="utf-8"))


def _write_json(p: Path, x: Any) -> None:
    p.write_text(json.dumps(x, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")


def _stats(vals: list[float]) -> dict[str, Any]:
    if not vals:
        return {"n": 0, "mean": None, "median": None, "min": None, "max": None}
    s = sorted(vals)
    return {
        "n": len(vals),
        "mean": sum(vals) / len(vals),
        "median": statistics.median(vals),
        "min": s[0],
        "max": s[-1],
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--run-dir", required=True, type=Path)
    ap.add_argument("--baseline", default="baseline", type=Path)
    ap.add_argument("--account-usdt", type=float, default=20.0)
    ap.add_argument("--maker-fee", type=float, default=None)
    ap.add_argument("--taker-fee", type=float, default=None)
    ap.add_argument("--initial-balance", type=float, default=None)
    ap.add_argument("--stop-pct", type=float, default=None)
    args = ap.parse_args(argv)
    run = args.run_dir.resolve()
    base = (run / args.baseline).resolve()
    manifest = _json(base / "run_manifest.json")
    cfg = manifest.get("config", {})

    def declared(name: str, override: float | None) -> float:
        if override is not None:
            return float(override)
        if name not in cfg:
            raise ValueError(f"{name} is not declared in run_manifest.json; pass --{name.replace('_', '-')}")
        return float(cfg[name])

    maker_fee = declared("maker_fee", args.maker_fee)
    taker_fee = declared("taker_fee", args.taker_fee)
    initial_balance = declared("initial_balance", args.initial_balance)
    stop_pct = declared("bracket_stop_pct", args.stop_pct)
    account_usdt = float(args.account_usdt)
    pos = _jsonl(run / "positions.jsonl")
    decisions = _jsonl(base / "expert_decisions.jsonl")
    bars = [
        {
            "ns": int(d["decision_ns"]),
            "open": float(d["market"]["open"]),
            "high": float(d["market"]["high"]),
            "low": float(d["market"]["low"]),
            "close": float(d["market"]["close"]),
        }
        for d in decisions
    ]
    idx = {b["ns"]: i for i, b in enumerate(bars)}
    rows: list[dict[str, Any]] = []
    mfe_rows: list[dict[str, Any]] = []
    post_rows: list[dict[str, Any]] = []

    for c in pos:
        sign = 1 if c["direction"] == "LONG" else -1
        ei = idx[int(c["entry_fill_ns"])]
        entry = float(c["entry_fill_price"])
        qty = float(c["quantity"])
        stop = float(c["stop_price"])
        target = float(c["target_price"])
        declared_risk_price = abs(float(c["entry_reference"]) - stop)
        fill_risk_price = abs(entry - stop)
        declared_risk = declared_risk_price * qty
        fill_risk = fill_risk_price * qty
        closed = c["status"] == "CLOSED"
        xi = idx[int(c["exit_fill_ns"])] if closed else len(bars) - 1
        exit_px = float(c["exit_fill_price"]) if closed else None

        # Conservative window: bars strictly after the entry bar and strictly
        # before the exit bar, plus the realised exit price. The upper bound
        # additionally includes the exit bar's high/low and is labelled as an
        # OHLC intrabar upper bound, not a claim about the event sequence.
        interior = bars[ei + 1:xi] if closed else bars[ei + 1:]
        def fav(high: float, low: float, entry: float = entry, sign: int = sign) -> float:
            return (high - entry) if sign == 1 else (entry - low)

        def adv(high: float, low: float, entry: float = entry, sign: int = sign) -> float:
            return (entry - low) if sign == 1 else (high - entry)

        if interior:
            hi = max(b["high"] for b in interior)
            lo = min(b["low"] for b in interior)
        else:
            hi = lo = entry
        if closed:
            mfe_px = max(fav(hi, lo), (exit_px - entry) * sign, 0.0)
            mae_px = max(adv(hi, lo), (entry - exit_px) * sign, 0.0)
            upper = bars[ei + 1:xi + 1]
            mfe_up = max(fav(max(b["high"] for b in upper), min(b["low"] for b in upper)), 0.0) if upper else mfe_px
            mae_up = max(adv(max(b["high"] for b in upper), min(b["low"] for b in upper)), 0.0) if upper else mae_px
        else:
            mfe_px = max(fav(hi, lo), 0.0)
            mae_px = max(adv(hi, lo), 0.0)
            mfe_up, mae_up = mfe_px, mae_px
        mfe_px, mae_px, mfe_up, mae_up = max(mfe_px, 0.0), max(mae_px, 0.0), max(mfe_up, 0.0), max(mae_up, 0.0)
        mfe_usdt = mfe_px * qty
        mae_usdt = mae_px * qty
        gross = c["gross_price_pnl_usdt"]
        net = c["net_realized_pnl_usdt"]
        implied = c["implied_fee_and_adjustment_usdt"]
        realised_move = ((exit_px - entry) * sign) if closed else None
        capture = (realised_move / mfe_px) if closed and mfe_px > 0 and realised_move is not None else None
        net_capture = (net / mfe_usdt) if closed and mfe_usdt > 0 and net is not None else None
        entry_notional = entry * qty
        exit_notional = (exit_px or 0.0) * qty
        entry_fee_rate = taker_fee if c["entry_order_type"] == "MARKET" else maker_fee
        exit_fee_rate = maker_fee if c["exit_kind"] == "TARGET" else taker_fee
        entry_fee = entry_notional * entry_fee_rate
        exit_fee = exit_notional * exit_fee_rate
        modelled_fee = entry_fee + exit_fee
        modelled_all_taker = entry_notional * taker_fee + exit_notional * taker_fee
        row = {
            "campaign_id": c["campaign_id"],
            "opportunity_id": c["opportunity_id"],
            "decision_utc": c["decision_utc"],
            "direction": c["direction"],
            "status": c["status"],
            "exit_kind": c["exit_kind"],
            "quantity": qty,
            "entry_reference": float(c["entry_reference"]),
            "entry_fill_price": entry,
            "stop_price": stop,
            "target_price": target,
            "exit_fill_price": exit_px,
            "holding_bars": c["holding_bars"],
            "declared_reward_risk": c["declared_reward_risk"],
            "declared_risk_usdt": declared_risk,
            "fill_risk_usdt": fill_risk,
            "gross_pnl_usdt": gross,
            "net_pnl_usdt": net,
            "implied_fee_and_adjustment_usdt": implied,
            "mfe_price": mfe_px,
            "mae_price": mae_px,
            "mfe_usdt": mfe_usdt,
            "mae_usdt": mae_usdt,
            "mfe_R_declared": (mfe_usdt / declared_risk) if declared_risk > 0 else None,
            "mae_R_declared": (mae_usdt / declared_risk) if declared_risk > 0 else None,
            "realized_net_R_declared": (net / declared_risk) if net is not None and declared_risk > 0 else None,
            "realized_net_R_fill": (net / fill_risk) if net is not None and fill_risk > 0 else None,
            "mfe_R_upper_ohlc": (mfe_up * qty / declared_risk) if declared_risk > 0 else None,
            "mae_R_upper_ohlc": (mae_up * qty / declared_risk) if declared_risk > 0 else None,
            "favorable_capture": capture,
            "net_capture": net_capture,
            "entry_notional_usdt": entry_notional,
            "exit_notional_usdt": exit_notional,
            "modelled_entry_fee_usdt": entry_fee,
            "modelled_exit_fee_usdt": exit_fee,
            "modelled_fee_usdt": modelled_fee,
            "modelled_fee_all_taker_usdt": modelled_all_taker,
            "funding_usdt": None,
            "funding_status": "NOT_FED_TO_THIS_PATH_NO_MARK_PRICE",
            "measured_execution_shortfall_usdt": None,
            "entry_slippage_vs_reference_usdt": (entry - float(c["entry_reference"])) * sign * qty,
            "execution_profile": None,
            "ohlc_uncertainty": "MFE/MAE conservative + upper interval; intrabar order unresolved",
        }
        rows.append(row)
        mfe_rows.append({k: row[k] for k in (
            "campaign_id", "direction", "exit_kind", "mfe_price", "mae_price", "mfe_usdt", "mae_usdt",
            "mfe_R_declared", "mae_R_declared", "mfe_R_upper_ohlc", "mae_R_upper_ohlc",
            "favorable_capture", "net_capture",
        )})
        if closed:
            for h in HORIZONS:
                j = xi + h
                if j >= len(bars):
                    continue
                path = bars[xi + 1:j + 1]
                term = (bars[j]["close"] - exit_px) * sign / exit_px
                ph = max(b["high"] for b in path)
                pl = min(b["low"] for b in path)
                mfe = max((ph - exit_px) if sign == 1 else (exit_px - pl), 0.0)
                mae = max((exit_px - pl) if sign == 1 else (ph - exit_px), 0.0)
                post_rows.append({
                    "campaign_id": c["campaign_id"],
                    "direction": c["direction"],
                    "exit_kind": c["exit_kind"],
                    "exit_utc": __import__("datetime").datetime.fromtimestamp(
                        int(c["exit_fill_ns"]) / 1e9, __import__("datetime").timezone.utc
                    ).isoformat().replace("+00:00", "Z"),
                    "exit_fill_price": exit_px,
                    "horizon_bars": h,
                    "terminal_return": term,
                    "post_exit_mfe": mfe,
                    "post_exit_mae": mae,
                    "continued": term > 0,
                    "reversed": term < 0,
                })

    # CSV columns are numeric/string only; no nested objects.
    fields = list(rows[0].keys())
    with (run / "trade_metrics.csv").open("x", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)
    with (run / "mfe_mae.jsonl").open("x", encoding="utf-8") as fh:
        for r in mfe_rows:
            fh.write(json.dumps(r, sort_keys=True) + "\n")
    _write_json(run / "post_exit_analysis.json", post_rows)

    closed = [r for r in rows if r["status"] == "CLOSED"]
    net_r = [r["realized_net_R_declared"] for r in closed if r["realized_net_R_declared"] is not None]
    gross_r = [
        r["gross_pnl_usdt"] / r["declared_risk_usdt"]
        for r in closed if r["gross_pnl_usdt"] is not None and r["declared_risk_usdt"] > 0
    ]
    wins = [x for x in net_r if x > 0]
    losses = [x for x in net_r if x < 0]
    avg_win = sum(wins) / len(wins) if wins else None
    avg_loss = sum(losses) / len(losses) if losses else None
    payoff = (avg_win / abs(avg_loss)) if avg_win is not None and avg_loss not in (None, 0) else None
    breakeven = (1 / (1 + payoff)) if payoff is not None else None
    win_rate = len(wins) / len(net_r) if net_r else None
    rr_values = sorted(
        {
            round(float(c["declared_reward_risk"]), 8)
            for c in closed
            if c.get("declared_reward_risk") is not None
        }
    )
    declared_rr = rr_values[0] if len(rr_values) == 1 else None
    asymmetric = {
        "closed_trades": len(closed),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": win_rate,
        "declared_reward_risk": declared_rr,
        "theoretical_breakeven_win_rate_at_declared_RR": (
            1 / (1 + declared_rr) if declared_rr is not None else None
        ),
        "realized_avg_win_R": avg_win,
        "realized_avg_loss_R": avg_loss,
        "realized_payoff_ratio": payoff,
        "realized_breakeven_win_rate": breakeven,
        "net_expectancy_R": (sum(net_r) / len(net_r)) if net_r else None,
        "gross_expectancy_R": (sum(gross_r) / len(gross_r)) if gross_r else None,
        "fee_and_adjustment_drag_R_mean": (
            statistics.mean([r["implied_fee_and_adjustment_usdt"] / r["declared_risk_usdt"] for r in closed])
            if closed else None
        ),
        "profit_factor_gross": (
            sum(r["gross_pnl_usdt"] for r in closed if r["gross_pnl_usdt"] > 0) /
            abs(sum(r["gross_pnl_usdt"] for r in closed if r["gross_pnl_usdt"] < 0))
            if any(r["gross_pnl_usdt"] < 0 for r in closed) else None
        ),
        "profit_factor_net": (
            sum(r["net_pnl_usdt"] for r in closed if r["net_pnl_usdt"] > 0) /
            abs(sum(r["net_pnl_usdt"] for r in closed if r["net_pnl_usdt"] < 0))
            if any(r["net_pnl_usdt"] < 0 for r in closed) else None
        ),
        "status": "INSUFFICIENT_SAMPLE_FOR_INFERENCE" if len(closed) < 100 else "DESCRIPTIVE",
        "limitations": [
            f"{len(closed)} closed trades are a small, overlapping, single-window sample.",
            "R uses the declared decision-bar risk, not a portfolio risk budget.",
        ],
    }
    cost = {
        "closed_trades": len(closed),
        "gross_pnl_usdt_sum": sum(r["gross_pnl_usdt"] for r in closed),
        "net_pnl_usdt_sum": sum(r["net_pnl_usdt"] for r in closed),
        "implied_fee_and_adjustment_usdt_sum": sum(r["implied_fee_and_adjustment_usdt"] for r in closed),
        "modelled_fee_usdt_sum": sum(r["modelled_fee_usdt"] for r in closed),
        "modelled_fee_all_taker_usdt_sum": sum(r["modelled_fee_all_taker_usdt"] for r in closed),
        "entry_slippage_vs_reference_usdt_sum": sum(r["entry_slippage_vs_reference_usdt"] for r in closed),
        "funding_usdt_sum": None,
        "funding_status": "NOT_FED_TO_THIS_PATH_NO_MARK_PRICE",
        "measured_execution_shortfall_usdt": None,
        "fee_reconciliation_residual_usdt": (
            sum(r["implied_fee_and_adjustment_usdt"] for r in closed)
            - sum(r["modelled_fee_usdt"] for r in closed)
        ),
        "interpretation": "Implied cost is gross price PnL minus native net realized PnL. It is not a pure fee measurement; it can include fee model and other native adjustments.",
    }
    _write_json(run / "asymmetric_payoff.json", asymmetric)
    _write_json(run / "cost_analysis.json", cost)

    # 20-USDT feasibility uses declared simulation metadata and observed median price.
    median_px = statistics.median(b["close"] for b in bars)
    def scenario(qty: float, label: str) -> dict[str, Any]:
        notional = qty * median_px
        stop_risk = notional * stop_pct
        fee = notional * taker_fee * 2
        return {
            "label": label,
            "quantity_btc": qty,
            "median_price_usdt": median_px,
            "notional_usdt": notional,
            "required_leverage_for_account": notional / account_usdt,
            "stop_risk_usdt": stop_risk,
            "stop_risk_pct_of_account": stop_risk / account_usdt,
            "round_trip_taker_fee_usdt": fee,
            "fee_pct_of_account": fee / account_usdt,
        }
    scenarios = [
        scenario(0.01, "baseline default order quantity"),
        scenario(0.001, "native minimum quantity"),
    ]
    max_required_leverage = max(s["required_leverage_for_account"] for s in scenarios)
    capital = {
        "account_usdt": account_usdt,
        "baseline_initial_balance_usdt": initial_balance,
        "native_min_quantity_btc": 0.001,
        "native_size_increment_btc": 0.001,
        "native_min_notional_usdt": 1.0,
        "native_default_leverage": 1,
        "scenarios": scenarios,
        "verdict": (
            "FEASIBLE_AT_1X"
            if max_required_leverage <= 1
            else "NOT_FEASIBLE_AT_1X_WITH_DEFAULT_OR_MINIMUM_QUANTITY"
        ),
        "limitations": [
            "Historical Binance leverage brackets, mark price, liquidation and actual exchange limits are absent.",
            "The native instrument metadata is a simulation contract, not a venue historical schedule.",
            (
                f"A {account_usdt:g}-USDT account would need up to "
                f"{max_required_leverage:.1f}x leverage or a smaller instrument; "
                "this is not a claim that such leverage is safe."
            ),
        ],
    }
    _write_json(run / "capital_20usdt.json", capital)

    summary = {
        "trades": len(rows),
        "closed": len(closed),
        "open_at_cutoff": len(rows) - len(closed),
        "net_R": _stats(net_r),
        "gross_R": _stats(gross_r),
        "mfe_R": _stats([r["mfe_R_declared"] for r in rows if r["mfe_R_declared"] is not None]),
        "mae_R": _stats([r["mae_R_declared"] for r in rows if r["mae_R_declared"] is not None]),
        "asymmetric_payoff": asymmetric,
        "cost": cost,
        "capital_20usdt": capital,
    }
    _write_json(run / "trade_metrics_summary.json", summary)
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
