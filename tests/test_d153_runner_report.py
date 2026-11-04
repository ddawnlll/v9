"""Tests for End-to-End D-153 Benchmark Runner and Forensic HTML Report (Rule 12, 31, 57).

Evaluation-claim rule: this module runs BenchmarkCase on the real
BurnedDiagnosticReal population only. Synthetic candles are banned here;
mechanics-only synthetic coverage lives in test_opportunities.py.
"""

from pathlib import Path

import pytest

from v8_next.adapters.expert_strategy import ExpertStrategyConfig
from v8_next.evaluation.benchmark_receipt import GateState, ReadinessStatus
from v8_next.evaluation.gate_resolution import DEFAULT_TAPE_PATH, load_tape_candles
from v8_next.evaluation.report import generate_forensic_html_report
from v8_next.evaluation.runner import BenchmarkCase, BenchmarkRunner

REAL_BARS = 500


def test_d153_runner_end_to_end_and_html_generation(tmp_path: Path):
    if not DEFAULT_TAPE_PATH.exists():
        pytest.skip(f"Real tape not found at {DEFAULT_TAPE_PATH}")
    candles = load_tape_candles(DEFAULT_TAPE_PATH, limit=REAL_BARS)

    case = BenchmarkCase(
        case_id="BC-D153-REAL-01",
        policy_id="pol_28_expert_ensemble",
        dataset_name="BTCUSDT-1H-REAL",
        allowed_populations=("BurnedDiagnosticReal",),
        strategy_config=ExpertStrategyConfig(min_support_quorum=1, max_contradiction_tolerance=28),
    )

    runner = BenchmarkRunner(output_dir=tmp_path / "benchmarks")
    result = runner.run(case, candles)

    assert result.total_bars == REAL_BARS
    assert result.total_trades >= 0
    # NX08.R1: this cell takes no trades, so there is no measured capability:
    # the score is MISSING (None) instead of a fabricated 0.0.
    if result.total_trades == 0:
        assert result.capability_score is None
    else:
        assert result.capability_score is not None
        assert 0.0 <= result.capability_score <= 100.0

    # Diagnostic gate vector (no gate resolution battery in this cell).
    # G0 is genuinely measured from candle lineage; G1/G2 have no measurement
    # in the runner and resolve to UNKNOWN (never PASS).
    assert result.gates.g0_identity == GateState.PASS
    assert result.gates.g1_causal_pit == GateState.UNKNOWN
    assert result.gates.g2_determinism_ledger == GateState.UNKNOWN
    assert result.gates.g3_scenario_robustness == GateState.UNKNOWN
    assert result.gates.g8_prospective_shadow == GateState.MISSING
    assert result.gates.g9_live_realization == GateState.MISSING

    # Readiness verdict & Certificate
    verdict = result.gates.readiness()
    assert verdict.status == ReadinessStatus.InsufficientEvidence
    assert "NO_ECONOMIC_CLAIM" in result.certificate.status
    # NX08.R2: no Minerva run and no projection in this cell, so there is no
    # robustness and no economic measurement. The readiness index is MISSING
    # rather than a number built on the retired 50.0/60.0 defaults.
    assert result.certificate.readiness_index is None
    assert result.certificate.minerva_robustness_score is None
    assert result.certificate.economic_score is None
    assert set(result.certificate.missing_measurements) >= {
        "minerva_robustness_score",
        "economic_score",
    }
    derivation = result.certificate.derivation
    assert derivation is not None
    assert derivation["binding"]["receipt_digest"] == result.receipt.receipt_digest
    assert derivation["raw_measurements"]["minerva_effective_score"] is None
    assert result.certificate.readiness_upper_bound is None or (
        result.certificate.readiness_index is not None
    )
    # NX08.R5: both scorer versions travel in the same receipt, side by side
    versions = result.receipt.scoring_versions
    assert "legacy_fixed_coverage_v1" in versions["scoring_versions"]
    assert "derived_coverage_v1" in versions["scoring_versions"]
    assert versions["delta_kind"] in ("TRANSFORM_ONLY", "NOT_COMPARABLE_MISSING_MEASUREMENT")
    if versions["delta_kind"] == "TRANSFORM_ONLY":
        assert versions["delta"] is not None
    else:
        # no measured aggregate on this cell: the comparison is not reported as a
        # delta at all, rather than as 0.0
        assert versions["delta"] is None
        assert versions["scoring_versions"]["derived_coverage_v1"]["aggregate"] is None
    assert versions["claim_status"] == "NO_ECONOMIC_CLAIM"

    # Terminal ASCII rendering check
    ascii_out = result.certificate.render_ascii()
    assert "G0ConstitutionalIntegrity::g0_identity" in ascii_out
    assert "G9Certificate::g9_live_realization" in ascii_out
    assert "READINESS INDEX:" in ascii_out

    # Check ledger persistence
    assert len(runner.ledger) == 1
    chain_ok, _ = runner.ledger.verify_chain()
    assert chain_ok is True

    # Generate and verify forensic HTML report
    html_path = tmp_path / "benchmarks" / "report.html"
    is_valid = generate_forensic_html_report(result.receipt, html_path)
    assert is_valid is True
    assert html_path.is_file()

    html_text = html_path.read_text(encoding="utf-8")
    assert "V8.5 Benchmark Fabric" in html_text
    assert "CONSTITUTIONAL NOTICE (Rule 12 & Rule 57)" in html_text
    assert "Hard-Gate Verification Matrix (G0-G9 Non-Compensable)" in html_text
    assert "G0ConstitutionalIntegrity::g0_identity" in html_text
    assert result.receipt.receipt_digest in html_text

    # No forced-pass mode exists: gates cannot be certified without evidence.
    # The all_pass backdoor was removed; a bare diagnostic run never holds.
    assert result.gates.all_pass() is False

pytestmark = pytest.mark.slow  # #469: tape/engine file, fast loop excludes via -m "not slow"
