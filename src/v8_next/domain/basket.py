"""Declarative multi-asset basket definitions over the real quad tape.

A basket is membership plus a weighting rule, decided before the run. Execution
is *not* implemented here: it is delegated to the native ``BacktestEngine``
through :mod:`v8_next.adapters.basket_backtest`, so capital, netting, margin,
commission and funding settlement all stay engine-owned and are never
independently aggregated into a second set of books.

Loading is delegated to :func:`v8_next.evaluation.multitape.load_multitape`, the
one multi-asset tape reader in this codebase. That keeps the tape's own integrity
contracts in force here: duplicated bar slots are a hard error, a leg that would
lose bars to the chronological intersection is reported rather than trimmed
silently, and a funding record without an interval is recorded as an absence
instead of the 8h venue convention. Nothing is synthesized: every price is a
venue kline from the tape.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from v8_next.domain.market import Candle
from v8_next.evaluation.multitape import FundingRow, MultiTape, load_multitape

#: Canonical quad tape: aligned hourly klines + funding for four instruments.
QUAD_TAPE_PATH = "research/tape/quad-1h-12m"
QUAD_INSTRUMENTS: tuple[str, ...] = ("BTCUSDT", "ETHUSDT", "SOLUSDT", "AVAXUSDT")
QUAD_VENUE = "BINANCE"
QUAD_SETTLEMENT = "USDT"

BasketKind = Literal["buy_hold", "equal_weight", "vol_target"]


@dataclass(frozen=True)
class BasketSpec:
    """Declarative basket: membership and weighting are fixed pre-run."""

    basket_id: str
    kind: BasketKind
    instruments: tuple[str, ...]
    description: str
    #: Trailing window (bars) for the inverse-volatility weights. Trailing only:
    #: the weight applied on bar i may not read bar i or later.
    vol_lookback: int = 48
    vol_target_annual: float = 0.20
    max_leverage: float = 2.0
    #: Bars between equal-weight rebalances. ``None`` means buy-and-hold.
    rebalance_bars: int | None = 24

    def venue_instrument_ids(self) -> tuple[str, ...]:
        """Engine-facing ids, in membership order."""
        return tuple(f"{sym}-PERP.{QUAD_VENUE}" for sym in self.instruments)

    @property
    def is_multi_asset(self) -> bool:
        return len(self.instruments) > 1


def single_leg_buy_hold(symbol: str, *, description: str | None = None) -> BasketSpec:
    """The one-instrument buy-and-hold basket for *symbol*.

    Membership is the named instrument and the rule is the one ``btc_buy_hold``
    declares: one order at the first aligned bar, held to the end. Built from the
    name so a per-leg benchmark curve is measured against a basket that executes
    that leg's own rule, instead of being priced by an index of the whole quad.
    """
    return BasketSpec(
        basket_id=f"{symbol}_buy_hold",
        kind="buy_hold",
        instruments=(symbol,),
        description=(
            description
            or f"Single-asset {symbol} buy-and-hold (reference, 1 instrument)"
        ),
        rebalance_bars=None,
    )


#: Canonical baskets. Every one of these is executable in the native engine; the
#: membership is a declared instrument list, never a universe inferred at runtime.
BASKETS: dict[str, BasketSpec] = {
    "btc_buy_hold": single_leg_buy_hold(
        "BTCUSDT", description="Single-asset BTC buy-and-hold (reference, 1 instrument)"
    ),
    "equal_weight_quad": BasketSpec(
        basket_id="equal_weight_quad",
        kind="equal_weight",
        instruments=QUAD_INSTRUMENTS,
        description="Equal-weight 25/25/25/25 BTC/ETH/SOL/AVAX, buy-and-hold with rebalance",
    ),
    "vol_target_quad": BasketSpec(
        basket_id="vol_target_quad",
        kind="vol_target",
        instruments=QUAD_INSTRUMENTS,
        description="Vol-target (20% ann) inverse-vol weighted across quad, trailing-only",
    ),
}

#: Short aliases used by the benchmark family ids.
BASKET_ALIASES: dict[str, str] = {
    "equal_weight": "equal_weight_quad",
    "vol_target": "vol_target_quad",
}


def resolve_basket(basket: str | BasketSpec) -> BasketSpec:
    """Accept a basket id (or alias) and return the spec, or raise by name."""
    if isinstance(basket, BasketSpec):
        return basket
    key = BASKET_ALIASES.get(basket, basket)
    try:
        return BASKETS[key]
    except KeyError:
        raise ValueError(f"unknown basket {basket!r}; known: {sorted(BASKETS)}") from None


def quad_tape_legs(
    tape_path: Path | str = QUAD_TAPE_PATH,
    *,
    limit: int | None = None,
    offset: int = 0,
) -> MultiTape:
    """Load the multi-asset tape through the canonical reader.

    Returns the ``MultiTape`` as read (candles, funding rows, coverage, dropped
    counters). A tape that does not carry every :data:`QUAD_INSTRUMENTS` leg is
    reported by name rather than silently run short.
    """
    tape = load_multitape(tape_path, limit, offset)
    missing = [sym for sym in QUAD_INSTRUMENTS if sym not in tape.candles]
    if missing:
        raise ValueError(
            f"quad tape {tape.tape_path} is missing instrument leg(s): {', '.join(missing)}"
        )
    return tape


def load_quad_candles(
    tape_path: Path | str = QUAD_TAPE_PATH,
    *,
    limit: int | None = None,
    offset: int = 0,
) -> dict[str, tuple[Candle, ...]]:
    """Quad klines per instrument, chronological and aligned.

    Alignment is the reader's chronological intersection; the legs are returned
    in :data:`QUAD_INSTRUMENTS` order so a caller never depends on file order.
    """
    tape = quad_tape_legs(tape_path, limit=limit, offset=offset)
    return {sym: tape.candles[sym] for sym in QUAD_INSTRUMENTS}


def quad_funding(
    tape_path: Path | str = QUAD_TAPE_PATH,
    *,
    limit: int | None = None,
    offset: int = 0,
) -> tuple[FundingRow, ...]:
    """Real venue funding rows carried by the tape, with the dropped count.

    The dropped count travels with the rows: a funding P&L computed over a tape
    that silently skipped malformed records would otherwise read as fully
    covered. Consumers must surface it.
    """
    return quad_tape_legs(tape_path, limit=limit, offset=offset).funding
