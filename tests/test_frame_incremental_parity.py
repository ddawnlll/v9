"""Frame incremental parity (#463).

MECHANICS ONLY synthetic section: small-candle equivalence of the
validated-incremental path against full revalidation, with zero evaluative
weight. No test here asserts economic performance on synthetic data.

Real-window section: old vs new frame hashes equal on a real window; skips
when the tape is absent. No PIT-semantics change: any doubt falls back to
the full path, so verdicts stay identical.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

from v8_next.adapters.tape_paths import tape_path
from v8_next.domain.market import Candle, CausalFrame, frame_at, frame_at_incremental

BTC_TAPE = tape_path("btcusdt-1h-12m", "tape.jsonl")
INSTRUMENT = "BTCUSDT-PERP.BINANCE"


def _synth_candles(n: int = 12) -> tuple[Candle, ...]:
    out: list[Candle] = []
    base = 1_000_000_000
    step = 3_600_000_000_000
    px = Decimal("100")
    for i in range(n):
        start = base + i * step
        end = start + step
        out.append(
            Candle(
                instrument_id=INSTRUMENT,
                start_ns=start,
                end_ns=end,
                open=px,
                high=px * Decimal("1.001"),
                low=px * Decimal("0.999"),
                close=px * Decimal("1.0005"),
                volume=Decimal("1"),
                received_ns=end,
                available_ns=end,
                source_hash="mech",
            )
        )
        px = px * Decimal("1.0005")
    return tuple(out)


def _hash(frame: CausalFrame) -> tuple[object, ...]:
    return (
        frame.decision_ns,
        tuple((c.start_ns, c.end_ns) for c in frame.candles),
    )


# ---------------------------------------------------------------------------
# MECHANICS ONLY (synthetic candles, equivalence coverage)
# ---------------------------------------------------------------------------


def test_mechanics_incremental_matches_full_on_prefixes() -> None:
    candles = _synth_candles(12)
    parent = None
    for i in range(2, len(candles)):
        prefix = candles[: i + 1]
        decision_ns = prefix[-1].end_ns
        expected = frame_at(INSTRUMENT, decision_ns, prefix)
        got = frame_at_incremental(parent, INSTRUMENT, decision_ns, prefix)
        assert _hash(got) == _hash(expected)
        parent = got


def test_mechanics_incremental_fallback_preserves_verdicts() -> None:
    candles = _synth_candles(6)
    prefix = candles[:4]
    decision_ns = prefix[-1].end_ns
    parent = frame_at(INSTRUMENT, decision_ns, prefix)
    # Time going backwards must delegate to the full path (identical verdict).
    earlier = prefix[-1].end_ns - 1
    assert _hash(frame_at_incremental(parent, INSTRUMENT, earlier, prefix)) == _hash(
        frame_at(INSTRUMENT, earlier, prefix)
    )
    # Foreign instrument tail must delegate, not silently trust. frame_at
    # filters foreign bars (it does not raise); the incremental path must
    # return the identical filtered frame.
    foreign = tuple(
        Candle(
            instrument_id="OTHER-PERP.BINANCE",
            start_ns=c.start_ns,
            end_ns=c.end_ns,
            open=c.open,
            high=c.high,
            low=c.low,
            close=c.close,
            volume=c.volume,
            received_ns=c.received_ns,
            available_ns=c.available_ns,
            source_hash=c.source_hash,
        )
        for c in candles[4:5]
    )
    mixed = prefix + foreign
    assert _hash(frame_at_incremental(parent, INSTRUMENT, mixed[-1].end_ns, mixed)) == _hash(
        frame_at(INSTRUMENT, mixed[-1].end_ns, mixed)
    )


# ---------------------------------------------------------------------------
# Real-window parity (frame hashes equal on a real window)
# ---------------------------------------------------------------------------


def test_frame_incremental_parity_on_real_window(cached_tape_candles: Any) -> None:
    if not BTC_TAPE.exists():
        pytest.skip(f"real BTC tape absent at {BTC_TAPE}")
    candles = tuple(cached_tape_candles(BTC_TAPE, limit=120))
    if len(candles) < 20:
        pytest.skip("tape loaded too few candles for a frame parity window")
    instrument = candles[0].instrument_id
    parent = None
    checked = 0
    for i in range(10, min(len(candles), 60)):
        prefix = candles[: i + 1]
        decision_ns = prefix[-1].end_ns
        expected = frame_at(instrument, decision_ns, prefix)
        got = frame_at_incremental(parent, instrument, decision_ns, prefix)
        assert _hash(got) == _hash(expected)
        parent = got
        checked += 1
    assert checked > 0

pytestmark = pytest.mark.slow  # #469: tape/engine file, fast loop excludes via -m "not slow"
