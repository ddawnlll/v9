"""S6 reconciliation — port of ``v8-core/src/analysis/reconcile.rs`` (issue #122).

The Phase-0 post-Candidate half of the frozen oracle, ported to the analysis plane. Three
steps mirror the Rust module function-for-function:

1. :func:`build_snapshots` — FT001. A candidate whose draft cannot be bound is reported
   ``UNBOUND_NO_DRAFT``, never dropped and never defaulted (FER CA010). The draft is bound to
   the ``DETECTED`` transition by its *birth clock* ``(expert_id, knowledge_time)``, so two
   episodes that re-evaluate at the same D-026 anchor with a moved structural geometry stay
   distinct.
2. :func:`assert_pit_lineage` — FT001c / RIR GR006. Returns the **list of named lineage
   violations** (empty = clean), never a ``bool``. Every ``BOUND`` snapshot's
   ``birth_state_id`` must resolve in the states ledger and no feature's
   ``max_input_available_time`` may exceed the birth state's ``as_of``.
3. :func:`reconcile_actual_actions` — FT010. The replay of the actual action against the
   observed ledger outcome, compared on the ten fields of
   :class:`v8_next.analysis.outcome.OutcomeSurface`: exact equality on ``endpoint`` /
   ``label_status`` / ``horizon_bars`` / ``ambiguous_bars``, ``RECONCILE_TOLERANCE`` on the
   six float fields. Never-entered candidates are ``NOT_APPLICABLE`` (FT010c).

Fail-closed rules this port enforces (and the Rust does not — see ``DIVERGENCES``): a
candidate that was not compared, or an observed outcome that does not carry all ten fields,
produces an explicit named status/reason. A reconciliation over zero compared candidates is
``RECONCILIATION_NOT_MEASURED``, never a vacuous ``RECONCILED``, and an unmeasured
``max_abs_deviation`` is ``None`` rather than the ``0.0`` that reads as bit-exact.

Everything here is ``NO_ECONOMIC_CLAIM``: reconciliation proves the replay agrees with the
ledger, it values nothing.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

from v8_next.analysis.outcome import (
    RECONCILE_EXACT_FIELDS,
    RECONCILE_FIELD_COUNT,
    RECONCILE_FLOAT_FIELDS,
    RECONCILE_TOLERANCE,
    OutcomeSurface,
)

CLAIM = "NO_ECONOMIC_CLAIM"

#: Mirror of ``tools/regret.py`` ``EVALUATOR_VERSION`` (the frozen-oracle evaluator id).
EVALUATOR_VERSION = "regret-phase0-v1"

#: The binding statuses (Rust ``BOUND`` / ``UNBOUND_NO_DRAFT``).
BOUND = "BOUND"
UNBOUND_NO_DRAFT = "UNBOUND_NO_DRAFT"

#: The reconciliation verdicts (Rust ``RECONCILED`` / ``RECONCILIATION_FAILED``) plus the
#: fail-closed status this port adds for a comparison over zero candidates.
RECONCILED = "RECONCILED"
RECONCILIATION_FAILED = "RECONCILIATION_FAILED"
RECONCILIATION_NOT_MEASURED = "RECONCILIATION_NOT_MEASURED"

#: The mismatch reasons (Rust ``MISMATCH_REASON_FIELD`` / ``MISMATCH_REASON_ENTRY_MISSING``).
MISMATCH_REASON_FIELD = "field_mismatch"
MISMATCH_REASON_ENTRY_MISSING = "entry_bar_or_outcome_missing"

#: The measurement status of a reconciliation result.
MEASURED = "MEASURED"
NOT_MEASURED_NO_COMPARED_CANDIDATE = "NOT_MEASURED_NO_COMPARED_CANDIDATE"

#: The terminal candidate states (mirror of ``v8-core/src/candidate.rs:TERMINAL``).
TERMINAL_STATES: tuple[str, ...] = (
    "REJECTED",
    "EXPIRED",
    "INVALIDATED",
    "CANCELLED",
    "CLOSED",
    "ARCHIVED",
)

#: The action id the Rust reconcile stamps on a replayed surface (``reconcile.rs:576``).
REPLAY_ACTION_ID = "ACTUAL"
#: The action id this port stamps on the leased observed-outcome surface.
OBSERVED_ACTION_ID = "OBSERVED"

#: Named divergences from ``reconcile.rs``: deliberate non-ports and removed imputations.
DIVERGENCES: tuple[str, ...] = (
    "run(args): the Rust CLI entry point's only caller is the v8-core binary's "
    "`analysis::reconcile` dispatcher; the Python API is ReconcileRequest/reconcile, so the "
    "argv/exit-code surface is not ported.",
    "read_tape / Dataset / FeatureStore / ScalarKernel: the tape reader, the columnar bar "
    "store and the replay kernel are adapter/runtime concerns, not ported here. "
    "`reconcile_actual_actions` consumes the replayed OutcomeSurface per candidate "
    "(`replays[candidate_id]`) rather than executing the kernel itself.",
    "the parallel worker path (`std::thread::scope` + `scheduler::chunk_bounds`): this port "
    "is single-threaded, so the same counters/verdict are computed on one worker. No "
    "concurrency is claimed.",
    "file writing: Rust `reconcile` writes `<out_dir>/reconciliation.json` as a side effect. "
    "This port writes nothing — `reconcile` returns the oracle-shaped summary and "
    "`reconciliation_artifact` returns the six-field artifact; where it lands is the "
    "caller's decision.",
    "size: Rust defaults an absent draft size to 1.0; this port reports None, because an "
    "admission size that was not measured must not read as a plausible constant (the field "
    "is excluded from reconciliation by FT010b anyway).",
    "birth_time: Rust imputes 0 for an absent DETECTED clock; this port reports None rather "
    "than timestamping a candidate at the epoch.",
    "a missing observed float field: Rust reads it as 0.0 (\"Python: float(obs.get(f, 0.0))\") "
    "and then compares; this port refuses the candidate with the named reason "
    "entry_bar_or_outcome_missing, because a fabricated 0.0 makes the comparison agree or "
    "disagree for the wrong reason.",
    "verdict over zero compared candidates: Rust returns RECONCILED whenever n_mismatched is "
    "0, which is a vacuous pass when nothing was compared; this port returns "
    "RECONCILIATION_NOT_MEASURED.",
    "max_abs_deviation with zero compared candidates: Rust initializes every field to 0.0, "
    "which reads as bit-exact; this port reports None for each field until a candidate is "
    "actually compared.",
    "assert_pit_lineage: Rust treats an absent `as_of` as 0 and an absent feature "
    "`max_input_available_time` as 0 (both unmatchable lineage facts); this port raises a "
    "named violation for each, so an unverifiable clock is not silently clean.",
)


def _mapping(value: object) -> Mapping[str, Any] | None:
    """Return ``value`` as a string-keyed mapping, or ``None`` when it is not a mapping."""
    if isinstance(value, Mapping):
        return cast("Mapping[str, Any]", value)
    return None


def _as_str(value: object) -> str:
    """Return ``value`` when it is a string, else the empty string (mirrors Rust ``unwrap_or("")``)."""
    return value if isinstance(value, str) else ""


def _as_int(value: object) -> int | None:
    """Return ``value`` as an ``int``, accepting integral floats (mirrors Rust ``exact_i64``).

    ``bool`` is rejected even though it subclasses ``int``: JSON ``true`` is not an integer
    field value, and Rust's ``as_i64`` returns ``None`` for it.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None


def _as_float(value: object) -> float | None:
    """Return ``value`` as a ``float``, or ``None`` when it is not a number."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _surface_float(surface: OutcomeSurface, name: str) -> float:
    """Read one float field of a surface (``name`` must be a ``RECONCILE_FLOAT_FIELDS`` member)."""
    return float(getattr(surface, name))


def _observed_surface(candidate_id: str, observed: Mapping[str, Any]) -> OutcomeSurface | None:
    """Project an observed-outcome ledger record onto the ten compared fields.

    Returns ``None`` when any of the ten fields is absent or the wrong shape. A partially
    written outcome is *missing evidence*, not a zero — the caller reports
    ``entry_bar_or_outcome_missing`` rather than letting an imputed value drive the verdict.
    """
    endpoint = observed.get("endpoint")
    label_status = observed.get("label_status")
    horizon = _as_int(observed.get("horizon_bars"))
    ambiguous = _as_int(observed.get("ambiguous_bars"))
    if not isinstance(endpoint, str) or not isinstance(label_status, str):
        return None
    if horizon is None or ambiguous is None:
        return None
    floats: dict[str, float] = {}
    for name in RECONCILE_FLOAT_FIELDS:
        value = _as_float(observed.get(name))
        if value is None:
            return None
        floats[name] = value
    return OutcomeSurface(
        candidate_id=candidate_id,
        action_id=OBSERVED_ACTION_ID,
        endpoint=endpoint,
        label_status=label_status,
        horizon_bars=horizon,
        ambiguous_bars=ambiguous,
        **floats,
    )


def _first_mismatched_field(replayed: OutcomeSurface, observed: OutcomeSurface) -> str:
    """The name of the first compared field that differs, in canonical order.

    Exact fields return the bare name; a float field returns
    ``"<name>:rust=<replayed>:obs=<observed>"`` (the Rust diagnostic shape). The field name
    is a diagnostic — the reconciliation *reason* string is the parity target, the name is
    not.
    """
    for name in RECONCILE_EXACT_FIELDS:
        if getattr(replayed, name) != getattr(observed, name):
            return name
    for name in RECONCILE_FLOAT_FIELDS:
        left = _surface_float(replayed, name)
        right = _surface_float(observed, name)
        if abs(left - right) > RECONCILE_TOLERANCE:
            return f"{name}:rust={left:.9f}:obs={right:.9f}"
    return "unknown"


def _draft_atr_ref(raw_draft: Mapping[str, Any] | None) -> float | None:
    """The ``atr_ref`` of a draft's frozen risk geometry, or ``None`` when it is not present."""
    draft = _mapping(raw_draft) if raw_draft is not None else None
    if draft is None:
        return None
    geometry = _mapping(draft.get("risk_geometry"))
    if geometry is None:
        return None
    return _as_float(geometry.get("atr_ref"))


@dataclass(frozen=True)
class CandidateSnapshot:
    """One joined Candidate row (mirror of ``tools/regret.py`` ``CandidateSnapshot``).

    Identity fields are bit-encoded and excluded from value parity (PARITY_AND_IDENTITY_SPEC
    §3); ``observed_outcome`` carries the ten reconciliation fields from the outcomes ledger.
    Fields the Rust marks ``#[allow(dead_code)]`` (consumed by the Phase-1 join rather than
    by reconciliation) are kept as data so the projection is complete.
    """

    candidate_id: str
    expert_id: str
    expert_version: str
    instrument: str
    direction: str
    setup_anchor_event_id: str
    geometry_version: str
    birth_time: int | None
    birth_state_id: str | None
    risk_geometry: Mapping[str, Any]
    size: float | None
    terminal_state: str | None
    terminal_reason_code: str | None
    entry_bar_available_time: int | None
    observed_outcome: Mapping[str, Any] | None
    binding_status: str
    raw_draft: Mapping[str, Any] | None
    predicate_ir: Mapping[str, Any] | None = None


@dataclass
class ReconciliationResult:
    """The reconciliation counters (mirror of ``tools/regret.py`` ``ReconciliationResult``).

    ``mismatches`` carries ``(candidate_id, reason)`` pairs. ``max_abs_deviation`` holds the
    largest absolute deviation seen per float field, or ``None`` for a field that was never
    compared (nothing to measure is absence, not a perfect ``0.0``).
    """

    n_executed: int
    n_reconciled: int
    n_mismatched: int
    n_not_applicable: int
    mismatches: tuple[tuple[str, str], ...]
    max_abs_deviation: dict[str, float | None]
    verdict: str
    measurement_status: str = NOT_MEASURED_NO_COMPARED_CANDIDATE
    fields_compared: int = RECONCILE_FIELD_COUNT
    claim: str = CLAIM

    def as_dict(self) -> dict[str, Any]:
        return {
            "n_executed": self.n_executed,
            "n_reconciled": self.n_reconciled,
            "n_mismatched": self.n_mismatched,
            "n_not_applicable": self.n_not_applicable,
            "mismatches": [list(pair) for pair in self.mismatches],
            "max_abs_deviation": dict(sorted(self.max_abs_deviation.items())),
            "verdict": self.verdict,
            "measurement_status": self.measurement_status,
            "fields_compared": self.fields_compared,
            "claim": self.claim,
        }


def build_snapshots(
    candidates: Sequence[Mapping[str, Any]],
    evaluations: Sequence[Mapping[str, Any]],
    outcomes: Sequence[Mapping[str, Any]],
) -> list[CandidateSnapshot]:
    """FT001 join: ``candidates``/``evaluations``/``outcomes`` -> snapshots.

    ``candidates`` are the transition records, ``evaluations`` the draft-carrying records and
    ``outcomes`` the observed outcomes. Drafts are keyed by ``(expert_id, knowledge_time)`` —
    the only binding that survives episodes re-evaluating at the same D-026 anchor with a
    moved structural geometry. A transition group with no matching draft is reported
    ``UNBOUND_NO_DRAFT``, never dropped.
    """
    drafts_by_clock: dict[tuple[str, int | None], Mapping[str, Any]] = {}
    for record in evaluations:
        draft = _mapping(record.get("draft"))
        if draft is None:
            continue
        key = (_as_str(record.get("expert_id")), _as_int(record.get("knowledge_time")))
        drafts_by_clock.setdefault(key, draft)

    transitions_by_cid: dict[str, list[Mapping[str, Any]]] = {}
    for record in candidates:
        if "to_state" not in record:
            continue
        cid = record.get("candidate_id")
        if isinstance(cid, str):
            transitions_by_cid.setdefault(cid, []).append(record)

    outcome_by_cid: dict[str, Mapping[str, Any]] = {}
    for outcome in outcomes:
        cid = outcome.get("candidate_id")
        if isinstance(cid, str) and cid not in outcome_by_cid:
            outcome_by_cid[cid] = outcome

    snapshots: list[CandidateSnapshot] = []
    for cid in sorted(transitions_by_cid):
        transitions = sorted(
            transitions_by_cid[cid], key=lambda r: _as_int(r.get("sequence")) or 0
        )
        detected = _first_in_state(transitions, "DETECTED")
        executed = _first_in_state(transitions, "EXECUTED")
        terminal = _last_terminal(transitions)

        draft = None
        if detected is not None:
            key = (_as_str(detected.get("expert_id")), _as_int(detected.get("knowledge_time")))
            draft = drafts_by_clock.get(key)
        entry_time = _as_int(executed.get("knowledge_time")) if executed is not None else None
        predicate_ir = _mapping(detected.get("predicate_ir")) if detected is not None else None
        observed = outcome_by_cid.get(cid)

        if draft is None:
            snapshots.append(
                _unbound_snapshot(cid, detected, terminal, entry_time, observed, predicate_ir)
            )
            continue
        snapshots.append(
            _bound_snapshot(
                cid, detected, draft, terminal, entry_time, observed, predicate_ir
            )
        )
    return snapshots


def _first_in_state(
    transitions: Sequence[Mapping[str, Any]], state: str
) -> Mapping[str, Any] | None:
    """The first transition whose ``to_state`` equals ``state`` (transitions already sorted)."""
    for transition in transitions:
        if transition.get("to_state") == state:
            return transition
    return None


def _last_terminal(transitions: Sequence[Mapping[str, Any]]) -> Mapping[str, Any] | None:
    """The last transition whose ``to_state`` is a terminal candidate state."""
    for transition in reversed(transitions):
        to_state = transition.get("to_state")
        if isinstance(to_state, str) and to_state in TERMINAL_STATES:
            return transition
    return None


def _terminal_state(terminal: Mapping[str, Any] | None) -> str | None:
    if terminal is None:
        return None
    return _as_str(terminal.get("to_state"))


def _terminal_reason(terminal: Mapping[str, Any] | None) -> str | None:
    if terminal is None:
        return None
    reason = terminal.get("reason_code")
    return reason if isinstance(reason, str) else None


def _unbound_snapshot(
    cid: str,
    detected: Mapping[str, Any] | None,
    terminal: Mapping[str, Any] | None,
    entry_time: int | None,
    observed: Mapping[str, Any] | None,
    predicate_ir: Mapping[str, Any] | None,
) -> CandidateSnapshot:
    """UNBOUND_NO_DRAFT: identity falls back to the DETECTED transition, never a drop (FER CA010)."""
    detected = detected or {}
    state_id = detected.get("state_id")
    return CandidateSnapshot(
        candidate_id=cid,
        expert_id=_as_str(detected.get("expert_id")),
        expert_version=_as_str(detected.get("expert_version")),
        instrument=_as_str(detected.get("instrument")),
        direction=_as_str(detected.get("direction")),
        setup_anchor_event_id=_as_str(detected.get("setup_anchor_event_id")),
        geometry_version=_as_str(detected.get("geometry_version")),
        birth_time=_as_int(detected.get("knowledge_time")),
        birth_state_id=state_id if isinstance(state_id, str) else None,
        risk_geometry={},
        size=None,
        terminal_state=_terminal_state(terminal),
        terminal_reason_code=_terminal_reason(terminal),
        entry_bar_available_time=entry_time,
        observed_outcome=observed,
        binding_status=UNBOUND_NO_DRAFT,
        raw_draft=None,
        predicate_ir=predicate_ir,
    )


def _bound_snapshot(
    cid: str,
    detected: Mapping[str, Any] | None,
    draft: Mapping[str, Any],
    terminal: Mapping[str, Any] | None,
    entry_time: int | None,
    observed: Mapping[str, Any] | None,
    predicate_ir: Mapping[str, Any] | None,
) -> CandidateSnapshot:
    """BOUND: the draft supplies identity/geometry; the DETECTED transition supplies state_id."""
    geometry = _mapping(draft.get("risk_geometry")) or {}
    state_id = detected.get("state_id") if detected is not None else None
    return CandidateSnapshot(
        candidate_id=cid,
        expert_id=_as_str(draft.get("expert_id")),
        expert_version=_as_str(draft.get("expert_version")),
        instrument=_as_str(draft.get("instrument")),
        direction=_as_str(draft.get("direction")),
        setup_anchor_event_id=_as_str(draft.get("setup_anchor_event_id")),
        geometry_version=_as_str(detected.get("geometry_version")) if detected is not None else "",
        birth_time=_as_int(draft.get("birth_time")),
        birth_state_id=state_id if isinstance(state_id, str) else None,
        risk_geometry=geometry,
        size=_as_float(draft.get("size")),
        terminal_state=_terminal_state(terminal),
        terminal_reason_code=_terminal_reason(terminal),
        entry_bar_available_time=entry_time,
        observed_outcome=observed,
        binding_status=BOUND,
        raw_draft=draft,
        predicate_ir=predicate_ir,
    )


def assert_pit_lineage(
    states: Sequence[Mapping[str, Any]], snapshots: Sequence[CandidateSnapshot]
) -> list[str]:
    """FT001c / RIR GR006: PIT lineage verification, returning the named violations.

    Every ``BOUND`` snapshot's ``birth_state_id`` must resolve in the states ledger and no
    feature's ``max_input_available_time`` may exceed the birth state's own ``as_of``
    (future leakage). The return is the **violation list** — empty means clean — never a
    ``bool``, so a report can name every leak instead of one aggregated verdict.
    """
    states_by_id: dict[str, Mapping[str, Any]] = {}
    for state in states:
        sid = state.get("state_id")
        if isinstance(sid, str) and sid not in states_by_id:
            states_by_id[sid] = state

    problems: list[str] = []
    for snapshot in snapshots:
        if snapshot.binding_status != BOUND:
            continue
        if snapshot.birth_state_id is None:
            problems.append(f"{snapshot.candidate_id}: no birth_state_id recorded")
            continue
        birth_state = states_by_id.get(snapshot.birth_state_id)
        if birth_state is None:
            problems.append(
                f"{snapshot.candidate_id}: birth_state_id {snapshot.birth_state_id} "
                "not found in states.jsonl"
            )
            continue
        as_of = _as_int(birth_state.get("as_of"))
        if as_of is None:
            problems.append(
                f"{snapshot.candidate_id}: state {snapshot.birth_state_id} has no as_of "
                "decision clock — lineage unverifiable"
            )
            continue
        features = _mapping(birth_state.get("features"))
        if features is None:
            continue
        for feature_name, feature_value in features.items():
            feature = _mapping(feature_value)
            if feature is None:
                continue
            available_time = _as_int(feature.get("max_input_available_time"))
            if available_time is None:
                problems.append(
                    f"{snapshot.candidate_id}: feature {feature_name} has no "
                    "max_input_available_time — lineage unverifiable"
                )
                continue
            if available_time > as_of:
                problems.append(
                    f"{snapshot.candidate_id}: feature {feature_name} "
                    f"max_input_available_time {available_time} > decision clock {as_of} "
                    "— future leakage"
                )
    return problems


def reconcile_actual_actions(
    snapshots: Sequence[CandidateSnapshot],
    replays: Mapping[str, OutcomeSurface],
) -> ReconciliationResult:
    """FT010: reconcile the replayed actual action against the observed ledger outcome.

    ``replays`` maps ``candidate_id`` to the replayed surface produced by executing the
    actual action (``Replay(C, a_actual, M)``). The observed outcome is
    ``snapshot.observed_outcome``. A ``BOUND`` snapshot with no entry bar is
    ``NOT_APPLICABLE`` (FT010c); a candidate whose entry bar or observed outcome cannot be
    resolved is ``entry_bar_or_outcome_missing``; a compared candidate that disagrees on a
    field is ``field_mismatch``. A reconciliation over zero *compared* candidates is
    ``RECONCILIATION_NOT_MEASURED`` — never a vacuous ``RECONCILED``.
    """
    mismatches: list[tuple[str, str]] = []
    max_abs_deviation: dict[str, float | None] = {name: None for name in RECONCILE_FLOAT_FIELDS}
    n_executed = 0
    n_reconciled = 0
    n_mismatched = 0
    n_not_applicable = 0
    n_compared = 0

    for snapshot in snapshots:
        if snapshot.binding_status != BOUND or snapshot.entry_bar_available_time is None:
            n_not_applicable += 1  # FT010c: never entered -> NOT_APPLICABLE
            continue
        n_executed += 1

        observed_raw = snapshot.observed_outcome
        observed = (
            _observed_surface(snapshot.candidate_id, observed_raw)
            if observed_raw is not None
            else None
        )
        replayed = replays.get(snapshot.candidate_id)
        if observed is None or replayed is None:
            n_mismatched += 1
            mismatches.append((snapshot.candidate_id, MISMATCH_REASON_ENTRY_MISSING))
            continue

        n_compared += 1
        for name in RECONCILE_FLOAT_FIELDS:
            deviation = abs(_surface_float(replayed, name) - _surface_float(observed, name))
            previous = max_abs_deviation[name]
            max_abs_deviation[name] = (
                deviation if previous is None else max(previous, deviation)
            )

        if replayed.values_match(observed):
            n_reconciled += 1
            continue

        n_mismatched += 1
        field_detail = _first_mismatched_field(replayed, observed)
        atr_ref = _draft_atr_ref(snapshot.raw_draft)
        atr_text = f"{atr_ref:.9f}" if atr_ref is not None else "None"
        mismatches.append(
            (
                snapshot.candidate_id,
                f"{MISMATCH_REASON_FIELD}:{snapshot.expert_id}:{snapshot.direction}:"
                f"{field_detail}:atr={atr_text}",
            )
        )

    if n_mismatched > 0:
        verdict = RECONCILIATION_FAILED
    elif n_compared == 0:
        verdict = RECONCILIATION_NOT_MEASURED
    else:
        verdict = RECONCILED
    measurement_status = MEASURED if n_compared > 0 else NOT_MEASURED_NO_COMPARED_CANDIDATE

    return ReconciliationResult(
        n_executed=n_executed,
        n_reconciled=n_reconciled,
        n_mismatched=n_mismatched,
        n_not_applicable=n_not_applicable,
        mismatches=tuple(mismatches),
        max_abs_deviation=max_abs_deviation,
        verdict=verdict,
        measurement_status=measurement_status,
    )


def reconciliation_artifact(result: ReconciliationResult) -> dict[str, Any]:
    """The six-field ``reconciliation.json`` artifact (the Rust deliverable of issue #122),
    plus this port's ``measurement_status`` and ``claim`` — the file never asserts an
    economic authority."""
    return {
        "n_executed": result.n_executed,
        "n_reconciled": result.n_reconciled,
        "n_mismatched": result.n_mismatched,
        "n_not_applicable": result.n_not_applicable,
        "max_abs_deviation": dict(sorted(result.max_abs_deviation.items())),
        "verdict": result.verdict,
        "measurement_status": result.measurement_status,
        "claim": CLAIM,
    }


def reconciliation_summary(
    result: ReconciliationResult,
    *,
    n_candidates: int,
    n_unbound: int,
    problems: Sequence[str],
) -> dict[str, Any]:
    """The oracle-shaped summary (mirror of ``reconcile.rs:summary_json``).

    ``halted`` is ``True`` unless the verdict is ``RECONCILED`` *and* the PIT lineage is
    clean; ``halt_reason`` names which invariant broke. The mismatch detail lives here, not
    in the artifact.
    """
    halted = result.verdict != RECONCILED or bool(problems)
    if result.verdict == RECONCILIATION_FAILED:
        halt_reason: str | None = (
            "reconciliation failed — load-bearing invariant broken, refusing to produce a cube"
        )
    elif result.verdict == RECONCILIATION_NOT_MEASURED:
        halt_reason = (
            "reconciliation not measured — no candidate reached the ten-field comparison, "
            "refusing a vacuous pass"
        )
    elif problems:
        halt_reason = "PIT lineage violation detected — future leakage, refusing to proceed"
    else:
        halt_reason = None

    summary: dict[str, Any] = {
        "evaluator_version": EVALUATOR_VERSION,
        "n_candidates": n_candidates,
        "n_unbound": n_unbound,
        "pit_lineage_problems": list(problems),
        "reconciliation": result.as_dict(),
        "halted": halted,
        "claim": CLAIM,
    }
    if halt_reason is not None:
        summary["halt_reason"] = halt_reason
    return summary


@dataclass(frozen=True)
class ReconcileRequest:
    """The S6 reconciliation request (mirror of ``reconcile.rs:ReconcileRequest``).

    The Rust request carries a tape path and an output directory because its ``reconcile``
    reads the tape and writes the artifact. This port takes the ledger arrays directly and
    writes nothing: ``tape_path`` / ``out_dir`` / ``manifest`` / ``universe`` /
    ``evaluations_path`` / ``cube_reduced_path`` are accepted for shape compatibility and are
    not consumed (the Rust marks the last three ``#[allow(dead_code)]`` for the same reason).
    ``replays`` is the per-candidate replayed surface ``reconcile_actual_actions`` compares
    against the observed ledger.
    """

    candidates: Sequence[Mapping[str, Any]] = ()
    evaluations: Sequence[Mapping[str, Any]] = ()
    outcomes: Sequence[Mapping[str, Any]] = ()
    states: Sequence[Mapping[str, Any]] = ()
    replays: Mapping[str, OutcomeSurface] = field(default_factory=dict)
    tape_path: Path | None = None
    out_dir: Path | None = None
    universe: Sequence[str] = ()
    manifest: Mapping[str, Any] = field(default_factory=dict)
    evaluations_path: Path | None = None
    cube_reduced_path: Path | None = None


def reconcile(request: ReconcileRequest) -> dict[str, Any]:
    """Build snapshots, assert PIT lineage, reconcile, and return the summary.

    Mirrors ``reconcile.rs:reconcile`` minus the tape reading and the artifact write; a
    caller that wants the six-field file uses :func:`reconciliation_artifact` on the result
    it recomputes with :func:`reconcile_actual_actions`.
    """
    snapshots = build_snapshots(request.candidates, request.evaluations, request.outcomes)
    problems = assert_pit_lineage(request.states, snapshots)
    result = reconcile_actual_actions(snapshots, request.replays)
    n_unbound = sum(1 for snap in snapshots if snap.binding_status == UNBOUND_NO_DRAFT)
    return reconciliation_summary(
        result,
        n_candidates=len(snapshots),
        n_unbound=n_unbound,
        problems=problems,
    )
