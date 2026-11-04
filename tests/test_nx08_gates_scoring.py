"""NX08 (#429) — canonical gate map, derived coverage, no certificate defaults.

Evidence classes:

* **mechanics** — the G0..G9 registry is derived from one source and matches the
  live GateVector/resolvers; coverage counts measured eligible domains; a missing
  measurement is ``None``/MISSING rather than a number; both scorer versions are
  reported side by side with a transform-only delta; and the removed false-success
  paths (a default tape for G5, unequal G6 windows, a G7 "prospective" run over the
  run's own tail) stay removed.

No test here requires a higher score, a PASS, or a readiness index: the assertions
are about what the artifacts are allowed to claim.
"""

from __future__ import annotations

import pytest

from v8_next.evaluation.benchmark_receipt import (
    GATE_DESCRIPTORS,
    BenchmarkReceipt,
    GateState,
    GateVector,
    ScoreEvidence,
)
from v8_next.evaluation.certificate import PolicyCertificate
from v8_next.evaluation.gate_registry import (
    READINESS_ROLES,
    gate_registry,
    registry_by_field,
    validate_registry,
)
from v8_next.evaluation.gate_resolution import (
    evaluate_g5_selection_control,
    evaluate_g6_frozen_oos,
    evaluate_g7_prospective_shadow,
)
from v8_next.evaluation.scoring import (
    LEGACY_FIXED_COVERAGE_FACTOR,
    compute_capability_breakdown,
    compute_capability_score,
    derive_coverage,
    domain_measurement_statuses,
    dual_scoring,
)
from v8_next.evaluation.statistics_plan import CANONICAL_G5_BLOCK_SIZE, g5_plan

#: MECHANICS ONLY: fixed determinants for the receipts in this file. The published
#: number is *derived* from them (#408): a hand-set score its own evidence cannot
#: produce is no longer constructible, so a fixture that declares a number binds the
#: evidence the number comes from. No economic weight is claimed for these numbers.
_MEASUREMENT: dict = dict(
    pnl_series=[0.01, -0.02, 0.03, 0.005] * 3, total_bars=60, total_trades=6, abstain_rate=0.2
)
_MEASURED_EVIDENCE = ScoreEvidence.from_breakdown(compute_capability_breakdown(**_MEASUREMENT))
_MEASURED_SCORE = compute_capability_score(**_MEASUREMENT)
assert _MEASURED_SCORE is not None, "the mechanics fixture must measure a number"


def _plan():
    return g5_plan(
        family="nx08-unit", pinned_ns=1, block_size=CANONICAL_G5_BLOCK_SIZE, reps=199, seed=3
    )


# --------------------------------------------------------------------------- #
# R3 — one canonical gate map
# --------------------------------------------------------------------------- #


def test_gate_registry_is_consistent_with_the_live_objects() -> None:
    assert validate_registry() == []
    registry = gate_registry()
    assert len(registry) == len(GATE_DESCRIPTORS) == len(GateVector.model_fields) == 10
    assert tuple(entry.vector_field for entry in registry) == tuple(GateVector.model_fields)
    assert tuple(entry.index for entry in registry) == tuple(range(10))
    assert {entry.canonical_id for entry in registry} == {
        d.canonical_id for d in GATE_DESCRIPTORS
    }
    for entry in registry:
        assert entry.readiness_role in READINESS_ROLES
        assert entry.requirement in ("Required", "RequiredBlocking", "Optional")
        assert entry.source_clause.strip()
        assert registry_by_field()[entry.vector_field] is not None


def test_gate_registry_pins_the_existing_semantics() -> None:
    """The operational meaning is fixed here; an undocumented change fails loudly."""
    expected = [
        (0, "G0ConstitutionalIntegrity", "g0_identity", "BLOCKING"),
        (1, "G1MeasurementIdentity", "g1_causal_pit", "BLOCKING"),
        (2, "G2HistoricalDiagnostic", "g2_determinism_ledger", "DIAGNOSTIC"),
        (3, "G3ScenarioRobustness", "g3_scenario_robustness", "DIAGNOSTIC"),
        (4, "G4SyntheticFalsification", "g4_structural_robustness", "DIAGNOSTIC"),
        (5, "G5SelectionControl", "g5_statistical_credibility", "DIAGNOSTIC"),
        (6, "G6FrozenOOSReplication", "g6_protected_oos", "DIAGNOSTIC"),
        (7, "G7ProspectiveShadow", "g7_generalization", "DIAGNOSTIC"),
        (8, "G8LiveRealization", "g8_prospective_shadow", "CAPITAL"),
        (9, "G9Certificate", "g9_live_realization", "CAPITAL"),
    ]
    assert [
        (e.index, e.canonical_id, e.vector_field, e.readiness_role) for e in gate_registry()
    ] == expected
    assert [e.requirement for e in gate_registry()][:2] == ["RequiredBlocking", "RequiredBlocking"]


# --------------------------------------------------------------------------- #
# R1 — coverage derived from real measurements
# --------------------------------------------------------------------------- #


def test_coverage_is_derived_and_names_inactive_and_missing_domains() -> None:
    statuses = domain_measurement_statuses(total_bars=500, total_trades=7, abstain_rate=0.1)
    coverage = derive_coverage(statuses)
    assert coverage["coverage_source"] == "DERIVED_FROM_MEASURED_DOMAINS"
    assert coverage["numerator"] == 4  # the four measurable domains
    assert coverage["denominator"] == 4
    assert coverage["coverage_factor"] == pytest.approx(1.0)
    assert coverage["statuses"]["RegimeRobustness"] == "INACTIVE"
    assert coverage["missing_domains"] == []
    assert "unmeasured" in coverage["basis"]
    # the domains this path cannot measure are named, and the share of ALL
    # declared domains that were measured is reported too
    assert coverage["inactive_domains"] == [
        "RegimeRobustness",
        "CrossAssetGeneralization",
        "StatisticalCredibility",
        "EvaluationSafety",
        "CapacityScalability",
        "RepresentationStability",
    ]
    assert coverage["all_domain_coverage_factor"] == pytest.approx(0.4)
    assert coverage["inactive_fraction"] == pytest.approx(0.6)

    # a run that took no trades: the trade domains abstained, the bar domains measured
    abstained = derive_coverage(
        domain_measurement_statuses(total_bars=500, total_trades=0, abstain_rate=1.0)
    )
    assert abstained["statuses"]["ExecutionFidelity"] == "ABSTAINED"
    assert abstained["statuses"]["OperationalSimplicity"] == "MEASURED"
    assert abstained["numerator"] == 2 and abstained["denominator"] == 4
    assert abstained["coverage_factor"] == pytest.approx(0.5)

    # nothing measured at all: no coverage factor exists
    empty = derive_coverage(domain_measurement_statuses(total_bars=0, total_trades=0, abstain_rate=0.0))
    assert empty["coverage_factor"] is None
    assert empty["denominator"] == 0
    assert len(empty["missing_domains"]) == 4


def test_unmeasured_domain_is_never_a_zero_and_never_a_pass() -> None:
    breakdown = compute_capability_breakdown([], 300, 0, 0.0)
    assert breakdown["aggregate"] is None
    assert breakdown["aggregate_status"] == "MISSING_NO_TRADES"
    assert breakdown["domains"] == {}
    assert compute_capability_score([], 300, 0, 0.0) is None
    # a caller may still supply the legacy convention, and then it is labelled
    legacy = compute_capability_breakdown(
        [0.01, -0.02, 0.03] * 5, 40, 4, 0.0, coverage_factor=LEGACY_FIXED_COVERAGE_FACTOR
    )
    assert legacy["coverage_source"] == "CALLER_SUPPLIED"
    assert legacy["aggregate"] is not None


# --------------------------------------------------------------------------- #
# R5 — both scorer versions, side by side, and no target score
# --------------------------------------------------------------------------- #


def test_dual_scoring_separates_transform_from_measurement() -> None:
    record = dual_scoring([0.01, -0.02, 0.03, 0.005] * 10, 60, 6, 0.2)
    versions = record["scoring_versions"]
    assert set(versions) == {"legacy_fixed_coverage_v1", "derived_coverage_v1"}
    assert versions["legacy_fixed_coverage_v1"]["coverage_factor"] == pytest.approx(
        LEGACY_FIXED_COVERAGE_FACTOR
    )
    assert versions["derived_coverage_v1"]["coverage_source"] == "DERIVED_FROM_MEASURED_DOMAINS"
    assert record["delta_kind"] == "TRANSFORM_ONLY"
    assert record["delta"] == pytest.approx(
        versions["derived_coverage_v1"]["aggregate"]
        - versions["legacy_fixed_coverage_v1"]["aggregate"]
    )
    assert record["measurement_changed"] is False
    assert record["claim_status"] == "NO_ECONOMIC_CLAIM"
    # identical inputs must give an identical measurement identity
    assert record["measurement_identity"] == dual_scoring(
        [0.01, -0.02, 0.03, 0.005] * 10, 60, 6, 0.2
    )["measurement_identity"]
    assert not any("PASS" in str(value) for value in versions.values())


# --------------------------------------------------------------------------- #
# R2 — no certificate defaults; missing stays missing
# --------------------------------------------------------------------------- #


def _receipt(**overrides: object) -> BenchmarkReceipt:
    base = dict(
        case_id="NX08-CASE",
        policy_id="pol",
        capability_score=_MEASURED_SCORE,
        coverage_factor=_MEASURED_EVIDENCE.coverage_factor,
        gates=GateVector(),
        computed_at_timestamp_ns=1_000,
        score_evidence=_MEASURED_EVIDENCE,
    )
    base.update(overrides)
    return BenchmarkReceipt.create(**base)  # type: ignore[arg-type]


def test_certificate_has_no_fabricated_robustness_or_economic_defaults() -> None:
    certificate = PolicyCertificate.generate(_receipt())
    assert certificate.minerva_robustness_score is None
    assert certificate.economic_score is None
    assert certificate.readiness_index is None
    assert certificate.readiness_upper_bound is None
    assert set(certificate.missing_measurements) == {
        "minerva_robustness_score",
        "economic_score",
    }
    assert certificate.robustness_seal_status == "SEAL_DENIED_NO_MINERVA_RUN"
    assert "MISSING" in certificate.render_ascii()
    derivation = certificate.derivation
    assert derivation is not None
    assert derivation["raw_measurements"]["minerva_effective_score"] is None
    assert derivation["raw_measurements"]["projection_economic_score"] is None
    assert derivation["denominator"] == 100.0**2
    assert derivation["binding"]["gate_vector"]["g0_identity"] == GateState.MISSING.value


def test_certificate_readiness_is_derived_from_the_formula_when_measured() -> None:
    class _Minerva:
        effective_score = 80.0
        seal_status = "SEAL_GRANTED"

    class _Projection:
        economic_score = 50.0

    certificate = PolicyCertificate.generate(
        _receipt(), projection=_Projection(), minerva=_Minerva()
    )
    assert certificate.readiness_index is not None
    # the same formula, on the coverage the receipt really measured and on the
    # capability number its own bound evidence produced (#408)
    expected = (
        (_MEASURED_SCORE / 100.0)
        * float(_MEASURED_EVIDENCE.coverage_factor)
        * (80.0 / 100.0)
        * (50.0 / 100.0)
        * 100.0
    )
    assert certificate.readiness_index == pytest.approx(round(expected, 1))
    assert certificate.missing_measurements == ()
    # upper bound comes from the same formula with capability at its ceiling
    assert certificate.readiness_upper_bound is not None
    assert certificate.readiness_upper_bound >= certificate.readiness_index


def test_receipt_carries_the_scorer_versions_but_not_in_the_digest() -> None:
    receipt = _receipt(scoring_versions=dual_scoring([0.01, -0.02] * 20, 40, 4, 0.0))
    assert "derived_coverage_v1" in receipt.scoring_versions["scoring_versions"]
    ok, message = receipt.verify()
    assert ok is True, message
    # the same receipt without the sidecar hashes identically: the sidecar is
    # informational and existing receipt digests keep verifying
    plain = _receipt()
    assert plain.receipt_digest == receipt.receipt_digest


# --------------------------------------------------------------------------- #
# R4 — the removed false-success paths stay removed
# --------------------------------------------------------------------------- #


def test_g5_regime_fallback_has_no_default_tape() -> None:
    with pytest.raises(ValueError, match="no default tape"):
        evaluate_g5_selection_control(
            [0.01, 0.02, 0.03],
            None,
            plan=_plan(),
            allow_regime_fallback=True,
            fallback_basis="declared",
        )


@pytest.fixture
def real_candles() -> list:
    """A real tape slice: these gates are measured, never synthetic."""
    from v8_next.evaluation.gate_resolution import DEFAULT_TAPE_PATH, load_tape_candles

    if not DEFAULT_TAPE_PATH.exists():
        pytest.skip(f"real tape absent at {DEFAULT_TAPE_PATH}")
    return load_tape_candles(DEFAULT_TAPE_PATH, limit=150)


def test_g6_compares_equal_length_windows(real_candles: list) -> None:
    candles = real_candles
    state, metrics = evaluate_g6_frozen_oos(candles)
    assert metrics["window_equality"] == "EQUAL_BARS"
    assert metrics["is_bars"] == metrics["oos_bars"] == metrics["comparison_window_bars"]
    assert metrics["is_bars_total"] >= metrics["comparison_window_bars"]
    assert metrics["oos_bars_total"] >= metrics["comparison_window_bars"]
    assert state in (GateState.PASS, GateState.BLOCKED)
    if state == GateState.BLOCKED:
        assert metrics["reason"]


def test_g7_cannot_mint_a_prospective_state_from_the_historical_tail(
    real_candles: list,
) -> None:
    state, metrics = evaluate_g7_prospective_shadow(real_candles)
    assert state == GateState.UNKNOWN
    assert metrics["passed"] is False
    assert metrics["reason"] == "PSEUDO_PROSPECTIVE_HISTORICAL_WINDOW_NOT_ACCEPTED"
    assert metrics["provenance_status"] == "NO_DECLARED_STREAM"
    assert metrics["historical_bars_available"] == len(real_candles)
    # an explicitly declared stream is accepted only with a named origin, and its
    # provenance is recorded as caller-declared rather than gate-verified
    with pytest.raises(ValueError, match="named origin"):
        evaluate_g7_prospective_shadow(
            real_candles, shadow_stream=real_candles[-100:]
        )

pytestmark = pytest.mark.slow  # #469: tape/engine file, fast loop excludes via -m "not slow"
