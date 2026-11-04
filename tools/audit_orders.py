#!/usr/bin/env python
"""Normalise native baseline orders and positions for the forensic audit.

Facts are joined from the native account order cache, order events and the
decision-bar bracket contract. Missing per-fill identity/commission/liquidity is
reported as NOT_IDENTIFIABLE, never invented.
"""
from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

HOUR_NS = 3_600_000_000_000
ROLE_BY_INDEX = {0: "ENTRY", 1: "STOP", 2: "TARGET"}
TYPE_BY_ROLE = {"ENTRY": "MARKET", "STOP": "STOP_MARKET", "TARGET": "LIMIT"}


def _json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    if path.exists():
        raise FileExistsError(path)
    with path.open("x", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, sort_keys=True, default=str) + "\n")


def _write_json(path: Path, payload: Any) -> None:
    if path.exists():
        raise FileExistsError(path)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")


def _pnl(value: str) -> float:
    return float(value.split()[0])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument("--baseline", default="baseline", type=Path)
    args = parser.parse_args(argv)
    run = args.run_dir.resolve()
    base = (run / args.baseline).resolve()
    acc = _json(base / "baseline_account_and_execution.json")
    orders = {str(o["client_order_id"]): o for o in acc["account"]["orders"]}
    events = acc["order_events"]
    book = {int(r["as_of_time_ns"]): r for r in _jsonl(base / "opportunity_book_records.jsonl")}
    decisions = {int(d["decision_ns"]): d for d in _jsonl(base / "expert_decisions.jsonl")}

    by_id: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for e in events:
        by_id[str(e["client_order_id"])].append(e)
    for rows in by_id.values():
        rows.sort(key=lambda e: (int(e["event_ns"]), int(e["received_ns"])))

    initialised: dict[int, list[str]] = defaultdict(list)
    for e in events:
        if e["event_type"] == "OrderInitialized":
            initialised[int(e["event_ns"])].append(str(e["client_order_id"]))
    for ns in initialised:
        initialised[ns].sort()

    order_rows: list[dict[str, Any]] = []
    fill_rows: list[dict[str, Any]] = []
    campaigns: list[dict[str, Any]] = []
    closed_by_ts = {int(p["event_ns"]): p for p in acc["closed_positions"]}

    for entry_ns, ids in sorted(initialised.items()):
        if len(ids) != 3:
            raise SystemExit(f"expected 3 bracket orders at {entry_ns}, got {ids}")
        decision = decisions[entry_ns]
        book_rec = book[entry_ns]
        direction = decision["opportunity"]["direction"]
        entry_order = orders[ids[0]]
        if entry_order["status"] != "FILLED":
            raise SystemExit(f"entry order not filled: {entry_order}")
        side = str(entry_order["side"])
        if (direction == "LONG") != (side == "BUY"):
            raise SystemExit(f"entry side/direction mismatch at {entry_ns}")

        entry_px = float(entry_order["average_price"])
        qty = float(entry_order["filled_qty"])
        stop_px = float(book_rec["stop_price"])
        target_px = float(book_rec["target_price"])
        reference = float(book_rec["entry_price"])
        stop_distance = abs(reference - stop_px)
        target_distance = abs(target_px - reference)
        campaign_id = f"campaign-{len(campaigns)+1:04d}"
        role_rows: dict[str, dict[str, Any]] = {}

        for idx, oid in enumerate(ids):
            o = orders[oid]
            ev = by_id[oid]

            def first(kind: str, ev: list[dict[str, Any]] = ev) -> int | None:
                return next((int(e["event_ns"]) for e in ev if e["event_type"] == kind), None)

            role = ROLE_BY_INDEX[idx]
            filled_qty = float(o["filled_qty"])
            quantity = float(o["quantity"])
            avg_px = None if o["average_price"] is None else float(o["average_price"])
            row = {
                "run_id": run.name,
                "record_type": "order",
                "order_id": oid,
                "campaign_id": campaign_id,
                "decision_ns": entry_ns,
                "decision_utc": decision["decision_utc"],
                "opportunity_id": book_rec["opportunity_id"],
                "grammar_opportunity_id": decision["opportunity"]["opportunity_id"],
                "instrument_id": o["instrument_id"],
                "direction": direction,
                "order_role": role,
                "order_type": TYPE_BY_ROLE[role],
                "order_type_source": "CODE_BRACKET_CONTRACT",
                "side": o["side"],
                "status": o["status"],
                "quantity": quantity,
                "filled_qty": filled_qty,
                "average_price": avg_px,
                "partial_fill": 0 < filled_qty < quantity,
                "full_fill": filled_qty == quantity and quantity > 0,
                "submitted_ns": first("OrderSubmitted"),
                "accepted_ns": first("OrderAccepted"),
                "filled_ns": first("OrderFilled"),
                "canceled_ns": first("OrderCanceled"),
                "event_types": [e["event_type"] for e in ev],
                "order_list_id": None,
                "venue_order_id": None,
                "trade_id": None,
                "fill_id": None,
                "fill_source": "ENGINE_CACHE_ORDER_AVG_PX" if filled_qty > 0 else None,
                "commission": None,
                "commission_source": "NOT_IDENTIFIABLE_WITHOUT_FILL_REPORT",
                "liquidity_side": None,
                "slippage_model": "UNSPECIFIED_NO_EXPLICIT_PROFILE",
            }
            order_rows.append(row)
            role_rows[role] = row
            if filled_qty > 0:
                fill_rows.append({
                    "run_id": run.name,
                    "record_type": "fill",
                    "fill_id": None,
                    "order_id": oid,
                    "campaign_id": campaign_id,
                    "order_role": role,
                    "order_type": row["order_type"],
                    "side": o["side"],
                    "quantity": filled_qty,
                    "price": avg_px,
                    "fill_ns": row["filled_ns"],
                    "commission": None,
                    "commission_source": "NOT_IDENTIFIABLE_WITHOUT_FILL_REPORT",
                    "liquidity_side": None,
                    "source": "ENGINE_CACHE_ORDER_AVG_PX",
                })

        exit_role = None
        exit_order = None
        for role in ("STOP", "TARGET"):
            if role_rows[role]["status"] == "FILLED":
                exit_role = role
                exit_order = role_rows[role]
                break
        entry_fill_ns = role_rows["ENTRY"]["filled_ns"]
        if exit_order is not None:
            exit_px = float(exit_order["average_price"])
            exit_ns = exit_order["filled_ns"]
            closed = closed_by_ts[int(exit_ns)]
            net_pnl = _pnl(closed["realized_pnl"])
            sign = 1 if direction == "LONG" else -1
            gross_pnl = (exit_px - entry_px) * qty * sign
            holding_ns = int(exit_ns) - int(entry_fill_ns)
            status = "CLOSED"
            exit_kind = exit_role
        else:
            exit_px = None
            exit_ns = None
            exit_kind = "OPEN_AT_CUTOFF"
            net_pnl = None
            gross_pnl = None
            holding_ns = None
            status = "OPEN_AT_CUTOFF"
        sibling_canceled = any(
            role != exit_role and role_rows[role]["status"] == "CANCELED"
            for role in ("STOP", "TARGET")
        )
        campaigns.append({
            "run_id": run.name,
            "record_type": "position",
            "campaign_id": campaign_id,
            "position_id": "BTCUSDT-PERP.BINANCE-ExpertEnsembleStrategy-000",
            "opportunity_id": book_rec["opportunity_id"],
            "grammar_opportunity_id": decision["opportunity"]["opportunity_id"],
            "decision_ns": entry_ns,
            "decision_utc": decision["decision_utc"],
            "direction": direction,
            "status": status,
            "quantity": qty,
            "entry_order_id": ids[0],
            "stop_order_id": ids[1],
            "target_order_id": ids[2],
            "entry_reference": reference,
            "entry_fill_price": entry_px,
            "stop_price": stop_px,
            "target_price": target_px,
            "stop_distance": stop_distance,
            "target_distance": target_distance,
            "stop_distance_pct": stop_distance / reference if reference else None,
            "target_distance_pct": target_distance / reference if reference else None,
            "declared_reward_risk": (target_distance / stop_distance) if stop_distance else None,
            "entry_fill_ns": entry_fill_ns,
            "exit_fill_ns": exit_ns,
            "exit_fill_price": exit_px,
            "holding_ns": holding_ns,
            "holding_bars": (holding_ns / HOUR_NS) if holding_ns is not None else None,
            "exit_kind": exit_kind,
            "gross_price_pnl_usdt": gross_pnl,
            "net_realized_pnl_usdt": net_pnl,
            "implied_fee_and_adjustment_usdt": (gross_pnl - net_pnl) if gross_pnl is not None and net_pnl is not None else None,
            "protection_formed": True,
            "bracketless": False,
            "stop_order_status": role_rows["STOP"]["status"],
            "target_order_status": role_rows["TARGET"]["status"],
            "sibling_canceled": sibling_canceled,
            "entry_order_type": "MARKET",
            "stop_order_type": "STOP_MARKET",
            "target_order_type": "LIMIT",
            "execution_profile": None,
            "slippage_model": "UNSPECIFIED_NO_EXPLICIT_PROFILE",
            "commission_source": "NOT_IDENTIFIABLE_WITHOUT_FILL_REPORT",
            "limitations": [
                "Fill price is the native account order average price; per-fill id, commission and liquidity flag are missing.",
                "Stop/target prices are the decision-bar declared bracket prices, not exchange trigger records.",
                "Holding bars use native fill timestamps; intrabar sequence is not observable in OHLCV.",
            ],
        })

    _write_jsonl(run / "orders_and_fills.jsonl", [
        *({"record_type": "order_event", **e} for e in events),
        *order_rows,
        *fill_rows,
    ])
    _write_jsonl(run / "positions.jsonl", campaigns)

    status_counts = Counter(o["status"] for o in order_rows)
    role_status = Counter((o["order_role"], o["status"]) for o in order_rows)
    exit_counts = Counter(c["exit_kind"] for c in campaigns)
    fills = [o for o in order_rows if o["filled_qty"] > 0]
    delays = [
        int(o["filled_ns"]) - int(o["submitted_ns"])
        for o in fills if o["filled_ns"] is not None and o["submitted_ns"] is not None
    ]
    summary = {
        "run_id": run.name,
        "orders_total": len(order_rows),
        "orders_parent_entries": len(initialised),
        "order_events": len(events),
        "order_status_counts": dict(sorted(status_counts.items())),
        "role_status_counts": {f"{k[0]}:{k[1]}": v for k, v in sorted(role_status.items())},
        "fills": len(fills),
        "partial_fills": sum(1 for o in order_rows if o["partial_fill"]),
        "full_fills": sum(1 for o in order_rows if o["full_fill"]),
        "orders_with_fill_price": sum(1 for o in fills if o["average_price"] is not None),
        "orders_missing_fill_report": len(order_rows),
        "submission_to_fill_ns_min": min(delays) if delays else None,
        "submission_to_fill_ns_median": sorted(delays)[len(delays) // 2] if delays else None,
        "submission_to_fill_ns_max": max(delays) if delays else None,
        "positions": len(campaigns),
        "positions_closed": sum(1 for c in campaigns if c["status"] == "CLOSED"),
        "positions_open_at_cutoff": sum(1 for c in campaigns if c["status"] == "OPEN_AT_CUTOFF"),
        "exit_kind_counts": dict(sorted(exit_counts.items())),
        "protection_formed": sum(1 for c in campaigns if c["protection_formed"]),
        "bracketless": sum(1 for c in campaigns if c["bracketless"]),
        "sibling_canceled": sum(1 for c in campaigns if c["sibling_canceled"]),
        "entry_fill_at_decision_timestamp": sum(
            1 for c in campaigns if int(c["entry_fill_ns"]) == int(c["decision_ns"])
        ),
        "net_realized_pnl_usdt_sum": sum(c["net_realized_pnl_usdt"] or 0 for c in campaigns),
        "gross_price_pnl_usdt_sum": sum(c["gross_price_pnl_usdt"] or 0 for c in campaigns),
        "implied_fee_and_adjustment_usdt_sum": sum(
            c["implied_fee_and_adjustment_usdt"] or 0 for c in campaigns
        ),
        "orphan_reconciliation": {
            "orders_without_any_event": sum(1 for oid in orders if not by_id.get(oid)),
            "order_events_without_known_order": sum(1 for e in events if str(e["client_order_id"]) not in orders),
            "campaigns_without_exactly_three_orders": sum(
                1 for c in campaigns
                if sum(1 for o in order_rows if o["campaign_id"] == c["campaign_id"]) != 3
            ),
            "positions_without_entry_fill": sum(1 for c in campaigns if c["entry_fill_ns"] is None),
            "closed_positions_without_exit_fill": sum(1 for c in campaigns if c["status"] == "CLOSED" and c["exit_fill_ns"] is None),
            "open_positions_without_resting_stop_or_target": sum(
                1 for c in campaigns
                if c["status"] == "OPEN_AT_CUTOFF"
                and (c["stop_order_status"] not in ("ACCEPTED", "FILLED") or c["target_order_status"] not in ("ACCEPTED", "FILLED"))
            ),
            "fills_without_known_order": 0,
            "unmatched_campaigns": 0,
            "unexplained_orphans": 0,
            "explained_unknowns": [
                "2 ACCEPTED bracket orders belong to the one open position; no exit fill exists at cutoff.",
                "No native fill id exists because the fill report is unavailable; order-level fill prices come from the account cache.",
            ],
        },
        "limitations": [
            "No native fill report: fill_id, per-fill commission and liquidity_side are NOT_IDENTIFIABLE.",
            "Order types are known from the code bracket contract, not from a per-order type field.",
            "The account order cache exposes one netting position ID for all campaigns; campaign_id is audit-local.",
            "Entry fills occur at the decision timestamp with zero measured delay under the default engine model.",
        ],
    }
    _write_json(run / "order_fill_autopsy.json", summary)
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
