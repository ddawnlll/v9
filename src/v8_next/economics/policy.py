"""Unified recurrent ``TraderDecisionPolicy`` (M2-04) — one advisory decision per cut.

Before this module the decision plane could only answer one question ("may this
entry pass?") with one gate. :class:`TraderDecisionPolicy` instead consumes a
single :class:`DecisionRequest` — belief, expert stances, the opportunity's
lifecycle, position presence, venue state and portfolio capacity — and emits one
advisory :class:`~v8_next.domain.decision.TraderDecision` covering all six typed
actions (``WAIT``/``ENTER``/``HOLD``/``REDUCE``/``EXIT``/``ABSTAIN``).

Design invariants:

- **Advisory, shadow-only.** The only output is a ``TraderDecision`` whose
  ``advisory_only`` is pinned ``True``. Nothing here constructs, submits,
  amends or cancels an order, and no execution module is imported. Adaptive
  side effects are structurally impossible rather than merely disabled.
- **Legality is data.** Candidate actions derived from evidence are intersected
  with :meth:`v8_next.economics.legality.LegalActionTable.legal` and resolved
  with :func:`~v8_next.economics.legality.resolve_legal_conflict`, so a
  consensus of entries can never outrank an exit and an unreadable world
  (``venue=UNKNOWN``, ``position=UNCERTAIN``) collapses to ``WAIT``/``ABSTAIN``.
- **Fail closed, never default.** Stale, future-dated or malformed belief yields
  ``ABSTAIN`` with a typed reason code; a missing opportunity can never produce
  an entry. Unreadable *inputs* are data, not exceptions: only a malformed
  envelope (non-``Decimal`` quantities, a foreign enum) raises.
- **Deterministic and explainable.** Thresholds are module constants quoted in
  :data:`POLICY_HASH`; expert order is irrelevant; reason codes are canonical.
  :meth:`TraderDecisionPolicy.explain` returns the full provenance (candidates,
  legal set, evidence weights, active constraints) behind the action.
- **Dependency-injected views.** Belief, opportunity and expert stances are
  read through small :class:`typing.Protocol` descriptions, so this module can
  be wired to the live belief ledger while neither side imports the other.

Evidence is reduced to two weighted sums. ``support`` and ``contradict`` are the
sums of expert weights in ``[0, 1]``; ``net_ratio = (support - contradict) /
(support + contradict)`` in ``[-1, 1]``. Decisions are read from that pair:

======================  ==============================================  ===============
Condition               Action                                          Reasons
======================  ==============================================  ===============
no exposure             ``support >= 0.50`` and ``net_ratio >= 0.25``    ``ENTER`` / ``WAIT`` gates
no exposure             ``net_ratio <= -0.20``                          ``ABSTAIN``
no exposure             minority share ``>= 0.25``                       ``WAIT`` (conflict)
no exposure             neither                                           ``WAIT`` (no edge)
exposure                ``net_ratio <= -0.60``                          ``EXIT``
exposure                ``net_ratio <= -0.20``                          ``REDUCE``
exposure                otherwise                                       ``HOLD``
stale/unknown belief    —                                               ``ABSTAIN``
======================  ==============================================  ===============
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from types import MappingProxyType
from typing import Annotated, Final, Mapping, NamedTuple, Protocol, Self, Sequence

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    model_validator,
)

from v8_next.domain.decision import (
    DecisionAction,
    IntentKind,
    OpportunityLifecycle,
    ReasonCode,
    TraderDecision,
    VenueOrderStatus,
)
from v8_next.economics.decisions import StanceKind
from v8_next.economics.legality import (
    PRECEDENCE,
    LegalActionTable,
    PositionPresence,
    resolve_legal_conflict,
)
from v8_next.telemetry.identity import canonical_digest

__all__ = [
    "POLICY_HASH",
    "POLICY_HASHES",
    "POLICY_VERSION",
    "CapacityState",
    "DecisionRequest",
    "ExpertStanceView",
    "BeliefView",
    "OpportunityView",
    "PolicyExplanation",
    "PortfolioState",
    "PositionState",
    "TraderDecisionPolicy",
    "policy_hash_for",
]

#: Version of the thresholds below. Bumping any threshold that changes an
#: action requires a new version and a new :data:`POLICY_HASH`.
POLICY_VERSION: Final[str] = "trader-policy-v1"

#: Schema tag of the policy explanation payload.
EXPLANATION_SCHEMA_VERSION: Final[str] = "trader-policy-explanation-v1"

# --------------------------------------------------------------------- thresholds

#: Absolute weighted support needed before an entry is even considered.
ENTER_MIN_SUPPORT: Final[Decimal] = Decimal("0.50")
#: Relative direction needed before an entry is considered; prevents two tiny
#: agreeing experts from outvoting one large dissenter on magnitude alone.
ENTER_MIN_NET_RATIO: Final[Decimal] = Decimal("0.25")
#: Net contradiction at or below this (with exposure) proposes ``REDUCE``.
REDUCE_NET_RATIO: Final[Decimal] = Decimal("-0.20")
#: Net contradiction at or below this (with exposure) proposes ``EXIT``.
EXIT_NET_RATIO: Final[Decimal] = Decimal("-0.60")
#: Minority weight share that makes the expert panel internally inconsistent.
MIXED_EVIDENCE_RATIO: Final[Decimal] = Decimal("0.25")
#: Belief uncertainty above this blocks new entry (not management).
ENTRY_UNCERTAINTY_CEILING: Final[Decimal] = Decimal("0.60")
#: Gross exposure / equity above which an open position is reduced.
MAX_GROSS_EXPOSURE_RATIO: Final[Decimal] = Decimal("0.80")
#: Precision kept on derived ratios so explanations are stable across hosts.
RATIO_PRECISION: Final[Decimal] = Decimal("0.0001")

_POLICY_PARAMETERS: Final[Mapping[str, str]] = MappingProxyType(
    {
        "enter_min_support": str(ENTER_MIN_SUPPORT),
        "enter_min_net_ratio": str(ENTER_MIN_NET_RATIO),
        "reduce_net_ratio": str(REDUCE_NET_RATIO),
        "exit_net_ratio": str(EXIT_NET_RATIO),
        "mixed_evidence_ratio": str(MIXED_EVIDENCE_RATIO),
        "entry_uncertainty_ceiling": str(ENTRY_UNCERTAINTY_CEILING),
        "max_gross_exposure_ratio": str(MAX_GROSS_EXPOSURE_RATIO),
        "ratio_precision": str(RATIO_PRECISION),
        "entry_min_weight_exclusive": "false",
    }
)

#: Digest of the policy version plus every threshold and the precedence order.
POLICY_HASH: Final[str] = canonical_digest(
    EXPLANATION_SCHEMA_VERSION,
    {
        "policy_version": POLICY_VERSION,
        "parameters": dict(_POLICY_PARAMETERS),
        "precedence": [action.value for action in PRECEDENCE],
    },
)

#: Versions whose hash this build can certify. Receipts fail closed on anything
#: outside this registry rather than recording an unverifiable provenance claim.
POLICY_HASHES: Final[Mapping[str, str]] = MappingProxyType({POLICY_VERSION: POLICY_HASH})


def policy_hash_for(version: str) -> str | None:
    """Hash of a policy version this build knows, else ``None`` (fail closed)."""
    return POLICY_HASHES.get(version)


# ------------------------------------------------------------------------- schema


def _require_decimal(value: object) -> object:
    if not isinstance(value, Decimal):
        raise ValueError("money/exposure values must be Decimal, never float")
    return value


#: ``Decimal`` that rejects float coercion; deterministic by construction.
StrictDecimal = Annotated[Decimal, BeforeValidator(_require_decimal), Field(allow_inf_nan=False)]


class CapacityState(StrEnum):
    """Portfolio capacity as read, never as hoped.

    The default is ``UNKNOWN``: an unread capacity is a blocked entry, not an
    implicit "there is room".
    """

    AVAILABLE = "AVAILABLE"
    FULL = "FULL"
    UNKNOWN = "UNKNOWN"


class PositionState(BaseModel):
    """What we believe about our own exposure, with its size.

    ``exposure`` is the absolute notional at risk; ``quantity`` is signed.
    Contradictory states (``NONE`` carrying exposure, ``OPEN`` carrying nothing,
    ``UNCERTAIN`` asserting a number) are rejected at construction instead of
    being resolved silently.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    presence: PositionPresence
    quantity: StrictDecimal = Decimal("0")
    exposure: StrictDecimal = Decimal("0")

    @model_validator(mode="after")
    def _validate_presence(self) -> Self:
        if self.exposure < 0:
            raise ValueError("POSITION: exposure cannot be negative")
        empty = self.quantity == 0 and self.exposure == 0
        if self.presence is PositionPresence.NONE and not empty:
            raise ValueError("POSITION: NONE presence cannot carry exposure")
        if self.presence is PositionPresence.UNCERTAIN and not empty:
            raise ValueError("POSITION: UNCERTAIN presence cannot assert exposure")
        if self.presence is PositionPresence.OPEN and empty:
            raise ValueError("POSITION: OPEN presence must carry quantity or exposure")
        return self


class PortfolioState(BaseModel):
    """Portfolio capacity read at the decision cut."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    equity: StrictDecimal = Decimal("0")
    committed_exposure: StrictDecimal = Decimal("0")
    capacity: CapacityState = CapacityState.UNKNOWN

    @model_validator(mode="after")
    def _validate_amounts(self) -> Self:
        if self.equity < 0:
            raise ValueError("PORTFOLIO: equity cannot be negative")
        if self.committed_exposure < 0:
            raise ValueError("PORTFOLIO: committed exposure cannot be negative")
        return self


class BeliefView(Protocol):
    """Structural view of a belief snapshot (no import of ``v8_next.belief``).

    ``as_of_ns`` is optional: when present it is enforced against the decision
    cut, so a stale-but-unflagged snapshot cannot smuggle future knowledge in.
    """

    version_hash: str
    regime: str
    uncertainty: Decimal
    stale: bool
    abstain: bool


class OpportunityView(Protocol):
    """Structural view of one opportunity episode (book id + lifecycle)."""

    opportunity_id: str
    lifecycle: OpportunityLifecycle


class ExpertStanceView(Protocol):
    """Structural view of one expert's stance at the cut."""

    expert_id: str
    stance: str
    weight: Decimal


@dataclass(frozen=True, slots=True)
class DecisionRequest:
    """Everything the policy is allowed to know, injected by the caller.

    Built once per cut; the policy holds no state and reads nothing global. The
    envelope validates itself (a malformed envelope is a caller bug and raises),
    while unreadable *data* inside it degrades to a typed fail-closed action.
    """

    decision_time_ns: int
    instrument_id: str
    venue_status: VenueOrderStatus
    position: PositionState
    portfolio: PortfolioState
    opportunity: OpportunityView | None = None
    belief: BeliefView | None = None
    expert_stances: Sequence[ExpertStanceView] = ()

    def __post_init__(self) -> None:
        if (
            isinstance(self.decision_time_ns, bool)
            or not isinstance(self.decision_time_ns, int)
            or self.decision_time_ns < 0
        ):
            raise ValueError("DECISION_REQUEST: decision_time_ns must be a non-negative int")
        if not isinstance(self.instrument_id, str) or not self.instrument_id.strip():
            raise ValueError("DECISION_REQUEST: instrument_id cannot be empty")
        if not isinstance(self.venue_status, VenueOrderStatus):
            raise TypeError("DECISION_REQUEST: venue_status must be VenueOrderStatus")
        if not isinstance(self.position, PositionState):
            raise TypeError("DECISION_REQUEST: position must be PositionState")
        if not isinstance(self.portfolio, PortfolioState):
            raise TypeError("DECISION_REQUEST: portfolio must be PortfolioState")
        object.__setattr__(self, "expert_stances", tuple(self.expert_stances))


class PolicyExplanation(BaseModel):
    """Auditable provenance of one advisory decision.

    Carries no order, size or price: it explains *why* an action was proposed,
    which is what the decision receipt archives.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = EXPLANATION_SCHEMA_VERSION
    policy_version: str = POLICY_VERSION
    policy_hash: str = POLICY_HASH
    decision_id: str
    action: DecisionAction
    intent_kind: IntentKind
    reason_codes: tuple[ReasonCode, ...]
    candidates: tuple[DecisionAction, ...]
    legal_actions: tuple[DecisionAction, ...]
    support_weight: Decimal
    contradict_weight: Decimal
    net_evidence_ratio: Decimal
    uncertainty: Decimal | None
    regime: str | None
    opportunity_id: str | None
    lifecycle: OpportunityLifecycle | None
    stale: bool
    armed: bool
    constraints: tuple[str, ...]

    @model_validator(mode="after")
    def _validate_provenance(self) -> Self:
        if self.action not in self.legal_actions:
            raise ValueError("EXPLANATION: selected action must be legal")
        if not set(self.candidates) <= set(DecisionAction):
            raise ValueError("EXPLANATION: candidates must be decision actions")
        if len(set(self.reason_codes)) != len(self.reason_codes):
            raise ValueError("EXPLANATION: duplicate reason codes")
        return self


# ---------------------------------------------------------------------- internals


class _BeliefFacts(NamedTuple):
    version_hash: str
    regime: str
    uncertainty: Decimal
    stale: bool
    abstain: bool


class _OpportunityFacts(NamedTuple):
    opportunity_id: str
    lifecycle: OpportunityLifecycle


class _Assessment(NamedTuple):
    action: DecisionAction
    intent_kind: IntentKind
    reason_codes: tuple[ReasonCode, ...]
    candidates: tuple[DecisionAction, ...]
    legal: tuple[DecisionAction, ...]
    support: Decimal
    contradict: Decimal
    net_ratio: Decimal
    uncertainty: Decimal | None
    regime: str | None
    opportunity_id: str | None
    lifecycle: OpportunityLifecycle | None
    belief_version_hash: str | None
    stale: bool
    armed: bool
    constraints: tuple[str, ...]


_INTENT_BY_ACTION: Final[Mapping[DecisionAction, IntentKind]] = MappingProxyType(
    {
        DecisionAction.WAIT: IntentKind.NONE,
        DecisionAction.ABSTAIN: IntentKind.NONE,
        DecisionAction.ENTER: IntentKind.LIMIT_ENTRY,
        DecisionAction.HOLD: IntentKind.PROTECTIVE,
        DecisionAction.REDUCE: IntentKind.REDUCE,
        DecisionAction.EXIT: IntentKind.EXIT,
    }
)
if set(_INTENT_BY_ACTION) != set(DecisionAction):  # pragma: no cover - import guard
    raise AssertionError("every decision action needs exactly one intent kind")


def _ordered(actions: Iterable[DecisionAction]) -> tuple[DecisionAction, ...]:
    """Sort by risk-first precedence so output never depends on input order."""
    return tuple(sorted(actions, key=PRECEDENCE.index))


def _ratio(numerator: Decimal, denominator: Decimal) -> Decimal:
    if denominator == 0:
        return Decimal("0")
    return (numerator / denominator).quantize(RATIO_PRECISION)


def _normalize_stance(raw: object) -> StanceKind | None:
    if isinstance(raw, StanceKind):
        return raw
    if isinstance(raw, str):
        try:
            return StanceKind(raw.strip().upper())
        except ValueError:
            return None
    return None


def _read_belief(
    belief: object | None, decision_time_ns: int
) -> tuple[_BeliefFacts | None, ReasonCode | None]:
    """Read the belief view, or name why it cannot be used.

    Any unreadable shape is ``BELIEF_MISSING``; a snapshot dated after the cut is
    ``INPUT_FUTURE``. Neither falls back to a defaulted belief.
    """
    if belief is None:
        return None, ReasonCode.BELIEF_MISSING
    version_hash = getattr(belief, "version_hash", None)
    regime = getattr(belief, "regime", None)
    uncertainty = getattr(belief, "uncertainty", None)
    stale = getattr(belief, "stale", None)
    abstain = getattr(belief, "abstain", None)
    as_of_ns = getattr(belief, "as_of_ns", None)
    if not isinstance(version_hash, str) or not version_hash.strip():
        return None, ReasonCode.BELIEF_MISSING
    if not isinstance(regime, str) or not regime.strip() or regime.strip().upper() == "UNKNOWN":
        return None, ReasonCode.BELIEF_MISSING
    if (
        not isinstance(uncertainty, Decimal)
        or not uncertainty.is_finite()
        or uncertainty < 0
        or uncertainty > 1
    ):
        return None, ReasonCode.BELIEF_MISSING
    if not isinstance(stale, bool) or not isinstance(abstain, bool):
        return None, ReasonCode.BELIEF_MISSING
    if as_of_ns is not None:
        if isinstance(as_of_ns, bool) or not isinstance(as_of_ns, int) or as_of_ns < 0:
            return None, ReasonCode.BELIEF_MISSING
        if as_of_ns > decision_time_ns:
            return None, ReasonCode.INPUT_FUTURE
    return _BeliefFacts(version_hash, regime, uncertainty, stale, abstain), None


def _read_opportunity(
    opportunity: object | None, decision_time_ns: int
) -> tuple[_OpportunityFacts | None, ReasonCode | None]:
    """Read the opportunity view; a missing or unreadable episode is not armed."""
    if opportunity is None:
        return None, ReasonCode.OPPORTUNITY_NOT_ARMED
    opportunity_id = getattr(opportunity, "opportunity_id", None)
    lifecycle = getattr(opportunity, "lifecycle", None)
    as_of_ns = getattr(opportunity, "as_of_ns", None)
    if not isinstance(opportunity_id, str) or not opportunity_id.strip():
        return None, ReasonCode.OPPORTUNITY_NOT_ARMED
    if isinstance(lifecycle, OpportunityLifecycle):
        resolved = lifecycle
    elif isinstance(lifecycle, str):
        try:
            resolved = OpportunityLifecycle(lifecycle)
        except ValueError:
            return None, ReasonCode.OPPORTUNITY_NOT_ARMED
    else:
        return None, ReasonCode.OPPORTUNITY_NOT_ARMED
    if as_of_ns is not None:
        if isinstance(as_of_ns, bool) or not isinstance(as_of_ns, int) or as_of_ns < 0:
            return None, ReasonCode.OPPORTUNITY_NOT_ARMED
        if as_of_ns > decision_time_ns:
            return None, ReasonCode.INPUT_FUTURE
    return _OpportunityFacts(opportunity_id, resolved), None


def _read_stances(
    stances: Sequence[ExpertStanceView],
) -> tuple[Decimal, Decimal, bool]:
    """Weighted support/contradiction, and whether any stance was unreadable.

    An unknown stance word, an out-of-range or non-``Decimal`` weight and a
    duplicated expert id all make the panel unreadable; a panel with a single
    unreadable member cannot be trusted as a consensus, so the caller abstains.
    """
    support = Decimal("0")
    contradict = Decimal("0")
    seen: set[str] = set()
    unreadable = False
    for stance in stances:
        expert_id = getattr(stance, "expert_id", None)
        raw_stance = getattr(stance, "stance", None)
        weight = getattr(stance, "weight", None)
        if not isinstance(expert_id, str) or not expert_id.strip():
            unreadable = True
            continue
        if expert_id in seen:
            unreadable = True
            continue
        seen.add(expert_id)
        kind = _normalize_stance(raw_stance)
        if kind is None:
            unreadable = True
            continue
        if isinstance(weight, bool) or not isinstance(weight, (Decimal, int)):
            unreadable = True
            continue
        weight_decimal = Decimal(weight)
        if weight_decimal < 0 or weight_decimal > 1:
            unreadable = True
            continue
        if kind is StanceKind.SUPPORT:
            support += weight_decimal
        elif kind is StanceKind.CONTRADICT:
            contradict += weight_decimal
    return support, contradict, unreadable


# ------------------------------------------------------------------------ policy


class TraderDecisionPolicy:
    """Stateless advisory policy: one request in, one decision out.

    The instance owns no mutable state and no dependency beyond its modules, so
    replaying the same request mints the same ``decision_id``. Shadow mode is a
    property of the type (advisory-only output), not a switch that can be left
    enabled by misconfiguration.
    """

    __slots__ = ()

    @property
    def policy_version(self) -> str:
        return POLICY_VERSION

    @property
    def policy_hash(self) -> str:
        return POLICY_HASH

    @property
    def shadow_mode(self) -> bool:
        """True: this policy records proposals and never acts on them."""
        return True

    def evaluate(self, request: DecisionRequest) -> TraderDecision:
        """Return the advisory decision for one cut."""
        assessment = self._assess(request)
        return TraderDecision.create(
            decision_time_ns=request.decision_time_ns,
            instrument_id=request.instrument_id,
            opportunity_id=assessment.opportunity_id,
            action=assessment.action,
            intent_kind=assessment.intent_kind,
            policy_version=POLICY_VERSION,
            reason_codes=assessment.reason_codes,
            belief_version_hash=assessment.belief_version_hash,
        )

    def explain(self, request: DecisionRequest) -> PolicyExplanation:
        """Return the full provenance behind :meth:`evaluate` for this request."""
        assessment = self._assess(request)
        decision_id = TraderDecision.create(
            decision_time_ns=request.decision_time_ns,
            instrument_id=request.instrument_id,
            opportunity_id=assessment.opportunity_id,
            action=assessment.action,
            intent_kind=assessment.intent_kind,
            policy_version=POLICY_VERSION,
            reason_codes=assessment.reason_codes,
            belief_version_hash=assessment.belief_version_hash,
        ).decision_id
        return PolicyExplanation(
            decision_id=decision_id,
            action=assessment.action,
            intent_kind=assessment.intent_kind,
            reason_codes=assessment.reason_codes,
            candidates=assessment.candidates,
            legal_actions=assessment.legal,
            support_weight=assessment.support,
            contradict_weight=assessment.contradict,
            net_evidence_ratio=assessment.net_ratio,
            uncertainty=assessment.uncertainty,
            regime=assessment.regime,
            opportunity_id=assessment.opportunity_id,
            lifecycle=assessment.lifecycle,
            stale=assessment.stale,
            armed=assessment.armed,
            constraints=assessment.constraints,
        )

    # ------------------------------------------------------------------ internals

    def _assess(self, request: DecisionRequest) -> _Assessment:  # noqa: C901
        reasons: set[ReasonCode] = set()
        candidates: set[DecisionAction] = set()
        constraints: set[str] = set()

        belief, belief_reason = _read_belief(request.belief, request.decision_time_ns)
        opportunity, opportunity_reason = _read_opportunity(
            request.opportunity, request.decision_time_ns
        )
        support, contradict, unreadable_stances = _read_stances(request.expert_stances)

        presence = request.position.presence
        exposed = presence is PositionPresence.OPEN or request.position.quantity != 0
        stale = belief.stale if belief is not None else False

        # An unreadable position cannot justify any management action.
        if presence is PositionPresence.UNCERTAIN:
            candidates.add(DecisionAction.ABSTAIN)
            reasons.add(ReasonCode.POSITION_UNCERTAIN)
            constraints.add("POSITION_UNCERTAIN")
        if presence is PositionPresence.PENDING_INTENT:
            reasons.add(ReasonCode.VENUE_PENDING)
            constraints.add("VENUE_PENDING")

        # Belief: stale, abstaining or unreadable evidence blocks new risk.
        if belief is None:
            candidates.add(DecisionAction.ABSTAIN)
            reasons.add(belief_reason or ReasonCode.BELIEF_MISSING)
            constraints.add("BELIEF_UNUSABLE")
        elif stale:
            candidates.add(DecisionAction.ABSTAIN)
            reasons.add(ReasonCode.BELIEF_STALE)
            constraints.add("STALE")
        elif belief.abstain:
            candidates.add(DecisionAction.ABSTAIN)
            reasons.add(ReasonCode.EVIDENCE_INSUFFICIENT)
            constraints.add("BELIEF_ABSTAIN")

        # Opportunity lifecycle.
        armed = False
        lifecycle: OpportunityLifecycle | None = None
        if opportunity is None:
            candidates.add(DecisionAction.ABSTAIN)
            reasons.add(opportunity_reason or ReasonCode.OPPORTUNITY_NOT_ARMED)
            constraints.add("OPPORTUNITY_MISSING")
        else:
            lifecycle = opportunity.lifecycle
            armed = lifecycle is OpportunityLifecycle.ARMED
            if lifecycle is OpportunityLifecycle.INVALIDATED:
                reasons.add(ReasonCode.OPPORTUNITY_INVALIDATED)
                constraints.add("OPPORTUNITY_TERMINAL")
                if exposed:
                    candidates.add(DecisionAction.EXIT)
                    reasons.add(ReasonCode.POSITION_EXIT_REQUIRED)
                else:
                    candidates.add(DecisionAction.ABSTAIN)
            elif lifecycle is OpportunityLifecycle.EXPIRED:
                reasons.add(ReasonCode.OPPORTUNITY_EXPIRED)
                constraints.add("OPPORTUNITY_TERMINAL")
                if exposed:
                    candidates.add(DecisionAction.EXIT)
                    reasons.add(ReasonCode.POSITION_EXIT_REQUIRED)
                else:
                    candidates.add(DecisionAction.ABSTAIN)
            elif armed:
                reasons.add(ReasonCode.OPPORTUNITY_ARMED)
            else:
                reasons.add(ReasonCode.OPPORTUNITY_NOT_ARMED)
                constraints.add("OPPORTUNITY_NOT_ARMED")

        # Venue state: an unreadable venue is a blocking reason, not a default.
        if request.venue_status is VenueOrderStatus.UNKNOWN:
            candidates.add(DecisionAction.ABSTAIN)
            reasons.add(ReasonCode.VENUE_UNKNOWN)
            constraints.add("VENUE_UNKNOWN")
        elif request.venue_status in {
            VenueOrderStatus.SUBMITTED,
            VenueOrderStatus.ACCEPTED,
        }:
            reasons.add(ReasonCode.VENUE_PENDING)
            constraints.add("VENUE_PENDING")

        # Portfolio capacity.
        capacity = request.portfolio.capacity
        equity = request.portfolio.equity
        gross = max(request.position.exposure, request.portfolio.committed_exposure)
        budget = equity * MAX_GROSS_EXPOSURE_RATIO
        if capacity is CapacityState.UNKNOWN:
            reasons.add(ReasonCode.RISK_UNKNOWN)
            constraints.add("CAPACITY_UNKNOWN")
        elif capacity is CapacityState.FULL:
            reasons.add(ReasonCode.RISK_DENIED)
            constraints.add("CAPACITY_FULL")
        if equity <= 0:
            reasons.add(ReasonCode.RISK_DENIED)
            constraints.add("NO_EQUITY")
        if equity > 0 and gross > budget:
            constraints.add("GROSS_EXPOSURE_EXCEEDED")
            if exposed:
                candidates.add(DecisionAction.REDUCE)
                reasons.add(ReasonCode.POSITION_REDUCE_REQUIRED)
            else:
                reasons.add(ReasonCode.RISK_DENIED)
                constraints.add("PORTFOLIO_COMMITTED")

        # Evidence ladder: only meaningful when the belief itself is usable and
        # the position is readable enough to act on.
        net_ratio = _ratio(support - contradict, support + contradict)
        if (
            belief is not None
            and not stale
            and not belief.abstain
            and presence is not PositionPresence.UNCERTAIN
        ):
            if unreadable_stances:
                candidates.add(DecisionAction.ABSTAIN)
                reasons.add(ReasonCode.EVIDENCE_CONFLICT)
                constraints.add("EVIDENCE_UNREADABLE")
            elif support + contradict == 0:
                reasons.add(ReasonCode.NO_EDGE)
                if exposed:
                    candidates.add(DecisionAction.HOLD)
                    reasons.add(ReasonCode.POSITION_OPEN)
                else:
                    candidates.add(DecisionAction.WAIT)
            else:
                minority = _ratio(min(support, contradict), support + contradict)
                mixed = minority >= MIXED_EVIDENCE_RATIO
                if exposed:
                    if net_ratio <= EXIT_NET_RATIO:
                        candidates.add(DecisionAction.EXIT)
                        reasons.add(ReasonCode.POSITION_EXIT_REQUIRED)
                    elif net_ratio <= REDUCE_NET_RATIO:
                        candidates.add(DecisionAction.REDUCE)
                        reasons.add(ReasonCode.POSITION_REDUCE_REQUIRED)
                    else:
                        candidates.add(DecisionAction.HOLD)
                        reasons.add(ReasonCode.POSITION_OPEN)
                        if mixed:
                            reasons.add(ReasonCode.EVIDENCE_CONFLICT)
                        if support >= ENTER_MIN_SUPPORT:
                            reasons.add(ReasonCode.SCALE_IN_DISABLED)
                elif net_ratio <= REDUCE_NET_RATIO:
                    candidates.add(DecisionAction.ABSTAIN)
                    reasons.add(ReasonCode.BELIEF_CONTRADICTORY)
                elif mixed:
                    candidates.add(DecisionAction.WAIT)
                    reasons.add(ReasonCode.EVIDENCE_CONFLICT)
                elif support >= ENTER_MIN_SUPPORT and net_ratio >= ENTER_MIN_NET_RATIO:
                    entry_actions, entry_reasons = self._entry_candidates(
                        request, belief, armed, capacity
                    )
                    candidates.update(entry_actions)
                    reasons.update(entry_reasons)
                else:
                    candidates.add(DecisionAction.WAIT)
                    reasons.add(ReasonCode.EVIDENCE_INSUFFICIENT)

        if not candidates:
            candidates.add(DecisionAction.WAIT)
            reasons.add(ReasonCode.POLICY_WAIT)

        legal = LegalActionTable.legal(
            presence,
            venue=request.venue_status,
            stale=stale,
            armed_opportunity=armed,
        )
        action = resolve_legal_conflict(_ordered(candidates), legal=legal)
        if not reasons:
            reasons.add(ReasonCode.POLICY_WAIT)

        return _Assessment(
            action=action,
            intent_kind=_INTENT_BY_ACTION[action],
            reason_codes=tuple(sorted(reasons)),
            candidates=_ordered(candidates),
            legal=_ordered(legal),
            support=support,
            contradict=contradict,
            net_ratio=net_ratio,
            uncertainty=belief.uncertainty if belief is not None else None,
            regime=belief.regime if belief is not None else None,
            opportunity_id=opportunity.opportunity_id if opportunity is not None else None,
            lifecycle=lifecycle,
            belief_version_hash=belief.version_hash if belief is not None else None,
            stale=stale,
            armed=armed,
            constraints=tuple(sorted(constraints)),
        )

    @staticmethod
    def _entry_candidates(
        request: DecisionRequest,
        belief: _BeliefFacts,
        armed: bool,
        capacity: CapacityState,
    ) -> tuple[set[DecisionAction], set[ReasonCode]]:
        """Gate a proposed entry; every failure waits and names its reason."""
        wait = {DecisionAction.WAIT}
        if belief.uncertainty > ENTRY_UNCERTAINTY_CEILING:
            return wait, {ReasonCode.EVIDENCE_INSUFFICIENT}
        if not armed:
            return wait, {ReasonCode.OPPORTUNITY_NOT_ARMED}
        if capacity is not CapacityState.AVAILABLE:
            blocked = (
                ReasonCode.RISK_UNKNOWN
                if capacity is CapacityState.UNKNOWN
                else ReasonCode.RISK_DENIED
            )
            return wait, {blocked}
        if request.position.presence is not PositionPresence.NONE:
            return wait, set()
        equity = request.portfolio.equity
        if equity <= 0:
            return wait, {ReasonCode.RISK_DENIED}
        gross = max(request.position.exposure, request.portfolio.committed_exposure)
        if gross > equity * MAX_GROSS_EXPOSURE_RATIO:
            return wait, {ReasonCode.RISK_DENIED}
        return {DecisionAction.ENTER}, set()
