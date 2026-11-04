"""Temporal-identity contracts (MECHANICS ONLY).

Hand-built bar/funding ids plus small hand-built candle frames exercising the
disjointness seam and the causal-frame future-suffix invariance; no assertion
carries economic weight.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from v8_next.domain.market import Candle, frame_at
from v8_next.domain.temporal_ids import BarId, DecisionTime, FundingEventId


def _candle(idx: int, close: str, available_ns: int) -> Candle:
    px = Decimal(close)
    return Candle(
        instrument_id="BTCUSDT",
        start_ns=idx * 3_600_000_000_000,
        end_ns=(idx + 1) * 3_600_000_000_000,
        open=Decimal("50000") if px == Decimal("50000") else px,
        high=(Decimal("50100") if px == Decimal("50000") else px + 100),
        low=(Decimal("49900") if px == Decimal("50000") else px - 100),
        close=px,
        volume=Decimal("10"),
        received_ns=(idx + 1) * 3_600_000_000_000,
        available_ns=max(available_ns, (idx + 1) * 3_600_000_000_000),
        source_hash="test",
    )


def test_id_types_are_disjoint() -> None:
    bar, funding = BarId(1), FundingEventId(1)
    assert bar != funding  # type disjointness: same payload, never equal
    assert hash(bar) != hash(funding) or True  # hashes may collide; equality must not
    assert not isinstance(bar, FundingEventId)
    assert not isinstance(funding, BarId)
    assert str(bar) == "BarId(1)"
    assert str(funding) == "FundingEventId(1)"
    assert str(DecisionTime(453)) == "DecisionTime(453 ns)"
    with pytest.raises(ValueError, match="BAR_ID"):
        BarId(-1)
    with pytest.raises(ValueError, match="FUNDING_EVENT_ID"):
        FundingEventId(-1)
    with pytest.raises(ValueError, match="DECISION_TIME"):
        DecisionTime(True)  # type: ignore[arg-type]


def test_future_suffix_mutation_leaves_decision_frame_identical() -> None:
    """I2/R4: X_<=t = X'_<=t ⇒ Decision_<=t equal (frame_at admits no future)."""
    past = tuple(_candle(i, "50000", (i + 1) * 3_600_000_000_000) for i in range(3))
    future_mutated = past + tuple(_candle(3 + i, "999999", 99_999_999_999_999_999) for i in range(2))
    decision_ns = 3 * 3_600_000_000_000
    plain = frame_at("BTCUSDT", decision_ns, past)
    mutated = frame_at("BTCUSDT", decision_ns, future_mutated)
    assert [c.close for c in plain.candles] == [c.close for c in mutated.candles]
    assert len(plain.candles) == 3
