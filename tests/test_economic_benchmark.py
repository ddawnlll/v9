"""Economic benchmark tests.

MECHANICS ONLY synthetic section: arithmetic/validator unit coverage with zero
evaluative weight. No test here asserts economic performance on synthetic data.

Evaluative section: real-tape BenchmarkCase via the economic fabric; skips when
the tape is absent. Synthetic candles are banned there.
"""

import inspect
import json
import math
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Sequence

import pytest

from v8_next.adapters.tape_paths import tape_path
from v8_next.evaluation import economic_benchmark as eb

# ---------------------------------------------------------------------------
# MECHANICS ONLY (synthetic fixtures, arithmetic/validator coverage)
# ---------------------------------------------------------------------------


def _synth_bars(n: int = 120, drift: float = 0.001, noise: float = 0.0, seed: int = 3) -> list[eb.BarView]:
    import numpy as np

    bars: list[eb.BarView] = []
    rng = np.random.default_rng(seed)
    px = 100.0
    shocks = rng.standard_normal(n) * noise
    for i in range(n):
        o = px
        c = px * (1.0 + drift + float(shocks[i]))
        bars.append(
            eb.BarView(
                end_ns=(i + 1) * 3_600_000_000_000,
                open=o,
                high=max(o, c) * 1.001,
                low=min(o, c) * 0.999,
                close=c,
            )
        )
        px = c
    return bars


def test_mechanics_cash_is_flat() -> None:
    bars = _synth_bars()
    fams = eb.compute_benchmark_family(bars, 10000.0, 0.0005)
    assert all(v == 10000.0 for v in fams["cash"]["equity"])
    assert fams["cash"]["turnover"] == 0.0


def test_mechanics_buy_hold_construction() -> None:
    bars = _synth_bars()
    fams = eb.compute_benchmark_family(bars, 10000.0, 0.0005)
    eq: list[float] = fams["btc_buy_hold"]["equity"]
    assert eq[0] == pytest.approx(10000.0 * (1.0 - 0.0005))
    assert eq[-1] / eq[0] == pytest.approx(bars[-1].close / bars[0].close)


def test_mechanics_vol_target_trailing_only() -> None:
    bars = _synth_bars(noise=0.004)
    fams = eb.compute_benchmark_family(bars, 10000.0, 0.0005)
    exp: list[float] = fams["vol_target"]["exposure"]
    assert exp[0] == 0.0  # no lookback yet: flat by construction
    assert any(e > 0 for e in exp[1:])


def test_mechanics_chronology_validator_catches_defects() -> None:
    bars = _synth_bars()
    ok, _ = eb.validate_chronology(bars)
    assert ok is True
    bad = list(bars)
    bad[5] = bars[4]  # duplicate timestamp
    ok2, note = eb.validate_chronology(bad)
    assert ok2 is False and "MONOTONIC" in note


def test_mechanics_future_leak_verdict_is_measured_from_the_series() -> None:
    """#437 R1/R4: the verdict discriminates between two series, not one argument.

    On HEAD this cannot pass: `detect_future_leak` accepted a caller-supplied
    `closes_shift` int and returned `False` for any nonzero value without reading
    `bars`, so "displaced series fails / the same series undisplaced passes" had
    no measurement behind it.
    """
    bars = _synth_bars()
    clean, clean_note = eb.detect_future_leak(bars)
    assert clean is True, clean_note
    assert eb.FUTURE_LEAK_PROBE in clean_note

    # The known defect: each bar carries the *next* bar's close, so bar i quotes a
    # price that did not exist until bar i+1 had closed.
    displaced = eb.displaced_closes_against_timestamps(bars)
    assert len(displaced) == len(bars) - 1
    leak, leak_note = eb.detect_future_leak(displaced)
    assert leak is False, "a close displaced against its own timestamp is a leak"
    assert "FUTURE_LEAK" in leak_note  # named reason, not a bare boolean

    # A close vector that repeats one value carries no per-bar price either.
    c0 = bars[0].close
    duplicated = [
        eb.BarView(b.end_ns, b.open, max(b.high, c0), min(b.low, c0), c0) for b in bars
    ]
    dup_ok, dup_note = eb.detect_future_leak(duplicated)
    assert dup_ok is False and "FUTURE_LEAK" in dup_note

    # The verdict is a function of the series alone: no flag is even accepted.
    assert list(inspect.signature(eb.detect_future_leak).parameters) == ["bars"]


def test_mechanics_future_leak_probe_can_fail_and_its_control_measures_it() -> None:
    """#437 R1/R2: the known-defect control publishes a measurement, not a constant.

    The control runs the probe against a genuinely displaced copy of the *same*
    series the numbers come from. `CAUGHT` therefore means the probe still
    discriminates, and the identical call on the undisplaced series is what shows
    the finding was not foregone.
    """
    bars = _synth_bars()
    control = eb.future_leak_positive_control(bars)
    assert control["status"] == "CAUGHT"
    assert control["probe"] == eb.FUTURE_LEAK_PROBE
    assert control["bars"] == len(bars) - 1
    assert "FUTURE_LEAK" in control["reason"]
    # Same series, undisplaced: the probe passes it. Same call, opposite verdict.
    assert eb.detect_future_leak(bars)[0] is True
    # A probe that never inspected a series cannot report OK.
    assert eb.detect_future_leak([bars[0]])[0] is False


def _mechanics_validity_verdicts(**overrides: Any) -> eb.EvidenceVerdicts:
    """Verdicts for a mechanics-only input set, one keyword change at a time."""
    bars = _synth_bars()
    kwargs: dict[str, Any] = {
        "chrono_ok": True,
        "chrono_note": "OK",
        "leak_probe": eb.detect_future_leak(bars),
        "excess": 0.05,
        "excess_ci": (0.01, 0.09),
        "stats": {
            "dsr": {"verdict": "COMPUTED"},
            "pbo": {"verdict": "COMPUTED"},
            "spa": {"verdict": "COMPUTED"},
        },
        "mix": {"incremental_net": 0.01},
        "cost_basis_ok": True,
        "funding_missing": False,
        "live_fills_present": False,
        "parity_ok": True,
    }
    kwargs.update(overrides)
    return eb.build_verdicts(**kwargs)


def _paired_duplicate_closes(n: int = 40) -> list[eb.BarView]:
    """MECHANICS ONLY: closes repeat in pairs, with ranges wide enough to contain
    either of two neighbouring closes. Shape only: zero evaluative weight."""
    closes = [100.0 + (i // 2) for i in range(n)]  # 100,100,101,101,102,102,...
    bars: list[eb.BarView] = []
    for i, c in enumerate(closes):
        o = closes[i - 1] if i else c
        bars.append(
            eb.BarView((i + 1) * 3_600_000_000_000, o, max(o, c) * 1.5, min(o, c) * 0.5, c)
        )
    return bars


def test_mechanics_future_leak_control_can_report_missed() -> None:
    """#437 R2: `known_defect_future_leak_caught` is a measurement in both directions.

    A control that can only ever answer CAUGHT is the tautology this card exists to
    remove. The series below is displaced invisibly: the neighbouring close stays
    inside every bar's own range and at least one boundary still carries a close
    into the next bar's open, so this probe does *not* catch that displacement and
    says so -- `MISSED`, named, rather than a pass dressed as a catch. The measured
    boundary of the probe is published instead of assumed away.
    """
    bars = _paired_duplicate_closes()
    assert eb.detect_future_leak(bars)[0] is True
    control = eb.future_leak_positive_control(bars)
    assert control["status"] == "MISSED"
    assert control["probe"] == eb.FUTURE_LEAK_PROBE
    assert control["bars"] == len(bars) - 1
    # #493: MISSED is measured on the displaced copy too, so it is named the same
    # way -- the block says which series the miss belongs to in both directions.
    assert control["bars_in"] == len(bars)
    assert control["bars_out"] == len(bars) - 1
    assert control["input"] == eb.FUTURE_LEAK_DISPLACED_INPUT
    assert len(control["input_sha256"]) == 64
    assert control["real_series"]["status"] == "OK"
    assert control["real_series"]["bars"] == len(bars)


def _assert_leak_block_is_named(block: dict[str, Any], where: str) -> None:
    """#493: a published known-defect verdict names the series it measured.

    Every `known_defect_future_leak_caught` block -- single series or
    per-instrument roll-up -- must carry its input identity (`input`,
    `input_rule`, `input_sha256`), its extent (`bars_in`/`bars_out`) and the
    real-series measurement beside it. Without these, `CAUGHT` publishes the
    displaced copy's violation counts as if they were the canonical tape's.
    """
    assert block["status"] in {"CAUGHT", "MISSED"}, where
    for key in ("input", "input_rule", "bars_in", "bars_out", "input_sha256", "real_series"):
        assert key in block, f"{where}: a {block['status']} block omits {key!r}"
    assert "displaced_closes_against_timestamps" in block["input"], where
    assert "close of bar i+1" in block["input_rule"], where
    assert re.fullmatch(r"[0-9a-f]{64}", str(block["input_sha256"])), where
    assert isinstance(block["bars_in"], int) and isinstance(block["bars_out"], int), where
    assert 0 <= block["bars_out"] <= block["bars_in"], where
    real = block["real_series"]
    for key in ("status", "bars", "reason", "series_sha256"):
        assert key in real, f"{where}: the real-series reading omits {key!r}"
    assert re.fullmatch(r"[0-9a-f]{64}", str(real["series_sha256"])), where
    # One block, two series: a block whose inputs share a digest measured one of
    # them twice, which is exactly the ambiguity #493 removes.
    assert block["input_sha256"] != real["series_sha256"], where


def test_mechanics_known_defect_control_names_the_series_it_measured() -> None:
    """#493 A1/A2: the CAUGHT verdict belongs to the *displaced* copy and says so.

    On HEAD the block published `status`, `probe`, `bars` (= displaced length)
    and a reason reading "N/M closes are not in their own bar", so a per-instrument
    finding looked like a live leak on the canonical tape while the same receipt's
    `research_validity` reported the identical probe VALID. The block must name the
    copy it measured and publish the real-series reading in the same block.
    """
    bars = _synth_bars()
    control = eb.future_leak_positive_control(bars)
    assert control["status"] == "CAUGHT"
    _assert_leak_block_is_named(control, "single-series control")
    assert control["input"] == eb.FUTURE_LEAK_DISPLACED_INPUT
    assert control["input_rule"] == eb.FUTURE_LEAK_DISPLACED_RULE
    assert control["bars_in"] == len(bars)
    assert control["bars_out"] == len(bars) - 1
    # The digest is the displaced copy's, so it can never be the tape's own.
    assert control["input_sha256"] == eb.series_sha256(
        eb.displaced_closes_against_timestamps(bars)
    )
    assert control["input_sha256"] != eb.series_sha256(bars)
    # Same block, second subject: the probe's reading of the series as it stands.
    real = control["real_series"]
    assert real["status"] == "OK"
    assert real["bars"] == len(bars)
    assert eb.FUTURE_LEAK_PROBE in real["reason"]
    assert {control["subject"], real["subject"]} == {"displaced_series", "canonical_series"}


def test_mechanics_leak_control_publishes_the_real_series_side_unchanged() -> None:
    """#493 A2: the real-series verdict is the real series' -- both directions.

    Same call, same block: a series whose own closes leak says so on the
    `real_series` subject, and the probe's undecidability stays undecidable
    instead of being republished as a leak (or as a catch) it never measured.
    """
    leaky = eb.displaced_closes_against_timestamps(_synth_bars())
    control = eb.future_leak_positive_control(leaky)
    assert control["real_series"]["status"] == "LEAK"
    assert "FUTURE_LEAK" in control["real_series"]["reason"]
    assert control["bars_in"] == len(leaky) and control["bars_out"] == len(leaky) - 1

    short = eb.future_leak_positive_control(_synth_bars()[:1])
    assert short["real_series"]["status"] == "UNDECIDABLE"
    assert short["bars_in"] == 1 and short["bars_out"] == 0


def test_mechanics_leak_control_blocks_are_named_in_both_producer_shapes() -> None:
    """#493 A3: neither producer shape may publish a verdict without its input.

    `app/economic.py` publishes one series' control; `app/portfolio.py` publishes
    the per-instrument roll-up. Both are checked through the module functions the
    producers call, so a roll-up that dropped the provenance (as HEAD's hand-built
    dict did) fails here.
    """
    caught = eb.future_leak_positive_control(_synth_bars(seed=3))
    missed = eb.future_leak_positive_control(_paired_duplicate_closes())
    assert (caught["status"], missed["status"]) == ("CAUGHT", "MISSED")

    rollup = eb.aggregate_future_leak_control({"AAAUSDT": caught, "BBBUSDT": caught})
    _assert_leak_block_is_named(rollup, "per-instrument roll-up")
    assert rollup["status"] == "CAUGHT"
    assert rollup["bars_in"] == caught["bars_in"] * 2
    assert rollup["bars_out"] == caught["bars_out"] * 2
    assert rollup["input_sha256"] == eb.series_set_sha256(
        {"AAAUSDT": caught["input_sha256"], "BBBUSDT": caught["input_sha256"]}
    )
    assert set(rollup["instruments"]) == {"AAAUSDT", "BBBUSDT"}
    assert rollup["real_series"]["status"] == "OK"
    assert rollup["real_series"]["bars"] == caught["real_series"]["bars"] * 2

    # `CAUGHT` keeps its #437 meaning: one missed instrument makes the run MISSED.
    mixed = eb.aggregate_future_leak_control({"AAAUSDT": caught, "BBBUSDT": missed})
    assert mixed["status"] == "MISSED"
    _assert_leak_block_is_named(mixed, "mixed roll-up")


def test_canonical_control_producers_publish_the_named_block() -> None:
    """#493 A3: both receipt producers publish the block the module names.

    Source-level guard: `app/portfolio.py` must roll its per-instrument controls up
    through `aggregate_future_leak_control` (the hand-built status dict is what
    published the copy's counts unnamed), and `app/economic.py` must publish
    `future_leak_positive_control`'s block verbatim rather than re-summarizing it.
    """
    project = Path(eb.project_root())
    app = project / "src/v8_next/app"
    portfolio = " ".join((app / "portfolio.py").read_text(encoding="utf-8").split())
    economic = " ".join((app / "economic.py").read_text(encoding="utf-8").split())
    assert '"known_defect_future_leak_caught": eb.aggregate_future_leak_control(' in portfolio
    assert "leak_control = eb.future_leak_positive_control(bars)" in economic
    assert '"known_defect_future_leak_caught": leak_control,' in economic


def test_mechanics_research_validity_requires_a_probe_that_ran() -> None:
    """#437 R3: `VALID` is a probe result, and an absent probe is not a pass.

    The canonical portfolio path published `chrono_ok=True,
    chrono_note="OK; leak_probe=OK"` as literals, so `research_validity` read
    `VALID` on a series no probe had inspected. Nothing may publish `VALID`
    without a probe result, and the probe's own identity has to be in the note.
    """
    measured = _mechanics_validity_verdicts()
    assert measured.research_validity == "VALID"
    assert f"leak_probe=OK; probe={eb.FUTURE_LEAK_PROBE}" in measured.research_note

    # The probe did not run: fail closed, and name the absence in the receipt.
    unrun = _mechanics_validity_verdicts(leak_probe=None)
    assert unrun.research_validity == "INVALID"
    assert "FUTURE_LEAK_PROBE_NOT_RUN" in unrun.research_note
    assert unrun.economic == "INCONCLUSIVE"
    assert "no economic inference" in unrun.economic_note

    # The probe ran and failed on the very series: same state, the probe's reason.
    displaced = eb.displaced_closes_against_timestamps(_synth_bars())
    failed = _mechanics_validity_verdicts(leak_probe=eb.detect_future_leak(displaced))
    assert failed.research_validity == "INVALID"
    assert "FUTURE_LEAK" in failed.research_note


def test_canonical_paths_measure_validity_instead_of_hardcoding_it() -> None:
    """#437 R3: the literal `chrono_ok=True, chrono_note="OK; leak_probe=OK"` is barred.

    Source-level guard: both app paths must hand `build_verdicts` the measured
    probe result. A literal re-introduction in either path fails here rather than
    silently publishing a validity nobody inspected.
    """
    project = Path(eb.project_root())
    for name in ("portfolio.py", "economic.py"):
        text = (project / "src/v8_next/app" / name).read_text(encoding="utf-8")
        flat = " ".join(text.split())
        assert 'chrono_note="OK; leak_probe=OK"' not in flat, name
        assert "leak_probe=(" in flat, f"{name} does not hand a probe result to build_verdicts"
        assert "detect_future_leak(" in flat, f"{name} does not run the future-leak probe"


def test_mechanics_controls_behave() -> None:
    pos = eb.positive_control_known_effect()
    assert pos["scope"] == "SYNTHETIC_CONTROL"
    assert pos["detected"] is True and pos["expected"] is True
    neg = eb.negative_control_shuffled([0.01, -0.01] * 200, seed=11)
    assert neg["scope"] == "SYNTHETIC_CONTROL"
    assert neg["declares_winner"] is False


def test_mechanics_metrics_and_capacity() -> None:
    bars = _synth_bars()
    fams = eb.compute_benchmark_family(bars, 10000.0, 0.0005)
    eq: list[float] = fams["btc_buy_hold"]["equity"]
    m = eb.metrics_for_curve(eq, fams["btc_buy_hold"]["exposure"], 1.0, 5.0, None, "ANALYTIC_MODEL", eq, 1)
    assert m.net_return > 0 and m.max_drawdown <= 0.0
    rows = eb.capacity_table(0.05, 2.0, 10000.0, 0.0005)
    assert rows[0]["breakeven_extra_cost_bp"] == pytest.approx(0.05 * 10000.0 / 20000.0 * 1e4)
    assert "L2" not in " ".join(rows[0]["modeled"])
    assert "OHLCV-derived ADV" in rows[0]["data_resolution"]


def test_mechanics_degenerate_sharpe_flagged() -> None:
    flat = [10000.0] * 100
    flat[50] += 1.0  # single nonzero bar
    m = eb.metrics_for_curve(flat, [0.0] * 100, 0.0, 0.0, None, "ANALYTIC_MODEL", flat, 0)
    assert m.sharpe_degenerate is True
    noisy = list(10000.0 + float(i) % 7 - 3.0 for i in range(100))
    m2 = eb.metrics_for_curve(noisy, [1.0] * 100, 1.0, 5.0, None, "ANALYTIC_MODEL", noisy, 1)
    assert m2.sharpe_degenerate is False


def _mechanics_return_path(
    n: int = 500, drift: float = 0.0004, vol: float = 0.01, seed: int = 13
) -> list[float]:
    """MECHANICS ONLY: seeded per-bar returns with zero evaluative weight."""
    import numpy as np

    rng = np.random.default_rng(seed)
    return [float(v) for v in drift + vol * rng.standard_normal(n)]


def _mechanics_equity(rets: list[float], start: float = 10000.0) -> list[float]:
    """MECHANICS ONLY: compound a synthetic return path into an equity curve."""
    eq = [start]
    for r in rets:
        eq.append(eq[-1] * (1.0 + r))
    return eq


def test_mechanics_sharpe_ci_stored_annualized() -> None:
    """#388 acceptance (1): the STORED sharpe_ci_* are annualized.

    The block bootstrap resamples per-bar returns, so its percentiles are
    per-bar Sharpe values. Stored beside the annualized point estimate they must
    first be scaled by sqrt(HOURS_PER_YEAR); this ratio is 1.0 pre-fix.
    """
    eq = _mechanics_equity(_mechanics_return_path())
    m = eb.metrics_for_curve(eq, [1.0] * len(eq), 1.0, 5.0, None, "ANALYTIC_MODEL", eq, 1)
    # Exactly the array metrics_for_curve derived from `eq`: same bootstrap input.
    per_bar_lo, per_bar_hi = eb.block_bootstrap_ci(eb.per_bar_returns(eq))
    assert per_bar_lo is not None and per_bar_hi is not None
    root = math.sqrt(eb.HOURS_PER_YEAR)
    assert eb.SHARPE_ANNUALIZATION == pytest.approx(root, rel=1e-15)
    assert m.sharpe_ci_low is not None and m.sharpe_ci_high is not None
    assert m.sharpe_ci_low == pytest.approx(per_bar_lo * root, rel=1e-12)
    assert m.sharpe_ci_high == pytest.approx(per_bar_hi * root, rel=1e-12)
    assert m.sharpe_ci_low / per_bar_lo == pytest.approx(root, rel=1e-9)
    assert m.sharpe_ci_high / per_bar_hi == pytest.approx(root, rel=1e-9)
    assert m.sharpe_ci_low == pytest.approx(
        eb.sharpe_ci_annualized(eb.per_bar_returns(eq))[0], rel=1e-12
    )


def test_mechanics_sharpe_ci_brackets_its_own_point() -> None:
    """#388 acceptance (2): ci_low <= sharpe_annualized <= ci_high on sound rows."""
    eq = _mechanics_equity(_mechanics_return_path())
    m = eb.metrics_for_curve(eq, [1.0] * len(eq), 1.0, 5.0, None, "ANALYTIC_MODEL", eq, 1)
    assert m.sharpe_degenerate is False
    assert m.sharpe_ci_low is not None and m.sharpe_ci_high is not None
    assert m.sharpe_ci_low <= m.sharpe_annualized <= m.sharpe_ci_high
    # Discriminator: the per-bar interval (the pre-fix publication) cannot
    # contain an annualized point of this magnitude, so this pins the fix.
    per_bar_lo, per_bar_hi = eb.block_bootstrap_ci(eb.per_bar_returns(eq))
    assert per_bar_lo is not None and per_bar_hi is not None
    assert per_bar_hi < m.sharpe_annualized
    assert not (per_bar_lo <= m.sharpe_annualized <= per_bar_hi)


def _mechanics_oos_metric(*, degenerate: bool, sharpe_annualized: float) -> eb.MetricSet:
    """Render-path fixture: a metric row with an explicit degeneracy flag.

    MECHANICS ONLY. No evaluative weight: these are not measured returns.
    """
    return eb.MetricSet(
        net_return=0.0047,
        excess_vs_primary=-0.001,
        sharpe_per_bar=sharpe_annualized / (365.0 * 24.0) ** 0.5,
        sharpe_annualized=sharpe_annualized,
        sharpe_ci_low=None,
        sharpe_ci_high=None,
        sharpe_degenerate=degenerate,
        max_drawdown=-0.01,
        tail_mean_5pct=-0.001,
        avg_exposure=1.0,
        # #472: a measured four-leg share, not the pre-fix constant `1.0` the
        # producer published for every curve regardless of its structure.
        concentration=0.25,
        concentration_legs={"A": 0.25, "B": 0.25, "C": 0.25, "D": 0.25},
        concentration_basis=eb.CONCENTRATION_MEASURED,
        turnover_notional_over_capital=0.5,
        commission_cost=1.0,
        funding_cost=None,
        cost_basis="ANALYTIC_MODEL+OOS_SLICE",
        n_bars=150,
        n_trades=1,
    )


# --------------------------------------------------------------------------- #
# #472 — `concentration` is MEASURED from the curve's own leg weights
# --------------------------------------------------------------------------- #


def _mechanics_four_leg_closes(n: int = 120) -> dict[str, list[float]]:
    """MECHANICS ONLY: four seeded close paths. No market data, no claim.

    Zero evaluative weight: these exist so the producer builds a real four-leg
    basket whose declared leg weights can be checked against the numbers it
    publishes beside them.
    """
    import numpy as np

    rng = np.random.default_rng(472)
    out: dict[str, list[float]] = {}
    for sym in ("LEG1", "LEG2", "LEG3", "LEG4"):
        level = 100.0
        path = [level]
        for r in rng.standard_normal(n - 1):
            level *= 1.0 + 0.002 * float(r)
            path.append(level)
        out[sym] = path
    return out


def _mechanics_curve_metric(curve: dict[str, object]) -> eb.MetricSet:
    """The published metric of one produced curve, through the real builder."""
    return eb.metrics_for_curve(
        [float(v) for v in curve["equity"]],  # type: ignore[union-attr]
        [float(v) for v in curve["exposure"]],  # type: ignore[union-attr]
        float(curve["turnover"]),  # type: ignore[arg-type]
        float(curve["commission"]),  # type: ignore[arg-type]
        None,
        "ANALYTIC_MODEL",
        None,
        int(curve["n_trades"]),  # type: ignore[arg-type]
        leg_weights=curve.get("leg_weights"),  # type: ignore[arg-type]
    )


def test_mechanics_concentration_is_measured_not_a_constant() -> None:
    """#472 (1/2): structurally different curves cannot share a concentration.

    MECHANICS ONLY: seeded closes through the real producer and the real metric
    builder, no tape, no claim. The pre-fix producer published the literal `1.0`
    for a zero-exposure `cash` curve and for a four-leg basket alike, so the
    declared G2 diagnostic carried no measurement at all.
    """
    fams = eb.compute_multileg_family(_mechanics_four_leg_closes())
    cash, equal_weight = fams["cash"], fams["equal_weight"]
    assert len(equal_weight["leg_weights"]) == 4
    assert cash["leg_weights"] == {}

    m_cash = _mechanics_curve_metric(cash)
    m_ew = _mechanics_curve_metric(equal_weight)

    # Nothing was held, so nothing is measured -- named, not zero-filled.
    assert m_cash.concentration is None
    assert eb.concentration_basis_state(m_cash.concentration_basis) == (
        eb.CONCENTRATION_NOT_MEASURED_NO_LEG_WEIGHTS
    )
    assert m_cash.concentration_legs == {}
    # A four-leg equal-weight basket measures its own 1/4, and says how.
    assert m_ew.concentration == pytest.approx(0.25, rel=0, abs=0)
    assert m_ew.concentration_basis == eb.CONCENTRATION_MEASURED
    assert set(m_ew.concentration_legs) == {"LEG1", "LEG2", "LEG3", "LEG4"}
    assert all(w == pytest.approx(0.25, rel=0, abs=0) for w in m_ew.concentration_legs.values())
    # The number is a function of the published leg weights, not of a rule.
    legs = m_ew.concentration_legs
    assert m_ew.concentration == pytest.approx(
        max(legs.values()) / sum(legs.values()), rel=0, abs=0
    )
    # The discriminator the pre-fix producer could not satisfy.
    assert m_cash.concentration != m_ew.concentration

    # `1.0` stays reachable, but only by a curve that really holds one leg.
    m_single = _mechanics_curve_metric(fams["bh_LEG1"])
    assert m_single.concentration == pytest.approx(1.0, rel=0, abs=0)
    assert m_single.concentration_basis == eb.CONCENTRATION_MEASURED


def test_mechanics_concentration_fails_loud_on_foreign_weights() -> None:
    """#472: leg weights that do not add up to the exposure are another run's.

    MECHANICS ONLY. A mismatch must raise instead of being silently normalized
    into this curve's number, which is how a constant could re-enter unnoticed.
    """
    eq = _mechanics_equity(_mechanics_return_path())
    exposure = [1.0] * len(eq)
    with pytest.raises(ValueError, match=eb.LEG_WEIGHTS_DO_NOT_SUM_TO_EXPOSURE):
        eb.concentration_from_leg_weights({"A": [0.5] * len(eq)}, exposure)
    with pytest.raises(ValueError, match=eb.LEG_WEIGHTS_DO_NOT_SUM_TO_EXPOSURE):
        # Right sum, wrong number of bars: a slice of another window is not this
        # curve's weights.
        eb.metrics_for_curve(
            eq, exposure, 1.0, 5.0, None, "ANALYTIC_MODEL", eq, 1,
            leg_weights={"A": [1.0] * (len(eq) - 1)},
        )


def _mechanics_receipt(
    metrics: dict[str, eb.MetricSet],
    oos_metrics: dict[str, eb.MetricSet] | None = None,
) -> eb.EconomicReceipt:
    """Minimal render-only receipt (MECHANICS ONLY: no tape, no evaluative claim)."""
    osm = metrics if oos_metrics is None else oos_metrics
    run = eb.RunIdentity(
        dataset=eb.DatasetIdentity(
            tape_path="MECHANICS_ONLY",
            tape_sha256="0" * 64,
            universe=("MECHANICS_ONLY",),
            period_start_ns=0,
            period_end_ns=3_600_000_000_000,
            n_bars=500,
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
    return eb.EconomicReceipt(
        receipt_id="mechanics-only",
        run=run,
        metrics=metrics,
        oos_metrics=osm,
        verdicts=verdicts,
        statistics={},
        controls={},
        portfolio_mix={},
        capacity_scenarios=[],
        parity={},
        shadow_live={},
        limitations=[],
    )


def _render_section(report: str, heading: str, next_heading: str) -> str:
    return report.split(heading, 1)[1].split(next_heading, 1)[0]


def test_mechanics_oos_section_flags_degenerate_variance() -> None:
    """#387: an explosive OOS Sharpe must never print unflagged.

    The chronological OOS block must carry the same DEGENERATE-VARIANCE marker
    the in-sample table already emits for the same `sharpe_degenerate` flag.
    """
    token = "DEGENERATE-VARIANCE"
    degenerate = _mechanics_oos_metric(
        degenerate=True, sharpe_annualized=68940.776  # observed in the #387 receipt
    )
    sound = _mechanics_oos_metric(degenerate=False, sharpe_annualized=1.443)
    report = eb.render_report(_mechanics_receipt({"challenger": degenerate, "incumbent": sound}))

    table = _render_section(report, "## Metrics (cost-adjusted, shared basis)", "## Chronological OOS")
    table_rows = {ln.split(" |", 1)[0][2:]: ln for ln in table.splitlines() if ln.startswith("| ")}
    assert token in table_rows["challenger"]  # established in-sample behaviour
    assert token not in table_rows["incumbent"]

    oos = _render_section(report, "## Chronological OOS", "## Statistics")
    oos_rows = {ln.split(":", 1)[0][2:]: ln for ln in oos.splitlines() if ln.startswith("- ")}
    assert "68940.776" in oos_rows["challenger"]
    assert token in oos_rows["challenger"]
    assert token not in oos_rows["incumbent"]
    assert eb.DEGENERATE_VARIANCE_MARKER == token  # one render token, both sections


def test_mechanics_report_publishes_aligned_sharpe_ci() -> None:
    """#388 acceptance (3): both Sharpe cells publish the CI on the point scale.

    In-sample table and chronological OOS block must carry the same annualized
    interval, disclose the scale, and keep honest absence where no interval was
    computed. MECHANICS ONLY: the row is synthetic arithmetic, not performance.
    """
    eq = _mechanics_equity(_mechanics_return_path())
    m = eb.metrics_for_curve(eq, [1.0] * len(eq), 1.0, 5.0, None, "ANALYTIC_MODEL", eq, 1)
    assert m.sharpe_ci_low is not None and m.sharpe_ci_high is not None
    assert m.sharpe_ci_low <= m.sharpe_annualized <= m.sharpe_ci_high
    cell = eb.sharpe_ci_cell(m)
    assert cell == f"[{m.sharpe_ci_low:.3f},{m.sharpe_ci_high:.3f}]"
    report = eb.render_report(_mechanics_receipt({"incumbent": m}))

    table = _render_section(
        report, "## Metrics (cost-adjusted, shared basis)", "## Chronological OOS"
    )
    assert "annualized" in table  # the scale claim is on the page, not implied
    row = next(ln for ln in table.splitlines() if ln.startswith("| incumbent |"))
    assert f"{m.sharpe_annualized:.3f} {cell}" in row  # point and CI, one scale

    oos = _render_section(report, "## Chronological OOS", "## Statistics")
    oos_row = next(ln for ln in oos.splitlines() if ln.startswith("- incumbent:"))
    assert f"Sharpe_ann {m.sharpe_annualized:.3f} CI {cell}" in oos_row

    # A row whose CI was not computed publishes no interval: absence stays absent.
    absent = eb.render_report(
        _mechanics_receipt(
            {"challenger": _mechanics_oos_metric(degenerate=True, sharpe_annualized=1.443)}
        )
    )
    absent_oos = _render_section(absent, "## Chronological OOS", "## Statistics")
    assert "CI [" not in absent_oos and "n/a" not in absent_oos


def _mechanics_excess_metric(*, lo: float | None, hi: float | None) -> eb.MetricSet:
    """Render-path fixture: a row with an explicit excess-return interval.

    MECHANICS ONLY. No evaluative weight: these are not measured returns.
    """
    return _mechanics_oos_metric(degenerate=False, sharpe_annualized=1.443).model_copy(
        update={"excess_ci_low": lo, "excess_ci_high": hi}
    )


def _excess_cell(report: str, curve: str) -> str:
    """The `excess vs primary [CI]` cell of one metrics-table row."""
    table = _render_section(
        report, "## Metrics (cost-adjusted, shared basis)", "## Chronological OOS"
    )
    row = next(ln for ln in table.splitlines() if ln.startswith(f"| {curve} |"))
    return row.strip().strip("|").split("|")[3].strip()


def test_mechanics_report_publishes_excess_interval() -> None:
    """The #396 excess interval must be readable in the artifact that carries it.

    Every `MetricSet` in the receipt carries `excess_ci_low/high` on the scale of
    `excess_vs_primary`, but `render_report` printed only the point estimate, so
    two receipts whose windows differ materially in the interval rendered the
    same report. MECHANICS ONLY: synthetic arithmetic, no evaluative weight.
    """
    m = _mechanics_excess_metric(lo=-0.6326, hi=0.0236)
    assert m.excess_ci_low is not None and m.excess_ci_high is not None
    assert m.excess_vs_primary is not None
    assert m.excess_ci_low <= m.excess_vs_primary <= m.excess_ci_high
    cell = eb.excess_ci_cell(m)
    assert cell == f"[{m.excess_ci_low:.4f},{m.excess_ci_high:.4f}]"

    report = eb.render_report(_mechanics_receipt({"incumbent": m}))
    table = _render_section(
        report, "## Metrics (cost-adjusted, shared basis)", "## Chronological OOS"
    )
    assert "excess vs primary [CI]" in table  # the column names its own scale
    assert "never a Sharpe ratio" in table  # read off the page, not inferred
    # Point estimate and interval in one row, on the one scale.
    assert _excess_cell(report, "incumbent") == f"{m.excess_vs_primary:.4f} {cell}"

    # Discriminator: the interval is the ONLY difference between the two
    # receipts, and at HEAD the two rendered reports were byte-identical.
    other = _mechanics_excess_metric(lo=-0.1111, hi=0.2222)
    other_report = eb.render_report(_mechanics_receipt({"incumbent": other}))
    assert eb.excess_ci_cell(other) in other_report
    assert other_report != report
    assert eb.excess_ci_cell(other) not in report

    # An interval that could not be computed publishes no bracket: absence stays
    # absent, exactly as the Sharpe CI path does.
    absent = _mechanics_excess_metric(lo=None, hi=None)
    assert eb.excess_ci_cell(absent) == ""
    absent_cell = _excess_cell(
        eb.render_report(_mechanics_receipt({"challenger": absent})), "challenger"
    )
    assert absent_cell == f"{absent.excess_vs_primary:.4f}"
    assert "[" not in absent_cell


# ---------------------------------------------------------------------------
# #438 — the degeneracy marker is derived from the condition the report
# declares, so it cannot miss an explosive Sharpe CI and cannot depend on the
# window length the block happened to have.
# ---------------------------------------------------------------------------

#: The published Sharpe cells of the challenger's IN-SAMPLE row in the frozen
#: #388 evidence receipt (artifacts/econ-388-fixed/main-postmerge/
#: economic_receipt_cdec1dfe.json). `sharpe_degenerate` is stored false there
#: while the interval's own magnitude runs to 1.0e4x the point estimate: the
#: observed instance of the defect.
_OBSERVED_CHALLENGER_IS = (-2.299585492181923, -21.24267809093839, 23403.497444363755)
#: The same curve's 150-bar OOS block from the same receipt, which is marked.
_OBSERVED_CHALLENGER_OOS = (68940.77561873154, 54769.290143657956, 132330.4941927873)


def _mechanics_sharpe_row(
    *, sharpe_annualized: float, ci_low: float | None, ci_high: float | None,
    degenerate: bool = False,
) -> eb.MetricSet:
    """Render-path fixture: one row with explicit published Sharpe cells.

    MECHANICS ONLY. No evaluative weight: the cells are arithmetic, not measured
    performance.
    """
    return _mechanics_oos_metric(
        degenerate=degenerate, sharpe_annualized=sharpe_annualized
    ).model_copy(update={"sharpe_ci_low": ci_low, "sharpe_ci_high": ci_high})


def _is_row(report: str, curve: str) -> str:
    """The in-sample metrics-table row of one curve."""
    table = _render_section(
        report, "## Metrics (cost-adjusted, shared basis)", "## Chronological OOS"
    )
    return next(ln for ln in table.splitlines() if ln.startswith(f"| {curve} |"))


def _sharpe_column(row: str) -> str:
    """The `Sharpe_ann [CI]` cell of a metrics-table row."""
    return row.strip().strip("|").split("|")[4].strip()


def test_mechanics_oversized_sharpe_ci_cannot_publish_unmarked() -> None:
    """#438 (3): a row past the declared CI/point ratio never prints unmarked.

    The challenger's frozen in-sample row publishes `-2.300 [-21.243,23403.497]`:
    the interval's own magnitude is 1.0e4x the point estimate, so its
    percentiles are bootstrap artefacts rather than a range the estimate lives
    in. The report must mark that row and publish no interval for it, while a
    row just inside the declared threshold keeps publishing its interval.
    MECHANICS ONLY: rendering policy over published cells, zero evaluative
    weight.
    """
    ann, lo, hi = _OBSERVED_CHALLENGER_IS
    assert max(abs(lo), abs(hi)) / abs(ann) > eb.DEGENERATE_CI_RATIO  # declared
    assert eb.unreproducible_variance(ann, lo, hi) is True

    row = _mechanics_sharpe_row(sharpe_annualized=ann, ci_low=lo, ci_high=hi)
    assert row.sharpe_degenerate is False  # the stored flag the defect ships with
    cell = _sharpe_column(_is_row(eb.render_report(_mechanics_receipt({"challenger": row})), "challenger"))
    assert eb.DEGENERATE_VARIANCE_MARKER in cell
    assert "n/a" in cell
    # The explosive percentiles are not published as a range at all.
    assert f"{lo:.3f}" not in cell and f"{hi:.3f}" not in cell

    # The declared threshold is the boundary, and it is the one on the page.
    assert eb.DEGENERATE_CI_RATIO == pytest.approx(1000.0)
    under = _mechanics_sharpe_row(sharpe_annualized=1.0, ci_low=-1000.0, ci_high=5.0)
    over = _mechanics_sharpe_row(sharpe_annualized=1.0, ci_low=-1001.0, ci_high=5.0)
    assert eb.unreproducible_variance(1.0, -1000.0, 5.0) is False
    assert eb.unreproducible_variance(1.0, -1001.0, 5.0) is True
    under_cell = _sharpe_column(
        _is_row(eb.render_report(_mechanics_receipt({"incumbent": under})), "incumbent")
    )
    over_cell = _sharpe_column(
        _is_row(eb.render_report(_mechanics_receipt({"incumbent": over})), "incumbent")
    )
    assert under_cell == "1.000 [-1000.000,5.000]"
    assert eb.DEGENERATE_VARIANCE_MARKER not in under_cell
    assert over_cell == f"1.000 n/a {eb.DEGENERATE_VARIANCE_MARKER}"


def test_mechanics_degeneracy_verdict_is_window_length_independent() -> None:
    """#438 (4): one curve, two window lengths, one verdict.

    A curve whose variance rests on a single bar is uninterpretable at 500 bars
    and at 150 bars. The criterion reads the published cells, never the window
    length, so the same curve cannot be marked in one block and trusted in the
    other. MECHANICS ONLY: seeded arithmetic, zero evaluative weight.
    """
    for n in (500, 150):
        eq = [10000.0] * n
        eq[n // 3] += 1.0  # one informative bar, the other n-1 are flat
        m = eb.metrics_for_curve(eq, [0.0] * n, 0.0, 0.0, None, "ANALYTIC_MODEL", eq, 0, None)
        assert m.sharpe_degenerate is True
        assert m.sharpe_ci_low is not None and m.sharpe_ci_high is not None
        # The arm that reads only the published cells -- no bar count, no window
        # length -- reaches the same verdict at both lengths.
        assert eb.unreproducible_variance(m.sharpe_annualized, m.sharpe_ci_low, m.sharpe_ci_high) is True
        assert max(abs(m.sharpe_ci_low), abs(m.sharpe_ci_high)) / abs(m.sharpe_annualized) > (
            eb.DEGENERATE_CI_RATIO
        )
        cell = _sharpe_column(
            _is_row(eb.render_report(_mechanics_receipt({"x": m})), "x")
        )
        assert eb.DEGENERATE_VARIANCE_MARKER in cell and "n/a" in cell

    # The same curve in both frozen blocks of the #388 receipt: marked (IS, 500
    # bars) and marked (OOS, 150 bars) by one criterion.
    for ann, lo_, hi_ in (_OBSERVED_CHALLENGER_IS, _OBSERVED_CHALLENGER_OOS):
        assert eb.unreproducible_variance(ann, lo_, hi_) is True


FROZEN_ECON_388_RECEIPT_REL = (
    "artifacts/econ-388-fixed/main-postmerge/economic_receipt_cdec1dfe.json"
)
FROZEN_ECON_388_REPORT_REL = (
    "artifacts/econ-388-fixed/main-postmerge/economic_report_cdec1dfe.md"
)
#: Rows acceptance pins a marker vector for: before and after, unmarked.
PINNED_UNMARKED = ("incumbent", "btc_buy_hold", "vol_target", "equal_weight")


def _frozen_evidence_path(rel: str) -> Path | None:
    """Frozen #388 evidence: this checkout, else the shared main checkout.

    Evidence artifacts live outside git (worktrees do not carry them), so a
    worktree falls back to the parent checkout. Absence skips: never synthesize.
    """
    root = Path(__file__).resolve().parents[2]
    for candidate in (root / rel, root.parent.parent / rel):
        if candidate.is_file():
            return candidate
    return None


def _frozen_metric_rows(path: Path, block: str) -> dict[str, eb.MetricSet]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return {name: eb.MetricSet.model_validate(row) for name, row in data[block].items()}


def test_economic_degeneracy_marker_recomputed_from_frozen_receipt() -> None:
    """#438 (1)+(2): the cdec1dfe receipt renders with its challenger row marked.

    The frozen receipt stores `sharpe_degenerate: false` for a challenger row
    whose published interval runs to 1.0e4x its point estimate. Regenerating the
    report from that receipt's own rows must mark the row and publish `n/a`
    instead of the interval, while every other row renders byte-for-byte as the
    frozen report has it -- the before/after marker vector is pinned, so the fix
    cannot be read as a marker that moves when the renderer changes. Real
    evidence, read-only: skips when the artifacts are absent.
    """
    receipt_path = _frozen_evidence_path(FROZEN_ECON_388_RECEIPT_REL)
    report_path = _frozen_evidence_path(FROZEN_ECON_388_REPORT_REL)
    if receipt_path is None or report_path is None:
        pytest.skip("frozen #388 economic evidence absent")
    assert receipt_path is not None and report_path is not None  # narrowed for the type checker

    metrics = _frozen_metric_rows(receipt_path, "metrics")
    oos = _frozen_metric_rows(receipt_path, "oos_metrics")
    frozen = report_path.read_text(encoding="utf-8")
    frozen_is = {
        ln.split(" |", 1)[0][2:]: ln
        for ln in _render_section(
            frozen, "## Metrics (cost-adjusted, shared basis)", "## Chronological OOS"
        ).splitlines()
        if ln.startswith("| ")
    }
    frozen_oos = {
        ln.split(":", 1)[0][2:]: ln
        for ln in _render_section(frozen, "## Chronological OOS", "## Statistics").splitlines()
        if ln.startswith("- ")
    }

    # (1) the shipped defect: an explosive in-sample interval, unmarked.
    assert metrics["challenger"].sharpe_degenerate is False
    assert eb.DEGENERATE_VARIANCE_MARKER not in frozen_is["challenger"]

    regenerated = eb.render_report(_mechanics_receipt(metrics, oos))
    is_rows = {
        ln.split(" |", 1)[0][2:]: ln
        for ln in _render_section(
            regenerated, "## Metrics (cost-adjusted, shared basis)", "## Chronological OOS"
        ).splitlines()
        if ln.startswith("| ")
    }
    oos_rows = {
        ln.split(":", 1)[0][2:]: ln
        for ln in _render_section(regenerated, "## Chronological OOS", "## Statistics").splitlines()
        if ln.startswith("- ")
    }
    challenger_cell = _sharpe_column(is_rows["challenger"])
    assert eb.DEGENERATE_VARIANCE_MARKER in challenger_cell
    assert "n/a" in challenger_cell
    assert "23403.497" not in challenger_cell  # the interval is not published

    # (2) the pin: every other row keeps the frozen report's marker vector and
    # its Sharpe cells. The IS table is compared cell-by-cell rather than line
    # by line: the excess-return interval column was added to the table by a
    # later render fix (#396/t_fcd334d4), so the frozen file predates it while
    # every Sharpe cell in it must survive untouched.
    for name in PINNED_UNMARKED:
        assert _sharpe_column(is_rows[name]) == _sharpe_column(frozen_is[name])
        assert oos_rows[name] == frozen_oos[name]
        assert eb.DEGENERATE_VARIANCE_MARKER not in is_rows[name]
        assert eb.DEGENERATE_VARIANCE_MARKER not in oos_rows[name]
    # cash and simple_trend come out of the fix exactly as the frozen report has
    # them: cash marked in both blocks (its variance is absent, not explosive),
    # simple_trend unmarked in the 500-bar table (36 informative bars) and
    # marked in the 150-bar OOS slice (5). The criterion is the same in both; the
    # blocks' variance support is not. A marked OOS row now publishes no
    # interval, where the frozen report printed one beside the marker.
    assert _sharpe_column(is_rows["cash"]) == _sharpe_column(frozen_is["cash"])
    assert oos_rows["cash"] == frozen_oos["cash"]
    assert _sharpe_column(is_rows["simple_trend"]) == _sharpe_column(frozen_is["simple_trend"])
    assert eb.DEGENERATE_VARIANCE_MARKER in frozen_oos["simple_trend"]
    assert oos_rows["simple_trend"] == (
        frozen_oos["simple_trend"].replace(" CI [-15.463,-4.699]", "")
    )

    # (4) the same curve, both frozen blocks: one verdict from one criterion.
    assert metrics["challenger"].sharpe_degenerate is False  # IS was trusted
    assert oos["challenger"].sharpe_degenerate is True  # OOS was not
    assert eb.degenerate_row(metrics["challenger"]) is True
    assert eb.degenerate_row(oos["challenger"]) is True
    # The OOS line for the same curve suppresses its interval the same way.
    assert eb.DEGENERATE_VARIANCE_MARKER in oos_rows["challenger"]
    assert "CI [" not in oos_rows["challenger"]
    assert regenerated.rstrip().endswith("claim_status: NO_ECONOMIC_CLAIM")


def test_mechanics_allocator_mix_shape() -> None:
    import numpy as np

    rng = np.random.default_rng(9)
    inc = list(0.001 + 0.002 * rng.standard_normal(200))
    ch = [0.0] * 200
    mix = eb.allocator_mix(inc, ch)
    assert mix["scope"] == "ALLOCATOR_LEVEL_NOT_ENGINE_JOINT"
    assert mix["incremental_net"] < mix["p_net"]  # dead challenger sleeve drags


# ---------------------------------------------------------------------------
# Error-path separation: bad data must never mint an economic verdict,
# and no statistical state may ever flip capital.
# ---------------------------------------------------------------------------


def test_mechanics_invalid_data_yields_no_economic_verdict() -> None:
    stats = {
        "dsr": {"verdict": "SUPPORTED_DESCRIPTIVE"},
        "pbo": {"verdict": "SUPPORTED_DESCRIPTIVE"},
        "spa": {"verdict": "SUPPORTED_DESCRIPTIVE"},
    }
    v = eb.build_verdicts(
        chrono_ok=False,
        chrono_note="NON_MONOTONIC_TIME_7",
        leak_probe=None,  # no probe ran; chronology already failed closed
        excess=0.05,  # positive excess must NOT leak through invalid data
        excess_ci=(0.01, 0.09),
        stats=stats,
        mix={"incremental_net": 0.01},
        cost_basis_ok=True,
        funding_missing=False,
        live_fills_present=True,
        parity_ok=True,
    )
    assert v.research_validity == "INVALID"
    assert v.economic == "INCONCLUSIVE"
    assert v.capital == "NOT_AUTHORIZED"


def test_mechanics_failed_estimators_stay_unsuccessful() -> None:
    stats = {
        "dsr": {"verdict": "UNDERPOWERED", "reason": "boom"},
        "pbo": {"verdict": "UNDERPOWERED", "reason": "boom"},
        "spa": {"verdict": "UNSUPPORTED", "reason": "arch absent"},
    }
    v = eb.build_verdicts(
        chrono_ok=True,
        chrono_note="OK",
        leak_probe=eb.detect_future_leak(_synth_bars()),
        excess=0.05,
        excess_ci=(0.01, 0.09),
        stats=stats,
        mix={"incremental_net": 0.01},
        cost_basis_ok=True,
        funding_missing=True,
        live_fills_present=False,
        parity_ok=True,
    )
    assert v.statistical in ("UNDERPOWERED", "UNSUPPORTED")
    assert v.statistical != "SUPPORTED"
    assert v.economic == "POSITIVE_DESCRIPTIVE"  # descriptive only
    assert v.capital == "NOT_AUTHORIZED"
    assert "NO_ECONOMIC_CLAIM" == eb.EconomicReceipt.__pydantic_fields__["claim_status"].default


def _starved_stats() -> dict[str, Any]:
    """Legacy-shaped reasons `run_statistics` used to record without the extra.

    Kept as written (no `unavailable` list) on purpose: a receipt written before
    #440 records the missing dependency only in the fail-closed reason, and that
    reading must keep working.
    """
    return {
        "dsr": {"verdict": "UNDERPOWERED", "reason": "ModuleNotFoundError: No module named 'scipy'"},
        "pbo": {"verdict": "UNDERPOWERED", "reason": "ModuleNotFoundError: No module named 'scipy'"},
        "spa": {"verdict": "UNSUPPORTED", "reason": "ModuleNotFoundError: No module named 'arch'"},
    }


def test_mechanics_unprovisioned_estimators_are_named_not_implied() -> None:
    """A missing optional `research` dependency must not read as "ran, found nothing".

    `scipy` and `arch` are declared only in `[project.optional-dependencies].research`,
    so a fresh sync without the extra lands in a state whose published verdict
    used to be `UNDERPOWERED`/`UNSUPPORTED` — power vocabulary that reads as a
    *measured* statistical outcome. #440 splits that state out and names it.
    """
    # Dependency order, not alphabetical: the estimators import them in this order.
    assert eb.unprovisioned_estimators(_starved_stats()) == ("scipy", "arch")
    v = eb.build_verdicts(
        chrono_ok=True,
        chrono_note="OK",
        leak_probe=eb.detect_future_leak(_synth_bars()),
        excess=-0.02,
        excess_ci=(-0.63, -0.02),
        stats=_starved_stats(),
        mix={"incremental_net": 0.0},
        cost_basis_ok=True,
        funding_missing=False,
        live_fills_present=False,
        parity_ok=True,
    )
    # Fail closed, and named: the verdict says what was never measured and is
    # never the power word that would read as a measurement.
    assert v.statistical == "ESTIMATOR_UNAVAILABLE: scipy,arch"
    assert v.statistical != "UNDERPOWERED" and v.statistical != "UNSUPPORTED"
    assert eb.statistical_verdict_state(v.statistical) == "ESTIMATOR_UNAVAILABLE"
    assert "scipy" in v.statistical and "arch" in v.statistical
    assert v.capital == "NOT_AUTHORIZED"
    assert "no estimator output" in v.statistical_note
    assert "scipy" in v.statistical_note and "arch" in v.statistical_note
    assert "extra research" in v.statistical_note
    assert "UNDERPOWERED" not in v.statistical_note
    assert "UNSUPPORTED" not in v.statistical_note
    # An estimator that ran and was merely underpowered is NOT a provisioning failure.
    unpowered = {
        "dsr": {"verdict": "UNDERPOWERED", "reason": "interval count 20 is too few"},
        "pbo": {"verdict": "UNDERPOWERED", "reason": "interval count 20 is too few"},
        "spa": {"verdict": "UNSUPPORTED", "reason": "no variant produced a p-value"},
    }
    assert eb.unprovisioned_estimators(unpowered) == ()
    v2 = eb.build_verdicts(
        chrono_ok=True,
        chrono_note="OK",
        leak_probe=eb.detect_future_leak(_synth_bars()),
        excess=-0.02,
        excess_ci=(-0.63, -0.02),
        stats=unpowered,
        mix={"incremental_net": 0.0},
        cost_basis_ok=True,
        funding_missing=False,
        live_fills_present=False,
        parity_ok=True,
    )
    assert v2.statistical == "UNSUPPORTED"
    assert v2.statistical_note == (
        "no estimator output: DSR=UNDERPOWERED PBO=UNDERPOWERED SPA=UNSUPPORTED"
    )


def _mechanics_family(n: int = 500) -> tuple[dict[str, Sequence[float]], list[int]]:
    """MECHANICS ONLY equity family: real *shape*, zero evaluative weight.

    Three positive curves on one chronology, long enough to tile 20 intervals
    (INTERVAL_BARS x 20) so the CSCV partition count is admissible. No market
    data, no performance claim: this fixture exists to exercise which state the
    statistics block publishes, not to measure anything.
    """
    import numpy as np

    rng = np.random.default_rng(440)
    curves: dict[str, Sequence[float]] = {"cash": [1.0] * n}
    for name, vol, drift in (("incumbent", 0.004, 0.0002), ("challenger", 0.006, 0.0003)):
        steps = drift + vol * rng.standard_normal(n)
        curves[name] = [float(v) for v in np.cumprod(1.0 + steps)]
    end_ns = [(i + 1) * 3_600_000_000_000 for i in range(n)]
    return curves, end_ns


def _mechanics_family_verdicts(stats: dict[str, Any]) -> eb.EvidenceVerdicts:
    """The verdicts one deterministic input set produces for a given statistics block."""
    return eb.build_verdicts(
        chrono_ok=True,
        chrono_note="MECHANICS_ONLY",
        leak_probe=eb.detect_future_leak(_synth_bars()),
        excess=-0.02,
        excess_ci=(-0.63, -0.02),
        stats=stats,
        mix={"incremental_net": 0.0},
        cost_basis_ok=True,
        funding_missing=False,
        live_fills_present=False,
        parity_ok=True,
    )


def test_mechanics_estimator_unavailable_is_not_a_power_verdict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#440 G-A/G-B: the same inputs, one variable changed — availability.

    Blocking `scipy`/`arch` (the `research` extras) must leave every field of the
    receipt *outside* the statistics block and the statistical verdict it
    determines untouched, must publish `ESTIMATOR_UNAVAILABLE` for every
    estimator that never ran, must not publish the power vocabulary for them, and
    must name the unimportable dependencies in the published verdict string
    itself — not only in the note.
    """
    from v8_next.evaluation.statistics_plan import g5_plan

    curves, end_ns = _mechanics_family()
    plan = g5_plan(family="mechanics-440", pinned_ns=1, block_size=5, reps=200, seed=7)

    monkeypatch.setattr(eb, "module_available", lambda name: True)
    available = eb.run_statistics(curves, end_ns, "cash", plan=plan)
    receipt_ok = _mechanics_receipt({}).model_copy(
        update={"statistics": available, "verdicts": _mechanics_family_verdicts(available)}
    )

    monkeypatch.setattr(eb, "module_available", lambda name: name not in ("scipy", "arch"))
    blocked = eb.run_statistics(curves, end_ns, "cash", plan=plan)
    receipt_blocked = _mechanics_receipt({}).model_copy(
        update={"statistics": blocked, "verdicts": _mechanics_family_verdicts(blocked)}
    )

    # (1) every statistic carries a state from the declared set
    for stats in (available, blocked):
        for key in ("dsr", "pbo", "spa"):
            assert stats[key]["verdict"] in eb.STATISTIC_STATES, (key, stats[key])

    # (2) blocked: the state says "never ran", and the power vocabulary is absent
    for key in ("dsr", "pbo", "spa"):
        assert blocked[key]["verdict"] == "ESTIMATOR_UNAVAILABLE", blocked[key]
        assert blocked[key]["verdict"] not in ("UNDERPOWERED", "UNSUPPORTED")
        assert blocked[key]["reason"]
    assert sorted(blocked["dsr"]["unavailable"]) == ["scipy"]
    assert sorted(blocked["pbo"]["unavailable"]) == ["scipy"]
    assert sorted(blocked["spa"]["unavailable"]) == ["arch", "scipy"]
    assert not any(
        blocked[key]["verdict"] in ("UNDERPOWERED", "UNSUPPORTED") for key in ("dsr", "pbo", "spa")
    )

    # (3) the same inputs measured: none of them reads as unmeasured
    for key in ("dsr", "pbo", "spa"):
        assert available[key]["verdict"] != "ESTIMATOR_UNAVAILABLE", available[key]

    # (4) G-A: identical in every field outside the statistics block, the
    # statistical verdict the block determines, and the availability record that
    # states the one changed variable.
    shared_ok = {k: v for k, v in available.items() if k not in ("dsr", "pbo", "spa")}
    shared_blocked = {k: v for k, v in blocked.items() if k not in ("dsr", "pbo", "spa")}
    assert shared_ok == shared_blocked
    dumped_ok, dumped_blocked = receipt_ok.model_dump(), receipt_blocked.model_dump()
    assert {k for k in dumped_ok if dumped_ok[k] != dumped_blocked[k]} == {
        "statistics",
        "verdicts",
        "estimator_availability",
    }
    assert {
        k for k in dumped_ok["verdicts"] if dumped_ok["verdicts"][k] != dumped_blocked["verdicts"][k]
    } == {"statistical", "statistical_note"}
    for domain in ("research_validity", "economic", "portfolio", "execution", "capital"):
        assert dumped_ok["verdicts"][domain] == dumped_blocked["verdicts"][domain]
    assert dumped_blocked["claim_status"] == "NO_ECONOMIC_CLAIM"

    # (5) G-B: the published verdict string itself names what is not importable
    verdict = receipt_blocked.verdicts.statistical
    assert verdict == "ESTIMATOR_UNAVAILABLE: scipy,arch"
    assert verdict not in ("UNDERPOWERED", "UNSUPPORTED")
    assert "scipy" in verdict and "arch" in verdict
    assert eb.statistical_verdict_state(verdict) in eb.VERDICT_STATES
    assert "UNDERPOWERED" not in receipt_blocked.verdicts.statistical_note
    assert "UNSUPPORTED" not in receipt_blocked.verdicts.statistical_note

    # (6) R3: the receipt separates "not measured" from "measured" without parsing
    # prose — the availability record and the closed state token both say it.
    assert receipt_blocked.estimator_availability.unavailable == ("arch", "scipy")
    assert receipt_blocked.estimator_availability.checked["polars"] is True
    assert receipt_blocked.estimator_availability.probe == eb.AVAILABILITY_PROBE
    assert receipt_ok.estimator_availability.unavailable == ()
    assert receipt_ok.estimator_availability.checked == {
        "arch": True,
        "polars": True,
        "scipy": True,
    }
    report = eb.render_report(receipt_blocked)
    assert "| statistical | ESTIMATOR_UNAVAILABLE: scipy,arch |" in report
    assert "not importable by this interpreter: arch, scipy" in report
    # A fully provisioned receipt renders exactly as it did before #440.
    assert "Estimator availability" not in eb.render_report(receipt_ok)


def test_canonical_reproduction_commands_provision_the_research_extra() -> None:
    """The documented canonical command must carry the extra its statistics need.

    `scipy`/`arch` are optional, so a documented `uv run --project v8-next python
    -m ...` without `--extra research` silently degrades `statistical` on a fresh
    sync. The documented canonical commands are the reproduction contract; they
    must not be under-provisioned.
    """
    project = Path(eb.project_root())
    bare = re.compile(r"uv run --project v8-next python -m v8_next\.app\.(cli|economic)\b")
    for path in (
        project / "src/v8_next/app/portfolio.py",
        project / "src/v8_next/app/economic.py",
        project.parent / "docs/contracts/ECONOMIC_BENCHMARK_COVERAGE.md",
    ):
        text = " ".join(path.read_text(encoding="utf-8").split())
        assert not bare.search(text), (
            f"{path}: documented canonical command omits `--extra research`, so a "
            "fresh sync degrades `statistical` to a fail-closed UNSUPPORTED"
        )


def test_mechanics_rules_fire_only_on_registrations() -> None:
    base = {
        "chrono_ok": True, "chrono_note": "OK",
        "leak_probe": eb.detect_future_leak(_synth_bars()), "excess": -0.02,
        "mix": {"incremental_net": 0.0}, "cost_basis_ok": True,
        "funding_missing": False, "live_fills_present": False, "parity_ok": True,
    }
    computed = {
        "dsr": {"verdict": "COMPUTED", "dsr_confidence": 0.5,
                "selected_sharpe_nonannualized": -0.1, "selected_variant": "x"},
        "pbo": {"verdict": "COMPUTED"},
        "spa": {"verdict": "COMPUTED", "pvalues": {"consistent": 0.4}},
        "variant_excess_vs_baseline": -0.01,
    }
    v = eb.build_verdicts(**base, excess_ci=(-0.05, 0.03), stats=dict(computed))
    assert v.statistical == "INCONCLUSIVE"
    v = eb.build_verdicts(**base, excess_ci=(-0.05, -0.01), stats=dict(computed))
    assert v.statistical == "SUPPORTS_UNDERPERFORMANCE"
    edge_spa = dict(computed)
    edge_spa["variant_excess_vs_baseline"] = 0.02
    edge_spa["spa"] = {"verdict": "COMPUTED", "pvalues": {"consistent": 0.01}}
    v = eb.build_verdicts(**base, excess_ci=(-0.05, 0.03), stats=edge_spa)
    assert v.statistical == "SUPPORTS_EDGE"
    edge_dsr = dict(computed)
    edge_dsr["dsr"] = {"verdict": "COMPUTED", "dsr_confidence": 0.97,
                       "selected_sharpe_nonannualized": 0.4, "selected_variant": "y"}
    v = eb.build_verdicts(**base, excess_ci=(-0.05, 0.03), stats=edge_dsr)
    assert v.statistical == "SUPPORTS_EDGE"


# ---------------------------------------------------------------------------
# Evaluative: real tape only
# ---------------------------------------------------------------------------


def test_economic_benchmark_real_tape_end_to_end(tmp_path: Path) -> None:
    from v8_next.app import economic as econ_mod
    from v8_next.evaluation.gate_resolution import DEFAULT_TAPE_PATH

    if not DEFAULT_TAPE_PATH.exists():
        pytest.skip(f"Real tape not found at {DEFAULT_TAPE_PATH}")
    rc = econ_mod.main(
        [
            "--tape-path",
            str(DEFAULT_TAPE_PATH),
            "--bars",
            "500",
            "--output-dir",
            str(tmp_path / "econ"),
            "--primary",
            "btc_buy_hold",
        ]
    )
    assert rc == 0
    import json

    receipts = sorted((tmp_path / "econ").glob("economic_receipt_*.json"))
    assert receipts, "tagged economic receipt missing"
    receipt = json.loads(receipts[0].read_text())
    assert receipt["claim_status"] == "NO_ECONOMIC_CLAIM"
    assert receipt["verdicts"]["capital"] == "NOT_AUTHORIZED"
    assert eb.statistical_verdict_state(receipt["verdicts"]["statistical"]) in (
        "SUPPORTS_EDGE",
        "SUPPORTS_UNDERPERFORMANCE",
        "INCONCLUSIVE",
        "UNDERPOWERED",
        "UNSUPPORTED",
        "ESTIMATOR_UNAVAILABLE",
    )
    # No invented universal PASS: statistical support never flips economic/capital.
    assert set(receipt["metrics"]) >= {"incumbent", "challenger", "btc_buy_hold", "cash"}
    assert list((tmp_path / "econ").glob("economic_report_*.md")), "tagged economic report missing"
    report = next(iter((tmp_path / "econ").glob("economic_report_*.md"))).read_text(encoding="utf-8")
    # #387: a frozen split that actually produced an OOS slice must be covered.
    if receipt["run"]["dataset"]["n_bars"] >= eb.OOS_FIT_BARS + 48:
        assert receipt["oos_metrics"], "OOS slice missing: flagged render path untested"
    # #387: every OOS row must carry the marker iff its MetricSet is flagged.
    for name, m in receipt["oos_metrics"].items():
        row = f"- {name}: "
        line = next((ln for ln in report.splitlines() if ln.startswith(row)), None)
        assert line is not None, f"OOS row missing for {name}"
        assert (eb.DEGENERATE_VARIANCE_MARKER in line) == bool(m["sharpe_degenerate"]), line
    assert list((tmp_path / "econ").glob("incumbent_trades_*.jsonl"))
    # Missing-data honesty: funding stays missing, live stays unrun.
    assert receipt["metrics"]["incumbent"]["funding_cost"] is None
    assert receipt["shadow_live"]["model_vs_real_fills"] == "UNRUN"


#: PYTHONPATH `sitecustomize` that makes the optional `research` extras
#: unimportable, so the "fresh sync without the extra" state can be reproduced
#: deterministically without touching any environment.
_STARVED_EXTRA_BLOCKER = '''"""Make the optional `research` extras (`scipy`, `arch`) unimportable."""
import importlib.abc
import sys

_BLOCKED = {"scipy", "arch"}


class _Blocker(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        root = fullname.split(".")[0]
        if root in _BLOCKED:
            raise ModuleNotFoundError(f"No module named '{root}'", name=root)
        return None


sys.meta_path.insert(0, _Blocker())
'''


def test_economic_portfolio_starved_estimators_name_the_missing_dependency(
    tmp_path: Path,
) -> None:
    """G-C (#440): the canonical window, one variable changed — availability.

    The canonical portfolio command with the optional `research` extras made
    unimportable must (a) stay fail-closed — `statistical` may never read as
    computation having happened, `claim_status` stays `NO_ECONOMIC_CLAIM` — and
    (b) publish the unmeasured state and name the dependencies on the operator
    surface, instead of either an ordinary "no estimator output" or the power
    vocabulary that reads as a measurement.
    """
    project = Path(eb.project_root())
    # A worktree checkout (`<repo>/.worktrees/<task>/v8-next`) does not carry the
    # untracked tape; an ancestor checkout does, so the gate runs instead of
    # skipping when this test is invoked from a worktree.
    candidates = [parent / "research/tape/quad-1h-12m" for parent in (project.parent, *project.parents)]
    tape = next((p for p in candidates if p.is_dir()), None)
    if tape is None:
        pytest.skip(f"Real tape not found at {candidates[0]}")
    blocker = tmp_path / "blocker"
    blocker.mkdir()
    (blocker / "sitecustomize.py").write_text(_STARVED_EXTRA_BLOCKER, encoding="utf-8")
    out_dir = tmp_path / "starved"
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join((str(blocker), str(project / "src")))
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "v8_next.app.cli",
            "benchmark-portfolio",
            "--tape-path",
            str(tape),
            "--bars",
            "500",
            "--output-dir",
            str(out_dir),
        ],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(project.parent),
        timeout=900,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    receipts = sorted(out_dir.glob("economic_receipt_*.json"))
    reports = sorted(out_dir.glob("economic_report_*.md"))
    assert receipts and reports, "starved run produced no receipt/report"
    receipt = json.loads(receipts[0].read_text(encoding="utf-8"))
    # Fail closed, and named: a missing dependency is not a computed statistical
    # result, and it is not a weak one either.
    verdict = receipt["verdicts"]["statistical"]
    assert verdict == "ESTIMATOR_UNAVAILABLE: scipy,arch"
    assert verdict not in ("UNDERPOWERED", "UNSUPPORTED")
    assert receipt["claim_status"] == "NO_ECONOMIC_CLAIM"
    assert receipt["verdicts"]["capital"] == "NOT_AUTHORIZED"
    for key in ("dsr", "pbo", "spa"):
        assert receipt["statistics"][key]["verdict"] == "ESTIMATOR_UNAVAILABLE"
        assert receipt["statistics"][key]["verdict"] not in ("UNDERPOWERED", "UNSUPPORTED")
        assert receipt["statistics"][key]["reason"]
    # R3: machine-readable, beside the versions record that says what is installed.
    # The two records disagree here on purpose -- `importlib.metadata` still knows
    # the wheel is installed while this interpreter cannot import it -- which is
    # exactly why the receipt carries both.
    assert receipt["estimator_availability"]["unavailable"] == ["arch", "scipy"]
    assert receipt["estimator_availability"]["probe"] == eb.AVAILABILITY_PROBE
    assert "scipy" in receipt["run"]["code"]["estimator_versions"]
    note = receipt["verdicts"]["statistical_note"]
    assert "no estimator output" in note
    assert "scipy" in note and "arch" in note
    assert "extra research" in note
    assert "UNDERPOWERED" not in note and "UNSUPPORTED" not in note
    # The operator surface must carry the same fact, and the report the note.
    assert f"[!] statistical={verdict}" in proc.stdout
    assert "scipy" in proc.stdout
    report_text = reports[0].read_text(encoding="utf-8")
    assert f"| statistical | {verdict} | {note} |" in report_text
    assert "not importable by this interpreter: arch, scipy" in report_text


# ---------------------------------------------------------------------------
# #397 — turnover and commission must be one fill set, in the declared fee
# convention (`taker_fee` on traded notional). Entry AND exit legs.
# ---------------------------------------------------------------------------


def _mechanics_engine_result() -> dict[str, Any]:
    """MECHANICS ONLY synthetic engine result: one campaign, entry + exit.

    Ledger arithmetic with zero evaluative weight: two fills at known prices and
    the two position records the engine emits for them. No market data, no
    performance claim.
    """
    end_ns = [(i + 1) * 3_600_000_000_000 for i in range(6)]
    return {
        "opened_positions": [
            {
                "position_id": "MECH-1",
                "instrument_id": "MECH-PERP.BINANCE",
                "side": "LONG",
                "quantity": "2",
                "avg_px_open": "100",
                "event_ns": end_ns[1],
            }
        ],
        "closed_positions": [
            {
                "position_id": "MECH-1",
                "instrument_id": "MECH-PERP.BINANCE",
                "realized_pnl": "4 USDT",
                "avg_px_close": "102",
                "event_ns": end_ns[3],
            }
        ],
        "account": {
            "balance_total": "10004 USDT",
            "orders": [
                {"status": "FILLED", "filled_qty": "2", "average_price": "100"},
                {"status": "FILLED", "filled_qty": "2", "average_price": "102"},
            ],
            "positions": [],
        },
        "_end_ns": end_ns,
    }


def test_mechanics_engine_turnover_counts_entry_and_exit_fills() -> None:
    """#397 (G1, mechanics): one campaign, both legs, one fee convention.

    A campaign that opens 2 @ 100 and closes 2 @ 102 trades 404 USDT of
    notional, so turnover must be 404/capital and the commission estimate
    404 * taker_fee. Pre-fix the close was carried as a zero-notional event:
    turnover stopped at 200/capital while the commission still charged both
    fills, publishing an implied fee ~2x the declared taker fee.
    """
    engine_result = _mechanics_engine_result()
    end_ns = engine_result.pop("_end_ns")
    capital, fee = 10000.0, 0.0005
    closes = [100.0, 101.0, 102.0, 103.0, 104.0, 105.0]

    ser = eb.portfolio_series_from_engine(
        engine_result, {"MECH-PERP.BINANCE": closes}, end_ns, capital, fee
    )

    assert ser["commission"] == pytest.approx(404.0 * fee, rel=1e-12)
    assert ser["turnover"] == pytest.approx(404.0 / capital, rel=1e-12)
    # The published pair prices at exactly the declared fee.
    assert ser["commission"] == pytest.approx(ser["turnover"] * capital * fee, rel=1e-12)

    notional = ser["cost_reconciliation"]["turnover_notional"]
    assert notional["entry_notional_usdt"] == pytest.approx(200.0, rel=1e-12)
    assert notional["exit_notional_usdt"] == pytest.approx(204.0, rel=1e-12)
    assert notional["total_notional_usdt"] == pytest.approx(404.0, rel=1e-12)
    assert notional["entry_fills"] == 1 and notional["exit_fills"] == 1
    assert notional["exit_price_source"] == {"engine_avg_px_close": 1, "bar_close_proxy": 0}
    assert notional["fills_notional_usdt"] == pytest.approx(404.0, rel=1e-12)
    assert notional["fills_notional_unattributed_usdt"] == pytest.approx(0.0, abs=1e-9)


def test_mechanics_engine_turnover_falls_back_to_bar_close_when_unpriced() -> None:
    """#397 (G1, mechanics): a missing engine close price is NOT a free exit.

    The bar-close fill proxy is used and disclosed, so the exit still carries
    turnover instead of silently dropping out of the denominator.
    """
    engine_result = _mechanics_engine_result()
    end_ns = engine_result.pop("_end_ns")
    engine_result["closed_positions"][0]["avg_px_close"] = ""
    capital, fee = 10000.0, 0.0005
    closes = [100.0, 101.0, 102.0, 103.0, 104.0, 105.0]

    ser = eb.portfolio_series_from_engine(
        engine_result, {"MECH-PERP.BINANCE": closes}, end_ns, capital, fee
    )

    # Exit at the bar-3 close (103), not at zero notional.
    assert ser["turnover"] == pytest.approx((2 * 100 + 2 * 103) / capital, rel=1e-12)
    notional = ser["cost_reconciliation"]["turnover_notional"]
    assert notional["exit_notional_usdt"] == pytest.approx(206.0, rel=1e-12)
    assert notional["exit_price_source"] == {"engine_avg_px_close": 0, "bar_close_proxy": 1}


def _quad_tape_dir() -> Path | None:
    """The quad tape from the repo root, else the canonical checkout, else None."""
    for candidate in (
        Path(__file__).resolve().parents[2] / "research" / "tape" / "quad-1h-12m",
        tape_path("quad-1h-12m"),
    ):
        if (candidate / "tape.jsonl").exists():
            return candidate
    return None


def _btc_tape_file() -> Path | None:
    """The single-instrument tape: env/honoured default, then the repo root."""
    from v8_next.evaluation.gate_resolution import DEFAULT_TAPE_PATH

    for candidate in (
        DEFAULT_TAPE_PATH,
        Path(__file__).resolve().parents[2] / "research" / "tape" / "btcusdt-1h-12m" / "tape.jsonl",
        tape_path("btcusdt-1h-12m", "tape.jsonl"),
    ):
        if candidate.exists():
            return candidate
    return None


def _persisted_fill_pairs(out_dir: Path) -> tuple[float, float, int]:
    """Re-derive entry/exit notional from the run's persisted fill records.

    Uses the same chronological per-slot pairing contract the series uses, so
    the aggregate is an independent recomputation, not a restatement.
    """
    opens_raw = [
        json.loads(line)
        for line in sorted(out_dir.glob("portfolio_P_trades_*.jsonl"))[0].read_text().splitlines()
        if line.strip()
    ]
    closed_raw = [
        json.loads(line)
        for line in sorted(out_dir.glob("portfolio_closed_*.jsonl"))[0].read_text().splitlines()
        if line.strip()
    ]
    opens = [
        {
            "position_id": o["trade_id"],
            "instrument_id": o["instrument_id"],
            "side": o["side"],
            "quantity": o["quantity"],
            "avg_px_open": o["avg_px_open"],
            "event_ns": o["fill_time_ns"],
        }
        for o in opens_raw
    ]
    closes = [
        {
            "position_id": c["position_id"],
            "instrument_id": c["instrument_id"],
            "avg_px_close": c["avg_px_close"],
            "event_ns": c["fill_time_ns"],
        }
        for c in closed_raw
    ]
    pairs = eb.pair_positions(opens, closes)
    entry = sum(abs(float(o["quantity"])) * float(o["avg_px_open"]) for o, _ in pairs)
    exit_ = sum(
        abs(float(o["quantity"])) * float(str(c["avg_px_close"]))
        for o, c in pairs
        if c is not None and c.get("avg_px_close")
    )
    return entry, exit_, len(closes)


def test_economic_portfolio_turnover_matches_commission_fill_set(tmp_path: Path) -> None:
    """#397 (G1/G2) on the canonical quad engine path. Real tape; skips if absent.

    Reproduces the canonical 385-bar quad window and checks the published pair:
    ``commission_cost == turnover_notional_over_capital * capital * taker_fee``
    within the 1% fill-price tolerance, turnover re-derived independently from
    the persisted entry/exit fills, and implied fee 5.0 bps for P and P+E.
    """
    tape = _quad_tape_dir()
    if tape is None:
        pytest.skip("quad tape absent")
    from v8_next.app import portfolio as port_mod

    out_dir = tmp_path / "port"
    rc = port_mod.main(
        [
            "--tape-path",
            str(tape),
            "--bars",
            "385",
            "--output-dir",
            str(out_dir),
            "--primary",
            "equal_weight",
        ]
    )
    assert rc == 0
    receipt = json.loads(
        sorted(out_dir.glob("economic_receipt_*.json"))[0].read_text(encoding="utf-8")
    )
    capital = receipt["run"]["capital"]
    fee = receipt["run"]["taker_fee"]

    for name in ("portfolio_P", "portfolio_PE"):
        m = receipt["metrics"][name]
        traded = m["turnover_notional_over_capital"] * capital
        assert traded > 0.0
        assert m["commission_cost"] == pytest.approx(traded * fee, rel=0.01)
        assert m["commission_cost"] / traded * 1e4 == pytest.approx(fee * 1e4, abs=0.1)

    entry, exit_, n_closes = _persisted_fill_pairs(out_dir)
    assert n_closes > 0, "window produced no exits: the check would be vacuous"
    p_traded = receipt["metrics"]["portfolio_P"]["turnover_notional_over_capital"] * capital
    assert p_traded == pytest.approx(entry + exit_, rel=0.01)
    # The defect this pins: entry-only notional priced the same commission at 2x.
    assert p_traded > entry
    assert receipt["claim_status"] == "NO_ECONOMIC_CLAIM"


def test_economic_incumbent_turnover_and_capacity_breakeven(tmp_path: Path) -> None:
    """#397 (G1/G3) on the engine-backed single-instrument path. Skips without tape.

    The incumbent/challenger rows are engine-backed: their published turnover
    must price at the declared fee, the capacity breakeven must be derived from
    that corrected notional, and no claim may be minted by the fix.
    """
    tape = _btc_tape_file()
    if tape is None:
        pytest.skip("Real tape not found")
    from v8_next.app import economic as econ_mod

    rc = econ_mod.main(
        [
            "--tape-path",
            str(tape),
            "--bars",
            "500",
            "--output-dir",
            str(tmp_path / "econ"),
            "--primary",
            "btc_buy_hold",
        ]
    )
    assert rc == 0
    receipt = json.loads(
        sorted((tmp_path / "econ").glob("economic_receipt_*.json"))[0].read_text(encoding="utf-8")
    )
    capital = receipt["run"]["capital"]
    fee = receipt["run"]["taker_fee"]

    for name in ("incumbent", "challenger"):
        m = receipt["metrics"][name]
        traded = m["turnover_notional_over_capital"] * capital
        assert traded > 0.0
        assert m["commission_cost"] == pytest.approx(traded * fee, rel=0.01)
        assert m["commission_cost"] / traded * 1e4 == pytest.approx(fee * 1e4, abs=0.1)

    inc = receipt["metrics"]["incumbent"]
    breakeven = receipt["capacity_scenarios"][0]["breakeven_extra_cost_bp"]
    # G3: breakeven is the excess over the notional the receipt publishes.
    assert breakeven == pytest.approx(
        inc["excess_vs_primary"] / inc["turnover_notional_over_capital"] * 1e4, abs=1e-6
    )
    assert receipt["capacity_scenarios"][0]["assumed_fee_bp"] == pytest.approx(fee * 1e4)
    assert receipt["claim_status"] == "NO_ECONOMIC_CLAIM"


# ---------------------------------------------------------------------------
# #396 — `excess_ci_low/high` is the interval of the EXCESS RETURN it is
# attached to. A per-bar Sharpe ratio is a different estimand and must never be
# published as a return interval (it does not even contain its own point).
# ---------------------------------------------------------------------------

CANONICAL_RECEIPT_588D668B_REL = "artifacts/benchmarks/economic_receipt_588d668b.json"


def _canonical_receipt_path() -> Path | None:
    """Frozen #396 evidence receipt: this checkout, else the shared main checkout.

    Evidence artifacts live outside git (worktrees do not carry them), so the
    worktree falls back to the parent checkout. Absence skips: never synthesize.
    """
    root = Path(__file__).resolve().parents[2]
    for candidate in (root / CANONICAL_RECEIPT_588D668B_REL,
                      root.parent.parent / CANONICAL_RECEIPT_588D668B_REL):
        if candidate.is_file():
            return candidate
    return None


def _mechanics_excess_paths(
    n: int = 300, gap: float = -0.002, seed: int = 21
) -> tuple[list[float], list[float]]:
    """MECHANICS ONLY: a strategy path and a primary path with a known window gap.

    Zero evaluative weight: seeded arithmetic, no market data, no claim. The
    strategy loses `gap` per bar to the primary on average, so the window excess
    is large enough to be excluded by a per-bar Sharpe interval.
    """
    import numpy as np

    rng = np.random.default_rng(seed)
    primary = [float(v) for v in 0.0005 + 0.01 * rng.standard_normal(n)]
    strat = [p + gap + float(v) for p, v in zip(primary, 0.002 * rng.standard_normal(n), strict=True)]
    return strat, primary


def test_mechanics_excess_ci_is_return_interval_not_sharpe() -> None:
    """#396 (G1): the stored `excess_ci_*` is the CI of the excess RETURN.

    Pre-fix the interval was the block-bootstrap Sharpe of the per-bar return
    difference, so it was on a ratio scale, equal to that Sharpe interval by
    construction, and could exclude the `excess_vs_primary` it was published
    beside. The module's `EXCESS_CI_RULE` text must name the statistic the code
    actually applies.
    """
    strat, primary = _mechanics_excess_paths()
    eq = _mechanics_equity(strat)
    p_eq = _mechanics_equity(primary)
    m = eb.metrics_for_curve(eq, [1.0] * len(eq), 1.0, 5.0, None, "ANALYTIC_MODEL", p_eq, 1)

    assert m.excess_vs_primary is not None
    assert m.excess_ci_low is not None and m.excess_ci_high is not None
    # (1) the interval contains the point estimate it belongs to.
    assert m.excess_ci_low <= m.excess_vs_primary <= m.excess_ci_high

    # (2) it is NOT the diff series' bootstrap Sharpe statistic (the pre-fix value).
    sharpe_lo, sharpe_hi = eb.block_bootstrap_ci(
        [a - b for a, b in zip(eb.per_bar_returns(eq), eb.per_bar_returns(p_eq), strict=False)]
    )
    assert sharpe_lo is not None and sharpe_hi is not None
    assert (m.excess_ci_low, m.excess_ci_high) != (sharpe_lo, sharpe_hi)
    # Discriminator: the Sharpe interval cannot hold a window-scale return.
    assert not (sharpe_lo <= m.excess_vs_primary <= sharpe_hi)

    # (3) the applied estimator is the return-scale one, and the rule names it.
    assert (m.excess_ci_low, m.excess_ci_high) == pytest.approx(
        eb.period_excess_ci(eq, p_eq), rel=0, abs=0
    )
    rule = eb.EXCESS_CI_RULE
    assert "EXCESS RETURN" in rule
    assert "RETURN scale" in rule
    assert "per-bar excess" not in rule.lower()
    import inspect

    assert "period_excess_ci" in inspect.getsource(eb.metrics_for_curve)
    # The pre-fix Sharpe primitive must not be reachable from the excess block.
    assert "excess_ci = block_bootstrap_ci(diff)" not in inspect.getsource(eb.metrics_for_curve)


def test_mechanics_excess_rule_sign_test_survives_on_return_interval() -> None:
    """#396 (G3): verdict semantics are unchanged — the rule stays a sign test.

    `EXCESS_CI_RULE` still fires SUPPORTS_UNDERPERFORMANCE iff the published
    interval is entirely below zero, on the return scale, for the canonical
    point estimate recorded in the bound receipt; the fix mints no claim.
    """
    receipt_path = _canonical_receipt_path()
    if receipt_path is None:
        pytest.skip("canonical #396 receipt not present in this checkout")
    assert receipt_path is not None  # narrowed for the type checker
    recorded = json.loads(receipt_path.read_text(encoding="utf-8"))
    inc = recorded["metrics"]["portfolio_P"]
    point = inc["excess_vs_primary"]
    # The recorded (pre-fix) interval published a Sharpe statistic and excludes
    # its own point estimate: exactly the defect this card closes.
    assert not (inc["excess_ci_low"] <= point <= inc["excess_ci_high"])

    computed_stats = {
        "dsr": {"verdict": "COMPUTED", "dsr_confidence": 0.5,
                "selected_sharpe_nonannualized": -0.1, "selected_variant": "portfolio_P"},
        "pbo": {"verdict": "COMPUTED"},
        "spa": {"verdict": "COMPUTED", "pvalues": {"consistent": 0.4}},
        "variant_excess_vs_baseline": -0.01,
    }
    base = {
        "chrono_ok": True, "chrono_note": "OK",
        "leak_probe": eb.detect_future_leak(_synth_bars()), "excess": point,
        "mix": {"incremental_net": -0.0038}, "cost_basis_ok": True,
        "funding_missing": False, "live_fills_present": False, "parity_ok": True,
    }
    # Return-scale interval around the same point: below zero => same verdict.
    v = eb.build_verdicts(
        **base, excess_ci=(point - 0.15, point + 0.15), stats=dict(computed_stats)
    )
    assert v.statistical == "SUPPORTS_UNDERPERFORMANCE"
    assert "EXCESS_CI_RULE" in v.statistical_note
    assert eb.EconomicReceipt.__pydantic_fields__["claim_status"].default == "NO_ECONOMIC_CLAIM"
    assert recorded["claim_status"] == "NO_ECONOMIC_CLAIM"
    assert recorded["verdicts"]["statistical"] == "SUPPORTS_UNDERPERFORMANCE"
    assert "EXCESS_CI_RULE" in recorded["verdicts"]["statistical_note"]


def _receipt_canon(obj: object) -> str:
    """Canonical JSON text of a receipt cell, for byte-identity comparison."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def test_economic_portfolio_control_names_the_series_it_measured(tmp_path: Path) -> None:
    """#493 A2/A3/A4 on the real engine receipt. Skips when the quad tape is absent.

    The defect: this receipt published `known_defect_future_leak_caught=CAUGHT`
    with per-instrument reasons reading "N/M closes are not in their own bar"
    while the same receipt's `research_validity` reported the identical probe
    VALID. Both statements were about different series and the block never said
    which. So: the published block must name the displaced copy it measured
    (input/rule/extent/digest), carry the real-series reading beside it, and the
    measured sections must be byte-identical between two independent runs of the
    same window -- the change is provenance only.
    """
    tape = _quad_tape_dir()
    if tape is None:
        pytest.skip("quad tape absent")
    from v8_next.app import portfolio as port_mod

    receipts = []
    for name in ("a", "b"):
        out_dir = tmp_path / name
        rc = port_mod.main(
            ["--tape-path", str(tape), "--bars", "168", "--output-dir", str(out_dir)]
        )
        assert rc == 0
        receipts.append(
            json.loads(
                sorted(out_dir.glob("economic_receipt_*.json"))[0].read_text(encoding="utf-8")
            )
        )
    receipt, regenerated = receipts

    block = receipt["controls"]["known_defect_future_leak_caught"]
    _assert_leak_block_is_named(block, "receipt control")
    assert block["probe"] == eb.FUTURE_LEAK_PROBE
    instruments = block["instruments"]
    assert set(instruments) == {"AVAXUSDT", "BTCUSDT", "ETHUSDT", "SOLUSDT"}
    for name, per in instruments.items():
        _assert_leak_block_is_named(per, f"receipt control {name}")
        assert per["bars_out"] == per["bars_in"] - 1
        # The copy is the measurement; the tape itself is the second subject.
        assert per["status"] == "CAUGHT"
        real = per["real_series"]
        assert real["status"] == "OK"
        assert f"close_in_own_bar={real['bars']}/{real['bars']}" in real["reason"]
    # The roll-up is re-derivable from its own per-instrument cells.
    assert block["bars_in"] == sum(p["bars_in"] for p in instruments.values())
    assert block["bars_out"] == sum(p["bars_out"] for p in instruments.values())
    assert block["input_sha256"] == eb.series_set_sha256(
        {n: p["input_sha256"] for n, p in instruments.items()}
    )
    # The half that used to read as a contradiction: same receipt, same probe,
    # the real series VALID -- now published as the block's other named subject.
    assert receipt["verdicts"]["research_validity"] == "VALID"
    assert block["real_series"]["status"] == "OK"

    # A4: provenance only -- the measured cells do not move between runs.
    for key in ("metrics", "oos_metrics", "statistics", "verdicts", "controls"):
        assert _receipt_canon(receipt[key]) == _receipt_canon(regenerated[key]), key


def test_economic_report_control_names_the_series_it_measured(tmp_path: Path) -> None:
    """#493 A3: the second producer (`app/economic.py`) publishes the same block.

    The single-instrument path hands the control through verbatim, so it must
    carry the identical provenance on a real receipt -- otherwise a `CAUGHT`
    block can still be published unnamed from one of the two producers.
    """
    tape = _btc_tape_file()
    if tape is None:
        pytest.skip("Real tape not found")
    from v8_next.app import economic as econ_mod

    out_dir = tmp_path / "econ"
    rc = econ_mod.main(
        ["--tape-path", str(tape), "--bars", "168", "--output-dir", str(out_dir)]
    )
    assert rc == 0
    receipt = json.loads(
        sorted(out_dir.glob("economic_receipt_*.json"))[0].read_text(encoding="utf-8")
    )
    block = receipt["controls"]["known_defect_future_leak_caught"]
    _assert_leak_block_is_named(block, "economic receipt control")
    assert block["bars_out"] == block["bars_in"] - 1
    # Two different series in one block: the copy that was caught is not the
    # series that stayed VALID, and the digests say so without trusting a label.
    assert block["input_sha256"] != block["real_series"]["series_sha256"]
    assert receipt["verdicts"]["research_validity"] == "VALID"
    assert block["real_series"]["status"] == "OK"
    assert receipt["claim_status"] == "NO_ECONOMIC_CLAIM"


pytestmark = pytest.mark.slow  # #469: tape/engine file, fast loop excludes via -m "not slow"
