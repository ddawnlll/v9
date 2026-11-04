"""Smoke tests for the central v8-next CLI (status dashboard + benchmark binding)."""

from pathlib import Path

import pytest

from v8_next.app.cli import main as cli_main
from v8_next.evaluation.benchmark_receipt import BenchmarkLedger
from v8_next.evaluation.gate_resolution import DEFAULT_TAPE_PATH


def test_cli_status_without_ledger(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    rc = cli_main(["status", "--output-dir", str(tmp_path / "empty")])
    assert rc == 0
    out = capsys.readouterr().out
    assert "=== v8-next status ===" in out
    assert "rev:" in out
    assert "tapes:" in out
    assert "no ledger" in out


def test_cli_status_with_ledger(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    if not DEFAULT_TAPE_PATH.exists():
        pytest.skip(f"Real tape not found at {DEFAULT_TAPE_PATH}")
    from v8_next.adapters.expert_strategy import ExpertStrategyConfig
    from v8_next.evaluation.gate_resolution import load_tape_candles
    from v8_next.evaluation.runner import BenchmarkCase, BenchmarkRunner

    out_dir = tmp_path / "benchmarks"
    candles = load_tape_candles(DEFAULT_TAPE_PATH, limit=120)
    case = BenchmarkCase(
        case_id="BC-CLI-STATUS-01",
        policy_id="pol_cli_smoke",
        dataset_name="BTCUSDT-1H-REAL",
        strategy_config=ExpertStrategyConfig(min_support_quorum=1, max_contradiction_tolerance=28),
    )
    BenchmarkRunner(output_dir=out_dir).run(case, candles)
    assert (out_dir / "benchmark_ledger.jsonl").is_file()

    ledger = BenchmarkLedger.load_jsonl(out_dir / "benchmark_ledger.jsonl")
    assert len(ledger) == 1

    rc = cli_main(["status", "--output-dir", str(out_dir)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "chain: VERIFIED" in out
    assert "BC-CLI-STATUS-01" in out
    assert "READINESS INDEX:" in out

pytestmark = pytest.mark.slow  # #469: tape/engine file, fast loop excludes via -m "not slow"
