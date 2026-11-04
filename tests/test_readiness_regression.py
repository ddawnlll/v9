"""Regression gate for the locked benchmark: a drop is reported, never re-baselined away."""

from __future__ import annotations

import pytest

from v8_next.app.cli import regression_report_lines
from v8_next.app.readiness import (
    DECLARED_CORRECTIONS,
    REGRESSION_TOLERANCES,
    regression_against,
)

BASE = {
    "readiness": 12.5,
    "gate_passes": 5,
    "pillars_measured": 3,
    "per_policy": {
        "plain_swing": {"net_measured_sum": -0.5, "win_rate_pct": 10.0, "campaigns": 100},
    },
}


def test_identical_state_is_ok() -> None:
    result = regression_against(BASE, BASE)
    assert result["verdict"] == "OK"
    assert not result["regressions"]


def test_an_improvement_is_ok_and_recorded() -> None:
    better = {
        **BASE,
        "readiness": 18.0,
        "gate_passes": 6,
        "per_policy": {"plain_swing": {"net_measured_sum": -0.4, "win_rate_pct": 12.0}},
    }
    result = regression_against(BASE, better)
    assert result["verdict"] == "OK"
    deltas = {row["metric"]: row["delta"] for row in result["findings"]}
    assert deltas["readiness"] == 5.5 and deltas["gate_passes"] == 1


def test_a_dropped_gate_is_a_regression() -> None:
    worse = {**BASE, "gate_passes": 4}
    result = regression_against(BASE, worse)
    assert result["verdict"] == "REGRESSION"
    assert any("gate_passes" in line for line in result["regressions"])


def test_a_dropped_score_is_a_regression_even_within_float_noise() -> None:
    worse = {**BASE, "readiness": 12.49}
    result = regression_against(BASE, worse)
    assert result["verdict"] == "REGRESSION"


def test_tolerances_bound_the_paper_trade_metrics() -> None:
    marginal = {
        **BASE,
        "per_policy": {"plain_swing": {"net_measured_sum": -0.5004, "win_rate_pct": 10.4}},
    }
    assert regression_against(BASE, marginal)["verdict"] == "OK"
    beyond = {
        **BASE,
        "per_policy": {"plain_swing": {"net_measured_sum": -0.51, "win_rate_pct": 9.0}},
    }
    result = regression_against(BASE, beyond)
    assert result["verdict"] == "REGRESSION"
    assert len(result["regressions"]) == 2


def test_a_missing_policy_is_a_regression() -> None:
    result = regression_against(BASE, {**BASE, "per_policy": {}})
    assert result["verdict"] == "REGRESSION"
    assert "missing" in result["regressions"][0]


def test_tolerances_are_declared_not_implicit() -> None:
    assert set(REGRESSION_TOLERANCES) == {
        "readiness", "gate_passes", "pillars_measured", "net_measured_sum", "win_rate_pct",
    }
    assert REGRESSION_TOLERANCES["readiness"] == 0.0


# --- #458 Target (3) / D-166: a declared correction is marked, the baseline is not moved -----


def test_a_declared_correction_is_a_correction_and_not_a_regression() -> None:
    """The pinned 4 -> the producer's measured 2 is the declared correction, not a verdict."""
    pinned = {**BASE, "gate_passes": 4}
    result = regression_against(pinned, {**BASE, "gate_passes": 2})
    assert result["verdict"] == "OK"
    assert result["regressions"] == []
    row = next(row for row in result["findings"] if row["metric"] == "gate_passes")
    assert row["regressed"] is False
    assert row["baseline"] == 4 and row["current"] == 2
    assert row["correction"]["superseded"] == 4 and row["correction"]["corrected"] == 2
    assert row["correction"]["authority"] == "D-166"
    assert result["corrections"] == [row["correction"]]


def test_the_correction_still_prints_the_metric_and_its_authority() -> None:
    pinned = {**BASE, "gate_passes": 4}
    lines = regression_report_lines(regression_against(pinned, {**BASE, "gate_passes": 2}))
    assert lines[0] == "[regression] VERDICT: OK"
    marked = [line for line in lines if "CORRECTION" in line and "gate_passes" in line]
    assert marked, lines
    assert not any("REGRESSED" in line for line in lines)
    assert any("D-166" in line for line in lines)
    # honest about the thing that did not happen: the baseline's bytes stay put
    assert any("is not moved" in line for line in lines)


def test_a_correction_covers_exactly_its_declared_pair_and_nothing_beyond_it() -> None:
    """A further drop is a new regression: one is not a licence to stop reporting the metric."""
    pinned = {**BASE, "gate_passes": 4}
    result = regression_against(pinned, {**BASE, "gate_passes": 1})
    assert result["verdict"] == "REGRESSION"
    assert result["corrections"] == []
    assert any("gate_passes 4 -> 1" in line for line in result["regressions"])


def test_an_undeclared_delta_still_reports_a_regression() -> None:
    dropped_score = regression_against(BASE, {**BASE, "readiness": 11.5})
    assert dropped_score["verdict"] == "REGRESSION"
    assert dropped_score["corrections"] == []
    # an undeclared metric keeps the strict comparison even on the corrected value's path
    pinned = {**BASE, "gate_passes": 5}
    assert regression_against(pinned, {**BASE, "gate_passes": 4})["verdict"] == "REGRESSION"


def test_the_declaration_can_be_withheld_for_a_strict_comparison() -> None:
    pinned = {**BASE, "gate_passes": 4}
    result = regression_against(pinned, {**BASE, "gate_passes": 2}, corrections={})
    assert result["verdict"] == "REGRESSION"
    assert result["corrections"] == []


def test_every_declared_correction_names_its_authority_and_reason() -> None:
    """A correction is declared with its authority and its reason, never implicit (#458 T3)."""
    assert set(DECLARED_CORRECTIONS) == {"gate_passes"}
    row = DECLARED_CORRECTIONS["gate_passes"]
    assert row["superseded"] == 4 and row["corrected"] == 2
    assert row["authority"] == "D-166"
    assert row["reason"].strip()
    assert "resolve_structural_gates" in row["reason"]


def test_a_correction_for_a_metric_the_comparison_never_reads_is_rejected() -> None:
    """A declaration that could not be consulted is refused by name, not silently ignored."""
    with pytest.raises(ValueError, match="never reads"):
        regression_against(BASE, BASE, corrections={"capital_score": {"superseded": 1, "corrected": 0}})
