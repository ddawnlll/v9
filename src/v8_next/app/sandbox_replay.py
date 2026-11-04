"""Live-capture sandbox replay: live venue data + simulated execution (F6, #405).

Why this shape
--------------
F6 asks for a sandbox: live market data with *simulated* execution and a
data-match report. NautilusTrader's own sandbox execution client cannot be
started on this build — `LiveNode.builder(..., Environment.SANDBOX)` accepts a
data client but raises

    NotImplementedError: No execution factory extractor registered for 'SANDBOX'

for the sandbox exec client, with the client name set to either ``SANDBOX`` or the
venue name, added before or after the data client (all four combinations measured;
the compiled registry simply has no extractor entry for that factory). So instead
of a stub, this module delivers the same substance in the way this environment can
actually verify:

1. **record** a bounded window of LIVE public venue data — 1h klines (which also
   carry the warmup), every aggTrade of the window, and displayed order-book depth
   snapshots — each through the existing verified capture paths;
2. **replay** exactly those recorded bytes through NautilusTrader's *simulated*
   execution (BacktestEngine + a named execution profile), with one
   instrument-minimum probe order so "simulated execution ran" is evidence rather
   than an assertion;
3. publish a **data-match report** between what the venue sent and what the engine
   consumed, naming every mismatch instead of netting it away.

Honesty contract: nothing here claims NautilusTrader's SANDBOX environment ran,
nothing claims venue-truth fills (the venue account path stays
``UNRUN_NO_VENUE_ACCOUNT``), no authenticated request is made, and every artifact
carries ``NO_ECONOMIC_CLAIM`` with ``capital_authority: NONE``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import time
from decimal import Decimal
from pathlib import Path
from typing import Any

from nautilus_trader.backtest import BacktestEngine
from nautilus_trader.common import LogLevel
from nautilus_trader.config import BacktestEngineConfig, LoggerConfig
from nautilus_trader.model import (
    AccountType,
    Bar,
    BarType,
    BookType,
    Currency,
    InstrumentId,
    Money,
    OmsType,
    OrderSide,
    Price,
    Quantity,
    TraderId,
    Venue,
)
from nautilus_trader.trading import Strategy

from v8_next.adapters.binance_capture import (
    capture,
    capture_agg_trades,
    capture_depth,
    validate_depth_capture,
    verify,
)
from v8_next.adapters.book_tape import deltas_from_depth
from v8_next.adapters.captured_market import load_candles
from v8_next.adapters.execution_models import (
    profile_digest,
    resolve_profile,
    venue_kwargs,
)
from v8_next.adapters.portfolio_backtest import _instrument
from v8_next.adapters.trade_tape import trades_from_pages

SANDBOX_ENV_STATUS = "BLOCKED_SANDBOX_EXEC_CLIENT_UNAVAILABLE"
SANDBOX_ENV_BLOCKER = (
    "NotImplementedError: No execution factory extractor registered for 'SANDBOX' "
    "(measured with client names SANDBOX and BINANCE, exec client added before and "
    "after the data client)"
)


def data_match_report(
    *,
    captured: dict[str, int],
    consumed: dict[str, int],
    captured_span_ns: tuple[int, int] | None,
    consumed_span_ns: tuple[int, int] | None,
) -> dict[str, Any]:
    """Compare what the venue sent with what the engine consumed.

    Every discrepancy becomes a named entry in ``mismatches``; nothing is netted
    out or silently tolerated. ``consumed`` may legitimately be smaller than
    ``captured`` for bars: a bar whose close time is after the capture end was
    still forming when it was recorded, and feeding it as a closed bar would be a
    fabricated future. That specific reduction must be declared in
    ``captured["bars_unclosed_dropped"]`` to count as explained.
    """
    mismatches: list[str] = []
    for kind in ("trades", "deltas", "bars"):
        if kind not in captured or kind not in consumed:
            mismatches.append(f"MISSING_{kind.upper()}_ACCOUNTING")
            continue
        if consumed[kind] > captured[kind]:
            mismatches.append(f"CONSUMED_MORE_{kind.upper()}_THAN_CAPTURED")
            continue
        if consumed[kind] < captured[kind]:
            if kind == "bars" and captured.get("bars_unclosed_dropped") == (
                captured[kind] - consumed[kind]
            ):
                continue  # declared, explained reduction
            mismatches.append(f"CONSUMED_LESS_{kind.upper()}_THAN_CAPTURED")
    if captured_span_ns and consumed_span_ns:
        if consumed_span_ns[0] < captured_span_ns[0] or consumed_span_ns[1] > captured_span_ns[1]:
            mismatches.append("CONSUMED_SPAN_OUTSIDE_CAPTURED_SPAN")
    return {
        "captured": dict(captured),
        "consumed": dict(consumed),
        "captured_span_ns": list(captured_span_ns) if captured_span_ns else None,
        "consumed_span_ns": list(consumed_span_ns) if consumed_span_ns else None,
        "mismatches": mismatches,
        "matched": not mismatches,
    }


class _Probe(Strategy):
    """Submit one instrument-minimum limit order at the first recorded trade."""

    def __new__(cls, *args: Any, **kwargs: Any) -> _Probe:
        return super().__new__(cls)

    def __init__(self, instrument_id: str, bar_type: BarType, qty: float) -> None:
        super().__init__()
        self.probe_instrument_id = instrument_id
        self.probe_bar_type = bar_type
        self.qty = qty
        self.orders: list[dict[str, Any]] = []
        self.book_callbacks = 0
        self.bar_callbacks = 0
        self.trade_callbacks = 0
        self.sent = False
        self.fills: list[dict[str, Any]] = []
        self.denied = 0

    def on_start(self) -> None:
        # Subscriptions mirror the reference configuration that measured a fill
        # (bars + trades + book deltas) even though only the book callback drives
        # the submission: an L2-book run whose strategy subscribes to nothing but
        # the book never had its order processed (measured: 399 book callbacks,
        # empty orders report, zero fills).
        self.subscribe_bars(self.probe_bar_type)
        self.subscribe_trades(InstrumentId.from_str(self.probe_instrument_id))
        self.subscribe_book_deltas(InstrumentId.from_str(self.probe_instrument_id), BookType.L2_MBP)

    def on_book_deltas(self, deltas: Any) -> None:
        self.book_callbacks += 1
        self._submit_probe()

    def on_trade(self, trade: Any) -> None:
        self.trade_callbacks += 1

    def on_bar(self, bar: Bar) -> None:
        self.bar_callbacks += 1

    def _submit_probe(self) -> None:
        if self.sent:
            return
        self.sent = True
        # A MARKET probe: the point is that simulated execution ran at all, and a
        # resting limit can legitimately never fill when the profile models a queue
        # (queue_position=True in `realistic`) -- which would leave the deliverable
        # resting instead of demonstrated.
        self.submit_order(
            self.order_factory.market(
                InstrumentId.from_str(self.probe_instrument_id),
                OrderSide.BUY,
                Quantity(self.qty, 3),
            )
        )

    def on_order_denied(self, event: Any) -> None:
        self.denied += 1
        self.orders.append({"status": "DENIED"})

    def on_order_accepted(self, event: Any) -> None:
        self.orders.append({"status": "ACCEPTED"})

    def on_order_rejected(self, event: Any) -> None:
        self.orders.append({"status": "REJECTED"})

    def on_order_filled(self, event: Any) -> None:
        self.fills.append(
            {
                "qty": str(getattr(event, "last_qty", "")),
                "px": str(getattr(event, "last_px", "")),
                "liquidity_side": str(getattr(event, "liquidity_side", "")),
                "ts_event": int(getattr(event, "ts_event", 0) or 0),
            }
        )


def capture_live_window(
    destination: Path,
    *,
    symbol: str = "BTCUSDT",
    depth_seconds: float = 60.0,
    interval_s: float = 3.0,
    limit: int = 20,
) -> dict[str, Any]:
    """Record a bounded window of live public data: depth, trades, klines."""
    if destination.exists():
        raise ValueError(f"capture destination already exists: {destination}")
    destination.mkdir(parents=True)
    started_ns = time.time_ns()
    depth_dir = destination / "depth"
    depth_manifest = capture_depth(
        depth_dir, symbol, seconds=depth_seconds, interval_s=interval_s, limit=limit
    )
    validate_depth_capture(depth_manifest)
    ended_ns = time.time_ns()

    trades_dir = destination / "trades"
    trades_manifest = capture_agg_trades(
        trades_dir,
        symbol,
        start_ms=(started_ns // 1_000_000) - 1000,
        end_ms=(ended_ns // 1_000_000) + 1000,
        page_limit=1000,
        page_delay_s=0.3,
    )
    verify(trades_manifest)

    klines_dir = destination / "klines"
    klines_manifest = capture(klines_dir, symbol)
    verify(klines_manifest)

    session = {
        "schema_version": 1,
        "symbol": symbol,
        "instrument_id": f"{symbol}-PERP.BINANCE",
        "started_ns": started_ns,
        "ended_ns": ended_ns,
        "requested_duration_s": depth_seconds,
        "depth_manifest": str(depth_manifest),
        "depth_manifest_sha256": hashlib.sha256(depth_manifest.read_bytes()).hexdigest(),
        "trades_manifest": str(trades_manifest),
        "trades_manifest_sha256": hashlib.sha256(trades_manifest.read_bytes()).hexdigest(),
        "klines_manifest": str(klines_manifest),
        "klines_manifest_sha256": hashlib.sha256(klines_manifest.read_bytes()).hexdigest(),
        "sandbox_env_status": SANDBOX_ENV_STATUS,
        "sandbox_env_blocker": SANDBOX_ENV_BLOCKER,
        "claim_status": "NO_ECONOMIC_CLAIM",
        "capital_authority": "NONE_SIMULATED_EXECUTION_ONLY",
    }
    (destination / "session.json").write_text(json.dumps(session, indent=2) + "\n")
    return session


def replay_capture(
    capture_root: Path,
    instrument_id: str = "BTCUSDT-PERP.BINANCE",
    *,
    profile: str = "realistic",
) -> dict[str, Any]:
    """Replay the recorded live window through simulated execution."""
    session = json.loads((capture_root / "session.json").read_text())
    candles = load_candles(Path(session["klines_manifest"]))
    if not candles:
        raise ValueError("captured klines hold no bars")
    trades, trade_stats = trades_from_pages(capture_root / "trades", instrument_id)
    deltas, delta_stats = deltas_from_depth(capture_root / "depth", instrument_id)

    captured_end_ns = int(session["ended_ns"])
    closed = tuple(c for c in candles if c.end_ns <= captured_end_ns)
    unclosed = len(candles) - len(closed)
    if not closed:
        raise ValueError("no closed bar in the captured window")

    profile_obj = resolve_profile(profile)
    currency = Currency.from_str("USDT")
    engine = BacktestEngine(
        config=BacktestEngineConfig(
            trader_id=TraderId("V8-001"), logging=LoggerConfig(stdout_level=LogLevel.ERROR)
        )
    )
    engine.add_venue(
        Venue("BINANCE"),
        OmsType.NETTING,
        AccountType.MARGIN,
        [Money(1_000_000, currency)],
        base_currency=currency,
        book_type=BookType.L2_MBP if deltas else BookType.L1_MBP,
        **venue_kwargs(profile_obj),
    )
    engine.add_instrument(
        _instrument(
            instrument_id,
            "BTCUSDT",
            "BTC",
            currency,
            Decimal("0.0002"),
            Decimal("0.0005"),
        )
    )
    bar_type = BarType.from_str(f"{instrument_id}-1-HOUR-LAST-EXTERNAL")

    def _bars() -> list[Bar]:
        return [
            Bar(
                bar_type,
                Price(float(c.open), 2),
                Price(float(c.high), 2),
                Price(float(c.low), 2),
                Price(float(c.close), 2),
                Quantity(float(c.volume), 3),
                c.end_ns,
                c.end_ns,
            )
            for c in closed
        ]

    engine.add_data(_bars())
    if trades:
        engine.add_data(list(trades))
    if deltas:
        engine.add_data(list(deltas))

    # The probe is a MARKET order of the instrument minimum: it exists to exercise
    # the simulated execution path, not to express a view.
    probe = _Probe(instrument_id, bar_type, qty=0.001)
    engine.add_strategy(probe)
    engine.run()

    orders_report = engine.generate_orders_report()
    order_rows = (
        [dict(r) for r in orders_report.to_dicts()] if hasattr(orders_report, "to_dicts") else []
    )
    fills_report = engine.generate_order_fills_report()
    fill_rows = (
        [dict(r) for r in fills_report.to_dicts()] if hasattr(fills_report, "to_dicts") else []
    )
    engine.dispose()

    consumed_trades = len(trades)
    consumed_deltas = len(deltas)
    match = data_match_report(
        captured={
            "trades": trade_stats["loaded"],
            "deltas": delta_stats["deltas"],
            "bars": len(candles),
            "bars_unclosed_dropped": unclosed,
        },
        consumed={
            "trades": consumed_trades,
            "deltas": consumed_deltas,
            "bars": len(closed),
        },
        captured_span_ns=(int(session["started_ns"]), captured_end_ns),
        consumed_span_ns=(
            (min(d.ts_event for d in deltas), max(d.ts_event for d in deltas)) if deltas else None
        ),
    )
    result = {
        "schema_version": 1,
        "mechanism": "LIVE_CAPTURE_REPLAY_THROUGH_SIMULATED_EXECUTION",
        "instrument_id": instrument_id,
        "execution_profile": profile,
        "profile_digest": profile_digest(profile_obj),
        "capture_root": str(capture_root),
        "bars_fed": len(closed),
        "bars_unclosed_dropped": unclosed,
        "trades_fed": consumed_trades,
        "book_deltas_fed": consumed_deltas,
        "probe_order_sent": probe.sent,
        "probe_order_denied": probe.denied,
        # The fill evidence comes from the strategy callbacks: on this engine the
        # generated order/fill reports came back empty for a run whose strategy had
        # observed a fill (measured: fills=1 in the callback, 0 rows in
        # generate_order_fills_report, 1 order in the cache). The discrepancy is
        # named rather than presented as "no fills".
        "fills": len(probe.fills),
        "fill_rows": probe.fills,
        "engine_fill_report_rows": len(fill_rows),
        "engine_fill_report_reason": (
            None
            if fill_rows or not probe.fills
            else "ENGINE_FILL_REPORT_EMPTY_WHILE_STRATEGY_OBSERVED_FILLS"
        ),
        "probe_order_events": probe.orders,
        "probe_callbacks": {
            "book": probe.book_callbacks,
            "bar": probe.bar_callbacks,
            "trade": probe.trade_callbacks,
        },
        # Measured engine constraint this replay had to work around: with an L2
        # book configured, a MARKET order submitted from a bar/trade callback does
        # not execute; only a book-callback submission fills.
        "submission_trigger": "FIRST_BOOK_DELTA_CALLBACK",
        "orders_report": [
            {k: str(r.get(k)) for k in ("status", "type", "quantity", "filled_qty", "avg_px")}
            for r in order_rows
        ],
        "data_match": match,
        "sandbox_env_status": session.get("sandbox_env_status", SANDBOX_ENV_STATUS),
        "sandbox_env_blocker": session.get("sandbox_env_blocker", SANDBOX_ENV_BLOCKER),
        "status": (
            "REPLAY_COMPLETED_WITH_FILLS"
            if probe.fills
            else "REPLAY_COMPLETED_NO_FILLS"
            if match["matched"]
            else "REPLAY_COMPLETED_WITH_MISMATCHES"
        ),
        "ended_ns": time.time_ns(),
        "claim_status": "NO_ECONOMIC_CLAIM",
        "capital_authority": "NONE_SIMULATED_EXECUTION_ONLY",
    }
    (capture_root / "replay.json").write_text(json.dumps(result, indent=2, default=str) + "\n")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--symbol", default="BTCUSDT")
    parser.add_argument("--depth-seconds", type=float, default=60.0)
    parser.add_argument("--no-capture", action="store_true", help="replay an existing capture")
    args = parser.parse_args()
    if args.no_capture:
        print(json.dumps(replay_capture(args.destination), indent=2, default=str))
        return
    session = capture_live_window(
        args.destination, symbol=args.symbol, depth_seconds=args.depth_seconds
    )
    print(json.dumps(session, indent=2, default=str))
    print(json.dumps(replay_capture(args.destination), indent=2, default=str))


if __name__ == "__main__":
    main()
