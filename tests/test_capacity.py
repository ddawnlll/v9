"""MECHANICS ONLY: microstructure capacity refuses missing or unscalable inputs."""

import pytest

from v8_next.evaluation.capacity import (
    CAPACITY_STATUSES,
    CapacityScenario,
    MicrostructureObservation,
    SwingCapacityObservation,
    measure_capacity,
    measure_capacity_bounds_from_participation,
    measure_swing_capacity,
)


def _row(**overrides: object) -> MicrostructureObservation:
    fields: dict[str, object] = {
        "timestamp_ns": 1,
        "instrument_id": "BTCUSDT-PERP.BINANCE",
        "mid": 100.0,
        "spread": 1.0,
        "adv_qty": 1_000.0,
        "requested_qty": 10.0,
        "filled_qty": 8.0,
        "fill_price": 100.5,
    }
    fields.update(overrides)
    return MicrostructureObservation(**fields)  # type: ignore[arg-type]


def _swing_row(**overrides: object) -> SwingCapacityObservation:
    fields: dict[str, object] = {
        "timestamp_ns": 1,
        "instrument_id": "BTCUSDT-PERP.BINANCE",
        "bar_close": 100.0,
        "bar_volume": 100_000.0,
        "adv_notional": 1_000_000.0,
        "requested_notional": 10_000.0,
        "filled_notional": 8_000.0,
        "fill_price": 100.5,
        "decision_price": 100.0,
        "fee_notional": 4.0,
        "fill_model": "REAL_FILL_RECORD",
    }
    fields.update(overrides)
    return SwingCapacityObservation(**fields)  # type: ignore[arg-type]


def test_swing_capacity_uses_ohlcv_adv_and_real_fill_without_l2() -> None:
    result = measure_swing_capacity((_swing_row(),), CapacityScenario(scale=1.0, requested_multiplier=1.0))
    assert result.status == "NO_ECONOMIC_CLAIM"
    assert result.participation == pytest.approx(0.01)
    assert result.fill_ratio == pytest.approx(0.8)
    assert result.impact_bps is None
    assert result.shortfall_bps == pytest.approx(54.99999999999999)


def test_swing_capacity_without_real_fills_is_data_blocked() -> None:
    result = measure_swing_capacity((), CapacityScenario(scale=1.0, requested_multiplier=1.0))
    assert result.status == "DATA_BLOCKED"
    assert result.reason == "NO_REAL_FILL_OBSERVATIONS"


def test_capacity_missing_microstructure_is_data_blocked() -> None:
    result = measure_capacity((), CapacityScenario(scale=1.0, requested_multiplier=1.0))
    assert result.status == "DATA_BLOCKED"
    assert result.impact_bps is None
    assert result.reason == "NO_MICROSTRUCTURE_OBSERVATIONS"


def test_capacity_reports_observed_fill_and_impact_at_the_observed_scale() -> None:
    rows = (_row(),)
    result = measure_capacity(rows, CapacityScenario(scale=1.0, requested_multiplier=1.0))
    assert result.status == "NO_ECONOMIC_CLAIM"
    assert result.fill_ratio == 0.8
    assert result.impact_bps == pytest.approx(0.5 / 100.0 * 10_000.0)
    assert result.spread_cost_bps == pytest.approx(1.0 / 100.0 * 10_000.0)
    assert result.participation == pytest.approx(10.0 / 1_000.0)
    assert result.projected_participation is None
    assert result.reason is None


def test_capacity_counterfactual_scale_stays_unresolved() -> None:
    """Observed fills are 1x fills; a 2x scenario is not a measurement of 2x."""

    result = measure_capacity((_row(),), CapacityScenario(scale=2.0, requested_multiplier=2.0))
    assert result.status == "UNRESOLVED"
    assert result.reason == "OBSERVED_FILLS_AT_OTHER_SCALE"
    # measured participation stays the observed quantity; the counterfactual is
    # published as a labelled projection instead of overwriting it.
    assert result.participation == pytest.approx(10.0 / 1_000.0)
    assert result.projected_participation == pytest.approx(2.0 * 10.0 / 1_000.0)
    assert result.fill_ratio == 0.8
    assert result.impact_bps is not None


def test_capacity_rejects_unsequenced_observations() -> None:
    rows = (_row(timestamp_ns=5), _row(timestamp_ns=5))
    with pytest.raises(ValueError, match="UNSEQUENCED_MICROSTRUCTURE_OBSERVATIONS"):
        measure_capacity(rows, CapacityScenario(scale=1.0, requested_multiplier=1.0))


def test_capacity_rejects_mixed_instruments() -> None:
    rows = (_row(timestamp_ns=1), _row(timestamp_ns=2, instrument_id="ETHUSDT-PERP.BINANCE"))
    with pytest.raises(ValueError, match="UNSINGLE_INSTRUMENT_OBSERVATIONS"):
        measure_capacity(rows, CapacityScenario(scale=1.0, requested_multiplier=1.0))


def test_participation_bound_projection_never_publishes_a_measured_basis() -> None:
    """#477: a bound beyond the observed participation scale is UNRESOLVED.

    The canonical run observes ~2e-6 peak participation; a 1%/5%/10% threshold
    is a 10^3-10^5x projection. It may keep its arithmetic number, but it must
    name the projection and can never be published as MEASURED_*.
    """
    rows = measure_capacity_bounds_from_participation([1e-6, 2e-6], 10_000.0)
    assert len(rows) == 3
    for row in rows:
        assert row["status"] in CAPACITY_STATUSES
        assert row["status"] == "UNRESOLVED"
        assert row["reason"] == "OBSERVED_FILLS_AT_OTHER_SCALE"
        assert not row["kind"].startswith("MEASURED")
        assert row["observed_peak_participation"] == pytest.approx(2e-6)
        assert row["requested_participation_multiplier"] > 1e3
        assert row["max_capital_linear"] == pytest.approx(
            10_000.0 * row["participation_threshold"] / 2e-6
        )


def test_participation_bound_at_the_observed_scale_keeps_no_economic_claim() -> None:
    rows = measure_capacity_bounds_from_participation(
        [2e-6], 10_000.0, thresholds=(2e-6,)
    )
    assert rows[0]["kind"] == "MEASURED_OBSERVED_PARTICIPATION_BOUND"
    assert rows[0]["status"] == "NO_ECONOMIC_CLAIM"
    assert rows[0]["reason"] is None
    assert rows[0]["max_capital_linear"] == pytest.approx(10_000.0)


def test_participation_bound_without_observations_is_data_blocked() -> None:
    rows = measure_capacity_bounds_from_participation([], 10_000.0)
    assert all(row["status"] == "DATA_BLOCKED" for row in rows)
    assert all(row["reason"] == "NO_OBSERVED_PARTICIPATION" for row in rows)
    assert all(row["max_capital_linear"] is None for row in rows)
    assert all(not row["kind"].startswith("MEASURED") for row in rows)


def test_every_published_capacity_entry_carries_a_contract_status() -> None:
    """#477 R1 + gate 2: no `capacity_scenarios[*]` entry may ship without a status.

    The bound entries were fixed first; the three HYPOTHETICAL_SCENARIO entries kept
    publishing only `k_status`, so 3 of the 6 entries in the canonical receipt's
    `capacity_scenarios` block failed the issue's own gate 2. An uncalibrated impact
    model mints no economic claim (NO_ECONOMIC_CLAIM); a scenario whose sigma or peak
    participation was never measured is DATA_BLOCKED, not an absent key.
    """
    from v8_next.app.portfolio import capacity_from_participation

    vocab = {"DATA_BLOCKED", "UNRESOLVED", "NO_ECONOMIC_CLAIM"}

    measured = capacity_from_participation([1e-6, 2e-6], 10000.0, [0.001, -0.002, 0.0015] * 50)
    assert len(measured) == 6
    for entry in measured:
        assert "status" in entry, f"{entry['kind']} publishes no status"
        assert entry["status"] in vocab
        assert entry.get("reason"), f"{entry['kind']} publishes a status with no reason"
    scenarios = [r for r in measured if r["kind"] == "HYPOTHETICAL_SCENARIO"]
    assert len(scenarios) == 3
    assert all(r["status"] == "NO_ECONOMIC_CLAIM" for r in scenarios)
    assert all(r["reason"] == "HYPOTHETICAL_IMPACT_MODEL_UNCALIBRATED" for r in scenarios)

    blocked = capacity_from_participation([], 10000.0)
    for entry in blocked:
        assert entry["status"] in vocab
        assert entry.get("reason")
    blocked_scenarios = [r for r in blocked if r["kind"] == "HYPOTHETICAL_SCENARIO"]
    assert len(blocked_scenarios) == 3
    assert all(r["status"] == "DATA_BLOCKED" for r in blocked_scenarios)
    assert all(r["impact_bps_at_peak"] is None for r in blocked_scenarios)
