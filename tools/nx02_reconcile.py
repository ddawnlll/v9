#!/usr/bin/env python
"""NX02 (#423) R6 — reconcile table for the two reporting paths on a real window.

Runs ONE real engine execution on a small real window and reports both reporting
paths over it, side by side with the native account:

* ``runner_path`` — ``campaign_accounting`` + ``reconcile_native_account``
  (the contract ``evaluation/runner.py`` now uses);
* ``economic_path`` — ``strategy_series_from_engine`` (the portfolio/economic
  reporting path) and its own cost reconciliation block;
* ``native`` — the engine's own ``balance_total`` and per-position totals.

The table is written to ``--out`` and each file is hashed. Nothing is synthesized;
a missing tape fails the run.

Usage (from the repository root):

    uv run --project . --extra dev --extra research \\
        python tools/nx02_reconcile.py
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from decimal import Decimal
from pathlib import Path

from v8_next.adapters.expert_strategy import ExpertStrategyConfig, run_expert_strategy_backtest
from v8_next.evaluation.economic_benchmark import (
    CAPITAL_DEFAULT,
    bars_from_candles,
    campaign_accounting,
    reconcile_native_account,
    strategy_series_from_engine,
)
from v8_next.evaluation.gate_resolution import DEFAULT_TAPE_PATH, load_tape_candles


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", default=None)
    parser.add_argument("--bars", type=int, default=500, help="real window length")
    parser.add_argument("--out", default="docs/evidence/v87/NX02")
    parser.add_argument("--quorum", type=int, default=1)
    parser.add_argument("--tolerance", type=int, default=28)
    args = parser.parse_args(argv)

    repo_root = (
        Path(args.repo_root).resolve() if args.repo_root else Path(__file__).resolve().parents[1]
    )
    out_dir = (repo_root / args.out).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    if not DEFAULT_TAPE_PATH.exists():
        print(f"[NX02] FAIL: real tape absent at {DEFAULT_TAPE_PATH}")
        return 2

    candles = load_tape_candles(DEFAULT_TAPE_PATH, limit=args.bars)
    config = ExpertStrategyConfig(
        min_support_quorum=args.quorum, max_contradiction_tolerance=args.tolerance
    )
    engine_result = run_expert_strategy_backtest(
        tuple(candles), config, initial_balance=Decimal(str(CAPITAL_DEFAULT))
    )
    bars = bars_from_candles(tuple(candles))
    cutoff_ns = candles[-1].end_ns

    accounting = campaign_accounting(
        engine_result["opened_positions"],
        engine_result.get("closed_positions", []),
        bars=bars,
        cutoff_ns=cutoff_ns,
    )
    runner_view = reconcile_native_account(
        engine_result.get("account") or {},
        accounting=accounting,
        initial_balance=CAPITAL_DEFAULT,
    )
    economic_view = strategy_series_from_engine(engine_result, bars, capital=CAPITAL_DEFAULT)

    table = {
        "window": {
            "tape": str(DEFAULT_TAPE_PATH),
            "bars": len(candles),
            "first_bar_end_ns": candles[0].end_ns,
            "last_bar_end_ns": cutoff_ns,
            "quorum": args.quorum,
            "contradiction_tolerance": args.tolerance,
            "initial_balance_usdt": str(CAPITAL_DEFAULT),
        },
        "units": accounting.units,
        "native": {
            "balance_total": (engine_result.get("account") or {}).get("balance_total"),
            "positions": len((engine_result.get("account") or {}).get("positions") or []),
        },
        "runner_path": {
            "accounting": accounting.as_dict(),
            "reconciliation": runner_view,
        },
        "economic_path": {
            "n_trades": economic_view.get("n_trades"),
            "cost_reconciliation": economic_view.get("cost_reconciliation"),
            "funding": economic_view.get("funding"),
        },
        "cross_checks": {
            "runner_status": runner_view["status"],
            "economic_closed_loop_error": (economic_view.get("cost_reconciliation") or {}).get(
                "closed_loop_error"
            ),
            "runner_vs_economic_trade_count": {
                "runner_completed_campaigns": accounting.closed_campaigns,
                "economic_n_completed_campaigns": economic_view.get("n_completed_campaigns"),
                "economic_n_open_positions": economic_view.get("n_open_positions"),
                "economic_n_trades_legacy": economic_view.get("n_trades"),
            },
            "same_engine_run": True,
        },
    }
    path = out_dir / "reconcile_table.json"
    path.write_text(json.dumps(table, indent=2, sort_keys=True, default=str) + "\n")
    print(f"[NX02] bars={len(candles)} cutoff_ns={cutoff_ns}")
    print(f"[NX02] runner_status={runner_view['status']} reasons={runner_view['reasons']}")
    print(f"[NX02] native_balance={runner_view['native_balance_usdt']}")
    print(f"[NX02] replay_balance={runner_view['replay_balance_usdt']}")
    print(f"[NX02] closed_campaigns={accounting.closed_campaigns} open={accounting.open_campaigns}")
    print(f"[NX02] open_risk_usdt={accounting.open_risk_usdt}")
    print(f"[NX02] writer={path.relative_to(repo_root)} sha256:{_sha256(path)}")
    return 0 if runner_view["status"] == "MATCHED" else 3


if __name__ == "__main__":
    sys.exit(main())
