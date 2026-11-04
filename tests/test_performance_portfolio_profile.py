"""The synthetic portfolio fixture must actually exercise native funding and fills."""
import json
import subprocess
import sys
from pathlib import Path


def test_portfolio_mechanics_fixture_records_execution_and_funding(tmp_path: Path) -> None:
    script = Path(__file__).resolve().parents[1] / "tools" / "performance" / "portfolio_profile.py"
    output = tmp_path / "result.json"
    subprocess.run([sys.executable, str(script), "--bars", "100", "--instruments", "1",
                    "--output", str(output)], check=True, capture_output=True, text=True)
    result = json.loads(output.read_text())
    assert result["symbols"] == ["BTCUSDT"]
    assert result["decisions"] == 100
    assert result["fills"] > 0
    assert result["funding_rows"] == result["funding_settlements_fed"] == 12
    assert result["native_position_adjustments"] > 0
    assert "synthetic" in result["workload"]
