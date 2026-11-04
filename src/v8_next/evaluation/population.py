"""Benchmark populations & partitioners — thin port of v8-core/src/benchmark/population.rs (D-153 §§34–35).

CPCV (purged combinatorial) and chronological walk-forward splits with purge
+ embargo non-leakage. Segments carry their data role; a protected frozen-OOS
segment with any other role is refused (fail closed, never silently scored).

DIVERGENCE (named, not silent): the ``DataRole`` here is the 8-variant
assurance ontology (Development / BurnedDiagnostic / FrozenOOS /
ShadowProspective / LiveRealized / Synthetic*). ``evaluation/store.py`` keeps
its own 3-variant ``Literal`` for the research-ledger layer; the two are not
merged — this module never imports or alters the store role.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from enum import StrEnum

__all__ = [
    "CpcvPartitioner",
    "DataRole",
    "EvaluationPopulation",
    "PartitionSplit",
    "PopulationSegment",
    "WalkForwardPartitioner",
]


class DataRole(StrEnum):
    DEVELOPMENT = "Development"
    BURNED_DIAGNOSTIC = "BurnedDiagnostic"
    FROZEN_OOS = "FrozenOOS"
    SHADOW_PROSPECTIVE = "ShadowProspective"
    LIVE_REALIZED = "LiveRealized"
    SYNTHETIC_DEV = "SyntheticDev"
    SYNTHETIC_QUALIFICATION = "SyntheticQualification"
    SYNTHETIC_NOVELTY = "SyntheticNovelty"


class EvaluationPopulation(StrEnum):
    BURNED_DIAGNOSTIC_REAL = "BurnedDiagnosticReal"
    CHRONOLOGICAL_WALK_FORWARD = "ChronologicalWalkForward"
    PURGED_COMBINATORIAL_KFOLD = "PurgedCombinatorialKFold"
    PROTECTED_FROZEN_OOS = "ProtectedFrozenOos"
    FOUNDRY_SYNTHETIC_NOVELTY = "FoundrySyntheticNovelty"
    EXTERNAL_EXECUTION_PARITY = "ExternalExecutionParity"

    def is_synthetic(self) -> bool:
        return self is EvaluationPopulation.FOUNDRY_SYNTHETIC_NOVELTY


@dataclass(frozen=True)
class PopulationSegment:
    population_type: EvaluationPopulation
    segment_id: str
    start_timestamp_ns: int
    end_timestamp_ns: int
    data_role: DataRole
    is_embargoed: bool = False

    def audit_access(self) -> None:
        if (
            self.population_type is EvaluationPopulation.PROTECTED_FROZEN_OOS
            and self.data_role is not DataRole.FROZEN_OOS
        ):
            raise ValueError("FROZEN_OOS_MISMATCH: ProtectedFrozenOos needs FrozenOOS role")

    def as_dict(self) -> dict[str, object]:
        return {
            "population_type": self.population_type.value,
            "segment_id": self.segment_id,
            "start_timestamp_ns": self.start_timestamp_ns,
            "end_timestamp_ns": self.end_timestamp_ns,
            "data_role": self.data_role.value,
            "is_embargoed": self.is_embargoed,
        }


@dataclass
class PartitionSplit:
    split_id: int
    train_segments: list[PopulationSegment] = field(default_factory=list)
    test_segments: list[PopulationSegment] = field(default_factory=list)
    purge_window_ns: int = 0
    embargo_window_ns: int = 0

    def as_dict(self) -> dict[str, object]:
        return {
            "split_id": self.split_id,
            "train_segments": [s.as_dict() for s in self.train_segments],
            "test_segments": [s.as_dict() for s in self.test_segments],
            "purge_window_ns": self.purge_window_ns,
            "embargo_window_ns": self.embargo_window_ns,
        }


class CpcvPartitioner:
    """Purged combinatorial K-fold splits (D-153 §35)."""

    def __init__(self, n_splits: int, k_test_groups: int, purge_window_ns: int, embargo_window_ns: int) -> None:
        if n_splits < 2:
            raise ValueError("CPCV_CONFIG: n_splits must be at least 2")
        if not 1 <= k_test_groups < n_splits:
            raise ValueError("CPCV_CONFIG: k_test_groups must be in [1, n_splits)")
        self.n_splits = n_splits
        self.k_test_groups = k_test_groups
        self.purge_window_ns = purge_window_ns
        self.embargo_window_ns = embargo_window_ns

    def generate_splits(self, start_ns: int, end_ns: int) -> list[PartitionSplit]:
        total = max(0, end_ns - start_ns)
        group_len = total // self.n_splits if self.n_splits else 0
        groups: list[PopulationSegment] = []
        for i in range(self.n_splits):
            g_start = start_ns + i * group_len
            g_end = end_ns if i == self.n_splits - 1 else g_start + group_len
            groups.append(
                PopulationSegment(
                    EvaluationPopulation.PURGED_COMBINATORIAL_KFOLD,
                    f"cpcv_group_{i}",
                    g_start,
                    g_end,
                    DataRole.BURNED_DIAGNOSTIC,
                )
            )
        splits: list[PartitionSplit] = []
        combos = list(itertools.combinations(range(self.n_splits), self.k_test_groups))
        for split_idx, test_indices in enumerate(combos):
            test_set = set(test_indices)
            test_segs = [g for j, g in enumerate(groups) if j in test_set]
            train_segs: list[PopulationSegment] = []
            for j, grp in enumerate(groups):
                if j in test_set:
                    continue
                seg = grp
                for t in test_set:
                    if j > t and j - t == 1:
                        seg = PopulationSegment(
                            seg.population_type,
                            seg.segment_id,
                            seg.start_timestamp_ns + self.embargo_window_ns,
                            seg.end_timestamp_ns,
                            seg.data_role,
                            True,
                        )
                if seg.start_timestamp_ns < seg.end_timestamp_ns:
                    train_segs.append(seg)
            splits.append(
                PartitionSplit(split_idx, train_segs, test_segs, self.purge_window_ns, self.embargo_window_ns)
            )
        return splits


class WalkForwardPartitioner:
    """Chronological walk-forward splits (D-153 §34)."""

    def __init__(
        self,
        n_folds: int,
        expanding_window: bool,
        train_ratio: float,
        purge_window_ns: int,
        embargo_window_ns: int,
    ) -> None:
        if n_folds < 1:
            raise ValueError("WF_CONFIG: n_folds must be >= 1")
        if not 0.0 < train_ratio < 1.0:
            raise ValueError("WF_CONFIG: train_ratio must be in (0, 1)")
        self.n_folds = n_folds
        self.expanding_window = expanding_window
        self.train_ratio = train_ratio
        self.purge_window_ns = purge_window_ns
        self.embargo_window_ns = embargo_window_ns

    def generate_splits(self, start_ns: int, end_ns: int) -> list[PartitionSplit]:
        total = max(0, end_ns - start_ns)
        step = total // (self.n_folds + 1) if self.n_folds else 0
        splits: list[PartitionSplit] = []
        for fold in range(self.n_folds):
            train_start = start_ns if self.expanding_window else start_ns + fold * (step // 2)
            train_end = start_ns + (fold + 1) * step
            test_start = train_end + self.purge_window_ns
            test_end = min(test_start + step, end_ns)
            splits.append(
                PartitionSplit(
                    fold,
                    [
                        PopulationSegment(
                            EvaluationPopulation.CHRONOLOGICAL_WALK_FORWARD,
                            f"wf_train_{fold}",
                            train_start,
                            train_end,
                            DataRole.DEVELOPMENT,
                        )
                    ],
                    [
                        PopulationSegment(
                            EvaluationPopulation.CHRONOLOGICAL_WALK_FORWARD,
                            f"wf_test_{fold}",
                            test_start,
                            test_end,
                            DataRole.BURNED_DIAGNOSTIC,
                        )
                    ],
                    self.purge_window_ns,
                    self.embargo_window_ns,
                )
            )
        return splits
