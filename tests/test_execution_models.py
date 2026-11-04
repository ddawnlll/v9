"""MECHANICS ONLY — execution model + telemetry contracts. No tape, no claims.

These tests exercise arithmetic and configuration invariants only. They assert
nothing about economic performance and carry zero evaluative weight. Evaluative
execution evidence is tested on the real tape in
``test_execution_integration.py``.
"""

from __future__ import annotations

import pytest

from v8_next.adapters.execution_models import (
    DEFAULT_PROFILE,
    FILL_MODEL_IS_SLIPPED,
    FILL_MODEL_REGISTRY,
    PROFILES,
    ExecutionProfile,
    build_fee_model,
    build_fill_model,
    build_latency_model,
    profile_digest,
    profile_summary,
    resolve_profile,
    venue_kwargs,
)
from v8_next.adapters.portfolio_backtest import (
    _as_float,
    _execution_telemetry,
    _fill_signature,
    _json_safe,
    _money_amounts,
)

# ---------------------------------------------------------------- registry


def test_every_declared_profile_builds_a_full_venue_kwarg_set() -> None:
    for name in PROFILES:
        kw = venue_kwargs(name)
        assert set(kw) == {
            "fill_model",
            "fee_model",
            "latency_model",
            "margin_model",
            "bar_execution",
            "bar_adaptive_high_low_ordering",
            "trade_execution",
            "liquidity_consumption",
            "queue_position",
            "use_market_order_acks",
            "price_protection_points",
        }
        expected = FILL_MODEL_REGISTRY[PROFILES[name].fill_model]
        assert type(kw["fill_model"]) is expected
        assert kw["fee_model"] is not None
        assert kw["bar_execution"] is True
        assert kw["trade_execution"] is True


def test_default_profile_is_the_documented_one() -> None:
    assert DEFAULT_PROFILE in PROFILES


def test_slippage_declaration_matches_the_registry_not_a_runtime_probe() -> None:
    # The concrete fill models do not expose is_slipped() at runtime, so the
    # declaration must be explicit and complete for every registered model.
    assert set(FILL_MODEL_IS_SLIPPED) == set(FILL_MODEL_REGISTRY)
    assert FILL_MODEL_IS_SLIPPED["default"] is False
    assert FILL_MODEL_IS_SLIPPED["one_tick_slippage"] is True


def test_latency_model_is_absent_exactly_when_all_latencies_are_zero() -> None:
    zero = ExecutionProfile(
        name="zero",
        fill_model="default",
        prob_fill_on_limit=1.0,
        prob_slippage=0.0,
        random_seed=0,
    )
    assert build_latency_model(zero) is None
    profiled = ExecutionProfile(
        name="lat",
        fill_model="default",
        prob_fill_on_limit=1.0,
        prob_slippage=0.0,
        random_seed=0,
        base_latency_nanos=1,
    )
    assert build_latency_model(profiled) is not None


# ---------------------------------------------------------------- determinism


def test_profile_digest_is_stable_across_calls_and_distinct_across_profiles() -> None:
    for name in PROFILES:
        assert profile_digest(name) == profile_digest(name)
        assert len(profile_digest(name)) == 64
    digests = {profile_digest(n) for n in PROFILES}
    assert len(digests) == len(PROFILES), "profiles must not collide"


def test_digest_changes_when_any_execution_parameter_changes() -> None:
    base = ExecutionProfile(
        name="x", fill_model="default", prob_fill_on_limit=1.0,
        prob_slippage=0.0, random_seed=0,
    )
    changed_seed = ExecutionProfile(
        name="x", fill_model="default", prob_fill_on_limit=1.0,
        prob_slippage=0.0, random_seed=1,
    )
    changed_fill = ExecutionProfile(
        name="x", fill_model="one_tick_slippage", prob_fill_on_limit=1.0,
        prob_slippage=0.0, random_seed=0,
    )
    assert profile_digest(base) != profile_digest(changed_seed)
    assert profile_digest(base) != profile_digest(changed_fill)


def test_fill_model_rebuild_is_reproducible_for_the_same_seed() -> None:
    p = PROFILES["realistic"]
    assert type(build_fill_model(p)) is type(build_fill_model(p))
    assert type(build_fee_model(p)) is type(build_fee_model(p))


def test_summary_never_claims_venue_truth() -> None:
    for name in PROFILES:
        s = profile_summary(name)
        assert s["evidence_class"] == "MODELLED_EXECUTION_ASSUMPTION_NOT_VENUE_TRUTH"
        assert s["digest"] == profile_digest(name)
        assert s["profile"] == name


def test_depth_dependent_knobs_are_reported_as_inert_without_depth_data() -> None:
    """A knob that does nothing must not be presented as if it does."""
    from v8_next.adapters.execution_models import DEPTH_DEPENDENT_KNOBS

    assert set(DEPTH_DEPENDENT_KNOBS) == {"liquidity_consumption", "queue_position"}

    # realistic enables both, and declares no depth data
    summary = profile_summary("realistic")
    assert summary["depth_data_available"] is False
    assert summary["inert_knobs_without_depth_data"] == summary["depth_dependent_knobs_enabled"]
    assert set(summary["depth_dependent_knobs_enabled"]) == set(DEPTH_DEPENDENT_KNOBS)

    # declaring depth data clears the inert flag without changing the knobs
    with_depth = ExecutionProfile(
        name="depth",
        fill_model="default",
        prob_fill_on_limit=1.0,
        prob_slippage=0.0,
        random_seed=0,
        liquidity_consumption=True,
        queue_position=True,
        depth_data_available=True,
    )
    s = profile_summary(with_depth)
    assert s["inert_knobs_without_depth_data"] == []
    assert set(s["depth_dependent_knobs_enabled"]) == set(DEPTH_DEPENDENT_KNOBS)

    # baseline enables no depth-dependent knob at all
    assert profile_summary("baseline")["depth_dependent_knobs_enabled"] == []


# ---------------------------------------------------------------- validation


def test_profile_rejects_unknown_fill_model() -> None:
    with pytest.raises(ValueError, match="unknown fill_model"):
        ExecutionProfile(
            name="bad", fill_model="does_not_exist",
            prob_fill_on_limit=1.0, prob_slippage=0.0, random_seed=0,
        )


def test_profile_rejects_out_of_range_probabilities_and_negative_seed() -> None:
    with pytest.raises(ValueError, match="prob_fill_on_limit"):
        ExecutionProfile(name="b", fill_model="default", prob_fill_on_limit=1.5,
                         prob_slippage=0.0, random_seed=0)
    with pytest.raises(ValueError, match="prob_slippage"):
        ExecutionProfile(name="b", fill_model="default", prob_fill_on_limit=1.0,
                         prob_slippage=-0.1, random_seed=0)
    with pytest.raises(ValueError, match="random_seed"):
        ExecutionProfile(name="b", fill_model="default", prob_fill_on_limit=1.0,
                         prob_slippage=0.0, random_seed=-1)


def test_profile_rejects_negative_latency() -> None:
    with pytest.raises(ValueError, match="base_latency_nanos"):
        ExecutionProfile(name="b", fill_model="default", prob_fill_on_limit=1.0,
                         prob_slippage=0.0, random_seed=0, base_latency_nanos=-1)


def test_resolve_profile_accepts_name_or_object_and_rejects_unknown_name() -> None:
    assert resolve_profile("baseline").name == "baseline"
    assert resolve_profile(PROFILES["realistic"]) is PROFILES["realistic"]
    with pytest.raises(ValueError, match="unknown execution profile"):
        resolve_profile("nope")


# ---------------------------------------------------------------- telemetry


def _profile() -> ExecutionProfile:
    return PROFILES["baseline"]


LEG = "BTCUSDT-PERP.BINANCE"


def test_shortfall_is_positive_when_a_buy_pays_above_the_decision_price() -> None:
    block = _execution_telemetry(
        _profile(),
        [],
        "list",
        [{"instrument_id": LEG, "side": "BUY", "avg_px_open": "101.0", "event_ns": 200}],
        [{"instrument_id": LEG, "decision_ns": 100, "close": "100.0"}],
    )
    assert block["slippage_samples"] == 1
    assert block["slippage_bps_mean"] == pytest.approx(100.0)
    assert block["decision_to_position_event_ns_mean"] == 100


def test_shortfall_is_positive_when_a_sell_fills_below_the_decision_price() -> None:
    block = _execution_telemetry(
        _profile(),
        [],
        "list",
        [{"instrument_id": LEG, "side": "SELL", "avg_px_open": "99.0", "event_ns": 200}],
        [{"instrument_id": LEG, "decision_ns": 100, "close": "100.0"}],
    )
    assert block["slippage_bps_mean"] == pytest.approx(100.0)


def test_shortfall_is_negative_when_the_fill_is_favourable() -> None:
    block = _execution_telemetry(
        _profile(),
        [],
        "list",
        [{"instrument_id": LEG, "side": "BUY", "avg_px_open": "99.0", "event_ns": 200}],
        [{"instrument_id": LEG, "decision_ns": 100, "close": "100.0"}],
    )
    assert block["slippage_bps_mean"] == pytest.approx(-100.0)


@pytest.mark.parametrize(
    ("side", "fill_px", "expected"),
    [
        # Nautilus position events report LONG/SHORT, not BUY/SELL.
        ("LONG", "101.0", 100.0),
        ("SHORT", "99.0", 100.0),
        ("LONG", "99.0", -100.0),
        ("SHORT", "101.0", -100.0),
    ],
)
def test_shortfall_understands_position_side_names(
    side: str, fill_px: str, expected: float
) -> None:
    block = _execution_telemetry(
        _profile(),
        [],
        "list",
        [{"instrument_id": LEG, "side": side, "avg_px_open": fill_px, "event_ns": 200}],
        [{"instrument_id": LEG, "decision_ns": 100, "close": "100.0"}],
    )
    assert block["slippage_samples"] == 1
    assert block["slippage_bps_mean"] == pytest.approx(expected)


def test_telemetry_stays_none_rather_than_inventing_numbers() -> None:
    block = _execution_telemetry(
        _profile(), [], "NONE",
        [{"instrument_id": LEG, "side": "BUY", "avg_px_open": "not-a-number", "event_ns": 200}],
        [{"instrument_id": LEG, "decision_ns": 100, "close": "100.0"}],
    )
    assert block["slippage_bps_mean"] is None
    assert block["slippage_bps_max"] is None
    assert block["commission_total"] is None
    assert block["fills_count"] == 0


def test_telemetry_ignores_positions_with_no_prior_decision() -> None:
    block = _execution_telemetry(
        _profile(), [], "list",
        [{"instrument_id": LEG, "side": "BUY", "avg_px_open": "101.0", "event_ns": 50}],
        [{"instrument_id": LEG, "decision_ns": 100, "close": "100.0"}],
    )
    assert block["slippage_samples"] == 0
    assert block["decision_to_position_event_ns_mean"] is None


def test_commission_is_summed_from_real_fill_record_format() -> None:
    """Regression: the engine emits commissions as ["0.49407624 USDT"], not Money()."""
    block = _execution_telemetry(
        _profile(),
        [
            {"commissions": ["0.49407624 USDT"]},
            {"commissions": ["0.00592376 USDT"]},
            {"not_commission": "x"},
        ],
        "DataFrame", [], [],
    )
    assert block["fills_count"] == 3
    assert block["commission_totals_by_currency"] == {"USDT": pytest.approx(0.5, abs=1e-8)}
    assert block["commission_total"] == pytest.approx(0.5, abs=1e-8)


def test_commission_currencies_are_never_summed_together() -> None:
    block = _execution_telemetry(
        _profile(),
        [{"commissions": ["0.5 USDT", "1.0 USDC"]}],
        "DataFrame", [], [],
    )
    assert block["commission_totals_by_currency"] == {
        "USDT": pytest.approx(0.5),
        "USDC": pytest.approx(1.0),
    }
    # two currencies present -> no single total is invented
    assert block["commission_total"] is None


def test_commission_parsing_accepts_money_repr_and_plain_numbers() -> None:
    assert _money_amounts("Money(0.5, USDT)") == {"USDT": pytest.approx(0.5)}
    assert _money_amounts(0.25) == {"": pytest.approx(0.25)}
    assert _money_amounts(None) == {}
    assert _money_amounts("no amount here") == {}


def test_json_safe_makes_report_values_serialisable() -> None:
    assert _json_safe(float("nan")) is None
    assert _json_safe(float("inf")) is None
    assert _json_safe(1.5) == 1.5
    assert _json_safe(None) is None
    assert _json_safe("x") == "x"
    assert _json_safe(["0.5 USDT"]) == ["0.5 USDT"]


def test_latency_is_reported_as_unobservable_under_bar_execution() -> None:
    """A configured latency of 1ms must not be published as measured 0 latency."""
    profile = PROFILES["realistic"]
    assert profile.base_latency_nanos > 0
    block = _execution_telemetry(
        profile, [], "DataFrame",
        [{"instrument_id": "BTCUSDT-PERP.BINANCE", "side": "BUY",
          "avg_px_open": "101.0", "event_ns": 200}],
        [{"instrument_id": "BTCUSDT-PERP.BINANCE", "decision_ns": 200, "close": "100.0"}],
    )
    assert block["configured_latency_nanos"] > 0
    assert block["decision_to_position_event_ns_mean"] == 0
    assert block["latency_observability"] == "BAR_EXECUTION_STAMPS_FILLS_AT_BAR_TIME"


def test_order_lifetime_is_measured_from_engine_timestamps() -> None:
    block = _execution_telemetry(
        _profile(),
        [{"ts_init": 1_000, "ts_last": 4_000}, {"ts_init": 2_000, "ts_last": 2_500}],
        "DataFrame", [], [],
    )
    assert block["order_to_fill_ns_mean"] == 1750
    assert block["order_to_fill_ns_max"] == 3000
    assert block["latency_observability"] == "MEASURED_ORDER_LIFETIME"


def test_shortfall_never_compares_across_instruments() -> None:
    """Regression: matching a fill to another leg's decision produced -2.4e6 bps."""
    block = _execution_telemetry(
        _profile(), [], "DataFrame",
        [{"instrument_id": "AVAXUSDT-PERP.BINANCE", "side": "BUY",
          "avg_px_open": "20.0", "event_ns": 300}],
        [
            # BTC decision at ~100k is the most recent overall, but it is a
            # different instrument and must not be used as AVAX's reference.
            {"instrument_id": "BTCUSDT-PERP.BINANCE", "decision_ns": 200, "close": "105000.0"},
        ],
    )
    assert block["slippage_samples"] == 0
    assert block["slippage_bps_mean"] is None
    assert block["slippage_unmatched_positions"] == 1


def test_shortfall_uses_the_same_instrument_decision() -> None:
    block = _execution_telemetry(
        _profile(), [], "DataFrame",
        [{"instrument_id": "AVAXUSDT-PERP.BINANCE", "side": "BUY",
          "avg_px_open": "20.2", "event_ns": 300}],
        [
            {"instrument_id": "BTCUSDT-PERP.BINANCE", "decision_ns": 250, "close": "105000.0"},
            {"instrument_id": "AVAXUSDT-PERP.BINANCE", "decision_ns": 200, "close": "20.0"},
        ],
    )
    assert block["slippage_samples"] == 1
    assert block["slippage_bps_mean"] == pytest.approx(100.0)


def test_cross_series_reference_is_rejected_not_averaged() -> None:
    """Regression: a single-instrument config over a multi-asset tape gave -5.8e7 bps."""
    block = _execution_telemetry(
        _profile(), [], "DataFrame",
        [{"instrument_id": LEG, "side": "BUY", "avg_px_open": "105000.0", "event_ns": 300}],
        # same instrument id, but the price belongs to a different asset's series
        [{"instrument_id": LEG, "decision_ns": 200, "close": "20.0"}],
    )
    assert block["slippage_samples"] == 0
    assert block["slippage_rejected_cross_series"] == 1
    assert block["slippage_bps_mean"] is None


def test_same_series_reference_within_band_is_still_measured() -> None:
    block = _execution_telemetry(
        _profile(), [], "DataFrame",
        [{"instrument_id": LEG, "side": "BUY", "avg_px_open": "101.0", "event_ns": 300}],
        [{"instrument_id": LEG, "decision_ns": 200, "close": "100.0"}],
    )
    assert block["slippage_samples"] == 1
    assert block["slippage_rejected_cross_series"] == 0


def test_telemetry_uses_the_last_decision_at_or_before_the_fill() -> None:
    block = _execution_telemetry(
        _profile(), [], "list",
        [{"instrument_id": LEG, "side": "BUY", "avg_px_open": "110.0", "event_ns": 300}],
        [
            {"instrument_id": LEG, "decision_ns": 100, "close": "100.0"},
            {"instrument_id": LEG, "decision_ns": 200, "close": "110.0"},
        ],
    )
    # reference must be the 200ns decision (110), not the earlier one
    assert block["slippage_bps_mean"] == pytest.approx(0.0)
    assert block["decision_to_position_event_ns_mean"] == 100


# ---------------------------------------------------------------- primitives


def test_as_float_rejects_non_finite_and_unparseable_values() -> None:
    assert _as_float("1.5") == pytest.approx(1.5)
    assert _as_float(2) == pytest.approx(2.0)
    assert _as_float(None) is None
    assert _as_float("abc") is None
    assert _as_float(float("nan")) is None
    assert _as_float(float("inf")) is None


def test_fill_signature_is_order_independent_and_content_sensitive() -> None:
    a = {"instrument_id": "BTCUSDT-PERP.BINANCE", "side": "BUY", "quantity": "1", "last_px": "100", "ts_event": 5}
    b = {"instrument_id": "ETHUSDT-PERP.BINANCE", "side": "SELL", "quantity": "2", "last_px": "200", "ts_event": 6}
    assert _fill_signature([a, b]) == _fill_signature([b, a])
    changed = dict(b, last_px="201")
    assert _fill_signature([a, b]) != _fill_signature([a, changed])
    assert _fill_signature([]) == _fill_signature([])
