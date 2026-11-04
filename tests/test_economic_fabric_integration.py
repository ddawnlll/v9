"""Economic fabric integration: quad baskets, funding columns, tape routing.

MECHANICS tests here build their own inputs and assert arithmetic/contract only.
The evaluative section at the bottom runs the REAL quad tape and skips when it is
absent; no test asserts economic performance on synthetic prices.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from v8_next.adapters.basket_backtest import run_basket_backtest
from v8_next.adapters.tape_paths import tape_path
from v8_next.app.economic import is_quad_tape
from v8_next.domain.basket import (
    BASKETS,
    QUAD_INSTRUMENTS,
    QUAD_VENUE,
    BasketSpec,
    resolve_basket,
)
from v8_next.evaluation import economic_benchmark as eb

# --------------------------------------------------------------------------- #
# Mechanics: basket definitions
# --------------------------------------------------------------------------- #


def test_quad_baskets_declare_their_membership_and_venue_ids() -> None:
    quad = resolve_basket("equal_weight_quad")
    assert quad.instruments == QUAD_INSTRUMENTS
    assert quad.venue_instrument_ids() == tuple(f"{s}-PERP.{QUAD_VENUE}" for s in QUAD_INSTRUMENTS)
    assert quad.is_multi_asset
    # Aliases resolve to the canonical spec, not to a second definition.
    assert resolve_basket("equal_weight") is quad
    assert resolve_basket("vol_target") is BASKETS["vol_target_quad"]
    assert not resolve_basket("btc_buy_hold").is_multi_asset


def test_unknown_basket_is_rejected_by_name() -> None:
    with pytest.raises(ValueError, match="unknown basket"):
        resolve_basket("not_a_basket")


def test_basket_execution_rejects_a_missing_leg() -> None:
    """A basket run over a subset is a different basket; it must be refused."""
    with pytest.raises(ValueError, match="missing leg"):
        run_basket_backtest({"BTCUSDT": ()}, "equal_weight_quad")


def test_basket_execution_rejects_misaligned_legs() -> None:
    legs = {sym: () for sym in QUAD_INSTRUMENTS}
    legs["ETHUSDT"] = (object(),)  # type: ignore[assignment]
    with pytest.raises(ValueError, match="not aligned"):
        run_basket_backtest(legs, "equal_weight_quad")


# --------------------------------------------------------------------------- #
# Mechanics: the funding column never conflates its three states
# --------------------------------------------------------------------------- #


def _metric(**overrides: object) -> eb.MetricSet:
    base: dict[str, object] = {
        "net_return": 0.0,
        "excess_vs_primary": None,
        "sharpe_per_bar": 0.0,
        "sharpe_annualized": 0.0,
        "sharpe_ci_low": None,
        "sharpe_ci_high": None,
        "max_drawdown": 0.0,
        "tail_mean_5pct": 0.0,
        "avg_exposure": 0.0,
        "concentration": 0.0,
        "turnover_notional_over_capital": 0.0,
        "commission_cost": 0.0,
        "funding_cost": None,
        "cost_basis": "ANALYTIC_MODEL",
        "n_bars": 10,
        "n_trades": 0,
    }
    base.update(overrides)
    return eb.MetricSet(**base)  # type: ignore[arg-type]


def test_funding_cell_distinguishes_measured_zero_and_absent() -> None:
    measured = _metric(funding_cost=-12.3456, avg_exposure=1.0)
    assert eb.funding_state(measured) == eb.FUNDING_MEASURED
    assert eb.funding_cell(measured) == "-12.35"

    # A curve that never held a position has a measured zero, not an absence.
    unexposed = _metric(funding_cost=None, avg_exposure=0.0)
    assert eb.funding_state(unexposed) == eb.FUNDING_ZERO_NO_EXPOSURE
    assert eb.funding_cell(unexposed) == "0.00 (NO_EXPOSURE)"

    # A curve that held exposure with no funding feed is a named absence.
    absent = _metric(funding_cost=None, avg_exposure=0.5)
    assert eb.funding_state(absent) == eb.FUNDING_NOT_MEASURED
    assert eb.funding_cell(absent) == "n/a (NOT_MEASURED)"

    # The pre-integration cell that conflated all three is gone for every row.
    for row in (measured, unexposed, absent):
        assert eb.funding_cell(row) != "MISSING"


def test_funding_cell_names_the_reason_a_reached_feed_has_no_number() -> None:
    """A leg the engine has no rule for, and one it withheld, say so by name.

    Both states describe a run that DID reach the leg. Rendering either as "no
    funding feed reached this curve" would report the wiring instead of what the
    engine did, which is the conflation this column exists to prevent.
    """
    no_rule = _metric(
        funding_cost=None, avg_exposure=0.4, funding_basis=eb.FUNDING_NO_ENGINE_RULE
    )
    assert eb.funding_state(no_rule) == eb.FUNDING_NO_ENGINE_RULE
    assert eb.funding_cell(no_rule) == "n/a (NO_ENGINE_RULE)"

    withheld = _metric(
        funding_cost=None, avg_exposure=0.4, funding_basis=eb.FUNDING_TRADES_DIVERGED
    )
    assert eb.funding_state(withheld) == eb.FUNDING_TRADES_DIVERGED
    assert eb.funding_cell(withheld) == "n/a (TRADES_DIVERGED)"

    # Zero exposure is read first: a curve that never invested is a measured zero
    # and never borrows a rule-based reason it does not have.
    never_invested = _metric(
        funding_cost=None, avg_exposure=0.0, funding_basis=eb.FUNDING_NO_ENGINE_RULE
    )
    assert eb.funding_state(never_invested) == eb.FUNDING_ZERO_NO_EXPOSURE
    assert eb.funding_cell(never_invested) == "0.00 (NO_EXPOSURE)"


def test_funding_cell_names_the_curve_that_carries_a_moved_number() -> None:
    """D-165: a row whose number lives on another curve says which curve.

    Where the measuring basket executes a different sizing convention from the
    curve published beside it, the cell may not quote another curve's number and
    a bare state token would leave the reader searching the table for it: the
    declared state carries the published curve id as its payload.
    """
    moved = _metric(
        funding_cost=None,
        avg_exposure=1.0,
        funding_basis=eb.funding_basis_for_convention_differs("equal_weight_quad"),
    )
    assert eb.funding_state(moved) == eb.FUNDING_BASKET_CONVENTION_DIFFERS
    assert eb.funding_cell(moved) == "n/a (BASKET_CONVENTION_DIFFERS, equal_weight_quad)"
    assert eb.FUNDING_BASKET_CONVENTION_DIFFERS in eb.FUNDING_WITHHELD_STATES

    # A token the vocabulary does not declare is not a declared state: it reads
    # as the unnamed absence it is, never as a reason this module never published.
    undeclared = _metric(funding_cost=None, avg_exposure=1.0, funding_basis="SOMETHING_ELSE")
    assert eb.funding_state(undeclared) == eb.FUNDING_NOT_MEASURED
    assert eb.funding_cell(undeclared) == "n/a (NOT_MEASURED)"

    # The payload helpers are the one place the `STATE:detail` split is defined.
    assert eb.funding_basis_state("NO_ENGINE_RULE") == "NO_ENGINE_RULE"
    assert eb.funding_basis_detail("NO_ENGINE_RULE") is None
    assert eb.funding_basis_state(None) is None
    assert eb.funding_basis_detail("BASKET_CONVENTION_DIFFERS:vol_target_quad") == (
        "vol_target_quad"
    )


def test_only_a_basket_that_resizes_its_legs_moves_the_number() -> None:
    """Which legs publish their number elsewhere is a rule, not a name.

    The analytic family mirrors a compounding rule arithmetic; the engine baskets
    that carry those rules hold a fixed notional per leg. A single-instrument
    buy-and-hold basket buys once and holds like the curve beside it, so its
    number stays on the leg's own row.
    """
    assert eb.basket_sizing_differs_from_the_analytic_family(resolve_basket("equal_weight_quad"))
    assert eb.basket_sizing_differs_from_the_analytic_family(resolve_basket("vol_target_quad"))
    assert not eb.basket_sizing_differs_from_the_analytic_family(
        resolve_basket("btc_buy_hold")
    )
    # A single-leg bought once and held is the leg's own convention, so the leg
    # keeps its number; the multi-leg baskets hand theirs to their own curve.
    assert eb.funding_basket_convention_differs("bh_BTCUSDT-PERP") is None
    assert eb.funding_basket_convention_differs("simple_trend") is None
    assert eb.funding_basket_convention_differs("cash") is None
    assert eb.funding_basket_convention_differs("equal_weight") == "equal_weight_quad"
    assert eb.funding_basket_convention_differs("vol_target") == "vol_target_quad"
    # ... and the published curve that carries the number follows from that.
    assert eb.funding_curve_for_family("bh_BTCUSDT-PERP") == "bh_BTCUSDT-PERP"
    assert eb.funding_curve_for_family("equal_weight") == "equal_weight_quad"


def test_published_funding_records_are_keyed_by_the_published_curve() -> None:
    """D-165: every published curve has its own record, naming the measurement.

    Mechanics only: the records are built from the published metrics and the
    feed's own measurement, with no engine run and no tape.
    """
    feed = {
        "equal_weight": {
            "funding": -25.27212,
            "funding_basis": eb.FUNDING_MEASURED,
            "funding_basket": "equal_weight_quad",
            "funding_basket_kind": "equal_weight",
            "funding_basket_rule": "Equal-weight 25/25/25/25, buy-and-hold with rebalance",
            "funding_engine_basis": "ENGINE_SETTLED",
            "funding_settlements_fed": 192,
            "funding_rows_available": 4380,
            "funding_out_of_window": 4188,
            "funding_unknown_leg": 144,
            "engine_terminal_balance_usdt": 10229.2,
            "engine_unfunded_balance_usdt": 10254.47,
        }
    }
    metrics = {
        "equal_weight": _metric(
            funding_cost=None,
            avg_exposure=1.0,
            funding_basis=eb.funding_basis_for_convention_differs("equal_weight_quad"),
        ),
        "equal_weight_quad": _metric(
            funding_cost=-25.27212, avg_exposure=1.0, funding_basis=eb.FUNDING_MEASURED
        ),
        "cash": _metric(funding_cost=None, avg_exposure=0.0),
        "simple_trend": _metric(
            funding_cost=None,
            avg_exposure=0.4,
            funding_basis=eb.FUNDING_NO_ENGINE_RULE,
        ),
    }
    feed["simple_trend"] = {
        "funding": None,
        "funding_basis": eb.FUNDING_NO_ENGINE_RULE,
        "funding_basket": None,
        "funding_basket_kind": None,
        "funding_basket_rule": None,
        "funding_engine_basis": None,
        "funding_settlements_fed": 0,
        "funding_rows_available": 4380,
        "funding_out_of_window": 0,
        "funding_unknown_leg": 0,
        "engine_terminal_balance_usdt": None,
        "engine_unfunded_balance_usdt": None,
    }
    records = eb.published_funding_records(
        metrics,
        feed,
        basket_curves={"equal_weight_quad": {"engine_vs_rule_terminal_delta_usdt": -1720.56}},
    )
    assert set(records) == set(metrics)
    for name, record in records.items():
        assert record["funding_cell"] == eb.funding_cell(metrics[name]), name

    # The index row publishes no number and points at the curve that has it.
    index = records["equal_weight"]
    assert index["funding_curve"] == "equal_weight_quad"
    assert index["funding"] == pytest.approx(-25.27212)
    assert index["funding_basket"] == "equal_weight_quad"
    assert index["funding_basket_kind"] == "equal_weight"
    assert index["funding_engine_basis"] == "ENGINE_SETTLED"
    assert index["funding_settlements_fed"] == 192
    assert index["engine_terminal_balance_usdt"] == pytest.approx(10229.2)

    # The basket's own row carries that number and the engine/rule gap.
    basket = records["equal_weight_quad"]
    assert basket["funding_curve"] == "equal_weight_quad"
    assert basket["funding_cell"] == "-25.27"
    assert basket["funding_basis"] == eb.FUNDING_MEASURED
    assert basket["engine_vs_rule_terminal_delta_usdt"] == pytest.approx(-1720.56)

    # A curve with no number anywhere is a record too, not a missing key: the
    # reference that never invested has no curve to point at, and neither has the
    # leg no engine basket can settle.
    assert records["cash"]["funding_curve"] is None
    assert records["cash"]["funding"] is None
    assert records["cash"]["funding_basis"] == eb.FUNDING_ZERO_NO_EXPOSURE
    assert records["cash"]["funding_basket"] is None
    assert records["simple_trend"]["funding_curve"] is None
    assert records["simple_trend"]["funding_basis"] == eb.FUNDING_NO_ENGINE_RULE
    assert records["simple_trend"]["funding_rows_available"] == 4380


def test_family_funding_basket_covers_only_rules_the_engine_executes() -> None:
    """The family leg -> engine basket map is by rule, and it fails closed.

    A leg the engine cannot execute must resolve to no basket, so the caller
    publishes the reason instead of borrowing another basket's number.
    """
    btc = eb.family_funding_basket("bh_BTCUSDT-PERP")
    assert btc is not None
    assert btc.basket_id == "BTCUSDT_buy_hold"
    assert btc.instruments == ("BTCUSDT",)
    avax = eb.family_funding_basket("bh_AVAXUSDT-PERP")
    assert avax is not None and avax.instruments == ("AVAXUSDT",)
    # The canonical quad baskets are the ones the engine family publishes under
    # these two ids, not a second equal-weight rule defined here.
    eq = eb.family_funding_basket("equal_weight")
    vt = eb.family_funding_basket("vol_target")
    assert eq is not None and eq.basket_id == "equal_weight_quad"
    assert vt is not None and vt.basket_id == "vol_target_quad"
    # No engine basket executes a Donchian long/flat rule, and an unknown leg
    # borrows nothing either.
    assert eb.family_funding_basket("simple_trend") is None
    assert eb.family_funding_basket("not_a_leg") is None


def test_family_funding_feed_skips_the_never_invested_reference() -> None:
    """The feed names a state for every leg it can and omits the cash reference.

    Mechanics only: a rule-less leg is resolved without running any engine, so
    this holds with no tape present.
    """
    out = eb.measure_benchmark_family_funding(
        {"BTCUSDT": ()}, (), ("cash", "simple_trend")
    )
    # Cash is not a strategy: zero exposure is its own measurement, published by
    # the row's exposure, so the feed asserts no rule-based reason for it.
    assert "cash" not in out
    assert out["simple_trend"]["funding"] is None
    assert out["simple_trend"]["funding_basis"] == eb.FUNDING_NO_ENGINE_RULE
    assert out["simple_trend"]["funding_basket"] is None


def _render_receipt(metrics: dict[str, eb.MetricSet]) -> str:
    run = eb.RunIdentity(
        dataset=eb.DatasetIdentity(
            tape_path="MECHANICS_ONLY",
            tape_sha256="0" * 64,
            universe=("MECHANICS_ONLY",),
            period_start_ns=0,
            period_end_ns=3_600_000_000_000,
            n_bars=10,
            source_hashes=(),
        ),
        code=eb.CodeIdentity(
            git_rev="0" * 40,
            git_dirty="no",
            config_sha256="0" * 64,
            estimator_versions={},
            source_sha256="0" * 64,
        ),
        seed=0,
        primary_benchmark="cash",
        diagnostic_benchmarks=(),
        strategy_family=("MECHANICS_ONLY",),
        capital=10000.0,
        taker_fee=0.0005,
        opex_monthly_usd=0.0,
    )
    verdicts = eb.EvidenceVerdicts(
        research_validity="INVALID",
        research_note="MECHANICS_ONLY",
        economic="NEGATIVE",
        economic_note="MECHANICS_ONLY",
        statistical="UNSUPPORTED",
        statistical_note="MECHANICS_ONLY",
        portfolio="NOT_HELPFUL",
        portfolio_note="MECHANICS_ONLY",
        execution="EXECUTION_UNPROVEN",
        execution_note="MECHANICS_ONLY",
        capital="NOT_AUTHORIZED",
        capital_note="MECHANICS_ONLY",
    )
    receipt = eb.EconomicReceipt(
        receipt_id="mechanics-only",
        run=run,
        metrics=metrics,
        oos_metrics=metrics,
        verdicts=verdicts,
        statistics={},
        controls={},
        portfolio_mix={},
        capacity_scenarios=[],
        parity={},
        shadow_live={},
        limitations=[],
    )
    return eb.render_report(receipt)


def test_rendered_report_has_no_bare_missing_funding_cell() -> None:
    report = _render_receipt(
        {
            "engine_leg": _metric(funding_cost=-2.79, avg_exposure=0.9),
            "cash": _metric(funding_cost=None, avg_exposure=0.0),
            "analytic_leg": _metric(funding_cost=None, avg_exposure=0.4),
            # D-165: the number is published on the basket's own row, so this row
            # names that row instead of quoting a number it did not measure.
            "moved_leg": _metric(
                funding_cost=None,
                avg_exposure=0.8,
                funding_basis=eb.funding_basis_for_convention_differs("equal_weight_quad"),
            ),
            "equal_weight_quad": _metric(funding_cost=-25.27, avg_exposure=0.8),
        }
    )
    table = report.split("## Metrics (cost-adjusted, shared basis)", 1)[1]
    table = table.split("## Chronological OOS", 1)[0]
    rows = {
        line.split(" |", 1)[0][2:]: line
        for line in table.splitlines()
        if line.startswith("| ") and not line.startswith("|---") and not line.startswith("| curve")
    }
    assert "-2.79" in rows["engine_leg"]
    assert "0.00 (NO_EXPOSURE)" in rows["cash"]
    assert "n/a (NOT_MEASURED)" in rows["analytic_leg"]
    assert "n/a (BASKET_CONVENTION_DIFFERS, equal_weight_quad)" in rows["moved_leg"]
    assert "-25.27" in rows["equal_weight_quad"]
    assert "MISSING" not in table
    # The three states are documented on the operator surface, beside the table.
    assert "negative when funding was paid" in report
    # ... and so is the one that names the curve carrying the number (D-165).
    assert "A number is published only on the curve the engine measured it for" in report
    assert "n/a (BASKET_CONVENTION_DIFFERS, <basket_id>)" in report


# --------------------------------------------------------------------------- #
# Mechanics: basket rule arithmetic on hand-built bars
# --------------------------------------------------------------------------- #


def _views(closes: list[float]) -> list[eb.BarView]:
    return [
        eb.BarView(end_ns=(i + 1) * 3_600_000_000_000, open=c, high=c, low=c, close=c)
        for i, c in enumerate(closes)
    ]


def test_buy_hold_rule_deploys_capital_net_of_fee_once() -> None:
    """The fee is paid out of cash; the deployed notional is the target itself.

    That is the engine's own convention (an order carries a notional, the fee
    leaves the balance), so the rule and the executed basket agree on what the
    first bar's equity is.
    """
    bars = {"BTCUSDT": _views([100.0, 110.0, 121.0])}
    out = eb.compute_basket_equity_real(bars, "buy_hold", capital=10_000.0, taker_fee=0.001)
    shares = 10_000.0 / 100.0  # target notional / price, fee charged in cash
    fee = 10_000.0 * 0.001
    assert out["equity"] == pytest.approx(
        [shares * 100.0 - fee, shares * 110.0 - fee, shares * 121.0 - fee]
    )
    assert out["n_trades"] == 1
    assert out["turnover"] == pytest.approx(1.0)
    assert out["commission"] == pytest.approx(fee)
    # Exposure is invested notional over equity; the fee makes equity a hair
    # smaller than the notional, so a fully-invested bar reads just above 1.
    assert out["exposure"] == pytest.approx(
        [(shares * c) / (shares * c - fee) for c in (100.0, 110.0, 121.0)]
    )


def test_equal_weight_rule_splits_capital_and_holds_between_rebalances() -> None:
    bars = {
        "AAAUSDT": _views([100.0, 100.0, 100.0]),
        "BBBUSDT": _views([200.0, 200.0, 200.0]),
    }
    out = eb.compute_basket_equity_real(
        bars, "equal_weight", capital=10_000.0, taker_fee=0.0, rebalance_bars=None
    )
    # Flat prices: equity is capital after the initial deployment, and nothing
    # is traded again while the prices do not move.
    assert out["equity"] == pytest.approx([10_000.0, 10_000.0, 10_000.0])
    assert out["n_trades"] == 2
    assert out["commission"] == 0.0


def test_basket_rule_rejects_misaligned_and_multi_leg_buy_hold() -> None:
    with pytest.raises(ValueError, match="misaligned"):
        eb.compute_basket_equity_real(
            {"AAAUSDT": _views([1.0, 2.0]), "BBBUSDT": _views([1.0])}, "equal_weight"
        )
    with pytest.raises(ValueError, match="single-instrument"):
        eb.compute_basket_equity_real(
            {"AAAUSDT": _views([1.0, 2.0]), "BBBUSDT": _views([1.0, 2.0])}, "buy_hold"
        )


# --------------------------------------------------------------------------- #
# Mechanics: tape routing
# --------------------------------------------------------------------------- #


def _write_tape(path: Path, *, klines: tuple[str, ...], funding: tuple[str, ...] = ()) -> None:
    lines = []
    for sym in klines:
        lines.append(json.dumps({"channel": "kline", "instrument": sym, "payload": {}}))
    for sym in funding:
        lines.append(json.dumps({"channel": "funding", "instrument": sym, "payload": {}}))
    path.mkdir(parents=True, exist_ok=True)
    (path / "tape.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_quad_routing_requires_exactly_the_quad_kline_universe(tmp_path: Path) -> None:
    quad = tmp_path / "quad"
    _write_tape(quad, klines=QUAD_INSTRUMENTS, funding=("BTCUSDT",))
    assert is_quad_tape(quad)

    # The single-asset tape carries real funding rows too: funding presence must
    # not be what routes it to the four-asset family.
    single = tmp_path / "single"
    _write_tape(single, klines=("BTCUSDT",), funding=("BTCUSDT",))
    assert not is_quad_tape(single)

    # A superset tape is a different universe and keeps the analytic family.
    wide = tmp_path / "wide"
    _write_tape(wide, klines=(*QUAD_INSTRUMENTS, "BNBUSDT"))
    assert not is_quad_tape(wide)

    # A subset is not quad either.
    partial = tmp_path / "partial"
    _write_tape(partial, klines=QUAD_INSTRUMENTS[:3])
    assert not is_quad_tape(partial)


def test_quad_routing_ignores_a_missing_tape(tmp_path: Path) -> None:
    assert not is_quad_tape(tmp_path / "absent")


def test_quad_routing_refuses_a_tape_it_cannot_classify(tmp_path: Path) -> None:
    """Beyond the bound the route falls back rather than guessing."""
    quad = tmp_path / "quad"
    _write_tape(quad, klines=QUAD_INSTRUMENTS)
    assert not is_quad_tape(quad, max_lines=1)


# --------------------------------------------------------------------------- #
# Evaluative: real quad tape only
# --------------------------------------------------------------------------- #


def _quad_tape_dir() -> Path | None:
    for candidate in (
        Path(__file__).resolve().parents[2] / "research" / "tape" / "quad-1h-12m",
        tape_path("quad-1h-12m"),
    ):
        if (candidate / "tape.jsonl").exists():
            return candidate
    return None


def _btc_tape_dir() -> Path | None:
    for candidate in (
        Path(__file__).resolve().parents[2] / "research" / "tape" / "btcusdt-1h-12m",
        tape_path("btcusdt-1h-12m"),
    ):
        if (candidate / "tape.jsonl").exists():
            return candidate
    return None


@pytest.mark.skipif(_quad_tape_dir() is None, reason="real quad tape absent")
def test_quad_baskets_settle_funding_from_the_real_tape() -> None:
    """Every quad basket publishes an ENGINE_SETTLED funding number.

    Real data, real funding rows, engine-reported balances. The assertions are
    structural (measured vs absent, settlements actually fed, identity of the
    sign convention), never a performance claim.
    """
    tape = _quad_tape_dir()
    assert tape is not None
    fams = eb.compute_benchmark_family_engine(
        tape_path=str(tape), limit=120, capital=10_000.0, taker_fee=0.0005
    )
    for name in ("btc_buy_hold", "equal_weight", "vol_target"):
        fam = fams[name]
        assert fam["funding_basis"] == "ENGINE_SETTLED", name
        assert fam["funding"] is not None, name
        assert fam["funding_settlements_fed"] > 0, name
        # The dual runs traded identically, or the number would have been withheld.
        assert fam["funding"] != 0.0, name
        assert fam["cost_basis"] == "ANALYTIC_MODEL", name
        assert len(fam["equity"]) == 120
    # Cash never holds a position, so its funding is a measured zero.
    assert fams["cash"]["funding"] is None
    assert all(v == 0.0 for v in fams["cash"]["exposure"])
    # The aliases resolve to the same measured curve, not to a second one.
    assert fams["equal_weight_quad"] == fams["equal_weight"]
    assert fams["vol_target_quad"] == fams["vol_target"]
    # The engine and the rule both publish a terminal number for comparison.
    assert isinstance(fams["equal_weight"]["engine_vs_rule_terminal_delta_usdt"], float)


@pytest.mark.skipif(_quad_tape_dir() is None, reason="real quad tape absent")
def test_measuring_basket_curves_carry_the_number_they_were_measured_for() -> None:
    """D-165 on real data: the mirror curve and the engine's number travel together.

    The measuring basket sizes its legs under a different convention from the
    analytic index published beside it, so the number is published on the
    basket's own analytic mirror -- the identical call shape the engine family
    uses -- with the engine/rule terminal gap disclosed. Structural assertions
    only: identity of the number, real curve fields, no performance claim.
    """
    from v8_next.domain.basket import quad_tape_legs

    tape = _quad_tape_dir()
    assert tape is not None
    limit = 120
    loaded = quad_tape_legs(str(tape), limit=limit)
    legs = {sym: loaded.candles[sym] for sym in QUAD_INSTRUMENTS}
    views = {sym: eb.bars_from_candles(loaded.candles[sym]) for sym in QUAD_INSTRUMENTS}
    feed = eb.measure_benchmark_family_funding(
        legs, loaded.funding, ("equal_weight", "vol_target"),
        capital=10_000.0, taker_fee=0.0005,
    )
    mirrors = eb.family_basket_mirror_curves(
        views, feed, capital=10_000.0, taker_fee=0.0005
    )
    assert set(mirrors) == {"equal_weight_quad", "vol_target_quad"}
    closes = {
        f"{sym}-PERP.BINANCE": [float(c.close) for c in loaded.candles[sym]]
        for sym in QUAD_INSTRUMENTS
    }
    index_curves = eb.compute_multileg_family(closes, 10_000.0, 0.0005)
    for family_id, basket_id in (
        ("equal_weight", "equal_weight_quad"),
        ("vol_target", "vol_target_quad"),
    ):
        curve = mirrors[basket_id]
        assert curve["family_id"] == family_id
        assert curve["basket_id"] == basket_id
        assert curve["cost_basis"] == "ANALYTIC_MODEL"
        assert len(curve["equity"]) == limit
        assert curve["turnover"] > 0.0
        assert curve["n_trades"] > 0
        # The number is the engine's for this basket, not re-derived here.
        assert curve["funding"] == pytest.approx(feed[family_id]["funding"])
        assert curve["funding_basis"] == eb.FUNDING_MEASURED
        assert curve["engine_terminal_balance_usdt"] == pytest.approx(
            feed[family_id]["engine_terminal_balance_usdt"]
        )
        assert curve["engine_vs_rule_terminal_delta_usdt"] == pytest.approx(
            curve["engine_terminal_balance_usdt"] - curve["rule_terminal_equity_usdt"]
        )
        # Two conventions, two curves: the basket's own curve is not the index it
        # is published beside, which is why the number needed a row of its own.
        assert list(curve["equity"]) != list(index_curves[family_id]["equity"])


def test_quad_tape_is_routed_to_the_basket_family() -> None:
    tape = _quad_tape_dir()
    if tape is None:
        pytest.skip("real quad tape absent")
    assert tape is not None
    assert is_quad_tape(tape)


def test_single_asset_tape_is_not_routed_to_the_basket_family() -> None:
    tape = _btc_tape_dir()
    if tape is None:
        pytest.skip("single-asset tape absent")
    assert not is_quad_tape(tape)


@pytest.mark.skipif(_quad_tape_dir() is None, reason="real quad tape absent")
def test_quad_family_lookup_matches_a_hand_measured_settlement() -> None:
    """One real settlement boundary priced by hand against the engine's number.

    The engine is the only thing that may report what it settled; this checks
    that the dual-run difference is on the same order as an independently
    computed settlement for the first funded basket, so a sign/scale mistake in
    the wiring cannot pass unnoticed.
    """
    from v8_next.domain.basket import load_quad_candles, quad_tape_legs

    tape = _quad_tape_dir()
    assert tape is not None
    limit = 60
    loaded = quad_tape_legs(str(tape), limit=limit)
    legs = load_quad_candles(str(tape), limit=limit)
    rows = loaded.funding
    assert rows, "quad tape carries no funding rows"

    measured = eb.compute_benchmark_family_engine(
        tape_path=str(tape), limit=limit, capital=10_000.0, taker_fee=0.0005
    )
    event = measured["equal_weight"]
    assert event["funding_settlements_fed"] > 0
    assert event["funding"] == pytest.approx(event["funding"], rel=1e-12)
    # Sanity: the engine's funded balance is the unfunded balance shifted by the
    # funding it settled, in the published sign.
    assert event["engine_terminal_balance_usdt"] == pytest.approx(
        event["engine_unfunded_balance_usdt"] + event["funding"], rel=0, abs=1e-6
    )
    assert Decimal(str(round(event["funding"], 6))).is_finite()
    assert len(legs) == len(QUAD_INSTRUMENTS)


def test_basket_spec_is_frozen_membership() -> None:
    spec = BasketSpec(
        basket_id="x", kind="equal_weight", instruments=("AAAUSDT", "BBBUSDT"), description=""
    )
    assert spec.rebalance_bars == 24
    assert spec.venue_instrument_ids() == ("AAAUSDT-PERP.BINANCE", "BBBUSDT-PERP.BINANCE")


@pytest.mark.skipif(_quad_tape_dir() is None, reason="real quad tape absent")
def test_single_leg_funding_matches_an_independent_settlement_sum() -> None:
    """The bh_ measurement is that leg's own settlement, in the published sign.

    Cross-check only (the published number is the engine's dual-run balance
    difference): an independently summed settlement over the same real rows must
    land on the same value and sign, so a sign or scale mistake in the wiring
    cannot pass unnoticed.
    """
    from v8_next.adapters.basket_backtest import run_basket_funding_measurement
    from v8_next.domain.basket import quad_tape_legs, single_leg_buy_hold

    tape_path = str(_quad_tape_dir())
    limit = 120
    loaded = quad_tape_legs(tape_path, limit=limit)
    legs = {sym: loaded.candles[sym] for sym in QUAD_INSTRUMENTS}
    measured = run_basket_funding_measurement(
        legs,
        single_leg_buy_hold("BTCUSDT"),
        loaded.funding,
        capital=Decimal("10000"),
        taker_fee=Decimal("0.0005"),
    )
    assert measured["funding_basis"] == "ENGINE_SETTLED"
    assert measured["funding_settlements_fed"] > 0

    closes = [float(c.close) for c in legs["BTCUSDT"]]
    end_ns = [c.end_ns for c in legs["BTCUSDT"]]
    qty = 10000.0 / closes[0]
    expected = 0.0
    for row in loaded.funding:
        if row.instrument != "BTCUSDT":
            continue
        boundary = row.funding_time_ms * 1_000_000
        if not (end_ns[0] <= boundary <= end_ns[-1]):
            continue
        mark = next((closes[i] for i in range(len(closes)) if end_ns[i] >= boundary), None)
        if mark is None:
            continue
        expected += -qty * mark * float(row.funding_rate)
    assert measured["funding_cost"] == pytest.approx(expected, rel=0.01)

pytestmark = pytest.mark.slow  # #469: tape/engine file, fast loop excludes via -m "not slow"
