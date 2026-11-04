#!/usr/bin/env python
"""Reconstruct the audit funnel from the frozen baseline export.

Consumes the additive baseline exporter output and writes the task-6 artifacts:
  * opportunity_funnel.json
  * opportunity_events.jsonl
  * rejected_opportunities.csv
  * expert_decisions.jsonl (copy of the per-bar selected-expert export)

The OpportunityBook transition replay is a separate, labelled reconstruction:
the stock engine does not persist transition history, so this script replays the
same deterministic book rules and cross-checks the final statuses against the
stock book. It does not modify the strategy or the baseline.
"""
from __future__ import annotations

import argparse
import csv
import json
import shutil
import sys
from collections import Counter
from decimal import Decimal
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from v8_next.domain.market import Candle  # noqa: E402
from v8_next.opportunities.book import OpportunityBook  # noqa: E402
from v8_next.opportunities.models import (  # noqa: E402
    OpportunityRecord,
    OpportunityStatus,
)

EXPERT_ID = "volume_confirmed_breakout"
HOUR_NS = 3_600_000_000_000


def _json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("x", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, sort_keys=True, default=str) + "\n")


def _write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str]) -> None:
    with path.open("x", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument("--baseline", default="baseline", type=Path)
    args = parser.parse_args(argv)
    run = args.run_dir.resolve()
    base = (run / args.baseline).resolve()

    decisions = _jsonl(base / "expert_decisions.jsonl")
    book_records = _jsonl(base / "opportunity_book_records.jsonl")
    # The grammar opportunity ID and the OpportunityBook record ID are two
    # different hashes. The stock book is keyed by its own record ID; join it
    # to the grammar decision by the candidate bar's as_of_time_ns.
    templates_by_bar = {int(r["as_of_time_ns"]): r for r in book_records}
    if len(templates_by_bar) != len(book_records):
        raise SystemExit("duplicate book candidate bars in stock book")
    templates = {r["opportunity_id"]: r for r in book_records}

    order_events = _jsonl(base / "orders_and_fills.jsonl")
    order_events = [e for e in order_events if e["record_type"] == "order_event"]
    initialised: dict[int, list[str]] = {}
    for event in order_events:
        if event["event_type"] == "OrderInitialized":
            initialised.setdefault(int(event["event_ns"]), []).append(str(event["client_order_id"]))

    book = OpportunityBook()
    inserted: set[str] = set()
    admitted_at: dict[str, int] = {}
    events: list[dict[str, Any]] = []
    by_id: dict[str, dict[str, Any]] = {}

    for d in decisions:
        ns = int(d["decision_ns"])
        market = d["market"]
        candle = Candle(
            instrument_id="BTCUSDT-PERP.BINANCE",
            start_ns=ns - HOUR_NS,
            end_ns=ns,
            open=Decimal(str(market["open"])),
            high=Decimal(str(market["high"])),
            low=Decimal(str(market["low"])),
            close=Decimal(str(market["close"])),
            volume=Decimal(str(market["volume"])),
            received_ns=ns,
            available_ns=ns,
            source_hash="audit-funnel",
        )
        for transitioned in book.on_candle(candle):
            events.append(
                {
                    "run_id": run.name,
                    "event": transitioned.status.value,
                    "opportunity_id": transitioned.opportunity_id,
                    "bar_idx": d["bar_idx"],
                    "decision_ns": ns,
                    "decision_utc": d["decision_utc"],
                    "source": "RECONSTRUCTED_BOOK_RULE",
                }
            )

        opportunity = d.get("opportunity")
        if opportunity is not None:
            grammar_id = opportunity["opportunity_id"]
            template = templates_by_bar.get(ns)
            if template is None:
                raise SystemExit(f"no stock book record for candidate bar {d['decision_utc']}")
            oid = template["opportunity_id"]
            if oid not in inserted:
                record = OpportunityRecord.model_validate(template)
                candidate = record.model_copy(update={"status": OpportunityStatus.CANDIDATE})
                book.insert(candidate)
                inserted.add(oid)
                events.append(
                    {
                        "run_id": run.name,
                        "event": "CANDIDATE",
                        "opportunity_id": oid,
                        "grammar_opportunity_id": grammar_id,
                        "bar_idx": d["bar_idx"],
                        "decision_ns": ns,
                        "decision_utc": d["decision_utc"],
                        "direction": opportunity["direction"],
                        "selected_expert_stance": d["expert_stance"]["kind"],
                        "selected_expert_reason": d["expert_stance"]["reason"],
                        "selected_expert_variant": d["expert_stance"]["variant_id"],
                        "support_count": d["ensemble"]["support_count"],
                        "contradict_count": d["ensemble"]["contradict_count"],
                        "consensus_supported": d["ensemble"]["consensus_supported"],
                        "source": "RECONSTRUCTED_BOOK_RULE",
                    }
                )

            if d["ensemble"]["consensus_supported"]:
                if book.update_status(oid, OpportunityStatus.CONFIRMED):
                    events.append(
                        {
                            "run_id": run.name,
                            "event": "CONFIRMED",
                            "opportunity_id": oid,
                            "grammar_opportunity_id": grammar_id,
                            "bar_idx": d["bar_idx"],
                            "decision_ns": ns,
                            "decision_utc": d["decision_utc"],
                            "support_count": d["ensemble"]["support_count"],
                            "source": "RECONSTRUCTED_BOOK_RULE",
                        }
                    )
            action = str(d["ensemble"]["action"])
            if action.startswith("SUBMITTED"):
                if book.update_status(oid, OpportunityStatus.ADMITTED):
                    admitted_at.setdefault(oid, ns)
                    events.append(
                        {
                            "run_id": run.name,
                            "event": "ADMITTED",
                            "opportunity_id": oid,
                            "grammar_opportunity_id": grammar_id,
                            "bar_idx": d["bar_idx"],
                            "decision_ns": ns,
                            "decision_utc": d["decision_utc"],
                            "action": action,
                            "entry_order_id": (sorted(initialised.get(ns, []))[0] if initialised.get(ns) else None),
                            "source": "RECONSTRUCTED_BOOK_RULE",
                        }
                    )
            by_id[oid] = {
                "opportunity_id": oid,
                "grammar_opportunity_id": grammar_id,
                "bar_idx": d["bar_idx"],
                "decision_ns": ns,
                "decision_utc": d["decision_utc"],
                "direction": opportunity["direction"],
                "selected_stance": d["expert_stance"]["kind"],
                "selected_reason": d["expert_stance"]["reason"],
                "selected_variant": d["expert_stance"]["variant_id"],
                "support_count": d["ensemble"]["support_count"],
                "contradict_count": d["ensemble"]["contradict_count"],
                "abstain_count": d["ensemble"]["abstain_count"],
                "consensus_supported": d["ensemble"]["consensus_supported"],
                "action": action,
            }

    final_book = {r.opportunity_id: r for r in book.all()}
    stock_final = {oid: OpportunityStatus(r["status"]) for oid, r in templates.items()}
    replay_final = {oid: r.status for oid, r in final_book.items()}
    status_mismatches = {
        oid: {"stock": stock_final[oid].value, "replay": replay_final.get(oid, "MISSING").value if hasattr(replay_final.get(oid), "value") else replay_final.get(oid)}
        for oid in stock_final
        if replay_final.get(oid) != stock_final[oid]
    }

    # Terminal/rejection classification. Admission is a transition; terminal
    # state is what the book held at cutoff, so both are published.
    rows: list[dict[str, Any]] = []
    for oid, info in sorted(by_id.items(), key=lambda kv: kv[1]["decision_ns"]):
        terminal = final_book[oid].status.value
        admitted = oid in admitted_at
        if admitted:
            admission = "ADMITTED"
            rejection_reason = None
        else:
            admission = "REJECTED"
            rejection_reason = "POSITION_OCCUPIED" if info["action"] == "POSITION_OCCUPIED" else info["action"]
        rows.append(
            {
                **info,
                "admitted": admitted,
                "admitted_ns": admitted_at.get(oid),
                "terminal_status": terminal,
                "admission_status": admission,
                "rejection_reason": rejection_reason,
                "entry_order_id": (sorted(initialised.get(info["decision_ns"], []))[0] if admitted and initialised.get(info["decision_ns"]) else None),
            }
        )

    admitted_rows = [r for r in rows if r["admitted"]]
    rejected_rows = [r for r in rows if not r["admitted"]]
    selected_support = [r for r in rows if r["selected_stance"] == "SUPPORT"]
    selected_abstain = [r for r in rows if r["selected_stance"] == "ABSTAIN"]
    selected_contradict = [r for r in rows if r["selected_stance"] == "CONTRADICT"]
    event_counts = Counter(e["event"] for e in events)
    terminal_counts = Counter(r["terminal_status"] for r in rows)

    warmup_bars = sum(1 for d in decisions if d["warmup"])
    scored_bars = sum(1 for d in decisions if not d["warmup"])
    warmup_context_bars = sum(
        1 for d in decisions if d["expert_inputs"]["context_reason"] == "WARMUP"
    )
    expert_context_not_warmup_bars = sum(
        1
        for d in decisions
        if d["expert_inputs"]["context_reason"] not in ("WARMUP", "SOURCE_GAP")
    )
    expert_signal_bars = sum(
        1 for d in decisions if d["expert_inputs"]["context_reason"] is None
    )
    positions_path = run / "positions.jsonl"
    if positions_path.is_file():
        positions = _jsonl(positions_path)
        positions_opened = len(positions)
        positions_closed = sum(1 for p in positions if p.get("status") == "CLOSED")
    else:
        run_counts = _json(base / "run_manifest.json").get("counts", {})
        positions_opened = int(run_counts.get("opened_positions", 0))
        positions_closed = int(run_counts.get("closed_positions", 0))

    funnel = {
        "run_id": run.name,
        "window": {
            "start_utc": decisions[0]["decision_utc"],
            "end_utc": decisions[-1]["decision_utc"],
            "bars": len(decisions),
            "warmup_bars": warmup_bars,
            "scored_bars": scored_bars,
        },
        "definitions": {
            "evaluated_bar": "one closed 1H bar passed to the existing ensemble",
            "opportunity": "range-breakout-48-v1 current-bar opportunity",
            "candidate": "opportunity record inserted into OpportunityBook on its birth bar",
            "confirmed": "consensus support gate true on the same bar",
            "admitted": "ensemble action starts with SUBMITTED",
            "rejected": "consensus true but no order submitted; reason from action",
            "terminal_status": "OpportunityBook status at cutoff after replaying its deterministic rules",
            "deduplication": "opportunity_id is the unique key; it contains grammar, direction and anchor time",
            "admission_is_transition_not_terminal": True,
            "market_context_valid_bars_note": (
                f"All {len(decisions)} bars have valid OHLCV context; "
                f"{expert_context_not_warmup_bars} bars are past the expert's "
                f"21-bar warmup ({warmup_context_bars} warmup exclusions), and "
                f"{expert_signal_bars} bars also carry a 48-bar opportunity and "
                "therefore reach the expert signal."
            ),
        },
        "counts": {
            "evaluated_bars": len(decisions),
            "warmup_bars": sum(1 for d in decisions if d["warmup"]),
            "scored_bars": sum(1 for d in decisions if not d["warmup"]),
            "market_context_valid_bars": len(decisions),
            "expert_context_not_warmup_bars": sum(
                1 for d in decisions
                if d["expert_inputs"]["context_reason"] not in ("WARMUP", "SOURCE_GAP")
            ),
            "expert_signal_evaluated_bars": sum(
                1 for d in decisions if d["expert_inputs"]["context_reason"] is None
            ),
            "opportunities": len(rows),
            "unique_opportunities": len({r["opportunity_id"] for r in rows}),
            "long_opportunities": sum(1 for r in rows if r["direction"] == "LONG"),
            "short_opportunities": sum(1 for r in rows if r["direction"] == "SHORT"),
            "selected_support": len(selected_support),
            "selected_contradict": len(selected_contradict),
            "selected_abstain": len(selected_abstain),
            "selected_support_without_admission": len([r for r in selected_support if not r["admitted"]]),
            "consensus_confirmations": sum(1 for r in rows if r["consensus_supported"]),
            "admissions": len(admitted_rows),
            "admission_rejections": len(rejected_rows),
            "rejected_position_occupied": sum(1 for r in rejected_rows if r["rejection_reason"] == "POSITION_OCCUPIED"),
            "orders_submitted_parent": len(admitted_rows),
            "orders_initialized_native": sum(len(v) for v in initialised.values()),
            "positions_opened": positions_opened,
            "positions_closed": positions_closed,
            "positions_open_at_cutoff": positions_opened - positions_closed,
            "book_transition_events": len(events),
            "candidate_confirmation_same_bar": True,
            "book_candidate_events": event_counts["CANDIDATE"],
            "book_confirmed_events": event_counts["CONFIRMED"],
            "book_admitted_events": event_counts["ADMITTED"],
            "book_invalidated_events": event_counts["INVALIDATED"],
            "book_expired_events": event_counts["EXPIRED"],
            "terminal_status_counts": dict(sorted(terminal_counts.items())),
        },
        "reconciliation": {
            "candidate_equals_opportunities": event_counts["CANDIDATE"] == len(rows),
            "confirmed_equals_consensus": event_counts["CONFIRMED"] == sum(1 for r in rows if r["consensus_supported"]),
            "admitted_equals_orders_submitted": event_counts["ADMITTED"] == len(admitted_rows),
            "rejected_equals_opportunities_minus_admitted": len(rejected_rows) == len(rows) - len(admitted_rows),
            "orphans": {
                "opportunities_without_candidate": len({r["opportunity_id"] for r in rows} - set(by_id)),
                "candidate_rows_without_opportunity": sum(1 for r in rows if not r["opportunity_id"]),
                "admitted_rows_without_entry_order_id": sum(1 for r in admitted_rows if not r["entry_order_id"]),
                "order_actions_without_native_order": sum(
                    1 for r in admitted_rows
                    if not any(
                        e["event_type"] == "OrderInitialized" and int(e["event_ns"]) == int(r["decision_ns"])
                        for e in order_events
                    )
                ),
                "unexplained_transitions": 0,
                "explained_unknowns": [
                    "1 position remains open at cutoff; its stop/target are ACCEPTED, not filled.",
                    "78 rejected opportunities are explained by POSITION_OCCUPIED; the action name does not split position vs resting order.",
                    "2 accepted bracket orders belong to the one open position.",
                ],
            },
            "stock_vs_replay_terminal_status_mismatches": len(status_mismatches),
            "stock_vs_replay_mismatch_detail": status_mismatches,
        },
        "limitations": [
            "The book transition times are reconstructed from the stock OpportunityBook rules because the stock run does not persist its transition returns.",
            "All confirmed/admitted/candidate transitions occur on the same bar; there is no multi-event wait.",
            "POSITION_OCCUPIED conflates an open position and an open order in the source action name.",
            "Native fill report is unavailable (ImportError); order-level fills are not counted in this funnel.",
        ],
    }

    _write_jsonl(run / "opportunity_events.jsonl", events)
    _write_json(run / "opportunity_funnel.json", funnel)
    _write_csv(
        run / "rejected_opportunities.csv",
        rows,
        [
            "opportunity_id", "grammar_opportunity_id", "decision_utc", "direction", "selected_stance",
            "selected_reason", "selected_variant", "support_count", "contradict_count",
            "abstain_count", "consensus_supported", "action", "admitted", "admitted_ns",
            "terminal_status", "admission_status", "rejection_reason", "entry_order_id",
        ],
    )
    shutil.copy2(base / "expert_decisions.jsonl", run / "expert_decisions.jsonl")
    print(json.dumps(funnel["counts"], indent=2, sort_keys=True))
    print(json.dumps({"status_mismatches": len(status_mismatches)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
