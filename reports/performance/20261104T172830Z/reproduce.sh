#!/bin/sh
set -eu
# Only the monograph's BTC development window is copied. Do not use a four-year tape.
source_dir=${V9_DEV_SOURCE:-/Users/hootie/src/v8/research/tape/multi-1h-4y}
work_dir=$(mktemp -d "${TMPDIR:-/tmp}/v9-development-perf.XXXXXX")
mkdir -p "$work_dir/tape/jan" "$work_dir/tape/q1" "$work_dir/engine" "$work_dir/research"
for month in 01 02 03; do
  filename="BTCUSDT-1h-2025-$month.zip"
  cp "$source_dir/$filename" "$work_dir/tape/q1/$filename"
  cp "$source_dir/$filename.CHECKSUM" "$work_dir/tape/q1/$filename.CHECKSUM"
done
(cd "$work_dir/tape/q1" && for month in 01 02 03; do shasum -a 256 -c "BTCUSDT-1h-2025-$month.zip.CHECKSUM"; done)
cp "$work_dir/tape/q1/BTCUSDT-1h-2025-01.zip" "$work_dir/tape/jan/"
tools/performance/build_linux_image.sh
docker run --rm --cpus=2 --memory=4g --memory-swap=4g \
  -v "$PWD/tools/performance:/work/tools/performance:ro" \
  -v "$work_dir/tape:/tape:ro" -v "$work_dir/engine:/out" v9-performance:local sh -c '
set -eu
for n in 1 2 3; do
  timeout 90 .venv/bin/python tools/performance/run_process.py --measurement /out/jan-$n-process.json -- --tape /tape/jan --bars 744 --output /out/jan-$n.json > /dev/null
  timeout 90 .venv/bin/python tools/performance/run_process.py --measurement /out/q1-$n-process.json -- --tape /tape/q1 --bars 2160 --output /out/q1-$n.json > /dev/null
done
timeout 90 .venv/bin/python tools/performance/run_process.py --measurement /out/q1-stage-process.json -- --tape /tape/q1 --bars 2160 --stage-trace --output /out/q1-stage.json > /dev/null
timeout 90 .venv/bin/python tools/performance/run_process.py --measurement /out/q1-profile-process.json -- --tape /tape/q1 --bars 2160 --profile /out/q1.pstats --output /out/q1-profile.json > /dev/null
timeout 90 .venv/bin/python tools/performance/run_process.py --measurement /out/q1-expert-process.json -- --tape /tape/q1 --bars 2160 --expert-timings --output /out/q1-expert.json > /dev/null
.venv/bin/python -c "import pstats; pstats.Stats(\"/out/q1.pstats\").sort_stats(\"cumulative\").print_stats(50)" > /out/q1-profile-top.txt
cat /sys/fs/cgroup/memory.events > /out/memory-events.txt
'
docker run --rm --cpus=2 --memory=4g --memory-swap=4g \
  -v "$PWD/tools/performance:/work/tools/performance:ro" \
  -v "$work_dir/tape:/tape:ro" -v "$work_dir/research:/out" v9-performance:local sh -c '
set -eu
for n in 1 2 3; do
  timeout 90 .venv/bin/python tools/performance/run_process.py --script research --measurement /out/jan-$n-process.json -- --tape /tape/jan --bars 744 --artifacts /out/jan-artifacts-$n --output /out/jan-$n.json > /dev/null
  timeout 90 .venv/bin/python tools/performance/run_process.py --script research --measurement /out/q1-$n-process.json -- --tape /tape/q1 --bars 2160 --artifacts /out/q1-artifacts-$n --output /out/q1-$n.json > /dev/null
done
cat /sys/fs/cgroup/memory.events > /out/memory-events.txt
'
uv run --locked --extra dev python tools/performance/waterfall.py --process "$work_dir/engine/q1-stage-process.json" --child "$work_dir/engine/q1-stage.json" --output "$work_dir/engine/timings.csv"
printf 'Results: %s\n' "$work_dir"
