"""D-153 / D-152 Capability Scoring & Gate Authority Evaluation (Rule 12, 31, 57).

Computes multidimensional capability scores via penalized harmonic mean
and evaluates G0-G9 hard-gates directly per D-152 §5 and D-153 §74-80.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Mapping

import numpy as np

from v8_next.evaluation.benchmark_receipt import GateState, GateVector, ScoreEvidence


def canonical_json(payload: Any) -> str:
    """Stable JSON for hashing measurement identities."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


class CapabilityDomain(StrEnum):
    ExecutionFidelity = "ExecutionFidelity"
    RegimeRobustness = "RegimeRobustness"
    CrossAssetGeneralization = "CrossAssetGeneralization"
    MicrostructureInvariance = "MicrostructureInvariance"
    DefeaterResistance = "DefeaterResistance"
    StatisticalCredibility = "StatisticalCredibility"
    EvaluationSafety = "EvaluationSafety"
    CapacityScalability = "CapacityScalability"
    RepresentationStability = "RepresentationStability"
    OperationalSimplicity = "OperationalSimplicity"


@dataclass(frozen=True)
class BoundedScore:
    value: float
    lower_diagnostic_band: float
    upper_diagnostic_band: float
    sample_size: int
    effective_sample_size: float


class CapabilityScoreCalculator:
    """CapabilityScore aggregation per D-153 §76."""

    def __init__(self, domain_weights: Mapping[CapabilityDomain, float] | None = None) -> None:
        if domain_weights is not None:
            self.domain_weights = dict(domain_weights)
        else:
            self.domain_weights = {d: 0.10 for d in CapabilityDomain}

    @classmethod
    def monograph_v1(cls) -> CapabilityScoreCalculator:
        """Monograph V1 provisional domain weights (D-153 §76, Monograph line 1283)."""
        return cls(
            {
                CapabilityDomain.MicrostructureInvariance: 0.12,
                CapabilityDomain.OperationalSimplicity: 0.15,
                CapabilityDomain.ExecutionFidelity: 0.10,
                CapabilityDomain.CrossAssetGeneralization: 0.20,
                CapabilityDomain.RepresentationStability: 0.08,
                CapabilityDomain.StatisticalCredibility: 0.07,
                CapabilityDomain.EvaluationSafety: 0.05,
                CapabilityDomain.DefeaterResistance: 0.08,
                CapabilityDomain.RegimeRobustness: 0.10,
                CapabilityDomain.CapacityScalability: 0.05,
            }
        )

    def calculate_aggregate_with_coverage(
        self,
        domain_scores: Mapping[CapabilityDomain, BoundedScore],
        coverage_factor: float = 0.60,
        hard_invariants_passed: bool = True,
    ) -> float:
        """Calculate aggregate capability score with coverage penalty multiplier (D-153 §76)."""
        if not hard_invariants_passed or not domain_scores:
            return 0.0

        weighted_inverse_sum = 0.0
        total_weight = 0.0

        for domain, score in domain_scores.items():
            w = self.domain_weights.get(domain, 0.10)
            total_weight += w
            effective_val = max(0.001, score.lower_diagnostic_band)
            weighted_inverse_sum += w / effective_val

        if total_weight <= 0.0 or weighted_inverse_sum <= 0.0:
            return 0.0

        harmonic_mean = total_weight / weighted_inverse_sum
        clamped_coverage = max(0.10, min(1.0, coverage_factor))
        raw = max(0.0, min(1.0, harmonic_mean * clamped_coverage))
        return raw * 100.0


#: Measurement status of a capability domain in one run (NX08.R1). "Missing" is
#: its own status: an unmeasured domain is never scored as zero, never counted as
#: evidence of absence, and never silently folded into a fixed denominator.
DOMAIN_STATUSES = ("MEASURED", "ABSTAINED", "INACTIVE", "MISSING")

#: What each measurable domain needs; anything outside this map is INACTIVE here.
DOMAIN_INPUT_KIND: dict[CapabilityDomain, str] = {
    CapabilityDomain.ExecutionFidelity: "trades",
    CapabilityDomain.DefeaterResistance: "trades",
    CapabilityDomain.OperationalSimplicity: "bars",
    CapabilityDomain.MicrostructureInvariance: "bars",
}

#: The retired fixed coverage constant (NX08.R1). Kept under a name that says what
#: it is so the legacy side of a dual scoring can name its own assumption instead
#: of passing an anonymous 0.60.
LEGACY_FIXED_COVERAGE_FACTOR = 0.60


@dataclass(frozen=True)
class DomainMeasurementStatus:
    """One domain's measurement status, with the reason it holds that status."""

    domain: str
    status: str
    sample_size: int
    kind: str
    #: Why the domain holds a non-measured status, when that status was resolved
    #: from evidence rather than from the input kind alone (e.g. a degenerate
    #: statistic). ``None`` for the statuses the input kind fully explains.
    reason: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "domain": self.domain,
            "status": self.status,
            "sample_size": self.sample_size,
            "kind": self.kind,
            "reason": self.reason,
        }


def domain_measurement_statuses(
    *, total_bars: int, total_trades: int, abstain_rate: float
) -> tuple[DomainMeasurementStatus, ...]:
    """Status per measurable domain: measured, abstained, inactive or missing."""
    if total_bars < 0 or total_trades < 0:
        raise ValueError("bar and trade counts cannot be negative")
    statuses: list[DomainMeasurementStatus] = []
    for domain in CapabilityDomain:
        kind = DOMAIN_INPUT_KIND.get(domain)
        if kind is None:
            statuses.append(DomainMeasurementStatus(domain.value, "INACTIVE", 0, "unmeasured_domain"))
            continue
        if kind == "trades":
            if total_bars == 0:
                status, size = "MISSING", 0
            elif total_trades > 0:
                status, size = "MEASURED", total_trades
            else:
                status, size = "ABSTAINED", 0
        else:
            status, size = ("MEASURED", total_bars) if total_bars > 0 else ("MISSING", 0)
        statuses.append(DomainMeasurementStatus(domain.value, status, size, kind))
    return tuple(statuses)


def derive_coverage(
    statuses: tuple[DomainMeasurementStatus, ...],
) -> dict[str, Any]:
    """Coverage from real eligible measurements, not from a fixed constant.

    ``eligible`` are the domains this run could have measured; ``MEASURED`` are the
    ones it actually did. A run with no eligible domain has no coverage factor at
    all -- ``None``, not 0.60 and not 0.0.
    """
    measurable = [
        item for item in statuses if item.status in ("MEASURED", "ABSTAINED")
    ]
    measured = [item for item in measurable if item.status == "MEASURED"]
    factor = (len(measured) / len(measurable)) if measurable else None
    inactive = [item for item in statuses if item.status == "INACTIVE"]
    all_domains = len(statuses)
    return {
        "coverage_factor": factor,
        "coverage_source": "DERIVED_FROM_MEASURED_DOMAINS",
        "numerator": len(measured),
        "denominator": len(measurable),
        # a second, deliberately unflattering view: what share of ALL declared
        # domains this run measured. Both are reported so the coverage term can
        # never be read as if the untouched domains had been evaluated.
        "all_domain_coverage_factor": (len(measured) / all_domains) if all_domains else None,
        "inactive_domains": [item.domain for item in inactive],
        "inactive_fraction": (len(inactive) / all_domains) if all_domains else None,
        "measured_domains": [item.domain for item in measured],
        "unmeasured_domains": [item.domain for item in measurable if item.status != "MEASURED"],
        "missing_domains": [item.domain for item in statuses if item.status == "MISSING"],
        "statuses": {item.domain: item.status for item in statuses},
        "basis": (
            "coverage = measured eligible domains / eligible domains; an unmeasured "
            "domain is neither a success nor an economic zero"
        ),
    }


#: Declared diagnostic convention (NOT a calibration): a mean absolute
#: implementation shortfall of this many basis points scores zero execution
#: fidelity, and a measured shortfall of zero scores the top of the domain's
#: declared [0.0, 1.0] range. Published with every score so the convention is
#: auditable and can be replaced by a calibrated reference once one exists.
EXECUTION_FIDELITY_REFERENCE_BPS = 10.0

#: The measured statistic the convention above is declared over: the mean
#: *magnitude* of the per-fill implementation shortfall, published by
#: ``v8_next.adapters.execution_telemetry.execution_telemetry``. The signed mean
#: is deliberately NOT used: adverse and favourable fills cancel in it, so a
#: window whose fills all deviated can still average to ~0 bps.
EXECUTION_FIDELITY_SHORTFALL_FIELD = "slippage_bps_abs_mean"

#: Named reasons a *present* shortfall statistic cannot be published as a
#: measurement (#439). Magnitudes are non-negative, so an exactly-zero value is
#: either every sample being identical or the declared configuration being unable
#: to produce a deviation at all. In both cases 0.0 bps describes the setup
#: instead of execution quality, and scoring it would put the top of the domain's
#: declared range on a number that cannot discriminate -- so the domain resolves
#: to a non-measured status, named. Each constant says which condition was
#: observed: "not measured" is only honest when it names what was missing.
EXECUTION_FIDELITY_DEGENERATE_INERT_CONFIGURATION = "DEGENERATE_INERT_EXECUTION_CONFIGURATION"
EXECUTION_FIDELITY_DEGENERATE_ZERO_VARIATION = "DEGENERATE_ZERO_SHORTFALL_ACROSS_SAMPLES"
EXECUTION_FIDELITY_DEGENERATE_ZERO_SIGNED_MEAN = "DEGENERATE_ZERO_SIGNED_MEAN_WITHOUT_MAGNITUDE"

EXECUTION_FIDELITY_DEGENERATE_REASONS = (
    EXECUTION_FIDELITY_DEGENERATE_INERT_CONFIGURATION,
    EXECUTION_FIDELITY_DEGENERATE_ZERO_VARIATION,
    EXECUTION_FIDELITY_DEGENERATE_ZERO_SIGNED_MEAN,
)

#: The ``DOMAIN_STATUSES`` member a degenerate statistic resolves to: the domain
#: was eligible and had samples, but produced nothing publishable, so it abstains
#: instead of carrying a score. (``MISSING`` stays reserved for "nothing to
#: measure at all", which is why a config-degenerate block is not one.)
EXECUTION_FIDELITY_DEGENERATE_DOMAIN_STATUS = "ABSTAINED"

#: Producer-side signal published by ``adapters.execution_telemetry``: the
#: declared semantics cannot place a fill away from its decision price. Read here
#: so a block that carries the signal is judged without re-deriving the profile,
#: while blocks persisted before the signal existed fall back to the raw knobs.
EXECUTION_FIDELITY_CANNOT_SLIP_SIGNAL = "slippage_configuration_cannot_slip"


def _measured_shortfall_statistic(
    execution: Mapping[str, Any],
) -> tuple[float, bool] | None:
    """The magnitude statistic a block carries, or ``None`` when it carries none.

    Returns ``(measured_magnitude, uses_declared_magnitude_field)``. A block
    persisted before the magnitude statistic existed still carries measured
    evidence, so |signed mean| is accepted -- flagged as the fallback, because a
    zero there is also what two opposite-signed fills produce and therefore
    cannot prove a zero deviation.
    """
    if (execution.get("slippage_samples") or 0) <= 0:
        return None
    declared = execution.get(EXECUTION_FIDELITY_SHORTFALL_FIELD)
    if isinstance(declared, (int, float)) and not isinstance(declared, bool):
        return abs(float(declared)), True
    signed = execution.get("slippage_bps_mean")
    if isinstance(signed, (int, float)) and not isinstance(signed, bool):
        return abs(float(signed)), False
    return None


def _declares_configuration_that_cannot_slip(execution: Mapping[str, Any]) -> bool:
    """Whether the block's own declared semantics forbid a shortfall altogether."""
    if execution.get(EXECUTION_FIDELITY_CANNOT_SLIP_SIGNAL) is True:
        return True
    prob_slippage = execution.get("prob_slippage")
    if isinstance(prob_slippage, bool) or not isinstance(prob_slippage, (int, float)):
        return False
    return float(prob_slippage) <= 0.0 and not execution.get("fill_model_slipped")


def execution_fidelity_degeneracy(
    execution: Mapping[str, Any] | None,
) -> tuple[str | None, int]:
    """``(named reason, samples)`` when a present shortfall statistic is degenerate.

    ``(None, samples)`` when the statistic can discriminate, or when no usable
    measurement exists at all, so a caller abstains only on a named degeneracy.
    """
    if not execution:
        return None, 0
    samples = int(execution.get("slippage_samples") or 0)
    read = _measured_shortfall_statistic(execution)
    if read is None:
        return None, samples
    measured, uses_declared_field = read
    if measured != 0.0:
        return None, samples
    if _declares_configuration_that_cannot_slip(execution):
        return EXECUTION_FIDELITY_DEGENERATE_INERT_CONFIGURATION, samples
    if uses_declared_field:
        return EXECUTION_FIDELITY_DEGENERATE_ZERO_VARIATION, samples
    return EXECUTION_FIDELITY_DEGENERATE_ZERO_SIGNED_MEAN, samples


def _with_execution_fidelity_status(
    statuses: tuple[DomainMeasurementStatus, ...],
    *,
    status: str,
    sample_size: int,
    reason: str,
) -> tuple[DomainMeasurementStatus, ...]:
    """Re-resolve ExecutionFidelity's status on the existing status structure.

    The reason rides in the existing :class:`DomainMeasurementStatus` record so
    it is serialized with the breakdown; no parallel status vocabulary is
    invented for it.
    """
    if status not in DOMAIN_STATUSES or reason not in EXECUTION_FIDELITY_DEGENERATE_REASONS:
        raise ValueError(
            f"degenerate ExecutionFidelity status must be a declared DOMAIN_STATUSES "
            f"member ({DOMAIN_STATUSES}) with a named reason "
            f"({EXECUTION_FIDELITY_DEGENERATE_REASONS}); got status={status!r} "
            f"reason={reason!r}"
        )
    return tuple(
        DomainMeasurementStatus(
            domain=item.domain,
            status=status,
            sample_size=sample_size,
            kind=item.kind,
            reason=reason,
        )
        if item.domain == CapabilityDomain.ExecutionFidelity.value
        else item
        for item in statuses
    )


def _execution_fidelity(
    sharpe_proxy: float,
    execution: Mapping[str, Any] | None,
) -> tuple[float | None, str, float | None]:
    """ExecutionFidelity from measured shortfall when it exists, else the proxy.

    The previous value was a rescaled PnL Sharpe that contained no execution
    information at all. Measured implementation shortfall is preferred; when no
    fills were measured the proxy is retained and its source published, so a
    score is never silently attributed to execution evidence it does not have.

    Declared measured mapping: ``1 - shortfall_bps / EXECUTION_FIDELITY_REFERENCE_BPS``
    clipped to the domain's declared [0.0, 1.0] range. The measured branch is
    therefore neither floored at nor capped by the proxy's undeclared
    [0.05, 0.50] band, which is what made every run publish a constant 0.50:
    10 bps is what the declared reference scores as zero.

    A statistic that is present but degenerate scores nothing (``None``) and the
    source names the degeneracy: 0.0 bps of measured magnitude is either every
    sample being identical or a configuration that cannot slip, so the top of the
    declared range would be published on a number that cannot vary (#439). The
    third element is the shortfall value that was read (``None`` when the proxy
    was used), so the abstention stays auditable in the published breakdown.
    """
    if execution:
        read = _measured_shortfall_statistic(execution)
        if read is not None:
            measured, _ = read
            reason, _ = execution_fidelity_degeneracy(execution)
            if reason is not None:
                return None, reason, measured
            fidelity = 1.0 - measured / EXECUTION_FIDELITY_REFERENCE_BPS
            return (
                float(np.clip(fidelity, 0.0, 1.0)),
                "MEASURED_IMPLEMENTATION_SHORTFALL",
                measured,
            )
    return (
        float(np.clip(sharpe_proxy * 0.25 + 0.10, 0.05, 0.50)),
        "PNL_SHARPE_PROXY",
        None,
    )


#: Diagnostic bands per measurable domain (#408). One source of truth: the
#: producer's own ``value * lo`` / ``value * hi`` and the recomputation of a
#: published aggregate from a receipt's bound evidence must be the same floating
#: point arithmetic in the same order, or a recomputed number would differ in its
#: last bits from the published one -- and then "recomputable" would stop meaning
#: "reproducible".
DOMAIN_DIAGNOSTIC_BANDS: dict[CapabilityDomain, tuple[float, float]] = {
    CapabilityDomain.ExecutionFidelity: (0.8, 1.2),
    CapabilityDomain.OperationalSimplicity: (0.8, 1.2),
    CapabilityDomain.DefeaterResistance: (0.7, 1.3),
    CapabilityDomain.MicrostructureInvariance: (0.75, 1.25),
}


def bounded_domain_scores(
    raw: Mapping[CapabilityDomain, tuple[float, int]],
) -> dict[CapabilityDomain, BoundedScore]:
    """``BoundedScore`` per measured domain: ``(value, sample_size)`` in, bands added.

    Insertion order is the aggregate's own summation order (float addition is not
    associative), so callers hand the domains over in the order the measurement
    published them, exactly as the producer and the recomputation both do (#408).
    """
    return {
        domain: BoundedScore(
            value=value,
            lower_diagnostic_band=value * DOMAIN_DIAGNOSTIC_BANDS[domain][0],
            upper_diagnostic_band=value * DOMAIN_DIAGNOSTIC_BANDS[domain][1],
            sample_size=samples,
            effective_sample_size=float(samples),
        )
        for domain, (value, samples) in raw.items()
    }


def _score_evidence_document(
    *,
    total_bars: int,
    total_trades: int,
    abstain_rate: float,
    coverage_factor: float | None,
    aggregate_status: str,
    domain_scores: Mapping[CapabilityDomain, BoundedScore] | None = None,
) -> dict[str, Any]:
    """The determinant record a receipt binds so its number can be recomputed (#408).

    ``domain_values`` carries the *unrounded* per-domain values in the aggregate's
    own summation order: the ``domains`` view beside it rounds them for reading, and
    a rounded number cannot be the basis of a recomputation.
    """
    return {
        "total_bars": total_bars,
        "total_trades": total_trades,
        "abstain_rate": abstain_rate,
        "coverage_factor": coverage_factor,
        "hard_invariants_passed": True,
        "aggregate_status": aggregate_status,
        "domain_values": (
            []
            if domain_scores is None
            else [
                [domain.value, bounded.value, bounded.sample_size]
                for domain, bounded in domain_scores.items()
            ]
        ),
    }


def compute_capability_breakdown(
    pnl_series: list[float],
    total_bars: int,
    total_trades: int,
    abstain_rate: float,
    coverage_factor: float | None = None,
    execution: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Per-domain capability breakdown for loop engineering.

    Returns {domain: {score, band_low, band_high, weight, sample_size}} plus
    the aggregate. An agent asking 'why did I stall at 8.8' reads this, not
    the scalar. Empty/underpowered input yields empty domains (never zeros
    disguised as measurements).

    NX08.R1: coverage is **derived** from the domains this run really measured.
    There is no fixed denominator: a run whose trade domains abstained reports a
    lower coverage with the abstained domains named, and a run with nothing
    eligible reports no coverage factor at all (``None``) and an aggregate of
    ``None`` -- never a fabricated score built on an assumed 0.60.
    """
    calc = CapabilityScoreCalculator.monograph_v1()
    statuses = domain_measurement_statuses(
        total_bars=total_bars, total_trades=total_trades, abstain_rate=abstain_rate
    )
    # A degenerate ExecutionFidelity statistic abstains by name before coverage is
    # derived (#439), so an eligible-but-unmeasurable domain is counted as exactly
    # that. With no trades the domain already abstains for NO_TRADES, which is the
    # reason that path publishes, so the override is skipped there.
    degenerate_reason, degenerate_samples = execution_fidelity_degeneracy(execution)
    if degenerate_reason is not None and total_trades > 0:
        statuses = _with_execution_fidelity_status(
            statuses,
            status=EXECUTION_FIDELITY_DEGENERATE_DOMAIN_STATUS,
            sample_size=degenerate_samples,
            reason=degenerate_reason,
        )
    coverage = derive_coverage(statuses)
    if coverage_factor is None:
        coverage_factor = coverage["coverage_factor"]
        coverage_source = coverage["coverage_source"]
    else:
        coverage_source = "CALLER_SUPPLIED"
    if not pnl_series or total_trades == 0:
        return {
            "domains": {},
            "aggregate": None,
            "aggregate_status": "MISSING_NO_TRADES",
            "coverage_factor": coverage_factor,
            "coverage_source": coverage_source,
            "coverage": coverage,
            "domain_measurement_statuses": [item.as_dict() for item in statuses],
            "execution_fidelity_source": "NO_TRADES",
            "execution_fidelity_reference_bps": EXECUTION_FIDELITY_REFERENCE_BPS,
            "execution_fidelity_shortfall_bps": None,
            "score_evidence": _score_evidence_document(
                total_bars=total_bars,
                total_trades=total_trades,
                abstain_rate=abstain_rate,
                coverage_factor=coverage_factor,
                aggregate_status="MISSING_NO_TRADES",
            ),
        }
    if coverage_factor is None:
        return {
            "domains": {},
            "aggregate": None,
            "aggregate_status": "MISSING_NO_ELIGIBLE_MEASUREMENT",
            "coverage_factor": None,
            "coverage_source": coverage["coverage_source"],
            "coverage": coverage,
            "domain_measurement_statuses": [item.as_dict() for item in statuses],
            "execution_fidelity_source": "NO_ELIGIBLE_DOMAIN",
            "execution_fidelity_reference_bps": EXECUTION_FIDELITY_REFERENCE_BPS,
            "score_evidence": _score_evidence_document(
                total_bars=total_bars,
                total_trades=total_trades,
                abstain_rate=abstain_rate,
                coverage_factor=None,
                aggregate_status="MISSING_NO_ELIGIBLE_MEASUREMENT",
            ),
        }

    arr = np.array(pnl_series, dtype=np.float64)
    std = float(np.std(arr)) if len(arr) > 1 else 0.01
    mean = float(np.mean(arr))
    sharpe_proxy = (mean / std) if std > 1e-9 else 0.5

    exec_val, exec_source, exec_shortfall_bps = _execution_fidelity(sharpe_proxy, execution)
    op_val = float(np.clip(1.0 - abstain_rate * 0.3, 0.10, 0.60))
    def_val = 0.15 if total_trades >= 1 else 0.01
    micro_val = float(np.clip(0.10 + (total_bars / 500.0) * 0.10, 0.05, 0.30))

    # A domain whose only statistic was degenerate publishes no score at all
    # (#439): it stays out of the aggregate instead of being scored on a number
    # that cannot discriminate, exactly like the other non-measured domains.
    raw: dict[CapabilityDomain, tuple[float, int]] = {}
    if exec_val is not None:
        raw[CapabilityDomain.ExecutionFidelity] = (exec_val, total_trades)
    raw[CapabilityDomain.OperationalSimplicity] = (op_val, total_bars)
    raw[CapabilityDomain.DefeaterResistance] = (def_val, total_trades)
    raw[CapabilityDomain.MicrostructureInvariance] = (micro_val, total_bars)
    domain_scores = bounded_domain_scores(raw)
    score = calc.calculate_aggregate_with_coverage(
        domain_scores=domain_scores,
        coverage_factor=coverage_factor,
        hard_invariants_passed=True,
    )
    domains: dict[str, Any] = {}
    for domain, bs in domain_scores.items():
        domains[domain.value] = {
            "score": round(bs.value * 100.0, 1),
            "band_low": round(bs.lower_diagnostic_band * 100.0, 1),
            "band_high": round(bs.upper_diagnostic_band * 100.0, 1),
            "weight": calc.domain_weights.get(domain, 0.10),
            "sample_size": bs.sample_size,
        }
    return {
        "domains": domains,
        "aggregate": round(score, 1),
        "aggregate_status": "MEASURED",
        "coverage_factor": coverage_factor,
        "coverage_source": coverage_source,
        "coverage": coverage,
        "domain_statuses": {item.domain: item.status for item in statuses},
        #: The full measurement record of every domain, including the named reason
        #: a domain resolved to a non-measured status (e.g. a degenerate
        #: ExecutionFidelity statistic): the status vocabulary stays the existing
        #: one, and the reason is serialized next to it rather than inferred.
        "domain_measurement_statuses": [item.as_dict() for item in statuses],
        "execution_fidelity_source": exec_source,
        "execution_fidelity_reference_bps": EXECUTION_FIDELITY_REFERENCE_BPS,
        # The shortfall value that was read, so a reader can check a published
        # score against the declared mapping -- and, when the value was degenerate,
        # see the input the domain abstained on instead of a score.
        "execution_fidelity_shortfall_bps": (
            round(exec_shortfall_bps, 6) if exec_shortfall_bps is not None else None
        ),
        #: #408. The determinants of the aggregate above, unrounded and in the order
        #: the aggregate summed them: the record a receipt binds so that the number
        #: it publishes is recomputable from the receipt alone.
        "score_evidence": _score_evidence_document(
            total_bars=total_bars,
            total_trades=total_trades,
            abstain_rate=abstain_rate,
            coverage_factor=coverage_factor,
            aggregate_status="MEASURED",
            domain_scores=domain_scores,
        ),
    }


def compute_capability_score(
    pnl_series: list[float],
    total_bars: int,
    total_trades: int,
    abstain_rate: float,
    coverage_factor: float | None = None,
    execution: Mapping[str, Any] | None = None,
) -> float | None:
    """Compute multidimensional CapabilityScore in [0.0, 100.0] per D-153 §76.

    ``None`` when the run has no measurable basis (NX08.R1/R2): a missing
    measurement stays missing instead of being reported as a zero score.
    """
    breakdown = compute_capability_breakdown(
        pnl_series=pnl_series,
        total_bars=total_bars,
        total_trades=total_trades,
        abstain_rate=abstain_rate,
        coverage_factor=coverage_factor,
        execution=execution,
    )
    aggregate = breakdown["aggregate"]
    return None if aggregate is None else float(aggregate)


def recompute_capability_score(evidence: ScoreEvidence) -> float | None:
    """The published number, recomputed from the evidence a receipt bound (#408).

    ``None`` when the bound evidence declares no measured basis at all (a missing
    measurement stays missing -- never a zero). Otherwise this is the producer's own
    arithmetic, on the producer's own inputs, in the producer's own order, so a
    number that follows from its evidence comes back bit-equal.
    """
    if evidence.aggregate_status != "MEASURED" or evidence.coverage_factor is None:
        return None
    raw: dict[CapabilityDomain, tuple[float, int]] = {}
    for name, value, samples in evidence.domain_values:
        try:
            domain = CapabilityDomain(name)
        except ValueError:
            # an undeclared domain cannot contribute to the declared aggregate
            return None
        raw[domain] = (value, samples)
    if not raw:
        return None
    score = CapabilityScoreCalculator.monograph_v1().calculate_aggregate_with_coverage(
        domain_scores=bounded_domain_scores(raw),
        coverage_factor=evidence.coverage_factor,
        hard_invariants_passed=evidence.hard_invariants_passed,
    )
    return float(round(score, 1))


#: Version tags for the two scorers kept side by side in one receipt (NX08.R5).
SCORING_VERSION_LEGACY = "legacy_fixed_coverage_v1"
SCORING_VERSION_CURRENT = "derived_coverage_v1"


def dual_scoring(
    pnl_series: list[float],
    total_bars: int,
    total_trades: int,
    abstain_rate: float,
    execution: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Both scorers on the same fixed inputs, in one record (NX08.R5).

    The two versions differ only in how coverage enters the aggregate, so the
    comparison separates a **transform-only** change (same measurements, different
    coverage/denominator convention) from a **measurement change** (different raw
    inputs). Neither a target score nor a PASS is implied: the record reports the
    delta and its kind, and the caller still owns any conclusion.
    """
    legacy = compute_capability_breakdown(
        pnl_series=pnl_series,
        total_bars=total_bars,
        total_trades=total_trades,
        abstain_rate=abstain_rate,
        coverage_factor=LEGACY_FIXED_COVERAGE_FACTOR,
        execution=execution,
    )
    current = compute_capability_breakdown(
        pnl_series=pnl_series,
        total_bars=total_bars,
        total_trades=total_trades,
        abstain_rate=abstain_rate,
        coverage_factor=None,
        execution=execution,
    )
    measurement_identity = hashlib.sha256(
        canonical_json(
            {
                "bars": total_bars,
                "trades": total_trades,
                "abstain_rate": round(float(abstain_rate), 12),
                "pnl": [round(float(value), 12) for value in pnl_series],
            }
        ).encode()
    ).hexdigest()
    legacy_aggregate = legacy["aggregate"]
    current_aggregate = current["aggregate"]
    delta = (
        round(float(current_aggregate) - float(legacy_aggregate), 4)
        if legacy_aggregate is not None and current_aggregate is not None
        else None
    )
    return {
        "scoring_versions": {
            SCORING_VERSION_LEGACY: {
                "aggregate": legacy_aggregate,
                "coverage_factor": legacy["coverage_factor"],
                "coverage_source": legacy["coverage_source"],
                "convention": "retired fixed coverage constant; kept only as the comparison side",
            },
            SCORING_VERSION_CURRENT: {
                "aggregate": current_aggregate,
                "coverage_factor": current["coverage_factor"],
                "coverage_source": current["coverage_source"],
                "coverage": current["coverage"],
                "convention": "coverage derived from measured eligible domains",
            },
        },
        "delta": delta,
        "delta_kind": "TRANSFORM_ONLY" if delta is not None else "NOT_COMPARABLE_MISSING_MEASUREMENT",
        "measurement_identity": measurement_identity,
        "measurement_changed": False,
        "note": (
            "both sides read the same raw measurements; only the coverage "
            "transform differs, so any delta here is transform-only by construction"
        ),
        "claim_status": "NO_ECONOMIC_CLAIM",
    }


def evaluate_gate_vector(
    total_bars: int,
    total_trades: int,
    pnl_series: list[float],
    mismatches: int | None = 0,
    has_continuous_lineage: bool | None = True,
    is_causal_pit: bool | None = True,
    has_data_gaps: bool = False,
    g2_state: GateState | None = None,
    g3_state: GateState | None = None,
    g4_state: GateState | None = None,
    g5_state: GateState | None = None,
    g6_state: GateState | None = None,
    g7_state: GateState | None = None,
    g8_state: GateState | None = None,
    g9_state: GateState | None = None,
) -> GateVector:
    """Evaluate G0-G9 hard gates per D-152 §5 and D-153 specifications.

    Unmeasured structural inputs (None) resolve to UNKNOWN, never PASS:
    no gate may certify what was not empirically established.
    """
    # G0 Identity: verified lineage and no data gaps.
    # None (unmeasured) resolves to UNKNOWN, never PASS.
    if has_continuous_lineage is None:
        g0 = GateState.UNKNOWN
    else:
        g0 = GateState.PASS if (has_continuous_lineage and not has_data_gaps) else GateState.BLOCKED

    # G1 Causal PIT: strict point-in-time causation (None -> UNKNOWN).
    if is_causal_pit is None:
        g1 = GateState.UNKNOWN
    else:
        g1 = GateState.PASS if is_causal_pit else GateState.BLOCKED

    # G2 Determinism Ledger: zero non-deterministic mismatches (None -> UNKNOWN).
    # A measured rerun-parity state, when supplied, wins over the mismatch count:
    # the rerun compares two real engine executions, which is direct evidence.
    if g2_state is not None:
        g2 = g2_state
    elif mismatches is None:
        g2 = GateState.UNKNOWN
    else:
        g2 = GateState.PASS if mismatches == 0 else GateState.BLOCKED

    # G3 Scenario Robustness: the regimes the expert strategy was re-run across are
    # the only G3 measurement in this tree, so the cell is named for that measurement.
    g3 = g3_state if g3_state is not None else GateState.UNKNOWN

    # G4 Synthetic / Structural Robustness: unperturbed diagnostic cell or adversarial falsification
    finite_pnl = all(math.isfinite(p) for p in pnl_series) if pnl_series else True
    if g4_state is not None:
        g4 = g4_state
    else:
        g4 = GateState.UNKNOWN if finite_pnl else GateState.BLOCKED

    # G5 Selection Control: Constitution Rule 12 keeps uncertified diagnostic at UNKNOWN
    g5 = g5_state if g5_state is not None else GateState.UNKNOWN

    # G6 Protected OOS: reserved for out-of-sample partition
    g6 = g6_state if g6_state is not None else GateState.UNKNOWN

    # G7 Prospective Shadow Succession
    g7 = g7_state if g7_state is not None else GateState.UNKNOWN

    # G8 Live Realization: venue-settled fills required; diagnostic fold when research candidate
    g8 = g8_state if g8_state is not None else GateState.MISSING

    # G9 Certificate: requires ClaimRegistry route; scalar collapse forbidden
    g9 = g9_state if g9_state is not None else GateState.MISSING

    return GateVector(
        g0_identity=g0,
        g1_causal_pit=g1,
        g2_determinism_ledger=g2,
        g3_scenario_robustness=g3,
        g4_structural_robustness=g4,
        g5_statistical_credibility=g5,
        g6_protected_oos=g6,
        g7_generalization=g7,
        g8_prospective_shadow=g8,
        g9_live_realization=g9,
    )
