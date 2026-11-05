#!/usr/bin/env python
"""Build the 10-symbol, 2025-H1 tape set for the cross-market validation.

Every archive is verified against Binance's published ``.CHECKSUM`` before a
single row is read (``archive_verification.json`` is produced by
``cross_market_fetch_archives.py``). The verification digest is the one the tape builder
re-checks, so a corrupted scratch file can never reach a measurement.

Writes, under ``research/tape/``:

* ``<symbol>-1h-2025H1``  — H1 execution tape, with the funding channel
* ``<symbol>-15m-2025H1`` — M15 context tape

and a manifest of every archive digest at
``reports/v9-cross-market-validation/archive_hashes.txt``.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "tools"))

from build_tape import build, load_manifest  # noqa: E402

OUT_ROOT = REPO / "research" / "tape"
HASH_MANIFEST = REPO / "reports" / "v9-cross-market-validation" / "archive_hashes.txt"

SYMBOLS = [
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
]
MONTHS = [f"2025-{m:02d}" for m in range(1, 7)]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--archives",
        type=Path,
        default=Path(os.environ.get("ARCHIVE_DIR", str(REPO / "archives"))),
        help="directory holding the 180 archives and .CHECKSUM files",
    )
    parser.add_argument(
        "--verification",
        type=Path,
        default=REPO / "reports" / "v9-cross-market-validation" / "data" / "archive_verification.json",
        help="archive_verification.json produced by cross_market_fetch_archives.py",
    )
    parser.add_argument("--out-root", type=Path, default=OUT_ROOT)
    args = parser.parse_args(argv)
    archives = args.archives
    report = json.loads(args.verification.read_text())
    verified = {r["archive"]: r["sha256"] for r in report["archives"] if r["status"] == "VERIFIED"}
    expected_archives = [
        f"{s}-{ch}-{m}.zip"
        for s in SYMBOLS
        for ch in ("1h", "15m", "fundingRate")
        for m in MONTHS
    ]
    missing = [name for name in expected_archives if name not in verified]
    if missing:
        print(f"UNVERIFIED: {missing}")
        return 1
    HASH_MANIFEST.parent.mkdir(parents=True, exist_ok=True)
    HASH_MANIFEST.write_text(
        "".join(f"{verified[name]}  {name}\n" for name in sorted(expected_archives)),
        encoding="utf-8",
    )
    manifest = load_manifest(HASH_MANIFEST)
    assert len(manifest) == 180, len(manifest)

    summaries = {}
    for symbol in SYMBOLS:
        lower = symbol.lower()
        klines_1h = [archives / f"{symbol}-1h-{m}.zip" for m in MONTHS]
        funding = [archives / f"{symbol}-fundingRate-{m}.zip" for m in MONTHS]
        klines_15m = [archives / f"{symbol}-15m-{m}.zip" for m in MONTHS]
        out_1h = args.out_root / f"{lower}-1h-2025H1"
        out_15m = args.out_root / f"{lower}-15m-2025H1"
        summaries[f"{lower}-1h-2025H1"] = build(klines_1h, funding, out_1h, manifest)
        summaries[f"{lower}-15m-2025H1"] = build(klines_15m, [], out_15m, manifest)
        print(
            json.dumps(
                {
                    "symbol": symbol,
                    "h1_rows": summaries[f"{lower}-1h-2025H1"]["channel_counts"].get("kline"),
                    "funding_rows": summaries[f"{lower}-1h-2025H1"]["channel_counts"].get("funding"),
                    "m15_rows": summaries[f"{lower}-15m-2025H1"]["channel_counts"].get("kline"),
                    "h1_sha256": summaries[f"{lower}-1h-2025H1"]["tape_sha256"],
                    "m15_sha256": summaries[f"{lower}-15m-2025H1"]["tape_sha256"],
                }
            ),
            flush=True,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
