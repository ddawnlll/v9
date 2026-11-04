# V9 backtesting performance and memory benchmark

Measured on `codex/performance-forensics` at base commit `a4fa50d`, inside an enforced
2-vCPU / 4-GiB Linux cgroup (ARM Linux container on an Apple M3 Pro). All numbers below come
from files under `evidence/`; nothing here is estimated unless it is labelled `ESTIMATE`.

Scope: the **real production path** — `run_expert_strategy_backtest` (the 28-expert ensemble on
NautilusTrader) for one instrument, and `run_portfolio_backtest` (the same ensemble across legs
through one shared Nautilus account, with funding settlements) for ten instruments — replaying
real Binance hourly tape from the monograph's BTC development window, 2025-01-01 → 2025-04-01.

---

## Executive summary

| Question | Answer (measured) |
|---|---|
| Full backtest duration, 1 instrument / 3 months (2,160 bars) | **2.795 s** median end-to-end child wall (min 2.761, max 2.804; P95 = 2.804) |
| Full backtest duration, 10 instruments / 3 months (21,600 bars) | **13.728 s** median (min 13.612, max 15.268; P95 = 15.268) |
| Full backtest duration, 1 instrument / 1 month (744 bars) | **1.074 s** median (min 1.063, max 2.414 — the max is the cold first process in a fresh container) |
| Engine-only duration (Nautilus `engine.run`, 3-month single leg) | **2.375 s = 82.65 %** of an instrumented 2.874 s child (inclusive of all Python strategy callbacks) |
| Average RAM (time-weighted, 3-month single leg) | **216.5 MiB** (median 231.0 MiB) |
| Average RAM (10 instruments / 3 months) | **416.8 MiB** (median 437.0 MiB) |
| Median RAM | 231.0 MiB single leg; 437.0 MiB ten legs |
| P95 RAM | 238.2 MiB single leg; 523.8 MiB ten legs |
| Maximum RAM (OS high-water mark) | **240.7 MiB** single leg; **546.9 MiB** ten legs |
| Average CPU utilisation | **53.2 %** of the 2-vCPU quota (single leg); **50.9 %** (ten legs) |
| Bars/s throughput | **772.8 bars/s** single leg; **1,573.4 leg-bars/s** over ten legs (21,600 bars in 13.7 s) |
| Five largest bottlenecks (pre-optimisation) | 1) per-expert full-history indicator recomputation 62.5 %; 2) full-prefix `CausalFrame` construction with cache re-validation 19.5 %; 3) full-prefix `continuous` scan 18.2 %; 4) full-prefix `__post_init__` validation 9.1 % (self); 5) per-expert dispatch/scan-helper machinery 16.9 % (self, combined) |

> **After optimisation (§12)**: the same workloads now run **1.70×** (single leg) and **2.36×**
> (ten legs) faster with byte-identical output, and ten-leg peak RSS is 9.8 % lower. Those figures
> were measured on the host after the measurement container was removed, so they are a *ratio*, not
> a revision of the table above — see §12.1.

Three findings dominate everything else:

1. **The engine is not the cost.** Native `BacktestEngine.run` is 22.074 s inclusive against
   21.818 s inside the Python `on_bar` callback in the profiled ten-leg run: about **1.1 %** of
   engine time is native dispatch and event handling. The rest is our Python code. Do not look at
   NautilusTrader for speedups.
2. **Cost is per-bar full-prefix work, repeated 28 times.** Every bar builds a frame, validates the
   whole prefix, rescans it for continuity, and each of the 28 experts recomputes its indicators
   over the whole prefix. Per-expert *bodies* are almost free — 0.151 s of self time across 28
   experts × 2,160 bars — while 6.01 s of inclusive expert time goes into shared helpers, Polars
   and frame access.
3. **The measured workloads fit a 4-GiB VPS with wide margin; larger universes do not, by
   extrapolation.** 10 instruments × 3 months peaks at 547 MiB with zero OOM and zero CPU
   throttling inside the enforced cgroup. A 40-instrument, 1-year replay is an `ESTIMATE` of
   ~4.3 GiB and therefore **unsupported at 4 GiB**.

---

## 1. What was measured, and how

* **Baseline timing**: fresh child process per repetition, timed from outside by
  `tools/performance/run_process.py` (`RUSAGE_CHILDREN`). It includes interpreter startup, the
  backtest, output and teardown; it excludes the parent's own write.
* **Repetitions**: three per workload. With three samples the nearest-rank P95 equals the maximum,
  so P95 is reported but not treated as a stable tail.
* **Memory**: `tools/performance/memory_timeline.py` samples `/proc/<pid>/statm|status|stat` about
  every 100 ms from outside the child (median gap 0.101 s, max 0.110 s). Samples are joined to the
  child's own phase boundaries through a shared `time.monotonic()` origin, so phases are not guessed.
* **Instrumented runs** (stage trace, cProfile, tracemalloc, per-expert traces) are reported
  separately and never used as baseline timings.
* **Warm vs cold**: the first process in a fresh container is measurably slower — the same B1
  workload took 4.29 s as a container's first run and 2.79–2.80 s afterwards. Triples retain that
  first run as the max and label it.
* **Thread limits**: `POLARS_MAX_THREADS=OMP_NUM_THREADS=OPENBLAS_NUM_THREADS=2`, so CPU figures are
  not inflated by a 10-core host.
* **Provenance**: `environment.json` (base SHA, per-file SHA-256, image digest, cgroup limits,
  dataset window and archive hashes in `evidence/tape-hashes.txt`; 60/60 archives verified against
  their vendor `.CHECKSUM` sidecars).

---

## 2. Runtime waterfall

Non-overlapping phases from a `sys.monitoring` stage trace of the unchanged
`run_expert_strategy_backtest` on the 3-month single-leg tape (`evidence/profile/b1-stage*.json`,
reconciled in `measurements.csv`, case `B1-stage-trace`). The instrumented child wall is 2.874 s —
**not** a baseline.

| Phase | Wall (s) | % of instrumented child |
|---|---:|---:|
| Script imports after the internal timer starts | 0.2050 | 7.13 % |
| Tape load + `Candle` construction | 0.0564 | 1.96 % |
| Instrument setup | 0.0002 | 0.01 % |
| Bar conversion to native `Bar` | 0.0041 | 0.14 % |
| Execution profile resolution | 0.0000 | 0.00 % |
| **Engine initialisation** (`BacktestEngine(...)` + venue) | 0.1029 | 3.58 % |
| `engine.add_instrument` | 0.0001 | 0.00 % |
| **`engine.add_data(bars)`** | 0.0007 | 0.03 % |
| Strategy setup (source map, prefix cache) | 0.0025 | 0.09 % |
| **`engine.run()`** (inclusive of every Python callback) | 2.3752 | **82.65 %** |
| Economic state extraction | 0.0004 | 0.01 % |
| Native report extraction | 0.0117 | 0.41 % |
| Fill-report extraction | 0.0018 | 0.06 % |
| Result assembly | 0.0000 | 0.00 % |
| `engine.dispose()` | 0.0001 | 0.01 % |
| Interpreter startup + output + teardown (unattributed) | 0.1085 | 3.77 % |
| **Instrumentation inconsistency** | 0.2580 | — |

The last row is not a phase. `sys.monitoring` inflates the enclosed intervals enough that the stage
sum exceeds the enclosing replay span by 0.258 s (11.5 %). The waterfall is therefore **indicative
and does not reconcile to 100 %**; it is reported that way rather than clamped into a tidier table.

Answers to the natural follow-up — *where inside `engine.run` does the time go?* — are in
§5 (hotspots). `add_data`, disposal, report extraction and serialisation are all noise (<0.5 %).

---

## 3. RAM timeline

Time-weighted statistics over `memory_timeline.csv`; the phase column is the last phase boundary the
sample follows.

| Case | Samples | First sample | Time-weighted mean | Median | P95 | Peak sampled | OS peak (`ru_maxrss`) | Final sample |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| B1 — 1 instrument, 3 months | 29 | 1.4 MiB | **216.5 MiB** | 231.0 MiB | 238.2 MiB | 240.4 MiB | 240.7 MiB | 240.4 MiB |
| B3 — 10 instruments, 3 months | 137 | 4.6 MiB | **416.8 MiB** | 437.0 MiB | 523.8 MiB | 545.9 MiB | 546.9 MiB | 437.0 MiB |

Phase-resolved peaks (same evidence, grouped by phase label):

| Phase | B1 peak | B3 peak |
|---|---:|---:|
| Process spawn (pre-timer) | 1.4 MiB | 4.6 MiB |
| Interpreter + imports of polars/nautilus | 116.6 MiB | 130.0 MiB |
| Tape load and input preparation | 135.2 MiB | 170.0 MiB |
| **Replay (`engine.run`)** | **238.2 MiB** | **545.9 MiB** |
| Post-replay output/teardown | 240.4 MiB | 437.0 MiB |

Reading it honestly:

* The same workload's peak varies by a few MiB between run sets (241.1 MiB in the §4 triple,
  240.7 MiB OS peak here, 241.9 MiB in the §6 sweep). Those are different processes, not
  contradictions; each table cites its own source rows in `evidence/`.
* Imports alone account for ~117 MiB of resident memory before any market data exists.
* The single-leg replay adds ~103 MiB over the loading floor; the ten-leg replay adds ~376 MiB.
  That is 9.2 KiB per additional bar on one leg and ~16.2 KiB per additional leg-bar in the
  ten-leg case (derived from the difference between measured peaks, allocator retention included —
  an upper bound on *retained* growth, not an allocation attribution).
* Memory is **not returned after the single-leg replay** (final sample 240.4 MiB = peak). The
  ten-leg run releases ~109 MiB when the replay returns and the result is serialised (546 → 437 MiB).
* **Memory growth is not attributed to Python vs native.** The one allocation-ownership measurement
  available (prior `tracemalloc` run, `reports/performance/20261104T173240Z/`) saw 52.5 MiB peak
  traced, of which 15.7 MiB was `stance_record` mappings at `economics/decisions.py:75` and 4.1 MiB
  per-stance version strings at `experts/registry.py:663`. Everything else — including all
  Rust/Polars/Arrow allocations — is **UNATTRIBUTED**.
* cgroup counters: `memory.events` recorded `oom 0`, `oom_kill 0`; `cpu.stat` recorded
  `nr_throttled 0`, `throttled_usec 0` across the run set. Swap was capped at 4 GiB and no swap
  usage was observed; no major page faults were recorded in any sample.

---

## 4. CPU, throughput and I/O

| Case | Wall median | User CPU | System CPU | CPU % of 2 vCPU | Bars/s | Decisions | Fills |
|---|---:|---:|---:|---:|---:|---:|---:|
| B0 — 744 bars | 1.074 s | 1.051 s | 0.156 s | 55.5 % | 692.7 | 744 | 1 |
| B1 — 2,160 bars | 2.795 s | 2.618 s | 0.356 s | 53.2 % | 772.8 | 2,160 | 1 |
| B3 — 21,600 bars | 13.728 s | 13.274 s | 0.693 s | 50.9 % | 1,573.4 | 21,600 | 203 |

* **Half the quota is used.** The replay is effectively single-threaded Python work; ~50 % of two
  vCPUs means one busy core plus overhead. There is no spare-thread exploitation to remove.
* 12 threads resident throughout (Polars pool + native runtime), 12.0 % system CPU (B1) — no
  measurable I/O wait. Final sample: 8,495 voluntary / 84 non-voluntary context switches for B1,
  15,771 / 386 for B3, and **0 major page faults in every sample** — the working set fits in RAM and
  nothing was evicted to swap.
* Peak observed CPU utilisation equals the average within sampling noise; no burst above ~56 %.
* Events/decisions throughput: 2,160 decisions in 2.795 s = **773 decisions/s**; the ten-leg case
  reaches 1,573 decisions/s plus 2,690 funding settlements (`scaling-instruments-10`).
* **I/O is negligible.** Tape read for 3 months of hourlies is ~92 KiB compressed / ~2,160 rows;
  input preparation measured 0.0564 s (1.96 %). Result artifacts: 32,577 bytes total
  (`result.json` 17,587 B, `forensic.html` 11,398 B, ledger 3,229 B, trades 363 B), with
  serialisation timed at 0.76 ms (JSON) and 0.72 ms (HTML) — `evidence/research/`. Audit output is
  dominated by in-memory records, not disk.

---

## 5. Top 10 hotspots

`hotspots.csv` holds the machine-readable table. Percentages of a **profiled** run are indicative,
not exact: cProfile inflates Python-heavy code and deflates native code, and nested entries overlap.

| # | Hotspot | Measured | Basis |
|---:|---|---:|---|
| 1 | Per-expert full-history indicator recomputation (`observe_all_28`, 604,800 `observe_expert` calls) | 14.239 s / 62.5 % | inclusive, 22.786 s profiled backtest |
| 2 | `CausalFrame.from_ordered_prefix` prefix-cache re-validation (21,600 calls, two full `to_list()` comparisons + constructor validation per bar) | 4.442 s / 19.5 % | inclusive |
| 3 | `CausalFrame.continuous` full-prefix scan (21,600 calls, 23.3 M generator steps) | 4.147 s / 18.2 % | inclusive |
| 4 | `CausalFrame.__post_init__` full-prefix validation (43,200 calls = 2 per bar) | 1.806 s / 9.1 % | self, 19.834 s profiled total |
| 5 | Dispatch + scan helpers: `context_reason` 3.580 s, `stance_record` 1.379 s, `directional_stance` 1.345 s, `stance_with` 0.735 s, `observe_expert` self 1.190 s (583k–605k calls each) | 3.359 s / 16.9 % (helpers' self time) | self |
| 6 | Polars lazy evaluation per feature request (`PyLazyFrame.collect`, 38,186 calls ≈ 1.8 per bar per leg) | 1.365 s / 6.9 % | self |
| 7 | `pattern_pivots` full-prefix pivot scan (2,007 calls) | 1.161 s / 5.1 % | inclusive |
| 8 | `stance_record` record construction (604,800 mappings; 15.7 MiB retained) | 1.379 s / 6.1 % | inclusive |
| 9 | `opportunity_at` grammar/decision path (21,600 calls, Decimal comparisons) | 0.962 s / 4.2 % | inclusive |
| 10 | Native `BacktestEngine.run` minus `on_bar` — event dispatch and order/fill processing | 0.256 s / **1.1 %** | inclusive minus callback |

Supporting evidence for #1 and #5, which is the single most useful result here:

* Inclusive dispatch time summed over all 28 experts is **6.010 s** across 2,160 bars
  (median expert 134.4 ms; top: `fib_rsi_bb_confluence` 782.8 ms, `divergence_12_setups` 612.3 ms,
  `pandf_breakout` 581.2 ms, `rsi_stoch_reversion` 531.5 ms).
* Exclusive `observer_self` time summed over all 28 experts is **0.151 s** — 2.5 % of that.
  **The expert logic itself is cheap; the shared prefix/frame/feature machinery around it is not.**
* Per-expert attribution is not independent: shared lazily computed features are charged to the
  expert that first asks for them.

Where the time is *not*: startup/imports 7.1 %, tape load 2.0 %, engine init 3.6 %, `add_data`
0.03 %, report/serialisation <0.5 %, `dispose` 0.01 %, native dispatch 1.1 %.

---

## 6. Scaling analysis

**Data size** (same real window, prefix lengths; 3 fresh processes each):

| Bars | Wall median | Bars/s | Peak RSS | Marginal cost of the added 720-bar block |
|---:|---:|---:|---:|---:|
| 720 | 1.107 s | 650.3 | 228.9 MiB | — |
| 1,440 | 1.861 s | 773.7 | 234.3 MiB | +0.754 s |
| 2,160 | 2.837 s | 761.2 | 241.9 MiB | +0.976 s |

Least squares over these three points: `wall ≈ 0.205 s + 1.20 ms/bar`, residuals ±0.07 s. The curve
is close to linear in this range, but the marginal cost per equal 720-bar block rises 29 %, which is
the visible onset of the O(n²) prefix work that hotspots #2–#4 predict. Three points cannot
separate the quadratic term from cache effects; **a longer window would be required to fit it**, and
none exists inside the authorized dev window.

**Instrument count** (2,160 bars per leg, identical tape window, funding fed):

| Instruments | Wall median | Peak RSS | Marginal per added instrument |
|---:|---:|---:|---:|
| 1 | 2.906 s | 248.3 MiB | — |
| 2 | 5.087 s | 286.0 MiB | — |
| 5 | 8.194 s | 382.2 MiB | — |
| 10 | 13.742 s | 547.4 MiB | **+1.204 s, +33.2 MiB** (1→10 average) |

Time and memory are **linear in instrument count** at fixed history length, with a fixed ~2.9 s
single-instrument base. Note the interesting structural fact: the ten-leg run processes 21,600 bars
in 13.7 s while the single-leg run needs 2.8 s for 2,160 bars — per-bar cost is dominated by fixed
process overhead plus per-leg prefix work, not by leg count.

**Extrapolations** (`ESTIMATE`, linear trend from measured points, no measurement):

| Workload | Estimated wall | Estimated peak RSS | 4-GiB verdict |
|---|---:|---:|---|
| 40 instruments × 3 months (86,400 bars) | ~50 s | ~1.57 GiB | plausibly fits |
| 40 instruments × 1 year (262,080 bars) | ~150–200 s | ~4.34 GiB | **exceeds 4 GiB** |

Memory per million bars (marginal, measured, ten-leg shape): **~16.2 GiB per million leg-bars**,
i.e. **~16.5 KiB per leg-bar**. Estimated growth with additional symbols: **+33.2 MiB per symbol**
at 3-month history; with additional history at fixed universe: **+9.2 KiB per bar** per leg.

---

## 7. Optimization opportunities (ranked)

### 1. Make the causal frame incremental — highest value, lowest risk

**Evidence**: hotspots #2, #3, #4 total 4.442 + 4.147 + 1.806 ≈ 10.4 s of a 22.8 s profiled ten-leg
backtest (overlapping, so treat as an upper bound), and they are pure re-verification of data the
process already validated one bar earlier. `CausalFrame.from_ordered_prefix` documents itself as
"O(n) per call"; it is called once per bar per leg. `frame_at_incremental` already exists in
`domain/market.py` and implements exactly the fail-closed tail validation with a full-path fallback,
but the ensemble path does not use it.

**Change**: route `ExpertEnsembleStrategy.on_bar` through the incremental frame, keep the positional
guard, validate only the appended candle, and carry the continuity predicate forward instead of
rescanning the prefix. **Effort S** (the mechanism exists and is tested for parity), **semantic risk
L** (identical accept/reject; falls back to the full path on any doubt), **Amdahl ceiling ~1.6×** on
the profiled ten-leg run before touching anything else.

### 2. Cache shared indicator state across the 28 experts

**Evidence**: hotspot #1 — 14.239 s of 22.786 s (62.5 %) inclusive, driven by 604,800
`observe_expert` calls and 38,186 lazy Polars collects, while the 28 expert bodies' own self time is
0.151 s. `wilder_rsi` alone is 358 calls / 0.475 s in the single-leg profile; `pattern_pivots`
1.161 s in the ten-leg profile.

**Change**: a per-frame, parameter-keyed feature cache so an indicator requested by N experts is
computed once, and incremental indicator state so it is computed over the appended bar rather than
the whole prefix. **Effort L** (touches feature semantics), **semantic risk M** (causal availability
and warm-up conventions must not move), **Amdahl ceiling ~2.7×** if the recomputation were free.

### 3. Collapse per-expert dispatch and audit-record churn

**Evidence**: hotspot #5 — 583,200 `context_reason` calls, 604,800 `stance_record` calls,
604,800 `directional_stance` calls, 3.359 s of helper self time (16.9 %), plus 15.7 MiB of retained
stance mappings and 4.1 MiB of per-stance version strings in the allocation profile.

**Change**: hoist the registry lookup and constant version strings out of the bar loop, dispatch the
28 observers in one pass, and use a compact immutable stance record that preserves audit identity
(same fields, same ordering, no dropped evidence). **Effort M**, **semantic risk M** (audit
identity), **Amdahl ceiling ~1.2×**.

Combined, these are additive only to the extent that the profiled shares are accurate; a realistic
expectation is **1.5–2.5× end-to-end** on the ten-leg workload with no change in decisions, plus a
material reduction in retained memory. Nothing here requires leaving Python, and nothing here
requires touching NautilusTrader: it accounts for ~1.1 % of engine time.

---

## 8. Goose evaluation

Reference: <https://aardappel.github.io/goose/>. No Goose backtester was written and no engine was
rewritten.

**What was actually measured** (prior branch evidence, `reports/performance/20261104T173104Z/`,
parity-verified): the single predicate `CausalFrame.continuous` over all 2,160 prefixes of the same
development window. Production Python 98.20 ms; Goose 0.601 ms; **NumPy 0.084 ms** — on already
prepared arrays. All 2,160 prefix results matched production. Compilation 0.13 s with a warm
toolchain cache. The Amdahl ceiling for removing the predicate *entirely* from a profiled replay was
~1.09×, before FFI marshalling.

**What the new measurement adds**: `continuous` is 4.147 s of a 22.786 s *profiled* ten-leg backtest
(18.2 %), and the work around it — #1 per-expert recomputation at 62.5 %, #5 dispatch at 16.9 % — is
dominated by dynamic Python dispatch, dict/attribute churn, and Polars (already Rust) query planning.
A systems language would have to replace all of that, along with the point-in-time availability
semantics, the OpportunityBook transitions, the decision/audit record contracts and the funding
accounting, to move the number.

**Assessment**:

* Hotspots suitable for compiled code: the prefix validation loop and the continuity scan — i.e.
  exactly the part where a *vectorised* existing-language fix already beats Goose by ~7× on the
  narrow loop (0.084 ms vs 0.601 ms).
* Is Python/Polars/NumPy optimization sufficient? **For the measured bottlenecks, yes** — the
  dominant costs are algorithmic (O(n²) re-verification, 28× recomputation), not interpreter
  arithmetic. Fixing the algorithm removes the work; compiling it only makes the waste faster.
* Would streaming/bounded history reduce memory? The measured curve says memory is linear in
  leg-bars; bounding the retained prefix would bound it, but that is an audit-semantics decision,
  not a language decision.
* Would Goose require duplicating execution and audit semantics? **Yes.** The engine, fills,
  funding settlements, opportunity lifecycle and audit trail are Nautilus + Python contracts today.
* Best future Goose experiment, if any: a single bounded, hot, side-effect-free kernel — the
  validated-incremental frame/continuity step — behind a parity fixture. Speed/RAM advantages beyond
  the predicate remain **UNVERIFIED**; no FFI was implemented, no end-to-end parity was attempted.

**Recommendation: `OPTIMIZE_DATA_PIPELINE`.** Keep NautilusTrader and Python; make the causal frame
and the shared feature computation incremental (that is the data pipeline feeding every expert).
Goose is not justified by measured evidence: it cannot reach the 62.5 % bucket that is Polars query
work and Python dispatch, and the one place it could help is already beaten by NumPy.

---

## 9. Is the 2-vCPU / 4-GiB VPS viable?

**For the measured workloads: demonstrated, with a host-class caveat.**

* Every reported run executed inside a **real, enforced cgroup**: `cpu.max = 200000 100000`
  (2 CPU equivalents), `memory.max = 4294967296`, `memory.swap.max = 4294967296`.
* 10 instruments × 3 months completed in 13.7 s with a 547 MiB peak, `memory.events: oom 0`,
  `cpu.stat: nr_throttled 0`.
* The single-instrument 3-month replay uses 53 % of two vCPUs and 241 MiB — an order of magnitude of
  headroom on both axes.

Caveats that prevent a stronger claim: the container is **ARM Linux on an Apple M3 Pro**, not a
commodity x86 VPS — CPU microarchitecture, memory bandwidth and disk are not representative, so
absolute latencies on a cheap VPS will be worse; the cgroup's CPU quota limits *cycles available*
but does not emulate a slower core. **A 2-vCPU/4-GiB VPS is therefore demonstrated plausible for
B0–B3-style workloads (≤10 instruments, ≤3 months) and unsupported, by extrapolation (~4.3 GiB), for
a 40-instrument 1-year replay.**

---

## 10. Limitations — what is NOT measured

**NOT_RUN**
* `B2` — 1 instrument × 1 year: no 12-month window inside the authorized BTC development window.
* `B4` — 40 instruments × 3 months: real hourly tape exists for 10 symbols only; no synthetic
  40-leg replay was substituted.
* `B5` — 40 instruments × 1 year: neither the universe nor the window exists locally.
* A long-history quadratic fit: three prefix lengths (720/1,440/2,160) cannot isolate the n² term.

**NOT_MEASURED**
* Per-phase memory attribution (`peak during data loading`, `during strategy`, `during
  audit/reporting`) for the ten-leg portfolio path: no stage trace exists for
  `run_portfolio_backtest`. Only process-level samples and grouped phase labels are reported.
* Allocation ownership of the ~16.2 KiB per leg-bar growth: native/Rust/Arrow shares are
  **UNATTRIBUTED**; the only traced figures come from a separate `tracemalloc` run on the
  single-instrument path.
* Cold-start vs warm-start for the ten-leg portfolio path as separate triples (only one container's
  first-run penalty was observed, on the single-leg path).
* Swap usage, major faults, cgroup `memory.peak` per-run attribution: `memory.events` stayed clean
  and `memory.peak` inside the container namespace did not track the process maximum, so it is not
  used for attribution.
* Energy, disk throughput under contention, NUMA effects: out of scope.

**BLOCKED**
* MarketBelief and the unified V9 Trader Decision components are **NOT_IMPLEMENTED** in this
  checkout. No cost is reported for them; nothing imaginary is included.

**NOT USED AS EVIDENCE**
* `tests/test_perf_hot_paths.py` and `tests/test_vectorized_indicators_perf.py` exist at this base
  SHA but assert microbenchmark *thresholds*, so neither is used as a measurement here. They do
  confirm two useful facts about intent: the flat `stance_record` record replaced `dataclasses.asdict`
  in the hottest loop, and feature/indicator helpers are expected to be loop-free Polars.

**UNVERIFIED**
* Behaviour on a genuine x86 Linux VPS.
* Any end-to-end Goose parity, FFI cost, or memory saving.
* Whether removing prefix re-validation changes decisions: `frame_at_incremental` claims parity and
  has fixtures, but no end-to-end decision-diff was run for the ensemble path.
* Profitability or economic performance of anything measured here. This is a performance record;
  no synthetic data was used in any reported timing or memory row, and no economic claim is made.

---

## 11. Reproducing

`reproduce.sh` rebuilds the pinned ARM Linux image, verifies the 60 tape archives against their
vendor checksums, runs the triples, the scaling sweeps, the stage trace, the per-expert trace, the
cProfile runs and the 100 ms memory timelines, then regenerates `measurements.csv` and
`memory_timeline.csv` from `evidence/`. Raw `pstats` binaries are **not** committed: they are
regenerated into a temporary directory by `reproduce.sh`; only their text summaries are kept in
`evidence/profile/`.

Prior investigation directories (`reports/performance/20261104T1*/`) are retained unchanged as
branch history and are cited where this report relies on them (the allocation snapshot in §3 and the
Goose predicate comparison in §8).

---

## 12. Optimisation results — same output, less compute

Sections 1–11 benchmark the system as it was. This section records what was then changed, what it
bought, and what had to be rejected because it could not be proved byte-identical.

**Hard constraint**: every change had to leave the backtest output bit-identical — decisions, fills,
opened/closed positions, opportunity-book transitions and audit events. That is enforced by a frozen
digest gate, not by inspection: `parity_capture.py` replays the unmodified production adapters and
hashes every section; the baselines in `parity/baseline/` were captured from commit `bfa7613`
**before** any optimisation and are never regenerated from optimised code. `parity_diff.py` fails
closed on any divergence. B0 and B1 compare 16 sections including the full audit stream (2,742 and
7,995 events); B3 compares 30 sections covering 21,600 decisions.

### 12.1 Measurement basis — read this before comparing numbers

The A/B below was measured **on the host** (Apple M3 Pro, macOS, native, no container), each stage
back to back in a single session with one discarded warm-up and three fresh processes per workload
per stage. The 2 vCPU / 4 GiB container used in §1–§11 was deleted at the user's request, so the
absolute figures here are **not** comparable with §1–§11 and must not be mixed with them: only the
ratios are meaningful, and they are host ratios. Container measurements remain the only
constrained-resource and memory-timeline evidence.

Workloads are the same real tape: B1 = BTCUSDT Jan–Mar 2025 (2,160 bars), B3 = ten symbols
Jan–Mar 2025 (21,600 leg-bars) with funding settlements.

### 12.2 What changed

| # | Change | Mechanism | Parity argument |
|---|---|---|---|
| 1 | Incremental causal frame | `extend_causal_frame` appends one candle to the previous frame; continuity is carried forward (`_continuous_hint`) instead of rescanned; anything doubtful returns `None` and the old full path runs | the fast path only accepts the exact next candle of the caller-owned prefix; `tests/test_domain_frame_incremental.py` pins that it never invents a verdict on duplicate/overlapping/late/regressed/mismatched input |
| 2 | Leaner dispatch | per-spec cached witness version string, cached 28 canonical specs, `observe_spec` entry point, single-allocation stance stamping, positional `Stance` construction with cached family versions | same fields, same values, same ordering; only the number of allocations changed |
| 3 | Per-bar indicator memo | `per_bar_memo` caches pure indicator results on the per-bar bundle that `observe_all_28` installs and clears; key carries the bar-owned series identity plus arguments, so a value cannot outlive its prefix | measured duplicates only: 368 of 492 (bar, indicator) pairs were recomputing an identical result; outside a bundle nothing is cached; `tests/test_experts_bar_memo.py` pins value equality and no cross-bar reuse |
| 4 | Cheaper records | `stance_record` is one `dict` copy instead of ten `getattr`; `stance_with` stamps from the instance dict | identical field order and values |

### 12.3 Attribution (host, 3 fresh processes per cell, medians)

| Stage | B1 wall | B1 cumulative | B3 wall | B3 cumulative | B3 peak RSS |
|---|---|---|---|---|---|
| `bfa7613` pre-optimisation | 1.804 s | 1.00× | 9.045 s | 1.00× | 459.3 MiB |
| `278d404` incremental frame | 1.424 s | 1.27× | 5.271 s | 1.72× | 460.4 MiB |
| `bc68f49` dispatch | 1.354 s | 1.33× | 4.621 s | 1.96× | 414.1 MiB |
| `87fc4df` indicator memo + records | **1.059 s** | **1.70×** | **3.836 s** | **2.36×** | 414.5 MiB |

Net: single leg **1.70×** (1.804 → 1.059 s, min 1.055 / max 1.076), ten legs **2.36×**
(9.045 → 3.836 s, min 3.784 / max 3.846), ten-leg peak RSS **−9.8 %** (459.3 → 414.5 MiB) because
per-stance version strings are now shared instead of rebuilt. Reproduce with
`reports/performance/reproduce_optimization.sh` (no Docker). Raw per-run JSON:
`evidence/optimized/optimization_attribution.csv`.

Same machine, profiled ten-leg run: **12.013 s → 5.646 s** of profiled time and **45.7 M → 18.2 M**
function calls. The three pre-optimisation hotspots — `__post_init__` 1.468 s, the continuity
generator 1.384 s (23.3 M calls), `from_ordered_prefix` 1.067 s, `builtins.all` 0.820 s — are gone
from the profile entirely; `extend_causal_frame` costs 0.202 s inclusive.

### 12.4 Hotspot ranking after the work

| Rank | Component | Self time | Share of profiled total |
|---|---|---|---|
| 1 | `decisions.py:stance_record` (604,800 calls) | 0.465 s | 8.2 % |
| 2 | `registry.py:_with_witness` (604,800 calls) | 0.391 s | 6.9 % |
| 3 | `expert_strategy.py:on_bar` self | 0.311 s | 5.5 % |
| 4 | `PyLazyFrame.collect` (22,272 calls, 38,186 before) | 0.299 s | 5.3 % |
| 5 | `registry.py:observe_spec` self | 0.271 s | 4.8 % |
| 6 | `common.py:directional_stance` self | 0.239 s | 4.2 % |
| 7 | native `BacktestEngine.add_data` | 0.232 s | 4.1 % |

What remains is **per-stance bookkeeping on bars where nothing happens**, not computation: the
engine evaluates all 28 experts on all 21,600 bars while only ~5.7 % of bars (123 of 2,160 per leg)
ever open an opportunity. The next order-of-magnitude win is not a faster version of this loop but
a proven no-op fast path (or dropping to 27 experts' worth of work when nothing is in play); it
changes what the audit stream contains unless it is proved equivalent, so it was **not** attempted
here.

### 12.5 Rejected, with reason

- **Incremental indicator state over the appended bar** (the "maintain RSI/ATR/pivots from the
  previous bar" half of the §7 item 2 proposal). Polars `ewm_mean` and `rolling_*` results depend on
  the exact window handed to them; a bounded-tail reimplementation is not provably bit-identical for
  every warm-up and null path, and after the memo the entire Polars block is 1.0 s of a 5.6 s
  profiled run. Risk outweighed the measurable gain, so only the duplicate-work half shipped.
- **Skipping expert bodies on no-opportunity bars**: experts may compute before their
  `context_reason` guard returns, so an external guard could change what is recorded. Diverges by
  construction; not attempted.
- **Goose / Rust rewrite, engine replacement, streaming or bounded-history redesign**: out of scope
  by the task's stated boundaries, and §5/#10 already shows native engine work at ~4 %.
- Note also that §7's headline hypothesis — "62.5 % is per-expert full-history indicator
  recomputation" — did not survive the measurement: `observe_all_28`'s inclusive time was dominated
  by dispatch and stance construction, and indicators are requested well under once per bar on quiet
  bars. The dispatch and per-bar-sharing fixes address the real cost; the 2.36× measured is close to
  the 2.7× Amdahl ceiling that hypothesis was credited with only because both touched the same
  inclusive slice.

### 12.6 Gates

| Gate | Result |
|---|---|
| Parity B0 / B1 / B3 vs `bfa7613` baselines | PASS — 16 / 16 / 30 sections byte-identical |
| Test suite | 1,089 passed, 66 skipped, 506 deselected |
| `ruff check src tests tools` | clean |
| `mypy src` | clean (208 files) |
| New tests | `test_performance_parity.py`, `test_domain_frame_incremental.py`, `test_experts_bar_memo.py` |

---

## 13. Long-history check on real 4-year tape (10 symbols)

Section 6 could only sweep 3 months per leg, because the authorised development window is Jan–Mar
2025. The vendor capture set `multi-1h-4y` does hold 48 months (2022-07 … 2026-06) for the same ten
symbols, so the *performance* question "what does a long backtest cost" was measured there. This is
a timing and memory load only: no economic claim is made on data outside the authorised window, and
nothing here is an out-of-sample result.

| Workload | Leg-bars | Wall | Peak RSS | ms / leg-bar | KiB / leg-bar | Fills | Funding settlements |
|---|---|---|---|---|---|---|---|
| 10 symbols × 3 months (B3) | 21,600 | 3.836 s (median of 3) | 0.40 GiB | 0.178 | 19.65 | 203 | 2,690 |
| 1 symbol × 4 years | 35,040 | 45.5 s (1 run) | 0.59 GiB | 1.300 | 17.77 | 1,190 | 4,380 |
| **10 symbols × 4 years** | **350,400** | **286.2 s** | **3.65 GiB** | **0.817** | **10.91** | 2,799 | 43,880 |
| 40 symbols × 4 years | 1,401,600 | ~19.1 min `ESTIMATE` | ~14.6 GiB `ESTIMATE` | — | — | — | — |

Raw rows: `evidence/optimized/long_history_scaling.csv`.

Three things this settles:

1. **Cost is linear in instruments but not in history length.** Ten 4-year legs cost 74.6× the
   ten 3-month legs while carrying 16.2× the bars, so cost per leg-bar rises from 0.178 to 0.817 ms
   as a leg gets longer (a single 4-year leg runs at 1.300 ms/bar against 0.490 ms/bar for 2,160
   bars). Per-instrument series are independent, so adding instruments stays near-linear; extending
   history does not. Anything that keeps per-bar state proportional to the prefix — indicator
   windows over the whole prefix on opportunity bars, the opportunity book, the retained decision
   list — shows up here.
2. **A 4 GiB VPS holds ten symbols for four years, barely.** 3.65 GiB peak for 350,400 leg-bars
   gives ~0.35 GiB of headroom. It fits, but there is no room for a second concurrent run and no
   safety margin for a larger decision payload.
3. **40 symbols × 4 years is still out of reach, and cannot even be measured here.** The real tape
   has ten symbols, so 40 is `NOT_RUN` rather than substituted with synthetic data; the linear
   extrapolation (~19 min, ~14.6 GiB) exceeds a 4 GiB VPS by roughly 3.6× in memory. Speed is no
   longer the binding constraint — retained state per leg is.

Also fixed while loading this tape: `load_tape_candles` parsed each monthly archive with Polars'
default first-100-rows schema inference, and archives that open with integer-looking volumes and
later carry fractional ones failed the whole load (`could not parse 1017592.90 as dtype i64`).
Inference is now full-file, which is identical for every archive that already parsed and unblocks
the rest (§ commit `6632cdd`). Parity against the frozen manifests is unchanged.
