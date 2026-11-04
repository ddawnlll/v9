"""Finite, Expert-independent opportunity grammar — port of v8-core/src/oracle/opportunity.rs.

(TARGET_ORACLE_SPEC §5: the Opportunity Universe is Expert-independent, finite, versioned,
and preregistered per evaluation family.)

Sequence-dependent predicate forms (cross/rising/falling/persist/sequence) are accepted only
as registered syntax: the narrow adapter carries no observation history, so evaluating one
returns False (fail closed) rather than constructing a backdated signal.

DIVERGENCES from the Rust module: identity digests use hashlib (sha256) with the same
domain tags; reproducible inside this port, not bit-equal to the Rust Canon digests.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Mapping

from v8_next.oracle.artifacts import OpportunityUniverseVersion
from v8_next.oracle.authority import OracleRefused
from v8_next.oracle.information import InformationSet
from v8_next.oracle.taxonomy import OracleRefusal

__all__ = [
    "CandidateTemplate",
    "Direction",
    "GrammarCandidate",
    "OpportunityGrammar",
    "ParameterGrid",
    "PredicateNode",
    "PrimitiveDefinition",
    "PrimitiveFamily",
    "PrimitiveRegistry",
    "TemplateRegistry",
    "ValueRef",
]

#: Maximum predicate nesting depth (mirrors the Rust bound).
MAX_PREDICATE_DEPTH = 16


def _digest(domain: str, payload: Any) -> str:
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False, default=str)
    return hashlib.sha256(f"{domain}|{blob}".encode()).hexdigest()


class PrimitiveFamily(StrEnum):
    PRICE_RETURN = "PRICE_RETURN"
    VOLATILITY_RANGE = "VOLATILITY_RANGE"
    VOLUME_ACTIVITY = "VOLUME_ACTIVITY"
    LIQUIDITY = "LIQUIDITY"
    ORDER_FLOW = "ORDER_FLOW"
    FUNDING_BASIS = "FUNDING_BASIS"
    DERIVATIVES_STRESS = "DERIVATIVES_STRESS"


class Direction(StrEnum):
    LONG = "LONG"
    SHORT = "SHORT"


@dataclass(frozen=True)
class PrimitiveDefinition:
    family: PrimitiveFamily
    source_version: str


@dataclass(frozen=True)
class PrimitiveRegistry:
    version: str
    primitives: Mapping[str, PrimitiveDefinition]
    allowed_operators: frozenset[str]

    def identity(self) -> str:
        return _digest(
            "primitive-registry-v1",
            {
                "version": self.version,
                "primitives": {
                    name: {"family": d.family.value, "source_version": d.source_version}
                    for name, d in sorted(self.primitives.items())
                },
                "allowed_operators": sorted(self.allowed_operators),
            },
        )


@dataclass(frozen=True)
class ParameterGrid:
    grid_id: str
    values: Mapping[str, tuple[Any, ...]]

    def identity(self) -> str:
        return _digest(
            "parameter-grid-v1",
            {"grid_id": self.grid_id, "values": {k: list(v) for k, v in sorted(self.values.items())}},
        )

    def assignments(self, names: tuple[str, ...]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = [{}]
        for name in names:
            values = self.values.get(name)
            if not values:
                raise OracleRefused(
                    OracleRefusal.INSUFFICIENT_SUPPORT,
                    f"parameter {name!r} has no grid values",
                )
            out = [{**partial, name: value} for partial in out for value in values]
        return out


class ValueRef:
    """A literal threshold or a named grid parameter."""

    __slots__ = ("kind", "value")

    def __init__(self, kind: str, value: Any) -> None:
        self.kind = kind
        self.value = value

    @classmethod
    def literal(cls, value: float) -> ValueRef:
        return cls("literal", float(value))

    @classmethod
    def parameter(cls, name: str) -> ValueRef:
        return cls("parameter", name)

    def resolve(self, parameters: Mapping[str, Any]) -> float | None:
        if self.kind == "literal":
            result = float(self.value)
            return result if result == result else None
        raw = parameters.get(self.value)
        if isinstance(raw, bool):
            return None
        if isinstance(raw, (int, float)):
            result = float(raw)
            return result if result == result else None
        return None

    def __eq__(self, other: object) -> bool:
        return isinstance(other, ValueRef) and (self.kind, self.value) == (other.kind, other.value)

    def __hash__(self) -> int:
        return hash((self.kind, self.value))

    def __repr__(self) -> str:
        return f"ValueRef({self.kind}={self.value!r})"


class PredicateNode:
    """One node of the bounded predicate IR (operator tag + payload)."""

    __slots__ = ("operator", "payload")

    def __init__(self, operator: str, payload: Any) -> None:
        self.operator = operator
        self.payload = payload

    # -- constructors ---------------------------------------------------------
    @classmethod
    def above(cls, feature: str, threshold: ValueRef) -> PredicateNode:
        return cls("ABOVE", (feature, threshold))

    @classmethod
    def below(cls, feature: str, threshold: ValueRef) -> PredicateNode:
        return cls("BELOW", (feature, threshold))

    @classmethod
    def cross_above(cls, feature: str, threshold: ValueRef) -> PredicateNode:
        return cls("CROSS_ABOVE", (feature, threshold))

    @classmethod
    def cross_below(cls, feature: str, threshold: ValueRef) -> PredicateNode:
        return cls("CROSS_BELOW", (feature, threshold))

    @classmethod
    def in_range(cls, feature: str, lo: ValueRef, hi: ValueRef) -> PredicateNode:
        return cls("IN_RANGE", (feature, lo, hi))

    @classmethod
    def rising(cls, feature: str, n: int) -> PredicateNode:
        return cls("RISING", (feature, n))

    @classmethod
    def falling(cls, feature: str, n: int) -> PredicateNode:
        return cls("FALLING", (feature, n))

    @classmethod
    def persist(cls, predicate: PredicateNode, n: int) -> PredicateNode:
        return cls("PERSIST", (predicate, n))

    @classmethod
    def and_(cls, nodes: tuple[PredicateNode, ...]) -> PredicateNode:
        return cls("AND", nodes)

    @classmethod
    def or_(cls, nodes: tuple[PredicateNode, ...]) -> PredicateNode:
        return cls("OR", nodes)

    @classmethod
    def not_(cls, node: PredicateNode) -> PredicateNode:
        return cls("NOT", node)

    @classmethod
    def sequence(cls, first: PredicateNode, second: PredicateNode, max_delay: int) -> PredicateNode:
        return cls("SEQUENCE", (first, second, max_delay))

    def __eq__(self, other: object) -> bool:
        return (
            isinstance(other, PredicateNode)
            and self.operator == other.operator
            and self.payload == other.payload
        )

    def __repr__(self) -> str:
        return f"PredicateNode({self.operator}, {self.payload!r})"

    # -- validation -----------------------------------------------------------
    def validate(
        self,
        registry: PrimitiveRegistry,
        parameters: frozenset[str],
        depth: int = 0,
    ) -> None:
        if depth > MAX_PREDICATE_DEPTH or self.operator not in registry.allowed_operators:
            raise OracleRefused(
                OracleRefusal.INSUFFICIENT_SUPPORT,
                f"predicate operator {self.operator!r} is not registered",
            )

        def need_feature(name: str) -> None:
            if name not in registry.primitives:
                raise OracleRefused(
                    OracleRefusal.MISSING_DECISION_TIME_DATA,
                    f"feature {name!r} is not a registered primitive",
                )

        def need_value(ref: ValueRef) -> None:
            if ref.kind == "literal":
                if not isinstance(ref.value, float) or ref.value != ref.value:
                    raise OracleRefused(OracleRefusal.INSUFFICIENT_SUPPORT, "non-finite literal")
            elif not (ref.kind == "parameter" and ref.value in parameters):
                raise OracleRefused(
                    OracleRefusal.INSUFFICIENT_SUPPORT,
                    f"unbound parameter {ref.value!r}",
                )

        op, p = self.operator, self.payload
        if op in ("ABOVE", "BELOW", "CROSS_ABOVE", "CROSS_BELOW"):
            need_feature(p[0])
            need_value(p[1])
        elif op == "IN_RANGE":
            need_feature(p[0])
            need_value(p[1])
            need_value(p[2])
        elif op in ("RISING", "FALLING"):
            need_feature(p[0])
            if not (isinstance(p[1], int) and p[1] > 0):
                raise OracleRefused(OracleRefusal.INSUFFICIENT_SUPPORT, f"{op} needs n > 0")
        elif op == "PERSIST":
            if not (isinstance(p[1], int) and p[1] > 0):
                raise OracleRefused(OracleRefusal.INSUFFICIENT_SUPPORT, "PERSIST needs n > 0")
            p[0].validate(registry, parameters, depth + 1)
        elif op in ("AND", "OR"):
            if not p:
                raise OracleRefused(OracleRefusal.INSUFFICIENT_SUPPORT, f"{op} needs children")
            for node in p:
                node.validate(registry, parameters, depth + 1)
        elif op == "NOT":
            p.validate(registry, parameters, depth + 1)
        elif op == "SEQUENCE":
            if not (isinstance(p[2], int) and p[2] > 0):
                raise OracleRefused(OracleRefusal.INSUFFICIENT_SUPPORT, "SEQUENCE needs delay > 0")
            p[0].validate(registry, parameters, depth + 1)
            p[1].validate(registry, parameters, depth + 1)

    # -- evaluation (fail closed: no history forms evaluate true) --------------
    def evaluate(self, information: InformationSet, parameters: Mapping[str, Any]) -> bool:
        op, p = self.operator, self.payload
        if op == "ABOVE":
            got = information.value_f64(p[0])
            want = p[1].resolve(parameters)
            return got is not None and want is not None and got > want
        if op == "BELOW":
            got = information.value_f64(p[0])
            want = p[1].resolve(parameters)
            return got is not None and want is not None and got < want
        if op == "IN_RANGE":
            got = information.value_f64(p[0])
            lo = p[1].resolve(parameters)
            hi = p[2].resolve(parameters)
            return (
                got is not None
                and lo is not None
                and hi is not None
                and lo <= got <= hi
            )
        if op == "AND":
            return all(node.evaluate(information, parameters) for node in p)
        if op == "OR":
            return any(node.evaluate(information, parameters) for node in p)
        if op == "NOT":
            return not p.evaluate(information, parameters)
        # CROSS_*, RISING, FALLING, PERSIST, SEQUENCE: no observation history in the
        # narrow adapter — False fails closed rather than constructing a backdated signal.
        return False


@dataclass(frozen=True)
class CandidateTemplate:
    template_id: str
    mechanism_family_id: str
    behavior_family_id: str
    habitat_predicate: PredicateNode
    setup_predicate: PredicateNode
    trigger_predicate: PredicateNode
    direction: Direction
    invalidation: str
    expiry: str
    risk_geometry: Mapping[str, Any]
    parameter_names: tuple[str, ...]


@dataclass(frozen=True)
class TemplateRegistry:
    version: str
    templates: tuple[CandidateTemplate, ...]

    def identity(self) -> str:
        return _digest(
            "template-registry-v1",
            {
                "version": self.version,
                "templates": [
                    {
                        "template_id": t.template_id,
                        "mechanism_family_id": t.mechanism_family_id,
                        "behavior_family_id": t.behavior_family_id,
                        "direction": t.direction.value,
                        "invalidation": t.invalidation,
                        "expiry": t.expiry,
                        "parameter_names": list(t.parameter_names),
                    }
                    for t in self.templates
                ],
            },
        )


@dataclass
class GrammarCandidate:
    grammar_candidate_id: str
    universe_id: str
    template_id: str
    instrument: str
    timeframe: str
    direction: Direction
    decision_time: int
    parameters: dict[str, Any]

    def identity(self) -> str:
        return _digest(
            "grammar-candidate-v1",
            {
                "universe_id": self.universe_id,
                "template_id": self.template_id,
                "instrument": self.instrument,
                "timeframe": self.timeframe,
                "direction": self.direction.value,
                "decision_time": self.decision_time,
                "parameters": self.parameters,
            },
        )

    def bind_identity(self) -> None:
        self.grammar_candidate_id = self.identity()


@dataclass(frozen=True)
class OpportunityGrammar:
    version: str
    primitives: PrimitiveRegistry
    templates: TemplateRegistry
    grid: ParameterGrid

    def validate(self) -> None:
        if (
            not self.version
            or not self.primitives.version
            or not self.templates.version
            or not self.grid.grid_id
            or not self.templates.templates
        ):
            raise OracleRefused(OracleRefusal.INSUFFICIENT_SUPPORT, "grammar is incomplete")
        seen: set[str] = set()
        for template in self.templates.templates:
            if (
                not template.template_id
                or not template.mechanism_family_id
                or not template.behavior_family_id
                or not template.invalidation
                or not template.expiry
                or template.template_id in seen
            ):
                raise OracleRefused(
                    OracleRefusal.INSUFFICIENT_SUPPORT,
                    f"template {template.template_id!r} is incomplete or duplicated",
                )
            seen.add(template.template_id)
            names = frozenset(template.parameter_names)
            if len(names) != len(template.parameter_names):
                raise OracleRefused(
                    OracleRefusal.INSUFFICIENT_SUPPORT,
                    f"template {template.template_id!r} repeats a parameter name",
                )
            template.habitat_predicate.validate(self.primitives, names)
            template.setup_predicate.validate(self.primitives, names)
            template.trigger_predicate.validate(self.primitives, names)
            self.grid.assignments(template.parameter_names)

    def search_universe_size(self, instruments: int, timeframes: int) -> int:
        self.validate()
        count = 0
        for template in self.templates.templates:
            count += len(self.grid.assignments(template.parameter_names))
        size = count * instruments * timeframes
        if size <= 0:
            raise OracleRefused(OracleRefusal.INSUFFICIENT_SUPPORT, "empty search universe")
        return size

    def generate(
        self,
        universe: OpportunityUniverseVersion,
        information: InformationSet,
    ) -> list[GrammarCandidate]:
        self.validate()
        if (
            universe.primitive_registry_hash != self.primitives.identity()
            or universe.behavior_template_registry_hash != self.templates.identity()
            or universe.parameter_grid_hash != self.grid.identity()
            or universe.predicate_ir_version != self.version
            or universe.search_universe_size
            != self.search_universe_size(
                len(universe.instrument_universe), len(universe.timeframe_set)
            )
        ):
            raise OracleRefused(
                OracleRefusal.INSUFFICIENT_SUPPORT,
                "universe hashes do not match the registered grammar",
            )
        if information.decision_time < 0:
            raise OracleRefused(
                OracleRefusal.MISSING_DECISION_TIME_DATA, "negative decision time"
            )
        candidates: list[GrammarCandidate] = []
        for template in self.templates.templates:
            for parameters in self.grid.assignments(template.parameter_names):
                if not (
                    template.habitat_predicate.evaluate(information, parameters)
                    and template.setup_predicate.evaluate(information, parameters)
                    and template.trigger_predicate.evaluate(information, parameters)
                ):
                    continue
                for instrument in universe.instrument_universe:
                    for timeframe in universe.timeframe_set:
                        candidate = GrammarCandidate(
                            grammar_candidate_id="",
                            universe_id=universe.universe_id,
                            template_id=template.template_id,
                            instrument=instrument,
                            timeframe=timeframe,
                            direction=template.direction,
                            decision_time=information.decision_time,
                            parameters=dict(parameters),
                        )
                        candidate.bind_identity()
                        candidates.append(candidate)
        candidates.sort(key=lambda c: c.grammar_candidate_id)
        return candidates

    @staticmethod
    def population_hash(candidates: list[GrammarCandidate]) -> str:
        ids = sorted(c.grammar_candidate_id for c in candidates)
        return _digest("grammar-population-v1", {"ids": ids, "count": len(ids)})
