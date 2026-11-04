"""#491 — the closed campaign's exit leg must enter the published equity path.

Mechanics-only regression test. Both engine->series builders used to remove the
position from the mark book *before* the measured exit price was marked, so the
segment from the last bar close to ``avg_px_close`` never entered ``equity_mtm``.
The terminal gap was then smeared uniformly over every bar by the #436 graft.

Gate 1 of the issue is asserted exactly, with no tolerance, for BOTH builders:
one campaign, entry 100 at bar 1, close 110 at bar 2, closes 100/105/120,
``taker_fee=0``, ``capital=1000`` => ``equity_mtm == [1000.0, 1005.0, 1010.0]``
and ``reconciliation_adjustment_per_bar == 0.0``.

The second test charges a commission so the graft is non-zero, and asserts the
R2 identity: the residual it absorbs equals exactly the excluded costs and
nothing else. Engine position records carry size under ``quantity`` (not
``qty``), which the builders read.
"""

from __future__ import annotations

from typing import Any

import pytest

from v8_next.evaluation import economic_benchmark as eb

CAPITAL = 1000.0
INST = "MECH-PERP.BINANCE"
STEP_NS = 3_600_000_000_000
T0_NS = 1_753_128_000_000_000_000
CLOSES = [100.0, 105.0, 120.0]
ENTRY_PX = 100.0
EXIT_PX = 110.0
QTY = 1.0


def _end_ns() -> list[int]:
    return [T0_NS + i * STEP_NS for i in range(len(CLOSES))]


def _engine(*, balance_total: float, realized_pnl: float, fee: float) -> dict[str, Any]:
    """One fully-closed campaign: entry at bar 1, exit at bar 2.

    ``realized_pnl`` is the engine's own net figure (its commission already
    inside it), so the closed-loop identity is exact. ``quantity`` and ``side``
    are the keys the builders actually read.
    """
    orders = [
        {"status": "FILLED", "filled_qty": str(QTY), "average_price": str(ENTRY_PX)},
        {"status": "FILLED", "filled_qty": str(QTY), "average_price": str(EXIT_PX)},
    ]
    return {
        "opened_positions": [
            {
                "position_id": "MECH-1",
                "instrument_id": INST,
                "side": "BUY",
                "quantity": str(QTY),
                "avg_px_open": str(ENTRY_PX),
                "event_ns": _end_ns()[1],
            }
        ],
        "closed_positions": [
            {
                "position_id": "MECH-1",
                "instrument_id": INST,
                "side": "BUY",
                "quantity": str(QTY),
                "avg_px_close": str(EXIT_PX),
                "event_ns": _end_ns()[2],
                "realized_pnl": f"{realized_pnl} USDT",
            }
        ],
        "account": {
            "balance_total": f"{balance_total} USDT",
            "orders": orders,
            "positions": [],
        },
    }


def _bars() -> list[eb.BarView]:
    return [
        eb.BarView(end_ns=ns, open=c, high=c, low=c, close=c)
        for ns, c in zip(_end_ns(), CLOSES, strict=True)
    ]


def _portfolio(engine: dict[str, Any], fee: float) -> dict[str, Any]:
    return eb.portfolio_series_from_engine(
        engine,
        {INST: list(CLOSES)},
        _end_ns(),
        CAPITAL,
        fee,
    )


def _strategy(engine: dict[str, Any], fee: float) -> dict[str, Any]:
    return eb.strategy_series_from_engine(engine, _bars(), CAPITAL, fee)


BUILDERS = {
    "portfolio": _portfolio,
    "strategy": _strategy,
}


@pytest.mark.parametrize("name", sorted(BUILDERS))
def test_491_exit_leg_booked_at_engine_close_exactly(name: str) -> None:
    """Issue gate 1: drop the +5 exit leg => [1000, 1005, 1005] and drift 2.5."""
    builder = BUILDERS[name]
    engine = _engine(balance_total=1010.0, realized_pnl=10.0, fee=0.0)

    ser = builder(engine, 0.0)

    # Exact equality, no tolerance: the only values the three bars may carry.
    assert ser["equity_mtm"] == [1000.0, 1005.0, 1010.0]
    assert ser["equity"] == [1000.0, 1005.0, 1010.0]
    assert ser["reconciliation_adjustment_per_bar"] == 0.0
    # The exit fill stays attributed to the engine's own close price.
    assert ser["cost_reconciliation"]["turnover_notional"]["exit_fills"] == 1
    assert ser["cost_reconciliation"]["turnover_notional"]["exit_price_source"] == {
        "engine_avg_px_close": 1,
        "bar_close_proxy": 0,
    }


@pytest.mark.parametrize("name", sorted(BUILDERS))
def test_491_exit_leg_booked_at_bar_close_proxy_when_engine_price_absent(name: str) -> None:
    """A missing `avg_px_close` keeps the named proxy count and is still marked.

    Fail closed means named, not zero-filled: the proxy exit is counted in
    ``bar_close_proxy_exits`` and priced at the documented bar close (110 on the
    exit bar, which here is the fixture's close[2]).
    """
    builder = BUILDERS[name]
    engine = _engine(balance_total=1010.0, realized_pnl=10.0, fee=0.0)
    del engine["closed_positions"][0]["avg_px_close"]
    # Exit at the bar close of bar 2 (120) => terminal gross = 20, balance 1020.
    engine["account"]["balance_total"] = "1020 USDT"
    engine["closed_positions"][0]["realized_pnl"] = "20 USDT"
    engine["account"]["orders"][1]["average_price"] = str(CLOSES[2])

    ser = builder(engine, 0.0)

    assert ser["equity_mtm"] == [1000.0, 1005.0, 1020.0]
    assert ser["reconciliation_adjustment_per_bar"] == 0.0
    assert ser["cost_reconciliation"]["turnover_notional"]["exit_price_source"] == {
        "engine_avg_px_close": 0,
        "bar_close_proxy": 1,
    }


@pytest.mark.parametrize("name", sorted(BUILDERS))
def test_491_graft_residual_is_exactly_the_excluded_costs(name: str) -> None:
    """R2 identity: with a commission charged the residual is that commission.

    Entry notional 100 + exit notional 110 at ``taker_fee=0.001`` => 0.21 USDT.
    The mark path deliberately excludes commission, so ``equity_mtm`` stays at
    1010 while the engine balance is 1009.79. The graft absorbs exactly -0.21 in
    total and the series publishes that identity as measured and explained.

    This is the SYNTHETIC case only: on a real run the identity state is measured and, in
    this checkout's reference window, still ``UNEXPLAINED_RESIDUAL`` (see
    ``test_published_cost_reconciliation_carries_the_exit_leg_identity`` and
    ``audit_out/critical_fixes_20261101/E2E_EVIDENCE.md``). The synthetic equality is not
    evidence that the real residual is explained.
    """
    builder = BUILDERS[name]
    fee = 0.001
    commission = (ENTRY_PX + EXIT_PX) * QTY * fee
    gross_pnl = (EXIT_PX - ENTRY_PX) * QTY
    balance = CAPITAL + gross_pnl - commission
    engine = _engine(
        balance_total=balance,
        realized_pnl=gross_pnl - commission,
        fee=fee,
    )

    ser = builder(engine, fee)
    recon = ser["cost_reconciliation"]
    identity = recon["exit_leg_identity"]
    drift = ser["reconciliation_adjustment_per_bar"]

    # The mark path is gross of the excluded cost, and the graft carries it.
    assert ser["equity_mtm"] == [1000.0, 1005.0, 1010.0]
    assert drift != 0.0
    assert drift * (len(CLOSES) - 1) == pytest.approx(-commission, abs=1e-12)
    assert recon["reconciliation_adjustment_total_usdt"] == pytest.approx(
        -commission, abs=1e-12
    )
    # R2: the residual equals the excluded costs and nothing else.
    assert recon["estimated_commission_total"] == pytest.approx(commission, abs=1e-12)
    assert identity["terms_usdt"]["commission"] == pytest.approx(commission, abs=1e-12)
    assert identity["terms_usdt"]["open_unrealized"] == 0.0
    assert identity["unexplained_usdt"] == pytest.approx(0.0, abs=1e-12)
    assert identity["expected_gap_usdt"] == pytest.approx(
        -(commission) if name == "strategy" else commission, abs=1e-12
    )
    assert identity["state"] == "EXPLAINED_BY_EXCLUDED_COSTS"


def test_published_cost_reconciliation_carries_the_exit_leg_identity() -> None:
    """#491 R2: the identity must reach the receipt, not stop inside the builder.

    The builders computed ``exit_leg_identity`` and the receipt-side projection dropped it,
    so a reader could never see whether the graft residual was the costs the mark path
    excludes or an unexplained remainder. §14 of the issue requires a residual that is not
    explainable by fees + funding to publish a NAMED non-reconciling state; that is only
    true if the block survives into the published receipt.
    """
    from v8_next.evaluation.economic_benchmark import published_cost_reconciliation

    identity = {
        "state": "UNEXPLAINED_RESIDUAL",
        "gap_usdt": 12.5,
        "expected_gap_usdt": 6.7,
        "unexplained_usdt": 5.8,
        "tolerance_usdt": 0.05,
        "terms_usdt": {"commission": 6.7, "funding": None, "open_unrealized": 0.0},
    }
    published = published_cost_reconciliation(
        {"engine_series_residual_measured": True, "exit_leg_identity": identity},
        cost_basis="VERIFIED_ENGINE",
    )
    assert published is not None
    assert published["exit_leg_identity"] == identity
    assert published["exit_leg_identity"]["state"] == "UNEXPLAINED_RESIDUAL"
    assert published["exit_leg_identity"]["unexplained_usdt"] == 5.8
    # A curve that measured no reconciliation still publishes no block at all.
    assert published_cost_reconciliation(None, cost_basis="ESTIMATED") is None
