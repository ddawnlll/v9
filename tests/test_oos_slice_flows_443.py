"""#443 — the frozen OOS slice publishes ITS OWN cost/flow, not the window's.

Evidence classes:

* **mechanics** — the slice-flow arithmetic (`oos_slice_flow`) and the
  engine-fill partition, on synthetic engine ledgers and synthetic bars.
  MECHANICS ONLY: no evaluative weight, no economic claim, no real tape.
* **evaluative** — the canonical quad portfolio receipt regenerated on the real
  tape: acceptance (a)+(b) of #443 measured on the published rows, and the
  pre-slice flow recomputed independently from the same tape. Skips when the
  real tape is absent.

The defect: both OOS constructors handed the FULL-window `turnover`,
`commission` and `n_trades` to the slice's `MetricSet` while appending
`+OOS_SLICE` to its `cost_basis`, so a curve that buys once at bar 0 published
that fill's cost on the last-150-bar row.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from v8_next.app import portfolio as portfolio_mod
from v8_next.app.economic import (
    FLOW_SOURCE_ANALYTIC_PER_BAR,
    FLOW_SOURCE_ENGINE_FILLS,
    oos_slice_flow,
)
from v8_next.evaluation import economic_benchmark as eb

CAPITAL = 10_000.0
TAKER_FEE = 0.0005
FLOW_FIELDS = ("turnover_notional_over_capital", "commission_cost", "n_trades")
#: `MetricSet` field -> the frozen family builder's own field for the same quantity.
FAMILY_FLOW_KEYS = {
    "turnover_notional_over_capital": "turnover",
    "commission_cost": "commission",
    "n_trades": "n_trades",
}
TAPE_RELATIVE = "research/tape/quad-1h-12m"


def _tape_path() -> Any:
    """The real quad tape, from this checkout or an ancestor checkout."""
    project = eb.project_root()
    candidates = [parent / TAPE_RELATIVE for parent in (project.parent, *project.parents)]
    return next((p for p in candidates if (p / "tape.jsonl").is_file()), None)


def _bars(n: int, *, close: float = 100.0) -> list[eb.BarView]:
    """MECHANICS ONLY synthetic bars (flat closes): no evaluative weight."""
    return eb.bars_from_candles(
        [
            SimpleNamespace(end_ns=1_000_000_000 * (i + 1), open=close, high=close,
                            low=close, close=close)
            for i in range(n)
        ]
    )


# --------------------------------------------------------------------------- #
# mechanics — the slice-flow arithmetic
# --------------------------------------------------------------------------- #
def test_flow_before_the_slice_is_not_published_as_the_slices_own() -> None:
    """MECHANICS ONLY: a buy-and-hold's single fill (bar 0) is not the slice's."""
    turnover, commission, n_trades = oos_slice_flow(
        full_turnover=1.0, full_commission=5.0, full_n_trades=1,
        prefix_turnover=1.0, prefix_commission=5.0, prefix_n_trades=1,
        capital=CAPITAL, taker_fee=TAKER_FEE, source=FLOW_SOURCE_ANALYTIC_PER_BAR,
    )
    assert (turnover, commission, n_trades) == (0.0, 0.0, 0)
    # The engine path prices the same (empty) slice at the declared fee.
    assert oos_slice_flow(
        full_turnover=1.0, full_commission=5.0, full_n_trades=1,
        prefix_turnover=1.0, prefix_commission=5.0, prefix_n_trades=1,
        capital=CAPITAL, taker_fee=TAKER_FEE, source=FLOW_SOURCE_ENGINE_FILLS,
    ) == (0.0, 0.0, 0)


def test_flow_inside_the_slice_survives_as_the_difference() -> None:
    """MECHANICS ONLY: the part of the window's flow that happened in the slice."""
    analytic = oos_slice_flow(
        full_turnover=2.0, full_commission=10.0, full_n_trades=3,
        prefix_turnover=0.5, prefix_commission=3.0, prefix_n_trades=1,
        capital=CAPITAL, taker_fee=TAKER_FEE, source=FLOW_SOURCE_ANALYTIC_PER_BAR,
    )
    assert analytic == (1.5, 7.0, 2)
    # The engine path's commission is the declared taker fee on the slice's own
    # traded notional: 1.5 * 10000 * 0.0005.
    engine = oos_slice_flow(
        full_turnover=2.0, full_commission=10.0, full_n_trades=3,
        prefix_turnover=0.5, prefix_commission=3.0, prefix_n_trades=1,
        capital=CAPITAL, taker_fee=TAKER_FEE, source=FLOW_SOURCE_ENGINE_FILLS,
    )
    assert engine == (1.5, 7.5, 2)


def test_engine_fill_partition_counts_only_fills_inside_the_slice() -> None:
    """MECHANICS ONLY: what the pre-slice re-measurement must exclude.

    Three campaigns on one synthetic ledger: one entirely before the slice, one
    opened before it and closed inside it, one entirely inside it. Only the two
    fills of the last two campaigns are the slice's.
    """
    n, split = 20, 12
    bars = _bars(n)
    ns_of = {i: bars[i].end_ns for i in range(n)}

    def pos(open_idx: int, side: str = "BUY", qty: str = "1") -> dict[str, str]:
        return {"event_ns": str(ns_of[open_idx]), "side": side, "quantity": qty,
                "avg_px_open": "100"}

    def closed(close_idx: int, realized: str = "0") -> dict[str, str]:
        return {"event_ns": str(ns_of[close_idx]), "realized_pnl": realized,
                "avg_px_close": "100"}

    engine_result: dict[str, Any] = {
        "account": {"balance_total": f"{CAPITAL} USDT", "positions": [], "orders": []},
        "opened_positions": [pos(2), pos(8), pos(15)],
        "closed_positions": [closed(4), closed(16), closed(18)],
    }
    # One fill is 1 * 100 / 10000 = 0.01 turnover.
    full = eb.strategy_series_from_engine(engine_result, bars, CAPITAL, TAKER_FEE)
    prefix = eb.strategy_series_from_engine(engine_result, bars[:split], CAPITAL, TAKER_FEE)
    # Pre-slice (bars < 12): entry@2 + exit@4 and the entry@8. The exit@16 is a
    # fill of the slice (bar 16), so it must NOT be in the pre-slice measurement
    # even though the campaign it closes was opened before the slice.
    assert prefix["n_trades"] == 2
    assert prefix["turnover"] == pytest.approx(3.0 * 0.01, abs=1e-12)
    assert full["n_trades"] == 3
    assert full["turnover"] == pytest.approx(6.0 * 0.01, abs=1e-12)

    turnover, commission, n_trades = oos_slice_flow(
        full_turnover=float(full["turnover"]),
        full_commission=float(full["commission"]),
        full_n_trades=int(full["n_trades"]),
        prefix_turnover=float(prefix["turnover"]),
        prefix_commission=float(prefix["commission"]),
        prefix_n_trades=int(prefix["n_trades"]),
        capital=CAPITAL, taker_fee=TAKER_FEE, source=FLOW_SOURCE_ENGINE_FILLS,
    )
    # Inside the slice: the pre-slice campaign's exit@16, and the entry@15 with
    # its exit@18 -- three fills, one of them an entry.
    assert turnover == pytest.approx(3.0 * 0.01, abs=1e-12)
    assert n_trades == 1
    assert commission == pytest.approx(3.0 * 0.01 * CAPITAL * TAKER_FEE, abs=1e-9)


# --------------------------------------------------------------------------- #
# evaluative — the canonical quad receipt on the real tape
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def quad_receipt(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    """One canonical quad portfolio run; skips when the real tape is absent."""
    tape = _tape_path()
    if tape is None:
        pytest.skip(f"Real tape not found ({TAPE_RELATIVE})")
    out_dir = tmp_path_factory.mktemp("oos443")
    rc = portfolio_mod.main(
        [
            "--tape-path", str(tape), "--bars", "500", "--start-bar", "0",
            "--output-dir", str(out_dir), "--primary", "equal_weight",
            "--seed", "7", "--capital", str(CAPITAL), "--taker-fee", str(TAKER_FEE),
        ]
    )
    assert rc == 0
    receipts = sorted(out_dir.glob("economic_receipt_*.json"))
    assert receipts, "tagged economic receipt missing"
    receipt: dict[str, Any] = json.loads(receipts[0].read_text())
    receipt["_out_dir"] = str(out_dir)
    return receipt


def test_buy_and_hold_slice_publishes_no_cost_while_the_window_row_keeps_its(
    quad_receipt: dict[str, Any],
) -> None:
    """Acceptance (a): a fill that happened before the slice is not the slice's.

    `equal_weight` and every `bh_*` buy once at bar 0, 350 bars before the frozen
    OOS slice starts, so their OOS cost/flow must be 0 while the full-window row
    keeps the nonzero values of the fill it did make.
    """
    full = quad_receipt["metrics"]
    oos = quad_receipt["oos_metrics"]
    hold_ids = ["equal_weight", *[k for k in full if k.startswith("bh_")]]
    assert hold_ids, "no buy-and-hold curve in the receipt"
    for name in hold_ids:
        assert full[name]["turnover_notional_over_capital"] > 0.0
        assert full[name]["commission_cost"] > 0.0
        assert full[name]["n_trades"] > 0
        assert oos[name]["n_bars"] == 150, "the OOS row must still be the slice"
        assert oos[name]["turnover_notional_over_capital"] == 0.0, name
        assert oos[name]["commission_cost"] == 0.0, name
        assert oos[name]["n_trades"] == 0, name


def test_no_oos_flow_field_mirrors_the_window_unless_the_flow_is_in_the_slice(
    quad_receipt: dict[str, Any],
) -> None:
    """Acceptance (b): the published slice flow is the pre-slice flow subtracted.

    The pre-slice flow is recomputed here from the same real tape through the
    frozen family builder, and every OOS flow field must equal
    `full - pre_slice`. A field may only equal its full-window counterpart when
    the whole flow lies inside the slice (pre-slice flow 0).
    """
    tape = _tape_path()
    if tape is None:  # pragma: no cover - the fixture already skipped
        pytest.skip(f"Real tape not found ({TAPE_RELATIVE})")
    from v8_next.evaluation.multitape import load_multitape

    multi = load_multitape(tape, limit=500, offset=0)
    closes = {
        f"{raw}-PERP.BINANCE": [float(c.close) for c in series]
        for raw, series in multi.candles.items()
    }
    split = min(eb.OOS_FIT_BARS, multi.n_bars - 48)
    full_fam = eb.compute_multileg_family(closes, CAPITAL, TAKER_FEE)
    pre_fam = eb.compute_multileg_family(
        {inst: series[:split] for inst, series in closes.items()}, CAPITAL, TAKER_FEE
    )
    # D-165: the two basket rows are curves of their own, so the same subtraction
    # is asserted on them, through the builder that produced them.
    views = {sym: eb.bars_from_candles(series) for sym, series in multi.candles.items()}
    full_basket = eb.family_basket_mirror_curves(views, capital=CAPITAL, taker_fee=TAKER_FEE)
    pre_basket = eb.family_basket_mirror_curves(
        {sym: bars[:split] for sym, bars in views.items()},
        capital=CAPITAL,
        taker_fee=TAKER_FEE,
    )
    full = quad_receipt["metrics"]
    oos = quad_receipt["oos_metrics"]
    analytic = [n for n in full_fam if n in full and n in oos]
    assert len(analytic) >= 6, analytic
    # The added rows are enumerated here too: `full - pre` is a claim about every
    # published OOS row, not only the ones the family builder emits.
    basket_rows = [n for n in full_basket if n in full and n in oos]
    assert basket_rows == ["equal_weight_quad", "vol_target_quad"], basket_rows
    for name in analytic + basket_rows:
        pre = pre_fam[name] if name in pre_fam else pre_basket[name]
        for field in FLOW_FIELDS:
            full_value = full[name][field]
            pre_value = pre[FAMILY_FLOW_KEYS[field]]
            assert oos[name][field] == pytest.approx(full_value - pre_value, abs=1e-9), (
                f"{name}.{field}: OOS row is not the window minus its pre-slice part"
            )
            # Not a relabel: equality with the window row needs the flow to be
            # inside the slice.
            if full_value != 0 and pre_value == full_value:
                assert oos[name][field] != full_value, f"{name}.{field} mirrors the window"
    # The slice must actually contain flow on at least one curve, else (b) is
    # vacuous: the check above is only discriminating where flow exists in-slice.
    in_slice = [
        name for name in analytic + basket_rows
        if full[name]["turnover_notional_over_capital"]
        != (pre_fam if name in pre_fam else pre_basket)[name]["turnover"]
    ]
    assert in_slice, "no curve has flow inside the slice; (b) proved nothing"


def test_every_oos_row_earns_the_oos_slice_provenance_suffix(
    quad_receipt: dict[str, Any],
) -> None:
    """Acceptance (c) on the receipt: the `+OOS_SLICE` suffix must be earned.

    The suffix may stay only if every field of the row is the slice's. The row is
    the slice (`n_bars`, net return, Sharpe, excess CI already were); a cost/flow
    field taken from the window is not, and the model says so directly for the
    curves whose only fill is the buy at bar 0 -- 350 bars before the slice. The
    full-window row keeps its own values; nothing is deleted.
    """
    full = quad_receipt["metrics"]
    oos = quad_receipt["oos_metrics"]
    assert set(oos) == set(full)
    # `bh_*` and `equal_weight` buy once at bar 0 and never rebalance: the whole
    # flow of those curves precedes the slice by construction.
    single_buy_ids = ["equal_weight", *[k for k in full if k.startswith("bh_")]]
    for name, row in oos.items():
        assert row["cost_basis"].endswith("+OOS_SLICE"), row["cost_basis"]
        assert row["n_bars"] == 150
        # The engine rows price the slice's traded notional at the declared fee,
        # the one convention the full-window row is published in (#397).
        if name in ("portfolio_P", "portfolio_PE"):
            assert row["commission_cost"] == pytest.approx(
                row["turnover_notional_over_capital"] * CAPITAL * TAKER_FEE, rel=1e-9
            )
            assert full[name]["commission_cost"] == pytest.approx(
                full[name]["turnover_notional_over_capital"] * CAPITAL * TAKER_FEE, rel=1e-2
            )
        assert full[name]["n_bars"] == 500
        assert full[name]["turnover_notional_over_capital"] >= row[
            "turnover_notional_over_capital"
        ], f"{name}: the slice cannot trade more than the window it sits in"
        if name in single_buy_ids:
            # A bar-0 fill is not covered by a `+OOS_SLICE` row, so the suffix
            # there is a claim about a flow the row does not contain.
            for field in FLOW_FIELDS:
                assert row[field] == 0, f"{name}.{field} carries the window's fill"

pytestmark = pytest.mark.slow  # #469: tape/engine file, fast loop excludes via -m "not slow"
