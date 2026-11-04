#!/bin/sh
# Reproduce the V9 performance/memory benchmark.
#
# Requires: docker (2 CPU equivalents / 4 GiB are enforced with flags), shasum,
# and the git-ignored sibling market-data root holding the Binance monthly
# archives. Nothing here writes into git history; raw pstats binaries are kept
# in the temporary work directory only.
#
#   ./reports/performance/reproduce.sh
#
# The evidence committed under reports/performance/evidence/ was produced with
# exactly the commands below, in the order below.
set -eu

REPO=$(cd "$(dirname "$0")/../.." && pwd)
cd "$REPO"

SOURCE_DIR=${V9_DEV_SOURCE:-/Users/hootie/src/v8/research/tape/multi-1h-4y}
SYMBOLS=BTCUSDT,ETHUSDT,BNBUSDT,SOLUSDT,XRPUSDT,ADAUSDT,DOGEUSDT,AVAXUSDT,LINKUSDT,LTCUSDT
WORK=$(mktemp -d "${TMPDIR:-/tmp}/v9-performance-benchmark.XXXXXX")
EVIDENCE="$WORK/evidence"
mkdir -p "$EVIDENCE/profile" "$EVIDENCE/verify" "$EVIDENCE/scaling" \
         "$EVIDENCE/research" "$EVIDENCE/control" "$WORK/tape/jan" "$WORK/tape/multi"

# --- 1. Dataset: only the monograph's BTC development window (2025-01-01..2025-04-01).
mkdir -p "$WORK/tape/q1"
for month in 01 02 03; do
  cp "$SOURCE_DIR/BTCUSDT-1h-2025-$month.zip" "$SOURCE_DIR/BTCUSDT-1h-2025-$month.zip.CHECKSUM" \
     "$WORK/tape/q1/" "$WORK/tape/jan/"
  for symbol in BTCUSDT ETHUSDT BNBUSDT SOLUSDT XRPUSDT ADAUSDT DOGEUSDT AVAXUSDT LINKUSDT LTCUSDT; do
    cp "$SOURCE_DIR/$symbol-1h-2025-$month.zip" "$SOURCE_DIR/$symbol-1h-2025-$month.zip.CHECKSUM" \
       "$SOURCE_DIR/$symbol-fundingRate-2025-$month.zip" \
       "$SOURCE_DIR/$symbol-fundingRate-2025-$month.zip.CHECKSUM" "$WORK/tape/multi/"
  done
done
# The single-month case is the January archive alone.
mv "$WORK/tape/jan/BTCUSDT-1h-2025-02.zip" "$WORK/tape/jan/BTCUSDT-1h-2025-02.zip.CHECKSUM" "$WORK/tape/q1/"
mv "$WORK/tape/jan/BTCUSDT-1h-2025-03.zip" "$WORK/tape/jan/BTCUSDT-1h-2025-03.zip.CHECKSUM" "$WORK/tape/q1/"
(cd "$WORK/tape/multi" && for check in *.CHECKSUM; do shasum -a 256 -c "$check"; done)
(cd "$WORK/tape/q1" && shasum -a 256 -c BTCUSDT-1h-2025-01.zip.CHECKSUM)
(cd "$WORK/tape/jan" && shasum -a 256 -c BTCUSDT-1h-2025-01.zip.CHECKSUM)
(cd "$WORK/tape/multi" && shasum -a 256 *.zip) > "$EVIDENCE/tape-hashes.txt"
(cd "$WORK/tape/q1" && shasum -a 256 *.zip) >> "$EVIDENCE/tape-hashes.txt"

# --- 2. Pinned measurement image (python:3.12-slim digest, uv, thread limits).
tools/performance/build_linux_image.sh

run() { # run <out-subdir> <command...>, inside the enforced 2-vCPU / 4-GiB cgroup
  subdir=$1
  shift
  docker run --rm --cpus=2 --memory=4g --memory-swap=4g \
    -v "$REPO/tools/performance:/work/tools/performance:ro" \
    -v "$WORK/tape:/tape:ro" -v "$EVIDENCE/$subdir:/out" \
    v9-performance:local "$@"
}

# --- 3. Triples for B0 (1 month), B1 (3 months), B3 (10 instruments, 3 months).
run verify sh -c '
set -eu
for n in 1 2 3; do
  timeout 120 .venv/bin/python tools/performance/run_process.py --measurement /out/b0-$n-process.json \
    -- --tape /tape/jan --bars 744 --output /out/b0-$n.json > /dev/null
  timeout 120 .venv/bin/python tools/performance/run_process.py --measurement /out/b1-$n-process.json \
    -- --tape /tape/q1 --bars 2160 --output /out/b1-$n.json > /dev/null
  timeout 900 .venv/bin/python tools/performance/run_process.py --measurement /out/b3-$n-process.json \
    --script portfolio_tape -- --tape /tape/multi --symbols '"$SYMBOLS"' --bars 2160 --with-funding \
    --output /out/b3-$n.json > /dev/null
done
cat /sys/fs/cgroup/memory.events > /out/memory-events-after.txt
cat /sys/fs/cgroup/cpu.stat > /out/cpu-stat-after.txt'

# --- 4. Memory timelines (100 ms), stage trace, per-expert trace, cProfile.
run profile sh -c '
set -eu
SYMBOLS='"$SYMBOLS"'
# Warm-up runs are discarded: a fresh container'"'"'s first process is measurably slower.
.venv/bin/python tools/performance/ensemble_profile.py --tape /tape/q1 --bars 2160 --output /tmp/warm1.json > /dev/null
.venv/bin/python tools/performance/portfolio_tape.py --tape /tape/multi --symbols $SYMBOLS --bars 2160 --with-funding --output /tmp/warm2.json > /dev/null
timeout 300 .venv/bin/python tools/performance/memory_timeline.py --timeline /out/b1-timeline.csv \
  --measurement /out/b1-timeline-process.json --interval 0.1 \
  --script tools/performance/ensemble_profile.py -- --tape /tape/q1 --bars 2160 --output /out/b1-timeline-child.json > /dev/null
timeout 900 .venv/bin/python tools/performance/memory_timeline.py --timeline /out/b3-timeline.csv \
  --measurement /out/b3-timeline-process.json --interval 0.1 \
  --script tools/performance/portfolio_tape.py -- --tape /tape/multi --symbols $SYMBOLS --bars 2160 \
  --with-funding --output /out/b3-timeline-child.json > /dev/null
timeout 300 .venv/bin/python tools/performance/run_process.py --measurement /out/b1-stage-process.json \
  -- --tape /tape/q1 --bars 2160 --stage-trace --output /out/b1-stage.json > /dev/null
timeout 300 .venv/bin/python tools/performance/run_process.py --measurement /out/b1-expert-process.json \
  -- --tape /tape/q1 --bars 2160 --expert-exclusive --output /out/b1-expert.json > /dev/null
timeout 300 .venv/bin/python tools/performance/run_process.py --measurement /out/b1-profile-process.json \
  -- --tape /tape/q1 --bars 2160 --profile /out/b1.pstats --output /out/b1-profile.json > /dev/null
timeout 900 .venv/bin/python tools/performance/run_process.py --measurement /out/b3-profile-process.json \
  --script portfolio_tape -- --tape /tape/multi --symbols $SYMBOLS --bars 2160 --with-funding \
  --profile /out/b3.pstats --output /out/b3-profile.json > /dev/null
.venv/bin/python -c "import pstats; pstats.Stats(\"/out/b1.pstats\").sort_stats(\"cumulative\").print_stats(60)" > /out/b1-profile-top.txt
.venv/bin/python -c "import pstats; pstats.Stats(\"/out/b1.pstats\").sort_stats(\"tottime\").print_stats(40)" > /out/b1-profile-self.txt
.venv/bin/python -c "import pstats; pstats.Stats(\"/out/b3.pstats\").sort_stats(\"cumulative\").print_stats(60)" > /out/b3-profile-top.txt
cat /sys/fs/cgroup/memory.events > /out/memory-events.txt
cat /sys/fs/cgroup/cpu.stat > /out/cpu-stat.txt
rm -f /out/*.pstats'

# --- 5. Data-size and instrument-count scaling.
run scaling sh -c '
set -eu
SYMBOLS='"$SYMBOLS"'
.venv/bin/python tools/performance/ensemble_profile.py --tape /tape/q1 --bars 2160 --output /tmp/warm.json > /dev/null
for bars in 720 1440 2160; do
  for n in 1 2 3; do
    timeout 300 .venv/bin/python tools/performance/run_process.py --measurement /out/dur-$bars-$n-process.json \
      -- --tape /tape/q1 --bars $bars --output /out/dur-$bars-$n.json > /dev/null
  done
done
for count in 1 2 5 10; do
  subset=$(echo $SYMBOLS | tr "," "\n" | head -$count | paste -sd, -)
  for n in 1 2 3; do
    timeout 900 .venv/bin/python tools/performance/run_process.py --measurement /out/inst-$count-$n-process.json \
      --script portfolio_tape -- --tape /tape/multi --symbols $subset --bars 2160 --with-funding \
      --output /out/inst-$count-$n.json > /dev/null
  done
done'

# --- 6. BenchmarkRunner (result JSON + verified forensic HTML) for serialisation cost.
run research sh -c '
set -eu
timeout 300 .venv/bin/python tools/performance/run_process.py --script research \
  --measurement /out/q1-research-process.json \
  -- --tape /tape/q1 --bars 2160 --artifacts /out/q1-artifacts --output /out/q1-research.json > /dev/null
du -sb /out/q1-artifacts/* > /out/artifact-sizes.txt'

# --- 7. Cold-start control (first process in a fresh container vs warm).
run control sh -c '
set -eu
for n in 1 2 3; do
  timeout 300 .venv/bin/python tools/performance/run_process.py --measurement /out/b1-$n-process.json \
    -- --tape /tape/q1 --bars 2160 --output /out/b1-$n.json > /dev/null
done
timeout 300 .venv/bin/python tools/performance/memory_timeline.py --timeline /out/b1-t025.csv \
  --measurement /out/b1-t025-process.json --interval 0.25 \
  --script tools/performance/ensemble_profile.py -- --tape /tape/q1 --bars 2160 --output /out/b1-t025-child.json > /dev/null'

# --- 8. Regenerate the CSV artifacts from the evidence just produced.
.venv/bin/python tools/performance/assemble_report.py --evidence "$EVIDENCE" --out "$WORK/assembled"

printf '\nEvidence and regenerated CSVs: %s\n' "$WORK"
printf 'Compare with reports/performance/{measurements,memory_timeline}.csv.\n'
printf 'hotspots.csv and PERFORMANCE_REPORT.md carry analysis and are not regenerated.\n'
