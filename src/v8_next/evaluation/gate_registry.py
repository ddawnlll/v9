"""NX08 (#429) — one canonical source for the G0..G9 label/field/resolver/readiness map.

The gate labels live in ``benchmark_receipt.GATE_DESCRIPTORS``, the states live on
``GateVector``, the operational resolvers live in ``gate_resolution``, and the
readiness decision lives on ``GateVector.readiness``. Those four were consistent by
convention only. This module derives the single map from the descriptor table,
attaches the resolver and readiness role explicitly, and can prove the mapping
still matches reality: field order, field names, resolver symbols and roles are all
checked against the live objects, so a renamed field or a missing resolver is a
test failure rather than a silent drift.

Nothing here re-defines gate meaning: a role is attached to the descriptor that
already carries the label and the source clause, and the tests pin the existing
semantics instead of inventing new ones.
"""

from __future__ import annotations

from dataclasses import dataclass

from v8_next.evaluation import gate_resolution
from v8_next.evaluation.benchmark_receipt import GATE_DESCRIPTORS, GateVector

#: Which resolver function owns each canonical gate id, as it exists today in
#: ``evaluation.gate_resolution``. Every value is a real symbol in that module: #447
#: removed the ``resolver:receipt_structural`` declaration, which named no resolver at
#: all and was skipped by ``validate_registry`` -- a published state was fed by a map
#: entry nobody could check. The structural trio G0/G1/G2 is resolved by one measured
#: step, ``resolve_structural_gates``, which produces all three cells from the
#: measured lineage, the unmeasured PIT input and the verified ledger.
GATE_RESOLVERS: dict[str, str] = {
    "G0ConstitutionalIntegrity": "resolve_structural_gates",
    "G1MeasurementIdentity": "resolve_structural_gates",
    "G2HistoricalDiagnostic": "resolve_structural_gates",
    "G3ScenarioRobustness": "evaluate_g3_scenario_robustness",
    "G4SyntheticFalsification": "evaluate_g4_synthetic_falsification",
    "G5SelectionControl": "evaluate_g5_selection_control",
    "G6FrozenOOSReplication": "evaluate_g6_frozen_oos",
    "G7ProspectiveShadow": "evaluate_g7_prospective_shadow",
    "G8LiveRealization": "evaluate_g8_live_realization",
    "G9Certificate": "evaluate_g9_certificate_authority",
}

#: Readiness role of each gate. ``BLOCKING`` gates gate readiness; ``DIAGNOSTIC``
#: gates record state without granting authority; ``CAPITAL`` marks the gates whose
#: PASS is a precondition for any capital-facing statement.
GATE_READINESS_ROLES: dict[str, str] = {
    "G0ConstitutionalIntegrity": "BLOCKING",
    "G1MeasurementIdentity": "BLOCKING",
    "G2HistoricalDiagnostic": "DIAGNOSTIC",
    "G3ScenarioRobustness": "DIAGNOSTIC",
    "G4SyntheticFalsification": "DIAGNOSTIC",
    "G5SelectionControl": "DIAGNOSTIC",
    "G6FrozenOOSReplication": "DIAGNOSTIC",
    "G7ProspectiveShadow": "DIAGNOSTIC",
    "G8LiveRealization": "CAPITAL",
    "G9Certificate": "CAPITAL",
}

READINESS_ROLES = ("BLOCKING", "DIAGNOSTIC", "CAPITAL")


@dataclass(frozen=True)
class GateRegistryEntry:
    index: int
    canonical_id: str
    vector_field: str
    requirement: str
    resolver: str
    readiness_role: str
    source_clause: str

    def as_dict(self) -> dict[str, object]:
        return {
            "index": self.index,
            "canonical_id": self.canonical_id,
            "vector_field": self.vector_field,
            "requirement": self.requirement,
            "resolver": self.resolver,
            "readiness_role": self.readiness_role,
            "source_clause": self.source_clause,
        }


def gate_registry() -> tuple[GateRegistryEntry, ...]:
    """The G0..G9 map, derived from the descriptor table that already defines it."""
    return tuple(
        GateRegistryEntry(
            index=descriptor.index,
            canonical_id=descriptor.canonical_id,
            vector_field=descriptor.vector_field,
            requirement=descriptor.requirement,
            resolver=GATE_RESOLVERS[descriptor.canonical_id],
            readiness_role=GATE_READINESS_ROLES[descriptor.canonical_id],
            source_clause=descriptor.source_clause,
        )
        for descriptor in GATE_DESCRIPTORS
    )


def registry_by_field() -> dict[str, GateRegistryEntry]:
    return {entry.vector_field: entry for entry in gate_registry()}


def registry_by_id() -> dict[str, GateRegistryEntry]:
    return {entry.canonical_id: entry for entry in gate_registry()}


def validate_registry() -> list[str]:
    """Return every inconsistency; an empty list means the map matches reality.

    #447: every declared resolver is resolved against ``gate_resolution``. A
    ``resolver:`` prefix used to be skipped as a self-declaring structural marker,
    which let the map point at a symbol that does not exist and still validate --
    the check the registry exists for was the one place it could be bypassed.
    """
    problems: list[str] = []
    registry = gate_registry()
    fields = tuple(GateVector.model_fields)
    if len(registry) != len(fields):
        problems.append(f"registry has {len(registry)} gates but GateVector has {len(fields)} fields")
    if tuple(entry.vector_field for entry in registry) != fields:
        problems.append("registry field order/names do not match GateVector")
    if tuple(entry.index for entry in registry) != tuple(range(len(registry))):
        problems.append("gate indexes are not contiguous from 0")
    if len({entry.canonical_id for entry in registry}) != len(registry):
        problems.append("duplicate canonical gate id")
    for entry in registry:
        if entry.requirement not in ("Required", "RequiredBlocking", "Optional"):
            problems.append(f"{entry.canonical_id}: unknown requirement {entry.requirement!r}")
        if entry.readiness_role not in READINESS_ROLES:
            problems.append(f"{entry.canonical_id}: unknown readiness role {entry.readiness_role!r}")
        if not entry.source_clause.strip():
            problems.append(f"{entry.canonical_id}: empty source clause")
        resolver = getattr(gate_resolution, entry.resolver, None)
        if not callable(resolver):
            problems.append(
                f"{entry.canonical_id}: resolver {entry.resolver!r} is not a callable "
                "in gate_resolution"
            )
    for canonical_id in GATE_RESOLVERS:
        if canonical_id not in {entry.canonical_id for entry in registry}:
            problems.append(f"{canonical_id}: resolver declared for an unknown gate")
    return problems
