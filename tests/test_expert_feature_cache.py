"""Feature-cache contracts (#15 / M1-06).

Mechanics (synthetic, zero evaluative weight): spec validation, keyed values,
incremental extension, explicit invalidation, bounded memory, fail-closed
errors. Real tape (slow): the 28 expert stances and reason codes are identical
with and without the cache, and the cache proves the prefix append-only
(extensions without reseeds).
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

from v8_next.adapters.tape_paths import tape_path
from v8_next.domain.market import Candle, CausalFrame, build_candle_dataframe, extend_causal_frame
from v8_next.economics.decisions import opportunity_at
from v8_next.experts.cache import (
    DEFAULT_MAX_ENTRIES,
    FEATURE_CACHE_VERSION,
    MACD_LINE,
    MEAN_RANGE_14,
    RSI_14,
    SIGNIFICANT_SWINGS_10,
    TREND_EMAS,
    CacheKey,
    FeatureCache,
    FeatureSpec,
    prefix_digest,
)
from v8_next.experts.features import (
    close_series,
    macd_line,
    mean_range,
    significant_swings,
    trend_emas,
    wilder_rsi,
)
from v8_next.experts.registry import observe_all_28

BTC_TAPE = tape_path("btcusdt-1h-2025Q1", "tape.jsonl")
INSTRUMENT = "BTCUSDT-PERP.BINANCE"
STEP_NS = 3_600_000_000_000


def _candles(
    n: int,
    *,
    instrument: str = INSTRUMENT,
    base_ns: int = 1_600_000_000_000_000_000,
    start_px: str = "100",
    drift: str = "0.4",
) -> tuple[Candle, ...]:
    out: list[Candle] = []
    price = Decimal(start_px)
    step = Decimal(drift)
    for i in range(n):
        start = base_ns + i * STEP_NS
        end = start + STEP_NS
        close = price + step * i
        out.append(
            Candle(
                instrument_id=instrument,
                start_ns=start,
                end_ns=end,
                open=close - step / 2,
                high=close + step,
                low=close - step,
                close=close,
                volume=Decimal("10") + Decimal(i % 3),
                received_ns=end,
                available_ns=end,
                source_hash=f"mech-{i}",
            )
        )
    return tuple(out)


def _frame(candles: tuple[Candle, ...], df: Any = None) -> CausalFrame:
    return CausalFrame.from_ordered_prefix(
        candles[-1].instrument_id,
        candles[-1].end_ns,
        candles,
        build_candle_dataframe(candles) if df is None else df,
    )


# --------------------------------------------------------------------------- #
# mechanics: specs, keys, values
# --------------------------------------------------------------------------- #
def test_spec_rejects_unknown_feature_and_wrong_arity() -> None:
    with pytest.raises(ValueError, match="unknown feature"):
        FeatureSpec(name="not_a_feature")
    with pytest.raises(ValueError, match="takes 1 parameter"):
        FeatureSpec(name="wilder_rsi")
    with pytest.raises(ValueError, match="non-negative"):
        FeatureSpec(name="wilder_rsi", params=(-1,))
    assert RSI_14.key() == "wilder_rsi:14"
    assert TREND_EMAS.key() == "trend_emas"


def test_cache_rejects_invalid_bounds() -> None:
    with pytest.raises(ValueError):
        FeatureCache(max_entries=0)
    with pytest.raises(ValueError):
        FeatureCache(max_bytes=0)
    with pytest.raises(ValueError):
        FeatureCache(version="")


def test_cached_values_equal_direct_kernels() -> None:
    candles = _candles(40)
    frame = _frame(candles)
    cache = FeatureCache()

    assert cache.value(frame, RSI_14) == wilder_rsi(close_series(frame), 14)
    assert cache.value(frame, TREND_EMAS) == trend_emas(frame)
    assert cache.value(frame, SIGNIFICANT_SWINGS_10) == significant_swings(frame, 10)
    assert cache.value(frame, MACD_LINE) == macd_line(frame)
    assert cache.value(frame, MEAN_RANGE_14) == mean_range(frame, 14)
    assert cache.stats().computations == 5


def test_same_bar_repeat_is_a_hit_not_a_recompute() -> None:
    frame = _frame(_candles(30))
    cache = FeatureCache()
    first = cache.value(frame, RSI_14)
    second = cache.value(frame, RSI_14)
    assert first == second
    stats = cache.stats()
    assert stats.computations == 1
    assert stats.hits == 1
    assert stats.misses == 1


def test_incremental_extension_does_not_recompute_history() -> None:
    candles = _candles(40)
    appended = _candles(41)
    df = build_candle_dataframe(appended)
    frame = _frame(candles, df)
    cache = FeatureCache()

    before = cache.value(frame, RSI_14)
    assert cache.stats().computations == 1
    assert cache.stats().extensions == 0

    extended = extend_causal_frame(frame, appended[-1].end_ns, appended[-1], df)
    assert extended is not None
    after = cache.value(extended, RSI_14)

    stats = cache.stats()
    assert stats.extensions == 1
    assert stats.reseeds == 0
    assert stats.invalidations == 0
    # Exactly one new computation (the new bar), and the history is carried.
    assert stats.computations == 2
    assert after[:-1] == before
    assert after == wilder_rsi(close_series(extended), 14)


def test_mutating_an_input_bar_invalidates_only_that_instrument() -> None:
    candles_a = _candles(40, instrument="AAA-PERP.BINANCE")
    candles_b = _candles(40, instrument="BBB-PERP.BINANCE")
    frame_a = _frame(candles_a)
    frame_b = _frame(candles_b)
    cache = FeatureCache()

    original_a = cache.value(frame_a, RSI_14)
    cache.value(frame_b, RSI_14)
    assert cache.stats().computations == 2

    # Same bar count, one revised bar in the middle of A's prefix.
    mutated = list(candles_a)
    victim = mutated[17]
    shift = Decimal("0.5")
    mutated[17] = Candle(
        instrument_id=victim.instrument_id,
        start_ns=victim.start_ns,
        end_ns=victim.end_ns,
        open=victim.open + shift,
        high=victim.high + shift,
        low=victim.low + shift,
        close=victim.close + shift,
        volume=victim.volume,
        received_ns=victim.received_ns,
        available_ns=victim.available_ns,
        source_hash=victim.source_hash,
    )
    mutated_a = _frame(tuple(mutated))

    revised_a = cache.value(mutated_a, RSI_14)
    stats = cache.stats()
    assert revised_a != original_a
    assert stats.invalidations == 1  # A's stale entry, dropped by name
    assert stats.computations == 3  # exactly one recompute

    # B's key survives the revision of A: a repeat is a hit, not a recompute.
    assert cache.value(frame_b, RSI_14) is not None
    after_b = cache.stats()
    assert after_b.computations == 3
    assert after_b.hits == 1


def test_shorter_prefix_reseeds_and_invalidates() -> None:
    candles = _candles(30)
    longer = _frame(candles)
    shorter = _frame(candles[:20])
    cache = FeatureCache()
    cache.value(longer, TREND_EMAS)
    cache.value(shorter, TREND_EMAS)
    stats = cache.stats()
    assert stats.reseeds == 1
    assert stats.invalidations == 1
    assert stats.computations == 2


def test_cache_respects_max_entry_bound() -> None:
    frame = _frame(_candles(40))
    cache = FeatureCache(max_entries=2)
    for spec in (RSI_14, TREND_EMAS, MACD_LINE):
        cache.value(frame, spec)
    stats = cache.stats()
    assert stats.entries == 2
    assert stats.evictions == 1
    assert len(cache.keys()) == 2
    assert stats.max_entries == 2


def test_cache_respects_max_byte_bound() -> None:
    frame = _frame(_candles(40))
    cache = FeatureCache(max_entries=64, max_bytes=200)
    for spec in (RSI_14, TREND_EMAS, MACD_LINE, MEAN_RANGE_14):
        cache.value(frame, spec)
    stats = cache.stats()
    assert stats.estimated_bytes <= 200
    assert stats.evictions > 0


def test_default_bound_is_small_and_versioned() -> None:
    cache = FeatureCache()
    assert cache.stats().max_entries == DEFAULT_MAX_ENTRIES
    assert cache.stats().version == FEATURE_CACHE_VERSION
    keys = cache.keys()
    assert keys == ()
    key = CacheKey(
        version=FEATURE_CACHE_VERSION,
        instrument_id=INSTRUMENT,
        spec_key=RSI_14.key(),
        as_of_ns=0,
        input_digest=prefix_digest(build_candle_dataframe(_candles(3)), 3, INSTRUMENT),
    )
    assert key.version == cache.version


def test_explicit_invalidate_drops_entries_by_name() -> None:
    frame = _frame(_candles(30))
    cache = FeatureCache()
    cache.value(frame, RSI_14)
    cache.value(frame, TREND_EMAS)
    assert cache.invalidate(INSTRUMENT) == 2
    assert cache.stats().entries == 0
    assert cache.invalidate() == 0


def test_gap_frame_fails_closed_without_caching() -> None:
    candles = list(_candles(30))
    gap = Candle(
        instrument_id=candles[-1].instrument_id,
        start_ns=candles[-1].end_ns + 1,
        end_ns=candles[-1].end_ns + STEP_NS,
        open=Decimal("1"),
        high=Decimal("2"),
        low=Decimal("1"),
        close=Decimal("1.5"),
        volume=Decimal("1"),
        received_ns=candles[-1].end_ns + STEP_NS,
        available_ns=candles[-1].end_ns + STEP_NS,
        source_hash="gap",
    )
    candles.append(gap)
    frame = _frame(tuple(candles))
    assert not frame.continuous
    cache = FeatureCache()
    with pytest.raises(ValueError, match="source gap"):
        cache.value(frame, RSI_14)
    stats = cache.stats()
    assert stats.entries == 0
    assert stats.computations == 0


def test_input_digest_is_deterministic_and_content_addressed() -> None:
    candles = _candles(12)
    df = build_candle_dataframe(candles)
    first = prefix_digest(df, 12, INSTRUMENT)
    second = prefix_digest(build_candle_dataframe(candles), 12, INSTRUMENT)
    assert first == second
    assert prefix_digest(df, 11, INSTRUMENT) != first
    assert prefix_digest(df, 12, "OTHER") != first
    frame = _frame(candles, df)
    cache = FeatureCache()
    assert cache.digest_for(frame) == first


# --------------------------------------------------------------------------- #
# real tape: stance parity and proven append-only extension
# --------------------------------------------------------------------------- #
@pytest.mark.slow
def test_observe_all_28_stances_match_without_cache_on_real_tape() -> None:
    if not BTC_TAPE.exists():
        pytest.skip(f"real BTC tape absent at {BTC_TAPE}")
    from v8_next.evaluation.resource_baseline import replay_expert_decisions

    baseline = replay_expert_decisions(BTC_TAPE, use_cache=False)
    candidate = replay_expert_decisions(BTC_TAPE, use_cache=True)

    assert baseline.bars == candidate.bars >= 2000
    assert baseline.stances == candidate.stances == 28 * baseline.bars
    assert baseline.decision_digest() == candidate.decision_digest()

    stats = candidate.cache
    assert stats is not None
    assert stats.version == FEATURE_CACHE_VERSION
    assert stats.bars_observed == baseline.bars
    assert stats.reseeds == 0  # the tape only ever appends
    assert stats.extensions == baseline.bars - 1


@pytest.mark.slow
def test_observe_all_28_repeat_on_same_bar_is_identical() -> None:
    if not BTC_TAPE.exists():
        pytest.skip(f"real BTC tape absent at {BTC_TAPE}")
    from v8_next.evaluation.gate_resolution import load_tape_candles

    candles = tuple(load_tape_candles(BTC_TAPE, limit=140))
    if len(candles) < 120:
        pytest.skip("tape slice too short")
    df = build_candle_dataframe(candles)
    frame = CausalFrame.from_ordered_prefix(
        candles[-1].instrument_id, candles[-1].end_ns, candles, df
    )
    opportunity = opportunity_at(frame)
    cache = FeatureCache()
    cached = cache.observe_all_28(frame, opportunity)
    direct = observe_all_28(frame, opportunity)
    assert tuple((s.observer_id, s.kind, s.reason) for s in cached) == tuple(
        (s.observer_id, s.kind, s.reason) for s in direct
    )
    # A second pass must reuse the retained prefix rather than reseed it.
    cache.observe_all_28(frame, opportunity)
    stats = cache.stats()
    assert stats.reseeds == 0
    assert stats.extensions == 0
