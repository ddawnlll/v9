"""Adaptive (V9 policy) execution arm — **offline simulation only**.

The arm is a native :class:`nautilus_trader.trading.Strategy`. It reuses the V8
expert strategy's execution machinery (the ``OrderFactory.bracket`` submission,
the reduce-only protective children, the per-order client-order-id ledger) but
takes its ENTER/HOLD/REDUCE/EXIT decisions from the V9 advisory policy chain:

    closed bar -> build_view/assemble_context -> belief.advance
               -> experts.evidence.produce_evidence -> OpportunityBook snapshot
               -> economics.policy.TraderDecisionPolicy.evaluate
               -> native order (ENTER) or native reduce-only path (REDUCE/EXIT)

Authorization boundary (structural, not a runtime flag):

* Orders can only be created from inside this ``Strategy`` subclass, i.e. while a
  ``BacktestEngine`` is driving the bar callbacks. The module holds no venue
  adapter import, no credentials, no network client and no CLI entry point; the
  runner that instantiates it lives in :mod:`v8_next.evaluation.sim_arm` and
  refuses to construct without an explicit ``SimArmConfig(enabled=True)``.
* Default is OFF: ``SimArmConfig()`` has ``enabled=False`` and constructing the
  strategy with it raises :class:`SimArmDisabled`. There is no environment
  variable, no file and no implicit default that can turn the arm on.
* Every emitted order carries ``sim_arm=true``, ``advisory_only=false``,
  ``decision_id=<decision id>`` and ``arm_role=<role>`` in its native tags, so
  the audit ledger can join an order to the V9 decision that produced it without
  inferring identity from timestamps.

This module must never be imported by a live/dry-run entry point. The boundary
is pinned by ``tests/test_sim_adaptive_arm.py`` (import scan + call-site scan).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from decimal import ROUND_CEILING, Decimal
from typing import Any, Final, cast

from nautilus_trader.model import (
    Bar,
    ClientOrderId,
    OrderSide,
    OrderType,
    Position,
    Price,
    Quantity,
)

from v8_next.adapters.expert_strategy import (
    ExpertEnsembleStrategy,
    ExpertStrategyConfig,
    _bar_duration_ns,
)
from v8_next.adapters.native_callback_trace import capture_native_callback
from v8_next.adapters.protection_invariants import (
    Direction,
    OrderRole,
    ProtectionEvent,
    ProtectionEventKind,
    Side,
)
from v8_next.adapters.risk_sizing import check_max_notional
from v8_next.belief import advance
from v8_next.belief.state import BeliefStatus, MarketBeliefV1, UncertaintyState
from v8_next.domain.decision import (
    DecisionAction,
    OpportunityLifecycle,
    VenueOrderStatus,
    lifecycle_of,
)
from v8_next.domain.horizons import Timeframe, assemble_context
from v8_next.domain.market import Candle, CausalFrame, frame_at_incremental
from v8_next.domain.observation import (
    Observation,
    ObservationKind,
    QualityState,
    build_view,
)
from v8_next.domain.positioning import PositioningReading
from v8_next.economics.decisions import Opportunity, StanceKind, opportunity_at
from v8_next.economics.grammar import grammar_opportunity
from v8_next.economics.legality import PositionPresence
from v8_next.economics.policy import (
    CapacityState,
    DecisionRequest,
    PortfolioState,
    PositionState,
    TraderDecisionPolicy,
)
from v8_next.experts.cache import FeatureCache
from v8_next.experts.evidence import EvidenceBundle, produce_evidence
from v8_next.opportunities.models import (
    EconomicExposureStructure,
    ExposureDirection,
    OpportunityRecord,
    OpportunityStatus,
)

__all__ = [
    "ADVISORY_TAG",
    "AUTHORIZED_ARM_ROLES",
    "AdaptiveSimStrategy",
    "ArmOrderMarker",
    "DECISION_TAG_PREFIX",
    "SIM_ARM_TAG",
    "SimArmConfig",
    "SimArmDisabled",
    "SimArmRefused",
]

#: Native order tag markers. ``sim_arm`` and ``advisory_only`` are the two
#: markers the audit ledger reads to separate a simulated arm order from any
#: other order in the same account.
SIM_ARM_TAG: Final[str] = "sim_arm=true"
ADVISORY_TAG: Final[str] = "advisory_only=false"
DECISION_TAG_PREFIX: Final[str] = "decision_id="
ROLE_TAG_PREFIX: Final[str] = "arm_role="

#: Order roles this arm can emit. Closed set: an unknown role would make the
#: protection ledger ambiguous.
AUTHORIZED_ARM_ROLES: Final[tuple[str, ...]] = (
    "ENTRY",
    "STOP",
    "TARGET",
    "REDUCE",
    "EXIT",
)

#: Declared band for the belief's uncertainty *state* enum, identical to the
#: mapping the shadow-parity adaptive chain publishes
#: (``v8_next.evaluation.shadow_parity.UNCERTAINTY_BANDS``, pinned equal by the
#: test suite). The policy compares it to ``ENTRY_UNCERTAINTY_CEILING``.
UNCERTAINTY_BANDS: Final[dict[UncertaintyState, Decimal]] = {
    UncertaintyState.LOW: Decimal("0.10"),
    UncertaintyState.MEDIUM: Decimal("0.40"),
    UncertaintyState.HIGH: Decimal("0.75"),
    UncertaintyState.UNKNOWN: Decimal("1.00"),
}

#: Evidence class every order-side audit event of this arm declares.
ARM_EVIDENCE_CLASS: Final[str] = "OBSERVED_SIMULATION"


class SimArmRefused(RuntimeError):
    """The adaptive arm refused to run. ``code`` is a stable wire value."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


class SimArmDisabled(SimArmRefused):
    """The arm was constructed without an explicit ``enabled=True``."""

    def __init__(self, message: str = "SimArmConfig.enabled is not true") -> None:
        super().__init__("SIM_ARM_DISABLED", message)


@dataclass(frozen=True)
class SimArmConfig:
    """Execution parameters for the adaptive arm.

    ``enabled`` defaults to ``False``. Enabling the arm requires an explicit
    ``SimArmConfig(enabled=True)``: there is deliberately no environment
    variable, config file or CLI switch that can set it.

    Sizing, stop and target are the *same policy* the V8 strategy applies
    (``target_notional`` steps, else ``order_quantity``; ``bracket_stop_pct`` /
    ``bracket_target_pct`` around the decision close; ``max_notional_per_order``
    as the pre-submit risk gate), so a V9-vs-V8 comparison isolates the decision
    source and not the sizing.
    """

    enabled: bool = False
    instrument_id: str = "BTCUSDT-PERP.BINANCE"
    bar_type_str: str = "BTCUSDT-PERP.BINANCE-1-HOUR-LAST-EXTERNAL"
    grammar_policy: str = "range-breakout-48-v1"
    min_support_quorum: int = 1
    #: Matches the V8 audit/portfolio baseline gate (`audit.pipeline._config`:
    #: quorum 1, contradiction tolerance 28). Both arms must share it or the
    #: V9-vs-V8 delta would include a gate change, not only the decision source.
    max_contradiction_tolerance: int = 28
    order_quantity: Decimal = Decimal("0.01")
    #: Matches the measured V8 portfolio baseline (`per_leg_notional=1000`).
    #: Fixed 0.01 BTC would size a different notional as price moves, making the
    #: absolute PnL incomparable even when the trade and net-R series match.
    target_notional: Decimal | None = Decimal("1000")
    bracket_stop_pct: Decimal = Decimal("0.02")
    bracket_target_pct: Decimal = Decimal("0.04")
    max_notional_per_order: Decimal | None = None
    max_concurrent_positions: int = 1

    def __post_init__(self) -> None:
        if not isinstance(self.enabled, bool):
            raise TypeError("enabled must be a bool")
        if not self.instrument_id.strip():
            raise ValueError("instrument_id cannot be empty")
        if not self.bar_type_str.strip():
            raise ValueError("bar_type_str cannot be empty")
        if self.min_support_quorum < 1:
            raise ValueError("min_support_quorum must be at least 1")
        if self.max_contradiction_tolerance < 0:
            raise ValueError("max_contradiction_tolerance cannot be negative")
        if self.order_quantity <= 0:
            raise ValueError("order_quantity must be positive")
        if self.target_notional is not None and self.target_notional <= 0:
            raise ValueError("target_notional must be positive when set")
        if self.bracket_stop_pct <= 0 or self.bracket_target_pct <= 0:
            raise ValueError("bracket percentages must be positive")
        if self.bracket_target_pct < self.bracket_stop_pct:
            raise ValueError("bracket_target_pct must be at least bracket_stop_pct")
        if self.max_notional_per_order is not None and self.max_notional_per_order <= 0:
            raise ValueError("max_notional_per_order must be positive when set")
        if self.max_concurrent_positions < 1:
            raise ValueError("max_concurrent_positions must be at least 1")

    def baseline_config(self) -> ExpertStrategyConfig:
        """The V8 config carrying the *identical* execution parameters.

        The comparison baseline is the existing expert ensemble driven by the
        same tape window, fees and execution profile; only the decision source
        differs. Every sizing/stop/target/risk parameter is mapped here so the
        two configs cannot drift.
        """
        return ExpertStrategyConfig(
            instrument_id=self.instrument_id,
            bar_type_str=self.bar_type_str,
            grammar_policy=self.grammar_policy,
            min_support_quorum=self.min_support_quorum,
            max_contradiction_tolerance=self.max_contradiction_tolerance,
            order_quantity=self.order_quantity,
            target_notional=self.target_notional,
            bracket_stop_pct=self.bracket_stop_pct,
            bracket_target_pct=self.bracket_target_pct,
            max_notional_per_order=self.max_notional_per_order,
            max_concurrent_positions=self.max_concurrent_positions,
        )


@dataclass(frozen=True, slots=True)
class ArmOrderMarker:
    """The decision identity an emitted order carries (ledger join surface)."""

    client_order_id: str
    decision_id: str
    role: str
    sim_arm: bool = True
    advisory_only: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "client_order_id": self.client_order_id,
            "decision_id": self.decision_id,
            "role": self.role,
            "sim_arm": self.sim_arm,
            "advisory_only": self.advisory_only,
        }


@dataclass(slots=True)
class _BeliefFacts:
    """``economics.policy.BeliefView`` over one belief snapshot."""

    version_hash: str
    regime: str
    uncertainty: Decimal
    stale: bool
    abstain: bool
    as_of_ns: int


@dataclass(slots=True)
class _OpportunityFacts:
    """``economics.policy.OpportunityView`` over one book episode."""

    opportunity_id: str
    lifecycle: OpportunityLifecycle
    as_of_ns: int


@dataclass(slots=True)
class _StanceFacts:
    """``economics.policy.ExpertStanceView`` over one evidence row."""

    expert_id: str
    stance: str
    weight: Decimal


@dataclass(slots=True)
class _OrderFacts:
    """What the arm knows about an order it emitted."""

    role: OrderRole
    arm_role: str
    decision_id: str
    direction: Direction | None
    position_id: str = ""


def _as_decimal(value: Any) -> Decimal:
    """Nautilus mixes ``Price``/``Quantity`` objects with plain ``float`` fields.

    ``Position.avg_px_open`` is a float in 2.0.0rc4 while ``Order.price`` is a
    ``Price``. Converting at the boundary keeps Decimal arithmetic without
    guessing which representation a given native field uses.
    """
    if isinstance(value, Decimal):
        return value
    converter = getattr(value, "as_decimal", None)
    if callable(converter):
        return cast(Decimal, converter())
    return Decimal(str(value))


def _canonical_digest(tag: str, payload: object) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(f"{tag}:{encoded}".encode()).hexdigest()


def _direction_of_side(order_side: str) -> Direction | None:
    name = order_side.split(".")[-1].upper()
    if name == "BUY":
        return Direction.LONG
    if name == "SELL":
        return Direction.SHORT
    return None


class AdaptiveSimStrategy(ExpertEnsembleStrategy):
    """V9 policy-driven strategy. Simulation only; see the module docstring.

    Inherits the V8 execution machinery (order factory, bracket submission,
    order/position telemetry ledgers, ``_account_equity``) and replaces the
    admission decision with the V9 advisory policy chain. The parent's
    ``on_bar`` is deliberately overridden: this arm must not take the ensemble
    consensus as its decision.
    """

    def __init__(
        self,
        config: SimArmConfig,
        *,
        source_candles: dict[int, Candle] | None = None,
        readings: tuple[PositioningReading, ...] = (),
        audit_recorder: Any | None = None,
    ) -> None:
        if not isinstance(config, SimArmConfig):
            raise TypeError(f"expected SimArmConfig, got {type(config).__name__}")
        if config.enabled is not True:
            raise SimArmDisabled(
                "the adaptive arm is off by default; construct SimArmConfig(enabled=True) "
                "explicitly to run an offline simulation"
            )
        super().__init__(
            config.baseline_config(),
            source_candles=source_candles,
            readings=readings,
            audit_recorder=audit_recorder,
        )
        self.sim_config = config
        # ---- V9 chain state -------------------------------------------------
        self._policy = TraderDecisionPolicy()
        self._feature_cache = FeatureCache()
        self._belief: MarketBeliefV1 | None = None
        self._observations: list[Observation] = []
        self._frame: CausalFrame | None = None
        # ---- arm ledgers ----------------------------------------------------
        #: Extended decision ledger (the parent's ``decisions`` keeps its schema).
        self.arm_decisions: list[dict[str, Any]] = []
        #: Submission ledger: one marker per emitted order.
        self.order_markers: list[ArmOrderMarker] = []
        #: Protection-invariants input, in observed native order.
        self.protection_events: list[ProtectionEvent] = []
        self.native_callback_trace: list[dict[str, Any]] = []
        self.book_transitions: list[dict[str, Any]] = []
        # ---- native order bookkeeping ---------------------------------------
        self._order_facts: dict[str, _OrderFacts] = {}
        #: Protective submission awaiting its native ACK. Published together with
        #: the ACK so the invariant checker never observes an intra-step
        #: submission→ACK gap as a real missing acknowledgement.
        self._pending_protective: dict[str, ProtectionEvent] = {}
        self._pending_protective_ack: dict[str, ProtectionEvent] = {}
        self._position_decision: dict[str, str] = {}
        self._entry_opportunities: dict[str, str] = {}
        self._reduce_decision: dict[str, str] = {}
        self._live_protection: dict[str, str] = {}
        self._pending_flat: dict[str, ProtectionEvent] = {}
        #: Positions this session has seen open and not yet seen close. Used as a
        #: fail-closed overlap guard: a cache miss must never permit a second
        #: entry that would leave the first bracket under-sized.
        self._seen_open_positions: set[str] = set()
        self._reprotect: set[str] = set()
        self._any_order_seen = False

    # ------------------------------------------------------------------ entry
    def on_bar(self, bar: Bar) -> None:
        """One closed bar: advance the V9 chain, then act on its decision."""
        candle = self._candle_from(bar)
        self.candles.append(candle)
        self._observations.append(_observation_of(candle, len(self._observations)))
        cut = candle.end_ns

        self._flush_pending_flat(unconditional=True)
        self._flush_pending_pairs()
        self._reprotect_remaining(candle)
        for transitioned in self.opportunity_book.on_candle(candle):
            self.book_transitions.append(
                {
                    "opportunity_id": transitioned.opportunity_id,
                    "status": str(transitioned.status),
                    "event_ns": cut,
                }
            )

        frame = frame_at_incremental(
            self._frame, str(self.instrument_id), int(bar.ts_init), tuple(self.candles)
        )
        self._frame = frame

        view = build_view(
            self._observations,
            cut_time_ns=cut,
            copy_inputs=False,
            assume_ordered=True,
        )
        context = assemble_context(
            {Timeframe.H1: view}, instrument_id=str(self.instrument_id), decision_time_ns=cut
        )
        belief = advance(self._belief, view, context, now_ns=cut)
        self._belief = belief

        opportunity = self._detect(frame)
        record = self._remember_opportunity(opportunity, candle)
        bundle = produce_evidence(
            view=view,
            context=context,
            frame=frame,
            cache=self._feature_cache,
            belief=belief,
            opportunity=opportunity,
            readings=self.readings,
        )
        supported = record is not None and self._supported(bundle)
        if supported and record is not None:
            self.opportunity_book.update_status(
                record.opportunity_id,
                OpportunityStatus.CONFIRMED,
                event_time_ns=cut,
                witness_ids=tuple(
                    row.expert_id for row in bundle.evidence if row.stance is StanceKind.SUPPORT
                ),
                reason="V9_EVIDENCE_QUORUM",
            )

        position = self._open_position()
        direction = (
            record.direction
            if record is not None
            else (position_direction(position) if position is not None else None)
        )
        opportunity_view = self._opportunity_view(cut, record, position)
        request = self._request(cut, belief, opportunity_view, bundle, position)
        decision = self._policy.evaluate(request)
        explanation = self._policy.explain(request)
        if decision.action is DecisionAction.ENTER and opportunity_view is not None:
            # Bind the actual entry decision before native submission callbacks
            # can fire. A later opportunity must never replace the entry thesis.
            self._entry_opportunities[decision.decision_id] = opportunity_view.opportunity_id
        outcome = self._apply(decision.action, decision.decision_id, candle, position, direction)
        if decision.action is DecisionAction.ENTER and not outcome.startswith("SUBMITTED_BRACKET_"):
            self._entry_opportunities.pop(decision.decision_id, None)

        self.arm_decisions.append(
            {
                "decision_id": decision.decision_id,
                "decision_time_ns": cut,
                "instrument_id": str(self.instrument_id),
                "action": decision.action.value,
                "intent_kind": decision.intent_kind.value,
                "reason_codes": [code.value for code in decision.reason_codes],
                "policy_version": decision.policy_version,
                "belief_version_hash": decision.belief_version_hash,
                "belief_status": belief.status.value,
                "belief_regime": belief.regime_label.value,
                "belief_uncertainty": belief.uncertainty.value,
                "support": str(explanation.support_weight),
                "contradict": str(explanation.contradict_weight),
                "net_ratio": (
                    None
                    if explanation.net_evidence_ratio is None
                    else str(explanation.net_evidence_ratio)
                ),
                "constraints": list(explanation.constraints),
                "opportunity_id": decision.opportunity_id,
                "lifecycle": (
                    None if explanation.lifecycle is None else explanation.lifecycle.value
                ),
                "position_presence": request.position.presence.value,
                "evidence_quorum_met": supported,
                "executed": outcome,
                "sim_arm": True,
                "advisory_only": False,
            }
        )

    def finish(self) -> None:
        """Flush buffered protection events at the end of a replay.

        A bracket child submitted for an entry that never filled has no native
        position to attribute to; emitting it with the empty position id is the
        truth (protection without exposure) and lets the invariant checker say
        so instead of silently dropping the order from the ledger.
        """
        # A protective submission that never received an ACK is published as
        # submitted so the checker reports a genuine MISSING_ACK instead of the
        # event silently disappearing.
        for client_id in sorted(self._pending_protective):
            self.protection_events.append(self._pending_protective[client_id])
        for client_id in sorted(self._pending_protective_ack):
            self.protection_events.append(self._pending_protective_ack[client_id])
        self._pending_protective.clear()
        self._pending_protective_ack.clear()
        self._flush_pending_flat(unconditional=True)

    # ----------------------------------------------------------- V9 chain bits
    def _candle_from(self, bar: Bar) -> Candle:
        """Reuse the source candle when the engine replays an identical bar."""
        known = self.source_candles.get(int(bar.ts_event))
        if known is not None:
            return known
        duration_ns = _bar_duration_ns(bar)
        start_ns = bar.ts_event - duration_ns if bar.ts_event >= duration_ns else 0
        return Candle(
            instrument_id=str(self.instrument_id),
            start_ns=start_ns,
            end_ns=int(bar.ts_event),
            open=bar.open.as_decimal(),
            high=bar.high.as_decimal(),
            low=bar.low.as_decimal(),
            close=bar.close.as_decimal(),
            volume=bar.volume.as_decimal(),
            received_ns=int(bar.ts_init),
            available_ns=int(bar.ts_init),
            source_hash="nautilus-bar-feed",
        )

    def _detect(self, frame: CausalFrame) -> Opportunity | None:
        policy = self.sim_config.grammar_policy
        if policy == "range-breakout-48-v1":
            return opportunity_at(frame)
        return grammar_opportunity(frame, policy)

    def _remember_opportunity(
        self, opportunity: Opportunity | None, candle: Candle
    ) -> OpportunityRecord | None:
        """Insert the bar's detection into the book (mirrors the V8 broker path)."""
        if opportunity is None:
            return None
        direction = (
            ExposureDirection.LONG
            if opportunity.direction == "LONG"
            else ExposureDirection.SHORT
        )
        raw_symbol = str(self.instrument_id).split(".")[0].replace("-PERP", "")
        try:
            exposure = self.exposure_resolver.resolve_ticker(raw_symbol, "binance-um", direction)
        except Exception:
            exposure = EconomicExposureStructure.single_perp(
                raw_symbol,
                raw_symbol.replace("USDT", ""),
                "binance-um",
                "USDT",
                direction,
            )
        entry_px = candle.close
        stop_dist = entry_px * self.sim_config.bracket_stop_pct
        target_dist = entry_px * self.sim_config.bracket_target_pct
        stop_px = (
            entry_px - stop_dist if direction is ExposureDirection.LONG else entry_px + stop_dist
        )
        target_px = (
            entry_px + target_dist if direction is ExposureDirection.LONG else entry_px - target_dist
        )
        record = OpportunityRecord.create(
            exposure=exposure,
            instrument_id=str(self.instrument_id),
            direction=direction,
            entry_price=entry_px,
            stop_price=stop_px,
            target_price=target_px,
            as_of_time_ns=candle.end_ns,
            valid_until_ns=opportunity.expires_ns,
            status=OpportunityStatus.CANDIDATE,
        )
        self.opportunity_book.insert(record)
        return record

    def _supported(self, bundle: EvidenceBundle) -> bool:
        """The V8 consensus gate, applied to the V9 evidence panel."""
        support = sum(1 for row in bundle.evidence if row.stance is StanceKind.SUPPORT)
        contradict = sum(1 for row in bundle.evidence if row.stance is StanceKind.CONTRADICT)
        return (
            support >= self.sim_config.min_support_quorum
            and contradict <= self.sim_config.max_contradiction_tolerance
        )

    def _opportunity_view(
        self, cut: int, record: OpportunityRecord | None, position: Position | None
    ) -> _OpportunityFacts | None:
        """The episode the policy may act on, as it was knowable at ``cut``.

        While flat, only the episode *this bar detected* is offered. With an
        open position, only the episode attached to its actual entry decision
        may be used. A missing join fails closed instead of selecting a newer,
        unrelated opportunity.
        """
        snapshot = self.opportunity_book.snapshot(cut)
        episodes = {episode.opportunity.opportunity_id: episode for episode in snapshot.episodes}
        if position is None:
            if record is None:
                return None
            episode = episodes.get(record.opportunity_id)
            if episode is None:
                return None
            return _OpportunityFacts(
                opportunity_id=episode.opportunity.opportunity_id,
                lifecycle=lifecycle_of(episode.status),
                as_of_ns=episode.opportunity.as_of_time_ns,
            )
        entry_decision = self._position_decision.get(str(position.id))
        opportunity_id = self._entry_opportunities.get(entry_decision or "")
        episode = episodes.get(opportunity_id or "")
        if episode is None or episode.opportunity.instrument_id != str(self.instrument_id):
            return None
        return _OpportunityFacts(
            opportunity_id=episode.opportunity.opportunity_id,
            lifecycle=lifecycle_of(episode.status),
            as_of_ns=episode.opportunity.as_of_time_ns,
        )

    def _request(
        self,
        cut: int,
        belief: MarketBeliefV1,
        opportunity_view: _OpportunityFacts | None,
        bundle: EvidenceBundle,
        position: Position | None,
    ) -> DecisionRequest:
        open_positions = self._open_positions()
        open_orders = list(self.cache.orders_open(instrument_id=self.instrument_id))
        if open_positions:
            presence = PositionPresence.OPEN
            held = open_positions[0]
            quantity = held.quantity.as_decimal()
            signed = quantity if position_direction(held) is ExposureDirection.LONG else -quantity
            exposure = abs(quantity) * _as_decimal(held.avg_px_open)
        elif open_orders:
            presence = PositionPresence.PENDING_INTENT
            signed = Decimal(0)
            exposure = Decimal(0)
        else:
            presence = PositionPresence.NONE
            signed = Decimal(0)
            exposure = Decimal(0)
        equity = self._equity()
        capacity = (
            CapacityState.UNKNOWN
            if equity is None
            else CapacityState.AVAILABLE
            if len(open_positions) < self.sim_config.max_concurrent_positions
            else CapacityState.FULL
        )
        committed = sum(
            (abs(p.quantity.as_decimal()) * _as_decimal(p.avg_px_open) for p in open_positions),
            Decimal(0),
        )
        return DecisionRequest(
            decision_time_ns=cut,
            instrument_id=str(self.instrument_id),
            venue_status=self._venue_status(),
            position=PositionState(presence=presence, quantity=signed, exposure=exposure),
            portfolio=PortfolioState(
                equity=Decimal(0) if equity is None else equity,
                committed_exposure=committed,
                capacity=capacity,
            ),
            opportunity=opportunity_view,
            belief=_BeliefFacts(
                version_hash=belief.version_hash,
                regime=belief.regime_label.value,
                uncertainty=UNCERTAINTY_BANDS[belief.uncertainty],
                stale=not belief.is_actionable_evidence(),
                abstain=belief.status is BeliefStatus.ABSTAIN,
                as_of_ns=belief.max_input_available_at,
            ),
            expert_stances=tuple(
                _StanceFacts(
                    expert_id=row.expert_id,
                    stance=row.stance.value,
                    weight=row.confidence,
                )
                for row in bundle.evidence
            ),
        )

    def _venue_status(self) -> VenueOrderStatus:
        """Pending native order state, never a guess.

        With nothing pending the honest reading is "no live order": the state of
        the last order the arm observed (``CANCELED`` once any order existed,
        ``REJECTED`` before the first submission). Both mean "flat and free to
        enter" to the legality table, and neither is ``UNKNOWN`` because the
        cache always holds a real reading here.
        """
        open_orders = list(self.cache.orders_open(instrument_id=self.instrument_id))
        if open_orders:
            for order in open_orders:
                if "SUBMITTED" in str(order.status).upper():
                    return VenueOrderStatus.SUBMITTED
            return VenueOrderStatus.ACCEPTED
        return VenueOrderStatus.CANCELED if self._any_order_seen else VenueOrderStatus.REJECTED

    def _equity(self) -> Decimal | None:
        instrument = self.cache.instrument(self.instrument_id)
        if instrument is None:
            return None
        money = self._account_equity(instrument)
        if money is None:
            return None
        return cast(Decimal, money.as_decimal())

    # ------------------------------------------------------------- execution
    def _apply(
        self,
        action: DecisionAction,
        decision_id: str,
        candle: Candle,
        position: Position | None,
        direction: ExposureDirection | None,
    ) -> str:
        """Translate one policy action into the native order flow."""
        if action is DecisionAction.ENTER:
            return self._enter(decision_id, candle, position, direction)
        if action is DecisionAction.HOLD:
            return "HOLD_NO_ORDER"
        if action is DecisionAction.REDUCE:
            return self._reduce(decision_id, position)
        if action is DecisionAction.EXIT:
            return self._exit(decision_id, position)
        return f"{action.value}_NO_ORDER"

    def _enter(
        self,
        decision_id: str,
        candle: Candle,
        position: Position | None,
        direction: ExposureDirection | None,
    ) -> str:
        if position is not None or self._seen_open_positions:
            return "SKIPPED_POSITION_OCCUPIED"
        if list(self.cache.orders_open(instrument_id=self.instrument_id)):
            return "SKIPPED_PENDING_ORDERS"
        if direction is None:
            return "SKIPPED_NO_DIRECTION"
        instrument = self.cache.instrument(self.instrument_id)
        if instrument is None:
            return "SKIPPED_NO_INSTRUMENT"
        entry_px = candle.close
        side = OrderSide.BUY if direction is ExposureDirection.LONG else OrderSide.SELL
        quantity, sizing_note = self._entry_quantity(instrument, entry_px)
        if quantity is None:
            return sizing_note
        stop_dist = entry_px * self.sim_config.bracket_stop_pct
        target_dist = entry_px * self.sim_config.bracket_target_pct
        if side is OrderSide.BUY:
            stop_px = entry_px - stop_dist
            target_px = entry_px + target_dist
        else:
            stop_px = entry_px + stop_dist
            target_px = entry_px - target_dist
        sl_price = Price(float(stop_px), instrument.price_precision)
        tp_price = Price(float(target_px), instrument.price_precision)
        short = decision_id[:12]
        children = list(
            self.order_factory.bracket(
                instrument_id=self.instrument_id,
                order_side=side,
                quantity=quantity,
                entry_order_type=OrderType.MARKET,
                entry_price=None,
                sl_trigger_price=sl_price,
                tp_price=tp_price,
                entry_tags=self._tags(decision_id, "ENTRY"),
                sl_tags=self._tags(decision_id, "STOP"),
                tp_tags=self._tags(decision_id, "TARGET"),
                entry_client_order_id=ClientOrderId(f"S-{short}-E"),
                sl_client_order_id=ClientOrderId(f"S-{short}-S"),
                tp_client_order_id=ClientOrderId(f"S-{short}-T"),
            )
        )
        order_direction = Direction.LONG if side is OrderSide.BUY else Direction.SHORT
        for child, role, line in zip(
            children,
            ("ENTRY", "STOP", "TARGET"),
            (OrderRole.ENTRY, OrderRole.STOP, OrderRole.TARGET),
            strict=True,
        ):
            client_order_id = str(child.client_order_id)
            self._order_facts[client_order_id] = _OrderFacts(
                role=line,
                arm_role=role,
                decision_id=decision_id,
                direction=order_direction,
            )
            self._mark_order(client_order_id, decision_id, role)
        self.submit_order_list(children)
        self._any_order_seen = True
        self._audit(
            "PROTECTIVE_STOP_CREATED",
            int(candle.end_ns),
            evidence=ARM_EVIDENCE_CLASS,
            decision_id=decision_id,
            arm_role="STOP",
            order_role="STOP_LOSS",
            order_type="STOP_MARKET",
            trigger_price=str(sl_price),
            quantity=str(quantity),
            child_index=1,
            sim_arm=True,
            advisory_only=False,
        )
        self._audit(
            "TARGET_CREATED",
            int(candle.end_ns),
            evidence=ARM_EVIDENCE_CLASS,
            decision_id=decision_id,
            arm_role="TARGET",
            order_role="TAKE_PROFIT",
            order_type="LIMIT",
            price=str(tp_price),
            quantity=str(quantity),
            child_index=2,
            sim_arm=True,
            advisory_only=False,
        )
        return f"SUBMITTED_BRACKET_{'LONG' if side is OrderSide.BUY else 'SHORT'}_{quantity}({sizing_note})"

    def _entry_quantity(self, instrument: Any, px: Decimal) -> tuple[Quantity | None, str]:
        """The V8 sizing policy: target notional (or fixed size), floored, capped.

        Returns ``(quantity, note)``. ``quantity`` is ``None`` with a named
        refusal instead of a silently defaulted size.
        """
        step = instrument.size_increment.as_decimal()
        precision = instrument.size_precision
        base = self.sim_config.order_quantity
        note = "FIXED_QUANTITY"
        if self.sim_config.target_notional is not None:
            if px <= 0 or step <= 0:
                return None, "SIZING_IMPOSSIBLE_NO_STEP_OR_PRICE"
            steps = (self.sim_config.target_notional / px) // step
            base = steps * step
            note = "TARGET_NOTIONAL"
        if base <= 0:
            return None, "BELOW_MIN_QUANTITY"
        quantity = Quantity.from_str(format(base, f".{precision}f"))
        price = Price(float(px), instrument.price_precision)
        if quantity.as_decimal() < instrument.min_quantity.as_decimal():
            if px <= 0 or step <= 0:
                return None, "SIZING_IMPOSSIBLE_NO_STEP_OR_PRICE"
            min_notional_qty = (
                (instrument.min_notional.as_decimal() / px) / step
            ).to_integral_value(rounding=ROUND_CEILING) * step
            floored = max(instrument.min_quantity.as_decimal(), min_notional_qty)
            floored_quantity = Quantity.from_str(format(floored, f".{precision}f"))
            allowed, _ = check_max_notional(
                floored_quantity, price, self.sim_config.max_notional_per_order
            )
            if not allowed:
                return None, "DENIED_MAX_NOTIONAL"
            quantity = floored_quantity
            note = "SIZED_TO_MIN_NOTIONAL"
        allowed, _ = check_max_notional(
            quantity, price, self.sim_config.max_notional_per_order
        )
        if not allowed:
            return None, "DENIED_MAX_NOTIONAL"
        return quantity, note

    def _reduce(self, decision_id: str, position: Position | None) -> str:
        if position is None:
            return "REDUCE_WITHOUT_POSITION"
        # Cancel-first reduction leaves exposure without its only verified stop.
        # Until a native, independently audited transition preserves coverage,
        # keep the advisory visible but refuse its order side effect.
        return "REDUCE_BLOCKED_UNVERIFIED_PROTECTION_TRANSITION"

    def _exit(self, decision_id: str, position: Position | None) -> str:
        if position is None:
            return "EXIT_WITHOUT_POSITION"
        # A submitted market exit is not a fill; retain the verified stop while
        # the native exit/protection handoff remains unqualified.
        return "EXIT_BLOCKED_UNVERIFIED_PROTECTION_TRANSITION"

    # ------------------------------------------------------- re-protection
    def _reprotect_remaining(self, candle: Candle) -> None:
        """Re-establish protection for a position reduced on an earlier bar."""
        if not self._reprotect:
            return
        instrument = self.cache.instrument(self.instrument_id)
        if instrument is None:
            return
        open_positions = {str(p.id): p for p in self._open_positions()}
        for position_id in sorted(self._reprotect):
            position = open_positions.get(position_id)
            if position is None:
                self._reprotect.discard(position_id)
                continue
            if self._protection_in_flight(position_id):
                continue
            if self._protective_orders(position_id):
                self._reprotect.discard(position_id)
                continue
            quantity = position.quantity
            if quantity.as_decimal() < instrument.min_quantity.as_decimal():
                self._reprotect.discard(position_id)
                self._record_protection_gap(position_id, "REPROTECT_BELOW_MIN_QUANTITY", candle)
                continue
            entry_px = _as_decimal(position.avg_px_open)
            long_side = position_direction(position) is ExposureDirection.LONG
            stop_dist = entry_px * self.sim_config.bracket_stop_pct
            target_dist = entry_px * self.sim_config.bracket_target_pct
            stop_px = entry_px - stop_dist if long_side else entry_px + stop_dist
            target_px = entry_px + target_dist if long_side else entry_px - target_dist
            side = OrderSide.SELL if long_side else OrderSide.BUY
            decision_id = self._reduce_decision.get(position_id) or self._position_decision.get(
                position_id, ""
            )
            if not decision_id:
                # No decision to attribute the protection to: refuse rather than
                # mint an order the ledger cannot join back to a decision.
                self._reprotect.discard(position_id)
                self._record_protection_gap(position_id, "REPROTECT_WITHOUT_DECISION", candle)
                continue
            tail = position_id[-6:]
            stop = self.order_factory.stop_market(
                instrument_id=self.instrument_id,
                order_side=side,
                quantity=quantity,
                trigger_price=Price(float(stop_px), instrument.price_precision),
                reduce_only=True,
                tags=self._tags(decision_id, "STOP"),
                client_order_id=ClientOrderId(f"R-{tail}-S"),
            )
            target = self.order_factory.limit(
                instrument_id=self.instrument_id,
                order_side=side,
                quantity=quantity,
                price=Price(float(target_px), instrument.price_precision),
                reduce_only=True,
                tags=self._tags(decision_id, "TARGET"),
                client_order_id=ClientOrderId(f"R-{tail}-T"),
            )
            direction = Direction.LONG if long_side else Direction.SHORT
            for order, role, arm_role in (
                (stop, OrderRole.STOP, "STOP"),
                (target, OrderRole.TARGET, "TARGET"),
            ):
                client_order_id = str(order.client_order_id)
                self._order_facts[client_order_id] = _OrderFacts(
                    role=role,
                    arm_role=arm_role,
                    decision_id=decision_id,
                    direction=direction,
                    position_id=position_id,
                )
                self._mark_order(client_order_id, decision_id, arm_role)
                self.submit_order(order, position_id=position.id)
            self._any_order_seen = True
            self._reprotect.discard(position_id)
            self._audit(
                "PROTECTIVE_STOP_CREATED",
                int(candle.end_ns),
                evidence=ARM_EVIDENCE_CLASS,
                decision_id=decision_id,
                arm_role="STOP",
                quantity=str(quantity),
                trigger_price=str(stop_px),
                reason="REPROTECT_REDUCED_POSITION",
                sim_arm=True,
                advisory_only=False,
            )

    def _protection_in_flight(self, position_id: str) -> bool:
        """True while a reduce/exit order for this position has not resolved."""
        for order in self.cache.orders_open(instrument_id=self.instrument_id):
            facts = self._order_facts.get(str(order.client_order_id))
            if facts is None:
                continue
            if facts.position_id != position_id:
                continue
            if facts.arm_role in {"REDUCE", "EXIT", "ENTRY"}:
                return True
        return False

    def _record_protection_gap(
        self, position_id: str, reason: str, candle: Candle
    ) -> None:
        self._audit(
            "POSITION_CHANGED",
            int(candle.end_ns),
            evidence=ARM_EVIDENCE_CLASS,
            position_id=position_id,
            protection_note=reason,
            sim_arm=True,
            advisory_only=False,
        )

    def _protective_orders(self, position_id: str) -> list[Any]:
        found = []
        for order in self.cache.orders_open(instrument_id=self.instrument_id):
            order_type = str(order.order_type).upper()
            if "STOP" in order_type or ("LIMIT" in order_type and bool(order.is_reduce_only)):
                facts = self._order_facts.get(str(order.client_order_id))
                if facts is not None and facts.position_id == position_id:
                    found.append(order)
        return found

    # ------------------------------------------------------------- ledgers
    def _tags(self, decision_id: str, arm_role: str) -> list[str]:
        if arm_role not in AUTHORIZED_ARM_ROLES:
            raise ValueError(f"unauthorized arm role {arm_role!r}")
        return [
            SIM_ARM_TAG,
            ADVISORY_TAG,
            f"{DECISION_TAG_PREFIX}{decision_id}",
            f"{ROLE_TAG_PREFIX}{arm_role}",
        ]

    def _mark_order(self, client_order_id: str, decision_id: str, arm_role: str) -> None:
        self.order_markers.append(
            ArmOrderMarker(
                client_order_id=client_order_id, decision_id=decision_id, role=arm_role
            )
        )

    def _open_positions(self) -> list[Position]:
        return list(self.cache.positions_open(instrument_id=self.instrument_id))

    def _open_position(self) -> Position | None:
        positions = self._open_positions()
        return positions[0] if positions else None

    def order_ledger(self) -> dict[str, Any]:
        """Digestible order identity: markers plus the decisions that minted them."""
        markers = [marker.to_dict() for marker in self.order_markers]
        return {
            "orders": markers,
            "decisions": list(self.arm_decisions),
            "digest": _canonical_digest("sim-arm-orders", markers),
            "decisions_digest": _canonical_digest(
                "sim-arm-decisions",
                [
                    [
                        row["decision_id"],
                        row["action"],
                        row["executed"],
                        row["reason_codes"],
                    ]
                    for row in self.arm_decisions
                ],
            ),
        }

    # ------------------------------------------------- native event translation
    def on_order_event(self, event: Any) -> None:
        """Record the native event, then translate it into a protection event."""
        capture_native_callback(self.native_callback_trace, "on_order_event", event)
        super().on_order_event(event)
        kind = _PROTECTION_KIND.get(type(event).__name__)
        if kind is None:
            return
        client_order_id = str(getattr(event, "client_order_id", ""))
        if not client_order_id:
            return
        order = self.cache.order(event.client_order_id)
        facts = self._order_facts.get(client_order_id)
        translated = self._protection_event(
            kind,
            int(getattr(event, "ts_event", 0) or 0),
            client_order_id,
            order,
            facts,
            event,
        )
        if translated is None:
            return
        self._admit_protection_event(translated, order, facts)

    def _protection_event(
        self,
        kind: ProtectionEventKind,
        ts_ns: int,
        client_order_id: str,
        order: Any,
        facts: _OrderFacts | None,
        native_event: Any,
    ) -> ProtectionEvent | None:
        if facts is None:
            return None
        if order is None and facts.role is not OrderRole.OTHER:
            return None
        quantity = order.quantity.as_decimal() if order is not None else Decimal(0)
        filled = order.filled_qty.as_decimal() if order is not None else Decimal(0)
        price = _order_price(order)
        side_text = str(order.side) if order is not None else ""
        position_id = facts.position_id or str(getattr(native_event, "position_id", "") or "")
        return ProtectionEvent(
            kind=kind,
            ts_ns=ts_ns,
            client_order_id=client_order_id,
            position_id=position_id,
            role=facts.role,
            direction=facts.direction,
            side=Side(side_text.split(".")[-1]) if side_text else None,
            quantity=quantity,
            filled_quantity=filled,
            price=price,
            reduce_only=bool(getattr(order, "is_reduce_only", False)),
            cancel_reject=type(native_event).__name__ == "OrderCancelRejected",
            reason=str(getattr(native_event, "reason", "") or ""),
        )

    def _admit_protection_event(
        self, event: ProtectionEvent, order: Any, facts: _OrderFacts | None
    ) -> None:
        protective = facts is not None and facts.role in (OrderRole.STOP, OrderRole.TARGET)
        if protective:
            client_id = event.client_order_id
            if event.kind is ProtectionEventKind.ORDER_SUBMITTED:
                # Publish the submission only together with its ACK. Publishing
                # it first makes the checker evaluate a transient "live order
                # without ACK" that the same engine step then resolves.
                self._pending_protective[client_id] = event
                return
            if event.kind is ProtectionEventKind.ACK:
                submission = self._pending_protective.pop(client_id, None)
                if submission is not None:
                    position_id = event.position_id or submission.position_id
                    if not position_id and facts is not None:
                        position_id = self._position_for_decision(facts.decision_id)
                    if position_id:
                        self._publish_protective(
                            replace(submission, position_id=position_id),
                            replace(event, position_id=position_id),
                        )
                    else:
                        # The ACK arrived before the position id is known; keep
                        # the pair until a position callback supplies it.
                        self._pending_protective[client_id] = submission
                        self._pending_protective_ack[client_id] = event
                    return
                self.protection_events.append(event)
                self._live_protection.setdefault(client_id, event.position_id)
                return
            if event.kind in (
                ProtectionEventKind.CANCELED,
                ProtectionEventKind.REJECTED,
                ProtectionEventKind.FILL,
            ):
                submission = self._pending_protective.pop(client_id, None)
                acknowledgement = self._pending_protective_ack.pop(client_id, None)
                if submission is not None:
                    # An unresolved ACK cannot be assigned to a position merely
                    # because a later fill carries one. Keep the missing binding
                    # visible to the checker and retain the callback in order.
                    if acknowledgement is None:
                        position_id = event.position_id or submission.position_id
                        if position_id:
                            submission = replace(submission, position_id=position_id)
                            event = replace(event, position_id=position_id)
                    self.protection_events.append(submission)
                if acknowledgement is not None:
                    self.protection_events.append(acknowledgement)
                self.protection_events.append(event)
                if event.kind in (
                    ProtectionEventKind.CANCELED,
                    ProtectionEventKind.REJECTED,
                ):
                    self._live_protection.pop(client_id, None)
                elif event.quantity > 0 and event.filled_quantity >= event.quantity:
                    self._live_protection.pop(client_id, None)
                return
        if (
            event.kind is ProtectionEventKind.FILL
            and facts is not None
            and facts.role is OrderRole.ENTRY
            and event.position_id
        ):
            self._position_decision[event.position_id] = facts.decision_id
            self._seen_open_positions.add(event.position_id)
            # The position id can become known on the entry fill before the
            # PositionOpened callback. Flush buffered protective events here so
            # their ACK never arrives before their submission in the ledger.
            self._flush_pending_children(event.position_id)
        self.protection_events.append(event)
        if event.position_id:
            self._flush_pending_flat(position_id=event.position_id)

    def _publish_protective(
        self, submission: ProtectionEvent, acknowledgement: ProtectionEvent
    ) -> None:
        self.protection_events.append(submission)
        self.protection_events.append(acknowledgement)
        self._live_protection[acknowledgement.client_order_id] = acknowledgement.position_id

    def on_position_opened(self, event: Any) -> None:
        capture_native_callback(self.native_callback_trace, "on_position_opened", event)
        super().on_position_opened(event)
        position_id = str(getattr(event, "position_id", "") or "")
        if not position_id:
            return
        self._seen_open_positions.add(position_id)
        quantity = getattr(event, "quantity", None)
        side = str(getattr(event, "side", "")).split(".")[-1].upper()
        direction = (
            Direction.LONG if side == "LONG" else Direction.SHORT if side == "SHORT" else None
        )
        if quantity is not None:
            self._append_snapshot(
                int(getattr(event, "ts_event", 0) or 0),
                position_id,
                _as_decimal(quantity),
                direction,
            )
        self._flush_pending_children(position_id)

    def _flush_pending_children(self, position_id: str) -> None:
        """Append buffered protective submission/ACK events for ``position_id``."""
        self._flush_pending_pairs(position_id)

    def _position_for_decision(self, decision_id: str) -> str:
        if not decision_id:
            return ""
        matches = [
            pid for pid, recorded in self._position_decision.items()
            if recorded == decision_id and pid in self._seen_open_positions
        ]
        return matches[0] if len(matches) == 1 else ""

    def _flush_pending_pairs(self, position_id: str | None = None) -> None:
        """Publish any submission/ACK pair whose position id is now known."""
        for client_id in sorted(self._pending_protective_ack):
            acknowledgement = self._pending_protective_ack.pop(client_id)
            submission = self._pending_protective.pop(client_id, None)
            if submission is None:
                continue
            resolved = acknowledgement.position_id or submission.position_id
            facts = self._order_facts.get(client_id)
            if not resolved and facts is not None:
                resolved = self._position_for_decision(facts.decision_id)
            if not resolved:
                # Keep the pair until a position callback supplies the id.
                self._pending_protective_ack[client_id] = acknowledgement
                self._pending_protective[client_id] = submission
                continue
            self._publish_protective(
                replace(submission, position_id=resolved),
                replace(acknowledgement, position_id=resolved),
            )

    def on_position_changed(self, event: Any) -> None:
        capture_native_callback(self.native_callback_trace, "on_position_changed", event)
        super().on_position_changed(event)

    def on_position_closed(self, event: Any) -> None:
        capture_native_callback(self.native_callback_trace, "on_position_closed", event)
        super().on_position_closed(event)
        position_id = str(getattr(event, "position_id", "") or "")
        if not position_id:
            return
        self._pending_flat[position_id] = ProtectionEvent(
            kind=ProtectionEventKind.POSITION_CHANGED,
            ts_ns=int(getattr(event, "ts_event", 0) or 0),
            position_id=position_id,
            quantity=Decimal(0),
        )
        self._flush_pending_flat(position_id=position_id)
        self._seen_open_positions.discard(position_id)
        self._reprotect.discard(position_id)
        decision_id = self._position_decision.pop(position_id, None)
        if decision_id is not None:
            self._entry_opportunities.pop(decision_id, None)

    def _append_snapshot(
        self, ts_ns: int, position_id: str, quantity: Decimal, direction: Direction | None
    ) -> None:
        self.protection_events.append(
            ProtectionEvent(
                kind=ProtectionEventKind.POSITION_CHANGED,
                ts_ns=ts_ns,
                position_id=position_id,
                direction=None if quantity <= 0 else direction,
                quantity=quantity,
            )
        )

    def _flush_pending_flat(
        self, *, position_id: str | None = None, unconditional: bool = False
    ) -> None:
        """Publish a flat snapshot once the bracket's sibling cancels are seen.

        ``POSITION_CHANGED(0)`` marks still-live protection as stale, and the
        engine's OCO sibling cancel is reported *after* the position snapshot.
        Publishing the flat snapshot immediately would therefore report a
        protection defect that is really a one-event reporting race. It is held
        until every protective order of that position is terminal — or until the
        next bar, where leftover protection is a genuine defect and is reported.
        """
        targets = [position_id] if position_id is not None else sorted(self._pending_flat)
        for pid in targets:
            if pid is None:
                continue
            event = self._pending_flat.get(pid)
            if event is None:
                continue
            if not unconditional and any(
                live_position == pid for live_position in self._live_protection.values()
            ):
                continue
            self._pending_flat.pop(pid, None)
            self.protection_events.append(event)


_PROTECTION_KIND: Final[dict[str, ProtectionEventKind]] = {
    "OrderSubmitted": ProtectionEventKind.ORDER_SUBMITTED,
    "OrderAccepted": ProtectionEventKind.ACK,
    "OrderFilled": ProtectionEventKind.FILL,
    "OrderCanceled": ProtectionEventKind.CANCELED,
    "OrderRejected": ProtectionEventKind.REJECTED,
    "OrderCancelRejected": ProtectionEventKind.REJECTED,
}


def position_direction(position: Any) -> ExposureDirection:
    """Position side as an exposure direction (Nautilus sides are unsigned)."""
    side = str(getattr(position, "side", "")).upper()
    return ExposureDirection.LONG if "LONG" in side else ExposureDirection.SHORT


def _order_price(order: Any) -> Decimal | None:
    if order is None:
        return None
    for attribute in ("trigger_price", "price"):
        value = getattr(order, attribute, None)
        if value is not None:
            return Decimal(str(value))
    return None


def _observation_of(candle: Candle, sequence: int) -> Observation:
    """The canonical BAR observation: available only once the bar has closed."""
    available = candle.available_ns if candle.available_ns is not None else candle.end_ns
    return Observation(
        instrument_id=candle.instrument_id,
        kind=ObservationKind.BAR,
        event_time=candle.start_ns,
        available_at=available,
        received_at=candle.received_ns,
        published_at=available,
        source="sim-arm-tape",
        sequence=sequence,
        quality=QualityState.OK,
        payload={
            "start_ns": candle.start_ns,
            "end_ns": candle.end_ns,
            "open": str(candle.open),
            "high": str(candle.high),
            "low": str(candle.low),
            "close": str(candle.close),
            "volume": str(candle.volume),
            "source_hash": candle.source_hash,
        },
    )
