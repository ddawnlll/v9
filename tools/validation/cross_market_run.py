#!/usr/bin/env python
"""Cross-market validation runner for the frozen V9 candidate.

This tool is validation-only. It never places an order outside a NautilusTrader
``BacktestEngine``, never reads credentials and never changes a strategy signal,
threshold, stop, target or sizing formula. It drives the frozen arms through the
same ``sim_arm`` internals used by the documented BTC baseline and writes one
JSON evidence file per symbol.

Arms
----
A  V8 expert ensemble, H1 context, fixed 2 %/4 % bracket.
B  V9 policy, H1 context, fixed bracket, no thesis lifecycle.
C  V9 policy, real M15+H1+completed-H4 context, fixed bracket.
D  V9 policy, multi-horizon context, structural entry geometry + thesis
   lifecycle + native stop/target amendments.
A1..A6 research-only ablations of D (documented in the report).

Usage:
    PYTHONPATH=src python tools/validation/cross_market_run.py --symbol ETHUSDT
"""

from __future__ import annotations

import argparse
import json
import resource
import sys
import time
from decimal import Decimal
from pathlib import Path
from typing import Any

from v8_next.adapters.adaptive_strategy import AdaptiveSimStrategy, SimArmConfig
from v8_next.adapters.expert_strategy import ExpertEnsembleStrategy
from v8_next.evaluation.ablations import BASELINE_ARM_ID, V9_INTERVENTION_KIND
from v8_next.evaluation.economic_benchmark import pair_positions, parse_money
from v8_next.evaluation.instrument_metadata import InstrumentFilters, filters_for
from v8_next.evaluation.multitape import load_multitape
from v8_next.evaluation.sim_arm import (
    _authorize_tape,
    _drive,
    _load_intraday_candles,
    _manifest_sha,
    _measure,
    _slice_identity,
    _window,
)

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

#: Default account per symbol/experiment. Independent books, never one portfolio.
INITIAL_BALANCE = Decimal("10000")
MAKER_FEE = Decimal("0.0002")
TAKER_FEE = Decimal("0.0005")
EXECUTION_PROFILE = "baseline"
HOLD_NS = 3_600_000_000_000


class _ExactCostCapture:
    """Validation-only observer: exact funding/commission per closed position.

    The frozen ``_measure`` reads ``engine.cache.positions()`` after the run.
    In a NETTING replay that cache retains only live positions, so a multi-trade
    run undercounts funding on closed campaigns. This mixin reads each position
    while ``on_position_closed`` still has it in cache. It changes no decision.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.captured_funding = Decimal("0")
        self.captured_commissions = Decimal("0")
        self.captured_position_ids: set[str] = set()

    def on_position_closed(self, event: Any) -> None:
        position_id = str(getattr(event, "position_id", ""))
        position = self.cache.position(event.position_id)
        if position is not None and position_id:
            self.captured_position_ids.add(position_id)
            for adjustment in position.adjustments():
                if str(adjustment.adjustment_type).upper() != "FUNDING":
                    continue
                parsed = parse_money(str(adjustment.pnl_change))
                if parsed is not None:
                    self.captured_funding += parsed
            for commission in position.commissions():
                parsed = parse_money(str(commission))
                if parsed is not None:
                    self.captured_commissions += parsed
        super().on_position_closed(event)

    def finalize_cost_capture(self) -> None:
        """Include funding already accrued on positions still open at cutoff."""
        for position in self.cache.positions():
            position_id = str(getattr(position, "id", ""))
            if position_id in self.captured_position_ids or position.is_closed:
                continue
            for adjustment in position.adjustments():
                if str(adjustment.adjustment_type).upper() != "FUNDING":
                    continue
                parsed = parse_money(str(adjustment.pnl_change))
                if parsed is not None:
                    self.captured_funding += parsed


class ValidationAdaptiveStrategy(_ExactCostCapture, AdaptiveSimStrategy):
    pass


class ValidationEnsembleStrategy(_ExactCostCapture, ExpertEnsembleStrategy):
    pass


def _norm_side(record: dict[str, Any]) -> str:
    side = str(record.get("side", "")).upper()
    if "LONG" in side or side == "BUY":
        return "LONG"
    if "SHORT" in side or side == "SELL":
        return "SHORT"
    return "UNKNOWN"


def _config(
    symbol: str,
    *,
    horizons: bool,
    adaptive: bool,
    stop_amend: bool = True,
    target_amend: bool = True,
    structural_stop: bool = True,
    structural_target: bool = True,
) -> SimArmConfig:
    return SimArmConfig(
        enabled=True,
        instrument_id=f"{symbol}-PERP.BINANCE",
        bar_type_str=f"{symbol}-PERP.BINANCE-1-HOUR-LAST-EXTERNAL",
        horizon_profiles_enabled=horizons,
        adaptive_management_enabled=adaptive,
        stop_amendments_enabled=stop_amend,
        target_amendments_enabled=target_amend,
        structural_stop_enabled=structural_stop,
        structural_target_enabled=structural_target,
    )


def _run_one(
    *,
    arm: str,
    symbol: str,
    tape: Path,
    intraday: Path | None,
    filters: InstrumentFilters | None,
    config: SimArmConfig,
    baseline: bool,
) -> dict[str, Any]:
    started = time.perf_counter()
    before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    jsonl, tape_sha = _authorize_tape(tape, family="1h")
    slice_symbol, _, slice_window = _slice_identity(jsonl.parent)
    loaded = load_multitape(jsonl)
    window = _window(loaded, config, None)
    intraday_candles = ()
    intraday_sha = ""
    if intraday is not None:
        intraday_candles, intraday_sha = _load_intraday_candles(
            intraday, config, symbol=slice_symbol, window=slice_window
        )
    if baseline:
        strategy: Any = ValidationEnsembleStrategy(
            config.baseline_config(),
            source_candles={candle.end_ns: candle for candle in window.candles},
        )
    else:
        strategy = ValidationAdaptiveStrategy(
            config,
            source_candles={candle.end_ns: candle for candle in window.candles},
            intraday_candles=intraday_candles,
        )
    result = _drive(
        window=window,
        strategy=strategy,
        funding_dropped_at_load=loaded.funding_dropped,
        initial_balance=INITIAL_BALANCE,
        maker_fee=MAKER_FEE,
        taker_fee=TAKER_FEE,
        execution_profile=EXECUTION_PROFILE,
        instrument_filters=filters,
    )
    strategy.finalize_cost_capture()
    measurement = _measure(
        arm_id=BASELINE_ARM_ID if baseline else f"v9_{arm.lower()}",
        arm_kind=None if baseline else V9_INTERVENTION_KIND,
        strategy=strategy,
        result=result,
        window=window,
        config=config,
        tape_sha256=tape_sha,
        intraday_sha256=intraday_sha,
        tape_dir=jsonl.parent,
        initial_balance=INITIAL_BALANCE,
        maker_fee=MAKER_FEE,
        taker_fee=TAKER_FEE,
        execution_profile=EXECUTION_PROFILE,
    )
    wall_s = time.perf_counter() - started
    peak_kb = max(before, resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)

    opened = [dict(row) for row in strategy.opened_positions]
    closed = [dict(row) for row in strategy.closed_positions]
    pairs = pair_positions(opened, closed)
    holding_ns: list[int] = []
    long_n = short_n = 0
    for open_record, close_record in pairs:
        if _norm_side(open_record) == "LONG":
            long_n += 1
        elif _norm_side(open_record) == "SHORT":
            short_n += 1
        if close_record is not None:
            start = int(open_record.get("event_ns", 0) or 0)
            end = int(close_record.get("event_ns", 0) or 0)
            if end >= start:
                holding_ns.append(end - start)

    net_pnl = Decimal(str(measurement.net_pnl))
    #: PositionClosed.realized_pnl is already net of commissions/funding (it
    #: equals the account balance delta on every arm). Keep the native account
    #: delta as authoritative and derive the gross price leg explicitly.
    fees = strategy.captured_commissions
    funding = strategy.captured_funding
    gross_price_pnl = net_pnl + fees - funding
    funding_cache_undercount = (
        None
        if measurement.funding_total is None
        else Decimal(str(measurement.funding_total)) - funding
    )

    # One-way traded notional, for the post-hoc cost stress. This is measured
    # from native fills' price/quantity, not assumed from turnover.
    one_way_notional = Decimal("0")
    price_gross_reconstructed = Decimal("0")
    for open_record, close_record in pairs:
        qty = parse_money(open_record.get("quantity"))
        entry = parse_money(open_record.get("avg_px_open"))
        if qty is not None and entry is not None:
            one_way_notional += abs(qty * entry)
        if close_record is not None:
            close_px = parse_money(close_record.get("avg_px_close"))
            if qty is not None and close_px is not None:
                one_way_notional += abs(qty * close_px)
            if qty is not None and entry is not None and close_px is not None:
                direction = 1 if _norm_side(open_record) == "LONG" else -1
                price_gross_reconstructed += Decimal(direction) * (close_px - entry) * qty

    # Engine order status census (native, not inferred).
    account = result["account"]
    status_counts: dict[str, int] = {}
    for order in account.get("orders") or []:
        status = str(order.get("status", "UNKNOWN")).upper()
        status_counts[status] = status_counts.get(status, 0) + 1

    orphan_closes = sum(
        1
        for row in closed
        if str(row.get("position_id")) not in {str(o.get("position_id")) for o in opened}
    )

    pnls = [float(parse_money(row.get("realized_pnl")) or 0) for row in closed]
    gross_profit = sum(p for p in pnls if p > 0)
    gross_loss = abs(sum(p for p in pnls if p < 0))
    profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else None

    lifecycle = (
        [dict(row) for row in strategy.lifecycle_events]
        if isinstance(strategy, AdaptiveSimStrategy)
        else []
    )
    kind_counts: dict[str, int] = {}
    for event in lifecycle:
        kind = str(event.get("kind", "UNKNOWN"))
        kind_counts[kind] = kind_counts.get(kind, 0) + 1

    management = (
        [dict(row) for row in strategy.management_log]
        if isinstance(strategy, AdaptiveSimStrategy)
        else []
    )
    action_counts: dict[str, int] = {}
    for receipt in management:
        action = str(receipt.get("action", "UNKNOWN"))
        action_counts[action] = action_counts.get(action, 0) + 1

    ledger = strategy.order_ledger() if isinstance(strategy, AdaptiveSimStrategy) else {}
    decisions = list(ledger.get("decisions", []))
    orders = list(ledger.get("orders", []))
    unlinked_orders = sum(1 for order in orders if order.get("decision_id") in (None, ""))

    desired_hold = sum(holding_ns) / HOLD_NS
    total_window_h = len(window.candles)
    open_positions = measurement.open_campaigns
    open_notional = None
    if open_positions:
        # Engine does not supply a mark source in this tape; report the fact.
        open_notional = "MARK_TO_MARKET_UNAVAILABLE_NO_MARK_SOURCE"

    execution = dict(measurement.execution)
    return {
        "arm": arm,
        "symbol": symbol,
        "config": {
            "instrument_id": config.instrument_id,
            "bar_type_str": config.bar_type_str,
            "horizon_profiles_enabled": config.horizon_profiles_enabled,
            "adaptive_management_enabled": config.adaptive_management_enabled,
            "stop_amendments_enabled": config.stop_amendments_enabled,
            "target_amendments_enabled": config.target_amendments_enabled,
            "structural_stop_enabled": config.structural_stop_enabled,
            "structural_target_enabled": config.structural_target_enabled,
            "bracket_stop_pct": str(config.bracket_stop_pct),
            "bracket_target_pct": str(config.bracket_target_pct),
            "min_support_quorum": config.min_support_quorum,
            "max_contradiction_tolerance": config.max_contradiction_tolerance,
            "target_notional": str(config.target_notional),
            "initial_balance": str(INITIAL_BALANCE),
        },
        "bars_h1": len(window.candles),
        "bars_m15": len(intraday_candles),
        "window_start_ns": window.candles[0].start_ns,
        "window_end_ns": window.candles[-1].end_ns,
        "metrics": {
            "trades": measurement.trades,
            "net_pnl": measurement.net_pnl,
            "return_pct": 100.0 * measurement.net_pnl / float(INITIAL_BALANCE),
            "net_r_mean": measurement.net_r,
            "net_r_total": (
                float(sum(measurement.net_r_samples)) if measurement.net_r_samples else None
            ),
            "net_r_samples": list(measurement.net_r_samples),
            "max_drawdown": measurement.max_drawdown,
            "max_drawdown_basis": "closed-campaign realized equity, peak-to-trough, before open MTM",
            "hit_rate": measurement.hit_rate,
            "fee_total": measurement.fee_total,
            "funding_total": float(funding),
            "funding_total_cache_based_frozen_measure": measurement.funding_total,
            "funding_cache_undercount": (
                None if funding_cache_undercount is None else float(funding_cache_undercount)
            ),
            "gross_price_pnl": float(gross_price_pnl),
            "price_pnl_reconstructed_from_positions": float(price_gross_reconstructed),
            "price_reconstruction_residual": float(price_gross_reconstructed - gross_price_pnl),
            "reconciliation_basis": (
                "net_pnl is the native account delta; gross_price_pnl = net_pnl + "
                "captured_commissions - captured_funding; PositionClosed.realized_pnl "
                "is net of both and must equal net_pnl"
            ),
            "closed_realized_vs_net_residual": float(
                net_pnl
                - sum(
                    (parse_money(row.get("realized_pnl")) or Decimal("0"))
                    for row in closed
                )
            ),
            "open_campaigns": open_positions,
            "open_notional": open_notional,
            "initial_equity": float(INITIAL_BALANCE),
            "final_realized_equity": float(INITIAL_BALANCE + Decimal(str(measurement.net_pnl))),
            "long_trades": long_n,
            "short_trades": short_n,
            "holding_hours_mean": (desired_hold / len(holding_ns)) if holding_ns else None,
            "holding_hours_median": (
                sorted(holding_ns)[len(holding_ns) // 2] / HOLD_NS if holding_ns else None
            ),
            "time_in_market_hours": desired_hold,
            "window_hours": total_window_h,
            "exposure_pct": (100.0 * desired_hold / total_window_h) if total_window_h else None,
            "profit_factor": profit_factor,
            "campaigns_closed": len(closed),
            "one_way_traded_notional": float(one_way_notional),
            "breakeven_incremental_bps_per_side": (
                (measurement.net_pnl / float(one_way_notional) * 10_000.0)
                if one_way_notional > 0
                else None
            ),
        },
        "execution": execution,
        "protection": dict(measurement.protection),
        "order_status_counts": status_counts,
        "protection_event_counts": kind_counts,
        "management_action_counts": action_counts,
        "decision_count": len(decisions),
        "order_count": len(orders),
        "unlinked_orders": unlinked_orders,
        "orphan_closes": orphan_closes,
        "lifecycle_event_counts": kind_counts,
        "horizon_evidence": dict(measurement.horizon_evidence),
        "positions": {"opened": opened, "closed": closed},
        "unsupported": list(measurement.unsupported),
        "wall_seconds": wall_s,
        "process_peak_rss_kb": peak_kb,
        "provenance": {
            "tape_sha256": measurement.provenance.tape_sha256,
            "intraday_tape_sha256": measurement.provenance.intraday_tape_sha256,
            "tape_manifest_sha256": _manifest_sha(jsonl.parent),
            "config_sha256": measurement.provenance.config_sha256,
            "execution_profile": measurement.provenance.execution_profile,
            "execution_profile_digest": measurement.provenance.execution_profile_digest,
            "engine": measurement.provenance.engine,
            "engine_version": measurement.provenance.engine_version,
            "funding_basis": measurement.provenance.funding_basis,
            "mark_basis": measurement.provenance.mark_basis,
        },
    }



def build_arms(symbol: str) -> list[tuple[str, bool, SimArmConfig, str | None]]:
    """Return (arm, baseline, config, intraday_suffix)."""
    return [
        ("A", True, _config(symbol, horizons=False, adaptive=False), None),
        ("B", False, _config(symbol, horizons=False, adaptive=False), None),
        ("C", False, _config(symbol, horizons=True, adaptive=False), "15m"),
        ("D", False, _config(symbol, horizons=True, adaptive=True), "15m"),
        (
            "A1_no_amend",
            False,
            _config(symbol, horizons=True, adaptive=True, stop_amend=False, target_amend=False),
            "15m",
        ),
        (
            "A2_stop_amend_only",
            False,
            _config(symbol, horizons=True, adaptive=True, stop_amend=True, target_amend=False),
            "15m",
        ),
        (
            "A3_target_amend_only",
            False,
            _config(symbol, horizons=True, adaptive=True, stop_amend=False, target_amend=True),
            "15m",
        ),
        (
            "A4_structural_stop_only",
            False,
            _config(
                symbol,
                horizons=True,
                adaptive=True,
                stop_amend=False,
                target_amend=False,
                structural_stop=True,
                structural_target=False,
            ),
            "15m",
        ),
        (
            "A5_structural_target_only",
            False,
            _config(
                symbol,
                horizons=True,
                adaptive=True,
                stop_amend=False,
                target_amend=False,
                structural_stop=False,
                structural_target=True,
            ),
            "15m",
        ),
        (
            "A6_fixed_geometry",
            False,
            _config(
                symbol,
                horizons=True,
                adaptive=True,
                stop_amend=False,
                target_amend=False,
                structural_stop=False,
                structural_target=False,
            ),
            "15m",
        ),
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--symbol", required=True, choices=SYMBOLS)
    parser.add_argument("--tape-root", type=Path, default=Path("research/tape"))
    parser.add_argument("--out", type=Path, default=Path("reports/v9-cross-market-validation/raw"))
    parser.add_argument("--arms", nargs="*", default=None, help="subset of arm ids")
    args = parser.parse_args(argv)
    symbol = args.symbol
    lower = symbol.lower()
    tape = args.tape_root / f"{lower}-1h-2025H1"
    intraday = args.tape_root / f"{lower}-15m-2025H1"
    if not tape.is_dir():
        raise SystemExit(f"missing verified H1 tape: {tape}")
    filters = None if symbol == "BTCUSDT" else filters_for(symbol)
    out_path = args.out / f"{symbol}.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)

    payload: dict[str, Any] = {
        "schema_version": "v9-cross-market-validation-arm-v1",
        "symbol": symbol,
        "instrument_filters": None if filters is None else filters.__dict__ | {
            "price_increment": str(filters.price_increment),
            "size_increment": str(filters.size_increment),
            "min_quantity": str(filters.min_quantity),
            "max_quantity": str(filters.max_quantity),
            "min_notional": str(filters.min_notional),
            "maintenance_margin": str(filters.maintenance_margin),
        },
        "arms": {},
    }
    for arm, baseline, config, suffix in build_arms(symbol):
        if args.arms and arm not in args.arms:
            continue
        arm_intraday = intraday if suffix == "15m" else None
        if arm_intraday is not None and not arm_intraday.is_dir():
            payload["arms"][arm] = {"arm": arm, "status": "NOT_RUN", "reason": f"missing {arm_intraday}"}
            continue
        sys.stderr.write(f"[{symbol}] {arm} ...\n")
        sys.stderr.flush()
        record = _run_one(
            arm=arm,
            symbol=symbol,
            tape=tape,
            intraday=arm_intraday,
            filters=filters,
            config=config,
            baseline=baseline,
        )
        record["status"] = "MEASURED"
        payload["arms"][arm] = record
        m = record["metrics"]
        sys.stderr.write(
            f"[{symbol}] {arm} trades={m['trades']} pnl={m['net_pnl']:.4f} "
            f"r={m['net_r_mean']} dd={m['max_drawdown']} wall={record['wall_seconds']:.1f}s\n"
        )
        sys.stderr.flush()
        out_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    out_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    print(out_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
