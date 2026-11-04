"""Bounded SANDBOX session: live public venue data + simulated execution.

F6 (#405) asks for "canli veri + simule execution" and a data-match report. This
module runs a real, bounded session against the venue's public market data with
NautilusTrader's *sandbox* execution client -- live prices, simulated fills, no
authenticated order path and no capital authority. Everything it writes is
labelled ``NO_ECONOMIC_CLAIM``.

What the session records:

* every quote/trade/bar the live data client delivered (counts + a sha256 of the
  recorded rows), so a reader can match live-received data against a REST capture
  of the same window instead of trusting a claim that they matched;
* the order lifecycle the sandbox produced (submitted -> accepted -> filled or
  not), including the fill price, so "simulated execution ran" is evidence and
  not an assertion;
* the exact environment, venue, instrument and duration in force.

If the venue cannot be reached, the session fails closed with the error recorded
in ``failure.json`` -- it never writes a success record with zero data.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import time
from pathlib import Path
from typing import Any, TextIO

from nautilus_trader.adapters.binance import (
    BinanceDataClientConfig,
    BinanceDataClientFactory,
    BinanceInstrumentProviderConfig,
    BinanceProductType,
)
from nautilus_trader.adapters.sandbox import (
    SandboxExecutionClientConfig,
    SandboxExecutionClientFactory,
)
from nautilus_trader.common import Environment
from nautilus_trader.live import LiveNode
from nautilus_trader.model import (
    AccountType,
    Currency,
    InstrumentId,
    Money,
    OmsType,
    OrderSide,
    Quantity,
    TraderId,
    Venue,
)
from nautilus_trader.trading import Strategy

from v8_next.evaluation.store import canonical

INSTRUMENT = "BTCUSDT-PERP.BINANCE"
SANDBOX_VENUE = "BINANCE"


class SandboxProbe(Strategy):
    """Record live market data and prove the sandbox execution path with one order.

    The single MARKET order is deliberately the smallest size the instrument
    accepts: it exists to demonstrate that the sandbox executes against live
    prices, never to express a view.
    """

    def __new__(cls, *args: Any, **kwargs: Any) -> SandboxProbe:
        return super().__new__(cls)

    def __init__(self, output: TextIO, order_output: TextIO) -> None:
        super().__init__()
        self.output = output
        self.order_output = order_output
        self.stop_node: Any = None
        self.failure: str | None = None
        self.quotes = 0
        self.trades = 0
        self.bars = 0
        self.orders_submitted = 0
        self.orders_accepted = 0
        self.orders_denied = 0
        self.orders_rejected = 0
        self.fills: list[dict[str, Any]] = []
        self.first_trade_ns: int | None = None

    def _record(self, kind: str, payload: dict[str, Any]) -> None:
        self.output.write(canonical({"kind": kind, **payload}) + "\n")
        self.output.flush()

    def on_start(self) -> None:
        instrument_id = InstrumentId.from_str(INSTRUMENT)
        self.subscribe_quotes(instrument_id)
        self.subscribe_trades(instrument_id)

    def _maybe_probe(self) -> None:
        """Submit the probe order once, at the first observed venue trade."""
        if self.orders_submitted or self.failure is not None:
            return
        instrument = self.cache.instrument(InstrumentId.from_str(INSTRUMENT))
        if instrument is None:
            return
        quantity = instrument.min_quantity
        if quantity is None or float(quantity) <= 0:
            self.failure = "instrument exposes no orderable minimum quantity"
            if self.stop_node is not None:
                self.stop_node()
            return
        self.orders_submitted += 1
        order = self.order_factory.market(
            instrument.id, OrderSide.BUY, Quantity(float(quantity), instrument.size_precision)
        )
        self.submit_order(order)
        self.order_output.write(
            canonical(
                {
                    "event": "SUBMITTED",
                    "client_order_id": str(order.client_order_id),
                    "instrument_id": str(instrument.id),
                    "quantity": str(quantity),
                    "purpose": "SANDBOX_EXECUTION_PATH_PROBE_NOT_A_TRADE_VIEW",
                    "claim_status": "NO_ECONOMIC_CLAIM",
                }
            )
            + "\n"
        )
        self.order_output.flush()

    def on_quote(self, quote: Any) -> None:
        try:
            self.quotes += 1
            self._record(
                "quote",
                {
                    "instrument_id": str(quote.instrument_id),
                    "event_ns": int(quote.ts_event),
                    "received_ns": int(quote.ts_init),
                    "bid": str(quote.bid_price),
                    "ask": str(quote.ask_price),
                },
            )
        except Exception as error:  # pragma: no cover - defensive
            self._fail(error)

    def on_trade(self, trade: Any) -> None:
        try:
            self.trades += 1
            if self.first_trade_ns is None:
                self.first_trade_ns = int(trade.ts_event)
            self._record(
                "trade",
                {
                    "instrument_id": str(trade.instrument_id),
                    "event_ns": int(trade.ts_event),
                    "received_ns": int(trade.ts_init),
                    "price": str(trade.price),
                    "size": str(trade.size),
                    "aggressor_side": str(trade.aggressor_side),
                    "trade_id": str(trade.trade_id),
                },
            )
            self._maybe_probe()
        except Exception as error:
            self._fail(error)

    def _order_event(self, event: Any, name: str) -> None:
        try:
            row = {
                "event": name,
                "client_order_id": str(getattr(event, "client_order_id", "")),
                "instrument_id": str(getattr(event, "instrument_id", "")),
                "event_ns": int(getattr(event, "ts_event", 0) or 0),
                "received_ns": int(getattr(event, "ts_init", 0) or 0),
                "claim_status": "NO_ECONOMIC_CLAIM",
            }
            if name == "FILLED":
                row["last_px"] = str(getattr(event, "last_px", ""))
                row["last_qty"] = str(getattr(event, "last_qty", ""))
                row["liquidity_side"] = str(getattr(event, "liquidity_side", ""))
                row["commission"] = str(getattr(event, "commission", ""))
                self.fills.append(row)
            self.order_output.write(canonical(row) + "\n")
            self.order_output.flush()
        except Exception as error:  # pragma: no cover - defensive
            self._fail(error)

    def on_order_submitted(self, event: Any) -> None:
        self._order_event(event, "SUBMITTED_ACK")

    def on_order_accepted(self, event: Any) -> None:
        self.orders_accepted += 1
        self._order_event(event, "ACCEPTED")

    def on_order_denied(self, event: Any) -> None:
        self.orders_denied += 1
        self._order_event(event, "DENIED")

    def on_order_rejected(self, event: Any) -> None:
        self.orders_rejected += 1
        self._order_event(event, "REJECTED")

    def on_order_filled(self, event: Any) -> None:
        self._order_event(event, "FILLED")

    def _fail(self, error: Exception) -> None:
        self.failure = f"{type(error).__name__}: {error}"
        if self.stop_node is not None:
            self.stop_node()


async def sandbox_session(destination: Path, seconds: int = 45) -> dict[str, Any]:
    """Run one bounded SANDBOX session and write its evidence bundle."""
    if seconds <= 0 or seconds > 900:
        raise ValueError("sandbox session duration must be in (0, 900] seconds")
    destination.mkdir(parents=True, exist_ok=False)
    started_ns = time.time_ns()
    builder = LiveNode.builder("v8-sandbox-observer", TraderId("V8-SBX"), Environment.SANDBOX)
    builder.add_data_client(
        "BINANCE",
        BinanceDataClientFactory(),
        BinanceDataClientConfig(
            product_type=BinanceProductType.USD_M,
            instrument_provider=BinanceInstrumentProviderConfig(load_ids=[INSTRUMENT]),
        ),
    )
    try:
        builder.add_exec_client(
            SANDBOX_VENUE,
            SandboxExecutionClientFactory(),
            SandboxExecutionClientConfig(
                venue=Venue(SANDBOX_VENUE),
                starting_balances=[Money(10_000, Currency.from_str("USDT"))],
                oms_type=OmsType.NETTING,
                account_type=AccountType.MARGIN,
                base_currency=Currency.from_str("USDT"),
                bar_execution=True,
                trade_execution=True,
            ),
        )
        node = builder.build()
    except NotImplementedError as error:
        # This build's compiled registry has no extractor for the sandbox exec
        # client (measured with both client names, 'SANDBOX' and the venue name):
        # the live SANDBOX session cannot be started from Python here. Fail with
        # a NAMED blocker instead of an empty session, so the gap is visible in
        # the artifact rather than looking like "no data arrived".
        blocked = {
            "schema_version": 1,
            "environment": "SANDBOX",
            "venue": SANDBOX_VENUE,
            "instrument_id": INSTRUMENT,
            "requested_seconds": seconds,
            "started_ns": started_ns,
            "ended_ns": time.time_ns(),
            "status": "BLOCKED_SANDBOX_EXEC_CLIENT_UNAVAILABLE",
            "blocker": f"{type(error).__name__}: {error}",
            "retry_condition": (
                "a nautilus-trader build whose exec-factory registry exposes the "
                "sandbox adapter, or a documented registration call for it"
            ),
            "quotes": 0,
            "trades": 0,
            "claim_status": "NO_ECONOMIC_CLAIM",
            "capital_authority": "NONE_SIMULATED_EXECUTION_ONLY",
        }
        (destination / "blocked.json").write_text(canonical(blocked) + "\n")
        return blocked
    with (
        (destination / "live.jsonl").open("x") as output,
        (destination / "orders.jsonl").open("x") as order_output,
    ):
        actor = SandboxProbe(output, order_output)
        actor.stop_node = node.handle().stop
        node.add_strategy(actor)

        async def stop_after() -> None:
            await asyncio.sleep(seconds)
            node.handle().stop()

        stopper = asyncio.create_task(stop_after())
        try:
            await node.run_async()
            output.flush()
            order_output.flush()
            live_bytes = (destination / "live.jsonl").read_bytes()
            order_bytes = (destination / "orders.jsonl").read_bytes()
            status = (
                "FAILED_CALLBACK_ERROR"
                if actor.failure
                else "FILLED_SANDBOX_ORDER"
                if actor.fills
                else "NO_SANDBOX_FILL"
                if actor.trades
                else "NO_LIVE_MARKET_DATA"
            )
            result = {
                "schema_version": 1,
                "environment": "SANDBOX",
                "venue": SANDBOX_VENUE,
                "instrument_id": INSTRUMENT,
                "requested_seconds": seconds,
                "started_ns": started_ns,
                "ended_ns": time.time_ns(),
                "status": status,
                "failure": actor.failure,
                "quotes": actor.quotes,
                "trades": actor.trades,
                "first_trade_ns": actor.first_trade_ns,
                "orders": {
                    "submitted": actor.orders_submitted,
                    "accepted": actor.orders_accepted,
                    "denied": actor.orders_denied,
                    "rejected": actor.orders_rejected,
                    "fills": len(actor.fills),
                    "fill_px": [f.get("last_px") for f in actor.fills],
                    "liquidity_side": [f.get("liquidity_side") for f in actor.fills],
                },
                "live_jsonl_sha256": hashlib.sha256(live_bytes).hexdigest(),
                "orders_jsonl_sha256": hashlib.sha256(order_bytes).hexdigest(),
                "data_match_note": (
                    "Match live-received rows against a REST capture of the same "
                    "window (capture_agg_trades / capture) by timestamp and trade id; "
                    "the counts here are the live side of that comparison."
                ),
                "claim_status": "NO_ECONOMIC_CLAIM",
                "capital_authority": "NONE_SIMULATED_EXECUTION_ONLY",
            }
            (destination / "result.json").write_text(canonical(result) + "\n")
            return result
        except Exception as error:
            (destination / "failure.json").write_text(
                canonical(
                    {
                        "status": "FAILED",
                        "error_type": type(error).__name__,
                        "error": str(error),
                        "callback_failure": actor.failure,
                        "quotes": actor.quotes,
                        "trades": actor.trades,
                        "ended_ns": time.time_ns(),
                        "claim_status": "NO_ECONOMIC_CLAIM",
                    }
                )
                + "\n"
            )
            raise
        finally:
            stopper.cancel()
            await asyncio.gather(stopper, return_exceptions=True)
            node.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--seconds", type=int, default=45)
    args = parser.parse_args()
    print(json.dumps(asyncio.run(sandbox_session(args.destination, args.seconds)), default=str))


if __name__ == "__main__":
    main()
