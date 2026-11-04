"""MECHANICS ONLY — #498: a net funding CREDIT is not a funding cost.

``FIELD_BASIS["friction_retention_ratio"]`` declares a SIGNED subtraction
(``(gross_return_sum - fee_cost_sum - funding_cost_sum) / gross_return_sum``), but the 4-year
paper lane handed ``metrics_from_campaigns`` the *magnitude* of the funding column
(``funding_cost_sum=abs(funding)``). On the published P2 lane (``families.causal_trend``:
gross ``0.65886053``, net fee cost ``1.306``, net funding CREDIT ``-0.0193954``) that charged
the credit as a cost: the vector published retention ``-1.0116478982`` where the declared
expression on the rows' own sign yields ``-0.9527723113``, and the same distorted
``measured_net`` fed ``slippage_fragility_score``. ``funding_drag_ratio`` was never affected —
its declared basis absolutizes the funding column itself — and must stay unchanged.

The recorded P2 artifact is read as a fixture, never re-pinned: this module asserts that the
fixed publication (a) is recomputable from the family's own published cost columns and (b) is
not lower than that recomputation, while the recorded byte is exactly the charge-the-credit
value the issue was measured on.

No economic claim is made here; the constants are recorded lane totals used as arithmetic
fixtures only. Zero evaluative weight.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from v8_next.system_proving.metrics import FIELD_BASIS, metrics_from_campaigns

V8_NEXT = Path(__file__).resolve().parents[1]
REPO_ROOT = V8_NEXT.parent
TOOL_SRC = V8_NEXT / "tools/nx14_paper_trade_4y.py"

#: The recorded four-year P2 lane the issue was measured on. ``scratch/`` exists only in the
#: primary checkout; the tracked copy carries the same published cost columns.
RECORDED_ARTIFACTS = (
    REPO_ROOT / "scratch/p2-bracket-contract-459/paper_trade_4y.json",
    REPO_ROOT / "docs/evidence/v87-r3/PAPER_4Y/paper_trade_4y.json",
)

#: Recorded totals of ``families.causal_trend`` (issue #498): gross, net fee cost as a positive
#: magnitude, and a net funding CREDIT.
GROSS = 0.65886053
FEE_COST = 1.306
FUNDING_CREDIT = -0.0193954

#: The declared expression on the rows' own sign, and the shape that charges the credit.
DECLARED_RETENTION = -0.9527723113
CHARGED_RETENTION = -1.0116478982117434

#: ``abs(funding_cost_sum) / max(abs(gross_return_sum), eps)`` — unaffected by the sign.
FUNDING_DRAG = 0.0294377932

FAMILIES = ("causal_trend", "plain_swing")


def _vector(
    *,
    gross: float = GROSS,
    fee_cost_sum: float = FEE_COST,
    funding_cost_sum: float | None = FUNDING_CREDIT,
):
    return metrics_from_campaigns(
        campaigns=1,
        failures=0,
        gross_return_sum=gross,
        fee_cost_sum=fee_cost_sum,
        funding_cost_sum=funding_cost_sum,
        bars=1,
    )


def _recorded_lane() -> dict[str, Any]:
    for path in RECORDED_ARTIFACTS:
        if path.is_file():
            return json.loads(path.read_text())
    pytest.skip("no recorded P2 paper-trade artifact in this checkout")


def test_retention_basis_declares_a_signed_subtraction() -> None:
    """The contract text the lane must satisfy; ``metrics.py`` is not edited by this fix."""
    basis = FIELD_BASIS["friction_retention_ratio"]
    assert basis.statement == (
        "(gross_return_sum - fee_cost_sum - funding_cost_sum) / gross_return_sum"
    )
    assert "signed" in basis.unit
    assert set(basis.inputs) == {"gross_return_sum", "fee_cost_sum", "funding_cost_sum"}


def test_a_net_funding_credit_is_not_charged_as_a_cost() -> None:
    """Acceptance 1: the credit lifts retention to the declared value and is strictly better."""
    credit = _vector(funding_cost_sum=FUNDING_CREDIT)
    charged = _vector(funding_cost_sum=abs(FUNDING_CREDIT))

    assert credit.friction_retention_ratio == pytest.approx(DECLARED_RETENTION, abs=1e-9)
    assert charged.friction_retention_ratio == pytest.approx(CHARGED_RETENTION, abs=1e-9)
    assert credit.friction_retention_ratio > charged.friction_retention_ratio


def test_funding_drag_normalises_the_sign_inside_its_own_basis() -> None:
    """Acceptance 2: ``funding_drag_ratio`` stays put — its declared ``abs()`` carries the sign.

    ``0.0294377932`` is the 10-dp form of ``0.0294377931547971``; the float identity is asserted
    exactly, the published decimal to its own precision.
    """
    credit = _vector(funding_cost_sum=FUNDING_CREDIT)
    charged = _vector(funding_cost_sum=abs(FUNDING_CREDIT))

    assert credit.funding_drag_ratio == charged.funding_drag_ratio  # sign-invariant, bit for bit
    assert credit.funding_drag_ratio == pytest.approx(
        abs(FUNDING_CREDIT) / max(abs(GROSS), 2.220446049250313e-16), abs=1e-15
    )
    assert round(credit.funding_drag_ratio, 10) == FUNDING_DRAG
    assert credit.funding_drag_ratio == pytest.approx(FUNDING_DRAG, abs=5e-11)


@pytest.mark.parametrize("family", FAMILIES)
def test_retention_recomputes_from_the_family_own_cost_columns(family: str) -> None:
    """Acceptance 3: recomputable from the published columns, and never lower than the recompute."""
    entry = _recorded_lane()["families"][family]
    gross = entry["gross_return_sum"]
    fee_magnitude = abs(entry["fee_cost_return_sum"])  # the fee column is negative by construction
    funding = entry["funding_cost_return_sum"]
    assert funding < 0, f"{family} is not a net-funding-credit family; guard does not apply"

    recomputed = (gross - fee_magnitude - funding) / gross
    charged = (gross - fee_magnitude - abs(funding)) / gross
    published = _vector(
        gross=gross, fee_cost_sum=fee_magnitude, funding_cost_sum=funding
    ).friction_retention_ratio

    assert published is not None
    assert published == pytest.approx(recomputed, abs=1e-12), "published, not recomputable"
    assert published >= recomputed - 1e-12, "published retention is lower than its own recompute"
    assert published > charged, "the net funding credit is still being charged as a cost"

    recorded = entry["robustness_vector"]["friction_retention_ratio"]
    # The published columns are rounded to 8 dp while the recorded vector read the unrounded
    # sums; against ``plain_swing``'s 0.165 gross that rounding amplifies to ~1e-7 in retention.
    # The two shapes are 6e-2 apart, so the guard still names exactly one of them.
    assert recorded == pytest.approx(charged, abs=1e-6), (
        "the recorded P2 lane predates #498: its publication is exactly the charge-the-credit "
        "value. This card does not re-pin that byte; the guard is that the fixed publication "
        "is never the lower of the two."
    )
    assert recorded < published, "the recorded distortion must be the worse number"

    # The drag was already sign-normalised, so the fix leaves it on both the recorded byte and
    # the recomputation (same 8-dp rounding caveat).
    recorded_drag = entry["robustness_vector"]["funding_drag_ratio"]
    assert recorded_drag == pytest.approx(
        abs(funding) / max(abs(gross), 2.220446049250313e-16), abs=1e-7
    )
    assert recorded_drag < 1.0


def test_the_lane_hands_the_vector_the_signed_funding_column() -> None:
    """Line 560 of the lane: funding goes in signed, the fee column keeps its magnitude."""
    source = TOOL_SRC.read_text()
    assert "funding_cost_sum=abs(funding)" not in source
    assert "funding_cost_sum=funding" in source
    assert "fee_cost_sum=abs(fees)" in source
