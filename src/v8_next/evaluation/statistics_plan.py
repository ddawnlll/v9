"""NX07 (#428) — the statistical plan is pinned before any result is seen.

Everything a multiplicity correction depends on lives in one immutable record:
block length, resampling count, seed, the declared trial multiplicity, the
effective-independent-trials declaration and its basis, the CSCV partitions, and
-- explicitly -- which statistics are **authority conditions** and which are
**diagnostics**.

Two rules are enforced here rather than trusted to callers:

* a family's plan is pinned once. Re-pinning the same content is a no-op;
  re-pinning different content for the same family raises, because choosing a
  longer block, a luckier seed, or a smaller multiplicity after seeing a p-value
  is exactly the tuning this contract forbids;
* authority and diagnostic sets must both be non-empty and disjoint, so a
  receipt can never present a diagnostic as the condition the gate turned on.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, Literal

from v8_next.evaluation.store import canonical

if TYPE_CHECKING:  # pragma: no cover
    from v8_next.evaluation.store import ResearchStore

#: Canonical G5 resampling block length, in per-campaign intervals. Declared once
#: here and pinned into every plan, so it is a pre-registration and not a
#: per-run choice.
CANONICAL_G5_BLOCK_SIZE = 5

#: What the G5 gate actually branches on today (``dsr_conf >= 0.95`` and the
#: Bonferroni-adjusted p-value). Declared as data so a receipt can state it.
G5_AUTHORITY_CONDITIONS: tuple[str, ...] = (
    "deflated_sharpe_confidence>=0.95",
    "bonferroni_adjusted_p<=0.05",
)

#: Computed and reported next to the authority conditions, but not branched on.
G5_DIAGNOSTICS: tuple[str, ...] = (
    "white_reality_check_p_value",
    "probability_of_backtest_overfitting",
    "sample_sufficiency",
    "regime_attribution",
)


@dataclass(frozen=True)
class StatisticsPlan:
    """One pinned statistical procedure for one family."""

    family: str
    block_size: int
    reps: int
    seed: int
    multiplicity_trials: int
    effective_independent_trials: float
    independence_basis: str
    pbo_partitions: int
    pbo_metric: Literal["mean_return", "sharpe"]
    pbo_max_splits: int
    wrc_block_divisor: int
    wrc_reps: int
    wrc_seed: int
    authority_conditions: tuple[str, ...]
    diagnostics: tuple[str, ...]
    pinned_ns: int
    interval_bars: int = 0

    def __post_init__(self) -> None:
        if not self.family or not self.family.strip():
            raise ValueError("statistics plan needs a family")
        for name in ("block_size", "reps", "seed", "multiplicity_trials", "pbo_max_splits"):
            value = getattr(self, name)
            if name == "seed":
                if value < 0:
                    raise ValueError(f"{name} must be non-negative")
                continue
            if value <= 0:
                raise ValueError(f"{name} must be positive")
        if self.effective_independent_trials <= 0 or self.effective_independent_trials > self.multiplicity_trials:
            raise ValueError("effective independent trials must be in (0, multiplicity]")
        if not self.independence_basis.strip():
            raise ValueError("an independence basis is required, not assumed")
        if self.pbo_partitions <= 1:
            raise ValueError("CSCV partitions must be > 1")
        if self.wrc_block_divisor <= 0 or self.wrc_reps <= 0:
            raise ValueError("WRC resampling parameters must be positive")
        if not self.authority_conditions or not self.diagnostics:
            raise ValueError("authority conditions and diagnostics must both be declared")
        if set(self.authority_conditions) & set(self.diagnostics):
            raise ValueError("a statistic cannot be both an authority condition and a diagnostic")
        if self.pinned_ns <= 0:
            raise ValueError("a plan must carry the instant it was pinned")

    def identity(self) -> str:
        """Content hash: everything except the pinning instant."""
        return "sha256:" + hashlib.sha256(canonical(self._payload()).encode()).hexdigest()

    def pinned(self, pinned_ns: int) -> StatisticsPlan:
        return replace(self, pinned_ns=pinned_ns)

    def _payload(self) -> dict[str, Any]:
        """Every field except ``pinned_ns`` -- the content that must not drift."""
        return {
            "family": self.family,
            "block_size": self.block_size,
            "reps": self.reps,
            "seed": self.seed,
            "multiplicity_trials": self.multiplicity_trials,
            "effective_independent_trials": self.effective_independent_trials,
            "independence_basis": self.independence_basis,
            "pbo_partitions": self.pbo_partitions,
            "pbo_metric": self.pbo_metric,
            "pbo_max_splits": self.pbo_max_splits,
            "wrc_block_divisor": self.wrc_block_divisor,
            "wrc_reps": self.wrc_reps,
            "wrc_seed": self.wrc_seed,
            "authority_conditions": list(self.authority_conditions),
            "diagnostics": list(self.diagnostics),
            "interval_bars": self.interval_bars,
        }

    def as_dict(self) -> dict[str, Any]:
        payload = self._payload()
        payload["pinned_ns"] = self.pinned_ns
        payload["identity"] = self.identity()
        return payload


@dataclass(frozen=True)
class SelectionFamily:
    """A provenance-bound record of what was actually searched.

    Four stress transformations of one strategy are not a selection family, and
    a caller's assertion that it searched ``N`` variants is not evidence either.
    This record can only be built from a concrete member list plus the instant
    the family was registered; its identity is derived from that content, so a
    gate can report the exact family a correction covered and cannot be handed
    a bare trial count.
    """

    family_id: str
    members: tuple[str, ...]
    registered_ns: int
    basis: str

    def __post_init__(self) -> None:
        if not self.family_id.strip():
            raise ValueError("selection family needs an id")
        if not self.members:
            raise ValueError("selection family needs at least one member")
        if len(set(self.members)) != len(self.members):
            raise ValueError("selection family members must be unique")
        if any(not m.strip() for m in self.members):
            raise ValueError("selection family members must be named")
        if self.registered_ns <= 0:
            raise ValueError("selection family must carry its registration instant")
        if not self.basis.strip():
            raise ValueError("selection family needs a stated basis")

    def identity(self) -> str:
        return "sha256:" + hashlib.sha256(
            canonical(
                {
                    "family_id": self.family_id,
                    "members": list(self.members),
                    "registered_ns": self.registered_ns,
                    "basis": self.basis,
                }
            ).encode()
        ).hexdigest()

    def as_dict(self) -> dict[str, Any]:
        return {
            "family_id": self.family_id,
            "members": list(self.members),
            "member_count": len(self.members),
            "registered_ns": self.registered_ns,
            "basis": self.basis,
            "identity": self.identity(),
        }


def g5_plan(
    family: str,
    *,
    pinned_ns: int,
    block_size: int,
    reps: int,
    seed: int,
    multiplicity_trials: int = 4,
    effective_independent_trials: float = 4.0,
    independence_basis: str = (
        "four preregistered execution variants (champion, fee-stressed, conservative, "
        "slippage-stressed) on one shared tape; not proven independent"
    ),
    interval_bars: int = 0,
) -> StatisticsPlan:
    """The G5 plan: authority = DSR confidence and Bonferroni-adjusted p."""
    return StatisticsPlan(
        family=family,
        block_size=block_size,
        reps=reps,
        seed=seed,
        multiplicity_trials=multiplicity_trials,
        effective_independent_trials=effective_independent_trials,
        independence_basis=independence_basis,
        pbo_partitions=4,
        pbo_metric="mean_return",
        pbo_max_splits=64,
        wrc_block_divisor=5,
        wrc_reps=reps,
        wrc_seed=seed,
        authority_conditions=G5_AUTHORITY_CONDITIONS,
        diagnostics=G5_DIAGNOSTICS,
        interval_bars=interval_bars,
        pinned_ns=pinned_ns,
    )


def sample_sufficiency(
    n_observations: int, block_size: int, *, min_independent_samples: int = 2
) -> dict[str, Any]:
    """Sample adequacy that counts dependence, not rows.

    Overlapping observations from one path are not independent samples, so the
    honest count is how many non-overlapping blocks of the pinned block length
    the series holds -- and a series too short to hold even
    ``min_independent_samples`` blocks is reported as such instead of being
    described as an ``n`` of raw rows.
    """
    if n_observations < 0:
        raise ValueError("observation count cannot be negative")
    if block_size <= 0:
        raise ValueError("block size must be positive")
    observations_per_block = max(1, block_size)
    independent = n_observations // observations_per_block
    return {
        "observations": n_observations,
        "block_size": observations_per_block,
        "independent_samples": independent,
        "min_independent_samples": min_independent_samples,
        "verdict": (
            "UNKNOWN"
            if n_observations == 0
            else ("SUFFICIENT" if independent >= min_independent_samples else "INSUFFICIENT")
        ),
        "basis": "non-overlapping blocks of the pinned block length, not raw row count",
    }


def pin_statistics_plan(store: ResearchStore, plan: StatisticsPlan) -> str:
    """Register the plan for its family; a different plan for the same family raises."""
    plan_id = plan.identity()
    existing = store.db.execute(
        "SELECT plan_id FROM statistics_plans WHERE family=?", (plan.family,)
    ).fetchone()
    if existing is not None:
        if existing[0] != plan_id:
            raise ValueError(
                f"statistics plan for family {plan.family!r} is already pinned as "
                f"{existing[0]}; a different plan cannot be pinned after the fact"
            )
        return plan_id
    store.db.execute(
        "INSERT INTO statistics_plans (family, plan_id, payload, pinned_ns) VALUES (?,?,?,?)",
        (
            plan.family,
            plan_id,
            json.dumps(plan.as_dict(), sort_keys=True, separators=(",", ":")),
            plan.pinned_ns,
        ),
    )
    return plan_id


def load_statistics_plan(store: ResearchStore, family: str) -> StatisticsPlan:
    row = store.db.execute(
        "SELECT payload FROM statistics_plans WHERE family=?", (family,)
    ).fetchone()
    if row is None:
        raise KeyError(f"no statistics plan pinned for family {family!r}")
    payload = json.loads(row[0])
    payload["authority_conditions"] = tuple(payload["authority_conditions"])
    payload["diagnostics"] = tuple(payload["diagnostics"])
    payload.pop("identity", None)
    return StatisticsPlan(**payload)


def require_pinned_before_results(plan: StatisticsPlan, computed_ns: int) -> None:
    """A plan pinned after the numbers existed is not a pre-registration."""
    if plan.pinned_ns > computed_ns:
        raise ValueError(
            "statistics plan was pinned after the results it is supposed to govern "
            f"(pinned_ns={plan.pinned_ns} > computed_ns={computed_ns})"
        )
