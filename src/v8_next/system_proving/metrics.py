"""14-metric system robustness vector — port of v8-core/src/system_proving/metrics.rs
(D-147, D-149, M3).

Field names are the Rust ones. The Rust invariant is carried over verbatim: the cashflow
discrepancy must be zero for valid double-entry reconciliation.

#457: a published field name is a claim about what was measured. Every field of the vector is
one of three declared things, and which one it is travels with the vector:

* ``MEASURED``   — a function of its own named inputs through its own expression; no two
  measured fields share one expression;
* ``DERIVED``    — a declared restatement of exactly one other named field (``derived_from``),
  never a hidden copy;
* ``UNMEASURED`` — ``null`` plus a reason naming the input this producer does not carry.

No field is a literal constant, another field's copy, or another quantity's clamp. The
per-field basis is published beside the vector (:meth:`SystemRobustnessVector.field_reports`),
so a reader of the artifact reads an absent measurement as an absent measurement instead of a
pass-shaped number.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

DOUBLE_ENTRY_EPSILON = 1e-6

MEASURED = "MEASURED"
DERIVED = "DERIVED"
UNMEASURED = "UNMEASURED"

#: The 14 fields, in the Rust struct's own order. The Rust contract: names and order.
FIELD_NAMES: tuple[str, ...] = (
    "scenario_failure_fraction",
    "tail_capture_efficiency",
    "friction_retention_ratio",
    "recovery_horizon_bars",
    "max_adverse_excursion_pct",
    "ruin_margin_pct",
    "slippage_fragility_score",
    "turnover_efficiency",
    "capital_utilization_pct",
    "funding_drag_ratio",
    "regime_stability_score",
    "habitat_selectivity_score",
    "expert_displacement_rate",
    "cashflow_discrepancy_usdt",
)

#: Basis of the drawdown/ruin pair. The uncompounded sum of campaign net returns is the ONLY
#: path this producer carries: an uncompounded sum is not a compounded equity curve, so the
#: numbers below are in return-sum units and never percentages of a capital base.
DRAWDOWN_BASIS: dict[str, Any] = {
    "basis": "uncompounded_sum_of_campaign_returns",
    "unit": "percent of that path's own running peak, in return-sum units (not capital percent)",
    "fields": ["max_adverse_excursion_pct", "ruin_margin_pct"],
    "ruin_margin_relation": "100 - max_adverse_excursion_pct (DERIVED, declared, unclamped)",
}


@dataclass(frozen=True)
class FieldBasis:
    """What one published field is, and which inputs it is allowed to read."""

    status: str
    statement: str
    unit: str
    inputs: tuple[str, ...] = ()
    derived_from: str | None = None
    #: True for a field this producer never measures of its own accord: the declaration is
    #: ``UNMEASURED`` (a published number would be a pass-shaped guess) and only a value the
    #: caller supplies — its own measurement — is published as ``MEASURED``.
    measured_when_supplied: bool = False


#: Per-field basis. Every entry declares either the field's own expression over named inputs,
#: or the single named field it restates. No two MEASURED entries share a (statement, inputs)
#: signature — pinned by tests/test_robustness_vector_invariants_457.py.
FIELD_BASIS: dict[str, FieldBasis] = {
    "scenario_failure_fraction": FieldBasis(
        status=MEASURED,
        statement="failures / campaigns",
        unit="fraction of campaigns charged to one of the seven disjoint failure domains",
        inputs=("failures", "campaigns"),
    ),
    "tail_capture_efficiency": FieldBasis(
        status=UNMEASURED,
        statement="share of the campaign return distribution's tail that the family captured",
        unit="fraction of a declared tail",
    ),
    "friction_retention_ratio": FieldBasis(
        status=MEASURED,
        statement="(gross_return_sum - fee_cost_sum - funding_cost_sum) / gross_return_sum",
        unit="fraction of gross return retained after measured fees and funding (signed)",
        inputs=("gross_return_sum", "fee_cost_sum", "funding_cost_sum"),
    ),
    "recovery_horizon_bars": FieldBasis(
        status=MEASURED,
        statement=(
            "tape bars from the trough of the deepest drawdown of the uncompounded "
            "campaign-return path to the first point at which that path re-attains the peak "
            "it fell from"
        ),
        unit="tape bars",
        inputs=("campaign_net_returns", "campaign_bars_held"),
    ),
    "max_adverse_excursion_pct": FieldBasis(
        status=MEASURED,
        statement=(
            "100 * max(running peak - cumulative campaign net return) over the uncompounded "
            "sum of campaign net returns, or, when no path is supplied, the caller's own "
            "max_drawdown_pct measurement"
        ),
        unit="percent of the path's own running peak, in return-sum units",
        inputs=("campaign_net_returns", "max_drawdown_pct"),
    ),
    "ruin_margin_pct": FieldBasis(
        status=DERIVED,
        statement=(
            "100 - max_adverse_excursion_pct: the same drawdown restated as a margin, in the "
            "same return-sum units; not an independent measurement and not clamped"
        ),
        unit="percent of the path's own running peak, in return-sum units",
        inputs=("max_adverse_excursion_pct",),
        derived_from="max_adverse_excursion_pct",
    ),
    "slippage_fragility_score": FieldBasis(
        status=MEASURED,
        statement=(
            "(measured_net - net_with_modelled_slippage_sum) / abs(measured_net), where "
            "measured_net = gross_return_sum - fee_cost_sum - funding_cost_sum"
        ),
        unit="fraction of the measured net return consumed by the declared slippage model",
        inputs=(
            "gross_return_sum",
            "fee_cost_sum",
            "funding_cost_sum",
            "net_with_modelled_slippage_sum",
        ),
    ),
    "turnover_efficiency": FieldBasis(
        status=UNMEASURED,
        statement="traded notional over the capital that carried it",
        unit="notional per unit of capital",
    ),
    "capital_utilization_pct": FieldBasis(
        status=UNMEASURED,
        statement="share of a capital base actually deployed as position size",
        unit="percent of a capital base",
    ),
    "funding_drag_ratio": FieldBasis(
        status=MEASURED,
        statement="abs(funding_cost_sum) / max(abs(gross_return_sum), DOUBLE_ENTRY_EPSILON)",
        unit="fraction of gross return paid as funding (saturating at 1.0)",
        inputs=("funding_cost_sum", "gross_return_sum"),
    ),
    "regime_stability_score": FieldBasis(
        status=DERIVED,
        statement=(
            "1 - scenario_failure_fraction: the share of campaigns not charged to a failure "
            "domain, restated as a stability score; a declared derivation, not an independent "
            "measurement of regime segmentation"
        ),
        unit="fraction of campaigns",
        inputs=("scenario_failure_fraction",),
        derived_from="scenario_failure_fraction",
    ),
    "habitat_selectivity_score": FieldBasis(
        status=MEASURED,
        statement="campaigns / bars, where bars is the decision frames the tape offered",
        unit="entries per decision frame — how selectively the family fired",
        inputs=("campaigns", "bars"),
    ),
    "expert_displacement_rate": FieldBasis(
        status=UNMEASURED,
        statement="share of campaigns an expert baseline would have won that the family took",
        unit="fraction of campaigns",
    ),
    "cashflow_discrepancy_usdt": FieldBasis(
        status=UNMEASURED,
        statement=(
            "the double-entry residual the caller's books reconcile, passed in as an input; "
            "this producer keeps no second ledger and measures no residual of its own, so the "
            "field is UNMEASURED unless a residual is supplied"
        ),
        unit="USDT",
        inputs=("cashflow_discrepancy_usdt",),
        measured_when_supplied=True,
    ),
}

#: Why a field is published as ``null``. Absence is never silent: every null carries the name
#: of the input this producer does not have (or the condition its denominator failed).
FIELD_ABSENCE_REASON: dict[str, str] = {
    "scenario_failure_fraction": "no campaigns were run: a failure fraction has no denominator",
    "tail_capture_efficiency": (
        "no tail cut is declared over a per-campaign return distribution in this producer, so "
        "a tail capture share would be a pass-shaped guess"
    ),
    "friction_retention_ratio": (
        "no positive gross return was supplied: a retention fraction has no denominator"
    ),
    "recovery_horizon_bars": (
        "the uncompounded campaign-return path never re-attains the peak it fell from inside "
        "the tape, or no per-campaign path was supplied: there is no recovery to measure"
    ),
    "max_adverse_excursion_pct": (
        "neither a per-campaign return path nor a max_drawdown_pct measurement was supplied"
    ),
    "ruin_margin_pct": (
        "the drawdown it restates (max_adverse_excursion_pct) is not measured, so the margin "
        "cannot be derived"
    ),
    "slippage_fragility_score": (
        "no modelled-slippage net total was supplied, or the measured net return is zero: a "
        "degradation share has no denominator"
    ),
    "turnover_efficiency": (
        "this producer holds no traded notional and no capital base (a returns-only decision "
        "plane), so turnover would be a pass-shaped guess"
    ),
    "capital_utilization_pct": (
        "no position sizing / no capital basis in this producer: a utilization share cannot be "
        "measured from a fee column"
    ),
    "funding_drag_ratio": "no gross return was supplied: a funding drag fraction has no denominator",
    "regime_stability_score": (
        "the failure fraction it restates (scenario_failure_fraction) is not measured"
    ),
    "habitat_selectivity_score": (
        "no decision-frame count was supplied: a selection rate has no denominator"
    ),
    "expert_displacement_rate": (
        "this producer runs no expert baseline and carries no displaced-expert series, so the "
        "rate would be a pass-shaped zero"
    ),
    "cashflow_discrepancy_usdt": "no double-entry residual was supplied to reconcile",
}


@dataclass(frozen=True)
class SystemRobustnessVector:
    scenario_failure_fraction: float | None
    tail_capture_efficiency: float | None
    friction_retention_ratio: float | None
    recovery_horizon_bars: int | None
    max_adverse_excursion_pct: float | None
    ruin_margin_pct: float | None
    slippage_fragility_score: float | None
    turnover_efficiency: float | None
    capital_utilization_pct: float | None
    funding_drag_ratio: float | None
    regime_stability_score: float | None
    habitat_selectivity_score: float | None
    expert_displacement_rate: float | None
    cashflow_discrepancy_usdt: float | None

    def is_double_entry_reconciled(self) -> bool | None:
        """The double-entry verdict, or ``None`` when no residual was measured.

        The invariant (``|residual| < DOUBLE_ENTRY_EPSILON``) is a statement about a measured
        residual. With no residual supplied there is nothing to reconcile, so the verdict is
        absent — never the pass-shaped ``True`` a ``0.0`` default would mint for a producer
        that never kept the second ledger.
        """
        if self.cashflow_discrepancy_usdt is None:
            return None
        return abs(self.cashflow_discrepancy_usdt) < DOUBLE_ENTRY_EPSILON

    def as_dict(self) -> dict[str, float | int | None]:
        return {
            field: getattr(self, field)
            for field in FIELD_NAMES
        }

    def unmeasured_fields(self) -> list[dict[str, str]]:
        """Every field published as ``null``, with the reason it is absent."""
        return [
            {"field": field, "status": UNMEASURED, "reason": FIELD_ABSENCE_REASON[field]}
            for field in FIELD_NAMES
            if self.as_dict()[field] is None
        ]

    def field_reports(self) -> dict[str, dict[str, Any]]:
        """The vector's provenance: what each published field is, and what a null lacks.

        The per-field basis is a property of the field, not of the run (``FIELD_BASIS``); the
        value and, for a null, the named reason are properties of this run. A field whose
        basis is ``MEASURED`` or ``DERIVED`` but whose value is absent here is reported as
        ``UNMEASURED`` for this run, never as a zero. A field this producer does not measure
        on its own (``measured_when_supplied``) is ``UNMEASURED`` unless the caller supplied
        its measurement, so no run can inherit a ``MEASURED`` claim it did not earn.
        """
        published = self.as_dict()
        reports: dict[str, dict[str, Any]] = {}
        for name in FIELD_NAMES:
            basis = FIELD_BASIS[name]
            value = published[name]
            if value is None:
                status = UNMEASURED
            elif basis.measured_when_supplied:
                status = MEASURED
            else:
                status = basis.status
            report: dict[str, Any] = {
                "status": status,
                "basis": basis.statement,
                "unit": basis.unit,
                "inputs": list(basis.inputs),
                "value": value,
            }
            if basis.derived_from is not None:
                report["derived_from"] = basis.derived_from
            if value is None:
                report["reason"] = FIELD_ABSENCE_REASON[name]
            reports[name] = report
        return reports


def _drawdown_and_recovery(
    net_returns: Sequence[float], bars_held: Sequence[int] | None
) -> tuple[float, int | None]:
    """Deepest drawdown of the uncompounded campaign-return path, and its recovery horizon.

    Named basis: ``uncompounded_sum_of_campaign_returns``. The path is the running sum of the
    campaign net returns — never a compounded equity curve, never a share of capital — so the
    drawdown is measured against the path's own running peak (``peak > 0`` gate, as the Rust
    side's runner does). The recovery horizon is the tape bars between the trough of that
    deepest drawdown and the first point at which the path re-attains the peak it fell from;
    ``None`` when the tape ends before the path recovers.
    """
    held = bars_held if bars_held is not None else ()
    equity = 0.0
    peak = 0.0
    max_dd = 0.0
    trough = -1
    trough_peak = 0.0
    elapsed = 0
    offsets: list[int] = []
    path: list[float] = []
    for index, value in enumerate(net_returns):
        offsets.append(elapsed)
        equity += float(value)
        path.append(equity)
        if equity >= peak:
            peak = equity
        drawdown = (peak - equity) if peak > 0 else 0.0
        if drawdown > max_dd:
            max_dd = drawdown
            trough = index
            trough_peak = peak
        elapsed += max(0, int(held[index])) if index < len(held) else 1
    if max_dd <= DOUBLE_ENTRY_EPSILON:
        # nothing to recover from: a zero drawdown has a zero horizon by construction
        return 0.0, 0
    for index in range(trough + 1, len(path)):
        if path[index] >= trough_peak:
            return max_dd, offsets[index] - offsets[trough]
    return max_dd, None


def metrics_from_campaigns(
    *,
    campaigns: int,
    failures: int,
    gross_return_sum: float,
    fee_cost_sum: float,
    funding_cost_sum: float,
    max_drawdown_pct: float | None = None,
    bars: int,
    cashflow_discrepancy_usdt: float | None = None,
    campaign_net_returns: Sequence[float] | None = None,
    campaign_bars_held: Sequence[int] | None = None,
    net_with_modelled_slippage_sum: float | None = None,
) -> SystemRobustnessVector:
    """The vector a run actually measured, from the totals and columns the run carries.

    Each field reads only its own named inputs (``FIELD_BASIS``). A field whose quantity this
    producer cannot measure — a capital base, a traded notional, a declared tail cut, an expert
    baseline — is published as ``null`` with a named reason rather than as a pass-shaped number.
    The drawdown/ruin pair is read off the uncompounded campaign-return path
    (``DRAWDOWN_BASIS``) when the producer supplies it, and off the caller's own
    ``max_drawdown_pct`` measurement otherwise; ``ruin_margin_pct`` is a declared restatement of
    ``max_adverse_excursion_pct`` and is never clamped.

    The double-entry residual is the caller's own measurement: this producer keeps no second
    ledger, so ``cashflow_discrepancy_usdt`` has NO default residual. Without one it is
    published as ``null`` with its named absence reason and the reconciliation verdict is
    absent, instead of a ``0.0`` that would mint ``double_entry_reconciled: true``.
    """
    total = float(campaigns)
    fails = float(failures)
    fail_fraction = fails / total if total > 0 else None
    measured_net = gross_return_sum - fee_cost_sum - funding_cost_sum
    retention = measured_net / gross_return_sum if gross_return_sum > 0 else None

    drawdown_pct: float | None = None
    recovery_bars: int | None = None
    if campaign_net_returns is not None and len(campaign_net_returns) > 0:
        drawdown, recovery_bars = _drawdown_and_recovery(campaign_net_returns, campaign_bars_held)
        drawdown_pct = 100.0 * drawdown
    elif max_drawdown_pct is not None:
        drawdown_pct = float(max_drawdown_pct)

    slippage_fragility: float | None = None
    if (
        net_with_modelled_slippage_sum is not None
        and abs(measured_net) > DOUBLE_ENTRY_EPSILON
    ):
        slippage_fragility = (
            measured_net - net_with_modelled_slippage_sum
        ) / abs(measured_net)

    return SystemRobustnessVector(
        scenario_failure_fraction=fail_fraction,
        tail_capture_efficiency=None,
        friction_retention_ratio=retention,
        recovery_horizon_bars=recovery_bars,
        max_adverse_excursion_pct=drawdown_pct,
        ruin_margin_pct=100.0 - drawdown_pct if drawdown_pct is not None else None,
        slippage_fragility_score=slippage_fragility,
        turnover_efficiency=None,
        capital_utilization_pct=None,
        funding_drag_ratio=min(
            1.0, abs(funding_cost_sum) / max(abs(gross_return_sum), DOUBLE_ENTRY_EPSILON)
        ),
        regime_stability_score=1.0 - fail_fraction if fail_fraction is not None else None,
        habitat_selectivity_score=min(1.0, total / max(1, bars)),
        expert_displacement_rate=None,
        cashflow_discrepancy_usdt=cashflow_discrepancy_usdt,
    )
