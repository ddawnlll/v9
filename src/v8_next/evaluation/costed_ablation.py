"""Costed native finalist replay and single-intervention ablations (M4-02).

This module does **not** run an engine and does not assert an edge. It defines the
evidence contract for measuring one intervention at a time against a costed native
baseline, and it fails closed on every missing ingredient:

* a run is ``MEASURED`` only when Nautilus produced the fills and fee, funding and
  slippage provenance is ``OBSERVED`` or ``MODELED``;
* missing provenance, unsupported microstructure or any ``NOT_RUN``/``BLOCKED`` arm
  makes the report ``BLOCKED`` with no deltas and claim ``NONE``;
* the only claim a complete report may carry is ``DESCRIPTIVE_DELTA_ONLY``. There is
  no API that returns "superior" or a profitability verdict.

Representative (not economic) numbers from a synthetic fixture are used in tests; the
repository's own artifact is generated as ``BLOCKED`` because M3-07 has no economic
finalist and no authorized costed native replay exists.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from decimal import Decimal
from enum import StrEnum
from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

SCHEMA_VERSION: Final[str] = "v9-costed-ablation-v1"
ZERO = Decimal("0")


class Intervention(StrEnum):
    BASELINE = "BASELINE"
    MARKET_BELIEF = "MARKET_BELIEF"
    EXPERT_ROUTING = "EXPERT_ROUTING"
    ENTRY_TIMING = "ENTRY_TIMING"
    POSITION_THESIS = "POSITION_THESIS"
    PORTFOLIO_ALLOCATION = "PORTFOLIO_ALLOCATION"


INTERVENTIONS: tuple[Intervention, ...] = tuple(Intervention)


class ProvenanceStatus(StrEnum):
    OBSERVED = "OBSERVED"
    MODELED = "MODELED"
    MISSING = "MISSING"
    UNSUPPORTED = "UNSUPPORTED"


class Microstructure(StrEnum):
    L2_DEPTH = "L2_DEPTH"
    L1_QUOTES = "L1_QUOTES"
    TICKS = "TICKS"
    BARS_ONLY = "BARS_ONLY"
    UNSUPPORTED = "UNSUPPORTED"


class RunStatus(StrEnum):
    MEASURED = "MEASURED"
    NOT_RUN = "NOT_RUN"
    BLOCKED = "BLOCKED"


class ClaimKind(StrEnum):
    NONE = "NONE"
    DESCRIPTIVE_DELTA_ONLY = "DESCRIPTIVE_DELTA_ONLY"


class Interval(BaseModel):
    """Deterministic percentile bootstrap interval supplied by the runner."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    low: Decimal
    high: Decimal
    level: Decimal = Decimal("0.95")
    samples: int = Field(ge=1)
    seed: int = Field(ge=0)
    method: Literal["percentile_bootstrap"] = "percentile_bootstrap"

    @model_validator(mode="after")
    def _ordered(self) -> Interval:
        for value in (self.low, self.high, self.level):
            if not value.is_finite():
                raise ValueError("interval values must be finite")
        if self.low > self.high:
            raise ValueError("interval low cannot exceed high")
        if not (ZERO < self.level < Decimal("1")):
            raise ValueError("interval level must be in (0, 1)")
        return self


class CostProvenance(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    fee: ProvenanceStatus
    funding: ProvenanceStatus
    slippage: ProvenanceStatus
    partial_fill_model: str = Field(min_length=1)
    stop_first_tie: bool
    microstructure: Microstructure

    def complete(self) -> bool:
        allowed = {ProvenanceStatus.OBSERVED, ProvenanceStatus.MODELED}
        return (
            self.fee in allowed
            and self.funding in allowed
            and self.slippage in allowed
            and self.microstructure is not Microstructure.UNSUPPORTED
        )

    def blockers(self) -> tuple[str, ...]:
        names = ("fee", "funding", "slippage")
        missing = tuple(
            f"COST_PROVENANCE_MISSING:{name}"
            for name in names
            if getattr(self, name)
            in {ProvenanceStatus.MISSING, ProvenanceStatus.UNSUPPORTED}
        )
        micro = (
            ("MICROSTRUCTURE_UNSUPPORTED",)
            if self.microstructure is Microstructure.UNSUPPORTED
            else ()
        )
        return (*missing, *micro)


class CostedRun(BaseModel):
    """One arm. A non-measured arm carries no numbers at all, by construction."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str = Field(min_length=1)
    intervention: Intervention
    status: RunStatus
    code_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    config_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    dataset_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    engine_version: str = Field(min_length=1)
    costs: CostProvenance
    trades: int | None = Field(default=None, ge=0)
    fills: int | None = Field(default=None, ge=0)
    gross_pnl: Decimal | None = None
    net_pnl: Decimal | None = None
    net_r: Decimal | None = None
    max_drawdown: Decimal | None = Field(default=None, ge=ZERO)
    fees: Decimal | None = Field(default=None, ge=ZERO)
    funding: Decimal | None = None
    net_pnl_ci: Interval | None = None
    reasons: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _status_exclusive(self) -> CostedRun:
        metrics = (
            self.trades,
            self.fills,
            self.gross_pnl,
            self.net_pnl,
            self.net_r,
            self.max_drawdown,
            self.fees,
            self.funding,
        )
        for value in (self.gross_pnl, self.net_pnl, self.net_r, self.funding):
            if value is not None and not value.is_finite():
                raise ValueError("money values must be finite")
        if self.status is RunStatus.MEASURED:
            if any(value is None for value in metrics):
                raise ValueError("MEASURED requires trades, fills, PnL, DD, fees and funding")
            if self.net_pnl_ci is None:
                raise ValueError("MEASURED requires a confidence interval for net PnL")
            if not self.costs.complete():
                raise ValueError(
                    "MEASURED requires complete fee/funding/slippage provenance and "
                    "supported microstructure"
                )
            if self.reasons:
                raise ValueError("MEASURED cannot carry blockers")
            return self
        if any(value is not None for value in metrics) or self.net_pnl_ci is not None:
            raise ValueError(f"{self.status} cannot carry economic numbers")
        if not self.reasons:
            raise ValueError(f"{self.status} must name its blocking reasons")
        return self


class DeltaRow(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    intervention: Intervention
    metric: str
    baseline: Decimal
    value: Decimal
    delta: Decimal
    delta_ci_low: Decimal | None = None
    delta_ci_high: Decimal | None = None


class AblationReport(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: str = SCHEMA_VERSION
    status: RunStatus
    claim: ClaimKind
    preregistration_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    baseline: CostedRun
    interventions: tuple[CostedRun, ...]
    deltas: tuple[DeltaRow, ...] = ()
    blockers: tuple[str, ...] = ()
    digest: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def _no_unearned_claim(self) -> AblationReport:
        if self.status is not RunStatus.MEASURED and self.claim is not ClaimKind.NONE:
            raise ValueError("a blocked report cannot carry a claim")
        if self.status is RunStatus.MEASURED and self.claim is not ClaimKind.DESCRIPTIVE_DELTA_ONLY:
            raise ValueError("a measured ablation is descriptive only")
        if self.baseline.intervention is not Intervention.BASELINE:
            raise ValueError("baseline arm must be labelled BASELINE")
        expected = _report_digest(self)
        if expected != self.digest:
            raise ValueError("report digest does not re-derive from content")
        return self


def _canonical(payload: Mapping[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def _report_digest(report: AblationReport) -> str:
    body = report.model_dump(mode="json", exclude={"digest"})
    return hashlib.sha256(_canonical(body).encode()).hexdigest()


def _delta(
    intervention: CostedRun,
    baseline: CostedRun,
    metric: str,
    attribute: str,
) -> DeltaRow | None:
    base_value = getattr(baseline, attribute)
    value = getattr(intervention, attribute)
    if base_value is None or value is None:
        return None
    base_ci = baseline.net_pnl_ci
    value_ci = intervention.net_pnl_ci
    low = high = None
    if attribute == "net_pnl" and base_ci is not None and value_ci is not None:
        low = value_ci.low - base_ci.high
        high = value_ci.high - base_ci.low
    return DeltaRow(
        intervention=intervention.intervention,
        metric=metric,
        baseline=base_value,
        value=value,
        delta=value - base_value,
        delta_ci_low=low,
        delta_ci_high=high,
    )


def build_ablation_report(
    runs: Iterable[CostedRun], *, preregistration_digest: str
) -> AblationReport:
    """Assemble a fail-closed single-intervention ablation report."""
    run_list = list(runs)
    baselines = [run for run in run_list if run.intervention is Intervention.BASELINE]
    if len(baselines) != 1:
        raise ValueError(f"exactly one BASELINE arm is required, got {len(baselines)}")
    baseline = baselines[0]
    interventions = [
        run for run in run_list if run.intervention is not Intervention.BASELINE
    ]
    seen: set[Intervention] = set()
    for run in interventions:
        if run.intervention in seen:
            raise ValueError(f"duplicate intervention arm: {run.intervention.value}")
        seen.add(run.intervention)
    if not interventions:
        raise ValueError("at least one single-intervention ablation is required")
    for run in run_list:
        if run.status is not RunStatus.MEASURED or baseline.status is not RunStatus.MEASURED:
            # A blocked arm has no dataset/config/engine identity to compare; it is
            # reported through its typed reasons instead.
            continue
        if run.dataset_sha256 != baseline.dataset_sha256:
            raise ValueError("ablation arms must share one dataset")
        if run.config_sha256 != baseline.config_sha256:
            raise ValueError("ablation arms must share one config")
        if run.engine_version != baseline.engine_version:
            raise ValueError("ablation arms must share one native engine version")

    blockers: list[str] = []
    if baseline.status is not RunStatus.MEASURED:
        blockers.extend(f"BASELINE:{reason}" for reason in baseline.reasons)
    for run in interventions:
        if run.status is not RunStatus.MEASURED:
            blockers.extend(
                f"{run.intervention.value}:{reason}" for reason in run.reasons
            )
    blockers.extend(f"BASELINE:{reason}" for reason in baseline.costs.blockers())
    for run in interventions:
        blockers.extend(
            f"{run.intervention.value}:{reason}" for reason in run.costs.blockers()
        )

    deltas: list[DeltaRow] = []
    if not blockers:
        for run in interventions:
            for metric, attribute in (
                ("net_pnl", "net_pnl"),
                ("net_r", "net_r"),
                ("max_drawdown", "max_drawdown"),
                ("fees", "fees"),
                ("funding", "funding"),
            ):
                row = _delta(run, baseline, metric, attribute)
                if row is not None:
                    deltas.append(row)

    status = RunStatus.MEASURED if not blockers else RunStatus.BLOCKED
    claim = ClaimKind.DESCRIPTIVE_DELTA_ONLY if not blockers else ClaimKind.NONE
    ordered = tuple(sorted(interventions, key=lambda item: item.intervention.value))
    report = AblationReport.model_construct(
        schema_version=SCHEMA_VERSION,
        status=status,
        claim=claim,
        preregistration_digest=preregistration_digest,
        baseline=baseline,
        interventions=ordered,
        deltas=tuple(deltas),
        blockers=tuple(blockers),
        digest="0" * 64,
    )
    digest = _report_digest(report)
    return AblationReport(
        schema_version=SCHEMA_VERSION,
        status=status,
        claim=claim,
        preregistration_digest=preregistration_digest,
        baseline=baseline,
        interventions=ordered,
        deltas=tuple(deltas),
        blockers=tuple(blockers),
        digest=digest,
    )


def declared_blocked_report(
    *, preregistration_digest: str = "0" * 64, reason: str
) -> AblationReport:
    """The honest artifact when no costed native finalist replay exists."""
    costs = CostProvenance(
        fee=ProvenanceStatus.MISSING,
        funding=ProvenanceStatus.MISSING,
        slippage=ProvenanceStatus.MISSING,
        partial_fill_model="NOT_RUN",
        stop_first_tie=True,
        microstructure=Microstructure.UNSUPPORTED,
    )

    def blocked(intervention: Intervention, run_id: str) -> CostedRun:
        return CostedRun(
            run_id=run_id,
            intervention=intervention,
            status=RunStatus.NOT_RUN,
            code_sha="0" * 40,
            config_sha256="0" * 64,
            dataset_sha256="0" * 64,
            engine_version="NOT_RUN",
            costs=costs,
            reasons=(reason,),
        )

    baseline = blocked(Intervention.BASELINE, "baseline-not-run")
    arms = tuple(
        blocked(intervention, f"{intervention.value.lower()}-not-run")
        for intervention in INTERVENTIONS
        if intervention is not Intervention.BASELINE
    )
    return build_ablation_report(
        (baseline, *arms), preregistration_digest=preregistration_digest
    )


def to_json(report: AblationReport) -> str:
    return json.dumps(report.model_dump(mode="json"), indent=2, sort_keys=True)
