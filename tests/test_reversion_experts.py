from dataclasses import replace
from decimal import Decimal

import polars as pl
import pytest

from v8_next.domain.market import Candle, CausalFrame
from v8_next.economics.decisions import Opportunity, StanceKind
from v8_next.experts.features import wilder_rsi
from v8_next.experts.reversion import (
    bollinger_direction,
    observe_bollinger_reversion,
    observe_rsi_reversion,
)


def context(closes):
    bars = tuple(
        Candle(
            "instrument",
            i,
            i + 1,
            Decimal(c),
            Decimal(c) + Decimal("0.1"),
            Decimal(c) - Decimal("0.1"),
            Decimal(c),
            Decimal(10),
            i + 1,
            i + 1,
            "fixture",
        )
        for i, c in enumerate(closes)
    )
    return CausalFrame("instrument", len(bars), bars), Opportunity(
        "o", "e", "instrument", "LONG", len(bars), len(bars) + 10
    )


@pytest.mark.parametrize(
    "price,expected", [(102, "SHORT"), (103, None), (98, "LONG"), (97, None), (100, None)]
)
def test_band_boundaries(price, expected):
    assert bollinger_direction(price, 100, 1) == expected


def test_actual_bands_fade_both_directions_and_flat_abstains():
    for sign, side in [(1, "SHORT"), (-1, "LONG")]:
        values = [100 + (-1) ** i for i in range(19)] + [100 + sign * 3]
        frame, opportunity = context(values)
        stance = observe_bollinger_reversion(frame, replace(opportunity, direction=side))
        assert stance.kind == StanceKind.SUPPORT
    frame, opportunity = context([100] * 20)
    assert observe_bollinger_reversion(frame, opportunity).reason == "DEGENERATE_FEATURE"


def test_wilder_seed_recurrence_and_prefix_causality():
    prices = [100.0 + (i % 7) - (i % 3) for i in range(45)]
    result = wilder_rsi(pl.Series(prices))
    assert result[:14] == (None,) * 14
    changes = [b - a for a, b in zip(prices, prices[1:], strict=False)]
    gain = sum(max(d, 0) for d in changes[:14]) / 14
    loss = sum(max(-d, 0) for d in changes[:14]) / 14
    expected = [100 - 100 / (1 + gain / loss)]
    for d in changes[14:]:
        gain = (13 * gain + max(d, 0)) / 14
        loss = (13 * loss + max(-d, 0)) / 14
        expected.append(100 - 100 / (1 + gain / loss))
    assert result[14:] == pytest.approx(expected)
    assert wilder_rsi(pl.Series(prices[:25])) == result[:25]
    assert wilder_rsi(pl.Series([100.0] * 21))[-1] == 50


def test_rsi_requires_recovery_then_break_of_signal_extreme():
    # Twenty declines establish oversold; a recovery candle alone cannot break
    # its own high. Subsequent closes must cross that signal candle's extreme.
    prices = list(range(120, 99, -1)) + [105, 107, 110]
    frame, opportunity = context(prices)
    assert observe_rsi_reversion(frame, opportunity).kind == StanceKind.SUPPORT
    signal_frame, signal_opportunity = context(prices[:-2])
    assert observe_rsi_reversion(signal_frame, signal_opportunity).kind == StanceKind.ABSTAIN
    short_frame, short_opportunity = context([240 - p for p in prices])
    assert (
        observe_rsi_reversion(short_frame, replace(short_opportunity, direction="SHORT")).kind
        == StanceKind.SUPPORT
    )


@pytest.mark.parametrize("observe", [observe_bollinger_reversion, observe_rsi_reversion])
def test_observer_context_rejects_cross_instrument_and_abstains_gaps(observe):
    frame, opportunity = context(list(range(100, 125)))
    with pytest.raises(ValueError, match="outside observer domain"):
        observe(frame, replace(opportunity, instrument_id="other"))
    gap = replace(frame, candles=frame.candles[:3] + frame.candles[4:])
    assert observe(gap, opportunity).reason == "SOURCE_GAP"
    assert observe(frame, None).kind == StanceKind.ABSTAIN


def test_rsi_campaign_geometry_tracks_recovery_direction():
    from v8_next.economics.protection import protection_at

    prices = list(range(120, 99, -1)) + [105, 107, 110]
    for values, direction in [(prices, "LONG"), ([240 - p for p in prices], "SHORT")]:
        frame, opportunity = context(values)
        protection = protection_at(
            frame, replace(opportunity, direction=direction), "rsi-reversion:a:v2", Decimal(".01")
        )
        assert protection is not None
        assert protection.validity_indicator == "rsi14-reversion"
        sign = 1 if direction == "LONG" else -1
        close = frame.candles[-1].close
        assert (close - protection.stop_price) * sign == Decimal(".2")
        assert (protection.target_price - close) * sign == Decimal(".2")
        assert protection.expires_ns == frame.decision_ns + 8
    frame, opportunity = context(prices[:-2])
    assert protection_at(frame, opportunity, "rsi-reversion:a:v2", Decimal(".01")) is None


def test_bollinger_fade_protection_uses_clamped_sigma_not_unit_range():
    from v8_next.economics.protection import protection_at

    for sign, direction in [(1, "SHORT"), (-1, "LONG")]:
        values = [100 + (-1) ** i for i in range(19)] + [100 + sign * 3]
        frame, opportunity = context(values)
        protection = protection_at(
            frame,
            replace(opportunity, direction=direction),
            "bollinger-reversion:a:v2",
            Decimal(".01"),
        )
        assert protection is not None
        from v8_next.experts.features import close_series

        series = close_series(frame)
        expected = Decimal(str(series.mean())) + sign * 3 * Decimal(str(series.std(ddof=0)))
        assert abs(protection.close_invalidation_price - expected) < Decimal("1e-12")
        # Sigma exceeds 2 * mean range (.2); geometry is clamped to .4.
        assert abs(frame.candles[-1].close - protection.stop_price) == Decimal(".4")
        assert abs(protection.target_price - frame.candles[-1].close) == Decimal(".4")


def test_fade_run_keeps_anchor_volatility_when_later_bar_range_changes():
    from v8_next.experts.reversion import bollinger_fade_distance

    frame, _ = context([100 + (-1) ** i for i in range(19)] + [103, Decimal("103.2")])
    assert bollinger_fade_distance(frame) == Decimal(".4")
    changed = replace(
        frame,
        candles=frame.candles[:-1]
        + (replace(frame.candles[-1], high=Decimal(120), low=Decimal(90)),),
    )
    assert bollinger_fade_distance(changed) == Decimal(".4")
