"""Reconcile contracts — S6 reconciliation mechanics (MECHANICS ONLY).

These tests pin the *vocabulary* and the *mechanics* of the ported reconcile plane: the
binding/verdict/mismatch status strings, the field-by-field mismatch attribution, and the
named PIT-lineage violations. Every fixture is hand-written structure (one entered
candidate, one ghost candidate, one leaky state) used only to exercise the arithmetic; no
assertion here carries evaluative weight and no test asserts economic performance. There is
no tape and no network.
"""

from __future__ import annotations

from typing import Any

import pytest

from v8_next.analysis import (
    BOUND,
    EVALUATOR_VERSION,
    MISMATCH_REASON_ENTRY_MISSING,
    MISMATCH_REASON_FIELD,
    RECONCILE_EXACT_FIELDS,
    RECONCILE_FLOAT_FIELDS,
    RECONCILED,
    RECONCILIATION_FAILED,
    RECONCILIATION_NOT_MEASURED,
    UNBOUND_NO_DRAFT,
    CandidateSnapshot,
    OutcomeSurface,
    ReconcileRequest,
    assert_pit_lineage,
    build_snapshots,
    reconcile_actual_actions,
    reconciliation_artifact,
)
from v8_next.analysis.reconcile import reconcile

DETECTED_AT = 3000
ENTRY_TIME = 5000
SYMBOL = "SOLUSDT"


# --------------------------------------------------------------------------------------
# fixtures (structural only)
# --------------------------------------------------------------------------------------


def _replay(cid: str, **overrides: Any) -> OutcomeSurface:
    values: dict[str, Any] = {
        "endpoint": "TARGET",
        "label_status": "MATURE",
        "horizon_bars": 1,
        "ambiguous_bars": 0,
        "net_r": 1.5,
        "entry_price": 107.0,
        "risk_unit_price": 1.07,
        "mae_r": 0.9345794392523364,
        "mfe_r": 1.8691588785046729,
        "market_move_r": 0.9345794392523364,
    }
    values.update(overrides)
    return OutcomeSurface(candidate_id=cid, action_id="ACTUAL", **values)


def _observed(cid: str, **overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "candidate_id": cid,
        "endpoint": "TARGET",
        "label_status": "MATURE",
        "horizon_bars": 1,
        "ambiguous_bars": 0,
        "net_r": 1.5,
        "entry_price": 107.0,
        "risk_unit_price": 1.07,
        "mae_r": 0.9345794392523364,
        "mfe_r": 1.8691588785046729,
        "market_move_r": 0.9345794392523364,
    }
    values.update(overrides)
    return values


def _snapshot(
    cid: str,
    *,
    binding: str = BOUND,
    entry: int | None = ENTRY_TIME,
    observed: dict[str, Any] | None = None,
    expert: str = "fragile_expert",
    direction: str = "LONG",
    birth_state_id: str | None = "st-sol-1",
    risk_geometry: dict[str, Any] | None = None,
    raw_draft: dict[str, Any] | None = None,
) -> CandidateSnapshot:
    return CandidateSnapshot(
        candidate_id=cid,
        expert_id=expert,
        expert_version="1.0",
        instrument=SYMBOL,
        direction=direction,
        setup_anchor_event_id="sol-setup-1",
        geometry_version="geom-hex",
        birth_time=DETECTED_AT,
        birth_state_id=birth_state_id,
        risk_geometry=risk_geometry or {},
        size=1.0,
        terminal_state=None,
        terminal_reason_code=None,
        entry_bar_available_time=entry,
        observed_outcome=observed,
        binding_status=binding,
        raw_draft=raw_draft,
    )


def _transition(
    cid: str, seq: int, to_state: str, kt: int, **extra: Any
) -> dict[str, Any]:
    return {
        "candidate_id": cid,
        "sequence": seq,
        "to_state": to_state,
        "reason_code": "test",
        "knowledge_time": kt,
        **extra,
    }


def _entered_transitions(cid: str, expert: str = "fragile_expert") -> list[dict[str, Any]]:
    return [
        _transition(
            cid,
            1,
            "DETECTED",
            DETECTED_AT,
            expert_id=expert,
            expert_version="1.0",
            instrument=SYMBOL,
            direction="LONG",
            setup_anchor_event_id="sol-setup-1",
            geometry_version="geom-hex",
            state_id="st-sol-1",
        ),
        _transition(cid, 2, "PENDING", 3100),
        _transition(cid, 3, "TRIGGERED", 3200),
        _transition(cid, 4, "ACCEPTED", 3300),
        _transition(cid, 5, "ORDER_SUBMITTED", 3400),
        _transition(cid, 6, "EXECUTED", ENTRY_TIME),
    ]


def _detected_only_transition(cid: str, expert: str) -> dict[str, Any]:
    return _transition(
        cid,
        1,
        "DETECTED",
        DETECTED_AT,
        expert_id=expert,
        expert_version="1.0",
        instrument=SYMBOL,
        direction="LONG",
        setup_anchor_event_id="sol-setup-2",
        geometry_version="geom-hex",
        state_id="st-sol-2",
    )


def _draft(expert: str, **overrides: Any) -> dict[str, Any]:
    draft: dict[str, Any] = {
        "expert_id": expert,
        "expert_version": "1.0",
        "instrument": SYMBOL,
        "direction": "LONG",
        "risk_geometry": {"target_r": 1.5, "stop_r": 1.0, "expiry_bars": 8},
        "birth_time": DETECTED_AT,
        "setup_anchor_event_id": "sol-setup-1",
        "size": 1.0,
    }
    draft.update(overrides)
    return draft


def _evaluation(expert: str, knowledge_time: int, **overrides: Any) -> dict[str, Any]:
    record: dict[str, Any] = {
        "expert_id": expert,
        "knowledge_time": knowledge_time,
        "draft": _draft(expert),
    }
    record.update(overrides)
    return record


def _state(sid: str, feature_available_time: int) -> dict[str, Any]:
    return {
        "state_id": sid,
        "as_of": DETECTED_AT,
        "features": {
            "sma20": {
                "name": "sma20",
                "value": 100.0,
                "max_input_available_time": feature_available_time,
            }
        },
    }


# --------------------------------------------------------------------------------------
# status vocabulary is the ported closed set
# --------------------------------------------------------------------------------------


def test_status_and_reason_vocabulary_is_the_ported_closed_set() -> None:
    assert EVALUATOR_VERSION == "regret-phase0-v1"
    assert (BOUND, UNBOUND_NO_DRAFT) == ("BOUND", "UNBOUND_NO_DRAFT")
    assert (RECONCILED, RECONCILIATION_FAILED, RECONCILIATION_NOT_MEASURED) == (
        "RECONCILED",
        "RECONCILIATION_FAILED",
        "RECONCILIATION_NOT_MEASURED",
    )
    assert (MISMATCH_REASON_FIELD, MISMATCH_REASON_ENTRY_MISSING) == (
        "field_mismatch",
        "entry_bar_or_outcome_missing",
    )


def test_the_module_is_addressable_and_names_its_divergences() -> None:
    import v8_next.analysis.reconcile as reconcile_module

    assert reconcile_module.EVALUATOR_VERSION == "regret-phase0-v1"
    assert reconcile_module.DIVERGENCES, "every deliberate non-port is named, not silently dropped"
    assert callable(reconcile_module.reconcile)


def test_the_ten_compared_fields_are_owned_by_the_outcome_surface() -> None:
    assert len(RECONCILE_EXACT_FIELDS) == 4
    assert len(RECONCILE_FLOAT_FIELDS) == 6
    surface = _replay("cid")
    compared = [getattr(surface, name) for name in (*RECONCILE_EXACT_FIELDS, *RECONCILE_FLOAT_FIELDS)]
    assert len(compared) == 10
    assert surface.mismatched_field(_replay("other")) is None, "identity is not compared"


# --------------------------------------------------------------------------------------
# build_snapshots
# --------------------------------------------------------------------------------------


def test_snapshot_binds_a_draft_to_the_detected_birth_clock() -> None:
    candidates = _entered_transitions("cid-1")
    evaluations = [_evaluation("fragile_expert", DETECTED_AT)]
    outcomes = [_observed("cid-1")]
    snaps = build_snapshots(candidates, evaluations, outcomes)
    assert len(snaps) == 1
    snap = snaps[0]
    assert snap.binding_status == BOUND
    assert snap.entry_bar_available_time == ENTRY_TIME
    assert snap.birth_state_id == "st-sol-1"
    assert snap.birth_time == DETECTED_AT
    assert snap.instrument == SYMBOL
    assert snap.direction == "LONG"
    assert snap.expert_id == "fragile_expert"
    assert snap.observed_outcome is not None
    assert snap.raw_draft is not None
    assert dict(snap.risk_geometry) == {"target_r": 1.5, "stop_r": 1.0, "expiry_bars": 8}


def test_a_candidate_without_a_draft_is_reported_not_dropped() -> None:
    candidates = [_detected_only_transition("cid-2", "ghost_expert")]
    snaps = build_snapshots(candidates, [], [])
    assert len(snaps) == 1
    snap = snaps[0]
    assert snap.binding_status == UNBOUND_NO_DRAFT
    assert snap.expert_id == "ghost_expert"
    assert snap.raw_draft is None
    assert snap.risk_geometry == {}
    assert snap.size is None, "an unmeasured admission size is None, never the Rust 1.0 constant"
    assert snap.entry_bar_available_time is None


def test_transitions_sort_by_sequence_for_detected_executed_terminal() -> None:
    candidates = _entered_transitions("cid-1")
    candidates.append(_transition("cid-1", 7, "CLOSED", 5000))
    candidates.reverse()
    snaps = build_snapshots(candidates, [_evaluation("fragile_expert", DETECTED_AT)], [])
    assert len(snaps) == 1
    assert snaps[0].entry_bar_available_time == ENTRY_TIME
    assert snaps[0].terminal_state == "CLOSED"


def test_the_birth_clock_join_keeps_two_episodes_distinct() -> None:
    first = _entered_transitions("cid-a") + []
    second = [
        _transition(
            "cid-b",
            1,
            "DETECTED",
            4000,
            expert_id="fragile_expert",
            instrument=SYMBOL,
            direction="LONG",
            state_id="st-sol-b",
        )
    ]
    evaluations = [
        _evaluation("fragile_expert", DETECTED_AT, draft=_draft("fragile_expert", size=1.0)),
        _evaluation("fragile_expert", 4000, draft=_draft("fragile_expert", size=2.0)),
    ]
    snaps = build_snapshots(first + second, evaluations, [])
    by_id = {snap.candidate_id: snap for snap in snaps}
    assert by_id["cid-a"].size == 1.0
    assert by_id["cid-b"].size == 2.0
    assert by_id["cid-b"].entry_bar_available_time is None


# --------------------------------------------------------------------------------------
# assert_pit_lineage — returns the named violation list, never a bool
# --------------------------------------------------------------------------------------


def test_pit_lineage_is_clean_when_states_resolve() -> None:
    snaps = build_snapshots(
        _entered_transitions("cid-1"),
        [_evaluation("fragile_expert", DETECTED_AT)],
        [],
    )
    problems = assert_pit_lineage([_state("st-sol-1", DETECTED_AT - 1)], snaps)
    assert problems == []
    assert isinstance(problems, list)


def test_pit_lineage_names_future_leakage_with_the_oracle_text() -> None:
    snaps = build_snapshots(
        _entered_transitions("cid-1"),
        [_evaluation("fragile_expert", DETECTED_AT)],
        [],
    )
    problems = assert_pit_lineage([_state("st-sol-1", 99999)], snaps)
    assert len(problems) == 1
    assert problems[0].endswith(
        "feature sma20 max_input_available_time 99999 > decision clock 3000 — future leakage"
    )


def test_pit_lineage_names_missing_and_unresolved_birth_states() -> None:
    snaps = [
        _snapshot("cid-a", birth_state_id="st-gone"),
        _snapshot("cid-b", birth_state_id=None),
    ]
    problems = assert_pit_lineage([_state("st-sol-1", DETECTED_AT - 1)], snaps)
    assert len(problems) == 2
    assert problems[0] == "cid-a: birth_state_id st-gone not found in states.jsonl"
    assert problems[1] == "cid-b: no birth_state_id recorded"


def test_pit_lineage_ignores_unbound_snapshots() -> None:
    snaps = [_snapshot("cid-a", binding=UNBOUND_NO_DRAFT, birth_state_id=None)]
    assert assert_pit_lineage([], snaps) == []


def test_pit_lineage_names_an_unverifiable_clock_instead_of_defaulting_to_zero() -> None:
    state = {
        "state_id": "st-sol-1",
        "features": {"sma20": {"max_input_available_time": 99999}},
    }
    problems = assert_pit_lineage([state], [_snapshot("cid-a")])
    assert problems == ["cid-a: state st-sol-1 has no as_of decision clock — lineage unverifiable"]

    feature_without_clock = {"state_id": "st-sol-1", "as_of": DETECTED_AT, "features": {"sma20": {}}}
    problems = assert_pit_lineage([feature_without_clock], [_snapshot("cid-a")])
    assert problems == [
        "cid-a: feature sma20 has no max_input_available_time — lineage unverifiable"
    ]


# --------------------------------------------------------------------------------------
# reconcile_actual_actions — counters, verdicts, mismatch attribution
# --------------------------------------------------------------------------------------


def test_a_matching_pair_reconciles_and_separates_not_applicable() -> None:
    snaps = [
        _snapshot("cid-1", observed=_observed("cid-1")),
        _snapshot("cid-2", entry=None, observed=None),
        _snapshot("cid-3", binding=UNBOUND_NO_DRAFT, observed=_observed("cid-3")),
    ]
    result = reconcile_actual_actions(snaps, {"cid-1": _replay("cid-1")})
    assert result.n_executed == 1
    assert result.n_reconciled == 1
    assert result.n_mismatched == 0
    assert result.n_not_applicable == 2
    assert result.verdict == RECONCILED
    assert result.mismatches == ()
    assert result.measurement_status == "MEASURED"
    for name in RECONCILE_FLOAT_FIELDS:
        assert result.max_abs_deviation[name] == 0.0


def test_a_corrupted_field_fails_reconciliation_and_is_attributed_to_the_field() -> None:
    snaps = [_snapshot("cid-1", observed=_observed("cid-1", net_r=2.0))]
    result = reconcile_actual_actions(snaps, {"cid-1": _replay("cid-1")})
    assert result.verdict == RECONCILIATION_FAILED
    assert result.n_executed == 1
    assert result.n_reconciled == 0
    assert result.n_mismatched == 1
    assert len(result.mismatches) == 1
    candidate_id, reason = result.mismatches[0]
    assert candidate_id == "cid-1"
    assert reason.startswith("field_mismatch:")
    assert "net_r:rust=1.500000000:obs=2.000000000" in reason
    assert reason.endswith("atr=None")
    assert result.max_abs_deviation["net_r"] == 0.5
    for name in RECONCILE_FLOAT_FIELDS:
        if name != "net_r":
            assert result.max_abs_deviation[name] == 0.0


@pytest.mark.parametrize("field", RECONCILE_EXACT_FIELDS)
def test_each_exact_field_carries_its_own_mismatch_attribution(field: str) -> None:
    changed: Any = {"endpoint": "STOP", "label_status": "RIGHT_CENSORED"}.get(field, 9)
    snaps = [_snapshot("cid-1", observed=_observed("cid-1", **{field: changed}))]
    result = reconcile_actual_actions(snaps, {"cid-1": _replay("cid-1")})
    assert result.verdict == RECONCILIATION_FAILED
    assert f":{field}:atr=" in result.mismatches[0][1]


@pytest.mark.parametrize("field", RECONCILE_FLOAT_FIELDS)
def test_each_float_field_carries_its_own_mismatch_attribution(field: str) -> None:
    snaps = [_snapshot("cid-1", observed=_observed("cid-1", **{field: 123.0}))]
    result = reconcile_actual_actions(snaps, {"cid-1": _replay("cid-1")})
    assert result.verdict == RECONCILIATION_FAILED
    assert f"{field}:rust=" in result.mismatches[0][1]


def test_an_absent_observed_outcome_is_named_entry_bar_or_outcome_missing() -> None:
    snaps = [_snapshot("cid-1", observed=None)]
    result = reconcile_actual_actions(snaps, {"cid-1": _replay("cid-1")})
    assert result.n_executed == 1
    assert result.n_mismatched == 1
    assert result.mismatches == (("cid-1", MISMATCH_REASON_ENTRY_MISSING),)
    assert result.verdict == RECONCILIATION_FAILED


def test_an_absent_replay_is_named_entry_bar_or_outcome_missing() -> None:
    snaps = [_snapshot("cid-1", observed=_observed("cid-1"))]
    result = reconcile_actual_actions(snaps, {})
    assert result.n_mismatched == 1
    assert result.mismatches == (("cid-1", MISMATCH_REASON_ENTRY_MISSING),)


def test_a_partially_written_outcome_is_refused_not_read_as_zero() -> None:
    partial = _observed("cid-1")
    del partial["mfe_r"]
    snaps = [_snapshot("cid-1", observed=partial)]
    result = reconcile_actual_actions(snaps, {"cid-1": _replay("cid-1")})
    assert result.mismatches == (("cid-1", MISMATCH_REASON_ENTRY_MISSING),)
    assert result.max_abs_deviation["mfe_r"] is None


def test_integral_float_exact_fields_still_compare_equal() -> None:
    snaps = [_snapshot("cid-1", observed=_observed("cid-1", horizon_bars=1.0))]
    result = reconcile_actual_actions(snaps, {"cid-1": _replay("cid-1")})
    assert result.n_reconciled == 1


def test_a_within_tolerance_float_still_reconciles() -> None:
    snaps = [_snapshot("cid-1", observed=_observed("cid-1", net_r=1.5 + 1e-13))]
    result = reconcile_actual_actions(snaps, {"cid-1": _replay("cid-1")})
    assert result.verdict == RECONCILED


def test_nothing_compared_is_not_measured_never_a_vacuous_reconciled() -> None:
    snaps = [_snapshot("cid-1", entry=None, observed=None)]
    result = reconcile_actual_actions(snaps, {})
    assert result.n_executed == 0
    assert result.verdict == RECONCILIATION_NOT_MEASURED
    assert result.verdict != RECONCILED
    assert result.measurement_status == "NOT_MEASURED_NO_COMPARED_CANDIDATE"
    for name in RECONCILE_FLOAT_FIELDS:
        assert result.max_abs_deviation[name] is None


def test_an_empty_ledger_is_not_measured() -> None:
    result = reconcile_actual_actions([], {})
    assert result.verdict == RECONCILIATION_NOT_MEASURED
    assert result.n_not_applicable == 0


# --------------------------------------------------------------------------------------
# ReconcileRequest / reconcile / artifacts
# --------------------------------------------------------------------------------------


def _clean_request() -> ReconcileRequest:
    return ReconcileRequest(
        candidates=tuple(_entered_transitions("cid-1") + [_detected_only_transition("cid-2", "ghost")]),
        evaluations=(
            _evaluation("fragile_expert", DETECTED_AT),
            _evaluation("ghost", DETECTED_AT, draft=_draft("ghost", setup_anchor_event_id="sol-setup-2")),
        ),
        outcomes=(_observed("cid-1"),),
        states=(_state("st-sol-1", DETECTED_AT - 1), _state("st-sol-2", DETECTED_AT - 1)),
        replays={"cid-1": _replay("cid-1")},
    )


def test_reconcile_summary_is_oracle_shaped_and_does_not_halt_when_clean() -> None:
    summary = reconcile(_clean_request())
    assert summary["evaluator_version"] == "regret-phase0-v1"
    assert summary["n_candidates"] == 2
    assert summary["n_unbound"] == 0
    assert summary["pit_lineage_problems"] == []
    assert summary["reconciliation"]["verdict"] == RECONCILED
    assert summary["reconciliation"]["n_executed"] == 1
    assert summary["halted"] is False
    assert "halt_reason" not in summary
    assert summary["claim"] == "NO_ECONOMIC_CLAIM"


def test_reconcile_halts_on_a_pit_lineage_violation() -> None:
    request = _clean_request()
    leaky = ReconcileRequest(
        candidates=request.candidates,
        evaluations=request.evaluations,
        outcomes=request.outcomes,
        states=(_state("st-sol-1", DETECTED_AT - 1), _state("st-sol-2", 99999)),
        replays=request.replays,
    )
    summary = reconcile(leaky)
    assert summary["halted"] is True
    assert summary["halt_reason"] == (
        "PIT lineage violation detected — future leakage, refusing to proceed"
    )
    # The verdict is untouched by lineage (the summary halts separately).
    assert summary["reconciliation"]["verdict"] == RECONCILED


def test_reconcile_halts_when_nothing_was_measured() -> None:
    summary = reconcile(ReconcileRequest())
    assert summary["halted"] is True
    assert summary["reconciliation"]["verdict"] == RECONCILIATION_NOT_MEASURED
    assert "not measured" in summary["halt_reason"]


def test_reconcile_artifact_carries_only_the_six_counters_plus_status() -> None:
    request = _clean_request()
    snapshots = build_snapshots(request.candidates, request.evaluations, request.outcomes)
    result = reconcile_actual_actions(snapshots, request.replays)
    artifact = reconciliation_artifact(result)
    assert set(artifact) == {
        "n_executed",
        "n_reconciled",
        "n_mismatched",
        "n_not_applicable",
        "max_abs_deviation",
        "verdict",
        "measurement_status",
        "claim",
    }
    assert artifact["verdict"] == RECONCILED
    assert "mismatches" not in artifact, "the artifact deliberately excludes the mismatch detail"
