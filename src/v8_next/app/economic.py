"""Economic benchmark command: real data -> ledger -> benchmark -> stats -> report.

Single-command reproduction (`--extra research` is required: `scipy`/`arch` are
optional dependencies and the statistical verdict cannot be computed without
them):
    uv run --project v8-next --extra research python -m v8_next.app.economic \
        --tape-path research/tape/btcusdt-1h-12m --bars 500 \
        --output-dir artifacts/economic-benchmark --primary btc_buy_hold

Sends no orders, opens no private clients, allocates no capital. Reads public
tape data and replays it through the local Nautilus engine only.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any, Sequence

from v8_next.adapters.expert_strategy import ExpertStrategyConfig, run_expert_strategy_backtest
from v8_next.evaluation import economic_benchmark as eb
from v8_next.evaluation.gate_resolution import load_tape_candles
from v8_next.evaluation.parity import ArtifactBinding
from v8_next.evaluation.statistics_plan import (
    CANONICAL_G5_BLOCK_SIZE,
    g5_plan,
)


def tape_fingerprint(tape_path: Path) -> str:
    h = hashlib.sha256()
    if tape_path.is_dir():
        for child in sorted(tape_path.iterdir()):
            h.update(child.name.encode())
            h.update(str(child.stat().st_size).encode())
            if child.suffix == ".CHECKSUM" or child.name.endswith("CHECKSUM"):
                h.update(child.read_bytes())
    else:
        h.update(tape_path.read_bytes())
    return h.hexdigest()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="V8 economic benchmark (NO_ECONOMIC_CLAIM).")
    p.add_argument("--tape-path", default="research/tape/btcusdt-1h-12m")
    p.add_argument("--bars", type=int, default=500)
    p.add_argument("--output-dir", default="artifacts/economic-benchmark")
    p.add_argument("--primary", default="btc_buy_hold", choices=list(eb.BENCHMARK_IDS))
    p.add_argument("--challenger-quorum", type=int, default=1)
    p.add_argument("--challenger-tolerance", type=int, default=0)
    p.add_argument("--seed", type=int, default=7)
    p.add_argument("--capital", type=float, default=eb.CAPITAL_DEFAULT)
    p.add_argument("--taker-fee", type=float, default=eb.TAKER_FEE_DEFAULT)
    p.add_argument("--opex-monthly", type=float, default=30.0)
    p.add_argument("--live-fills", default=None)
    # Capital policy (Task 4): no live orders; missing file stays unauthorized safely; no prompt.
    p.add_argument("--capital-policy", default=None, help="Path to capital policy JSON (max_notional, max_exposure_frac, authorized). Absent -> unauthorized safe default.")
    p.add_argument("--max-notional", type=str, default=None, help="Override max_notional (decimal string) for test/CLI policy.")
    p.add_argument("--max-exposure-frac", type=str, default=None, help="Override max_exposure_frac (decimal string, (0,1]) for test/CLI policy.")
    return p.parse_args(argv)


def resolve_capital_policy(args: argparse.Namespace) -> Any:
    """Resolve CapitalPolicy from file and/or CLI overrides. Pure, non-blocking.

    Precedence: file -> CLI overrides -> safe unauthorized default.
    No live orders, no private clients, no interactive approval.

    CLI overrides can only ADJUST sizing fields; they can never authorize.
    Authorization comes exclusively from a policy file with authorized=true.
    There is deliberately no --capital-authorized flag: a CLI switch must not
    be able to mint capital permission.
    """
    from v8_next.domain.capital_policy import CapitalPolicy

    policy = CapitalPolicy.from_file(getattr(args, "capital_policy", None))
    max_n = getattr(args, "max_notional", None)
    max_f = getattr(args, "max_exposure_frac", None)
    # CLI overrides produce a test policy; merging preserves file base unless overridden.
    # authorized is never touched here: file base (default False) always stands.
    if max_n is not None or max_f is not None:
        base = policy.model_dump()
        if max_n is not None:
            base["max_notional"] = max_n
        if max_f is not None:
            base["max_exposure_frac"] = max_f
        base["authorized"] = policy.authorized
        policy = CapitalPolicy(**base)
    return policy


def run_engine(
    candles: list[Any],
    quorum: int,
    tolerance: int,
    capital: float,
    fee: float,
) -> dict[str, Any]:
    cfg = ExpertStrategyConfig(
        min_support_quorum=quorum,
        max_contradiction_tolerance=tolerance,
        bracket_stop_pct=Decimal("0.02"),
        bracket_target_pct=Decimal("0.04"),
    )
    return run_expert_strategy_backtest(
        tuple(candles),
        cfg,
        taker_fee=Decimal(str(fee)),
        initial_balance=Decimal(str(capital)),
    )


#: #443: where a curve's full-window commission is measured, so the OOS slice
#: row can price its own share in the same convention. The engine path publishes
#: `taker_fee` applied to the FILL SET -- and a fill carries no bar index -- so a
#: slice of it is priced on the slice's own traded notional. The analytic path
#: accumulates its cost bar by bar, so its slice share is a measured difference.
FLOW_SOURCE_ENGINE_FILLS = "ENGINE_FILLS"
FLOW_SOURCE_ANALYTIC_PER_BAR = "ANALYTIC_PER_BAR"

#: Absolute residue below which a measured slice delta is the float dust of two
#: accumulations rather than a negative flow.
_SLICE_FLOW_DUST = 1e-9


def _slice_delta(full: float, prefix: float, label: str) -> float:
    """`full` minus the same measurement restricted to the pre-slice bars."""
    delta = float(full) - float(prefix)
    if delta >= 0.0:
        return delta
    if delta < -_SLICE_FLOW_DUST:
        # Operator-visible: the premise (the pre-slice measurement is a subset of
        # the window's) did not hold, so the slice share is not attributable by
        # difference. Publishing a negative flow would be worse than publishing
        # the zero the difference cannot support.
        print(
            f"[!] OOS slice flow: {label} measured negative ({delta!r}); the "
            "pre-slice measurement is not a subset of the full window's",
            file=sys.stderr,
        )
    return 0.0


def oos_slice_flow(
    *,
    full_turnover: float,
    full_commission: float,
    full_n_trades: int,
    prefix_turnover: float,
    prefix_commission: float,
    prefix_n_trades: int,
    capital: float,
    taker_fee: float,
    source: str,
) -> tuple[float, float, int]:
    """Cost/flow fields of the frozen OOS slice, measured ON the slice (#443).

    The frozen series/family builders are causal, so running the same builder on
    the first `OOS_FIT_BARS` bars reproduces exactly the part of the window's flow
    that happened BEFORE the slice, in the one convention the full-window row
    publishes. The slice's own flow is the difference of the two measurements --
    never the window's total handed to a slice-scoped `MetricSet`, which charged
    the slice for fills that happened before it existed.
    """
    turnover = _slice_delta(full_turnover, prefix_turnover, "turnover_notional_over_capital")
    n_trades = max(0, int(full_n_trades) - int(prefix_n_trades))
    if source == FLOW_SOURCE_ENGINE_FILLS:
        # `est_comm_total` is `taker_fee` on the traded notional, and the traded
        # notional is what turnover is built from (#397), so the slice's share is
        # the same fee on the slice's own traded notional.
        commission = turnover * float(capital) * float(taker_fee)
    else:
        commission = _slice_delta(full_commission, prefix_commission, "commission_cost")
    return turnover, commission, n_trades


#: The instrument field of a tape record. Used only to classify a tape's kline
#: universe; no payload is parsed.
_INSTRUMENT_FIELD = re.compile(r'"instrument"\s*:\s*"([^"]+)"')


def is_quad_tape(tape_path: Path, *, max_lines: int = 50_000) -> bool:
    """True when the tape's kline universe is exactly the quad universe.

    Detection is on the KLINE legs, never on the funding channel: the
    single-asset BTC tape carries real funding rows too, so "has funding" would
    route it to the quad path and silently swap a one-asset benchmark family for
    a four-asset one. A tape is quad when every quad instrument has a kline leg
    here and no other instrument does -- the four-year ten-instrument tape is
    *not* quad, and keeps the family it was always scored against.

    The scan is bounded and conservative: a tape too large to classify within the
    bound falls back to the analytic family rather than guessing.
    """
    from v8_next.domain.basket import QUAD_INSTRUMENTS

    p = tape_path / "tape.jsonl" if tape_path.is_dir() else tape_path
    if not p.is_file():
        return False
    quad = set(QUAD_INSTRUMENTS)
    seen: set[str] = set()
    try:
        with open(p, encoding="utf-8") as fh:
            for i, line in enumerate(fh):
                if i >= max_lines:
                    # Unclassifiable within the bound: refuse the quad route.
                    return False
                if '"kline"' not in line:
                    continue
                match = _INSTRUMENT_FIELD.search(line)
                if match is None:
                    continue
                seen.add(match.group(1))
                if not seen <= quad:
                    return False
    except OSError:
        return False
    return seen == quad


def build_receipt(args: argparse.Namespace) -> tuple[eb.EconomicReceipt, dict[str, Any]]:
    tape_path = Path(args.tape_path)
    if not tape_path.exists():
        print(f"error: real tape not found at {tape_path}; synthetic fallback banned.", file=sys.stderr)
        raise SystemExit(2)
    # Single truth: a quad multi-asset tape scores the BTC strategy against the
    # engine-executed basket family (shared account, real funding settled); a
    # single-asset tape keeps the analytic family it was always scored against.
    # Resolved before the candles are read because a multi-instrument tape must
    # be filtered to one leg: interleaved symbols are not a chronological series.
    quad_mode = is_quad_tape(tape_path)
    candles = load_tape_candles(tape_path, limit=args.bars, instrument="BTCUSDT" if quad_mode else None)
    bars = eb.bars_from_candles(candles)
    chrono_ok, chrono_note = eb.validate_chronology(bars)
    leak_ok, leak_note = eb.detect_future_leak(bars)
    if not chrono_ok or not leak_ok:
        raise SystemExit(f"INVALID input data: {chrono_note} / {leak_note}")

    # Task 4 capital policy decision path: no live orders, absent stays unauthorized, test policies gate accept/reject
    _capital_policy = resolve_capital_policy(args)
    _capital_decision = _capital_policy.decision()
    print(f"[+] capital policy: authorized={_capital_policy.authorized} max_notional={_capital_policy.max_notional} max_exposure_frac={_capital_policy.max_exposure_frac} decision={_capital_decision['decision']} ({_capital_decision['reason']})", flush=True)

    gi = eb.git_info()
    cfg_obj = {
        "incumbent": {"quorum": 1, "tolerance": 28},
        "challenger": {"quorum": args.challenger_quorum, "tolerance": args.challenger_tolerance},
        "bracket": ["0.02", "0.04"],
        "bars": args.bars,
        "seed": args.seed,
        "capital_policy": _capital_policy.to_receipt_fields(),
        "capital_decision": _capital_decision,
    }
    run = eb.RunIdentity(
        dataset=eb.DatasetIdentity(
            tape_path=str(tape_path),
            tape_sha256=tape_fingerprint(tape_path),
            universe=("BTCUSDT-PERP.BINANCE",),
            period_start_ns=bars[0].end_ns,
            period_end_ns=bars[-1].end_ns,
            n_bars=len(bars),
            source_hashes=tuple(c.source_hash for c in candles[:3]) + tuple(c.source_hash for c in candles[-3:]),
        ),
        code=eb.CodeIdentity(
            git_rev=gi["rev"],
            git_dirty=gi["dirty"],
            config_sha256=hashlib.sha256(json.dumps(cfg_obj, sort_keys=True).encode()).hexdigest(),
            estimator_versions=eb.estimator_versions(),
            source_sha256=eb.source_sha256(),
        ),
        seed=args.seed,
        primary_benchmark=args.primary,
        diagnostic_benchmarks=tuple(b for b in eb.BENCHMARK_IDS if b != args.primary),
        strategy_family=(
            "incumbent_q1_t28",
            f"challenger_q{args.challenger_quorum}_t{args.challenger_tolerance}",
            "simple_trend",
            "vol_target",
        ),
        capital=args.capital,
        taker_fee=args.taker_fee,
        opex_monthly_usd=args.opex_monthly,
    )

    print("[+] running incumbent engine replay (quorum=1,tol=28) ...", flush=True)
    inc_res = run_engine(candles, 1, 28, args.capital, args.taker_fee)
    print(
        "[+] running challenger engine replay (quorum=%d,tol=%d) ..."
        % (args.challenger_quorum, args.challenger_tolerance),
        flush=True,
    )
    ch_res = run_engine(
        candles, args.challenger_quorum, args.challenger_tolerance, args.capital, args.taker_fee
    )
    print("[+] determinism rerun of incumbent ...", flush=True)
    inc_res2 = run_engine(candles, 1, 28, args.capital, args.taker_fee)

    def eng_digest(r: dict[str, Any]) -> str:
        bal = str(r["account"]["balance_total"])
        n = len(r["opened_positions"])
        realized = sum(
            float(str(c.get("realized_pnl", "0").split()[0]))
            for c in r.get("closed_positions", [])
            if isinstance(c, dict) and c.get("realized_pnl")
        )
        return hashlib.sha256(f"{bal}|{n}|{realized:.8f}".encode()).hexdigest()

    parity_ok = eng_digest(inc_res) == eng_digest(inc_res2)

    inc_ser = eb.strategy_series_from_engine(inc_res, bars, args.capital, args.taker_fee)
    ch_ser = eb.strategy_series_from_engine(ch_res, bars, args.capital, args.taker_fee)
    # Single truth: a quad multi-asset tape gets the engine-executed basket
    # family (shared account, real funding settled), a single-asset tape keeps
    # the analytic family it was always scored against.
    if quad_mode:
        print("[+] quad tape detected: engine-executed basket family ...", flush=True)
        fams = eb.compute_benchmark_family_engine(
            tape_path=str(tape_path),
            limit=args.bars,
            capital=args.capital,
            taker_fee=args.taker_fee,
        )
    else:
        fams = eb.compute_benchmark_family(bars, args.capital, args.taker_fee)

    curves: dict[str, dict[str, Any]] = {
        "incumbent": inc_ser,
        "challenger": ch_ser,
    }
    for bid, fam in fams.items():
        curves[bid] = {
            "equity": fam["equity"],
            "exposure": fam["exposure"],
            "turnover": float(fam["turnover"]),
            "commission": float(fam["commission"]),
            "funding": fam.get("funding"),
            "cost_basis": str(fam.get("cost_basis", "ANALYTIC_MODEL")),
            "n_trades": int(fam["n_trades"]),
            # #472: this curve's own per-leg exposure weights travel with it, so
            # the published concentration is measured on the run that built the
            # curve instead of published as a constant.
            "leg_weights": fam.get("leg_weights"),
        }
    primary_eq: list[float] = list(curves[args.primary]["equity"])

    def raw_count(key: str) -> int | None:
        raw = curves[key].get("raw_equity")
        if not raw:
            return None
        steps = [abs(b - a) for a, b in zip(raw, raw[1:], strict=False)]
        return sum(1 for s in steps if s > 1e-9)

    metrics = {
        name: eb.metrics_for_curve(
            list(c["equity"]),
            list(c["exposure"]),
            float(c["turnover"]),
            float(c["commission"]),
            c["funding"],
            str(c["cost_basis"]),
            primary_eq,
            int(c["n_trades"]),
            raw_count(name),
            # #445: the measured closed-loop residual, its terms and its
            # tolerance travel INTO the receipt (the engine legs carry one; the
            # analytic legs reconciled nothing of their own and publish none).
            cost_reconciliation=eb.published_cost_reconciliation(
                c.get("cost_reconciliation"), cost_basis=str(c["cost_basis"])
            ),
            # #472: measured from this curve's own leg weights, or named absent.
            leg_weights=c.get("leg_weights"),
        )
        for name, c in curves.items()
    }
    # Frozen chronological OOS: last N bars, renormalized to segment start.
    oos_n = len(bars) - eb.OOS_FIT_BARS
    oos_metrics: dict[str, eb.MetricSet] = {}
    if oos_n >= 48:
        # #443: the slice row's cost/flow fields are measured on the slice. The
        # pre-slice measurement is the SAME builder run on the bars this row
        # excludes, so the two measurements differ by exactly the flow that
        # happened inside the slice. `pre == 0` means the slice is the whole
        # window: there is no pre-slice flow to subtract.
        pre = eb.OOS_FIT_BARS
        prefix_flow: dict[str, tuple[float, float, int]] = {}
        if pre >= 1:
            for leg_name, leg_res in (("incumbent", inc_res), ("challenger", ch_res)):
                leg_ser = eb.strategy_series_from_engine(
                    leg_res, bars[:pre], args.capital, args.taker_fee
                )
                prefix_flow[leg_name] = (
                    float(leg_ser["turnover"]),
                    float(leg_ser["commission"]),
                    int(leg_ser["n_trades"]),
                )
            pre_fams = (
                eb.compute_benchmark_family_engine(
                    tape_path=str(tape_path),
                    limit=pre,
                    capital=args.capital,
                    taker_fee=args.taker_fee,
                    verify_engine=False,
                )
                if quad_mode
                else eb.compute_benchmark_family(bars[:pre], args.capital, args.taker_fee)
            )
            for bench_id, fam in pre_fams.items():
                prefix_flow[bench_id] = (
                    float(fam["turnover"]),
                    float(fam["commission"]),
                    int(fam["n_trades"]),
                )
        for name, c in curves.items():
            eq = list(c["equity"])[eb.OOS_FIT_BARS :]
            ex = list(c["exposure"])[eb.OOS_FIT_BARS :]
            raw = list(c.get("raw_equity") or eq)[eb.OOS_FIT_BARS :]
            base = eq[0] if eq[0] > 0 else args.capital
            eq_n = [v / base * args.capital for v in eq]
            pe = primary_eq[eb.OOS_FIT_BARS :]
            pbase = pe[0] if pe[0] > 0 else args.capital
            pe_n = [v / pbase * args.capital for v in pe]
            raw_steps = [abs(b - a) for a, b in zip(raw, raw[1:], strict=False)]
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
                    if name in ("incumbent", "challenger")
                    else FLOW_SOURCE_ANALYTIC_PER_BAR
                ),
            )
            oos_metrics[name] = eb.metrics_for_curve(
                eq_n, ex, s_turn, s_comm, None,
                str(c["cost_basis"]) + "+OOS_SLICE", pe_n, s_trades,
                sum(1 for s in raw_steps if s > 1e-9) if raw_steps else None,
                # #472: the slice's leg weights are offset exactly like the slice's
                # exposure, so the slice row measures the sliced run and never a
                # full-window number.
                leg_weights=eb.slice_leg_weights(
                    c.get("leg_weights"), eb.OOS_FIT_BARS
                ),
            )

    fam_eq: dict[str, Sequence[float]] = {
        "incumbent": [float(v) for v in inc_ser["equity"]],
        "challenger": [float(v) for v in ch_ser["equity"]],
        "simple_trend": [float(v) for v in fams["simple_trend"]["equity"]],
        "vol_target": [float(v) for v in fams["vol_target"]["equity"]],
        args.primary: [float(v) for v in primary_eq],
    }
    # NX07.R4: the statistical plan is pinned and written before it is used.
    stats_plan = g5_plan(
        family="economic-family",
        pinned_ns=min(int(b.end_ns) for b in bars),
        block_size=CANONICAL_G5_BLOCK_SIZE,
        reps=eb.BOOTSTRAP_REPS,
        seed=args.seed,
    )
    # The plan is written beside the receipt this run will produce; the receipt
    # path itself is resolved later in build_receipt, so the plan travels with the
    # run's identity instead of an assumed directory.
    plan_path = Path(args.output_dir) / "statistics_plan.json"
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    plan_path.write_text(json.dumps(stats_plan.as_dict(), indent=2, sort_keys=True) + "\n")
    stats = eb.run_statistics(fam_eq, [b.end_ns for b in bars], args.primary, plan=stats_plan)

    inc_rets = eb.per_bar_returns(list(inc_ser["equity"]))
    ch_rets = eb.per_bar_returns(list(ch_ser["equity"]))
    mix = eb.allocator_mix(inc_rets, ch_rets)
    neg = eb.negative_control_shuffled(inc_rets, seed=args.seed + 100)
    pos = eb.positive_control_known_effect()
    # Known-defect control: the probe is run against a genuinely displaced copy of
    # the *same* series the numbers came from, so this is a measurement that can
    # come out MISSED -- not a constant the caller sets (#437). The block is
    # published verbatim: since #493 it names the displaced series it measured
    # (input/input_rule/bars_in/bars_out/input_sha256) and carries the same probe's
    # measurement of the real series beside it (real_series), so `CAUGHT` on the
    # copy can never be read as a leak on the canonical tape.
    leak_control = eb.future_leak_positive_control(bars)
    controls = {
        "positive_control": pos,
        "positive_caught": bool(pos["detected"]) == bool(pos["expected"]),
        "negative_control": neg,
        "negative_caught": bool(neg["declares_winner"]) == bool(neg["expected"]),
        "known_defect_future_leak_caught": leak_control,
        "known_defect_zero_latency": {
            "status": "CAUGHT_BY_DESIGN",
            "reason": "no latency model attached; execution stays SIM_ONLY",
        },
        "ablation": {
            "incumbent": "quorum=1,tolerance=28",
            "challenger": f"quorum={args.challenger_quorum},tolerance={args.challenger_tolerance}",
            "ablated_component": "contradiction-tolerance gate (28->0: contradiction-intolerant)",
            "same_engine_path": True,
        },
    }

    excess = metrics["incumbent"].excess_vs_primary
    cap_table = eb.capacity_table(excess or 0.0, metrics["incumbent"].turnover_notional_over_capital, args.capital, args.taker_fee)
    live_path = Path(args.live_fills) if args.live_fills else None
    live_present = bool(live_path and live_path.is_file())
    shadow_live = {
        "backtest_shadow_signal": "UNRUN",
        "shadow_vs_real_orders": "UNRUN",
        "model_vs_real_fills": "UNRUN",
        "account_reconciliation": "UNRUN_NO_VENUE_ACCOUNT",
        "live_fills_path": str(live_path) if live_path else None,
        "live_fills_present": live_present,
        "reason": "no venue-settled fills supplied; nothing simulated in their place",
    }
    cost_ok = inc_ser["cost_basis"] == "VERIFIED_ENGINE"
    verdicts = eb.build_verdicts(
        chrono_ok=chrono_ok,
        chrono_note=chrono_note,
        leak_probe=(leak_ok, leak_note),
        excess=excess,
        excess_ci=(metrics["incumbent"].excess_ci_low, metrics["incumbent"].excess_ci_high),
        stats=stats,
        mix=mix,
        cost_basis_ok=cost_ok,
        funding_missing=True,
        live_fills_present=live_present,
        parity_ok=parity_ok,
    )
    months = len(bars) / (30 * 24)
    opex_total = args.opex_monthly * months
    strat_net = metrics["incumbent"].net_return * args.capital
    limitations = [
        eb.SOURCE_IDENTITY_NOTE,
        (
            "Quad 500-bar BTC/ETH/SOL/AVAX engine-executed baskets in one shared "
            "MARGIN/NETTING account; no out-of-window generalization claimed."
            if quad_mode
            else "Single 500-bar BTCUSDT window; no cross-asset generalization claimed."
        ),
        (
            "Funding is measured per curve and never implied: the quad baskets "
            "settle the tape's real funding rows in the engine and publish the "
            "balance difference against the identical unfunded run; the "
            "incumbent/challenger ensemble legs are not fed funding events in "
            "this scope and publish NOT_MEASURED. Cash is zero by construction."
            if quad_mode
            else "Funding settlement not fed to engine on this path: the strategy "
            "and analytic legs publish NOT_MEASURED, which is a named absence, "
            "never a zero and never a bare blank."
        ),
        "OPEX shown separately: business net = %.2f - %.2f (opex) on incumbent leg." % (strat_net, opex_total),
        "P+E is allocator-level sleeve rerun, not joint engine execution.",
        "Data resolution (1h OHLCV bars: open/high/low/close/volume; no L2/tick/spread/depth) "
        "SUPPORTS: linear taker fee (notional * taker_fee, config) and bar-close turnover/slippage proxy only.",
        "Capacity UNMODELED (requires data NOT in 1h bars; no coefficients invented): market impact / price impact vs depth, "
        "participation rate / %ADV / queue position, intraday slippage distribution / bid-ask spread, "
        "liquidity curvature / nonlinearity.",
        "Claim that CANNOT be validated from 1h bars: 'capacity at N*capital (10x/100x) with preserved edge net of impact/participation' — "
        "requires L2 depth + ADV/participation + tick spread data not present; linear 10x/100x rows are accounting extrapolations, not validations. "
        "Even at 1x, impact/participation remain UNVALIDATED.",
        (
            "Vol-target is engine-executed as a quad basket; simple_trend stays an "
            "analytic single-asset model with an assumed taker fee, not engine fills."
            if quad_mode
            else "Vol-target/simple-trend are analytic models with assumed taker fee, not engine fills."
        ),
        (
            "Baskets settle funding while the strategy legs do not, so the two are "
            "not funding-comparable on this run; each row publishes its own basis."
            if quad_mode
            else "Benchmarks exclude funding on the same basis as the strategy (comparable)."
        ),
        "MINERVA-style statistical confidence (if any) carries zero economic/capital authority.",
    ]
    _capital_fields = _capital_policy.to_receipt_fields()
    _extended_limitations = list(limitations)
    if not _capital_policy.authorized:
        _extended_limitations.append(
            f"Capital policy UNAUTHORIZED (max_notional={_capital_fields['max_notional']} max_exposure_frac={_capital_fields['max_exposure_frac']}): no live orders, no capital permission; CLI sizing overrides (--max-notional/--max-exposure-frac) can never authorize, only a policy file with authorized=true can."
        )
    receipt = eb.EconomicReceipt(
        receipt_id=run.digest()[:32],
        run=run,
        metrics=metrics,
        oos_metrics=oos_metrics,
        verdicts=verdicts,
        statistics=stats,
        controls=controls,
        portfolio_mix=mix,
        capacity_scenarios=cap_table,
        parity={"engine_rerun_parity": "EXACT_MATCH" if parity_ok else "DIVERGED", "capital_policy": _capital_fields, "capital_decision": _capital_decision},
        shadow_live=shadow_live,
        limitations=_extended_limitations,
    )
    aux = {
        "capital_policy": _capital_fields,
        "capital_decision": _capital_decision,
        "inc_digest": eng_digest(inc_res),
        "inc_digest_rerun": eng_digest(inc_res2),
        "engine_trades": [
            {
                "trade_id": p.get("position_id"),
                "side": p.get("side"),
                "quantity": p.get("quantity"),
                "fill_time_ns": p.get("event_ns"),
            }
            for p in inc_res.get("opened_positions", [])
        ],
        "challenger_trades": [
            {
                "trade_id": p.get("position_id"),
                "side": p.get("side"),
                "quantity": p.get("quantity"),
                "fill_time_ns": p.get("event_ns"),
            }
            for p in ch_res.get("opened_positions", [])
        ],
    }
    return receipt, aux


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    receipt, aux = build_receipt(args)

    # Version bound artifacts per run config: never overwrite a file an older
    # ledger entry is bound to (the chain fail-closed on exactly this).
    tag = receipt.receipt_id[:8]
    trades_path = out_dir / f"incumbent_trades_{tag}.jsonl"
    with open(trades_path, "w", encoding="utf-8") as f:
        for row in aux["engine_trades"]:
            f.write(json.dumps(row) + "\n")
    challenger_path = out_dir / f"challenger_trades_{tag}.jsonl"
    with open(challenger_path, "w", encoding="utf-8") as f:
        for row in aux["challenger_trades"]:
            f.write(json.dumps(row) + "\n")
    dataset_path = out_dir / f"canonical_dataset_{tag}.json"
    dataset_path.write_text(
        json.dumps(receipt.run.dataset.model_dump(), indent=2), encoding="utf-8"
    )
    bind_trades = ArtifactBinding.from_file("engine_trades", trades_path)
    bind_data = ArtifactBinding.from_file("canonical_dataset", dataset_path)
    ok1, _ = bind_trades.verify()
    ok2, _ = bind_data.verify()
    parity = dict(receipt.parity)
    parity["artifact_bindings"] = [bind_trades.model_dump(), bind_data.model_dump()]
    parity["bindings_verified"] = bool(ok1 and ok2)
    receipt = receipt.model_copy(update={"parity": parity})

    receipt_path = out_dir / f"economic_receipt_{tag}.json"
    receipt_path.write_text(receipt.model_dump_json(indent=2), encoding="utf-8")
    report_path = out_dir / f"economic_report_{tag}.md"
    report_path.write_text(eb.render_report(receipt), encoding="utf-8")
    # Version bound artifacts per run (see portfolio.py): never overwrite a
    # file an older ledger entry is bound to.

    # Determinism: receipt digest must be stable for fixed inputs (rerun check).
    print(f"[+] receipt: {receipt.receipt_id} digest: {receipt.digest()[:16]}...")
    for name, m in receipt.metrics.items():
        ex = f"{m.excess_vs_primary:+.4f}" if m.excess_vs_primary is not None else "n/a"
        print(f"    {name:<12} net {m.net_return:+.4f} excess {ex} Sharpe_ann {m.sharpe_annualized:+.3f}")
    v = receipt.verdicts
    print(f"[+] validity={v.research_validity} economic={v.economic} statistical={v.statistical}")
    unprovisioned = eb.unprovisioned_estimator_hint(receipt.statistics)
    if unprovisioned:
        # Operator-visible: a fail-closed UNSUPPORTED caused by an unimportable
        # optional dependency must not read as an estimator result.
        print(
            f"[!] statistical={v.statistical}: {unprovisioned} — this is a missing "
            f"dependency, not a computed statistical result"
        )
    print(f"[+] portfolio={v.portfolio} execution={v.execution} capital={v.capital}")
    print(f"[+] parity(engine rerun)={parity['engine_rerun_parity']} bindings={parity['bindings_verified']}")
    print(f"[+] wrote {receipt_path} + {report_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
