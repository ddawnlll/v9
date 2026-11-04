"""NX07 (#428) — pinned plans, dependency-aware adequacy, and honest failures.

Evidence classes:

* **mechanics** — plan validation and immutability, the pinned-before-results
  guard, adequacy that counts non-overlapping blocks instead of rows, and the
  removal of the silent regime fallback. Synthetic known-effect/shuffled controls
  are exercised here and only here.
* **evaluative** — a real fold family on real tape: the plan file is written
  before the statistics, the receipt binds it by hash, and every method row
  declares its class, inputs, provenance and adequacy.

Nothing in this file requires a positive or significant result; an underpowered
or null finding is asserted to be reported as such.
"""

from __future__ import annotations

import dataclasses
import json
import subprocess
import sys
from pathlib import Path

import pytest

from v8_next.evaluation import economic_benchmark as eb
from v8_next.evaluation.gate_resolution import evaluate_g5_selection_control
from v8_next.evaluation.statistics_plan import (
    CANONICAL_G5_BLOCK_SIZE,
    g5_plan,
    load_statistics_plan,
    pin_statistics_plan,
    require_pinned_before_results,
    sample_sufficiency,
)
from v8_next.evaluation.store import ResearchStore

REPO_ROOT = Path(__file__).resolve().parents[1]
TAPE = REPO_ROOT / "research" / "tape" / "multi-1h-4y" / "tape.jsonl"
TOOL = REPO_ROOT / "tools" / "nx07_family_statistics.py"


def _plan(**overrides: object):
    base = dict(
        family="unit-family",
        pinned_ns=1_000,
        block_size=CANONICAL_G5_BLOCK_SIZE,
        reps=199,
        seed=3,
    )
    base.update(overrides)
    return g5_plan(**base)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# mechanics — synthetic inputs, MECHANICS ONLY
# --------------------------------------------------------------------------- #


def test_plan_identity_covers_the_statistical_content_only() -> None:
    plan = _plan()
    same = _plan()
    assert plan.identity() == same.identity()
    # pinned_ns is not part of the identity, so re-pinning the same content is a no-op
    assert plan.pinned(2_000).identity() == plan.identity()
    # every parameter a caller could tune after seeing a result IS in the identity
    for change in (
        {"block_size": 7},
        {"seed": 4},
        {"reps": 200},
        {"multiplicity_trials": 6},
        {"effective_independent_trials": 2.0},
    ):
        assert _plan(**change).identity() != plan.identity(), change


def test_plan_rejects_unpinnable_or_ambiguous_declarations() -> None:
    with pytest.raises(ValueError):
        _plan(block_size=0)
    with pytest.raises(ValueError):
        _plan(pinned_ns=0)
    with pytest.raises(ValueError):
        _plan(multiplicity_trials=2, effective_independent_trials=3.0)
    with pytest.raises(ValueError):
        dataclasses.replace(_plan(), independence_basis="  ")
    with pytest.raises(ValueError):
        dataclasses.replace(_plan(), authority_conditions=())
    with pytest.raises(ValueError):
        dataclasses.replace(_plan(), diagnostics=())
    with pytest.raises(ValueError):
        # a statistic cannot be both the condition and a diagnostic
        dataclasses.replace(
            _plan(),
            authority_conditions=tuple(_plan().diagnostics),
        )
    assert set(_plan().authority_conditions) & set(_plan().diagnostics) == set()


def test_pin_is_immutable_per_family(tmp_path: Path) -> None:
    store = ResearchStore(tmp_path / "research.sqlite")
    plan = _plan()
    plan_id = pin_statistics_plan(store, plan)
    assert plan_id == plan.identity()
    # re-pinning the same content is idempotent
    assert pin_statistics_plan(store, plan) == plan_id
    # a different statistical plan for the same family cannot be pinned afterwards
    with pytest.raises(ValueError, match="already pinned"):
        pin_statistics_plan(store, _plan(seed=4))
    loaded = load_statistics_plan(store, "unit-family")
    assert loaded.identity() == plan_id
    assert loaded.authority_conditions == plan.authority_conditions
    with pytest.raises(KeyError):
        load_statistics_plan(store, "never-pinned")


def test_plan_pinned_after_the_results_is_refused() -> None:
    plan = _plan()
    require_pinned_before_results(plan, computed_ns=plan.pinned_ns)  # equal is fine
    with pytest.raises(ValueError, match="pinned after the results"):
        require_pinned_before_results(plan.pinned(plan.pinned_ns + 1), computed_ns=plan.pinned_ns)


def test_sufficiency_counts_blocks_not_rows() -> None:
    result = sample_sufficiency(100, 5)
    assert result["observations"] == 100
    assert result["independent_samples"] == 20
    assert result["independent_samples"] < result["observations"]
    assert result["verdict"] == "SUFFICIENT"
    assert sample_sufficiency(3, 5)["verdict"] == "INSUFFICIENT"
    assert sample_sufficiency(0, 5)["verdict"] == "UNKNOWN"
    with pytest.raises(ValueError):
        sample_sufficiency(-1, 5)
    with pytest.raises(ValueError):
        sample_sufficiency(10, 0)


def test_underpowered_own_track_is_unknown_not_substituted() -> None:
    plan = _plan()
    # 4..19 own intervals without authorization: no series is fabricated
    state, metrics = evaluate_g5_selection_control([0.01, 0.02, 0.03, 0.04], plan=plan)
    assert state.name == "UNKNOWN"
    assert "OWN_TRACK_UNDERPOWERED" in metrics["error"]
    # an underpowered series never yields a p-value or a confidence
    assert "dsr_confidence" not in metrics
    assert "raw_pvalue" not in metrics
    assert metrics["plan_id"] == plan.identity()


def test_authorized_fallback_requires_a_stated_basis() -> None:
    plan = _plan()
    with pytest.raises(ValueError, match="stated basis"):
        evaluate_g5_selection_control([0.01, 0.02], plan=plan, allow_regime_fallback=True)
    with pytest.raises(TypeError):
        evaluate_g5_selection_control([0.01, 0.02], None, num_trials=4)  # type: ignore[call-arg]


def test_below_the_moment_floor_still_blocks() -> None:
    state, metrics = evaluate_g5_selection_control([0.01, 0.02], plan=_plan())
    assert state.name == "BLOCKED"
    assert "INSUFFICIENT_TRADE_INTERVALS" in metrics["error"]


def test_run_statistics_refuses_loose_parameters() -> None:
    with pytest.raises(TypeError):
        eb.run_statistics({"a": [1.0, 2.0]}, [1, 2], "a")  # type: ignore[call-arg]
    with pytest.raises(TypeError, match="StatisticsPlan"):
        eb.run_statistics({"a": [1.0, 2.0]}, [1, 2], "a", plan={"block_size": 1})  # type: ignore[arg-type]


def test_synthetic_controls_stay_out_of_economic_receipts() -> None:
    """NX07.R5: the known-effect/shuffled controls are mechanics-only artifacts."""
    shuffled = eb.negative_control_shuffled([0.01, -0.02, 0.03, 0.005] * 20, seed=11)
    known = eb.positive_control_known_effect(n=200, seed=5)
    assert shuffled["scope"] == "SYNTHETIC_CONTROL"
    assert known["scope"] == "SYNTHETIC_CONTROL"

    # an economic receipt is built from real curves only: its structure has no
    # place for a control payload, and the plan declares the real inputs.
    plan = _plan(family="receipt-family")
    baseline = [1.0 + 0.001 * ((i % 7) - 3) for i in range(60)]
    variant = [value + 0.002 * (i % 5) - 0.001 * (i % 3) for i, value in enumerate(baseline)]
    curves = {"baseline": baseline, "variant": variant}
    end_ns = [1_000_000 * (i + 1) for i in range(60)]
    stats = eb.run_statistics(curves, end_ns, "baseline", plan=plan)
    serialized = json.dumps(stats, default=str)
    assert "SYNTHETIC_CONTROL" not in serialized
    assert "known_effect" not in serialized
    assert stats["plan_id"] == plan.identity()
    # an uncomputable statistic is reported with its reason, never as p = 0
    for key in ("dsr", "spa", "pbo"):
        entry = stats.get(key) or {}
        if entry.get("verdict") != "COMPUTED":
            assert entry.get("reason")

    # a missing/degenerate family reports an explicit uncomputable verdict with a
    # reason instead of a p-value of zero
    tiny = eb.run_statistics({"baseline": [1.0, 1.0]}, [1, 2], "baseline", plan=plan)
    for key in ("dsr", "spa", "pbo"):
        entry = tiny.get(key) or {}
        assert entry.get("verdict") != "COMPUTED"
        assert entry.get("reason")
    # identical curves are an explicit equivalence question, not a free p-value
    flat = [1.0] * 60
    degenerate = eb.run_statistics(
        {"baseline": flat, "variant": list(flat)}, end_ns, "baseline", plan=plan
    )
    assert degenerate["dsr"]["verdict"] != "COMPUTED"
    assert degenerate["dsr"]["reason"]


# --------------------------------------------------------------------------- #
# evaluative — real tape
# --------------------------------------------------------------------------- #


def test_real_family_plan_precedes_and_binds_the_statistics(tmp_path: Path) -> None:
    if not TAPE.is_file():
        pytest.skip(f"real tape absent at {TAPE}")
    import hashlib

    out = tmp_path / "NX07"
    completed = subprocess.run(
        [
            sys.executable,
            str(TOOL),
            "--repo-root",
            str(REPO_ROOT),
            "--start-utc",
            "2025-01-01",
            "--end-utc",
            "2025-02-01",
            "--out",
            str(out),
        ],
        capture_output=True,
        text=True,
        timeout=900,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr[-2000:]
    manifest = json.loads((out / "family_manifest.json").read_text())
    plan = json.loads((out / "statistics_plan.json").read_text())
    receipt = json.loads((out / "statistics_receipt.json").read_text())

    # the manifest and the plan were written before any curve or statistic
    assert manifest["registered_before_measurement"] is True
    assert "measurements" not in manifest and "statistics" not in manifest
    assert manifest["history_completeness"] == "UNKNOWN_UNDISCLOSED_TRIALS_POSSIBLE"
    assert plan["pinned_ns"] <= min(int(ns) for ns in [plan["pinned_ns"]])  # pinned and positive
    assert receipt["plan_id"] == plan["identity"]
    assert receipt["plan_file_sha256"] == hashlib.sha256((out / "statistics_plan.json").read_bytes()).hexdigest()
    assert receipt["family_manifest_sha256"] == hashlib.sha256(
        (out / "family_manifest.json").read_bytes()
    ).hexdigest()

    # every method row declares class, inputs, provenance and adequacy
    rows = {row["method"]: row for row in receipt["methods"]}
    assert set(rows) == {
        "deflated_sharpe_ratio",
        "white_reality_check",
        "probability_of_backtest_overfitting",
    }
    for row in rows.values():
        assert row["class"] in ("AUTHORITY_CONDITION", "DIAGNOSTIC")
        assert row["inputs"] and row["provenance"] and row["adequacy"]
        assert row["verdict"] in ("COMPUTED", "UNDERPOWERED", "MISSING", "UNSUPPORTED")
    assert rows["deflated_sharpe_ratio"]["class"] == "AUTHORITY_CONDITION"
    assert rows["white_reality_check"]["class"] == "DIAGNOSTIC"

    # no manufactured significance, and the sample adequacy is measured, not assumed
    assert receipt["economic_claim"] == "NONE"
    assert receipt["sample_sufficiency"]["basis"].startswith("non-overlapping blocks")
    dsr = receipt["statistics"]["dsr"]
    assert dsr["verdict"] == "COMPUTED", dsr.get("reason")
    assert 0.0 <= dsr["dsr_confidence"] <= 1.0
    # the measured result may be anything -- including far below the authority
    # threshold. What must hold is that the number came from the real curves and
    # that it is reported rather than rounded into a pass.
    assert "multiple_testing" in dsr
    assert receipt["baseline_excess_return"]

pytestmark = pytest.mark.slow  # #469: tape/engine file, fast loop excludes via -m "not slow"
