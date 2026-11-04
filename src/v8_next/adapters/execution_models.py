"""Explicit NautilusTrader execution models for v8-next.

Every knob NautilusTrader exposes for simulated execution is materialised here
as a named, digestible *profile* instead of being left at engine defaults. The
previous behaviour (``fill_model=None, fee_model=None, latency_model=None``)
silently meant "fill the full size at the bar price, zero slippage, zero
latency" — an assumption that was never written down, never versioned, and could
not be cited in an execution claim.

Determinism contract
--------------------
Each fill model is constructed with a pinned ``random_seed`` and the whole
profile is hashed by :func:`profile_digest`. The digest is what a receipt cites,
so two runs that claim the same execution semantics provably used the same
parameters. Any profile with a stochastic fill model is still reproducible
because the seed is part of the digest.

Honesty contract
----------------
These are *model assumptions*, not measurements. A profile name is reported
verbatim (e.g. ``realistic``), its parameters are published in the digest, and
nothing here may be presented as venue-observed execution. Venue truth requires
real fills (G8 ``UNRUN_NO_VENUE_ACCOUNT`` stays unresolved by construction).

Measured limits of the available knobs (real tape, bar data, default L1 book):

* ``bar_execution`` changes execution (fills 5 -> 0 when disabled) and is wired;
* ``fill_model`` changes fill prices (slippage 0.0 -> 0.0004 bps measured);
* ``liquidity_consumption``, ``queue_position``, ``use_market_order_acks`` and
  ``price_protection_points`` produce **no observable difference** without L2/L3
  depth. They are still accepted and forwarded, and the summary reports them as
  ``inert_knobs_without_depth_data`` so a profile cannot imply realism it is not
  delivering.

Measured again with a real captured L2 book (F2, issue #401): once a run really
receives order-book depth **and** the order flow can consume it, the two
depth-dependent knobs stop being inert -- ``liquidity_consumption`` bounds a
marketable limit's fill to the displayed size at the touch, and
``queue_position`` caps a resting limit's fill to the volume that traded after
the queue ahead of it was consumed. ``profile_summary`` therefore reports them as
``depth_dependent_knobs_active`` (with the measured evidence token) *only* when
``depth_data_available`` is set, which the portfolio backtest sets only when it
actually fed captured book deltas to that engine. The bar-only inert claim is
unchanged: on bar data the knobs are still inert.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from typing import Any

from nautilus_trader.execution import (
    DefaultFillModel,
    MakerTakerFeeModel,
    OneTickSlippageFillModel,
    SizeAwareFillModel,
    StaticLatencyModel,
    ThreeTierFillModel,
    TwoTierFillModel,
    VolumeSensitiveFillModel,
)
from nautilus_trader.model import LeveragedMarginModel, StandardMarginModel

# Fill models available in the installed NautilusTrader (2.0.0rc4). All share
# the constructor (prob_fill_on_limit, prob_slippage, random_seed).
FILL_MODEL_REGISTRY: dict[str, Any] = {
    "default": DefaultFillModel,
    "one_tick_slippage": OneTickSlippageFillModel,
    "two_tier": TwoTierFillModel,
    "three_tier": ThreeTierFillModel,
    "size_aware": SizeAwareFillModel,
    "volume_sensitive": VolumeSensitiveFillModel,
}

FILL_MODEL_IS_SLIPPED: dict[str, bool] = {
    "default": False,
    "one_tick_slippage": True,
    "two_tier": True,
    "three_tier": True,
    "size_aware": True,
    "volume_sensitive": True,
}

#: Knobs the engine accepts but that need an order book with displayed depth to
#: do anything. Measured on the real tape: with bar data and the default L1 book
#: both are inert, so a profile that enables them without depth data publishes
#: them as inert instead of claiming realism it cannot deliver. Measured with a
#: real captured L2 book (F2): both change the fill once depth is present, so the
#: summary promotes them to active only when ``depth_data_available`` is set.
DEPTH_DEPENDENT_KNOBS: tuple[str, ...] = ("liquidity_consumption", "queue_position")


@dataclass(frozen=True)
class ExecutionProfile:
    """A named, fully-specified simulated-execution configuration.

    Attributes are exactly the parameters passed to ``BacktestEngine.add_venue``
    so a profile can be published next to the result it produced.
    """

    name: str
    fill_model: str
    prob_fill_on_limit: float
    prob_slippage: float
    random_seed: int
    base_latency_nanos: int = 0
    insert_latency_nanos: int = 0
    update_latency_nanos: int = 0
    cancel_latency_nanos: int = 0
    fee_model: str = "maker_taker"
    #: Margin model for the simulated venue account. "none" keeps the engine
    #: default (no explicit margin model); "standard"/"leveraged" pass the NT
    #: model so margin accounting is venue-rule based instead of implicit.
    margin_model: str = "none"
    bar_execution: bool = True
    bar_adaptive_high_low_ordering: bool = False
    trade_execution: bool = True
    liquidity_consumption: bool = False
    queue_position: bool = False
    use_market_order_acks: bool = False
    price_protection_points: int | None = None
    #: Whether the caller has order-book depth (L2/L3) data for this run. The
    #: depth-dependent knobs are accepted by the engine but have no observable
    #: effect without a book to consume from, so the summary reports them as
    #: inert rather than letting a profile imply realism it cannot deliver.
    depth_data_available: bool = False

    def __post_init__(self) -> None:
        if self.fill_model not in FILL_MODEL_REGISTRY:
            raise ValueError(
                f"unknown fill_model {self.fill_model!r}; "
                f"known: {sorted(FILL_MODEL_REGISTRY)}"
            )
        if not 0.0 <= self.prob_fill_on_limit <= 1.0:
            raise ValueError("prob_fill_on_limit must be in [0, 1]")
        if not 0.0 <= self.prob_slippage <= 1.0:
            raise ValueError("prob_slippage must be in [0, 1]")
        if self.random_seed < 0:
            raise ValueError("random_seed must be >= 0 (determinism contract)")
        if self.margin_model not in ("none", "standard", "leveraged"):
            raise ValueError(
                f"unknown margin_model {self.margin_model!r}; "
                "known: none, standard, leveraged"
            )
        for field in (
            "base_latency_nanos",
            "insert_latency_nanos",
            "update_latency_nanos",
            "cancel_latency_nanos",
        ):
            if getattr(self, field) < 0:
                raise ValueError(f"{field} must be >= 0")


#: Named profiles. ``baseline`` reproduces the previous engine-default behaviour
#: but now *explicitly* and digestibly; the others add frictions.
PROFILES: dict[str, ExecutionProfile] = {
    "baseline": ExecutionProfile(
        name="baseline",
        fill_model="default",
        prob_fill_on_limit=1.0,
        prob_slippage=0.0,
        random_seed=0,
    ),
    "realistic": ExecutionProfile(
        name="realistic",
        fill_model="one_tick_slippage",
        prob_fill_on_limit=0.95,
        prob_slippage=1.0,
        random_seed=7,
        base_latency_nanos=1_000_000,
        liquidity_consumption=True,
        queue_position=True,
    ),
    "volume_aware": ExecutionProfile(
        name="volume_aware",
        fill_model="volume_sensitive",
        prob_fill_on_limit=0.95,
        prob_slippage=1.0,
        random_seed=7,
        base_latency_nanos=1_000_000,
        insert_latency_nanos=500_000,
        liquidity_consumption=True,
        queue_position=True,
    ),
}

DEFAULT_PROFILE = "baseline"


def resolve_profile(profile: str | ExecutionProfile) -> ExecutionProfile:
    """Accept a profile name or an explicit profile object."""
    if isinstance(profile, ExecutionProfile):
        return profile
    try:
        return PROFILES[profile]
    except KeyError:
        raise ValueError(
            f"unknown execution profile {profile!r}; known: {sorted(PROFILES)}"
        ) from None


def build_fill_model(profile: ExecutionProfile) -> Any:
    cls = FILL_MODEL_REGISTRY[profile.fill_model]
    return cls(
        prob_fill_on_limit=profile.prob_fill_on_limit,
        prob_slippage=profile.prob_slippage,
        random_seed=profile.random_seed,
    )


def build_latency_model(profile: ExecutionProfile) -> Any | None:
    """Return a static latency model, or None when every latency is zero."""
    if not any(
        (
            profile.base_latency_nanos,
            profile.insert_latency_nanos,
            profile.update_latency_nanos,
            profile.cancel_latency_nanos,
        )
    ):
        return None
    return StaticLatencyModel(
        base_latency_nanos=profile.base_latency_nanos,
        insert_latency_nanos=profile.insert_latency_nanos,
        update_latency_nanos=profile.update_latency_nanos,
        cancel_latency_nanos=profile.cancel_latency_nanos,
    )


def build_fee_model(profile: ExecutionProfile) -> Any:
    if profile.fee_model == "maker_taker":
        return MakerTakerFeeModel()
    raise ValueError(f"unknown fee_model {profile.fee_model!r}")


def build_margin_model(profile: ExecutionProfile) -> Any | None:
    """Margin model for ``add_venue``, or None to keep the engine default."""
    if profile.margin_model == "none":
        return None
    if profile.margin_model == "standard":
        return StandardMarginModel()
    if profile.margin_model == "leveraged":
        return LeveragedMarginModel()
    raise ValueError(f"unknown margin_model {profile.margin_model!r}")


def venue_kwargs(profile: str | ExecutionProfile) -> dict[str, Any]:
    """Keyword arguments for ``BacktestEngine.add_venue``.

    Note: ``base_currency``/``default_leverage``/``liquidation_enabled`` are
    supplied by the caller because they are account facts, not execution
    semantics.
    """
    p = resolve_profile(profile)
    return {
        "fill_model": build_fill_model(p),
        "fee_model": build_fee_model(p),
        "latency_model": build_latency_model(p),
        "margin_model": build_margin_model(p),
        "bar_execution": p.bar_execution,
        "bar_adaptive_high_low_ordering": p.bar_adaptive_high_low_ordering,
        "trade_execution": p.trade_execution,
        "liquidity_consumption": p.liquidity_consumption,
        "queue_position": p.queue_position,
        "use_market_order_acks": p.use_market_order_acks,
        "price_protection_points": p.price_protection_points,
    }


def profile_digest(profile: str | ExecutionProfile) -> str:
    """Stable sha256 over the profile's parameters (sorted keys)."""
    p = resolve_profile(profile)
    payload = json.dumps(asdict(p), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def profile_summary(profile: str | ExecutionProfile) -> dict[str, Any]:
    """Publishable description of the execution semantics in force."""
    p = resolve_profile(profile)
    enabled_depth_knobs = [k for k in DEPTH_DEPENDENT_KNOBS if getattr(p, k)]
    return {
        "profile": p.name,
        "digest": profile_digest(p),
        "fill_model": p.fill_model,
        "fill_model_slipped": FILL_MODEL_IS_SLIPPED[p.fill_model],
        "prob_fill_on_limit": p.prob_fill_on_limit,
        "prob_slippage": p.prob_slippage,
        "random_seed": p.random_seed,
        "latency_nanos": {
            "base": p.base_latency_nanos,
            "insert": p.insert_latency_nanos,
            "update": p.update_latency_nanos,
            "cancel": p.cancel_latency_nanos,
        },
        "fee_model": p.fee_model,
        "margin_model": p.margin_model,
        "bar_execution": p.bar_execution,
        "bar_adaptive_high_low_ordering": p.bar_adaptive_high_low_ordering,
        "trade_execution": p.trade_execution,
        "liquidity_consumption": p.liquidity_consumption,
        "queue_position": p.queue_position,
        "use_market_order_acks": p.use_market_order_acks,
        "price_protection_points": p.price_protection_points,
        "depth_data_available": p.depth_data_available,
        # Depth-dependent knobs are accepted by the engine either way; whether
        # they *do* anything depends on whether this run received book depth.
        # Measured on the real quad tape with bar data and the default L1 book:
        # bar_execution changes fills, but these knobs produce no observable
        # difference. Measured with a real captured L2 book (F2): both change the
        # fill. So they are published as active only when depth data is present,
        # and as inert otherwise -- a profile cannot imply realism it is not
        # delivering.
        "depth_dependent_knobs_enabled": enabled_depth_knobs,
        "depth_dependent_knobs_active": (
            enabled_depth_knobs if p.depth_data_available else []
        ),
        "inert_knobs_without_depth_data": (
            [] if p.depth_data_available else enabled_depth_knobs
        ),
        "depth_dependent_knobs_evidence": (
            "MEASURED_FILL_CHANGES_WITH_CAPTURED_L2_BOOK"
            if p.depth_data_available
            else "NOT_APPLICABLE_NO_DEPTH_DATA"
        ),
        # These are modelled assumptions, never venue-observed execution.
        "evidence_class": "MODELLED_EXECUTION_ASSUMPTION_NOT_VENUE_TRUTH",
    }
