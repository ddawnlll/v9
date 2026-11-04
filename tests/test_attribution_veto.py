"""Veto attribution + reconciliation-surface contracts (MECHANICS ONLY).

These tests pin the *shape* of the analysis vocabulary and the anti-fabrication rules the
port had to preserve. Nothing here measures anything about markets and no assertion carries
evaluative weight: the numbers that appear are hand-written structure (two wins, one loss)
used only to exercise the arithmetic of the report.

The one real-data claim this issue owes — every losing trade of the four-year report charged
to exactly one of the seven domains, conservation verified — is produced by the report run
recorded in `docs/evidence/v87-port-chain/451/`, not by a unit test on synthetic candles.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from v8_next.analysis import (
    ADMITTED_RISK_REASONS,
    CANDIDATE_REJECTION_REASONS,
    CANONICAL_VETO_REASONS,
    RECONCILE_EXACT_FIELDS,
    RECONCILE_EXCLUDED_FIELDS,
    RECONCILE_FIELD_COUNT,
    RECONCILE_FLOAT_FIELDS,
    RECONCILE_TOLERANCE,
    RISK_GATE_REFUSAL_REASONS,
    AllocationRejectionReason,
    OutcomeSurface,
    RefusalStage,
    RefusedDecision,
    compute_veto_attribution,
    reconcile_surface,
    refused_beside_lost,
)
from v8_next.oracle import Identifiability
from v8_next.system_proving import FailureDomain
from v8_next.system_proving.attribution import FailureAttributionBreakdown


def _row(
    candidate_id: str,
    reason: str,
    authority: Identifiability,
    *,
    avoided: float | None,
    missed: float | None,
    replayed: float | None = None,
) -> RefusedDecision:
    return RefusedDecision(
        candidate_id=candidate_id,
        expert_id="expert-v1",
        veto_reason=reason,
        stage=RefusalStage.CAMPAIGN_ADMISSION,
        authority_status=authority,
        avoided_loss_usdt=avoided,
        missed_profit_usdt=missed,
        hypothetical_net_return=replayed,
    )


# --------------------------------------------------------------------------------------
# vocabulary is a closed set owned by the ported allocator taxonomy
# --------------------------------------------------------------------------------------


def test_canonical_veto_reason_set_is_the_ported_allocation_taxonomy_plus_the_gates() -> None:
    allocation_codes = {member.value for member in AllocationRejectionReason}
    assert len(allocation_codes) == 9, "allocator.rs:16-44 declares exactly nine codes"
    assert CANONICAL_VETO_REASONS == (
        allocation_codes | set(CANDIDATE_REJECTION_REASONS) | set(RISK_GATE_REFUSAL_REASONS)
    )
    assert set(RISK_GATE_REFUSAL_REASONS).isdisjoint(ADMITTED_RISK_REASONS)


def test_declared_risk_gate_refusals_re_derived_from_the_gate_source() -> None:
    """The veto ledger validates against the gate's own vocabulary, not a copy that can drift.

    Every uppercase reason token in `v8_next/risk/admission.py` and `risk/sizing.py` is read
    back out of the source; the declared refusal tuple must be exactly those tokens minus the
    admitted ones. A new gate reason therefore fails this test until it is declared here too.
    """
    risk_dir = Path(__file__).resolve().parents[1] / "src" / "v8_next" / "risk"
    scanned: set[str] = set()
    for name in ("admission.py", "sizing.py"):
        for line in (risk_dir / name).read_text().splitlines():
            stripped = line.strip()
            if not stripped.startswith("return "):
                continue
            tokens = re.findall(r'"([A-Z][A-Z0-9_]{3,})"', stripped)
            scanned |= set(tokens)
    declared_admits = set(ADMITTED_RISK_REASONS)
    assert declared_admits <= scanned, "the admitted tokens must exist in the gate source"
    assert scanned - declared_admits == set(RISK_GATE_REFUSAL_REASONS)


def test_an_unlisted_reason_is_recorded_but_named_as_uncanonical() -> None:
    row = _row("c0", "MADE_UP_REASON", Identifiability.MODEL_DERIVED, avoided=1.0, missed=0.0)
    assert row.reason_is_canonical is False
    summary, _ = compute_veto_attribution(
        [row], total_suppressed=0, admitted_parents=0
    )
    assert summary.uncanonical_reasons == ("MADE_UP_REASON",)
    assert summary.status == "VETO_ATTRIBUTION_PARTIAL"


# --------------------------------------------------------------------------------------
# the gate-value algebra, with absence that is never read as zero
# --------------------------------------------------------------------------------------


def test_gate_value_algebra_matches_the_rust_formula() -> None:
    rows = [
        _row("c0", "PORTFOLIO_HEAT_EXCEEDED", Identifiability.IDENTIFIED, avoided=120.0, missed=45.0, replayed=-1.5),
        _row("c1", "EXISTING_EXPOSURE_CONFLICT", Identifiability.PARTIALLY_IDENTIFIED, avoided=80.0, missed=20.0, replayed=0.5),
    ]
    summary, _ = compute_veto_attribution(rows, total_suppressed=0, admitted_parents=0)
    assert summary.total_avoided_loss_usdt == pytest.approx(200.0)
    assert summary.total_missed_profit_usdt == pytest.approx(65.0)
    assert summary.net_gate_defensive_value_usdt == pytest.approx(135.0)
    assert summary.gate_defensive_efficiency_ratio == pytest.approx(200.0 / 265.0)
    assert summary.avoided_loss_return_units == pytest.approx(1.5)
    assert summary.missed_profit_return_units == pytest.approx(0.5)
    assert summary.counterfactual_rows == 2
    assert summary.status == "VETO_ATTRIBUTION_MEASURED"
    assert summary.claim == "NO_ECONOMIC_CLAIM"
    assert summary.authority_distribution == {"IDENTIFIED": 1, "PARTIALLY_IDENTIFIED": 1}


def test_an_unmeasured_counterfactual_is_not_summed_as_zero() -> None:
    rows = [
        _row("c0", "PORTFOLIO_HEAT_EXCEEDED", Identifiability.IDENTIFIED, avoided=120.0, missed=45.0),
        _row("c1", "PORTFOLIO_HEAT_EXCEEDED", Identifiability.NOT_IDENTIFIABLE, avoided=None, missed=None),
    ]
    summary, _ = compute_veto_attribution(rows, total_suppressed=0, admitted_parents=0)
    assert summary.total_avoided_loss_usdt is None
    assert summary.total_missed_profit_usdt is None
    assert summary.net_gate_defensive_value_usdt is None
    assert summary.unmeasured_rows == 2
    assert any("unmeasured_rows=1" in factor for factor in summary.missing_factors)
    assert summary.status == "VETO_ATTRIBUTION_PARTIAL"
    assert rows[1].net_gate_value_usdt is None


def test_efficiency_is_missing_not_a_perfect_one_when_nothing_was_measurable() -> None:
    rows = [
        _row("c0", "MIN_NOTIONAL_REJECTED", Identifiability.IDENTIFIED, avoided=0.0, missed=0.0),
        _row("c1", "MIN_NOTIONAL_REJECTED", Identifiability.IDENTIFIED, avoided=0.0, missed=0.0),
    ]
    summary, _ = compute_veto_attribution(rows, total_suppressed=0, admitted_parents=0)
    assert summary.gate_defensive_efficiency_ratio is None
    assert "NO_MEASURABLE_VETO_VALUE" in summary.missing_factors


def test_dedup_win_rates_are_derived_or_missing_never_the_rust_constants() -> None:
    rows = [_row("c0", "PORTFOLIO_HEAT_EXCEEDED", Identifiability.IDENTIFIED, avoided=1.0, missed=1.0)]

    _, dedup_missing = compute_veto_attribution(
        rows, total_suppressed=14_766, admitted_parents=27_881
    )
    assert dedup_missing.parent_win_rate is None
    assert dedup_missing.suppressed_hypothetical_win_rate is None
    assert dedup_missing.signal_redundancy_regret_r is None
    assert dedup_missing.epistemic_authority == Identifiability.NOT_IDENTIFIABLE.value
    assert dedup_missing.status == "DEDUP_SUPPRESSION_AUDIT_PARTIAL"
    assert dedup_missing.claim == "NO_ECONOMIC_CLAIM"

    _, dedup_measured = compute_veto_attribution(
        rows,
        total_suppressed=14_766,
        admitted_parents=27_881,
        parent_win_rate=0.51,
        suppressed_hypothetical_win_rate=0.44,
    )
    assert dedup_measured.signal_redundancy_regret_r == pytest.approx(-0.07)
    assert dedup_measured.status == "DEDUP_SUPPRESSION_AUDIT_MEASURED"


def test_the_rust_modules_fabricated_constants_are_absent_from_this_port() -> None:
    """The Rust veto module ships 0.415 / 0.412 / 0.0 as measured-looking constants.

    They must not reappear: a hardcoded win rate in a report about gate value is exactly
    the class of fabricated input the repository bans. The check reads the module's numeric
    *literals* (docstrings are allowed to name the removed constants and say why).
    """
    source = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "v8_next"
        / "analysis"
        / "veto_attribution.py"
    ).read_text()
    literals = {
        node.value
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Constant) and isinstance(node.value, float)
    }
    assert 0.415 not in literals
    assert 0.412 not in literals
    strings = {
        node.value
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }
    assert "VETO_ATTRIBUTION_CERTIFIED" not in strings, "the Rust status constant is not a status here"


def test_summary_identity_excludes_wall_clock_and_is_content_addressed() -> None:
    rows = [
        _row("c0", "PORTFOLIO_HEAT_EXCEEDED", Identifiability.IDENTIFIED, avoided=10.0, missed=1.0),
    ]
    first, _ = compute_veto_attribution(rows, total_suppressed=3, admitted_parents=4)
    second, _ = compute_veto_attribution(rows, total_suppressed=3, admitted_parents=4)
    assert first.summary_id == second.summary_id
    assert re.fullmatch(r"veto-summary-[0-9a-f]{12}", first.summary_id)

    changed, _ = compute_veto_attribution(rows, total_suppressed=3, admitted_parents=4)  # same
    assert changed.summary_id == first.summary_id
    other, _ = compute_veto_attribution(
        [
            _row(
                "c0", "PORTFOLIO_HEAT_EXCEEDED", Identifiability.IDENTIFIED, avoided=11.0, missed=1.0
            )
        ],
        total_suppressed=3,
        admitted_parents=4,
    )
    assert other.summary_id != first.summary_id


# --------------------------------------------------------------------------------------
# refused-beside-lost: the two projections are disjoint and both totals are printed
# --------------------------------------------------------------------------------------


def test_refused_beside_lost_prints_both_sides_with_totals() -> None:
    refused = {"PORTFOLIO_HEAT_EXCEEDED": 3, "EXISTING_EXPOSURE_CONFLICT": 2}
    lost = {FailureDomain.EXIT.value: 4, FailureDomain.SELECTION.value: 1}
    block = refused_beside_lost(refused, lost)
    assert block["refused_total"] == 5
    assert block["lost_total"] == 5
    assert set(block["refused_by_reason"]).isdisjoint(block["lost_by_domain"])
    assert block["claim"] == "NO_ECONOMIC_CLAIM"


def test_attribution_domains_remain_the_seven_canonical_names() -> None:
    breakdown = FailureAttributionBreakdown()
    for domain in FailureDomain:
        breakdown.record_failure(domain)
    assert breakdown.verify_conservation()
    assert len(list(FailureDomain)) == 7
    assert {d.value for d in FailureDomain} == {
        "DETECTION",
        "REPRESENTATION",
        "RECONCILIATION",
        "SELECTION",
        "ALLOCATION",
        "EXECUTION",
        "EXIT",
    }


# --------------------------------------------------------------------------------------
# the ten-field reconciliation surface
# --------------------------------------------------------------------------------------


class _Outcome:
    """A replay outcome stand-in for the mechanics of the projection (MECHANICS ONLY)."""

    def __init__(self, **overrides: object) -> None:
        self.endpoint = "TARGET"
        self.label_status = "MATURE"
        self.horizon_bars = 7
        self.ambiguous_bars = 1
        self.net_r = 1.2345
        self.entry_price = 100.0
        self.risk_unit_price = 5.0
        self.mae_r = 0.5
        self.mfe_r = 1.5
        self.market_move_r = 2.0
        self.label_available_time = 1_234
        for name, value in overrides.items():
            setattr(self, name, value)


def test_field_set_is_exactly_the_frozen_ten() -> None:
    assert RECONCILE_EXACT_FIELDS == ("endpoint", "label_status", "horizon_bars", "ambiguous_bars")
    assert RECONCILE_FLOAT_FIELDS == (
        "net_r",
        "entry_price",
        "risk_unit_price",
        "mae_r",
        "mfe_r",
        "market_move_r",
    )
    assert len(RECONCILE_EXACT_FIELDS) + len(RECONCILE_FLOAT_FIELDS) == RECONCILE_FIELD_COUNT == 10
    assert RECONCILE_TOLERANCE == 1e-12
    assert RECONCILE_EXCLUDED_FIELDS == ("label_available_time",)


def test_projection_drops_the_excluded_field_and_ignores_identity() -> None:
    surface = reconcile_surface(_Outcome(), candidate_id="cid-a", action_id="act-1")
    assert isinstance(surface, OutcomeSurface)
    assert surface.values_match(
        reconcile_surface(_Outcome(label_available_time=999), candidate_id="other", action_id="other")
    )
    assert surface.mismatched_field(
        reconcile_surface(_Outcome(label_available_time=999), candidate_id="other", action_id="other")
    ) is None


@pytest.mark.parametrize(
    ("field", "value", "expected"),
    [
        ("net_r", 1.2345 + 1e-10, "net_r"),
        ("mae_r", 0.5 + 1e-9, "mae_r"),
        ("endpoint", "STOP", "endpoint"),
        ("horizon_bars", 8, "horizon_bars"),
        ("ambiguous_bars", 2, "ambiguous_bars"),
        ("label_status", "RIGHT_CENSORED", "label_status"),
        ("entry_price", 100.0 + 1e-8, "entry_price"),
        ("risk_unit_price", 5.0 + 1e-8, "risk_unit_price"),
        ("mfe_r", 1.5 + 1e-8, "mfe_r"),
        ("market_move_r", 2.0 + 1e-8, "market_move_r"),
    ],
)
def test_each_field_has_a_discriminating_mismatch(
    field: str, value: object, expected: str
) -> None:
    base = reconcile_surface(_Outcome(), candidate_id="cid", action_id="act")
    other = reconcile_surface(_Outcome(**{field: value}), candidate_id="cid", action_id="act")
    assert base.values_match(other) is False
    assert base.mismatched_field(other) == expected


def test_a_field_within_tolerance_still_matches() -> None:
    base = reconcile_surface(_Outcome(), candidate_id="cid", action_id="act")
    other = reconcile_surface(_Outcome(net_r=1.2345 + 1e-13), candidate_id="cid", action_id="act")
    assert base.values_match(other) is True


def test_a_missing_field_fails_loudly_instead_of_projecting_a_zero() -> None:
    incomplete = _Outcome()
    del incomplete.mfe_r
    with pytest.raises(ValueError, match="missing reconcile fields"):
        reconcile_surface(incomplete, candidate_id="cid", action_id="act")
