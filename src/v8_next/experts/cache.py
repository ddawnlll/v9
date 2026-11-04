"""Bounded, versioned, incremental feature cache shared by all 28 experts (#15).

Why this exists
---------------
Each bar, the 28 experts ask the same small set of indicators for the *same*
causal prefix: Wilder RSI, the EMA pair, the significant swings, the MACD line,
the mean range. Before the append-only engine each expert recomputed them over
rows ``[0, n)`` -- 28 full-prefix computations per bar, ``O(n^2)`` per replay.
:class:`FeatureCache` is the *bounded, keyed* layer on top of that engine:

* **Keying.** A value is stored under
  ``(version, instrument, feature spec, as-of bar close, input digest)``.
  The input digest identifies the exact prefix content the value was derived
  from, so a cached value can never be served for different inputs.
* **Incremental extension.** When the frame's numeric cache is the one already
  retained (the replay path built by ``extend_causal_frame``), appending one
  bar extends the digest with the newcomer rows only and lets
  :class:`~v8_next.experts.engine.FeatureEngine` roll its state forward: no
  history is re-read and no indicator is recomputed.
* **Explicit invalidation.** If the prefix can no longer be proven append-only
  (a foreign numeric cache, a shorter prefix, a rebuilt frame without a
  retained cache), every key for that instrument is dropped *by name*
  (``invalidations`` counts them) instead of serving a doubted value.
* **Bounded memory.** ``max_entries`` is a hard LRU bound; ``max_bytes``
  optionally bounds the estimated value bytes too. Evictions are counted.

Fail-closed
-----------
Nothing here invents a value. A feature function that raises (a source gap, a
non-finite price, a warmup shortfall) propagates its exception and caches
nothing. A cache miss recomputes; an unprovable prefix invalidates. The engine
the cache owns keeps the *same* fallbacks it always had, so ``observe_all_28``
through this cache returns the stances the unbundled path returns -- proven by
``tests/test_expert_feature_cache.py`` on the real tape.

Threading
---------
One cache belongs to one instrument feed and is single-threaded by
construction, exactly like the ``FeatureEngine`` it owns.
"""

from __future__ import annotations

import hashlib
from collections import OrderedDict
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Final, Self

import polars as pl
from pydantic import BaseModel, ConfigDict, Field, model_validator

from v8_next.domain.market import CausalFrame
from v8_next.domain.positioning import PositioningReading
from v8_next.economics.decisions import Opportunity, Stance
from v8_next.experts.engine import FeatureEngine
from v8_next.experts.features import (
    _ACTIVE_LAZY,
    _LazyBundle,
    close_series,
    macd_line,
    mean_range,
    significant_swings,
    trend_emas,
    wilder_rsi,
)
from v8_next.experts.registry import observe_all_28

#: Bumped whenever a cached value's *meaning* changes (a kernel, a warmup
#: convention, a spec). The version is part of every key, so a bump makes old
#: entries unreachable instead of silently reinterpreted.
FEATURE_CACHE_VERSION: Final = "v9.feature-cache.1"

#: Columns whose bytes identify a prefix. ``open`` is included although no
#: wired indicator reads it: the digest names the input bar, not the consumers.
_DIGEST_COLUMNS: Final[tuple[str, ...]] = (
    "start_ns",
    "end_ns",
    "open",
    "high",
    "low",
    "close",
    "volume",
)

#: Default LRU bound. Values are indicator tuples (an RSI tuple is ``8 * n``
#: bytes), so the bound is deliberately small: a replay wants the current bar
#: plus the handful of bars a multi-horizon observer asks for, never history.
DEFAULT_MAX_ENTRIES: Final = 512


class FeatureSpec(BaseModel):
    """One indicator request: a named kernel plus its integer parameters.

    Construction is validated once (module-level constants below), never on the
    per-expert hot path; the cache keys on :meth:`key` instead.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    params: tuple[int, ...] = ()

    @model_validator(mode="after")
    def _known_spec(self) -> Self:
        computer = _COMPUTERS.get(self.name)
        if computer is None:
            raise ValueError(f"unknown feature {self.name!r}")
        if any(param < 0 for param in self.params):
            raise ValueError("feature parameters must be non-negative")
        arity = _PARAMS_ARITY[self.name]
        if len(self.params) != arity:
            raise ValueError(
                f"feature {self.name!r} takes {arity} parameter(s), got {len(self.params)}"
            )
        return self

    def key(self) -> str:
        """Stable key fragment for this spec (no pydantic object in the hot path)."""
        if not self.params:
            return self.name
        return f"{self.name}:{','.join(str(param) for param in self.params)}"


@dataclass(frozen=True, slots=True)
class CacheKey:
    """Identity of one cached value: version, instrument, spec, bar, inputs."""

    version: str
    instrument_id: str
    spec_key: str
    as_of_ns: int
    input_digest: str


class CacheStats(BaseModel):
    """Machine-readable cache state; counters are what the parity tests assert."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: str
    entries: int = Field(ge=0)
    max_entries: int = Field(ge=1)
    estimated_bytes: int = Field(ge=0)
    computations: int = Field(ge=0)
    hits: int = Field(ge=0)
    misses: int = Field(ge=0)
    evictions: int = Field(ge=0)
    invalidations: int = Field(ge=0)
    extensions: int = Field(ge=0)
    reseeds: int = Field(ge=0)
    bars_observed: int = Field(ge=0)


@dataclass(slots=True)
class _Track:
    """Retained prefix state for one instrument."""

    #: The numeric cache whose rows the digest covers. Identity is the proof
    #: that the retained rows were not rewritten (the producers only take
    #: ``head`` views); see ``FeatureEngine`` for the same trust model.
    source: pl.DataFrame | None = None
    n: int = 0
    digest: str | None = None
    engine: FeatureEngine = field(default_factory=FeatureEngine)


def _rows_bytes(source: pl.DataFrame, start: int, stop: int) -> bytes:
    """Byte view of rows ``[start, stop)`` in a fixed column order.

    ``to_numpy().tobytes()`` is layout-deterministic for the numeric dtypes the
    frame schema fixes (Int64/Float64), so the digest does not depend on the
    Polars build or on dict ordering.
    """
    out = bytearray()
    for name in _DIGEST_COLUMNS:
        out += source[name].slice(start, stop - start).to_numpy().tobytes()
    return bytes(out)


def _chain(previous: str | None, instrument_id: str, start: int, stop: int, rows: bytes) -> str:
    """``H(previous || instrument || range || rows)``: an append-only chain."""
    hasher = hashlib.sha256()
    hasher.update((previous or "").encode())
    hasher.update(f"|{instrument_id}|{start}|{stop}|".encode())
    hasher.update(rows)
    return hasher.hexdigest()


def prefix_digest(source: pl.DataFrame, n: int, instrument_id: str) -> str:
    """Digest of the first ``n`` rows of ``source`` (full walk, no chain)."""
    return _chain(None, instrument_id, 0, n, _rows_bytes(source, 0, n))


def _entry_bytes(value: Any) -> int:
    """Estimated retained bytes of one cached value (documented as an estimate)."""
    if isinstance(value, tuple):
        # Floats/ints/None: one object reference per slot in the tuple plus a
        # fixed per-entry overhead. A float object itself is not counted
        # twice: values are shared per tuple only.
        return 64 + 16 * len(value)
    if isinstance(value, bool | int | float):
        return 72
    if isinstance(value, bytes | str):
        return 64 + len(value)
    return 128


def _rsi_computer(frame: CausalFrame, params: tuple[int, ...]) -> Any:
    return wilder_rsi(close_series(frame), params[0])


def _emas_computer(frame: CausalFrame, params: tuple[int, ...]) -> Any:
    del params
    return trend_emas(frame)


def _swings_computer(frame: CausalFrame, params: tuple[int, ...]) -> Any:
    return significant_swings(frame, params[0])


def _macd_computer(frame: CausalFrame, params: tuple[int, ...]) -> Any:
    del params
    return macd_line(frame)


def _mean_range_computer(frame: CausalFrame, params: tuple[int, ...]) -> Any:
    return mean_range(frame, params[0])


_COMPUTERS: Final[dict[str, Callable[[CausalFrame, tuple[int, ...]], Any]]] = {
    "wilder_rsi": _rsi_computer,
    "trend_emas": _emas_computer,
    "significant_swings": _swings_computer,
    "macd_line": _macd_computer,
    "mean_range": _mean_range_computer,
}

_PARAMS_ARITY: Final[dict[str, int]] = {
    "wilder_rsi": 1,
    "trend_emas": 0,
    "significant_swings": 1,
    "macd_line": 0,
    "mean_range": 1,
}

#: Specs the wired experts actually ask for. Named constants so callers never
#: rebuild (and re-validate) a spec per bar.
RSI_14: Final[FeatureSpec] = FeatureSpec(name="wilder_rsi", params=(14,))
TREND_EMAS: Final[FeatureSpec] = FeatureSpec(name="trend_emas")
SIGNIFICANT_SWINGS_10: Final[FeatureSpec] = FeatureSpec(name="significant_swings", params=(10,))
MACD_LINE: Final[FeatureSpec] = FeatureSpec(name="macd_line")
MEAN_RANGE_14: Final[FeatureSpec] = FeatureSpec(name="mean_range", params=(14,))


class FeatureCache:
    """Bounded, versioned feature state for one feed (see the module docstring)."""

    __slots__ = (
        "_bars_observed",
        "_bytes",
        "_computations",
        "_entries",
        "_evictions",
        "_hits",
        "_invalidations",
        "_max_bytes",
        "_max_entries",
        "_misses",
        "_reseeds",
        "_tracks",
        "_version",
        "_extensions",
    )

    def __init__(
        self,
        *,
        max_entries: int = DEFAULT_MAX_ENTRIES,
        max_bytes: int | None = None,
        version: str = FEATURE_CACHE_VERSION,
    ) -> None:
        if max_entries < 1:
            raise ValueError("max_entries must be positive")
        if max_bytes is not None and max_bytes < 1:
            raise ValueError("max_bytes must be positive when given")
        if not version:
            raise ValueError("cache version must be non-empty")
        self._version = version
        self._max_entries = max_entries
        self._max_bytes = max_bytes
        self._entries: OrderedDict[CacheKey, Any] = OrderedDict()
        self._tracks: dict[str, _Track] = {}
        self._bytes = 0
        self._computations = 0
        self._hits = 0
        self._misses = 0
        self._evictions = 0
        self._invalidations = 0
        self._extensions = 0
        self._reseeds = 0
        self._bars_observed = 0

    # ------------------------------------------------------------------ state
    @property
    def version(self) -> str:
        return self._version

    @property
    def max_entries(self) -> int:
        return self._max_entries

    def stats(self) -> CacheStats:
        return CacheStats(
            version=self._version,
            entries=len(self._entries),
            max_entries=self._max_entries,
            estimated_bytes=self._bytes,
            computations=self._computations,
            hits=self._hits,
            misses=self._misses,
            evictions=self._evictions,
            invalidations=self._invalidations,
            extensions=self._extensions,
            reseeds=self._reseeds,
            bars_observed=self._bars_observed,
        )

    def invalidate(self, instrument_id: str | None = None) -> int:
        """Drop cached values by name (one instrument, or every instrument).

        Returns the number of entries dropped. A caller uses this when it knows
        an input was revised in a way the digest cannot see (for example a
        policy-level decision to reinterpret a bar); ordinary revisions are
        detected by :meth:`_sync` itself.
        """
        if instrument_id is None:
            dropped = len(self._entries)
            self._entries.clear()
            self._bytes = 0
            self._tracks.clear()
        else:
            dropped = self._drop_instrument(instrument_id)
            self._tracks.pop(instrument_id, None)
        self._invalidations += dropped
        return dropped

    def _drop_instrument(self, instrument_id: str) -> int:
        stale = [key for key in self._entries if key.instrument_id == instrument_id]
        for key in stale:
            self._bytes -= _entry_bytes(self._entries.pop(key))
        return len(stale)

    def _sync(self, frame: CausalFrame) -> _Track:
        """Prove the retained prefix append-only, or invalidate and reseed."""
        instrument_id = frame.instrument_id
        n = len(frame.candles)
        # A retained numeric cache is the replay fast path; the frame's own
        # ``df`` is stable only per frame object, so a bare ``frame_at`` feed
        # still hits within one bar and invalidates on the next.
        source = frame._prefix_df if frame._prefix_df is not None else frame.df  # noqa: SLF001
        track = self._tracks.get(instrument_id)
        if track is None:
            track = _Track(source=source, n=n, digest=prefix_digest(source, n, instrument_id))
            self._tracks[instrument_id] = track
            return track
        if source is track.source and n == track.n:
            return track
        if source is track.source and n > track.n and track.digest is not None:
            track.digest = _chain(
                track.digest,
                instrument_id,
                track.n,
                n,
                _rows_bytes(source, track.n, n),
            )
            track.n = n
            self._extensions += 1
            return track
        # Foreign numeric cache or a shorter prefix: the retained rows can no
        # longer be shown unchanged, so nothing derived from them may be
        # served. The engine reseeds itself on the new identity; the retained
        # values are dropped by name.
        if self._entries:
            self._invalidations += self._drop_instrument(instrument_id)
        track.source = source
        track.n = n
        track.digest = prefix_digest(source, n, instrument_id)
        self._reseeds += 1
        return track

    # --------------------------------------------------------------- scoping
    @contextmanager
    def _bundle(self, frame: CausalFrame, track: _Track) -> Iterator[None]:
        token = _ACTIVE_LAZY.set(_LazyBundle(frame, track.engine))
        try:
            yield
        finally:
            _ACTIVE_LAZY.reset(token)

    @contextmanager
    def scoped(self, frame: CausalFrame) -> Iterator[None]:
        """Run any expert path with this cache's retained indicator state.

        Equivalent to the bundle ``observe_all_28`` installs, but reused across
        bars: the indicator fast paths see the retained prefix and fall back to
        full computation exactly where they always would.
        """
        track = self._sync(frame)
        with self._bundle(frame, track):
            yield

    # ------------------------------------------------------------------ values
    def value(self, frame: CausalFrame, spec: FeatureSpec) -> Any:
        """Indicator value for this frame's prefix, cached by input digest.

        The return type is the feature's own: a tuple for the series kernels
        (``wilder_rsi``), a scalar float for ``macd_line``/``mean_range``, a
        pair for ``trend_emas``/``significant_swings``. Failures raise; nothing
        is cached for an input the kernel refused.
        """
        track = self._sync(frame)
        digest = track.digest
        if digest is None:  # pragma: no cover - _sync always sets one
            raise RuntimeError("cache track has no input digest")
        key = CacheKey(
            version=self._version,
            instrument_id=frame.instrument_id,
            spec_key=spec.key(),
            as_of_ns=frame.decision_ns,
            input_digest=digest,
        )
        cached = self._entries.get(key, _ABSENT)
        if cached is not _ABSENT:
            self._entries.move_to_end(key)
            self._hits += 1
            return cached
        self._misses += 1
        computer = _COMPUTERS[spec.name]
        with self._bundle(frame, track):
            result = computer(frame, spec.params)
        self._computations += 1
        self._store(key, result)
        return result

    def _store(self, key: CacheKey, value: Any) -> None:
        self._entries[key] = value
        self._bytes += _entry_bytes(value)
        while len(self._entries) > self._max_entries or (
            self._max_bytes is not None and self._bytes > self._max_bytes
        ):
            dropped, dropped_value = self._entries.popitem(last=False)
            del dropped
            self._bytes -= _entry_bytes(dropped_value)
            self._evictions += 1

    # --------------------------------------------------------------- ensemble
    def observe_all_28(
        self,
        frame: CausalFrame,
        opportunity: Opportunity | None,
        *,
        readings: tuple[PositioningReading, ...] = (),
    ) -> tuple[Stance, ...]:
        """``observe_all_28`` with this cache's retained, invalidated state.

        The dispatchers are exactly the registry's; only the indicator state
        they see is shared and retained across bars. Stances are therefore
        identical to the uncached path by construction, which the real-tape
        parity test pins.
        """
        track = self._sync(frame)
        self._bars_observed += 1
        return observe_all_28(frame, opportunity, readings=readings, engine=track.engine)

    # ------------------------------------------------------------- inspection
    def digest_for(self, frame: CausalFrame) -> str:
        """The input digest this cache would key ``frame`` under."""
        track = self._sync(frame)
        if track.digest is None:  # pragma: no cover - _sync always sets one
            raise RuntimeError("cache track has no input digest")
        return track.digest

    def retained_bars(self, instrument_id: str) -> int | None:
        """Rows the cache holds a proven digest for, or None if unseen."""
        track = self._tracks.get(instrument_id)
        return None if track is None else track.n

    def keys(self) -> tuple[CacheKey, ...]:
        return tuple(self._entries)


_ABSENT: Final = object()

#: Labels for the digest columns, exported for receipt consumers that need to
#: state exactly what an input digest covers.
DIGEST_COLUMNS: Final[tuple[str, ...]] = _DIGEST_COLUMNS


def decision_digest(rows: Sequence[object]) -> str:
    """Domain-separated digest of a decision stream (receipts, parity gates).

    Imported lazily: the identity module pulls in the research store stack, and
    this module sits on the expert hot path.
    """
    from v8_next.telemetry.identity import canonical_digest

    return canonical_digest("expert-decisions", list(rows))
