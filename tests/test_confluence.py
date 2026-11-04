from dataclasses import replace
from decimal import Decimal
from itertools import product

import pytest

from v8_next.domain.market import Candle, CausalFrame
from v8_next.economics.decisions import Opportunity, StanceKind
from v8_next.experts.confluence import (
    confluence_direction,
    confluence_legs,
    observe_confluence,
    recovery_direction,
)


def test_all_vote_combinations_preserve_strict_and_majority_semantics():
    for votes in product((None, "LONG", "SHORT"), repeat=3):
        strict = votes[0] if votes[0] is not None and len(set(votes)) == 1 else None
        majority = next((side for side in ("LONG", "SHORT") if votes.count(side) >= 2), None)
        assert confluence_direction(votes, "a") == strict
        assert confluence_direction(votes, "b") == majority
    with pytest.raises(ValueError, match="unsupported"):
        confluence_direction((None, None, None), "unknown")


def test_recovery_needs_observed_other_side_and_preserves_long_precedence():
    assert recovery_direction((None, 35, 40)) is None
    assert recovery_direction((None, 25, 35, 40)) == "LONG"
    assert recovery_direction((None, 75, 65, 60)) == "SHORT"
    assert recovery_direction((None, 25, 80, 60)) == "LONG"


def test_real_bands_and_impulse_produce_majority_without_fabricated_rsi_vote():
    prices = [100] * 40 + [96, 104] * 9 + [96, 90]
    bars = [
        Candle(
            "i",
            i,
            i + 1,
            Decimal(p),
            Decimal(p) + 1,
            Decimal(p) - 1,
            Decimal(p),
            Decimal(10),
            i + 1,
            i + 1,
            "fixture",
        )
        for i, p in enumerate(prices)
    ]
    bars[10] = replace(bars[10], low=Decimal(70))
    bars[25] = replace(bars[25], high=Decimal(150))
    bars[-1] = replace(bars[-1], low=Decimal(87))
    frame = CausalFrame("i", len(bars), tuple(bars))
    opportunity = Opportunity("o", "e", "i", "LONG", len(bars), len(bars) + 10)
    legs = confluence_legs(frame)
    assert legs[0] == legs[2] == "LONG"
    assert observe_confluence(frame, opportunity, variant="b").kind == StanceKind.SUPPORT
    assert observe_confluence(frame, opportunity, variant="a").kind == (
        StanceKind.SUPPORT if legs[1] == "LONG" else StanceKind.ABSTAIN
    )
    assert observe_confluence(frame, None, variant="b").kind == StanceKind.ABSTAIN
    from v8_next.economics.protection import protection_at

    protection = protection_at(frame, opportunity, "confluence:b:v2", Decimal(".01"))
    assert protection is not None
    assert protection.expires_ns == frame.decision_ns + 8
    strict = protection_at(frame, opportunity, "confluence:a:v2", Decimal(".01"))
    assert (strict is not None) == (legs[1] == "LONG")


def test_strict_confluence_with_computed_recovery_leg():
    prices = list(range(120, 100, -1)) + [100] * 20 + [96, 104] * 9 + [96, 90]
    bars = [
        Candle(
            "i",
            i,
            i + 1,
            Decimal(p),
            Decimal(p) + 1,
            Decimal(p) - 1,
            Decimal(p),
            Decimal(10),
            i + 1,
            i + 1,
            "fixture",
        )
        for i, p in enumerate(prices)
    ]
    bars[10] = replace(bars[10], low=Decimal(70))
    bars[25] = replace(bars[25], high=Decimal(150))
    bars[-1] = replace(bars[-1], low=Decimal(87))
    frame = CausalFrame("i", len(bars), tuple(bars))
    opportunity = Opportunity("o", "e", "i", "LONG", len(bars), len(bars) + 10)
    assert confluence_legs(frame) == ("LONG", "LONG", "LONG")
    assert observe_confluence(frame, opportunity, variant="a").kind == StanceKind.SUPPORT
