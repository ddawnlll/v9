"""Preregistered research contract: load, validate, hash.

The contract is the single source of truth shared by every experimental arm.
Nothing here inspects market data or profitability; it only makes the declared
assumptions unambiguous and machine-checkable before an experiment runs.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

REQUIRED_KEYS: tuple[str, ...] = (
    "contract_version",
    "research_run_id",
    "source_revision",
    "dataset_ids",
    "authorized_dev_status",
    "instrument_universe",
    "time_windows",
    "bar_granularity",
    "execution_profile",
    "account_configuration",
    "risk_convention",
    "fee_convention",
    "slippage_convention",
    "funding_convention",
    "intrabar_ordering_convention",
    "baseline_policy",
    "experiment_arms",
    "fixed_parameters",
    "metrics",
    "rejection_criteria",
    "statistical_method",
    "random_seeds",
    "maximum_replays",
    "maximum_parameter_variations",
)

#: Hard ceilings. A worker that exceeds these has left the preregistration.
#: Amendment AM-001 (see RESEARCH_CONTRACT.json / AMENDMENTS.md) raises them by
#: exactly one arm so the single positive candidate and the lifecycle gate can be
#: validated on the native harness instead of only on the bar-path model.
MAX_ARMS_PER_EXPERIMENT = 5
MAX_NON_BASELINE_REPLAYS_PER_EXPERIMENT = 3


class ContractError(ValueError):
    """The contract is missing a declared assumption or exceeds its budget."""


def load_contract(path: Path | str) -> dict[str, Any]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ContractError("contract root must be a JSON object")
    return data


def contract_sha256(contract: Mapping[str, Any]) -> str:
    """Stable digest of the contract content (sorted keys, no whitespace drift)."""
    payload = json.dumps(contract, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def validate_contract(contract: Mapping[str, Any]) -> list[str]:
    """Return the list of violations. Empty list means the contract is valid."""
    problems: list[str] = [f"missing key: {key}" for key in REQUIRED_KEYS if key not in contract]

    if contract.get("authorized_dev_status") not in {"DEV_ONLY", "DEV"}:
        problems.append("authorized_dev_status must be DEV_ONLY")
    if contract.get("bar_granularity") not in {"1h", "HOUR", "1-HOUR"}:
        problems.append("unsupported bar_granularity")
    if contract.get("intrabar_ordering_convention") not in {
        "STOP_FIRST",
        "TARGET_FIRST",
    }:
        problems.append("intrabar_ordering_convention must be declared explicitly")
    if contract.get("funding_convention") in {None, ""}:
        problems.append("funding_convention must be declared (including UNAVAILABLE)")

    arms = contract.get("experiment_arms") or {}
    if not isinstance(arms, dict) or not arms:
        problems.append("experiment_arms must map experiment id -> arms")
    else:
        for experiment_id, experiment in arms.items():
            if not isinstance(experiment, dict):
                problems.append(f"{experiment_id}: arm block must be an object")
                continue
            declared = experiment.get("arms") or []
            if len(declared) > MAX_ARMS_PER_EXPERIMENT:
                problems.append(
                    f"{experiment_id}: {len(declared)} arms exceeds "
                    f"MAX_ARMS_PER_EXPERIMENT={MAX_ARMS_PER_EXPERIMENT}"
                )
            if not declared:
                problems.append(f"{experiment_id}: no arms declared")
            native_finalists = [
                a for a in declared
                if isinstance(a, dict)
                and a.get("kind") == "native"
                and not a.get("is_baseline")
            ]
            if len(native_finalists) > MAX_NON_BASELINE_REPLAYS_PER_EXPERIMENT:
                problems.append(
                    f"{experiment_id}: {len(native_finalists)} non-baseline native "
                    f"arms exceeds {MAX_NON_BASELINE_REPLAYS_PER_EXPERIMENT}"
                )

    if not contract.get("rejection_criteria"):
        problems.append("rejection_criteria must be fixed before experiments run")
    if not contract.get("metrics"):
        problems.append("metrics must be declared before experiments run")
    seeds = contract.get("random_seeds")
    if not isinstance(seeds, dict) or not seeds:
        problems.append("random_seeds must be recorded")

    coefficients = contract.get("fee_convention") or {}
    if isinstance(coefficients, dict):
        if "taker" not in coefficients or "maker" not in coefficients:
            problems.append("fee_convention must declare maker and taker")
    return problems


def worst_status(statuses: list[str]) -> str:
    """Aggregate arm statuses into one experiment status (no silent promotion)."""
    for status in ("BLOCKED", "FAILED", "INCONCLUSIVE", "REJECTED"):
        if status in statuses:
            return status
    return "SUPPORTED" if statuses else "INCONCLUSIVE"
