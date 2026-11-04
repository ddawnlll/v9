"""#471 — the portfolio producer's structural cells are measured, never minted by defaults.

The producer defect: ``app/portfolio.py`` built its ``GateVector`` with

    evaluate_gate_vector(total_bars=..., total_trades=..., pnl_series=...)

so the three blocking/required cells -- G0 identity, G1 causal PIT, G2 determinism
ledger -- were decided by the rule's keyword *defaults* (``has_continuous_lineage=True``,
``is_causal_pit=True``, ``mismatches=0``), i.e. by the absence of an argument. The
canonical resolver ``evaluation.gate_resolution.resolve_structural_gates`` exists to
produce exactly those three cells from measurement, so a published PASS stood behind no
per-bar lineage / PIT / ledger measurement on the portfolio path, and that vector is
hashed into the receipt digest and appended to the durable ledger.

These tests exercise the ``portfolio.py`` producer itself -- the function the run's
call site invokes -- and assert its structural cells against the canonical resolver for
the same inputs, cell for cell.

Evidence class: producer wiring (mechanics). The bars below are hand-made, marked
MECHANICS ONLY and never quoted as a result: what is under test is *which* inputs decide
the three structural cells, not economic performance. No test here requires a PASS, a
capability score or a readiness index on synthetic input.
"""

from __future__ import annotations

import ast
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable

from v8_next.app import portfolio as portfolio_module
from v8_next.domain.market import Candle
from v8_next.evaluation.benchmark_receipt import (
    BenchmarkLedger,
    BenchmarkReceipt,
    GateState,
    GateVector,
    LedgerEntry,
)
from v8_next.evaluation.gate_resolution import (
    CAUSAL_PIT_NOT_MEASURED,
    LEDGER_EMPTY_NO_ENTRIES,
    LEDGER_VERIFICATION_FAILED,
    classify_market_regimes,
    evaluate_g3_scenario_robustness,
    resolve_structural_gates,
)
from v8_next.evaluation.scoring import evaluate_gate_vector

#: The three blocking/required cells this card is about.
STRUCTURAL_FIELDS = ("g0_identity", "g1_causal_pit", "g2_determinism_ledger")

#: The gate producer the portfolio call site must go through (#471).
PRODUCER_NAME = "resolve_portfolio_gates"

#: Cells the portfolio producer does not measure at all: it runs no gate battery, so
#: publishing PASS on any of these would be a state no measurement stands behind.
#: #495: G3 is no longer in this tuple -- ``resolve_portfolio_gates`` now measures the
#: scenario-robustness cell on the bars it was handed, so it is asserted separately.
UNMEASURED_ON_THIS_PATH = (
    "g1_causal_pit",
    "g2_determinism_ledger",
    "g4_structural_robustness",
    "g5_statistical_credibility",
    "g6_protected_oos",
    "g7_generalization",
    "g8_prospective_shadow",
    "g9_live_realization",
)

BAR_NS = 3_600_000_000_000


def _mechanics_candles(bars: int = 8) -> tuple[Candle, ...]:
    """MECHANICS ONLY: gap-free, single-instrument, hand-made bars (no market data)."""
    start = 1_700_000_000_000_000_000
    return tuple(
        Candle(
            instrument_id="BTCUSDT-PERP.BINANCE",
            start_ns=start + i * BAR_NS,
            end_ns=start + (i + 1) * BAR_NS,
            open=Decimal("100"),
            high=Decimal("101"),
            low=Decimal("99"),
            close=Decimal("100"),
            volume=Decimal("1"),
            received_ns=start + (i + 1) * BAR_NS,
            available_ns=start + (i + 1) * BAR_NS,
            source_hash=f"mechanics-only-{i}",
        )
        for i in range(bars)
    )


def _producer() -> Callable[..., GateVector]:
    """The producer the portfolio call site uses, or a failure naming the defect."""
    producer = getattr(portfolio_module, PRODUCER_NAME, None)
    assert producer is not None, (
        f"v8_next.app.portfolio exposes no {PRODUCER_NAME}(): the portfolio run still builds "
        "its gate vector straight from `evaluate_gate_vector(...)`, whose keyword defaults "
        "mint G0/G1/G2 PASS with no measurement behind them (#471)"
    )
    return producer


def _enclosing_function(tree: ast.AST, target: ast.AST) -> str | None:
    """Name of the innermost function definition holding ``target``."""
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for child in ast.walk(node):
                if child is target:
                    return node.name
    return None


def _tampered_ledger() -> BenchmarkLedger:
    """MECHANICS ONLY: an entry whose published digest was altered after it was built."""
    receipt = BenchmarkReceipt.create(
        case_id="BC-471-LEDGER-PROBE",
        policy_id="pol_probe",
        capability_score=None,
        gates=GateVector(),
        computed_at_timestamp_ns=1_000,
    )
    tampered = receipt.model_copy(update={"receipt_digest": "f" * 64})
    return BenchmarkLedger([LedgerEntry.create(0, BenchmarkLedger.GENESIS_HASH, tampered)])


# --------------------------------------------------------------------------- #
# 1 — the producer's structural cells are the resolver's cells, never a default
# --------------------------------------------------------------------------- #


def test_portfolio_producer_publishes_the_resolved_structural_cells() -> None:
    producer = _producer()
    candles = _mechanics_candles()
    ledger = BenchmarkLedger()

    vector = producer(candles, ledger, total_trades=3, pnl_series=[1.0, -1.0, 0.5])

    # Acceptance (1): the emitted vector carries what the canonical resolver produced
    # for the same inputs -- cell for cell, not a second derivation.
    resolution = resolve_structural_gates(candles, ledger)
    assert tuple(getattr(vector, field) for field in STRUCTURAL_FIELDS) == tuple(
        getattr(resolution.gates, field) for field in STRUCTURAL_FIELDS
    )

    # ... and on this path G1 has no per-bar PIT audit behind it, so it is UNKNOWN with
    # a named reason, never PASS.
    assert vector.g1_causal_pit is GateState.UNKNOWN
    assert resolution.metrics["g1"]["reason"] == CAUSAL_PIT_NOT_MEASURED
    assert resolution.inputs.is_causal_pit is None

    # G2 likewise: the empty ledger verifies vacuously, so the cell is UNKNOWN.
    assert vector.g2_determinism_ledger is not GateState.PASS
    assert resolution.metrics["g2"]["reason"] == LEDGER_EMPTY_NO_ENTRIES

    # G0 IS measured on the bars the producer was handed: a lineage measurement may
    # pass or block, but it is never a placeholder.
    assert vector.g0_identity is resolution.gates.g0_identity
    assert vector.g0_identity in (GateState.PASS, GateState.BLOCKED)
    assert resolution.metrics["g0"]["input"] == "has_continuous_lineage"

    # #495: G3 is measured here too, on the same bars, by the resolver that owns the
    # cell. The published state is that resolver's own verdict, never the rule default.
    expected_g3, _g3_basis = evaluate_g3_scenario_robustness(
        classify_market_regimes(candles=candles)
    )
    assert vector.g3_scenario_robustness is expected_g3
    assert vector.g3_scenario_robustness in (GateState.PASS, GateState.BLOCKED)
    assert vector.g3_scenario_robustness is not GateState.UNKNOWN


def test_portfolio_producer_publishes_no_pass_that_no_measurement_stands_behind() -> None:
    producer = _producer()
    vector = producer(
        _mechanics_candles(), BenchmarkLedger(), total_trades=3, pnl_series=[1.0, -1.0]
    )

    unearned = [
        field for field in UNMEASURED_ON_THIS_PATH if getattr(vector, field) is GateState.PASS
    ]
    assert unearned == [], (
        f"the portfolio producer published PASS on cells no measurement stands behind: {unearned}"
    )


def test_portfolio_producer_never_stores_the_rule_default_in_the_g3_cell() -> None:
    """#495: the G3 cell is measured on this path, not left at the rule's default.

    The defect: ``resolve_portfolio_gates`` supplied no ``g3_state``, so a ``Required``
    cell was written to the append-only ledger as the bare default while its name
    claimed a benchmark-coverage check. The producer now measures the cell by the same
    resolver that owns it, so its state differs from the defaulted vector.
    """
    producer = _producer()
    candles = _mechanics_candles()
    vector = producer(candles, BenchmarkLedger(), total_trades=3, pnl_series=[1.0, -1.0])

    defaulted = evaluate_gate_vector(
        total_bars=len(candles), total_trades=3, pnl_series=[1.0, -1.0]
    )
    assert defaulted.g3_scenario_robustness is GateState.UNKNOWN
    assert vector.g3_scenario_robustness is not defaulted.g3_scenario_robustness, (
        "the portfolio receipt stored the rule default in the G3 cell: the cell was never "
        "measured, and a Required cell may not be published as an unmeasured default (#495)"
    )
    assert vector.g3_scenario_robustness in (GateState.PASS, GateState.BLOCKED)


def test_portfolio_receipt_carries_the_g3_basis_that_reproduces_its_state() -> None:
    """#495 R3: a non-default G3 state travels with the partition that decided it.

    The defect: ``resolve_portfolio_gates`` called the G3 resolver and threw its
    measured metrics away, so a receipt could publish ``g3_scenario_robustness =
    PASS`` while carrying none of the regimes, drawdowns or variances that produced
    it. The basis is now published beside the state; a reader can name the evaluated
    regimes and check that the state derives from them without re-running the run.
    """
    from v8_next.evaluation.benchmark_receipt import (
        G3_SCENARIO_BASIS_ABSENT,
        G3ScenarioBasis,
    )

    producer = _producer()
    candles = _mechanics_candles()
    vector, basis = producer(
        candles,
        BenchmarkLedger(),
        total_trades=3,
        pnl_series=[1.0, -1.0],
        include_g3_basis=True,
    )

    # The published state is the resolver's own state, over the partition it measured.
    expected_state, expected_metrics = evaluate_g3_scenario_robustness(
        classify_market_regimes(candles=candles)
    )
    assert vector.g3_scenario_robustness is not GateState.UNKNOWN
    assert basis.state is vector.g3_scenario_robustness is expected_state
    assert len(basis.regimes) == 4, "the fixture must measure more than one regime"
    assert basis.evaluated_regimes == tuple(sorted(expected_metrics["regimes"]))
    for name, measured in basis.regimes.items():
        expected = expected_metrics["regimes"][name]
        assert measured.bars == expected["bars"]
        assert measured.trades == expected["trades"]
        assert measured.max_drawdown == expected["max_drawdown"]
        assert measured.return_variance == expected["return_variance"]
    assert basis.absence is None
    assert basis.reason == expected_metrics["reason"]

    # A receipt publishes the state and its basis together; both survive a round-trip.
    receipt = BenchmarkReceipt.create(
        case_id="BC-495-R3",
        policy_id="pol_495",
        capability_score=None,
        gates=vector,
        computed_at_timestamp_ns=1,
        g3_scenario_robustness_basis=basis,
    )
    reread = BenchmarkReceipt.model_validate_json(receipt.model_dump_json())
    assert reread.g3_scenario_robustness_basis is not None
    assert (
        reread.g3_scenario_robustness_basis.state
        is reread.gates.g3_scenario_robustness
    )
    assert set(reread.g3_scenario_robustness_basis.regimes) == set(
        expected_metrics["regimes"]
    )

    # An unmeasurable partition is a named absence, never an empty mapping that reads
    # as "measured nothing".
    absent = G3ScenarioBasis.from_metrics(GateState.BLOCKED, {"regimes": {}})
    assert absent.regimes == {}
    assert absent.absence == G3_SCENARIO_BASIS_ABSENT


def test_portfolio_producer_reports_a_ledger_that_does_not_verify_as_blocked() -> None:
    """A measured ledger failure is the ledger's own verdict, published -- not smoothed."""
    producer = _producer()
    candles = _mechanics_candles()
    ledger = _tampered_ledger()
    assert ledger.verify_report().ok is False

    vector = producer(candles, ledger, total_trades=1, pnl_series=[1.0])

    resolution = resolve_structural_gates(candles, ledger)
    assert resolution.metrics["g2"]["reason"].startswith(LEDGER_VERIFICATION_FAILED)
    assert vector.g2_determinism_ledger is GateState.BLOCKED
    assert vector.g2_determinism_ledger is resolution.gates.g2_determinism_ledger


# --------------------------------------------------------------------------- #
# 2 — why the missing arguments mattered: the defaults mint the PASS
# --------------------------------------------------------------------------- #


def test_the_rule_defaults_are_what_minted_the_unearned_pass() -> None:
    """Documents the defect the producer wiring removes: absence of an argument = PASS."""
    defaulted = evaluate_gate_vector(
        total_bars=len(_mechanics_candles()), total_trades=3, pnl_series=[1.0]
    )
    assert tuple(getattr(defaulted, field) for field in STRUCTURAL_FIELDS) == (
        GateState.PASS,
        GateState.PASS,
        GateState.PASS,
    )


# --------------------------------------------------------------------------- #
# 3 — the call site's wiring: one rule call, inside the producer, with the inputs
# --------------------------------------------------------------------------- #


def test_portfolio_call_site_hands_the_structural_inputs_to_the_rule() -> None:
    source = Path(portfolio_module.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)

    rule_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "evaluate_gate_vector"
    ]
    assert len(rule_calls) == 1, (
        f"portfolio.py must reach the gate rule exactly once, inside {PRODUCER_NAME}(); "
        f"found {len(rule_calls)} call sites"
    )

    kwargs = {keyword.arg for keyword in rule_calls[0].keywords}
    missing = sorted({"has_continuous_lineage", "is_causal_pit", "g2_state"} - kwargs)
    assert not missing, (
        f"the portfolio call site reaches the rule without {missing}, so the rule's keyword "
        "defaults (lineage=True, PIT=True, mismatches=0) decide G0/G1/G2 (#471)"
    )
    assert _enclosing_function(tree, rule_calls[0]) == PRODUCER_NAME

    producer_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == PRODUCER_NAME
    ]
    callers: list[Any] = [_enclosing_function(tree, node) for node in producer_calls]
    assert "main" in callers, (
        f"main() does not publish through {PRODUCER_NAME}(): the run's call site is "
        f"elsewhere (producer callers: {callers})"
    )
