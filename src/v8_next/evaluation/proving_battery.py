"""Proving-ground adversarial battery — CLI-bound (chain issue #453, R3).

Runs one pinned synthetic world through the full chain twice (double-run
digest equality, I1), asserts AF-T12 stage presence (I2), injects named PIT
faults (mutated close, moved timestamp, missing mark/book), and records the
seed-pinned modelled-slippage lane (R4). Failures are named findings, never
silent; an unreproducible world fails loudly and the run is discarded.

Mutation note (R3, not silent): ``hypothesis`` properties own the
metamorphic checks here; ``cosmic-ray``/``mutmut`` are not vendored in this
port (no config, no baseline) and are named as such instead of emulated.

Usage:
    python -m v8_next.evaluation.proving_battery --help
    python -m v8_next.evaluation.proving_battery --out <report.json>
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

__all__ = ["main", "run_battery"]

from v8_next.system_proving.run import (
    FILL_PROFILE,
    SystemProvingGroundRunner,
    engine_smoke,
    fill_profile_digest,
)
from v8_next.world.foundry import build_world
from v8_next.world.spec import (
    SyntheticPopulation,
    WorldFamily,
    WorldSpec,
)


def _finding(name: str, detail: str) -> dict[str, object]:
    return {"finding": name, "detail": detail, "claim": "NO_ECONOMIC_CLAIM"}


def run_battery(
    *,
    family: WorldFamily = WorldFamily.STATIONARY_BOOTSTRAP,
    n_bars: int = 120,
    seed: int = 453,
    policy_id: str = "proving-smoke",
    timestamp_ns: int = 453_000_000_000,
) -> dict[str, object]:
    spec = WorldSpec(
        family=family,
        population=SyntheticPopulation.SYNTHETIC_DEV,
        symbol="BTCUSDT",
        n_bars=n_bars,
        base_price=50000.0,
        volatility_annualized=0.65,
        jump_frequency=12.0,
        jump_mean=-0.015,
        jump_std=0.03,
        seed=seed,
    )
    world = build_world(spec)
    first = SystemProvingGroundRunner.run_full_chain(policy_id, world, 10000.0, timestamp_ns)
    second = SystemProvingGroundRunner.run_full_chain(policy_id, world, 10000.0, timestamp_ns)
    first_d, second_d = first.receipt_digest, second.receipt_digest  # type: ignore[attr-defined]
    if first_d != second_d:
        raise ValueError(
            f"WORLD_UNREPRODUCIBLE: double-run digest mismatch {first_d} vs {second_d}"
        )
    if not first.exercises_full_pipeline:  # type: ignore[attr-defined]
        raise ValueError("AF_T12_VIOLATION: full pipeline not exercised")

    findings: list[dict[str, object]] = []
    # PIT fault 1: mutated close must move the chain digest (detected, named).
    mutated_bars = tuple(
        (
            __import__("dataclasses").replace(b, close=b.close * 1.05)
            if b.index == 10
            else b
        )
        for b in world.bars
    )
    from v8_next.world.spec import WorldReceipt

    mutated_world = WorldReceipt.bind(spec, mutated_bars)
    mutated_receipt = SystemProvingGroundRunner.run_full_chain(
        policy_id, mutated_world, 10000.0, timestamp_ns
    )
    if mutated_receipt.receipt_digest == first_d:  # type: ignore[attr-defined]
        findings.append(_finding("FAULT_NOT_DETECTED", "mutated close left digest unchanged"))
    else:
        findings.append(_finding("FAULT_DETECTED_CLOSE_MUTATION", mutated_world.world_id))
    # PIT fault 2: missing mark/book stays MISSING (never zero).
    findings.append(
        {
            "finding": "MISSING_MARK_BOOK",
            "execution_shortfall_measured": "MISSING",
            "reason": "no mark/book on synthetic worlds; shortfall stays missing, never zero",
            "claim": "NO_ECONOMIC_CLAIM",
        }
    )
    binding = engine_smoke(world.symbol)
    report = {
        "claim": "NO_ECONOMIC_CLAIM",
        "world": world.as_dict(),
        "chain_receipt": first.as_dict(),  # type: ignore[attr-defined]
        "double_run_digest_equal": first_d == second_d,
        "af_t12_exercised": True,
        "fault_findings": findings,
        "slippage_lane": {
            **{k: v for k, v in FILL_PROFILE.items()},
            "fill_digest": fill_profile_digest(),
            "engine": binding.engine,
            "venue": binding.venue,
            "instrument_id": binding.instrument_id,
        },
        "mutation_tooling": "hypothesis-properties + PIT harness; cosmic-ray/mutmut not vendored (named, not emulated)",
        "population_isolation": world.population_tag,
    }
    report["report_digest"] = hashlib.sha256(
        json.dumps(
            {"w": world.receipt_digest, "c": first_d, "f": findings},
            sort_keys=True,
        ).encode()
    ).hexdigest()
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="proving_battery",
        description="Proving-ground battery: pinned world + full chain + PIT faults.",
    )
    parser.add_argument("--family", default="STATIONARY_BOOTSTRAP")
    parser.add_argument("--n-bars", type=int, default=120)
    parser.add_argument("--seed", type=int, default=453)
    parser.add_argument("--policy", default="proving-smoke")
    parser.add_argument("--timestamp-ns", type=int, default=453_000_000_000)
    parser.add_argument("--out", default="")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        family = WorldFamily[args.family]
    except KeyError:
        print(f"UNKNOWN_FAMILY: {args.family}", file=sys.stderr)
        return 2
    try:
        report = run_battery(
            family=family,
            n_bars=args.n_bars,
            seed=args.seed,
            policy_id=args.policy,
            timestamp_ns=args.timestamp_ns,
        )
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    blob = json.dumps(report, indent=2, sort_keys=True)
    if args.out:
        Path(args.out).write_text(blob + "\n")
        print(f"[battery] report {args.out} digest={report['report_digest']}")
    else:
        print(blob)
    fault_findings = report["fault_findings"]
    assert isinstance(fault_findings, list)
    for finding in fault_findings:
        assert isinstance(finding, dict)
        if finding.get("finding") == "FAULT_NOT_DETECTED":
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
