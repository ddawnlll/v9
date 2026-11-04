"""Chronological paired-loss inputs; no estimator, imputation or claim authority."""

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class IntervalLoss:
    start_ns: int
    end_ns: int
    available_ns: int
    loss: Decimal | None


def paired_differentials(
    baseline: tuple[IntervalLoss, ...],
    variant: tuple[IntervalLoss, ...],
    *,
    frozen_ns: int,
    evaluation_end_ns: int,
    decision_ns: int,
) -> tuple[Decimal, ...]:
    """Positive means lower variant loss; caller must verify outcome provenance.

    Intervals are half-open and must share a complete nonoverlapping chronology.
    A frozen boundary does not establish that the holdout has never been inspected.
    These values cannot supply utility or significance without their further gates.
    """
    if not 0 <= frozen_ns < evaluation_end_ns < decision_ns:
        raise ValueError("invalid chronological evaluation boundaries")
    if not baseline or len(baseline) != len(variant):
        raise ValueError("missing paired outcomes")
    result = []
    previous = frozen_ns
    for left, right in zip(baseline, variant, strict=True):
        if (left.start_ns, left.end_ns) != (right.start_ns, right.end_ns):
            raise ValueError("loss intervals are not aligned")
        if left.start_ns != previous or not left.start_ns < left.end_ns <= evaluation_end_ns:
            raise ValueError("gap, overlap or outcome crosses evaluation boundary")
        for row in (left, right):
            if not row.end_ns <= row.available_ns < decision_ns:
                raise ValueError("outcome not available before decision")
            if row.loss is None or not row.loss.is_finite():
                raise ValueError("missing or nonfinite economic loss")
        assert left.loss is not None and right.loss is not None
        result.append(left.loss - right.loss)
        previous = left.end_ns
    if previous != evaluation_end_ns:
        raise ValueError("incomplete evaluation chronology")
    return tuple(result)
