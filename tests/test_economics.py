from dataclasses import replace
from decimal import Decimal

import pytest

from v8_next.economics.decisions import (
    Opportunity,
    Stance,
    StanceKind,
    UtilityInputs,
    reconcile,
    utility_admission,
)


def test_cloned_observers_cannot_inflate_reconciliation():
    opportunity = Opportunity("o", "exposure", "instrument", "LONG", 0, 100)
    stance = Stance("a", "same-data", StanceKind.SUPPORT, "reason", "o", 10)
    assert reconcile(opportunity, (stance,)) == reconcile(
        opportunity, (stance, replace(stance, observer_id="clone"))
    )
    assert not hasattr(stance, "quantity")
    assert not hasattr(stance, "submit_order")


def test_conflicting_witness_prevents_admission():
    opportunity = Opportunity("o", "exposure", "instrument", "LONG", 0, 100)
    stance = Stance("a", "same-data", StanceKind.SUPPORT, "reason", "o", 10)
    assert (
        reconcile(opportunity, (stance, replace(stance, kind=StanceKind.CONTRADICT)))
        == "CONTRADICTED"
    )
    with pytest.raises(ValueError, match="another opportunity"):
        reconcile(opportunity, (replace(stance, opportunity_id="other"),))


def test_missing_calibration_never_becomes_zero_cost_edge():
    assert (
        utility_admission(UtilityInputs(None, None, None, None, None, None, None))
        == "REJECTED_MISSING_CALIBRATION"
    )


def test_costs_and_uncertainty_are_all_subtracted():
    inputs = UtilityInputs(
        Decimal(5), Decimal(1), Decimal(1), Decimal(1), Decimal(1), Decimal(1), "test-only"
    )
    assert utility_admission(inputs) == "REJECTED_SUB_FRICTION"
    assert utility_admission(replace(inputs, gross_edge=Decimal(6))) == "UTILITY_ELIGIBLE"
