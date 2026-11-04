"""Recoverability waterfall + information boundary contracts (MECHANICS ONLY).

Hand-written stage values exercising the waterfall arithmetic and the fail-closed future
refusal; no assertion carries evaluative weight.
"""

from __future__ import annotations

import pytest

from v8_next.oracle import (
    Feature,
    InformationAdapter,
    InformationField,
    InformationSet,
    OracleRefused,
    compute_recoverability_chain,
)


def test_measured_chain_certifies_monotonic_waterfall() -> None:
    waterfall = compute_recoverability_chain(
        hindsight_ceiling_r=500.0,
        pit_recoverable_r=225.0,
        promotable_r=123.75,
        realized_live_r=50.0,
        hindsight_trades=10_000,
        pit_trades=6_000,
        promotable_trades=4_200,
        live_trades=2_460,
    )
    assert len(waterfall.stages) == 4
    assert waterfall.monotonicity_verified
    assert waterfall.status == "RECOVERABILITY_CHAIN_CERTIFIED"
    assert waterfall.claim == "NO_ECONOMIC_CLAIM"
    ceilings = [s.theoretical_ceiling_r for s in waterfall.stages]
    assert ceilings == sorted(ceilings, reverse=True)
    counts = [s.recoverable_trades_count for s in waterfall.stages]
    assert counts == sorted(counts, reverse=True)
    assert waterfall.pit_information_loss_r == pytest.approx(275.0)
    assert waterfall.actionable_recoverable_alpha_r == pytest.approx(175.0)
    assert waterfall.waterfall_id.startswith("waterfall-")


def test_utility_ordering_violation_is_named() -> None:
    waterfall = compute_recoverability_chain(
        hindsight_ceiling_r=100.0,
        pit_recoverable_r=200.0,
        promotable_r=50.0,
        realized_live_r=10.0,
    )
    assert not waterfall.monotonicity_verified
    assert waterfall.status == "MONOTONICITY_VIOLATION"


def test_unmeasured_stage_is_not_read_as_zero() -> None:
    waterfall = compute_recoverability_chain(
        hindsight_ceiling_r=500.0,
        pit_recoverable_r=None,
        promotable_r=123.75,
        realized_live_r=50.0,
        missing_reason="MISSING_LEGAL_ACTION_MANIFEST",
    )
    assert waterfall.status == "RECOVERABILITY_UNMEASURED"
    assert not waterfall.monotonicity_verified
    assert waterfall.stages[1].theoretical_ceiling_r is None
    assert waterfall.stages[1].missing_reason == "MISSING_LEGAL_ACTION_MANIFEST"


def test_future_field_cannot_enter_information_set() -> None:
    information = InformationSet(decision_time=100)
    with pytest.raises(OracleRefused) as excinfo:
        information.insert(
            InformationField(
                name="future_return",
                value=1.0,
                event_time=101,
                knowledge_time=101,
                availability_time=101,
                source_id="tape",
                source_version="v1",
            )
        )
    assert excinfo.value.refusal.value == "MISSING_DECISION_TIME_DATA"
    assert information.get("future_return") is None


def test_future_suffix_mutation_leaves_decision_information_identical() -> None:
    """I2/R6: decision-time information is identical with or without future bars.

    Two information sets built at the same decision time — one where the tape continues
    with mutated future bars, one where it ends — hold exactly the same fields, because
    the future was never insertable in the first place.
    """
    closes_then = [100.0, 101.0, 102.0]
    closes_mutated_future = [100.0, 101.0, 102.0, 999.0, 0.01]

    def build(closes: list[float], decision_time: int) -> InformationSet:
        information = InformationSet(decision_time=decision_time)
        for i, close in enumerate(closes):
            if i > decision_time:
                continue
            information.insert(
                InformationField(
                    name=f"close_lag_{decision_time - i}",
                    value=close,
                    event_time=i,
                    knowledge_time=i,
                    availability_time=i,
                    source_id="tape",
                    source_version="v1",
                )
            )
        return information

    plain = build(closes_then, 2)
    mutated = build(closes_mutated_future, 2)
    assert plain.names() == mutated.names()
    assert [plain.value_f64(n) for n in plain.names()] == [
        mutated.value_f64(n) for n in mutated.names()
    ]


def test_adapter_refuses_future_computed_features() -> None:
    with pytest.raises(OracleRefused):
        InformationAdapter.feature(
            Feature(
                name="close",
                value=101.0,
                max_input_available_time=101,
                feature_version="v1",
            ),
            event_time=100,
            decision_time=100,
            source_id="state",
        )
    field = InformationAdapter.feature(
        Feature(name="close", value=101.0, max_input_available_time=100, feature_version="v1"),
        event_time=99,
        decision_time=100,
        source_id="state",
    )
    assert field.knowledge_time == 100
