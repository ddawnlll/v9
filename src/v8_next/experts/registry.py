"""Authoritative 28-expert registry and catalog (matching v8-core/src/experts/mod.rs).

Epistemic Demarcation (Rules 13, 14, 20, 21):
Experts are epistemic witnesses, NOT economic sovereigns.
They observe pre-existing Opportunities and emit typed evidence stances.
They have ZERO capital, portfolio, or execution authority.

Architectural Scope Note on squeeze_swing and divergence_12_setups:
- `squeeze_swing` is present in Rust's VARIANT_TABLE (v8-core/src/experts/mod.rs:86)
  with variants ("default", "m1", "m2", "m3"), but is excluded from the 28-expert
  dispatch table TABLE because it serves as an opportunity/compression baseline.
- `divergence_12_setups` is one of the canonical 28 active generator-experts in
  TABLE, but is omitted from Rust's VARIANT_TABLE (v8-core/src/experts/mod.rs:59)
  because its setup detection evaluates internally rather than accepting a
  request-level constructor variant override.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from v8_next.domain.market import CausalFrame
from v8_next.domain.positioning import PositioningReading
from v8_next.economics.decisions import Opportunity, Stance, StanceKind
from v8_next.experts.bollinger import observe_bollinger_breakout
from v8_next.experts.breakouts import observe_failed_breakout, observe_volume_breakout
from v8_next.experts.candlestick import (
    VARIANTS as CANDLESTICK_VARIANTS,
)
from v8_next.experts.candlestick import (
    observe_candlestick,
)
from v8_next.experts.climax import observe_volume_climax
from v8_next.experts.confluence import observe_confluence
from v8_next.experts.divergence import observe_divergence
from v8_next.experts.donchian import observe_donchian
from v8_next.experts.failed_moves import (
    WARMUP as FAILED_MOVES_WARMUP,
)
from v8_next.experts.failed_moves import (
    observe_failed_move,
)
from v8_next.experts.features import _ACTIVE_LAZY, _LazyBundle
from v8_next.experts.fibonacci import observe_fib_projection, observe_fib_retracement
from v8_next.experts.gaps import observe_gap
from v8_next.experts.ichimoku import observe_ichimoku
from v8_next.experts.levels import observe_floor_pivot, observe_range_breakout
from v8_next.experts.measuring import (
    VARIANTS as MEASURING_VARIANTS,
)
from v8_next.experts.measuring import (
    observe_measuring,
)
from v8_next.experts.momentum import observe_macd_stoch, observe_obv_adl
from v8_next.experts.pandf import observe_pandf
from v8_next.experts.positioning import observe_funding, observe_open_interest
from v8_next.experts.profile import observe_profile
from v8_next.experts.reclaim import observe_breakout_retest, observe_liquidity_reclaim
from v8_next.experts.reversion import observe_bollinger_reversion, observe_rsi_reversion
from v8_next.experts.trend import observe_trend_depth, observe_trend_pullback


@dataclass(frozen=True)
class ExpertSpec:
    """Descriptor for a ported expert witness in the authoritative 28-table."""

    expert_id: str  # Canonical Rust identifier (e.g. 'bollinger_breakout')
    alias: str  # Hyphenated identifier (e.g. 'bollinger-breakout')
    behavior_family: str  # Behavior family ID from witness_adapter
    mechanism_family: str  # Mechanism family ID from witness_adapter
    dependency_group: str  # Dependency group ID from witness_adapter
    requires: tuple[str, ...]  # Feature requirement groups from REQUIRES_TABLE
    supported_variants: tuple[str, ...]  # Explicit constructor variants from VARIANT_TABLE
    default_variant: str | None  # Default variant if variant-parameterized
    warmup: int  # Minimum bar history required to evaluate
    observer_fn: Callable[..., Stance]  # Real executable observer function
    takes_variant: bool = False
    takes_readings: bool = False
    version: str = "v1"
    ported: bool = True


# The 28 canonical active experts in exact dispatch order from v8-core/src/experts/mod.rs TABLE.
CANONICAL_28_EXPERTS: tuple[str, ...] = (
    "bollinger_breakout",
    "bollinger_reversion",
    "breakout_retest",
    "candlestick_reversal",
    "divergence_12_setups",
    "donchian_breakout",
    "failed_breakout",
    "failed_breakout_2b",
    "fib_projection_reversal",
    "fib_retracement_continuation",
    "fib_rsi_bb_confluence",
    "floor_trader_pivot",
    "funding_crowding_reversal",
    "gap_exhaustion",
    "ichimoku_cloud",
    "liquidity_sweep_reclaim",
    "macd_stoch_trend",
    "market_profile_value_area",
    "obv_adl_regime",
    "open_interest_divergence",
    "pandf_breakout",
    "pattern_measuring_objective",
    "range_breakout_1to1",
    "rsi_stoch_reversion",
    "trend_pullback",
    "trend_pullback_depth",
    "volume_climax_reversal",
    "volume_confirmed_breakout",
)

# Explicitly dispatched constructor variants (mirrors Rust VARIANT_TABLE in v8-core/src/experts/mod.rs:59).
# Note: squeeze_swing is present in VARIANT_TABLE as in Rust, while divergence_12_setups evaluates internally.
VARIANT_TABLE: dict[str, tuple[str, ...]] = {
    "bollinger_breakout": ("a", "b", "c"),
    "breakout_retest": ("a", "b", "c"),
    "candlestick_reversal": CANDLESTICK_VARIANTS,
    "failed_breakout_2b": ("b", "c", "d", "e", "f", "g"),
    "fib_rsi_bb_confluence": ("a", "b"),
    "funding_crowding_reversal": ("a", "b", "c", "d"),
    "gap_exhaustion": ("a", "b", "c"),
    "market_profile_value_area": ("a", "b", "c", "d"),
    "open_interest_divergence": ("a", "b", "c", "d"),
    "pandf_breakout": ("a", "b", "c", "d"),
    "pattern_measuring_objective": MEASURING_VARIANTS,
    "squeeze_swing": ("default", "m1", "m2", "m3"),
}

# Declared feature requirements (mirrors Rust REQUIRES_TABLE).
REQUIRES_TABLE: dict[str, tuple[str, ...]] = {
    "bollinger_breakout": ("volatility", "history"),
    "bollinger_reversion": ("trend", "volatility", "history"),
    "breakout_retest": ("location", "volatility", "history"),
    "candlestick_reversal": ("candle_shape", "volatility", "history"),
    "divergence_12_setups": ("oscillator", "location", "volatility", "history"),
    "donchian_breakout": ("location", "volatility", "history", "participation"),
    "failed_breakout": ("location", "volatility", "history"),
    "failed_breakout_2b": ("location", "volatility", "history", "candle_shape"),
    "fib_projection_reversal": ("location", "volatility", "history"),
    "fib_retracement_continuation": ("location", "volatility", "history"),
    "fib_rsi_bb_confluence": ("oscillator", "location", "volatility", "history"),
    "floor_trader_pivot": ("location", "volatility", "history", "session"),
    "funding_crowding_reversal": ("positioning", "volatility", "history"),
    "gap_exhaustion": ("candle_shape", "location", "volatility", "history"),
    "ichimoku_cloud": ("volatility", "history"),
    "liquidity_sweep_reclaim": ("location", "volatility", "history"),
    "macd_stoch_trend": ("oscillator", "volatility", "history"),
    "market_profile_value_area": ("session", "volatility", "history"),
    "obv_adl_regime": ("participation", "trend", "volatility", "history"),
    "open_interest_divergence": ("positioning", "participation", "volatility", "history"),
    "pandf_breakout": ("volatility", "history"),
    "pattern_measuring_objective": ("location", "volatility", "history"),
    "range_breakout_1to1": ("location", "volatility", "history", "participation"),
    "rsi_stoch_reversion": ("oscillator", "volatility", "history"),
    "trend_pullback": ("trend", "volatility", "history"),
    "trend_pullback_depth": ("trend", "location", "volatility", "history"),
    "volume_climax_reversal": ("trend", "volatility", "participation", "history"),
    "volume_confirmed_breakout": ("location", "volatility", "participation", "history"),
}

_EXPERT_SPECS_LIST: tuple[ExpertSpec, ...] = (
    ExpertSpec(
        expert_id="bollinger_breakout",
        alias="bollinger-breakout",
        behavior_family="breakout",
        mechanism_family="volatility",
        dependency_group="dep_breakout",
        requires=REQUIRES_TABLE["bollinger_breakout"],
        supported_variants=VARIANT_TABLE["bollinger_breakout"],
        default_variant="a",
        warmup=20,
        observer_fn=observe_bollinger_breakout,
        takes_variant=True,
        version="v1",
    ),
    ExpertSpec(
        expert_id="bollinger_reversion",
        alias="bollinger-reversion",
        behavior_family="mean_reversion",
        mechanism_family="volatility",
        dependency_group="dep_reversion",
        requires=REQUIRES_TABLE["bollinger_reversion"],
        supported_variants=(),
        default_variant=None,
        warmup=20,
        observer_fn=observe_bollinger_reversion,
        version="v1",
    ),
    ExpertSpec(
        expert_id="breakout_retest",
        alias="breakout-retest",
        behavior_family="breakout",
        mechanism_family="structural",
        dependency_group="dep_breakout",
        requires=REQUIRES_TABLE["breakout_retest"],
        supported_variants=VARIANT_TABLE["breakout_retest"],
        default_variant="a",
        warmup=21,
        observer_fn=observe_breakout_retest,
        takes_variant=True,
        version="v1",
    ),
    ExpertSpec(
        expert_id="candlestick_reversal",
        alias="candlestick-reversal",
        behavior_family="reversal",
        mechanism_family="price_action",
        dependency_group="dep_pattern",
        requires=REQUIRES_TABLE["candlestick_reversal"],
        supported_variants=VARIANT_TABLE["candlestick_reversal"],
        default_variant=None,
        warmup=2,
        observer_fn=observe_candlestick,
        takes_variant=True,
        version="v1",
    ),
    ExpertSpec(
        expert_id="divergence_12_setups",
        alias="divergence-12-setups",
        behavior_family="divergence",
        mechanism_family="oscillator",
        dependency_group="dep_oscillator",
        requires=REQUIRES_TABLE["divergence_12_setups"],
        supported_variants=(),  # In Rust mod.rs:59, divergence_12_setups has no constructor variants
        default_variant="a",
        warmup=21,
        observer_fn=observe_divergence,
        takes_variant=True,
        version="v1",
    ),
    ExpertSpec(
        expert_id="donchian_breakout",
        alias="donchian-breakout",
        behavior_family="breakout",
        mechanism_family="channel",
        dependency_group="dep_breakout",
        requires=REQUIRES_TABLE["donchian_breakout"],
        supported_variants=(),
        default_variant=None,
        warmup=21,
        observer_fn=observe_donchian,
        version="v1",
    ),
    ExpertSpec(
        expert_id="failed_breakout",
        alias="failed-breakout",
        behavior_family="trap",
        mechanism_family="structural",
        dependency_group="dep_trap",
        requires=REQUIRES_TABLE["failed_breakout"],
        supported_variants=(),
        default_variant=None,
        warmup=2,
        observer_fn=observe_failed_breakout,
        version="v1",
    ),
    ExpertSpec(
        expert_id="failed_breakout_2b",
        alias="failed-breakout-2b",
        behavior_family="trap",
        mechanism_family="structural",
        dependency_group="dep_trap",
        requires=REQUIRES_TABLE["failed_breakout_2b"],
        supported_variants=VARIANT_TABLE["failed_breakout_2b"],
        default_variant="b",
        warmup=FAILED_MOVES_WARMUP["b"],
        observer_fn=observe_failed_move,
        takes_variant=True,
        version="v1",
    ),
    ExpertSpec(
        expert_id="fib_projection_reversal",
        alias="fib-projection-reversal",
        behavior_family="reversal",
        mechanism_family="geometric",
        dependency_group="dep_fib",
        requires=REQUIRES_TABLE["fib_projection_reversal"],
        supported_variants=(),
        default_variant=None,
        warmup=21,
        observer_fn=observe_fib_projection,
        version="v1",
    ),
    ExpertSpec(
        expert_id="fib_retracement_continuation",
        alias="fib-retracement-continuation",
        behavior_family="continuation",
        mechanism_family="geometric",
        dependency_group="dep_fib",
        requires=REQUIRES_TABLE["fib_retracement_continuation"],
        supported_variants=(),
        default_variant=None,
        warmup=21,
        observer_fn=observe_fib_retracement,
        version="v1",
    ),
    ExpertSpec(
        expert_id="fib_rsi_bb_confluence",
        alias="fib-rsi-bb-confluence",
        behavior_family="confluence",
        mechanism_family="confluence",
        dependency_group="dep_confluence",
        requires=REQUIRES_TABLE["fib_rsi_bb_confluence"],
        supported_variants=VARIANT_TABLE["fib_rsi_bb_confluence"],
        default_variant="a",
        warmup=21,
        observer_fn=observe_confluence,
        takes_variant=True,
        version="v1",
    ),
    ExpertSpec(
        expert_id="floor_trader_pivot",
        alias="floor-trader-pivot",
        behavior_family="range",
        mechanism_family="pivot",
        dependency_group="dep_pivot",
        requires=REQUIRES_TABLE["floor_trader_pivot"],
        supported_variants=(),
        default_variant=None,
        warmup=25,
        observer_fn=observe_floor_pivot,
        version="v1",
    ),
    ExpertSpec(
        expert_id="funding_crowding_reversal",
        alias="funding-crowding-reversal",
        behavior_family="crowding",
        mechanism_family="derivatives",
        dependency_group="dep_derivatives",
        requires=REQUIRES_TABLE["funding_crowding_reversal"],
        supported_variants=VARIANT_TABLE["funding_crowding_reversal"],
        default_variant="a",
        warmup=11,
        observer_fn=observe_funding,
        takes_variant=True,
        takes_readings=True,
        version="v1",
    ),
    ExpertSpec(
        expert_id="gap_exhaustion",
        alias="gap-exhaustion",
        behavior_family="exhaustion",
        mechanism_family="gap",
        dependency_group="dep_gap",
        requires=REQUIRES_TABLE["gap_exhaustion"],
        supported_variants=VARIANT_TABLE["gap_exhaustion"],
        default_variant="a",
        warmup=21,
        observer_fn=observe_gap,
        takes_variant=True,
        version="v1",
    ),
    ExpertSpec(
        expert_id="ichimoku_cloud",
        alias="ichimoku-cloud",
        behavior_family="cloud",
        mechanism_family="trend",
        dependency_group="dep_ichimoku",
        requires=REQUIRES_TABLE["ichimoku_cloud"],
        supported_variants=(),
        default_variant=None,
        warmup=27,
        observer_fn=observe_ichimoku,
        version="v1",
    ),
    ExpertSpec(
        expert_id="liquidity_sweep_reclaim",
        alias="liquidity-sweep-reclaim",
        behavior_family="sweep",
        mechanism_family="liquidity",
        dependency_group="dep_liquidity",
        requires=REQUIRES_TABLE["liquidity_sweep_reclaim"],
        supported_variants=(),
        default_variant=None,
        warmup=2,
        observer_fn=observe_liquidity_reclaim,
        version="v1",
    ),
    ExpertSpec(
        expert_id="macd_stoch_trend",
        alias="macd-stoch-trend",
        behavior_family="trend",
        mechanism_family="oscillator",
        dependency_group="dep_oscillator",
        requires=REQUIRES_TABLE["macd_stoch_trend"],
        supported_variants=(),
        default_variant=None,
        warmup=34,
        observer_fn=observe_macd_stoch,
        version="v1",
    ),
    ExpertSpec(
        expert_id="market_profile_value_area",
        alias="market-profile-value-area",
        behavior_family="value_area",
        mechanism_family="profile",
        dependency_group="dep_profile",
        requires=REQUIRES_TABLE["market_profile_value_area"],
        supported_variants=VARIANT_TABLE["market_profile_value_area"],
        default_variant="a",
        warmup=25,
        observer_fn=observe_profile,
        takes_variant=True,
        version="v1",
    ),
    ExpertSpec(
        expert_id="obv_adl_regime",
        alias="obv-adl-regime",
        behavior_family="flow",
        mechanism_family="volume",
        dependency_group="dep_volume",
        requires=REQUIRES_TABLE["obv_adl_regime"],
        supported_variants=(),
        default_variant=None,
        warmup=20,
        observer_fn=observe_obv_adl,
        version="v1",
    ),
    ExpertSpec(
        expert_id="open_interest_divergence",
        alias="open-interest-divergence",
        behavior_family="divergence",
        mechanism_family="derivatives",
        dependency_group="dep_derivatives",
        requires=REQUIRES_TABLE["open_interest_divergence"],
        supported_variants=VARIANT_TABLE["open_interest_divergence"],
        default_variant="a",
        warmup=100,
        observer_fn=observe_open_interest,
        takes_variant=True,
        takes_readings=True,
        version="v1",
    ),
    ExpertSpec(
        expert_id="pandf_breakout",
        alias="pandf-breakout",
        behavior_family="breakout",
        mechanism_family="point_figure",
        dependency_group="dep_breakout",
        requires=REQUIRES_TABLE["pandf_breakout"],
        supported_variants=VARIANT_TABLE["pandf_breakout"],
        default_variant="a",
        warmup=20,
        observer_fn=observe_pandf,
        takes_variant=True,
        version="v1",
    ),
    ExpertSpec(
        expert_id="pattern_measuring_objective",
        alias="pattern-measuring-objective",
        behavior_family="objective",
        mechanism_family="chart_pattern",
        dependency_group="dep_pattern",
        requires=REQUIRES_TABLE["pattern_measuring_objective"],
        supported_variants=VARIANT_TABLE["pattern_measuring_objective"],
        default_variant="head_shoulders",
        warmup=21,
        observer_fn=observe_measuring,
        takes_variant=True,
        version="v1",
    ),
    ExpertSpec(
        expert_id="range_breakout_1to1",
        alias="range-breakout-1to1",
        behavior_family="breakout",
        mechanism_family="range",
        dependency_group="dep_breakout",
        requires=REQUIRES_TABLE["range_breakout_1to1"],
        supported_variants=(),
        default_variant=None,
        warmup=100,
        observer_fn=observe_range_breakout,
        version="v1",
    ),
    ExpertSpec(
        expert_id="rsi_stoch_reversion",
        alias="rsi-stoch-reversion",
        behavior_family="mean_reversion",
        mechanism_family="oscillator",
        dependency_group="dep_reversion",
        requires=REQUIRES_TABLE["rsi_stoch_reversion"],
        supported_variants=(),
        default_variant=None,
        warmup=21,
        observer_fn=observe_rsi_reversion,
        version="v1",
    ),
    ExpertSpec(
        expert_id="trend_pullback",
        alias="trend-pullback",
        behavior_family="trend_following",
        mechanism_family="momentum",
        dependency_group="dep_trend",
        requires=REQUIRES_TABLE["trend_pullback"],
        supported_variants=(),
        default_variant=None,
        warmup=20,
        observer_fn=observe_trend_pullback,
        version="v1",
    ),
    ExpertSpec(
        expert_id="trend_pullback_depth",
        alias="trend-pullback-depth",
        behavior_family="trend_following",
        mechanism_family="momentum",
        dependency_group="dep_trend",
        requires=REQUIRES_TABLE["trend_pullback_depth"],
        supported_variants=(),
        default_variant=None,
        warmup=21,
        observer_fn=observe_trend_depth,
        version="v1",
    ),
    ExpertSpec(
        expert_id="volume_climax_reversal",
        alias="volume-climax-reversal",
        behavior_family="climax",
        mechanism_family="volume",
        dependency_group="dep_volume",
        requires=REQUIRES_TABLE["volume_climax_reversal"],
        supported_variants=(),
        default_variant=None,
        warmup=100,
        observer_fn=observe_volume_climax,
        version="v1",
    ),
    ExpertSpec(
        expert_id="volume_confirmed_breakout",
        alias="volume-confirmed-breakout",
        behavior_family="breakout",
        mechanism_family="volume",
        dependency_group="dep_volume",
        requires=REQUIRES_TABLE["volume_confirmed_breakout"],
        supported_variants=(),
        default_variant=None,
        warmup=21,
        observer_fn=observe_volume_breakout,
        version="v1",
    ),
)

# Registry mapping canonical ID and alias to ExpertSpec.
EXPERT_REGISTRY: dict[str, ExpertSpec] = {}
for _spec in _EXPERT_SPECS_LIST:
    EXPERT_REGISTRY[_spec.expert_id] = _spec
    EXPERT_REGISTRY[_spec.alias] = _spec


def get_expert(expert_id: str) -> ExpertSpec:
    """Retrieve an expert specification by canonical name or alias. Fails closed."""
    if expert_id not in EXPERT_REGISTRY:
        raise KeyError(f"unregistered expert: {expert_id!r}")
    return EXPERT_REGISTRY[expert_id]


def validate_variant_overrides(overrides: Mapping[str, str]) -> None:
    """Validate variant overrides against VARIANT_TABLE, matching Rust mod.rs semantics."""
    for expert_id, variant in overrides.items():
        canonical = expert_id.replace("-", "_") if "-" in expert_id else expert_id
        if canonical not in VARIANT_TABLE:
            raise ValueError(
                f"variant override for {expert_id!r} is unsupported: "
                "this family has no explicit variant dispatch"
            )
        allowed = VARIANT_TABLE[canonical]
        if variant not in allowed:
            raise ValueError(
                f"unsupported variant {variant!r} for {expert_id!r}; expected one of {allowed!r}"
            )


def verify_expert_implementation(expert_id: str) -> bool:
    """Dynamically verify that an expert observer is genuinely ported, callable, and fails closed."""
    if expert_id not in EXPERT_REGISTRY:
        return False
    spec = EXPERT_REGISTRY[expert_id]
    if not callable(spec.observer_fn):
        return False

    # Execute dynamic fail-closed contract verification on an empty/gap frame
    gap_frame = CausalFrame("test-instrument", 0, ())
    try:
        kwargs: dict[str, Any] = {}
        if spec.takes_variant and spec.default_variant is not None:
            kwargs["variant"] = spec.default_variant
        if spec.takes_readings:
            kwargs["readings"] = ()
        stance = spec.observer_fn(gap_frame, None, **kwargs)
        if not isinstance(stance, Stance) or stance.kind != StanceKind.ABSTAIN:
            return False
    except Exception:
        return False

    return True


def registry_rows() -> list[tuple[str, bool]]:
    """(expert_id, ported) rows dynamically verified for all 28 canonical experts."""
    return [(spec_id, verify_expert_implementation(spec_id)) for spec_id in CANONICAL_28_EXPERTS]


def observe_expert(
    expert_id: str,
    frame: CausalFrame,
    opportunity: Opportunity | None,
    *,
    variant: str | None = None,
    readings: tuple[PositioningReading, ...] = (),
) -> Stance:
    """Dispatch observation to a registered expert by ID or alias with authoritative metadata.

    Epistemic Demarcation & Adapter Pattern (matching Rust witness_adapter.rs):
    - Rejects unknown expert IDs with KeyError.
    - Rejects invalid variant overrides with ValueError.
    - Preserves causal frame integrity, warmup, and opportunity requirements.
    - Authoritatively applies registered metadata (expert_id, behavior_family,
      mechanism_family, dependency_group, version) onto the emitted Stance.
    """
    spec = get_expert(expert_id)
    canonical = spec.expert_id

    # Validate variant dispatch against VARIANT_TABLE
    if variant is not None:
        if canonical not in VARIANT_TABLE:
            raise ValueError(
                f"variant override for {expert_id!r} is unsupported: "
                "this family has no explicit variant dispatch"
            )
        if variant not in VARIANT_TABLE[canonical]:
            raise ValueError(
                f"unsupported variant {variant!r} for {expert_id!r}; "
                f"expected one of {VARIANT_TABLE[canonical]!r}"
            )

    chosen_variant = variant if variant is not None else spec.default_variant

    kwargs: dict[str, Any] = {}
    if spec.takes_variant and chosen_variant is not None:
        kwargs["variant"] = chosen_variant
    if spec.takes_readings:
        kwargs["readings"] = readings

    raw_stance = spec.observer_fn(frame, opportunity, **kwargs)

    # Authoritatively wrap and apply the canonical witness metadata, mirroring
    # Rust's LegacyExpertWitnessAdapter.observe().
    #
    # Built from the raw stance's own instance dict rather than
    # ``dataclasses.replace``: replace re-derives the field list and deep-copies
    # every unhashable/unknown value on each of the 28 x bars calls per bar
    # (measured: 499k replace calls, 1.88s cumulative over one portfolio run),
    # while the dict merge keeps every field -- including any field added later --
    # without either cost. Equivalence to the replace path is pinned by
    # ``test_perf_hot_paths``.
    metadata = {
        "observer_id": spec.expert_id,
        "behavior_family": spec.behavior_family,
        "mechanism_family": spec.mechanism_family,
        "dependency_group": spec.dependency_group,
        "version": f"{spec.expert_id}-witness-{spec.version}",
        "variant_id": chosen_variant or raw_stance.variant_id or "UNRESOLVED",
    }
    return type(raw_stance)(**{**raw_stance.__dict__, **metadata})


def observe_all_28(
    frame: CausalFrame,
    opportunity: Opportunity | None,
    *,
    readings: tuple[PositioningReading, ...] = (),
) -> tuple[Stance, ...]:
    """Observe an opportunity across all 28 canonical active experts with authoritative metadata.

    Per-bar shared feature bundle: one computation, 28 readers. The bundle is
    a pure function of this bar's frame, built lazily on first series access
    and cleared afterwards (per-bar invalidation, no cross-bar leakage).
    Quiet bars that never touch a series pay only the ContextVar set/reset.
    Misses fail closed to the unbundled path, so stances stay identical.
    """
    token = _ACTIVE_LAZY.set(_LazyBundle(frame))
    try:
        return tuple(
            observe_expert(spec_id, frame, opportunity, readings=readings)
            for spec_id in CANONICAL_28_EXPERTS
        )
    finally:
        _ACTIVE_LAZY.reset(token)
