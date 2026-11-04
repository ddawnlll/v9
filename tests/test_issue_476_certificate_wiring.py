"""Issue #476 regression: certificate inputs are never silently absent.

Mechanics-only: synthetic OHLC bars exercise the runner wiring, not an economic
claim.  Before this branch the runner called ``PolicyCertificate.generate`` with
the receipt alone, so the certificate could not name a projection or Minerva
producer.  This test fails on that code (the bridge module does not exist) and
passes only when both producers reach the certificate.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from v8_next.adapters.expert_strategy import ExpertStrategyConfig
from v8_next.certification import MINERVA_REFUSAL
from v8_next.domain.market import Candle
from v8_next.evaluation.report import generate_forensic_html_report
from v8_next.evaluation.runner import BenchmarkCase, BenchmarkRunner

HOUR_NS = 3_600_000_000_000
GENESIS_NS = 1_600_000_000 * 10**9


def _mechanics_candles(count: int = 160) -> list[Candle]:
    candles: list[Candle] = []
    for index in range(count):
        base = 100.0 + (index % 20) * 0.5
        open_ = base
        close = base + (0.8 if index % 2 == 0 else -0.8)
        high = max(open_, close) + 0.3
        low = min(open_, close) - 0.3
        values = tuple(Decimal(str(value)) for value in (open_, high, low, close))
        candles.append(
            Candle(
                "BTCUSDT-PERP.BINANCE",
                GENESIS_NS + index * HOUR_NS,
                GENESIS_NS + (index + 1) * HOUR_NS,
                *values,
                Decimal("10"),
                GENESIS_NS + (index + 1) * HOUR_NS,
                GENESIS_NS + (index + 1) * HOUR_NS,
                f"issue-476-{index}",
            )
        )
    return candles


def test_runner_hands_certificate_named_projection_and_minerva_inputs(tmp_path: Path) -> None:
    case = BenchmarkCase(
        case_id="BC-ISSUE-476-MECHANICS",
        policy_id="pol_issue_476",
        dataset_name="SYNTHETIC-MECHANICS-ONLY",
        strategy_config=ExpertStrategyConfig(
            min_support_quorum=1, max_contradiction_tolerance=28
        ),
    )
    result = BenchmarkRunner(output_dir=tmp_path / "benchmarks").run(
        case, _mechanics_candles()
    )

    certificate_inputs = result.gate_metrics["certificate_inputs"]
    assert certificate_inputs["projection"] is not None
    assert certificate_inputs["minerva"] is not None

    derivation = result.certificate.derivation
    assert derivation is not None
    assert derivation["projection_refusal_reason"]
    assert derivation["minerva_refusal_reason"]
    assert result.certificate.robustness_seal_status.startswith(MINERVA_REFUSAL)

    html_path = tmp_path / "certificate.html"
    assert generate_forensic_html_report(
        result.receipt, html_path, certificate=result.certificate
    )
    html = html_path.read_text(encoding="utf-8")
    assert "Named projection refusal:" in html
    assert "Named Minerva refusal:" in html


def test_caller_supplied_min_trl_days_is_not_discarded() -> None:
    """#476 R2: a measured ``min_trl_days`` handed to the bridge must survive it.

    ``minerva_for_certificate`` used to re-declare ``min_trl_days: float | None = None``
    inside its own body, shadowing the parameter, so a caller-supplied measurement was
    silently replaced by ``None`` and the certificate refused for an unmeasured input it
    had been given.  Both current call sites pass ``None``, so nothing caught it.
    """
    from v8_next.certification import minerva_for_certificate

    result = minerva_for_certificate(
        statistics={
            "dsr": {"dsr_confidence": 0.97},
            "pbo": {"pbo": 0.10},
            "spa": {"pvalues": {"consistent": 0.01}},
        },
        actual_track_days=400.0,
        min_trl_days=12.5,
        min_trl_plan=None,
        observed_intervals=None,
        worst_regime_return_bps=-50.0,
        regime_floor_bps=-200.0,
    )

    assert result.measured_inputs is not None
    assert result.measured_inputs["min_trl_days"] == 12.5
    assert "min_trl_days=UNMEASURED" not in (result.refusal_reason or "")
    assert "DECLARED_MINTRL_PLAN_REQUIRED" not in (result.refusal_reason or "")
