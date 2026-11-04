"""Allocation snapshots are separate from timing baselines and fail closed."""
import json
import subprocess
import sys
from pathlib import Path


def test_allocation_snapshot_and_directory_window_guard(tmp_path: Path) -> None:
    script = Path(__file__).resolve().parents[1] / "tools" / "performance" / "ensemble_profile.py"
    summary = tmp_path / "summary.json"
    allocations = tmp_path / "allocations.json"
    subprocess.run([sys.executable, str(script), "--bars", "4", "--allocations", str(allocations),
                    "--output", str(summary)], check=True, capture_output=True, text=True)
    sample = json.loads(summary.read_text())
    snapshot = json.loads(allocations.read_text())
    assert sample["allocations_instrumented"] is True
    assert sample["decisions"] == 4
    assert snapshot["stack_depth"] == 1
    assert snapshot["peak_bytes"] >= snapshot["current_bytes"] > 0
    assert snapshot["top_lines"]
    invalid = subprocess.run([sys.executable, str(script), "--tape", str(tmp_path),
                              "--start-ms", "1", "--output", str(summary)],
                             capture_output=True, text=True)
    assert invalid.returncode != 0
    assert "directory loader does not apply window bounds" in invalid.stderr
