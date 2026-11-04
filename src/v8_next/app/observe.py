"""Prospective decision observation; not yet a complete paper execution service."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import time
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any

from v8_next.adapters.binance_capture import capture, verify
from v8_next.adapters.captured_market import load_candles
from v8_next.domain.experiment import RulePaperExperiment
from v8_next.domain.market import frame_at
from v8_next.economics.decisions import (
    UtilityInputs,
    observe_squeeze,
    opportunity_at,
    reconcile,
    utility_admission,
)
from v8_next.evaluation.store import ResearchStore, canonical


def source_hash() -> str:
    root = Path(__file__).resolve().parents[1]
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*.py")):
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(path.read_bytes())
    lock = root.parents[1] / "uv.lock"
    if lock.is_file():
        digest.update(b"source-lock:")
        digest.update(lock.read_bytes())
    else:
        digest.update(b"installed-distribution:")
    # Include actual versions in either mode; a lock file alone does not establish
    # that the running interpreter uses that resolution. This is version identity,
    # not authentication of dependency binaries.
    for name in (
        "nautilus-trader",
        "numpy",
        "polars",
        "polars-runtime-32",
        "pydantic",
        "pydantic-core",
        "annotated-types",
        "typing-extensions",
        "typing-inspection",
    ):
        digest.update(f"{name}=={importlib.metadata.version(name)}\n".encode())
    return digest.hexdigest()


def initialize(run: Path, paper_config: dict[str, Any] | None = None) -> dict[str, object]:
    """Freeze before fetching any prospective data; changed code requires a new run."""
    policy: dict[str, object] = {
        "code_and_lock_hash": source_hash(),
        "instruments": [
            symbol + "-PERP.BINANCE" for symbol in (paper_config or {}).get("symbols", ["BTCUSDT"])
        ],
        "observer": "squeeze-observer-v1",
        "execution_grammar_policy": (paper_config or {}).get(
            "grammar_policy", "range-breakout-48-v1"
        ),
        "execution_observer_policy": (paper_config or {}).get("observer_policy", "squeeze"),
        "baseline": "range-breakout-without-compression-v1",
        "mode": "PROSPECTIVE_DECISION_OBSERVATION",
        "max_closed_bar_age_ns": 2 * 3600 * 1_000_000_000,
        "claim_status": "NO_ECONOMIC_CLAIM",
        "paper_config": paper_config,
    }
    run.mkdir(parents=True, exist_ok=True)
    path = run / "policy.json"
    if path.exists():
        existing = json.loads(path.read_text())
        if existing["policy"] != policy:
            raise ValueError("frozen policy/code changed; start a new run")
        return dict(existing)
    frozen_ns = time.time_ns()
    if (paper_config or {}).get("experiment_window") is not None:
        RulePaperExperiment(frozen_ns, *(paper_config or {})["experiment_window"])
    frozen = {
        "policy": policy,
        "frozen_ns": frozen_ns,
        "policy_hash": hashlib.sha256(canonical(policy).encode()).hexdigest(),
    }
    with path.open("x") as stream:
        stream.write(canonical(frozen) + "\n")
    return frozen


def observe_capture(run: Path, manifest: Path, frozen: dict[str, object]) -> dict[str, object]:
    verify(manifest)
    metadata = json.loads(manifest.read_text())
    bars_metadata = next(a for a in metadata["artifacts"] if a["path"] == "bars.json")
    requested = int(bars_metadata["request_time_ns"])
    received = int(bars_metadata["received_time_ns"])
    if requested <= int(str(frozen["frozen_ns"])):
        raise ValueError("capture predates policy freeze")
    candles = load_candles(manifest)
    if not candles:
        raise ValueError("no closed candles")
    # This response version is known to THIS prospective observer at receipt.
    # It is never made available to past decisions or used as historical PIT.
    known = tuple(replace(c, available_ns=received) for c in candles)
    frame = frame_at(candles[0].instrument_id, received, known)
    opportunity = opportunity_at(frame)
    stance = observe_squeeze(frame, opportunity)
    stale = received - candles[-1].end_ns > 2 * 3600 * 1_000_000_000
    reconciliation = reconcile(opportunity, (stance,)) if opportunity else "NO_OPPORTUNITY"
    utility = utility_admission(UtilityInputs(None, None, None, None, None, None, None))
    reason = (
        "STALE_DATA"
        if stale
        else (utility if reconciliation == "SUPPORTED_OBSERVATION" else stance.reason)
    )
    snapshot_hash = hashlib.sha256(manifest.read_bytes()).hexdigest()
    decision_id = hashlib.sha256(
        canonical([frozen["policy_hash"], snapshot_hash]).encode()
    ).hexdigest()
    payload: dict[str, object] = {
        "decision_id": decision_id,
        "decision_ns": received,
        "policy_hash": frozen["policy_hash"],
        "source_manifest_sha256": snapshot_hash,
        "source_manifest": str(manifest.resolve()),
        "clock_policy": "response-version-known-at-receipt",
        "historical_availability": "UNKNOWN",
        "opportunity": asdict(opportunity) if opportunity else None,
        "stance": asdict(stance),
        "reconciliation": reconciliation,
        "baseline": "BREAKOUT" if opportunity else "NO_OPPORTUNITY",
        "action": "NO_TRADE",
        "reason": reason,
        "utility_status": utility,
        "claim_status": "NO_ECONOMIC_CLAIM",
        "execution_status": "NOT_SUBMITTED",
    }
    store = ResearchStore(run / "research.sqlite")
    try:
        # Register the two frozen comparison definitions once. dataset_hash is
        # the first captured manifest; every subsequent input remains linked by
        # its decision's source_manifest_sha256, not counted as a new hypothesis.
        for variant in ("range-breakout-baseline", "squeeze-observer-v1"):
            trial_id = hashlib.sha256(
                canonical([frozen["policy_hash"], variant]).encode()
            ).hexdigest()
            if (
                store.db.execute("SELECT 1 FROM trials WHERE trial_id=?", (trial_id,)).fetchone()
                is None
            ):
                store.register_trial(
                    trial_id,
                    "compression-breakout",
                    str(frozen["policy_hash"]),
                    snapshot_hash,
                    "PROSPECTIVE",
                    int(str(frozen["frozen_ns"])),
                )
        store.record_decision(decision_id, payload)
    finally:
        store.close()
    output = run / f"decision-{decision_id}.json"
    if output.exists():
        if output.read_text() != canonical(payload) + "\n":
            raise ValueError("decision artifact diverges from replay")
    else:
        with output.open("x") as stream:
            stream.write(canonical(payload) + "\n")
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_directory", type=Path)
    parser.add_argument("--replay-capture", type=Path)
    args = parser.parse_args()
    frozen = initialize(args.run_directory)
    manifest = args.replay_capture or capture(args.run_directory / f"capture-{time.time_ns()}")
    result = observe_capture(args.run_directory, manifest, frozen)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
