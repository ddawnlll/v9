"""A submission is not proof of native protection acknowledgement."""
from decimal import Decimal
from types import SimpleNamespace

from v8_next.adapters.adaptive_strategy import AdaptiveSimStrategy, SimArmConfig
from v8_next.opportunities.models import ExposureDirection


def test_bracket_submission_records_creation_not_protection_ack() -> None:
    rows = []
    children = [SimpleNamespace(client_order_id=name) for name in ("entry", "stop", "target")]
    strategy = SimpleNamespace(
        _seen_open_positions=set(),
        cache=SimpleNamespace(orders_open=lambda **_: [], instrument=lambda _: SimpleNamespace(price_precision=2)),
        instrument_id="BTC",
        sim_config=SimArmConfig(enabled=True),
        _entry_quantity=lambda *_: (Decimal("1"), "TEST"),
        order_factory=SimpleNamespace(bracket=lambda **_: children),
        _tags=lambda *_: [],
        _order_facts={},
        _mark_order=lambda *_: None,
        submit_order_list=lambda *_: None,
        _audit=lambda event, *args, **kwargs: rows.append((event, kwargs)),
        _any_order_seen=False,
    )
    candle = SimpleNamespace(close=Decimal("100"), end_ns=1)
    AdaptiveSimStrategy._enter(strategy, "123456789abc", candle, None, ExposureDirection.LONG)
    assert [event for event, _ in rows] == ["ORDER_CREATED", "ORDER_CREATED"]
    assert [row["client_order_id"] for _, row in rows] == ["stop", "target"]


def test_reprotect_submission_does_not_claim_acknowledged_stop() -> None:
    rows = []
    quantity = SimpleNamespace(as_decimal=lambda: Decimal("1"))
    position = SimpleNamespace(id="P", quantity=quantity, avg_px_open=100, side="LONG")
    strategy = SimpleNamespace(
        _reprotect={"P"},
        cache=SimpleNamespace(instrument=lambda _: SimpleNamespace(
            min_quantity=quantity, price_precision=2
        )),
        instrument_id="BTC",
        _open_positions=lambda: [position],
        _protection_in_flight=lambda _: False,
        _protective_orders=lambda _: [],
        sim_config=SimArmConfig(enabled=True),
        _reduce_decision={},
        _position_decision={"P": "decision"},
        order_factory=SimpleNamespace(
            stop_market=lambda **kwargs: SimpleNamespace(client_order_id=kwargs["client_order_id"]),
            limit=lambda **kwargs: SimpleNamespace(client_order_id=kwargs["client_order_id"]),
        ),
        _tags=lambda *_: [],
        _order_facts={},
        _mark_order=lambda *_: None,
        submit_order=lambda *args, **kwargs: None,
        _audit=lambda event, *args, **kwargs: rows.append((event, kwargs)),
        _any_order_seen=False,
    )
    AdaptiveSimStrategy._reprotect_remaining(strategy, SimpleNamespace(end_ns=1))
    assert [event for event, _ in rows] == ["ORDER_CREATED", "ORDER_CREATED"]
    assert [row["arm_role"] for _, row in rows] == ["STOP", "TARGET"]
    assert all(row["position_id"] == "P" for _, row in rows)
