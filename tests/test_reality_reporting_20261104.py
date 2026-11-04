"""MECHANICS ONLY: finite Monte Carlo reporting is versioned and bounded."""

import pytest

from v8_next.evaluation.reality_check import _monte_carlo_pvalue


@pytest.mark.parametrize("exceed,reps,expected", [(0, 19, 0.05), (1, 19, 0.1), (19, 19, 1.0)])
def test_corrected_reporting(exceed: int, reps: int, expected: float) -> None:
    assert _monte_carlo_pvalue(exceed, reps, "PLUS_ONE_V1") == expected


def test_legacy_identity_and_invalid_input() -> None:
    assert _monte_carlo_pvalue(0, 19, "LEGACY_EXCEED_OVER_REPS_V1") == 0.0
    with pytest.raises(ValueError):
        _monte_carlo_pvalue(20, 19, "PLUS_ONE_V1")
    with pytest.raises(ValueError):
        _monte_carlo_pvalue(0, 19, "UNKNOWN")
