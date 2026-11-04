"""Versioned expert habitat discipline, bound causally to decision time.

A habitat entry names the decision-time trend/volatility cells where a thesis
is allowed to speak. Out-of-habitat evidence becomes abstention in both
directions: it can neither support nor contradict admission. Families without
an entry stay UNQUALIFIED and pass through unchanged; nothing here invents
support, utility, or economic authority.

v1 covers only the single default paper path: compression-breakout observations
require the decision-time LowVolSqueeze volatility cell. The thesis is a
compression release, so demanding measured compression reuses the frozen
regime labels without fitting new thresholds. Trend is agnostic for this
family. Missing regime inputs stay missing and abstain; outcomes never
relabel habitat after the fact.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from v8_next.economics.decisions import Opportunity, Stance, StanceKind
from v8_next.economics.regime import RegimeObservation

HABITAT_VERSION = "habitat-trend-vol-v1"

# observer_id -> allowed decision-time cells; None on an axis is agnostic.
# Absent observers are UNQUALIFIED, never silently mapped. Keyed by observer
# identity (the selection key), not behavior family: the frozen baseline
# stance inherits the compression-breakout default family without sharing
# the compression-release thesis.
HABITAT: dict[str, dict[str, frozenset[str] | None]] = {
    "squeeze-swing": {"trend": None, "volatility": frozenset({"LowVolSqueeze"})},
}


def habitat_status(
    stance: Stance, regime: RegimeObservation, opportunity: Opportunity | None
) -> str:
    """Decision-time habitat verdict for one stance; never touches outcomes."""
    if stance.kind == StanceKind.ABSTAIN or stance.opportunity_id is None:
        return "NOT_APPLICABLE"
    if (
        opportunity is None
        or stance.opportunity_id != opportunity.opportunity_id
        or opportunity.instrument_id != regime.instrument_id
    ):
        return "NOT_APPLICABLE"
    entry = HABITAT.get(stance.observer_id)
    if entry is None:
        return "UNQUALIFIED"
    allowed_trend = entry["trend"]
    if allowed_trend is not None and regime.trend not in allowed_trend:
        return "OUT_OF_HABITAT"
    allowed_volatility = entry["volatility"]
    if allowed_volatility is not None and regime.volatility not in allowed_volatility:
        return "OUT_OF_HABITAT"
    return "IN_HABITAT"


def apply_habitat(
    stances: tuple[Stance, ...],
    regime: RegimeObservation,
    opportunity: Opportunity | None,
) -> tuple[tuple[Stance, ...], list[dict[str, Any]]]:
    """Demote out-of-habitat evidence to abstention, preserving originals.

    Only stances bound to the admitted opportunity are evaluated; everything
    else passes through with a NOT_APPLICABLE mark. The report parallels the
    input order for audit; admission must use the adjusted tuple.
    """
    adjusted: list[Stance] = []
    report: list[dict[str, Any]] = []
    for stance in stances:
        status = habitat_status(stance, regime, opportunity)
        if status == "OUT_OF_HABITAT":
            adjusted.append(
                replace(stance, kind=StanceKind.ABSTAIN, reason="OUT_OF_HABITAT")
            )
        else:
            adjusted.append(stance)
        report.append(
            {
                "observer_id": stance.observer_id,
                "variant_id": stance.variant_id,
                "behavior_family": stance.behavior_family,
                "habitat": status,
                "habitat_version": HABITAT_VERSION,
            }
        )
    return tuple(adjusted), report
