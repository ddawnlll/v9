"""MECHANICS ONLY: stress diagnostics must not certify selection history."""

import pytest

from v8_next.evaluation import gate_resolution
from v8_next.evaluation.benchmark_receipt import GateState
from v8_next.evaluation.statistics_plan import CANONICAL_G5_BLOCK_SIZE, SelectionFamily, g5_plan


def test_unbound_hundred_trial_family_cannot_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    def dsr(*args: object, **kwargs: object) -> dict:
        return {"dsr_confidence": 0.999, "multiple_testing": {
            "raw_pvalues": {"champion": 0.001},
            "adjustments": {"bonferroni": {"adjusted_pvalues": {"champion": 0.004}}},
        }}

    monkeypatch.setattr(gate_resolution, "deflated_sharpe_diagnostic", dsr)
    monkeypatch.setattr(gate_resolution, "reality_check_diagnostic", lambda *a, **k: {"p_value": 0.01})
    plan = g5_plan("selected-history", pinned_ns=1, block_size=CANONICAL_G5_BLOCK_SIZE,
                   reps=20, seed=1, multiplicity_trials=100, effective_independent_trials=10,
                   independence_basis="declared, not evidence of selection membership")
    state, metrics = gate_resolution.evaluate_g5_selection_control(
        [0.03, 0.01, 0.02, 0.04, 0.01] * 6, plan=plan
    )
    assert state == GateState.BLOCKED
    assert metrics["stress_diagnostic_passed"] is True
    assert metrics["trials"] == 100
    assert len(metrics["diagnostic_family"]) == 4
    assert metrics["wrc_family"] == ["champion"]
    assert metrics["selection_family_status"] == "MISSING_PROVENANCE_BOUND_SELECTION_HISTORY"


def test_provenance_bound_family_is_the_only_pass_route() -> None:
    family = SelectionFamily(
        family_id="mechanics-preregistered-search",
        members=("champion", "fee_stressed", "conservative", "slippage_stressed"),
        registered_ns=1,
        basis="MECHANICS ONLY: fixture, not an economic selection history",
    )
    plan = g5_plan("bound", pinned_ns=1, block_size=CANONICAL_G5_BLOCK_SIZE,
                   reps=20, seed=1, multiplicity_trials=4, effective_independent_trials=4)
    assert family.identity().startswith("sha256:")
    assert len(family.as_dict()["members"]) == 4
    # A family narrower than the pinned multiplicity cannot be corrected for.
    narrow = SelectionFamily("narrow", ("champion",), 1, "fixture")
    with pytest.raises(ValueError, match="unique"):
        SelectionFamily("dupes", ("a", "a"), 1, "fixture")
    with pytest.raises(ValueError, match="registration"):
        SelectionFamily("bad", ("a",), 0, "fixture")
    assert narrow.identity() != family.identity()
    assert plan.multiplicity_trials == len(family.members)


def test_family_size_must_match_pinned_multiplicity(monkeypatch: pytest.MonkeyPatch) -> None:
    def dsr(*args: object, **kwargs: object) -> dict:
        return {"dsr_confidence": 0.999, "multiple_testing": {
            "raw_pvalues": {"champion": 0.001},
            "adjustments": {"bonferroni": {"adjusted_pvalues": {"champion": 0.004}}},
        }}

    monkeypatch.setattr(gate_resolution, "deflated_sharpe_diagnostic", dsr)
    monkeypatch.setattr(gate_resolution, "reality_check_diagnostic", lambda *a, **k: {"p_value": 0.01})
    plan = g5_plan("bound", pinned_ns=1, block_size=CANONICAL_G5_BLOCK_SIZE,
                   reps=20, seed=1, multiplicity_trials=4, effective_independent_trials=4)
    one_member = SelectionFamily("narrow", ("champion",), 1, "fixture")
    state, metrics = gate_resolution.evaluate_g5_selection_control(
        [0.03, 0.01, 0.02, 0.04, 0.01] * 6, plan=plan, selection_family=one_member
    )
    assert state == GateState.BLOCKED
    assert metrics["selection_family_status"] == "FAMILY_SIZE_DISAGREES_WITH_PINNED_MULTIPLICITY"


def test_contradictory_independent_trial_count_is_rejected() -> None:
    with pytest.raises(ValueError, match="effective independent"):
        g5_plan("bad", pinned_ns=1, block_size=1, reps=20, seed=1,
                multiplicity_trials=1, effective_independent_trials=2)
