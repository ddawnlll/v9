"""4-stage recoverability waterfall — port of v8-core/src/oracle/recoverability.rs.

(O5 challenger chain, ISSUE_AUD005B; invariants I1.)

ANTI-SYNTHETIC DESIGN (deliberate divergence from the Rust function): the Rust
``compute_recoverability_chain(hindsight_ceiling_r, realized_live_r, total_trades)``
derives its intermediate stages from hardcoded ratios (0.45/0.60/0.55/0.70, a floor of 100
trades, and a constant 2460 live trades). Hardcoded measurement fractions are fabrication
under this repo's zero-tolerance rule, so this port takes all four stage values — and
their trade counts — as caller-supplied inputs and contributes only the waterfall
arithmetic plus the monotonicity/subset verification. A stage value that was not measured
must be passed as ``None`` with a named reason; it is never filled from a ratio.

Stages: HINDSIGHT_OPPORTUNITY_CEILING → DECISION_TIME_PIT_RECOVERABLE →
PROMOTABLE_POLICY_BOUNDED → LIVE_SUPPORTED_EXECUTABLE.
Invariants: monotonic subset (counts) and utility ordering
U(Live) ≤ U(Promotable) ≤ U(PITRecoverable) ≤ V*(S_t). Explicitly NO_ECONOMIC_CLAIM.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "NO_ECONOMIC_CLAIM",
    "RecoverabilityStageRecord",
    "RecoverableGapWaterfall",
    "STAGE_AUTHORITIES",
    "STAGE_NAMES",
    "compute_recoverability_chain",
]

#: Canonical stage names in order.
STAGE_NAMES = (
    "HINDSIGHT_OPPORTUNITY_CEILING",
    "DECISION_TIME_PIT_RECOVERABLE",
    "PROMOTABLE_POLICY_BOUNDED",
    "LIVE_SUPPORTED_EXECUTABLE",
)

#: Epistemic authority per stage (the Rust stage authorities, spelled the same).
STAGE_AUTHORITIES = (
    "HINDSIGHT_ORACLE",
    "IDENTIFIED_PIT_FILTRATION",
    "MULTIPLE_TESTING_ADMISSIBLE",
    "EMPIRICAL_EXECUTION_TRUTH",
)

#: The claim every waterfall carries.
NO_ECONOMIC_CLAIM = "NO_ECONOMIC_CLAIM"


@dataclass(frozen=True)
class RecoverabilityStageRecord:
    stage_index: int
    stage_name: str
    theoretical_ceiling_r: float | None
    recoverable_trades_count: int | None
    stage_loss_r: float | None
    stage_loss_fraction_pct: float | None
    epistemic_authority: str
    missing_reason: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "stage_index": self.stage_index,
            "stage_name": self.stage_name,
            "theoretical_ceiling_r": self.theoretical_ceiling_r,
            "recoverable_trades_count": self.recoverable_trades_count,
            "stage_loss_r": self.stage_loss_r,
            "stage_loss_fraction_pct": self.stage_loss_fraction_pct,
            "epistemic_authority": self.epistemic_authority,
            "missing_reason": self.missing_reason,
        }


@dataclass
class RecoverableGapWaterfall:
    waterfall_id: str = ""
    hindsight_ceiling_v_star_r: float | None = None
    pit_information_loss_r: float | None = None
    multiple_testing_promotability_loss_r: float | None = None
    execution_and_friction_loss_r: float | None = None
    realized_live_net_utility_r: float | None = None
    total_unreachable_hindsight_fraction_pct: float | None = None
    actionable_recoverable_alpha_r: float | None = None
    stages: list[RecoverabilityStageRecord] = field(default_factory=list)
    monotonicity_verified: bool = False
    status: str = ""
    claim: str = NO_ECONOMIC_CLAIM

    def bind_identity(self) -> None:
        blob = json.dumps(
            {
                "hindsight": self.hindsight_ceiling_v_star_r,
                "pit": self.stages[1].theoretical_ceiling_r if len(self.stages) > 1 else None,
                "promotable": self.stages[2].theoretical_ceiling_r if len(self.stages) > 2 else None,
                "live": self.realized_live_net_utility_r,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        digest = hashlib.sha1(f"recoverability-waterfall-v1|{blob}".encode()).hexdigest()[:12]
        self.waterfall_id = f"waterfall-{digest}"

    def as_dict(self) -> dict[str, Any]:
        return {
            "waterfall_id": self.waterfall_id,
            "hindsight_ceiling_v_star_r": self.hindsight_ceiling_v_star_r,
            "pit_information_loss_r": self.pit_information_loss_r,
            "multiple_testing_promotability_loss_r": self.multiple_testing_promotability_loss_r,
            "execution_and_friction_loss_r": self.execution_and_friction_loss_r,
            "realized_live_net_utility_r": self.realized_live_net_utility_r,
            "total_unreachable_hindsight_fraction_pct": self.total_unreachable_hindsight_fraction_pct,
            "actionable_recoverable_alpha_r": self.actionable_recoverable_alpha_r,
            "stages": [s.as_dict() for s in self.stages],
            "monotonicity_verified": self.monotonicity_verified,
            "status": self.status,
            "claim": self.claim,
        }


def compute_recoverability_chain(
    hindsight_ceiling_r: float | None,
    pit_recoverable_r: float | None,
    promotable_r: float | None,
    realized_live_r: float | None,
    hindsight_trades: int | None = None,
    pit_trades: int | None = None,
    promotable_trades: int | None = None,
    live_trades: int | None = None,
    missing_reason: str | None = None,
) -> RecoverableGapWaterfall:
    """Build the waterfall from measured stage values; verify the I1 invariants.

    Any stage that was not measured is ``None`` (with ``missing_reason`` naming why) and
    forces status ``RECOVERABILITY_UNMEASURED`` — a missing stage never reads as zero.
    """
    values = (hindsight_ceiling_r, pit_recoverable_r, promotable_r, realized_live_r)
    counts = (hindsight_trades, pit_trades, promotable_trades, live_trades)
    if any(v is None for v in values):
        reason = missing_reason or "UNMEASURED_RECOVERABILITY_STAGE"
        stages = [
            RecoverabilityStageRecord(
                stage_index=i + 1,
                stage_name=name,
                theoretical_ceiling_r=v,
                recoverable_trades_count=c,
                stage_loss_r=None,
                stage_loss_fraction_pct=None,
                epistemic_authority=STAGE_AUTHORITIES[i],
                missing_reason=reason if v is None else None,
            )
            for i, (name, v, c) in enumerate(zip(STAGE_NAMES, values, counts, strict=True))
        ]
        waterfall = RecoverableGapWaterfall(
            hindsight_ceiling_v_star_r=hindsight_ceiling_r,
            realized_live_net_utility_r=realized_live_r,
            stages=stages,
            monotonicity_verified=False,
            status="RECOVERABILITY_UNMEASURED",
        )
        waterfall.bind_identity()
        return waterfall

    assert (
        hindsight_ceiling_r is not None
        and pit_recoverable_r is not None
        and promotable_r is not None
        and realized_live_r is not None
    )
    pit_loss = max(hindsight_ceiling_r - pit_recoverable_r, 0.0)
    promotability_loss = max(pit_recoverable_r - promotable_r, 0.0)
    friction_loss = max(promotable_r - realized_live_r, 0.0)
    total_gap = hindsight_ceiling_r - realized_live_r
    unreachable_pct = (
        ((pit_loss + promotability_loss) / total_gap) * 100.0 if total_gap > 1e-9 else 0.0
    )
    monotonic_values = (
        realized_live_r <= promotable_r
        and promotable_r <= pit_recoverable_r
        and pit_recoverable_r <= hindsight_ceiling_r
    )
    monotonic_counts = all(
        a is None or b is None or a <= b
        for a, b in zip(
            (live_trades, promotable_trades, pit_trades),
            (promotable_trades, pit_trades, hindsight_trades),
            strict=True,
        )
    )
    monotonic = monotonic_values and monotonic_counts

    def fraction(loss: float, base: float) -> float:
        return (loss / base) * 100.0 if base > 1e-9 else 0.0

    losses = (0.0, pit_loss, promotability_loss, friction_loss)
    bases = (hindsight_ceiling_r, hindsight_ceiling_r, pit_recoverable_r, promotable_r)
    stages = [
        RecoverabilityStageRecord(
            stage_index=i + 1,
            stage_name=name,
            theoretical_ceiling_r=value,
            recoverable_trades_count=count,
            stage_loss_r=loss,
            stage_loss_fraction_pct=fraction(loss, base),
            epistemic_authority=STAGE_AUTHORITIES[i],
        )
        for i, (name, value, count, loss, base) in enumerate(
            zip(STAGE_NAMES, values, counts, losses, bases, strict=True)
        )
    ]
    waterfall = RecoverableGapWaterfall(
        hindsight_ceiling_v_star_r=hindsight_ceiling_r,
        pit_information_loss_r=pit_loss,
        multiple_testing_promotability_loss_r=promotability_loss,
        execution_and_friction_loss_r=friction_loss,
        realized_live_net_utility_r=realized_live_r,
        total_unreachable_hindsight_fraction_pct=unreachable_pct,
        actionable_recoverable_alpha_r=pit_recoverable_r - realized_live_r,
        stages=stages,
        monotonicity_verified=monotonic,
        status="RECOVERABILITY_CHAIN_CERTIFIED" if monotonic else "MONOTONICITY_VIOLATION",
    )
    waterfall.bind_identity()
    return waterfall
