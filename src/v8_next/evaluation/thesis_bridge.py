"""Simulation-only, advisory-only bridge for native position evidence.

The caller supplies observed native receipts; this module cannot verify their
source, query a venue, persist them, or submit orders. Integration must establish
receipt authenticity and event ordering independently before invoking it.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from v8_next.domain.decision import VenueOrderStatus
from v8_next.domain.protection import ProtectionState
from v8_next.economics.legality import PositionPresence
from v8_next.economics.management import ManagementDecision, evaluate_management
from v8_next.economics.thesis import (
    ExpertEvidence,
    InvalidationRule,
    PositionEvaluation,
    PositionSnapshot,
    PositionThesis,
    StopKind,
    TargetPlan,
    ThesisBelief,
    build_thesis,
    evaluate_position,
)


@dataclass(frozen=True)
class NativeOrderEvidence:
    order_id: str
    position_id: str
    instrument_id: str
    side: str
    quantity: Decimal
    price: Decimal
    status: VenueOrderStatus
    reduce_only: bool
    observed_ns: int


@dataclass(frozen=True)
class EntryEvidence:
    """A caller-bound event bundle, retaining the exact serialized decision evidence."""

    decision_id: str
    opportunity_id: str
    instrument_id: str
    side: str
    decision_ns: int
    decision_json: str
    belief: ThesisBelief
    experts: tuple[ExpertEvidence, ...]
    evidence_available_ns: int
    policy_version: str
    fill_id: str
    entry_order_filled: bool
    entry_fill_count: int
    settlement_currency: str
    contract_type: str
    contract_multiplier: Decimal
    entry_order_id: str
    position_id: str
    fill_quantity: Decimal
    fill_price: Decimal
    fill_ns: int
    stop: NativeOrderEvidence
    target: NativeOrderEvidence
    simulation_only: bool


@dataclass(frozen=True)
class ThesisEntryReceipt:
    thesis: PositionThesis
    entry: EntryEvidence
    evidence_sha256: str
    advisory_only: bool = True


def _finite_positive(value: Decimal) -> bool:
    return isinstance(value, Decimal) and value.is_finite() and value > 0


def _order_valid(order: NativeOrderEvidence, entry: EntryEvidence, as_of_ns: int) -> bool:
    return (
        bool(order.order_id.strip())
        and order.position_id == entry.position_id
        and order.instrument_id == entry.instrument_id
        and order.side == ("SHORT" if entry.side == "LONG" else "LONG")
        and order.status is VenueOrderStatus.ACCEPTED
        and order.reduce_only is True
        and _finite_positive(order.quantity)
        and _finite_positive(order.price)
        and order.quantity == entry.fill_quantity
        and entry.fill_ns <= order.observed_ns <= as_of_ns
    )


def capture_entry(
    entry: EntryEvidence,
    *,
    as_of_ns: int,
    expected_bars: int,
    max_bars: int,
    invalidation_rules: tuple[InvalidationRule, ...],
    max_order_age_ns: int,
    max_input_age_ns: int,
) -> ThesisEntryReceipt:
    """Freeze actual fill and acknowledged stop/target; never creates an order."""
    if entry.simulation_only is not True:
        raise ValueError("simulation-only evidence required")
    if any(not value.strip() for value in (
        entry.decision_id, entry.opportunity_id, entry.instrument_id, entry.policy_version,
        entry.fill_id, entry.entry_order_id, entry.position_id,
    )):
        raise ValueError("missing entry identity")
    if (entry.entry_order_filled is not True or entry.entry_fill_count != 1
            or entry.settlement_currency != "USDT"
            or entry.contract_type != "LINEAR"):
        raise ValueError("completed linear USDT fill required")
    if not _finite_positive(entry.contract_multiplier):
        raise ValueError("contract multiplier required")
    if max_order_age_ns < 0 or max_input_age_ns < 0:
        raise ValueError("invalid freshness limit")
    if entry.side not in {"LONG", "SHORT"}:
        raise ValueError("unknown position side")
    if not (_finite_positive(entry.fill_quantity) and _finite_positive(entry.fill_price)):
        raise ValueError("invalid fill")
    if not (0 <= entry.belief.as_of_ns <= entry.decision_ns <= entry.fill_ns <= as_of_ns):
        raise ValueError("future or unordered entry evidence")
    if not (0 <= entry.evidence_available_ns <= entry.decision_ns):
        raise ValueError("future expert evidence")
    if (entry.decision_ns - entry.belief.as_of_ns > max_input_age_ns
            or entry.decision_ns - entry.evidence_available_ns > max_input_age_ns):
        raise ValueError("stale entry inputs")
    if entry.belief.stale or entry.belief.abstain:
        raise ValueError("entry belief unavailable")
    if not entry.experts or len({expert.expert_id for expert in entry.experts}) != len(entry.experts):
        raise ValueError("missing or duplicate expert evidence")
    try:
        decision: Any = json.loads(entry.decision_json)
    except (ValueError, TypeError) as exc:
        raise ValueError("decision evidence is not JSON") from exc
    if not isinstance(decision, dict) or any(
        decision.get(key) != value for key, value in (
            ("decision_id", entry.decision_id), ("opportunity_id", entry.opportunity_id),
            ("belief_id", entry.belief.belief_id), ("action", "ENTER"),
            ("instrument_id", entry.instrument_id),
        )
    ):
        raise ValueError("decision evidence identity mismatch")
    if not _order_valid(entry.stop, entry, as_of_ns) or not _order_valid(entry.target, entry, as_of_ns):
        raise ValueError("unverified native protection")
    if any(as_of_ns - item.observed_ns > max_order_age_ns for item in (entry.stop, entry.target)):
        raise ValueError("stale native protection")
    if len({entry.entry_order_id, entry.stop.order_id, entry.target.order_id}) != 3:
        raise ValueError("order identities must be distinct")
    if entry.side == "LONG":
        geometry_ok = entry.stop.price < entry.fill_price < entry.target.price
    else:
        geometry_ok = entry.target.price < entry.fill_price < entry.stop.price
    if not geometry_ok:
        raise ValueError("invalid stop/target geometry")
    thesis = build_thesis(
        entry_decision_id=entry.decision_id, original_opportunity_id=entry.opportunity_id,
        original_market_belief_id=entry.belief.belief_id, native_position_id=entry.position_id,
        instrument_id=entry.instrument_id, side=entry.side, entry_price=entry.fill_price,
        initial_stop_price=entry.stop.price, initial_stop_kind=StopKind.NATIVE_PROTECTIVE,
        original_risk_usdt=(abs(entry.fill_price - entry.stop.price) * entry.fill_quantity
                            * entry.contract_multiplier),
        target_plan=TargetPlan(target_price=entry.target.price, expected_bars=expected_bars, max_bars=max_bars),
        invalidation_rules=invalidation_rules, entry_regime=entry.belief.regime,
        policy_version=entry.policy_version, created_ns=as_of_ns,
    )
    # JSON stores exact input bytes in the receipt; hash binds the full immutable
    # bundle, including expert weights and native observation times.
    from dataclasses import asdict

    digest = hashlib.sha256(json.dumps(asdict(entry), sort_keys=True, default=str,
                                       separators=(",", ":")).encode()).hexdigest()
    return ThesisEntryReceipt(thesis=thesis, entry=entry, evidence_sha256=digest)


@dataclass(frozen=True)
class CurrentEvidence:
    snapshot: PositionSnapshot
    instrument_id: str
    side: str
    stop: NativeOrderEvidence | None
    venue_status: VenueOrderStatus
    observed_ns: int
    max_age_ns: int
    simulation_only: bool


def evaluate_snapshot(
    receipt: ThesisEntryReceipt,
    current: CurrentEvidence,
    belief: ThesisBelief | None,
    *,
    now_ns: int,
    evidence: tuple[ExpertEvidence, ...],
    evidence_available_ns: int,
    fee_bps: Decimal,
    lot_step: Decimal,
    tick_size: Decimal,
) -> tuple[PositionEvaluation, ManagementDecision]:
    """Evaluate verified snapshot projections and return advisory-only decisions."""
    thesis, snapshot = receipt.thesis, current.snapshot
    if current.simulation_only is not True or current.instrument_id != thesis.instrument_id:
        raise ValueError("unverified simulation or instrument identity")
    if snapshot.native_position_id != thesis.native_position_id or current.side != thesis.side:
        raise ValueError("position identity mismatch")
    if current.max_age_ns < 0 or not (0 <= current.observed_ns <= now_ns):
        raise ValueError("invalid snapshot timing")
    if snapshot.quantity != 0 and (
        snapshot.average_price != thesis.entry_price or abs(snapshot.quantity) > receipt.entry.fill_quantity
    ):
        raise ValueError("scale-in or changed entry basis is unsupported")
    if snapshot.available_at_ns > current.observed_ns or not (0 <= evidence_available_ns <= now_ns):
        raise ValueError("future snapshot or expert evidence")
    stale = (
        now_ns - current.observed_ns > current.max_age_ns
        or now_ns - evidence_available_ns > current.max_age_ns
        or (belief is not None and now_ns - belief.as_of_ns > current.max_age_ns)
    )
    stop = current.stop
    valid_stop = bool(
        stop is not None and bool(stop.order_id.strip())
        and current.venue_status is VenueOrderStatus.ACCEPTED
        and stop.position_id == thesis.native_position_id
        and stop.instrument_id == thesis.instrument_id
        and stop.side == ("SHORT" if thesis.side == "LONG" else "LONG")
        and stop.status is VenueOrderStatus.ACCEPTED and stop.reduce_only is True
        and stop.order_id in snapshot.protective_order_ids
        and _finite_positive(stop.quantity) and stop.quantity == abs(snapshot.quantity)
        and _finite_positive(stop.price) and 0 <= stop.observed_ns <= current.observed_ns
        and now_ns - stop.observed_ns <= current.max_age_ns
        and (stop.price >= thesis.initial_stop_price if thesis.side == "LONG"
             else stop.price <= thesis.initial_stop_price)
        and not stale
    )
    bound_belief = belief
    if snapshot.quantity != 0 and (
        belief is None or snapshot.latest_belief_id != belief.belief_id
    ):
        bound_belief = None
    position_eval = evaluate_position(thesis, snapshot, bound_belief, now_ns=now_ns, evidence=evidence)
    management = evaluate_management(
        position_eval,
        protection_state=ProtectionState.PROTECTED if valid_stop else ProtectionState.UNKNOWN,
        protective_stop_price=stop.price if valid_stop and stop is not None else None,
        fee_bps=fee_bps, mark_price=snapshot.mark_price, side=thesis.side,
        entry_price=thesis.entry_price, quantity=snapshot.quantity,
        initial_stop_price=thesis.initial_stop_price, instrument_id=thesis.instrument_id,
        lot_step=lot_step, tick_size=tick_size, venue_status=current.venue_status,
        position_presence=PositionPresence.OPEN if snapshot.quantity != 0 else PositionPresence.NONE,
        stale=stale,
    )
    return position_eval, management
