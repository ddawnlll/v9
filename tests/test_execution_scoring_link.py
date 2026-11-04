"""MECHANICS ONLY (no tape) + real-tape discrimination tests.

No economic claim is made anywhere in this file. The mechanics section pins how
the ExecutionFidelity domain decides between *measured* execution evidence and
the legacy PnL-Sharpe proxy, which measured statistic it reads, when a present
statistic is degenerate and must therefore abstain under a named reason (#439),
and what it publishes about that choice. The last section runs the shared
portfolio engine over the real quad tape — it is evaluative, skips when the tape
is absent, and asserts only that the published dimension follows the measured
shortfall (and abstains on a degenerate one) instead of reporting a constant.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from v8_next.adapters.tape_paths import tape_path
from v8_next.evaluation.scoring import (
    DOMAIN_STATUSES,
    EXECUTION_FIDELITY_DEGENERATE_INERT_CONFIGURATION,
    EXECUTION_FIDELITY_DEGENERATE_ZERO_SIGNED_MEAN,
    EXECUTION_FIDELITY_DEGENERATE_ZERO_VARIATION,
    EXECUTION_FIDELITY_REFERENCE_BPS,
    EXECUTION_FIDELITY_SHORTFALL_FIELD,
    compute_capability_breakdown,
)

PNL = [0.01, -0.005, 0.02, -0.01, 0.015]

#: The execution block pinned from the #439 evidence receipt (read-only):
#: ``artifacts/nx397-turnover-fix/portfolio-after-merge/economic_receipt_a9035114.json``.
#: 18 measured samples under the ``baseline`` profile: default fill model,
#: ``prob_slippage=0.0``, bar execution. Every magnitude is therefore 0.0 by
#: construction, and the block predates the magnitude statistic, so the reader
#: falls back to the signed mean -- which is exactly the block that published
#: ExecutionFidelity 1.0 labelled MEASURED_IMPLEMENTATION_SHORTFALL.
PINNED_DEGENERATE_BLOCK: dict[str, Any] = {
    "profile": "baseline",
    "fill_model": "default",
    "fill_model_slipped": False,
    "prob_fill_on_limit": 1.0,
    "prob_slippage": 0.0,
    "bar_execution": True,
    "trade_execution": True,
    "slippage_samples": 18,
    "slippage_unmatched_positions": 0,
    "slippage_rejected_cross_series": 0,
    "slippage_bps_mean": 0.0,
    "slippage_bps_max": 0.0,
    "slippage_bps_min": -0.0,
}


def _breakdown(execution=None):
    return compute_capability_breakdown(
        pnl_series=PNL,
        total_bars=500,
        total_trades=len(PNL),
        abstain_rate=0.0,
        execution=execution,
    )


def test_without_execution_evidence_the_source_is_declared_as_the_proxy() -> None:
    out = _breakdown(None)
    assert out["execution_fidelity_source"] == "PNL_SHARPE_PROXY"
    assert out["execution_fidelity_reference_bps"] == EXECUTION_FIDELITY_REFERENCE_BPS


def test_zero_samples_do_not_count_as_measured_execution() -> None:
    out = _breakdown({"slippage_samples": 0, "slippage_bps_mean": 0.0})
    assert out["execution_fidelity_source"] == "PNL_SHARPE_PROXY"


def test_measured_shortfall_is_used_when_samples_exist() -> None:
    out = _breakdown({"slippage_samples": 3, "slippage_bps_mean": 6.0})
    assert out["execution_fidelity_source"] == "MEASURED_IMPLEMENTATION_SHORTFALL"
    # 1 - 6/10 = 0.40
    assert out["domains"]["ExecutionFidelity"]["score"] == pytest.approx(40.0)
    # the measured input the score came from is published, not just the score
    assert out["execution_fidelity_shortfall_bps"] == pytest.approx(6.0)


# ------------------------------------------------------------------ #439 G1/G2/G3


def test_degenerate_zero_shortfall_is_not_published_as_a_measurement() -> None:
    """#439 G1: a statistic its own configuration forces to zero is not evidence.

    The pinned block scored ExecutionFidelity 1.0 -- the top of the domain's
    declared range -- and labelled the source MEASURED_IMPLEMENTATION_SHORTFALL,
    while the statistic it read was 18 identical zeros produced by a fill model
    that cannot slip. A degenerate statistic must resolve to a named
    non-measurement instead, with the reason serialized into the breakdown.
    """
    out = _breakdown(dict(PINNED_DEGENERATE_BLOCK))

    assert out["execution_fidelity_source"] != "MEASURED_IMPLEMENTATION_SHORTFALL"
    assert out["execution_fidelity_source"] == EXECUTION_FIDELITY_DEGENERATE_INERT_CONFIGURATION

    # nothing is published as a score for a domain whose only statistic is degenerate
    assert "ExecutionFidelity" not in out["domains"]

    # the input it abstained on is retained, so the abstention stays auditable
    assert out["execution_fidelity_shortfall_bps"] == pytest.approx(0.0)
    assert out["execution_fidelity_reference_bps"] == EXECUTION_FIDELITY_REFERENCE_BPS

    # the reason rides in the existing measurement-status structure, not a new one
    statuses = {item["domain"]: item for item in out["domain_measurement_statuses"]}
    execution = statuses["ExecutionFidelity"]
    assert execution["status"] in DOMAIN_STATUSES
    assert execution["status"] != "MEASURED"
    assert execution["reason"] == EXECUTION_FIDELITY_DEGENERATE_INERT_CONFIGURATION
    assert execution["sample_size"] == 18

    # and the derived coverage counts it as eligible-but-unmeasured, by name
    assert out["coverage"]["statuses"]["ExecutionFidelity"] == execution["status"]
    assert out["domain_statuses"]["ExecutionFidelity"] == execution["status"]
    assert "ExecutionFidelity" in out["coverage"]["unmeasured_domains"]
    assert out["coverage"]["numerator"] < out["coverage"]["denominator"]


def test_zero_magnitude_across_samples_abstains_under_its_own_reason() -> None:
    """A zero magnitude with no declared configuration is still no variation.

    The magnitude field is the declared statistic and magnitudes are
    non-negative, so a mean of exactly 0.0 means every sample was identical: the
    number cannot discriminate one fill's execution from another's.
    """
    out = _breakdown({"slippage_samples": 18, EXECUTION_FIDELITY_SHORTFALL_FIELD: 0.0})
    assert out["execution_fidelity_source"] == EXECUTION_FIDELITY_DEGENERATE_ZERO_VARIATION
    assert "ExecutionFidelity" not in out["domains"]


def test_zero_signed_mean_without_the_magnitude_statistic_abstains_by_name() -> None:
    """The legacy fallback cannot prove a zero deviation, so it does not claim one.

    |signed mean| of 0.0 is also what two opposite-signed fills produce, so a
    block that carries only the signed mean and reads zero has no evidence that
    execution was clean -- it abstains under a reason that says exactly that,
    instead of publishing the top of the range.
    """
    out = _breakdown({"slippage_samples": 18, "slippage_bps_mean": 0.0})
    assert out["execution_fidelity_source"] == EXECUTION_FIDELITY_DEGENERATE_ZERO_SIGNED_MEAN
    assert "ExecutionFidelity" not in out["domains"]


def test_non_degenerate_shortfall_keeps_the_declared_mapping() -> None:
    """#439 G2: a real measured deviation still maps through clip(1 - bps/10).

    The degeneracy guard must not touch the declared mapping: 5 bps is still 0.5.
    """
    out = _breakdown({"slippage_samples": 18, EXECUTION_FIDELITY_SHORTFALL_FIELD: 5.0})
    assert out["execution_fidelity_source"] == "MEASURED_IMPLEMENTATION_SHORTFALL"
    assert out["domains"]["ExecutionFidelity"]["score"] == pytest.approx(50.0)
    assert out["execution_fidelity_shortfall_bps"] == pytest.approx(5.0)
    assert out["coverage"]["statuses"]["ExecutionFidelity"] == "MEASURED"

    # a measurement beats a declaration: an inert configuration that somehow
    # measured a real deviation is scored on the measurement, not the profile
    declared_inert = _breakdown({"slippage_samples": 18, EXECUTION_FIDELITY_SHORTFALL_FIELD: 5.0,
                                 "prob_slippage": 0.0, "fill_model_slipped": False})
    assert declared_inert["execution_fidelity_source"] == "MEASURED_IMPLEMENTATION_SHORTFALL"
    assert declared_inert["domains"]["ExecutionFidelity"]["score"] == pytest.approx(50.0)


def test_missing_or_empty_shortfall_never_becomes_the_top_of_the_range() -> None:
    """#439 G3: fail closed -- absent evidence must not score 1.0 by default."""
    cases = {
        "samples but no statistic": {"slippage_samples": 18},
        "statistic present but empty": {
            "slippage_samples": 18,
            EXECUTION_FIDELITY_SHORTFALL_FIELD: None,
            "slippage_bps_mean": None,
        },
        "no samples": {"slippage_samples": 0, "slippage_bps_mean": 0.0},
        "no block": None,
    }
    for label, block in cases.items():
        out = _breakdown(block)
        assert out["execution_fidelity_source"] == "PNL_SHARPE_PROXY", label
        assert out["domains"]["ExecutionFidelity"]["score"] <= 50.0, label
        assert out["execution_fidelity_shortfall_bps"] is None, label


def _declared_score(shortfall_bps: float) -> float:
    """Published ExecutionFidelity for a measured shortfall of that magnitude."""
    out = _breakdown(
        {
            "slippage_samples": 3,
            "slippage_bps_mean": shortfall_bps,
            EXECUTION_FIDELITY_SHORTFALL_FIELD: shortfall_bps,
        }
    )
    assert out["execution_fidelity_source"] == "MEASURED_IMPLEMENTATION_SHORTFALL"
    assert out["execution_fidelity_shortfall_bps"] == pytest.approx(shortfall_bps)
    return out["domains"]["ExecutionFidelity"]["score"]


def test_declared_range_is_monotone_and_zero_at_the_reference() -> None:
    """The declared mapping is 1 - bps/10 over [0, 1]: monotone, no hidden ceiling.

    The 10 bps reference is zero — the convention the same module states one line
    above the mapping — and a smaller measured deviation scores higher. The exact
    upper bound is reached by a deviation that is real but negligible, not by a
    degenerate zero (see #439 G1).
    """
    magnitudes = [0.25, 1.0, 2.5, 5.0, 7.5, 9.9, 10.0]
    scores = [_declared_score(m) for m in magnitudes]

    assert all(
        lower > higher for lower, higher in zip(scores, scores[1:], strict=False)
    )
    assert scores[-1] == pytest.approx(0.0)
    for magnitude, score in zip(magnitudes, scores, strict=True):
        expected = max(0.0, 1.0 - magnitude / EXECUTION_FIDELITY_REFERENCE_BPS) * 100.0
        assert score == pytest.approx(round(expected, 1))
    # past the reference the domain sits on its declared floor, never below it
    assert _declared_score(1e6) == pytest.approx(0.0)


def test_opposite_signed_fills_do_not_cancel_away_the_measured_deviation() -> None:
    """The mechanism behind the reported "baseline 0.0 vs realistic 0.0004 bps".

    Two fills that both landed 0.05 bps away from their decision price, on
    opposite sides, average to 0.0 bps in the signed mean — the statistic that
    made every published fidelity a constant. The magnitude statistic the
    convention names must not cancel them, and the published score must not read
    as untouched execution.
    """
    out = _breakdown(
        {"slippage_samples": 2, "slippage_bps_mean": 0.0,
         EXECUTION_FIDELITY_SHORTFALL_FIELD: 0.05}
    )
    assert out["execution_fidelity_source"] == "MEASURED_IMPLEMENTATION_SHORTFALL"
    assert out["execution_fidelity_shortfall_bps"] == pytest.approx(0.05)
    assert out["domains"]["ExecutionFidelity"]["score"] == pytest.approx(99.5)


def test_shortfall_sign_does_not_matter_only_magnitude() -> None:
    adverse = _breakdown(
        {"slippage_samples": 3, EXECUTION_FIDELITY_SHORTFALL_FIELD: 6.0}
    )
    favourable = _breakdown(
        {"slippage_samples": 3, EXECUTION_FIDELITY_SHORTFALL_FIELD: -6.0}
    )
    assert (
        adverse["domains"]["ExecutionFidelity"]["score"]
        == favourable["domains"]["ExecutionFidelity"]["score"]
        == pytest.approx(40.0)
    )


def test_a_block_predating_the_magnitude_field_keeps_its_measurement() -> None:
    """A persisted block from before the magnitude field keeps its measured link.

    |signed mean| is the same quantity minus the cancellation, so such a block
    stays bound to the evidence it has instead of silently downgrading to the
    PnL proxy.
    """
    legacy = _breakdown({"slippage_samples": 3, "slippage_bps_mean": -2.0})
    assert legacy["execution_fidelity_source"] == "MEASURED_IMPLEMENTATION_SHORTFALL"
    assert legacy["execution_fidelity_shortfall_bps"] == pytest.approx(2.0)
    assert legacy["domains"]["ExecutionFidelity"]["score"] == pytest.approx(80.0)


def test_negligible_deviation_is_the_top_of_the_range_not_the_old_ceiling() -> None:
    """Rescoped from the removed ``_documented_saturation_when_the_model_is_negligible``.

    That test pinned a near-zero-friction model at a 0.50 ceiling which
    contradicted the declared convention in the same file. Under the declared
    mapping a deviation that is real but five orders of magnitude below the 10 bps
    reference sits at the top of the range: it is indistinguishable at the
    published resolution *by that scale's design*, and the reference that sets the
    scale is published next to every score. An exactly-zero statistic is not that
    case -- it carries no deviation to measure and publishes no score (#439).
    """
    tiny = _breakdown(
        {"slippage_samples": 3, "slippage_bps_mean": 0.000404,
         EXECUTION_FIDELITY_SHORTFALL_FIELD: 0.000404}
    )
    assert tiny["domains"]["ExecutionFidelity"]["score"] == pytest.approx(100.0)
    assert tiny["domains"]["ExecutionFidelity"]["score"] != pytest.approx(50.0)
    assert tiny["execution_fidelity_reference_bps"] == EXECUTION_FIDELITY_REFERENCE_BPS

    zero = _breakdown(
        {"slippage_samples": 3, "slippage_bps_mean": 0.0,
         EXECUTION_FIDELITY_SHORTFALL_FIELD: 0.0}
    )
    assert "ExecutionFidelity" not in zero["domains"]
    assert zero["execution_fidelity_source"] == EXECUTION_FIDELITY_DEGENERATE_ZERO_VARIATION


def test_telemetry_publishes_the_statistic_the_declared_convention_reads() -> None:
    """MECHANICS ONLY: producer/consumer agreement on the declared statistic.

    Synthetic fills with zero evaluative weight, crafted so the two statistics
    disagree: a buy and a sell both landing 100.01 against a 100.00 decision
    price deviate ~1 bps each, and cancel in the signed mean.
    """
    from v8_next.adapters.execution_models import PROFILES
    from v8_next.adapters.execution_telemetry import execution_telemetry

    block = execution_telemetry(
        PROFILES["baseline"],
        [],
        "TEST",
        [
            {"instrument_id": "X", "side": "BUY", "avg_px_open": 100.01, "event_ns": 10},
            {"instrument_id": "X", "side": "SELL", "avg_px_open": 100.01, "event_ns": 20},
        ],
        [
            {"instrument_id": "X", "decision_ns": 1, "close": 100.0},
            {"instrument_id": "X", "decision_ns": 11, "close": 100.0},
        ],
    )
    assert block["slippage_samples"] == 2
    assert block["slippage_bps_mean"] == pytest.approx(0.0)
    assert block[EXECUTION_FIDELITY_SHORTFALL_FIELD] == pytest.approx(1.0)
    # the declared configuration is the inert one, yet the measured deviation is
    # real: a measurement is never vetoed by what the profile declares (#439)
    assert block["slippage_configuration_cannot_slip"] is True
    assert block["slippage_magnitudes_all_zero"] is False

    out = _breakdown(block)
    assert out["execution_fidelity_source"] == "MEASURED_IMPLEMENTATION_SHORTFALL"
    assert out["domains"]["ExecutionFidelity"]["score"] == pytest.approx(90.0)


def test_telemetry_names_the_degeneracy_of_a_configuration_that_cannot_slip() -> None:
    """MECHANICS ONLY: producer-side signals for a statistic that cannot vary.

    The ``baseline`` profile declares ``prob_slippage=0.0`` with a fill model that
    never slips, so the magnitudes it produces are zero by construction and say
    nothing about the venue. The block must publish that (and the spread of its
    magnitudes), and the domain must abstain on it under a named reason -- this is
    the exact pipeline that produced the pinned 1.0 in #439.
    """
    from v8_next.adapters.execution_models import PROFILES
    from v8_next.adapters.execution_telemetry import execution_telemetry

    block = execution_telemetry(
        PROFILES["baseline"],
        [],
        "TEST",
        [{"instrument_id": "X", "side": "BUY", "avg_px_open": 100.0, "event_ns": 10}],
        [{"instrument_id": "X", "decision_ns": 1, "close": 100.0}],
    )
    assert block["slippage_samples"] == 1
    assert block[EXECUTION_FIDELITY_SHORTFALL_FIELD] == pytest.approx(0.0)
    assert block["slippage_bps_abs_max"] == pytest.approx(0.0)
    assert block["slippage_magnitudes_all_zero"] is True
    assert block["slippage_configuration_cannot_slip"] is True

    out = _breakdown(block)
    assert out["execution_fidelity_source"] == EXECUTION_FIDELITY_DEGENERATE_INERT_CONFIGURATION
    assert "ExecutionFidelity" not in out["domains"]


def test_telemetry_does_not_name_a_slipping_configuration_degenerate() -> None:
    """MECHANICS ONLY: the signals must not fire for a configuration that can slip.

    A fill model that slips measures a real deviation, so the same block shape
    stays measurable and follows the declared mapping.
    """
    from v8_next.adapters.execution_models import PROFILES
    from v8_next.adapters.execution_telemetry import execution_telemetry

    block = execution_telemetry(
        PROFILES["realistic"],
        [],
        "TEST",
        [{"instrument_id": "X", "side": "BUY", "avg_px_open": 100.01, "event_ns": 10}],
        [{"instrument_id": "X", "decision_ns": 1, "close": 100.0}],
    )
    assert block["slippage_magnitudes_all_zero"] is False
    assert block["slippage_configuration_cannot_slip"] is False
    assert block["slippage_bps_abs_max"] == pytest.approx(
        block[EXECUTION_FIDELITY_SHORTFALL_FIELD]
    )

    out = _breakdown(block)
    assert out["execution_fidelity_source"] == "MEASURED_IMPLEMENTATION_SHORTFALL"
    assert out["domains"]["ExecutionFidelity"]["score"] == pytest.approx(90.0)


def test_telemetry_leaves_the_degeneracy_signals_unset_without_samples() -> None:
    """MECHANICS ONLY: no samples is not a degeneracy, it is an absence."""
    from v8_next.adapters.execution_models import PROFILES
    from v8_next.adapters.execution_telemetry import execution_telemetry

    block = execution_telemetry(PROFILES["baseline"], [], "TEST", [], [])
    assert block["slippage_samples"] == 0
    assert block["slippage_magnitudes_all_zero"] is None
    assert block["slippage_bps_abs_max"] is None
    assert block[EXECUTION_FIDELITY_SHORTFALL_FIELD] is None


def test_no_trades_keeps_an_empty_domain_set_and_names_the_reason() -> None:
    out = compute_capability_breakdown(
        pnl_series=[], total_bars=500, total_trades=0, abstain_rate=0.0,
        execution={"slippage_samples": 3, "slippage_bps_mean": 1.0},
    )
    assert out["domains"] == {}
    # NX08.R1: with no trades the aggregate is missing, not zero, and the reason
    # is named; the coverage factor is derived (trade domains abstained).
    assert out["aggregate"] is None
    assert out["aggregate_status"] == "MISSING_NO_TRADES"
    assert out["coverage_source"] == "DERIVED_FROM_MEASURED_DOMAINS"
    assert out["coverage"]["statuses"]["ExecutionFidelity"] == "ABSTAINED"
    assert out["coverage"]["numerator"] < out["coverage"]["denominator"]
    assert out["execution_fidelity_source"] == "NO_TRADES"
    # no domain was scored, so no measured input may be published as if one was
    assert out["execution_fidelity_shortfall_bps"] is None


# ------------------------------------------------- telemetry -> utility inputs


def test_execution_telemetry_artifact_round_trips_and_detects_tampering(tmp_path) -> None:
    from v8_next.adapters.execution_telemetry import (
        load_execution_telemetry,
        persist_execution_telemetry,
    )

    block = {
        "profile": "realistic",
        "digest": "abc123",
        "fills_count": 5,
        "slippage_samples": 3,
        "slippage_bps_mean": 2.0,
        "commission_total": 1.5,
    }
    path = persist_execution_telemetry(tmp_path / "execution_telemetry.json", block)
    assert load_execution_telemetry(path) == block

    # tampering is detected by the artifact's own digest
    import json

    tampered = dict(block)
    tampered["slippage_bps_mean"] = 999.0
    path.write_text(json.dumps({**tampered, "sha256": json.loads(path.read_text())["sha256"]}))
    assert load_execution_telemetry(path) == {}


def test_absent_or_malformed_artifact_yields_nothing(tmp_path) -> None:
    from v8_next.adapters.execution_telemetry import load_execution_telemetry

    assert load_execution_telemetry(tmp_path / "missing.json") == {}
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    assert load_execution_telemetry(bad) == {}


def test_friction_inputs_only_report_measured_values() -> None:
    from v8_next.adapters.execution_telemetry import execution_friction_inputs

    assert execution_friction_inputs({}) == {}
    # no samples -> no slippage field at all (the caller's None must survive)
    no_samples = execution_friction_inputs({"slippage_samples": 0, "commission_total": 1.5})
    assert "slippage" not in no_samples
    assert no_samples["fees"] == pytest.approx(1.5)

    measured = execution_friction_inputs(
        {
            "profile": "realistic",
            "digest": "d" * 64,
            "slippage_samples": 3,
            "slippage_bps_mean": -2.0,
            "commission_total": 1.5,
        }
    )
    assert measured["slippage"] == pytest.approx(2.0 / 1e4)
    assert measured["fees"] == pytest.approx(1.5)
    assert measured["calibration_receipt"] == f"execution_profile:realistic:{'d' * 64}"


def test_partially_measured_friction_still_fails_closed() -> None:
    """Only fees and slippage are measured, so spread and funding stay None.

    UtilityInputs.net() requires every friction term, so admission must keep
    rejecting on missing calibration rather than treating unmeasured terms as
    free. This pins that the wiring does not accidentally open the gate.
    """
    from decimal import Decimal

    from v8_next.adapters.execution_telemetry import execution_friction_inputs
    from v8_next.economics.decisions import UtilityInputs, utility_admission

    friction = execution_friction_inputs(
        {"profile": "realistic", "digest": "d" * 64, "slippage_samples": 3,
         "slippage_bps_mean": 2.0, "commission_total": 1.5}
    )
    inputs = UtilityInputs(
        gross_edge=Decimal("10"),
        fees=Decimal(str(friction["fees"])),
        spread=None,
        slippage=Decimal(str(friction["slippage"])),
        funding_cost=None,
        uncertainty=Decimal("0.5"),
        calibration_receipt=friction["calibration_receipt"],
    )
    assert utility_admission(inputs) == "REJECTED_MISSING_CALIBRATION"


def test_fully_measured_friction_lets_the_admission_evaluate() -> None:
    """With every friction term measured the decision evaluates, not rejects."""
    from decimal import Decimal

    from v8_next.economics.decisions import UtilityInputs, utility_admission

    def inputs(gross_edge: str) -> UtilityInputs:
        return UtilityInputs(
            gross_edge=Decimal(gross_edge),
            fees=Decimal("1"),
            spread=Decimal("1"),
            slippage=Decimal("1"),
            funding_cost=Decimal("1"),
            uncertainty=Decimal("0.5"),
            calibration_receipt="execution_profile:realistic:" + "d" * 64,
        )

    assert utility_admission(inputs("10")) == "UTILITY_ELIGIBLE"
    assert utility_admission(inputs("2")) == "REJECTED_SUB_FRICTION"


# --------------------------------------------- real tape: does it discriminate
#
# Everything below runs the shared portfolio engine over the real quad tape.
# It is evaluative (it asserts a property of a published dimension), it never
# uses synthetic candles, and it skips when the tape is absent. 120 bars is the
# window the defect report itself used — no full scan is needed to see whether
# the dimension responds to measured execution.

QUAD_TAPE = tape_path("quad-1h-12m")
QUAD_BARS = 120

#: The #439 evidence receipt (read-only). Evidence artifacts live outside git
#: (worktrees do not carry them), so the worktree falls back to the parent
#: checkout and absence skips: the pinned shape is also asserted inline.
PINNED_RECEIPT_REL = (
    "artifacts/nx397-turnover-fix/portfolio-after-merge/economic_receipt_a9035114.json"
)


def _pinned_receipt_path() -> Path | None:
    root = Path(__file__).resolve().parents[2]
    for candidate in (root / PINNED_RECEIPT_REL, root.parent.parent / PINNED_RECEIPT_REL):
        if candidate.is_file():
            return candidate
    return None


def test_evidence_receipt_execution_block_no_longer_scores_the_top_of_the_range() -> None:
    """#439 on the pinned receipt: the block that published 1.0 abstains by name.

    Read-only evidence: the receipt's own execution block (18 samples under
    ``baseline``, ``prob_slippage=0.0``, bar execution) is fed to the scoring
    entry point as-is. Pre-fix it published ExecutionFidelity 100.0 labelled
    MEASURED_IMPLEMENTATION_SHORTFALL; it must now resolve to the named
    non-measurement and publish no score.
    """
    path = _pinned_receipt_path()
    if path is None:
        pytest.skip(f"#439 evidence receipt absent ({PINNED_RECEIPT_REL})")
    assert path is not None
    import json

    block = json.loads(path.read_text(encoding="utf-8"))["execution"]
    # the receipt really is the pinned block, not a shape that merely resembles it
    assert block["slippage_samples"] == 18
    assert block["slippage_bps_mean"] == 0.0
    assert block["prob_slippage"] == 0.0
    assert block["bar_execution"] is True

    out = _breakdown(block)
    assert out["execution_fidelity_source"] == EXECUTION_FIDELITY_DEGENERATE_INERT_CONFIGURATION
    assert out["domains"].get("ExecutionFidelity", {}).get("score") != 100.0
    assert "ExecutionFidelity" not in out["domains"]


def _quad_run(profile: str) -> dict[str, Any]:
    from decimal import Decimal

    from v8_next.adapters.portfolio_backtest import (
        SleeveSpec,
        run_portfolio_backtest,
    )
    from v8_next.evaluation.multitape import load_multitape

    if not (QUAD_TAPE / "tape.jsonl").exists():
        pytest.skip(f"quad tape absent at {QUAD_TAPE}")
    tape = load_multitape(QUAD_TAPE, limit=QUAD_BARS)
    return run_portfolio_backtest(
        tape.candles,
        (SleeveSpec("incumbent", 1, 28, 1.0),),
        tape.funding,
        per_leg_notional=Decimal("1000"),
        taker_fee=Decimal("0.0005"),
        initial_balance=Decimal("10000"),
        funding_dropped=tape.funding_dropped,
        execution_profile=profile,
    )


def _realized_pnl(run: dict[str, Any]) -> list[float]:
    return [
        float(str(c.get("realized_pnl", "0").split()[0]))
        for c in run.get("closed_positions", [])
        if isinstance(c, dict) and c.get("realized_pnl")
    ]


def _published_fidelity(profile: str) -> float:
    """Run one profile over the real tape and publish its ExecutionFidelity."""
    run = _quad_run(profile)
    block = run["execution"]
    if block["slippage_samples"] == 0:
        pytest.skip(f"{profile}: this window measured no fill shortfall")
    assert EXECUTION_FIDELITY_SHORTFALL_FIELD in block, (
        f"{profile}: the telemetry no longer publishes "
        f"{EXECUTION_FIDELITY_SHORTFALL_FIELD}, the statistic the declared "
        "convention reads; the domain would silently fall back to the signed "
        "mean that cancels between adverse and favourable fills"
    )
    pnl = _realized_pnl(run)
    if not pnl:
        pytest.skip(f"{profile}: this window closed no positions to score against")

    out = compute_capability_breakdown(
        pnl_series=pnl,
        total_bars=QUAD_BARS,
        total_trades=len(run["opened_positions"]),
        abstain_rate=0.0,
        execution=block,
    )
    assert out["execution_fidelity_source"] == "MEASURED_IMPLEMENTATION_SHORTFALL"
    measured = block[EXECUTION_FIDELITY_SHORTFALL_FIELD]
    # the published score must be the declared mapping of THIS block's measured
    # shortfall, not a fixed value that happens to be non-zero
    assert out["execution_fidelity_shortfall_bps"] == pytest.approx(measured)
    expected = max(0.0, 1.0 - measured / EXECUTION_FIDELITY_REFERENCE_BPS) * 100.0
    assert out["domains"]["ExecutionFidelity"]["score"] == pytest.approx(
        round(expected, 1)
    )
    return float(out["domains"]["ExecutionFidelity"]["score"])


def test_real_tape_degenerate_baseline_is_not_published_as_execution_evidence() -> None:
    """#439 on the real tape: the no-slippage profile publishes no measurement.

    ``baseline`` declares ``prob_slippage=0.0`` with the default fill model, so
    every magnitude it produces over this window is 0.0 by construction. The
    domain must abstain by name instead of publishing the top of its range; the
    published value it replaces was 100.0 from a statistic that cannot vary.
    """
    run = _quad_run("baseline")
    block = run["execution"]
    if block["slippage_samples"] == 0:
        pytest.skip("baseline: this window measured no fill shortfall")
    pnl = _realized_pnl(run)
    if not pnl:
        pytest.skip("baseline: this window closed no positions to score against")

    assert block["slippage_configuration_cannot_slip"] is True, block["profile"]
    assert block[EXECUTION_FIDELITY_SHORTFALL_FIELD] == pytest.approx(0.0)

    out = compute_capability_breakdown(
        pnl_series=pnl,
        total_bars=QUAD_BARS,
        total_trades=len(run["opened_positions"]),
        abstain_rate=0.0,
        execution=block,
    )
    assert out["execution_fidelity_source"] == EXECUTION_FIDELITY_DEGENERATE_INERT_CONFIGURATION
    assert "ExecutionFidelity" not in out["domains"]
    statuses = {item["domain"]: item for item in out["domain_measurement_statuses"]}
    assert statuses["ExecutionFidelity"]["status"] == "ABSTAINED"
    assert statuses["ExecutionFidelity"]["reason"] == (
        EXECUTION_FIDELITY_DEGENERATE_INERT_CONFIGURATION
    )


def test_real_tape_published_fidelity_follows_measured_execution_evidence() -> None:
    """Profiles that model slippage publish their own measured shortfall.

    The two slipping profiles are run over the same 120-bar quad window: each
    published value is the declared mapping of that profile's own measured
    magnitude (asserted in ``_published_fidelity``), so the dimension responds to
    measured execution instead of publishing the constant 0.50 every run used to,
    and the profile that cannot slip abstains rather than scoring the top of the
    range (see the test above).
    """
    scores = {
        profile: _published_fidelity(profile)
        for profile in ("realistic", "volume_aware")
    }
    assert len(set(scores.values())) > 1, scores
    assert 50.0 not in scores.values(), scores

pytestmark = pytest.mark.slow  # #469: tape/engine file, fast loop excludes via -m "not slow"
