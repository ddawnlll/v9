"""F2 (#401): captured L2 book + the depth-dependent execution knobs.

Evidence classes:

* **mechanics** — synthetic depth payloads written to ``tmp_path``: the CLEAR+ADD
  mapping, the top-N level bound, unparsable row accounting, one-sided snapshots,
  window restriction, and the published-profile promotion. MECHANICS ONLY.
* **evaluative** — a real bounded capture of BTCUSDT displayed depth (72
  snapshots) plus the real aggTrades of the same window
  (``research/tape/btcusdt-l2-2026*`` and ``btcusdt-trades-2026*``). The tests
  assert that the same capture digests identically twice, and that the two
  depth-dependent knobs **change the fill** — which is exactly the claim
  ``profile_summary`` is allowed to make when depth data is present.

The probe order is the smallest size that exercises the path; nothing here
asserts economic performance and the probe is not a strategy.
"""

from __future__ import annotations

import json
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
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

from v8_next.adapters.book_tape import (
    DEPTH_LEVEL_BOUND,
    book_digest,
    deltas_from_depth,
    depth_to_deltas,
)
from v8_next.adapters.execution_models import PROFILES, profile_summary
from v8_next.adapters.portfolio_backtest import _instrument
from v8_next.adapters.tape_paths import tape_path
from v8_next.adapters.trade_tape import trades_from_pages

QUAD_TAPE = tape_path("quad-1h-12m")
L2_CAPTURE = tape_path("btcusdt-l2-20260911T0320Z")
TRADE_CAPTURE = tape_path("btcusdt-trades-20260911T0320Z")
INSTRUMENT = "BTCUSDT-PERP.BINANCE"
BAR_TYPE_STR = f"{INSTRUMENT}-1-HOUR-LAST-EXTERNAL"
IID = InstrumentId.from_str(INSTRUMENT)
BT = BarType.from_str(BAR_TYPE_STR)
BALANCE = 1_000_000


# --------------------------------------------------------------------------- #
# mechanics
# --------------------------------------------------------------------------- #
def _snapshot(bids: list[list[str]], asks: list[list[str]], ts_ms: int) -> dict[str, Any]:
    """MECHANICS ONLY: a synthetic depth payload in the venue's shape."""
    return {
        "lastUpdateId": 1,
        "E": ts_ms,
        "T": ts_ms - 1,
        "bids": bids,
        "asks": asks,
    }


def test_snapshot_maps_to_clear_then_adds_bounded_by_the_level_bound(tmp_path: Path) -> None:
    payload = _snapshot(
        [[f"{100.00 - i * 0.01:.2f}", "1.0"] for i in range(DEPTH_LEVEL_BOUND + 5)],
        [[f"{100.01 + i * 0.01:.2f}", "2.0"] for i in range(DEPTH_LEVEL_BOUND + 5)],
        1_700_000_000_000,
    )
    deltas, stats = depth_to_deltas(
        payload, IID, sequence_base=0, ts_ns=1_700_000_000_000 * 1_000_000
    )
    assert stats["clears"] == 1
    assert stats["adds"] == 2 * DEPTH_LEVEL_BOUND
    assert stats["levels_beyond_bound"] == 10
    assert str(deltas[0].action) == "CLEAR"
    sides = [str(d.order.side) for d in deltas[1:]]
    assert sides.count("BUY") == DEPTH_LEVEL_BOUND
    assert sides.count("SELL") == DEPTH_LEVEL_BOUND
    assert all(int(d.sequence) == i for i, d in enumerate(deltas))


def test_unparsable_rows_are_counted_and_one_sided_snapshots_are_flagged() -> None:
    payload = _snapshot([["100.00", "1.0"], ["oops", "1.0"]], [["100.01", "2.0"]], 1)
    deltas, stats = depth_to_deltas(payload, IID, sequence_base=0, ts_ns=1)
    assert stats["rows"] == 3
    assert stats["rows_unparsable"] == 1
    assert stats["adds"] == 2
    assert len(deltas) == 3

    empty, empty_stats = depth_to_deltas(
        _snapshot([], [["100.01", "2.0"]], 1), IID, sequence_base=0, ts_ns=1
    )
    assert empty == []
    assert empty_stats["snapshots_empty_side"] == 1


def test_deltas_from_depth_windows_and_digests_deterministically(tmp_path: Path) -> None:
    """MECHANICS ONLY: a synthetic two-snapshot capture, written as a real manifest."""
    destination = tmp_path / "cap"
    destination.mkdir()
    artifacts = []
    for index, (ts_ms, bid) in enumerate(((1_000, "100.00"), (2_000, "100.50"))):
        payload = _snapshot([[bid, "1.0"]], [[f"{float(bid) + 0.01:.2f}", "2.0"]], ts_ms)
        path = destination / f"depth-{index:04d}.json"
        path.write_text(json.dumps(payload))
        artifacts.append(
            {
                "path": path.name,
                "sha256": "0" * 64,
                "source_url": "synthetic",
                "request_time_ns": ts_ms * 1_000_000,
                "received_time_ns": ts_ms * 1_000_000,
                "venue_time_ns": ts_ms * 1_000_000,
                "first": {"best_bid": bid, "best_ask": f"{float(bid) + 0.01:.2f}"},
            }
        )
    (destination / "manifest.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "symbol": "BTCUSDT",
                "granularity": "SYNTHETIC_MECHANICS_ONLY",
                "claim_status": "NO_ECONOMIC_CLAIM",
                "seconds_requested": 1.0,
                "achieved_seconds": 1.0,
                "interval_s": 1.0,
                "level_limit": 20,
                "snapshots": len(artifacts),
                "artifacts": artifacts,
            }
        )
    )
    first, stats = deltas_from_depth(destination, INSTRUMENT)
    second, _ = deltas_from_depth(destination, INSTRUMENT)
    assert stats["snapshots_in_window"] == 2
    assert book_digest(first) == book_digest(second)

    windowed, window_stats = deltas_from_depth(
        destination, INSTRUMENT, start_ns=1_999 * 1_000_000, end_ns=1_999 * 1_000_000
    )
    assert window_stats["snapshots_in_window"] == 1
    assert window_stats["snapshots_out_of_window"] == 1
    assert len(windowed) < len(first)


def test_profile_summary_promotes_the_knobs_only_with_depth_data() -> None:
    without = profile_summary("realistic")
    assert without["depth_data_available"] is False
    assert set(without["inert_knobs_without_depth_data"]) == {
        "liquidity_consumption",
        "queue_position",
    }
    assert without["depth_dependent_knobs_active"] == []
    assert without["depth_dependent_knobs_evidence"] == "NOT_APPLICABLE_NO_DEPTH_DATA"

    with_depth = profile_summary(replace(PROFILES["realistic"], depth_data_available=True))
    assert with_depth["inert_knobs_without_depth_data"] == []
    assert set(with_depth["depth_dependent_knobs_active"]) == {
        "liquidity_consumption",
        "queue_position",
    }
    assert (
        with_depth["depth_dependent_knobs_evidence"]
        == "MEASURED_FILL_CHANGES_WITH_CAPTURED_L2_BOOK"
    )


# --------------------------------------------------------------------------- #
# evaluative: real captured L2 book + real trades of the same window
# --------------------------------------------------------------------------- #
class _Probe(Strategy):
    """Submit one limit order at the capture's opening touch, then report fills."""

    def __new__(cls, *args: Any, **kwargs: Any) -> _Probe:
        return super().__new__(cls)

    def __init__(self, price: float, qty: float) -> None:
        super().__init__()
        self.price = price
        self.qty = qty
        self.sent = False
        self.fills: list[tuple[str, str, str]] = []
        self.denied = 0

    def on_start(self) -> None:
        self.subscribe_bars(BT)
        self.subscribe_book_deltas(IID, BookType.L2_MBP)

    def on_bar(self, bar: Bar) -> None:
        if self.sent:
            return
        self.sent = True
        self.submit_order(
            self.order_factory.limit(
                IID, OrderSide.BUY, Quantity(self.qty, 3), Price(self.price, 2)
            )
        )

    def on_order_denied(self, event: Any) -> None:
        self.denied += 1

    def on_order_filled(self, event: Any) -> None:
        self.fills.append(
            (
                str(getattr(event, "last_qty", "")),
                str(getattr(event, "last_px", "")),
                str(getattr(event, "liquidity_side", "")),
            )
        )


def _captures() -> tuple[tuple[Any, ...], tuple[Any, ...], int]:
    if not (L2_CAPTURE / "manifest.json").exists():
        pytest.skip(f"L2 capture absent at {L2_CAPTURE}")
    if not list(TRADE_CAPTURE.glob("trades-*.json")):
        pytest.skip(f"trade capture absent at {TRADE_CAPTURE}")
    deltas, stats = deltas_from_depth(L2_CAPTURE, INSTRUMENT)
    if not deltas:
        pytest.skip("L2 capture produced no deltas")
    trades, _ = trades_from_pages(TRADE_CAPTURE, INSTRUMENT)
    if not trades:
        pytest.skip("trade capture holds no trades")
    return deltas, trades, stats["deltas"]


def _opening_touch(deltas: tuple[Any, ...]) -> tuple[float, float]:
    bid = ask = None
    for delta in deltas[1 : 2 * DEPTH_LEVEL_BOUND + 1]:
        side = str(delta.order.side)
        if side == "BUY" and bid is None:
            bid = float(str(delta.order.price))
        elif side == "SELL" and ask is None:
            ask = float(str(delta.order.price))
        if bid is not None and ask is not None:
            break
    assert bid is not None and ask is not None
    return bid, ask


def _run(
    deltas: tuple[Any, ...],
    trades: tuple[Any, ...],
    *,
    price: float,
    qty: float,
    queue_position: bool,
    liquidity_consumption: bool,
) -> dict[str, Any]:
    t0 = int(deltas[0].ts_event)
    engine = BacktestEngine(
        config=BacktestEngineConfig(
            trader_id=TraderId("V8-001"), logging=LoggerConfig(stdout_level=LogLevel.ERROR)
        )
    )
    engine.add_venue(
        Venue("BINANCE"),
        OmsType.NETTING,
        AccountType.MARGIN,
        [Money(BALANCE, Currency.from_str("USDT"))],
        base_currency=Currency.from_str("USDT"),
        book_type=BookType.L2_MBP,
        bar_execution=True,
        trade_execution=True,
        queue_position=queue_position,
        liquidity_consumption=liquidity_consumption,
        reject_stop_orders=False,
    )
    engine.add_instrument(
        _instrument(
            INSTRUMENT,
            "BTCUSDT",
            "BTC",
            Currency.from_str("USDT"),
            Decimal("0.0002"),
            Decimal("0.0005"),
        )
    )
    engine.add_data(list(deltas))
    engine.add_data(list(trades))
    # The probe bar opens 1s into the capture, so the order rests while the real
    # book and trade flow pass it (a bar after the window would only ever match
    # the final book state).
    bar_ts = t0 + 1_000_000_000
    engine.add_data(
        [
            Bar(
                BT,
                Price(price, 2),
                Price(price, 2),
                Price(price, 2),
                Price(price, 2),
                Quantity(50.0, 3),
                bar_ts,
                bar_ts,
            )
        ]
    )
    probe = _Probe(price, qty)
    engine.add_strategy(probe)
    engine.run()
    fills = list(probe.fills)
    engine.dispose()
    return {
        "fills": fills,
        "fill_count": len(fills),
        "total": f"{sum(Decimal(f[0]) for f in fills):.3f}",
        "signature": book_digest(()) if not fills else str(fills),
        "denied": probe.denied,
    }


def test_same_capture_digests_identically_twice() -> None:
    deltas, _, count = _captures()
    again, _ = deltas_from_depth(L2_CAPTURE, INSTRUMENT)
    assert book_digest(deltas) == book_digest(again)
    print(f"\n[F2] capture deltas={count} digest={book_digest(deltas)[:16]}")


def test_liquidity_consumption_changes_the_fill_on_the_real_book() -> None:
    deltas, trades, count = _captures()
    _, ask = _opening_touch(deltas)
    qty = 5.0  # deliberately larger than the displayed size at the touch
    off = _run(
        deltas, trades, price=ask + 0.01, qty=qty, queue_position=False, liquidity_consumption=False
    )
    on = _run(
        deltas, trades, price=ask + 0.01, qty=qty, queue_position=False, liquidity_consumption=True
    )
    assert off["fill_count"] > 0 and on["fill_count"] > 0
    assert on["fills"] != off["fills"], "liquidity_consumption did not change the fill"
    print(
        f"\n[F2] liquidity_consumption qty={qty} deltas={count} "
        f"off={off['fill_count']} fills {off['fills'][:3]} | "
        f"on={on['fill_count']} fills {on['fills'][:3]}"
    )


def test_queue_position_changes_the_fill_on_the_real_book() -> None:
    deltas, trades, count = _captures()
    bid, _ = _opening_touch(deltas)
    off = _run(
        deltas, trades, price=bid, qty=0.05, queue_position=False, liquidity_consumption=False
    )
    on = _run(deltas, trades, price=bid, qty=0.05, queue_position=True, liquidity_consumption=False)
    assert off["fill_count"] > 0 and on["fill_count"] > 0
    assert off["fills"] != on["fills"], "queue_position did not change the fill"
    assert all(f[2] == "MAKER" for f in off["fills"] + on["fills"]), "probe was not passive"
    print(
        f"\n[F2] queue_position passive_at_bid deltas={count} "
        f"off={off['fill_count']} fills {off['fills']} | on={on['fill_count']} fills {on['fills']}"
    )


def test_repeat_run_is_bit_identical() -> None:
    deltas, trades, _ = _captures()
    bid, _ = _opening_touch(deltas)
    first = _run(
        deltas, trades, price=bid, qty=0.05, queue_position=True, liquidity_consumption=False
    )
    second = _run(
        deltas, trades, price=bid, qty=0.05, queue_position=True, liquidity_consumption=False
    )
    assert first["fills"] == second["fills"]


def test_a_book_window_disjoint_from_the_bars_cannot_claim_active_knobs() -> None:
    """Disjoint book/bar windows must not be published as depth-exercised execution.

    Measured engine behaviour with an L2 book configured: MARKET orders submitted
    from a bar callback do not execute at all, so a run whose captured book does
    not overlap its bar window silently produces zero fills. The wiring therefore
    reports the knobs as inert with a named reason instead of active.
    """
    if not (QUAD_TAPE / "tape.jsonl").exists():
        pytest.skip(f"real quad tape absent at {QUAD_TAPE}")
    if not (L2_CAPTURE / "manifest.json").exists():
        pytest.skip(f"L2 capture absent at {L2_CAPTURE}")
    from decimal import Decimal as D

    from v8_next.adapters.portfolio_backtest import SleeveSpec, run_portfolio_backtest
    from v8_next.evaluation.multitape import load_multitape

    tape = load_multitape(QUAD_TAPE, limit=120)
    deltas, _ = deltas_from_depth(L2_CAPTURE, INSTRUMENT)
    result = run_portfolio_backtest(
        {"BTCUSDT": tape.candles["BTCUSDT"]},
        (SleeveSpec("incumbent", 1, 28, 1.0),),
        (),
        per_leg_notional=D("1000"),
        taker_fee=D("0.0005"),
        initial_balance=D("10000"),
        execution_profile="realistic",
        book_deltas=deltas,
    )
    assert result["book_deltas_fed"] == len(deltas)
    assert result["book_window_overlap"] is False
    assert result["book_window_reason"] == "NO_OVERLAP_BETWEEN_CAPTURED_BOOK_AND_BAR_WINDOW"
    execution = result["execution"]
    assert execution["depth_data_available"] is False
    assert execution["depth_dependent_knobs_active"] == []
    assert set(execution["inert_knobs_without_depth_data"]) == {
        "liquidity_consumption",
        "queue_position",
    }
    print(
        f"\n[F2] disjoint windows: bar_window={result['bar_window_ns']} "
        f"book_window={result['book_window_ns']} fills={execution['fills_count']} "
        f"reason={result['book_window_reason']}"
    )

pytestmark = pytest.mark.slow  # #469: tape/engine file, fast loop excludes via -m "not slow"
