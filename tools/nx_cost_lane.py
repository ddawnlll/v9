#!/usr/bin/env python
"""Cost lane — bind real funding, real fees and a declared slippage rule to the swing family.

Runs the pre-registered family on a real window of the four-year tape and reports one
campaign's costs as **separate** fields: measured fees, measured funding (real settlement
rows at real settlement times), a declared slippage assumption, and the measured execution
shortfall which stays absent for want of mark price or an order book.

The slippage fraction is not chosen to flatter the result: the run sweeps a declared set of
fractions and publishes the whole curve, so the reader can see the dependence instead of
trusting one number. ``--slippage-fraction`` pins a single value for a reconciliation run.

Usage (from the repository root):

    uv run --project . --extra dev --extra research \\
        python tools/nx_cost_lane.py [--window 2025-01] [--policy plain_swing]
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

from v8_next.domain.market import frame_at
from v8_next.economics.grammar import POLICY_REQUIRED_BARS
from v8_next.economics.swing_baseline import (
    SHARED_CONTRACT,
    policy_spec,
    replay_bracket,
    swing_signal,
)
from v8_next.economics.swing_costs import (
    EXECUTION_SHORTFALL_MISSING,
    SLIPPAGE_MODELS,
    SWING_COST_MODEL_VERSION,
    SlippageModel,
    aggregate,
    campaign_cost,
)
from v8_next.evaluation.multitape import load_multitape

HOUR_NS = 3_600 * 10**9
DEFAULT_SWEEP = (0.0, 0.05, 0.10, 0.25)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _ms(value: str) -> int:
    return int(
        datetime.datetime.fromisoformat(value).replace(tzinfo=datetime.timezone.utc).timestamp() * 1000
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", default=None)
    parser.add_argument("--tape", default="research/tape/multi-1h-4y/tape.jsonl")
    parser.add_argument("--instrument", default="BTCUSDT")
    parser.add_argument("--start-utc", default="2025-01-01")
    parser.add_argument("--end-utc", default="2025-02-01")
    parser.add_argument("--history-bars", type=int, default=96)
    parser.add_argument("--policies", default="plain_swing,causal_trend")
    parser.add_argument("--slippage-fraction", type=float, default=None)
    parser.add_argument("--out", default="docs/evidence/v87-r3/COST")
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
    if args.instrument not in tape.candles:
        print(f"[COST] FAIL: {args.instrument} not in tape instruments {tape.instruments}")
        return 2
    series = list(tape.candles[args.instrument])
    # the grammar resolves exposure through the venue-qualified instrument id the tape
    # carries ("BTCUSDT-PERP.BINANCE"), never the bare base symbol used to select the leg
    instrument_id = series[0].instrument_id
    funding_rows = tuple(tape.funding)
    btc_rows = [row for row in funding_rows if row.instrument == args.instrument]
    if not btc_rows:
        print(f"[COST] FAIL: no funding rows for {args.instrument}")
        return 2

    fractions = (
        (args.slippage_fraction,) if args.slippage_fraction is not None else DEFAULT_SWEEP
    )
    taker_fee = Decimal(SHARED_CONTRACT["taker_fee"])
    window_bars = sum(
        1 for candle in series if _ms(args.start_utc) * 1_000_000 <= candle.end_ns <= _ms(args.end_utc) * 1_000_000
    )

    report: dict[str, Any] = {
        "lane": "cost",
        "model_version": SWING_COST_MODEL_VERSION,
        "claim_status": "NO_ECONOMIC_CLAIM",
        "evidence_class": (
            "DECISION_PLANE_DIAGNOSTIC: real bars, real taker fee and real funding settlement "
            "rows; not engine fills, not a venue settlement. Slippage is a declared assumption, "
            "the measured execution shortfall is absent (no mark price, no book)."
        ),
        "window": {
            "tape": str(tape_path.relative_to(repo_root)),
            "tape_sha256": tape.tape_sha256,
            "instrument": args.instrument,
            "instrument_id": instrument_id,
            "start_utc": args.start_utc,
            "end_utc": args.end_utc,
            "window_bars": window_bars,
            "history_bars": args.history_bars,
        },
        "funding_provenance": {
            "source": "tape funding channel (binance-um-funding-v1-ms, 8h settlements)",
            "rows_for_instrument": len(btc_rows),
            "rows_all_instruments": len(funding_rows),
            "dropped_at_load": tape.funding_dropped,
            "raw_count": tape.funding_raw_count,
            "interval_counts": tape.funding_interval_counts,
            "interval_defaulted_rows": tape.funding_interval_defaulted,
            "mark_price_absent": tape.mark_price_absent,
            "absence_notes": list(tape.absence_notes),
        },
        "slippage_provenance": {
            "swept_fractions": list(fractions),
            "model": "entry_bar_range_fraction",
            "registry": SLIPPAGE_MODELS,
            "note": (
                "the fraction is an assumption and the range it scales is measured; the sweep is "
                "published whole so no single number is presented as the cost of execution"
            ),
        },
        "execution_shortfall": {
            "measured_return": None,
            "status": EXECUTION_SHORTFALL_MISSING,
            "blocker": "no mark price and no order book in this tape",
        },
        "family": {},
    }

    for policy_id in [name.strip() for name in args.policies.split(",") if name.strip()]:
        spec = policy_spec(policy_id)
        if spec.grammar_policy is None:
            continue
        warmup = int(POLICY_REQUIRED_BARS[spec.grammar_policy])
        has_bracket = spec.protection_policy not in (None, "timeout-only-v1")
        index = warmup
        per_fraction: dict[float, list[Any]] = {fraction: [] for fraction in fractions}
        campaign_rows: list[dict[str, Any]] = []
        considered = 0
        while index < len(series) - 1:
            bar = series[index]
            frame = frame_at(instrument_id, bar.end_ns, tuple(series[: index + 1]))
            decision = swing_signal(frame, spec, bar_ns=HOUR_NS)
            if decision is None:
                index += 1
                continue
            outcome = replay_bracket(
                decision, series[index + 1 :], bar_ns=HOUR_NS, bps_fee=taker_fee, has_bracket=has_bracket
            )
            index += max(1, outcome.bars_held) + 1
            if bar.start_ns < _ms(args.start_utc) * 1_000_000:
                continue  # only campaigns opened inside the scored window
            considered += 1
            for fraction in fractions:
                cost = campaign_cost(
                    decision=decision,
                    outcome=outcome,
                    funding_rows=funding_rows,
                    slippage=SlippageModel(model="entry_bar_range_fraction", range_fraction=fraction),
                    entry_bar_low=bar.low,
                    entry_bar_high=bar.high,
                    dropped_rows_in_tape=tape.funding_dropped,
                )
                per_fraction[fraction].append(cost)
            campaign_rows.append(per_fraction[fractions[-1]][-1].as_dict())

        entry: dict[str, Any] = {
            "policy_id": policy_id,
            "grammar_policy": spec.grammar_policy,
            "protection_policy": spec.protection_policy,
            "warmup_bars": warmup,
            "campaigns_in_window": considered,
            "totals_by_slippage_fraction": {
                str(fraction): aggregate(per_fraction[fraction]) for fraction in fractions
            },
            "campaigns": campaign_rows,
        }
        report["family"][policy_id] = entry

    path = out_dir / "cost_lane.json"
    path.write_text(json.dumps(report, indent=2, sort_keys=True, default=str) + "\n")

    for policy_id, entry in report["family"].items():
        print(f"[COST] {policy_id}: kampanya={entry['campaigns_in_window']} warmup={entry['warmup_bars']}")
        for fraction, totals in entry["totals_by_slippage_fraction"].items():
            print(
                f"        slip={fraction:>4}  ölçülen(gross+fee+funding)={totals['net_return_measured_only_sum']:+.8f}"
                f"  funding={totals['funding_cost_return_sum']:+.8f}"
                f"  fee={totals['fee_cost_return_sum']:+.8f}"
                f"  modellenen_slip={totals['slippage_modelled_return_sum']:+.8f}"
                f"  toplam={totals['net_return_with_modelled_slippage_sum']:+.8f}"
                f"  funding_eksik_ kampanya={totals['funding_incomplete_campaigns']}"
            )
    print(f"[COST] funding satırları ({args.instrument}): {len(btc_rows)} | mark price yok: {tape.mark_price_absent}")
    print(f"[COST] ölçülmüş execution shortfall: {EXECUTION_SHORTFALL_MISSING}")
    print(f"[COST] cost_lane.json sha256:{_sha256(path)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
