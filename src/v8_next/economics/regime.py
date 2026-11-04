"""Descriptive causal regimes, never calibrated habitat or admission authority."""

from dataclasses import dataclass
from decimal import Decimal

from v8_next.domain.market import CausalFrame
from v8_next.domain.positioning import PositioningReading, positioning_at
from v8_next.economics.decisions import numeric
from v8_next.experts.features import adx_series, close_series, simple_atr_series


@dataclass(frozen=True)
class RegimeObservation:
    instrument_id: str
    decision_ns: int
    volume: str | None
    funding: str | None
    volume_ratio: str | None
    funding_rate: str | None
    version: str = "volume20-settled-funding-v1"
    trend: str | None = None
    volatility: str | None = None
    habitat_status: str = "UNQUALIFIED"


def observe_regime(
    frame: CausalFrame, *, readings: tuple[PositioningReading, ...] = ()
) -> RegimeObservation:
    """Current-inclusive 20 closed bars; funding retains provider expiry policy.

    Thresholds come from legacy quant.rs. Unlike its ablation caller, this
    version requires a full 20-bar window and never labels missing inputs normal.
    Trend uses SMA-seeded Wilder ADX14 and first-close EMA5/20.
    Volatility uses range14 divided by its full 49-value median, not true ATR.
    """
    ratio = None
    volume = None
    is_continuous = frame.continuous
    if is_continuous and len(frame.candles) >= 20:
        window = frame.candles[-20:]
        average = sum((c.volume for c in window), Decimal(0)) / 20
        if average > 0:
            ratio = window[-1].volume / average
            volume = (
                "VolumeExpansion"
                if ratio >= Decimal("1.30")
                else "VolumeDrought"
                if ratio <= Decimal("0.70")
                else "NormalVolume"
            )
    trend = None
    volatility = None
    if is_continuous and len(frame.candles) >= 28:
        highs = [float(c.high) for c in frame.candles]
        lows = [float(c.low) for c in frame.candles]
        closes = [float(c.close) for c in frame.candles]
        adxs = adx_series(highs, lows, closes, 14)
        latest_adx = numeric(adxs[-1])
        closes_series = close_series(frame)
        fast_ema = numeric(closes_series.ewm_mean(span=5, adjust=False)[-1])
        slow_ema = numeric(closes_series.ewm_mean(span=20, adjust=False)[-1])
        latest_close = closes[-1]
        if latest_adx > 20.0 and fast_ema > slow_ema and latest_close > fast_ema:
            trend = "BullTrend"
        elif latest_adx > 20.0 and fast_ema < slow_ema and latest_close < fast_ema:
            trend = "BearTrend"
        else:
            trend = "ChopRange"

    if is_continuous and len(frame.candles) >= 62:
        highs = [float(c.high) for c in frame.candles]
        lows = [float(c.low) for c in frame.candles]
        atrs = simple_atr_series(highs, lows, 14)
        # atrs starts at bar index 13.
        # Complete 49-value median, matching the legacy i-48..=i window
        atr_window = sorted(atrs[-49:])
        median_atr = atr_window[len(atr_window) // 2]
        latest_atr = atrs[-1]
        if median_atr > 0.0:
            vol_ratio = latest_atr / median_atr
            if vol_ratio >= 1.35:
                volatility = "HighVol"
            elif vol_ratio <= 0.75:
                volatility = "LowVolSqueeze"
            else:
                volatility = "NormalVol"

    decision_ns = frame.decision_ns
    rate = (
        positioning_at(readings, frame.instrument_id, "settled_funding_rate", decision_ns)
        if decision_ns
        else None
    )
    funding = None
    if rate is not None:
        funding = (
            "CrowdedLong"
            if rate >= Decimal("0.00015")
            else "CrowdedShort"
            if rate <= Decimal("-0.00015")
            else "NeutralFunding"
        )
    return RegimeObservation(
        frame.instrument_id,
        decision_ns,
        volume,
        funding,
        str(ratio) if ratio is not None else None,
        str(rate) if rate is not None else None,
        trend=trend,
        volatility=volatility,
        version="adx14-range14-median49-volume20-funding-v2",
    )
