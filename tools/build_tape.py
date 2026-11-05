#!/usr/bin/env python
"""Build a JSONL multitape from hash-verified Binance USD-M monthly archives.

Writes ``<out>/tape.jsonl`` (plus ``<out>/tape.manifest.json``) in the format
read by :func:`v8_next.evaluation.multitape.load_multitape`: one row per event,

    {"channel": "kline" | "funding", "instrument": ..., "event_time": ...,
     "payload": {...}, "payload_hash": "..."}

Every archive is checked against a hash manifest *before* a single row is read.
A missing manifest entry, a digest mismatch, a duplicate bar slot or an
unparsable row fails the run instead of producing a partial tape. Nothing is
synthesized: prices, volumes and funding rates are copied verbatim from the
vendor CSV as decimal strings (no float round-trip), and each payload carries a
sha256 over its own canonical JSON so a later reader can prove the bytes.

Usage (from the repository root):

    uv run --project . --extra dev python tools/build_tape.py \\
        --kline /path/BTCUSDT-1h-2025-01.zip /path/BTCUSDT-1h-2025-02.zip ... \\
        --funding /path/BTCUSDT-fundingRate-2025-01.zip ... \\
        --out research/tape/btcusdt-1h-2025Q1
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import zipfile
from pathlib import Path
from typing import Any, Iterator

SCHEMA_VERSION = "v9.tape.1"

KLINE_COLUMNS = (
    "open_time",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "close_time",
    "quote_volume",
    "count",
    "taker_buy_volume",
    "taker_buy_quote_volume",
    "ignore",
)

FUNDING_COLUMNS = ("calc_time", "funding_interval_hours", "last_funding_rate")


def canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def payload_hash(payload: dict[str, Any]) -> str:
    """sha256 over the payload's own canonical JSON, excluding the hash field."""
    body = {k: v for k, v in payload.items() if k != "payload_hash"}
    return hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def load_manifest(path: Path) -> dict[str, str]:
    """Map archive basename -> sha256 from a ``sha256sum``-style manifest."""
    if not path.is_file():
        raise FileNotFoundError(f"hash manifest not found: {path}")
    out: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        digest, _, name = line.partition("  ")
        if not digest or not name:
            raise ValueError(f"malformed manifest line in {path}: {line!r}")
        out[name.strip()] = digest.strip().lower()
    return out


def verify_archive(path: Path, manifest: dict[str, str], *, require: bool) -> str:
    expected = manifest.get(path.name)
    if expected is None:
        if require:
            raise ValueError(
                f"{path.name} is not in the hash manifest; refusing to build a tape "
                "from an unverifiable archive"
            )
        return ""
    actual = sha256_file(path)
    if actual != expected:
        raise ValueError(
            f"{path.name} digest mismatch: manifest {expected}, file {actual}"
        )
    return actual


def _rows(archive: Path, member_suffix: str) -> Iterator[list[str]]:
    """Yield CSV rows (header stripped) from the single member of an archive."""
    with zipfile.ZipFile(archive) as zf:
        names = [n for n in zf.namelist() if n.endswith(member_suffix)]
        if len(names) != 1:
            raise ValueError(
                f"{archive.name}: expected exactly one {member_suffix!r} member, "
                f"found {names}"
            )
        with zf.open(names[0]) as raw:
            text = io.TextIOWrapper(raw, encoding="utf-8", newline="")
            yield from csv.reader(text)


def instrument_of(archive_name: str) -> str:
    # BTCUSDT-1h-2025-01.zip / BTCUSDT-fundingRate-2025-01.zip
    return archive_name.split("-", 1)[0]


def kline_records(archive: Path) -> Iterator[dict[str, Any]]:
    instrument = instrument_of(archive.name)
    reader = _rows(archive, ".csv")
    header = tuple(next(reader, ()))
    if header[: len(KLINE_COLUMNS)] != KLINE_COLUMNS:
        raise ValueError(f"{archive.name}: unexpected kline header {header!r}")
    for line, row in enumerate(reader, start=2):
        if not row:
            continue
        if len(row) != len(KLINE_COLUMNS):
            raise ValueError(f"{archive.name}:{line}: expected 12 fields, got {len(row)}")
        (
            open_time,
            open_px,
            high,
            low,
            close,
            volume,
            close_time,
            quote_volume,
            count,
            taker_buy_volume,
            taker_buy_quote_volume,
            _ignore,
        ) = row
        open_ms = int(open_time)
        close_ms = int(close_time)
        if close_ms <= open_ms:
            raise ValueError(f"{archive.name}:{line}: close_time <= open_time")
        payload: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "open_time_ms": open_ms,
            "close_time_ms": close_ms,
            "open": open_px,
            "high": high,
            "low": low,
            "close": close,
            "volume": volume,
            "quote_asset_volume": quote_volume,
            "trades": int(count),
            "taker_buy_volume": taker_buy_volume,
            "taker_buy_quote_asset_volume": taker_buy_quote_volume,
            "closed": True,
        }
        payload["payload_hash"] = payload_hash(payload)
        yield {
            "channel": "kline",
            "instrument": instrument,
            # A bar is knowable when it closes, not when it opens.
            "event_time": close_ms + 1,
            "payload": payload,
            "payload_hash": payload["payload_hash"],
        }


def funding_records(archive: Path) -> Iterator[dict[str, Any]]:
    instrument = instrument_of(archive.name)
    reader = _rows(archive, ".csv")
    header = tuple(next(reader, ()))
    if header[: len(FUNDING_COLUMNS)] != FUNDING_COLUMNS:
        raise ValueError(f"{archive.name}: unexpected funding header {header!r}")
    for line, row in enumerate(reader, start=2):
        if not row:
            continue
        if len(row) != len(FUNDING_COLUMNS):
            raise ValueError(f"{archive.name}:{line}: expected 3 fields, got {len(row)}")
        calc_time, interval, rate = row
        payload: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "funding_time_ms": int(calc_time),
            "funding_interval_hours": float(interval),
            "funding_rate": rate,
        }
        payload["payload_hash"] = payload_hash(payload)
        yield {
            "channel": "funding",
            "instrument": instrument,
            "event_time": int(calc_time),
            "payload": payload,
            "payload_hash": payload["payload_hash"],
        }


def build(
    kline_archives: list[Path],
    funding_archives: list[Path],
    out_dir: Path,
    manifest: dict[str, str],
    *,
    require_manifest: bool = True,
) -> dict[str, Any]:
    if not kline_archives:
        raise ValueError("at least one kline archive is required")

    verified: dict[str, str] = {}
    records: list[dict[str, Any]] = []
    for archive in [*kline_archives, *funding_archives]:
        verified[archive.name] = verify_archive(
            archive, manifest, require=require_manifest
        )

    for archive in kline_archives:
        records.extend(kline_records(archive))
    for archive in funding_archives:
        records.extend(funding_records(archive))

    slots: dict[str, dict[int, int]] = {}
    for rec in records:
        if rec["channel"] != "kline":
            continue
        leg = slots.setdefault(rec["instrument"], {})
        ms = int(rec["payload"]["open_time_ms"])
        leg[ms] = leg.get(ms, 0) + 1
    duplicated = {inst: sum(c - 1 for c in leg.values() if c > 1) for inst, leg in slots.items()}
    if any(duplicated.values()):
        raise ValueError(f"duplicated bar slots in input archives: {duplicated}")

    records.sort(key=lambda r: (int(r["event_time"]), r["channel"], r["instrument"]))

    out_dir.mkdir(parents=True, exist_ok=True)
    tape_path = out_dir / "tape.jsonl"
    tmp = tape_path.with_suffix(".jsonl.tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        for rec in records:
            fh.write(canonical_json(rec))
            fh.write("\n")
    tmp.replace(tape_path)

    channel_counts: dict[str, int] = {}
    per_instrument: dict[str, dict[str, Any]] = {}
    for rec in records:
        channel_counts[rec["channel"]] = channel_counts.get(rec["channel"], 0) + 1
        slot = per_instrument.setdefault(
            rec["instrument"], {"kline_rows": 0, "funding_rows": 0}
        )
        slot["funding_rows" if rec["channel"] == "funding" else "kline_rows"] += 1
    for inst, leg in slots.items():
        first, last = min(leg), max(leg)
        per_instrument[inst]["first_open_ms"] = first
        per_instrument[inst]["last_open_ms"] = last

    summary = {
        "tape_sha256": sha256_file(tape_path),
        "rows": len(records),
        "channel_counts": channel_counts,
        "instruments": per_instrument,
        "archives": verified,
        "schema_version": SCHEMA_VERSION,
        "hash_manifest_required": require_manifest,
    }
    (out_dir / "tape.manifest.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Build a verified JSONL multitape from Binance monthly archives."
    )
    parser.add_argument("--kline", nargs="+", required=True, type=Path)
    parser.add_argument("--funding", nargs="*", default=[], type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument(
        "--hash-manifest",
        type=Path,
        default=Path("reports/performance/evidence/tape-hashes.txt"),
    )
    parser.add_argument(
        "--allow-unverified",
        action="store_true",
        help="permit archives absent from the manifest (never for evidence runs)",
    )
    args = parser.parse_args()

    manifest = load_manifest(args.hash_manifest) if args.hash_manifest.is_file() else {}
    if not manifest and not args.allow_unverified:
        raise SystemExit(f"hash manifest missing or empty: {args.hash_manifest}")

    summary = build(
        args.kline,
        args.funding,
        args.out,
        manifest,
        require_manifest=not args.allow_unverified,
    )
    print(json.dumps(summary, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
