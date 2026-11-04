"""#449 — every declared risk limit is measured against the record, or named UNMEASURED.

The defect: ``risk_factor()`` published ``RESPECTED`` / factor 1.0 while every ``max_drawdown``
slot it read was ``null`` -- the numeric filter at the comparison site dropped the nulls, so
the declared ``max_drawdown: -0.25`` limit had no code path that could ever breach it, and
``max_fee_drag_vs_gross`` / ``max_exposure`` were consulted by no code at all. The same P2
artifact publishes ``max_adverse_excursion_pct`` 96.32 (``causal_trend``) and 100.83
(``plain_swing``) -- under ``families`` (plural) and in *percent* -- invisible to a reader
that looks for ``family`` (singular) and compares fractions. A record that reaches ruin was
published as limits-respected.

#470 -- the repaired cell was then compared across *bases*: the limit is declared as a
fraction of the capital base, while the P2 record declares its excursion ``percent of that
path's own running peak, in return-sum units (not capital percent)``. 96.32 and -0.25 are two
different quantities, so the published BREACH was asserted, not measured (Rule 12), and the
error is symmetric -- the very same comparison can publish a capital breach as RESPECTED. A
verdict is now derived only from a number published on the limit's own basis; a limit with no
same-basis number is ``UNMEASURED`` with reason ``RISK_LIMIT_BASIS_MISMATCH``. The numeric
factor is unchanged (0.0 before the fix, 0.0 after); the cell's name and its named reason
change, no threshold, score or verdict does.

Evidence class: mechanics / arithmetic. The fixture artifacts written here are test-local and
labelled ``MECHANICS ONLY``: they carry zero evaluative weight and never enter
``docs/evidence``. The two assertions that read the real repository artifacts read them
read-only (they are the frozen P2 evidence) and skip when the artifacts are absent. No
assertion here mints an economic claim, no threshold is moved, and no return is asserted.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from v8_next.app.readiness import (
    BASELINE_REL,
    BASIS_UNKNOWN,
    LIMIT_BASES,
    PILLAR_ARTIFACTS,
    RETURN_SUM_BASIS,
    RISK_LIMIT_BASIS_MISMATCH,
    RISK_LIMITS,
    risk_factor,
)

#: MECHANICS ONLY: the label every fixture written by this file carries.
MECHANICS_ONLY = "MECHANICS ONLY: test-local arithmetic fixture, zero evaluative weight"

REAL_P2 = PILLAR_ARTIFACTS["P2_paper_trade_4y"]

STATUS_VOCABULARY = {"RESPECTED", "BREACH", "UNMEASURED"}

#: the basis declaration the frozen P2 record itself publishes for its excursion field
RETURN_SUM_DECLARATION = (
    "percent of that path's own running peak, in return-sum units (not capital percent)"
)

#: the declaration of a source that does measure against the capital base
CAPITAL_DECLARATION = "percent of the capital base deployed as position size"


def _artifact(tmp_path: Path, name: str, payload: dict[str, Any]) -> tuple[Path, list[str]]:
    """Write one labelled MECHANICS ONLY fixture and return (repo_root, rel paths)."""
    rel = f"tests_local/{name}"
    path = tmp_path / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    body = {"claim_status": "NO_ECONOMIC_CLAIM", "evidence_class": MECHANICS_ONLY, **payload}
    path.write_text(json.dumps(body, indent=2, sort_keys=True) + "\n")
    return tmp_path, [rel]


def _fee_gross_family() -> dict[str, Any]:
    """A MECHANICS ONLY family publishing the fee and gross sums the fee-drag limit reads."""
    return {
        "families": {
            "causal_trend": {"fee_cost_return_sum": -0.02, "gross_return_sum": 0.5},
        }
    }


def _repo_root() -> Path:
    """The repository root, located from this file; skipped when the evidence is absent."""
    for parent in Path(__file__).resolve().parents:
        if (parent / REAL_P2[2]).is_file():
            return parent
    pytest.skip(f"the P2 paper-trade evidence is absent at {REAL_P2[2]}")


def _excursion_artifact(
    tmp_path: Path,
    name: str,
    excursion_pct: float,
    *,
    exposure: Any = None,
    declared_unit: str | None = RETURN_SUM_DECLARATION,
    capital_drawdown: float | None = None,
) -> tuple[Path, list[str]]:
    """A one-policy paper-trade record publishing a percentage adverse excursion.

    ``declared_unit`` is the family's own ``drawdown_basis.unit`` -- the text the record
    publishes its own basis in. A fixture that drops it publishes no basis at all, and the
    reader must name that instead of assuming the number is on the limit's basis.
    """
    family: dict[str, Any] = {
        "fee_cost_return_sum": -0.02,
        "gross_return_sum": 0.5,
        "robustness_vector": {"max_adverse_excursion_pct": excursion_pct},
    }
    if declared_unit is not None:
        family["drawdown_basis"] = {
            "basis": "uncompounded_sum_of_campaign_returns",
            "fields": ["max_adverse_excursion_pct"],
            "unit": declared_unit,
        }
    payload: dict[str, Any] = {"families": {"causal_trend": family}}
    if exposure is not None:
        payload["exposure"] = exposure
    if capital_drawdown is not None:
        payload["max_drawdown"] = capital_drawdown
    return _artifact(tmp_path, name, payload)


def _assert_no_cross_basis_verdict(result: dict[str, Any]) -> None:
    """F1: no BREACH/RESPECTED cell is derived from a pair it does not share a basis with."""
    for name, row in result["limit_assessments"].items():
        assert row["limit_basis"] == LIMIT_BASES[name]
        matching = [o for o in row["observations"] if o["basis"] == row["limit_basis"]]
        for observation in row["observations"]:
            assert observation["limit_basis"] == row["limit_basis"]
        if row["status"] in {"BREACH", "RESPECTED"}:
            assert matching, f"{name} published {row['status']} with no same-basis number"
        # every published breach names the limit's own basis as the basis it was read on
        for line in row["breaches"]:
            assert f"basis {row['limit_basis']} = the limit's own basis" in line
        assert bool(row["breaches"]) == (row["status"] == "BREACH")
        # A1: every observation's (limit basis, source basis) pair is written down
        assert {(row["limit_basis"], o["basis"]) for o in row["observations"]} == {
            (pair["limit_basis"], pair["source_basis"]) for pair in row["basis_pairs"]
        }
        for pair in row["basis_pairs"]:
            assert pair["matches"] == (pair["source_basis"] == pair["limit_basis"])


def test_a_record_whose_drawdown_slots_are_all_null_is_never_respected(tmp_path: Path) -> None:
    """F1: null drawdown slots may not add up to a respected limit."""
    root, rels = _artifact(
        tmp_path,
        "null_drawdowns.json",
        {
            "family": {
                "causal_trend": {
                    "totals_by_slippage_fraction": {
                        "0.0": {
                            "max_drawdown": None,
                            "net_return_measured_only_sum": -0.029,
                            "fee_cost_return_sum": -0.027,
                            "gross_return_sum": 0.044,
                        }
                    }
                }
            }
        },
    )
    result = risk_factor(root, rels)

    assert result["status"] != "RESPECTED"
    assert result["factor"] == 0.0
    assert result["metrics"]["causal_trend.slippage_0.0.max_drawdown"] is None

    drawdown = result["limit_assessments"]["max_drawdown"]
    assert drawdown["status"] == "UNMEASURED"
    assert drawdown["observations"] == []
    assert drawdown["basis_pairs"] == []
    assert drawdown["breaches"] == []
    assert any(line.startswith("max_drawdown:") for line in result["unmeasured"])
    assert any("max_adverse_excursion_pct" in line for line in result["unmeasured"])
    # F3: the absent source is named -- no number is invented for it
    assert result["breaches"] == []


def test_a_percentage_excursion_is_normalised_before_the_fraction_comparison(
    tmp_path: Path,
) -> None:
    """A5/#470: 96.32 % of the path's own peak is not -0.25 of capital -- named, not verdicted."""
    root, rels = _excursion_artifact(tmp_path, "excursion_9632.json", 96.32, exposure=0.4)
    result = risk_factor(root, rels)

    drawdown = result["limit_assessments"]["max_drawdown"]
    assert drawdown["status"] == "UNMEASURED"
    assert drawdown["reason"] == RISK_LIMIT_BASIS_MISMATCH
    assert drawdown["breaches"] == []
    assert not any("max_drawdown" in line for line in result["breaches"])
    assert any(line.startswith("max_drawdown:") for line in result["unmeasured"])
    assert any(RISK_LIMIT_BASIS_MISMATCH in line for line in result["unmeasured"])

    row = drawdown["observations"][0]
    assert row["published"] == 96.32
    assert row["unit"] == "percent"
    # the unit is still normalised before the comparison...
    assert row["measured"] == pytest.approx(0.9632)
    assert "max_adverse_excursion_pct" in row["source"]
    # ...but the two numbers are not on one basis, so no capital-basis cell comes of it
    assert row["limit_basis"] == drawdown["limit_basis"] == LIMIT_BASES["max_drawdown"]
    assert row["basis"] == RETURN_SUM_BASIS
    assert drawdown["basis_pairs"] == [
        {
            "limit_basis": LIMIT_BASES["max_drawdown"],
            "source_basis": RETURN_SUM_BASIS,
            "artifact": rels[0],
            "source": row["source"],
            "matches": False,
        }
    ]

    # the two other declared limits were measured on the same record and respected
    assert result["limit_assessments"]["max_fee_drag_vs_gross"]["status"] == "RESPECTED"
    assert result["limit_assessments"]["max_exposure"]["status"] == "RESPECTED"
    # the limit is unmeasured, so the factor stays 0.0 -- as it was when it was a breach
    assert result["status"] == "UNMEASURED"
    assert result["factor"] == 0.0
    _assert_no_cross_basis_verdict(result)


def test_an_excursion_that_declares_no_basis_is_named_unknown(tmp_path: Path) -> None:
    """A1/A2: a source that publishes no basis is read as 'unknown', never as the limit's."""
    root, rels = _excursion_artifact(
        tmp_path, "undeclared_excursion.json", 96.32, exposure=0.4, declared_unit=None
    )
    result = risk_factor(root, rels)

    drawdown = result["limit_assessments"]["max_drawdown"]
    assert drawdown["status"] == "UNMEASURED"
    assert drawdown["reason"] == RISK_LIMIT_BASIS_MISMATCH
    assert drawdown["breaches"] == []
    assert not any("max_drawdown" in line for line in result["breaches"])

    row = drawdown["observations"][0]
    assert row["basis"] == BASIS_UNKNOWN
    assert row["limit_basis"] == drawdown["limit_basis"] == LIMIT_BASES["max_drawdown"]
    assert drawdown["basis_pairs"][0]["source_basis"] == BASIS_UNKNOWN
    assert drawdown["basis_pairs"][0]["matches"] is False
    assert result["factor"] == 0.0
    _assert_no_cross_basis_verdict(result)


def test_a_capital_fraction_drawdown_keeps_its_verdict_on_the_limit_s_own_basis(
    tmp_path: Path,
) -> None:
    """A3: a drawdown published as a capital fraction keeps BREACH / RESPECTED."""
    root, rels = _artifact(
        tmp_path,
        "fraction_drawdown_beyond.json",
        {"max_drawdown": -0.30, "exposure": 0.4, **_fee_gross_family()},
    )
    result = risk_factor(root, rels)

    drawdown = result["limit_assessments"]["max_drawdown"]
    assert drawdown["status"] == "BREACH"
    assert drawdown["limit_basis"] == "capital_fraction"
    row = drawdown["observations"][0]
    assert row["source"] == "max_drawdown"
    assert row["unit"] == "fraction"
    assert row["measured"] == pytest.approx(0.30)
    assert row["basis"] == row["limit_basis"] == drawdown["limit_basis"]
    assert drawdown["basis_pairs"][0]["matches"] is True
    assert drawdown["breaches"][0].startswith("max_drawdown 0.30000000 beyond -0.25")
    assert result["factor"] == 0.0
    _assert_no_cross_basis_verdict(result)

    # ...and one inside the limit is respected, on the same basis
    root, rels = _artifact(
        tmp_path,
        "fraction_drawdown_inside.json",
        {"max_drawdown": -0.10, "exposure": 0.4, **_fee_gross_family()},
    )
    result = risk_factor(root, rels)

    drawdown = result["limit_assessments"]["max_drawdown"]
    assert drawdown["status"] == "RESPECTED"
    assert drawdown["breaches"] == []
    assert result["status"] == "RESPECTED"
    assert result["factor"] == 1.0
    _assert_no_cross_basis_verdict(result)


def test_a_mismatched_number_never_carries_the_verdict_beside_a_same_basis_pair(
    tmp_path: Path,
) -> None:
    """A2/F1: the verdict comes from the capital pair only; the return-sum cell is recorded."""
    root, rels = _excursion_artifact(
        tmp_path, "both_bases.json", 96.32, exposure=0.4, capital_drawdown=-0.30
    )
    result = risk_factor(root, rels)

    drawdown = result["limit_assessments"]["max_drawdown"]
    assert drawdown["status"] == "BREACH"
    assert [row["basis"] for row in drawdown["observations"]] == [
        "capital_fraction",
        RETURN_SUM_BASIS,
    ]
    assert [row["matches"] for row in drawdown["basis_pairs"]] == [True, False]
    assert len(drawdown["breaches"]) == 1
    assert "0.30000000" in drawdown["breaches"][0]
    assert not any("96.32" in line for line in result["breaches"])
    assert result["factor"] == 0.0
    _assert_no_cross_basis_verdict(result)


def test_a_tenth_is_respected_only_after_the_unit_is_normalised(tmp_path: Path) -> None:
    """A2/A3: 10.0 % of capital is 0.10 as a fraction -- respected; raw it would breach."""
    root, rels = _excursion_artifact(
        tmp_path,
        "excursion_10.json",
        10.0,
        exposure=0.4,
        declared_unit=CAPITAL_DECLARATION,
    )
    result = risk_factor(root, rels)

    drawdown = result["limit_assessments"]["max_drawdown"]
    assert drawdown["status"] == "RESPECTED"
    assert drawdown["observations"][0]["measured"] == pytest.approx(0.10)
    assert drawdown["observations"][0]["basis"] == drawdown["limit_basis"]
    assert result["status"] == "RESPECTED"
    assert result["factor"] == 1.0
    assert result["breaches"] == []
    assert result["unmeasured"] == []
    _assert_no_cross_basis_verdict(result)


def test_the_fee_drag_limit_is_compared_not_merely_read(tmp_path: Path) -> None:
    """A3: the fee-drag limit has a consumer -- a real ratio breaches it by name."""
    root, rels = _artifact(
        tmp_path,
        "fee_drag.json",
        {
            "families": {
                "plain_swing": {
                    "fee_cost_return_sum": -1.2,
                    "gross_return_sum": 0.5,
                    "robustness_vector": {"max_adverse_excursion_pct": 5.0},
                }
            },
            "exposure": 0.4,
        },
    )
    result = risk_factor(root, rels)

    fee_drag = result["limit_assessments"]["max_fee_drag_vs_gross"]
    assert fee_drag["status"] == "BREACH"
    assert fee_drag["observations"][0]["measured"] == pytest.approx(2.4)
    assert fee_drag["observations"][0]["basis"] == fee_drag["limit_basis"]
    assert fee_drag["basis_pairs"][0]["matches"] is True
    assert any("max_fee_drag_vs_gross" in line for line in result["breaches"])
    assert result["status"] == "BREACH"
    assert result["factor"] == 0.0
    _assert_no_cross_basis_verdict(result)


def test_every_declared_limit_is_evaluated_or_named_unmeasured(tmp_path: Path) -> None:
    """A3: no declared limit is silently unconsulted; a mismatch is named, not verdicted."""
    root, rels = _excursion_artifact(tmp_path, "limits.json", 5.0)
    result = risk_factor(root, rels)

    assert set(result["limit_assessments"]) == set(RISK_LIMITS)
    for name, row in result["limit_assessments"].items():
        assert row["status"] in STATUS_VOCABULARY
        assert row["limit"] == RISK_LIMITS[name]
        assert row["limit_basis"] == LIMIT_BASES[name]
        assert row["source"]
        if row["status"] == "UNMEASURED":
            assert any(line.startswith(f"{name}:") for line in result["unmeasured"])
            if row["observations"]:
                # A2: numbers on another basis are named and never verdicted
                assert row["reason"] == RISK_LIMIT_BASIS_MISMATCH
                assert all(
                    observation["basis"] != row["limit_basis"]
                    for observation in row["observations"]
                )
                assert row["breaches"] == []
            else:
                assert row["reason"] != RISK_LIMIT_BASIS_MISMATCH
        else:
            assert row["observations"], f"{name} has a verdict with no measurement behind it"
            # A1/F1: a verdict stands only on a pair of one basis
            assert all(
                observation["basis"] == row["limit_basis"] for observation in row["observations"]
            )

    # this record publishes no exposure at all: the limit is named, never assumed respected
    exposure = result["limit_assessments"]["max_exposure"]
    assert exposure["status"] == "UNMEASURED"
    assert any(line.startswith("max_exposure:") for line in result["unmeasured"])
    # ...and the factor is therefore 0.0 even though nothing breached
    assert result["breaches"] == []
    assert result["status"] == "UNMEASURED"
    assert result["factor"] == 0.0
    _assert_no_cross_basis_verdict(result)


def test_no_record_at_all_is_missing(tmp_path: Path) -> None:
    """The module contract is kept: no record is MISSING, not a silent zero on a limit."""
    result = risk_factor(tmp_path, ["docs/evidence/v87-r3/PAPER_4Y/paper_trade_4y.json"])

    assert result["status"] == "MISSING"
    assert result["factor"] == 0.0
    assert result["metrics"] == {}
    assert set(result["limit_assessments"]) == set(RISK_LIMITS)
    for name, row in result["limit_assessments"].items():
        assert row["status"] == "UNMEASURED"
        assert row["limit_basis"] == LIMIT_BASES[name]
        assert row["basis_pairs"] == []


def test_the_real_p2_record_is_never_reported_as_limits_respected() -> None:
    """A1/A2: the frozen record's excursion is not a capital fraction: named, never verdicted."""
    result = risk_factor(_repo_root(), list(REAL_P2))

    assert result["status"] != "RESPECTED"
    assert result["factor"] == 0.0
    assert result["breaches"]

    drawdown = result["limit_assessments"]["max_drawdown"]
    assert drawdown["status"] == "UNMEASURED"
    assert drawdown["reason"] == RISK_LIMIT_BASIS_MISMATCH
    assert drawdown["breaches"] == []
    assert not any("max_drawdown" in line for line in result["breaches"])
    assert any(
        "max_adverse_excursion_pct" in row["source"] for row in drawdown["observations"]
    )
    assert all(row["basis"] == RETURN_SUM_BASIS for row in drawdown["observations"])
    assert all(row["limit_basis"] == drawdown["limit_basis"] for row in drawdown["observations"])
    assert drawdown["limit_basis"] == LIMIT_BASES["max_drawdown"]
    assert all(pair["matches"] is False for pair in drawdown["basis_pairs"])
    assert max(row["measured"] for row in drawdown["observations"]) > abs(RISK_LIMITS["max_drawdown"])
    assert any("96.32" in str(row["published"]) for row in drawdown["observations"])
    assert any("100.82" in str(row["published"]) for row in drawdown["observations"])

    # the exposure limit has no source in this artifact set: named, not assumed
    assert result["limit_assessments"]["max_exposure"]["status"] == "UNMEASURED"
    assert any(line.startswith("max_exposure:") for line in result["unmeasured"])
    _assert_no_cross_basis_verdict(result)


def test_the_published_baseline_names_every_limit_and_its_verdict() -> None:
    """A4: the republished baseline carries the breach/unmeasured limit by name.

    The baseline is the frozen snapshot published before #470: it still carries the
    cross-basis drawdown cell it was written from. It is read, never re-pinned here.
    """
    root = _repo_root()
    path = root / BASELINE_REL
    if not path.is_file():
        pytest.skip(f"no published baseline at {BASELINE_REL}")
    baseline = json.loads(path.read_text())

    assert baseline["claim_status"] == "NO_ECONOMIC_CLAIM"
    limits = baseline["risk_limits"]
    assert set(limits["assessments"]) == set(RISK_LIMITS)
    assert limits["status"] in STATUS_VOCABULARY
    for name, row in limits["assessments"].items():
        assert row["status"] in STATUS_VOCABULARY
        assert row["limit"] == RISK_LIMITS[name]
        assert row["source"]
    # the record breaches the fee-drag limit and publishes no exposure: the baseline says so
    assert limits["status"] != "RESPECTED"
    assert baseline["risk_factor"] == 0.0
    assert any("max_fee_drag_vs_gross" in line for line in limits["breaches"])
    assert any(line.startswith("max_exposure:") for line in limits["unmeasured"])
    # ...and it still carries the cross-basis drawdown cell #470 corrects (96.32 percent of
    # a return-sum path read as a capital fraction). The frozen snapshot is read, not pinned.
    assert any("max_adverse_excursion_pct" in line for line in limits["breaches"])
