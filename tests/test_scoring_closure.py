"""Scoring-closure contracts (MECHANICS ONLY for hand-built fixtures).

Hand-built ScoreEvidence records exercising the receipt→score binding seam;
no assertion carries economic weight. Real-window numbers live in
``docs/evidence/v87-port-chain/455/``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from v8_next.evaluation.benchmark_receipt import ScoreEvidence
from v8_next.evaluation.scoring import recompute_capability_score


def _evidence(**overrides: object) -> ScoreEvidence:
    base: dict[str, object] = {
        "total_bars": 120,
        "total_trades": 6,
        "abstain_rate": 0.0,
        "coverage_factor": 0.8,
        "hard_invariants_passed": True,
        "aggregate_status": "MEASURED",
        "domain_values": (
            ("MicrostructureInvariance", 1.0, 6),
            ("ExecutionFidelity", 1.2, 6),
        ),
    }
    base.update(overrides)
    return ScoreEvidence.model_validate(base)


def test_recompute_is_deterministic_per_evidence_bytes() -> None:
    once = recompute_capability_score(_evidence())
    twice = recompute_capability_score(_evidence())
    assert once is not None and once == twice


def test_tampered_evidence_does_not_reproduce_the_score() -> None:
    honest = recompute_capability_score(_evidence())
    tampered = recompute_capability_score(_evidence(domain_values=(("MicrostructureInvariance", 1.0, 6), ("ExecutionFidelity", 0.6, 6))))
    assert honest is not None and tampered is not None and tampered != honest
    coverage_tampered = recompute_capability_score(_evidence(coverage_factor=0.5))
    assert coverage_tampered is not None and coverage_tampered != honest
    refused = recompute_capability_score(
        _evidence(domain_values=(("SYNTHETIC_FANTASY", 99.9, 6),))
    )
    assert refused is None  # undeclared (synthetic-tagged) domains never score


def test_score_without_measured_basis_is_missing_never_zero() -> None:
    assert recompute_capability_score(_evidence(coverage_factor=None)) is None
    assert recompute_capability_score(_evidence(aggregate_status="MISSING_NO_TRADES")) is None
    assert recompute_capability_score(_evidence(domain_values=())) is None


def test_research_store_overlap_rejected_and_burn_is_monotone(tmp_path: Path) -> None:
    from v8_next.evaluation.store import ResearchStore

    store = ResearchStore(tmp_path / "research.db")
    with pytest.raises(ValueError, match="invalid training source coverage"):
        store.assert_no_holdout_overlap("", 10, 5)
    assert store.holdout_pristine("lineage-1", "ds-1") is True
    store.burn_holdout("lineage-1", "ds-1", 453)
    assert store.holdout_pristine("lineage-1", "ds-1") is False  # burns never unburn: frozen OOS never reopened
