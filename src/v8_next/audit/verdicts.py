"""Independent audit verdicts for a trade campaign.

A single ``PASS`` is not enough to call a campaign audited. Four dimensions are
reported separately and may only be combined through :func:`derive_verdict`:

* ``mechanical`` — is the trade's price path identifiable from the tape and the
  declared intrabar contract (STOP-FIRST is a model assumption, not an
  observation)? A bar covering both stop and target is a tie and therefore
  ``NOT_IDENTIFIABLE`` even though the path model labels it ``STOP_FIRST``.
* ``economic`` — is the realized outcome present and net of fees, with native
  protective-order evidence? A counterfactual path is never economic proof.
* ``pit`` — did the point-in-time checks run and pass? ``NOT_RUN`` by default so
  an unaudited campaign cannot become complete by omission.
* ``completeness`` — is every required source identity present so the claim can
  be traced back to telemetry? Missing source IDs can never yield
  ``AUDIT_COMPLETE``; constructing such a verdict raises :class:`AuditVerdictError`.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from enum import StrEnum
from typing import Any, Self

from pydantic import BaseModel, ConfigDict

#: Label emitted only when the completeness dimension is ``PASS``.
AUDIT_COMPLETE = "AUDIT_COMPLETE"

#: Identities every auditable campaign must be able to name.
REQUIRED_SOURCE_IDS: tuple[str, ...] = (
    "position_id",
    "campaign_id",
    "opportunity_id",
    "decision_id",
)

_UNRESOLVED = frozenset({"NOT_RUN", "BLOCKED", "INCOMPLETE", "INVALID"})


class VerdictState(StrEnum):
    """Outcome vocabulary shared by all four dimensions."""

    PASS = "PASS"
    FAIL = "FAIL"
    NOT_IDENTIFIABLE = "NOT_IDENTIFIABLE"
    INVALID = "INVALID"
    INCOMPLETE = "INCOMPLETE"
    NOT_RUN = "NOT_RUN"
    BLOCKED = "BLOCKED"


class AuditVerdictError(ValueError):
    """Typed refusal to publish an unearned verdict.

    ``code`` is the machine-readable discriminator; consumers branch on it
    rather than on message prose.
    """

    code: str = "AUDIT_VERDICT_ERROR"

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _state(value: VerdictState | str) -> VerdictState:
    return value if isinstance(value, VerdictState) else VerdictState(str(value))


class AuditVerdict(BaseModel):
    """Frozen four-dimension verdict with the evidence that earned it.

    Construction refuses a ``completeness`` of ``PASS`` that the other fields do
    not support, raising :class:`AuditVerdictError` (pydantic validators would
    wrap it in a generic ``ValidationError``).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    mechanical: VerdictState = VerdictState.NOT_RUN
    economic: VerdictState = VerdictState.NOT_RUN
    pit: VerdictState = VerdictState.NOT_RUN
    completeness: VerdictState = VerdictState.NOT_RUN
    reasons: tuple[str, ...] = ()
    labels: tuple[str, ...] = ()
    #: Sorted ``(name, value)`` pairs of the identities the verdict was derived from.
    source_ids: tuple[tuple[str, str], ...] = ()

    def __init__(self, **data: Any) -> None:
        super().__init__(**data)
        _assert_completeness_earned(self)

    @classmethod
    def model_validate(cls, obj: Any, **kwargs: Any) -> Self:
        # Route mappings through the constructor so the typed refusal also
        # guards ``model_validate``; the core validator would wrap it.
        if isinstance(obj, Mapping):
            return cls(**dict(obj))
        return super().model_validate(obj, **kwargs)

    @property
    def source_id_map(self) -> dict[str, str]:
        return dict(self.source_ids)

    @property
    def audit_complete(self) -> bool:
        """True only when the evidence itself is complete (not that the trade won)."""
        return self.completeness is VerdictState.PASS


def _assert_completeness_earned(verdict: AuditVerdict) -> None:
    if verdict.completeness is not VerdictState.PASS:
        return
    present = {name for name, _ in verdict.source_ids}
    missing = [name for name in REQUIRED_SOURCE_IDS if name not in present]
    if missing:
        raise AuditVerdictError(
            "COMPLETENESS_WITHOUT_SOURCE_IDS",
            f"completeness PASS requires source ids; missing={missing}",
        )
    unresolved = [
        name
        for name, state in (
            ("mechanical", verdict.mechanical),
            ("economic", verdict.economic),
            ("pit", verdict.pit),
        )
        if state.value in _UNRESOLVED
    ]
    if unresolved:
        raise AuditVerdictError(
            "COMPLETENESS_WITH_UNRESOLVED_DIMENSION",
            f"completeness PASS with unresolved dimensions: {unresolved}",
        )
    if any(reason.startswith("MISSING_SOURCE_ID") for reason in verdict.reasons):
        raise AuditVerdictError(
            "COMPLETENESS_WITH_MISSING_SOURCE_REASON",
            "completeness PASS contradicts a MISSING_SOURCE_ID reason",
        )


def audit_label(verdict: AuditVerdict) -> str:
    """``AUDIT_COMPLETE`` only for a complete verdict; otherwise the blocking state."""
    if verdict.audit_complete:
        return AUDIT_COMPLETE
    return f"AUDIT_{verdict.completeness.value}"


def require_audit_complete(verdict: AuditVerdict, *, context: str = "audit") -> AuditVerdict:
    """Return the verdict or raise :class:`AuditVerdictError` (never silently downgrade)."""
    if not verdict.audit_complete:
        raise AuditVerdictError(
            "AUDIT_NOT_COMPLETE",
            f"{context}: completeness={verdict.completeness.value} "
            f"mechanical={verdict.mechanical.value} economic={verdict.economic.value} "
            f"pit={verdict.pit.value} reasons={list(verdict.reasons)}",
        )
    return verdict


def derive_verdict(
    *,
    mechanical: VerdictState | str,
    economic: VerdictState | str,
    pit: VerdictState | str,
    source_ids: Mapping[str, str | None] | None = None,
    labels: Sequence[str] = (),
    reasons: Sequence[str] = (),
) -> AuditVerdict:
    """Combine the dimensions into a verdict whose completeness is derived, never asserted.

    Completeness precedence (most severe first): ``BLOCKED``, ``NOT_RUN``,
    missing required source IDs (``INCOMPLETE``), ``INCOMPLETE``, ``INVALID``,
    else ``PASS``. ``NOT_IDENTIFIABLE`` and ``FAIL`` describe an audit that ran,
    so they do not by themselves make the evidence incomplete.
    """
    states = {
        "mechanical": _state(mechanical),
        "economic": _state(economic),
        "pit": _state(pit),
    }
    provided: dict[str, str] = {}
    for key, value in (source_ids or {}).items():
        if value is None:
            continue
        text = str(value).strip()
        if text:
            provided[str(key)] = text
    missing = [key for key in REQUIRED_SOURCE_IDS if key not in provided]
    reason_list = list(reasons)
    reason_list.extend(f"MISSING_SOURCE_ID:{key}" for key in missing)

    values = set(states.values())
    if VerdictState.BLOCKED in values:
        completeness = VerdictState.BLOCKED
    elif VerdictState.NOT_RUN in values:
        completeness = VerdictState.NOT_RUN
    elif missing:
        completeness = VerdictState.INCOMPLETE
    elif VerdictState.INVALID in values:
        completeness = VerdictState.INVALID
    elif VerdictState.INCOMPLETE in values:
        completeness = VerdictState.INCOMPLETE
    else:
        completeness = VerdictState.PASS

    return AuditVerdict(
        mechanical=states["mechanical"],
        economic=states["economic"],
        pit=states["pit"],
        completeness=completeness,
        reasons=tuple(reason_list),
        labels=tuple(labels),
        source_ids=tuple(sorted(provided.items())),
    )


def source_ids_from_metrics(metrics: Mapping[str, Any]) -> dict[str, str | None]:
    """Extract the required campaign identities from one trade-metrics row."""
    found: dict[str, str | None] = {}
    for key in REQUIRED_SOURCE_IDS:
        value = metrics.get(key)
        if value is None:
            found[key] = None
            continue
        text = str(value).strip()
        found[key] = text or None
    return found


def _mechanical_state(metrics: Mapping[str, Any]) -> tuple[VerdictState, list[str], list[str]]:
    reason = metrics.get("metric_unavailable_reason")
    if (
        reason == "MISSING_ENTRY_OR_QUANTITY"
        or metrics.get("entry_price") is None
        or metrics.get("quantity") is None
        or metrics.get("opened_ns") is None
    ):
        return (
            VerdictState.INVALID,
            ["MECHANICAL_INPUT_MISSING_ENTRY_QUANTITY_OR_TIME"],
            [],
        )
    if reason == "NO_BARS_IN_POSITION_WINDOW":
        return (
            VerdictState.NOT_IDENTIFIABLE,
            ["MECHANICAL_NO_BARS_IN_POSITION_WINDOW"],
            ["NO_BARS_IN_POSITION_WINDOW"],
        )
    if metrics.get("risk_invalid_reason") == "ZERO_STOP_DISTANCE":
        return (
            VerdictState.INVALID,
            ["MECHANICAL_INVALID_RISK_DENOMINATOR:ZERO_STOP_DISTANCE"],
            [],
        )
    if metrics.get("path_model_outcome") == "ENTRY_BAR_ONLY" or reason == "NO_POST_ENTRY_BAR":
        return (
            VerdictState.NOT_IDENTIFIABLE,
            ["MECHANICAL_NO_POST_ENTRY_BAR"],
            ["ENTRY_BAR_ONLY"],
        )
    if metrics.get("same_bar_stop_and_target") is True:
        # The declared STOP-FIRST contract labels the bar but the tape does not
        # identify which level filled first.
        return (
            VerdictState.NOT_IDENTIFIABLE,
            ["MECHANICAL_SAME_BAR_STOP_AND_TARGET_UNRESOLVED"],
            ["STOP_FIRST"],
        )
    if metrics.get("path_model_applied") == "NONE_NO_STOP":
        return (
            VerdictState.NOT_IDENTIFIABLE,
            ["MECHANICAL_NO_PROTECTIVE_STOP_PATH"],
            ["NONE_NO_STOP"],
        )
    if metrics.get("mfe_price") is None or metrics.get("mae_price") is None:
        return (
            VerdictState.NOT_IDENTIFIABLE,
            ["MECHANICAL_EXCURSION_UNMEASURED"],
            [],
        )
    return VerdictState.PASS, [], []


def _economic_state(metrics: Mapping[str, Any]) -> tuple[VerdictState, list[str], list[str]]:
    if (
        metrics.get("gross_pnl_usdt") is None
        or metrics.get("realized_net_pnl_usdt") is None
    ):
        return (
            VerdictState.INCOMPLETE,
            ["ECONOMIC_OUTCOME_NOT_REALIZED"],
            ["UNREALIZED_AT_CUTOFF"],
        )
    if metrics.get("protection_evidence") != "NATIVE_ORDER_PRESENT":
        return (
            VerdictState.INCOMPLETE,
            ["ECONOMIC_PROTECTIVE_EVIDENCE_MISSING"],
            [str(metrics.get("protection_evidence") or "NO_NATIVE_PROTECTIVE_ORDER")],
        )
    if metrics.get("risk_denominator_status") == "INVALID":
        return (
            VerdictState.INVALID,
            ["ECONOMIC_INVALID_RISK_DENOMINATOR"],
            [],
        )
    return VerdictState.PASS, [], ["REALIZED_PNL_NATIVE"]


def verdict_from_trade_metrics(
    metrics: Mapping[str, Any],
    *,
    source_ids: Mapping[str, str | None] | None = None,
    pit: VerdictState | str = VerdictState.NOT_RUN,
    reasons: Sequence[str] = (),
) -> AuditVerdict:
    """Derive the four dimensions from one :func:`compute_trade_metrics` row.

    ``pit`` defaults to ``NOT_RUN``: the lineage audit is a separate, explicit
    check and must not be assumed by the metrics reader.
    """
    mechanical, mechanical_reasons, mechanical_labels = _mechanical_state(metrics)
    economic, economic_reasons, economic_labels = _economic_state(metrics)
    return derive_verdict(
        mechanical=mechanical,
        economic=economic,
        pit=pit,
        source_ids=(
            source_ids_from_metrics(metrics) if source_ids is None else source_ids
        ),
        labels=tuple(mechanical_labels + economic_labels),
        reasons=tuple(mechanical_reasons + economic_reasons + list(reasons)),
    )
