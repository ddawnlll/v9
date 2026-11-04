"""Authoritative catalog and observation entry point for all 28 canonical active experts.

Epistemic Demarcation:
Experts are epistemic witnesses, NOT economic sovereigns.
They observe pre-existing Opportunities and emit typed evidence stances.
They have ZERO capital, portfolio, or execution authority.
"""

from collections.abc import Callable
from functools import partial

from v8_next.domain.market import CausalFrame
from v8_next.domain.positioning import PositioningReading
from v8_next.economics.decisions import Opportunity, Stance, observe_squeeze
from v8_next.experts.bollinger import observe_bollinger_breakout
from v8_next.experts.breakouts import observe_failed_breakout, observe_volume_breakout
from v8_next.experts.candlestick import VARIANTS, observe_candlestick
from v8_next.experts.climax import observe_volume_climax
from v8_next.experts.confluence import observe_confluence
from v8_next.experts.divergence import observe_divergence
from v8_next.experts.donchian import observe_donchian
from v8_next.experts.failed_moves import observe_failed_move
from v8_next.experts.fibonacci import observe_fib_projection, observe_fib_retracement
from v8_next.experts.gaps import observe_gap
from v8_next.experts.ichimoku import observe_ichimoku
from v8_next.experts.levels import observe_floor_pivot, observe_range_breakout
from v8_next.experts.measuring import observe_measuring
from v8_next.experts.momentum import observe_macd_stoch, observe_obv_adl
from v8_next.experts.pandf import observe_pandf
from v8_next.experts.positioning import observe_funding, observe_open_interest
from v8_next.experts.profile import observe_profile
from v8_next.experts.reclaim import observe_breakout_retest, observe_liquidity_reclaim
from v8_next.experts.registry import (
    CANONICAL_28_EXPERTS,
    EXPERT_REGISTRY,
    REQUIRES_TABLE,
    VARIANT_TABLE,
    ExpertSpec,
    get_expert,
    observe_all_28,
    observe_expert,
    registry_rows,
    validate_variant_overrides,
)
from v8_next.experts.reversion import observe_bollinger_reversion, observe_rsi_reversion
from v8_next.experts.trend import observe_trend_depth, observe_trend_pullback

__all__ = [
    "CANONICAL_28_EXPERTS",
    "EXPERT_REGISTRY",
    "OBSERVERS",
    "REQUIRES_TABLE",
    "VARIANT_TABLE",
    "ExpertSpec",
    "Observer",
    "get_expert",
    "observe_all",
    "observe_all_28",
    "observe_expert",
    "registry_rows",
    "validate_variant_overrides",
]

Observer = Callable[[CausalFrame, Opportunity | None], Stance]
OBSERVERS: tuple[Observer, ...] = (
    observe_squeeze,
    *(partial(observe_pandf, variant=v) for v in ("a", "b", "c", "d")),
    *(partial(observe_divergence, variant=v) for v in ("a", "b")),
    *(partial(observe_measuring, variant=v) for v in ("head_shoulders", "double_top", "triangle")),
    *(partial(observe_failed_move, variant=v) for v in ("b", "c", "d", "e", "f", "g")),
    *(partial(observe_profile, variant=v) for v in ("a", "b", "c", "d")),
    observe_volume_climax,
    *(partial(observe_confluence, variant=v) for v in ("a", "b")),
    observe_fib_retracement,
    observe_fib_projection,
    observe_obv_adl,
    observe_macd_stoch,
    observe_floor_pivot,
    observe_range_breakout,
    observe_ichimoku,
    *(partial(observe_gap, variant=v) for v in ("a", "b", "c")),
    *(partial(observe_bollinger_breakout, variant=v) for v in ("a", "b", "c")),
    observe_candlestick,
    *(partial(observe_candlestick, variant=v) for v in VARIANTS),
    observe_donchian,
    observe_bollinger_reversion,
    observe_rsi_reversion,
    observe_failed_breakout,
    observe_volume_breakout,
    observe_trend_pullback,
    observe_trend_depth,
    observe_liquidity_reclaim,
    observe_breakout_retest,
    partial(observe_breakout_retest, variant="b"),
    partial(observe_breakout_retest, variant="c"),
)


def observe_all(
    frame: CausalFrame,
    opportunity: Opportunity | None,
    *,
    readings: tuple[PositioningReading, ...] = (),
) -> tuple[Stance, ...]:
    """Inspect existing opportunities across all observers; adding observers cannot create episodes."""
    return tuple(observer(frame, opportunity) for observer in OBSERVERS) + tuple(
        observer(frame, opportunity, variant=variant, readings=readings)
        for observer in (observe_funding, observe_open_interest)
        for variant in ("a", "b", "c", "d")
    )
