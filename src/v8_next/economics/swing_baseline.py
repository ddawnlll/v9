"""NX06 (#427) — a pre-registered swing baseline family over the existing grammar.

Three policies, fixed before any window is opened:

* ``cash`` — no exposure; the comparison floor (its return is zero by construction
  and is reported as such, never as a "measured" result).
* ``causal_trend`` — the existing ``trend-continuation-v2`` grammar with
  ``timeout-only-v1`` protection: direction and a timeout, no stop/target.
* ``plain_swing`` — the existing ``range-breakout-48-v1`` grammar with the
  existing **squeeze** protection (``protection_at``), which is where the 336-bar
  expiry lives.

Two different intervals live in this module and are never conflated:

* the **opportunity TTL** the grammar itself stamps on an episode
  (``opportunity.expires_ns``), and
* the **open-trade expiry** the protection applies (``min(opportunity TTL,
  336 bars)`` for the squeeze family).

A signal is only ever computed from data available at the decision instant, and
an outcome is only ever computed from bars strictly *after* the decision bar: a
bar that opens beyond the stop fills at the open, never at the stop price, and
when one bar touches both barriers the stop is taken first (documented
conservatism, not a claim about the venue's ordering).

The replayed outcomes here are a *decision-plane diagnostic*: real bars, real
costs, but not the native engine's fill model. Engine fills stay the engine's
business (see ``adapters/portfolio_backtest``); this module never presents a
replay as a venue settlement, an economic certificate, or a capacity claim.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Literal, Sequence, cast

from v8_next.domain.market import Candle, CausalFrame
from v8_next.economics.decisions import Opportunity
from v8_next.economics.grammar import (
    POLICY_HORIZON_BARS,
    POLICY_REQUIRED_BARS,
    grammar_opportunity,
)
from v8_next.economics.protection import CampaignProtection, protection_at

SWING_FAMILY_VERSION = "v87-swing-family-v1"

#: Price increment the engine's own instrument metadata declares for these
#: perpetuals (``adapters.portfolio_backtest._instrument``): the protection must
#: round to the same tick the engine will trade, not to a venue quote convention.
ENGINE_TICK = Decimal("0.01")

#: Shared risk/cost contract. Every policy in the family is compared under exactly
#: these numbers, so a difference in outcomes is never a difference in assumptions.
SHARED_CONTRACT: dict[str, str] = {
    "initial_balance_usdt": "10000",
    "risk_per_trade_fraction": "0.01",
    "taker_fee": "0.0005",
    "maker_fee": "0.0002",
    "funding": "MISSING_NOT_FED_TO_THIS_PATH",
    "slippage": "NOT_MODELLED_IN_REPLAY",
}


@dataclass(frozen=True)
class SwingPolicySpec:
    """One pre-registered member of the family."""

    policy_id: str
    grammar_policy: str | None
    protection_policy: str | None
    description: str

    def identity(self) -> str:
        """Pre-registration hash: recorded before a fold is opened."""
        payload = {
            "policy_id": self.policy_id,
            "grammar_policy": self.grammar_policy,
            "protection_policy": self.protection_policy,
            "required_bars": None
            if self.grammar_policy is None
            else POLICY_REQUIRED_BARS[self.grammar_policy],
            "horizon_bars": None
            if self.grammar_policy is None
            else POLICY_HORIZON_BARS[self.grammar_policy],
            "contract": SHARED_CONTRACT,
            "family_version": SWING_FAMILY_VERSION,
        }
        return "sha256:" + hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

    def as_dict(self) -> dict[str, Any]:
        return {
            "policy_id": self.policy_id,
            "grammar_policy": self.grammar_policy,
            "protection_policy": self.protection_policy,
            "description": self.description,
            "identity": self.identity(),
        }


SWING_FAMILY: tuple[SwingPolicySpec, ...] = (
    SwingPolicySpec(
        policy_id="cash",
        grammar_policy=None,
        protection_policy=None,
        description="no exposure; the comparison floor",
    ),
    SwingPolicySpec(
        policy_id="causal_trend",
        grammar_policy="trend-continuation-v2",
        protection_policy="timeout-only-v1",
        description="existing causal trend grammar, timeout-only protection",
    ),
    SwingPolicySpec(
        policy_id="plain_swing",
        grammar_policy="range-breakout-48-v1",
        protection_policy="squeeze:baseline:v2",
        description="existing 48-bar range breakout with the existing squeeze protection",
    ),
)


def family_registry() -> dict[str, str]:
    """``policy_id -> pre-registration hash`` for the whole family."""
    return {spec.policy_id: spec.identity() for spec in SWING_FAMILY}


def policy_spec(policy_id: str) -> SwingPolicySpec:
    for spec in SWING_FAMILY:
        if spec.policy_id == policy_id:
            return spec
    raise KeyError(f"unknown swing policy {policy_id!r}")


@dataclass(frozen=True)
class ExpiryHorizon:
    """The same expiry expressed in the units a reviewer needs to check it."""

    bars: int
    bar_ns: int

    @property
    def hours(self) -> float:
        return self.bars * self.bar_ns / 3_600_000_000_000

    @property
    def days(self) -> float:
        return self.hours / 24.0

    @property
    def ns(self) -> int:
        return self.bars * self.bar_ns

    def as_dict(self) -> dict[str, Any]:
        return {
            "bars": self.bars,
            "bar_ns": self.bar_ns,
            "hours": self.hours,
            "days": self.days,
            "ns": self.ns,
        }


def open_trade_expiry(spec: SwingPolicySpec, bar_ns: int) -> ExpiryHorizon | None:
    """Open-trade expiry of a policy's protection, in bars and in wall units.

    ``None`` for the cash policy and for timeout-only protection, which stamps no
    protection expiry of its own (the opportunity TTL governs it).
    """
    if bar_ns <= 0:
        raise ValueError("bar_ns must be positive")
    if spec.protection_policy in (None, "timeout-only-v1"):
        return None
    from v8_next.economics.protection import PROTECTION_TTL_BARS

    # the TTL table is keyed by protection FAMILY ("squeeze"), while the policy
    # name is "squeeze:baseline:v2" — split exactly the way protection.py does.
    family = spec.protection_policy.split(":")[0]
    ttl = PROTECTION_TTL_BARS.get(family, PROTECTION_TTL_BARS["_default"])
    return ExpiryHorizon(bars=ttl, bar_ns=bar_ns)


def opportunity_ttl_bars(opportunity: Opportunity, bar_ns: int) -> float:
    """The grammar's own episode TTL in bars — a different interval from the above."""
    return (opportunity.expires_ns - opportunity.anchor_ns) / bar_ns


@dataclass(frozen=True)
class SwingDecision:
    """A decision stamped with everything a later reviewer needs, nothing more."""

    policy_id: str
    opportunity_id: str
    instrument_id: str
    direction: Literal["LONG", "SHORT"]
    decision_ns: int
    entry_reference: Decimal
    stop_price: Decimal
    target_price: Decimal
    expires_ns: int
    opportunity_ttl_bars: float
    protection_policy: str | None
    open_trade_expiry_bars: int | None

    def identity(self) -> str:
        payload = {
            "policy_id": self.policy_id,
            "opportunity_id": self.opportunity_id,
            "direction": self.direction,
            "decision_ns": self.decision_ns,
            "entry_reference": str(self.entry_reference),
            "stop_price": str(self.stop_price),
            "target_price": str(self.target_price),
            "expires_ns": self.expires_ns,
        }
        return "sha256:" + hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

    def as_dict(self) -> dict[str, Any]:
        return {
            "policy_id": self.policy_id,
            "opportunity_id": self.opportunity_id,
            "instrument_id": self.instrument_id,
            "direction": self.direction,
            "decision_ns": self.decision_ns,
            "entry_reference": str(self.entry_reference),
            "stop_price": str(self.stop_price),
            "target_price": str(self.target_price),
            "expires_ns": self.expires_ns,
            "opportunity_ttl_bars": self.opportunity_ttl_bars,
            "protection_policy": self.protection_policy,
            "open_trade_expiry_bars": self.open_trade_expiry_bars,
            "decision_identity": self.identity(),
        }


def swing_signal(
    frame: CausalFrame,
    spec: SwingPolicySpec,
    *,
    bar_ns: int,
    readings: tuple[Any, ...] = (),
) -> SwingDecision | None:
    """Decision at ``frame.decision_ns`` from the closed bars available then.

    ``frame`` is the causal view: the grammar and the protection both refuse data
    that is not yet available, so a decision can never be stamped from a future
    bar. The protection's expiry is ``min(opportunity TTL, protection TTL)``, which
    is exactly the distinction NX06.R1 asks to keep visible.
    """
    if spec.grammar_policy is None:  # cash never opens a campaign
        return None
    opportunity = grammar_opportunity(frame, spec.grammar_policy)
    if opportunity is None:
        return None
    # The grammar also emits NEUTRAL episodes; they are observations, not campaigns.
    if opportunity.direction not in ("LONG", "SHORT"):
        return None
    direction = cast(Literal["LONG", "SHORT"], opportunity.direction)
    expiry = open_trade_expiry(spec, bar_ns)
    protection: CampaignProtection | None = protection_at(
        frame, opportunity, spec.protection_policy or "timeout-only-v1", ENGINE_TICK, readings=readings
    )
    if protection is not None:
        stop, target, expires = protection.stop_price, protection.target_price, protection.expires_ns
    else:
        # timeout-only: no bracket, the opportunity TTL is the whole contract
        entry = frame.candles[-1].close
        stop = entry
        target = entry
        expires = opportunity.expires_ns
    return SwingDecision(
        policy_id=spec.policy_id,
        opportunity_id=opportunity.opportunity_id,
        instrument_id=opportunity.instrument_id,
        direction=direction,
        decision_ns=frame.decision_ns,
        entry_reference=frame.candles[-1].close,
        stop_price=stop,
        target_price=target,
        expires_ns=expires,
        opportunity_ttl_bars=opportunity_ttl_bars(opportunity, bar_ns),
        protection_policy=spec.protection_policy,
        open_trade_expiry_bars=None if expiry is None else expiry.bars,
    )


@dataclass(frozen=True)
class ReplayOutcome:
    """Outcome of one decision, computed only from bars after the decision bar."""

    exit_kind: Literal["STOP", "TARGET", "EXPIRY", "OPEN_AT_CUTOFF"]
    exit_ns: int
    exit_price: Decimal
    bars_held: int
    hours_held: float
    gross_return: float
    fee_cost_return: float
    net_return: float
    gap_through_stop: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "exit_kind": self.exit_kind,
            "exit_ns": self.exit_ns,
            "exit_price": str(self.exit_price),
            "bars_held": self.bars_held,
            "hours_held": self.hours_held,
            "gross_return": self.gross_return,
            "fee_cost_return": self.fee_cost_return,
            "net_return": self.net_return,
            "gap_through_stop": self.gap_through_stop,
        }


def replay_bracket(
    decision: SwingDecision,
    future_bars: Sequence[Candle],
    *,
    bar_ns: int,
    bps_fee: Decimal,
    has_bracket: bool = True,
) -> ReplayOutcome:
    """Walk the bars strictly after the decision bar and resolve the campaign.

    Rules, applied bar by bar in event order and never with hindsight:

    * a bar whose *open* is already beyond the stop fills at that open (a gap is
      not filled at the stop price);
    * when a single bar touches both the stop and the target, the **stop** is taken
      first — the conservative, deterministic reading of an unknowable intrabar
      order;
    * expiry is the decision's own ``expires_ns``; the campaign is closed at the
      first bar end at or after it;
    * a campaign still open at the end of the supplied bars is reported as
      ``OPEN_AT_CUTOFF`` with its mark-to-market, never as a closed trade.
    """
    if bar_ns <= 0:
        raise ValueError("bar_ns must be positive")
    for candle in future_bars:
        if candle.start_ns < decision.decision_ns:
            raise ValueError("replay received a bar at or before the decision instant")
    entry = decision.entry_reference
    sign = 1 if decision.direction == "LONG" else -1
    fee = float(bps_fee) * 2.0  # entry and exit legs at the same taker fee

    def outcome(kind: str, ns: int, price: Decimal, bars: int, gap: bool) -> ReplayOutcome:
        gross = sign * (float(price) - float(entry)) / float(entry)
        return ReplayOutcome(
            exit_kind=kind,  # type: ignore[arg-type]
            exit_ns=ns,
            exit_price=price,
            bars_held=bars,
            hours_held=bars * bar_ns / 3_600_000_000_000,
            gross_return=gross,
            fee_cost_return=fee,
            net_return=gross - fee,
            gap_through_stop=gap,
        )

    bars = list(future_bars)
    for index, candle in enumerate(bars, start=1):
        if has_bracket:
            stop_hit = (
                candle.open <= decision.stop_price
                if decision.direction == "LONG"
                else candle.open >= decision.stop_price
            )
            if stop_hit:
                return outcome("STOP", candle.end_ns, candle.open, index, True)
            stop_touched = (
                candle.low <= decision.stop_price
                if decision.direction == "LONG"
                else candle.high >= decision.stop_price
            )
            target_touched = (
                candle.high >= decision.target_price
                if decision.direction == "LONG"
                else candle.low <= decision.target_price
            )
            if stop_touched:
                return outcome("STOP", candle.end_ns, decision.stop_price, index, False)
            if target_touched:
                return outcome("TARGET", candle.end_ns, decision.target_price, index, False)
        if candle.end_ns >= decision.expires_ns:
            return outcome("EXPIRY", candle.end_ns, candle.close, index, False)
    if not bars:
        raise ValueError("no bars after the decision instant")
    last = bars[-1]
    return outcome("OPEN_AT_CUTOFF", last.end_ns, last.close, len(bars), False)
