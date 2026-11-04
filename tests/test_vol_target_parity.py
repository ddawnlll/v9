"""Vol-target vectorization parity (#465).

MECHANICS ONLY synthetic section: Polars rolling_std(ddof=1) vs per-window
numpy std on small log-return vectors, with zero evaluative weight. No test
here asserts economic performance on synthetic data.

Real-window section: identical rolling stats on real closes; skips when the
tape is absent. Float divergence -> STOP and keep the loop (per issue).
No numba: stdlib+polars decomposition only.
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np
import polars as pl
import pytest

from v8_next.adapters.tape_paths import tape_path
from v8_next.evaluation.economic_benchmark import (
    VOL_LOOKBACK,
    bars_from_candles,
    compute_benchmark_family,
)

BTC_TAPE = tape_path("btcusdt-1h-12m", "tape.jsonl")


def _rolling_numpy(logrets: list[float]) -> list[float | None]:
    out: list[float | None] = []
    lr = logrets[1:]
    for p in range(len(lr)):
        window = lr[max(0, p - VOL_LOOKBACK + 1) : p + 1]
        if len(window) < 2:
            out.append(None)
        else:
            out.append(float(np.std(np.asarray(window), ddof=1)))
    return out


def _rolling_polars(logrets: list[float]) -> list[float | None]:
    lr = logrets[1:]
    if not lr:
        return []
    return list(
        pl.Series(lr).rolling_std(window_size=VOL_LOOKBACK, ddof=1, min_samples=2).to_list()
    )


def test_mechanics_rolling_std_matches_numpy() -> None:
    logrets = [0.0, 0.001, -0.002, 0.003, -0.001, 0.002, 0.0, -0.003]
    for a, b in zip(_rolling_numpy(logrets), _rolling_polars(logrets), strict=True):
        if a is None or b is None:
            assert a is None and b is None
        else:
            assert a == pytest.approx(b, rel=1e-12, abs=1e-15)


def test_vol_target_rolling_parity_on_real_closes(cached_tape_candles: Any) -> None:
    if not BTC_TAPE.exists():
        pytest.skip(f"real BTC tape absent at {BTC_TAPE}")
    candles = tuple(cached_tape_candles(BTC_TAPE, limit=120))
    if len(candles) < 60:
        pytest.skip("tape loaded too few candles for a rolling parity window")
    closes = [float(c.close) for c in candles]
    n = len(closes)
    logrets = [0.0] + [math.log(closes[i] / closes[i - 1]) for i in range(1, n)]
    for a, b in zip(_rolling_numpy(logrets), _rolling_polars(logrets), strict=True):
        if a is None or b is None:
            assert a is None and b is None
        else:
            assert a == pytest.approx(b, rel=1e-12, abs=1e-15)
    bars = bars_from_candles(candles)
    fams = compute_benchmark_family(bars, 10000.0, 0.0005)
    assert len(fams["vol_target"]["equity"]) == n
    assert all(v >= 0 for v in fams["vol_target"]["exposure"])

pytestmark = pytest.mark.slow  # #469: tape/engine file, fast loop excludes via -m "not slow"
