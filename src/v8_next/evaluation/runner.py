"""D-153 End-to-End Benchmark Runner (Rule 12, Rule 57, D-153).

Orchestrates:
1. BenchmarkCase specification and physical evidence verification.
2. NautilusTrader BacktestEngine execution of the 28-expert ensemble.
3. Physical trade/order ledger serialization and ArtifactBinding hashing.
4. CapabilityScore calculation and GateVector evaluation.
5. Self-verifying BenchmarkReceipt generation and BenchmarkLedger persistence.
6. PolicyCertificate generation and ReadinessIndex derivation.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import asdict
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping, Sequence

from pydantic import BaseModel, ConfigDict

from v8_next.adapters.execution_models import ExecutionProfile
from v8_next.adapters.expert_strategy import (
    ExpertStrategyConfig,
    run_expert_strategy_backtest,
)
from v8_next.certification import (
    minerva_for_certificate,
    projection_for_certificate,
)
from v8_next.domain.capital_policy import CapitalPolicy
from v8_next.domain.market import Candle
from v8_next.evaluation.benchmark_receipt import (
    BenchmarkLedger,
    BenchmarkReceipt,
    G3ScenarioBasis,
    GateState,
    GateVector,
    ScoreEvidence,
    WindowEvidence,
)
from v8_next.evaluation.certificate import PolicyCertificate
from v8_next.evaluation.claims import StatutoryClaimRecord
from v8_next.evaluation.economic_benchmark import (
    BOOTSTRAP_REPS,
    BOOTSTRAP_SEED,
    bars_from_candles,
    campaign_accounting,
    pair_positions,
    parse_money,
    reconcile_native_account,
)
from v8_next.evaluation.gate_resolution import (
    MEASURED,
    WindowBinding,
    classify_market_regimes,
    evaluate_g3_scenario_robustness,
    evaluate_g4_synthetic_falsification,
    evaluate_g5_selection_control,
    evaluate_g6_frozen_oos,
    evaluate_g7_prospective_shadow,
    evaluate_g8_live_realization,
    evaluate_g9_certificate_authority,
    window_identity,
)
from v8_next.evaluation.parity import ArtifactBinding
from v8_next.evaluation.scoring import (
    compute_capability_breakdown,
    compute_capability_score,
    dual_scoring,
    evaluate_gate_vector,
)
from v8_next.evaluation.statistics_plan import CANONICAL_G5_BLOCK_SIZE, g5_plan

#: Initial engine balance in USDT for this runner path. Pinned explicitly so the
#: accounting reconciliation replays the same number instead of inheriting an
#: adapter default that could drift (NX02.R4).
ENGINE_INITIAL_BALANCE = Decimal("10000")

#: Declared unit of the series handed to G5 selection control (#407). G5's
#: registered execution variants are proportional frictions (1bp taker / 2bp
#: slippage), so the estimator's input must be the dimensionless per-campaign
#: return from the shared accounting contract — never the absolute USDT PnL
#: series that feeds scoring, which would silently mix two units inside one
#: DSR/WRC estimator (NX02.R3).
G5_SERIES_UNIT = "dimensionless_campaign_return"

#: Reason tokens for a ledger row whose own realized PnL could not be
#: produced. Absence is published as absence, never as a foreign value.
PNL_UNMEASURED_OPEN = "OPEN_CAMPAIGN_UNREALIZED_AT_CUTOFF"
PNL_UNMEASURED_UNPARSABLE = "CLOSE_PNL_UNPARSABLE"
PNL_UNMEASURED_UNPAIRED = "NO_LIFECYCLE_PAIR"
PNL_UNMEASURED_OUT_OF_RANGE = "PNL_EXCEEDS_TAPE_RANGE_BOUND"


class BenchmarkCase(BaseModel):
    """Specification of a Benchmark evaluation job."""

    model_config = ConfigDict(frozen=True)

    case_id: str
    policy_id: str
    dataset_name: str
    instrument_id: str = "BTCUSDT-PERP.BINANCE"
    strategy_config: ExpertStrategyConfig = ExpertStrategyConfig()
    allowed_populations: tuple[str, ...] = ("BurnedDiagnosticReal",)


class BenchmarkRunResult(BaseModel):
    """Output of an end-to-end benchmark run."""

    model_config = ConfigDict(frozen=True)

    case: BenchmarkCase
    receipt: BenchmarkReceipt
    certificate: PolicyCertificate
    total_bars: int
    total_trades: int
    #: ``None`` when the run had no eligible measurement (NX08.R1): a missing
    #: score stays missing instead of being reported as zero.
    capability_score: float | None = None
    gates: GateVector
    ledger_entry_hash: str
    native_ledger_binding: ArtifactBinding
    claim_record: StatutoryClaimRecord | None = None
    gate_metrics: dict[str, Any] | None = None
    domain_scores: dict[str, Any] | None = None
    accounting: dict[str, Any] | None = None
    reconciliation: dict[str, Any] | None = None


def measure_candle_lineage(candles: Sequence[Candle]) -> tuple[bool | None, dict[str, Any]]:
    """Genuine G0 input: single-instrument, gap-free, overlap-free ordering.

    Returns (lineage_ok, info). None means unmeasurable (no candles) and must
    resolve to UNKNOWN downstream, never PASS.
    """
    if not candles:
        return None, {"status": "UNRUN", "reason": "NO_CANDLES"}
    instruments = sorted({c.instrument_id for c in candles})
    if len(instruments) != 1:
        return False, {
            "status": "BLOCKED",
            "reason": "MIXED_INSTRUMENTS",
            "instruments": instruments,
            "n_bars": len(candles),
        }
    ordered = sorted(candles, key=lambda c: (c.start_ns, c.end_ns))
    for a, b in zip(ordered, ordered[1:], strict=False):
        if b.start_ns < a.end_ns:
            return False, {
                "status": "BLOCKED",
                "reason": "OVERLAP_OR_DUPLICATE",
                "at_ns": b.start_ns,
                "n_bars": len(candles),
            }
        if b.start_ns > a.end_ns:
            return False, {
                "status": "BLOCKED",
                "reason": "LINEAGE_GAP",
                "gap_ns": [a.end_ns, b.start_ns],
                "n_bars": len(candles),
            }
    return True, {
        "status": "MEASURED",
        "reason": "SINGLE_INSTRUMENT_GAP_FREE",
        "instrument": instruments[0],
        "n_bars": len(candles),
        "span_ns": [ordered[0].start_ns, ordered[-1].end_ns],
    }


def build_input_binding(
    case: BenchmarkCase,
    candles: Sequence[Candle],
    capital_fields: dict[str, Any],
    extra_identity: Mapping[str, str] | None = None,
) -> str:
    """Canonical input identity for the receipt digest (v4).

    Binds case/policy identity, strategy config, bar count/span, per-candle
    keys (ns bounds, close, instrument, source hash) and capital assumptions.
    Any tape/config substitution changes this digest and fails verification.

    ``extra_identity`` (NX05.R2) carries the shared run identity — tape sha256,
    window, execution profile, code/lock hash — so the receipt of one run can
    never verify as the receipt of another window of the same tape.
    """
    per_candle = [
        [c.start_ns, c.end_ns, str(c.close), c.instrument_id, c.source_hash] for c in candles
    ]
    candles_digest = hashlib.sha256(
        json.dumps(per_candle, separators=(",", ":")).encode()
    ).hexdigest()
    payload = {
        "case_id": case.case_id,
        "policy_id": case.policy_id,
        "dataset": case.dataset_name,
        "strategy_config": json.dumps(asdict(case.strategy_config), sort_keys=True, default=str),
        "n_bars": len(candles),
        "span_ns": [candles[0].start_ns, candles[-1].end_ns] if candles else [],
        "candles_digest": candles_digest,
        "capital": capital_fields,
        "run_identity": dict(sorted((extra_identity or {}).items())),
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, default=str).encode()
    ).hexdigest()


def instrument_price_ranges(candles: Sequence[Candle]) -> dict[str, Decimal]:
    """Per-instrument ``max(high) - min(low)`` over the run's tape window.

    This is the largest per-unit price excursion the tape can produce, so it
    bounds what any single position on this window could have realized.
    """
    highs: dict[str, Decimal] = {}
    lows: dict[str, Decimal] = {}
    for candle in candles:
        key = candle.instrument_id
        if key not in highs or candle.high > highs[key]:
            highs[key] = candle.high
        if key not in lows or candle.low < lows[key]:
            lows[key] = candle.low
    return {key: highs[key] - lows[key] for key in highs}


def _tape_range_bound(
    open_record: Mapping[str, Any], price_ranges: Mapping[str, Decimal] | None
) -> Decimal | None:
    """``|quantity| * (max high - min low)`` for the record's instrument."""
    if not price_ranges:
        return None
    price_range = price_ranges.get(str(open_record["instrument_id"]))
    quantity = parse_money(open_record.get("quantity"))
    if price_range is None or quantity is None:
        return None
    return abs(quantity) * price_range


def attribute_campaign_pnl(
    opened_positions: Sequence[Any],
    closed_positions: Sequence[Any],
    *,
    pnl_unit: str,
    price_ranges: Mapping[str, Decimal] | None = None,
) -> list[dict[str, Any]]:
    """One ledger row per native trade, each carrying *its own* realized PnL (#407).

    Attribution uses the shared :func:`pair_positions` lifecycle contract
    (instrument + position slot + event order), so under NETTING a reused
    ``position_id`` can never hand one campaign another campaign's PnL, and the
    chronological pairing keeps each row bound to the close that consumed it.

    A row whose own realized PnL cannot be produced is published as
    ``pnl = None`` plus a named reason:

    * ``OPEN_CAMPAIGN_UNREALIZED_AT_CUTOFF`` — the campaign is still open, so it
      has no realized PnL yet. Its cutoff mark lives in the separately declared
      ``open_risk_usdt`` of the accounting view; it is not this column's value.
    * ``CLOSE_PNL_UNPARSABLE`` — the engine close carried no parsable PnL.
    * ``NO_LIFECYCLE_PAIR`` — no pairing record for this open (absent, not zero).
    * ``PNL_EXCEEDS_TAPE_RANGE_BOUND`` — the value exceeds
      ``|quantity| * (max high - min low)`` on this window, which no trade of
      this size on this tape could have realized.

    In every case the row carries ``pnl_unit`` and the bound that was applied,
    so a consumer can tell an unmeasured row from a measured one without
    consulting any other artifact.
    """
    open_records = [o for o in opened_positions if isinstance(o, dict)]
    close_records = [c for c in closed_positions if isinstance(c, dict)]

    attribution: dict[int, tuple[float | None, str | None, int | None]] = {}
    for open_rec, close_rec in pair_positions(list(open_records), list(close_records)):
        if close_rec is None:
            attribution[id(open_rec)] = (None, PNL_UNMEASURED_OPEN, None)
            continue
        close_ns = int(close_rec.get("event_ns", 0) or 0)
        realized = parse_money(close_rec.get("realized_pnl"))
        if realized is None:
            attribution[id(open_rec)] = (None, PNL_UNMEASURED_UNPARSABLE, close_ns)
            continue
        attribution[id(open_rec)] = (float(realized), None, close_ns)

    rows: list[dict[str, Any]] = []
    for open_rec in open_records:
        pnl_val, reason, attributed_close_ns = attribution.get(
            id(open_rec), (None, PNL_UNMEASURED_UNPAIRED, None)
        )
        bound = _tape_range_bound(open_rec, price_ranges)
        if pnl_val is not None and bound is not None and abs(pnl_val) > float(bound):
            pnl_val, reason = None, PNL_UNMEASURED_OUT_OF_RANGE
        rows.append(
            {
                "trade_id": open_rec["position_id"],
                "instrument_id": open_rec["instrument_id"],
                "side": open_rec["side"],
                "quantity": open_rec["quantity"],
                "fill_time_ns": open_rec["event_ns"],
                "close_time_ns": attributed_close_ns,
                "pnl": pnl_val,
                "pnl_unit": pnl_unit,
                "pnl_tape_range_bound_usdt": None if bound is None else float(bound),
                "pnl_unmeasured_reason": reason,
                "campaign_completed": pnl_val is not None,
            }
        )
    return rows


def summarize_pnl_attribution(
    rows: Sequence[Mapping[str, Any]],
    *,
    accounting: Any,
    price_ranges: Mapping[str, Decimal] | None = None,
) -> dict[str, Any]:
    """Cross-check the attributed rows against the accounting aggregate (#407).

    The row total and the accounting total are computed independently from the
    same pairing contract; ``consistent_with_accounting`` is False whenever a
    row was published as unmeasured for a value the accounting view counted, or
    the other way round.
    """
    measured_total = sum(
        (Decimal(str(row["pnl"])) for row in rows if row["pnl"] is not None),
        Decimal(0),
    )
    accounting_total = sum(
        (Decimal(str(value)) for value in accounting.realized_pnl_usdt), Decimal(0)
    )
    reasons: dict[str, int] = {}
    for row in rows:
        reason = row["pnl_unmeasured_reason"]
        if reason is not None:
            reasons[str(reason)] = reasons.get(str(reason), 0) + 1
    return {
        "rows": len(rows),
        "measured_rows": sum(1 for row in rows if row["pnl"] is not None),
        "unmeasured_reasons": reasons,
        "measured_total_usdt": str(measured_total),
        "accounting_total_usdt": str(accounting_total),
        "consistent_with_accounting": measured_total == accounting_total,
        "pnl_unit": str(accounting.units["pnl"]),
        "g5_series_unit": G5_SERIES_UNIT,
        "g5_series_samples": len(accounting.campaign_returns),
        "tape_price_ranges_usdt": {
            key: str(value) for key, value in sorted((price_ranges or {}).items())
        },
        "unmeasured_rows_are_null": all(
            row["pnl"] is None
            for row in rows
            if row["pnl_unmeasured_reason"] is not None
        ),
    }


class BenchmarkRunner:
    """Executes a benchmark case through NautilusTrader and records cryptographic receipt."""

    def __init__(self, output_dir: Path | str = "artifacts/benchmarks") -> None:
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.ledger_path = self.output_dir / "benchmark_ledger.jsonl"
        self.ledger = BenchmarkLedger.load_jsonl(self.ledger_path)

    def run(
        self,
        case: BenchmarkCase,
        candles: Sequence[Candle],
        resolve_gates: bool = False,
        tape_path: Path | str | None = None,
        live_fills_path: Path | str | None = None,
        capital_policy: CapitalPolicy | str | Path | None = None,
        execution_profile: str | ExecutionProfile | None = None,
        measure_determinism: bool = False,
        run_identity: Mapping[str, str] | None = None,
        scored_window: WindowBinding | None = None,
        window_evidence: WindowEvidence | None = None,
    ) -> BenchmarkRunResult:
        """Execute the complete benchmark run.

        capital_policy: None -> safe unauthorized default (no live orders, no promotion).
        Accepts CapitalPolicy instance, path to JSON, or None. Test policies verify accept/reject.
        Never prompts, never blocks.

        execution_profile: named Nautilus simulated-execution profile (fill model,
        fee model, latency model, liquidity/queue knobs). None keeps the engine
        defaults, and the receipt then reports that no execution semantics were
        declared rather than implying a model that was never chosen.

        scored_window: the window the caller declares it handed in (#406/#421). It is
        recorded, and it is the boundary a gate may compare a protected or prospective
        window against; no gate may re-label this same window as out-of-sample.

        window_evidence: the evidence class of the window that produced this run
        (#444). The producing path declares it (``WindowEvidence.from_window`` off the
        same ``WindowSpec`` it built), it is bound into the receipt digest, and a class
        that proves no economic evidence mints no capability score. ``None`` means the
        caller declared no window class at all; the receipt then carries none either,
        instead of the runner guessing one.
        """
        # 0. Capital policy validation (Task 4): safe unauthorized default, no live orders.
        if isinstance(capital_policy, CapitalPolicy):
            _cp = capital_policy
        elif isinstance(capital_policy, (str, Path)):
            _cp = CapitalPolicy.from_file(capital_policy)
        elif capital_policy is None:
            _cp = CapitalPolicy.unauthorized()
        else:
            _cp = CapitalPolicy.model_validate(capital_policy)  # dict-like
        _capital_decision = _cp.decision()
        # Expose for downstream parity; no prompt, no live order path.
        # Unauthorized policy does NOT block execution; it gates capital verdict only.
        # Engine still runs locally (paper/simulation); no venue orders emitted.

        # 0b. Genuine G0 lineage input (measured, never assumed).
        # G1 (causal PIT) and G2 (determinism rerun) have no measurement in this
        # runner, so they resolve to UNKNOWN downstream via None (never PASS).
        _lineage_ok, _lineage_info = measure_candle_lineage(tuple(candles))
        # 0c. Canonical input identity bound into the receipt digest (v4).
        _input_binding = build_input_binding(
            case, tuple(candles), _cp.to_receipt_fields(), run_identity
        )

        # 1. Run NautilusTrader Backtest
        backtest_result = run_expert_strategy_backtest(
            tuple(candles),
            case.strategy_config,
            initial_balance=ENGINE_INITIAL_BALANCE,
            execution_profile=execution_profile,
        )

        opened_positions = backtest_result["opened_positions"]
        closed_positions = backtest_result.get("closed_positions", [])
        decisions = backtest_result["decisions"]
        total_bars = len(candles)

        # 2. Accounting (NX02): both reporting paths share this one lifecycle and
        # accounting contract. Absolute PnL is USDT; per-campaign returns are
        # dimensionless and are never mixed into the same series. Open campaigns
        # are marked at the cutoff bar end (no artificial close, no future price).
        bars_view = bars_from_candles(tuple(candles))
        accounting = campaign_accounting(
            opened_positions,
            closed_positions,
            bars=bars_view,
            cutoff_ns=candles[-1].end_ns if candles else 0,
            capital=float(ENGINE_INITIAL_BALANCE),
        )
        pnl_series: list[float] = list(accounting.realized_pnl_usdt)
        # trade_count is the number of COMPLETED campaigns; open risk is reported
        # separately in `accounting` and never counted as a trade.
        total_trades = accounting.trade_count
        reconciliation = reconcile_native_account(
            backtest_result.get("account") or {},
            accounting=accounting,
            initial_balance=float(ENGINE_INITIAL_BALANCE),
        )

        abstain_count = sum(d["abstain_count"] for d in decisions)
        total_expert_votes = max(1, len(decisions) * 28)
        abstain_rate = abstain_count / total_expert_votes

        # NX08.R1: no fixed coverage constant. The breakdown derives coverage from
        # the domains this run actually measured and names the ones it did not;
        # the receipt carries the derived factor (or None when nothing is eligible).
        coverage_factor = None
        # Measured execution evidence from the engine run feeds ExecutionFidelity
        # in place of the PnL-Sharpe proxy whenever real fills were measured.
        execution_evidence = backtest_result.get("execution") or {}

        # Optional measured determinism rerun. The G2 gate previously reported
        # UNRUN_DETERMINISM_RERUN_NOT_PERFORMED because nothing ever compared two
        # engine executions. With explicit execution semantics the fill
        # signature is a real identity to compare, so G2 becomes measurable
        # evidence instead of a permanent UNKNOWN. Opt-in: it doubles engine time.
        g2_metric: dict[str, Any] = {
            "status": "UNRUN",
            "reason": "DETERMINISM_RERUN_NOT_PERFORMED",
            "note": "single engine execution only; rerun-parity check not performed; UNKNOWN, never PASS",
        }
        if measure_determinism:
            rerun = run_expert_strategy_backtest(
                tuple(candles),
                case.strategy_config,
                initial_balance=ENGINE_INITIAL_BALANCE,
                execution_profile=execution_profile,
            )
            first_exec = backtest_result.get("execution") or {}
            second_exec = rerun.get("execution") or {}
            first_sig = first_exec.get("fill_signature")
            second_sig = second_exec.get("fill_signature")
            fills = int(first_exec.get("fills_count") or 0)
            positions_match = (
                backtest_result["opened_positions"] == rerun["opened_positions"]
                and backtest_result.get("closed_positions", [])
                == rerun.get("closed_positions", [])
            )
            if fills == 0 or not first_sig or not second_sig:
                g2_metric = {
                    "status": "UNKNOWN",
                    "reason": "NO_FILLS_TO_COMPARE",
                    "fills_count": fills,
                    "note": "an empty fill set cannot evidence rerun parity; UNKNOWN, never PASS",
                }
            elif first_sig == second_sig and positions_match:
                g2_metric = {
                    "status": "PASS",
                    "reason": "DETERMINISM_RERUN_EXACT_FILL_MATCH",
                    "fill_signature": first_sig,
                    "fills_count": fills,
                    "positions_match": True,
                }
            else:
                g2_metric = {
                    "status": "FAIL",
                    "reason": "DETERMINISM_RERUN_DIVERGED",
                    "fill_signature_first": first_sig,
                    "fill_signature_second": second_sig,
                    "positions_match": positions_match,
                    "fills_count": fills,
                }
        # G2 gate state from the measured rerun (UNKNOWN when not measured).
        _g2_state = (
            GateState.PASS
            if g2_metric.get("status") == "PASS"
            else GateState.BLOCKED
            if g2_metric.get("status") == "FAIL"
            else GateState.UNKNOWN
        )
        breakdown = compute_capability_breakdown(
            pnl_series=pnl_series,
            total_bars=total_bars,
            total_trades=total_trades,
            abstain_rate=abstain_rate,
            coverage_factor=coverage_factor,
            execution=execution_evidence,
        )
        capability_score = compute_capability_score(
            pnl_series=pnl_series,
            total_bars=total_bars,
            total_trades=total_trades,
            abstain_rate=abstain_rate,
            coverage_factor=coverage_factor,
            execution=execution_evidence,
        )
        coverage_factor = breakdown["coverage_factor"]
        # NX08.R5: both scorer versions are computed on this run's fixed inputs and
        # travel in the receipt side by side; the delta is transform-only by
        # construction because the measurements are the same object.
        scoring_versions = dual_scoring(
            pnl_series=pnl_series,
            total_bars=total_bars,
            total_trades=total_trades,
            abstain_rate=abstain_rate,
            execution=execution_evidence,
        )

        # #444 sibling (D153 path): the declared window class decides whether this
        # run may publish a numeric capability score at all. A window that proves no
        # economic evidence (a bar-count ``smoke`` run) is liveness/mechanics
        # evidence: the run still lands in the ledger and still names its class, but
        # it mints no score — and no per-domain decomposition of one, which would be
        # the same claim in smaller pieces.
        domain_scores: dict[str, Any] | None = breakdown["domains"] or None
        if window_evidence is not None and not window_evidence.admits_capability_score():
            capability_score = None
            domain_scores = None

        gate_metrics: dict[str, Any] = {}
        claim_record: StatutoryClaimRecord | None = None
        g3_basis: G3ScenarioBasis | None = None
        pre_pass = False
        # Structural gate inputs: G0 measured above; G1/G2 unmeasured here.
        gate_metrics["g0"] = dict(_lineage_info)
        gate_metrics["execution"] = dict(execution_evidence)
        gate_metrics["g1"] = {
            "status": "UNRUN",
            "reason": "CAUSAL_PIT_NOT_MEASURED_IN_RUNNER",
            "note": "per-bar decision-vs-availability audit not performed; UNKNOWN, never PASS",
        }
        gate_metrics["g2"] = dict(g2_metric)
        gate_metrics["accounting"] = accounting.as_dict()
        gate_metrics["reconciliation"] = dict(reconciliation)

        if resolve_gates:
            # 1. G3: Scenario Robustness across 4 market regimes
            regimes = classify_market_regimes(tape_path=tape_path, candles=candles)
            g3_state, g3_m = evaluate_g3_scenario_robustness(regimes, case.strategy_config)
            gate_metrics["g3"] = g3_m
            # #495 R3: the measured partition travels with the state it decided.
            g3_basis = G3ScenarioBasis.from_metrics(g3_state, g3_m)

            # 2. G4: Synthetic / Adversarial Falsification
            g4_state, g4_m = evaluate_g4_synthetic_falsification(candles, case.strategy_config)
            gate_metrics["g4"] = g4_m

            # 3. G5: Selection Control (DSR & WRC). The series is the declared
            # dimensionless per-campaign return from the same accounting
            # contract that produced the ledger rows (#407); the absolute USDT
            # PnL series above stays with the scoring path, never mixed in here.
            g5_plan_ = g5_plan(
                family=f"D153-G5:{case.case_id}",
                pinned_ns=int(candles[0].start_ns),
                block_size=CANONICAL_G5_BLOCK_SIZE,
                reps=BOOTSTRAP_REPS,
                seed=BOOTSTRAP_SEED,
            )
            g5_state, g5_m = evaluate_g5_selection_control(
                list(accounting.campaign_returns),
                candles,
                plan=g5_plan_,
                # NX07.R3: the regime fallback is authorized here explicitly and
                # with a stated basis; it is no longer a silent substitution.
                allow_regime_fallback=True,
                fallback_tape=tape_path,
                fallback_basis=(
                    "windowed run: the own track holds fewer than 20 campaigns by "
                    "construction, so the regime series is used in the same declared "
                    "unit (NX02.R3) and reported as sample_source=regime_fallback"
                ),
            )
            gate_metrics["g5"] = g5_m

            # 4. G6: Frozen OOS Replication. #406/#421: this path registers no
            # protected window, so the gate must not publish a retention verdict
            # over the run's own scored window -- it fails closed by name.
            g6_state, g6_m = evaluate_g6_frozen_oos(candles, case.strategy_config)
            gate_metrics["g6"] = g6_m

            # 5. G7: Prospective Shadow. #406/#421: this path has no declared
            # forward window either, so no prospective state is minted from the
            # run's own bars.
            g7_state, g7_m = evaluate_g7_prospective_shadow(candles, case.strategy_config, output_dir=self.output_dir)
            gate_metrics["g7"] = g7_m

            # 6. G8: Live Realization (D-152 §5 Diagnostic Fold or PASS with signed fills)
            g8_state, g8_m = evaluate_g8_live_realization(live_fills_path=live_fills_path)
            gate_metrics["g8"] = g8_m

            # #406/#421: the window contract travels with the run, so a verdict can
            # be read together with the windows it was (not) measured over.
            gate_metrics["window_binding"] = {
                "scored_window": window_identity(tuple(candles)),
                "declared_window": dict(
                    sorted(
                        (k, v)
                        for k, v in (run_identity or {}).items()
                        if str(k).startswith("window_") or k == "profile"
                    )
                ),
                "declared_scored_window": (
                    scored_window.as_dict() if scored_window is not None else None
                ),
                "protected_window": None,
                "prospective_window": None,
                "note": (
                    "no protected (G6) or prospective (G7) window is registered for this "
                    "run; both gates therefore publish no PASS over the scored window"
                ),
            }

            # Check if all prerequisite gates G0-G7 passed. A gate that could only
            # judge an unbound window is not an established member of the vector,
            # so pre_pass also requires that G6 and G7 were actually measured.
            pre_pass = all(
                st == GateState.PASS
                for st in (g3_state, g4_state, g5_state, g6_state, g7_state)
            ) and g6_m.get("measurement") == MEASURED and g7_m.get("measurement") == MEASURED
            g9_pre_state = GateState.MISSING

            gates = evaluate_gate_vector(
                total_bars=total_bars,
                total_trades=total_trades,
                pnl_series=pnl_series,
                mismatches=None,
                has_continuous_lineage=_lineage_ok,
                is_causal_pit=None,
                g2_state=_g2_state,
                g3_state=g3_state,
                g4_state=g4_state,
                g5_state=g5_state,
                g6_state=g6_state,
                g7_state=g7_state,
                g8_state=g8_state,
                g9_state=g9_pre_state,
            )
        else:
            gates = evaluate_gate_vector(
                total_bars=total_bars,
                total_trades=total_trades,
                pnl_series=pnl_series,
                mismatches=None,
                has_continuous_lineage=_lineage_ok,
                is_causal_pit=None,
                g2_state=_g2_state,
            )

        # 3. Export physical trade ledger to disk. Every row carries THAT trade's
        # own realized PnL in the single declared unit; a value that cannot be
        # attributed to the row is published as null plus a named reason rather
        # than another campaign's PnL or a rescaled one (#407).
        entry_idx = len(self.ledger.entries)
        suffix = f"_{entry_idx}" if entry_idx > 0 else ""
        ledger_file = self.output_dir / f"{case.case_id}{suffix}_trades.jsonl"
        price_ranges = instrument_price_ranges(tuple(candles))
        trade_rows = attribute_campaign_pnl(
            opened_positions,
            closed_positions,
            pnl_unit=accounting.units["pnl"],
            price_ranges=price_ranges,
        )
        gate_metrics["pnl_attribution"] = summarize_pnl_attribution(
            trade_rows, accounting=accounting, price_ranges=price_ranges
        )

        with open(ledger_file, "w", encoding="utf-8") as f:
            for r in trade_rows:
                f.write(json.dumps(r) + "\n")

        # Bind physical artifact via SHA-256
        if trade_rows:
            artifact_binding = ArtifactBinding.from_file("native_trades", ledger_file)
            bindings = (artifact_binding,)
        else:
            ledger_file.touch()
            artifact_binding = ArtifactBinding.from_file("native_trades", ledger_file)
            bindings = (artifact_binding,)

        # 4. Generate self-verifying BenchmarkReceipt
        computed_at_ns = candles[-1].end_ns if candles else 0
        # #446: the two time quantities are named apart. `computed_at_ns` is the end of the
        # window this run measured (the canon field keeps that meaning byte-for-byte); the
        # run's own wall clock is read here and travels beside the digest, outside every
        # canon, so the same inputs still hash to the same receipt_digest on a rerun.
        run_time_ns = time.time_ns()
        window_end_ns: int | None = computed_at_ns if candles else None
        # #408: the determinants of the number above travel with it, so the
        # published score is a function of the evidence the receipt binds and any
        # consumer can recompute it from the receipt alone.
        score_evidence = ScoreEvidence.from_breakdown(breakdown)
        receipt = BenchmarkReceipt.create(
            case_id=case.case_id,
            policy_id=case.policy_id,
            capability_score=capability_score,
            coverage_factor=coverage_factor,
            scoring_versions=scoring_versions,
            gates=gates,
            computed_at_timestamp_ns=computed_at_ns,
            artifact_bindings=bindings,
            input_binding=_input_binding,
            score_evidence=score_evidence,
            # #444: the declared window class travels with the receipt, so this
            # score can never be read apart from the window that minted it.
            window_evidence=window_evidence,
            window_end_timestamp_ns=window_end_ns,
            run_time_timestamp_ns=run_time_ns,
            g3_scenario_robustness_basis=g3_basis,
        )

        # 5. Append to append-only BenchmarkLedger
        entry = self.ledger.append(receipt)
        self.ledger.save_jsonl(self.ledger_path)

        # 6. If resolve_gates was requested and prerequisites held, mint the StatutoryClaimRecord via ClaimRegistry
        if resolve_gates:
            if pre_pass:
                g9_state, g9_m, claim_record = evaluate_g9_certificate_authority(
                    ledger=self.ledger,
                    receipt_digest=receipt.receipt_digest,
                    gates=gates,
                    capability_score=capability_score,
                    output_dir=self.output_dir,
                    live_realization_verified=(gates.g8_prospective_shadow == GateState.PASS),
                )
                gate_metrics["g9"] = g9_m
                if g9_state == GateState.PASS:
                    gates = evaluate_gate_vector(
                        total_bars=total_bars,
                        total_trades=total_trades,
                        pnl_series=pnl_series,
                        mismatches=None,
                        has_continuous_lineage=_lineage_ok,
                        is_causal_pit=None,
                        # The re-mint re-states this run, so it hands over the same
                        # structural cells the pre-G9 vector carried -- including the
                        # G2 state this run measured. Omitting it left the rule to
                        # resolve its absent argument to UNKNOWN, and the two ledger
                        # entries for one run disagreed about a hard gate.
                        g2_state=_g2_state,
                        g3_state=g3_state,
                        g4_state=g4_state,
                        g5_state=g5_state,
                        g6_state=g6_state,
                        g7_state=g7_state,
                        g8_state=g8_state,
                        g9_state=GateState.PASS,
                    )
                    receipt = BenchmarkReceipt.create(
                        case_id=case.case_id,
                        policy_id=case.policy_id,
                        capability_score=capability_score,
                        coverage_factor=coverage_factor,
                        gates=gates,
                        computed_at_timestamp_ns=computed_at_ns,
                        artifact_bindings=bindings,
                        input_binding=_input_binding,
                        score_evidence=score_evidence,
                        # the post-G9 receipt declares the same window class
                        window_evidence=window_evidence,
                        # ...and the same two time quantities as the run it re-states
                        window_end_timestamp_ns=window_end_ns,
                        run_time_timestamp_ns=run_time_ns,
                        g3_scenario_robustness_basis=g3_basis,
                    )
                    # The post-G9 receipt is a NEW record: append it so the
                    # returned receipt is bound in the ledger (never orphaned).
                    # The claim above stays parented to the pre-G9 entry.
                    entry = self.ledger.append(receipt)
                    self.ledger.save_jsonl(self.ledger_path)
            else:
                gate_metrics["g9"] = {
                    "status": "BLOCKED",
                    "reason": "Prerequisite gates G0-G7 did not all pass",
                }

        # 7. Generate PolicyCertificate from measured producers. A producer that
        # cannot measure on this path returns a named refusal; the certificate is
        # never handed a bare absent argument (#476).
        projection = projection_for_certificate(
            policy_id=receipt.policy_id,
            benchmark_receipt_id=receipt.receipt_digest,
            capability_score=capability_score,
            campaign_returns=accounting.campaign_returns,
            initial_capital_usd=float(ENGINE_INITIAL_BALANCE),
            has_synthetic_population_only=(
                bool(case.allowed_populations)
                and all(
                    population == "FoundrySyntheticNovelty"
                    for population in case.allowed_populations
                )
            ),
        )
        track_days = (
            (candles[-1].end_ns - candles[0].start_ns) / 86_400_000_000_000.0
            if len(candles) > 1
            else None
        )
        minerva = minerva_for_certificate(
            statistics=None,
            actual_track_days=track_days,
            min_trl_days=None,
            min_trl_plan=None,
            observed_intervals=len(accounting.campaign_returns),
            worst_regime_return_bps=None,
            regime_floor_bps=None,
        )
        certificate = PolicyCertificate.generate(
            receipt, projection=projection, minerva=minerva
        )
        gate_metrics["certificate_inputs"] = {
            "projection": projection.as_dict(),
            "minerva": minerva.as_dict(),
        }

        # Attach capital policy decision to gate_metrics (always, non-blocking).
        # Does not emit live orders, does not block execution, does not prompt.
        _capital_gate_metrics = {"capital_policy": _cp.to_receipt_fields(), "capital_decision": _capital_decision}
        if gate_metrics is not None:
            gate_metrics = {**gate_metrics, **_capital_gate_metrics}
        else:
            # Always expose capital decision even in diagnostic-only mode (safe unauthorized default)
            gate_metrics = _capital_gate_metrics

        return BenchmarkRunResult(
            case=case,
            receipt=receipt,
            certificate=certificate,
            total_bars=total_bars,
            total_trades=total_trades,
            capability_score=capability_score,
            gates=gates,
            ledger_entry_hash=entry.entry_hash,
            native_ledger_binding=artifact_binding,
            claim_record=claim_record,
            gate_metrics=gate_metrics,
            domain_scores=domain_scores,
            accounting=accounting.as_dict(),
            reconciliation=dict(reconciliation),
        )
