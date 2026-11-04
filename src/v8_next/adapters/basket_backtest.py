"""Native multi-asset basket execution in ONE shared BacktestEngine.

A basket is not an aggregation of per-leg books: every leg of every basket runs
in the same ``MARGIN``/``NETTING`` account with one capital pool, so margin,
netting, commission and funding settlement are the engine's arithmetic and never
a second independent stack.

Funding settles natively, exactly as in :mod:`v8_next.adapters.portfolio_backtest`:
every in-window tape funding row becomes a ``MarkPriceUpdate`` (mark = leg close
at the boundary, a labeled mark-proxy) plus a ``FundingRateUpdate``. Rows outside
the window are excluded, never extrapolated.

The funding *cost* of a basket is measured as the engine's own balance
difference between an identical run fed the real funding rows and one fed none.
That is the same convention the canonical portfolio path publishes, and it has
one property worth naming: the two runs differ in exactly one input, so the
difference is attributable to funding and not to a re-derived settlement model.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import numpy as np
from nautilus_trader.backtest import BacktestEngine
from nautilus_trader.common import LogLevel
from nautilus_trader.config import BacktestEngineConfig, LoggerConfig
from nautilus_trader.model import (
    AccountType,
    Bar,
    BarType,
    Currency,
    FundingRateUpdate,
    InstrumentId,
    MarkPriceUpdate,
    Money,
    OmsType,
    OrderSide,
    Price,
    Quantity,
    Venue,
)
from nautilus_trader.trading import Strategy

from v8_next.adapters.engine_state import economic_state

# A basket leg must be the *same* instrument and the *same* bar construction the
# canonical portfolio path uses. Importing its builders instead of re-deriving
# them is what keeps the two paths from drifting apart silently.
from v8_next.adapters.portfolio_backtest import _bars, _instrument, base_currency, trade_signature
from v8_next.domain.basket import QUAD_SETTLEMENT, QUAD_VENUE, BasketSpec, resolve_basket
from v8_next.domain.market import Candle
from v8_next.evaluation.multitape import FundingRow

#: Bar type every leg of a basket is aggregated at. The engine needs one bar type
#: per instrument and the basket only acts on aligned prints.
BAR_AGGREGATION = "1-HOUR-LAST-EXTERNAL"

#: Basis tokens a funding measurement publishes. A number exists only under the
#: first two; the third names why the number was withheld instead.
BASIS_ENGINE_SETTLED = "ENGINE_SETTLED"  # the engine settled the tape's funding rows
BASIS_NO_FUNDING_ROWS = "NO_FUNDING_ROWS"  # measured zero: no row reached the window
BASIS_TRADES_DIVERGED = "TRADES_DIVERGED"  # funded/unfunded runs traded differently


def basket_bar_type(instrument_id: str) -> BarType:
    return BarType.from_str(f"{instrument_id}-{BAR_AGGREGATION}")


class BasketStrategy(Strategy):
    """Weighting rule for one basket, executed as native orders.

    The rule is causal: the weights for bar *i* are computed from bars strictly
    before *i* is acted on, so no bar is both the signal and the earned return.
    """

    def __new__(cls, *args: object, **kwargs: object) -> BasketStrategy:
        return super().__new__(cls)

    def __init__(
        self,
        spec: BasketSpec,
        capital: Decimal,
        *,
        venue: str = QUAD_VENUE,
        currency: str = QUAD_SETTLEMENT,
    ) -> None:
        super().__init__()
        self.basket = spec
        self.capital = capital
        self.instrument_ids = tuple(
            InstrumentId.from_str(f"{sym}-PERP.{venue}") for sym in spec.instruments
        )
        self.currency = currency
        self._closes: dict[InstrumentId, list[float]] = {iid: [] for iid in self.instrument_ids}
        self._aligned_bars = 0
        #: One record per rebalance decision: the rule, the bar it fired on and
        #: the target notional per instrument. Audit material, not a P&L ledger.
        self.decisions: list[dict[str, Any]] = []

    def on_start(self) -> None:
        for iid in self.instrument_ids:
            self.subscribe_bars(basket_bar_type(str(iid)))

    def on_bar(self, bar: Bar) -> None:
        iid = bar.bar_type.instrument_id
        closes = self._closes.get(iid)
        if closes is None:
            return
        closes.append(float(bar.close))
        # Act only on an aligned print: every leg has the same bar count, so no
        # weight is ever computed from a partially-updated basket.
        head = len(self._closes[self.instrument_ids[0]])
        if any(len(v) != head for v in self._closes.values()):
            return
        self._aligned_bars += 1
        idx = self._aligned_bars

        if self.basket.kind == "buy_hold":
            if idx != 1:
                return
            self._rebalance({iid: float(self.capital)}, idx, "BUY_HOLD")
            return

        if self.basket.kind == "equal_weight":
            every = self.basket.rebalance_bars
            if idx != 1 and (every is None or every <= 0 or idx % every != 0):
                return
            per = float(self.capital) / len(self.instrument_ids)
            self._rebalance({iid: per for iid in self.instrument_ids}, idx, "EQUAL_WEIGHT")
            return

        if self.basket.kind == "vol_target":
            targets = self._inverse_vol_targets(idx)
            if targets is None:
                return
            self._rebalance(targets, idx, "VOL_TARGET")
            return

        raise ValueError(f"unknown basket kind {self.basket.kind!r}")

    def _inverse_vol_targets(self, idx: int) -> dict[InstrumentId, float] | None:
        """Inverse-volatility notionals from trailing returns only.

        Returns ``None`` (no order) while the trailing window is short or when no
        leg has an estimable variance: with nothing to invert there is no weight
        to publish, and a fabricated equal weight would be a different rule.
        """
        lookback = self.basket.vol_lookback
        if idx < lookback + 1:
            return None
        inv_vol: dict[InstrumentId, float] = {}
        for iid in self.instrument_ids:
            closes = self._closes[iid]
            rets = [
                closes[i] / closes[i - 1] - 1.0
                for i in range(len(closes) - lookback, len(closes))
            ]
            sd = float(np.std(np.asarray(rets), ddof=1)) if len(rets) >= 2 else 0.0
            inv_vol[iid] = (1.0 / sd) if sd > 1e-12 else 0.0
        total = sum(inv_vol.values())
        if total <= 1e-12:
            return None
        per_leg_cap = float(self.capital) * self.basket.max_leverage / len(self.instrument_ids)
        return {
            iid: min(float(self.capital) * weight / total, per_leg_cap)
            for iid, weight in inv_vol.items()
        }

    def _rebalance(self, targets: dict[InstrumentId, float], idx: int, rule: str) -> None:
        """Submit market deltas toward *targets*; the engine nets them."""
        for iid, target_notional in targets.items():
            instrument = self.cache.instrument(iid)
            if instrument is None:
                continue
            held = sum(
                float(pos.quantity)
                for pos in self.cache.positions_open()
                if pos.instrument_id == iid and not pos.is_closed
            )
            closes = self._closes[iid]
            px = closes[-1] if closes else 0.0
            if px <= 0:
                continue
            delta = target_notional / px - held
            increment = float(instrument.size_increment)
            if abs(delta) < increment * 0.5:
                continue
            order = self.order_factory.market(
                instrument_id=iid,
                order_side=OrderSide.BUY if delta > 0 else OrderSide.SELL,
                quantity=Quantity(abs(delta), instrument.size_precision),
            )
            self.submit_order(order)
        self.decisions.append(
            {
                "bar": idx,
                "rule": rule,
                "target_notional": {str(k): v for k, v in targets.items()},
            }
        )


def run_basket_backtest(
    legs: dict[str, tuple[Candle, ...]],
    basket: str | BasketSpec,
    funding: tuple[FundingRow, ...] = (),
    *,
    capital: Decimal = Decimal("10000"),
    maker_fee: Decimal = Decimal("0.0002"),
    taker_fee: Decimal = Decimal("0.0005"),
    venue: str = QUAD_VENUE,
    currency: str = QUAD_SETTLEMENT,
) -> dict[str, Any]:
    """Execute one basket over *legs* in a single shared MARGIN/NETTING account.

    *legs* maps raw symbols (``BTCUSDT``) to chronological candles. A basket that
    names a leg the caller did not supply is rejected by name: running it over a
    subset would publish a different basket under the same id.
    """
    spec = resolve_basket(basket)
    missing = [sym for sym in spec.instruments if sym not in legs]
    if missing:
        raise ValueError(f"basket {spec.basket_id} missing leg(s): {', '.join(missing)}")
    width = len(legs[spec.instruments[0]])
    misaligned = [sym for sym in spec.instruments if len(legs[sym]) != width]
    if misaligned:
        raise ValueError(f"basket {spec.basket_id} legs are not aligned: {', '.join(misaligned)}")

    curr = Currency.from_str(currency)
    ven = Venue(venue)
    engine = BacktestEngine(
        BacktestEngineConfig(
            bypass_logging=True,
            logging=LoggerConfig(stdout_level=LogLevel.WARNING),
        )
    )
    settlements = 0
    out_of_window = 0
    unknown_leg = 0
    try:
        engine.add_venue(
            ven,
            OmsType.NETTING,
            AccountType.MARGIN,
            [Money(float(capital), curr)],
            default_leverage=Decimal(1),
            liquidation_enabled=False,
        )
        for raw in spec.instruments:
            instrument_id = f"{raw}-PERP.{venue}"
            candles = legs[raw]
            engine.add_instrument(
                _instrument(instrument_id, raw, base_currency(raw), curr, maker_fee, taker_fee)
            )
            engine.add_data(_bars(candles, basket_bar_type(instrument_id)))
        window_start = min(legs[sym][0].end_ns for sym in spec.instruments)
        window_end = max(legs[sym][-1].end_ns for sym in spec.instruments)
        for row in funding:
            boundary = row.funding_time_ms * 1_000_000
            if not window_start <= boundary <= window_end:
                out_of_window += 1
                continue
            if row.instrument not in spec.instruments:
                unknown_leg += 1
                continue
            mark = next(
                (float(c.close) for c in legs[row.instrument] if c.end_ns >= boundary), None
            )
            if mark is None:
                unknown_leg += 1
                continue
            fund_iid = InstrumentId.from_str(f"{row.instrument}-PERP.{venue}")
            engine.add_data([MarkPriceUpdate(fund_iid, Price(mark, 2), boundary, boundary)])
            engine.add_data(
                [
                    FundingRateUpdate(
                        fund_iid,
                        row.funding_rate,
                        boundary,
                        boundary,
                        next_funding_ns=boundary,
                    )
                ]
            )
            settlements += 1
        strategy = BasketStrategy(spec, capital, venue=venue, currency=currency)
        engine.add_strategy(strategy)
        engine.run()
        account = economic_state(engine, ven, curr)
        return {
            "basket_id": spec.basket_id,
            "kind": spec.kind,
            "instruments": spec.instruments,
            "instrument_ids": list(spec.venue_instrument_ids()),
            "capital": float(capital),
            "account": account,
            "decisions": strategy.decisions,
            "engine_iterations": engine.iteration,
            "funding_settlements_fed": settlements,
            "funding_out_of_window": out_of_window,
            "funding_unknown_leg": unknown_leg,
            "funding_rows_available": len(funding),
            "bars_per_leg": {sym: len(legs[sym]) for sym in spec.instruments},
        }
    finally:
        engine.dispose()


def _balance(engine_result: dict[str, Any]) -> float:
    return float(str(engine_result["account"]["balance_total"]).split()[0])


def run_basket_funding_measurement(
    legs: dict[str, tuple[Candle, ...]],
    basket: str | BasketSpec,
    funding: tuple[FundingRow, ...] = (),
    **kwargs: Any,
) -> dict[str, Any]:
    """Funding's effect on the basket's equity, measured by the engine itself.

    Two runs, one input apart: the identical basket with the tape's real funding
    rows and the identical basket with none. The difference is the funding the
    engine actually settled -- no settlement model is re-derived outside the
    engine. The sign convention is the one the canonical portfolio path already
    publishes (``funding_paid``: negative when funding was paid, positive when
    received), so a single report column never carries two conventions.

    When the tape carries no funding row for the window, ``funding_cost`` is
    ``0.0`` with basis ``NO_FUNDING_ROWS``: a measured zero, not an unavailable
    value, and the row counts say why.
    """
    funded = run_basket_backtest(legs, basket, funding, **kwargs)
    unfunded = run_basket_backtest(legs, basket, (), **kwargs)
    # The difference is funding only if the funding feed did not change what was
    # traded. If it did, the delta is not attributable and is withheld rather
    # than published as a funding number.
    if trade_signature(funded) != trade_signature(unfunded):
        return {
            "basket_id": funded["basket_id"],
            "kind": funded["kind"],
            "funding_cost": None,
            "funding_basis": BASIS_TRADES_DIVERGED,
            "funding_settlements_fed": funded["funding_settlements_fed"],
            "funding_rows_available": funded["funding_rows_available"],
            "funding_out_of_window": funded["funding_out_of_window"],
            "funding_unknown_leg": funded["funding_unknown_leg"],
            "funded_balance_usdt": _balance(funded),
            "unfunded_balance_usdt": _balance(unfunded),
            "instruments": list(funded["instruments"]),
            "engine_iterations": funded["engine_iterations"],
        }
    effect = _balance(funded) - _balance(unfunded)
    basis = BASIS_NO_FUNDING_ROWS if not funding else BASIS_ENGINE_SETTLED
    return {
        "basket_id": funded["basket_id"],
        "kind": funded["kind"],
        "funding_cost": float(effect),
        "funding_basis": basis,
        "funding_settlements_fed": funded["funding_settlements_fed"],
        "funding_rows_available": funded["funding_rows_available"],
        "funding_out_of_window": funded["funding_out_of_window"],
        "funding_unknown_leg": funded["funding_unknown_leg"],
        "funded_balance_usdt": _balance(funded),
        "unfunded_balance_usdt": _balance(unfunded),
        "instruments": list(funded["instruments"]),
        "engine_iterations": funded["engine_iterations"],
    }


def basket_equity_from_engine(engine_result: dict[str, Any]) -> float:
    """Realized terminal equity of the executed basket (engine balance).

    Only the terminal balance is a realized engine number: a per-bar curve would
    have to be reconstructed from the fill set, which is an independent model of
    the fills this module deliberately does not build. The per-bar shape of a
    basket's *rule* is computed from real closes elsewhere
    (:func:`v8_next.evaluation.economic_benchmark.compute_basket_equity_real`);
    this function publishes the executed end state that the two must agree with.
    """
    return _balance(engine_result)

