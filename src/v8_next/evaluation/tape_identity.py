"""NX01 (#422) — four-year tape identity, burn map and Python data binding.

Read-only measurement over the real Binance UM tape. Nothing in this module
synthesizes market data, promotes a period to ``PROTECTED_OOS``, or substitutes
zero for an unmeasured quantity.

Two vocabulary levels are kept apart on purpose:

* ``store.DataRole`` is the *store's* three-valued role
  (``DEVELOPMENT`` / ``HOLDOUT`` / ``PROSPECTIVE``).
* ``TapeRole`` is what the recorded access history actually supports for a
  period of the tape (``BURNED_DIAGNOSTIC`` / ``USAGE_UNKNOWN`` /
  ``PROTECTED_OOS``).

``BURNED_DIAGNOSTIC`` and ``USAGE_UNKNOWN`` are **not** ``DataRole`` values.
The mapping into the store is explicit and one-way (``ROLE_TO_DATA_ROLE``):
a burned or unknown period can only ever be registered as ``DEVELOPMENT``
data; only a period whose preservation is *proven* may be registered as
``HOLDOUT``. Anything else fails closed.
"""

from __future__ import annotations

import hashlib
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from v8_next.evaluation.store import BurnRecord, ResearchStore

#: Tape-period role vocabulary. Distinct from ``store.DataRole``.
TapeRole = Literal["PROTECTED_OOS", "BURNED_DIAGNOSTIC", "USAGE_UNKNOWN"]

#: Explicit, one-way mapping from a tape role to what the store may record.
ROLE_TO_DATA_ROLE: dict[TapeRole, str] = {
    "PROTECTED_OOS": "HOLDOUT",
    "BURNED_DIAGNOSTIC": "DEVELOPMENT",
    "USAGE_UNKNOWN": "DEVELOPMENT",
}

#: Roles that may never be presented as a preserved final test.
NON_PROTECTED_TAPE_ROLES: tuple[TapeRole, ...] = ("BURNED_DIAGNOSTIC", "USAGE_UNKNOWN")

HOUR_MS = 3_600_000


def sha256_file(path: Path | str, *, chunk: int = 1 << 22) -> str:
    """Streaming sha256 of a file (never loads it whole)."""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(chunk), b""):
            h.update(block)
    return h.hexdigest()


def canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def iso_utc(ms: int) -> str:
    """UTC ISO-8601 rendering of a millisecond timestamp (no wall clock)."""
    import datetime

    return (
        datetime.datetime.fromtimestamp(ms / 1000, datetime.timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )


# --------------------------------------------------------------------------- #
# archive three-way verification (physical file / sidecar / registry)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class ArchiveCheck:
    """One archive verified three ways. ``OK`` only when all three agree."""

    symbol: str
    channel: str
    month: str
    zip_name: str
    zip_sha256: str
    sidecar_sha256: str | None
    registry_sha256: str | None
    zip_present: bool
    sidecar_present: bool

    @property
    def status(self) -> str:
        if not self.zip_present:
            return "ZIP_MISSING"
        if not self.sidecar_present:
            return "SIDECAR_MISSING"
        if self.registry_sha256 is None:
            return "REGISTRY_ENTRY_MISSING"
        if self.zip_sha256 != self.registry_sha256:
            return "ZIP_VS_REGISTRY_MISMATCH"
        if self.zip_sha256 != self.sidecar_sha256:
            return "ZIP_VS_SIDECAR_MISMATCH"
        return "OK"

    @property
    def three_way_ok(self) -> bool:
        return self.status == "OK"


def archive_zip_name(symbol: str, channel: str, month: str) -> str:
    """Archive name convention of the four-year dataset (as recorded)."""
    if channel == "kline":
        return f"{symbol}-1h-{month}.zip"
    return f"{symbol}-fundingRate-{month}.zip"


def _sidecar_digest(sidecar: Path) -> str | None:
    """``<sha256>  <filename>`` — the digest is the first field."""
    try:
        text = sidecar.read_text().strip()
    except OSError:
        return None
    first = text.split()
    return first[0] if first else None


def verify_archives(tape_dir: Path | str) -> tuple[ArchiveCheck, ...]:
    """Verify every archive three ways: physical zip, ``.CHECKSUM``, ``source.json``.

    Read-only. A missing or disagreeing artifact yields a failing ``status``;
    it is never skipped silently.
    """
    d = Path(tape_dir)
    source_path = d / "source.json"
    if not source_path.is_file():
        raise FileNotFoundError(f"archive registry not found: {source_path}")
    registry = json.loads(source_path.read_text())
    entries = registry.get("archives")
    if not isinstance(entries, list) or not entries:
        raise ValueError("archive registry carries no entries")

    checks: list[ArchiveCheck] = []
    for entry in entries:
        symbol = str(entry["symbol"])
        channel = str(entry["channel"])
        month = str(entry["month"])
        expected = str(entry["zip_sha256"])
        zip_name = archive_zip_name(symbol, channel, month)
        zpath = d / zip_name
        present = zpath.is_file()
        digest = sha256_file(zpath) if present else ""
        sidecar = d / f"{zip_name}.CHECKSUM"
        checks.append(
            ArchiveCheck(
                symbol=symbol,
                channel=channel,
                month=month,
                zip_name=zip_name,
                zip_sha256=digest,
                sidecar_sha256=_sidecar_digest(sidecar) if sidecar.is_file() else None,
                registry_sha256=expected,
                zip_present=present,
                sidecar_present=sidecar.is_file(),
            )
        )
    return tuple(checks)


def archive_set_digest(checks: tuple[ArchiveCheck, ...]) -> str:
    """Content digest over the verified archive set (sorted, wall-clock free)."""
    payload = [
        {"zip": c.zip_name, "sha256": c.zip_sha256, "status": c.status}
        for c in sorted(checks, key=lambda c: c.zip_name)
    ]
    return hashlib.sha256(canonical_json(payload).encode()).hexdigest()


# --------------------------------------------------------------------------- #
# read-only tape inventory
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class LegInventory:
    """Per-instrument coverage measured on the tape (never imputed)."""

    instrument: str
    kline_rows: int
    unique_slots: int
    duplicate_slots: int
    internal_gap_slots: int
    first_open_ms: int
    last_open_ms: int
    funding_rows: int
    funding_interval_counts: dict[str, int] = field(default_factory=dict)
    funding_interval_defaulted: int = 0

    @property
    def coverage_vs_own_grid(self) -> float:
        span_slots = (self.last_open_ms - self.first_open_ms) // HOUR_MS + 1
        if span_slots <= 0:
            return 0.0
        return self.unique_slots / span_slots

    def as_dict(self) -> dict[str, Any]:
        return {
            "instrument": self.instrument,
            "kline_rows": self.kline_rows,
            "unique_slots": self.unique_slots,
            "duplicate_slots": self.duplicate_slots,
            "internal_gap_slots": self.internal_gap_slots,
            "coverage_vs_own_grid": round(self.coverage_vs_own_grid, 6),
            "first_open_ms": self.first_open_ms,
            "first_open_utc": iso_utc(self.first_open_ms),
            "last_open_ms": self.last_open_ms,
            "last_open_utc": iso_utc(self.last_open_ms),
            "funding_rows": self.funding_rows,
            "funding_interval_counts": dict(sorted(self.funding_interval_counts.items())),
            "funding_interval_defaulted": self.funding_interval_defaulted,
        }


@dataclass(frozen=True)
class TapeInventory:
    """Identity + coverage of a physical tape file. Measurement, not a claim."""

    tape_path: str
    tape_sha256: str
    tape_bytes: int
    total_rows: int
    channel_counts: dict[str, int]
    symbols: tuple[str, ...]
    interval: str | None
    schema_versions: tuple[str, ...]
    legs: tuple[LegInventory, ...]
    mark_price_channels: tuple[str, ...]
    absence_notes: tuple[str, ...]
    unparsable_rows: int
    unclosed_kline_rows: int
    window_start_ms: int
    window_end_ms: int
    archive_checks: tuple[ArchiveCheck, ...] = ()
    registry_sha256: str | None = None

    @property
    def archive_total(self) -> int:
        return len(self.archive_checks)

    @property
    def archive_matches(self) -> int:
        return sum(1 for c in self.archive_checks if c.three_way_ok)

    @property
    def n_bars(self) -> int:
        return sum(leg.unique_slots for leg in self.legs)

    @property
    def duplicated_slots(self) -> int:
        return sum(leg.duplicate_slots for leg in self.legs)

    @property
    def internal_gap_slots(self) -> int:
        return sum(leg.internal_gap_slots for leg in self.legs)

    def funding_interval_counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for leg in self.legs:
            for key, count in leg.funding_interval_counts.items():
                out[key] = out.get(key, 0) + count
        return dict(sorted(out.items()))

    def as_dict(self) -> dict[str, Any]:
        return {
            "tape_path": self.tape_path,
            "tape_sha256": self.tape_sha256,
            "tape_bytes": self.tape_bytes,
            "total_rows": self.total_rows,
            "channel_counts": dict(sorted(self.channel_counts.items())),
            "symbols": list(self.symbols),
            "interval": self.interval,
            "schema_versions": list(self.schema_versions),
            "window_start_ms": self.window_start_ms,
            "window_start_utc": iso_utc(self.window_start_ms),
            "window_end_ms": self.window_end_ms,
            "window_end_utc": iso_utc(self.window_end_ms),
            "bars": self.n_bars,
            "duplicated_slots": self.duplicated_slots,
            "internal_gap_slots": self.internal_gap_slots,
            "unparsable_rows": self.unparsable_rows,
            "unclosed_kline_rows": self.unclosed_kline_rows,
            "funding_interval_counts": self.funding_interval_counts(),
            "mark_price_channels": list(self.mark_price_channels),
            "absence_notes": list(self.absence_notes),
            "archives_total": self.archive_total,
            "archives_three_way_ok": self.archive_matches,
            "archive_set_digest": archive_set_digest(self.archive_checks)
            if self.archive_checks
            else None,
            "registry_sha256": self.registry_sha256,
            "legs": [leg.as_dict() for leg in self.legs],
        }

    @property
    def data_id(self) -> str:
        """Content-addressed dataset identity (no wall clock)."""
        payload = {
            "tape_sha256": self.tape_sha256,
            "tape_bytes": self.tape_bytes,
            "schema_versions": list(self.schema_versions),
            "symbols": list(self.symbols),
            "interval": self.interval,
        }
        return "sha256:" + hashlib.sha256(canonical_json(payload).encode()).hexdigest()


def _interval_key(value: Any) -> str:
    return f"{float(value):g}h"


def inventory_tape(
    tape_path: Path | str,
    *,
    verify_archive_set: bool = False,
    archive_dir: Path | str | None = None,
) -> TapeInventory:
    """Measure a JSONL tape read-only: coverage, duplicates, gaps, funding, absence.

    One streaming pass. Duplicated and unclosed kline rows are *counted*, never
    dropped, so a caller can fail closed instead of silently losing rows.
    """
    p = Path(tape_path)
    if p.is_dir():
        p = p / "tape.jsonl"
    if not p.is_file():
        raise FileNotFoundError(f"tape not found: {p}")

    digest = sha256_file(p)
    size = p.stat().st_size

    slots: dict[str, dict[int, int]] = {}
    funding_per_leg: dict[str, int] = {}
    interval_counts: dict[str, dict[str, int]] = {}
    interval_defaulted: dict[str, int] = {}
    channel_counts: dict[str, int] = {}
    schema_versions: set[str] = set()
    channels_seen: set[str] = set()
    total = 0
    unparsable = 0
    unclosed = 0

    with open(p, "rb") as fh:
        for raw in fh:
            if not raw.strip():
                continue
            total += 1
            try:
                row = json.loads(raw)
            except (ValueError, UnicodeDecodeError):
                unparsable += 1
                continue
            channel = str(row.get("channel", ""))
            instrument = str(row.get("instrument", ""))
            channel_counts[channel] = channel_counts.get(channel, 0) + 1
            channels_seen.add(channel)
            payload = row.get("payload")
            if not isinstance(payload, dict):
                continue
            version = payload.get("schema_version")
            if version:
                schema_versions.add(str(version))
            if channel == "kline":
                if not payload.get("closed", False):
                    unclosed += 1
                    continue
                open_ms = int(payload["open_time_ms"])
                leg = slots.setdefault(instrument, {})
                leg[open_ms] = leg.get(open_ms, 0) + 1
            elif channel == "funding":
                funding_per_leg[instrument] = funding_per_leg.get(instrument, 0) + 1
                hours = payload.get("funding_interval_hours")
                if hours is None:
                    interval_defaulted[instrument] = interval_defaulted.get(instrument, 0) + 1
                else:
                    bucket = interval_counts.setdefault(instrument, {})
                    key = _interval_key(hours)
                    bucket[key] = bucket.get(key, 0) + 1

    if not slots:
        raise ValueError(f"no closed kline rows in {p}")

    legs: list[LegInventory] = []
    for instrument in sorted(slots):
        leg = slots[instrument]
        duplicates = sum(count - 1 for count in leg.values() if count > 1)
        first = min(leg)
        last = max(leg)
        span_slots = (last - first) // HOUR_MS + 1
        gaps = span_slots - len(leg)
        legs.append(
            LegInventory(
                instrument=instrument,
                kline_rows=sum(leg.values()),
                unique_slots=len(leg),
                duplicate_slots=duplicates,
                internal_gap_slots=gaps,
                first_open_ms=first,
                last_open_ms=last,
                funding_rows=funding_per_leg.get(instrument, 0),
                funding_interval_counts=interval_counts.get(instrument, {}),
                funding_interval_defaulted=interval_defaulted.get(instrument, 0),
            )
        )

    mark_channels = tuple(sorted(c for c in channels_seen if "mark" in c.lower()))
    absences: list[str] = []
    if not mark_channels:
        absences.append(
            "MARK_PRICE_ABSENT: no mark-price channel in the tape; funding mark is "
            "unknown, not zero"
        )
    window_start_ms = min(leg.first_open_ms for leg in legs)
    window_end_ms = max(leg.last_open_ms for leg in legs) + HOUR_MS - 1

    archive_checks: tuple[ArchiveCheck, ...] = ()
    registry_digest: str | None = None
    if verify_archive_set:
        target = Path(archive_dir) if archive_dir is not None else p.parent
        registry = target / "source.json"
        if registry.is_file():
            registry_digest = sha256_file(registry)
        archive_checks = verify_archives(target)

    interval = "1h" if (window_start_ms % HOUR_MS) == 0 else None
    return TapeInventory(
        tape_path=str(p),
        tape_sha256=digest,
        tape_bytes=size,
        total_rows=total,
        channel_counts=channel_counts,
        symbols=tuple(sorted(slots)),
        interval=interval,
        schema_versions=tuple(sorted(schema_versions)),
        legs=tuple(legs),
        mark_price_channels=mark_channels,
        absence_notes=tuple(absences),
        unparsable_rows=unparsable,
        unclosed_kline_rows=unclosed,
        window_start_ms=window_start_ms,
        window_end_ms=window_end_ms,
        archive_checks=archive_checks,
        registry_sha256=registry_digest,
    )


# --------------------------------------------------------------------------- #
# policy-lineage burn map (from recorded access, verified this session)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class RecordedAccess:
    """A recorded read of a tape, with the artifact that records it."""

    label: str
    evidence_file: str
    evidence_line: int
    evidence_contains: str
    slice_rows: int | None = None
    window_ms: tuple[int, int] | None = None

    def verify(self, repo_root: Path) -> tuple[bool, str]:
        """Re-read the citing artifact. Absence is a failure, not a skip."""
        p = repo_root / self.evidence_file
        if not p.is_file():
            return False, f"EVIDENCE_FILE_MISSING: {self.evidence_file}"
        lines = p.read_text(errors="replace").splitlines()
        if self.evidence_line < 1 or self.evidence_line > len(lines):
            return False, f"EVIDENCE_LINE_OUT_OF_RANGE: {self.evidence_file}:{self.evidence_line}"
        cited = lines[self.evidence_line - 1]
        if self.evidence_contains not in cited:
            return False, (
                f"EVIDENCE_TEXT_MISMATCH: {self.evidence_file}:{self.evidence_line} "
                f"does not contain {self.evidence_contains!r}"
            )
        return True, "OK"


@dataclass(frozen=True)
class BurnSegment:
    """A period of the tape with the role its recorded access supports."""

    segment_id: str
    role: TapeRole
    start_ms: int
    end_ms: int
    instruments: tuple[str, ...]
    evidence: tuple[str, ...]
    note: str = ""

    @property
    def data_role(self) -> str:
        return ROLE_TO_DATA_ROLE[self.role]

    @property
    def is_protected_final(self) -> bool:
        return self.role == "PROTECTED_OOS"

    def as_dict(self) -> dict[str, Any]:
        return {
            "segment_id": self.segment_id,
            "role": self.role,
            "data_role": self.data_role,
            "from": iso_utc(self.start_ms),
            "to": iso_utc(self.end_ms),
            "from_ms": self.start_ms,
            "to_ms": self.end_ms,
            "instruments": list(self.instruments),
            "evidence": list(self.evidence),
            "note": self.note,
            "protected_final": self.is_protected_final,
        }


def default_recorded_accesses() -> tuple[RecordedAccess, ...]:
    """The reads of the four-year tape recorded in this repository.

    Every entry is re-verified against the file it cites before it is used.
    """
    return (
        RecordedAccess(
            label="parity S0 slice",
            evidence_file="tests/parity/test_parity_s0.py",
            evidence_line=139,
            evidence_contains='load_real_tape("multi-1h-4y", limit=25_000)',
            slice_rows=25_000,
        ),
        RecordedAccess(
            label="parity S1 slice",
            evidence_file="tests/parity/test_parity_s1.py",
            evidence_line=198,
            evidence_contains='load_real_tape("multi-1h-4y", limit=20_000)',
            slice_rows=20_000,
        ),
        RecordedAccess(
            label="parity S4 real-tape slice",
            evidence_file="tests/parity/test_parity_s4_realtape.py",
            evidence_line=144,
            evidence_contains='load_real_tape("multi-1h-4y", limit=_TAPE_LIMIT)',
            slice_rows=2_000,
        ),
    )


def _slice_window(
    tape_path: Path, rows: int, *, chunk: int = 4096
) -> tuple[int, int]:
    """Wall-free measurement: the first/last kline open time inside the first
    ``rows`` lines of the tape in file order."""
    first: int | None = None
    last: int | None = None
    seen = 0
    with open(tape_path, "rb") as fh:
        for raw in fh:
            if not raw.strip():
                continue
            seen += 1
            if seen > rows:
                break
            try:
                row = json.loads(raw)
            except ValueError:
                continue
            if row.get("channel") != "kline":
                continue
            payload = row.get("payload") or {}
            open_ms = payload.get("open_time_ms")
            if open_ms is None:
                continue
            open_ms = int(open_ms)
            first = open_ms if first is None else min(first, open_ms)
            last = open_ms if last is None else max(last, open_ms)
    if first is None or last is None:
        raise ValueError(f"no kline rows inside the first {rows} lines of {tape_path}")
    return first, last + HOUR_MS - 1


def policy_lineage_burn_table(
    *,
    repo_root: Path | str,
    tape_path: Path | str,
    recorded_accesses: tuple[RecordedAccess, ...] | None = None,
) -> tuple[tuple[RecordedAccess, tuple[int, int]], ...]:
    """Verify each recorded access and measure its real slice boundary.

    Returns ``(access, measured_window_ms)`` pairs. A cited artifact that does
    not exist, or a slice whose boundary cannot be measured, raises: an
    unverifiable burn claim is never accepted as a burn claim.
    """
    root = Path(repo_root)
    p = Path(tape_path)
    accesses = recorded_accesses if recorded_accesses is not None else default_recorded_accesses()
    verified: list[tuple[RecordedAccess, tuple[int, int]]] = []
    failures: list[str] = []
    for access in accesses:
        ok, reason = access.verify(root)
        if not ok:
            failures.append(f"{access.label}: {reason}")
            continue
        if access.slice_rows is None:
            raise ValueError(f"{access.label}: a verified access must carry slice_rows")
        verified.append((access, _slice_window(p, access.slice_rows)))
    if failures:
        raise ValueError("unverifiable recorded access: " + "; ".join(failures))
    if not verified:
        raise ValueError("no recorded access verified")
    return tuple(verified)


def burn_segments(
    *,
    inventory: TapeInventory,
    verified_accesses: tuple[tuple[RecordedAccess, tuple[int, int]], ...],
    tail_burned_from_ms: int | None,
    tail_evidence: tuple[str, ...] = (),
) -> tuple[BurnSegment, ...]:
    """Build the burn table from measured boundaries only.

    ``tail_burned_from_ms`` must be *measured* by the caller (see
    ``measured_tail_burn_start``); passing ``None`` means the tail could not be
    shown to be burned, and the segment is then recorded as ``USAGE_UNKNOWN``,
    never silently as burned.
    """
    segments: list[BurnSegment] = []
    for access, window in verified_accesses:
        segments.append(
            BurnSegment(
                segment_id=f"ACCESS_{access.label.replace(' ', '_').upper()}",
                role="BURNED_DIAGNOSTIC",
                start_ms=window[0],
                end_ms=window[1],
                instruments=inventory.symbols,
                evidence=(
                    f"{access.evidence_file}:{access.evidence_line} — {access.evidence_contains}",
                    f"measured: first {access.slice_rows} tape lines span "
                    f"{iso_utc(window[0])} .. {iso_utc(window[1])}",
                ),
                note="read by a recorded parity run; diagnostic only, never a final test",
            )
        )
    head_end = max((w[1] for _, w in verified_accesses), default=inventory.window_start_ms - 1)

    if tail_burned_from_ms is None:
        segments.append(
            BurnSegment(
                segment_id="TAIL_UNPROVEN_UNKNOWN",
                role="USAGE_UNKNOWN",
                start_ms=head_end + 1,
                end_ms=inventory.window_end_ms,
                instruments=inventory.symbols,
                evidence=("no tail-burn measurement was produced for this run",),
                note="USAGE_UNKNOWN is not preserved OOS and is not a burned segment",
            )
        )
    else:
        if tail_burned_from_ms <= head_end:
            raise ValueError("tail burn start must follow the burned head slice")
        segments.append(
            BurnSegment(
                segment_id="DEVELOPMENT_RESIDUE_UNKNOWN",
                role="USAGE_UNKNOWN",
                start_ms=head_end + 1,
                end_ms=tail_burned_from_ms - 1,
                instruments=inventory.symbols,
                evidence=("no recorded access found for this interval",),
                note="residue after the burned head slice",
            )
        )
        segments.append(
            BurnSegment(
                segment_id="TAIL_BURNED",
                role="BURNED_DIAGNOSTIC",
                start_ms=tail_burned_from_ms,
                end_ms=inventory.window_end_ms,
                instruments=inventory.symbols,
                evidence=tail_evidence,
                note="consumed before this work; may host diagnostics, never a final test",
            )
        )
    return tuple(segments)


@dataclass(frozen=True)
class CoveredTape:
    """A physical tape measured against a window: how much of it a window holds."""

    path: str
    sha256: str
    instruments: tuple[str, ...]
    window_start_ms: int
    window_end_ms: int

    def covers(self, start_ms: int, end_ms: int) -> bool:
        return self.window_start_ms >= start_ms and self.window_end_ms <= end_ms


def measure_covering_tape(path: Path | str) -> CoveredTape:
    """Measure a small physical tape's instruments and window (read-only)."""
    p = Path(path)
    if p.is_dir():
        p = p / "tape.jsonl"
    inv = inventory_tape(p)
    return CoveredTape(
        path=str(p),
        sha256=inv.tape_sha256,
        instruments=inv.symbols,
        window_start_ms=inv.window_start_ms,
        window_end_ms=inv.window_end_ms,
    )


# --------------------------------------------------------------------------- #
# 24/12/12 calendar bound to real UTC dates
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Fold:
    fold_id: str
    role: Literal["DEVELOPMENT", "DIAGNOSTIC_FOLD", "FINAL"]
    start_ms: int
    end_ms: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "fold_id": self.fold_id,
            "role": self.role,
            "from": iso_utc(self.start_ms),
            "to": iso_utc(self.end_ms),
            "from_ms": self.start_ms,
            "to_ms": self.end_ms,
        }


@dataclass(frozen=True)
class SwingCalendar:
    dev_start_ms: int
    dev_end_ms: int
    development: Fold
    folds: tuple[Fold, ...]
    final: Fold
    final_eligible: bool
    final_ineligibility_reason: str | None
    burn_segments: tuple[BurnSegment, ...]

    @property
    def no_protected_final(self) -> bool:
        return not self.final_eligible

    def as_dict(self) -> dict[str, Any]:
        return {
            "development": self.development.as_dict(),
            "folds": [f.as_dict() for f in self.folds],
            "final": self.final.as_dict(),
            "final_eligible": self.final_eligible,
            "final_ineligibility_reason": self.final_ineligibility_reason,
            "no_protected_final": self.no_protected_final,
            "burn_segments": [s.as_dict() for s in self.burn_segments],
        }


def _add_months(ms: int, months: int) -> int:
    """Calendar-month arithmetic in UTC (no fixed-day shortcut)."""
    import datetime

    dt = datetime.datetime.fromtimestamp(ms / 1000, datetime.timezone.utc)
    month_index = dt.month - 1 + months
    year = dt.year + month_index // 12
    month = month_index % 12 + 1
    return int(dt.replace(year=year, month=month).timestamp() * 1000)


def swing_calendar(
    *,
    inventory: TapeInventory,
    segments: tuple[BurnSegment, ...],
    development_months: int = 24,
    diagnostic_folds: int = 4,
    final_months: int = 12,
) -> SwingCalendar:
    """Bind the 24/12/12 walk-forward calendar to real UTC dates.

    The final window is eligible only when the whole window lies inside a
    segment whose role is ``PROTECTED_OOS``. A burned or unknown final window
    yields ``final_eligible=False`` and a named reason — it is never silently
    treated as eligible, and no role is edited to make it eligible.
    """
    start = inventory.window_start_ms
    dev_end = _add_months(start, development_months)
    fold_months = 12 // diagnostic_folds
    if fold_months * diagnostic_folds != 12:
        raise ValueError("diagnostic fold layout must split 12 months evenly")
    folds: list[Fold] = []
    cursor = dev_end
    for i in range(diagnostic_folds):
        nxt = _add_months(cursor, fold_months)
        folds.append(
            Fold(fold_id=f"FOLD_{i + 1}", role="DIAGNOSTIC_FOLD", start_ms=cursor, end_ms=nxt - 1)
        )
        cursor = nxt
    final_end = _add_months(cursor, final_months)
    final = Fold(fold_id="FINAL", role="FINAL", start_ms=cursor, end_ms=final_end - 1)

    protected = [s for s in segments if s.role == "PROTECTED_OOS"]
    covered = any(s.start_ms <= final.start_ms and final.end_ms <= s.end_ms for s in protected)
    blockers = [
        s.segment_id
        for s in segments
        if s.role in NON_PROTECTED_TAPE_ROLES
        and s.start_ms <= final.end_ms
        and final.start_ms <= s.end_ms
    ]
    eligible = covered and not blockers
    reason: str | None = None
    if not eligible:
        if blockers:
            reason = (
                "NO_PROTECTED_FINAL: the final window overlaps "
                + ", ".join(sorted(blockers))
                + " (burned or usage-unknown); the final is not opened"
            )
        else:
            reason = "NO_PROTECTED_FINAL: no PROVEN_OOS segment covers the final window"
    return SwingCalendar(
        dev_start_ms=start,
        dev_end_ms=dev_end - 1,
        development=Fold(
            fold_id="DEVELOPMENT", role="DEVELOPMENT", start_ms=start, end_ms=dev_end - 1
        ),
        folds=tuple(folds),
        final=final,
        final_eligible=eligible,
        final_ineligibility_reason=reason,
        burn_segments=segments,
    )


# --------------------------------------------------------------------------- #
# manifest bound to the store and to catalog consumers
# --------------------------------------------------------------------------- #
def runtime_identity() -> dict[str, str]:
    """Runtime identity of the measuring process (no wall clock)."""
    from importlib.metadata import PackageNotFoundError, version

    def _v(name: str) -> str:
        try:
            return version(name)
        except PackageNotFoundError:  # pragma: no cover - declared dependency
            return "ABSENT"

    return {
        "python": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
        "nautilus_trader": _v("nautilus-trader"),
        "polars": _v("polars"),
        "numpy": _v("numpy"),
        "pydantic": _v("pydantic"),
    }


@dataclass(frozen=True)
class TapeManifest:
    """Revision-bound identity of a tape plus the roles measured on it."""

    manifest_version: str
    data_id: str
    tape_sha256: str
    tape_bytes: int
    source_registry_sha256: str | None
    archive_set_digest: str | None
    symbols: tuple[str, ...]
    interval: str | None
    schema_versions: tuple[str, ...]
    window_start_ms: int
    window_end_ms: int
    role_map: dict[str, str]
    final_eligible: bool
    runtime: dict[str, str]
    config_identity: str

    def identity_digest(self) -> str:
        """Content digest. Wall-clock free so identical inputs re-derive it."""
        payload = {
            "manifest_version": self.manifest_version,
            "data_id": self.data_id,
            "tape_sha256": self.tape_sha256,
            "tape_bytes": self.tape_bytes,
            "source_registry_sha256": self.source_registry_sha256,
            "archive_set_digest": self.archive_set_digest,
            "symbols": list(self.symbols),
            "interval": self.interval,
            "schema_versions": list(self.schema_versions),
            "window_start_ms": self.window_start_ms,
            "window_end_ms": self.window_end_ms,
            "role_map": self.role_map,
            "final_eligible": self.final_eligible,
            "runtime": self.runtime,
            "config_identity": self.config_identity,
        }
        return "sha256:" + hashlib.sha256(canonical_json(payload).encode()).hexdigest()

    def as_dict(self) -> dict[str, Any]:
        out = dict(self.__dict__)
        out["symbols"] = list(self.symbols)
        out["schema_versions"] = list(self.schema_versions)
        out["identity_digest"] = self.identity_digest()
        return out


def build_manifest(
    *,
    inventory: TapeInventory,
    calendar: SwingCalendar,
    config_identity: str,
    manifest_version: str = "v87-nx01-manifest-v1",
) -> TapeManifest:
    role_map: dict[str, str] = {
        s.segment_id: str(s.role) for s in calendar.burn_segments
    }
    return TapeManifest(
        manifest_version=manifest_version,
        data_id=inventory.data_id,
        tape_sha256=inventory.tape_sha256,
        tape_bytes=inventory.tape_bytes,
        source_registry_sha256=inventory.registry_sha256,
        archive_set_digest=archive_set_digest(inventory.archive_checks)
        if inventory.archive_checks
        else None,
        symbols=inventory.symbols,
        interval=inventory.interval,
        schema_versions=inventory.schema_versions,
        window_start_ms=inventory.window_start_ms,
        window_end_ms=inventory.window_end_ms,
        role_map=role_map,
        final_eligible=calendar.final_eligible,
        runtime=runtime_identity(),
        config_identity=config_identity,
    )


def verify_manifest_against_file(
    manifest: TapeManifest, tape_path: Path | str
) -> tuple[bool, str]:
    """Re-hash the physical tape and compare. Any disagreement fails closed."""
    p = Path(tape_path)
    if p.is_dir():
        p = p / "tape.jsonl"
    if not p.is_file():
        return False, f"TAPE_MISSING: {p}"
    size = p.stat().st_size
    digest = sha256_file(p)
    if digest != manifest.tape_sha256:
        return False, f"TAPE_HASH_MISMATCH: manifest {manifest.tape_sha256}, file {digest}"
    if size != manifest.tape_bytes:
        return False, f"TAPE_SIZE_MISMATCH: manifest {manifest.tape_bytes}, file {size}"
    return True, "OK"


def bind_manifest_to_store(
    manifest: TapeManifest,
    store: ResearchStore,
    *,
    calendar: SwingCalendar,
    lineage: str,
) -> dict[str, Any]:
    """Register the manifest's dataset window and burn records in the store.

    Fail-closed rules:
    * a burned/unknown period is only ever registered as ``DEVELOPMENT``;
    * a burned period registered under a different dataset hash for the same
      lineage is refused rather than overwritten.
    """
    for segment in calendar.burn_segments:
        if segment.role not in NON_PROTECTED_TAPE_ROLES:
            continue
        if segment.data_role != "DEVELOPMENT":
            raise ValueError(
                f"{segment.segment_id}: {segment.role} may only map to DEVELOPMENT, "
                f"got {segment.data_role}"
            )
    existing = {b.dataset_hash for b in store.list_burns(lineage)}
    conflicts = sorted(h for h in existing if h != manifest.data_id)
    if conflicts:
        raise ValueError(
            f"lineage {lineage!r} already carries burned dataset hashes {conflicts}; "
            f"refusing to re-register {manifest.data_id}"
        )

    burn_segments = [
        segment for segment in calendar.burn_segments if segment.role == "BURNED_DIAGNOSTIC"
    ]
    registered: list[BurnRecord] = []
    if burn_segments:
        # A dataset is burned once for the declared lineage. Keeping the
        # segment id here made the conflict lookup (`list_burns(lineage)`) and
        # the holdout join (`burns.lineage = trials.family`) miss the record.
        record = BurnRecord(
            lineage=lineage,
            dataset_hash=manifest.data_id,
            burned_ns=min(segment.start_ms for segment in burn_segments) * 1_000_000,
        )
        store.burn_holdout(record)
        registered.append(record)
    return {
        "dataset_hash": manifest.data_id,
        "identity_digest": manifest.identity_digest(),
        "burns_registered": [r.lineage for r in registered],
        "burn_segments": [segment.segment_id for segment in burn_segments],
        "final_eligible": manifest.final_eligible,
    }


def readback_calendar(calendar: SwingCalendar, path: Path | str) -> SwingCalendar:
    """Write the calendar to disk and read it back; return the re-read object.

    The round trip is the read-back check: a downstream consumer (NX03/NX05)
    gets the same windows only if the serialized form reproduces them.
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = calendar.as_dict()
    target.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    return calendar_from_dict(json.loads(target.read_text()))


def calendar_from_dict(payload: dict[str, Any]) -> SwingCalendar:
    def fold(node: dict[str, Any]) -> Fold:
        return Fold(
            fold_id=str(node["fold_id"]),
            role=node["role"],
            start_ms=int(node["from_ms"]),
            end_ms=int(node["to_ms"]),
        )

    def segment(node: dict[str, Any]) -> BurnSegment:
        return BurnSegment(
            segment_id=str(node["segment_id"]),
            role=node["role"],
            start_ms=int(node["from_ms"]),
            end_ms=int(node["to_ms"]),
            instruments=tuple(node["instruments"]),
            evidence=tuple(node["evidence"]),
            note=str(node.get("note", "")),
        )

    return SwingCalendar(
        dev_start_ms=int(payload["development"]["from_ms"]),
        dev_end_ms=int(payload["development"]["to_ms"]),
        development=fold(payload["development"]),
        folds=tuple(fold(f) for f in payload["folds"]),
        final=fold(payload["final"]),
        final_eligible=bool(payload["final_eligible"]),
        final_ineligibility_reason=payload["final_ineligibility_reason"],
        burn_segments=tuple(segment(s) for s in payload["burn_segments"]),
    )
