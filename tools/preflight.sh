#!/usr/bin/env bash
# Pre-flight gate for v8-next benchmark runs. Cheap (<60s). Run BEFORE any
# full-window battery: catches env/window/tape mistakes in seconds instead
# of wasting a 30-minute engine chain.
#
# Usage: bash tools/preflight.sh [--bars N] [--start-bar M] [--smoke]
set -euo pipefail

BARS=385
START_BAR=0
SMOKE=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --bars) BARS="$2"; shift 2;;
    --start-bar) START_BAR="$2"; shift 2;;
    --smoke) SMOKE=1; shift;;
    *) echo "unknown arg $1" >&2; exit 2;;
  esac
done

cd "$(dirname "$0")/../.."  # repo root (~/src/v8)
echo "=== preflight bars=$BARS start=$START_BAR ==="

# 1. Research extra present (arch/scipy). A bare `uv sync` prunes them and
#    silently downgrades every statistic to UNSUPPORTED.
uv run --project . --extra research python -c \
  "import arch, scipy, nautilus_trader; print('deps ok:', arch.__version__)" \
  || { echo "FATAL: research extra missing; run: uv sync --project . --locked --extra dev --extra research" >&2; exit 1; }

# 2. Window math: PBO needs intervals%4==0 and >=8 (24-bar intervals).
INTERVALS=$(( (BARS - 1) / 24 ))
if (( INTERVALS < 8 )) || (( INTERVALS % 4 != 0 )); then
  echo "FATAL: bars=$BARS -> $INTERVALS intervals; PBO needs n%4==0,n>=8 (e.g. 385->16, 4321->180)" >&2
  exit 1
fi
echo "window ok: $INTERVALS daily intervals (PBO eligible)"

# 3. Wiring closure (#476 R3): every capability module must have an importer outside
#    its own package and outside tests. Fails closed and runs before anything that
#    needs a tape, so a wiring regression is caught in seconds rather than after a
#    full window. The current backlog is tracked on #451-#454; run the tool directly
#    (`uv run --project . python tools/audit_wiring.py`) for the list.
echo "--- wiring closure (#476 R3)"
uv run --project . python tools/audit_wiring.py || {
  echo "FATAL: capability modules have no consumer outside their own package/tests." >&2
  echo "       Every module in the list above is a requirement that was never wired;" >&2
  echo "       #476 R1 tracks the backlog on #451-#454. A new orphan is a regression." >&2
  exit 1
}

# 4. Tapes present with expected sizes.
for t in research/tape/quad-1h-12m/tape.jsonl; do
  [[ -s "$t" ]] || { echo "FATAL: missing $t" >&2; exit 1; }
done
echo "tape ok"

# 5. Dirty-tree warning (never blocks, but recorded).
if [[ -n "$(git status --porcelain -- src tests 2>/dev/null)" ]]; then
  echo "WARN: dirty v8-next tree:"
  git status --porcelain -- src tests | head -n 10
fi

# 6. Optional 120-bar smoke through the real quad engine path.
if (( SMOKE )); then
  uv run --project . --extra dev --extra research pytest -q \
    tests/test_portfolio_benchmark.py -k "not end_to_end" || exit 1
fi

echo "PREFLIGHT PASS"
