"""NX03 (#424) — historical walk-forward plan, kept apart from the prospective freeze.

``ForwardPlan`` / ``freeze_forward_plan`` / ``bind_forward_data`` are prospective
instruments: they read the real wall clock, require the freeze to happen before the
observation window starts, and bind exactly one immutable data window. Loosening
them so a backward-looking run could use them would let a historical plan be
registered *after* its window was observed — i.e. it would leak the future.

A historical plan is therefore its own typed structure with its own store schema.
It never writes the forward tables, and the forward checks are untouched.

Schema (``v87-historical-plan-v1``):

* ``plan_id`` — caller-declared identity; ``folds`` are ordered and disjoint.
* ``dataset_id`` / ``calendar_digest`` — the NX01 tape identity and the NX01
  calendar the plan was derived from, so a plan cannot be re-pointed at another tape.
* ``history`` — why the warmup/purge numbers are what they are (measured from the
  real grammar/protection constants, never an arbitrary constant).
* ``folds`` — per fold: train window, scored window, warmup window inside the past,
  and the purge that separates them.
* ``aggregation`` — whether folds reset (independent) or are carried as one
  continuous portfolio; the choice is explicit because the two are not comparable.
  With ``CONTINUOUS_PORTFOLIO`` an open position is carried at the fold boundary
  and marked with the fold-end bar close, so the two aggregations are never mixed
  and a fold's returns are never counted twice.
* ``train_window`` — ``EXPANDING_FROM_DATASET_START`` (each fold trains on all
  history before it, including earlier scored folds) or ``ROLLING``. Stated
  explicitly because an expanding window reuses earlier scored periods as training
  data, which a reader must be able to see rather than infer.
* ``final_eligible`` / ``final_eligibility_reason`` — the final window is opened
  only when the NX01 burn table proves it protected. The reason is metadata; it is
  never a ``GateState`` value.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, replace
from typing import Any, Literal

from v8_next.economics.grammar import (
    POLICIES,
    POLICY_HORIZON_BARS,
    POLICY_REQUIRED_BARS,
)
from v8_next.economics.protection import PROTECTION_TTL_BARS
from v8_next.evaluation.store import ResearchStore
from v8_next.evaluation.tape_identity import SwingCalendar, iso_utc

HISTORICAL_PLAN_VERSION = "v87-historical-plan-v1"

Aggregation = Literal["INDEPENDENT_FOLD_RESET", "CONTINUOUS_PORTFOLIO"]
TrainWindow = Literal["EXPANDING_FROM_DATASET_START", "ROLLING"]
FoldRole = Literal["DEVELOPMENT", "DIAGNOSTIC_FOLD", "FINAL"]

#: Bars of a complete previous UTC day that session levels consume
#: (``experts.levels.previous_session_bars`` requires exactly 24 hourly bars).
SESSION_BARS = 24


def canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


@dataclass(frozen=True)
class HistoryRequirements:
    """Measured warmup/purge requirements of a plan's policy set."""

    policies: tuple[str, ...]
    required_bars: dict[str, int]
    horizon_bars: dict[str, int]
    warmup_bars: int
    purge_bars: int
    protection_ttl_bars: int
    session_bars: int
    rationale: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "policies": list(self.policies),
            "required_bars": dict(sorted(self.required_bars.items())),
            "horizon_bars": dict(sorted(self.horizon_bars.items())),
            "warmup_bars": self.warmup_bars,
            "purge_bars": self.purge_bars,
            "protection_ttl_bars": self.protection_ttl_bars,
            "session_bars": self.session_bars,
            "rationale": self.rationale,
        }


def derive_history_requirements(
    policies: tuple[str, ...], *, family: str = "squeeze"
) -> HistoryRequirements:
    """Derive warmup and purge from the real policy/protection contracts.

    * warmup = the largest history any selected grammar actually reads
      (``POLICY_REQUIRED_BARS``) and at least one full previous UTC session;
    * purge  = the longest interval an opened episode can stay unresolved
      (``POLICY_HORIZON_BARS``) or a protection's TTL, whichever is longer.

    Both come from constants the runtime itself uses, so a walk-forward window can
    never be tightened by an arbitrary literal.
    """
    unknown = [p for p in policies if p not in POLICIES]
    if unknown:
        raise ValueError(f"unknown grammar policy in plan: {sorted(unknown)}")
    if not policies:
        raise ValueError("a historical plan needs at least one policy")
    required = {p: POLICY_REQUIRED_BARS[p] for p in policies}
    horizons = {p: POLICY_HORIZON_BARS[p] for p in policies}
    ttl = PROTECTION_TTL_BARS.get(family, PROTECTION_TTL_BARS["_default"])
    warmup = max(max(required.values()), SESSION_BARS)
    purge = max(max(horizons.values()), ttl)
    return HistoryRequirements(
        policies=tuple(policies),
        required_bars=required,
        horizon_bars=horizons,
        warmup_bars=warmup,
        purge_bars=purge,
        protection_ttl_bars=ttl,
        session_bars=SESSION_BARS,
        rationale=(
            "warmup = max(policy required bars, one previous UTC session); "
            "purge = max(policy outcome horizon, protection TTL)"
        ),
    )


@dataclass(frozen=True)
class FoldWindow:
    """One walk-forward fold: what is trained on, what is scored, and the gap.

    Boundaries are nanoseconds since the epoch, the runtime's own convention
    (``ForwardPlan`` and the engine work in ns). The NX01 calendar works in
    milliseconds; ``build_historical_plan`` converts once, at the boundary, so no
    window can be compared across two different units.
    """

    fold_id: str
    role: FoldRole
    train_start_ns: int
    train_end_ns: int  # exclusive
    scored_start_ns: int
    scored_end_ns: int
    warmup_start_ns: int
    warmup_end_ns: int
    purge_bars: int
    tape_role: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "fold_id": self.fold_id,
            "role": self.role,
            "train_start_ns": self.train_start_ns,
            "train_end_ns": self.train_end_ns,
            "train_start_utc": iso_utc(self.train_start_ns // 1_000_000),
            "train_end_utc": iso_utc(self.train_end_ns // 1_000_000),
            "scored_start_ns": self.scored_start_ns,
            "scored_end_ns": self.scored_end_ns,
            "scored_start_utc": iso_utc(self.scored_start_ns // 1_000_000),
            "scored_end_utc": iso_utc(self.scored_end_ns // 1_000_000),
            "warmup_start_ns": self.warmup_start_ns,
            "warmup_start_utc": iso_utc(self.warmup_start_ns // 1_000_000),
            "warmup_end_ns": self.warmup_end_ns,
            "purge_bars": self.purge_bars,
            "tape_role": self.tape_role,
        }


@dataclass(frozen=True)
class HistoricalPlan:
    """A frozen, backward-looking walk-forward plan. Not a claim, not a permit."""

    plan_id: str
    version: str
    dataset_id: str
    calendar_digest: str
    instrument: str
    policies: tuple[str, ...]
    baseline: str
    initial_balance_usdt: str
    maker_fee: str
    taker_fee: str
    bar_ns: int
    history: HistoryRequirements
    aggregation: Aggregation
    train_window: TrainWindow
    folds: tuple[FoldWindow, ...]
    final_eligible: bool
    final_eligibility_reason: str | None
    code_and_lock_hash: str = ""

    def fold(self, fold_id: str) -> FoldWindow:
        for window in self.folds:
            if window.fold_id == fold_id:
                return window
        raise KeyError(fold_id)

    def verify(self) -> tuple[bool, str]:
        """Re-derive every structural invariant from the plan's own fields."""
        if not self.plan_id.strip():
            return False, "PLAN_ID_EMPTY"
        if self.version != HISTORICAL_PLAN_VERSION:
            return False, f"UNSUPPORTED_PLAN_VERSION: {self.version}"
        if self.baseline not in self.policies:
            return False, "BASELINE_NOT_IN_POLICIES"
        if not self.final_eligible and not self.final_eligibility_reason:
            return False, "FINAL_INELIGIBLE_WITHOUT_REASON"
        if self.final_eligible:
            final = self.fold("FINAL")
            if final.tape_role != "PROTECTED_OOS":
                return False, "FINAL_CLAIMED_WITHOUT_PROTECTED_TAPE_ROLE"
        expected_purge = self.history.purge_bars
        previous_end: int | None = None
        for window in self.folds:
            if window.train_end_ns >= window.scored_start_ns:
                return False, f"TRAIN_OVERLAPS_SCORED: {window.fold_id}"
            if window.warmup_end_ns >= window.scored_start_ns:
                return False, f"WARMUP_INSIDE_SCORED: {window.fold_id}"
            if window.warmup_start_ns <= window.train_end_ns:
                return False, f"WARMUP_OUTSIDE_PAST: {window.fold_id}"
            if window.purge_bars != expected_purge:
                return False, f"PURGE_NOT_FROM_MEASURED_CONTRACT: {window.fold_id}"
            if (
                window.scored_start_ns - window.train_end_ns
            ) != window.purge_bars * self.bar_ns:
                return False, f"PURGE_GAP_MISMATCH: {window.fold_id}"
            if previous_end is not None and window.scored_start_ns <= previous_end:
                return False, f"FOLDS_OVERLAP: {window.fold_id}"
            previous_end = window.scored_end_ns
        return True, "OK"

    def digest(self) -> str:
        return "sha256:" + hashlib.sha256(canonical(self.as_dict(include_digest=False)).encode()).hexdigest()

    def as_dict(self, *, include_digest: bool = True) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "plan_id": self.plan_id,
            "version": self.version,
            "dataset_id": self.dataset_id,
            "calendar_digest": self.calendar_digest,
            "instrument": self.instrument,
            "policies": list(self.policies),
            "baseline": self.baseline,
            "initial_balance_usdt": self.initial_balance_usdt,
            "maker_fee": self.maker_fee,
            "taker_fee": self.taker_fee,
            "bar_ns": self.bar_ns,
            "history": self.history.as_dict(),
            "aggregation": self.aggregation,
            "train_window": self.train_window,
            "folds": [f.as_dict() for f in self.folds],
            "final_eligible": self.final_eligible,
            "final_eligibility_reason": self.final_eligibility_reason,
            "code_and_lock_hash": self.code_and_lock_hash,
        }
        if include_digest:
            payload["plan_digest"] = self.digest()
        return payload


def calendar_digest(calendar: SwingCalendar) -> str:
    """Stable identity of the NX01 calendar a plan was derived from."""
    payload = {
        "development": [calendar.development.start_ms, calendar.development.end_ms],
        "folds": [[f.start_ms, f.end_ms] for f in calendar.folds],
        "final": [calendar.final.start_ms, calendar.final.end_ms],
        "final_eligible": calendar.final_eligible,
        "roles": {s.segment_id: s.role for s in calendar.burn_segments},
    }
    return "sha256:" + hashlib.sha256(canonical(payload).encode()).hexdigest()


def _segment_role(calendar: SwingCalendar, start_ms: int, end_ms: int) -> str:
    overlapping = [
        s for s in calendar.burn_segments if s.start_ms <= end_ms and start_ms <= s.end_ms
    ]
    roles = sorted({s.role for s in overlapping})
    return "+".join(roles) if roles else "UNMAPPED"


def build_historical_plan(
    *,
    calendar: SwingCalendar,
    dataset_id: str,
    instrument: str,
    policies: tuple[str, ...],
    baseline: str,
    initial_balance_usdt: str,
    maker_fee: str,
    taker_fee: str,
    bar_ns: int,
    aggregation: Aggregation,
    train_window: TrainWindow = "EXPANDING_FROM_DATASET_START",
    plan_id: str,
    family: str = "squeeze",
    code_and_lock_hash: str = "",
) -> HistoricalPlan:
    """Build the 24/12/12 walk-forward plan from a verified NX01 calendar.

    Warmup is subtracted from every scored window and stays inside the past; the
    training window stops one measured purge before the scored window starts, so
    no outcome interval opened during training can resolve inside the scored
    window. The final window is opened only when the tape role proves it protected.
    """
    if bar_ns <= 0:
        raise ValueError("bar_ns must be positive")
    history = derive_history_requirements(policies, family=family)
    windows: list[FoldWindow] = []

    ns = 1_000_000  # calendar milliseconds -> runtime nanoseconds, once

    def window_for(fold_id: str, role: FoldRole, start_ms: int, end_ms: int) -> FoldWindow:
        start_ns = start_ms * ns
        train_end_ns = start_ns - history.purge_bars * bar_ns
        warmup_start_ns = start_ns - history.warmup_bars * bar_ns
        train_start_ns = calendar.dev_start_ms * ns
        if train_end_ns <= train_start_ns:
            raise ValueError(f"{fold_id}: purge leaves no training window")
        if warmup_start_ns <= train_end_ns:
            raise ValueError(
                f"{fold_id}: warmup overlaps the training window "
                f"(warmup {history.warmup_bars} bars <= purge {history.purge_bars} bars)"
            )
        return FoldWindow(
            fold_id=fold_id,
            role=role,
            train_start_ns=train_start_ns,
            train_end_ns=train_end_ns,  # exclusive: training covers [train_start, train_end)
            scored_start_ns=start_ns,
            scored_end_ns=(end_ms + 1) * ns - 1,
            warmup_start_ns=warmup_start_ns,
            warmup_end_ns=start_ns - 1,
            purge_bars=history.purge_bars,
            tape_role=_segment_role(calendar, start_ms, end_ms),
        )

    for fold in calendar.folds:
        windows.append(
            window_for(fold.fold_id, "DIAGNOSTIC_FOLD", fold.start_ms, fold.end_ms)
        )
    final_role = _segment_role(calendar, calendar.final.start_ms, calendar.final.end_ms)
    if calendar.final_eligible:
        windows.append(
            window_for("FINAL", "FINAL", calendar.final.start_ms, calendar.final.end_ms)
        )

    plan = HistoricalPlan(
        plan_id=plan_id,
        version=HISTORICAL_PLAN_VERSION,
        dataset_id=dataset_id,
        calendar_digest=calendar_digest(calendar),
        instrument=instrument,
        policies=tuple(policies),
        baseline=baseline,
        initial_balance_usdt=initial_balance_usdt,
        maker_fee=maker_fee,
        taker_fee=taker_fee,
        bar_ns=bar_ns,
        history=history,
        aggregation=aggregation,
        train_window=train_window,
        folds=tuple(windows),
        final_eligible=calendar.final_eligible,
        final_eligibility_reason=calendar.final_ineligibility_reason
        if not calendar.final_eligible
        else None,
        code_and_lock_hash=code_and_lock_hash,
    )
    ok, reason = plan.verify()
    if not ok:
        raise ValueError(f"historical plan is not self-consistent: {reason}")
    if not calendar.final_eligible and final_role != "UNMAPPED":
        # record the measured tail role for the reader even when the final is closed
        plan = replace(
            plan,
            final_eligibility_reason=(
                f"{calendar.final_ineligibility_reason}; measured tail tape_role={final_role}"
            ),
        )
    return plan


def freeze_historical_plan(
    store: ResearchStore,
    plan: HistoricalPlan,
    *,
    registered_ns: int | None = None,
) -> str:
    """Register a historical plan once, immutably. Identical retries are idempotent.

    Unlike the prospective freeze, this reads no wall clock against the plan's own
    windows: the plan looks backwards, so "frozen before the window" is not a
    meaningful constraint. What is enforced is immutability and identity: a second
    registration of the same ``plan_id`` with different bytes is refused.
    """
    ok, reason = plan.verify()
    if not ok:
        raise ValueError(f"refusing to freeze an inconsistent historical plan: {reason}")
    digest = plan.digest()
    payload = canonical(plan.as_dict(include_digest=False))
    store.record_historical_plan(
        plan_id=plan.plan_id,
        payload=payload,
        digest=digest,
        registered_ns=time.time_ns() if registered_ns is None else registered_ns,
    )
    return digest


def readback_historical_plan(store: ResearchStore, plan_id: str) -> HistoricalPlan:
    """Read a plan back from the store and re-derive its digest (read-back check)."""
    row = store.get_historical_plan(plan_id)
    if row is None:
        raise KeyError(f"no historical plan {plan_id!r} in the store")
    payload, digest = row
    if "sha256:" + hashlib.sha256(payload.encode()).hexdigest() != digest:
        raise ValueError("stored historical plan payload does not match its digest")
    raw = json.loads(payload)
    folds = tuple(
        FoldWindow(
            fold_id=str(f["fold_id"]),
            role=f["role"],
            train_start_ns=int(f["train_start_ns"]),
            train_end_ns=int(f["train_end_ns"]),
            scored_start_ns=int(f["scored_start_ns"]),
            scored_end_ns=int(f["scored_end_ns"]),
            warmup_start_ns=int(f["warmup_start_ns"]),
            warmup_end_ns=int(f["warmup_end_ns"]),
            purge_bars=int(f["purge_bars"]),
            tape_role=str(f["tape_role"]),
        )
        for f in raw["folds"]
    )
    history_raw = raw["history"]
    history = HistoryRequirements(
        policies=tuple(history_raw["policies"]),
        required_bars={k: int(v) for k, v in history_raw["required_bars"].items()},
        horizon_bars={k: int(v) for k, v in history_raw["horizon_bars"].items()},
        warmup_bars=int(history_raw["warmup_bars"]),
        purge_bars=int(history_raw["purge_bars"]),
        protection_ttl_bars=int(history_raw["protection_ttl_bars"]),
        session_bars=int(history_raw["session_bars"]),
        rationale=str(history_raw["rationale"]),
    )
    plan = HistoricalPlan(
        plan_id=str(raw["plan_id"]),
        version=str(raw["version"]),
        dataset_id=str(raw["dataset_id"]),
        calendar_digest=str(raw["calendar_digest"]),
        instrument=str(raw["instrument"]),
        policies=tuple(raw["policies"]),
        baseline=str(raw["baseline"]),
        initial_balance_usdt=str(raw["initial_balance_usdt"]),
        maker_fee=str(raw["maker_fee"]),
        taker_fee=str(raw["taker_fee"]),
        bar_ns=int(raw["bar_ns"]),
        history=history,
        aggregation=raw["aggregation"],
        train_window=raw["train_window"],
        folds=folds,
        final_eligible=bool(raw["final_eligible"]),
        final_eligibility_reason=raw["final_eligibility_reason"],
        code_and_lock_hash=str(raw.get("code_and_lock_hash", "")),
    )
    ok, reason = plan.verify()
    if not ok:
        raise ValueError(f"stored historical plan is inconsistent: {reason}")
    return plan
