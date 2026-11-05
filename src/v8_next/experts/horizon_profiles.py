"""Versioned per-expert evaluation profiles (RFC 1.4.1 §5.5, M1).

The shared :class:`~v8_next.domain.horizons.HorizonContext` says *what the market
looked like* on 15m/1h/4h at a decision cut. This module says *which of those
views a given expert is allowed to reason on*, and what it must do when that
view is absent.

Why a separate table
--------------------
A single H1 ``CausalFrame`` for all 28 witnesses silently asserts that every
behaviour has the same decision horizon. That is an implementation shortcut, not
the architecture: a liquidity-sweep trap is a short-horizon structural claim,
while a Donchian or Ichimoku regime claim is a higher-timeframe claim. Evaluating
all of them on one frame either starves the fast experts of structure or feeds
the slow experts noise.

The table is explicit, versioned and falsifiable:

* **primary** — the horizon whose bar closes trigger this expert's evaluation and
  whose frame the witness reads. It is the *only* frame the expert is dispatched
  on, so no expert sees a horizon combination it did not declare.
* **confirmations** — horizons the expert may consult as context. They are
  advisory: a missing confirmation degrades context quality but does not by
  itself silence the witness. A profile that must have a confirmation declares it
  in ``required_confirmations`` (none do in v1) and then abstains with
  ``CONTEXT_INCOMPLETE``.
* **max_staleness_ns** — how old the primary alignment may be at the decision cut.
  A witness whose primary bar is older than this abstains with
  ``CONTEXT_INCOMPLETE`` instead of asserting on stale structure.
* **required_features / optional_data** — copied from the registry's
  ``REQUIRES_TABLE`` and the expert's positioning needs, so the profile and the
  executable observer cannot drift.
* **missing_behavior** — always ``ABSTAIN_CONTEXT_INCOMPLETE`` in v1: a missing
  horizon is a real result, never a zero-filled bar.

These assignments are *hypotheses recorded before the run*, not tuned values.
They are deliberately coarse (the behaviour family, not the variant) so the table
cannot be re-fit per bar. Changing an assignment requires a new
``PROFILE_TABLE_VERSION`` and a new measurement.
"""

from __future__ import annotations

from enum import StrEnum
from types import MappingProxyType
from typing import Final, Mapping

from pydantic import BaseModel, ConfigDict, model_validator

from v8_next.domain.horizons import Timeframe
from v8_next.experts.registry import (
    CANONICAL_28_EXPERTS,
    get_expert,
)

__all__ = [
    "HORIZON_PROFILES",
    "PROFILE_TABLE_VERSION",
    "ExpertHorizonProfile",
    "MissingBehavior",
    "habitats_for",
    "profile_for",
    "profiles_by_primary",
    "validate_profiles",
]

#: Bumped on any assignment, window or rule change. It is bound into evidence
#: provenance so a stance can never be re-read under a different table.
PROFILE_TABLE_VERSION: Final = "v9.expert-horizon-profiles.1"

#: A missing primary horizon is an abstention, never an invented bar.
class MissingBehavior(StrEnum):
    ABSTAIN_CONTEXT_INCOMPLETE = "ABSTAIN_CONTEXT_INCOMPLETE"


class ExpertHorizonProfile(BaseModel):
    """One expert's declared evaluation contract."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    expert_id: str
    version: str
    primary: Timeframe
    confirmations: tuple[Timeframe, ...] = ()
    required_confirmations: tuple[Timeframe, ...] = ()
    #: Primary-horizon bars the registry requires before dispatch.
    warmup_bars: int
    #: Maximum age of the selected primary bar at the decision cut.
    max_staleness_ns: int
    required_features: tuple[str, ...]
    optional_data: tuple[str, ...] = ()
    habitats: tuple[str, ...]
    evaluation_trigger: str = "ON_PRIMARY_BAR_CLOSE"
    missing_behavior: MissingBehavior = MissingBehavior.ABSTAIN_CONTEXT_INCOMPLETE
    rationale: str

    @model_validator(mode="after")
    def _check(self) -> ExpertHorizonProfile:
        if self.expert_id not in CANONICAL_28_EXPERTS:
            raise ValueError(f"{self.expert_id!r} is not one of the 28 canonical experts")
        if not self.version.strip() or not self.rationale.strip():
            raise ValueError("version and rationale cannot be empty")
        if self.warmup_bars <= 0:
            raise ValueError("warmup_bars must be positive")
        if self.max_staleness_ns <= 0:
            raise ValueError("max_staleness_ns must be positive")
        if self.primary in self.confirmations:
            raise ValueError("primary horizon cannot also be a confirmation")
        if self.primary in self.required_confirmations:
            raise ValueError("primary horizon cannot be a required confirmation")
        if not set(self.required_confirmations) <= set(self.confirmations):
            raise ValueError("required confirmations must also be listed as confirmations")
        if not self.required_features:
            raise ValueError("required_features cannot be empty")
        if not self.habitats:
            raise ValueError("habitats cannot be empty")
        spec = get_expert(self.expert_id)
        if tuple(sorted(self.required_features)) != tuple(sorted(spec.requires)):
            raise ValueError(
                f"{self.expert_id}: required_features {self.required_features} do not match "
                f"the registry's {spec.requires}"
            )
        if self.warmup_bars != spec.warmup:
            raise ValueError(
                f"{self.expert_id}: warmup {self.warmup_bars} does not match registry "
                f"warmup {spec.warmup}"
            )
        return self

    @property
    def profile_id(self) -> str:
        """Stable identity of this profile (table version + expert + primary)."""
        return f"{PROFILE_TABLE_VERSION}:{self.expert_id}:{self.primary.value}"


_NS_PER_MINUTE = 60 * 1_000_000_000

#: Habitat vocabulary shared with MarketBelief's regime labels.
_TREND = ("trend",)
_RANGE = ("range",)
_TREND_RANGE = ("trend", "range")
_TRANSITIONAL = ("trend", "transition", "shock")
_RANGE_TRANSITIONAL = ("range", "transition", "shock")
_ALL = ("trend", "range", "transition", "shock", "unknown")

#: (primary, confirmations, staleness windows in primary bars, habitats, rationale)
#: Grouped by the mechanism family that motivates the horizon choice.
_ASSIGNMENTS: Final[Mapping[str, tuple[Timeframe, tuple[Timeframe, ...], int, tuple[str, ...], str]]] = MappingProxyType(
    {
        # --- short horizon: intrabar traps, absorption and exhaustion --------
        "liquidity_sweep_reclaim": (Timeframe.M15, (Timeframe.H1,), 2, _RANGE_TRANSITIONAL, "short_structure_liquidity"),
        "failed_breakout": (Timeframe.M15, (Timeframe.H1,), 2, _TRANSITIONAL, "short_structure_liquidity"),
        "failed_breakout_2b": (Timeframe.M15, (Timeframe.H1,), 2, _TRANSITIONAL, "short_structure_liquidity"),
        "candlestick_reversal": (Timeframe.M15, (Timeframe.H1,), 2, _TRANSITIONAL, "short_structure_liquidity"),
        "volume_climax_reversal": (Timeframe.M15, (Timeframe.H1, Timeframe.H4), 2, _TRANSITIONAL, "short_structure_liquidity"),
        "gap_exhaustion": (Timeframe.M15, (Timeframe.H1,), 2, _TRANSITIONAL, "short_structure_liquidity"),
        "rsi_stoch_reversion": (Timeframe.M15, (Timeframe.H1,), 2, _RANGE_TRANSITIONAL, "short_structure_liquidity"),
        "bollinger_reversion": (Timeframe.M15, (Timeframe.H1,), 2, _RANGE, "short_structure_liquidity"),
        # --- intermediate horizon: the standard swing context ----------------
        "bollinger_breakout": (Timeframe.H1, (Timeframe.H4,), 2, _TREND_RANGE, "intermediate_swing_context"),
        "breakout_retest": (Timeframe.H1, (Timeframe.H4,), 2, _TREND_RANGE, "intermediate_swing_context"),
        "divergence_12_setups": (Timeframe.H1, (Timeframe.H4,), 2, _TREND_RANGE, "intermediate_swing_context"),
        "fib_retracement_continuation": (Timeframe.H1, (Timeframe.H4,), 2, _TREND, "intermediate_swing_context"),
        "fib_rsi_bb_confluence": (Timeframe.H1, (Timeframe.H4,), 2, _TREND_RANGE, "intermediate_swing_context"),
        "floor_trader_pivot": (Timeframe.H1, (), 2, _ALL, "intermediate_swing_context"),
        "funding_crowding_reversal": (Timeframe.H1, (Timeframe.H4,), 3, _ALL, "intermediate_swing_context"),
        "macd_stoch_trend": (Timeframe.H1, (Timeframe.H4,), 2, _TREND, "intermediate_swing_context"),
        "obv_adl_regime": (Timeframe.H1, (Timeframe.H4,), 3, _TREND_RANGE, "intermediate_swing_context"),
        "open_interest_divergence": (Timeframe.H1, (Timeframe.H4,), 3, _ALL, "intermediate_swing_context"),
        "range_breakout_1to1": (Timeframe.H1, (Timeframe.H4,), 2, _TREND_RANGE, "intermediate_swing_context"),
        "trend_pullback": (Timeframe.H1, (Timeframe.H4,), 2, _TREND, "intermediate_swing_context"),
        "trend_pullback_depth": (Timeframe.H1, (Timeframe.H4,), 2, _TREND, "intermediate_swing_context"),
        "volume_confirmed_breakout": (Timeframe.H1, (Timeframe.H4,), 2, _TREND_RANGE, "intermediate_swing_context"),
        # --- higher horizon: regime, channel and measured-move claims ---------
        "donchian_breakout": (Timeframe.H4, (Timeframe.H1,), 2, _TREND, "higher_timeframe_regime"),
        "fib_projection_reversal": (Timeframe.H4, (Timeframe.H1,), 2, _TREND_RANGE, "higher_timeframe_regime"),
        "ichimoku_cloud": (Timeframe.H4, (Timeframe.H1,), 2, _TREND, "higher_timeframe_regime"),
        "market_profile_value_area": (Timeframe.H4, (Timeframe.H1,), 2, _ALL, "higher_timeframe_regime"),
        "pandf_breakout": (Timeframe.H4, (Timeframe.H1,), 2, _TREND_RANGE, "higher_timeframe_regime"),
        "pattern_measuring_objective": (Timeframe.H4, (Timeframe.H1,), 2, _TREND_RANGE, "higher_timeframe_regime"),
    }
)

#: Declared alternative data a profile may consume when the tape actually carries
#: it. ``positioning`` maps to the registry's declared requirement; funding and
#: open interest arrive through the same positioning readings today, so they are
#: named separately only when the observer reads that specific field.
_OPTIONAL_DATA: Final[Mapping[str, tuple[str, ...]]] = MappingProxyType(
    {
        "funding_crowding_reversal": ("funding_rate",),
        "open_interest_divergence": ("open_interest",),
        "obv_adl_regime": ("bar_volume",),
        "volume_climax_reversal": ("bar_volume",),
        "volume_confirmed_breakout": ("bar_volume",),
    }
)


def _profile(expert_id: str) -> ExpertHorizonProfile:
    primary, confirmations, staleness_bars, habitats, rationale = _ASSIGNMENTS[expert_id]
    spec = get_expert(expert_id)
    optional: tuple[str, ...] = _OPTIONAL_DATA.get(expert_id, ())
    if "positioning" in spec.requires and "funding_rate" not in optional:
        optional = (*optional, "positioning_readings")
    return ExpertHorizonProfile(
        expert_id=expert_id,
        version=PROFILE_TABLE_VERSION,
        primary=primary,
        confirmations=confirmations,
        warmup_bars=spec.warmup,
        max_staleness_ns=staleness_bars * primary.duration_ns,
        required_features=spec.requires,
        optional_data=optional,
        habitats=habitats,
        rationale=rationale,
    )


if set(_ASSIGNMENTS) != set(CANONICAL_28_EXPERTS):  # pragma: no cover - import guard
    missing = sorted(set(CANONICAL_28_EXPERTS) - set(_ASSIGNMENTS))
    extra = sorted(set(_ASSIGNMENTS) - set(CANONICAL_28_EXPERTS))
    raise RuntimeError(
        f"expert horizon profiles must cover all 28 canonical experts; "
        f"missing={missing} extra={extra}"
    )

HORIZON_PROFILES: Final[Mapping[str, ExpertHorizonProfile]] = MappingProxyType(
    {expert_id: _profile(expert_id) for expert_id in CANONICAL_28_EXPERTS}
)


def validate_profiles() -> None:
    """Fail closed unless the table covers 28 unique canonical ids."""
    if set(HORIZON_PROFILES) != set(CANONICAL_28_EXPERTS):
        raise ValueError("horizon profile table does not cover the canonical 28")
    for expert_id, profile in HORIZON_PROFILES.items():
        if profile.expert_id != expert_id:
            raise ValueError(f"profile key {expert_id!r} carries {profile.expert_id!r}")


def profile_for(expert_id: str) -> ExpertHorizonProfile:
    """Return the declared profile; an unknown id is a contract error."""
    profile = HORIZON_PROFILES.get(expert_id)
    if profile is None:
        raise KeyError(f"no horizon profile for expert {expert_id!r}")
    return profile


def profiles_by_primary() -> Mapping[Timeframe, tuple[str, ...]]:
    """Expert ids in registry order, grouped by primary horizon."""
    grouped: dict[Timeframe, list[str]] = {timeframe: [] for timeframe in Timeframe}
    for expert_id in CANONICAL_28_EXPERTS:
        grouped[HORIZON_PROFILES[expert_id].primary].append(expert_id)
    return MappingProxyType({tf: tuple(ids) for tf, ids in grouped.items()})


def habitats_for(expert_id: str) -> tuple[str, ...]:
    return profile_for(expert_id).habitats
