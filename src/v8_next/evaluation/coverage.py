"""M4-04 (#32) point-in-time universe coverage and regime qualification.

What this module is
-------------------
A *reporting* layer over recorded tapes and point-in-time instrument metadata. It
answers questions that must be answered before any altcoin economic claim is made:

* which symbols the supplied tapes actually cover, under which dataset role;
* what each symbol's observed span, expected bar count, duplicate/out-of-order
  integrity, missing bars, funding rows and mark rows are;
* which delisting history exists for the symbol (and therefore whether a
  survivorship bias can be excluded);
* which source bytes and metadata versions the numbers were computed from;
* whether an independent (non-BTC) universe is provisioned at all, and if not,
  the named blocker -- never a synthetic stand-in.

What this module is not
-----------------------
It does not evaluate a strategy, mutate a belief, load an execution adapter or
produce an economic verdict. Every regime block therefore carries
``economic_result = NOT_RUN`` unless a caller hands in outcomes produced by a
real economic run, and ``PASS`` is not a member of the conclusion vocabulary.
Nothing here imputes a missing bar, a missing funding settlement or a missing
mark price: absences are counted, named and published as ``PARTIAL``/``BLOCKED``.

Point-in-time rules, all fail closed
------------------------------------
* Rows after ``as_of_ns`` are excluded from every count and reported as
  ``rows_after_as_of`` rather than silently mixed into coverage.
* Instrument metadata is resolved through
  :func:`v8_next.adapters.universe.resolve_metadata` at the same ``as_of_ns``; a
  record that only became readable later is not used, and an unresolved version
  leaves ``survivorship_risk`` set with a named reason.
* Duplicate timestamps, out-of-order rows and off-grid timestamps within one
  channel are a :class:`TapeIntegrityError`: a ratio computed over ambiguous
  bars would be a fabricated number.
* Bars at or after a resolved ``delisted_at`` are contradictory and mark the
  symbol ``BLOCKED``.

Run::

    python -m v8_next.evaluation.coverage \
        --tape research/tape/btcusdt-1h-2025Q1 --json
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation
from enum import StrEnum
from pathlib import Path
from typing import Any, Final, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from v8_next.adapters.universe import (
    ECONOMIC_QUALIFICATION_BLOCKER,
    InstrumentMetadata,
    ResolutionStatus,
    resolve_metadata,
)
from v8_next.evaluation.protocol import ROLE_ORDER, DatasetRole
from v8_next.evaluation.tape_identity import sha256_file
from v8_next.telemetry.identity import canonical_digest

SCHEMA_VERSION: Final = "coverage-report-v1"

#: One funding settlement per eight hours is the venue convention. It is used
#: only to *expect* settlements (and is named as a defaulted assumption in the
#: row's gaps); it is never used to synthesize a funding row.
DEFAULT_FUNDING_INTERVAL_NS: Final = 8 * 3_600 * 1_000_000_000

#: Channels this module counts. A channel outside these sets is ignored (not
#: guessed at), so an unknown capture channel cannot inflate coverage.
KLINE_CHANNELS: Final[frozenset[str]] = frozenset({"kline", "candle", "bar"})
FUNDING_CHANNELS: Final[frozenset[str]] = frozenset({"funding", "funding_rate"})
MARK_CHANNELS: Final[frozenset[str]] = frozenset({"mark", "mark_price"})

#: Role order is the report's row order (development first, sealed last).
ROLE_INDEX: Final[dict[DatasetRole, int]] = {role: i for i, role in enumerate(ROLE_ORDER)}

#: Role lookup key that applies to every symbol without an explicit role.
ANY_SYMBOL: Final = "*"

#: Descriptive regime classifier parameters. These are *reporting* thresholds,
#: not economic convictions: they segment the supplied bars so that coverage can
#: be broken down by market state. No confidence interval is attached.
REGIME_WINDOW_BARS: Final = 24
REGIME_TREND_THRESHOLD: Final = Decimal("0.02")
REGIME_HIGH_VOL_VARIANCE: Final = Decimal("0.0001")  # (1% per bar)^2
REGIME_METHOD: Final = "rolling_return_and_variance_v1"

_Q6: Final = Decimal("0.000001")
_NS_PER_MS: Final = 1_000_000


class DataStatus(StrEnum):
    """Whether a row/report covers what the intake says it should."""

    AVAILABLE = "AVAILABLE"
    PARTIAL = "PARTIAL"
    BLOCKED = "BLOCKED"


class UniverseStatus(StrEnum):
    """Whether an independent (non-BTC) universe was provisioned at all."""

    PROVISIONED = "PROVISIONED"
    NOT_RUN = "NOT_RUN"


class Regime(StrEnum):
    """Descriptive market-state label for one bar window."""

    TREND_UP = "TREND_UP"
    TREND_DOWN = "TREND_DOWN"
    RANGE = "RANGE"
    HIGH_VOL = "HIGH_VOL"


class RegimeConclusion(StrEnum):
    """Regime-level conclusion. ``PASS`` is deliberately absent."""

    NO_EDGE = "NO_EDGE"
    INCONCLUSIVE = "INCONCLUSIVE"
    NOT_RUN = "NOT_RUN"


REGIME_ORDER: Final[tuple[Regime, ...]] = (
    Regime.TREND_UP,
    Regime.TREND_DOWN,
    Regime.RANGE,
    Regime.HIGH_VOL,
)


# --------------------------------------------------------------------------- #
# errors
# --------------------------------------------------------------------------- #
class CoverageError(ValueError):
    """A coverage contract was violated. ``code`` is the machine discriminator."""

    code: str = "COVERAGE_ERROR"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        resolved = code or type(self).code
        super().__init__(f"{resolved}: {message}")
        self.code = resolved


class TapeIntegrityError(CoverageError):
    """A tape cannot be measured unambiguously (duplicate/out-of-order/off-grid)."""

    code = "TAPE_INTEGRITY"


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _ratio(numerator: int, denominator: int) -> Decimal:
    if denominator <= 0 or numerator <= 0:
        return Decimal(0)
    return (Decimal(numerator) / Decimal(denominator)).quantize(_Q6, rounding=ROUND_HALF_EVEN)


def _iso_ns(timestamp_ns: int) -> str:
    seconds, fraction = divmod(timestamp_ns, 1_000_000_000)
    moment = datetime.fromtimestamp(seconds, timezone.utc)
    return moment.strftime("%Y-%m-%dT%H:%M:%S") + f".{fraction:09d}Z"


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    return None


def _decimal_field(value: Any, *, name: str, context: str) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise TapeIntegrityError(
            f"{context}: {name} is not a decimal value ({value!r})", code="INVALID_RECORD"
        ) from exc
    if not parsed.is_finite():
        raise TapeIntegrityError(
            f"{context}: {name} is not finite ({value!r})", code="INVALID_RECORD"
        )
    return parsed


def _version_text(versions: set[str]) -> str:
    if not versions:
        return "UNKNOWN"
    if len(versions) == 1:
        return next(iter(versions))
    return "mixed:" + ",".join(sorted(versions))


# --------------------------------------------------------------------------- #
# report models
# --------------------------------------------------------------------------- #
class SymbolCoverage(BaseModel):
    """Measured coverage of one instrument across one supplied tape.

    Times are nanoseconds since the Unix epoch. ``first_ts``/``last_ts`` are the
    first and last *included* bar open instants; ``expected_bars`` is derived
    from that span and the caller's ``expected_interval_ns`` alone.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    symbol: str = Field(min_length=1)
    role: DatasetRole
    tape: str = Field(min_length=1)
    first_ts: int | None = Field(default=None, ge=0)
    last_ts: int | None = Field(default=None, ge=0)
    bar_count: int = Field(ge=0)
    expected_bars: int = Field(ge=0)
    missing_bar_ratio: Decimal = Field(ge=0, le=1)
    funding_rows: int = Field(ge=0)
    expected_funding_rows: int = Field(ge=0)
    mark_rows: int = Field(ge=0)
    expected_mark_rows: int = Field(ge=0)
    metadata_versions: Mapping[str, str] = Field(default_factory=dict)
    delisted_at: int | None = Field(default=None, ge=0)
    survivorship_risk: bool
    survivorship_reason: str | None = None
    rows_after_as_of: int = Field(default=0, ge=0)
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: DataStatus
    gaps: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _counts_are_consistent(self) -> Self:
        if (self.first_ts is None) != (self.last_ts is None):
            raise ValueError("first_ts and last_ts must both be present or both absent")
        if self.first_ts is not None and self.last_ts is not None:
            if self.first_ts > self.last_ts:
                raise ValueError("first_ts must not follow last_ts")
        if self.bar_count == 0 and self.expected_bars != 0:
            raise ValueError("a row without bars cannot expect bars")
        if self.expected_bars < self.bar_count:
            raise ValueError("expected_bars cannot be below the observed bar count")
        missing = self.expected_bars - self.bar_count
        if self.missing_bar_ratio != _ratio(missing, self.expected_bars):
            raise ValueError("missing_bar_ratio does not match the observed counts")
        if self.survivorship_risk and not (self.survivorship_reason or "").strip():
            raise ValueError("survivorship risk requires a named reason")
        if (
            self.delisted_at is not None
            and self.last_ts is not None
            and self.last_ts >= self.delisted_at
            and self.status is not DataStatus.BLOCKED
        ):
            raise ValueError("bars at/after delisting must be reported BLOCKED")
        return self


class RoleCoverage(BaseModel):
    """How many symbols the report carries per dataset role."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    role: DatasetRole
    symbols: int = Field(ge=0)


class CoverageAggregate(BaseModel):
    """Totals over every row. Ratios are recomputed and cross-checked."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    symbols: int = Field(ge=0)
    by_role: tuple[RoleCoverage, ...]
    bar_count: int = Field(ge=0)
    expected_bars: int = Field(ge=0)
    missing_bars: int = Field(ge=0)
    missing_bar_ratio: Decimal = Field(ge=0, le=1)
    funding_rows: int = Field(ge=0)
    expected_funding_rows: int = Field(ge=0)
    mark_rows: int = Field(ge=0)
    expected_mark_rows: int = Field(ge=0)
    rows_after_as_of: int = Field(default=0, ge=0)
    delisted_symbols: tuple[str, ...] = ()
    survivorship_risk: bool = False

    @model_validator(mode="after")
    def _aggregate_is_consistent(self) -> Self:
        if sum(row.symbols for row in self.by_role) != self.symbols:
            raise ValueError("role counts must sum to the symbol total")
        if self.expected_bars < self.bar_count:
            raise ValueError("aggregate expected_bars cannot be below bar_count")
        if self.missing_bars != self.expected_bars - self.bar_count:
            raise ValueError("missing_bars does not match the bar counts")
        if self.missing_bar_ratio != _ratio(self.missing_bars, self.expected_bars):
            raise ValueError("aggregate missing_bar_ratio does not match the counts")
        return self


class RegimeCount(BaseModel):
    """Bars labelled with one descriptive regime."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    regime: Regime
    bars: int = Field(ge=0)
    share: Decimal = Field(ge=0, le=1)


class RegimeReport(BaseModel):
    """Per-regime bar counts and an explicitly non-economic conclusion.

    ``economic_result`` is ``NOT_RUN`` unless a caller supplied outcomes from an
    actual economic evaluation; the validator refuses ``NO_EDGE``/``INCONCLUSIVE``
    while it is ``NOT_RUN``. ``PASS`` is not representable.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    method: str = REGIME_METHOD
    window_bars: int = Field(default=REGIME_WINDOW_BARS, ge=2)
    trend_threshold: Decimal = REGIME_TREND_THRESHOLD
    high_vol_variance: Decimal = REGIME_HIGH_VOL_VARIANCE
    warmup_bars: int = Field(ge=0)
    classified_bars: int = Field(ge=0)
    pooled_symbols: tuple[str, ...] = ()
    regimes: tuple[RegimeCount, ...]
    conclusion: RegimeConclusion
    conclusion_reason: str = Field(min_length=1)
    uncertainty: str = Field(min_length=1)
    economic_result: Literal["NOT_RUN", "PROVIDED"] = "NOT_RUN"

    @model_validator(mode="after")
    def _regime_block_is_consistent(self) -> Self:
        if tuple(count.regime for count in self.regimes) != REGIME_ORDER:
            raise ValueError("regime counts must carry every regime in canonical order")
        total = sum(count.bars for count in self.regimes)
        if total != self.classified_bars:
            raise ValueError("regime bar counts must sum to classified_bars")
        for count in self.regimes:
            if count.share != _ratio(count.bars, self.classified_bars):
                raise ValueError("regime share does not match its bar count")
        if self.economic_result == "NOT_RUN" and self.conclusion is not RegimeConclusion.NOT_RUN:
            raise ValueError("no economic run exists, so the regime conclusion cannot be decisive")
        if self.classified_bars == 0 and self.conclusion is not RegimeConclusion.NOT_RUN:
            raise ValueError("no classified bars means the regime conclusion is NOT_RUN")
        return self


class CoverageReport(BaseModel):
    """The published coverage document for one set of supplied tapes."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["coverage-report-v1"] = "coverage-report-v1"
    as_of_ns: int | None = Field(default=None, ge=0)
    expected_interval_ns: int = Field(gt=0)
    tapes: tuple[str, ...] = ()
    symbols: tuple[SymbolCoverage, ...]
    aggregate: CoverageAggregate
    data_status: DataStatus
    universe_status: UniverseStatus
    universe_reason: str = Field(min_length=1)
    altcoin_symbols: tuple[str, ...] = ()
    blockers: tuple[str, ...] = ()
    regimes: RegimeReport

    @model_validator(mode="after")
    def _report_is_consistent(self) -> Self:
        ordered = tuple(sorted(self.symbols, key=lambda row: (ROLE_INDEX[row.role], row.symbol)))
        if ordered != self.symbols:
            raise ValueError("symbol rows must be ordered by role then symbol")
        statuses = {row.status for row in self.symbols}
        if not self.symbols:
            expected = DataStatus.BLOCKED
        elif DataStatus.BLOCKED in statuses:
            expected = DataStatus.BLOCKED
        elif DataStatus.PARTIAL in statuses:
            expected = DataStatus.PARTIAL
        else:
            expected = DataStatus.AVAILABLE
        if self.data_status is not expected:
            raise ValueError("data_status does not follow the per-symbol statuses")
        if self.universe_status is UniverseStatus.NOT_RUN and not self.blockers:
            raise ValueError("a NOT_RUN universe requires a named blocker")
        return self

    def digest(self) -> str:
        """Domain-separated digest of this report (deterministic, no wall clock)."""
        return canonical_digest(SCHEMA_VERSION, self.model_dump(mode="json"))

    def to_dict(self) -> dict[str, Any]:
        payload = self.model_dump(mode="json")
        payload["report_digest"] = self.digest()
        return payload

    def to_json(self) -> str:
        """Canonical one-line JSON, byte-identical for identical inputs."""
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"), allow_nan=False)


# --------------------------------------------------------------------------- #
# tape scanning
# --------------------------------------------------------------------------- #
@dataclass
class _SymbolScan:
    """Streaming accumulation for one instrument inside one tape."""

    symbol: str
    bar_timestamps: list[int] = field(default_factory=list)
    bar_closes: list[Decimal] = field(default_factory=list)
    funding_rows: int = 0
    mark_rows: int = 0
    funding_intervals_ns: set[int] = field(default_factory=set)
    unclosed_bars: int = 0
    rows_after_as_of: int = 0


@dataclass
class _TapeScan:
    label: str
    sha256: str
    manifest_versions: dict[str, str]
    schema_versions: set[str]
    symbols: dict[str, _SymbolScan]


def _resolve_tape(path: Path) -> tuple[Path, Path | None]:
    if path.is_dir():
        tape = path / "tape.jsonl"
        if not tape.is_file():
            raise CoverageError(f"no tape.jsonl in {path}", code="TAPE_NOT_FOUND")
    elif path.is_file():
        tape = path
    else:
        raise CoverageError(f"tape path does not exist: {path}", code="TAPE_NOT_FOUND")
    for name in ("tape.manifest.json", "manifest.json"):
        candidate = tape.parent / name
        if candidate.is_file():
            return tape, candidate
    return tape, None


def _manifest_versions(manifest: Path | None) -> dict[str, str]:
    if manifest is None:
        return {}
    try:
        document = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CoverageError(
            f"unreadable tape manifest {manifest}: {exc}", code="MANIFEST_UNREADABLE"
        ) from exc
    if not isinstance(document, dict):
        raise CoverageError(f"tape manifest is not an object: {manifest}", code="MANIFEST_MALFORMED")
    version = document.get("schema_version")
    if not isinstance(version, str) or not version.strip():
        return {}
    return {"manifest": version.strip()}


def _scan_tape(label: str, path: Path, *, as_of_ns: int | None) -> _TapeScan:
    tape_path, manifest = _resolve_tape(path)
    symbols: dict[str, _SymbolScan] = {}
    schema_versions: set[str] = set()
    last_seen: dict[tuple[str, str], int] = {}

    with tape_path.open("r", encoding="utf-8") as handle:
        for line_number, raw in enumerate(handle, start=1):
            if not raw.strip():
                continue
            try:
                record = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise TapeIntegrityError(
                    f"{tape_path}:{line_number}: malformed JSON ({exc})", code="MALFORMED_ROW"
                ) from exc
            if not isinstance(record, dict):
                raise TapeIntegrityError(
                    f"{tape_path}:{line_number}: row is not an object", code="MALFORMED_ROW"
                )
            symbol_value = record.get("instrument")
            if not isinstance(symbol_value, str) or not symbol_value.strip():
                raise TapeIntegrityError(
                    f"{tape_path}:{line_number}: row carries no instrument",
                    code="MISSING_INSTRUMENT",
                )
            symbol = symbol_value.strip()
            payload_value = record.get("payload")
            payload: dict[str, Any] = payload_value if isinstance(payload_value, dict) else {}
            version = payload.get("schema_version") or record.get("schema_version")
            if isinstance(version, str) and version.strip():
                schema_versions.add(version.strip())

            channel = str(record.get("channel", "")).lower()
            scan = symbols.setdefault(symbol, _SymbolScan(symbol=symbol))
            context = f"{tape_path}:{line_number}:{symbol}:{channel}"

            if channel in KLINE_CHANNELS:
                open_ms = _as_int(payload.get("open_time_ms"))
                if open_ms is None:
                    open_ms = _as_int(record.get("event_time"))
                if open_ms is None:
                    raise TapeIntegrityError(
                        f"{context}: kline has no open timestamp", code="MISSING_TIMESTAMP"
                    )
                timestamp_ns = open_ms * _NS_PER_MS
                if as_of_ns is not None and timestamp_ns > as_of_ns:
                    scan.rows_after_as_of += 1
                    continue
                if payload.get("closed") is False:
                    scan.unclosed_bars += 1
                    continue
                _check_order(last_seen, symbol, "kline", timestamp_ns, context)
                scan.bar_timestamps.append(timestamp_ns)
                scan.bar_closes.append(
                    _decimal_field(payload.get("close"), name="close", context=context)
                )
            elif channel in FUNDING_CHANNELS:
                funding_ms = _as_int(payload.get("funding_time_ms"))
                if funding_ms is None:
                    funding_ms = _as_int(record.get("event_time"))
                if funding_ms is None:
                    raise TapeIntegrityError(
                        f"{context}: funding row has no settlement timestamp",
                        code="MISSING_TIMESTAMP",
                    )
                timestamp_ns = funding_ms * _NS_PER_MS
                if as_of_ns is not None and timestamp_ns > as_of_ns:
                    scan.rows_after_as_of += 1
                    continue
                _check_order(last_seen, symbol, "funding", timestamp_ns, context)
                scan.funding_rows += 1
                hours = payload.get("funding_interval_hours")
                if hours is not None:
                    interval = _decimal_field(
                        hours, name="funding_interval_hours", context=context
                    )
                    if interval <= 0:
                        raise TapeIntegrityError(
                            f"{context}: funding interval must be positive", code="INVALID_RECORD"
                        )
                    scan.funding_intervals_ns.add(int(interval * 3_600 * 1_000_000_000))
            elif channel in MARK_CHANNELS:
                mark_ms = _as_int(payload.get("mark_time_ms"))
                if mark_ms is None:
                    mark_ms = _as_int(record.get("event_time"))
                if mark_ms is None:
                    raise TapeIntegrityError(
                        f"{context}: mark row has no timestamp", code="MISSING_TIMESTAMP"
                    )
                timestamp_ns = mark_ms * _NS_PER_MS
                if as_of_ns is not None and timestamp_ns > as_of_ns:
                    scan.rows_after_as_of += 1
                    continue
                _check_order(last_seen, symbol, "mark", timestamp_ns, context)
                scan.mark_rows += 1

    return _TapeScan(
        label=label,
        sha256=sha256_file(tape_path),
        manifest_versions=_manifest_versions(manifest),
        schema_versions=schema_versions,
        symbols=symbols,
    )


def _check_order(
    last_seen: dict[tuple[str, str], int],
    symbol: str,
    channel: str,
    timestamp_ns: int,
    context: str,
) -> None:
    key = (symbol, channel)
    previous = last_seen.get(key)
    if previous is None:
        last_seen[key] = timestamp_ns
        return
    if timestamp_ns == previous:
        raise TapeIntegrityError(
            f"{context}: duplicate timestamp {_iso_ns(timestamp_ns)}", code="DUPLICATE_TIMESTAMP"
        )
    if timestamp_ns < previous:
        raise TapeIntegrityError(
            f"{context}: out-of-order timestamp {_iso_ns(timestamp_ns)} after {_iso_ns(previous)}",
            code="OUT_OF_ORDER_ROWS",
        )
    last_seen[key] = timestamp_ns


# --------------------------------------------------------------------------- #
# per-symbol coverage
# --------------------------------------------------------------------------- #
def _bar_gaps(timestamps: Sequence[int], *, interval_ns: int, symbol: str) -> list[str]:
    gaps: list[str] = []
    for previous, current in zip(timestamps, timestamps[1:], strict=False):
        delta = current - previous
        if delta % interval_ns != 0:
            raise TapeIntegrityError(
                f"{symbol}: bar {_iso_ns(current)} is off the {interval_ns}ns grid",
                code="NON_ALIGNED_TIMESTAMP",
            )
        missing = delta // interval_ns - 1
        if missing > 0:
            gaps.append(
                f"BARS:gap:after={_iso_ns(previous)}:missing={missing}:resume={_iso_ns(current)}"
            )
    return gaps


def _metadata_versions(
    tape_versions: Mapping[str, str], instrument_version: str | None
) -> dict[str, str]:
    versions = dict(tape_versions)
    if instrument_version is not None:
        versions["instrument"] = instrument_version
    return versions


def _build_symbol_row(
    scan: _SymbolScan,
    *,
    role: DatasetRole,
    tape_label: str,
    tape_sha256: str,
    tape_versions: Mapping[str, str],
    expected_interval_ns: int,
    as_of_ns: int | None,
    metadata_history: Sequence[InstrumentMetadata] | None,
) -> tuple[SymbolCoverage, list[str]]:
    """Return the row and the report-level blockers it contributes."""
    blockers: list[str] = []
    gaps: list[str] = []
    timestamps = scan.bar_timestamps
    gaps.extend(_bar_gaps(timestamps, interval_ns=expected_interval_ns, symbol=scan.symbol))

    instrument_version: str | None = None
    delisted_at: int | None = None
    survivorship_risk = True
    survivorship_reason: str | None = "NO_DELISTING_METADATA"
    if metadata_history:
        if as_of_ns is None:
            survivorship_reason = "AS_OF_REQUIRED_FOR_METADATA"
            gaps.append("METADATA:as_of_required")
        else:
            resolution = resolve_metadata(metadata_history, as_of_ns=as_of_ns)
            if resolution.status is ResolutionStatus.RESOLVED and resolution.record is not None:
                record = resolution.record
                instrument_version = record.metadata_version
                delisted_at = record.delisted_at
                survivorship_risk = False
                survivorship_reason = None
            else:
                if resolution.record is not None:
                    instrument_version = resolution.record.metadata_version
                gaps.append(f"METADATA:{resolution.status.value}")
                survivorship_reason = f"METADATA_{resolution.status.value}"

    if not timestamps:
        gaps.insert(0, "BARS:absent")
        status = DataStatus.BLOCKED
        blockers.append(f"SYMBOL:{scan.symbol}:NO_BARS")
        return (
            SymbolCoverage(
                symbol=scan.symbol,
                role=role,
                tape=tape_label,
                first_ts=None,
                last_ts=None,
                bar_count=0,
                expected_bars=0,
                missing_bar_ratio=Decimal(0),
                funding_rows=scan.funding_rows,
                expected_funding_rows=0,
                mark_rows=scan.mark_rows,
                expected_mark_rows=0,
                metadata_versions=_metadata_versions(tape_versions, instrument_version),
                delisted_at=delisted_at,
                survivorship_risk=survivorship_risk,
                survivorship_reason=survivorship_reason,
                rows_after_as_of=scan.rows_after_as_of,
                source_sha256=tape_sha256,
                status=status,
                gaps=tuple(gaps),
            ),
            blockers,
        )

    first_ts = timestamps[0]
    last_ts = timestamps[-1]
    expected_bars = (last_ts - first_ts) // expected_interval_ns + 1
    bar_count = len(timestamps)
    missing_bars = expected_bars - bar_count

    funding_interval_ns = DEFAULT_FUNDING_INTERVAL_NS
    if scan.funding_intervals_ns:
        funding_interval_ns = min(scan.funding_intervals_ns)
        if len(scan.funding_intervals_ns) > 1:
            gaps.append(
                "FUNDING:interval_conflict:"
                + ",".join(str(value) for value in sorted(scan.funding_intervals_ns))
            )
    else:
        gaps.append("FUNDING:interval_defaulted:8h")
    expected_funding_rows = (last_ts - first_ts) // funding_interval_ns + 1
    funding_rows = scan.funding_rows
    if funding_rows == 0:
        gaps.append(f"FUNDING:absent:expected={expected_funding_rows}")
    elif funding_rows < expected_funding_rows:
        gaps.append(
            f"FUNDING:missing={expected_funding_rows - funding_rows}"
            f":expected={expected_funding_rows}:observed={funding_rows}"
        )

    expected_mark_rows = bar_count
    mark_rows = scan.mark_rows
    if mark_rows == 0:
        gaps.append(f"MARK:absent:expected={expected_mark_rows}")
    elif mark_rows < expected_mark_rows:
        gaps.append(
            f"MARK:missing={expected_mark_rows - mark_rows}"
            f":expected={expected_mark_rows}:observed={mark_rows}"
        )

    if scan.unclosed_bars:
        gaps.append(f"BARS:unclosed={scan.unclosed_bars}")
    if scan.rows_after_as_of:
        gaps.append(f"ROWS:after_as_of={scan.rows_after_as_of}")

    bars_after_delisting = delisted_at is not None and last_ts >= delisted_at
    if bars_after_delisting:
        gaps.append(f"BARS:after_delisting:last={_iso_ns(last_ts)}")
        blockers.append(f"SYMBOL:{scan.symbol}:BARS_AFTER_DELISTING")
        return (
            SymbolCoverage(
                symbol=scan.symbol,
                role=role,
                tape=tape_label,
                first_ts=first_ts,
                last_ts=last_ts,
                bar_count=bar_count,
                expected_bars=expected_bars,
                missing_bar_ratio=_ratio(missing_bars, expected_bars),
                funding_rows=funding_rows,
                expected_funding_rows=expected_funding_rows,
                mark_rows=mark_rows,
                expected_mark_rows=expected_mark_rows,
                metadata_versions=_metadata_versions(tape_versions, instrument_version),
                delisted_at=delisted_at,
                survivorship_risk=survivorship_risk,
                survivorship_reason=survivorship_reason,
                rows_after_as_of=scan.rows_after_as_of,
                source_sha256=tape_sha256,
                status=DataStatus.BLOCKED,
                gaps=tuple(gaps),
            ),
            blockers,
        )

    incomplete = (
        missing_bars > 0
        or funding_rows != expected_funding_rows
        or mark_rows != expected_mark_rows
        or scan.unclosed_bars > 0
        or any(gap.startswith("METADATA:") for gap in gaps)
    )
    status = DataStatus.PARTIAL if incomplete else DataStatus.AVAILABLE
    return (
        SymbolCoverage(
            symbol=scan.symbol,
            role=role,
            tape=tape_label,
            first_ts=first_ts,
            last_ts=last_ts,
            bar_count=bar_count,
            expected_bars=expected_bars,
            missing_bar_ratio=_ratio(missing_bars, expected_bars),
            funding_rows=funding_rows,
            expected_funding_rows=expected_funding_rows,
            mark_rows=mark_rows,
            expected_mark_rows=expected_mark_rows,
            metadata_versions=_metadata_versions(tape_versions, instrument_version),
            delisted_at=delisted_at,
            survivorship_risk=survivorship_risk,
            survivorship_reason=survivorship_reason,
            rows_after_as_of=scan.rows_after_as_of,
            source_sha256=tape_sha256,
            status=status,
            gaps=tuple(gaps),
        ),
        blockers,
    )


# --------------------------------------------------------------------------- #
# regime classification (descriptive only)
# --------------------------------------------------------------------------- #
def _classify_closes(
    closes: Sequence[Decimal], *, window: int
) -> tuple[dict[Regime, int], int]:
    """Label each bar window with a descriptive regime; returns counts and warmup."""
    counts: dict[Regime, int] = {regime: 0 for regime in REGIME_ORDER}
    size = len(closes)
    if size <= window:
        return counts, size

    returns: list[Decimal] = [Decimal(0)] * size
    for index in range(1, size):
        returns[index] = closes[index] / closes[index - 1] - 1

    window_sum = sum(returns[1 : window + 1], Decimal(0))
    window_square_sum = sum((value * value for value in returns[1 : window + 1]), Decimal(0))
    for index in range(window, size):
        if index > window:
            arriving = returns[index]
            leaving = returns[index - window]
            window_sum += arriving - leaving
            window_square_sum += arriving * arriving - leaving * leaving
        mean = window_sum / window
        variance = window_square_sum / window - mean * mean
        if variance < 0:  # Decimal rounding at the 28-digit context boundary
            variance = Decimal(0)
        window_return = closes[index] / closes[index - window] - 1
        if variance >= REGIME_HIGH_VOL_VARIANCE:
            counts[Regime.HIGH_VOL] += 1
        elif window_return >= REGIME_TREND_THRESHOLD:
            counts[Regime.TREND_UP] += 1
        elif window_return <= -REGIME_TREND_THRESHOLD:
            counts[Regime.TREND_DOWN] += 1
        else:
            counts[Regime.RANGE] += 1
    return counts, window


def _regime_report(
    closes_by_symbol: Mapping[str, Sequence[Decimal]],
    *,
    outcomes: Mapping[Regime, RegimeConclusion] | None,
) -> RegimeReport:
    pooled: dict[Regime, int] = {regime: 0 for regime in REGIME_ORDER}
    warmup = 0
    pooled_symbols: list[str] = []
    for symbol in sorted(closes_by_symbol):
        closes = closes_by_symbol[symbol]
        if not closes:
            continue
        counts, symbol_warmup = _classify_closes(closes, window=REGIME_WINDOW_BARS)
        for regime, count in counts.items():
            pooled[regime] += count
        warmup += symbol_warmup
        pooled_symbols.append(symbol)

    classified = sum(pooled.values())
    regimes = tuple(
        RegimeCount(regime=regime, bars=pooled[regime], share=_ratio(pooled[regime], classified))
        for regime in REGIME_ORDER
    )
    if classified == 0:
        conclusion = RegimeConclusion.NOT_RUN
        reason = (
            "no classified bars: every supplied symbol has at most "
            f"{REGIME_WINDOW_BARS} bars, fewer than one regime window"
        )
    elif outcomes is None:
        conclusion = RegimeConclusion.NOT_RUN
        reason = (
            "no economic result exists for any regime: this block reports descriptive "
            "bar counts only"
        )
    else:
        supplied = [outcomes.get(regime, RegimeConclusion.NOT_RUN) for regime in REGIME_ORDER]
        if any(value is RegimeConclusion.NOT_RUN for value in supplied):
            conclusion = RegimeConclusion.NOT_RUN
            reason = "at least one regime has no economic result"
        elif any(value is RegimeConclusion.INCONCLUSIVE for value in supplied):
            conclusion = RegimeConclusion.INCONCLUSIVE
            reason = "at least one regime's economic result is inconclusive"
        else:
            conclusion = RegimeConclusion.NO_EDGE
            reason = "every regime carries a supplied NO_EDGE economic result"
    uncertainty = (
        f"descriptive counts over {classified} classified bars with a "
        f"{REGIME_WINDOW_BARS}-bar window; no confidence interval is attached and no "
        "economic outcome series was evaluated"
    )
    return RegimeReport(
        warmup_bars=warmup,
        classified_bars=classified,
        pooled_symbols=tuple(pooled_symbols),
        regimes=regimes,
        conclusion=conclusion,
        conclusion_reason=reason,
        uncertainty=uncertainty,
        economic_result="PROVIDED" if outcomes else "NOT_RUN",
    )


# --------------------------------------------------------------------------- #
# report builder
# --------------------------------------------------------------------------- #
def _role_for(symbol: str, roles: Mapping[str, DatasetRole]) -> DatasetRole:
    role = roles.get(symbol)
    if role is None:
        role = roles.get(ANY_SYMBOL)
    if role is None:
        raise CoverageError(
            f"no dataset role assigned for symbol {symbol!r} (use the symbol or {ANY_SYMBOL!r})",
            code="ROLE_NOT_ASSIGNED",
        )
    return role


def _coerce_roles(roles: Mapping[str, DatasetRole | str]) -> dict[str, DatasetRole]:
    coerced: dict[str, DatasetRole] = {}
    for key, value in roles.items():
        try:
            coerced[key] = value if isinstance(value, DatasetRole) else DatasetRole(value)
        except ValueError as exc:
            raise CoverageError(
                f"unknown dataset role {value!r} for {key!r}", code="UNKNOWN_ROLE"
            ) from exc
    return coerced


def _is_altcoin(symbol: str) -> bool:
    """BTC-class contracts are the development reference, not the altcoin universe."""
    return not symbol.upper().startswith("BTC")


def build_coverage_report(
    tape_paths: Mapping[str, Path],
    *,
    expected_interval_ns: int,
    roles: Mapping[str, DatasetRole | str],
    as_of_ns: int | None = None,
    metadata: Mapping[str, Sequence[InstrumentMetadata]] | None = None,
    regime_outcomes: Mapping[Regime, RegimeConclusion] | None = None,
) -> CoverageReport:
    """Measure every supplied tape and publish one deterministic coverage report.

    ``tape_paths`` maps a label to a tape directory or ``tape.jsonl`` path.
    ``roles`` maps a symbol (or ``"*"``) to the dataset role the caller is
    claiming for it; a symbol without a role is a caller contract violation and
    raises rather than being guessed. ``metadata`` carries point-in-time
    instrument records (the :class:`~v8_next.adapters.universe.InstrumentMetadata`
    contract) per symbol; nothing is inferred when it is absent.
    """
    if type(expected_interval_ns) is not int or expected_interval_ns <= 0:
        raise CoverageError("expected_interval_ns must be a positive int", code="INVALID_INTERVAL")
    if as_of_ns is not None and (type(as_of_ns) is not int or as_of_ns < 0):
        raise CoverageError("as_of_ns must be a non-negative int", code="INVALID_AS_OF")
    resolved_roles = _coerce_roles(roles)
    if regime_outcomes is not None and not regime_outcomes:
        regime_outcomes = None
    for regime, outcome in (regime_outcomes or {}).items():
        if not isinstance(outcome, RegimeConclusion):
            raise CoverageError(
                f"regime outcome for {regime} must be a RegimeConclusion", code="INVALID_OUTCOME"
            )

    rows: list[SymbolCoverage] = []
    blockers: list[str] = []
    seen_symbols: dict[str, str] = {}
    closes_by_symbol: dict[str, list[Decimal]] = {}
    tape_labels: list[str] = []

    for label, path in tape_paths.items():
        scan = _scan_tape(label, Path(path), as_of_ns=as_of_ns)
        tape_labels.append(label)
        tape_versions = dict(scan.manifest_versions)
        tape_versions["tape"] = _version_text(scan.schema_versions)
        if not scan.symbols:
            blockers.append(f"TAPE:{label}:NO_INSTRUMENTS")
            continue
        for symbol in sorted(scan.symbols):
            if symbol in seen_symbols:
                raise CoverageError(
                    f"symbol {symbol} appears in both {seen_symbols[symbol]!r} and {label!r}",
                    code="DUPLICATE_SYMBOL",
                )
            seen_symbols[symbol] = label
            role = _role_for(symbol, resolved_roles)
            row, row_blockers = _build_symbol_row(
                scan.symbols[symbol],
                role=role,
                tape_label=label,
                tape_sha256=scan.sha256,
                tape_versions=tape_versions,
                expected_interval_ns=expected_interval_ns,
                as_of_ns=as_of_ns,
                metadata_history=(metadata or {}).get(symbol),
            )
            rows.append(row)
            blockers.extend(row_blockers)
            closes_by_symbol[symbol] = list(scan.symbols[symbol].bar_closes)

    rows.sort(key=lambda row: (ROLE_INDEX[row.role], row.symbol))
    if not rows:
        blockers.append("NO_TAPE_SUPPLIED")

    by_role = tuple(
        RoleCoverage(role=role, symbols=sum(1 for row in rows if row.role is role))
        for role in ROLE_ORDER
    )
    aggregate = CoverageAggregate(
        symbols=len(rows),
        by_role=by_role,
        bar_count=sum(row.bar_count for row in rows),
        expected_bars=sum(row.expected_bars for row in rows),
        missing_bars=sum(row.expected_bars - row.bar_count for row in rows),
        missing_bar_ratio=_ratio(
            sum(row.expected_bars - row.bar_count for row in rows),
            sum(row.expected_bars for row in rows),
        ),
        funding_rows=sum(row.funding_rows for row in rows),
        expected_funding_rows=sum(row.expected_funding_rows for row in rows),
        mark_rows=sum(row.mark_rows for row in rows),
        expected_mark_rows=sum(row.expected_mark_rows for row in rows),
        rows_after_as_of=sum(row.rows_after_as_of for row in rows),
        delisted_symbols=tuple(sorted(row.symbol for row in rows if row.delisted_at is not None)),
        survivorship_risk=any(row.survivorship_risk for row in rows),
    )

    altcoin_symbols = tuple(sorted(row.symbol for row in rows if _is_altcoin(row.symbol)))
    if not altcoin_symbols:
        supplied = ", ".join(sorted(row.symbol for row in rows)) or "none"
        universe_status = UniverseStatus.NOT_RUN
        universe_reason = (
            "no altcoin tape is provisioned; supplied BTC-class symbols: " + supplied
        )
        blockers.append(
            f"{ECONOMIC_QUALIFICATION_BLOCKER}: universe_status=NOT_RUN; "
            f"only BTC-class tapes supplied ({supplied})"
        )
    else:
        universe_status = UniverseStatus.PROVISIONED
        universe_reason = (
            f"altcoin tape provisioned for {len(altcoin_symbols)} symbol(s): "
            + ", ".join(altcoin_symbols)
        )
        if aggregate.delisted_symbols and not any(
            _is_altcoin(symbol) for symbol in aggregate.delisted_symbols
        ):
            blockers.append("SURVIVORSHIP:NO_DELISTED_SYMBOL_IN_UNIVERSE")
        missing_metadata = sorted(
            row.symbol for row in rows if _is_altcoin(row.symbol) and row.survivorship_risk
        )
        if missing_metadata:
            blockers.append(
                "SURVIVORSHIP:NO_DELISTING_METADATA:" + ",".join(missing_metadata)
            )

    if not rows:
        data_status = DataStatus.BLOCKED
    elif any(row.status is DataStatus.BLOCKED for row in rows):
        data_status = DataStatus.BLOCKED
    elif any(row.status is DataStatus.PARTIAL for row in rows):
        data_status = DataStatus.PARTIAL
    else:
        data_status = DataStatus.AVAILABLE

    return CoverageReport(
        as_of_ns=as_of_ns,
        expected_interval_ns=expected_interval_ns,
        tapes=tuple(tape_labels),
        symbols=tuple(rows),
        aggregate=aggregate,
        data_status=data_status,
        universe_status=universe_status,
        universe_reason=universe_reason,
        altcoin_symbols=altcoin_symbols,
        blockers=tuple(blockers),
        regimes=_regime_report(closes_by_symbol, outcomes=regime_outcomes),
    )


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _parse_as_of(value: str) -> int:
    text = value.strip()
    if not text:
        raise argparse.ArgumentTypeError("--as-of cannot be blank")
    if text.isdigit():
        return int(text)
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"--as-of must be integer nanoseconds or ISO-8601, got {value!r}"
        ) from exc
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return int(moment.timestamp()) * 1_000_000_000


def _load_metadata(path: Path) -> dict[str, list[InstrumentMetadata]]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CoverageError(
            f"unreadable metadata file {path}: {exc}", code="METADATA_UNREADABLE"
        ) from exc
    if isinstance(document, dict):
        payload = document.get("instruments", document.get("symbols", []))
    else:
        payload = document
    if not isinstance(payload, list):
        raise CoverageError("metadata JSON must be a list of instrument records", code="METADATA_MALFORMED")
    grouped: dict[str, list[InstrumentMetadata]] = {}
    for entry in payload:
        try:
            record = InstrumentMetadata.model_validate(entry)
        except ValidationError as exc:
            raise CoverageError(
                f"invalid instrument metadata in {path}: {exc}", code="METADATA_MALFORMED"
            ) from exc
        grouped.setdefault(record.symbol, []).append(record)
    for history in grouped.values():
        history.sort(key=lambda record: (record.available_at, record.metadata_version))
    return grouped


def _tape_label(path: Path, index: int) -> str:
    label = path.name if path.is_dir() else path.stem
    return f"{label}" if label else f"tape-{index}"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="v8_next.evaluation.coverage",
        description=(
            "Measure point-in-time coverage of supplied tapes (M4-04). Reports only: "
            "nothing here evaluates a strategy."
        ),
    )
    parser.add_argument(
        "--tape",
        action="append",
        required=True,
        metavar="PATH",
        help="Tape directory or tape.jsonl (repeatable)",
    )
    parser.add_argument(
        "--interval-seconds",
        type=int,
        default=3600,
        help="Expected bar interval in seconds (default 3600)",
    )
    parser.add_argument("--as-of", type=_parse_as_of, default=None, help="PIT cutoff (ns or ISO-8601)")
    parser.add_argument(
        "--role",
        action="append",
        default=[],
        metavar="SYMBOL=DEVELOPMENT|VALIDATION",
        help="Dataset role override (repeatable); SEALED_OOS is never assignable here",
    )
    parser.add_argument(
        "--default-role",
        choices=("DEVELOPMENT", "VALIDATION"),
        default="DEVELOPMENT",
        help="Role for symbols without an override (default DEVELOPMENT)",
    )
    parser.add_argument("--metadata", default=None, help="Instrument-metadata JSON history")
    parser.add_argument("--out", default=None, help="Write the report JSON here")
    parser.add_argument("--json", action="store_true", help="Emit canonical one-line JSON")
    args = parser.parse_args(argv)

    roles: dict[str, DatasetRole | str] = {ANY_SYMBOL: DatasetRole(args.default_role)}
    for assignment in args.role:
        symbol, _, role_text = assignment.partition("=")
        if not symbol or not role_text:
            parser.error(f"--role expects SYMBOL=ROLE, got {assignment!r}")
        if role_text == DatasetRole.SEALED_OOS.value:
            parser.error(
                "--role cannot assign SEALED_OOS: no authorization is minted on this path"
            )
        try:
            roles[symbol] = DatasetRole(role_text)
        except ValueError:
            parser.error(f"unknown role {role_text!r} for {symbol!r}")

    tape_paths: dict[str, Path] = {}
    for index, raw in enumerate(args.tape):
        path = Path(raw)
        label = _tape_label(path, index)
        while label in tape_paths:
            label = f"{label}-{index}"
        tape_paths[label] = path

    metadata = _load_metadata(Path(args.metadata)) if args.metadata else None
    try:
        report = build_coverage_report(
            tape_paths,
            expected_interval_ns=args.interval_seconds * 1_000_000_000,
            roles=roles,
            as_of_ns=args.as_of,
            metadata=metadata,
        )
    except CoverageError as exc:
        json.dump(
            {"data_status": "BLOCKED", "error": {"code": exc.code, "detail": str(exc)}},
            sys.stdout,
            sort_keys=True,
        )
        sys.stdout.write("\n")
        return 2

    text = report.to_json() if args.json else json.dumps(report.to_dict(), indent=2, sort_keys=True)
    if args.out is not None:
        Path(args.out).write_text(text + "\n", encoding="utf-8")
    sys.stdout.write(text + "\n")
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
