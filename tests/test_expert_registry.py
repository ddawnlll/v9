"""Authoritative parity and behavioral tests for all 28 canonical active experts.

Matching v8-core/src/experts/mod.rs and witness_adapter.rs.
"""

from dataclasses import replace
from decimal import Decimal

import pytest

from v8_next.domain.market import Candle, CausalFrame
from v8_next.domain.positioning import PositioningReading
from v8_next.economics.decisions import Opportunity, StanceKind
from v8_next.experts.levels import HOUR_NS
from v8_next.experts.registry import (
    CANONICAL_28_EXPERTS,
    EXPERT_REGISTRY,
    REQUIRES_TABLE,
    VARIANT_TABLE,
    get_expert,
    observe_all_28,
    observe_expert,
    registry_rows,
    validate_variant_overrides,
    verify_expert_implementation,
)


def sample_frame(length: int = 120, continuous: bool = True) -> CausalFrame:
    candles = tuple(
        Candle(
            "instrument",
            i * 3600 * 10**9,
            (i + 1) * 3600 * 10**9,
            Decimal("100") + Decimal(i % 5),
            Decimal("105") + Decimal(i % 5),
            Decimal("95") + Decimal(i % 5),
            Decimal("101") + Decimal(i % 5),
            Decimal("1000") + Decimal(i * 10),
            (i + 1) * 3600 * 10**9,
            (i + 1) * 3600 * 10**9,
            "test-only",
        )
        for i in range(length)
    )
    decision_ns = length * 3600 * 10**9
    if not continuous and length > 2:
        candles = candles[:1] + candles[2:]
    return CausalFrame("instrument", decision_ns, candles)


def flat_market_frame(length: int = 120) -> CausalFrame:
    candles = tuple(
        Candle(
            "instrument",
            i * 3600 * 10**9,
            (i + 1) * 3600 * 10**9,
            Decimal("100"),
            Decimal("100.5"),
            Decimal("99.5"),
            Decimal("100"),
            Decimal("1000"),
            (i + 1) * 3600 * 10**9,
            (i + 1) * 3600 * 10**9,
            "test-flat",
        )
        for i in range(length)
    )
    decision_ns = length * 3600 * 10**9
    return CausalFrame("instrument", decision_ns, candles)


def sample_opportunity(
    direction: str = "LONG", decision_ns: int = 120 * 3600 * 10**9
) -> Opportunity:
    return Opportunity(
        opportunity_id="test-opp-1",
        exposure_id="test-exp-1",
        instrument_id="instrument",
        direction=direction,
        anchor_ns=decision_ns - 3600 * 10**9,
        expires_ns=decision_ns + 3600 * 10**9,
    )


def sample_reading(metric: str, value: Decimal, decision_ns: int) -> PositioningReading:
    return PositioningReading(
        instrument_id="instrument",
        metric=metric,  # type: ignore[arg-type]
        value=value,
        event_ns=decision_ns - 1000,
        received_ns=decision_ns - 500,
        available_ns=decision_ns - 100,
        valid_until_ns=decision_ns + 100_000_000_000,
        source_hash="test-source",
    )


def test_canonical_28_experts_count_and_uniqueness():
    assert len(CANONICAL_28_EXPERTS) == 28
    assert len(set(CANONICAL_28_EXPERTS)) == 28
    for expert_id in CANONICAL_28_EXPERTS:
        assert expert_id in EXPERT_REGISTRY
        spec = get_expert(expert_id)
        assert spec.expert_id == expert_id
        assert spec.version == "v1"
        assert spec.ported is True


def test_dynamic_port_verification_and_registry_rows():
    for expert_id in CANONICAL_28_EXPERTS:
        assert verify_expert_implementation(expert_id) is True
    rows = registry_rows()
    assert len(rows) == 28
    for expert_id, ported in rows:
        assert expert_id in CANONICAL_28_EXPERTS
        assert ported is True


def test_requires_table_covers_all_28():
    assert len(REQUIRES_TABLE) == 28
    for expert_id in CANONICAL_28_EXPERTS:
        assert expert_id in REQUIRES_TABLE
        assert len(REQUIRES_TABLE[expert_id]) > 0


def test_variant_table_matches_rust_mod_rs():
    # squeeze_swing is present in Rust VARIANT_TABLE (mod.rs:86)
    assert "squeeze_swing" in VARIANT_TABLE
    # divergence_12_setups is not in Rust VARIANT_TABLE (mod.rs:59)
    assert "divergence_12_setups" not in VARIANT_TABLE

    for expert_id, variants in VARIANT_TABLE.items():
        if expert_id != "squeeze_swing":
            assert expert_id in CANONICAL_28_EXPERTS
            spec = get_expert(expert_id)
            assert spec.supported_variants == variants


def test_validate_variant_overrides_semantics():
    valid = {
        "bollinger_breakout": "b",
        "candlestick_reversal": "three_black_crows",
        "gap_exhaustion": "c",
        "squeeze_swing": "m1",
    }
    validate_variant_overrides(valid)

    with pytest.raises(ValueError, match="unsupported variant"):
        validate_variant_overrides({"candlestick_reversal": "not_a_candle"})

    with pytest.raises(ValueError, match="has no explicit variant dispatch"):
        validate_variant_overrides({"volume_confirmed_breakout": "a"})

    with pytest.raises(ValueError, match="has no explicit variant dispatch"):
        validate_variant_overrides({"divergence_12_setups": "a"})


def test_get_expert_fails_closed_on_unknown():
    with pytest.raises(KeyError, match="unregistered expert"):
        get_expert("unknown_expert_id")


@pytest.mark.parametrize("expert_id", CANONICAL_28_EXPERTS)
def test_all_28_experts_fail_closed_on_source_gap(expert_id):
    frame = sample_frame(length=120, continuous=False)
    opp = sample_opportunity(decision_ns=frame.decision_ns)
    stance = observe_expert(expert_id, frame, opp)
    assert stance.kind == StanceKind.ABSTAIN
    assert stance.reason == "SOURCE_GAP"


@pytest.mark.parametrize("expert_id", CANONICAL_28_EXPERTS)
def test_all_28_experts_fail_closed_on_insufficient_warmup(expert_id):
    spec = get_expert(expert_id)
    if spec.warmup > 1:
        frame = sample_frame(length=spec.warmup - 1, continuous=True)
        opp = sample_opportunity(decision_ns=frame.decision_ns)
        stance = observe_expert(expert_id, frame, opp)
        assert stance.kind == StanceKind.ABSTAIN
        assert stance.reason in {"WARMUP", "NO_OPPORTUNITY", "NO_PRIOR_BREAKOUT"}


@pytest.mark.parametrize("expert_id", CANONICAL_28_EXPERTS)
def test_all_28_experts_fail_closed_on_no_opportunity(expert_id):
    frame = sample_frame(length=120, continuous=True)
    stance = observe_expert(expert_id, frame, None)
    assert stance.kind == StanceKind.ABSTAIN
    assert stance.opportunity_id is None


def test_observe_expert_authoritatively_applies_registry_metadata():
    """Verify observe_expert acts as LegacyExpertWitnessAdapter, stamping registry metadata."""
    frame = sample_frame(length=120, continuous=True)
    opp = sample_opportunity(decision_ns=frame.decision_ns)
    for expert_id in CANONICAL_28_EXPERTS:
        spec = get_expert(expert_id)
        stance = observe_expert(expert_id, frame, opp)
        assert stance.observer_id == spec.expert_id
        assert stance.behavior_family == spec.behavior_family
        assert stance.mechanism_family == spec.mechanism_family
        assert stance.dependency_group == spec.dependency_group
        assert stance.version == f"{spec.expert_id}-witness-v1"


def test_observe_all_28_ensemble():
    frame = sample_frame(length=120, continuous=True)
    opp = sample_opportunity(decision_ns=frame.decision_ns)
    readings = (
        sample_reading("settled_funding_rate", Decimal("0.0015"), frame.decision_ns),
        sample_reading("open_interest", Decimal("50000"), frame.decision_ns),
        sample_reading("long_short_ratio", Decimal("1.2"), frame.decision_ns),
    )
    stances = observe_all_28(frame, opp, readings=readings)
    assert len(stances) == 28
    observed_ids = {s.observer_id for s in stances}
    assert observed_ids == set(CANONICAL_28_EXPERTS)


# ==============================================================================
# Comprehensive 28/28 Real Signal Parity Fixtures (SUPPORT / CONTRADICT / ABSTAIN)
# ==============================================================================


def make_candle(
    i: int,
    open_: float,
    high: float,
    low: float,
    close: float,
    volume: float = 10.0,
    time_unit: int = 3600 * 10**9,
) -> Candle:
    return Candle(
        "instrument",
        i * time_unit,
        (i + 1) * time_unit,
        Decimal(str(open_)),
        Decimal(str(high)),
        Decimal(str(low)),
        Decimal(str(close)),
        Decimal(str(volume)),
        (i + 1) * time_unit,
        (i + 1) * time_unit,
        "parity-fixture",
    )


def generate_setup_for_expert(
    expert_id: str,
) -> tuple[CausalFrame, str, str | None, tuple[PositioningReading, ...]]:
    """Build concrete real market data that triggers the given expert."""
    time_unit = 3600 * 10**9

    if expert_id == "bollinger_breakout":
        # 20 flat bars around 100, 21st bar expands upward
        bars = [make_candle(i, 100, 101, 99, 100) for i in range(20)]
        bars.append(make_candle(20, 101, 115, 101, 114))
        frame = CausalFrame("instrument", 21 * time_unit, tuple(bars))
        return frame, "LONG", "a", ()

    if expert_id == "bollinger_reversion":
        # 19 bars alternating, 20th bar touches 103 (between 2-sigma and 3-sigma)
        values = [100 + (-1) ** i for i in range(19)] + [103]
        bars = [make_candle(i, v, v + 0.1, v - 0.1, v) for i, v in enumerate(values)]
        frame = CausalFrame("instrument", len(bars) * time_unit, tuple(bars))
        return frame, "SHORT", None, ()

    if expert_id == "breakout_retest":
        # 45 bars; bar 15 forms swing high at 110, bar -2 breaches at 112, bar -1 retests at 111 (low=110)
        bars = [make_candle(i, 100, 101, 99, 100) for i in range(45)]
        bars[15] = replace(bars[15], high=Decimal("110"))
        bars[-2] = replace(
            bars[-2],
            open=Decimal("112"),
            close=Decimal("112"),
            high=Decimal("113"),
            low=Decimal("111"),
        )
        bars[-1] = replace(
            bars[-1],
            open=Decimal("111"),
            close=Decimal("111"),
            high=Decimal("112"),
            low=Decimal("110"),
        )
        frame = CausalFrame("instrument", len(bars) * time_unit, tuple(bars))
        return frame, "LONG", "a", ()

    if expert_id == "candlestick_reversal":
        # Candle 0 down, Candle 1 classic hammer
        c0 = make_candle(0, 100, 101, 89, 90)
        c1 = make_candle(1, 90, 93, 84, 92)  # body=2, lower=6 >= 2*body, upper=1 <= body
        frame = CausalFrame("instrument", 2 * time_unit, (c0, c1))
        return frame, "LONG", "hammer", ()

    if expert_id == "divergence_12_setups":
        # Higher highs in price with lower RSI
        closes = list(range(100, 121)) + list(range(119, 99, -1))
        bars = []
        for i, close in enumerate(closes):
            high, low = close + 1, close - 1
            if i == 20:
                high = 130
            if i == 32:
                high = 140
            bars.append(make_candle(i, close, high, low, close))
        frame = CausalFrame("instrument", len(bars) * time_unit, tuple(bars))
        return frame, "SHORT", None, ()

    if expert_id == "donchian_breakout":
        bars = [make_candle(i, 100, 101, 99, 100) for i in range(20)]
        bars.append(make_candle(20, 101, 106, 101, 105))
        frame = CausalFrame("instrument", 21 * time_unit, tuple(bars))
        return frame, "LONG", None, ()

    if expert_id == "failed_breakout":
        # 20 flat, breakout to 103, then close back to 100 within 5 bars
        closes = [100] * 20 + [103, 100]
        bars = [make_candle(i, c, c + 1, c - 1, c) for i, c in enumerate(closes)]
        frame = CausalFrame("instrument", len(bars) * time_unit, tuple(bars))
        return frame, "SHORT", None, ()

    if expert_id == "failed_breakout_2b":
        # Variant e: current open < previous low < current close
        bars = [make_candle(i, 100, 101, 99, 100) for i in range(5)]
        bars[-2] = make_candle(3, 100, 101, 99, 100)
        bars[-1] = make_candle(4, 98, 101, 97, 100)  # open 98 < prev low 99 < close 100
        frame = CausalFrame("instrument", len(bars) * time_unit, tuple(bars))
        return frame, "LONG", "e", ()

    if expert_id == "fib_projection_reversal":
        bars = [make_candle(i, 100, 101, 99, 100) for i in range(37)]
        bars[10] = replace(bars[10], low=Decimal("70"))
        bars[25] = replace(bars[25], high=Decimal("150"))
        bars[-1] = replace(
            bars[-1],
            open=Decimal("190"),
            close=Decimal("190"),
            high=Decimal("200"),
            low=Decimal("189"),
        )
        frame = CausalFrame("instrument", 37 * time_unit, tuple(bars))
        return frame, "SHORT", None, ()

    if expert_id == "fib_retracement_continuation":
        bars = [make_candle(i, 100, 101, 99, 100) for i in range(37)]
        bars[10] = replace(bars[10], low=Decimal("70"))
        bars[25] = replace(bars[25], high=Decimal("150"))
        bars[-1] = replace(
            bars[-1],
            open=Decimal("125"),
            close=Decimal("125"),
            high=Decimal("126"),
            low=Decimal("119"),
        )
        frame = CausalFrame("instrument", 37 * time_unit, tuple(bars))
        return frame, "LONG", None, ()

    if expert_id == "fib_rsi_bb_confluence":
        prices = [100] * 40 + [96, 104] * 9 + [96, 90]
        bars = [make_candle(i, p, p + 1, p - 1, p) for i, p in enumerate(prices)]
        bars[10] = replace(bars[10], low=Decimal("70"))
        bars[25] = replace(bars[25], high=Decimal("150"))
        bars[-1] = replace(bars[-1], low=Decimal("87"), close=Decimal("90"))
        frame = CausalFrame("instrument", len(bars) * time_unit, tuple(bars))
        return frame, "LONG", "b", ()

    if expert_id == "floor_trader_pivot":
        # 26 hourly bars; bars 0..23 complete previous day, bar 25 drift between pivot and resistance1
        bars = [make_candle(i, 100, 101, 99, 100, time_unit=HOUR_NS) for i in range(26)]
        bars[23] = replace(bars[23], close=Decimal("101"))
        bars[24] = replace(bars[24], open=Decimal("100.5"), close=Decimal("101"))
        bars[25] = replace(
            bars[25], high=Decimal("120"), open=Decimal("100.5"), close=Decimal("101")
        )
        frame = CausalFrame("instrument", 26 * HOUR_NS, tuple(bars))
        return frame, "LONG", None, ()

    if expert_id == "funding_crowding_reversal":
        bars = [make_candle(i, 100, 101, 99, 100) for i in range(100)]
        bars[-1] = make_candle(99, 98, 99, 97, 98)
        frame = CausalFrame("instrument", 100 * time_unit, tuple(bars))
        readings: tuple[PositioningReading, ...] = (
            sample_reading("settled_funding_rate", Decimal("0.001"), 100 * time_unit),
        )
        return frame, "SHORT", "a", readings

    if expert_id == "gap_exhaustion":
        # 18 bars at 100, then 3 up gaps (110, 120, 130), bar -1 close < open
        bars = [make_candle(i, 100, 101, 99, 100) for i in range(18)]
        bars.append(make_candle(18, 110, 111, 109, 110))
        bars.append(make_candle(19, 120, 121, 119, 120))
        bars.append(make_candle(20, 130, 131, 128, 129))
        frame = CausalFrame("instrument", 21 * time_unit, tuple(bars))
        return frame, "SHORT", "a", ()

    if expert_id == "ichimoku_cloud":
        values = [110] * 10 + [100] * 16 + [125]
        bars = [make_candle(i, v, v + 1, v - 1, v) for i, v in enumerate(values)]
        bars[12] = replace(bars[12], low=Decimal("90"))
        frame = CausalFrame("instrument", len(bars) * time_unit, tuple(bars))
        return frame, "LONG", None, ()

    if expert_id == "liquidity_sweep_reclaim":
        bars = [make_candle(i, 100, 101, 99, 100) for i in range(21)]
        # Last bar sweeps below 99 (low=98) and reclaims above (close=100)
        bars[-1] = make_candle(20, 99.5, 102, 98, 100)
        frame = CausalFrame("instrument", 21 * time_unit, tuple(bars))
        return frame, "LONG", None, ()

    if expert_id == "macd_stoch_trend":
        prices = list(range(100, 140)) + [137, 135, 140]
        bars = [make_candle(i, p - 1, p, p - 2, p) for i, p in enumerate(prices)]
        frame = CausalFrame("instrument", len(bars) * time_unit, tuple(bars))
        return frame, "LONG", None, ()

    if expert_id == "market_profile_value_area":
        prior = [
            make_candle(0, 90, 100, 90, 100, time_unit=HOUR_NS),
            make_candle(1, 100, 110, 100, 100, time_unit=HOUR_NS),
            *(
                make_candle(i, 100, 101, 100, 100.5, time_unit=HOUR_NS)
                for i in range(2, 24)
            ),
        ]
        # Current bar close 99 < POC (100)
        prior.append(make_candle(24, 99, 99.5, 98.5, 99, time_unit=HOUR_NS))
        frame = CausalFrame("instrument", 25 * HOUR_NS, tuple(prior))
        return frame, "LONG", "a", ()

    if expert_id == "obv_adl_regime":
        prices = list(range(100, 130))
        bars = [make_candle(i, p - 1, p, p - 2, p, volume=10.0) for i, p in enumerate(prices)]
        frame = CausalFrame("instrument", len(bars) * time_unit, tuple(bars))
        return frame, "LONG", None, ()

    if expert_id == "open_interest_divergence":
        bars = [make_candle(i, 100, 101, 99, 100, volume=10.0) for i in range(100)]
        bars[-1] = make_candle(99, 102, 103, 101, 102, volume=20.0)
        frame = CausalFrame("instrument", 100 * time_unit, tuple(bars))
        readings = (
            sample_reading("open_interest", Decimal("1000"), 100 * time_unit),
            sample_reading("long_short_ratio", Decimal("1.2"), 100 * time_unit),
        )
        return frame, "LONG", "a", readings

    if expert_id == "pandf_breakout":
        prices = [100] * 20 + [104, 100, 105]
        bars = [make_candle(i, p, p + 0.5, p - 0.5, p, volume=1.0) for i, p in enumerate(prices)]
        frame = CausalFrame("instrument", len(bars) * time_unit, tuple(bars))
        return frame, "LONG", "a", ()

    if expert_id == "pattern_measuring_objective":
        bars = [make_candle(i, 100, 100.2, 99.8, 100) for i in range(34)]
        bars[5] = replace(bars[5], high=Decimal("120"))
        bars[10] = replace(bars[10], low=Decimal("90"))
        bars[15] = replace(bars[15], high=Decimal("140"))  # Head
        bars[20] = replace(bars[20], low=Decimal("95"))
        bars[25] = replace(bars[25], high=Decimal("120"))
        bars[-1] = replace(
            bars[-1],
            open=Decimal("90"),
            close=Decimal("90"),
            low=Decimal("89"),
            high=Decimal("91"),
        )
        frame = CausalFrame("instrument", 34 * time_unit, tuple(bars))
        return frame, "SHORT", "head_shoulders", ()

    if expert_id == "range_breakout_1to1":
        bars = [
            make_candle(i, 100, 100.5, 99.5, 100, volume=10.0) for i in range(100)
        ]
        bars[-1] = make_candle(99, 102, 102.2, 101, 102, volume=20.0)
        frame = CausalFrame("instrument", 100 * time_unit, tuple(bars))
        return frame, "LONG", None, ()

    if expert_id == "rsi_stoch_reversion":
        prices = list(range(120, 99, -1)) + [105, 107, 110]
        bars = [make_candle(i, p, p + 0.1, p - 0.1, p) for i, p in enumerate(prices)]
        frame = CausalFrame("instrument", len(bars) * time_unit, tuple(bars))
        return frame, "LONG", None, ()

    if expert_id == "trend_pullback":
        prices = list(range(100, 140)) + [128]
        bars = [make_candle(i, p, p + 1, p - 1, p) for i, p in enumerate(prices)]
        frame = CausalFrame("instrument", len(bars) * time_unit, tuple(bars))
        return frame, "LONG", None, ()

    if expert_id == "trend_pullback_depth":
        prices = [100] * 25 + list(range(119, 131))
        bars = [make_candle(i, p, p + 1, p - 1, p) for i, p in enumerate(prices)]
        bars[10] = replace(bars[10], low=Decimal("70"))
        bars[25] = replace(bars[25], high=Decimal("150"))
        frame = CausalFrame("instrument", len(bars) * time_unit, tuple(bars))
        return frame, "LONG", None, ()

    if expert_id == "volume_climax_reversal":
        bars = [make_candle(i, 100 + i, 102 + i, 99 + i, 101 + i, volume=10.0) for i in range(100)]
        bars[-1] = replace(bars[-1], volume=Decimal("100"))  # Spike z >= 3
        frame = CausalFrame("instrument", 100 * time_unit, tuple(bars))
        return frame, "SHORT", None, ()

    if expert_id == "volume_confirmed_breakout":
        bars = [make_candle(i, 100, 101, 99, 100, volume=10.0) for i in range(20)]
        bars.append(make_candle(20, 103, 104, 102, 103, volume=20.0))
        frame = CausalFrame("instrument", 21 * time_unit, tuple(bars))
        return frame, "LONG", None, ()

    raise NotImplementedError(f"no fixture for {expert_id}")


@pytest.mark.parametrize("expert_id", CANONICAL_28_EXPERTS)
def test_all_28_experts_produce_support_and_contradict(expert_id):
    """Rigorous 28/28 verification: Real frame signals generate SUPPORT, CONTRADICT and ABSTAIN."""
    frame, expected_dir, variant, readings = generate_setup_for_expert(expert_id)
    opp_aligned = sample_opportunity(direction=expected_dir, decision_ns=frame.decision_ns)
    opp_opposed = sample_opportunity(
        direction="SHORT" if expected_dir == "LONG" else "LONG",
        decision_ns=frame.decision_ns,
    )

    # 1. Aligned Opportunity -> SUPPORT
    stance_support = observe_expert(
        expert_id, frame, opp_aligned, variant=variant, readings=readings
    )
    assert stance_support.kind == StanceKind.SUPPORT, (
        f"{expert_id} failed to emit SUPPORT on aligned opportunity: reason={stance_support.reason}"
    )

    # 2. Opposing Opportunity -> CONTRADICT
    stance_contradict = observe_expert(
        expert_id, frame, opp_opposed, variant=variant, readings=readings
    )
    assert stance_contradict.kind == StanceKind.CONTRADICT, (
        f"{expert_id} failed to emit CONTRADICT on opposed opportunity: reason={stance_contradict.reason}"
    )

    # 3. Flat / non-triggering frame -> ABSTAIN
    flat_frame = flat_market_frame(length=120)
    flat_opp = sample_opportunity(direction=expected_dir, decision_ns=flat_frame.decision_ns)
    stance_abstain = observe_expert(expert_id, flat_frame, flat_opp)
    assert stance_abstain.kind == StanceKind.ABSTAIN, (
        f"{expert_id} should ABSTAIN on flat frame, got {stance_abstain.kind}"
    )

    # 4. Authoritative metadata must match spec
    spec = get_expert(expert_id)
    assert stance_support.observer_id == spec.expert_id
    assert stance_support.behavior_family == spec.behavior_family
    assert stance_support.mechanism_family == spec.mechanism_family
    assert stance_support.dependency_group == spec.dependency_group
    assert stance_support.version == f"{spec.expert_id}-witness-v1"


def test_registry_lookup_by_alias_and_canonical():
    """Verify registry can be queried by either canonical snake_case or hyphenated alias."""
    for canonical in CANONICAL_28_EXPERTS:
        spec_canonical = get_expert(canonical)
        spec_alias = get_expert(spec_canonical.alias)
        assert spec_canonical is spec_alias
