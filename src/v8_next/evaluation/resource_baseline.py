"""Resource-baseline measurement receipts and the parity gate (M0-08 / #9).

What this module is for
-----------------------
The 4 GiB claim is a *resource* claim, so it has to be produced by a measurement
and published with its scope, not asserted in prose. This module is the small,
deterministic harness that produces the receipt:

* :func:`measure` times an operation end to end (cold first run, then
  ``repetitions`` warm samples), reports median/p95/min/max wall from
  ``time.perf_counter`` and peak RSS from :func:`resource.getrusage`.
* Machine-readability is the point: every quantity that was not measured
  carries :class:`MeasurementStatus` ``NOT_MEASURED``/``NOT_RUN`` **and a named
  refusal reason**. A model validator refuses a receipt that carries a number
  without ``MEASURED``, or a refusal without the corresponding empty field.
* :class:`ResourceReceipt` binds the numbers to their identity: runtime
  (Python/Polars/NautilusTrader), code SHA, config SHA, tape SHA and the
  decision stream digest. Any of those that cannot be obtained is named
  ``NOT_MEASURED`` -- never invented.
* :func:`parity_gate` compares a baseline digest set with a candidate digest
  set over the five evidence streams. A missing digest can never yield ``PASS``.

What is deliberately not here
-----------------------------
No baseline numbers are hard-coded. PR #1's 2-vCPU/4-GiB figures are a proposed
baseline whose harness (``reports/performance/``) is *not* re-run here; a
measurement produced by this module stands on its own receipt or is not
published. Scope below 40 legs / 12 months is labelled ``NOT_RUN`` even when the
wall/RSS numbers themselves are measured.

Run::

    python -m v8_next.evaluation.resource_baseline \
        --tape research/tape/btcusdt-1h-2025Q1 --mode candidate --repetitions 5 \
        --out reports/overnight/evidence/M0-08-candidate.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import resource
import statistics
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Final, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from v8_next.adapters.tape_paths import REPO_ROOT
from v8_next.domain.market import CausalFrame, build_candle_dataframe, extend_causal_frame
from v8_next.economics.decisions import opportunity_at
from v8_next.evaluation.gate_resolution import load_tape_candles
from v8_next.evaluation.tape_identity import runtime_identity, sha256_file
from v8_next.experts.cache import (
    MACD_LINE,
    MEAN_RANGE_14,
    RSI_14,
    SIGNIFICANT_SWINGS_10,
    TREND_EMAS,
    CacheStats,
    FeatureCache,
    FeatureSpec,
    decision_digest,
)
from v8_next.experts.features import (
    close_series,
    macd_line,
    mean_range,
    significant_swings,
    trend_emas,
    wilder_rsi,
)
from v8_next.experts.registry import observe_all_28
from v8_next.telemetry.identity import canonical_digest

RECEIPT_VERSION: Final = "v9.resource-baseline.1"

#: The deployment target under review (a 4 GiB VPS). The receipt records whether
#: the measured run stayed inside it; scope is published separately.
TARGET_PEAK_RSS_BYTES: Final = 4 * 1024**3
TARGET_LEGS: Final = 40
TARGET_MONTHS: Final = 12.0
#: 365.25 days / 12 = 30.4375 days, in nanoseconds (30.4375 * 86400 * 1e9).
MONTH_NS: Final = 2_629_800_000_000_000

#: Repetitions required before a warm median is published as MEASURED.
MIN_REPETITIONS: Final = 5

#: Evidence streams a candidate must reproduce. Order is the comparison order.
REQUIRED_PARITY_STREAMS: Final[tuple[str, ...]] = (
    "decision",
    "order",
    "fill",
    "funding",
    "report",
)


class MeasurementStatus(StrEnum):
    """Whether a quantity was observed -- and if not, why it is absent."""

    MEASURED = "MEASURED"
    NOT_MEASURED = "NOT_MEASURED"
    NOT_RUN = "NOT_RUN"


class GateStatus(StrEnum):
    """Parity outcome. ``BLOCKED`` is not a soft pass: it never certifies."""

    PASS = "PASS"
    FAIL = "FAIL"
    BLOCKED = "BLOCKED"


def _require_reason(reason: str) -> str:
    if not reason.strip():
        raise ValueError("an unmeasured quantity requires a named refusal reason")
    return reason


class TimingSummary(BaseModel):
    """Wall-clock samples for one phase (cold run, or the warm repetitions)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: MeasurementStatus
    repetitions: int = Field(ge=0)
    median_wall_s: float | None = Field(default=None, ge=0)
    p95_wall_s: float | None = Field(default=None, ge=0)
    min_wall_s: float | None = Field(default=None, ge=0)
    max_wall_s: float | None = Field(default=None, ge=0)
    refusal_reason: str | None = None

    @model_validator(mode="after")
    def _status_matches_numbers(self) -> Self:
        numbers = (
            self.median_wall_s,
            self.p95_wall_s,
            self.min_wall_s,
            self.max_wall_s,
        )
        if self.status is MeasurementStatus.MEASURED:
            if self.repetitions < 1:
                raise ValueError("MEASURED timing requires at least one repetition")
            if any(value is None for value in numbers):
                raise ValueError("MEASURED timing requires median, p95, min and max")
            if self.refusal_reason is not None:
                raise ValueError("MEASURED timing cannot carry a refusal reason")
        else:
            if any(value is not None for value in numbers):
                raise ValueError("unmeasured timing cannot carry numbers")
            _require_reason(self.refusal_reason or "")
        return self

    @classmethod
    def not_measured(cls, reason: str) -> Self:
        return cls(
            status=MeasurementStatus.NOT_MEASURED,
            repetitions=0,
            refusal_reason=_require_reason(reason),
        )

    @classmethod
    def not_run(cls, reason: str) -> Self:
        return cls(
            status=MeasurementStatus.NOT_RUN,
            repetitions=0,
            refusal_reason=_require_reason(reason),
        )


class MemorySummary(BaseModel):
    """Peak RSS of the measuring process, as the OS reports it.

    ``ru_maxrss`` is a high-water mark and its unit is platform-defined: bytes on
    macOS, KiB on Linux. Both the raw value and the unit are published so a
    reader never has to guess which one they are looking at.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: MeasurementStatus
    peak_rss_bytes: int | None = Field(default=None, ge=0)
    ru_maxrss_raw: int | None = Field(default=None, ge=0)
    ru_maxrss_unit: Literal["bytes", "kibibytes"] | None = None
    refusal_reason: str | None = None

    @model_validator(mode="after")
    def _status_matches_numbers(self) -> Self:
        if self.status is MeasurementStatus.MEASURED:
            if self.peak_rss_bytes is None or self.ru_maxrss_raw is None:
                raise ValueError("MEASURED memory requires the rusage high-water mark")
            if self.ru_maxrss_unit is None:
                raise ValueError("MEASURED memory requires the rusage unit")
        else:
            if any(
                value is not None
                for value in (self.peak_rss_bytes, self.ru_maxrss_raw, self.ru_maxrss_unit)
            ):
                raise ValueError("unmeasured memory cannot carry numbers")
            _require_reason(self.refusal_reason or "")
        return self

    @classmethod
    def not_measured(cls, reason: str) -> Self:
        return cls(
            status=MeasurementStatus.NOT_MEASURED,
            refusal_reason=_require_reason(reason),
        )


class TapeIdentity(BaseModel):
    """Physical identity of the tape the numbers were measured on."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str
    sha256: str | None = None
    bytes: int | None = Field(default=None, ge=0)
    bars: int | None = Field(default=None, ge=0)
    instrument_id: str | None = None
    status: MeasurementStatus = MeasurementStatus.NOT_RUN
    refusal_reason: str | None = None

    @model_validator(mode="after")
    def _status_matches_numbers(self) -> Self:
        if self.status is MeasurementStatus.MEASURED:
            if not self.sha256 or self.bytes is None:
                raise ValueError("MEASURED tape identity requires sha256 and byte count")
        else:
            if self.sha256 is not None:
                raise ValueError("unmeasured tape identity cannot carry a digest")
            _require_reason(self.refusal_reason or "")
        return self

    @classmethod
    def from_path(
        cls,
        path: Path | str,
        *,
        bars: int | None = None,
        instrument_id: str | None = None,
    ) -> Self:
        p = Path(path)
        if not p.is_file():
            return cls(
                path=str(p),
                status=MeasurementStatus.NOT_RUN,
                refusal_reason=f"TAPE_ABSENT: {p}",
                bars=bars,
                instrument_id=instrument_id,
            )
        return cls(
            path=str(p),
            sha256=sha256_file(p),
            bytes=p.stat().st_size,
            bars=bars,
            instrument_id=instrument_id,
            status=MeasurementStatus.MEASURED,
        )


class ScopeCoverage(BaseModel):
    """The deployment scope a run actually covers, against the 40-leg/12-month goal."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    legs: int = Field(ge=0)
    months: float = Field(ge=0)
    target_legs: int = TARGET_LEGS
    target_months: float = TARGET_MONTHS
    status: MeasurementStatus
    refusal_reason: str | None = None

    @model_validator(mode="after")
    def _status_matches_scope(self) -> Self:
        if self.status is MeasurementStatus.MEASURED and not self.covers_target():
            raise ValueError("MEASURED scope must cover the 40-leg/12-month target")
        if self.status is not MeasurementStatus.MEASURED:
            _require_reason(self.refusal_reason or "")
        return self

    def covers_target(self) -> bool:
        return self.legs >= self.target_legs and self.months >= self.target_months

    @classmethod
    def observed(cls, *, legs: int, months: float) -> Self:
        if legs >= TARGET_LEGS and months >= TARGET_MONTHS:
            return cls(legs=legs, months=months, status=MeasurementStatus.MEASURED)
        return cls(
            legs=legs,
            months=months,
            status=MeasurementStatus.NOT_RUN,
            refusal_reason=(
                f"SCOPE_BELOW_TARGET: measured {legs} leg(s) / {months:.2f} month(s), "
                f"target is {TARGET_LEGS} legs / {TARGET_MONTHS:.0f} months"
            ),
        )


class ParityStreamDigests(BaseModel):
    """One digest per evidence stream, or ``None`` when the stream was not run."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    decision: str | None = None
    order: str | None = None
    fill: str | None = None
    funding: str | None = None
    report: str | None = None

    @model_validator(mode="after")
    def _digests_are_well_formed(self) -> Self:
        for stream in REQUIRED_PARITY_STREAMS:
            digest = getattr(self, stream)
            if digest is not None and not _is_digest(digest):
                raise ValueError(f"blank or malformed digest for stream {stream!r}")
        return self

    def digest(self, stream: str) -> str | None:
        if stream not in REQUIRED_PARITY_STREAMS:
            raise ValueError(f"unknown parity stream {stream!r}")
        value: str | None = getattr(self, stream)
        return value

    def missing(self) -> tuple[str, ...]:
        return tuple(stream for stream in REQUIRED_PARITY_STREAMS if self.digest(stream) is None)


def _is_digest(value: str) -> bool:
    return len(value.strip()) >= 32 and all(char in "0123456789abcdef" for char in value)


class StreamVerdict(BaseModel):
    """Verdict for one evidence stream."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    stream: str
    status: GateStatus
    baseline_digest: str | None = None
    candidate_digest: str | None = None
    reason: str | None = None


class ParityGateResult(BaseModel):
    """Aggregate parity outcome; ``blocking_reasons`` names every gap."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: GateStatus
    streams: tuple[StreamVerdict, ...]
    blocking_reasons: tuple[str, ...] = ()

    def passed(self) -> bool:
        return self.status is GateStatus.PASS


def parity_gate(
    baseline: ParityStreamDigests,
    candidate: ParityStreamDigests,
) -> ParityGateResult:
    """Compare baseline and candidate digests over the five evidence streams.

    Precedence is fail-closed: any *observed* drift is ``FAIL`` (positive
    evidence the candidate changed behaviour); otherwise any missing digest is
    ``BLOCKED`` (no evidence either way); only five matching digests are
    ``PASS``. A missing digest can never be reported as a pass.
    """
    verdicts: list[StreamVerdict] = []
    blockers: list[str] = []
    for stream in REQUIRED_PARITY_STREAMS:
        base = baseline.digest(stream)
        cand = candidate.digest(stream)
        if base is None or cand is None:
            absent = [
                side for side, value in (("baseline", base), ("candidate", cand)) if value is None
            ]
            reason = f"DIGEST_MISSING:{stream}:{'+'.join(absent)}"
            blockers.append(reason)
            verdicts.append(
                StreamVerdict(
                    stream=stream,
                    status=GateStatus.BLOCKED,
                    baseline_digest=base,
                    candidate_digest=cand,
                    reason=reason,
                )
            )
            continue
        if base != cand:
            reason = f"DIGEST_DRIFT:{stream}"
            blockers.append(reason)
            verdicts.append(
                StreamVerdict(
                    stream=stream,
                    status=GateStatus.FAIL,
                    baseline_digest=base,
                    candidate_digest=cand,
                    reason=reason,
                )
            )
            continue
        verdicts.append(
            StreamVerdict(
                stream=stream,
                status=GateStatus.PASS,
                baseline_digest=base,
                candidate_digest=cand,
            )
        )
    if any(verdict.status is GateStatus.FAIL for verdict in verdicts):
        status = GateStatus.FAIL
    elif any(verdict.status is GateStatus.BLOCKED for verdict in verdicts):
        status = GateStatus.BLOCKED
    else:
        status = GateStatus.PASS
    return ParityGateResult(
        status=status, streams=tuple(verdicts), blocking_reasons=tuple(blockers)
    )


class ResourceReceipt(BaseModel):
    """Machine-readable record of one measured resource run."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    receipt_version: str = RECEIPT_VERSION
    issue_id: str
    workload: str
    mode: Literal["baseline", "candidate"]
    cold: TimingSummary
    warm: TimingSummary
    memory: MemorySummary
    target_peak_rss_bytes: int = TARGET_PEAK_RSS_BYTES
    within_rss_target: bool | None = None
    oom: bool | None = None
    #: Field name -> why the field is ``None``. Every ``None`` above must appear
    #: here and every key here must be ``None``; the validator is bidirectional.
    refusals: dict[str, str] = Field(default_factory=dict)
    runtime: dict[str, str] = Field(default_factory=dict)
    code_sha: str | None = None
    config_sha256: str | None = None
    tape: TapeIdentity
    digests: ParityStreamDigests
    scope: ScopeCoverage
    cache: CacheStats | None = None
    notes: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _refusals_match_empty_fields(self) -> Self:
        governed: tuple[str, ...] = ("code_sha", "config_sha256", "within_rss_target", "oom")
        for name in governed:
            value = getattr(self, name)
            reason = self.refusals.get(name)
            if value is None and not (reason or "").strip():
                raise ValueError(f"{name} is None without a refusal reason")
            if value is not None and reason is not None:
                raise ValueError(f"{name} carries a refusal reason but has a value")
        for name in self.refusals:
            if name not in governed:
                raise ValueError(f"refusal names unknown field {name!r}")
        if (
            self.cold.status is MeasurementStatus.MEASURED
            and self.warm.status is not MeasurementStatus.MEASURED
        ):
            raise ValueError("a cold run without a warm measurement is not a baseline")
        if (
            self.warm.status is MeasurementStatus.MEASURED
            and self.warm.repetitions < MIN_REPETITIONS
        ):
            raise ValueError(
                f"warm median requires at least {MIN_REPETITIONS} repetitions, "
                f"got {self.warm.repetitions}"
            )
        if self.within_rss_target is not None and self.memory.peak_rss_bytes is None:
            raise ValueError("within_rss_target requires a measured peak RSS")
        return self

    def as_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")

    def to_json(self) -> str:
        return json.dumps(self.as_dict(), indent=2, sort_keys=True) + "\n"

    def write(self, path: Path | str) -> Path:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(self.to_json())
        return target


# --------------------------------------------------------------------------- #
# measurement primitives
# --------------------------------------------------------------------------- #
def peak_rss() -> tuple[int, int, Literal["bytes", "kibibytes"]]:
    """``(bytes, raw_ru_maxrss, unit)`` for the process high-water mark."""
    raw = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    if sys.platform == "darwin":
        return raw, raw, "bytes"
    return raw * 1024, raw, "kibibytes"


def nearest_rank_p95(values: list[float]) -> float:
    """Nearest-rank 95th percentile (``ceil(0.95 n)``-th smallest)."""
    if not values:
        raise ValueError("p95 requires at least one sample")
    ordered = sorted(values)
    rank = max(1, -(-95 * len(ordered) // 100))
    return ordered[rank - 1]


@dataclass(frozen=True)
class Measurement:
    """One measured run: cold phase, warm repetitions, process high-water mark."""

    cold: TimingSummary
    warm: TimingSummary
    memory: MemorySummary


def measure(
    operation: Callable[[], object],
    *,
    warmup: int = 1,
    repetitions: int = MIN_REPETITIONS,
) -> Measurement:
    """Time ``operation``: first call cold, then warmup, then ``repetitions``.

    The cold sample is the very first invocation (interpreter/module caches
    still cold); the warm samples follow a discarded warmup. Peak RSS is the
    OS high-water mark for the whole measuring process, which is why the CLI
    runs one mode per process.
    """
    if repetitions < 1:
        raise ValueError("repetitions must be positive")
    if warmup < 0:
        raise ValueError("warmup must be non-negative")

    cold_start = time.perf_counter()
    operation()
    cold_s = time.perf_counter() - cold_start

    for _ in range(warmup):
        operation()

    samples: list[float] = []
    for _ in range(repetitions):
        started = time.perf_counter()
        operation()
        samples.append(time.perf_counter() - started)

    rss_bytes, rss_raw, rss_unit = peak_rss()
    cold = TimingSummary(
        status=MeasurementStatus.MEASURED,
        repetitions=1,
        median_wall_s=cold_s,
        p95_wall_s=cold_s,
        min_wall_s=cold_s,
        max_wall_s=cold_s,
    )
    warm = TimingSummary(
        status=MeasurementStatus.MEASURED,
        repetitions=len(samples),
        median_wall_s=statistics.median(samples),
        p95_wall_s=nearest_rank_p95(samples),
        min_wall_s=min(samples),
        max_wall_s=max(samples),
    )
    memory = MemorySummary(
        status=MeasurementStatus.MEASURED,
        peak_rss_bytes=rss_bytes,
        ru_maxrss_raw=rss_raw,
        ru_maxrss_unit=rss_unit,
    )
    return Measurement(cold=cold, warm=warm, memory=memory)


# --------------------------------------------------------------------------- #
# the measured workload: the 28-expert ensemble over a real tape
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class ReplayResult:
    """One full replay of the ensemble over a tape slice."""

    bars: int
    instrument_id: str
    stances: int
    start_ns: int
    end_ns: int
    rows: tuple[object, ...]
    cache: CacheStats | None

    def decision_digest(self) -> str:
        return decision_digest(self.rows)


def replay_expert_decisions(
    tape: Path | str,
    *,
    use_cache: bool,
    limit: int | None = None,
    max_entries: int = 512,
    instrument_id: str | None = None,
) -> ReplayResult:
    """Replay every bar of ``tape`` through the 28-expert ensemble.

    ``use_cache`` selects the candidate path (:class:`FeatureCache`, retained
    incremental indicator state) or the baseline path (the same
    ``observe_all_28`` without a cache). Both ask the same dispatchers and the
    same feature kernels; only state retention differs, so the stance streams
    must be identical.
    """
    candles = tuple(load_tape_candles(tape, limit=limit, instrument=instrument_id))
    if not candles:
        raise ValueError(f"tape {tape} yielded no candles")
    numeric_cache = build_candle_dataframe(candles)
    instrument = candles[0].instrument_id
    cache = FeatureCache(max_entries=max_entries) if use_cache else None

    frame: CausalFrame | None = None
    rows: list[object] = []
    stances = 0
    for candle in candles:
        if frame is None:
            frame = CausalFrame.from_ordered_prefix(
                instrument, candle.end_ns, (candle,), numeric_cache
            )
        else:
            extended = extend_causal_frame(frame, candle.end_ns, candle, numeric_cache)
            if extended is None:  # pragma: no cover - the tape is ordered by construction
                raise RuntimeError(f"candle at {candle.start_ns} does not extend the frame")
            frame = extended
        opportunity = opportunity_at(frame)
        observed = (
            cache.observe_all_28(frame, opportunity)
            if cache is not None
            else observe_all_28(frame, opportunity)
        )
        stances += len(observed)
        rows.append(
            [
                frame.decision_ns,
                [
                    [stance.observer_id, stance.kind.value, stance.reason, stance.opportunity_id]
                    for stance in observed
                ],
            ]
        )
    return ReplayResult(
        bars=len(candles),
        instrument_id=instrument,
        stances=stances,
        start_ns=candles[0].start_ns,
        end_ns=candles[-1].end_ns,
        rows=tuple(rows),
        cache=cache.stats() if cache is not None else None,
    )


#: The five indicator specs the wired experts consume, in cache order.
FEATURE_SPECS: Final[tuple[FeatureSpec, ...]] = (
    RSI_14,
    TREND_EMAS,
    SIGNIFICANT_SWINGS_10,
    MACD_LINE,
    MEAN_RANGE_14,
)

#: A feature cannot answer below its longest warmup (MACD needs 34 bars).
FEATURE_WARMUP_BARS: Final = 40


@dataclass(frozen=True)
class FeatureReplayResult:
    """One replay of the shared feature reads over a tape slice."""

    bars: int
    instrument_id: str
    start_ns: int
    end_ns: int
    reads: int
    values_digest: str
    cache: CacheStats | None


def _direct_feature(frame: CausalFrame, spec: FeatureSpec) -> Any:
    """The unbundled reference read: full-prefix kernel, no retained state."""
    if spec.name == "wilder_rsi":
        return wilder_rsi(close_series(frame), spec.params[0])
    if spec.name == "trend_emas":
        return trend_emas(frame)
    if spec.name == "significant_swings":
        return significant_swings(frame, spec.params[0])
    if spec.name == "macd_line":
        return macd_line(frame)
    if spec.name == "mean_range":
        return mean_range(frame, spec.params[0])
    raise ValueError(f"unsupported feature {spec.name!r}")  # pragma: no cover


def _current_reading(value: Any) -> Any:
    """The bar's current reading of a feature value.

    Series kernels return the whole retained prefix (``wilder_rsi`` returns one
    entry per bar); digesting that on every bar would itself be ``O(n^2)``, so
    the receipt binds the *current* reading -- the value a decision consumes.
    Full-tuple equality is asserted by the unit tests instead.
    """
    if isinstance(value, tuple):
        return value[-1] if value else None
    return value


def replay_feature_values(
    tape: Path | str,
    *,
    use_cache: bool,
    consumers: int = 1,
    limit: int | None = None,
    max_entries: int = 512,
    instrument_id: str | None = None,
) -> FeatureReplayResult:
    """Read the five shared specs every bar from ``consumers`` readers.

    ``use_cache`` selects :meth:`FeatureCache.value` (retained incremental
    kernels, bounded LRU) or the direct full-prefix kernels. Both see the same
    ``CausalFrame`` sequence and record a value or the literal
    ``"UNAVAILABLE"`` where a kernel refuses (warmup), so the two value streams
    are directly comparable.
    """
    if consumers < 1:
        raise ValueError("consumers must be positive")
    candles = tuple(load_tape_candles(tape, limit=limit, instrument=instrument_id))
    if not candles:
        raise ValueError(f"tape {tape} yielded no candles")
    numeric_cache = build_candle_dataframe(candles)
    instrument = candles[0].instrument_id
    cache = FeatureCache(max_entries=max_entries) if use_cache else None

    frame: CausalFrame | None = None
    stream: list[object] = []
    reads = 0
    for candle in candles:
        if frame is None:
            frame = CausalFrame.from_ordered_prefix(
                instrument, candle.end_ns, (candle,), numeric_cache
            )
        else:
            extended = extend_causal_frame(frame, candle.end_ns, candle, numeric_cache)
            if extended is None:  # pragma: no cover - ordered tape by construction
                raise RuntimeError(f"candle at {candle.start_ns} does not extend the frame")
            frame = extended
        if len(frame.candles) < FEATURE_WARMUP_BARS:
            continue
        bar_values: list[object] = []
        for _ in range(consumers):
            for spec in FEATURE_SPECS:
                try:
                    value = (
                        cache.value(frame, spec)
                        if cache is not None
                        else _direct_feature(frame, spec)
                    )
                except ValueError:
                    value = "UNAVAILABLE"
                reads += 1
                bar_values.append(_current_reading(value))
        stream.append([frame.decision_ns, bar_values])
    return FeatureReplayResult(
        bars=len(candles),
        instrument_id=instrument,
        start_ns=candles[0].start_ns,
        end_ns=candles[-1].end_ns,
        reads=reads,
        values_digest=canonical_digest("shared-feature-values", stream),
        cache=cache.stats() if cache is not None else None,
    )


class FeatureValueReceipt(BaseModel):
    """Measured feature-read run, bound to its runtime and tape identity."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    receipt_version: str = RECEIPT_VERSION
    issue_id: str = "M1-06"
    workload: str = "shared feature values (5 specs), per-bar reads over real tape"
    mode: Literal["baseline", "candidate"]
    consumers: int = Field(ge=1)
    cold: TimingSummary
    warm: TimingSummary
    memory: MemorySummary
    runtime: dict[str, str] = Field(default_factory=dict)
    code_sha: str | None = None
    refusals: dict[str, str] = Field(default_factory=dict)
    tape: TapeIdentity
    values_digest: str
    reads: int = Field(ge=0)
    cache: CacheStats | None = None
    notes: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _refusals_match_empty_fields(self) -> Self:
        reason = self.refusals.get("code_sha")
        if self.code_sha is None and not (reason or "").strip():
            raise ValueError("code_sha is None without a refusal reason")
        if self.code_sha is not None and reason is not None:
            raise ValueError("code_sha carries a refusal reason but has a value")
        for name in self.refusals:
            if name != "code_sha":
                raise ValueError(f"refusal names unknown field {name!r}")
        if self.values_digest.strip() == "":
            raise ValueError("feature values digest cannot be empty")
        if (
            self.warm.status is MeasurementStatus.MEASURED
            and self.warm.repetitions < MIN_REPETITIONS
        ):
            raise ValueError(
                f"warm median requires at least {MIN_REPETITIONS} repetitions, "
                f"got {self.warm.repetitions}"
            )
        return self

    def as_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")

    def to_json(self) -> str:
        return json.dumps(self.as_dict(), indent=2, sort_keys=True) + "\n"

    def write(self, path: Path | str) -> Path:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(self.to_json())
        return target


def values_parity(baseline: str | None, candidate: str | None) -> GateStatus:
    """PASS/FAIL/BLOCKED on two feature-value digests (missing never passes)."""
    if baseline is None or candidate is None or not baseline.strip() or not candidate.strip():
        return GateStatus.BLOCKED
    return GateStatus.PASS if baseline == candidate else GateStatus.FAIL


def build_feature_receipt(
    *,
    tape: Path | str,
    mode: Literal["baseline", "candidate"],
    consumers: int = 1,
    repetitions: int = MIN_REPETITIONS,
    warmup: int = 1,
    limit: int | None = None,
    max_entries: int = 512,
    issue_id: str = "M1-06",
) -> FeatureValueReceipt:
    """Measure the feature-value workload and bind the numbers to their identity."""
    if repetitions < MIN_REPETITIONS:
        raise ValueError(f"at least {MIN_REPETITIONS} repetitions are required")
    tape_path = Path(tape)
    last: list[FeatureReplayResult] = []

    def operation() -> None:
        last.append(
            replay_feature_values(
                tape_path,
                use_cache=mode == "candidate",
                consumers=consumers,
                limit=limit,
                max_entries=max_entries,
            )
        )

    measurement = measure(operation, warmup=warmup, repetitions=repetitions)
    result = last[-1]
    tape_identity = TapeIdentity.from_path(
        tape_path if tape_path.is_file() else tape_path / "tape.jsonl",
        bars=result.bars,
        instrument_id=result.instrument_id,
    )
    code_sha = git_sha()
    refusals: dict[str, str] = {}
    if code_sha is None:
        refusals["code_sha"] = "GIT_HEAD_UNREADABLE: run the measurement inside a git checkout"
    return FeatureValueReceipt(
        issue_id=issue_id,
        mode=mode,
        consumers=consumers,
        cold=measurement.cold,
        warm=measurement.warm,
        memory=measurement.memory,
        runtime=runtime_identity(),
        code_sha=code_sha,
        refusals=refusals,
        tape=tape_identity,
        values_digest=result.values_digest,
        reads=result.reads,
        cache=result.cache,
        notes=(
            "values_digest compares the five shared specs bar by bar against the "
            "unbundled full-prefix kernels.",
        ),
    )


def funding_digest(tape: Path | str) -> str:
    """Digest of the tape's funding events, in file order (real tape bytes)."""
    path = Path(tape)
    if path.is_dir():
        path = path / "tape.jsonl"
    hasher = hashlib.sha256()
    count = 0
    with path.open("rb") as handle:
        for line in handle:
            if b'"channel":"funding"' in line:
                hasher.update(line)
                count += 1
    if count == 0:
        raise ValueError(f"tape {path} carries no funding events")
    return hasher.hexdigest()


# --------------------------------------------------------------------------- #
# receipt assembly
# --------------------------------------------------------------------------- #
def git_sha(repo: Path | str = REPO_ROOT) -> str | None:
    """HEAD SHA of the measuring checkout, or None when git cannot answer."""
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo,
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    sha = completed.stdout.strip()
    return sha or None


def config_sha256(**config: object) -> str:
    """Identity of the effective workload configuration (no wall clock)."""
    return canonical_digest("resource-baseline-config", config)


def build_receipt(
    *,
    tape: Path | str,
    mode: Literal["baseline", "candidate"],
    repetitions: int = MIN_REPETITIONS,
    warmup: int = 1,
    limit: int | None = None,
    max_entries: int = 512,
    issue_id: str = "M0-08",
) -> ResourceReceipt:
    """Measure one mode and bind the numbers to their identity."""
    if repetitions < MIN_REPETITIONS:
        raise ValueError(f"at least {MIN_REPETITIONS} repetitions are required")
    tape_path = Path(tape)
    last: list[ReplayResult] = []

    def operation() -> None:
        last.append(
            replay_expert_decisions(
                tape_path,
                use_cache=mode == "candidate",
                limit=limit,
                max_entries=max_entries,
            )
        )

    measurement = measure(operation, warmup=warmup, repetitions=repetitions)
    result = last[-1]
    cache_stats = result.cache

    tape_identity = TapeIdentity.from_path(
        tape_path if tape_path.is_file() else tape_path / "tape.jsonl",
        bars=result.bars,
        instrument_id=result.instrument_id,
    )

    scope = ScopeCoverage.observed(legs=1, months=(result.end_ns - result.start_ns) / MONTH_NS)

    code_sha = git_sha()
    refusals: dict[str, str] = {}
    if code_sha is None:
        refusals["code_sha"] = "GIT_HEAD_UNREADABLE: run the measurement inside a git checkout"
    config = config_sha256(
        mode=mode,
        tape=tape_identity.sha256,
        bars=result.bars,
        max_entries=max_entries,
        cache_version=None if cache_stats is None else cache_stats.version,
    )

    memory = measurement.memory
    within_target: bool | None = None
    oom: bool | None = None
    if memory.status is MeasurementStatus.MEASURED and memory.peak_rss_bytes is not None:
        within_target = memory.peak_rss_bytes <= TARGET_PEAK_RSS_BYTES
        # The replay completed inside this process, so no OOM occurred. This is
        # an observation of the measured run, not a prediction for larger scope.
        oom = False
    else:  # pragma: no cover - measure() either measures or raises
        refusals["within_rss_target"] = "PEAK_RSS_NOT_MEASURED"
        refusals["oom"] = "PEAK_RSS_NOT_MEASURED"

    digests = ParityStreamDigests(
        decision=result.decision_digest(),
        funding=funding_digest(tape_path),
        order=None,
        fill=None,
        report=None,
    )

    notes = (
        "order/fill/report digests are NOT_RUN: this slice measures the expert "
        "decision layer only and runs no economic execution replay.",
        "PR #1's 2-vCPU/4-GiB container figures are a proposed baseline and were "
        "not re-run; this receipt stands on its own measurement.",
    )

    return ResourceReceipt(
        issue_id=issue_id,
        workload="28-expert ensemble, per-bar observe_all_28 over real tape",
        mode=mode,
        cold=measurement.cold,
        warm=measurement.warm,
        memory=memory,
        target_peak_rss_bytes=TARGET_PEAK_RSS_BYTES,
        within_rss_target=within_target,
        oom=oom,
        refusals=refusals,
        runtime=runtime_identity(),
        code_sha=code_sha,
        config_sha256=config,
        tape=tape_identity,
        digests=digests,
        scope=scope,
        cache=cache_stats,
        notes=notes,
    )


def load_receipt(path: Path | str) -> ResourceReceipt:
    """Read a receipt back, re-validating every invariant on the way in."""
    return ResourceReceipt.model_validate(json.loads(Path(path).read_text()))


def compare_receipts(baseline: ResourceReceipt, candidate: ResourceReceipt) -> ParityGateResult:
    """The parity gate over two measured receipts."""
    return parity_gate(baseline.digests, candidate.digests)


def load_feature_receipt(path: Path | str) -> FeatureValueReceipt:
    """Read a feature-value receipt back, re-validating every invariant."""
    return FeatureValueReceipt.model_validate(json.loads(Path(path).read_text()))


def _summary(
    receipt: ResourceReceipt | FeatureValueReceipt,
    gate: ParityGateResult | GateStatus | None,
) -> dict[str, Any]:
    receipt_dict: dict[str, Any] = receipt.as_dict()
    gate_dict: Any = gate
    if isinstance(gate, ParityGateResult):
        gate_dict = gate.model_dump(mode="json")
    elif isinstance(gate, GateStatus):
        gate_dict = gate.value
    return {"receipt": receipt_dict, "parity": gate_dict}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="v8_next.evaluation.resource_baseline",
        description="Measure one resource baseline run and write its receipt (M0-08 / M1-06).",
    )
    parser.add_argument("--tape", required=True, help="Tape directory or tape.jsonl path")
    parser.add_argument(
        "--workload",
        choices=("ensemble", "features"),
        default="ensemble",
        help="ensemble: 28-expert observe_all_28; features: the five shared specs",
    )
    parser.add_argument("--mode", choices=("baseline", "candidate"), required=True)
    parser.add_argument("--repetitions", type=int, default=MIN_REPETITIONS)
    parser.add_argument("--warmup", type=int, default=1)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--max-entries", type=int, default=512)
    parser.add_argument("--consumers", type=int, default=1)
    parser.add_argument("--out", default=None, help="Write the receipt JSON here")
    parser.add_argument(
        "--compare",
        default=None,
        help="Another receipt of the same workload to gate against",
    )
    args = parser.parse_args(argv)

    summary: dict[str, Any]
    if args.workload == "features":
        feature_receipt = build_feature_receipt(
            tape=Path(args.tape),
            mode=args.mode,
            consumers=args.consumers,
            repetitions=args.repetitions,
            warmup=args.warmup,
            limit=args.limit,
            max_entries=args.max_entries,
        )
        status: GateStatus | None = None
        if args.compare is not None:
            other = load_feature_receipt(args.compare)
            status = (
                values_parity(other.values_digest, feature_receipt.values_digest)
                if args.mode == "candidate"
                else values_parity(feature_receipt.values_digest, other.values_digest)
            )
        if args.out is not None:
            feature_receipt.write(args.out)
        summary = _summary(feature_receipt, status)
    else:
        receipt = build_receipt(
            tape=Path(args.tape),
            mode=args.mode,
            repetitions=args.repetitions,
            warmup=args.warmup,
            limit=args.limit,
            max_entries=args.max_entries,
        )
        gate: ParityGateResult | None = None
        if args.compare is not None:
            other_receipt = load_receipt(args.compare)
            gate = (
                compare_receipts(other_receipt, receipt)
                if args.mode == "candidate"
                else compare_receipts(receipt, other_receipt)
            )
        if args.out is not None:
            receipt.write(args.out)
        summary = _summary(receipt, gate)

    json.dump(summary, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
