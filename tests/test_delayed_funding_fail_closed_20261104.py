"""MECHANICS ONLY: delayed funding must not be attributed to a different trade."""

from decimal import Decimal

import pytest

from v8_next.adapters.settlements import FinalFunding
from v8_next.app.paper import _refuse_unattributed_delayed_funding

IID = "BTC-PERP.BINANCE"


def settlement(boundary: int, received: int) -> FinalFunding:
    return FinalFunding(IID, boundary, received, Decimal("0.01"), Decimal("100"), "x")


def position(opened: int, closed: int) -> dict[str, object]:
    return {"instrument_id": IID, "opened_ns": opened, "closed_ns": closed}


def test_same_position_at_boundary_and_receipt_is_attributable() -> None:
    # One trade spanned both instants: the re-timed charge still belongs to it.
    _refuse_unattributed_delayed_funding((settlement(10, 20),), [position(5, 30)])
    _refuse_unattributed_delayed_funding((settlement(10, 20),), [position(5, 0)])
    # No exposure at either instant: nothing to misattribute.
    _refuse_unattributed_delayed_funding((settlement(10, 20),), [position(0, 5)])
    _refuse_unattributed_delayed_funding((settlement(10, 20),), [])


def test_exposure_that_changed_between_boundary_and_receipt_is_refused() -> None:
    cases = [
        [position(0, 15), position(15, 0)],  # closed before receipt, reopened
        [position(0, 5), position(15, 0)],  # flat at the boundary, open at receipt
        [position(0, 30), position(15, 0)],  # a second trade appears later
    ]
    for positions in cases:
        with pytest.raises(ValueError, match="boundary exposure accounting"):
            _refuse_unattributed_delayed_funding((settlement(10, 20),), positions)


def test_closing_exactly_on_the_boundary_is_treated_as_exposed() -> None:
    # The snapshot cannot order a close against the settlement instant, so a
    # close at the boundary counts as exposure; with no receipt-time position the
    # attribution differs and the run is refused.
    with pytest.raises(ValueError, match="boundary exposure accounting"):
        _refuse_unattributed_delayed_funding((settlement(10, 20),), [position(0, 10)])


def test_on_time_settlement_needs_no_attribution_check() -> None:
    _refuse_unattributed_delayed_funding((settlement(10, 10),), [position(0, 15)])
