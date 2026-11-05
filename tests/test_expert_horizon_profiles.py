"""The expert horizon table must cover all 28 witnesses and stay bound to the registry."""

from __future__ import annotations

from collections import Counter

import pytest

from v8_next.domain.horizons import Timeframe
from v8_next.experts.horizon_profiles import (
    HORIZON_PROFILES,
    PROFILE_TABLE_VERSION,
    MissingBehavior,
    profile_for,
    profiles_by_primary,
    validate_profiles,
)
from v8_next.experts.registry import CANONICAL_28_EXPERTS, get_expert


def test_table_covers_all_28_canonical_experts_exactly_once() -> None:
    validate_profiles()
    assert set(HORIZON_PROFILES) == set(CANONICAL_28_EXPERTS)
    assert len(HORIZON_PROFILES) == 28
    for expert_id, profile in HORIZON_PROFILES.items():
        assert profile.expert_id == expert_id
        assert profile.version == PROFILE_TABLE_VERSION


def test_primary_horizons_are_declared_and_not_all_one_timeframe() -> None:
    grouped = profiles_by_primary()
    counts = Counter(profile.primary for profile in HORIZON_PROFILES.values())
    assert counts[Timeframe.M15] == 8
    assert counts[Timeframe.H1] == 14
    assert counts[Timeframe.H4] == 6
    assert sum(len(ids) for ids in grouped.values()) == 28
    # The whole point of the table: experts are not all on one frame.
    assert len(counts) == 3


def test_profile_contract_matches_the_registry() -> None:
    for expert_id in CANONICAL_28_EXPERTS:
        spec = get_expert(expert_id)
        profile = profile_for(expert_id)
        assert profile.warmup_bars == spec.warmup
        assert tuple(sorted(profile.required_features)) == tuple(sorted(spec.requires))
        assert profile.missing_behavior is MissingBehavior.ABSTAIN_CONTEXT_INCOMPLETE
        assert profile.primary not in profile.confirmations
        assert set(profile.required_confirmations) <= set(profile.confirmations)
        assert profile.max_staleness_ns > 0


def test_unknown_expert_fails_closed() -> None:
    with pytest.raises(KeyError):
        profile_for("not_an_expert")


def test_research_hypothesis_is_coarse_and_stable() -> None:
    """Profiles are grouped by mechanism family, not tuned per bar."""
    rationales = Counter(profile.rationale for profile in HORIZON_PROFILES.values())
    assert set(rationales) == {
        "short_structure_liquidity",
        "intermediate_swing_context",
        "higher_timeframe_regime",
    }
