"""#457 — no field of the robustness vector publishes a quantity it did not measure.

The defect: the 14-field system robustness vector that the four-year P2 artifact publishes
carried seven fields whose names were not measurements of those names —

* ``recovery_horizon_bars`` / ``expert_displacement_rate`` were literal constants (``0`` /
  ``0.0``);
* ``turnover_efficiency`` and ``habitat_selectivity_score`` were two names for ONE expression
  (``min(1.0, campaigns / bars)``), byte-identical in the artifact for both families;
* ``capital_utilization_pct`` was the fee column rescaled and clamped
  (``min(100.0, |fee_cost_sum| * 100)`` — both families clamped to exactly ``100.0``);
* ``ruin_margin_pct`` was ``100 - max_adverse_excursion_pct``, the drawdown restated as a
  margin with no declaration that it was a restatement.

No field carried a MISSING marker, so a reader (and every pillar/risk reader that loads this
artifact) read an unmeasured surface as a measured one.

Acceptance (measurable; needs no tape scan):

A1. No published field is a literal constant and no two fields are computed from one
    expression — asserted on the source (AST, no numeric literal in the constructor call) and
    on the per-field basis table (no two MEASURED fields share a signature).
A2. Every field is a function of its own named inputs: varying one input moves exactly the
    fields that name it and no others; an input the producer cannot supply publishes ``null``
    plus a named reason instead of a pass-shaped number.
A3. ``capital_utilization_pct`` is not ``min(100, |fee| * 100)``: it is ``null`` with the named
    reason "no position sizing / no capital basis in this producer", and two different fee
    totals cannot collapse onto one clamp value.
A4. The drawdown/ruin pair names its basis (``uncompounded_sum_of_campaign_returns`` + unit) in
    the artifact, and the drawdown's restatement is declared DERIVED.
A5. The regenerated artifact carries no ``100.0`` capital-utilization clamp and no constant
    field, while the stored receipt identities/digests are unchanged.
A6. Readiness is not fed by this vector at all (none of the 14 names appears in the readiness
    reader), so no gate value, ``claim_status`` or verdict can move as a side effect.

MECHANICS ONLY: the campaign columns in this module are synthetic arithmetic fixtures with
zero evaluative weight — they exercise the publication contract, not the market. The artifact
assertions at the end read the frozen canonical artifact and skip when it is absent.
"""

from __future__ import annotations

import ast
import inspect
import json
from pathlib import Path
from typing import Any

import pytest

from v8_next.system_proving.metrics import (
    DERIVED,
    DRAWDOWN_BASIS,
    FIELD_ABSENCE_REASON,
    FIELD_BASIS,
    FIELD_NAMES,
    MEASURED,
    UNMEASURED,
    SystemRobustnessVector,
    metrics_from_campaigns,
)

V8_NEXT = Path(__file__).resolve().parents[1]
REPO_ROOT = V8_NEXT.parent
METRICS_SRC = V8_NEXT / "src/v8_next/system_proving/metrics.py"
READINESS_SRC = V8_NEXT / "src/v8_next/app/readiness.py"
TOOL_SRC = V8_NEXT / "tools/nx14_paper_trade_4y.py"
RUST_VECTOR_SRC = REPO_ROOT / "v8-core/src/system_proving/metrics.rs"
ARTIFACT = REPO_ROOT / "docs/evidence/v87-r3/PAPER_4Y/paper_trade_4y.json"

#: MECHANICS ONLY — a synthetic uncompounded campaign-return path (zero evaluative weight).
#: Cumulative: 0.5, -0.4, -0.2, 0.2, 1.1 -> deepest drawdown 0.9 from the trough at bar 12,
#: re-attained at bar 45 (33 bars later).
NETS = (0.5, -0.9, 0.2, 0.4, 0.9)
BARS_HELD = (12, 8, 20, 5, 3)
#: The same columns with the peak never re-attained: the horizon has no measurement.
NETS_UNRECOVERED = (0.5, -0.9, 0.2, 0.4, 0.1)
#: The expected drawdown-derived numbers of the synthetic path above.
NETS_RECOVERY_BARS = 33
NETS_DRAWDOWN_PCT = 90.0

#: The receipt identities the four-year paper-trade artifact carried BEFORE this fix. The
#: receipt digest covers world/policy/counts/timestamp only, so a publication fix must not
#: rewrite them (#457 A5/F3).
STORED_RECEIPTS = {
    "causal_trend": ("spg-receipt-e0c16a7bebb13537", 1306, 4),
    "plain_swing": ("spg-receipt-978e06fa12fab335", 1112, 4),
}

#: The drawdown this artifact published BEFORE the fix. It is the one vector field the
#: readiness risk reader consumes (#449: families.<policy>.robustness_vector
#: .max_adverse_excursion_pct), so the fix must leave the number itself untouched.
STORED_DRAWDOWN_PCT = {
    "causal_trend": 96.32085738279993,
    "plain_swing": 100.82684889269997,
}


def _reference(**overrides: Any) -> SystemRobustnessVector:
    """MECHANICS ONLY — the producer's own call shape on a synthetic 5-campaign run."""
    inputs: dict[str, Any] = {
        "campaigns": 5,
        "failures": 3,
        "gross_return_sum": 0.75,
        "fee_cost_sum": 0.31,
        "funding_cost_sum": 0.02,
        "bars": 35_064,
        "campaign_net_returns": list(NETS),
        "campaign_bars_held": list(BARS_HELD),
        "net_with_modelled_slippage_sum": 0.35,
    }
    inputs.update(overrides)
    return metrics_from_campaigns(**inputs)


def _moved(left: SystemRobustnessVector, right: SystemRobustnessVector) -> set[str]:
    before, after = left.as_dict(), right.as_dict()
    return {name for name in FIELD_NAMES if before[name] != after[name]}


# --------------------------------------------------------------------------- #
# A1 — the vector is the Rust contract, and nothing in it is a literal
# --------------------------------------------------------------------------- #
def test_field_names_are_the_rust_contract() -> None:
    assert tuple(SystemRobustnessVector.__dataclass_fields__) == FIELD_NAMES
    assert tuple(_reference().as_dict()) == FIELD_NAMES
    if RUST_VECTOR_SRC.is_file():
        rust = RUST_VECTOR_SRC.read_text()
        for name in FIELD_NAMES:
            assert f"pub {name}:" in rust, f"{name} is not a Rust contract field"


def test_no_published_field_is_a_literal_in_the_source() -> None:
    tree = ast.parse(METRICS_SRC.read_text())
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "SystemRobustnessVector"
    ]
    assert len(calls) == 1, "the vector must be constructed in exactly one place"
    keywords = {keyword.arg: keyword.value for keyword in calls[0].keywords}
    assert set(keywords) == set(FIELD_NAMES), "every field is set explicitly, no default hides"
    for name, value in keywords.items():
        assert not (
            isinstance(value, ast.Constant) and isinstance(value.value, (int, float))
        ), f"{name} is constructed from a literal constant"


def test_every_field_declares_a_basis_and_no_two_measured_fields_share_one() -> None:
    assert set(FIELD_BASIS) == set(FIELD_NAMES)
    assert set(FIELD_ABSENCE_REASON) == set(FIELD_NAMES)
    signatures: dict[tuple[str, tuple[str, ...]], str] = {}
    for name in FIELD_NAMES:
        basis = FIELD_BASIS[name]
        assert basis.status in {MEASURED, DERIVED, UNMEASURED}
        assert basis.statement and basis.unit
        if basis.status == MEASURED:
            assert basis.inputs, f"{name} claims MEASURED without naming its inputs"
            signature = (basis.statement, tuple(basis.inputs))
            assert signature not in signatures, (
                f"{name} and {signatures.get(signature)} are computed from one expression"
            )
            signatures[signature] = name
        elif basis.status == DERIVED:
            assert basis.derived_from in FIELD_NAMES
            assert basis.derived_from != name
        else:
            assert len(FIELD_ABSENCE_REASON[name]) > 20
    reasons = list(FIELD_ABSENCE_REASON.values())
    assert len(set(reasons)) == len(reasons), "each absence names its own missing input"


# --------------------------------------------------------------------------- #
# A2 — each field is a function of its own named inputs
# --------------------------------------------------------------------------- #
def test_each_field_moves_only_with_the_input_it_names() -> None:
    base = _reference()
    assert _moved(base, _reference(failures=4)) == {
        "scenario_failure_fraction",
        "regime_stability_score",  # DERIVED from the failure fraction, declared
    }
    assert _moved(base, _reference(campaigns=10)) == {
        "scenario_failure_fraction",
        "regime_stability_score",
        "habitat_selectivity_score",  # campaigns / decision frames
    }
    assert _moved(base, _reference(fee_cost_sum=0.44)) == {
        "friction_retention_ratio",
        "slippage_fragility_score",
    }
    assert _moved(base, _reference(funding_cost_sum=0.05)) == {
        "friction_retention_ratio",
        "funding_drag_ratio",
        "slippage_fragility_score",
    }
    assert _moved(base, _reference(net_with_modelled_slippage_sum=0.10)) == {
        "slippage_fragility_score",
    }
    assert _moved(base, _reference(campaign_net_returns=list(NETS_UNRECOVERED))) == {
        "recovery_horizon_bars",
    }
    assert _moved(base, _reference(campaign_net_returns=[0.5, -1.4, 0.2, 0.4, 0.9])) == {
        "max_adverse_excursion_pct",
        "ruin_margin_pct",  # DERIVED from the drawdown, declared
    }
    assert _moved(base, _reference(campaign_bars_held=[10, 20, 30, 40, 50])) == {
        "recovery_horizon_bars",  # the horizon is measured in tape bars, not in campaigns
    }
    # the fee column is not a capital base: no fee variation may move utilization
    assert _reference(fee_cost_sum=1.112).capital_utilization_pct is None
    assert _reference(fee_cost_sum=130.6).capital_utilization_pct is None


def test_absent_inputs_publish_null_with_a_named_reason() -> None:
    # the legacy producer call: counts, totals and a caller-measured drawdown, nothing else
    vector = metrics_from_campaigns(
        campaigns=100, failures=94, gross_return_sum=0.66, fee_cost_sum=1.31,
        funding_cost_sum=0.02, max_drawdown_pct=12.0, bars=35_064,
    )
    nulls = {item["field"]: item["reason"] for item in vector.unmeasured_fields()}
    assert set(nulls) == {
        "tail_capture_efficiency",
        "recovery_horizon_bars",
        "slippage_fragility_score",
        "turnover_efficiency",
        "capital_utilization_pct",
        "expert_displacement_rate",
        "cashflow_discrepancy_usdt",  # #487: no residual was supplied
    }
    reports = vector.field_reports()
    for name, reason in nulls.items():
        assert vector.as_dict()[name] is None
        assert reports[name]["status"] == UNMEASURED
        assert reports[name]["value"] is None
        assert reports[name]["reason"] == reason and reason
    # #487: no residual means no verdict — a `0.0` default used to mint `true` here
    assert vector.is_double_entry_reconciled() is None
    # the fields whose inputs ARE present are measured, not null
    assert vector.scenario_failure_fraction == pytest.approx(0.94)
    assert vector.max_adverse_excursion_pct == pytest.approx(12.0)
    assert vector.habitat_selectivity_score is not None


def test_double_entry_verdict_is_absent_unless_a_residual_is_supplied() -> None:
    """#487: the reconciliation verdict is a statement about a MEASURED residual.

    A ``0.0`` default residual made the producer publish ``double_entry_reconciled: true`` —
    with the basis claiming ``MEASURED`` — for a residual nobody measured. The verdict is now
    absent (``None`` + the named absence reason) unless the caller supplies its own residual.
    """
    parameter = inspect.signature(metrics_from_campaigns).parameters[
        "cashflow_discrepancy_usdt"
    ]
    assert parameter.default is None, "a `0.0` default residual mints a verdict nobody measured"

    absent = _reference()
    assert absent.cashflow_discrepancy_usdt is None
    assert absent.is_double_entry_reconciled() is None
    report = absent.field_reports()["cashflow_discrepancy_usdt"]
    assert report["status"] == UNMEASURED
    assert report["value"] is None
    assert report["reason"] == FIELD_ABSENCE_REASON["cashflow_discrepancy_usdt"]
    assert FIELD_BASIS["cashflow_discrepancy_usdt"].status != MEASURED

    # a supplied residual IS the caller's measurement: the field becomes MEASURED and the
    # verdict follows the residual's own size, in both directions
    reconciled = _reference(cashflow_discrepancy_usdt=0.0)
    assert reconciled.field_reports()["cashflow_discrepancy_usdt"]["status"] == MEASURED
    assert reconciled.is_double_entry_reconciled() is True
    unreconciled = _reference(cashflow_discrepancy_usdt=0.25)
    assert unreconciled.is_double_entry_reconciled() is False
    assert _moved(absent, unreconciled) == {"cashflow_discrepancy_usdt"}


def test_recovery_horizon_is_read_off_the_path_and_never_a_constant() -> None:
    base = _reference()
    assert base.recovery_horizon_bars == NETS_RECOVERY_BARS
    assert base.max_adverse_excursion_pct == pytest.approx(NETS_DRAWDOWN_PCT)
    assert base.ruin_margin_pct == pytest.approx(100.0 - NETS_DRAWDOWN_PCT)
    # an unrecovered path publishes null plus the reason, never a zero
    unrecovered = _reference(campaign_net_returns=list(NETS_UNRECOVERED))
    assert unrecovered.recovery_horizon_bars is None
    report = unrecovered.field_reports()["recovery_horizon_bars"]
    assert report["status"] == UNMEASURED
    assert "never re-attains" in report["reason"]


# --------------------------------------------------------------------------- #
# A3/A4 — no clamp, no undeclared restatement
# --------------------------------------------------------------------------- #
def test_capital_utilization_is_not_the_fee_column_rescaled() -> None:
    source = METRICS_SRC.read_text()
    assert "min(100.0" not in source and "min(100," not in source
    assert "abs(fee_cost_sum) * 100.0" not in source
    vector = _reference()
    assert vector.capital_utilization_pct is None
    reason = vector.field_reports()["capital_utilization_pct"]["reason"]
    assert "no position sizing / no capital basis in this producer" in reason


def test_turnover_and_selectivity_are_no_longer_one_expression() -> None:
    source = METRICS_SRC.read_text()
    assert source.count("total / max(1, bars)") == 1, "one expression may carry one name only"
    vector = _reference()
    assert vector.turnover_efficiency is None
    assert vector.turnover_efficiency != vector.habitat_selectivity_score
    assert FIELD_BASIS["turnover_efficiency"].status == UNMEASURED
    assert FIELD_BASIS["habitat_selectivity_score"].inputs == ("campaigns", "bars")


def test_drawdown_and_ruin_name_their_basis_and_the_restatement_is_declared() -> None:
    assert DRAWDOWN_BASIS["basis"] == "uncompounded_sum_of_campaign_returns"
    assert DRAWDOWN_BASIS["unit"]
    assert FIELD_BASIS["ruin_margin_pct"].status == DERIVED
    assert FIELD_BASIS["ruin_margin_pct"].derived_from == "max_adverse_excursion_pct"
    assert FIELD_BASIS["max_adverse_excursion_pct"].status == MEASURED
    assert FIELD_BASIS["regime_stability_score"].derived_from == "scenario_failure_fraction"
    # the restatement is not clamped: a drawdown beyond the notional base is published as such
    vector = metrics_from_campaigns(
        campaigns=10, failures=5, gross_return_sum=0.5, fee_cost_sum=0.1,
        funding_cost_sum=0.0, max_drawdown_pct=150.0, bars=1000,
    )
    assert vector.ruin_margin_pct == pytest.approx(-50.0)
    reports = vector.field_reports()
    assert reports["ruin_margin_pct"]["derived_from"] == "max_adverse_excursion_pct"
    assert reports["ruin_margin_pct"]["status"] == DERIVED


def test_the_producer_hands_its_own_columns_not_a_derived_number() -> None:
    source = TOOL_SRC.read_text()
    assert "min(100.0" not in source
    assert "max_dd" not in source, "the tool must not hand-derive the drawdown the vector owns"
    assert "campaign_net_returns=nets" in source
    assert "campaign_bars_held=" in source
    assert "net_with_modelled_slippage_sum=net_with_modelled_slippage_10" in source


# --------------------------------------------------------------------------- #
# A5/A6 — the canonical artifact, and the surface this fix must not move
# --------------------------------------------------------------------------- #
def test_canonical_artifact_publishes_no_constant_and_no_clamped_duplicate() -> None:
    if not ARTIFACT.is_file():
        pytest.skip(f"canonical artifact absent: {ARTIFACT}")
    report = json.loads(ARTIFACT.read_text())
    for policy, family in report["families"].items():
        vector = family["robustness_vector"]
        assert set(vector) == set(FIELD_NAMES)
        assert vector["turnover_efficiency"] != vector["habitat_selectivity_score"]
        assert vector["capital_utilization_pct"] is None
        assert vector["expert_displacement_rate"] is None
        assert vector["tail_capture_efficiency"] is None
        assert vector["recovery_horizon_bars"] != 0, "the literal 0 horizon is gone"
        numbers = [value for value in vector.values() if isinstance(value, (int, float))]
        assert 100.0 not in numbers, "the capital-utilization clamp is gone"
        basis = family["robustness_vector_basis"]
        assert set(basis) == set(FIELD_NAMES)
        for name, item in basis.items():
            assert item["basis"] and item["unit"] and item["status"] in {
                MEASURED, DERIVED, UNMEASURED,
            }
            if item["value"] is None:
                assert item["reason"], f"{name} is missing without a reason (silent absence)"
        assert family["robustness_vector_unmeasured"], "the missing surface must be named"
        assert family["drawdown_basis"]["basis"] == "uncompounded_sum_of_campaign_returns"
        assert basis["max_adverse_excursion_pct"]["status"] == MEASURED
        assert basis["ruin_margin_pct"]["status"] == DERIVED
        # #487: this producer passes no double-entry residual, so the artifact carries NO
        # verdict — a fabricated `true` used to sit here — and the field that used to claim
        # MEASURED for it is a named null instead.
        assert family["double_entry_reconciled"] is None
        assert family["double_entry_reconciled_reason"] == FIELD_ABSENCE_REASON[
            "cashflow_discrepancy_usdt"
        ]
        assert vector["cashflow_discrepancy_usdt"] is None
        assert basis["cashflow_discrepancy_usdt"]["status"] != MEASURED
        assert basis["cashflow_discrepancy_usdt"]["value"] is None
        receipt_id, campaigns, _ = STORED_RECEIPTS[policy]
        receipt = family["system_proving_receipt"]
        assert receipt["receipt_id"] == receipt_id, "a stored receipt identity was rewritten"
        assert receipt["total_campaigns"] == campaigns
        assert len(receipt["receipt_digest"]) == 64
        # the one vector field readiness consumes keeps the number it published before the fix
        assert vector["max_adverse_excursion_pct"] == pytest.approx(STORED_DRAWDOWN_PCT[policy])
        assert report["claim_status"] == "NO_ECONOMIC_CLAIM"


def test_readiness_reads_no_field_this_fix_left_unmeasured_or_derived() -> None:
    """#449's risk reader owns its own surface; this fix must not move it.

    The reader consumes ``families.<policy>.robustness_vector.max_adverse_excursion_pct``
    (#449). Whatever the reader names from this vector must be a field this fix leaves
    MEASURED with its pre-fix number (pinned in the artifact test above); the five fields this
    fix publishes as ``null`` and the two it declares DERIVED must not be read at all, so no
    gate value, ``claim_status`` or verdict can move as a side effect of the fix.
    """
    source = READINESS_SRC.read_text()
    for name in FIELD_NAMES:
        if name not in source:
            continue
        basis = FIELD_BASIS[name]
        assert basis.status == MEASURED, (
            f"readiness reads {name}, which this fix leaves {basis.status}"
        )
