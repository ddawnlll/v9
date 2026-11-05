"""M0-01: the baseline manifest is reproducible and fails closed on missing evidence."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from v8_next.system_proving.baseline import (
    BASELINE_FLAGS,
    SCHEMA_VERSION,
    ArtifactStatus,
    BaselineManifest,
    ManifestIntegrityError,
    ProvenanceError,
    ProvenanceUnavailable,
    V9FeatureFlags,
    assert_safe_defaults,
    build_manifest,
    dataset_identity,
    load_manifest,
    manifest_digest_for,
    sha256_file,
    verify_manifest,
    write_manifest,
)

REPO = Path(__file__).resolve().parents[1]
PINNED_SHA = "0b69fc45c197354600256963249ca7b1f19b6bbd"
PINNED_NS = 1_762_291_200_000_000_000
TAPE = "research/tape/btcusdt-1h-2025Q1/tape.jsonl"
UNPROVISIONED = "research/tape/canonical-10-symbol-3m/tape.jsonl"


def _manifest(**overrides: object) -> BaselineManifest:
    kwargs: dict[str, object] = {
        "code_sha": PINNED_SHA,
        "generated_ns": PINNED_NS,
        "authorized_datasets": [TAPE],
        "blocked_datasets": {UNPROVISIONED: "NOT_PROVISIONED_OFFLINE"},
        # Reproducible on a clean checkout of PINNED_SHA: 1089 passed / 71 skipped.
        # The 5-test delta against the original coordinator run is local untracked
        # evidence artifacts (docs/evidence/**), not code; see reports/provenance/M0-01.md.
        "baseline_tests": {"passed": 1089, "skipped": 71, "deselected": 506},
    }
    kwargs.update(overrides)
    return build_manifest(REPO, **kwargs)  # type: ignore[arg-type]


def test_manifest_is_deterministic_and_self_consistent() -> None:
    first = _manifest()
    second = _manifest()
    assert first.to_json() == second.to_json()
    assert first.schema_version == SCHEMA_VERSION
    assert first.manifest_digest == manifest_digest_for(first)
    assert first.flags == BASELINE_FLAGS


def test_json_roundtrip_preserves_identity() -> None:
    manifest = _manifest()
    restored = BaselineManifest.model_validate_json(manifest.to_json())
    assert restored == manifest
    assert restored.manifest_digest == manifest_digest_for(restored)


def test_tampered_body_is_rejected() -> None:
    payload = json.loads(_manifest().to_json())
    payload["code_sha"] = "1" * 40
    with pytest.raises(ValidationError):
        BaselineManifest.model_validate(payload)


def test_tampered_file_is_rejected_on_load(tmp_path: Path) -> None:
    manifest = _manifest()
    path = tmp_path / "baseline.json"
    write_manifest(manifest, path)
    payload = json.loads(path.read_text())
    payload["baseline_tests"]["passed"] = 1
    path.write_text(json.dumps(payload))
    with pytest.raises(ValidationError):
        load_manifest(path)


def test_manifest_integrity_error_is_a_provenance_error() -> None:
    assert issubclass(ManifestIntegrityError, ProvenanceError)


def test_missing_required_artifact_is_unavailable(tmp_path: Path) -> None:
    with pytest.raises(ProvenanceUnavailable):
        build_manifest(tmp_path, code_sha=PINNED_SHA, generated_ns=PINNED_NS)


def test_blocked_dataset_never_fabricates_identity() -> None:
    item = dataset_identity(REPO, "research/tape/does-not-exist.jsonl", reason="ABSENT")
    assert item.status is ArtifactStatus.BLOCKED
    assert item.sha256 is None
    assert item.size_bytes is None
    assert item.reason == "ABSENT"


def test_available_dataset_is_hashed() -> None:
    item = dataset_identity(REPO, TAPE)
    assert item.status is ArtifactStatus.AVAILABLE
    assert item.sha256 == sha256_file(REPO / TAPE)


def test_blocked_dataset_cannot_carry_a_hash() -> None:
    with pytest.raises(ValidationError):
        type(dataset_identity(REPO, "missing.jsonl")).model_validate(
            {
                "path": "missing.jsonl",
                "status": "BLOCKED",
                "sha256": "0" * 64,
                "reason": "ABSENT",
            }
        )


def test_verify_reports_blocked_not_pass_when_evidence_is_absent() -> None:
    manifest = _manifest()
    result = verify_manifest(manifest, REPO)
    assert result.status is ArtifactStatus.BLOCKED
    assert not result.passed
    assert UNPROVISIONED in result.blocked_datasets
    assert result.mismatches == ()


def test_verify_detects_lockfile_drift() -> None:
    manifest = _manifest()
    drifted = manifest.model_copy(
        update={
            "lockfile": manifest.lockfile.model_copy(update={"sha256": "0" * 64}),
        }
    )
    result = verify_manifest(drifted, REPO)
    assert result.status is ArtifactStatus.DRIFT
    assert any("uv.lock" in item for item in result.mismatches)


def test_verify_passes_when_all_evidence_is_present() -> None:
    manifest = _manifest(blocked_datasets={})
    result = verify_manifest(manifest, REPO)
    assert result.status is ArtifactStatus.PASS
    assert result.passed
    assert result.blocked_datasets == ()


def test_verify_detects_dataset_content_drift(tmp_path: Path) -> None:
    dataset = tmp_path / "tape.jsonl"
    dataset.write_text("real fixture\n")
    manifest = build_manifest(REPO, code_sha=PINNED_SHA, generated_ns=PINNED_NS)
    # Re-point the identity at a file we can mutate, keeping the recorded hash.
    recorded = dataset_identity(tmp_path, "tape.jsonl")
    manifest = manifest.model_copy(update={"datasets": (recorded,)})
    dataset.write_text("mutated fixture\n")
    result = verify_manifest(manifest, tmp_path)
    assert result.status is ArtifactStatus.DRIFT


def test_flags_default_off_and_enabled_flags_are_refused() -> None:
    flags = V9FeatureFlags()
    assert not flags.any_enabled()
    assert_safe_defaults(flags)
    with pytest.raises(ProvenanceError):
        assert_safe_defaults(V9FeatureFlags(adaptive_orders=True))
    with pytest.raises(ValidationError):
        _manifest(flags=V9FeatureFlags(decision_policy=True))


def test_committed_manifest_matches_repository() -> None:
    manifest = load_manifest(REPO / "reports/provenance/baseline.json")
    result = verify_manifest(manifest, REPO)
    assert result.mismatches == ()
    assert result.status in {ArtifactStatus.PASS, ArtifactStatus.BLOCKED}
    assert manifest.flags.any_enabled() is False
    assert manifest.baseline_tests["passed"] > 0
