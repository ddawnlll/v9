#!/bin/sh
set -eu
# Requires Goose v2026.3, uv and the three checksum-pinned development archives.
source_dir=${V9_DEV_SOURCE:-/Users/hootie/src/v8/research/tape/multi-1h-4y}
goose_bin=${GOOSE_BIN:-goose}
work=$(mktemp -d "${TMPDIR:-/tmp}/v9-goose-continuity.XXXXXX")
mkdir -p "$work/tape"
for month in 01 02 03; do
  cp "$source_dir/BTCUSDT-1h-2025-$month.zip" "$work/tape/"
done
/usr/bin/time -p "$goose_bin" build tools/performance/continuity_bench.goose -o "$work/continuity_bench" 2> "$work/build-time.txt"
POLARS_MAX_THREADS=2 OMP_NUM_THREADS=2 OPENBLAS_NUM_THREADS=2 uv run --locked --extra dev python tools/performance/continuity_compare.py --tape "$work/tape" --goose-executable "$work/continuity_bench" --output "$work/goose-samples.json"
printf 'Results: %s\n' "$work"
