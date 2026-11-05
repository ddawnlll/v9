#!/usr/bin/env python
"""Cross-market four-arm measurement: A/B/C/D on 10 USD-M perpetuals, 2025 H1.

Every arm is measured by the frozen runner
(:mod:`v8_next.evaluation.sim_arm`) against a hash-verified tape; this module only
chooses the universe, sequences the arms one symbol at a time (bounded memory) and
derives the reporting metrics from the native campaign records.

Arms (identical capital, costs, window, execution profile for matched pairs)
    A  V8 expert ensemble, H1 context, fixed 2 %/4 % bracket
    B  V9 TraderDecisionPolicy, legacy single-H1 expert frames, static bracket
    C  V9 policy, real M15 + H1 + completed-H4 context, static bracket
    D  C plus the frozen PositionThesis / structural stop-target / native
       stop-amendment management

Nothing here can submit an order: the only execution object is a NautilusTrader
``BacktestEngine`` inside ``sim_arm``.

Usage
    uv run --locked --extra dev python tools/cross_market_matrix.py \
        --out reports/v9-cross-market-validation/data/matrix [--symbols DOGEUSDT]
"""

from __future__ import annotations

import argparse
import json
import resource
import statistics
import sys
import time
from pathlib import Path
from typing import Any, Callable

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from v8_next.adapters.adaptive_strategy import SimArmConfig  # noqa: E402
from v8_next.evaluation.economic_benchmark import parse_money  # noqa: E402
from v8_next.evaluation.instrument_metadata import filters_for  # noqa: E402
from v8_next.evaluation.sim_arm import run_baseline_arm, run_sim_arm  # noqa: E402

UNIVERSE = [
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
]

INITIAL_BALANCE = 10_000.0
MAKER_FEE = 0.0002
TAKER_FEE = 0.0005
WINDOW_START = "2025-01-01T00:00:00Z"
WINDOW_END_EXCLUSIVE = "2025-07-01T00:00:00Z"
BAR_MS = 3_600_000


def config_for(symbol: str) -> SimArmConfig:
    return SimArmConfig(
        enabled=True,
        instrument_id=f"{symbol}-PERP.BINANCE",
        bar_type_str=f"{symbol}-PERP.BINANCE-1-HOUR-LAST-EXTERNAL",
    )


def arm_definitions(symbol: str) -> list[tuple[str, Callable[..., Any], dict[str, Any]]]:
    cfg = config_for(symbol)
    legacy = SimArmConfig(
        enabled=True,
        instrument_id=cfg.instrument_id,
        bar_type_str=cfg.bar_type_str,
        horizon_profiles_enabled=False,
        adaptive_management_enabled=False,
    )
    static = SimArmConfig(
        enabled=True,
        instrument_id=cfg.instrument_id,
        bar_type_str=cfg.bar_type_str,
        adaptive_management_enabled=False,
    )
    h1 = REPO / "research" / "tape" / f"{symbol.lower()}-1h-2025H1"
    m15 = REPO / "research" / "tape" / f"{symbol.lower()}-15m-2025H1"
    filters = filters_for(symbol)
    return [
        ("A", run_baseline_arm, dict(tape=h1, config=cfg, instrument_filters=filters)),
        ("B", run_sim_arm, dict(tape=h1, config=legacy, instrument_filters=filters)),
        (
            "C",
            run_sim_arm,
            dict(tape=h1, config=static, intraday_tape=m15, instrument_filters=filters),
        ),
        (
            "D",
            run_sim_arm,
            dict(tape=h1, config=cfg, intraday_tape=m15, instrument_filters=filters),
        ),
    ]


def _money(value: str | None) -> float | None:
    """Parse an engine money string; ``None`` stays unmeasured, never zero."""
    if not value:
        return None
    parsed = parse_money(value)
    return None if parsed is None else float(parsed)


def derive(measurement: Any) -> dict[str, Any]:
    """Reporting metrics derived from the native measurement and campaign records."""
    campaigns = list(measurement.campaign_detail)
    closed = [c for c in campaigns if not c["open"]]
    pnls = [_money(c["realized_pnl"]) for c in closed]
    pnls_clean = [p for p in pnls if p is not None]
    gross = sum(pnls_clean) if pnls_clean else 0.0
    wins = [p for p in pnls_clean if p > 0]
    losses = [p for p in pnls_clean if p < 0]
    profit_factor = (
        math_sum(wins) / abs(math_sum(losses)) if losses and math_sum(losses) != 0 else None
    )
    holds = [
        (c["close_ns"] - c["open_ns"]) / 3_600_000_000_000
        for c in closed
        if c["close_ns"] is not None
    ]
    in_market_ns = sum((c["close_ns"] - c["open_ns"]) for c in closed if c["close_ns"])
    notionals = [
        abs(_money(c["quantity"]) * _money(c["entry_px"]))
        for c in campaigns
        if _money(c["quantity"]) is not None and _money(c["entry_px"]) is not None
    ]
    bars = measurement.bars
    window_ns = bars * BAR_MS * 1_000_000
    longs = sum(1 for c in closed if str(c["side"]).upper().endswith("LONG"))
    shorts = sum(1 for c in closed if str(c["side"]).upper().endswith("SHORT"))

    # Equity curve: realized account-independent GROSS price PnL after each closed
    # campaign.``realized_pnl`` from the native position record excludes commissions
    # and funding (those are reported separately and applied at account level), so
    # this curve is comparable across arms but is not the net balance curve.
    equity = INITIAL_BALANCE
    curve = [{"t_ns": 0, "equity": INITIAL_BALANCE}]
    for c in sorted(closed, key=lambda r: r["close_ns"] or 0):
        pnl = _money(c["realized_pnl"])
        if pnl is None:
            continue
        equity += pnl
        curve.append({"t_ns": c["close_ns"], "equity": round(equity, 6)})
    peak = -1e18
    max_dd = 0.0
    for point in curve:
        peak = max(peak, point["equity"])
        if peak > 0:
            max_dd = max(max_dd, (peak - point["equity"]) / peak)

    # Independent gross price PnL, recomputed from the native campaign prices
    # rather than trusting ``Position.realized_pnl`` (which in NautilusTrader
    # 2.0.0rc4 is already net of commissions and funding adjustments).
    gross_independent = 0.0
    gross_unparsable = 0
    for campaign in closed:
        quantity = _money(campaign["quantity"])
        entry = _money(campaign["entry_px"])
        exit_px = _money(campaign.get("close_px"))
        if quantity is None or entry is None or exit_px is None:
            gross_unparsable += 1
            continue
        sign = 1.0 if str(campaign["side"]).upper().endswith("LONG") else -1.0
        gross_independent += sign * quantity * (exit_px - entry)

    fees = measurement.fee_total
    funding = measurement.funding_total
    net = measurement.net_pnl
    # Funding adjustments are signed cash flows on the position: positive is a
    # credit. ``None`` means the tape carried no funding rows -> unmeasured.
    funding_cash = funding
    reconciled = (
        None if funding_cash is None else gross_independent + funding_cash - fees
    )
    residual = None if reconciled is None else net - reconciled

    audits = measurement.protection.get("state") != "NOT_APPLICABLE"
    amendments = _amendment_counts(measurement.orders) if audits else {}

    return {
        "trades_closed": measurement.trades,
        "campaigns_total": len(campaigns),
        "open_campaigns": measurement.open_campaigns,
        "long_trades": longs,
        "short_trades": shorts,
        "net_pnl": net,
        "return_pct": 100.0 * net / INITIAL_BALANCE,
        "gross_price_pnl": gross_independent,
        "gross_price_pnl_engine_net_of_costs": gross,
        "gross_unparsable_campaigns": gross_unparsable,
        "commissions": fees,
        "booked_funding": funding,
        "reconciliation_residual": residual,
        "reconciliation_basis": "net balance delta vs (independent gross price PnL "
        "+ signed funding adjustments - fill commissions); a non-zero residual is "
        "the cash flow of campaigns still open at the cutoff and of any fee charged "
        "on an entry whose exit is outside the window",
        "net_r_mean": measurement.net_r,
        "net_r_total": None
        if measurement.net_r is None
        else measurement.net_r * len(measurement.net_r_samples),
        "net_r_samples": list(measurement.net_r_samples),
        "profit_factor": profit_factor,
        "win_rate": measurement.hit_rate,
        "max_drawdown_pct": None
        if measurement.max_drawdown is None
        else 100.0 * measurement.max_drawdown,
        "max_drawdown_definition": "peak-to-trough of cumulative realized GROSS "
        "campaign PnL (commissions and funding excluded) from the 10,000 USDT "
        "initial balance",
        "time_in_market_pct": None if not window_ns else 100.0 * in_market_ns / window_ns,
        "exposure_notional_avg": statistics.fmean(notionals) if notionals else None,
        "holding_hours_mean": statistics.fmean(holds) if holds else None,
        "holding_hours_median": statistics.median(holds) if holds else None,
        "bars": bars,
        "decisions": measurement.decision_count,
        "run_name": measurement.arm_id,
        "risk_basis": measurement.risk_basis,
        "fee_total_engine": fees,
        "funding_total_engine": funding,
        "funding_coverage": measurement.funding_coverage,
        "protection_state": measurement.protection["state"],
        "protection_violations": len(measurement.protection["violations"]),
        "protection_snapshot": measurement.protection.get("snapshot"),
        # The V8 arm's bracket is not run through the invariant checker: only the
        # V9 arms emit the protection ledger, so A's protection state is
        # NOT_APPLICABLE and its order roles are raw sides, not semantic roles.
        "protection_auditable": measurement.protection.get("state") != "NOT_APPLICABLE",
        "amendments_auditable": measurement.protection.get("state") != "NOT_APPLICABLE",
        "orders": len(measurement.orders),
        "fills": measurement.execution.get("fills_count"),
        "amendments": amendments,
        "trade_digest": measurement.trade_digest,
        "orders_digest": measurement.orders_digest,
        "decisions_digest": measurement.decisions_digest,
        "execution_profile_digest": measurement.execution.get("digest"),
        "unsupported": list(measurement.unsupported),
        "provenance": _jsonable(measurement.provenance),
        "horizon_evidence": _jsonable(measurement.horizon_evidence),
        "equity_curve": curve,
        "campaigns": campaigns,
    }


def math_sum(values: list[float]) -> float:
    total = 0.0
    for value in values:
        total += value
    return total


def _amendment_counts(orders: tuple[dict[str, Any], ...] | list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for order in orders:
        role = str(order.get("role", "UNKNOWN"))
        counts[role] = counts.get(role, 0) + 1
    return counts


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def run_symbol(symbol: str, out_dir: Path) -> dict[str, Any]:
    target = out_dir / f"{symbol}.json"
    if target.is_file():
        print(json.dumps({"symbol": symbol, "status": "CACHED", "file": str(target)}), flush=True)
        return json.loads(target.read_text())

    arms: dict[str, Any] = {}
    for label, runner, kwargs in arm_definitions(symbol):
        started = time.perf_counter()
        measurement = runner(**kwargs)
        wall = time.perf_counter() - started
        payload = derive(measurement)
        payload["wall_s"] = round(wall, 2)
        payload["peak_rss_mib_run"] = round(
            resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024 / 1024, 1
        )
        arms[label] = payload
        print(
            json.dumps(
                {
                    "symbol": symbol,
                    "arm": label,
                    "trades": payload["trades_closed"],
                    "net_pnl": round(payload["net_pnl"], 6),
                    "net_r_total": None
                    if payload["net_r_total"] is None
                    else round(payload["net_r_total"], 6),
                    "violations": payload["protection_violations"],
                    "residual": None
                    if payload["reconciliation_residual"] is None
                    else round(payload["reconciliation_residual"], 9),
                    "wall_s": payload["wall_s"],
                }
            ),
            flush=True,
        )

    document = {
        "symbol": symbol,
        "window_start": WINDOW_START,
        "window_end_exclusive": WINDOW_END_EXCLUSIVE,
        "initial_balance": INITIAL_BALANCE,
        "maker_fee": MAKER_FEE,
        "taker_fee": TAKER_FEE,
        "instrument_filters": _jsonable(filters_for(symbol).__dict__),
        "arms": arms,
        "peak_rss_mib_process": round(
            resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024 / 1024, 1
        ),
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(document, indent=1, sort_keys=True))
    return document


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out",
        type=Path,
        default=REPO / "reports" / "v9-cross-market-validation" / "data" / "matrix",
    )
    parser.add_argument("--symbols", nargs="*", default=None)
    args = parser.parse_args()
    symbols = args.symbols or UNIVERSE
    args.out.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    for symbol in symbols:
        run_symbol(symbol, args.out)
    print(
        json.dumps(
            {
                "symbols": len(symbols),
                "wall_s": round(time.perf_counter() - started, 1),
                "peak_rss_mib_process": round(
                    resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024 / 1024, 1
                ),
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
