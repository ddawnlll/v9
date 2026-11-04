"""Experts package for v8-next: authoritative epistemic witnesses."""

from v8_next.experts.registry import (
    CANONICAL_28_EXPERTS,
    EXPERT_REGISTRY,
    REQUIRES_TABLE,
    VARIANT_TABLE,
    ExpertSpec,
    get_expert,
    observe_all_28,
    observe_expert,
    registry_rows,
    validate_variant_overrides,
)

__all__ = [
    "CANONICAL_28_EXPERTS",
    "EXPERT_REGISTRY",
    "REQUIRES_TABLE",
    "VARIANT_TABLE",
    "ExpertSpec",
    "get_expert",
    "observe_all_28",
    "observe_expert",
    "registry_rows",
    "validate_variant_overrides",
]
