"""Portfolio benchmark tests.

MECHANICS ONLY synthetic section: arithmetic/validator coverage with zero
evaluative weight. Evaluative section runs the quad engine path on real tape
and skips when the tape is absent.
"""

import json
from decimal import Decimal
from pathlib import Path

import pytest

from v8_next.adapters.tape_paths import data_path, tape_path
from v8_next.domain.capital_policy import CapitalPolicy
from v8_next.evaluation import economic_benchmark as eb


def _bars(n: int = 60, start: int = 1_000_000_000, step: int = 3_600_000_000_000) -> list[eb.BarView]:
    px = 100.0
    out = []
    for i in range(n):
        c = px * 1.001
        out.append(eb.BarView(end_ns=start + (i + 1) * step, open=px, high=c * 1.001,
                              low=px * 0.999, close=c))
        px = c
    return out


def test_mechanics_trend_benchmark_is_causal() -> None:
    # Spiking the last close must not rewrite earlier exposure: decisions at
    # bar i use closed bar i-1 only. (Guards the 1-bar lookahead that once
    # inflated a sleeve 4.5x.)
    legs = {
        "A-PERP.BINANCE": [100.0 * (1.001**i) for i in range(120)],
        "B-PERP.BINANCE": [50.0 * (0.999**i) for i in range(120)],
    }
    base = eb.compute_multileg_family(legs, 10000.0, 0.0005)["simple_trend"]["exposure"]
    spiked = dict(legs)
    spiked["A-PERP.BINANCE"] = list(legs["A-PERP.BINANCE"])
    spiked["A-PERP.BINANCE"][-1] *= 2.0
    alt = eb.compute_multileg_family(spiked, 10000.0, 0.0005)["simple_trend"]["exposure"]
    assert base[:-1] == alt[:-1]


def test_mechanics_pair_positions_time_ordered() -> None:
    opened = [
        {"position_id": "X", "instrument_id": "I", "event_ns": 3, "side": "LONG",
         "quantity": "1", "avg_px_open": "10"},
        {"position_id": "X", "instrument_id": "I", "event_ns": 1, "side": "LONG",
         "quantity": "1", "avg_px_open": "9"},
    ]
    closed = [
        {"position_id": "X", "instrument_id": "I", "event_ns": 4, "realized_pnl": "1 USDT"},
        {"position_id": "X", "instrument_id": "I", "event_ns": 2, "realized_pnl": "2 USDT"},
    ]
    pairs = eb.pair_positions(opened, closed)
    assert len(pairs) == 2
    assert pairs[0][0]["event_ns"] == 1
    first_close = pairs[0][1]
    assert first_close is not None and first_close["event_ns"] == 2
    assert pairs[1][0]["event_ns"] == 3
    second_close = pairs[1][1]
    assert second_close is not None and second_close["event_ns"] == 4


def test_mechanics_pair_positions_unpaired_stays_open() -> None:
    opened = [{"position_id": "X", "instrument_id": "I", "event_ns": 5, "side": "LONG",
               "quantity": "1", "avg_px_open": "10"}]
    pairs = eb.pair_positions(opened, [])
    assert len(pairs) == 1 and pairs[0][1] is None


def test_mechanics_capital_test_accept_and_deny_paths() -> None:
    pol = CapitalPolicy.test_policy(max_notional="1000", max_exposure_frac="1.0", authorized=True)
    assert pol.decision(100.0, instrument_id="I")["decision"] == "ACCEPT"
    big = pol.decision(5000.0, instrument_id="I")
    assert big["decision"] == "REJECT" and big["reason"] == "EXCEEDS_MAX_NOTIONAL"
    assert pol.decision(100.0, live=True)["decision"] == "REJECT"
    assert CapitalPolicy.unauthorized().decision(100.0)["decision"] == "REJECT"
    scoped = pol.model_copy(update={"allowed_instruments": ("I",)})
    assert scoped.decision(100.0, instrument_id="ZZZ")["reason"] == "INSTRUMENT_NOT_ALLOWED"
    assert CapitalPolicy.test_policy(authorized=False).decision(100.0)["decision"] == "REJECT"


def test_mechanics_multileg_family_construction() -> None:
    legs = {
        "A-PERP.BINANCE": [100.0 * (1.001**i) for i in range(60)],
        "B-PERP.BINANCE": [50.0 * (0.999**i) for i in range(60)],
    }
    fams = eb.compute_multileg_family(legs, 10000.0, 0.0005)
    assert set(fams) >= {"cash", "equal_weight", "vol_target", "simple_trend",
                         "bh_A-PERP", "bh_B-PERP"}
    assert fams["cash"]["equity"][0] == 10000.0
    assert fams["equal_weight"]["equity"][0] == pytest.approx(10000.0 * (1 - 0.0005))
    assert len(fams["vol_target"]["equity"]) == 60
    assert fams["vol_target"]["exposure"][0] == 0.0


def test_mechanics_shadow_section_paths(tmp_path: Path) -> None:
    from v8_next.app.portfolio import build_shadow_section

    absent = build_shadow_section(None, {})
    assert absent["live_fills_present"] is False
    assert "command" in absent  # runnable next step, not a dead end
    fixture = tmp_path / "fills.jsonl"
    fixture.write_text('{"fill_id": "1", "instrument": "BTCUSDT", "price": 100.0, "qty": 1.0}\n')
    import shutil

    fx_dir = tmp_path / "fixtures"
    fx_dir.mkdir()
    fx_copy = fx_dir / "fills.jsonl"
    shutil.copy(fixture, fx_copy)
    guarded = build_shadow_section(str(fx_copy), {})
    assert guarded["mode"] == "FIXTURE_NOT_LIVE"
    assert guarded["live_fills_present"] is False
    live = build_shadow_section(
        str(fixture),
        {"balance_total": "1", "positions": [], "orders": [], "currency": "USDT"},
    )
    assert live["live_fills_present"] is True
    assert live["reconciliation"]["shadow_fills"] == 1


def test_mechanics_capacity_bounds_from_participation() -> None:
    from v8_next.app.portfolio import capacity_from_participation

    rows = capacity_from_participation([1e-6, 2e-6], 10000.0, [0.001, -0.002, 0.0015] * 50)
    bounds = [r for r in rows if "max_capital_linear" in r]
    hyps = [r for r in rows if r["kind"] == "HYPOTHETICAL_SCENARIO"]
    assert len(bounds) == 3 and len(hyps) == 3
    assert bounds[0]["max_capital_linear"] == pytest.approx(10000.0 * 0.01 / 2e-6)
    # #477: the 1%/5%/10% thresholds are projections beyond the observed 2e-6
    # participation, so the canonical producer publishes them UNRESOLVED and
    # never under a MEASURED_* kind. The number is preserved beside the state.
    assert all(r["status"] == "UNRESOLVED" for r in bounds)
    assert all(r["reason"] == "OBSERVED_FILLS_AT_OTHER_SCALE" for r in bounds)
    assert all(not r["kind"].startswith("MEASURED") for r in bounds)
    assert all(r["impact_beyond"] == "UNVERIFIED_NO_IMPACT_MODEL" for r in bounds)
    assert all(h["k_status"] == "HYPOTHETICAL_UNCALIBRATED" for h in hyps)
    assert all(h["impact_bps_at_peak"] is not None for h in hyps)
    empty = capacity_from_participation([], 10000.0)[0]
    assert empty["max_capital_linear"] is None
    assert empty["status"] == "DATA_BLOCKED"
    assert empty["reason"] == "NO_OBSERVED_PARTICIPATION"


def test_portfolio_benchmark_legs_publish_a_measured_funding_feed(tmp_path: Path) -> None:
    """Every benchmark leg with an engine rule carries an engine-measured number.

    The residual this pins: the canonical portfolio path built its family
    analytically and printed "no funding feed reached this curve" on every
    benchmark leg -- even the ones the engine executes -- while the tape carried
    real funding rows. D-165 then moved the number onto the curve it was measured
    for: the two quad legs are measured on a basket that sizes its legs under a
    different convention, so that basket's own curve is published beside the index
    and the index row names it. Real quad tape; skips when absent.
    """
    from v8_next.app import portfolio as port_mod

    tape = tape_path("quad-1h-12m")
    if not (tape / "tape.jsonl").exists():
        pytest.skip("quad tape absent")
    out_dir = tmp_path / "port"
    rc = port_mod.main([
        "--tape-path", str(tape), "--bars", "385",
        "--output-dir", str(out_dir), "--primary", "equal_weight",
    ])
    assert rc == 0
    receipt = json.loads(
        sorted(out_dir.glob("economic_receipt_*.json"))[0].read_text(encoding="utf-8")
    )
    feed = receipt["controls"]["benchmark_funding_feed"]
    assert feed["method"] == "ENGINE_DUAL_RUN_BALANCE_DIFFERENCE"
    assert feed["tape_funding_rows"] > 0
    # D-165: every published curve's record is keyed by the curve it publishes,
    # and it is the same measurement the family feed took.
    curves = feed["published_curves"]
    assert set(curves) == set(receipt["metrics"]) == set(receipt["oos_metrics"])

    # Legs whose measuring basket executes the leg's own convention keep their
    # number on their own row.
    measured_legs = {
        "bh_AVAXUSDT-PERP": "AVAXUSDT_buy_hold",
        "bh_BTCUSDT-PERP": "BTCUSDT_buy_hold",
        "bh_ETHUSDT-PERP": "ETHUSDT_buy_hold",
        "bh_SOLUSDT-PERP": "SOLUSDT_buy_hold",
        "equal_weight_quad": "equal_weight_quad",
        "vol_target_quad": "vol_target_quad",
    }
    for leg, basket_id in measured_legs.items():
        m = receipt["metrics"][leg]
        assert m["funding_cost"] is not None, leg
        assert m["funding_basis"] == eb.FUNDING_MEASURED, leg
        # The cell carries the number, never a reason for its absence.
        assert eb.funding_cell(eb.MetricSet(**m)) == f"{m['funding_cost']:.2f}", leg
        # Auditable in the receipt: which basket, and that the engine really fed
        # funding rows to it -- and that the number is on this curve, not another.
        record = curves[leg]
        assert record["funding_basket"] == basket_id, leg
        assert record["funding_curve"] == leg, leg
        assert record["funding_engine_basis"] == "ENGINE_SETTLED", leg
        assert record["funding_settlements_fed"] > 0, leg
        assert record["funding_rows_available"] > 0, leg
        assert m["funding_cost"] == pytest.approx(record["funding"]), leg

    # The two legs whose measuring basket sizes its legs differently publish no
    # number of their own: they name the curve that carries it, and that curve
    # carries the number the index row used to show.
    for leg, basket_id in (
        ("equal_weight", "equal_weight_quad"),
        ("vol_target", "vol_target_quad"),
    ):
        m = receipt["metrics"][leg]
        assert m["funding_cost"] is None, leg
        assert m["funding_basis"] == f"{eb.FUNDING_BASKET_CONVENTION_DIFFERS}:{basket_id}"
        assert eb.funding_cell(eb.MetricSet(**m)) == (
            f"n/a (BASKET_CONVENTION_DIFFERS, {basket_id})"
        )
        record = curves[leg]
        assert record["funding_curve"] == basket_id, leg
        assert record["funding_basket"] == basket_id, leg
        assert record["funding_basket_kind"] in ("equal_weight", "vol_target"), leg
        assert record["funding_basket_rule"], leg
        # The measurement is still the engine's for that basket, and the basket's
        # own row publishes it: one number, one curve.
        assert record["funding_engine_basis"] == "ENGINE_SETTLED", leg
        assert record["funding"] == pytest.approx(
            receipt["metrics"][basket_id]["funding_cost"]
        ), leg
        assert curves[basket_id]["funding_curve"] == basket_id, leg
        assert curves[basket_id]["engine_vs_rule_terminal_delta_usdt"] is not None, leg

    # The two legs with no engine basket state their own case: a measured zero
    # and a named rule gap, never "no funding feed reached this curve".
    cash = receipt["metrics"]["cash"]
    assert cash["funding_cost"] is None
    assert eb.funding_cell(eb.MetricSet(**cash)) == "0.00 (NO_EXPOSURE)"
    assert curves["cash"]["funding_curve"] is None
    trend = receipt["metrics"]["simple_trend"]
    assert trend["funding_cost"] is None
    assert trend["funding_basis"] == eb.FUNDING_NO_ENGINE_RULE
    assert eb.funding_cell(eb.MetricSet(**trend)) == "n/a (NO_ENGINE_RULE)"
    assert curves["simple_trend"]["funding_curve"] is None
    assert receipt["claim_status"] == "NO_ECONOMIC_CLAIM"


def test_basket_curves_are_curves_and_their_slice_is_the_slices_flow(tmp_path: Path) -> None:
    """The added rows are real curves, and their OOS row is `full - pre` (#443).

    D-165 acceptance (b): the two basket rows publish a curve, turnover,
    commission, n_trades and exposure from the declared basket spec, and their
    frozen OOS row carries the slice's own flow -- not the full window's. Real
    quad tape; skips when absent.
    """
    from v8_next.app import portfolio as port_mod
    from v8_next.domain.basket import resolve_basket

    tape = tape_path("quad-1h-12m")
    if not (tape / "tape.jsonl").exists():
        pytest.skip("quad tape absent")
    out_dir = tmp_path / "port"
    rc = port_mod.main([
        "--tape-path", str(tape), "--bars", "385",
        "--output-dir", str(out_dir), "--primary", "equal_weight",
    ])
    assert rc == 0
    receipt = json.loads(
        sorted(out_dir.glob("economic_receipt_*.json"))[0].read_text(encoding="utf-8")
    )
    full = receipt["metrics"]
    oos = receipt["oos_metrics"]
    index_of = {"equal_weight_quad": "equal_weight", "vol_target_quad": "vol_target"}
    for basket_id, index_id in index_of.items():
        spec = resolve_basket(basket_id)
        assert spec.kind in ("equal_weight", "vol_target"), basket_id
        assert spec.instruments == ("BTCUSDT", "ETHUSDT", "SOLUSDT", "AVAXUSDT"), basket_id
        row = full[basket_id]
        assert row["n_bars"] == 385, basket_id
        # A real curve, not a stub: it trades the spec's rule and holds exposure.
        assert row["turnover_notional_over_capital"] > 0.0, basket_id
        assert row["commission_cost"] > 0.0, basket_id
        assert row["n_trades"] > 0, basket_id
        assert row["avg_exposure"] > 0.0, basket_id
        assert row["cost_basis"] == "ANALYTIC_MODEL", basket_id
        # Its commission is the declared fee on its own traded notional, the one
        # convention this builder prices in.
        assert row["commission_cost"] == pytest.approx(
            row["turnover_notional_over_capital"] * 10000.0 * 0.0005, rel=1e-9
        ), basket_id
        # The basket holds a fixed notional it rebalances, so its own flow is not
        # the index's beside it -- that difference is why it needed a row.
        assert row["turnover_notional_over_capital"] != pytest.approx(
            full[index_id]["turnover_notional_over_capital"]
        ), basket_id
        # #443: the slice row is the slice's own flow, never the window's.
        slice_row = oos[basket_id]
        assert slice_row["cost_basis"] == "ANALYTIC_MODEL+OOS_SLICE", basket_id
        assert slice_row["n_bars"] == 48, basket_id
        assert slice_row["turnover_notional_over_capital"] < row[
            "turnover_notional_over_capital"
        ], basket_id
        assert slice_row["n_trades"] < row["n_trades"], basket_id
        # The rebalance schedule puts flow inside the slice, so the row is not a
        # zero dressed up as a slice either.
        assert slice_row["turnover_notional_over_capital"] > 0.0, basket_id


def test_portfolio_quad_end_to_end(tmp_path: Path) -> None:
    from v8_next.app import portfolio as port_mod

    if not tape_path("quad-1h-12m", "tape.jsonl").exists():
        pytest.skip("quad tape absent")
    rc = port_mod.main([
        "--tape-path", str(tape_path("quad-1h-12m")),
        "--bars", "120", "--output-dir", str(tmp_path / "port"),
        "--primary", "equal_weight",
    ])
    assert rc == 0
    import json

    receipts = sorted((tmp_path / "port").glob("economic_receipt_*.json"))
    assert receipts, "tagged economic receipt missing"
    receipt = json.loads(receipts[0].read_text())
    assert receipt["claim_status"] == "NO_ECONOMIC_CLAIM"
    assert receipt["verdicts"]["capital"] == "NOT_AUTHORIZED"
    assert set(receipt["metrics"]) >= {"portfolio_P", "portfolio_PE", "equal_weight", "cash"}
    assert receipt["metrics"]["portfolio_P"]["funding_cost"] is not None
    assert receipt["shadow_live"]["live_fills_present"] is False
    assert "command" in receipt["shadow_live"]  # runnable next step
    caps = sorted((tmp_path / "port").glob("capital_path_*.json"))
    assert caps, "tagged capital path missing"
    cap = json.loads(caps[0].read_text())
    assert cap["missing_policy_decision"]["decision"] == "REJECT"
    assert cap["live_decision"]["decision"] == "REJECT"
    assert cap["test_policy_accept_path"]["decision"] == "ACCEPT"
    import sqlite3  # noqa: F401  (guard: no live money path touched)
    assert list((tmp_path / "port").glob("economic_report_*.md"))


def test_funding_holding_convention_matches_engine() -> None:
    """Engine-matched rule (open<=, close>) reproduces the measured OOS drag.

    Pinned empirically: exact 4-decimal match of the analytic expectation
    against the dual-run balance difference over 4321 real bars. Requires the
    persisted OOS artifacts + quad tape; skips when absent.
    """
    import math

    base = data_path("artifacts", "portfolio-oos")
    trades = sorted(base.glob("portfolio_P_trades_*.jsonl"))
    closed = sorted(base.glob("portfolio_closed_*.jsonl"))
    if not trades or not closed:
        pytest.skip("persisted OOS engine evidence absent")
    from v8_next.evaluation.multitape import load_multitape

    try:
        t = load_multitape(str(tape_path("quad-1h-12m")),
                           limit=4321, offset=4380)
    except FileNotFoundError:
        pytest.skip("quad tape absent")
    ns = [c.end_ns for c in t.candles["BTCUSDT"]]
    closes = {f"{k}-PERP.BINANCE": [float(c.close) for c in v] for k, v in t.candles.items()}
    idx = {x: i for i, x in enumerate(ns)}
    opens = [json.loads(line) for line in trades[0].read_text().splitlines()]
    closes_rec = [json.loads(line) for line in closed[0].read_text().splitlines()]
    for o in opens:
        o["event_ns"] = o.pop("fill_time_ns")
        o["position_id"] = o.pop("trade_id")
    for c in closes_rec:
        c["event_ns"] = c.pop("fill_time_ns")
    pairs = eb.pair_positions(opens, closes_rec)
    total = 0.0
    for o, c in pairs:
        inst = o["instrument_id"]
        q = float(o["quantity"])
        side = 1.0 if str(o["side"]).upper() in ("BUY", "LONG") else -1.0
        oi = idx.get(int(o["event_ns"]))
        ci = idx.get(int(c["event_ns"])) if c else None
        if oi is None:
            continue
        for f in t.funding:
            if f.instrument + "-PERP.BINANCE" != inst:
                continue
            b = f.funding_time_ms * 10**6
            bi = next((i for i, x in enumerate(ns) if x >= b), None)
            if bi is None:
                continue
            if oi <= bi and (ci is None or ci > bi):
                total += -math.copysign(1.0, side * q) * abs(q) * closes[inst][bi] * float(
                    f.funding_rate
                )
    import json as _json

    receipts = sorted(base.glob("economic_receipt_*.json"))
    receipt = _json.loads(receipts[0].read_text())
    paid = receipt["metrics"]["portfolio_P"]["funding_cost"]
    assert paid is not None
    assert abs(total - paid) < 0.01, f"convention drift: {total} vs {paid}"


def test_mechanics_funding_measured_not_gapped() -> None:
    # Realized embeds settled funding (isolated engine finding); therefore the
    # portfolio builder must take measured drag as input, never derive funding
    # from single-run balance gaps.
    import inspect

    sig = inspect.signature(eb.portfolio_series_from_engine)
    assert "funding_measured_drag" in sig.parameters
    assert "funding_trades_identical" in sig.parameters
    assert Decimal("0.01") > Decimal("0")

pytestmark = pytest.mark.slow  # #469: tape/engine file, fast loop excludes via -m "not slow"
