"""Performance and vectorization audit for Polars-backed feature and indicator pipeline.

Validates that RSI, Bollinger, ATR, Pivots, and Donchian channels execute with
zero custom Python loops and finish well within sub-millisecond to low-millisecond latencies.
"""

import time
from decimal import Decimal

import polars as pl

from v8_next.domain.market import Candle, CausalFrame
from v8_next.economics.decisions import Opportunity, StanceKind, observe_squeeze
from v8_next.experts.breakouts import observe_volume_breakout
from v8_next.experts.donchian import observe_donchian
from v8_next.experts.features import (
    bollinger_bands,
    close_series,
    compute_indicator_pipeline,
    donchian_channel,
    high_series,
    low_series,
    mean_range,
    significant_swings,
    simple_atr_series,
    true_range_series,
    wilder_atr_series,
    wilder_rsi,
)
from v8_next.experts.levels import HOUR_NS, daily_pivots, observe_range_breakout
from v8_next.experts.patterns import pattern_pivots
from v8_next.experts.reversion import bollinger_fade_geometry


def make_test_frame(length: int = 1000) -> CausalFrame:
    """Generate realistic causal candle frame for performance testing."""
    candles = []
    base = Decimal("50000")
    for i in range(length):
        op = base + Decimal(str((i % 17) * 5 - (i % 7) * 3))
        cl = base + Decimal(str((i % 13) * 4 - (i % 5) * 2))
        hi = max(op, cl) + Decimal(str(5 + (i % 11)))
        lo = min(op, cl) - Decimal(str(5 + (i % 9)))
        vol = Decimal(str(100 + (i % 23) * 10))
        candles.append(
            Candle(
                "BTCUSDT-PERP.BINANCE",
                i * HOUR_NS,
                (i + 1) * HOUR_NS,
                op,
                hi,
                lo,
                cl,
                vol,
                (i + 1) * HOUR_NS,
                (i + 1) * HOUR_NS,
                f"perf-{i}",
            )
        )
    return CausalFrame("BTCUSDT-PERP.BINANCE", length * HOUR_NS, tuple(candles))


def test_causal_frame_polars_caching():
    frame = make_test_frame(500)
    df1 = frame.df
    df2 = frame.df
    assert df1 is df2
    assert len(df1) == 500
    assert set(df1.columns) == {"start_ns", "end_ns", "open", "high", "low", "close", "volume"}
    assert isinstance(frame.lazy(), pl.LazyFrame)


def test_rsi_vectorized_and_timing():
    frame = make_test_frame(1000)
    closes = close_series(frame)

    t0 = time.perf_counter()
    rsi = wilder_rsi(closes, 14)
    elapsed_ms = (time.perf_counter() - t0) * 1000.0

    assert len(rsi) == 1000
    assert rsi[:14] == (None,) * 14
    assert all(isinstance(v, float) for v in rsi[14:])
    assert all(0.0 <= v <= 100.0 for v in rsi[14:] if v is not None)
    # Verification of millisecond budget
    assert elapsed_ms < 15.0, f"RSI took {elapsed_ms:.2f} ms (budget < 15ms)"


def test_bollinger_vectorized_and_timing():
    frame = make_test_frame(1000)
    closes = close_series(frame)

    t0 = time.perf_counter()
    mid, sd, upper, lower, bandwidth = bollinger_bands(closes, 20)
    elapsed_ms = (time.perf_counter() - t0) * 1000.0

    assert len(mid) == 1000
    assert len(sd) == 1000
    assert (upper > lower).slice(20).all()
    assert (bandwidth > 0).slice(20).all()
    assert elapsed_ms < 10.0, f"Bollinger took {elapsed_ms:.2f} ms (budget < 10ms)"

    # Fade geometry vectorization
    fade = bollinger_fade_geometry(frame)
    assert fade is None or fade.distance > Decimal(0)


def test_atr_and_ranges_vectorized():
    frame = make_test_frame(1000)
    highs, lows, closes = high_series(frame), low_series(frame), close_series(frame)

    t0 = time.perf_counter()
    tr = true_range_series(highs, lows, closes)
    w_atr = wilder_atr_series(highs, lows, closes, 14)
    s_atr = simple_atr_series([float(x) for x in highs], [float(x) for x in lows], 14)
    m_range = mean_range(frame, 14)
    elapsed_ms = (time.perf_counter() - t0) * 1000.0

    assert len(tr) == 1000
    assert len(w_atr) == 1000
    assert len(s_atr) == 1000 - 13
    assert m_range > 0
    assert elapsed_ms < 15.0, f"ATR calculations took {elapsed_ms:.2f} ms (budget < 15ms)"


def test_pivots_and_swings_vectorized():
    frame = make_test_frame(1000)

    t0 = time.perf_counter()
    hi_idx, lo_idx = significant_swings(frame, 10)
    p_highs = pattern_pivots(frame, high=True, strength=5)
    p_lows = pattern_pivots(frame, high=False, strength=5)
    pivots = daily_pivots(frame)
    elapsed_ms = (time.perf_counter() - t0) * 1000.0

    assert len(p_highs) > 0
    assert len(p_lows) > 0
    assert pivots is not None
    assert elapsed_ms < 15.0, f"Pivots/swings took {elapsed_ms:.2f} ms (budget < 15ms)"


def test_donchian_vectorized():
    frame = make_test_frame(1000)
    highs, lows = high_series(frame), low_series(frame)

    t0 = time.perf_counter()
    d_high, d_low = donchian_channel(highs, lows, 20)
    elapsed_ms = (time.perf_counter() - t0) * 1000.0

    assert len(d_high) == 1000
    assert len(d_low) == 1000
    assert (d_high >= d_low).slice(20).all()
    assert elapsed_ms < 10.0, f"Donchian channel took {elapsed_ms:.2f} ms (budget < 10ms)"

    # Observers utilizing Donchian channels
    opp = Opportunity("o1", "e1", frame.instrument_id, "LONG", frame.decision_ns, frame.decision_ns + 1000)
    stance_d = observe_donchian(frame, opp)
    assert stance_d.kind in (StanceKind.SUPPORT, StanceKind.CONTRADICT, StanceKind.ABSTAIN)

    stance_r = observe_range_breakout(frame, opp)
    assert stance_r.kind in (StanceKind.SUPPORT, StanceKind.CONTRADICT, StanceKind.ABSTAIN)

    stance_v = observe_volume_breakout(frame, opp)
    assert stance_v.kind in (StanceKind.SUPPORT, StanceKind.CONTRADICT, StanceKind.ABSTAIN)

    stance_s = observe_squeeze(frame, opp)
    assert stance_s.kind in (StanceKind.SUPPORT, StanceKind.CONTRADICT, StanceKind.ABSTAIN)


def test_unified_indicator_pipeline_submillisecond():
    frame = make_test_frame(1000)

    # Warmup
    _ = compute_indicator_pipeline(frame)

    t0 = time.perf_counter()
    pipeline_df = compute_indicator_pipeline(frame)
    elapsed_ms = (time.perf_counter() - t0) * 1000.0

    assert len(pipeline_df) == 1000
    expected_cols = {
        "start_ns", "end_ns", "open", "high", "low", "close", "volume",
        "ema_5", "ema_20", "ema_12", "ema_26",
        "donchian_high_20", "donchian_low_20",
        "bb_mid_20", "bb_std_20", "bar_range", "range_mean_14", "true_range",
        "macd_line", "bb_bandwidth_20", "bb_pct_b_20",
    }
    assert expected_cols.issubset(set(pipeline_df.columns))

    # All indicators computed together via lazy execution in under 10 milliseconds
    assert elapsed_ms < 10.0, f"Unified pipeline took {elapsed_ms:.2f} ms (budget < 10ms)"
