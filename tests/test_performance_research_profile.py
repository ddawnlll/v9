"""The research profile exercises persisted receipts without economic authority."""
import json
import subprocess
import sys
from pathlib import Path


def test_research_mechanics_writes_verified_non_economic_artifacts(tmp_path: Path) -> None:
    script = Path(__file__).resolve().parents[1] / "tools" / "performance" / "research_profile.py"
    output = tmp_path / "summary.json"
    artifacts = tmp_path / "artifacts"
    subprocess.run([sys.executable, str(script), "--bars", "50", "--artifacts", str(artifacts),
                    "--output", str(output)], check=True, capture_output=True, text=True)
    result = json.loads(output.read_text())
    assert result["receipt_html_verified"] is True
    assert "NO_ECONOMIC_CLAIM" in result["certificate_status"]
    assert {"result.json", "forensic.html", "benchmark_ledger.jsonl"} <= set(result["artifact_files"])
    assert result["trades"] == 0
