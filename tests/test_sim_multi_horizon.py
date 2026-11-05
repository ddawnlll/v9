"""The native replay must consume the multi-horizon context, not just accept it."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from v8_next.adapters.adaptive_strategy import SimArmConfig
from v8_next.evaluation.sim_arm import run_sim_arm
from v8_next.experts.horizon_profiles import PROFILE_TABLE_VERSION

ROOT = Path(__file__).resolve().parents[1]
TAPE = ROOT / "research/tape/btcusdt-1h-2025Q1"
M15 = ROOT / "research/tape/btcusdt-15m-2025Q1"


@pytest.fixture(scope="module")
def measurement():
    return run_sim_arm(
        tape=TAPE,
        config=SimArmConfig(enabled=True),
        limit=220,
        intraday_tape=M15,
    )


def test_every_declared_horizon_actually_produced_stances(measurement) -> None:
    evidence = measurement.horizon_evidence
    assert evidence["profile_table_version"] == PROFILE_TABLE_VERSION
    counts = evidence["stances_by_primary"]
    for timeframe in ("M15", "H1", "H4"):
        bucket = counts[timeframe]
        asserted = bucket["SUPPORT"] + bucket["CONTRADICT"]
        assert asserted > 0, f"{timeframe} experts never asserted: {bucket}"
    # Each group's assertions must be attributable to its own primary horizon.
    for timeframe, expert_ids in evidence["experts_by_primary"].items():
        assert expert_ids, timeframe


def test_alignments_are_complete_at_the_cut(measurement) -> None:
    alignments = measurement.horizon_evidence["last_alignments"]
    for timeframe in ("M15", "H1", "H4"):
        assert alignments[timeframe]["complete"] is True
        assert alignments[timeframe]["quality"] == "OK"
        assert alignments[timeframe]["as_of_ns"] > 0


def test_intraday_tape_is_bound_into_provenance(measurement) -> None:
    manifest = json.loads((M15 / "tape.manifest.json").read_text())
    assert measurement.provenance.intraday_tape_sha256 == manifest["tape_sha256"]
    assert measurement.provenance.tape_sha256


def test_decisions_record_the_profile_table(measurement) -> None:
    assert measurement.decisions
    for decision in measurement.decisions:
        assert decision["horizon_profile_table"] == PROFILE_TABLE_VERSION


def test_protection_stays_clean_with_multi_horizon_management(measurement) -> None:
    assert measurement.protection["violations"] == []
    assert measurement.costed is True


def test_missing_intraday_tape_abstains_instead_of_inventing_m15() -> None:
    """Without the M15 tape, M15 experts must abstain, never see synthetic bars."""
    measurement = run_sim_arm(
        tape=TAPE,
        config=SimArmConfig(enabled=True),
        limit=120,
    )
    evidence = measurement.horizon_evidence
    assert evidence["bars_by_horizon"]["M15"] == 0
    m15 = evidence["stances_by_primary"]["M15"]
    assert m15["SUPPORT"] == 0 and m15["CONTRADICT"] == 0
    assert m15["ABSTAIN"] > 0
    assert evidence["last_alignments"]["M15"]["quality"] == "MISSING"
    assert measurement.provenance.intraday_tape_sha256 == ""
