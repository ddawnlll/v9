"""Availability-gated information set — port of v8-core/src/oracle/information.rs.

(TARGET_ORACLE_SPEC §4, rule 4: future information is evaluator-only.)

An ``InformationSet`` is the decision-time information boundary: a field enters only when
both its knowledge time and its availability time are at or before the decision time. A
field from the future is refused with ``MISSING_DECISION_TIME_DATA`` — it is never stored,
never readable, and therefore can never leak into a decision-time feature.

DIVERGENCES from the Rust module:
- ``InformationAdapter.feature`` adapts the local ``Feature`` protocol below instead of
  ``crate::state::Feature`` (v8-next carries no state feature store; the four clock fields
  are identical).
- Identity digests use hashlib (sha256) with the same domain tags; they are reproducible
  inside this port but not bit-equal to the Rust blake3/sha1 Canon digests.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from v8_next.oracle.authority import OracleRefused
from v8_next.oracle.taxonomy import OracleRefusal

__all__ = [
    "Feature",
    "InformationAdapter",
    "InformationField",
    "InformationSet",
]


@dataclass(frozen=True)
class InformationField:
    """One named value with its three clocks (event / knowledge / availability)."""

    name: str
    value: Any
    event_time: int
    knowledge_time: int
    availability_time: int
    source_id: str
    source_version: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "value": self.value,
            "event_time": self.event_time,
            "knowledge_time": self.knowledge_time,
            "availability_time": self.availability_time,
            "source_id": self.source_id,
            "source_version": self.source_version,
        }


@dataclass(frozen=True)
class Feature:
    """Minimal computed-state feature surface (mirrors the ``state::Feature`` fields used).

    ``max_input_available_time`` is the PIT availability boundary of the inputs the value
    was computed from; a value computed from future inputs must not be adapted.
    """

    name: str
    value: Any
    max_input_available_time: int
    feature_version: str


class InformationSet:
    """The decision-time information boundary at one ``decision_time``."""

    def __init__(self, decision_time: int) -> None:
        self.decision_time = decision_time
        self._fields: dict[str, InformationField] = {}

    def insert(self, field_: InformationField) -> None:
        """Store a field, refusing future knowledge/availability (fail closed)."""
        if field_.knowledge_time > self.decision_time or field_.availability_time > self.decision_time:
            raise OracleRefused(
                OracleRefusal.MISSING_DECISION_TIME_DATA,
                f"field {field_.name!r} is not available at decision_time {self.decision_time}",
            )
        self._fields[field_.name] = field_

    def get(self, name: str) -> InformationField | None:
        return self._fields.get(name)

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._fields))

    def value_f64(self, name: str) -> float | None:
        field_ = self.get(name)
        if field_ is None:
            return None
        value = field_.value
        if isinstance(value, bool):
            return None
        if isinstance(value, (int, float)):
            result = float(value)
            return result if result == result else None  # NaN never enters
        return None

    def snapshot(self) -> Mapping[str, InformationField]:
        return dict(self._fields)


class InformationAdapter:
    """Narrow adapter from an already-computed feature to a boundary field."""

    @staticmethod
    def feature(
        feature: Feature,
        event_time: int,
        decision_time: int,
        source_id: str,
    ) -> InformationField:
        if feature.max_input_available_time > decision_time or feature.value is None:
            raise OracleRefused(
                OracleRefusal.MISSING_DECISION_TIME_DATA,
                f"feature {feature.name!r} is not available at decision_time {decision_time}",
            )
        return InformationField(
            name=feature.name,
            value=feature.value,
            event_time=event_time,
            knowledge_time=feature.max_input_available_time,
            availability_time=feature.max_input_available_time,
            source_id=source_id,
            source_version=feature.feature_version,
        )
