"""#408 sibling (t_a1cf3a66) — the NX08 gate manifest tool must run to completion.

`v8-next/tools/nx08_gate_manifest.py` publishes a STRUCTURE DEMO receipt that used
to hand-give its capability score (``42.0``) with no score evidence bound, so #408's
binding rule refused it: the tool died *after* writing ``gate_manifest.json`` and
*before* ``scoring_manifest.json``, leaving half its outputs refreshed and a
non-zero exit code.

What is held here is the defect class, not the one literal:

* the tool runs to completion and writes BOTH artifacts;
* the demo's number is the canonical breakdown's aggregate over the determinants the
  manifest declares, and the receipt is verified by re-deriving that number from the
  evidence it binds (``receipt_verifies``), so a hand-given number cannot pass;
* the gate map's own consistency check still equals ``validate_registry()``;
* the demo stays classified ``SYNTHETIC_STRUCTURE_ONLY_NOT_A_MEASUREMENT`` and is not
  published as a capability result.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
TOOL = REPO_ROOT / "tools" / "nx08_gate_manifest.py"
SYNTHETIC_CLASS = "SYNTHETIC_STRUCTURE_ONLY_NOT_A_MEASUREMENT"
DEMO_CASE_ID = "NX08-MANIFEST-STRUCTURE-DEMO"


@pytest.fixture(scope="module")
def manifest_run(
    tmp_path_factory: pytest.TempPathFactory,
) -> tuple[subprocess.CompletedProcess[str], dict, dict]:
    out = tmp_path_factory.mktemp("nx08-manifest")
    completed = subprocess.run(
        [sys.executable, str(TOOL), "--repo-root", str(REPO_ROOT), "--out", str(out)],
        capture_output=True,
        text=True,
        timeout=900,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr[-4000:]
    gate = json.loads((out / "gate_manifest.json").read_text())
    scoring = json.loads((out / "scoring_manifest.json").read_text())
    return completed, gate, scoring


def test_tool_writes_both_manifests_and_exits_zero(
    manifest_run: tuple[subprocess.CompletedProcess[str], dict, dict],
) -> None:
    completed, gate, scoring = manifest_run
    assert gate["gate_count"] == len(gate["gates"]) == 10
    assert scoring["economic_claim"] == "NONE"
    # The receipt is verified by re-deriving the published number from the evidence it
    # carries (#408), so this is the binding assertion: a hand-given or unreproducible
    # number would make verify() return False by name.
    assert scoring["receipt_verifies"] is True
    assert "score_binding=OK" in completed.stdout


def test_gate_manifest_consistency_still_matches_the_registry(
    manifest_run: tuple[subprocess.CompletedProcess[str], dict, dict],
) -> None:
    from v8_next.evaluation.gate_registry import validate_registry

    _, gate, _ = manifest_run
    assert gate["consistency_problems"] == validate_registry()


def test_demo_number_is_the_canonical_aggregate_of_its_declared_determinants(
    manifest_run: tuple[subprocess.CompletedProcess[str], dict, dict],
) -> None:
    _, _, scoring = manifest_run
    demo = scoring["structural_demo_inputs"]
    certificate = scoring["certificate"]
    current = scoring["dual_scoring_record"]["scoring_versions"]["derived_coverage_v1"]

    assert demo["case_id"] == DEMO_CASE_ID
    assert demo["aggregate_status"] == "MEASURED"
    assert set(demo["declared_determinants"]) == {
        "total_bars",
        "total_trades",
        "abstain_rate",
        "series",
    }
    # Two independent producers in one artifact, over the same declared determinants:
    # the receipt's published score and the current side of the dual scoring record.
    # A hand-given literal cannot satisfy both, and the coverage the certificate used
    # is the coverage the evidence carries.
    assert demo["capability_score"] == current["aggregate"]
    assert certificate["research_capability_score"] == demo["capability_score"]
    assert certificate["derivation"]["raw_measurements"]["capability_score"] == demo["capability_score"]
    assert demo["coverage_factor"] == current["coverage_factor"]
    assert certificate["derivation"]["raw_measurements"]["coverage_factor"] == demo["coverage_factor"]


def test_demo_is_still_classified_synthetic_and_never_a_capability_result(
    manifest_run: tuple[subprocess.CompletedProcess[str], dict, dict],
) -> None:
    completed, _, scoring = manifest_run
    demo = scoring["structural_demo_inputs"]
    assert demo["input_class"] == SYNTHETIC_CLASS
    assert "NOT a capability result" in demo["warning"]
    assert SYNTHETIC_CLASS in completed.stdout
    assert scoring["evidence_class"].startswith("MANIFEST OF MEASURED CODE")
