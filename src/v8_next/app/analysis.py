"""The analysis plane over a real window: attribution, refusals, reconciliation, phases.

This is the composition point the Rust ``analysis`` subcommand occupies
(``v8-core/src/analysis/mod.rs``): over a window of real bars it walks the decision plane,
resolves every signal it can, and answers four questions with named evidence rather than
prose:

1. **What lost, and to which domain** — every losing campaign is charged to exactly one of
   the seven canonical failure domains, and conservation (the domain counts summing to the
   failure count) is verified, not asserted.
2. **What was refused, and why** — the decision plane replays one campaign at a time
   (``nx14``'s frozen contract; the Rust loop's rule 16 is the per-direction form of the same
   admission decision), so every signal that arrives while a campaign is open is a *refusal*.
   Each refusal carries its canonical reason, its replayed counterfactual, and an epistemic
   authority tag. The two readings of the admission rule are both published: suppressing
   every signal while a position is open (the frozen artifact's stream) and suppressing only
   the same-direction signals (the Rust rule).
3. **What could not be reconciled** — each campaign is projected onto the ten-field
   reconciliation surface. The decision plane carries five of the ten fields, so the report
   names the five it *cannot* bind instead of comparing zeros.
4. **What the regret phases say** — the phase plane is invoked when it can be reached over the
   same window; an unreachable phase is reported as ``MISSING`` with its reason, never as a
   pass.

Nothing here is an economic claim: it is a diagnostic over real bars, with the measured cost
columns kept apart from the modelled ones and the measured execution shortfall left absent.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from v8_next.adapters.swing_engine import SwingEngineConfig, SwingEngineStrategy
from v8_next.analysis import (
    CLAIM,
    RefusalStage,
    RefusedDecision,
    compute_veto_attribution,
    refused_beside_lost,
)
from v8_next.analysis.outcome import (
    RECONCILE_EXACT_FIELDS,
    RECONCILE_EXCLUDED_FIELDS,
    RECONCILE_FIELD_COUNT,
    RECONCILE_FLOAT_FIELDS,
    RECONCILE_TOLERANCE,
)
from v8_next.economics.grammar import POLICY_REQUIRED_BARS
from v8_next.economics.swing_baseline import (
    SHARED_CONTRACT,
    policy_spec,
    replay_bracket,
    swing_signal,
)
from v8_next.economics.swing_costs import (
    EXECUTION_SHORTFALL_MISSING,
    SlippageModel,
    campaign_cost,
)
from v8_next.evaluation.multitape import load_multitape
from v8_next.oracle import Identifiability
from v8_next.system_proving import FailureAttributionBreakdown
from v8_next.system_proving.attribution import classify_exit_failure

HOUR_NS = 3_600 * 10**9

#: The admission rule this plane applies, named as data so a reader can disagree with it.
ADMISSION_RULE = "ONE_OPEN_CAMPAIGN_PER_INSTRUMENT"

#: The Rust loop's per-direction form of the same rule (runloop.rs rule 16).
ADMISSION_RULE_PER_DIRECTION = "ONE_OPEN_EXPOSURE_PER_INSTRUMENT_DIRECTION"

#: The ten reconciliation fields the decision plane *does* carry, and the ones it cannot bind.
_REPLAY_FIELD_MAP = {
    "endpoint": "exit_kind",
    "horizon_bars": "bars_held",
    "net_r": "net_measured",
    "entry_price": None,
    "risk_unit_price": None,
    "label_status": None,
    "ambiguous_bars": None,
    "mae_r": None,
    "mfe_r": None,
    "market_move_r": None,
}


def _frames(series: list[Any], instrument_id: str) -> list[Any]:
    cfg = SwingEngineConfig(
        policy_id="plain_swing",
        instrument_id=instrument_id,
        bar_type_str=f"{instrument_id}-1-HOUR-LAST-EXTERNAL",
    )
    source = {index: candle for index, candle in enumerate(series)}
    framing = SwingEngineStrategy(cfg, source, warmup_bars=10**9)
    frames = []
    for index, candle in enumerate(series):
        framing.on_bar(SimpleNamespace(ts_event=index))  # type: ignore[arg-type]
        frames.append(framing._decision_frame(candle))
    return frames


def reconcile_coverage() -> dict[str, Any]:
    """Which of the ten frozen reconciliation fields the decision plane can bind.

    Every field is listed with the source it binds to, or ``None`` plus the named reason. A
    field with no source is reported ``UNBOUND`` and never compared as a zero: the frozen
    ``RECONCILE_TOLERANCE`` comparing two absent zeros would read as agreement.
    """
    bound: dict[str, str] = {
        name: source for name, source in _REPLAY_FIELD_MAP.items() if source is not None
    }
    unbound = [name for name, source in _REPLAY_FIELD_MAP.items() if source is None]
    return {
        "fields_compared": RECONCILE_FIELD_COUNT,
        "bound": bound,
        "unbound": unbound,
        "unbound_reason": "MISSING_FIELD_IN_DECISION_PLANE_ARTIFACT",
        "excluded_by_contract": list(RECONCILE_EXCLUDED_FIELDS),
        "exact_fields": list(RECONCILE_EXACT_FIELDS),
        "float_fields": list(RECONCILE_FLOAT_FIELDS),
        "status": "RECONCILIATION_PARTIAL" if unbound else "RECONCILIATION_COMPLETE",
    }


def analyse_window(
    *,
    repo_root: Path,
    tape_rel: str,
    policy_id: str,
    instrument: str = "BTCUSDT",
    offset_bars: int = 0,
    limit_bars: int | None = None,
    window_label: str | None = None,
    frozen_report: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run the analysis plane over a bounded window of the real tape."""
    tape_path = (repo_root / tape_rel).resolve()
    tape = load_multitape(tape_path, limit=limit_bars, offset=offset_bars)
    series = list(tape.candles[instrument])
    if len(series) < 2:
        raise ValueError(f"window carries {len(series)} bars; an analysis window needs >= 2")
    instrument_id = series[0].instrument_id
    funding_rows = tuple(row for row in tape.funding if row.instrument == instrument)
    taker = Decimal(SHARED_CONTRACT["taker_fee"])

    spec = policy_spec(policy_id)
    grammar_policy = spec.grammar_policy
    if grammar_policy is None:
        raise ValueError(f"policy {policy_id!r} declares no grammar policy to warm up")
    warmup = int(POLICY_REQUIRED_BARS[grammar_policy])
    if warmup >= len(series) - 1:
        raise ValueError(
            f"window of {len(series)} bars is shorter than the policy's {warmup}-bar warmup"
        )
    has_bracket_policy = spec.protection_policy not in (None, "timeout-only-v1")

    frames = _frames(series, instrument_id)
    breakdown = FailureAttributionBreakdown()
    admitted: list[dict[str, Any]] = []
    refusals: list[RefusedDecision] = []
    per_direction_refusals = 0

    open_until = -1  # the frozen single-position admission rule
    open_until_by_direction: dict[str, int] = {}
    index = warmup
    while index < len(series) - 1:
        decision = swing_signal(frames[index], spec, bar_ns=HOUR_NS)
        if decision is None:
            index += 1
            continue

        outcome = replay_bracket(
            decision,
            series[index + 1 :],
            bar_ns=HOUR_NS,
            bps_fee=taker,
            has_bracket=has_bracket_policy,
        )
        bracketless = Decimal(decision.stop_price) == Decimal(decision.entry_reference)
        measured = campaign_cost(
            decision=decision,
            outcome=outcome,
            funding_rows=funding_rows,
            slippage=SlippageModel(model="entry_bar_range_fraction", range_fraction=0.0),
            entry_bar_low=series[index].low,
            entry_bar_high=series[index].high,
            dropped_rows_in_tape=tape.funding_dropped,
        )
        campaign_bars = max(1, outcome.bars_held)

        blocked = index < open_until
        direction = decision.direction
        if direction is None:
            raise ValueError(
                f"decision at bar {index} carries no direction; the replay cannot be attributed"
            )
        blocked_per_direction = index < open_until_by_direction.get(direction, -1)
        if blocked_per_direction:
            per_direction_refusals += 1

        if blocked:
            refusals.append(
                RefusedDecision(
                    candidate_id=decision.identity(),
                    expert_id=policy_id,
                    veto_reason="EXISTING_EXPOSURE_CONFLICT",
                    stage=RefusalStage.CAMPAIGN_ADMISSION,
                    # The refused signal was replayed anyway, so its own outcome is the
                    # counterfactual: nothing about it was modelled away.
                    authority_status=Identifiability.IDENTIFIED,
                    avoided_loss_usdt=None,
                    missed_profit_usdt=None,
                    hypothetical_mfe_r=None,
                    hypothetical_mae_r=None,
                    hypothetical_net_return=measured.net_return_measured_only,
                )
            )
            index += 1
            continue

        domain = None
        if measured.net_return_measured_only < 0:
            domain = classify_exit_failure(
                exit_kind=outcome.exit_kind,
                net_return=measured.net_return_measured_only,
                has_bracket=has_bracket_policy and not bracketless,
            )
            breakdown.record_failure(domain)
        admitted.append(
            {
                "decision_identity": decision.identity(),
                "decision_ns": int(decision.decision_ns),
                "direction": decision.direction,
                "exit_kind": outcome.exit_kind,
                "bars_held": outcome.bars_held,
                "bracketless": bracketless,
                "gross_return": outcome.gross_return,
                "fee_cost_return": -abs(outcome.fee_cost_return),
                "funding_cost_return": measured.funding_cost_return,
                "funding_status": measured.funding_status,
                "net_measured": measured.net_return_measured_only,
                "execution_shortfall_measured": None,
                "execution_shortfall_status": EXECUTION_SHORTFALL_MISSING,
                "failure_domain": None if domain is None else domain.value,
            }
        )
        open_until = index + campaign_bars + 1
        open_until_by_direction[direction] = open_until
        # Keep walking bar by bar: the bars inside this campaign are exactly the ones the
        # frozen artifact silently skipped, and each signal among them is a recorded refusal.
        index += 1

    refused_by_reason: dict[str, int] = {}
    for row in refusals:
        refused_by_reason[row.veto_reason] = refused_by_reason.get(row.veto_reason, 0) + 1
    lost_by_domain = {
        domain.value: count for domain, count in breakdown.counts_by_domain.items()
    }
    veto_summary, dedup = compute_veto_attribution(
        refusals,
        total_suppressed=len(refusals),
        admitted_parents=len(admitted),
    )

    nets = [row["net_measured"] for row in admitted]
    report: dict[str, Any] = {
        "pillar": "analysis_plane",
        "claim_status": CLAIM,
        "evidence_class": (
            "DIAGNOSTIC over real bars and real funding settlement rows; decision plane, "
            "not engine fills; the measured execution shortfall stays absent"
        ),
        "tape": {
            "path": tape_rel,
            "sha256": tape.tape_sha256,
            "instrument_id": instrument_id,
            "window_label": window_label or f"offset={offset_bars},limit={limit_bars}",
            "offset_bars": offset_bars,
            "limit_bars": limit_bars,
            "bars": len(series),
            "first_bar_ns": int(series[0].end_ns),
            "last_bar_ns": int(series[-1].end_ns),
        },
        "policy": {
            "policy_id": policy_id,
            "grammar_policy": spec.grammar_policy,
            "protection_policy": spec.protection_policy,
            "warmup_bars": warmup,
        },
        "admission": {
            "rule": ADMISSION_RULE,
            "secondary_rule": ADMISSION_RULE_PER_DIRECTION,
            "secondary_rule_refusals": per_direction_refusals,
            "secondary_rule_note": (
                "the frozen artifact suppresses every signal while a campaign is open; the "
                "Rust loop's rule 16 suppresses only same-direction signals. Both counts are "
                "published rather than one being chosen to make the other agree."
            ),
        },
        "admitted_campaigns": len(admitted),
        "attribution": breakdown.as_dict(),
        "conservation_verified": breakdown.verify_conservation(),
        "refusals": {
            "total": len(refusals),
            "by_reason": dict(sorted(refused_by_reason.items())),
            "summary": veto_summary.as_dict(),
            "rows_sample": [row.as_dict() for row in refusals[:5]],
            "dedup": dedup.as_dict(),
        },
        "refused_beside_lost": refused_beside_lost(refused_by_reason, lost_by_domain),
        "reconciliation": reconcile_coverage(),
        "phase_plane": phase_plane(),
        "reconciliation_vs_frozen": (
            None
            if frozen_report is None
            else reconcile_against_frozen(admitted, frozen_report, policy_id=policy_id)
        ),
        "windows": {
            "net_measured_sum": round(sum(nets), 8),
            "gross_return_sum": round(sum(row["gross_return"] for row in admitted), 8),
            "fee_cost_return_sum": round(sum(row["fee_cost_return"] for row in admitted), 8),
            "funding_cost_return_sum": round(
                sum(row["funding_cost_return"] for row in admitted), 8
            ),
        },
        "cost_columns": {
            "fee_cost_return": "MEASURED_TAKER_FEE",
            "funding_cost_return": "MEASURED_SETTLEMENT_ROWS",
            "slippage_modelled_return": "NOT_RUN_IN_THIS_SURFACE",
            "execution_shortfall_measured_return": None,
            "execution_shortfall_status": EXECUTION_SHORTFALL_MISSING,
        },
    }
    if not breakdown.verify_conservation():
        raise ValueError(
            "attribution conservation failed: domain counts do not sum to the failure count"
        )
    return report


def phase_plane() -> dict[str, Any]:
    """Run the ported regret phase plane over the declared FT universe, failing closed.

    The phase plane measures two frozen estimands (FT001) — the *legal hindsight gap* and the
    *actual-vs-no-trade* gap. Both are functions of a legal-action manifest that this checkout
    does not produce: the decision plane replays the campaign it took, not the set of actions it
    could legally have taken instead. So the plane is executed over the declared universe and
    every slice reports its named refusal rather than a number invented from the wrong estimand.

    What is measured here is therefore the *declaration* (72 slices, the policy set) and the
    refusal counts; the estimand rows are named as the missing input, with the chain issue that
    owns the opportunity universe as their producer.
    """
    from v8_next.analysis.phases import (
        LABEL,
        declare_policies,
        declare_slices,
        score_slice,
    )

    verdicts: dict[str, int] = {}
    for key, expert_id, symbol, direction, estimand in declare_slices():
        result = score_slice(key, expert_id, symbol, direction, estimand, [])
        verdicts[result.discovery_verdict] = verdicts.get(result.discovery_verdict, 0) + 1
    policies = declare_policies({})
    return {
        "label": LABEL,
        "declared_slices": len(declare_slices()),
        "evaluated_slices": sum(verdicts.values()),
        "verdict_histogram": dict(sorted(verdicts.items())),
        "declared_policies": [policy.policy_id for policy in policies],
        "declared_policy_count": len(policies),
        "estimands_required": ["mean_legal_hindsight_gap", "mean_actual_vs_no_trade"],
        "estimand_rows": None,
        "missing_input": "MISSING_LEGAL_ACTION_MANIFEST",
        "missing_input_owner": "chain issue #452 (oracle substrate: opportunity universe)",
        "claim": CLAIM,
    }


def reconcile_against_frozen(
    admitted: list[dict[str, Any]],
    frozen_report: dict[str, Any],
    *,
    policy_id: str,
) -> dict[str, Any]:
    """Reconcile this plane's replayed campaigns against a frozen report's own rows.

    The two are independent implementations of the same contract, and they share exactly
    three of the ten reconciliation fields — the exit endpoint, the holding horizon and the
    net return — so the comparison is restricted to those and the other seven stay declared
    unbound (``reconcile_coverage``). Matching is by decision identity, not by row order: a
    count that agreed while the rows were shuffled would not be a reconciliation.

    A reconciliation over zero compared candidates is ``RECONCILIATION_NOT_MEASURED``, never a
    vacuous ``RECONCILED``.
    """
    family = frozen_report.get("families", {}).get(policy_id)
    if family is None:
        return {
            "status": "RECONCILIATION_NOT_MEASURED",
            "reason": f"frozen report carries no family {policy_id!r}",
            "compared": 0,
            "reconciled": 0,
            "mismatched": 0,
        }
    observed = {row["decision_identity"]: row for row in family.get("trades", [])}
    field_map = {"endpoint": "exit_kind", "horizon_bars": "bars_held", "net_r": "net_measured"}

    compared = 0
    reconciled = 0
    mismatches: list[dict[str, Any]] = []
    absent: list[str] = []
    open_at_cutoff: list[str] = []
    for row in admitted:
        identity = row["decision_identity"]
        if row["exit_kind"] == "OPEN_AT_CUTOFF":
            # A bounded window cannot know how a still-open campaign resolves, so comparing
            # its mark-to-market against a full-run exit would compare a truncation against an
            # outcome. It is excluded and named, never counted as agreement.
            open_at_cutoff.append(identity)
            continue
        frozen = observed.get(identity)
        if frozen is None:
            absent.append(identity)
            continue
        compared += 1
        differences: dict[str, dict[str, Any]] = {}
        for left, right in field_map.items():
            mine, theirs = row[right], frozen[right]
            if left == "net_r":
                if abs(float(mine) - float(theirs)) > RECONCILE_TOLERANCE:
                    differences[left] = {"this_plane": mine, "frozen": theirs}
            elif mine != theirs:
                differences[left] = {"this_plane": mine, "frozen": theirs}
        if differences:
            mismatches.append({"decision_identity": identity, "fields": differences})
        else:
            reconciled += 1

    if compared == 0:
        status = "RECONCILIATION_NOT_MEASURED"
    elif mismatches:
        status = "RECONCILIATION_FAILED"
    else:
        status = "RECONCILED"
    return {
        "status": status,
        "fields_compared": sorted(field_map),
        "fields_unbound": reconcile_coverage()["unbound"],
        "compared": compared,
        "reconciled": reconciled,
        "mismatched": len(mismatches),
        "absent_from_frozen_report": len(absent),
        "excluded_open_at_cutoff": len(open_at_cutoff),
        "excluded_reason": "TRUNCATED_WINDOW_TAIL_CANNOT_RESOLVE" if open_at_cutoff else None,
        "mismatch_sample": mismatches[:5],
        "mismatch_reason": "field_mismatch" if mismatches else None,
        "frozen_artifact_family": policy_id,
    }


def _digest(report: dict[str, Any]) -> str:
    payload = json.dumps(report, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode()).hexdigest()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", default=None)
    parser.add_argument("--tape", default="research/tape/multi-1h-4y/tape.jsonl")
    parser.add_argument("--instrument", default="BTCUSDT")
    parser.add_argument("--policy", default="causal_trend")
    parser.add_argument(
        "--offset-bars", type=int, default=0, help="Skip this many leading bars (window start)."
    )
    parser.add_argument(
        "--limit-bars", type=int, default=None, help="Cap the window to this many bars."
    )
    parser.add_argument("--window-label", default=None)
    parser.add_argument(
        "--reconcile-against",
        type=Path,
        default=None,
        help="Frozen report artifact whose per-campaign rows are reconciled against this run.",
    )
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args(argv)

    package_root = Path(__file__).resolve().parents[3]
    repo_root = (
        Path(args.repo_root).resolve()
        if args.repo_root
        else (package_root)
    )
    try:
        frozen_report = (
            json.loads(args.reconcile_against.read_text())
            if args.reconcile_against is not None
            else None
        )
    except (OSError, json.JSONDecodeError) as exc:
        print(f"error: cannot read --reconcile-against: {exc}", file=sys.stderr)
        return 2
    try:
        report = analyse_window(
            repo_root=repo_root,
            tape_rel=args.tape,
            policy_id=args.policy,
            instrument=args.instrument,
            offset_bars=args.offset_bars,
            limit_bars=args.limit_bars,
            window_label=args.window_label,
            frozen_report=frozen_report,
        )
    except (ValueError, FileNotFoundError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    report["report_sha256"] = _digest(report)
    print(
        f"[analysis] {report['tape']['window_label']}: {report['admitted_campaigns']} campaigns, "
        f"{report['refusals']['total']} refused, conservation="
        f"{report['conservation_verified']}, {report['reconciliation']['status']}"
    )
    for reason, count in report["refusals"]["by_reason"].items():
        print(f"    refused {count:6d} x {reason}")
    for domain, count in report["attribution"]["counts_by_domain"].items():
        print(f"    lost    {count:6d} x {domain}")
    frozen_check = report.get("reconciliation_vs_frozen")
    if frozen_check is not None:
        print(
            f"[analysis] reconcile vs frozen: {frozen_check['status']} "
            f"({frozen_check['reconciled']}/{frozen_check['compared']} equal, "
            f"{frozen_check['mismatched']} mismatched)"
        )
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(report, indent=2, sort_keys=True, default=str) + "\n")
        print(f"[analysis] wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
