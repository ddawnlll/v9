#!/bin/sh
set -eu
source_dir=${V9_DEV_SOURCE:-/Users/hootie/src/v8/research/tape/multi-1h-4y}
work=$(mktemp -d "${TMPDIR:-/tmp}/v9-allocation.XXXXXX")
mkdir -p "$work/tape" "$work/out"
for month in 01 02 03; do
 cp "$source_dir/BTCUSDT-1h-2025-$month.zip" "$work/tape/"
 cp "$source_dir/BTCUSDT-1h-2025-$month.zip.CHECKSUM" "$work/tape/"
done
(cd "$work/tape" && for month in 01 02 03; do shasum -a 256 -c "BTCUSDT-1h-2025-$month.zip.CHECKSUM"; done)
tools/performance/build_linux_image.sh
docker run --rm --cpus=2 --memory=4g --memory-swap=4g -v "$PWD/tools/performance:/work/tools/performance:ro" -v "$work/tape:/tape:ro" -v "$work/out:/out" v9-performance:local sh -c '
set +e
timeout 90 .venv/bin/python tools/performance/run_process.py --measurement /out/allocation-process.json -- --tape /tape --bars 2160 --allocations /out/allocations.json --allocation-frames 10 --output /out/allocation-summary.json > /dev/null
status=$?
echo "$status" > /out/status.txt
cat /sys/fs/cgroup/memory.events > /out/memory-events.txt
exit "$status"
'
printf 'Results: %s\n' "$work"
