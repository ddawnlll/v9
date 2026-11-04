"""#441 — the readiness economic factor names its source; an absence stays absent.

MECHANICS ONLY: the receipt here is a local arithmetic fixture. It publishes no
economic result and asserts no performance. The assertions are about what the
certificate is allowed to *call* the economic factor — a named producer or a named
absence — and about the factor staying missing (and the readiness index staying
``None``) when nothing produced it, even though the receipt binds measured economic
evidence.
"""

from __future__ import annotations

import inspect

from v8_next.evaluation.benchmark_receipt import (
    BenchmarkReceipt,
    GateState,
    GateVector,
    ScoreEvidence,
)
from v8_next.evaluation.certificate import (
    ECONOMIC_FACTOR_PRODUCER,
    ECONOMIC_FACTOR_UNMEASURED,
    ECONOMIC_FACTOR_UNPRODUCED,
    PolicyCertificate,
)
from v8_next.evaluation.scoring import (
    compute_capability_breakdown,
    compute_capability_score,
)

#: MECHANICS ONLY: fixed determinants, no economic weight is claimed for them. Same
#: shape as the NX08.R2 fixture, so the only factors left missing here are the
#: robustness score and the economic score.
_MEASUREMENT: dict = dict(
    pnl_series=[0.01, -0.02, 0.03, 0.005] * 3, total_bars=60, total_trades=6, abstain_rate=0.2
)
_MEASURED_EVIDENCE = ScoreEvidence.from_breakdown(compute_capability_breakdown(**_MEASUREMENT))
_MEASURED_SCORE = compute_capability_score(**_MEASUREMENT)
assert _MEASURED_SCORE is not None, "the mechanics fixture must measure a number"

_ECONOMIC_DIGEST = "b" * 64
_ECONOMIC_PATH = "/tmp/economic_receipt_mechanics_only_441.json"


def _receipt(**overrides: object) -> BenchmarkReceipt:
    base = dict(
        case_id="NX08-CASE-441",
        policy_id="pol",
        capability_score=_MEASURED_SCORE,
        coverage_factor=_MEASURED_EVIDENCE.coverage_factor,
        gates=GateVector(),
        computed_at_timestamp_ns=1_000,
        score_evidence=_MEASURED_EVIDENCE,
        economic_evidence_digest=_ECONOMIC_DIGEST,
        economic_receipt_path=_ECONOMIC_PATH,
    )
    base.update(overrides)
    return BenchmarkReceipt.create(**base)  # type: ignore[arg-type]


def test_bound_economic_evidence_does_not_produce_the_readiness_factor() -> None:
    """The defect: a receipt that binds economic evidence still has no producer."""
    receipt = _receipt()
    ok, message = receipt.verify()
    assert ok is True, message
    # the receipt really carries the bound economic pair (not an empty one)
    assert receipt.economic_evidence_digest == _ECONOMIC_DIGEST
    assert receipt.economic_receipt_path == _ECONOMIC_PATH

    certificate = PolicyCertificate.generate(receipt)

    # no number is fabricated for the factor, so the headline stays missing
    assert certificate.economic_score is None
    assert certificate.readiness_index is None
    assert certificate.readiness_upper_bound is None
    assert set(certificate.missing_measurements) == {
        "minerva_robustness_score",
        "economic_score",
    }

    derivation = certificate.derivation
    assert derivation is not None
    assert "economic_score" in derivation["missing_measurements"]
    assert derivation["raw_measurements"]["projection_economic_score"] is None
    # ... and the absence is named: "unimplemented", not a bare hole a reader would
    # take for an ordinary pending measurement
    assert derivation["economic_factor_source"] == "ECONOMIC_FACTOR_UNPRODUCED"
    assert derivation["economic_factor_source"] == ECONOMIC_FACTOR_UNPRODUCED
    # the bound pair is not consumed as a number anywhere in the derivation
    economic_measurements = {
        name: value
        for name, value in derivation["raw_measurements"].items()
        if "economic" in name
    }
    assert economic_measurements == {"projection_economic_score": None}


def test_economic_factor_source_discriminates_unproduced_from_unmeasured() -> None:
    """One producer exists (``projection``); naming it is not naming a measurement."""

    class _EmptyProjection:
        economic_score = None

    class _MeasuredProjection:
        economic_score = 50.0

    unproduced = PolicyCertificate.generate(_receipt())
    unmeasured = PolicyCertificate.generate(_receipt(), projection=_EmptyProjection())
    produced = PolicyCertificate.generate(_receipt(), projection=_MeasuredProjection())

    assert unproduced.derivation["economic_factor_source"] == ECONOMIC_FACTOR_UNPRODUCED
    assert unmeasured.derivation["economic_factor_source"] == ECONOMIC_FACTOR_UNMEASURED
    assert produced.derivation["economic_factor_source"] == ECONOMIC_FACTOR_PRODUCER
    assert ECONOMIC_FACTOR_PRODUCER == "projection.economic_score"
    # three distinct tokens: a reader never has to parse free text
    assert len({ECONOMIC_FACTOR_UNPRODUCED, ECONOMIC_FACTOR_UNMEASURED, ECONOMIC_FACTOR_PRODUCER}) == 3

    # an absent factor is still absent on every leg above
    assert unmeasured.economic_score is None
    assert unmeasured.readiness_index is None
    assert unmeasured.derivation["raw_measurements"]["projection_economic_score"] is None

    # the produced leg reports the caller's number as a *source*, and still publishes
    # no readiness index, because the robustness factor is absent in this fixture
    assert produced.economic_score == 50.0
    assert produced.readiness_index is None
    assert "minerva_robustness_score" in produced.missing_measurements
    assert "economic_score" not in produced.missing_measurements


def test_generate_signature_and_contract_are_unchanged_for_existing_call_sites() -> None:
    """The production call sites pass one argument; this fix adds no parameter."""
    parameters = inspect.signature(PolicyCertificate.generate).parameters
    assert "projection" in parameters
    assert parameters["projection"].default is None
    assert parameters["minerva"].default is None

    certificate = PolicyCertificate.generate(_receipt())
    assert certificate.economic_score is None
    # the derivation contract revision is not bumped: no consumer identity changes
    assert certificate.derivation["transform_version"] == "readiness-v2-missing-aware"


def test_render_names_the_source_without_minting_a_number() -> None:
    certificate = PolicyCertificate.generate(_receipt())
    rendered = certificate.render_ascii()
    assert "READINESS INDEX: MISSING / 100" in rendered
    assert "Economic factor source: ECONOMIC_FACTOR_UNPRODUCED" in rendered
    assert "NO_ECONOMIC_CLAIM" in certificate.authority_verdict
    assert "NO_ECONOMIC_CLAIM" in certificate.status

    derivation = certificate.derivation
    assert derivation is not None
    # the gate vector the receipt carried travels unchanged
    gate_vector = derivation["binding"]["gate_vector"]
    assert gate_vector["g0_identity"] == GateState.MISSING.value
    assert set(gate_vector.values()) == {GateState.MISSING.value}
