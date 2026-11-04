"""#447 — the readiness gate vector's G0/G1/G2 cells are measured, never declared.

The producer defect: ``resolve_all_gates()`` published ``g0_identity``,
``g1_causal_pit`` and ``g2_determinism_ledger`` as literal ``GateState.PASS``, so the
published ``gate_coverage.passed`` counted a state no measurement stood behind, and the
registry entry feeding that count named a resolver symbol that exists nowhere in the
tree. Every published gate state must be a measurement; when the measurement cannot be
taken on a path, the state is ``UNKNOWN``/``BLOCKED`` with a named reason.

Evidence class: producer wiring (mechanics). The structural cells are asserted against
the canonical rule in ``evaluation/scoring.py`` -- this file pins that the rule is used,
not a second ontology, and that its *defaults* are not what mints a PASS. No assertion
here requires a PASS, a score or a readiness index; no economic performance is asserted
on synthetic input.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from v8_next.evaluation import gate_registry as gate_registry_module
from v8_next.evaluation import gate_resolution
from v8_next.evaluation.benchmark_receipt import BenchmarkLedger, GateState, GateVector
from v8_next.evaluation.gate_registry import (
    GATE_RESOLVERS,
    registry_by_field,
    validate_registry,
)
from v8_next.evaluation.gate_resolution import (
    CAUSAL_PIT_NOT_MEASURED,
    DEFAULT_TAPE_PATH,
    LEDGER_EMPTY_NO_ENTRIES,
    LEDGER_VERIFICATION_FAILED,
    load_tape_candles,
    measure_ledger_parity,
    resolve_all_gates,
    resolve_structural_gates,
)
from v8_next.evaluation.scoring import evaluate_gate_vector

STRUCTURAL_FIELDS = ("g0_identity", "g1_causal_pit", "g2_determinism_ledger")

#: MECHANICS ONLY: the probe digest is a fixed 64-hex string so G9's registry lookup is
#: deterministic; it binds no artifact and carries no claim.
_PROBE_DIGEST = "0" * 64


def _real_candles(limit: int = 150) -> tuple:
    """A real tape slice: the structural inputs are measured on real bars."""
    if not Path(DEFAULT_TAPE_PATH).is_file():
        pytest.skip(f"real tape absent at {DEFAULT_TAPE_PATH}")
    return tuple(load_tape_candles(DEFAULT_TAPE_PATH, limit=limit))


def _resolve_probe(candles: tuple, tmp_path: Path):
    """Run the whole battery the readiness audit runs, on a fresh empty ledger."""
    return resolve_all_gates(
        candles=candles,
        # no campaign was measured on this probe; G5 falls back to the run's own bars
        # exactly as it does for the audit, and its verdict is not asserted here.
        campaign_return_series=[],
        ledger=BenchmarkLedger(),
        receipt_digest=_PROBE_DIGEST,
        capability_score=0.0,
        strategy_config=None,
        output_dir=tmp_path,
    )


# --------------------------------------------------------------------------- #
# 1 — an empty ledger does not resolve G2 to PASS, and names why
# --------------------------------------------------------------------------- #


def test_empty_ledger_publishes_no_pass_for_g2_and_names_the_reason(tmp_path: Path) -> None:
    candles = _real_candles()
    report = _resolve_probe(candles, tmp_path)

    assert report.gates.g2_determinism_ledger is not GateState.PASS
    assert report.g2_metrics["reason"] == LEDGER_EMPTY_NO_ENTRIES
    assert report.g2_metrics["status"] == "UNRUN"
    assert "UNKNOWN, never PASS" in report.g2_metrics["note"]

    # the same rule, called directly on the same ledger, agrees with the published cell
    state, metrics = measure_ledger_parity(BenchmarkLedger())
    assert state is report.gates.g2_determinism_ledger
    assert metrics == report.g2_metrics


def test_ledger_that_does_not_verify_blocks_by_name() -> None:
    """A measured ledger failure is BLOCKED (named), not an absence and not a PASS."""
    # MECHANICS ONLY: a receipt whose published digest was altered after the entry was
    # built. The ledger's own verification is the measurement under test.
    from v8_next.evaluation.benchmark_receipt import BenchmarkReceipt, LedgerEntry

    receipt = BenchmarkReceipt.create(
        case_id="BC-447-LEDGER-PROBE",
        policy_id="pol_probe",
        capability_score=None,
        gates=GateVector(),
        computed_at_timestamp_ns=1_000,
    )
    tampered = receipt.model_copy(update={"receipt_digest": "f" * 64})
    ledger = BenchmarkLedger([LedgerEntry.create(0, BenchmarkLedger.GENESIS_HASH, tampered)])
    assert ledger.verify_report().ok is False

    state, metrics = measure_ledger_parity(ledger)
    assert state is GateState.BLOCKED
    assert metrics["reason"].startswith(LEDGER_VERIFICATION_FAILED)
    assert state is not GateState.PASS


# --------------------------------------------------------------------------- #
# 2 — an unmeasured structural input never resolves to PASS
# --------------------------------------------------------------------------- #


def test_unmeasured_lineage_and_pit_inputs_never_resolve_to_pass() -> None:
    """With nothing to measure, the cells are UNKNOWN -- and the reason is named."""
    resolution = resolve_structural_gates((), BenchmarkLedger())

    assert resolution.inputs.has_continuous_lineage is None
    assert resolution.inputs.is_causal_pit is None
    assert resolution.inputs.g2_state is GateState.UNKNOWN

    assert resolution.gates.g0_identity is GateState.UNKNOWN
    assert resolution.gates.g1_causal_pit is GateState.UNKNOWN
    assert resolution.gates.g2_determinism_ledger is GateState.UNKNOWN
    assert resolution.metrics["g0"]["reason"] == "NO_CANDLES"
    assert resolution.metrics["g1"]["reason"] == CAUSAL_PIT_NOT_MEASURED
    assert resolution.metrics["g2"]["reason"] == LEDGER_EMPTY_NO_ENTRIES

    # the canonical rule on exactly these inputs agrees, cell for cell
    derived = evaluate_gate_vector(
        total_bars=0,
        total_trades=0,
        pnl_series=[],
        mismatches=None,
        has_continuous_lineage=None,
        is_causal_pit=None,
        g2_state=GateState.UNKNOWN,
    )
    assert tuple(getattr(derived, field) for field in STRUCTURAL_FIELDS) == tuple(
        getattr(resolution.gates, field) for field in STRUCTURAL_FIELDS
    )

    # ... and this is why the inputs are handed over explicitly: the rule's own
    # defaults (lineage=True, PIT=True, mismatches=0) would have minted a PASS.
    with_defaults = evaluate_gate_vector(total_bars=0, total_trades=0, pnl_series=[])
    assert with_defaults.g0_identity is GateState.PASS
    assert with_defaults.g1_causal_pit is GateState.PASS
    assert with_defaults.g2_determinism_ledger is GateState.PASS


# --------------------------------------------------------------------------- #
# 3 — the published cells are the canonical rule's output, not literals
# --------------------------------------------------------------------------- #


def test_structural_cells_are_derived_by_the_canonical_rule(tmp_path: Path) -> None:
    candles = _real_candles()
    ledger = BenchmarkLedger()

    resolution = resolve_structural_gates(candles, ledger)
    expected = evaluate_gate_vector(
        total_bars=len(candles),
        total_trades=0,
        pnl_series=[],
        mismatches=None,
        has_continuous_lineage=resolution.inputs.has_continuous_lineage,
        is_causal_pit=resolution.inputs.is_causal_pit,
        g2_state=resolution.inputs.g2_state,
    )
    assert tuple(getattr(resolution.gates, field) for field in STRUCTURAL_FIELDS) == tuple(
        getattr(expected, field) for field in STRUCTURAL_FIELDS
    )

    # the battery measures the same inputs from the same bars and ledger, so its
    # published vector must carry exactly these cells -- and never the old literal
    report = _resolve_probe(candles, tmp_path)
    assert tuple(getattr(report.gates, field) for field in STRUCTURAL_FIELDS) == tuple(
        getattr(resolution.gates, field) for field in STRUCTURAL_FIELDS
    )
    assert report.gates.g0_identity is not None
    assert report.gates.g1_causal_pit is GateState.UNKNOWN
    assert report.gates.g2_determinism_ledger is not GateState.PASS
    # G0 is a real measurement on real bars: PASS or BLOCKED, never a placeholder
    assert report.gates.g0_identity in (GateState.PASS, GateState.BLOCKED)
    assert report.g0_metrics["input"] == "has_continuous_lineage"
    assert report.g0_metrics["measured"] is resolution.inputs.has_continuous_lineage


# --------------------------------------------------------------------------- #
# 4 — the registry map names real resolvers; the old bypass is gone
# --------------------------------------------------------------------------- #


def test_registry_map_declares_existing_resolvers_only() -> None:
    assert validate_registry() == []
    for canonical_id, symbol in GATE_RESOLVERS.items():
        assert not symbol.startswith("resolver:"), f"{canonical_id}: {symbol} names no resolver"
        assert callable(getattr(gate_resolution, symbol, None)), f"{canonical_id}: {symbol}"

    registry = registry_by_field()
    for field in STRUCTURAL_FIELDS:
        assert registry[field].resolver == "resolve_structural_gates"


def test_a_resolver_that_does_not_exist_is_reported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The prefix that used to be skipped is a problem now, like any other ghost."""
    monkeypatch.setitem(
        gate_registry_module.GATE_RESOLVERS,
        "G0ConstitutionalIntegrity",
        "resolver:receipt_structural",
    )
    problems = validate_registry()
    assert any(
        "G0ConstitutionalIntegrity" in problem and "resolver:receipt_structural" in problem
        for problem in problems
    ), problems

    monkeypatch.setitem(
        gate_registry_module.GATE_RESOLVERS, "G0ConstitutionalIntegrity", "evaluate_g0_ghost"
    )
    problems = validate_registry()
    assert any(
        "G0ConstitutionalIntegrity" in problem and "evaluate_g0_ghost" in problem
        for problem in problems
    ), problems

    # an identifier that exists but is not callable is not a resolver either
    monkeypatch.setitem(
        gate_registry_module.GATE_RESOLVERS, "G0ConstitutionalIntegrity", "MEASURED"
    )
    assert any("G0ConstitutionalIntegrity" in problem for problem in validate_registry())

pytestmark = pytest.mark.slow  # #469: tape/engine file, fast loop excludes via -m "not slow"
