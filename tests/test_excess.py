from decimal import Decimal

import pytest

from v8_next.evaluation.alignment import IntervalLoss
from v8_next.evaluation.excess import excess_losses


def test_reference_return_subtracts_from_strategy_return_and_preserves_availability():
    strategy = (IntervalLoss(10, 20, 100, Decimal("-.10")),)
    reference = (IntervalLoss(10, 20, 150, Decimal("-.02")),)
    result = excess_losses({"a": strategy}, reference, decision_ns=200)["a"][0]
    assert result.loss == Decimal("-.08")
    assert result.available_ns == 150


@pytest.mark.parametrize(
    "reference",
    [
        (),
        (IntervalLoss(11, 20, 100, Decimal(0)),),
        (IntervalLoss(10, 20, 200, Decimal(0)),),
        (IntervalLoss(10, 20, 100, None),),
    ],
)
def test_missing_unaligned_or_future_reference_cannot_be_imputed(reference):
    with pytest.raises(ValueError):
        excess_losses({"a": (IntervalLoss(10, 20, 100, Decimal(0)),)}, reference, decision_ns=200)
