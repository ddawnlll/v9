"""Expert bundle parity (#464).

MECHANICS ONLY synthetic section: bundle-vs-direct equivalence on small
frames, with zero evaluative weight. No test here asserts economic
performance on synthetic data.

Real-window section: stance-stream hash equal (bundled observe_all_28 vs
per-expert direct path without a bundle) on a real window; skips when the
tape is absent. Per-bar invalidation: no state survives to the next bar.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

from v8_next.adapters.tape_paths import tape_path
from v8_next.domain.market import Candle, frame_at
from v8_next.economics.decisions import opportunity_at
from v8_next.experts.features import _ACTIVE_LAZY
from v8_next.experts.registry import CANONICAL_28_EXPERTS, observe_all_28, observe_expert

BTC_TAPE = tape_path("btcusdt-1h-12m", "tape.jsonl")
INSTRUMENT = "BTCUSDT-PERP.BINANCE"


def _synth_frame(n: int = 30):
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
                close=px,
                volume=Decimal("1"),
                received_ns=end,
                available_ns=end,
                source_hash="mech",
            )
        )
    candles = tuple(out)
    return frame_at(INSTRUMENT, candles[-1].end_ns, candles)


def _stance_hash(stances) -> tuple[tuple[str, str, str], ...]:
    return tuple((s.observer_id, s.kind.value, s.reason) for s in stances)


def test_mechanics_bundle_matches_direct_path() -> None:
    frame = _synth_frame(30)
    opportunity = opportunity_at(frame)
    bundled = observe_all_28(frame, opportunity)
    assert _ACTIVE_LAZY.get() is None  # per-bar invalidation: cleared afterwards
    direct = tuple(observe_expert(spec_id, frame, opportunity) for spec_id in CANONICAL_28_EXPERTS)
    assert _stance_hash(bundled) == _stance_hash(direct)


def test_expert_bundle_parity_on_real_window(cached_tape_candles: Any) -> None:
    if not BTC_TAPE.exists():
        pytest.skip(f"real BTC tape absent at {BTC_TAPE}")
    candles = tuple(cached_tape_candles(BTC_TAPE, limit=120))
    if len(candles) < 60:
        pytest.skip("tape loaded too few candles for an expert parity window")
    frame = frame_at(candles[0].instrument_id, candles[-1].end_ns, candles)
    opportunity = opportunity_at(frame)
    bundled = observe_all_28(frame, opportunity)
    assert len(bundled) == 28
    direct = tuple(observe_expert(spec_id, frame, opportunity) for spec_id in CANONICAL_28_EXPERTS)
    assert _stance_hash(bundled) == _stance_hash(direct)
    assert _ACTIVE_LAZY.get() is None

pytestmark = pytest.mark.slow  # #469: tape/engine file, fast loop excludes via -m "not slow"
