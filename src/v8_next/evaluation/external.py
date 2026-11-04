"""External disagreement detector — thin port of v8-core/src/benchmark/external.rs detector (D-153 §§2.6, 4.6).

The deleted adapters stay deleted here too: no hardcoded vector pairs, no
tolerance-based comparison (PARITY_AND_IDENTITY_SPEC §3 forbids it), no
absence-as-zero. What survives is the real invariant layer over the ported
``parity`` receipts: identity must verify, outcome must be an exact match,
``DATA_BLOCKED`` is an error (never a silent pass), unsupported semantics are
named findings. All findings carry NO_ECONOMIC_CLAIM.
"""

from __future__ import annotations

from dataclasses import dataclass

from v8_next.evaluation.parity import ParityOutcomeKind, ParityReceipt

__all__ = ["DisagreementDetector", "DisagreementFinding"]


@dataclass(frozen=True)
class DisagreementFinding:
    finding: str
    detail: str
    claim: str = "NO_ECONOMIC_CLAIM"

    def as_dict(self) -> dict[str, object]:
        return {"finding": self.finding, "detail": self.detail, "claim": self.claim}


class DisagreementDetector:
    @staticmethod
    def assert_parity(receipt: ParityReceipt) -> None:
        if receipt.outcome is ParityOutcomeKind.DATA_BLOCKED:
            raise ValueError("PARITY_DATA_BLOCKED: adapter read no artifacts; parity not established")
        if not receipt.is_agreement():
            raise ValueError(f"PARITY_DIVERGED: outcome is {receipt.outcome.value}")

    @staticmethod
    def check_order_semantics(supported: tuple[str, ...], requested: str) -> DisagreementFinding | None:
        if requested in supported:
            return None
        return DisagreementFinding(
            finding="UNSUPPORTED_ORDER_SEMANTICS",
            detail=f"order semantic {requested!r} is outside the declared mapping {list(supported)!r}",
        )
