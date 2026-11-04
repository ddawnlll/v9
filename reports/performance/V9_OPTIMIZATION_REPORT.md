# V9 Optimization Report — Eliminating Quadratic Recalculation

Branch: `codex/perf-quadratic-elimination` · Final SHA: `3518a6a`
Base for ratios: `bfa7613` (pre-optimisation, the same stage the prior report's §12 uses as "before")
Prior report: `reports/performance/PERFORMANCE_REPORT.md` (unchanged, still the container-benchmark record)

Mission: make V9 substantially faster and more memory-efficient **without changing trading behavior,
scientific validity, or execution safety**. Python only; no framework replacements; no dropped
experts, records, or validation.

## Executive summary

| Workload (host medians, 3 fresh processes after warmup) | Before (`bfa7613`) | After (`3518a6a`) | Improvement |
|---|---|---|---|
| B0 wall — 744 bars × 1 leg | 0.830 s | 0.463 s | **1.79×** |
| B0 engine (Nautilus `run`) | 0.608 s | 0.277 s | **2.19×** |
| B1 wall — 2,160 bars × 1 leg | 1.903 s | 0.794 s | **2.40×** |
| B1 engine | 1.715 s | 0.596 s | **2.88×** |
| B1 CPU time (user+sys) | 2.551 s | 0.904 s | **2.82×** |
| B1 peak RSS | 187.7 MiB | 184.2 MiB | −1.9% |
| B1 throughput | 1,260 bars/s | 3,624 bars/s | **2.88×** |
| B3 wall — 2,160 bars × 10 legs | 9.454 s | 3.961 s | **2.39×** |
| B3 engine | 9.153 s | 3.493 s | **2.62×** |
| B3 peak RSS | 449.0 MiB | 416.3 MiB | **−7.3%** |
| B3 throughput | 2,360 leg-bars/s | 6,184 leg-bars/s | **2.62×** |
| S2160 wall (2025 tape) | 1.844 s | 0.933 s | **1.98×** |
| S4320 wall | 4.776 s | 1.627 s | **2.94×** |
| S8640 wall | 13.224 s | 3.206 s | **4.12×** |
| S8640 engine | 12.996 s | 2.950 s | **4.41×** |
| S8640 peak RSS | 264.8 MiB | 256.2 MiB | −3.3% |

Per-bar cost as history grows 2,160 → 8,640 bars:

| Metric | Before | After |
|---|---|---|
| ms / leg-bar (2,160) | 0.76 | 0.34 |
| ms / leg-bar (4,320) | 1.04 | 0.32 |
| ms / leg-bar (8,640) | 1.50 | 0.34 |
| per-bar growth over 4× history | **1.97× (quadratic signature)** | **1.00× (flat)** |

Verdict on the goals: the **3× primary target is met on engine time at ≥2× history (3.3–4.4×)
and on end-to-end wall at 4× history (4.1×)**. On the short dev-window walls (B0/B1/B3) we reach
**2.4×**, not 3×: there the fixed costs (interpreter startup ~0.16 s, the policy-protected
28-stance decisions evidence log, and the 28-Python-observer-per-bar floor) dominate, and the
remaining per-bar work is either inherent (P&F Decimal loop under a changing box) or explicitly
protected (all 28 experts evaluate every bar; every stance is recorded). §F ranks what is left.

No behavior change: B0/B1 parity 16/16 sections byte-identical, B3 parity 30/30 sections
byte-identical against the frozen manifests; 1,089 unit tests pass; ruff + mypy clean.

## 1. Baseline and method

- **Base commit.** Work started at `9b0cba7` on `codex/performance-forensics`, branched to
  `codex/perf-quadratic-elimination`. Ratios use `bfa7613` (pre-optimisation) vs `3518a6a`
  (final), each checked out as a clean worktree and measured back-to-back.
- **Environment.** Host Apple M3 Pro, macOS, Python 3.12.12, Polars 1.44.1, NautilusTrader
  2.0.0rc4 — the 2 vCPU / 4 GiB Linux measurement container from the prior report was deleted
  at the user's request before this task, so **all absolute numbers are host numbers** and only
  **ratios measured back-to-back on one machine** are claimed. Thread limits from the prior
  report were kept (`POLARS_MAX_THREADS=OMP_NUM_THREADS=OPENBLAS_NUM_THREADS=2` where set).
- **Workloads.** B0/B1/B3 reuse the prior report's tape staging (Binance 1h + fundingRate,
  2025-01-01→2025-04-01 dev window, 60/60 archives checksum-verified) and its harnesses
  (`tools/performance/ensemble_profile.py`, `portfolio_tape.py`, `parity_capture.py`,
  `parity_diff.py`). Scaling runs use the 2025 full-year BTC tape (`/tmp` staging, first
  2,160 / 4,320 / 8,640 bars = 1×/2×/4× history, oldest-first, same experts and config).
- **Protocol.** One discarded warmup, then three fresh processes per workload per tree;
  medians reported (ranges in §A). Wall = child process end to end (includes interpreter
  startup, backtest, output, teardown); engine = Nautilus `engine.run` inclusive of all Python
  callbacks; CPU = `resource` user+sys; RSS = child high-water mark. Instrumented runs
  (cProfile, expert trace) are reported separately and never as timings.
- **Equivalence.** Canonical decision traces before/after via the frozen parity manifests
  (`reports/performance/parity/baseline/`), plus targeted fuzz harnesses and the new
  `tests/test_incremental_engine_parity.py` gate (see §D).

## 2. Before vs. after (section A)

### A.1 End-to-end (medians of 3; ranges in parentheses)

| Metric | Before | After | Improvement |
|---|---|---|---|
| B0 wall (744 bars) | 0.830 s (0.786–0.874) | 0.463 s (0.459–0.477) | 1.79× |
| B0 engine | 0.608 s (0.586–0.629) | 0.277 s (0.272–0.293) | 2.19× |
| B1 wall (2,160 bars) | 1.903 s (1.842–2.080) | 0.794 s (0.785–0.806) | 2.40× |
| B1 engine | 1.715 s (1.641–1.884) | 0.596 s (0.596–0.598) | 2.88× |
| B1 CPU | 2.551 s | 0.904 s | 2.82× |
| B1 peak RSS | 187.7 MiB | 184.2 MiB | −1.9% |
| B1 bars/second (engine) | 1,260 | 3,624 | 2.88× |
| B3 wall (21,600 leg-bars) | 9.454 s (9.396–9.512) | 3.961 s (3.642–4.052) | 2.39× |
| B3 engine | 9.153 s (9.096–9.210) | 3.493 s (3.334–3.720) | 2.62× |
| B3 peak RSS | 449.0 MiB (438.6–459.5) | 416.3 MiB (373.3–416.4) | −7.3% |
| B3 leg-bars/second (engine) | 2,360 | 6,184 | 2.62× |
| B3 fills / funding settlements | 203 / 2,690 | 203 / 2,690 | identical |

### A.2 Component breakdown (call counts — profiler-overhead-proof — plus instrumented shares)

| Component | Before (B1, 2,160 bars) | After | Effect |
|---|---|---|---|
| Polars `PyLazyFrame.collect` | 12,288 calls | 5,407 calls | −56%; gone from #1 hotspot |
| `pattern_pivots` full-prefix rolling | 615 full recomputes | 0 (861 engine hits live) | eliminated |
| `wilder_rsi` full-prefix EWM + DataFrame | ~123 full recomputes | 0 (122 engine hits live) | eliminated |
| `trend_emas` / `macd_line` full EWMs | per opportunity bar | 0 (123/122 engine hits live) | eliminated |
| `dataclasses.replace` in hot path | 19,873 calls/tree | 0 (`stance_with` positional) | eliminated |
| RSI recovery `pl.Series` from full tuple | 2 builds / opp bar | 0 (scalar reverse scan) | eliminated |
| P&F Decimal validation rescan | full-prefix `any()` per opp bar | skipped (Candle guarantees) | −1 O(n) Decimal pass |
| P&F core loop | unchanged algorithm | hoisted `-3*box` | constant only (see blocker) |
| `OpportunityBook.on_candle` scan | all episodes every candle | active-index only | O(active) per bar |
| `previous_session_bars` filter | O(n) Python/bar | O(log n) bisect + validation | eliminated |
| Full-prefix-then-tail ops (momentum, swings, mean_range) | O(n) tables/diffs | O(14–20) slices | eliminated |
| 28-wide dispatch | 3 calls/expert/bar | 1 precomputed closure | −1.2M calls / B3 |
| `stance_record` decisions log | 60,480 dicts (B1) | unchanged (required evidence) | floor, kept |
| P&F `_columns_validated` loop | 123 × O(n) Decimal | same (inherent, §F) | now the top hotspot |

### A.3 Attribution (isolated where measured, profile-share otherwise)

- **Pivot/indicator engine + tail-scan batch** (the `ff2aaca` commit): B1 engine 0.898 s → 0.621 s
  (**−31%**, measured end to end on the same tape). This is the quadratic-elimination core:
  per-bar full-prefix Polars/rolling work becomes O(1)-amortized appends.
- **EMA/Wilder/MACD engine** (same commit, second half): 0.621 s → 0.593 s (−5% on B1; the share
  grows with history length — see scaling).
- **Dispatch/witness/stance batch** (`b2367ca`): a further −2–3% on B1/B3 walls; its value is
  mostly visible at scale (1.2M fewer calls per B3).
- The remaining B1 wall (0.79 s) is ~0.20 s fixed startup/fixture plus the policy floor:
  28 Python observers per bar plus the 28-stance evidence log (§F).

## 3. What changed and why (section B, hotspot order)

1. **P&F (former 36%)** — `src/v8_next/experts/pandf.py`. Skipped the duplicate full-prefix
   Decimal validation (frame candles already guarantee positive finite Decimals; the public
   `columns()` keeps its validation for direct callers, fuzz-pinned over 5,000 cases) and
   hoisted the loop-invariant `-3*box`. **Blocker documented:** exact incremental P&F is
   impractical because the box (mean of the latest 14 ranges) *never* repeats — measured 0.0%
   consecutive-repeat rate over 2,141 real bars — so every bar must reinterpret the full history
   under a new box. No box freezing, no float approximation, no threshold change.
2. **Full-prefix Polars (former 17%)** — new `src/v8_next/experts/engine.py` (`FeatureEngine`,
   one per instrument leg, threaded through `observe_all_28` on the per-bar bundle):
   append-only confirmed-pivot lists per (side, strength) with newly-confirmable-index-only
   extension (exact by construction — pivot confirmation needs full windows on both sides, and
   per-index max/min over identical doubles reproduces the rolling kernels bit-for-bit);
   O(1) EMA/Wilder/MACD via form-B updates (`prev + α*(x-prev)`), proven bit-exact vs Polars
   in 15,000/15,000 fuzz trials (magnitudes 0.01–60,000, horizons to 2,160 bars). Handover is
   self-verifying: EMA endpoints checked against the legacy outputs, the Wilder rebuild checked
   against the legacy endpoint, any mismatch discards state and falls back. The seed is taken
   from Polars, never from a Python resum (magnitude-fragile — measured).
3. **Patterns (former 14%)** — `pattern_pivots`/`significant_swings` serve from the engine;
   `measuring`/`fibonacci`/`divergence` inherit it through `pattern_pivots`. Variant B/C
   definitions, precedence, equal-price strictness, invalidation, and confirmation timing are
   unchanged (300-bar growing-prefix replay agreement + B1/B3 byte parity).
4. **Wilder RSI (former 11%)** — retained full RSI history (arbitrary-index callers like
   divergence keep working), O(1) average updates, SMA seed preserved, flat/gain-only/loss-only
   branches identical (fuzz over flat/up/down/volatile/tiny regimes × 400 bars).
5. **OpportunityBook (former 5%)** — per-instrument active index with O(1) swap-remove,
   insertion-ordered transitions, price-before-TTL priority preserved, terminal episodes kept
   for audit, re-admission handled.
6. **Bookkeeping** — RSI recovery reverse-scan (12k-case fuzz), session-window bisection
   (4,000-calendar fuzz), tail-slice fixes, precomputed 28-wide dispatch closures (20k-case
   witness fuzz), positional `stance_with`, single-pass stance partition. `dataclasses.replace`
   is gone from every hot path.

## 4. Scaling (section C)

Single-leg BTC 2025 tape, oldest-first prefixes (same experts, config, machine):

| Bars | Before engine | After engine | Engine × | Before wall | After wall | Wall × |
|---|---|---|---|---|---|---|
| 2,160 | 1.643 s | 0.725 s | 2.27× | 1.844 s | 0.933 s | 1.98× |
| 4,320 | 4.511 s | 1.387 s | 3.25× | 4.776 s | 1.627 s | 2.94× |
| 8,640 | 12.996 s | 2.950 s | 4.41× | 13.224 s | 3.206 s | 4.12× |

Per-bar cost: before 0.76 → 1.04 → 1.50 ms (1.97× growth per 4× history — the O(N²)
signature); after 0.34 → 0.32 → 0.34 ms (**flat**). Peak RSS at 8,640 bars: 264.8 → 256.2 MiB.

Remaining superlinear residue (small): the P&F Decimal loop on opportunity bars (inherent —
box changes every bar), the `parent.candles + (candle,)` tuple copy per bar (price of the
immutable-frame contract, ~6% at 8,640 bars), and the retained RSI-tuple materialization per
opportunity bar (pointer copies only). Multi-instrument scaling stays linear in legs (B3 =
10 × single-leg cost plus fixed funding/account overhead).

## 5. Correctness (section D)

- **Trace equivalence (performance-only change, proven):** B0 16/16, B1 16/16, B3 30/30
  manifest sections byte-identical vs the frozen baselines — every expert stance, opportunity
  creation/transition, trading decision, order, fill, accounting, and PnL. No categorical,
  timestamp, identity, or order deviation; no numeric deviation at all (digests, not tolerances).
- **Tests:** 1,089 passed / 66 skipped / 0 failed (default); 75 + 14 + 8 passed in the
  slow-marked perf/parity/hot-path gates; new `tests/test_incremental_engine_parity.py`
  (5 tests: recovery scan, P&F core, session windows, pivot replay, EMA/RSI replay).
- **Fuzz:** 9,000 + 3,000 (RSI recovery), 5,000 (P&F), 4,000 (sessions), 1,500×3 (swings),
  20,000×28 (witness fusion), 15,000 (EMA form-B), 300×8 + 5×400×3 (engine replay) — zero
  mismatches; two harness-side expectation bugs found and fixed, no source bugs.
- **Gates:** `ruff check src tests` clean; `mypy src` clean (209 files).
- **Semantics intentionally unchanged:** 28 experts, MarketBelief/causal boundaries,
  opportunity lifecycle, STOP-FIRST, fees/funding/slippage, native execution, no look-ahead,
  full audit evidence (the decisions log format is untouched).

## 6. Codebase impact (section E)

- **Dependencies:** none added, none removed (Polars/Nautilus stay; no NumPy/Numba needed —
  the wins are algorithmic, and per-bar native kernels were already fast enough).
- **Diff:** +906/−121 across 13 files (`src/` × 12 + `tools/performance/expert_trace.py`),
  plus one new module (`experts/engine.py`, 393 lines, soundness contract in its docstring)
  and one new test file. Two commits (`ff2aaca`, `b2367ca`, `3518a6a`), logically separated.
- **Maintenance tradeoffs:** the engine is fallback-first (any unprovable state → legacy
  full compute, no behavior delta possible); handover is self-verifying (endpoint checks +
  discard); producers never mutate adopted caches (documented invariant, asserted by the
  alignment checks in `extend_causal_frame`). Cost: ~400 lines of stateful machinery plus
  per-key pivot lists (bounded, O(pivots)) and O(n) retained float triplets per leg (<1 MiB
  at 8,640 bars) — the decisions evidence log still dominates RSS.
- **Live/replay parity:** live feeds take the `frame_at` path (no numeric cache), so the
  engine never engages live — live behavior is the legacy path bit-for-bit; replay uses the
  engine only on identity-proven prefixes with byte-identical output (parity-gated).

## 7. Remaining bottlenecks (section F, by measured share)

1. **Per-bar 28-expert Python dispatch + decisions evidence log** (~40–50% of B3 engine).
   604,800 observer evaluations + 604,800 stance records per B3 are the price of the
   "evaluate all 28 experts, record every stance" policy. Further gains need contract changes
   (fewer evaluations, binary evidence) — out of scope for a behavior-preserving task.
2. **P&F Decimal loop** (top per-opportunity-bar cost, grows with history). Exact incremental
   blocked on the 0%-repeat box (measured §B.1). Only a semantic change (frozen box, tick
   quantization) would move it — explicitly not done.
3. **Opportunity-bar native kernels** (`macd_stoch`, `band_setup` full-prefix rolling/masks).
   Windowable only with unbounded run-start walkbacks; poor value/risk — skipped.
4. **Immutable-frame tuple copy** (`parent.candles + (candle,)` per bar, ~6% at 8,640 bars).
   Structural (persistent-vector redesign) — skipped.
5. **Fixed startup** (~0.16 s imports + ~0.04 s fixture per process) — bounds short-run walls
   at ~4–5× regardless of engine work.

## 8. Reproduction

```sh
# Stage tape (sibling checkout holding the vendor archives)
mkdir -p /tmp/v9perf/tape-q1 /tmp/v9perf/tape-multi
cp <src>/BTCUSDT-1h-2025-0{1,2,3}.zip /tmp/v9perf/tape-q1/
# (multi: all 10 symbols × 01..03 klines + fundingRate, 60 zips)
# Before/after (same machine, back to back):
git worktree add -f --detach /tmp/v9perf/tree-bfa bfa7613
for i in 1 2 3; do PYTHONPATH=src .venv/bin/python tools/performance/ensemble_profile.py \
  --tape /tmp/v9perf/tape-q1 --bars 2160 --output /tmp/after-b1-$i.json; done
for i in 1 2 3; do PYTHONPATH=/tmp/v9perf/tree-bfa/src .venv/bin/python \
  tools/performance/ensemble_profile.py --tape /tmp/v9perf/tape-q1 --bars 2160 \
  --output /tmp/before-b1-$i.json; done
# Parity (must print PARITY, 16/16 and 30/30):
PYTHONPATH=src:tools/performance .venv/bin/python tools/performance/parity_capture.py \
  --tape /tmp/v9perf/tape-q1 --bars 2160 --workload B1 --manifest /tmp/check-b1.json
.venv/bin/python tools/performance/parity_diff.py \
  --left reports/performance/parity/baseline/B1.json --right /tmp/check-b1.json
```

## Answer

**We saved 5.5 s wall (9.45 → 3.96 s) on the 10-leg run and 1.1 s (1.90 → 0.79 s) on the
single-leg run — 2.4× end-to-end on dev-window workloads, 4.1× wall (13.2 → 3.2 s) at 4×
history with per-bar cost now flat (0.34 ms) instead of doubling. RAM saved: ~33 MiB peak on
B3 (449 → 416 MiB, −7%), roughly unchanged elsewhere — the retained engine state is <1 MiB
per leg; the decisions evidence log dominates. The biggest bottleneck now is the
policy-protected per-bar floor itself: 28 Python expert evaluations plus the 28-stance
evidence log every bar (~half the runtime), followed by the exact P&F Decimal loop whose
incremental form is blocked by the every-bar-changing box.**

*Skipped / not checked:* a generic backtesting framework; Rust/C++/Go rewrites; replacing
Nautilus/Polars; new dependencies; live-trading validation (replay-only task); the deleted
2 vCPU container (ratios-only claims, §1); alternative semantics (frozen P&F box, dropped
experts/records) — all rejected for behavior or cost reasons. Risk to know: the engine's
soundness rests on cache immutability (documented, check-guarded) — any future cache
mutation must re-prove the adopt path.
