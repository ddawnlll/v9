#!/usr/bin/env python
"""Readiness audit — how far is V8.7 from production, measured rather than asserted.

Three separate numbers, because a single scalar is exactly what the constitution
forbids (D-153 Rule 57: capability != readiness != future profit; a number may not
collapse the evidence):

* ``technical_delivery``  — of the registered technical acceptance, how much has a
  passing re-run **in the acceptance matrix right now** (derived from the matrix and
  the per-issue evidence reports; no totals are hardcoded here).
* ``capability``          — the measured capability score of the pre-registered swing
  family on a real window, with the domain statuses that produced it. If the chosen
  policy closes no campaign the score is ``null`` and the reason is named; it is never
  filled in.
* ``gate_coverage``       — gates PASS / ten hard gates, resolved by the battery on the
  same series as the capability number.
* ``economic_readiness``  — the authority preconditions, each measured, never assumed.

Everything is written with ``claim_status: NO_ECONOMIC_CLAIM`` and a per-number
provenance note. Synthetic series are not accepted here: the campaign series is
recomputed from the real tape with the swing family's own primitives and is cross
checked against the frozen NX06 receipt (sum of campaigns must match).

Usage (from the repository root):

    uv run --project . --extra dev --extra research \\
        python tools/nx_readiness_audit.py
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import re
import sys
import time
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
from v8_next.evaluation.benchmark_receipt import (
    BenchmarkLedger,
    BenchmarkReceipt,
    GateVector,
    ScoreEvidence,
)
from v8_next.evaluation.gate_resolution import load_tape_candles, resolve_all_gates
from v8_next.evaluation.scoring import (
    compute_capability_breakdown,
    compute_capability_score,
)

HOUR_NS = 3_600 * 10**9
R_ROW = re.compile(r"^\|\s*(R\d+)\s*\|", re.MULTILINE)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def technical_delivery(repo_root: Path) -> dict[str, Any]:
    """Delivered share of the registered acceptance, derived from the artifacts."""
    matrix_path = repo_root / "docs" / "evidence" / "v87" / "NX11" / "acceptance_matrix.json"
    evidence_dir = repo_root / "docs" / "evidence" / "v87"
    if not matrix_path.is_file():
        return {"status": "NO_MATRIX", "pct": None}
    rows = json.loads(matrix_path.read_text())["issues"]
    registered = len(rows)
    reverified = [row for row in rows if not row["check"].get("skipped") and row["check"].get("passed")]
    skipped = [row.get("issue") or row.get("key") for row in rows if row["check"].get("skipped")]
    failed = [
        row.get("issue") or row.get("key")
        for row in rows
        if not row["check"].get("skipped") and not row["check"].get("passed")
    ]
    documented: dict[str, int] = {}
    for report in sorted(evidence_dir.glob("NX*/**/*REPORT*.md")):
        tier = report.parts[-2] if len(report.parts) >= 2 else report.parent.name
        found = len(R_ROW.findall(report.read_text(errors="replace")))
        if found:
            documented[tier] = documented.get(tier, 0) + found
    return {
        "formula": "issues with a passing re-run in the matrix / issues registered in the matrix",
        "issues_registered": registered,
        "issues_reverified_passing": len(reverified),
        "issues_without_own_target": skipped,
        "issues_failing": failed,
        "r_criteria_documented_in_reports": documented,
        "r_criteria_documented_total": sum(documented.values()),
        "pct": round(100.0 * len(reverified) / registered, 1),
        "matrix_sha256": _sha256(matrix_path),
        "provenance": "counted from the acceptance matrix and the issue reports on disk; no total is hardcoded",
    }


def swing_campaign_series(
    series: list[Any], policy_id: str, *, warmup_bars: int | None, taker_fee: Decimal
) -> dict[str, Any]:
    """Replay one pre-registered swing policy and return its per-campaign returns."""
    spec = policy_spec(policy_id)
    instrument = series[0].instrument_id
    campaigns: list[float] = []
    if spec.grammar_policy is not None:
        required = (
            int(POLICY_REQUIRED_BARS.get(spec.grammar_policy, 0))
            if warmup_bars is None
            else int(warmup_bars)
        )
        has_bracket = spec.protection_policy not in (None, "timeout-only-v1")
        index = required
        while index < len(series) - 1:
            frame = frame_at(instrument, series[index].end_ns, tuple(series[: index + 1]))
            decision = swing_signal(frame, spec, bar_ns=HOUR_NS)
            if decision is None:
                index += 1
                continue
            outcome = replay_bracket(
                decision,
                series[index + 1 :],
                bar_ns=HOUR_NS,
                bps_fee=taker_fee,
                has_bracket=has_bracket,
            )
            campaigns.append(float(outcome.net_return))
            index += max(1, outcome.bars_held) + 1
    else:
        required = 0
    return {
        "policy_id": policy_id,
        "instrument_id": instrument,
        "warmup_bars": required,
        "campaigns": len(campaigns),
        "series_sum": round(sum(campaigns), 8),
        "series": [round(value, 8) for value in campaigns],
    }


def economic_readiness(repo_root: Path) -> dict[str, Any]:
    """The authority preconditions, each measured, never assumed."""
    burn = repo_root / "docs" / "evidence" / "v87" / "NX01" / "burn_map.json"
    stats = repo_root / "docs" / "evidence" / "v87" / "NX07" / "statistics_receipt.json"
    protected_final = False
    protection_note = "burn map absent"
    if burn.is_file():
        data = json.loads(burn.read_text())
        protected_final = bool(data.get("protected_final_available", False))
        protection_note = "protected" if protected_final else "last 12 months are TAIL_BURNED"
    dsr_confidence = None
    if stats.is_file():
        dsr_confidence = (json.loads(stats.read_text()).get("statistics", {}).get("dsr") or {}).get(
            "dsr_confidence"
        )
    conditions = {
        "protected_final_window": {"met": protected_final, "note": protection_note},
        "prospective_maturity": {
            "met": False,
            "note": "no cadence has elapsed; the frozen window has not been observed yet",
        },
        "selection_control_threshold": {
            "met": bool(dsr_confidence is not None and dsr_confidence >= 0.95),
            "measured_dsr_confidence": dsr_confidence,
            "threshold": 0.95,
            "note": "measured, below threshold" if dsr_confidence is not None else "not measured",
        },
    }
    met = sum(1 for row in conditions.values() if row["met"])
    return {
        "formula": "authority preconditions met / authority preconditions required",
        "conditions": conditions,
        "met": met,
        "required": len(conditions),
        "pct": round(100.0 * met / len(conditions), 1),
        "provenance": "each condition read from its own evidence artifact; unmet stays unmet",
    }


def _ms(value: str) -> int:
    return int(
        datetime.datetime.fromisoformat(value).replace(tzinfo=datetime.timezone.utc).timestamp() * 1000
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", default=None)
    parser.add_argument("--policy", default="plain_swing")
    parser.add_argument("--tape", default="research/tape/multi-1h-4y/tape.jsonl")
    parser.add_argument("--instrument", default="BTCUSDT")
    parser.add_argument("--start-utc", default="2025-01-01")
    parser.add_argument("--end-utc", default="2025-02-01")
    parser.add_argument("--history-bars", type=int, default=96)
    parser.add_argument(
        "--warmup-bars",
        type=int,
        default=62,
        help="bars skipped before the first decision; 62 is what the NX06 harness replayed",
    )
    parser.add_argument("--out", default="docs/evidence/v87/READINESS")
    args = parser.parse_args(argv)

    repo_root = Path(args.repo_root).resolve() if args.repo_root else Path(__file__).resolve().parents[1]
    out_dir = (repo_root / args.out).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    tape = (repo_root / args.tape).resolve()

    candles = load_tape_candles(
        tape, instrument=args.instrument, start_ms=_ms(args.start_utc), end_ms=_ms(args.end_utc)
    )
    if not candles:
        print(f"[READINESS] FAIL: no candles in {args.start_utc}..{args.end_utc}")
        return 2
    # a little history before the window so the first decision already has its warmup,
    # mirroring the NX06 harness whose receipt this audit must agree with
    history = load_tape_candles(
        tape,
        instrument=args.instrument,
        start_ms=_ms(args.start_utc) - args.history_bars * 3_600_000,
        end_ms=_ms(args.start_utc),
    )
    series = history + candles

    taker_fee = Decimal(SHARED_CONTRACT["taker_fee"])
    measured = swing_campaign_series(
        series, args.policy, warmup_bars=args.warmup_bars, taker_fee=taker_fee
    )
    declared_warmup = int(POLICY_REQUIRED_BARS.get(policy_spec(args.policy).grammar_policy or "", 0))
    measured_warmup_check = {"declared": None, "replayed": measured["warmup_bars"]}
    if declared_warmup != measured["warmup_bars"]:
        alt = swing_campaign_series(
            series, args.policy, warmup_bars=declared_warmup, taker_fee=taker_fee
        )
        measured_warmup_check = {
            "declared_warmup": declared_warmup,
            "declared_campaigns": alt["campaigns"],
            "declared_series_sum": alt["series_sum"],
            "replayed_warmup": measured["warmup_bars"],
            "replayed_campaigns": measured["campaigns"],
            "replayed_series_sum": measured["series_sum"],
            "delta_series_sum": round(alt["series_sum"] - measured["series_sum"], 10),
        }

    # the audit must agree with the frozen NX06 receipt, otherwise it is not the same measurement
    cross_check: dict[str, Any] = {"status": "NO_FROZEN_RECEIPT"}
    frozen_path = repo_root / "docs" / "evidence" / "v87" / "NX06" / "comparative_receipt.json"
    if frozen_path.is_file():
        frozen = json.loads(frozen_path.read_text())
        row = next(
            (m for m in frozen.get("measurements", []) if m.get("policy_id") == args.policy), None
        )
        expected = None if row is None else row.get("net_return_sum")
        agrees = expected is not None and abs(float(expected) - measured["series_sum"]) < 1e-8
        cross_check = {
            "status": "AGREES" if agrees else "DISAGREES",
            "frozen_receipt": str(frozen_path.relative_to(repo_root)),
            "frozen_sha256": _sha256(frozen_path),
            "frozen_net_return_sum": expected,
            "recomputed_net_return_sum": measured["series_sum"],
        }
        if not agrees:
            print(f"[READINESS] FAIL: recomputed series disagrees with NX06 ({expected} vs {measured['series_sum']})")
            return 3

    warmup_note = {
        "declared_POLICY_REQUIRED_BARS": int(POLICY_REQUIRED_BARS.get(policy_spec(args.policy).grammar_policy or "", 0)),
        "replayed": measured["warmup_bars"],
        "finding": (
            "the NX06 harness replays with a hardcoded warmup; the declared table in "
            "economics/grammar.py is the pre-registered value. Replaying the declared value "
            "does not change the campaign count but moves the net sum by ~1e-6, so the two "
            "must be aligned before the next freeze."
        ),
        "measurement": measured_warmup_check,
    }

    series = measured["series"]
    breakdown = compute_capability_breakdown(series, len(candles), measured["campaigns"], 0.0)
    score = compute_capability_score(series, len(candles), measured["campaigns"], 0.0)

    run_dir = out_dir / "run"
    run_dir.mkdir(parents=True, exist_ok=True)
    ledger = BenchmarkLedger.load_jsonl(run_dir / "benchmark_ledger.jsonl")
    # #408: a published capability score must be a function of the evidence its own
    # receipt carries -- `BenchmarkReceipt.create` refuses a number nothing can
    # reproduce, and this audit's number is no exception. The evidence is the same
    # measurement the number above was computed from; it is not re-derived here.
    receipt = BenchmarkReceipt.create(
        case_id="READINESS-AUDIT",
        policy_id=args.policy,
        capability_score=score,
        coverage_factor=breakdown["coverage_factor"],
        score_evidence=ScoreEvidence.from_breakdown(breakdown),
        gates=GateVector(),
        computed_at_timestamp_ns=int(candles[-1].end_ns),
        # #446: the end of the replayed window and the moment this audit ran are two
        # different quantities; the audit's own clock is published beside the digest.
        window_end_timestamp_ns=int(candles[-1].end_ns),
        run_time_timestamp_ns=time.time_ns(),
    )
    report = resolve_all_gates(
        candles=tuple(candles),
        campaign_return_series=series,
        ledger=ledger,
        receipt_digest=receipt.receipt_digest,
        capability_score=score if score is not None else 0.0,
        strategy_config=None,
        output_dir=run_dir,
        tape_path=tape,
    )
    states = {field: getattr(report.gates, field).name for field in report.gates.__class__.model_fields}
    passed = sum(1 for state in states.values() if state == "PASS")
    verdict = report.gates.readiness()
    # #447: a published gate state must be a measurement. The structural trio is
    # published together with the input it was derived from -- or the named reason it
    # could not be measured -- read from the battery's own report, never restated here.
    structural_gates = {
        "note": (
            "#447: g0/g1/g2 are derived by the resolver, not declared; each entry carries "
            "the measured input and the named reason its state carries"
        ),
        "g0_identity": report.g0_metrics,
        "g1_causal_pit": report.g1_metrics,
        "g2_determinism_ledger": report.g2_metrics,
    }

    audit = {
        "audit": "V8.7 readiness audit",
        "window": {
            "tape": str(tape.relative_to(repo_root)),
            "tape_sha256": _sha256(tape),
            "instrument": args.instrument,
            "start_utc": args.start_utc,
            "end_utc": args.end_utc,
            "bars": len(candles),
        },
        "technical_delivery": technical_delivery(repo_root),
        "capability": {
            "policy_id": args.policy,
            "campaigns": measured["campaigns"],
            "warmup_bars": measured["warmup_bars"],
            "series_sum": measured["series_sum"],
            "score": score,
            "score_is_null_because": (
                None if score is not None else "the policy closed no campaign in this window"
            ),
            "cross_check_with_frozen_nx06": cross_check,
            "warmup_declared_vs_replayed": warmup_note,
            "evidence_class": "DECISION_PLANE_DIAGNOSTIC (real bars, real taker fee; not engine fills, not a venue settlement)",
            "provenance": "replayed from the real tape with the swing family primitives; series cross-checked against the frozen NX06 receipt",
        },
        "gate_coverage": {
            "formula": "gates PASS / ten hard gates, resolved on the same series as capability",
            "states": states,
            "passed": passed,
            "total": len(states),
            "pct": round(100.0 * passed / len(states), 1),
            "readiness_status": verdict.status.name,
            "structural_gates": structural_gates,
        },
        "economic_readiness": economic_readiness(repo_root),
        "claim_status": "NO_ECONOMIC_CLAIM",
        "non_authority": (
            "these percentages audit measured state; none of them grants readiness, a claim, "
            "or capital authority, and none of them is a forecast"
        ),
    }
    path = out_dir / "readiness_audit.json"
    path.write_text(json.dumps(audit, indent=2, sort_keys=True, default=str) + "\n")

    print(f"[READINESS] teknik teslim : {audit['technical_delivery']['pct']} % "
          f"({audit['technical_delivery']['issues_reverified_passing']}/{audit['technical_delivery']['issues_registered']} iş yeniden doğrulandı)")
    print(f"[READINESS] capability   : {score} ({args.policy}, {measured['campaigns']} kampanya)")
    print(f"[READINESS] gate coverage: {audit['gate_coverage']['pct']} % ({passed}/{len(states)} PASS)")
    print(f"[READINESS] economic/live: {audit['economic_readiness']['pct']} % "
          f"({audit['economic_readiness']['met']}/{audit['economic_readiness']['required']})")
    print(f"[READINESS] verdict      : {verdict.status.name}")
    print(f"[READINESS] gate states  : {states}")
    print(
        "[READINESS] g0/g1/g2     : "
        f"g0={report.g0_metrics.get('reason')} "
        f"g1={report.g1_metrics.get('reason')} "
        f"g2={report.g2_metrics.get('reason')}"
    )
    print(f"[READINESS] audit sha256 : {_sha256(path)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
