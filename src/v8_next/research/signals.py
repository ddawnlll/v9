"""Preregistered raw directional signals, independent of the V9 gate.

Each builder reads only the bars up to and including the signal bar and returns
the entry **bar index** plus direction; the caller enters at the next bar's open.
The module contains no opportunity grammar, no expert panel and no entry gate:
the point of the screen is to measure whether the raw price/volume mechanism
carries signal before any V9 policy is layered above it.

This is measurement code. It never claims an economic edge and never trades.
"""

from __future__ import annotations

import bisect
import statistics
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Final

from v8_next.research.screen import Bar

LONG: Final[int] = 1
SHORT: Final[int] = -1


@dataclass(frozen=True)
class RawSignal:
    """One signal at a closed bar; the entry executes on the next bar's open."""

    bar_index: int
    direction: int
    reason: str

    def __post_init__(self) -> None:
        if self.direction not in (LONG, SHORT):
            raise ValueError(f"direction must be +1 or -1, got {self.direction}")
        if self.bar_index < 0:
            raise ValueError("bar index must be non-negative")


@dataclass(frozen=True)
class SignalSpec:
    """A named builder with the exact parameters the screen is registered under."""

    candidate_id: str
    builder: Callable[..., list[RawSignal]]
    params: dict[str, float]
    warmup_bars: int
    hypothesis: str
    needs_funding: bool = False


def _typical_bar_direction(bar: Bar) -> float:
    span = bar.high - bar.low
    if span <= 0:
        return 0.5
    return (bar.close - bar.low) / span


def bollinger_reversion(
    bars: Sequence[Bar], *, window: int = 20, k: float = 2.0
) -> list[RawSignal]:
    """Close beyond a ``k``-sigma band of the trailing ``window`` closes.

    Hypothesis: a one-bar close outside a two-sigma band mean-reverts further
    than the round-trip cost of taking the other side. No trend filter, no
    volume filter, no gate.
    """
    if window < 2:
        raise ValueError("window must be at least 2")
    signals: list[RawSignal] = []
    for index in range(window - 1, len(bars)):
        closes = [bar.close for bar in bars[index - window + 1 : index + 1]]
        mean = statistics.fmean(closes)
        sigma = statistics.pstdev(closes)
        if sigma <= 0:
            continue
        latest = closes[-1]
        if latest < mean - k * sigma:
            signals.append(RawSignal(index, LONG, "close_below_lower_band"))
        elif latest > mean + k * sigma:
            signals.append(RawSignal(index, SHORT, "close_above_upper_band"))
    return signals


def volume_climax_reversal(
    bars: Sequence[Bar],
    *,
    window: int = 50,
    multiple: float = 4.0,
    close_extreme: float = 0.4,
) -> list[RawSignal]:
    """A volume spike on a bar that closes at an extreme of its own range.

    Hypothesis: climactic participation at an extreme marks exhaustion, so the
    next bar offers a favourable entry in the opposite direction. The volume
    reference is the median of the *prior* ``window`` bars, never the current one.
    """
    if window < 2:
        raise ValueError("window must be at least 2")
    if multiple <= 0:
        raise ValueError("multiple must be positive")
    if not 0.0 < close_extreme < 0.5:
        raise ValueError("close_extreme must be in (0, 0.5)")
    signals: list[RawSignal] = []
    for index in range(window, len(bars)):
        reference = statistics.median(bar.volume for bar in bars[index - window : index])
        if reference <= 0:
            continue
        bar = bars[index]
        if bar.volume < multiple * reference:
            continue
        location = _typical_bar_direction(bar)
        if bar.close < bar.open and location <= close_extreme:
            signals.append(RawSignal(index, LONG, "down_climax_at_low"))
        elif bar.close > bar.open and location >= 1.0 - close_extreme:
            signals.append(RawSignal(index, SHORT, "up_climax_at_high"))
    return signals


def liquidity_sweep_reclaim(
    bars: Sequence[Bar], *, lookback: int = 24
) -> list[RawSignal]:
    """Sweep a prior extreme intrabar, then close back inside the range.

    Hypothesis: a stop run through a recent swing extreme followed by an
    immediate reclaim is liquidity provision by the sweeper, and the reclaim
    direction continues. The sweep level uses bars strictly before the signal
    bar, so the level itself is knowable in advance.
    """
    if lookback < 2:
        raise ValueError("lookback must be at least 2")
    signals: list[RawSignal] = []
    for index in range(lookback, len(bars)):
        prior = bars[index - lookback : index]
        prior_low = min(bar.low for bar in prior)
        prior_high = max(bar.high for bar in prior)
        bar = bars[index]
        if bar.low < prior_low and bar.close > prior_low:
            signals.append(RawSignal(index, LONG, "sweep_low_reclaim"))
        elif bar.high > prior_high and bar.close < prior_high:
            signals.append(RawSignal(index, SHORT, "sweep_high_reclaim"))
    return signals


def _quantile(sorted_values: Sequence[float], q: float) -> float:
    if len(sorted_values) == 1:
        return float(sorted_values[0])
    position = q * (len(sorted_values) - 1)
    lower = int(position)
    upper = min(lower + 1, len(sorted_values) - 1)
    weight = position - lower
    return float(sorted_values[lower]) * (1.0 - weight) + float(sorted_values[upper]) * weight


def funding_extreme_reversion(
    bars: Sequence[Bar],
    funding: Sequence[tuple[int, float]],
    *,
    window: int = 30,
    quantile: float = 0.10,
) -> list[RawSignal]:
    """Fade the crowded side when the settlement funding rate is extreme.

    Hypothesis: the funding rate is the price of leveraged crowding, so an
    extreme settlement rate marks a one-sided book that unwinds. A rate above the
    trailing ``1 - quantile`` band is a crowded long (fade short); a rate below
    the ``quantile`` band is a crowded short (fade long). Ties are not signals:
    the comparison is strict, so the modal constant rate never triggers.

    The settlement instant is the first moment the rate is an accomplished venue
    fact; the entry is the next bar open after the first bar that closes at or
    after it. Rates are never back-filled or interpolated between settlements.
    """
    if window < 5:
        raise ValueError("window must be at least 5 settlements")
    if not 0.0 < quantile < 0.5:
        raise ValueError("quantile must be in (0, 0.5)")
    settlements = [ns for ns, _ in funding]
    rates = [rate for _, rate in funding]
    if len(rates) <= window:
        return []
    ends = [bar.end_ns for bar in bars]
    signals: list[RawSignal] = []
    for index in range(window - 1, len(rates)):
        trailing = sorted(rates[index - window + 1 : index + 1])
        lower = _quantile(trailing, quantile)
        upper = _quantile(trailing, 1.0 - quantile)
        latest = rates[index]
        if latest < lower:
            direction, reason = LONG, "funding_crowded_short"
        elif latest > upper:
            direction, reason = SHORT, "funding_crowded_long"
        else:
            continue
        bar_index = bisect.bisect_left(ends, settlements[index])
        if bar_index >= len(bars) - 1:
            continue
        signals.append(RawSignal(bar_index, direction, reason))
    return signals


#: The preregistered primary parameterisation of each candidate.
SIGNAL_SPECS: Final[dict[str, SignalSpec]] = {
    "bollinger_reversion": SignalSpec(
        candidate_id="bollinger_reversion",
        builder=bollinger_reversion,
        params={"window": 20, "k": 2.0},
        warmup_bars=20,
        hypothesis=(
            "A one-bar close outside a 2-sigma band of the trailing 20 H1 closes "
            "mean-reverts in the opposite direction by more than the round-trip cost."
        ),
    ),
    "volume_climax_reversal": SignalSpec(
        candidate_id="volume_climax_reversal",
        builder=volume_climax_reversal,
        params={"window": 50, "multiple": 4.0, "close_extreme": 0.4},
        warmup_bars=50,
        hypothesis=(
            "A 4x median-volume bar that closes in the extreme 40% of its own "
            "range marks exhaustion, and the next bar reverses it."
        ),
    ),
    "liquidity_sweep_reclaim": SignalSpec(
        candidate_id="liquidity_sweep_reclaim",
        builder=liquidity_sweep_reclaim,
        params={"lookback": 24},
        warmup_bars=24,
        hypothesis=(
            "A sweep of the prior 24-bar extreme that closes back inside the "
            "range continues in the reclaim direction."
        ),
    ),
    "funding_extreme_reversion": SignalSpec(
        candidate_id="funding_extreme_reversion",
        builder=funding_extreme_reversion,
        params={"window": 30, "quantile": 0.10},
        warmup_bars=1,
        hypothesis=(
            "An extreme settlement funding rate marks leveraged crowding, and the "
            "crowded side reverses over the following bars by more than the "
            "round-trip cost."
        ),
        needs_funding=True,
    ),
}

#: Declared sensitivity grid per candidate. Reported, never used for selection.
SENSITIVITY_GRID: Final[dict[str, tuple[dict[str, float], ...]]] = {
    "bollinger_reversion": (
        {"k": 1.5},
        {"k": 2.5},
        {"window": 10},
        {"window": 40},
    ),
    "volume_climax_reversal": (
        {"multiple": 3.0},
        {"multiple": 6.0},
        {"window": 20},
        {"window": 100},
    ),
    "liquidity_sweep_reclaim": (
        {"lookback": 12},
        {"lookback": 48},
    ),
    "funding_extreme_reversion": (
        {"quantile": 0.05},
        {"quantile": 0.20},
        {"window": 15},
        {"window": 60},
    ),
}


def build_signals(
    candidate_id: str,
    bars: Sequence[Bar],
    funding: Sequence[tuple[int, float]] = (),
    **overrides: float,
) -> list[RawSignal]:
    """Build one candidate's signals with the registered parameters, or overrides."""
    try:
        spec = SIGNAL_SPECS[candidate_id]
    except KeyError as exc:
        raise ValueError(f"unknown candidate {candidate_id!r}") from exc
    unknown = set(overrides) - set(spec.params)
    if unknown:
        raise ValueError(f"unknown parameters for {candidate_id}: {sorted(unknown)}")
    params = {**spec.params, **overrides}
    if candidate_id == "bollinger_reversion":
        return spec.builder(bars, window=int(params["window"]), k=params["k"])
    if candidate_id == "volume_climax_reversal":
        return spec.builder(
            bars,
            window=int(params["window"]),
            multiple=params["multiple"],
            close_extreme=params["close_extreme"],
        )
    if candidate_id == "funding_extreme_reversion":
        if not funding:
            raise ValueError("funding_extreme_reversion needs the funding series")
        return spec.builder(
            bars, funding, window=int(params["window"]), quantile=params["quantile"]
        )
    return spec.builder(bars, lookback=int(params["lookback"]))
