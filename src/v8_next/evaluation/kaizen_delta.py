"""Kaizen benchmark delta — thin port of v8-core/src/benchmark/kaizen_feed.rs BenchmarkDelta (D-153 §§95–102).

Paired incumbent-vs-challenger comparison across capability domains with the
research-debt penalty (family-wise error inflation: ln(trials) × 0.02 capped
at 0.30). Pareto-superior needs no domain degraded beyond -0.01 and a
positive debt-adjusted composite delta. The loop consumes diagnostic deltas
only — no access to protected evaluation data (zero-leakage interface).

DIVERGENCE (named): the Rust entry reads ``BenchmarkReceipt.domain_results``;
the Python receipt carries no domain map, so this port compares the scorer's
domain-score maps (``scoring`` layer) keyed by the same ``CapabilityDomain``.
No parallel receipt ontology is invented.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Mapping

from v8_next.evaluation.scoring import CapabilityDomain

__all__ = ["BenchmarkDelta", "DomainDelta"]


@dataclass(frozen=True)
class DomainDelta:
    domain: CapabilityDomain
    incumbent_score: float
    challenger_score: float
    delta: float
    statistically_significant: bool

    def as_dict(self) -> dict[str, object]:
        return {
            "domain": self.domain.value,
            "incumbent_score": self.incumbent_score,
            "challenger_score": self.challenger_score,
            "delta": self.delta,
            "statistically_significant": self.statistically_significant,
        }


@dataclass(frozen=True)
class BenchmarkDelta:
    incumbent_policy_id: str
    challenger_policy_id: str
    domain_deltas: tuple[DomainDelta, ...]
    composite_delta: float
    challenger_is_pareto_superior: bool
    research_trials_consumed: int
    accrued_research_debt_penalty: float
    claim: str = "NO_ECONOMIC_CLAIM"

    def as_dict(self) -> dict[str, object]:
        return {
            "incumbent_policy_id": self.incumbent_policy_id,
            "challenger_policy_id": self.challenger_policy_id,
            "domain_deltas": [d.as_dict() for d in self.domain_deltas],
            "composite_delta": self.composite_delta,
            "challenger_is_pareto_superior": self.challenger_is_pareto_superior,
            "research_trials_consumed": self.research_trials_consumed,
            "accrued_research_debt_penalty": self.accrued_research_debt_penalty,
            "claim": self.claim,
        }


def compute_delta(
    incumbent_policy_id: str,
    challenger_policy_id: str,
    incumbent_scores: Mapping[CapabilityDomain, float],
    challenger_scores: Mapping[CapabilityDomain, float],
    incumbent_composite: float,
    challenger_composite: float,
    trials_count: int,
) -> BenchmarkDelta:
    if trials_count < 1:
        raise ValueError("KAIZEN_TRIALS: trials_count must be >= 1")
    deltas: list[DomainDelta] = []
    pareto = True
    for domain in CapabilityDomain:
        inc = float(incumbent_scores.get(domain, 0.0))
        chal = float(challenger_scores.get(domain, 0.0))
        delta = chal - inc
        if delta < -0.01:
            pareto = False
        deltas.append(
            DomainDelta(
                domain=domain,
                incumbent_score=inc,
                challenger_score=chal,
                delta=delta,
                statistically_significant=abs(delta) > 0.05,
            )
        )
    penalty = min(math.log(float(trials_count)) * 0.02, 0.30) if trials_count > 1 else 0.0
    comp = challenger_composite - incumbent_composite - penalty
    return BenchmarkDelta(
        incumbent_policy_id=incumbent_policy_id,
        challenger_policy_id=challenger_policy_id,
        domain_deltas=tuple(deltas),
        composite_delta=comp,
        challenger_is_pareto_superior=pareto and comp > 0.0,
        research_trials_consumed=trials_count,
        accrued_research_debt_penalty=penalty,
    )
