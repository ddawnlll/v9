"""Self-verifying Benchmark Receipt and Append-Only Ledger (D-153 §3, Issue #328).

Epistemic Invariant:
    receipt_digest = H(canonical_encode(all_authority_relevant_fields))
    Ledger maintains a cryptographic hash chain: entry_hash = H(parent_hash || receipt_digest).
    Any mutation at rest, sequence gap, or tampering is detected and fails closed.

Chronology (#446): the ledger is the chronology authority, so every entry carries *two*
named time quantities -- ``window_end_timestamp_ns`` (the end of the data window the run
measured) and ``run_time_timestamp_ns`` (the run's own wall clock). Neither is in the
canon: a wall clock inside ``receipt_digest`` would make the same inputs hash differently
on every run, breaking the determinism the chain proves. They are published beside the
digest through ``BenchmarkReceipt.time_publication``, which refuses each quantity by name
when it was not measured instead of substituting the other for it.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Mapping, Sequence

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    model_validator,
)

from v8_next.evaluation.parity import ArtifactBinding

if TYPE_CHECKING:  # the window spec is a type-only dependency here (no import cycle)
    from v8_next.evaluation.run_window import WindowSpec

#: NX04 (#425). The canonical payload shape is *versioned and proven*, never
#: rediscovered at verification time. Each entry below was reproduced from the
#: original ledger bytes (artifacts/benchmarks/benchmark_ledger.jsonl) without
#: rewriting a single byte of it; the provenance column names the measured range.
RECEIPT_DIGEST_VERSION = "v8.5-digest-v7"

#: #444. Named refusal for the one combination that must never verify: a receipt
#: whose declared window proves no economic evidence (``profile=smoke``) while
#: carrying a numeric capability score. The score is the minted claim; the window
#: class is what says whether the run was allowed to mint it.
NON_EVIDENTIAL_WINDOW_CAPABILITY_SCORE = "NON_EVIDENTIAL_WINDOW_CAPABILITY_SCORE"

#: #448. The class a receipt carries when it declares no window evidence record at
#: all. Records written before the field existed (``v2``-``v5``), and receipts whose
#: caller declared no window, cannot be shown to be evidential: the class is named as
#: *undeclared* rather than silently assumed to be evidential. A number read off such
#: an entry is refused by this name, not published.
EVIDENCE_CLASS_UNDECLARED = "EVIDENCE_CLASS_UNDECLARED"

#: #490. Named refusal for an evidential receipt that never recorded the sample size it
#: was measured on. Publication-only: the record still certifies under its own canon
#: (D-164 -- stored bytes are not rewritten), it simply may not have its number read as
#: a capability score, because nothing in it says how many bars produced that number.
EVIDENTIAL_WINDOW_BARS_UNMEASURED = "EVIDENTIAL_WINDOW_BARS_UNMEASURED"

#: #448. Named refusal for a ledger read that has no evidential entry to publish from:
#: nothing in the ledger declares the class a capability score must have been minted
#: under, so the reader refuses by name instead of publishing the newest entry's
#: number. The refusal quotes the entry and the class it would otherwise have taken.
NO_EVIDENTIAL_LEDGER_ENTRY = "NO_EVIDENTIAL_LEDGER_ENTRY"

#: #408. Named refusals for a published capability score that is not a function of
#: the evidence its own receipt binds. The number is the claim; the bound evidence
#: is what has to carry it, so a number nobody can recompute is not a measurement
#: -- it is an assertion. Three distinct shapes, named apart:
#:
#: * ``UNBOUND``          -- a number with no evidence record bound at all;
#: * ``NOT_RECOMPUTABLE`` -- evidence is bound, but the number does not follow from it;
#: * ``CONTRADICTS``      -- two receipts bound to the same evidence publish
#:   different numbers (a ledger-level contradiction: one receipt cannot see its
#:   twin, so this is adjudicated where both are visible).
CAPABILITY_SCORE_UNBOUND_TO_EVIDENCE = "CAPABILITY_SCORE_UNBOUND_TO_EVIDENCE"
CAPABILITY_SCORE_NOT_RECOMPUTABLE = "CAPABILITY_SCORE_NOT_RECOMPUTABLE"
CAPABILITY_SCORE_CONTRADICTS_BOUND_EVIDENCE = "CAPABILITY_SCORE_CONTRADICTS_BOUND_EVIDENCE"

#: #408. Named refusal for a receipt whose own bound carriers disagree with each
#: other (the published coverage term and the evidence record's coverage term are
#: two carriers of one measurement).
CAPABILITY_SCORE_EVIDENCE_INCONSISTENT = "CAPABILITY_SCORE_EVIDENCE_INCONSISTENT"

#: #446. Unit and epoch of BOTH time quantities a ledger entry publishes. The field names
#: carry the unit (``_ns``); this constant is the declaration a reader repeats, so no
#: consumer has to guess whether a stored integer is seconds, milliseconds or nanoseconds
#: since the epoch.
TIME_FIELD_UNIT = "unix_epoch_ns (UTC)"

#: #446. Floor of a plausible reading: 2020-01-01T00:00:00Z in nanoseconds. A value below
#: it renders as a 1970 date, which is what a mis-scaled reading does
#: (``180000000000000`` ns = 50 hours = 1970-01-03). It is a scale check on the published
#: number, never a claim about when a run may happen; it is refused by name and is not
#: published as a date.
TIME_EPOCH_FLOOR_NS = 1_577_836_800_000_000_000

#: #446. How far ahead of the reading clock a published run time may sit (one day): a
#: clock that is off by a little is not refused, one that is off by a lot is.
MAX_CLOCK_SKEW_NS = 86_400_000_000_000

#: #446. Named refusals for the two time quantities. A published time field carries either
#: the measured number or the name of the reason it is not published -- never a substitute
#: for it (the defect was exactly that: the window end republished as the run time).
RUN_TIME_UNMEASURED = "RUN_TIME_UNMEASURED"
RUN_TIME_OUT_OF_RANGE = "RUN_TIME_OUT_OF_RANGE"
RUN_TIME_PRECEDES_WINDOW_END = "RUN_TIME_PRECEDES_WINDOW_END"
WINDOW_END_UNDECLARED = "WINDOW_END_UNDECLARED"
WINDOW_END_MISSCALED = "WINDOW_END_MISSCALED"
WINDOW_END_INCONSISTENT = "WINDOW_END_INCONSISTENT"

#: #446. The names the two quantities are published under, so a reader can say *which*
#: quantity it is holding instead of calling both of them "the timestamp".
RUN_TIME_FIELD = "run_time"
WINDOW_END_FIELD = "window_end"


@dataclass(frozen=True)
class TimePublication:
    """The two time quantities one entry may publish, each named or refused (#446).

    ``run_time_ns`` is the run's own wall clock; ``window_end_ns`` is the end of the data
    window that run measured. They are different quantities and are never substituted for
    one another: an entry that recorded no run time publishes ``None`` for it plus the name
    of the reason (:data:`RUN_TIME_UNMEASURED`) -- it does not publish its window end under
    a run-time name, which is what made the ledger unable to say when any run happened.
    """

    run_time_ns: int | None
    run_time_refusal: str = ""
    window_end_ns: int | None = None
    window_end_refusal: str = ""
    unit: str = TIME_FIELD_UNIT

    @property
    def publishes_run_time(self) -> bool:
        return self.run_time_ns is not None

    @property
    def publishes_window_end(self) -> bool:
        return self.window_end_ns is not None

    def as_dict(self) -> dict[str, Any]:
        """The vector as data, with each quantity's name, unit and refusal."""
        return {
            "unit": self.unit,
            RUN_TIME_FIELD: self.run_time_ns,
            f"{RUN_TIME_FIELD}_kind": RUN_TIME_FIELD if self.publishes_run_time else "",
            f"{RUN_TIME_FIELD}_refusal": self.run_time_refusal,
            WINDOW_END_FIELD: self.window_end_ns,
            f"{WINDOW_END_FIELD}_kind": WINDOW_END_FIELD if self.publishes_window_end else "",
            f"{WINDOW_END_FIELD}_refusal": self.window_end_refusal,
        }


@dataclass(frozen=True)
class ReceiptCanon:
    """Canonical field layout of one receipt-digest version.

    ``economic_pair`` / ``input_binding`` describe what the producing era emitted:

    * ``absent``          — the field must be empty for this version;
    * ``present``         — the field must be non-empty for this version;
    * ``always_present``  — emitted whether or not it is empty.

    A receipt whose data contradicts its version's proven shape is refused with
    an explicit reason (``CANON_SHAPE_UNPROVEN``): guessing a layout from the
    data would be exactly the "try field subsets until the hash matches" fallback
    NX04.R2 forbids.

    ``window_evidence`` (added with ``v8.5-digest-v6``, #444) declares whether the
    canonical payload carries the evidence class of the window that produced the
    receipt:

    * ``absent``          — the version has no such field (``v2``-``v5`` entries
      keep verifying under exactly the bytes they were written with);
    * ``always_present``  — the field is emitted whether or not it is populated,
      so a score can never be read apart from the class that minted it.

    ``score_evidence`` (added with ``v8.5-digest-v7``, #408) declares whether the
    canonical payload carries the determinants of the published capability score:

    * ``absent``          — the version has no such field (``v2``-``v6`` entries
      keep verifying under exactly the bytes they were written with);
    * ``always_present``  — the field is emitted whether or not it is populated, so
      a published number is never stored apart from the evidence that must produce
      it.
    """

    version: str
    economic_pair: Literal["absent", "present", "always_present"]
    input_binding: Literal["absent", "always_present"]
    provenance: str
    window_evidence: Literal["absent", "always_present"] = "absent"
    score_evidence: Literal["absent", "always_present"] = "absent"


RECEIPT_CANON_TABLE: dict[str, ReceiptCanon] = {
    "v8.5-digest-v2": ReceiptCanon(
        version="v8.5-digest-v2",
        economic_pair="absent",
        input_binding="absent",
        provenance=(
            "proven on seq 0-11 of artifacts/benchmarks/benchmark_ledger.jsonl "
            "(12/12 entries reproduce their stored digest with a 9-field canon)"
        ),
    ),
    "v8.5-digest-v3": ReceiptCanon(
        version="v8.5-digest-v3",
        economic_pair="present",
        input_binding="absent",
        provenance=(
            "proven on seq 12 (11-field canon: economic pair present, "
            "input_binding not yet in the digest)"
        ),
    ),
    "v8.5-digest-v4": ReceiptCanon(
        version="v8.5-digest-v4",
        economic_pair="always_present",
        input_binding="always_present",
        provenance=(
            "proven on seq 13-19 (12-field canon; input_binding joined the digest "
            "and is emitted even when empty)"
        ),
    ),
    "v8.5-digest-v5": ReceiptCanon(
        version="v8.5-digest-v5",
        economic_pair="always_present",
        input_binding="always_present",
        provenance=(
            "new writes from NX04 onward. Field layout is identical to v4; the version "
            "marker is itself inside the canon, so a new receipt gets a new digest while "
            "every stored record keeps its original bytes, digest and parent hash"
        ),
    ),
    "v8.5-digest-v6": ReceiptCanon(
        version="v8.5-digest-v6",
        economic_pair="always_present",
        input_binding="always_present",
        window_evidence="always_present",
        provenance=(
            "new writes from #444 onward. Layout is v5's plus a trailing window-evidence "
            "record (profile, evidence class, smoke flag, economic-evidence flag, bar "
            "count): a capability score is never stored apart from the evidence class of "
            "the window that minted it. v2-v5 keep their own bytes, digest and parent hash"
        ),
    ),
    "v8.5-digest-v7": ReceiptCanon(
        version="v8.5-digest-v7",
        economic_pair="always_present",
        input_binding="always_present",
        window_evidence="always_present",
        score_evidence="always_present",
        provenance=(
            "new writes from #408 onward. Layout is v6's plus a trailing score-evidence "
            "record (declared counts, coverage used, measured per-domain values in the "
            "aggregate's summation order): a published capability score is never stored "
            "apart from the evidence it must be a function of, so any consumer can "
            "recompute it from the receipt alone. v2-v6 keep their own bytes, digest and "
            "parent hash"
        ),
    ),
}


class WindowEvidence(BaseModel):
    """The evidence class of the window a receipt's numbers came from (#444).

    A window that proves no economic evidence (a bar-count ``smoke`` run) is
    liveness/mechanics evidence. Carrying its class inside the digest means the
    class cannot be edited after the numbers were minted, and
    :meth:`BenchmarkReceipt.verify` can refuse a capability score that the window
    was never allowed to produce.
    """

    model_config = ConfigDict(frozen=True)

    profile: str
    evidence_class: str
    is_smoke: bool
    economic_evidence: bool
    bars: int | None = None

    @classmethod
    def from_window(cls, window: WindowSpec) -> WindowEvidence:
        """Read the class straight off the window spec (one source of truth).

        #490: the bar count is the one the window *carries* -- the count the run
        actually loaded when it has been measured, falling back to the smoke
        shortcut only for a window whose length *is* that request
        (:attr:`WindowSpec.carried_bars`). A UTC-bounded window that never loaded a
        tape therefore records ``bars=None``; nothing is imputed for it here.
        """
        return cls(
            profile=window.profile,
            evidence_class=window.evidence_class,
            is_smoke=window.is_smoke,
            economic_evidence=window.proves_economic_evidence,
            bars=window.carried_bars,
        )

    def canon_fields(self) -> list[Any]:
        """Fixed-order fields for the digest payload (never a dict's key order)."""
        return [
            self.profile,
            self.evidence_class,
            self.is_smoke,
            self.economic_evidence,
            self.bars,
        ]

    def admits_capability_score(self) -> bool:
        """Whether this window class may mint a capability score at all."""
        return self.economic_evidence


class ScoreEvidence(BaseModel):
    """The determinants of a published capability score (#408).

    A capability score is a *derived* number: it must be a function of evidence the
    receipt carries and of nothing else. This record is that evidence -- the run's
    declared counts, the coverage the aggregate used, and the measured per-domain
    values in the order the aggregate summed them -- so a consumer can recompute the
    published number from ``receipt + artifact_bindings`` alone, and
    :meth:`BenchmarkReceipt.verify` can refuse a number its own evidence does not
    support.

    The producer publishes it through ``scoring.compute_capability_breakdown`` (key
    ``score_evidence``); :meth:`from_breakdown` is the one way a receipt gets one, so
    the arithmetic that produced the number and the arithmetic that rechecks it are
    the same code.
    """

    model_config = ConfigDict(frozen=True)

    #: Declared run counts. They do not enter the aggregate directly (the measured
    #: domain values do), but they are what the measurement was taken over.
    total_bars: int
    total_trades: int
    abstain_rate: float
    #: The coverage term the aggregate was computed with; ``None`` means the run had
    #: no eligible measurement (no coverage factor at all, never a fabricated 0.60).
    coverage_factor: float | None = None
    hard_invariants_passed: bool = True
    #: ``MEASURED`` when an aggregate follows from this evidence; otherwise the named
    #: status the breakdown published (``MISSING_NO_TRADES``,
    #: ``MISSING_NO_ELIGIBLE_MEASUREMENT``), which declares that no number does.
    aggregate_status: str = "MEASURED"
    #: ``(domain, unrounded value, sample size)`` in the aggregate's summation order.
    domain_values: tuple[tuple[str, float, int], ...] = ()

    @classmethod
    def from_breakdown(cls, breakdown: Mapping[str, Any]) -> ScoreEvidence:
        """The evidence record a breakdown published for its own aggregate."""
        document = breakdown.get("score_evidence")
        if not isinstance(document, Mapping):
            raise ValueError("breakdown carries no score_evidence record")
        return cls.model_validate(document)

    def canon_fields(self) -> list[Any]:
        """Fixed-order fields for the digest payload (never a dict's key order)."""
        return [
            self.total_bars,
            self.total_trades,
            self.abstain_rate,
            self.coverage_factor,
            [[name, value, samples] for name, value, samples in self.domain_values],
            self.hard_invariants_passed,
            self.aggregate_status,
        ]

    def admits_capability_score(self) -> bool:
        """Whether a number follows from this evidence at all."""
        return self.aggregate_status == "MEASURED" and self.coverage_factor is not None and bool(
            self.domain_values
        )


class GateState(StrEnum):
    PASS = "PASS"
    BLOCKED = "BLOCKED"
    UNKNOWN = "UNKNOWN"
    DEFEATED = "DEFEATED"
    NOT_APPLICABLE = "NOT_APPLICABLE"
    MISSING = "MISSING"

    def is_pass(self) -> bool:
        return self == GateState.PASS

    def is_failure(self) -> bool:
        return self in (GateState.BLOCKED, GateState.DEFEATED)

    def failure_class(self) -> str | None:
        if self in (GateState.PASS, GateState.NOT_APPLICABLE):
            return None
        if self in (GateState.BLOCKED, GateState.DEFEATED):
            return "HardFailure"
        if self == GateState.UNKNOWN:
            return "InsufficientEvidence"
        return "MissingEvidence"


#: #435. Named reason code for a ``RequiredBlocking`` gate whose state was never
#: established (``NOT_APPLICABLE``). It is not a measured failure, so it is named
#: apart from ``HardFailure``; it is a blocking precondition that was never met,
#: so it cannot certify or mint either.
REQUIRED_BLOCKING_GATE_UNEVALUATED = "REQUIRED_BLOCKING_GATE_UNEVALUATED"


@dataclass(frozen=True)
class GateDescriptor:
    index: int
    canonical_id: str
    vector_field: str
    requirement: str
    source_clause: str


GATE_DESCRIPTORS: tuple[GateDescriptor, ...] = (
    GateDescriptor(
        index=0,
        canonical_id="G0ConstitutionalIntegrity",
        vector_field="g0_identity",
        requirement="RequiredBlocking",
        source_clause="G0 constitutional/causal integrity (PIT, ChronosGate, determinism, ledger conservation, receipt integrity, non-escalation, synthetic isolation, claim typing): hard fail blocks all.",
    ),
    GateDescriptor(
        index=1,
        canonical_id="G1MeasurementIdentity",
        vector_field="g1_causal_pit",
        requirement="RequiredBlocking",
        source_clause="G1 measurement identity (estimand, data role, lineage, search lineage, cost/execution/world versions, burn marking): hard fail blocks inference.",
    ),
    GateDescriptor(
        index=2,
        canonical_id="G2HistoricalDiagnostic",
        vector_field="g2_determinism_ledger",
        requirement="Required",
        source_clause="G2 historical diagnostic court: outcome is diagnostic state, promotion NONE.",
    ),
    GateDescriptor(
        index=3,
        canonical_id="G3ScenarioRobustness",
        vector_field="g3_scenario_robustness",
        requirement="Required",
        source_clause="G3 scenario robustness: behavioral robustness across the 4 declared market regimes. Unknown stays unknown.",
    ),
    GateDescriptor(
        index=4,
        canonical_id="G4SyntheticFalsification",
        vector_field="g4_structural_robustness",
        requirement="Required",
        source_clause="G4 adversarial/synthetic falsification: PASS mints nothing; FAIL is passport-scoped.",
    ),
    GateDescriptor(
        index=5,
        canonical_id="G5SelectionControl",
        vector_field="g5_statistical_credibility",
        requirement="Required",
        source_clause="G5 selection control: WRC + genuine DSR + SPA remain the active burden ... keeps G5 at NO_ECONOMIC_CLAIM.",
    ),
    GateDescriptor(
        index=6,
        canonical_id="G6FrozenOOSReplication",
        vector_field="g6_protected_oos",
        requirement="Required",
        source_clause="G6 frozen-OOS replication: PASS = bounded replication only.",
    ),
    GateDescriptor(
        index=7,
        canonical_id="G7ProspectiveShadow",
        vector_field="g7_generalization",
        requirement="Required",
        source_clause="G7 prospective shadow succession: `EvaluationEpoch` forward evidence, survival/drift/drawdown, e-process state.",
    ),
    GateDescriptor(
        index=8,
        canonical_id="G8LiveRealization",
        vector_field="g8_prospective_shadow",
        requirement="Required",
        source_clause="G8 live realization: venue-settled fills/costs/settlement/deviation/capacity/incidents. Historical/synthetic never substitutes.",
    ),
    GateDescriptor(
        index=9,
        canonical_id="G9Certificate",
        vector_field="g9_live_realization",
        requirement="Required",
        source_clause="G9 certificate: non-scalar `ProductionEvidenceCertificate` + profile conclusion; scalar collapse forbidden.",
    ),
)


class ReadinessStatus(StrEnum):
    Certified = "READY_NOT_CLAIMED"
    HardFailure = "BLOCKED"
    InsufficientEvidence = "NO_ECONOMIC_CLAIM"


@dataclass(frozen=True)
class GateEvaluation:
    descriptor: GateDescriptor
    state: GateState

    def admits_not_applicable(self) -> bool:
        """Whether this gate's own declared requirement admits NOT_APPLICABLE.

        One source of truth: the descriptor's ``requirement``. A gate declared
        ``RequiredBlocking`` must be *established* (PASS) to hold; the fold clauses
        that legitimately resolve to NOT_APPLICABLE (live realization / prospective
        shadow, D-152 §5) declare ``Required``. Deciding this from the descriptor is
        what keeps "never evaluated" and "verified" distinguishable, instead of a
        global PASS-or-NOT_APPLICABLE OR that certifies an unevaluated blocking
        gate exactly like a fully-PASS vector (#435).
        """
        return self.descriptor.requirement != "RequiredBlocking"

    def holds(self) -> bool:
        if self.state == GateState.PASS:
            return True
        return self.state == GateState.NOT_APPLICABLE and self.admits_not_applicable()

    def refusal_reason(self) -> str | None:
        """Named reason this gate is not established; ``None`` when it holds."""
        if self.holds():
            return None
        if self.state == GateState.NOT_APPLICABLE:
            return (
                f"{REQUIRED_BLOCKING_GATE_UNEVALUATED}: {self.descriptor.canonical_id} "
                f"({self.descriptor.vector_field})=NOT_APPLICABLE, "
                f"requirement={self.descriptor.requirement}"
            )
        return (
            f"{self.descriptor.canonical_id} ({self.descriptor.vector_field})="
            f"{self.state.value}, requirement={self.descriptor.requirement}"
        )


@dataclass(frozen=True)
class ReadinessVerdict:
    evaluations: tuple[GateEvaluation, ...]
    status: ReadinessStatus
    failing_positions: tuple[int, ...]
    hard_failures: tuple[int, ...]
    evidence_gaps: tuple[int, ...]
    #: #435. Positions whose requirement is ``RequiredBlocking`` and whose state was
    #: never established (NOT_APPLICABLE). They block readiness without being
    #: measured failures, so they are named apart from ``hard_failures`` rather than
    #: collapsed into it.
    unevaluated_blocking: tuple[int, ...] = ()

    def status_string(self) -> str:
        return self.status.value

    def blocking_reasons(self) -> tuple[str, ...]:
        """Named reasons this vector cannot certify; empty when it can."""
        reasons = [ev.refusal_reason() for ev in self.evaluations if not ev.holds()]
        return tuple(reason for reason in reasons if reason is not None)


class GateVector(BaseModel):
    model_config = ConfigDict(frozen=True, populate_by_name=True)

    g0_identity: GateState = GateState.MISSING
    g1_causal_pit: GateState = GateState.MISSING
    g2_determinism_ledger: GateState = GateState.MISSING
    #: #495. The cell is published as ``g3_scenario_robustness`` because that is the
    #: quantity the only resolver in the tree measures. It used to be published as
    #: ``g3_benchmark_coverage``, which claimed a benchmark-coverage check no resolver
    #: performed. Entries already written to the append-only ledger were digested under
    #: the old key and are immutable, so the old key is accepted on **read** only: it
    #: lets a stored receipt reproduce the digest it was written under. Nothing is
    #: published under it any more.
    g3_scenario_robustness: GateState = Field(
        default=GateState.MISSING,
        validation_alias=AliasChoices("g3_scenario_robustness", "g3_benchmark_coverage"),
    )
    g4_structural_robustness: GateState = GateState.MISSING
    g5_statistical_credibility: GateState = GateState.MISSING
    g6_protected_oos: GateState = GateState.MISSING
    g7_generalization: GateState = GateState.MISSING
    g8_prospective_shadow: GateState = GateState.MISSING
    g9_live_realization: GateState = GateState.MISSING

    def evaluated_gates(self) -> list[GateEvaluation]:
        ordered_fields = [
            self.g0_identity,
            self.g1_causal_pit,
            self.g2_determinism_ledger,
            self.g3_scenario_robustness,
            self.g4_structural_robustness,
            self.g5_statistical_credibility,
            self.g6_protected_oos,
            self.g7_generalization,
            self.g8_prospective_shadow,
            self.g9_live_realization,
        ]
        return [
            GateEvaluation(descriptor=GATE_DESCRIPTORS[i], state=ordered_fields[i])
            for i in range(10)
        ]

    def all_pass(self) -> bool:
        return all(g.holds() for g in self.evaluated_gates())

    def readiness(self) -> ReadinessVerdict:
        """Adjudicate the vector: a gate holds only if its own requirement is met.

        An unevaluated blocking gate (NOT_APPLICABLE on a ``RequiredBlocking``
        descriptor) is a blocking precondition that was never established. It is
        reported in ``unevaluated_blocking`` — distinct from a measured
        ``HardFailure`` and from an ordinary ``evidence_gap`` — and it blocks
        readiness rather than leaving the vector certifiable (#435).
        """
        evals = self.evaluated_gates()
        failing: list[int] = []
        hard: list[int] = []
        gaps: list[int] = []
        unevaluated_blocking: list[int] = []

        for ev in evals:
            if ev.holds():
                continue
            failing.append(ev.descriptor.index)
            if ev.state == GateState.NOT_APPLICABLE:
                # reachable only for a RequiredBlocking descriptor: for every
                # other requirement this state holds, so it never reaches here
                unevaluated_blocking.append(ev.descriptor.index)
            elif ev.state.failure_class() == "HardFailure":
                hard.append(ev.descriptor.index)
            else:
                gaps.append(ev.descriptor.index)

        if hard or unevaluated_blocking:
            status = ReadinessStatus.HardFailure
        elif not failing:
            status = ReadinessStatus.Certified
        else:
            status = ReadinessStatus.InsufficientEvidence

        return ReadinessVerdict(
            evaluations=tuple(evals),
            status=status,
            failing_positions=tuple(failing),
            hard_failures=tuple(hard),
            evidence_gaps=tuple(gaps),
            unevaluated_blocking=tuple(unevaluated_blocking),
        )


#: #495 R3. Named absence for a G3 cell whose regime partition could not be
#: measured. It is published instead of an empty mapping, so a reader cannot
#: mistake "no regimes measured" for "measured no effect".
G3_SCENARIO_BASIS_ABSENT = "G3_SCENARIO_ROBUSTNESS_PARTITION_NOT_MEASURED"


class G3RegimeMeasurement(BaseModel):
    """What one regime contributed to the G3 measurement (bars, trades, dd, variance)."""

    model_config = ConfigDict(frozen=True)

    bars: int
    trades: int
    max_drawdown: float
    return_variance: float


class G3ScenarioBasis(BaseModel):
    """The measured inputs behind a G3 cell (#495 R3).

    A ``PASS`` in ``gates.g3_scenario_robustness`` is only reproducible from the
    receipt if the regime partition it was measured over travels with it. This
    block is that partition: every evaluated regime and its per-regime numbers,
    plus the named reason for the state. When no partition could be measured the
    block carries :data:`G3_SCENARIO_BASIS_ABSENT` instead of an empty mapping.

    The block is published beside the digest, not inside any canon: stored
    ledger receipts keep their bytes and digests (D-164), and a new receipt's
    basis cannot change the meaning of the state it accompanies. The producer
    still refuses a non-``PASS`` basis that names no absence and a ``PASS`` basis
    with no measured regimes.
    """

    model_config = ConfigDict(frozen=True)

    state: GateState
    regimes: dict[str, G3RegimeMeasurement] = Field(default_factory=dict)
    evaluated_regimes: tuple[str, ...] = ()
    reason: str | None = None
    absence: str | None = None
    max_observed_drawdown: float | None = None
    max_observed_variance: float | None = None
    max_drawdown_limit: float | None = None
    min_trades_per_regime: int | None = None
    min_regime_trades: int | None = None
    empty_regimes: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _state_and_basis_travel_together(self) -> G3ScenarioBasis:
        if self.state is GateState.PASS and (self.absence is not None or not self.regimes):
            raise ValueError(
                "G3 PASS requires a measured regime basis; a PASS with no basis is not "
                "reproducible from the receipt"
            )
        if self.absence is not None and self.regimes:
            raise ValueError("G3 basis cannot be both absent and measured")
        if not self.regimes and self.absence is None:
            raise ValueError(
                "G3 basis with no measured regimes must name its absence "
                f"({G3_SCENARIO_BASIS_ABSENT})"
            )
        return self

    @classmethod
    def from_metrics(
        cls, state: GateState, metrics: Mapping[str, Any]
    ) -> G3ScenarioBasis:
        """Build the typed basis from :func:`evaluate_g3_scenario_robustness` output."""
        raw = metrics.get("regimes") or {}
        regimes = {
            str(name): G3RegimeMeasurement(
                bars=int(row["bars"]),
                trades=int(row["trades"]),
                max_drawdown=float(row["max_drawdown"]),
                return_variance=float(row["return_variance"]),
            )
            for name, row in raw.items()
        }
        measured = bool(regimes)
        reason = metrics.get("reason")
        return cls(
            state=state,
            regimes=regimes,
            evaluated_regimes=tuple(sorted(regimes)),
            reason=None if reason is None else str(reason),
            absence=None if measured else G3_SCENARIO_BASIS_ABSENT,
            max_observed_drawdown=(
                float(metrics["max_observed_drawdown"]) if measured else None
            ),
            max_observed_variance=(
                float(metrics["max_observed_variance"]) if measured else None
            ),
            max_drawdown_limit=(
                float(metrics["max_drawdown_limit"]) if measured else None
            ),
            min_trades_per_regime=(
                int(metrics["min_trades_per_regime"]) if measured else None
            ),
            min_regime_trades=(int(metrics["min_regime_trades"]) if measured else None),
            empty_regimes=tuple(str(n) for n in metrics.get("empty_regimes") or ()),
        )


def build_canon_payload(
    *,
    digest_version: str,
    case_id: str,
    policy_id: str,
    capability_score: float | None,
    coverage_factor: float | None,
    gates: GateVector,
    artifact_bindings: Sequence[ArtifactBinding],
    computed_at_timestamp_ns: int,
    economic_evidence_digest: str,
    economic_receipt_path: str,
    input_binding: str,
    window_evidence: WindowEvidence | None = None,
    score_evidence: ScoreEvidence | None = None,
) -> tuple[list[Any] | None, str | None]:
    """Canonical payload for a version's PROVEN layout: ``(canon, refusal_reason)``.

    One source of truth for both creation and verification, so a receipt can
    never be written under one layout and read back under another. Refusal
    reasons are explicit; a receipt is never hashed under a guessed layout.
    """
    canon = RECEIPT_CANON_TABLE.get(digest_version)
    if canon is None:
        return None, f"UNSUPPORTED_DIGEST_VERSION: {digest_version}"
    sorted_bindings = sorted(artifact_bindings, key=lambda b: (b.role, b.path))
    payload: list[Any] = [
        "BenchmarkReceipt",
        digest_version,
        case_id,
        policy_id,
        None if capability_score is None else round(capability_score, 8),
        None if coverage_factor is None else round(coverage_factor, 4),
        [getattr(gates, f).value for f in sorted(gates.__class__.model_fields)],
        [[b.role, b.sha256_hex, b.bytes] for b in sorted_bindings],
        computed_at_timestamp_ns,
    ]
    pair = [economic_evidence_digest, economic_receipt_path]
    if canon.economic_pair == "always_present":
        payload.extend(pair)
    elif canon.economic_pair == "present":
        if not any(pair):
            return None, (
                f"CANON_SHAPE_UNPROVEN: {digest_version} was never observed with an "
                "empty economic pair"
            )
        payload.extend(pair)
    elif any(pair):
        return None, (
            f"CANON_SHAPE_UNPROVEN: {digest_version} was never observed with a "
            "populated economic pair"
        )
    if canon.input_binding == "always_present":
        payload.append(input_binding)
    elif input_binding:
        return None, (
            f"CANON_SHAPE_UNPROVEN: {digest_version} was never observed with an input_binding"
        )
    #: #444. The evidence class of the producing window is part of the digest from
    #: v6 on: a capability score can be checked against the class that minted it.
    if canon.window_evidence == "always_present":
        payload.append(None if window_evidence is None else window_evidence.canon_fields())
    elif window_evidence is not None:
        return None, (
            f"CANON_SHAPE_UNPROVEN: {digest_version} was never observed with a "
            "window evidence record"
        )
    #: #408. The determinants of the published capability score are part of the
    #: digest from v7 on: a number can be rechecked against the evidence that is
    #: supposed to produce it, instead of being taken on the receipt's word.
    if canon.score_evidence == "always_present":
        payload.append(None if score_evidence is None else score_evidence.canon_fields())
    elif score_evidence is not None:
        return None, (
            f"CANON_SHAPE_UNPROVEN: {digest_version} was never observed with a "
            "score evidence record"
        )
    return payload, None


def bound_evidence_fingerprint(receipt: BenchmarkReceipt) -> str | None:
    """sha256 of a receipt's canonical payload with the published number nulled (#408).

    Everything else the version binds is kept, so two receipts with the same
    fingerprint are bound to byte-identical evidence -- the same case, the same
    policy, the same gate vector, the same artifacts by sha256, the same window
    class, and (from v7) the same score determinants. Such a pair may not publish
    different capability scores: the number is a function of the evidence, not an
    independent field.

    ``None`` when the version's payload cannot be built at all (the entry's own
    digest verdict names that; this pass adds nothing to it).
    """
    canon, _ = build_canon_payload(
        digest_version=receipt.digest_version,
        case_id=receipt.case_id,
        policy_id=receipt.policy_id,
        capability_score=None,
        coverage_factor=receipt.coverage_factor,
        gates=receipt.gates,
        artifact_bindings=receipt.artifact_bindings,
        computed_at_timestamp_ns=receipt.computed_at_timestamp_ns,
        economic_evidence_digest=receipt.economic_evidence_digest,
        economic_receipt_path=receipt.economic_receipt_path,
        input_binding=receipt.input_binding,
        window_evidence=receipt.window_evidence,
        score_evidence=receipt.score_evidence,
    )
    if canon is None:
        return None
    return hashlib.sha256(json.dumps(canon, separators=(",", ":")).encode()).hexdigest()


def _window_end_refusal(
    *, declared_ns: int | None, digested_ns: int, identity: str
) -> str | None:
    """Named reason a declared window end may not be published; ``None`` when it may (#446).

    One implementation for both the write path (:meth:`BenchmarkReceipt.create`) and the
    read path (:meth:`BenchmarkReceipt.window_end_refusal_reason`), so a record cannot be
    written under one rule and published under another.
    """
    if declared_ns is None:
        return (
            f"{WINDOW_END_UNDECLARED}: {identity} stores "
            f"computed_at_timestamp_ns={digested_ns} but declares no "
            "window_end_timestamp_ns, so the record itself does not say which quantity that "
            "reading is; it is named as undeclared and is not published as a window end"
        )
    if declared_ns != digested_ns:
        return (
            f"{WINDOW_END_INCONSISTENT}: declared window_end_timestamp_ns={declared_ns} differs "
            "from the window end carried inside the digest "
            f"(computed_at_timestamp_ns={digested_ns}); the record states one quantity twice "
            "with two different values"
        )
    if declared_ns < TIME_EPOCH_FLOOR_NS:
        return (
            f"{WINDOW_END_MISSCALED}: declared window_end_timestamp_ns={declared_ns} is below "
            f"the {TIME_FIELD_UNIT} floor {TIME_EPOCH_FLOOR_NS} (2020-01-01T00:00:00Z); a "
            "reading that renders as a 1970 date is a scale error and is not published"
        )
    return None


def _run_time_refusal(
    *,
    declared_ns: int | None,
    window_end_ns: int | None,
    digested_ns: int,
    identity: str,
    now_ns: int | None = None,
) -> str | None:
    """Named reason a declared run time may not be published; ``None`` when it may (#446).

    Three shapes, named apart: nothing was measured (``RUN_TIME_UNMEASURED``), the reading
    is not a plausible clock reading (``RUN_TIME_OUT_OF_RANGE``), or it precedes the window
    it claims to have measured (``RUN_TIME_PRECEDES_WINDOW_END``).
    """
    if declared_ns is None:
        return (
            f"{RUN_TIME_UNMEASURED}: {identity} carries no run_time_timestamp_ns; the record "
            "does not say when the run happened, and the window end it stores is a different "
            "quantity, not a run time"
        )
    clock = time.time_ns() if now_ns is None else now_ns
    ceiling = clock + MAX_CLOCK_SKEW_NS
    if declared_ns < TIME_EPOCH_FLOOR_NS or declared_ns > ceiling:
        return (
            f"{RUN_TIME_OUT_OF_RANGE}: run_time_timestamp_ns={declared_ns} is outside "
            f"[{TIME_EPOCH_FLOOR_NS}, {ceiling}] in {TIME_FIELD_UNIT}; a reading this far from "
            "the reading clock is refused instead of being published as a run time"
        )
    # Order the run against the window it measured: the declared window end when the record
    # has one, else the digested reading -- and only when that reading is itself plausible,
    # otherwise there is no window to order against and nothing is claimed.
    order_against = window_end_ns
    if order_against is None or order_against < TIME_EPOCH_FLOOR_NS:
        order_against = digested_ns if digested_ns >= TIME_EPOCH_FLOOR_NS else None
    if order_against is not None and declared_ns < order_against:
        return (
            f"{RUN_TIME_PRECEDES_WINDOW_END}: run_time_timestamp_ns={declared_ns} is earlier "
            f"than the window end {order_against} this run measured"
        )
    return None


class BenchmarkReceipt(BaseModel):
    """Self-verifying cryptographic benchmark receipt."""

    model_config = ConfigDict(frozen=True)

    case_id: str
    policy_id: str
    digest_version: str = RECEIPT_DIGEST_VERSION
    #: ``None`` means the run had no eligible measurement; a missing score is
    #: reported as missing rather than as a zero (NX08.R1/R2).
    capability_score: float | None = None
    #: ``None`` means the run had no eligible measurement; it is not zero coverage
    #: and it is not a fabricated 0.60 (NX08.R1).
    coverage_factor: float | None = None
    #: Side-by-side scorer versions (NX08.R5). Informational: deliberately NOT part
    #: of the digest payload, so existing receipts keep verifying under their own
    #: version instead of being re-hashed.
    scoring_versions: dict[str, Any] = Field(default_factory=dict)
    gates: GateVector
    #: #495 R3. The measured inputs behind a non-default ``gates.g3_scenario_robustness``:
    #: the regime partition that was evaluated, its per-regime measurements and the named
    #: reason for the state. It travels *beside* every digest canon, exactly like
    #: ``scoring_versions`` and the two time fields, so stored ledger receipts keep their
    #: bytes and digests (D-164) while a new receipt's PASS is reproducible from itself.
    #: ``None`` means the producer supplied no G3 measurement at all (the rule's bare
    #: default); it is never an empty block standing for "measured nothing".
    g3_scenario_robustness_basis: G3ScenarioBasis | None = None
    artifact_bindings: tuple[ArtifactBinding, ...] = ()
    computed_at_timestamp_ns: int
    receipt_digest: str = ""
    economic_evidence_digest: str = ""
    economic_receipt_path: str = ""
    # Canonical input identity bound into the digest (v4): tape/data digest,
    # strategy config, bar count/span, capital assumptions. Any input
    # substitution changes this binding and fails verification.
    input_binding: str = ""
    #: The evidence class of the window that produced these numbers (#444), bound
    #: into the digest from ``v8.5-digest-v6`` on. ``None`` means the receipt
    #: carries no window class at all: true for records written before v6 (which
    #: had no such field) and for callers that do not declare a window. A declared
    #: non-evidential class cannot carry a capability score (see :meth:`verify`).
    window_evidence: WindowEvidence | None = None
    #: The determinants of ``capability_score`` (#408), bound into the digest from
    #: ``v8.5-digest-v7`` on. ``None`` means no evidence record is bound: true for
    #: records written before v7 (which had no such field) and for receipts that
    #: publish no number. A numeric score without this record cannot be minted or
    #: verified (see :meth:`capability_score_refusal_reason`).
    score_evidence: ScoreEvidence | None = None
    #: #446. The two time quantities one entry carries, named apart -- the end of the data
    #: window the run measured and the wall clock of the run itself. The ledger used to
    #: carry only ``computed_at_timestamp_ns``, which every producer filled with the window
    #: end, so the published ledger could not say when any run happened and the product
    #: surface republished a 2025 window end as the 2026 run's date. Neither field enters
    #: any digest canon: a wall clock inside ``receipt_digest`` would make the same inputs
    #: hash differently on every run, which is the determinism (G2) the receipt chain exists
    #: to prove. They travel *beside* the digest and are published only through
    #: :meth:`time_publication`, which refuses each quantity by name rather than substituting
    #: the other for it.
    #:
    #: ``None`` means the record declares no such quantity -- true for every entry written
    #: before this field existed. Those entries keep their own bytes, digest and parent hash,
    #: and a reader names their time as unmeasured/undeclared instead of inferring one.
    window_end_timestamp_ns: int | None = None
    run_time_timestamp_ns: int | None = None

    @classmethod
    def create(
        cls,
        case_id: str,
        policy_id: str,
        capability_score: float | None,
        gates: GateVector,
        computed_at_timestamp_ns: int,
        coverage_factor: float | None = None,
        artifact_bindings: Sequence[ArtifactBinding] = (),
        economic_evidence_digest: str = "",
        economic_receipt_path: str = "",
        input_binding: str = "",
        window_evidence: WindowEvidence | None = None,
        scoring_versions: dict[str, Any] | None = None,
        score_evidence: ScoreEvidence | None = None,
        window_end_timestamp_ns: int | None = None,
        run_time_timestamp_ns: int | None = None,
        g3_scenario_robustness_basis: G3ScenarioBasis | None = None,
    ) -> BenchmarkReceipt:
        """Build a receipt whose published number follows from the evidence it binds.

        #408: the score is *derived*, not declared. A caller may declare the measured
        number (it is then checked against the evidence), or publish no number at all
        -- ``None`` means "this receipt makes no claim", which is how a window class
        that may not mint a score is honoured (#444). What a caller may not do is
        store a number its own evidence does not produce
        (``CAPABILITY_SCORE_NOT_RECOMPUTABLE``) or a number with no evidence at all
        (``CAPABILITY_SCORE_UNBOUND_TO_EVIDENCE``). The defect shape -- two receipts
        bound to identical evidence publishing different numbers -- is therefore not
        constructible on this path at all.
        """
        if score_evidence is None:
            if capability_score is not None:
                raise ValueError(
                    f"cannot create receipt: {CAPABILITY_SCORE_UNBOUND_TO_EVIDENCE}: "
                    f"capability_score={capability_score} with no score evidence bound; a "
                    "published number must be a function of the evidence the receipt carries"
                )
        else:
            from v8_next.evaluation.scoring import recompute_capability_score

            if capability_score is not None:
                recomputed = recompute_capability_score(score_evidence)
                if recomputed is None or recomputed != capability_score:
                    raise ValueError(
                        f"cannot create receipt: {CAPABILITY_SCORE_NOT_RECOMPUTABLE}: declared "
                        f"capability_score={capability_score}, recomputed from the supplied "
                        f"evidence={recomputed}"
                    )
            if coverage_factor is None:
                coverage_factor = score_evidence.coverage_factor
            elif coverage_factor != score_evidence.coverage_factor:
                raise ValueError(
                    f"cannot create receipt: {CAPABILITY_SCORE_EVIDENCE_INCONSISTENT}: "
                    f"coverage_factor={coverage_factor} contradicts the supplied evidence "
                    f"({score_evidence.coverage_factor})"
                )
        # #446. A *declared* time quantity is adjudicated before the receipt is built, so a
        # producer cannot write a record whose own chronology is self-contradicting, a
        # constant, or mis-scaled (the three shapes a "time" field degenerates into when
        # nobody measures it). Undeclared (``None``) stays a legal write: the record then
        # claims nothing about that quantity, and the reader refuses it by name
        # (``RUN_TIME_UNMEASURED`` / ``WINDOW_END_UNDECLARED``) instead of publishing a
        # substitute for it -- which is how the records stored before this field existed
        # keep their meaning and their bytes.
        identity = f"receipt for case {case_id!r} (policy {policy_id!r}, {RECEIPT_DIGEST_VERSION})"
        declared_time_refusals = (
            []
            if window_end_timestamp_ns is None
            else [
                _window_end_refusal(
                    declared_ns=window_end_timestamp_ns,
                    digested_ns=computed_at_timestamp_ns,
                    identity=identity,
                )
            ]
        )
        if run_time_timestamp_ns is not None:
            declared_time_refusals.append(
                _run_time_refusal(
                    declared_ns=run_time_timestamp_ns,
                    window_end_ns=window_end_timestamp_ns,
                    digested_ns=computed_at_timestamp_ns,
                    identity=identity,
                )
            )
        for time_refusal in declared_time_refusals:
            if time_refusal is not None:
                raise ValueError(f"cannot create receipt: {time_refusal}")

        if g3_scenario_robustness_basis is not None and (
            g3_scenario_robustness_basis.state is not gates.g3_scenario_robustness
        ):
            raise ValueError(
                "cannot create receipt: G3 basis contradicts the published cell: "
                f"basis state={g3_scenario_robustness_basis.state.value}, "
                f"gates.g3_scenario_robustness={gates.g3_scenario_robustness.value}"
            )

        sorted_bindings = sorted(artifact_bindings, key=lambda b: (b.role, b.path))
        canon, refusal = build_canon_payload(
            digest_version=RECEIPT_DIGEST_VERSION,
            case_id=case_id,
            policy_id=policy_id,
            capability_score=capability_score,
            coverage_factor=coverage_factor,
            gates=gates,
            artifact_bindings=artifact_bindings,
            computed_at_timestamp_ns=computed_at_timestamp_ns,
            economic_evidence_digest=economic_evidence_digest,
            economic_receipt_path=economic_receipt_path,
            input_binding=input_binding,
            window_evidence=window_evidence,
            score_evidence=score_evidence,
        )
        if canon is None:
            raise ValueError(f"cannot create receipt: {refusal}")
        digest = hashlib.sha256(json.dumps(canon, separators=(",", ":")).encode()).hexdigest()

        return cls(
            case_id=case_id,
            policy_id=policy_id,
            digest_version=RECEIPT_DIGEST_VERSION,
            capability_score=capability_score,
            coverage_factor=coverage_factor,
            gates=gates,
            artifact_bindings=tuple(sorted_bindings),
            computed_at_timestamp_ns=computed_at_timestamp_ns,
            receipt_digest=digest,
            economic_evidence_digest=economic_evidence_digest,
            economic_receipt_path=economic_receipt_path,
            input_binding=input_binding,
            window_evidence=window_evidence,
            score_evidence=score_evidence,
            scoring_versions=dict(scoring_versions or {}),
            window_end_timestamp_ns=window_end_timestamp_ns,
            run_time_timestamp_ns=run_time_timestamp_ns,
            g3_scenario_robustness_basis=g3_scenario_robustness_basis,
        )

    def window_refusal_reason(self) -> str | None:
        """Named refusal when the declared window class cannot support the claims.

        #444: a window that proves no economic evidence (``profile=smoke``) is
        liveness/mechanics evidence, so a numeric capability score on such a
        receipt is a minted claim the window never supported. The class is carried
        *inside* the digest, so it cannot be dropped after the number was minted.
        """
        evidence = self.window_evidence
        if evidence is None or evidence.admits_capability_score():
            return None
        if self.capability_score is None:
            return None
        return (
            f"{NON_EVIDENTIAL_WINDOW_CAPABILITY_SCORE}: window profile="
            f"{evidence.profile!r} evidence_class={evidence.evidence_class!r} proves no "
            f"economic evidence, so it may not mint capability_score={self.capability_score}"
        )

    def evidence_class(self) -> str:
        """The evidence class this receipt declares, named (#448).

        ``EVIDENCE_CLASS_UNDECLARED`` for a receipt that carries no window evidence
        record: an entry written before the class existed cannot be shown to be
        evidential, so it is named as undeclared instead of being assumed to be one.
        """
        evidence = self.window_evidence
        return EVIDENCE_CLASS_UNDECLARED if evidence is None else evidence.evidence_class

    def declares_evidential_window(self) -> bool:
        """Whether this receipt's *own* declaration admits a published capability score.

        One source of truth for the reader rule (#448): the class a score must have been
        minted under is the one the receipt itself carries, never a caller's flag and
        never a default.
        """
        evidence = self.window_evidence
        return evidence is not None and evidence.economic_evidence

    def window_end_refusal_reason(self) -> str | None:
        """Named reason this receipt's window end may not be published; ``None`` when it may.

        #446. The end of the measured window is the *only* quantity
        ``computed_at_timestamp_ns`` ever carried, so what a record written before this field
        existed lacks is not the reading but the declaration of what the reading is. It is
        named :data:`WINDOW_END_UNDECLARED` -- never assumed, and above all never published
        as a run time, which is the defect this refusal closes.
        """
        return _window_end_refusal(
            declared_ns=self.window_end_timestamp_ns,
            digested_ns=self.computed_at_timestamp_ns,
            identity=self._time_identity(),
        )

    def run_time_refusal_reason(self, *, now_ns: int | None = None) -> str | None:
        """Named reason this receipt's run time may not be published; ``None`` when it may.

        #446. Every record written before the run-time field existed is
        :data:`RUN_TIME_UNMEASURED`: the run's own clock was never recorded, and the window
        end the record stores is a different quantity, not a substitute for it.
        """
        return _run_time_refusal(
            declared_ns=self.run_time_timestamp_ns,
            window_end_ns=self.window_end_timestamp_ns,
            digested_ns=self.computed_at_timestamp_ns,
            identity=self._time_identity(),
            now_ns=now_ns,
        )

    def time_publication(self, *, now_ns: int | None = None) -> TimePublication:
        """The entry's two time quantities, each published or refused by name (#446).

        One source of truth for readers (``tools/generate_status.py``): the run's own wall
        clock, and the end of the window it measured, as two named and separately refused
        quantities. A consumer that wants "when did this run happen" reads
        :attr:`TimePublication.run_time_ns` and, when it is ``None``, publishes the refusal
        name -- it does not fall back to the window end.
        """
        run_refusal = self.run_time_refusal_reason(now_ns=now_ns)
        window_refusal = self.window_end_refusal_reason()
        return TimePublication(
            run_time_ns=None if run_refusal else self.run_time_timestamp_ns,
            run_time_refusal=run_refusal or "",
            window_end_ns=None if window_refusal else self.window_end_timestamp_ns,
            window_end_refusal=window_refusal or "",
        )

    def published_run_time_ns(self, *, now_ns: int | None = None) -> int | None:
        """The run's own wall clock, or ``None`` when this record does not declare one."""
        return self.time_publication(now_ns=now_ns).run_time_ns

    def _time_identity(self) -> str:
        """How a time refusal names the record it refused."""
        return f"receipt {self.receipt_digest} (digest_version={self.digest_version})"

    def score_publication_refusal_reason(self) -> str | None:
        """Named reason this receipt's number may not be *published*; ``None`` when it may.

        Distinct from :meth:`window_refusal_reason` (#444), which adjudicates whether the
        receipt *verifies*: a pre-v6 record is not a tamper, it simply never declared a
        class, and its digest still certifies under its own version. What it cannot do is
        have its number read as a capability score, because nothing in the record says the
        window that produced it was allowed to mint one. Named shapes, one per distinct
        reason -- the #490 measurement shape is the third:

        * the receipt declares no window class at all (``EVIDENCE_CLASS_UNDECLARED``);
        * the declared class proves no economic evidence
          (``NON_EVIDENTIAL_WINDOW_CAPABILITY_SCORE``, the same name #444 refuses with);
        * the declared class is evidential but the record carries no measured bar count
          (``EVIDENTIAL_WINDOW_BARS_UNMEASURED``) -- a number whose sample size is
          unknown is not a measurement class;
        * the receipt publishes no number -- there is nothing to withhold, so no refusal.
        """
        if self.capability_score is None:
            return None
        evidence = self.window_evidence
        if evidence is None:
            return (
                f"{EVIDENCE_CLASS_UNDECLARED}: receipt {self.receipt_digest} "
                f"(digest_version={self.digest_version}) publishes a capability score and "
                "declares no window evidence class; a capability score is published only from "
                "an entry that declares window_evidence.economic_evidence=true, so the stored "
                "number is refused and is not repeated here"
            )
        if not evidence.economic_evidence:
            return (
                f"{NON_EVIDENTIAL_WINDOW_CAPABILITY_SCORE}: receipt {self.receipt_digest} was "
                f"minted by a {evidence.evidence_class!r} window "
                f"(profile={evidence.profile!r}) that proves no economic evidence; its recorded "
                "capability score is refused and is not published"
            )
        if evidence.bars is None:
            # #490. No imputation and no coercion to 0: an unrecorded sample size is
            # named, not filled in. The number itself is deliberately absent from this
            # text (#448.A1 -- a refusal never republishes what it refuses).
            return (
                f"{EVIDENTIAL_WINDOW_BARS_UNMEASURED}: receipt {self.receipt_digest} "
                f"(digest_version={self.digest_version}) was minted by an evidential window "
                f"(profile={evidence.profile!r}, evidence_class={evidence.evidence_class!r}) "
                "that records no measured bar count; with no sample size bound into the "
                "record its capability score is refused and is not published"
            )
        return None

    def capability_score_refusal_reason(self) -> str | None:
        """Named refusal when the number does not follow from the bound evidence (#408).

        A receipt that agrees with itself -- its digest covers the number it stores
        -- is still not evidence *for* that number: the number has to be produced by
        the evidence the receipt binds, and this is where that is rechecked by
        re-deriving it. Every shape gets its own name, so "does not follow" and
        "nothing bound to follow" are never the same verdict.
        """
        if self.capability_score is None:
            return None
        evidence = self.score_evidence
        if evidence is None:
            return (
                f"{CAPABILITY_SCORE_UNBOUND_TO_EVIDENCE}: receipt publishes "
                f"capability_score={self.capability_score} and binds no score evidence "
                f"(digest_version={self.digest_version})"
            )
        if self.coverage_factor != evidence.coverage_factor:
            return (
                f"{CAPABILITY_SCORE_EVIDENCE_INCONSISTENT}: published coverage_factor="
                f"{self.coverage_factor} contradicts the bound score evidence "
                f"({evidence.coverage_factor})"
            )
        # imported here, not at module scope: the scoring module depends on this one
        # (its aggregates are the arithmetic this refusal re-derives), so the
        # dependency is one-way by construction.
        from v8_next.evaluation.scoring import recompute_capability_score

        recomputed = recompute_capability_score(evidence)
        if recomputed is None or recomputed != self.capability_score:
            return (
                f"{CAPABILITY_SCORE_NOT_RECOMPUTABLE}: stored capability_score="
                f"{self.capability_score}, recomputed from the bound evidence={recomputed}"
            )
        return None

    def verify(self) -> tuple[bool, str]:
        """Recompute cryptographic digest and verify against attached artifacts."""
        # 0. A claim the window class cannot support is refused by name before any
        #    hash is recomputed: a receipt that agrees with itself is still not
        #    evidence that a smoke window minted a capability score (#444).
        claim_refusal = self.window_refusal_reason()
        if claim_refusal is not None:
            return False, claim_refusal
        # 0b. Same rule for the number's own determinants (#408): the published
        #     score is re-derived from the evidence bound to this receipt.
        score_refusal = self.capability_score_refusal_reason()
        if score_refusal is not None:
            return False, score_refusal
        # 1. Recompute the digest under the version's PROVEN canon. Historical
        # versions are not silently reinterpreted: an unproven shape is refused
        # by name instead of being hashed under a guessed layout.
        canon, refusal = build_canon_payload(
            digest_version=self.digest_version,
            case_id=self.case_id,
            policy_id=self.policy_id,
            capability_score=self.capability_score,
            coverage_factor=self.coverage_factor,
            gates=self.gates,
            artifact_bindings=self.artifact_bindings,
            computed_at_timestamp_ns=self.computed_at_timestamp_ns,
            economic_evidence_digest=self.economic_evidence_digest,
            economic_receipt_path=self.economic_receipt_path,
            input_binding=self.input_binding,
            window_evidence=self.window_evidence,
            score_evidence=self.score_evidence,
        )
        if canon is None:
            return False, str(refusal)
        expected_digest = hashlib.sha256(json.dumps(canon, separators=(",", ":")).encode()).hexdigest()
        if expected_digest != self.receipt_digest:
            return False, f"DIGEST_TAMPERED: expected {expected_digest}, stored {self.receipt_digest}"

        # 2. Check artifact bindings on disk
        for b in self.artifact_bindings:
            ok, err = b.verify()
            if not ok:
                return False, f"ARTIFACT_TAMPERED: [{b.role}] {err}"

        return True, "OK"

    def verify_digest(self) -> tuple[bool, str]:
        """Digest-only verdict (no artifact I/O); used by the ledger report.

        Only the digest is adjudicated here. The window-class claim check lives in
        :meth:`verify` (and on the append path through it), so a report can still
        say "the bytes are the bytes" without conflating that with "the claims
        hold".
        """
        canon, refusal = build_canon_payload(
            digest_version=self.digest_version,
            case_id=self.case_id,
            policy_id=self.policy_id,
            capability_score=self.capability_score,
            coverage_factor=self.coverage_factor,
            gates=self.gates,
            artifact_bindings=self.artifact_bindings,
            computed_at_timestamp_ns=self.computed_at_timestamp_ns,
            economic_evidence_digest=self.economic_evidence_digest,
            economic_receipt_path=self.economic_receipt_path,
            input_binding=self.input_binding,
            window_evidence=self.window_evidence,
            score_evidence=self.score_evidence,
        )
        if canon is None:
            return False, str(refusal)
        expected = hashlib.sha256(json.dumps(canon, separators=(",", ":")).encode()).hexdigest()
        if expected != self.receipt_digest:
            return False, f"DIGEST_TAMPERED: expected {expected}, stored {self.receipt_digest}"
        return True, "OK"


class LedgerEntry(BaseModel):
    model_config = ConfigDict(frozen=True)

    sequence_number: int
    parent_entry_hash: str
    receipt: BenchmarkReceipt
    entry_hash: str

    @classmethod
    def create(
        cls,
        sequence_number: int,
        parent_entry_hash: str,
        receipt: BenchmarkReceipt,
    ) -> LedgerEntry:
        canon = [
            sequence_number,
            parent_entry_hash,
            receipt.receipt_digest,
        ]
        entry_hash = hashlib.sha256(json.dumps(canon, separators=(",", ":")).encode()).hexdigest()
        return cls(
            sequence_number=sequence_number,
            parent_entry_hash=parent_entry_hash,
            receipt=receipt,
            entry_hash=entry_hash,
        )


@dataclass(frozen=True)
class Publication:
    """What a latest-benchmark reader may publish, and the class it read (#448).

    ``entry`` is the most recently appended entry whose receipt declares an evidential
    window class; ``None`` when the ledger holds no such entry. ``latest_entry`` is
    exactly what the previous rule took (the newest appended entry) and is kept only so
    the refusal can name what it refused: its sequence, its ``entry_hash`` and its
    class. Order plays no part beyond "most recent evidential": an evidential entry
    appended before a non-evidential one is still the one selected, so a later smoke
    run can never displace the measurement it followed.

    ``refusal_reason`` is empty when a score may be published, and otherwise names the
    reason it may not: :data:`NO_EVIDENTIAL_LEDGER_ENTRY` when no entry declares an
    evidential window, or the selected receipt's own publication refusal when what it
    declares is not enough to publish a number from (e.g.
    :data:`EVIDENTIAL_WINDOW_BARS_UNMEASURED`, #490).
    """

    entry: LedgerEntry | None
    latest_entry: LedgerEntry | None
    refusal_reason: str = ""

    @property
    def publishes_capability_score(self) -> bool:
        """Whether this read has an evidential entry to publish a number from."""
        return self.entry is not None and not self.refusal_reason

    @property
    def entry_hash(self) -> str:
        """Identity of the entry this read selected (``""`` when it selected none)."""
        return self.entry.entry_hash if self.entry is not None else ""

    @property
    def sequence_number(self) -> int | None:
        return self.entry.sequence_number if self.entry is not None else None

    @property
    def evidence_class(self) -> str:
        """The class of the entry in hand -- the selected one, else the refused newest."""
        read = self.entry if self.entry is not None else self.latest_entry
        return EVIDENCE_CLASS_UNDECLARED if read is None else read.receipt.evidence_class()

    @property
    def capability_score(self) -> float | None:
        """The number this read publishes; ``None`` when it refuses or holds no number."""
        if not self.publishes_capability_score:
            return None
        assert self.entry is not None
        return self.entry.receipt.capability_score

    def as_dict(self) -> dict[str, Any]:
        """The read as data, for a report that has to name what it used."""
        newest = self.latest_entry
        return {
            "publishes_capability_score": self.publishes_capability_score,
            "evidence_class": self.evidence_class,
            "capability_score": self.capability_score,
            "entry_hash": self.entry_hash,
            "sequence_number": self.sequence_number,
            #: what the newest appended entry is, whether or not it was selected
            "latest_entry_hash": "" if newest is None else newest.entry_hash,
            "latest_sequence_number": None if newest is None else newest.sequence_number,
            "latest_evidence_class": (
                EVIDENCE_CLASS_UNDECLARED if newest is None else newest.receipt.evidence_class()
            ),
            "latest_case_id": None if newest is None else newest.receipt.case_id,
            "latest_policy_id": None if newest is None else newest.receipt.policy_id,
            #: #448: whether the refused entry recorded a number at all. The number itself
            #: is deliberately not written here: a recorded value is not a published
            #: measurement, and the ledger keeps the record.
            "latest_records_capability_score": (
                None if newest is None else newest.receipt.capability_score is not None
            ),
            "refusal_reason": self.refusal_reason,
        }

    def describe(self) -> str:
        """One line naming the entry this read used and the class of the one it read."""
        if self.entry is not None:
            receipt = self.entry.receipt
            return (
                f"evidence class: {self.evidence_class}  entry: seq "
                f"{self.entry.sequence_number} {self.entry.entry_hash} "
                f"(case={receipt.case_id} policy={receipt.policy_id} "
                f"digest_version={receipt.digest_version})"
            )
        newest = self.latest_entry
        if newest is None:
            return f"evidence class: {self.evidence_class}  entry: none (ledger empty)"
        receipt = newest.receipt
        return (
            f"evidence class: {self.evidence_class} (newest entry, refused)  entry: seq "
            f"{newest.sequence_number} {newest.entry_hash} (case={receipt.case_id} "
            f"policy={receipt.policy_id} digest_version={receipt.digest_version})"
        )


@dataclass(frozen=True)
class EntryVerification:
    """Per-entry verdict with the three questions kept apart (NX04.R4).

    A valid hash chain and intact artifacts are different claims: an entry whose
    chain link is sound but whose bound artifact is missing is NOT reported as a
    success, and neither failure is allowed to mask the other.

    ``score_binding`` (#408) is the fourth claim, kept apart for the same reason: a
    published capability score that is not a function of the evidence bound to the
    entry is a claim that does not hold, and naming it must not masquerade as a
    broken byte history -- the chain, the digest and the artifacts are adjudicated
    exactly as before and reported beside it.
    """

    sequence_number: int
    digest_version: str
    digest: str  # OK | DIGEST_TAMPERED | CANON_SHAPE_UNPROVEN | UNSUPPORTED_DIGEST_VERSION
    digest_detail: str
    chain: str  # OK | SEQUENCE_GAP | PARENT_HASH_MISMATCH | ENTRY_HASH_TAMPERED
    artifacts: str  # OK | NO_BINDINGS | MISSING | TAMPERED
    artifact_detail: str = ""
    #: OK | NOT_APPLICABLE (no number published) | a named CAPABILITY_SCORE_* refusal.
    score_binding: str = "NOT_APPLICABLE"
    score_binding_detail: str = ""
    #: #480. Whether the refusal is still *actionable* against this entry's own
    #: version contract: from ``v8.5-digest-v7`` the canon binds the determinants of
    #: the published number, so a number stored apart from them -- or two such
    #: entries whose identical bound evidence publishes different numbers -- is a
    #: live claim defect that a new receipt can still repair. A ``v2``-``v6`` entry
    #: never bound those determinants at all: its refusal is still named (the claim
    #: really does not follow), but the history cannot be re-derived without
    #: rewriting bytes, so a ledger-wide gate must not be blocked forever by it.
    score_binding_live: bool = False

    @property
    def fully_valid(self) -> bool:
        return (
            self.digest == "OK"
            and self.chain == "OK"
            and self.artifacts in ("OK", "NO_BINDINGS")
        )

    @property
    def score_binding_valid(self) -> bool:
        """Whether the published number is carried by the entry's own bound evidence."""
        return self.score_binding in ("OK", "NOT_APPLICABLE")

    @property
    def score_binding_reason(self) -> str:
        """``COMPONENT:sequence`` for a refusal, the bare verdict otherwise (#480).

        A reason that names only the component cannot say *which* entry it refused:
        every score-binding reason is therefore suffixed with the sequence of the
        entry that produced it. Entries with nothing to carry keep their bare
        verdict (``OK`` / ``NOT_APPLICABLE``), so a pass is never formatted as a
        refusal.
        """
        if self.score_binding in ("OK", "NOT_APPLICABLE"):
            return self.score_binding
        return f"{self.score_binding}:{self.sequence_number}"

    def as_dict(self) -> dict[str, Any]:
        return {
            "sequence_number": self.sequence_number,
            "digest_version": self.digest_version,
            "digest": self.digest,
            "digest_detail": self.digest_detail,
            "chain": self.chain,
            "artifacts": self.artifacts,
            "artifact_detail": self.artifact_detail,
            "score_binding": self.score_binding,
            "score_binding_detail": self.score_binding_detail,
            "score_binding_reason": self.score_binding_reason,
            "score_binding_live": self.score_binding_live,
            "fully_valid": self.fully_valid,
        }


@dataclass(frozen=True)
class LedgerVerificationReport:
    """Whole-ledger verdict: chain, digests, artifacts and score binding reported separately."""

    entries: tuple[EntryVerification, ...]
    #: OK | CHAIN_INVALID | DIGEST_TAMPERED | DIGEST_CANON_UNPROVEN |
    #: ARTIFACTS_INCOMPLETE | SCORE_BINDING_INVALID. The byte-history verdicts come
    #: first (a broken chain outranks a claim defect); SCORE_BINDING_INVALID is
    #: emitted only for a *live* score-binding refusal (#480), so ``overall`` and
    #: ``score_binding`` can never disagree about whether this ledger is usable.
    overall: str
    #: #408/#480. ``OK`` | the first named ``CAPABILITY_SCORE_*:<sequence>`` refusal
    #: found, in ledger order. The sequence suffix is part of the contract: a reason
    #: that names only the component cannot say which entry it refused.
    #:
    #: Kept apart from ``overall`` for a *historical* refusal: a pre-``v7`` entry
    #: never bound the determinants of its number, so its claim does not follow while
    #: its bytes are intact. Collapsing the two would retroactively invalidate a
    #: history that cannot be re-derived without rewriting stored bytes.
    score_binding: str = "OK"

    @property
    def ok(self) -> bool:
        return self.overall == "OK"

    @property
    def chain_valid(self) -> bool:
        return all(e.chain == "OK" for e in self.entries)

    @property
    def digests_valid(self) -> bool:
        return all(e.digest == "OK" for e in self.entries)

    @property
    def artifacts_intact(self) -> bool:
        return all(e.artifacts in ("OK", "NO_BINDINGS") for e in self.entries)

    @property
    def score_binding_failures(self) -> tuple[EntryVerification, ...]:
        """Every entry whose published number is not carried by its bound evidence."""
        return tuple(e for e in self.entries if not e.score_binding_valid)

    @property
    def live_score_binding_failures(self) -> tuple[EntryVerification, ...]:
        """Refusals this ledger's *own* version contract can still repair (#480).

        An entry whose canon binds score evidence (``v7``+) that publishes an
        unsupported number is a defect of the current contract; the same shape on a
        ``v2``-``v6`` entry is history that never bound those determinants.
        """
        return tuple(e for e in self.entries if not e.score_binding_valid and e.score_binding_live)

    @property
    def historical_score_binding_failures(self) -> tuple[EntryVerification, ...]:
        """Refusals carried by pre-score-evidence entries: named, never re-derivable."""
        return tuple(e for e in self.entries if not e.score_binding_valid and not e.score_binding_live)

    @property
    def live_score_binding_reasons(self) -> tuple[str, ...]:
        """``CAPABILITY_SCORE_*:<sequence>`` for every live refusal, in ledger order."""
        return tuple(e.score_binding_reason for e in self.live_score_binding_failures)

    @property
    def historical_score_binding_reasons(self) -> tuple[str, ...]:
        """``CAPABILITY_SCORE_*:<sequence>`` for every pre-``v7`` refusal, in ledger order."""
        return tuple(e.score_binding_reason for e in self.historical_score_binding_failures)

    @property
    def live_score_binding_reason(self) -> str | None:
        """The first live ``CAPABILITY_SCORE_*:<sequence>`` reason, or ``None``."""
        reasons = self.live_score_binding_reasons
        return reasons[0] if reasons else None

    @property
    def score_binding_status(self) -> str:
        """``OK`` | ``HISTORICAL_ONLY`` | ``INVALID`` -- the binding's own verdict.

        ``INVALID`` iff a live refusal exists; ``HISTORICAL_ONLY`` when the only
        refusals belong to entries that never bound the number's determinants. The
        three values make ``overall`` and ``score_binding`` readable together: a
        ledger is byte-intact (``overall == "OK"``) while still naming a historical
        claim defect, and that combination is stated here rather than implied.
        """
        if self.live_score_binding_failures:
            return "INVALID"
        if self.historical_score_binding_failures:
            return "HISTORICAL_ONLY"
        return "OK"

    @property
    def score_bindings_valid(self) -> bool:
        """Whether every *live* published number is carried by its bound evidence.

        #480: pre-score-evidence refusals are excluded here on purpose. They are
        still reported (``historical_score_binding_reasons``) and still make
        ``score_binding`` non-``OK``, but a ledger-wide gate cannot be blocked
        forever by entries whose bytes may not be rewritten to carry evidence they
        were never written with.
        """
        return not self.live_score_binding_failures

    def as_dict(self) -> dict[str, Any]:
        return {
            "overall": self.overall,
            "chain_valid": self.chain_valid,
            "digests_valid": self.digests_valid,
            "artifacts_intact": self.artifacts_intact,
            "score_binding": self.score_binding,
            "score_binding_status": self.score_binding_status,
            "score_bindings_valid": self.score_bindings_valid,
            "live_score_binding_reasons": list(self.live_score_binding_reasons),
            "historical_score_binding_reasons": list(self.historical_score_binding_reasons),
            "entries": [e.as_dict() for e in self.entries],
        }


class BenchmarkLedger:
    """Append-only cryptographically chained benchmark ledger.

    Append-only is a property of the *bytes*, not only of the receipts: a run that adds
    one entry leaves the lines already stored byte-identical. An entry read from disk is
    re-written exactly as it was read (its `entry_hash` is what identifies the line), and
    only an entry created in this process is rendered through the current model -- so the
    model gaining a field since those lines were written cannot rewrite them.
    """

    GENESIS_HASH = "0" * 64

    def __init__(
        self,
        entries: list[LedgerEntry] | None = None,
        stored_lines: Mapping[str, str] | None = None,
    ) -> None:
        self._entries: list[LedgerEntry] = list(entries) if entries else []
        #: entry_hash -> the line that entry was read from on disk, verbatim.
        self._stored_lines: dict[str, str] = dict(stored_lines) if stored_lines else {}

    @property
    def entries(self) -> list[LedgerEntry]:
        return list(self._entries)

    def append(self, receipt: BenchmarkReceipt) -> LedgerEntry:
        ok, err = receipt.verify()
        if not ok:
            raise ValueError(f"Cannot append unverified receipt: {err}")

        seq = len(self._entries)
        parent_hash = self._entries[-1].entry_hash if self._entries else self.GENESIS_HASH
        entry = LedgerEntry.create(seq, parent_hash, receipt)
        self._entries.append(entry)
        return entry

    def latest_evidential_entry(self) -> LedgerEntry | None:
        """The most recently appended entry whose receipt declares an evidential window.

        #448: the rule is the receipt's own declaration, never append order. Entries
        that declare no class (pre-``v6`` records, or receipts with no window) and
        entries whose class proves no economic evidence are skipped over; the first
        evidential one found from the end is the entry a reader may publish from.
        """
        for entry in reversed(self._entries):
            if entry.receipt.declares_evidential_window():
                return entry
        return None

    def publication(self) -> Publication:
        """The entry a latest-benchmark reader may publish from, or the named refusal.

        #448: the ledger's *newest* entry is not the publication. A capability score and
        a gate vector are published only from an entry whose receipt declares an
        evidential window class; with no such entry the reader refuses by name -- quoting
        the entry it refused and the class that entry carries -- instead of publishing a
        number nothing stands behind. There is no fallback and no default class.

        #490: the selected entry must also carry the measurement its number rests on. Its
        own :meth:`BenchmarkReceipt.score_publication_refusal_reason` decides, so "the
        number exists, the window is evidential, and the window records how many bars it
        was measured on" is one predicate in one place; a receipt that predates the
        measurement is refused by name rather than published on an imputed count.
        """
        newest = self._entries[-1] if self._entries else None
        selected = self.latest_evidential_entry()
        if selected is not None:
            # #490: declaring an evidential class is necessary but not sufficient. The
            # publication predicate is the receipt's own: a number may be published only
            # when it exists, its window declares economic evidence, and that window
            # records the bar count it was measured on. A receipt missing the measurement
            # is refused here by name -- never imputed, never coerced to 0, never given a
            # default class -- so an unmeasured number cannot reach a dashboard.
            refusal = selected.receipt.score_publication_refusal_reason()
            if refusal:
                return Publication(entry=None, latest_entry=newest, refusal_reason=refusal)
            return Publication(entry=selected, latest_entry=newest)
        if newest is None:
            return Publication(
                entry=None,
                latest_entry=None,
                refusal_reason=(
                    f"{NO_EVIDENTIAL_LEDGER_ENTRY}: the ledger holds no entries, so no "
                    "entry declares a window evidence class"
                ),
            )
        receipt = newest.receipt
        return Publication(
            entry=None,
            latest_entry=newest,
            refusal_reason=(
                f"{NO_EVIDENTIAL_LEDGER_ENTRY}: no ledger entry declares an evidential "
                f"window class (window_evidence.economic_evidence=true); the newest entry "
                f"seq {newest.sequence_number} entry_hash={newest.entry_hash} carries class "
                f"{receipt.evidence_class()} (case={receipt.case_id} "
                f"policy={receipt.policy_id} digest_version={receipt.digest_version}) and its "
                "recorded capability score is refused, not published"
            ),
        )

    def verify_report(self) -> LedgerVerificationReport:
        """Verify the ledger in full, with the three claims kept apart (NX04.R4).

        The digest is recomputed under the entry's version-resolved canon, the
        chain link is recomputed from the entry's own fields, and every bound
        artifact is re-hashed on disk. A missing artifact and a broken chain are
        reported as distinct verdicts -- neither is collapsed into the other.

        #480: every score-binding refusal is named ``COMPONENT:sequence`` and is
        classified against the entry's *own* version contract. A ``v7``+ entry binds
        the determinants of its number, so its refusal is live: it is reported in
        ``overall`` as ``SCORE_BINDING_INVALID`` unless a byte-history defect already
        outranks it. A ``v2``-``v6`` refusal is history that cannot be re-derived --
        still named in ``score_binding``/``historical_score_binding_reasons``, but
        never turned into a generic ledger failure (and never into ``...:OK``).
        """
        expected_parent = self.GENESIS_HASH
        entries: list[EntryVerification] = []
        contradicted = self._score_binding_contradictions()
        for i, ledger_entry in enumerate(self._entries):
            receipt = ledger_entry.receipt
            digest_ok, digest_detail = receipt.verify_digest()
            digest = "OK" if digest_ok else self._digest_failure_class(digest_detail)

            if ledger_entry.sequence_number != i:
                chain = "SEQUENCE_GAP"
            elif ledger_entry.parent_entry_hash != expected_parent:
                chain = "PARENT_HASH_MISMATCH"
            else:
                canon = [
                    ledger_entry.sequence_number,
                    ledger_entry.parent_entry_hash,
                    receipt.receipt_digest,
                ]
                recomputed = hashlib.sha256(
                    json.dumps(canon, separators=(",", ":")).encode()
                ).hexdigest()
                chain = (
                    "OK" if recomputed == ledger_entry.entry_hash else "ENTRY_HASH_TAMPERED"
                )

            artifacts, artifact_detail = self._artifact_verdict(receipt)
            if ledger_entry.sequence_number in contradicted:
                score_binding = CAPABILITY_SCORE_CONTRADICTS_BOUND_EVIDENCE
                score_binding_detail = contradicted[ledger_entry.sequence_number]
            elif receipt.capability_score is None:
                # nothing published, nothing to carry: not a defect, and not a pass
                score_binding = "NOT_APPLICABLE"
                score_binding_detail = ""
            else:
                refusal = receipt.capability_score_refusal_reason()
                score_binding = "OK" if refusal is None else refusal.split(":", 1)[0]
                score_binding_detail = refusal or ""
            score_binding_live = self._versions_bind_score_evidence(receipt.digest_version) and (
                score_binding not in ("OK", "NOT_APPLICABLE")
            )
            entries.append(
                EntryVerification(
                    sequence_number=ledger_entry.sequence_number,
                    digest_version=receipt.digest_version,
                    digest=digest,
                    digest_detail=digest_detail,
                    chain=chain,
                    artifacts=artifacts,
                    artifact_detail=artifact_detail,
                    score_binding=score_binding,
                    score_binding_detail=score_binding_detail,
                    score_binding_live=score_binding_live,
                )
            )
            expected_parent = ledger_entry.entry_hash

        overall = "OK"
        if any(e.chain != "OK" for e in entries):
            overall = "CHAIN_INVALID"
        elif any(e.digest == "DIGEST_TAMPERED" for e in entries):
            overall = "DIGEST_TAMPERED"
        elif any(e.digest != "OK" for e in entries):
            overall = "DIGEST_CANON_UNPROVEN"
        elif any(e.artifacts not in ("OK", "NO_BINDINGS") for e in entries):
            overall = "ARTIFACTS_INCOMPLETE"
        elif any(not e.score_binding_valid and e.score_binding_live for e in entries):
            overall = "SCORE_BINDING_INVALID"
        score_binding = "OK"
        for verification in entries:
            if not verification.score_binding_valid:
                score_binding = verification.score_binding_reason
                break
        return LedgerVerificationReport(entries=tuple(entries), overall=overall, score_binding=score_binding)

    @staticmethod
    def _versions_bind_score_evidence(digest_version: str) -> bool:
        """Treat only known evidence-binding canon versions as live claims."""
        canon = RECEIPT_CANON_TABLE.get(digest_version)
        return canon is not None and canon.score_evidence == "always_present"

    def _score_binding_contradictions(self) -> dict[int, str]:
        """Entries bound to identical evidence that publish different numbers (#408).

        The per-receipt check re-derives a number from its own evidence; this one
        adjudicates the shape a single receipt cannot see: two receipts whose bound
        evidence is byte-identical (fingerprint equal) while their published
        capability scores differ. The evidence is supposed to *produce* the number,
        so one of the two is an assertion nothing carries -- and the ledger cannot
        say which, so it refuses both by name rather than picking a winner.

        Version-agnostic and retro-invalidation-free: entries whose own bytes
        certify under their own digest version stay certified (``digest``/``chain``
        verdicts untouched); what does not survive is the *claim*.
        """
        grouped: dict[str, list[int]] = {}
        published: dict[int, float] = {}
        for entry in self._entries:
            receipt = entry.receipt
            if receipt.capability_score is None:
                continue
            fingerprint = bound_evidence_fingerprint(receipt)
            if fingerprint is None:
                continue
            grouped.setdefault(fingerprint, []).append(entry.sequence_number)
            published[entry.sequence_number] = receipt.capability_score

        contradicted: dict[int, str] = {}
        for sequences in grouped.values():
            numbers = sorted({published[s] for s in sequences})
            if len(numbers) < 2:
                continue
            for sequence in sequences:
                others = sorted(s for s in sequences if s != sequence)
                contradicted[sequence] = (
                    f"{CAPABILITY_SCORE_CONTRADICTS_BOUND_EVIDENCE}: bound evidence identical "
                    f"to seq {others}, published capability_scores {numbers} (this receipt "
                    f"publishes {published[sequence]}); the chain/digest/artifact verdicts of "
                    f"these entries are adjudicated separately and are unaffected"
                )
        return contradicted

    @staticmethod
    def _digest_failure_class(detail: str) -> str:
        if detail.startswith("UNSUPPORTED_DIGEST_VERSION"):
            return "UNSUPPORTED_DIGEST_VERSION"
        if detail.startswith("CANON_SHAPE_UNPROVEN"):
            return "CANON_SHAPE_UNPROVEN"
        return "DIGEST_TAMPERED"

    @staticmethod
    def _artifact_verdict(receipt: BenchmarkReceipt) -> tuple[str, str]:
        if not receipt.artifact_bindings:
            return "NO_BINDINGS", ""
        details: list[str] = []
        verdict = "OK"
        for binding in receipt.artifact_bindings:
            ok, err = binding.verify()
            if ok:
                continue
            details.append(f"[{binding.role}] {err}")
            verdict = "MISSING" if err.startswith("FILE_MISSING") else "TAMPERED"
        return verdict, "; ".join(details)

    def verify_chain(self) -> tuple[bool, str]:
        """Legacy single-verdict view of :meth:`verify_report` (chain + digest +
        artifacts). Kept for existing callers; the structured report is the
        interface that distinguishes the three claims.
        """
        report = self.verify_report()
        if report.ok:
            return True, "OK"
        for entry in report.entries:
            i = entry.sequence_number
            if entry.chain == "SEQUENCE_GAP":
                return False, f"SEQUENCE_GAP: entry {i} has seq {entry.sequence_number}"
            if entry.chain == "PARENT_HASH_MISMATCH":
                return False, f"BROKEN_CHAIN: entry {i} parent_hash mismatch"
            if entry.chain == "ENTRY_HASH_TAMPERED":
                return False, f"ENTRY_HASH_TAMPERED at {i}"
            if entry.digest != "OK":
                return False, f"ENTRY_RECEIPT_INVALID at {i}: {entry.digest_detail}"
            if entry.artifacts not in ("OK", "NO_BINDINGS"):
                return False, f"ENTRY_RECEIPT_INVALID at {i}: ARTIFACT_TAMPERED: {entry.artifact_detail}"
        return False, f"LEDGER_INVALID: {report.overall}"

    def save_jsonl(self, path: Path | str) -> None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            for entry in self._entries:
                f.write(self._line_for(entry) + "\n")

    def _line_for(self, entry: LedgerEntry) -> str:
        """The bytes to store for ``entry``: the line it already has, else a fresh one.

        A stored line is reused only while it still parses back to exactly the entry it
        was read for, so a ledger whose entry was rebuilt in memory is re-serialised
        rather than written back stale.
        """
        stored = self._stored_lines.get(entry.entry_hash)
        if stored is not None and self._stored_line_describes(stored, entry):
            return stored
        return entry.model_dump_json()

    @staticmethod
    def _stored_line_describes(stored_line: str, entry: LedgerEntry) -> bool:
        """Whether ``stored_line`` still parses back to exactly ``entry``."""
        try:
            return LedgerEntry.model_validate_json(stored_line) == entry
        except ValidationError:
            return False

    @classmethod
    def load_jsonl(cls, path: Path | str) -> BenchmarkLedger:
        p = Path(path)
        if not p.is_file():
            return cls([])
        entries: list[LedgerEntry] = []
        stored_lines: dict[str, str] = {}
        with open(p, "r", encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    entry = LedgerEntry.model_validate_json(line)
                    entries.append(entry)
                    stored_lines[entry.entry_hash] = line.rstrip("\n")
        return cls(entries, stored_lines)

    def __len__(self) -> int:
        return len(self._entries)
