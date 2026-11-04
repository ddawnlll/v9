"""Benchmark CLI command (D-153 End-to-End Benchmark Runner & Report).

Usage:
    python -m v8_next.app.benchmark [--case-id ID] [--output-dir DIR] [--html-out PATH]
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
from typing import Any

from v8_next.adapters.expert_strategy import ExpertStrategyConfig
from v8_next.app.rerun_guard import (
    REFUSED_EXIT_CODE,
    refusal_message,
    registered_run_payload,
)
from v8_next.evaluation import economic_benchmark as eb
from v8_next.evaluation.benchmark_receipt import WindowEvidence
from v8_next.evaluation.gate_resolution import WindowBinding, load_tape_candles
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
from v8_next.evaluation.runner import BenchmarkCase, BenchmarkRunner
from v8_next.evaluation.store import ResearchStore, canonical

# Canonical population tape: 4-year multi-symbol 1h venue capture.
# Single-symbol BTC-only tape is kept only as a fallback for environments
# where the multi tape was not downloaded.
DEFAULT_BENCHMARK_TAPE = Path("research/tape/multi-1h-4y/tape.jsonl")
FALLBACK_BENCHMARK_TAPE = Path("research/tape/btcusdt-1h-12m/tape.jsonl")


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


def window_artifacts(result: Any, html_out: Path, ledger_dir: Path) -> list[dict[str, Any]]:
    """Artifacts this run bound, with physical hashes (read-back evidence)."""
    rows: list[dict[str, Any]] = []
    for role, path in (
        ("native_trades", Path(result.native_ledger_binding.path)),
        ("benchmark_ledger", ledger_dir / "benchmark_ledger.jsonl"),
        ("forensic_report", html_out),
    ):
        if path.is_file():
            rows.append(
                {
                    "role": role,
                    "path": str(path),
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                    "bytes": path.stat().st_size,
                }
            )
    return rows


def _render_score(value: float | None) -> str:
    """A missing capability score prints as MISSING, never as 0.0 (NX08.R1)."""
    return "MISSING" if value is None else f"{value:.1f}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="V8.5 Benchmark Fabric Runner")
    parser.add_argument("--case-id", default="BC-D153-CANONICAL-01", help="Benchmark Case ID")
    parser.add_argument("--policy-id", default="pol_28_expert_ensemble", help="Target Policy ID")
    parser.add_argument("--output-dir", default="artifacts/benchmarks", help="Output artifact directory")
    parser.add_argument("--html-out", default="artifacts/benchmarks/forensic_report.html", help="HTML report output path")
    parser.add_argument(
        "--resolve-gates",
        action="store_true",
        default=True,
        help="Execute empirical gate resolution battery for G3-G9 (default: True)",
    )
    parser.add_argument(
        "--diagnostic-only",
        dest="resolve_gates",
        action="store_false",
        help="Run single diagnostic cell only without resolving G3-G9",
    )
    parser.add_argument(
        "--tape-path",
        default=None,
        help="Path to real market tape (default: research/tape/multi-1h-4y/tape.jsonl, "
        "fallback: research/tape/btcusdt-1h-12m/tape.jsonl)",
    )
    parser.add_argument(
        "--instrument",
        default="BTCUSDT",
        help="Instrument symbol to extract from the tape (multi-symbol tapes are "
        "filtered before the bar limit applies; single-symbol tapes ignore this)",
    )
    parser.add_argument(
        "--bars",
        type=int,
        default=5000,
        help="Number of real hourly bars to load after instrument filtering "
        "(default: 5000; tiny slices leave G3-G7 with no measurable sample)",
    )
    parser.add_argument(
        "--live-fills",
        default=None,
        help="Optional path to venue-settled fills.jsonl for G8 verification",
    )
    parser.add_argument(
        "--execution-profile",
        default=None,
        help=(
            "Nautilus simulated-execution profile (baseline | realistic | "
            "volume_aware): fill model, fee model, latency model plus the "
            "liquidity/queue knobs. Omit to keep engine defaults; the receipt "
            "then reports no declared execution semantics instead of implying one."
        ),
    )
    parser.add_argument(
        "--start-utc",
        type=parse_utc_ms,
        default=None,
        help="UTC window start (YYYY-MM-DD). Required for --profile fold|benchmark.",
    )
    parser.add_argument(
        "--end-utc",
        type=parse_utc_ms,
        default=None,
        help="UTC window end, exclusive (YYYY-MM-DD). Required for --profile fold|benchmark.",
    )
    parser.add_argument("--fold-id", default=None, help="Walk-forward fold id this window is")
    parser.add_argument(
        "--profile",
        choices=("smoke", "fold", "benchmark"),
        default=None,
        help=(
            "Execution profile. A bar-count window is a SMOKE run (liveness only, never "
            "economic evidence); fold/benchmark runs must be UTC-bounded."
        ),
    )
    parser.add_argument(
        "--allow-rerun",
        action="store_true",
        help="Override the completed-window guard (re-executes a finished run key)",
    )
    parser.add_argument(
        "--determinism-rerun",
        action="store_true",
        help=(
            "Re-execute the engine and compare fill signatures, turning G2 from "
            "UNRUN into measured evidence. Doubles engine runtime."
        ),
    )
    args = parser.parse_args(argv)

    if (args.start_utc is None) != (args.end_utc is None):
        print("error: --start-utc and --end-utc must be given together", file=sys.stderr)
        return 2
    profile = args.profile or ("smoke" if args.start_utc is None else "benchmark")

    print(f"=== Starting D-153 Benchmark Execution: {args.case_id} ===")
    case = BenchmarkCase(
        case_id=args.case_id,
        policy_id=args.policy_id,
        dataset_name=f"{args.instrument}-1H-PERP",
        strategy_config=ExpertStrategyConfig(
            min_support_quorum=1,
            max_contradiction_tolerance=28,
            bracket_stop_pct=Decimal("0.02"),
            bracket_target_pct=Decimal("0.04"),
        ),
    )

    runner = BenchmarkRunner(output_dir=args.output_dir)

    if args.tape_path:
        tape_file = Path(args.tape_path)
    elif DEFAULT_BENCHMARK_TAPE.exists():
        tape_file = DEFAULT_BENCHMARK_TAPE
    else:
        tape_file = FALLBACK_BENCHMARK_TAPE
    if not tape_file.exists():
        print(
            f"error: real tape not found at {tape_file}; synthetic fallback is "
            "banned for benchmark runs (full D-153 rule).",
            file=sys.stderr,
        )
        return 2
    window = WindowSpec(
        tape_path=str(tape_file),
        profile=profile,
        instrument=args.instrument,
        start_ms=args.start_utc,
        end_ms=args.end_utc,
        fold_id=args.fold_id,
        bars=None if args.start_utc is not None else args.bars,
    )
    window.validate()
    print(f"[+] Window: {window.label()}")
    if window.is_smoke:
        print(
            "[!] SMOKE RUN: bar-count window. This is liveness/mechanics evidence only; "
            "it is NOT a release benchmark and NOT economic sufficiency evidence."
        )
    # #444 sibling (D153 path): the class the window is allowed to prove is read off
    # the same WindowSpec built above (one source of truth) and travels into the
    # receipt, so a capability score can never be published apart from it.
    # #490: this record is provisional until the tape is read -- the bar count is a
    # measurement, so it is re-derived from the bound window after the load below.
    window_evidence = WindowEvidence.from_window(window)
    print(
        f"[+] Window evidence class: {window_evidence.evidence_class} "
        f"(profile={window_evidence.profile}, smoke={window_evidence.is_smoke}, "
        f"economic_evidence={window_evidence.economic_evidence})"
    )

    print(f"[+] Loading verified real Binance market tape from {tape_file}...")
    candles = load_tape_candles(
        tape_file,
        limit=window.bars,
        instrument=args.instrument,
        start_ms=window.start_ms,
        end_ms=window.end_ms,
    )
    print(f"[+] Loaded {len(candles)} real hourly {args.instrument} bars.")

    # #490: the window this run *measured* is the one that carries the bar count. The
    # spec was built from the CLI request (which for a UTC window declares no count), so
    # the loaded count is bound here -- after the tape exists, before any receipt field
    # is derived from it -- and the evidence record is re-derived from the bound window.
    window = dataclasses.replace(window, measured_bars=len(candles))
    window_evidence = WindowEvidence.from_window(window)
    print(
        f"[+] Window evidence: {window_evidence.evidence_class} / "
        f"bars={window_evidence.bars} (measured from the loaded tape)"
    )

    # #406/#421: the window this run declares and the window it loaded are bound
    # together before any gate runs. `load_tape_candles` picks the slice by file
    # order (`head(limit)`), so a declared bar count that does not describe the bars
    # actually loaded is named here instead of being inherited silently, and the
    # loaded window travels into the gates as the scored window they may not
    # re-label as out-of-sample.
    loaded_window = (
        WindowBinding.from_candles(candles, origin=window.label(), declared_by="benchmark_cli")
        if candles
        else None
    )
    if window.bars is not None and len(candles) != int(window.bars):
        print(
            f"[!] WINDOW_DECLARATION_MISMATCH: {window.bars} bars declared, "
            f"{len(candles)} loaded from {tape_file}",
            file=sys.stderr,
        )

    project_root = Path(__file__).resolve().parents[3]
    tape_bytes_path = tape_file / "tape.jsonl" if tape_file.is_dir() else tape_file
    dataset_sha = hashlib.sha256(tape_bytes_path.read_bytes()).hexdigest()
    run_key = RunKey.build(
        window=window,
        case_id=case.case_id,
        policy_id=case.policy_id,
        dataset_sha256=dataset_sha,
        strategy_config=json.dumps(
            {k: str(v) for k, v in dataclasses.asdict(case.strategy_config).items()},
            sort_keys=True,
        ),
        capital=str(eb.CAPITAL_DEFAULT),
        taker_fee=str(eb.TAKER_FEE_DEFAULT),
        baseline="cash",
        execution_profile_digest=hashlib.sha256(
            str(args.execution_profile or "UNSPECIFIED").encode()
        ).hexdigest(),
        code_and_lock_hash=eb.code_and_lock_hash(project_root),
    )
    manifest_path = Path(args.output_dir) / "runs" / f"{run_key.digest.split(':')[1][:16]}.json"
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
    # --allow-rerun overrides the completed-window guard, but it cannot override the
    # run-identity contract: the registered payload folds in the sha256 of every
    # artifact, and this run appends to ``benchmark_ledger.jsonl``, so a second
    # registration under the same run key can never match. ResearchStore.record_run
    # refuses that rewrite -- but only after the whole window has executed and
    # written its artifacts, which surfaced as an uncaught ValueError (exit 1) with
    # the ledger already appended and the manifest already COMPLETED. Fail closed
    # here instead: no manifest write, no ledger append, no artifact.
    if registered_run_payload(Path(args.output_dir), run_key.digest) is not None:
        print(
            f"[!] {refusal_message(Path(args.output_dir), run_key.digest)}",
            file=sys.stderr,
        )
        return REFUSED_EXIT_CODE
    if existing is not None and not existing.completed:
        print(
            f"[!] INCOMPLETE prior run for this key (state={existing.state}); "
            "this run continues it, the partial one proves nothing."
        )
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

    result = runner.run(
        case,
        candles,
        resolve_gates=args.resolve_gates,
        tape_path=tape_file if tape_file.exists() else None,
        live_fills_path=Path(args.live_fills) if args.live_fills else None,
        execution_profile=args.execution_profile,
        measure_determinism=args.determinism_rerun,
        run_identity=run_key.components,
        scored_window=loaded_window,
        window_evidence=window_evidence,
    )
    execution = result.gate_metrics.get("execution") if result.gate_metrics else None
    if execution:
        print(
            f"[+] Execution profile: {execution.get('profile')} "
            f"(digest {str(execution.get('digest'))[:12]}) | "
            f"fills={execution.get('fills_count')} "
            f"shortfall_bps={execution.get('slippage_bps_mean')} "
            f"samples={execution.get('slippage_samples')} | "
            f"evidence={execution.get('evidence_class')}"
        )
    g2 = (result.gate_metrics or {}).get("g2", {})
    print(f"[+] G2 determinism: {g2.get('status')} ({g2.get('reason')})")
    print(f"[+] Nautilus Backtest Completed: {result.total_bars} bars, {result.total_trades} trades")
    print(f"[+] Capability Score: {_render_score(result.capability_score)} / 100")
    print(
        f"[+]   score evidence class: {window_evidence.evidence_class} "
        f"(economic_evidence={window_evidence.economic_evidence})"
    )
    if result.domain_scores:
        print("[+] Per-domain breakdown (loop engineering: lowest first):")
        for name, d in sorted(result.domain_scores.items(), key=lambda kv: kv[1]["score"]):
            print(
                f"    {name:<28} {d['score']:>5.1f}  "
                f"band[{d['band_low']:.1f},{d['band_high']:.1f}]  "
                f"w={d['weight']:.2f}  n={d['sample_size']}"
            )
    else:
        if window_evidence.admits_capability_score():
            print("[+] Per-domain breakdown: absent (no trades — nothing to decompose)")
        else:
            print(
                "[+] Per-domain breakdown: absent (no capability score minted — a "
                f"{window_evidence.evidence_class} window proves no economic evidence)"
            )
    if result.gate_metrics:
        print("[+] Gate metric deltas:")
        for gname in sorted(result.gate_metrics):
            m = result.gate_metrics[gname]
            if isinstance(m, dict):
                keys = ", ".join(f"{k}={m[k]}" for k in list(m)[:6])
                print(f"    {gname}: {keys}")
    elif args.resolve_gates:
        print("[+] Gate metric deltas: battery ran but emitted no metrics")
    else:
        print("[+] Gates G3-G9 unresolved (diagnostic cell); re-run without --diagnostic-only")
    print(f"[+] Ledger Entry Hash: {result.ledger_entry_hash[:16]}...{result.ledger_entry_hash[-8:]}")
    print(f"[+] Bound Artifact: {result.native_ledger_binding.path} (SHA: {result.native_ledger_binding.sha256_hex[:12]}...)")

    if result.claim_record is not None:
        print(f"[+] Statutory Claim Record Minted: {result.claim_record.claim_id[:16]}... ({result.claim_record.claim_class})")

    # Render Terminal ASCII Policy Certificate
    print()
    print(result.certificate.render_ascii())

    # Generate forensic HTML report. The publish path declares the render identity
    # beside the digest: the same receipt renders differently under a different
    # contract, so the identity is what says whether the artifact is current (#442).
    report_path = Path(args.html_out)
    is_valid = generate_forensic_html_report(
        result.receipt, report_path, certificate=result.certificate
    )
    identity = render_identity(result.receipt, result.certificate)
    print(f"[+] Forensic HTML Report Generated: {report_path} (Valid: {is_valid})")
    print(
        f"[+]   render_contract={identity['render_contract']} "
        f"render_identity={identity['render_identity_digest'][:16]}… "
        f"receipt_digest={identity['receipt_digest'][:16]}…"
    )

    # Verify ledger chain
    chain_valid, chain_msg = runner.ledger.verify_chain()
    print(f"[+] Benchmark Ledger Cryptographic Chain: {'VERIFIED' if chain_valid else 'BROKEN'} ({chain_msg})")

    artifacts = window_artifacts(result, report_path, Path(args.output_dir))
    write_window_manifest(
        manifest_path,
        WindowRunManifest(
            run_key=run_key.digest,
            window=window.as_dict(),
            state="COMPLETED" if chain_valid else "RUNNING",
            artifacts=tuple(artifacts),
            started_ns=existing.started_ns if existing is not None else 0,
            finished_ns=time.time_ns(),
            detail={
                "bars": len(candles),
                "ledger_chain": chain_msg,
                "trade_signature": (
                    (result.gate_metrics or {}).get("execution", {}).get("fill_signature")
                ),
                "input_binding": result.receipt.input_binding,
                "receipt_digest": result.receipt.receipt_digest,
                "render_contract": identity["render_contract"],
                "render_identity": identity["render_identity_digest"],
                "economic_evidence": window.proves_economic_evidence,
            },
        ),
    )
    # NX05.R2: bind the same identity to telemetry and to a ResearchStore row, so
    # a consumer can look the run up instead of trusting a directory listing.
    telemetry_path = Path(args.output_dir) / "runs" / f"{run_key.digest.split(':')[1][:16]}.telemetry.json"
    telemetry_path.write_text(
        json.dumps(
            {
                "run_key": run_key.digest,
                "components": dict(sorted(run_key.components.items())),
                "window": window.as_dict(),
                "execution": (result.gate_metrics or {}).get("execution"),
                "economic_evidence": window.proves_economic_evidence,
            },
            indent=2,
            sort_keys=True,
            default=str,
        )
        + "\n"
    )
    store = ResearchStore(Path(args.output_dir) / "runs" / "runs.sqlite")
    try:
        store.record_run(
            run_key=run_key.digest,
            payload=canonical(
                {
                    "run_key": run_key.digest,
                    "window": window.as_dict(),
                    "artifacts": artifacts,
                    "receipt_digest": result.receipt.receipt_digest,
                }
            ),
            digest=run_key.digest,
            registered_ns=time.time_ns(),
        )
        print(f"[+] Run recorded in ResearchStore: {Path(args.output_dir) / 'runs' / 'runs.sqlite'}")
    finally:
        store.close()
    print(f"[+] Execution telemetry: {telemetry_path}")
    print(f"[+] Window manifest: {manifest_path} (state={'COMPLETED' if chain_valid else 'RUNNING'})")
    for artifact in artifacts:
        print(
            f"[+]   artifact {artifact['role']}: {Path(str(artifact['path'])).name} "
            f"sha256:{(artifact['sha256'] or '')[:16]}…"
        )
    if not window.proves_economic_evidence:
        print("[!] Profile smoke: this run is NOT economic evidence (NO_ECONOMIC_CLAIM).")

    return 0 if chain_valid else 1


if __name__ == "__main__":
    sys.exit(main())
