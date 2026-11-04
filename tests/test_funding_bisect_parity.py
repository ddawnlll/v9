"""Funding boundary bisect parity (#461).

MECHANICS ONLY synthetic section: small-timeline equivalence of the binary
search against the linear scan, with zero evaluative weight. No test here
asserts economic performance on synthetic data.

Real-window section: identical boundary-index sequences on the real quad tape
(funding rows x shared end_ns timeline); skips when the tape is absent.
"""

from __future__ import annotations

from typing import Any

import pytest

from v8_next.adapters.tape_paths import tape_path
from v8_next.evaluation.economic_benchmark import funding_boundary_index

QUAD_TAPE = tape_path("quad-1h-12m", "tape.jsonl")


def _linear(end_ns: list[int], boundary: int) -> int | None:
    return next((i for i, ns in enumerate(end_ns) if ns >= boundary), None)


# ---------------------------------------------------------------------------
# MECHANICS ONLY (synthetic timelines, equivalence coverage)
# ---------------------------------------------------------------------------


def test_mechanics_bisect_matches_linear_on_small_timelines() -> None:
    end_ns = [10, 20, 20, 30, 50]
    for boundary in [-5, 0, 10, 11, 19, 20, 21, 29, 30, 31, 49, 50, 51, 10_000]:
        assert funding_boundary_index(end_ns, boundary) == _linear(end_ns, boundary)


def test_mechanics_bisect_past_end_maps_to_none() -> None:
    assert funding_boundary_index([1, 2, 3], 4) is None
    assert funding_boundary_index([], 0) is None
    assert funding_boundary_index([5], 5) == 0
    assert funding_boundary_index([5], 6) is None


def test_mechanics_bisect_unsorted_fails_closed() -> None:
    with pytest.raises(ValueError, match="sorted end_ns"):
        funding_boundary_index([30, 10, 20], 15)


# ---------------------------------------------------------------------------
# Real-window parity (recorded funding legs, before/after equality)
# ---------------------------------------------------------------------------


def test_funding_bisect_parity_on_real_window(cached_multitape: Any) -> None:
    if not QUAD_TAPE.exists():
        pytest.skip(f"real quad tape absent at {QUAD_TAPE}")
    tape = cached_multitape(QUAD_TAPE, limit=500)
    if not tape.candles or not tape.funding:
        pytest.skip("quad tape loaded no candles or funding rows")
    first_inst = sorted(tape.candles)[0]
    end_ns = [c.end_ns for c in tape.candles[first_inst]]
    assert len(end_ns) >= 2
    checked = 0
    for row in tape.funding:
        boundary = int(row.funding_time_ms) * 1_000_000
        assert funding_boundary_index(end_ns, boundary) == _linear(end_ns, boundary)
        checked += 1
        if checked >= 200:
            break
    assert checked > 0

pytestmark = pytest.mark.slow  # #469: tape/engine file, fast loop excludes via -m "not slow"
