"""Burn registration must use the same lineage key as trials and holdouts."""

from types import SimpleNamespace

import pytest

from v8_next.evaluation.store import ResearchStore
from v8_next.evaluation.tape_identity import bind_manifest_to_store


def _calendar() -> SimpleNamespace:
    return SimpleNamespace(
        burn_segments=[
            SimpleNamespace(
                segment_id="seg-early",
                role="BURNED_DIAGNOSTIC",
                data_role="DEVELOPMENT",
                start_ms=100,
            ),
            SimpleNamespace(
                segment_id="seg-late",
                role="BURNED_DIAGNOSTIC",
                data_role="DEVELOPMENT",
                start_ms=200,
            ),
        ]
    )


def _manifest(dataset_hash: str) -> SimpleNamespace:
    return SimpleNamespace(
        data_id=dataset_hash,
        final_eligible=False,
        identity_digest=lambda: f"identity-{dataset_hash}",
    )


def test_bind_manifest_burns_under_lineage_and_holdout_sees_it(tmp_path) -> None:
    store = ResearchStore(tmp_path / "research.sqlite")
    try:
        report = bind_manifest_to_store(
            _manifest("dataset-1"), store, calendar=_calendar(), lineage="family-1"
        )
        burns = store.list_burns("family-1")
        assert [burn.dataset_hash for burn in burns] == ["dataset-1"]
        # one dataset burn, credited at the earliest burned segment
        assert burns[0].burned_ns == 100 * 1_000_000
        assert report["burns_registered"] == ["family-1"]
        assert report["burn_segments"] == ["seg-early", "seg-late"]

        store.register_trial("trial-1", "family-1", "policy", "dataset-1", "HOLDOUT", 10)
        holdout = store.get_holdouts("family-1")[0]
        assert holdout.is_burned is True
        assert holdout.burned_ns == 100 * 1_000_000
        assert store.holdout_pristine("family-1", "dataset-1") is False

        with pytest.raises(ValueError, match="already carries burned dataset"):
            bind_manifest_to_store(
                _manifest("dataset-2"), store, calendar=_calendar(), lineage="family-1"
            )
    finally:
        store.close()
