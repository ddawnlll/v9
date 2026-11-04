"""Incremental belief updates, transitions and abstention (M1-04).

The update contract is deliberately narrow:

* ``advance`` is **idempotent**: identical evidence at the same ``now_ns`` returns
  the identical belief (same ``version_hash``), and re-applying one observation
  never mints a second transition event.
* ``advance`` is **incremental**: a cheap dependency fingerprint (input ids,
  alignment state, quality, policy digest) is compared before any feature
  arithmetic runs. An irrelevant observation (a kind the belief does not
  consume) or a re-delivered identical snapshot does not recompute the view.
* ``advance`` **fails closed**: absent required inputs produce an ``ABSTAIN``
  belief naming what is missing, aged evidence produces ``STALE`` (never
  ``FRESH``), and a belief whose ``max_input_available_at`` is in the future of
  ``now_ns`` raises.
* Regime changes are **hysteretic**: a challenger must beat the incumbent's
  score by ``hysteresis_band`` *and* persist for ``min_candidate_streak``
  consecutive evidence sets. A suppressed challenger stays observable through
  ``regime_candidate``/``candidate_streak``; an accepted one mints one
  :class:`~v8_next.belief.state.TransitionEvidence` event.
* Thresholds are **versioned**: any deviation from the default thresholds with
  the default ``BELIEF_POLICY_VERSION`` is refused, so a threshold change cannot
  ship under an unchanged policy identity.

``v8_next.telemetry.belief`` is the ex-ante *receipt ledger* (identity, stages,
append-only receipts). It contains no market-state arithmetic, so there is
nothing to reuse here beyond the digest primitive; the belief schema also
carries none of its ``UNAVAILABLE_EX_ANTE_DIMENSIONS`` (no outcome probability,
no expected MFE/MAE), consistent with that module's invariant 1.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Final, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from v8_next.belief.state import (
    BPS_QUANTUM,
    FRESH_QUALITY,
    PRICE_QUANTUM,
    SCORE_QUANTUM,
    BeliefProvenance,
    BeliefStatus,
    BreakoutState,
    DataRole,
    HorizonConsistency,
    HorizonsView,
    LiquidityAvailability,
    LiquidityView,
    MarketBeliefV1,
    RegimeKind,
    RegimeScores,
    StateOrigin,
    StructureDirection,
    StructureView,
    SwingState,
    TransitionEvidence,
    TransitionReason,
    UncertaintyState,
    VolatilityChangeState,
    VolatilityLevel,
    VolatilityView,
    aggregate_quality,
    transition_event_id,
    verify_version_hash,
)
from v8_next.domain.horizons import HorizonAlignment, HorizonContext, Timeframe
from v8_next.domain.market import Candle
from v8_next.domain.observation import (
    Observation,
    ObservationKind,
    ObservationView,
    QualityState,
    observation_id,
    quality_severity,
)
from v8_next.telemetry.identity import canonical_digest

__all__ = [
    "BELIEF_POLICY_VERSION",
    "DEFAULT_POLICY",
    "FEATURE_VERSION",
    "MODEL_VERSION",
    "BeliefContractError",
    "BeliefPolicy",
    "BeliefPolicyError",
    "UpdateTrigger",
    "advance",
    "policy_digest",
    "update_trigger",
]

_NS_PER_HOUR: Final[int] = 3_600 * 1_000_000_000

#: Identity of the threshold set. Bumped whenever a threshold value changes;
#: :class:`BeliefPolicy` refuses a deviating threshold under this version.
BELIEF_POLICY_VERSION: Final[str] = "belief-policy/1"

#: Identity of the feature arithmetic (window statistics, quantisation).
FEATURE_VERSION: Final[str] = "market-belief-features/v1"

#: Identity of the regime scoring heuristic. Never a calibrated model id.
MODEL_VERSION: Final[str] = "regime-heuristic/v1"

#: Kinds the belief consumes. A snapshot carrying only other kinds (e.g.
#: ``LIQUIDATION``) is not relevant evidence and must not force a recompute.
_PROXY_KINDS: Final[tuple[ObservationKind, ...]] = (
    ObservationKind.MARK,
    ObservationKind.QUOTE,
    ObservationKind.OPEN_INTEREST,
    ObservationKind.FUNDING,
)

#: Deterministic tie order for equal scores (earlier wins).
_REGIME_PRIORITY: Final[tuple[RegimeKind, ...]] = (
    RegimeKind.SHOCK,
    RegimeKind.TREND,
    RegimeKind.TRANSITION,
    RegimeKind.RANGE,
)

#: Fields compared to populate ``changed_fields`` on an update.
_DIFF_FIELDS: Final[tuple[str, ...]] = (
    "policy_version",
    "quality",
    "regime",
    "regime_label",
    "structure",
    "volatility",
    "liquidity",
    "horizons",
    "status",
    "uncertainty",
    "origin",
)


class BeliefPolicyError(Exception):
    """A belief policy is internally inconsistent or unversioned.

    Deliberately not a ``ValueError``: pydantic wraps ``ValueError`` raised inside a
    validator into a generic ``ValidationError``, which would erase the policy
    discriminator. This type propagates with its ``code`` intact.
    """

    code = "BELIEF_POLICY_ERROR"


class BeliefContractError(ValueError):
    """The update contract was violated (PIT order, foreign type, instrument mix)."""

    code = "BELIEF_CONTRACT_ERROR"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        resolved = code or type(self).code
        super().__init__(f"{resolved}: {message}")
        self.code = resolved


class UpdateTrigger(StrEnum):
    """Why an update is needed; ``None`` means "identical evidence, nothing to do"."""

    #: No previous belief exists.
    INITIAL = "INITIAL"
    #: The dependency fingerprint changed: relevant observations or alignments moved.
    INPUT_CHANGED = "INPUT_CHANGED"
    #: Evidence did not change but aged past the staleness horizon.
    STALENESS_CROSSED = "STALENESS_CROSSED"
    #: The caller supplied a different threshold set.
    POLICY_CHANGED = "POLICY_CHANGED"


class BeliefPolicy(BaseModel):
    """Versioned thresholds. A changed threshold requires a changed ``version``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: str = BELIEF_POLICY_VERSION
    #: Bars in the structure/volatility window.
    window_bars: int = Field(default=20, ge=3)
    #: Bars required before any regime may be named.
    min_bars: int = Field(default=6, ge=3)
    #: Minimum best score for a regime to be named at all.
    regime_min_score: Decimal = Field(default=Decimal("0.35"), gt=0, le=1)
    #: A challenger must beat the incumbent by this much to be considered.
    hysteresis_band: Decimal = Field(default=Decimal("0.10"), gt=0, lt=1)
    #: Consecutive evidence sets a challenger must persist before it wins.
    min_candidate_streak: int = Field(default=2, ge=1)
    #: Evidence older than this at ``now_ns`` is STALE.
    staleness_horizon_ns: int = Field(default=2 * _NS_PER_HOUR, gt=0)
    #: Last-move / median-move ratio at which shock evidence saturates.
    shock_multiple: Decimal = Field(default=Decimal("3"), gt=1)
    volatility_low_bps: Decimal = Field(default=Decimal("20"), gt=0)
    volatility_high_bps: Decimal = Field(default=Decimal("100"), gt=0)
    #: Relative change in range that counts as rising/falling volatility.
    volatility_change_band: Decimal = Field(default=Decimal("0.25"), gt=0, lt=1)
    #: Net window move (bps) required to name a direction.
    structure_band_bps: Decimal = Field(default=Decimal("5"), gt=0)
    #: Position within the window range that counts as near-high/near-low.
    swing_band: Decimal = Field(default=Decimal("0.25"), gt=0, lt=Decimal("0.5"))
    #: Score margins below these values raise uncertainty.
    uncertainty_margin_low: Decimal = Field(default=Decimal("0.15"), gt=0, le=1)
    uncertainty_margin_high: Decimal = Field(default=Decimal("0.05"), gt=0, le=1)

    @model_validator(mode="after")
    def _check(self) -> Self:
        if not self.version.strip():
            raise BeliefPolicyError("policy version cannot be empty")
        if self.min_bars > self.window_bars:
            raise BeliefPolicyError("min_bars cannot exceed window_bars")
        if self.volatility_low_bps >= self.volatility_high_bps:
            raise BeliefPolicyError("volatility_low_bps must be below volatility_high_bps")
        if self.uncertainty_margin_high >= self.uncertainty_margin_low:
            raise BeliefPolicyError("uncertainty_margin_high must be below uncertainty_margin_low")
        thresholds = self._thresholds()
        if self.version == BELIEF_POLICY_VERSION and thresholds != _DEFAULT_THRESHOLDS:
            raise BeliefPolicyError(
                "thresholds differ from the defaults under the default policy version; "
                "bump 'version' when a threshold changes"
            )
        return self

    def _thresholds(self) -> dict[str, object]:
        return {
            name: getattr(self, name)
            for name in type(self).model_fields
            if name != "version"
        }


_DEFAULT_THRESHOLDS: Final[dict[str, object]] = {
    name: field.default
    for name, field in BeliefPolicy.model_fields.items()
    if name != "version"
}

DEFAULT_POLICY: Final[BeliefPolicy] = BeliefPolicy()


def policy_digest(policy: BeliefPolicy) -> str:
    """Deterministic digest of the threshold set, stored in every belief."""
    payload: dict[str, Any] = policy.model_dump(mode="json")
    payload.pop("version", None)
    return canonical_digest("belief_policy", payload)


def _strict_int(value: object, *, field: str) -> int:
    if isinstance(value, bool):
        raise BeliefContractError(f"{field} must be an integer", code="MALFORMED_INPUT")
    if isinstance(value, int):
        return int(value)
    index = getattr(value, "__index__", None)
    if index is None:
        raise BeliefContractError(f"{field} must be an integer", code="MALFORMED_INPUT")
    return int(index())


def _quantise(value: Decimal, quantum: Decimal) -> Decimal:
    return value.quantize(quantum)


def _clamp01(value: Decimal) -> Decimal:
    if value < Decimal(0):
        return Decimal(0)
    if value > Decimal(1):
        return Decimal(1)
    return value


@dataclass(frozen=True, slots=True)
class _Inputs:
    """Cheap, deterministic projection of the evidence (no feature arithmetic)."""

    instrument_id: str
    decision_time_ns: int
    bars: tuple[Candle, ...]
    proxies: Mapping[ObservationKind, Observation]
    observation_ids: tuple[str, ...]
    max_available_at: int
    at_decision_quality: QualityState


@dataclass(frozen=True, slots=True)
class _Dimensions:
    structure: StructureView
    volatility: VolatilityView
    liquidity: LiquidityView
    scores: RegimeScores
    candidate: RegimeKind
    candidate_score: Decimal


def _select_inputs(view: ObservationView, context: HorizonContext, policy: BeliefPolicy) -> _Inputs:
    """Project the view onto the inputs this belief version actually consumes.

    Bars must have closed and become available by ``decision_time_ns``; proxy
    channels contribute only their latest available observation. Observations
    available after the decision time are invisible here even when the view's
    cut is later, which is what makes a belief immune to future data.
    """
    instrument = context.instrument_id
    decision = context.decision_time_ns
    usable = tuple(
        observation
        for observation in view.observations(instrument)
        if observation.available_at <= decision
    )

    latest: dict[ObservationKind, Observation] = {}
    bar_by_start: dict[int, Observation] = {}
    for observation in usable:
        if observation.kind is ObservationKind.BAR:
            bar_by_start[observation.event_time] = observation
            continue
        if observation.kind not in _PROXY_KINDS:
            continue
        current = latest.get(observation.kind)
        if current is None or (observation.event_time, observation.sequence) >= (
            current.event_time,
            current.sequence,
        ):
            latest[observation.kind] = observation

    admissible = [
        bar
        for bar in view.bars(instrument)
        if bar.available_ns is not None
        and bar.available_ns <= decision
        and bar.end_ns <= decision
    ]
    bars = tuple(admissible[-policy.window_bars :])

    used: list[Observation] = []
    for bar in bars:
        backing = bar_by_start.get(bar.start_ns)
        if backing is not None:
            used.append(backing)
    used.extend(latest[kind] for kind in _PROXY_KINDS if kind in latest)

    max_available_at = max(
        [observation.available_at for observation in used]
        + [bar.available_ns for bar in bars if bar.available_ns is not None],
        default=0,
    )
    return _Inputs(
        instrument_id=instrument,
        decision_time_ns=decision,
        bars=bars,
        proxies=MappingProxyType(latest),
        observation_ids=tuple(sorted({observation_id(item) for item in used})),
        max_available_at=max_available_at,
        at_decision_quality=view.quality_at(instrument, decision),
    )


def _alignment_state(context: HorizonContext) -> list[list[object]]:
    return [
        [
            context.alignments[timeframe].timeframe.value,
            context.alignments[timeframe].as_of_ns,
            context.alignments[timeframe].complete,
            context.alignments[timeframe].quality.value,
        ]
        for timeframe in Timeframe
    ]


def _dependency_fingerprint(
    inputs: _Inputs, context: HorizonContext, policy: BeliefPolicy
) -> str:
    return canonical_digest(
        "market_belief_dependency",
        {
            "instrument_id": inputs.instrument_id,
            "decision_time_ns": inputs.decision_time_ns,
            "observation_ids": list(inputs.observation_ids),
            "quality": inputs.at_decision_quality.value,
            "alignments": _alignment_state(context),
            "policy_digest": policy_digest(policy),
        },
    )


def _decimal_from(payload: Mapping[str, Any], key: str) -> Decimal | None:
    """Read one numeric payload field; absent/non-finite/malformed yields ``None``."""
    if key not in payload:
        return None
    raw = payload[key]
    if raw is None or isinstance(raw, bool):
        return None
    try:
        value = Decimal(str(raw))
    except (InvalidOperation, ValueError, TypeError):
        return None
    if not value.is_finite():
        return None
    return value


def _liquidity(proxies: Mapping[ObservationKind, Observation]) -> LiquidityView:
    """Report liquidity proxies or their absence. Never fabricates a number."""
    missing: list[str] = []
    unreadable = False

    mark: Decimal | None = None
    mark_observation = proxies.get(ObservationKind.MARK)
    if mark_observation is None:
        missing.append("MARK")
    else:
        mark = _decimal_from(mark_observation.payload, "price")
        if mark is None:
            mark = _decimal_from(mark_observation.payload, "mark_price")
        if mark is None or mark <= 0:
            mark = None
            missing.append("MARK")
            unreadable = True

    spread: Decimal | None = None
    quote = proxies.get(ObservationKind.QUOTE)
    if quote is None:
        missing.append("QUOTE")
    else:
        bid = _decimal_from(quote.payload, "best_bid")
        ask = _decimal_from(quote.payload, "best_ask")
        if bid is None or ask is None or bid <= 0 or ask <= bid:
            missing.append("QUOTE")
            unreadable = True
        else:
            mid = (bid + ask) / Decimal(2)
            spread = _quantise((ask - bid) / mid * Decimal(10000), BPS_QUANTUM)

    open_interest: Decimal | None = None
    oi_observation = proxies.get(ObservationKind.OPEN_INTEREST)
    if oi_observation is None:
        missing.append("OPEN_INTEREST")
    else:
        open_interest = _decimal_from(oi_observation.payload, "open_interest")
        if open_interest is None or open_interest < 0:
            open_interest = None
            missing.append("OPEN_INTEREST")
            unreadable = True

    funding_rate: Decimal | None = None
    funding_observation = proxies.get(ObservationKind.FUNDING)
    if funding_observation is None:
        missing.append("FUNDING")
    else:
        funding_rate = _decimal_from(funding_observation.payload, "funding_rate")
        if funding_rate is None:
            missing.append("FUNDING")
            unreadable = True

    values = (mark, spread, open_interest, funding_rate)
    present = sum(1 for value in values if value is not None)
    if present == 0 and not unreadable:
        availability = LiquidityAvailability.MISSING
        quality = QualityState.MISSING
    elif unreadable:
        availability = LiquidityAvailability.UNKNOWN
        quality = QualityState.UNKNOWN
    elif present == len(values):
        availability = LiquidityAvailability.PRESENT
        quality = QualityState.OK
    else:
        availability = LiquidityAvailability.PARTIAL
        quality = QualityState.OK

    return LiquidityView(
        availability=availability,
        mark_price=mark,
        spread_bps=spread,
        open_interest=open_interest,
        funding_rate=funding_rate,
        missing_dimensions=tuple(sorted(missing)),
        quality=quality,
    )


def _structure(bars: tuple[Candle, ...], policy: BeliefPolicy) -> StructureView:
    if len(bars) < 2:
        return StructureView(
            direction=StructureDirection.UNKNOWN,
            swing_state=SwingState.UNKNOWN,
            breakout_state=BreakoutState.UNKNOWN,
            quality=QualityState.MISSING,
        )

    closes = [bar.close for bar in bars]
    highs = [bar.high for bar in bars]
    lows = [bar.low for bar in bars]
    first_close = closes[0]
    last_close = closes[-1]
    window_low = min(lows)
    window_high = max(highs)
    net_bps = (last_close - first_close) / first_close * Decimal(10000)

    if net_bps > policy.structure_band_bps:
        direction = StructureDirection.UP
    elif net_bps < -policy.structure_band_bps:
        direction = StructureDirection.DOWN
    else:
        direction = StructureDirection.FLAT

    span = window_high - window_low
    if span <= 0:
        swing = SwingState.UNKNOWN
    else:
        position = (last_close - window_low) / span
        if position >= Decimal(1) - policy.swing_band:
            swing = SwingState.NEAR_HIGH
        elif position <= policy.swing_band:
            swing = SwingState.NEAR_LOW
        else:
            swing = SwingState.MIDDLE

    prior = bars[:-1]
    if last_close > max(bar.high for bar in prior):
        breakout = BreakoutState.UP
    elif last_close < min(bar.low for bar in prior):
        breakout = BreakoutState.DOWN
    else:
        breakout = BreakoutState.NONE

    return StructureView(
        direction=direction,
        swing_state=swing,
        breakout_state=breakout,
        window_low=_quantise(window_low, PRICE_QUANTUM),
        window_high=_quantise(window_high, PRICE_QUANTUM),
        reference_price=_quantise(last_close, PRICE_QUANTUM),
        quality=QualityState.OK,
    )


def _volatility(bars: tuple[Candle, ...], policy: BeliefPolicy) -> VolatilityView:
    if len(bars) < 2:
        return VolatilityView(
            level=VolatilityLevel.UNKNOWN,
            change_state=VolatilityChangeState.UNKNOWN,
            quality=QualityState.MISSING,
        )

    ranges = [(bar.high - bar.low) / bar.close * Decimal(10000) for bar in bars]
    mean_bps = _quantise(sum(ranges, Decimal(0)) / Decimal(len(ranges)), BPS_QUANTUM)
    if mean_bps < policy.volatility_low_bps:
        level = VolatilityLevel.LOW
    elif mean_bps > policy.volatility_high_bps:
        level = VolatilityLevel.HIGH
    else:
        level = VolatilityLevel.NORMAL

    last_range = ranges[-1]
    at_or_below = sum(1 for value in ranges if value <= last_range)
    percentile = _quantise(Decimal(at_or_below) / Decimal(len(ranges)), SCORE_QUANTUM)

    change = VolatilityChangeState.UNKNOWN
    if len(ranges) >= 4:
        half = len(ranges) // 2
        first = sum(ranges[:half], Decimal(0)) / Decimal(half)
        second = sum(ranges[half:], Decimal(0)) / Decimal(len(ranges) - half)
        if first > 0:
            relative = (second - first) / first
            if relative > policy.volatility_change_band:
                change = VolatilityChangeState.RISING
            elif relative < -policy.volatility_change_band:
                change = VolatilityChangeState.FALLING
            else:
                change = VolatilityChangeState.FLAT

    return VolatilityView(
        level=level,
        range_bps=mean_bps,
        range_percentile=percentile,
        change_state=change,
        quality=QualityState.OK,
    )


def _regime_scores(
    bars: tuple[Candle, ...], policy: BeliefPolicy
) -> tuple[RegimeScores, RegimeKind, Decimal]:
    """Bounded heuristic scores. Documented as scores, never as probabilities."""
    if len(bars) < policy.min_bars:
        return (
            RegimeScores(
                trend=Decimal(0),
                range=Decimal(0),
                transition=Decimal(0),
                shock=Decimal(0),
                unknown=Decimal(1),
            ),
            RegimeKind.UNKNOWN,
            Decimal(0),
        )

    closes = [bar.close for bar in bars]
    deltas = [closes[index + 1] - closes[index] for index in range(len(closes) - 1)]
    path = sum((abs(delta) for delta in deltas), Decimal(0))
    net = abs(closes[-1] - closes[0])
    efficiency = Decimal(0) if path == 0 else net / path
    trend = _quantise(_clamp01(efficiency), SCORE_QUANTUM)
    range_score = _quantise(_clamp01(Decimal(1) - efficiency), SCORE_QUANTUM)

    moves = sorted(abs(delta) for delta in deltas)
    median = moves[len(moves) // 2]
    shock = Decimal(0)
    if median > 0:
        span = policy.shock_multiple - Decimal(1)
        if span > 0:
            ratio = abs(deltas[-1]) / median
            shock = _quantise(_clamp01((ratio - Decimal(1)) / span), SCORE_QUANTUM)

    tail = deltas[-5:]
    signs = [0 if delta == 0 else (1 if delta > 0 else -1) for delta in tail]
    pairs = [(signs[index - 1], signs[index]) for index in range(1, len(signs))]
    flips = sum(1 for left, right in pairs if left != 0 and right != 0 and left != right)
    transition = _quantise(Decimal(flips) / Decimal(max(1, len(pairs))), SCORE_QUANTUM)

    scores = RegimeScores(
        trend=trend,
        range=range_score,
        transition=transition,
        shock=shock,
        unknown=Decimal(0),
    )
    best = max(
        _REGIME_PRIORITY, key=lambda kind: (scores.score(kind), -_REGIME_PRIORITY.index(kind))
    )
    best_score = scores.score(best)
    if best_score < policy.regime_min_score:
        return scores, RegimeKind.UNKNOWN, Decimal(0)
    return scores, best, best_score


def _features(inputs: _Inputs, policy: BeliefPolicy) -> _Dimensions:
    scores, candidate, candidate_score = _regime_scores(inputs.bars, policy)
    return _Dimensions(
        structure=_structure(inputs.bars, policy),
        volatility=_volatility(inputs.bars, policy),
        liquidity=_liquidity(inputs.proxies),
        scores=scores,
        candidate=candidate,
        candidate_score=candidate_score,
    )


def _unknown_dimensions(proxies: Mapping[ObservationKind, Observation]) -> _Dimensions:
    return _Dimensions(
        structure=StructureView(
            direction=StructureDirection.UNKNOWN,
            swing_state=SwingState.UNKNOWN,
            breakout_state=BreakoutState.UNKNOWN,
            quality=QualityState.MISSING,
        ),
        volatility=VolatilityView(
            level=VolatilityLevel.UNKNOWN,
            change_state=VolatilityChangeState.UNKNOWN,
            quality=QualityState.MISSING,
        ),
        liquidity=_liquidity(proxies),
        scores=RegimeScores(
            trend=Decimal(0),
            range=Decimal(0),
            transition=Decimal(0),
            shock=Decimal(0),
            unknown=Decimal(1),
        ),
        candidate=RegimeKind.UNKNOWN,
        candidate_score=Decimal(0),
    )


def _horizons_view(alignments: tuple[HorizonAlignment, ...]) -> HorizonsView:
    complete_count = sum(1 for alignment in alignments if alignment.complete)
    worst = aggregate_quality(tuple(alignment.quality for alignment in alignments))
    if worst is QualityState.UNKNOWN:
        consistency = HorizonConsistency.UNKNOWN
    elif complete_count == 0:
        consistency = HorizonConsistency.MISSING
    elif all(
        alignment.complete and alignment.quality is QualityState.OK for alignment in alignments
    ):
        consistency = HorizonConsistency.ALIGNED
    else:
        consistency = HorizonConsistency.DEGRADED
    return HorizonsView(
        alignments=alignments,
        consistency=consistency,
        complete_count=complete_count,
        as_of_max_ns=max(alignment.as_of_ns for alignment in alignments),
    )


def _uncertainty(
    status: BeliefStatus,
    quality: QualityState,
    scores: RegimeScores,
    liquidity: LiquidityView,
    horizons: HorizonsView,
    policy: BeliefPolicy,
) -> UncertaintyState:
    if status is not BeliefStatus.FRESH:
        return UncertaintyState.UNKNOWN
    if quality is QualityState.UNKNOWN:
        return UncertaintyState.UNKNOWN
    ranked = sorted((scores.score(kind) for kind in _REGIME_PRIORITY), reverse=True)
    margin = ranked[0] - ranked[1] if len(ranked) > 1 else Decimal(0)
    if (
        quality_severity(quality) >= quality_severity(QualityState.STALE)
        or margin < policy.uncertainty_margin_high
    ):
        return UncertaintyState.HIGH
    if margin < policy.uncertainty_margin_low or horizons.consistency is not HorizonConsistency.ALIGNED:
        return UncertaintyState.MEDIUM
    if liquidity.availability is not LiquidityAvailability.PRESENT:
        return UncertaintyState.MEDIUM
    return UncertaintyState.LOW


def _changed_fields(previous: MarketBeliefV1, values: Mapping[str, Any]) -> tuple[str, ...]:
    old = previous.model_dump()
    changed: list[str] = []
    for name in _DIFF_FIELDS:
        if name not in values:
            continue
        value = values[name]
        new = value.model_dump() if isinstance(value, BaseModel) else value
        if old.get(name) != new:
            changed.append(name)
    return tuple(sorted(changed))


def _resolve_regime(
    previous: MarketBeliefV1 | None,
    dimensions: _Dimensions,
    inputs: _Inputs,
    policy: BeliefPolicy,
) -> tuple[RegimeKind, RegimeKind, int, TransitionEvidence | None]:
    candidate = dimensions.candidate
    candidate_score = dimensions.candidate_score
    scores = dimensions.scores

    if previous is None or previous.policy_version != policy.version:
        if candidate is RegimeKind.UNKNOWN:
            return RegimeKind.UNKNOWN, RegimeKind.UNKNOWN, 0, None
        return candidate, candidate, 0, None

    incumbent = previous.regime_label
    if candidate is incumbent:
        return incumbent, incumbent, 0, None

    streak = (
        previous.candidate_streak + 1 if previous.regime_candidate is candidate else 1
    )
    if incumbent is RegimeKind.UNKNOWN:
        incumbent_score = Decimal(0)
        reason = TransitionReason.RECOVERY_FROM_UNKNOWN
        accepted = candidate is not RegimeKind.UNKNOWN and candidate_score >= policy.regime_min_score
    else:
        incumbent_score = scores.score(incumbent)
        reason = TransitionReason.SCORE_BAND_EXCEEDED
        accepted = (
            candidate is not RegimeKind.UNKNOWN
            and candidate_score >= incumbent_score + policy.hysteresis_band
        )
    if not accepted or streak < policy.min_candidate_streak:
        return incumbent, candidate, streak, None

    trigger_ids = tuple(
        sorted(set(inputs.observation_ids) - set(previous.input_observation_ids))
    )
    event_id = transition_event_id(
        previous_state_id=previous.version_hash,
        from_regime=incumbent,
        to_regime=candidate,
        reason=reason,
        hysteresis_band=policy.hysteresis_band,
        candidate_score=candidate_score,
        incumbent_score=incumbent_score,
        trigger_observation_ids=trigger_ids,
        occurred_at_ns=inputs.decision_time_ns,
    )
    transition = TransitionEvidence(
        event_id=event_id,
        previous_state_id=previous.version_hash,
        from_regime=incumbent,
        to_regime=candidate,
        reason=reason,
        hysteresis_band=policy.hysteresis_band,
        candidate_score=candidate_score,
        incumbent_score=incumbent_score,
        trigger_observation_ids=trigger_ids,
        occurred_at_ns=inputs.decision_time_ns,
    )
    return candidate, candidate, 0, transition


def _source_hash(inputs: _Inputs, context: HorizonContext) -> str:
    return canonical_digest(
        "market_belief_source",
        {
            "observation_ids": list(inputs.observation_ids),
            "max_input_available_at": inputs.max_available_at,
            "decision_time_ns": inputs.decision_time_ns,
            "alignments": _alignment_state(context),
        },
    )


def _provenance(inputs: _Inputs, context: HorizonContext, policy: BeliefPolicy) -> BeliefProvenance:
    return BeliefProvenance(
        source_hash=_source_hash(inputs, context),
        feature_version=FEATURE_VERSION,
        model_version=MODEL_VERSION,
        policy_version=policy.version,
        policy_digest=policy_digest(policy),
        data_role=DataRole.OBSERVED,
    )


def _aged(max_available_at: int, now_ns: int, policy: BeliefPolicy) -> bool:
    return now_ns > max_available_at + policy.staleness_horizon_ns


def _as_stale(previous: MarketBeliefV1) -> MarketBeliefV1:
    """Degrade a belief to ``STALE`` without inventing or erasing evidence."""
    if previous.status in (BeliefStatus.STALE, BeliefStatus.ABSTAIN):
        return previous
    quality = aggregate_quality((previous.quality, QualityState.STALE))
    uncertainty = (
        UncertaintyState.UNKNOWN
        if previous.uncertainty is UncertaintyState.UNKNOWN
        else UncertaintyState.HIGH
    )
    values: dict[str, Any] = {
        "status": BeliefStatus.STALE,
        "origin": StateOrigin.STALE,
        "quality": quality,
        "uncertainty": uncertainty,
    }
    changed = _changed_fields(previous, values)
    data = previous.model_dump()
    data.update(
        status=BeliefStatus.STALE,
        origin=StateOrigin.STALE,
        quality=quality,
        uncertainty=uncertainty,
        transition=None,
        changed_fields=changed,
        version_hash="",
    )
    return MarketBeliefV1.model_validate(data)


@dataclass(frozen=True, slots=True)
class _Assessment:
    inputs: _Inputs
    fingerprint: str
    trigger: UpdateTrigger | None


def _assess(
    previous: MarketBeliefV1 | None,
    view: ObservationView,
    context: HorizonContext,
    now_ns: int,
    policy: BeliefPolicy,
) -> _Assessment:
    if not isinstance(view, ObservationView):
        raise TypeError(f"expected ObservationView, got {type(view).__name__}")
    if not isinstance(context, HorizonContext):
        raise TypeError(f"expected HorizonContext, got {type(context).__name__}")
    if previous is not None:
        if not isinstance(previous, MarketBeliefV1):
            raise TypeError(f"expected MarketBeliefV1, got {type(previous).__name__}")
        verify_version_hash(previous)
        if previous.instrument_id != context.instrument_id:
            raise BeliefContractError(
                f"belief is for {previous.instrument_id}, context for {context.instrument_id}",
                code="INSTRUMENT_MISMATCH",
            )
        if now_ns < previous.max_input_available_at:
            raise BeliefContractError(
                f"now_ns {now_ns} precedes the previous belief's inputs "
                f"({previous.max_input_available_at}); evidence cannot be re-read backwards",
                code="NOW_BEFORE_INPUT",
            )
    inputs = _select_inputs(view, context, policy)
    if now_ns < inputs.max_available_at:
        raise BeliefContractError(
            f"now_ns {now_ns} precedes input availability {inputs.max_available_at}",
            code="NOW_BEFORE_INPUT",
        )
    fingerprint = _dependency_fingerprint(inputs, context, policy)
    if previous is None:
        return _Assessment(inputs, fingerprint, UpdateTrigger.INITIAL)
    if previous.policy_version != policy.version:
        return _Assessment(inputs, fingerprint, UpdateTrigger.POLICY_CHANGED)
    if previous.dependency_fingerprint != fingerprint:
        return _Assessment(inputs, fingerprint, UpdateTrigger.INPUT_CHANGED)
    if _aged(inputs.max_available_at, now_ns, policy) and previous.status not in (
        BeliefStatus.STALE,
        BeliefStatus.ABSTAIN,
    ):
        return _Assessment(inputs, fingerprint, UpdateTrigger.STALENESS_CROSSED)
    return _Assessment(inputs, fingerprint, None)


def update_trigger(
    previous: MarketBeliefV1 | None,
    view: ObservationView,
    context: HorizonContext,
    *,
    now_ns: int,
    policy: BeliefPolicy | None = None,
) -> UpdateTrigger | None:
    """Report whether an update is needed without computing any feature.

    ``None`` means the evidence and its freshness are unchanged: ``advance``
    would return ``previous`` unchanged.
    """
    resolved = DEFAULT_POLICY if policy is None else policy
    now = _strict_int(now_ns, field="now_ns")
    return _assess(previous, view, context, now, resolved).trigger


def advance(
    previous: MarketBeliefV1 | None,
    view: ObservationView,
    context: HorizonContext,
    *,
    now_ns: int,
    policy: BeliefPolicy | None = None,
) -> MarketBeliefV1:
    """Advance the belief by one evidence snapshot.

    Idempotent for identical evidence, stale after ``staleness_horizon_ns``
    without new evidence, abstaining when required inputs are absent, and
    hysteretic on regime changes. See the module docstring for the contract.
    """
    resolved = DEFAULT_POLICY if policy is None else policy
    if not isinstance(resolved, BeliefPolicy):
        raise TypeError(f"expected BeliefPolicy, got {type(resolved).__name__}")
    now = _strict_int(now_ns, field="now_ns")
    assessment = _assess(previous, view, context, now, resolved)
    if assessment.trigger is None:
        if previous is None:
            raise BeliefContractError("no previous belief to reuse", code="MISSING_PREVIOUS")
        return previous
    if assessment.trigger is UpdateTrigger.STALENESS_CROSSED:
        if previous is None:
            raise BeliefContractError("no previous belief to degrade", code="MISSING_PREVIOUS")
        return _as_stale(previous)

    inputs = assessment.inputs
    alignments = tuple(context.alignments[timeframe] for timeframe in Timeframe)
    horizons = _horizons_view(alignments)
    # Only horizons that produced a finished bar contribute evidence quality;
    # an absent horizon is reported through ``horizons.consistency`` and, when no
    # horizon is complete at all, through ``missing_inputs``/ABSTAIN. Including an
    # absent horizon here would turn every single-timeframe cut into UNKNOWN.
    evidence_quality = aggregate_quality(
        (
            inputs.at_decision_quality,
            *(alignment.quality for alignment in alignments if alignment.complete),
        )
    )

    missing: list[str] = []
    if len(inputs.bars) < resolved.min_bars:
        missing.append("bars")
    if not any(alignment.complete for alignment in alignments):
        missing.append("horizon")

    stale = _aged(inputs.max_available_at, now, resolved)
    previous_for_transition = (
        previous if previous is not None and previous.policy_version == resolved.version else None
    )

    if missing:
        dimensions = _unknown_dimensions(inputs.proxies)
        label = RegimeKind.UNKNOWN
        candidate = RegimeKind.UNKNOWN
        streak = 0
        transition: TransitionEvidence | None = None
        status = BeliefStatus.ABSTAIN
        origin = StateOrigin.MISSING_INPUT
        quality = aggregate_quality((evidence_quality, QualityState.MISSING))
    else:
        dimensions = _features(inputs, resolved)
        label, candidate, streak, transition = _resolve_regime(
            previous_for_transition, dimensions, inputs, resolved
        )
        quality = evidence_quality
        if stale:
            quality = aggregate_quality((quality, QualityState.STALE))
            status = BeliefStatus.STALE
            origin = StateOrigin.STALE
        elif quality not in FRESH_QUALITY:
            status = BeliefStatus.UNKNOWN
            origin = StateOrigin.UPDATED
        else:
            status = BeliefStatus.FRESH
            origin = StateOrigin.INITIAL if previous is None else StateOrigin.UPDATED
        if transition is not None and status is not BeliefStatus.FRESH:
            transition = None
        if status is BeliefStatus.STALE and previous_for_transition is not None:
            # Aged evidence cannot affirm a regime change: keep the incumbent
            # label so a stale belief never silently adopts a new regime. The
            # challenger stays visible through regime_candidate/candidate_streak.
            label = previous_for_transition.regime_label

    if missing:
        uncertainty = UncertaintyState.UNKNOWN
    else:
        uncertainty = _uncertainty(
            status, quality, dimensions.scores, dimensions.liquidity, horizons, resolved
        )

    values: dict[str, Any] = {
        "policy_version": resolved.version,
        "status": status,
        "origin": origin,
        "quality": quality,
        "regime": dimensions.scores,
        "regime_label": label,
        "structure": dimensions.structure,
        "volatility": dimensions.volatility,
        "liquidity": dimensions.liquidity,
        "horizons": horizons,
        "uncertainty": uncertainty,
    }
    changed = () if previous is None else _changed_fields(previous, values)

    return MarketBeliefV1(
        instrument_id=inputs.instrument_id,
        decision_time_ns=inputs.decision_time_ns,
        max_input_available_at=inputs.max_available_at,
        input_observation_ids=inputs.observation_ids,
        dependency_fingerprint=assessment.fingerprint,
        policy_version=resolved.version,
        status=status,
        origin=origin,
        quality=quality,
        regime_label=label,
        regime=dimensions.scores,
        regime_candidate=candidate,
        candidate_streak=streak,
        structure=dimensions.structure,
        volatility=dimensions.volatility,
        liquidity=dimensions.liquidity,
        horizons=horizons,
        uncertainty=uncertainty,
        transition=transition,
        changed_fields=changed,
        missing_inputs=tuple(sorted(missing)),
        provenance=_provenance(inputs, context, resolved),
        version_hash="",
    )
