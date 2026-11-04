"""Point-in-time market observations and the availability cut (M1-01).

The epistemic contract of this module is one sentence: *an observation may only
be used by a decision when its ``available_at`` is not after the decision cut.*
Everything else follows from keeping that sentence true without asking callers
to remember it.

Four clocks are deliberately distinct on :class:`Observation`:

``event_time``
    When the phenomenon happened (for ``BAR`` observations this is the bar's
    ``start_ns``). It is *not* when the value became knowable.
``published_at``
    When the exchange published the value, if the feed states it.
``received_at``
    When the local process received the value.
``available_at``
    The earliest time a decision is allowed to consume the value. It is the
    only clock :func:`build_view` compares against the cut.

Causal sanity is enforced at construction: ``received_at >= event_time`` and
``available_at >= received_at >= published_at`` (when published). A record that
claims to be available before it was received is not a record this code is
willing to reason about.

:func:`build_view` partitions a snapshot into usable and rejected observations.
Rejected observations are *kept* (typed as :class:`ObservationRejection`), never
silently dropped, and asking for one explicitly raises :class:`PitViolationError`
instead of returning absence. Two usable records that claim the same
``(instrument_id, kind, event_time)`` with different payloads are contradictory
and raise. A record whose quality is ``AMENDED_FUTURE`` raises unconditionally:
an amendment of a value that was not knowable at the cut must never be resolved
by preference.

Quality is a first-class result, not a filter. A ``MISSING`` or ``STALE``
observation whose ``available_at <= cut`` is part of the view and is reported by
:meth:`ObservationView.quality`; only data-existence matters for bar
construction (a ``MISSING`` bar has no payload to fabricate from, and is
therefore excluded from :meth:`ObservationView.bars` while still degrading the
instrument's quality state).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from types import MappingProxyType
from typing import Annotated, Any, Self

from pydantic import BaseModel, BeforeValidator, ConfigDict, model_validator

from v8_next.domain.market import Candle
from v8_next.telemetry.identity import canonical_digest

__all__ = [
    "Observation",
    "ObservationKind",
    "ObservationRejection",
    "ObservationView",
    "PitViolationError",
    "QualityState",
    "RejectionReason",
    "StrictIntValue",
    "build_view",
    "observation_id",
    "quality_severity",
    "worst_quality",
]


class QualityState(StrEnum):
    """Trustworthiness of an observation at the decision cut."""

    OK = "OK"
    STALE = "STALE"
    MISSING = "MISSING"
    CONTRADICTORY = "CONTRADICTORY"
    AMENDED_FUTURE = "AMENDED_FUTURE"
    UNKNOWN = "UNKNOWN"


class ObservationKind(StrEnum):
    """Market data channel carried by an observation."""

    BAR = "BAR"
    FUNDING = "FUNDING"
    OPEN_INTEREST = "OPEN_INTEREST"
    MARK = "MARK"
    QUOTE = "QUOTE"
    LIQUIDATION = "LIQUIDATION"


class RejectionReason(StrEnum):
    """Why an observation is present in the snapshot but unusable at the cut."""

    NOT_YET_AVAILABLE = "NOT_YET_AVAILABLE"
    QUALITY_BLOCKED = "QUALITY_BLOCKED"


#: Severity ordering used when several quality states meet. Higher is worse and
#: therefore wins: fail-closed aggregation never upgrades a state.
_QUALITY_SEVERITY: Mapping[QualityState, int] = MappingProxyType(
    {
        QualityState.OK: 0,
        QualityState.UNKNOWN: 1,
        QualityState.STALE: 2,
        QualityState.MISSING: 3,
        QualityState.CONTRADICTORY: 4,
        QualityState.AMENDED_FUTURE: 5,
    }
)

#: Quality states whose BAR payload may be turned into a :class:`Candle`.
#: ``MISSING`` has no payload by definition, while ``CONTRADICTORY``/``UNKNOWN``
#: payloads are not trusted enough to become market data.
_BAR_BUILDABLE_QUALITY = frozenset({QualityState.OK, QualityState.STALE})


def quality_severity(state: QualityState) -> int:
    """Return the fail-closed severity rank of ``state`` (higher is worse)."""
    return _QUALITY_SEVERITY[state]


def worst_quality(first: QualityState, *rest: QualityState) -> QualityState:
    """Return the most severe quality state; never upgrades a state."""
    worst = first
    for state in rest:
        if quality_severity(state) > quality_severity(worst):
            worst = state
    return worst


class PitViolationError(ValueError):
    """A point-in-time contract was violated.

    ``code`` is the machine-readable discriminator; consumers branch on it and
    never on message prose.
    """

    code: str = "PIT_VIOLATION"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        resolved = code or type(self).code
        super().__init__(f"{resolved}: {message}")
        self.code = resolved


def _strict_int(value: object) -> int:
    """Accept exact integers (including ``__index__`` types), reject bool/float/str."""
    if isinstance(value, bool):
        raise ValueError("boolean is not an integer")
    if isinstance(value, int):
        return int(value)
    index = getattr(value, "__index__", None)
    if index is None:
        raise ValueError(f"integer required, got {type(value).__name__}")
    return int(index())


#: Timestamp / sequence fields: exact integers, never coerced from text or floats.
StrictIntValue = Annotated[int, BeforeValidator(_strict_int)]


class Observation(BaseModel):
    """One immutable market observation with its full availability lineage."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    instrument_id: str
    kind: ObservationKind
    event_time: StrictIntValue
    available_at: StrictIntValue
    received_at: StrictIntValue
    published_at: StrictIntValue | None = None
    source: str
    sequence: StrictIntValue
    quality: QualityState
    payload: dict[str, Any]

    @model_validator(mode="after")
    def _check_causality(self) -> Self:
        if not self.instrument_id.strip():
            raise ValueError("instrument_id cannot be empty")
        if not self.source.strip():
            raise ValueError("source cannot be empty")
        if self.event_time < 0:
            raise ValueError("event_time must be non-negative")
        if self.sequence < 0:
            raise ValueError("sequence must be non-negative")
        if self.received_at < self.event_time:
            raise ValueError("received_at cannot precede event_time")
        if self.available_at < self.received_at:
            raise ValueError("available_at cannot precede received_at")
        if self.published_at is not None:
            if self.published_at < self.event_time:
                raise ValueError("published_at cannot precede event_time")
            if self.published_at > self.available_at:
                raise ValueError("published_at cannot follow available_at")
        return self


class ObservationRejection(BaseModel):
    """An observation kept in the snapshot but unusable at the cut."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    observation: Observation
    reason: RejectionReason


def _normalise(value: Any) -> Any:
    """Project payload values onto canonical-JSON so identity is reproducible."""
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, Mapping):
        return {str(key): _normalise(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_normalise(item) for item in value]
    return value


def observation_id(observation: Observation) -> str:
    """Deterministic content identity of ``observation`` (provenance included).

    Identical economic content from the same availability lineage yields the
    same id on any machine; a different payload or availability clock yields a
    different id.
    """
    try:
        return canonical_digest(
            "market_observation",
            {
                "instrument_id": observation.instrument_id,
                "kind": observation.kind.value,
                "event_time": observation.event_time,
                "available_at": observation.available_at,
                "received_at": observation.received_at,
                "published_at": observation.published_at,
                "source": observation.source,
                "sequence": observation.sequence,
                "quality": observation.quality.value,
                "payload": _normalise(observation.payload),
            },
        )
    except (TypeError, ValueError) as exc:
        raise PitViolationError(
            f"observation payload is not canonical JSON: {exc}", code="MALFORMED_PAYLOAD"
        ) from exc


def _payload_signature(observation: Observation) -> str:
    """Payload-only identity used to decide whether duplicates contradict."""
    try:
        return canonical_digest("market_observation_payload", _normalise(observation.payload))
    except (TypeError, ValueError) as exc:
        raise PitViolationError(
            f"observation payload is not canonical JSON: {exc}", code="MALFORMED_PAYLOAD"
        ) from exc


def _payload_int(payload: Mapping[str, Any], key: str) -> int:
    if key not in payload:
        raise PitViolationError(f"BAR payload missing {key!r}", code="MALFORMED_BAR")
    try:
        return _strict_int(payload[key])
    except ValueError as exc:
        raise PitViolationError(
            f"BAR payload field {key!r} must be an integer", code="MALFORMED_BAR"
        ) from exc


def _payload_price(payload: Mapping[str, Any], key: str) -> Decimal:
    if key not in payload:
        raise PitViolationError(f"BAR payload missing {key!r}", code="MALFORMED_BAR")
    raw = payload[key]
    if isinstance(raw, bool):
        raise PitViolationError(f"BAR payload field {key!r} must be numeric", code="MALFORMED_BAR")
    try:
        return Decimal(str(raw))
    except (InvalidOperation, ValueError) as exc:
        raise PitViolationError(
            f"BAR payload field {key!r} is not a number", code="MALFORMED_BAR"
        ) from exc


def _candle_from_observation(observation: Observation, identity: str) -> Candle:
    """Materialise the canonical bar payload; malformed data fails closed."""
    payload = observation.payload
    start_ns = _payload_int(payload, "start_ns")
    end_ns = _payload_int(payload, "end_ns")
    if start_ns != observation.event_time:
        raise PitViolationError(
            "BAR event_time must equal payload start_ns "
            f"({observation.event_time} != {start_ns})",
            code="MALFORMED_BAR",
        )
    source_hash = payload.get("source_hash")
    if not isinstance(source_hash, str) or not source_hash:
        source_hash = identity
    try:
        return Candle(
            instrument_id=observation.instrument_id,
            start_ns=start_ns,
            end_ns=end_ns,
            open=_payload_price(payload, "open"),
            high=_payload_price(payload, "high"),
            low=_payload_price(payload, "low"),
            close=_payload_price(payload, "close"),
            volume=_payload_price(payload, "volume"),
            received_ns=observation.received_at,
            available_ns=observation.available_at,
            source_hash=source_hash,
        )
    except ValueError as exc:
        raise PitViolationError(f"BAR payload is not a valid candle: {exc}", code="MALFORMED_BAR") from exc


@dataclass(frozen=True, slots=True)
class ObservationView:
    """Immutable point-in-time snapshot of the market at ``cut_time_ns``.

    Construction goes through :func:`build_view`; the class itself keeps only
    precomputed, immutable indexes so ``bars``/``quality`` are mapping lookups.
    """

    cut_time_ns: int
    max_input_available_at: int
    _observations: Mapping[str, tuple[Observation, ...]] = field(repr=False)
    _bars: Mapping[str, tuple[Candle, ...]] = field(repr=False)
    _bars_by_start: Mapping[str, Mapping[int, Candle]] = field(repr=False)
    _bar_blockers: Mapping[str, Mapping[int, RejectionReason]] = field(repr=False)
    _quality: Mapping[str, QualityState] = field(repr=False)
    _quality_atoms: Mapping[str, tuple[tuple[int, QualityState], ...]] = field(repr=False)
    _observation_ids: tuple[str, ...] = field(repr=False)
    _usable_by_id: Mapping[str, Observation] = field(repr=False)
    _rejected_by_id: Mapping[str, ObservationRejection] = field(repr=False)
    _rejections: tuple[ObservationRejection, ...] = field(repr=False)

    def bars(self, instrument_id: str) -> tuple[Candle, ...]:
        """Completed, trusted bars for ``instrument_id`` known at the cut, ordered by start."""
        return self._bars.get(instrument_id, ())

    def quality(self, instrument_id: str) -> QualityState:
        """Worst quality among usable observations; ``MISSING`` when there are none."""
        return self._quality.get(instrument_id, QualityState.MISSING)

    def quality_at(self, instrument_id: str, at_ns: int) -> QualityState:
        """Worst quality among observations available by ``at_ns``.

        ``at_ns`` later than the cut is clamped to the cut: the view cannot know
        more than it captured. An earlier ``at_ns`` supports historical decisions
        without letting later quality judgements leak backwards.
        """
        at = _strict_int(at_ns)
        atoms = self._quality_atoms.get(instrument_id)
        if not atoms:
            return QualityState.MISSING
        effective = min(at, self.cut_time_ns)
        states = [state for available_at, state in atoms if available_at <= effective]
        if not states:
            return QualityState.MISSING
        return worst_quality(states[0], *states[1:])

    def observation_ids(self) -> tuple[str, ...]:
        """Content ids of the usable observations, in deterministic order."""
        return self._observation_ids

    def observations(self, instrument_id: str | None = None) -> tuple[Observation, ...]:
        """Usable observations (optionally for one instrument), deterministic order."""
        if instrument_id is not None:
            return self._observations.get(instrument_id, ())
        return tuple(
            observation
            for key in sorted(self._observations)
            for observation in self._observations[key]
        )

    def rejections(self) -> tuple[ObservationRejection, ...]:
        """Observations present in the snapshot but not available at the cut."""
        return self._rejections

    def require_observation(self, identity: str) -> Observation:
        """Return a usable observation by id; raise for a rejected or unknown id."""
        usable = self._usable_by_id.get(identity)
        if usable is not None:
            return usable
        rejection = self._rejected_by_id.get(identity)
        if rejection is not None:
            raise PitViolationError(
                f"observation {identity} was not available at cut {self.cut_time_ns} "
                f"(available_at={rejection.observation.available_at})",
                code=rejection.reason.value,
            )
        raise KeyError(identity)

    def require_bar(self, instrument_id: str, *, start_ns: int) -> Candle:
        """Strict bar lookup: absence is an error when the bar exists but is unusable."""
        start = _strict_int(start_ns)
        candle = self._bars_by_start.get(instrument_id, {}).get(start)
        if candle is not None:
            return candle
        reason = self._bar_blockers.get(instrument_id, {}).get(start)
        if reason is not None:
            raise PitViolationError(
                f"bar {instrument_id}@{start} is unusable at cut {self.cut_time_ns}: {reason.value}",
                code=reason.value,
            )
        raise KeyError((instrument_id, start))


def build_view(observations: Iterable[Observation], *, cut_time_ns: int) -> ObservationView:
    """Partition ``observations`` into the usable and rejected sets at ``cut_time_ns``.

    The input is snapshotted (deep-copied) first, so later mutation of caller-owned
    records or their payloads cannot change this view. Future records are rejected
    with :class:`RejectionReason.NOT_YET_AVAILABLE`; requesting one explicitly is a
    :class:`PitViolationError`. Contradictory duplicates and ``AMENDED_FUTURE``
    records raise here because no deterministic, safe resolution exists.
    """
    cut = _strict_int(cut_time_ns)
    snapshot: list[Observation] = []
    for observation in observations:
        if not isinstance(observation, Observation):
            raise TypeError(f"expected Observation, got {type(observation).__name__}")
        if observation.quality is QualityState.AMENDED_FUTURE:
            raise PitViolationError(
                f"AMENDED_FUTURE observation for {observation.instrument_id} "
                f"{observation.kind.value} at {observation.event_time}",
                code=QualityState.AMENDED_FUTURE.value,
            )
        snapshot.append(observation.model_copy(deep=True))

    def sort_key(observation: Observation) -> tuple[object, ...]:
        return (
            observation.instrument_id,
            observation.kind.value,
            observation.event_time,
            observation.sequence,
            observation.source,
            observation.available_at,
            observation.received_at,
            observation.quality.value,
        )

    snapshot.sort(key=sort_key)

    usable: list[Observation] = []
    rejections: list[ObservationRejection] = []
    for observation in snapshot:
        if observation.available_at > cut:
            rejections.append(
                ObservationRejection(
                    observation=observation, reason=RejectionReason.NOT_YET_AVAILABLE
                )
            )
        else:
            usable.append(observation)

    seen: dict[tuple[str, ObservationKind, int], Observation] = {}
    deduped: list[Observation] = []
    for observation in usable:
        key = (observation.instrument_id, observation.kind, observation.event_time)
        previous = seen.get(key)
        if previous is not None:
            if _payload_signature(previous) != _payload_signature(observation):
                raise PitViolationError(
                    f"CONTRADICTORY duplicate for {observation.instrument_id} "
                    f"{observation.kind.value} at {observation.event_time}: "
                    f"{previous.source!r} and {observation.source!r} disagree",
                    code=QualityState.CONTRADICTORY.value,
                )
            continue
        seen[key] = observation
        deduped.append(observation)

    grouped: dict[str, list[Observation]] = {}
    for observation in deduped:
        grouped.setdefault(observation.instrument_id, []).append(observation)

    bars: dict[str, tuple[Candle, ...]] = {}
    bars_by_start: dict[str, Mapping[int, Candle]] = {}
    quality: dict[str, QualityState] = {}
    quality_atoms: dict[str, tuple[tuple[int, QualityState], ...]] = {}
    usable_by_id: dict[str, Observation] = {}
    rejected_by_id: dict[str, ObservationRejection] = {}
    identities: list[str] = []

    for instrument_id in grouped:
        items = grouped[instrument_id]
        instrument_bars: list[Candle] = []
        candle_index: dict[int, Candle] = {}
        for observation in items:
            identity = observation_id(observation)
            usable_by_id[identity] = observation
            identities.append(identity)
            if observation.kind is not ObservationKind.BAR:
                continue
            if observation.quality not in _BAR_BUILDABLE_QUALITY:
                continue
            candle = _candle_from_observation(observation, identity)
            instrument_bars.append(candle)
            candle_index[candle.start_ns] = candle
        instrument_bars.sort(key=lambda candle: (candle.start_ns, candle.end_ns))
        bars[instrument_id] = tuple(instrument_bars)
        bars_by_start[instrument_id] = MappingProxyType(candle_index)
        states = [observation.quality for observation in items]
        quality[instrument_id] = worst_quality(states[0], *states[1:])
        quality_atoms[instrument_id] = tuple(
            (observation.available_at, observation.quality) for observation in items
        )

    blockers: dict[str, Mapping[int, RejectionReason]] = {}
    for rejection in rejections:
        observation = rejection.observation
        identity = observation_id(observation)
        rejected_by_id[identity] = rejection
        if observation.kind is not ObservationKind.BAR:
            continue
        blocked_starts = dict(blockers.get(observation.instrument_id, {}))
        blocked_starts[observation.event_time] = rejection.reason
        blockers[observation.instrument_id] = MappingProxyType(blocked_starts)
    for instrument_id, items in grouped.items():
        for observation in items:
            if observation.kind is not ObservationKind.BAR:
                continue
            if observation.quality in _BAR_BUILDABLE_QUALITY:
                continue
            blocked_starts = dict(blockers.get(instrument_id, {}))
            blocked_starts[observation.event_time] = RejectionReason.QUALITY_BLOCKED
            blockers[instrument_id] = MappingProxyType(blocked_starts)

    max_available = max((observation.available_at for observation in deduped), default=cut)

    return ObservationView(
        cut_time_ns=cut,
        max_input_available_at=max_available,
        _observations=MappingProxyType({key: tuple(value) for key, value in grouped.items()}),
        _bars=MappingProxyType(bars),
        _bars_by_start=MappingProxyType(bars_by_start),
        _bar_blockers=MappingProxyType(blockers),
        _quality=MappingProxyType(quality),
        _quality_atoms=MappingProxyType(quality_atoms),
        _observation_ids=tuple(identities),
        _usable_by_id=MappingProxyType(usable_by_id),
        _rejected_by_id=MappingProxyType(rejected_by_id),
        _rejections=tuple(rejections),
    )
