#!/usr/bin/env bash
# End-to-end replay of the V9 cross-market validation.
#
# Run this from a clean worktree of the frozen candidate. The measurement itself
# takes ~25 min on an 8-core arm64 machine at ~400 MiB peak RSS per process.
#
#   git worktree add /tmp/v9val -b validation/cross-market-run 09908af840864a20e5bc4836ea4d21f4ceb93b35
#   cd /tmp/v9val && ./reports/v9-cross-market-validation/REPRODUCE.sh
#
# Environment:
#   ARCHIVE_DIR          directory holding the 180 monthly archives + .CHECKSUM files
#   SKIP_FETCH=1         reuse archives without re-downloading
#   SYMBOLS              override the universe (space separated)
#
# No step places an order, reads a credential or reaches a trading endpoint. The
# only network access is downloading public archives from data.binance.vision.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT"

PY="uv run --locked --extra dev python"
OUT="reports/v9-cross-market-validation"
DATA="$OUT/data"
FROZEN="09908af840864a20e5bc4836ea4d21f4ceb93b35"
export PYTHONUNBUFFERED=1

echo "== 0. provenance =="
echo "frozen candidate : $FROZEN"
git rev-parse HEAD
python -c "import nautilus_trader, sys; print('engine', nautilus_trader.__version__, 'python', sys.version.split()[0])"

echo "== 1. archives: verify against Binance's published SHA-256, fetch failures =="
if [[ "${SKIP_FETCH:-0}" != "1" ]]; then
  : "${ARCHIVE_DIR:?set ARCHIVE_DIR to the archive directory}"
  ARCHIVE_DIR="$ARCHIVE_DIR" $PY tools/cross_market_fetch_archives.py
fi
wc -l < "$OUT/archive_hashes.txt"   # expect 180

echo "== 2. build the 20 hash-verified tapes =="
$PY tools/cross_market_build_tapes.py

echo "== 3. data audit: gaps, duplicates, continuity, funding coverage, derivable H4 =="
$PY tools/cross_market_data_audit.py

echo "== 4. frozen-baseline parity (must match the four documented BTCUSDT Q1 arms) =="
$PY - <<'PY'
from pathlib import Path
from v8_next.adapters.adaptive_strategy import SimArmConfig
from v8_next.evaluation.sim_arm import run_sim_arm, run_baseline_arm

tape = Path("research/tape/btcusdt-1h-2025Q1")
m15 = Path("research/tape/btcusdt-15m-2025Q1")
base = SimArmConfig(enabled=True)
legacy = SimArmConfig(enabled=True, horizon_profiles_enabled=False, adaptive_management_enabled=False)
static = SimArmConfig(enabled=True, adaptive_management_enabled=False)
expected = {
    "A": ("4f53cda18c2baa0c", "a2e716f7577c5e7a", -21.234951),
    "B": ("a874dbbd164b0cd4", "a420388cde77e9fe", -23.102285),
    "C": ("6c3c541980c02fc5", "a25c8c077a69ac38", -13.880205),
    "D": ("4aa9cdfce051ce5f", "a901ba717f922371", 20.162531),
}
runs = [
    ("A", run_baseline_arm, dict(tape=tape, config=base)),
    ("B", run_sim_arm, dict(tape=tape, config=legacy)),
    ("C", run_sim_arm, dict(tape=tape, config=static, intraday_tape=m15)),
    ("D", run_sim_arm, dict(tape=tape, config=base, intraday_tape=m15)),
]
ok = True
for label, runner, kwargs in runs:
    m = runner(**kwargs)
    dec, trade, pnl = expected[label]
    good = (
        m.decisions_digest[:16] == dec
        and m.trade_digest[:16] == trade
        and round(m.net_pnl, 6) == pnl
    )
    ok &= good
    print(f"{label}: net_pnl={m.net_pnl:.6f} trade_digest={m.trade_digest[:16]} parity={good}")
raise SystemExit(0 if ok else 1)
PY

echo "== 5. four-arm matrix, 10 symbols, one symbol per process =="
printf '%s\n' ${SYMBOLS:-ETHUSDT SOLUSDT BNBUSDT XRPUSDT DOGEUSDT ADAUSDT AVAXUSDT LINKUSDT DOTUSDT LTCUSDT} \
  | xargs -P 2 -I{} $PY tools/cross_market_matrix.py --symbols {}

echo "== 6. management ablations + cost stress, 10 symbols =="
printf '%s\n' ${SYMBOLS:-ETHUSDT SOLUSDT BNBUSDT XRPUSDT DOGEUSDT ADAUSDT AVAXUSDT LINKUSDT DOTUSDT LTCUSDT} \
  | xargs -P 3 -I{} $PY tools/cross_market_ablations.py --symbols {}

echo "== 7. five-grammar opportunity census (research only, no orders) =="
mkdir -p "$DATA/census"
for s in ${SYMBOLS:-ETHUSDT SOLUSDT BNBUSDT XRPUSDT DOGEUSDT ADAUSDT AVAXUSDT LINKUSDT DOTUSDT LTCUSDT}; do
  lower="$(echo "$s" | tr 'A-Z' 'a-z')"
  $PY -m v8_next.evaluation.grammar_census \
      --tape "research/tape/$lower-1h-2025H1" --symbol "$s" \
      --instrument-id "$s-PERP.BINANCE" --out "$DATA/census/$s.json"
done

echo "== 8. synthesis: tables, statistics, benchmark =="
$PY tools/cross_market_analysis.py

echo "== 9. HTML dashboard (self-contained, no JavaScript) =="
$PY tools/cross_market_dashboard.py

echo "== 10. configuration freeze =="
$PY tools/cross_market_freeze.py

echo "done: $OUT"
