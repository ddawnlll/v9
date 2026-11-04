"""#476 R4: an issue body without a wiring manifest is rejected by form.

The seven port-chain issues were closed as "completed R IDs: all" while nothing imported
the modules they delivered, because the mandatory wiring manifest was never enforced on
API-created issues. ``tools/check_issue_wiring_manifest.py`` is the enforcement mechanism;
these tests pin its verdicts.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

TOOL = Path(__file__).resolve().parents[1] / "tools" / "check_issue_wiring_manifest.py"

GOOD = """## Objective
Wire the thing.

## W1-W7 Wiring manifest
- W1 Producer: `adapters/foo.run` -> `result`
- W2 Transport: engine dict -> `evaluation.bar.series`
- W3 Consumer: `evaluation.metrics_for_curve`
- W4 Final surface: `economic_receipt.metrics.foo`
- W5 Identity/lineage + join key: `case_id + run_key.digest`
- W6 Missing/stale/mismatch semantics: named absence, never zero
- W7 E2E command + artifact: the CLI run above, receipt path

## Completed R IDs
R1

## Remaining R IDs
NONE
"""


def _run(body: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(TOOL), "--json"],
        input=body, capture_output=True, text=True,
    )


def test_complete_manifest_passes() -> None:
    result = _run(GOOD)
    assert result.returncode == 0, result.stderr
    assert '"ok": true' in result.stdout


def test_missing_manifest_is_named() -> None:
    result = _run("## Objective\nShip it.\n\n## Remaining R IDs\nNONE\n")
    assert result.returncode == 1
    assert "WIRING_MANIFEST_ABSENT" in result.stderr or "WIRING_MANIFEST_ABSENT" in result.stdout


def test_incomplete_manifest_names_the_missing_steps() -> None:
    body = GOOD.replace("- W4 Final surface: `economic_receipt.metrics.foo`\n", "")
    result = _run(body)
    assert result.returncode == 1
    combined = result.stdout + result.stderr
    assert "WIRING_MANIFEST_INCOMPLETE" in combined and "W4" in combined


def test_missing_remaining_section_is_named() -> None:
    body = GOOD.replace("## Remaining R IDs\nNONE\n", "")
    result = _run(body)
    assert result.returncode == 1
    assert "REMAINING_R_IDS_ABSENT" in result.stdout + result.stderr


def test_tool_cannot_claim_wiring_truth() -> None:
    """The checker validates form only, and says so — it must not imply verification."""
    result = _run(GOOD)
    assert "FORM only" in result.stdout
