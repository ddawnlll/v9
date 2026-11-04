"""Veto counterfactual attribution — port of ``v8-core/src/analysis/veto_attribution.rs``
(issue AUD-006A, F19/F27; readiness spec §6 veto paragraph).

A veto is a trade the system **refused**. Reporting only what was lost biases every
downstream reading toward executed trades, so each refusal is recorded with its canonical
reason and its counterfactual carries a strict epistemic authority tag
(IDENTIFIED | PARTIALLY_IDENTIFIED | MODEL_DERIVED | NOT_IDENTIFIABLE — the ported
``v8_next.oracle`` vocabulary, not a parallel one).

The port keeps the Rust algebra — Avoided Loss, Missed Profit,
``Net Gate Value = Σ avoid − Σ miss`` — and **corrects three fabricated defaults** the
Rust module ships (see the module-level divergence note in ``DIVERGENCES`` below): a
constant win rate pair, a constant ``signal_redundancy_regret_r = 0.0`` and an
efficiency ratio of ``1.0`` produced from no data. A quantity that was not measured stays
``None`` with a named reason; it never becomes a number that reads like a measurement.

Everything here is ``NO_ECONOMIC_CLAIM``: it explains gates, it does not value them.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from v8_next.oracle import Identifiability

CLAIM = "NO_ECONOMIC_CLAIM"

#: Named divergences from the Rust module, each a deliberately un-ported fabrication.
DIVERGENCES: tuple[str, ...] = (
    "parent_win_rate / suppressed_hypothetical_win_rate: Rust publishes the constants "
    "0.415 / 0.412; this port derives them from the supplied outcomes, or reports None "
    "with a named status when they were not measured.",
    "signal_redundancy_regret_r: Rust publishes the constant 0.0; this port derives it "
    "from the two win rates it is defined over, or reports None.",
    "gate_defensive_efficiency_ratio: Rust publishes 1.0 when no veto value is "
    "measurable (a perfect score from no data); this port reports None with the reason "
    "NO_MEASURABLE_VETO_VALUE.",
    "status: Rust's constant 'VETO_ATTRIBUTION_CERTIFIED' asserts an authority a "
    "diagnostic cannot hold; this port states what was actually measured.",
)


class AllocationRejectionReason(StrEnum):
    """Canonical typed rejection taxonomy — port of ``v8-core/src/allocator.rs:16-44``
    (VENUE_AND_CAPITAL_SIMULATION_SPEC §6.2, D-108). The Python owner of the vocabulary,
    so a veto reason is a closed-set member rather than free prose.
    """

    INSUFFICIENT_AVAILABLE_BALANCE = "INSUFFICIENT_AVAILABLE_BALANCE"
    MIN_NOTIONAL_REJECTED = "MIN_NOTIONAL_REJECTED"
    QUANTITY_ROUNDS_TO_ZERO = "QUANTITY_ROUNDS_TO_ZERO"
    MARGIN_LIMIT_EXCEEDED = "MARGIN_LIMIT_EXCEEDED"
    LEVERAGE_CONSTRAINT = "LEVERAGE_CONSTRAINT"
    PORTFOLIO_HEAT_EXCEEDED = "PORTFOLIO_HEAT_EXCEEDED"
    CAPITAL_CONSTRAINT_REJECTION = "CAPITAL_CONSTRAINT_REJECTION"
    ISOLATED_MARGIN_ONLY = "ISOLATED_MARGIN_ONLY"
    INVALID_GEOMETRY = "INVALID_GEOMETRY"


#: Non-allocator veto codes the Rust loop emits with the same standing
#: (`runloop.rs:1535`, `experiment.rs:779`, one active exposure per symbol).
CANDIDATE_REJECTION_REASONS: tuple[str, ...] = ("EXISTING_EXPOSURE_CONFLICT",)

#: The refusal vocabulary of the ported v8-next risk gate — the Python owner of these codes
#: is `v8_next/risk/admission.py:admit` and `v8_next/risk/sizing.py:stop_budget_notional`,
#: which return ``(None, REASON)``. Declared here so a veto row can be validated against the
#: same closed set the gate emits; `tests/test_attribution_veto.py` re-derives this tuple
#: from those two modules' source, so a new gate reason cannot drift away from the ledger.
RISK_GATE_REFUSAL_REASONS: tuple[str, ...] = (
    "UNRECONCILED_ACCOUNT",
    "STALE_OR_FUTURE_ACCOUNT",
    "NO_CAPACITY",
    "BELOW_VENUE_MINIMUM",
    "UNRECONCILED_STOP_EXPOSURE",
    "STALE_OR_FUTURE_STOP_EXPOSURE",
    "CAMPAIGN_CONCURRENCY_LIMIT",
    "INVALID_STOP_GEOMETRY",
    "NO_RISK_CAPITAL",
    "PORTFOLIO_HEAT_EXCEEDED",
)

#: The gate's *admitted* tokens — the complement of the refusal set, named so a reader cannot
#: mistake a green light for a veto code.
ADMITTED_RISK_REASONS: tuple[str, ...] = ("PORTFOLIO_FEASIBLE", "STOP_BUDGET_SIZED")

#: The closed set a veto reason must belong to.
CANONICAL_VETO_REASONS: frozenset[str] = frozenset(
    {member.value for member in AllocationRejectionReason}
    | set(CANDIDATE_REJECTION_REASONS)
    | set(RISK_GATE_REFUSAL_REASONS)
)

UNKNOWN_VETO_REASON = "UNKNOWN_VETO_REASON"


class RefusalStage(StrEnum):
    """Where the funnel refused the opportunity (belief.rs invariant 4: full funnel coverage).

    The stage names are the ported ``DecisionStage`` names (telemetry/span.rs), lowercased
    to the vocabulary the Rust veto path uses in prose — no new ontology.
    """

    WITNESS_OBSERVATION = "witness_observation"
    EVIDENCE_RECONCILIATION = "evidence_reconciliation"
    SELECTIVE_UTILITY = "selective_utility"
    PORTFOLIO_FEASIBILITY = "portfolio_feasibility"
    CAMPAIGN_ADMISSION = "campaign_admission"


@dataclass(frozen=True)
class RefusedDecision:
    """A campaign or decision the system did not take, with the reason it was not taken.

    ``avoided_loss_usdt`` / ``missed_profit_usdt`` are the counterfactual magnitudes. They
    stay ``None`` when the run could not measure them — a refused trade whose outcome is
    unknown must not contribute a ``0`` to the gate's defensive value.
    """

    candidate_id: str
    expert_id: str
    veto_reason: str
    stage: RefusalStage
    authority_status: Identifiability
    avoided_loss_usdt: float | None = None
    missed_profit_usdt: float | None = None
    hypothetical_mfe_r: float | None = None
    hypothetical_mae_r: float | None = None
    #: The replayed counterfactual in **return units** (not USDT, not an R-multiple): the
    #: decision plane replays every signal, so a refused signal's own outcome is a measured
    #: counterfactual, while its *money* value would need a position size the decision plane
    #: does not have. Declared divergence: Rust assumes the USDT value exists.
    hypothetical_net_return: float | None = None

    @property
    def reason_is_canonical(self) -> bool:
        return self.veto_reason in CANONICAL_VETO_REASONS

    @property
    def net_gate_value_usdt(self) -> float | None:
        if self.avoided_loss_usdt is None or self.missed_profit_usdt is None:
            return None
        return self.avoided_loss_usdt - self.missed_profit_usdt

    def as_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "expert_id": self.expert_id,
            "veto_reason": self.veto_reason,
            "reason_is_canonical": self.reason_is_canonical,
            "stage": self.stage.value,
            "authority_status": self.authority_status.value,
            "avoided_loss_usdt": self.avoided_loss_usdt,
            "missed_profit_usdt": self.missed_profit_usdt,
            "net_gate_value_usdt": self.net_gate_value_usdt,
            "hypothetical_mfe_r": self.hypothetical_mfe_r,
            "hypothetical_mae_r": self.hypothetical_mae_r,
            "hypothetical_net_return": self.hypothetical_net_return,
            "claim": CLAIM,
        }


#: The Rust name for the row type; kept so a reader who knows the Rust module finds it.
VetoAttributionRow = RefusedDecision


@dataclass
class VetoAttributionSummary:
    """Gate defensive efficiency over the recorded refusals."""

    summary_id: str
    total_candidates_vetoed: int
    total_avoided_loss_usdt: float | None
    total_missed_profit_usdt: float | None
    net_gate_defensive_value_usdt: float | None
    gate_defensive_efficiency_ratio: float | None
    authority_distribution: dict[str, int]
    status: str
    claim: str = CLAIM
    unmeasured_rows: int = 0
    uncanonical_reasons: tuple[str, ...] = ()
    missing_factors: tuple[str, ...] = ()
    #: Counterfactual measured in return units over the refused signals that were replayed.
    avoided_loss_return_units: float | None = None
    missed_profit_return_units: float | None = None
    counterfactual_rows: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "summary_id": self.summary_id,
            "total_candidates_vetoed": self.total_candidates_vetoed,
            "total_avoided_loss_usdt": self.total_avoided_loss_usdt,
            "total_missed_profit_usdt": self.total_missed_profit_usdt,
            "net_gate_defensive_value_usdt": self.net_gate_defensive_value_usdt,
            "gate_defensive_efficiency_ratio": self.gate_defensive_efficiency_ratio,
            "avoided_loss_return_units": self.avoided_loss_return_units,
            "missed_profit_return_units": self.missed_profit_return_units,
            "counterfactual_rows": self.counterfactual_rows,
            "authority_distribution": dict(sorted(self.authority_distribution.items())),
            "unmeasured_rows": self.unmeasured_rows,
            "uncanonical_reasons": list(self.uncanonical_reasons),
            "missing_factors": list(self.missing_factors),
            "status": self.status,
            "claim": self.claim,
        }


@dataclass
class DedupRegretReport:
    """Deduplication suppression audit: what the parent filter threw away."""

    report_id: str
    total_suppressed_duplicates: int
    admitted_parent_candidates: int
    parent_win_rate: float | None
    suppressed_hypothetical_win_rate: float | None
    signal_redundancy_regret_r: float | None
    epistemic_authority: str
    status: str
    claim: str = CLAIM
    missing_factors: tuple[str, ...] = field(default_factory=tuple)

    def as_dict(self) -> dict[str, Any]:
        return {
            "report_id": self.report_id,
            "total_suppressed_duplicates": self.total_suppressed_duplicates,
            "admitted_parent_candidates": self.admitted_parent_candidates,
            "parent_win_rate": self.parent_win_rate,
            "suppressed_hypothetical_win_rate": self.suppressed_hypothetical_win_rate,
            "signal_redundancy_regret_r": self.signal_redundancy_regret_r,
            "epistemic_authority": self.epistemic_authority,
            "missing_factors": list(self.missing_factors),
            "status": self.status,
            "claim": self.claim,
        }


def _identity(prefix: str, payload: Mapping[str, Any]) -> str:
    """Content identity: sha256 over canonical JSON. Wall clock never enters."""
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return f"{prefix}-{hashlib.sha256(blob.encode()).hexdigest()[:12]}"


def _sum_or_missing(
    rows: Iterable[RefusedDecision], attribute: str
) -> tuple[float | None, int]:
    """Sum a counterfactual column, refusing to read an unmeasured row as ``0``."""
    total = 0.0
    unmeasured = 0
    for row in rows:
        value = getattr(row, attribute)
        if value is None:
            unmeasured += 1
            continue
        total += value
    if unmeasured:
        return None, unmeasured
    return total, 0


def compute_veto_attribution(
    refused: Iterable[RefusedDecision],
    *,
    total_suppressed: int,
    admitted_parents: int,
    parent_win_rate: float | None = None,
    suppressed_hypothetical_win_rate: float | None = None,
) -> tuple[VetoAttributionSummary, DedupRegretReport]:
    """Aggregate the recorded refusals into the gate-value summary and the dedup report.

    Mirrors ``veto_attribution.rs:compute_veto_attribution`` with the fabrications removed:
    a column whose rows were not measured yields ``None`` plus the count that was missing,
    instead of a total that silently treated absence as zero.
    """
    rows = tuple(refused)
    total_avoided, unmeasured_avoided = _sum_or_missing(rows, "avoided_loss_usdt")
    total_missed, unmeasured_missed = _sum_or_missing(rows, "missed_profit_usdt")

    authority_distribution: dict[str, int] = {}
    for row in rows:
        authority_distribution[row.authority_status.value] = (
            authority_distribution.get(row.authority_status.value, 0) + 1
        )

    missing_factors: list[str] = []
    if unmeasured_avoided:
        missing_factors.append(f"avoided_loss_usdt_unmeasured_rows={unmeasured_avoided}")
    if unmeasured_missed:
        missing_factors.append(f"missed_profit_usdt_unmeasured_rows={unmeasured_missed}")

    # Return-unit counterfactual: measurable wherever the refused signal was replayed.
    replayed = [row for row in rows if row.hypothetical_net_return is not None]
    if replayed:
        avoided_return = sum(
            -row.hypothetical_net_return
            for row in replayed
            if row.hypothetical_net_return is not None and row.hypothetical_net_return < 0
        )
        missed_return = sum(
            row.hypothetical_net_return
            for row in replayed
            if row.hypothetical_net_return is not None and row.hypothetical_net_return >= 0
        )
    else:
        avoided_return = None
        missed_return = None
        missing_factors.append("NO_REPLAYED_REFUSAL_COUNTERFACTUAL")

    if total_avoided is None and rows and not missing_factors:
        missing_factors.append("NO_VETO_VALUE_SOURCE")

    if total_avoided is None or total_missed is None:
        net_value: float | None = None
        efficiency: float | None = None
    else:
        net_value = total_avoided - total_missed
        denominator = total_avoided + total_missed
        if denominator > 1e-9:
            efficiency = total_avoided / denominator
        else:
            # No measurable veto value at all: exhaustive-sounding 1.0 would be fabricated.
            efficiency = None
            missing_factors.append("NO_MEASURABLE_VETO_VALUE")

    uncanonical = tuple(
        sorted({row.veto_reason for row in rows if not row.reason_is_canonical})
    )
    status = (
        "VETO_ATTRIBUTION_MEASURED"
        if not missing_factors and not uncanonical
        else "VETO_ATTRIBUTION_PARTIAL"
    )

    summary = VetoAttributionSummary(
        summary_id=_identity(
            "veto-summary",
            {
                "n": len(rows),
                "avoided": total_avoided,
                "missed": total_missed,
                "net": net_value,
                "avoided_return": avoided_return,
                "missed_return": missed_return,
                "authority": dict(sorted(authority_distribution.items())),
            },
        ),
        total_candidates_vetoed=len(rows),
        total_avoided_loss_usdt=total_avoided,
        total_missed_profit_usdt=total_missed,
        net_gate_defensive_value_usdt=net_value,
        gate_defensive_efficiency_ratio=efficiency,
        authority_distribution=authority_distribution,
        status=status,
        claim=CLAIM,
        unmeasured_rows=unmeasured_avoided + unmeasured_missed,
        uncanonical_reasons=uncanonical,
        missing_factors=tuple(dict.fromkeys(missing_factors)),
        avoided_loss_return_units=avoided_return,
        missed_profit_return_units=missed_return,
        counterfactual_rows=len(replayed),
    )

    dedup_missing: list[str] = []
    if parent_win_rate is None:
        dedup_missing.append("parent_win_rate_not_measured")
    if suppressed_hypothetical_win_rate is None:
        dedup_missing.append("suppressed_hypothetical_win_rate_not_measured")
    if parent_win_rate is None or suppressed_hypothetical_win_rate is None:
        redundancy: float | None = None
    else:
        redundancy = suppressed_hypothetical_win_rate - parent_win_rate

    dedup = DedupRegretReport(
        report_id=_identity(
            "dedup-regret",
            {
                "total_suppressed": total_suppressed,
                "admitted_parents": admitted_parents,
                "parent_win_rate": parent_win_rate,
                "suppressed_win_rate": suppressed_hypothetical_win_rate,
            },
        ),
        total_suppressed_duplicates=total_suppressed,
        admitted_parent_candidates=admitted_parents,
        parent_win_rate=parent_win_rate,
        suppressed_hypothetical_win_rate=suppressed_hypothetical_win_rate,
        signal_redundancy_regret_r=redundancy,
        epistemic_authority=(
            Identifiability.PARTIALLY_IDENTIFIED.value
            if redundancy is not None
            else Identifiability.NOT_IDENTIFIABLE.value
        ),
        status=(
            "DEDUP_SUPPRESSION_AUDIT_MEASURED" if not dedup_missing else "DEDUP_SUPPRESSION_AUDIT_PARTIAL"
        ),
        claim=CLAIM,
        missing_factors=tuple(dedup_missing),
    )
    return summary, dedup


def refused_beside_lost(
    refused_by_reason: Mapping[str, int], lost_by_domain: Mapping[str, int]
) -> dict[str, Any]:
    """The P2 report column: refused-what-and-why beside lost-what-and-why.

    Both sides are counts over closed vocabularies (canonical veto reasons; the seven
    failure domains), and both totals are printed so a reader can see that the refused set
    and the executed set are disjoint projections of the same funnel.
    """
    return {
        "refused_by_reason": dict(sorted(refused_by_reason.items())),
        "refused_total": sum(refused_by_reason.values()),
        "lost_by_domain": dict(sorted(lost_by_domain.items())),
        "lost_total": sum(lost_by_domain.values()),
        "claim": CLAIM,
    }
