"""Active MACD/stochastic and CMF/close-count regime observations."""

import polars as pl

from v8_next.domain.market import CausalFrame
from v8_next.economics.decisions import Opportunity, Stance, numeric, stance_with
from v8_next.experts.common import context_reason, directional_stance
from v8_next.experts.features import close_series, macd_line, trend_emas


def regime_hit(
    close: float, fast: float, slow: float, cmf: float, net: float
) -> tuple[str, str] | None:
    if cmf < -0.15 and close < slow:
        return "d", "LONG"
    if close < slow and close > fast and cmf > 0:
        return "c", "LONG"
    if close > slow and close < fast and cmf < 0:
        return "c", "SHORT"
    if close < slow and close <= fast and net >= 3 and cmf > 0:
        return "b", "LONG"
    if close > slow and close >= fast and net <= -3 and cmf < 0:
        return "b", "SHORT"
    if net >= 3 and cmf > 0 and fast > slow:
        return "a", "LONG"
    if net <= -3 and cmf < 0 and fast < slow:
        return "a", "SHORT"
    return None


def observe_obv_adl(frame: CausalFrame, opportunity: Opportunity | None) -> Stance:
    reason = context_reason(frame, opportunity, 20)
    hit = None
    if reason is None:
        values = frame.df.select(["close", "high", "low", "volume"]).tail(20)
        if not all(values[c].is_finite().all() for c in values.columns):
            raise ValueError("non-finite native feature input")
        total = numeric(values["volume"].sum())
        reason = "MISSING_VOLUME"
        if total > 0:
            flow = values.select(
                pl.when(pl.col("high") > pl.col("low"))
                .then(
                    (2 * pl.col("close") - pl.col("low") - pl.col("high"))
                    / (pl.col("high") - pl.col("low"))
                    * pl.col("volume")
                )
                .otherwise(0.0)
                .sum()
                .alias("flow")
            )["flow"][0]
            cmf = numeric(flow) / total
            closes = close_series(frame)
            net = numeric(closes.diff().tail(10).sign().sum())
            fast, slow = trend_emas(frame)
            hit = regime_hit(numeric(closes[-1]), fast, slow, cmf, net)
            reason = "VOLUME_REGIME" if hit else "NO_VOLUME_REGIME"
    stance = directional_stance(
        frame,
        opportunity,
        family="obv-adl-regime",
        dependency="ohlcv-cmf-close-count-v1",
        mechanism="participation-regime-hypothesis",
        direction=hit[1] if hit else None,
        reason=reason,
    )
    return stance_with(stance, variant_id=hit[0] if hit else "UNRESOLVED")


def observe_macd_stoch(frame: CausalFrame, opportunity: Opportunity | None) -> Stance:
    reason = context_reason(frame, opportunity, 34)
    direction = None
    if reason is None:
        closes = close_series(frame)
        highs = frame.df["high"]
        lows = frame.df["low"]
        if not highs.is_finite().all() or not lows.is_finite().all():
            raise ValueError("non-finite native feature input")
        high, low = highs.rolling_max(14), lows.rolling_min(14)
        # Declared neutral oscillator convention for a flat price window.
        data = pl.DataFrame({"close": closes, "high": high, "low": low})
        k = data.select(
            pl.when(pl.col("high") == pl.col("low"))
            .then(50.0)
            .otherwise(100 * (pl.col("close") - pl.col("low")) / (pl.col("high") - pl.col("low")))
            .alias("k")
        )["k"]
        d = k.rolling_mean(3)
        macd = macd_line(frame)
        above = macd > 0
        mask = ((k > d) if above else (k < d)).fill_null(False)
        reason = "NO_CONFIRMED_STOCH_RUN"
        if macd != 0 and mask[-1]:
            false_indices = (~mask).arg_true()
            start = int(false_indices[-1]) + 1 if len(false_indices) else 0
            if start > 0 and k[start - 1] is not None and d[start - 1] is not None:
                direction, reason = ("LONG" if above else "SHORT"), "MACD_ALIGNED_STOCH_RUN"
    return directional_stance(
        frame,
        opportunity,
        family="macd-stoch-trend",
        dependency="ohlcv-macd-stoch-v1",
        mechanism="momentum-cross-hypothesis",
        direction=direction,
        reason=reason,
    )
