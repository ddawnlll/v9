"""#495: the product dashboard reads the gate key that is actually published.

The rename of ``g3_benchmark_coverage`` -> ``g3_scenario_robustness`` reached
``GateVector``, the descriptor, ``scoring.py`` and ``app/portfolio.py``, but not the
committed dashboard ``site/app.html``, whose gate order still asked for the old key.
After the rename that cell would silently render ``UNKNOWN`` on a receipt that measured
it — the "silent default" failure mode #495 exists to remove, moved into a renderer.

Ledger entries written before the rename were digested under the old key and are
immutable (D-164), so the dashboard must still *read* it as a fallback. Both halves are
asserted here.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

DASHBOARD = Path(__file__).resolve().parents[1] / "site" / "app.html"
pytestmark = pytest.mark.skipif(not DASHBOARD.is_file(), reason="historical dashboard not migrated")


def _gate_order() -> list[str]:
    html = DASHBOARD.read_text(encoding="utf-8")
    match = re.search(r"const order=\[([^\]]+)\]", html)
    assert match, "gate order array not found in site/app.html"
    return [part.strip().strip("'\"") for part in match.group(1).split(",")]


def test_dashboard_gate_order_names_the_published_g3_cell() -> None:
    order = _gate_order()
    g3 = [key for key in order if key.startswith("g3_")]
    assert g3 == ["g3_scenario_robustness"], f"dashboard asks for {g3}"
    assert len(order) == 10, order


def test_dashboard_still_reads_the_pre_rename_key_as_a_fallback() -> None:
    html = DASHBOARD.read_text(encoding="utf-8")
    assert "gates.g3_benchmark_coverage" in html, (
        "stored ledgers were digested under the old key; the dashboard must still read it"
    )
    bare = re.findall(r"gates\['g3_benchmark_coverage'\]", html)
    assert not bare, "the old key must only appear as a fallback, not as the primary lookup"
