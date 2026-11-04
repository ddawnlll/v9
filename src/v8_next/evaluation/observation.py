"""Benchmark observation registry — thin port of v8-core/src/benchmark/observation.rs (D-153 §32, §64–75).

One canonical observation binds a raw value, its calibrated score, epistemic
authority, data role, and statistical bounds. Scores and bounds are clamped to
[0, 1] exactly as the Rust constructor does; missing data stays missing (the
registry refuses empty observation sets, never a zero-filled aggregate).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from v8_next.evaluation.scoring import CapabilityDomain
from v8_next.evaluation.store import DataRole

__all__ = ["MetricObservation", "ObservationRegistry"]


def _clamp01(value: float) -> float:
    return min(1.0, max(0.0, value))


@dataclass(frozen=True)
class MetricObservation:
    metric_id: str
    domain: CapabilityDomain
    authority: str
    population_role: DataRole
    raw_value: float
    normalized_score: float
    lower_bound_95: float
    upper_bound_95: float
    sample_size: int
    effective_sample_size: float
    passed_floor: bool
    notes: str = ""

    @classmethod
    def create(
        cls,
        metric_id: str,
        domain: CapabilityDomain,
        authority: str,
        population_role: DataRole,
        raw_value: float,
        normalized_score: float,
        lower_bound_95: float,
        upper_bound_95: float,
        sample_size: int,
        effective_sample_size: float,
        passed_floor: bool,
        notes: str = "",
    ) -> MetricObservation:
        return cls(
            metric_id=metric_id,
            domain=domain,
            authority=authority,
            population_role=population_role,
            raw_value=raw_value,
            normalized_score=_clamp01(normalized_score),
            lower_bound_95=_clamp01(lower_bound_95),
            upper_bound_95=_clamp01(upper_bound_95),
            sample_size=sample_size,
            effective_sample_size=effective_sample_size,
            passed_floor=passed_floor,
            notes=notes,
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "metric_id": self.metric_id,
            "domain": self.domain.value,
            "authority": self.authority,
            "population_role": self.population_role,
            "raw_value": self.raw_value,
            "normalized_score": self.normalized_score,
            "lower_bound_95": self.lower_bound_95,
            "upper_bound_95": self.upper_bound_95,
            "sample_size": self.sample_size,
            "effective_sample_size": self.effective_sample_size,
            "passed_floor": self.passed_floor,
            "notes": self.notes,
        }


@dataclass
class ObservationRegistry:
    """Per-domain observation lists; empty domains stay absent, never zero."""

    _by_domain: dict[CapabilityDomain, list[MetricObservation]] = field(default_factory=dict)

    def record(self, observation: MetricObservation) -> None:
        self._by_domain.setdefault(observation.domain, []).append(observation)

    def for_domain(self, domain: CapabilityDomain) -> tuple[MetricObservation, ...]:
        return tuple(self._by_domain.get(domain, ()))

    def covered_domains(self) -> tuple[CapabilityDomain, ...]:
        return tuple(d for d in CapabilityDomain if d in self._by_domain)
