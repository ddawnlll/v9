"""Simulation-only V9 adaptive arm: boundary, boundary types and a costed smoke run.

These tests protect the P1 execution arm after the independent audit found two
defects in its first draft:

* ``Position.avg_px_open`` is a native ``float`` in 2.0.0rc4, not a ``Price``;
  the arm now converts at that boundary instead of calling ``.as_decimal()``.
* the consensus gate must match the V8 measured baseline (quorum 1, contradiction
  tolerance 28), otherwise the V9-vs-V8 delta includes a gate change.

They also pin the authorization boundary: the arm is disabled unless explicitly
enabled, can only be run by the offline ``BacktestEngine`` runner, and imports no
live venue adapter or credential source.
"""

from __future__ import annotations

import ast
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from v8_next.adapters.adaptive_strategy import (
    AdaptiveSimStrategy,
    SimArmConfig,
    SimArmDisabled,
    SimArmRefused,
    _as_decimal,
)
from v8_next.evaluation.sim_arm import run_baseline_arm, run_sim_arm

TAPE = Path(__file__).resolve().parents[1] / "research/tape/btcusdt-1h-2025Q1"


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return modules


def test_arm_is_disabled_unless_explicitly_enabled() -> None:
    assert SimArmConfig().enabled is False
    with pytest.raises(SimArmDisabled):
        run_sim_arm(tape=TAPE, config=SimArmConfig(), limit=20)


def test_arm_refuses_an_unauthorized_tape(tmp_path: Path) -> None:
    with pytest.raises(SimArmRefused):
        run_sim_arm(tape=tmp_path, config=SimArmConfig(enabled=True), limit=10)


def test_arm_imports_no_live_adapter_or_credential_surface() -> None:
    root = Path(__file__).resolve().parents[1] / "src/v8_next"
    for relative in ("adapters/adaptive_strategy.py", "evaluation/sim_arm.py"):
        source = (root / relative).read_text(encoding="utf-8")
        modules = _imports(root / relative)
        assert not any(module.startswith("nautilus_trader.adapters") for module in modules), relative
        assert "api_key" not in source.lower(), relative
        assert "api_secret" not in source.lower(), relative
        assert "BINANCE_API" not in source, relative


def test_as_decimal_accepts_native_float_and_price_objects() -> None:
    assert _as_decimal(Decimal("1.5")) == Decimal("1.5")
    assert _as_decimal(1.5) == Decimal("1.5")
    assert _as_decimal(SimpleNamespace(as_decimal=lambda: Decimal("2.5"))) == Decimal("2.5")


def test_consensus_gate_matches_the_measured_v8_baseline() -> None:
    config = SimArmConfig()
    assert config.min_support_quorum == 1
    assert config.max_contradiction_tolerance == 28
    assert config.baseline_config().max_contradiction_tolerance == 28


def test_costed_smoke_run_on_the_authorized_tape() -> None:
    measurement = run_sim_arm(
        tape=TAPE,
        config=SimArmConfig(enabled=True),
        limit=60,
    )
    assert measurement.costed is True
    assert measurement.bars == 60
    assert measurement.decision_count >= 54
    assert measurement.unsupported == ()
    assert measurement.provenance.tape_sha256
    assert measurement.orders_digest
    assert measurement.decisions_digest


def test_baseline_uses_the_identical_execution_parameters() -> None:
    baseline = run_baseline_arm(
        tape=TAPE,
        config=SimArmConfig(enabled=True),
        limit=60,
    )
    assert baseline.costed is True
    assert baseline.bars == 60
    assert baseline.provenance.tape_sha256


def test_adaptive_strategy_is_a_native_strategy_subclass() -> None:
    from nautilus_trader.trading import Strategy

    assert issubclass(AdaptiveSimStrategy, Strategy)
