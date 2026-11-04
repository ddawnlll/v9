#!/bin/sh
set -eu
tools/performance/build_linux_image.sh
out=$(mktemp -d "${TMPDIR:-/tmp}/v9-synthetic-portfolio.XXXXXX")
mkdir -p "$out/portfolio_short" "$out/portfolio_long" "$out/research"
docker run --rm --cpus=2 --memory=4g --memory-swap=4g -v "$PWD/tools/performance:/work/tools/performance:ro" -v "$out/portfolio_short:/out" v9-performance:local sh -c '
set -eu
for n in 1 2 3; do
 .venv/bin/python tools/performance/run_process.py --script portfolio --measurement /out/one-$n-process.json -- --bars 720 --instruments 1 --output /out/one-$n.json > /dev/null
 .venv/bin/python tools/performance/run_process.py --script portfolio --measurement /out/ten-$n-process.json -- --bars 720 --instruments 10 --output /out/ten-$n.json > /dev/null
done
cat /sys/fs/cgroup/memory.events > /out/memory-events.txt
'
docker run --rm --cpus=2 --memory=4g --memory-swap=4g -v "$PWD/tools/performance:/work/tools/performance:ro" -v "$out/portfolio_long:/out" v9-performance:local sh -c '
set -eu
for n in 1 2 3; do
 timeout 90 .venv/bin/python tools/performance/run_process.py --script portfolio --measurement /out/long-$n-process.json -- --bars 2160 --instruments 10 --output /out/long-$n.json > /dev/null
done
timeout 90 .venv/bin/python tools/performance/run_process.py --script portfolio --measurement /out/profile-process.json -- --bars 2160 --instruments 10 --profile /out/profile.pstats --output /out/profile.json > /dev/null
.venv/bin/python -c "import pstats; pstats.Stats(\"/out/profile.pstats\").sort_stats(\"cumulative\").print_stats(50)" > /out/profile-top.txt
cat /sys/fs/cgroup/memory.events > /out/memory-events.txt
'
docker run --rm --cpus=2 --memory=4g --memory-swap=4g -v "$PWD/tools/performance:/work/tools/performance:ro" -v "$out/research:/out" v9-performance:local sh -c '
set -eu
for n in 1 2 3; do
 .venv/bin/python tools/performance/run_process.py --script research --measurement /out/run-$n-process.json -- --bars 720 --artifacts /out/artifacts-$n --output /out/run-$n.json > /dev/null
done
cat /sys/fs/cgroup/memory.events > /out/memory-events.txt
'
printf 'Results: %s\n' "$out"
