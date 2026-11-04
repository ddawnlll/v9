"""Arm runners: one native replay path, one causal screening path.

The native path delegates to the existing :func:`v8_next.audit.pipeline.run_native_audit`
(the same replay that produces the baseline). The screening path uses
:mod:`v8_next.research.screen`. No second execution engine exists.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

from v8_next.research import metrics as research_metrics
from v8_next.research import screen
from v8_next.research.arms import Arm, get_arm


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str), encoding="utf-8")


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, sort_keys=True, default=str) + "\n")


SCREEN_SUMMARY_KEYS = {
    "net_key": "net_r",
    "gross_key": "gross_r",
    "pnl_key": "net_usdt",
    "holding_key": "bars_measured",
}


def _summarize_screen(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Summarize counterfactual rows with the counterfactual column names."""
    return research_metrics.summarize(rows, **SCREEN_SUMMARY_KEYS)


def run_native_arm(
    *,
    arm_id: str,
    tape: Path | str,
    output_dir: Path | str,
    initial_balance: Decimal = Decimal("10000"),
    maker_fee: Decimal = Decimal("0.0002"),
    taker_fee: Decimal = Decimal("0.0005"),
    skip_equivalence: bool = True,
) -> dict[str, Any]:
    """Execute one native arm through the existing audit pipeline."""
    from v8_next.audit.pipeline import run_native_audit

    arm = get_arm(arm_id)
    if arm.kind != "native":
        raise ValueError(f"arm {arm_id} is a {arm.kind} arm; use run_screen_arm")
    out = Path(output_dir) / arm.arm_id
    result = run_native_audit(
        tape=Path(tape),
        output_dir=out,
        instrument="BTCUSDT",
        expert="volume_confirmed_breakout",
        execution_profile=arm.execution_profile,
        initial_balance=initial_balance,
        maker_fee=maker_fee,
        taker_fee=taker_fee,
        skip_equivalence=skip_equivalence,
        config_overrides=arm.config_overrides or None,
    )
    # Record the intervention and the command that actually reproduces this arm.
    manifest_path = out / "run_manifest.json"
    if manifest_path.is_file():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["research_arm"] = {
            "arm_id": arm.arm_id,
            "description": arm.description,
            "config_overrides": {k: str(v) for k, v in arm.config_overrides.items()},
            "execution_profile": arm.execution_profile,
            "default_off": True,
        }
        manifest.setdefault("reproduction", {})["command"] = (
            f"v8-next research native-arm --arm {arm.arm_id} --tape {Path(tape)} "
            f"--output-dir {Path(output_dir)}"
        )
        manifest["reproduction"]["note"] = (
            "Runs the same replay through the research runner so the arm's config "
            "overrides are applied; the plain `v8-next audit` command is the arm-off baseline."
        )
        manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    lifecycles = screen.read_jsonl(out / "trade_lifecycles.jsonl")
    summary = research_metrics.summarize(lifecycles)
    _write_json(out / "research_arm.json", {**arm.to_dict(), "native_summary": result["summary"]})
    _write_json(out / "research_metrics.json", summary)
    return {
        "arm": arm.to_dict(),
        "run_dir": str(out),
        "summary": result["summary"],
        "metrics": summary,
        "gates": {g["gate_id"]: g["status"] for g in result["gates"]},
        "validation": result["validation"]["status"],
    }


def _confirmed_next_bar_rows(
    opportunities: list[dict[str, Any]],
    bars: list[screen.Bar],
    *,
    fee_model: screen.FeeModel,
) -> list[dict[str, Any]]:
    """D1: enter only if the next closed bar continues beyond the detection level."""
    from bisect import bisect_right

    ends = [bar.end_ns for bar in bars]
    rows: list[dict[str, Any]] = []
    for opportunity in opportunities:
        if opportunity.get("event_type") not in (None, "OPPORTUNITY_DETECTED"):
            continue
        detected_ns = int(opportunity.get("event_time_ns") or 0)
        level = opportunity.get("entry_price")
        if not detected_ns or level is None:
            continue
        direction = 1 if str(opportunity.get("direction")).upper().endswith("LONG") else -1
        index = bisect_right(ends, detected_ns)
        if index >= len(bars):
            continue
        bar = bars[index]
        confirmed = (bar.close >= float(level)) if direction > 0 else (bar.close <= float(level))
        row: dict[str, Any] = {
            "opportunity_id": opportunity.get("opportunity_id"),
            "direction": opportunity.get("direction"),
            "detected_ns": detected_ns,
            "policy": "CONFIRM_NEXT_BAR",
            "state": "ARMED" if confirmed else "INVALIDATED",
            "detection_price": float(level),
            "confirmation_close": bar.close,
            "entry_ns": bar.end_ns if confirmed else None,
            "entry_price": bar.close if confirmed else None,
        }
        if not confirmed:
            row.update({"outcome": "NOT_CONFIRMED", "net_r": None, "net_pnl_usdt": None})
            rows.append(row)
            continue
        stop, target = screen.static_bracket(bar.close, direction)
        result = screen.evaluate_path(
            bars,
            entry_ns=bar.end_ns,
            entry=bar.close,
            direction=direction,
            stop=stop,
            target=target,
            policy="STATIC",
            fee_model=fee_model,
        )
        row.update(result)
        row["evidence_class"] = "MODEL_DERIVED"
        rows.append(row)
    return rows


def run_screen_arm(
    *,
    arm_id: str,
    baseline_run: Path | str,
    tape: Path | str,
    output_dir: Path | str,
    seed: int = 7,
    resamples: int = 1500,
    random_seeds: tuple[int, ...] = (7, 11, 13, 17, 19),
) -> dict[str, Any]:
    """Evaluate one screening arm against the shared baseline evidence."""
    arm: Arm = get_arm(arm_id)
    if arm.kind != "screen":
        raise ValueError(f"arm {arm_id} is a native arm; use run_native_arm")
    run = screen.load_run(baseline_run)
    bars = screen.load_bars(tape)
    fee_model = screen.FeeModel()
    lifecycles = run["lifecycles"]
    opportunities = run["opportunities"]

    payload: dict[str, Any] = {"arm": arm.to_dict(), "dataset": {"bars": len(bars)}}
    rows: list[dict[str, Any]] = []

    policy = arm.screen_policy
    if policy in {"STATIC", "BREAKEVEN_1R", "PARTIAL_50_1R", "TIME_STOP"}:
        rows = screen.exit_policy_rows(lifecycles, bars, policy, fee_model=fee_model)
        # Consistency check: modelled STATIC vs the observed native exit.
        if policy == "STATIC":
            pairs = [
                (row.get("native_net_r"), row.get("net_r"))
                for row in rows
                if row.get("native_net_r") is not None and row.get("net_r") is not None
            ]
            payload["model_vs_native"] = research_metrics.paired_delta_ci(
                pairs, seed=seed, resamples=resamples
            )
        pairs_policy = [
            (row.get("native_net_r"), row.get("net_r"))
            for row in rows
            if row.get("native_net_r") is not None and row.get("net_r") is not None
        ]
        payload["paired_vs_observed"] = research_metrics.paired_delta_ci(
            pairs_policy, seed=seed + 101, resamples=resamples
        )
    elif policy in {"SAME_BAR", "NEXT_BAR_ENTRY"}:
        rows = screen.opportunity_path_rows(
            opportunities, bars, entry_offset_bars=arm.entry_offset_bars, fee_model=fee_model
        )
        payload["coverage"] = {
            "opportunities": len(rows),
            "entered": sum(1 for row in rows if row.get("net_r") is not None),
        }
    elif policy == "CONFIRM_NEXT_BAR":
        rows = _confirmed_next_bar_rows(opportunities, bars, fee_model=fee_model)
        payload["coverage"] = {
            "opportunities": len(rows),
            "confirmed": sum(1 for row in rows if row.get("entry_ns") is not None),
            "invalidated": sum(1 for row in rows if row.get("state") == "INVALIDATED"),
        }
    elif policy in {"RETEST_LIMIT_TTL", "RETEST_TTL"}:
        rows = screen.retest_rows(opportunities, bars, fee_model=fee_model)
        payload["coverage"] = {
            "opportunities": len(rows),
            "filled": sum(1 for row in rows if row.get("entry_price") is not None),
            "expired": sum(1 for row in rows if row.get("outcome") == "EXPIRED"),
            "invalidated": sum(1 for row in rows if row.get("outcome") == "INVALIDATED"),
        }
    elif policy == "EXPERT_STANCE_CONDITIONING":
        rows = screen.opportunity_path_rows(opportunities, bars, fee_model=fee_model)
        stances = screen.load_stances(run["events"])
        for row in rows:
            stance = stances.get(str(row.get("opportunity_id")), {})
            row["selected_expert_stance"] = stance.get("volume_confirmed_breakout")
            row["support_count"] = sum(1 for v in stance.values() if v == "SUPPORT")
            row["contradict_count"] = sum(1 for v in stance.values() if v == "CONTRADICT")
        payload["by_stance"] = {
            label: _summarize_screen(
                [row for row in rows if str(row.get("selected_expert_stance") or "NO_STANCE") == label]
            )
            for label in ("SUPPORT", "ABSTAIN", "CONTRADICT", "NO_STANCE")
        }
    elif policy == "MATCHED_RANDOM":
        pool = screen.opportunity_path_rows(opportunities, bars, fee_model=fee_model)
        for row in pool:
            row["bucket"] = screen.volatility_bucket(row, bars)
            row["match_key"] = f"{row.get('direction')}|{row.get('bucket')}"
        draws = []
        sampled_rows: list[dict[str, Any]] = []
        for control_seed in random_seeds:
            sample = screen.matched_random_sample(
                pool, size=45, seed=control_seed, key="match_key"
            )
            draws.append(
                {
                    "seed": control_seed,
                    "n": len(sample),
                    **_summarize_screen([dict(row) for row in sample]),
                }
            )
            sampled_rows.extend({**dict(row), "control_seed": control_seed} for row in sample)
        # Row-level evidence is pooled across seeds and tagged, so a reader can
        # recompute any individual draw without trusting this summary.
        rows = sampled_rows
        payload["random_control_draws"] = draws
        expectations = [
            draw["net_expectancy_r"] for draw in draws if draw["net_expectancy_r"] is not None
        ]
        payload["random_control_net_expectancy_distribution"] = {
            "min": min(expectations) if expectations else None,
            "median": research_metrics.median(expectations),
            "max": max(expectations) if expectations else None,
            "draws": len(expectations),
        }
    else:
        raise ValueError(f"arm {arm_id} has no screening implementation ({policy!r})")

    # Screen rows use the counterfactual column names (net_r/gross_r/net_usdt);
    # native rows use the audit ledger names. Map explicitly, never guess.
    summary = _summarize_screen(rows)
    payload["metrics"] = summary
    payload["n_rows"] = len(rows)
    out = Path(output_dir) / arm.arm_id
    _write_jsonl(out / "arm_rows.jsonl", rows)
    _write_json(out / "arm_metrics.json", {**payload, "arm_id": arm.arm_id})
    return payload


# --------------------------------------------------------------------------- #
# D4 continuation: persistent retest/reclaim lifecycle
# --------------------------------------------------------------------------- #
D4_POLICY_ID = "D4_RETEST_RECLAIM_LIFECYCLE"


def _detected_opportunities(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        row
        for row in rows
        if row.get("event_type") in (None, "OPPORTUNITY_DETECTED")
        and row.get("entry_price") is not None
    ]


def run_d4_screen(
    *,
    baseline_run: Path | str,
    tape: Path | str,
    output_dir: Path | str,
) -> dict[str, Any]:
    """Replay the D4 state machine over the shared detection tape (no orders).

    The screen is the causal *state* reference only: it advances the exact
    policy the native strategy consumes, but it owns no execution, no fees and
    no PnL, so its counts are NOT native trading results.
    """
    from v8_next.research.lifecycle import (
        INVALIDATION_R,
        POLICY_ID,
        RECLAIM_R,
        TTL_BARS,
        RetestReclaimLifecycle,
    )

    run = screen.load_run(baseline_run)
    bars = screen.load_bars(tape)
    opportunities = _detected_opportunities(run["opportunities"])
    # A detection stamped at the observation time H belongs to the bar that
    # closed at H. The tape writes bar ends one tick early, so map to the last
    # bar whose end is <= the detection time instead of relying on equality.
    from bisect import bisect_right

    ends = [int(bar.end_ns) for bar in bars]
    by_bar: dict[int, list[dict[str, Any]]] = {}
    unplaced = 0
    for opportunity in opportunities:
        idx = bisect_right(ends, int(opportunity["event_time_ns"])) - 1
        if idx < 0:
            unplaced += 1
            continue
        by_bar.setdefault(idx, []).append(opportunity)

    policy = RetestReclaimLifecycle()
    for idx, bar in enumerate(bars):
        bar_ns = int(bar.end_ns)
        policy.advance(
            bar_ns=bar_ns,
            available_ns=bar_ns,
            high=bar.high,
            low=bar.low,
            close=bar.close,
        )
        for opportunity in by_bar.get(idx, []):
            direction = str(opportunity.get("direction"))
            policy.detect(
                opportunity_id=str(opportunity["opportunity_id"]),
                direction=direction,
                reference_price=float(opportunity["entry_price"]),
                stop_price=float(opportunity["stop_price"]),
                target_price=float(opportunity["target_price"]),
                detected_ns=bar_ns,
                available_ns=bar_ns,
                bar_ns=bar_ns,
                expert_supported=True,
            )

    counts = policy.state_counts()
    payload = {
        "arm_id": "D4_SCREEN",
        "policy": POLICY_ID,
        "evidence_class": "MODEL_DERIVED",
        "bars": len(bars),
        "opportunities": len(opportunities),
        "unplaced_opportunities": unplaced,
        "parameters": {
            "ttl_bars": TTL_BARS,
            "reclaim_r": RECLAIM_R,
            "invalidation_r": INVALIDATION_R,
        },
        "state_counts": counts,
        "screened_opportunities": counts["ARMED"]
        + counts["INVALIDATED"]
        + counts["EXPIRED"],
        "note": (
            "State counts only. No orders, fills, fees or PnL are claimed here; the "
            "native replay is the economic evidence."
        ),
    }
    out = Path(output_dir)
    _write_jsonl(out / "opportunity_transitions.jsonl", policy.transitions)
    _write_jsonl(out / "lifecycle_states.jsonl", policy.snapshot())
    _write_json(out / "state_counts.json", payload)
    return payload


def run_d4_native(
    *,
    tape: Path | str,
    output_dir: Path | str,
    initial_balance: Decimal = Decimal("10000"),
    maker_fee: Decimal = Decimal("0.0002"),
    taker_fee: Decimal = Decimal("0.0005"),
    skip_equivalence: bool = True,
    run_id: str | None = None,
) -> dict[str, Any]:
    """Execute the D4 native arm through the existing audit pipeline."""
    from v8_next.audit.pipeline import run_native_audit

    out = Path(output_dir)
    native_dir = out / "native"
    result = run_native_audit(
        tape=Path(tape),
        output_dir=native_dir,
        instrument="BTCUSDT",
        expert="volume_confirmed_breakout",
        initial_balance=initial_balance,
        maker_fee=maker_fee,
        taker_fee=taker_fee,
        skip_equivalence=skip_equivalence,
        config_overrides={"research_opportunity_lifecycle": D4_POLICY_ID},
        run_id=run_id,
    )
    lifecycle = result.get("research_lifecycle") or {}
    transitions = lifecycle.get("transitions") or []
    admissions = lifecycle.get("admissions") or []
    snapshot = lifecycle.get("lifecycles") or []
    counts = lifecycle.get("state_counts") or {}
    _write_jsonl(out / "opportunity_transitions.jsonl", transitions)
    _write_jsonl(out / "admission_decisions.jsonl", admissions)
    _write_jsonl(out / "lifecycle_states.jsonl", snapshot)
    _write_json(
        out / "state_counts.json",
        {
            "policy": D4_POLICY_ID,
            "evidence_class": "OBSERVED_NATIVE",
            "state_counts": counts,
            "transitions": len(transitions),
            "admissions": len(admissions),
        },
    )
    return {
        "run_dir": str(out),
        "native_run_dir": str(native_dir),
        "summary": result["summary"],
        "gates": {g["gate_id"]: g["status"] for g in result["gates"]},
        "validation": result["validation"]["status"],
        "state_counts": counts,
    }
