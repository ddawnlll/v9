"""Proving-ground contracts (MECHANICS ONLY where synthetic).

Worlds are synthetic by design (correctness evidence, AF-T12), so the bars
below are seed-pinned generator output — no assertion carries economic or
evaluative weight. All receipts carry NO_ECONOMIC_CLAIM. The real-tape
battery rule is untouched: nothing here scores edge, gates, or PnL.

Provisioning: the STATIONARY_BOOTSTRAP family resamples through ``arch``, which
is declared in the ``research`` extra only. Those tests skip explicitly (never
collapse) in a bare ``--extra dev`` environment so the documented fast loop can
still collect and run this module; the engine/refusal/digest contracts below
need no research dependency and always run.
"""

from __future__ import annotations

import hashlib
from types import SimpleNamespace
from typing import cast

import pytest

from v8_next.evaluation.proving_battery import run_battery
from v8_next.system_proving.metrics import FIELD_ABSENCE_REASON, UNMEASURED
from v8_next.system_proving.receipt import SystemProvingGroundReceipt
from v8_next.system_proving.run import (
    FILL_PROFILE,
    SystemProvingGroundRunner,
    engine_smoke,
    fill_profile_digest,
)
from v8_next.world.foundry import build_world
from v8_next.world.spec import (
    SYNTHETIC_TAG_PREFIX,
    SyntheticPopulation,
    WorldFamily,
    WorldSpec,
)

#: `arch` is the research extra's resampler; the STATIONARY_BOOTSTRAP family
#: cannot be generated without it, so those tests skip by name instead of
#: collapsing the whole module (and the fast loop's report) into an error.
_ARCH_REASON = "arch not installed (research extra)"


def _spec(**overrides: object) -> WorldSpec:
    base: dict[str, object] = {
        "family": WorldFamily.STATIONARY_BOOTSTRAP,
        "population": SyntheticPopulation.SYNTHETIC_DEV,
        "symbol": "BTCUSDT",
        "n_bars": 60,
        "base_price": 50000.0,
        "volatility_annualized": 0.65,
        "jump_frequency": 12.0,
        "jump_mean": -0.015,
        "jump_std": 0.03,
        "seed": 453,
    }
    base.update(overrides)
    return WorldSpec(**base)  # type: ignore[arg-type]


def test_world_is_deterministic_per_seed() -> None:
    pytest.importorskip("arch", reason=_ARCH_REASON)
    once, twice = build_world(_spec()), build_world(_spec())
    assert once.receipt_digest == twice.receipt_digest
    assert once.world_id == twice.world_id
    other = build_world(_spec(seed=454))
    assert other.receipt_digest != once.receipt_digest


def test_world_population_never_mixes_with_research() -> None:
    pytest.importorskip("arch", reason=_ARCH_REASON)
    world = build_world(_spec())
    assert world.population_tag.startswith(SYNTHETIC_TAG_PREFIX)
    assert world.population_tag not in ("observed-research", "protected-holdout")
    assert world.as_dict()["claim"] == "NO_ECONOMIC_CLAIM"


def test_unported_family_is_refused_not_substituted() -> None:
    with pytest.raises(ValueError, match="FAMILY_NOT_PORTED"):
        build_world(_spec(family=WorldFamily.LEARNED_GENERATIVE_WORLD))


def test_empty_world_is_refused() -> None:
    with pytest.raises(ValueError, match="WORLD_SPEC_EMPTY"):
        _spec(n_bars=0)


def test_full_chain_exercises_af_t12_and_conserves() -> None:
    pytest.importorskip("arch", reason=_ARCH_REASON)
    world = build_world(_spec(n_bars=120))
    receipt = cast(
        SystemProvingGroundReceipt,
        SystemProvingGroundRunner.run_full_chain("p", world, 10000.0, 453),
    )
    assert receipt.exercises_full_pipeline is True
    assert receipt.attribution.verify_conservation()
    # #487 follow-up: this runner keeps no second ledger, so it reconciles no residual and
    # must publish no verdict. The assertion is the absence itself, not a truthy pass —
    # a fabricated ``0.0`` residual used to mint ``double_entry_reconciled: true`` here.
    assert receipt.metrics.cashflow_discrepancy_usdt is None
    assert receipt.metrics.is_double_entry_reconciled() is None
    double_entry = receipt.metrics.field_reports()["cashflow_discrepancy_usdt"]
    assert double_entry["status"] == UNMEASURED
    assert double_entry["value"] is None
    assert double_entry["reason"] == FIELD_ABSENCE_REASON["cashflow_discrepancy_usdt"]
    assert receipt.as_dict()["world_id"] == world.world_id


def test_full_chain_publishes_no_double_entry_verdict_without_a_second_ledger() -> None:
    """Arch-independent pin: no residual is minted where none was reconciled.

    ``STATIONARY_BOOTSTRAP`` worlds need ``arch``, so the family tests above skip in the
    fast loop. The double-entry contract is a property of the runner's own books, not of
    the generator: a stub world (deterministic fixture bars, no economic claim) reaches
    the same ``run_full_chain`` path and keeps this regression assertable everywhere.
    """
    world = SimpleNamespace(
        world_id="synthetic-fixture-world",
        bars=[SimpleNamespace(close=50000.0 + 10.0 * (index % 7)) for index in range(40)],
    )
    receipt = cast(
        SystemProvingGroundReceipt,
        SystemProvingGroundRunner.run_full_chain("p", world, 10000.0, 453),
    )

    assert receipt.exercises_full_pipeline is True
    assert receipt.metrics.cashflow_discrepancy_usdt is None
    assert receipt.metrics.is_double_entry_reconciled() is None
    published = receipt.as_dict()["metrics"]
    assert isinstance(published, dict)
    assert published["cashflow_discrepancy_usdt"] is None
    double_entry = receipt.metrics.field_reports()["cashflow_discrepancy_usdt"]
    assert double_entry["status"] == UNMEASURED
    assert double_entry["reason"] == FIELD_ABSENCE_REASON["cashflow_discrepancy_usdt"]

    # The receipt identity still covers only the run's identity fields — never the
    # metrics, so the verdict/residual cannot leak into the digest.
    identity = (
        f"{world.world_id}|p|{receipt.total_trades}|{receipt.total_campaigns}|453"
    )
    digest = hashlib.sha256(identity.encode()).hexdigest()
    assert receipt.receipt_digest == digest
    assert receipt.receipt_id == f"spg-receipt-{digest[:16]}"


def test_double_run_digest_equal() -> None:
    pytest.importorskip("arch", reason=_ARCH_REASON)
    world = build_world(_spec(n_bars=120))
    first = cast(
        SystemProvingGroundReceipt,
        SystemProvingGroundRunner.run_full_chain("p", world, 10000.0, 453),
    )
    second = cast(
        SystemProvingGroundReceipt,
        SystemProvingGroundRunner.run_full_chain("p", world, 10000.0, 453),
    )
    assert first.receipt_digest == second.receipt_digest


def test_slippage_lane_is_seed_pinned_and_modelled() -> None:
    assert FILL_PROFILE["column"] == "MODELLED"
    assert FILL_PROFILE["model"] == "ProbabilisticFillModel"
    assert fill_profile_digest(453) == fill_profile_digest(453)
    assert fill_profile_digest(453) != fill_profile_digest(454)


def test_engine_smoke_runs_synthetic_without_venue_fetch() -> None:
    binding = engine_smoke("BTCUSDT")
    assert binding.engine == "BacktestEngine"
    assert binding.venue == "SYNTH"
    assert binding.fill_model == "ProbabilisticFillModel"


def test_battery_report_is_fail_closed_and_named() -> None:
    pytest.importorskip("arch", reason=_ARCH_REASON)
    report = run_battery(n_bars=60)
    assert report["claim"] == "NO_ECONOMIC_CLAIM"
    assert report["double_run_digest_equal"] is True
    assert report["af_t12_exercised"] is True
    findings = cast("list[dict[str, object]]", report["fault_findings"])
    names = [str(finding["finding"]) for finding in findings]
    assert "FAULT_DETECTED_CLOSE_MUTATION" in names
    assert "MISSING_MARK_BOOK" in names
