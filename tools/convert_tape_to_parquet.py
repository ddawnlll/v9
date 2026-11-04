#!/usr/bin/env python
"""One-time tape.jsonl -> tape.parquet conversion ceremony (#467 R1).

Wave 2 only: records src/dst sha256 into docs/evidence/. Wave 1 writes this
script without running any conversion. JSONL stays the frozen oracle until
the R3 gated cutover on green parity. No Decimal-type change.
"""

from __future__ import annotations

import argparse
import json

from v8_next.evaluation.multitape import convert_tape_to_parquet


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--jsonl", required=True, help="tape.jsonl path or containing dir")
    p.add_argument("--parquet", required=True, help="output tape.parquet path or dir")
    p.add_argument("--ceremony", default=None, help="optional ceremony JSON output path")
    args = p.parse_args()
    ceremony = convert_tape_to_parquet(args.jsonl, args.parquet, ceremony_path=args.ceremony)
    print(json.dumps(ceremony, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
