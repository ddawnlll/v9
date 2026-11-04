"""Execution-safety regressions for the swing engine (P0/P1 remediation).

All cases are deterministic native-engine simulations; no tape or network. The
tests inspect native orders, fills and positions, not only the strategy plan.
"""

from __future__ import annotations

import json
from decimal import Decimal
from types import SimpleNamespace

from nautilus_trader.backtest import BacktestEngine
from nautilus_trader.config import BacktestEngineConfig
from nautilus_trader.model import (
    AccountType,
    Bar,
    BarType,
    Money,
    OmsType,
    Venue,
)

from v8_next.adapters import swing_engine as se
from v8_next.adapters.swing_engine import (
    SwingEngineConfig,
    SwingEngineEvents,
    SwingEngineStrategy,
    build_instrument,
)
from v8_next.domain.market import Candle

H = 3_600_000_000_000
BASE_NS = 1_700_000_000_000_000_000
INSTRUMENT = "BTCUSDT-PERP.BINANCE"
BAR_TYPE = f"{INSTRUMENT}-1-HOUR-LAST-EXTERNAL"


class Decision:
    def __init__(self, *, expiry_bars: int = 336, direction: str = "LONG") -> None:
        self.entry_reference = "100"
        self.stop_price = "95" if direction == "LONG" else "105"
        self.target_price = "105" if direction == "LONG" else "95"
        self.direction = direction
        self.decision_ns = BASE_NS + H
        self.expires_ns = self.decision_ns + expiry_bars * H
        self.open_trade_expiry_bars = expiry_bars
        self.opportunity_id = "mechanics-opportunity"

    def as_dict(self) -> dict[str, object]:
        return {
            "opportunity_id": self.opportunity_id,
            "direction": self.direction,
            "entry_reference": self.entry_reference,
            "stop_price": self.stop_price,
            "target_price": self.target_price,
            "decision_ns": self.decision_ns,
            "expires_ns": self.expires_ns,
        }


def candle(index: int, open_: float, high: float, low: float, close: float, volume: float = 100.0) -> Candle:
    start = BASE_NS + index * H
    return Candle(
        instrument_id=INSTRUMENT,
        start_ns=start,
        end_ns=start + H,
        open=Decimal(str(open_)),
        high=Decimal(str(high)),
        low=Decimal(str(low)),
        close=Decimal(str(close)),
        volume=Decimal(str(volume)),
        received_ns=start + H,
        available_ns=start + H,
        source_hash="mechanics-only",
    )


def run_case(monkeypatch, candles: list[Candle], decision: Decision, **config_overrides):
    calls = {"n": 0}

    def fake_signal(frame, spec, bar_ns):
        calls["n"] += 1
        return decision if calls["n"] == 1 else None

    monkeypatch.setattr(se, "swing_signal", fake_signal)
    cfg = SwingEngineConfig(
        policy_id="plain_swing",
        instrument_id=INSTRUMENT,
        bar_type_str=BAR_TYPE,
        initial_balance=Decimal("10000"),
        risk_per_trade_fraction=Decimal("0.001"),
        **config_overrides,
    )
    currency = se.Currency.from_str(cfg.currency)
    venue = Venue(cfg.venue)
    engine = BacktestEngine(BacktestEngineConfig(bypass_logging=True))
    try:
        engine.add_venue(
            venue,
            OmsType.NETTING,
            AccountType.MARGIN,
            [Money(float(cfg.initial_balance), currency)],
            default_leverage=Decimal(1),
        )
        engine.add_instrument(build_instrument(cfg, currency))
        engine.add_data(
            [
                Bar(
                    BarType.from_str(BAR_TYPE),
                    se.Price(float(c.open), 2),
                    se.Price(float(c.high), 2),
                    se.Price(float(c.low), 2),
                    se.Price(float(c.close), 2),
                    se.Quantity(float(c.volume), 3),
                    c.end_ns,
                    c.end_ns,
                )
                for c in candles
            ]
        )
        strategy = SwingEngineStrategy(cfg, {c.end_ns: c for c in candles}, warmup_bars=0)
        engine.add_strategy(strategy)
        engine.run()
        snapshot = {
            "strategy": strategy,
            "bars": len(candles),
            "orders": {
                str(order.client_order_id): {
                    "status": str(order.status),
                    "is_reduce_only": bool(order.is_reduce_only),
                    "filled_qty": order.filled_qty.as_decimal(),
                    "position_id": str(order.position_id) if order.position_id else None,
                }
                for order in engine.cache.orders()
            },
            "open_positions": [
                {"side": str(p.side), "quantity": p.quantity.as_decimal()}
                for p in engine.cache.positions_open()
            ],
            "all_positions": [
                {"side": str(p.side), "quantity": p.quantity.as_decimal(), "closed": p.is_closed}
                for p in engine.cache.positions()
            ],
        }
    finally:
        engine.dispose()
    snapshot["events"] = strategy.events
    snapshot["net_filled"] = sum(
        (
            Decimal(str(fill["last_qty"]))
            if fill["side"] in ("BUY", "LONG")
            else -Decimal(str(fill["last_qty"]))
        )
        for fill in strategy.events.fills
    )
    return snapshot


def _bracket(case) -> dict:
    records = [
        record["bracket_submitted"]
        for record in case["events"].decisions
        if isinstance(record, dict) and "bracket_submitted" in record
    ]
    assert records, "the campaign must submit a bracket"
    return records[0]


def _assert_campaign_clean(case, *, entry_id: str, stop_id: str, target_id: str) -> None:
    assert case["open_positions"] == [], json.dumps(case["open_positions"])
    assert case["net_filled"] == 0, f"fill-derived net exposure {case['net_filled']} != 0"
    assert case["events"].safety_violations == []
    assert case["events"].unmatched_fills == []
    assert case["orders"][stop_id]["is_reduce_only"] is True
    assert case["orders"][target_id]["is_reduce_only"] is True
    assert case["orders"][entry_id]["status"] == "FILLED"


def test_stop_fill_retires_target_and_long_tail_revisit_is_inert(monkeypatch) -> None:
    bars = [
        candle(0, 100, 101, 99, 100),
        candle(1, 100, 101, 99, 100),
        candle(2, 100, 101, 94, 96),  # stop 95 triggers
    ]
    bars += [candle(i, 110, 111, 109, 110) for i in range(3, 300)]
    bars += [
        candle(300, 110, 111, 90, 96),  # revisit both old levels, hundreds of bars later
        candle(301, 110, 116, 109, 115),
    ]
    case = run_case(monkeypatch, bars, Decision())
    bracket = _bracket(case)
    stop_id = bracket["stop_client_order_id"]
    target_id = bracket["target_client_order_id"]
    _assert_campaign_clean(case, entry_id=bracket["entry_client_order_id"], stop_id=stop_id, target_id=target_id)
    assert case["orders"][stop_id]["status"] == "FILLED"
    assert case["orders"][target_id]["status"] == "CANCELED"
    assert case["events"].cancel_acks, "the sibling cancellation must be acknowledged"
    assert len(case["events"].fills) == 2  # entry + stop only


def test_target_fill_retires_stop_and_long_tail_revisit_is_inert(monkeypatch) -> None:
    bars = [
        candle(0, 100, 101, 99, 100),
        candle(1, 100, 101, 99, 100),
        candle(2, 100, 106, 99, 105),  # target 105 triggers
    ]
    bars += [candle(i, 110, 111, 109, 110) for i in range(3, 300)]
    bars += [candle(300, 110, 111, 90, 96), candle(301, 110, 116, 109, 115)]
    case = run_case(monkeypatch, bars, Decision())
    bracket = _bracket(case)
    stop_id = bracket["stop_client_order_id"]
    target_id = bracket["target_client_order_id"]
    _assert_campaign_clean(case, entry_id=bracket["entry_client_order_id"], stop_id=stop_id, target_id=target_id)
    assert case["orders"][target_id]["status"] == "FILLED"
    assert case["orders"][stop_id]["status"] == "CANCELED"
    assert len(case["events"].fills) == 2


def test_timeout_retires_exits_and_close_cannot_reverse(monkeypatch) -> None:
    bars = [
        candle(0, 100, 101, 99, 100),
        candle(1, 100, 101, 99, 100),
        candle(2, 100, 101, 99, 100),  # timeout at decision_ns + 2h
        candle(3, 110, 111, 90, 96),   # old levels revisited after closure
        candle(4, 110, 116, 109, 115),
    ]
    case = run_case(monkeypatch, bars, Decision(expiry_bars=2))
    bracket = _bracket(case)
    stop_id = bracket["stop_client_order_id"]
    target_id = bracket["target_client_order_id"]
    _assert_campaign_clean(case, entry_id=bracket["entry_client_order_id"], stop_id=stop_id, target_id=target_id)
    assert case["orders"][stop_id]["status"] == "CANCELED"
    assert case["orders"][target_id]["status"] == "CANCELED"
    assert case["events"].cancel_acks, "timeout cancellation must be acknowledged"


def test_same_bar_stop_and_target_eligibility_cannot_reverse(monkeypatch) -> None:
    bars = [
        candle(0, 100, 101, 99, 100),
        candle(1, 100, 101, 99, 100),
        candle(2, 100, 116, 84, 100),  # both stop 95 and target 105 inside one bar
        candle(3, 110, 111, 109, 110),
        candle(4, 110, 116, 109, 115),
    ]
    case = run_case(monkeypatch, bars, Decision())
    bracket = _bracket(case)
    assert case["open_positions"] == []
    assert case["net_filled"] == 0
    assert case["events"].safety_violations == []
    filled = [
        cid
        for cid, order in case["orders"].items()
        if cid in (bracket["stop_client_order_id"], bracket["target_client_order_id"])
        and order["status"] == "FILLED"
    ]
    assert len(filled) == 1, "exactly one bracket leg may fill; the other must be canceled"


def test_partial_entry_protects_actual_filled_quantity(monkeypatch) -> None:
    bars = [
        candle(0, 100, 101, 99, 100),
        candle(1, 100, 101, 99, 100, volume=0.5),
        candle(2, 100, 101, 99, 100, volume=0.5),
        candle(3, 100, 101, 99, 100, volume=0.5),
        candle(4, 100, 101, 99, 100, volume=0.5),
        candle(5, 100, 101, 99, 100, volume=0.5),
    ]
    case = run_case(monkeypatch, bars, Decision())
    bracket = _bracket(case)
    entry_id = bracket["entry_client_order_id"]
    fill_qty = sum(
        Decimal(str(f["last_qty"]))
        for f in case["events"].fills
        if f["client_order_id"] == entry_id
    )
    assert len({f["last_qty"] for f in case["events"].fills if f["client_order_id"] == entry_id}) >= 1
    assert Decimal(bracket["protection_quantity"]) == fill_qty
    assert case["orders"][bracket["stop_client_order_id"]]["is_reduce_only"] is True
    assert case["orders"][bracket["target_client_order_id"]]["is_reduce_only"] is True


def test_cancel_uses_client_order_id_never_order_object(monkeypatch) -> None:
    seen: list[str] = []
    original = SwingEngineStrategy.cancel_order

    def recording_cancel(self, client_order_id, *args, **kwargs):
        seen.append(type(client_order_id).__name__)
        return original(self, client_order_id, *args, **kwargs)

    monkeypatch.setattr(SwingEngineStrategy, "cancel_order", recording_cancel)
    bars = [
        candle(0, 100, 101, 99, 100),
        candle(1, 100, 101, 99, 100),
        candle(2, 100, 101, 94, 96),
        candle(3, 110, 111, 109, 110),
    ]
    run_case(monkeypatch, bars, Decision())
    assert seen, "the scenario must request a cancellation"
    assert set(seen) == {"ClientOrderId"}


def _bare_strategy(plan: dict) -> SwingEngineStrategy:
    strategy = SwingEngineStrategy.__new__(SwingEngineStrategy)
    strategy.events = SwingEngineEvents()
    strategy.plan = plan
    strategy.active_position_id = "P-1"
    strategy._seen_fill_events = set()
    strategy._cancel_requested = {}
    strategy._cancel_attempts = {}
    strategy._retiring_orders = {}
    strategy._unmatched_fill_ids = []
    return strategy


def test_cancel_rejection_is_visible_and_queryable() -> None:
    strategy = _bare_strategy({"status": "POSITION_OPEN", "stop_client_order_id": "O-STOP"})
    strategy._order = lambda _cid: SimpleNamespace(is_closed=False)  # type: ignore[method-assign]
    strategy.on_order_cancel_rejected(
        SimpleNamespace(client_order_id="O-STOP", reason="already_filled", ts_event=9)
    )
    assert strategy.events.cancel_rejections == [
        {"client_order_id": "O-STOP", "reason": "already_filled", "ts_event": 9}
    ]
    assert any(
        row["event"] == "CANCEL_REJECTED_ORDER_STILL_LIVE"
        for row in strategy.events.safety_violations
    )


def test_stop_rejection_triggers_reduce_only_fail_safe() -> None:
    strategy = _bare_strategy(
        {
            "status": "POSITION_OPEN",
            "entry_client_order_id": "O-ENTRY",
            "stop_client_order_id": "O-STOP",
            "target_client_order_id": "O-TARGET",
            "direction": "LONG",
        }
    )
    emergency: list[str] = []
    strategy._position_quantity = lambda: Decimal("1")  # type: ignore[method-assign]
    strategy._emergency_flatten = lambda code: emergency.append(code)  # type: ignore[method-assign]
    strategy.on_order_rejected(
        SimpleNamespace(client_order_id="O-STOP", reason="reduce_only_unsupported", ts_event=10)
    )
    assert strategy.events.protection_failures[0]["event"] == "STOP_REJECTED"
    assert emergency == ["STOP_REJECTED"]
    assert any(row["event"] == "PROTECTION_FAILED" for row in strategy.events.safety_violations)


def test_duplicate_fill_callback_cannot_duplicate_protection() -> None:
    strategy = _bare_strategy(
        {
            "status": "ENTRY_SUBMITTED",
            "entry_client_order_id": "O-ENTRY",
            "has_bracket": False,
            "direction": "LONG",
        }
    )
    strategy.active_position_id = None
    event = SimpleNamespace(
        client_order_id="O-ENTRY",
        order_side="BUY",
        last_qty="1",
        last_px="100",
        ts_event=11,
        commission="0",
        trade_id="T-1",
        position_id="P-1",
        event_id="E-1",
    )
    strategy.on_order_filled(event)
    strategy.on_order_filled(event)
    assert strategy.plan is not None
    assert strategy.plan["entry_filled_qty"] == "1"
    assert any(
        row["event_type"] == "DuplicateFillIgnored" for row in strategy.events.order_events
    )


def test_unmatched_late_fill_is_named_not_absorbed() -> None:
    strategy = _bare_strategy({})
    strategy.plan = None
    event = SimpleNamespace(
        client_order_id="O-STALE",
        order_side="BUY",
        last_qty="1",
        last_px="100",
        ts_event=12,
        commission="0",
        trade_id="T-2",
        position_id=None,
        event_id="E-2",
    )
    strategy.on_order_filled(event)
    assert strategy.events.unmatched_fills
    assert strategy.events.unmatched_fills[-1]["reason"] == "NO_LIVE_CAMPAIGN"
