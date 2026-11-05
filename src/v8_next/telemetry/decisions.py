"""Immutable decision receipts and an append-only decision ledger (M2-05).

A :class:`~v8_next.domain.decision.TraderDecision` says *what* the advisory
policy proposed. A :class:`DecisionReceipt` archives *why it may be believed*:
the exact input events used (observation ids plus their availability), the
belief version hash, the opportunity episode, the policy version **and hash**,
the active constraints, the structured reason codes and the proposed intent.

Invariants:

- **Immutable and append-only.** Receipts are frozen pydantic models; the
  ledger only ever appends. Re-appending the same ``decision_id`` with the same
  body is idempotent (one record, the original digest); a different body under
  the same id is refused by name.
- **Same replay ⇒ same digest.** The digest covers content only, in a fixed
  order; neither wall-clock time nor insertion order participate. Observations
  are sorted by id before hashing, so caller ordering cannot change it.
- **Point-in-time or nothing.** Every input carries ``available_at_ns``. A
  receipt whose newest input was not available at the decision cut is rejected
  on append and fails :meth:`DecisionReceipt.verify`.
- **Fail-closed provenance.** Only policy versions this build can certify
  (:data:`~v8_next.economics.policy.POLICY_HASHES`) may be recorded; an unknown
  version raises instead of archiving an unverifiable hash. A non-``WAIT`` /
  non-``ABSTAIN`` decision with no opportunity is an *orphan*: rejected by
  default, recordable only through the explicit ``allow_orphans`` audit path,
  and always reported by :meth:`DecisionLedger.orphans`.
- **A proposal is not a trade.** The receipt records ``proposed_intent`` only.
  It has no quantity, price, fill, cashflow or venue field, and no module here
  imports an execution adapter: nothing in this file can move money.
"""

from __future__ import annotations

from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator

from v8_next.domain.decision import (
    DecisionAction,
    IntentKind,
    ReasonCode,
    TraderDecision,
    derive_decision_id,
)
from v8_next.economics.policy import POLICY_HASHES, policy_hash_for
from v8_next.telemetry.identity import canonical_digest

__all__ = [
    "DecisionInputRef",
    "DecisionLedger",
    "DecisionReceipt",
    "DecisionReceiptError",
    "OrphanDecision",
    "ReceiptConflict",
    "ReceiptInputs",
    "ReceiptRejected",
    "SCHEMA_VERSION",
    "UnknownPolicyVersion",
    "is_orphan",
]

#: Domain-separated schema tag of the receipt digest payload.
SCHEMA_VERSION: Final[str] = "decision-receipt-v1"

#: Actions that carry no intent and therefore need no opportunity episode.
_NO_INTENT_ACTIONS: Final[frozenset[DecisionAction]] = frozenset(
    {DecisionAction.WAIT, DecisionAction.ABSTAIN}
)


class DecisionReceiptError(ValueError):
    """A receipt or ledger contract was violated.

    ``code`` is the machine-readable discriminator so consumers branch on it,
    never on message prose.
    """

    code: str = "DECISION_RECEIPT_ERROR"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        resolved = code or type(self).code
        super().__init__(f"{resolved}: {message}")
        self.code = resolved


class ReceiptRejected(DecisionReceiptError):
    """The receipt cannot be trusted: bad provenance, non-PIT input or bad digest."""

    code = "RECEIPT_REJECTED"


class ReceiptConflict(DecisionReceiptError):
    """A different receipt body claims an already-recorded ``decision_id``."""

    code = "RECEIPT_CONFLICT"


class OrphanDecision(DecisionReceiptError):
    """An actionable decision references no opportunity episode."""

    code = "ORPHAN_DECISION"


class UnknownPolicyVersion(DecisionReceiptError):
    """The decision names a policy version this build cannot certify."""

    code = "UNKNOWN_POLICY_VERSION"


def is_orphan(action: DecisionAction, opportunity_id: str | None) -> bool:
    """An actionable decision with no referenced opportunity is unaccountable."""
    return opportunity_id is None and action not in _NO_INTENT_ACTIONS


class DecisionInputRef(BaseModel):
    """One input event used by a decision, with the time it became usable."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    observation_id: str
    available_at_ns: StrictInt = Field(ge=0)

    @model_validator(mode="after")
    def _validate_identity(self) -> DecisionInputRef:
        if not self.observation_id.strip():
            raise ValueError("RECEIPT_INPUT: observation_id cannot be empty")
        return self


class ReceiptInputs(BaseModel):
    """Caller-supplied evidence bundle for one append."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    inputs: tuple[DecisionInputRef, ...] = ()
    constraints: tuple[str, ...] = ()

    def ordered(self) -> tuple[DecisionInputRef, ...]:
        """Inputs sorted by observation id, rejecting duplicate references."""
        ordered = tuple(sorted(self.inputs, key=lambda ref: ref.observation_id))
        ids = [ref.observation_id for ref in ordered]
        if len(set(ids)) != len(ids):
            raise ReceiptRejected("duplicate observation reference", code="DUPLICATE_INPUT")
        return ordered


def _digest(
    *,
    decision_id: str,
    decision_time_ns: int,
    instrument_id: str,
    opportunity_id: str | None,
    action: DecisionAction,
    proposed_intent: IntentKind,
    reason_codes: tuple[ReasonCode, ...],
    policy_version: str,
    policy_hash: str,
    belief_version_hash: str | None,
    observation_ids: tuple[str, ...],
    input_available_at_ns: tuple[int, ...],
    max_input_available_at_ns: int | None,
    constraints: tuple[str, ...],
) -> str:
    """Content digest: fixed field order, no wall clock, no insertion position."""
    return canonical_digest(
        SCHEMA_VERSION,
        [
            decision_id,
            decision_time_ns,
            instrument_id,
            opportunity_id,
            action.value,
            proposed_intent.value,
            [code.value for code in reason_codes],
            policy_version,
            policy_hash,
            belief_version_hash,
            list(observation_ids),
            list(input_available_at_ns),
            max_input_available_at_ns,
            list(constraints),
        ],
    )


class DecisionReceipt(BaseModel):
    """Frozen snapshot of one advisory decision and its provenance."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = SCHEMA_VERSION
    digest: str
    decision_id: str
    decision_time_ns: StrictInt = Field(ge=0)
    instrument_id: str
    opportunity_id: str | None
    action: DecisionAction
    proposed_intent: IntentKind
    reason_codes: tuple[ReasonCode, ...] = ()
    policy_version: str
    policy_hash: str
    belief_version_hash: str | None = None
    observation_ids: tuple[str, ...] = ()
    input_available_at_ns: tuple[StrictInt, ...] = ()
    max_input_available_at_ns: StrictInt | None = None
    constraints: tuple[str, ...] = ()
    advisory_only: Literal[True] = True

    @model_validator(mode="after")
    def _validate_identity(self) -> DecisionReceipt:
        if not self.instrument_id.strip():
            raise ValueError("RECEIPT: instrument_id cannot be empty")
        if not self.policy_version.strip():
            raise ValueError("RECEIPT: policy_version cannot be empty")
        if not self.digest.strip():
            raise ValueError("RECEIPT: digest cannot be empty")
        return self

    @classmethod
    def create(cls, *, decision: TraderDecision, inputs: ReceiptInputs) -> DecisionReceipt:
        """Archive a decision with the evidence that produced it.

        PIT is *checked* by :meth:`verify` and by the ledger, not here: this
        factory can mint the self-consistent non-PIT value a verifier must
        reject. An unknown policy version is refused outright, because an
        uncertifiable hash is not provenance.
        """
        policy_hash = policy_hash_for(decision.policy_version)
        if policy_hash is None:
            raise UnknownPolicyVersion(
                f"no certified hash for policy version {decision.policy_version!r}"
            )
        ordered = inputs.ordered()
        observation_ids = tuple(ref.observation_id for ref in ordered)
        available = tuple(ref.available_at_ns for ref in ordered)
        max_available = max(available) if available else None
        constraints = tuple(sorted(set(inputs.constraints)))
        digest = _digest(
            decision_id=decision.decision_id,
            decision_time_ns=decision.decision_time_ns,
            instrument_id=decision.instrument_id,
            opportunity_id=decision.opportunity_id,
            action=decision.action,
            proposed_intent=decision.intent_kind,
            reason_codes=decision.reason_codes,
            policy_version=decision.policy_version,
            policy_hash=policy_hash,
            belief_version_hash=decision.belief_version_hash,
            observation_ids=observation_ids,
            input_available_at_ns=available,
            max_input_available_at_ns=max_available,
            constraints=constraints,
        )
        return cls(
            digest=digest,
            decision_id=decision.decision_id,
            decision_time_ns=decision.decision_time_ns,
            instrument_id=decision.instrument_id,
            opportunity_id=decision.opportunity_id,
            action=decision.action,
            proposed_intent=decision.intent_kind,
            reason_codes=decision.reason_codes,
            policy_version=decision.policy_version,
            policy_hash=policy_hash,
            belief_version_hash=decision.belief_version_hash,
            observation_ids=observation_ids,
            input_available_at_ns=available,
            max_input_available_at_ns=max_available,
            constraints=constraints,
        )

    def _recompute_digest(self) -> str:
        return _digest(
            decision_id=self.decision_id,
            decision_time_ns=self.decision_time_ns,
            instrument_id=self.instrument_id,
            opportunity_id=self.opportunity_id,
            action=self.action,
            proposed_intent=self.proposed_intent,
            reason_codes=self.reason_codes,
            policy_version=self.policy_version,
            policy_hash=self.policy_hash,
            belief_version_hash=self.belief_version_hash,
            observation_ids=self.observation_ids,
            input_available_at_ns=self.input_available_at_ns,
            max_input_available_at_ns=self.max_input_available_at_ns,
            constraints=self.constraints,
        )

    def verify(self, *, expected_policy_hash: str | None = None) -> bool:
        """True only for a self-consistent, PIT, policy-certified receipt."""
        if self.schema_version != SCHEMA_VERSION:
            return False
        certified = POLICY_HASHES.get(self.policy_version)
        if certified is None or certified != self.policy_hash:
            return False
        if expected_policy_hash is not None and expected_policy_hash != self.policy_hash:
            return False
        if (self.proposed_intent is IntentKind.NONE) != (self.action in _NO_INTENT_ACTIONS):
            return False
        if len(self.observation_ids) != len(self.input_available_at_ns):
            return False
        if tuple(sorted(self.observation_ids)) != self.observation_ids:
            return False
        if len(set(self.observation_ids)) != len(self.observation_ids):
            return False
        if any(available < 0 for available in self.input_available_at_ns):
            return False
        expected_max = max(self.input_available_at_ns) if self.input_available_at_ns else None
        if expected_max != self.max_input_available_at_ns:
            return False
        if expected_max is not None and expected_max > self.decision_time_ns:
            return False
        if self.digest != self._recompute_digest():
            return False
        expected_id = derive_decision_id(
            decision_time_ns=self.decision_time_ns,
            instrument_id=self.instrument_id,
            opportunity_id=self.opportunity_id,
            action=self.action,
            policy_version=self.policy_version,
        )
        if expected_id != self.decision_id:
            return False
        if len(set(self.reason_codes)) != len(self.reason_codes):
            return False
        if tuple(sorted(self.reason_codes)) != self.reason_codes:
            return False
        if tuple(sorted(set(self.constraints))) != self.constraints:
            return False
        return True


class DecisionLedger:
    """Append-only, idempotent store of decision receipts.

    In-memory by design: durability is a storage concern, not an authority to
    mutate. The ledger never rewrites or deletes a record.
    """

    __slots__ = ("_records",)

    def __init__(self) -> None:
        self._records: dict[str, DecisionReceipt] = {}

    def __len__(self) -> int:
        return len(self._records)

    def __contains__(self, decision_id: object) -> bool:
        return isinstance(decision_id, str) and decision_id in self._records

    def get(self, decision_id: str) -> DecisionReceipt | None:
        return self._records.get(decision_id)

    def receipts(self) -> tuple[DecisionReceipt, ...]:
        """Immutable snapshot in append order."""
        return tuple(self._records.values())

    def verify(self, receipt: DecisionReceipt) -> bool:
        """Reject a receipt whose inputs were not PIT at the decision cut."""
        return receipt.verify()

    def orphans(self) -> tuple[str, ...]:
        """Decision ids that are actionable but reference no opportunity."""
        return tuple(
            sorted(
                decision_id
                for decision_id, receipt in self._records.items()
                if is_orphan(receipt.action, receipt.opportunity_id)
            )
        )

    def append(
        self,
        decision: TraderDecision,
        inputs: ReceiptInputs,
        *,
        allow_orphans: bool = False,
    ) -> DecisionReceipt:
        """Record a decision once; identical replays are idempotent.

        Rejects a non-PIT or otherwise unverifiable receipt, an unknown policy
        version, and (unless ``allow_orphans``) an actionable decision with no
        referenced opportunity.
        """
        receipt = DecisionReceipt.create(decision=decision, inputs=inputs)
        if (
            receipt.max_input_available_at_ns is not None
            and receipt.max_input_available_at_ns > receipt.decision_time_ns
        ):
            raise ReceiptRejected(
                "inputs were not available at the decision cut", code="NON_PIT_INPUT"
            )
        if not receipt.verify():
            raise ReceiptRejected("receipt failed verification", code="VERIFY_FAILED")
        if is_orphan(receipt.action, receipt.opportunity_id) and not allow_orphans:
            raise OrphanDecision(
                f"{receipt.action.value} decision {receipt.decision_id} references no opportunity"
            )
        existing = self._records.get(receipt.decision_id)
        if existing is not None:
            if existing.digest != receipt.digest:
                raise ReceiptConflict(
                    f"decision {receipt.decision_id} already recorded with a different body"
                )
            return existing
        self._records[receipt.decision_id] = receipt
        return receipt
