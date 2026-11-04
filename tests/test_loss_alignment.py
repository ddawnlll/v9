from dataclasses import replace
from decimal import Decimal

import pytest

from v8_next.evaluation.alignment import IntervalLoss, paired_differentials


def test_paired_losses_preserve_sign_without_reordering_or_imputation():
    baseline = (IntervalLoss(10, 20, 21, Decimal(3)), IntervalLoss(20, 30, 31, Decimal(1)))
    variant = (replace(baseline[0], loss=Decimal(1)), replace(baseline[1], loss=Decimal(2)))

    def compare(rows):
        return paired_differentials(
            baseline, rows, frozen_ns=10, evaluation_end_ns=30, decision_ns=40
        )

    assert compare(variant) == (Decimal(2), Decimal(-1))
    for invalid in (
        variant[::-1],
        variant[:1],
        (replace(variant[0], loss=None), variant[1]),
        (variant[0], replace(variant[1], available_ns=40)),
        (replace(variant[0], start_ns=9), variant[1]),
    ):
        with pytest.raises(ValueError):
            compare(invalid)
