from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

from v8_next.domain.market import Candle
from v8_next.economics.grammar import POLICIES, POLICY_REQUIRED_BARS
from v8_next.evaluation.grammar_census import (
    CENSUS_VERSION,
    GRAMMAR_ORDER,
    main,
    panel_gate_audit,
    run_grammar_census,
)

HOUR_NS = 3_600_000_000_000
INSTRUMENT = "BTCUSDT-PERP.BINANCE"


def bar(index: int, o: str, h: str, low: str, c: str) -> Candle:
    start = index * HOUR_NS
    return Candle(
        INSTRUMENT,
        start,
        start + HOUR_NS,
        Decimal(o),
        Decimal(h),
        Decimal(low),
        Decimal(c),
        Decimal(1),
        start + HOUR_NS,
        start + HOUR_NS,
        f"fixture-{index}",
    )


def flat(index: int, price: str = "100") -> Candle:
    return bar(index, price, str(Decimal(price) + Decimal("0.5")),
               str(Decimal(price) - Decimal("0.5")), price)


def breakout_stream(length: int = 130) -> tuple[Candle, ...]:
    """48 quiet bars, a breakout close, a target run, then quiet drift."""
    candles = [flat(i) for i in range(48)]
    candles.append(bar(48, "100", "101.2", "99.9", "101"))
    candles.append(bar(49, "101", "106.2", "100.9", "105"))
    candles.append(bar(50, "105", "107.6", "104.9", "107.5"))
    for i in range(51, length):
        candles.append(bar(i, "107", "107.2", "106.8", "107"))
    return tuple(candles)


def distribution(census, policy: str):
    return next(d for d in census.distributions if d.policy == policy)


def test_every_grammar_reads_the_same_stream_and_warmup():
    candles = breakout_stream()
    census = run_grammar_census(candles, tape_sha256="fixture")

    assert [d.policy for d in census.distributions] == list(GRAMMAR_ORDER)
    assert set(GRAMMAR_ORDER) == set(POLICIES)
    for item in census.distributions:
        assert item.bars_evaluated == len(candles) - POLICY_REQUIRED_BARS[item.policy] + 1
    assert census.bars == len(candles)
    assert census.version == CENSUS_VERSION
    assert census.claim_status == "NO_ECONOMIC_CLAIM"
    assert census.authority == "OFFLINE_PIT_CENSUS"


def test_repeat_triggers_inside_a_window_are_fragments_not_episodes():
    census = run_grammar_census(breakout_stream())
    breakout = distribution(census, "range-breakout-48-v1")

    # bars 48, 49 and 50 all close above the prior 48-bar high; the first opens
    # the episode and the other two repeat the same hypothesis.
    assert breakout.directional_bars == 3
    assert breakout.episodes == 1
    assert breakout.fragments == 2
    assert breakout.target_hits == 1
    assert breakout.hit_rate == 1.0
    assert breakout.censored == 0


def test_same_bar_stop_and_target_resolves_to_the_stop():
    candles = [flat(i) for i in range(48)]
    candles.append(bar(48, "100", "101.2", "99.9", "101"))
    candles.append(bar(49, "101", "106", "98", "99"))
    census = run_grammar_census(tuple(candles))

    breakout = distribution(census, "range-breakout-48-v1")
    assert breakout.stop_hits == 1
    assert breakout.target_hits == 0
    # the same bar closes below the 48-bar low, opening a separate short episode
    assert breakout.episodes == 2
    assert breakout.censored == 1


def test_episode_beyond_the_tape_is_censored_not_scored():
    candles = [flat(i) for i in range(48)]
    candles.append(bar(48, "100", "101.2", "99.9", "101"))
    for i in range(49, 60):
        candles.append(bar(i, "101", "101.1", "100.9", "101"))
    census = run_grammar_census(tuple(candles))

    breakout = distribution(census, "range-breakout-48-v1")
    assert breakout.episodes == 1
    assert breakout.censored == 1
    assert breakout.hit_rate is None


def test_grammars_produce_different_candidate_counts():
    census = run_grammar_census(breakout_stream())
    counts = {d.policy: d.directional_bars for d in census.distributions}

    assert counts["range-breakout-48-v1"] == 3
    assert counts["volatility-extreme-v2"] > 0
    assert counts["mean-reversion-v2"] == 1
    assert counts["compression-expansion-v2"] == 0
    assert len(set(counts.values())) >= 3


def test_panel_gate_audit_reports_a_vacuous_tolerance():
    vacuous = panel_gate_audit(
        expert_count=28, min_support_quorum=1, max_contradiction_tolerance=28
    )
    binding = panel_gate_audit(
        expert_count=28, min_support_quorum=1, max_contradiction_tolerance=0
    )

    assert vacuous.contradiction_can_block is False
    assert binding.contradiction_can_block is True
    assert vacuous.min_support_can_pass_with == "a single support vote"
    assert "never block" in vacuous.note

    with pytest.raises(ValueError):
        panel_gate_audit(
            expert_count=28, min_support_quorum=29, max_contradiction_tolerance=1
        )
    with pytest.raises(ValueError):
        panel_gate_audit(
            expert_count=28, min_support_quorum=1, max_contradiction_tolerance=-1
        )


def test_digest_is_deterministic_and_binds_the_bracket():
    candles = breakout_stream()
    first = run_grammar_census(candles, tape_sha256="fixture")
    again = run_grammar_census(candles, tape_sha256="fixture")
    tighter = run_grammar_census(
        candles, tape_sha256="fixture", stop_pct=Decimal("0.01")
    )

    assert first.evidence_digest == again.evidence_digest
    assert first.evidence_digest.startswith("sha256:")
    assert first.evidence_digest != tighter.evidence_digest
    assert replace(first, evidence_digest="").as_dict() == replace(
        again, evidence_digest=""
    ).as_dict()


def test_unknown_grammar_and_ragged_stream_are_refused():
    with pytest.raises(ValueError):
        run_grammar_census(breakout_stream(), policies=("not-a-grammar",))
    with pytest.raises(ValueError):
        run_grammar_census(breakout_stream(), policies=("mean-reversion-v2",) * 2)
    with pytest.raises(ValueError):
        run_grammar_census(())
    missing_availability = list(breakout_stream())
    missing_availability[3] = replace(missing_availability[3], available_ns=None)
    with pytest.raises(ValueError):
        run_grammar_census(tuple(missing_availability))


def test_census_module_never_reaches_an_execution_surface():
    source = Path("src/v8_next/evaluation/grammar_census.py").read_text()
    imported = [
        line.strip()
        for line in source.splitlines()
        if line.startswith(("import ", "from "))
    ]
    for forbidden in (
        "nautilus_trader",
        "OrderFactory",
        "submit_order",
        "BacktestEngine",
        "SimArmConfig",
    ):
        assert not any(forbidden in line for line in imported)


def test_cli_writes_a_census_without_a_verified_tape(tmp_path, capsys):
    with pytest.raises(ValueError):
        main(["--tape", str(tmp_path)])

    assert capsys.readouterr().out == ""
