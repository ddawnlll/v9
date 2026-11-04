"""System proving ground receipt — port of v8-core/src/system_proving/receipt.rs.

The digest covers the world/policy identity, the trade and campaign counts and the timestamp,
exactly as the Rust receipt does; the receipt id is ``spg-receipt-<first 16 hex>``.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from v8_next.system_proving.attribution import FailureAttributionBreakdown
from v8_next.system_proving.metrics import SystemRobustnessVector


@dataclass(frozen=True)
class SystemProvingGroundReceipt:
    receipt_id: str
    world_id: str
    policy_id: str
    total_trades: int
    total_campaigns: int
    metrics: SystemRobustnessVector
    attribution: FailureAttributionBreakdown
    exercises_full_pipeline: bool
    evaluated_at_timestamp_ns: int
    receipt_digest: str

    @classmethod
    def new(
        cls,
        *,
        world_id: str,
        policy_id: str,
        total_trades: int,
        total_campaigns: int,
        metrics: SystemRobustnessVector,
        attribution: FailureAttributionBreakdown,
        exercises_full_pipeline: bool,
        timestamp_ns: int,
    ) -> SystemProvingGroundReceipt:
        digest = hashlib.sha256(
            (
                f"{world_id}|{policy_id}|{total_trades}|{total_campaigns}|{timestamp_ns}"
            ).encode()
        ).hexdigest()
        return cls(
            receipt_id=f"spg-receipt-{digest[:16]}",
            world_id=world_id,
            policy_id=policy_id,
            total_trades=total_trades,
            total_campaigns=total_campaigns,
            metrics=metrics,
            attribution=attribution,
            exercises_full_pipeline=exercises_full_pipeline,
            evaluated_at_timestamp_ns=timestamp_ns,
            receipt_digest=digest,
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "receipt_id": self.receipt_id,
            "world_id": self.world_id,
            "policy_id": self.policy_id,
            "total_trades": self.total_trades,
            "total_campaigns": self.total_campaigns,
            "metrics": self.metrics.as_dict(),
            "attribution": self.attribution.as_dict(),
            "exercises_full_pipeline": self.exercises_full_pipeline,
            "evaluated_at_timestamp_ns": self.evaluated_at_timestamp_ns,
            "receipt_digest": self.receipt_digest,
        }
