"""M4-02 costed ablation model and native baseline replay tests.

The unit tests fix the delta arithmetic and the honesty gates on hand-computed
numbers. The final test replays the real authorised development tape through the
native NautilusTrader path, so the published baseline is reproducibly measured
rather than mocked; it skips only when the tape is absent.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest

from v8_next.evaluation.ablations import (
    BASELINE_ARM_ID,
    MICROSTRUCTURE_UNSUPPORTED,
    MICROSTRUCTURE_UNSUPPORTED_TOKEN,
    NOT_RUN_ADAPTIVE_ORDERS,
    V9_INTERVENTION_KIND,
    AblationReport,
    ArmResult,
    CostedResultError,
    Intervention,
    Provenance,
    bootstrap_mean_ci,
    build_report,
    compute_delta,
    main,
    publish_costed_result,
    run_native_baseline,
)

#: The authorised development tape is committed inside this checkout; the
#: optional-dataset resolver points at the owner's external tree, so the test
#: binds the tape path the benchmark actually authorises.
DEV_TAPE = Path(__file__).resolve().parents[1] / "research" / "tape" / "btcusdt-1h-2025Q1"


def _provenance(tape_sha: str = "a" * 64) -> Provenance:
    return Provenance(
        tape_sha256=tape_sha,
        tape_manifest_sha256="b" * 64,
        config_sha256="c" * 64,
        engine="nautilus_trader.BacktestEngine",
        engine_version="2.0.0rc4",
        execution_profile="baseline",
        execution_profile_digest="d" * 64,
        fee_model="maker_taker",
        maker_fee="0.0002",
        taker_fee="0.0005",
        funding_basis="TAPE_FUNDING_ROWS=1 COVERAGE=FULL",
        mark_basis="OHLC_CLOSE_ASOF_BOUNDED_HISTORICAL_MODEL_NOT_VENUE_MARK",
        intrabar_tie_basis="NAUTILUS_DEFAULT_ORDERING",
        slippage_basis="FILL_MODEL=default SLIPPED=False PROB_SLIPPAGE=0.0",
        partial_fills_basis="ORDER_FILL_REPORT_ROWS=2 PARTIAL_ORDERS=0",
    )


def _arm(
    *,
    arm_id: str = BASELINE_ARM_ID,
    intervention: Intervention | None = None,
    costed: bool = True,
    trades: int | None = 44,
    net_r: float | None = 0.5,
    net_pnl: float | None = 100.0,
    max_drawdown: float | None = 0.1,
    hit_rate: float | None = 0.5,
    fee_total: float | None = 12.5,
    funding_total: float | None = -1.25,
    ci_low: float | None = 0.1,
    ci_high: float | None = 0.9,
    samples: tuple[float, ...] = (0.0, 1.0),
    unsupported: tuple[str, ...] = (),
    provenance: Provenance | None = None,
    arm_kind: str | None = None,
    preregistration_digest: str | None = None,
) -> ArmResult:
    return ArmResult(
        arm_id=arm_id,
        intervention=intervention,
        costed=costed,
        trades=trades,
        net_r=net_r,
        net_pnl=net_pnl,
        max_drawdown=max_drawdown,
        hit_rate=hit_rate,
        fee_total=fee_total,
        funding_total=funding_total,
        ci_low=ci_low,
        ci_high=ci_high,
        ci_method="BOOTSTRAP_PERCENTILE_95_OF_MEAN (resamples=2000)",
        provenance=(
            _provenance() if (costed and provenance is None) else provenance
        ),
        net_r_samples=samples,
        risk_basis="BRACKET_RISK",
        open_campaigns=1,
        unsupported=unsupported,
        arm_kind=arm_kind,
        preregistration_digest=preregistration_digest,
    )


def _supplied_for_all() -> dict[Intervention, ArmResult]:
    supplied: dict[Intervention, ArmResult] = {}
    for index, intervention in enumerate(Intervention):
        supplied[intervention] = _arm(
            arm_id=f"{intervention.value.lower()}_arm",
            intervention=intervention,
            net_r=1.0 + index * 0.1,
            net_pnl=200.0 + index * 10.0,
        )
    return supplied


def test_delta_arithmetic_on_hand_computed_numbers() -> None:
    baseline = _arm(net_r=2.0, net_pnl=200.0, max_drawdown=0.10, hit_rate=0.5,
                    samples=(1.0, 3.0))
    treatment = _arm(
        arm_id="belief_arm",
        intervention=Intervention.BELIEF,
        net_r=1.25,
        net_pnl=150.0,
        max_drawdown=0.05,
        hit_rate=0.75,
        samples=(1.0, 1.5),
    )

    delta = compute_delta(baseline, treatment)

    assert delta.delta_net_r == pytest.approx(-0.75)
    assert delta.delta_net_pnl == pytest.approx(-50.0)
    assert delta.delta_max_drawdown == pytest.approx(-0.05)
    assert delta.delta_hit_rate == pytest.approx(0.25)
    assert delta.delta_ci_low is not None and delta.delta_ci_high is not None
    assert delta.delta_ci_low <= delta.delta_net_r <= delta.delta_ci_high
    assert delta.ci_method is not None and "BOOTSTRAP" in delta.ci_method


def test_costed_baseline_and_treatment_produce_delta_with_ci() -> None:
    baseline = _arm(net_r=0.25, samples=(-0.5, 0.5, 0.75))
    treatment = _arm(
        arm_id="entry_timing_arm",
        intervention=Intervention.ENTRY_TIMING,
        net_r=0.40,
        samples=(-0.2, 0.6, 0.8),
    )
    supplied = {Intervention.ENTRY_TIMING: treatment}

    report = build_report(baseline=baseline, supplied_arms=supplied)

    assert report.status == "PARTIAL"
    assert len(report.deltas) == 1
    delta = report.deltas[0]
    assert delta.intervention is Intervention.ENTRY_TIMING
    assert delta.delta_net_r == pytest.approx(0.15)
    assert delta.delta_ci_low is not None
    assert delta.delta_ci_high is not None
    assert delta.delta_ci_low <= delta.delta_net_r <= delta.delta_ci_high


def test_not_run_arm_produces_no_delta_and_is_counted_unsupported() -> None:
    report = build_report(baseline=_arm(), supplied_arms={})

    assert report.status == "PARTIAL"
    assert report.deltas == ()
    assert len(report.not_run) == len(Intervention)
    unsupported = dict(report.unsupported)
    for intervention in Intervention:
        arm_id = f"{intervention.value.lower()}_arm"
        assert arm_id in report.not_run
        assert unsupported[arm_id] == (NOT_RUN_ADAPTIVE_ORDERS,)
        arm = next(arm for arm in report.arms if arm.arm_id == arm_id)
        assert arm.costed is False
        assert arm.trades is None
        assert arm.net_r is None
        assert arm.net_pnl is None


def test_report_cannot_be_complete_while_an_intervention_is_not_run() -> None:
    arms = tuple(
        _arm(arm_id=f"{intervention.value.lower()}_arm", intervention=intervention)
        for intervention in Intervention
    )
    deltas = tuple(
        compute_delta(_arm(), arm) for arm in arms
    )
    not_run_arm = _arm(
        arm_id="belief_arm",
        intervention=Intervention.BELIEF,
        costed=False,
        trades=None,
        net_r=None,
        net_pnl=None,
        max_drawdown=None,
        hit_rate=None,
        fee_total=None,
        funding_total=None,
        ci_low=None,
        ci_high=None,
        provenance=None,
        samples=(),
        unsupported=(NOT_RUN_ADAPTIVE_ORDERS,),
    )
    arms_with_gap = (not_run_arm, *arms[1:])

    with pytest.raises(ValueError, match="NOT_RUN"):
        AblationReport(
            baseline=_arm(),
            arms=arms_with_gap,
            deltas=deltas,
            status="COMPLETE",
            unsupported=(),
            microstructure=MICROSTRUCTURE_UNSUPPORTED,
        )


def test_complete_report_requires_every_intervention_costed_with_delta_ci() -> None:
    report = build_report(baseline=_arm(), supplied_arms=_supplied_for_all())

    assert report.status == "COMPLETE"
    assert {delta.intervention for delta in report.deltas} == set(Intervention)
    for delta in report.deltas:
        assert delta.delta_net_r is not None
        assert delta.delta_ci_low is not None
        assert delta.delta_ci_high is not None
    assert report.not_run == ()


def test_fee_funding_and_provenance_are_carried_through_json() -> None:
    arm = _arm(fee_total=37.91535683, funding_total=0.70494565)
    report = build_report(baseline=arm, supplied_arms={})

    payload = json.loads(report.to_json())
    baseline = payload["baseline"]

    assert baseline["fee_total"] == pytest.approx(37.91535683)
    assert baseline["funding_total"] == pytest.approx(0.70494565)
    provenance = baseline["provenance"]
    assert provenance["fee_model"] == "maker_taker"
    assert provenance["taker_fee"] == "0.0005"
    assert provenance["funding_basis"]
    assert provenance["mark_basis"].startswith("OHLC_CLOSE_ASOF_BOUNDED")
    assert provenance["intrabar_tie_basis"]
    assert provenance["slippage_basis"]
    assert provenance["partial_fills_basis"]

    restored = ArmResult.from_dict(baseline)
    assert restored.fee_total == pytest.approx(37.91535683)
    assert restored.funding_total == pytest.approx(0.70494565)
    assert restored.provenance is not None
    assert restored.provenance.funding_basis == provenance["funding_basis"]


def test_missing_microstructure_is_unsupported_never_zero() -> None:
    report = build_report(baseline=_arm(), supplied_arms={})

    assert report.microstructure == MICROSTRUCTURE_UNSUPPORTED
    assert report.microstructure.startswith("UNSUPPORTED")
    assert MICROSTRUCTURE_UNSUPPORTED_TOKEN in _arm(
        unsupported=(MICROSTRUCTURE_UNSUPPORTED_TOKEN,)
    ).unsupported

    payload = report.to_dict()
    for arm in payload["arms"]:
        assert arm["costed"] is False
        for field in ("net_r", "net_pnl", "max_drawdown", "hit_rate", "fee_total",
                      "funding_total", "ci_low", "ci_high"):
            assert arm[field] is None
    assert "UNSUPPORTED" in json.dumps(payload)


def test_json_is_deterministic() -> None:
    first = build_report(baseline=_arm(), supplied_arms=_supplied_for_all())
    second = build_report(baseline=_arm(), supplied_arms=_supplied_for_all())

    assert first.to_json() == second.to_json()
    assert json.dumps(first.to_dict(), sort_keys=True) == json.dumps(
        second.to_dict(), sort_keys=True
    )


def test_no_baseline_is_not_run_and_produces_no_delta() -> None:
    supplied_only = build_report(baseline=None, supplied_arms=_supplied_for_all())
    nothing_run = build_report(baseline=None, supplied_arms={})

    assert supplied_only.status == "NOT_RUN"
    assert supplied_only.deltas == ()
    # Supplied costed arms stay costed; what is absent is the baseline, so no
    # delta may be computed and the report cannot claim COMPLETE.
    assert supplied_only.not_run == ()
    assert nothing_run.status == "NOT_RUN"
    assert nothing_run.deltas == ()
    assert len(nothing_run.not_run) == len(Intervention)


def test_delta_refuses_an_uncosted_arm() -> None:
    uncosted = _arm(costed=False, trades=None, net_r=None, net_pnl=None,
                    max_drawdown=None, hit_rate=None, fee_total=None,
                    funding_total=None, ci_low=None, ci_high=None, samples=())

    with pytest.raises(ValueError, match="both arms costed"):
        compute_delta(_arm(), uncosted)


def test_delta_refuses_arms_measured_on_different_tapes() -> None:
    baseline = _arm(provenance=_provenance("a" * 64))
    treatment = _arm(
        arm_id="belief_arm",
        intervention=Intervention.BELIEF,
        provenance=_provenance("b" * 64),
    )

    with pytest.raises(ValueError, match="mismatched: tape"):
        compute_delta(baseline, treatment)


def test_delta_refuses_arms_measured_on_a_different_engine_or_profile() -> None:
    baseline = _arm()
    other_engine = replace(_provenance(), engine="some_other_engine", engine_version="9.9.9")
    other_profile = replace(_provenance(), execution_profile_digest="e" * 64)

    for provenance, mismatch in (
        (other_engine, "engine, engine_version"),
        (other_profile, "execution_profile_digest"),
    ):
        treatment = _arm(
            arm_id="entry_timing_arm",
            intervention=Intervention.ENTRY_TIMING,
            provenance=provenance,
        )
        with pytest.raises(ValueError, match=f"mismatched: {mismatch}"):
            compute_delta(baseline, treatment)


def test_delta_accepts_arms_measured_on_one_basis() -> None:
    delta = compute_delta(_arm(), _arm(
        arm_id="belief_arm",
        intervention=Intervention.BELIEF,
        net_r=0.7,
    ))
    assert delta.delta_net_r == pytest.approx(0.2)


# --------------------------------------------------------------------------- #
# the costed-result producer (shared contract)
# --------------------------------------------------------------------------- #
REGISTERED_COSTS = {
    "fee_bps_per_side": "5",
    "funding_bps_per_day": "1",
    "slippage_bps_per_side": "2",
    "funding_included": True,
}
PREREGISTERED_ARM_ID = "v9-adaptive-policy"


def _write_preregistration(root: Path, *, costs: dict[str, object] | None = None) -> Path:
    path = root / "reports" / "evaluation" / "preregistration.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "hypothesis_id": "V9-H1-test",
                "dataset_role": "DEVELOPMENT",
                "intervention": {
                    "arm_id": PREREGISTERED_ARM_ID,
                    "kind": V9_INTERVENTION_KIND,
                },
                "costs": REGISTERED_COSTS if costs is None else costs,
            }
        ),
        encoding="utf-8",
    )
    return path


def _prereg_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _v9_arm(preregistration: Path, **overrides: object) -> ArmResult:
    defaults: dict[str, object] = {
        "arm_id": PREREGISTERED_ARM_ID,
        "arm_kind": V9_INTERVENTION_KIND,
        "preregistration_digest": _prereg_digest(preregistration),
    }
    defaults.update(overrides)
    return _arm(**defaults)  # type: ignore[arg-type]


def _publish(arm: ArmResult | None, preregistration: Path, **overrides: object) -> Path:
    arguments: dict[str, object] = {
        "costs": REGISTERED_COSTS,
        "net_return": "1.25",
        "verdict": "PASS",
        "preregistration_path": preregistration,
        "output_path": preregistration.parent / "costed_result.json",
    }
    arguments.update(overrides)
    return publish_costed_result(arm, **arguments)  # type: ignore[arg-type]


def test_publish_refuses_an_absent_or_uncosted_arm_and_writes_nothing(tmp_path: Path) -> None:
    prereg = _write_preregistration(tmp_path)
    out = prereg.parent / "costed_result.json"

    with pytest.raises(CostedResultError, match="ARM_MISSING"):
        _publish(None, prereg)
    with pytest.raises(CostedResultError, match="NOT_COSTED"):
        _publish(_v9_arm(prereg, costed=False), prereg)
    assert not out.exists()


def test_publish_refuses_the_v8_baseline_arm(tmp_path: Path) -> None:
    prereg = _write_preregistration(tmp_path)
    baseline = _arm(
        arm_id=BASELINE_ARM_ID,
        arm_kind="V8_BASELINE",
        preregistration_digest=_prereg_digest(prereg),
    )

    with pytest.raises(CostedResultError, match="NOT_V9_INTERVENTION"):
        _publish(baseline, prereg)
    assert not (prereg.parent / "costed_result.json").exists()


def test_publish_refuses_a_digest_or_cost_mismatch(tmp_path: Path) -> None:
    prereg = _write_preregistration(tmp_path)
    out = prereg.parent / "costed_result.json"

    with pytest.raises(CostedResultError, match="DIGEST_MISMATCH"):
        _publish(_v9_arm(prereg, preregistration_digest="0" * 64), prereg)
    with pytest.raises(CostedResultError, match="COSTS_DIFFER_FROM_REGISTRATION"):
        _publish(
            _v9_arm(prereg),
            prereg,
            costs={**REGISTERED_COSTS, "fee_bps_per_side": "6"},
        )
    with pytest.raises(CostedResultError, match="PREREGISTRATION_MISSING"):
        _publish(_v9_arm(prereg), tmp_path / "absent.json")
    assert not out.exists()


def test_publish_writes_a_digest_bound_result_the_verdict_accepts(tmp_path: Path) -> None:
    from v8_next.evaluation.verdict import DimensionStatus, _economic

    prereg = _write_preregistration(tmp_path)
    (prereg.parent / "access_report.json").write_text(
        json.dumps({"permitted_roles": ["DEVELOPMENT"]}), encoding="utf-8"
    )

    written = _publish(
        _v9_arm(prereg),
        prereg,
        net_return="1.25",
        verdict="pass",
        uncertainty={"ci_low": "0.10", "ci_high": "2.00"},
    )

    payload = json.loads(written.read_text(encoding="utf-8"))
    assert payload["preregistration_digest"] == _prereg_digest(prereg)
    assert payload["dataset_role"] == "DEVELOPMENT"
    assert payload["net_return"] == "1.25"
    assert payload["verdict"] == "PASS"
    assert payload["costs"] == {
        "fee_bps_per_side": "5",
        "funding_bps_per_day": "1",
        "slippage_bps_per_side": "2",
        "funding_included": True,
    }
    assert payload["uncertainty"] == {"ci_low": "0.10", "ci_high": "2.00"}

    verdict = _economic(tmp_path)
    assert verdict.status is DimensionStatus.PASS


def test_bootstrap_ci_is_deterministic_and_exact_for_constant_samples() -> None:
    assert bootstrap_mean_ci([1.0, 1.0, 1.0]) == (1.0, 1.0)
    first = bootstrap_mean_ci([-1.0, 0.5, 2.0, 0.25])
    second = bootstrap_mean_ci([-1.0, 0.5, 2.0, 0.25])
    assert first == second
    low, high = first
    assert low is not None and high is not None and low <= high
    assert bootstrap_mean_ci([1.0]) == (None, None)


def test_supplied_arm_with_wrong_intervention_is_rejected() -> None:
    wrong = _arm(arm_id="belief_arm", intervention=Intervention.ALLOCATION)

    with pytest.raises(ValueError, match="declares"):
        build_report(
            baseline=_arm(),
            supplied_arms={Intervention.BELIEF: wrong},
        )


def test_supplied_uncosted_arm_stays_not_run_with_reason() -> None:
    uncosted = _arm(
        arm_id="belief_arm",
        intervention=Intervention.BELIEF,
        costed=False,
        trades=None,
        net_r=None,
        net_pnl=None,
        max_drawdown=None,
        hit_rate=None,
        fee_total=None,
        funding_total=None,
        ci_low=None,
        ci_high=None,
        samples=(),
        unsupported=("SUPPLIED_PLACEHOLDER",),
    )

    report = build_report(
        baseline=_arm(), supplied_arms={Intervention.BELIEF: uncosted}
    )
    arm = next(arm for arm in report.arms if arm.intervention is Intervention.BELIEF)

    assert arm.costed is False
    assert report.deltas == ()
    assert "SUPPLIED_ARM_NOT_COSTED" in arm.unsupported
    assert "SUPPLIED_PLACEHOLDER" in arm.unsupported


@pytest.mark.skipif(not (DEV_TAPE / "tape.jsonl").is_file(), reason="dev tape absent")
def test_real_native_baseline_replay_is_costed_and_provenanced() -> None:
    arm = run_native_baseline(tape=DEV_TAPE, bootstrap_resamples=200)

    assert arm.costed is True
    assert arm.arm_id == BASELINE_ARM_ID
    assert arm.provenance is not None
    assert arm.trades is not None and arm.trades > 0
    assert arm.fee_total is not None and arm.fee_total > 0
    assert arm.funding_total is not None
    assert arm.net_pnl is not None
    assert arm.net_r is not None
    assert arm.ci_low is not None and arm.ci_high is not None
    assert arm.ci_low <= arm.net_r <= arm.ci_high
    assert arm.provenance.engine_version == "2.0.0rc4"
    assert len(arm.provenance.tape_sha256) == 64
    assert len(arm.provenance.config_sha256) == 64
    assert MICROSTRUCTURE_UNSUPPORTED_TOKEN in arm.unsupported
    assert arm.provenance.funding_basis.endswith(
        "DUAL_RUN_ISOLATION=VALID"
    ) or "DUAL_RUN_ISOLATION=DIVERGED" in arm.provenance.funding_basis

    # The provenance hash is the hash of the tape bytes actually replayed.
    tape_file = DEV_TAPE / "tape.jsonl"
    assert arm.provenance.tape_sha256 == hashlib.sha256(tape_file.read_bytes()).hexdigest()

    report = build_report(baseline=arm, supplied_arms={})
    assert report.status == "PARTIAL"
    assert report.deltas == ()
    assert len(report.not_run) == len(Intervention)


def test_cli_no_baseline_publishes_not_run_json(tmp_path: Path) -> None:
    out = tmp_path / "ablation.json"

    exit_code = main(["--tape", "unused", "--out", str(out), "--no-baseline"])

    assert exit_code == 0
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["status"] == "NOT_RUN"
    assert payload["baseline"] is None
    assert payload["deltas"] == []
    assert payload["invocation"]["module"] == "v8_next.evaluation.ablations"
    assert payload["invocation"]["argv"][:2] == ["--tape", "unused"]


def test_cli_supplied_costed_arm_is_carried_without_baseline(tmp_path: Path) -> None:
    arm_path = tmp_path / "belief.json"
    arm_path.write_text(json.dumps(_arm(
        arm_id="belief_arm", intervention=Intervention.BELIEF
    ).to_dict()), encoding="utf-8")
    out = tmp_path / "ablation.json"

    exit_code = main(
        [
            "--tape",
            "unused",
            "--out",
            str(out),
            "--no-baseline",
            "--supplied-arm",
            f"BELIEF={arm_path}",
        ]
    )

    assert exit_code == 0
    payload = json.loads(out.read_text(encoding="utf-8"))
    belief = next(arm for arm in payload["arms"] if arm["intervention"] == "BELIEF")
    assert belief["costed"] is True
    assert belief["fee_total"] == pytest.approx(12.5)
    assert payload["deltas"] == []


def test_cli_records_process_measurement_when_asked(tmp_path: Path) -> None:
    out = tmp_path / "measured.json"

    assert main(["--tape", "unused", "--out", str(out), "--no-baseline",
                 "--record-measurement"]) == 0

    payload = json.loads(out.read_text(encoding="utf-8"))
    measurement = payload["measurement"]
    assert measurement["wall_seconds"] >= 0.0
    assert measurement["peak_rss_bytes"] > 0
    assert measurement["peak_rss_basis"] in (
        "ru_maxrss_bytes_macos",
        "ru_maxrss_kilobytes_converted",
    )
