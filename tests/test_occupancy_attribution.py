"""M3-04: factual occupancy attribution and separated counterfactual performance.

The fixtures are deterministic multi-opportunity portfolios. Each test proves one
of the acceptance conditions: capacity-limited admission with a reasoned loser,
no double spending, realized PnL strictly apart from hypothetical PnL, and
fail-closed behaviour for unknown capacity.
"""

from decimal import Decimal

import pytest
from pydantic import ValidationError

from v8_next.economics.legality import PositionPresence
from v8_next.economics.occupancy import (
    CapacityState,
    CounterfactualNotApplicableError,
    CounterfactualOnAdmittedError,
    DuplicateCounterfactualError,
    DuplicateRealizationError,
    NotAdmittedError,
    OccupancyEntry,
    OccupancyLedger,
    OccupancyOutcome,
    OccupancyReason,
    OccupancyRequest,
    UnknownOpportunityError,
)
from v8_next.opportunities.models import OpportunityStatus

D = Decimal
INSTRUMENT = "BTCUSDT-PERP.BINANCE"


def capacity(
    *,
    equity: str = "1000",
    committed: str | None = "0",
    reserved: str = "0",
    risk_budget: str | None = None,
    position: PositionPresence = PositionPresence.NONE,
    position_notional: str | None = "0",
    as_of: int = 100,
    available_at: int = 100,
    source: str = "test-capacity",
) -> CapacityState:
    return CapacityState(
        as_of_ns=as_of,
        available_at_ns=available_at,
        source=source,
        position=position,
        equity=D(equity) if equity is not None else None,
        committed_notional=D(committed) if committed is not None else None,
        reserved_notional=D(reserved),
        risk_budget_notional=D(risk_budget) if risk_budget is not None else None,
        position_notional=D(position_notional) if position_notional is not None else None,
    )


def request(
    opportunity_id: str,
    notional: str = "600",
    *,
    expires_at: int = 10_000,
    status: OpportunityStatus = OpportunityStatus.CANDIDATE,
) -> OccupancyRequest:
    return OccupancyRequest(
        opportunity_id=opportunity_id,
        instrument_id=INSTRUMENT,
        requested_notional=D(notional),
        expires_at_ns=expires_at,
        status=status,
    )


# ------------------------------------------------- capacity-limited admission


def test_capacity_limited_admission_blocks_the_loser_with_a_reason():
    ledger = OccupancyLedger()
    winner = ledger.evaluate(request("A", "600"), capacity(), decision_time_ns=100)
    loser = ledger.evaluate(request("B", "600"), capacity(), decision_time_ns=101)

    assert winner.outcome is OccupancyOutcome.ADMITTED
    assert winner.reason is OccupancyReason.CAPACITY_AVAILABLE
    assert winner.committed_after == D("600")
    assert winner.available_capacity_after == D("400")

    assert loser.outcome is OccupancyOutcome.OCCUPIED_CAPACITY
    assert loser.reason is OccupancyReason.CAPACITY_CONSUMED
    assert loser.committed_before == loser.committed_after == D("600")
    assert loser.exposure_before == loser.exposure_after == D("600")
    assert loser.available_capacity_after == D("400")

    assert ledger.committed_notional == D("600")
    assert [entry.opportunity_id for entry in ledger.matched_alternatives()] == ["B"]
    assert [entry.entry_id for entry in ledger.decision_log()] == [
        winner.entry_id,
        loser.entry_id,
    ]


def test_multi_opportunity_fixture_never_double_spends():
    ledger = OccupancyLedger()
    outcomes = [
        ledger.evaluate(request("A", "600"), capacity(), decision_time_ns=100),
        ledger.evaluate(request("B", "600"), capacity(), decision_time_ns=101),
        ledger.evaluate(request("C", "300"), capacity(), decision_time_ns=102),
        ledger.evaluate(request("D", "200"), capacity(), decision_time_ns=103),
    ]

    assert [entry.outcome for entry in outcomes] == [
        OccupancyOutcome.ADMITTED,
        OccupancyOutcome.OCCUPIED_CAPACITY,
        OccupancyOutcome.ADMITTED,
        OccupancyOutcome.OCCUPIED_CAPACITY,
    ]
    admitted = [entry for entry in outcomes if entry.outcome.is_admitted]
    assert sum((entry.requested_notional for entry in admitted), D(0)) == D("900")
    assert ledger.committed_notional == D("900")
    assert all(entry.committed_after <= D("1000") for entry in outcomes)
    ledger.assert_no_double_spend()


def test_duplicate_evaluation_cannot_double_count_capital():
    ledger = OccupancyLedger()
    first = ledger.evaluate(request("A", "600"), capacity(), decision_time_ns=100)
    duplicate = ledger.evaluate(request("A", "600"), capacity(), decision_time_ns=100)

    assert first.outcome is OccupancyOutcome.ADMITTED
    assert duplicate.outcome is OccupancyOutcome.OCCUPIED_CAPACITY
    assert duplicate.reason is OccupancyReason.DUPLICATE_OPPORTUNITY
    assert duplicate.committed_after == D("600")
    assert ledger.committed_notional == D("600")

    later = ledger.evaluate(request("A", "600"), capacity(), decision_time_ns=200)
    assert later.outcome is OccupancyOutcome.OCCUPIED_CAPACITY
    assert later.reason is OccupancyReason.DUPLICATE_OPPORTUNITY
    assert ledger.committed_notional == D("600")


# ----------------------------------------------------- position and risk blocks


def test_open_and_pending_positions_block_without_reserving_capital():
    ledger = OccupancyLedger()
    open_entry = ledger.evaluate(
        request("A", "100"),
        capacity(position=PositionPresence.OPEN, position_notional="500"),
        decision_time_ns=100,
    )
    pending_entry = ledger.evaluate(
        request("B", "100"),
        capacity(position=PositionPresence.PENDING_INTENT, position_notional="0"),
        decision_time_ns=101,
    )

    assert open_entry.outcome is OccupancyOutcome.BLOCKED_POSITION
    assert open_entry.reason is OccupancyReason.POSITION_OPEN
    assert open_entry.exposure_before == D("500")
    assert open_entry.committed_after == D("0")

    assert pending_entry.outcome is OccupancyOutcome.BLOCKED_POSITION
    assert pending_entry.reason is OccupancyReason.POSITION_PENDING
    assert ledger.committed_notional == D("0")
    assert all(
        entry.outcome is not OccupancyOutcome.ADMITTED for entry in ledger.matched_alternatives()
    )


def test_risk_reservation_blocks_the_request_that_would_breach_the_budget():
    ledger = OccupancyLedger()
    reserved = capacity(equity="5000", reserved="850", risk_budget="900")

    blocked = ledger.evaluate(request("A", "100"), reserved, decision_time_ns=100)
    assert blocked.outcome is OccupancyOutcome.BLOCKED_RISK
    assert blocked.reason is OccupancyReason.RISK_RESERVED
    assert blocked.exposure_before == D("850")
    assert blocked.committed_after == D("0")

    fits = ledger.evaluate(request("B", "50"), reserved, decision_time_ns=101)
    assert fits.outcome is OccupancyOutcome.ADMITTED
    assert fits.committed_after == D("50")
    assert blocked.entry_id != fits.entry_id


def test_no_capital_blocks_with_empty_equity():
    ledger = OccupancyLedger()
    entry = ledger.evaluate(request("A", "100"), capacity(equity="0"), decision_time_ns=100)
    assert entry.outcome is OccupancyOutcome.BLOCKED_NO_CAPITAL
    assert entry.reason is OccupancyReason.NO_CAPITAL
    assert entry.committed_after == D("0")
    assert ledger.committed_notional == D("0")


# ------------------------------------------------------------- fail closed


@pytest.mark.parametrize(
    ("state", "reason"),
    [
        (capacity(available_at=200), OccupancyReason.CAPACITY_FUTURE),
        (capacity(as_of=200), OccupancyReason.CAPACITY_FUTURE),
        (capacity(equity=None), OccupancyReason.CAPACITY_UNKNOWN),
        (capacity(committed=None), OccupancyReason.CAPACITY_UNKNOWN),
        (capacity(position_notional=None), OccupancyReason.CAPACITY_UNKNOWN),
        (
            capacity(position=PositionPresence.UNCERTAIN),
            OccupancyReason.CAPACITY_UNKNOWN,
        ),
        (capacity(committed="1100"), OccupancyReason.CAPACITY_INCONSISTENT),
    ],
)
def test_unknown_capacity_never_admits(state: CapacityState, reason: OccupancyReason) -> None:
    ledger = OccupancyLedger()
    entry = ledger.evaluate(request("A", "100"), state, decision_time_ns=100)

    assert entry.outcome is OccupancyOutcome.UNKNOWN
    assert entry.reason is reason
    assert entry.committed_after == entry.committed_before
    assert ledger.committed_notional == D("0")
    ledger.assert_no_double_spend()


def test_unknown_capacity_evaluation_does_not_reserve_and_can_retry():
    ledger = OccupancyLedger()
    unknown = ledger.evaluate(request("A", "600"), capacity(available_at=200), decision_time_ns=100)
    admitted = ledger.evaluate(request("A", "600"), capacity(), decision_time_ns=101)

    assert unknown.outcome is OccupancyOutcome.UNKNOWN
    assert admitted.outcome is OccupancyOutcome.ADMITTED
    assert admitted.committed_before == D("0")
    assert admitted.committed_after == D("600")


# ------------------------------------------------ lifecycle-denied opportunities


def test_expired_and_invalidated_opportunities_are_not_admitted():
    ledger = OccupancyLedger()
    expired = ledger.evaluate(request("A", "100", expires_at=50), capacity(), decision_time_ns=100)
    invalidated = ledger.evaluate(
        request("B", "100", status=OpportunityStatus.INVALIDATED),
        capacity(),
        decision_time_ns=100,
    )
    expired_status = ledger.evaluate(
        request("C", "100", status=OpportunityStatus.EXPIRED),
        capacity(),
        decision_time_ns=100,
    )

    assert expired.outcome is OccupancyOutcome.EXPIRED
    assert expired.reason is OccupancyReason.OPPORTUNITY_EXPIRED
    assert invalidated.outcome is OccupancyOutcome.INVALIDATED
    assert invalidated.reason is OccupancyReason.OPPORTUNITY_INVALIDATED
    assert expired_status.outcome is OccupancyOutcome.EXPIRED
    assert ledger.committed_notional == D("0")
    # Lifecycle denials are not capacity alternatives.
    assert ledger.matched_alternatives() == ()
    with pytest.raises(CounterfactualNotApplicableError):
        ledger.record_counterfactual("A", hypothetical_pnl=D("10"), decision_time_ns=100)


# ------------------------------------- realized vs hypothetical separation


def test_realized_and_hypothetical_pnl_are_strictly_separate():
    ledger = OccupancyLedger()
    ledger.evaluate(request("A", "600"), capacity(), decision_time_ns=100)
    loser = ledger.evaluate(request("B", "600"), capacity(), decision_time_ns=101)

    # The blocked alternative would have made money; that is simulated only.
    ledger.record_counterfactual("B", hypothetical_pnl=D("90"), decision_time_ns=102)
    # The admitted opportunity actually lost money; that is the only real number.
    ledger.realize("A", realized_pnl=D("-10"), decision_time_ns=102)

    assert ledger.realized_pnl_total() == D("-10")
    assert ledger.hypothetical_pnl_total() == D("90")
    # The two totals answer different questions and cannot be reconciled into one.
    assert ledger.realized_pnl_total() != ledger.hypothetical_pnl_total()
    assert ledger.realized_pnl("A") == D("-10")
    assert ledger.realized_pnl("B") is None

    factual = {entry.opportunity_id: entry for entry in ledger.decision_log()}
    assert factual["A"].realized_pnl == D("-10")
    assert factual["A"].hypothetical_pnl is None
    assert factual["B"].realized_pnl is None
    assert factual["B"].hypothetical_pnl is None
    assert loser.outcome is OccupancyOutcome.OCCUPIED_CAPACITY
    assert all(entry.hypothetical_pnl is None for entry in ledger.decision_log())

    simulated = ledger.counterfactual_performance()
    assert len(simulated) == 1
    assert simulated[0].opportunity_id == "B"
    assert simulated[0].hypothetical_pnl == D("90")
    assert simulated[0].outcome is OccupancyOutcome.OCCUPIED_CAPACITY
    assert simulated[0].entry_id == loser.entry_id


def test_denied_opportunity_can_never_be_realized():
    ledger = OccupancyLedger()
    ledger.evaluate(request("A", "600"), capacity(), decision_time_ns=100)
    ledger.evaluate(request("B", "600"), capacity(), decision_time_ns=101)

    with pytest.raises(NotAdmittedError):
        ledger.realize("B", realized_pnl=D("50"), decision_time_ns=102)
    assert ledger.realized_pnl_total() == D("0")

    with pytest.raises(CounterfactualOnAdmittedError):
        ledger.record_counterfactual("A", hypothetical_pnl=D("50"), decision_time_ns=102)
    with pytest.raises(UnknownOpportunityError):
        ledger.record_counterfactual("missing", hypothetical_pnl=D("50"), decision_time_ns=102)
    with pytest.raises(CounterfactualOnAdmittedError):
        ledger.record_counterfactual("A", hypothetical_pnl=D("50"), decision_time_ns=103)


def test_realization_and_counterfactual_are_idempotent_but_not_revisable():
    ledger = OccupancyLedger()
    ledger.evaluate(request("A", "600"), capacity(), decision_time_ns=100)
    ledger.evaluate(request("B", "600"), capacity(), decision_time_ns=101)

    ledger.realize("A", realized_pnl=D("-10"), decision_time_ns=102)
    ledger.realize("A", realized_pnl=D("-10"), decision_time_ns=103)
    assert ledger.realized_pnl_total() == D("-10")
    with pytest.raises(DuplicateRealizationError):
        ledger.realize("A", realized_pnl=D("-11"), decision_time_ns=103)

    ledger.record_counterfactual("B", hypothetical_pnl=D("90"), decision_time_ns=102)
    ledger.record_counterfactual("B", hypothetical_pnl=D("90"), decision_time_ns=104)
    assert ledger.hypothetical_pnl_total() == D("90")
    with pytest.raises(DuplicateCounterfactualError):
        ledger.record_counterfactual("B", hypothetical_pnl=D("91"), decision_time_ns=104)


# --------------------------------------------- determinism and schema guards


def _replay() -> tuple[OccupancyLedger, list[OccupancyEntry]]:
    ledger = OccupancyLedger()
    entries = [
        ledger.evaluate(request("A", "600"), capacity(), decision_time_ns=100),
        ledger.evaluate(request("B", "600"), capacity(), decision_time_ns=101),
        ledger.evaluate(request("C", "300"), capacity(), decision_time_ns=102),
    ]
    return ledger, entries


def test_replay_is_deterministic():
    first_ledger, first = _replay()
    second_ledger, second = _replay()

    assert [entry.entry_id for entry in first] == [entry.entry_id for entry in second]
    assert [entry.outcome for entry in first] == [entry.outcome for entry in second]
    assert [entry.reason for entry in first] == [entry.reason for entry in second]
    assert first_ledger.decision_log() == second_ledger.decision_log()
    assert first_ledger.committed_notional == second_ledger.committed_notional


def test_entry_schema_refuses_mixed_or_misattributed_pnl():
    ledger = OccupancyLedger()
    admitted = ledger.evaluate(request("A", "600"), capacity(), decision_time_ns=100)
    blocked = ledger.evaluate(request("B", "600"), capacity(), decision_time_ns=101)

    with pytest.raises(ValidationError):
        OccupancyEntry(
            **admitted.model_dump(exclude={"realized_pnl", "hypothetical_pnl"}),
            realized_pnl=D("1"),
            hypothetical_pnl=D("2"),
        )
    with pytest.raises(ValidationError):
        OccupancyEntry(
            **blocked.model_dump(exclude={"realized_pnl", "hypothetical_pnl"}),
            realized_pnl=D("1"),
        )
    with pytest.raises(ValidationError):
        OccupancyEntry(
            **admitted.model_dump(exclude={"realized_pnl", "hypothetical_pnl"}),
            hypothetical_pnl=D("1"),
        )


def test_request_rejects_non_positive_or_non_finite_notional():
    with pytest.raises(ValidationError):
        request("A", "0")
    with pytest.raises(ValidationError):
        request("A", "-1")
    with pytest.raises(ValidationError):
        OccupancyRequest(
            opportunity_id="A",
            instrument_id=INSTRUMENT,
            requested_notional=D("NaN"),
            expires_at_ns=1,
        )
