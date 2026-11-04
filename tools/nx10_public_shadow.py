#!/usr/bin/env python
"""NX10 (#431) — a real bounded public capture, its restart proof, and its maturity report.

Three artifacts, in this order, each written physically:

1. ``forward_plan.json`` — a **future** observation window registered through the
   existing forward-plan freeze (the store stamps the real wall clock; the caller
   cannot backdate it). Historical replay is never labelled prospective here.
2. ``capture/`` — a bounded capture of public venue trades through an
   unauthenticated endpoint, written as rolling, hash-chained batches with the
   arrival and event clocks recorded separately.
3. ``maturity.json`` — what the capture does and does not prove. One bounded capture
   is technical evidence of a working pipeline; it is not prospective maturity, not
   a settlement, and not a reason to pass G7.

Restart is demonstrated, not asserted: the capture is resumed from disk, the replay
re-checks every batch's own payload hash, and re-seen trade identities are dropped
and counted (exactly-once across a restart).

Usage (from the repository root):

    uv run --project . --extra dev --extra research \\
        python tools/nx10_public_shadow.py --seconds 18
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
import urllib.error
import urllib.request
from decimal import Decimal
from pathlib import Path
from typing import Any

from v8_next.domain.config import PaperConfig
from v8_next.evaluation.forward_plan import ForwardPlan, freeze_forward_plan
from v8_next.evaluation.gate_resolution import evaluate_g7_prospective_shadow
from v8_next.evaluation.prospective_capture import (
    CAPTURE_VERSION,
    PUBLIC_PAPER_LABEL,
    PUBLIC_PAPER_SOURCE,
    CaptureState,
    TradeEvent,
    load_capture,
    merge_batch,
    write_batch,
    write_manifest,
)
from v8_next.evaluation.store import ResearchStore

HOUR_NS = 3_600 * 10**9
ENDPOINT = "https://fapi.binance.com/fapi/v1/aggTrades?symbol=BTCUSDT&limit=1000"
MAX_GAP_NS = 5 * 60 * 10**9  # five minutes of public trades: longer is a hole, not a poll


def _get(url: str, timeout: float = 10.0) -> Any:
    request = urllib.request.Request(url, headers={"User-Agent": "v8-next-nx10/1.0"})
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
        return json.loads(response.read().decode("utf-8"))


def fetch_public_trades(symbol: str = "BTCUSDT") -> list[TradeEvent]:
    """One poll of public aggTrades; unauthenticated and explicitly labelled so."""
    arrival_ns = time.time_ns()
    payload = _get(ENDPOINT.replace("BTCUSDT", symbol))
    events: list[TradeEvent] = []
    for row in payload:
        events.append(
            TradeEvent(
                source=PUBLIC_PAPER_SOURCE,
                symbol=symbol,
                trade_id=str(row["a"]),
                event_ns=int(row["T"]) * 1_000_000,
                arrival_ns=arrival_ns,
                price=str(row["p"]),
                qty=str(row["q"]),
            )
        )
    return events


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", default=None)
    parser.add_argument("--out", default="docs/evidence/v87/NX10")
    parser.add_argument("--seconds", type=int, default=18, help="bounded capture budget")
    parser.add_argument("--poll-seconds", type=float, default=3.0)
    parser.add_argument("--symbol", default="BTCUSDT")
    args = parser.parse_args(argv)

    repo_root = (
        Path(args.repo_root).resolve() if args.repo_root else Path(__file__).resolve().parents[1]
    )
    out_dir = (repo_root / args.out).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    capture_dir = out_dir / "capture"
    lock_hash = hashlib.sha256((repo_root / "uv.lock").read_bytes()).hexdigest()

    # ---- 1. freeze a FUTURE window through the existing forward-plan path ----
    now_ns = time.time_ns()
    start_ns = ((now_ns // HOUR_NS) + 1) * HOUR_NS
    end_ns = start_ns + 24 * HOUR_NS
    policies = {
        "squeeze_swing": PaperConfig(
            maker_fee=Decimal("0.0002"),
            taker_fee=Decimal("0.0005"),
            initial_balance=Decimal("10000"),
            max_notional=Decimal("5000"),
            max_exposure_fraction=Decimal("0.5"),
            campaign_policy="squeeze:baseline:v2",
        ),
        "causal_trend": PaperConfig(
            maker_fee=Decimal("0.0002"),
            taker_fee=Decimal("0.0005"),
            initial_balance=Decimal("10000"),
            max_notional=Decimal("5000"),
            max_exposure_fraction=Decimal("0.5"),
            campaign_policy="timeout-only-v1",
        ),
    }
    plan = ForwardPlan(
        instrument_id="BTCUSDT-PERP.BINANCE",
        start_ns=start_ns,
        end_ns=end_ns,
        policies=policies,
        baseline="causal_trend",
        block_size=5,
        reps=999,
        seed=7,
        warmup_bars=62,
    )
    store = ResearchStore(out_dir / "research.sqlite")
    plan_digest = freeze_forward_plan(store, "NX10-PUBLIC-SHADOW-01", plan, lock_hash)
    freeze_clock = time.time_ns()
    plan_record = {
        "plan_id": "NX10-PUBLIC-SHADOW-01",
        "plan_digest": plan_digest,
        "start_ns": plan.start_ns,
        "end_ns": plan.end_ns,
        "start_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(plan.start_ns // 10**9)),
        "end_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(plan.end_ns // 10**9)),
        "freeze_clock_ns": freeze_clock,
        "window_is_in_the_future": plan.start_ns > freeze_clock,
        "code_and_lock_hash": lock_hash,
        "policies": sorted(policies),
        "baseline": plan.baseline,
        "resampling": {"block_size": plan.block_size, "reps": plan.reps, "seed": plan.seed},
        "prospective_label": "PROSPECTIVE_WINDOW_NOT_YET_OBSERVED",
        "historical_replay_labelled_prospective": False,
        "note": (
            "the window opens after the freeze clock; nothing in this file was measured "
            "on a historical replay"
        ),
    }
    (out_dir / "forward_plan.json").write_text(
        json.dumps(plan_record, indent=2, sort_keys=True) + "\n"
    )
    print(f"[NX10] forward plan frozen start_utc={plan_record['start_utc']} future={plan_record['window_is_in_the_future']}")

    # ---- 2. bounded public capture, then the restart proof ----
    state = CaptureState()
    batches: list[dict[str, Any]] = []
    manifest: dict[str, Any] = {
        "capture_version": CAPTURE_VERSION,
        "source": PUBLIC_PAPER_SOURCE,
        "label": PUBLIC_PAPER_LABEL,
        "symbol": args.symbol,
        "max_gap_ns": MAX_GAP_NS,
        "polls": 0,
        "batches": {},
        "chain_hash": "GENESIS",
        "started_ns": time.time_ns(),
        "authority": "NONE",
    }
    write_manifest(capture_dir, manifest)

    deadline = time.monotonic() + args.seconds
    poll = 0
    while time.monotonic() < deadline:
        poll += 1
        try:
            events = fetch_public_trades(args.symbol)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            manifest["last_error"] = f"{type(exc).__name__}: {exc}"
            write_manifest(capture_dir, manifest)
            print(f"[NX10] poll {poll}: unreachable ({type(exc).__name__})")
            time.sleep(min(3.0, max(0.0, deadline - time.monotonic())))
            continue
        state, report = merge_batch(state, events, max_gap_ns=MAX_GAP_NS)
        manifest["polls"] = poll
        if report.get("batch_identity"):
            manifest["batches"][f"batch_{poll:04d}"] = report["batch_identity"]
            manifest["chain_hash"] = report["batch_identity"]["chain_hash"]
            accepted_events = [
                TradeEvent.from_dict(record) for record in report.get("accepted_events", [])
            ]
            write_batch(capture_dir, poll, accepted_events, report)
        elif report.get("recovery_required"):
            # a refused merge is recorded as a visible recovery artifact
            write_batch(capture_dir, poll, [], report)
        write_manifest(capture_dir, manifest)
        batches.append({"poll": poll, "report": report})
        if poll > 1 or args.seconds <= 6:
            break
        time.sleep(min(args.poll_seconds, max(0.0, deadline - time.monotonic())))

    # write the events of the last merge so the artifact is self-contained
    manifest_path = write_manifest(capture_dir, manifest)
    (capture_dir / "polls.json").write_text(
        json.dumps(batches, indent=2, sort_keys=True) + "\n"
    )

    # restart: rebuild state from disk and merge one more poll on top of it
    recovered_manifest, resumed_state, resume_report = load_capture(capture_dir)
    post_restart: dict[str, Any] = {"resume": resume_report}
    try:
        events = fetch_public_trades(args.symbol)
        resumed_state, after = merge_batch(resumed_state, events, max_gap_ns=MAX_GAP_NS)
        post_restart["after_restart_merge"] = after
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        post_restart["after_restart_merge"] = {"status": "UNREACHABLE", "error": str(exc)}

    # ---- 3. maturity report: what this does and does not prove ----
    g7_state, g7_metrics = evaluate_g7_prospective_shadow(
        [], shadow_stream=None
    )
    maturity = {
        "capture_version": CAPTURE_VERSION,
        "label": PUBLIC_PAPER_LABEL,
        "authority": "NONE",
        "bounded_capture": True,
        "capture_seconds": args.seconds,
        "polls": manifest["polls"],
        "batches": len(manifest["batches"]),
        "chain_hash": manifest["chain_hash"],
        "capture_manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
        "exactly_once": {
            "duplicates_dropped": resumed_state.duplicates,
            "out_of_order_rejected": resumed_state.out_of_order,
            "accepted": resumed_state.accepted,
            "gaps": resumed_state.gaps,
        },
        "restart": post_restart,
        "arrival_and_event_clocks_recorded": True,
        "identity_binding": {
            "code_and_lock_hash": lock_hash,
            "plan_digest": plan_digest,
            "per_batch_chain": True,
        },
        "technical_acceptance": "BOUNDED_CAPTURE_AND_RESTART_EVIDENCE",
        "prospective_maturity": "PENDING",
        "prospective_maturity_reason": (
            "the frozen window has not been observed; one bounded capture is pipeline "
            "evidence, not maturity, and holding/markout conditions are unmeasured"
        ),
        "economic_claim": "NONE",
        "g7_state": g7_state.name,
        "g7_reason": g7_metrics.get("reason"),
        "g7_note": (
            "G7 cannot pass on this evidence: no declared prospective stream and no "
            "maturity (NX10.R5)"
        ),
        "settlement": {
            "mode": "PUBLIC_PAPER_NOT_SETTLED",
            "authority": "NONE",
            "reason": (
                "an unauthenticated public capture is technical evidence; it is not a "
                "venue settlement and never becomes one by having the right columns"
            ),
        },
    }
    maturity_path = out_dir / "maturity.json"
    maturity_path.write_text(json.dumps(maturity, indent=2, sort_keys=True, default=str) + "\n")

    print(f"[NX10] polls={manifest['polls']} batches={len(manifest['batches'])} accepted={resumed_state.accepted} duplicates={resumed_state.duplicates}")
    print(f"[NX10] restart chain_verified={resume_report['chain_verified']} replayed={resume_report['events_replayed']}")
    print(f"[NX10] g7={g7_state.name} maturity={maturity['prospective_maturity']}")
    print(f"[NX10] manifest sha256:{maturity['capture_manifest_sha256']}")
    print(f"[NX10] maturity sha256:{hashlib.sha256(maturity_path.read_bytes()).hexdigest()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
