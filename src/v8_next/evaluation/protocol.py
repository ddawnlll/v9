"""M4-03 (#31): research protocol — dataset roles, sealed OOS, correction, walk-forward.

This module is the *enforcement* half of preregistration. It owns four things a
research report must not be able to fake:

* **Dataset roles.** ``DEVELOPMENT`` and ``VALIDATION`` may be read under the open
  policy; ``SEALED_OOS`` may only be read with an authorization minted by
  :func:`authorize_sealed`. The :class:`AccessLedger` is append-only and every
  read is a recorded :class:`AccessToken` bound to a slice identity, a purpose
  and an evaluation budget, so the report can state which roles were consumed.
* **Reuse detection.** A second sealed read of the same slice under the same
  authorization is a typed violation (:class:`SealedReuseViolation`). A
  re-authorization is a distinct mint, and is recorded as such.
* **Multiple testing.** A family correction (Bonferroni/Holm) is computed over a
  declared family whose size is explicit. A claim that was not corrected, or
  that is made outside the declared family, resolves to :attr:`Verdict.INVALID`
  — never to ``PASS``.
* **Purged walk-forward.** :func:`purged_walk_forward` emits deterministic,
  half-open index ranges whose training indices never fall inside the purge
  before, or the embargo after, *any* test block.

The verdict gate (:func:`assess_verdict`) is the only function in the package
that returns ``PASS`` for a research claim. It cannot emit ``PASS`` without a
registered :class:`~v8_next.evaluation.preregistration.Preregistration`, a token
the ledger actually issued, the registered dataset role, a sealed read issued
*after* the registration was created, a corrected test run at the registered
alpha, and the :class:`~v8_next.evaluation.preregistration.ExperimentResult` the
claim rests on — whose gate vector must match the registration field by field and
whose observed trades/drawdown must satisfy the registered gates.
``NO_EDGE``, ``STOP`` and ``INCONCLUSIVE`` are first-class accepted outcomes: they
are recorded, not converted into a failure or into a permissive default.

No order objects, no execution adapter import, no live-trading authority.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import TYPE_CHECKING, Any, ClassVar, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from v8_next.telemetry.identity import canonical_digest

# ``Preregistration`` imports this module, so the reverse reference is typing-only:
# at runtime protocol.py never imports preregistration.py and no cycle exists.
if TYPE_CHECKING:  # keeps the import-time graph acyclic
    from v8_next.evaluation.preregistration import ExperimentResult, Preregistration

# --- vocabulary --------------------------------------------------------------


class DatasetRole(StrEnum):
    """What a dataset may be used for. The role is a property of the read."""

    DEVELOPMENT = "DEVELOPMENT"
    VALIDATION = "VALIDATION"
    SEALED_OOS = "SEALED_OOS"


#: Roles the open policy permits without a separately minted authorization.
OPEN_ROLES: frozenset[DatasetRole] = frozenset({DatasetRole.DEVELOPMENT, DatasetRole.VALIDATION})

#: Every role, in the canonical order used by reports.
ROLE_ORDER: tuple[DatasetRole, ...] = (
    DatasetRole.DEVELOPMENT,
    DatasetRole.VALIDATION,
    DatasetRole.SEALED_OOS,
)


class Verdict(StrEnum):
    """Outcome vocabulary for a research claim.

    ``PASS`` is the only affirmative outcome and is gated by :func:`assess_verdict`.
    ``NO_EDGE`` and ``STOP`` are accepted conclusions, not errors.
    """

    PASS = "PASS"
    NO_EDGE = "NO_EDGE"
    INCONCLUSIVE = "INCONCLUSIVE"
    STOP = "STOP"
    INVALID = "INVALID"


#: Outcomes a preregistration must accept before any data is read.
NEGATIVE_OUTCOMES: frozenset[Verdict] = frozenset({Verdict.NO_EDGE, Verdict.STOP})

#: Outcomes that do not assert an edge.
NON_CLAIM_OUTCOMES: frozenset[Verdict] = frozenset(
    {Verdict.NO_EDGE, Verdict.INCONCLUSIVE, Verdict.STOP, Verdict.INVALID}
)


class CorrectionMethod(StrEnum):
    """Declared multiple-testing correction."""

    BONFERRONI = "BONFERRONI"
    HOLM = "HOLM"


# --- errors ------------------------------------------------------------------


class ResearchProtocolError(ValueError):
    """Typed refusal from the research protocol.

    ``code`` is the machine-readable discriminator; ``field`` names the input the
    refusal is about (when one applies).
    """

    code: str = "RESEARCH_PROTOCOL_ERROR"

    def __init__(self, message: str, *, code: str | None = None, field: str | None = None) -> None:
        resolved = code or type(self).code
        super().__init__(f"{resolved}: {message}")
        self.code = resolved
        self.message = message
        self.field = field


class SealedAccessError(ResearchProtocolError):
    """A sealed-OOS read was requested without usable authority."""

    code = "SEALED_ACCESS_DENIED"


class SealedReuseViolation(ResearchProtocolError):
    """A sealed slice was read again under an authorization that already opened it."""

    code = "SEALED_SLICE_REUSE"

    def __init__(
        self,
        message: str,
        *,
        slice_id: str,
        authorization_id: str,
        field: str | None = "slice_id",
    ) -> None:
        super().__init__(message, field=field)
        self.slice_id = slice_id
        self.authorization_id = authorization_id


class HypothesisBudgetViolation(ResearchProtocolError):
    """A testing family does not match the declared family/budget."""

    code = "HYPOTHESIS_BUDGET_VIOLATION"


class WalkForwardError(ResearchProtocolError):
    """The requested walk-forward plan cannot be built without leakage."""

    code = "WALK_FORWARD_INVALID"


class VerdictRefused(ResearchProtocolError):
    """An outcome was not allowed by the preregistration or the protocol."""

    code = "VERDICT_REFUSED"


# --- sealed authorization ----------------------------------------------------


class _MintCapability:
    """Private capability object; only :func:`authorize_sealed` holds the sole mint."""

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - never printed by the protocol
        return "<sealed-authorization-mint>"


_SEALED_MINT = _MintCapability()


class SealedAuthorization(BaseModel):
    """Authority to read one sealed slice, minted only by :func:`authorize_sealed`.

    Direct construction is refused by the capability check, so a test cannot
    fabricate "authorization" by passing plausible field values.
    """

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    authorization_id: str
    granted_by: str
    purpose: str
    reference: str
    max_slices: int = Field(ge=1)
    issued_ns: int = Field(ge=0)
    capability: _MintCapability = Field(exclude=True, repr=False)

    @model_validator(mode="after")
    def _minted_only(self) -> Self:
        if self.capability is not _SEALED_MINT:
            raise ValueError("sealed authorization must be minted by authorize_sealed()")
        if not self.granted_by.strip():
            raise ValueError("sealed authorization requires a named grantor")
        if not self.purpose.strip():
            raise ValueError("sealed authorization requires a declared purpose")
        if not self.reference.strip():
            raise ValueError("sealed authorization requires a reference")
        return self

    def digest(self) -> str:
        return canonical_digest(
            "sealed-authorization",
            {
                "authorization_id": self.authorization_id,
                "granted_by": self.granted_by,
                "purpose": self.purpose,
                "reference": self.reference,
                "max_slices": self.max_slices,
                "issued_ns": self.issued_ns,
            },
        )


def authorize_sealed(
    *,
    granted_by: str,
    purpose: str,
    reference: str,
    max_slices: int = 1,
    issued_ns: int = 0,
) -> SealedAuthorization:
    """Mint an explicit sealed-OOS authorization.

    This is a deliberately distinct call from ``request_access``: granting read
    authority is a separate act from exercising it, and the grant is recorded on
    the resulting token.
    """
    if max_slices < 1:
        raise SealedAccessError("max_slices must be at least 1", field="max_slices")
    if issued_ns < 0:
        raise SealedAccessError("issued_ns must be non-negative", field="issued_ns")
    identifier = canonical_digest(
        "sealed-authorization-id",
        {
            "granted_by": granted_by,
            "purpose": purpose,
            "reference": reference,
            "max_slices": max_slices,
            "issued_ns": issued_ns,
        },
    )
    return SealedAuthorization(
        authorization_id=identifier,
        granted_by=granted_by,
        purpose=purpose,
        reference=reference,
        max_slices=max_slices,
        issued_ns=issued_ns,
        capability=_SEALED_MINT,
    )


# --- access ledger -----------------------------------------------------------


class AccessToken(BaseModel):
    """One recorded dataset read. Identical content reproduces the identical token."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    token_id: str
    seq: int = Field(ge=1)
    role: DatasetRole
    purpose: str
    budget: int = Field(ge=1)
    slice_id: str
    issued_ns: int = Field(ge=0)
    authorization_id: str | None = None

    @classmethod
    def mint(
        cls,
        *,
        seq: int,
        role: DatasetRole,
        purpose: str,
        budget: int,
        slice_id: str,
        issued_ns: int,
        authorization_id: str | None = None,
    ) -> AccessToken:
        """Build a token whose ``token_id`` is derived from its content.

        This is the single derivation of a token identity: the ledger uses it to mint,
        and :meth:`AccessLedger.from_entries` uses it to check that a rehydrated row is
        the row this ledger would have written.
        """
        payload = _token_payload(
            seq=seq,
            role=role,
            purpose=purpose,
            budget=budget,
            slice_id=slice_id,
            issued_ns=issued_ns,
            authorization_id=authorization_id,
        )
        return cls(token_id=canonical_digest("research-access", payload), **payload)

    def digest(self) -> str:
        return canonical_digest("research-access-token", self.model_dump(mode="json"))


@dataclass(frozen=True)
class ReuseReport:
    """How often one slice was read and whether distinct authority backs each read."""

    role: DatasetRole
    slice_id: str
    accesses: int
    authorization_ids: tuple[str, ...]
    reused: bool
    permitted: bool


@dataclass(frozen=True)
class RoleSummary:
    """Per-role view of the ledger: included for every role, even unused ones."""

    role: DatasetRole
    accesses: int
    budget_total: int
    purposes: tuple[str, ...]
    slices: tuple[str, ...]


class AccessReport(BaseModel):
    """Which roles were consumed, with the lineage of each read."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    consumed_roles: tuple[DatasetRole, ...]
    by_role: tuple[RoleSummary, ...]
    sealed_slices: tuple[str, ...]
    entries: int
    lineage_digest: str

    def for_role(self, role: DatasetRole) -> RoleSummary:
        for summary in self.by_role:
            if summary.role is role:
                return summary
        raise ResearchProtocolError(f"role {role} missing from report", field="role")


def _token_payload(
    *,
    seq: int,
    role: DatasetRole,
    purpose: str,
    budget: int,
    slice_id: str,
    issued_ns: int,
    authorization_id: str | None,
) -> dict[str, Any]:
    return {
        "seq": seq,
        "role": role.value,
        "purpose": purpose,
        "budget": budget,
        "slice_id": slice_id,
        "issued_ns": issued_ns,
        "authorization_id": authorization_id,
    }


class AccessLedger:
    """Append-only record of dataset reads.

    The ledger is the only way to obtain a token, and :func:`assess_verdict`
    accepts nothing but a token the ledger recorded. There is no ``update`` or
    ``clear``: entries are immutable and the sequence only grows.
    """

    def __init__(self) -> None:
        self._entries: list[AccessToken] = []

    @property
    def entries(self) -> tuple[AccessToken, ...]:
        return tuple(self._entries)

    def __len__(self) -> int:
        return len(self._entries)

    def request_access(
        self,
        role: DatasetRole,
        purpose: str,
        budget: int,
        *,
        slice_id: str = "canonical-dataset",
        issued_ns: int | None = None,
        authorization: SealedAuthorization | None = None,
    ) -> AccessToken:
        """Record a read and return its token, or refuse with a typed error.

        Open roles need only a purpose and a positive budget. ``SEALED_OOS``
        additionally needs an authorization minted by :func:`authorize_sealed`
        whose purpose matches the request and whose authority has not already
        opened this slice, *and* the instant the read was issued: the verdict
        gate compares that instant with the registration's ``created_ns``, so a
        sealed read that cannot say when it happened cannot be evidence.
        """
        if not purpose.strip():
            raise ResearchProtocolError("an access purpose must be declared", field="purpose")
        if budget < 1:
            raise ResearchProtocolError(
                "an access budget must be a positive evaluation count", field="budget"
            )
        if issued_ns is None:
            if role is DatasetRole.SEALED_OOS:
                self._check_sealed(purpose, slice_id, authorization)
                raise SealedAccessError(
                    "a sealed read must record the instant it was issued "
                    "(issued_ns), so it can be ordered against the registration",
                    code="SEALED_READ_WITHOUT_ISSUE_TIME",
                    field="issued_ns",
                )
            issued_ns = 0
        if issued_ns < 0:
            raise ResearchProtocolError("issued_ns must be non-negative", field="issued_ns")
        if not slice_id.strip():
            raise ResearchProtocolError("a slice identity must be declared", field="slice_id")
        if role is DatasetRole.SEALED_OOS:
            self._check_sealed(purpose, slice_id, authorization)

        seq = len(self._entries) + 1
        token = AccessToken.mint(
            seq=seq,
            role=role,
            purpose=purpose,
            budget=budget,
            slice_id=slice_id,
            issued_ns=issued_ns,
            authorization_id=(
                authorization.authorization_id if authorization is not None else None
            ),
        )
        self._entries.append(token)
        return token

    def _check_sealed(
        self,
        purpose: str,
        slice_id: str,
        authorization: SealedAuthorization | None,
    ) -> None:
        if authorization is None:
            raise SealedAccessError(
                "SEALED_OOS requires an authorization minted by authorize_sealed()",
                field="authorization",
            )
        if authorization.purpose != purpose:
            raise SealedAccessError(
                "sealed authorization purpose does not match the requested purpose",
                field="purpose",
            )
        used = [
            entry
            for entry in self._entries
            if entry.role is DatasetRole.SEALED_OOS
            and entry.authorization_id == authorization.authorization_id
        ]
        if len(used) >= authorization.max_slices:
            raise SealedAccessError(
                f"sealed authorization already opened {len(used)} slice(s); "
                f"max_slices={authorization.max_slices}",
                field="authorization",
            )
        for entry in used:
            if entry.slice_id != slice_id:
                raise SealedReuseViolation(
                    "one sealed authorization may open exactly one slice; "
                    f"it already opened {entry.slice_id!r}",
                    slice_id=slice_id,
                    authorization_id=authorization.authorization_id,
                )
            raise SealedReuseViolation(
                "sealed slice already read under this authorization; "
                "a repeated sealed read needs a separately minted authorization",
                slice_id=slice_id,
                authorization_id=authorization.authorization_id,
            )

    def token_record(self, token_id: str) -> AccessToken | None:
        for entry in self._entries:
            if entry.token_id == token_id:
                return entry
        return None

    @classmethod
    def from_entries(
        cls,
        entries: Iterable[AccessToken],
        *,
        authorizations: Iterable[SealedAuthorization] = (),
    ) -> AccessLedger:
        """Rehydrate a persisted ledger, refusing entries that prove leakage/reuse.

        Every entry's ``token_id`` is re-derived from its content, so a hand-written
        row cannot pass itself off as a read this ledger issued. A sealed row must
        also name an authorization the caller supplies as minted: an
        ``authorization_id`` is a content digest anyone can compute, so the id
        alone proves nothing about the grant.
        """
        minted: set[str] = set()
        for authorization in authorizations:
            if not isinstance(authorization, SealedAuthorization):
                raise TypeError(
                    "authorizations must be SealedAuthorization instances minted by "
                    "authorize_sealed(); a bare id is not authority"
                )
            minted.add(authorization.authorization_id)

        ledger = cls()
        for entry in entries:
            if not isinstance(entry, AccessToken):
                raise TypeError("ledger entries must be AccessToken instances")
            if entry.seq != len(ledger._entries) + 1:
                raise ResearchProtocolError(
                    "ledger entries must be contiguous and ordered", field="seq"
                )
            expected = AccessToken.mint(
                seq=entry.seq,
                role=entry.role,
                purpose=entry.purpose,
                budget=entry.budget,
                slice_id=entry.slice_id,
                issued_ns=entry.issued_ns,
                authorization_id=entry.authorization_id,
            )
            if entry.token_id != expected.token_id:
                raise ResearchProtocolError(
                    "ledger entry token id does not re-derive from its content; "
                    "the row was not issued by this ledger",
                    code="TOKEN_DIGEST_MISMATCH",
                    field="token_id",
                )
            if entry.role is DatasetRole.SEALED_OOS:
                if entry.authorization_id is None:
                    raise SealedAccessError(
                        "sealed entry without a recorded authorization",
                        field="authorization_id",
                    )
                if entry.authorization_id not in minted:
                    raise SealedAccessError(
                        "sealed entry names an authorization this caller cannot prove "
                        "was minted; a rehydrated ledger needs the mints themselves",
                        code="UNVERIFIED_AUTHORIZATION",
                        field="authorization_id",
                    )
            ledger._entries.append(entry)
        ledger.assert_consistent()
        return ledger

    def assert_consistent(self) -> None:
        """Refuse a ledger in which one authorization read two slices or one twice."""
        opened: dict[str, str] = {}
        for entry in self._entries:
            if entry.role is not DatasetRole.SEALED_OOS:
                continue
            if entry.authorization_id is None:
                raise SealedAccessError(
                    "sealed entry without a recorded authorization", field="authorization_id"
                )
            previous = opened.get(entry.authorization_id)
            if previous is not None:
                raise SealedReuseViolation(
                    f"sealed authorization read {previous!r} and then {entry.slice_id!r}",
                    slice_id=entry.slice_id,
                    authorization_id=entry.authorization_id,
                    field="authorization_id",
                )
            opened[entry.authorization_id] = entry.slice_id

    def sealed_uses(self, authorization: SealedAuthorization) -> int:
        return sum(
            1
            for entry in self._entries
            if entry.role is DatasetRole.SEALED_OOS
            and entry.authorization_id == authorization.authorization_id
        )

    def reuse_detection(self) -> tuple[ReuseReport, ...]:
        """One report per sealed slice; repeats are flagged, never silently merged."""
        reports: list[ReuseReport] = []
        slices: list[str] = []
        for entry in self._entries:
            if entry.role is DatasetRole.SEALED_OOS and entry.slice_id not in slices:
                slices.append(entry.slice_id)
        for slice_id in slices:
            reads = [
                entry
                for entry in self._entries
                if entry.role is DatasetRole.SEALED_OOS and entry.slice_id == slice_id
            ]
            identifiers = tuple(
                entry.authorization_id if entry.authorization_id is not None else ""
                for entry in reads
            )
            distinct = len(set(identifiers))
            reports.append(
                ReuseReport(
                    role=DatasetRole.SEALED_OOS,
                    slice_id=slice_id,
                    accesses=len(reads),
                    authorization_ids=identifiers,
                    reused=len(reads) > 1,
                    permitted=distinct == len(reads)
                    and all(identifier for identifier in identifiers),
                )
            )
        return tuple(reports)

    def report(self) -> AccessReport:
        summaries: list[RoleSummary] = []
        consumed: list[DatasetRole] = []
        for role in ROLE_ORDER:
            reads = [entry for entry in self._entries if entry.role is role]
            if reads:
                consumed.append(role)
            purposes: list[str] = []
            slices: list[str] = []
            for entry in reads:
                if entry.purpose not in purposes:
                    purposes.append(entry.purpose)
                if entry.slice_id not in slices:
                    slices.append(entry.slice_id)
            summaries.append(
                RoleSummary(
                    role=role,
                    accesses=len(reads),
                    budget_total=sum(entry.budget for entry in reads),
                    purposes=tuple(purposes),
                    slices=tuple(slices),
                )
            )
        sealed = [entry.slice_id for entry in self._entries if entry.role is DatasetRole.SEALED_OOS]
        return AccessReport(
            consumed_roles=tuple(consumed),
            by_role=tuple(summaries),
            sealed_slices=tuple(dict.fromkeys(sealed)),
            entries=len(self._entries),
            lineage_digest=canonical_digest(
                "research-access-lineage",
                [entry.model_dump(mode="json") for entry in self._entries],
            ),
        )


# --- multiple testing --------------------------------------------------------


class HypothesisFamily(BaseModel):
    """The declared family a correction is computed over. Its size is explicit."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    hypotheses: tuple[str, ...]

    @model_validator(mode="after")
    def _declared(self) -> Self:
        if not self.name.strip():
            raise ValueError("hypothesis family requires a name")
        if not self.hypotheses:
            raise ValueError("hypothesis family must declare at least one hypothesis")
        if len(set(self.hypotheses)) != len(self.hypotheses):
            raise ValueError("hypothesis family names must be unique")
        if any(not name.strip() for name in self.hypotheses):
            raise ValueError("hypothesis names cannot be empty")
        return self

    @property
    def declared_size(self) -> int:
        return len(self.hypotheses)

    def digest(self) -> str:
        return canonical_digest("hypothesis-family", self.model_dump(mode="json"))


class TestingReport(BaseModel):
    """Result of one correction over one declared family."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: Not a pytest test class; the name is part of the protocol vocabulary.
    __test__: ClassVar[bool] = False

    family_name: str
    family_digest: str
    family_size: int = Field(ge=1)
    method: CorrectionMethod | None
    alpha: float
    adjusted: dict[str, float]
    rejected: dict[str, bool]
    corrected: bool
    degrees_of_freedom: int = Field(ge=1)

    def verdict_for(self, hypothesis: str, *, preregistered: bool) -> Verdict:
        """Resolve one hypothesis to a verdict. Unearned claims are ``INVALID``."""
        if hypothesis not in self.adjusted:
            raise ResearchProtocolError(
                f"{hypothesis!r} is not in family {self.family_name!r}", field="hypothesis"
            )
        if not self.corrected:
            return Verdict.INVALID
        if not preregistered:
            return Verdict.INVALID
        return Verdict.PASS if self.rejected[hypothesis] else Verdict.NO_EDGE

    def any_rejected(self) -> bool:
        return any(self.rejected.values())


def _holm(p_values: list[float]) -> list[float]:
    """Holm step-down adjusted p-values, monotone and capped at 1.0."""
    count = len(p_values)
    order = sorted(range(count), key=lambda index: p_values[index])
    adjusted = [0.0] * count
    running = 0.0
    for rank, index in enumerate(order):
        candidate = min(1.0, (count - rank) * p_values[index])
        running = max(running, candidate)
        adjusted[index] = running
    return adjusted


def multiple_testing(
    p_values: Mapping[str, float],
    family: HypothesisFamily,
    *,
    alpha: float = 0.05,
    method: CorrectionMethod | None = CorrectionMethod.HOLM,
) -> TestingReport:
    """Correct a declared family. ``method=None`` is recorded as uncorrected.

    The family must be exactly the tested set: testing a smaller or larger set
    than declared is a budget violation, not an adjustment. ``corrected=False``
    is preserved in the report and :meth:`TestingReport.verdict_for` resolves it
    to ``INVALID`` rather than to a permissive ``PASS``.

    ``method`` must be a :class:`CorrectionMethod` member (or ``None``). A plain
    string such as ``"HOLM"`` compares equal to the member but is not one, so it
    would otherwise select no adjustment while the report still claimed
    ``corrected=True``. It is refused instead.
    """
    if not 0.0 < alpha < 1.0:
        raise ResearchProtocolError("alpha must be strictly between 0 and 1", field="alpha")
    if method is not None and not isinstance(method, CorrectionMethod):
        raise ResearchProtocolError(
            f"correction method {method!r} is not a CorrectionMethod member; "
            "a correction must be a declared member, not a look-alike value",
            code="CORRECTION_METHOD_NOT_DECLARED",
            field="method",
        )
    names = tuple(p_values.keys())
    if len(names) != family.declared_size or set(names) != set(family.hypotheses):
        raise HypothesisBudgetViolation(
            "tested p-values do not match the declared hypothesis family "
            f"(declared {family.declared_size}, tested {len(names)}; "
            f"missing {sorted(set(family.hypotheses) - set(names))}, "
            f"undeclared {sorted(set(names) - set(family.hypotheses))})",
            field="hypotheses",
        )
    ordered = list(family.hypotheses)
    raw: list[float] = []
    for name in ordered:
        value = float(p_values[name])
        if not 0.0 <= value <= 1.0 or value != value:  # NaN fails closed on the second test
            raise ResearchProtocolError(
                f"p-value for {name!r} must be finite and within [0, 1]", field=name
            )
        raw.append(value)

    count = len(ordered)
    if method is None:
        adjusted_values = raw
    elif method is CorrectionMethod.BONFERRONI:
        adjusted_values = [min(1.0, value * count) for value in raw]
    elif method is CorrectionMethod.HOLM:
        adjusted_values = _holm(raw)
    else:  # pragma: no cover - a new member must be implemented, never silently skipped
        raise ResearchProtocolError(
            f"correction method {method} has no implementation; refusing to report "
            "raw p-values as corrected",
            code="CORRECTION_METHOD_UNIMPLEMENTED",
            field="method",
        )
    adjusted = {name: adjusted_values[index] for index, name in enumerate(ordered)}
    rejected = {name: adjusted[name] <= alpha for name in ordered}
    return TestingReport(
        family_name=family.name,
        family_digest=family.digest(),
        family_size=count,
        method=method,
        alpha=alpha,
        adjusted=adjusted,
        rejected=rejected,
        corrected=method is not None,
        degrees_of_freedom=count,
    )


# --- purged walk-forward -----------------------------------------------------


@dataclass(frozen=True, order=True)
class IndexRange:
    """Half-open ``[start, end)`` index range over a bar-indexed dataset."""

    start: int
    end: int

    def __post_init__(self) -> None:
        if self.end <= self.start:
            raise WalkForwardError(f"index range [{self.start}, {self.end}) is empty", field="end")

    def __contains__(self, index: object) -> bool:
        return isinstance(index, int) and self.start <= index < self.end

    def __len__(self) -> int:
        return self.end - self.start

    def indices(self) -> range:
        return range(self.start, self.end)


@dataclass(frozen=True)
class Fold:
    """One purged walk-forward fold: training ranges, test range, and both gaps."""

    fold_id: str
    train: tuple[IndexRange, ...]
    test: IndexRange
    purge_bars: int
    embargo_bars: int

    @property
    def train_bars(self) -> int:
        return sum(len(segment) for segment in self.train)

    def train_indices(self) -> tuple[int, ...]:
        return tuple(index for segment in self.train for index in segment.indices())


def _subtract(candidate: IndexRange, forbidden: Sequence[IndexRange]) -> tuple[IndexRange, ...]:
    """Return ``candidate`` minus the union of ``forbidden``, as sorted disjoint ranges."""
    remaining: list[IndexRange] = [candidate]
    for block in sorted(forbidden):
        next_remaining: list[IndexRange] = []
        for segment in remaining:
            if block.end <= segment.start or block.start >= segment.end:
                next_remaining.append(segment)
                continue
            if block.start > segment.start:
                next_remaining.append(IndexRange(segment.start, block.start))
            if block.end < segment.end:
                next_remaining.append(IndexRange(block.end, segment.end))
        remaining = next_remaining
    return tuple(remaining)


def purged_walk_forward(
    *,
    total_bars: int,
    folds: int,
    purge_bars: int,
    embargo_bars: int,
    min_train_bars: int = 1,
) -> tuple[Fold, ...]:
    """Build deterministic expanding-window folds that cannot leak.

    Bars are partitioned into ``folds + 1`` contiguous blocks: block 0 is the
    initial training history and blocks 1..folds are scored test blocks. For every
    test block ``[start, end)`` the training candidate ``[0, start)`` has removed
    both ``[start - purge, start)`` (outcome overlap before the test) and
    ``[end, end + embargo)`` (label bleed after it). The removal is applied for
    *every* test block in the plan, so a training index never sits inside another
    fold's purge or embargo either. A fold without enough training history is a
    typed error — folds are never silently skipped.
    """
    if total_bars < 2:
        raise WalkForwardError("walk-forward needs at least 2 bars", field="total_bars")
    if folds < 1:
        raise WalkForwardError("walk-forward needs at least 1 fold", field="folds")
    if folds + 1 > total_bars:
        raise WalkForwardError("not enough bars for the initial training block", field="folds")
    if purge_bars < 0 or embargo_bars < 0:
        raise WalkForwardError("purge/embargo must be non-negative", field="purge_bars")
    if min_train_bars < 1:
        raise WalkForwardError("min_train_bars must be at least 1", field="min_train_bars")

    blocks = folds + 1
    bounds = [total_bars * index // blocks for index in range(blocks + 1)]
    tests = [IndexRange(bounds[index], bounds[index + 1]) for index in range(1, blocks)]
    if any(len(block) < 1 for block in tests):
        raise WalkForwardError("every fold needs a non-empty test block", field="folds")

    forbidden: list[IndexRange] = []
    for block in tests:
        if purge_bars:
            forbidden.append(IndexRange(block.start - purge_bars, block.start))
        if embargo_bars:
            forbidden.append(IndexRange(block.end, block.end + embargo_bars))

    built: list[Fold] = []
    for index, block in enumerate(tests):
        candidate = IndexRange(0, block.start)
        train = tuple(segment for segment in _subtract(candidate, forbidden) if len(segment) > 0)
        total_train = sum(len(segment) for segment in train)
        if total_train < min_train_bars:
            raise WalkForwardError(
                f"fold {index:02d} has {total_train} training bars "
                f"(minimum {min_train_bars}); purge/embargo consume the history",
                field="min_train_bars",
            )
        built.append(
            Fold(
                fold_id=f"fold_{index:02d}",
                train=train,
                test=block,
                purge_bars=purge_bars,
                embargo_bars=embargo_bars,
            )
        )
    return tuple(built)


# --- verdict gate ------------------------------------------------------------


class VerdictResult(BaseModel):
    """An assessed outcome, with the authority it does and does not carry."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    verdict: Verdict
    code: str
    role: DatasetRole
    preregistration_digest: str
    primary_edge_eligible: bool
    reasons: tuple[str, ...] = ()
    #: Digest of the experiment result the claim was assessed against (``None`` when the
    #: claim carried no result, e.g. a preregistered negative outcome).
    experiment_digest: str | None = None


def assess_verdict(
    *,
    preregistration: Preregistration,
    ledger: AccessLedger,
    token: AccessToken,
    claimed: Verdict,
    testing: TestingReport | None = None,
    experiment: ExperimentResult | None = None,
) -> VerdictResult:
    """Assess one claimed outcome against the preregistration and the ledger.

    Refusals that are structural (no registered preregistration, a token the
    ledger never issued, a role substitution, a correction the budget does not
    declare, a sealed read issued before the registration existed) raise. Claims
    that the evidence cannot carry resolve to ``INVALID``. ``NO_EDGE``/``STOP``/
    ``INCONCLUSIVE`` are accepted when preregistered.

    ``PASS`` additionally requires the :class:`ExperimentResult` the claim rests
    on: its gate vector is diffed against the registration
    (:meth:`Preregistration.compare_against_result`), it must carry the claimed
    verdict, the test must have run at the registered alpha, and the observed
    trade count/drawdown must satisfy the registered gates. Without a result there
    is no evidence to check, so ``PASS`` is refused rather than assumed.
    """
    preregistration.validate()
    digest = preregistration.digest()

    recorded = ledger.token_record(token.token_id)
    if recorded is None:
        raise VerdictRefused(
            "access token was not issued by this ledger", code="UNREGISTERED_ACCESS_TOKEN"
        )
    if recorded != token:
        raise VerdictRefused("access token does not match the recorded read", code="TOKEN_MISMATCH")
    if preregistration.dataset_role is None:  # pragma: no cover - validate() refuses this
        raise VerdictRefused("preregistration has no dataset role", code="NO_DATASET_ROLE")
    if token.role is not preregistration.dataset_role:
        raise VerdictRefused(
            f"token role {token.role} is not the registered role {preregistration.dataset_role}",
            code="DATASET_ROLE_MISMATCH",
            field="dataset_role",
        )
    if token.role not in OPEN_ROLES and token.authorization_id is None:
        raise VerdictRefused(
            "a sealed read must carry a recorded authorization", code="SEALED_WITHOUT_AUTHORITY"
        )
    created_ns = preregistration.created_ns
    if token.role is DatasetRole.SEALED_OOS and created_ns is not None:
        if token.issued_ns < created_ns:
            raise VerdictRefused(
                f"sealed read was issued at {token.issued_ns} ns, before the registration "
                f"was created at {created_ns} ns: the hypothesis was formed after the read",
                code="POST_HOC_PREREGISTRATION",
                field="issued_ns",
            )
    if (
        claimed is not Verdict.PASS
        and claimed is not Verdict.INCONCLUSIVE
        and claimed not in preregistration.accepted_outcomes
    ):
        raise VerdictRefused(
            f"outcome {claimed} was not accepted at registration",
            code="OUTCOME_NOT_PREREGISTERED",
            field="accepted_outcomes",
        )

    if claimed is not Verdict.PASS:
        code = "ACCEPTED_NEGATIVE_OUTCOME" if claimed in NEGATIVE_OUTCOMES else "ACCEPTED_OUTCOME"
        return VerdictResult(
            verdict=claimed,
            code=code,
            role=token.role,
            preregistration_digest=digest,
            primary_edge_eligible=False,
            reasons=(f"preregistered outcome {claimed}",),
        )

    budget = preregistration.hypothesis_budget
    if budget is None:  # pragma: no cover - validate() refuses this
        raise VerdictRefused("preregistration has no hypothesis budget", code="NO_BUDGET")
    if testing is None or not testing.corrected:
        return VerdictResult(
            verdict=Verdict.INVALID,
            code="UNCORRECTED_CLAIM",
            role=token.role,
            preregistration_digest=digest,
            primary_edge_eligible=False,
            reasons=("PASS claimed without a multiple-testing correction",),
        )
    if experiment is None:
        raise VerdictRefused(
            "PASS requires the ExperimentResult the claim was measured on: its gate "
            "vector is compared with the registration and its observations are checked "
            "against the registered gates",
            code="EXPERIMENT_RESULT_REQUIRED",
            field="experiment",
        )
    preregistration.compare_against_result(experiment)  # raises PostHocGateChange
    if experiment.verdict is not claimed:
        raise VerdictRefused(
            f"the experiment result reports {experiment.verdict}, not the claimed {claimed}",
            code="RESULT_VERDICT_MISMATCH",
            field="verdict",
        )
    if budget.alpha is not None and Decimal(str(testing.alpha)) != budget.alpha:
        raise VerdictRefused(
            f"the test ran at alpha {testing.alpha}, but the registration fixed "
            f"{budget.alpha}; a wider alpha is a different test",
            code="ALPHA_MISMATCH",
            field="alpha",
        )
    if budget.max_hypotheses is None or testing.family_size > budget.max_hypotheses:
        raise VerdictRefused(
            f"family size {testing.family_size} exceeds the registered budget",
            code="HYPOTHESIS_BUDGET_EXCEEDED",
            field="max_hypotheses",
        )
    if budget.correction is not None and testing.method is not budget.correction:
        raise VerdictRefused(
            f"correction {testing.method} is not the registered {budget.correction}",
            code="CORRECTION_MISMATCH",
            field="correction",
        )
    if testing.family_name != budget.family:
        raise VerdictRefused(
            f"tested family {testing.family_name!r} is not the registered {budget.family!r}",
            code="FAMILY_MISMATCH",
            field="family",
        )
    hypothesis = preregistration.hypothesis_id
    if hypothesis not in testing.adjusted:
        return VerdictResult(
            verdict=Verdict.INVALID,
            code="HYPOTHESIS_NOT_TESTED",
            role=token.role,
            preregistration_digest=digest,
            primary_edge_eligible=False,
            reasons=(f"registered hypothesis {hypothesis!r} was not in the tested family",),
        )
    if not testing.rejected[hypothesis]:
        return VerdictResult(
            verdict=Verdict.INVALID,
            code="CLAIM_NOT_SUPPORTED",
            role=token.role,
            preregistration_digest=digest,
            primary_edge_eligible=False,
            reasons=(f"{hypothesis!r} did not survive correction",),
            experiment_digest=experiment.digest(),
        )
    registered = preregistration.gates()
    if registered.minimum_trades is not None and (
        experiment.observed_trades < registered.minimum_trades
    ):
        return VerdictResult(
            verdict=Verdict.INVALID,
            code="GATE_NOT_MET",
            role=token.role,
            preregistration_digest=digest,
            primary_edge_eligible=False,
            reasons=(
                f"observed {experiment.observed_trades} trades, below the registered "
                f"minimum of {registered.minimum_trades}",
            ),
            experiment_digest=experiment.digest(),
        )
    if registered.max_drawdown is not None and (
        experiment.observed_drawdown > registered.max_drawdown
    ):
        return VerdictResult(
            verdict=Verdict.INVALID,
            code="GATE_NOT_MET",
            role=token.role,
            preregistration_digest=digest,
            primary_edge_eligible=False,
            reasons=(
                f"observed drawdown {experiment.observed_drawdown}, above the registered "
                f"maximum of {registered.max_drawdown}",
            ),
            experiment_digest=experiment.digest(),
        )
    sealed = token.role is DatasetRole.SEALED_OOS
    return VerdictResult(
        verdict=Verdict.PASS,
        code="PASS_WITH_REGISTERED_PREREGISTRATION",
        role=token.role,
        preregistration_digest=digest,
        primary_edge_eligible=sealed,
        reasons=(
            f"corrected by {testing.method} over family {testing.family_name!r} at "
            f"alpha {testing.alpha}",
            f"gates matched and satisfied: {experiment.observed_trades} trades, "
            f"drawdown {experiment.observed_drawdown}",
            "sealed OOS read" if sealed else f"{token.role} read: no primary OOS edge",
        ),
        experiment_digest=experiment.digest(),
    )


def require_primary_edge(result: VerdictResult) -> VerdictResult:
    """Return ``result`` only if it is a sealed-OOS ``PASS``; otherwise refuse."""
    if result.verdict is not Verdict.PASS or not result.primary_edge_eligible:
        raise VerdictRefused(
            f"{result.role} verdict {result.verdict} cannot claim a primary OOS edge "
            f"({result.code})",
            code="NOT_PRIMARY_OOS_EVIDENCE",
            field="dataset_role",
        )
    return result


class ResultReport(BaseModel):
    """The report a research run publishes: role, verdict, lineage, budget."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    preregistration_digest: str
    dataset_role: DatasetRole
    verdict: Verdict
    verdict_code: str
    primary_edge_eligible: bool
    consumed_roles: tuple[DatasetRole, ...]
    by_role: tuple[RoleSummary, ...]
    lineage_digest: str
    degrees_of_freedom: int

    def for_role(self, role: DatasetRole) -> RoleSummary:
        for summary in self.by_role:
            if summary.role is role:
                return summary
        raise ResearchProtocolError(f"role {role} missing from report", field="role")


def build_result_report(
    *,
    preregistration: Preregistration,
    ledger: AccessLedger,
    result: VerdictResult,
    testing: TestingReport | None = None,
) -> ResultReport:
    """Publish a report that separates development, validation and sealed OOS."""
    access = ledger.report()
    if preregistration.dataset_role is None:  # pragma: no cover - validate() refuses this
        raise VerdictRefused("preregistration has no dataset role", code="NO_DATASET_ROLE")
    return ResultReport(
        preregistration_digest=preregistration.digest(),
        dataset_role=preregistration.dataset_role,
        verdict=result.verdict,
        verdict_code=result.code,
        primary_edge_eligible=result.primary_edge_eligible,
        consumed_roles=access.consumed_roles,
        by_role=access.by_role,
        lineage_digest=access.lineage_digest,
        degrees_of_freedom=testing.degrees_of_freedom if testing is not None else 0,
    )
