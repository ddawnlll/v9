import pytest

from v8_next.evaluation.store import ResearchStore


def test_restart_replay_is_idempotent_and_divergence_fails(tmp_path):
    path = tmp_path / "research.sqlite"
    store = ResearchStore(path)
    assert store.record_decision("d", {"reason": "ABSTAIN"})
    store.close()
    store = ResearchStore(path)
    try:
        assert not store.record_decision("d", {"reason": "ABSTAIN"})
        with pytest.raises(ValueError, match="replay"):
            store.record_decision("d", {"reason": "TRADE"})
        assert not store.record_decision("d", {"reason": "ABSTAIN"})
    finally:
        store.close()


def test_holdout_burn_survives_restart_and_new_trial_name(tmp_path):
    path = tmp_path / "research.sqlite"
    store = ResearchStore(path)
    store.register_trial("t1", "family", "policy", "data", "HOLDOUT", 1)
    assert store.holdout_pristine("lineage", "data")
    store.burn_holdout("lineage", "data", 2)
    store.close()
    store = ResearchStore(path)
    try:
        store.register_trial("renamed", "family", "policy", "data", "HOLDOUT", 3)
        assert not store.holdout_pristine("lineage", "data")
        assert store.family_size("family") == 2
        with pytest.raises(ValueError, match="rewritten"):
            store.register_trial("t1", "other", "policy", "data", "HOLDOUT", 1)
    finally:
        store.close()


@pytest.mark.parametrize("terminal", ["REJECTED", "EXPIRED", "INVALIDATED"])
def test_terminal_opportunity_cannot_reopen(tmp_path, terminal):
    store = ResearchStore(tmp_path / "research.sqlite")
    try:
        store.transition("o", "DETECTED", 1, "setup")
        store.transition("o", terminal, 2, "reason")
        with pytest.raises(ValueError, match="terminal"):
            store.transition("o", "PENDING", 3, "resurrection")
    finally:
        store.close()


def test_observation_and_lifecycle_commit_together_without_resurrection(tmp_path):
    store = ResearchStore(tmp_path / "research.sqlite")
    payload = {
        "decision_ns": 10,
        "reason": "REJECTED_MISSING_CALIBRATION",
        "opportunity": {"opportunity_id": "o", "expires_ns": 100},
    }
    try:
        assert store.record_decision("d1", payload)
        assert not store.record_decision("d1", payload)
        assert store.record_decision("d2", {**payload, "decision_ns": 11, "reason": "ABSTAIN"})
        states = store.db.execute("SELECT state FROM lifecycle ORDER BY sequence").fetchall()
        assert states == [("DETECTED",), ("PENDING",), ("REJECTED",)]
        with pytest.raises(ValueError, match="backwards"):
            store.record_decision("out-of-order", {**payload, "decision_ns": 9})
        assert store.db.execute("SELECT COUNT(*) FROM decisions").fetchone()[0] == 2
    finally:
        store.close()


def test_no_signal_observation_expires_pending_opportunity_after_restart(tmp_path):
    path = tmp_path / "research.sqlite"
    store = ResearchStore(path)
    store.record_decision(
        "first",
        {
            "decision_ns": 10,
            "opportunity": {"opportunity_id": "waiting", "expires_ns": 20},
            "reason": "ABSTAIN",
        },
    )
    store.close()
    store = ResearchStore(path)
    try:
        store.record_decision("later", {"decision_ns": 25, "opportunity": None})
        rows = store.db.execute(
            "SELECT state,decision_ns,reason FROM lifecycle WHERE opportunity_id='waiting' "
            "ORDER BY sequence"
        ).fetchall()
        assert rows[-1] == ("EXPIRED", 25, "DEADLINE_OBSERVED")
        assert not store.record_decision("later", {"decision_ns": 25, "opportunity": None})
        store.record_decision("later-again", {"decision_ns": 30, "opportunity": None})
        assert store.db.execute("SELECT COUNT(*) FROM lifecycle").fetchone()[0] == len(rows)
    finally:
        store.close()


def test_campaign_observation_restart_and_divergence(tmp_path):
    path = tmp_path / "research.sqlite"
    store = ResearchStore(path)
    payload = {"submitted": True, "realization": "SIMULATED", "entry_order": {"status": "FILLED"}}
    store.record_campaign_observation("campaign", 10, payload)
    store.close()
    store = ResearchStore(path)
    try:
        store.record_campaign_observation("campaign", 10, payload)
        with pytest.raises(ValueError, match="diverged"):
            store.record_campaign_observation("campaign", 10, {"submitted": False})
        with pytest.raises(ValueError, match="backwards"):
            store.record_campaign_observation("campaign", 9, payload)
        assert store.db.execute("SELECT COUNT(*) FROM campaign_observations").fetchone()[0] == 1
    finally:
        store.close()


@pytest.mark.parametrize(
    "first,second",
    [
        ("DEVELOPMENT", "HOLDOUT"),
        ("PROSPECTIVE", "HOLDOUT"),
        ("HOLDOUT", "DEVELOPMENT"),
        ("HOLDOUT", "PROSPECTIVE"),
    ],
)
def test_dataset_role_cannot_be_laundered_by_new_family_or_restart(tmp_path, first, second):
    path = tmp_path / "research.sqlite"
    store = ResearchStore(path)
    store.register_trial("original", "family-a", "policy-a", "same-data", first, 1)
    store.close()
    store = ResearchStore(path)
    try:
        with pytest.raises(ValueError, match="cannot mix"):
            store.register_trial("renamed", "family-b", "policy-b", "same-data", second, 2)
        assert store.family_size("family-b") == 0
        store.register_trial("other", "family-b", "policy-b", "different-data", second, 3)
        assert store.family_size("family-b") == 1
    finally:
        store.close()


def test_concurrent_holdout_and_development_registration_cannot_both_commit(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    path = tmp_path / "research.sqlite"
    ResearchStore(path).close()
    barrier = Barrier(2)

    def register(role):
        store = ResearchStore(path)
        try:
            barrier.wait()
            try:
                store.register_trial(role, role, "p", "same-data", role, 1)
                return True
            except ValueError:
                return False
        finally:
            store.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(register, ("HOLDOUT", "DEVELOPMENT")))
    assert sorted(results) == [False, True]


@pytest.mark.parametrize("first,second", [("DEVELOPMENT", "HOLDOUT"), ("HOLDOUT", "DEVELOPMENT")])
def test_different_manifests_cannot_hide_overlapping_holdout_windows(tmp_path, first, second):
    store = ResearchStore(tmp_path / "research.sqlite")
    try:
        store.register_trial("a", "a", "p", "hash-a", first, 1)
        store.register_dataset_window("hash-a", "BTC", 10, 30)
        store.register_trial("b", "b", "p", "hash-b", second, 2)
        with pytest.raises(ValueError, match="overlapping"):
            store.register_dataset_window("hash-b", "BTC", 20, 40)
        assert store.family_size("b") == 1  # Failed attempt remains counted.
        store.register_dataset_window("hash-b", "BTC", 30, 40)  # Adjacent is not overlap.
        store.register_dataset_window("hash-b", "ETH", 10, 30)
        with pytest.raises(ValueError, match="cannot be rewritten"):
            store.register_dataset_window("hash-a", "BTC", 10, 29)
    finally:
        store.close()


def test_training_source_holdout_overlap_includes_warmup_and_preserves_holdout(tmp_path):
    store = ResearchStore(tmp_path / "research.sqlite")
    try:
        store.register_trial("held", "family", "policy", "data", "HOLDOUT", 1)
        store.register_dataset_window("data", "BTC", 100, 200)
        for start, end in ((50, 101), (199, 250), (100, 200)):
            with pytest.raises(ValueError, match="protected holdout"):
                store.assert_no_holdout_overlap("BTC", start, end)
        store.assert_no_holdout_overlap("BTC", 0, 100)
        store.assert_no_holdout_overlap("BTC", 200, 300)
        store.assert_no_holdout_overlap("ETH", 100, 200)
        assert store.holdout_pristine("family", "data")
    finally:
        store.close()
