"""Independent two-campaign accounting oracle (M0-02, issue #3).

Every source record and every expected number below is hand-constructed and
hand-computed; nothing is copied from an engine aggregate.
"""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest

from v8_next.audit.oracle_two_campaign import (
    CampaignRecord,
    FillRecord,
    FundingRecord,
    MarkRecord,
    OracleReport,
    OracleViolation,
    OracleViolationCode,
    OrderRecord,
    OrderSide,
    recompute,
    reconcile_aggregate,
)

D = Decimal


# --------------------------------------------------------------------------- #
# hand-written two-campaign fixture
# --------------------------------------------------------------------------- #
CAMPAIGNS = (
    CampaignRecord(
        campaign_id="campaign-alpha",
        instrument_id="BTCUSDT-PERP",
        initial_equity=D("1000"),
        opened_ns=100,
    ),
    CampaignRecord(
        campaign_id="campaign-beta",
        instrument_id="ETHUSDT-PERP",
        initial_equity=D("500"),
        opened_ns=200,
    ),
)

ORDERS = (
    OrderRecord(
        order_id="ORD-A1",
        campaign_id="campaign-alpha",
        instrument_id="BTCUSDT-PERP",
        side=OrderSide.BUY,
        quantity=D("2"),
        submitted_ns=100,
    ),
    OrderRecord(
        order_id="ORD-A2",
        campaign_id="campaign-alpha",
        instrument_id="BTCUSDT-PERP",
        side=OrderSide.SELL,
        quantity=D("2"),
        submitted_ns=120,
    ),
    OrderRecord(
        order_id="ORD-B1",
        campaign_id="campaign-beta",
        instrument_id="ETHUSDT-PERP",
        side=OrderSide.SELL,
        quantity=D("3"),
        submitted_ns=200,
    ),
)

FILLS = (
    # alpha: buy 2 @ 100, then sell 1 @ 110 and 1 @ 120 (FIFO realized 10 + 20).
    FillRecord(
        fill_id="FIL-A1",
        order_id="ORD-A1",
        campaign_id="campaign-alpha",
        instrument_id="BTCUSDT-PERP",
        side=OrderSide.BUY,
        quantity=D("2"),
        price=D("100"),
        fee=D("0.10"),
        fee_id="FEE-A1",
        event_time_ns=110,
    ),
    FillRecord(
        fill_id="FIL-A2",
        order_id="ORD-A2",
        campaign_id="campaign-alpha",
        instrument_id="BTCUSDT-PERP",
        side=OrderSide.SELL,
        quantity=D("1"),
        price=D("110"),
        fee=D("0.05"),
        fee_id="FEE-A2",
        event_time_ns=130,
    ),
    FillRecord(
        fill_id="FIL-A3",
        order_id="ORD-A2",
        campaign_id="campaign-alpha",
        instrument_id="BTCUSDT-PERP",
        side=OrderSide.SELL,
        quantity=D("1"),
        price=D("120"),
        fee=D("0.05"),
        fee_id="FEE-A3",
        event_time_ns=140,
    ),
    # beta: short 3 (1 @ 200, 2 @ 210), marked at 205.
    FillRecord(
        fill_id="FIL-B1",
        order_id="ORD-B1",
        campaign_id="campaign-beta",
        instrument_id="ETHUSDT-PERP",
        side=OrderSide.SELL,
        quantity=D("1"),
        price=D("200"),
        fee=D("0.02"),
        fee_id="FEE-B1",
        event_time_ns=210,
    ),
    FillRecord(
        fill_id="FIL-B2",
        order_id="ORD-B1",
        campaign_id="campaign-beta",
        instrument_id="ETHUSDT-PERP",
        side=OrderSide.SELL,
        quantity=D("2"),
        price=D("210"),
        fee=D("0.04"),
        fee_id="FEE-B2",
        event_time_ns=220,
    ),
)

FUNDING = (
    FundingRecord(
        funding_id="FUND-A1",
        campaign_id="campaign-alpha",
        instrument_id="BTCUSDT-PERP",
        amount=D("-0.5"),
        event_time_ns=125,
    ),
)

MARKS = (
    MarkRecord(
        mark_id="MARK-A",
        campaign_id="campaign-alpha",
        instrument_id="BTCUSDT-PERP",
        price=D("130"),
        mark_time_ns=150,
    ),
    MarkRecord(
        mark_id="MARK-B",
        campaign_id="campaign-beta",
        instrument_id="ETHUSDT-PERP",
        price=D("205"),
        mark_time_ns=230,
    ),
)


def _report(
    *,
    campaigns=CAMPAIGNS,
    orders=ORDERS,
    fills=FILLS,
    funding=FUNDING,
    marks=MARKS,
) -> OracleReport:
    return recompute(campaigns=campaigns, orders=orders, fills=fills, funding=funding, marks=marks)


def _code(exc_info: pytest.ExceptionInfo[OracleViolation]) -> OracleViolationCode:
    return exc_info.value.code


# --------------------------------------------------------------------------- #
# A. independent recomputation of the two-campaign fixture
# --------------------------------------------------------------------------- #
def test_two_campaign_fixture_reconciles_independently() -> None:
    report = _report()
    assert [ledger.campaign_id for ledger in report.ledgers] == [
        "campaign-alpha",
        "campaign-beta",
    ]

    alpha = report.ledger("campaign-alpha")
    # FIFO: (110-100)*1 + (120-100)*1 = 30 realized; fees 0.10+0.05+0.05; funding -0.5.
    assert alpha.initial_equity == D("1000")
    assert alpha.realized_pnl == D("30")
    assert alpha.fees == D("0.20")
    assert alpha.funding == D("-0.5")
    assert alpha.open_quantity == D("0")
    assert alpha.unrealized_pnl == D("0")
    assert alpha.mark_price == D("130")
    assert alpha.final_equity == D("1029.30")
    assert alpha.fill_ids == ("FIL-A1", "FIL-A2", "FIL-A3")
    assert alpha.fee_ids == ("FEE-A1", "FEE-A2", "FEE-A3")
    assert alpha.funding_ids == ("FUND-A1",)

    beta = report.ledger("campaign-beta")
    # Short 1 @ 200 and 2 @ 210 marked at 205: (205-200)*-1 + (205-210)*-2 = 5.
    assert beta.realized_pnl == D("0")
    assert beta.fees == D("0.06")
    assert beta.open_quantity == D("-3")
    assert beta.unrealized_pnl == D("5")
    assert beta.final_equity == D("504.94")

    assert report.total_initial_equity == D("1500")
    assert report.total_final_equity == D("1534.24")


def test_per_order_fills_are_recomputed_and_realized_attributed_to_closer() -> None:
    alpha = _report().ledger("campaign-alpha")
    first, second = alpha.orders
    assert first.order_id == "ORD-A1"
    assert first.filled_quantity == D("2")
    assert first.notional == D("200")
    assert first.fees == D("0.10")
    assert first.realized_pnl == D("0")
    assert first.fill_ids == ("FIL-A1",)
    # Both closing fills belong to ORD-A2, so all realized PnL is attributed there.
    assert second.order_id == "ORD-A2"
    assert second.filled_quantity == D("2")
    assert second.notional == D("230")
    assert second.fees == D("0.10")
    assert second.realized_pnl == D("30")
    assert second.fill_ids == ("FIL-A2", "FIL-A3")


def test_provenance_digest_is_order_independent_and_reconciles_aggregate() -> None:
    report = _report()
    shuffled = recompute(
        campaigns=tuple(reversed(CAMPAIGNS)),
        orders=tuple(reversed(ORDERS)),
        fills=tuple(reversed(FILLS)),
        funding=FUNDING,
        marks=tuple(reversed(MARKS)),
    )
    assert report.provenance_digest == shuffled.provenance_digest
    assert len(report.provenance_digest) == 64

    # A matching aggregate (supplied as data, never imported) reconciles.
    reconcile_aggregate(
        report,
        {"campaign-alpha": D("1029.30"), "campaign-beta": D("504.94")},
    )


def test_oracle_does_not_import_the_engine_aggregation_path() -> None:
    import ast
    import inspect

    import v8_next.audit.oracle_two_campaign as module

    source = inspect.getsource(module)
    imported: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            imported.add(node.module)
    forbidden = {
        "v8_next.adapters",
        "v8_next.audit.pipeline",
        "v8_next.audit.trade_metrics",
    }
    assert not any(
        module_name == bad or module_name.startswith(f"{bad}.")
        for module_name in imported
        for bad in forbidden
    )


# --------------------------------------------------------------------------- #
# B. hard, typed failures
# --------------------------------------------------------------------------- #
def test_swapped_campaign_id_is_a_hard_failure() -> None:
    swapped = tuple(
        replace(fill, campaign_id="campaign-beta") if fill.fill_id == "FIL-A2" else fill
        for fill in FILLS
    )
    with pytest.raises(OracleViolation) as excinfo:
        _report(fills=swapped)
    assert _code(excinfo) is OracleViolationCode.CAMPAIGN_MISMATCH
    assert excinfo.value.record_id == "FIL-A2"


def test_cross_campaign_swap_is_rejected_even_when_internally_consistent() -> None:
    # Both the order and its fills are relabelled to the other campaign: the fill/order
    # campaign check passes, but the record now names the wrong campaign instrument.
    moved_orders = tuple(
        replace(order, campaign_id="campaign-beta") if order.order_id == "ORD-A2" else order
        for order in ORDERS
    )
    moved_fills = tuple(
        replace(fill, campaign_id="campaign-beta")
        if fill.order_id == "ORD-A2"
        else fill
        for fill in FILLS
    )
    with pytest.raises(OracleViolation) as excinfo:
        _report(orders=moved_orders, fills=moved_fills)
    assert _code(excinfo) is OracleViolationCode.INSTRUMENT_MISMATCH


def test_duplicated_fill_is_a_hard_failure() -> None:
    duplicated = FILLS + (FILLS[1],)
    with pytest.raises(OracleViolation) as excinfo:
        _report(fills=duplicated)
    assert _code(excinfo) is OracleViolationCode.DUPLICATE_FILL
    assert excinfo.value.record_id == "FIL-A2"


def test_duplicated_fee_is_a_hard_failure() -> None:
    double_charged = tuple(
        replace(fill, fee_id="FEE-A2") if fill.fill_id == "FIL-A3" else fill for fill in FILLS
    )
    with pytest.raises(OracleViolation) as excinfo:
        _report(fills=double_charged)
    assert _code(excinfo) is OracleViolationCode.DUPLICATE_FEE
    assert excinfo.value.record_id == "FEE-A2"


def test_orphan_fill_and_missing_mark_are_hard_failures() -> None:
    orphan = (replace(FILLS[0], order_id="ORD-MISSING"),) + FILLS[1:]
    with pytest.raises(OracleViolation) as excinfo:
        _report(fills=orphan)
    assert _code(excinfo) is OracleViolationCode.ORPHAN_FILL

    with pytest.raises(OracleViolation) as excinfo:
        _report(marks=(MARKS[1],))
    assert _code(excinfo) is OracleViolationCode.MISSING_MARK
    assert excinfo.value.record_id == "campaign-alpha"


def test_mark_before_last_fill_is_a_hard_failure() -> None:
    early_mark = (replace(MARKS[0], mark_time_ns=135), MARKS[1])
    with pytest.raises(OracleViolation) as excinfo:
        _report(marks=early_mark)
    assert _code(excinfo) is OracleViolationCode.MARK_BEFORE_FILL


def test_aggregate_mismatch_is_a_hard_failure() -> None:
    report = _report()
    with pytest.raises(OracleViolation) as excinfo:
        reconcile_aggregate(
            report,
            {"campaign-alpha": D("1030"), "campaign-beta": D("504.94")},
        )
    assert _code(excinfo) is OracleViolationCode.ARITHMETIC_MISMATCH
    assert excinfo.value.record_id == "campaign-alpha"

    with pytest.raises(OracleViolation) as excinfo:
        reconcile_aggregate(report, {"campaign-alpha": D("1029.30")})
    assert _code(excinfo) is OracleViolationCode.ARITHMETIC_MISMATCH


def test_money_must_be_decimal_not_float() -> None:
    with pytest.raises(ValueError, match="Decimal required"):
        CampaignRecord(
            campaign_id="campaign-alpha",
            instrument_id="BTCUSDT-PERP",
            initial_equity=1000.0,  # type: ignore[arg-type]
            opened_ns=100,
        )
