import hashlib
import json

import pytest

from v8_next.app.evaluate import evaluate
from v8_next.evaluation.store import ResearchStore, canonical


def test_evaluator_reports_registry_without_claiming_adjustment(tmp_path):
    policy = {"test_only": True}
    digest = hashlib.sha256(canonical(policy).encode()).hexdigest()
    (tmp_path / "policy.json").write_text(json.dumps({"policy": policy, "policy_hash": digest}))
    store = ResearchStore(tmp_path / "research.sqlite")
    try:
        store.register_trial("baseline", "family", digest, "test-data", "DEVELOPMENT", 1)
        store.register_trial("observer", "family", digest, "test-data", "DEVELOPMENT", 1)
        store.burn_holdout("family", "held-out", 2)
        store.record_campaign_observation(
            "campaign", 2, {"campaign_id": "campaign", "realization": "SIMULATED"}
        )
        report = evaluate(tmp_path)
        assert report["research_lineage"]["registered_family_sizes"] == {"family": 2}
        assert report["research_lineage"]["holdout_burns"] == [
            {"lineage": "family", "dataset_hash": "held-out", "burned_ns": 2}
        ]
        assert report["research_lineage"]["multiplicity_adjustment"] is None
        assert report["claim_status"] == "NO_ECONOMIC_CLAIM"
        assert report["campaign_history"]["observations"][0]["campaign_id"] == "campaign"
        store.db.execute("UPDATE campaign_observations SET digest='tampered'")
        with pytest.raises(ValueError, match="campaign observation hash"):
            evaluate(tmp_path)
        store.db.execute("DELETE FROM campaign_observations")
        store.register_trial("foreign", "family", "other-policy", "test-data", "DEVELOPMENT", 3)
        with pytest.raises(ValueError, match="trial belongs"):
            evaluate(tmp_path)
    finally:
        store.close()


def test_protected_holdout_is_checked_before_outcome_read(tmp_path):
    policy = {"test_only": True}
    digest = hashlib.sha256(canonical(policy).encode()).hexdigest()
    (tmp_path / "policy.json").write_text(json.dumps({"policy": policy, "policy_hash": digest}))
    store = ResearchStore(tmp_path / "research.sqlite")
    try:
        store.register_trial("protected", "family", digest, "held-out", "HOLDOUT", 10)
        # Deliberately unreadable outcome: preflight must refuse before decoding it.
        store.db.execute("INSERT INTO decisions VALUES ('x','not-json','wrong-hash')")
        with pytest.raises(ValueError, match="protected holdout"):
            evaluate(tmp_path)
        store.burn_holdout("other-family", "held-out", 20)
        with pytest.raises(ValueError, match="protected holdout"):
            evaluate(tmp_path)
        store.burn_holdout("family", "held-out", 20)
        with pytest.raises(ValueError, match="decision hash mismatch"):
            evaluate(tmp_path)
        store.db.execute("DELETE FROM decisions")
        result = evaluate(tmp_path)
        assert result["promotion"].startswith("BLOCKED")
        assert result["research_lineage"]["multiplicity_adjustment"] is None
    finally:
        store.close()
