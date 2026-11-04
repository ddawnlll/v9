"""Canonical economic trace identity, trajectory modality and provenance — port of
``v8-core/src/telemetry/identity.rs`` (EEO-001H, D-136).

Constitutional invariants carried over from the Rust module:

1. **Opportunity sovereignty** — :class:`EconomicTraceId` identifies one execution or decision
   trajectory *of* an opportunity; it is not the opportunity identity itself.
2. **Trajectory identity** — the trajectory tag separates baseline, challenger and
   counterfactual branches while preserving opportunity identity alignment.
3. **Provenance isolation** — :class:`TraceProvenance` captures the tape/policy/constitution/code
   commit state without destroying identity alignment between those branches.
4. **Typed modality** — :class:`TrajectoryType` distinguishes ``Observed`` from
   ``Counterfactual`` without naming a heuristic.
5. **Identity lineage decoupling (EEO-002 invariant 5)** — :class:`EconomicTraceId`,
   :class:`SpanId`, ``BeliefReceiptId`` and :class:`TraceProvenance` are four *distinct* types
   over four distinct digest inputs. No identity in this module takes a wall clock: the only
   time any of them binds is the Point-In-Time decision timestamp the caller supplies, so the
   same content reproduces the same identity on any machine at any time.

Digest convention: the Rust side hashes a ``Canon`` byte encoding with BLAKE3. This port reuses
the repository's existing canonical-JSON helper (``v8_next.evaluation.store.canonical``) with
SHA-256. The bytes differ from the Rust digest, the property does not — same content gives the
same identity, different content gives a different one (``canonical`` rejects non-finite floats,
so a NaN fails closed instead of entering a digest as an anonymous number).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol

from v8_next.evaluation.store import canonical

#: The digest every identity in this package is computed with.
HASH_ALGORITHM = "sha256"


def canonical_digest(tag: str, payload: object) -> str:
    """Domain-separated digest of a canonical-JSON payload.

    ``tag`` names the identity domain (the Rust side pushes the same name as the first
    ``Canon::push_str``), so two identities with the same field values in different domains
    cannot collide. Wall-clock time is never part of ``payload`` at any call site in this
    package; a caller that needs a time dimension passes the Point-In-Time decision timestamp.
    """
    return hashlib.sha256(f"{tag}|{canonical(payload)}".encode()).hexdigest()


class TraceLineageError(Exception):
    """A trace identity, lineage or provenance contract was violated.

    The named failure every constructor in this package raises. ``code`` is the machine-readable
    discriminator (the Python counterpart of the Rust ``V8CoreError::TraceLineageError`` arm) and
    is carried on every subclass so a consumer branches on the code, never on message prose.
    """

    code: str = "TRACE_LINEAGE_ERROR"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        resolved = code or type(self).code
        super().__init__(f"{resolved}: {message}")
        self.code = resolved


class TrajectoryType(StrEnum):
    """Execution modality for an economic decision trajectory."""

    #: Canonical observed forward simulation or physical execution.
    Observed = "Observed"
    #: Counterfactual branch under a registered or exploratory policy intervention.
    Counterfactual = "Counterfactual"

    def is_observed(self) -> bool:
        return self is TrajectoryType.Observed

    def is_counterfactual(self) -> bool:
        return self is TrajectoryType.Counterfactual

    def as_str(self) -> str:
        return str(self.value)


@dataclass(frozen=True)
class TraceProvenance:
    """Cryptographic provenance context binding the decision environment (D-136-RP-001 §6.1)."""

    tape_hash: str
    policy_hash: str
    constitution_hash: str
    code_hash: str

    @classmethod
    def new(
        cls,
        tape_hash: str,
        policy_hash: str,
        constitution_hash: str,
        code_hash: str,
    ) -> TraceProvenance:
        """Validate and construct a provenance bundle; an empty hash is refused.

        The Rust constructor returns ``Err(TraceLineageError)`` per empty field; this port raises
        the same named error with the same per-field message.
        """
        for field_name, value in (
            ("tape_hash", tape_hash),
            ("policy_hash", policy_hash),
            ("constitution_hash", constitution_hash),
            ("code_hash", code_hash),
        ):
            if not value:
                raise TraceLineageError(f"{field_name} cannot be empty in TraceProvenance")
        return cls(
            tape_hash=str(tape_hash),
            policy_hash=str(policy_hash),
            constitution_hash=str(constitution_hash),
            code_hash=str(code_hash),
        )

    def compute_hash(self) -> str:
        """Deterministic digest of the provenance bundle. Takes no time input."""
        return canonical_digest(
            "TraceProvenance",
            [self.tape_hash, self.policy_hash, self.constitution_hash, self.code_hash],
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "tape_hash": self.tape_hash,
            "policy_hash": self.policy_hash,
            "constitution_hash": self.constitution_hash,
            "code_hash": self.code_hash,
            "provenance_hash": self.compute_hash(),
        }


@dataclass(frozen=True, order=True)
class EconomicTraceId:
    """Canonical economic trace identifier (digest-derived hex).

    Identifies a specific execution/decision trajectory for an opportunity. Deliberately a
    distinct type from :class:`SpanId` and from ``BeliefReceiptId``: identity lineage decoupling
    (EEO-002 invariant 5) is a type-level property here, not a naming convention.
    """

    value: str

    @classmethod
    def new(cls, identifier: str) -> EconomicTraceId:
        return cls(str(identifier))

    @classmethod
    def compute(
        cls,
        opportunity_id: str,
        trajectory_tag: str,
        trajectory_type: TrajectoryType,
        pit_timestamp: int,
    ) -> EconomicTraceId:
        """Deterministic identity for an economic trajectory.

        The trajectory tag distinguishes baseline vs challenger vs counterfactual branches while
        preserving opportunity identity alignment. ``pit_timestamp`` is the Point-In-Time
        decision timestamp, never a wall clock.
        """
        return cls(
            canonical_digest(
                "EconomicTraceId",
                [opportunity_id, trajectory_tag, trajectory_type.as_str(), int(pit_timestamp)],
            )
        )

    def as_str(self) -> str:
        return self.value

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, order=True)
class SpanId:
    """Canonical decision/evidence span identifier (digest-derived hex)."""

    value: str

    @classmethod
    def new(cls, identifier: str) -> SpanId:
        return cls(str(identifier))

    @classmethod
    def compute(
        cls,
        trace_id: EconomicTraceId,
        parent_span_id: SpanId | None,
        stage_name: str,
        start_time: int,
        disambiguator: str,
    ) -> SpanId:
        """Deterministic identity for a span.

        A span with no parent encodes an explicit ``null`` in the parent slot (the Rust
        ``Canon::push_null``), so "the root span of this stage at this time" and "the child of
        span X" cannot collide.
        """
        return cls(
            canonical_digest(
                "SpanId",
                [
                    trace_id.as_str(),
                    None if parent_span_id is None else parent_span_id.as_str(),
                    stage_name,
                    int(start_time),
                    disambiguator,
                ],
            )
        )

    def as_str(self) -> str:
        return self.value

    def __str__(self) -> str:
        return self.value


class OpportunityEpisodeLike(Protocol):
    """Structural view of the Rust ``OpportunityEpisode`` used by the ``from_episode`` helpers.

    The v8-next counterpart (``v8_next.opportunities.models.OpportunityRecord``) spells the two
    fields ``opportunity_id`` and ``as_of_time_ns``; the Rust struct spells them ``episode_id``
    and ``as_of_time``. The protocol names the Python spelling, and the mismatch is recorded here
    rather than papered over with a duck-typed getattr chain.
    """

    opportunity_id: str
    as_of_time_ns: int


@dataclass(frozen=True)
class EconomicTraceContext:
    """Immutable root context of an economic trace (D-136-RP-001 §6.1).

    Preserves opportunity identity, trajectory identity and provenance as three distinct
    dimensions. Construct through :meth:`new` (or the episode helpers) so the trace id is always
    derived from the content rather than accepted from a caller.
    """

    trace_id: EconomicTraceId
    opportunity_id: str
    trajectory_type: TrajectoryType
    trajectory_tag: str
    pit_timestamp: int
    provenance: TraceProvenance

    @classmethod
    def new(
        cls,
        opportunity_id: str,
        trajectory_type: TrajectoryType,
        trajectory_tag: str,
        pit_timestamp: int,
        provenance: TraceProvenance,
    ) -> EconomicTraceContext:
        """Validate and construct a context, deriving its trace identity from the content."""
        if not opportunity_id:
            raise TraceLineageError("opportunity_id cannot be empty")
        if not trajectory_tag:
            raise TraceLineageError("trajectory_tag cannot be empty")
        return cls(
            trace_id=EconomicTraceId.compute(
                opportunity_id, trajectory_tag, trajectory_type, pit_timestamp
            ),
            opportunity_id=str(opportunity_id),
            trajectory_type=trajectory_type,
            trajectory_tag=str(trajectory_tag),
            pit_timestamp=int(pit_timestamp),
            provenance=provenance,
        )

    @classmethod
    def from_episode(
        cls,
        episode: OpportunityEpisodeLike,
        tape_hash: str,
        policy_hash: str,
        constitution_hash: str,
        code_hash: str,
    ) -> EconomicTraceContext:
        """Context for an observed opportunity episode (Rust ``from_episode``)."""
        return cls.from_episode_trajectory(
            episode,
            TrajectoryType.Observed,
            "canonical_observed",
            tape_hash,
            policy_hash,
            constitution_hash,
            code_hash,
        )

    @classmethod
    def from_episode_trajectory(
        cls,
        episode: OpportunityEpisodeLike,
        trajectory_type: TrajectoryType,
        trajectory_tag: str,
        tape_hash: str,
        policy_hash: str,
        constitution_hash: str,
        code_hash: str,
    ) -> EconomicTraceContext:
        """Context for a named challenger or counterfactual trajectory (Rust helper)."""
        provenance = TraceProvenance.new(
            tape_hash, policy_hash, constitution_hash, code_hash
        )
        return cls.new(
            episode.opportunity_id,
            trajectory_type,
            trajectory_tag,
            episode.as_of_time_ns,
            provenance,
        )

    @property
    def tape_hash(self) -> str:
        return self.provenance.tape_hash

    @property
    def policy_hash(self) -> str:
        return self.provenance.policy_hash

    @property
    def constitution_hash(self) -> str:
        return self.provenance.constitution_hash

    @property
    def code_hash(self) -> str:
        return self.provenance.code_hash

    def compute_hash(self) -> str:
        """Digest of the whole trace context payload. Takes no wall-clock input."""
        return canonical_digest(
            "EconomicTraceContext",
            [
                self.trace_id.as_str(),
                self.opportunity_id,
                self.trajectory_type.as_str(),
                self.trajectory_tag,
                self.pit_timestamp,
                self.provenance.compute_hash(),
            ],
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "trace_id": self.trace_id.as_str(),
            "opportunity_id": self.opportunity_id,
            "trajectory_type": self.trajectory_type.as_str(),
            "trajectory_tag": self.trajectory_tag,
            "pit_timestamp": self.pit_timestamp,
            "is_observed": self.trajectory_type.is_observed(),
            "is_counterfactual": self.trajectory_type.is_counterfactual(),
            "provenance": self.provenance.as_dict(),
            "context_hash": self.compute_hash(),
        }
