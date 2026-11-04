#!/bin/sh
set -eu
# Run from the repository root; requires Docker and dependency access.
tools/performance/build_linux_image.sh
mkdir -p /tmp/v9-monitor-repro
docker run --rm --cpus=2 --memory=4g --memory-swap=4g \
  -v "$PWD/tools/performance:/work/tools/performance:ro" \
  -v /tmp/v9-monitor-repro:/out v9-performance:local sh -c '
set -eu
for n in 1 2 3; do
  .venv/bin/python tools/performance/run_process.py --measurement /out/base-$n-process.json -- --bars 720 --output /out/base-$n.json > /dev/null
  .venv/bin/python tools/performance/run_process.py --measurement /out/monitor-$n-process.json -- --bars 720 --stage-trace --output /out/monitor-$n.json > /dev/null
done
.venv/bin/python tools/performance/run_process.py --measurement /out/breakout-process.json -- --bars 100 --fixture breakout --stage-trace --output /out/breakout.json > /dev/null
cat /sys/fs/cgroup/memory.events > /out/memory-events.txt
cat /sys/fs/cgroup/memory.max > /out/memory-max.txt
cat /sys/fs/cgroup/cpu.max > /out/cpu-max.txt
'
uv run --locked --extra dev python tools/performance/waterfall.py --process /tmp/v9-monitor-repro/monitor-1-process.json --child /tmp/v9-monitor-repro/monitor-1.json --output /tmp/v9-monitor-repro/timings.csv
