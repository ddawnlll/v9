"""Ordered economic admission. This module does not mint evidence authority."""

from dataclasses import dataclass
from decimal import Decimal

from v8_next.domain.campaign import PaperCampaign
from v8_next.domain.experiment import RulePaperExperiment
from v8_next.economics.decisions import (
    Opportunity,
    Stance,
    UtilityInputs,
    reconcile,
    utility_admission,
)
from v8_next.economics.protection import CampaignProtection
from v8_next.economics.regime import RegimeObservation
from v8_next.risk.admission import RiskLimits, RiskSnapshot, admit
from v8_next.risk.sizing import StopBudget, StopExposure, stop_budget_notional


@dataclass(frozen=True)
class InstrumentConstraints:
    step: Decimal
    min_quantity: Decimal
    max_quantity: Decimal
    min_notional: Decimal


@dataclass(frozen=True)
class CampaignDecision:
    reason: str
    campaign: PaperCampaign | None = None


def decide_campaign(
    opportunity: Opportunity,
    stances: tuple[Stance, ...],
    utility: UtilityInputs,
    snapshot: RiskSnapshot,
    limits: RiskLimits,
    constraints: InstrumentConstraints,
    decision_ns: int,
    price: Decimal,
    requested_notional: Decimal,
    already_allocated: frozenset[str],
    *,
    calibration_verified: bool,
    experiment: RulePaperExperiment | None = None,
    decision_regime: RegimeObservation | None = None,
    protection: CampaignProtection | None = None,
    protection_required: bool = False,
    stop_budget: StopBudget | None = None,
    stop_exposure: StopExposure | None = None,
) -> CampaignDecision:
    """Caller must verify calibration provenance and reserve admitted risk atomically.

    A true verification flag is not a certificate or a way to promote economic
    claims. The app must supply it only from the calibration verification boundary.
    Until that boundary exists, production callers must leave it false. An
    explicit preregistered rule experiment measures policy responses without a
    forecast; it retains identity/protection/risk checks and a distinct result.
    """
    if experiment is not None:
        if not experiment.start_ns <= decision_ns < experiment.end_ns:
            return CampaignDecision("OUTSIDE_RULE_EXPERIMENT_WINDOW")
        if protection is None:
            return CampaignDecision("RULE_EXPERIMENT_REQUIRES_PROTECTION")
    if opportunity.identity_status != "CANONICAL" or opportunity.direction not in {"LONG", "SHORT"}:
        return CampaignDecision("UNRESOLVED_OPPORTUNITY_IDENTITY")
    if opportunity.opportunity_id in already_allocated:
        return CampaignDecision("DUPLICATE_OPPORTUNITY")
    if decision_ns < opportunity.anchor_ns:
        return CampaignDecision("FUTURE_OPPORTUNITY")
    if decision_ns >= opportunity.expires_ns:
        return CampaignDecision("EXPIRED")
    if any(s.decision_ns > decision_ns for s in stances):
        return CampaignDecision("FUTURE_EVIDENCE")
    reconciled = reconcile(opportunity, stances)
    if reconciled != "SUPPORTED_OBSERVATION":
        return CampaignDecision(reconciled)
    if protection_required and protection is None:
        return CampaignDecision("MISSING_CAMPAIGN_GEOMETRY")
    if protection is not None:
        if (
            protection.opportunity_id != opportunity.opportunity_id
            or protection.instrument_id != opportunity.instrument_id
            or protection.direction != opportunity.direction
        ):
            return CampaignDecision("MISMATCHED_CAMPAIGN_GEOMETRY")
        if protection.observed_ns > decision_ns or protection.expires_ns <= decision_ns:
            return CampaignDecision("INVALID_CAMPAIGN_GEOMETRY_CLOCK")
        sign = 1 if opportunity.direction == "LONG" else -1
        if (price - protection.stop_price) * sign <= 0 or (
            protection.target_price - price
        ) * sign <= 0:
            return CampaignDecision("PRICE_OUTSIDE_CAMPAIGN_GEOMETRY")
    if experiment is None:
        if not calibration_verified:
            return CampaignDecision("UNVERIFIED_CALIBRATION")
        utility_result = utility_admission(utility)
        if utility_result != "UTILITY_ELIGIBLE":
            return CampaignDecision(utility_result)
    if stop_budget is not None:
        if protection is None or stop_exposure is None:
            return CampaignDecision("MISSING_STOP_RISK_INPUTS")
        sized, reason = stop_budget_notional(
            snapshot,
            stop_exposure,
            stop_budget,
            price=price,
            stop=protection.stop_price,
            direction=opportunity.direction,
            decision_ns=decision_ns,
        )
        if sized is None:
            return CampaignDecision(reason)
        requested_notional = min(requested_notional, sized)
    elif stop_exposure is not None:
        return CampaignDecision("MISSING_STOP_BUDGET_POLICY")
    admitted = admit(
        snapshot,
        limits,
        decision_ns,
        price,
        requested_notional,
        constraints.step,
        constraints.min_quantity,
        constraints.max_quantity,
        constraints.min_notional,
    )
    if admitted.quantity is None:
        return CampaignDecision(admitted.reason)
    return CampaignDecision(
        "RULE_PAPER_EXPERIMENT_SELECTED" if experiment is not None else "PAPER_CAMPAIGN_ADMITTED",
        PaperCampaign(
            "paper-" + opportunity.opportunity_id,
            opportunity.opportunity_id,
            opportunity.instrument_id,
            opportunity.direction,
            admitted.quantity,
            decision_ns,
            min(opportunity.expires_ns, protection.expires_ns)
            if protection
            else opportunity.expires_ns,
            protection.stop_price if protection else None,
            protection.target_price if protection else None,
            protection.close_invalidation_price if protection else None,
            protection.live_channel_bars if protection else None,
            protection.validity_indicator if protection else None,
            protection.close_breach_price if protection else None,
            decision_regime=decision_regime,
        ),
    )
