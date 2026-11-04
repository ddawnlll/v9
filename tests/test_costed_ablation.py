"""M4-02: costed single-intervention ablation contract is descriptive and fail-closed."""

from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError

from v8_next.evaluation.costed_ablation import (
    AblationReport,
    ClaimKind,
    CostedRun,
    CostProvenance,
    Interval,
    Intervention,
    Microstructure,
    ProvenanceStatus,
    RunStatus,
    build_ablation_report,
    declared_blocked_report,
    to_json,
)

D = Decimal
CODE = "b" * 40
CFG = "c" * 64
DATA = "d" * 64
PREREG = "e" * 64
ENGINE = "nautilus-trader-2.0.0rc4"


def costs(
    *, fee: ProvenanceStatus = ProvenanceStatus.OBSERVED,
    funding: ProvenanceStatus = ProvenanceStatus.OBSERVED,
    slippage: ProvenanceStatus = ProvenanceStatus.OBSERVED,
    micro: Microstructure = Microstructure.BARS_ONLY,
) -> CostProvenance:
    return CostProvenance(
        fee=fee,
        funding=funding,
        slippage=slippage,
        partial_fill_model="size_aware",
        stop_first_tie=True,
        microstructure=micro,
    )


def interval(low: str, high: str) -> Interval:
    return Interval(low=D(low), high=D(high), samples=1_000, seed=7)


def measured(
    intervention: Intervention,
    *,
    net_pnl: str = "100",
    net_r: str = "1.0",
    drawdown: str = "20",
    fees: str = "3",
    funding: str = "-1",
    ci: tuple[str, str] = ("90", "110"),
    data: str = DATA,
    engine: str = ENGINE,
    cost: CostProvenance | None = None,
) -> CostedRun:
    return CostedRun(
        run_id=f"{intervention.value.lower()}-measured",
        intervention=intervention,
        status=RunStatus.MEASURED,
        code_sha=CODE,
        config_sha256=CFG,
        dataset_sha256=data,
        engine_version=engine,
        costs=costs() if cost is None else cost,
        trades=40,
        fills=48,
        gross_pnl=D("110"),
        net_pnl=D(net_pnl),
        net_r=D(net_r),
        max_drawdown=D(drawdown),
        fees=D(fees),
        funding=D(funding),
        net_pnl_ci=interval(*ci),
    )


def not_run(intervention: Intervention, reason: str = "NO_FINALIST") -> CostedRun:
    return CostedRun(
        run_id=f"{intervention.value.lower()}-not-run",
        intervention=intervention,
        status=RunStatus.NOT_RUN,
        code_sha="0" * 40,
        config_sha256="0" * 64,
        dataset_sha256="0" * 64,
        engine_version="NOT_RUN",
        costs=costs(
            fee=ProvenanceStatus.MISSING,
            funding=ProvenanceStatus.MISSING,
            slippage=ProvenanceStatus.MISSING,
            micro=Microstructure.UNSUPPORTED,
        ),
        reasons=(reason,),
    )


def test_complete_ablation_is_descriptive_only() -> None:
    report = build_ablation_report(
        (
            measured(Intervention.BASELINE),
            measured(Intervention.ENTRY_TIMING, net_pnl="120"),
            measured(Intervention.POSITION_THESIS, net_pnl="80"),
        ),
        preregistration_digest=PREREG,
    )
    assert report.status is RunStatus.MEASURED
    assert report.claim is ClaimKind.DESCRIPTIVE_DELTA_ONLY
    assert report.blockers == ()
    assert {row.metric for row in report.deltas} == {
        "net_pnl",
        "net_r",
        "max_drawdown",
        "fees",
        "funding",
    }
    entry = next(
        row
        for row in report.deltas
        if row.intervention is Intervention.ENTRY_TIMING and row.metric == "net_pnl"
    )
    assert entry.delta == D("20")
    assert (entry.delta_ci_low, entry.delta_ci_high) == (D("-20"), D("20"))


def test_drawdown_delta_is_direction_preserving() -> None:
    report = build_ablation_report(
        (
            measured(Intervention.BASELINE, drawdown="20"),
            measured(Intervention.PORTFOLIO_ALLOCATION, drawdown="12"),
        ),
        preregistration_digest=PREREG,
    )
    row = next(item for item in report.deltas if item.metric == "max_drawdown")
    assert row.delta == D("-8")


def test_measured_arm_rejects_incomplete_cost_provenance() -> None:
    with pytest.raises(ValidationError, match="provenance"):
        measured(
            Intervention.MARKET_BELIEF,
            cost=costs(fee=ProvenanceStatus.MISSING),
        )
    with pytest.raises(ValidationError, match="provenance"):
        measured(
            Intervention.EXPERT_ROUTING,
            cost=costs(micro=Microstructure.UNSUPPORTED),
        )


def test_not_run_arm_blocks_and_contributes_no_numbers() -> None:
    report = build_ablation_report(
        (measured(Intervention.BASELINE), not_run(Intervention.ENTRY_TIMING)),
        preregistration_digest=PREREG,
    )
    assert report.status is RunStatus.BLOCKED
    assert report.deltas == ()
    assert any("ENTRY_TIMING:NO_FINALIST" in item for item in report.blockers)


def test_measured_requires_a_confidence_interval() -> None:
    with pytest.raises(ValidationError, match="confidence interval"):
        CostedRun(
            run_id="x",
            intervention=Intervention.BASELINE,
            status=RunStatus.MEASURED,
            code_sha=CODE,
            config_sha256=CFG,
            dataset_sha256=DATA,
            engine_version=ENGINE,
            costs=costs(),
            trades=1,
            fills=1,
            gross_pnl=D("1"),
            net_pnl=D("1"),
            net_r=D("1"),
            max_drawdown=D("1"),
            fees=D("0"),
            funding=D("0"),
        )


def test_not_run_cannot_carry_numbers_or_lack_reasons() -> None:
    base = dict(
        run_id="x",
        intervention=Intervention.BASELINE,
        status=RunStatus.NOT_RUN,
        code_sha="0" * 40,
        config_sha256="0" * 64,
        dataset_sha256="0" * 64,
        engine_version="NOT_RUN",
        costs=costs(),
        reasons=("X",),
    )
    with pytest.raises(ValidationError, match="cannot carry economic numbers"):
        CostedRun(**{**base, "net_pnl": D("1")})
    with pytest.raises(ValidationError, match="must name"):
        CostedRun(**{**base, "reasons": ()})


def test_exactly_one_baseline_and_one_arm_per_intervention() -> None:
    with pytest.raises(ValueError, match="exactly one BASELINE"):
        build_ablation_report(
            (measured(Intervention.BASELINE), measured(Intervention.BASELINE)),
            preregistration_digest=PREREG,
        )
    with pytest.raises(ValueError, match="at least one"):
        build_ablation_report(
            (measured(Intervention.BASELINE),), preregistration_digest=PREREG
        )
    with pytest.raises(ValueError, match="duplicate intervention"):
        build_ablation_report(
            (
                measured(Intervention.BASELINE),
                measured(Intervention.ENTRY_TIMING),
                measured(Intervention.ENTRY_TIMING),
            ),
            preregistration_digest=PREREG,
        )


def test_arms_must_share_dataset_config_and_engine() -> None:
    with pytest.raises(ValueError, match="one dataset"):
        build_ablation_report(
            (
                measured(Intervention.BASELINE),
                measured(Intervention.ENTRY_TIMING, data="f" * 64),
            ),
            preregistration_digest=PREREG,
        )
    with pytest.raises(ValueError, match="one native engine"):
        build_ablation_report(
            (
                measured(Intervention.BASELINE),
                measured(Intervention.ENTRY_TIMING, engine="other"),
            ),
            preregistration_digest=PREREG,
        )


def test_interval_and_claim_vocabulary_are_closed() -> None:
    with pytest.raises(ValidationError):
        interval("10", "5")
    assert {item.value for item in ClaimKind} == {"NONE", "DESCRIPTIVE_DELTA_ONLY"}
    assert {item.value for item in Intervention} == {
        "BASELINE",
        "MARKET_BELIEF",
        "EXPERT_ROUTING",
        "ENTRY_TIMING",
        "POSITION_THESIS",
        "PORTFOLIO_ALLOCATION",
    }


def test_declared_blocked_report_is_honest() -> None:
    report = declared_blocked_report(reason="NO_FINALIST_FROM_M3_07_AND_NO_NATIVE_REPLAY")
    assert report.status is RunStatus.BLOCKED
    assert report.claim is ClaimKind.NONE
    assert report.deltas == ()
    assert len(report.interventions) == 5
    assert len(report.blockers) >= 6
    assert all(run.status is RunStatus.NOT_RUN for run in report.interventions)


def test_json_roundtrip_and_tamper_detection() -> None:
    report = build_ablation_report(
        (
            measured(Intervention.BASELINE),
            measured(Intervention.MARKET_BELIEF, net_pnl="90"),
        ),
        preregistration_digest=PREREG,
    )
    restored = AblationReport.model_validate_json(to_json(report))
    assert restored == report
    payload = report.model_dump(mode="json")
    payload["status"] = "MEASURED"
    payload["claim"] = "DESCRIPTIVE_DELTA_ONLY"
    payload["digest"] = "0" * 64
    with pytest.raises(ValidationError, match="digest"):
        AblationReport.model_validate(payload)
