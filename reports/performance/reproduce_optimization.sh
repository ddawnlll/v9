#!/bin/sh
# Reproduce the optimisation A/B locally — no Docker required.
#
# The benchmark in section 1-11 of PERFORMANCE_REPORT.md was measured inside a
# 2 vCPU / 4 GiB Linux container. That container was deleted at the user's
# request, so this script measures the optimisations on the host instead and its
# figures are NOT comparable with the container's absolute numbers: it exists to
# reproduce the *ratio* between the pre-optimisation tree and the optimised tree
# on one machine, back to back.
#
# Usage: sh reports/performance/reproduce_optimization.sh [tape_root]
set -eu
REPO=$(cd "$(dirname "$0")/../.." && pwd)
TAPE_ROOT=${1:-/tmp/v9-perf-benchmark/tape}
PY=$REPO/.venv/bin/python
OUT=${OUT:-/tmp/v9-optimization-ab}
BASE_SHA=${BASE_SHA:-bfa7613}
STAGES=${STAGES:-"$BASE_SHA 278d404 bc68f49 HEAD"}
SYMS=BTCUSDT,ETHUSDT,BNBUSDT,SOLUSDT,XRPUSDT,ADAUSDT,DOGEUSDT,AVAXUSDT,LINKUSDT,LTCUSDT

for required in "$TAPE_ROOT/jan" "$TAPE_ROOT/q1" "$TAPE_ROOT/multi"; do
  [ -d "$required" ] || { echo "missing tape staging: $required (see reproduce.sh steps 1-2)" >&2; exit 1; }
done

mkdir -p "$OUT"
for sha in $STAGES; do
  tree=$OUT/tree-$sha
  [ -d "$tree" ] || git -C "$REPO" worktree add -f --detach "$tree" "$sha" >/dev/null
  # Warm-up runs are discarded: the first process after a checkout pays import
  # and page-cache costs that are not the steady state under comparison.
  ( cd "$tree" && PYTHONPATH=$tree/src $PY tools/performance/ensemble_profile.py \
      --tape "$TAPE_ROOT/q1" --bars 2160 --output /tmp/warm-b1.json >/dev/null
    cd "$tree" && PYTHONPATH=$tree/src $PY tools/performance/portfolio_tape.py \
      --tape "$TAPE_ROOT/multi" --symbols "$SYMS" --bars 2160 --with-funding \
      --output /tmp/warm-b3.json >/dev/null )
  for run in 1 2 3; do
    ( cd "$tree"
      PYTHONPATH=$tree/src $PY tools/performance/run_process.py \
        --measurement "$OUT/$sha-b1-$run.json" -- \
        --tape "$TAPE_ROOT/q1" --bars 2160 --output /tmp/ab-b1.json >/dev/null
      PYTHONPATH=$tree/src $PY tools/performance/run_process.py \
        --measurement "$OUT/$sha-b3-$run.json" --script portfolio_tape -- \
        --tape "$TAPE_ROOT/multi" --symbols "$SYMS" --bars 2160 --with-funding \
        --output /tmp/ab-b3.json >/dev/null )
  done
  echo "measured $sha"
done

# Byte-level parity against the frozen pre-optimisation manifests.
for workload in B0 B1 B3; do
  case $workload in
    B0) args="--tape $TAPE_ROOT/jan --bars 744" ;;
    B1) args="--tape $TAPE_ROOT/q1 --bars 2160" ;;
    B3) args="--tape $TAPE_ROOT/multi --symbols $SYMS --bars 2160 --with-funding" ;;
  esac
  # shellcheck disable=SC2086
  $PY "$REPO/tools/performance/parity_capture.py" $args --workload "$workload" \
    --manifest "$OUT/parity/$workload.json" >/dev/null
  $PY "$REPO/tools/performance/parity_diff.py" \
    --left "$REPO/reports/performance/parity/baseline/$workload.json" \
    --right "$OUT/parity/$workload.json"
done

$PY - "$OUT" <<'PY'
import glob, json, statistics, sys
out = sys.argv[1]
rows = {}
for path in sorted(glob.glob(f"{out}/*-b[13]-*.json")):
    sha = path.rsplit("/", 1)[-1].split("-")[0]
    workload = path.rsplit("/", 1)[-1].split("-")[1]
    rows.setdefault((sha, workload), []).append(json.load(open(path))["child_wall_s"])
base = {w: statistics.median(v) for (s, w), v in rows.items() if s == sorted({s for s, _ in rows})[0]}
for (sha, workload), wall in sorted(rows.items()):
    print(f"{workload} {sha:9s} median {statistics.median(wall):6.3f}s  "
          f"cumulative {base[workload] / statistics.median(wall):5.2f}x")
PY
