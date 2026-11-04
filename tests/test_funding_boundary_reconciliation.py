"""M0-06: funding boundary, receipt finality and causal equity reconciliation."""

from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError

from v8_next.audit.funding_boundary import (
    FundingIssueCode,
    FundingReportStatus,
    FundingSettlement,
    PositionEvent,
    PriceMark,
    issues_by_code,
    reconcile_funding_boundary,
)

D = Decimal
BTC = "BTCUSDT-PERP.BINANCE"


def position(
    event_id: str = "pe-1",
    *,
    delta: str = "1",
    event_time_ns: int = 100,
    available_at_ns: int = 100,
) -> PositionEvent:
    return PositionEvent(
        event_id=event_id,
        campaign_id="camp-1",
        instrument_id=BTC,
        quantity_delta=D(delta),
        event_time_ns=event_time_ns,
        available_at_ns=available_at_ns,
    )


def mark(price: str, at: int) -> PriceMark:
    return PriceMark(instrument_id=BTC, price=D(price), event_time_ns=at, available_at_ns=at)


def settlement(
    *,
    settlement_id: str = "set-1",
    amount: str = "-5.1",
    rate: str = "0.0001",
    boundary: int = 200,
    received: int = 200,
    accounting: int | None = 300,
) -> FundingSettlement:
    return FundingSettlement(
        settlement_id=settlement_id,
        campaign_id="camp-1",
        instrument_id=BTC,
        amount=D(amount),
        rate=D(rate),
        boundary_time_ns=boundary,
        received_at_ns=received,
        accounting_time_ns=accounting,
    )


MARKS = (mark("50000", 100), mark("51000", 200), mark("52000", 300))


def report(**overrides: object):
    kwargs: dict[str, object] = {
        "initial_equity": D("100000"),
        "settlements": (settlement(),),
        "position_events": (position(),),
        "marks": MARKS,
        "as_of_ns": 300,
        "opening_time_ns": 0,
        "realized_cashflows": D("-50000"),
        "final_equity": D("101994.9"),
    }
    kwargs.update(overrides)
    return reconcile_funding_boundary(**kwargs)  # type: ignore[arg-type]


def test_boundary_attribution_and_equity_reconcile() -> None:
    result = report()
    assert result.passed, result.issues
    assert result.attributions[0].quantity_at_boundary == D("1")
    assert result.attributions[0].mark_price == D("51000")
    assert result.attributions[0].expected_amount == D("-5.1")
    assert result.equity is not None
    assert result.equity.funding_total == D("-5.1")
    assert result.equity.unrealized_change == D("52000")
    assert result.equity.expected_final_equity == D("101994.9")
    assert result.equity.agreed


def test_post_boundary_exposure_change_cannot_retroactively_change_funding() -> None:
    later = position("pe-2", delta="5", event_time_ns=250, available_at_ns=250)
    result = report(position_events=(position(), later))
    assert result.attributions[0].quantity_at_boundary == D("1")
    assert result.attributions[0].agreed


def test_future_mark_is_not_used_for_the_boundary() -> None:
    future_only = (mark("50000", 250), mark("52000", 300))
    result = report(marks=future_only, final_equity=None)
    assert result.status is FundingReportStatus.BLOCKED
    assert FundingIssueCode.NO_CAUSAL_MARK in issues_by_code(result)


def test_off_grid_boundary_uses_last_causal_mark() -> None:
    result = report(settlements=(settlement(boundary=250),), final_equity=None)
    assert result.status is FundingReportStatus.PASS
    assert result.attributions[0].mark_price == D("51000")


def test_delayed_receipt_is_flagged_but_still_attributed() -> None:
    result = report(settlements=(settlement(received=250),))
    assert result.passed, result.issues
    assert result.attributions[0].delayed is True


def test_delayed_past_the_accounting_cut_is_blocked() -> None:
    result = report(settlements=(settlement(received=250, accounting=220),), final_equity=None)
    assert result.status is FundingReportStatus.BLOCKED
    assert FundingIssueCode.NOT_FINAL_AT_ACCOUNTING_TIME in issues_by_code(result)


def test_net_zero_exposure_cannot_carry_non_zero_funding() -> None:
    bought = position("pe-1", delta="1", event_time_ns=100, available_at_ns=100)
    sold = position("pe-2", delta="-1", event_time_ns=150, available_at_ns=150)
    result = report(position_events=(bought, sold), final_equity=None)
    assert result.status is FundingReportStatus.MISMATCH
    assert FundingIssueCode.FUNDING_MISMATCH in issues_by_code(result)
    assert result.attributions[0].expected_amount == D("0")


def test_unknown_exposure_is_blocked_not_zero() -> None:
    result = report(position_events=(), final_equity=None)
    assert result.status is FundingReportStatus.BLOCKED
    assert FundingIssueCode.MISSING_EXPOSURE in issues_by_code(result)
    assert result.attributions == ()
    assert result.equity is not None
    assert result.equity.funding_total == D("0")


def test_position_evidence_available_after_boundary_is_look_ahead() -> None:
    late = position(event_time_ns=250, available_at_ns=250)
    result = report(position_events=(late,), final_equity=None)
    assert result.status is FundingReportStatus.BLOCKED
    assert FundingIssueCode.MISSING_EXPOSURE in issues_by_code(result)


def test_duplicate_settlement_id_is_rejected() -> None:
    result = report(settlements=(settlement(), settlement()), final_equity=None)
    assert result.status is FundingReportStatus.MISMATCH
    assert FundingIssueCode.DUPLICATE_SETTLEMENT in issues_by_code(result)


def test_two_ids_claiming_one_boundary_are_rejected() -> None:
    second = settlement(settlement_id="set-2")
    result = report(settlements=(settlement(), second), final_equity=None)
    assert result.status is FundingReportStatus.MISMATCH
    assert FundingIssueCode.DUPLICATE_BOUNDARY_CLAIM in issues_by_code(result)


def test_equity_mismatch_is_a_hard_failure() -> None:
    result = report(final_equity=D("101000"))
    assert result.status is FundingReportStatus.MISMATCH
    assert FundingIssueCode.EQUITY_MISMATCH in issues_by_code(result)


def test_short_position_receives_funding() -> None:
    short = position(delta="-1")
    result = reconcile_funding_boundary(
        initial_equity=D("1000"),
        settlements=(settlement(amount="5.1"),),
        position_events=(short,),
        marks=MARKS,
        as_of_ns=300,
        opening_time_ns=0,
        realized_cashflows=D("50000"),
        final_equity=D("-994.9"),
    )
    assert result.passed, result.issues
    assert result.attributions[0].expected_amount == D("5.1")


def test_missing_position_history_blocks_equity() -> None:
    result = report(position_events=(), position_history_complete=False, final_equity=None)
    assert result.status is FundingReportStatus.BLOCKED
    assert FundingIssueCode.MISSING_EXPOSURE in issues_by_code(result)


def test_invalid_records_are_rejected_at_construction() -> None:
    with pytest.raises(ValidationError):
        FundingSettlement(
            settlement_id="s",
            campaign_id="c",
            instrument_id=BTC,
            amount=D("NaN"),
            rate=D("0.0001"),
            boundary_time_ns=1,
            received_at_ns=1,
        )
    with pytest.raises(ValidationError):
        PriceMark(
            instrument_id=BTC,
            price=D("0"),
            event_time_ns=1,
            available_at_ns=1,
        )
    with pytest.raises(ValidationError):
        PositionEvent(
            event_id="e",
            campaign_id="c",
            instrument_id=BTC,
            quantity_delta=D("Infinity"),
            event_time_ns=1,
            available_at_ns=1,
        )
