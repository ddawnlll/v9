"""Equivalence pins for the quadratic-elimination fast paths.

Every fast path below falls back to a legacy full-prefix computation on any
doubt; these tests prove the fast path and the legacy path agree on
adversarial inputs (including flat/gain-only/loss-only RSI seeds, session
boundaries, gaps, and spanning bars). They run in ~2s and guard the exact
soundness contract the performance work depends on.
"""

import random
import sys
from decimal import Decimal
from pathlib import Path

import polars as pl

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from v8_next.domain.market import (  # noqa: E402
    Candle,
    CausalFrame,
    build_candle_dataframe,
    extend_causal_frame,  # noqa: E402
)
from v8_next.experts.confluence import recovery_direction  # noqa: E402
from v8_next.experts.engine import FeatureEngine  # noqa: E402
from v8_next.experts.features import (  # noqa: E402
    _ACTIVE_LAZY,
    _emas_full,
    _LazyBundle,
    _macd_full,
    _significant_swings_full,
    _wilder_rsi_full,
    macd_line,
    significant_swings,
    trend_emas,
    wilder_rsi,
)
from v8_next.experts.levels import DAY_NS, previous_session_bars  # noqa: E402
from v8_next.experts.pandf import _columns_validated, columns  # noqa: E402
from v8_next.experts.patterns import _pattern_pivots_full, pattern_pivots  # noqa: E402
from v8_next.experts.reversion import recovery_run_start  # noqa: E402

HOUR = 3600_000_000_000


def _candle(symbol: str, start: int, o: float, h: float, low: float, c: float) -> Candle:
    return Candle(
        symbol,
        start,
        start + HOUR,
        Decimal(str(o)),
        Decimal(str(h)),
        Decimal(str(low)),
        Decimal(str(c)),
        Decimal(10),
        start + HOUR,
        start + HOUR,
        "h",
    )


def test_recovery_run_start_matches_series_scan() -> None:
    rng = random.Random(7)
    for _ in range(500):
        n = rng.randint(1, 60)
        vals = tuple(
            rng.choice([None, round(rng.uniform(0, 100), 6)]) for _ in range(n)
        )
        for threshold, above in ((30, True), (70, False)):
            series = pl.Series("rsi", list(vals), dtype=pl.Float64)
            mask = ((series > threshold) if above else (series < threshold)).fill_null(False)
            if not mask[-1]:
                want = None
            else:
                false_indices = (~mask).arg_true()
                want = int(false_indices[-1]) + 1 if len(false_indices) else 0
            assert recovery_run_start(vals, threshold, above) == want
    assert recovery_direction((5.0, 31.0)) == "LONG"
    assert recovery_direction((None, 31.0)) is None
    assert recovery_direction((None, None)) is None


def test_columns_validated_matches_columns() -> None:
    rng = random.Random(11)
    for _ in range(300):
        price = Decimal("100")
        closes = []
        for _ in range(rng.randint(0, 40)):
            price += Decimal(rng.randint(-500, 500)) / Decimal(100)
            closes.append(price if price > 0 else Decimal("0.01"))
        box = Decimal(rng.randint(1, 300)) / Decimal(100)
        assert _columns_validated(tuple(closes), box) == columns(tuple(closes), box)


def _legacy_session_bars(frame: CausalFrame):
    if not frame.continuous:
        raise ValueError("source gap")
    if not frame.candles:
        return None
    from v8_next.experts.levels import DAY_NS as DAY

    session = frame.candles[-1].start_ns // DAY * DAY
    previous = tuple(
        c for c in frame.candles if session - DAY <= c.start_ns and c.end_ns <= session
    )
    if (
        len(previous) != 24
        or previous[0].start_ns != session - DAY
        or previous[-1].end_ns != session
        or any(c.end_ns - c.start_ns != HOUR for c in previous)
    ):
        return None
    return previous


def test_previous_session_bars_matches_filter() -> None:
    rng = random.Random(23)
    base = 1735689600000000000
    for trial in range(300):
        mode = trial % 4
        n = rng.randint(0, 100)
        if mode == 0:
            off = rng.randint(0, 30)
            candles = tuple(
                _candle("T", base + (off + i) * HOUR, 100, 101, 99, 100) for i in range(n)
            )
        elif mode == 1:
            candles, t = [], base
            for _ in range(n):
                duration = HOUR if rng.random() > 0.1 else 2 * HOUR
                candles.append(_candle("T", t, 100, 101, 99, 100))
                t += duration
            candles = tuple(candles)
        elif mode == 2:
            day = rng.randint(0, 3)
            candles = tuple(
                _candle("T", base + day * DAY_NS + i * HOUR, 100, 101, 99, 100)
                for i in range(n)
            )
        else:
            t = base + rng.randint(0, 50) * HOUR
            candles = tuple(_candle("T", t + i * HOUR, 100, 101, 99, 100) for i in range(n))
        frame = (
            CausalFrame("T", 0, ())
            if not candles
            else CausalFrame("T", candles[-1].end_ns, candles, _trusted_order=True)
        )
        try:
            want = _legacy_session_bars(frame)
        except ValueError:
            want = "RAISE"
        try:
            got = previous_session_bars(frame)
        except ValueError:
            got = "RAISE"
        assert got == want


def _replay_frames(closes: list[float]) -> tuple[tuple[Candle, ...], object]:
    candles = tuple(
        _candle("T-PERP.BINANCE", i * HOUR, c, c * 1.001, c * 0.999, c)
        for i, c in enumerate(closes)
    )
    return candles, build_candle_dataframe(candles)


def test_engine_pivots_match_full_recompute() -> None:
    rng = random.Random(41)
    closes = [100.0]
    for _ in range(199):
        closes.append(max(closes[-1] * (1 + rng.uniform(-0.03, 0.03)), 1.0))
    candles, full_df = _replay_frames(closes)
    engine = FeatureEngine()
    parent = None
    for n in range(1, 201):
        prefix = candles[:n]
        if parent is None:
            frame = CausalFrame.from_ordered_prefix(
                "T-PERP.BINANCE", prefix[-1].end_ns, prefix, full_df
            )
        else:
            frame = extend_causal_frame(parent, prefix[-1].end_ns, prefix[-1], full_df)
            assert frame is not None
        parent = frame
        token = _ACTIVE_LAZY.set(_LazyBundle(frame, engine))
        try:
            for strength in (3, 10):
                for high in (True, False):
                    assert pattern_pivots(frame, high=high, strength=strength) == (
                        _pattern_pivots_full(frame, high=high, strength=strength)
                    )
            if n >= 34:
                assert significant_swings(frame, 10) == _significant_swings_full(frame, 10)
        finally:
            _ACTIVE_LAZY.reset(token)


def test_engine_ema_rsi_match_full_recompute() -> None:
    rng = random.Random(59)
    regimes = {
        "flat": [100.0] * 250,
        "gain_only": [100.0 + i * 0.5 for i in range(250)],
        "volatile": [abs(100.0 + rng.uniform(-40, 40)) + 1.0 for _ in range(250)],
    }
    for closes in regimes.values():
        candles, full_df = _replay_frames(closes)
        engine = FeatureEngine()
        parent = None
        for n in range(1, 251):
            prefix = candles[:n]
            if parent is None:
                frame = CausalFrame.from_ordered_prefix(
                    "T-PERP.BINANCE", prefix[-1].end_ns, prefix, full_df
                )
            else:
                frame = extend_causal_frame(parent, prefix[-1].end_ns, prefix[-1], full_df)
            parent = frame
            token = _ACTIVE_LAZY.set(_LazyBundle(frame, engine))
            try:
                series = frame.df["close"]
                if n >= 20:
                    assert trend_emas(frame) == _emas_full(series)
                if n >= 34:
                    assert macd_line(frame) == _macd_full(series)
                if n > 14:
                    assert wilder_rsi(series) == _wilder_rsi_full(series, 14)
            finally:
                _ACTIVE_LAZY.reset(token)
