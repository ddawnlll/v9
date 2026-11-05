#!/usr/bin/env python
"""Fetch and hash-verify the 180 public Binance USD-M monthly archives.

Grid: 10 symbols x 6 months (2025-01..2025-06) x 3 channels
(``1h`` klines, ``15m`` klines, ``fundingRate``).

Every file is checked against the vendor's published ``.CHECKSUM`` before it is
accepted. A mismatch or missing checksum fails closed; nothing is repaired by
substitution. Outputs:

* ``<ARCHIVE_DIR>/<name>.zip`` and ``<name>.zip.CHECKSUM``
* ``reports/v9-cross-market-validation/archive_hashes.txt`` (180 sha256sum lines)
* ``reports/v9-cross-market-validation/data/archive_verification.json``

Usage:
    ARCHIVE_DIR=/path/to/archives python tools/cross_market_fetch_archives.py
"""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import os
import time
import urllib.request
from pathlib import Path
from typing import Any

BASE = "https://data.binance.vision/data/futures/um/monthly"
SYMBOLS = (
    "ETHUSDT",
    "SOLUSDT",
    "BNBUSDT",
    "XRPUSDT",
    "DOGEUSDT",
    "ADAUSDT",
    "AVAXUSDT",
    "LINKUSDT",
    "DOTUSDT",
    "LTCUSDT",
)
MONTHS = tuple(f"2025-{month:02d}" for month in range(1, 7))


def _url(symbol: str, channel: str, month: str) -> str:
    if channel == "fundingRate":
        return f"{BASE}/fundingRate/{symbol}/{symbol}-fundingRate-{month}.zip"
    return f"{BASE}/klines/{symbol}/{channel}/{symbol}-{channel}-{month}.zip"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _download(url: str, destination: Path, *, timeout: int, retries: int) -> None:
    """Fetch one URL with bounded retries; no silent fallback on final failure."""
    last: Exception | None = None
    for attempt in range(retries):
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "v9-validation/1.0"})
            with urllib.request.urlopen(request, timeout=timeout) as response:
                destination.write_bytes(response.read())
            return
        except Exception as exc:  # noqa: BLE001 - retried, then reported
            last = exc
            if attempt + 1 < retries:
                time.sleep(1.5 * (attempt + 1))
    raise last if last is not None else RuntimeError(f"failed to fetch {url}")


def _fetch_one(job: tuple[str, str, str, Path, int]) -> dict[str, Any]:
    symbol, channel, month, destination, retries = job
    url = _url(symbol, channel, month)
    name = url.rsplit("/", 1)[1]
    archive = destination / name
    checksum_path = destination / f"{name}.CHECKSUM"
    try:
        if not archive.exists():
            _download(url, archive, timeout=180, retries=retries)
        if not checksum_path.exists():
            _download(url + ".CHECKSUM", checksum_path, timeout=60, retries=retries)
        expected = checksum_path.read_text().split()[0].lower()
        actual = _sha256(archive)
        return {
            "archive": name,
            "symbol": symbol,
            "channel": channel,
            "month": month,
            "status": "VERIFIED" if actual == expected else "CHECKSUM_MISMATCH",
            "sha256": actual,
            "expected": expected,
            "source_url": url,
        }
    except Exception as exc:  # noqa: BLE001 - every failure is reported, never hidden
        return {
            "archive": name,
            "symbol": symbol,
            "channel": channel,
            "month": month,
            "status": "FETCH_FAILED",
            "error": repr(exc),
            "source_url": url,
        }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--archive-dir",
        type=Path,
        default=Path(os.environ.get("ARCHIVE_DIR", "archives")),
        help="destination for archives and .CHECKSUM files",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("reports/v9-cross-market-validation"),
    )
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument("--retries", type=int, default=3)
    args = parser.parse_args(argv)
    args.archive_dir.mkdir(parents=True, exist_ok=True)
    jobs = [
        (symbol, channel, month, args.archive_dir, max(1, args.retries))
        for symbol in SYMBOLS
        for channel in ("1h", "15m", "fundingRate")
        for month in MONTHS
    ]
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        results = list(pool.map(_fetch_one, jobs))

    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "data").mkdir(parents=True, exist_ok=True)
    hashes = [f"{row['sha256']}  {row['archive']}\n" for row in results if row["status"] == "VERIFIED"]
    (args.out / "archive_hashes.txt").write_text("".join(hashes))
    counts: dict[str, int] = {}
    for row in results:
        counts[row["status"]] = counts.get(row["status"], 0) + 1
    payload = {
        "base": BASE,
        "archive_dir": str(args.archive_dir),
        "archives": results,
        "counts": counts,
        "required": len(jobs),
    }
    (args.out / "data" / "archive_verification.json").write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n"
    )
    for row in results:
        if row["status"] != "VERIFIED":
            print("FAIL", row["archive"], row["status"], row.get("error", row.get("expected")))
    print("verified", counts.get("VERIFIED", 0), "of", len(jobs), "required")
    return 0 if counts.get("VERIFIED", 0) == len(jobs) else 1


if __name__ == "__main__":
    raise SystemExit(main())
