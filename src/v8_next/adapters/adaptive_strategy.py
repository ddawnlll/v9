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
from v8_next.domain.horizon_data import aggregate_to_timeframe
from v8_next.domain.horizons import Timeframe, assemble_context
from v8_next.domain.market import (
    Candle,
    CausalFrame,
    build_candle_dataframe,
    extend_causal_frame,
    frame_at_incremental,
)
from v8_next.domain.observation import (
    Observation,
    ObservationKind,
    QualityState,
    build_view,
)
from v8_next.domain.positioning import PositioningReading
from v8_next.domain.protection import ProtectionState
from v8_next.economics.decisions import Opportunity, StanceKind, opportunity_at
from v8_next.economics.grammar import grammar_opportunity
from v8_next.economics.legality import PositionPresence
from v8_next.economics.management import ProtectiveKind
from v8_next.economics.policy import (
    CapacityState,
    DecisionRequest,
    PortfolioState,
    PositionState,
    TraderDecisionPolicy,
)
from v8_next.economics.position_lifecycle import (
    LIFECYCLE_VERSION,
    InitialStop,
    StopPolicyV1,
    TargetPolicyV1,
    assess_position,
    build_position_thesis,
    structural_objective,
)
from v8_next.economics.thesis import (
    ExpertEvidence as ThesisEvidence,
)
from v8_next.economics.thesis import (
    PositionSnapshot,
    PositionThesis,
    StopKind,
    TargetPlan,
    ThesisBelief,
)
from v8_next.economics.thesis import (
    StanceKind as ThesisStance,
)
from v8_next.experts.cache import FeatureCache
from v8_next.experts.evidence import (
    EvidenceBundle,
    produce_evidence,
    produce_evidence_multi,
)
from v8_next.experts.horizon_profiles import (
    HORIZON_PROFILES,
    PROFILE_TABLE_VERSION,
)
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
    #: Evaluate each expert on its declared primary horizon (M15/H1/H4). When
    #: ``False`` the arm keeps the legacy single-H1-frame evaluation, which is
    #: the C-arm ablation ("multi-horizon off") and must not be used for a V9
    #: claim.
    horizon_profiles_enabled: bool = True
    #: Identity of the optional intraday tape supplying real M15 bars. The runner
    #: verifies it like the H1 slice; a missing tape is ``MISSING`` context, not
    #: interpolated candles.
    intraday_tape: str | None = None
    #: Build a PositionThesis on entry and reassess it on every bar. Default on:
    #: this is the simulation-only arm's whole purpose. The V8 baseline and the
    #: C/D ablations turn it off to isolate the effect.
    adaptive_management_enabled: bool = True
    #: Native stop amendments (break-even candidate). Simulation-only; no live
    #: authority is implied by this flag.
    stop_amendments_enabled: bool = True
    #: Native target extensions. Simulation-only.
    target_amendments_enabled: bool = True
    #: Research-only ablation: when ``False`` the initial stop is the fixed
    #: ``bracket_stop_pct`` distance instead of the structural lookback stop.
    #: Default ``True`` preserves the frozen adaptive entry geometry exactly.
    structural_stop_enabled: bool = True
    #: Research-only ablation: when ``False`` the initial target is the fixed
    #: ``bracket_target_pct`` distance instead of the structural objective.
    #: Default ``True`` preserves the frozen adaptive entry geometry exactly.
    structural_target_enabled: bool = True
    #: Round-trip fee used by the break-even policy, in basis points.
    fee_bps: Decimal = Decimal("5")

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
        if not isinstance(self.horizon_profiles_enabled, bool):
            raise TypeError("horizon_profiles_enabled must be a bool")
        if self.intraday_tape is not None and not self.intraday_tape.strip():
            raise ValueError("intraday_tape cannot be blank when set")
        for name in (
            "adaptive_management_enabled",
            "stop_amendments_enabled",
            "target_amendments_enabled",
            "structural_stop_enabled",
            "structural_target_enabled",
        ):
            if not isinstance(getattr(self, name), bool):
                raise TypeError(f"{name} must be a bool")
        if self.fee_bps < 0:
            raise ValueError("fee_bps cannot be negative")

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
        intraday_candles: tuple[Candle, ...] = (),
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
        # Once replay diverges from the prevalidated source order, never infer
        # that later bars restore the prefix. Use the exact fallback instead.
        self._source_prefix_intact = True
        # ---- multi-horizon state (RFC 1.4.1 §4.4) ---------------------------
        #: Real bars per horizon. H1 is the execution tape; H4 is aggregated from
        #: complete H1 bars; M15 is only ever the verified intraday tape.
        self._horizon_candles: dict[Timeframe, list[Candle]] = {
            Timeframe.M15: [],
            Timeframe.H1: [],
            Timeframe.H4: [],
        }
        self._horizon_observations: dict[Timeframe, list[Observation]] = {
            timeframe: [] for timeframe in Timeframe
        }
        self._horizon_views: dict[Timeframe, Any] = {}
        self._horizon_view_obs_count: dict[Timeframe, int] = {
            timeframe: 0 for timeframe in Timeframe
        }
        self._horizon_frames: dict[Timeframe, CausalFrame] = {}
        #: One cache per horizon: alternating frames through one cache would
        #: invalidate its append-only chain every bar.
        self._horizon_caches: dict[Timeframe, FeatureCache] = {
            timeframe: FeatureCache() for timeframe in Timeframe
        }
        self._intraday_source: tuple[Candle, ...] = tuple(
            sorted(intraday_candles, key=lambda candle: candle.end_ns)
        )
        self._intraday_cursor = 0
        #: One stable numeric table per horizon. Passing the *same* DataFrame
        #: across bars is what lets FeatureCache chain its digest over the new
        #: rows instead of re-hashing the whole prefix each bar.
        self._horizon_numeric: dict[Timeframe, Any] = self._numeric_tables(
            source_candles
        )
        #: Runtime observation of the profiles: stance counts per primary horizon.
        self.horizon_evaluations: dict[str, dict[str, int]] = {
            timeframe.value: {"SUPPORT": 0, "CONTRADICT": 0, "ABSTAIN": 0}
            for timeframe in Timeframe
        }
        self.horizon_abstain_reasons: dict[str, int] = {}
        self.horizon_alignments: dict[str, dict[str, Any]] = {}
        # ---- adaptive position lifecycle (RFC 1.4.1 §9) --------------------
        self._stop_policy = StopPolicyV1()
        self._target_policy = TargetPolicyV1()
        self._theses: dict[str, PositionThesis] = {}
        self._entry_context: dict[str, dict[str, Any]] = {}
        self._consumed_entry_contexts: set[str] = set()
        self._bars_since_entry: dict[str, int] = {}
        self._target_amendments: dict[str, int] = {}
        self._amend_in_flight: dict[str, dict[str, Any]] = {}
        self.management_log: list[dict[str, Any]] = []
        self.lifecycle_events: list[dict[str, Any]] = []
        # ---- arm ledgers ----------------------------------------------------
        #: Extended decision ledger (the parent's ``decisions`` keeps its schema).
        self.arm_decisions: list[dict[str, Any]] = []
        #: Submission ledger: one marker per emitted order.
        self.order_markers: list[ArmOrderMarker] = []
        #: Protection-invariants input, in observed native order.
        self.protection_events: list[ProtectionEvent] = []
        self.book_transitions: list[dict[str, Any]] = []
        # ---- native order bookkeeping ---------------------------------------
        self._order_facts: dict[str, _OrderFacts] = {}
        #: Protective submission awaiting its native ACK. Published together with
        #: the ACK so the invariant checker never observes an intra-step
        #: submission→ACK gap as a real missing acknowledgement.
        self._pending_protective: dict[str, ProtectionEvent] = {}
        self._pending_protective_ack: dict[str, ProtectionEvent] = {}
        self._position_decision: dict[str, str] = {}
        self._reduce_decision: dict[str, str] = {}
        self._live_protection: dict[str, str] = {}
        self._pending_flat: dict[str, ProtectionEvent] = {}
        #: Opened-position snapshots are published on the next bar, after the
        #: bracket children's ACK callbacks for the opening batch, so the
        #: invariant checker does not report a transient missing ACK that the
        #: venue answers later in the same event batch.
        self._pending_snapshots: dict[str, tuple[int, Decimal, Direction | None]] = {}
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
        self._horizon_candles[Timeframe.H1].append(candle)
        self._horizon_observations[Timeframe.H1].append(self._observations[-1])
        cut = candle.end_ns

        self._flush_pending_flat(unconditional=True)
        self._flush_pending_snapshots()
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

        index = len(self.candles) - 1
        self._source_prefix_intact = (
            self._source_prefix_intact
            and index < len(self._ordered_source)
            and self._ordered_source[index] is candle
        )
        frame = None
        if (
            self._source_prefix_intact
            and self._frame is not None
            and len(self._frame.candles) == index
            and self._source_prefix_df is not None
        ):
            frame = extend_causal_frame(
                self._frame, int(bar.ts_init), candle, self._source_prefix_df
            )
        if frame is None:
            frame = frame_at_incremental(
                self._frame, str(self.instrument_id), int(bar.ts_init), tuple(self.candles)
            )
        self._frame = frame

        view = self._horizon_view(Timeframe.H1, cut)
        views, frames, context = self._multi_horizon_inputs(cut, view, frame)
        belief = advance(self._belief, view, context, now_ns=cut)
        self._belief = belief

        opportunity = self._detect(frame)
        record = self._remember_opportunity(opportunity, candle)
        if self.sim_config.horizon_profiles_enabled:
            bundle = produce_evidence_multi(
                views=views,
                frames=frames,
                caches=self._horizon_caches,
                context=context,
                profiles=HORIZON_PROFILES,
                belief=belief,
                opportunity=opportunity,
                readings=self.readings,
            )
        else:
            # Legacy single-H1 path (ablation C: multi-horizon off). Kept so the
            # multi-horizon effect can be measured against this exact arm.
            bundle = produce_evidence(
                view=view,
                context=context,
                frame=frame,
                cache=self._feature_cache,
                belief=belief,
                opportunity=opportunity,
                readings=self.readings,
            )
        self._record_horizon_evidence(bundle, context)
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
        management_outcome: str | None = None
        if position is not None:
            position_id = str(position.id)
            self._bars_since_entry[position_id] = (
                self._bars_since_entry.get(position_id, 0) + 1
            )
            if self.sim_config.adaptive_management_enabled:
                management_outcome = self._manage_position(
                    position, belief, bundle, candle
                )
        direction = (
            record.direction
            if record is not None
            else (position_direction(position) if position is not None else None)
        )
        opportunity_view = self._opportunity_view(cut, record, position)
        request = self._request(cut, belief, opportunity_view, bundle, position)
        decision = self._policy.evaluate(request)
        explanation = self._policy.explain(request)
        if management_outcome is not None:
            outcome = management_outcome
        else:
            outcome = self._apply(
                decision.action,
                decision.decision_id,
                candle,
                position,
                direction,
                belief=belief,
                opportunity_id=(record.opportunity_id if record is not None else None),
            )

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
                "horizon_profile_table": PROFILE_TABLE_VERSION,
                "horizon_alignments": dict(self.horizon_alignments),
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
    def _numeric_tables(self, source_candles: dict[int, Candle] | None) -> dict[Timeframe, Any]:
        """Precompute the full numeric table for horizons known up front."""
        tables: dict[Timeframe, Any] = {}
        base = tuple(
            sorted(
                (source_candles or {}).values(), key=lambda candle: candle.end_ns
            )
        )
        if base:
            # The parent strategy already built this exact ordered table.
            assert self._source_prefix_df is not None
            tables[Timeframe.H1] = self._source_prefix_df
            if source_candles and len(self.source_candles) == len(base):
                aggregate = aggregate_to_timeframe(
                    base, Timeframe.H4, source_timeframe=Timeframe.H1
                )
                if aggregate:
                    tables[Timeframe.H4] = build_candle_dataframe(aggregate)
        if self._intraday_source:
            tables[Timeframe.M15] = build_candle_dataframe(self._intraday_source)
        return tables

    def _horizon_frame(self, timeframe: Timeframe, cut: int) -> CausalFrame | None:
        """Extend the horizon's validated frame by its new candles (O(new))."""
        candles = tuple(self._horizon_candles[timeframe])
        if not candles:
            return None
        table = self._horizon_numeric.get(timeframe)
        previous = self._horizon_frames.get(timeframe)
        if previous is None or len(previous.candles) > len(candles):
            frame = CausalFrame.from_ordered_prefix(
                str(self.instrument_id), cut, candles, table
            )
        else:
            frame = previous
            appended = candles[len(previous.candles):]
            for candle in appended:
                candidate = extend_causal_frame(frame, cut, candle, table)
                if candidate is None:
                    frame = CausalFrame.from_ordered_prefix(
                        str(self.instrument_id), cut, candles, table
                    )
                    break
                frame = candidate
            else:
                if frame.decision_ns != cut:
                    frame = replace(frame, decision_ns=cut)
        self._horizon_frames[timeframe] = frame
        return frame

    def _horizon_view(self, timeframe: Timeframe, cut: int) -> Any:
        """Return the point-in-time view for one horizon, extended incrementally.

        ``ObservationView.extended`` proves the incremental path is equivalent to
        a full rebuild; when it cannot (a rejection could become usable, the feed
        is not strictly ordered) the caller falls back to ``build_view``. This is
        the difference between an O(n^2) and O(n) replay, not a weaker PIT gate.
        """
        observations = self._horizon_observations[timeframe]
        previous = self._horizon_views.get(timeframe)
        seen = self._horizon_view_obs_count.get(timeframe, 0)
        view = None
        if previous is not None and seen <= len(observations):
            view = previous.extended(observations[seen:], cut_time_ns=cut)
        if view is None:
            view = build_view(
                observations,
                cut_time_ns=cut,
                copy_inputs=False,
                assume_ordered=True,
            )
        self._horizon_views[timeframe] = view
        self._horizon_view_obs_count[timeframe] = len(observations)
        return view

    def _ingest_intraday(self, cut: int) -> None:
        """Append real M15 bars whose close *and* receipt are at or before ``cut``.

        The intraday tape is an independent verified source; nothing is derived
        from the H1 tape. A bar that has not closed at the decision cut is never
        ingested, which is why the cursor only advances over admissible rows.
        """
        source = self._intraday_source
        if not source:
            return
        candles = self._horizon_candles[Timeframe.M15]
        observations = self._horizon_observations[Timeframe.M15]
        cursor = self._intraday_cursor
        while cursor < len(source):
            candidate = source[cursor]
            if candidate.end_ns > cut:
                break
            cursor += 1
            if candidate.available_ns is None or candidate.available_ns > cut:
                continue
            if candles and candidate.end_ns <= candles[-1].end_ns:
                continue
            candles.append(candidate)
            observations.append(_observation_of(candidate, len(observations)))
        self._intraday_cursor = cursor

    def _extend_higher_horizons(self) -> None:
        """Publish an H4 bar only when four consecutive complete H1 bars exist."""
        base = self._horizon_candles[Timeframe.H1]
        if len(base) < 4:
            return
        aggregated = aggregate_to_timeframe(
            tuple(base[-4:]), Timeframe.H4, source_timeframe=Timeframe.H1
        )
        if not aggregated:
            return
        candidate = aggregated[-1]
        higher = self._horizon_candles[Timeframe.H4]
        if higher and candidate.end_ns <= higher[-1].end_ns:
            return
        higher.append(candidate)
        self._horizon_observations[Timeframe.H4].append(
            _observation_of(candidate, len(self._horizon_observations[Timeframe.H4]))
        )

    def _multi_horizon_inputs(
        self, cut: int, base_view: Any, base_frame: CausalFrame
    ) -> tuple[dict[Timeframe, Any], dict[Timeframe, CausalFrame], Any]:
        """Build the causal views/frames/context for the bar's decision cut.

        H1 is always present (the execution tape). H4 appears once a complete
        4-bar aggregate exists; M15 appears only when a verified intraday tape
        supplied bars. Any absent horizon stays absent from ``views`` so
        ``assemble_context`` records it as ``MISSING`` instead of inventing it.
        """
        self._extend_higher_horizons()
        self._ingest_intraday(cut)
        views: dict[Timeframe, Any] = {Timeframe.H1: base_view}
        self._horizon_views[Timeframe.H1] = base_view
        frames: dict[Timeframe, CausalFrame] = {Timeframe.H1: base_frame}
        decision_ns = int(base_frame.decision_ns)
        for timeframe in (Timeframe.M15, Timeframe.H4):
            observations = self._horizon_observations[timeframe]
            candles = self._horizon_candles[timeframe]
            if not observations or not candles:
                continue
            views[timeframe] = self._horizon_view(timeframe, cut)
            frame = self._horizon_frame(timeframe, decision_ns)
            if frame is None:
                continue
            frames[timeframe] = frame
        context = assemble_context(
            views, instrument_id=str(self.instrument_id), decision_time_ns=cut
        )
        self._horizon_views = views
        return views, frames, context

    def _record_horizon_evidence(self, bundle: EvidenceBundle, context: Any) -> None:
        """Count stances per declared primary horizon for runtime evidence."""
        for row in bundle.evidence:
            timeframe = HORIZON_PROFILES[row.expert_id].primary.value
            bucket = self.horizon_evaluations[timeframe]
            bucket[row.stance.value] = bucket.get(row.stance.value, 0) + 1
            if row.stance is StanceKind.ABSTAIN:
                key = f"{timeframe}:{row.reason}"
                self.horizon_abstain_reasons[key] = (
                    self.horizon_abstain_reasons.get(key, 0) + 1
                )
        self.horizon_alignments = {
            timeframe.value: {
                "as_of_ns": alignment.as_of_ns,
                "complete": alignment.complete,
                "quality": alignment.quality.value,
            }
            for timeframe, alignment in context.alignments.items()
        }

    def horizon_evidence(self) -> dict[str, Any]:
        """Runtime proof of which horizons the experts actually read."""
        from v8_next.experts.horizon_profiles import profiles_by_primary

        by_primary = profiles_by_primary()
        return {
            "profile_table_version": PROFILE_TABLE_VERSION,
            "profiles_enabled": self.sim_config.horizon_profiles_enabled,
            "experts_by_primary": {
                timeframe.value: list(ids) for timeframe, ids in by_primary.items()
            },
            "stances_by_primary": {
                timeframe: dict(counts)
                for timeframe, counts in self.horizon_evaluations.items()
            },
            "abstain_reasons": dict(sorted(self.horizon_abstain_reasons.items())),
            "last_alignments": dict(self.horizon_alignments),
            "bars_by_horizon": {
                timeframe.value: len(candles)
                for timeframe, candles in self._horizon_candles.items()
            },
            "intraday_source_bars": len(self._intraday_source),
        }

    # --------------------------------------------------- adaptive management
    def _live_stop_price(self, position_id: str) -> Decimal | None:
        for order in self._protective_orders(position_id):
            if "STOP" in str(order.order_type).upper():
                return _order_price(order)
        return None

    def _bind_bracket_to_position(self, position_id: str, decision_id: str) -> None:
        """Attach an entry's stop/target orders to the native position id.

        Bracket children are created with their entry decision, not a position
        id (the position does not exist yet). Without this binding the management
        plane reads an open position as ``UNPROTECTED`` and would try to add a
        second stop.
        """
        for client_order_id, facts in list(self._order_facts.items()):
            if (
                facts.decision_id == decision_id
                and facts.role in (OrderRole.STOP, OrderRole.TARGET)
                and not facts.position_id
            ):
                self._order_facts[client_order_id] = replace(
                    facts, position_id=position_id
                )

    def _protection_state(
        self,
        position_id: str,
        quantity: Decimal,
        protective: list[Any],
        stop_price: Decimal | None,
    ) -> ProtectionState:
        """Native loss coverage for one position, fail-closed on doubt.

        A live stop that has not been acknowledged is a ``GAP``, not protection.
        A stop smaller than the filled quantity is ``PARTIAL``. Nothing readable
        is ``UNKNOWN``.
        """
        if stop_price is None or not protective:
            return ProtectionState.UNPROTECTED
        stops = [order for order in protective if "STOP" in str(order.order_type).upper()]
        if not stops:
            return ProtectionState.UNPROTECTED
        if any(str(order.client_order_id) not in self._live_protection for order in stops):
            return ProtectionState.GAP
        covered = sum((order.quantity.as_decimal() for order in stops), Decimal(0))
        if covered >= quantity:
            return ProtectionState.PROTECTED
        return ProtectionState.PARTIAL

    def _create_thesis(
        self, position_id: str, position: Position, ts_ns: int
    ) -> PositionThesis | None:
        """Freeze the entry rationale once the native position exists."""
        decision_id = self._position_decision.get(position_id)
        context = self._entry_context.get(decision_id) if decision_id else None
        if context is None or decision_id is None:
            for candidate in reversed(list(self._entry_context)):
                if candidate not in self._consumed_entry_contexts:
                    decision_id = candidate
                    context = self._entry_context[candidate]
                    break
        if context is None or decision_id is None:
            return None
        self._consumed_entry_contexts.add(decision_id)
        side = (
            "LONG"
            if position_direction(position) is ExposureDirection.LONG
            else "SHORT"
        )
        entry_price = _as_decimal(position.avg_px_open)
        live_stop = self._live_stop_price(position_id)
        stop_price = live_stop if live_stop is not None else context["stop_price"]
        initial_stop = InitialStop(
            price=stop_price,
            kind=StopKind.NATIVE_PROTECTIVE,
            distance=abs(entry_price - stop_price),
            basis="LIVE_NATIVE_STOP" if live_stop is not None else "ENTRY_CONTEXT",
        )
        belief = ThesisBelief(
            belief_id=context["belief_id"],
            version_hash=context["belief_version_hash"],
            regime=context["belief_regime"],
            as_of_ns=int(context["belief_as_of_ns"]),
        )
        target_plan = TargetPlan(
            target_price=context["target_price"],
            expected_bars=self._target_policy.expected_bars,
            max_bars=self._target_policy.max_bars,
        )
        thesis = build_position_thesis(
            entry_decision_id=decision_id,
            opportunity_id=context["opportunity_id"],
            belief=belief,
            native_position_id=position_id,
            instrument_id=str(self.instrument_id),
            side=side,
            entry_price=entry_price,
            initial_stop=initial_stop,
            quantity=position.quantity.as_decimal(),
            target=target_plan,
            created_ns=ts_ns or int(context["decision_time_ns"]),
        )
        self._theses[position_id] = thesis
        self._bars_since_entry[position_id] = 0
        self._target_amendments[position_id] = 0
        self.lifecycle_events.append(
            {
                "kind": "THESIS_CREATED",
                "event_ns": thesis.created_ns,
                "position_id": position_id,
                "thesis_id": thesis.thesis_id,
                "entry_decision_id": decision_id,
                "side": side,
                "entry_price": str(entry_price),
                "initial_stop_price": str(stop_price),
                "original_risk_usdt": str(thesis.original_risk_usdt),
                "target_price": str(target_plan.target_price),
                "lifecycle_version": LIFECYCLE_VERSION,
            }
        )
        self._audit(
            "THESIS_CREATED",
            thesis.created_ns,
            evidence=ARM_EVIDENCE_CLASS,
            decision_id=decision_id,
            position_id=position_id,
            thesis_id=thesis.thesis_id,
            sim_arm=True,
            advisory_only=False,
        )
        return thesis
    def _manage_position(
        self,
        position: Position,
        belief: MarketBeliefV1,
        bundle: EvidenceBundle,
        candle: Candle,
    ) -> str | None:
        """Reassess the open position and emit at most one native management action."""
        position_id = str(position.id)
        thesis = self._theses.get(position_id)
        if thesis is None:
            # The native position callback may not have fired yet; the entry
            # context is still there, so build the thesis now instead of
            # leaving the position unmanaged for a bar.
            thesis = self._create_thesis(
                position_id, position, int(candle.end_ns)
            )
        if thesis is None:
            return None
        if belief.status is BeliefStatus.ABSTAIN or not belief.is_actionable_evidence():
            # No new risk and no blind exit: keep the verified native stop and
            # defer the reassessment until the belief is actionable again.
            return "MANAGEMENT_DEFERRED_BELIEF_NOT_ACTIONABLE"
        if self._protection_in_flight(position_id):
            return "MANAGEMENT_DEFERRED_REDUCTION_IN_FLIGHT"
        instrument = self.cache.instrument(self.instrument_id)
        if instrument is None:
            return "MANAGEMENT_DEFERRED_NO_INSTRUMENT"
        quantity = position.quantity.as_decimal()
        if quantity <= 0:
            return None
        protective = self._protective_orders(position_id)
        stop_price = self._live_stop_price(position_id)
        protection_state = self._protection_state(
            position_id, quantity, protective, stop_price
        )
        thesis_belief = ThesisBelief(
            belief_id=belief.version_hash,
            version_hash=belief.version_hash,
            regime=belief.regime_label.value,
            as_of_ns=belief.max_input_available_at,
            stale=False,
            abstain=False,
        )
        evidence = tuple(
            ThesisEvidence(
                expert_id=row.expert_id,
                stance=ThesisStance(row.stance.value),
                weight=row.confidence,
            )
            for row in bundle.evidence
        )
        snapshot = PositionSnapshot(
            native_position_id=position_id,
            quantity=quantity,
            average_price=_as_decimal(position.avg_px_open),
            mark_price=candle.close,
            protective_order_ids=tuple(
                str(order.client_order_id) for order in protective
            ),
            elapsed_bars=self._bars_since_entry.get(position_id, 0),
            available_at_ns=int(candle.available_ns or candle.end_ns),
        )
        objective = structural_objective(
            thesis.side,
            self._horizon_candles[Timeframe.H4]
            or self._horizon_candles[Timeframe.H1],
        )
        tick_size = _tick_size(instrument)
        assessment = assess_position(
            thesis=thesis,
            snapshot=snapshot,
            belief=thesis_belief,
            evidence=evidence,
            protection_state=protection_state,
            protective_stop_price=stop_price,
            fee_bps=self.sim_config.fee_bps,
            lot_step=instrument.size_increment.as_decimal(),
            tick_size=tick_size,
            target_policy=self._target_policy,
            objective=objective,
            target_amendments=self._target_amendments.get(position_id, 0),
            now_ns=int(candle.end_ns),
            venue_status=self._venue_status(),
            position_presence=PositionPresence.OPEN,
            stale=False,
        )
        receipt = assessment.to_receipt()
        receipt.update(
            {
                "event_ns": int(candle.end_ns),
                "mark_price": str(candle.close),
                "quantity": str(quantity),
                "protective_stop_price": (
                    None if stop_price is None else str(stop_price)
                ),
            }
        )
        self.management_log.append(receipt)

        action = assessment.action
        if action is DecisionAction.EXIT and assessment.management.admission_ok:
            outcome = self._exit(assessment.management.management_id, position)
            self.lifecycle_events.append(
                {
                    "kind": "THESIS_EXIT",
                    "event_ns": int(candle.end_ns),
                    "position_id": position_id,
                    "management_id": assessment.management.management_id,
                    "reasons": [
                        code.value
                        for code in assessment.management.advisory.reason_codes
                    ],
                }
            )
            return f"MANAGED_{outcome}"
        if action is DecisionAction.REDUCE and assessment.management.admission_ok:
            reduce_quantity = assessment.management.advisory.reduce_quantity
            if reduce_quantity is not None:
                outcome = self._reduce_quantity(
                    assessment.management.management_id, position, reduce_quantity
                )
                return f"MANAGED_{outcome}"
        if not assessment.management.protected:
            if self.sim_config.stop_amendments_enabled:
                for proposal in assessment.management.proposals:
                    if proposal.kind is ProtectiveKind.PROTECTIVE_ADD:
                        return self._amend_stop(
                            position,
                            proposal.price,
                            assessment.management.management_id,
                        )
            return "MANAGEMENT_PROTECTION_GAP_NO_PROPOSAL"
        if self.sim_config.stop_amendments_enabled:
            for proposal in assessment.management.proposals:
                if proposal.kind is ProtectiveKind.PROTECTIVE_BREAK_EVEN:
                    return self._amend_stop(
                        position,
                        proposal.price,
                        assessment.management.management_id,
                    )
        if (
            self.sim_config.target_amendments_enabled
            and assessment.target_amendment is not None
        ):
            return self._amend_target(
                position,
                assessment.target_amendment,
                assessment.management.management_id,
            )
        return "MANAGEMENT_HOLD_NO_ORDER"

    def _amend_stop(
        self, position: Position, price: Decimal, decision_id: str
    ) -> str:
        """Submit a tighter stop, then cancel the old one only after the ACK.

        A stop can never be widened: that would silently increase open risk.
        The old stop stays live until the replacement is acknowledged, so a
        rejection or timeout never leaves the position uncovered.
        """
        position_id = str(position.id)
        if position_id in self._amend_in_flight:
            return "AMEND_ALREADY_IN_FLIGHT"
        instrument = self.cache.instrument(self.instrument_id)
        if instrument is None:
            return "AMEND_DEFERRED_NO_INSTRUMENT"
        long_side = position_direction(position) is ExposureDirection.LONG
        live = [
            order
            for order in self._protective_orders(position_id)
            if "STOP" in str(order.order_type).upper()
        ]
        current = _order_price(live[0]) if live else None
        if live:
            if current is None:
                return "AMEND_DEFERRED_STOP_PRICE_UNREADABLE"
            if long_side and price <= current:
                return "AMEND_REFUSED_WIDEN"
            if not long_side and price >= current:
                return "AMEND_REFUSED_WIDEN"
            live_client_order_id = str(live[0].client_order_id)
            if live_client_order_id not in self._live_protection:
                # The venue has not acknowledged the live stop yet; amending it
                # would race the ACK and could lose protection.
                return "AMEND_DEFERRED_STOP_NOT_ACKED"
            self._amend_in_flight[position_id] = {
                "kind": "STOP",
                "new": live_client_order_id,
                "old": [],
                "decision_id": decision_id,
                "price": str(price),
            }
            try:
                self.modify_order(
                    live[0].client_order_id,
                    trigger_price=Price(float(price), instrument.price_precision),
                )
            except Exception as exc:  # pragma: no cover - adapter refusal
                self._amend_in_flight.pop(position_id, None)
                self.lifecycle_events.append(
                    {
                        "kind": "STOP_AMEND_REFUSED_BY_ADAPTER",
                        "event_ns": None,
                        "position_id": position_id,
                        "client_order_id": live_client_order_id,
                        "reason": f"{type(exc).__name__}: {exc}",
                    }
                )
                return "STOP_AMEND_REFUSED_BY_ADAPTER"
            self.lifecycle_events.append(
                {
                    "kind": "STOP_AMEND_REQUESTED",
                    "event_ns": None,
                    "position_id": position_id,
                    "client_order_id": live_client_order_id,
                    "from": None if current is None else str(current),
                    "to": str(price),
                    "decision_id": decision_id,
                    "mode": "NATIVE_MODIFY",
                }
            )
            self._audit(
                "PROTECTIVE_STOP_AMEND_REQUESTED",
                0,
                evidence=ARM_EVIDENCE_CLASS,
                decision_id=decision_id,
                position_id=position_id,
                trigger_price=str(price),
                replaced=[live_client_order_id],
                sim_arm=True,
                advisory_only=False,
            )
            return f"STOP_AMEND_REQUESTED_{price}"
        if any(
            "STOP" in str(order.order_type).upper()
            for order in self.cache.orders_open(instrument_id=self.instrument_id)
        ):
            # A stop submission is in flight but unacknowledged: submitting a
            # second one would double the coverage quantity.
            return "AMEND_DEFERRED_STOP_PENDING"
        old: list[str] = []
        tag = position_id[-6:]
        client_order_id = ClientOrderId(f"A-{tag}-{len(self.lifecycle_events):03d}-S")
        order = self.order_factory.stop_market(
            instrument_id=self.instrument_id,
            order_side=OrderSide.SELL if long_side else OrderSide.BUY,
            quantity=position.quantity,
            trigger_price=Price(float(price), instrument.price_precision),
            reduce_only=True,
            tags=self._tags(decision_id, "STOP"),
            client_order_id=client_order_id,
        )
        self._order_facts[str(client_order_id)] = _OrderFacts(
            role=OrderRole.STOP,
            arm_role="STOP",
            decision_id=decision_id,
            direction=Direction.LONG if long_side else Direction.SHORT,
            position_id=position_id,
        )
        self._mark_order(str(client_order_id), decision_id, OrderRole.STOP)
        self._amend_in_flight[position_id] = {
            "kind": "STOP",
            "new": str(client_order_id),
            "old": old,
            "decision_id": decision_id,
            "price": str(price),
        }
        self.submit_order(order, position_id=position.id)
        self._any_order_seen = True
        self.lifecycle_events.append(
            {
                "kind": "STOP_AMEND_REQUESTED",
                "event_ns": None,
                "position_id": position_id,
                "client_order_id": str(client_order_id),
                "from": None if current is None else str(current),
                "to": str(price),
                "decision_id": decision_id,
            }
        )
        self._audit(
            "PROTECTIVE_STOP_AMEND_REQUESTED",
            0,
            evidence=ARM_EVIDENCE_CLASS,
            decision_id=decision_id,
            position_id=position_id,
            trigger_price=str(price),
            replaced=old,
            sim_arm=True,
            advisory_only=False,
        )
        return f"STOP_AMEND_REQUESTED_{price}"

    def _amend_target(
        self, position: Position, price: Decimal, decision_id: str
    ) -> str:
        """Submit a further target, then cancel the old one after the ACK."""
        position_id = str(position.id)
        if position_id in self._amend_in_flight:
            return "AMEND_ALREADY_IN_FLIGHT"
        instrument = self.cache.instrument(self.instrument_id)
        if instrument is None:
            return "AMEND_DEFERRED_NO_INSTRUMENT"
        long_side = position_direction(position) is ExposureDirection.LONG
        live = [
            order
            for order in self._protective_orders(position_id)
            if "LIMIT" in str(order.order_type).upper()
        ]
        current = _order_price(live[0]) if live else None
        if live:
            if current is None:
                return "AMEND_DEFERRED_TARGET_PRICE_UNREADABLE"
            if long_side and price <= current:
                return "TARGET_AMEND_REFUSED_NOT_FURTHER"
            if not long_side and price >= current:
                return "TARGET_AMEND_REFUSED_NOT_FURTHER"
            live_client_order_id = str(live[0].client_order_id)
            if live_client_order_id not in self._live_protection:
                return "AMEND_DEFERRED_TARGET_NOT_ACKED"
            self._amend_in_flight[position_id] = {
                "kind": "TARGET",
                "new": live_client_order_id,
                "old": [],
                "decision_id": decision_id,
                "price": str(price),
            }
            self.modify_order(
                live[0].client_order_id,
                price=Price(float(price), instrument.price_precision),
            )
            self.lifecycle_events.append(
                {
                    "kind": "TARGET_AMEND_REQUESTED",
                    "event_ns": None,
                    "position_id": position_id,
                    "client_order_id": live_client_order_id,
                    "from": None if current is None else str(current),
                    "to": str(price),
                    "decision_id": decision_id,
                    "mode": "NATIVE_MODIFY",
                }
            )
            self._audit(
                "TARGET_AMEND_REQUESTED",
                0,
                evidence=ARM_EVIDENCE_CLASS,
                decision_id=decision_id,
                position_id=position_id,
                price=str(price),
                replaced=[live_client_order_id],
                sim_arm=True,
                advisory_only=False,
            )
            return f"TARGET_AMEND_REQUESTED_{price}"
        old: list[str] = []
        tag = position_id[-6:]
        client_order_id = ClientOrderId(f"A-{tag}-{len(self.lifecycle_events):03d}-T")
        order = self.order_factory.limit(
            instrument_id=self.instrument_id,
            order_side=OrderSide.SELL if long_side else OrderSide.BUY,
            quantity=position.quantity,
            price=Price(float(price), instrument.price_precision),
            reduce_only=True,
            tags=self._tags(decision_id, "TARGET"),
            client_order_id=client_order_id,
        )
        self._order_facts[str(client_order_id)] = _OrderFacts(
            role=OrderRole.TARGET,
            arm_role="TARGET",
            decision_id=decision_id,
            direction=Direction.LONG if long_side else Direction.SHORT,
            position_id=position_id,
        )
        self._mark_order(str(client_order_id), decision_id, OrderRole.TARGET)
        self._amend_in_flight[position_id] = {
            "kind": "TARGET",
            "new": str(client_order_id),
            "old": old,
            "decision_id": decision_id,
            "price": str(price),
        }
        self.submit_order(order, position_id=position.id)
        self._any_order_seen = True
        self.lifecycle_events.append(
            {
                "kind": "TARGET_AMEND_REQUESTED",
                "event_ns": None,
                "position_id": position_id,
                "client_order_id": str(client_order_id),
                "from": None if current is None else str(current),
                "to": str(price),
                "decision_id": decision_id,
            }
        )
        self._audit(
            "TARGET_AMEND_REQUESTED",
            0,
            evidence=ARM_EVIDENCE_CLASS,
            decision_id=decision_id,
            position_id=position_id,
            price=str(price),
            replaced=old,
            sim_arm=True,
            advisory_only=False,
        )
        return f"TARGET_AMEND_REQUESTED_{price}"

    def _resolve_amendment(self, client_order_id: str, event: Any) -> None:
        """Advance the two-phase amendment when its replacement resolves."""
        event_name = type(event).__name__
        for position_id, state in list(self._amend_in_flight.items()):
            if state["new"] != client_order_id:
                continue
            if event_name in {"OrderAccepted", "OrderUpdated"}:
                for old_id in state["old"]:
                    if old_id and old_id != client_order_id:
                        self.cancel_order(ClientOrderId(old_id))
                if state["kind"] == "TARGET":
                    self._target_amendments[position_id] = (
                        self._target_amendments.get(position_id, 0) + 1
                    )
                self.lifecycle_events.append(
                    {
                        "kind": f"{state['kind']}_AMEND_ACKED",
                        "event_ns": int(getattr(event, "ts_event", 0) or 0),
                        "position_id": position_id,
                        "client_order_id": client_order_id,
                        "price": state["price"],
                        "replaced": state["old"],
                    }
                )
                del self._amend_in_flight[position_id]
            elif event_name in {"OrderRejected", "OrderCancelRejected"}:
                self.lifecycle_events.append(
                    {
                        "kind": f"{state['kind']}_AMEND_REJECTED",
                        "event_ns": int(getattr(event, "ts_event", 0) or 0),
                        "position_id": position_id,
                        "client_order_id": client_order_id,
                        "reason": str(getattr(event, "reason", "") or ""),
                    }
                )
                del self._amend_in_flight[position_id]
            elif event_name == "OrderFilled":
                del self._amend_in_flight[position_id]

    def _reduce_quantity(
        self, decision_id: str, position: Position, quantity: Decimal
    ) -> str:
        instrument = self.cache.instrument(self.instrument_id)
        if instrument is None:
            return "SKIPPED_NO_INSTRUMENT"
        step = instrument.size_increment.as_decimal()
        held = position.quantity.as_decimal()
        rounded = (quantity / step) * step if step > 0 else quantity
        if rounded <= 0 or rounded >= held:
            return "REDUCE_QUANTITY_INVALID"
        if rounded < instrument.min_quantity.as_decimal():
            return "REDUCE_BELOW_MIN_QUANTITY"
        self._cancel_protection()
        long_side = position_direction(position) is ExposureDirection.LONG
        short = decision_id[:12]
        order = self.order_factory.market(
            instrument_id=self.instrument_id,
            order_side=OrderSide.SELL if long_side else OrderSide.BUY,
            quantity=Quantity.from_str(format(rounded, f".{instrument.size_precision}f")),
            reduce_only=True,
            tags=self._tags(decision_id, "REDUCE"),
            client_order_id=ClientOrderId(f"S-{short}-R"),
        )
        client_order_id = str(order.client_order_id)
        self._order_facts[client_order_id] = _OrderFacts(
            role=OrderRole.OTHER,
            arm_role="REDUCE",
            decision_id=decision_id,
            direction=Direction.LONG if long_side else Direction.SHORT,
            position_id=str(position.id),
        )
        self._mark_order(client_order_id, decision_id, "REDUCE")
        self.submit_order(order, position_id=position.id)
        self._any_order_seen = True
        self._reprotect.add(str(position.id))
        self._reduce_decision[str(position.id)] = decision_id
        return f"SUBMITTED_REDUCE_ONLY_{rounded}"

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

        While flat, only the episode *this bar detected* is offered: an episode
        that stayed armed from an earlier bar is not a fresh entry signal, so it
        must not become one (the V8 gate is per detection bar too). With an open
        position the book's latest live episode is offered instead, because the
        management actions need the identity the position was opened under.
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
        live = [
            episode
            for episode in episodes.values()
            if episode.opportunity.instrument_id == str(self.instrument_id)
            and lifecycle_of(episode.status)
            in (
                OpportunityLifecycle.WATCHING,
                OpportunityLifecycle.ARMED,
                OpportunityLifecycle.ADMITTED,
            )
        ]
        if not live:
            return None
        latest = max(live, key=lambda episode: episode.opportunity.as_of_time_ns)
        return _OpportunityFacts(
            opportunity_id=latest.opportunity.opportunity_id,
            lifecycle=lifecycle_of(latest.status),
            as_of_ns=latest.opportunity.as_of_time_ns,
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
        *,
        belief: MarketBeliefV1 | None = None,
        opportunity_id: str | None = None,
    ) -> str:
        """Translate one policy action into the native order flow."""
        if action is DecisionAction.ENTER:
            return self._enter(
                decision_id,
                candle,
                position,
                direction,
                belief=belief,
                opportunity_id=opportunity_id,
            )
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
        *,
        belief: MarketBeliefV1 | None = None,
        opportunity_id: str | None = None,
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
        side_text = "LONG" if side is OrderSide.BUY else "SHORT"
        if self.sim_config.adaptive_management_enabled:
            stop_dist = entry_px * self.sim_config.bracket_stop_pct
            target_dist = entry_px * self.sim_config.bracket_target_pct
            if self.sim_config.structural_stop_enabled:
                initial_stop = self._stop_policy.initial_stop(
                    side=side_text,
                    entry_price=entry_px,
                    bars=tuple(self._horizon_candles[Timeframe.H1][:-1]),
                )
                stop_px = initial_stop.price
            else:
                stop_px = (
                    entry_px - stop_dist
                    if side is OrderSide.BUY
                    else entry_px + stop_dist
                )
                initial_stop = InitialStop(
                    price=stop_px,
                    kind=StopKind.NATIVE_PROTECTIVE,
                    distance=stop_dist,
                    basis="FIXED_BRACKET_PCT_ABLATION",
                )
            if self.sim_config.structural_target_enabled:
                objective = structural_objective(
                    side_text,
                    self._horizon_candles[Timeframe.H4]
                    or self._horizon_candles[Timeframe.H1],
                )
                target_plan = self._target_policy.initial_target(
                    side=side_text,
                    entry_price=entry_px,
                    stop_price=initial_stop.price,
                    objective=objective,
                )
                target_px = target_plan.target_price
            else:
                target_px = (
                    entry_px + target_dist
                    if side is OrderSide.BUY
                    else entry_px - target_dist
                )
                target_plan = TargetPlan(
                    target_price=target_px,
                    expected_bars=self._target_policy.expected_bars,
                    max_bars=self._target_policy.max_bars,
                )
        else:
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
        if self.sim_config.adaptive_management_enabled and belief is not None:
            if opportunity_id is None:
                raise ValueError(
                    "an adaptive entry must name the opportunity its thesis is bound to"
                )
            self._entry_context[decision_id] = {
                "opportunity_id": opportunity_id,
                "belief_id": belief.version_hash,
                "belief_version_hash": belief.version_hash,
                "belief_regime": belief.regime_label.value,
                "belief_as_of_ns": belief.max_input_available_at,
                "entry_price": entry_px,
                "stop_price": stop_px,
                "target_price": target_px,
                "quantity": quantity.as_decimal(),
                "side": side_text,
                "decision_time_ns": int(candle.end_ns),
            }
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
        instrument = self.cache.instrument(self.instrument_id)
        if instrument is None:
            return "SKIPPED_NO_INSTRUMENT"
        held = position.quantity.as_decimal()
        step = instrument.size_increment.as_decimal()
        half = ((held / 2) // step) * step if step > 0 else held / 2
        if half <= 0 or half < instrument.min_quantity.as_decimal():
            return "REDUCE_BELOW_MIN_QUANTITY"
        self._cancel_protection()
        quantity = Quantity.from_str(format(half, f".{instrument.size_precision}f"))
        long_side = position_direction(position) is ExposureDirection.LONG
        side = OrderSide.SELL if long_side else OrderSide.BUY
        short = decision_id[:12]
        order = self.order_factory.market(
            instrument_id=self.instrument_id,
            order_side=side,
            quantity=quantity,
            reduce_only=True,
            tags=self._tags(decision_id, "REDUCE"),
            client_order_id=ClientOrderId(f"S-{short}-R"),
        )
        client_order_id = str(order.client_order_id)
        self._order_facts[client_order_id] = _OrderFacts(
            role=OrderRole.OTHER,
            arm_role="REDUCE",
            decision_id=decision_id,
            direction=Direction.LONG if long_side else Direction.SHORT,
            position_id=str(position.id),
        )
        self._mark_order(client_order_id, decision_id, "REDUCE")
        self.submit_order(order, position_id=position.id)
        self._any_order_seen = True
        self._reprotect.add(str(position.id))
        self._reduce_decision[str(position.id)] = decision_id
        return f"SUBMITTED_REDUCE_ONLY_{quantity}"

    def _exit(self, decision_id: str, position: Position | None) -> str:
        if position is None:
            return "EXIT_WITHOUT_POSITION"
        self._cancel_protection()
        long_side = position_direction(position) is ExposureDirection.LONG
        side = OrderSide.SELL if long_side else OrderSide.BUY
        short = decision_id[:12]
        order = self.order_factory.market(
            instrument_id=self.instrument_id,
            order_side=side,
            quantity=position.quantity,
            reduce_only=True,
            tags=self._tags(decision_id, "EXIT"),
            client_order_id=ClientOrderId(f"S-{short}-X"),
        )
        client_order_id = str(order.client_order_id)
        self._order_facts[client_order_id] = _OrderFacts(
            role=OrderRole.OTHER,
            arm_role="EXIT",
            decision_id=decision_id,
            direction=Direction.LONG if long_side else Direction.SHORT,
            position_id=str(position.id),
        )
        self._mark_order(client_order_id, decision_id, "EXIT")
        self.submit_order(order, position_id=position.id)
        self._any_order_seen = True
        self._reprotect.discard(str(position.id))
        return f"SUBMITTED_EXIT_REDUCE_ONLY_{position.quantity}"

    def _cancel_protection(self) -> int:
        """Cancel live protective children before changing the exposure.

        A partial or full exit that left a full-size stop resting would leave
        protection larger than the position (``REDUCE_ONLY_OVER_QUANTITY``) or
        protection standing after the position is flat
        (``STALE_PROTECTION_ORDER``); the arm therefore cancels first and
        re-protects only what remains.
        """
        cancelled = 0
        for order in list(self.cache.orders_open(instrument_id=self.instrument_id)):
            order_type = str(order.order_type).upper()
            is_stop = "STOP" in order_type
            is_target = "LIMIT" in order_type and bool(order.is_reduce_only)
            if not (is_stop or is_target):
                continue
            self.cancel_order(order.client_order_id)
            cancelled += 1
        return cancelled

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
        self._resolve_amendment(client_order_id, event)

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
                # A buffered ACK can still be waiting for a position id when the
                # order resolves (the entry fill usually arrives before the
                # child ACK callback). It is a real venue acknowledgement whose
                # ts is earlier than this resolution, so publish it first and
                # never let the resolution branch orphan it: an orphaned ACK is
                # later dropped by ``_flush_pending_pairs`` and the ledger then
                # reports a MISSING_ACK for an order the venue did acknowledge.
                acknowledgement = self._pending_protective_ack.pop(client_id, None)
                if submission is not None:
                    position_id = (
                        event.position_id
                        or submission.position_id
                        or (acknowledgement.position_id if acknowledgement else "")
                    )
                    if position_id:
                        submission = replace(submission, position_id=position_id)
                        event = replace(event, position_id=position_id)
                        if acknowledgement is not None:
                            acknowledgement = replace(
                                acknowledgement, position_id=position_id
                            )
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
            self._bind_bracket_to_position(event.position_id, facts.decision_id)
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
        super().on_position_opened(event)
        position_id = str(getattr(event, "position_id", "") or "")
        if not position_id:
            return
        self._seen_open_positions.add(position_id)
        position = self.cache.position(event.position_id)
        quantity = position.quantity.as_decimal() if position is not None else Decimal(0)
        direction = (
            Direction.LONG
            if position_direction(position) is ExposureDirection.LONG
            else Direction.SHORT
        )
        self._flush_pending_children(position_id)
        # Publish the open snapshot in event order. Deferring it to the next bar
        # let a newer flat snapshot (same netting position id) precede it, which
        # reordered the protection ledger and latched a permanent conflict.
        self._flush_pending_flat(position_id=position_id, unconditional=True)
        self._append_snapshot(
            int(getattr(event, "ts_event", 0) or 0), position_id, quantity, direction
        )
        if self.sim_config.adaptive_management_enabled and position is not None:
            decision_id = self._position_decision.get(position_id)
            if decision_id:
                self._bind_bracket_to_position(position_id, decision_id)
            self._create_thesis(
                position_id, position, int(getattr(event, "ts_event", 0) or 0)
            )

    def _flush_pending_children(self, position_id: str) -> None:
        """Append buffered protective submission/ACK events for ``position_id``."""
        self._flush_pending_pairs(position_id)

    def _flush_pending_pairs(self, position_id: str | None = None) -> None:
        """Publish any submission/ACK pair whose position id is now known."""
        for client_id in sorted(self._pending_protective_ack):
            acknowledgement = self._pending_protective_ack.pop(client_id)
            submission = self._pending_protective.pop(client_id, None)
            resolved = acknowledgement.position_id or (
                submission.position_id if submission is not None else ""
            ) or position_id
            if not resolved and len(self._seen_open_positions) == 1:
                # Netting mode reuses one position id; while exactly one position
                # is open, a protective ACK with no explicit id belongs to it.
                resolved = next(iter(self._seen_open_positions))
            if not resolved:
                # Keep the pair until a position callback supplies the id.
                self._pending_protective_ack[client_id] = acknowledgement
                if submission is not None:
                    self._pending_protective[client_id] = submission
                continue
            if submission is None:
                # The submission was already published with its resolution; the
                # buffered ACK still has to reach the ledger. Dropping it would
                # fabricate a missing acknowledgement.
                self.protection_events.append(
                    replace(acknowledgement, position_id=resolved)
                )
                self._live_protection.setdefault(client_id, resolved)
                continue
            self._publish_protective(
                replace(submission, position_id=resolved),
                replace(acknowledgement, position_id=resolved),
            )

    def on_position_closed(self, event: Any) -> None:
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

    def _flush_pending_snapshots(self) -> None:
        for position_id in sorted(self._pending_snapshots):
            ts_ns, quantity, direction = self._pending_snapshots.pop(position_id)
            self._append_snapshot(ts_ns, position_id, quantity, direction)

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
    "OrderUpdated": ProtectionEventKind.AMENDED,
    "OrderFilled": ProtectionEventKind.FILL,
    "OrderCanceled": ProtectionEventKind.CANCELED,
    "OrderRejected": ProtectionEventKind.REJECTED,
    "OrderCancelRejected": ProtectionEventKind.REJECTED,
    "OrderPendingCancel": ProtectionEventKind.CANCEL_REQUEST,
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


def _tick_size(instrument: Any) -> Decimal:
    value = getattr(instrument, "price_increment", None)
    if value is None:
        return Decimal("0.01")
    return Decimal(str(value))


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
