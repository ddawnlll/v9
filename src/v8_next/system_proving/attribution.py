"""Failure-domain attribution algebra — port of v8-core/src/system_proving/attribution.rs
(D-147, D-149, M3).

Every trade or campaign failure is classified into exactly one of **7 disjoint** pipeline
domains, and the conservation invariant requires the domain counts to sum to the total number
of failures: no double counting, no unattributed loss. The domain names are the Rust ones, not
a local renaming, so a report from either system reads the same.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class FailureDomain(StrEnum):
    """The 7 disjoint pipeline failure domains."""

    DETECTION = "DETECTION"
    REPRESENTATION = "REPRESENTATION"
    RECONCILIATION = "RECONCILIATION"
    SELECTION = "SELECTION"
    ALLOCATION = "ALLOCATION"
    EXECUTION = "EXECUTION"
    EXIT = "EXIT"


@dataclass
class FailureAttributionBreakdown:
    counts_by_domain: dict[FailureDomain, int] = field(default_factory=dict)
    total_failures: int = 0

    def record_failure(self, domain: FailureDomain) -> None:
        self.counts_by_domain[domain] = self.counts_by_domain.get(domain, 0) + 1
        self.total_failures += 1

    def verify_conservation(self) -> bool:
        """Invariant: the domain counts must equal the total failures exactly."""
        return sum(self.counts_by_domain.values()) == self.total_failures

    def as_dict(self) -> dict[str, object]:
        return {
            "counts_by_domain": {
                domain.value: count for domain, count in sorted(self.counts_by_domain.items())
            },
            "total_failures": self.total_failures,
            "conservation_verified": self.verify_conservation(),
        }


def classify_exit_failure(*, exit_kind: str, net_return: float, has_bracket: bool) -> FailureDomain:
    """The declared classifier used by this port.

    The Rust runner charged every losing trade to ``Exit`` because its synthetic loop knew
    nothing else about the trade. This port classifies from what a real run actually reports,
    and the rules are stated so they can be argued with:

    * a loss on a campaign that carried no bracket is an ``EXIT`` failure - the exit contract
      is what could not protect it;
    * a loss that exited on its target or expiry is ``SELECTION`` - the selection chose a
      direction the market did not follow;
    * a loss stopped out with a live bracket is ``EXECUTION`` - the entry/exit machinery had
      the protection and still lost, which is the execution model's business.
    """
    if net_return >= 0:
        raise ValueError("classify_exit_failure is only defined for losing campaigns")
    if not has_bracket:
        return FailureDomain.EXIT
    if exit_kind in ("TARGET", "EXPIRY"):
        return FailureDomain.SELECTION
    return FailureDomain.EXECUTION
