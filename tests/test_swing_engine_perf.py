"""Swing-engine bounded-frame parity via the production path (no economic claim).

Evidence classes:
* mechanics (synthetic bars, MECHANICS ONLY) — production
  ``SwingEngineStrategy._decision_frame``/``on_bar`` stay causal, reject future
  data, preserve gap/duplicate/overlap/duration semantics via exact fallback,
  and restrict the fast path to the proven grammar+protection combination.
  Zero evaluative weight; never asserts profitability.
* evaluative (real tape) — production ``_decision_frame`` yields identical
  ``swing_signal`` decisions to full-history ``frame_at`` sequentially on real
  BTC hourly tape, with no future data. Skips when the tape is absent.

The 128 bound is a conservative documented bound proven only for the exact
``plain_swing`` / ``range-breakout-48-v1`` / ``squeeze:baseline:v2``
combination (49-bar grammar, 69-bar squeeze baseline warmup, 73 worst-case for
m2/m3, 14-bar span). It is not an empirically universal guarantee: other
policies use the unchanged exact path via ``_fast_path_eligible``, and if
policy dependencies change the bound, guard and tests must be revisited
together.
"""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from v8_next.adapters.swing_engine import (
    SWING_FRAME_WINDOW_BARS,
    SwingEngineConfig,
    SwingEngineStrategy,
)
from v8_next.domain.market import Candle, frame_at
from v8_next.economics.grammar import POLICY_REQUIRED_BARS
from v8_next.economics.swing_baseline import policy_spec, swing_signal
from v8_next.evaluation.gate_resolution import load_tape_candles

REPO_ROOT = Path(__file__).resolve().parents[2]
TAPE = REPO_ROOT / "research" / "tape" / "multi-1h-4y" / "tape.jsonl"
HOUR_NS = 3_600 * 10**9
INSTRUMENT = "BTCUSDT-PERP.BINANCE"


def _synthetic_candle(
    index: int,
    *,
    start_ns: int = 1_700_000_000_000_000_000,
    instrument: str = INSTRUMENT,
    available_ns: int | None = None,
) -> Candle:
    # MECHANICS ONLY synthetic bar. Continuous hourly grid like real tape:
    # end_ns == next start_ns, available == end unless overridden.
    bar_start = start_ns + index * HOUR_NS
    bar_end = bar_start + HOUR_NS
    base = Decimal("20000") + Decimal(index * 5)
    return Candle(
        instrument_id=instrument,
        start_ns=bar_start,
        received_ns=bar_end,
        available_ns=bar_end if available_ns is None else available_ns,
        end_ns=bar_end,
        source_hash="mechanics-only",
        open=base,
        high=base + Decimal("50"),
        low=base - Decimal("50"),
        close=base + Decimal("10"),
        volume=Decimal("1"),
    )


def _production_strategy(
    candles: list[Candle], *, policy_id: str = "plain_swing"
) -> SwingEngineStrategy:
    """Build the real strategy and feed history through the real ``on_bar``.

    A huge warmup suppresses order submission (no engine attached) while still
    exercising the production guard bookkeeping in ``on_bar``. Bars are fed via
    stub objects exposing only ``ts_event``, which is all ``on_bar`` reads.
    """
    cfg = SwingEngineConfig(policy_id=policy_id)
    # Key by position, not by end_ns, so duplicate/conflicting histories keep
    # their order instead of collapsing in a dict keyed by timestamp.
    source: dict[int, Candle] = {i: c for i, c in enumerate(candles)}
    strategy = SwingEngineStrategy(cfg, source, warmup_bars=10**9)
    for i in range(len(candles)):
        strategy.on_bar(SimpleNamespace(ts_event=i))  # type: ignore[arg-type]
    return strategy


def _source_by_end(candles: list[Candle]) -> dict[int, Candle]:
    return {c.end_ns: c for c in candles}


def test_window_covers_proven_lookback() -> None:
    assert POLICY_REQUIRED_BARS["range-breakout-48-v1"] == 49
    assert SWING_FRAME_WINDOW_BARS >= 73
    assert SWING_FRAME_WINDOW_BARS >= POLICY_REQUIRED_BARS["range-breakout-48-v1"]
    assert SWING_FRAME_WINDOW_BARS == 128


def test_production_plain_swing_beyond_window_is_bounded_mechanics_only() -> None:
    # MECHANICS ONLY: clean 200-bar history through the production path.
    history = [_synthetic_candle(i) for i in range(200)]
    strategy = _production_strategy(history, policy_id="plain_swing")
    frame = strategy._decision_frame(history[-1])
    full = frame_at(INSTRUMENT, history[-1].end_ns, tuple(history))
    assert len(strategy.seen) == 200
    assert len(frame.candles) == SWING_FRAME_WINDOW_BARS
    assert frame.continuous
    assert full.continuous
    # No future data: every candle admissible at the decision instant.
    assert all(
        c.available_ns is not None and c.available_ns <= history[-1].end_ns
        for c in frame.candles
    )
    spec = policy_spec("plain_swing")
    # Parity with the exact path on the same history.
    full_decision = swing_signal(full, spec, bar_ns=HOUR_NS)
    bounded_decision = swing_signal(frame, spec, bar_ns=HOUR_NS)
    if full_decision is None or bounded_decision is None:
        assert full_decision is None and bounded_decision is None
    else:
        assert full_decision.as_dict() == bounded_decision.as_dict()


def test_production_causal_trend_beyond_window_stays_exact_mechanics_only() -> None:
    # MECHANICS ONLY: the fast path is proven only for plain_swing. Any other
    # policy (here causal_trend, whose dependencies were not proven bounded)
    # must use the unchanged exact path even beyond 128 bars. This fails on the
    # pre-guard implementation, which truncated every policy to 128.
    history = [_synthetic_candle(i) for i in range(200)]
    strategy = _production_strategy(history, policy_id="causal_trend")
    frame = strategy._decision_frame(history[-1])
    full = frame_at(INSTRUMENT, history[-1].end_ns, tuple(history))
    assert len(strategy.seen) == 200
    assert len(frame.candles) == len(full.candles) == 200
    assert frame.candles == full.candles


def test_production_foreign_old_candle_forces_exact_mechanics_only() -> None:
    # MECHANICS ONLY: a foreign-instrument bar at index 10 (outside the final
    # 128 tail) is filtered by exact frame_at, creating a gap. A naive tail
    # would hide it and look continuous. Production must latch globally and
    # return the exact filtered gap.
    history = [_synthetic_candle(i) for i in range(200)]
    history[10] = _synthetic_candle(10, instrument="ETHUSDT-PERP.BINANCE")
    strategy = _production_strategy(history, policy_id="plain_swing")
    assert strategy._history_has_foreign
    frame = strategy._decision_frame(history[-1])
    full = frame_at(INSTRUMENT, history[-1].end_ns, tuple(history))
    assert len(full.candles) == 199
    assert not full.continuous
    assert frame.candles == full.candles
    assert not frame.continuous
    assert len(frame.candles) != SWING_FRAME_WINDOW_BARS


def test_production_late_old_candle_forces_exact_mechanics_only() -> None:
    # MECHANICS ONLY: an unavailable old bar (available None) outside the tail
    # is filtered by exact frame_at. Production must not hide it.
    history = [_synthetic_candle(i) for i in range(200)]
    missing = history[10]
    history[10] = Candle(
        instrument_id=missing.instrument_id,
        start_ns=missing.start_ns,
        end_ns=missing.end_ns,
        open=missing.open,
        high=missing.high,
        low=missing.low,
        close=missing.close,
        volume=missing.volume,
        received_ns=missing.received_ns,
        available_ns=None,
        source_hash="mechanics-only",
    )
    strategy = _production_strategy(history, policy_id="plain_swing")
    assert strategy._history_has_late_or_missing
    frame = strategy._decision_frame(history[-1])
    full = frame_at(INSTRUMENT, history[-1].end_ns, tuple(history))
    assert len(full.candles) == 199
    assert not full.continuous
    assert frame.candles == full.candles
    assert not frame.continuous


def test_production_gap_stays_exact_mechanics_only() -> None:
    # MECHANICS ONLY: a raw one-bar gap must stay non-continuous via fallback.
    history = [_synthetic_candle(i) for i in range(200)]
    gapped = history[:50] + history[51:]
    strategy = _production_strategy(gapped, policy_id="plain_swing")
    assert not strategy._history_continuous
    frame = strategy._decision_frame(gapped[-1])
    full = frame_at(INSTRUMENT, gapped[-1].end_ns, tuple(gapped))
    assert not full.continuous
    assert frame.candles == full.candles
    assert not frame.continuous


def test_production_duplicate_preserves_error_mechanics_only() -> None:
    # MECHANICS ONLY: conflicting versions at the same start_ns must raise the
    # same ValueError as the exact path, never be silently deduped by truncation.
    history = [_synthetic_candle(i) for i in range(200)]
    conflicting = replace(history[10], close=history[10].close + Decimal("5"))
    assert conflicting.start_ns == history[10].start_ns
    assert conflicting != history[10]
    duped = history[:100] + [conflicting] + history[100:]
    # Exact path raises on conflicting bar versions.
    with pytest.raises(ValueError, match="conflicting"):
        frame_at(INSTRUMENT, duped[-1].end_ns, tuple(history[:101] + [conflicting]))
    strategy = _production_strategy(duped, policy_id="plain_swing")
    assert strategy._history_has_duplicate
    with pytest.raises(ValueError, match="conflicting"):
        strategy._decision_frame(duped[-1])


def test_production_irregular_duration_forces_exact_mechanics_only() -> None:
    # MECHANICS ONLY: an old 30-minute bar outside the tail must force fallback;
    # the exact path raises on irregular durations once an opportunity exists,
    # or at least preserves a different admissible frame than a naive tail.
    history = [_synthetic_candle(i) for i in range(200)]
    short = history[10]
    history[10] = Candle(
        instrument_id=short.instrument_id,
        start_ns=short.start_ns,
        end_ns=short.start_ns + HOUR_NS // 2,
        open=short.open,
        high=short.high,
        low=short.low,
        close=short.close,
        volume=short.volume,
        received_ns=short.start_ns + HOUR_NS // 2,
        available_ns=short.start_ns + HOUR_NS // 2,
        source_hash="mechanics-only",
    )
    strategy = _production_strategy(history, policy_id="plain_swing")
    assert not strategy._duration_regular
    frame = strategy._decision_frame(history[-1])
    full = frame_at(INSTRUMENT, history[-1].end_ns, tuple(history))
    assert frame.candles == full.candles
    assert len(frame.candles) != SWING_FRAME_WINDOW_BARS


def test_production_no_future_data_mechanics_only() -> None:
    # MECHANICS ONLY: sequential production frames must never contain bars
    # beyond the decision instant.
    history = [_synthetic_candle(i) for i in range(150)]
    cfg = SwingEngineConfig(policy_id="plain_swing")
    source: dict[int, Candle] = {i: c for i, c in enumerate(history)}
    strategy = SwingEngineStrategy(cfg, source, warmup_bars=10**9)
    for i, candle in enumerate(history):
        strategy.on_bar(SimpleNamespace(ts_event=i))  # type: ignore[arg-type]
        frame = strategy._decision_frame(candle)
        assert all(c.end_ns <= candle.end_ns for c in frame.candles)
        assert all(
            c.available_ns is not None and c.available_ns <= candle.end_ns
            for c in frame.candles
        )
        assert frame.candles[-1].end_ns == candle.end_ns


def test_production_sequential_plain_swing_parity_on_real_tape() -> None:
    # Evaluative: every sequential decision through the production method
    # matches the exact full-history path on real tape, with no future data.
    if not TAPE.is_file():
        pytest.skip(f"real tape absent at {TAPE}")
    candles = load_tape_candles(TAPE, instrument="BTCUSDT", limit=200)
    if len(candles) < 200:
        pytest.skip("tape loaded too few candles")
    spec = policy_spec("plain_swing")
    cfg = SwingEngineConfig(policy_id="plain_swing")
    source: dict[int, Candle] = {i: c for i, c in enumerate(candles)}
    strategy = SwingEngineStrategy(cfg, source, warmup_bars=10**9)
    mismatches = 0
    for i, candle in enumerate(candles):
        strategy.on_bar(SimpleNamespace(ts_event=i))  # type: ignore[arg-type]
        assert len(strategy.seen) == i + 1
        frame = strategy._decision_frame(candle)
        history = candles[: i + 1]
        full = frame_at(INSTRUMENT, candle.end_ns, tuple(history))
        assert all(c.available_ns is not None and c.available_ns <= candle.end_ns for c in frame.candles)
        assert all(c.available_ns is not None and c.available_ns <= candle.end_ns for c in full.candles)
        full_decision = swing_signal(full, spec, bar_ns=HOUR_NS)
        prod_decision = swing_signal(frame, spec, bar_ns=HOUR_NS)
        if full_decision is None and prod_decision is None:
            continue
        if (full_decision is None) != (prod_decision is None):
            mismatches += 1
            continue
        assert full_decision is not None and prod_decision is not None
        if full_decision.as_dict() != prod_decision.as_dict():
            mismatches += 1
    assert mismatches == 0


def test_production_truncation_boundary_on_real_tape() -> None:
    # Evaluative: around bar 128 the production path switches from exact to
    # bounded; the switch must be seamless on real tape.
    if not TAPE.is_file():
        pytest.skip(f"real tape absent at {TAPE}")
    candles = load_tape_candles(TAPE, instrument="BTCUSDT", limit=200)
    if len(candles) < 200:
        pytest.skip("tape loaded too few candles")
    spec = policy_spec("plain_swing")
    cfg = SwingEngineConfig(policy_id="plain_swing")
    source: dict[int, Candle] = {i: c for i, c in enumerate(candles)}
    strategy = SwingEngineStrategy(cfg, source, warmup_bars=10**9)
    for i in range(len(candles)):
        strategy.on_bar(SimpleNamespace(ts_event=i))  # type: ignore[arg-type]
    for idx in (127, 128, 129, 150, 199):
        candle = candles[idx]
        # Replay the prefix through a fresh production strategy to match exact
        # sequential guard state at that prefix length.
        prefix = candles[: idx + 1]
        sub = SwingEngineStrategy(
            cfg, {i: c for i, c in enumerate(prefix)}, warmup_bars=10**9
        )
        for i in range(len(prefix)):
            sub.on_bar(SimpleNamespace(ts_event=i))  # type: ignore[arg-type]
        frame = sub._decision_frame(candle)
        full = frame_at(INSTRUMENT, candle.end_ns, tuple(prefix))
        if idx < SWING_FRAME_WINDOW_BARS:
            assert len(frame.candles) == len(full.candles)
        else:
            assert len(frame.candles) == SWING_FRAME_WINDOW_BARS
        full_decision = swing_signal(full, spec, bar_ns=HOUR_NS)
        prod_decision = swing_signal(frame, spec, bar_ns=HOUR_NS)
        if full_decision is None or prod_decision is None:
            assert full_decision is None and prod_decision is None
        else:
            assert full_decision.as_dict() == prod_decision.as_dict()

pytestmark = pytest.mark.slow  # #469: tape/engine file, fast loop excludes via -m "not slow"
