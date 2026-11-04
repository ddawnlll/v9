import hashlib
from copy import deepcopy

import pytest

from v8_next.evaluation.family import family_losses
from v8_next.evaluation.store import ResearchStore, canonical


def setup_family(tmp_path):
    store = ResearchStore(tmp_path / "family.sqlite")
    results = []
    for name in ("a", "b"):
        frozen = dict(
            role="DEVELOPMENT",
            code_and_lock_hash="code",
            execution_model="native",
            config=dict(initial_balance="100", maker_fee="0", taker_fee="0", policy=name),
        )
        ph = hashlib.sha256(canonical(frozen).encode()).hexdigest()
        tid = hashlib.sha256(canonical(["f", "data", ph]).encode()).hexdigest()
        store.register_trial(tid, "f", ph, "data", "DEVELOPMENT", 100)
        marks = [
            dict(
                end_ns=t,
                observed_ns=t,
                phase="PRE_STRATEGY_BAR_CALLBACK",
                currency="USDT",
                cash="100",
                unrealized_pnl="0",
                equity="100",
                source_hash="fixture",
                close_price="10",
            )
            for t in (10, 20, 30)
        ]
        results.append(
            dict(
                trial_id=tid,
                family="f",
                dataset_hash="data",
                policy_hash=ph,
                frozen_policy=frozen,
                registered_ns=100,
                computed_ns=200,
                scope="OFFLINE_COUNTERFACTUAL_POLICY_EXPERIMENT",
                promotion_eligible=False,
                calibration_eligible=False,
                equity_marks=marks,
            )
        )
    return store, results


def test_complete_family_recomputes_instead_of_trusting_cached_losses(tmp_path):
    store, results = setup_family(tmp_path)
    try:
        results[0]["period_losses"] = "untrusted cached field"
        losses = family_losses(results, store=store, family="f", decision_ns=300)
        assert len(losses) == 2
        assert all(row.loss == 0 for rows in losses.values() for row in rows)
        with pytest.raises(ValueError, match="incomplete"):
            family_losses(results[:1], store=store, family="f", decision_ns=300)
    finally:
        store.close()


@pytest.mark.parametrize(
    "mutation", ["source", "identity", "future", "duplicate", "missing_failed"]
)
def test_incompatible_or_omitted_trials_reject(tmp_path, mutation):
    store, original = setup_family(tmp_path)
    results = deepcopy(original)
    try:
        if mutation == "source":
            results[1]["equity_marks"][0]["source_hash"] = "changed"
        elif mutation == "identity":
            results[1]["frozen_policy"]["config"]["maker_fee"] = ".01"
        elif mutation == "future":
            results[1]["computed_ns"] = 301
        elif mutation == "duplicate":
            results[1] = results[0]
        else:
            store.register_trial("failed", "f", "p", "data", "DEVELOPMENT", 100)
        with pytest.raises(ValueError):
            family_losses(results, store=store, family="f", decision_ns=300)
    finally:
        store.close()


def test_family_connects_real_valuation_shape_to_numerical_diagnostics(tmp_path):
    pytest.importorskip("arch", reason="arch not installed (research extra)")
    from v8_next.evaluation.family import compare_family

    store, results = setup_family(tmp_path)
    try:
        for result in results:
            result["equity_marks"] = [
                {**result["equity_marks"][0], "end_ns": t, "observed_ns": t}
                for t in range(10, 90, 10)
            ]
        for i, mark in enumerate(results[1]["equity_marks"]):
            mark["equity"] = str(100 + i * i)
            mark["unrealized_pnl"] = str(i * i)
        report = compare_family(
            results,
            store=store,
            family="f",
            baseline_trial_id=results[0]["trial_id"],
            decision_ns=300,
            block_size=2,
            reps=19,
            seed=12,
        )
        assert report["diagnostic"]["sample_intervals"] == 7
        assert report["diagnostic"]["wrc"]
        assert report["promotion_eligible"] is False
        assert report["scope"] == "DEVELOPMENT_EXPLORATION_NOT_OOS"
    finally:
        store.close()


def test_compare_cli_rejects_missing_family_without_writing_report(tmp_path, monkeypatch):
    from v8_next.app.compare import main

    store, results = setup_family(tmp_path)
    store.close()
    source = tmp_path / "one.json"
    source.write_text(canonical(results[0]))
    output = tmp_path / "report.json"
    monkeypatch.setattr(
        "sys.argv",
        [
            "compare",
            str(source),
            "--output",
            str(output),
            "--store",
            str(tmp_path / "family.sqlite"),
            "--family",
            "f",
            "--baseline",
            results[0]["trial_id"],
            "--block-size",
            "1",
            "--reps",
            "19",
            "--seed",
            "12",
        ],
    )
    with pytest.raises(ValueError, match="incomplete"):
        main()
    assert not output.exists()


def test_comparison_pbo_requires_full_candidate_set_not_baseline(tmp_path):
    pytest.importorskip("arch", reason="arch not installed (research extra)")
    from v8_next.evaluation.family import compare_family
    from v8_next.evaluation.overfitting import CSCVPlan

    store, results = setup_family(tmp_path)
    try:
        third = deepcopy(results[1])
        third["frozen_policy"]["config"]["policy"] = "c"
        third["policy_hash"] = hashlib.sha256(
            canonical(third["frozen_policy"]).encode()
        ).hexdigest()
        third["trial_id"] = hashlib.sha256(
            canonical(["f", "data", third["policy_hash"]]).encode()
        ).hexdigest()
        store.register_trial(
            third["trial_id"], "f", third["policy_hash"], "data", "DEVELOPMENT", 100
        )
        results.append(third)
        for j, result in enumerate(results):
            result["equity_marks"] = [
                {
                    **result["equity_marks"][0],
                    "end_ns": i * 10,
                    "observed_ns": i * 10,
                    "equity": str(100 + j * i * i),
                    "unrealized_pnl": str(j * i * i),
                }
                for i in range(1, 10)
            ]
        kwargs = dict(
            store=store,
            family="f",
            baseline_trial_id=results[0]["trial_id"],
            decision_ns=300,
            block_size=2,
            reps=19,
            seed=12,
        )
        plan = CSCVPlan(2, "mean_return", 2, tuple(r["trial_id"] for r in results[1:]))
        report = compare_family(results, **kwargs, pbo_plan=plan)
        assert report["diagnostic"]["pbo"] is not None
        from decimal import Decimal

        from v8_next.evaluation.alignment import IntervalLoss
        from v8_next.evaluation.deflated_sharpe import DSRPlan

        dsr = DSRPlan(results[1]["trial_id"], plan.registered_variants, 2, "test-only assumption")
        reference = tuple(
            IntervalLoss(i * 10, (i + 1) * 10, 210, Decimal("-.001")) for i in range(1, 9)
        )
        with pytest.raises(ValueError, match="requires explicit reference"):
            compare_family(results, **kwargs, dsr_plan=dsr)
        report = compare_family(
            results,
            **kwargs,
            dsr_plan=dsr,
            reference_losses=reference,
            reference_basis="test fixture",
        )
        assert 0 <= report["diagnostic"]["dsr"]["dsr_confidence"] <= 1
        assert (
            report["diagnostic"]["dsr_reference_status"] == "CALLER_SUPPLIED_NOT_SOURCE_CERTIFIED"
        )
        with pytest.raises(ValueError, match="complete registered"):
            compare_family(results, **kwargs, pbo_plan=CSCVPlan(2, "mean_return", 2, ()))
    finally:
        store.close()


@pytest.mark.parametrize(
    "field", ["accounting_as_of_ns", "allocation_priority", "capture_manifest_hashes"]
)
def test_family_rejects_different_frozen_execution_assumptions(tmp_path, field):
    store, results = setup_family(tmp_path)
    try:
        # Both are legitimately registered policies: reject incompatibility,
        # not merely a tampered policy hash.
        store.db.execute("DELETE FROM trials")
        for i, result in enumerate(results):
            frozen = result["frozen_policy"]
            frozen.update(
                accounting_as_of_ns=150,
                allocation_priority="ASCENDING",
                capture_manifest_hashes=["source-a", "source-b"],
            )
            if i:
                frozen[field] = {
                    "accounting_as_of_ns": 160,
                    "allocation_priority": "DESCENDING",
                    "capture_manifest_hashes": ["source-a", "source-c"],
                }[field]
            ph = hashlib.sha256(canonical(frozen).encode()).hexdigest()
            tid = hashlib.sha256(canonical(["f", "data", ph]).encode()).hexdigest()
            result.update(policy_hash=ph, trial_id=tid)
            store.register_trial(tid, "f", ph, "data", "DEVELOPMENT", 100)
        with pytest.raises(ValueError, match="incompatible"):
            family_losses(results, store=store, family="f", decision_ns=300)
    finally:
        store.close()
