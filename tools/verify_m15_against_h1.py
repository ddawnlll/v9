"""Compare downloaded Binance monthly M15 archives with the local H1 tape.

Usage: uv run python tools/verify_m15_against_h1.py /path/to/archives
The caller must independently verify the archive checksums against the publisher.
"""
from __future__ import annotations

import csv
import json
import sys
import zipfile
from decimal import Decimal
from pathlib import Path

archives = sorted(Path(sys.argv[1]).glob('BTCUSDT-15m-2025-*.zip'))
if len(archives) != 3:
    raise SystemExit('expected three 2025 Q1 archives')
rows = []
for archive in archives:
    with zipfile.ZipFile(archive) as z:
        names = z.namelist()
        if len(names) != 1:
            raise SystemExit(f'unexpected archive members: {archive}')
        rows.extend(csv.DictReader(z.read(names[0]).decode().splitlines()))
times = [int(row['open_time']) for row in rows]
hours = []
with Path('research/tape/btcusdt-1h-2025Q1/tape.jsonl').open() as tape:
    for line in tape:
        record = json.loads(line)
        if record['channel'] == 'kline':
            hours.append(record['payload'])
if len(rows) != len(hours) * 4:
    raise SystemExit('bar count mismatch')
mismatches = []
for i, hour in enumerate(hours):
    group = rows[i * 4:i * 4 + 4]
    actual = (int(group[0]['open_time']), Decimal(group[0]['open']),
              max(Decimal(row['high']) for row in group),
              min(Decimal(row['low']) for row in group), Decimal(group[-1]['close']),
              sum(Decimal(row['volume']) for row in group))
    expected = (hour['open_time_ms'], *(Decimal(hour[k]) for k in ('open', 'high', 'low', 'close', 'volume')))
    if actual != expected:
        mismatches.append({'open_ms': hour['open_time_ms'], 'm15': [str(v) for v in actual],
                           'h1': [str(v) for v in expected]})
print(json.dumps({'bars': len(rows), 'gaps': sum(b - a != 900_000 for a, b in zip(times, times[1:], strict=False)),
                  'hours_compared': len(hours), 'mismatches': mismatches}, indent=2))
