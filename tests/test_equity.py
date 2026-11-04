from decimal import Decimal

import pytest

from v8_next.evaluation.equity import equity_losses


def marks():
    return [
        dict(
            end_ns=i * 10,
            observed_ns=i * 10,
            phase="PRE_STRATEGY_BAR_CALLBACK",
            currency="USDT",
            cash="100",
            unrealized_pnl=str(pnl),
            equity=str(100 + pnl),
            source_hash="test-only",
        )
        for i, pnl in enumerate((0, 10, 5), 1)
    ]


def test_open_exposure_returns_use_fixed_capital_and_actual_computation_clock():
    result = equity_losses(marks(), capital=Decimal(100), computed_ns=1000)
    assert [r.loss for r in result] == [Decimal("-.1"), Decimal(".05")]
    assert [r.available_ns for r in result] == [1000, 1000]
    assert result[0].start_ns == 10
    assert result[-1].end_ns == 30


@pytest.mark.parametrize(
    "field,value",
    [
        ("end_ns", 40),
        ("equity", "111"),
        ("cash", "NaN"),
        ("phase", "POST_CALLBACK"),
        ("currency", "BTC"),
        ("source_hash", ""),
        ("observed_ns", 1001),
    ],
)
def test_missing_or_incompatible_marks_reject(field, value):
    source = marks()
    source[-1][field] = value
    with pytest.raises(ValueError):
        equity_losses(source, capital=Decimal(100), computed_ns=1000)


def test_portfolio_input_identity_and_universe_are_verified():
    import hashlib
    import json
    from copy import deepcopy

    source = marks()
    for mark in source:
        mark["valuation_inputs"] = {
            symbol: {
                "price": "100",
                "event_ns": mark["end_ns"],
                "observed_ns": mark["observed_ns"],
                "source_hash": f"test-{symbol}",
            }
            for symbol in ("BTC", "ETH")
        }
        mark["source_hash"] = hashlib.sha256(
            json.dumps(mark["valuation_inputs"], sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
    assert len(equity_losses(source, capital=Decimal(100), computed_ns=1000)) == 2
    for field, value in (("price", "101"), ("event_ns", 0), ("observed_ns", 1001)):
        changed = deepcopy(source)
        changed[1]["valuation_inputs"]["ETH"][field] = value
        with pytest.raises(ValueError):
            equity_losses(changed, capital=Decimal(100), computed_ns=1000)
    changed = deepcopy(source)
    del changed[1]["valuation_inputs"]["ETH"]
    with pytest.raises(ValueError, match="universe"):
        equity_losses(changed, capital=Decimal(100), computed_ns=1000)
