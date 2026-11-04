#!/usr/bin/env python
"""Audit-only exporter for the `volume_confirmed_breakout` witness.

This tool does not change strategy code or trading semantics. It runs the
existing 28-expert Nautilus ensemble on a frozen tape, then writes additive
forensic records that the stock run already carries but does not export as
machine-readable files:

  * per-bar ensemble decisions (including all 28 stances);
  * per-bar selected-expert inputs and stance;
  * the final OpportunityBook records;
  * native fills / order events / position events;
  * a manifest with dataset, code and configuration identity.

Usage from the repository root:
  .venv/Scripts/python.exe tools/audit_volume_breakout.py \
    --tape <tape.jsonl> --out <audit-run-dir> --run-id <id>

The caller must use a new output directory. Existing files are refused rather
than overwritten.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

# Allow direct execution from the repository root.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from v8_next.adapters.expert_strategy import (  # noqa: E402
    ExpertStrategyConfig,
    run_expert_strategy_backtest,
)
from v8_next.domain.market import CausalFrame, frame_at  # noqa: E402
from v8_next.economics.decisions import opportunity_at  # noqa: E402
from v8_next.evaluation.multitape import load_multitape  # noqa: E402
from v8_next.experts.breakouts import observe_volume_breakout, volume_variant  # noqa: E402
from v8_next.experts.common import context_reason  # noqa: E402

EXPERT_ID = "volume_confirmed_breakout"
EXPERT_ALIAS = "volume-confirmed-breakout"
EXPECTED_OBSERVER_ID = EXPERT_ID
HOUR_NS = 3_600_000_000_000
DEFAULT_START_MS = 1_735_689_600_000  # 2025-01-01T00:00:00Z
DEFAULT_END_MS = 1_735_689_600_000 + 90 * 24 * 3_600_000  # 2025-04-01T00:00:00Z
DEFAULT_WARMUP_BARS = 96


def _iso(ns: int | None) -> str | None:
    if ns is None:
        return None
    return datetime.fromtimestamp(int(ns) / 1e9, timezone.utc).isoformat().replace("+00:00", "Z")


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _code_tree_sha() -> str:
    """Digest the Python source tree used by this replay (not the whole repo)."""
    root = Path(__file__).resolve().parents[1] / "src" / "v8_next"
    h = hashlib.sha256()
    for path in sorted(root.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        h.update(str(path.relative_to(root)).encode())
        h.update(b"\0")
        h.update(path.read_bytes())
        h.update(b"\0")
    return h.hexdigest()


def _git(*args: str) -> str:
    try:
        result = subprocess.run(
            ["git", *args], capture_output=True, text=True, timeout=10, check=False
        )
        return result.stdout.strip()
    except Exception:
        return "UNKNOWN"


def _write_json(path: Path, payload: Any) -> None:
    if path.exists():
        raise FileExistsError(f"refusing to overwrite {path}")
    path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    if path.exists():
        raise FileExistsError(f"refusing to overwrite {path}")
    with path.open("x", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, sort_keys=True, default=str) + "\n")


def _expert_inputs(frame: CausalFrame, opportunity: Any) -> dict[str, Any] | None:
    """Compute the selected expert's public inputs using the same expressions.

    The values are recomputed for the audit record, then the stock observer is
    called; a disagreement between the recomputation and the stock stance is
    recorded as a forensic finding instead of being silently merged.
    """
    reason = context_reason(frame, opportunity, 21)
    if reason is not None:
        return {
            "context_reason": reason,
            "channel_high": None,
            "channel_low": None,
            "side": None,
            "volume": None,
            "mean20": None,
            "z100": None,
            "proximity100": None,
            "variant": None,
        }
    current = frame.candles[-1]
    high_series = frame.df["high"]
    low_series = frame.df["low"]
    volume_series = frame.df["volume"]
    chan_high = float(high_series.slice(-21, 20).max())
    chan_low = float(low_series.slice(-21, 20).min())
    side = "LONG" if current.close > Decimal(str(chan_high)) else (
        "SHORT" if current.close < Decimal(str(chan_low)) else None
    )
    mean20 = float(volume_series.tail(20).mean())
    z100 = None
    proximity100 = None
    if len(volume_series) >= 100:
        window = volume_series.tail(100)
        sd = float(window.std(ddof=0))
        lo, hi = float(window.min()), float(window.max())
        if sd > 0:
            z100 = (float(volume_series[-1]) - float(window.mean())) / sd
        if hi > lo:
            proximity100 = (float(volume_series[-1]) - lo) / (hi - lo)
    variant = volume_variant(float(volume_series[-1]), mean20, z100, proximity100)
    return {
        "context_reason": None,
        "channel_high": chan_high,
        "channel_low": chan_low,
        "side": side,
        "volume": float(volume_series[-1]),
        "mean20": mean20,
        "z100": z100,
        "proximity100": proximity100,
        "variant": variant,
    }


def _serialise_book(book: Any) -> list[dict[str, Any]]:
    records = book.all() if hasattr(book, "all") else []
    out: list[dict[str, Any]] = []
    for record in records:
        if hasattr(record, "model_dump"):
            out.append(record.model_dump(mode="json"))
        else:
            out.append(dict(record))
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tape", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--start-ms", type=int, default=DEFAULT_START_MS)
    parser.add_argument("--end-ms", type=int, default=DEFAULT_END_MS)
    parser.add_argument("--warmup-bars", type=int, default=DEFAULT_WARMUP_BARS)
    parser.add_argument("--initial-balance", default="10000")
    parser.add_argument("--maker-fee", default="0.0002")
    parser.add_argument("--taker-fee", default="0.0005")
    parser.add_argument("--entry-order-type", default="MARKET")
    args = parser.parse_args(argv)

    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=True)
    if any(out.iterdir()):
        raise SystemExit(f"output directory is not empty: {out}")

    tape = load_multitape(args.tape, start_ms=args.start_ms, end_ms=args.end_ms)
    candles = tape.candles["BTCUSDT"]
    if len(candles) != 2160:
        raise SystemExit(f"unexpected frozen-window bar count: {len(candles)}")
    if candles[-1].end_ns <= candles[0].end_ns:
        raise SystemExit("empty candle range")

    config = ExpertStrategyConfig(
        min_support_quorum=1,
        max_contradiction_tolerance=28,
        bracket_stop_pct=Decimal("0.02"),
        bracket_target_pct=Decimal("0.04"),
        entry_order_type=args.entry_order_type,
    )
    result = run_expert_strategy_backtest(
        candles,
        config,
        maker_fee=Decimal(args.maker_fee),
        taker_fee=Decimal(args.taker_fee),
        initial_balance=Decimal(args.initial_balance),
    )

    candle_by_end = {c.end_ns: c for c in candles}
    warmup_end_ns = candles[min(args.warmup_bars, len(candles) - 1)].end_ns
    expert_rows: list[dict[str, Any]] = []
    ensemble_rows: list[dict[str, Any]] = []
    recompute_mismatches = 0

    for order_index, decision in enumerate(result["decisions"]):
        decision_ns = int(decision["decision_ns"])
        candle = candle_by_end.get(decision_ns)
        if candle is None:
            raise SystemExit(f"decision bar {decision_ns} not found in source candles")
        opportunity = decision.get("opportunity")
        selected = next(
            (
                s
                for s in decision.get("stances", [])
                if s.get("observer_id") == EXPECTED_OBSERVER_ID
            ),
            None,
        )
        if selected is None:
            raise SystemExit(f"selected expert stance missing at {decision_ns}")
        frame = frame_at("BTCUSDT-PERP.BINANCE", decision_ns, candles)
        inputs = _expert_inputs(frame, opportunity_at(frame))
        recomputed = observe_volume_breakout(frame, opportunity_at(frame))
        stock_matches = (
            recomputed.kind.value == selected.get("kind")
            and recomputed.reason == selected.get("reason")
            and recomputed.variant_id == selected.get("variant_id")
        )
        if not stock_matches:
            recompute_mismatches += 1

        common = {
            "run_id": args.run_id,
            "bar_idx": order_index,
            "decision_ns": decision_ns,
            "decision_utc": _iso(decision_ns),
            "warmup": decision_ns < warmup_end_ns,
            "instrument_id": "BTCUSDT-PERP.BINANCE",
            "market": {
                "open": decision.get("open"),
                "high": decision.get("high"),
                "low": decision.get("low"),
                "close": decision.get("close"),
                "volume": str(candle.volume),
                "available_ns": candle.available_ns,
                "available_utc": _iso(candle.available_ns),
            },
            "opportunity": opportunity,
            "expert_inputs": inputs,
            "expert_stance": selected,
            "expert_recompute_matches": stock_matches,
            "ensemble": {
                "support_count": decision.get("support_count"),
                "contradict_count": decision.get("contradict_count"),
                "abstain_count": decision.get("abstain_count"),
                "consensus_supported": decision.get("consensus_supported"),
                "action": decision.get("action"),
                "sizing_mode": decision.get("sizing_mode"),
            },
        }
        expert_rows.append(common)
        ensemble_rows.append(
            {
                **common,
                "stances": decision.get("stances", []),
            }
        )

    book = _serialise_book(result.get("opportunity_book"))
    account = result.get("account", {})
    execution = result.get("execution", {})
    base_manifest = {
        "run_id": args.run_id,
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "mode": "BASELINE_EXISTING_ENSEMBLE",
        "authority": "DIAGNOSTIC_ONLY_NO_ECONOMIC_CLAIM",
        "dataset": {
            "path": str(args.tape.resolve()),
            "sha256": _sha256(args.tape.resolve()),
            "sha1_multitape": tape.tape_sha256,
            "instrument": "BTCUSDT",
            "bar_interval": "1h",
            "start_ms": args.start_ms,
            "end_ms": args.end_ms,
            "bars": len(candles),
            "first_bar_end_ns": candles[0].end_ns,
            "last_bar_end_ns": candles[-1].end_ns,
            "funding_rows": len(tape.funding),
            "funding_dropped": tape.funding_dropped,
            "mark_price_absent": tape.mark_price_absent,
        },
        "code": {
            "git_head": _git("rev-parse", "HEAD"),
            "git_branch": _git("branch", "--show-current"),
            "git_dirty": bool(_git("status", "--porcelain")),
            "v8_next_source_sha256": _code_tree_sha(),
        },
        "config": {
            "strategy": "ExpertEnsembleStrategy",
            "grammar_policy": config.grammar_policy,
            "min_support_quorum": config.min_support_quorum,
            "max_contradiction_tolerance": config.max_contradiction_tolerance,
            "bracket_stop_pct": str(config.bracket_stop_pct),
            "bracket_target_pct": str(config.bracket_target_pct),
            "entry_order_type": config.entry_order_type,
            "execution_algo": config.execution_algo,
            "order_quantity": str(config.order_quantity),
            "max_concurrent_positions": config.max_concurrent_positions,
            "initial_balance": args.initial_balance,
            "maker_fee": args.maker_fee,
            "taker_fee": args.taker_fee,
            "execution_profile": None,
            "slippage": "UNSPECIFIED_NO_EXPLICIT_PROFILE",
            "funding": "NATIVE_IF_VALID_MARK_AVAILABLE_ELSE_ABSENT",
        },
        "warmup": {
            "bars": args.warmup_bars,
            "warmup_end_ns": warmup_end_ns,
            "warmup_end_utc": _iso(warmup_end_ns),
        },
        "counts": {
            "decisions": len(result["decisions"]),
            "expert_rows": len(expert_rows),
            "expert_recompute_mismatches": recompute_mismatches,
            "opened_positions": len(result.get("opened_positions", [])),
            "closed_positions": len(result.get("closed_positions", [])),
            "order_events": len(result.get("order_events", [])),
            "fills": len(result.get("fill_records", [])),
            "opportunity_book_records": len(book),
        },
        "account": account,
        "execution": execution,
        "limitations": [
            "Native Nautilus simulated fills, not venue settlement.",
            "OHLCV tape has no mark price, quotes, trades, order book or historical exchange limits.",
            "No explicit execution profile was selected; slippage semantics are unspecified.",
            "Expert-only PnL is not produced by this baseline exporter.",
        ],
    }

    _write_jsonl(out / "expert_decisions.jsonl", expert_rows)
    _write_jsonl(out / "ensemble_decisions.jsonl", ensemble_rows)
    _write_jsonl(out / "opportunity_book_records.jsonl", book)
    _write_jsonl(
        out / "orders_and_fills.jsonl",
        [
            *({"record_type": "order_event", **row} for row in result.get("order_events", [])),
            *({"record_type": "fill", **row} for row in result.get("fill_records", [])),
            *({"record_type": "position_opened", **row} for row in result.get("opened_positions", [])),
            *({"record_type": "position_closed", **row} for row in result.get("closed_positions", [])),
        ],
    )
    _write_json(
        out / "baseline_account_and_execution.json",
        {
            "run_id": args.run_id,
            "account": account,
            "execution": execution,
            "opened_positions": result.get("opened_positions", []),
            "closed_positions": result.get("closed_positions", []),
            "order_events": result.get("order_events", []),
            "fill_records": result.get("fill_records", []),
            "opportunity_book": book,
        },
    )
    _write_json(out / "run_manifest.json", base_manifest)
    print(
        json.dumps(
            {
                "run_id": args.run_id,
                "bars": len(candles),
                "decisions": len(result["decisions"]),
                "opened": len(result.get("opened_positions", [])),
                "closed": len(result.get("closed_positions", [])),
                "fills": len(result.get("fill_records", [])),
                "recompute_mismatches": recompute_mismatches,
                "output": str(out),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
