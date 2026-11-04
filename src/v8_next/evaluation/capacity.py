"""Evidence-backed swing capacity measurement.

The primary benchmark is hourly swing trading.  Capacity is measured from
hourly OHLCV/ADV observations plus fills produced by the declared execution
model; order-book/L2 data is not part of this contract.  Historical L2
captures remain archived evidence for retired microstructure experiments.

Fail-closed posture (guards, not estimators): this module publishes measured
quantities only at the scale at which the observations were actually taken.  A
counterfactual capital/participation scale is reported as a labelled
projection and the scenario resolves to ``UNRESOLVED``; observed fills are
never re-used as if they had been taken at another scale (no invented impact
coefficients, no linear notional extrapolation read as a measurement).
"""

from __future__ import annotations

import math
from typing import Any, Literal, Sequence

from pydantic import BaseModel, ConfigDict, Field, model_validator

CAPACITY_STATUSES = ("DATA_BLOCKED", "UNRESOLVED", "NO_ECONOMIC_CLAIM")


class SwingCapacityObservation(BaseModel):
    """One hourly bar and the fill actually observed on that bar.

    ``adv_notional`` must come from the declared OHLCV lookback.  Fill fields
    are required from a real execution record or an explicitly named fill
    model; they are never inferred from L2 or copied to another scale.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    timestamp_ns: int = Field(ge=0)
    instrument_id: str
    bar_close: float = Field(gt=0.0)
    bar_volume: float = Field(ge=0.0)
    adv_notional: float = Field(gt=0.0)
    requested_notional: float = Field(gt=0.0)
    filled_notional: float = Field(ge=0.0)
    fill_price: float = Field(gt=0.0)
    decision_price: float = Field(gt=0.0)
    fee_notional: float = Field(ge=0.0)
    fill_model: str

    @model_validator(mode="after")
    def validate_row(self) -> SwingCapacityObservation:
        if not self.instrument_id.strip() or not self.fill_model.strip():
            raise ValueError("instrument_id and fill_model are required")
        if self.filled_notional > self.requested_notional:
            raise ValueError("filled notional exceeds requested notional")
        if not all(math.isfinite(value) for value in (
            self.bar_close, self.bar_volume, self.adv_notional,
            self.requested_notional, self.filled_notional, self.fill_price,
            self.decision_price, self.fee_notional,
        )):
            raise ValueError("swing capacity values must be finite")
        return self


class MicrostructureObservation(BaseModel):
    """Deprecated L2-era observation kept for retired experiments only.

    The primary hourly swing benchmark uses :class:`SwingCapacityObservation`
    and :func:`measure_swing_capacity` instead.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    timestamp_ns: int = Field(ge=0)
    instrument_id: str
    mid: float = Field(gt=0.0)
    spread: float = Field(ge=0.0)
    adv_qty: float = Field(gt=0.0)
    requested_qty: float = Field(gt=0.0)
    filled_qty: float = Field(ge=0.0)
    fill_price: float = Field(gt=0.0)

    @model_validator(mode="after")
    def validate_row(self) -> MicrostructureObservation:
        if not self.instrument_id.strip():
            raise ValueError("instrument_id is required")
        if self.filled_qty > self.requested_qty:
            raise ValueError("filled quantity exceeds requested quantity")
        if not all(
            math.isfinite(value)
            for value in (
                self.mid,
                self.spread,
                self.adv_qty,
                self.requested_qty,
                self.filled_qty,
                self.fill_price,
            )
        ):
            raise ValueError("microstructure values must be finite")
        return self


class CapacityScenario(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    scale: float = Field(gt=0.0)
    requested_multiplier: float = Field(gt=0.0)


class CapacityMeasurement(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["DATA_BLOCKED", "UNRESOLVED", "NO_ECONOMIC_CLAIM"]
    scale: float
    participation: float | None
    fill_ratio: float | None
    spread_cost_bps: float | None
    impact_bps: float | None
    shortfall_bps: float | None
    observation_count: int
    # Measured participation is the observation-time quantity; anything asked
    # for at another requested size is a labelled projection, never a fill
    # measurement.
    projected_participation: float | None = None
    reason: str | None = None


def _validate_sequence(observations: tuple[MicrostructureObservation, ...]) -> None:
    """Enforce the sequencing/single-instrument contract of this module.

    The observations must be a strictly increasing event sequence for exactly
    one instrument; otherwise the row statistics below average across states
    that were never contemporaneous, which is not a measurement.
    """

    instruments = {row.instrument_id for row in observations}
    if len(instruments) != 1:
        raise ValueError(
            "UNSINGLE_INSTRUMENT_OBSERVATIONS: capacity rows must come from one "
            f"instrument, got {sorted(instruments)}"
        )
    previous = observations[0].timestamp_ns
    for row in observations[1:]:
        if row.timestamp_ns <= previous:
            raise ValueError(
                "UNSEQUENCED_MICROSTRUCTURE_OBSERVATIONS: timestamps must increase "
                f"strictly (saw {row.timestamp_ns} after {previous})"
            )
        previous = row.timestamp_ns


def _scenario_state(
    requested_multiplier: float,
) -> tuple[bool, Literal["UNRESOLVED", "NO_ECONOMIC_CLAIM"], str | None]:
    """One status ontology for every capacity surface (observed vs projected)."""
    scaled = requested_multiplier != 1.0
    return (
        scaled,
        "UNRESOLVED" if scaled else "NO_ECONOMIC_CLAIM",
        "OBSERVED_FILLS_AT_OTHER_SCALE" if scaled else None,
    )


def measure_capacity_bounds_from_participation(
    participations: Sequence[float],
    capital: float,
    thresholds: tuple[float, ...] = (0.01, 0.05, 0.10),
) -> list[dict[str, Any]]:
    """Publish capital bounds at a participation threshold under this contract.

    The canonical portfolio run observes fills at its own participation scale.
    A bound that asks for a *different* participation threshold is a projection
    of those observations, so it carries ``UNRESOLVED`` (with the projection
    named) and is never published under a ``MEASURED_*`` kind. Only a threshold
    the run actually observed keeps a measured kind, and even then the status
    stays ``NO_ECONOMIC_CLAIM``. Missing participation is ``DATA_BLOCKED`` --
    never an implied zero or an infinite bound.

    The arithmetic bound and its extrapolation identity are preserved beside
    the state, so no number is hidden or re-pinned by this routing.
    """
    peak = max(participations) if participations else 0.0
    out: list[dict[str, Any]] = []
    for threshold in thresholds:
        multiplier = threshold / peak if peak > 0 else None
        max_cap = capital * multiplier if multiplier is not None else None
        status: str
        reason: str | None
        if multiplier is None:
            status = "DATA_BLOCKED"
            reason = "NO_OBSERVED_PARTICIPATION"
            kind = "UNRESOLVED_LINEAR_BOUND"
        else:
            _scaled, status, reason = _scenario_state(multiplier)
            kind = (
                "MEASURED_OBSERVED_PARTICIPATION_BOUND"
                if status == "NO_ECONOMIC_CLAIM"
                else "PROJECTED_LINEAR_BOUND"
            )
        out.append(
            {
                "kind": kind,
                "status": status,
                "reason": reason,
                "participation_threshold": threshold,
                "observed_peak_participation": peak if peak > 0 else None,
                "requested_participation_multiplier": multiplier,
                "max_capital_linear": max_cap,
                "basis": "LINEAR_EXTRAPOLATION_OF_MEASURED_PARTICIPATION",
                "impact_beyond": "UNVERIFIED_NO_IMPACT_MODEL",
            }
        )
    return out


def measure_swing_capacity(
    observations: tuple[SwingCapacityObservation, ...], scenario: CapacityScenario
) -> CapacityMeasurement:
    """Measure observed swing capacity without L2 or scale extrapolation."""
    if not observations:
        return CapacityMeasurement(
            status="DATA_BLOCKED", scale=scenario.scale, participation=None,
            fill_ratio=None, spread_cost_bps=None, impact_bps=None,
            shortfall_bps=None, observation_count=0,
            projected_participation=None, reason="NO_REAL_FILL_OBSERVATIONS",
        )
    instruments = {row.instrument_id for row in observations}
    if len(instruments) != 1:
        raise ValueError("UNSINGLE_INSTRUMENT_OBSERVATIONS")
    previous = observations[0].timestamp_ns
    for row in observations[1:]:
        if row.timestamp_ns <= previous:
            raise ValueError("UNSEQUENCED_SWING_OBSERVATIONS")
        previous = row.timestamp_ns
    n = len(observations)
    participation = sum(row.requested_notional / row.adv_notional for row in observations) / n
    fill_ratio = sum(row.filled_notional / row.requested_notional for row in observations) / n
    shortfall = sum(
        abs(row.fill_price - row.decision_price) / row.decision_price * 10_000.0
        + row.fee_notional / row.filled_notional * 10_000.0
        if row.filled_notional > 0 else 0.0
        for row in observations
    ) / n
    scaled, status, reason = _scenario_state(scenario.requested_multiplier)
    return CapacityMeasurement(
        status=status,
        scale=scenario.scale, participation=participation,
        fill_ratio=fill_ratio, spread_cost_bps=None, impact_bps=None,
        shortfall_bps=shortfall, observation_count=n,
        projected_participation=participation * scenario.requested_multiplier if scaled else None,
        reason=reason,
    )


def measure_capacity(
    observations: tuple[MicrostructureObservation, ...], scenario: CapacityScenario
) -> CapacityMeasurement:
    if not observations:
        return CapacityMeasurement(
            status="DATA_BLOCKED",
            scale=scenario.scale,
            participation=None,
            fill_ratio=None,
            spread_cost_bps=None,
            impact_bps=None,
            shortfall_bps=None,
            observation_count=0,
            projected_participation=None,
            reason="NO_MICROSTRUCTURE_OBSERVATIONS",
        )
    _validate_sequence(observations)
    n = len(observations)
    participation = sum(row.requested_qty / row.adv_qty for row in observations) / n
    fill_ratio = sum(row.filled_qty / row.requested_qty for row in observations) / n
    spread_cost = sum(row.spread / row.mid * 10_000.0 for row in observations) / n
    impact = sum(abs(row.fill_price - row.mid) / row.mid * 10_000.0 for row in observations) / n
    # Spread and impact are separately reported; neither is reused as a hidden
    # linear coefficient for another scale.
    shortfall = sum(
        (abs(row.fill_price - row.mid) + row.spread / 2.0) / row.mid * 10_000.0
        for row in observations
    ) / n
    # The observations are fills at their own requested size.  Asking for
    # another requested size is a counterfactual: its participation is an
    # arithmetic projection of declared inputs, and the fill/impact statistics
    # published here remain measurements of the observed scale only.
    scaled, status, reason = _scenario_state(scenario.requested_multiplier)
    projected = participation * scenario.requested_multiplier if scaled else None
    return CapacityMeasurement(
        status=status,
        scale=scenario.scale,
        participation=participation,
        fill_ratio=fill_ratio,
        spread_cost_bps=spread_cost,
        impact_bps=impact,
        shortfall_bps=shortfall,
        observation_count=n,
        projected_participation=projected,
        reason=reason,
    )
