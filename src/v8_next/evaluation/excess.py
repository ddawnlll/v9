"""Explicit aligned reference subtraction; no assumed zero risk-free series."""

from v8_next.evaluation.alignment import IntervalLoss, paired_differentials


def excess_losses(
    losses: dict[str, tuple[IntervalLoss, ...]],
    reference: tuple[IntervalLoss, ...],
    *,
    decision_ns: int,
) -> dict[str, tuple[IntervalLoss, ...]]:
    """Both inputs are negative fixed-capital returns in the same currency.

    Reference provenance and financial suitability are caller responsibilities.
    Positive reference return increases excess loss. Availability is the later
    of the strategy computation and the reference observation.
    """
    if not reference or not losses:
        raise ValueError("explicit nonempty excess-return reference and family required")
    output = {}
    for name, rows in losses.items():
        differences = paired_differentials(
            rows,
            reference,
            frozen_ns=reference[0].start_ns,
            evaluation_end_ns=reference[-1].end_ns,
            decision_ns=decision_ns,
        )
        output[name] = tuple(
            IntervalLoss(row.start_ns, row.end_ns, max(row.available_ns, ref.available_ns), value)
            for row, ref, value in zip(rows, reference, differences, strict=True)
        )
    return output
