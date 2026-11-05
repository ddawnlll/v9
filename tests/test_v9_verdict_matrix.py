"""M4-06 (#34): the promotion matrix is fail-closed and never self-approves.

Every test here is about a refusal: the worst dimension wins, missing evidence is
never a pass, a digest mismatch blocks, economic PASS needs all three registered
inputs, and no code path can set ``live_approved`` to ``True``.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import pytest

from v8_next.evaluation import verdict as verdict_module
from v8_next.evaluation.verdict import (
    DimensionStatus,
    DimensionVerdict,
    EvidenceRef,
    PromotionDimension,
    PromotionMatrix,
    build_matrix,
    collect_from_artifacts,
    evidence_ref,
    render_markdown,
    verify_evidence_refs,
    worst_status,
)

#: Independent severity order. Deliberately duplicated so the table test is not
#: a restatement of the implementation.
SEVERITY_ORDER = (
    DimensionStatus.PASS,
    DimensionStatus.NO_EDGE,
    DimensionStatus.INCONCLUSIVE,
    DimensionStatus.FAIL,
    DimensionStatus.NOT_RUN,
    DimensionStatus.BLOCKED,
)

ALL_STATUSES = tuple(SEVERITY_ORDER)

COSTS: dict[str, object] = {
    "fee_bps_per_side": 2,
    "funding_bps_per_day": 1,
    "slippage_bps_per_side": 1.5,
    "funding_included": True,
}


def _verdict(
    dimension: PromotionDimension,
    status: DimensionStatus,
    notes: tuple[str, ...] = (),
    evidence: tuple[EvidenceRef, ...] = (),
) -> DimensionVerdict:
    return DimensionVerdict(
        dimension=dimension,
        status=status,
        evidence=evidence,
        notes=notes or (("synthetic evidence",) if status is DimensionStatus.PASS else ()),
    )


def _matrix(
    *,
    mechanical: DimensionStatus = DimensionStatus.PASS,
    pit: DimensionStatus = DimensionStatus.PASS,
    economic: DimensionStatus = DimensionStatus.PASS,
    resource: DimensionStatus = DimensionStatus.PASS,
) -> PromotionMatrix:
    return build_matrix(
        mechanical=_verdict(PromotionDimension.MECHANICAL, mechanical),
        pit=_verdict(PromotionDimension.PIT, pit),
        economic=_verdict(PromotionDimension.ECONOMIC, economic),
        resource=_verdict(PromotionDimension.RESOURCE, resource),
        live_approval=_verdict(
            PromotionDimension.LIVE_APPROVAL,
            DimensionStatus.BLOCKED,
            notes=("blocked prerequisite: owner authorization",),
        ),
    )


def _expected_worst(left: DimensionStatus, right: DimensionStatus) -> DimensionStatus:
    return SEVERITY_ORDER[max(SEVERITY_ORDER.index(left), SEVERITY_ORDER.index(right))]


@pytest.mark.parametrize("left", ALL_STATUSES)
@pytest.mark.parametrize("right", ALL_STATUSES)
def test_worst_dimension_over_all_pairs(left: DimensionStatus, right: DimensionStatus) -> None:
    expected = _expected_worst(left, right)
    assert worst_status([left, right]) is expected
    assert worst_status([right, left]) is expected

    matrix = _matrix(mechanical=left, pit=right)
    assert matrix.scientific_verdict is expected
    # LIVE_APPROVAL is always BLOCKED, so the five-dimension verdict is BLOCKED.
    assert matrix.overall_verdict is DimensionStatus.BLOCKED


def test_worst_over_three_and_empty() -> None:
    assert worst_status([]) is DimensionStatus.PASS
    assert (
        worst_status(
            [DimensionStatus.PASS, DimensionStatus.FAIL, DimensionStatus.INCONCLUSIVE]
        )
        is DimensionStatus.FAIL
    )


def test_overall_is_worst_of_all_five() -> None:
    matrix = _matrix(
        mechanical=DimensionStatus.PASS,
        pit=DimensionStatus.INCONCLUSIVE,
        economic=DimensionStatus.NOT_RUN,
        resource=DimensionStatus.FAIL,
    )
    assert matrix.overall_verdict is DimensionStatus.BLOCKED
    assert matrix.scientific_verdict is DimensionStatus.NOT_RUN


@pytest.mark.parametrize("status", ALL_STATUSES)
def test_live_approval_must_be_blocked(status: DimensionStatus) -> None:
    if status is DimensionStatus.BLOCKED:
        return
    with pytest.raises(ValueError):
        build_matrix(
            mechanical=_verdict(PromotionDimension.MECHANICAL, DimensionStatus.PASS),
            pit=_verdict(PromotionDimension.PIT, DimensionStatus.PASS),
            economic=_verdict(PromotionDimension.ECONOMIC, DimensionStatus.PASS),
            resource=_verdict(PromotionDimension.RESOURCE, DimensionStatus.PASS),
            live_approval=_verdict(PromotionDimension.LIVE_APPROVAL, status),
        )


def test_live_approved_is_never_true_by_construction() -> None:
    assert "live_approved" not in PromotionMatrix.model_fields
    for status in ALL_STATUSES:
        matrix = _matrix(
            mechanical=status,
            pit=status,
            economic=status,
            resource=status,
        )
        assert matrix.live_approved is False
        assert matrix.requires_owner_authorization is True


def test_module_has_no_assignment_that_sets_live_approved_true() -> None:
    source_path = Path(verdict_module.__file__)
    source = source_path.read_text(encoding="utf-8")
    # Matches ``live_approved = True`` and ``live_approved: bool = True``.
    assert re.search(r"live_approved\s*(?::[^=\n]+)?=\s*True", source) is None
    # The only surface is the read-only property returning the literal False.
    assert "def live_approved(self) -> bool:" in source


def test_empty_repository_never_passes(tmp_path: Path) -> None:
    matrix = collect_from_artifacts(tmp_path)
    assert matrix.mechanical.status is DimensionStatus.BLOCKED
    assert matrix.pit.status is DimensionStatus.BLOCKED
    assert matrix.economic.status is DimensionStatus.NOT_RUN
    assert matrix.resource.status is DimensionStatus.NOT_RUN
    assert matrix.live_approval.status is DimensionStatus.BLOCKED
    for verdict in matrix.dimensions:
        assert verdict.status is not DimensionStatus.PASS
    assert matrix.overall_verdict is DimensionStatus.BLOCKED
    assert matrix.live_approved is False


def test_partial_mechanical_evidence_is_blocked(tmp_path: Path) -> None:
    evidence_dir = tmp_path / "reports/overnight"
    evidence_dir.mkdir(parents=True)
    (evidence_dir / "test-results.jsonl").write_text(
        json.dumps({"wave": 0, "passed": 10}) + "\n", encoding="utf-8"
    )
    provenance = tmp_path / "reports/provenance"
    provenance.mkdir(parents=True)
    (provenance / "baseline.json").write_text(
        json.dumps({"code_sha": "abcdef0", "baseline_tests": {"passed": 10}}),
        encoding="utf-8",
    )
    # M0-02 / M0-03 lineage evidence is intentionally absent.
    matrix = collect_from_artifacts(tmp_path)
    assert matrix.mechanical.status is DimensionStatus.BLOCKED
    assert any("M0-02" in note for note in matrix.mechanical.notes)


def test_digest_mismatch_blocks_the_dimension(tmp_path: Path) -> None:
    artifact = tmp_path / "reports/evidence.md"
    artifact.parent.mkdir(parents=True)
    artifact.write_text("original evidence\n", encoding="utf-8")
    ref = evidence_ref(tmp_path, "reports/evidence.md")
    matrix = build_matrix(
        mechanical=_verdict(
            PromotionDimension.MECHANICAL,
            DimensionStatus.PASS,
            evidence=(ref,),
        ),
        pit=_verdict(PromotionDimension.PIT, DimensionStatus.PASS),
        economic=_verdict(PromotionDimension.ECONOMIC, DimensionStatus.NOT_RUN),
        resource=_verdict(PromotionDimension.RESOURCE, DimensionStatus.NOT_RUN),
        live_approval=_verdict(
            PromotionDimension.LIVE_APPROVAL, DimensionStatus.BLOCKED
        ),
    )
    assert matrix.verify(tmp_path).mechanical.status is DimensionStatus.PASS

    artifact.write_text("tampered evidence\n", encoding="utf-8")
    problems = verify_evidence_refs(tmp_path, (ref,))
    assert problems and "sha256 mismatch" in problems[0]
    blocked = matrix.verify(tmp_path)
    assert blocked.mechanical.status is DimensionStatus.BLOCKED
    assert any("sha256 mismatch" in note for note in blocked.mechanical.notes)


def test_missing_evidence_file_blocks_the_dimension(tmp_path: Path) -> None:
    ref = EvidenceRef(path="reports/absent.md", sha256="0" * 64)
    matrix = _matrix(mechanical=DimensionStatus.PASS).model_copy(
        update={
            "mechanical": _verdict(
                PromotionDimension.MECHANICAL,
                DimensionStatus.PASS,
                evidence=(ref,),
            )
        }
    )
    blocked = matrix.verify(tmp_path)
    assert blocked.mechanical.status is DimensionStatus.BLOCKED
    assert any("missing" in note for note in blocked.mechanical.notes)


def _write_economic_bundle(
    root: Path,
    *,
    prereg: bool = True,
    access: bool = True,
    costed: bool = True,
    role: str = "DEVELOPMENT",
    permitted: list[str] | None = None,
    sealed_authorized: bool = False,
    verdict: str = "PASS",
    costs: dict[str, object] | None = None,
    digest: str | None = None,
    net_return: str = "0.012",
    trades: object = 42,
    uncertainty: object = None,
    prereg_extra: dict[str, object] | None = None,
) -> str:
    directory = root / "reports/evaluation"
    directory.mkdir(parents=True, exist_ok=True)
    prereg_path = directory / "preregistration.json"
    if prereg:
        registration: dict[str, object] = {
            "hypothesis_id": "H-1",
            "dataset_role": role,
            "costs": costs or COSTS,
            "minimum_trades": 30,
            "accepted_outcomes": ["NO_EDGE", "INCONCLUSIVE", "STOP", "INVALID"],
            "decision_rule": ("PASS only when the Holm-corrected interval excludes zero."),
            "hypothesis_budget": {
                "family": "v9-test-family",
                "max_hypotheses": 4,
                "max_trials": 4,
                "alpha": "0.05",
                "correction": "HOLM",
            },
        }
        if prereg_extra is not None:
            registration.update(prereg_extra)
        prereg_path.write_text(json.dumps(registration), encoding="utf-8")
    digest_value = digest or (
        hashlib.sha256(prereg_path.read_bytes()).hexdigest() if prereg else "0" * 64
    )
    if access:
        (directory / "access_report.json").write_text(
            json.dumps(
                {
                    "permitted_roles": permitted if permitted is not None else [role],
                    "sealed_authorized": sealed_authorized,
                }
            ),
            encoding="utf-8",
        )
    if costed:
        if uncertainty is None:
            uncertainty = {
                "ci_lower": "0.004",
                "ci_upper": "0.028",
                "family_size": 4,
                "correction": "HOLM",
                "p_value": "0.002",
                "ci_alpha": "0.0125",
            }
        (directory / "costed_result.json").write_text(
            json.dumps(
                {
                    "preregistration_digest": digest_value,
                    "dataset_role": role,
                    "costs": costs or COSTS,
                    "net_return": net_return,
                    "trades": trades,
                    "verdict": verdict,
                    "uncertainty": uncertainty,
                }
            ),
            encoding="utf-8",
        )
    return digest_value


def test_economic_pass_requires_all_three_artifacts(tmp_path: Path) -> None:
    _write_economic_bundle(tmp_path)
    matrix = collect_from_artifacts(tmp_path)
    assert matrix.economic.status is DimensionStatus.PASS
    assert len(matrix.economic.evidence) == 3
    assert any("H-1" in note for note in matrix.economic.notes)


@pytest.mark.parametrize("missing", ["prereg", "access", "costed"])
def test_economic_pass_impossible_when_one_artifact_is_missing(
    tmp_path: Path, missing: str
) -> None:
    kwargs: dict[str, bool] = {"prereg": True, "access": True, "costed": True}
    kwargs[missing] = False
    _write_economic_bundle(tmp_path, **kwargs)
    matrix = collect_from_artifacts(tmp_path)
    assert matrix.economic.status is DimensionStatus.NOT_RUN
    assert matrix.economic.status is not DimensionStatus.PASS


def test_economic_role_must_be_permitted(tmp_path: Path) -> None:
    _write_economic_bundle(tmp_path, permitted=["VALIDATION"])
    matrix = collect_from_artifacts(tmp_path)
    assert matrix.economic.status is DimensionStatus.BLOCKED
    assert any("not permitted" in note for note in matrix.economic.notes)


def test_economic_sealed_role_needs_authorization(tmp_path: Path) -> None:
    _write_economic_bundle(tmp_path, role="SEALED_OOS", sealed_authorized=False)
    assert collect_from_artifacts(tmp_path).economic.status is DimensionStatus.BLOCKED

    other = tmp_path / "authorized"
    _write_economic_bundle(other, role="SEALED_OOS", sealed_authorized=True)
    assert collect_from_artifacts(other).economic.status is DimensionStatus.PASS


def test_economic_costed_result_must_bind_preregistration(tmp_path: Path) -> None:
    _write_economic_bundle(tmp_path, digest="f" * 64)
    matrix = collect_from_artifacts(tmp_path)
    assert matrix.economic.status is DimensionStatus.BLOCKED
    assert any("different preregistration" in note for note in matrix.economic.notes)


def test_economic_costed_result_must_carry_funding(tmp_path: Path) -> None:
    incomplete = dict(COSTS)
    incomplete["funding_included"] = False
    _write_economic_bundle(tmp_path, costs=incomplete)
    matrix = collect_from_artifacts(tmp_path)
    assert matrix.economic.status is DimensionStatus.BLOCKED
    assert any("funding_included" in note for note in matrix.economic.notes)


def test_economic_negative_outcomes_are_publishable(tmp_path: Path) -> None:
    _write_economic_bundle(tmp_path, verdict="NO_EDGE")
    assert collect_from_artifacts(tmp_path).economic.status is DimensionStatus.NO_EDGE


def test_economic_registered_pass_is_earned_not_declared(tmp_path: Path) -> None:
    _write_economic_bundle(tmp_path, verdict="PASS")
    verdict = collect_from_artifacts(tmp_path).economic
    assert verdict.status is DimensionStatus.PASS
    assert any("earned by the registered decision rule" in note for note in verdict.notes)
    assert any("not preregistered as an accepted outcome" in note for note in verdict.notes)


def test_economic_pass_refused_without_positive_net_return(tmp_path: Path) -> None:
    # Audit repro: accepted_outcomes has no PASS, minimum_trades=30, costed
    # net_return=-0.50 with a declared PASS must not render PASS.
    _write_economic_bundle(tmp_path, verdict="PASS", net_return="-0.50", trades=1)
    verdict = collect_from_artifacts(tmp_path).economic
    assert verdict.status is DimensionStatus.BLOCKED
    joined = " ".join(verdict.notes)
    assert "non-positive costed net_return" in joined
    assert "below the registered minimum" in joined


def test_economic_pass_refused_below_minimum_trades(tmp_path: Path) -> None:
    _write_economic_bundle(tmp_path, verdict="PASS", trades=29)
    verdict = collect_from_artifacts(tmp_path).economic
    assert verdict.status is DimensionStatus.BLOCKED
    assert any("below the registered minimum of 30" in note for note in verdict.notes)


def test_economic_pass_refused_without_integer_trade_count(tmp_path: Path) -> None:
    _write_economic_bundle(tmp_path, verdict="PASS", trades="many")
    verdict = collect_from_artifacts(tmp_path).economic
    assert verdict.status is DimensionStatus.BLOCKED
    assert any("integer trade count" in note for note in verdict.notes)


def test_economic_verdict_must_be_an_accepted_outcome(tmp_path: Path) -> None:
    _write_economic_bundle(
        tmp_path,
        verdict="INCONCLUSIVE",
        prereg_extra={"accepted_outcomes": ["NO_EDGE", "STOP"]},
    )
    verdict = collect_from_artifacts(tmp_path).economic
    assert verdict.status is DimensionStatus.BLOCKED
    assert any("not an accepted outcome" in note for note in verdict.notes)


def test_economic_pass_refused_when_pass_is_preregistered(tmp_path: Path) -> None:
    _write_economic_bundle(
        tmp_path,
        verdict="PASS",
        prereg_extra={"accepted_outcomes": ["NO_EDGE", "INCONCLUSIVE", "STOP", "INVALID", "PASS"]},
    )
    verdict = collect_from_artifacts(tmp_path).economic
    assert verdict.status is DimensionStatus.BLOCKED
    assert any("pre-accepts PASS" in note for note in verdict.notes)


def test_economic_pass_refused_when_ci_includes_zero(tmp_path: Path) -> None:
    _write_economic_bundle(
        tmp_path,
        verdict="PASS",
        uncertainty={
            "ci_lower": "-0.004",
            "ci_upper": "0.028",
            "family_size": 4,
            "correction": "HOLM",
            "p_value": "0.002",
        },
    )
    verdict = collect_from_artifacts(tmp_path).economic
    assert verdict.status is DimensionStatus.BLOCKED
    assert any("does not exclude zero" in note for note in verdict.notes)


def test_economic_pass_refused_without_holm_correction(tmp_path: Path) -> None:
    _write_economic_bundle(
        tmp_path,
        verdict="PASS",
        uncertainty={
            "ci_lower": "0.004",
            "ci_upper": "0.028",
            "family_size": 4,
            "correction": "NONE",
            "p_value": "0.002",
        },
    )
    verdict = collect_from_artifacts(tmp_path).economic
    assert verdict.status is DimensionStatus.BLOCKED
    assert any("does not declare the registered HOLM" in note for note in verdict.notes)


@pytest.mark.parametrize(
    "uncertainty",
    [
        # An uncorrected 95% interval: alpha 0.05 > Holm-adjusted 0.0125.
        {
            "ci_lower": "0.004",
            "ci_upper": "0.028",
            "family_size": 4,
            "correction": "HOLM",
            "ci_alpha": "0.05",
        },
        # A p-value that does not survive Holm over the registered family.
        {
            "ci_lower": "0.004",
            "ci_upper": "0.028",
            "family_size": 4,
            "correction": "HOLM",
            "p_value": "0.030",
        },
        # Family size understated relative to the registration.
        {
            "ci_lower": "0.004",
            "ci_upper": "0.028",
            "family_size": 1,
            "correction": "HOLM",
            "p_value": "0.002",
        },
        # No uncertainty block at all.
        {},
    ],
)
def test_economic_pass_refused_without_corrected_rule(
    tmp_path: Path, uncertainty: dict[str, object]
) -> None:
    _write_economic_bundle(tmp_path, verdict="PASS", uncertainty=uncertainty)
    verdict = collect_from_artifacts(tmp_path).economic
    assert verdict.status is DimensionStatus.BLOCKED
    assert verdict.status is not DimensionStatus.PASS


def test_economic_pass_refused_without_registered_decision_rule(tmp_path: Path) -> None:
    _write_economic_bundle(tmp_path, verdict="PASS", prereg_extra={"hypothesis_budget": None})
    verdict = collect_from_artifacts(tmp_path).economic
    assert verdict.status is DimensionStatus.BLOCKED
    assert any("hypothesis budget" in note for note in verdict.notes)


def test_economic_bundle_bound_to_another_preregistration_is_refused(
    tmp_path: Path,
) -> None:
    # A digest that is a valid 64-hex string but is not the on-disk prereg digest.
    _write_economic_bundle(tmp_path, digest="a" * 64)
    verdict = collect_from_artifacts(tmp_path).economic
    assert verdict.status is DimensionStatus.BLOCKED
    assert any("different preregistration" in note for note in verdict.notes)


def _write_resource_receipt(
    root: Path,
    *,
    within: bool = True,
    scope_status: str = "MEASURED",
    peak: int = 220 * 1024**2,
) -> Path:
    receipt = root / "reports/overnight/evidence/receipts/x-receipt.json"
    receipt.parent.mkdir(parents=True, exist_ok=True)
    receipt.write_text(
        json.dumps(
            {
                "memory": {"peak_rss_bytes": peak, "status": "MEASURED"},
                "scope": {
                    "status": scope_status,
                    "legs": 1,
                    "months": 2.96,
                    "refusal_reason": None
                    if scope_status == "MEASURED"
                    else "SCOPE_BELOW_TARGET",
                },
                "within_rss_target": within,
            }
        ),
        encoding="utf-8",
    )
    return receipt


def test_resource_receipt_is_mapped_honestly(tmp_path: Path) -> None:
    assert collect_from_artifacts(tmp_path).resource.status is DimensionStatus.NOT_RUN

    _write_resource_receipt(tmp_path)
    assert collect_from_artifacts(tmp_path).resource.status is DimensionStatus.PASS

    _write_resource_receipt(tmp_path, scope_status="NOT_RUN")
    assert collect_from_artifacts(tmp_path).resource.status is DimensionStatus.INCONCLUSIVE

    _write_resource_receipt(tmp_path, within=False)
    assert collect_from_artifacts(tmp_path).resource.status is DimensionStatus.FAIL


def test_auxiliary_receipt_without_deployment_scope_is_not_qualification(
    tmp_path: Path,
) -> None:
    receipt = tmp_path / "reports/overnight/evidence/receipts/aux-receipt.json"
    receipt.parent.mkdir(parents=True)
    receipt.write_text(
        json.dumps({"memory": {"peak_rss_bytes": 123, "status": "MEASURED"}}),
        encoding="utf-8",
    )
    matrix = collect_from_artifacts(tmp_path)
    assert matrix.resource.status is DimensionStatus.NOT_RUN
    assert any("auxiliary" in note for note in matrix.resource.notes)


def test_corrupt_optional_test_log_blocks_mechanical(tmp_path: Path) -> None:
    root = tmp_path
    baseline = root / "reports/provenance/baseline.json"
    baseline.parent.mkdir(parents=True)
    baseline.write_text(
        json.dumps({"code_sha": "abcdef0", "baseline_tests": {"passed": 10}}),
        encoding="utf-8",
    )
    for relative in ("reports/overnight/evidence/M0-02.md", "reports/overnight/evidence/M0-03.md"):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("lineage\n", encoding="utf-8")
    (root / "reports/overnight/test-results.jsonl").write_text(
        "{not json}\n", encoding="utf-8"
    )
    matrix = collect_from_artifacts(root)
    assert matrix.mechanical.status is DimensionStatus.BLOCKED
    assert any("unreadable" in note for note in matrix.mechanical.notes)


def _write_mechanical_bundle(root: Path, rows: list[dict[str, object]]) -> None:
    baseline = root / "reports/provenance/baseline.json"
    baseline.parent.mkdir(parents=True, exist_ok=True)
    baseline.write_text(
        json.dumps({"code_sha": "abcdef0", "baseline_tests": {"passed": 10}}),
        encoding="utf-8",
    )
    for relative in (
        "reports/overnight/evidence/M0-02.md",
        "reports/overnight/evidence/M0-03.md",
    ):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("lineage\n", encoding="utf-8")
    (root / "reports/overnight/test-results.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8"
    )


def test_failing_test_log_blocks_mechanical(tmp_path: Path) -> None:
    _write_mechanical_bundle(
        tmp_path,
        [{"wave": 0, "passed": 0, "failed": 2693, "result": "FAILED"}],
    )
    verdict = collect_from_artifacts(tmp_path).mechanical
    assert verdict.status is DimensionStatus.BLOCKED
    joined = " ".join(verdict.notes)
    assert "failed=2693" in joined
    assert "reports/overnight/test-results.jsonl" in joined
    assert any(ref.path == "reports/overnight/test-results.jsonl" for ref in verdict.evidence)


@pytest.mark.parametrize(
    "row",
    [
        {"wave": 1, "passed": 10, "errors": 2},
        {"wave": 2, "passed": 10, "result": "error"},
        {"wave": 3, "passed": 10, "status": "FAILED_IMPORT"},
    ],
)
def test_any_failure_marker_in_the_test_log_blocks_mechanical(
    tmp_path: Path, row: dict[str, object]
) -> None:
    _write_mechanical_bundle(tmp_path, [{"wave": 0, "passed": 2693, "failed": 0}, row])
    assert collect_from_artifacts(tmp_path).mechanical.status is DimensionStatus.BLOCKED


def test_passing_test_log_keeps_mechanical_pass(tmp_path: Path) -> None:
    _write_mechanical_bundle(
        tmp_path,
        [
            {"wave": 0, "passed": 2693, "failed": 0, "result": "PASSED"},
            {"wave": 1, "passed": 2701, "failed": 0, "status": "ok"},
        ],
    )
    verdict = collect_from_artifacts(tmp_path).mechanical
    assert verdict.status is DimensionStatus.PASS
    assert any("recorded test-run rows: 2" in note for note in verdict.notes)


def test_rendered_markdown_lists_every_dimension_and_is_deterministic(tmp_path: Path) -> None:
    matrix = collect_from_artifacts(tmp_path)
    rendered = render_markdown(matrix)
    assert rendered == render_markdown(collect_from_artifacts(tmp_path))
    for dimension in PromotionDimension:
        assert dimension.value in rendered
    for verdict in matrix.dimensions:
        assert verdict.status.value in rendered
    assert "blocked prerequisite:" in rendered
    assert "Live approved: `False`" in rendered
    assert "requires owner authorization: `True`" in rendered
    assert matrix.overall_verdict.value in rendered


def test_rendered_markdown_publishes_evidence_hashes(tmp_path: Path) -> None:
    artifact = tmp_path / "reports/evidence.md"
    artifact.parent.mkdir(parents=True)
    artifact.write_text("evidence\n", encoding="utf-8")
    ref = evidence_ref(tmp_path, "reports/evidence.md")
    matrix = build_matrix(
        mechanical=_verdict(
            PromotionDimension.MECHANICAL,
            DimensionStatus.PASS,
            evidence=(ref,),
        ),
        pit=_verdict(PromotionDimension.PIT, DimensionStatus.PASS),
        economic=_verdict(PromotionDimension.ECONOMIC, DimensionStatus.NOT_RUN),
        resource=_verdict(PromotionDimension.RESOURCE, DimensionStatus.NOT_RUN),
        live_approval=_verdict(
            PromotionDimension.LIVE_APPROVAL, DimensionStatus.BLOCKED
        ),
    )
    rendered = render_markdown(matrix)
    assert "reports/evidence.md" in rendered
    assert ref.sha256 in rendered


def test_repository_collector_reports_the_current_honest_verdict() -> None:
    root = Path(__file__).resolve().parents[1]
    matrix = collect_from_artifacts(root)
    assert matrix.mechanical.status is DimensionStatus.PASS
    assert matrix.pit.status is DimensionStatus.PASS
    assert matrix.live_approval.status is DimensionStatus.BLOCKED
    assert matrix.overall_verdict is DimensionStatus.BLOCKED
    assert matrix.live_approved is False
    assert matrix.requires_owner_authorization is True


def test_published_evidence_hashes_match_disk() -> None:
    """Every hash quoted in M4-06.md must equal the file on disk at this commit."""

    root = Path(__file__).resolve().parents[1]
    evidence_path = root / "reports/overnight/evidence/M4-06.md"
    evidence = evidence_path.read_text(encoding="utf-8")
    checked = 0
    for line in evidence.splitlines():
        quoted = re.findall(r"`([^`]+)`", line)
        paths = [item for item in quoted if (root / item).is_file()]
        hashes = re.findall(r"\b[0-9a-f]{64}\b", line)
        if not hashes:
            continue
        assert len(paths) == len(hashes), f"unpaired evidence hash in line: {line}"
        for relative, expected in zip(paths, hashes, strict=True):
            actual = hashlib.sha256((root / relative).read_bytes()).hexdigest()
            assert actual == expected, f"stale hash quoted for {relative}: {expected}"
            checked += 1
    assert checked >= 15, f"only {checked} evidence hashes checked"
