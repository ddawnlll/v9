"""Ordered economic allocation over one reconciled portfolio snapshot.

Caller supplies explicit priority and owns atomic application of the returned
reservations. This module neither executes orders nor invents calibrated utility.
"""

from dataclasses import dataclass, replace
from decimal import Decimal

from v8_next.domain.experiment import RulePaperExperiment
from v8_next.economics.controller import CampaignDecision, InstrumentConstraints, decide_campaign
from v8_next.economics.decisions import Opportunity, Stance, UtilityInputs
from v8_next.economics.protection import CampaignProtection
from v8_next.economics.regime import RegimeObservation
from v8_next.risk.admission import RiskLimits, RiskSnapshot, admit
from v8_next.risk.sizing import StopBudget, StopExposure


@dataclass(frozen=True)
class AllocationProposal:
    opportunity: Opportunity
    stances: tuple[Stance, ...]
    utility: UtilityInputs
    calibration_verified: bool
    constraints: InstrumentConstraints
    price: Decimal
    requested_notional: Decimal
    protection: CampaignProtection
    decision_regime: RegimeObservation | None = None


def allocate_ordered(
    proposals: tuple[AllocationProposal, ...],
    snapshots: dict[str, RiskSnapshot],
    limits: RiskLimits,
    *,
    decision_ns: int,
    already_allocated: frozenset[str],
    stop_budget: StopBudget | None = None,
    stop_exposure: StopExposure | None = None,
    experiment: RulePaperExperiment | None = None,
) -> tuple[CampaignDecision, ...]:
    """Input order is the declared priority, never implicitly sorted by votes.

    Exposure snapshots share one global account state. Initial scalar reservation
    semantics conservatively charge all existing reservations to every exposure;
    explicit exposure reservations narrow that charge when supplied. New batch
    reservations charge the global limit and only their own exposure limit.
    Native multi-instrument execution still requires separate qualification.
    """
    used = set(already_allocated)
    reserved = Decimal(0)
    by_exposure: dict[str, Decimal] = {}
    current_stop_exposure = stop_exposure
    results = []
    account_state = None
    for proposal in proposals:
        exposure = proposal.opportunity.exposure_id
        if exposure not in snapshots:
            raise ValueError("missing exposure snapshot")
        snapshot = snapshots[exposure]
        identity = (
            snapshot.equity,
            snapshot.gross_notional,
            snapshot.reserved_notional,
            snapshot.as_of_ns,
            snapshot.reconciled,
        )
        if account_state is None:
            account_state = identity
        elif identity != account_state:
            raise ValueError("allocation snapshots disagree on global account state")
        bounded_snapshot = replace(
            snapshot,
            reserved_notional=snapshot.reserved_notional + reserved,
            exposure_reserved_notional=(
                snapshot.reserved_notional
                if snapshot.exposure_reserved_notional is None
                else snapshot.exposure_reserved_notional
            )
            + by_exposure.get(exposure, Decimal(0)),
        )
        bound_price = max(proposal.protection.stop_price, proposal.protection.target_price)
        capacity = admit(
            bounded_snapshot,
            limits,
            decision_ns,
            bound_price,
            proposal.requested_notional,
            proposal.constraints.step,
            proposal.constraints.min_quantity,
            proposal.constraints.max_quantity,
            proposal.constraints.min_notional,
        )
        requested = (
            capacity.quantity * proposal.price if capacity.quantity is not None else Decimal(0)
        )
        if stop_budget is not None:
            # Same contract as sizing.stop_budget_notional (budget/distance*price):
            # risk is measured entry-to-stop, direction-aware. The full
            # stop-to-target band is NOT the sizing basis (it only bounds the
            # worst-case pending reservation in StopExposure).
            direction = proposal.opportunity.direction
            if direction == "LONG":
                distance = proposal.price - proposal.protection.stop_price
            elif direction == "SHORT":
                distance = proposal.protection.stop_price - proposal.price
            else:
                distance = Decimal(0)
            if distance > 0:
                requested = min(
                    requested, snapshot.equity * stop_budget.risk_fraction / distance * proposal.price
                )
            # distance <= 0 flows to decide_campaign, which rejects invalid
            # geometry with PRICE_OUTSIDE_CAMPAIGN_GEOMETRY.
        decision = decide_campaign(
            proposal.opportunity,
            proposal.stances,
            proposal.utility,
            bounded_snapshot,
            limits,
            proposal.constraints,
            decision_ns,
            proposal.price,
            requested,
            frozenset(used),
            calibration_verified=proposal.calibration_verified,
            experiment=experiment,
            decision_regime=proposal.decision_regime,
            protection=proposal.protection,
            protection_required=True,
            stop_budget=stop_budget,
            stop_exposure=current_stop_exposure,
        )
        results.append(decision)
        if decision.campaign is not None:
            # Unsubmitted market entry can occur anywhere inside the permitted
            # stop/target band. Book the worst-case (band) basis to match
            # native_stop_exposure's pending reservation. After fill, native
            # re-measures on realized entry-to-stop distance, so booked heat
            # steps down by design (see native_stop_exposure docstring).
            notional = decision.campaign.quantity * max(
                proposal.protection.stop_price, proposal.protection.target_price
            )
            reserved += notional
            by_exposure[exposure] = by_exposure.get(exposure, Decimal(0)) + notional
            used.add(proposal.opportunity.opportunity_id)
            if current_stop_exposure is not None:
                current_stop_exposure = replace(
                    current_stop_exposure,
                    open_and_reserved_risk=current_stop_exposure.open_and_reserved_risk
                    + decision.campaign.quantity
                    * abs(proposal.protection.target_price - proposal.protection.stop_price),
                    active_and_reserved_campaigns=current_stop_exposure.active_and_reserved_campaigns
                    + 1,
                )
    return tuple(results)
