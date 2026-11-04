"""Frozen exit geometry: economic policy only, never simulated fill logic."""

from collections.abc import Callable
from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from functools import partial

from v8_next.domain.market import CausalFrame
from v8_next.domain.positioning import PositioningReading
from v8_next.economics.decisions import Opportunity, Stance, StanceKind, observe_squeeze
from v8_next.experts.bollinger import band_setup
from v8_next.experts.breakouts import (
    last_close_breakout,
    observe_failed_breakout,
    observe_volume_breakout,
)
from v8_next.experts.candlestick import VARIANTS as CANDLE_VARIANTS
from v8_next.experts.candlestick import candle_pattern
from v8_next.experts.climax import observe_volume_climax
from v8_next.experts.confluence import observe_confluence
from v8_next.experts.divergence import divergence_setup, observe_divergence
from v8_next.experts.failed_moves import failed_move, observe_failed_move
from v8_next.experts.features import close_series, significant_swings
from v8_next.experts.fibonacci import fib_impulse, observe_fib_projection, observe_fib_retracement
from v8_next.experts.gaps import gap_setup
from v8_next.experts.ichimoku import observe_ichimoku
from v8_next.experts.levels import (
    daily_pivots,
    observe_floor_pivot,
    observe_range_breakout,
    previous_session_bars,
)
from v8_next.experts.measuring import VARIANTS, measuring_setup
from v8_next.experts.momentum import observe_macd_stoch, observe_obv_adl
from v8_next.experts.pandf import pandf_setup
from v8_next.experts.patterns import pattern_retest_setup
from v8_next.experts.positioning import observe_funding, observe_open_interest
from v8_next.experts.profile import observe_profile, tpo_profile
from v8_next.experts.reclaim import observe_breakout_retest, observe_liquidity_reclaim
from v8_next.experts.reversion import (
    bollinger_fade_geometry,
    observe_bollinger_reversion,
    observe_rsi_reversion,
)
from v8_next.experts.trend import observe_trend_depth, observe_trend_pullback

PROTECTION_POLICIES = frozenset(
    {
        "timeout-only-v1",
        "squeeze:baseline:v2",
        "squeeze:m1:v2",
        "squeeze:m2:v2",
        "squeeze:m3:v2",
        "donchian:a:v2",
        "trend-pullback:a:v2",
        "trend-depth:a:v2",
        "rsi-reversion:a:v2",
        "volume-breakout:active:v2",
        "failed-breakout:a:v2",
        "bollinger-reversion:a:v2",
        "obv-adl:active:v2",
        "macd-stoch:active:v2",
        "volume-climax:active:v2",
        "ichimoku:cross:v2",
        "fib-retracement:a:v2",
        "fib-projection:a:v2",
        "confluence:a:v2",
        "confluence:b:v2",
        "floor-pivot:a:v2",
        "range-breakout:a:v2",
        "divergence:a:v2",
        "divergence:b:v2",
        "liquidity-reclaim:a:v2",
        "breakout-retest:a:v2",
        "breakout-retest:b:v2",
        "breakout-retest:c:v2",
        *(f"funding:{v}:v2" for v in "abcd"),
        *(f"open-interest:{v}:v2" for v in "abcd"),
        *(f"profile:{v}:v2" for v in "abcd"),
        *(f"failed-move:{v}:v2" for v in "bcdefg"),
        *(f"gap:{v}:v2" for v in "abc"),
        *(f"candlestick:{v}:v2" for v in CANDLE_VARIANTS),
        *(f"pandf:{v}:v2" for v in "abcd"),
        *(f"bollinger:{v}:v2" for v in "abc"),
        *(f"measuring:{v}:v2" for v in VARIANTS),
    }
)


@dataclass(frozen=True)
class CampaignProtection:
    policy: str
    opportunity_id: str
    instrument_id: str
    direction: str
    observed_ns: int
    expires_ns: int
    stop_price: Decimal
    target_price: Decimal
    close_invalidation_price: Decimal | None = None
    live_channel_bars: int | None = None
    validity_indicator: str | None = None
    close_breach_price: Decimal | None = None

    def __post_init__(self) -> None:
        if self.close_breach_price is not None and (
            not self.close_breach_price.is_finite() or self.close_breach_price <= 0
        ):
            raise ValueError("invalid strict close breach reference")
        if self.validity_indicator is not None and (
            self.validity_indicator
            not in {"kijun26", "ema5-above-ema20", "macd-zero", "rsi14-reversion"}
            or self.live_channel_bars is not None
            or (
                self.close_invalidation_price is not None
                and self.validity_indicator not in {"ema5-above-ema20", "rsi14-reversion"}
            )
        ):
            raise ValueError("unknown or ambiguous indicator validity")
        if self.live_channel_bars is not None and (
            type(self.live_channel_bars) is not int
            or self.live_channel_bars <= 0
            or self.close_invalidation_price is not None
        ):
            raise ValueError("invalid or ambiguous live channel validity")
        if self.close_invalidation_price is not None and (
            not self.close_invalidation_price.is_finite() or self.close_invalidation_price <= 0
        ):
            raise ValueError("invalid close invalidation reference")
        if self.policy not in PROTECTION_POLICIES or self.policy == "timeout-only-v1":
            raise ValueError("unknown protected campaign policy")
        if self.direction not in {"LONG", "SHORT"} or self.expires_ns <= self.observed_ns:
            raise ValueError("invalid protection direction or clocks")
        if any(not p.is_finite() or p <= 0 for p in (self.stop_price, self.target_price)):
            raise ValueError("invalid protection prices")
        if (self.target_price - self.stop_price) * (1 if self.direction == "LONG" else -1) <= 0:
            raise ValueError("inverted protection")

#: Maximum holding horizon a protection may open, in bars, per family. A
#: walk-forward plan derives its purge/embargo from this instead of a literal.
PROTECTION_TTL_BARS: dict[str, int] = {"squeeze": 336, "_default": 8}


def protection_at(
    frame: CausalFrame,
    opportunity: Opportunity,
    policy: str,
    tick: Decimal,
    *,
    readings: tuple[PositioningReading, ...] = (),
) -> CampaignProtection | None:
    if policy not in PROTECTION_POLICIES:
        raise ValueError("unknown campaign policy")
    if policy == "timeout-only-v1":
        return None
    if not tick.is_finite() or tick <= 0:
        raise ValueError("positive venue tick required")
    if (
        opportunity.instrument_id != frame.instrument_id
        or opportunity.anchor_ns > frame.decision_ns
    ):
        raise ValueError("protection outside opportunity domain")
    if not frame.continuous or not frame.candles or opportunity.direction not in {"LONG", "SHORT"}:
        return None
    family, variant, _ = policy.split(":")
    sign = 1 if opportunity.direction == "LONG" else -1
    close = frame.candles[-1].close
    invalidation_price = None
    breach_price = None
    unit_geometry_observers: dict[str, Callable[[CausalFrame, Opportunity | None], Stance]] = {
        "trend-pullback": observe_trend_pullback,
        "trend-depth": observe_trend_depth,
        "rsi-reversion": observe_rsi_reversion,
        "volume-breakout": observe_volume_breakout,
        "obv-adl": observe_obv_adl,
        "macd-stoch": observe_macd_stoch,
        "volume-climax": observe_volume_climax,
        "fib-retracement": observe_fib_retracement,
        "fib-projection": observe_fib_projection,
        "confluence": partial(observe_confluence, variant=variant),
        "divergence": partial(observe_divergence, variant=variant),
        "failed-move": partial(observe_failed_move, variant=variant),
    }
    if family == "squeeze":
        if observe_squeeze(frame, opportunity, variant=variant).kind != StanceKind.SUPPORT:
            return None
        # Legacy state::atr_series is mean high-low range, not Wilder true range.
        span = sum((c.high - c.low for c in frame.candles[-14:]), Decimal(0)) / 14
        if span <= 0:
            return None
        stop, target = close - sign * 2 * span, close + sign * 4 * span
    elif family in unit_geometry_observers:
        observer = unit_geometry_observers[family]
        if len(frame.candles) < 14 or observer(frame, opportunity).kind != StanceKind.SUPPORT:
            return None
        span = sum((c.high - c.low for c in frame.candles[-14:]), Decimal(0)) / 14
        if span <= 0:
            return None
        if family in {"obv-adl", "volume-climax"}:
            invalidation_price = frame.candles[-1].low if sign == 1 else frame.candles[-1].high
        elif family == "volume-breakout":
            prior = frame.candles[-21:-1]
            invalidation_price = (
                max(c.high for c in prior) if sign == 1 else min(c.low for c in prior)
            )
        if family in {"fib-retracement", "fib-projection"}:
            impulse = fib_impulse(frame)
            assert impulse is not None
            invalidation_price = (
                impulse.retracement(Decimal(".786"))
                if family == "fib-retracement"
                else impulse.extension(Decimal("1.618"))
            )
        if family == "failed-move":
            failure = failed_move(frame, variant)
            assert failure is not None
            invalidation_price = failure.reference
        if family == "divergence":
            divergence = divergence_setup(frame, variant)
            assert divergence is not None
            # Both strict same-side constraints reduce to the tighter level.
            invalidation_price = (
                max(divergence.barrier, divergence.extremum)
                if sign == 1
                else min(divergence.barrier, divergence.extremum)
            )
        if family == "confluence":
            impulse = fib_impulse(frame)
            assert impulse is not None
            breach_price = impulse.retracement(Decimal(".786"))
            closes = close_series(frame).tail(20)
            invalidation_price = Decimal(str(closes.mean())) - sign * 3 * Decimal(
                str(closes.std(ddof=0))
            )
        if family == "trend-depth":
            _, low_index = significant_swings(frame)
            assert low_index is not None
            invalidation_price = frame.candles[low_index].low
        # Active Rust v1 declares one range unit each way, not its alternate
        # v2 structural stop. Execution remains V8-next frozen absolute geometry.
        stop, target = close - sign * span, close + sign * span
    elif family in {"funding", "open-interest"}:
        positioning_observer = observe_funding if family == "funding" else observe_open_interest
        if (
            len(frame.candles) < 14
            or positioning_observer(frame, opportunity, variant=variant, readings=readings).kind
            != StanceKind.SUPPORT
        ):
            return None
        span = sum((c.high - c.low for c in frame.candles[-14:]), Decimal(0)) / 14
        if span <= 0:
            return None
        if family == "open-interest":
            window = frame.candles[-5:]
        elif variant == "d":
            window = frame.candles[-10:]
        else:
            window = frame.candles[-6:-1]
        stop = min(c.low for c in window) if sign == 1 else max(c.high for c in window)
        invalidation_price = stop
        if family == "funding" and variant == "d":
            stop -= sign * span
        target = close + sign * span
    elif family in {"liquidity-reclaim", "breakout-retest"}:
        reclaim_observer = (
            observe_liquidity_reclaim
            if family == "liquidity-reclaim"
            else partial(observe_breakout_retest, variant=variant)
        )
        if (
            len(frame.candles) < 14
            or reclaim_observer(frame, opportunity).kind != StanceKind.SUPPORT
        ):
            return None
        span = sum((c.high - c.low for c in frame.candles[-14:]), Decimal(0)) / 14
        if span <= 0:
            return None
        if family == "liquidity-reclaim":
            prior = frame.candles[:-1]
            stop = min(c.low for c in prior) if sign == 1 else max(c.high for c in prior)
            invalidation_price = stop
        elif variant != "a":
            setup = pattern_retest_setup(frame, variant)
            assert setup is not None
            invalidation_price = setup.level
            risk_distance = min(
                Decimal(2) * span, max(Decimal(".8") * span, (close - setup.stop_reference) * sign)
            )
            stop = close - sign * risk_distance
        else:
            high_index, low_index = significant_swings(frame)
            index = high_index if sign == 1 else low_index
            assert index is not None
            level = frame.candles[index].high if sign == 1 else frame.candles[index].low
            invalidation_price = level
            reference = (
                min(frame.candles[-1].low, level - span)
                if sign == 1
                else max(frame.candles[-1].high, level + span)
            )
            risk_distance = min(
                Decimal(2) * span, max(Decimal(".8") * span, (close - reference) * sign)
            )
            stop = close - sign * risk_distance
        if family == "breakout-retest" and variant != "a":
            setup = pattern_retest_setup(frame, variant)
            assert setup is not None
            target = close + sign * abs(setup.extreme - setup.level)
        else:
            target = close + sign * span
    elif family == "profile":
        if observe_profile(frame, opportunity, variant=variant).kind != StanceKind.SUPPORT:
            return None
        previous = previous_session_bars(frame)
        assert previous is not None
        bucket = sum((c.high - c.low for c in frame.candles[-14:]), Decimal(0)) / 14
        profile = tpo_profile(previous, bucket)
        if profile is None:
            return None
        if variant == "c":
            invalidation_price = profile.poc
            stop = profile.value_low if sign == 1 else profile.value_high
            target = profile.day_high if sign == 1 else profile.day_low
        else:
            stop = profile.day_low if sign == 1 else profile.day_high
            invalidation_price = stop
            target = profile.poc
    elif family == "floor-pivot":
        if observe_floor_pivot(frame, opportunity).kind != StanceKind.SUPPORT:
            return None
        levels = daily_pivots(frame)
        assert levels is not None
        stop = levels.pivot
        invalidation_price = levels.pivot
        target = levels.resistance1 if sign == 1 else levels.support1
    elif family == "range-breakout":
        if observe_range_breakout(frame, opportunity).kind != StanceKind.SUPPORT:
            return None
        prior = frame.candles[-21:-1]
        invalidation_price = max(c.high for c in prior) if sign == 1 else min(c.low for c in prior)
        height = max(c.high for c in prior) - min(c.low for c in prior)
        stop, target = close - sign * height, close + sign * height
    elif family == "ichimoku":
        if observe_ichimoku(frame, opportunity).kind != StanceKind.SUPPORT:
            return None
        window = frame.candles[-26:]
        kijun = (max(c.high for c in window) + min(c.low for c in window)) / 2
        span = sum((c.high - c.low for c in frame.candles[-14:]), Decimal(0)) / 14
        if span <= 0:
            return None
        distance = min(Decimal(2) * span, max(Decimal(".8") * span, abs(close - kijun)))
        stop, target = close - sign * distance, close + sign * Decimal("1.5") * span
    elif family == "bollinger-reversion":
        if observe_bollinger_reversion(frame, opportunity).kind != StanceKind.SUPPORT:
            return None
        fade = bollinger_fade_geometry(frame)
        if fade is None:
            return None
        stop, target = close - sign * fade.distance, close + sign * fade.distance
        invalidation_price = fade.invalidation_price
    elif family == "failed-breakout":
        if (
            len(frame.candles) < 14
            or observe_failed_breakout(frame, opportunity).kind != StanceKind.SUPPORT
        ):
            return None
        breakout = last_close_breakout(frame)
        assert breakout is not None
        stop = breakout[1]
        invalidation_price = stop
        span = sum((c.high - c.low for c in frame.candles[-14:]), Decimal(0)) / 14
        if span <= 0:
            return None
        target = close - span
    elif family in {"donchian", "gap", "candlestick"}:
        if len(frame.candles) < 14:
            return None
        span = sum((c.high - c.low for c in frame.candles[-14:]), Decimal(0)) / 14
        if span <= 0:
            return None
        if family == "donchian":
            if sign != 1 or len(frame.candles) < 21:
                return None
            prior = frame.candles[-21:-1]
            if close <= max(c.high for c in prior):
                return None
            stop = min(c.low for c in prior)
        elif family == "gap":
            gap = gap_setup(frame, variant)
            if gap is None or gap.direction != opportunity.direction:
                return None
            stop = gap.stop_reference
            invalidation_price = gap.bottom if sign == 1 else gap.top
        else:
            candle = candle_pattern(frame, variant)
            if candle is None or candle.direction != opportunity.direction:
                return None
            invalidation_price = candle.trigger_reference
            raw_distance = (close - candle.stop_reference) * sign
            if raw_distance <= 0:
                return None
            distance = min(Decimal(2) * span, max(Decimal(".8") * span, raw_distance))
            stop = close - sign * distance
        target = close + sign * span
    elif family == "pandf":
        pf = pandf_setup(frame, variant)
        if pf is None or pf.direction != opportunity.direction:
            return None
        stop, target = pf.stop, pf.target
        invalidation_price = pf.stop
    elif family == "measuring":
        measured = measuring_setup(frame, variant)
        if measured is None or measured.direction != opportunity.direction:
            return None
        stop, target = measured.stop_reference, close + sign * measured.target_distance
        invalidation_price = measured.level
    else:
        band = band_setup(frame, variant)
        if band is None or band.direction != opportunity.direction:
            return None
        invalidation_price = Decimal(str(band.mid_reference))
        if variant != "a":
            invalidation_price += sign * 2 * Decimal(str(band.sd_reference))
        span = Decimal(str(band.mean_range_reference))
        stop = close - sign * Decimal(str(band.stop_r)) * span
        target = close + sign * Decimal(str(band.target_r)) * span
    # Tighten stop and round target toward entry, never silently enlarge price risk.
    stop = (stop / tick).to_integral_value(
        rounding=ROUND_CEILING if sign == 1 else ROUND_FLOOR
    ) * tick
    target = (target / tick).to_integral_value(
        rounding=ROUND_FLOOR if sign == 1 else ROUND_CEILING
    ) * tick
    if stop <= 0 or target <= 0 or (close - stop) * sign <= 0 or (target - close) * sign <= 0:
        return None
    duration = frame.candles[-1].end_ns - frame.candles[-1].start_ns
    if any(c.end_ns - c.start_ns != duration for c in frame.candles):
        raise ValueError("protection requires regular bars")
    expires = min(
        opportunity.expires_ns,
        frame.candles[-1].end_ns + PROTECTION_TTL_BARS.get(family, PROTECTION_TTL_BARS["_default"]) * duration,
    )
    if expires <= frame.decision_ns:
        return None
    return CampaignProtection(
        policy,
        opportunity.opportunity_id,
        frame.instrument_id,
        opportunity.direction,
        frame.decision_ns,
        expires,
        stop,
        target,
        invalidation_price,
        20 if family == "donchian" else None,
        "kijun26"
        if family == "ichimoku"
        else (
            "ema5-above-ema20"
            if family in {"trend-pullback", "trend-depth"}
            else "macd-zero"
            if family == "macd-stoch"
            else "rsi14-reversion"
            if family in {"rsi-reversion", "confluence"}
            else None
        ),
        breach_price,
    )
