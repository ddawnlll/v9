import hashlib
import json

import pytest

from v8_next.evaluation import calibration


def test_forged_campaign_checkpoint_rejected_before_accounting(tmp_path, monkeypatch):
    monkeypatch.setattr(calibration, "source_hash", lambda: "fixture-runtime")
    manifest = tmp_path / "manifest.json"
    manifest.write_text("{}")
    (tmp_path / "policy.json").write_text(
        json.dumps({"policy": {"paper_config": {}, "code_and_lock_hash": "fixture-runtime"}})
    )
    (tmp_path / "paper-state.json").write_text(
        json.dumps(
            {
                "policy_hash": "test-policy",
                "manifests": ["manifest.json"],
                "manifest_hashes": [hashlib.sha256(manifest.read_bytes()).hexdigest()],
                "native_state": {"campaigns": [{"campaign_id": "forged"}]},
                "revised_accounting": {"accounting_as_of_ns": 10},
            }
        )
    )
    monkeypatch.setattr(calibration, "evaluate", lambda _: {"policy_hash": "test-policy"})
    monkeypatch.setattr(calibration, "replay_account", lambda *_: {"campaigns": []})

    def forbidden_accounting(*_):
        pytest.fail("forged decisions must not enter accounting replay")

    monkeypatch.setattr(calibration, "replay_frozen_campaigns", forbidden_accounting)
    with pytest.raises(ValueError, match="diverged"):
        calibration.inspect_calibration_source(tmp_path, 20)


@pytest.mark.parametrize("identity", [None, "different-runtime"])
def test_calibration_rejects_missing_or_changed_runtime_before_replay(
    tmp_path, monkeypatch, identity
):
    (tmp_path / "policy.json").write_text(json.dumps({"policy": {"code_and_lock_hash": identity}}))
    monkeypatch.setattr(calibration, "source_hash", lambda: "current-runtime")

    def forbidden(*args):
        pytest.fail("changed runtime must not recompute source evidence")

    monkeypatch.setattr(calibration, "evaluate", forbidden)
    monkeypatch.setattr(calibration, "replay_account", forbidden)
    with pytest.raises(ValueError, match="frozen runtime"):
        calibration.inspect_calibration_source(tmp_path, 20)


def test_open_outcomes_cannot_silently_disappear_from_calibration_sample():
    from v8_next.evaluation.calibration import outcome_sample_blockers

    blockers = outcome_sample_blockers([{"is_closed": True}, {"is_closed": False}], "UNQUALIFIED")
    assert "OPEN_OUTCOME_CENSORING_POLICY_REQUIRED" in blockers
    assert "FUNDING_COVERAGE_UNQUALIFIED" in blockers
    assert "STATISTICAL_METHOD_AND_TRIAL_FAMILY_REVIEW_REQUIRED" in blockers
    assert outcome_sample_blockers([], "COMPLETE")[0] == "NO_EXECUTED_OUTCOME_SAMPLE"
    assert outcome_sample_blockers([{"is_closed": True}], "COMPLETE") == [
        "STATISTICAL_METHOD_AND_TRIAL_FAMILY_REVIEW_REQUIRED"
    ]


def test_recomputed_paper_report_includes_full_selection_estimator(tmp_path, monkeypatch):
    monkeypatch.setattr(calibration, "source_hash", lambda: "fixture-runtime")
    manifest = tmp_path / "manifest.json"
    manifest.write_text("{}")
    (tmp_path / "policy.json").write_text(
        json.dumps({"policy": {"paper_config": {}, "code_and_lock_hash": "fixture-runtime"}})
    )
    native = {"campaigns": []}
    revised = dict(
        accounting_as_of_ns=10,
        positions=[],
        position_closures=[],
        funding_coverage="COMPLETE",
        realization="SIMULATED",
        outcomes=dict(
            rows=[],
            reconciliation="CLOSED_CASH_RECONCILED",
            selection_cash_scorecard={"status": "INCOMPLETE_OR_UNRECONCILED_COHORT"},
        ),
    )
    checkpoint = dict(
        policy_hash="test-policy",
        manifests=["manifest.json"],
        manifest_hashes=[hashlib.sha256(manifest.read_bytes()).hexdigest()],
        native_state=native,
        revised_accounting=revised,
    )
    (tmp_path / "paper-state.json").write_text(json.dumps(checkpoint))
    monkeypatch.setattr(calibration, "evaluate", lambda _: {"policy_hash": "test-policy"})
    monkeypatch.setattr(calibration, "replay_account", lambda *_: native)
    monkeypatch.setattr(calibration, "replay_frozen_campaigns", lambda *_: revised)
    result = calibration.inspect_calibration_source(tmp_path, 20, bootstrap_plan=(1, 99, 7))
    assert result["selection_cash_estimate"]["estimate"] is None
    assert result["selection_cash_estimate"]["reason"] == "COMPLETE_RECONCILED_SELECTION_REQUIRED"
    assert result["accounting_recomputed"] is True
    assert result["eligible_for_utility"] is False


def test_no_exposure_funding_status_does_not_exempt_position_history():
    status = "NOT_APPLICABLE_NO_POSITION_EXPOSURE"
    assert "FUNDING_COVERAGE_UNQUALIFIED" not in calibration.outcome_sample_blockers([], status)
    assert "NO_EXECUTED_OUTCOME_SAMPLE" in calibration.outcome_sample_blockers([], status)
    assert "FUNDING_COVERAGE_UNQUALIFIED" in calibration.outcome_sample_blockers(
        [{"is_closed": True}], status
    )
