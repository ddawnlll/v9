"""Comprehensive Tests for G3-G9 Gate Resolution Battery (Rule 12, Rule 30, Rule 57).

Validates:
1. G3: Scenario Robustness across 4 market regimes (Bull Trend, Bear Crash, Chop/Range, High-Vol Spill).
2. G4: Synthetic / Adversarial Falsification with execution shocks & bracket stop-loss.
3. G5: Selection Control via Deflated Sharpe Ratio (DSR) & White's Reality Check (WRC).
4. G6: Frozen Out-of-Sample (OOS) Replication with performance retention >= 60%.
5. G7: Prospective Shadow Succession with causal e-process martingale & drift tracking.
6. G8: Live Realization (D-152 §5 Diagnostic Fold NOT_APPLICABLE in research, PASS with signed fills).
7. G9: Central ClaimRegistry verification of ledger hash chain & StatutoryClaimRecord issuance.
8. End-to-end BenchmarkRunner execution with resolve_gates=True.
"""

import hashlib
import inspect
import json
from decimal import Decimal
from pathlib import Path

import pytest

from v8_next.adapters.expert_strategy import ExpertStrategyConfig
from v8_next.adapters.shadow_ingest import make_venue_attestation
from v8_next.domain.market import Candle
from v8_next.evaluation.benchmark_receipt import (
    BenchmarkLedger,
    BenchmarkReceipt,
    GateState,
    GateVector,
    ReadinessStatus,
    ScoreEvidence,
)
from v8_next.evaluation.claims import ClaimRegistry, StatutoryClaimClass
from v8_next.evaluation.gate_resolution import (
    DEFAULT_TAPE_PATH,
    WindowBinding,
    classify_market_regimes,
    evaluate_g3_scenario_robustness,
    evaluate_g4_synthetic_falsification,
    evaluate_g5_selection_control,
    evaluate_g6_frozen_oos,
    evaluate_g7_prospective_shadow,
    evaluate_g8_live_realization,
    evaluate_g9_certificate_authority,
    load_tape_candles,
)
from v8_next.evaluation.runner import BenchmarkCase, BenchmarkRunner
from v8_next.evaluation.scoring import (
    compute_capability_breakdown,
    compute_capability_score,
)

#: MECHANICS ONLY: fixed determinants for the receipts in this file, so the number
#: they publish is *derived* from the evidence they bind (#408). A hand-set score
#: its own evidence cannot produce is no longer constructible -- these receipts exist
#: to exercise the G9 claim authority, not to declare a capability of their own.
_MEASUREMENT: dict = dict(
    pnl_series=[0.01, -0.02, 0.03, 0.005] * 3, total_bars=60, total_trades=6, abstain_rate=0.2
)
_MEASURED_EVIDENCE = ScoreEvidence.from_breakdown(compute_capability_breakdown(**_MEASUREMENT))
_MEASURED_SCORE = compute_capability_score(**_MEASUREMENT)
assert _MEASURED_SCORE is not None, "the mechanics fixture must measure a number"


@pytest.fixture
def real_candles() -> list[Candle]:
    """Load a slice of real tape candles for fast unit test execution."""
    if not DEFAULT_TAPE_PATH.exists():
        pytest.skip(f"Real tape not found at {DEFAULT_TAPE_PATH}")
    return load_tape_candles(DEFAULT_TAPE_PATH, limit=120)


def test_load_tape_candles_real_data():
    """Verify loading real Binance 1h klines with Polars."""
    if not DEFAULT_TAPE_PATH.exists():
        pytest.skip("Real tape not found")
    candles = load_tape_candles(DEFAULT_TAPE_PATH, limit=50)
    assert len(candles) == 50
    assert candles[0].instrument_id == "BTCUSDT-PERP.BINANCE"
    assert candles[0].open > 0
    assert candles[0].close > 0
    assert candles[0].end_ns > candles[0].start_ns


def test_classify_market_regimes_4_partitions(real_candles: list[Candle]):
    """Verify market regime classification into Bull, Bear, Chop, High-Vol."""
    regimes = classify_market_regimes(candles=real_candles, slice_length=20)
    expected_regimes = {"Bull Trend", "Bear Crash", "Chop/Range", "High-Vol Spill"}
    assert set(regimes.keys()) == expected_regimes
    for name, c_list in regimes.items():
        assert len(c_list) > 0, f"Regime {name} has no candles"


def test_g3_scenario_robustness(real_candles: list[Candle]):
    """G3: honest sample floors — empty regimes fail closed with named reason."""
    regimes = classify_market_regimes(candles=real_candles, slice_length=25)
    cfg = ExpertStrategyConfig(min_support_quorum=1, max_contradiction_tolerance=28)
    state, metrics = evaluate_g3_scenario_robustness(regimes, cfg)
    assert metrics["max_drawdown_limit"] == 0.10
    assert metrics["min_trades_per_regime"] == 1
    assert len(metrics["regimes"]) == 4
    if metrics["empty_regimes"]:
        assert state == GateState.BLOCKED
        assert "INSUFFICIENT_REGIME_TRADES" in metrics["reason"]
        for name in metrics["empty_regimes"]:
            assert metrics["regimes"][name]["trades"] == 0
    else:
        assert metrics["min_regime_trades"] >= 1
        assert state == GateState.PASS
        assert metrics["passed"] is True
        assert metrics["max_observed_drawdown"] <= 0.10
        assert metrics["max_observed_variance"] <= 0.20


#: #495 — the G3 cell, and the one measurement its name may describe. A published
#: vector field that names benchmark *coverage* is a different question from the
#: regime/scenario robustness the resolver actually measures, so the three surfaces
#: (vector field, registry resolver, published basis) are pinned to one quantity here.
G3_VECTOR_FIELD = "g3_scenario_robustness"
G3_RESOLVER = "evaluate_g3_scenario_robustness"
G3_CANONICAL_ID = "G3ScenarioRobustness"


def test_g3_cell_name_resolver_and_published_basis_name_the_same_measurement():
    """#495: the G3 cell's name, its resolver and its published basis must agree.

    The defect this pins: the cell was published as ``g3_benchmark_coverage`` while
    the only resolver in the tree measured regime/scenario robustness, so a receipt
    claimed a benchmark-coverage check that was never taken. The invariant below is
    mechanical -- the field name, the registry resolver and the metrics/basis the
    resolver publishes must describe the *same* quantity -- so this class of drift
    cannot come back under either name.
    """
    from v8_next.evaluation import gate_resolution as gate_resolution_module
    from v8_next.evaluation.benchmark_receipt import GATE_DESCRIPTORS
    from v8_next.evaluation.gate_registry import registry_by_id
    from v8_next.evaluation.scoring import evaluate_gate_vector

    entry = registry_by_id()[G3_CANONICAL_ID]
    descriptor = next(d for d in GATE_DESCRIPTORS if d.canonical_id == G3_CANONICAL_ID)

    # 1. The three surfaces agree on the published cell name.
    assert entry.vector_field == descriptor.vector_field == G3_VECTOR_FIELD
    assert G3_VECTOR_FIELD in GateVector.model_fields, "the vector field must exist by that name"

    # 2. The resolver the registry points at is the one that measures this cell.
    assert entry.resolver == G3_RESOLVER
    resolver = getattr(gate_resolution_module, entry.resolver, None)
    assert callable(resolver), f"{entry.resolver!r} is not a callable in gate_resolution"

    # 3. The basis the resolver publishes is the regime partition it measures over --
    #    not a benchmark curve set, a primary id or a funding basis.
    resolver_params = list(inspect.signature(resolver).parameters)
    assert resolver_params[0] == "regimes", (
        f"{entry.resolver}() must take the regime partition it measures; "
        f"parameters are {resolver_params}"
    )
    _, basis = resolver({})
    assert "regimes" in basis, f"the published basis must name the regime partition: {basis}"
    assert "max_observed_drawdown" in basis
    assert "max_observed_variance" in basis
    benchmark_vocabulary = ("coverage", "benchmark", "primary_benchmark", "funding_basis")
    leaked = sorted(
        key for key in basis if any(word in key.lower() for word in benchmark_vocabulary)
    )
    assert leaked == [], f"the G3 basis publishes benchmark vocabulary it never measured: {leaked}"

    # 4. The resolver's own docstring must publish the same cell name.
    doc = (resolver.__doc__ or "").lower()
    assert G3_VECTOR_FIELD in doc, (
        f"{entry.resolver}() docstring must name the cell it publishes ({G3_VECTOR_FIELD})"
    )
    assert "coverage" not in doc and "benchmark" not in doc, (
        "the G3 resolver docstring must not claim a benchmark measurement it does not take"
    )
    assert "scenario robustness" in doc and "regime" in doc

    # 5. The descriptor's source clause describes the same quantity.
    clause = descriptor.source_clause.lower()
    assert "coverage" not in clause and "benchmark" not in clause
    assert "scenario" in clause and "robust" in clause

    # 6. No surface of the published vector claims benchmark coverage at all.
    coverage_fields = [name for name in GateVector.model_fields if "coverage" in name]
    assert coverage_fields == [], (
        f"the gate vector still publishes a cell named for coverage: {coverage_fields}"
    )

    # 7. The canonical scoring rule publishes the renamed cell, and an unspecified
    #    G3 stays unmeasured (UNKNOWN) rather than being defaulted to a verdict.
    default_vector = evaluate_gate_vector(total_bars=1, total_trades=0, pnl_series=[])
    assert getattr(default_vector, G3_VECTOR_FIELD) is GateState.UNKNOWN


def test_g4_synthetic_falsification_adversarial_shock(real_candles: list[Candle]):
    """G4: survival without shocked executions fails closed (vacuous PASS banned)."""
    cfg = ExpertStrategyConfig(
        min_support_quorum=1,
        max_contradiction_tolerance=28,
        bracket_stop_pct=Decimal("0.02"),
        bracket_target_pct=Decimal("0.04"),
    )
    state, metrics = evaluate_g4_synthetic_falsification(real_candles, cfg)
    assert metrics["min_closed_trades"] == 2
    if metrics["closed_trades"] < 2:
        assert state == GateState.BLOCKED
        assert "INSUFFICIENT_SHOCK_SAMPLE" in metrics["reason"]
    else:
        assert metrics["account_survived"] is True
        assert metrics["final_equity"] >= 7000.0
        assert state == GateState.PASS


def test_g5_selection_control_dsr_and_wrc():
    """A passing stress diagnostic cannot certify an undocumented selection family."""
    from v8_next.evaluation.statistics_plan import CANONICAL_G5_BLOCK_SIZE, g5_plan

    pnls = [0.03, 0.025, 0.04, 0.01, 0.035, 0.02, 0.05, 0.015, 0.03, 0.045] * 3
    plan = g5_plan(
        family="test-g5",
        pinned_ns=1,
        block_size=CANONICAL_G5_BLOCK_SIZE,
        reps=200,
        seed=42,
    )
    state, metrics = evaluate_g5_selection_control(pnls, plan=plan)
    assert state == GateState.BLOCKED
    assert metrics["passed"] is False
    assert metrics["stress_diagnostic_passed"] is True
    assert metrics["selection_family_status"] == "MISSING_PROVENANCE_BOUND_SELECTION_HISTORY"
    assert metrics["dsr_confidence"] >= 0.95
    assert metrics["adjusted_bonferroni_pvalue"] <= 0.05
    assert metrics["own_sample_count"] == 30
    assert metrics["sample_source"] == "own_track"
    # NX07.R3/R4: the plan is identified in the metrics and it declares, as data,
    # which statistics the gate turns on and which it only reports.
    assert metrics["plan_id"] == plan.identity()
    assert metrics["trials"] == plan.multiplicity_trials
    assert "deflated_sharpe_confidence>=0.95" in metrics["authority_conditions"]
    assert "white_reality_check_p_value" in metrics["diagnostics"]
    assert not set(metrics["authority_conditions"]) & set(metrics["diagnostics"])


def test_g6_frozen_oos_replication(real_candles: list[Candle]):
    """G6: the verdict names the window it was measured over (#406/#421).

    Without a declared protected window the gate publishes no retention verdict at
    all — the slice a run happened to load (``head(limit)`` file order) is not a
    declaration. With the evaluated window declared, the criterion decides.
    """
    cfg = ExpertStrategyConfig(min_support_quorum=1, max_contradiction_tolerance=28)

    unbound_state, unbound = evaluate_g6_frozen_oos(real_candles, cfg, min_retention_ratio=0.60)
    assert unbound_state == GateState.BLOCKED
    assert "PROTECTED_WINDOW_ABSENT" in unbound["reason"]
    assert unbound["measurement"] == "unmeasured"
    assert unbound["retention_ratio"] is None
    assert unbound["passed"] is False

    declared = WindowBinding.from_candles(
        real_candles, origin="test_g6_frozen_oos_replication", declared_by="test"
    )
    state, metrics = evaluate_g6_frozen_oos(
        real_candles, cfg, min_retention_ratio=0.60, protected_window=declared
    )
    assert metrics["measurement"] == "measured"
    assert metrics["protected_window"]["n_bars"] == len(real_candles)
    assert metrics["passed"] == (state == GateState.PASS)
    assert "is_profit" in metrics and "oos_profit" in metrics
    if state == GateState.BLOCKED:
        assert metrics["reason"] is not None
        assert any(
            code in metrics["reason"]
            for code in ("NO_IS_EDGE_TO_RETAIN", "PROFIT_RETENTION_BREACH", "OOS_BLOWUP")
        )
    else:
        assert metrics["passed"] is True
        assert metrics["is_profit"] > 0
        assert metrics["retention_ratio"] >= 0.60
        assert metrics["oos_final_balance"] >= 8000.0


def test_g7_prospective_shadow_streaming(tmp_path: Path, real_candles: list[Candle]):
    """G7: the prospective verdict needs a declared forward window (#406/#421).

    NX08.R4 keeps the declared-stream requirement (asserted in the test below);
    #406/#421 adds the binding: the run's own tail (``candles[-100:]``) overlaps the
    scored window, so it is refused by name instead of being graded as if it were
    prospective, and a stream without a declared window publishes no verdict either.
    """
    out_dir = tmp_path / "shadow"
    in_sample_state, in_sample = evaluate_g7_prospective_shadow(
        real_candles,
        output_dir=out_dir,
        shadow_stream=real_candles[-100:],
        shadow_origin="TEST_FIXTURE_SAME_RUN_TAIL",
        shadow_window=WindowBinding.from_candles(real_candles[-100:], origin="same_run_tail"),
    )
    assert in_sample_state != GateState.PASS
    assert in_sample["measurement"] == "unmeasured"
    assert "PROSPECTIVE_WINDOW_NOT_FORWARD_OF_SCORED_WINDOW" in in_sample["reason"]

    state, metrics = evaluate_g7_prospective_shadow(
        real_candles,
        output_dir=out_dir,
        shadow_stream=real_candles[-100:],
        shadow_origin="TEST_FIXTURE_DECLARED_STREAM",
    )
    assert state != GateState.PASS
    assert metrics["measurement"] == "unmeasured"
    assert "PROSPECTIVE_WINDOW_ABSENT" in metrics["reason"]
    assert metrics["provenance_status"] == "NO_DECLARED_WINDOW"


def test_g7_refuses_the_runs_own_historical_tail(real_candles: list[Candle], tmp_path: Path):
    """NX08.R4: no prospective state can be minted from the run's own tail."""
    state, metrics = evaluate_g7_prospective_shadow(
        real_candles, output_dir=tmp_path / "shadow-removed"
    )
    assert state == GateState.UNKNOWN
    assert metrics["reason"] == "PSEUDO_PROSPECTIVE_HISTORICAL_WINDOW_NOT_ACCEPTED"
    assert metrics["passed"] is False
    # and a declared stream without a named origin is refused outright
    with pytest.raises(ValueError, match="named origin"):
        evaluate_g7_prospective_shadow(
            real_candles, tmp_path / "shadow-unnamed", shadow_stream=real_candles[-100:]
        )


def test_g8_live_realization_modes(tmp_path: Path, monkeypatch) -> None:
    """G8: public paper stays diagnostic; settlement needs attestation + reconciliation."""
    # 1. Candidate / Research phase without live venue
    state, metrics = evaluate_g8_live_realization(live_fills_path=None)
    assert state == GateState.NOT_APPLICABLE
    assert metrics["mode"] == "DIAGNOSTIC_FOLD"

    # 2. A well-formed file is public paper (NX10.R4): technical evidence, not a
    # settlement. It must NOT reach PASS however valid its columns look.
    fills_file = tmp_path / "fills.jsonl"
    fills_file.write_text(
        json.dumps(
            {
                "fill_id": "F-001",
                "instrument": "BTCUSDT",
                "price": 105000,
                "qty": 0.01,
                "side": "BUY",
                "venue_time_ns": 1,
                "order_id": "O-001",
            }
        )
        + "\n"
    )
    state_paper, metrics_paper = evaluate_g8_live_realization(live_fills_path=fills_file)
    assert state_paper == GateState.NOT_APPLICABLE
    assert metrics_paper["mode"] == "PUBLIC_PAPER_NOT_SETTLED"
    assert metrics_paper["authority"] == "NONE"
    assert metrics_paper["fills_count"] == 1
    assert "PUBLIC_PAPER_TECHNICAL_EVIDENCE_ONLY" in metrics_paper["reason"]

    # 2b. A caller label plus an account id is not authenticity.
    with pytest.raises(ValueError, match="account identity"):
        evaluate_g8_live_realization(
            live_fills_path=fills_file,
            provenance="authenticated_venue_statement",
        )
    state_unverified, metrics_unverified = evaluate_g8_live_realization(
        live_fills_path=fills_file,
        provenance="authenticated_venue_statement",
        account_id="acct-unit-test",
    )
    assert state_unverified == GateState.BLOCKED
    assert metrics_unverified["mode"] == "UNVERIFIED_PROVENANCE"

    # 2c. A trusted-ingestion attestation is necessary but not sufficient: with
    # no account snapshot there is no reconciliation, so no PASS.
    monkeypatch.setenv("V8_VENUE_ATTESTATION_KEY", "test-key")
    sha = hashlib.sha256(fills_file.read_bytes()).hexdigest()
    receipt = make_venue_attestation(
        fills_sha256=sha, account_id="acct-unit-test", key="test-key"
    )
    attestation_path = tmp_path / "fills.attestation.json"
    attestation_path.write_text(json.dumps(receipt))
    state_unreconciled, metrics_unreconciled = evaluate_g8_live_realization(
        live_fills_path=fills_file,
        provenance="authenticated_venue_statement",
        account_id="acct-unit-test",
        attestation_path=attestation_path,
        account=None,
    )
    assert state_unreconciled == GateState.BLOCKED
    assert metrics_unreconciled["mode"] == "RECONCILIATION_MISSING"

    # 2d. Attested + reconciled reaches PASS, and only then is it labelled reconciled.
    account = {
        "account_id": "acct-unit-test",
        "balance_total": "10000 USDT",
        "positions": [{"side": "LONG", "quantity": "0.01"}],
        "orders": [{"status": "FILLED"}],
        "currency": "USDT",
    }
    state_live, metrics_live = evaluate_g8_live_realization(
        live_fills_path=fills_file,
        provenance="authenticated_venue_statement",
        account_id="acct-unit-test",
        attestation_path=attestation_path,
        account=account,
    )
    assert state_live == GateState.PASS
    assert metrics_live["mode"] == "LIVE_VENUE_SETTLED"
    assert metrics_live["account_reconciled"] is True
    assert metrics_live["reconciliation"]["reconciled"] is True

def test_g9_claim_registry_and_certificate_authority(tmp_path: Path):
    """G9: ClaimRegistry verifies ledger hash chain and issues signed StatutoryClaimRecord."""
    ledger_path = tmp_path / "ledger.jsonl"
    ledger = BenchmarkLedger.load_jsonl(ledger_path)

    gates_ready = GateVector(
        g0_identity=GateState.PASS,
        g1_causal_pit=GateState.PASS,
        g2_determinism_ledger=GateState.PASS,
        g3_scenario_robustness=GateState.PASS,
        g4_structural_robustness=GateState.PASS,
        g5_statistical_credibility=GateState.PASS,
        g6_protected_oos=GateState.PASS,
        g7_generalization=GateState.PASS,
        g8_prospective_shadow=GateState.NOT_APPLICABLE,
        g9_live_realization=GateState.MISSING,
    )

    receipt = BenchmarkReceipt.create(
        case_id="BC-TEST-G9",
        policy_id="pol_test",
        capability_score=_MEASURED_SCORE,
        coverage_factor=_MEASURED_EVIDENCE.coverage_factor,
        score_evidence=_MEASURED_EVIDENCE,
        gates=gates_ready,
        computed_at_timestamp_ns=1000,
    )
    ledger.append(receipt)

    state, metrics, claim = evaluate_g9_certificate_authority(
        ledger=ledger,
        receipt_digest=receipt.receipt_digest,
        gates=gates_ready,
        capability_score=_MEASURED_SCORE,
        output_dir=tmp_path,
        live_realization_verified=False,
    )

    assert state == GateState.PASS
    assert claim is not None
    assert claim.claim_class == StatutoryClaimClass.ReadyNotClaimed
    assert claim.verify_signature() is True
    assert metrics["signature_verified"] is True


def test_end_to_end_benchmark_runner_resolved_gates(tmp_path: Path):
    """End-to-end: honest fail-closed chain on real tape (no vacuous PASS).

    On the current tape the policy barely fires, so G3/G4/G6 fail closed with
    named reasons, G5 discloses its regime-fallback source, no claim is minted
    and the certificate stays BLOCKED. The test pins the honesty machinery
    (reasons, disclosure, no-claim), not tape-dependent counts.
    """
    if not DEFAULT_TAPE_PATH.exists():
        pytest.skip("Real tape not found")
    candles = load_tape_candles(DEFAULT_TAPE_PATH, limit=500)
    case = BenchmarkCase(
        case_id="BC-INTEGRATION-01",
        policy_id="pol_28_ensemble",
        dataset_name="BTCUSDT-1H",
        strategy_config=ExpertStrategyConfig(min_support_quorum=1, max_contradiction_tolerance=28),
    )

    runner = BenchmarkRunner(output_dir=tmp_path / "benchmarks")
    result = runner.run(
        case,
        candles,
        resolve_gates=True,
    )

    # Structural gates: G0 measured from real lineage; G1/G2 unmeasured -> UNKNOWN.
    assert result.gates.g0_identity == GateState.PASS
    assert result.gates.g1_causal_pit == GateState.UNKNOWN
    assert result.gates.g2_determinism_ledger == GateState.UNKNOWN

    # Thin-sample gates fail closed with named reasons
    gm = result.gate_metrics or {}
    for gname, reason_code in (
        ("g3", "INSUFFICIENT_REGIME_TRADES"),
        ("g4", "INSUFFICIENT_SHOCK_SAMPLE"),
        ("g6", None),
    ):
        state = getattr(result.gates, {"g3": "g3_scenario_robustness", "g4": "g4_structural_robustness", "g6": "g6_protected_oos"}[gname])
        assert state == GateState.BLOCKED, f"{gname} must fail closed on thin samples"
        assert gm[gname]["reason"] is not None
        if reason_code is not None:
            assert reason_code in gm[gname]["reason"]
    assert gm["g6"]["reason"] is not None

    # G5 discloses its sample source; G7 holds its window; G8 stays out of scope
    assert gm["g5"]["sample_source"] in ("own_track", "regime_fallback")
    assert gm["g5"]["own_sample_count"] <= result.total_trades + len(candles)
    # NX08.R4: a historical run has no declared prospective stream, so G7 cannot
    # pass on the last 100 bars of its own candles.
    assert result.gates.g7_generalization == GateState.UNKNOWN
    assert (
        gm["g7"]["reason"] == "PSEUDO_PROSPECTIVE_HISTORICAL_WINDOW_NOT_ACCEPTED"
    )
    assert result.gates.g8_prospective_shadow == GateState.NOT_APPLICABLE

    # No claim minted; certificate fails closed; ledger verifies
    assert result.claim_record is None
    verdict = result.gates.readiness()
    assert verdict.status == ReadinessStatus.HardFailure
    assert "BLOCKED" in result.certificate.status
    assert "NO_ECONOMIC_CLAIM" in result.certificate.authority_verdict or "BLOCKED" in result.certificate.authority_verdict
    chain_ok, _ = runner.ledger.verify_chain()
    assert chain_ok is True


# --------------------------------------------------------------------------- #
# #435 — an unevaluated gate (NOT_APPLICABLE) is not an established gate (PASS)
# --------------------------------------------------------------------------- #

#: The verbatim fixture of published ledger entry 0 — case BC-D153-CANONICAL-01
#: at `v8.5-digest-v2`: every gate PASS except the live-realization fold, which
#: the owning clause (D-152 §5) legitimately leaves NOT_APPLICABLE.
CANONICAL_LIVE_FOLD_FIXTURE = (
    Path(__file__).parent / "fixtures" / "ledger_canon" / "v8.5-digest-v2.json"
)


def _all_pass_gate_states() -> dict[str, GateState]:
    return {field: GateState.PASS for field in GateVector.model_fields}


def test_not_applicable_on_a_required_blocking_gate_is_not_an_established_state() -> None:
    """#435 (a): a never-evaluated blocking gate cannot certify like a PASS one.

    The two vectors differ only in whether constitutional integrity was ever
    established; `GateState.holds()`/`readiness()` must tell them apart, and the
    verdict must name the unevaluated blocking gate instead of staying silent.
    """
    fully_established = GateVector(**_all_pass_gate_states())
    never_evaluated = GateVector(
        **{**_all_pass_gate_states(), "g0_identity": GateState.NOT_APPLICABLE}
    )

    established_verdict = fully_established.readiness()
    unevaluated_verdict = never_evaluated.readiness()

    assert established_verdict.status == ReadinessStatus.Certified
    assert established_verdict.status_string() == "READY_NOT_CLAIMED"
    assert fully_established.all_pass() is True

    assert unevaluated_verdict.status != established_verdict.status
    assert unevaluated_verdict.status == ReadinessStatus.HardFailure
    assert unevaluated_verdict.status_string() == "BLOCKED"
    assert unevaluated_verdict.unevaluated_blocking == (0,)
    assert unevaluated_verdict.failing_positions == (0,)
    assert unevaluated_verdict.hard_failures == ()
    assert unevaluated_verdict.evidence_gaps == ()
    assert unevaluated_verdict.blocking_reasons() == (
        "REQUIRED_BLOCKING_GATE_UNEVALUATED: G0ConstitutionalIntegrity "
        "(g0_identity)=NOT_APPLICABLE, requirement=RequiredBlocking",
    )
    assert never_evaluated.all_pass() is False

    # G1 is declared RequiredBlocking too, and the same rule applies to it
    g1_unevaluated = GateVector(
        **{**_all_pass_gate_states(), "g1_causal_pit": GateState.NOT_APPLICABLE}
    )
    assert g1_unevaluated.readiness().status == ReadinessStatus.HardFailure
    assert g1_unevaluated.readiness().unevaluated_blocking == (1,)

    # ... while the fold the owning clause admits stays admitted: the G8
    # live-realization slot is declared Required, not RequiredBlocking
    fold = GateVector(
        **{**_all_pass_gate_states(), "g8_prospective_shadow": GateState.NOT_APPLICABLE}
    )
    assert fold.readiness().status == ReadinessStatus.Certified
    assert fold.readiness().unevaluated_blocking == ()


def test_claim_minting_refuses_an_unevaluated_blocking_gate(tmp_path: Path) -> None:
    """#435 (b): the claim precondition refuses a never-evaluated blocking gate."""
    gates = GateVector(
        **{**_all_pass_gate_states(), "g0_identity": GateState.NOT_APPLICABLE}
    )
    ledger = BenchmarkLedger.load_jsonl(tmp_path / "ledger.jsonl")
    receipt = BenchmarkReceipt.create(
        case_id="BC-TEST-435",
        policy_id="pol_test",
        capability_score=_MEASURED_SCORE,
        coverage_factor=_MEASURED_EVIDENCE.coverage_factor,
        score_evidence=_MEASURED_EVIDENCE,
        gates=gates,
        computed_at_timestamp_ns=1000,
    )
    ledger.append(receipt)
    registry = ClaimRegistry(tmp_path / "registry")

    minted, reason, claim = registry.verify_ledger_and_issue_claim(
        ledger=ledger,
        receipt_digest=receipt.receipt_digest,
        gates=gates,
        capability_score=_MEASURED_SCORE,
        live_realization_verified=False,
    )

    assert minted is False
    assert claim is None
    assert "G0ConstitutionalIntegrity" in reason
    assert "NOT_APPLICABLE" in reason
    assert "REQUIRED_BLOCKING_GATE_UNEVALUATED" in reason
    # nothing was appended: a refused claim leaves no registry row
    assert registry.registry_file.exists() is False


def test_canonical_live_fold_verdict_is_unchanged() -> None:
    """#435 (c): the published live-fold vector keeps its 45d006b6 verdict.

    At 45d006b6 this vector's verdict was READY_NOT_CLAIMED with no failing
    position, no hard failure and no evidence gap (the fold is admitted because
    its descriptor declares `Required`). #435 must not downgrade it and must not
    promote it: the fold stays admitted and still not established.
    """
    published = json.loads(CANONICAL_LIVE_FOLD_FIXTURE.read_text(encoding="utf-8"))
    gates = GateVector(**published["receipt"]["gates"])
    assert gates.g8_prospective_shadow == GateState.NOT_APPLICABLE

    verdict = gates.readiness()
    assert verdict.status == ReadinessStatus.Certified
    assert verdict.status_string() == "READY_NOT_CLAIMED"
    assert verdict.failing_positions == ()
    assert verdict.hard_failures == ()
    assert verdict.evidence_gaps == ()
    assert verdict.unevaluated_blocking == ()
    assert gates.all_pass() is True

    fold_eval = next(ev for ev in verdict.evaluations if ev.state == GateState.NOT_APPLICABLE)
    assert fold_eval.descriptor.vector_field == "g8_prospective_shadow"
    assert fold_eval.descriptor.requirement == "Required"
    assert fold_eval.holds() is True
    # admitted is not the same claim as established
    assert fold_eval.state.is_pass() is False

pytestmark = pytest.mark.slow  # #469: tape/engine file, fast loop excludes via -m "not slow"
