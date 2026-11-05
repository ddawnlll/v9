#!/usr/bin/env python
"""Research-only management ablations and execution-cost stress.

Ablations use the frozen flags already present on ``SimArmConfig``; no strategy
code is changed, and the full arm (``FULL``) is the production configuration:

============  =====================  ==========  =========  ==========  ==========
key           adaptive management    struct stop struct tgt  stop amend  tgt amend
============  =====================  ==========  =========  ==========  ==========
C_STATIC      no                     fixed       fixed       no          no
STOP_ONLY     yes                    structural  fixed       no          no
TARGET_ONLY   yes                    fixed       structural  no          no
BE_ONLY       yes                    fixed       fixed       yes         no
FULL          yes                    structural  structural  yes         yes
============  =====================  ==========  =========  ==========  ==========

Cost stress keeps the reference fee profile (maker 2 bps / taker 5 bps) and adds
an incremental per-side execution cost of +2/+5/+10 bps to *both* sides. In this
bar-driven engine a fee increment and a slippage increment are the same cash flow
(notional x bps per side), so the added cost is charged as commission and never
stacked on an existing slippage model (``fill_model=default``, ``SLIPPED=False``).
The break-even increment is solved from the measured cost turnover, not guessed.

Usage
    uv run --locked --extra dev python tools/cross_market_ablations.py \
        --out reports/v9-cross-market-validation/data/ablations
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from decimal import Decimal
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "tools"))

from cross_market_matrix import UNIVERSE, derive  # noqa: E402

from v8_next.adapters.adaptive_strategy import SimArmConfig  # noqa: E402
from v8_next.evaluation.economic_benchmark import parse_money  # noqa: E402
from v8_next.evaluation.instrument_metadata import filters_for  # noqa: E402
from v8_next.evaluation.sim_arm import run_sim_arm  # noqa: E402

ABLATIONS: dict[str, dict[str, Any]] = {
    "C_STATIC": dict(
        adaptive_management_enabled=False,
        structural_stop_enabled=False,
        structural_target_enabled=False,
        stop_amendments_enabled=False,
        target_amendments_enabled=False,
    ),
    "STOP_ONLY": dict(
        adaptive_management_enabled=True,
        structural_stop_enabled=True,
        structural_target_enabled=False,
        stop_amendments_enabled=False,
        target_amendments_enabled=False,
    ),
    "TARGET_ONLY": dict(
        adaptive_management_enabled=True,
        structural_stop_enabled=False,
        structural_target_enabled=True,
        stop_amendments_enabled=False,
        target_amendments_enabled=False,
    ),
    "BE_ONLY": dict(
        adaptive_management_enabled=True,
        structural_stop_enabled=False,
        structural_target_enabled=False,
        stop_amendments_enabled=True,
        target_amendments_enabled=False,
    ),
    "FULL": dict(
        adaptive_management_enabled=True,
        structural_stop_enabled=True,
        structural_target_enabled=True,
        stop_amendments_enabled=True,
        target_amendments_enabled=True,
    ),
}

STRESS_BPS = (2, 5, 10)
BASE_MAKER = Decimal("0.0002")
BASE_TAKER = Decimal("0.0005")


def config_for(symbol: str, **flags: Any) -> SimArmConfig:
    return SimArmConfig(
        enabled=True,
        instrument_id=f"{symbol}-PERP.BINANCE",
        bar_type_str=f"{symbol}-PERP.BINANCE-1-HOUR-LAST-EXTERNAL",
        **flags,
    )


def paths(symbol: str) -> tuple[Path, Path]:
    return (
        REPO / "research" / "tape" / f"{symbol.lower()}-1h-2025H1",
        REPO / "research" / "tape" / f"{symbol.lower()}-15m-2025H1",
    )


def entry_matched(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    """Campaigns that entered at the identical bar and side in two arms.

    ``C`` and ``D`` can diverge once an earlier exit frees capital, so a raw
    difference mixes a management effect with a portfolio-path effect. Matching on
    ``(open_ns, side)`` isolates the campaigns both arms actually took.
    """
    def index(arm: dict[str, Any]) -> dict[tuple[int, str], dict[str, Any]]:
        rows: dict[tuple[int, str], dict[str, Any]] = {}
        for campaign in arm["campaigns"]:
            if campaign["open"]:
                continue
            key = (int(campaign["open_ns"]), str(campaign["side"]).upper())
            rows.setdefault(key, campaign)
        return rows

    left_rows, right_rows = index(left), index(right)
    shared = sorted(set(left_rows) & set(right_rows))
    matched = [
        {
            "open_ns": key[0],
            "side": key[1],
            "left_pnl": left_rows[key]["realized_pnl"],
            "right_pnl": right_rows[key]["realized_pnl"],
            "left_close_ns": left_rows[key]["close_ns"],
            "right_close_ns": right_rows[key]["close_ns"],
        }
        for key in shared
    ]
    def total(rows: list[dict[str, Any]], key: str) -> float:
        # ``realized_pnl`` is an engine money string such as "16.54 USDT".
        parsed = [parse_money(row[key]) for row in rows if row[key]]
        return sum(float(value) for value in parsed if value is not None)

    return {
        "matched_campaigns": len(matched),
        "left_campaigns": len(left_rows),
        "right_campaigns": len(right_rows),
        "left_pnl_matched": total(matched, "left_pnl") if matched else 0.0,
        "right_pnl_matched": total(matched, "right_pnl") if matched else 0.0,
        "matched_delta": (total(matched, "right_pnl") - total(matched, "left_pnl"))
        if matched
        else 0.0,
        "unmatched_left_only": len(set(left_rows) - set(right_rows)),
        "unmatched_right_only": len(set(right_rows) - set(left_rows)),
        "detail": matched,
    }


def run_symbol(symbol: str, out_dir: Path) -> dict[str, Any]:
    target = out_dir / f"{symbol}.json"
    if target.is_file():
        print(json.dumps({"symbol": symbol, "status": "CACHED"}), flush=True)
        return json.loads(target.read_text())

    h1, m15 = paths(symbol)
    filters = filters_for(symbol)
    arms: dict[str, Any] = {}
    for key, flags in ABLATIONS.items():
        started = time.perf_counter()
        measurement = run_sim_arm(
            tape=h1,
            intraday_tape=m15,
            config=config_for(symbol, **flags),
            instrument_filters=filters,
        )
        payload = derive(measurement)
        payload["wall_s"] = round(time.perf_counter() - started, 2)
        arms[key] = payload
        print(
            json.dumps(
                {
                    "symbol": symbol,
                    "arm": key,
                    "trades": payload["trades_closed"],
                    "net_pnl": round(payload["net_pnl"], 6),
                    "net_r_total": None
                    if payload["net_r_total"] is None
                    else round(payload["net_r_total"], 6),
                    "amendments": payload["amendments"],
                    "violations": payload["protection_violations"],
                }
            ),
            flush=True,
        )

    stress: dict[str, Any] = {}
    base_fee = arms["FULL"]["fee_total_engine"]
    for bps in STRESS_BPS:
        delta = Decimal(bps) / Decimal(10_000)
        measurement = run_sim_arm(
            tape=h1,
            intraday_tape=m15,
            config=config_for(symbol, **ABLATIONS["FULL"]),
            instrument_filters=filters,
            maker_fee=BASE_MAKER + delta,
            taker_fee=BASE_TAKER + delta,
        )
        payload = derive(measurement)
        payload["incremental_bps_per_side"] = bps
        payload["added_cost"] = payload["fee_total_engine"] - base_fee
        stress[f"+{bps}bps"] = payload
        print(
            json.dumps(
                {
                    "symbol": symbol,
                    "stress": f"+{bps}bps",
                    "net_pnl": round(payload["net_pnl"], 6),
                    "added_cost": round(payload["added_cost"], 4),
                }
            ),
            flush=True,
        )

    # Break-even: paths are unchanged by a fee increment (fees never feed a
    # decision except the fixed break-even policy constant), so total cost grows
    # linearly in bps. Break-even increment = net_pnl_0 / (cost per bps).
    net0 = arms["FULL"]["net_pnl"]
    per_bps = None
    for bps in STRESS_BPS:
        added = stress[f"+{bps}bps"]["added_cost"]
        if added > 0:
            per_bps = added / bps
            break
    break_even = None if not per_bps else round(net0 / per_bps, 3)

    document = {
        "symbol": symbol,
        "ablations": arms,
        "stress": stress,
        "break_even_incremental_bps_per_side": break_even,
        "cost_per_bps_per_side": per_bps,
        "entry_matched": {
            "D_vs_C": entry_matched(arms["C_STATIC"], arms["FULL"]),
            "STOP_ONLY_vs_C": entry_matched(arms["C_STATIC"], arms["STOP_ONLY"]),
            "TARGET_ONLY_vs_C": entry_matched(arms["C_STATIC"], arms["TARGET_ONLY"]),
            "BE_ONLY_vs_C": entry_matched(arms["C_STATIC"], arms["BE_ONLY"]),
        },
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(document, indent=1, sort_keys=True))
    return document


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out",
        type=Path,
        default=REPO / "reports" / "v9-cross-market-validation" / "data" / "ablations",
    )
    parser.add_argument("--symbols", nargs="*", default=None)
    args = parser.parse_args()
    started = time.perf_counter()
    for symbol in args.symbols or UNIVERSE:
        run_symbol(symbol, args.out)
    print(json.dumps({"wall_s": round(time.perf_counter() - started, 1)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
