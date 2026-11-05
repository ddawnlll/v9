"""P1 control arms: matched random-entry null, PIT regime split, honest verdicts.

Every test here is arithmetic over measurements the test itself supplies; no
market number is claimed. The last test labels the real authorised development
tape with the same splitter and checks the point-in-time property on it.
"""

from __future__ import annotations

import json
import random
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

from v8_next.evaluation.ablations import (
    V9_INTERVENTION_KIND,
    ArmResult,
    Intervention,
    Provenance,
)
from v8_next.evaluation.control_arms import (
    CONTROL_ARM_KIND,
    GATE_CONTROL_ARMS_ABSENT,
    GATE_INSIDE_CONTROL_SPREAD,
    GATE_NO_CONTROL_DRAWS_IN_BUCKET,
    GATE_P_ABOVE_ALPHA,
    GATE_P_RESOLUTION_INSUFFICIENT,
    GATE_TOO_FEW_CAMPAIGNS,
    GATE_TOO_FEW_DRAWS,
    GATE_TREATMENT_NOT_MEASURED,
    SCHEMA_VERSION,
    CampaignOutcome,
    ControlArmMismatch,
    ControlArmRefusal,
    ControlBattery,
    ControlVerdict,
    MatchSpec,
    OpportunityOutcome,
    Regime,
    RegimeBar,
    RegimeConfig,
    RegimeLabel,
    compare_by_regime,
    compare_to_controls,
    label_regimes,
    random_entry_controls,
    regime_counts,
    regime_lookup,
    spec_mismatches,
)

DEV_TAPE = Path(__file__).resolve().parents[1] / "research" / "tape" / "btcusdt-1h-2025Q1"
BUDGET = 40
FAST_RESAMPLES = 50


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


def _provenance(**overrides: object) -> Provenance:
    payload: dict[str, object] = {
        "tape_sha256": "a" * 64,
        "tape_manifest_sha256": "b" * 64,
        "config_sha256": "c" * 64,
        "engine": "nautilus_trader.BacktestEngine",
        "engine_version": "2.0.0rc4",
        "execution_profile": "baseline",
        "execution_profile_digest": "d" * 64,
        "fee_model": "maker_taker",
        "maker_fee": "0.0002",
        "taker_fee": "0.0005",
        "funding_basis": "TAPE_FUNDING_ROWS=1 COVERAGE=FULL",
        "mark_basis": "OHLC_CLOSE_ASOF_BOUNDED_HISTORICAL_MODEL_NOT_VENUE_MARK",
        "intrabar_tie_basis": "NAUTILUS_DEFAULT_ORDERING",
        "slippage_basis": "FILL_MODEL=default SLIPPED=False PROB_SLIPPAGE=0.0",
        "partial_fills_basis": "ORDER_FILL_REPORT_ROWS=2 PARTIAL_ORDERS=0",
    }
    payload.update(overrides)
    return Provenance(**payload)  # type: ignore[arg-type]


def _spec(**overrides: object) -> MatchSpec:
    payload: dict[str, object] = {
        "calendar": "2025Q1_BTCUSDT",
        "capital": Decimal("10000"),
        "venue": "BINANCE",
        "stop_bps": Decimal("100"),
        "target_bps": Decimal("200"),
        "fee_bps_per_side": Decimal("5"),
        "funding_bps_per_day": Decimal("1.5"),
        "slippage_bps_per_side": Decimal("2"),
        "opportunity_exposure": "ALL_QUALIFIED_OPPORTUNITIES_EQUAL_CAMPAIGN_COUNT",
    }
    payload.update(overrides)
    return MatchSpec(**payload)  # type: ignore[arg-type]


def _pool(count: int = 60, *, span: int = 60) -> tuple[OpportunityOutcome, ...]:
    values = [-0.8 + 0.03 * index for index in range(count)]
    return tuple(
        OpportunityOutcome(
            opportunity_id=f"opp-{index:03d}",
            bar_index=index % span,
            net_r=round(values[index], 6),
        )
        for index in range(count)
    )


def _battery(
    *,
    outcomes: tuple[OpportunityOutcome, ...] | None = None,
    spec: MatchSpec | None = None,
    provenance: Provenance | None = None,
    seed: int = 7,
    draws: int = 50,
    exposure_budget: int = BUDGET,
) -> ControlBattery:
    return random_entry_controls(
        spec=spec or _spec(),
        outcomes=_pool() if outcomes is None else outcomes,
        provenance=provenance or _provenance(),
        exposure_budget=exposure_budget,
        seed=seed,
        draws=draws,
        resamples=FAST_RESAMPLES,
    )


def _treatment(
    *,
    samples: tuple[float, ...],
    provenance: Provenance | None = None,
    costed: bool = True,
    net_r: float | None = None,
    arm_id: str = "v9_adaptive",
) -> ArmResult:
    resolved = net_r if net_r is not None else (sum(samples) / len(samples) if samples else None)
    return ArmResult(
        arm_id=arm_id,
        intervention=Intervention.ALLOCATION,
        costed=costed,
        trades=len(samples),
        net_r=resolved,
        net_pnl=None,
        max_drawdown=None,
        hit_rate=None,
        fee_total=None,
        funding_total=None,
        ci_low=None,
        ci_high=None,
        ci_method=None,
        provenance=_provenance() if provenance is None else provenance,
        net_r_samples=tuple(samples),
        risk_basis="BRACKET_RISK",
        open_campaigns=0,
        arm_kind=V9_INTERVENTION_KIND,
    )


# --------------------------------------------------------------------------- #
# Matching
# --------------------------------------------------------------------------- #


def test_controls_carry_the_matched_dimensions() -> None:
    battery = _battery()
    assert len(battery.arms) == battery.draws == 50
    assert all(arm.arm_kind == CONTROL_ARM_KIND for arm in battery.arms)
    assert all(arm.costed for arm in battery.arms)
    assert all(arm.trades == BUDGET for arm in battery.arms)
    # The bracket/capital/venue/calendar travel with every arm's risk basis.
    for arm in battery.arms[:3]:
        assert arm.risk_basis is not None
        assert "STOP_BPS=100" in arm.risk_basis
        assert "TARGET_BPS=200" in arm.risk_basis
        assert "CAPITAL=10000" in arm.risk_basis
        assert "VENUE=BINANCE" in arm.risk_basis
        assert "CALENDAR=2025Q1_BTCUSDT" in arm.risk_basis
        # Same measurement basis as the treatment, by construction.
        assert arm.provenance == battery.provenance


def test_matched_treatment_is_compared_without_refusal() -> None:
    battery = _battery()
    treatment = _treatment(samples=tuple(5.0 for _ in range(BUDGET)))
    result = compare_to_controls(treatment, battery, treatment_spec=_spec())
    assert result.verdict is ControlVerdict.PASS
    assert result.spec_digest == _spec().digest
    assert result.seed == battery.seed


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("calendar", "2024Q4_BTCUSDT"),
        ("capital", Decimal("5000")),
        ("venue", "BYBIT"),
        ("stop_bps", Decimal("150")),
        ("target_bps", Decimal("400")),
        ("fee_bps_per_side", Decimal("7")),
        ("funding_bps_per_day", Decimal("0")),
        ("slippage_bps_per_side", Decimal("9")),
        ("opportunity_exposure", "HALF_THE_OPPORTUNITIES"),
    ],
)
def test_mismatched_matched_dimension_is_refused(field: str, value: object) -> None:
    battery = _battery(spec=_spec(**{field: value}))
    treatment = _treatment(samples=tuple(0.05 for _ in range(BUDGET)))
    with pytest.raises(ControlArmMismatch) as raised:
        compare_to_controls(treatment, battery, treatment_spec=_spec())
    assert raised.value.field == field
    assert field in raised.value.fields
    assert "mismatched" in raised.value.message
    assert spec_mismatches(_spec(), _spec(**{field: value})) == (field,)


def test_control_measured_on_another_tape_is_refused() -> None:
    battery = _battery(provenance=_provenance(tape_sha256="e" * 64))
    treatment = _treatment(samples=tuple(0.05 for _ in range(BUDGET)))
    with pytest.raises(ControlArmMismatch) as raised:
        compare_to_controls(treatment, battery, treatment_spec=_spec())
    assert raised.value.field == "provenance"
    assert "tape" in raised.value.message


def test_control_priced_by_another_cost_model_is_refused() -> None:
    battery = _battery(provenance=_provenance(maker_fee="0.0001"))
    treatment = _treatment(samples=tuple(0.05 for _ in range(BUDGET)))
    with pytest.raises(ControlArmMismatch) as raised:
        compare_to_controls(treatment, battery, treatment_spec=_spec())
    assert raised.value.field == "maker_fee"
    assert "cost model" in raised.value.message


def test_control_exposure_budget_must_equal_treatment_campaigns() -> None:
    battery = _battery(exposure_budget=BUDGET)
    treatment = _treatment(samples=tuple(0.05 for _ in range(BUDGET - 5)))
    with pytest.raises(ControlArmMismatch) as raised:
        compare_to_controls(treatment, battery, treatment_spec=_spec())
    assert raised.value.field == "opportunity_exposure"
    assert "budget" in raised.value.message


def test_uncosted_control_arm_is_refused() -> None:
    battery = _battery()
    stripped = replace(battery, arms=(replace(battery.arms[0], costed=False), *battery.arms[1:]))
    treatment = _treatment(samples=tuple(0.05 for _ in range(BUDGET)))
    with pytest.raises(ControlArmMismatch) as raised:
        compare_to_controls(treatment, stripped, treatment_spec=_spec())
    assert raised.value.field == "costed"


def test_treatment_without_provenance_is_refused() -> None:
    battery = _battery()
    treatment = replace(_treatment(samples=tuple(0.05 for _ in range(BUDGET))), provenance=None)
    with pytest.raises(ControlArmMismatch) as raised:
        compare_to_controls(treatment, battery, treatment_spec=_spec())
    assert raised.value.code == "PROVENANCE_ABSENT"


# --------------------------------------------------------------------------- #
# Determinism
# --------------------------------------------------------------------------- #


def test_same_seed_reproduces_identical_draws() -> None:
    once = _battery(seed=20251105)
    twice = _battery(seed=20251105)
    assert once.distribution == twice.distribution
    assert once.selections == twice.selections
    assert [arm.arm_id for arm in once.arms] == [arm.arm_id for arm in twice.arms]
    assert once.to_dict() == twice.to_dict()


def test_different_seed_changes_the_draws() -> None:
    left = _battery(seed=1)
    right = _battery(seed=2)
    assert left.selections != right.selections
    assert left.distribution != right.distribution


def test_pool_ordering_does_not_change_the_draws() -> None:
    ordered = _battery(seed=11)
    shuffled_pool = list(_pool())
    random.Random(99).shuffle(shuffled_pool)
    shuffled = _battery(outcomes=tuple(shuffled_pool), seed=11)
    assert ordered.selections == shuffled.selections
    assert ordered.distribution == shuffled.distribution


def test_generator_never_uses_the_global_rng(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = random.getstate()

    def boom(*args: object, **kwargs: object) -> None:
        raise AssertionError("the global RNG was used")

    for name in ("random", "seed", "sample", "randrange", "shuffle", "choice"):
        monkeypatch.setattr(random, name, boom)
    battery = _battery()
    assert len(battery.arms) == battery.draws
    assert random.getstate() == state


def test_battery_publishes_its_seed_and_spec() -> None:
    battery = _battery(seed=424242)
    payload = battery.to_dict()
    assert payload["schema_version"] == SCHEMA_VERSION
    assert payload["seed"] == 424242
    assert payload["spec_digest"] == _spec().digest
    assert payload["arm_kind"] == CONTROL_ARM_KIND
    assert len(payload["arm_ids"]) == battery.draws
    assert len(payload["distribution"]) == battery.draws
    # Deterministic JSON: the same battery serialises to the same bytes.
    assert json.dumps(payload, sort_keys=True) == json.dumps(
        _battery(seed=424242).to_dict(), sort_keys=True
    )


@pytest.mark.parametrize(
    ("kwargs", "field"),
    [
        ({"draws": 0}, "draws"),
        ({"exposure_budget": 0}, "exposure_budget"),
        ({"exposure_budget": 61}, "exposure_budget"),
        ({"resamples": 0}, "resamples"),
    ],
)
def test_generator_refuses_impossible_inputs(kwargs: dict[str, int], field: str) -> None:
    payload: dict[str, object] = {
        "spec": _spec(),
        "outcomes": _pool(),
        "provenance": _provenance(),
        "exposure_budget": BUDGET,
        "seed": 3,
        "draws": 4,
        "resamples": FAST_RESAMPLES,
    }
    payload.update(kwargs)
    with pytest.raises(ControlArmRefusal) as raised:
        random_entry_controls(**payload)  # type: ignore[arg-type]
    assert raised.value.field == field


def test_generator_refuses_duplicate_opportunity_ids() -> None:
    pool = _pool()
    duplicated = (pool[0], replace(pool[1], opportunity_id=pool[0].opportunity_id), *pool[2:])
    with pytest.raises(ControlArmRefusal) as raised:
        _battery(outcomes=duplicated)
    assert raised.value.field == "opportunity_id"


def test_outcome_refuses_non_finite_net_r() -> None:
    with pytest.raises(ControlArmRefusal):
        OpportunityOutcome(opportunity_id="x", bar_index=1, net_r=float("nan"))
    with pytest.raises(ControlArmRefusal):
        OpportunityOutcome(opportunity_id="x", bar_index=-1, net_r=0.1)


# --------------------------------------------------------------------------- #
# Statistical honesty
# --------------------------------------------------------------------------- #


def test_treatment_drawn_from_the_same_distribution_is_no_edge() -> None:
    battery = _battery(seed=7)
    # The treatment is another draw from the very same pool: it must not win.
    same_arm = random_entry_controls(
        spec=_spec(),
        outcomes=_pool(),
        provenance=_provenance(),
        exposure_budget=BUDGET,
        seed=999,
        draws=1,
        resamples=FAST_RESAMPLES,
    ).arms[0]
    treatment = _treatment(samples=tuple(same_arm.net_r_samples))
    result = compare_to_controls(treatment, battery, treatment_spec=_spec())
    assert result.verdict is ControlVerdict.NO_EDGE
    assert result.gate == GATE_INSIDE_CONTROL_SPREAD
    assert result.empirical_p is not None
    assert result.empirical_p > result.alpha
    assert result.percentile_rank is not None and 0.0 <= result.percentile_rank <= 1.0


def test_treatment_at_the_control_mean_is_no_edge() -> None:
    battery = _battery(seed=7)
    control_mean = sum(battery.distribution) / len(battery.distribution)
    treatment = _treatment(samples=tuple(control_mean for _ in range(BUDGET)))
    result = compare_to_controls(treatment, battery, treatment_spec=_spec())
    assert result.verdict is ControlVerdict.NO_EDGE
    assert result.delta_net_r == pytest.approx(0.0)


def test_far_outside_treatment_gets_the_smallest_attainable_p() -> None:
    battery = _battery(seed=7)
    treatment = _treatment(samples=tuple(5.0 for _ in range(BUDGET)))
    result = compare_to_controls(treatment, battery, treatment_spec=_spec())
    draws = len(battery.distribution)
    assert result.verdict is ControlVerdict.PASS
    assert result.empirical_p == pytest.approx(1.0 / (draws + 1))
    assert result.percentile_rank == pytest.approx(1.0)
    assert result.control_q95 is not None and result.control_q95 < 5.0
    assert result.delta_net_r is not None and result.delta_net_r > 0.0


def test_alpha_above_the_smallest_p_still_requires_an_actual_exceedance() -> None:
    battery = _battery(seed=7)
    treatment = _treatment(samples=tuple(5.0 for _ in range(BUDGET)))
    # A permissive alpha must not turn an inside-spread arm into a PASS.
    inside = _treatment(samples=tuple(0.0 for _ in range(BUDGET)))
    result = compare_to_controls(inside, battery, treatment_spec=_spec(), alpha=0.99)
    assert result.verdict is ControlVerdict.NO_EDGE
    assert result.gate == GATE_INSIDE_CONTROL_SPREAD
    shallow = compare_to_controls(treatment, battery, treatment_spec=_spec(), alpha=0.5)
    assert shallow.verdict is ControlVerdict.PASS


def test_too_few_campaigns_is_inconclusive() -> None:
    battery = _battery(seed=7, exposure_budget=5)
    treatment = _treatment(samples=(5.0,) * 5, net_r=5.0)
    result = compare_to_controls(treatment, battery, treatment_spec=_spec())
    assert result.verdict is ControlVerdict.INCONCLUSIVE
    assert result.gate == GATE_TOO_FEW_CAMPAIGNS
    # The numbers that exist are still published; only the claim is withheld.
    assert result.empirical_p == pytest.approx(1.0 / (len(battery.distribution) + 1))


def test_too_few_draws_is_inconclusive() -> None:
    battery = _battery(seed=7, draws=5, exposure_budget=BUDGET)
    treatment = _treatment(samples=tuple(5.0 for _ in range(BUDGET)))
    result = compare_to_controls(treatment, battery, treatment_spec=_spec(), min_campaigns=5)
    assert result.verdict is ControlVerdict.INCONCLUSIVE
    assert result.gate == GATE_TOO_FEW_DRAWS


def test_inside_spread_with_two_exceedances_is_no_edge_not_pass() -> None:
    battery = _battery(seed=7)
    widened = replace(
        battery,
        arms=(
            replace(battery.arms[0], net_r=10.0, net_r_samples=(10.0,)),
            replace(battery.arms[1], net_r=10.0, net_r_samples=(10.0,)),
            *battery.arms[2:],
        ),
    )
    treatment = _treatment(samples=tuple(1.0 for _ in range(BUDGET)))
    result = compare_to_controls(treatment, widened, treatment_spec=_spec())
    assert result.verdict is ControlVerdict.NO_EDGE
    assert result.gate == GATE_P_ABOVE_ALPHA
    assert result.empirical_p == pytest.approx(3.0 / (len(widened.distribution) + 1))
    assert result.control_q95 is not None and result.control_q95 < 1.0


def test_p_value_resolution_is_enforced() -> None:
    battery = _battery(seed=7, draws=10, exposure_budget=BUDGET)
    treatment = _treatment(samples=tuple(5.0 for _ in range(BUDGET)))
    result = compare_to_controls(
        treatment, battery, treatment_spec=_spec(), min_draws=5, min_campaigns=5
    )
    assert result.verdict is ControlVerdict.INCONCLUSIVE
    assert result.gate == GATE_P_RESOLUTION_INSUFFICIENT
    assert result.empirical_p == pytest.approx(1.0 / 11.0)


def test_absent_treatment_is_not_run_with_the_reason() -> None:
    battery = _battery(seed=7)
    missing = _treatment(samples=(), net_r=None)
    result = compare_to_controls(missing, battery, treatment_spec=_spec())
    assert result.verdict is ControlVerdict.NOT_RUN
    assert result.gate == GATE_TREATMENT_NOT_MEASURED
    assert "no measured per-campaign net R" in result.reason
    assert result.empirical_p is None
    assert result.treatment_net_r is None


def test_uncosted_treatment_is_not_run() -> None:
    battery = _battery(seed=7)
    treatment = _treatment(samples=tuple(0.5 for _ in range(BUDGET)), costed=False)
    result = compare_to_controls(treatment, battery, treatment_spec=_spec())
    assert result.verdict is ControlVerdict.NOT_RUN
    assert result.gate == GATE_TREATMENT_NOT_MEASURED


def test_battery_without_arms_is_inconclusive() -> None:
    battery = _battery(seed=7)
    empty = replace(battery, arms=(), selections=(), draws=0, exposure_budget=None)
    treatment = _treatment(samples=tuple(0.5 for _ in range(BUDGET)))
    result = compare_to_controls(treatment, empty, treatment_spec=_spec())
    assert result.verdict is ControlVerdict.INCONCLUSIVE
    assert result.gate == GATE_CONTROL_ARMS_ABSENT


def test_alpha_outside_unit_interval_is_refused() -> None:
    battery = _battery(seed=7)
    treatment = _treatment(samples=tuple(0.5 for _ in range(BUDGET)))
    for alpha in (0.0, 1.0, -0.1):
        with pytest.raises(ControlArmRefusal) as raised:
            compare_to_controls(treatment, battery, treatment_spec=_spec(), alpha=alpha)
        assert raised.value.field == "alpha"


# --------------------------------------------------------------------------- #
# Regime splitter
# --------------------------------------------------------------------------- #

SMALL = RegimeConfig(
    trend_lookback=5,
    vol_lookback=5,
    slope_threshold_per_bar=0.001,
    high_vol_multiple=1.5,
)


def _bars() -> tuple[RegimeBar, ...]:
    """Four documented segments: up, down, range, high-vol, plus the warmup."""
    closes: list[float] = [100.0]
    block = 40
    for _ in range(block):  # trend up: +1% per bar
        closes.append(closes[-1] * 1.01)
    for _ in range(block):  # trend down: -1% per bar
        closes.append(closes[-1] * 0.99)
    for index in range(block):  # range: +-0.02% around the level
        step = 1.0002 if index % 2 == 0 else 0.9998
        closes.append(closes[-1] * step)
    for index in range(block):  # high vol: alternating +-4%
        step = 1.04 if index % 2 == 0 else 0.96
        closes.append(closes[-1] * step)
    return tuple(
        RegimeBar(close=close, high=close * 1.0005, low=close * 0.9995) for close in closes
    )


def test_regime_labels_cover_every_documented_regime() -> None:
    labels = label_regimes(_bars(), SMALL)
    present = {label.regime for label in labels}
    assert {
        Regime.UNLABELLED,
        Regime.TREND_UP,
        Regime.TREND_DOWN,
        Regime.RANGE,
        Regime.HIGH_VOL,
    } <= present
    assert all(label.index == index for index, label in enumerate(labels))


def test_regime_labels_are_point_in_time() -> None:
    bars = _bars()
    full = label_regimes(bars, SMALL)
    for index in range(len(bars)):
        prefix = label_regimes(bars[: index + 1], SMALL)
        assert prefix == full[: index + 1], f"label at {index} used a future bar"
        # Truncating the future and appending a different future must not move it.
        mutated = label_regimes(
            (*bars[: index + 1], RegimeBar(close=1.0, high=1.0, low=1.0)), SMALL
        )
        assert mutated[: index + 1] == full[: index + 1]


def test_regime_labels_are_deterministic() -> None:
    assert label_regimes(_bars(), SMALL) == label_regimes(_bars(), SMALL)


def test_regime_counts_sum_to_the_total() -> None:
    bars = _bars()
    labels = label_regimes(bars, SMALL)
    counts = regime_counts(labels)
    assert sum(counts.values()) == len(bars)
    assert counts[Regime.UNLABELLED] == SMALL.warmup
    assert set(counts) == set(Regime)


def test_regime_config_refuses_degenerate_windows() -> None:
    with pytest.raises(ControlArmRefusal):
        RegimeConfig(trend_lookback=1)
    with pytest.raises(ControlArmRefusal):
        RegimeConfig(vol_lookback=1)
    with pytest.raises(ControlArmRefusal):
        RegimeConfig(slope_threshold_per_bar=0.0)
    with pytest.raises(ControlArmRefusal):
        RegimeConfig(high_vol_multiple=1.0)


def test_regime_bar_refuses_bad_prices() -> None:
    with pytest.raises(ControlArmRefusal):
        RegimeBar(close=0.0, high=1.0, low=1.0)
    with pytest.raises(ControlArmRefusal):
        RegimeBar(close=1.0, high=0.5, low=1.0)


# --------------------------------------------------------------------------- #
# Per-regime arm results
# --------------------------------------------------------------------------- #


def _regime_fixture() -> tuple[
    tuple[RegimeBar, ...],
    tuple[RegimeLabel, ...],
    tuple[CampaignOutcome, ...],
    ArmResult,
    ControlBattery,
]:
    bars = _bars()
    labels = label_regimes(bars, SMALL)
    lookup = regime_lookup(labels)
    up_bars = [label.index for label in labels if label.regime is Regime.TREND_UP][:40]
    high_vol_bars = [label.index for label in labels if label.regime is Regime.HIGH_VOL][:2]
    chosen = [*up_bars, *high_vol_bars]
    campaigns = tuple(
        CampaignOutcome(bar_index=bar, net_r=0.05 + 0.001 * index)
        for index, bar in enumerate(chosen)
    )
    treatment = _treatment(samples=tuple(campaign.net_r for campaign in campaigns))
    pool = tuple(
        OpportunityOutcome(
            opportunity_id=f"ctrl-{index:03d}",
            bar_index=chosen[index % len(chosen)],
            net_r=-0.4 + 0.02 * (index % 20),
        )
        for index in range(80)
    )
    battery = _battery(outcomes=pool, exposure_budget=len(campaigns))
    assert len(lookup) == len(bars)
    return bars, labels, campaigns, treatment, battery


def test_per_regime_buckets_sum_to_the_total() -> None:
    bars, labels, campaigns, treatment, battery = _regime_fixture()
    report = compare_by_regime(
        treatment,
        campaigns,
        battery,
        labels,
        treatment_spec=_spec(),
        min_campaigns=5,
        min_draws=5,
        alpha=0.5,
    )
    assert report.total_bars == len(bars)
    assert report.total_campaigns == len(campaigns)
    assert sum(report.bar_counts.values()) == len(bars)
    assert sum(bucket.campaigns for bucket in report.buckets) == len(campaigns)
    assert sum(bucket.bars for bucket in report.buckets) == len(bars)
    assert {bucket.regime for bucket in report.buckets} == set(Regime)


def test_sparse_regime_bucket_is_inconclusive() -> None:
    _, labels, campaigns, treatment, battery = _regime_fixture()
    report = compare_by_regime(
        treatment,
        campaigns,
        battery,
        labels,
        treatment_spec=_spec(),
        min_campaigns=5,
        min_draws=5,
        alpha=0.5,
    )
    buckets = {bucket.regime: bucket for bucket in report.buckets}
    assert buckets[Regime.HIGH_VOL].campaigns == 2
    assert buckets[Regime.HIGH_VOL].comparison.verdict is ControlVerdict.INCONCLUSIVE
    assert buckets[Regime.HIGH_VOL].comparison.gate == GATE_TOO_FEW_CAMPAIGNS
    assert buckets[Regime.TREND_UP].campaigns >= 30
    assert buckets[Regime.TREND_UP].comparison.verdict is not ControlVerdict.INCONCLUSIVE
    # A bucket with no control entries reports that absence as INCONCLUSIVE too.
    assert buckets[Regime.RANGE].campaigns == 0
    assert buckets[Regime.RANGE].comparison.verdict is ControlVerdict.INCONCLUSIVE


def test_regime_report_publishes_counts_and_verdicts() -> None:
    _, labels, campaigns, treatment, battery = _regime_fixture()
    report = compare_by_regime(
        treatment,
        campaigns,
        battery,
        labels,
        treatment_spec=_spec(),
        min_campaigns=5,
        min_draws=5,
        alpha=0.5,
    )
    payload = report.to_dict()
    assert payload["schema_version"] == SCHEMA_VERSION
    assert payload["total_campaigns"] == len(campaigns)
    assert sum(payload["bar_counts"].values()) == len(labels)
    assert payload["overall"]["verdict"] in {verdict.value for verdict in ControlVerdict}
    assert len(payload["buckets"]) == len(Regime)
    assert all("comparison" in bucket for bucket in payload["buckets"])
    assert json.dumps(payload, sort_keys=True) == json.dumps(
        compare_by_regime(
            treatment,
            campaigns,
            battery,
            labels,
            treatment_spec=_spec(),
            min_campaigns=5,
            min_draws=5,
            alpha=0.5,
        ).to_dict(),
        sort_keys=True,
    )


def test_regime_bucket_without_control_entries_names_the_absence() -> None:
    bars = _bars()
    labels = label_regimes(bars, SMALL)
    range_bars = [label.index for label in labels if label.regime is Regime.RANGE][:10]
    up_bar = next(label.index for label in labels if label.regime is Regime.TREND_UP)
    campaigns = tuple(CampaignOutcome(bar_index=bar, net_r=0.1) for bar in range_bars)
    treatment = _treatment(samples=tuple(campaign.net_r for campaign in campaigns))
    # Every control entry lands in TREND_UP, so RANGE has treatment but no null.
    pool = tuple(
        OpportunityOutcome(opportunity_id=f"ctrl-{index:03d}", bar_index=up_bar, net_r=0.02)
        for index in range(20)
    )
    battery = _battery(outcomes=pool, exposure_budget=len(campaigns))
    report = compare_by_regime(
        treatment,
        campaigns,
        battery,
        labels,
        treatment_spec=_spec(),
        min_campaigns=5,
        min_draws=5,
        alpha=0.5,
    )
    range_bucket = {bucket.regime: bucket for bucket in report.buckets}[Regime.RANGE]
    assert range_bucket.campaigns == len(campaigns)
    assert range_bucket.comparison.gate == GATE_NO_CONTROL_DRAWS_IN_BUCKET
    assert range_bucket.comparison.verdict is ControlVerdict.INCONCLUSIVE


def test_regime_report_refuses_misattributed_campaigns() -> None:
    _, labels, campaigns, treatment, battery = _regime_fixture()
    wrong = tuple(replace(campaign, net_r=99.0) for campaign in campaigns)
    with pytest.raises(ControlArmRefusal) as raised:
        compare_by_regime(
            treatment, wrong, battery, labels, treatment_spec=_spec(), min_campaigns=5
        )
    assert raised.value.code == "CAMPAIGN_MISMATCH"


def test_regime_report_refuses_out_of_range_bars() -> None:
    _, labels, campaigns, treatment, battery = _regime_fixture()
    outside = (
        CampaignOutcome(bar_index=len(labels) + 5, net_r=campaigns[0].net_r),
        *campaigns[1:],
    )
    with pytest.raises(ControlArmRefusal) as raised:
        compare_by_regime(
            treatment, outside, battery, labels, treatment_spec=_spec(), min_campaigns=5
        )
    assert raised.value.code == "REGIME_LABEL_RANGE"


# --------------------------------------------------------------------------- #
# The authorised development tape
# --------------------------------------------------------------------------- #


@pytest.mark.skipif(not DEV_TAPE.exists(), reason="authorised development tape absent")
def test_regime_split_of_the_authorised_development_tape_is_pit_and_complete() -> None:
    from v8_next.research.screen import load_bars

    tape_jsonl = DEV_TAPE / "tape.jsonl"
    raw = load_bars(tape_jsonl)
    bars = tuple(RegimeBar(close=bar.close, high=bar.high, low=bar.low) for bar in raw)
    labels = label_regimes(bars)
    assert len(labels) == len(bars)
    counts = regime_counts(labels)
    assert sum(counts.values()) == len(bars)
    assert counts[Regime.UNLABELLED] == DEFAULT_WARMUP
    # Point-in-time on real data: every prefix reproduces the full labels.
    for index in (0, 1, DEFAULT_WARMUP, 500, len(bars) - 1):
        prefix = label_regimes(bars[: index + 1])
        assert prefix == labels[: index + 1]
    assert label_regimes(bars) == labels
    # The split is a real partition of the tape, not one dominant bucket.
    assert sum(1 for regime in Regime if regime is not Regime.UNLABELLED and counts[regime]) >= 2


DEFAULT_WARMUP = RegimeConfig().warmup
