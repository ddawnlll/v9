"""Diagnostic fixed-capital returns from complete native bar equity marks."""

import hashlib
import json
from decimal import Decimal
from typing import Any

from v8_next.evaluation.alignment import IntervalLoss


def equity_losses(
    marks: list[dict[str, Any]], *, capital: Decimal, computed_ns: int
) -> tuple[IntervalLoss, ...]:
    """Negative equity increments / fixed initial capital, not compounded returns.

    First mark is the starting valuation, never an invented pre-data balance.
    Historical callback clocks are modeled; availability is computation time.
    No risk-free subtraction is implied, so these are not DSR excess returns.
    """
    if not capital.is_finite() or capital <= 0 or len(marks) < 2:
        raise ValueError("positive capital and at least two equity marks required")
    values = []
    times = []
    input_universe = None
    detailed = "valuation_inputs" in marks[0]
    for mark in marks:
        if mark["phase"] != "PRE_STRATEGY_BAR_CALLBACK" or mark["currency"] != "USDT":
            raise ValueError("incompatible equity valuation convention")
        end = mark["end_ns"]
        observed = mark["observed_ns"]
        if (
            type(end) is not int
            or type(observed) is not int
            or not 0 <= end <= observed <= computed_ns
        ):
            raise ValueError("invalid equity observation chronology")
        cash, pnl, equity = (Decimal(mark[k]) for k in ("cash", "unrealized_pnl", "equity"))
        if not all(v.is_finite() for v in (cash, pnl, equity)) or cash + pnl != equity:
            raise ValueError("unreconciled or nonfinite equity mark")
        if not mark["source_hash"]:
            raise ValueError("missing equity source identity")
        if ("valuation_inputs" in mark) != detailed:
            raise ValueError("mixed equity provenance schemas")
        if detailed:
            inputs = mark["valuation_inputs"]
            if not isinstance(inputs, dict) or not inputs:
                raise ValueError("missing portfolio valuation inputs")
            universe = frozenset(inputs)
            if input_universe is not None and universe != input_universe:
                raise ValueError("portfolio valuation universe changed")
            input_universe = universe
            for item in inputs.values():
                price = Decimal(item["price"])
                if (
                    not price.is_finite()
                    or price <= 0
                    or not item["source_hash"]
                    or item["event_ns"] != end
                    or not end <= item["observed_ns"] <= observed
                ):
                    raise ValueError("invalid portfolio valuation input")
            identity = (
                next(iter(inputs.values()))["source_hash"]
                if len(inputs) == 1
                else hashlib.sha256(
                    json.dumps(inputs, sort_keys=True, separators=(",", ":")).encode()
                ).hexdigest()
            )
            if identity != mark["source_hash"]:
                raise ValueError("portfolio valuation identity mismatch")
        times.append(end)
        values.append(equity)
    duration = times[1] - times[0]
    if duration <= 0 or any(b - a != duration for a, b in zip(times, times[1:], strict=False)):
        raise ValueError("missing, repeated or irregular equity boundary")
    return tuple(
        IntervalLoss(times[i - 1], times[i], computed_ns, -(values[i] - values[i - 1]) / capital)
        for i in range(1, len(times))
    )
