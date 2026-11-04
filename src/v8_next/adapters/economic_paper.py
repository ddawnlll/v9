"""Thin quote-to-economic-controller adapter for the local native paper engine."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict
from decimal import Decimal
from typing import Callable

from nautilus_trader.model import Currency, InstrumentId, QuoteTick, Venue

from v8_next.adapters.campaign import PaperCampaignAdapter
from v8_next.adapters.portfolio_equity import EquityMark
from v8_next.adapters.portfolio_risk import native_portfolio_risk
from v8_next.adapters.settlements import funding_exposure_history
from v8_next.domain.experiment import RulePaperExperiment
from v8_next.domain.market import CausalFrame
from v8_next.domain.positioning import PositioningReading
from v8_next.economics.allocation import AllocationProposal, allocate_ordered
from v8_next.economics.controller import InstrumentConstraints, decide_campaign
from v8_next.economics.decisions import (
    Opportunity,
    Stance,
    UtilityInputs,
)
from v8_next.economics.grammar import POLICIES, grammar_opportunity
from v8_next.economics.habitat import HABITAT_VERSION, apply_habitat
from v8_next.economics.observer_policy import policy_stances, validate_observer_policy
from v8_next.economics.protection import PROTECTION_POLICIES, protection_at
from v8_next.economics.regime import RegimeObservation, observe_regime
from v8_next.risk.admission import RiskLimits
from v8_next.risk.sizing import StopBudget

CalibrationProvider = Callable[[Opportunity, int], tuple[UtilityInputs, bool]]
# Closed-lifetime funding evidence to a readmission verdict. The callable
# receives native exposure lifetimes plus closure history (instrument_id,
# opened_ns, closed_ns, is_closed) and the decision clock, and returns a
# mapping with at least a "reconciled" boolean plus auditable detail.
# None (the default) means no reconciliation input: any position history
# keeps blocking. A verdict never certifies venue cash settlement.
FundingReconciliation = Callable[[list[dict[str, object]], int], dict[str, object]]


class EconomicPaperAdapter(PaperCampaignAdapter):
    def __new__(cls, *args: object, **kwargs: object) -> EconomicPaperAdapter:
        # The native Strategy allocator accepts only its engine config; domain
        # constructor arguments belong to Python __init__, not the native base.
        return super().__new__(cls)

    def __init__(
        self,
        frames: Mapping[int | tuple[str, int], CausalFrame],
        limits: RiskLimits,
        constraints: InstrumentConstraints | Mapping[str, InstrumentConstraints],
        requested_notional: Decimal,
        calibration: CalibrationProvider | None = None,
        *,
        observer: str = "squeeze",
        grammar: str = "range-breakout-48-v1",
        campaign_policy: str = "timeout-only-v1",
        stop_budget: StopBudget | None = None,
        positioning_readings: tuple[PositioningReading, ...] = (),
        experiment: RulePaperExperiment | None = None,
        funding_reconciliation: FundingReconciliation | None = None,
    ) -> None:
        super().__init__(())
        self.observer = validate_observer_policy(observer)
        self.experiment = experiment
        if grammar not in POLICIES:
            raise ValueError("unknown opportunity grammar")
        self.grammar = grammar
        if campaign_policy not in PROTECTION_POLICIES:
            raise ValueError("unknown campaign policy")
        self.campaign_policy = campaign_policy
        self.stop_budget = stop_budget
        self.positioning_readings = positioning_readings
        self.frames: dict[tuple[str, int], CausalFrame] = {}
        for key, frame in frames.items():
            identity = key if isinstance(key, tuple) else (frame.instrument_id, key)
            if identity != (frame.instrument_id, frame.decision_ns) or identity in self.frames:
                raise ValueError("duplicate or mismatched paper frame identity")
            self.frames[identity] = frame
        self.validity_frames = dict(self.frames)
        self.limits = limits
        self.constraints = constraints
        self.instruments = frozenset(key[0] for key in self.frames)
        if isinstance(constraints, Mapping) and not self.instruments <= constraints.keys():
            raise ValueError("missing per-instrument constraints")
        self.requested_notional = requested_notional
        self.calibration = calibration
        self.funding_reconciliation = funding_reconciliation
        self.decisions: list[dict[str, object]] = []
        self.allocated: set[str] = set()

    def on_start(self) -> None:
        for instrument in sorted(self.instruments):
            self.subscribe_quotes(InstrumentId.from_str(instrument))

    def on_quote(self, quote: QuoteTick) -> None:
        if self.callback_failure is not None:
            return
        try:
            self.process_economic_quote(quote)
        except Exception as error:
            if self.callback_failure is None:
                self.callback_failure = f"{type(error).__name__}: {error}"
            raise

    def process_economic_quote(self, quote: QuoteTick) -> None:
        super().on_quote(quote)
        if self.callback_failure is not None:
            return
        frame = self.frames.get((str(quote.instrument_id), quote.ts_init))
        if frame is None:
            return
        constraints = (
            self.constraints[str(quote.instrument_id)]
            if isinstance(self.constraints, Mapping)
            else self.constraints
        )
        opportunity = grammar_opportunity(frame, self.grammar)
        resolved = (
            opportunity
            if opportunity
            and opportunity.identity_status == "CANONICAL"
            and opportunity.direction in {"LONG", "SHORT"}
            else None
        )
        stances = policy_stances(frame, resolved, self.observer, readings=self.positioning_readings)
        stance = stances[0]
        regime = observe_regime(frame, readings=self.positioning_readings)
        # Habitat demotion is abstention-only and causal: originals stay in the
        # record while admission uses the adjusted tuple.
        habitat_stances, habitat_report = apply_habitat(stances, regime, resolved)
        record: dict[str, object] = {
            "decision_ns": quote.ts_init,
            "stance": asdict(stance),
            "stances": [asdict(s) for s in stances],
            "observer_policy": self.observer,
            "campaign_policy": self.campaign_policy,
            "opportunity": asdict(opportunity) if opportunity else None,
            "regime": asdict(regime),
            "habitat_version": HABITAT_VERSION,
            "habitat": habitat_report,
            "admitted_stances": [asdict(s) for s in habitat_stances],
            "claim_status": "NO_ECONOMIC_CLAIM",
            "authority": "RULE_PAPER_EXPERIMENT" if self.experiment else "CALIBRATED_PAPER",
        }
        if not frame.candles or quote.ts_init - frame.candles[-1].end_ns > 2 * 3600 * 10**9:
            record["reason"] = "STALE_DATA"
        elif opportunity is None:
            record["reason"] = stance.reason
        elif resolved is None:
            record["reason"] = "UNRESOLVED_OPPORTUNITY_IDENTITY"
        elif (
            self.has_unprojected_submission()
            or self.cache.positions_open()
            or self.cache.orders_open()
            or any(
                c.campaign_id not in self.submitted | self.expired | self.invalidated
                for c in self.campaigns
            )
        ):
            record["reason"] = "ONE_ACTIVE_EXPOSURE_LIMIT"
        elif self.cache.positions():
            # Earlier guards establish no open position, open order, unprojected
            # submission or pending campaign. Closed lifetimes can still carry
            # late funding liabilities, so readmission requires verified query
            # coverage for every lifetime, never a zero-risk assumption.
            lifetimes = funding_exposure_history(
                {
                    "positions": [
                        {
                            "instrument_id": str(position.instrument_id),
                            "opened_ns": position.ts_opened,
                            "closed_ns": position.ts_closed,
                            "is_closed": position.is_closed,
                        }
                        for position in self.cache.positions()
                    ],
                    "position_closures": [
                        {
                            "instrument_id": record["instrument_id"],
                            "opened_ns": record["opened_ns"],
                            "closed_ns": record["closed_ns"],
                        }
                        for record in self.closed_position_records()
                    ],
                }
            )
            funding_detail: dict[str, object] = {
                "reconciled": False,
                "reason": "NO_RECONCILIATION_INPUT",
            }
            if self.funding_reconciliation is not None:
                funding_detail = self.funding_reconciliation(lifetimes, quote.ts_init)
            record["funding_lifetimes"] = lifetimes
            record["funding_coverage"] = funding_detail
            if not funding_detail.get("reconciled"):
                record["reason"] = "UNRECONCILED_FUNDING_AFTER_EXPOSURE"
            else:
                self._admit(
                    quote,
                    frame,
                    opportunity,
                    resolved,
                    habitat_stances,
                    regime,
                    record,
                    constraints,
                )
        else:
            self._admit(
                quote,
                frame,
                opportunity,
                resolved,
                habitat_stances,
                regime,
                record,
                constraints,
            )
        self.decisions.append(record)

    def _admit(
        self,
        quote: QuoteTick,
        frame: CausalFrame,
        opportunity: Opportunity,
        resolved: Opportunity | None,
        stances: tuple[Stance, ...],
        regime: RegimeObservation,
        record: dict[str, object],
        constraints: InstrumentConstraints,
    ) -> None:
        """Shared portfolio/utility/protection admission after all exposure guards."""
        account = self.cache.account_for_venue(Venue("BINANCE"))
        if account is None:
            raise ValueError("native paper account unavailable")
        # Earlier guards establish no position history or outstanding entry;
        # cash equity therefore has no unresolved position funding adjustment.
        portfolio = native_portfolio_risk(
            self.cache,
            self.campaigns,
            pending_ids=frozenset(),
            instrument_exposures={opportunity.instrument_id: opportunity.exposure_id},
            marks={
                str(quote.instrument_id): EquityMark(
                    quote.ask_price, quote.ts_event, quote.ts_init
                )
            },
            equity=account.balance_total(Currency.from_str("USDT")).as_decimal(),
            accounting_reconciled=True,
            observed_ns=quote.ts_init,
        )
        if portfolio is None:
            record["reason"] = "UNRECONCILED_PORTFOLIO_RISK"
            self.decisions.append(record)
            return
        snapshot = portfolio.snapshots[opportunity.exposure_id]
        utility, verified = (
            self.calibration(opportunity, quote.ts_init)
            if self.calibration and self.experiment is None
            else (UtilityInputs(None, None, None, None, None, None, None), False)
        )
        protection = None
        if self.campaign_policy != "timeout-only-v1":
            instrument = self.cache.instrument(quote.instrument_id)
            if instrument is None:
                raise ValueError("protection instrument metadata missing")
            protection = protection_at(
                frame,
                opportunity,
                self.campaign_policy,
                instrument.price_increment.as_decimal(),
                readings=self.positioning_readings,
            )
            record["protection"] = (
                {
                    k: str(v) if isinstance(v, Decimal) else v
                    for k, v in asdict(protection).items()
                }
                if protection
                else None
            )
        if protection is not None:
            decision = allocate_ordered(
                (
                    AllocationProposal(
                        opportunity,
                        stances,
                        utility,
                        verified,
                        constraints,
                        quote.ask_price.as_decimal()
                        if opportunity.direction == "LONG"
                        else quote.bid_price.as_decimal(),
                        self.requested_notional,
                        protection,
                        decision_regime=regime,
                    ),
                ),
                portfolio.snapshots,
                self.limits,
                decision_ns=quote.ts_init,
                already_allocated=frozenset(self.allocated),
                stop_budget=self.stop_budget,
                stop_exposure=portfolio.stop_exposure if self.stop_budget is not None else None,
                experiment=self.experiment,
            )[0]
        else:
            decision = decide_campaign(
                opportunity,
                stances,
                utility,
                snapshot,
                self.limits,
                constraints,
                quote.ts_init,
                quote.ask_price.as_decimal()
                if opportunity.direction == "LONG"
                else quote.bid_price.as_decimal(),
                self.requested_notional,
                frozenset(self.allocated),
                calibration_verified=verified,
                experiment=self.experiment,
                decision_regime=regime,
                protection=protection,
                protection_required=self.campaign_policy != "timeout-only-v1",
                stop_budget=self.stop_budget,
                stop_exposure=portfolio.stop_exposure if self.stop_budget is not None else None,
            )
        record["reason"] = decision.reason
        if decision.campaign is not None:
            self.allocated.add(opportunity.opportunity_id)
            self.campaigns += (decision.campaign,)
            record["campaign_id"] = decision.campaign.campaign_id
        self.decisions.append(record)
