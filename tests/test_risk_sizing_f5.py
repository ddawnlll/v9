"""F5: NT risk primitives — FixedRiskSizer sizing, notional cap, margin models.

Mechanics tests are arithmetic-only. The strategy-level test runs the engine
over the real F1 fixture and asserts the decision record carries the sizing
mode (default path unchanged).
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest
from nautilus_trader.model import Currency, Money, Price

from v8_next.adapters.execution_models import (
    ExecutionProfile,
    build_margin_model,
    venue_kwargs,
)
from v8_next.adapters.risk_sizing import (
    check_max_notional,
    risk_sized_quantity,
)
from v8_next.adapters.tape_paths import tape_path


def _instrument():
    from v8_next.adapters.portfolio_backtest import _instrument as make

    c = Currency.from_str("USDT")
    return make("BTCUSDT-PERP.BINANCE", "BTCUSDT", "BTC", c, Decimal("0.0002"), Decimal("0.0005"))


def _money(amount: str = "10000"):
    return Money(float(amount), Currency.from_str("USDT"))


def test_sizer_matches_hand_computation() -> None:
    # $10000 * 1% = $100 risk over a $2000 stop -> 0.05 BTC
    q = risk_sized_quantity(
        _instrument(), Price(100000.0, 2), Price(98000.0, 2),
        _money(), Decimal("0.01"),
    )
    assert q.as_decimal() == Decimal("0.05")


def test_sizer_scales_linearly_with_risk_fraction() -> None:
    one = risk_sized_quantity(
        _instrument(), Price(100000.0, 2), Price(98000.0, 2),
        _money(), Decimal("0.01"),
    ).as_decimal()
    two = risk_sized_quantity(
        _instrument(), Price(100000.0, 2), Price(98000.0, 2),
        _money(), Decimal("0.02"),
    ).as_decimal()
    assert two == one * 2


def test_sizer_rejects_out_of_range_fractions() -> None:
    with pytest.raises(ValueError, match="risk_fraction"):
        risk_sized_quantity(
            _instrument(), Price(100000.0, 2), Price(98000.0, 2),
            _money(), Decimal("0"),
        )
    with pytest.raises(ValueError, match="risk_fraction"):
        risk_sized_quantity(
            _instrument(), Price(100000.0, 2), Price(98000.0, 2),
            _money(), Decimal("1.5"),
        )


def test_notional_cap_denies_above_and_allows_below() -> None:
    px = Price(100000.0, 2)
    allowed, notional = check_max_notional(Decimal("0.05"), px, Decimal("10000"))
    assert allowed is True
    assert notional == Decimal("5000")
    denied, notional = check_max_notional(Decimal("0.5"), px, Decimal("10000"))
    assert denied is False
    assert notional == Decimal("50000")


def test_no_cap_means_no_guard() -> None:
    allowed, _ = check_max_notional(Decimal("999"), Price(100000.0, 2), None)
    assert allowed is True


def test_margin_model_builds_and_validates() -> None:
    assert build_margin_model(ExecutionProfile(
        name="x", fill_model="default", prob_fill_on_limit=1.0,
        prob_slippage=0.0, random_seed=0,
    )) is None
    std = build_margin_model(ExecutionProfile(
        name="x", fill_model="default", prob_fill_on_limit=1.0,
        prob_slippage=0.0, random_seed=0, margin_model="standard",
    ))
    assert type(std).__name__ == "StandardMarginModel"
    lev = build_margin_model(ExecutionProfile(
        name="x", fill_model="default", prob_fill_on_limit=1.0,
        prob_slippage=0.0, random_seed=0, margin_model="leveraged",
    ))
    assert type(lev).__name__ == "LeveragedMarginModel"
    with pytest.raises(ValueError, match="margin_model"):
        ExecutionProfile(
            name="x", fill_model="default", prob_fill_on_limit=1.0,
            prob_slippage=0.0, random_seed=0, margin_model="fancy",
        )


def test_margin_model_reaches_the_venue() -> None:
    kw = venue_kwargs(ExecutionProfile(
        name="x", fill_model="default", prob_fill_on_limit=1.0,
        prob_slippage=0.0, random_seed=0, margin_model="standard",
    ))
    assert type(kw["margin_model"]).__name__ == "StandardMarginModel"
    assert venue_kwargs("baseline")["margin_model"] is None


def test_default_path_records_flat_sizing_mode() -> None:
    """Regression: with no risk options the decision record is unchanged in
    behaviour (sizing_mode FLAT on every decision). Runs on the real F1 klines."""
    import json

    from v8_next.adapters.expert_strategy import run_expert_strategy_backtest
    from v8_next.domain.market import Candle

    fix = Path(__file__).parent / "fixtures" / "f1-btc-12h" / "klines.json"
    rows = json.loads(fix.read_text())
    candles = tuple(
        Candle(
            "BTCUSDT-PERP.BINANCE",
            int(k[0]) * 1_000_000, (int(k[6]) + 1) * 1_000_000,
            Decimal(str(k[1])), Decimal(str(k[2])), Decimal(str(k[3])),
            Decimal(str(k[4])), Decimal(str(k[5])),
            (int(k[6]) + 1) * 1_000_000, (int(k[6]) + 1) * 1_000_000,
            "f5-real-klines",
        )
        for k in rows
    )
    result = run_expert_strategy_backtest(candles)
    assert result["decisions"], "expected decisions on 12 real bars"
    assert all(d["sizing_mode"] == "FLAT" for d in result["decisions"])


def test_dust_order_floored_to_min_notional_instead_of_denied() -> None:
    """Min-notional fix: a signal sized below the venue minimum is rounded UP
    to the minimum (capped by max_notional) with a named action, never sent
    to the venue to die with NOTIONAL_BELOW_MINIMUM."""
    from v8_next.adapters.expert_strategy import (
        ExpertStrategyConfig,
        run_expert_strategy_backtest,
    )
    from v8_next.evaluation.gate_resolution import load_tape_candles

    tape = tape_path("btcusdt-1h-12m", "tape.jsonl")
    if not tape.exists():
        pytest.skip("single-asset real tape absent")
    candles = tuple(load_tape_candles(tape, limit=120))
    if not candles:
        pytest.skip("tape loaded no candles")
    result = run_expert_strategy_backtest(
        candles,
        ExpertStrategyConfig(
            min_support_quorum=1,
            max_contradiction_tolerance=28,
            order_quantity=Decimal("0.0000001"),
        ),
    )
    actions = [d["action"] for d in result["decisions"]]
    assert any(a.startswith("SIZED_TO_MIN_NOTIONAL") for a in actions), (
        f"dust signal was not floored: {sorted(set(actions))}"
    )
    assert not any("BELOW_MIN" in a or "NOTIONAL_BELOW" in a for a in actions), (
        "dust reached the venue gate instead of being floored"
    )
    print(f"\n[F5] floored={[a for a in actions if a=='SIZED_TO_MIN_NOTIONAL']}")

pytestmark = pytest.mark.slow  # #469: tape/engine file, fast loop excludes via -m "not slow"
