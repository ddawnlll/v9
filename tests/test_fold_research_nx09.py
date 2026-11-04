"""NX09 (#430) — the registered four-fold research: freeze first, no final.

Evidence classes:

* **mechanics** — the paired-bootstrap rule refuses to report an interval over too
  few pairs, the drawdown helper is checked against a known curve, and
  ``NO_PROTECTED_FINAL`` is proven to be metadata rather than a gate state.
* **artifact read-back** — the delivered evidence files are read from disk and
  checked: the freeze carries no result, the folds come from the frozen plan, the
  final window is absent, every breakdown field is present, and a flat or negative
  result is delivered as-is.

The full four-fold run is reproducible with the command recorded in the report;
these tests verify the artifacts it produced instead of re-running minutes of real
tape work on every suite invocation.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from v8_next.evaluation.benchmark_receipt import GateState

REPO_ROOT = Path(__file__).resolve().parents[1]
NX09_DIR = REPO_ROOT / "docs" / "evidence" / "v87" / "NX09"
TOOL = REPO_ROOT / "tools" / "nx09_fold_research.py"


def _tool_module():
    spec = importlib.util.spec_from_file_location("nx09_tool", TOOL)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load(name: str) -> dict:
    path = NX09_DIR / name
    if not path.is_file():
        pytest.skip(f"NX09 evidence absent at {path}; run tools/nx09_fold_research.py first")
    return json.loads(path.read_text())


# --------------------------------------------------------------------------- #
# mechanics — MECHANICS ONLY
# --------------------------------------------------------------------------- #


def test_paired_interval_refuses_to_report_over_too_few_pairs() -> None:
    module = _tool_module()
    result = module.paired_bootstrap_ci([0.01, 0.02, 0.03], [0.0, 0.0, 0.0], block_size=5, reps=199, seed=7)
    assert result["status"] == "UNDERPOWERED"
    assert "ci_low" not in result and "ci_high" not in result
    assert result["paired_campaigns"] == 3


def test_paired_interval_is_deterministic_under_the_pinned_seed() -> None:
    module = _tool_module()
    candidate = [0.01, -0.004, 0.007, 0.002, -0.001, 0.005, 0.003, -0.002, 0.006, 0.001] * 3
    baseline = [0.0] * len(candidate)
    first = module.paired_bootstrap_ci(candidate, baseline, block_size=5, reps=199, seed=7)
    second = module.paired_bootstrap_ci(candidate, baseline, block_size=5, reps=199, seed=7)
    assert first == second
    assert first["status"] == "COMPUTED"
    assert first["ci_low"] <= first["mean_paired_difference"] <= first["ci_high"]


def test_drawdown_helper_matches_a_known_curve() -> None:
    module = _tool_module()
    assert module._drawdown([100.0, 110.0, 99.0, 105.0]) == pytest.approx(99.0 / 110.0 - 1.0, abs=1e-8)
    assert module._drawdown([100.0, 101.0, 102.0]) == 0.0


def test_no_protected_final_is_metadata_not_a_gate_state() -> None:
    assert "NO_PROTECTED_FINAL" not in GateState.__members__
    assert set(GateState.__members__) == {
        "PASS",
        "BLOCKED",
        "UNKNOWN",
        "DEFEATED",
        "NOT_APPLICABLE",
        "MISSING",
    }


# --------------------------------------------------------------------------- #
# artifact read-back — the delivered evidence
# --------------------------------------------------------------------------- #


def test_freeze_was_written_before_any_result_and_carries_no_result() -> None:
    freeze = _load("fold_freeze.json")
    assert freeze["frozen_before_any_fold_result"] is True
    assert "folds" in freeze and all("fold_id" in window for window in freeze["folds"])
    serialized = json.dumps(freeze)
    for forbidden in ("net_return", "paired_ci", "excess_vs_baseline", "fold_results"):
        assert forbidden not in serialized, forbidden
    assert freeze["plan_digest"].startswith("sha256:")
    assert freeze["family_registry"] and freeze["ablations"]
    assert freeze["resampling_plan"]["block_size"] > 0 and freeze["resampling_plan"]["reps"] > 0
    assert freeze["source_hashes"]["tape_sha256"]
    assert freeze["source_hashes"]["code_and_lock_hash"]


def test_the_final_window_was_never_opened() -> None:
    freeze = _load("fold_freeze.json")
    results = _load("fold_results.json")
    if freeze["final_eligible"]:
        pytest.skip("this dataset has a protected final; different acceptance path")
    assert freeze["NO_PROTECTED_FINAL"] is True
    assert freeze["final_eligibility_reason"]
    assert results["NO_PROTECTED_FINAL"] is True
    assert results["final_eligibility_reason"] == freeze["final_eligibility_reason"]
    scored = set(results["folds"])
    assert "FINAL" not in scored
    plan_folds = {window["fold_id"] for window in freeze["folds"]}
    assert scored and scored <= plan_folds


def test_every_fold_reports_a_breakdown_not_one_pooled_number() -> None:
    results = _load("fold_results.json")
    measured = 0
    for _fold_id, entry in results["folds"].items():
        assert entry["fold"]["scored_start_ns"] < entry["fold"]["scored_end_ns"]
        for _symbol, row in entry["symbols"].items():
            if row.get("status") != "MEASURED":
                continue
            measured += 1
            assert row["bars"] > 0
            assert set(row["regimes"]) == {
                "Bull Trend",
                "Bear Crash",
                "Chop/Range",
                "High-Vol Spill",
            }
            policies = row["policies"]
            assert "cash" in policies
            for policy_id, policy in policies.items():
                assert "net_return" in policy and "max_drawdown" in policy
                assert "fee_cost" in policy and "exposure_bars" in policy
                if policy_id == results["baseline_policy"]:
                    continue
                assert "excess_vs_baseline" in policy
                ci = policy["paired_ci"]
                assert ci["status"] in ("COMPUTED", "UNDERPOWERED")
                if ci["status"] == "UNDERPOWERED":
                    assert "ci_low" not in ci
    assert measured >= 1


def test_a_flat_or_negative_finding_is_delivered_as_is() -> None:
    results = _load("fold_results.json")
    assert results["economic_claim"] == "NONE"
    assert results["selected_from_fold_results"].startswith("NONE")
    assert "NOT certified" in results["prospective_backlog"] or "not certified" in results[
        "prospective_backlog"
    ].lower()
    assert "DIAGNOSTIC" in results["evidence_class"]
    # no policy is marked selected/promoted/winner anywhere in the payload: the
    # check is structural (a truthy selection flag), not a phrase match on the
    # honest statement that no selection was made
    def _selection_flags(node: object) -> list[str]:
        found: list[str] = []
        if isinstance(node, dict):
            for key, value in node.items():
                if key.lower() in ("selected", "promoted", "winner", "is_winner") and value:
                    found.append(key)
                found.extend(_selection_flags(value))
        elif isinstance(node, list):
            for item in node:
                found.extend(_selection_flags(item))
        return found

    assert _selection_flags(results) == []
    assert "\"passed\": true" not in json.dumps(results).lower()
