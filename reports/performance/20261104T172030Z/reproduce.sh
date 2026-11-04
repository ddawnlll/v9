#!/bin/sh
set -eu
# Run from the repository root. Requires Docker and network for image/dependencies.
tools/performance/build_linux_image.sh
mkdir -p /tmp/v9-linux-repro
docker run --rm --cpus=2 --memory=4g --memory-swap=4g \
  -v "$PWD/tools/performance:/work/tools/performance:ro" \
  -v /tmp/v9-linux-repro:/out v9-performance:local sh -c '
set -eu
cat /sys/fs/cgroup/memory.max > /out/memory-max.txt
cat /sys/fs/cgroup/cpu.max > /out/cpu-max.txt
uname -a > /out/uname.txt
.venv/bin/python --version > /out/python.txt
for n in 1 2 3; do
  .venv/bin/python tools/performance/run_process.py --measurement /out/flat-process-$n.json -- --bars 720 --output /out/flat-$n.json > /dev/null
  .venv/bin/python tools/performance/run_process.py --measurement /out/breakout-process-$n.json -- --bars 100 --fixture breakout --output /out/breakout-$n.json > /dev/null
done
.venv/bin/python tools/performance/run_process.py --measurement /out/expert-process.json -- --bars 100 --fixture breakout --expert-timings --output /out/expert.json > /dev/null
.venv/bin/python tools/performance/run_process.py --measurement /out/profile-process.json -- --bars 720 --profile /out/profile.pstats --output /out/profile.json > /dev/null
.venv/bin/python -c "import pstats; pstats.Stats(\"/out/profile.pstats\").sort_stats(\"cumulative\").print_stats(50)" > /out/profile-top.txt
cat /sys/fs/cgroup/memory.events > /out/memory-events.txt
'
# Optional: install the v2026.3 Goose compiler separately and put it on PATH.
# goose build tools/performance/continuity.goose -o /tmp/v9-continuity
# uv run --locked --extra dev python tools/performance/continuity_parity.py --executable /tmp/v9-continuity
