"""M2-04: unified recurrent ``TraderDecisionPolicy`` in shadow mode.

Every scenario asserts an actually exercised action. The policy is dependency
injected with duck-typed views, so these fixtures are the same shape the live
belief ledger and opportunity book will provide.
"""

from __future__ import annotations

import ast
import itertools
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from v8_next.domain.decision import (
    DecisionAction,
    IntentKind,
    OpportunityLifecycle,
    ReasonCode,
    TraderDecision,
    VenueOrderStatus,
    derive_decision_id,
)
from v8_next.economics import policy as policy_module
from v8_next.economics.legality import PositionPresence
from v8_next.economics.policy import (
    POLICY_HASH,
    POLICY_VERSION,
    CapacityState,
    DecisionRequest,
    PolicyExplanation,
    PortfolioState,
    PositionState,
    TraderDecisionPolicy,
)

NS = 1_700_000_000_000_000_000
INSTRUMENT = "BTCUSDT-PERP.BINANCE"
OPPORTUNITY = "opp-0001"

EXPECTED_INTENT = {
    DecisionAction.WAIT: IntentKind.NONE,
    DecisionAction.ABSTAIN: IntentKind.NONE,
    DecisionAction.ENTER: IntentKind.LIMIT_ENTRY,
    DecisionAction.HOLD: IntentKind.PROTECTIVE,
    DecisionAction.REDUCE: IntentKind.REDUCE,
    DecisionAction.EXIT: IntentKind.EXIT,
}
NO_ACTION = frozenset({DecisionAction.WAIT, DecisionAction.ABSTAIN})


# ------------------------------------------------------------------------- fixtures


@dataclass(frozen=True)
class FakeBelief:
    version_hash: str = "belief-a"
    regime: str = "TREND_UP"
    uncertainty: Decimal = Decimal("0.20")
    stale: bool = False
    abstain: bool = False
    as_of_ns: int | None = NS - 1_000_000


@dataclass(frozen=True)
class FakeOpportunity:
    opportunity_id: str = OPPORTUNITY
    lifecycle: OpportunityLifecycle = OpportunityLifecycle.ARMED
    as_of_ns: int | None = NS - 1_000_000


@dataclass(frozen=True)
class FakeExpert:
    expert_id: str
    stance: str
    weight: Decimal


def expert(expert_id: str, stance: str, weight: str) -> FakeExpert:
    return FakeExpert(expert_id=expert_id, stance=stance, weight=Decimal(weight))


def flat() -> PositionState:
    return PositionState(presence=PositionPresence.NONE)


def long_position(exposure: str = "10000") -> PositionState:
    return PositionState(
        presence=PositionPresence.OPEN,
        quantity=Decimal("1"),
        exposure=Decimal(exposure),
    )


def portfolio(
    equity: str = "100000",
    committed: str = "0",
    capacity: CapacityState = CapacityState.AVAILABLE,
) -> PortfolioState:
    return PortfolioState(
        equity=Decimal(equity),
        committed_exposure=Decimal(committed),
        capacity=capacity,
    )


def make_request(**overrides: Any) -> DecisionRequest:
    base: dict[str, Any] = {
        "decision_time_ns": NS,
        "instrument_id": INSTRUMENT,
        "venue_status": VenueOrderStatus.REJECTED,
        "position": flat(),
        "portfolio": portfolio(),
        "opportunity": FakeOpportunity(),
        "belief": FakeBelief(),
        "expert_stances": (expert("e1", "SUPPORT", "0.90"),),
    }
    base.update(overrides)
    return DecisionRequest(**base)


def decide(**overrides: Any) -> TraderDecision:
    return TraderDecisionPolicy().evaluate(make_request(**overrides))


def explain(**overrides: Any) -> PolicyExplanation:
    return TraderDecisionPolicy().explain(make_request(**overrides))


# ---------------------------------------------------------------- six typed actions

SCENARIOS = [
    pytest.param(
        {"expert_stances": (expert("e1", "SUPPORT", "0.30"),)},
        DecisionAction.WAIT,
        ReasonCode.EVIDENCE_INSUFFICIENT,
        id="WAIT-insufficient-support",
    ),
    pytest.param(
        {"expert_stances": (expert("e1", "SUPPORT", "0.90"),)},
        DecisionAction.ENTER,
        ReasonCode.OPPORTUNITY_ARMED,
        id="ENTER-armed-flat",
    ),
    pytest.param(
        {
            "position": long_position(),
            "venue_status": VenueOrderStatus.FILLED,
            "expert_stances": (expert("e1", "SUPPORT", "0.90"),),
        },
        DecisionAction.HOLD,
        ReasonCode.POSITION_OPEN,
        id="HOLD-supported-open",
    ),
    pytest.param(
        {
            "position": long_position(),
            "venue_status": VenueOrderStatus.FILLED,
            "expert_stances": (
                expert("e1", "SUPPORT", "0.20"),
                expert("e2", "CONTRADICT", "0.40"),
            ),
        },
        DecisionAction.REDUCE,
        ReasonCode.POSITION_REDUCE_REQUIRED,
        id="REDUCE-contradicted-open",
    ),
    pytest.param(
        {
            "position": long_position(),
            "venue_status": VenueOrderStatus.FILLED,
            "expert_stances": (
                expert("e1", "SUPPORT", "0.10"),
                expert("e2", "CONTRADICT", "0.90"),
            ),
        },
        DecisionAction.EXIT,
        ReasonCode.POSITION_EXIT_REQUIRED,
        id="EXIT-belief-against-position",
    ),
    pytest.param(
        {"belief": FakeBelief(stale=True)},
        DecisionAction.ABSTAIN,
        ReasonCode.BELIEF_STALE,
        id="ABSTAIN-stale-belief",
    ),
]


@pytest.mark.parametrize(("overrides", "expected", "expected_reason"), SCENARIOS)
def test_all_six_actions_are_reachable(
    overrides: dict[str, Any],
    expected: DecisionAction,
    expected_reason: ReasonCode,
) -> None:
    decision = decide(**overrides)
    explanation = explain(**overrides)

    assert decision.action is expected
    assert decision.intent_kind is EXPECTED_INTENT[expected]
    assert decision.advisory_only is True
    assert decision.policy_version == POLICY_VERSION
    assert expected_reason in decision.reason_codes
    assert decision.decision_id == derive_decision_id(
        decision_time_ns=decision.decision_time_ns,
        instrument_id=decision.instrument_id,
        opportunity_id=decision.opportunity_id,
        action=decision.action,
        policy_version=decision.policy_version,
    )
    # The explanation is the same decision, fully attributed.
    assert explanation.decision_id == decision.decision_id
    assert explanation.action is expected
    assert explanation.reason_codes == decision.reason_codes
    assert expected in explanation.legal_actions
    assert expected in explanation.candidates or expected is DecisionAction.ABSTAIN
    assert explanation.policy_hash == POLICY_HASH


def test_every_decision_action_is_reachable_in_one_policy() -> None:
    reached = {decide(**scenario.values[0]).action for scenario in SCENARIOS}
    assert reached == set(DecisionAction)


# ---------------------------------------------------------------- deterministic


def test_replay_mints_identical_decisions_and_explanations() -> None:
    first = decide()
    second = decide()
    assert first == second
    assert explain() == explain()
    assert first.decision_id == second.decision_id


def test_expert_order_does_not_change_the_identity() -> None:
    stances = (
        expert("e1", "SUPPORT", "0.30"),
        expert("e2", "SUPPORT", "0.40"),
        expert("e3", "CONTRADICT", "0.10"),
    )
    outcomes = {
        decide(expert_stances=ordering).decision_id for ordering in itertools.permutations(stances)
    }
    assert len(outcomes) == 1


def test_reason_codes_are_canonical() -> None:
    decision = decide()
    assert tuple(sorted(set(decision.reason_codes))) == decision.reason_codes
    assert all(isinstance(code, ReasonCode) for code in decision.reason_codes)


# ---------------------------------------------------------------- fail closed


def test_stale_belief_never_enters_even_with_max_support() -> None:
    decision = decide(
        belief=FakeBelief(stale=True), expert_stances=(expert("e1", "SUPPORT", "1.0"),)
    )
    explanation = explain(
        belief=FakeBelief(stale=True), expert_stances=(expert("e1", "SUPPORT", "1.0"),)
    )
    assert decision.action is DecisionAction.ABSTAIN
    assert ReasonCode.BELIEF_STALE in decision.reason_codes
    assert DecisionAction.ENTER not in explanation.candidates
    assert DecisionAction.ENTER not in explanation.legal_actions


@pytest.mark.parametrize(
    "overrides",
    [
        {"expert_stances": (expert("e1", "SUPPORT", "1.0"),)},
        {
            "position": long_position(),
            "venue_status": VenueOrderStatus.FILLED,
            "expert_stances": (expert("e1", "SUPPORT", "0.90"),),
        },
        {
            "position": long_position(),
            "venue_status": VenueOrderStatus.FILLED,
            "expert_stances": (
                expert("e1", "SUPPORT", "0.10"),
                expert("e2", "CONTRADICT", "0.90"),
            ),
        },
        {"belief": FakeBelief(stale=True)},
    ],
)
def test_unknown_venue_allows_only_wait_or_abstain(overrides: dict[str, Any]) -> None:
    merged = {**overrides, "venue_status": VenueOrderStatus.UNKNOWN}
    decision = decide(**merged)
    explanation = explain(**merged)
    assert decision.action in NO_ACTION
    assert set(explanation.legal_actions) <= NO_ACTION
    assert ReasonCode.VENUE_UNKNOWN in decision.reason_codes


def test_missing_opportunity_abstains_instead_of_entering() -> None:
    decision = decide(opportunity=None)
    assert decision.action is DecisionAction.ABSTAIN
    assert ReasonCode.OPPORTUNITY_NOT_ARMED in decision.reason_codes
    assert decision.opportunity_id is None


def test_unarmed_opportunity_waits_without_entering() -> None:
    decision = decide(opportunity=FakeOpportunity(lifecycle=OpportunityLifecycle.WATCHING))
    assert decision.action is DecisionAction.WAIT
    assert ReasonCode.OPPORTUNITY_NOT_ARMED in decision.reason_codes


def test_uncertain_position_abstains() -> None:
    decision = decide(position=PositionState(presence=PositionPresence.UNCERTAIN))
    assert decision.action is DecisionAction.ABSTAIN
    assert ReasonCode.POSITION_UNCERTAIN in decision.reason_codes


def test_missing_belief_abstains() -> None:
    decision = decide(belief=None)
    assert decision.action is DecisionAction.ABSTAIN
    assert ReasonCode.BELIEF_MISSING in decision.reason_codes
    assert decision.belief_version_hash is None
    assert explain(belief=None).uncertainty is None


@pytest.mark.parametrize(
    "belief",
    [
        FakeBelief(uncertainty=0.2),  # type: ignore[arg-type]
        FakeBelief(regime="UNKNOWN"),
        FakeBelief(version_hash="   "),
        FakeBelief(stale="yes"),  # type: ignore[arg-type]
        FakeBelief(uncertainty=Decimal("1.4")),
    ],
)
def test_malformed_belief_fails_closed(belief: object) -> None:
    decision = decide(belief=belief)
    assert decision.action is DecisionAction.ABSTAIN
    assert ReasonCode.BELIEF_MISSING in decision.reason_codes


def test_future_dated_belief_abstains_as_pit_violation() -> None:
    decision = decide(belief=FakeBelief(as_of_ns=NS + 1))
    assert decision.action is DecisionAction.ABSTAIN
    assert ReasonCode.INPUT_FUTURE in decision.reason_codes


def test_future_dated_opportunity_abstains_as_pit_violation() -> None:
    decision = decide(opportunity=FakeOpportunity(as_of_ns=NS + 1))
    assert decision.action is DecisionAction.ABSTAIN
    assert ReasonCode.INPUT_FUTURE in decision.reason_codes


@pytest.mark.parametrize(
    ("capacity", "expected_reason"),
    [
        (CapacityState.UNKNOWN, ReasonCode.RISK_UNKNOWN),
        (CapacityState.FULL, ReasonCode.RISK_DENIED),
    ],
)
def test_unknown_or_full_capacity_blocks_entry(
    capacity: CapacityState, expected_reason: ReasonCode
) -> None:
    decision = decide(portfolio=portfolio(capacity=capacity))
    assert decision.action is DecisionAction.WAIT
    assert expected_reason in decision.reason_codes


def test_high_uncertainty_blocks_entry() -> None:
    decision = decide(belief=FakeBelief(uncertainty=Decimal("0.90")))
    assert decision.action is DecisionAction.WAIT
    assert ReasonCode.EVIDENCE_INSUFFICIENT in decision.reason_codes


def test_conflicting_expert_panel_never_enters() -> None:
    stances = (expert("e1", "SUPPORT", "0.50"), expert("e2", "CONTRADICT", "0.50"))
    decision = decide(expert_stances=stances)
    explanation = explain(expert_stances=stances)
    assert decision.action is DecisionAction.WAIT
    assert ReasonCode.EVIDENCE_CONFLICT in decision.reason_codes
    assert DecisionAction.ENTER not in explanation.candidates


def test_unreadable_stance_abstains() -> None:
    decision = decide(expert_stances=(expert("e1", "MAYBE", "0.90"),))
    assert decision.action is DecisionAction.ABSTAIN
    assert ReasonCode.EVIDENCE_CONFLICT in decision.reason_codes


def test_duplicate_expert_identity_abstains() -> None:
    stances = (expert("e1", "SUPPORT", "0.90"), expert("e1", "CONTRADICT", "0.10"))
    decision = decide(expert_stances=stances)
    assert decision.action is DecisionAction.ABSTAIN
    assert ReasonCode.EVIDENCE_CONFLICT in decision.reason_codes


def test_gross_exposure_over_budget_forces_reduce() -> None:
    decision = decide(
        position=long_position(exposure="95000"),
        venue_status=VenueOrderStatus.FILLED,
        expert_stances=(expert("e1", "SUPPORT", "0.90"),),
    )
    explanation = explain(
        position=long_position(exposure="95000"),
        venue_status=VenueOrderStatus.FILLED,
        expert_stances=(expert("e1", "SUPPORT", "0.90"),),
    )
    assert decision.action is DecisionAction.REDUCE
    assert ReasonCode.POSITION_REDUCE_REQUIRED in decision.reason_codes
    assert "GROSS_EXPOSURE_EXCEEDED" in explanation.constraints


def test_flat_belief_contradiction_abstains_instead_of_trading() -> None:
    stances = (expert("e1", "SUPPORT", "0.10"), expert("e2", "CONTRADICT", "0.90"))
    decision = decide(expert_stances=stances)
    assert decision.action is DecisionAction.ABSTAIN
    assert ReasonCode.BELIEF_CONTRADICTORY in decision.reason_codes


# ---------------------------------------------------------------- explainability


def test_explanation_reports_evidence_and_constraints() -> None:
    explanation = explain(
        expert_stances=(expert("e1", "SUPPORT", "0.60"), expert("e2", "SUPPORT", "0.20"))
    )
    assert explanation.support_weight == Decimal("0.80")
    assert explanation.contradict_weight == Decimal("0")
    assert explanation.net_evidence_ratio == Decimal("1.0000")
    assert explanation.regime == "TREND_UP"
    assert explanation.lifecycle is OpportunityLifecycle.ARMED
    assert explanation.opportunity_id == OPPORTUNITY
    assert explanation.armed is True
    assert explanation.stale is False
    assert explanation.constraints == tuple(sorted(explanation.constraints))


def test_constraints_name_the_blocking_gate() -> None:
    explanation = explain(position=PositionState(presence=PositionPresence.UNCERTAIN))
    assert "POSITION_UNCERTAIN" in explanation.constraints
    assert explanation.action is DecisionAction.ABSTAIN


# ---------------------------------------------------------------- envelope & shape


@pytest.mark.parametrize(
    "field",
    ["decision_time_ns", "instrument_id"],
)
def test_malformed_envelope_is_refused(field: str) -> None:
    bad = -1 if field == "decision_time_ns" else "  "
    with pytest.raises(ValueError):
        make_request(**{field: bad})


def test_float_money_is_refused() -> None:
    with pytest.raises(ValidationError):
        PortfolioState(equity=1.5)  # type: ignore[arg-type]


def test_inconsistent_position_is_refused() -> None:
    with pytest.raises(ValidationError):
        PositionState(presence=PositionPresence.NONE, exposure=Decimal("10"))


# ---------------------------------------------------------------- no order path


def _module_imports(module: object) -> set[str]:
    path = Path(module.__file__)  # type: ignore[attr-defined]
    tree = ast.parse(path.read_text())
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def _called_attribute_names(module: object) -> set[str]:
    path = Path(module.__file__)  # type: ignore[attr-defined]
    tree = ast.parse(path.read_text())
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            names.add(node.func.attr)
    return names


@pytest.mark.parametrize("module", [policy_module])
def test_no_execution_adapter_is_imported(module: object) -> None:
    imports = _module_imports(module)
    assert not any("nautilus" in name for name in imports)
    assert not any("adapter" in name for name in imports)
    assert "v8_next.belief" not in imports


@pytest.mark.parametrize("module", [policy_module])
def test_no_order_verb_is_called(module: object) -> None:
    forbidden = {"submit", "submit_order", "place_order", "amend_order", "cancel_order"}
    assert not (_called_attribute_names(module) & forbidden)


def test_output_is_structurally_advisory() -> None:
    decision = decide()
    assert decision.advisory_only is True
    assert TraderDecisionPolicy().shadow_mode is True
    fields = set(type(decision).model_fields)
    for forbidden in ("quantity", "price", "side", "order_type", "venue_order_id"):
        assert forbidden not in fields
    with pytest.raises(ValidationError):
        TraderDecision(
            decision_id=derive_decision_id(
                decision_time_ns=NS,
                instrument_id=INSTRUMENT,
                opportunity_id=OPPORTUNITY,
                action=DecisionAction.ENTER,
                policy_version=POLICY_VERSION,
            ),
            decision_time_ns=NS,
            instrument_id=INSTRUMENT,
            opportunity_id=OPPORTUNITY,
            action=DecisionAction.ENTER,
            intent_kind=IntentKind.LIMIT_ENTRY,
            policy_version=POLICY_VERSION,
            advisory_only=False,  # type: ignore[arg-type]
        )
