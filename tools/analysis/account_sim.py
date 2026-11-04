#!/usr/bin/env python3
"""Replay the production 28-expert ensemble on a real tape with a chosen account size.

This is a *simulation record*, not a performance claim and not investment
advice: it replays the same `run_portfolio_backtest` adapter the benchmark
measures, on historical Binance hourly archives, with the repository's declared
baseline execution profile (fill/fee/latency models all fixed by name).

Boundaries that make the output what it is:

* **Window**: whatever monthly archives the caller points at. The authorised
  development window is BTCUSDT Jan-Mar 2025; other windows are exploratory.
* **Universe**: real archives only. The capture set holds ten symbols, so a
  "40 symbols" request cannot be served from real data and is not faked here.
* **Sizing**: ``--start-balance`` sets the account, ``--notional-per-leg`` the
  target notional per instrument. The benchmark's 10,000 / 1,000 pair is 10 %
  of equity per leg; a 500 account is comparable at 50.
* **Status**: the adapter stamps ``NO_ECONOMIC_CLAIM`` / ``SIMULATED`` on the
  result. In-sample, 90 days is a tiny sample, and fees plus funding are paid.

Usage:
  python tools/analysis/account_sim.py --tape <dir> --symbols A,B --bars 2160 \
      --start-balance 500 --notional-per-leg 50 --with-funding --output out.json
"""
from __future__ import annotations

import argparse
import json
import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools" / "performance"))

from portfolio_tape import _load_funding  # noqa: E402  (same funding source as the benchmark)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tape", type=Path, required=True)
    parser.add_argument("--symbols", required=True, help="comma-separated raw symbols, e.g. BTCUSDT,ETHUSDT")
    parser.add_argument("--bars", type=int, required=True, help="hourly candles per instrument")
    parser.add_argument("--start-balance", type=Decimal, required=True)
    parser.add_argument("--notional-per-leg", type=Decimal, required=True)
    parser.add_argument("--with-funding", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    from v8_next.adapters.portfolio_backtest import SleeveSpec, run_portfolio_backtest
    from v8_next.evaluation.gate_resolution import load_tape_candles

    symbols = [part.strip() for part in args.symbols.split(",") if part.strip()]
    legs = {s: tuple(load_tape_candles(args.tape, limit=args.bars, instrument=s)) for s in symbols}
    for symbol, candles in legs.items():
        if not candles:
            raise ValueError(f"no candles for {symbol} in {args.tape}")

    funding: tuple = ()
    if args.with_funding:
        first = min(leg[0].start_ns for leg in legs.values())
        last = max(leg[-1].end_ns for leg in legs.values())
        rows, _dropped = _load_funding(args.tape, symbols, first // 1_000_000, last // 1_000_000)
        funding = tuple(rows)

    result = run_portfolio_backtest(
        legs,
        (SleeveSpec("account-sim", 1, 28, 1.0),),
        funding,
        per_leg_notional=args.notional_per_leg,
        initial_balance=args.start_balance,
        execution_profile="baseline",
    )

    closed = result["closed_positions"]
    def money(value: object) -> Decimal:
        """Native reports print money as ``"3.86962354 USDT"``."""
        return Decimal(str(value).split()[0])

    pnl = [money(row["realized_pnl"]) for row in closed]
    # The execution block already totals commissions per currency; fill rows
    # carry a list of per-currency strings.
    fees = sum(
        (Decimal(str(value)) for value in result["execution"]["commission_totals_by_currency"].values()),
        Decimal(0),
    )
    per_symbol: dict[str, dict[str, object]] = {}
    for row in closed:
        bucket = per_symbol.setdefault(str(row["instrument_id"]), {"trades": 0, "pnl": Decimal(0)})
        bucket["trades"] = int(bucket["trades"]) + 1
        bucket["pnl"] = bucket["pnl"] + money(row["realized_pnl"])
    ending = Decimal(str(result["account"]["balance_total"]).split()[0])
    window = (min(leg[0].start_ns for leg in legs.values()), max(leg[-1].end_ns for leg in legs.values()))
    # Equity path from realised trade P&L, so the headline number carries its
    # drawdown: a +4 % four-year result with a -20 % drawdown is not "free".
    curve: list[tuple[int, Decimal]] = []
    running = Decimal(0)
    for row in sorted(closed, key=lambda item: int(item["event_ns"])):
        running += money(row["realized_pnl"])
        curve.append((int(row["event_ns"]), running))
    peak = Decimal(0)
    max_dd = Decimal(0)
    for _, value in curve:
        peak = max(peak, value)
        max_dd = max(max_dd, peak - value)
    import datetime as _dt

    by_year: dict[str, Decimal] = {}

    for row in closed:
        stamp = _dt.datetime.fromtimestamp(int(row["event_ns"]) / 1e9, tz=_dt.UTC)
        key = str(stamp.year)
        by_year[key] = by_year.get(key, Decimal(0)) + money(row["realized_pnl"])
    summary = {
        "claim_status": result["account"]["claim_status"],
        "realization": result["account"]["realization"],
        "symbols": symbols,
        "bars_per_instrument": {s: len(c) for s, c in legs.items()},
        "window_ns": list(window),
        "days": round((window[1] - window[0]) / 86_400_000_000_000, 1),
        "start_balance": str(args.start_balance),
        "notional_per_leg": str(args.notional_per_leg),
        "ending_balance": str(ending),
        "net_pnl": str(ending - args.start_balance),
        "net_return_pct": str(((ending / args.start_balance) - 1) * 100),
        "gross_realized_pnl": str(sum(pnl, Decimal(0))),
        "fees_paid": str(-fees),
        "funding_settlements": result.get("funding_settlements_fed"),
        "trades": len(closed),
        "wins": sum(1 for value in pnl if value > 0),
        "losses": sum(1 for value in pnl if value < 0),
        "fills": len(result["fill_records"]),
        "decisions": result["decisions_count"],
        "max_drawdown": str(max_dd),
        "max_drawdown_pct_of_start": str(max_dd / args.start_balance * 100),
        "pnl_by_year": {key: str(value) for key, value in sorted(by_year.items())},
        "fees_as_pct_of_gross_edge": (
            str(fees / (sum(pnl, Decimal(0)) + fees) * 100)
            if (sum(pnl, Decimal(0)) + fees) != 0
            else None
        ),
        "equity_curve": [{"event_ns": event_ns, "cumulative_pnl": str(value)} for event_ns, value in curve],
        "per_symbol": {
            key: {"trades": value["trades"], "pnl": str(value["pnl"])}
            for key, value in sorted(per_symbol.items())
        },
        "caveat": (
            "SIMULATED on historical hourly archives with the repository's baseline "
            "fill/fee/latency profile. In-sample development window, no out-of-sample "
            "confirmation, no economic claim, not investment advice."
        ),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps({k: summary[k] for k in (
        "days", "start_balance", "ending_balance", "net_pnl", "net_return_pct",
        "gross_realized_pnl", "fees_paid", "trades", "wins", "losses", "fills")}, indent=2))


if __name__ == "__main__":
    main()
