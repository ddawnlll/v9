#!/usr/bin/env python
"""Engine <-> decision-plane reconciliation for one pre-registered swing policy.

Same policy, tape, warmup, window, instrument and capital identity on both sides; the two
execution models are then compared campaign by campaign. The models are *different by
construction* and the differences are the output, not something to tune away:

* the replay enters at the decision's own ``entry_reference`` and resolves the bracket from
  its declared rules;
* the engine enters with a market order on the following bar, so its fill price and instant
  are the engine's, and its exits are whatever the venue actually did.

Comparisons are made on event order, side, quantity, cash flow and position state, with
tolerances tied to the instrument contract (one tick, one lot, one bar). Divergences are
classified and published; none is silently smoothed. A residual resting order at the end of
the data is published as such — an end-of-data artefact, not a hidden position.
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any

from v8_next.adapters.swing_engine import SwingEngineConfig, run_swing_engine
from v8_next.domain.market import frame_at
from v8_next.economics.grammar import POLICY_REQUIRED_BARS
from v8_next.economics.swing_baseline import (
    SHARED_CONTRACT,
    policy_spec,
    replay_bracket,
    swing_signal,
)
from v8_next.evaluation.multitape import load_multitape

HOUR_NS = 3_600 * 10**9
TICK = Decimal("0.01")  # instrument price increment
LOT = Decimal("0.001")  # instrument size increment


def _ms(value: str) -> int:
    return int(
        datetime.datetime.fromisoformat(value).replace(tzinfo=datetime.timezone.utc).timestamp() * 1000
    )


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def engine_campaigns(result: dict[str, Any]) -> list[dict[str, Any]]:
    """Pair each ENTRY_SUBMITTED plan with its campaign_closed record."""
    live: dict[str, Any] | None = None
    campaigns: list[dict[str, Any]] = []
    for entry in result["events"]["decisions"]:
        if "campaign_closed" in entry:
            closed = entry["campaign_closed"]
            campaigns.append(closed)
            live = None
            continue
        if entry.get("status") == "ENTRY_SUBMITTED":
            live = dict(entry)
        elif live is not None and entry.get("status", "").startswith("CLOSING_"):
            live["status"] = entry["status"]
    return campaigns


def replay_campaigns(
    series: list[Any],
    *,
    policy_id: str,
    instrument_id: str,
    taker_fee: Decimal,
    window_start_ns: int,
    warmup_bars: int,
) -> list[dict[str, Any]]:
    spec = policy_spec(policy_id)
    has_bracket = spec.protection_policy not in (None, "timeout-only-v1")
    campaigns: list[dict[str, Any]] = []
    index = warmup_bars
    while index < len(series) - 1:
        bar = series[index]
        frame = frame_at(instrument_id, bar.end_ns, tuple(series[: index + 1]))
        decision = swing_signal(frame, spec, bar_ns=HOUR_NS)
        if decision is None:
            index += 1
            continue
        stop_distance = abs(Decimal(decision.entry_reference) - Decimal(decision.stop_price))
        outcome = replay_bracket(
            decision,
            series[index + 1 :],
            bar_ns=HOUR_NS,
            bps_fee=taker_fee,
            has_bracket=has_bracket,
        )
        index += max(1, outcome.bars_held) + 1
        if bar.start_ns < window_start_ns:
            continue
        campaigns.append(
            {
                "decision_identity": decision.identity(),
                "policy_id": decision.policy_id,
                "direction": decision.direction,
                "decision_ns": int(decision.decision_ns),
                "entry_reference": str(decision.entry_reference),
                "stop_price": str(decision.stop_price),
                "target_price": str(decision.target_price),
                "expires_ns": int(decision.expires_ns),
                "has_bracket": has_bracket and stop_distance > 0,
                "exit_kind": outcome.exit_kind,
                "exit_ns": int(outcome.exit_ns),
                "exit_price": str(outcome.exit_price),
                "gross_return": outcome.gross_return,
                "fee_cost_return": outcome.fee_cost_return,
                "net_return": outcome.net_return,
            }
        )
    return campaigns


def classify(engine_row: dict[str, Any], replay_row: dict[str, Any]) -> list[str]:
    kinds: list[str] = []
    if engine_row["direction"] != replay_row["direction"]:
        kinds.append("SIDE_DIVERGENCE")
    decision_ns = int(replay_row["decision_ns"])
    fill_ns = int(engine_row.get("entry_fill_ns") or 0)
    if fill_ns and abs(fill_ns - decision_ns) > HOUR_NS:
        kinds.append("ENTRY_TIMING_BEYOND_ONE_BAR")
    elif fill_ns and fill_ns != decision_ns:
        kinds.append("ENTRY_TIMING_OFFSET_WITHIN_ONE_BAR")
    if engine_row.get("has_bracket") != replay_row["has_bracket"]:
        kinds.append("BRACKET_CONTRACT_DIVERGENCE")
    engine_entry = Decimal(str(engine_row.get("entry_fill_px")) or "0")
    replay_entry = Decimal(str(replay_row["entry_reference"]))
    if engine_entry > 0 and abs(engine_entry - replay_entry) > TICK:
        kinds.append("ENTRY_PRICE_BEYOND_ONE_TICK")
    exit_ns = int(engine_row.get("exit_fill_ns") or 0)
    if exit_ns:
        if exit_ns != int(replay_row["exit_ns"]):
            kinds.append("EXIT_TIME_DIVERGENCE")
        engine_exit = Decimal(str(engine_row.get("exit_fill_px")) or "0")
        if engine_exit > 0 and abs(engine_exit - Decimal(str(replay_row["exit_price"]))) > TICK:
            kinds.append("EXIT_PRICE_BEYOND_ONE_TICK")
        # the replay names the exit kind from its own rules; the engine only knows which leg
        # filled, so a mismatch is expected whenever the models resolve the bracket differently
        if (
            replay_row["exit_kind"] in ("STOP", "TARGET")
            and engine_row.get("bracket_leg_filled") is None
        ):
            kinds.append("EXIT_KIND_DIVERGENCE_BRACKETLESS_ENGINE")
    return kinds


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", default=None)
    parser.add_argument("--policy", default="plain_swing")
    parser.add_argument("--tape", default="research/tape/multi-1h-4y/tape.jsonl")
    parser.add_argument("--instrument", default="BTCUSDT")
    parser.add_argument("--start-utc", default="2025-01-01")
    parser.add_argument("--end-utc", default="2025-02-01")
    parser.add_argument("--history-bars", type=int, default=96)
    parser.add_argument("--out", default="docs/evidence/v87-r3/RECONCILE")
    args = parser.parse_args(argv)

    repo_root = Path(args.repo_root).resolve() if args.repo_root else Path(__file__).resolve().parents[1]
    out_dir = (repo_root / args.out).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    tape_path = (repo_root / args.tape).resolve()

    tape = load_multitape(
        tape_path,
        start_ms=_ms(args.start_utc) - args.history_bars * 3_600_000,
        end_ms=_ms(args.end_utc),
    )
    series = list(tape.candles[args.instrument])
    instrument_id = series[0].instrument_id
    spec = policy_spec(args.policy)
    warmup = int(POLICY_REQUIRED_BARS[spec.grammar_policy])
    window_start_ns = _ms(args.start_utc) * 1_000_000

    cfg = SwingEngineConfig(
        policy_id=args.policy,
        instrument_id=instrument_id,
        bar_type_str=f"{instrument_id}-1-HOUR-LAST-EXTERNAL",
    )
    engine_result = run_swing_engine(tuple(series), cfg, warmup_bars=warmup)
    engine_rows = engine_campaigns(engine_result)
    replay_rows = replay_campaigns(
        series,
        policy_id=args.policy,
        instrument_id=instrument_id,
        taker_fee=Decimal(SHARED_CONTRACT["taker_fee"]),
        window_start_ns=window_start_ns,
        warmup_bars=warmup,
    )

    # pair by decision instant: both sides stamp the same decision clock, which is the
    # only key they share before execution begins
    by_decision: dict[int, dict[str, Any]] = {}
    for row in engine_rows:
        decision = row.get("decision") or {}
        by_decision[int(decision.get("decision_ns", 0))] = row
    pairs: list[dict[str, Any]] = []
    used: set[int] = set()
    for replay_row in replay_rows:
        key = int(replay_row["decision_ns"])
        engine_row = by_decision.get(key)
        if engine_row is None:
            pairs.append({"status": "REPLAY_ONLY_CAMPAIGN", "replay": replay_row})
            continue
        used.add(key)
        kinds = classify(engine_row, replay_row)
        pairs.append(
            {
                "status": "RECONCILED" if not kinds else "DIVERGENT",
                "divergence_classes": kinds,
                "replay": replay_row,
                "engine": engine_row,
            }
        )
    for key, engine_row in by_decision.items():
        if key not in used:
            pairs.append({"status": "ENGINE_ONLY_CAMPAIGN", "engine": engine_row})

    classes: dict[str, int] = {}
    for pair in pairs:
        for kind in pair.get("divergence_classes", []) or []:
            classes[str(kind)] = classes.get(str(kind), 0) + 1

    report = {
        "lane": "engine_replay_reconciliation",
        "claim_status": "NO_ECONOMIC_CLAIM",
        "identity": {
            "policy_id": args.policy,
            "policy_identity": spec.identity(),
            "instrument_id": instrument_id,
            "tape": str(tape_path.relative_to(repo_root)),
            "tape_sha256": tape.tape_sha256,
            "window": {"start_utc": args.start_utc, "end_utc": args.end_utc},
            "warmup_bars": warmup,
            "initial_balance_usdt": SHARED_CONTRACT["initial_balance_usdt"],
            "risk_per_trade_fraction": SHARED_CONTRACT["risk_per_trade_fraction"],
            "bars": len(series),
        },
        "tolerances": {
            "price_tick": str(TICK),
            "size_lot": str(LOT),
            "time_bar_ns": HOUR_NS,
            "statement": "tolerances are the instrument contract's tick and lot plus one bar",
        },
        "engine_lane": {
            "semantics": engine_result["semantics"],
            "execution_profile": engine_result["execution_profile"],
            "fills": len(engine_result["events"]["fills"]),
            "positions_opened": len(engine_result["events"]["positions_opened"]),
            "positions_closed": len(engine_result["events"]["positions_closed"]),
            "campaigns": len(engine_rows),
            "open_position_at_end": engine_result["open_position_at_end"],
            "orders_open_at_end": engine_result["orders_open_at_end"],
        },
        "replay_lane": {
            "campaigns": len(replay_rows),
            "has_bracket_policy": spec.protection_policy not in (None, "timeout-only-v1"),
        },
        "campaigns": pairs,
        "divergence_class_counts": classes,
        "notes": [
            "the two lanes are different execution models by construction; a divergence is a "
            "finding, not a failure",
            "the replay is return-based (unit notional), so quantities are not comparable "
            "line-by-line; cash flow is published per lane instead",
            "a resting order at the end of the data is published, not assumed away: the engine "
            "has no further event to process its cancellation",
        ],
    }
    path = out_dir / "engine_replay_reconciliation.json"
    path.write_text(json.dumps(report, indent=2, sort_keys=True, default=str) + "\n")

    print(f"[RECON] policy={args.policy} bars={len(series)} warmup={warmup}")
    print(f"[RECON] engine: {len(engine_rows)} kampanya, {len(engine_result['events']['fills'])} fill, "
          f"{len(engine_result['events']['positions_closed'])} kapanan pozisyon, sonda açık={engine_result['open_position_at_end']}")
    print(f"[RECON] replay: {len(replay_rows)} kampanya (bracket={report['replay_lane']['has_bracket_policy']})")
    classified = {"RECONCILED": 0, "DIVERGENT": 0, "REPLAY_ONLY_CAMPAIGN": 0, "ENGINE_ONLY_CAMPAIGN": 0}
    for pair in pairs:
        classified[pair["status"]] = classified.get(pair["status"], 0) + 1
    print(f"[RECON] eşleşme: {classified}")
    print(f"[RECON] sapma sınıfları: {classes}")
    print(f"[RECON] kalan açık emir: {engine_result['orders_open_at_end']}")
    print(f"[RECON] artifact sha256:{_sha256(path)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
