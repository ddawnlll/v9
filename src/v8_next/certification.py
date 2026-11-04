"""Certificate inputs produced from a benchmark run, or named refusals.

Issue #476: the D-153 projection and Minerva ports existed but no benchmark path
constructed them, so ``PolicyCertificate.generate`` always rendered the
corresponding factors as bare ``MISSING``.  This module is the single bridge
between the run's measured artifacts and the certificate:

* :func:`projection_for_certificate` calls the real capital-outcome projection
  when the run has at least five campaign returns and a capability score.  The
  projection is diagnostic by construction (``NO_ECONOMIC_CLAIM``); it does not
  mint a 0-100 economic factor, so the returned object names that absence
  instead of leaving a hole.
* :func:`minerva_for_certificate` calls :class:`MinervaEvaluator` only when all
  five inputs were measured.  Otherwise it returns a named refusal.  MinTRL
  track length is sourced from :mod:`v8_next.evaluation.mintrl` when a declared
  plan is supplied; no assumption is invented at this layer.

Neither helper catches a refusal and converts it to zero: the refusal token is
part of the certificate derivation.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from v8_next.evaluation.minerva import MinervaEvaluator, MinervaRobustness
from v8_next.evaluation.mintrl import MinTRLPlan, estimate_min_trl
from v8_next.evaluation.projection import CapitalOutcomeProjection, project_from_returns

__all__ = [
    "CertificateMinerva",
    "CertificateProjection",
    "minerva_for_certificate",
    "projection_for_certificate",
]

PROJECTION_DIAGNOSTIC_ONLY = "PROJECTION_DIAGNOSTIC_ONLY_NO_ECONOMIC_FACTOR"
MINERVA_REFUSAL = "MINERVA_UNMEASURED_NAMED_REFUSAL"


@dataclass(frozen=True)
class CertificateProjection:
    """Projection handed to the certificate, with its absence named."""

    economic_score: float | None
    refusal_reason: str
    diagnostic: CapitalOutcomeProjection | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "economic_score": self.economic_score,
            "refusal_reason": self.refusal_reason,
            "diagnostic": None if self.diagnostic is None else self.diagnostic.as_dict(),
        }


@dataclass(frozen=True)
class CertificateMinerva:
    """Minerva robustness handed to the certificate, or its named refusal."""

    effective_score: float | None
    seal_status: str
    refusal_reason: str
    measured_inputs: Mapping[str, float | None]
    robustness: MinervaRobustness | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "effective_score": self.effective_score,
            "seal_status": self.seal_status,
            "refusal_reason": self.refusal_reason,
            "measured_inputs": dict(self.measured_inputs),
            "robustness": None if self.robustness is None else self.robustness.as_dict(),
        }


def projection_for_certificate(
    *,
    policy_id: str,
    benchmark_receipt_id: str,
    capability_score: float | None,
    campaign_returns: Sequence[float],
    initial_capital_usd: float,
    has_synthetic_population_only: bool,
) -> CertificateProjection:
    """Build a diagnostic projection or name why it cannot be built."""
    if capability_score is None:
        return CertificateProjection(
            economic_score=None,
            refusal_reason="PROJECTION_UNMEASURED: capability_score is absent",
        )
    trade_returns_bps = [float(value) * 10_000.0 for value in campaign_returns]
    try:
        diagnostic = project_from_returns(
            policy_id=policy_id,
            benchmark_receipt_id=benchmark_receipt_id,
            capability_score=float(capability_score),
            trade_returns_bps=trade_returns_bps,
            initial_capital_usd=initial_capital_usd,
            has_synthetic_population_only=has_synthetic_population_only,
            mc_simulations=0,
            mc_horizon_trades=0,
        )
    except ValueError as exc:
        return CertificateProjection(economic_score=None, refusal_reason=str(exc))
    return CertificateProjection(
        economic_score=None,
        refusal_reason=PROJECTION_DIAGNOSTIC_ONLY,
        diagnostic=diagnostic,
    )


def _stat(stats: Mapping[str, Any] | None, *path: str) -> float | None:
    current: Any = stats
    for key in path:
        if not isinstance(current, Mapping):
            return None
        current = current.get(key)
    if isinstance(current, bool) or not isinstance(current, (int, float)):
        return None
    value = float(current)
    return value if value == value and value not in (float("inf"), float("-inf")) else None


def minerva_for_certificate(
    *,
    statistics: Mapping[str, Any] | None,
    actual_track_days: float | None,
    min_trl_days: float | None,
    min_trl_plan: MinTRLPlan | None,
    observed_intervals: int | None,
    worst_regime_return_bps: float | None,
    regime_floor_bps: float | None,
    prudex: Any | None = None,
) -> CertificateMinerva:
    """Build Minerva robustness from measured statistics or refuse by name.

    The statistical inputs are read from the existing estimator output:
    ``statistics['dsr']['dsr_confidence']``, ``statistics['pbo']['pbo']`` and
    ``statistics['spa']['pvalues']['consistent']``.  No estimator is re-run and
    no threshold is substituted here.
    """
    dsr = _stat(statistics, "dsr", "dsr_confidence")
    pbo = _stat(statistics, "pbo", "pbo")
    spa_p = _stat(statistics, "spa", "pvalues", "consistent")
    # ``min_trl_days`` is a caller-supplied measurement. It used to be shadowed by a
    # local of the same name initialised to None, so a measured value handed in was
    # silently discarded and the certificate refused for a missing input it had been
    # given. The parameter is now resolved into its own name and never re-initialised.
    resolved_min_trl_days = min_trl_days
    min_trl_reason: str | None = None
    if resolved_min_trl_days is None and min_trl_plan is not None and observed_intervals is not None:
        result = estimate_min_trl(min_trl_plan, observed_intervals)
        min_trl_reason = (
            result.reason
            or (
                "MINTRL_INTERVAL_UNIT_DAYS_UNDECLARED: required_intervals="
                f"{result.required_intervals}"
            )
        )
    elif resolved_min_trl_days is None:
        min_trl_reason = "DECLARED_MINTRL_PLAN_REQUIRED"

    measured: dict[str, float | None] = {
        "dsr_confidence": dsr,
        "pbo": pbo,
        "spa_p_value": spa_p,
        "actual_track_days": actual_track_days,
        "min_trl_days": resolved_min_trl_days,
        "worst_regime_return_bps": worst_regime_return_bps,
        "regime_floor_bps": regime_floor_bps,
    }
    missing = [name for name, value in measured.items() if value is None]
    if missing:
        reasons = [f"{name}=UNMEASURED" for name in missing]
        if min_trl_reason is not None:
            reasons.append(f"min_trl={min_trl_reason}")
        return CertificateMinerva(
            effective_score=None,
            seal_status=f"{MINERVA_REFUSAL}: " + ", ".join(reasons),
            refusal_reason=", ".join(reasons),
            measured_inputs=measured,
        )
    try:
        robustness = MinervaEvaluator.evaluate(
            dsr=float(dsr),
            pbo=float(pbo),
            spa_p_value=float(spa_p),
            actual_track_days=float(actual_track_days),
            min_trl_days=float(resolved_min_trl_days),
            worst_regime_return_bps=float(worst_regime_return_bps),
            regime_floor_bps=float(regime_floor_bps),
            prudex=prudex,
        )
    except ValueError as exc:
        return CertificateMinerva(
            effective_score=None,
            seal_status=f"{MINERVA_REFUSAL}: {exc}",
            refusal_reason=str(exc),
            measured_inputs=measured,
        )
    return CertificateMinerva(
        effective_score=robustness.effective_score,
        seal_status=robustness.seal_status,
        refusal_reason="",
        measured_inputs=measured,
        robustness=robustness,
    )
