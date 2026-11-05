#!/usr/bin/env python
"""Run the five-grammar census on each verified H1 tape (observational only).

The census does not feed any frozen A/B/C/D decision. It records detections,
deduplicated episodes, bracket outcomes and cross-grammar overlap with no costs.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from v8_next.evaluation.grammar_census import _load_verified_candles, run_grammar_census

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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--tape-root", type=Path, default=Path("research/tape"))
    parser.add_argument(
        "--out", type=Path, default=Path("reports/v9-cross-market-validation/raw/opportunity_census.json")
    )
    args = parser.parse_args(argv)
    payload: dict[str, object] = {"schema_version": "v9-cross-market-opportunity-census-v1", "symbols": {}}
    for symbol in SYMBOLS:
        directory = args.tape_root / f"{symbol.lower()}-1h-2025H1"
        candles, sha = _load_verified_candles(directory, symbol)
        census = run_grammar_census(
            candles,
            instrument_id=f"{symbol}-PERP.BINANCE",
            tape_sha256=sha,
        )
        payload["symbols"][symbol] = census.as_dict()
        print(symbol, "digest", census.evidence_digest[:16], "bars", census.bars)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    print(args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
