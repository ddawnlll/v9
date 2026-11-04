"""Multi-asset tape loading: real venue klines, funding rows, quote volumes.

Reads the multi-asset tape.jsonl directly (channel/instrument/payload records
with per-record hashes). Nothing is synthesized: instruments, spans and funding
rows are whatever the file contains. Single-asset BTC loading stays in
gate_resolution.load_tape_candles; this module owns the multi-asset path.

Data integrity is checked *before* the chronological intersection (NX01.R3):
duplicate slots in a leg are a hard error, and a leg that would lose bars to the
intersection is reported per instrument instead of being trimmed silently. A
missing mark price or funding interval is recorded as an absence (NX01.R4), never
imputed as zero or as the 8h venue convention.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import polars as pl

from v8_next.domain.market import Candle

INSTRUMENT_SUFFIX = "-PERP.BINANCE"

#: Frozen oracle name (default read path; never silently replaced).
JSONL_TAPE_NAME = "tape.jsonl"
#: Columnar runtime path (flag-selected only; cutover gated on green parity).
PARQUET_TAPE_NAME = "tape.parquet"

#: Venue convention. Only ever a *reported* default, never a measurement.
DEFAULT_FUNDING_INTERVAL_HOURS = 8.0


@dataclass(frozen=True, slots=True)
class FundingRow:
    instrument: str  # e.g. BTCUSDT
    #: Venue settlement instant (the economic boundary of the funding charge).
    funding_time_ms: int
    funding_rate: Decimal
    interval_hours: float
    #: Local receipt instant, when the capture recorded one. None means the
    #: tape cannot say when the record became knowable, so no receipt-time
    #: attribution is claimed for it.
    received_ns: int | None = None
    #: True when the record carried no interval key and the venue convention
    #: was applied to the engine feed. The assumption is reported, not hidden.
    interval_defaulted: bool = False


@dataclass(frozen=True)
class LegCoverage:
    """Per-instrument coverage measured before the intersection."""

    instrument: str
    bars: int
    duplicate_slots: int
    missing_vs_union: int

    def as_dict(self) -> dict[str, int | str]:
        return {
            "instrument": self.instrument,
            "bars": self.bars,
            "duplicate_slots": self.duplicate_slots,
            "missing_vs_union": self.missing_vs_union,
        }


@dataclass(frozen=True)
class MultiTape:
    instruments: tuple[str, ...]  # e.g. ("BTCUSDT", ...)
    candles: dict[str, tuple[Candle, ...]]
    quote_volumes: dict[str, tuple[float, ...]]  # per-bar quote volume, same order
    funding: tuple[FundingRow, ...]
    tape_path: str
    tape_sha256: str
    # Malformed funding records skipped at load (never silently free).
    # Consumers must surface this alongside funding cost, not ignore it.
    funding_dropped: int = 0
    funding_raw_count: int = 0
    coverage: dict[str, LegCoverage] = field(default_factory=dict)
    #: Bars the chronological intersection removed from a leg, per instrument.
    #: Empty when every leg shares one grid (the normal case).
    intersection_dropped: dict[str, int] = field(default_factory=dict)
    funding_interval_counts: dict[str, int] = field(default_factory=dict)
    funding_interval_defaulted: int = 0
    mark_price_absent: bool = True
    absence_notes: tuple[str, ...] = ()

    @property
    def n_bars(self) -> int:
        return min(len(v) for v in self.candles.values()) if self.candles else 0

    @property
    def funding_interval_hours_present(self) -> bool:
        """True only when every funding row carried a measured interval."""
        return not self.funding_interval_defaulted


def funding_interval_hours(payload: dict[str, Any]) -> float | None:
    """Extract the funding interval, accepting known key variants.

    Returns ``None`` when the record carries no interval key at all. The caller
    decides what the engine convention is and must report that it applied — a
    fixed 8h assumption is never returned as if it were a measurement.
    """
    for key in ("funding_interval_hours", "fundingIntervalHours", "interval_hours"):
        value = payload.get(key)
        if value is not None:
            return float(value)
    return None


def _interval_key(value: float) -> str:
    return f"{value:g}h"


def _file_sha(p: Path) -> str:
    # Metadata can be preserved when content changes; never cache evidence hashes
    # solely by file identity, size and timestamps.
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def load_multitape(
    tape_path: Path | str,
    limit: int | None = None,
    offset: int = 0,
    *,
    strict_intersection: bool = True,
    start_ms: int | None = None,
    end_ms: int | None = None,
    tape_format: str = "jsonl",
    parquet_path: Path | str | None = None,
) -> MultiTape:
    """Load every instrument in the tape with aligned chronological bars.

    offset skips that many leading bars per leg (frozen-OOS windowing); limit
    caps the leg length after the offset.

    ``start_ms``/``end_ms`` select a half-open UTC window on the bar open time for
    the kline legs and on the funding time for the funding rows. The read is a lazy
    scan, so a bounded window over a large tape stays bounded (NX05.R3); it applies
    before ``offset``/``limit``.

    ``tape_format`` selects the runtime read path (``"jsonl"`` default, the frozen
    parity oracle; ``"parquet"`` flag-selected columnar path). The parquet path
    never silently substitutes: a missing parquet file or schema mismatch fails
    loudly, and cutover of the default happens ONLY on green parity (R3). No
    Decimal-type change is smuggled here: values are decoded exactly as on the
    JSONL path (``Decimal(str(...))``).

    Raises ``ValueError`` when a leg carries duplicated bar slots, or (with
    ``strict_intersection``, the default) when the chronological intersection
    would drop bars from any leg. Set ``strict_intersection=False`` only to
    inspect a heterogeneous tape; the dropped counts are then reported on the
    returned ``MultiTape.intersection_dropped``.
    """
    if tape_format not in ("jsonl", "parquet"):
        raise ValueError(f"unknown tape_format {tape_format!r}; expected 'jsonl' or 'parquet'")
    p = Path(tape_path)
    if tape_format == "parquet":
        # Flag-selected columnar path: explicit file, never a silent fallback.
        if parquet_path is not None:
            pq = Path(parquet_path)
        elif p.suffix == ".parquet" and p.is_file():
            pq = p
        elif p.is_dir() and (p / PARQUET_TAPE_NAME).is_file():
            pq = p / PARQUET_TAPE_NAME
        else:
            raise FileNotFoundError(
                f"parquet tape not found for {p} (pass parquet_path explicitly); "
                "JSONL remains the frozen oracle until R3 cutover"
            )
        sha = _file_sha(pq)
        scan = pl.scan_parquet(pq)
        p = pq
    else:
        if p.is_dir():
            p = p / JSONL_TAPE_NAME
        if not p.is_file():
            raise FileNotFoundError(f"Multi tape not found at {p}")
        sha = _file_sha(p)
        scan = pl.scan_ndjson(p)
    if start_ms is not None:
        scan = scan.filter(
            (pl.col("channel") == "kline")
            & (pl.col("payload").struct.field("open_time_ms") >= start_ms)
            | (pl.col("channel") == "funding")
            & (pl.col("payload").struct.field("funding_time_ms") >= start_ms)
        )
    if end_ms is not None:
        scan = scan.filter(
            (pl.col("channel") == "kline")
            & (pl.col("payload").struct.field("open_time_ms") < end_ms)
            | (pl.col("channel") == "funding")
            & (pl.col("payload").struct.field("funding_time_ms") < end_ms)
        )
    df = scan.collect()
    if df.height == 0:
        raise ValueError(
            f"no rows for window [{start_ms},{end_ms}) in {p}"
        )
    # Explicit schema gate (both paths): struct-field doubt fails loudly,
    # never with defaulted values. Missing columns are a loud failure.
    for required in ("channel", "instrument", "event_time", "payload"):
        if required not in df.columns:
            raise ValueError(f"tape schema mismatch at {p}: missing column {required!r}")
    if not isinstance(df.schema["payload"], pl.Struct):
        raise ValueError(f"tape schema mismatch at {p}: 'payload' is not a Struct")

    kline = df.filter(pl.col("channel") == "kline").sort(["instrument", "event_time"])
    instruments: list[str] = sorted(kline["instrument"].unique().to_list())
    if not instruments:
        raise ValueError("no kline instruments in tape")
    # Per-leg end_ns -> (candle, quote_volume), then intersect on chronology.
    leg_maps: dict[str, dict[int, tuple[Candle, float]]] = {}
    duplicates: dict[str, int] = {}
    for inst in instruments:
        sub = kline.filter(pl.col("instrument") == inst)
        if offset:
            sub = sub.slice(offset)
        if limit is not None:
            sub = sub.head(limit)
        m: dict[int, tuple[Candle, float]] = {}
        dup = 0
        for row in sub["payload"].to_list():
            if not row.get("closed", True):
                continue
            c = Candle(
                instrument_id=f"{inst}{INSTRUMENT_SUFFIX}",
                start_ns=int(row["open_time_ms"]) * 1_000_000,
                end_ns=(int(row["close_time_ms"]) + 1) * 1_000_000,
                open=Decimal(str(row["open"])),
                high=Decimal(str(row["high"])),
                low=Decimal(str(row["low"])),
                close=Decimal(str(row["close"])),
                volume=Decimal(str(row["volume"])),
                received_ns=(int(row["close_time_ms"]) + 1) * 1_000_000,
                available_ns=(int(row["close_time_ms"]) + 1) * 1_000_000,
                source_hash=str(row["payload_hash"]),
            )
            if c.end_ns in m:
                # Integrity failure, not a silent overwrite: the later record
                # would otherwise win with no trace left behind.
                dup += 1
                continue
            m[c.end_ns] = (c, float(row.get("quote_asset_volume") or 0.0))
        if not m:
            raise ValueError(f"no closed bars for {inst}")
        if dup:
            raise ValueError(
                f"{inst}: {dup} duplicated bar slot(s) in the tape; refusing to "
                "drop rows silently"
            )
        duplicates[inst] = dup
        leg_maps[inst] = m

    grid_union = sorted(set().union(*[set(m) for m in leg_maps.values()]))
    coverage: dict[str, LegCoverage] = {}
    for inst, m in leg_maps.items():
        missing = len(grid_union) - len(m)
        coverage[inst] = LegCoverage(
            instrument=inst,
            bars=len(m),
            duplicate_slots=duplicates.get(inst, 0),
            missing_vs_union=missing,
        )
    missing_by_leg = {inst: c.missing_vs_union for inst, c in coverage.items()}
    if strict_intersection and any(missing_by_leg.values()):
        details = ", ".join(f"{k}={v}" for k, v in sorted(missing_by_leg.items()) if v)
        raise ValueError(
            "legs do not share one chronological grid; the intersection would drop "
            f"bars ({details})"
        )
    common_sorted = sorted(set.intersection(*[set(m) for m in leg_maps.values()]))
    dropped = {inst: len(m) - len(common_sorted) for inst, m in leg_maps.items()}
    if len(common_sorted) < 2:
        raise ValueError("no common chronological window across legs")
    candles = {inst: tuple(leg_maps[inst][ns][0] for ns in common_sorted) for inst in instruments}
    volumes = {inst: tuple(leg_maps[inst][ns][1] for ns in common_sorted) for inst in instruments}

    funding: list[FundingRow] = []
    funding_dropped = 0
    interval_counts: dict[str, int] = {}
    interval_defaulted = 0
    if "funding" in df["channel"].unique().to_list():
        names = df.filter(pl.col("channel") == "funding")["instrument"].to_list()
        payloads = df.filter(pl.col("channel") == "funding")["payload"].to_list()
    else:
        names, payloads = [], []
    for nm, pay in zip(names, payloads, strict=False):
        if "funding_time_ms" not in pay or "funding_rate" not in pay:
            funding_dropped += 1
            continue
        try:
            hours = funding_interval_hours(pay)
            rate = Decimal(str(pay["funding_rate"]))
            if not rate.is_finite():
                raise ValueError("nonfinite funding rate")
            if hours is not None and (not math.isfinite(hours) or hours <= 0):
                raise ValueError("invalid funding interval")
            row = FundingRow(
                instrument=str(nm),
                funding_time_ms=int(pay["funding_time_ms"]),
                funding_rate=rate,
                interval_hours=DEFAULT_FUNDING_INTERVAL_HOURS if hours is None else hours,
                interval_defaulted=hours is None,
            )
        except (KeyError, ValueError, TypeError, InvalidOperation, OverflowError):
            # Never silently free: dropped records are counted on the tape.
            funding_dropped += 1
            continue
        funding.append(row)
        if hours is None:
            interval_defaulted += 1
        else:
            key = _interval_key(hours)
            interval_counts[key] = interval_counts.get(key, 0) + 1

    # Exact, cheap measurement: the payload column is a Struct whose fields are
    # the union of every payload key the file actually carries.
    payload_dtype = kline.schema["payload"]
    payload_fields = (
        [f.name for f in payload_dtype.fields] if isinstance(payload_dtype, pl.Struct) else []
    )
    mark_keys = {name for name in payload_fields if "mark" in name.lower()}
    absences: list[str] = []
    if not mark_keys:
        absences.append(
            "MARK_PRICE_ABSENT: the tape carries no mark-price field; funding mark is "
            "unknown, not zero"
        )
    if interval_defaulted:
        absences.append(
            f"FUNDING_INTERVAL_DEFAULTED: {interval_defaulted} funding record(s) carried no "
            f"interval key and used the {DEFAULT_FUNDING_INTERVAL_HOURS:g}h convention"
        )
    if funding_dropped:
        absences.append(f"FUNDING_RECORDS_DROPPED: {funding_dropped} malformed funding record(s)")

    return MultiTape(
        instruments=tuple(instruments),
        candles=candles,
        quote_volumes=volumes,
        funding=tuple(sorted(funding, key=lambda r: r.funding_time_ms)),
        tape_path=str(p),
        tape_sha256=sha,
        funding_dropped=funding_dropped,
        funding_raw_count=len(names),
        coverage=coverage,
        intersection_dropped=dropped,
        funding_interval_counts=dict(sorted(interval_counts.items())),
        funding_interval_defaulted=interval_defaulted,
        mark_price_absent=not mark_keys,
        absence_notes=tuple(absences),
    )


def convert_tape_to_parquet(
    jsonl_path: Path | str,
    parquet_path: Path | str,
    *,
    ceremony_path: Path | str | None = None,
) -> dict[str, Any]:
    """One-time ``tape.jsonl (sha256) -> tape.parquet (sha256)`` ceremony (R1).

    Reads the frozen JSONL oracle, sorts by ``(instrument, event_time)`` for
    symbol/time partition pruning, and writes a single parquet file with the
    identical rows (no value recoding, no Decimal-type change: downstream
    decodes with the same ``Decimal(str(...))`` as the JSONL path). Returns
    the ceremony record (both sha256, row counts); when ``ceremony_path`` is
    given the record is also written as JSON. Executed in Wave 2 only; Wave 1
    writes the code path without running any conversion.
    """
    import json

    src = Path(jsonl_path)
    if src.is_dir():
        src = src / JSONL_TAPE_NAME
    if not src.is_file():
        raise FileNotFoundError(f"JSONL oracle not found at {src}")
    dst = Path(parquet_path)
    if dst.is_dir():
        dst = dst / PARQUET_TAPE_NAME
    src_sha = _file_sha(src)
    df = pl.scan_ndjson(src).collect()
    for required in ("channel", "instrument", "event_time", "payload"):
        if required not in df.columns:
            raise ValueError(f"tape schema mismatch at {src}: missing column {required!r}")
    df = df.sort(["instrument", "event_time"])
    dst.parent.mkdir(parents=True, exist_ok=True)
    df.write_parquet(dst)
    dst_sha = _file_sha(dst)
    ceremony: dict[str, Any] = {
        "src_path": str(src),
        "src_sha256": src_sha,
        "dst_path": str(dst),
        "dst_sha256": dst_sha,
        "rows": df.height,
        "layout": "sorted by (instrument, event_time) for symbol/time partition pruning",
        "decimal_policy": "no type change: values decoded as Decimal(str(...)) on both paths",
    }
    if ceremony_path is not None:
        out = Path(ceremony_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(ceremony, indent=2, sort_keys=True))
    return ceremony
