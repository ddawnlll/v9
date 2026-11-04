"""Canonical portfolio benchmark: quad tape -> engine P/P+E -> receipt chain.

Single-command reproduction (`--extra research` is required: `scipy`/`arch` are
optional dependencies and the statistical verdict cannot be computed without
them):
    uv run --project v8-next --extra research python -m v8_next.app.cli benchmark-portfolio \
        --tape-path research/tape/quad-1h-12m --bars 336 \
        --output-dir artifacts/portfolio-benchmark --primary equal_weight

P and P+E execute at engine level in one shared account each (same per-leg
risk budget; P+E splits sleeves 50/50). Funding drag is measured by dual-run
balance difference under verified identical trade signatures. Results bind
into the D-153 BenchmarkReceipt/BenchmarkLedger/PolicyCertificate chain with
the economic receipt attached; gate logic itself is untouched. No live orders.
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import sys
import time
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal, Sequence, overload

from v8_next.adapters.basket_backtest import BASIS_ENGINE_SETTLED
from v8_next.adapters.execution_models import DEFAULT_PROFILE as DEFAULT_EXECUTION_PROFILE
from v8_next.adapters.execution_models import PROFILES as EXECUTION_PROFILES
from v8_next.adapters.portfolio_backtest import (
    SleeveSpec,
    run_portfolio_backtest,
    trade_signature,
)
from v8_next.adapters.shadow_ingest import (
    ingest_status,
    reconcile_shadow_account,
)
from v8_next.app.economic import (
    FLOW_SOURCE_ANALYTIC_PER_BAR,
    FLOW_SOURCE_ENGINE_FILLS,
    oos_slice_flow,
)
from v8_next.app.rerun_guard import (
    REFUSED_EXIT_CODE,
    refusal_message,
    registered_run_payload,
)
from v8_next.app.rerun_guard import (
    RERUN_WOULD_REWRITE_REGISTERED_RUN_IDENTITY as RERUN_WOULD_REWRITE_REGISTERED_RUN_IDENTITY,
)
from v8_next.certification import (
    minerva_for_certificate,
    projection_for_certificate,
)
from v8_next.domain.capital_policy import CapitalPolicy
from v8_next.domain.market import Candle
from v8_next.evaluation import economic_benchmark as eb
from v8_next.evaluation.benchmark_receipt import (
    BenchmarkLedger,
    BenchmarkReceipt,
    G3ScenarioBasis,
    GateVector,
    ScoreEvidence,
    WindowEvidence,
)
from v8_next.evaluation.certificate import PolicyCertificate
from v8_next.evaluation.gate_resolution import (
    classify_market_regimes,
    evaluate_g3_scenario_robustness,
    resolve_structural_gates,
)
from v8_next.evaluation.multitape import MultiTape, load_multitape
from v8_next.evaluation.parity import ArtifactBinding
from v8_next.evaluation.report import generate_forensic_html_report, render_identity
from v8_next.evaluation.run_window import (
    RunKey,
    WindowAlreadyCompleted,
    WindowRunManifest,
    WindowSpec,
    assert_window_resumable,
    load_window_manifest,
    write_window_manifest,
)
from v8_next.evaluation.scoring import (
    compute_capability_breakdown,
    compute_capability_score,
    evaluate_gate_vector,
)
from v8_next.evaluation.statistics_plan import (
    CANONICAL_G5_BLOCK_SIZE,
    g5_plan,
)
from v8_next.evaluation.store import ResearchStore, canonical

CASE_ID = "BC-QUAD-PORTFOLIO-01"
POLICY_ID = "pol_portfolio_quad"
PRIMARY_DEFAULT = "equal_weight"
# The named refusal and its exit code live in ``v8_next.app.rerun_guard``; they are
# re-exported here (imported by name) because the portfolio CLI is one of the two
# call sites and this module's tests read them off it.


def parse_utc_ms(value: str) -> int:
    """Parse ``YYYY-MM-DD`` or an ISO-8601 instant as a UTC epoch millisecond."""
    import datetime

    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.datetime.fromisoformat(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a UTC date or instant: {value!r}") from None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.timezone.utc)
    return int(parsed.timestamp() * 1000)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Canonical quad portfolio benchmark.")
    p.add_argument("--case-id", default=CASE_ID)
    p.add_argument("--policy-id", default=POLICY_ID)
    p.add_argument("--tape-path", default="research/tape/quad-1h-12m")
    p.add_argument("--bars", type=int, default=385)
    p.add_argument("--start-bar", type=int, default=0)
    p.add_argument(
        "--start-utc",
        type=parse_utc_ms,
        default=None,
        help="UTC window start (YYYY-MM-DD); replaces the bar-count window.",
    )
    p.add_argument(
        "--end-utc",
        type=parse_utc_ms,
        default=None,
        help="UTC window end, exclusive (YYYY-MM-DD); replaces the bar-count window.",
    )
    p.add_argument("--fold-id", default=None, help="Walk-forward fold id this window is")
    p.add_argument(
        "--profile",
        choices=("smoke", "fold", "benchmark"),
        default=None,
        help=(
            "Execution profile. A bar-count window is a SMOKE run (liveness only, never "
            "economic evidence); fold/benchmark runs must be UTC-bounded."
        ),
    )
    p.add_argument(
        "--allow-rerun",
        action="store_true",
        help=(
            "Override the completed-window guard (re-executes a finished run key). Does "
            "not override a registered run identity: no run this output dir registered "
            "can be re-registered under the same run key."
        ),
    )
    p.add_argument("--output-dir", default="artifacts/portfolio-benchmark")
    p.add_argument("--primary", default=PRIMARY_DEFAULT)
    p.add_argument("--seed", type=int, default=7)
    p.add_argument("--capital", type=float, default=eb.CAPITAL_DEFAULT)
    p.add_argument("--taker-fee", type=float, default=eb.TAKER_FEE_DEFAULT)
    p.add_argument("--per-leg-notional", type=float, default=1000.0)
    p.add_argument("--opex-monthly", type=float, default=30.0)
    p.add_argument("--capital-policy", default=None)
    p.add_argument("--html-out", default=None)
    p.add_argument("--live-fills", default=None)
    p.add_argument(
        "--execution-profile",
        default=DEFAULT_EXECUTION_PROFILE,
        help=(
            "Nautilus simulated-execution profile: fill model, fee model and "
            "latency model actually in force. One of: "
            f"{', '.join(sorted(EXECUTION_PROFILES))}"
        ),
    )
    return p.parse_args(argv)


def build_shadow_section(
    live_fills: str | None, engine_account: dict[str, Any]
) -> dict[str, Any]:
    """Shadow/live evidence via the single shadow_ingest source.

    No venue account => UNRUN with format/source/command (never simulated).
    Fixture paths never count as live (fixture guard).
    """
    from v8_next.adapters.shadow_ingest import load_shadow_fills

    if not live_fills:
        status = ingest_status(None)
        return {**status, "live_fills_present": False}
    fills, meta = load_shadow_fills(live_fills, source="live")
    section: dict[str, Any] = {**meta, "live_fills_present": bool(fills)}
    if fills:
        section["reconciliation"] = reconcile_shadow_account(fills, engine_account)
    return section


def capacity_from_participation(
    participations: Sequence[float],
    capital: float,
    p_bar_returns: Sequence[float] | None = None,
    k_grid: tuple[float, ...] = (0.1, 0.5, 1.0),
) -> list[dict[str, Any]]:
    """Participation-bound capacity from real bar quote volumes (#477).

    The bound entries are produced by the swing-capacity contract in
    :mod:`v8_next.evaluation.capacity`, not rebuilt here: a threshold outside
    the participation scale the run actually observed is a labelled projection
    and publishes ``UNRESOLVED`` with its reason, never a ``MEASURED_*`` kind.
    The numeric bound and its extrapolation identity are preserved beside that
    state. Scenario part: square-root impact k*sigma*sqrt(p) with HYPOTHETICAL
    k. sigma is measured (std of portfolio per-bar returns); k is not calibrated
    to any venue data, so scenarios never read as measured capacity.
    Slippage/impact beyond participation is an UNVERIFIED claim.
    """
    import math as _math

    import numpy as _np

    from v8_next.evaluation.capacity import measure_capacity_bounds_from_participation

    out = measure_capacity_bounds_from_participation(participations, capital)
    peak = max(participations) if participations else 0.0
    sigma = None
    if p_bar_returns:
        arr = _np.asarray(list(p_bar_returns), dtype=float)
        if arr.size >= 2:
            sigma = float(arr.std(ddof=1))
    for k in k_grid:
        # #477 R1: EVERY entry this function publishes carries a contract status.
        # An uncalibrated hypothetical impact model mints no economic claim, and one
        # whose sigma or peak participation was not measured is DATA_BLOCKED -- never
        # an absent key, and never a zero dressed up as a measurement.
        scenario_measured = sigma is not None and peak > 0
        out.append(
            {
                "kind": "HYPOTHETICAL_SCENARIO",
                "model": "sqrt_impact_k_sigma_sqrt_p",
                "k": k,
                "status": "NO_ECONOMIC_CLAIM" if scenario_measured else "DATA_BLOCKED",
                "reason": (
                    "HYPOTHETICAL_IMPACT_MODEL_UNCALIBRATED"
                    if scenario_measured
                    else "NO_MEASURED_SIGMA_OR_PEAK_PARTICIPATION"
                ),
                "k_status": "HYPOTHETICAL_UNCALIBRATED",
                "sigma_per_bar": sigma,
                "impact_bps_at_peak": (
                    k * sigma * _math.sqrt(peak) * 1e4
                    if sigma is not None and peak > 0
                    else None
                ),
            }
        )
    return out


def build_shared_inputs(
    tape: MultiTape,
) -> tuple[dict[str, list[Any]], dict[str, list[float]], dict[str, list[float]]]:
    """Single frame/feature build reused across the engine passes (#466).

    Pure function of the loaded tape: per-instrument BarViews, per-leg closes
    on the shared end_ns timeline, and per-leg quote volumes. The 5 engine
    passes (P fund/nofund, P+E fund/nofund, determinism rerun) observe
    identical state because they receive the same objects in the same order;
    CLI flags/outputs unchanged. Build failure fails loudly (no partial reuse).
    """
    shared_bars = {inst: eb.bars_from_candles(tape.candles[inst]) for inst in tape.instruments}
    if not shared_bars:
        raise ValueError("shared build requires at least one instrument leg")
    shared_closes = {
        f"{k}-PERP.BINANCE": [float(c.close) for c in v] for k, v in tape.candles.items()
    }
    shared_qvols = {f"{k}-PERP.BINANCE": list(v) for k, v in tape.quote_volumes.items()}
    return shared_bars, shared_closes, shared_qvols


@overload
def resolve_portfolio_gates(
    candles: Sequence[Candle],
    ledger: BenchmarkLedger,
    *,
    total_trades: int,
    pnl_series: list[float],
    include_g3_basis: Literal[False] = False,
) -> GateVector: ...


@overload
def resolve_portfolio_gates(
    candles: Sequence[Candle],
    ledger: BenchmarkLedger,
    *,
    total_trades: int,
    pnl_series: list[float],
    include_g3_basis: Literal[True],
) -> tuple[GateVector, G3ScenarioBasis]: ...


def resolve_portfolio_gates(
    candles: Sequence[Candle],
    ledger: BenchmarkLedger,
    *,
    total_trades: int,
    pnl_series: list[float],
    include_g3_basis: bool = False,
) -> GateVector | tuple[GateVector, G3ScenarioBasis]:
    """The portfolio run's gate vector: G0/G1/G2 resolved from measurement (#471), G3 too (#495).

    The canonical portfolio run used to build its vector with only the bar count, the
    trade count and the PnL series. The three blocking/required cells were therefore
    decided by the gate rule's keyword defaults (``has_continuous_lineage=True``,
    ``is_causal_pit=True``, ``mismatches=0``) -- a hard-gate PASS standing behind no
    measurement -- and that vector is hashed into ``receipt_digest`` and appended to the
    durable ledger. None of those three quantities exists as a measurement on this path:

    * **G0** is the runner's own candle-lineage measurement, taken on ``candles`` -- the
      bar series this run's window identity is declared on (the receipt's ``span_ns``
      and ``source_hashes`` are read from it). A gap or an overlap fails it closed.
    * **G1** has no per-bar decision-vs-availability audit on this path at all, so the
      resolver publishes it UNKNOWN with ``CAUSAL_PIT_NOT_MEASURED_ON_THIS_PATH``; the
      portfolio run performs no PIT audit, so it may not publish a PASS here.
    * **G2** is measured on ``ledger`` -- the ledger this run appends to. An empty ledger
      verifies vacuously (UNKNOWN, named), a ledger that does not verify is BLOCKED
      (named); a clean ledger still leaves the engine-rerun half unmeasured.

    #495: **G3** was the one ``Required`` cell this path never supplied, so it fell to the
    rule's default (UNKNOWN) and the cell read as "unmeasured" next to a name that
    claimed a benchmark-coverage check nobody performed. The cell is now
    ``g3_scenario_robustness`` -- the quantity the only G3 resolver measures -- and it is
    measured here from this run's own bars: the same regime partition the runner and the
    resolution battery take (:func:`classify_market_regimes`) is re-run through the same
    resolver (:func:`evaluate_g3_scenario_robustness`). It is a diagnostic cell (registry
    role ``DIAGNOSTIC``): a PASS mints no authority, and an unmeasurable partition fails
    closed with its own named reason rather than defaulting.

    The three structural cells published here are the ones :func:`resolve_structural_gates`
    produced for these inputs: the resolver's own measured inputs are handed to the
    canonical rule rather than a second vector being derived, so the receipt carries the
    same resolution the resolver publishes.

    #495 R3: ``include_g3_basis=True`` returns the resolver's measured regime partition
    beside the vector, so the receipt can publish the inputs that reproduce the state.
    The basis is returned only when asked for; the default call still returns the vector
    alone, which keeps the existing producer contract.
    """
    structural = resolve_structural_gates(tuple(candles), ledger)
    # #495: measured on the bars handed to the producer, by the resolver that owns G3.
    regimes = classify_market_regimes(candles=tuple(candles))
    g3_state, g3_metrics = evaluate_g3_scenario_robustness(regimes)
    g3_basis = G3ScenarioBasis.from_metrics(g3_state, g3_metrics)
    vector = evaluate_gate_vector(
        total_bars=len(candles),
        total_trades=total_trades,
        pnl_series=pnl_series,
        # No mismatch count is measured on this path: G2 is the ledger's own verdict,
        # and leaving ``mismatches`` at its default would be an unmeasured zero.
        mismatches=None,
        has_continuous_lineage=structural.inputs.has_continuous_lineage,
        is_causal_pit=structural.inputs.is_causal_pit,
        g2_state=structural.inputs.g2_state,
        g3_state=g3_state,
    )
    if include_g3_basis:
        return vector, g3_basis
    return vector


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    tape_path = Path(args.tape_path)
    if not tape_path.exists():
        print(f"error: real tape not found at {tape_path}; synthetic fallback banned.", file=sys.stderr)
        return 2
    if (args.start_utc is None) != (args.end_utc is None):
        print("error: --start-utc and --end-utc must be given together", file=sys.stderr)
        return 2
    profile = args.profile or ("smoke" if args.start_utc is None else "benchmark")
    if args.start_utc is not None and args.start_bar:
        print("error: --start-bar is a smoke offset; it cannot be combined with a UTC window", file=sys.stderr)
        return 2
    window = WindowSpec(
        tape_path=str(tape_path),
        profile=profile,
        instrument=None,
        start_ms=args.start_utc,
        end_ms=args.end_utc,
        fold_id=args.fold_id,
        bars=None if args.start_utc is not None else args.bars,
    )
    window.validate()
    print(f"[+] Window: {window.label()}")
    if window.is_smoke:
        print(
            "[!] SMOKE RUN: bar-count window. Liveness/mechanics evidence only; NOT a "
            "release benchmark and NOT economic sufficiency evidence."
        )
    tape: MultiTape = load_multitape(
        tape_path,
        limit=window.bars,
        offset=args.start_bar,
        start_ms=window.start_ms,
        end_ms=window.end_ms,
    )
    n = tape.n_bars
    end_ns = [c.end_ns for c in tape.candles[tape.instruments[0]]]
    print(f"[+] quad tape: {list(tape.instruments)} x {n} bars, {len(tape.funding)} funding rows")
    # #490: from here on the window carries the bar count this run actually loaded.
    # ``WindowSpec`` is frozen, and ``bars`` must stay the caller's declaration (a
    # UTC-bounded window declares none), so the measurement is bound in its own field.
    # The run key is unaffected -- it is built from the declared window -- so a
    # completed window is not re-keyed by this.
    window = dataclasses.replace(window, measured_bars=n)

    run_key = RunKey.build(
        window=window,
        case_id=args.case_id,
        policy_id=args.policy_id,
        dataset_sha256=tape.tape_sha256,
        strategy_config=json.dumps(
            {
                "sleeves": ["P", "P+E"],
                "per_leg_notional": args.per_leg_notional,
                "primary": args.primary,
                "seed": args.seed,
                "start_bar": args.start_bar,
            },
            sort_keys=True,
        ),
        capital=str(args.capital),
        taker_fee=str(args.taker_fee),
        baseline=args.primary,
        execution_profile_digest=hashlib.sha256(
            str(args.execution_profile or "UNSPECIFIED").encode()
        ).hexdigest(),
        code_and_lock_hash=eb.code_and_lock_hash(),
    )
    manifest_path = out_dir / "runs" / f"{run_key.digest.split(':')[1][:16]}.json"
    existing = load_window_manifest(manifest_path)
    try:
        assert_window_resumable(existing, run_key.digest)
    except WindowAlreadyCompleted as exc:
        print(f"[!] {exc}", file=sys.stderr)
        if not args.allow_rerun:
            print(
                "[!] refusing to re-execute a completed window (no second ledger append, "
                "no second cash flow). Pass --allow-rerun to override deliberately.",
                file=sys.stderr,
            )
            return REFUSED_EXIT_CODE
    # A re-execution always writes a different payload for the same run key: the
    # payload folds in the sha256 of every artifact, and this run appends to
    # ``benchmark_ledger.jsonl``, so the hash can never match the first
    # registration. ResearchStore.record_run refuses the rewrite -- but only
    # after the whole run has executed and written its artifacts, which surfaced
    # as an uncaught ValueError (exit 1) with the run already recorded COMPLETED.
    # Fail closed here instead: no manifest write, no ledger append, no artifact.
    if registered_run_payload(out_dir, run_key.digest) is not None:
        print(f"[!] {refusal_message(out_dir, run_key.digest)}", file=sys.stderr)
        return REFUSED_EXIT_CODE
    if existing is not None and not existing.completed:
        print(f"[!] INCOMPLETE prior run for this key (state={existing.state}); continuing it.")
    write_window_manifest(
        manifest_path,
        WindowRunManifest(
            run_key=run_key.digest,
            window=window.as_dict(),
            state="RUNNING",
            started_ns=time.time_ns(),
        ),
    )
    print(f"[+] Run key: {run_key.digest}")

    # #466: single shared frame/feature build for the 5 engine passes below.
    # Pure function of the tape; every pass observes identical state. Any pass
    # depending on another pass's side effects would STOP here (none: each
    # run constructs a fresh engine and receives immutable tuples in order).
    shared_bars, shared_closes, shared_qvols = build_shared_inputs(tape)

    # Point-in-time validity of the series this run's numbers come from: every
    # instrument is measured, not asserted. Publishing `chrono_ok=True` and
    # `leak_probe=OK` as literals is what let a receipt claim a validity nobody
    # inspected (#437), so the probes run here and their measured results travel
    # into the verdicts and the controls below.
    series_validity: dict[str, dict[str, Any]] = {}
    for instrument in tape.instruments:
        instrument_bars = shared_bars[instrument]
        chrono_result = eb.validate_chronology(instrument_bars)
        leak_result = eb.detect_future_leak(instrument_bars)
        series_validity[instrument] = {
            "bars": len(instrument_bars),
            "chronology": {"ok": chrono_result[0], "note": chrono_result[1]},
            "leak_probe": {"ok": leak_result[0], "note": leak_result[1]},
            "known_defect_future_leak": eb.future_leak_positive_control(instrument_bars),
        }
    chrono_ok = all(v["chronology"]["ok"] for v in series_validity.values())
    leak_ok = all(v["leak_probe"]["ok"] for v in series_validity.values())
    chrono_note = "; ".join(f"{i}: {v['chronology']['note']}" for i, v in series_validity.items())
    leak_note = "; ".join(f"{i}: {v['leak_probe']['note']}" for i, v in series_validity.items())
    if not chrono_ok or not leak_ok:
        print(f"INVALID input data: {chrono_note} / {leak_note}", file=sys.stderr)
        return 2

    sleeves_p = (SleeveSpec("incumbent", 1, 28, 1.0),)
    sleeves_pe = (SleeveSpec("incumbent", 1, 28, 0.5), SleeveSpec("challenger", 1, 0, 0.5))
    kw: dict[str, Any] = {
        "per_leg_notional": Decimal(str(args.per_leg_notional)),
        "taker_fee": Decimal(str(args.taker_fee)),
        "initial_balance": Decimal(str(args.capital)),
        "execution_profile": args.execution_profile,
    }
    print("[+] engine P (funding + no-funding dual) ...", flush=True)
    p_fund = run_portfolio_backtest(
        tape.candles, sleeves_p, tape.funding, funding_dropped=tape.funding_dropped, **kw
    )
    p_nofund = run_portfolio_backtest(
        tape.candles, sleeves_p, (), funding_dropped=tape.funding_dropped, **kw
    )
    print("[+] engine P+E (funding + no-funding dual) ...", flush=True)
    pe_fund = run_portfolio_backtest(
        tape.candles, sleeves_pe, tape.funding, funding_dropped=tape.funding_dropped, **kw
    )
    print("[+] determinism rerun P ...", flush=True)
    p_nofund2 = run_portfolio_backtest(
        tape.candles, sleeves_p, (), funding_dropped=tape.funding_dropped, **kw
    )

    p_ident = trade_signature(p_nofund) == trade_signature(p_fund)
    pe_nf = run_portfolio_backtest(
        tape.candles, sleeves_pe, (), funding_dropped=tape.funding_dropped, **kw
    )
    pe_ident = trade_signature(pe_nf) == trade_signature(pe_fund)
    det_ok = trade_signature(p_nofund) == trade_signature(p_nofund2)
    if not (p_ident and pe_ident):
        print("error: funding feed changed trade signatures; measurement invalid", file=sys.stderr)
        return 2
    print(
        f"[+] funding coverage: P={p_fund['funding_coverage']} "
        f"(fed={p_fund['funding_settlements_fed']} dropped_at_load={p_fund['funding_dropped_at_load']} "
        f"out_of_window={p_fund['funding_out_of_window']} unknown_leg={p_fund['funding_unknown_leg']})"
    )
    p_drag = float(str(p_nofund["account"]["balance_total"]).split()[0]) - float(
        str(p_fund["account"]["balance_total"]).split()[0]
    )
    pe_drag = float(str(pe_nf["account"]["balance_total"]).split()[0]) - float(
        str(pe_fund["account"]["balance_total"]).split()[0]
    )

    closes = shared_closes
    qvols = shared_qvols
    p_ser = eb.portfolio_series_from_engine(
        p_fund, closes, end_ns, args.capital, args.taker_fee, qvols, tape.funding, p_drag, True
    )
    pe_ser = eb.portfolio_series_from_engine(
        pe_fund, closes, end_ns, args.capital, args.taker_fee, qvols, tape.funding, pe_drag, True
    )
    print(f"[+] P basis={p_ser['cost_basis']} funding={p_ser['funding']:.4f} "
          f"P+E basis={pe_ser['cost_basis']} funding={pe_ser['funding']:.4f}")

    fams = eb.compute_multileg_family(closes, args.capital, args.taker_fee)
    if args.primary not in fams:
        print(f"error: unknown primary {args.primary}", file=sys.stderr)
        return 2
    # The analytic family carries no funding rows of its own, so this path used to
    # publish "no feed reached this curve" on every benchmark leg -- a statement
    # about the wiring, not about the strategy. The feed is now measured per leg
    # by the engine on the tape this run actually loaded: the basket that carries
    # the leg's rule, run twice (real funding rows / none) over the same legs and
    # funding rows. A leg the engine has no basket for says so by name.
    family_funding = eb.measure_benchmark_family_funding(
        {sym: tape.candles[sym] for sym in tape.instruments},
        tape.funding,
        tuple(fams),
        capital=args.capital,
        taker_fee=args.taker_fee,
    )
    feed_summary = ", ".join(
        f"{leg}={spec['funding']:.2f}" if spec.get("funding") is not None
        else f"{leg}={spec.get('funding_basis')}"
        for leg, spec in sorted(family_funding.items())
    )
    print(f"[+] benchmark funding feed: {feed_summary}", flush=True)
    # D-165: the curve the engine measured a leg's funding for is not always the
    # curve that leg is published as. Where the measuring basket sizes its legs
    # under a different convention, that basket's own curve is published beside
    # the analytic index -- the mirror of the engine basket over this run's own
    # legs -- and the index row names the basket that carries the number instead
    # of borrowing it. The mirror is the same builder `compute_benchmark_family_engine`
    # uses, so the two describe one basket, not two rules.
    instrument_views = {
        inst: eb.bars_from_candles(tape.candles[inst]) for inst in tape.instruments
    }
    basket_curves = eb.family_basket_mirror_curves(
        instrument_views, family_funding, capital=args.capital, taker_fee=args.taker_fee,
    )
    primary_eq: list[float] = list(fams[args.primary]["equity"])
    curves: dict[str, dict[str, Any]] = {
        "portfolio_P": {**p_ser, "n_trades": p_ser["n_trades"]},
        "portfolio_PE": {**pe_ser, "n_trades": pe_ser["n_trades"]},
    }
    #: Published curve id -> the `funding_basis` its cell is read with. A leg whose
    #: measuring basket executes the curve's own convention keeps the measured
    #: state; a leg whose basket differs publishes the named state and the basket.
    funding_basis_by_curve: dict[str, str] = {}
    for bid, fam in fams.items():
        differs = eb.funding_basket_convention_differs(bid)
        curves[bid] = {
            "equity": fam["equity"], "exposure": fam["exposure"],
            "turnover": float(fam["turnover"]), "commission": float(fam["commission"]),
            "funding": None if differs else family_funding.get(bid, {}).get("funding"),
            "cost_basis": "ANALYTIC_MODEL", "n_trades": int(fam["n_trades"]),
            # #472: the curve's own per-leg exposure weights travel with it, so the
            # published concentration is measured on this run instead of constant.
            "leg_weights": fam.get("leg_weights"),
        }
        if differs is not None:
            funding_basis_by_curve[bid] = eb.funding_basis_for_convention_differs(differs)
        else:
            basis = family_funding.get(bid, {}).get("funding_basis")
            if basis is not None:
                funding_basis_by_curve[bid] = str(basis)
    for basket_id, curve in basket_curves.items():
        # The measuring basket's own curve: the engine's measured number, on the
        # curve that number was measured for.
        curves[basket_id] = {
            "equity": curve["equity"], "exposure": curve["exposure"],
            "turnover": float(curve["turnover"]), "commission": float(curve["commission"]),
            "funding": curve["funding"], "cost_basis": str(curve["cost_basis"]),
            "n_trades": int(curve["n_trades"]),
            # #472: the basket mirror publishes its own per-leg weights (#472).
            "leg_weights": curve.get("leg_weights"),
        }
        funding_basis_by_curve[basket_id] = str(curve["funding_basis"])

    def raw_count(key: str) -> int | None:
        raw = curves[key].get("raw_equity")
        if not raw:
            return None
        return sum(1 for a, b in zip(raw, raw[1:], strict=False) if abs(b - a) > 1e-9)

    metrics = {
        name: eb.metrics_for_curve(
            list(c["equity"]), list(c["exposure"]), float(c["turnover"]),
            float(c["commission"]), c["funding"], str(c["cost_basis"]), primary_eq,
            int(c["n_trades"]), raw_count(name),
            # #445: the measured closed-loop residual, its terms and its
            # tolerance travel INTO the receipt. A curve that reconciled nothing
            # of its own (the analytic legs) carries none and publishes none:
            # `None` in, `None` out, never an empty block reading as a zero.
            cost_reconciliation=eb.published_cost_reconciliation(
                c.get("cost_reconciliation"), cost_basis=str(c["cost_basis"])
            ),
            # #472: measured from this curve's own leg weights, or named absent.
            leg_weights=c.get("leg_weights"),
        )
        for name, c in curves.items()
    }
    # Attach measured funding to the engine legs (PnL impact, not double-count).
    for name, ser in (("portfolio_P", p_ser), ("portfolio_PE", pe_ser)):
        m = metrics[name]
        metrics[name] = m.model_copy(update={"funding_cost": float(ser["funding"] or 0.0)})
    # Every published curve names its funding state, so a row without a number
    # says which of the declared reasons applies -- and, where the measuring
    # basket sizes differently, which curve carries its number -- instead of
    # leaving the reader to assume "nobody measured it".
    for name, basis in funding_basis_by_curve.items():
        metric = metrics.get(name)
        if metric is None:
            continue
        metrics[name] = metric.model_copy(update={"funding_basis": basis})

    fit = min(eb.OOS_FIT_BARS, n - 48)
    oos_metrics: dict[str, eb.MetricSet] = {}
    if n - fit >= 48:
        # #443: the slice row's cost/flow fields are measured on the slice (see
        # `oos_slice_flow`). The pre-slice measurement re-runs the same builders
        # on the bars this row excludes; only their FLOW fields are read, so the
        # throwaway call is given the flow-identical inputs those fields are
        # built from (positions, closes, timeline) and nothing else. `pre == 0`
        # means the slice is the whole window: no pre-slice flow to subtract.
        pre = max(0, fit)
        prefix_flow: dict[str, tuple[float, float, int]] = {}
        if pre >= 1:
            legs_pre = {inst: series[:pre] for inst, series in closes.items()}
            for leg_name, leg_res in (("portfolio_P", p_fund), ("portfolio_PE", pe_fund)):
                leg_ser = eb.portfolio_series_from_engine(
                    leg_res, legs_pre, end_ns[:pre], args.capital, args.taker_fee,
                    None, (), None, True,
                )
                prefix_flow[leg_name] = (
                    float(leg_ser["turnover"]),
                    float(leg_ser["commission"]),
                    int(leg_ser["n_trades"]),
                )
            pre_fams = eb.compute_multileg_family(legs_pre, args.capital, args.taker_fee)
            for bench_id, fam in pre_fams.items():
                prefix_flow[bench_id] = (
                    float(fam["turnover"]),
                    float(fam["commission"]),
                    int(fam["n_trades"]),
                )
            if pre >= 2:
                # D-165: the two basket rows are curves too, so their slice row
                # must be `full - pre` on the same builder, never `full - 0`.
                pre_basket_curves = eb.family_basket_mirror_curves(
                    {sym: bars[:pre] for sym, bars in instrument_views.items()},
                    capital=args.capital,
                    taker_fee=args.taker_fee,
                )
                for basket_id, curve in pre_basket_curves.items():
                    prefix_flow[basket_id] = (
                        float(curve["turnover"]),
                        float(curve["commission"]),
                        int(curve["n_trades"]),
                    )
        for name, c in curves.items():
            eq = list(c["equity"])[fit:]
            ex = list(c["exposure"])[fit:]
            base = eq[0] if eq[0] > 0 else args.capital
            eq_n = [v / base * args.capital for v in eq]
            pe = primary_eq[fit:]
            pb = pe[0] if pe[0] > 0 else args.capital
            pe_n = [v / pb * args.capital for v in pe]
            p_turn, p_comm, p_trades = prefix_flow.get(name, (0.0, 0.0, 0))
            s_turn, s_comm, s_trades = oos_slice_flow(
                full_turnover=float(c["turnover"]),
                full_commission=float(c["commission"]),
                full_n_trades=int(c["n_trades"]),
                prefix_turnover=p_turn,
                prefix_commission=p_comm,
                prefix_n_trades=p_trades,
                capital=args.capital,
                taker_fee=args.taker_fee,
                source=(
                    FLOW_SOURCE_ENGINE_FILLS
                    if name in ("portfolio_P", "portfolio_PE")
                    else FLOW_SOURCE_ANALYTIC_PER_BAR
                ),
            )
            oos_metrics[name] = eb.metrics_for_curve(
                eq_n, ex, s_turn, s_comm, None,
                str(c["cost_basis"]) + "+OOS_SLICE", pe_n, s_trades,
                # #472: the slice's leg weights are offset exactly like the slice's
                # exposure (`fit`, the same offset `ex` used above).
                leg_weights=eb.slice_leg_weights(c.get("leg_weights"), fit),
            )

    fam_eq: dict[str, Sequence[float]] = {
        "portfolio_P": [float(v) for v in p_ser["equity"]],
        "portfolio_PE": [float(v) for v in pe_ser["equity"]],
        "simple_trend": [float(v) for v in fams["simple_trend"]["equity"]],
        "vol_target": [float(v) for v in fams["vol_target"]["equity"]],
        args.primary: [float(v) for v in primary_eq],
    }
    # NX07.R4: the statistical plan is pinned and written before it is used.
    stats_plan = g5_plan(
        family="portfolio-family",
        pinned_ns=min(int(ns) for ns in end_ns),
        block_size=CANONICAL_G5_BLOCK_SIZE,
        reps=eb.BOOTSTRAP_REPS,
        seed=args.seed,
    )
    (out_dir / "statistics_plan.json").write_text(
        json.dumps(stats_plan.as_dict(), indent=2, sort_keys=True) + "\n"
    )
    stats = eb.run_statistics(fam_eq, end_ns, args.primary, plan=stats_plan)

    # Engine-level P+E contribution: incremental net of the two shared-account runs.
    mix = {
        "method": "ENGINE_SHARED_ACCOUNT_RERUN",
        "scope": "ENGINE_LEVEL_SAME_BUDGET",
        "p_net": metrics["portfolio_P"].net_return,
        "pe_net": metrics["portfolio_PE"].net_return,
        "incremental_net": metrics["portfolio_PE"].net_return - metrics["portfolio_P"].net_return,
        "sleeves": ["incumbent 1.0", "incumbent 0.5 + challenger 0.5"],
    }

    p_rets = eb.per_bar_returns(list(p_ser["equity"]))
    # Synthetic positive/shuffled controls are mechanics-only test fixtures.
    # They must never enter a real evaluation receipt or be used as evidence.
    controls: dict[str, Any] = {
        "positive_control": {
            "status": "UNRUN_SYNTHETIC_CONTROL_TEST_ONLY",
            "reason": "synthetic controls are restricted to test harnesses",
        },
        "negative_control": {
            "status": "UNRUN_SYNTHETIC_CONTROL_TEST_ONLY",
            "reason": "shuffled inputs are restricted to test harnesses",
        },
        # #493: the per-instrument control is a verdict on each instrument's
        # *displaced* copy, so it is rolled up by the module that knows how to
        # name it (input/rule/extent/digest + the real-series measurement beside
        # it) instead of publishing the copy's violation counts as if they were
        # the canonical tape's. `status` semantics are unchanged.
        "known_defect_future_leak_caught": eb.aggregate_future_leak_control(
            {i: v["known_defect_future_leak"] for i, v in series_validity.items()}
        ),
        "ablation": {
            "P": "incumbent quorum=1,tolerance=28 x4 legs",
            "P+E": "incumbent 0.5 + challenger(tol=0) 0.5 x4 legs, same per-leg budget",
            "same_engine_path": True,
        },
    }
    controls["positive_caught"] = None
    controls["negative_caught"] = None

    def account_balance(run: dict[str, Any]) -> float:
        """The engine account's realized balance, in USDT."""
        return float(str(run["account"]["balance_total"]).split()[0])

    # The two engine portfolio rows fund themselves on their own shared-account
    # dual run rather than on a benchmark basket, so their audit record carries
    # that run's counters and both balances instead of a basket id (D-165). The
    # measurement basis is the engine's own settled funding only when the dual
    # runs were reconciled; otherwise the curve's cost basis names what failed.
    engine_funding_records = {
        "portfolio_P": {
            "funding_engine_basis": (
                BASIS_ENGINE_SETTLED
                if p_ser.get("funding_reconciled")
                else str(p_ser["cost_basis"])
            ),
            "funding_settlements_fed": int(p_fund.get("funding_settlements_fed") or 0),
            "funding_rows_available": len(tape.funding),
            "funding_out_of_window": int(p_fund.get("funding_out_of_window") or 0),
            "funding_unknown_leg": int(p_fund.get("funding_unknown_leg") or 0),
            "engine_terminal_balance_usdt": account_balance(p_fund),
            "engine_unfunded_balance_usdt": account_balance(p_nofund),
        },
        "portfolio_PE": {
            "funding_engine_basis": (
                BASIS_ENGINE_SETTLED
                if pe_ser.get("funding_reconciled")
                else str(pe_ser["cost_basis"])
            ),
            "funding_settlements_fed": int(pe_fund.get("funding_settlements_fed") or 0),
            "funding_rows_available": len(tape.funding),
            "funding_out_of_window": int(pe_fund.get("funding_out_of_window") or 0),
            "funding_unknown_leg": int(pe_fund.get("funding_unknown_leg") or 0),
            "engine_terminal_balance_usdt": account_balance(pe_fund),
            "engine_unfunded_balance_usdt": account_balance(pe_nf),
        },
    }
    # Funding-feed audit: which engine basket each benchmark leg's number was
    # measured on, the engine's own row counters and both run balances. The cell
    # in the `funding $` column is this record, not an assertion. `legs` is the
    # measurement keyed by the family leg the feed ran; `published_curves` is the
    # same measurement keyed by the curve each row of the table publishes, with
    # the published curve that carries the number (D-165).
    controls["benchmark_funding_feed"] = {
        "method": "ENGINE_DUAL_RUN_BALANCE_DIFFERENCE",
        "tape_funding_rows": len(tape.funding),
        # The engine's account balances carry realized PnL only: the unrealized
        # mark of a still-open position is not in them. They are published so the
        # reader can recompute `funded - unfunded` (the funding the engine
        # settled), never as the curve's terminal equity.
        "balance_basis": "REALIZED_ACCOUNT_BALANCE_EXCLUDES_OPEN_UNREALIZED_MARK",
        "legs": family_funding,
        "published_curves": eb.published_funding_records(
            metrics,
            family_funding,
            basket_curves=basket_curves,
            engine_measurements=engine_funding_records,
        ),
    }

    # Capital eligibility is a verifiable input, never inferred from benchmark
    # results.  Without an explicit policy artifact the decision is denied.
    if args.capital_policy:
        try:
            capital_policy = CapitalPolicy.from_file(args.capital_policy)
        except (OSError, ValueError) as exc:
            print(f"error: invalid capital policy: {exc}", file=sys.stderr)
            return 2
    else:
        capital_policy = CapitalPolicy.unauthorized()
    peak_notional = max(
        (abs(float(p.get("quantity") or 0)) * float(p.get("avg_px_open") or 0)
         for p in p_fund["opened_positions"] if isinstance(p, dict)),
        default=0.0,
    )
    cap_decision = capital_policy.decision(
        peak_notional, instrument_id=f"{tape.instruments[0]}-PERP.BINANCE")
    cap_live = capital_policy.decision(peak_notional, live=True)
    cap_missing = CapitalPolicy.unauthorized().decision(peak_notional)
    cap_wrong_inst = capital_policy.decision(peak_notional, instrument_id="NOPE-PERP.BINANCE")
    # Accept-path evidence (non-governing): a controlled TEST policy proves the
    # accept branch end-to-end. It never authorizes the run (see verdict).
    test_policy = CapitalPolicy.test_policy(
        max_notional="5000", max_exposure_frac="1.0", authorized=True
    ).model_copy(update={
        "allowed_instruments": tuple(f"{k}-PERP.BINANCE" for k in tape.instruments),
    })
    cap_test_accept = test_policy.decision(
        peak_notional, instrument_id=f"{tape.instruments[0]}-PERP.BINANCE")
    capital_path = {
        "test_decision": cap_decision,
        "test_policy_accept_path": cap_test_accept,
        "live_decision": cap_live,
        "missing_policy_decision": cap_missing,
        "wrong_instrument_decision": cap_wrong_inst,
        "policy_path": str(args.capital_policy) if args.capital_policy else None,
        "production": "DENIED_NO_APPROVAL_ARTIFACT" if not args.capital_policy else "CONFIGURED_POLICY_VERIFIED",
    }

    capacity = capacity_from_participation(
        p_ser.get("participation", []), args.capital, p_rets)
    live_path = Path(args.live_fills) if args.live_fills else None
    shadow = build_shadow_section(args.live_fills, p_fund["account"])
    shadow["live_fills_arg"] = str(live_path) if live_path else None

    gi = eb.git_info()
    run = eb.RunIdentity(
        dataset=eb.DatasetIdentity(
            tape_path=str(tape_path), tape_sha256=tape.tape_sha256,
            universe=tuple(f"{k}-PERP.BINANCE" for k in tape.instruments),
            period_start_ns=end_ns[0], period_end_ns=end_ns[-1], n_bars=n,
            source_hashes=tuple(c.source_hash for c in tape.candles[tape.instruments[0]][:3])
            + tuple(c.source_hash for c in tape.candles[tape.instruments[0]][-3:]),
        ),
        code=eb.CodeIdentity(
            git_rev=gi["rev"], git_dirty=gi["dirty"],
            config_sha256=hashlib.sha256(json.dumps(
                {"sleeves": ["P", "P+E"], "per_leg_notional": args.per_leg_notional,
                 "bars": args.bars, "start_bar": args.start_bar, "seed": args.seed,
                 "primary": args.primary, "opex_monthly": args.opex_monthly,
                 "capital_policy": args.capital_policy},
                sort_keys=True).encode()).hexdigest(),
            estimator_versions=eb.estimator_versions(),
            source_sha256=eb.source_sha256(),
        ),
        seed=args.seed, primary_benchmark=args.primary,
        diagnostic_benchmarks=tuple(b for b in fams if b != args.primary),
        strategy_family=("portfolio_P", "portfolio_PE", "simple_trend", "vol_target"),
        capital=args.capital, taker_fee=args.taker_fee, opex_monthly_usd=args.opex_monthly,
    )
    excess = metrics["portfolio_P"].excess_vs_primary
    verdicts = eb.build_verdicts(
        chrono_ok=chrono_ok, chrono_note=chrono_note,
        leak_probe=(leak_ok, leak_note), excess=excess, stats=stats,
        excess_ci=(metrics["portfolio_P"].excess_ci_low, metrics["portfolio_P"].excess_ci_high),
        mix={"incremental_net": mix["incremental_net"]},
        cost_basis_ok=p_ser["cost_basis"] == "VERIFIED_ENGINE_FUNDING",
        funding_missing=False, live_fills_present=bool(shadow.get("live_fills_present")),
        parity_ok=det_ok,
    )
    # Measured execution evidence from the funded engine run: the fill/latency/
    # fee semantics actually in force plus the frictions they produced. It is
    # bound into the receipt so a reader can see which execution model the
    # numbers came from, and it feeds the ExecutionFidelity domain below.
    execution_evidence = p_fund.get("execution") or {}
    receipt = eb.EconomicReceipt(
        receipt_id=run.digest()[:32], run=run, metrics=metrics, oos_metrics=oos_metrics,
        verdicts=verdicts, statistics=stats, controls=controls, portfolio_mix=mix,
        capacity_scenarios=capacity, parity={"engine_rerun_parity": "EXACT_MATCH" if det_ok else "DIVERGED"},
        shadow_live=shadow,
        execution=execution_evidence,
        limitations=[
            eb.SOURCE_IDENTITY_NOTE,
            "Mark proxy: funding settlement marks use leg closes at the boundary.",
            "P+E is engine-level shared-account execution, not post-hoc summation.",
            "Capacity beyond participation is UNVERIFIED (no impact model).",
            "Production capital stays denied without a human-signed approval artifact.",
            "Benchmark-leg funding is measured by the engine, not modelled: each leg "
            "the engine executes is run twice (the tape's real funding rows / none) "
            "over this run's legs, and the published cell is the balance difference "
            "between the two. A `funding $` number is published only on the curve "
            "the engine measured it for. Where the measuring basket sizes its legs "
            "under a different convention from the analytic curve beside it -- the "
            "two quad legs: `equal_weight_quad` holds a fixed `capital/k` notional "
            "rebalanced every 24 bars against the index's compounding per-bar "
            "equal-value series, and `vol_target_quad` an inverse-vol fixed "
            "notional at full capital with a per-leg cap against the vol-scaled "
            "analytic curve -- the basket's own curve is published in the same "
            "table under its basket id beside the index, carrying the engine's "
            "measured number and `engine_vs_rule_terminal_delta_usdt` (the engine's "
            "funded balance minus that curve's own terminal equity), and the index "
            "row publishes the named state "
            "`n/a (BASKET_CONVENTION_DIFFERS, <basket_id>)` instead of another "
            "curve's number. Every published curve's funding is recorded, with its "
            "measuring basket, that basket's kind and rule, the engine's row "
            "counters, both run balances and the published curve that carries the "
            "number, in `controls.benchmark_funding_feed.published_curves`; the "
            "basket rows are additional diagnostics of the same window and enter no "
            "statistics family and no verdict. Legs the engine has no basket for "
            "publish NO_ENGINE_RULE (simple_trend) and the never-invested reference "
            "publishes zero by construction (cash). Which convention the "
            "pre-declared primary benchmark is itself computed under stays open as "
            "O-034: no published net, excess, statistic or verdict moves here.",
        ],
    )
    # Version bound artifacts per run config: reruns with different inputs must
    # never overwrite files an older ledger entry is bound to (chain caught
    # exactly this failure). Identical inputs reproduce identical names+bytes.
    tag = receipt.receipt_id[:8]
    receipt_path = out_dir / f"economic_receipt_{tag}.json"
    receipt_path.write_text(receipt.model_dump_json(indent=2), encoding="utf-8")
    report_path = out_dir / f"economic_report_{tag}.md"
    report_path.write_text(eb.render_report(receipt), encoding="utf-8")
    trades_path = out_dir / f"portfolio_P_trades_{tag}.jsonl"
    with open(trades_path, "w", encoding="utf-8") as f:
        for p in p_fund["opened_positions"]:
            f.write(json.dumps({
                "trade_id": p.get("position_id"), "instrument_id": p.get("instrument_id"),
                "side": p.get("side"), "quantity": p.get("quantity"),
                "avg_px_open": p.get("avg_px_open"),
                "fill_time_ns": p.get("event_ns"),
            }) + "\n")
    dataset_path = out_dir / f"canonical_dataset_{tag}.json"
    dataset_path.write_text(json.dumps(receipt.run.dataset.model_dump(), indent=2), encoding="utf-8")
    closed_path = out_dir / f"portfolio_closed_{tag}.jsonl"
    with open(closed_path, "w", encoding="utf-8") as f:
        for c in p_fund["closed_positions"]:
            if not isinstance(c, dict):
                continue
            f.write(json.dumps({
                "position_id": c.get("position_id"), "instrument_id": c.get("instrument_id"),
                "realized_pnl": c.get("realized_pnl"), "avg_px_close": c.get("avg_px_close"),
                "fill_time_ns": c.get("event_ns"),
            }) + "\n")
    binding_list = [
        ArtifactBinding.from_file("engine_trades", trades_path),
        ArtifactBinding.from_file("engine_closed_trades", closed_path),
        ArtifactBinding.from_file("canonical_dataset", dataset_path),
        ArtifactBinding.from_file("economic_receipt", receipt_path),
        ArtifactBinding.from_file("market_tape", tape_path / "tape.jsonl" if tape_path.is_dir() else tape_path),
    ]
    if args.capital_policy:
        binding_list.append(ArtifactBinding.from_file("capital_policy", args.capital_policy))
    bindings = tuple(binding_list)

    # Bind into the D-153 receipt/ledger/certificate chain.
    pnl_series = [
        float(str(c.get("realized_pnl", "0").split()[0]))
        for c in p_fund["closed_positions"] if isinstance(c, dict) and c.get("realized_pnl")
    ]
    # execution_evidence (bound above) feeds the ExecutionFidelity domain in
    # place of the PnL Sharpe proxy.
    # #471: the gate vector is built from measurement, not from the rule's defaults.
    # The ledger is read here -- the receipt does not exist yet, and the ledger this run
    # appends to (below, once `bench_receipt` is built) is what G2's parity is measured
    # on -- and the bar series handed over is the one this run's window identity is
    # declared on. With no per-bar PIT audit on this path, G1 is UNKNOWN by name and G2
    # is UNKNOWN/BLOCKED, never the PASS the missing arguments used to mint.
    ledger = BenchmarkLedger.load_jsonl(out_dir / "benchmark_ledger.jsonl")
    gates, g3_basis = resolve_portfolio_gates(
        tape.candles[tape.instruments[0]],
        ledger,
        total_trades=len(p_fund["opened_positions"]),
        pnl_series=pnl_series or [0.0],
        # #495 R3: the G3 state and the partition it was measured over travel
        # together, so a PASS in the receipt is reproducible from the receipt.
        include_g3_basis=True,
    )
    # #444: the window's evidence class is read off the window spec and travels
    # into the receipt. A bar-count smoke window is liveness/mechanics evidence,
    # so it mints no capability score at all: the run still lands in the ledger
    # (it is traceable), but a score from a window that proves no economic
    # evidence is not a weaker benchmark result — it is not a result.
    window_evidence = WindowEvidence.from_window(window)
    capability: float | None
    _breakdown: dict[str, Any] | None
    if window_evidence.admits_capability_score():
        capability = compute_capability_score(
            pnl_series=pnl_series or [0.0], total_bars=n,
            total_trades=len(p_fund["opened_positions"]), abstain_rate=0.0,
            execution=execution_evidence)
        _breakdown = compute_capability_breakdown(
            pnl_series=pnl_series or [0.0], total_bars=n,
            total_trades=len(p_fund["opened_positions"]), abstain_rate=0.0,
            execution=execution_evidence)
    else:
        capability = None
        _breakdown = None
        print(
            "[!] smoke window: no capability score minted — a smoke window proves no "
            "economic evidence (liveness/mechanics only, NO_ECONOMIC_CLAIM)."
        )
    bench_receipt = BenchmarkReceipt.create(
        case_id=args.case_id, policy_id=args.policy_id, capability_score=capability,
        gates=gates, computed_at_timestamp_ns=end_ns[-1],
        # #495 R3: the measured G3 regime partition travels beside the digest.
        g3_scenario_robustness_basis=g3_basis,
        # #446: `end_ns[-1]` is the end of the window this run measured; when the run
        # itself happened is a different quantity, read here and published beside the
        # digest (outside every canon, so a rerun over the same tape still hashes to the
        # same receipt_digest).
        window_end_timestamp_ns=end_ns[-1],
        run_time_timestamp_ns=time.time_ns(),
        artifact_bindings=bindings,
        economic_evidence_digest=receipt.digest(), economic_receipt_path=str(receipt_path.resolve()),
        window_evidence=window_evidence,
        # #408: the determinants behind the published number travel with it, so a
        # consumer can recompute the score from the receipt alone instead of taking
        # the headline on trust.
        score_evidence=None if _breakdown is None else ScoreEvidence.from_breakdown(_breakdown),
        input_binding=hashlib.sha256(
            json.dumps(
                {
                    "tape_sha256": tape.tape_sha256,
                    "case_id": args.case_id,
                    "policy_id": args.policy_id,
                    "bars": n,
                    "start_bar": args.start_bar,
                    "span_ns": [end_ns[0], end_ns[-1]],
                    "seed": args.seed,
                    "capital": args.capital,
                    "taker_fee": args.taker_fee,
                    "per_leg_notional": args.per_leg_notional,
                    "opex_monthly": args.opex_monthly,
                    "capital_policy": capital_policy.to_receipt_fields(),
                    # Execution semantics are part of the run identity: a
                    # different fill/latency/fee model is a different run.
                    "execution_profile": execution_evidence.get("profile"),
                    "execution_profile_digest": execution_evidence.get("digest"),
                },
                sort_keys=True,
                default=str,
            ).encode()
        ).hexdigest(),
    )
    # #471: the ledger was read above, when G2's parity was measured; this run appends
    # to that same ledger object, so the receipt's gate vector and the chain it joins
    # cannot describe two different ledgers.
    entry = ledger.append(bench_receipt)
    ledger.save_jsonl(out_dir / "benchmark_ledger.jsonl")
    ok, msg = bench_receipt.verify()
    cok, cmsg = ledger.verify_chain()
    # #476: hand the certificate the produced projection and Minerva refusal.
    # Campaign returns are the same dimensionless unit the accounting contract
    # publishes; the projection stays diagnostic (NO_ECONOMIC_CLAIM).
    campaign = eb.campaign_accounting(
        p_fund["opened_positions"],
        p_fund["closed_positions"],
        bars=shared_bars[tape.instruments[0]],
        cutoff_ns=end_ns[-1],
        capital=args.capital,
    )
    projection = projection_for_certificate(
        policy_id=bench_receipt.policy_id,
        benchmark_receipt_id=bench_receipt.receipt_digest,
        capability_score=capability,
        campaign_returns=campaign.campaign_returns,
        initial_capital_usd=float(args.capital),
        has_synthetic_population_only=False,
    )
    minerva = minerva_for_certificate(
        statistics=stats,
        actual_track_days=(end_ns[-1] - end_ns[0]) / 86_400_000_000_000.0,
        min_trl_days=None,
        min_trl_plan=None,
        observed_intervals=len(campaign.campaign_returns),
        worst_regime_return_bps=None,
        regime_floor_bps=None,
    )
    certificate = PolicyCertificate.generate(
        bench_receipt, projection=projection, minerva=minerva
    )
    print(certificate.render_ascii())
    html_path = Path(args.html_out) if args.html_out else out_dir / f"forensic_report_{tag}.html"
    generate_forensic_html_report(bench_receipt, html_path, certificate=certificate)
    # The canonical publish path names what it published: the artifact's render
    # contract + identity, beside the receipt digest it renders (#442).
    identity = render_identity(bench_receipt, certificate)
    print(
        f"[+] Forensic HTML Report Generated: {html_path} "
        f"render_contract={identity['render_contract']} "
        f"render_identity={identity['render_identity_digest'][:16]}… "
        f"receipt_digest={identity['receipt_digest'][:16]}…"
    )

    print(f"[+] P net {metrics['portfolio_P'].net_return:+.4f} "
          f"P+E net {metrics['portfolio_PE'].net_return:+.4f} "
          f"incremental {mix['incremental_net']:+.4f}")
    print(
        f"[+] execution profile={execution_evidence.get('profile')} "
        f"(digest {str(execution_evidence.get('digest'))[:12]}) "
        f"fills={execution_evidence.get('fills_count')} "
        f"shortfall_bps={execution_evidence.get('slippage_bps_mean')} "
        f"samples={execution_evidence.get('slippage_samples')} "
        f"latency={execution_evidence.get('latency_observability')}"
    )
    v = verdicts
    print(f"[+] validity={v.research_validity} economic={v.economic} statistical={v.statistical}")
    unprovisioned = eb.unprovisioned_estimator_hint(stats)
    if unprovisioned:
        # Operator-visible: a fail-closed UNSUPPORTED caused by an unimportable
        # optional dependency must not read as an estimator result.
        print(
            f"[!] statistical={v.statistical}: {unprovisioned} — this is a missing "
            f"dependency, not a computed statistical result"
        )
    print(f"[+] portfolio={v.portfolio} execution={v.execution} capital={v.capital}")
    print(f"[+] capital_path test={cap_decision['decision']} missing={cap_missing['decision']}")
    print(f"[+] receipt chain: {ok} ({msg}) ledger: {cok} ({cmsg}) entry={entry.entry_hash[:16]}...")
    # Persist capital + capacity evidence alongside the receipt.
    (out_dir / f"capital_path_{tag}.json").write_text(json.dumps(capital_path, indent=2), encoding="utf-8")

    # NX05.R4: the window manifest is the resume contract. A run that did not
    # verify stays RUNNING (incomplete evidence), never COMPLETED.
    artifacts: list[dict[str, Any]] = []
    for role, path in (
        ("economic_receipt", receipt_path),
        ("benchmark_ledger", out_dir / "benchmark_ledger.jsonl"),
        ("forensic_report", html_path),
        ("capital_path", out_dir / f"capital_path_{tag}.json"),
    ):
        candidate = Path(path)
        if candidate.is_file():
            artifacts.append(
                {
                    "role": role,
                    "path": str(candidate),
                    "sha256": hashlib.sha256(candidate.read_bytes()).hexdigest(),
                    "bytes": candidate.stat().st_size,
                }
            )
    write_window_manifest(
        manifest_path,
        WindowRunManifest(
            run_key=run_key.digest,
            window=window.as_dict(),
            state="COMPLETED" if (ok and cok) else "RUNNING",
            artifacts=tuple(artifacts),
            started_ns=existing.started_ns if existing is not None else 0,
            finished_ns=time.time_ns(),
            detail={
                "bars": n,
                "ledger_chain": cmsg,
                "trade_signature": trade_signature(p_fund),
                "receipt_verify": msg,
                "render_contract": identity["render_contract"],
                "render_identity": identity["render_identity_digest"],
                "economic_evidence": window.proves_economic_evidence,
                "evidence_class": window.evidence_class,
            },
        ),
    )
    run_store = ResearchStore(out_dir / "runs" / "runs.sqlite")
    try:
        run_store.record_run(
            run_key=run_key.digest,
            payload=canonical(
                {
                    "run_key": run_key.digest,
                    "window": window.as_dict(),
                    "artifacts": artifacts,
                    "receipt_digest": receipt.digest(),
                }
            ),
            digest=run_key.digest,
            registered_ns=time.time_ns(),
        )
        print(f"[+] Run recorded in ResearchStore: {out_dir / 'runs' / 'runs.sqlite'}")
    finally:
        run_store.close()
    telemetry_path = out_dir / "runs" / f"{run_key.digest.split(':')[1][:16]}.telemetry.json"
    telemetry_path.write_text(
        json.dumps(
            {
                "run_key": run_key.digest,
                "components": dict(sorted(run_key.components.items())),
                "window": window.as_dict(),
                "execution": execution_evidence,
                "economic_evidence": window.proves_economic_evidence,
                "evidence_class": window.evidence_class,
            },
            indent=2,
            sort_keys=True,
            default=str,
        )
        + "\n"
    )
    print(f"[+] Execution telemetry: {telemetry_path}")
    print(f"[+] Window manifest: {manifest_path} (state={'COMPLETED' if (ok and cok) else 'RUNNING'})")
    for artifact in artifacts:
        print(
            f"[+]   artifact {artifact['role']}: {Path(str(artifact['path'])).name} "
            f"sha256:{(artifact['sha256'] or '')[:16]}…"
        )
    print(f"[+] Evidence class: {window.evidence_class} (economic_evidence={window.proves_economic_evidence})")
    if not window.proves_economic_evidence:
        print("[!] Profile smoke: this run is NOT economic evidence (NO_ECONOMIC_CLAIM).")
    return 0 if (ok and cok) else 1


if __name__ == "__main__":
    sys.exit(main())
