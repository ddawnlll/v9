"""Native NautilusTrader Strategy adapter for the canonical 28-expert ensemble.

Epistemic & Execution Demarcation (matching V8_NEXT_IMPLEMENTATION_SCOPE.md):
- Python owns opportunity identity, the 28 expert witness stances, and consensus gating.
- NautilusTrader owns generic bar dispatch, order lifecycle, order lists, positions,
  margin accounting, and fee/funding settlement.
- No custom simulator or manual scheduling loop.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from typing import Any

from nautilus_trader.backtest import BacktestEngine
from nautilus_trader.common import LogLevel
from nautilus_trader.config import BacktestEngineConfig, LoggerConfig
from nautilus_trader.model import (
    AccountType,
    Bar,
    BarType,
    ClientOrderId,
    CryptoPerpetual,
    Currency,
    InstrumentId,
    Money,
    OmsType,
    OrderSide,
    OrderType,
    Price,
    Quantity,
    Symbol,
    TriggerType,
    Venue,
)
from nautilus_trader.trading import Strategy

from v8_next.adapters.engine_state import economic_state
from v8_next.adapters.execution_models import (
    ExecutionProfile,
    resolve_profile,
    venue_kwargs,
)
from v8_next.adapters.execution_telemetry import (
    execution_telemetry,
    fill_signature,
    native_fill_records,
)
from v8_next.domain.market import (
    Candle,
    CausalFrame,
    build_candle_dataframe,
    extend_causal_frame,
    frame_at,
)
from v8_next.domain.positioning import PositioningReading
from v8_next.economics.decisions import (
    Opportunity,
    StanceKind,
    opportunity_at,
    opportunity_record,
    stance_record,
)
from v8_next.economics.grammar import grammar_opportunity
from v8_next.experts.engine import FeatureEngine
from v8_next.experts.registry import CANONICAL_28_EXPERTS, observe_all_28
from v8_next.opportunities.book import OpportunityBook
from v8_next.opportunities.exposure import ExposureResolver
from v8_next.opportunities.models import (
    ExposureDirection,
    OpportunityRecord,
    OpportunityStatus,
)

_FIXED_AGGREGATION_NS = {
    "SECOND": 1_000_000_000,
    "MINUTE": 60_000_000_000,
    "HOUR": 3_600_000_000_000,
    "DAY": 86_400_000_000_000,
    "WEEK": 604_800_000_000_000,
}


def _bar_duration_ns(bar: Bar) -> int:
    """Derive a bar's duration from its own BarType; never assume 1h.

    Raises on non-fixed aggregations (e.g. MONTH, tick-based) instead of
    guessing start_ns, which would silently corrupt causal alignment.
    """
    spec = bar.bar_type.spec  # BarSpecification(step, aggregation, price_type)
    agg = getattr(spec.aggregation, "name", str(spec.aggregation))
    name = str(agg).split(".")[-1].upper()
    step = int(spec.step)
    if name not in _FIXED_AGGREGATION_NS or step <= 0:
        raise ValueError(f"non-fixed bar aggregation {agg!r}; refusing to guess start_ns")
    return step * _FIXED_AGGREGATION_NS[name]


#: Round-trip cost buffer for the research breakeven stop: one taker fee in and
#: one out at the default 0.0005 taker rate. Declared, not fitted.
_BREAKEVEN_COST_BUFFER = Decimal("0.001")


@dataclass(frozen=True)
class ExpertStrategyConfig:
    """Execution parameters and consensus gates for the 28-expert ensemble strategy."""

    instrument_id: str = "BTCUSDT-PERP.BINANCE"
    bar_type_str: str = "BTCUSDT-PERP.BINANCE-1-HOUR-LAST-EXTERNAL"
    grammar_policy: str = "range-breakout-48-v1"
    min_support_quorum: int = 1
    max_contradiction_tolerance: int = 0
    order_quantity: Decimal = Decimal("0.01")
    target_notional: Decimal | None = None
    #: Entry order type for new positions: MARKET or LIMIT (at the decision
    #: close). LIMIT rests in the book and fills only when touched, so it is
    #: the passive path; MARKET crosses immediately. Anything else fails closed
    #: at config validation instead of silently becoming a market order.
    entry_order_type: str = "MARKET"
    #: Child-order execution algorithm for entries: NONE sends the full size at
    #: once; TWAP splits the parent quantity into twap_slices equal children
    #: submitted on consecutive bars. NONE keeps the previous path byte-identical.
    execution_algo: str = "NONE"
    twap_slices: int = 4
    #: Fraction of account equity risked per trade (FixedRiskSizer). When set,
    #: quantity comes from entry/stop distance instead of target_notional.
    #: None keeps the previous sizing path byte-identical.
    #: Entry order type: MARKET | LIMIT | STOP_MARKET. A stop entry needs an
    #: explicit offset (``entry_stop_offset_pct``); without one the order is
    #: rejected by name rather than being silently turned into a market order.
    entry_stop_offset_pct: Decimal | None = None
    #: Optional order-emulation trigger (NautilusTrader TriggerType name, e.g.
    #: ``DEFAULT``). Set means the order is held by the OrderEmulator until its
    #: trigger condition is observable, which is how a passive/stop path is
    #: exercised without a live venue.
    entry_emulation_trigger: str | None = None
    risk_fraction: Decimal | None = None
    #: Hard per-order notional cap (RiskEngineConfig.max_notional_per_order
    #: equivalent). Orders above it are denied pre-submit and counted. None
    #: means no guard configured.
    max_notional_per_order: Decimal | None = None
    bracket_stop_pct: Decimal | None = None
    bracket_target_pct: Decimal | None = None
    max_concurrent_positions: int = 1
    #: RESEARCH-ONLY exit policy, disabled by default. When None the baseline
    #: static bracket path is byte-identical. ``BREAKEVEN_1R`` moves the resting
    #: stop to entry plus a declared round-trip cost buffer after the market has
    #: causally demonstrated +1R on a CLOSED bar; the modification is submitted
    #: at that bar's close, so it can only protect subsequent bars.
    research_exit_policy: str | None = None
    #: RESEARCH-ONLY admission lifecycle, disabled by default. When True, an
    #: opportunity detected on bar t-1 must still be confirmed by bar t's close
    #: (price holding beyond the level that produced it) before admission. This
    #: is a post-breakout persistence gate, not a breakout predictor.
    research_require_confirmation: bool = False
    #: RESEARCH-ONLY persistent opportunity lifecycle, disabled by default. When
    #: set to ``D4_RETEST_RECLAIM_LIFECYCLE`` the native strategy advances a
    #: per-opportunity retest/reclaim state machine over already closed bars and
    #: may only submit an entry once that machine reaches its ARMED state on a
    #: strictly earlier bar. It replaces, and is mutually exclusive with, the
    #: one-bar confirmation gate above.
    research_opportunity_lifecycle: str | None = None

    def __post_init__(self) -> None:
        # A stop without a target used to fall through to the non-bracket path
        # and submit an unprotected position. Fail closed at construction time
        # instead of silently trading a bracket that was only half configured.
        if (self.bracket_stop_pct is None) != (self.bracket_target_pct is None):
            raise ValueError(
                "bracket_stop_pct and bracket_target_pct must be configured together"
            )
        if self.research_exit_policy not in (None, "BREAKEVEN_1R"):
            raise ValueError(
                f"unknown research_exit_policy {self.research_exit_policy!r}; "
                "supported: None, BREAKEVEN_1R"
            )
        if self.research_opportunity_lifecycle not in (
            None,
            "D4_RETEST_RECLAIM_LIFECYCLE",
        ):
            raise ValueError(
                "unknown research_opportunity_lifecycle "
                f"{self.research_opportunity_lifecycle!r}; supported: None, "
                "D4_RETEST_RECLAIM_LIFECYCLE"
            )
        if self.research_opportunity_lifecycle and self.research_require_confirmation:
            raise ValueError(
                "research_opportunity_lifecycle and research_require_confirmation are "
                "two different admission policies; configure exactly one"
            )


class ExpertEnsembleStrategy(Strategy):
    """NautilusTrader Strategy powered by the canonical 28-expert witness ensemble.

    On every native Bar tick:
    1. Updates the causal prefix and builds a continuous PIT CausalFrame.
    2. Identifies opportunities via the configured grammar.
    3. Invokes observe_all_28() across all 28 canonical active witnesses.
    4. Evaluates consensus (support quorum vs contradiction tolerance).
    5. Dispatches native orders via NautilusTrader's OrderFactory when admitted.
    6. Retains full decision and stance ledgers for post-run audit.
    """

    def __new__(cls, *args: object, **kwargs: object) -> ExpertEnsembleStrategy:
        return super().__new__(cls)

    def __init__(
        self,
        config: ExpertStrategyConfig | None = None,
        *,
        source_candles: dict[int, Candle] | None = None,
        readings: tuple[PositioningReading, ...] = (),
        audit_recorder: Any | None = None,
    ) -> None:
        super().__init__()
        # Observe-only forensic recorder. Attaching one cannot change any
        # decision: every call below is a pure append and is wrapped so a
        # recorder failure never interrupts the replay.
        self.audit_recorder = audit_recorder
        self.ensemble_config = config or ExpertStrategyConfig()
        self.instrument_id = InstrumentId.from_str(self.ensemble_config.instrument_id)
        self.bar_type = BarType.from_str(self.ensemble_config.bar_type_str)
        self.source_candles = source_candles or {}
        ordered_source = tuple(sorted(self.source_candles.values(), key=lambda c: c.end_ns))
        self._ordered_source = ordered_source
        self._source_prefix_df = build_candle_dataframe(ordered_source) if ordered_source else None
        self.readings = readings

        # Opportunity book & exposure resolver
        self.opportunity_book = OpportunityBook()
        self.exposure_resolver = ExposureResolver()

        # Internal state & diagnostic ledgers
        self.candles: list[Candle] = []
        #: Last frame built for this bar sequence. Only used to carry validation
        #: forward one bar; always replaced by a fully validated frame on doubt.
        self._frame: CausalFrame | None = None
        #: Cross-bar indicator state for this instrument feed. Indicator fast
        #: paths use it only on proven prefixes and fall back otherwise, so a
        #: fresh engine is indistinguishable from none.
        self._feature_engine = FeatureEngine()
        self.decisions: list[dict[str, Any]] = []
        self.order_events: list[dict[str, Any]] = []
        self.opened_positions: list[dict[str, Any]] = []
        self.closed_positions: list[dict[str, Any]] = []
        # Pending TWAP child slices: dicts with side/quantity/bracket/prices.
        self.pending_twap: list[dict[str, Any]] = []
        #: Research-only stop-management state, keyed by native position id.
        self._research_stop_state: dict[str, dict[str, Any]] = {}
        #: Prior bar's opportunity, used by the research confirmation gate.
        self._research_prior_opportunity: dict[str, Any] | None = None
        #: RESEARCH-ONLY D4 persistent lifecycle (None unless configured).
        self._d4_lifecycle: Any | None = None
        if self.ensemble_config.research_opportunity_lifecycle:
            from v8_next.research.lifecycle import POLICY_ID, RetestReclaimLifecycle

            if self.ensemble_config.research_opportunity_lifecycle != POLICY_ID:
                raise ValueError(
                    f"unsupported research_opportunity_lifecycle "
                    f"{self.ensemble_config.research_opportunity_lifecycle!r}"
                )
            self._d4_lifecycle = RetestReclaimLifecycle()

    def on_start(self) -> None:
        """Subscribe to configured bar type on strategy start."""
        self.subscribe_bars(self.bar_type)

    def _account_equity(self, instrument: Any) -> Any | None:
        """Quote-currency account equity, or None when it cannot be read.

        Never guesses: a missing portfolio/account/balance keeps the caller
        failing closed instead of sizing against an assumed balance.
        """
        try:
            portfolio = self.portfolio
            if portfolio is None:
                return None
            # ``portfolio.account()`` needs a venue (or an account id) to resolve
            # the account: calling it bare returned None in-engine, which made
            # every risk-fraction decision fail closed as
            # RISK_SIZING_NO_STOP_OR_EQUITY and left the sizer inert in real runs.
            # The venue lives on the instrument id (``CryptoPerpetual`` exposes no
            # ``venue`` attribute in this build), so read it from there.
            instrument_id = getattr(instrument, "id", None)
            venue = getattr(instrument_id, "venue", None)
            if venue is None:
                return None
            account = portfolio.account(venue)
            if account is None:
                return None
            quote = getattr(instrument, "quote_currency", None)
            balance = account.balance(quote) if quote is not None else None
            if balance is None:
                balances = account.balances()
                if quote is not None and quote in balances:
                    balance = balances[quote]
                else:
                    balance = next(iter(balances.values()), None) if balances else None
            if balance is None:
                return None
            return balance.total
        except Exception:
            return None


    # ------------------------------------------------------------------ #
    # research-only exit policy
    # ------------------------------------------------------------------ #
    def _research_find_stop(self, position: Any) -> Any | None:
        """Find the resting protective stop of an open position."""
        stop: Any | None = None
        for order in self.cache.orders_open(instrument_id=self.instrument_id):
            if order.order_type != OrderType.STOP_MARKET:
                continue
            if order.is_closed:
                continue
            # The bracket's stop is the child on the opposite side of the
            # position; a same-side stop would be an entry, not protection.
            position_is_long = "LONG" in str(position.side).upper()
            if position_is_long and order.side == OrderSide.BUY:
                continue
            if not position_is_long and order.side == OrderSide.SELL:
                continue
            stop = order
            break
        return stop

    def _research_apply_exit_policy(self, bar: Bar) -> None:
        policy = self.ensemble_config.research_exit_policy
        if policy is None:
            return
        try:
            instrument = self.cache.instrument(self.instrument_id)
            if instrument is None:
                return
            open_ids = set()
            for position in self.cache.positions_open(instrument_id=self.instrument_id):
                pid = str(position.id)
                open_ids.add(pid)
                state = self._research_stop_state.get(pid)
                if state is None:
                    stop_order = self._research_find_stop(position)
                    if stop_order is None:
                        self._audit(
                            "POSITION_CHANGED",
                            int(bar.ts_event),
                            evidence="OBSERVED_SIMULATION",
                            policy=policy,
                            research_note="RESEARCH_STOP_NOT_FOUND",
                            position_id=pid,
                            open_orders=len(
                                self.cache.orders_open(instrument_id=self.instrument_id)
                            ),
                        )
                        continue
                    entry = float(position.avg_px_open)
                    trigger = stop_order.trigger_price or stop_order.price
                    stop_px = float(trigger) if trigger is not None else None
                    if stop_px is None:
                        continue
                    risk = abs(entry - stop_px)
                    if risk <= 0:
                        continue
                    state = {
                        "entry": entry,
                        "risk": risk,
                        "is_long": "LONG" in str(position.side).upper(),
                        "stop_client_id": str(stop_order.client_order_id),
                        # The bar on which the position first becomes visible:
                        # its high/low predate the fill, so it cannot trigger.
                        "seen_bar_end_ns": int(bar.ts_event),
                        "triggered": False,
                    }
                    self._research_stop_state[pid] = state
                    self._audit(
                        "POSITION_CHANGED",
                        int(bar.ts_event),
                        evidence="OBSERVED_SIMULATION",
                        policy=policy,
                        research_note="RESEARCH_STOP_TRACKED",
                        position_id=pid,
                        entry=str(entry),
                        initial_stop=str(stop_px),
                        risk=str(risk),
                        is_long=state["is_long"],
                    )
                    continue
                if state["triggered"]:
                    continue
                if int(bar.ts_event) <= state["seen_bar_end_ns"]:
                    continue
                entry = state["entry"]
                risk = state["risk"]
                is_long = state["is_long"]
                trigger_px = entry + risk if is_long else entry - risk
                high = bar.high.as_double()
                low = bar.low.as_double()
                reached = high >= trigger_px if is_long else low <= trigger_px
                if not reached:
                    continue
                order = self.cache.order(ClientOrderId(state["stop_client_id"]))
                if order is None or order.is_closed:
                    state["triggered"] = True
                    continue
                buffer = entry * float(_BREAKEVEN_COST_BUFFER)
                new_stop = entry + buffer if is_long else entry - buffer
                self.modify_order(
                    ClientOrderId(state["stop_client_id"]),
                    trigger_price=Price(new_stop, instrument.price_precision),
                )
                state["triggered"] = True
                self._audit(
                    "PROTECTIVE_STOP_UPDATED",
                    int(bar.ts_event),
                    evidence="OBSERVED_SIMULATION",
                    policy=policy,
                    position_id=pid,
                    from_trigger=str(order.trigger_price),
                    to_trigger=str(new_stop),
                    trigger_bar_high=str(bar.high),
                    trigger_bar_low=str(bar.low),
                    evidence_class_note="RESEARCH_ONLY_DISABLED_BY_DEFAULT",
                    research_note="RESEARCH_STOP_MOVED_BREAKEVEN_1R",
                )
            for pid in list(self._research_stop_state):
                if pid not in open_ids:
                    self._research_stop_state.pop(pid, None)
        except Exception:
            # A research hook never interrupts the replay path.
            return

    def _twap_slices(self, quantity: Decimal, step: Decimal, n: int) -> list[Decimal]:
        """Split a parent quantity into n step-aligned child slices.

        First n-1 slices are floored to the step; the last takes the remainder
        so the children always sum back to the parent.
        """
        if n <= 1 or step <= 0 or quantity <= 0:
            return [quantity]
        child = (quantity / n // step) * step
        slices = [child] * (n - 1)
        slices.append(quantity - sum(slices))
        return [s for s in slices if s > 0]

    def _drain_twap_slice(self, instrument: Any, record: Any) -> str | None:
        """Submit one pending TWAP child; return its action or None."""
        if not self.pending_twap or self.ensemble_config.execution_algo != "TWAP":
            return None
        sl = self.pending_twap.pop(0)
        qty = Quantity.from_str(format(sl["quantity"], f".{instrument.size_precision}f"))
        if sl["bracket"]:
            order_list = self.order_factory.bracket(
                instrument_id=self.instrument_id,
                order_side=sl["side"],
                quantity=qty,
                entry_order_type=sl["entry_order_type"],
                entry_price=sl.get("entry_price"),
                sl_trigger_price=sl["sl_price"],
                tp_price=sl["tp_price"],
            )
            self.submit_order_list(order_list)
        elif sl["entry_order_type"] == OrderType.LIMIT:
            self.submit_order(
                self.order_factory.limit(
                    instrument_id=self.instrument_id,
                    order_side=sl["side"],
                    quantity=qty,
                    price=sl["entry_price"],
                )
            )
        else:
            self.submit_order(
                self.order_factory.market(
                    instrument_id=self.instrument_id,
                    order_side=sl["side"],
                    quantity=qty,
                )
            )
        if record is not None:
            self.opportunity_book.update_status(record.opportunity_id, OpportunityStatus.ADMITTED)
        return f"TWAP_CHILD_{sl['done']}/{sl['total']}"

    def on_bar(self, bar: Bar) -> None:
        """Process incoming native Bar tick through the 28-expert ensemble."""
        # 1. Obtain or construct causal Candle
        use_prefix = False
        if bar.ts_event in self.source_candles:
            candle = self.source_candles[bar.ts_event]
            # Positional guard: the prefix cache is only valid while the live
            # sequence is exactly the ordered source (identity + position).
            # Any deviation (gap, reorder, duplicate) falls back to frame_at.
            if (
                self._source_prefix_df is not None
                and len(self.candles) < len(self._ordered_source)
                and self._ordered_source[len(self.candles)] is candle
            ):
                use_prefix = True
        else:
            duration_ns = _bar_duration_ns(bar)
            start_ns = bar.ts_event - duration_ns if bar.ts_event >= duration_ns else 0
            candle = Candle(
                instrument_id=str(self.instrument_id),
                start_ns=start_ns,
                end_ns=bar.ts_event,
                open=bar.open.as_decimal(),
                high=bar.high.as_decimal(),
                low=bar.low.as_decimal(),
                close=bar.close.as_decimal(),
                volume=bar.volume.as_decimal(),
                received_ns=bar.ts_init,
                available_ns=bar.ts_init,
                source_hash="nautilus-bar-feed",
            )
        self.candles.append(candle)
        self._audit(
            "MARKET_OBSERVED",
            bar.ts_init,
            evidence="OBSERVED_TAPE",
            open=str(candle.open),
            high=str(candle.high),
            low=str(candle.low),
            close=str(candle.close),
            volume=str(candle.volume),
            bar_start_ns=candle.start_ns,
            bar_end_ns=candle.end_ns,
            market_data_available_ns=candle.available_ns,
            received_ns=candle.received_ns,
        )

        # 1b. Research-only exit policy (no-op unless explicitly configured).
        self._research_apply_exit_policy(bar)

        # 2. Advance OpportunityBook with newly closed candle (invalidation & TTL check)
        for transitioned in self.opportunity_book.on_candle(candle):
            self._audit(
                "OPPORTUNITY_EXPIRED"
                if transitioned.status == OpportunityStatus.EXPIRED
                else "OPPORTUNITY_INVALIDATED",
                candle.end_ns,
                evidence="DERIVED",
                opportunity_id=transitioned.opportunity_id,
                status=str(transitioned.status),
            )

        # 3. Build CausalFrame
        #
        # Fast path: extend the previous bar's validated frame by exactly the
        # candle just appended. `use_prefix` already proved the live bar is the
        # next element of the ordered source this frame was built from, so the
        # parent's validation is carried forward and only the appended candle is
        # verified. `extend_causal_frame` returns None instead of guessing, and
        # any fallback revalidates the whole prefix as before.
        frame: CausalFrame | None = None
        if use_prefix and self._frame is not None and len(self._frame.candles) + 1 == len(self.candles):
            frame = extend_causal_frame(
                self._frame, bar.ts_init, candle, self._source_prefix_df
            )
        if frame is None:
            if use_prefix:
                frame = CausalFrame.from_ordered_prefix(
                    str(self.instrument_id), bar.ts_init, tuple(self.candles), self._source_prefix_df
                )
            else:
                frame = frame_at(str(self.instrument_id), bar.ts_init, tuple(self.candles))
        self._frame = frame

        # 4. Detect Opportunity
        opportunity: Opportunity | None
        if self.ensemble_config.grammar_policy == "range-breakout-48-v1":
            opportunity = opportunity_at(frame)
        else:
            opportunity = grammar_opportunity(frame, self.ensemble_config.grammar_policy)

        # 5. Record into OpportunityBook if newly detected
        record: OpportunityRecord | None = None
        if opportunity is not None:
            dir_enum = (
                ExposureDirection.LONG
                if opportunity.direction == "LONG"
                else ExposureDirection.SHORT
            )
            raw_sym = str(self.instrument_id).split(".")[0].replace("-PERP", "")
            venue_str = "binance-um"
            try:
                exposure = self.exposure_resolver.resolve_ticker(raw_sym, venue_str, dir_enum)
            except Exception:
                from v8_next.opportunities.models import EconomicExposureStructure

                exposure = EconomicExposureStructure.single_perp(
                    raw_sym, raw_sym.replace("USDT", ""), venue_str, "USDT", dir_enum
                )

            entry_px = bar.close.as_decimal()
            stop_px: Decimal | None = None
            target_px: Decimal | None = None
            if self.ensemble_config.bracket_stop_pct is not None:
                stop_dist = entry_px * self.ensemble_config.bracket_stop_pct
                stop_px = (
                    entry_px - stop_dist
                    if dir_enum == ExposureDirection.LONG
                    else entry_px + stop_dist
                )
            if self.ensemble_config.bracket_target_pct is not None:
                target_dist = entry_px * self.ensemble_config.bracket_target_pct
                target_px = (
                    entry_px + target_dist
                    if dir_enum == ExposureDirection.LONG
                    else entry_px - target_dist
                )

            record = OpportunityRecord.create(
                exposure=exposure,
                instrument_id=str(self.instrument_id),
                direction=dir_enum,
                entry_price=entry_px,
                stop_price=stop_px,
                target_price=target_px,
                as_of_time_ns=candle.end_ns,
                valid_until_ns=opportunity.expires_ns,
                status=OpportunityStatus.CANDIDATE,
            )
            self.opportunity_book.insert(record)
            self._audit(
                "OPPORTUNITY_DETECTED",
                candle.end_ns,
                evidence="DERIVED",
                opportunity_id=record.opportunity_id,
                status=str(record.status),
                direction=str(record.direction),
                entry_price=str(record.entry_price),
                stop_price=str(record.stop_price) if record.stop_price is not None else None,
                target_price=(
                    str(record.target_price) if record.target_price is not None else None
                ),
                valid_until_ns=record.valid_until_ns,
                as_of_time_ns=record.as_of_time_ns,
            )

        # 6. Invoke all 28 canonical active witnesses
        try:
            stances = observe_all_28(
                frame, opportunity, readings=self.readings, engine=self._feature_engine
            )
        except Exception as exc:  # audit the failure, then preserve fail-closed behaviour
            self._audit(
                "EXPERT_EVALUATED",
                candle.end_ns,
                evidence="DERIVED",
                expert_scope="ALL_28",
                expert_count=len(CANONICAL_28_EXPERTS),
                stance="EVALUATION_ERROR",
                error_type=type(exc).__name__,
                opportunity_id=(record.opportunity_id if record is not None else None),
            )
            raise
        supports = [s for s in stances if s.kind == StanceKind.SUPPORT]
        contradicts = [s for s in stances if s.kind == StanceKind.CONTRADICT]
        abstains = [s for s in stances if s.kind == StanceKind.ABSTAIN]

        # 7. Evaluate Consensus & Admission
        is_supported = (
            opportunity is not None
            and len(supports) >= self.ensemble_config.min_support_quorum
            and len(contradicts) <= self.ensemble_config.max_contradiction_tolerance
        )
        if record is not None and is_supported:
            self.opportunity_book.update_status(record.opportunity_id, OpportunityStatus.CONFIRMED)
            self._audit(
                "OPPORTUNITY_UPDATED",
                candle.end_ns,
                evidence="DERIVED",
                opportunity_id=record.opportunity_id,
                status=str(OpportunityStatus.CONFIRMED),
                from_status=str(OpportunityStatus.CANDIDATE),
            )
        if record is None:
            # No opportunity exists, so the witness panel was not invoked for a
            # trade. Record the whole panel once, truthfully, as NO_OPPORTUNITY
            # instead of fabricating 28 per-expert stances.
            self._audit(
                "EXPERT_EVALUATED",
                candle.end_ns,
                evidence="DERIVED",
                expert_scope="ALL_28",
                expert_count=len(CANONICAL_28_EXPERTS),
                stance="NO_OPPORTUNITY",
                support_count=len(supports),
                contradiction_count=len(contradicts),
                abstain_count=len(abstains),
                consensus_supported=False,
            )
        else:
            self._audit(
                "CONSENSUS_EVALUATED",
                candle.end_ns,
                evidence="DERIVED",
                support_count=len(supports),
                contradiction_count=len(contradicts),
                abstain_count=len(abstains),
                consensus_supported=is_supported,
                opportunity_id=record.opportunity_id,
            )
            seen: set[str] = set()
            for stance in stances:
                seen.add(stance.observer_id)
                from v8_next.audit.forensic import evaluation_state

                raw_stance = getattr(stance.kind, "value", stance.kind)
                state = evaluation_state(raw_stance)
                raw_label = str(raw_stance)
                raw_field = {} if raw_label.upper() == state else {"raw_stance": raw_label}
                self._audit(
                    "EXPERT_EVALUATED",
                    candle.end_ns,
                    evidence="DERIVED",
                    expert_id=stance.observer_id,
                    opportunity_id=record.opportunity_id,
                    decision_id=f"D{candle.end_ns}",
                    stance=state,
                    reason_code=stance.reason,
                    **raw_field,
                    variant_id=stance.variant_id,
                    market_data_available_ns=candle.available_ns,
                )
            for missing in CANONICAL_28_EXPERTS:
                if missing in seen:
                    continue
                self._audit(
                    "EXPERT_EVALUATED",
                    candle.end_ns,
                    evidence="DERIVED",
                    expert_id=missing,
                    opportunity_id=record.opportunity_id,
                    decision_id=f"D{candle.end_ns}",
                    stance="NOT_EVALUATED",
                    reason_code="WITNESS_NOT_INVOKED",
                )

        # 5b. Research-only admission lifecycle: the previous bar's opportunity
        # must still be confirmed by this bar's close. No-op by default.
        confirmation_ok = True
        if self.ensemble_config.research_require_confirmation:
            prior = self._research_prior_opportunity
            if prior is None or opportunity is None:
                confirmation_ok = False
            elif str(prior["direction"]) != str(opportunity.direction):
                confirmation_ok = False
            else:
                level = float(prior["level"])
                close = bar.close.as_double()
                confirmation_ok = close >= level if prior["is_long"] else close <= level
            if is_supported and not confirmation_ok:
                self._audit(
                    "ADMISSION_REJECTED",
                    candle.end_ns,
                    evidence="DERIVED",
                    opportunity_id=(record.opportunity_id if record is not None else None),
                    reason_code="RESEARCH_CONFIRMATION_FAILED",
                    prior_level=(self._research_prior_opportunity or {}).get("level"),
                    close=str(bar.close),
                    evidence_class_note="RESEARCH_ONLY_DISABLED_BY_DEFAULT",
                )

        # 5c. RESEARCH-ONLY D4 persistent lifecycle (no-op unless configured).
        # This runs *before* any order decision so the native strategy consumes
        # the state machine rather than logging it after the fact.
        d4_due: list[Any] = []
        d4_entry: Any | None = None
        if self._d4_lifecycle is not None:
            if candle.available_ns is None:
                raise TypeError("D4 lifecycle requires candle available_ns")
            market_available_ns = int(candle.available_ns)
            # (a) advance lifecycles that already exist on this completed bar
            self._d4_lifecycle.advance(
                bar_ns=int(candle.end_ns),
                available_ns=market_available_ns,
                high=bar.high.as_double(),
                low=bar.low.as_double(),
                close=bar.close.as_double(),
            )
            # (b) register this bar's fresh detection in WATCHING (never entering
            # immediately); registration happens after the advance so a new
            # opportunity can never transition on its own detection bar.
            if record is not None and opportunity is not None:
                if record.stop_price is None or record.target_price is None:
                    raise TypeError("D4 lifecycle requires stop and target prices")
                self._d4_lifecycle.detect(
                    opportunity_id=record.opportunity_id,
                    direction=opportunity.direction,
                    reference_price=float(record.entry_price),
                    stop_price=float(record.stop_price),
                    target_price=float(record.target_price),
                    detected_ns=int(candle.end_ns),
                    available_ns=market_available_ns,
                    bar_ns=int(candle.end_ns),
                    expert_supported=bool(is_supported),
                )
                self._d4_risk_from_config(record)
            # (c) an entry armed on a strictly earlier bar may be submitted now
            d4_due = self._d4_lifecycle.due_entries(int(candle.end_ns))
            blocked = [lc for lc in d4_due if not lc.expert_supported]
            for lifecycle in blocked:
                self._d4_lifecycle.cancel(
                    lifecycle,
                    bar_ns=int(candle.end_ns),
                    available_ns=market_available_ns,
                    reason="EXPERT_GATE_NOT_SUPPORTED",
                )
            d4_due = [lc for lc in d4_due if lc.expert_supported]
            d4_entry = d4_due[0] if d4_due else None

        # 6. Execute Native Orders via NautilusTrader OrderFactory
        action = "NO_ACTION"
        sizing_mode = "FLAT"
        # Drain one pending TWAP child even when this bar carries no supported
        # opportunity: children continue a scheduled parent. A drained bar emits
        # only the child (parent gate below is skipped for it).
        twap_action = None
        if self.ensemble_config.execution_algo == "TWAP" and self.pending_twap:
            drain_instrument = self.cache.instrument(self.instrument_id)
            if drain_instrument is not None:
                twap_action = self._drain_twap_slice(drain_instrument, record)
        if twap_action is not None:
            action = twap_action
            sizing_mode = "TWAP"
        gate_open = (
            d4_entry is not None
            if self._d4_lifecycle is not None
            else (is_supported and opportunity is not None and confirmation_ok)
        )
        entry_direction = d4_entry.direction if d4_entry is not None else (
            opportunity.direction if opportunity is not None else None
        )
        entry_opportunity_id = (
            d4_entry.opportunity_id
            if d4_entry is not None
            else (record.opportunity_id if record is not None else None)
        )
        if gate_open and twap_action is None:
            open_positions = [
                p
                for p in self.cache.positions_open()
                if p.instrument_id == self.instrument_id and not p.is_closed
            ]
            open_orders = [
                o
                for o in self.cache.orders_open()
                if o.instrument_id == self.instrument_id and not o.is_closed
            ]

            if (
                len(open_positions) < self.ensemble_config.max_concurrent_positions
                and not open_orders
            ):
                instrument = self.cache.instrument(self.instrument_id)
                if instrument is not None:
                    side = OrderSide.BUY if entry_direction == "LONG" else OrderSide.SELL
                    base_qty = self.ensemble_config.order_quantity
                    if self.ensemble_config.target_notional is not None:
                        px = bar.close.as_decimal()
                        step = instrument.size_increment.as_decimal()
                        if px > 0 and step > 0:
                            steps = (self.ensemble_config.target_notional / px) // step
                            base_qty = steps * step
                    if d4_entry is not None:
                        # Declared risk-normalized sizing: the entry price moved
                        # between detection and the armed bar, so the quantity is
                        # scaled to keep the USDT risk target implied by the
                        # original opportunity (config quantity x original R).
                        # The stop/target stay the existing fixed % bracket.
                        stop_pct = self.ensemble_config.bracket_stop_pct
                        step = instrument.size_increment.as_decimal()
                        px = bar.close.as_decimal()
                        risk_target = Decimal(str(d4_entry.risk_target_usdt))
                        denom = px * stop_pct if stop_pct is not None else Decimal(0)
                        if denom > 0 and step > 0 and risk_target > 0:
                            steps = (risk_target / denom) / step
                            base_qty = steps.to_integral_value(rounding=ROUND_FLOOR) * step
                            sizing_mode = "D4_RISK_NORMALIZED"
                    sizing_mode = "FLAT"
                    if self.ensemble_config.risk_fraction is not None:
                        # Risk-fraction sizing (FixedRiskSizer): quantity from
                        # entry/stop distance and account equity. Fail closed
                        # when the stop distance or the equity is unavailable.
                        from v8_next.adapters.risk_sizing import risk_sized_quantity

                        sizing_mode = "RISK_FRACTION"
                        stop_pct = self.ensemble_config.bracket_stop_pct
                        entry_px = bar.close.as_decimal()
                        equity = self._account_equity(instrument)
                        if stop_pct is None or entry_px <= 0 or equity is None:
                            action = "RISK_SIZING_NO_STOP_OR_EQUITY"
                            base_qty = Decimal(0)
                        else:
                            stop_dist = entry_px * stop_pct
                            stop_px = (
                                entry_px - stop_dist
                                if entry_direction == "LONG"
                                else entry_px + stop_dist
                            )
                            base_qty = risk_sized_quantity(
                                instrument,
                                Price(float(entry_px), instrument.price_precision),
                                Price(float(stop_px), instrument.price_precision),
                                equity,
                                self.ensemble_config.risk_fraction,
                            ).as_decimal()
                    if base_qty > 0 and self.ensemble_config.max_notional_per_order is not None:
                        from v8_next.adapters.risk_sizing import check_max_notional

                        allowed, _notional = check_max_notional(
                            base_qty,
                            bar.close,
                            self.ensemble_config.max_notional_per_order,
                        )
                        if not allowed:
                            action = "DENIED_MAX_NOTIONAL"
                            base_qty = Decimal(0)
                    qty_str = format(base_qty, f".{instrument.size_precision}f")
                    quantity: Quantity | None = None
                    floored_to_minimum = False
                    if Decimal(qty_str) < instrument.min_quantity.as_decimal():
                        # Only a genuinely unsized order is BELOW_MIN_QUANTITY. A
                        # risk decision that zeroed the size must keep its own
                        # name: overwriting DENIED_MAX_NOTIONAL (or a missing
                        # stop/equity) with a size symptom makes the brake
                        # unauditable from the decision ledger.
                        if action == "NO_ACTION":
                            action = "BELOW_MIN_QUANTITY"
                    else:
                        quantity = Quantity.from_str(qty_str)
                    if quantity is None and action in ("NO_ACTION", "BELOW_MIN_QUANTITY"):
                        # Floor UP to the venue minimum instead of dying at the
                        # venue gate: a real desk rounds a dust order up to the
                        # minimum or drops the signal with a named reason, it
                        # does not submit a known-to-deny order. The floor is
                        # capped by max_notional so the ceiling brake keeps its
                        # meaning; the risk-fraction overshoot is recorded on
                        # the decision (action name) instead of hidden.
                        step = Decimal(10) ** -instrument.size_precision
                        min_step_qty = instrument.min_quantity.as_decimal()
                        px = bar.close.as_decimal()
                        min_notional_qty = (
                            (instrument.min_notional.as_decimal() / px) / step
                        ).to_integral_value(rounding=ROUND_CEILING) * step
                        floored = max(min_step_qty, min_notional_qty)
                        if (
                            self.ensemble_config.max_notional_per_order is None
                            or floored * px <= self.ensemble_config.max_notional_per_order
                        ):
                            quantity = Quantity.from_str(
                                format(floored, f".{instrument.size_precision}f")
                            )
                            qty_str = format(floored, f".{instrument.size_precision}f")
                            action = "SIZED_TO_MIN_NOTIONAL"
                            floored_to_minimum = True

                    # Determine if bracket protection order list is configured
                    if quantity is None:
                        pass
                    elif (
                        self.ensemble_config.bracket_stop_pct is not None
                        and self.ensemble_config.bracket_target_pct is not None
                    ):
                        entry_px = bar.close.as_decimal()
                        stop_dist = entry_px * self.ensemble_config.bracket_stop_pct
                        target_dist = entry_px * self.ensemble_config.bracket_target_pct
                        if entry_direction == "LONG":
                            stop_px = entry_px - stop_dist
                            target_px = entry_px + target_dist
                        else:
                            stop_px = entry_px + stop_dist
                            target_px = entry_px - target_dist

                        sl_price = Price(float(stop_px), instrument.price_precision)
                        tp_price = Price(float(target_px), instrument.price_precision)

                        entry_type = self.ensemble_config.entry_order_type
                        entry_trigger_price = None
                        if entry_type == "MARKET":
                            entry_order_type = OrderType.MARKET
                            entry_limit_price = None
                        elif entry_type == "LIMIT":
                            entry_order_type = OrderType.LIMIT
                            entry_limit_price = Price(float(entry_px), instrument.price_precision)
                        elif entry_type == "STOP_MARKET":
                            offset = self.ensemble_config.entry_stop_offset_pct
                            if offset is None or offset <= 0:
                                action = "REJECTED_STOP_MARKET_WITHOUT_OFFSET"
                                entry_order_type = None
                                entry_limit_price = None
                            else:
                                entry_order_type = OrderType.STOP_MARKET
                                entry_limit_price = None
                                trigger = (
                                    entry_px + entry_px * offset
                                    if side == OrderSide.BUY
                                    else entry_px - entry_px * offset
                                )
                                entry_trigger_price = Price(
                                    float(trigger), instrument.price_precision
                                )
                        else:
                            action = f"REJECTED_UNKNOWN_ENTRY_TYPE_{entry_type}"
                            entry_order_type = None
                            entry_limit_price = None
                        emulation_trigger = None
                        emulation_label = "NO_EMULATION"
                        configured_trigger = self.ensemble_config.entry_emulation_trigger
                        if entry_order_type is not None and configured_trigger is not None:
                            try:
                                emulation_trigger = TriggerType.from_str(configured_trigger)
                            except ValueError:
                                action = f"REJECTED_UNKNOWN_EMULATION_TRIGGER_{configured_trigger}"
                                entry_order_type = None
                            else:
                                emulation_label = f"EMULATED_{configured_trigger}"
                        if entry_order_type is None:
                            quantity = None
                        elif entry_order_type == OrderType.STOP_MARKET:
                            assert entry_trigger_price is not None
                            # NautilusTrader's bracket builder accepts only MARKET
                            # and LIMIT entries (it raises on STOP_MARKET), so a
                            # stop entry is submitted on its own and the ledger
                            # says UNBRACKETED rather than dropping the protection
                            # orders silently.
                            self.submit_order(
                                self.order_factory.stop_market(
                                    instrument_id=self.instrument_id,
                                    order_side=side,
                                    quantity=quantity,
                                    trigger_price=entry_trigger_price,
                                    emulation_trigger=emulation_trigger,
                                )
                            )
                            if record is not None:
                                self.opportunity_book.update_status(
                                    record.opportunity_id, OpportunityStatus.ADMITTED
                                )
                            action = (
                                f"SUBMITTED_STOP_MARKET_UNBRACKETED_{side.name}_"
                                f"{qty_str}_{emulation_label}"
                            )
                            if floored_to_minimum:
                                action = f"SIZED_TO_MIN_NOTIONAL+{action}"
                        else:
                            submit_qty = quantity
                            twap_tag = ""
                            if (
                                self.ensemble_config.execution_algo == "TWAP"
                                and quantity is not None
                            ):
                                slices = self._twap_slices(
                                    quantity.as_decimal(),
                                    instrument.size_increment.as_decimal(),
                                    self.ensemble_config.twap_slices,
                                )
                                if len(slices) > 1:
                                    submit_qty = Quantity.from_str(
                                        format(slices[0], f".{instrument.size_precision}f")
                                    )
                                    for idx, s in enumerate(slices[1:], start=2):
                                        self.pending_twap.append(
                                            {
                                                "side": side,
                                                "quantity": s,
                                                "bracket": True,
                                                "entry_order_type": entry_order_type,
                                                "entry_price": entry_limit_price,
                                                "entry_trigger_price": entry_trigger_price,
                                                "emulation_trigger": emulation_trigger,
                                                "sl_price": sl_price,
                                                "tp_price": tp_price,
                                                "done": idx,
                                                "total": len(slices),
                                            }
                                        )
                                    twap_tag = f"TWAP_PARENT_1/{len(slices)}_"
                            order_list = self.order_factory.bracket(
                                instrument_id=self.instrument_id,
                                order_side=side,
                                quantity=submit_qty,
                                entry_order_type=entry_order_type,
                                entry_price=entry_limit_price,
                                entry_trigger_price=entry_trigger_price,
                                emulation_trigger=emulation_trigger,
                                sl_trigger_price=sl_price,
                                tp_price=tp_price,
                            )
                            self.submit_order_list(order_list)
                            # Protection lifecycle is recorded at submission: the
                            # native accept event does not always expose the child
                            # order type, so inferring protection from fills alone
                            # would leave G7 unprovable.
                            self._audit(
                                "PROTECTIVE_STOP_CREATED",
                                int(bar.ts_event),
                                evidence="OBSERVED_SIMULATION",
                                order_role="STOP_LOSS",
                                order_type="STOP_MARKET",
                                trigger_price=str(sl_price),
                                quantity=str(submit_qty),
                                child_index=1,
                            )
                            self._audit(
                                "TARGET_CREATED",
                                int(bar.ts_event),
                                evidence="OBSERVED_SIMULATION",
                                order_role="TAKE_PROFIT",
                                order_type="LIMIT",
                                price=str(tp_price),
                                quantity=str(submit_qty),
                                child_index=2,
                            )
                            if record is not None:
                                self.opportunity_book.update_status(
                                    record.opportunity_id, OpportunityStatus.ADMITTED
                                )
                            action = (
                                f"{twap_tag}SUBMITTED_BRACKET_{entry_type}_{side.name}_"
                                f"{qty_str}_{emulation_label}"
                            )
                            if floored_to_minimum:
                                action = f"SIZED_TO_MIN_NOTIONAL+{action}"
                    else:
                        entry_type = self.ensemble_config.entry_order_type
                        if entry_type == "LIMIT":
                            order = self.order_factory.limit(
                                instrument_id=self.instrument_id,
                                order_side=side,
                                quantity=quantity,
                                price=Price(
                                    float(bar.close.as_decimal()), instrument.price_precision
                                ),
                            )
                        elif entry_type == "MARKET":
                            order = self.order_factory.market(
                                instrument_id=self.instrument_id,
                                order_side=side,
                                quantity=quantity,
                            )
                        elif entry_type == "STOP_MARKET":
                            # A stop entry without bracket configuration has no
                            # target/stop pair to attach to; fail closed by name
                            # instead of submitting a market order in disguise.
                            order = None
                            action = "REJECTED_STOP_MARKET_REQUIRES_BRACKET_CONFIG"
                        else:
                            order = None
                            action = f"REJECTED_UNKNOWN_ENTRY_TYPE_{entry_type}"
                        if order is not None:
                            submit_order = order
                            twap_tag = ""
                            if self.ensemble_config.execution_algo == "TWAP":
                                slices = self._twap_slices(
                                    quantity.as_decimal(),
                                    instrument.size_increment.as_decimal(),
                                    self.ensemble_config.twap_slices,
                                )
                                if len(slices) > 1:
                                    if entry_type == "LIMIT":
                                        submit_order = self.order_factory.limit(
                                            instrument_id=self.instrument_id,
                                            order_side=side,
                                            quantity=Quantity.from_str(
                                                format(slices[0], f".{instrument.size_precision}f")
                                            ),
                                            price=Price(
                                                float(bar.close.as_decimal()),
                                                instrument.price_precision,
                                            ),
                                        )
                                    else:
                                        submit_order = self.order_factory.market(
                                            instrument_id=self.instrument_id,
                                            order_side=side,
                                            quantity=Quantity.from_str(
                                                format(slices[0], f".{instrument.size_precision}f")
                                            ),
                                        )
                                    for idx, s in enumerate(slices[1:], start=2):
                                        self.pending_twap.append(
                                            {
                                                "side": side,
                                                "quantity": s,
                                                "bracket": False,
                                                "entry_order_type": (
                                                    OrderType.LIMIT
                                                    if entry_type == "LIMIT"
                                                    else OrderType.MARKET
                                                ),
                                                "entry_price": (
                                                    Price(
                                                        float(bar.close.as_decimal()),
                                                        instrument.price_precision,
                                                    )
                                                    if entry_type == "LIMIT"
                                                    else None
                                                ),
                                                "sl_price": None,
                                                "tp_price": None,
                                                "done": idx,
                                                "total": len(slices),
                                            }
                                        )
                                    twap_tag = f"TWAP_PARENT_1/{len(slices)}_"
                            self.submit_order(submit_order)
                            if record is not None:
                                self.opportunity_book.update_status(
                                    record.opportunity_id, OpportunityStatus.ADMITTED
                                )
                            action = f"{twap_tag}SUBMITTED_{entry_type}_{side.name}_{qty_str}"
                            if floored_to_minimum:
                                action = f"SIZED_TO_MIN_NOTIONAL+{action}"
            else:
                action = "POSITION_OCCUPIED"

            if self._d4_lifecycle is not None:
                self._d4_finalize_submission(d4_entry, d4_due, action, candle)

        if entry_opportunity_id is not None:
            if action.startswith(("SUBMITTED", "SIZED_TO_MIN_NOTIONAL+SUBMITTED", "TWAP")):
                self._audit(
                    "ADMISSION_ACCEPTED",
                    candle.end_ns,
                    evidence="DERIVED",
                    opportunity_id=entry_opportunity_id,
                    decision_id=f"D{candle.end_ns}",
                    action=action,
                    sizing_mode=sizing_mode,
                )
                self._audit(
                    "ORDER_SUBMITTED",
                    candle.end_ns,
                    evidence="OBSERVED_SIMULATION",
                    opportunity_id=entry_opportunity_id,
                    decision_id=f"D{candle.end_ns}",
                    action=action,
                    intent_id=f"D{candle.end_ns}",
                    order_linkage="INTENT_ONLY (submit return value not exposed; native ids follow)",
                )
            elif action in {"POSITION_OCCUPIED"}:
                self._audit(
                    "ADMISSION_REJECTED",
                    candle.end_ns,
                    evidence="DERIVED",
                    opportunity_id=entry_opportunity_id,
                    decision_id=f"D{candle.end_ns}",
                    reason_code=action,
                    action=action,
                )

        # 7. Record Full Diagnostic Trace
        self.decisions.append(
            {
                "bar_idx": len(self.candles) - 1,
                "decision_ns": bar.ts_init,
                # Audit lineage: the decision id used by the event ledger and the
                # opportunity the decision was made about. Without these two
                # fields the lifecycle builder cannot join a campaign to its
                # opportunity/decision and silently writes nulls.
                "decision_id": f"D{candle.end_ns}",
                "opportunity_id": entry_opportunity_id,
                "open": str(bar.open),
                "high": str(bar.high),
                "low": str(bar.low),
                "close": str(bar.close),
                "opportunity": opportunity_record(opportunity) if opportunity else None,
                "support_count": len(supports),
                "contradict_count": len(contradicts),
                "abstain_count": len(abstains),
                "consensus_supported": is_supported,
                "action": action,
                "sizing_mode": sizing_mode,
                "stances": [stance_record(s) for s in stances],
            }
        )

        # Remember this bar's opportunity for the research confirmation gate.
        if self.ensemble_config.research_require_confirmation:
            if opportunity is not None and record is not None:
                self._research_prior_opportunity = {
                    "direction": opportunity.direction,
                    "level": float(record.entry_price),
                    "is_long": str(opportunity.direction).upper().endswith("LONG"),
                    "bar_end_ns": int(candle.end_ns),
                    "opportunity_id": record.opportunity_id,
                }
            else:
                self._research_prior_opportunity = None

    def _d4_risk_from_config(self, record: Any) -> None:
        """Declare the D4 risk target: config quantity x original R price.

        Keeping the USDT risk equal to the one the unchanged baseline would
        have taken on the original opportunity is what makes delayed entries
        comparable; the quantity is then rescaled at submission.
        """
        policy = self._d4_lifecycle
        if policy is None or record is None:
            return
        lifecycle = policy.lifecycles.get(str(record.opportunity_id))
        if lifecycle is None:
            return
        config_qty = Decimal(str(self.ensemble_config.order_quantity))
        lifecycle.risk_target_usdt = float(config_qty * Decimal(str(lifecycle.r_price)))
        policy.risk_target_usdt = lifecycle.risk_target_usdt

    def _d4_finalize_submission(
        self, d4_entry: Any, d4_due: list[Any], action: str, candle: Any
    ) -> None:
        """Record submission / cancellation of the bar's due D4 entries."""
        policy = self._d4_lifecycle
        if policy is None:
            return
        bar_ns = int(candle.end_ns)
        available_ns = int(candle.available_ns)
        submitted = action.startswith(
            ("SUBMITTED", "SIZED_TO_MIN_NOTIONAL+SUBMITTED", "TWAP")
        )
        for lifecycle in d4_due:
            if lifecycle is d4_entry and submitted:
                policy.mark_submitted(
                    lifecycle, bar_ns=bar_ns, available_ns=available_ns, order_ref=None
                )
                policy.record_admission(
                    lifecycle,
                    decision="ACCEPTED",
                    reason="D4_ARMED_ENTRY_SUBMITTED",
                    bar_ns=bar_ns,
                    available_ns=available_ns,
                    action=action,
                )
                continue
            reason = action if lifecycle is d4_entry else "SLOT_NOT_AVAILABLE"
            if lifecycle is d4_entry and reason == "NO_ACTION":
                reason = "NO_EXECUTABLE_ORDER_PATH"
            policy.cancel(
                lifecycle, bar_ns=bar_ns, available_ns=available_ns, reason=reason
            )
            policy.record_admission(
                lifecycle,
                decision="REJECTED",
                reason=reason,
                bar_ns=bar_ns,
                available_ns=available_ns,
                action=action,
            )

    def research_lifecycle_records(self) -> dict[str, Any] | None:
        """Expose the D4 ledger to the research runner (None when disabled)."""
        policy = self._d4_lifecycle
        if policy is None:
            return None
        return {
            "policy": self.ensemble_config.research_opportunity_lifecycle,
            "transitions": policy.transitions,
            "admissions": policy.admissions,
            "lifecycles": policy.snapshot(),
            "state_counts": policy.state_counts(),
        }

    def _audit(self, event_type: str, event_time_ns: int, *, evidence: str, **fields: Any) -> None:
        """Append one forensic event. Never raises into the replay path."""
        recorder = self.audit_recorder
        if recorder is None:
            return
        from v8_next.audit.forensic import EvidenceClass

        try:
            recorder.record(
                event_type,
                event_time_ns=int(event_time_ns),
                source_component="ExpertEnsembleStrategy",
                evidence_class=EvidenceClass(evidence),
                instrument_id=str(self.instrument_id),
                **fields,
            )
        except Exception:  # pragma: no cover - observation must not break trading
            return

    def on_order_event(self, event: Any) -> None:
        """Track native order state transitions."""
        record: dict[str, Any] = {
            "event_type": type(event).__name__,
            "client_order_id": str(getattr(event, "client_order_id", "")),
            "instrument_id": str(getattr(event, "instrument_id", "")),
            "event_ns": getattr(event, "ts_event", 0),
            "received_ns": getattr(event, "ts_init", 0),
            "order_type": str(getattr(event, "order_type", "")),
            "venue_order_id": str(getattr(event, "venue_order_id", "")),
            "position_id": str(getattr(event, "position_id", "")),
            "side": str(getattr(event, "side", "")),
            "price": str(getattr(event, "price", "")),
            "quantity": str(getattr(event, "quantity", "")),
            "filled_qty": str(getattr(event, "filled_qty", "")),
            "reason": str(getattr(event, "reason", "")),
        }
        # Native OrderSubmitted/OrderAccepted events do not carry the order's
        # economic attributes. Read them from the cache so the ledger retains
        # side/type/quantity/price instead of empty strings.
        order = None
        try:
            order = self.cache.order(event.client_order_id)
        except Exception:
            order = None
        if order is not None:
            venue = getattr(order, "venue_order_id", None)
            record["order_type"] = record["order_type"] or str(getattr(order, "type", ""))
            record["venue_order_id"] = record["venue_order_id"] or (
                str(venue) if venue is not None else ""
            )
            record["side"] = record["side"] or str(getattr(order, "side", ""))
            record["quantity"] = record["quantity"] or str(getattr(order, "quantity", ""))
            record["filled_qty"] = record["filled_qty"] or str(getattr(order, "filled_qty", ""))
            order_price = getattr(order, "price", None)
            trigger_price = getattr(order, "trigger_price", None)
            if not record["price"]:
                if order_price is not None:
                    record["price"] = str(order_price)
                elif trigger_price is not None:
                    record["price"] = str(trigger_price)
        self.order_events.append(record)
        from v8_next.audit.forensic import ORDER_EVENT_TYPES

        mapped = ORDER_EVENT_TYPES.get(record["event_type"])
        if mapped is not None:
            self._audit(
                mapped,
                int(record["event_ns"] or 0),
                evidence="OBSERVED_SIMULATION",
                client_order_id=record["client_order_id"],
                venue_order_id=record["venue_order_id"],
                position_id=record["position_id"] or None,
                order_type=record["order_type"],
                side=record["side"],
                price=record["price"],
                quantity=record["quantity"],
                filled_qty=record["filled_qty"],
                reason=record["reason"],
                native_event_class=record["event_type"],
            )
            # Bracket children are the only orders whose client id ends in -2
            # (stop) or -3 (target); emit explicit protection lifecycle events so
            # G7 is proven from the ledger, not inferred from fill tags.
            client_id = record["client_order_id"]
            order_type = record["order_type"].upper()
            reduce_only = bool(getattr(order, "is_reduce_only", False)) if order is not None else False
            is_stop = "STOP" in order_type
            is_target = not is_stop and "LIMIT" in order_type and (reduce_only or order is not None)
            if is_stop or is_target:
                base = "PROTECTIVE_STOP" if is_stop else "TARGET"
                if mapped == "ORDER_ACCEPTED":
                    protection_event = f"{base}_CREATED"
                elif mapped in {"ORDER_CANCELED", "ORDER_FILLED"}:
                    protection_event = f"{base}_CANCELED"
                else:
                    protection_event = None
                if protection_event is not None:
                    self._audit(
                        protection_event,
                        int(record["event_ns"] or 0),
                        evidence="OBSERVED_SIMULATION",
                        client_order_id=client_id,
                        venue_order_id=record["venue_order_id"] or None,
                        order_type=record["order_type"] or None,
                        side=record["side"] or None,
                        price=record["price"] or None,
                        reason=record["reason"] or (
                            "FILLED" if mapped == "ORDER_FILLED" else None
                        ),
                        native_event_class=record["event_type"],
                    )

    def on_position_opened(self, event: Any) -> None:
        """Track newly opened native position."""
        record = {
            "position_id": str(getattr(event, "position_id", "")),
            "instrument_id": str(getattr(event, "instrument_id", "")),
            "side": str(getattr(event, "side", "")),
            "quantity": str(getattr(event, "quantity", "")),
            "avg_px_open": str(getattr(event, "avg_px_open", "")),
            "event_ns": getattr(event, "ts_event", 0),
        }
        self.opened_positions.append(record)
        if self._d4_lifecycle is not None and record["position_id"]:
            side = str(record.get("side") or "").upper()
            direction = "LONG" if side.endswith("LONG") else "SHORT"
            pending = self._d4_lifecycle.by_direction(direction)
            if pending:
                self._d4_lifecycle.mark_entered(
                    pending[0],
                    bar_ns=int(record["event_ns"] or 0),
                    available_ns=int(record["event_ns"] or 0),
                    position_id=record["position_id"],
                )
        self._audit(
            "POSITION_OPENED",
            int(record["event_ns"] or 0),
            evidence="OBSERVED_SIMULATION",
            position_id=record["position_id"],
            side=record["side"],
            quantity=record["quantity"],
            avg_px_open=record["avg_px_open"],
            linkage_note="decision_id resolved by audit pipeline from entry fill timestamp",
        )

    def on_position_closed(self, event: Any) -> None:
        """Track closed native position and realized PnL."""
        record = {
            "position_id": str(getattr(event, "position_id", "")),
            "instrument_id": str(getattr(event, "instrument_id", "")),
            "realized_pnl": str(getattr(event, "realized_pnl", "")),
            "realized_return": str(getattr(event, "realized_return", "")),
            "avg_px_close": str(getattr(event, "avg_px_close", "") or ""),
            "event_ns": getattr(event, "ts_event", 0),
        }
        self.closed_positions.append(record)
        self._audit(
            "POSITION_CLOSED",
            int(record["event_ns"] or 0),
            evidence="OBSERVED_SIMULATION",
            position_id=record["position_id"],
            realized_pnl=record["realized_pnl"],
            realized_return=record["realized_return"],
            avg_px_close=record["avg_px_close"],
        )


def run_expert_strategy_backtest(
    candles: tuple[Candle, ...],
    config: ExpertStrategyConfig | None = None,
    *,
    maker_fee: Decimal = Decimal("0.0002"),
    taker_fee: Decimal = Decimal("0.0005"),
    initial_balance: Decimal = Decimal("10000"),
    venue: str = "BINANCE",
    currency: str = "USDT",
    readings: tuple[PositioningReading, ...] = (),
    fill_model: Any = None,
    latency_model: Any = None,
    fee_model: Any = None,
    execution_profile: str | ExecutionProfile | None = None,
    audit_recorder: Any | None = None,
) -> dict[str, Any]:
    """Execute the 28-expert ensemble strategy through NautilusTrader's BacktestEngine.

    Constructs a qualified simulation environment, feeds causal bars, dispatches
    to the Strategy adapter, and returns economic accounting and witness trace ledgers.
    """
    cfg = config or ExpertStrategyConfig()
    curr = Currency.from_str(currency)
    ven = Venue(venue)
    instrument_id = InstrumentId.from_str(cfg.instrument_id)
    bar_type = BarType.from_str(cfg.bar_type_str)

    # 1. Construct CryptoPerpetual instrument
    instrument = CryptoPerpetual(
        instrument_id=instrument_id,
        raw_symbol=Symbol(cfg.instrument_id.split(".")[0].replace("-PERP", "")),
        base_currency=Currency.from_str("BTC"),
        quote_currency=curr,
        settlement_currency=curr,
        is_inverse=False,
        price_precision=2,
        size_precision=3,
        price_increment=Price(0.01, 2),
        size_increment=Quantity(0.001, 3),
        min_quantity=Quantity(0.001, 3),
        max_quantity=Quantity(100.0, 3),
        min_notional=Money(1, curr),
        ts_event=0,
        ts_init=0,
        margin_init=Decimal("1"),
        margin_maint=Decimal("0.05"),
        maker_fee=maker_fee,
        taker_fee=taker_fee,
    )

    # 2. Build Nautilus Bars
    bars = [
        Bar(
            bar_type,
            Price(float(c.open), 2),
            Price(float(c.high), 2),
            Price(float(c.low), 2),
            Price(float(c.close), 2),
            Quantity(float(c.volume), 3),
            c.end_ns,
            c.end_ns,
        )
        for c in candles
    ]

    # Execution semantics: an explicit profile supplies the fill/fee/latency
    # models plus the liquidity-consumption and queue-position knobs. Explicit
    # model arguments still win over the profile, so existing callers that pass
    # their own models keep behaving exactly as before.
    profile = resolve_profile(execution_profile) if execution_profile is not None else None
    if profile is not None:
        venue_exec: dict[str, Any] = dict(venue_kwargs(profile))
        if fill_model is not None:
            venue_exec["fill_model"] = fill_model
        if latency_model is not None:
            venue_exec["latency_model"] = latency_model
        if fee_model is not None:
            venue_exec["fee_model"] = fee_model
    else:
        venue_exec = {
            "fill_model": fill_model,
            "latency_model": latency_model,
            "fee_model": fee_model,
        }

    # 3. Instantiate Engine and Strategy
    engine = BacktestEngine(
        BacktestEngineConfig(
            bypass_logging=True,
            logging=LoggerConfig(stdout_level=LogLevel.WARNING),
        )
    )
    try:
        engine.add_venue(
            ven,
            OmsType.NETTING,
            AccountType.MARGIN,
            [Money(float(initial_balance), curr)],
            default_leverage=Decimal(1),
            **venue_exec,
        )
        engine.add_instrument(instrument)
        engine.add_data(bars)

        source_map = {c.end_ns: c for c in candles}
        strategy = ExpertEnsembleStrategy(
            cfg,
            source_candles=source_map,
            readings=readings,
            audit_recorder=audit_recorder,
        )
        engine.add_strategy(strategy)

        # 4. Run Backtest
        engine.run()

        # 5. Extract Economic State & Diagnostics
        account_state = economic_state(engine, ven, curr)
        from v8_next.audit.native_telemetry import extract_native_reports

        native_reports = extract_native_reports(engine)

        # Measured execution evidence. When no profile was declared, no
        # execution semantics are claimed: only the facts that do not depend on
        # the model (fill count, fill identity) are reported.
        fill_records, fill_report_type = native_fill_records(engine)
        if profile is not None:
            execution_block = execution_telemetry(
                profile,
                fill_records,
                fill_report_type,
                strategy.opened_positions,
                [{**d, "instrument_id": str(instrument_id)} for d in strategy.decisions],
            )
        else:
            execution_block = {
                "profile": None,
                "evidence_class": "UNSPECIFIED_EXECUTION_SEMANTICS",
                "fills_count": len(fill_records),
                "fills_report_type": fill_report_type,
                "fill_signature": fill_signature(fill_records),
            }
        return {
            "strategy_id": str(strategy.strategy_id),
            "instrument_id": str(instrument_id),
            "total_bars": len(bars),
            "decisions_count": len(strategy.decisions),
            "decisions": strategy.decisions,
            "order_events": strategy.order_events,
            "opened_positions": strategy.opened_positions,
            "closed_positions": strategy.closed_positions,
            "account": account_state,
            "execution": execution_block,
            # The raw fill rows carry the maker/taker split (liquidity_side) and
            # the commission per fill, which an order-type claim has to be able to
            # cite. Reporting only a count and a signature made the F3 acceptance
            # (maker fill ratio + rebate effect) unmeasurable from this path.
            "fill_records": fill_records,
            "fill_report_type": fill_report_type,
            "native_reports": native_reports,
            "opportunity_book": strategy.opportunity_book,
            "research_lifecycle": strategy.research_lifecycle_records(),
        }
    finally:
        engine.dispose()
