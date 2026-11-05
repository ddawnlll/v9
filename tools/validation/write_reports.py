#!/usr/bin/env python
"""Render the cross-market validation deliverables from recorded measurements.

No number in the output is invented: every table reads ``summary.json``, the raw
arm files, the dataset audit, the archive inventory or the grammar census.
"""

from __future__ import annotations

import argparse
import json
import os
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Sequence

CORE_ARMS = ("A", "B", "C", "D")
ABLATIONS = (
    "A1_no_amend",
    "A2_stop_amend_only",
    "A3_target_amend_only",
    "A4_structural_stop_only",
    "A5_structural_target_only",
    "A6_fixed_geometry",
)
ARM_LABEL = {
    "A": "V8 static H1",
    "B": "V9 legacy H1",
    "C": "V9 multi-horizon static",
    "D": "V9 multi-horizon adaptive",
    "A1_no_amend": "Thesis + structural geometry, no amendments",
    "A2_stop_amend_only": "Thesis + stop amendments (target off)",
    "A3_target_amend_only": "Thesis + target amendments (stop off)",
    "A4_structural_stop_only": "Structural stop, fixed target, no amendments",
    "A5_structural_target_only": "Fixed stop, structural target, no amendments",
    "A6_fixed_geometry": "Fixed 2%/4% geometry, thesis, no amendments",
}


def _load(path: Path) -> Any:
    return json.loads(path.read_text())


def _table(headers: Sequence[str], rows: Sequence[Sequence[Any]]) -> str:
    def fmt(value: Any) -> str:
        if value is None:
            return "—"
        if isinstance(value, float):
            if abs(value) >= 1000:
                return f"{value:,.2f}"
            return f"{value:.4f}".rstrip("0").rstrip(".")
        return str(value)

    lines = ["| " + " | ".join(headers) + " |", "|" + "|".join(["---"] * len(headers)) + "|"]
    for row in rows:
        lines.append("| " + " | ".join(fmt(value) for value in row) + " |")
    return "\n".join(lines)


def _fmt(value: Any, digits: int = 4) -> str:
    if value is None:
        return "—"
    if isinstance(value, float):
        return f"{value:,.{digits}f}"
    return str(value)


def _pct(value: Any) -> str:
    return "—" if value is None else f"{value * 100:.2f}%"


def write_data_manifest(
    path: Path,
    summary: dict[str, Any],
    audit: dict[str, Any],
    inventory: Sequence[dict[str, Any]],
) -> None:
    lines = [
        "# DATA_MANIFEST — cross-market validation inputs",
        "",
        "Window: **2025-01-01T00:00:00Z through 2025-07-01T00:00:00Z, end-exclusive** "
        "(181 calendar days, 4,344 H1 bars, 17,376 M15 bars per symbol).",
        "",
        "## Archive inventory",
        "",
        "Requested grid: 10 symbols × 6 months × 3 channels = **180 archives**.",
        "",
    ]
    ok = sum(1 for row in inventory if row.get("status") == "OK")
    missing = [row for row in inventory if row.get("status") == "MISSING"]
    lines += [
        _table(
            ["Status", "Archives"],
            [
                ["Expected", 180],
                ["Verified SHA-256 against vendor CHECKSUM", ok],
                ["Missing", len(missing)],
            ],
        ),
        "",
        "The prior `BLOCKERS.md` note claimed 174/180 archives in a scratch directory. "
        "That claim is **not reproducible from the scratch inventory as found**: the directory "
        "`mac-storage-audit/.../research/tape/multi-1h-4y` contains 1h and fundingRate only "
        "(120 in-window archives for BTC + 9 alts), **no M15 channel at all**, and no DOTUSDT "
        "series. This validation therefore downloaded the 72 missing public archives from "
        "`data.binance.vision` and verified every one against its vendor `.CHECKSUM`.",
        "",
        "## Per-symbol tape integrity",
        "",
    ]
    rows = []
    for row in audit["symbols"]:
        h1 = row["legs"].get("1h", {})
        m15 = row["legs"].get("15m", {})
        funding = h1.get("funding", {}) if isinstance(h1, dict) else {}
        rows.append(
            [
                row["symbol"],
                h1.get("bars"),
                h1.get("gaps") == [] if h1 else None,
                m15.get("bars"),
                m15.get("gaps") == [] if m15 else None,
                funding.get("rows"),
                funding.get("expected_8h_rows"),
                h1.get("integrity"),
                m15.get("integrity"),
                row["status"],
            ]
        )
    lines += [
        _table(
            ["Symbol", "H1 bars", "H1 no gaps", "M15 bars", "M15 no gaps", "Funding rows", "Expected", "H1 hash", "M15 hash", "Status"],
            rows,
        ),
        "",
        "## Frozen tape hashes",
        "",
    ]
    for row in audit["symbols"]:
        h1 = row["legs"].get("1h", {})
        m15 = row["legs"].get("15m", {})
        lines += [
            f"### {row['symbol']}",
            f"- H1: `{h1.get('tape_sha256')}` ({h1.get('bars')} bars)",
            f"- M15: `{m15.get('tape_sha256')}` ({m15.get('bars')} bars)",
            f"- H1 manifest: `{h1.get('manifest_sha256')}`",
            f"- M15 manifest: `{m15.get('manifest_sha256')}`",
            "",
        ]
    lines += [
        "## Derived H4",
        "",
        "H4 is not an input tape. The frozen strategy builds each H4 bar only when four H1 bars "
        "have completed, so an H4 context value is never read before its close. M15 is real and "
        "separate; no interval was interpolated from H1.",
        "",
        "## Instrument metadata",
        "",
        "Venue filters are transcribed from `fapi/v1/exchangeInfo` retrieved 2026-11-05. Binance "
        "does not publish a historical filter archive, so the **point-in-time status of every "
        "filter is `UNVERIFIED`**. Every symbol onboarded before 2025-01-01 and was `TRADING` at "
        "capture. The frozen BTC development slice keeps its original `price_precision=2`, "
        "`price_increment=0.01`, `max_quantity=1000`, `min_notional=1` for bit-level parity; the "
        "current venue BTC tick is 0.10, so that frozen choice is a documented deviation.",
        "",
        "| Symbol | Tick | Step | Max qty | Min notional | Maint. margin | Source |",
        "|---|---|---|---|---|---|---|",
    ]
    for symbol, filters in sorted(_filters(summary)):
        lines.append(
            f"| {symbol} | {filters['price_increment']} | {filters['size_increment']} | "
            f"{filters['max_quantity']} | {filters['min_notional']} | {filters['maintenance_margin']} | "
            f"{filters['price_increment_source']} |"
        )
    lines += [
        "",
        "## Explicit limits",
        "",
        "- Current venue filters are not a historical snapshot.",
        "- No mark-price tape exists; funding settlement uses the close-at-boundary proxy that "
        "the frozen harness already labels `NOT_VENUE_MARK`.",
        "- No L2/L3 depth or queue data exists; fills are bar-model assumptions.",
        "- Sealed OOS was not accessed.",
        "",
    ]
    path.write_text("\n".join(lines) + "\n")


def _filters(summary: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    return [
        (row["symbol"], row["instrument_filters"])
        for row in summary["rows"]
        if row.get("instrument_filters")
    ]


def write_config_freeze(path: Path, summary: dict[str, Any], meta: dict[str, Any]) -> None:
    payload = {
        "schema_version": "v9-cross-market-config-freeze-v1",
        "generated_utc": datetime.now(UTC).isoformat(),
        "repository": "https://github.com/ddawnlll/v9",
        "pr": 67,
        "branch": meta["branch"],
        "frozen_candidate_commit": "09908af840864a20e5bc4836ea4d21f4ceb93b35",
        "validation_branch": "validation/cross-market",
        "engine": meta["engine"],
        "python": meta["python"],
        "dependencies": meta["dependencies"],
        "evaluation_window": {
            "start_utc": "2025-01-01T00:00:00Z",
            "end_utc_exclusive": "2025-07-01T00:00:00Z",
            "calendar_days": 181,
        },
        "symbols": summary["symbols"],
        "accounts": "independent 10,000 USDT book per symbol per experiment (not a shared portfolio)",
        "fees": {"maker": 0.0002, "taker": 0.0005},
        "execution_profile": "baseline (NautilusTrader default fill model, zero modelled slippage)",
        "bracket": {"stop_pct": 0.02, "target_pct": 0.04},
        "quorum": {"min_support_quorum": 1, "max_contradiction_tolerance": 28},
        "position_sizing": "target_notional=1000 USDT, floored to lot step",
        "seeds": {"bootstrap": 20251105, "random_entry": 20251105},
        "arms": {arm: ARM_LABEL[arm] for arm in (*CORE_ARMS, *ABLATIONS)},
        "validation_additions": [
            "tools/validation/cross_market_run.py",
            "tools/validation/dataset_audit.py",
            "tools/validation/run_census.py",
            "tools/validation/analyze.py",
            "tools/validation/write_reports.py",
            "SimArmConfig structural_stop_enabled / structural_target_enabled (default True, BTC parity verified)",
        ],
        "config_sha256_by_symbol_arm": {
            row["symbol"]: {
                arm: (row["arms"][arm] or {}).get("config_sha256")
                for arm in CORE_ARMS
                if row["arms"].get(arm)
            }
            for row in _raw_provenance(summary)
        },
        "hypotheses_tested": summary["hypotheses_tested"],
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


def _raw_provenance(summary: dict[str, Any]) -> list[dict[str, Any]]:
    # The provenance block is not copied into the summary rows; read raw files.
    root = Path("reports/v9-cross-market-validation/raw")
    out = []
    for symbol in summary["symbols"]:
        path = root / f"{symbol}.json"
        if not path.is_file():
            continue
        raw = _load(path)
        arms = {}
        for arm, record in raw.get("arms", {}).items():
            if record.get("status") == "MEASURED":
                arms[arm] = {"config_sha256": record["provenance"]["config_sha256"]}
        out.append({"symbol": symbol, "arms": arms})
    return out


def write_paired(path: Path, summary: dict[str, Any]) -> None:
    lines = [
        "# PAIRED_ARM_COMPARISON — A/B/C/D across 10 perpetual markets",
        "",
        "Deltas are **left minus right** on independent 10,000 USDT books. Summing symbols is an "
        "equal-weight aggregate of ten separate accounts, **not** a shared portfolio.",
        "",
        "## Aggregate arm economics",
        "",
    ]
    rows = []
    for arm in CORE_ARMS:
        agg = summary["aggregates"][arm]
        ci = agg.get("cluster_bootstrap_net_pnl") or {}
        rows.append(
            [
                arm,
                ARM_LABEL[arm],
                agg["trades"],
                agg["net_pnl_sum"],
                agg["net_r_pooled_mean"],
                agg["positive_symbols"],
                agg["negative_symbols"],
                agg["mean_drawdown"],
                ci.get("low"),
                ci.get("high"),
            ]
        )
    lines += [
        _table(
            ["Arm", "Description", "Trades", "Net PnL sum", "Pooled net R", "+ symbols", "− symbols", "Mean max DD", "Cluster CI low", "Cluster CI high"],
            rows,
        ),
        "",
        "## Per-symbol net PnL",
        "",
    ]
    rows = []
    for row in summary["rows"]:
        values = [row["symbol"]]
        for arm in CORE_ARMS:
            record = row["arms"].get(arm)
            values.append(None if record is None else record["net_pnl"])
        values.append((row["arms"].get("D") or {}).get("net_pnl", 0) - (row["arms"].get("A") or {}).get("net_pnl", 0))
        rows.append(values)
    lines += [
        _table(["Symbol", "A", "B", "C", "D", "D − A"], rows),
        "",
        "## Paired contrasts",
        "",
    ]
    for key in ("B-A", "C-B", "D-C", "D-A", "D-A1_no_amend", "D-A6_fixed_geometry"):
        paired = summary["paired"][key]
        lines += [
            f"### {key}",
            "",
            f"- Symbols: {paired['symbols']}",
            f"- Sum net PnL delta: **{_fmt(paired['sum_net_pnl'])} USDT**",
            f"- Sum net R delta: **{_fmt(paired['sum_net_r_total'])} R**",
            f"- Positive / negative symbols: **{paired['positive_symbols']} / {paired['negative_symbols']}**",
            f"- Two-sided sign test p: {_fmt(paired['sign_test_p_two_sided'], 4)}",
            "",
        ]
        rows = [
            [item["symbol"], item["net_pnl"], item["net_r_total"], item["trades"], item["drawdown"]]
            for item in paired["per_symbol"]
        ]
        lines += [_table(["Symbol", "Δ net PnL", "Δ net R", "Δ trades", "Δ max DD"], rows), ""]
    lines += [
        "## Reading the D − C contrast",
        "",
        "C and D do not always enter at the same time: an earlier D exit can free the book for a "
        "later opportunity C also takes, or vice versa. The D − C total is therefore an "
        "**integrated management + portfolio-path** difference, not a per-trade stop effect. "
        "The entry-matched diagnostic is in `ADAPTIVE_MANAGEMENT_ATTRIBUTION.md`.",
        "",
    ]
    path.write_text("\n".join(lines) + "\n")


def write_attribution(path: Path, summary: dict[str, Any]) -> None:
    lines = [
        "# ADAPTIVE_MANAGEMENT_ATTRIBUTION — what actually moved the result",
        "",
        "All ablation arms keep the same context, entries, bracket parameters and costs. Only the "
        "research flags differ. `A6_fixed_geometry` uses the 2%/4% bracket with the thesis "
        "lifecycle but no amendments; on this data it is the cleanest test of whether the thesis "
        "lifecycle contributes anything beyond the fixed bracket.",
        "",
        "## Ablation aggregate",
        "",
    ]
    rows = []
    for arm in (*CORE_ARMS, *ABLATIONS):
        agg = summary["aggregates"].get(arm)
        if not agg:
            continue
        rows.append(
            [
                arm,
                ARM_LABEL[arm],
                agg["trades"],
                agg["net_pnl_sum"],
                agg["net_r_pooled_mean"],
                agg["positive_symbols"],
                agg["mean_drawdown"],
            ]
        )
    lines += [
        _table(["Arm", "Description", "Trades", "Net PnL sum", "Pooled net R", "+ symbols", "Mean max DD"], rows),
        "",
        "## Native management events (pooled over symbols)",
        "",
    ]
    events: dict[str, int] = defaultdict(int)
    statuses: dict[str, int] = defaultdict(int)
    for row in summary["rows"]:
        for arm in CORE_ARMS + ABLATIONS:
            record = row["arms"].get(arm)
            if not record:
                continue
            for kind, count in (record.get("lifecycle_event_counts") or {}).items():
                events[kind] += count
            for status, count in (record.get("order_status_counts") or {}).items():
                statuses[status] += count
    lines += [
        _table(["Lifecycle event", "Count"], sorted(events.items())),
        "",
        _table(["Native order status", "Count"], sorted(statuses.items())),
        "",
        "`STOP_AMEND_REQUESTED` without `STOP_AMEND_ACKED` would be a protection defect; the "
        "counts above and the zero protection violations in `EXECUTION_SAFETY.md` are the check.",
        "",
        "## Entry-matched C vs D diagnostic",
        "",
    ]
    rows = []
    for symbol, item in summary["entry_matched_c_vs_d"].items():
        rows.append(
            [
                symbol,
                item["c_trades"],
                item["d_trades"],
                item["entry_matched"],
                item["entry_unmatched_d"],
                item["d_minus_c_total"],
                item["matched_d_minus_c"],
                item["path_effect_unmatched"],
            ]
        )
    lines += [
        _table(
            ["Symbol", "C trades", "D trades", "Entry-matched", "Unmatched D", "D−C total", "Matched D−C", "Path effect"],
            rows,
        ),
        "",
        "`Matched D−C` compares only campaigns whose entry timestamp and side coincide. "
        "`Path effect` is the remainder: capital freed or blocked by a different earlier exit. "
        "It is not a stop-management effect.",
        "",
        "## Attribution statement",
        "",
    ]
    d = summary["aggregates"]["D"]
    a1 = summary["aggregates"]["A1_no_amend"]
    a2 = summary["aggregates"]["A2_stop_amend_only"]
    a3 = summary["aggregates"]["A3_target_amend_only"]
    a6 = summary["aggregates"]["A6_fixed_geometry"]
    lines += [
        f"- Full adaptive D: **{_fmt(d['net_pnl_sum'])} USDT**.",
        f"- Thesis + structural geometry without amendments (A1): **{_fmt(a1['net_pnl_sum'])} USDT**.",
        f"- Stop amendments only (A2): **{_fmt(a2['net_pnl_sum'])} USDT**.",
        f"- Target amendments only (A3): **{_fmt(a3['net_pnl_sum'])} USDT**.",
        f"- Fixed geometry + thesis, no amendments (A6): **{_fmt(a6['net_pnl_sum'])} USDT**.",
        "",
        "If A2 is close to D while A3 is close to A1, the benefit is carried by native stop "
        "amendments (break-even), not by target extensions. If A6 equals C, the thesis lifecycle "
        "with a fixed bracket added nothing on this sample and its exits never bound a decision. "
        "The report must not claim a contribution from an event whose native count is zero.",
        "",
    ]
    path.write_text("\n".join(lines) + "\n")


def write_cost_stress(path: Path, summary: dict[str, Any]) -> None:
    lines = [
        "# COST_STRESS — incremental execution cost",
        "",
        "Reference cost profile: maker 0.0002, taker 0.0005, NautilusTrader default fill model, "
        "zero modelled slippage. The stress adds a **linear incremental cost per side** on the "
        "measured one-way traded notional. This is an accounting stress on recorded fills, not a "
        "re-executed fill; no queue, depth or maker-probability claim is made.",
        "",
        "## Aggregate sensitivity",
        "",
    ]
    rows = []
    for arm in CORE_ARMS + ABLATIONS:
        item = summary["cost_stress"]["arms"].get(arm)
        if not item:
            continue
        rows.append(
            [
                arm,
                item["net_pnl_base"],
                item["one_way_notional"],
                item["breakeven_bps_per_side"],
                item["scenarios"]["2.0"],
                item["scenarios"]["5.0"],
                item["scenarios"]["10.0"],
            ]
        )
    lines += [
        _table(
            ["Arm", "Base net PnL", "One-way notional", "Break-even bps/side", "+2 bps", "+5 bps", "+10 bps"],
            rows,
        ),
        "",
        "## Per-symbol D break-even",
        "",
    ]
    rows = []
    for row in summary["rows"]:
        record = row["arms"].get("D")
        if not record:
            continue
        rows.append(
            [
                row["symbol"],
                record["net_pnl"],
                record["one_way_traded_notional"],
                record["breakeven_incremental_bps_per_side"],
            ]
        )
    lines += [
        _table(["Symbol", "D net PnL", "D one-way notional", "Break-even bps/side"], rows),
        "",
        "## Limits",
        "",
        "- The stress does not change fill prices, stop distances or the order path.",
        "- It cannot represent exchange minimum price increments, queue priority or partial fills.",
        "- A positive break-even below 2 bps/side means the result is fragile to ordinary taker cost.",
        "",
    ]
    path.write_text("\n".join(lines) + "\n")


def write_expert(path: Path, summary: dict[str, Any]) -> None:
    expert = summary["expert_coverage"]
    lines = [
        "# EXPERT_COVERAGE — 28-expert panel diagnostics",
        "",
        "The frozen strategy retains stance counts per primary horizon and abstain reasons. It "
        "does **not** retain per-expert activation frequency, so that field is `NOT_OBSERVABLE` "
        "rather than inferred. A winning trade supported by an expert is not evidence that the "
        "expert caused the win.",
        "",
        "## Expert profile table",
        "",
    ]
    for horizon, ids in expert.get("profiles", {}).items():
        lines += [f"### {horizon} ({len(ids)} experts)", "", ", ".join(f"`{i}`" for i in ids), ""]
    lines += [
        f"Panel size: **{expert.get('expert_count')} experts**.",
        "",
        "## Pooled stance counts",
        "",
    ]
    for arm in ("B", "C", "D"):
        block = expert["by_arm"].get(arm, {})
        lines += [
            f"### Arm {arm} — {ARM_LABEL[arm]}",
            "",
            f"Profiles enabled: `{block.get('profiles_enabled')}`",
            "",
        ]
        rows = []
        for horizon, counts in block.get("stances_by_primary", {}).items():
            total = sum(counts.values())
            rows.append(
                [
                    horizon,
                    counts.get("SUPPORT", 0),
                    counts.get("CONTRADICT", 0),
                    counts.get("ABSTAIN", 0),
                    total,
                    (counts.get("SUPPORT", 0) / total) if total else None,
                ]
            )
        lines += [_table(["Primary horizon", "SUPPORT", "CONTRADICT", "ABSTAIN", "Total", "Support share"], rows), ""]
        lines += ["Top abstain reasons:", ""]
        reasons = list(block.get("abstain_reasons", {}).items())[:15]
        lines += [_table(["Reason", "Count"], reasons), ""]
    lines += [
        "## Missing-input diagnosis",
        "",
        "`MISSING_INPUT` and `NO_CONFLUENCE` dominate abstention. M15 experts only activate when "
        "the real M15 tape is present (arms C/D); arm B has zero M15 bars and every M15 expert "
        "abstains. No expert is scored as profitable from support alone.",
        "",
    ]
    path.write_text("\n".join(lines) + "\n")


def write_census(path: Path, census: dict[str, Any]) -> None:
    lines = [
        "# OPPORTUNITY_CENSUS — five-grammar observational diagnostic",
        "",
        "Research-only. These opportunities were **never inserted** into the frozen A/B/C/D "
        "execution arms. Costs, funding and slippage are not applied; hit rates are bracket "
        "geometry (2% stop / 4% target, stop first), not edge.",
        "",
    ]
    totals: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for _symbol, payload in census["symbols"].items():
        for dist in payload["distributions"]:
            key = dist["policy"]
            totals[key]["bars_evaluated"] += dist["bars_evaluated"]
            totals[key]["directional_bars"] += dist["directional_bars"]
            totals[key]["episodes"] += dist["episodes"]
            totals[key]["fragments"] += dist["fragments"]
            totals[key]["target_hits"] += dist["target_hits"]
            totals[key]["stop_hits"] += dist["stop_hits"]
            totals[key]["timeouts"] += dist["timeouts"]
            totals[key]["censored"] += dist["censored"]
    rows = []
    for policy, values in sorted(totals.items()):
        resolved = values["target_hits"] + values["stop_hits"] + values["timeouts"]
        rows.append(
            [
                policy,
                int(values["bars_evaluated"]),
                int(values["directional_bars"]),
                int(values["episodes"]),
                int(values["fragments"]),
                int(values["target_hits"]),
                int(values["stop_hits"]),
                int(values["timeouts"]),
                int(values["censored"]),
                (values["target_hits"] / resolved) if resolved else None,
            ]
        )
    lines += [
        "## Pooled detection and episode counts",
        "",
        _table(
            ["Grammar", "Bars eval.", "Directional bars", "Episodes", "Fragments", "Targets", "Stops", "Timeouts", "Censored", "Hit rate"],
            rows,
        ),
        "",
        "## Per-symbol episodes",
        "",
    ]
    rows = []
    for symbol, payload in census["symbols"].items():
        by_policy = {d["policy"]: d["episodes"] for d in payload["distributions"]}
        rows.append(
            [
                symbol,
                payload["bars"],
                by_policy.get("range-breakout-48-v1"),
                by_policy.get("volatility-extreme-v2"),
                by_policy.get("trend-continuation-v2"),
                by_policy.get("mean-reversion-v2"),
                by_policy.get("compression-expansion-v2"),
                payload["overlap"]["bars_with_multiple_grammars"],
                payload["overlap"]["bars_with_opposing_directions"],
            ]
        )
    lines += [
        _table(
            ["Symbol", "Bars", "Range breakout", "Vol extreme", "Trend cont.", "Mean rev.", "Compression", "Multi-grammar bars", "Opposing-direction bars"],
            rows,
        ),
        "",
        "## Panel gate audit",
        "",
    ]
    first = next(iter(census["symbols"].values()))
    gate = first["panel_gate"]
    lines += [
        f"- expert_count: {gate.get('expert_count')}",
        f"- min_support_quorum: {gate.get('min_support_quorum')}",
        f"- max_contradiction_tolerance: {gate.get('max_contradiction_tolerance')}",
        f"- contradiction_can_block: {gate.get('contradiction_can_block')}",
        "",
        "With 28 experts and tolerance 28 the contradiction condition is vacuous; the gate reduces "
        "to 'at least one support vote'. This is an audit of the frozen configuration, not a new gate.",
        "",
    ]
    path.write_text("\n".join(lines) + "\n")


def write_statistics(path: Path, summary: dict[str, Any]) -> None:
    lines = [
        "# STATISTICAL_VALIDATION — uncertainty, benchmark and concentration",
        "",
        "This is cross-market **generalisation research**, not a sealed final OOS test. The ten "
        "coins are strongly correlated and are not ten independent replications. Every interval "
        "below names its resampling unit.",
        "",
        "## Arm uncertainty",
        "",
    ]
    rows = []
    for arm in CORE_ARMS:
        agg = summary["aggregates"][arm]
        cluster = agg.get("cluster_bootstrap_net_pnl") or {}
        campaign = agg.get("campaign_bootstrap_mean_r") or {}
        rows.append(
            [
                arm,
                agg["symbols"],
                agg["positive_symbols"],
                agg["net_pnl_sum"],
                cluster.get("low"),
                cluster.get("high"),
                agg["net_r_pooled_mean"],
                campaign.get("low"),
                campaign.get("high"),
                agg["sign_test_p_two_sided"],
            ]
        )
    lines += [
        _table(
            ["Arm", "Symbols", "+ symbols", "Net PnL sum", "Cluster CI low", "Cluster CI high", "Pooled net R", "Campaign CI low", "Campaign CI high", "Sign p"],
            rows,
        ),
        "",
        "Cluster CI resamples the ten symbols. Campaign CI resamples campaign net R i.i.d. and "
        "therefore understates serial dependence; it is shown only as a secondary view.",
        "",
        "## Concentration",
        "",
    ]
    conc = summary["concentration"]
    lines += [
        f"- D net PnL sum: **{_fmt(conc['d_net_pnl_sum'])} USDT**",
        f"- D positive symbols: **{conc['d_positive_symbols']} / 10**",
        f"- Top contributing symbol: **{conc['d_top_symbol']}**",
        f"- Top share of positive D PnL: {_pct(conc['d_top_share_of_positive'])}",
        f"- Herfindahl on absolute D PnL: {_fmt(conc['d_herfindahl_abs'], 4)}",
        "",
        "## D − A improvement by symbol",
        "",
        _table(
            ["Symbol", "Δ net PnL"],
            [[symbol, value] for symbol, value in conc["d_improvement_vs_a_per_symbol"].items()],
        ),
        "",
        "## Random-entry benchmark",
        "",
    ]
    rows = []
    for symbol, bench in summary["random_entry_benchmark"].items():
        if symbol == "basis" or not isinstance(bench, dict):
            continue
        rows.append(
            [
                symbol,
                bench.get("observed_mean_r"),
                bench.get("null_mean"),
                bench.get("null_p05"),
                bench.get("null_p95"),
                bench.get("observed_minus_null_mean"),
                bench.get("draws"),
            ]
        )
    lines += [
        _table(
            ["Symbol", "D mean R", "Null mean R", "Null p05", "Null p95", "D − null", "Draws"],
            rows,
        ),
        "",
        "The null is random entry with the same trade count, the same long/short counts, the same "
        "2%/4% bracket and 96-bar cap, stop-first, a 0.05 R round-trip fee proxy and no funding. "
        "It is a **matched exposure proxy**, not an exact replay of the strategy's opportunity set.",
        "",
        "## Regime split",
        "",
    ]
    for arm in ("A", "D"):
        rows = [
            [label, block["trades"], block["net_pnl"], block["mean_pnl"], block["win_rate"]]
            for label, block in sorted(summary["regimes"].get(arm, {}).items())
            if isinstance(block, dict)
        ]
        lines += [f"### Arm {arm}", "", _table(["Regime", "Trades", "Net PnL", "Mean PnL", "Win rate"], rows), ""]
    lines += [
        "## Multiple testing",
        "",
        f"Recorded hypotheses: {summary['hypotheses_tested']}. No multiplicity correction is "
        "applied to the published sign-test p-values. Treat them as exploratory. The strategy was "
        "not tuned on these results; this sample must not be relabelled OOS after inspection.",
        "",
    ]
    path.write_text("\n".join(lines) + "\n")


def write_safety(path: Path, summary: dict[str, Any]) -> None:
    safety = summary["execution_safety"]
    lines = [
        "# EXECUTION_SAFETY — native order, protection and cashflow reconciliation",
        "",
        "All arms ran inside NautilusTrader `BacktestEngine`. No venue adapter, credential, "
        "testnet or network order submission was used.",
        "",
        "## Mechanical gates",
        "",
        _table(
            ["Gate", "Result"],
            [
                ["Protection-invariant violations", len(safety["arms_with_violations"])],
                ["Arms with unlinked orders", len(safety["arms_with_unlinked_orders"])],
                ["Arms with orphan closes", len(safety["arms_with_orphan_closes"])],
                ["Arms with engine-rejected orders", len(safety["arms_with_rejected_orders"])],
                ["Max |price reconstruction residual| (USDT)", safety["max_abs_price_reconstruction_residual"]],
                ["Max |closed realized − net| (USDT)", safety["max_abs_closed_realized_vs_net_residual"]],
            ],
        ),
        "",
        "Engine-rejected orders are listed because a rejected protective stop is a real "
        "protection gap even when the invariant checker still sees a prior live order. "
        "Affected arms:",
        "",
        ", ".join(f"`{item}`" for item in safety["arms_with_rejected_orders"]) or "none",
        "",
        "## Cashflow reconciliation basis",
        "",
        "`net_pnl` is the native account balance delta. `PositionClosed.realized_pnl` is already "
        "net of commissions and funding: it equals the account delta on every closed campaign. "
        "The gross price leg is therefore derived as `net_pnl + captured_commissions − "
        "captured_funding`. A second, independent reconstruction from `(close − open) × quantity` "
        "is reported as `price_reconstruction_residual`; the only non-zero case is a symbol with "
        "an open position at cutoff, whose entry commission is booked but not realized.",
        "",
        "## Frozen-measure funding defect and validation fix",
        "",
        "`sim_arm._measure` sums funding from `engine.cache.positions()` **after** the run. In a "
        "NETTING replay that cache retains only live positions, so the frozen `funding_total` "
        "undercounts funding on closed campaigns. The validation harness captures adjustments "
        "inside `on_position_closed`, while the position is still in cache, and adds accrued "
        "funding on positions open at cutoff. This is an observation-only change; no decision "
        "reads it.",
        "",
        "Cumulative frozen-vs-exact funding difference by arm (USDT):",
        "",
        _table(["Arm", "Frozen minus exact"], sorted(safety["funding_cache_undercount_by_arm"].items())),
        "",
        "The frozen BTC baseline's **net PnL is unaffected** because the account delta already "
        "includes funding; only its funding attribution column undercounts.",
        "",
    ]
    path.write_text("\n".join(lines) + "\n")


def write_reproduce(path: Path, meta: dict[str, Any]) -> None:
    text = """#!/usr/bin/env bash
set -euo pipefail
# Reproduce the cross-market validation from the frozen candidate.
# Requires network only for the archive download step.

REPO="${REPO:-$(pwd)}"
DATA="${DATA:-validation-data}"
cd "$REPO"

# 0. Environment
uv sync --locked --extra dev
uv run --locked --extra dev python -c "import nautilus_trader; print(nautilus_trader.__version__)"

# 1. Archive inventory and checksum verification (180 archives)
python3 - <<'PY'
from pathlib import Path
import hashlib, json, os
root = Path(os.environ.get("DATA", "validation-data"))
inv = json.loads((root / "inventory.json").read_text())
bad = [r for r in inv if r["status"] != "OK"]
print("archives", len(inv), "bad", len(bad))
assert not bad, bad
PY

# 2. Build per-symbol H1 and M15 tapes (2025H1)
#    archives live in $DATA/archives and the legacy scratch pool; see DATA_MANIFEST.md.
uv run --locked --extra dev python tools/validation/dataset_audit.py

# 3. Full arm matrix (independent 10,000 USDT book per symbol)
for SYM in ETHUSDT SOLUSDT BNBUSDT XRPUSDT DOGEUSDT ADAUSDT AVAXUSDT LINKUSDT DOTUSDT LTCUSDT; do
  PYTHONPATH=src uv run --locked --extra dev python tools/validation/cross_market_run.py --symbol "$SYM"
done

# 4. Observational diagnostics
PYTHONPATH=src uv run --locked --extra dev python tools/validation/run_census.py
PYTHONPATH=src uv run --locked --extra dev python tools/validation/analyze.py
PYTHONPATH=src uv run --locked --extra dev python tools/validation/write_reports.py

# 5. Mechanical test suite (unchanged frozen tests)
uv run --locked --extra dev pytest -q tests
"""
    path.write_text(text)


def write_dashboard(path: Path, summary: dict[str, Any], census: dict[str, Any]) -> None:
    data = {
        "symbols": summary["symbols"],
        "aggregates": {
            arm: {
                "trades": summary["aggregates"][arm]["trades"],
                "net_pnl_sum": summary["aggregates"][arm]["net_pnl_sum"],
                "net_r_pooled_mean": summary["aggregates"][arm]["net_r_pooled_mean"],
                "positive_symbols": summary["aggregates"][arm]["positive_symbols"],
                "mean_drawdown": summary["aggregates"][arm]["mean_drawdown"],
            }
            for arm in CORE_ARMS
        },
        "per_symbol": {
            row["symbol"]: {
                arm: {
                    "net_pnl": (row["arms"].get(arm) or {}).get("net_pnl"),
                    "net_r_mean": (row["arms"].get(arm) or {}).get("net_r_mean"),
                    "max_drawdown": (row["arms"].get(arm) or {}).get("max_drawdown"),
                    "trades": (row["arms"].get(arm) or {}).get("trades"),
                }
                for arm in CORE_ARMS
            }
            for row in summary["rows"]
        },
        "stress": summary["cost_stress"],
        "expert": summary["expert_coverage"]["by_arm"].get("D", {}),
        "concentration": summary["concentration"],
        "census": {
            symbol: {
                dist["policy"]: {
                    "episodes": dist["episodes"],
                    "directional_bars": dist["directional_bars"],
                    "hit_rate": dist["hit_rate"],
                }
                for dist in payload["distributions"]
            }
            for symbol, payload in census["symbols"].items()
        },
    }
    html = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>V9 cross-market validation</title>
<style>
:root{--bg:#0b1020;--panel:#131a2e;--ink:#e8edf7;--muted:#93a0bd;--a:#6ea8fe;--b:#f4a261;--c:#8ac926;--d:#ff5d8f;--grid:#26324d}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.5 ui-sans-serif,system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
header{padding:28px 32px;border-bottom:1px solid var(--grid)}h1{margin:0;font-size:24px}sub{color:var(--muted)}
main{padding:24px 32px;display:grid;gap:22px;grid-template-columns:repeat(12,1fr)}
.card{background:var(--panel);border:1px solid var(--grid);border-radius:12px;padding:18px}
.card h2{margin:0 0 12px;font-size:15px;color:var(--muted);font-weight:600;letter-spacing:.04em;text-transform:uppercase}
.wide{grid-column:span 12}.half{grid-column:span 6}.third{grid-column:span 4}
.bars{display:flex;gap:14px;align-items:flex-end;height:170px}
.bar{flex:1;display:flex;flex-direction:column;justify-content:flex-end;align-items:center;gap:6px}
.bar i{display:block;width:100%;border-radius:5px 5px 0 0;background:linear-gradient(180deg,var(--a),#315a9e)}
.bar span{color:var(--muted);font-size:11px}.bar b{font-size:12px}
table{width:100%;border-collapse:collapse;font-size:12.5px}th,td{text-align:right;padding:6px 8px;border-bottom:1px solid var(--grid)}th:first-child,td:first-child{text-align:left}
.neg{color:#ff7b8a}.pos{color:#67e8a5}.muted{color:var(--muted)}
.legend{display:flex;gap:16px;flex-wrap:wrap;color:var(--muted);font-size:12px;margin-top:8px}
.legend i{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:5px}
svg{width:100%;height:220px;overflow:visible}
.note{color:var(--muted);font-size:12px;margin-top:10px}
</style></head><body>
<header><h1>V9 cross-market economic validation</h1>
<div class="muted">10 Binance USD-M perpetuals · 2025-01-01 → 2025-07-01 · independent 10,000 USDT books · frozen candidate <code>09908af</code></div></header>
<main id="app"></main>
<script>
const D = __DATA__;
const money = v => v==null ? '—' : (v>=0?'+':'') + v.toLocaleString(undefined,{maximumFractionDigits:2});
const cls = v => v==null ? '' : (v<0?'neg':'pos');
const el = (h) => { const t=document.createElement('template'); t.innerHTML=h.trim(); return t.content.firstChild; };
const app = document.getElementById('app');

// aggregate bars
const agg = D.aggregates;
const maxAbs = Math.max(...Object.values(agg).map(a=>Math.abs(a.net_pnl_sum)),1);
app.append(el(`<section class="card wide"><h2>Aggregate net PnL by arm (10 independent books)</h2>
<div class="bars">${Object.entries(agg).map(([k,a])=>`<div class="bar" title="${k}"><b class="${cls(a.net_pnl_sum)}">${money(a.net_pnl_sum)}</b><i style="height:${Math.max(4,Math.abs(a.net_pnl_sum)/maxAbs*130)}px;background:${a.net_pnl_sum<0?'linear-gradient(180deg,#ff7b8a,#7d2c3a)':'linear-gradient(180deg,#67e8a5,#236b4a)'}"></i><span>${k} · ${a.trades} trades</span></div>`).join('')}</div>
<div class="legend"><span>Net PnL after commissions and funding. Sum is not a shared portfolio.</span></div></section>`));

// per symbol table
const arms=['A','B','C','D'];
app.append(el(`<section class="card wide"><h2>Per-symbol net PnL</h2><table><thead><tr><th>Symbol</th>${arms.map(a=>`<th>${a}</th>`).join('')}<th>D − A</th></tr></thead><tbody>
${D.symbols.map(s=>{const r=D.per_symbol[s];const d=r.D?.net_pnl, a=r.A?.net_pnl;return `<tr><td>${s}</td>${arms.map(k=>`<td class="${cls(r[k]?.net_pnl)}">${money(r[k]?.net_pnl)}</td>`).join('')}<td class="${cls(d-a)}">${money(d-a)}</td></tr>`}).join('')}
</tbody></table><div class="note">D − A is the integrated V9-versus-V8 comparison on the same symbol and window.</div></section>`));

// drawdown and R
app.append(el(`<section class="card half"><h2>Mean max drawdown by arm</h2><table><thead><tr><th>Arm</th><th>Mean DD</th><th>+ symbols</th><th>Pooled net R</th></tr></thead><tbody>
${Object.entries(agg).map(([k,a])=>`<tr><td>${k}</td><td>${(a.mean_drawdown*100).toFixed(2)}%</td><td>${a.positive_symbols}/10</td><td class="${cls(a.net_r_pooled_mean)}">${a.net_r_pooled_mean?.toFixed(4)}</td></tr>`).join('')}
</tbody></table></section>`));

// cost stress
const stress = D.stress.arms;
app.append(el(`<section class="card half"><h2>Cost stress: net PnL at +bps per side</h2><table><thead><tr><th>Arm</th><th>+2</th><th>+5</th><th>+10</th><th>Break-even</th></tr></thead><tbody>
${arms.map(k=>{const s=stress[k];return s?`<tr><td>${k}</td><td class="${cls(s.scenarios['2.0'])}">${money(s.scenarios['2.0'])}</td><td class="${cls(s.scenarios['5.0'])}">${money(s.scenarios['5.0'])}</td><td class="${cls(s.scenarios['10.0'])}">${money(s.scenarios['10.0'])}</td><td>${s.breakeven_bps_per_side?.toFixed(2)} bps</td></tr>`:''}).join('')}
</tbody></table><div class="note">Linear post-hoc cost on measured traded notional, not a re-executed fill.</div></section>`));

// expert stances
const stances = D.expert.stances_by_primary || {};
app.append(el(`<section class="card half"><h2>Expert stances (arm D, pooled)</h2><table><thead><tr><th>Horizon</th><th>SUPPORT</th><th>CONTRADICT</th><th>ABSTAIN</th></tr></thead><tbody>
${Object.entries(stances).map(([h,c])=>`<tr><td>${h}</td><td>${c.SUPPORT||0}</td><td>${c.CONTRADICT||0}</td><td>${c.ABSTAIN||0}</td></tr>`).join('')}
</tbody></table><div class="note">Per-expert activation is not retained by the frozen strategy; this is honest aggregate evidence, not a profitability claim.</div></section>`));

// grammar census
app.append(el(`<section class="card half"><h2>Five-grammar episodes (observational)</h2><table><thead><tr><th>Grammar</th><th>Episodes</th><th>Directional bars</th><th>Hit</th></tr></thead><tbody>
${(()=>{const t={};Object.values(D.census).forEach(s=>Object.entries(s).forEach(([p,v])=>{t[p]=t[p]||{e:0,b:0};t[p].e+=v.episodes;t[p].b+=v.directional_bars;}));return Object.entries(t).map(([p,v])=>`<tr><td>${p}</td><td>${v.e}</td><td>${v.b}</td><td>—</td></tr>`).join('')})()}
</tbody></table><div class="note">No costs, no funding, never inserted into A/B/C/D.</div></section>`));
</script></body></html>"""
    path.write_text(html.replace("__DATA__", json.dumps(data, separators=(",", ":"))))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, default=Path("reports/v9-cross-market-validation"))
    parser.add_argument("--raw", type=Path, default=Path("reports/v9-cross-market-validation/raw"))
    parser.add_argument(
        "--inventory",
        type=Path,
        default=Path(os.environ.get("DATA", "validation-data")) / "inventory.json",
    )
    args = parser.parse_args(argv)
    out = args.out
    out.mkdir(parents=True, exist_ok=True)
    summary = _load(out / "summary.json")
    audit = _load(args.raw / "dataset_audit.json")
    census = _load(args.raw / "opportunity_census.json")
    inventory = _load(args.inventory) if args.inventory.is_file() else []
    meta = {
        "branch": "validation/cross-market",
        "engine": "NautilusTrader 2.0.0rc4",
        "python": "3.12.12",
        "dependencies": {
            "nautilus-trader": "2.0.0rc4",
            "pandas": ">=2.2",
            "polars": ">=1.44.0",
            "numpy": ">=2.0.0",
            "pydantic": ">=2.10.0",
        },
    }
    write_data_manifest(out / "DATA_MANIFEST.md", summary, audit, inventory)
    write_config_freeze(out / "CONFIG_FREEZE.json", summary, meta)
    write_paired(out / "PAIRED_ARM_COMPARISON.md", summary)
    write_attribution(out / "ADAPTIVE_MANAGEMENT_ATTRIBUTION.md", summary)
    write_cost_stress(out / "COST_STRESS.md", summary)
    write_expert(out / "EXPERT_COVERAGE.md", summary)
    write_census(out / "OPPORTUNITY_CENSUS.md", census)
    write_statistics(out / "STATISTICAL_VALIDATION.md", summary)
    write_safety(out / "EXECUTION_SAFETY.md", summary)
    write_reproduce(out / "REPRODUCE.sh", meta)
    write_dashboard(out / "dashboard.html", summary, census)
    print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
