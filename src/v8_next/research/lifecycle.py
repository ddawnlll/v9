"""Research-only D4 persistent opportunity lifecycle.

``D4_RETEST_RECLAIM_LIFECYCLE`` is a *causal state machine* layered on an
already detected opportunity: the original breakout must be retested and then
reclaimed on a strictly later closed bar before any entry may be submitted.
It is deliberately not a renamed confirmation gate: it carries per-opportunity
state across bars, waits for two distinct subsequently completed bars, and has
its own terminal outcomes (INVALIDATED / EXPIRED / CANCELLED).

The module holds no engine, no order routing and no economics of its own. It
runs over bar data that is already causally available to the caller, so the
native strategy can consume it directly and the screening path can reuse the
exact same transitions. Default OFF: nothing here executes unless a research
configuration selects the policy by name.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

POLICY_ID = "D4_RETEST_RECLAIM_LIFECYCLE"

#: Reclaim threshold beyond the reference level, in units of the original risk.
RECLAIM_R = 0.1
#: Close-breach invalidation threshold beyond the reference level, in R.
INVALIDATION_R = 0.5
#: Maximum subsequent completed bars allowed between detection and ARMED.
TTL_BARS = 6

#: Declared submission timing: the order is created on the first completed bar
#: strictly after the ARMED bar and is filled by the engine's existing
#: same-close MARKET assumption on that later bar. The bar-only tape cannot
#: prove any earlier executable event.
SUBMISSION_RULE = "MARKET_ON_FIRST_COMPLETED_BAR_AFTER_ARMED"

TERMINAL_STATES = ("INVALIDATED", "EXPIRED", "CANCELLED")
ACTIVE_STATES = ("WATCHING", "RETEST_SEEN", "ARMED")


@dataclass
class Lifecycle:
    """One opportunity's persistent state."""

    opportunity_id: str
    direction: str
    reference_price: float
    stop_price: float
    target_price: float
    r_price: float
    risk_target_usdt: float
    detected_ns: int
    available_ns: int
    detected_bar_ns: int
    expert_supported: bool
    state: str = "WATCHING"
    retest_bar_ns: int | None = None
    armed_bar_ns: int | None = None
    bars_waited: int = 0
    submitted: bool = False
    terminal_reason: str | None = None

    @property
    def is_long(self) -> bool:
        return str(self.direction).upper().endswith("LONG")

    def to_dict(self) -> dict[str, Any]:
        return {
            "opportunity_id": self.opportunity_id,
            "direction": self.direction,
            "reference_price": self.reference_price,
            "stop_price": self.stop_price,
            "target_price": self.target_price,
            "r_price": self.r_price,
            "risk_target_usdt": self.risk_target_usdt,
            "detected_ns": self.detected_ns,
            "available_ns": self.available_ns,
            "detected_bar_ns": self.detected_bar_ns,
            "expert_supported": self.expert_supported,
            "state": self.state,
            "retest_bar_ns": self.retest_bar_ns,
            "armed_bar_ns": self.armed_bar_ns,
            "bars_waited": self.bars_waited,
            "submitted": self.submitted,
            "terminal_reason": self.terminal_reason,
        }


class RetestReclaimLifecycle:
    """Causal D4 state machine over already-closed bars."""

    def __init__(self, *, risk_target_usdt: float = 0.0) -> None:
        self.risk_target_usdt = float(risk_target_usdt)
        self.lifecycles: dict[str, Lifecycle] = {}
        self.transitions: list[dict[str, Any]] = []
        self.admissions: list[dict[str, Any]] = []

    # -- transition ledger -------------------------------------------------
    def _transition(
        self,
        lifecycle: Lifecycle,
        previous: str,
        new: str,
        *,
        trigger: str,
        event_time_ns: int,
        available_ns: int,
        source_bar_ns: int,
        order_id: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        row = {
            "policy": POLICY_ID,
            "opportunity_id": lifecycle.opportunity_id,
            "previous_state": previous,
            "new_state": new,
            "event_time_ns": int(event_time_ns),
            "market_data_available_ns": int(available_ns),
            "trigger": trigger,
            "source_bar_ns": int(source_bar_ns),
            "direction": lifecycle.direction,
            "reference_price": lifecycle.reference_price,
            "stop_price": lifecycle.stop_price,
            "target_price": lifecycle.target_price,
            "r_price": lifecycle.r_price,
            "bars_waited": lifecycle.bars_waited,
            "related_order_id": order_id,
            "detail": detail or {},
        }
        self.transitions.append(row)
        return row

    def record_admission(
        self,
        lifecycle: Lifecycle,
        *,
        decision: str,
        reason: str,
        bar_ns: int,
        available_ns: int,
        action: str | None = None,
    ) -> dict[str, Any]:
        row = {
            "policy": POLICY_ID,
            "opportunity_id": lifecycle.opportunity_id,
            "bar_ns": int(bar_ns),
            "market_data_available_ns": int(available_ns),
            "decision": decision,
            "reason": reason,
            "action": action,
            "state": lifecycle.state,
            "direction": lifecycle.direction,
            "reference_price": lifecycle.reference_price,
            "armed_bar_ns": lifecycle.armed_bar_ns,
            "bars_waited": lifecycle.bars_waited,
        }
        self.admissions.append(row)
        return row

    # -- state machine -----------------------------------------------------
    def detect(
        self,
        *,
        opportunity_id: str,
        direction: str,
        reference_price: float,
        stop_price: float,
        target_price: float,
        detected_ns: int,
        available_ns: int,
        bar_ns: int,
        expert_supported: bool,
    ) -> Lifecycle | None:
        """Register a newly detected opportunity in WATCHING (no entry)."""
        if opportunity_id in self.lifecycles:
            return None
        r_price = abs(float(reference_price) - float(stop_price))
        if r_price <= 0:
            return None
        lifecycle = Lifecycle(
            opportunity_id=opportunity_id,
            direction=str(direction),
            reference_price=float(reference_price),
            stop_price=float(stop_price),
            target_price=float(target_price),
            r_price=r_price,
            risk_target_usdt=self.risk_target_usdt,
            detected_ns=int(detected_ns),
            available_ns=int(available_ns),
            detected_bar_ns=int(bar_ns),
            expert_supported=bool(expert_supported),
            state="DETECTED",
        )
        self.lifecycles[opportunity_id] = lifecycle
        self._transition(
            lifecycle,
            "DETECTED",
            "WATCHING",
            trigger="OPPORTUNITY_DETECTED",
            event_time_ns=detected_ns,
            available_ns=available_ns,
            source_bar_ns=bar_ns,
            detail={"expert_supported": bool(expert_supported)},
        )
        lifecycle.state = "WATCHING"
        return lifecycle

    def advance(
        self,
        *,
        bar_ns: int,
        available_ns: int,
        high: float,
        low: float,
        close: float,
    ) -> list[Lifecycle]:
        """Advance every active lifecycle on a newly available completed bar.

        Invalidation is evaluated before retest/reclaim on every bar. A retest
        and a reclaim can never happen on the same bar: ARMED requires a bar
        strictly later than the retest bar.
        """
        armed_now: list[Lifecycle] = []
        for lifecycle in self.lifecycles.values():
            if lifecycle.state not in ("WATCHING", "RETEST_SEEN"):
                continue
            lifecycle.bars_waited += 1
            long_ = lifecycle.is_long
            ref = lifecycle.reference_price
            r = lifecycle.r_price

            breached = (close < ref - INVALIDATION_R * r) if long_ else (
                close > ref + INVALIDATION_R * r
            )
            stop_touched = (low <= lifecycle.stop_price) if long_ else (
                high >= lifecycle.stop_price
            )
            if breached or stop_touched:
                trigger = (
                    "CLOSE_BREACH_INVALIDATION_BAND" if breached else "INITIAL_STOP_TOUCHED"
                )
                self._terminal(lifecycle, "INVALIDATED", trigger, bar_ns, available_ns, close)
                continue

            if lifecycle.state == "WATCHING":
                touched = (low <= ref) if long_ else (high >= ref)
                if touched:
                    previous = lifecycle.state
                    lifecycle.state = "RETEST_SEEN"
                    lifecycle.retest_bar_ns = int(bar_ns)
                    self._transition(
                        lifecycle,
                        previous,
                        "RETEST_SEEN",
                        trigger="RETEST_TOUCHED_REFERENCE_LEVEL",
                        event_time_ns=bar_ns,
                        available_ns=available_ns,
                        source_bar_ns=bar_ns,
                        detail={"low": low, "high": high, "reference_price": ref},
                    )
            elif lifecycle.state == "RETEST_SEEN" and lifecycle.retest_bar_ns is not None:
                if bar_ns > lifecycle.retest_bar_ns:
                    reclaimed = (close >= ref + RECLAIM_R * r) if long_ else (
                        close <= ref - RECLAIM_R * r
                    )
                    if reclaimed:
                        previous = lifecycle.state
                        lifecycle.state = "ARMED"
                        lifecycle.armed_bar_ns = int(bar_ns)
                        self._transition(
                            lifecycle,
                            previous,
                            "ARMED",
                            trigger="RECLAIM_THRESHOLD_CLOSED_THROUGH",
                            event_time_ns=bar_ns,
                            available_ns=available_ns,
                            source_bar_ns=bar_ns,
                            detail={"close": close, "reclaim_threshold": (
                                ref + RECLAIM_R * r if long_ else ref - RECLAIM_R * r
                            )},
                        )
                        armed_now.append(lifecycle)
                        continue

            if lifecycle.state in ("WATCHING", "RETEST_SEEN") and lifecycle.bars_waited >= TTL_BARS:
                self._terminal(lifecycle, "EXPIRED", "TTL_BARS_ELAPSED", bar_ns, available_ns, close)
        return armed_now

    def _terminal(
        self,
        lifecycle: Lifecycle,
        state: str,
        trigger: str,
        bar_ns: int,
        available_ns: int,
        close: float,
        *,
        order_id: str | None = None,
    ) -> None:
        previous = lifecycle.state
        lifecycle.state = state
        lifecycle.terminal_reason = trigger
        self._transition(
            lifecycle,
            previous,
            state,
            trigger=trigger,
            event_time_ns=bar_ns,
            available_ns=available_ns,
            source_bar_ns=bar_ns,
            order_id=order_id,
            detail={"close": close},
        )

    # -- admission ---------------------------------------------------------
    def due_entries(self, bar_ns: int) -> list[Lifecycle]:
        """ARMED lifecycles that became causally available on an earlier bar."""
        due = [
            lifecycle
            for lifecycle in self.lifecycles.values()
            if lifecycle.state == "ARMED"
            and not lifecycle.submitted
            and lifecycle.armed_bar_ns is not None
            and lifecycle.armed_bar_ns < int(bar_ns)
        ]
        due.sort(key=lambda lc: (lc.armed_bar_ns or 0, lc.opportunity_id))
        return due

    def cancel(self, lifecycle: Lifecycle, *, bar_ns: int, available_ns: int, reason: str) -> None:
        self._terminal(
            lifecycle,
            "CANCELLED",
            reason,
            bar_ns,
            available_ns,
            float("nan"),
        )
        lifecycle.submitted = False

    def mark_submitted(self, lifecycle: Lifecycle, *, bar_ns: int, available_ns: int, order_ref: str | None = None) -> None:
        previous = lifecycle.state
        lifecycle.state = "ENTRY_PENDING"
        lifecycle.submitted = True
        self._transition(
            lifecycle,
            previous,
            "ENTRY_PENDING",
            trigger=SUBMISSION_RULE,
            event_time_ns=bar_ns,
            available_ns=available_ns,
            source_bar_ns=bar_ns,
            order_id=order_ref,
        )

    def mark_entered(self, lifecycle: Lifecycle, *, bar_ns: int, available_ns: int, position_id: str | None = None) -> None:
        if lifecycle.state != "ENTRY_PENDING":
            return
        previous = lifecycle.state
        lifecycle.state = "ENTERED"
        self._transition(
            lifecycle,
            previous,
            "ENTERED",
            trigger="NATIVE_POSITION_OPENED",
            event_time_ns=bar_ns,
            available_ns=available_ns,
            source_bar_ns=bar_ns,
            order_id=position_id,
        )

    def by_direction(self, direction: str) -> list[Lifecycle]:
        want = str(direction).upper().endswith("LONG")
        return [
            lifecycle
            for lifecycle in self.lifecycles.values()
            if lifecycle.is_long == want and lifecycle.state == "ENTRY_PENDING"
        ]

    def state_counts(self) -> dict[str, int]:
        counts = {
            "WATCHING": 0,
            "RETEST_SEEN": 0,
            "ARMED": 0,
            "ENTRY_PENDING": 0,
            "ENTERED": 0,
            "INVALIDATED": 0,
            "EXPIRED": 0,
            "CANCELLED": 0,
        }
        for lifecycle in self.lifecycles.values():
            counts[lifecycle.state] = counts.get(lifecycle.state, 0) + 1
        counts["ARMED_LONG"] = sum(
            1 for lc in self.lifecycles.values() if lc.armed_bar_ns and lc.is_long
        )
        counts["ARMED_SHORT"] = sum(
            1 for lc in self.lifecycles.values() if lc.armed_bar_ns and not lc.is_long
        )
        return counts

    def snapshot(self) -> list[dict[str, Any]]:
        return [lc.to_dict() for lc in self.lifecycles.values()]
