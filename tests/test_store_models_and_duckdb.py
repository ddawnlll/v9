from pathlib import Path

import polars as pl
import pytest
from pydantic import ValidationError

from v8_next.economics.decisions import (
    Opportunity,
    Stance,
    StanceKind,
    build_reconciliation_receipt,
)
from v8_next.evaluation.store import (
    BurnRecord,
    CampaignObservationRecord,
    DatasetWindowRecord,
    DependencyGroupProof,
    LifecycleRecord,
    ReconciliationReceipt,
    ReplayReconciliationRecord,
    ResearchStore,
    TrialRecord,
)


def test_trial_record_validation():
    valid = TrialRecord(
        trial_id="trial-1",
        family="family-a",
        policy_hash="pol-hash-1",
        dataset_hash="ds-hash-1",
        role="DEVELOPMENT",
        registered_ns=1_000_000,
    )
    assert valid.trial_id == "trial-1"
    assert valid.role == "DEVELOPMENT"

    with pytest.raises(ValidationError):
        TrialRecord(
            trial_id="",  # Empty ID rejected
            family="family-a",
            policy_hash="pol-hash-1",
            dataset_hash="ds-hash-1",
            role="DEVELOPMENT",
            registered_ns=1_000_000,
        )

    with pytest.raises(ValidationError):
        TrialRecord(
            trial_id="trial-1",
            family="family-a",
            policy_hash="pol-hash-1",
            dataset_hash="ds-hash-1",
            role="INVALID_ROLE",  # type: ignore[arg-type]
            registered_ns=1_000_000,
        )


def test_dataset_window_and_burn_records_validation():
    valid_window = DatasetWindowRecord(
        dataset_hash="ds-1",
        instrument_id="BTCUSDT-PERP.BINANCE",
        start_ns=100,
        end_ns=200,
    )
    assert valid_window.start_ns == 100
    assert valid_window.end_ns == 200

    # Inverted window rejected
    with pytest.raises(ValidationError):
        DatasetWindowRecord(
            dataset_hash="ds-1",
            instrument_id="BTCUSDT-PERP.BINANCE",
            start_ns=200,
            end_ns=100,
        )

    # Negative start rejected
    with pytest.raises(ValidationError):
        DatasetWindowRecord(
            dataset_hash="ds-1",
            instrument_id="BTCUSDT-PERP.BINANCE",
            start_ns=-1,
            end_ns=100,
        )

    valid_burn = BurnRecord(
        lineage="family-a",
        dataset_hash="ds-1",
        burned_ns=500_000,
    )
    assert valid_burn.lineage == "family-a"

    with pytest.raises(ValidationError):
        BurnRecord(lineage="  ", dataset_hash="ds-1", burned_ns=500_000)


def test_reconciliation_receipt_digest_computation_and_divergence():
    proof = DependencyGroupProof(
        dependency_group="group-1",
        raw_evidence_count=2,
        kinds=("SUPPORT", "SUPPORT"),
        effective_stance="SUPPORT",
    )
    receipt = ReconciliationReceipt(
        receipt_id="rec-1",
        opportunity_id="opp-1",
        reconciliation_ns=100_000,
        participating_evidence_ids=("obs-1", "obs-2"),
        dependency_group_proofs=(proof,),
        aggregate_stance="SUPPORTED_OBSERVATION",
    )
    assert receipt.digest != ""
    assert len(receipt.digest) == 64

    # Divergent digest check
    with pytest.raises(ValidationError, match="digest mismatch"):
        ReconciliationReceipt(
            receipt_id="rec-1",
            opportunity_id="opp-1",
            reconciliation_ns=100_000,
            participating_evidence_ids=("obs-1", "obs-2"),
            dependency_group_proofs=(proof,),
            aggregate_stance="SUPPORTED_OBSERVATION",
            digest="invalid-sha256-hash",
        )


def test_build_reconciliation_receipt_from_economic_stances():
    opp = Opportunity("opp-123", "ep-1", "BTCUSDT", "LONG", 1000, 2000)
    stances = (
        Stance(
            "squeeze-1",
            "volatility-group",
            StanceKind.SUPPORT,
            "BREAKOUT",
            "opp-123",
            1000,
        ),
        Stance(
            "squeeze-2",
            "volatility-group",
            StanceKind.SUPPORT,
            "BREAKOUT",
            "opp-123",
            1000,
        ),
        Stance(
            "trend-1",
            "trend-group",
            StanceKind.ABSTAIN,
            "FLAT",
            "opp-123",
            1000,
        ),
    )

    receipt = build_reconciliation_receipt(opp, stances, reconciliation_ns=1050)
    assert receipt.opportunity_id == "opp-123"
    assert receipt.aggregate_stance == "SUPPORTED_OBSERVATION"
    assert len(receipt.dependency_group_proofs) == 2
    vol_proof = next(p for p in receipt.dependency_group_proofs if p.dependency_group == "volatility-group")
    assert vol_proof.raw_evidence_count == 2
    assert vol_proof.effective_stance == "SUPPORT"

    # Mismatched opportunity stance raises error
    foreign_stance = Stance(
        "obs",
        "grp",
        StanceKind.SUPPORT,
        "REASON",
        "other-opp",
        1000,
    )
    with pytest.raises(ValueError, match="another opportunity"):
        build_reconciliation_receipt(opp, (foreign_stance,), reconciliation_ns=1050)



def test_research_store_trial_and_holdout_management(tmp_path: Path):
    store = ResearchStore(tmp_path / "research.sqlite")
    try:
        t1 = store.register_trial(
            TrialRecord(
                trial_id="t-dev-1",
                family="compression",
                policy_hash="pol-1",
                dataset_hash="ds-1",
                role="DEVELOPMENT",
                registered_ns=10,
            )
        )
        assert t1.trial_id == "t-dev-1"
        assert store.get_trial("t-dev-1") == t1
        assert store.family_size("compression") == 1

        # Holdout registration
        store.register_trial(
            trial_id="t-hold-1",
            family="compression",
            policy_hash="pol-1",
            dataset_hash="ds-holdout-1",
            role="HOLDOUT",
            registered_ns=20,
        )
        store.register_dataset_window("ds-holdout-1", "BTCUSDT", 100, 500)

        # Introspect holdouts before burn
        holdouts = store.get_holdouts(lineage="compression")
        assert len(holdouts) == 1
        assert holdouts[0].trial_id == "t-hold-1"
        assert holdouts[0].is_burned is False
        assert holdouts[0].start_ns == 100
        assert holdouts[0].end_ns == 500

        # Burn holdout
        burn = store.burn_holdout(
            BurnRecord(lineage="compression", dataset_hash="ds-holdout-1", burned_ns=30)
        )
        assert burn.burned_ns == 30
        assert not store.holdout_pristine("compression", "ds-holdout-1")

        # Introspect holdouts after burn
        holdouts_after = store.get_holdouts(lineage="compression")
        assert len(holdouts_after) == 1
        assert holdouts_after[0].is_burned is True
        assert holdouts_after[0].burned_ns == 30

        # List trials
        trials = store.list_trials(family="compression")
        assert len(trials) == 2
    finally:
        store.close()


def test_research_store_reconciliation_receipts_and_replays(tmp_path: Path):
    store = ResearchStore(tmp_path / "research.sqlite")
    try:
        proof = DependencyGroupProof(
            dependency_group="volatility",
            raw_evidence_count=1,
            kinds=("SUPPORT",),
            effective_stance="SUPPORT",
        )
        receipt = ReconciliationReceipt(
            receipt_id="rec-alpha",
            opportunity_id="opp-1",
            reconciliation_ns=1000,
            participating_evidence_ids=("obs-1",),
            dependency_group_proofs=(proof,),
            aggregate_stance="SUPPORTED_OBSERVATION",
        )

        # Insert receipt
        assert store.record_reconciliation_receipt(receipt) is True
        # Idempotent re-insert
        assert store.record_reconciliation_receipt(receipt) is False

        # Divergent receipt raises ValueError
        divergent_receipt = receipt.model_copy(update={"aggregate_stance": "CONTRADICTED"})
        with pytest.raises(ValueError, match="diverged"):
            store.record_reconciliation_receipt(divergent_receipt)

        # Retrieve receipt
        fetched = store.get_reconciliation_receipt("rec-alpha")
        assert fetched is not None
        assert fetched.receipt_id == receipt.receipt_id
        assert fetched.aggregate_stance == receipt.aggregate_stance
        assert fetched.digest == receipt.digest

        # List receipts
        all_receipts = store.list_reconciliation_receipts(opportunity_id="opp-1")
        assert len(all_receipts) == 1
        assert all_receipts[0].receipt_id == "rec-alpha"

        # Replay reconciliation slip
        replay_slip = ReplayReconciliationRecord(
            reconciliation_id="replay-1",
            run_id="run-session-001",
            reconciled_ns=2000,
            status="MATCHED",
            expected_hash="hash-expected-123",
            recovered_hash="hash-expected-123",
            mismatches=(),
        )
        assert store.record_replay_reconciliation(replay_slip) is True
        assert store.record_replay_reconciliation(replay_slip) is False

        fetched_replay = store.get_replay_reconciliation("replay-1")
        assert fetched_replay is not None
        assert fetched_replay.status == "MATCHED"

        replays = store.list_replay_reconciliations(run_id="run-session-001")
        assert len(replays) == 1
    finally:
        store.close()


def test_research_store_campaign_observation_and_lifecycle_records(tmp_path: Path):
    store = ResearchStore(tmp_path / "research.sqlite")
    try:
        # Campaign observation
        obs = CampaignObservationRecord(
            campaign_id="camp-1",
            observed_ns=10,
            payload={"action": "SUBMIT", "qty": "1.5"},
        )
        saved_obs = store.record_campaign_observation(obs)
        assert saved_obs.campaign_id == "camp-1"

        all_obs = store.list_campaign_observations("camp-1")
        assert len(all_obs) == 1
        assert all_obs[0].payload == {"action": "SUBMIT", "qty": "1.5"}

        # Lifecycle record
        lf1 = store.transition("opp-1", "DETECTED", 10, "GRAMMAR_DETECTED")
        assert lf1.state == "DETECTED"
        assert lf1.sequence == 0

        lf2 = store.transition(
            LifecycleRecord(
                opportunity_id="opp-1",
                sequence=1,
                state="PENDING",
                decision_ns=15,
                reason="EVALUATING",
            )
        )
        assert lf2.state == "PENDING"
        assert lf2.sequence == 1

        events = store.list_lifecycle_events("opp-1")
        assert len(events) == 2
        assert [e.state for e in events] == ["DETECTED", "PENDING"]
    finally:
        store.close()


def test_duckdb_olap_integration(tmp_path: Path):
    duckdb = pytest.importorskip("duckdb", reason="duckdb not installed (research extra)")

    db_path = tmp_path / "research.sqlite"
    store = ResearchStore(db_path)
    try:
        # Populate trials
        store.register_trial("t1", "family-alpha", "pol1", "ds1", "DEVELOPMENT", 100)
        store.register_trial("t2", "family-alpha", "pol2", "ds2", "DEVELOPMENT", 200)
        store.register_trial("t3", "family-beta", "pol3", "ds3", "HOLDOUT", 300)

        # Populate burns
        store.burn_holdout("family-beta", "ds3", 350)

        # Populate reconciliation receipt
        proof = DependencyGroupProof(
            dependency_group="grp1",
            raw_evidence_count=1,
            kinds=("SUPPORT",),
            effective_stance="SUPPORT",
        )
        receipt = ReconciliationReceipt(
            receipt_id="rec-1",
            opportunity_id="opp-99",
            reconciliation_ns=500,
            participating_evidence_ids=("obs1",),
            dependency_group_proofs=(proof,),
            aggregate_stance="SUPPORTED_OBSERVATION",
        )
        store.record_reconciliation_receipt(receipt)

        # Test duckdb_connection & analytical query
        duck_conn = store.duckdb_connection(read_only=True)
        try:
            res = duck_conn.execute("""
                SELECT family, count(*) as trial_count, max(registered_ns) as latest_ns
                FROM research.trials
                GROUP BY family
                ORDER BY family
            """).fetchall()
            assert res == [("family-alpha", 2, 200), ("family-beta", 1, 300)]
        finally:
            duck_conn.close()

        # Test store.query_duckdb helper
        rows = store.query_duckdb(
            "SELECT count(*) FROM research.reconciliation_receipts WHERE aggregate_stance = 'SUPPORTED_OBSERVATION'"
        )
        assert rows == [(1,)]

        # Test to_polars export
        df = store.to_polars("trials")
        assert isinstance(df, pl.DataFrame)
        assert df.height == 3
        assert set(df["family"].to_list()) == {"family-alpha", "family-beta"}

        # Test export_duckdb
        export_target = tmp_path / "export.duckdb"
        store.export_duckdb(export_target)
        assert export_target.exists()

        standalone_conn = duckdb.connect(str(export_target))
        try:
            tables = [
                r[0]
                for r in standalone_conn.execute(
                    "SELECT table_name FROM information_schema.tables WHERE table_schema='main'"
                ).fetchall()
            ]
            assert "trials" in tables
            assert "reconciliation_receipts" in tables
            assert "burns" in tables
            count = standalone_conn.execute("SELECT count(*) FROM trials").fetchone()[0]
            assert count == 3
        finally:
            standalone_conn.close()
    finally:
        store.close()
