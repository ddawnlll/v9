"""P1 — matched random-entry control arms, regime split and null comparison.

A treatment arm is only interpretable against the *distribution* of what a
matched control would have produced, not against one arbitrary draw. This module
owns three things and nothing else:

* :func:`random_entry_controls` builds ``RANDOM_ENTRY_MATCHED_EXITS`` arms from a
  pool of measured random-entry outcomes. Every draw selects exactly the
  treatment's campaign budget from that pool with a private
  ``random.Random(seed)``; the seed is published in the result and the same seed
  reproduces the same draws. No module-level RNG is touched, so a caller's RNG
  state cannot leak into the control distribution.
* :func:`label_regimes` labels bars into ``TREND_UP`` / ``TREND_DOWN`` /
  ``RANGE`` / ``HIGH_VOL`` using only data at or before each bar (point-in-time).
  Bars without a full trailing window stay ``UNLABELLED`` rather than defaulting
  into a bucket.
* :func:`compare_to_controls` and :func:`compare_by_regime` place the treatment
  inside the empirical null and refuse to claim superiority when it sits inside
  the control spread.

Matching is enforced, not described: the control battery carries a
:class:`MatchSpec` (calendar, capital, venue, bracket, cost model, opportunity
exposure rule) that must equal the treatment's, and every arm's
:class:`~v8_next.evaluation.ablations.Provenance` is checked with the same
tape/engine/execution-profile guard ``compute_delta`` uses. A mismatch is a typed
refusal, never a silently widened comparison.

Nothing here submits an order, reads sealed data, or fabricates a number: inputs
are measurements supplied by the caller, and an absent treatment is ``NOT_RUN``
with its reason.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from statistics import median, pstdev
from typing import Any, Mapping, Sequence

from v8_next.evaluation.ablations import (
    BOOTSTRAP_RESAMPLES,
    ArmResult,
    Provenance,
    _require_comparable_provenance,
    bootstrap_mean_ci,
    sha256_json,
)
from v8_next.evaluation.preregistration import ArmKind

SCHEMA_VERSION = "v9.control_arms.1"

#: Default seed for the control generator; recorded on every battery.
DEFAULT_SEED = 20251105
#: How many independent draws of the null each battery carries.
DEFAULT_DRAWS = 50
#: Below this many draws the null distribution cannot evidence an edge.
MIN_DRAWS = 20
#: Below this many treatment campaigns the comparison is uninformative.
MIN_CAMPAIGNS = 30
#: One-sided level at which an edge may be claimed.
DEFAULT_ALPHA = 0.05

CONTROL_ARM_KIND = ArmKind.RANDOM_ENTRY_MATCHED_EXITS.value

#: Why a comparison could not be made or did not reach an edge.
GATE_TREATMENT_NOT_MEASURED = "TREATMENT_NOT_MEASURED"
GATE_CONTROL_ARMS_ABSENT = "CONTROL_ARMS_ABSENT"
GATE_NO_CONTROL_DRAWS_IN_BUCKET = "NO_CONTROL_DRAWS_IN_BUCKET"
GATE_TOO_FEW_CAMPAIGNS = "TOO_FEW_CAMPAIGNS"
GATE_TOO_FEW_DRAWS = "TOO_FEW_DRAWS"
GATE_P_RESOLUTION_INSUFFICIENT = "P_RESOLUTION_INSUFFICIENT"
GATE_INSIDE_CONTROL_SPREAD = "INSIDE_CONTROL_SPREAD"
GATE_P_ABOVE_ALPHA = "P_ABOVE_ALPHA"
GATE_NONPOSITIVE_DELTA = "NONPOSITIVE_DELTA"
GATE_OUTSIDE_CONTROL_DISTRIBUTION = "OUTSIDE_CONTROL_DISTRIBUTION"

MATCH_FIELDS = (
    "calendar",
    "capital",
    "venue",
    "stop_bps",
    "target_bps",
    "fee_bps_per_side",
    "funding_bps_per_day",
    "slippage_bps_per_side",
    "opportunity_exposure",
)


class ControlArmRefusal(ValueError):
    """A control battery was refused; nothing was compared.

    ``code`` is a stable machine token and ``field``/``fields`` name the fact
    that failed so a caller fixes the input rather than the symptom.
    """

    code = "CONTROL_ARM_REFUSED"

    def __init__(
        self,
        message: str,
        *,
        field: str,
        fields: tuple[str, ...] | None = None,
        code: str | None = None,
    ) -> None:
        resolved = code or type(self).code
        super().__init__(f"{resolved}: {message}")
        self.code = resolved
        self.message = message
        self.field = field
        self.fields = fields or (field,)


class ControlArmMismatch(ControlArmRefusal):
    """A control arm was not matched to the treatment it is compared against."""

    code = "CONTROL_ARM_MISMATCH"


class ControlVerdict(StrEnum):
    """Outcome of a comparison against the control distribution.

    ``PASS`` is the only affirmative outcome and is gated on the treatment lying
    outside the control spread at ``alpha``. ``NOT_RUN`` means no honest
    comparison was possible; it is never a soft pass.
    """

    PASS = "PASS"
    NO_EDGE = "NO_EDGE"
    INCONCLUSIVE = "INCONCLUSIVE"
    NOT_RUN = "NOT_RUN"


# --------------------------------------------------------------------------- #
# Matched dimensions
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class MatchSpec:
    """The dimensions a control arm shares with its treatment arm.

    Two arms with different specs are not comparable: the difference would
    conflate the intervention with a different window, capital, venue, bracket or
    cost model. ``stop_bps``/``target_bps`` are the bracket in basis points of
    entry price; the three cost terms are basis points as charged by the replay
    that produced the outcomes.
    """

    calendar: str
    capital: Decimal
    venue: str
    stop_bps: Decimal
    target_bps: Decimal
    fee_bps_per_side: Decimal
    funding_bps_per_day: Decimal
    slippage_bps_per_side: Decimal
    opportunity_exposure: str

    def __post_init__(self) -> None:
        if not self.calendar.strip():
            raise ControlArmRefusal("calendar window must be named", field="calendar")
        if self.capital <= 0:
            raise ControlArmRefusal(
                f"capital must be positive, got {self.capital}", field="capital"
            )
        if not self.venue.strip():
            raise ControlArmRefusal("venue must be named", field="venue")
        if self.stop_bps <= 0:
            raise ControlArmRefusal(f"stop must be positive, got {self.stop_bps}", field="stop_bps")
        if self.target_bps <= 0:
            raise ControlArmRefusal(
                f"target must be positive, got {self.target_bps}", field="target_bps"
            )
        for name, value in (
            ("fee_bps_per_side", self.fee_bps_per_side),
            ("funding_bps_per_day", self.funding_bps_per_day),
            ("slippage_bps_per_side", self.slippage_bps_per_side),
        ):
            if value < 0:
                raise ControlArmRefusal(f"{name} must be non-negative, got {value}", field=name)
        if not self.opportunity_exposure.strip():
            raise ControlArmRefusal(
                "opportunity exposure rule must be named", field="opportunity_exposure"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "calendar": self.calendar,
            "capital": str(self.capital),
            "venue": self.venue,
            "stop_bps": str(self.stop_bps),
            "target_bps": str(self.target_bps),
            "fee_bps_per_side": str(self.fee_bps_per_side),
            "funding_bps_per_day": str(self.funding_bps_per_day),
            "slippage_bps_per_side": str(self.slippage_bps_per_side),
            "opportunity_exposure": self.opportunity_exposure,
        }

    @property
    def digest(self) -> str:
        """Stable sha256 of the matched dimensions."""
        return sha256_json(self.to_dict())


def spec_mismatches(left: MatchSpec, right: MatchSpec) -> tuple[str, ...]:
    """Names of the matched dimensions on which two specs differ."""
    return tuple(name for name in MATCH_FIELDS if getattr(left, name) != getattr(right, name))


def require_specs_match(treatment: MatchSpec, control: MatchSpec) -> None:
    """Refuse a comparison whose two arms were not matched dimension by dimension."""
    mismatched = spec_mismatches(treatment, control)
    if mismatched:
        raise ControlArmMismatch(
            "control arms must share the treatment's calendar, capital, venue, "
            f"bracket, cost model and exposure rule; mismatched: {', '.join(mismatched)}",
            field=mismatched[0],
            fields=mismatched,
        )


# --------------------------------------------------------------------------- #
# Measured random-entry outcomes
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class OpportunityOutcome:
    """One measured random entry with the treatment's matched bracket and costs.

    ``net_r`` is the campaign result in R multiples as produced by the replay
    that priced this entry; ``bar_index`` is the entry bar so the same outcome can
    be attributed to a point-in-time regime. This module never derives ``net_r``;
    an unmeasured outcome simply does not exist.
    """

    opportunity_id: str
    bar_index: int
    net_r: float

    def __post_init__(self) -> None:
        if not self.opportunity_id.strip():
            raise ControlArmRefusal("opportunity id must be named", field="opportunity_id")
        if self.bar_index < 0:
            raise ControlArmRefusal(
                f"bar index must be non-negative, got {self.bar_index}", field="bar_index"
            )
        if not math.isfinite(self.net_r):
            raise ControlArmRefusal(f"net_r must be finite, got {self.net_r}", field="net_r")

    def to_dict(self) -> dict[str, Any]:
        return {
            "opportunity_id": self.opportunity_id,
            "bar_index": self.bar_index,
            "net_r": _round(self.net_r),
        }


def _round(value: float | None, digits: int = 10) -> float | None:
    if value is None:
        return None
    return round(float(value), digits)


def _mean(values: Sequence[float]) -> float | None:
    if not values:
        return None
    return float(sum(values) / len(values))


def _sorted(values: Sequence[float]) -> list[float]:
    return sorted(float(value) for value in values)


def _quantile(sorted_values: Sequence[float], q: float) -> float | None:
    """Linear-interpolation quantile, matching the rule used by ``ablations``."""
    if not sorted_values:
        return None
    if len(sorted_values) == 1:
        return float(sorted_values[0])
    position = q * (len(sorted_values) - 1)
    lower = int(position)
    upper = min(lower + 1, len(sorted_values) - 1)
    weight = position - lower
    return float(sorted_values[lower]) * (1.0 - weight) + float(sorted_values[upper]) * weight


def _risk_basis(spec: MatchSpec) -> str:
    return (
        f"BRACKET_STOP_BPS={spec.stop_bps} TARGET_BPS={spec.target_bps} "
        f"CAPITAL={spec.capital} VENUE={spec.venue} CALENDAR={spec.calendar}"
    )


def _ci_method(resamples: int, seed: int) -> str:
    return (
        "BOOTSTRAP_PERCENTILE_95_OF_MEAN "
        f"(resamples={resamples}, seed={seed}, unit=per-campaign net R)"
    )


def _draw_arm(
    *,
    arm_id: str,
    selection: Sequence[OpportunityOutcome],
    spec: MatchSpec,
    provenance: Provenance,
    resamples: int,
    ci_seed: int,
) -> ArmResult:
    """One control arm from one draw of the opportunity pool."""
    samples = tuple(outcome.net_r for outcome in selection)
    low, high = bootstrap_mean_ci(samples, resamples=resamples, seed=ci_seed)
    return ArmResult(
        arm_id=arm_id,
        intervention=None,
        costed=True,
        trades=len(samples),
        net_r=_mean(samples),
        net_pnl=None,
        max_drawdown=None,
        hit_rate=(
            None if not samples else sum(1 for value in samples if value > 0.0) / len(samples)
        ),
        fee_total=None,
        funding_total=None,
        ci_low=low,
        ci_high=high,
        ci_method=_ci_method(resamples, ci_seed),
        provenance=provenance,
        net_r_samples=samples,
        risk_basis=_risk_basis(spec),
        open_campaigns=0,
        arm_kind=CONTROL_ARM_KIND,
    )


@dataclass(frozen=True)
class ControlBattery:
    """``draws`` independent random-entry arms plus the identity of the null.

    ``selections`` is the per-draw opportunity set, so the distribution can be
    split by anything the caller can attribute to an opportunity (a regime, a
    session) without regenerating draws.
    """

    spec: MatchSpec
    seed: int
    draws: int
    #: Campaigns selected per draw; ``None`` for a regime subset, where the
    #: per-arm exposure budget is not the treatment's budget.
    exposure_budget: int | None
    provenance: Provenance
    resamples: int
    arms: tuple[ArmResult, ...]
    selections: tuple[tuple[OpportunityOutcome, ...], ...]
    arm_kind: str = CONTROL_ARM_KIND

    @property
    def distribution(self) -> tuple[float, ...]:
        """Per-draw arm net R — the empirical null the treatment is placed in."""
        return tuple(arm.net_r for arm in self.arms if arm.net_r is not None)

    def quantiles(self) -> tuple[float | None, float | None, float | None]:
        """5th, 50th and 95th percentile of the control distribution."""
        ordered = _sorted(self.distribution)
        return (
            _quantile(ordered, 0.05),
            _quantile(ordered, 0.50),
            _quantile(ordered, 0.95),
        )

    def to_dict(self) -> dict[str, Any]:
        q05, q50, q95 = self.quantiles()
        return {
            "schema_version": SCHEMA_VERSION,
            "arm_kind": self.arm_kind,
            "spec": self.spec.to_dict(),
            "spec_digest": self.spec.digest,
            "seed": self.seed,
            "draws": self.draws,
            "exposure_budget": self.exposure_budget,
            "resamples": self.resamples,
            "provenance": self.provenance.to_dict(),
            "distribution": [_round(value) for value in self.distribution],
            "control_mean": _round(_mean(self.distribution)),
            "control_q05": _round(q05),
            "control_q50": _round(q50),
            "control_q95": _round(q95),
            "arm_ids": [arm.arm_id for arm in self.arms],
        }

    def subset(self, labels: Mapping[int, "Regime"], regime: "Regime") -> ControlBattery:
        """The same draws restricted to entries whose bar carries ``regime``.

        Draws with no entry in the bucket are dropped: an empty draw carries no
        information about that bucket and must not be read as a zero.
        """
        selections: list[tuple[OpportunityOutcome, ...]] = []
        arms: list[ArmResult] = []
        for index, selection in enumerate(self.selections):
            kept = tuple(
                outcome for outcome in selection if labels.get(outcome.bar_index) is regime
            )
            if not kept:
                continue
            selections.append(kept)
            arms.append(
                _draw_arm(
                    arm_id=f"{self.arms[index].arm_id}__{regime.value.lower()}",
                    selection=kept,
                    spec=self.spec,
                    provenance=self.provenance,
                    resamples=self.resamples,
                    ci_seed=self.seed + 1000 * (index + 1),
                )
            )
        return ControlBattery(
            spec=self.spec,
            seed=self.seed,
            draws=len(selections),
            exposure_budget=None,
            provenance=self.provenance,
            resamples=self.resamples,
            arms=tuple(arms),
            selections=tuple(selections),
        )


def random_entry_controls(
    *,
    spec: MatchSpec,
    outcomes: Sequence[OpportunityOutcome],
    provenance: Provenance,
    exposure_budget: int,
    seed: int = DEFAULT_SEED,
    draws: int = DEFAULT_DRAWS,
    arm_prefix: str = "random_entry_matched_exits",
    resamples: int = BOOTSTRAP_RESAMPLES,
) -> ControlBattery:
    """Draw ``draws`` matched random-entry arms from measured opportunity outcomes.

    Each draw takes exactly ``exposure_budget`` opportunities without replacement,
    which is the treatment's campaign count, so the null spends the same
    opportunity budget. The pool is sorted by ``(bar_index, opportunity_id)``
    before sampling, so the draws depend on the pool's *content* and the seed,
    never on the caller's ordering.

    The supplied ``outcomes`` must have been priced under ``spec`` (same window,
    venue, bracket, fees, funding and slippage); this function records that claim
    and never computes an R multiple of its own.
    """
    if draws < 1:
        raise ControlArmRefusal(f"draws must be at least 1, got {draws}", field="draws")
    if exposure_budget < 1:
        raise ControlArmRefusal(
            f"exposure budget must be at least 1, got {exposure_budget}",
            field="exposure_budget",
        )
    if resamples < 1:
        raise ControlArmRefusal(f"resamples must be at least 1, got {resamples}", field="resamples")
    ordered = tuple(sorted(outcomes, key=lambda item: (item.bar_index, item.opportunity_id)))
    seen: set[str] = set()
    for outcome in ordered:
        if outcome.opportunity_id in seen:
            raise ControlArmRefusal(
                f"duplicate opportunity id {outcome.opportunity_id!r} in the pool",
                field="opportunity_id",
            )
        seen.add(outcome.opportunity_id)
    if len(ordered) < exposure_budget:
        raise ControlArmRefusal(
            f"pool holds {len(ordered)} measured outcomes but the exposure budget is "
            f"{exposure_budget}; a draw without replacement is impossible",
            field="exposure_budget",
            code="INSUFFICIENT_OPPORTUNITY_POOL",
        )

    rng = random.Random(seed)
    arms: list[ArmResult] = []
    selections: list[tuple[OpportunityOutcome, ...]] = []
    for index in range(draws):
        picked = tuple(rng.sample(ordered, exposure_budget))
        selections.append(picked)
        arms.append(
            _draw_arm(
                arm_id=f"{arm_prefix}_{index:03d}",
                selection=picked,
                spec=spec,
                provenance=provenance,
                resamples=resamples,
                ci_seed=seed + 1000 * (index + 1),
            )
        )
    return ControlBattery(
        spec=spec,
        seed=seed,
        draws=draws,
        exposure_budget=exposure_budget,
        provenance=provenance,
        resamples=resamples,
        arms=tuple(arms),
        selections=tuple(selections),
    )


# --------------------------------------------------------------------------- #
# Point-in-time regime split
# --------------------------------------------------------------------------- #


class Regime(StrEnum):
    """Documented market regimes; every bar carries exactly one label."""

    TREND_UP = "TREND_UP"
    TREND_DOWN = "TREND_DOWN"
    RANGE = "RANGE"
    HIGH_VOL = "HIGH_VOL"
    #: Not enough trailing history to classify — never defaulted into ``RANGE``.
    UNLABELLED = "UNLABELLED"


@dataclass(frozen=True)
class RegimeBar:
    """A closed bar: the only input the splitter reads."""

    close: float
    high: float
    low: float

    def __post_init__(self) -> None:
        for name in ("close", "high", "low"):
            value = getattr(self, name)
            if not math.isfinite(value) or value <= 0.0:
                raise ControlArmRefusal(
                    f"bar {name} must be finite and positive, got {value}", field=name
                )
        if self.low > self.high:
            raise ControlArmRefusal(f"bar low {self.low} exceeds high {self.high}", field="low")


@dataclass(frozen=True)
class RegimeConfig:
    """Documented, frozen labelling rule.

    * ``trend_lookback``: bars in the least-squares slope window on log close.
    * ``slope_threshold_per_bar``: ``|slope|`` above this is a trend, not a range.
    * ``vol_lookback``: bars in the realised-volatility window.
    * ``high_vol_multiple``: volatility above this multiple of the *trailing*
      median volatility (previous ``vol_lookback`` values) is ``HIGH_VOL``.

    ``warmup`` = ``max(trend_lookback, 2 * vol_lookback)``: the first bars with no
    full trailing window are ``UNLABELLED``.
    """

    trend_lookback: int = 20
    vol_lookback: int = 20
    slope_threshold_per_bar: float = 0.001
    high_vol_multiple: float = 1.5

    def __post_init__(self) -> None:
        if self.trend_lookback < 2:
            raise ControlArmRefusal(
                f"trend_lookback must be at least 2, got {self.trend_lookback}",
                field="trend_lookback",
            )
        if self.vol_lookback < 2:
            raise ControlArmRefusal(
                f"vol_lookback must be at least 2, got {self.vol_lookback}",
                field="vol_lookback",
            )
        if self.slope_threshold_per_bar <= 0.0:
            raise ControlArmRefusal(
                "slope threshold must be positive", field="slope_threshold_per_bar"
            )
        if self.high_vol_multiple <= 1.0:
            raise ControlArmRefusal("high-vol multiple must exceed 1", field="high_vol_multiple")

    @property
    def warmup(self) -> int:
        return max(self.trend_lookback, 2 * self.vol_lookback)


DEFAULT_REGIME_CONFIG = RegimeConfig()


@dataclass(frozen=True)
class RegimeLabel:
    """The regime of one bar, by index into the labelled bar sequence."""

    index: int
    regime: Regime

    def to_dict(self) -> dict[str, Any]:
        return {"index": self.index, "regime": self.regime.value}


def _slope(window: Sequence[float]) -> float:
    """Least-squares slope of ``window`` against its own index."""
    count = len(window)
    x_mean = (count - 1) / 2.0
    y_mean = sum(window) / count
    numerator = sum((x - x_mean) * (y - y_mean) for x, y in enumerate(window))
    denominator = sum((x - x_mean) ** 2 for x in range(count))
    return numerator / denominator


def label_regimes(
    bars: Sequence[RegimeBar],
    config: RegimeConfig = DEFAULT_REGIME_CONFIG,
) -> tuple[RegimeLabel, ...]:
    """Label every bar with a regime computed from data at or before that bar.

    The label of bar ``i`` depends only on ``bars[: i + 1]``: appending future bars
    cannot change it. Classification order is fixed and documented:

    1. ``i < config.warmup`` → ``UNLABELLED`` (no full trailing window);
    2. trailing realised volatility above ``high_vol_multiple`` times the previous
       ``vol_lookback`` volatilities → ``HIGH_VOL``;
    3. log-close slope above ``+threshold`` → ``TREND_UP``, below ``-threshold`` →
       ``TREND_DOWN``;
    4. otherwise ``RANGE``.

    Deterministic: no RNG, no clock, no dictionary ordering in the arithmetic.
    """
    closes = [bar.close for bar in bars]
    logs = [math.log(value) for value in closes]
    returns: list[float | None] = [None] * len(closes)
    for index in range(1, len(closes)):
        returns[index] = logs[index] - logs[index - 1]

    vols: list[float | None] = [None] * len(closes)
    slopes: list[float | None] = [None] * len(closes)
    for index in range(len(closes)):
        if index >= config.vol_lookback:
            window = returns[index - config.vol_lookback + 1 : index + 1]
            vols[index] = pstdev([float(value) for value in window if value is not None])
        if index >= config.trend_lookback - 1:
            slopes[index] = _slope(logs[index - config.trend_lookback + 1 : index + 1])

    labels: list[RegimeLabel] = []
    for index in range(len(closes)):
        slope = slopes[index]
        vol = vols[index]
        if index < config.warmup or slope is None or vol is None:
            labels.append(RegimeLabel(index=index, regime=Regime.UNLABELLED))
            continue
        previous = vols[index - config.vol_lookback : index]
        previous_values = [value for value in previous if value is not None]
        trailing_median = float(median(previous_values)) if previous_values else 0.0
        high_vol = trailing_median > 0.0 and vol > config.high_vol_multiple * trailing_median
        if high_vol:
            regime = Regime.HIGH_VOL
        elif slope > config.slope_threshold_per_bar:
            regime = Regime.TREND_UP
        elif slope < -config.slope_threshold_per_bar:
            regime = Regime.TREND_DOWN
        else:
            regime = Regime.RANGE
        labels.append(RegimeLabel(index=index, regime=regime))
    return tuple(labels)


def regime_counts(labels: Sequence[RegimeLabel]) -> dict[Regime, int]:
    """Per-regime bar counts, including ``UNLABELLED`` so they sum to the total."""
    counts = {regime: 0 for regime in Regime}
    for label in labels:
        counts[label.regime] += 1
    return counts


def regime_lookup(labels: Sequence[RegimeLabel]) -> dict[int, Regime]:
    """Bar index → regime, for attributing measured entries to a bucket."""
    return {label.index: label.regime for label in labels}


# --------------------------------------------------------------------------- #
# Null comparison
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ControlComparison:
    """Where the treatment sits inside the empirical control distribution."""

    verdict: ControlVerdict
    gate: str
    reason: str
    treatment_arm_id: str
    treatment_campaigns: int
    treatment_net_r: float | None
    control_draws: int
    control_mean: float | None
    control_q05: float | None
    control_q50: float | None
    control_q95: float | None
    percentile_rank: float | None
    empirical_p: float | None
    delta_net_r: float | None
    alpha: float
    seed: int | None
    spec_digest: str | None
    arm_kind: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict.value,
            "gate": self.gate,
            "reason": self.reason,
            "treatment_arm_id": self.treatment_arm_id,
            "treatment_campaigns": self.treatment_campaigns,
            "treatment_net_r": _round(self.treatment_net_r),
            "control_draws": self.control_draws,
            "control_mean": _round(self.control_mean),
            "control_q05": _round(self.control_q05),
            "control_q50": _round(self.control_q50),
            "control_q95": _round(self.control_q95),
            "percentile_rank": _round(self.percentile_rank),
            "empirical_p": _round(self.empirical_p),
            "delta_net_r": _round(self.delta_net_r),
            "alpha": self.alpha,
            "seed": self.seed,
            "spec_digest": self.spec_digest,
            "arm_kind": self.arm_kind,
        }


def _evaluate(
    *,
    treatment_arm_id: str,
    treatment_net_r: float | None,
    treatment_campaigns: int,
    distribution: Sequence[float],
    alpha: float,
    min_draws: int,
    min_campaigns: int,
    seed: int | None,
    spec_digest: str | None,
    arm_kind: str,
    empty_gate: str = GATE_NO_CONTROL_DRAWS_IN_BUCKET,
    absent_is_not_run: bool = True,
) -> ControlComparison:
    """The one place the decision rule lives.

    Superiority is only claimed when the treatment is *outside* the control spread
    (strictly above the 95th percentile) **and** the one-sided empirical p-value
    (plus-one resolution) is at most ``alpha``. Being inside the spread is
    ``NO_EDGE`` — the explicit statement that the intervention did not beat the
    null, not a failure to measure.
    """
    ordered = _sorted(distribution)
    q05 = _quantile(ordered, 0.05)
    q50 = _quantile(ordered, 0.50)
    q95 = _quantile(ordered, 0.95)
    control_mean = _mean(ordered)
    draws = len(ordered)

    def comparison(
        verdict: ControlVerdict,
        gate: str,
        reason: str,
        *,
        percentile_rank: float | None = None,
        empirical_p: float | None = None,
        delta: float | None = None,
    ) -> ControlComparison:
        return ControlComparison(
            verdict=verdict,
            gate=gate,
            reason=reason,
            treatment_arm_id=treatment_arm_id,
            treatment_campaigns=treatment_campaigns,
            treatment_net_r=treatment_net_r,
            control_draws=draws,
            control_mean=control_mean,
            control_q05=q05,
            control_q50=q50,
            control_q95=q95,
            percentile_rank=percentile_rank,
            empirical_p=empirical_p,
            delta_net_r=delta,
            alpha=alpha,
            seed=seed,
            spec_digest=spec_digest,
            arm_kind=arm_kind,
        )

    if treatment_net_r is None or treatment_campaigns < 1:
        if absent_is_not_run:
            return comparison(
                ControlVerdict.NOT_RUN,
                GATE_TREATMENT_NOT_MEASURED,
                "the treatment arm carries no measured per-campaign net R; there is "
                "nothing to place inside the control distribution",
            )
        # A regime bucket with zero measured campaigns is a known count, not an
        # unmeasured arm: it is a sufficiency failure, not an absence of data.
        return comparison(
            ControlVerdict.INCONCLUSIVE,
            GATE_TOO_FEW_CAMPAIGNS,
            f"no campaign in this bucket is below the {min_campaigns} required to "
            "compare against the control distribution",
        )
    if draws == 0:
        return comparison(
            ControlVerdict.INCONCLUSIVE,
            empty_gate,
            "no control draw carries a campaign here, so no null distribution "
            "exists for this comparison",
        )

    exceed = sum(1 for value in ordered if value >= treatment_net_r)
    empirical_p = (1 + exceed) / (draws + 1)
    percentile_rank = sum(1 for value in ordered if value < treatment_net_r) / draws
    delta = treatment_net_r - control_mean if control_mean is not None else None

    if treatment_campaigns < min_campaigns:
        return comparison(
            ControlVerdict.INCONCLUSIVE,
            GATE_TOO_FEW_CAMPAIGNS,
            f"{treatment_campaigns} treatment campaigns is below the {min_campaigns} "
            "required to compare against the control distribution",
            percentile_rank=percentile_rank,
            empirical_p=empirical_p,
            delta=delta,
        )
    if draws < min_draws:
        return comparison(
            ControlVerdict.INCONCLUSIVE,
            GATE_TOO_FEW_DRAWS,
            f"{draws} control draws is below the {min_draws} required to describe "
            "the null distribution",
            percentile_rank=percentile_rank,
            empirical_p=empirical_p,
            delta=delta,
        )
    resolution = 1.0 / (draws + 1)
    if resolution > alpha:
        return comparison(
            ControlVerdict.INCONCLUSIVE,
            GATE_P_RESOLUTION_INSUFFICIENT,
            f"{draws} draws give a smallest attainable one-sided p of "
            f"{resolution:.6f}, above alpha={alpha}; add draws before claiming an edge",
            percentile_rank=percentile_rank,
            empirical_p=empirical_p,
            delta=delta,
        )

    outside = q95 is not None and treatment_net_r > q95
    if not outside:
        return comparison(
            ControlVerdict.NO_EDGE,
            GATE_INSIDE_CONTROL_SPREAD,
            f"treatment net R {treatment_net_r:.6f} is inside the control spread "
            f"(q95={q95}); the null is not beaten",
            percentile_rank=percentile_rank,
            empirical_p=empirical_p,
            delta=delta,
        )
    if empirical_p > alpha:
        return comparison(
            ControlVerdict.NO_EDGE,
            GATE_P_ABOVE_ALPHA,
            f"empirical p={empirical_p:.6f} exceeds alpha={alpha}",
            percentile_rank=percentile_rank,
            empirical_p=empirical_p,
            delta=delta,
        )
    if delta is None or delta <= 0.0:
        return comparison(
            ControlVerdict.NO_EDGE,
            GATE_NONPOSITIVE_DELTA,
            "treatment does not exceed the control mean",
            percentile_rank=percentile_rank,
            empirical_p=empirical_p,
            delta=delta,
        )
    return comparison(
        ControlVerdict.PASS,
        GATE_OUTSIDE_CONTROL_DISTRIBUTION,
        f"treatment net R {treatment_net_r:.6f} exceeds the control distribution "
        f"(q95={q95}, empirical p={empirical_p:.6f})",
        percentile_rank=percentile_rank,
        empirical_p=empirical_p,
        delta=delta,
    )


def _require_shared_measurement_basis(treatment: ArmResult, arm: ArmResult) -> None:
    """Refuse a control arm measured on a different basis than the treatment.

    Delegates the tape/engine/execution-profile guard to ``ablations`` (the same
    check ``compute_delta`` uses) and additionally requires the declared fee terms
    to be identical, since a differing cost model would price the same entry
    differently.
    """
    try:
        _require_comparable_provenance(arm, treatment)
    except ValueError as exc:
        raise ControlArmMismatch(str(exc), field="provenance", code="CONTROL_ARM_MISMATCH") from exc
    left = arm.provenance
    right = treatment.provenance
    assert left is not None and right is not None  # guaranteed by the guard above
    fee_fields = ("fee_model", "maker_fee", "taker_fee")
    mismatched = tuple(name for name in fee_fields if getattr(left, name) != getattr(right, name))
    if mismatched:
        raise ControlArmMismatch(
            "a control arm must be priced by the treatment's cost model; "
            f"mismatched: {', '.join(mismatched)}",
            field=mismatched[0],
            fields=mismatched,
        )


def compare_to_controls(
    treatment: ArmResult,
    controls: ControlBattery,
    *,
    treatment_spec: MatchSpec,
    alpha: float = DEFAULT_ALPHA,
    min_draws: int = MIN_DRAWS,
    min_campaigns: int = MIN_CAMPAIGNS,
) -> ControlComparison:
    """Place a measured treatment arm inside its matched control distribution.

    Refusals (typed, nothing compared) are about the *inputs*: differing matched
    dimensions, a control arm priced on another tape/engine/profile, a control
    arm that is not costed, an exposure budget that is not the treatment's campaign
    count. ``NOT_RUN`` is different — it means the treatment itself has no measured
    numbers, and it names that absence instead of substituting zero.
    """
    if not 0.0 < alpha < 1.0:
        raise ControlArmRefusal(f"alpha must be in (0, 1), got {alpha}", field="alpha")
    if min_draws < 1:
        raise ControlArmRefusal(f"min_draws must be at least 1, got {min_draws}", field="min_draws")
    if min_campaigns < 1:
        raise ControlArmRefusal(
            f"min_campaigns must be at least 1, got {min_campaigns}", field="min_campaigns"
        )

    require_specs_match(treatment_spec, controls.spec)

    samples = tuple(treatment.net_r_samples)
    measured = treatment.costed and treatment.net_r is not None and bool(samples)
    if not measured:
        # An unmeasured (or uncosted) treatment is NOT_RUN whatever the controls
        # hold: naming the absence is the whole result, and no default is invented.
        return _evaluate(
            treatment_arm_id=treatment.arm_id,
            treatment_net_r=None,
            treatment_campaigns=len(samples),
            distribution=controls.distribution,
            alpha=alpha,
            min_draws=min_draws,
            min_campaigns=min_campaigns,
            seed=controls.seed,
            spec_digest=controls.spec.digest,
            arm_kind=controls.arm_kind,
            empty_gate=GATE_CONTROL_ARMS_ABSENT,
        )

    if controls.exposure_budget is not None and len(samples) != controls.exposure_budget:
        raise ControlArmMismatch(
            "control draws must spend the treatment's opportunity budget; "
            f"treatment campaigns={len(samples)} control exposure_budget="
            f"{controls.exposure_budget}",
            field="opportunity_exposure",
        )

    if treatment.provenance is None:
        raise ControlArmMismatch(
            "the treatment arm declares no provenance, so no control arm can be "
            "shown to share its tape and execution basis",
            field="provenance",
            code="PROVENANCE_ABSENT",
        )

    for arm in controls.arms:
        if not arm.costed:
            raise ControlArmMismatch(
                f"control arm {arm.arm_id} is not costed; an uncosted arm is not a measurement",
                field="costed",
            )
        _require_shared_measurement_basis(treatment, arm)

    return _evaluate(
        treatment_arm_id=treatment.arm_id,
        treatment_net_r=treatment.net_r,
        treatment_campaigns=len(samples),
        distribution=controls.distribution,
        alpha=alpha,
        min_draws=min_draws,
        min_campaigns=min_campaigns,
        seed=controls.seed,
        spec_digest=controls.spec.digest,
        arm_kind=controls.arm_kind,
        empty_gate=GATE_CONTROL_ARMS_ABSENT,
    )


# --------------------------------------------------------------------------- #
# Per-regime arm results
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class CampaignOutcome:
    """One treatment campaign: entry bar for regime attribution and its net R."""

    bar_index: int
    net_r: float

    def __post_init__(self) -> None:
        if self.bar_index < 0:
            raise ControlArmRefusal(
                f"bar index must be non-negative, got {self.bar_index}", field="bar_index"
            )
        if not math.isfinite(self.net_r):
            raise ControlArmRefusal(f"net_r must be finite, got {self.net_r}", field="net_r")


@dataclass(frozen=True)
class RegimeBucket:
    """One regime's arm results, with the sample counts that produced them."""

    regime: Regime
    bars: int
    campaigns: int
    treatment_net_r: float | None
    comparison: ControlComparison

    def to_dict(self) -> dict[str, Any]:
        return {
            "regime": self.regime.value,
            "bars": self.bars,
            "campaigns": self.campaigns,
            "treatment_net_r": _round(self.treatment_net_r),
            "comparison": self.comparison.to_dict(),
        }


@dataclass(frozen=True)
class RegimeReport:
    """Overall comparison plus every regime bucket, including ``UNLABELLED``."""

    overall: ControlComparison
    buckets: tuple[RegimeBucket, ...]
    bar_counts: Mapping[Regime, int]
    total_bars: int
    total_campaigns: int
    min_campaigns: int

    @property
    def sufficient_regimes(self) -> tuple[Regime, ...]:
        return tuple(
            bucket.regime
            for bucket in self.buckets
            if bucket.comparison.verdict is not ControlVerdict.INCONCLUSIVE
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "overall": self.overall.to_dict(),
            "total_bars": self.total_bars,
            "total_campaigns": self.total_campaigns,
            "min_campaigns": self.min_campaigns,
            "bar_counts": {regime.value: self.bar_counts.get(regime, 0) for regime in Regime},
            "sufficient_regimes": [regime.value for regime in self.sufficient_regimes],
            "buckets": [bucket.to_dict() for bucket in self.buckets],
        }


def compare_by_regime(
    treatment: ArmResult,
    campaigns: Sequence[CampaignOutcome],
    controls: ControlBattery,
    labels: Sequence[RegimeLabel],
    *,
    treatment_spec: MatchSpec,
    alpha: float = DEFAULT_ALPHA,
    min_draws: int = MIN_DRAWS,
    min_campaigns: int = MIN_CAMPAIGNS,
) -> RegimeReport:
    """Per-regime comparison of the treatment against the same control draws.

    ``campaigns`` must be the treatment's own campaigns, in the order that produced
    ``treatment.net_r_samples``; a mismatch is refused rather than silently
    re-bucketed. A bucket with fewer than ``min_campaigns`` campaigns is
    ``INCONCLUSIVE``, which is the point of the split: a regime where the arm has
    almost no campaigns cannot evidence anything.
    """
    if len(campaigns) != len(treatment.net_r_samples) or tuple(
        campaign.net_r for campaign in campaigns
    ) != tuple(treatment.net_r_samples):
        raise ControlArmRefusal(
            "campaign attribution must match the treatment arm's per-campaign "
            f"samples; campaigns={len(campaigns)} "
            f"samples={len(treatment.net_r_samples)}",
            field="campaigns",
            code="CAMPAIGN_MISMATCH",
        )
    if labels and len(labels) <= max((campaign.bar_index for campaign in campaigns), default=-1):
        raise ControlArmRefusal(
            "a campaign entry bar lies outside the labelled bar sequence",
            field="labels",
            code="REGIME_LABEL_RANGE",
        )

    lookup = regime_lookup(labels)
    counts = regime_counts(labels)
    overall = compare_to_controls(
        treatment,
        controls,
        treatment_spec=treatment_spec,
        alpha=alpha,
        min_draws=min_draws,
        min_campaigns=min_campaigns,
    )

    buckets: list[RegimeBucket] = []
    for regime in Regime:
        in_bucket = [campaign for campaign in campaigns if lookup.get(campaign.bar_index) is regime]
        bucket_samples = tuple(campaign.net_r for campaign in in_bucket)
        bucket_net_r = _mean(bucket_samples)
        subset = controls.subset(lookup, regime)
        comparison = _evaluate(
            treatment_arm_id=treatment.arm_id,
            treatment_net_r=bucket_net_r,
            treatment_campaigns=len(bucket_samples),
            distribution=subset.distribution,
            alpha=alpha,
            min_draws=min_draws,
            min_campaigns=min_campaigns,
            seed=controls.seed,
            spec_digest=controls.spec.digest if subset.arms else None,
            arm_kind=controls.arm_kind,
            absent_is_not_run=False,
        )
        buckets.append(
            RegimeBucket(
                regime=regime,
                bars=counts[regime],
                campaigns=len(in_bucket),
                treatment_net_r=bucket_net_r,
                comparison=comparison,
            )
        )
    return RegimeReport(
        overall=overall,
        buckets=tuple(buckets),
        bar_counts=counts,
        total_bars=len(labels),
        total_campaigns=len(campaigns),
        min_campaigns=min_campaigns,
    )
