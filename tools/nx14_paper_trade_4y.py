#!/usr/bin/env python
"""Four-year simulated paper-trade report with v8-core attribution (P2 of the locked benchmark).

Runs the pre-registered swing family over the four-year tape, then reports it the way the
ported audit kernel requires:

* every losing campaign is charged to exactly one of the **7 disjoint failure domains**
  (``system_proving.attribution``) and the conservation invariant is verified;
* the run is summarised as the **14-metric robustness vector** and sealed in a
  ``SystemProvingGroundReceipt`` with its digest;
* every field of that vector carries its own basis and every field this producer cannot
  measure is published as ``null`` with a named reason (#457) — a published name is a claim
  about what was measured;
* costs stay in separate columns (measured fee, measured funding, modelled slippage) and the
  measured execution shortfall stays absent;
* the **bracket contract** is published as two declared readings with their campaign counts
  and their divergence class (#459): inside one iteration the bracket flag comes from ONE
  source and is handed to both the replay and the failure classifier, a decision whose own
  protection never formed is counted, and neither reading is reconciled away.

Nothing here is an economic claim: it is a diagnostic report over real bars.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterable, Sequence

from v8_next.adapters.swing_engine import SwingEngineConfig, SwingEngineStrategy
from v8_next.domain.market import Candle
from v8_next.economics.grammar import POLICY_REQUIRED_BARS
from v8_next.economics.swing_baseline import (
    SHARED_CONTRACT,
    ReplayOutcome,
    SwingDecision,
    policy_spec,
    replay_bracket,
    swing_signal,
)
from v8_next.economics.swing_costs import (
    EXECUTION_SHORTFALL_MISSING,
    CampaignCost,
    SlippageModel,
    campaign_cost,
)
from v8_next.evaluation.multitape import FundingRow, load_multitape
from v8_next.system_proving import (
    FailureAttributionBreakdown,
    SystemProvingGroundReceipt,
)
from v8_next.system_proving.attribution import classify_exit_failure
from v8_next.system_proving.metrics import (
    DRAWDOWN_BASIS,
    FIELD_ABSENCE_REASON,
    metrics_from_campaigns,
)

HOUR_NS = 3_600 * 10**9
SLIPPAGE_FRACTIONS = (0.0, 0.05, 0.10)

#: The two declared bracket-contract readings (#459). A reading is ONE rule that turns a
#: decision into a bracket flag, and that flag is the only one handed to ``replay_bracket``
#: AND to ``classify_exit_failure`` for the campaigns of that reading — a bracket decision
#: never diverges inside one iteration.
#:
#: * ``READING_A_POLICY_NAME`` — a policy that declares a protection is replayed as bracketed
#:   for every one of its decisions. This is the contract the frozen P2 artifact
#:   (sha256 ``41ab7a0da11812818a67ce8f92709be083b1dd79c80ad4a7670d27a163eb32ec``) was
#:   produced under, so it is the **published** reading and its return columns are unchanged.
#: * ``READING_B_DECISION_CONTRACT`` — the engine lane's declared per-decision contract
#:   (``adapters/swing_engine.py``: ``has_bracket = has_bracket_config and stop_distance > 0``):
#:   a decision whose own protection did not form carries ``stop_price == entry_reference``,
#:   is inserted unprotected and is held to its declared expiry, never stopped out at its own
#:   entry.
READING_A_POLICY_NAME = "READING_A_POLICY_NAME"
READING_B_DECISION_CONTRACT = "READING_B_DECISION_CONTRACT"
PUBLISHED_READING = READING_A_POLICY_NAME
READING_BASIS: dict[str, str] = {
    READING_A_POLICY_NAME: (
        "has_bracket = the policy name declares a protection "
        "(protection_policy not in (None, 'timeout-only-v1'))"
    ),
    READING_B_DECISION_CONTRACT: (
        "has_bracket = the policy declares a protection AND this decision's own protection "
        "formed (stop_price != entry_reference)"
    ),
}

#: The one divergence class between the two readings. It is counted, never reconciled away:
#: deleting one reading to make the two agree is exactly what the finding forbids.
BRACKET_CONTRACT_DIVERGENCE = "BRACKET_CONTRACT_DIVERGENCE"
BRACKETLESS_DEFINITION = (
    "a decision is bracketless when its own protection did not form: swing_baseline stamps "
    "stop_price == entry_reference only when protection_at returned None"
)
DIVERGENCE_CLASS_DEFINITION = (
    "BRACKET_CONTRACT_DIVERGENCE: the bracket flag handed to replay_bracket differs between "
    "the two declared readings of the same campaign — the policy-name reading replays a "
    "decision whose own protection never formed in the bracket branch, the decision reading "
    "holds it unprotected to its expiry"
)
PUBLISHED_READING_STATEMENT = (
    f"the published return columns are produced under {READING_A_POLICY_NAME}: a policy that "
    "declares a protection is replayed as bracketed for every one of its decisions, which is "
    "the contract the predecessor P2 artifact "
    "(sha256 41ab7a0da11812818a67ce8f92709be083b1dd79c80ad4a7670d27a163eb32ec) was produced "
    f"under. {READING_B_DECISION_CONTRACT} is published beside it with its own counts and its "
    "numbers are NOT the published series"
)


def bracket_flags(declares_protection: bool, bracketless: bool) -> dict[str, bool]:
    """Both readings' bracket flags for one decision, derived from ONE per-decision fact.

    ``declares_protection`` is the policy's declared contract, ``bracketless`` is the decision
    fact (``stop_price == entry_reference``). Every flag this producer ever hands to
    ``replay_bracket`` comes from this table, so an iteration cannot mix two contracts and
    cannot take a second, silent bracket decision.
    """
    return {
        READING_A_POLICY_NAME: declares_protection,
        READING_B_DECISION_CONTRACT: declares_protection and not bracketless,
    }


class _ReadingTotals:
    """One reading's totals over the campaigns the pillar walks.

    Accumulated from the published rows themselves (the counter reading from the rows'
    ``counter_reading`` blocks, which are the rows' own numbers wherever the two readings
    agree), so every total is reproducible from the artifact's ``trades`` rows.

    A plain class, not a dataclass: the CLI loads this tool by ``exec_module`` without
    registering it in ``sys.modules``, where ``dataclasses`` cannot resolve the module
    namespace of a string-annotated field.
    """

    def __init__(self, reading: str) -> None:
        self.reading = reading
        self.campaigns = 0
        self.replayed_bracketed_campaigns = 0
        self.replayed_unprotected_campaigns = 0
        self.decisions_without_protection = 0
        self.stop_exits = 0
        self.stop_exits_charged_to_decisions_without_protection = 0
        self.gross_return_sum = 0.0
        self.fee_cost_return_sum = 0.0
        self.funding_cost_return_sum = 0.0
        self.net_measured_sum = 0.0
        self.net_with_modelled_slippage_10_sum = 0.0
        self.exit_kind_counts: dict[str, int] = {}
        self.breakdown = FailureAttributionBreakdown()

    @property
    def protective_stops(self) -> int:
        """STOP exits charged to a decision whose own protection formed.

        The distinction this field exists for: under the policy-name reading a bracketless
        decision stops out at its own entry reference, so ``stop_exits`` alone is NOT a count
        of protective stops (#459).
        """
        return self.stop_exits - self.stop_exits_charged_to_decisions_without_protection

    def record(
        self,
        *,
        bracketed: bool,
        decision_without_protection: bool,
        exit_kind: str,
        gross_return: float,
        fee_cost_return: float,
        funding_cost_return: float,
        net_measured: float,
        net_with_modelled_slippage_10: float,
    ) -> None:
        self.campaigns += 1
        if bracketed:
            self.replayed_bracketed_campaigns += 1
        else:
            self.replayed_unprotected_campaigns += 1
        if decision_without_protection:
            self.decisions_without_protection += 1
        self.exit_kind_counts[exit_kind] = self.exit_kind_counts.get(exit_kind, 0) + 1
        if exit_kind == "STOP":
            self.stop_exits += 1
            if decision_without_protection:
                self.stop_exits_charged_to_decisions_without_protection += 1
        self.gross_return_sum += gross_return
        self.fee_cost_return_sum += fee_cost_return
        self.funding_cost_return_sum += funding_cost_return
        self.net_measured_sum += net_measured
        self.net_with_modelled_slippage_10_sum += net_with_modelled_slippage_10

    def as_dict(self) -> dict[str, Any]:
        return {
            "reading": self.reading,
            "basis": READING_BASIS[self.reading],
            "campaigns": self.campaigns,
            "replayed_bracketed_campaigns": self.replayed_bracketed_campaigns,
            "replayed_unprotected_campaigns": self.replayed_unprotected_campaigns,
            "decisions_without_protection": self.decisions_without_protection,
            "exit_kind_counts": dict(sorted(self.exit_kind_counts.items())),
            "stop_exits": self.stop_exits,
            "stop_exits_charged_to_decisions_without_protection": (
                self.stop_exits_charged_to_decisions_without_protection
            ),
            "protective_stops": self.protective_stops,
            "gross_return_sum": round(self.gross_return_sum, 8),
            "fee_cost_return_sum": round(self.fee_cost_return_sum, 8),
            "funding_cost_return_sum": round(self.funding_cost_return_sum, 8),
            "net_measured_sum": round(self.net_measured_sum, 8),
            "net_with_modelled_slippage_10_sum": round(self.net_with_modelled_slippage_10_sum, 8),
            "failure_attribution": self.breakdown.as_dict(),
        }


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _replay_and_cost(
    *,
    decision: SwingDecision,
    future_bars: Sequence[Candle],
    funding_rows: Iterable[FundingRow],
    entry_bar_low: Decimal,
    entry_bar_high: Decimal,
    dropped_rows_in_tape: int,
    taker: Decimal,
    has_bracket: bool,
) -> tuple[ReplayOutcome, dict[float, CampaignCost]]:
    """Replay one decision under one declared bracket flag, then price all slippage rules.

    The caller passes the flag of the reading it is computing, and that same flag is the only
    one it may hand to ``classify_exit_failure``: one reading, one contract, one iteration.
    """
    outcome = replay_bracket(
        decision, future_bars, bar_ns=HOUR_NS, bps_fee=taker, has_bracket=has_bracket
    )
    per_fraction = {
        fraction: campaign_cost(
            decision=decision, outcome=outcome, funding_rows=funding_rows,
            slippage=SlippageModel(model="entry_bar_range_fraction", range_fraction=fraction),
            entry_bar_low=entry_bar_low, entry_bar_high=entry_bar_high,
            dropped_rows_in_tape=dropped_rows_in_tape,
        )
        for fraction in SLIPPAGE_FRACTIONS
    }
    return outcome, per_fraction


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", default=None)
    parser.add_argument("--tape", default="research/tape/multi-1h-4y/tape.jsonl")
    parser.add_argument("--instrument", default="BTCUSDT")
    parser.add_argument("--policies", default="plain_swing,causal_trend")
    parser.add_argument("--out", default="docs/evidence/v87-r3/PAPER_4Y")
    args = parser.parse_args(argv)

    repo_root = Path(args.repo_root).resolve() if args.repo_root else Path(__file__).resolve().parents[1]
    out_dir = (repo_root / args.out).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    tape_path = (repo_root / args.tape).resolve()

    tape = load_multitape(tape_path)
    series = list(tape.candles[args.instrument])
    instrument_id = series[0].instrument_id
    funding_rows = tuple(row for row in tape.funding if row.instrument == args.instrument)
    taker = Decimal(SHARED_CONTRACT["taker_fee"])

    # one pass of decision frames, then replay each policy over them
    cfg = SwingEngineConfig(
        policy_id="plain_swing", instrument_id=instrument_id,
        bar_type_str=f"{instrument_id}-1-HOUR-LAST-EXTERNAL",
    )
    source = {index: candle for index, candle in enumerate(series)}
    framing = SwingEngineStrategy(cfg, source, warmup_bars=10**9)
    frames = []
    for index, candle in enumerate(series):
        framing.on_bar(SimpleNamespace(ts_event=index))  # type: ignore[arg-type]
        frames.append(framing._decision_frame(candle))

    report: dict[str, Any] = {
        "pillar": "P2_paper_trade_4y",
        "claim_status": "NO_ECONOMIC_CLAIM",
        "evidence_class": (
            "DECISION_PLANE_DIAGNOSTIC over four years of real bars: real taker fee and real "
            "funding settlement rows; not engine fills, not a venue settlement; slippage is a "
            "declared assumption and the measured execution shortfall is absent"
        ),
        "tape": {"path": str(tape_path.relative_to(repo_root)), "sha256": tape.tape_sha256,
                 "bars": len(series), "instrument_id": instrument_id},
        "bracket_contract": {
            "published_reading": PUBLISHED_READING,
            "published_reading_statement": PUBLISHED_READING_STATEMENT,
            "divergence_class": BRACKET_CONTRACT_DIVERGENCE,
            "divergence_class_definition": DIVERGENCE_CLASS_DEFINITION,
            "bracketless_definition": BRACKETLESS_DEFINITION,
            "reading_basis": [
                {"reading": name, "basis": basis} for name, basis in READING_BASIS.items()
            ],
            "note": (
                "the per-family bracket_contract block is the authority for that family; this "
                "rollup repeats its counts by policy, never as one pooled number"
            ),
            "per_policy": {},
        },
        "families": {},
    }

    for policy_id in [name.strip() for name in args.policies.split(",") if name.strip()]:
        spec = policy_spec(policy_id)
        grammar_policy = spec.grammar_policy
        if grammar_policy is None:
            # the cash floor opens no campaign, so it has no warmup and no rows to replay:
            # fail closed with a named reason instead of indexing a table with None.
            raise SystemExit(f"policy {policy_id!r} declares no grammar policy to replay")
        warmup = int(POLICY_REQUIRED_BARS[grammar_policy])
        # the POLICY declares whether it carries a protection at all; the DECISION says whether
        # that protection formed. Both facts enter bracket_flags() once, per decision.
        declares_protection = spec.protection_policy not in (None, "timeout-only-v1")
        breakdown = FailureAttributionBreakdown()
        counter_breakdown = FailureAttributionBreakdown()
        trades: list[dict[str, Any]] = []
        index = warmup
        while index < len(series) - 1:
            decision = swing_signal(frames[index], spec, bar_ns=HOUR_NS)
            if decision is None:
                index += 1
                continue
            bracketless = Decimal(decision.stop_price) == Decimal(decision.entry_reference)
            has_bracket_by_reading = bracket_flags(declares_protection, bracketless)
            published_bracketed = has_bracket_by_reading[PUBLISHED_READING]
            outcome, per_fraction = _replay_and_cost(
                decision=decision, future_bars=series[index + 1:], funding_rows=funding_rows,
                entry_bar_low=series[index].low, entry_bar_high=series[index].high,
                dropped_rows_in_tape=tape.funding_dropped, taker=taker,
                has_bracket=published_bracketed,
            )
            measured = per_fraction[0.0]
            domain = None
            if measured.net_return_measured_only < 0:
                domain = classify_exit_failure(
                    exit_kind=outcome.exit_kind,
                    net_return=measured.net_return_measured_only,
                    # the SAME flag the published replay above used (#459 F1): the published
                    # failure attribution can never describe a different contract than the
                    # published returns.
                    has_bracket=published_bracketed,
                )
                breakdown.record_failure(domain)
            divergence_classes: list[str] = []
            counter_reading: dict[str, Any] | None = None
            if has_bracket_by_reading[READING_B_DECISION_CONTRACT] != published_bracketed:
                # the two declared readings disagree on this campaign: the counter reading is
                # published, counted and classified under its own flag, never averaged in.
                divergence_classes.append(BRACKET_CONTRACT_DIVERGENCE)
                counter_outcome, counter_fraction = _replay_and_cost(
                    decision=decision, future_bars=series[index + 1:], funding_rows=funding_rows,
                    entry_bar_low=series[index].low, entry_bar_high=series[index].high,
                    dropped_rows_in_tape=tape.funding_dropped, taker=taker,
                    has_bracket=has_bracket_by_reading[READING_B_DECISION_CONTRACT],
                )
                counter_measured = counter_fraction[0.0]
                counter_domain = None
                if counter_measured.net_return_measured_only < 0:
                    counter_domain = classify_exit_failure(
                        exit_kind=counter_outcome.exit_kind,
                        net_return=counter_measured.net_return_measured_only,
                        has_bracket=has_bracket_by_reading[READING_B_DECISION_CONTRACT],
                    )
                    counter_breakdown.record_failure(counter_domain)
                counter_reading = {
                    "reading": READING_B_DECISION_CONTRACT,
                    "bracketed": has_bracket_by_reading[READING_B_DECISION_CONTRACT],
                    "exit_kind": counter_outcome.exit_kind,
                    "bars_held": counter_outcome.bars_held,
                    "gross_return": counter_outcome.gross_return,
                    "fee_cost_return": -abs(counter_outcome.fee_cost_return),
                    "funding_cost_return": counter_measured.funding_cost_return,
                    "funding_status": counter_measured.funding_status,
                    "net_measured": counter_measured.net_return_measured_only,
                    "net_with_modelled_slippage_05": (
                        counter_fraction[0.05].net_return_with_modelled_slippage
                    ),
                    "net_with_modelled_slippage_10": (
                        counter_fraction[0.10].net_return_with_modelled_slippage
                    ),
                    "failure_domain": None if counter_domain is None else counter_domain.value,
                }
            trades.append({
                "decision_identity": decision.identity(),
                "exit_kind": outcome.exit_kind,
                "bars_held": outcome.bars_held,
                "bracketless": bracketless,
                "replayed_under": PUBLISHED_READING,
                "replayed_bracketed": published_bracketed,
                "divergence_classes": divergence_classes,
                "gross_return": outcome.gross_return,
                "fee_cost_return": -abs(outcome.fee_cost_return),
                "funding_cost_return": measured.funding_cost_return,
                "funding_status": measured.funding_status,
                "net_measured": measured.net_return_measured_only,
                "net_with_modelled_slippage_05": per_fraction[0.05].net_return_with_modelled_slippage,
                "net_with_modelled_slippage_10": per_fraction[0.10].net_return_with_modelled_slippage,
                "execution_shortfall_measured": None,
                "execution_shortfall_status": EXECUTION_SHORTFALL_MISSING,
                "failure_domain": None if domain is None else domain.value,
                "counter_reading": counter_reading,
            })
            index += max(1, outcome.bars_held) + 1

        # ---- the two declared readings, accumulated from the rows just built -------------
        published_totals = _ReadingTotals(reading=PUBLISHED_READING)
        counter_totals = _ReadingTotals(reading=READING_B_DECISION_CONTRACT)
        divergence_counts: dict[str, int] = {}
        for row in trades:
            published_totals.record(
                bracketed=bool(row["replayed_bracketed"]),
                decision_without_protection=bool(row["bracketless"]),
                exit_kind=str(row["exit_kind"]),
                gross_return=float(row["gross_return"]),
                fee_cost_return=float(row["fee_cost_return"]),
                funding_cost_return=float(row["funding_cost_return"]),
                net_measured=float(row["net_measured"]),
                net_with_modelled_slippage_10=float(row["net_with_modelled_slippage_10"]),
            )
            counter = row["counter_reading"]
            counter_totals.record(
                bracketed=(
                    bool(counter["bracketed"]) if counter is not None
                    else bool(row["replayed_bracketed"])
                ),
                decision_without_protection=bool(row["bracketless"]),
                exit_kind=str(counter["exit_kind"] if counter is not None else row["exit_kind"]),
                gross_return=float(
                    counter["gross_return"] if counter is not None else row["gross_return"]
                ),
                fee_cost_return=float(
                    counter["fee_cost_return"] if counter is not None else row["fee_cost_return"]
                ),
                funding_cost_return=float(
                    counter["funding_cost_return"] if counter is not None
                    else row["funding_cost_return"]
                ),
                net_measured=float(
                    counter["net_measured"] if counter is not None else row["net_measured"]
                ),
                net_with_modelled_slippage_10=float(
                    counter["net_with_modelled_slippage_10"] if counter is not None
                    else row["net_with_modelled_slippage_10"]
                ),
            )
            for name in row["divergence_classes"]:
                divergence_counts[name] = divergence_counts.get(name, 0) + 1
        published_totals.breakdown = breakdown
        counter_totals.breakdown = counter_breakdown

        nets = [row["net_measured"] for row in trades]
        wins = sum(1 for value in nets if value > 0)
        gross = sum(row["gross_return"] for row in trades)
        fees = sum(row["fee_cost_return"] for row in trades)
        funding = sum(row["funding_cost_return"] for row in trades)
        net_with_modelled_slippage_10 = sum(
            row["net_with_modelled_slippage_10"] for row in trades
        )
        # #457: the drawdown, the recovery horizon and the slippage degradation are read off
        # the per-campaign columns this producer actually measured — the vector derives them
        # itself instead of the tool hand-feeding a number under another field's name.
        # #487: NO double-entry residual is passed. This producer keeps no second ledger, so it
        # has no residual to reconcile: the vector publishes the field as null with its named
        # reason instead of inheriting a `0.0` that would mint `double_entry_reconciled: true`.
        metrics = metrics_from_campaigns(
            campaigns=len(trades), failures=breakdown.total_failures, gross_return_sum=gross,
            fee_cost_sum=abs(fees), funding_cost_sum=funding,
            bars=len(series),
            campaign_net_returns=nets,
            campaign_bars_held=[int(row["bars_held"]) for row in trades],
            net_with_modelled_slippage_sum=net_with_modelled_slippage_10,
        )
        receipt = SystemProvingGroundReceipt.new(
            world_id=f"{tape_path.parent.name}:{instrument_id}",
            policy_id=policy_id,
            total_trades=len(trades),
            total_campaigns=len(trades),
            metrics=metrics,
            attribution=breakdown,
            exercises_full_pipeline=False,
            timestamp_ns=int(series[-1].end_ns),
        )
        bracketless_campaigns = published_totals.decisions_without_protection
        family_bracket_contract = {
            "published_reading": PUBLISHED_READING,
            "published_reading_statement": PUBLISHED_READING_STATEMENT,
            "readings_identical": not divergence_counts,
            "bracketless_definition": BRACKETLESS_DEFINITION,
            "divergence_class": BRACKET_CONTRACT_DIVERGENCE,
            "divergence_class_definition": DIVERGENCE_CLASS_DEFINITION,
            "campaigns": len(trades),
            "bracketless_campaigns": bracketless_campaigns,
            "bracketed_campaigns": len(trades) - bracketless_campaigns,
            "divergence_class_counts": dict(sorted(divergence_counts.items())),
            "reading_a": published_totals.as_dict(),
            "reading_b": counter_totals.as_dict(),
            "notes": [
                "both readings cover the same decisions in the same order; only the bracket "
                "flag handed to replay_bracket differs",
                "a campaign whose two readings agree carries counter_reading: null — there is "
                "one replay and its counter row IS its published row",
                "the published return columns are the READING_A columns: no threshold, window "
                "or bracketing semantic moved with this publication",
            ],
        }
        report["bracket_contract"]["per_policy"][policy_id] = {
            "bracketless_campaigns": bracketless_campaigns,
            "bracketed_campaigns": len(trades) - bracketless_campaigns,
            "divergence_class_counts": dict(sorted(divergence_counts.items())),
        }
        report["families"][policy_id] = {
            "grammar_policy": spec.grammar_policy,
            "protection_policy": spec.protection_policy,
            "warmup_bars": warmup,
            "campaigns": len(trades),
            "win_rate_pct": round(100.0 * wins / len(trades), 2) if trades else None,
            "gross_return_sum": round(gross, 8),
            "fee_cost_return_sum": round(fees, 8),
            "funding_cost_return_sum": round(funding, 8),
            "net_measured_sum": round(sum(nets), 8),
            "net_with_modelled_slippage_10_sum": round(net_with_modelled_slippage_10, 8),
            "failure_attribution": breakdown.as_dict(),
            "conservation_verified": breakdown.verify_conservation(),
            "robustness_vector": metrics.as_dict(),
            # #457: the vector's provenance travels with it — every field names its own basis
            # and every null names the input this producer does not carry.
            "robustness_vector_basis": metrics.field_reports(),
            "robustness_vector_unmeasured": metrics.unmeasured_fields(),
            "drawdown_basis": DRAWDOWN_BASIS,
            "double_entry_reconciled": metrics.is_double_entry_reconciled(),
            # #487: the verdict above is the vector's own; when it is absent (no residual was
            # ever measured) the artifact names why, so a reader cannot read a null verdict as
            # a reconciled one either.
            "double_entry_reconciled_reason": (
                FIELD_ABSENCE_REASON["cashflow_discrepancy_usdt"]
                if metrics.is_double_entry_reconciled() is None
                else None
            ),
            "system_proving_receipt": receipt.as_dict(),
            # #459: which bracket contract the published columns were produced under, the
            # counter reading with its own counts, and the divergence class between them.
            "published_reading": PUBLISHED_READING,
            "bracketless_campaigns": bracketless_campaigns,
            "bracketed_campaigns": len(trades) - bracketless_campaigns,
            "divergence_class_counts": dict(sorted(divergence_counts.items())),
            "bracket_contract": family_bracket_contract,
            "trades": trades,
        }

    path = out_dir / "paper_trade_4y.json"
    path.write_text(json.dumps(report, indent=2, sort_keys=True, default=str) + "\n")

    for policy_id, entry in report["families"].items():
        print(f"[PAPER-4Y] {policy_id}: kampanya={entry['campaigns']} "
              f"kazanma={entry['win_rate_pct']}% net(measured)={entry['net_measured_sum']:+.8f}")
        print(f"           fee={entry['fee_cost_return_sum']:+.8f} funding={entry['funding_cost_return_sum']:+.8f} "
              f"net(+slip10)={entry['net_with_modelled_slippage_10_sum']:+.8f}")
        print(f"           attribution: {entry['failure_attribution']['counts_by_domain']} "
              f"conservation={entry['conservation_verified']}")
        print(f"           receipt: {entry['system_proving_receipt']['receipt_id']} "
              f"double_entry={entry['double_entry_reconciled']}"
              + (f" ({entry['double_entry_reconciled_reason']})"
                 if entry["double_entry_reconciled_reason"] else ""))
        unmeasured = ", ".join(item["field"] for item in entry["robustness_vector_unmeasured"])
        print(f"           robustness vector: every measured field carries its own basis; "
              f"null (unmeasured) = {unmeasured or 'none'}")
        contract = entry["bracket_contract"]
        print(f"           bracket contract: published={contract['published_reading']} "
              f"bracketless={contract['bracketless_campaigns']} "
              f"divergence={contract['divergence_class_counts'] or 'none'}")
        print(f"           stop exits: published={contract['reading_a']['stop_exits']} "
              f"(of which protective={contract['reading_a']['protective_stops']}, "
              f"at the entry reference={contract['reading_a']['stop_exits_charged_to_decisions_without_protection']}) "
              f"| decision contract={contract['reading_b']['stop_exits']} "
              f"(charged to a bracketless decision="
              f"{contract['reading_b']['stop_exits_charged_to_decisions_without_protection']})")
    print(f"[PAPER-4Y] artifact sha256:{_sha256(path)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
