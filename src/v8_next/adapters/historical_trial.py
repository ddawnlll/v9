"""Offline policy experiment. Never a calibrated economic admission path."""

import hashlib
import json
from dataclasses import asdict, replace
from decimal import Decimal
from typing import Any

from nautilus_trader.model import Bar, BarType, Currency, Venue

from v8_next.adapters.campaign import PaperCampaignAdapter
from v8_next.adapters.portfolio_equity import EquityMark, aligned_native_equity
from v8_next.adapters.portfolio_risk import native_portfolio_risk
from v8_next.domain.campaign import PaperCampaign
from v8_next.domain.config import PaperConfig
from v8_next.domain.market import Candle, frame_at
from v8_next.economics.decisions import linear_exposure_id, reconcile
from v8_next.economics.grammar import grammar_opportunity
from v8_next.economics.habitat import HABITAT_VERSION, apply_habitat
from v8_next.economics.observer_policy import policy_stances
from v8_next.economics.protection import protection_at
from v8_next.economics.regime import observe_regime
from v8_next.risk.admission import RiskLimits, admit
from v8_next.risk.sizing import stop_budget_notional


class HistoricalTrial(PaperCampaignAdapter):
    """Counterfactual policy application to native real-bar replay, not edge authority.

    Positions are experimental responses to a frozen rule. No utility estimate,
    calibration receipt, quote or synthetic market input is fabricated. Missing
    historical availability and microstructure keep results diagnostic only.
    """

    def __new__(cls, *args: object, **kwargs: object) -> "HistoricalTrial":
        return super().__new__(cls)

    def __init__(
        self,
        source: tuple[Candle, ...],
        policy: PaperConfig,
        *,
        selection_end_ns: int | None = None,
        selection_start_ns: int | None = None,
    ) -> None:
        super().__init__(())
        self.source = {(c.instrument_id, c.end_ns): c for c in source}
        if len(self.source) != len(source):
            raise ValueError("duplicate trial candle boundary")
        self.instruments = frozenset(c.instrument_id for c in source)
        self.prefixes: dict[str, list[Candle]] = {instrument: [] for instrument in self.instruments}
        self.boundary_bars: dict[str, Bar] = {}
        if selection_end_ns is not None and (
            type(selection_end_ns) is not int or selection_end_ns <= 0
        ):
            raise ValueError("invalid campaign selection cutoff")
        if selection_start_ns is not None and (
            type(selection_start_ns) is not int
            or selection_start_ns < 0
            or (selection_end_ns is not None and selection_start_ns >= selection_end_ns)
        ):
            raise ValueError("invalid campaign selection start")
        self.selection_start_ns = selection_start_ns
        self.selection_end_ns = selection_end_ns
        self.policy = policy
        self.decisions: list[dict[str, Any]] = []
        self.equity_marks: list[dict[str, Any]] = []
        self.failure: str | None = None

    def on_start(self) -> None:
        for instrument in sorted(self.instruments):
            self.subscribe_bars(BarType.from_str(f"{instrument}-1-HOUR-LAST-EXTERNAL"))

    def on_bar(self, bar: Bar) -> None:
        if self.failure is not None:
            return
        try:
            if bar.ts_init != bar.ts_event:
                raise ValueError("historical boundary requires explicit close-time model")
            instrument = str(bar.bar_type.instrument_id)
            if instrument not in self.instruments:
                raise ValueError("unexpected trial instrument")
            if self.boundary_bars and any(
                b.ts_event != bar.ts_event for b in self.boundary_bars.values()
            ):
                raise ValueError("incomplete portfolio bar boundary")
            if instrument in self.boundary_bars:
                raise ValueError("duplicate portfolio boundary bar")
            self.boundary_bars[instrument] = bar
            if self.boundary_bars.keys() == self.instruments:
                self.mark_equity(bar)
                for key in sorted(self.boundary_bars):
                    self.process_bar(self.boundary_bars[key])
                self.boundary_bars.clear()
        except Exception as error:
            self.failure = f"{type(error).__name__}: {error}"
            raise

    def on_stop(self) -> None:
        if self.boundary_bars and self.failure is None:
            self.failure = "ValueError: incomplete terminal portfolio bar boundary"

    def process_bar(self, bar: Bar) -> None:
        candle = self.source[(str(bar.bar_type.instrument_id), bar.ts_event)]
        prefix = self.prefixes[candle.instrument_id]
        prefix.append(replace(candle, available_ns=candle.end_ns))
        frame = frame_at(candle.instrument_id, bar.ts_init, tuple(prefix))
        if self.selection_start_ns is not None and bar.ts_init < self.selection_start_ns:
            self.decisions.append(
                dict(
                    decision_ns=bar.ts_init,
                    opportunity=None,
                    authority="OFFLINE_COUNTERFACTUAL",
                    claim_status="NO_ECONOMIC_CLAIM",
                    reason="WARMUP_ONLY_SELECTION_NOT_STARTED",
                )
            )
            return
        self.observe_validity(frame)
        # Prior decisions execute no earlier than the NEXT real bar close. The
        # native bar model supplies fills; no synthetic QuoteTick is constructed.
        self.advance_campaigns(
            bar.bar_type.instrument_id, bar.ts_init, bar.close.as_decimal(), bar.close.as_decimal()
        )
        if self.selection_end_ns is not None and bar.ts_init >= self.selection_end_ns:
            self.decisions.append(
                dict(
                    decision_ns=bar.ts_init,
                    opportunity=None,
                    authority="OFFLINE_COUNTERFACTUAL",
                    claim_status="NO_ECONOMIC_CLAIM",
                    reason="FOLLOWUP_ONLY_SELECTION_CLOSED",
                )
            )
            return
        opportunity = grammar_opportunity(frame, self.policy.grammar_policy)
        regime = observe_regime(frame)
        record: dict[str, Any] = {
            "decision_ns": bar.ts_init,
            "opportunity": asdict(opportunity) if opportunity else None,
            "authority": "OFFLINE_COUNTERFACTUAL",
            "claim_status": "NO_ECONOMIC_CLAIM",
            "regime": asdict(regime),
        }
        self.decisions.append(record)
        if (
            opportunity is None
            or opportunity.identity_status != "CANONICAL"
            or opportunity.direction not in {"LONG", "SHORT"}
        ):
            record["reason"] = "NO_CANONICAL_DIRECTIONAL_OPPORTUNITY"
            return
        stances = policy_stances(frame, opportunity, self.policy.observer_policy)
        record["stances"] = [asdict(s) for s in stances]
        habitat_stances, habitat_report = apply_habitat(stances, regime, opportunity)
        record["habitat_version"] = HABITAT_VERSION
        record["habitat"] = habitat_report
        record["admitted_stances"] = [asdict(s) for s in habitat_stances]
        result = reconcile(opportunity, habitat_stances)
        if result != "SUPPORTED_OBSERVATION":
            record["reason"] = result
            return
        pending_ids = frozenset(
            c.campaign_id
            for c in self.campaigns
            if c.campaign_id not in self.submitted | self.expired | self.invalidated
        )
        if (
            self.has_unresolved_campaign(candle.instrument_id)
            or any(
                str(p.instrument_id) == candle.instrument_id for p in self.cache.positions_open()
            )
            or any(str(o.instrument_id) == candle.instrument_id for o in self.cache.orders_open())
            or any(
                c.instrument_id == candle.instrument_id and c.campaign_id in pending_ids
                for c in self.campaigns
            )
        ):
            record["reason"] = "EXPERIMENT_EXPOSURE_OCCUPIED"
            return
        instrument = self.cache.instrument(bar.bar_type.instrument_id)
        account = self.cache.account_for_venue(Venue("BINANCE"))
        if instrument is None or account is None:
            raise ValueError("native trial instrument/account missing")
        protection = protection_at(
            frame, opportunity, self.policy.campaign_policy, instrument.price_increment.as_decimal()
        )
        if self.policy.campaign_policy != "timeout-only-v1" and protection is None:
            record["reason"] = "MISSING_CAMPAIGN_GEOMETRY"
            return
        marks = {
            key: EquityMark(value.close, value.ts_event, value.ts_init)
            for key, value in self.boundary_bars.items()
        }
        equity = aligned_native_equity(
            self.cache,
            marks,
            required_instruments=self.instruments,
            boundary_ns=bar.ts_event,
            observed_ns=bar.ts_init,
            venue=Venue("BINANCE"),
            currency=Currency.from_str("USDT"),
        )
        exposure_map = {key: linear_exposure_id(key) for key in self.instruments}
        if equity is None or any(value is None for value in exposure_map.values()):
            record["reason"] = "UNQUALIFIED_PORTFOLIO_VALUATION"
            return
        if self.has_unprojected_submission():
            record["reason"] = "UNPROJECTED_NATIVE_SUBMISSION"
            return
        portfolio = native_portfolio_risk(
            self.cache,
            self.campaigns,
            pending_ids=pending_ids,
            instrument_exposures={
                key: value for key, value in exposure_map.items() if value is not None
            },
            marks=marks,
            equity=sum(equity, Decimal(0)),
            accounting_reconciled=True,
            observed_ns=bar.ts_init,
        )
        if portfolio is None:
            record["reason"] = "UNQUALIFIED_PORTFOLIO_RISK"
            return
        snapshot = portfolio.snapshots[opportunity.exposure_id]
        valuation_price = (
            max(protection.stop_price, protection.target_price)
            if protection is not None
            else bar.close.as_decimal()
        )
        requested = self.policy.max_notional
        if self.policy.stop_budget is not None:
            if protection is None:
                record["reason"] = "MISSING_STOP_RISK_INPUTS"
                return
            sized, reason = stop_budget_notional(
                snapshot,
                portfolio.stop_exposure,
                self.policy.stop_budget,
                price=bar.close.as_decimal(),
                stop=protection.stop_price,
                direction=opportunity.direction,
                decision_ns=bar.ts_init,
            )
            if sized is None:
                record["reason"] = reason
                return
            # Match protected paper allocation: reserve the full unsubmitted
            # band, not a predicted close-price fill.
            band = abs(protection.target_price - protection.stop_price)
            requested = min(
                requested,
                snapshot.equity * self.policy.stop_budget.risk_fraction / band * valuation_price,
            )
        admission = admit(
            snapshot,
            RiskLimits(Decimal(1), self.policy.max_exposure_fraction, self.policy.max_notional, 0),
            bar.ts_init,
            valuation_price,
            requested,
            instrument.size_increment.as_decimal(),
            instrument.min_quantity.as_decimal(),
            instrument.max_quantity.as_decimal(),
            instrument.min_notional.as_decimal(),
        )
        if admission.quantity is None:
            record["reason"] = admission.reason
            return
        if admission.quantity * bar.close.as_decimal() < instrument.min_notional.as_decimal():
            record["reason"] = "BELOW_VENUE_MINIMUM"
            return
        campaign = PaperCampaign(
            "trial-" + opportunity.opportunity_id,
            opportunity.opportunity_id,
            candle.instrument_id,
            opportunity.direction,
            admission.quantity,
            bar.ts_init,
            protection.expires_ns if protection else opportunity.expires_ns,
            protection.stop_price if protection else None,
            protection.target_price if protection else None,
            protection.close_invalidation_price if protection else None,
            protection.live_channel_bars if protection else None,
            protection.validity_indicator if protection else None,
            protection.close_breach_price if protection else None,
            decision_regime=regime,
        )
        self.campaigns += (campaign,)
        record["reason"] = "COUNTERFACTUAL_POLICY_SELECTED_NOT_UTILITY_ADMITTED"
        record["campaign_id"] = campaign.campaign_id

    def mark_equity(self, bar: Bar) -> None:
        """Native cash plus native position valuation, before callback actions.

        This is not end-of-timestamp equity: orders submitted by this callback
        and subsequent same-clock events can still change cash and exposure.
        """
        candle = self.source[(str(bar.bar_type.instrument_id), bar.ts_event)]
        if candle.instrument_id != str(bar.bar_type.instrument_id):
            raise ValueError("equity source instrument mismatch")
        if self.equity_marks and bar.ts_event <= self.equity_marks[-1]["end_ns"]:
            raise ValueError("equity boundaries must increase")
        projected = aligned_native_equity(
            self.cache,
            {
                key: EquityMark(value.close, value.ts_event, value.ts_init)
                for key, value in self.boundary_bars.items()
            },
            venue=Venue("BINANCE"),
            currency=Currency.from_str("USDT"),
            observed_ns=bar.ts_init,
            required_instruments=self.instruments,
            boundary_ns=bar.ts_event,
        )
        if projected is None:
            raise ValueError("incomplete native equity valuation")
        cash, unrealized = projected
        positions = self.cache.positions_open()
        valuation_inputs = {
            key: {
                "price": str(value.close.as_decimal()),
                "event_ns": value.ts_event,
                "observed_ns": value.ts_init,
                "source_hash": self.source[(key, value.ts_event)].source_hash,
            }
            for key, value in sorted(self.boundary_bars.items())
        }
        source_identity = (
            candle.source_hash
            if len(self.instruments) == 1
            else hashlib.sha256(
                json.dumps(valuation_inputs, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
        )
        self.equity_marks.append(
            {
                "end_ns": bar.ts_event,
                "observed_ns": bar.ts_init,
                "phase": "PRE_STRATEGY_BAR_CALLBACK",
                "currency": "USDT",
                "cash": str(cash),
                "unrealized_pnl": str(unrealized),
                "equity": str(cash + unrealized),
                "open_positions": len(positions),
                "close_price": str(bar.close.as_decimal()) if len(self.instruments) == 1 else None,
                "source_hash": source_identity,
                "valuation_inputs": valuation_inputs,
            }
        )
