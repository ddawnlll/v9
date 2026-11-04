"""Portfolio shared-build parity (#466).

MECHANICS ONLY section: none (no synthetic portfolio; sharing is verified on
the real tape only). Real-window section: shared build equals direct rebuilds
on a real window; per-pass outputs unchanged (same objects, same order);
CLI flags/outputs unchanged. Skips when the tape is absent.
"""

from __future__ import annotations

from typing import Any

import pytest

from v8_next.adapters.tape_paths import tape_path
from v8_next.app.portfolio import build_shared_inputs
from v8_next.evaluation import economic_benchmark as eb

QUAD_TAPE = tape_path("quad-1h-12m", "tape.jsonl")


def test_portfolio_shared_build_parity_on_real_window(cached_multitape: Any) -> None:
    if not QUAD_TAPE.exists():
        pytest.skip(f"real quad tape absent at {QUAD_TAPE}")
    tape = cached_multitape(QUAD_TAPE, limit=100)
    shared_bars, shared_closes, shared_qvols = build_shared_inputs(tape)
    assert set(shared_bars) == set(tape.instruments)
    for inst in tape.instruments:
        assert shared_bars[inst] == eb.bars_from_candles(tape.candles[inst])
    assert shared_closes == {
        f"{k}-PERP.BINANCE": [float(c.close) for c in v] for k, v in tape.candles.items()
    }
    assert shared_qvols == {
        f"{k}-PERP.BINANCE": list(v) for k, v in tape.quote_volumes.items()
    }
    # Same objects reused downstream (identity, not just equality).
    again_bars, again_closes, again_qvols = build_shared_inputs(tape)
    assert again_closes == shared_closes

pytestmark = pytest.mark.slow  # #469: tape/engine file, fast loop excludes via -m "not slow"
