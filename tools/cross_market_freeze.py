#!/usr/bin/env python
"""Freeze the experiment configuration: code, dependencies, data and settings.

Writes ``reports/v9-cross-market-validation/CONFIG_FREEZE.json``. Every hash is
computed from the working tree at generation time, so a later reader can prove
which code and which bytes produced every number in the results tables.

Usage
    uv run --locked --extra dev python tools/cross_market_freeze.py
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from v8_next.adapters.adaptive_strategy import SimArmConfig  # noqa: E402
from v8_next.adapters.execution_models import profile_digest  # noqa: E402
from v8_next.evaluation.instrument_metadata import VENUE_FILTERS  # noqa: E402
from v8_next.experts.horizon_profiles import PROFILE_TABLE_VERSION  # noqa: E402

ROOT = REPO / "reports" / "v9-cross-market-validation"
FROZEN_CANDIDATE = "09908af840864a20e5bc4836ea4d21f4ceb93b35"

TRACKED_FILES = [
    "src/v8_next/evaluation/instrument_metadata.py",
    "src/v8_next/evaluation/sim_arm.py",
    "src/v8_next/adapters/adaptive_strategy.py",
    "src/v8_next/adapters/portfolio_backtest.py",
    "src/v8_next/adapters/expert_strategy.py",
    "src/v8_next/economics/decisions.py",
    "src/v8_next/economics/management.py",
    "src/v8_next/experts/horizon_profiles.py",
    "tools/build_tape.py",
    "tools/cross_market_build_tapes.py",
    "tools/cross_market_matrix.py",
    "tools/cross_market_ablations.py",
    "tools/cross_market_analysis.py",
]


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def git(*args: str) -> str:
    try:
        return subprocess.run(
            ["git", *args], cwd=REPO, capture_output=True, text=True, check=True
        ).stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return "UNAVAILABLE"


def differential_vs_frozen() -> dict[str, Any]:
    """Which tracked files differ from the frozen candidate, and how."""
    changed = []
    for name in TRACKED_FILES:
        current = git("diff", "--stat", FROZEN_CANDIDATE, "--", name)
        if current:
            changed.append(
                {
                    "file": name,
                    "diffstat": current.replace("\n", " | "),
                    "sha256_working_tree": sha256_file(REPO / name),
                }
            )
    return {
        "frozen_candidate": FROZEN_CANDIDATE,
        "files_differing_from_frozen_candidate": changed,
        "note": "A file appears here only when the validation branch changed it. "
        "Each change is recorded with its semantic-parity evidence in "
        "EXECUTION_SAFETY.md.",
    }


def config_fingerprint() -> dict[str, Any]:
    config = SimArmConfig(
        enabled=True,
        instrument_id="ETHUSDT-PERP.BINANCE",
        bar_type_str="ETHUSDT-PERP.BINANCE-1-HOUR-LAST-EXTERNAL",
    )
    payload = {name: str(value) for name, value in config.__dict__.items()}
    return {
        "resolved_default_config": payload,
        "sha256": hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
        "profile_table_version": PROFILE_TABLE_VERSION,
        "execution_profile": {
            "name": "baseline",
            "digest": profile_digest("baseline"),
        },
    }


def main() -> int:
    document = {
        "generated_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "frozen_candidate_commit": FROZEN_CANDIDATE,
        "validation_branch": git("rev-parse", "--abbrev-ref", "HEAD"),
        "validation_head": git("rev-parse", "HEAD"),
        "frozen_candidate_is_ancestor": git(
            "merge-base", "--is-ancestor", FROZEN_CANDIDATE, "HEAD"
        )
        == "",
        "engine": {
            "nautilus_trader": importlib.metadata.version("nautilus_trader"),
            "python": sys.version.split()[0],
            "fill_model": "default",
            "bar_execution": "engine default",
        },
        "dependency_lock": {
            "uv.lock_sha256": sha256_file(REPO / "uv.lock"),
            "pyproject_sha256": sha256_file(REPO / "pyproject.toml"),
        },
        "experiment": {
            "universe": [
                "ETHUSDT",
                "SOLUSDT",
                "BNBUSDT",
                "XRPUSDT",
                "DOGEUSDT",
                "ADAUSDT",
                "AVAXUSDT",
                "LINKUSDT",
                "DOTUSDT",
                "LTCUSDT",
            ],
            "window_start_utc": "2025-01-01T00:00:00Z",
            "window_end_exclusive_utc": "2025-07-01T00:00:00Z",
            "calendar_days": 181,
            "initial_balance_usdt_per_symbol_per_arm": 10000,
            "accounts": "one independent account per symbol per arm, not a portfolio",
            "maker_fee": "0.0002",
            "taker_fee": "0.0005",
            "bracket_stop_pct": "0.02",
            "bracket_target_pct": "0.04",
            "target_notional_usdt": "1000",
            "max_concurrent_positions": 1,
            "decision_cadence": "H1 bar close (M15/H4 are context, not cadence)",
            "min_support_quorum": 1,
            "max_contradiction_tolerance": 28,
            "grammar_policy": "range-breakout-48-v1",
        },
        "instrument_filters": {
            symbol: {
                "price_precision": f.price_precision,
                "price_increment": str(f.price_increment),
                "size_precision": f.size_precision,
                "size_increment": str(f.size_increment),
                "min_quantity": str(f.min_quantity),
                "max_quantity": str(f.max_quantity),
                "min_notional": str(f.min_notional),
                "onboard_date_ms": f.onboard_date_ms,
                "maintenance_margin": str(f.maintenance_margin),
                "price_increment_source": f.price_increment_source,
            }
            for symbol, f in sorted(VENUE_FILTERS.items())
        },
        "config_fingerprint": config_fingerprint(),
        "code_hashes": {name: sha256_file(REPO / name) for name in TRACKED_FILES},
        "data": {
            "archive_hashes_sha256": sha256_file(ROOT / "archive_hashes.txt")
            if (ROOT / "archive_hashes.txt").is_file()
            else None,
            "tape_manifests": {
                path.name: sha256_file(path)
                for path in sorted((REPO / "research" / "tape").glob("*-2025H1/tape.manifest.json"))
            },
        },
        "experiment_provenance": differential_vs_frozen(),
    }
    out = ROOT / "CONFIG_FREEZE.json"
    out.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"out": str(out), "files_changed": len(
        document["experiment_provenance"]["files_differing_from_frozen_candidate"]
    )}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
