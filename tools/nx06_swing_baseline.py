#!/usr/bin/env python
"""NX06 (#427) — pre-register the swing family, then measure it on a real window.

Two physical artifacts, written in that order and never the other way round:

1. ``family_registry.json`` — policy ids, their pre-registration hashes and the
   shared risk/cost contract. Written **before** the window is replayed, so a
   reviewer can see the family was fixed in advance. The file records the window
   it is about to be measured on but contains no result.
2. ``comparative_receipt.json`` — per policy: campaigns, exit kinds, holding
   distribution in bars and hours, gross/net/fee, exposure, turnover and open
   risk at the cutoff, plus the measured distinction between the grammar's
   opportunity TTL and the protection's open-trade expiry (336 bars for squeeze).

The outcomes are a decision-plane diagnostic on real bars with real taker fees.
They are not engine fills, not a venue settlement, and not an economic claim;
funding and slippage are reported as MISSING/NOT MODELLED rather than zero.

Usage (from the repository root):

    uv run --project . --extra dev --extra research \\
        python tools/nx06_swing_baseline.py
"""

from __future__ import annotations

import argparse
import hashlib
import json
import statistics
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any

from v8_next.domain.market import Candle, CausalFrame, frame_at_incremental
from v8_next.economics.grammar import POLICY_REQUIRED_BARS
from v8_next.economics.swing_baseline import (
    ENGINE_TICK,
    SHARED_CONTRACT,
    SWING_FAMILY,
    SWING_FAMILY_VERSION,
    family_registry,
    open_trade_expiry,
    policy_spec,
    replay_bracket,
    swing_signal,
)
from v8_next.evaluation.gate_resolution import load_tape_candles

HOUR_NS = 3_600 * 10**9
DEFAULT_WINDOW = ("2025-01-01", "2025-02-01")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _parse_utc_ms(value: str) -> int:
    import datetime

    parsed = datetime.datetime.fromisoformat(value.strip()).replace(
        tzinfo=datetime.timezone.utc
    )
    return int(parsed.timestamp() * 1000)


def _histogram(values: list[int]) -> dict[str, int]:
    buckets: dict[str, int] = {}
    for value in values:
        key = f"{value // 50 * 50}-{value // 50 * 50 + 49}"
        buckets[key] = buckets.get(key, 0) + 1
    return dict(sorted(buckets.items(), key=lambda kv: int(kv[0].split("-")[0])))


def measure_policy(
    policy_id: str,
    candles: list[Candle],
    *,
    instrument: str,
    taker_fee: Decimal,
) -> dict[str, Any]:
    spec = policy_spec(policy_id)
    # the warmup is the policy's declared pre-registration requirement, never a local
    # constant: a harness replaying a different warmup than the family declares is
    # measuring a different policy (the drift was -1.07e-06 on the net sum until this fix).
    required = 0 if spec.grammar_policy is None else int(POLICY_REQUIRED_BARS[spec.grammar_policy])
    expiry = open_trade_expiry(spec, HOUR_NS)

    if spec.grammar_policy is None:
        return {
            "policy_id": policy_id,
            "campaigns": 0,
            "exposure_bars": 0,
            "exposure_fraction": 0.0,
            "turnover": 0.0,
            "gross_return_sum": 0.0,
            "net_return_sum": 0.0,
            "fee_cost_sum": 0.0,
            "open_risk_return": 0.0,
            "exit_kinds": {},
            "holding_bars": {"min": None, "median": None, "max": None, "histogram": {}},
            "opportunity_ttl_bars": [],
            "open_trade_expiry_bars": None,
            "open_trade_expiry_hours": None,
            "open_trade_expiry_days": None,
            "first_decision_identity": None,
            "decisions": 0,
            "warmup_bars": 0,
            "warmup_source": "no grammar; nothing to warm up",
            "note": "no exposure by construction; the comparison floor, not a measurement",
        }

    outcomes: list[dict[str, Any]] = []
    decisions: list[dict[str, Any]] = []
    index = required
    open_risk = 0.0
    exposure_bars = 0
    parent_frame: CausalFrame | None = None
    while index < len(candles) - 1:
        decision_ns = candles[index].end_ns
        prefix = tuple(candles[: index + 1])
        frame = frame_at_incremental(parent_frame, instrument, decision_ns, prefix)
        parent_frame = frame
        decision = swing_signal(frame, spec, bar_ns=HOUR_NS)
        if decision is None:
            index += 1
            continue
        future = candles[index + 1 :]
        has_bracket = spec.protection_policy not in (None, "timeout-only-v1")
        outcome = replay_bracket(
            decision, future, bar_ns=HOUR_NS, bps_fee=taker_fee, has_bracket=has_bracket
        )
        outcomes.append(outcome.as_dict())
        decisions.append(decision.as_dict())
        exposure_bars += outcome.bars_held
        if outcome.exit_kind == "OPEN_AT_CUTOFF":
            open_risk += outcome.net_return
        # never overlap campaigns for one policy: continue after the exit bar
        index += max(1, outcome.bars_held) + 1

    holding = [int(o["bars_held"]) for o in outcomes]
    exit_kinds: dict[str, int] = {}
    for outcome in outcomes:
        exit_kinds[outcome["exit_kind"]] = exit_kinds.get(outcome["exit_kind"], 0) + 1
    notional = Decimal(SHARED_CONTRACT["initial_balance_usdt"]) * Decimal(
        SHARED_CONTRACT["risk_per_trade_fraction"]
    )
    turnover = float(notional) * 2 * len(outcomes) / float(SHARED_CONTRACT["initial_balance_usdt"])
    return {
        "policy_id": policy_id,
        "campaigns": len(outcomes),
        "exposure_bars": exposure_bars,
        "exposure_fraction": exposure_bars / max(1, len(candles)),
        "turnover": turnover,
        "gross_return_sum": round(sum(o["gross_return"] for o in outcomes), 8),
        "net_return_sum": round(sum(o["net_return"] for o in outcomes), 8),
        "fee_cost_sum": round(sum(o["fee_cost_return"] for o in outcomes), 8),
        "open_risk_return": round(open_risk, 8),
        "exit_kinds": dict(sorted(exit_kinds.items())),
        "holding_bars": {
            "min": min(holding) if holding else None,
            "median": statistics.median(holding) if holding else None,
            "max": max(holding) if holding else None,
            "histogram": _histogram(holding) if holding else {},
        },
        "opportunity_ttl_bars": sorted({d["opportunity_ttl_bars"] for d in decisions}),
        "open_trade_expiry_bars": None if expiry is None else expiry.bars,
        "open_trade_expiry_hours": None if expiry is None else expiry.hours,
        "open_trade_expiry_days": None if expiry is None else expiry.days,
        "first_decision_identity": decisions[0]["decision_identity"] if decisions else None,
        "decisions": len(decisions),
        "warmup_bars": required,
        "warmup_source": "POLICY_REQUIRED_BARS[grammar_policy] from economics/grammar.py",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", default=None)
    parser.add_argument("--tape", default="research/tape/multi-1h-4y/tape.jsonl")
    parser.add_argument("--instrument", default="BTCUSDT")
    parser.add_argument("--start-utc", default=DEFAULT_WINDOW[0])
    parser.add_argument("--end-utc", default=DEFAULT_WINDOW[1])
    parser.add_argument("--out", default="docs/evidence/v87/NX06")
    args = parser.parse_args(argv)

    repo_root = (
        Path(args.repo_root).resolve() if args.repo_root else Path(__file__).resolve().parents[1]
    )
    out_dir = (repo_root / args.out).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    tape = (repo_root / args.tape).resolve()
    if not tape.is_file():
        print(f"[NX06] FAIL: tape absent at {tape}")
        return 2

    start_ms, end_ms = _parse_utc_ms(args.start_utc), _parse_utc_ms(args.end_utc)
    window = {
        "tape": str(tape),
        "instrument": args.instrument,
        "start_utc": args.start_utc,
        "end_utc": args.end_utc,
        "start_ms": start_ms,
        "end_ms": end_ms,
    }

    # 1. pre-registration, before the window is replayed
    registry = {
        "family_version": SWING_FAMILY_VERSION,
        "about_to_be_measured_on": window,
        "registered_before_measurement": True,
        "shared_contract": SHARED_CONTRACT,
        "engine_tick": str(ENGINE_TICK),
        "policies": [spec.as_dict() for spec in SWING_FAMILY],
        "family_registry": family_registry(),
        "note": "no result is in this file; the measurement is written separately",
    }
    registry_path = out_dir / "family_registry.json"
    registry_path.write_text(json.dumps(registry, indent=2, sort_keys=True) + "\n")

    candles = load_tape_candles(
        tape, instrument=args.instrument, start_ms=start_ms, end_ms=end_ms
    )
    # A little history before the window so the first decision already has its warmup.
    history = load_tape_candles(
        tape,
        instrument=args.instrument,
        start_ms=start_ms - 96 * 3_600_000,
        end_ms=start_ms,
    )
    series = history + candles
    # the grammar resolves exposure through the venue-qualified instrument id the
    # tape itself carries ("BTCUSDT-PERP.BINANCE"), never the bare base symbol.
    instrument_id = series[0].instrument_id
    print(f"[NX06] window bars={len(candles)} (+{len(history)} warmup bars) instrument={instrument_id}")

    measurements = [
        measure_policy(
            spec.policy_id,
            series,
            instrument=instrument_id,
            taker_fee=Decimal(SHARED_CONTRACT["taker_fee"]),
        )
        for spec in SWING_FAMILY
    ]
    receipt = {
        "window": window,
        "bars": len(candles),
        "family_registry_sha256": _sha256(registry_path),
        "measurements": measurements,
        "contract": SHARED_CONTRACT,
        "evidence_class": (
            "DECISION_PLANE_DIAGNOSTIC: real bars and real taker fees, but not engine "
            "fills and not a venue settlement; funding/slippage MISSING, not zero"
        ),
        "economic_claim": "NONE",
        "success_condition": (
            "a higher return is NOT the acceptance criterion; a null or negative result "
            "is a complete technical delivery"
        ),
    }
    receipt_path = out_dir / "comparative_receipt.json"
    receipt_path.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")

    for measurement in measurements:
        print(
            f"[NX06] {measurement['policy_id']:<13} campaigns={measurement['campaigns']:<4} "
            f"exits={measurement['exit_kinds']} exposure={measurement['exposure_fraction']:.3f} "
            f"net_sum={measurement['net_return_sum']:+.6f} "
            f"holding_median={((measurement['holding_bars'] or {}).get('median'))}"
        )
    print(f"[NX06] registry sha256:{_sha256(registry_path)}")
    print(f"[NX06] receipt  sha256:{_sha256(receipt_path)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
